"""Fase 10, trinn 4: samlet sluttkontroll og ende-til-ende-integrasjonstest.

Ingen nye funksjoner - dette er en helhetlig kontroll av flyten bygget i trinn 1-3
(tests/test_import_deltakere.py, tests/test_import_deltakere_rute.py,
tests/test_import_deltakere_bekreft.py), via de faktiske HTTP-rutene, pluss en generell
personvernsveip av import_forhaandsvisning/hendelse/sensitivt etter en sammensatt kjoring.
"""
import io
from datetime import date, timedelta

import pytest

from adressehjelp import ADRESSE
from adressehjelp import ADRESSE, csv_med_adresse
from kurs import config, db, import_deltakere as imp
from kurs.integrasjoner import epost, visma

RUTE_NY = "/admin/kurs/{kid}/deltaker/ny"
RUTE_IMPORT = "/admin/kurs/{kid}/deltakere/importer"
RUTE_BEKREFT = "/admin/kurs/{kid}/deltakere/importer/bekreft"
RUTE_MAL = "/admin/deltaker-import-mal.csv"
RUTE_DELTAKERE = "/admin/kurs/{kid}/deltakere"
CSV_HEADER = "Fornavn;Etternavn;E-post;Telefon;Yrkestittel;Arbeidssted;HPR-nummer;Betaler;Firmanavn;Org.nr;" \
             "Fakturaadresse;Fakturareferanse;Fakturakommentar;Allergier;Tilrettelegging"
MIN_HEADER = "Fornavn;Etternavn;E-post"  # bare de obligatoriske kolonnene


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    return c


def _klient():
    from kurs.web import app as webapp
    return webapp.app.test_client()


def _logg_inn(klient):
    klient.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})


def _kurs(con, kode="T1", start=None, **kw):
    start = start or (date.today() + timedelta(days=30))
    return db.opprett_kurs(con, kode=kode, navn="Testkurs", datoer=[start.isoformat()],
                           sharepoint_mappe=f"Kurs/{kode}", **{"pris_nok": 1000, **kw})


def _admin_id(con):
    return con.execute("SELECT id FROM admin_bruker WHERE brukernavn=?", (config.ADMIN_BRUKERNAVN,)).fetchone()[0]


def _antall_mail(con):
    return len(list(config.UTBOKS.glob("*.html"))) if config.UTBOKS.exists() else 0


def _csv(*rader: str, header: str = CSV_HEADER, adresse: bool = True) -> bytes:
    return csv_med_adresse(header, rader, adresse)


def _last_opp(klient, kid, innhold: bytes, filnavn="import.csv"):
    return klient.post(RUTE_IMPORT.format(kid=kid), data={"fil": (io.BytesIO(innhold), filnavn)},
                       content_type="multipart/form-data")


def _hent_token(html: str) -> str:
    start = html.index('name="forhaandsvisning_token"')
    felt = html[start:start + 200]
    verdi_start = felt.index('value="') + len('value="')
    return felt[verdi_start:felt.index('"', verdi_start)]


def _snapshot(con):
    return (
        con.execute("SELECT COUNT(*) FROM deltaker").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM sensitivt").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0],
    )


# ==================== A-M: full flyt med alle forhaandsvisningskombinasjoner ====================

def test_full_flyt_alle_kombinasjoner_og_atomisk_bekreft(con):
    kid1 = _kurs(con, "K1")  # for aa gjore "kjent@x.no" til en kjent person paa et ANNET kurs
    kid = _kurs(con, "K2", kapasitet=3, pris_nok=1000, fakturering="person", type="fysisk")
    db.meld_paa(con, kid1, epost="kjent@x.no", fornavn="Kjent", etternavn="Person", deltaker={"telefon": "11111111"})
    db.meld_paa(con, kid, epost="forst@x.no", fornavn="Forst", etternavn="Test")  # bekreftet (1/3)
    db.meld_paa(con, kid, epost="allerede@x.no", fornavn="Allerede", etternavn="Test")  # bekreftet (2/3)
    pid_gjenganger, _ = db.meld_paa(con, kid, epost="gjenganger@x.no", fornavn="Gjenganger", etternavn="Test")
    db.meld_av(con, pid_gjenganger)  # avmeldt paa DETTE kurset - kan reaktiveres
    con.commit()

    # B. Last ned CSV-mal
    klient = _klient()
    _logg_inn(klient)
    mal_resp = klient.get(RUTE_MAL)
    assert mal_resp.status_code == 200
    assert mal_resp.mimetype == "text/csv"

    # A + C. Deltakere-fanen har knappen, last opp CSV som daekker ALLE kombinasjonene
    liste = klient.get(RUTE_DELTAKERE.format(kid=kid)).get_data(as_text=True)
    assert RUTE_IMPORT.format(kid=kid) in liste

    rader_csv = "\n".join([
        "Allerede;Test;allerede@x.no",         # hopp over (allerede aktiv paameldt)
        "Helt;Ny;helt.ny@x.no",                # ny -> bekreftet (fyller siste ledige plass, 3/3)
        "Gjenganger;Test;gjenganger@x.no",     # reaktiver -> venteliste (kurset na fullt)
        "Kjent;Person;kjent@x.no;99999999",    # ny (paa DETTE kurset) + eksisterende person + venteliste
    ])
    foer = _snapshot(con)
    resp = _last_opp(klient, kid, _csv(rader_csv, header="Fornavn;Etternavn;E-post;Telefon"))
    tekst = resp.get_data(as_text=True)

    # D. Forhaandsvisningen viser alle forventede kombinasjoner
    assert "Allerede påmeldt – hoppes over" in tekst
    assert "Ny påmelding" in tekst
    assert "Reaktiveres" in tekst
    assert "Eksisterende person" in tekst
    assert '<span class="merke ok">Påmeldt</span>' in tekst and "Bekreftet" not in tekst
    assert "Venteliste" in tekst

    # E. Ingenting lagret av ren forhaandsvisning
    assert _snapshot(con) == foer

    token = _hent_token(tekst)

    # F + G + H. Admin bekrefter -> atomisk import, tydelig resultat
    resp2 = klient.post(RUTE_BEKREFT.format(kid=kid), data={"forhaandsvisning_token": token})
    assert resp2.status_code == 302
    tekst2 = klient.get(resp2.headers["Location"]).get_data(as_text=True)
    start = tekst2.index('class="flash ok"')
    flash = tekst2[start:tekst2.index("</div>", start)]
    assert "Import fullført" in flash
    assert "3 deltakere registrert" in flash  # helt.ny + gjenganger(reaktivert) + kjent
    assert "1 påmeldt" in flash
    assert "2 venteliste" in flash
    assert "reaktivert" in flash
    assert "hoppet over" in flash

    # I. Preview-data slettet
    assert con.execute("SELECT COUNT(*) FROM import_forhaandsvisning").fetchone()[0] == 0

    # J. kilde/sveiper_utsatt/sveiper_kjort korrekt paa de FAKTISK importerte/reaktiverte radene
    for epost_ in ("helt.ny@x.no", "gjenganger@x.no", "kjent@x.no"):
        rad = con.execute(
            "SELECT p.kilde, p.sveiper_kjort, p.sveiper_utsatt FROM paamelding p "
            "JOIN deltaker d ON d.id=p.deltaker_id WHERE d.epost=? AND p.kurs_id=?", (epost_, kid)).fetchone()
        assert rad["kilde"] == "admin_import"
        assert rad["sveiper_kjort"] == 0
        assert rad["sveiper_utsatt"] == 1

    # "allerede" ble hoppet over - INGEN ny/endret rad for den
    assert con.execute(
        "SELECT COUNT(*) FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id "
        "WHERE d.epost='allerede@x.no' AND p.kurs_id=?", (kid,)).fetchone()[0] == 1

    # N. Eksisterende personopplysninger (telefon fra kid1) IKKE overskrevet av CSV-verdien
    rad_kjent = con.execute("SELECT telefon FROM deltaker WHERE epost='kjent@x.no'").fetchone()
    assert rad_kjent["telefon"] == "11111111"

    # K + L + M. Ingen e-post, faktura eller Visma-kall
    assert _antall_mail(con) == 0
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 0


def test_blokkerende_rad_vises_i_forhaandsvisning_uten_aa_lagre_noe(con):
    """D (blokkerende feil) + E, testet separat siden en blokkerende fil aldri faar et token."""
    kid = _kurs(con, kapasitet=5)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    foer = _snapshot(con)
    tekst = _last_opp(klient, kid, _csv("Ugyldig;Test;ikke-en-epost", header=MIN_HEADER)).get_data(as_text=True)
    assert "Blokkert" in tekst
    assert 'name="forhaandsvisning_token"' not in tekst
    assert _snapshot(con) == foer
    assert con.execute("SELECT COUNT(*) FROM import_forhaandsvisning").fetchone()[0] == 0


def test_ingen_visma_kalles_i_full_flyt(con, monkeypatch):
    kid = _kurs(con, kapasitet=5, pris_nok=1000, fakturering="person")
    con.commit()

    def _skal_ikke_kalles(*a, **kw):
        raise AssertionError("visma.fakturer() skal ALDRI kalles av import")
    monkeypatch.setattr(visma, "fakturer", _skal_ikke_kalles)

    def _skal_ikke_kalles_epost(*a, **kw):
        raise AssertionError("epost.send() skal ALDRI kalles av import")
    monkeypatch.setattr(epost, "send", _skal_ikke_kalles_epost)

    klient = _klient()
    _logg_inn(klient)
    tekst = _last_opp(klient, kid, _csv("Kari;Test;kari@x.no", header=MIN_HEADER)).get_data(as_text=True)
    token = _hent_token(tekst)
    resp = klient.post(RUTE_BEKREFT.format(kid=kid), data={"forhaandsvisning_token": token})
    assert resp.status_code == 302


# ==================== O: ordinaere paameldingsveier uendret ====================

def test_offentlig_paamelding_overskriver_fortsatt_eksisterende_felt(con):
    """Regresjon: fase 10 sin beskyttelsesmodus (beskytt_eksisterende_felt=True) er en eksplisitt
    opt-in KUN for import - den EKTE offentlige paameldingsruten (/kurs/<kode>) skal fortsatt
    kunne oppdatere kontaktinfo som for, uendret av fase 10."""
    kid1 = _kurs(con, "K1")
    _kurs(con, "K2")
    con.commit()
    db.meld_paa(con, kid1, epost="kari@x.no", fornavn="Kari", etternavn="Test", deltaker={"telefon": "11111111"})
    con.commit()
    klient = _klient()
    resp = klient.post("/kurs/K2", data={
        "fornavn": "Kari", "etternavn": "Test", "epost": "kari@x.no", "telefon": "99999999", "samtykke": "on", **ADRESSE})
    assert resp.status_code == 200
    rad = con.execute("SELECT telefon FROM deltaker WHERE epost='kari@x.no'").fetchone()
    assert rad["telefon"] == "99999999"  # OPPDATERT - motsatt av importens beskyttede modus


# ==================== P/Q/R: kursstatus og frist via faktiske ruter ====================

@pytest.mark.parametrize("status", ["utkast", "aapen", "full", "aktiv"])
def test_tillatte_statuser_kan_importeres_og_bekreftes(con, status):
    kid = _kurs(con, kapasitet=5)
    con.execute("UPDATE kurs SET status=? WHERE id=?", (status, kid))
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = _last_opp(klient, kid, _csv("Kari;Test;kari@x.no", header=MIN_HEADER)).get_data(as_text=True)
    token = _hent_token(tekst)
    resp = klient.post(RUTE_BEKREFT.format(kid=kid), data={"forhaandsvisning_token": token})
    assert resp.status_code == 302
    assert con.execute("SELECT COUNT(*) FROM paamelding WHERE kurs_id=?", (kid,)).fetchone()[0] == 1


@pytest.mark.parametrize("status", ["avlyst", "avsluttet"])
def test_blokkerte_statuser_gir_ingen_preview(con, status):
    kid = _kurs(con)
    con.execute("UPDATE kurs SET status=? WHERE id=?", (status, kid))
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = _last_opp(klient, kid, _csv("Kari;Test;kari@x.no", header=MIN_HEADER)).get_data(as_text=True)
    assert status in tekst.lower()
    assert con.execute("SELECT COUNT(*) FROM import_forhaandsvisning").fetchone()[0] == 0


def test_paameldingsfrist_blokkerer_ikke_adminimport(con):
    kid = _kurs(con, kapasitet=5, paameldingsfrist=(date.today() - timedelta(days=1)).isoformat())
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = _last_opp(klient, kid, _csv("Kari;Test;kari@x.no", header=MIN_HEADER)).get_data(as_text=True)
    token = _hent_token(tekst)
    resp = klient.post(RUTE_BEKREFT.format(kid=kid), data={"forhaandsvisning_token": token})
    assert resp.status_code == 302
    assert con.execute("SELECT COUNT(*) FROM paamelding WHERE kurs_id=?", (kid,)).fetchone()[0] == 1


# ==================== samlet personvernkontroll ====================

def test_samlet_personvernkontroll_hele_fase_10_flyten(con):
    """Kjorer opplasting (med allergi/faktura/telefon), en blokkert fil, en reaktivering og en
    vellykket bekreft - og sveiper deretter hendelse-tabellen for personopplysninger."""
    kid = _kurs(con, kode="PV1", type="fysisk", kapasitet=5, pris_nok=1000, fakturering="person")
    con.commit()
    klient = _klient()
    _logg_inn(klient)

    # blokkert forsok (skal ikke logge noe med persondata heller)
    _last_opp(klient, kid, _csv(
        "Hemmelig;Blokkert;ikke-en-epost;;;;;;;;;;;Skalldyrallergi;Rullestol", header=CSV_HEADER))

    # gyldig, sensitiv import
    tekst = _last_opp(klient, kid, _csv(
        "Hemmelig;Person;hemmelig.person@sensitiv-domene.no;98765432;Psykolog;Skjult Klinikk AS;;"
        "organisasjon;Skjult Bedrift AS;999900003;Skjult Vei 1;Konfidensiell ref;Ikke vis dette;"
        "Peanøttallergi;Tegnspråktolk", header=CSV_HEADER)).get_data(as_text=True)
    token = _hent_token(tekst)
    klient.post(RUTE_BEKREFT.format(kid=kid), data={"forhaandsvisning_token": token})

    forbudte_tekster = [
        "hemmelig", "skalldyrallergi", "rullestol", "hemmelig.person@sensitiv-domene.no",
        "98765432", "psykolog", "skjult klinikk", "skjult bedrift", "999900003", "skjult vei",
        "eksempel kommune", "postboks 100",          # firmaopplysningene fra registeret hører heller ikke hjemme i loggen
        "konfidensiell ref", "ikke vis dette", "peanøttallergi", "tegnspråktolk", "import.csv",
    ]
    rader = con.execute("SELECT handling, detaljer FROM hendelse").fetchall()
    assert len(rader) > 3
    for r in rader:
        detaljer = (r["detaljer"] or "").lower()
        for tekst_ in forbudte_tekster:
            assert tekst_ not in detaljer, f"'{tekst_}' funnet i hendelse.detaljer for handling={r['handling']!r}"

    # sensitivt-tabellen SKAL ha dataene (det er der de hoerer hjemme)
    sensitivt = con.execute(
        "SELECT allergier, tilrettelegging FROM sensitivt s JOIN paamelding p ON p.id=s.paamelding_id "
        "JOIN deltaker d ON d.id=p.deltaker_id WHERE d.epost='hemmelig.person@sensitiv-domene.no'").fetchone()
    assert sensitivt["allergier"] == "Peanøttallergi"

    # import_forhaandsvisning er tom igjen (bekreftet -> slettet; blokkert -> aldri lagret)
    assert con.execute("SELECT COUNT(*) FROM import_forhaandsvisning").fetchone()[0] == 0


def test_ingen_filnavn_i_hendelseslogg(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    _last_opp(klient, kid, _csv("Kari;Test;kari@x.no", header=MIN_HEADER),
             filnavn="veldig_gjenkjennelig_filnavn_med_navn_kari_nordmann.csv")
    for r in con.execute("SELECT detaljer FROM hendelse"):
        assert "veldig_gjenkjennelig_filnavn" not in (r["detaljer"] or "").lower()


# ==================== punkt 6: ingen parallell paameldingsmotor (kodekontroll) ====================

def test_import_modulen_kaller_kun_eksisterende_db_funksjoner(con):
    """Sikrer (ved kodeinnhold, ikke bare oppforsel) at import_deltakere.py fortsatt bruker
    db.meld_paa()/finn_eller_opprett_deltaker() og ALDRI importerer epost/visma/sveiper direkte -
    en strukturell vakt mot at noen ved en feiltakelse bygger en parallell motor senere."""
    import inspect
    from kurs import import_deltakere as m
    kildekode = inspect.getsource(m)
    assert "db.meld_paa(" in kildekode
    assert "import epost" not in kildekode
    assert "import visma" not in kildekode
    assert "import sveiper" not in kildekode
    assert "from .integrasjoner" not in kildekode
