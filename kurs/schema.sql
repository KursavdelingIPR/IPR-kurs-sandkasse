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
    paameldingsfrist TEXT,                          -- ISO-dato, NULL = ingen frist
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
    sveiper_utsatt  INTEGER NOT NULL DEFAULT 0,     -- 1 = registrert (typisk manuelt av admin), men bevisst
                                                     -- IKKE klar for automatisk e-post/fakturering ennaa
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

-- Sporer FORSOKET paa aa lage en faktura i Visma - helt adskilt fra selve `faktura`-tabellen, som
-- fortsatt KUN faar en rad naar Visma faktisk har bekreftet noe (uendret betydning, se trinn 2.5-
-- designet: en `faktura`-rad brukes bl.a. til aa laase pris/fakturafelt og summeres i rapporter, og
-- maa derfor aldri opprettes "i god tro" for Visma har svart).
-- status: reservert (forsok paagaar/kan ha blitt avbrutt), feilet (kjent trygt aa prove paa nytt),
--         ukjent (utfallet er IKKE avklart - proves ALDRI automatisk paa nytt).
-- Ved suksess slettes raden her og den ekte `faktura`-raden opprettes i samme lokale transaksjon.
-- Etter at et claim er vunnet sjekkes `faktura` PAA NYTT (foer commit og Visma-kall): en annen prosess kan ha
-- fullfort og slettet sitt forsok mellom foerste sjekk og claimen (ellers ville Visma faatt to fakturaer).
CREATE TABLE IF NOT EXISTS faktura_forsok (
    id              INTEGER PRIMARY KEY,
    paamelding_id   INTEGER NOT NULL REFERENCES paamelding(id),
    kursdag_id      INTEGER REFERENCES kursdag(id),
    status          TEXT NOT NULL CHECK (status IN ('reservert','feilet','ukjent')),
    feilmelding     TEXT,
    opprettet       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS faktura_forsok_unik ON faktura_forsok (paamelding_id, COALESCE(kursdag_id, 0));

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

-- Dedup for ALL utsending (Stians innkalling_logg, generalisert). For manuell e-post (fase 5) er
-- nokkelen 'adhoc:<kurs_id>:<tilfeldig>' - unik PR UTSENDELSE (ikke pr type), satt naar admin
-- forhaandsviser (se admin_utsending), slik at dobbeltklikk/refresh paa "Send" ikke sender to ganger,
-- mens den samme meldingen godt kan sendes paa nytt som en HELT NY utsendelse senere.
-- status: 'sendt' er standard (bakoverkompatibelt - alle rader skrevet av eldre kode/marker_sendt()
-- ER faktisk sendt). 'reservert'/'feilet'/'ukjent' brukes KUN av den atomiske claim-flyten i
-- Kjoring.send_en_gang() (se trinn 2.5-designet) - allerede_sendt() returnerer true KUN for 'sendt',
-- slik at et uavklart forsok aldri kan vises/telles som en faktisk sendt melding.
CREATE TABLE IF NOT EXISTS utsending_logg (
    nokkel          TEXT NOT NULL,                  -- f.eks. 'kurs:3'  / 'materiell:7' / 'adhoc:3:a1b2c3d4'
    mottaker        TEXT NOT NULL COLLATE NOCASE,
    type            TEXT NOT NULL,                  -- 'bekreftelse', 'ukefor', 'dagfor-2027-01-12', 'purring-7' ...
    status          TEXT NOT NULL DEFAULT 'sendt' CHECK (status IN ('reservert','sendt','feilet','ukjent')),
    sendt_ts        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (nokkel, mottaker, type)
);

-- Manuell e-post fra admin (fase 5). Selve teksten lagres HER, ikke gjentatt pr mottaker i
-- utsending_logg - koblingen mellom dem er nokkel. Raden opprettes ved forhaandsvisning (foer selve
-- sendingen), slik at nokkelen finnes og kan folge med bekreftelsen naar "Send" trykkes.
CREATE TABLE IF NOT EXISTS admin_utsending (
    id              INTEGER PRIMARY KEY,
    nokkel          TEXT UNIQUE NOT NULL,
    kurs_id         INTEGER NOT NULL REFERENCES kurs(id),
    emne            TEXT NOT NULL,
    tekst           TEXT NOT NULL,
    sendt_av_admin_id INTEGER REFERENCES admin_bruker(id),
    opprettet       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Hvem utsendelsen var ment for, fastsatt (og validert mot kurs_id) ved forhaandsvisning - slik at
-- selve sendingen ikke er avhengig av mottaker-ID-er som sendes inn paa nytt fra klienten.
CREATE TABLE IF NOT EXISTS admin_utsending_mottaker (
    utsending_id    INTEGER NOT NULL REFERENCES admin_utsending(id) ON DELETE CASCADE,
    paamelding_id   INTEGER NOT NULL REFERENCES paamelding(id),
    PRIMARY KEY (utsending_id, paamelding_id)
);

-- Bedriftspaamelding (fase 7): en kontaktperson melder paa flere deltakere til samme kurs i ett skjema.
-- innsendingsnokkel er utledet av kurs + kontakt-epost + hele deltakerlisten (IKKE tilfeldig) - det er
-- det som gjor at et dobbeltklikk/refresh gjenkjenner samme innsending i stedet for aa opprette en ny
-- firmapaamelding og behandle deltakerne paa nytt. kvittering_token er derimot kryptografisk tilfeldig,
-- og brukes kun i den offentlige kvitteringslenken.
CREATE TABLE IF NOT EXISTS firmapaamelding (
    id              INTEGER PRIMARY KEY,
    kurs_id         INTEGER NOT NULL REFERENCES kurs(id),
    innsendingsnokkel TEXT UNIQUE NOT NULL,
    kvittering_token  TEXT UNIQUE NOT NULL,
    kontakt_navn    TEXT NOT NULL,
    kontakt_epost   TEXT NOT NULL,
    kontakt_telefon TEXT,
    firmanavn       TEXT NOT NULL,
    org_nr          TEXT,
    faktura_ref     TEXT,
    faktura_adresse TEXT,
    faktura_postnr  TEXT,
    faktura_sted    TEXT,
    ehf             INTEGER NOT NULL DEFAULT 0,
    opprettet       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Utfallet for HVER rad som ble sendt inn, ogsaa de som feilet - en feilet meld_paa() oppretter ingen
-- paamelding-rad og ville ellers ikke etterlatt seg noe spor. Vises paa den offentlige kvitteringssiden,
-- men KUN navn og status/feilmelding derfra - ikke epost/telefon/HPR, som ikke trengs der.
CREATE TABLE IF NOT EXISTS firmapaamelding_rad (
    id                  INTEGER PRIMARY KEY,
    firmapaamelding_id  INTEGER NOT NULL REFERENCES firmapaamelding(id) ON DELETE CASCADE,
    navn                TEXT NOT NULL,
    epost               TEXT NOT NULL,
    paamelding_id       INTEGER REFERENCES paamelding(id),
    feilmelding         TEXT
);

CREATE TABLE IF NOT EXISTS innlogging_token (
    token           TEXT PRIMARY KEY,
    deltaker_id     INTEGER NOT NULL REFERENCES deltaker(id),
    utloper         TEXT NOT NULL,
    brukt           INTEGER NOT NULL DEFAULT 0
);

-- Fase 10: korttidslagret forhaandsvisning av CSV-import (deltaker/kurs.import_deltakere.py).
-- Radene kan inneholde sensitive opplysninger (allergi, faktura) - lever KUN server-side i
-- token-ets levetid (se FORHAANDSVISNING_LEVETID_MIN), ryddes ved bruk/utlop, og skal aldri i
-- hendelsesloggen. Nettleseren ser bare selve token-strengen, aldri radinnholdet.
CREATE TABLE IF NOT EXISTS import_forhaandsvisning (
    token           TEXT PRIMARY KEY,
    kurs_id         INTEGER NOT NULL REFERENCES kurs(id),
    admin_id        INTEGER NOT NULL REFERENCES admin_bruker(id),
    rader_json      TEXT NOT NULL,
    resultater_json TEXT NOT NULL,
    opprettet       TEXT NOT NULL DEFAULT (datetime('now')),
    utloper         TEXT NOT NULL
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
