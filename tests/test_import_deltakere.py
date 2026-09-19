"""Fase 10, trinn 1: kurs/import_deltakere.py - parsing, forhaandsvisning og import.

Ingen ruter/UI ennaa (kommer i trinn 2-3). Testene bruker modulen direkte, ikke Flask test-client.

Dekker ogsaa presiseringene fra oppfolgingsrundene:
  - resultater kan ha flere uavhengige egenskaper samtidig (se "resultatmodell"-testene)
  - eksisterende, ikke-tomme personopplysninger overskrives IKKE av import, men gammel
    standardoppforsel (offentlig/gruppe/manuell paamelding) er UENDRET (se "personopplysninger")
  - forhaandsvisning lagres server-side bak en tilfeldig token, ALDRI i en signert cookie/skjult
    felt med persondata (se "forhaandsvisningstabell")
"""
from datetime import date, datetime, timedelta

import pytest

from kurs import config, db, import_deltakere as imp

CSV_HEADER = "Navn;E-post;Telefon;Yrkestittel;Arbeidssted;HPR-nummer;Betaler;Firmanavn;Org.nr;" \
             "Fakturaadresse;Fakturareferanse;Fakturakommentar;Allergier;Tilrettelegging"


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    return c


def _kurs(con, kode="T1", start=None, **kw):
    start = start or (date.today() + timedelta(days=30))
    return db.opprett_kurs(con, kode=kode, navn="Testkurs", datoer=[start.isoformat()],
                           sharepoint_mappe=f"Kurs/{kode}", **{"pris_nok": 1000, **kw})


def _admin_id(con):
    return con.execute("SELECT id FROM admin_bruker WHERE brukernavn=?", (config.ADMIN_BRUKERNAVN,)).fetchone()[0]


def _csv(*rader: str, header: str = CSV_HEADER) -> bytes:
    return "\n".join([header, *rader]).encode("utf-8-sig")


def _rad(navn="Kari Nordmann", epost="kari@x.no", **over):
    r = {"navn": navn, "epost": epost}
    r.update(over)
    return r


def _antall_mail(con):
    return len(list(config.UTBOKS.glob("*.html"))) if config.UTBOKS.exists() else 0


def _snapshot(con, kid):
    return (
        con.execute("SELECT COUNT(*) FROM deltaker").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM sensitivt").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM hendelse").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0],
        con.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()[0],
    )


# ==================== parse_csv ====================

def test_parse_csv_gyldig_fil_med_alle_kolonner():
    rader = imp.parse_csv(_csv("Kari Nordmann;kari@x.no;99999999;Psykolog;Klinikk AS;1234567;"
                               "person;;;;;Ingen kommentar;Nøtteallergi;Rullestolrampe"))
    assert len(rader) == 1
    r = rader[0]
    assert r["navn"] == "Kari Nordmann"
    assert r["epost"] == "kari@x.no"
    assert r["telefon"] == "99999999"
    assert r["hpr_nr"] == "1234567"
    assert r["allergier"] == "Nøtteallergi"
    assert r["tilrettelegging"] == "Rullestolrampe"


def test_parse_csv_kun_minimumskolonner():
    rader = imp.parse_csv(_csv("Kari;kari@x.no", header="Navn;E-post"))
    assert rader == [{"navn": "Kari", "epost": "kari@x.no"}]


def test_parse_csv_mangler_epost_kolonne():
    with pytest.raises(imp.ImportFeil, match="E-post"):
        imp.parse_csv(_csv("Kari", header="Navn"))


def test_parse_csv_mangler_navn_kolonne():
    with pytest.raises(imp.ImportFeil, match="Navn"):
        imp.parse_csv(_csv("kari@x.no", header="E-post"))


def test_parse_csv_tom_fil():
    with pytest.raises(imp.ImportFeil, match="tom"):
        imp.parse_csv(b"")


def test_parse_csv_hopper_over_tomme_linjer():
    rader = imp.parse_csv(_csv("Kari;kari@x.no", "", "  ", "Ola;ola@x.no", header="Navn;E-post"))
    assert len(rader) == 2


def test_parse_csv_for_mange_rader():
    mange = [f"P{i};p{i}@x.no" for i in range(imp.MAKS_RADER + 1)]
    with pytest.raises(imp.ImportFeil, match="rader"):
        imp.parse_csv(_csv(*mange, header="Navn;E-post"))


def test_parse_csv_for_mange_kolonner():
    header = ";".join(f"Kolonne{i}" for i in range(imp.MAKS_KOLONNER + 1))
    with pytest.raises(imp.ImportFeil, match="kolonner"):
        imp.parse_csv(_csv("x" * (imp.MAKS_KOLONNER + 1), header=header))


def test_parse_csv_for_stor_fil():
    stor = b"Navn;E-post\n" + b"x" * (imp.MAKS_FILSTORRELSE + 1)
    with pytest.raises(imp.ImportFeil, match="stor"):
        imp.parse_csv(stor)


def test_parse_csv_komma_skilletegn_sniffes():
    rader = imp.parse_csv("Navn,E-post\nKari,kari@x.no".encode("utf-8-sig"))
    assert rader == [{"navn": "Kari", "epost": "kari@x.no"}]


def test_parse_csv_cp1252_fallback():
    tekst = "Navn;E-post;Arbeidssted\nKari;kari@x.no;Blåbær AS"
    rader = imp.parse_csv(tekst.encode("cp1252"))
    assert rader[0]["arbeidssted"] == "Blåbær AS"


def test_parse_csv_ukjent_kolonne_ignoreres_stille():
    rader = imp.parse_csv(_csv("Kari;kari@x.no;noe", header="Navn;E-post;Ukjent kolonne"))
    assert rader == [{"navn": "Kari", "epost": "kari@x.no"}]


# ==================== forhaandsvis(): resultatmodell (B/punkt 2) ====================

def test_resultatmodell_ny_rad_har_alle_forventede_felt(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    res = imp.forhaandsvis(con, kid, [_rad()], aktor="admin:test")
    r = res[0]
    assert set(r) == {"rad_nr", "navn", "epost", "handling", "resultatstatus",
                      "person_finnes_fra_for", "melding", "paamelding_id"}
    assert r["handling"] == imp.NY
    assert r["resultatstatus"] == imp.BEKREFTET
    assert r["person_finnes_fra_for"] is False


def test_resultatmodell_eksisterende_person_pa_annet_kurs_og_venteliste_samtidig(con):
    """Presist eksempelet fra oppfolgingen: personen finnes fra for (annet kurs) OG dette er en
    ny paamelding OG den blir satt paa venteliste - alle tre samtidig, ikke gjensidig utelukkende."""
    kid1 = _kurs(con, "K1")
    kid2 = _kurs(con, "K2", kapasitet=1)
    db.meld_paa(con, kid1, epost="kari@x.no", navn="Kari Nordmann")  # kjent person fra annet kurs
    db.meld_paa(con, kid2, epost="forst@x.no", navn="Forst")  # fyller kid2 sin kapasitet
    con.commit()
    res = imp.forhaandsvis(con, kid2, [_rad()], aktor="admin:test")
    r = res[0]
    assert r["handling"] == imp.NY               # ny paamelding PAA DETTE kurset
    assert r["resultatstatus"] == imp.VENTELISTE  # blir venteliste
    assert r["person_finnes_fra_for"] is True     # men personen finnes fra for (fra kid1)


def test_resultatmodell_reaktivering_som_venteliste_og_person_finnes(con):
    """Det andre eksempelet: eksisterende person OG tidligere avmeldt OG reaktiveres som venteliste."""
    kid = _kurs(con, kapasitet=1)
    pid, _ = db.meld_paa(con, kid, epost="kari@x.no", navn="Kari Nordmann")
    db.meld_av(con, pid)
    db.meld_paa(con, kid, epost="annen@x.no", navn="Annen")  # fyller kapasiteten
    con.commit()
    res = imp.forhaandsvis(con, kid, [_rad()], aktor="admin:test")
    r = res[0]
    assert r["handling"] == imp.REAKTIVER
    assert r["resultatstatus"] == imp.VENTELISTE
    assert r["person_finnes_fra_for"] is True
    assert r["paamelding_id"] == pid


def test_gyldig_rad_gir_ny_bekreftet(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    res = imp.forhaandsvis(con, kid, [_rad()], aktor="admin:test")
    assert res[0]["handling"] == imp.NY
    assert res[0]["resultatstatus"] == imp.BEKREFTET
    assert res[0]["paamelding_id"] is not None


def test_fullt_kurs_gir_ny_venteliste(con):
    kid = _kurs(con, kapasitet=1)
    db.meld_paa(con, kid, epost="forst@x.no", navn="Forst")
    con.commit()
    res = imp.forhaandsvis(con, kid, [_rad()], aktor="admin:test")
    assert res[0]["handling"] == imp.NY
    assert res[0]["resultatstatus"] == imp.VENTELISTE


def test_venteliste_simuleres_rad_for_rad_i_samme_import(con):
    kid = _kurs(con, kapasitet=1)
    con.commit()
    res = imp.forhaandsvis(con, kid, [_rad(epost="a@x.no"), _rad(epost="b@x.no")], aktor="admin:test")
    assert res[0]["resultatstatus"] == imp.BEKREFTET
    assert res[1]["resultatstatus"] == imp.VENTELISTE


def test_eksisterende_person_gjenbrukes_rulles_tilbake_likevel(con):
    kid1 = _kurs(con, "K1")
    kid2 = _kurs(con, "K2")
    db.meld_paa(con, kid1, epost="kari@x.no", navn="Kari Nordmann")
    con.commit()
    antall_foer = con.execute("SELECT COUNT(*) FROM deltaker").fetchone()[0]
    res = imp.forhaandsvis(con, kid2, [_rad()], aktor="admin:test")
    assert res[0]["person_finnes_fra_for"] is True
    assert con.execute("SELECT COUNT(*) FROM deltaker").fetchone()[0] == antall_foer  # rullet tilbake uansett


def test_allerede_paameldt_hoppes_over_ikke_feil(con):
    kid = _kurs(con)
    db.meld_paa(con, kid, epost="kari@x.no", navn="Kari Nordmann")
    con.commit()
    res = imp.forhaandsvis(con, kid, [_rad()], aktor="admin:test")
    assert res[0]["handling"] == imp.HOPP_OVER
    assert res[0]["resultatstatus"] is None
    assert not imp.har_blokkerende_rader(res)


def test_tidligere_avmeldt_reaktiveres(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="kari@x.no", navn="Kari Nordmann")
    db.meld_av(con, pid)
    con.commit()
    res = imp.forhaandsvis(con, kid, [_rad()], aktor="admin:test")
    assert res[0]["handling"] == imp.REAKTIVER
    assert res[0]["resultatstatus"] == imp.BEKREFTET
    assert res[0]["paamelding_id"] == pid


def test_ugyldig_epost_gir_blokkert(con):
    kid = _kurs(con)
    con.commit()
    res = imp.forhaandsvis(con, kid, [_rad(epost="ikke-en-epost")], aktor="admin:test")
    assert res[0]["handling"] == imp.BLOKKERT
    assert imp.har_blokkerende_rader(res)


def test_manglende_navn_gir_blokkert(con):
    kid = _kurs(con)
    con.commit()
    res = imp.forhaandsvis(con, kid, [_rad(navn="")], aktor="admin:test")
    assert res[0]["handling"] == imp.BLOKKERT


def test_organisasjon_uten_orgfelt_gir_blokkert(con):
    kid = _kurs(con)
    con.commit()
    res = imp.forhaandsvis(con, kid, [_rad(betaler="organisasjon")], aktor="admin:test")
    assert res[0]["handling"] == imp.BLOKKERT
    assert "org" in res[0]["melding"].lower() or "firma" in res[0]["melding"].lower()


def test_ugyldig_betalerverdi_gir_blokkert(con):
    kid = _kurs(con)
    con.commit()
    res = imp.forhaandsvis(con, kid, [_rad(betaler="bedrift")], aktor="admin:test")
    assert res[0]["handling"] == imp.BLOKKERT


def test_for_lang_celle_gir_blokkert(con):
    kid = _kurs(con)
    con.commit()
    res = imp.forhaandsvis(con, kid, [_rad(faktura_kommentar="x" * (imp.MAKS_CELLELENGDE + 1))], aktor="admin:test")
    assert res[0]["handling"] == imp.BLOKKERT


def test_dublett_i_samme_fil_blokkerer_kun_andre_forekomst(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    res = imp.forhaandsvis(con, kid, [_rad(), _rad()], aktor="admin:test")
    assert res[0]["handling"] == imp.NY
    assert res[1]["handling"] == imp.BLOKKERT
    assert imp.har_blokkerende_rader(res)


def test_dublett_i_fil_er_ikke_case_sensitiv(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    res = imp.forhaandsvis(con, kid, [_rad(epost="Kari@X.no"), _rad(epost="kari@x.no")], aktor="admin:test")
    assert res[1]["handling"] == imp.BLOKKERT


def test_utkast_kurs_tillates(con):
    kid = _kurs(con, status="utkast")
    con.commit()
    res = imp.forhaandsvis(con, kid, [_rad()], aktor="admin:test")
    assert res[0]["handling"] == imp.NY
    assert res[0]["resultatstatus"] == imp.BEKREFTET


def test_avlyst_kurs_gir_importfeil_for_hele_filen(con):
    kid = _kurs(con)
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kid,))
    con.commit()
    with pytest.raises(imp.ImportFeil, match="avlyst"):
        imp.forhaandsvis(con, kid, [_rad()], aktor="admin:test")


def test_avsluttet_kurs_gir_importfeil_for_hele_filen(con):
    kid = _kurs(con)
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()
    with pytest.raises(imp.ImportFeil, match="avsluttet"):
        imp.forhaandsvis(con, kid, [_rad()], aktor="admin:test")


def test_etter_paameldingsfrist_tillates_for_admin(con):
    kid = _kurs(con, paameldingsfrist=(date.today() - timedelta(days=1)).isoformat())
    con.commit()
    res = imp.forhaandsvis(con, kid, [_rad()], aktor="admin:test")
    assert res[0]["handling"] == imp.NY


# ==================== SAVEPOINT-garantien (eksplisitt krav) ====================

def test_forhaandsvisning_lar_ingen_spor_i_databasen(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    foer = _snapshot(con, kid)
    imp.forhaandsvis(con, kid, [_rad()], aktor="admin:test")
    assert _snapshot(con, kid) == foer


def test_forhaandsvisning_lar_ingen_spor_med_reaktivering_og_venteliste_og_allergi(con):
    kid = _kurs(con, kapasitet=1, type="fysisk")
    pid, _ = db.meld_paa(con, kid, epost="gammel@x.no", navn="Gammel")
    db.meld_av(con, pid)
    con.commit()
    foer = _snapshot(con, kid)
    imp.forhaandsvis(con, kid, [
        _rad(epost="gammel@x.no", navn="Gammel"),
        _rad(epost="ny@x.no", navn="Ny", allergier="Notter", tilrettelegging="Rullestol"),
    ], aktor="admin:test")
    assert _snapshot(con, kid) == foer


def test_forhaandsvisning_lar_ingen_spor_ved_ugyldig_rad(con):
    kid = _kurs(con)
    con.commit()
    foer = _snapshot(con, kid)
    imp.forhaandsvis(con, kid, [_rad(), _rad(epost="ugyldig")], aktor="admin:test")
    assert _snapshot(con, kid) == foer


def test_forhaandsvisning_lar_ingen_spor_naar_kurset_er_avlyst(con):
    kid = _kurs(con)
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kid,))
    con.commit()
    foer = _snapshot(con, kid)
    with pytest.raises(imp.ImportFeil):
        imp.forhaandsvis(con, kid, [_rad()], aktor="admin:test")
    assert _snapshot(con, kid) == foer


# ==================== importer(): faktisk import ====================

def test_import_committer_gyldige_rader(con):
    kid = _kurs(con, kapasitet=5, pris_nok=1000, fakturering="person")
    con.commit()
    rader = [_rad(allergier="Nøtter", tilrettelegging="Rullestol")]
    forste = imp.forhaandsvis(con, kid, rader, aktor="admin:test")
    con.commit()
    resultat = imp.importer(con, kid, rader, forste, aktor="admin:test")
    con.commit()
    assert resultat[0]["handling"] == imp.NY
    pid = resultat[0]["paamelding_id"]
    rad = con.execute(
        "SELECT kilde, sveiper_kjort, sveiper_utsatt FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert rad["kilde"] == "admin_import"
    assert rad["sveiper_kjort"] == 0
    assert rad["sveiper_utsatt"] == 1
    sensitivt = con.execute("SELECT allergier, tilrettelegging FROM sensitivt WHERE paamelding_id=?", (pid,)).fetchone()
    assert sensitivt["allergier"] == "Nøtter"
    assert sensitivt["tilrettelegging"] == "Rullestol"


def test_ingen_epost_eller_faktura_ved_import(con):
    kid = _kurs(con, pris_nok=1000, fakturering="person")
    con.commit()
    rader = [_rad()]
    forste = imp.forhaandsvis(con, kid, rader, aktor="admin:test")
    con.commit()
    imp.importer(con, kid, rader, forste, aktor="admin:test")
    con.commit()
    assert _antall_mail(con) == 0
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 0


def test_allerede_paameldt_rad_hoppes_over_uten_ny_paamelding(con):
    kid = _kurs(con, kapasitet=5)
    db.meld_paa(con, kid, epost="kari@x.no", navn="Kari Nordmann")
    con.commit()
    antall_foer = con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0]
    rader = [_rad()]
    forste = imp.forhaandsvis(con, kid, rader, aktor="admin:test")
    con.commit()
    resultat = imp.importer(con, kid, rader, forste, aktor="admin:test")
    con.commit()
    assert resultat[0]["handling"] == imp.HOPP_OVER
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == antall_foer


def test_import_ruller_tilbake_ved_endret_kapasitet(con):
    kid = _kurs(con, kapasitet=1)
    con.commit()
    rader = [_rad()]
    forste = imp.forhaandsvis(con, kid, rader, aktor="admin:test")
    con.commit()
    assert forste[0]["resultatstatus"] == imp.BEKREFTET

    db.meld_paa(con, kid, epost="annen@x.no", navn="Annen")  # fyller kapasiteten i mellomtiden
    con.commit()
    antall_foer = con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0]

    with pytest.raises(imp.EndretTilstandFeil) as e:
        imp.importer(con, kid, rader, forste, aktor="admin:test")
    assert any("venteliste" in a.lower() or "bekreftet" in a.lower() for a in e.value.avvik)
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == antall_foer


def test_import_ruller_ikke_tilbake_naar_bare_person_finnes_flagg_endres(con):
    """person_finnes_fra_for er informativt og skal IKKE trigge avbrudd selv om det faktisk
    endrer seg mellom forhaandsvisning og bekreft - kun handling+resultatstatus er
    beslutningsrelevante (se sammenlign_resultater)."""
    kid1 = _kurs(con, "K1", kapasitet=5)
    kid2 = _kurs(con, "K2", kapasitet=5)
    con.commit()
    rader = [_rad()]
    forste = imp.forhaandsvis(con, kid2, rader, aktor="admin:test")
    assert forste[0]["person_finnes_fra_for"] is False
    con.commit()

    # "kari" registreres na paa et ANNET kurs i mellomtiden - person_finnes_fra_for for VAAR
    # rad ville blitt True ved en ny forhaandsvisning, men UTFALLET (ny+bekreftet) er uendret.
    db.meld_paa(con, kid1, epost="kari@x.no", navn="Kari Nordmann")
    con.commit()

    resultat = imp.importer(con, kid2, rader, forste, aktor="admin:test")
    con.commit()
    assert resultat[0]["handling"] == imp.NY
    assert resultat[0]["resultatstatus"] == imp.BEKREFTET
    assert resultat[0]["person_finnes_fra_for"] is True  # informasjonen er likevel korrekt i resultatet


def test_import_ruller_tilbake_ved_kurs_avlyst_etter_forhaandsvisning(con):
    kid = _kurs(con)
    con.commit()
    rader = [_rad()]
    forste = imp.forhaandsvis(con, kid, rader, aktor="admin:test")
    con.commit()
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kid,))
    con.commit()
    antall_foer = con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0]
    with pytest.raises(imp.ImportFeil):
        imp.importer(con, kid, rader, forste, aktor="admin:test")
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == antall_foer


def test_forhaandsvisning_matcher_faktisk_import_nar_ingenting_har_endret_seg(con):
    kid = _kurs(con, kapasitet=2)
    con.commit()
    rader = [_rad(epost="a@x.no"), _rad(epost="b@x.no"), _rad(epost="c@x.no")]
    forste = imp.forhaandsvis(con, kid, rader, aktor="admin:test")
    con.commit()
    resultat = imp.importer(con, kid, rader, forste, aktor="admin:test")
    con.commit()
    assert [(r["handling"], r["resultatstatus"]) for r in resultat] == \
           [(r["handling"], r["resultatstatus"]) for r in forste]
    assert con.execute("SELECT COUNT(*) FROM paamelding WHERE kurs_id=?", (kid,)).fetchone()[0] == 3


# ==================== eksisterende personopplysninger: A) gammel standard uendret, B) import beskyttet ====================

def test_A_gammel_standardoppforsel_overskriver_fortsatt_ved_vanlig_paamelding(con):
    """db.meld_paa() UTEN beskytt_eksisterende_felt (default) skal fortsatt overskrive et
    eksisterende, ikke-tomt telefonnummer - akkurat som for fase 10. Offentlig paamelding,
    gruppepaamelding og fase 9 sin admin-registrering bruker ALLE denne default-oppforselen
    uendret, og skal kunne oppdatere kontaktinfo naar en deltaker melder seg paa igjen."""
    kid1 = _kurs(con, "K1")
    kid2 = _kurs(con, "K2")
    db.meld_paa(con, kid1, epost="kari@x.no", navn="Kari Nordmann", deltaker={"telefon": "11111111"})
    con.commit()
    db.meld_paa(con, kid2, epost="kari@x.no", navn="Kari Nordmann", deltaker={"telefon": "99999999"})
    con.commit()
    rad = con.execute("SELECT telefon FROM deltaker WHERE epost='kari@x.no'").fetchone()
    assert rad["telefon"] == "99999999"  # oppdatert, IKKE beholdt - uendret fra for fase 10


def test_finn_eller_opprett_deltaker_default_overskriver(con):
    db.finn_eller_opprett_deltaker(con, "kari@x.no", "Kari", telefon="11111111")
    db.finn_eller_opprett_deltaker(con, "kari@x.no", "Kari", telefon="22222222")
    rad = con.execute("SELECT telefon FROM deltaker WHERE epost='kari@x.no'").fetchone()
    assert rad["telefon"] == "22222222"


def test_finn_eller_opprett_deltaker_beskyttet_modus_overskriver_ikke(con):
    db.finn_eller_opprett_deltaker(con, "kari@x.no", "Kari", telefon="11111111")
    db.finn_eller_opprett_deltaker(con, "kari@x.no", "Kari", beskytt_eksisterende_felt=True, telefon="22222222")
    rad = con.execute("SELECT telefon FROM deltaker WHERE epost='kari@x.no'").fetchone()
    assert rad["telefon"] == "11111111"


def test_finn_eller_opprett_deltaker_beskyttet_modus_fyller_tomt_felt(con):
    db.finn_eller_opprett_deltaker(con, "kari@x.no", "Kari")  # ingen telefon
    db.finn_eller_opprett_deltaker(con, "kari@x.no", "Kari", beskytt_eksisterende_felt=True, telefon="22222222")
    rad = con.execute("SELECT telefon FROM deltaker WHERE epost='kari@x.no'").fetchone()
    assert rad["telefon"] == "22222222"


# ==================== B) importens beskyttede oppforsel ====================

def test_eksisterende_ikke_tomt_felt_overskrives_ikke_av_import(con):
    """finn_eller_opprett_deltaker(): en gammel importfil med et ANNET (eldre/feil) telefonnummer
    skal IKKE overskrive et telefonnummer personen allerede har registrert."""
    kid1 = _kurs(con, "K1")
    kid2 = _kurs(con, "K2")
    db.meld_paa(con, kid1, epost="kari@x.no", navn="Kari Nordmann", deltaker={"telefon": "11111111"})
    con.commit()
    rader = [_rad(telefon="00000000")]  # gammel/feil verdi i importfilen
    forste = imp.forhaandsvis(con, kid2, rader, aktor="admin:test")
    con.commit()
    imp.importer(con, kid2, rader, forste, aktor="admin:test")
    con.commit()
    rad = con.execute("SELECT telefon FROM deltaker WHERE epost='kari@x.no'").fetchone()
    assert rad["telefon"] == "11111111"


def test_eksisterende_tomt_felt_fylles_av_import(con):
    """Et felt som er TOMT fra for skal derimot fylles inn av importen."""
    kid1 = _kurs(con, "K1")
    kid2 = _kurs(con, "K2")
    db.meld_paa(con, kid1, epost="kari@x.no", navn="Kari Nordmann")  # ingen arbeidssted registrert
    con.commit()
    rader = [_rad(arbeidssted="Ny Klinikk AS")]
    forste = imp.forhaandsvis(con, kid2, rader, aktor="admin:test")
    con.commit()
    imp.importer(con, kid2, rader, forste, aktor="admin:test")
    con.commit()
    rad = con.execute("SELECT arbeidssted FROM deltaker WHERE epost='kari@x.no'").fetchone()
    assert rad["arbeidssted"] == "Ny Klinikk AS"


def test_navn_pa_eksisterende_person_endres_ikke_av_import(con):
    kid1 = _kurs(con, "K1")
    kid2 = _kurs(con, "K2")
    db.meld_paa(con, kid1, epost="kari@x.no", navn="Kari Opprinnelig Nordmann")
    con.commit()
    rader = [_rad(navn="Feil Navn")]
    forste = imp.forhaandsvis(con, kid2, rader, aktor="admin:test")
    con.commit()
    imp.importer(con, kid2, rader, forste, aktor="admin:test")
    con.commit()
    rad = con.execute("SELECT navn FROM deltaker WHERE epost='kari@x.no'").fetchone()
    assert rad["navn"] == "Kari Opprinnelig Nordmann"


# ==================== forhaandsvisningstabell (server-side, ikke signert token) ====================

def test_lagre_og_hente_forhaandsvisning_rundtrip(con):
    kid = _kurs(con)
    con.commit()
    rader = [_rad()]
    resultater = [imp._resultat(1, rader[0], handling=imp.NY, melding="ok", resultatstatus=imp.BEKREFTET)]
    token = imp.lagre_forhaandsvisning(con, kid, _admin_id(con), rader, resultater)
    con.commit()
    innhold = imp.hent_forhaandsvisning(con, token, kid, _admin_id(con))
    assert innhold["rader"] == rader
    assert innhold["resultater"] == resultater


def test_token_er_kort_og_inneholder_ikke_radinnhold(con):
    """Selve strengen nettleseren far skal vaere en kort, ugjennomsiktig id - ikke inneholde
    navn/e-post/allergi. Data ligger KUN server-side."""
    kid = _kurs(con)
    con.commit()
    rader = [_rad(navn="Skjult Person", epost="skjult@x.no", allergier="Hemmelig allergi")]
    token = imp.lagre_forhaandsvisning(con, kid, _admin_id(con), rader, [])
    con.commit()
    assert len(token) < 100
    assert "skjult" not in token.lower() and "hemmelig" not in token.lower()


def test_ukjent_token_avvises(con):
    with pytest.raises(imp.TokenFeil):
        imp.hent_forhaandsvisning(con, "finnes-ikke", 1, 1)


def test_utlopt_forhaandsvisning_avvises_og_slettes(con):
    kid = _kurs(con)
    con.commit()
    for_lenge_siden = datetime.now() - timedelta(hours=1)
    token = imp.lagre_forhaandsvisning(con, kid, _admin_id(con), [_rad()], [], idag=for_lenge_siden)
    con.commit()
    with pytest.raises(imp.TokenFeil, match="utløpt"):
        imp.hent_forhaandsvisning(con, token, kid, _admin_id(con))
    assert con.execute("SELECT COUNT(*) FROM import_forhaandsvisning WHERE token=?", (token,)).fetchone()[0] == 0


def test_forhaandsvisning_for_annet_kurs_avvises(con):
    kid1 = _kurs(con, "K1")
    kid2 = _kurs(con, "K2")
    con.commit()
    token = imp.lagre_forhaandsvisning(con, kid1, _admin_id(con), [_rad()], [])
    con.commit()
    with pytest.raises(imp.TokenFeil):
        imp.hent_forhaandsvisning(con, token, kid2, _admin_id(con))


def test_forhaandsvisning_for_annen_admin_avvises(con):
    kid = _kurs(con)
    con.commit()
    token = imp.lagre_forhaandsvisning(con, kid, _admin_id(con), [_rad()], [])
    con.commit()
    with pytest.raises(imp.TokenFeil):
        imp.hent_forhaandsvisning(con, token, kid, _admin_id(con) + 999)


def test_slett_forhaandsvisning_fjerner_raden(con):
    kid = _kurs(con)
    con.commit()
    token = imp.lagre_forhaandsvisning(con, kid, _admin_id(con), [_rad()], [])
    con.commit()
    imp.slett_forhaandsvisning(con, token)
    con.commit()
    with pytest.raises(imp.TokenFeil):
        imp.hent_forhaandsvisning(con, token, kid, _admin_id(con))


def test_rydd_utlopte_forhaandsvisninger_sletter_kun_utlopte(con):
    kid = _kurs(con)
    con.commit()
    gammel = imp.lagre_forhaandsvisning(con, kid, _admin_id(con), [_rad()], [],
                                        idag=datetime.now() - timedelta(hours=1))
    ny = imp.lagre_forhaandsvisning(con, kid, _admin_id(con), [_rad()], [])
    con.commit()
    antall_slettet = imp.rydd_utlopte_forhaandsvisninger(con)
    con.commit()
    assert antall_slettet == 1
    assert con.execute("SELECT COUNT(*) FROM import_forhaandsvisning WHERE token=?", (gammel,)).fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM import_forhaandsvisning WHERE token=?", (ny,)).fetchone()[0] == 1


def test_forhaandsvisning_kommer_ikke_i_hendelsesloggen(con):
    kid = _kurs(con)
    con.commit()
    imp.lagre_forhaandsvisning(con, kid, _admin_id(con), [
        _rad(navn="Hemmelig Person", epost="hemmelig@x.no", allergier="Peanotter")], [])
    con.commit()
    for r in con.execute("SELECT detaljer FROM hendelse"):
        d = (r["detaljer"] or "").lower()
        assert "hemmelig" not in d and "peanotter" not in d


# ==================== sammenlign_resultater ====================

def test_sammenlign_resultater_ingen_avvik():
    r = [imp._resultat(1, _rad(), handling=imp.NY, melding="ok", resultatstatus=imp.BEKREFTET)]
    assert imp.sammenlign_resultater(r, r) == []


def test_sammenlign_resultater_resultatstatus_endret_gir_avvik():
    gammel = [imp._resultat(1, _rad(), handling=imp.NY, melding="ok", resultatstatus=imp.BEKREFTET)]
    ny = [imp._resultat(1, _rad(), handling=imp.NY, melding="ok", resultatstatus=imp.VENTELISTE)]
    avvik = imp.sammenlign_resultater(gammel, ny)
    assert len(avvik) == 1


def test_sammenlign_resultater_handling_endret_gir_avvik():
    gammel = [imp._resultat(1, _rad(), handling=imp.NY, melding="ok", resultatstatus=imp.BEKREFTET)]
    ny = [imp._resultat(1, _rad(), handling=imp.HOPP_OVER, melding="ok")]
    assert imp.sammenlign_resultater(gammel, ny) != []


def test_sammenlign_resultater_kun_person_finnes_endret_gir_ikke_avvik():
    """person_finnes_fra_for er informasjon, ikke en beslutning - skal IKKE trigge avbrudd alene."""
    gammel = [imp._resultat(1, _rad(), handling=imp.NY, melding="ok", resultatstatus=imp.BEKREFTET,
                            person_finnes_fra_for=False)]
    ny = [imp._resultat(1, _rad(), handling=imp.NY, melding="ok", resultatstatus=imp.BEKREFTET,
                        person_finnes_fra_for=True)]
    assert imp.sammenlign_resultater(gammel, ny) == []


def test_sammenlign_resultater_antall_rader_endret_gir_avvik():
    gammel = [imp._resultat(1, _rad(), handling=imp.NY, melding="ok", resultatstatus=imp.BEKREFTET)]
    ny = []
    assert imp.sammenlign_resultater(gammel, ny) != []


# ==================== malfil ====================

def test_malfil_har_header_og_eksempelrad():
    tekst = imp.malfil_csv()
    linjer = tekst.strip().splitlines()
    assert len(linjer) == 2
    assert linjer[0].split(";") == imp.MALFIL_HEADER
    rader = imp.parse_csv(tekst.encode("utf-8-sig"))
    assert len(rader) == 1
    assert "@" in rader[0]["epost"]


# ==================== personvern: ingen personopplysninger i hendelseslogg ====================

def test_import_logger_ikke_personopplysninger_utover_meld_paa_sin_egen(con):
    kid = _kurs(con)
    con.commit()
    rader = [_rad(epost="hemmelig.person@sensitiv-domene.no", navn="Hemmelig Person",
                  allergier="Peanotter", tilrettelegging="Tegnspraaktolk")]
    forste = imp.forhaandsvis(con, kid, rader, aktor="admin:test")
    con.commit()
    imp.importer(con, kid, rader, forste, aktor="admin:test")
    con.commit()
    for r in con.execute("SELECT detaljer FROM hendelse"):
        d = (r["detaljer"] or "").lower()
        assert "hemmelig" not in d and "peanotter" not in d and "tegnspraaktolk" not in d
