"""HPR-nummer samles ikke inn lenger (Camilla 02.10.2026: «ta bort HPR-nummer»).

Verken det offentlige påmeldingsskjemaet, bedriftspåmeldingen eller skjemabyggeren viser feltet, selv om kurset er i et spesialistløp. Koden som viser og lagrer det
finnes fortsatt (skjemafelt.HPR_I_SKJEMA), og det som alt står på personene røres ikke. Søket, importen og «Legg til deltaker» (administrators eget skjema) er uendret."""
import pytest

from adressehjelp import ADRESSE
from kurs import config, db, skjemafelt


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _klient(admin=False):
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    if admin:
        k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def _kurs(con, kode="H1", **kw):
    kid = db.opprett_kurs(con, kode=kode, navn="EFT-kurs", datoer=["2099-03-02", "2099-03-03"], sharepoint_mappe=f"K/{kode}",
                          **{"type": "fysisk", "sted": "Oslo", "pris_nok": 4500, "fakturering": "person", "betaling": "samlet", "spesialistlop": "EFT", **kw})
    con.commit()
    return kid


EPOST = "test.person@eksempel.no"
BASIS = {"fornavn": "Test", "etternavn": "Person", "epost": EPOST, "samtykke": "on", "samtykke_lagring": "on", **ADRESSE}


def _person(con, epost=EPOST):
    return con.execute("SELECT * FROM deltaker WHERE epost=?", (epost,)).fetchone()


def test_hpr_er_slaatt_av_som_standard():
    assert skjemafelt.HPR_I_SKJEMA is False
    assert skjemafelt.vis_hpr({"spesialistlop": "EFT"}) is False and skjemafelt.vis_hpr({"spesialistlop": None}) is False


def test_hpr_kan_slaas_paa_igjen_og_gjelder_da_bare_spesialistlop(monkeypatch):
    monkeypatch.setattr(skjemafelt, "HPR_I_SKJEMA", True)
    assert skjemafelt.vis_hpr({"spesialistlop": "EFT"}) is True and skjemafelt.vis_hpr({"spesialistlop": None}) is False


def test_offentlig_paamelding_til_spesialistlop_har_ikke_hpr_felt(con):
    _kurs(con)
    side = _klient().get("/kurs/H1").get_data(as_text=True)
    assert "HPR" not in side and 'name="hpr_nr"' not in side


def test_innsendt_hpr_lagres_ikke(con):
    _kurs(con)
    assert _klient().post("/kurs/H1", data={**BASIS, "hpr_nr": "1234567"}).status_code == 200
    assert _person(con) is not None and _person(con)["hpr_nr"] is None


def test_hpr_som_alt_staar_paa_personen_beholdes(con):
    _kurs(con)
    _kurs(con, kode="H2")
    _klient().post("/kurs/H1", data=BASIS)
    con.execute("UPDATE deltaker SET hpr_nr='7654321' WHERE epost=?", (EPOST,))
    con.commit()
    assert _klient().post("/kurs/H2", data=BASIS).status_code == 200
    assert _person(con)["hpr_nr"] == "7654321"


def test_bedriftspaamelding_til_spesialistlop_har_ikke_hpr_felt(con):
    _kurs(con)
    side = _klient().get("/kurs/H1/gruppe").get_data(as_text=True)
    assert "deltaker_hpr" not in side and "HPR" not in side


def test_skjemabyggeren_har_ingen_rad_for_hpr(con):
    kid = _kurs(con)
    side = _klient(admin=True).get(f"/admin/kurs/{kid}/paameldingsskjema").get_data(as_text=True)
    assert "HPR" not in side and "hpr_nr" not in side


def test_legg_til_deltaker_for_administrator_har_fortsatt_hpr_feltet(con):
    kid = _kurs(con)
    admin = _klient(admin=True)
    assert 'name="hpr_nr"' in admin.get(f"/admin/kurs/{kid}/deltaker/ny").get_data(as_text=True)
