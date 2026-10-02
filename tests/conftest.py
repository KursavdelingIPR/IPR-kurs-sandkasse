"""Felles testoppsett.

Standard: hver test bruker sin EGEN SQLite-fil (tmp_path), uansett hva DATABASE_URL er satt til i miljoet eller i .env -
en test skal aldri kunne skrive til en ekte driftsdatabase.

PostgreSQL-modus (valgfri): settes TEST_DATABASE_URL til en TOM TESTDATABASE (aldri sandbox/prod), kjores HELE testsuiten
mot ekte PostgreSQL. Hver SQLite-sti en test ber om (db.koble(sti) / config.DB_STI) faar da sitt eget, nyopprettede
PostgreSQL-skjema, som slettes etter testen. Tester merket `kun_sqlite` (de som bevisst lager en gammel SQLite-fil med
sqlite3 direkte) hoppes over i denne modusen; tester merket `kun_postgres` kjores KUN her.

    TEST_DATABASE_URL=postgresql://bruker:passord@127.0.0.1:5432/ipr_test python -m pytest tests
"""
import hashlib
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PG_TEST_URL = os.environ.get("TEST_DATABASE_URL", "").strip()


def pytest_configure(config):
    config.addinivalue_line("markers", "kun_sqlite: SQLite-spesifikk test (hoppes over i PostgreSQL-modus)")
    config.addinivalue_line("markers", "kun_postgres: kjores kun naar TEST_DATABASE_URL peker paa en PostgreSQL-testdatabase")
    config.addinivalue_line("markers", "uten_standardadresse: testen handler om privat adresse og registrerer bevisst UTEN adresse "
                                       "(se _standardadresse_ved_registrering under)")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if PG_TEST_URL and "kun_sqlite" in item.keywords:
            item.add_marker(pytest.mark.skip(reason="SQLite-spesifikk test (PostgreSQL-modus)"))
        if not PG_TEST_URL and "kun_postgres" in item.keywords:
            item.add_marker(pytest.mark.skip(reason="krever TEST_DATABASE_URL (PostgreSQL-testdatabase)"))


@pytest.fixture
def hpr_i_skjema(monkeypatch):
    """HPR-nummer samles ikke inn lenger i skjemaene (skjemafelt.HPR_I_SKJEMA = False, Camilla 02.10.2026). Koden som viser, validerer og lagrer HPR finnes
    fortsatt og kan slås på igjen; testene av den slår den på med denne (se test_hpr_nummer_samles_ikke_inn.py for standarden: av)."""
    from kurs import skjemafelt
    monkeypatch.setattr(skjemafelt, "HPR_I_SKJEMA", True)


class _PgTestskjemaer:
    """Ett PostgreSQL-skjema per SQLite-sti i EN test. Alle tilkoblinger spores og lukkes foer skjemaene slettes, slik at
    en tilkobling testen glemte aa lukke (aapen transaksjon) aldri kan blokkere DROP SCHEMA."""

    _kontrollert = False

    def __init__(self, url: str):
        import psycopg
        self._pg, self._url = psycopg, url
        self._skjemaer: dict[str, str] = {}
        self._tilkoblinger: list = []
        if not _PgTestskjemaer._kontrollert:
            self._krev_tom_testdatabase()
            _PgTestskjemaer._kontrollert = True

    def _krev_tom_testdatabase(self) -> None:
        """Sikring: testene skal ALDRI kjoeres mot en database med data. `public` skal kun ha citext-utvidelsen - finnes
        det tabeller der, er dette ikke en tom testdatabase (og tabellene ville dessuten skjult feil via search_path)."""
        with self._pg.connect(self._url, autocommit=True) as c:
            tabeller = c.execute("SELECT COUNT(*) FROM pg_tables WHERE schemaname = 'public'").fetchone()[0]
            citext = c.execute("SELECT 1 FROM pg_extension WHERE extname = 'citext'").fetchone()
        if tabeller:
            pytest.exit(f"TEST_DATABASE_URL peker paa en database med {tabeller} tabell(er) i public. Bruk en TOM "
                        "testdatabase (aldri sandbox/prod).", returncode=2)
        if not citext:
            pytest.exit("Testdatabasen mangler utvidelsen citext (CREATE EXTENSION citext; som eier/superbruker).",
                        returncode=2)

    def skjema_for(self, sti) -> str:
        from kurs import config
        nokkel = str(Path(sti or config.DB_STI).resolve())
        if nokkel not in self._skjemaer:
            navn = "t_" + hashlib.sha1(f"{nokkel}:{time.time_ns()}:{os.getpid()}".encode()).hexdigest()[:24]
            with self._pg.connect(self._url, autocommit=True) as c:
                c.execute(f'CREATE SCHEMA "{navn}"')
            self._skjemaer[nokkel] = navn
        return self._skjemaer[nokkel]

    def koble(self, sti=None):
        from kurs import db
        skjema = self.skjema_for(sti)
        raa = self._pg.connect(self._url, options=f"-c search_path={skjema},public", row_factory=db._rad_fabrikk)
        self._tilkoblinger.append(raa)
        return db._PgTilkobling(raa, self._pg)

    def rydd(self) -> None:
        for raa in self._tilkoblinger:
            try:
                raa.close()
            except Exception:  # noqa: BLE001 - opprydding skal aldri feile testen
                pass
        if self._skjemaer:
            with self._pg.connect(self._url, autocommit=True) as c:
                for navn in self._skjemaer.values():
                    c.execute(f'DROP SCHEMA IF EXISTS "{navn}" CASCADE')


_AKTIV: "_PgTestskjemaer | None" = None


def pg_skrivelaas_holdes(sti) -> bool:
    """PostgreSQL-motstykket til SQLite sin «kan en annen forbindelse ta skrivelaasen naa?»: holder NOEN tilkobling en
    skrivelaas (INSERT/UPDATE/DELETE i en uavsluttet transaksjon -> RowExclusiveLock eller sterkere) paa en tabell i
    testens skjema? Brukes av tester som kontrollerer at ingen laas holdes over et eksternt (nettverks)kall."""
    assert _AKTIV is not None, "kun i PostgreSQL-modus"
    skjema = _AKTIV.skjema_for(sti)
    with _AKTIV._pg.connect(_AKTIV._url, autocommit=True) as c:
        antall = c.execute(
            """SELECT COUNT(*) FROM pg_locks l JOIN pg_class k ON k.oid = l.relation
               JOIN pg_namespace n ON n.oid = k.relnamespace
               WHERE n.nspname = %s AND l.granted AND l.pid <> pg_backend_pid()
                 AND l.mode IN ('RowExclusiveLock','ShareRowExclusiveLock','ExclusiveLock','AccessExclusiveLock')""",
            (skjema,)).fetchone()[0]
    return antall > 0


@pytest.fixture(autouse=True)
def _database_i_tester(monkeypatch, request):
    global _AKTIV
    from kurs import config, db
    monkeypatch.setattr(config, "DATABASE_URL", "")
    if not PG_TEST_URL or "kun_sqlite" in request.keywords:
        yield None
        return
    skjemaer = _PgTestskjemaer(PG_TEST_URL)
    monkeypatch.setattr(db, "koble", skjemaer.koble)
    _AKTIV = skjemaer
    try:
        yield skjemaer
    finally:
        _AKTIV = None
        skjemaer.rydd()


# ============================ standard privat adresse i tester ============================
# Alle nye, reelle deltakere har privat adresse (adresse, postnr, poststed), og en privat faktura lages ALDRI uten (kurs/privatadresse.py,
# brukerens beslutning 01.10.2026). Testene som handler om påmelding, e-post, status, oppmøte og selve fakturamotoren registrerer ofte
# med db.meld_paa uten adresse, fordi adressen ikke er poenget. Da får de en standardadresse (som en ekte registrering har), slik at fakturaen
# lages som de forventer. Tester som handler om selve adressen (merket «Privat adresse mangler», holdet, nødbremsen, fakturaadressen) og
# registrerer bevisst uten adresse, merker seg med `pytestmark = pytest.mark.uten_standardadresse` (hele filen) eller @pytest.mark.uten_standardadresse.
# Gir testen selv en adresse (helt eller delvis), røres ingenting.

STANDARD_PRIVATADRESSE = {"adresse": "Standardveien 1", "postnr": "0150", "poststed": "Oslo"}


@pytest.fixture(autouse=True)
def _standardadresse_ved_registrering(monkeypatch, request):
    if request.node.get_closest_marker("uten_standardadresse"):
        yield
        return
    from kurs import db
    opprinnelig = db.meld_paa

    def meld_paa(con, kurs_id, *args, deltaker=None, **kw):
        d = dict(deltaker or {})
        if not any(d.get(k) for k in STANDARD_PRIVATADRESSE):
            d.update(STANDARD_PRIVATADRESSE)
        return opprinnelig(con, kurs_id, *args, deltaker=d, **kw)

    meld_paa.__wrapped__ = opprinnelig
    monkeypatch.setattr(db, "meld_paa", meld_paa)
    yield


# ============================ CSRF i tester ============================
# Alle state-endrende skjema krever csrf_token (kurs/web/sikkerhet.py). Testklienten legger tokenet til automatisk i
# POST-skjemadata (dict), slik at de ~280 eksisterende POST-kallene tester selve funksjonaliteten uendret. Tester som
# kontrollerer CSRF-vernet setter `klient.injiser_csrf = False`. Malene kontrolleres separat (tests/test_sikkerhet.py:
# hvert POST-skjema maa inneholde feltet).

def _csrf_testklient():
    from flask.testing import FlaskClient

    class CsrfKlient(FlaskClient):
        injiser_csrf = True

        def open(self, *args, **kwargs):
            metode = (kwargs.get("method") or (args[1] if len(args) > 1 else "GET")).upper()
            data = kwargs.get("data")
            if self.injiser_csrf and metode in ("POST", "PUT", "PATCH", "DELETE") and isinstance(data, dict) \
                    and "csrf_token" not in data:
                with self.session_transaction() as s:
                    token = s.get("csrf")
                    if not token:
                        import secrets
                        token = s["csrf"] = secrets.token_urlsafe(32)
                kwargs["data"] = {**data, "csrf_token": token}
            elif self.injiser_csrf and metode in ("POST", "PUT", "PATCH", "DELETE") and data is None \
                    and kwargs.get("json") is None and not kwargs.get("content_type"):
                with self.session_transaction() as s:
                    token = s.get("csrf")
                    if not token:
                        import secrets
                        token = s["csrf"] = secrets.token_urlsafe(32)
                kwargs["data"] = {"csrf_token": token}
            return super().open(*args, **kwargs)

    return CsrfKlient


@pytest.fixture(autouse=True, scope="session")
def _csrf_i_testklienten():
    from kurs.web import app as webapp
    webapp.app.test_client_class = _csrf_testklient()
    yield


@pytest.fixture(autouse=True)
def _nullstill_takbegrensning():
    """Takbegrensningen (kurs/web/sikkerhet.py) teller per prosess - nullstilles per test slik at mange innlogginger paa
    tvers av tester aldri gir 429. Tester av selve vernet gjoer mange kall innenfor EN test."""
    from kurs.web import sikkerhet
    sikkerhet.takbegrenser.nullstill()
    yield
    sikkerhet.takbegrenser.nullstill()
