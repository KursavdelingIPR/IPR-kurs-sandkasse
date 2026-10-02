# Midlertidige migreringsnumre (parallelle grener)

Flere grener utvikles samtidig og trenger hver sin databasemigrering. Migreringene må ha fortløpende numre, og en
database husker bare *nummeret* den er migrert til. To grener kan derfor ikke bruke samme nummer i samme database.

## Rekkefølge (planlagt)

| Endelig nr. | Migrering | Gren | Nummer i grenen nå |
|---|---|---|---|
| 9 | `samlinger` | `claude/opprett-kurs-samlinger` (Opprett kurs) | 9 |
| 10 | `eposthistorikk` | `claude/eposter-signaturer` | **9 (midlertidig)** |
| 11 | `signaturer` | `claude/eposter-signaturer` | **10 (midlertidig)** |
| 12 | `kalenderperioder` | `claude/aarsplan` | **9 (midlertidig)** |
| 13 | `paameldingsskjema` | `claude/paameldingsskjema` | **9 (midlertidig)** |
| 14 | `deltaker_adresse` | `claude/paameldingsskjema` (deltakerens adresse) | **10 (midlertidig)** |
| 15 | `kursside` | `claude/kursside` | **14 (midlertidig i grenen)**; i forhåndsvisningen er den 15, etter `deltaker_adresse` (14) |
| 16 | `ekstradeltaker` | status Ekstradeltaker (`paamelding.ekstradeltaker_ts`) | 16 (bygget oppå forhåndsvisningen «samlet»; kommer sist i `MIGRERINGER`) |
| 17 | `kursside_apningstid` | kursside (åpningstid per kurs) | 17 (må komme etter `kursside` og `ekstradeltaker`; deler ikke tabell med andre migreringer). Får en annen gren samme nummer, gis den som kom sist neste nummer: funksjonen (`_m17_kursside_apningstid`) er idempotent og sjekker selv om kolonnen finnes |
| 18 | `ekstradeltaker_samling` | samlingsutvalg for ekstradeltakere (bygger på `ekstradeltaker`, 16, og `samlinger`, 9) | 18 (ny tabell sist i begge skjemafilene; må komme etter 16 og 17). Nummer 18 fordi 16 er `ekstradeltaker` og 17 er `kursside_apningstid`. Funksjonen (`_m18_ekstradeltaker_samling`) er idempotent (lager tabellen bare hvis den mangler) |

## Slik renummereres en gren når grenen foran er merget

1. Hent `main` med den mergede grenen, og rebase grenen på den (`git rebase main`).
2. I `kurs/migreringer.py`: gi migreringene i grenen neste ledige nummer (f.eks. 9 → 10). Funksjonsnavnet
   (`_m9_eposthistorikk`) kan gjerne endres tilsvarende. Fjern kommentaren om midlertidig nummer.
3. Oppdater raden i `MIGRATIONS.md` og tabellen over.
4. Kjør hele testsuiten.
5. Test først mot en **kopi** av sandkassedatabasen.

## Kjente overlapp ved rebase

- `kurs/schema.sql` og `schema_postgres.sql`, tabellen `kurs`: «Opprett kurs» legger til `paameldingsfrist_manuell`
  sist i tabellen, og signaturene legger til `signatur_id` og `signatur_html` på samme sted. Løsning: behold alle tre,
  i migreringsrekkefølgen (`paameldingsfrist_manuell`, deretter `signatur_id`, `signatur_html`).
- `kurs/integrasjoner/epost.py`, `kurs/web/app.py` og `kurs/web/static/app.js` er endret i begge grener (ulike steder
  i filene). Se over konfliktene linje for linje; ingen av endringene erstatter hverandre.
- `tests/test_paameldingsstatus.py` (liste over migreringer som kjøres) og `tests/test_skjema_speiling.py` (antall
  tabeller) må få tallene for alle grenene til sammen.
- `kurs/migreringer.py`: vakten (`_skjemaets_kolonner`, `_kontroller_skjema`) er lagt inn **identisk** i
  e-postgrenen og årsplangrenen. Behold én kopi.
- Filer som er identiske i flere grener (samme innhold gir ingen konflikt): `kurs/helligdager.py` (kalender og årsplan),
  `kurs/web/static/planlegging.css` og endringen i `base.html` (liste, kalender og årsplan), og denne filen
  (e-post og årsplan). Endres en av dem, må den kopieres til de andre grenene.
- Påmeldingsskjemaet (`claude/paameldingsskjema`) skriver om `kurs/web/templates/kurs.html` og forhåndsvisningen i
  `kurs/web/app.py` (`kursside`, `admin_forhandsvis_paamelding`). «Opprett kurs» endrer også betalingsvalget i
  `kurs.html`: behold den nye layouten (feltnavn til venstre, `felt_rad`) og legg betalingsvalget fra «Opprett kurs» inn
  i fakturadelen. Oppdater gullstandarden (`tests/test_skjemafelt_gullstandard.py::_generer`) etterpå.
  Informasjonsboksen viser samlingene fra «Opprett kurs» (`dager | samlinger(kurs)`): «Samling 1: 07.–09.11.2026»
  med klokkeslettet på egen, dempet linje under (`<span class="dempet">`), ikke én linje per dag. Tre tester må
  tilpasses: `test_informasjonsboksen_folger_valgene` (samlingen og klokkeslettet i stedet for «mandag 2. mars 2099»),
  `test_skjemafeltene_er_interaktive_ikke_globalt_disabled` (kurset trenger to samlinger for at betalingsvalget vises) og
  `tests/test_samlinger.py::test_paameldingssiden_viser_datoene_gruppert_og_aldri_intern_kommentar` (sammenligner den
  synlige teksten, fordi klokkeslettet står i et eget element).
  Forhåndsvisningen på 5005 (`forhandsvisning/samlet`) har dette allerede løst og kan brukes som mal.
- Påmeldingsskjemaet endrer også `kurs/sveiper.py` (`_opprett`: ingen faktura uten firmanavn), `kurs/daglig.py` (nytt steg 0),
  `kurs/hendelseslogg.py`, `kurs/import_deltakere.py`, `kurs/seed_demo.py`, `kurs/maler/epost/bekreftelse.html` og malene
  `admin.html`, `admin_deltaker.html`, `admin_deltaker_ny.html`, `admin_kurs_deltakere.html`, `admin_import_deltakere.html`,
  `kvittering.html`, `kurs_gruppe.html` og `kurs_gruppe_kvittering.html` (merket «Firmaopplysninger må kontrolleres»).
  Én felles regel for alle veier inn (`kurs/firmaopplysninger.py`): «Opprett kurs» og andre grener som lager påmeldinger,
  må bruke `slaa_opp`/`paamelding_felter` for firma. Ingen ny migrering: merket følger av dataene (firma betaler, og
  firmanavnet er tomt eller organisasjonsnummeret er ugyldig).
- Tabelltallet i `tests/test_skjema_speiling.py` øker med 2 (`kurs_ekstrafelt`, `paamelding_svar`).
- Deltakerens adresse (migrering 14, `deltaker_adresse`; i skjemagrenen nummer 10, rett etter `paameldingsskjema`): tre nye kolonner sist i tabellen `deltaker` i begge skjemafilene, og `_m14_deltaker_adresse` sist i `MIGRERINGER`. Når nummeret endres: `tests/test_paameldingsstatus.py` (listen over migreringer som kjøres), `MIGRATIONS.md` og tabellen over. Endrer også `kurs/skjemafelt.py` (låste felt `adresse`, `postnr`, `poststed`; de private fakturafeltene `faktura_adresse`/`faktura_postnr`/`faktura_sted` er avviklet, `AVVIKLEDE_FELT`) - samme sted som en annen gren kan ha lagt inn sin egen `AVVIKLEDE_FELT`: behold én, med alle navnene.
- Kursside (`claude/kursside`, migrering `kursside`): tabelltallet i `tests/test_skjema_speiling.py` øker med 4 (`kursside`,
  `kursside_versjon`, `kursside_fil`, `kursside_fil_innhold`), og listen over migreringer i
  `tests/test_paameldingsstatus.py` (`kjor_manglende(con) == [8, …]`) får nummeret til kursside sist. Ingen kode utenfor
  migreringslista avhenger av selve nummeret (`_m14_kursside` kan gis nytt nummer og navn uten videre). Kursside legger
  nye tabeller sist i begge skjemafilene og endrer ingen eksisterende tabell. Kursside legger også nye linjer i
  `kurs/web/app.py` (to `installer`-kall nederst, import, `KUN_KURSADMIN_ENDEPUNKTER`, innloggingsrutene `logg_inn`,
  `logg_inn_token` og `logg_ut`), `kurs/web/templates/admin_kurs_faner.html`, `admin_kurs_header.html`,
  `admin_aktiviteter.html` og `logg_inn.html`, `kurs/config.py`, `kurs/web/sikkerhet.py` (takgrenser) og `kurs/daglig.py`
  (nytt steg 9). Menyendringene i `base.html`/`admin_base.html` (senere byggetrinn) overlapper med «globalt deltakersøk»
  (søkefelt i admin-menyen) og med «ingen ledige plasser»/informasjonsboksen (`kurs.html`, `paamelding.css`): behold
  begge endringene.
- **Ekstradeltaker og Påmeldt som vanligste status (migrering 16, `ekstradeltaker`).** Én ny kolonne sist i tabellen `paamelding` i begge
  skjemafilene (`ekstradeltaker_ts`, med CHECK) og `_m16_ekstradeltaker` sist i `MIGRERINGER`. Tester som teller migreringer må få 16
  med (`tests/test_paameldingsstatus.py`: `kjor_manglende(con) == [8, …, 16]`; `tests/test_adresse_personvern.py` og
  `tests/test_kursside_migrering.py`: `[14, 15, 16]`, og «siste migrering» er nå `_m16_ekstradeltaker`). Statusnøklene i
  `db.PAAMELDINGSSTATUSER` er endret fra `bekreftet` til `paameldt` (databaseverdien `status='bekreftet'` er uendret): **ny kode og nye
  tester i andre grener må bruke `STATUS.paameldt`, `STATUS_FLERTALL.paameldt`, `db.PAAMELDINGSSTATUSER['paameldt']` og
  `db.statusnavn(...)`** (den tar også databaseverdien `bekreftet`). `db.sett_paamelding_status(..., "bekreftet")` virker fortsatt og
  betyr Påmeldt. Alle nye SELECT-er som gir en rad til `db.paameldingsstatus()` må ta med `ekstradeltaker_ts` (som `avslatt_ts`,
  `utgatt_ts` og `forlatt_ts`). Delte filer som er endret (små, avgrensede endringer): `kurs/db.py` (statusblokken og to UPDATE-er),
  `kurs/web/app.py` (deltakerlisten, statusruten, CSV-eksport, rapportene, `STATUSMERKE`), `kurs/web/static/app.js` (bare
  overbooking-spørsmålet), `kurs/web/templates/admin_kurs_deltakere.html`, `admin_deltaker.html`, `admin_rapport_kurs.html`, og
  `STATUS.bekreftet` -> `STATUS.paameldt` i elleve maler. Gullstandarden (`tests/gullstandard/`) er ikke berørt.
- **Ekstradeltaker: samlingsutvalg og ny telling (migrering 18, `ekstradeltaker_samling`).** Ny tabell sist i begge skjemafilene
  (`ekstradeltaker_samling`, etter `sensitivt`) og `_m18_ekstradeltaker_samling` sist i `MIGRERINGER` (den oppretter tabellen og setter ubehandlede ekstradeltakere fra migrering 16 til «holdt tilbake», `sveiper_utsatt=1`). Tester som teller migreringer må få 18
  med (`tests/test_paameldingsstatus.py`, `tests/test_adresse_personvern.py`, `tests/test_kursside_migrering.py`: «siste migrering» er nå
  `_m18_ekstradeltaker_samling`), og tabelltallet i `tests/test_skjema_speiling.py` øker med 1. **Reglene endres for alle grener som
  teller eller sender:** `db.antall_bekreftet` teller bare påmeldte (ikke ekstradeltakere), og alt som skal ha «alle med plass»
  bruker `db.antall_med_plass`; kursdager per deltaker hentes med `db.paameldingens_kursdager(con, paamelding)` (aldri `db.kursdager`
  når det gjelder en bestemt deltaker: påminnelser, e-post, innsjekk, Min side, kursside, oppmøte, kursbevis, søk); en SQL-teller av
  «påmeldte» må ha `AND ekstradeltaker_ts IS NULL`. Delte filer som er endret (avgrensede endringer): `kurs/db.py` (telling, utvalgsfunksjoner
  og statusovergangene), `kurs/sveiper.py` (ingen automatisk faktura/bekreftelse for ekstradeltaker), `kurs/daglig.py` (påminnelser per
  mottaker), `kurs/deltakerside.py`, `kurs/kursbevis.py`, `kurs/deltakersok.py`, `kurs/assistent.py`, `kurs/kursdatoer.py` (`visning`
  og kursets eget samlingsnummer), `kurs/statustekster.py`, `kurs/deltakerliste.py`, `kurs/kurskalender.py`, `kurs/hendelseslogg.py`,
  `kurs/web/app.py` (tellere, statusruten, tre nye ruter), `kurs/web/static/app.js`, `kurs/maler/kursbevis.html`, og malene
  `admin.html`, `admin_aktiviteter.html`, `admin_kurs_deltakere.html`, `admin_deltaker.html`, `admin_deltaker_ny.html`,
  `admin_kurs_oppsett.html`, `admin_rapporter.html`, `admin_rapport_kurs.html`, `admin_deltakerliste.html`, `admin_allergi.html`,
  `min_side.html`, `kursside_deltaker.html`, `innsjekk_resultat.html` og `base.html` (litt CSS). Nye: `_samlingsvalg.html`,
  `admin_deltaker_ekstra_valg.html`. `«Samling N»` regnes alltid på HELE kursets samlinger (aldri på et filtrert utvalg). Andre runde (oppfølging): `kurs/db.py` (`paameldingens_kursdager` slår opp `ekstradeltaker_ts` selv når raden mangler den, Logger-hendelsen ved registrering, retur fra Ekstradeltaker til Påmeldt), `kurs/deltakerside.py` (`kursinfo_for_innsjekk` med egne dagnumre, `dager_utenfor`), `kurs/sideinnhold.py` (`synlige_fil_ider(dok, idag, utenfor)` har en valgfri tredje parameter), `kurs/web/deltakerside_ruter.py` (filruten), `kurs/maltekster.py` (beskrivelsene av uke-før og dagen-før), `kurs/statustekster.py`, `kurs/hendelseslogg.py`, `kurs/web/app.py` (`_innsjekk_resultat`, QR-siden, plakatens teller), `kurs/web/static/app.js` (type/sted-advarselen), `kurs/web/templates/innsjekk.html`, `admin_bulk_forhandsvisning.html`, `admin_kurs_oppsett.html`, `admin_deltaker.html`, `admin_deltakerliste.html` og `base.html` (CSS: skravert celle i stedet for blek tekst). Tester: ny `tests/test_ekstradeltaker_rest.py` med vakttesten `KURSNIVAA` (hver bruk av `db.kursdager()` og `kursdager_for(kurs)` i `kurs/` er klassifisert: en ny bruk som gjelder en bestemt deltaker må bruke `db.paameldingens_kursdager`), og oppdaterte forventninger i `tests/test_statusvelger_i_listen.py` (42 overganger: en ekstradeltaker har egen melding og ingen automatisk bekreftelse) og `tests/test_status_etiketter.py` (malbeskrivelsene). Tredje runde (rettinger etter kritikernes gjennomgang): `kurs/behandling.py` (holdet for en ekstradeltaker nullstilles bare når bekreftelsen er fullført; `epost_var_sendt` i resultatet), `kurs/db.py` (`_laas_kurs` før plassene telles i `sett_paamelding_status` og `meld_paa`; Påmeldt <-> Ekstradeltaker på lukkede kurs; retur til Påmeldt holder tilbake i stedet for å lage faktura av seg selv; `lagre_samlinger` flytter utvalget til en gjenskapt samling og gir `utvalg_endret`; `samlingsutvalg_for_kurs` i datorekkefølge; `i_setning`), `kurs/kurskalender.py` (ekstradeltakere per samling), `kurs/deltakerliste.py` («Faktureres manuelt»), `kurs/kursbevis.py` og `kurs/maler/kursbevis.html` (filteret `i_setning`), `kurs/migreringer.py` (`_m18` skriver kurs-id-er til hendelsesloggen), `kurs/statustekster.py` (`behandlet=`), `kurs/web/app.py` (versjonskontroll av «Lagre samlinger», avvisning av motstridende samlingsvalg, tidlig avvisning før steget, dialogtekster i vinduet, filteret `i_setning`), `kurs/web/static/app.js` (`data-status-tekster`, samlingsvalget rydder seg selv), og malene `admin_deltaker.html`, `admin_deltaker_ny.html`, `admin_kurs_deltakere.html` og `_samlingsvalg.html`. Ny test: `tests/test_ekstradeltaker_kritikk.py` (én test per funn). Ingen ny migrering: migrering 18 er ikke kjørt noe sted ennå, og endringen i `_m18` er bare en logglinje.
- **Kursside, innsjekk og menyer (Bygg 3, samme gren `claude/kursside`).** Delte filer som er endret, og hva som må passes på ved sammenslåing:
  - `kurs/web/templates/base.html`: deltakermenyen (`{% block nav %}`: «Spør oss» bare når `assistent_aktiv`; «Logg inn» for uinnloggede, «Min side» og «Logg ut» for innloggede, i en egen
    gruppe `nav-konto` til høyre) og noen få CSS-linjer for gruppen (`header nav .nav-konto`, `a.logg-inn`). **Regenerer gullstandarden** (`tests/test_skjemafelt_gullstandard.py::_generer()`) etter
    sammenslåing: menyen står i alle 15 scenarier. Aldri flett JSON for hånd.
  - `kurs/web/templates/admin_base.html`: bare to linjer (fjernet «Offentlig side»; Kunnskapsbase pakket i `{% if assistent_aktiv %}`). «Globalt deltakersøk» legger et søkefelt i samme meny: behold begge endringene.
  - `kurs/web/app.py`: `_globale` (`assistent_aktiv`), innsjekk-delen (`_sjekk_inn`, `_innlogget_deltaker`, `_innsjekk_resultat`, `innsjekk_qr`, `innsjekk_kode`), `min_side()` (skrevet om: eksplisitte kolonner, `min_side_info`,
    fakturaer og dokumenter i egne deler) og `admin_qr` (dag n av m). Kursskjema/påmeldingsskjema-grenene rører ikke disse stedene.
  - `kurs/config.py`: `ASSISTENT_AKTIV` standard `"0"`. `.env.example` og `DEPLOYMENT.md`: `ASSISTENT_AKTIV=0`.
  - `kurs/maltekster.py`: standardteksten i avlysnings-e-posten uten «Spør oss» (koden `{sporsmal_url}` er beholdt). Tester: `test_epost_gullstandard.py`, `test_maltekster.py`, `test_maltekster_integrasjon.py`.
  - `kurs/hendelseslogg.py`: én ny linje (`oppmote_selv`). `kurs/deltakerside.py` og `kurs/web/deltakerside_ruter.py` (innsjekk, «Registrer oppmøte i dag»), `kursside_deltaker.html` og `kursside-delt.css` (svaret i «I dag»-kortet).
  - Skrevet om: `min_side.html`, `innsjekk.html`, `innsjekk_resultat.html`, `admin_qr.html`; to linjer i `logg_inn.html`; ny: `static/min-side.css`, `static/innsjekk.css`, `static/innsjekk-plakat.js`.
  - Tester som er justert (ikke svekket): `test_forhandsvis_paamelding.py` (menyen), `test_sikkerhet.py` (`ASSISTENT_AKTIV` slås på i testen av «Spør oss»). Nye: `test_kursside_innsjekk.py`, `test_kursside_meny.py`.
  - Dokumenter: `README.md`, `OPERATIONS.md`, `GDPR.md`, `SECURITY.md`, `LES MEG FORST.md`, `STATUS-PAMELDINGSSYSTEM.md`, `DEPLOYMENT.md`; nye `dokumentasjon/menyer-forklart.md` og `dokumentasjon/lenke-til-terapiakademiet.md`.
- Kalenderen utleder samlinger av sammenhengende datoer. Når «Opprett kurs» (tabellen `samling`) er merget, bør
  kalenderen og årsplanen bruke de lagrede samlingene i stedet.

## Vern mot forveksling

`migreringer._kontroller_skjema` sjekker at databasen har alle tabeller (ved oppstart) og alle kolonner (når
migreringene kjøres) som skjemafilen beskriver. Har en database blitt migrert av en annen gren med samme nummer,
stopper systemet med en tydelig feil i stedet for å hoppe stille over en migrering.

**Aldri** start en gren med midlertidig nummer mot sandkassedatabasen i `C:\IPR-kurs-sandkasse\data`. Hver arbeidsmappe
(`C:\IPR-kurs-natt\...`) har sin egen `data`-mappe.
