"""Fase 10, trinn 2: opplastingsrute + forhaandsvisning for CSV-import.

Selve bekreft/import-ruten (trinn 3) er IKKE bygget ennaa - preview-knappen er bevisst inaktiv.
"""
import io
from datetime import date, datetime, timedelta

import re

import pytest

from adressehjelp import ADRESSE, csv_med_adresse
from kurs import config, db, import_deltakere as imp

RUTE = "/admin/kurs/{kid}/deltakere/importer"
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


def _csv(*rader: str, header: str = CSV_HEADER, adresse: bool = True) -> bytes:
    return csv_med_adresse(header, rader, adresse)


def _last_opp(klient, kid, innhold: bytes, filnavn="import.csv"):
    return klient.post(RUTE.format(kid=kid), data={"fil": (io.BytesIO(innhold), filnavn)},
                       content_type="multipart/form-data")


def _antall_preview_rader(con):
    return con.execute("SELECT COUNT(*) FROM import_forhaandsvisning").fetchone()[0]


# ==================== tilgang ====================

def test_ruten_krever_admin_get(con):
    kid = _kurs(con)
    con.commit()
    resp = _klient().get(RUTE.format(kid=kid))
    assert resp.status_code == 302
    assert "/admin/logg-inn" in resp.headers["Location"]


def test_ruten_krever_admin_post(con):
    kid = _kurs(con)
    con.commit()
    resp = _last_opp(_klient(), kid, _csv("Kari;Nordmann;kari@x.no"))
    assert resp.status_code == 302
    assert "/admin/logg-inn" in resp.headers["Location"]
    assert _antall_preview_rader(con) == 0


# ==================== GET / knapp ====================

def test_get_viser_opplastingsskjema(con):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get(RUTE.format(kid=kid)).get_data(as_text=True)
    assert "csv" in tekst.lower()
    assert "2 MB" in tekst
    assert "300" in tekst
    assert 'enctype="multipart/form-data"' in tekst


def test_knapp_vises_pa_deltakere_fanen(con):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    assert RUTE.format(kid=kid) in tekst
    assert "Importer deltakere" in tekst


# ==================== gyldig fil -> preview ====================

def test_gyldig_csv_gir_preview(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = _last_opp(klient, kid, _csv("Kari;Nordmann;kari@x.no", header=MIN_HEADER))
    tekst = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Kari Nordmann" in tekst
    assert "kari@x.no" in tekst
    assert "Ny påmelding" in tekst
    assert '<span class="merke ok">Påmeldt</span>' in tekst and "Bekreftet" not in tekst


def test_preview_skriver_ingenting_til_databasen(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()

    def _snapshot():
        return (
            con.execute("SELECT COUNT(*) FROM deltaker").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM sensitivt").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM hendelse").fetchone()[0],
        )
    klient = _klient()
    _logg_inn(klient)                             # innlogging logges - foer tilstandsbildet
    foer = _snapshot()
    _last_opp(klient, kid, _csv("Kari;Nordmann;kari@x.no", header=MIN_HEADER))
    assert _snapshot() == foer


def test_gyldig_preview_oppretter_server_side_forhaandsvisning(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    _last_opp(klient, kid, _csv("Kari;Nordmann;kari@x.no", header=MIN_HEADER))
    rad = con.execute("SELECT kurs_id, admin_id FROM import_forhaandsvisning").fetchone()
    assert rad is not None
    assert rad["kurs_id"] == kid
    assert rad["admin_id"] == _admin_id(con)


def test_nettleseren_far_bare_token_ikke_json_med_radene(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = _last_opp(klient, kid, _csv("Kari;Nordmann;kari@x.no", header=MIN_HEADER)).get_data(as_text=True)
    start = tekst.index('name="forhaandsvisning_token"')
    felt = tekst[start:start + 200]
    verdi_start = felt.index('value="') + len('value="')
    verdi = felt[verdi_start:felt.index('"', verdi_start)]
    assert len(verdi) < 100
    assert "{" not in verdi and "navn" not in verdi.lower()


def test_bekreft_knapp_peker_til_bekreft_ruten(con):
    """Fra og med trinn 3 er 'Bekreft import'-knappen en fungerende POST til bekreft-ruten -
    se tests/test_import_deltakere_bekreft.py for selve bekreft-flyten."""
    kid = _kurs(con, kapasitet=5)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = _last_opp(klient, kid, _csv("Kari;Nordmann;kari@x.no", header=MIN_HEADER)).get_data(as_text=True)
    assert f"/admin/kurs/{kid}/deltakere/importer/bekreft" in tekst
    assert not re.search(r"<button[^>]*\sdisabled", tekst)


# ==================== blokkerende fil ====================

def test_blokkerende_fil_oppretter_ingen_preview(con):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = _last_opp(klient, kid, _csv("Ugyldig;Rad;ikke-en-epost", header=MIN_HEADER))
    tekst = resp.get_data(as_text=True)
    assert "Blokkert" in tekst
    assert _antall_preview_rader(con) == 0
    assert 'name="forhaandsvisning_token"' not in tekst


def test_dublett_i_fil_blokkerer_og_lager_ingen_preview(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    _last_opp(klient, kid, _csv("Kari;Nordmann;kari@x.no", "Kari;Nordmann;kari@x.no", header=MIN_HEADER))
    assert _antall_preview_rader(con) == 0


# ==================== filtype / størrelse ====================

def test_ugyldig_filtype_avvises(con):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = _last_opp(klient, kid, b"fornavn,etternavn,epost\nKari,Nordmann,kari@x.no", filnavn="import.txt")
    assert resp.status_code == 400
    assert _antall_preview_rader(con) == 0


def test_ingen_fil_valgt_avvises(con):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = klient.post(RUTE.format(kid=kid), data={}, content_type="multipart/form-data")
    assert resp.status_code == 400


def test_fil_paa_akkurat_maksgrensen_aksepteres_forbi_transportlaget(con):
    """En CSV som er NOYAKTIG MAKS_FILSTORRELSE skal IKKE avvises bare fordi multipart-overhead
    (grenser/hoder rundt selve filfeltet) gjor den totale request-kroppen litt storre enn
    filgrensen alene. Marginen (MAKS_REQUEST_BYTES_IMPORT) maa daekke dette. Selve INNHOLDET her
    er bevisst ugyldig (ett enormt felt) - poenget er kun aa bekrefte at vi kommer FORBI
    transport-lagets storrelsessjekker og faktisk naar parse_csv()/innholdsvalidering."""
    kid = _kurs(con)
    con.commit()
    header = b"Fornavn;Etternavn;E-post\n"
    innhold = header + b"x" * (imp.MAKS_FILSTORRELSE - len(header))
    assert len(innhold) == imp.MAKS_FILSTORRELSE
    klient = _klient()
    _logg_inn(klient)
    resp = _last_opp(klient, kid, innhold)
    assert resp.status_code != 413
    tekst = resp.get_data(as_text=True).lower()
    assert "for stor" not in tekst  # kom forbi begge storrelsessjekkene


def test_for_stor_fil_innenfor_request_grensen_avvises(con):
    """Fil som er over MAKS_FILSTORRELSE, men under den route-spesifikke request-grensen -
    fanges av vaar egen eksplisitte stream-lesing (les maks MAKS_FIL_BYTES + 1)."""
    kid = _kurs(con)
    con.commit()
    stor = b"Fornavn;Etternavn;E-post\n" + b"x" * (imp.MAKS_FILSTORRELSE + 100)
    klient = _klient()
    _logg_inn(klient)
    resp = _last_opp(klient, kid, stor)
    assert resp.status_code == 400
    assert "stor" in resp.get_data(as_text=True).lower()
    assert _antall_preview_rader(con) == 0


def test_ekstremt_stor_request_avvises_for_hele_kroppen_leses(con):
    """Langt over den route-spesifikke request-grensen - skal avvises av Werkzeug/Flask sin egen
    per-request max_content_length, ikke feile med en generisk 500."""
    kid = _kurs(con)
    con.commit()
    ekstremt_stor = b"Fornavn;Etternavn;E-post\n" + b"x" * (imp.MAKS_FILSTORRELSE + 2 * 1024 * 1024)
    klient = _klient()
    _logg_inn(klient)
    resp = _last_opp(klient, kid, ekstremt_stor)
    assert resp.status_code in (400, 413)
    assert _antall_preview_rader(con) == 0


def test_over_300_rader_avvises(con):
    kid = _kurs(con)
    con.commit()
    mange = [f"P{i};Test;p{i}@x.no" for i in range(imp.MAKS_RADER + 1)]
    klient = _klient()
    _logg_inn(klient)
    resp = _last_opp(klient, kid, _csv(*mange, header=MIN_HEADER))
    assert resp.status_code == 400
    assert "rader" in resp.get_data(as_text=True).lower()
    assert _antall_preview_rader(con) == 0


# ==================== kursstatus ====================

def test_avlyst_kurs_gir_ingen_preview(con):
    kid = _kurs(con)
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kid,))
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = _last_opp(klient, kid, _csv("Kari;Nordmann;kari@x.no", header=MIN_HEADER))
    assert "avlyst" in resp.get_data(as_text=True).lower()
    assert _antall_preview_rader(con) == 0


def test_avsluttet_kurs_gir_ingen_preview(con):
    kid = _kurs(con)
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = _last_opp(klient, kid, _csv("Kari;Nordmann;kari@x.no", header=MIN_HEADER))
    assert "avsluttet" in resp.get_data(as_text=True).lower()
    assert _antall_preview_rader(con) == 0


@pytest.mark.parametrize("status", ["utkast", "aapen", "full", "aktiv"])
def test_tillatte_kursstatuser_fungerer(con, status):
    kid = _kurs(con, kapasitet=5)
    con.execute("UPDATE kurs SET status=? WHERE id=?", (status, kid))
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = _last_opp(klient, kid, _csv("Kari;Nordmann;kari@x.no", header=MIN_HEADER))
    assert resp.status_code == 200
    assert _antall_preview_rader(con) == 1


# ==================== resultatmodell i visningen ====================

def test_preview_viser_kombinasjon_eksisterende_person_ny_og_venteliste(con):
    kid1 = _kurs(con, "K1")
    kid2 = _kurs(con, "K2", kapasitet=1)
    db.meld_paa(con, kid1, epost="kari@x.no", fornavn="Kari", etternavn="Nordmann")  # kjent person, annet kurs
    db.meld_paa(con, kid2, epost="forst@x.no", fornavn="Forst", etternavn="Test")  # fyller kid2
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = _last_opp(klient, kid2, _csv("Kari;Nordmann;kari@x.no", header=MIN_HEADER)).get_data(as_text=True)
    assert "Eksisterende person" in tekst
    assert "Ny påmelding" in tekst
    assert "Venteliste" in tekst


def test_preview_viser_reaktivering(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="kari@x.no", fornavn="Kari", etternavn="Nordmann")
    db.meld_av(con, pid)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = _last_opp(klient, kid, _csv("Kari;Nordmann;kari@x.no", header=MIN_HEADER)).get_data(as_text=True)
    assert "Reaktiveres" in tekst


def test_preview_viser_hopp_over(con):
    kid = _kurs(con)
    db.meld_paa(con, kid, epost="kari@x.no", fornavn="Kari", etternavn="Nordmann")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = _last_opp(klient, kid, _csv("Kari;Nordmann;kari@x.no", header=MIN_HEADER)).get_data(as_text=True)
    assert "hoppes over" in tekst.lower()
    assert _antall_preview_rader(con) == 1  # hopp_over blokkerer ikke resten av importen


# ==================== retensjon ====================

def test_ny_opplasting_erstatter_tidligere_preview_for_samme_admin_og_kurs(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    _last_opp(klient, kid, _csv("Kari;Nordmann;kari@x.no", header=MIN_HEADER))
    assert _antall_preview_rader(con) == 1
    _last_opp(klient, kid, _csv("Ola;Nordmann;ola@x.no", header=MIN_HEADER))
    assert _antall_preview_rader(con) == 1  # ikke 2 - den gamle er erstattet
    rad = con.execute("SELECT rader_json FROM import_forhaandsvisning").fetchone()
    assert "ola@x.no" in rad["rader_json"]
    assert "kari@x.no" not in rad["rader_json"]


def test_utlopte_previews_ryddes_ved_ny_opplasting(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    imp.lagre_forhaandsvisning(con, kid, _admin_id(con),
                               [{"fornavn": "Gammel", "etternavn": "Test", "epost": "gammel@x.no"}], [],
                               idag=datetime.now() - timedelta(hours=1))
    con.commit()
    assert _antall_preview_rader(con) == 1
    klient = _klient()
    _logg_inn(klient)
    _last_opp(klient, kid, _csv("Kari;Nordmann;kari@x.no", header=MIN_HEADER))
    rader = con.execute("SELECT rader_json FROM import_forhaandsvisning").fetchall()
    assert len(rader) == 1
    assert "kari@x.no" in rader[0]["rader_json"]


# ==================== personvern ====================

def test_ingen_personopplysninger_i_hendelsesloggen(con):
    kid = _kurs(con, type="fysisk", kapasitet=5)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    _last_opp(klient, kid, _csv(
        "Hemmelig;Person;hemmelig.person@sensitiv-domene.no;99999999;;;;;;;;;;Peanøttallergi;Tegnspråktolk",
        header=CSV_HEADER))
    for r in con.execute("SELECT detaljer FROM hendelse"):
        d = (r["detaljer"] or "").lower()
        assert "hemmelig" not in d and "peanøttallergi" not in d and "tegnspråktolk" not in d
