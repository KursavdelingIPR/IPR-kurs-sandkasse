"""Admin-innlogging (egne brukere, hashet passord), hendelseslogg og automatisk kurskode."""
from datetime import date

import pytest

from kurs import config, db


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    return c


def _klient(con):
    from kurs.web import app as webapp
    return webapp.app.test_client()


def test_bootstrap_lager_forste_admin(con):
    assert con.execute("SELECT COUNT(*) FROM admin_bruker").fetchone()[0] == 1
    assert db.verifiser_admin(con, config.ADMIN_BRUKERNAVN, config.ADMIN_PASSORD) is not None


def test_passord_lagres_hashet_ikke_klartekst(con):
    db.opprett_admin_bruker(con, "test.bruker", "Test Bruker", "et-hemmelig-passord")
    rad = con.execute("SELECT passord_hash FROM admin_bruker WHERE brukernavn='test.bruker'").fetchone()
    assert "et-hemmelig-passord" not in rad["passord_hash"]


def test_feil_passord_avvises(con):
    db.opprett_admin_bruker(con, "test.bruker", "Test Bruker", "riktig-passord")
    assert db.verifiser_admin(con, "test.bruker", "feil-passord") is None
    assert db.verifiser_admin(con, "test.bruker", "riktig-passord") is not None


def test_deaktivert_bruker_kan_ikke_logge_inn(con):
    bid = db.opprett_admin_bruker(con, "sluttet", "Sluttet Ansatt", "passord123")
    con.execute("UPDATE admin_bruker SET aktiv=0 WHERE id=?", (bid,))
    assert db.verifiser_admin(con, "sluttet", "passord123") is None


def test_web_innlogging_og_hendelseslogg_viser_hvem(con, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    db.opprett_admin_bruker(con, "kari", "Kari Admin", "sikkertpassord")
    con.commit()
    klient = _klient(con)
    r = klient.post("/admin/logg-inn", data={"brukernavn": "kari", "passord": "sikkertpassord"}, follow_redirects=True)
    assert r.status_code == 200
    kid = db.opprett_kurs(con, kode="LOGG1", navn="Loggtest", datoer=["2027-05-01"], aktor="admin:kari")
    con.commit()
    hendelse = con.execute("SELECT aktor FROM hendelse WHERE handling='kurs_opprettet' AND detaljer LIKE ?",
                           (f'%"kurs_id": {kid}%',)).fetchone()
    assert hendelse["aktor"] == "admin:kari"


def test_feil_innlogging_avvises(con):
    db.opprett_admin_bruker(con, "kari", "Kari Admin", "sikkertpassord")
    con.commit()
    r = _klient(con).post("/admin/logg-inn", data={"brukernavn": "kari", "passord": "feil"})
    assert "Feil brukernavn eller passord" in r.get_data(as_text=True)


def test_kurskode_genereres_automatisk_og_unikt(con):
    kode1 = db.generer_kode(con, "EFT spesialistutdanning – samling 1", ["2027-01-10"])
    assert kode1 == "EFT-2027"
    db.opprett_kurs(con, kode=kode1, navn="EFT spesialistutdanning – samling 1", datoer=["2027-01-10"])
    kode2 = db.generer_kode(con, "EFT spesialistutdanning – samling 2", ["2027-03-01"])
    assert kode2 == "EFT-2027-2"  # samme prefiks og aar -> maa vaere unik


def test_kurskode_haandterer_norske_bokstaver(con):
    kode = db.generer_kode(con, "Åpen dag for nye deltakere", ["2027-06-01"])
    assert kode == "AAPE-2027"
