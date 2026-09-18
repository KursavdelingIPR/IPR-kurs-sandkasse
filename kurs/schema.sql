-- IPR kursautomasjon – datamodell
-- Utvidet fra Stians referanse (kurs / paameldinger / innkalling_logg):
--  * kursdager er egen tabell (i stedet for komma-separert liste) -> oppmote pr dag + innsjekkkode pr dag
--  * deltaker (person) er skilt fra paamelding -> én "Min side" paa tvers av kurs og spesialistlop
--  * sensitive opplysninger (allergi/tilrettelegging) i egen tabell med sletterutine

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS kurs (
    id              INTEGER PRIMARY KEY,
    kode            TEXT UNIQUE NOT NULL,          -- kort kode brukt i URL-er, f.eks. EFT-2027-1
    navn            TEXT NOT NULL,
    type            TEXT NOT NULL DEFAULT 'digital' CHECK (type IN ('digital','fysisk','hybrid')),
    status          TEXT NOT NULL DEFAULT 'aapen' CHECK (status IN ('utkast','aapen','full','aktiv','avsluttet','avlyst')),
    sted            TEXT,
    start_kl        TEXT NOT NULL DEFAULT '09:00',
    slutt_kl        TEXT NOT NULL DEFAULT '16:00',
    timer_pr_dag    REAL NOT NULL DEFAULT 6,
    kapasitet       INTEGER,                        -- NULL = ubegrenset
    pris_nok        INTEGER NOT NULL DEFAULT 0,     -- eks. mva
    fakturering     TEXT NOT NULL DEFAULT 'person' CHECK (fakturering IN ('person','organisasjon','ingen')),
    -- samlet = én faktura ved påmelding. per_samling = én faktura per kursdag, før hver samling.
    -- deltaker_velger = deltakeren velger selv i påmeldingsskjemaet.
    betaling        TEXT NOT NULL DEFAULT 'samlet' CHECK (betaling IN ('samlet','per_samling','deltaker_velger')),
    faktura_dager_for INTEGER NOT NULL DEFAULT 14,   -- dager før hver samling delfaktura sendes
    visma_artikkel  TEXT,                           -- artikkelnr i Visma
    spesialistlop   TEXT,                           -- f.eks. 'EFT' -> timer summeres paa tvers av samlinger
    kursholder_epost TEXT,
    zoom_url        TEXT,
    zoom_id         TEXT,
    zoom_pw         TEXT,
    sharepoint_mappe TEXT,                          -- sti/URL til kursmappe i SharePoint
    notat           TEXT,
    ansvarlig_admin_id INTEGER REFERENCES admin_bruker(id),  -- intern eier i adm, brukes av "vis bare mine aktiviteter"
    opprettet       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kursdag (
    id              INTEGER PRIMARY KEY,
    kurs_id         INTEGER NOT NULL REFERENCES kurs(id) ON DELETE CASCADE,
    dato            TEXT NOT NULL,                  -- ISO yyyy-mm-dd
    start_kl        TEXT,                           -- NULL = arv fra kurs
    slutt_kl        TEXT,
    innsjekk_token  TEXT UNIQUE NOT NULL,           -- lang, hemmelig – ligger i QR-koden
    innsjekk_kode   TEXT NOT NULL,                  -- kort kode (6 tegn) for de som ikke faar scannet
    UNIQUE (kurs_id, dato)
);

CREATE TABLE IF NOT EXISTS deltaker (
    id              INTEGER PRIMARY KEY,
    epost           TEXT UNIQUE NOT NULL COLLATE NOCASE,
    navn            TEXT NOT NULL,
    telefon         TEXT,
    arbeidssted     TEXT,
    yrkestittel     TEXT,
    hpr_nr          TEXT,                           -- helsepersonellnr (aktuelt for spesialistutdanning)
    visma_kunde_id  TEXT,
    opprettet       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS paamelding (
    id              INTEGER PRIMARY KEY,
    kurs_id         INTEGER NOT NULL REFERENCES kurs(id),
    deltaker_id     INTEGER NOT NULL REFERENCES deltaker(id),
    status          TEXT NOT NULL DEFAULT 'bekreftet' CHECK (status IN ('bekreftet','venteliste','avmeldt')),
    betaler         TEXT NOT NULL DEFAULT 'person' CHECK (betaler IN ('person','organisasjon')),
    betaling        TEXT NOT NULL DEFAULT 'samlet' CHECK (betaling IN ('samlet','per_samling')),
    org_navn        TEXT,
    org_nr          TEXT,
    faktura_epost   TEXT,
    faktura_adresse TEXT,
    faktura_postnr  TEXT,
    faktura_sted    TEXT,
    faktura_ref     TEXT,                           -- bestillernr / ressursnr (kreves ofte av offentlige)
    ehf             INTEGER NOT NULL DEFAULT 0,
    rabattkode      TEXT,
    samtykke_ts     TEXT,                           -- tidspunkt for aksept av vilkaar/personvern
    kilde           TEXT NOT NULL DEFAULT 'skjema',
    sveiper_kjort   INTEGER NOT NULL DEFAULT 0,     -- 1 naar "ved paamelding"-kjeden er fullfort
    faktura_kommentar TEXT,                         -- merknad om registreringen/fakturaen
    intern_kommentar  TEXT,                         -- kun synlig for administratorer
    opprettet       TEXT NOT NULL DEFAULT (datetime('now')),
    oppdatert       TEXT NOT NULL DEFAULT (datetime('now')),  -- settes eksplisitt av db.py ved hver endring
    UNIQUE (kurs_id, deltaker_id)
);

-- Sensitive opplysninger holdes adskilt og slettes N dager etter kursslutt (se personvern.py)
CREATE TABLE IF NOT EXISTS sensitivt (
    paamelding_id   INTEGER PRIMARY KEY REFERENCES paamelding(id) ON DELETE CASCADE,
    allergier       TEXT,
    tilrettelegging TEXT
);

CREATE TABLE IF NOT EXISTS faktura (
    id              INTEGER PRIMARY KEY,
    paamelding_id   INTEGER NOT NULL REFERENCES paamelding(id),
    kursdag_id      INTEGER REFERENCES kursdag(id),  -- NULL = samlet faktura, ellers delfaktura for én samling
    visma_id        TEXT,
    faktura_nr      TEXT,
    belop_nok       INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'opprettet' CHECK (status IN ('opprettet','sendt','betalt','kreditert','feil')),
    feilmelding     TEXT,
    opprettet       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Aldri dobbeltfakturering: én samlet faktura, eller én per samling. (COALESCE fordi NULL != NULL i SQLite.)
CREATE UNIQUE INDEX IF NOT EXISTS faktura_unik ON faktura (paamelding_id, COALESCE(kursdag_id, 0));

CREATE TABLE IF NOT EXISTS oppmote (
    paamelding_id   INTEGER NOT NULL REFERENCES paamelding(id),
    kursdag_id      INTEGER NOT NULL REFERENCES kursdag(id),
    kilde           TEXT NOT NULL CHECK (kilde IN ('qr','kode','zoom','manuell')),
    minutter        INTEGER,                        -- fra Zoom-rapport (digitale kurs)
    ts              TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (paamelding_id, kursdag_id)
);

-- Dokumenter paa Min side. kurs-nivaa (deltaker_id NULL) eller personlig (kontrakt, kursbevis)
CREATE TABLE IF NOT EXISTS dokument (
    id              INTEGER PRIMARY KEY,
    kurs_id         INTEGER REFERENCES kurs(id),
    deltaker_id     INTEGER REFERENCES deltaker(id),
    type            TEXT NOT NULL CHECK (type IN ('presentasjon','kontrakt','kursbevis','veiledning','annet')),
    tittel          TEXT NOT NULL,
    url             TEXT NOT NULL,                  -- SharePoint-lenke eller lokal sti
    publisert       INTEGER NOT NULL DEFAULT 1,
    opprettet       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Materiell som psykologer/kursholdere skylder (purring)
CREATE TABLE IF NOT EXISTS materiell_krav (
    id              INTEGER PRIMARY KEY,
    kurs_id         INTEGER NOT NULL REFERENCES kurs(id),
    ansvarlig_navn  TEXT NOT NULL,
    ansvarlig_epost TEXT NOT NULL,
    beskrivelse     TEXT NOT NULL DEFAULT 'Kurspresentasjon',
    frist           TEXT NOT NULL,                  -- ISO-dato
    levert_ts       TEXT
);

-- Dedup for ALL utsending (Stians innkalling_logg, generalisert)
CREATE TABLE IF NOT EXISTS utsending_logg (
    nokkel          TEXT NOT NULL,                  -- f.eks. 'kurs:3'  / 'materiell:7'
    mottaker        TEXT NOT NULL COLLATE NOCASE,
    type            TEXT NOT NULL,                  -- 'bekreftelse', 'ukefor', 'dagfor-2027-01-12', 'purring-7' ...
    sendt_ts        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (nokkel, mottaker, type)
);

CREATE TABLE IF NOT EXISTS innlogging_token (
    token           TEXT PRIMARY KEY,
    deltaker_id     INTEGER NOT NULL REFERENCES deltaker(id),
    utloper         TEXT NOT NULL,
    brukt           INTEGER NOT NULL DEFAULT 0
);

-- Ansatte som kan logge inn i administrasjonen. Passord lagres kun som hash (aldri klartekst).
CREATE TABLE IF NOT EXISTS admin_bruker (
    id              INTEGER PRIMARY KEY,
    brukernavn      TEXT UNIQUE NOT NULL COLLATE NOCASE,
    navn            TEXT NOT NULL,
    passord_hash    TEXT NOT NULL,
    aktiv           INTEGER NOT NULL DEFAULT 1,
    opprettet       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Revisjonsspor: hva skjedde, naar, og hvem/hva gjorde det
CREATE TABLE IF NOT EXISTS hendelse (
    id              INTEGER PRIMARY KEY,
    ts              TEXT NOT NULL DEFAULT (datetime('now')),
    aktor           TEXT NOT NULL,                  -- 'system', 'admin', 'deltaker:12'
    handling        TEXT NOT NULL,
    detaljer        TEXT
);

-- Kunnskapsbase for KI-assistenten. KUN godkjente, gyldige svar kan vises til deltakere.
CREATE TABLE IF NOT EXISTS kunnskap (
    id              INTEGER PRIMARY KEY,
    kategori        TEXT NOT NULL,                  -- 'avmelding', 'praktisk', 'faktura', 'zoom' ...
    sporsmal        TEXT NOT NULL,                  -- typisk spørsmål + varianter (én per linje)
    svar            TEXT NOT NULL,                  -- godkjent svartekst – vises ORDRETT
    kurs_id         INTEGER REFERENCES kurs(id),    -- NULL = gjelder alle kurs
    gyldig_til      TEXT,                           -- ISO-dato, NULL = ingen utløp
    godkjent        INTEGER NOT NULL DEFAULT 0,
    oppdatert       TEXT NOT NULL DEFAULT (datetime('now')),
    oppdatert_av    TEXT
);

-- Alle spørsmål til assistenten, og om de ble besvart automatisk eller sendt til adm
CREATE TABLE IF NOT EXISTS henvendelse (
    id              INTEGER PRIMARY KEY,
    ts              TEXT NOT NULL DEFAULT (datetime('now')),
    deltaker_id     INTEGER REFERENCES deltaker(id),
    epost           TEXT,
    sporsmal        TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('besvart','til_adm','lukket')),
    kilder          TEXT,                           -- f.eks. 'K3,F1'
    grunn           TEXT
);
