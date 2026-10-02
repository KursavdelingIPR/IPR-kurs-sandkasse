"""Fase 1: aktivitetsoversikt, kursfaner og deltakerliste i admin."""
import sqlite3
from datetime import date, timedelta

import pytest

from kurs import config, db
from kurs.web.app import _fakturastatus
from listehjelp import synlige_rader


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


def _logg_inn(klient, brukernavn=None, passord=None):
    klient.post("/admin/logg-inn", data={
        "brukernavn": brukernavn or config.ADMIN_BRUKERNAVN,
        "passord": passord or config.ADMIN_PASSORD,
    })


def _kurs(con, kode, navn="Testkurs", start=None, **kw):
    start = start or date(2027, 5, 1)
    return db.opprett_kurs(con, kode=kode, navn=navn, datoer=[start.isoformat()],
                           sharepoint_mappe=f"Kurs/{kode}", **{"pris_nok": 1000, **kw})


# ---------------- migrering ----------------

@pytest.mark.kun_sqlite  # lager en gammel SQLite-fil direkte med sqlite3 (migreringstest for lokale databaser)
def test_migrering_legger_til_oppdatert_og_backfiller_uten_datatap(tmp_path):
    """Simulerer en database fra for feltene i fase 1 fantes. Ingen ALTER med ikke-konstant default."""
    sti = tmp_path / "gammel.db"
    gammel = sqlite3.connect(sti)
    gammel.row_factory = sqlite3.Row
    gammel.execute("CREATE TABLE admin_bruker (id INTEGER PRIMARY KEY, brukernavn TEXT, navn TEXT, "
                   "passord_hash TEXT, aktiv INTEGER DEFAULT 1)")
    gammel.execute("CREATE TABLE kurs (id INTEGER PRIMARY KEY, kode TEXT)")
    gammel.execute("CREATE TABLE deltaker (id INTEGER PRIMARY KEY, epost TEXT)")
    gammel.execute("CREATE TABLE paamelding (id INTEGER PRIMARY KEY, kurs_id INTEGER, deltaker_id INTEGER, "
                   "status TEXT, opprettet TEXT)")
    gammel.execute("INSERT INTO paamelding (id, kurs_id, deltaker_id, status, opprettet) "
                   "VALUES (1, 1, 1, 'bekreftet', '2020-06-01T10:00:00')")
    gammel.commit()
    gammel.close()

    con = sqlite3.connect(sti)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    db._migrer(con)
    con.commit()

    assert any(r["name"] == "ansvarlig_admin_id" for r in con.execute("PRAGMA table_info(kurs)"))
    assert any(r["name"] == "yrkestittel" for r in con.execute("PRAGMA table_info(deltaker)"))
    rad = con.execute("SELECT oppdatert FROM paamelding WHERE id=1").fetchone()
    assert rad["oppdatert"] == "2020-06-01T10:00:00"  # backfillet fra opprettet, ikke NULL eller "na"

    db._migrer(con)  # kjøres to ganger -> ingen feil, ingen endring
    con.commit()


def test_ny_database_har_de_samme_kolonnene(con):
    assert "ansvarlig_admin_id" in db.kolonner(con, "kurs")
    assert "yrkestittel" in db.kolonner(con, "deltaker")
    assert "oppdatert" in db.kolonner(con, "paamelding")


# ---------------- aktivitetsoversikt ----------------

def test_vis_bare_mine_aktiviteter_filtrerer_paa_ansvarlig(con):
    admin_id = con.execute("SELECT id FROM admin_bruker WHERE brukernavn=?", (config.ADMIN_BRUKERNAVN,)).fetchone()["id"]
    annen_id = db.opprett_admin_bruker(con, "kari", "Kari Admin", "passord123")
    _kurs(con, "MITT", navn="Mitt kurs", ansvarlig_admin_id=admin_id)
    _kurs(con, "ANNET", navn="Annet kurs", ansvarlig_admin_id=annen_id)
    con.commit()

    klient = _klient()
    _logg_inn(klient)
    tekst_alle = klient.get("/admin").get_data(as_text=True)
    assert "Mitt kurs" in tekst_alle and "Annet kurs" in tekst_alle

    tekst_mine = klient.get("/admin?mine=1").get_data(as_text=True)
    assert "Mitt kurs" in tekst_mine
    assert "Annet kurs" not in tekst_mine


def test_soek_paa_kursnavn(con):
    _kurs(con, "A1", navn="EFT spesialistutdanning")
    _kurs(con, "A2", navn="Parterapi i praksis")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get("/admin?sok=parterapi").get_data(as_text=True)
    assert "Parterapi i praksis" in tekst
    assert "EFT spesialistutdanning" not in tekst


def test_antall_rader_begrenser_resultatet(con):
    for n in range(3):
        _kurs(con, f"K{n}", navn=f"Kurs {n}", start=date(2027, 5, 1) + timedelta(days=n))
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = klient.get("/admin?antall=10")
    assert r.status_code == 200
    for n in range(3):
        assert f"Kurs {n}" in r.get_data(as_text=True)


def test_standardsortering_setter_avsluttede_kurs_sist(con):
    _kurs(con, "GAMMEL", navn="Gammelt kurs", start=date(2020, 1, 1), status="avsluttet")
    _kurs(con, "FREMTID", navn="Fremtidig kurs", start=date(2030, 1, 1))
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get("/admin?status=alle").get_data(as_text=True)          # standardvisningen skjuler avsluttede kurs
    assert tekst.index("Fremtidig kurs") < tekst.index("Gammelt kurs")


def test_meld_av_skjules_for_avsluttet_kurs(con):
    kid = _kurs(con, "FERDIG")
    db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))  # slik daglig.py setter det etter siste dag
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    assert "Meld av" not in tekst


def test_gammel_kurslenke_redirigerer_til_deltakerfanen(con):
    kid = _kurs(con, "REDIR")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = klient.get(f"/admin/kurs/{kid}", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["Location"].endswith(f"/admin/kurs/{kid}/deltakere")


def test_ansvarlig_kan_settes_og_fjernes(con):
    kid = _kurs(con, "ANS")
    admin_id = con.execute("SELECT id FROM admin_bruker").fetchone()["id"]
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(f"/admin/kurs/{kid}/ansvarlig", data={"ansvarlig_admin_id": str(admin_id)})
    assert db.koble(config.DB_STI).execute("SELECT ansvarlig_admin_id FROM kurs WHERE id=?", (kid,)).fetchone()[0] == admin_id
    klient.post(f"/admin/kurs/{kid}/ansvarlig", data={"ansvarlig_admin_id": ""})
    assert db.koble(config.DB_STI).execute("SELECT ansvarlig_admin_id FROM kurs WHERE id=?", (kid,)).fetchone()[0] is None


# ---------------- deltakere-fanen ----------------

def test_statustellere_stemmer(con):
    import re
    kid = _kurs(con, "TEL", kapasitet=1)
    db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    db.meld_paa(con, kid, epost="b@x.no", fornavn="B", etternavn="Test")  # havner paa venteliste, kapasitet=1
    pc, _ = db.meld_paa(con, kid, epost="c@x.no", fornavn="C", etternavn="kapasitet")  # ogsaa venteliste
    db.meld_av(con, pc)  # avmeldt, ingen paa venteliste til aa rykke opp siden b framleis venter
    con.commit()

    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    tall = [int(n) for n in re.findall(r'<div class="stat">(\d+)</div>', tekst)]
    assert tall == [1, 1, 1, 3]  # Bekreftet, Venteliste, Avmeldt, Totalt registrert – i den rekkefolgen kortene vises


def test_soek_og_statusfilter_paa_deltakere(con):
    kid = _kurs(con, "SOEK", kapasitet=1)
    db.meld_paa(con, kid, epost="kari@x.no", fornavn="Kari", etternavn="Nordmann")
    db.meld_paa(con, kid, epost="ola@x.no", fornavn="Ola", etternavn="Hansen")  # venteliste
    con.commit()

    klient = _klient()
    _logg_inn(klient)
    tekst = synlige_rader(klient.get(f"/admin/kurs/{kid}/deltakere?sok=kari").get_data(as_text=True))
    assert "Kari Nordmann" in tekst and "Ola Hansen" not in tekst

    tekst = synlige_rader(klient.get(f"/admin/kurs/{kid}/deltakere?status=venteliste").get_data(as_text=True))
    assert "Ola Hansen" in tekst and "Kari Nordmann" not in tekst


def test_sist_oppdatert_endres_ved_avmelding(con):
    kid = _kurs(con, "OPPD")
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    forste = con.execute("SELECT oppdatert FROM paamelding WHERE id=?", (pid,)).fetchone()[0]
    db.meld_av(con, pid)
    con.commit()
    etter = con.execute("SELECT oppdatert, status FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert etter["status"] == "avmeldt"
    assert etter["oppdatert"] >= forste


def test_fakturastatus_utledes_riktig():
    kurs_gratis = {"fakturering": "person", "pris_nok": 0}
    kurs_betalt = {"fakturering": "person", "pris_nok": 1000}
    assert _fakturastatus(kurs_gratis, {"faktura_antall": 0, "faktura_ubetalt": 0}) == "–"
    assert _fakturastatus(kurs_betalt, {"faktura_antall": 0, "faktura_ubetalt": 0}) == "Ikke fakturert"
    assert _fakturastatus(kurs_betalt, {"faktura_antall": 1, "faktura_ubetalt": 0}) == "Betalt"
    assert _fakturastatus(kurs_betalt, {"faktura_antall": 3, "faktura_ubetalt": 3}) == "Fakturert"
    assert _fakturastatus(kurs_betalt, {"faktura_antall": 3, "faktura_ubetalt": 1}) == "Delvis betalt"


def test_oppmotefunksjon_er_uendret_paa_ny_side(con):
    kid = _kurs(con, "OPP")
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    dag = db.kursdager(con, kid)[0]
    klient = _klient()
    _logg_inn(klient)
    r = klient.post(f"/admin/kurs/{kid}/oppmote", data={"paamelding_id": str(pid), "kursdag_id": str(dag["id"])},
                    follow_redirects=True)
    assert r.status_code == 200
    assert db.koble(config.DB_STI).execute(
        "SELECT COUNT(*) FROM oppmote WHERE paamelding_id=? AND kursdag_id=?", (pid, dag["id"])
    ).fetchone()[0] == 1


# ---------------- fase 8: samme filtre som kalenderen (sted/ansvarlig/status) ----------------

def test_bergen_filter_paavirker_listen(con):
    _kurs(con, "BGO", navn="Bergenskurs", type="fysisk", sted="IPR, Bergen")
    _kurs(con, "OSL", navn="Oslokurs", type="fysisk", sted="Oslo, hotell")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = klient.get("/admin?sted=bergen").get_data(as_text=True)
    assert "Bergenskurs" in t and "Oslokurs" not in t


def test_oslo_filter_paavirker_listen(con):
    _kurs(con, "BGO", navn="Bergenskurs", type="fysisk", sted="IPR, Bergen")
    _kurs(con, "OSL", navn="Oslokurs", type="fysisk", sted="Oslo, hotell")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = klient.get("/admin?sted=oslo").get_data(as_text=True)
    assert "Oslokurs" in t and "Bergenskurs" not in t


def test_online_filter_fanger_digital_type_og_stedtekst_som_i_kalenderen(con):
    _kurs(con, "DIG", navn="Digitalt kurs", type="digital")
    _kurs(con, "ZOOMSTED", navn="Kurs med Zoom i stedfelt", type="fysisk", sted="Zoom-rom 1")
    _kurs(con, "FYS", navn="Fysisk kurs", type="fysisk", sted="IPR, Bergen")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = klient.get("/admin?sted=online").get_data(as_text=True)
    assert "Digitalt kurs" in t and "Kurs med Zoom i stedfelt" in t
    assert "Fysisk kurs" not in t


def test_ansvarlig_dropdown_filter_paavirker_listen(con):
    admin_id = con.execute("SELECT id FROM admin_bruker WHERE brukernavn=?", (config.ADMIN_BRUKERNAVN,)).fetchone()[0]
    annen_id = db.opprett_admin_bruker(con, "kari", "Kari Admin", "passord123")
    _kurs(con, "MITT", navn="Mitt kurs", ansvarlig_admin_id=admin_id)
    _kurs(con, "ANNET", navn="Annet kurs", ansvarlig_admin_id=annen_id)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = klient.get(f"/admin?ansvarlig={admin_id}").get_data(as_text=True)
    assert "Mitt kurs" in t and "Annet kurs" not in t


def test_statusfilter_paavirker_listen(con):
    _kurs(con, "AAPEN", navn="Åpent kurs")
    _kurs(con, "AVLYST", navn="Avlyst kurs", status="avlyst")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = klient.get("/admin?status=avlyst").get_data(as_text=True)
    assert "Avlyst kurs" in t and "Åpent kurs" not in t


def test_nye_filtre_kan_kombineres_med_soek_og_mine(con):
    admin_id = con.execute("SELECT id FROM admin_bruker WHERE brukernavn=?", (config.ADMIN_BRUKERNAVN,)).fetchone()[0]
    annen_id = db.opprett_admin_bruker(con, "kari", "Kari Admin", "passord123")
    _kurs(con, "TREFF", navn="Bergen mitt kurs", type="fysisk", sted="IPR, Bergen", ansvarlig_admin_id=admin_id)
    _kurs(con, "FEIL1", navn="Bergen annet kurs", type="fysisk", sted="IPR, Bergen", ansvarlig_admin_id=annen_id)
    _kurs(con, "FEIL2", navn="Oslo mitt kurs", type="fysisk", sted="Oslo", ansvarlig_admin_id=admin_id)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = klient.get(f"/admin?sted=bergen&ansvarlig={admin_id}&mine=1&sok=bergen").get_data(as_text=True)
    assert "Bergen mitt kurs" in t
    assert "Bergen annet kurs" not in t
    assert "Oslo mitt kurs" not in t


def test_nye_filtre_beholdes_i_sorteringslenker(con):
    _kurs(con, "BGO", navn="Bergenskurs", type="fysisk", sted="IPR, Bergen")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = klient.get("/admin?sted=bergen").get_data(as_text=True)
    assert "sted=bergen" in t


def test_nullstill_filtre_paa_aktivitetsoversikten(con):
    _kurs(con, "BGO", navn="Bergenskurs", type="fysisk", sted="IPR, Bergen")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = klient.get("/admin?sted=bergen").get_data(as_text=True)
    assert 'href="/admin"' in t
    assert "Nullstill filtre" in t
