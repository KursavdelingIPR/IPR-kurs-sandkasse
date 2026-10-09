"""Databasetilgang. SQLite (standard: én fil, ingen server) eller PostgreSQL naar DATABASE_URL er satt.

Backend-valg (se koble()):
  * DATABASE_URL tom (lokal Windows-sandkasse, tester): SQLite i config.DB_STI - noyaktig som foer.
  * DATABASE_URL satt (drift, f.eks. Azure Database for PostgreSQL): PostgreSQL via psycopg, gjennom et tynt adapterlag
    (_PgTilkobling) som gir resten av koden SAMME grensesnitt som sqlite3: con.execute("... ? ...", params),
    rad["navn"] og rad[0], cursor.rowcount/fetchone/fetchall, commit/rollback, og db.DatabaseFeil/IntegritetsFeil.
psycopg importeres KUN naar PostgreSQL faktisk brukes.
"""
import base64
import hashlib
import json
import re
import secrets
import sqlite3
import string
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

from . import config, ekstrafelt, kursdatoer, skjemafelt

_SCHEMA = Path(__file__).with_name("schema.sql")
_SCHEMA_POSTGRES = Path(__file__).with_name("schema_postgres.sql")


# ---------- felles databaseunntak (fase 1 + 2) ----------
# Resten av appen fanger KUN db.DatabaseFeil / db.IntegritetsFeil - aldri sqlite3.* eller psycopg.* direkte. De er
# ENKLE KLASSER (ikke tupler), saa de kan brukes fritt ogsaa inne i andre `except (A, B, ...)`-tupler. sqlite3 sine
# unntaksklasser er felles basisklasser: adapterens oversatte PostgreSQL-unntak ARVER fra dem, slik at samme
# `except` fanger feil fra begge databaser.

class PostgresFeil(sqlite3.Error):
    """Feil fra PostgreSQL, oversatt av adapteren. Meldingen er PostgreSQL sin korte hovedmelding (uten DETAIL, som kan
    inneholde verdier/persondata). Det opprinnelige psycopg-unntaket ligger i __cause__."""


class PostgresIntegritetsFeil(PostgresFeil, sqlite3.IntegrityError):
    """Brudd paa UNIQUE/NOT NULL/CHECK/FOREIGN KEY i PostgreSQL."""


DatabaseFeil = sqlite3.Error
IntegritetsFeil = sqlite3.IntegrityError

_SQLITE_TIDSFORMAT = "%Y-%m-%d %H:%M:%S"


def naa_utc() -> str:
    """Naa i SAMME format og tidssone som SQLite sin datetime('now') ('YYYY-MM-DD HH:MM:SS', UTC). Brukes der SQL-en
    tidligere kalte datetime('now'), saa lagrede og sammenlignede verdier er uendret - uten en SQLite-funksjon."""
    return datetime.now(timezone.utc).strftime(_SQLITE_TIDSFORMAT)


def utc_minutter_siden(minutter: int) -> str:
    """Som SQLite sin datetime('now', '-N minutes'): samme format og tidssone som naa_utc()."""
    return (datetime.now(timezone.utc) - timedelta(minutes=minutter)).strftime(_SQLITE_TIDSFORMAT)


def sett_inn(con, sql: str, params=()) -> int:
    """Kjoerer en INSERT og returnerer den nye radens id med `RETURNING id` (SQLite >= 3.35 og PostgreSQL) - i stedet
    for cursor.lastrowid, som PostgreSQL ikke har. fetchall() fullfoerer setningen, saa en senere commit aldri
    stoppes av en uferdig setning."""
    return con.execute(f"{sql} RETURNING id", params).fetchall()[0]["id"]


# Tidligere «INSERT OR REPLACE». Standard upsert (SQLite >= 3.24 og PostgreSQL) med samme resultat: raden for
# paameldingen faar de nye verdiene. (sensitivt har ingen avhengige rader, saa REPLACE sin slett+sett-inn ga ingen
# annen effekt enn en oppdatering.)
_SENSITIVT_UPSERT = """INSERT INTO sensitivt (paamelding_id, allergier, tilrettelegging) VALUES (?,?,?)
                       ON CONFLICT (paamelding_id) DO UPDATE SET allergier=excluded.allergier,
                                                                 tilrettelegging=excluded.tilrettelegging"""
_KODE_TEGN = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # uten 0/O/1/I – lett aa taste
_TRANSLIT_NORSK = str.maketrans({"æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O", "å": "aa", "Å": "AA"})


# ---------- PostgreSQL-adapter (fase 2) ----------

class Rad:
    """Rad fra PostgreSQL med samme bruk som sqlite3.Row: rad["navn"] (uten hensyn til store/smaa bokstaver, som
    sqlite3.Row), rad[0], keys(), dict(rad), len(rad) og iterasjon over verdiene. Ukjent nokkel -> IndexError (som
    sqlite3.Row)."""
    __slots__ = ("_navn", "_indeks", "_verdier")

    def __init__(self, navn: list, indeks: dict, verdier):
        self._navn, self._indeks, self._verdier = navn, indeks, tuple(verdier)

    def keys(self) -> list:
        return list(self._navn)

    def __getitem__(self, nokkel):
        if isinstance(nokkel, (int, slice)):
            return self._verdier[nokkel]
        try:
            return self._verdier[self._indeks[nokkel.lower()]]
        except (KeyError, AttributeError):
            raise IndexError("No item with that key") from None

    def __iter__(self):
        return iter(self._verdier)

    def __len__(self) -> int:
        return len(self._verdier)

    def __eq__(self, annen) -> bool:
        return isinstance(annen, Rad) and self._navn == annen._navn and self._verdier == annen._verdier

    __hash__ = None

    def __repr__(self) -> str:
        return f"Rad({dict(zip(self._navn, self._verdier))!r})"


def _rad_fabrikk(cursor):
    """psycopg row_factory: bygger Rad-objekter med kolonnenavnene fra resultatet."""
    navn = [k.name for k in (cursor.description or [])]
    indeks = {n.lower(): i for i, n in enumerate(navn)}
    return lambda verdier: Rad(navn, indeks, verdier)


def _til_pg_sql(sql: str) -> str:
    """sqlite3-plassholder '?' -> psycopg '%s' (kun UTENFOR strenglitteraler/siterte navn), og hver '%' -> '%%' (psycopg
    tolker % naar parametere sendes med, ogsaa inne i f.eks. LIKE '%bergen%'). Brukes kun naar det finnes parametere."""
    ut, sitat = [], None
    for tegn in sql:
        if sitat:
            if tegn == sitat:
                sitat = None
            ut.append("%%" if tegn == "%" else tegn)
        elif tegn in ("'", '"'):
            sitat = tegn
            ut.append(tegn)
        elif tegn == "?":
            ut.append("%s")
        elif tegn == "%":
            ut.append("%%")
        else:
            ut.append(tegn)
    return "".join(ut)


def _pg_verdi(verdi):
    """Samme parameterverdier som sqlite3 lagrer: bool -> 0/1 (kolonnene er heltall), date -> 'YYYY-MM-DD' og
    datetime -> 'YYYY-MM-DD HH:MM:SS[.ffffff]' (sqlite3 sin standardadapter; tidsstempler lagres som tekst)."""
    if isinstance(verdi, bool):
        return int(verdi)
    if isinstance(verdi, datetime):
        return verdi.isoformat(" ")
    if isinstance(verdi, date):
        return verdi.isoformat()
    return verdi


class _PgMarkor:
    """Cursor med sqlite3-oppforsel: fetchone() -> None og fetchall() -> [] naar setningen ikke ga noe resultat."""

    def __init__(self, cur):
        self._cur = cur

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def description(self):
        return self._cur.description

    def fetchone(self):
        return self._cur.fetchone() if self._cur.description is not None else None

    def fetchall(self) -> list:
        return self._cur.fetchall() if self._cur.description is not None else []

    def __iter__(self):
        return iter(self.fetchall())


_ENDRINGSSTATUS = ("INSERT", "UPDATE", "DELETE")
_SKRIVESTATUS = _ENDRINGSSTATUS + ("CREATE", "DROP", "ALTER", "TRUNCATE", "COMMENT", "GRANT", "REVOKE")


class _PgTilkobling:
    """Tynt adapterlag rundt en psycopg-tilkobling. Transaksjoner fungerer som med sqlite3: forste setning starter en
    transaksjon, commit()/rollback() avslutter den (db.transaksjon er uendret). psycopg-feil oversettes til
    PostgresFeil/PostgresIntegritetsFeil (del av db.DatabaseFeil/IntegritetsFeil).

    Speiler ogsaa de sqlite3-egenskapene koden og testene bruker for aa kontrollere transaksjonsdisiplin:
      * `in_transaction`: som sqlite3 i Pythons standardmodus, der en ren LESING aldri aapner en transaksjon. I PostgreSQL
        aapner ogsaa en SELECT en (lese)transaksjon uten skrivelaaser; den teller derfor IKKE. True betyr: en transaksjon
        med skriving (INSERT/UPDATE/DELETE/DDL) staar aapen, eller transaksjonen er avbrutt av en feil og maa rulles tilbake.
      * `total_changes`: antall rader endret av INSERT/UPDATE/DELETE via denne tilkoblingen.
      * `set_trace_callback`: kalles med hver SQL-setning som kjoeres."""

    def __init__(self, raa, psycopg_modul):
        self._raa, self._pg = raa, psycopg_modul
        self._endringer = 0
        self._har_skrevet = False
        self._sporing = None

    @property
    def avbrutt(self) -> bool:
        """True naar PostgreSQL har avbrutt transaksjonen etter en feil (alle videre setninger feiler til rollback)."""
        status = getattr(getattr(self._raa, "info", None), "transaction_status", None)
        return getattr(status, "name", "") == "INERROR"

    def _oversett(self, feil: Exception) -> Exception:
        diag = getattr(feil, "diag", None)
        melding = (getattr(diag, "message_primary", None) or type(feil).__name__) if diag else type(feil).__name__
        klasse = PostgresIntegritetsFeil if isinstance(feil, self._pg.IntegrityError) else PostgresFeil
        return klasse(melding)

    @property
    def in_transaction(self) -> bool:
        status = getattr(getattr(self._raa, "info", None), "transaction_status", None)
        navn = getattr(status, "name", "")
        return navn == "INERROR" or (navn == "INTRANS" and self._har_skrevet)

    @property
    def total_changes(self) -> int:
        return self._endringer

    def set_trace_callback(self, callback) -> None:
        self._sporing = callback

    def _tell_endringer(self, cur) -> None:
        status = (getattr(cur, "statusmessage", None) or "").split(" ", 1)[0].upper()
        if status in _SKRIVESTATUS:
            self._har_skrevet = True
        if status in _ENDRINGSSTATUS and cur.rowcount and cur.rowcount > 0:
            self._endringer += cur.rowcount

    def execute(self, sql: str, params=()):
        if self._sporing:
            self._sporing(sql)
        try:
            if params:
                cur = self._raa.execute(_til_pg_sql(sql), [_pg_verdi(v) for v in params])
            else:
                cur = self._raa.execute(sql)
        except self._pg.Error as e:
            raise self._oversett(e) from e
        self._tell_endringer(cur)
        return _PgMarkor(cur)

    def executemany(self, sql: str, sekvens) -> None:
        if self._sporing:
            self._sporing(sql)
        try:
            with self._raa.cursor() as cur:
                cur.executemany(_til_pg_sql(sql), [[_pg_verdi(v) for v in p] for p in sekvens])
                self._tell_endringer(cur)
        except self._pg.Error as e:
            raise self._oversett(e) from e

    def executescript(self, sql: str) -> None:
        """Flere setninger uten parametere (brukes kun av init() med schema_postgres.sql)."""
        try:
            self._raa.execute(sql)
        except self._pg.Error as e:
            raise self._oversett(e) from e

    def commit(self) -> None:
        try:
            self._raa.commit()
        except self._pg.Error as e:
            raise self._oversett(e) from e
        finally:
            self._har_skrevet = False

    def rollback(self) -> None:
        self._har_skrevet = False
        self._raa.rollback()

    def close(self) -> None:
        self._raa.close()


def er_postgres(con) -> bool:
    return isinstance(con, _PgTilkobling)


def _koble_postgres(url: str) -> _PgTilkobling:
    try:
        import psycopg
    except ImportError as e:
        raise RuntimeError("DATABASE_URL er satt, men psycopg er ikke installert (pip install -r requirements.txt).") from e
    return _PgTilkobling(psycopg.connect(url, row_factory=_rad_fabrikk), psycopg)


def koble(sti: Path | None = None):
    """SQLite (standard): `sti`, eller config.DB_STI. PostgreSQL: KUN naar ingen `sti` er gitt og config.DATABASE_URL er
    satt. En eksplisitt `sti` betyr alltid SQLite - derfor bruker testene (som gir egen sti) aldri PostgreSQL."""
    if sti is None and config.DATABASE_URL:
        return _koble_postgres(config.DATABASE_URL)
    sti = Path(sti or config.DB_STI)
    sti.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(sti)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init(con) -> list[int]:
    """Oppretter manglende tabeller/indekser, legger til manglende kolonner (eldre databaser), kjoerer manglende
    versjonerte migreringer (kurs/migreringer.py) og den forste admin-brukeren. Idempotent. I drift kjoeres dette av
    `python -m kurs.migrer` - aldri av gunicorn. Returnerer numrene paa migreringene som ble kjoert."""
    from . import migreringer   # importeres her: migreringer bruker db
    con.executescript((_SCHEMA_POSTGRES if er_postgres(con) else _SCHEMA).read_text(encoding="utf-8"))
    _migrer(con)
    con.commit()
    kjort = migreringer.kjor_manglende(con)
    if not con.execute("SELECT 1 FROM admin_bruker").fetchone():
        opprett_admin_bruker(con, config.ADMIN_BRUKERNAVN, "Standardbruker", config.ADMIN_PASSORD, rolle="system")
    con.commit()
    return kjort


def neste_teller(con, navn: str) -> int:
    """Neste verdi fra en teller som aldri gaar tilbake (UPDATE ... RETURNING: atomisk i SQLite og PostgreSQL - to
    samtidige kall kan aldri faa samme verdi). Telleren opprettes av migreringene; mangler den, er databasen ikke migrert."""
    rad = con.execute("UPDATE teller SET verdi = verdi + 1 WHERE navn=? RETURNING verdi", (navn,)).fetchone()
    if rad is None:
        raise DatabaseFeil(f"Telleren «{navn}» finnes ikke - kjoer migreringen (python -m kurs.migrer).")
    return int(rad["verdi"])


def hent_integrasjonstoken(con, navn: str) -> str | None:
    rad = con.execute("SELECT verdi FROM integrasjon_token WHERE navn=?", (navn,)).fetchone()
    return rad["verdi"] if rad else None


def lagre_integrasjonstoken(con, navn: str, verdi: str) -> None:
    """Lagrer (eller erstatter) et token. Kalleren committer. Verdien skal aldri logges."""
    con.execute("""INSERT INTO integrasjon_token (navn, verdi, oppdatert) VALUES (?,?,?)
                   ON CONFLICT (navn) DO UPDATE SET verdi=excluded.verdi, oppdatert=excluded.oppdatert""",
                (navn, verdi, naa_utc()))


def har_tabell(con, tabell: str) -> bool:
    if er_postgres(con):
        sql = ("SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() "
               "AND table_name = ?")
    else:
        sql = "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?"
    return con.execute(sql, (tabell,)).fetchone() is not None


def har_kolonne(con, tabell: str, kolonne: str) -> bool:
    if er_postgres(con):
        return con.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = ? AND column_name = ?", (tabell, kolonne)).fetchone() is not None
    return any(r["name"] == kolonne for r in con.execute(f"PRAGMA table_info({tabell})"))


def kolonner(con, tabell: str) -> list[str]:
    """Kolonnenavnene i tabellen, i tabellens rekkefoelge (tom liste hvis tabellen ikke finnes). `tabell` er et fast navn
    fra koden - aldri brukerinput."""
    if er_postgres(con):
        return [r["column_name"] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = ? ORDER BY ordinal_position", (tabell,))]
    return [r["name"] for r in con.execute(f"PRAGMA table_info({tabell})")]


def har_admin_bruker(con) -> bool:
    return har_tabell(con, "admin_bruker") and con.execute("SELECT 1 FROM admin_bruker").fetchone() is not None


def sql_tekstliste(con, uttrykk: str, skille: str = " ") -> str:
    """SQL-aggregat som slaar sammen tekstverdier: GROUP_CONCAT (SQLite) eller string_agg (PostgreSQL). `uttrykk` og
    `skille` er faste verdier fra koden - aldri brukerinput."""
    skille_sql = "'" + skille.replace("'", "''") + "'"
    return f"string_agg({uttrykk}, {skille_sql})" if er_postgres(con) else f"GROUP_CONCAT({uttrykk}, {skille_sql})"


def _migrer(con) -> None:
    """Legger til kolonner fra nyere versjoner av schema.sql i eksisterende databaser, uten aa miste data.

    SQLite tillater ikke ALTER TABLE ... ADD COLUMN med en ikke-konstant DEFAULT (som datetime('now'))
    naar tabellen har rader fra for. Vi legger derfor til kolonnen uten default og etterfyller (backfill)
    med en vanlig UPDATE i stedet. (Samme ALTER TABLE-syntaks fungerer i PostgreSQL; schema_postgres.sql har
    allerede alle kolonner, saa der er dette normalt ingen endring.)
    """
    if not har_kolonne(con, "kurs", "ansvarlig_admin_id"):
        con.execute("ALTER TABLE kurs ADD COLUMN ansvarlig_admin_id INTEGER REFERENCES admin_bruker(id)")
    if not har_kolonne(con, "deltaker", "yrkestittel"):
        con.execute("ALTER TABLE deltaker ADD COLUMN yrkestittel TEXT")
    if not har_kolonne(con, "paamelding", "oppdatert"):
        con.execute("ALTER TABLE paamelding ADD COLUMN oppdatert TEXT")
        con.execute("UPDATE paamelding SET oppdatert = opprettet WHERE oppdatert IS NULL")
    if not har_kolonne(con, "paamelding", "faktura_kommentar"):
        con.execute("ALTER TABLE paamelding ADD COLUMN faktura_kommentar TEXT")
    if not har_kolonne(con, "paamelding", "intern_kommentar"):
        con.execute("ALTER TABLE paamelding ADD COLUMN intern_kommentar TEXT")
    if not har_kolonne(con, "kurs", "paameldingsfrist"):
        con.execute("ALTER TABLE kurs ADD COLUMN paameldingsfrist TEXT")
    # Fase 12C5: paameldingssidens tekster. Kun additivt - NULL betyr standard (ingen intro / «Meld meg på»).
    if not har_kolonne(con, "kurs", "paamelding_intro"):
        con.execute("ALTER TABLE kurs ADD COLUMN paamelding_intro TEXT")
    if not har_kolonne(con, "kurs", "paamelding_knappetekst"):
        con.execute("ALTER TABLE kurs ADD COLUMN paamelding_knappetekst TEXT")
    if not har_kolonne(con, "paamelding", "sveiper_utsatt"):
        # Konstant default (0) - trygt aa legge til selv om tabellen har rader fra for.
        con.execute("ALTER TABLE paamelding ADD COLUMN sveiper_utsatt INTEGER NOT NULL DEFAULT 0")
    # Faktura tidligst seks maaneder foer forste kursdag: faktura_onskes_na (lagret valg) og faktura_tidligst_dato (planlagt
    # samlet faktura - satt av sveiper.fakturer, lest av sveiper.utsatte_fakturaer og oekonomilaasen). Bare manglende
    # kolonner legges til - eksisterende verdier roeres aldri (ingen UPDATE).
    if not har_kolonne(con, "paamelding", "faktura_onskes_na"):
        con.execute("ALTER TABLE paamelding ADD COLUMN faktura_onskes_na INTEGER NOT NULL DEFAULT 0")
    if not har_kolonne(con, "paamelding", "faktura_tidligst_dato"):
        con.execute("ALTER TABLE paamelding ADD COLUMN faktura_tidligst_dato TEXT")
    if har_tabell(con, "utsending_logg") and not har_kolonne(con, "utsending_logg", "status"):
        # Konstant default ('sendt') - alle eksisterende rader ER faktisk sendt (skrevet av den
        # gamle, ubetingede marker_sendt()), saa dette endrer ikke betydningen av noen rad fra for.
        con.execute(
            "ALTER TABLE utsending_logg ADD COLUMN status TEXT NOT NULL DEFAULT 'sendt' "
            "CHECK (status IN ('reservert','sendt','feilet','ukjent'))")


@contextmanager
def transaksjon(con: sqlite3.Connection):
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise


@contextmanager
def isolert(con, navn: str = "isolert"):
    """Kjoerer blokken i en SAVEPOINT. Feiler blokken, rulles KUN den tilbake og tilkoblingen kan brukes videre.

    Noedvendig for PostgreSQL: der avbryter en feilet setning hele transaksjonen, og ALLE videre setninger feiler til den
    rulles tilbake (SQLite fortsetter). Brukes rundt lesinger der en databasefeil oversettes til en kontrollert feil
    (MalFeil/SkjemaLesefeil) og kalleren deretter fortsetter - f.eks. logger en hendelse eller viser en side. Committer
    aldri selv. `navn` er et fast navn fra koden (aldri brukerinput)."""
    con.execute(f"SAVEPOINT {navn}")
    try:
        yield con
    except BaseException:
        try:
            con.execute(f"ROLLBACK TO SAVEPOINT {navn}")
            con.execute(f"RELEASE SAVEPOINT {navn}")
        except DatabaseFeil:
            pass    # tilkoblingen er ubrukelig (f.eks. lukket) - den opprinnelige feilen sendes videre uansett
        raise
    con.execute(f"RELEASE SAVEPOINT {navn}")


def begynn_transaksjon(con) -> None:
    """SQLite: starter en transaksjon hvis ingen er aktiv, slik at en SAVEPOINT (isolert) aldri blir den ytterste - da
    ville RELEASE ha committet (også i en tørrkjøring som skal rulles tilbake). PostgreSQL er alltid i en transaksjon."""
    if not er_postgres(con) and not con.in_transaction:
        con.execute("BEGIN")


def rull_tilbake_hvis_avbrutt(con) -> bool:
    """Etter en fanget feil: har PostgreSQL avbrutt transaksjonen, rulles den tilbake slik at tilkoblingen kan brukes
    videre (f.eks. til aa logge feilen og fortsette med neste rad). Ikke-committet arbeid i den avbrutte transaksjonen er
    uansett tapt. SQLite avbryter ikke transaksjoner slik - da gjoeres ingenting. True = rullet tilbake."""
    if er_postgres(con) and con.avbrutt:
        con.rollback()
        return True
    return False


def logg(con: sqlite3.Connection, handling: str, detaljer: dict | str | None = None, aktor: str = "system") -> None:
    if isinstance(detaljer, dict):
        detaljer = json.dumps(detaljer, ensure_ascii=False)
    con.execute("INSERT INTO hendelse (aktor, handling, detaljer) VALUES (?,?,?)", (aktor, handling, detaljer))


def ny_kode(lengde: int = 6) -> str:
    return "".join(secrets.choice(_KODE_TEGN) for _ in range(lengde))


# ---------- admin-brukere ----------

ROLLER = ("system", "kursadmin", "lese")
ROLLE_NAVN = {"system": "Systemadministrator", "kursadmin": "Kursadministrator", "lese": "Lesetilgang"}


class RolleFeil(Exception):
    pass


def opprett_admin_bruker(con, brukernavn: str, navn: str, passord: str, aktor: str = "system",
                         rolle: str = "kursadmin", entra_oid: str | None = None, epost: str | None = None) -> int:
    if rolle not in ROLLER:
        raise RolleFeil("Ugyldig rolle.")
    brukernavn = brukernavn.strip().lower()
    admin_id = sett_inn(
        con, "INSERT INTO admin_bruker (brukernavn, navn, passord_hash, rolle, entra_oid, epost) VALUES (?,?,?,?,?,?)",
        (brukernavn, navn.strip(), generate_password_hash(passord), rolle, entra_oid, epost),
    )
    logg(con, "admin_bruker_opprettet", {"brukernavn": brukernavn, "rolle": rolle, "entra": entra_oid is not None},
         aktor=aktor)
    return admin_id


def antall_aktive_systemadmin(con) -> int:
    return con.execute("SELECT COUNT(*) FROM admin_bruker WHERE aktiv=1 AND rolle='system'").fetchone()[0]


def sett_admin_rolle(con, admin_id: int, rolle: str, aktor: str = "system") -> bool:
    """Endrer rolle. Den siste aktive systemadministratoren kan aldri fratas rollen (da ville ingen kunne styre
    brukere). True = endret."""
    if rolle not in ROLLER:
        raise RolleFeil("Ugyldig rolle.")
    rad = con.execute("SELECT * FROM admin_bruker WHERE id=?", (admin_id,)).fetchone()
    if not rad:
        raise RolleFeil("Ukjent bruker.")
    if rad["rolle"] == rolle:
        return False
    if rad["rolle"] == "system" and rad["aktiv"] and antall_aktive_systemadmin(con) <= 1:
        raise RolleFeil("Den siste aktive systemadministratoren kan ikke fratas rollen.")
    con.execute("UPDATE admin_bruker SET rolle=? WHERE id=?", (rolle, admin_id))
    logg(con, "admin_rolle_endret", {"id": admin_id, "fra": rad["rolle"], "til": rolle}, aktor=aktor)
    return True


def finn_eller_opprett_entra_bruker(con, oid: str, navn: str, epost: str | None, rolle: str, aktor: str = "entra"):
    """Innlogging via Microsoft Entra ID. Finner brukeren paa entra_oid; finnes den ikke, knyttes en eksisterende LOKAL
    bruker med samme brukernavn (= e-post) til Entra, ellers opprettes en ny. Navn, e-post og ROLLE oppdateres fra Entra
    ved hver innlogging (Entra er fasit for rollen). En deaktivert bruker slipper ikke inn - deaktivering i appen vinner.
    Returnerer raden, eller None hvis brukeren er deaktivert."""
    if rolle not in ROLLER:
        raise RolleFeil("Ugyldig rolle.")
    navn = (navn or epost or oid).strip()
    epost = (epost or "").strip().lower() or None
    rad = con.execute("SELECT * FROM admin_bruker WHERE entra_oid=?", (oid,)).fetchone()
    if not rad and epost:
        lokal = con.execute("SELECT * FROM admin_bruker WHERE brukernavn=? AND entra_oid IS NULL", (epost,)).fetchone()
        if lokal:
            con.execute("UPDATE admin_bruker SET entra_oid=? WHERE id=?", (oid, lokal["id"]))
            logg(con, "admin_bruker_knyttet_entra", {"id": lokal["id"]}, aktor=aktor)
            rad = con.execute("SELECT * FROM admin_bruker WHERE id=?", (lokal["id"],)).fetchone()
    if not rad:
        brukernavn = epost or f"entra:{oid}"
        # Entra-brukere har ikke lokalt passord: et tilfeldig, ukjent passord gjoer lokal innlogging umulig.
        admin_id = opprett_admin_bruker(con, brukernavn, navn, secrets.token_urlsafe(32), aktor=aktor, rolle=rolle,
                                        entra_oid=oid, epost=epost)
        return con.execute("SELECT * FROM admin_bruker WHERE id=?", (admin_id,)).fetchone()
    if not rad["aktiv"]:
        return None
    endringer = {k: v for k, v in (("navn", navn), ("epost", epost), ("rolle", rolle)) if rad[k] != v}
    if endringer:
        if "rolle" in endringer:
            try:
                sett_admin_rolle(con, rad["id"], rolle, aktor=aktor)
            except RolleFeil:
                pass    # siste systemadmin beholder rollen lokalt selv om Entra sier noe annet
            endringer.pop("rolle")
        if endringer:
            con.execute(f"UPDATE admin_bruker SET {','.join(f'{k}=?' for k in endringer)} WHERE id=?",
                        [*endringer.values(), rad["id"]])
    return con.execute("SELECT * FROM admin_bruker WHERE id=?", (rad["id"],)).fetchone()


def verifiser_admin(con, brukernavn: str, passord: str) -> sqlite3.Row | None:
    rad = con.execute(
        "SELECT * FROM admin_bruker WHERE brukernavn=? AND aktiv=1", (brukernavn.strip().lower(),)
    ).fetchone()
    if rad and check_password_hash(rad["passord_hash"], passord):
        return rad
    return None


# ---------- kurs ----------

def generer_kode(con, navn: str, datoer: list[str]) -> str:
    """Lager en unik kurskode automatisk, ut fra forste ord i kursnavnet og aarstallet til forste kursdag."""
    forste_ord = re.split(r"[\s\-–—(),.:;/]+", navn.strip())[0] if navn.strip() else ""
    bokstaver = "".join(c for c in forste_ord.translate(_TRANSLIT_NORSK) if c.isascii() and c.isalnum())
    prefiks = (bokstaver[:4] or "KURS").upper()
    aar = date.fromisoformat(datoer[0]).year if datoer else date.today().year
    basis = f"{prefiks}-{aar}"
    kode, n = basis, 2
    while con.execute("SELECT 1 FROM kurs WHERE kode=?", (kode,)).fetchone():
        kode = f"{basis}-{n}"
        n += 1
    return kode


FAKTURA_DAGER_FOR_MIN, FAKTURA_DAGER_FOR_MAKS = 0, 180


def valider_faktura_dager_for(verdi) -> int:
    """Server-side fasit for kurs.faktura_dager_for (antall DAGER foer hver samling en delfaktura lages - IKKE seks
    kalendermaaneder): et helt tall 0..180, grensene inklusive. HTML min/max er bare brukerhjelp. Tar imot int (aldri bool/
    float/tekst - parsing av skjemaverdier er ikke gjort mer liberal her) og returnerer det. Ugyldig -> Paameldingsfeil."""
    if isinstance(verdi, bool) or not isinstance(verdi, int):
        raise Paameldingsfeil("«Dager før faktura» må være et helt tall.")
    if not FAKTURA_DAGER_FOR_MIN <= verdi <= FAKTURA_DAGER_FOR_MAKS:
        raise Paameldingsfeil(f"«Dager før faktura» må være mellom {FAKTURA_DAGER_FOR_MIN} og {FAKTURA_DAGER_FOR_MAKS}.")
    return verdi


def opprett_kurs(con, *, kode: str, navn: str, datoer: list[str], aktor: str = "system",
                 samlinger: "list[kursdatoer.Samling] | None" = None, idag: date | None = None, **felter) -> int:
    """Oppretter et kurs med et NYTT, permanent kursnummer (kurs.kursnr) fra telleren - kalleren kan aldri velge nummeret.
    `kode` er nettadresse-delen i offentlige lenker.

    Kursdagene kommer enten som `samlinger` (skjemaet «Opprett kurs», se lagre_samlinger) eller som den gamle datolisten
    `datoer`: da blir sammenhengende datoer én samling, og dagene arver kursets klokkeslett som før. Kan kurset ha delt
    faktura (betaling per_samling eller deltaker_velger), blir hver dato sin egen samling, slik at delfakturaen fortsatt
    er per kursdag for den gamle formen (samme regel som migrering 9). Fristen kalleren gir (også ingen frist) står, med
    mindre kalleren sender paameldingsfrist_manuell=0 - da foreslås den (lagre_samlinger)."""
    if "faktura_dager_for" in felter:   # foer noen skriving; utelatt felt beholder schema-defaulten (14)
        felter["faktura_dager_for"] = valider_faktura_dager_for(felter["faktura_dager_for"])
    felter.pop("kursnr", None)          # tildeles ALLTID her - ogsaa ved duplisering faar kopien nytt nummer
    felter.setdefault("paameldingsfrist_manuell", 1)
    kolonner = ["kursnr", "kode", "navn", *felter.keys()]
    kurs_id = sett_inn(
        con, f"INSERT INTO kurs ({','.join(kolonner)}) VALUES ({','.join('?' * len(kolonner))})",
        [neste_teller(con, "kursnr"), kode, navn, *felter.values()],
    )
    if samlinger is not None:
        lagre_samlinger(con, kurs_id, samlinger, aktor=aktor, idag=idag, logg_endring=False)
    else:
        kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kurs_id,)).fetchone()
        for s in kursdatoer.fra_datoliste(datoer, kurs["start_kl"], kurs["slutt_kl"], kurs["timer_pr_dag"],
                                          dag_for_dag=kurs["betaling"] in ("per_samling", "deltaker_velger")):
            sid = sett_inn(con, "INSERT INTO samling (kurs_id, navn, start_kl, slutt_kl, timer_pr_dag) VALUES (?,?,?,?,?)",
                           (kurs_id, None, s.start_kl, s.slutt_kl, s.timer))
            for d in s.datoer():
                con.execute("INSERT INTO kursdag (kurs_id, dato, innsjekk_token, innsjekk_kode, samling_id) VALUES (?,?,?,?,?)",
                            (kurs_id, d.isoformat(), secrets.token_urlsafe(24), ny_kode(), sid))
    logg(con, "kurs_opprettet", {"kurs_id": kurs_id, "kode": kode,
                                 "kursnr": con.execute("SELECT kursnr FROM kurs WHERE id=?", (kurs_id,)).fetchone()[0]},
         aktor=aktor)
    return kurs_id


def kursdager(con, kurs_id: int) -> list[sqlite3.Row]:
    """Kursdagene i dato-rekkefølge, med samlingen sin (navn, klokkeslett og timer)."""
    return con.execute(
        """SELECT kd.*, s.navn AS samling_navn, s.start_kl AS samling_start_kl, s.slutt_kl AS samling_slutt_kl,
                  s.timer_pr_dag AS samling_timer
           FROM kursdag kd LEFT JOIN samling s ON s.id=kd.samling_id WHERE kd.kurs_id=? ORDER BY kd.dato""",
        (kurs_id,)).fetchall()


def antall_samlinger(con, kurs_id: int) -> int:
    """Antall samlinger i kurset: kursdagene gruppert per samling, og en kursdag uten samling teller for seg - samme
    gruppering som kurssiden (kursdatoer.visning) og delfaktureringen (sveiper.samlinger_til_fakturering)."""
    r = con.execute("""SELECT COUNT(DISTINCT samling_id) AS samlinger,
                              SUM(CASE WHEN samling_id IS NULL THEN 1 ELSE 0 END) AS enkeltdager
                       FROM kursdag WHERE kurs_id=?""", (kurs_id,)).fetchone()
    return int(r["samlinger"] or 0) + int(r["enkeltdager"] or 0)


class Kursdagfeil(ValueError):
    """Kursgjennomføringen kan ikke lagres. `meldinger` er norske forklaringer til admin."""

    def __init__(self, meldinger: list[str]):
        super().__init__(" ".join(meldinger))
        self.meldinger = list(meldinger)


def lagrede_samlinger(con, kurs_id: int) -> "list[kursdatoer.Samling]":
    """Kursets samlinger i dato-rekkefølge, med dagene som avviker (til redigeringsskjemaet). En kursdag uten samling
    (skal ikke finnes etter migrering 9) blir sin egen samling."""
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kurs_id,)).fetchone()
    rader = {r["id"]: r for r in con.execute("SELECT * FROM samling WHERE kurs_id=?", (kurs_id,))}
    grupper: dict = {}
    for d in con.execute("SELECT * FROM kursdag WHERE kurs_id=? ORDER BY dato", (kurs_id,)):
        grupper.setdefault(d["samling_id"] if d["samling_id"] in rader else ("uten", d["id"]), []).append(d)
    ut = []
    for sid, dager in grupper.items():
        s = rader.get(sid)
        datoer = [date.fromisoformat(d["dato"]) for d in dager]
        start = s["start_kl"] if s else (dager[0]["start_kl"] or kurs["start_kl"])
        slutt = s["slutt_kl"] if s else (dager[0]["slutt_kl"] or kurs["slutt_kl"])
        avvik: dict = {}
        til_stede = set(datoer)
        for i in range((max(datoer) - min(datoer)).days + 1):
            dato = min(datoer) + timedelta(days=i)
            if dato not in til_stede:
                avvik[dato] = kursdatoer.Dagavvik(fjernet=True)
        for d in dager:
            egen_start, egen_slutt = d["start_kl"] or kurs["start_kl"], d["slutt_kl"] or kurs["slutt_kl"]
            a = kursdatoer.Dagavvik(start_kl=egen_start if egen_start != start else None,
                                    slutt_kl=egen_slutt if egen_slutt != slutt else None,
                                    timer=d["timer"], merknad=d["merknad"])
            if a.start_kl or a.slutt_kl or a.timer is not None or a.merknad:
                avvik[date.fromisoformat(d["dato"])] = a
        ut.append(kursdatoer.Samling(fra=min(datoer), til=max(datoer), start_kl=start, slutt_kl=slutt,
                                     timer=s["timer_pr_dag"] if s else kurs["timer_pr_dag"], navn=s["navn"] if s else None,
                                     id=sid if s else None, avvik=avvik))
    return sorted(ut, key=lambda x: x.fra)


def lagre_samlinger(con, kurs_id: int, samlinger: "list[kursdatoer.Samling]", aktor: str = "admin", *,
                    idag: date | None = None, logg_endring: bool = True) -> dict:
    """Lagrer kursgjennomføringen (samlinger og kursdager) for et kurs. Kalleren committer.

    En kursdag som fortsatt er med, beholder id, innsjekkode og QR (bare samling, klokkeslett, timer og merknad
    oppdateres). Nye datoer får nye kursdager. En kursdag som ikke lenger er med, slettes - men bare når den verken har
    registrert oppmøte eller en faktura (eller et fakturaforsøk) knyttet til seg. Ellers lagres ingenting, og
    Kursdagfeil forklarer hvorfor.

    Kursets standard klokkeslett og timer settes til den første samlingens (for visninger som bare kjenner kurset).
    Påmeldingsfristen følger første kursdag (en kalendermåned før, se kursdatoer.foreslaatt_frist) så lenge admin ikke
    har satt den selv - men bare når første kursdag faktisk flyttes. Endres noe annet (klokkeslett, merknad, en senere
    dag), står fristen, så en frist som er passert aldri åpner påmeldingen igjen av seg selv."""
    feil = kursdatoer.kontroller(samlinger)
    if feil:
        raise Kursdagfeil(feil)
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kurs_id,)).fetchone()
    finnes = {d["dato"]: d for d in con.execute("SELECT * FROM kursdag WHERE kurs_id=?", (kurs_id,))}
    onsket: dict[str, tuple] = {}
    for i, s in enumerate(samlinger):
        for d in s.datoer():
            a = s.avvik.get(d, kursdatoer.Dagavvik())
            onsket[d.isoformat()] = (i, a.start_kl or s.start_kl, a.slutt_kl or s.slutt_kl, a.timer, a.merknad)
    fjernes = [d for dato, d in sorted(finnes.items()) if dato not in onsket]
    sperret = []
    for d in fjernes:
        dato = kursdatoer.kort_dato(date.fromisoformat(d["dato"]))
        if con.execute("SELECT 1 FROM oppmote WHERE kursdag_id=?", (d["id"],)).fetchone():
            sperret.append(f"{dato} har registrert oppmøte.")
        if con.execute("SELECT 1 FROM faktura WHERE kursdag_id=? UNION ALL SELECT 1 FROM faktura_forsok WHERE kursdag_id=?",
                       (d["id"], d["id"])).fetchone():
            sperret.append(f"{dato} har en faktura knyttet til seg.")
    if sperret:
        raise Kursdagfeil(["Datoene ble ikke lagret. Disse kursdagene kan ikke fjernes:", *sperret,
                           "Behold dagene, eller fjern oppmøtet først. En fakturert kursdag kan ikke fjernes."])

    egne = {r["id"] for r in con.execute("SELECT id FROM samling WHERE kurs_id=?", (kurs_id,))}
    # Ekstradeltakernes samlingsutvalg slik det var FØR endringen, og datoene de da var på (til sammenligning etterpå)
    utvalg_for = samlingsutvalg_for_kurs(con, kurs_id)
    datoer_for: dict[int, set[str]] = {}
    for d in finnes.values():
        if d["samling_id"] is not None:
            datoer_for.setdefault(d["samling_id"], set()).add(d["dato"])
    utvalgsdatoer_for = {pid: set().union(*(datoer_for.get(i, set()) for i in ider)) for pid, ider in utvalg_for.items()}
    # En samling som fjernes og legges inn igjen med NØYAKTIG de samme datoene får ny id (raden hadde tom id). Utvalget følger da med til
    # den nye samlingen, i stedet for å forsvinne stille med den gamle (ON DELETE CASCADE).
    beholdt = {s.id for s in samlinger if s.id in egne}
    arvinger: dict[int, int] = {}                        # gammel samling-id -> plassen i `samlinger` til den nye samlingen med samme datoer
    for gammel in sorted(egne - beholdt):
        if not datoer_for.get(gammel):
            continue
        for i, s in enumerate(samlinger):
            if s.id not in egne and i not in arvinger.values() and {d.isoformat() for d in s.datoer()} == datoer_for[gammel]:
                arvinger[gammel] = i
                break
    # En ekstradeltaker som BARE er satt opp på samlinger som nå fjernes, ville plutselig fått «hele kurset» (ingen utvalgsrader er
    # «hele kurset») og alle påminnelser. Derfor stopper vi først: admin endrer utvalget til deltakeren, og kan så fjerne samlingen.
    bevares = {s.id for s in samlinger if s.id in egne and s.datoer()} | set(arvinger)
    stranda = [pid for pid, ider in utvalg_for.items() if not set(ider) & bevares]
    if stranda:
        raise Kursdagfeil([
            "Datoene ble ikke lagret. "
            + (f"{len(stranda)} {PAAMELDINGSSTATUSER_FLERTALL['ekstradeltaker'].lower()} er" if len(stranda) != 1 else "1 ekstradeltaker er")
            + " bare satt opp på samlinger som nå fjernes, og ville dermed blitt satt opp på hele kurset.",
            "Endre hvilke samlinger deltakeren er satt opp på (åpne deltakeren i deltakerlisten), og fjern samlingen etterpå."])
    sid: list[int] = []
    for s in samlinger:
        if s.id in egne:                                # en id kan bare brukes én gang (en kopiert rad blir ny)
            egne.discard(s.id)
            con.execute("UPDATE samling SET navn=?, start_kl=?, slutt_kl=?, timer_pr_dag=? WHERE id=?",
                        (s.navn, s.start_kl, s.slutt_kl, s.timer, s.id))
            sid.append(s.id)
        else:
            sid.append(sett_inn(con, """INSERT INTO samling (kurs_id, navn, start_kl, slutt_kl, timer_pr_dag)
                                         VALUES (?,?,?,?,?)""", (kurs_id, s.navn, s.start_kl, s.slutt_kl, s.timer)))
    for gammel, i in arvinger.items():                   # utvalget flyttes til den gjenskapte samlingen FØR den gamle slettes
        con.execute("""INSERT INTO ekstradeltaker_samling (paamelding_id, samling_id)
                       SELECT paamelding_id, ? FROM ekstradeltaker_samling WHERE samling_id=?""", (sid[i], gammel))
    lagt_til = 0
    for dato, (i, start, slutt, timer, merknad) in sorted(onsket.items()):
        if dato in finnes:
            con.execute("UPDATE kursdag SET samling_id=?, start_kl=?, slutt_kl=?, timer=?, merknad=? WHERE id=?",
                        (sid[i], start, slutt, timer, merknad, finnes[dato]["id"]))
        else:
            con.execute("""INSERT INTO kursdag (kurs_id, dato, innsjekk_token, innsjekk_kode, samling_id, start_kl,
                                                slutt_kl, timer, merknad) VALUES (?,?,?,?,?,?,?,?,?)""",
                        (kurs_id, dato, secrets.token_urlsafe(24), ny_kode(), sid[i], start, slutt, timer, merknad))
            lagt_til += 1
    for d in fjernes:
        con.execute("DELETE FROM kursdag WHERE id=?", (d["id"],))
    for gammel in egne:                                 # samlinger som ikke er med lenger (dagene er flyttet eller fjernet)
        con.execute("DELETE FROM samling WHERE id=? AND NOT EXISTS (SELECT 1 FROM kursdag WHERE samling_id=?)",
                    (gammel, gammel))                  # utvalgsradene til ekstradeltakere går med (ON DELETE CASCADE)
    normaliser_ekstradeltaker_utvalg(con, kurs_id)      # et utvalg som nå dekker alle samlingene er «hele kurset»
    # Hvem fikk færre (eller andre) dager enn før? Kalleren sier fra til administrator: utvalget skal aldri krympe i det stille.
    utvalg_endret = []
    if utvalg_for:
        na_alle = {r["dato"] for r in con.execute("SELECT dato FROM kursdag WHERE kurs_id=?", (kurs_id,))}
        na_samling: dict[int, set[str]] = {}
        for r in con.execute("SELECT samling_id, dato FROM kursdag WHERE kurs_id=? AND samling_id IS NOT NULL", (kurs_id,)):
            na_samling.setdefault(r["samling_id"], set()).add(r["dato"])
        na_utvalg = samlingsutvalg_for_kurs(con, kurs_id)
        for pid, for_ in utvalgsdatoer_for.items():
            na = set().union(*(na_samling.get(i, set()) for i in na_utvalg[pid])) if pid in na_utvalg else na_alle
            if na != for_:
                utvalg_endret.append({"paamelding_id": pid, "fra_dager": sorted(for_), "til_dager": sorted(na)})
    # En planlagt, utsatt samlet faktura (seks måneder før første kursdag) følger første kursdag - i SAMME transaksjon,
    # så en gammel dato aldri brukes mot de nye datoene.
    from . import sveiper   # importeres her: sveiper bruker db
    sveiper.oppdater_faktura_hold_etter_kursdagendring(con, kurs_id)

    forste = min((s for s in samlinger if s.datoer()), key=lambda s: s.datoer()[0])
    endringer = {"start_kl": forste.start_kl, "slutt_kl": forste.slutt_kl, "timer_pr_dag": forste.timer}
    forste_dato = forste.datoer()[0]
    if not kurs["paameldingsfrist_manuell"] and (not finnes or forste_dato.isoformat() != min(finnes)):
        endringer["paameldingsfrist"] = kursdatoer.foreslaatt_frist(forste_dato, idag or date.today()).isoformat()
    con.execute(f"UPDATE kurs SET {','.join(f'{k}=?' for k in endringer)} WHERE id=?", [*endringer.values(), kurs_id])
    oppsummering = {"kurs_id": kurs_id, "samlinger": len([s for s in samlinger if s.datoer()]),
                    "kursdager": len(onsket), "lagt_til": lagt_til, "fjernet": len(fjernes)}
    if utvalg_endret:                                   # bare antallet i loggen: hvem det gjaldt står i `utvalg_endret` (til meldingen)
        oppsummering["utvalg_endret_antall"] = len(utvalg_endret)
    if logg_endring:
        logg(con, "kursdager_endret", oppsummering, aktor=aktor)
    oppsummering["paameldingsfrist"] = endringer.get("paameldingsfrist")
    oppsummering["utvalg_endret"] = utvalg_endret       # [{"paamelding_id", "fra_dager", "til_dager"}]: ekstradeltakere som fikk andre dager
    return oppsummering


def antall_bekreftet(con, kurs_id: int) -> int:
    """Antall FULLVERDIGE deltakere med plass (Påmeldt): tallet som teller mot kapasiteten, i «plasser igjen», fullt kurs,
    opprykk fra ventelisten, overbooking og alle tall for påmeldte. Ekstradeltakere (ekstradeltaker_ts satt) tar ikke plass og
    er ikke med her: se antall_ekstradeltakere, og antall_med_plass for alle som får utsendinger og er på kurset."""
    return con.execute(
        "SELECT COUNT(*) FROM paamelding WHERE kurs_id=? AND status='bekreftet' AND ekstradeltaker_ts IS NULL", (kurs_id,)
    ).fetchone()[0]


def _laas_kurs(con, kurs_id: int) -> None:
    """Låser kurset for andre som skriver, til transaksjonen er ferdig (commit eller rollback). Kalles FØR plassene telles
    (antall_bekreftet) i det som gir noen en plass: uten låsen leser to samtidige forespørsler begge «en plass igjen» og begge får den
    (overbooking uten at noen fikk spørsmålet). SQLite tar skrivelåsen nå (ikke først ved første UPDATE, etter at tallet er lest), og
    PostgreSQL tar radlåsen på kursraden: den andre venter, og leser da det første resultatet. En UPDATE som ikke endrer noe."""
    con.execute("UPDATE kurs SET status=status WHERE id=?", (kurs_id,))


def antall_ekstradeltakere(con, kurs_id: int) -> int:
    """Antall ekstradeltakere på kurset: er på kurset (utsendinger, innsjekk, kursside, kursbevis), men tar ikke plass."""
    return con.execute(
        "SELECT COUNT(*) FROM paamelding WHERE kurs_id=? AND status='bekreftet' AND ekstradeltaker_ts IS NOT NULL", (kurs_id,)
    ).fetchone()[0]


def antall_med_plass(con, kurs_id: int) -> int:
    """Påmeldte og ekstradeltakere til sammen: alle som er på kurset og berøres av endringer i sted, format, datoer og avlysning."""
    return con.execute(
        "SELECT COUNT(*) FROM paamelding WHERE kurs_id=? AND status='bekreftet'", (kurs_id,)).fetchone()[0]


# ---------- ekstradeltakere: samlingsutvalg (migrering 18) ----------
#
# En ekstradeltaker deltar enten på hele kurset (INGEN rader i ekstradeltaker_samling) eller bare på utvalgte samlinger (én rad
# per samling). Alle samlinger valgt lagres aldri som rader: da er det «hele kurset», så en samling som legges til senere
# følger med. `paameldingens_kursdager` er ENESTE vei til kursdagene en påmelding er på: påminnelser, e-post, innsjekk, Min side,
# kursside, oppmøte, kursbevis, søk og rapporter bruker den. Utvalget gjelder bare når ekstradeltaker_ts er satt: rader som
# blir hengende på en påmelding som ikke er ekstradeltaker, har ingen virkning (og ryddes).

def er_ekstradeltaker(p) -> bool:
    """Er raden en ekstradeltaker? Raden må komme fra en SELECT som tok med ekstradeltaker_ts: uten kolonnen regnes den som ikke
    ekstradeltaker (en bare fakturering/pris/status-dict fra fakturamotoren har den heller ikke). Skal du finne KURSDAGENE til en
    påmelding, bruk paameldingens_kursdager: den slår kolonnen opp i databasen når raden mangler den, i stedet for å anta noe."""
    try:
        return bool(p["ekstradeltaker_ts"])
    except (KeyError, IndexError):          # radens SELECT tok ikke med kolonnen: regnes som ikke ekstradeltaker
        return False


def _paamelding_rad(con, paamelding):
    """Raden med id, kurs_id og ekstradeltaker_ts for en påmelding gitt som rad eller id. Har raden ikke alle tre kolonnene (en SELECT
    som glemte ekstradeltaker_ts), slås de opp i databasen. Å anta «ikke ekstradeltaker» ville gitt ALLE kursdager - påminnelser,
    e-post, innsjekk, kursside og kursbevis - til en som bare er på noen samlinger, uten at noe feilet. None = ukjent påmelding."""
    if not isinstance(paamelding, int):
        try:
            if {"id", "kurs_id", "ekstradeltaker_ts"} <= set(paamelding.keys()):
                return paamelding
        except AttributeError:
            pass
        paamelding = paamelding["id"]
    return con.execute("SELECT id, kurs_id, ekstradeltaker_ts FROM paamelding WHERE id=?", (paamelding,)).fetchone()


def samlingstekst(titler: list[str], *, og: bool = False) -> str:
    """Titlene som en tekst: «Samling 1, 3» (bare «Samling N») eller «Grunnkurs, Fordypning». Med og=True: «Samling 1 og 3»."""
    if not titler:
        return ""
    nummer = [t[len("Samling "):] for t in titler if t.startswith("Samling ") and t[len("Samling "):].isdigit()]
    if len(nummer) == len(titler):
        deler, ledd = nummer, "Samling "
    else:
        deler, ledd = list(titler), ""
    liste = deler[0] if len(deler) == 1 else (f"{', '.join(deler[:-1])} og {deler[-1]}" if og else ", ".join(deler))
    return ledd + liste


def i_setning(tekst: str) -> str:
    """Samlingsteksten midt i en setning: «Samling 1 og 3» blir «samling 1 og 3». Et navn admin selv har gitt en samling («EMDR-dagen»,
    «Veiledning ved NTNU») beholdes slik det står: å gjøre det om til små bokstaver ville gjort «EMDR» til «emdr»."""
    return tekst[:1].lower() + tekst[1:] if tekst.startswith("Samling ") else tekst


def kursets_samlinger(con, kurs_id: int | None, dager=None) -> list[dict]:
    """Samlingene med kursdager, i rekkefølgen de starter: [{"id", "nr", "tittel", "navn", "fra", "til", "antall_dager"}].
    `nr` er samlingens nummer i HELE kurset (den samme «Samling N» som kurssiden og e-postene bruker), og `tittel` er det lagrede
    navnet eller «Samling N». Bare samlinger som har kursdager tas med (en samling uten kursdager kan ikke velges).
    `dager` er kursets dager (rader fra `kursdager`) hvis kalleren har dem fra før."""
    ut: list[dict] = []
    for d in (kursdager(con, kurs_id) if dager is None else dager):
        sid = d["samling_id"]
        if sid is None:
            continue
        s = next((x for x in ut if x["id"] == sid), None)
        if s is None:
            s = {"id": int(sid), "nr": len(ut) + 1, "navn": d["samling_navn"], "tittel": d["samling_navn"] or f"Samling {len(ut) + 1}",
                 "fra": d["dato"], "til": d["dato"], "antall_dager": 0}
            ut.append(s)
        s["til"], s["antall_dager"] = d["dato"], s["antall_dager"] + 1
    return ut


def ekstradeltaker_samlinger(con, paamelding_id: int) -> list[int]:
    """Samlingene ekstradeltakeren er satt opp på (id-er). Tom liste = hele kurset."""
    return [int(r[0]) for r in con.execute(
        "SELECT samling_id FROM ekstradeltaker_samling WHERE paamelding_id=? ORDER BY samling_id", (paamelding_id,))]


def samlingsutvalg_for_kurs(con, kurs_id: int) -> dict[int, list[int]]:
    """{paamelding_id: [samling_id, ...]} for alle ekstradeltakere på kurset som har et utvalg (til lister)."""
    ut: dict[int, list[int]] = {}
    # I rekkefølgen samlingene starter (dato), ikke etter id: en samling som legges til senere kan ligge tidligst i tid og ha høyest id.
    # Da gir listene, eksportene og søket samme «Samling 1, 2» som e-postene og kursbeviset (kursets_samlinger er i datorekkefølge).
    for r in con.execute("""SELECT es.paamelding_id, es.samling_id FROM ekstradeltaker_samling es
                            JOIN paamelding p ON p.id=es.paamelding_id
                            WHERE p.kurs_id=? AND p.ekstradeltaker_ts IS NOT NULL
                            ORDER BY es.paamelding_id, (SELECT MIN(kd.dato) FROM kursdag kd WHERE kd.samling_id=es.samling_id),
                                     es.samling_id""", (kurs_id,)):
        ut.setdefault(int(r[0]), []).append(int(r[1]))
    return ut


def _ryd_ekstradeltaker_samlinger(con, paamelding_id: int) -> None:
    con.execute("DELETE FROM ekstradeltaker_samling WHERE paamelding_id=?", (paamelding_id,))


def _kontroller_utvalg(con, kurs_id: int, samlinger) -> list[int]:
    """Kontrollerer et samlingsutvalg FØR noe skrives, og gir det slik det lagres: [] = hele kurset (ingen valgt, eller alle
    samlingene valgt), ellers de valgte id-ene. En samling som ikke hører til kurset eller ikke har kursdager, avvises."""
    if not samlinger:
        return []
    try:
        valgt = {int(s) for s in samlinger}
    except (TypeError, ValueError):
        raise Paameldingsfeil("Ugyldig samling.") from None
    gyldige = {s["id"] for s in kursets_samlinger(con, kurs_id)}
    if not valgt <= gyldige:
        raise Paameldingsfeil("Velg samlinger som hører til dette kurset og har kursdager.")
    return [] if valgt == gyldige else sorted(valgt)


def _lagre_utvalg(con, paamelding_id: int, valgt: list[int]) -> None:
    _ryd_ekstradeltaker_samlinger(con, paamelding_id)
    for sid in valgt:
        con.execute("INSERT INTO ekstradeltaker_samling (paamelding_id, samling_id) VALUES (?,?)", (paamelding_id, sid))


def sett_ekstradeltaker_samlinger(con, paamelding_id: int, samlinger, aktor: str = "admin") -> list[int]:
    """Endrer hvilke samlinger en ekstradeltaker deltar på. `samlinger` tom/None = hele kurset; alle samlingene valgt = også
    hele kurset (ingen rader). Bare en ekstradeltaker kan ha utvalg. Returnerer utvalget som ble lagret ([] = hele kurset).
    Kalleren committer. Loggen har bare id-er."""
    p = con.execute("SELECT id, kurs_id, ekstradeltaker_ts FROM paamelding WHERE id=?", (paamelding_id,)).fetchone()
    if not p:
        raise Paameldingsfeil("Ukjent påmelding")
    if not p["ekstradeltaker_ts"]:
        raise Paameldingsfeil("Bare en ekstradeltaker kan velge samlinger.")
    valgt = _kontroller_utvalg(con, p["kurs_id"], samlinger)
    if valgt != ekstradeltaker_samlinger(con, paamelding_id):
        _lagre_utvalg(con, paamelding_id, valgt)
        con.execute("UPDATE paamelding SET oppdatert=? WHERE id=?", (datetime.now().isoformat(timespec="seconds"), paamelding_id))
        logg(con, "ekstradeltaker_utvalg", {"paamelding_id": paamelding_id, "hele_kurset": not valgt, "antall": len(valgt)}, aktor=aktor)
    return valgt


def normaliser_ekstradeltaker_utvalg(con, kurs_id: int) -> int:
    """Et utvalg som dekker ALLE kursets samlinger er «hele kurset»: radene fjernes, så en samling som legges til senere følger
    med. Kalles når samlinger er fjernet (db.lagre_samlinger). Returnerer antall utvalg som ble til «hele kurset»."""
    gyldige = {s["id"] for s in kursets_samlinger(con, kurs_id)}
    antall = 0
    for pid, ider in samlingsutvalg_for_kurs(con, kurs_id).items():
        if set(ider) >= gyldige:
            _ryd_ekstradeltaker_samlinger(con, pid)
            antall += 1
    return antall


def paameldingens_kursdager(con, paamelding, alle_dager=None) -> list:
    """Kursdagene en påmelding faktisk er på, i datorekkefølge, med samme radform som `kursdager` (samling, klokkeslett, timer).
    Alle andre enn ekstradeltakere med utvalg er på ALLE kursets dager, og får da de samme radene som `kursdager` gir. En
    ekstradeltaker med utvalg får bare dagene i de valgte samlingene, og hver rad har da også `samling_nr` (samlingens nummer i
    HELE kurset) og `samling_flere` (kurset har flere samlinger), slik at «Samling 3» heter det også når bare den er med
    (kursdatoer.visning bruker dem). En kursdag uten samling er bare med for «hele kurset». `paamelding` er en rad med id (og
    ekstradeltaker_ts og kurs_id) eller en id; `alle_dager` er kursets dager hvis kalleren har dem fra før."""
    paamelding = _paamelding_rad(con, paamelding)
    if paamelding is None:
        return []
    if alle_dager is None:
        alle_dager = kursdager(con, paamelding["kurs_id"])
    if not er_ekstradeltaker(paamelding):
        return alle_dager
    valgt = set(ekstradeltaker_samlinger(con, paamelding["id"]))
    if not valgt:
        return alle_dager
    numre = {s["id"]: s["nr"] for s in kursets_samlinger(con, None, alle_dager)}
    flere = len(numre) > 1
    return [{**dict(d), "samling_nr": numre[d["samling_id"]], "samling_flere": flere}
            for d in alle_dager if d["samling_id"] in valgt and d["samling_id"] in numre]


def sql_egen_dag(paamelding: str = "p", kursdag: str = "kd") -> str:
    """SQL-vilkåret «kursdagen er en av dagene påmeldingen er på» (samme regel som paameldingens_kursdager) til spørringer som
    teller oppmøte: alle andre enn ekstradeltakere med utvalg er på alle dager. Aliasene er faste navn i koden - aldri brukerinput."""
    p, kd = paamelding, kursdag
    return (f"({p}.ekstradeltaker_ts IS NULL "
            f"OR NOT EXISTS (SELECT 1 FROM ekstradeltaker_samling es WHERE es.paamelding_id={p}.id) "
            f"OR EXISTS (SELECT 1 FROM ekstradeltaker_samling es WHERE es.paamelding_id={p}.id AND es.samling_id={kd}.samling_id))")


def samlingsutvalg_tekst(con, paamelding, alle_dager=None, *, og: bool = False) -> str:
    """«Hele kurset» eller «Samling 1, 3» (med og=True: «Samling 1 og 3») for en påmelding. En som ikke er ekstradeltaker, er
    alltid på hele kurset."""
    paamelding = _paamelding_rad(con, paamelding)
    valgt = set(ekstradeltaker_samlinger(con, paamelding["id"])) if paamelding is not None and er_ekstradeltaker(paamelding) else set()
    if not valgt:
        return "Hele kurset"
    return samlingstekst([s["tittel"] for s in kursets_samlinger(con, paamelding["kurs_id"], alle_dager) if s["id"] in valgt], og=og)


# ---------- deltakere / paamelding ----------

def fullt_navn(fornavn: str | None, etternavn: str | None) -> str:
    """Fullt navn slik det lagres i `navn`/`kontakt_navn` og vises: «fornavn etternavn» (en tom del utelates). ENESTE sted
    navnet settes sammen - kursbevis, lister, fakturaer, sok og Zoom-oppmote leser `navn` som foer."""
    return " ".join(d for d in ((fornavn or "").strip(), (etternavn or "").strip()) if d)


def finn_eller_opprett_deltaker(con, epost: str, fornavn: str, etternavn: str, *, beskytt_eksisterende_felt: bool = False,
                                aktor: str | None = None, **felter) -> int:
    """Finner eller oppretter en deltaker (person) paa e-post. Fornavn og etternavn er paakrevd og lagres hver for seg;
    `navn` settes av fullt_navn(). Navnet til en person som finnes fra foer, endres ikke herfra (kun i deltakervinduet).

    `beskytt_eksisterende_felt` styrer hva som skjer naar personen ALLEREDE finnes og et felt i
    `felter` har en verdi som er ULIK det som staar der fra for:
      - False (STANDARD, uendret oppforsel - ALLE eksisterende kall beholder dette): feltet
        overskrives med den nye verdien (f.eks. et oppdatert telefonnummer ved neste paamelding).
      - True: feltet overskrives KUN hvis det er tomt fra for - en eksisterende, ikke-tom verdi
        beholdes alltid. Ment for kilder som IKKE noedvendigvis er ferskere enn det som allerede
        er registrert (f.eks. fase 10 sin CSV-import, som kan vaere en gammel Excel-fil) - de skal
        aldri kunne overskrive noe en admin/deltaker har registrert senere andre steder.

    ADRESSEN (adresse, postnr, poststed - deltakerens private adresse) folger de samme reglene som telefon og
    arbeidssted: melder samme e-post seg paa igjen med en NY adresse, overskrives den gamle (den nyeste er riktigst - med
    unntak av CSV-import, se over), og en tom verdi overskriver ALDRI en adresse som staar der fra for. Den offentlige
    paameldingen har ingen innlogging, saa en ny adresse er ikke bekreftet av personen. Derfor logges hver overskriving som
    «deltaker_endret» (bare feltnavnene, aldri verdiene; aktoer `aktor`, ellers `deltaker:<id>`), og den vises i Logger.
    En overskriving flytter IKKE fakturaadressen paa personens andre, eksisterende paameldinger - bare en rettelse fra
    administrator i deltakervinduet gjoer det (oppdater_deltaker).
    """
    fornavn, etternavn = (fornavn or "").strip(), (etternavn or "").strip()
    if not (fornavn and etternavn):
        raise Paameldingsfeil("Fyll inn både fornavn og etternavn.")
    epost = epost.strip().lower()
    rad = con.execute("SELECT * FROM deltaker WHERE epost=?", (epost,)).fetchone()
    if rad:
        if beskytt_eksisterende_felt:
            oppdater = {k: v for k, v in felter.items() if v and not rad[k]}
        else:
            oppdater = {k: v for k, v in felter.items() if v}
        if oppdater:
            con.execute(
                f"UPDATE deltaker SET {','.join(f'{k}=?' for k in oppdater)} WHERE id=?",
                [*oppdater.values(), rad["id"]],
            )
        overskrevet = [k for k in skjemafelt.ADRESSEFELT
                       if rad[k] and k in oppdater and str(oppdater[k]).strip() != rad[k].strip()]
        if overskrevet:
            logg(con, "deltaker_endret", {"deltaker_id": rad["id"], "felt": overskrevet},
                 aktor=aktor or f"deltaker:{rad['id']}")
        return rad["id"]
    kolonner = ["epost", "navn", "fornavn", "etternavn", *felter.keys()]
    return sett_inn(
        con, f"INSERT INTO deltaker ({','.join(kolonner)}) VALUES ({','.join('?' * len(kolonner))})",
        [epost, fullt_navn(fornavn, etternavn), fornavn, etternavn, *felter.values()],
    )


class Paameldingsfeil(Exception):
    pass


class DeltakerFeil(Exception):
    pass


# ---------- admin: redigering av deltaker/paamelding (fase 3) ----------

def _adresse_ved_redigering(gammel, felter: dict) -> dict:
    """Adressefeltene i en rettelse fra deltakervinduet (oppdater_deltaker). Returnerer `felter` med rensede adresseverdier,
    eller UTEN adressefeltene naar adressen ikke endres. Reglene (brukerens beslutning 30.09.2026):
      * Adressen slik den blir etter rettelsen = det som er sendt, resten av det som staar der fra for.
      * Er den UENDRET, valideres den ikke: en eldre person som mangler adressen (eller har en ufullstendig en fra foer
        kravet kom), skal kunne faa lagret telefon, arbeidssted m.m. uten at adressen maa fylles inn samtidig. Hun er merket
        «Privat adresse mangler» (skjemafelt.adresse_mangler) til adressen er lagt inn.
      * Er den endret, kreves ALLE tre feltene (samme validering som alle andre veier): en delvis adresse avvises, og en
        adresse som staar der kan aldri tommes eller gjoeres ufullstendig."""
    ny = {k: (felter[k] if k in felter else gammel[k]) for k in skjemafelt.ADRESSEFELT}
    rens = skjemafelt.rens_adresse(ny)
    uten_adresse = {k: v for k, v in felter.items() if k not in skjemafelt.ADRESSEFELT}
    if all((gammel[k] or "").strip() == (rens[k] or "") for k in skjemafelt.ADRESSEFELT):
        return uten_adresse
    if feil := skjemafelt.valider_adresse(ny):
        raise DeltakerFeil(" ".join(feil.values()))
    return {**uten_adresse, **{k: rens[k] for k in skjemafelt.ADRESSEFELT if k in felter}}


def oppdater_deltaker(con, deltaker_id: int, felter: dict, aktor: str = "admin") -> list[str]:
    """Oppdaterer person-opplysninger. Disse deles paa tvers av alle kurs personen er meldt paa,
    saa paamelding.oppdatert settes for ALLE deres paameldinger, ikke bare den man sto paa.

    Adressen (adresse, postnr, poststed): se _adresse_ved_redigering - en person uten adresse kan lagres uten den, en
    adresse som er fylt ut maa vaere komplett, og en adresse som staar der kan ikke tommes.

    Returnerer navnene paa feltene som faktisk ble endret. Hendelsesloggen faar kun feltnavnene,
    aldri gammel/ny verdi (personopplysninger skal ikke ligge i loggen).
    """
    gammel = con.execute("SELECT * FROM deltaker WHERE id=?", (deltaker_id,)).fetchone()
    if not gammel:
        raise DeltakerFeil("Ukjent deltaker")

    if "epost" in felter:
        epost = (felter["epost"] or "").strip().lower()
        if not epost or "@" not in epost:
            raise DeltakerFeil("Ugyldig e-postadresse.")
        opptatt = con.execute("SELECT 1 FROM deltaker WHERE epost=? AND id!=?", (epost, deltaker_id)).fetchone()
        if opptatt:
            raise DeltakerFeil("E-postadressen er allerede i bruk av en annen deltaker.")
        felter = {**felter, "epost": epost}
    if any(k in felter for k in skjemafelt.ADRESSEFELT):
        felter = _adresse_ved_redigering(gammel, felter)
    if "navn" in felter:
        raise DeltakerFeil("Fullt navn settes automatisk fra fornavn og etternavn.")
    if "fornavn" in felter or "etternavn" in felter:
        fornavn = (felter.get("fornavn", gammel["fornavn"]) or "").strip()
        etternavn = (felter.get("etternavn", gammel["etternavn"]) or "").strip()
        if not (fornavn and etternavn):
            raise DeltakerFeil("Fyll inn både fornavn og etternavn.")
        felter = {**felter, "fornavn": fornavn, "etternavn": etternavn, "navn": fullt_navn(fornavn, etternavn)}

    endret = [felt for felt, verdi in felter.items() if (gammel[felt] or "") != (verdi or "")]
    if not endret:
        return []
    con.execute(f"UPDATE deltaker SET {','.join(f'{f}=?' for f in felter)} WHERE id=?",
               [*felter.values(), deltaker_id])
    na = datetime.now().isoformat(timespec="seconds")
    con.execute("UPDATE paamelding SET oppdatert=? WHERE deltaker_id=?", (na, deltaker_id))
    logg(con, "deltaker_endret", {"deltaker_id": deltaker_id, "felt": endret}, aktor=aktor)
    if "epost" in endret:
        from . import minside      # (senere import: minside bruker db) Lenker sendt til den gamle adressen slutter å virke
        minside.roter_for_deltaker(con, deltaker_id, aktor, "epost_endret")
    if any(k in endret for k in skjemafelt.ADRESSEFELT):
        _foelg_fakturaadressen(con, deltaker_id, gammel, {k: felter.get(k, gammel[k]) for k in skjemafelt.ADRESSEFELT},
                               aktor)
    return endret


def _foelg_fakturaadressen(con, deltaker_id: int, gammel, ny: dict, aktor: str) -> int:
    """Personens adresse er rettet i deltakervinduet. Fakturaadressen paa en PRIVAT betalers paameldinger er en kopi av
    adressen fra registreringen (meld_paa) - og fakturamotoren leser bare kopien. Uten dette gikk fakturaer som lages
    etterpaa (holdte, delfakturaer, utsatte samlede) til den gamle adressen, selv om adressen var rettet.

    Kopien flyttes bare naar den er UENDRET siden registreringen (lik personens gamle adresse) eller tom: har
    administrator skrevet en egen fakturaadresse, eller er kopien adressen fra en annen registrering, roeres den ikke.
    Paameldinger med en ferdig samlet faktura roeres heller ikke (adressen er historikk). Firma-paameldinger roeres
    aldri (adressen kommer fra Enhetsregisteret). Logger bare feltnavnene. Returnerer antall paameldinger som ble endret."""
    def adr(kilde, nokler) -> tuple:
        return tuple((kilde[k] or "").strip() for k in nokler)

    gammel_adresse, ny_adresse = adr(gammel, skjemafelt.ADRESSEFELT), adr(ny, skjemafelt.ADRESSEFELT)
    antall = 0
    for p in con.execute(
            """SELECT id, faktura_adresse, faktura_postnr, faktura_sted FROM paamelding
               WHERE deltaker_id=? AND betaler='person'
                 AND NOT (betaling='samlet' AND EXISTS (SELECT 1 FROM faktura f WHERE f.paamelding_id=paamelding.id))""",
            (deltaker_id,)).fetchall():
        kopi = adr(p, _FAKTURAADRESSE)
        if kopi == ny_adresse or kopi not in (gammel_adresse, ("", "", "")):
            continue
        con.execute("UPDATE paamelding SET faktura_adresse=?, faktura_postnr=?, faktura_sted=? WHERE id=?",
                    (ny["adresse"] or None, ny["postnr"] or None, ny["poststed"] or None, p["id"]))
        logg(con, "paamelding_endret", {"paamelding_id": p["id"], "felt": list(_FAKTURAADRESSE)}, aktor=aktor)
        antall += 1
    return antall


def oppdater_paamelding(con, paamelding_id: int, felter: dict, aktor: str = "admin") -> list[str]:
    """Oppdaterer feltene som kun gjelder denne paameldingen (ikke status, se sett_paamelding_status).

    Hendelsesloggen faar kun feltnavnene som ble endret, aldri verdiene.
    """
    gammel = con.execute("SELECT * FROM paamelding WHERE id=?", (paamelding_id,)).fetchone()
    if not gammel:
        raise Paameldingsfeil("Ukjent påmelding")
    endret = [felt for felt, verdi in felter.items() if (gammel[felt] or "") != (verdi or "")]
    if not endret:
        return []
    na = datetime.now().isoformat(timespec="seconds")
    con.execute(f"UPDATE paamelding SET {','.join(f'{f}=?' for f in felter)}, oppdatert=? WHERE id=?",
               [*felter.values(), na, paamelding_id])
    logg(con, "paamelding_endret", {"paamelding_id": paamelding_id, "felt": endret}, aktor=aktor)
    return endret


def fakturaplan_sperre(con, kurs_id: int, p) -> str | None:
    """Hvorfor kan ikke fakturaplanen (samlet eller per samling) velges for denne påmeldingen? None = den kan velges. `p` er en påmeldingsrad.

    Planen hører til hver påmelding (paamelding.betaling). Kurset bestemmer hva nye påmeldinger får, men administrator kan gjøre et unntak for én
    deltaker (Camilla 02.10.2026: «det må selvfølgelig være mulig i enkelte tilfeller»), så lenge ingenting er fakturert eller forsøkt fakturert.
    Etter en faktura er planen låst: en samlet faktura (kursdag NULL) og delfakturaer er forskjellige plasser i faktura_unik, så et bytte kunne
    gitt dobbel fakturering (regel 6 i CLAUDE.md). Det rettes i Visma. En ekstradeltaker faktureres aldri automatisk, og med én samling er det
    ikke noe å velge."""
    if con.execute("SELECT 1 FROM faktura WHERE paamelding_id=?", (p["id"],)).fetchone():
        return "allerede fakturert – planen kan ikke endres etter at en faktura er laget (det rettes i Visma)"
    if con.execute("SELECT 1 FROM faktura_forsok WHERE paamelding_id=?", (p["id"],)).fetchone():
        return "et fakturaforsøk er ikke avklart – avklar det først under Uavklarte operasjoner"
    if er_ekstradeltaker(p):
        return "faktureres manuelt (ekstradeltaker)"
    if antall_samlinger(con, kurs_id) < 2:
        return "kurset har bare én samling"
    return None


def bytt_fakturaplan(con, paamelding_id: int, ny_plan: str, aktor: str = "admin") -> bool:
    """Bytter fakturaplanen (samlet eller per_samling) for ÉN påmelding. True = planen ble endret. Kalleren committer, og kjører deretter
    sveiper.kjor for påmeldingen (app._fakturaplan_videre), så den nye planen tas i bruk med en gang.

    Avvises (Paameldingsfeil) når fakturaplan_sperre sier nei, også om noen skulle poste mot ruten uansett. En bekreftet påmelding går gjennom
    «ved påmelding»-kjeden på nytt: hold-dato og sveiper_kjort nullstilles. Uten det ville ingen laget fakturaen etter bytte fra per samling til
    samlet, for morgenjobben fakturerer bare samlede fakturaer MED hold-dato (utsatte_fakturaer) og delfakturaer for plan «per samling»
    (forfalte_delfakturaer). Bekreftelsen sendes ikke på nytt (dedupliseres), og en holdt påmelding (sveiper_utsatt) røres ikke av kjeden.
    Hendelsen faktura_plan_endret (fra og til, aldri persondata) vises i Logger."""
    if ny_plan not in ("samlet", "per_samling"):
        raise Paameldingsfeil("Ugyldig fakturaplan.")
    p = con.execute("SELECT * FROM paamelding WHERE id=?", (paamelding_id,)).fetchone()
    if not p:
        raise Paameldingsfeil("Ukjent påmelding")
    if p["betaling"] == ny_plan:
        return False
    if grunn := fakturaplan_sperre(con, p["kurs_id"], p):
        raise Paameldingsfeil(f"Fakturaplanen kan ikke endres: {grunn}.")
    con.execute("UPDATE paamelding SET betaling=?, oppdatert=? WHERE id=?",
                (ny_plan, datetime.now().isoformat(timespec="seconds"), paamelding_id))
    logg(con, "faktura_plan_endret", {"paamelding_id": paamelding_id, "kurs_id": p["kurs_id"], "fra": p["betaling"], "til": ny_plan}, aktor=aktor)
    if p["status"] == "bekreftet":
        con.execute("UPDATE paamelding SET faktura_tidligst_dato=NULL, sveiper_kjort=0 WHERE id=?", (paamelding_id,))
    return True


def oppdater_sensitivt(con, paamelding_id: int, allergier: str | None, tilrettelegging: str | None,
                       aktor: str = "admin") -> bool:
    """Oppdaterer allergi/tilrettelegging. Innholdet logges ALDRI i hendelsesloggen, kun at det ble endret."""
    gammel = con.execute("SELECT * FROM sensitivt WHERE paamelding_id=?", (paamelding_id,)).fetchone()
    ny = ((allergier or "").strip() or None, (tilrettelegging or "").strip() or None)
    old = ((gammel["allergier"] if gammel else None), (gammel["tilrettelegging"] if gammel else None))
    if ny == old:
        return False
    if any(ny):
        con.execute(_SENSITIVT_UPSERT, (paamelding_id, *ny))
    else:
        con.execute("DELETE FROM sensitivt WHERE paamelding_id=?", (paamelding_id,))
    con.execute("UPDATE paamelding SET oppdatert=? WHERE id=?",
               (datetime.now().isoformat(timespec="seconds"), paamelding_id))
    logg(con, "sensitivt_endret", {"paamelding_id": paamelding_id}, aktor=aktor)
    return True


def _frigi_gammel_faktura_hold(con, paamelding_id: int) -> None:
    """Nullstiller en GAMMEL faktura_tidligst_dato ved overgang til bekreftet/venteliste (samme tankegang som meld_paa():
    en gammel fakturaplan skal aldri overleve inn i en ny fase av paameldingen - normal sveiper beregner ny plan fra dagens
    kursoppsett og dagens behandlingsdato). Roerer ALDRI en paamelding som allerede har ekte faktura eller faktura_forsok
    (ekstern oekonomisk aktivitet har skjedd/kan ha skjedd - datoen er da historikk). faktura_onskes_na roeres ikke."""
    con.execute(
        """UPDATE paamelding SET faktura_tidligst_dato=NULL
           WHERE id=? AND faktura_tidligst_dato IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM faktura f WHERE f.paamelding_id=paamelding.id)
             AND NOT EXISTS (SELECT 1 FROM faktura_forsok fo WHERE fo.paamelding_id=paamelding.id)""", (paamelding_id,))


# Påmeldingsstatus slik admin ser den (visningsstatus), i visningsrekkefølge. Kolonnen `status` har bare tre verdier
# (bekreftet, venteliste, avmeldt), og resten utledes av paameldingsstatus():
#   * Påmeldt og Ekstradeltaker er begge PÅ KURSET og har begge status='bekreftet'. Ekstradeltaker er en påmelding på kurset der
#     administrator har satt ekstradeltaker_ts (migrering 16): den tar ikke plass og teller ikke som påmeldt. Uten tidsstempelet er
#     den Påmeldt - den vanlige statusen.
#   * Avslått, Utgått og Forlatt er avmeldte påmeldinger med en egen dato (migrering 6 og 8).
# Høyst én av datoene kan være satt, og ekstradeltaker_ts bare på en påmelding som har plass: CHECK-reglene i skjemaet
# stopper alt annet.
#
# ORDENE SOM VISES ER DEFINERT HER OG BARE HER: filter, statuskolonne, deltakervindu, Logger, rapporter, eksporter og
# meldinger henter alle navnet herfra. Nøklene er visningsstatusene (paameldt, ekstradeltaker ...), aldri databaseverdien
# «bekreftet» - den heter ikke lenger noe som vises. Skal et ord endres, endres det på disse to linjene (enkeltall og flertall).
PAAMELDINGSSTATUSER = {"paameldt": "Påmeldt", "ekstradeltaker": "Ekstradeltaker", "venteliste": "Venteliste",
                       "avmeldt": "Avmeldt", "avslatt": "Avslått", "utgatt": "Utgått", "forlatt": "Forlatt"}
PAAMELDINGSSTATUSER_FLERTALL = {"paameldt": "Påmeldte", "ekstradeltaker": "Ekstradeltakere", "venteliste": "Venteliste",
                                "avmeldt": "Avmeldte", "avslatt": "Avslåtte", "utgatt": "Utgåtte", "forlatt": "Forlatte"}
# Visningsstatusene som er PÅ KURSET (databaseverdien er da status='bekreftet', DATABASEVERDI_PLASS): får påminnelser og kursbevis,
# kan sjekke inn og ser kurssiden (en ekstradeltaker bare for sine samlinger). Bare Påmeldt TAR EN PLASS og teller mot kapasiteten
# (antall_bekreftet); en ekstradeltaker tar ingen plass, får ikke bekreftelse eller faktura automatisk og teller ikke som påmeldt.
HAR_PLASS = ("paameldt", "ekstradeltaker")
DATABASEVERDI_PLASS = "bekreftet"
_STATUSDATO = {"avslatt": "avslatt_ts", "utgatt": "utgatt_ts", "forlatt": "forlatt_ts"}


def paameldingsstatus(p) -> str:
    """Visningsstatusen (en nøkkel i PAAMELDINGSSTATUSER) - den ENESTE stedet den utledes: 'avslatt', 'utgatt' eller
    'forlatt' når datoen for den er satt; 'ekstradeltaker' eller 'paameldt' når påmeldingen har plass (status
    'bekreftet') med eller uten ekstradeltaker_ts; ellers kolonnen status ('venteliste', 'avmeldt'). Radens SELECT må ta
    med tidsstemplene (avslatt_ts, utgatt_ts, forlatt_ts, ekstradeltaker_ts), ellers regnes de som ikke satt."""
    for status, kolonne in _STATUSDATO.items():
        try:
            if p[kolonne]:
                return status
        except (KeyError, IndexError):          # radens SELECT tok ikke med kolonnen
            continue
    if p["status"] != DATABASEVERDI_PLASS:
        return p["status"]
    try:
        return "ekstradeltaker" if p["ekstradeltaker_ts"] else "paameldt"
    except (KeyError, IndexError):              # radens SELECT tok ikke med kolonnen
        return "paameldt"


def statusnavn(verdi, *, flertall: bool = False) -> str:
    """Navnet på en statusverdi slik det vises. Tar en visningsstatus (paameldt, ekstradeltaker ...) eller databaseverdien
    'bekreftet' (= Påmeldt: databaseverdien sier ikke om personen er ekstradeltaker). Ukjente verdier returneres uendret."""
    if verdi == DATABASEVERDI_PLASS:
        verdi = "paameldt"
    return (PAAMELDINGSSTATUSER_FLERTALL if flertall else PAAMELDINGSSTATUSER).get(verdi, verdi)


def sql_visningsstatus(alias: str = "p") -> str:
    """SQL-uttrykket som gir samme visningsstatus som paameldingsstatus() (til CSV-eksport og rapporter). `alias` er et
    fast tabellalias i koden - aldri brukerinput."""
    a = alias
    return (f"CASE WHEN {a}.avslatt_ts IS NOT NULL THEN 'avslatt' WHEN {a}.utgatt_ts IS NOT NULL THEN 'utgatt' "
            f"WHEN {a}.forlatt_ts IS NOT NULL THEN 'forlatt' "
            f"WHEN {a}.status='bekreftet' AND {a}.ekstradeltaker_ts IS NOT NULL THEN 'ekstradeltaker' "
            f"WHEN {a}.status='bekreftet' THEN 'paameldt' ELSE {a}.status END")


def sett_paamelding_status(con, paamelding_id: int, ny_status: str, aktor: str = "admin", *,
                           tillat_overbooking: bool = False, samlinger=None) -> int | None:
    """Endrer påmeldingsstatus fra admin til en av PAAMELDINGSSTATUSER (databaseverdien 'bekreftet' tas som Påmeldt).
    Avslått, Utgått og Forlatt settes og fjernes i én UPDATE, så en påmelding aldri har mer enn én av dem (CHECK i
    skjemaet er en ekstra sperre).

    PÅMELDT tar en plass og teller mot kapasiteten. EKSTRADELTAKER er på kurset (utsendinger, innsjekk, kursside, kursbevis),
    men tar IKKE plass og teller ikke som fullverdig deltaker (antall_bekreftet). Derfor:
      * Påmeldt -> Ekstradeltaker frigjør en plass: første på ventelisten rykker opp etter de vanlige reglene (_rykk_opp),
        og personen blir på kurset.
      * Ekstradeltaker -> Påmeldt krever en ledig plass: er kurset fullt, avvises det uten tillat_overbooking, akkurat som
        Venteliste -> Påmeldt.
      * Venteliste, Avmeldt, Avslått, Utgått og Forlatt -> Ekstradeltaker er tillatt uten kapasitetssjekk, også på et fullt
        kurs (bare avlyst og avsluttet kurs avviser det).
      * Ekstradeltaker -> Venteliste, Avmeldt, Avslått, Utgått og Forlatt frigjør ingen plass (ingen opprykk).
    `samlinger` (bare for Ekstradeltaker): id-ene til samlingene personen deltar på; tom eller None = hele kurset. Utvalget
    kontrolleres før noe skrives, og ryddes når personen slutter å være ekstradeltaker eller forlater plassen.

    Ingen automatisk bekreftelse eller faktura for en ekstradeltaker (prisen er ikke kursets standardpris): den holdes tilbake
    (sveiper_utsatt=1) når bekreftelsen ikke er behandlet fra før (sveiper_kjort=0), og admin sender manuelt ved behov
    («Behandling holdt tilbake» i deltakervinduet). Er den behandlet, endres ingenting, og ingenting krediteres.

    Tilbake til Påmeldt (Ekstradeltaker -> Påmeldt) gjelder dagens regler for Påmeldt, men ingenting sendes eller faktureres
    automatisk hvis det alt er behandlet:
      * bekreftelsen er ikke behandlet (sveiper_kjort=0): holdet oppheves, og bekreftelse og faktura går nå som for alle andre;
      * bekreftelsen er behandlet, og alle fakturaene er laget (samlet: fakturaen finnes; per samling: alle delfakturaene), eller
        kurset faktureres ikke: ingenting endres, og ingenting krediteres;
      * bekreftelsen er sendt manuelt, men en faktura gjenstår (ingen faktura, eller bare noen av delfakturaene; en ekstradeltaker
        faktureres aldri automatisk, og administrator kan ha fakturert i Visma): INGEN faktura lages av seg selv, uansett om det er
        én faktura eller flere delfakturaer og om deltakeren alt har fått noen (brukerens beslutning 01.10.2026). Påmeldingen holdes
        tilbake igjen (sveiper_kjort=0, sveiper_utsatt=1), og administrator velger selv «Behandle fakturering nå» (bekreftelsen
        dedupliseres og sendes ikke på nytt; da lages alle forfalte delfakturaer som gjenstår, og resten følger morgenjobben som
        vanlig). Ellers kunne en faktura til standardpris blitt en dobbel faktura. De delfakturaene som er laget, står og krediteres
        ikke.
    Utvalget ryddes.

    Et avlyst eller avsluttet kurs avviser at NOEN nye kommer på kurset (Venteliste, Avmeldt, Avslått, Utgått og Forlatt -> Påmeldt
    eller Ekstradeltaker). Påmeldt <-> Ekstradeltaker legger ingen ny person til kurset og er bare en merkelapp (og utvalget): den
    virker derfor også etter kursslutt (rettelse av rapport og eksport), uten kapasitetssjekk, opprykk, bekreftelse eller faktura.

    Plassen og ventelisten følger ellers dagens regler: forlater en PÅMELDT plassen (til avmeldt, avslått, utgått, forlatt
    eller venteliste), rykker første på ventelisten opp når det faktisk er en ledig plass (_rykk_opp). Den admin nettopp satte
    på venteliste, velges aldri i samme operasjon. Et fullt kurs avviser at noen får en plass som Påmeldt - med mindre admin
    uttrykkelig har bekreftet at kurset skal overbookes (tillat_overbooking=True, bare fra deltakerlisten og deltakervinduet
    etter et eget spørsmål). Da loggføres det. Et avlyst eller avsluttet kurs avviser nye plasser uansett, og der rykker ingen
    opp. Kommer noen tilbake til en plass fra en annen status, er de Påmeldt - unntatt når admin velger Ekstradeltaker.
    Kurset låses (_laas_kurs) før plassene telles, så to samtidige endringer aldri begge får den siste plassen.

    Selve statusendringen sender aldri e-post og lager aldri faktura. Returnerer paamelding_id som bør kjøres gjennom
    sveiper.kjor() nå - den som fikk plass som Påmeldt, eller den som rykket opp fra ventelisten - ellers None. Den som settes på
    venteliste, får ventelistebeskjed av morgenjobben hvis den aldri er sendt (som før)."""
    kurs_av = con.execute("SELECT kurs_id FROM paamelding WHERE id=?", (paamelding_id,)).fetchone()
    if not kurs_av:
        raise Paameldingsfeil("Ukjent påmelding")
    _laas_kurs(con, kurs_av["kurs_id"])        # FØR påmeldingen og plassene leses: den andre samtidige endringen venter og ser resultatet
    p = con.execute("SELECT * FROM paamelding WHERE id=?", (paamelding_id,)).fetchone()
    if not p:
        raise Paameldingsfeil("Ukjent påmelding")
    if ny_status == DATABASEVERDI_PLASS:                   # databaseverdien tas som Påmeldt (den vanlige)
        ny_status = "paameldt"
    if ny_status not in PAAMELDINGSSTATUSER:
        raise Paameldingsfeil("Ugyldig statusendring.")
    gammel = paameldingsstatus(p)
    if ny_status == gammel:
        return None
    na = datetime.now().isoformat(timespec="seconds")
    endring = {"paamelding_id": paamelding_id, "fra": gammel, "til": ny_status}
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (p["kurs_id"],)).fetchone()
    lukket = kurs["status"] in ("avlyst", "avsluttet")

    if ny_status == "ekstradeltaker":
        if lukket and gammel not in HAR_PLASS:             # som meld_paa: ingen nye på kurset (og ingen bekreftelse/faktura)
            raise Paameldingsfeil(f"Kurset er {kurs['status']} – kan ikke melde på flere.")
        valgt = _kontroller_utvalg(con, p["kurs_id"], samlinger)          # FØR noe skrives
        # Holdt tilbake: ingen automatisk bekreftelse eller faktura. Er den allerede behandlet (sveiper_kjort=1), endres ingenting.
        holdt = 1 if (gammel not in HAR_PLASS or not p["sveiper_kjort"]) else p["sveiper_utsatt"]
        kjort = p["sveiper_kjort"] if gammel in HAR_PLASS else 0
        con.execute("UPDATE paamelding SET status='bekreftet', sveiper_kjort=?, sveiper_utsatt=?, avslatt_ts=NULL, utgatt_ts=NULL, "
                    "forlatt_ts=NULL, ekstradeltaker_ts=?, oppdatert=? WHERE id=?", (kjort, holdt, naa_utc(), na, paamelding_id))
        _lagre_utvalg(con, paamelding_id, valgt)
        if gammel not in HAR_PLASS:
            _frigi_gammel_faktura_hold(con, paamelding_id)
        # Påmeldt -> Ekstradeltaker: plassen blir ledig, og første på ventelisten rykker opp (personen selv velges ikke: den har plass)
        opprykket = _rykk_opp(con, p["kurs_id"]) if gammel == "paameldt" else None
        logg(con, "status_endret", {**endring, "hele_kurset": not valgt, "antall_samlinger": len(valgt)}, aktor=aktor)
        return opprykket

    if ny_status == "paameldt":
        if lukket and gammel != "ekstradeltaker":          # som meld_paa: ingen nye plasser (og ingen bekreftelse/faktura)
            raise Paameldingsfeil(f"Kurset er {kurs['status']} – kan ikke melde på flere.")
        if lukket:
            # Ekstradeltaker -> Påmeldt på et avlyst eller avsluttet kurs: ingen ny kommer på kurset, så bare merkelappen byttes (og
            # utvalget ryddes). Ingen kapasitetssjekk, opprykk, bekreftelse eller faktura. Er bekreftelsen ikke behandlet, holdes den
            # tilbake: et avsluttet kurs plukkes ellers opp av morgenjobbens gjenoppretting og ville sendt bekreftelse og faktura.
            con.execute("UPDATE paamelding SET ekstradeltaker_ts=NULL, oppdatert=?, "
                        "sveiper_utsatt=CASE WHEN sveiper_kjort=0 THEN 1 ELSE sveiper_utsatt END WHERE id=?", (na, paamelding_id))
            _ryd_ekstradeltaker_samlinger(con, paamelding_id)
            logg(con, "status_endret", endring, aktor=aktor)
            return None
        fullt = kurs["kapasitet"] is not None and antall_bekreftet(con, p["kurs_id"]) >= kurs["kapasitet"]
        if fullt and not tillat_overbooking:
            raise Paameldingsfeil("Kurset er fullt – kan ikke melde på flere uten å melde av noen først.")
        kjor_na = paamelding_id
        if gammel == "ekstradeltaker":
            # Tilbake til vanlig Påmeldt: se docstring. Bekreftelsen sendes aldri to ganger, og en faktura lages aldri av seg selv
            # når bekreftelsen alt er sendt manuelt.
            from . import sveiper   # importeres her: sveiper bruker db
            faktureres = sveiper.skal_faktureres({"fakturering": kurs["fakturering"], "pris_nok": kurs["pris_nok"], "status": "bekreftet"})
            # Gjenstår det en faktura (samlet: ingen faktura ennå; per samling: minst en delfaktura som ikke er laget)? Samme regel for én
            # faktura og for flere delfakturaer, også når deltakeren alt har fått noen: bare de som gjenstår, holdes tilbake.
            from . import rabatter
            gjenstaar = faktureres and sveiper.fakturering_gjenstaar(
                con, {**dict(p), "pris_nok": rabatter.deltakerpris(kurs["pris_nok"], p["rabatt_prosent"])})
            if not p["sveiper_kjort"]:                      # ikke behandlet: holdet oppheves, vanlige regler (morgenjobb og umiddelbar kjøring)
                kjort, utsatt = 0, 0
            elif not gjenstaar:                             # behandlet, og det er ingenting mer å lage: ingenting endres
                kjort, utsatt, kjor_na = 1, p["sveiper_utsatt"], None
            else:                                           # bekreftelsen sendt manuelt, faktura gjenstår: administrator bestemmer
                kjort, utsatt, kjor_na = 0, 1, None
            con.execute("UPDATE paamelding SET ekstradeltaker_ts=NULL, sveiper_kjort=?, sveiper_utsatt=?, oppdatert=? WHERE id=?",
                        (kjort, utsatt, na, paamelding_id))
            endring = {**endring, "holdt_tilbake": bool(utsatt)}
        else:
            # sveiper_utsatt nullstilles ogsaa: en administrativ bekreftelse er en bevisst handling som
            # skal fore til faktisk sending/fakturering naa - en tidligere "vent"-markering fra en manuell
            # registrering skal ikke stille denne handlingen ut av spill.
            con.execute("UPDATE paamelding SET status='bekreftet', sveiper_kjort=0, sveiper_utsatt=0, avslatt_ts=NULL, "
                        "utgatt_ts=NULL, forlatt_ts=NULL, ekstradeltaker_ts=NULL, oppdatert=? WHERE id=?", (na, paamelding_id))
            _frigi_gammel_faktura_hold(con, paamelding_id)
        _ryd_ekstradeltaker_samlinger(con, paamelding_id)
        if gammel == "ekstradeltaker" and kurs["kapasitet"] is not None and antall_bekreftet(con, p["kurs_id"]) >= kurs["kapasitet"]:
            # Siste plass er tatt igjen (Påmeldt -> Ekstradeltaker kan ha åpnet kurset via _rykk_opp). Alle andre veier til Påmeldt er som før.
            con.execute("UPDATE kurs SET status='full' WHERE id=? AND status='aapen'", (p["kurs_id"],))
        logg(con, "status_endret", {**endring, **({"over_kapasitet": kurs["kapasitet"]} if fullt else {})}, aktor=aktor)
        return kjor_na

    if ny_status == "venteliste":
        con.execute("UPDATE paamelding SET status='venteliste', sveiper_kjort=0, sveiper_utsatt=0, avslatt_ts=NULL, "
                    "utgatt_ts=NULL, forlatt_ts=NULL, ekstradeltaker_ts=NULL, oppdatert=? WHERE id=?", (na, paamelding_id))
        _ryd_ekstradeltaker_samlinger(con, paamelding_id)
        _frigi_gammel_faktura_hold(con, paamelding_id)
        # Fra en plass som Påmeldt blir den ledig: neste på ventelisten kan rykke opp - men aldri den admin nettopp flyttet
        # ned. En ekstradeltaker tok ingen plass, så da rykker ingen opp.
        opprykket = _rykk_opp(con, p["kurs_id"], unntatt=paamelding_id) if gammel == "paameldt" else None
        logg(con, "status_endret", endring, aktor=aktor)
        return opprykket

    # Avmeldt, avslått, utgått eller forlatt. En aktiv påmelding meldes av først (plassen og ventelisten som i dag;
    # meld_av nullstiller også ekstradeltaker_ts og utvalget, og en ekstradeltaker frigjør ingen plass).
    opprykket = meld_av(con, paamelding_id, aktor=aktor) if p["status"] in (DATABASEVERDI_PLASS, "venteliste") else None
    tid = naa_utc()
    con.execute("UPDATE paamelding SET avslatt_ts=?, utgatt_ts=?, forlatt_ts=?, oppdatert=? WHERE id=?",
                (tid if ny_status == "avslatt" else None, tid if ny_status == "utgatt" else None,
                 tid if ny_status == "forlatt" else None, na, paamelding_id))
    logg(con, "status_endret", endring, aktor=aktor)
    return opprykket


# ---------- admin: redigering av kurs (fase 4) ----------

# Pris og fakturaoppsett. Har kurset en oekonomisk binding (faktura, fakturaforsoek eller planlagt faktura), endres de bare naar
# administratoren uttrykkelig har bekreftet det (oppdater_kurs_felter med tillat_okonomi=True). Navnet er fra da de var helt laast.
KURS_LAASTE_FELT = ("pris_nok", "fakturering", "betaling", "faktura_dager_for")


def antall_faktura_for_kurs(con, kurs_id: int) -> int:
    return con.execute(
        "SELECT COUNT(*) FROM faktura f JOIN paamelding p ON p.id=f.paamelding_id WHERE p.kurs_id=?",
        (kurs_id,)).fetchone()[0]


def har_okonomisk_binding_for_kurs(con, kurs_id: int) -> bool:
    """True naar kursets oekonomifelt (KURS_LAASTE_FELT) er bundet: da endres de bare med uttrykkelig bekreftelse
    (oppdater_kurs_felter). Boolsk beslutning - antall_faktura_for_kurs() teller fortsatt bare fakturaer, og
    okonomisk_binding_oversikt() gir tallene til advarselen. Bundet naar minst ett av disse finnes:
      A. en ekte faktura
      B. en faktura_forsok-rad (reservert/feilet/ukjent) - ekstern oekonomisk aktivitet kan ha skjedd, ogsaa om
         deltakeren senere er avmeldt
      C. en AKTIV (bekreftet) paamelding med faktura_tidligst_dato (planlagt/utsatt faktura). En avmeldt paamelding med
         historisk hold-dato, uten faktura/forsok, laaser ikke kurset alene.
    """
    return con.execute(
        """SELECT EXISTS (SELECT 1 FROM faktura f JOIN paamelding p ON p.id=f.paamelding_id WHERE p.kurs_id=?)
               OR EXISTS (SELECT 1 FROM faktura_forsok fo JOIN paamelding p ON p.id=fo.paamelding_id WHERE p.kurs_id=?)
               OR EXISTS (SELECT 1 FROM paamelding WHERE kurs_id=? AND status='bekreftet'
                          AND faktura_tidligst_dato IS NOT NULL)""",
        (kurs_id, kurs_id, kurs_id)).fetchone()[0] == 1


def okonomisk_binding_oversikt(con, kurs_id: int) -> dict:
    """Det som binder kurset oekonomisk (se har_okonomisk_binding_for_kurs), som tall. Vises i advarselen i Oppsett og skrives til
    loggen naar pris eller fakturaoppsett endres. fakturaer: laget i Visma. forsok: fakturaforsoek uten faktura (reservert,
    feilet, ukjent). planlagt: paameldte som skal ha samlet faktura senere (utsatt) og ikke har faatt den ennaa."""
    forsok = con.execute("SELECT COUNT(*) FROM faktura_forsok fo JOIN paamelding p ON p.id=fo.paamelding_id WHERE p.kurs_id=?",
                         (kurs_id,)).fetchone()[0]
    planlagt = con.execute(
        """SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=? AND p.status='bekreftet' AND p.faktura_tidligst_dato IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM faktura f WHERE f.paamelding_id=p.id)""", (kurs_id,)).fetchone()[0]
    return {"fakturaer": antall_faktura_for_kurs(con, kurs_id), "forsok": forsok, "planlagt": planlagt}


def prisendring_feil(con, kurs_id: int, ny_pris: int, fakturering: str) -> str | None:
    """Feilmelding hvis den nye prisen ville gitt en faktura paa 0 kr, ellers None. Fakturaer som er laget endres aldri, saa en
    lavere pris er ellers uproblematisk. Men en deltaker som fakturaes per samling og alt har faatt fakturert like mye eller mer enn
    den nye prisen, mens det gjenstaar samlinger, ville faatt 0-kroners fakturaer (sveiper.delfakturaer_som_gjenstaar deler
    «prisen minus det som er fakturert» paa samlingene som gjenstaar). Gratis (0 kr) og kurs som ikke faktureres per person gir
    ingen flere fakturaer, og er alltid greit. Ren lesing."""
    if ny_pris <= 0 or fakturering != "person":
        return None
    from . import rabatter, sveiper   # importeres her: sveiper bruker db
    samlinger = sveiper.samlinger_til_fakturering(con, kurs_id)
    berort = 0
    for p in con.execute("""SELECT * FROM paamelding WHERE kurs_id=? AND status='bekreftet' AND ekstradeltaker_ts IS NULL
                            AND betaling='per_samling'""", (kurs_id,)).fetchall():
        gjenstaar = sveiper.delfakturaer_som_gjenstaar(
            con, {**dict(p), "pris_nok": rabatter.deltakerpris(ny_pris, p["rabatt_prosent"])}, samlinger)
        if any(belop <= 0 for *_, belop in gjenstaar):
            berort += 1
    if not berort:
        return None
    return (f"Prisen er for lav: {berort} {'deltaker har' if berort == 1 else 'deltakere har'} allerede fått fakturert så mye av "
            "kursavgiften at neste faktura per samling ville blitt på 0 kr. Sett en høyere pris.")


# Arrangør per kurs (kurs.merke, migrering 26): lagret verdi -> navn. «ipr» lagres som NULL.
KURSMERKER = {"ipr": "IPR", "terapiakademiet": "Terapiakademiet"}


def oppdater_kurs_felter(con, kurs_id: int, felter: dict, aktor: str = "admin", *, tillat_okonomi: bool = False) -> list[str]:
    """Oppdaterer kursfelt (ikke kapasitet/status, se egne funksjoner for dem).

    Saa snart kurset har en oekonomisk binding (se har_okonomisk_binding_for_kurs: faktura, faktura_forsok eller
    planlagt/utsatt faktura), blir de fakturarelaterte feltene (pris, fakturering, betaling, dager-for-faktura) STRIPPET her
    ogsaa - ikke bare skjult i UI - slik at de ikke kan endres via en direkte POST selv om skjemaet skulle tillate det.
    Unntaket er tillat_okonomi=True: det sier bare Oppsett, etter at administratoren har bekreftet at fakturaer som er laget ikke
    endres. Endringen gjelder da fakturaer som ikke er laget ennaa (motoren leser prisen og fakturaoppsettet naar fakturaen
    lages), og skrives til loggen som kurs_okonomi_endret med verdiene foer og etter og tallene fra okonomisk_binding_oversikt.
    En ny pris som ville gitt en faktura paa 0 kr avvises (prisendring_feil).
    """
    gammel = con.execute("SELECT * FROM kurs WHERE id=?", (kurs_id,)).fetchone()
    if not gammel:
        raise Paameldingsfeil("Ukjent kurs")
    if "faktura_dager_for" in felter:   # ugyldig verdi avvises FOER noe skrives - ogsaa naar feltet ellers ville blitt laast
        felter["faktura_dager_for"] = valider_faktura_dager_for(felter["faktura_dager_for"])
    bundet = har_okonomisk_binding_for_kurs(con, kurs_id)
    if bundet and not tillat_okonomi:
        felter = {k: v for k, v in felter.items() if k not in KURS_LAASTE_FELT}
    endret = [f for f, v in felter.items() if (gammel[f] if gammel[f] is not None else "") != (v if v is not None else "")]
    if not endret:
        return []
    if bundet and "pris_nok" in endret:
        feil = prisendring_feil(con, kurs_id, felter["pris_nok"], felter.get("fakturering", gammel["fakturering"]))
        if feil:
            raise Paameldingsfeil(feil)
    con.execute(f"UPDATE kurs SET {','.join(f'{f}=?' for f in felter)} WHERE id=?", [*felter.values(), kurs_id])
    logg(con, "kurs_endret", {"kurs_id": kurs_id, "felt": endret}, aktor=aktor)
    okonomi = [f for f in endret if f in KURS_LAASTE_FELT]
    if bundet and okonomi:
        logg(con, "kurs_okonomi_endret", {"kurs_id": kurs_id, "endringer": {f: [gammel[f], felter[f]] for f in okonomi},
                                          **okonomisk_binding_oversikt(con, kurs_id)}, aktor=aktor)
    return endret


def endre_kapasitet(con, kurs_id: int, ny_kapasitet: int | None, aktor: str = "admin") -> list[int]:
    """Endrer kapasitet. Okes den, rykkes venteliste automatisk opp saa langt det er plass, i samme
    rekkefolge som ved avmelding. Senkes den under antall påmeldte, fjernes INGEN automatisk -
    status settes bare til 'full' saa ingen nye slipper inn. Et avlyst kurs rykker aldri opp venteliste.

    Returnerer paamelding_id-ene som ble rykket opp - kalleren boer kjore disse gjennom sveiper.kjor().
    """
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kurs_id,)).fetchone()
    if not kurs:
        raise Paameldingsfeil("Ukjent kurs")
    if ny_kapasitet == kurs["kapasitet"]:
        return []
    con.execute("UPDATE kurs SET kapasitet=? WHERE id=?", (ny_kapasitet, kurs_id))
    logg(con, "kurs_endret", {"kurs_id": kurs_id, "felt": ["kapasitet"]}, aktor=aktor)

    opprykket = []
    if kurs["status"] != "avlyst":
        while ny_kapasitet is None or antall_bekreftet(con, kurs_id) < ny_kapasitet:
            neste = con.execute(
                "SELECT id FROM paamelding WHERE kurs_id=? AND status='venteliste' ORDER BY opprettet, id LIMIT 1",
                (kurs_id,)).fetchone()
            if not neste:
                break
            con.execute("UPDATE paamelding SET status='bekreftet', sveiper_kjort=0, oppdatert=? WHERE id=?",
                       (datetime.now().isoformat(timespec="seconds"), neste["id"]))
            _frigi_gammel_faktura_hold(con, neste["id"])
            logg(con, "flyttet_fra_venteliste", {"paamelding_id": neste["id"]}, aktor=aktor)
            opprykket.append(neste["id"])

    if kurs["status"] in ("aapen", "full"):
        na_fullt = ny_kapasitet is not None and antall_bekreftet(con, kurs_id) >= ny_kapasitet
        if na_fullt and kurs["status"] == "aapen":
            con.execute("UPDATE kurs SET status='full' WHERE id=?", (kurs_id,))
        elif not na_fullt and kurs["status"] == "full":
            con.execute("UPDATE kurs SET status='aapen' WHERE id=?", (kurs_id,))
    return opprykket


KURS_STATUS_OVERGANGER = {"utkast": ("aapen",), "aapen": ("utkast",)}


def sett_kurs_status(con, kurs_id: int, ny_status: str, aktor: str = "admin") -> None:
    """Kun utkast <-> aapen tilbys her. 'full'/'aktiv'/'avsluttet' styres utelukkende automatisk
    (kapasitet/dato), og 'avlyst' haandteres av avlys_kurs()/gjenaapne_kurs() med egen bekreftelse."""
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kurs_id,)).fetchone()
    if not kurs:
        raise Paameldingsfeil("Ukjent kurs")
    if ny_status not in KURS_STATUS_OVERGANGER.get(kurs["status"], ()):
        raise Paameldingsfeil("Ugyldig statusendring.")
    con.execute("UPDATE kurs SET status=? WHERE id=?", (ny_status, kurs_id))
    logg(con, "kurs_status_endret", {"kurs_id": kurs_id, "fra": kurs["status"], "til": ny_status}, aktor=aktor)


def avlys_kurs(con, kurs_id: int, aktor: str = "admin") -> None:
    """Setter kurset til avlyst. Dette alene stopper: nye paameldinger (meld_paa avviser status
    utenfor aapen/full/aktiv), nye fakturaer og paaminnelser/innkallinger (daglig.py plukker kun opp
    kurs med status aapen/full/aktiv), nytt Zoom-moete (samme grunn) og ventelisteopprykk (sjekket i
    endre_kapasitet og sett_paamelding_status). Eksisterende paameldinger roeres ikke - de er historikk.
    Fakturaer krediteres IKKE automatisk. Selve varselet til deltakerne sendes av den som kaller dette."""
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kurs_id,)).fetchone()
    if not kurs:
        raise Paameldingsfeil("Ukjent kurs")
    if kurs["status"] == "avlyst":
        raise Paameldingsfeil("Kurset er allerede avlyst.")
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kurs_id,))
    logg(con, "kurs_avlyst", {"kurs_id": kurs_id, "fra": kurs["status"]}, aktor=aktor)


def gjenaapne_kurs(con, kurs_id: int, aktor: str = "admin") -> None:
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kurs_id,)).fetchone()
    if not kurs:
        raise Paameldingsfeil("Ukjent kurs")
    if kurs["status"] != "avlyst":
        raise Paameldingsfeil("Kurset er ikke avlyst.")
    con.execute("UPDATE kurs SET status='aapen' WHERE id=?", (kurs_id,))
    logg(con, "kurs_gjenaapnet", {"kurs_id": kurs_id}, aktor=aktor)


_FAKTURAADRESSE = ("faktura_adresse", "faktura_postnr", "faktura_sted")


# Rabattfeltene på paamelding (migrering 32, kurs/rabatter.py) slik de er uten rabatt: ordinær pris, ingen sjekk
_INGEN_RABATT = {"priskategori": None, "rabatt_prosent": None, "rabatt_status": None, "psyflix_org": None, "psyflix_epost": None}


def meld_paa(con, kurs_id: int, *, epost: str, fornavn: str, etternavn: str, deltaker: dict | None = None,
             paamelding: dict | None = None, sensitivt: dict | None = None, idag: date | None = None,
             aktor: str | None = None, tillat_utkast: bool = False, ignorer_frist: bool = False,
             beskytt_eksisterende_felt: bool = False, svar: dict | None = None, ekstradeltaker: bool = False,
             samlinger=None) -> tuple[int, str]:
    """Registrer paamelding. Returnerer (paamelding_id, status). Setter venteliste naar kurset er fullt.

    `ekstradeltaker` og `samlinger` er kun for «Legg til deltaker» (administrator): en ekstradeltaker er på kurset, men tar ikke
    plass. Det gjøres INGEN kapasitetssjekk (også et fullt kurs gir da status 'bekreftet', aldri venteliste), påmeldingen
    holdes tilbake (sveiper_utsatt=1: ingen automatisk bekreftelse eller faktura) og får samlingsutvalget `samlinger` (tom/None =
    hele kurset; kontrolleres før noe skrives).

    `aktor`, `tillat_utkast` og `ignorer_frist` er kun ment for administrativ, manuell paamelding
    (fase 9) - offentlig paamelding/webhook/gruppepaamelding bruker aldri disse, og faar dermed
    uendret oppforsel. `avlyst` og `avsluttet` kan IKKE overstyres av noen - se KURS_STATUSVERDIER-
    bruken andre steder; det finnes bevisst ingen parameter for det.

    `beskytt_eksisterende_felt` sendes videre til finn_eller_opprett_deltaker() - se dens
    docstring. STANDARD er False (uendret oppforsel for alle eksisterende kall). Fase 10 sin
    CSV-import er eneste kaller som bruker True.

    `svar`: svarene paa kursets egne felt i skjemaet ({felt_id: tekst}, fra ekstrafelt.tolk_svar). Gis det (ogsaa
    tomt), ERSTATTER det paameldingens svar - ogsaa ved reaktivering. None (andre kallere) roerer ikke svarene.

    ADRESSEN: `deltaker` kan ha adresse, postnr og poststed (deltakerens private adresse - skjemaet, bedriftspaameldingen og
    «Legg til deltaker» krever dem alltid, webhook og CSV som standard (bare noedbremsen =0 slaar det av); det er kallerne som kontrollerer
    det, ikke denne funksjonen, og uten adresse er personen merket «Privat adresse mangler»). Betaler deltakeren selv (`betaler` er 'person', ogsaa
    uten angivelse) og kalleren ikke har gitt en egen fakturaadresse, KOPIERES personens adresse - slik den staar etter
    denne paameldingen - til paamelding.faktura_adresse/faktura_postnr/faktura_sted, saa fakturamotoren (sveiper/visma)
    leser den som foer. Kopieringen gjelder ALLE kurs, ogsaa gratiskurs (deltakerens adresse skal alltid legges til).
    Betaler firma, kopieres INGENTING: fakturaen gaar til firmaets adresse fra Enhetsregisteret, og systemet gaar aldri
    automatisk over til aa fakturere deltakerens private adresse.
    Kopien holdes i takt med personen av oppdater_deltaker (en rettelse fra administrator, se _foelg_fakturaadressen), og er
    den tom naar fakturaen lages, bruker sveiperen personens adresse (sveiper._fakturaadresse).
    """
    idag = idag or date.today()
    _laas_kurs(con, kurs_id)        # FØR kurset og plassene leses: to som melder seg på samtidig kan ikke begge få den siste plassen
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kurs_id,)).fetchone()
    if not kurs:
        raise Paameldingsfeil("Ukjent kurs")
    utvalg = _kontroller_utvalg(con, kurs_id, samlinger) if ekstradeltaker else []
    tillatte_statuser = ("aapen", "full", "aktiv") + (("utkast",) if tillat_utkast else ())
    if kurs["status"] not in tillatte_statuser:
        raise Paameldingsfeil("Kurset er ikke åpent for påmelding")
    if not ignorer_frist and kurs["paameldingsfrist"] and idag > date.fromisoformat(kurs["paameldingsfrist"]):
        raise Paameldingsfeil(f"Påmeldingsfristen ({kurs['paameldingsfrist']}) er passert.")

    deltaker_id = finn_eller_opprett_deltaker(
        con, epost, fornavn, etternavn, beskytt_eksisterende_felt=beskytt_eksisterende_felt, aktor=aktor,
        **(deltaker or {}))
    finnes = con.execute(
        "SELECT id, status, avslatt_ts FROM paamelding WHERE kurs_id=? AND deltaker_id=?", (kurs_id, deltaker_id)
    ).fetchone()
    if finnes and finnes["status"] != "avmeldt":
        raise Paameldingsfeil("Du er allerede påmeldt dette kurset")
    if finnes and finnes["avslatt_ts"]:     # et avslag står: bare admin kan gjenopprette det (statusendring på deltakersiden)
        raise Paameldingsfeil("Påmeldingen til dette kurset er ikke godkjent. Ta kontakt med kursadministrasjonen.")

    fullt = (kurs["kapasitet"] is not None and antall_bekreftet(con, kurs_id) >= kurs["kapasitet"]) and not ekstradeltaker
    status = "venteliste" if fullt else "bekreftet"
    na = datetime.now().isoformat(timespec="seconds")
    # sveiper_utsatt=0 er default her (ikke bare ved ny INSERT) fordi UPDATE-en under ved
    # REAKTIVERING ellers ville latt en gammel, avmeldt rad sin tidligere sveiper_utsatt-verdi
    # staa ubrukt igjen paa den nye paameldingen. Kaller (fase 9-adminrute) kan overstyre via
    # paamelding={"sveiper_utsatt": 1}.
    # faktura_onskes_na/faktura_tidligst_dato: samme begrunnelse som sveiper_utsatt - en NY registreringsrunde (ogsaa
    # reaktivering) skal aldri arve en gammel fakturabeslutning. Kaller kan overstyre faktura_onskes_na.
    # Rabatten (kurs/rabatter.py): samme begrunnelse - en ny registreringsrunde arver aldri en gammel pris. Kalleren setter den.
    felter = {"status": status, "samtykke_ts": na, "oppdatert": na, "sveiper_utsatt": 0,
              "faktura_onskes_na": 0, "faktura_tidligst_dato": None, **_INGEN_RABATT, **(paamelding or {})}
    # Ekstradeltaker: har plass uten å ta plass, og holdes tilbake (ingen automatisk bekreftelse eller faktura). Alle andre: nullstilt.
    felter["ekstradeltaker_ts"] = naa_utc() if ekstradeltaker else None
    if ekstradeltaker:
        felter["sveiper_utsatt"] = 1
    if (felter.get("betaler") or "person") == "person" and not any(felter.get(k) for k in _FAKTURAADRESSE):
        adr = con.execute("SELECT adresse, postnr, poststed FROM deltaker WHERE id=?", (deltaker_id,)).fetchone()
        felter |= {"faktura_adresse": adr["adresse"], "faktura_postnr": adr["postnr"], "faktura_sted": adr["poststed"]}
    # Betalingsmåte: deltakeren bestemmer bare når kurset er satt til 'deltaker_velger' og har flere samlinger. Med én
    # samling er det ikke noe å velge: én vanlig faktura, også om noen sender inn «per_samling».
    if kurs["betaling"] == "deltaker_velger":
        delt = felter.get("betaling") == "per_samling" and antall_samlinger(con, kurs_id) > 1
        felter["betaling"] = "per_samling" if delt else "samlet"
    else:
        felter["betaling"] = kurs["betaling"]

    if finnes:  # reaktiver tidligere avmelding - også en utgått eller forlatt påmelding (historikken står i hendelsesloggen)
        con.execute(
            f"UPDATE paamelding SET {','.join(f'{k}=?' for k in felter)}, sveiper_kjort=0, utgatt_ts=NULL, forlatt_ts=NULL "
            "WHERE id=?",
            [*felter.values(), finnes["id"]],
        )
        pid = finnes["id"]
        con.execute("DELETE FROM rabatt_bevis WHERE paamelding_id=?", (pid,))     # et gammelt studentbevis følger ikke med
    else:
        kolonner = ["kurs_id", "deltaker_id", *felter.keys()]
        pid = sett_inn(
            con, f"INSERT INTO paamelding ({','.join(kolonner)}) VALUES ({','.join('?' * len(kolonner))})",
            [kurs_id, deltaker_id, *felter.values()],
        )
    if finnes or utvalg:                    # ryddes også ved reaktivering: en gammel rad skal aldri følge med inn i en ny påmelding
        _lagre_utvalg(con, pid, utvalg)

    if sensitivt and any(sensitivt.values()):
        con.execute(_SENSITIVT_UPSERT, (pid, sensitivt.get("allergier"), sensitivt.get("tilrettelegging")))
    if svar is not None:
        lagre_svar(con, pid, svar)
    if fullt and kurs["status"] == "aapen":
        con.execute("UPDATE kurs SET status='full' WHERE id=?", (kurs_id,))
    hendelse = {"paamelding_id": pid, "kurs_id": kurs_id, "status": status}
    if ekstradeltaker:          # Logger skal vise «Registrert som ekstradeltaker» og hvor mye av kurset (bare tall, aldri navn)
        hendelse |= {"ekstradeltaker": True, "hele_kurset": not utvalg, "antall_samlinger": len(utvalg)}
    logg(con, "paamelding", hendelse, aktor=aktor or f"deltaker:{deltaker_id}")
    return pid, status


def _slett_forhaandsvisninger_med(con, epost: str) -> int:
    """CSV-importens forhaandsvisninger (import_forhaandsvisning) lagrer radene - navn, e-post og adresse - i opptil 30
    minutter, og ryddes ellers foerst av morgenjobben. Har en av radene personens e-post, slettes hele forhaandsvisningen
    (administrator laster opp filen paa nytt). En forhaandsvisning som ikke kan leses, kan ikke kontrolleres og slettes
    ogsaa - den er kortlevd uansett. Returnerer antall slettede forhaandsvisninger."""
    slettes = []
    for r in con.execute("SELECT token, rader_json FROM import_forhaandsvisning").fetchall():
        try:
            rader = json.loads(r["rader_json"])
        except ValueError:
            rader = None
        if not isinstance(rader, list) or any(
                isinstance(x, dict) and str(x.get("epost") or "").strip().lower() == epost.lower() for x in rader):
            slettes.append(r["token"])
    for token in slettes:
        con.execute("DELETE FROM import_forhaandsvisning WHERE token=?", (token,))
    return len(slettes)


ANONYM_FORNAVN, ANONYM_ETTERNAVN = "Anonymisert", "deltaker"
ANONYM_NAVN = fullt_navn(ANONYM_FORNAVN, ANONYM_ETTERNAVN)
ANONYM_DOMENE = "anonymisert.invalid"        # .invalid er reservert (RFC 2606) - kan aldri bli en ekte adresse


def anonymiser_deltaker(con, deltaker_id: int, aktor: str) -> dict:
    """Retten til sletting (GDPR art. 17). Fjerner personopplysningene om deltakeren i HELE databasen, men beholder
    anonyme rader (påmelding, oppmøte, faktura) slik at kurs-, økonomi- og regnskapstall fortsatt stemmer. Kalleren
    committer. Nekter hvis personen har aktive påmeldinger (bekreftet/venteliste på kurs som ikke er avsluttet/avlyst).

    Røres IKKE herfra (må gjøres manuelt, se GDPR.md): kundekort/faktura i Visma (regnskapsbilag, bokføringsloven),
    e-poster i kurs-postboksens «Sendte elementer», personlige mapper i SharePoint og Zoom-rapporter.
    Returnerer en PII-fri oppsummering (antall rader per område)."""
    d = con.execute("SELECT id, epost FROM deltaker WHERE id=?", (deltaker_id,)).fetchone()
    if not d:
        raise DeltakerFeil("Ukjent deltaker.")
    if d["epost"].lower().endswith("@" + ANONYM_DOMENE):
        raise DeltakerFeil("Deltakeren er allerede anonymisert.")
    aktive = con.execute(
        """SELECT COUNT(*) FROM paamelding p JOIN kurs k ON k.id=p.kurs_id WHERE p.deltaker_id=?
             AND p.status IN ('bekreftet','venteliste') AND k.status NOT IN ('avsluttet','avlyst')""",
        (deltaker_id,)).fetchone()[0]
    if aktive:
        raise DeltakerFeil("Personen har aktive påmeldinger. Meld av (eller vent til kurset er avsluttet) først.")
    gammel, ny = d["epost"], f"anonymisert-{deltaker_id}@{ANONYM_DOMENE}"
    ut = {}
    con.execute("""UPDATE deltaker SET navn=?, fornavn=?, etternavn=?, epost=?, telefon=NULL, arbeidssted=NULL,
                   yrkestittel=NULL, hpr_nr=NULL, adresse=NULL, postnr=NULL, poststed=NULL WHERE id=?""",
                (ANONYM_NAVN, ANONYM_FORNAVN, ANONYM_ETTERNAVN, ny, deltaker_id))
    ut["paameldinger"] = con.execute(
        """UPDATE paamelding SET faktura_epost=NULL, faktura_adresse=NULL, faktura_postnr=NULL, faktura_sted=NULL,
                  faktura_ref=NULL, faktura_kommentar=NULL, intern_kommentar=NULL, psyflix_org=NULL, psyflix_epost=NULL,
                  oppdatert=? WHERE deltaker_id=?""",
        (naa_utc(), deltaker_id)).rowcount
    ut["sensitivt"] = con.execute(
        "DELETE FROM sensitivt WHERE paamelding_id IN (SELECT id FROM paamelding WHERE deltaker_id=?)",
        (deltaker_id,)).rowcount
    ut["skjemasvar"] = con.execute(
        "DELETE FROM paamelding_svar WHERE paamelding_id IN (SELECT id FROM paamelding WHERE deltaker_id=?)",
        (deltaker_id,)).rowcount
    con.execute("DELETE FROM dokument_innhold WHERE dokument_id IN (SELECT id FROM dokument WHERE deltaker_id=?)",
                (deltaker_id,))
    ut["dokumenter"] = con.execute("DELETE FROM dokument WHERE deltaker_id=?", (deltaker_id,)).rowcount
    ut["innloggingslenker"] = con.execute("DELETE FROM innlogging_token WHERE deltaker_id=?", (deltaker_id,)).rowcount
    ut["utsendingslogg"] = con.execute("UPDATE utsending_logg SET mottaker=? WHERE mottaker=?", (ny, gammel)).rowcount
    # E-posthistorikken: kopiene til personen (for påmeldingene, og alt som ble sendt til adressen) og filene som ingen
    # annen e-post bruker. Vedleggsradene følger med (ON DELETE CASCADE).
    ut["epostkopier"] = con.execute(
        """DELETE FROM sendt_epost WHERE paamelding_id IN (SELECT id FROM paamelding WHERE deltaker_id=?)
             OR LOWER(til)=LOWER(?)""", (deltaker_id, gammel)).rowcount
    slett_ubrukte_epostfiler(con)
    # Rabattene: studentbevis som venter på godkjenning (Psyflix-opplysningene er tømt over)
    ut["rabattbevis"] = con.execute(
        "DELETE FROM rabatt_bevis WHERE paamelding_id IN (SELECT id FROM paamelding WHERE deltaker_id=?)",
        (deltaker_id,)).rowcount
    # Datakontrollen: «Det stemmer»-merkene for personen (bare hasher, men de hører til personen)
    ut["datakontroll"] = con.execute("DELETE FROM datakontroll_ok WHERE deltaker_id=?", (deltaker_id,)).rowcount
    # «Klar til sending» (kurs/godkjenning.py): e-poster til personen som venter på godkjenning eller er behandlet
    ut["epostkoe"] = con.execute(
        """DELETE FROM epost_godkjenning WHERE paamelding_id IN (SELECT id FROM paamelding WHERE deltaker_id=?)
             OR LOWER(mottaker)=LOWER(?)""", (deltaker_id, gammel)).rowcount
    ut["importforhandsvisninger"] = _slett_forhaandsvisninger_med(con, gammel)
    ut["firmarader"] = con.execute("UPDATE firmapaamelding_rad SET navn=?, epost=? WHERE LOWER(epost)=LOWER(?)",
                                   (ANONYM_NAVN, ny, gammel)).rowcount
    ut["firmakontakt"] = con.execute(
        """UPDATE firmapaamelding SET kontakt_navn=?, kontakt_fornavn=?, kontakt_etternavn=?, kontakt_epost=?,
                  kontakt_telefon=NULL WHERE LOWER(kontakt_epost)=LOWER(?)""",
        (ANONYM_NAVN, ANONYM_FORNAVN, ANONYM_ETTERNAVN, ny, gammel)).rowcount
    ut["henvendelser"] = con.execute(
        "UPDATE henvendelse SET epost=NULL, sporsmal='(slettet)' WHERE deltaker_id=? OR LOWER(epost)=LOWER(?)",
        (deltaker_id, gammel)).rowcount
    logg(con, "deltaker_anonymisert", {"deltaker_id": deltaker_id, **ut}, aktor=aktor)
    return ut


def avsla_paamelding(con, paamelding_id: int, aktor: str = "admin") -> int | None:
    """Fase 17: IPR avslår påmeldingen (f.eks. fordi opptakskravene ikke er oppfylt). Behandles som en avmelding - plassen
    frigjøres og første på ventelisten rykker opp (meld_av), ingen ny faktura, innkalling eller kursbevis - men merkes
    avslått, og deltakeren kan ikke melde seg på igjen selv (meld_paa). Admin kan gjenopprette med en statusendring.
    Returnerer paamelding_id som rykket opp fra venteliste (eller None). Fakturaer krediteres ikke automatisk."""
    p = con.execute("SELECT status FROM paamelding WHERE id=?", (paamelding_id,)).fetchone()
    if not p:
        raise Paameldingsfeil("Ukjent påmelding")
    if p["status"] not in ("bekreftet", "venteliste"):
        raise Paameldingsfeil("Bare påmeldte og de som står på venteliste kan avslås.")
    opprykket = meld_av(con, paamelding_id, aktor=aktor)
    con.execute("UPDATE paamelding SET avslatt_ts=? WHERE id=?", (naa_utc(), paamelding_id))
    logg(con, "paamelding_avslatt", {"paamelding_id": paamelding_id, "fra": p["status"]}, aktor=aktor)
    return opprykket


def meld_av(con, paamelding_id: int, aktor: str = "admin") -> int | None:
    """Avmelder og flytter forste paa ventelisten opp - men bare naar det da faktisk er en ledig plass. Etter at admin
    har overbooket kurset (sett_paamelding_status med tillat_overbooking), er kurset fortsatt fullt etter en avmelding,
    og da rykker ingen opp. Returnerer paamelding_id som ble flyttet opp."""
    p = con.execute("SELECT * FROM paamelding WHERE id=?", (paamelding_id,)).fetchone()
    na = datetime.now().isoformat(timespec="seconds")
    # Ekstradeltaker nullstilles: den som forlater plassen, er Påmeldt igjen hvis vedkommende får plass senere. Utvalget ryddes.
    con.execute("UPDATE paamelding SET status='avmeldt', ekstradeltaker_ts=NULL, oppdatert=? WHERE id=?", (na, paamelding_id))
    _ryd_ekstradeltaker_samlinger(con, paamelding_id)
    logg(con, "avmelding", {"paamelding_id": paamelding_id}, aktor=aktor)
    if p["status"] != "bekreftet" or p["ekstradeltaker_ts"]:     # en ekstradeltaker tok ingen plass: ingen plass blir ledig
        return None
    return _rykk_opp(con, p["kurs_id"])


def _rykk_opp(con, kurs_id: int, *, unntatt: int | None = None) -> int | None:
    """En bekreftet plass er nettopp blitt ledig: første på ventelisten får den - men bare når det faktisk er en ledig
    plass (etter overbooking kan kurset fortsatt være fullt, og da rykker ingen opp). `unntatt` er påmeldingen admin
    nettopp satte på venteliste; den velges aldri i samme operasjon. Er ingen på ventelisten, blir kurset åpent igjen.
    Et avlyst eller avsluttet kurs får aldri nye påmeldte (som meld_paa og endre_kapasitet): da rykker ingen opp, og
    ingen får bekreftelse eller faktura for et kurs som ikke holdes eller er ferdig.
    Returnerer paamelding_id som rykket opp."""
    kurs = con.execute("SELECT kapasitet, status FROM kurs WHERE id=?", (kurs_id,)).fetchone()
    if kurs["status"] in ("avlyst", "avsluttet"):
        return None
    kapasitet = kurs["kapasitet"]
    if kapasitet is not None and antall_bekreftet(con, kurs_id) >= kapasitet:
        return None                                  # fortsatt fullt (overbooket): kurset forblir «full»
    neste = con.execute(
        "SELECT id FROM paamelding WHERE kurs_id=? AND status='venteliste'" + (" AND id!=?" if unntatt else "")
        + " ORDER BY opprettet, id LIMIT 1", (kurs_id, unntatt) if unntatt else (kurs_id,),
    ).fetchone()
    if neste:
        # sveiper_utsatt nullstilles av samme grunn som i sett_paamelding_status(): et automatisk
        # opprykk skal faktisk fore til bekreftelse/fakturering, ikke stille forbli utsatt.
        na = datetime.now().isoformat(timespec="seconds")
        con.execute("UPDATE paamelding SET status='bekreftet', sveiper_kjort=0, sveiper_utsatt=0, oppdatert=? WHERE id=?",
                   (na, neste["id"]))
        _frigi_gammel_faktura_hold(con, neste["id"])
        logg(con, "flyttet_fra_venteliste", {"paamelding_id": neste["id"]})
        return neste["id"]
    con.execute("UPDATE kurs SET status='aapen' WHERE id=? AND status='full'", (kurs_id,))
    return None


# ---------- bedriftspaamelding (fase 7) ----------

def _innsendingsnokkel(kurs_id: int, kontakt_epost: str, deltaker_eposter: list[str]) -> str:
    """Stabil, INNHOLDSBASERT nokkel (ikke tilfeldig) - identisk gjeninnsending (dobbeltklikk/refresh)
    gir samme nokkel, slik at den gjenkjennes i stedet for aa behandles paa nytt."""
    unike = ",".join(sorted({e.strip().lower() for e in deltaker_eposter}))
    grunnlag = f"{kurs_id}:{kontakt_epost.strip().lower()}:{unike}"
    return hashlib.sha256(grunnlag.encode()).hexdigest()


def finn_eller_opprett_firmapaamelding(con, kurs_id: int, kontakt: dict, deltaker_eposter: list[str]) -> tuple[sqlite3.Row, bool]:
    """Oppretter en bedriftspaamelding, eller gjenkjenner en identisk gjeninnsending.

    Committer med en gang ved oppretting - det er det som gjor gjenkjenningen paalitelig ogsaa ved to
    tilnaermet samtidige forsok (det andre forsoket treffer da UNIQUE-indeksen og faar samme rad tilbake
    i stedet for aa opprette en ny). Returnerer (rad, True hvis nyopprettet / False hvis den fantes fra for -
    kalleren skal da IKKE behandle deltakerne paa nytt eller sende flere e-poster).
    """
    nokkel = _innsendingsnokkel(kurs_id, kontakt["epost"], deltaker_eposter)
    finnes = con.execute("SELECT * FROM firmapaamelding WHERE innsendingsnokkel=?", (nokkel,)).fetchone()
    if finnes:
        return finnes, False
    try:
        ny_id = sett_inn(
            con, """INSERT INTO firmapaamelding (kurs_id, innsendingsnokkel, kvittering_token, kontakt_navn, kontakt_fornavn,
               kontakt_etternavn, kontakt_epost, kontakt_telefon, firmanavn, org_nr, faktura_ref, faktura_adresse,
               faktura_postnr, faktura_sted, ehf)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (kurs_id, nokkel, secrets.token_urlsafe(32), fullt_navn(kontakt["fornavn"], kontakt["etternavn"]),
             kontakt["fornavn"], kontakt["etternavn"], kontakt["epost"], kontakt.get("telefon"),
             kontakt["firmanavn"], kontakt.get("org_nr"), kontakt.get("faktura_ref"), kontakt.get("faktura_adresse"),
             kontakt.get("faktura_postnr"), kontakt.get("faktura_sted"), 1 if kontakt.get("ehf") else 0))
        con.commit()
    except IntegritetsFeil:
        con.rollback()
        return con.execute("SELECT * FROM firmapaamelding WHERE innsendingsnokkel=?", (nokkel,)).fetchone(), False
    return con.execute("SELECT * FROM firmapaamelding WHERE id=?", (ny_id,)).fetchone(), True


def registrer_firmapaamelding_rad(con, firmapaamelding_id: int, navn: str, epost: str,
                                  paamelding_id: int | None, feilmelding: str | None) -> None:
    con.execute(
        "INSERT INTO firmapaamelding_rad (firmapaamelding_id, navn, epost, paamelding_id, feilmelding) VALUES (?,?,?,?,?)",
        (firmapaamelding_id, navn, epost, paamelding_id, feilmelding))


# ---------- utsending (dedup) ----------

def allerede_sendt(con, nokkel: str, mottaker: str, type_: str) -> bool:
    """True kun naar sendingen faktisk er bekreftet sendt - en reservert/feilet/ukjent rad teller ikke."""
    return con.execute(
        "SELECT 1 FROM utsending_logg WHERE nokkel=? AND mottaker=? AND type=? AND status='sendt'",
        (nokkel, mottaker, type_)
    ).fetchone() is not None


def marker_sendt(con, nokkel: str, mottaker: str, type_: str) -> None:
    """Registrerer en fullfort sending direkte (status faar kolonnens standardverdi 'sendt').

    For kallere som IKKE trenger den atomiske reserver-foerst-flyten under - typisk fordi de ikke
    gjor noe risikabelt eksternt kall selv (zoomimport-dedup i daglig.py). All e-post gaar gjennom
    claim-motoren (Kjoring.send_ferdigrendret_en_gang), ogsaa manuelle utsendelser.
    """
    con.execute(
        "INSERT INTO utsending_logg (nokkel, mottaker, type) VALUES (?,?,?) ON CONFLICT DO NOTHING",
        (nokkel, mottaker, type_))


def reserver_sending(con, nokkel: str, mottaker: str, type_: str) -> bool:
    """Atomisk claim av et NYTT sendeforsok. True hvis akkurat dette kallet vant claimen.

    Brukes av Kjoring.send_en_gang() FOR et evt. langt eksternt sendekall, slik at to samtidige
    kjoringer (f.eks. daglig jobb + manuell fase 9/11-behandling) aldri kan sende samme e-post
    to ganger - kun én av dem kan vinne INSERT-en.
    """
    cur = con.execute(
        "INSERT INTO utsending_logg (nokkel, mottaker, type, status) VALUES (?,?,?,'reservert') ON CONFLICT DO NOTHING",
        (nokkel, mottaker, type_))
    return cur.rowcount > 0


def reserver_sending_pa_nytt(con, nokkel: str, mottaker: str, type_: str) -> bool:
    """Atomisk claim av RETRY etter en kjent, trygg feil. True hvis akkurat dette kallet vant claimen."""
    cur = con.execute(
        """UPDATE utsending_logg SET status='reservert', sendt_ts=?
           WHERE nokkel=? AND mottaker=? AND type=? AND status='feilet'""",
        (naa_utc(), nokkel, mottaker, type_))
    return cur.rowcount > 0


def sett_sendt(con, nokkel: str, mottaker: str, type_: str) -> None:
    con.execute(
        "UPDATE utsending_logg SET status='sendt', sendt_ts=? WHERE nokkel=? AND mottaker=? AND type=?",
        (naa_utc(), nokkel, mottaker, type_))


def sett_sending_feilet(con, nokkel: str, mottaker: str, type_: str) -> None:
    con.execute(
        "UPDATE utsending_logg SET status='feilet' WHERE nokkel=? AND mottaker=? AND type=?",
        (nokkel, mottaker, type_))


def sett_sending_ukjent(con, nokkel: str, mottaker: str, type_: str) -> None:
    con.execute(
        "UPDATE utsending_logg SET status='ukjent' WHERE nokkel=? AND mottaker=? AND type=?",
        (nokkel, mottaker, type_))


# ---------- e-posthistorikk: uforanderlige kopier av sendte e-poster (se schema.sql, sendt_epost) ----------

def lagre_epostfil(con, innhold: bytes, mimetype: str) -> str:
    """Lagrer et bilde/vedlegg én gang per innhold og returnerer fingeravtrykket (sha256). Filen endres aldri."""
    sha = hashlib.sha256(innhold).hexdigest()
    con.execute("""INSERT INTO epost_fil (sha256, mimetype, storrelse, innhold, opprettet) VALUES (?,?,?,?,?)
                   ON CONFLICT (sha256) DO NOTHING""",
                (sha, mimetype, len(innhold), base64.b64encode(innhold).decode("ascii"), naa_utc()))
    return sha


def lagre_epostkopi(con, *, nokkel: str, type_: str, til: str, fra: str, sendt_av: str, emne: str, html: str,
                    paamelding_id: int | None = None, kurs_id: int | None = None, mal: str | None = None,
                    kopi: str | None = None, vedlegg=()) -> int:
    """Lagrer den uforanderlige kopien av en e-post FØR den sendes (status 'sender'), med bilder og vedlegg (objekter med
    filnavn, mimetype, innhold og cid - se epost.Vedlegg). Kalleren committer, sammen med reservasjonen i utsending_logg
    (Kjoring.send_ferdigrendret_en_gang): enten finnes både reservasjonen og kopien, eller ingen av dem."""
    if kurs_id is None and paamelding_id is not None:
        rad = con.execute("SELECT kurs_id FROM paamelding WHERE id=?", (paamelding_id,)).fetchone()
        kurs_id = rad["kurs_id"] if rad else None
    kopi_id = sett_inn(
        con, """INSERT INTO sendt_epost (paamelding_id, kurs_id, nokkel, type, mal, til, kopi, fra, sendt_av, emne, html,
                                         status, opprettet) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'sender', ?)""",
        (paamelding_id, kurs_id, nokkel, type_, mal, til, kopi, fra, sendt_av, emne, html, naa_utc()))
    for nr, v in enumerate(vedlegg, start=1):
        sha = lagre_epostfil(con, v.innhold, v.mimetype)
        con.execute("INSERT INTO sendt_epost_vedlegg (sendt_epost_id, nr, sha256, filnavn, innebygd_cid) VALUES (?,?,?,?,?)",
                    (kopi_id, nr, sha, v.filnavn, v.cid))
    return kopi_id


def sett_epostkopi_status(con, kopi_id: int, status: str, *, feilmelding: str | None = None) -> None:
    """Bare statusen endres - innholdet i kopien er det som ble sendt, og røres aldri. feilmelding er en sikker
    feiltekst (feil.sikker_feiltekst), aldri personopplysninger."""
    if status not in ("sendt", "feilet", "ukjent"):
        raise ValueError(f"Ugyldig status for e-postkopi: {status}")
    con.execute("""UPDATE sendt_epost SET status=?, feilmelding=COALESCE(?, feilmelding),
                          sendt_ts=CASE WHEN ?='sendt' THEN ? ELSE sendt_ts END WHERE id=?""",
                (status, feilmelding, status, naa_utc(), kopi_id))


def epostkopi_vedlegg(con, kopi_id: int) -> list:
    """Filene som hørte til en sendt e-post (bilder i teksten og vedlegg), med innholdet."""
    return con.execute(
        """SELECT v.nr, v.filnavn, v.innebygd_cid, f.mimetype, f.storrelse, f.innhold, f.sha256
           FROM sendt_epost_vedlegg v JOIN epost_fil f ON f.sha256=v.sha256 WHERE v.sendt_epost_id=? ORDER BY v.nr""",
        (kopi_id,)).fetchall()


def slett_ubrukte_epostfiler(con) -> int:
    """Fjerner filer som ingen sendt e-post bruker lenger (etter sletting av kopier)."""
    return con.execute("DELETE FROM epost_fil WHERE sha256 NOT IN (SELECT sha256 FROM sendt_epost_vedlegg)").rowcount


# ---------- faktura: forsok/claim (trinn 2.5) ----------
# Se schema.sql for hvorfor dette er en EGEN tabell og ikke en ny status/kolonne paa `faktura`.

def reserver_faktura(con, paamelding_id: int, kursdag_id: int | None) -> bool:
    """Atomisk claim av et NYTT Visma-forsok. True hvis akkurat dette kallet vant claimen."""
    na = datetime.now().isoformat(timespec="seconds")
    cur = con.execute(
        """INSERT INTO faktura_forsok (paamelding_id, kursdag_id, status, opprettet) VALUES (?,?,'reservert',?)
           ON CONFLICT DO NOTHING""",
        (paamelding_id, kursdag_id, na))
    return cur.rowcount > 0


def reserver_faktura_pa_nytt(con, paamelding_id: int, kursdag_id: int | None) -> bool:
    """Atomisk claim av RETRY etter en kjent, trygg feil. True hvis akkurat dette kallet vant claimen."""
    na = datetime.now().isoformat(timespec="seconds")
    cur = con.execute(
        """UPDATE faktura_forsok SET status='reservert', opprettet=?
           WHERE paamelding_id=? AND COALESCE(kursdag_id,0)=? AND status='feilet'""",
        (na, paamelding_id, kursdag_id or 0))
    return cur.rowcount > 0


def fjern_faktura_forsok(con, paamelding_id: int, kursdag_id: int | None) -> None:
    """Kalles etter en vellykket Visma-opprettelse - den ekte `faktura`-raden er naa den varige markoren."""
    con.execute(
        "DELETE FROM faktura_forsok WHERE paamelding_id=? AND COALESCE(kursdag_id,0)=?",
        (paamelding_id, kursdag_id or 0))


def sett_faktura_forsok_feilet(con, paamelding_id: int, kursdag_id: int | None, feilmelding: str) -> None:
    con.execute(
        "UPDATE faktura_forsok SET status='feilet', feilmelding=? WHERE paamelding_id=? AND COALESCE(kursdag_id,0)=?",
        (feilmelding, paamelding_id, kursdag_id or 0))


def sett_faktura_forsok_ukjent(con, paamelding_id: int, kursdag_id: int | None, feilmelding: str) -> None:
    con.execute(
        "UPDATE faktura_forsok SET status='ukjent', feilmelding=? WHERE paamelding_id=? AND COALESCE(kursdag_id,0)=?",
        (feilmelding, paamelding_id, kursdag_id or 0))


def faktura_forsok_rad(con, paamelding_id: int, kursdag_id: int | None):
    return con.execute(
        "SELECT * FROM faktura_forsok WHERE paamelding_id=? AND COALESCE(kursdag_id,0)=?",
        (paamelding_id, kursdag_id or 0)).fetchone()


# ---------- uavklarte operasjoner: felles definisjon for motor og forside ----------
# En 'reservert' rad er et forsok som pagar ELLER som ble avbrutt midt i (prosessen doede). Etter denne
# tiden regnes den som trolig forlatt og krever manuell kontroll. 'ukjent' krever alltid kontroll.
# 'feilet' teller IKKE - det er en kjent, trygg feil som (i fremtiden) haandteres automatisk.
# Merk klokkene: utsending_logg.sendt_ts er SQLite-tid (UTC), faktura_forsok.opprettet er Python-tid
# (lokal). Hver tabell sammenlignes derfor mot sin egen klokke - aldri paa tvers.
UAVKLART_GRENSE_MIN = 5


def _lokal_gammel_grense_iso() -> str:
    """Grensetidspunkt (lokal Python-tid, samme format som faktura_forsok.opprettet)."""
    return (datetime.now() - timedelta(minutes=UAVKLART_GRENSE_MIN)).isoformat(timespec="seconds")


def forsok_er_gammel(rad) -> bool:
    """True naar en faktura_forsok-rad har staatt uavklart lenger enn UAVKLART_GRENSE_MIN."""
    alder = datetime.now() - datetime.fromisoformat(rad["opprettet"])
    return alder.total_seconds() > UAVKLART_GRENSE_MIN * 60


def uavklarte_operasjoner(con) -> dict:
    """Levende telling (fra naavaerende tilstand, ikke fra hendelseslogg) av uavklarte operasjoner
    som krever manuell kontroll: e-post og faktura hver for seg, og totalt."""
    epost = con.execute(
        """SELECT COUNT(*) FROM utsending_logg
           WHERE status='ukjent' OR (status='reservert' AND sendt_ts < ?)""",
        (utc_minutter_siden(UAVKLART_GRENSE_MIN),)).fetchone()[0]
    gammel_grense = _lokal_gammel_grense_iso()
    faktura = con.execute(
        """SELECT COUNT(*) FROM faktura_forsok
           WHERE status='ukjent' OR (status='reservert' AND opprettet < ?)""", (gammel_grense,)).fetchone()[0]
    return {"epost": epost, "faktura": faktura, "totalt": epost + faktura}


def uavklarte_eposter(con) -> list:
    """E-poster med uavklart utfall (samme definisjon som uavklarte_operasjoner), eldste foerst."""
    return con.execute(
        """SELECT nokkel, mottaker, type, status, sendt_ts FROM utsending_logg
           WHERE status='ukjent' OR (status='reservert' AND sendt_ts < ?) ORDER BY sendt_ts, nokkel, type""",
        (utc_minutter_siden(UAVKLART_GRENSE_MIN),)).fetchall()


def uavklarte_fakturaer(con) -> list:
    """Fakturaforsoek med uavklart utfall, med det admin trenger for aa kontrollere i Visma."""
    return con.execute(
        """SELECT f.id, f.paamelding_id, f.kursdag_id, f.status, f.feilmelding, f.opprettet,
                  d.navn, p.kurs_id, k.kursnr, k.navn AS kursnavn, k.pris_nok, kd.dato AS kursdag_dato
           FROM faktura_forsok f JOIN paamelding p ON p.id=f.paamelding_id JOIN deltaker d ON d.id=p.deltaker_id
           JOIN kurs k ON k.id=p.kurs_id LEFT JOIN kursdag kd ON kd.id=f.kursdag_id
           WHERE f.status='ukjent' OR (f.status='reservert' AND f.opprettet < ?) ORDER BY f.opprettet, f.id""",
        (_lokal_gammel_grense_iso(),)).fetchall()


def avklar_epost(con, nokkel: str, mottaker: str, type_: str, *, sendt: bool, aktor: str) -> bool:
    """Admin har kontrollert en uavklart e-post (Sendte elementer). sendt=True -> 'sendt'; sendt=False -> 'feilet'
    (proeves igjen neste gang samme utsending kjoeres). Atomisk: False hvis raden ikke (lenger) er uavklart.
    Hendelsesloggen faar aldri mottakeradressen."""
    utfall = "sendt" if sendt else "feilet"
    cur = con.execute(
        """UPDATE utsending_logg SET status=? WHERE nokkel=? AND mottaker=? AND type=?
             AND (status='ukjent' OR (status='reservert' AND sendt_ts < ?))""",
        (utfall, nokkel, mottaker, type_, utc_minutter_siden(UAVKLART_GRENSE_MIN)))
    if cur.rowcount != 1:
        return False
    kopi = con.execute(
        """SELECT id FROM sendt_epost WHERE nokkel=? AND LOWER(til)=LOWER(?) AND type=? AND status IN ('sender','ukjent')
           ORDER BY id DESC LIMIT 1""", (nokkel, mottaker, type_)).fetchone()
    if kopi:                            # e-posthistorikken viser samme utfall som admin har kontrollert
        sett_epostkopi_status(con, kopi["id"], utfall)
    logg(con, "uavklart_epost_avklart", {"nokkel": nokkel, "type": type_, "utfall": utfall}, aktor=aktor)
    return True


def avklar_faktura(con, forsok_id: int, *, fakturert: bool, aktor: str, faktura_nr: str | None = None,
                   belop_nok: int | None = None, visma_id: str | None = None) -> bool:
    """Admin har kontrollert et uavklart fakturaforsoek i Visma.
      fakturert=True  -> fakturaen FINNES i Visma: registreres som faktura (status sendt), forsoeket fjernes.
      fakturert=False -> fakturaen finnes IKKE i Visma: forsoeket blir 'feilet' og proeves igjen ved neste kjoering.
    Atomisk og idempotent: False hvis forsoeket ikke (lenger) er uavklart. Den unike indeksen paa faktura hindrer
    uansett to fakturaer for samme paamelding/samling (IntegritetsFeil)."""
    rad = con.execute(
        """SELECT * FROM faktura_forsok WHERE id=? AND (status='ukjent' OR (status='reservert' AND opprettet < ?))""",
        (forsok_id, _lokal_gammel_grense_iso())).fetchone()
    if not rad:
        return False
    detaljer = {"paamelding_id": rad["paamelding_id"], "kursdag_id": rad["kursdag_id"],
                "utfall": "fakturert" if fakturert else "ikke_fakturert"}
    if fakturert:
        con.execute("""INSERT INTO faktura (paamelding_id, kursdag_id, visma_id, faktura_nr, belop_nok, status)
                       VALUES (?,?,?,?,?, 'sendt')""", (rad["paamelding_id"], rad["kursdag_id"], visma_id, faktura_nr, belop_nok))
        con.execute("DELETE FROM faktura_forsok WHERE id=?", (forsok_id,))
        detaljer["faktura_nr"] = faktura_nr
    else:
        con.execute("UPDATE faktura_forsok SET status='feilet' WHERE id=?", (forsok_id,))
    logg(con, "uavklart_faktura_avklart", detaljer, aktor=aktor)
    return True


def uavklart_status_for_paamelding(con, kurs_id: int, epost: str, paamelding_id: int, epost_type: str) -> str | None:
    """Uavklart e-post-/fakturaforsok for akkurat denne paameldingens BEHANDLING:
    e-postdelen gjelder KUN `epost_type` ('bekreftelse' for bekreftet, 'venteliste' for venteliste). Nokkelen
    kurs:<id> deles av mange e-posttyper (ukefor, dagfor-*, kursbevis, avlysning ...) - en uavklart melding av en
    ANNEN type sier ingenting om denne behandlingen. Fakturadelen er per paamelding_id. (Forsidens
    uavklarte_operasjoner() teller derimot ALLE typer - det er en global driftsoversikt.)
      'ukjent' - minst ett forsok er 'ukjent', eller 'reservert' og gammelt (trolig forlatt) -> krever kontroll
      'pagar'  - kun ferske 'reservert' forsok (en annen prosess jobber trolig med den akkurat naa)
      None     - ingen uavklarte forsok.
    Samme staleness-definisjon (UAVKLART_GRENSE_MIN) og tidsbaser som uavklarte_operasjoner()."""
    ukjent = con.execute(
        """SELECT 1 FROM utsending_logg WHERE nokkel=? AND mottaker=? AND type=?
                  AND (status='ukjent' OR (status='reservert' AND sendt_ts < ?))
           UNION ALL
           SELECT 1 FROM faktura_forsok WHERE paamelding_id=?
                  AND (status='ukjent' OR (status='reservert' AND opprettet < ?)) LIMIT 1""",
        (f"kurs:{kurs_id}", epost, epost_type, utc_minutter_siden(UAVKLART_GRENSE_MIN), paamelding_id,
         _lokal_gammel_grense_iso())
    ).fetchone() is not None
    if ukjent:
        return "ukjent"
    pagar = con.execute(
        """SELECT 1 FROM utsending_logg WHERE nokkel=? AND mottaker=? AND type=? AND status='reservert'
           UNION ALL
           SELECT 1 FROM faktura_forsok WHERE paamelding_id=? AND status='reservert' LIMIT 1""",
        (f"kurs:{kurs_id}", epost, epost_type, paamelding_id)).fetchone() is not None
    return "pagar" if pagar else None


# ---------- admin: manuell e-post (fase 5) ----------

def opprett_admin_utsending(con, kurs_id: int, emne: str, tekst: str, mottaker_paamelding_ider: list[int],
                            sendt_av_admin_id: int | None, signatur_html: str | None = None,
                            nokkel: str | None = None) -> tuple[int, str]:
    """Forbereder en manuell utsendelse (kalles ved forhaandsvisning) - sender IKKE noe selv.

    Lager en unik, stabil nokkel som skal folge med til selve sendingen (se Kjoring.send_admin_utsending),
    slik at et dobbeltklikk/refresh paa "Send" gjenbruker samme nokkel og dedupliseres riktig.
    Mottakerne er allerede validert av kalleren (tilhorer kurset) foer denne kalles.
    `nokkel` gis av kalleren naar skjemaet selv baerer nokkelen (e-post i deltakervinduet sendes uten forhaandsvisning).
    """
    nokkel = nokkel or f"adhoc:{kurs_id}:{secrets.token_hex(8)}"
    utsending_id = sett_inn(
        con, """INSERT INTO admin_utsending (nokkel, kurs_id, emne, tekst, sendt_av_admin_id, signatur_html)
                VALUES (?,?,?,?,?,?)""", (nokkel, kurs_id, emne, tekst, sendt_av_admin_id, signatur_html))
    con.executemany(
        "INSERT INTO admin_utsending_mottaker (utsending_id, paamelding_id) VALUES (?,?)",
        [(utsending_id, pid) for pid in mottaker_paamelding_ider])
    return utsending_id, nokkel


def admin_utsending_mottakere(con, utsending_id: int) -> list[sqlite3.Row]:
    """Den FASTSATTE mottakerlisten for en utsendelse - uavhengig av hva som evt. postes inn senere."""
    return con.execute(
        """SELECT p.id, d.navn, d.fornavn, d.epost FROM admin_utsending_mottaker m
           JOIN paamelding p ON p.id=m.paamelding_id JOIN deltaker d ON d.id=p.deltaker_id
           WHERE m.utsending_id=?""", (utsending_id,)).fetchall()


# ---------- oppmote ----------

def registrer_oppmote(con, paamelding_id: int, kursdag_id: int, kilde: str, minutter: int | None = None) -> bool:
    """True hvis ny registrering, False hvis allerede registrert."""
    cur = con.execute(
        "INSERT INTO oppmote (paamelding_id, kursdag_id, kilde, minutter) VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
        (paamelding_id, kursdag_id, kilde, minutter),
    )
    return cur.rowcount == 1


def iso(d: date) -> str:
    return d.isoformat()


def alfanumerisk(s: str) -> str:
    return "".join(c for c in s.upper() if c in string.ascii_uppercase + string.digits)


# ---------- redigerbare maltekster (fase 12B) ----------
# Lagrer KUN tekst. Vet ingenting om {koder}, Jinja eller gyldighet - det gjor kurs/maltekster.py. Committer ikke.

def hent_maltekst(con, mal: str, felt: str) -> str | None:
    """Overstyringen for (mal, felt), eller None hvis ingen finnes (-> standardtekst)."""
    rad = con.execute("SELECT tekst FROM mal_tekst WHERE mal=? AND felt=?", (mal, felt)).fetchone()
    return rad["tekst"] if rad else None


def hent_overstyringer_for_mal(con, mal: str) -> dict:
    """Alle lagrede overstyringer for en mal som {felt: tekst} (ingen rader -> tom dict)."""
    return {r["felt"]: r["tekst"] for r in con.execute("SELECT felt, tekst FROM mal_tekst WHERE mal=?", (mal,))}


def sett_maltekst(con, mal: str, felt: str, tekst: str, aktor: str = "system") -> None:
    """Lagrer/erstatter overstyringen og logger mal_endret med mal, felt og aktor - ALDRI teksten."""
    na = datetime.now().isoformat(timespec="seconds")
    con.execute(
        """INSERT INTO mal_tekst (mal, felt, tekst, oppdatert, oppdatert_av) VALUES (?,?,?,?,?)
           ON CONFLICT (mal, felt) DO UPDATE SET tekst=excluded.tekst, oppdatert=excluded.oppdatert,
                                                 oppdatert_av=excluded.oppdatert_av""",
        (mal, felt, tekst, na, aktor))
    logg(con, "mal_endret", {"mal": mal, "felt": felt}, aktor=aktor)


def slett_maltekst(con, mal: str, felt: str, aktor: str = "system") -> bool:
    """Fjerner overstyringen. Logger mal_tilbakestilt (mal, felt, aktor) KUN hvis en rad faktisk fantes. True = fantes."""
    cur = con.execute("DELETE FROM mal_tekst WHERE mal=? AND felt=?", (mal, felt))
    if cur.rowcount > 0:
        logg(con, "mal_tilbakestilt", {"mal": mal, "felt": felt}, aktor=aktor)
    return cur.rowcount > 0


# ---------- paameldingsskjema: per-kurs overstyringer (fase 12C2) ----------
# Validering/normalisering eies av kurs/skjemafelt.py (ren modul) og skjer FOER skriving. Her er kun SQL. Kolonnenavn
# kommer ALLTID fra skjemafelt.EGENSKAPER_REKKEFOLGE (whitelist), aldri fra input. Committer ikke. Loggen faar kun
# kurs_id, felt og egenskapsnavn - aldri label/hjelpetekst.

_SKJEMA_KOLONNER = skjemafelt.EGENSKAPER_REKKEFOLGE


def _skjema_db_verdi(verdi):
    return int(verdi) if isinstance(verdi, bool) else verdi


def hent_skjemaoverstyringer(con, kurs_id: int) -> "skjemafelt.Leseresultat":
    """Gyldige overstyringer for kurset + PII-frie advarsler for ignorerte rader/egenskaper (se skjemafelt.les_overstyringer).

    En REELL lesefeil (DatabaseFeil) blir skjemafelt.SkjemaLesefeil - ALDRI et tomt resultat, siden «ingen overstyringer»
    ville latt f.eks. et obligatorisk eller skjult felt stille falle tilbake til standard."""
    try:
        with isolert(con, "skjema_les"):     # en lesefeil skal ikke avbryte kallerens transaksjon (PostgreSQL)
            rader = [dict(r) for r in con.execute(
                f"SELECT felt, {','.join(_SKJEMA_KOLONNER)} FROM kurs_skjemafelt WHERE kurs_id=? ORDER BY felt",
                (kurs_id,))]
    except DatabaseFeil as e:
        raise skjemafelt.SkjemaLesefeil(kurs_id) from e
    return skjemafelt.les_overstyringer(rader)


def lagre_skjemafelt(con, kurs_id: int, felt: str, egenskaper: dict, aktor: str = "system") -> bool:
    """Setter den KOMPLETTE overstyringen for ett felt (utelatt/None = standard). Validerer foer skriving
    (SkjemafeltFeil -> ingenting skrives). Lagrer kun avvik; blir alt standard, slettes raden. Skriver/logger kun ved
    reell endring (da settes oppdatert/oppdatert_av). True = noe ble endret."""
    ny = skjemafelt.normaliser_overstyring(felt, egenskaper)
    if ny.er_tom:
        return tilbakestill_skjemafelt(con, kurs_id, felt, aktor=aktor)
    verdier = {k: _skjema_db_verdi(getattr(ny, k)) for k in _SKJEMA_KOLONNER}
    gammel = con.execute(f"SELECT {','.join(_SKJEMA_KOLONNER)} FROM kurs_skjemafelt WHERE kurs_id=? AND felt=?",
                         (kurs_id, felt)).fetchone()
    if gammel and all(type(gammel[k]) is type(v) and gammel[k] == v for k, v in verdier.items()):
        return False
    na = datetime.now().isoformat(timespec="seconds")
    con.execute(
        f"""INSERT INTO kurs_skjemafelt (kurs_id, felt, {','.join(_SKJEMA_KOLONNER)}, oppdatert, oppdatert_av)
            VALUES (?,?,{','.join('?' * len(_SKJEMA_KOLONNER))},?,?)
            ON CONFLICT (kurs_id, felt) DO UPDATE SET {','.join(f'{k}=excluded.{k}' for k in _SKJEMA_KOLONNER)},
                                                    oppdatert=excluded.oppdatert, oppdatert_av=excluded.oppdatert_av""",
        (kurs_id, felt, *verdier.values(), na, aktor))
    logg(con, "skjemafelt_endret", {"kurs_id": kurs_id, "felt": felt, "egenskaper": list(ny.satte_egenskaper())},
         aktor=aktor)
    return True


def tilbakestill_skjemafelt(con, kurs_id: int, felt: str, aktor: str = "system") -> bool:
    """Fjerner overstyringen for ett KJENT felt (-> kodet standard). Ogsaa for et laast felt, slik at en korrupt rad kan
    ryddes. Ukjent felt -> SkjemafeltFeil. Logger kun hvis en rad fantes. True = en rad ble slettet."""
    if not isinstance(felt, str) or felt not in skjemafelt.REGISTER:
        raise skjemafelt.SkjemafeltFeil(skjemafelt.UKJENT_FELT, None, None, "Ukjent skjemafelt.")
    cur = con.execute("DELETE FROM kurs_skjemafelt WHERE kurs_id=? AND felt=?", (kurs_id, felt))
    if cur.rowcount > 0:
        logg(con, "skjemafelt_tilbakestilt", {"kurs_id": kurs_id, "felt": felt}, aktor=aktor)
    return cur.rowcount > 0


def tilbakestill_skjema(con, kurs_id: int, aktor: str = "system") -> int:
    """Fjerner ALLE skjemaoverstyringer for kurset (ogsaa ukjente/korrupte rader). Returnerer antall slettede rader."""
    cur = con.execute("DELETE FROM kurs_skjemafelt WHERE kurs_id=?", (kurs_id,))
    if cur.rowcount > 0:
        logg(con, "skjema_tilbakestilt", {"kurs_id": kurs_id, "antall": cur.rowcount}, aktor=aktor)
    return cur.rowcount


# ---------- paameldingsskjema: kursets egne felt og svarene (skjemabyggeren, migrering 9) ----------
# Validering eies av kurs/ekstrafelt.py (ren modul) og skjer FOER skriving. Kolonnenavn kommer ALLTID fra
# _EKSTRA_KOLONNER (whitelist), aldri fra input. Committer ikke. Hendelsesloggen faar kun kurs_id, felt_id, type og
# navnene paa endrede egenskaper - aldri feltnavn, hjelpetekst, svaralternativer eller svar.

_EKSTRA_KOLONNER = ("type", "label", "hjelpetekst", "valg", "obligatorisk", "synlig", "plassering", "rekkefolge",
                    "vis_naar_felt", "vis_naar_verdi")


def _ekstra_db_verdier(verdier: dict, rekkefolge: int) -> dict:
    """Normaliserte verdier (ekstrafelt.normaliser) -> kolonneverdier: valg som JSON-liste (NULL uten valg), 0/1."""
    return {"type": verdier["type"], "label": verdier["label"], "hjelpetekst": verdier["hjelpetekst"],
            "valg": json.dumps(list(verdier["valg"]), ensure_ascii=False) if verdier["valg"] else None,
            "obligatorisk": int(verdier["obligatorisk"]), "synlig": int(verdier["synlig"]),
            "plassering": verdier["plassering"], "rekkefolge": int(rekkefolge),
            "vis_naar_felt": verdier["vis_naar_felt"], "vis_naar_verdi": verdier["vis_naar_verdi"]}


def hent_ekstrafelt(con, kurs_id: int) -> "ekstrafelt.Leseresultat":
    """Kursets egne felt, lest defensivt (ekstrafelt.les). En REELL lesefeil blir skjemafelt.SkjemaLesefeil - aldri et
    tomt resultat (da ville et obligatorisk felt stille forsvunnet fra skjemaet)."""
    try:
        with isolert(con, "ekstrafelt_les"):     # en lesefeil skal ikke avbryte kallerens transaksjon (PostgreSQL)
            rader = [dict(r) for r in con.execute(
                f"SELECT id, {','.join(_EKSTRA_KOLONNER)} FROM kurs_ekstrafelt WHERE kurs_id=? ORDER BY id",
                (kurs_id,))]
    except DatabaseFeil as e:
        raise skjemafelt.SkjemaLesefeil(kurs_id) from e
    return ekstrafelt.les(rader)


def antall_svar_per_ekstrafelt(con, kurs_id: int) -> dict:
    """{felt_id: antall paameldinger med svar} for kursets egne felt (felt uten svar er ikke med)."""
    return {r["felt_id"]: r["antall"] for r in con.execute(
        """SELECT s.felt_id, COUNT(*) AS antall FROM paamelding_svar s JOIN kurs_ekstrafelt e ON e.id=s.felt_id
           WHERE e.kurs_id=? GROUP BY s.felt_id""", (kurs_id,))}


def opprett_ekstrafelt(con, kurs_id: int, data: dict, rekkefolge: int, aktor: str = "system") -> int:
    """Nytt eget felt paa kurset. Validerer foer skriving (EkstrafeltFeil -> ingenting skrives) - ogsaa en ev.
    betingelse mot kursets andre felt. Returnerer id."""
    verdier = ekstrafelt.normaliser(data)
    antall = con.execute("SELECT COUNT(*) FROM kurs_ekstrafelt WHERE kurs_id=?", (kurs_id,)).fetchone()[0]
    if antall >= ekstrafelt.MAKS_FELT:
        raise ekstrafelt.EkstrafeltFeil(ekstrafelt.FOR_MANGE, None,
                                        f"Et kurs kan ha maks {ekstrafelt.MAKS_FELT} egne felt.")
    if verdier["vis_naar_felt"] is not None:
        nytt = ekstrafelt.som_felt(0, verdier, rekkefolge)
        feil = ekstrafelt.betingelsesfeil(nytt, {**hent_ekstrafelt(con, kurs_id).per_id(), 0: nytt})
        if feil:
            raise ekstrafelt.EkstrafeltFeil(ekstrafelt.UGYLDIG_BETINGELSE, "vis_naar", feil)
    kol = _ekstra_db_verdier(verdier, rekkefolge)
    felt_id = sett_inn(
        con, f"""INSERT INTO kurs_ekstrafelt (kurs_id, {','.join(kol)}, oppdatert, oppdatert_av)
                 VALUES (?,{','.join('?' * len(kol))},?,?)""",
        (kurs_id, *kol.values(), datetime.now().isoformat(timespec="seconds"), aktor))
    logg(con, "ekstrafelt_opprettet", {"kurs_id": kurs_id, "felt_id": felt_id, "type": verdier["type"]}, aktor=aktor)
    return felt_id


EPOSTDELING = "deling_epost"      # kurs_ekstrafelt.rolle for kursets spørsmål «Deling av e-post» (migrering 27)


def epostdeling_felt(con, kurs_id: int) -> int | None:
    """Id-en til kursets spørsmål om deling av e-post, eller None."""
    rad = con.execute("SELECT id FROM kurs_ekstrafelt WHERE kurs_id=? AND rolle=?", (kurs_id, EPOSTDELING)).fetchone()
    return rad["id"] if rad else None


def sikre_epostdeling(con, kurs_id: int, aktor: str = "system") -> int | None:
    """Sørger for at kurset har spørsmålet «Deling av e-post» (Ja/Nei, påkrevd) i påmeldingsskjemaet (Camilla 05.10.2026:
    alle påmeldingsskjema). Finnes det, skjer ingenting. Et felt med samme navn og type (f.eks. kopiert fra et annet kurs)
    merkes i stedet for å lage et nytt. Returnerer id-en, eller None når kurset alt har maks antall egne felt."""
    felt_id = epostdeling_felt(con, kurs_id)
    if felt_id:
        return felt_id
    mal = ekstrafelt.MALER["deling_epost"][1]
    rad = con.execute("SELECT id FROM kurs_ekstrafelt WHERE kurs_id=? AND type=? AND label=? ORDER BY id LIMIT 1",
                      (kurs_id, mal["type"], mal["label"])).fetchone()
    if rad:
        felt_id = rad["id"]
    else:
        if con.execute("SELECT COUNT(*) FROM kurs_ekstrafelt WHERE kurs_id=?", (kurs_id,)).fetchone()[0] >= ekstrafelt.MAKS_FELT:
            return None
        # Standardfeltenes plass teller bare under «Om deg»; spørsmålet står «til slutt», så overstyringene trengs ikke
        overstyringer = hent_skjemaoverstyringer(con, kurs_id).overstyringer if mal["plassering"] == skjemafelt.OM_DEG else {}
        plass = skjemafelt.neste_plass(overstyringer, hent_ekstrafelt(con, kurs_id).felt, mal["plassering"])
        felt_id = opprett_ekstrafelt(con, kurs_id, dict(mal), plass, aktor=aktor)
    con.execute("UPDATE kurs_ekstrafelt SET rolle=? WHERE id=?", (EPOSTDELING, felt_id))
    return felt_id


def har_svar(con, felt_id: int) -> bool:
    return con.execute("SELECT 1 FROM paamelding_svar WHERE felt_id=? LIMIT 1", (felt_id,)).fetchone() is not None


def oppdater_ekstrafelt(con, kurs_id: int, felt_id: int, verdier: dict, rekkefolge: int, aktor: str = "system") -> bool:
    """Setter HELE feltet: normaliserte verdier (ekstrafelt.normaliser) og plass. Kalleren har kontrollert
    betingelsene mot hele settet (ekstrafelt.kontroller_betingelser). Typen kan ikke endres naar feltet har svar.
    Skriver og logger kun ved reell endring. True = endret."""
    gammel = con.execute(f"SELECT {','.join(_EKSTRA_KOLONNER)} FROM kurs_ekstrafelt WHERE id=? AND kurs_id=?",
                         (felt_id, kurs_id)).fetchone()
    if not gammel:
        raise ekstrafelt.EkstrafeltFeil(ekstrafelt.UKJENT_FELT, None, "Feltet finnes ikke lenger.", felt_id)
    ny = _ekstra_db_verdier(verdier, rekkefolge)
    endret = [k for k in _EKSTRA_KOLONNER if gammel[k] != ny[k]]
    if not endret:
        return False
    if "type" in endret and har_svar(con, felt_id):
        raise ekstrafelt.EkstrafeltFeil(ekstrafelt.HAR_SVAR, "type",
                                        f"«{verdier['label']}» har allerede svar, så typen kan ikke endres.", felt_id)
    con.execute(f"""UPDATE kurs_ekstrafelt SET {','.join(f'{k}=?' for k in _EKSTRA_KOLONNER)}, oppdatert=?,
                    oppdatert_av=? WHERE id=? AND kurs_id=?""",
                (*[ny[k] for k in _EKSTRA_KOLONNER], datetime.now().isoformat(timespec="seconds"), aktor, felt_id,
                 kurs_id))
    logg(con, "ekstrafelt_endret", {"kurs_id": kurs_id, "felt_id": felt_id, "egenskaper": endret}, aktor=aktor)
    return True


def slett_ekstrafelt(con, kurs_id: int, felt_id: int, aktor: str = "system") -> None:
    """Sletter et eget felt. Nekter (EkstrafeltFeil) naar feltet har svar (skjul det i stedet) eller naar et annet felt
    vises etter svaret paa det."""
    if not con.execute("SELECT 1 FROM kurs_ekstrafelt WHERE id=? AND kurs_id=?", (felt_id, kurs_id)).fetchone():
        raise ekstrafelt.EkstrafeltFeil(ekstrafelt.UKJENT_FELT, None, "Feltet finnes ikke lenger.", felt_id)
    antall = con.execute("SELECT COUNT(*) FROM paamelding_svar WHERE felt_id=?", (felt_id,)).fetchone()[0]
    if antall:
        raise ekstrafelt.EkstrafeltFeil(
            ekstrafelt.HAR_SVAR, None, f"Feltet har svar fra {antall} {'deltaker' if antall == 1 else 'deltakere'} og "
            "kan ikke slettes. Skjul det i stedet (ta bort haken under «Vis»).", felt_id)
    avhengig = con.execute("SELECT label FROM kurs_ekstrafelt WHERE kurs_id=? AND vis_naar_felt=? ORDER BY id LIMIT 1",
                           (kurs_id, f"{skjemafelt.EKSTRA_REF}{felt_id}")).fetchone()
    if avhengig:
        raise ekstrafelt.EkstrafeltFeil(
            ekstrafelt.STYRER_ANDRE, None, f"«{avhengig['label']}» vises bare etter svaret på dette feltet. Endre "
            "«Vis bare når» for det feltet først.", felt_id)
    con.execute("DELETE FROM kurs_ekstrafelt WHERE id=? AND kurs_id=?", (felt_id, kurs_id))
    logg(con, "ekstrafelt_slettet", {"kurs_id": kurs_id, "felt_id": felt_id}, aktor=aktor)


def kopier_ekstrafelt(con, kurs_id: int, plan: list, aktor: str = "system") -> int:
    """Kopierer egne felt til et NYTT kurs (kursduplisering). `plan`: [(gammel_id, normaliserte verdier, rekkefolge)].
    Felt uten betingelse lages foerst, saa en betingelse som viser til et kopiert felt kan peke til kopien."""
    nye = {}
    for gammel_id, verdier, rekkefolge in sorted(plan, key=lambda p: (p[1]["vis_naar_felt"] is not None, p[0])):
        v = dict(verdier)
        if v["vis_naar_felt"] and v["vis_naar_felt"].startswith(skjemafelt.EKSTRA_REF):
            v["vis_naar_felt"] = f"{skjemafelt.EKSTRA_REF}{nye[int(v['vis_naar_felt'][len(skjemafelt.EKSTRA_REF):])]}"
        nye[gammel_id] = opprett_ekstrafelt(con, kurs_id, v, rekkefolge, aktor=aktor)
    return len(nye)


def lagre_svar(con, paamelding_id: int, svar: dict) -> None:
    """Erstatter paameldingens svar paa kursets egne felt ({felt_id: tekst}). Et felt som ikke hoerer til samme kurs som
    paameldingen, lagres aldri. Innholdet logges aldri."""
    con.execute("DELETE FROM paamelding_svar WHERE paamelding_id=?", (paamelding_id,))
    for felt_id, verdi in sorted(svar.items()):
        con.execute("""INSERT INTO paamelding_svar (paamelding_id, felt_id, verdi)
                       SELECT p.id, e.id, ? FROM paamelding p JOIN kurs_ekstrafelt e ON e.kurs_id=p.kurs_id
                       WHERE p.id=? AND e.id=?""", (verdi, paamelding_id, felt_id))


def hent_svar(con, paamelding_id: int) -> dict:
    """{felt_id: lagret tekst} for paameldingen."""
    return {r["felt_id"]: r["verdi"] for r in con.execute(
        "SELECT felt_id, verdi FROM paamelding_svar WHERE paamelding_id=?", (paamelding_id,))}


def hent_svar_for_kurs(con, kurs_id: int) -> dict:
    """{paamelding_id: {felt_id: lagret tekst}} for alle paameldinger paa kurset."""
    ut = {}
    for r in con.execute("""SELECT s.paamelding_id, s.felt_id, s.verdi FROM paamelding_svar s
                            JOIN paamelding p ON p.id=s.paamelding_id WHERE p.kurs_id=?""", (kurs_id,)):
        ut.setdefault(r["paamelding_id"], {})[r["felt_id"]] = r["verdi"]
    return ut
