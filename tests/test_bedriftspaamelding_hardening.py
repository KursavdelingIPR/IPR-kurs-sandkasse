"""Fase 12C3B: hardening av bedriftspaameldingens POST-felt (/kurs/<kode>/gruppe).

Audit/probe-funn: ruten leser kun NAVNGITTE felt (betaler='organisasjon' og kilde='gruppe' er hardkodet, betaling og
sensitive felt leses aldri) - UNNTATT deltaker_hpr, som ble lest og lagret selv naar HPR-feltet ikke vises (kurs uten
spesialistlop), og som da overskrev en eksisterende persons HPR. Det er rettet; resten er her laast med tester.
Alle testdata er fiktive."""
import pytest

from kurs import config, db
from kurs.web import app as webapp


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _kurs(con, kode="G1", **kw):
    felter = {"navn": "Eksempelkurs", "type": "fysisk", "pris_nok": 4500, "fakturering": "person", "kapasitet": 10, **kw}
    kid = db.opprett_kurs(con, kode=kode, datoer=["2099-03-02"], sharepoint_mappe=f"K/{kode}", **felter)
    con.commit()
    return kid


BASIS = {"kontakt_navn": "Kontakt Person", "kontakt_epost": "kontakt@eksempel.no", "firmanavn": "Eksempel AS",
         "org_nr": "000000000", "faktura_ref": "REF-1", "faktura_adresse": "Eksempelveien 1", "faktura_postnr": "0000",
         "faktura_sted": "Eksempelby", "samtykke": "on", "deltaker_navn": "Deltaker En",
         "deltaker_epost": "d1@eksempel.no"}


def _post(kode, **data):
    return webapp.app.test_client().post(f"/kurs/{kode}/gruppe", data={**BASIS, **data})


def _deltaker(con, epost="d1@eksempel.no"):
    return con.execute("SELECT * FROM deltaker WHERE epost=?", (epost,)).fetchone()


def _paamelding(con, kid, epost="d1@eksempel.no"):
    return con.execute("SELECT p.* FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE p.kurs_id=? AND "
                       "d.epost=?", (kid, epost)).fetchone()


def _antall(con, tabell):
    return con.execute(f"SELECT COUNT(*) FROM {tabell}").fetchone()[0]


# ============================ HPR ============================

@pytest.mark.parametrize("lop", [None, ""])
def test_skjult_hpr_lagres_ikke_for_ny_deltaker(con, lop):
    _kurs(con, spesialistlop=lop)
    assert _post("G1", deltaker_hpr="MANIPULERT").status_code == 302
    assert _deltaker(con)["hpr_nr"] is None


@pytest.mark.parametrize("sendt", ["NY-MANIPULERT", "", "   "])
def test_skjult_hpr_overskriver_eller_sletter_ikke_eksisterende(con, sendt):
    db.finn_eller_opprett_deltaker(con, "d1@eksempel.no", "Deltaker En", hpr_nr="GAMMEL-HPR")
    con.commit()
    kid = _kurs(con)
    assert _post("G1", deltaker_hpr=sendt).status_code == 302
    assert _paamelding(con, kid) is not None
    assert _deltaker(con)["hpr_nr"] == "GAMMEL-HPR"


def test_synlig_hpr_lagres_som_foer(con):
    _kurs(con, spesialistlop="EFT")
    assert _post("G1", deltaker_hpr=" 1234567 ").status_code == 302
    assert _deltaker(con)["hpr_nr"] == "1234567"


def test_synlig_hpr_oppdaterer_eksisterende_som_foer(con):
    db.finn_eller_opprett_deltaker(con, "d1@eksempel.no", "Deltaker En", hpr_nr="GAMMEL-HPR")
    con.commit()
    _kurs(con, spesialistlop="EFT")
    assert _post("G1", deltaker_hpr="NY-HPR").status_code == 302
    assert _deltaker(con)["hpr_nr"] == "NY-HPR"


def test_skjult_hpr_filtreres_foer_meld_paa(con, monkeypatch):
    """Grensen ligger FOER foerste varige skriving: meld_paa faar aldri den skjulte verdien."""
    _kurs(con)
    sett = []
    ekte = db.meld_paa

    def spion(*a, **kw):
        sett.append(kw["deltaker"])
        return ekte(*a, **kw)
    monkeypatch.setattr(db, "meld_paa", spion)
    assert _post("G1", deltaker_hpr="MANIPULERT").status_code == 302
    assert sett == [{"telefon": None, "arbeidssted": None, "hpr_nr": None}]


def test_flere_rader_skjult_hpr_ignoreres_for_alle(con):
    _kurs(con)
    data = {**BASIS, "deltaker_navn": ["A", "B"], "deltaker_epost": ["a@eksempel.no", "b@eksempel.no"],
            "deltaker_hpr": ["M1", "M2"]}
    assert webapp.app.test_client().post("/kurs/G1/gruppe", data=data).status_code == 302
    assert [_deltaker(con, e)["hpr_nr"] for e in ("a@eksempel.no", "b@eksempel.no")] == [None, None]


def test_gruppeskjemaet_viser_hpr_kun_med_spesialistlop(con):
    _kurs(con, "U")
    _kurs(con, "M", spesialistlop="EFT")
    k = webapp.app.test_client()
    assert 'name="deltaker_hpr"' not in k.get("/kurs/U/gruppe").get_data(as_text=True)
    assert 'name="deltaker_hpr"' in k.get("/kurs/M/gruppe").get_data(as_text=True)


# ============================ synlige deltakerfelt: dagens adferd ============================

def test_synlig_telefon_og_arbeidssted_oppdaterer_profil_som_foer(con):
    db.finn_eller_opprett_deltaker(con, "d1@eksempel.no", "Deltaker En", telefon="GAMMEL", arbeidssted="GAMMEL")
    con.commit()
    _kurs(con)
    assert _post("G1", deltaker_telefon="NY", deltaker_arbeidssted="NY AS").status_code == 302
    d = _deltaker(con)
    assert (d["telefon"], d["arbeidssted"]) == ("NY", "NY AS")


# ============================ sensitive felt ============================

@pytest.mark.parametrize("type_", ["fysisk", "digital"])
def test_sensitive_felt_kan_ikke_injiseres(con, type_):
    _kurs(con, type=type_)
    assert _post("G1", allergier="MANIP", tilrettelegging="MANIP", deltaker_allergier="MANIP",
                 deltaker_tilrettelegging="MANIP").status_code == 302
    assert _antall(con, "sensitivt") == 0


# ============================ administrative / system-felt ============================

ADMIN = {"status": "avmeldt", "kilde": "admin", "sveiper_utsatt": "1", "sveiper_kjort": "0",
         "intern_kommentar": "MANIP", "faktura_kommentar": "MANIP", "yrkestittel": "MANIP",
         "deltaker_yrkestittel": "MANIP", "paamelding_id": "999", "kurs_id": "999", "deltaker_id": "999",
         "faktura_onskes_na": "1", "faktura_tidligst_dato": "2000-01-01", "rabattkode": "MANIP",
         "samtykke_ts": "2000-01-01", "visma_kunde_id": "MANIP", "faktura_status": "betalt", "status_faktura": "betalt",
         "innsendingsnokkel": "MANIP", "kvittering_token": "MANIP"}


def test_administrative_felt_kan_ikke_styres_via_offentlig_post(con):
    kid = _kurs(con)
    assert _post("G1", **ADMIN).status_code == 302
    p = _paamelding(con, kid)
    assert (p["status"], p["kilde"], p["sveiper_utsatt"], p["intern_kommentar"], p["faktura_kommentar"],
            p["faktura_onskes_na"], p["rabattkode"]) == ("bekreftet", "gruppe", 0, None, None, 0, None)
    assert p["faktura_tidligst_dato"] != "2000-01-01" and p["samtykke_ts"] != "2000-01-01"
    assert p["kurs_id"] == kid and p["id"] != 999
    d = _deltaker(con)
    assert (d["yrkestittel"], d["visma_kunde_id"]) == (None, None) and d["id"] != 999
    firma = con.execute("SELECT * FROM firmapaamelding").fetchone()
    assert firma["innsendingsnokkel"] != "MANIP" and firma["kvittering_token"] != "MANIP"
    assert con.execute("SELECT COUNT(*) FROM faktura WHERE status='betalt'").fetchone()[0] == 0


# ============================ faktura / organisasjon ============================

@pytest.mark.parametrize("oppsett", [dict(), dict(betaling="deltaker_velger"), dict(betaling="per_samling"),
                                     dict(fakturering="organisasjon"), dict(fakturering="ingen"), dict(pris_nok=0)])
def test_betaler_og_fakturadata_kommer_kun_fra_firmafeltene(con, oppsett):
    """Bedriftspaamelding: betaler er ALLTID organisasjonen (domeneregel, ikke individregelen). Manipulerte betaler/
    betaling/faktura_epost/org_navn ignoreres; alle firma-/fakturafelt i skjemaet er alltid synlige og legitime."""
    kid = _kurs(con, **oppsett)
    assert _post("G1", ehf="on", betaler="person", betaling="per_samling", faktura_epost="manip@eksempel.no",
                 org_navn="MANIP ORG").status_code == 302
    p = _paamelding(con, kid)
    forventet_betaling = "per_samling" if oppsett.get("betaling") == "per_samling" else "samlet"
    assert (p["betaler"], p["org_navn"], p["org_nr"], p["faktura_epost"], p["faktura_ref"], p["faktura_adresse"],
            p["faktura_postnr"], p["faktura_sted"], p["ehf"], p["betaling"]) == (
        "organisasjon", "Eksempel AS", "000000000", "kontakt@eksempel.no", "REF-1", "Eksempelveien 1", "0000",
        "Eksempelby", 1, forventet_betaling)


# ============================ idempotens / gjeninnsending ============================

def _telle(con):
    return {t: _antall(con, t) for t in ("firmapaamelding", "firmapaamelding_rad", "deltaker", "paamelding",
                                         "utsending_logg", "faktura", "faktura_forsok", "sensitivt")}


def test_normal_og_identisk_gjeninnsending_dedupliseres_som_foer(con):
    _kurs(con)
    data = {**BASIS, "deltaker_navn": ["A", "B"], "deltaker_epost": ["a@eksempel.no", "b@eksempel.no"]}
    r1 = webapp.app.test_client().post("/kurs/G1/gruppe", data=data)
    etter_en = _telle(con)
    assert (etter_en["firmapaamelding"], etter_en["firmapaamelding_rad"], etter_en["paamelding"]) == (1, 2, 2)
    r2 = webapp.app.test_client().post("/kurs/G1/gruppe", data=data)
    assert r1.status_code == r2.status_code == 302 and r1.headers["Location"] == r2.headers["Location"]
    assert _telle(con) == etter_en


def test_manipulert_skjult_felt_paavirker_ikke_dedupe_og_endrer_ingenting(con):
    db.finn_eller_opprett_deltaker(con, "d1@eksempel.no", "Deltaker En", hpr_nr="GAMMEL-HPR")
    con.commit()
    _kurs(con)
    r1 = _post("G1")
    etter_en = _telle(con)
    r2 = _post("G1", deltaker_hpr="MANIPULERT", status="avmeldt", allergier="MANIP", **{"kilde": "admin"})
    assert r2.status_code == 302 and r2.headers["Location"] == r1.headers["Location"]   # samme innsending
    assert _telle(con) == etter_en
    assert _deltaker(con)["hpr_nr"] == "GAMMEL-HPR"
