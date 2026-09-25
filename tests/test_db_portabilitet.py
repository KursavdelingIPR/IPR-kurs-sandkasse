"""PostgreSQL-klargjoring, fase 1: SQLite-spesifikk SQL er gjort portabel UTEN endret oppforsel (fortsatt SQLite).

  * datetime('now') i SQL -> db.naa_utc() / db.utc_minutter_siden(): SAMME format og tidssone (UTC) som foer.
  * cursor.lastrowid -> db.sett_inn() med RETURNING id.
  * INSERT OR IGNORE -> ON CONFLICT DO NOTHING, INSERT OR REPLACE (sensitivt) -> ON CONFLICT ... DO UPDATE.
  * date(x) i SQL -> substr(x, 1, 10).
  * sqlite3.Error / IntegrityError -> db.DatabaseFeil / db.IntegritetsFeil (kun db.py kjenner sqlite3).
"""
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from kurs import config, db

KURS_MAPPE = Path(__file__).resolve().parent.parent / "kurs"


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _kurs(con, kode="P1"):
    kid = db.opprett_kurs(con, kode=kode, navn="Eksempelkurs", datoer=["2099-03-02", "2099-03-03"],
                          sharepoint_mappe=f"K/{kode}")
    con.commit()
    return kid


# ============================ tid: identisk med SQLite sin datetime('now') ============================

def _sekunder(a: str, b: str) -> float:
    return abs((datetime.strptime(a, "%Y-%m-%d %H:%M:%S") - datetime.strptime(b, "%Y-%m-%d %H:%M:%S")).total_seconds())


def _db_naa(con, minutter: int = 0) -> str:
    """Databasens EGEN «naa» (UTC) i lagringsformatet: SQLite datetime('now', ...) / PostgreSQL samme uttrykk som
    standardverdiene i schema_postgres.sql."""
    if db.er_postgres(con):
        return con.execute("SELECT to_char((now() - make_interval(mins => ?)) AT TIME ZONE 'UTC', "
                           "'YYYY-MM-DD HH24:MI:SS')", (minutter,)).fetchone()[0]
    return con.execute("SELECT datetime('now', ?)", (f"-{minutter} minutes",)).fetchone()[0]


def test_naa_utc_har_samme_format_og_tidssone_som_sqlite(con):
    sqlite_naa = _db_naa(con)
    python_naa = db.naa_utc()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", python_naa)
    assert len(python_naa) == len(sqlite_naa) and _sekunder(python_naa, sqlite_naa) <= 2


def test_utc_minutter_siden_er_som_sqlite_modifikator(con):
    sqlite_verdi = _db_naa(con, 5)
    assert _sekunder(db.utc_minutter_siden(5), sqlite_verdi) <= 2


def test_sendt_ts_sammenlignes_mot_riktig_klokke(con):
    """Foreldet reservasjon (eldre enn grensen) telles, fersk telles ikke - samme resultat som med datetime('now', ?)."""
    assert db.reserver_sending(con, "k:1", "a@eksempel.no", "t")
    assert db.uavklarte_operasjoner(con)["epost"] == 0
    con.execute("UPDATE utsending_logg SET sendt_ts=? WHERE mottaker='a@eksempel.no'",
                (db.utc_minutter_siden(db.UAVKLART_GRENSE_MIN + 1),))
    assert db.uavklarte_operasjoner(con)["epost"] == 1


def test_sett_sendt_bruker_utc_tid(con):
    db.reserver_sending(con, "k:1", "a@eksempel.no", "t")
    db.sett_sendt(con, "k:1", "a@eksempel.no", "t")
    ts = con.execute("SELECT sendt_ts FROM utsending_logg").fetchone()[0]
    assert _sekunder(ts, _db_naa(con)) <= 2


# ============================ RETURNING id ============================

def test_sett_inn_returnerer_ny_id_og_commit_fungerer(con):
    a = db.sett_inn(con, "INSERT INTO hendelse (aktor, handling) VALUES (?,?)", ("test", "en"))
    b = db.sett_inn(con, "INSERT INTO hendelse (aktor, handling) VALUES (?,?)", ("test", "to"))
    con.commit()                                               # ingen «SQL statements in progress»
    assert b == a + 1
    assert con.execute("SELECT handling FROM hendelse WHERE id=?", (b,)).fetchone()[0] == "to"


def test_opprett_kurs_og_deltaker_gir_riktige_id_er(con):
    kid = _kurs(con)
    assert con.execute("SELECT kode FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "P1"
    did = db.finn_eller_opprett_deltaker(con, "d@eksempel.no", "Deltaker")
    assert con.execute("SELECT epost FROM deltaker WHERE id=?", (did,)).fetchone()[0] == "d@eksempel.no"
    pid, _ = db.meld_paa(con, kid, epost="p@eksempel.no", navn="P")
    assert con.execute("SELECT kurs_id FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == kid


# ============================ ON CONFLICT ============================

def test_reserver_sending_vinner_kun_en_gang(con):
    assert db.reserver_sending(con, "k:1", "a@eksempel.no", "t") is True
    assert db.reserver_sending(con, "k:1", "a@eksempel.no", "t") is False
    assert db.reserver_sending(con, "k:1", "A@EKSEMPEL.NO", "t") is False     # COLLATE NOCASE gjelder fortsatt
    assert con.execute("SELECT COUNT(*) FROM utsending_logg").fetchone()[0] == 1


def test_marker_sendt_er_idempotent(con):
    db.marker_sendt(con, "k:1", "a@eksempel.no", "t")
    db.marker_sendt(con, "k:1", "a@eksempel.no", "t")
    assert con.execute("SELECT COUNT(*) FROM utsending_logg").fetchone()[0] == 1


@pytest.mark.parametrize("kursdag", [None, "dag"])
def test_reserver_faktura_vinner_kun_en_gang_ogsaa_via_uttrykksindeks(con, kursdag):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="p@eksempel.no", navn="P")
    dag_id = db.kursdager(con, kid)[0]["id"] if kursdag else None
    assert db.reserver_faktura(con, pid, dag_id) is True
    assert db.reserver_faktura(con, pid, dag_id) is False            # faktura_forsok_unik: COALESCE(kursdag_id, 0)
    assert con.execute("SELECT COUNT(*) FROM faktura_forsok").fetchone()[0] == 1


def test_oppmote_registreres_kun_en_gang(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="p@eksempel.no", navn="P")
    dag_id = db.kursdager(con, kid)[0]["id"]
    assert db.registrer_oppmote(con, pid, dag_id, "manuell") is True
    assert db.registrer_oppmote(con, pid, dag_id, "manuell") is False


def test_sensitivt_upsert_oppdaterer_eksisterende_rad(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="p@eksempel.no", navn="P", sensitivt={"allergier": "A", "tilrettelegging": None})
    assert db.oppdater_sensitivt(con, pid, "B", "T") is True
    rader = con.execute("SELECT * FROM sensitivt WHERE paamelding_id=?", (pid,)).fetchall()
    assert [(r["allergier"], r["tilrettelegging"]) for r in rader] == [("B", "T")]


def test_sensitivt_upsert_ved_reaktivert_paamelding(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="p@eksempel.no", navn="P", sensitivt={"allergier": "A"})
    db.meld_av(con, pid)
    pid2, _ = db.meld_paa(con, kid, epost="p@eksempel.no", navn="P", sensitivt={"allergier": "NY"})
    assert pid2 == pid
    assert con.execute("SELECT allergier FROM sensitivt WHERE paamelding_id=?", (pid,)).fetchall()[0][0] == "NY"


# ============================ felles unntak ============================

def test_felles_unntak_er_sqlite_sine_i_fase_1():
    assert db.DatabaseFeil is sqlite3.Error and db.IntegritetsFeil is sqlite3.IntegrityError


def test_firmapaamelding_gjenkjennes_via_integritetsfeil(con):
    kid = _kurs(con)
    kontakt = {"navn": "K", "epost": "k@eksempel.no", "firmanavn": "F"}
    a, ny_a = db.finn_eller_opprett_firmapaamelding(con, kid, kontakt, ["x@eksempel.no"])
    b, ny_b = db.finn_eller_opprett_firmapaamelding(con, kid, kontakt, ["x@eksempel.no"])
    assert (ny_a, ny_b) == (True, False) and a["id"] == b["id"]


# ============================ vakt: SQLite-spesifikk SQL utenfor db.py ============================

_FORBUDT = [r"\bsqlite3\b", r"lastrowid", r"INSERT\s+OR\s+(IGNORE|REPLACE)", r"datetime\('now'", r"\bdate\(\s*(ts|opprettet)"]


@pytest.mark.parametrize("fil", sorted(p for p in KURS_MAPPE.rglob("*.py") if p.name != "db.py"), ids=lambda p: p.name)
def test_ingen_sqlite_spesifikk_sql_utenfor_db_py(fil):
    tekst = fil.read_text(encoding="utf-8")
    funn = [m for m in _FORBUDT if re.search(m, tekst)]
    assert funn == [], f"{fil.name}: {funn}"


def test_db_py_bruker_ikke_lenger_sqlite_spesifikk_sql_i_sporringer():
    """Kommentarer/dokstrenger kan nevne de gamle formene; selve SQL-strengene skal ikke bruke dem."""
    kode = (KURS_MAPPE / "db.py").read_text(encoding="utf-8")
    uten_kommentarer = "\n".join(l for l in kode.splitlines() if not l.strip().startswith("#"))
    for m in [r'"INSERT\s+OR', r"'INSERT\s+OR", r"\"\"\"INSERT\s+OR", r"=datetime\('now'\)", r"< datetime\('now'",
              r"\bcur\.lastrowid\b"]:
        assert not re.search(m, uten_kommentarer), m
