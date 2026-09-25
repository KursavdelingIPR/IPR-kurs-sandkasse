"""Fase 13: rollebasert admin-tilgang (system / kursadmin / lese) - haandhevet server-side.

Ogsaa migrering 2 (eksisterende brukere blir system), brukeradministrasjon og vern av siste systemadministrator.
"""
from datetime import date, timedelta

import pytest

from kurs import config, db, migreringer


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _klient():
    from kurs.web import app as webapp
    return webapp.app.test_client()


def _logg_inn(brukernavn, passord):
    k = _klient()
    r = k.post("/admin/logg-inn", data={"brukernavn": brukernavn, "passord": passord})
    assert r.status_code == 302 and r.headers["Location"] == "/admin", brukernavn
    return k


def _bruker(con, rolle, navn=None):
    navn = navn or rolle
    db.opprett_admin_bruker(con, navn, navn.title(), "passord-som-holder", rolle=rolle)
    con.commit()
    return _logg_inn(navn, "passord-som-holder")


def _kurs(con, kode="R1"):
    start = date.today() + timedelta(days=30)
    kid = db.opprett_kurs(con, kode=kode, navn="Rollekurs", datoer=[start.isoformat()], sharepoint_mappe=f"Kurs/{kode}",
                          pris_nok=1000, type="fysisk")
    db.meld_paa(con, kid, epost="d@x.no", navn="D", sensitivt={"allergier": "Nøtter"})
    con.commit()
    return kid


# ============================ modell ============================

def test_forste_bruker_er_systemadministrator(con):
    assert con.execute("SELECT rolle FROM admin_bruker").fetchone()[0] == "system"


def test_ny_bruker_er_kursadmin_som_standard(con):
    bid = db.opprett_admin_bruker(con, "k", "K", "passord-som-holder")
    assert con.execute("SELECT rolle FROM admin_bruker WHERE id=?", (bid,)).fetchone()[0] == "kursadmin"
    with pytest.raises(db.RolleFeil):
        db.opprett_admin_bruker(con, "x", "X", "passord-som-holder", rolle="superbruker")
    with pytest.raises(db.IntegritetsFeil):
        con.execute("UPDATE admin_bruker SET rolle='superbruker' WHERE id=?", (bid,))     # CHECK i databasen
    con.rollback()


def test_siste_systemadministrator_kan_ikke_fratas_rollen_eller_deaktiveres(con):
    meg = con.execute("SELECT id FROM admin_bruker").fetchone()[0]
    with pytest.raises(db.RolleFeil):
        db.sett_admin_rolle(con, meg, "lese")
    andre = db.opprett_admin_bruker(con, "b", "B", "passord-som-holder", rolle="system")
    assert db.sett_admin_rolle(con, meg, "lese") is True
    with pytest.raises(db.RolleFeil):
        db.sett_admin_rolle(con, andre, "kursadmin")
    con.commit()
    admin = _logg_inn("b", "passord-som-holder")
    admin.post(f"/admin/brukere/{andre}/deaktiver")                     # seg selv: avvist
    assert con.execute("SELECT aktiv FROM admin_bruker WHERE id=?", (andre,)).fetchone()[0] == 1


@pytest.mark.kun_sqlite  # gjenskaper databasen FOER migrering 2
def test_migrering_2_gir_eksisterende_brukere_systemrolle(tmp_path, monkeypatch):
    import re
    import sqlite3
    monkeypatch.setattr(config, "DB_STI", tmp_path / "gammel.db")
    monkeypatch.setattr(config, "DEMO", True)
    skjema = re.sub(r"--[^\n]*", "", (config.ROT / "kurs" / "schema.sql").read_text(encoding="utf-8"))
    skjema = re.sub(r"\n\s+rolle\s+TEXT NOT NULL DEFAULT 'kursadmin'[^\n]*", "", skjema)
    skjema = re.sub(r"\n\s+entra_oid\s+TEXT UNIQUE,", "", skjema)
    skjema = re.sub(r"\n\s+epost\s+TEXT,\n(\s+opprettet)", r"\n\1", skjema, count=1) if False else skjema
    # epost-kolonnen: fjern kun den i admin_bruker (deltaker har ogsaa epost)
    skjema = re.sub(r"(passord_hash\s+TEXT NOT NULL,\n\s+aktiv[^\n]*\n)\s+epost\s+TEXT,\n", r"\1", skjema)
    raa = sqlite3.connect(tmp_path / "gammel.db")
    raa.executescript(skjema)
    assert "rolle" not in [r[1] for r in raa.execute("PRAGMA table_info(admin_bruker)")]
    raa.execute("INSERT INTO admin_bruker (brukernavn, navn, passord_hash) VALUES ('a', 'A', 'x')")
    raa.execute("INSERT INTO admin_bruker (brukernavn, navn, passord_hash, aktiv) VALUES ('b', 'B', 'x', 0)")
    raa.execute("INSERT INTO schema_versjon (versjon, navn) VALUES (1, 'kursnummer')")
    raa.execute("INSERT INTO teller (navn, verdi) VALUES ('kursnr', 1000)")
    raa.commit()
    raa.close()
    c = db.koble()
    assert db.init(c) == [2]
    assert [tuple(r) for r in c.execute("SELECT brukernavn, rolle, entra_oid FROM admin_bruker ORDER BY id")] == [
        ("a", "system", None), ("b", "system", None)]
    assert migreringer.gjeldende_versjon(c) == migreringer.KODEVERSJON
    c.close()


# ============================ haandhevelse ============================

def test_lesetilgang_kan_se_men_ikke_endre(con):
    kid = _kurs(con)
    lese = _bruker(con, "lese")
    assert lese.get("/admin").status_code == 200
    assert lese.get("/admin/aktiviteter").status_code == 200
    assert lese.get(f"/admin/kurs/{kid}/deltakere").status_code == 200
    assert lese.get(f"/admin/kurs/{kid}/oppsett").status_code == 200
    assert lese.get("/admin/rapporter").status_code == 200
    r = lese.post(f"/admin/kurs/{kid}/oppsett", data={"navn": "Endret", "type": "fysisk", "fakturering": "person",
                                                     "betaling": "samlet"})
    assert r.status_code == 403
    assert con.execute("SELECT navn FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "Rollekurs"
    assert lese.post("/admin/kurs/ny", data={"navn": "Nytt", "type": "fysisk", "datoer": "2028-01-01", "start_kl": "09:00",
                                             "slutt_kl": "16:00", "fakturering": "person"}).status_code == 403
    assert con.execute("SELECT COUNT(*) FROM kurs").fetchone()[0] == 1


def test_lesetilgang_faar_ikke_personopplysninger_eksportert(con):
    kid = _kurs(con)
    lese = _bruker(con, "lese")
    assert lese.get(f"/admin/kurs/{kid}/deltakere.csv").status_code == 403
    assert lese.get(f"/admin/kurs/{kid}/allergiliste").status_code == 403
    assert lese.get("/admin/rapporter/deltakere.csv").status_code == 403
    assert lese.get("/admin/rapporter/kurs.csv").status_code == 403
    assert lese.post(f"/admin/kurs/{kid}/deltakere/eksporter-valgte.csv", data={"paamelding_id": "1"}).status_code == 403
    assert lese.get("/admin/brukere").status_code == 403
    assert lese.get("/admin/daglig").status_code == 403
    assert lese.get("/admin/logg-ut").status_code == 302          # utlogging er alltid lov


def test_kursadmin_kan_alt_daglig_men_ikke_brukere_og_daglig_jobb(con):
    kid = _kurs(con)
    k = _bruker(con, "kursadmin")
    assert k.get("/admin/brukere").status_code == 403
    assert k.post("/admin/brukere", data={"navn": "x", "brukernavn": "x", "passord": "passord-som-holder"}).status_code == 403
    assert k.get("/admin/daglig").status_code == 403
    assert k.post("/admin/brukere/1/deaktiver").status_code == 403
    assert k.get(f"/admin/kurs/{kid}/deltakere.csv").status_code == 200
    assert k.get(f"/admin/kurs/{kid}/allergiliste").status_code == 200
    assert k.get("/admin/e-postmaler").status_code == 200
    r = k.post(f"/admin/kurs/{kid}/oppsett", data={"navn": "Endret", "type": "fysisk", "fakturering": "person",
                                                  "betaling": "samlet", "pris_nok": "1000"})
    assert r.status_code == 302
    assert con.execute("SELECT navn FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "Endret"
    assert con.execute("SELECT COUNT(*) FROM admin_bruker WHERE brukernavn='x'").fetchone()[0] == 0


def test_systemadmin_kan_alt(con):
    kid = _kurs(con)
    s = _logg_inn(config.ADMIN_BRUKERNAVN, config.ADMIN_PASSORD)
    assert s.get("/admin/brukere").status_code == 200
    assert s.get("/admin/daglig").status_code == 200
    assert s.get(f"/admin/kurs/{kid}/deltakere.csv").status_code == 200


def test_rolleendring_virker_ved_neste_request_uten_ny_innlogging(con):
    kid = _kurs(con)
    k = _bruker(con, "kursadmin", "kari")
    assert k.get(f"/admin/kurs/{kid}/deltakere.csv").status_code == 200
    bid = con.execute("SELECT id FROM admin_bruker WHERE brukernavn='kari'").fetchone()[0]
    db.sett_admin_rolle(con, bid, "lese")
    con.commit()
    assert k.get(f"/admin/kurs/{kid}/deltakere.csv").status_code == 403


def test_navigasjon_skjuler_systemsider_for_andre_roller(con):
    k = _bruker(con, "kursadmin")
    html = k.get("/admin").get_data(as_text=True)
    assert "/admin/brukere" not in html and "/admin/daglig" not in html
    s = _logg_inn(config.ADMIN_BRUKERNAVN, config.ADMIN_PASSORD)
    html = s.get("/admin").get_data(as_text=True)
    assert "/admin/brukere" in html and "/admin/daglig" in html
    lese = _bruker(con, "lese")
    assert "lesetilgang" in lese.get("/admin").get_data(as_text=True)


# ============================ brukeradministrasjon ============================

def test_systemadmin_oppretter_bruker_med_rolle_og_endrer_rolle(con):
    s = _logg_inn(config.ADMIN_BRUKERNAVN, config.ADMIN_PASSORD)
    r = s.post("/admin/brukere", data={"navn": "Lise", "brukernavn": "lise", "passord": "passord-som-holder", "rolle": "lese"})
    assert r.status_code == 302
    rad = con.execute("SELECT * FROM admin_bruker WHERE brukernavn='lise'").fetchone()
    assert rad["rolle"] == "lese"
    s.post(f"/admin/brukere/{rad['id']}/rolle", data={"rolle": "kursadmin"})
    assert con.execute("SELECT rolle FROM admin_bruker WHERE id=?", (rad["id"],)).fetchone()[0] == "kursadmin"
    s.post(f"/admin/brukere/{rad['id']}/rolle", data={"rolle": "tull"})
    assert con.execute("SELECT rolle FROM admin_bruker WHERE id=?", (rad["id"],)).fetchone()[0] == "kursadmin"
    assert s.post("/admin/brukere", data={"navn": "K", "brukernavn": "kort", "passord": "kort", "rolle": "lese"}).status_code == 302
    assert con.execute("SELECT COUNT(*) FROM admin_bruker WHERE brukernavn='kort'").fetchone()[0] == 0   # for kort passord


def test_lokal_innlogging_kan_slaas_av_i_drift(con, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_LOKAL_INNLOGGING", False)
    r = _klient().post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    assert r.status_code == 404
    html = _klient().get("/admin/logg-inn").get_data(as_text=True)
    assert 'name="passord"' not in html and "Innlogging er ikke satt opp" in html
