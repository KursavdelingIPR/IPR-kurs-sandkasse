"""PostgreSQL-klargjoring, fase 2: adapterlaget i kurs/db.py, backend-valg og kurs.migrer.

Disse testene krever IKKE en PostgreSQL-server eller psycopg: tilkoblingen erstattes av en falsk psycopg-tilkobling som
registrerer SQL og parametere. Ekte kjoering mot PostgreSQL maa gjoeres separat (se rapporten for fase 2).
"""
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from kurs import config, db, migrer

KURS_MAPPE = Path(__file__).resolve().parent.parent / "kurs"


# ============================ falsk psycopg ============================

class FalskPgFeil(Exception):
    def __init__(self, melding, primaer=None):
        super().__init__(melding)
        self.diag = SimpleNamespace(message_primary=primaer)


class FalskPgIntegritetsFeil(FalskPgFeil):
    pass


FALSK_PSYCOPG = SimpleNamespace(Error=FalskPgFeil, IntegrityError=FalskPgIntegritetsFeil)


class FalskMarkor:
    def __init__(self, rader=None, description=None, rowcount=-1):
        self._rader, self.description, self.rowcount = list(rader or []), description, rowcount
        self.kall = []

    def fetchone(self):
        return self._rader.pop(0) if self._rader else None

    def fetchall(self):
        ut, self._rader = self._rader, []
        return ut

    def executemany(self, sql, sekvens):
        self.kall.append((sql, sekvens))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self.fetchall())


def _rad(**kol):
    navn = list(kol)
    return db.Rad(navn, {n.lower(): i for i, n in enumerate(navn)}, list(kol.values()))


class FalskRaaTilkobling:
    """Registrerer alle kall. `svar` er en funksjon (sql, params) -> FalskMarkor, eller et unntak som skal kastes."""

    def __init__(self, svar=None):
        self.kall, self.svar = [], svar or (lambda sql, params: FalskMarkor())
        self.committet = self.rullet_tilbake = self.lukket = 0
        self.markor = FalskMarkor()

    def execute(self, sql, params=None):
        self.kall.append((sql, params))
        resultat = self.svar(sql, params)
        if isinstance(resultat, Exception):
            raise resultat
        return resultat

    def cursor(self):
        return self.markor

    def commit(self):
        self.committet += 1

    def rollback(self):
        self.rullet_tilbake += 1

    def close(self):
        self.lukket += 1


def _pg(svar=None):
    raa = FalskRaaTilkobling(svar)
    return db._PgTilkobling(raa, FALSK_PSYCOPG), raa


# ============================ plassholdere ============================

@pytest.mark.parametrize("sqlite_sql,pg_sql", [
    ("SELECT * FROM kurs WHERE id=?", "SELECT * FROM kurs WHERE id=%s"),
    ("INSERT INTO t (a,b) VALUES (?,?)", "INSERT INTO t (a,b) VALUES (%s,%s)"),
    ("SELECT '?' AS spm, x FROM t WHERE y=?", "SELECT '?' AS spm, x FROM t WHERE y=%s"),
    ("WHERE LOWER(sted) LIKE '%bergen%' AND id=?", "WHERE LOWER(sted) LIKE '%%bergen%%' AND id=%s"),
    ("WHERE a LIKE ? AND b = 'it''s ?'", "WHERE a LIKE %s AND b = 'it''s ?'"),
    ('SELECT "rar?kolonne" FROM t WHERE x=?', 'SELECT "rar?kolonne" FROM t WHERE x=%s'),
    ("SELECT 10 % 3, ?", "SELECT 10 %% 3, %s"),
])
def test_plassholdere_oversettes_kun_utenfor_strenger(sqlite_sql, pg_sql):
    assert db._til_pg_sql(sqlite_sql) == pg_sql


def test_parameterverdier_som_sqlite():
    assert db._pg_verdi(True) == 1 and db._pg_verdi(False) == 0 and type(db._pg_verdi(True)) is int
    assert db._pg_verdi(date(2099, 3, 2)) == "2099-03-02"
    assert db._pg_verdi(datetime(2099, 3, 2, 9, 30, 5)) == "2099-03-02 09:30:05"
    assert db._pg_verdi("tekst") == "tekst" and db._pg_verdi(None) is None and db._pg_verdi(5) == 5


def test_datetime_parameter_er_lik_sqlite_sin_lagring(tmp_path):
    c = sqlite3.connect(tmp_path / "x.db")
    c.execute("CREATE TABLE t (v TEXT)")
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        c.execute("INSERT INTO t VALUES (?)", (datetime(2099, 3, 2, 9, 30, 5),))
        c.execute("INSERT INTO t VALUES (?)", (date(2099, 3, 2),))
    assert [r[0] for r in c.execute("SELECT v FROM t")] == [db._pg_verdi(datetime(2099, 3, 2, 9, 30, 5)),
                                                           db._pg_verdi(date(2099, 3, 2))]
    c.close()


# ============================ radtype ============================

def test_rad_oppforer_seg_som_sqlite_row(tmp_path):
    c = sqlite3.connect(tmp_path / "x.db")
    c.row_factory = sqlite3.Row
    s = c.execute("SELECT 1 AS id, 'Ola' AS navn").fetchone()
    p = _rad(id=1, navn="Ola")
    for rad in (s, p):
        assert rad["id"] == 1 and rad["navn"] == "Ola" and rad[0] == 1 and rad[1] == "Ola"
        assert rad["NAVN"] == "Ola"                                     # uten hensyn til store/smaa bokstaver
        assert list(rad.keys()) == ["id", "navn"] and dict(rad) == {"id": 1, "navn": "Ola"}
        assert list(rad) == [1, "Ola"] and len(rad) == 2 and tuple(rad) == (1, "Ola")
        with pytest.raises(IndexError):
            rad["finnes_ikke"]
    assert p == _rad(id=1, navn="Ola") and p != _rad(id=2, navn="Ola")
    c.close()


def test_rad_fabrikk_bruker_kolonnenavn_fra_markoren():
    fabrikk = db._rad_fabrikk(SimpleNamespace(description=[SimpleNamespace(name="a"), SimpleNamespace(name="B")]))
    rad = fabrikk((1, 2))
    assert rad["a"] == 1 and rad["b"] == 2 and rad["B"] == 2 and rad.keys() == ["a", "B"]


# ============================ adapter ============================

def test_execute_med_parametere_oversetter_sql_og_verdier():
    pg, raa = _pg()
    pg.execute("SELECT * FROM t WHERE a=? AND b LIKE '%x%' AND c=?", (True, date(2099, 1, 2)))
    assert raa.kall == [("SELECT * FROM t WHERE a=%s AND b LIKE '%%x%%' AND c=%s", [1, "2099-01-02"])]


def test_execute_uten_parametere_sender_sql_uendret():
    pg, raa = _pg()
    pg.execute("SELECT * FROM t WHERE sted LIKE '%oslo%'")
    assert raa.kall == [("SELECT * FROM t WHERE sted LIKE '%oslo%'", None)]


def test_markor_gir_sqlite_oppforsel_uten_resultat():
    pg, _ = _pg(lambda sql, p: FalskMarkor(description=None, rowcount=3))
    cur = pg.execute("UPDATE t SET a=1")
    assert cur.fetchone() is None and cur.fetchall() == [] and cur.rowcount == 3 and list(cur) == []


def test_markor_gir_rader_og_kan_itereres():
    rader = [_rad(id=1), _rad(id=2)]
    pg, _ = _pg(lambda sql, p: FalskMarkor(list(rader), description=[("id",)]))
    assert [r["id"] for r in pg.execute("SELECT id FROM t")] == [1, 2]


def test_sett_inn_bruker_returning_id():
    pg, raa = _pg(lambda sql, p: FalskMarkor([_rad(id=42)], description=[("id",)]))
    assert db.sett_inn(pg, "INSERT INTO t (a) VALUES (?)", ("x",)) == 42
    assert raa.kall == [("INSERT INTO t (a) VALUES (%s) RETURNING id", ["x"])]


def test_integritetsfeil_oversettes_uten_detail_og_fanges_av_felles_unntak():
    pg, _ = _pg(lambda sql, p: FalskPgIntegritetsFeil(
        'duplicate key value violates unique constraint "kurs_kode_key"\nDETAIL: Key (kode)=(HEMMELIG) already exists.',
        primaer='duplicate key value violates unique constraint "kurs_kode_key"'))
    with pytest.raises(db.IntegritetsFeil) as e:
        pg.execute("INSERT INTO kurs (kode) VALUES (?)", ("HEMMELIG",))
    assert isinstance(e.value, db.PostgresIntegritetsFeil) and isinstance(e.value, db.DatabaseFeil)
    assert "HEMMELIG" not in str(e.value) and "kurs_kode_key" in str(e.value)
    assert isinstance(e.value.__cause__, FalskPgIntegritetsFeil)


def test_annen_feil_blir_databasefeil_men_ikke_integritetsfeil():
    pg, _ = _pg(lambda sql, p: FalskPgFeil("relation does not exist", primaer='relation "x" does not exist'))
    with pytest.raises(db.DatabaseFeil) as e:
        pg.execute("SELECT * FROM x")
    assert not isinstance(e.value, db.IntegritetsFeil)


def test_feil_uten_diag_gir_klassenavn():
    class UtenDiag(FalskPgFeil):
        pass
    feil = UtenDiag("x")
    del feil.diag
    pg, _ = _pg(lambda sql, p: feil)
    with pytest.raises(db.PostgresFeil) as e:
        pg.execute("SELECT 1")
    assert str(e.value) == "UtenDiag"


def test_executemany_executescript_commit_rollback_close():
    pg, raa = _pg()
    pg.executemany("INSERT INTO t (a,b) VALUES (?,?)", [(1, True), (2, False)])
    assert raa.markor.kall == [("INSERT INTO t (a,b) VALUES (%s,%s)", [[1, 1], [2, 0]])]
    pg.executescript("CREATE TABLE a (x TEXT); CREATE TABLE b (y TEXT);")
    assert raa.kall[-1] == ("CREATE TABLE a (x TEXT); CREATE TABLE b (y TEXT);", None)
    pg.commit()
    pg.rollback()
    pg.close()
    assert (raa.committet, raa.rullet_tilbake, raa.lukket) == (1, 1, 1)


def test_transaksjon_fungerer_uendret_med_adapteren():
    pg, raa = _pg()
    with db.transaksjon(pg):
        pg.execute("UPDATE t SET a=?", (1,))
    assert (raa.committet, raa.rullet_tilbake) == (1, 0)
    with pytest.raises(ValueError):
        with db.transaksjon(pg):
            raise ValueError("feil")
    assert (raa.committet, raa.rullet_tilbake) == (1, 1)


# ============================ backend-spesifikke hjelpere ============================

def test_sql_tekstliste_per_backend(tmp_path):
    lite = db.koble(tmp_path / "x.db")
    pg, _ = _pg()
    assert db.sql_tekstliste(lite, "faktura_nr") == "GROUP_CONCAT(faktura_nr, ' ')"
    assert db.sql_tekstliste(pg, "faktura_nr") == "string_agg(faktura_nr, ' ')"
    assert db.er_postgres(pg) and not db.er_postgres(lite)
    lite.close()


def test_sql_tekstliste_gir_samme_resultat_i_sqlite(tmp_path):
    c = db.koble(tmp_path / "x.db")
    c.execute("CREATE TABLE f (n TEXT)")
    c.executemany("INSERT INTO f VALUES (?)", [("A1",), ("B2",)])
    verdi = c.execute(f"SELECT {db.sql_tekstliste(c, 'n')} FROM f").fetchone()[0]
    assert sorted(verdi.split(" ")) == ["A1", "B2"]
    c.close()


def test_har_tabell_og_kolonne_bruker_information_schema_i_postgres():
    pg, raa = _pg(lambda sql, p: FalskMarkor([_rad(x=1)], description=[("x",)]))
    assert db.har_tabell(pg, "kurs") and db.har_kolonne(pg, "kurs", "navn")
    assert all("information_schema" in sql and "current_schema()" in sql and "%s" in sql for sql, _ in raa.kall)
    assert [p for _, p in raa.kall] == [["kurs"], ["kurs", "navn"]]


def test_init_paa_postgres_bruker_postgres_skjemaet():
    pg, raa = _pg(lambda sql, p: FalskMarkor([_rad(x=1)], description=[("x",)]))   # alt «finnes» allerede
    db.init(pg)
    assert raa.kall[0][0] == (KURS_MAPPE / "schema_postgres.sql").read_text(encoding="utf-8")
    assert not any("ALTER TABLE" in sql for sql, _ in raa.kall)                      # ingen manglende kolonner
    assert not any(sql.startswith("INSERT INTO admin_bruker") for sql, _ in raa.kall)
    assert raa.committet == 1


# ============================ backend-valg ============================

def test_uten_database_url_brukes_sqlite(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "DB_STI", tmp_path / "lokal.db")
    c = db.koble()
    assert isinstance(c, sqlite3.Connection) and (tmp_path / "lokal.db").exists()
    c.close()


def test_med_database_url_brukes_postgres(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://u:p@vert/db")
    brukt = []
    monkeypatch.setattr(db, "_koble_postgres", lambda url: brukt.append(url) or "PG-TILKOBLING")
    assert db.koble() == "PG-TILKOBLING" and brukt == ["postgresql://u:p@vert/db"]


def test_eksplisitt_sti_er_alltid_sqlite(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://u:p@vert/db")
    monkeypatch.setattr(db, "_koble_postgres", lambda url: pytest.fail("skal ikke kobles til PostgreSQL"))
    c = db.koble(tmp_path / "test.db")
    assert isinstance(c, sqlite3.Connection)
    c.close()


def test_testene_bruker_aldri_database_url():
    assert config.DATABASE_URL == ""               # satt av autouse-fiksturen i conftest.py


def test_manglende_psycopg_gir_tydelig_feil(monkeypatch):
    import builtins
    ekte_import = builtins.__import__

    def uten_psycopg(navn, *a, **kw):
        if navn == "psycopg":
            raise ImportError("ingen psycopg")
        return ekte_import(navn, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", uten_psycopg)
    with pytest.raises(RuntimeError) as e:
        db._koble_postgres("postgresql://u:p@vert/db")
    assert "psycopg er ikke installert" in str(e.value) and "u:p" not in str(e.value)


# ============================ kurs.migrer ============================

@pytest.fixture
def sqlite_migrering(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_STI", tmp_path / "migrer.db")
    monkeypatch.setattr(config, "DEMO", True)
    return tmp_path / "migrer.db"


def _tabeller(sti):
    c = sqlite3.connect(sti)
    t = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    c.close()
    return t


def test_migrer_oppretter_skjema_og_er_idempotent(sqlite_migrering, capsys):
    assert migrer.kjor() == 0
    assert migrer.kjor() == 0
    assert {"kurs", "paamelding", "kurs_skjemafelt", "admin_bruker"} <= _tabeller(sqlite_migrering)
    c = sqlite3.connect(sqlite_migrering)
    assert c.execute("SELECT COUNT(*) FROM admin_bruker").fetchone()[0] == 1
    c.close()
    assert "Migrering fullfoert." in capsys.readouterr().out


@pytest.mark.parametrize("passord", ["", "demo", "elleve-tegn"])     # siste: 11 tegn, grensen er 12
def test_migrer_nekter_svakt_foerste_adminpassord_i_prod(sqlite_migrering, monkeypatch, capsys, passord):
    monkeypatch.setattr(config, "DEMO", False)
    monkeypatch.setattr(config, "ADMIN_PASSORD", passord)
    assert migrer.kjor() == 2
    assert "admin_bruker" not in _tabeller(sqlite_migrering)
    assert "STOPP" in capsys.readouterr().out


def test_migrer_godtar_sterkt_passord_i_prod(sqlite_migrering, monkeypatch):
    monkeypatch.setattr(config, "DEMO", False)
    monkeypatch.setattr(config, "ADMIN_PASSORD", "et-langt-og-sterkt-passord")
    assert migrer.kjor() == 0


def test_migrer_i_prod_med_eksisterende_admin_krever_ikke_passord(sqlite_migrering, monkeypatch):
    assert migrer.kjor() == 0                                    # forste gang i demo: admin finnes
    monkeypatch.setattr(config, "DEMO", False)
    monkeypatch.setattr(config, "ADMIN_PASSORD", "demo")
    assert migrer.kjor() == 0


def test_migrer_skriver_aldri_ut_tilkoblingsstrengen(monkeypatch, capsys):
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://bruker:HEMMELIG-PASSORD@vert/db")
    monkeypatch.setattr(config, "DEMO", True)
    pg, _ = _pg(lambda sql, p: FalskMarkor([_rad(x=1)], description=[("x",)]))
    monkeypatch.setattr(db, "_koble_postgres", lambda url: pg)
    assert migrer.kjor() == 0
    ut = capsys.readouterr().out
    assert "PostgreSQL" in ut and "HEMMELIG" not in ut and "bruker:" not in ut


def test_gunicorn_oppstart_migrerer_ikke():
    tekst = (KURS_MAPPE.parent / "startup.py").read_text(encoding="utf-8")
    assert "init" not in tekst and "migrer" not in tekst


# ============================ vakt: SQLite-dialekt kun i db.py ============================

@pytest.mark.parametrize("fil", sorted(p for p in KURS_MAPPE.rglob("*.py") if p.name != "db.py"), ids=lambda p: p.name)
def test_ingen_sqlite_dialekt_utenfor_db_py(fil):
    tekst = fil.read_text(encoding="utf-8")
    funn = [m for m in (r"GROUP_CONCAT", r"\bPRAGMA\b", r"sqlite_master") if re.search(m, tekst)]
    assert funn == [], f"{fil.name}: {funn}"
