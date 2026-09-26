"""Databasetilgang. SQLite (standard: én fil, ingen server) eller PostgreSQL naar DATABASE_URL er satt.

Backend-valg (se koble()):
  * DATABASE_URL tom (lokal Windows-sandkasse, tester): SQLite i config.DB_STI - noyaktig som foer.
  * DATABASE_URL satt (drift, f.eks. Azure Database for PostgreSQL): PostgreSQL via psycopg, gjennom et tynt adapterlag
    (_PgTilkobling) som gir resten av koden SAMME grensesnitt som sqlite3: con.execute("... ? ...", params),
    rad["navn"] og rad[0], cursor.rowcount/fetchone/fetchall, commit/rollback, og db.DatabaseFeil/IntegritetsFeil.
psycopg importeres KUN naar PostgreSQL faktisk brukes.
"""
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

from . import config, skjemafelt

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


def opprett_kurs(con, *, kode: str, navn: str, datoer: list[str], aktor: str = "system", **felter) -> int:
    """Oppretter et kurs med et NYTT, permanent kursnummer (kurs.kursnr) fra telleren - kalleren kan aldri velge nummeret.
    `kode` er nettadresse-delen i offentlige lenker."""
    if "faktura_dager_for" in felter:   # foer noen skriving; utelatt felt beholder schema-defaulten (14)
        felter["faktura_dager_for"] = valider_faktura_dager_for(felter["faktura_dager_for"])
    felter.pop("kursnr", None)          # tildeles ALLTID her - ogsaa ved duplisering faar kopien nytt nummer
    kolonner = ["kursnr", "kode", "navn", *felter.keys()]
    kurs_id = sett_inn(
        con, f"INSERT INTO kurs ({','.join(kolonner)}) VALUES ({','.join('?' * len(kolonner))})",
        [neste_teller(con, "kursnr"), kode, navn, *felter.values()],
    )
    for d in datoer:
        con.execute(
            "INSERT INTO kursdag (kurs_id, dato, innsjekk_token, innsjekk_kode) VALUES (?,?,?,?)",
            (kurs_id, d, secrets.token_urlsafe(24), ny_kode()),
        )
    logg(con, "kurs_opprettet", {"kurs_id": kurs_id, "kode": kode,
                                 "kursnr": con.execute("SELECT kursnr FROM kurs WHERE id=?", (kurs_id,)).fetchone()[0]},
         aktor=aktor)
    return kurs_id


def kursdager(con, kurs_id: int) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM kursdag WHERE kurs_id=? ORDER BY dato", (kurs_id,)).fetchall()


def antall_bekreftet(con, kurs_id: int) -> int:
    return con.execute(
        "SELECT COUNT(*) FROM paamelding WHERE kurs_id=? AND status='bekreftet'", (kurs_id,)
    ).fetchone()[0]


# ---------- deltakere / paamelding ----------

def fullt_navn(fornavn: str | None, etternavn: str | None) -> str:
    """Fullt navn slik det lagres i `navn`/`kontakt_navn` og vises: «fornavn etternavn» (en tom del utelates). ENESTE sted
    navnet settes sammen - kursbevis, lister, fakturaer, sok og Zoom-oppmote leser `navn` som foer."""
    return " ".join(d for d in ((fornavn or "").strip(), (etternavn or "").strip()) if d)


def finn_eller_opprett_deltaker(con, epost: str, fornavn: str, etternavn: str, *, beskytt_eksisterende_felt: bool = False,
                                **felter) -> int:
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

def oppdater_deltaker(con, deltaker_id: int, felter: dict, aktor: str = "admin") -> list[str]:
    """Oppdaterer person-opplysninger. Disse deles paa tvers av alle kurs personen er meldt paa,
    saa paamelding.oppdatert settes for ALLE deres paameldinger, ikke bare den man sto paa.

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
    return endret


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


def sett_paamelding_status(con, paamelding_id: int, ny_status: str, aktor: str = "admin") -> int | None:
    """Endrer paameldingsstatus fra admin. Bruker eksisterende meld_av() for avmelding, slik at
    ventelisteopprykk skjer riktig. Bekreftet -> venteliste tilbys bevisst ikke (bruk avmelding).

    Returnerer paamelding_id som boer kjores gjennom sveiper.kjor() naa (nylig bekreftet), ellers None.
    """
    p = con.execute("SELECT * FROM paamelding WHERE id=?", (paamelding_id,)).fetchone()
    if not p:
        raise Paameldingsfeil("Ukjent påmelding")
    gammel_status = p["status"]
    if ny_status == gammel_status:
        return None
    na = datetime.now().isoformat(timespec="seconds")

    if ny_status == "avmeldt":
        opprykket = meld_av(con, paamelding_id, aktor=aktor)
        logg(con, "status_endret", {"paamelding_id": paamelding_id, "fra": gammel_status, "til": "avmeldt"}, aktor=aktor)
        return opprykket

    if ny_status == "bekreftet":
        if gammel_status == "bekreftet":
            raise Paameldingsfeil("Ugyldig statusendring.")
        kurs = con.execute("SELECT * FROM kurs WHERE id=?", (p["kurs_id"],)).fetchone()
        if kurs["status"] == "avlyst":
            raise Paameldingsfeil("Kurset er avlyst – kan ikke bekrefte flere.")
        if kurs["kapasitet"] is not None and antall_bekreftet(con, p["kurs_id"]) >= kurs["kapasitet"]:
            raise Paameldingsfeil("Kurset er fullt – kan ikke bekrefte flere uten å melde av noen først.")
        # sveiper_utsatt nullstilles ogsaa: en administrativ bekreftelse er en bevisst handling som
        # skal fore til faktisk sending/fakturering naa - en tidligere "vent"-markering fra en manuell
        # registrering skal ikke stille denne handlingen ut av spill.
        con.execute("UPDATE paamelding SET status='bekreftet', sveiper_kjort=0, sveiper_utsatt=0, avslatt_ts=NULL, "
                    "oppdatert=? WHERE id=?", (na, paamelding_id))
        _frigi_gammel_faktura_hold(con, paamelding_id)
        logg(con, "status_endret", {"paamelding_id": paamelding_id, "fra": gammel_status, "til": "bekreftet"}, aktor=aktor)
        return paamelding_id

    if ny_status == "venteliste":
        if gammel_status == "bekreftet":
            raise Paameldingsfeil("Kan ikke sette en bekreftet deltaker til venteliste direkte – meld av i stedet.")
        con.execute("UPDATE paamelding SET status='venteliste', sveiper_kjort=0, sveiper_utsatt=0, avslatt_ts=NULL, "
                    "oppdatert=? WHERE id=?", (na, paamelding_id))
        _frigi_gammel_faktura_hold(con, paamelding_id)
        logg(con, "status_endret", {"paamelding_id": paamelding_id, "fra": gammel_status, "til": "venteliste"}, aktor=aktor)
        return None

    raise Paameldingsfeil("Ugyldig statusendring.")


# ---------- admin: redigering av kurs (fase 4) ----------

KURS_LAASTE_FELT = ("pris_nok", "fakturering", "betaling", "faktura_dager_for")  # laast ved oekonomisk binding


def antall_faktura_for_kurs(con, kurs_id: int) -> int:
    return con.execute(
        "SELECT COUNT(*) FROM faktura f JOIN paamelding p ON p.id=f.paamelding_id WHERE p.kurs_id=?",
        (kurs_id,)).fetchone()[0]


def har_okonomisk_binding_for_kurs(con, kurs_id: int) -> bool:
    """True naar kursets oekonomifelt (KURS_LAASTE_FELT) er bundet og ikke lenger kan endres. Boolsk laasebeslutning -
    antall_faktura_for_kurs() teller fortsatt bare fakturaer. Bundet naar minst ett av disse finnes:
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


def oppdater_kurs_felter(con, kurs_id: int, felter: dict, aktor: str = "admin") -> list[str]:
    """Oppdaterer kursfelt (ikke kapasitet/status, se egne funksjoner for dem).

    Saa snart kurset har en oekonomisk binding (se har_okonomisk_binding_for_kurs: faktura, faktura_forsok eller
    planlagt/utsatt faktura), blir de fakturarelaterte feltene (pris,
    fakturering, betaling, dager-for-faktura) STRIPPET her ogsaa - ikke bare skjult i UI -
    slik at de ikke kan endres via en direkte POST selv om skjemaet skulle tillate det.
    """
    gammel = con.execute("SELECT * FROM kurs WHERE id=?", (kurs_id,)).fetchone()
    if not gammel:
        raise Paameldingsfeil("Ukjent kurs")
    if "faktura_dager_for" in felter:   # ugyldig verdi avvises FOER noe skrives - ogsaa naar feltet ellers ville blitt laast
        felter["faktura_dager_for"] = valider_faktura_dager_for(felter["faktura_dager_for"])
    if har_okonomisk_binding_for_kurs(con, kurs_id):
        felter = {k: v for k, v in felter.items() if k not in KURS_LAASTE_FELT}
    endret = [f for f, v in felter.items() if (gammel[f] if gammel[f] is not None else "") != (v if v is not None else "")]
    if not endret:
        return []
    con.execute(f"UPDATE kurs SET {','.join(f'{f}=?' for f in felter)} WHERE id=?", [*felter.values(), kurs_id])
    logg(con, "kurs_endret", {"kurs_id": kurs_id, "felt": endret}, aktor=aktor)
    return endret


def synk_materiell_ansvarlig(con, kurs_id: int, ny_epost: str | None, aktor: str = "admin") -> int:
    """Naar kursholder endres i Oppsett, oppdaterer vi ogsaa e-posten purringer gaar til - materiell_krav
    er en egen tabell som ellers ville fortsatt aa peke paa den gamle kursholderen. Rorer aldri et krav
    som allerede er levert (det er historikk)."""
    if not ny_epost:
        return 0
    cur = con.execute(
        "UPDATE materiell_krav SET ansvarlig_epost=? WHERE kurs_id=? AND levert_ts IS NULL",
        (ny_epost, kurs_id))
    if cur.rowcount:
        logg(con, "materiell_ansvarlig_endret", {"kurs_id": kurs_id, "antall": cur.rowcount}, aktor=aktor)
    return cur.rowcount


def endre_kapasitet(con, kurs_id: int, ny_kapasitet: int | None, aktor: str = "admin") -> list[int]:
    """Endrer kapasitet. Okes den, rykkes venteliste automatisk opp saa langt det er plass, i samme
    rekkefolge som ved avmelding. Senkes den under antall bekreftede, fjernes INGEN automatisk -
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


def meld_paa(con, kurs_id: int, *, epost: str, fornavn: str, etternavn: str, deltaker: dict | None = None,
             paamelding: dict | None = None, sensitivt: dict | None = None, idag: date | None = None,
             aktor: str | None = None, tillat_utkast: bool = False, ignorer_frist: bool = False,
             beskytt_eksisterende_felt: bool = False) -> tuple[int, str]:
    """Registrer paamelding. Returnerer (paamelding_id, status). Setter venteliste naar kurset er fullt.

    `aktor`, `tillat_utkast` og `ignorer_frist` er kun ment for administrativ, manuell paamelding
    (fase 9) - offentlig paamelding/webhook/gruppepaamelding bruker aldri disse, og faar dermed
    uendret oppforsel. `avlyst` og `avsluttet` kan IKKE overstyres av noen - se KURS_STATUSVERDIER-
    bruken andre steder; det finnes bevisst ingen parameter for det.

    `beskytt_eksisterende_felt` sendes videre til finn_eller_opprett_deltaker() - se dens
    docstring. STANDARD er False (uendret oppforsel for alle eksisterende kall). Fase 10 sin
    CSV-import er eneste kaller som bruker True.
    """
    idag = idag or date.today()
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kurs_id,)).fetchone()
    if not kurs:
        raise Paameldingsfeil("Ukjent kurs")
    tillatte_statuser = ("aapen", "full", "aktiv") + (("utkast",) if tillat_utkast else ())
    if kurs["status"] not in tillatte_statuser:
        raise Paameldingsfeil("Kurset er ikke åpent for påmelding")
    if not ignorer_frist and kurs["paameldingsfrist"] and idag > date.fromisoformat(kurs["paameldingsfrist"]):
        raise Paameldingsfeil(f"Påmeldingsfristen ({kurs['paameldingsfrist']}) er passert.")

    deltaker_id = finn_eller_opprett_deltaker(
        con, epost, fornavn, etternavn, beskytt_eksisterende_felt=beskytt_eksisterende_felt, **(deltaker or {}))
    finnes = con.execute(
        "SELECT id, status, avslatt_ts FROM paamelding WHERE kurs_id=? AND deltaker_id=?", (kurs_id, deltaker_id)
    ).fetchone()
    if finnes and finnes["status"] != "avmeldt":
        raise Paameldingsfeil("Du er allerede påmeldt dette kurset")
    if finnes and finnes["avslatt_ts"]:     # et avslag står: bare admin kan gjenopprette det (statusendring på deltakersiden)
        raise Paameldingsfeil("Påmeldingen til dette kurset er ikke godkjent. Ta kontakt med kursadministrasjonen.")

    fullt = kurs["kapasitet"] is not None and antall_bekreftet(con, kurs_id) >= kurs["kapasitet"]
    status = "venteliste" if fullt else "bekreftet"
    na = datetime.now().isoformat(timespec="seconds")
    # sveiper_utsatt=0 er default her (ikke bare ved ny INSERT) fordi UPDATE-en under ved
    # REAKTIVERING ellers ville latt en gammel, avmeldt rad sin tidligere sveiper_utsatt-verdi
    # staa ubrukt igjen paa den nye paameldingen. Kaller (fase 9-adminrute) kan overstyre via
    # paamelding={"sveiper_utsatt": 1}.
    # faktura_onskes_na/faktura_tidligst_dato: samme begrunnelse som sveiper_utsatt - en NY registreringsrunde (ogsaa
    # reaktivering) skal aldri arve en gammel fakturabeslutning. Kaller kan overstyre faktura_onskes_na.
    felter = {"status": status, "samtykke_ts": na, "oppdatert": na, "sveiper_utsatt": 0,
              "faktura_onskes_na": 0, "faktura_tidligst_dato": None, **(paamelding or {})}
    # Betalingsmåte: deltakeren bestemmer bare når kurset er satt til 'deltaker_velger'
    if kurs["betaling"] == "deltaker_velger":
        felter["betaling"] = "per_samling" if felter.get("betaling") == "per_samling" else "samlet"
    else:
        felter["betaling"] = kurs["betaling"]

    if finnes:  # reaktiver tidligere avmelding
        con.execute(
            f"UPDATE paamelding SET {','.join(f'{k}=?' for k in felter)}, sveiper_kjort=0 WHERE id=?",
            [*felter.values(), finnes["id"]],
        )
        pid = finnes["id"]
    else:
        kolonner = ["kurs_id", "deltaker_id", *felter.keys()]
        pid = sett_inn(
            con, f"INSERT INTO paamelding ({','.join(kolonner)}) VALUES ({','.join('?' * len(kolonner))})",
            [kurs_id, deltaker_id, *felter.values()],
        )

    if sensitivt and any(sensitivt.values()):
        con.execute(_SENSITIVT_UPSERT, (pid, sensitivt.get("allergier"), sensitivt.get("tilrettelegging")))
    if fullt and kurs["status"] == "aapen":
        con.execute("UPDATE kurs SET status='full' WHERE id=?", (kurs_id,))
    logg(con, "paamelding", {"paamelding_id": pid, "kurs_id": kurs_id, "status": status},
        aktor=aktor or f"deltaker:{deltaker_id}")
    return pid, status


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
                   yrkestittel=NULL, hpr_nr=NULL WHERE id=?""",
                (ANONYM_NAVN, ANONYM_FORNAVN, ANONYM_ETTERNAVN, ny, deltaker_id))
    ut["paameldinger"] = con.execute(
        """UPDATE paamelding SET faktura_epost=NULL, faktura_adresse=NULL, faktura_postnr=NULL, faktura_sted=NULL,
                  faktura_ref=NULL, faktura_kommentar=NULL, intern_kommentar=NULL, oppdatert=? WHERE deltaker_id=?""",
        (naa_utc(), deltaker_id)).rowcount
    ut["sensitivt"] = con.execute(
        "DELETE FROM sensitivt WHERE paamelding_id IN (SELECT id FROM paamelding WHERE deltaker_id=?)",
        (deltaker_id,)).rowcount
    con.execute("DELETE FROM dokument_innhold WHERE dokument_id IN (SELECT id FROM dokument WHERE deltaker_id=?)",
                (deltaker_id,))
    ut["dokumenter"] = con.execute("DELETE FROM dokument WHERE deltaker_id=?", (deltaker_id,)).rowcount
    ut["innloggingslenker"] = con.execute("DELETE FROM innlogging_token WHERE deltaker_id=?", (deltaker_id,)).rowcount
    ut["utsendingslogg"] = con.execute("UPDATE utsending_logg SET mottaker=? WHERE mottaker=?", (ny, gammel)).rowcount
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
        raise Paameldingsfeil("Bare bekreftede påmeldinger og påmeldinger på venteliste kan avslås.")
    opprykket = meld_av(con, paamelding_id, aktor=aktor)
    con.execute("UPDATE paamelding SET avslatt_ts=? WHERE id=?", (naa_utc(), paamelding_id))
    logg(con, "paamelding_avslatt", {"paamelding_id": paamelding_id, "fra": p["status"]}, aktor=aktor)
    return opprykket


def meld_av(con, paamelding_id: int, aktor: str = "admin") -> int | None:
    """Avmelder og flytter forste paa ventelisten opp. Returnerer paamelding_id som ble flyttet opp."""
    p = con.execute("SELECT * FROM paamelding WHERE id=?", (paamelding_id,)).fetchone()
    na = datetime.now().isoformat(timespec="seconds")
    con.execute("UPDATE paamelding SET status='avmeldt', oppdatert=? WHERE id=?", (na, paamelding_id))
    logg(con, "avmelding", {"paamelding_id": paamelding_id}, aktor=aktor)
    if p["status"] != "bekreftet":
        return None
    neste = con.execute(
        "SELECT id FROM paamelding WHERE kurs_id=? AND status='venteliste' ORDER BY opprettet, id LIMIT 1",
        (p["kurs_id"],),
    ).fetchone()
    if neste:
        # sveiper_utsatt nullstilles av samme grunn som i sett_paamelding_status(): et automatisk
        # opprykk skal faktisk fore til bekreftelse/fakturering, ikke stille forbli utsatt.
        con.execute("UPDATE paamelding SET status='bekreftet', sveiper_kjort=0, sveiper_utsatt=0, oppdatert=? WHERE id=?",
                   (na, neste["id"]))
        _frigi_gammel_faktura_hold(con, neste["id"])
        logg(con, "flyttet_fra_venteliste", {"paamelding_id": neste["id"]})
        return neste["id"]
    con.execute("UPDATE kurs SET status='aapen' WHERE id=? AND status='full'", (p["kurs_id"],))
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
                            sendt_av_admin_id: int | None) -> tuple[int, str]:
    """Forbereder en manuell utsendelse (kalles ved forhaandsvisning) - sender IKKE noe selv.

    Lager en unik, stabil nokkel som skal folge med til selve sendingen (se Kjoring.send_admin_utsending),
    slik at et dobbeltklikk/refresh paa "Send" gjenbruker samme nokkel og dedupliseres riktig.
    Mottakerne er allerede validert av kalleren (tilhorer kurset) foer denne kalles.
    """
    nokkel = f"adhoc:{kurs_id}:{secrets.token_hex(8)}"
    utsending_id = sett_inn(
        con, "INSERT INTO admin_utsending (nokkel, kurs_id, emne, tekst, sendt_av_admin_id) VALUES (?,?,?,?,?)",
        (nokkel, kurs_id, emne, tekst, sendt_av_admin_id))
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
