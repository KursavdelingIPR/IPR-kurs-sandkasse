-- IPR kursautomasjon – datamodell
-- Utvidet fra Stians referanse (kurs / paameldinger / innkalling_logg):
--  * kursdager er egen tabell (i stedet for komma-separert liste) -> oppmote pr dag + innsjekkkode pr dag
--  * deltaker (person) er skilt fra paamelding -> én "Min side" paa tvers av kurs og spesialistlop
--  * sensitive opplysninger (allergi/tilrettelegging) i egen tabell med sletterutine

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS kurs (
    id              INTEGER PRIMARY KEY,
    kursnr          INTEGER UNIQUE,                 -- permanent, numerisk kursnummer fra telleren 'kursnr' (aldri gjenbrukt,
                                                    -- endres aldri). Settes ALLTID av db.opprett_kurs; NULL kun i en
                                                    -- gammel database som ikke er migrert ennaa (migrering 1 fyller inn)
    kode            TEXT UNIQUE NOT NULL,          -- nettadresse-del (slug) i offentlige lenker, f.eks. EFT-2027-1
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
    -- samlet = én faktura ved påmelding. per_samling = én faktura per samling (migrering 9), før hver samling.
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
    paamelding_intro TEXT,                          -- ren tekst over skjemaet (fase 12C5), NULL = ingen introduksjonstekst
    paamelding_knappetekst TEXT,                    -- ren tekst paa paameldingsknappen, NULL = standard «Meld meg på»
    opprettet       TEXT NOT NULL DEFAULT (datetime('now')),
    paameldingsfrist_manuell INTEGER NOT NULL DEFAULT 0,  -- 1 = admin har satt fristen selv: endres ikke naar datoene endres
    -- Signatur i påminnelser, kursbevis, avlysning og avslag (migrering «signaturer»): signatur_html satt = kursets egen,
    -- tilpassede versjon; ellers signaturen signatur_id; ellers standardsignaturen. Se kurs/signaturer.py.
    signatur_id     INTEGER REFERENCES signatur(id),
    signatur_html   TEXT,
    -- Kursbeviset (migrering 25 «kursbevis_per_kurs», fanen Kursbevis): kursbevis_html satt = kursets egen, redigerte
    -- versjon (renset HTML med flettefelt, kurs/kursbevismal.py); NULL = standardbeviset. kursbevis_ramme: fargen på
    -- rammen (kursbevismal.RAMMER), NULL = blå.
    kursbevis_html  TEXT,
    kursbevis_ramme TEXT,
    -- Arrangør (migrering 26 «kursmerke»): 'terapiakademiet' eller NULL = IPR. Skiller kursene i kurslisten (svak rosa
    -- bakgrunn) og skal senere styre avsenderadresse og utseende per kurs.
    merke           TEXT,
    -- Evaluering etter kurset (migrering 28): 'av' = sendes ikke for dette kurset, NULL = sendes dagen etter siste kursdag
    evaluering      TEXT,
    -- Kursbevis-design (migrering 30): kursbevis_design.id, NULL = kursbeviset som før (kurs/kursbevisdesign.py). Ingen
    -- fremmednøkkel: tabellen står lenger ned i skjemaet (PostgreSQL krever at den finnes først); designer slettes ikke.
    kursbevis_design_id INTEGER
);

-- Samlinger (migrering 9): kursdagene gruppert, f.eks. «Samling 1» over fire dager eller en enkelt veiledningsdag. Kursets
-- datoer, klokkeslett og timer registreres per samling; QR, oppmøte, påminnelser og kursbevis ligger fortsatt per kursdag.
CREATE TABLE IF NOT EXISTS samling (
    id              INTEGER PRIMARY KEY,
    kurs_id         INTEGER NOT NULL REFERENCES kurs(id) ON DELETE CASCADE,
    navn            TEXT,                           -- valgfritt, f.eks. «Samling 1» eller «Veiledningsdag»
    start_kl        TEXT NOT NULL,                  -- standard for samlingens dager (en dag kan overstyre)
    slutt_kl        TEXT NOT NULL,
    timer_pr_dag    REAL NOT NULL                   -- timer per kursdag på kursbeviset (en dag kan overstyre, kursdag.timer)
);

CREATE TABLE IF NOT EXISTS kursdag (
    id              INTEGER PRIMARY KEY,
    kurs_id         INTEGER NOT NULL REFERENCES kurs(id) ON DELETE CASCADE,
    dato            TEXT NOT NULL,                  -- ISO yyyy-mm-dd
    start_kl        TEXT,                           -- NULL = arv fra kurs (nye og endrede kurs har alltid dagens klokkeslett)
    slutt_kl        TEXT,
    innsjekk_token  TEXT UNIQUE NOT NULL,           -- lang, hemmelig – ligger i QR-koden
    innsjekk_kode   TEXT NOT NULL,                  -- kort kode (6 tegn) for de som ikke faar scannet
    samling_id      INTEGER REFERENCES samling(id), -- migrering 9: samlingen dagen hører til
    merknad         TEXT,                           -- valgfritt, f.eks. «Veiledningsdag»
    timer           REAL,                           -- NULL = samlingens timer_pr_dag
    UNIQUE (kurs_id, dato)
);

-- Deltakerens PRIVATE adresse (adresse, postnr, poststed - migrering 14): påkrevd for alle deltakere i alle veier inn. En
-- personopplysning (ikke sensitiv etter art. 9): aldri i logger, e-post eller eksport, og tømmes ved anonymisering
-- (db.anonymiser_deltaker). Betaler deltakeren selv, kopieres den til paamelding.faktura_* når påmeldingen lages.
CREATE TABLE IF NOT EXISTS deltaker (
    id              INTEGER PRIMARY KEY,
    epost           TEXT UNIQUE NOT NULL COLLATE NOCASE,
    navn            TEXT NOT NULL,                  -- fullt navn: alltid «fornavn etternavn», settes av db.fullt_navn()
    fornavn         TEXT NOT NULL DEFAULT '',       -- fornavn/etternavn registreres hver for seg (migrering 7)
    etternavn       TEXT NOT NULL DEFAULT '',
    telefon         TEXT,
    arbeidssted     TEXT,
    yrkestittel     TEXT,
    hpr_nr          TEXT,                           -- helsepersonellnr (aktuelt for spesialistutdanning)
    visma_kunde_id  TEXT,
    opprettet       TEXT NOT NULL DEFAULT (datetime('now')),
    adresse         TEXT,                           -- privat adresse (migrering 14), se over tabellen
    postnr          TEXT,
    poststed        TEXT
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
    faktura_onskes_na INTEGER NOT NULL DEFAULT 0,   -- 1 = deltaker/admin har eksplisitt bedt om faktura med en gang
                                                     -- (lagret valg - beregnes aldri fra datoer)
    faktura_tidligst_dato TEXT,                     -- ISO-dato. NULL = ingen utsatt samlet faktura er planlagt. Dato =
                                                     -- systemet har besluttet at samlet faktura ikke opprettes foer da
    faktura_kommentar TEXT,                         -- merknad om registreringen/fakturaen
    intern_kommentar  TEXT,                         -- kun synlig for administratorer
    opprettet       TEXT NOT NULL DEFAULT (datetime('now')),
    oppdatert       TEXT NOT NULL DEFAULT (datetime('now')),  -- settes eksplisitt av db.py ved hver endring
    avslatt_ts      TEXT,                           -- satt = IPR har avslått påmeldingen (status er da 'avmeldt'). Se db.avsla_paamelding
    -- Utgått (påmeldingen ble aldri fullført) og Forlatt (deltakelsen avsluttet administrativt/ufrivillig), migrering 8.
    -- Som Avslått er de 'avmeldt' med en dato. CHECK: høyst én av Avslått/Utgått/Forlatt, og bare på avmeldte påmeldinger.
    utgatt_ts       TEXT CHECK (utgatt_ts IS NULL OR (status='avmeldt' AND avslatt_ts IS NULL)),
    forlatt_ts      TEXT CHECK (forlatt_ts IS NULL OR (status='avmeldt' AND avslatt_ts IS NULL AND utgatt_ts IS NULL)),
    -- ekstradeltaker (migrering 16): en påmelding som er PÅ KURSET (status er da 'bekreftet') og som administrator har merket
    -- som ekstradeltaker. NULL = den vanlige statusen (påmeldt). Fra migrering 18 tar en ekstradeltaker ikke plass og teller ikke som
    -- påmeldt (db.antall_bekreftet), får ikke automatisk bekreftelse eller faktura, og deltar på hele kurset eller bare samlingene i
    -- tabellen ekstradeltaker_samling. CHECK: bare på påmeldinger som har plass.
    ekstradeltaker_ts TEXT CHECK (ekstradeltaker_ts IS NULL OR status='bekreftet'),
    -- Rabattpriser (migrering 32, kurs/rabatter.py). priskategori: rabatten deltakeren valgte (NULL = ordinær pris; feltet står
    -- igjen når rabatten er avvist, så det synes hva som ble valgt). rabatt_prosent: rabatten slik den var ved påmeldingen
    -- (NULL = ordinær pris). Prisen er kursets pris minus prosenten, avrundet til hele kroner, og følger kursets pris til
    -- fakturaen er laget. rabatt_status: NULL = ingen sjekk, 'venter' = en administrator må godkjenne retten til rabatten
    -- (fakturaen holdes tilbake), 'godkjent', 'avvist' (= ordinær pris). psyflix_*: det Psyflix trenger for å sjekke medlemskapet.
    priskategori    TEXT,
    rabatt_prosent  INTEGER CHECK (rabatt_prosent IS NULL OR rabatt_prosent BETWEEN 1 AND 99),
    rabatt_status   TEXT CHECK (rabatt_status IS NULL OR rabatt_status IN ('venter','godkjent','avvist')),
    psyflix_org     TEXT,
    psyflix_epost   TEXT,
    UNIQUE (kurs_id, deltaker_id)
);

-- Sensitive opplysninger holdes adskilt og slettes N dager etter kursslutt (se personvern.py)
CREATE TABLE IF NOT EXISTS sensitivt (
    paamelding_id   INTEGER PRIMARY KEY REFERENCES paamelding(id) ON DELETE CASCADE,
    allergier       TEXT,
    tilrettelegging TEXT
);

-- Samlingsutvalget til ekstradeltakere (migrering 18). En ekstradeltaker (paamelding.ekstradeltaker_ts) deltar enten på HELE kurset
-- (ingen rader her) eller bare på utvalgte samlinger (én rad per samling). Utvalget ryddes når påmeldingen slutter å være
-- ekstradeltaker eller forlater plassen. Slettes en samling, går raden med (CASCADE). Alle samlinger valgt lagres aldri som rader:
-- da er det «hele kurset» (db.sett_ekstradeltaker_samlinger). Ingen persondata: bare to id-er.
CREATE TABLE IF NOT EXISTS ekstradeltaker_samling (
    paamelding_id   INTEGER NOT NULL REFERENCES paamelding(id) ON DELETE CASCADE,
    samling_id      INTEGER NOT NULL REFERENCES samling(id) ON DELETE CASCADE,
    PRIMARY KEY (paamelding_id, samling_id)
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
    opprettet       TEXT NOT NULL DEFAULT (datetime('now')),
    signatur_html   TEXT                            -- signaturen i akkurat denne utsendelsen (renset), NULL = eldre utsendelse
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
    kontakt_navn    TEXT NOT NULL,                  -- «kontakt_fornavn kontakt_etternavn», satt av db.fullt_navn()
    kontakt_fornavn   TEXT NOT NULL DEFAULT '',
    kontakt_etternavn TEXT NOT NULL DEFAULT '',
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

-- Tellere som aldri gaar tilbake (kursnummer). db.neste_teller() bruker UPDATE ... RETURNING - atomisk i begge databaser.
CREATE TABLE IF NOT EXISTS teller (
    navn            TEXT PRIMARY KEY,
    verdi           INTEGER NOT NULL
);

-- Versjonerte migreringer (kurs/migreringer.py): en rad per migrering som er kjoert. Appen nekter aa kjoere mot en
-- database med annen versjon enn koden forventer (se migreringer.kontroller).
CREATE TABLE IF NOT EXISTS schema_versjon (
    versjon         INTEGER PRIMARY KEY,
    navn            TEXT NOT NULL,
    kjort           TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Ansatte som kan logge inn i administrasjonen. Passord lagres kun som hash (aldri klartekst).
CREATE TABLE IF NOT EXISTS admin_bruker (
    id              INTEGER PRIMARY KEY,
    brukernavn      TEXT UNIQUE NOT NULL COLLATE NOCASE,
    navn            TEXT NOT NULL,
    passord_hash    TEXT NOT NULL,
    aktiv           INTEGER NOT NULL DEFAULT 1,
    -- Rolle (fase 13): system = alt (brukere, daglig jobb), kursadmin = daglig kursarbeid, lese = kun se.
    -- Haandheves server-side i kurs/web/app.py (krever_admin). Entra-brukere faar rollen fra Entra ved hver innlogging.
    rolle           TEXT NOT NULL DEFAULT 'kursadmin' CHECK (rolle IN ('system','kursadmin','lese')),
    entra_oid       TEXT UNIQUE,                    -- Microsoft Entra ID objekt-id (NULL = lokal bruker)
    epost           TEXT,
    opprettet       TEXT NOT NULL DEFAULT (datetime('now')),
    hovedsignatur_id INTEGER REFERENCES signatur(id) -- brukerens hovedsignatur i manuelle e-poster, NULL = standard
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

-- Overstyringer av redigerbare maltekster (fase 12B). KUN overstyringer: standardtekstene ligger i kode
-- (kurs/maltekster.py) og kopieres ALDRI inn her. Ingen rad = standardtekst. Databasen lagrer bare tekst - den vet
-- ingenting om {koder}; validering skjer i maltekster.py foer skriving og ved hvert oppslag.
CREATE TABLE IF NOT EXISTS mal_tekst (
    mal             TEXT NOT NULL,                  -- f.eks. 'bekreftelse'
    felt            TEXT NOT NULL,                  -- f.eks. 'innledning'
    tekst           TEXT NOT NULL,
    oppdatert       TEXT NOT NULL DEFAULT (datetime('now')),  -- settes eksplisitt av db.py ved hver endring
    oppdatert_av    TEXT,                           -- aktor, f.eks. 'admin:kari'
    PRIMARY KEY (mal, felt)
);

-- Per-kurs overstyringer av det ordinaere paameldingsskjemaet (fase 12C2). KUN avvik fra kodet standard: registeret i
-- kurs/skjemafelt.py er fasit for hvilke felt som finnes og hva som kan overstyres. Ingen rad = kodet standard.
-- NULL i en egenskapskolonne = standard for den egenskapen. En rad der alle egenskaper er NULL skal ikke finnes.
-- Bevisst INGEN CHECK paa `felt`: ukjente rader skal oppdages og ignoreres kontrollert ved lesing (med advarsel), uten
-- at tabellen maa bygges om naar registeret utvides. Validering skjer i skjemafelt.py foer skriving og ved hver lesing.
CREATE TABLE IF NOT EXISTS kurs_skjemafelt (
    kurs_id         INTEGER NOT NULL REFERENCES kurs(id) ON DELETE CASCADE,
    felt            TEXT NOT NULL,                  -- f.eks. 'telefon'
    synlig          INTEGER CHECK (synlig IS NULL OR synlig IN (0,1)),
    obligatorisk    INTEGER CHECK (obligatorisk IS NULL OR obligatorisk IN (0,1)),
    rekkefolge      INTEGER,                        -- plass i den konfigurerbare gruppen (0 er en gyldig verdi)
    label           TEXT,                           -- ren tekst, escapes ved rendring
    hjelpetekst     TEXT,                           -- ren tekst, escapes ved rendring
    oppdatert       TEXT NOT NULL DEFAULT (datetime('now')),  -- settes eksplisitt av db.py ved hver reell endring
    oppdatert_av    TEXT,                           -- aktor, f.eks. 'admin:kari'
    PRIMARY KEY (kurs_id, felt)
);

-- Årsplan / kurshjul: planlagte (foreløpige) aktiviteter og notater som ennå ikke er kurs. Vises sammen med kursdagene
-- i årsplanen. 'plan' opptar datoene (teller i overlapp og ledige perioder), 'notat' er bare en påminnelse.
CREATE TABLE IF NOT EXISTS planlagt_aktivitet (
    id              INTEGER PRIMARY KEY,
    type            TEXT NOT NULL DEFAULT 'plan' CHECK (type IN ('plan','notat')),
    tittel          TEXT NOT NULL,
    fra_dato        TEXT NOT NULL,                  -- ISO yyyy-mm-dd
    til_dato        TEXT NOT NULL,                  -- ISO yyyy-mm-dd, lik fra_dato for én dag
    sted            TEXT,
    notat           TEXT,                           -- ren tekst, kun internt
    opprettet_av    TEXT,                           -- aktor, f.eks. 'admin:kari'
    opprettet       TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (til_dato >= fra_dato)
);

-- Perioder i årsplanen (migrering «kalenderperioder»): skoleferier per område (Oslo, Vestland ...) som admin legger inn -
-- aldri faste, nasjonale datoer i koden - og egne perioder: sperret periode (vises som konflikt) eller advarsel. Ingen av
-- dem hindrer at kurs opprettes; de vises bare i årsplanen. gjelder avgrenser en sperre/advarsel til fysiske eller online
-- kurs (f.eks. «Ikke webinarer i juli»). Røde dager lagres ikke: de regnes ut (kurs/helligdager.py).
CREATE TABLE IF NOT EXISTS kalenderperiode (
    id              INTEGER PRIMARY KEY,
    type            TEXT NOT NULL CHECK (type IN ('skoleferie','sperre','advarsel')),
    navn            TEXT NOT NULL,                  -- f.eks. «Høstferie», «Intern ferie», «Ikke webinarer i juli»
    omraade         TEXT,                           -- skoleferie: området (Oslo, Vestland ...), ellers NULL
    fra_dato        TEXT NOT NULL,                  -- ISO yyyy-mm-dd
    til_dato        TEXT NOT NULL,
    gjelder         TEXT NOT NULL DEFAULT 'alle' CHECK (gjelder IN ('alle','fysisk','online')),
    notat           TEXT,                           -- ren tekst, kun internt
    opprettet_av    TEXT,
    opprettet       TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (til_dato >= fra_dato)
);

-- Innholdet i dokumenter systemet selv lager (i dag: kursbevis). Lagres i databasen - ikke som filer, som ikke
-- overlever omstart/skalering i Azure App Service og ikke er med i databasens sikkerhetskopi. dokument.url er da 'db:'.
CREATE TABLE IF NOT EXISTS dokument_innhold (
    dokument_id     INTEGER PRIMARY KEY REFERENCES dokument(id) ON DELETE CASCADE,
    mimetype        TEXT NOT NULL DEFAULT 'text/html',
    innhold         TEXT NOT NULL
);

-- Tokens integrasjonene selv må fornye og huske (i dag: Visma sitt refresh-token, som byttes ut ved HVER fornyelse og
-- ellers ville gått tapt ved omstart). Startverdien kommer fra miljøvariabel/Key Vault; deretter er denne raden fasit.
-- Verdien logges og vises aldri.
CREATE TABLE IF NOT EXISTS integrasjon_token (
    navn            TEXT PRIMARY KEY,
    verdi           TEXT NOT NULL,
    oppdatert       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- E-posthistorikk (migrering «eposthistorikk»): en uforanderlig kopi av HVER e-post systemet sender, lagret FØR sendingen
-- i samme transaksjon som reservasjonen i utsending_logg (Kjoring.send_ferdigrendret_en_gang). Kan ikke kopien lagres,
-- sendes ikke e-posten. Én rad per forsøk: et nytt forsøk etter en feil får en ny rad, så feilede forsøk kan vises.
-- Innholdet endres aldri etter lagring - bare status (sendt/feilet/ukjent) oppdateres. Innloggingslenker (personlig
-- token) går ikke gjennom motoren og lagres aldri. «Retten til sletting» sletter kopiene (db.anonymiser_deltaker).
CREATE TABLE IF NOT EXISTS sendt_epost (
    id              INTEGER PRIMARY KEY,
    paamelding_id   INTEGER REFERENCES paamelding(id),  -- NULL = gjelder ingen påmelding (kursholder, admin, bedrift)
    kurs_id         INTEGER REFERENCES kurs(id),
    nokkel          TEXT NOT NULL,                  -- samme nøkkel/type som utsending_logg (koblingen til dedup-raden)
    type            TEXT NOT NULL,
    mal             TEXT,                           -- malen e-posten ble laget fra (bekreftelse, dagfor ...), NULL = ukjent
    til             TEXT NOT NULL,
    kopi            TEXT,
    fra             TEXT NOT NULL,
    sendt_av        TEXT NOT NULL,                  -- 'system' eller 'admin:<brukernavn>' (som hendelse.aktor)
    emne            TEXT NOT NULL,
    html            TEXT NOT NULL,                  -- hele e-posten slik den ble sendt (bilder som cid:-referanser)
    status          TEXT NOT NULL CHECK (status IN ('sender','sendt','feilet','ukjent')),
    feilmelding     TEXT,                           -- sikker feiltekst (feil.sikker_feiltekst) - aldri personopplysninger
    opprettet       TEXT NOT NULL,                  -- UTC, lagret før sendingen
    sendt_ts        TEXT                            -- UTC, satt når e-posten er bekreftet sendt
);
CREATE INDEX IF NOT EXISTS sendt_epost_paamelding ON sendt_epost (paamelding_id, opprettet);

-- Bilder og vedlegg i sendte e-poster. Lagres én gang per innhold (sha256), og endres aldri: byttes en logo, blir den nye
-- en ny fil, og gamle e-poster viser fortsatt den gamle.
CREATE TABLE IF NOT EXISTS epost_fil (
    sha256          TEXT PRIMARY KEY,
    mimetype        TEXT NOT NULL,
    storrelse       INTEGER NOT NULL,
    innhold         TEXT NOT NULL,                  -- base64
    opprettet       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sendt_epost_vedlegg (
    sendt_epost_id  INTEGER NOT NULL REFERENCES sendt_epost(id) ON DELETE CASCADE,
    nr              INTEGER NOT NULL,
    sha256          TEXT NOT NULL REFERENCES epost_fil(sha256),
    filnavn         TEXT NOT NULL,
    innebygd_cid    TEXT,                           -- satt = bilde i teksten (cid:<verdi>), NULL = vanlig vedlegg
    PRIMARY KEY (sendt_epost_id, nr)
);

-- Signaturbiblioteket (migrering «signaturer», E-postmaler → Signaturer). innhold er renset HTML (kurs/signaturer.py:
-- bare trygg formatering, lenker og bilder fra signatur_bilde). Bildene står som /admin/signaturer/bilde/<id> og sendes
-- som innebygde bilder (cid) - aldri som lenker til eksterne bilder. Bare én signatur er standard (reserve). versjon
-- hindrer at to som redigerer samtidig overskriver hverandre. En signatur som er i bruk, kan ikke slettes.
CREATE TABLE IF NOT EXISTS signatur (
    id              INTEGER PRIMARY KEY,
    navn            TEXT NOT NULL,
    innhold         TEXT NOT NULL DEFAULT '',
    standard        INTEGER NOT NULL DEFAULT 0,
    versjon         INTEGER NOT NULL DEFAULT 1,
    opprettet       TEXT NOT NULL DEFAULT (datetime('now')),
    opprettet_av    TEXT,                           -- 'system' eller 'admin:<brukernavn>' (som hendelse.aktor)
    endret          TEXT,
    endret_av       TEXT
);

-- Logoer og andre bilder i signaturer: bare PNG, JPEG og GIF (kontrollert ut fra innholdet), maks 1 MB og 1200 px bredt.
-- Endres aldri - et nytt bilde blir en ny rad.
CREATE TABLE IF NOT EXISTS signatur_bilde (
    id              INTEGER PRIMARY KEY,
    filnavn         TEXT NOT NULL,
    mimetype        TEXT NOT NULL,
    bredde          INTEGER NOT NULL,
    hoyde           INTEGER NOT NULL,
    storrelse       INTEGER NOT NULL,
    sha256          TEXT NOT NULL,
    alt             TEXT NOT NULL,                  -- alternativ tekst (vises hvis bildet ikke lastes, leses av skjermlesere)
    innhold         TEXT NOT NULL,                  -- base64
    opprettet       TEXT NOT NULL DEFAULT (datetime('now')),
    opprettet_av    TEXT
);

-- Egne felt i påmeldingsskjemaet, per kurs (skjemabyggeren, migrering 13). Ledetekst, hjelpetekst og valg er ren tekst
-- (escapes ved rendring). Bevisst INGEN CHECK på type, plassering og vis_naar_felt: som i kurs_skjemafelt valideres de i
-- kurs/ekstrafelt.py før skriving og ved hver lesing, så nye typer kan komme uten at tabellen må bygges om. Et felt som
-- har svar, kan ikke slettes (fremmednøkkelen i paamelding_svar hindrer det) - det skjules i stedet.
-- Aldri sensitive opplysninger her: allergier og tilrettelegging har egne felt (tabellen sensitivt, slettes automatisk).
CREATE TABLE IF NOT EXISTS kurs_ekstrafelt (
    id              INTEGER PRIMARY KEY,
    kurs_id         INTEGER NOT NULL REFERENCES kurs(id) ON DELETE CASCADE,
    type            TEXT NOT NULL,                  -- tekst, langtekst, avkrysning, envalg, flervalg, nedtrekk
    label           TEXT NOT NULL,                  -- feltnavnet deltakeren ser
    hjelpetekst     TEXT,
    valg            TEXT,                           -- JSON-liste med svaralternativene (ikke for tekst/langtekst)
    obligatorisk    INTEGER NOT NULL DEFAULT 0 CHECK (obligatorisk IN (0,1)),
    synlig          INTEGER NOT NULL DEFAULT 1 CHECK (synlig IN (0,1)),
    plassering      TEXT NOT NULL DEFAULT 'om_deg', -- om_deg (sammen med telefon m.m.) eller til_slutt (før samtykket)
    rekkefolge      INTEGER NOT NULL DEFAULT 0,     -- plass innenfor plasseringen, sammen med standardfeltene der
    vis_naar_felt   TEXT,                           -- vises bare når dette feltet ('betaler' eller 'ekstra:<id>') ...
    vis_naar_verdi  TEXT,                           -- ... har denne verdien. Begge NULL = vises alltid
    opprettet       TEXT NOT NULL DEFAULT (datetime('now')),
    oppdatert       TEXT NOT NULL DEFAULT (datetime('now')),  -- settes eksplisitt av db.py ved hver endring
    oppdatert_av    TEXT,                           -- aktor, f.eks. 'admin:kari'
    rolle           TEXT,                           -- 'deling_epost' = kursets spørsmål om deling av e-post (migrering 27)
    CHECK ((vis_naar_felt IS NULL) = (vis_naar_verdi IS NULL))
);
-- Unik indeks ux_kurs_ekstrafelt_rolle (kurs_id, rolle) WHERE rolle IS NOT NULL lages av migrering 27 (epostdeling), etter kolonnen.

-- Deltakerens svar på kursets egne felt. Ren tekst; flervalg lagres som JSON-liste. Slettes sammen med påmeldingen og
-- ved anonymisering. Aldri i e-post, hendelseslogg eller driftslogg.
CREATE TABLE IF NOT EXISTS paamelding_svar (
    paamelding_id   INTEGER NOT NULL REFERENCES paamelding(id) ON DELETE CASCADE,
    felt_id         INTEGER NOT NULL REFERENCES kurs_ekstrafelt(id),
    verdi           TEXT NOT NULL,
    PRIMARY KEY (paamelding_id, felt_id)
);

-- Kursside (migrering «kursside»): siden deltakerne ser etter innlogging, én per kurs (kurs/sideinnhold.py, kurs/sidelager.py).
-- Innholdet er ETT JSON-dokument i to kopier: utkast (redigeres, autolagres) og publisert (det deltakerne ser). versjon øker
-- ved hver lagring av utkastet og hindrer at to som redigerer samtidig overskriver hverandre (UPDATE ... WHERE versjon=...).
-- Raden opprettes første gang siden lagres. Ingen deltakerdata utover fornavn + forbokstav i gruppetabeller som admin selv
-- skriver (tømmes automatisk etter kurset, se daglig.py). Aldri allergier/tilrettelegging.
CREATE TABLE IF NOT EXISTS kursside (
    kurs_id             INTEGER PRIMARY KEY REFERENCES kurs(id) ON DELETE CASCADE,
    utkast              TEXT NOT NULL DEFAULT '{}',     -- JSON (sideinnhold.py)
    publisert           TEXT,                           -- JSON; NULL = aldri publisert
    versjon             INTEGER NOT NULL DEFAULT 1,     -- utkastets versjon
    publisert_versjon   INTEGER,                        -- utkastversjonen som ble publisert (versjon > publisert_versjon = upubliserte endringer)
    aktiv               INTEGER NOT NULL DEFAULT 1 CHECK (aktiv IN (0,1)),   -- 0 = nedtatt for deltakerne (nødbrems)
    innsjekk_krever_kode INTEGER NOT NULL DEFAULT 1 CHECK (innsjekk_krever_kode IN (0,1)),
    stenges             TEXT,                           -- ISO-dato: en bestemt siste dag siden er åpen. NULL = bruk apen_dager (eller standard)
    apen_dager          INTEGER CHECK (apen_dager IS NULL OR apen_dager BETWEEN 0 AND 3650),   -- dager etter siste kursdag siden er åpen. NULL = standard (KURSSIDE_ETTERTILGANG_DAGER, 180), 0 = ingen tidsbegrensning, 1-3650 = så mange dager
    endret              TEXT,                           -- UTC, siste lagring av utkastet
    endret_av           TEXT,                           -- 'admin:<brukernavn>'
    publisert_tid       TEXT,
    publisert_av        TEXT,
    redigerer_av        TEXT,                           -- myk «noen redigerer nå»-markering (hjerteslag hvert 30. sekund)
    redigerer_til       TEXT                            -- UTC; markeringen gjelder til dette tidspunktet
);

-- Tidligere versjoner: publiserte versjoner (arsak 'publisert') og automatiske sikkerhetskopier av utkastet før noe destruktivt
-- (arsak 'sikkerhetskopi': gjenoppretting, «Start fra mal», «Hent fra annet kurs»). Beskjæres i sidelager: siste 30 + siste 20.
CREATE TABLE IF NOT EXISTS kursside_versjon (
    id              INTEGER PRIMARY KEY,
    kurs_id         INTEGER NOT NULL REFERENCES kurs(id) ON DELETE CASCADE,
    innhold         TEXT NOT NULL,                  -- JSON
    arsak           TEXT NOT NULL CHECK (arsak IN ('publisert','sikkerhetskopi')),
    merknad         TEXT,                           -- f.eks. «Før gjenoppretting», «Før mal»
    versjon         INTEGER,                        -- utkastets versjon da raden ble laget
    opprettet       TEXT NOT NULL DEFAULT (datetime('now')),
    opprettet_av    TEXT
);
CREATE INDEX IF NOT EXISTS kursside_versjon_kurs ON kursside_versjon (kurs_id, id);

-- Bilder og dokumenter til kurssiden. Metadata her, innholdet i kursside_fil_innhold (aldri SELECT * i lister). Samme innhold to
-- ganger i samme kurs blir én rad (UNIQUE kurs_id + sha256). Type og mimetype settes av serveren ut fra endelse OG innhold.
CREATE TABLE IF NOT EXISTS kursside_fil (
    id              INTEGER PRIMARY KEY,
    kurs_id         INTEGER NOT NULL REFERENCES kurs(id) ON DELETE CASCADE,
    filnavn         TEXT NOT NULL,                  -- visningsnavn (renset, med æøå), maks 120 tegn
    type            TEXT NOT NULL CHECK (type IN ('bilde','dokument')),
    mimetype        TEXT NOT NULL,
    storrelse       INTEGER NOT NULL,
    sha256          TEXT NOT NULL,
    bredde          INTEGER,                        -- bare bilder (kan mangle for webp)
    hoyde           INTEGER,
    opprettet       TEXT NOT NULL DEFAULT (datetime('now')),
    opprettet_av    TEXT,
    UNIQUE (kurs_id, sha256)
);
CREATE INDEX IF NOT EXISTS kursside_fil_kurs ON kursside_fil (kurs_id);

CREATE TABLE IF NOT EXISTS kursside_fil_innhold (
    fil_id          INTEGER PRIMARY KEY REFERENCES kursside_fil(id) ON DELETE CASCADE,
    innhold         TEXT NOT NULL                   -- base64 (ingen BLOB-kolonner i skjemaet: speilingstesten kjenner bare INTEGER/REAL/TEXT)
);

-- «Min side» for ett kurs (migrering 19): den personlige lenken i e-postene er en SIGNERT adresse (lenker.min_side_token) og lagres
-- aldri. En rad finnes bare når en administrator har stengt lenken eller laget en ny: versjon økes (lenker i sendte e-poster slutter da å
-- virke), og stengt=1 betyr at ingen lenke virker før administrator lager en ny. Ingen rad = versjon 0, åpen. Ingen personopplysninger.
CREATE TABLE IF NOT EXISTS min_side_lenke (
    paamelding_id   INTEGER PRIMARY KEY REFERENCES paamelding(id) ON DELETE CASCADE,
    versjon         INTEGER NOT NULL DEFAULT 0,
    stengt          INTEGER NOT NULL DEFAULT 0 CHECK (stengt IN (0,1)),
    endret          TEXT,                           -- UTC
    endret_av       TEXT                            -- 'admin:<brukernavn>'
);

-- Planlagte kurs (migrering 20): kursene fra kursplanleggingen (fanen «Kommende kurs» i Kurskalender-Excelen) som vises i
-- Kalender og Årsplan, men som IKKE er opprettet i systemet: ingen påmeldingsside, ingen deltakere, ingen e-post og ingen
-- fakturering, og de står ikke i kurslisten på Oversikten (kurs/planlagte_kurs.py). Fargen velges én gang etter datoene og
-- lagres, så alle samlingene i kurset har samme farge og kurs som går nær hverandre i tid får ulike farger
-- (kursfarger.velg). Ingen deltakerdata.
CREATE TABLE IF NOT EXISTS planlagt_kurs (
    id              INTEGER PRIMARY KEY,
    prosjektnr      TEXT,                           -- prosjektnummeret fra kursplanen, f.eks. «672» (ikke kurs.kursnr)
    navn            TEXT NOT NULL,                  -- f.eks. «EFT 1-årig» eller «Modul 2 (kull 14)»
    arrangor        TEXT NOT NULL DEFAULT 'IPR',    -- kort: IPR, TA (Terapiakademiet), IPRO ...
    ekstern         INTEGER NOT NULL DEFAULT 0 CHECK (ekstern IN (0,1)),   -- 1 = annen arrangør: grå i kalenderen
    status          TEXT NOT NULL DEFAULT 'planlagt' CHECK (status IN ('planlagt','avlyst')),
    farge           INTEGER NOT NULL DEFAULT 0 CHECK (farge BETWEEN 0 AND 31),   -- .kursfarge-N i static/kursfarger.css
    antall_samlinger INTEGER CHECK (antall_samlinger IS NULL OR antall_samlinger BETWEEN 1 AND 20),  -- hele kurset; NULL = de som er lagt inn
    notat           TEXT,                           -- ren tekst, kun internt
    sjekk           TEXT,                           -- NULL = i orden, ellers hva som må sjekkes (f.eks. usikker opplysning fra Excel)
    kilde           TEXT,                           -- hvor kurset kom fra, f.eks. «Excel «Kommende kurs», rad 6, 45, 61»
    opprettet_av    TEXT,
    opprettet       TEXT NOT NULL DEFAULT (datetime('now')),
    kurs_id         INTEGER REFERENCES kurs(id) ON DELETE SET NULL   -- migrering 23 (indeksen planlagt_kurs_kurs lages der): satt =
                                                                      -- holder sjekklisten og rollene til dette kurset i systemet
);

-- Samlingene (eller datoene) i et planlagt kurs. Veiledningsdager i samlingen står i veiledningsdager.
CREATE TABLE IF NOT EXISTS planlagt_samling (
    id              INTEGER PRIMARY KEY,
    planlagt_kurs_id INTEGER NOT NULL REFERENCES planlagt_kurs(id) ON DELETE CASCADE,
    nr              INTEGER CHECK (nr IS NULL OR nr BETWEEN 1 AND 20),   -- 2 = «2. samling»; NULL = ikke nummerert
    navn            TEXT,                           -- valgfritt, f.eks. «Veiledning»; tomt = «N. samling»
    tema            TEXT,
    fra_dato        TEXT NOT NULL,                  -- ISO yyyy-mm-dd
    til_dato        TEXT NOT NULL,
    veiledningsdager TEXT,                          -- ISO-datoer i samlingen, kommaseparert, f.eks. «2027-01-18»
    sted            TEXT,                           -- by: Oslo, Bergen, Online ...
    lokale          TEXT,                           -- Paleet, N58, Chr. Mich ...
    kursholdere     TEXT,                           -- fritekst, f.eks. «Anne Hilde (T1), Vanja (T2)»
    veiledere       TEXT,                           -- fritekst (veiledningsdagene)
    notat           TEXT,                           -- ren tekst, kun internt
    sjekk           TEXT,                           -- NULL = i orden, ellers hva som må sjekkes
    kurs_samling_id INTEGER REFERENCES samling(id) ON DELETE SET NULL,   -- migrering 23: samlingen i kurset den følger
    CHECK (til_dato >= fra_dato)
);
CREATE INDEX IF NOT EXISTS planlagt_samling_kurs ON planlagt_samling (planlagt_kurs_id);
CREATE INDEX IF NOT EXISTS planlagt_samling_dato ON planlagt_samling (fra_dato);

-- Sjekklister (migrering 21): én MAL per kurstype (f.eks. «EFST 1-årig») med punkter og regler for hvilke samlinger punktet
-- gjelder og når fristen er, og en SJEKKLISTE per samling i et planlagt kurs (kurs/sjekklister.py). Teksten kan ha lenker
-- ([tekst](https://…)) til interne dokumenter: de ligger bare i databasen, aldri i koden. Ingen deltakerdata.
CREATE TABLE IF NOT EXISTS sjekkliste_mal (
    id              INTEGER PRIMARY KEY,
    navn            TEXT NOT NULL,                  -- f.eks. «EFST 1-årig»
    beskrivelse     TEXT,
    opprettet_av    TEXT,
    opprettet       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sjekkliste_malpunkt (
    id              INTEGER PRIMARY KEY,
    mal_id          INTEGER NOT NULL REFERENCES sjekkliste_mal(id) ON DELETE CASCADE,
    rekkefolge      INTEGER NOT NULL DEFAULT 0,
    nivaa           INTEGER NOT NULL DEFAULT 0 CHECK (nivaa IN (0,1)),       -- 1 = underpunkt til punktet over
    type            TEXT NOT NULL DEFAULT 'oppgave' CHECK (type IN ('oppgave','husk')),   -- husk = påminnelse/lenke uten avkryssing
    tekst           TEXT NOT NULL,
    felt            INTEGER NOT NULL DEFAULT 0 CHECK (felt IN (0,1)),        -- 1 = har et tekstfelt («Lim inn her:»)
    gjelder         TEXT NOT NULL DEFAULT 'alle' CHECK (gjelder IN ('alle','forste','siste','ikke_forste','ikke_siste','nr')),
    gjelder_nr      INTEGER CHECK (gjelder_nr IS NULL OR gjelder_nr BETWEEN 1 AND 20),   -- samlingens nummer når gjelder='nr'
    gjelder_sted    TEXT,                           -- NULL = overalt; «fysisk», «online» eller en by («Oslo»)
    gjelder_navn    TEXT,                           -- NULL = alle kurs; ellers bare kurs der navnet inneholder teksten
    frist_antall    INTEGER CHECK (frist_antall IS NULL OR frist_antall BETWEEN -366 AND 366),  -- NULL = ingen frist; negativ = før
    frist_enhet     TEXT NOT NULL DEFAULT 'dager' CHECK (frist_enhet IN ('dager','virkedager','uker','maaneder')),
    frist_fra       TEXT NOT NULL DEFAULT 'start' CHECK (frist_fra IN ('start','slutt'))           -- regnet fra samlingens start/slutt
);
CREATE INDEX IF NOT EXISTS sjekkliste_malpunkt_mal ON sjekkliste_malpunkt (mal_id, rekkefolge);

-- Hvilken mal et planlagt kurs bruker (ingen rad = ingen sjekkliste).
CREATE TABLE IF NOT EXISTS planlagt_kurs_sjekkliste (
    planlagt_kurs_id INTEGER PRIMARY KEY REFERENCES planlagt_kurs(id) ON DELETE CASCADE,
    mal_id          INTEGER NOT NULL REFERENCES sjekkliste_mal(id) ON DELETE CASCADE
);

-- Sjekklisten for én samling: punktene fra malen som gjelder samlingen (fristen regnet ut fra datoene) og egne punkter.
CREATE TABLE IF NOT EXISTS sjekkliste_punkt (
    id              INTEGER PRIMARY KEY,
    samling_id      INTEGER NOT NULL REFERENCES planlagt_samling(id) ON DELETE CASCADE,
    malpunkt_id     INTEGER REFERENCES sjekkliste_malpunkt(id) ON DELETE SET NULL,   -- NULL = lagt til for hånd
    rekkefolge      INTEGER NOT NULL DEFAULT 0,
    nivaa           INTEGER NOT NULL DEFAULT 0 CHECK (nivaa IN (0,1)),
    type            TEXT NOT NULL DEFAULT 'oppgave' CHECK (type IN ('oppgave','husk')),
    tekst           TEXT NOT NULL,
    felt            INTEGER NOT NULL DEFAULT 0 CHECK (felt IN (0,1)),
    felt_verdi      TEXT,
    frist           TEXT,                           -- ISO yyyy-mm-dd, NULL = ingen frist
    frist_manuell   INTEGER NOT NULL DEFAULT 0 CHECK (frist_manuell IN (0,1)),  -- 1 = endret for hånd: følger ikke datoene
    status          TEXT NOT NULL DEFAULT 'aapen' CHECK (status IN ('aapen','utfort','ikke_aktuelt')),
    status_tid      TEXT,                           -- UTC, sist status ble endret
    status_av       TEXT,                           -- 'admin:<brukernavn>'
    notat           TEXT,                           -- migrering 24: hva som er gjort («Sendt 12.10», ref.nr. …)
    slettet         INTEGER NOT NULL DEFAULT 0 CHECK (slettet IN (0,1))   -- migrering 24: punkt fra malen tatt bort her
);
CREATE INDEX IF NOT EXISTS sjekkliste_punkt_samling ON sjekkliste_punkt (samling_id, rekkefolge);
CREATE INDEX IF NOT EXISTS sjekkliste_punkt_frist ON sjekkliste_punkt (status, frist);

-- Kursholdere (migrering 22): personene i kolonnene i fanen «Kommende kurs» (kurs/kursholdere.py), og rollen hver av dem
-- har på en samling eller en veiledningsdag i et planlagt kurs - oversikten Kalender → Kursholdere, som i Excel.
CREATE TABLE IF NOT EXISTS kursholder (
    id              INTEGER PRIMARY KEY,
    navn            TEXT NOT NULL UNIQUE,           -- kort navn som i kolonnen («Marit B.»)
    fullt_navn      TEXT,
    rekkefolge      INTEGER NOT NULL DEFAULT 0,
    aktiv           INTEGER NOT NULL DEFAULT 1 CHECK (aktiv IN (0,1)),
    opprettet       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Rollen på én samling (dag = '') eller én veiledningsdag (dag = ISO-dato) i samlingen. T/T1/T2 trainer, F facilitator,
-- B back-up, O opplæring, V/v veileder, bv back-up veileder, - ikke med. psybase: 1 = booket i Psybase (svart), 0 = ikke
-- lagt inn (rød), som skriftfargen i Excel.
CREATE TABLE IF NOT EXISTS planlagt_rolle (
    id              INTEGER PRIMARY KEY,
    samling_id      INTEGER NOT NULL REFERENCES planlagt_samling(id) ON DELETE CASCADE,
    dag             TEXT NOT NULL DEFAULT '',
    kursholder_id   INTEGER NOT NULL REFERENCES kursholder(id) ON DELETE CASCADE,
    rolle           TEXT NOT NULL CHECK (rolle IN ('T','T1','T2','F','B','O','V','v','bv','-')),
    psybase         INTEGER NOT NULL DEFAULT 0 CHECK (psybase IN (0,1)),
    endret          TEXT,                           -- UTC
    endret_av       TEXT,                           -- 'admin:<brukernavn>'
    UNIQUE (samling_id, dag, kursholder_id)
);
CREATE INDEX IF NOT EXISTS planlagt_rolle_kursholder ON planlagt_rolle (kursholder_id);

-- Booking per samling (migrering 24): lokale, hotell, grupperom og lunsj - nederst når kurset åpnes i Kalender (kurs/booking.py).
-- status NULL = ikke satt. tekst: hvor, referansenummer og annet (kan ha navnet på en kursholder; aldri deltakerdata).
CREATE TABLE IF NOT EXISTS samling_booking (
    id              INTEGER PRIMARY KEY,
    samling_id      INTEGER NOT NULL REFERENCES planlagt_samling(id) ON DELETE CASCADE,
    type            TEXT NOT NULL CHECK (type IN ('lokale','hotell','grupperom','lunsj')),
    status          TEXT CHECK (status IN ('ikke_booket','booket','trengs_ikke')),
    tekst           TEXT,
    endret          TEXT,                           -- UTC
    endret_av       TEXT,                           -- 'admin:<brukernavn>'
    UNIQUE (samling_id, type)
);

-- Evaluering etter kurset (migrering 28, Camilla 05.10.2026). Svarene er ANONYME: evaluering_svar har ingen kobling til påmelding
-- eller deltaker, og bare datoen (ikke klokkeslett) lagres. evaluering_besvart sier bare AT en påmelding har svart (én gang per
-- påmelding), ikke hva. Lenken i e-posten er signert (lenker.signatur), ingen token lagres.
CREATE TABLE IF NOT EXISTS evaluering_besvart (
    paamelding_id   INTEGER PRIMARY KEY REFERENCES paamelding(id) ON DELETE CASCADE,
    kurs_id         INTEGER NOT NULL REFERENCES kurs(id) ON DELETE CASCADE,
    besvart         TEXT NOT NULL                   -- dato (YYYY-MM-DD)
);
CREATE TABLE IF NOT EXISTS evaluering_svar (
    id              INTEGER PRIMARY KEY,
    kurs_id         INTEGER NOT NULL REFERENCES kurs(id) ON DELETE CASCADE,
    besvart         TEXT NOT NULL,                  -- dato (YYYY-MM-DD), aldri klokkeslett
    svar            TEXT NOT NULL,                  -- JSON {spørsmål: svar} (kurs/evaluering.py)
    -- Migrering 34 (ny evaluering, Camilla 09.10.2026): samlingen svaret gjelder (NULL = hele kurset), hvor det kom fra ('epost' =
    -- personlig lenke, 'delt' = delt lenke/QR; NULL = eldre svar fra e-post) og tid brukt på skjemaet i sekunder (NULL = ukjent).
    -- Fortsatt aldri hvem eller klokkeslett.
    samling_id      INTEGER,
    kilde           TEXT CHECK (kilde IS NULL OR kilde IN ('epost','delt')),
    sekunder        INTEGER
);
CREATE INDEX IF NOT EXISTS evaluering_svar_kurs ON evaluering_svar (kurs_id);

-- «Klar til sending» (Camilla 05.10.2026, kurs/godkjenning.py): automatiske e-poster som venter på godkjenning før de sendes
-- (alle unntatt bekreftelse og venteliste). html er det som sendes, men personlige lenker (Min side, opplastingslenke,
-- kvitteringslenke) er skjult og lages på nytt i det e-posten sendes - de lagres aldri. Innholdet slettes når e-posten er sendt
-- eller ikke skal sendes (det som ble sendt, ligger i e-posthistorikken). Unik på (nokkel, mottaker, type) som utsending_logg:
-- samme e-post legges aldri i køen to ganger. status: venter / sendt / avvist (Ikke send) / utgaatt / uavklart.
CREATE TABLE IF NOT EXISTS epost_godkjenning (
    id              INTEGER PRIMARY KEY,
    nokkel          TEXT NOT NULL,
    mottaker        TEXT NOT NULL,
    type            TEXT NOT NULL,
    mal             TEXT,
    paamelding_id   INTEGER REFERENCES paamelding(id) ON DELETE CASCADE,
    kurs_id         INTEGER REFERENCES kurs(id) ON DELETE CASCADE,
    emne            TEXT NOT NULL,
    html            TEXT,                           -- NULL når e-posten er ferdig behandlet
    lagre_kopi      INTEGER NOT NULL DEFAULT 1,     -- 0 = e-post med personlig tilgangslenke: ingen kopi i historikken
    utloper         TEXT,                           -- siste dag den kan sendes (YYYY-MM-DD), NULL = ingen frist
    status          TEXT NOT NULL DEFAULT 'venter' CHECK (status IN ('venter','sendt','avvist','utgaatt','uavklart')),
    merknad         TEXT,                           -- hvorfor den ikke ble sendt - aldri personopplysninger
    opprettet       TEXT NOT NULL,                  -- UTC
    behandlet       TEXT,                           -- UTC
    behandlet_av    TEXT,                           -- 'system' eller 'admin:<brukernavn>'
    UNIQUE (nokkel, mottaker, type)
);
CREATE INDEX IF NOT EXISTS epost_godkjenning_status ON epost_godkjenning (status, kurs_id);

-- Kursbevis-design (Camilla 06.10.2026, kurs/kursbevisdesign.py): lages én gang og velges per kurs (kurs.kursbevis_design_id).
-- logo/illustrasjon/skrift er nøkler i kursbevisdesign.LOGOER/ILLUSTRASJONER/SKRIFTER ('' = ingen). Signaturen er et bilde
-- fra signaturbiblioteket (signatur_bilde), pluss navn og tittel under streken.
CREATE TABLE IF NOT EXISTS kursbevis_design (
    id              INTEGER PRIMARY KEY,
    navn            TEXT NOT NULL UNIQUE,
    tittel          TEXT NOT NULL DEFAULT 'Kursbevis',
    farge           TEXT NOT NULL,                  -- #rrggbb: rammen og overskriften
    logo            TEXT NOT NULL DEFAULT '',
    illustrasjon    TEXT NOT NULL DEFAULT '',
    skrift          TEXT NOT NULL DEFAULT 'serif',
    signatur_bilde_id INTEGER REFERENCES signatur_bilde(id) ON DELETE SET NULL,
    signatur_navn   TEXT,
    signatur_tittel TEXT,
    opprettet       TEXT NOT NULL,                  -- UTC
    endret          TEXT                            -- UTC
);

-- «Det stemmer» i datakontrollen (Camilla 07.10.2026, kurs/datakontroll.py): en verdi som er sjekket og ikke skal markeres igjen.
-- Bare en sha256 av verdien (aldri en kopi av e-post, navn eller telefon). Endres verdien, kontrolleres den på nytt.
CREATE TABLE IF NOT EXISTS datakontroll_ok (
    id              INTEGER PRIMARY KEY,
    deltaker_id     INTEGER NOT NULL REFERENCES deltaker(id) ON DELETE CASCADE,
    kode            TEXT NOT NULL,                  -- epost / navn / telefon / postnr / dublett
    verdi_hash      TEXT NOT NULL,
    opprettet       TEXT NOT NULL,                  -- UTC
    av              TEXT NOT NULL,                  -- 'admin:<brukernavn>'
    UNIQUE (deltaker_id, kode, verdi_hash)
);

-- Rabattpriser per kurs (migrering 32, kurs/rabatter.py): én rad per rabatt kurset gir. Ingen rad = ingen slik rabatt.
-- prosent: prisen er kursets pris minus prosenten (hele kroner). maa_godkjennes = 1: en påmelding med rabatten venter på at en
-- administrator godkjenner retten til den, og fakturaen holdes tilbake til da.
CREATE TABLE IF NOT EXISTS kurs_rabatt (
    id              INTEGER PRIMARY KEY,
    kurs_id         INTEGER NOT NULL REFERENCES kurs(id) ON DELETE CASCADE,
    kategori        TEXT NOT NULL,                  -- nieft / ipr_terapeut / student / psyflix (rabatter.KATEGORIER)
    prosent         INTEGER NOT NULL CHECK (prosent BETWEEN 1 AND 99),
    maa_godkjennes  INTEGER NOT NULL DEFAULT 1,
    UNIQUE (kurs_id, kategori)
);

-- Studentbeviset til en studentpris som venter på godkjenning (migrering 32). Bare til administrator har sett på det: raden slettes
-- når rabatten er godkjent eller avvist, ved anonymisering og når påmeldingen slettes. innhold er base64 (som epost_fil).
CREATE TABLE IF NOT EXISTS rabatt_bevis (
    id              INTEGER PRIMARY KEY,
    paamelding_id   INTEGER NOT NULL UNIQUE REFERENCES paamelding(id) ON DELETE CASCADE,
    filnavn         TEXT NOT NULL,
    mimetype        TEXT NOT NULL,                  -- image/png, image/jpeg, image/webp eller application/pdf
    storrelse       INTEGER NOT NULL,
    innhold         TEXT NOT NULL,                  -- base64
    opprettet       TEXT NOT NULL                   -- UTC
);

-- Kursets egne tekster i e-postmalene (migrering 33, Camilla 09.10.2026: «veldig mange av kursene er forskjellige, med ulike tekster,
-- ulike regler»): en rad per kurs, mal og felt som er tilpasset for kurset (fanen Kommunikasjon). Ingen rad = fellesteksten under
-- E-postmaler gjelder. Bare tekst, med samme regler og flettefelt som maltekst (kurs/maltekster.py). Kopieres når kurset dupliseres.
CREATE TABLE IF NOT EXISTS kurs_maltekst (
    id              INTEGER PRIMARY KEY,
    kurs_id         INTEGER NOT NULL REFERENCES kurs(id) ON DELETE CASCADE,
    mal             TEXT NOT NULL,                  -- bekreftelse (maltekster.KURSVISE_MALER)
    felt            TEXT NOT NULL,                  -- emne / innledning / avslutning
    tekst           TEXT NOT NULL,
    oppdatert       TEXT NOT NULL,                  -- UTC
    av              TEXT NOT NULL,                  -- 'admin:<brukernavn>'
    UNIQUE (kurs_id, mal, felt)
);

-- Ny evaluering (migrering 34, kurs/evaluering.py, Camilla 09.10.2026). Spørsmålene per kurs: ingen rader = standardsettet. nokkel er det
-- som lagres i svarene (evaluering_svar.svar) og står fast; nr er rekkefølgen. valg er JSON (svaralternativene for type 'valg').
CREATE TABLE IF NOT EXISTS evaluering_sporsmal (
    id              INTEGER PRIMARY KEY,
    kurs_id         INTEGER NOT NULL REFERENCES kurs(id) ON DELETE CASCADE,
    nr              INTEGER NOT NULL,
    nokkel          TEXT NOT NULL,
    type            TEXT NOT NULL CHECK (type IN ('skala','tekst','valg')),
    tekst           TEXT NOT NULL,
    hjelpetekst     TEXT,
    lenketekst      TEXT,
    lenke           TEXT,
    valg            TEXT,
    paakrevd        INTEGER NOT NULL DEFAULT 0,
    UNIQUE (kurs_id, nokkel)
);

-- Evalueringens oppsett per kurs (migrering 34): egen innledning (NULL = standardteksten), stengt, og versjonen av den delte lenken
-- («Ny lenke» øker den, så gamle lenker og QR-koder slutter å virke). Ingen rad = standard.
CREATE TABLE IF NOT EXISTS evaluering_oppsett (
    kurs_id         INTEGER PRIMARY KEY REFERENCES kurs(id) ON DELETE CASCADE,
    innledning      TEXT,
    stengt          INTEGER NOT NULL DEFAULT 0,
    delt_versjon    INTEGER NOT NULL DEFAULT 0
);

-- At en påmelding har svart på evalueringen etter en SAMLING (migrering 34; etter hele kurset: evaluering_besvart). Bare at, aldri hva.
CREATE TABLE IF NOT EXISTS evaluering_besvart_samling (
    paamelding_id   INTEGER NOT NULL REFERENCES paamelding(id) ON DELETE CASCADE,
    samling_id      INTEGER NOT NULL,
    kurs_id         INTEGER NOT NULL REFERENCES kurs(id) ON DELETE CASCADE,
    besvart         TEXT NOT NULL,                  -- dato (YYYY-MM-DD)
    PRIMARY KEY (paamelding_id, samling_id)
);
