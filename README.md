# IPR Påmeldingssystem

Kurssystem for Institutt for Psykologisk Rådgivning (erstatter Pindena): påmelding, venteliste, fakturering i Visma,
e-post, QR-/Zoom-oppmøte, kursbevis, **Mine kurs** og en egen **Min side** for hvert kurs (program, presentasjoner, grupper,
litteratur, innsjekk og deltakerens egne opplysninger, med en personlig lenke i e-postene) – og et enkelt adminverktøy med roller, årsplan, deltakerlister og rapporter.

> **Status:** klar for utrulling til Azure **sandbox**. Produksjon venter på eksterne avklaringer (Azure-ressurser,
> app-registreringer, Visma-produkt) – se `STATUS-PAMELDINGSSYSTEM.md`. Lokalt kjører alt i demomodus: ingen ekte
> e-post, faktura eller Zoom. **Bruk kun oppdiktede data.**

## Kom i gang (Windows)

Legg mappen utenfor OneDrive, **dobbeltklikk `oppsett.bat`** (én gang per PC), og start med **`start.bat`**
(sjekker først at port 5000 er ledig). Se `LES MEG FORST.md`.

Manuelt i PowerShell:

```powershell
$py = ".\.venv\Scripts\python.exe"
& $py -m kurs.seed_demo        # nullstill og fyll med demodata
& $py kjor.py                  # http://127.0.0.1:5000  (admin: brukernavn admin, passord demo)
& $py -m pytest tests          # testene (SQLite). Mot PostgreSQL: se OPERATIONS.md
& $py -m kurs.daglig --tor     # vis hva morgenjobben ville gjort
& $py -m kurs.migrer --status  # databasens versjon
```

## Hva finnes

**For deltakere**

| Side | Hva |
|---|---|
| `/kurs/<kode>` | Påmeldingsskjema, satt opp per kurs i skjemabyggeren (standardfelt, egne felt, «vis bare når»). Firma betaler: firmanavn og adresse hentes fra Brønnøysundregistrene (skrives aldri inn – samme regel for offentlig skjema, adminregistrering, bedriftspåmelding, webhook og filimport; svarer registeret ikke, går påmeldingen gjennom med organisasjonsnummeret alene, merkes «Firmaopplysninger må kontrolleres», og fakturaen venter). Venteliste, allergier på fysiske kurs. **Deltakerens private adresse (adresse, postnummer, poststed) MÅ være med** ved alle nye påmeldinger, uansett vei: det offentlige skjemaet, bedriftspåmeldingen (per deltaker), «Legg til deltaker», webhooken og CSV-importen, også når arbeidsgiver betaler (fakturaen sendes automatisk ved påmelding, og da må adressen være klar). Adressefeltene i skjemabyggeren er låst. Webhooken og CSV-importen har hver sin innstilling (`ADRESSE_KREVES_I_WEBHOOK` og `ADRESSE_KREVES_I_CSV`), **på som standard**; `0` er en midlertidig nødbrems – se `OPERATIONS.md` 2f. Eldre deltakere og personer registrert uten adresse er merket «Privat adresse mangler» (deltakerlisten og deltakervinduet) og kan lagres uten adressen inntil den legges inn. Betaler deltakeren selv, faktureres denne adressen, og en privat faktura lages aldri uten komplett adresse: den holdes tilbake (fakturastatus «Venter på privat adresse») til adressen er lagt inn. Samme adresse er forhåndsvisningen for admin |
| `/kurs/<kode>/gruppe` | Bedriftspåmelding av flere deltakere, med kvittering |
| `/logg-inn` | **Én innloggingsside** for deltakerne (ingen passord: e-postlenke). Terapiakademiet.no lenker hit med en vanlig lenke; deltakeren havner på riktig Min side (`?neste=`). Tekst og adresse til å lime inn: `dokumentasjon/lenke-til-terapiakademiet.md` |
| `/kurs/<kode>/deltakerside` | **Min side** (for ett kurs): program, presentasjoner, grupper, litteratur, praktisk informasjon, innsjekk og kortet «Mine opplysninger» (det deltakeren selv ga ved påmeldingen), bare for deltakere med **bekreftet** påmelding på akkurat det kurset. Redigeres i admin (fanen Min side) |
| `/min/<lenke>` | Deltakerens **personlige lenke** til Min side (knappen «Åpne Min side» i bekreftelsen og påminnelsene). Signert og aldri lagret; virker til 30 dager etter siste kursdag; gir alt unntatt privatadresse, fakturaer og kursbevis (de krever bekreftet e-post). Kan stenges og fornyes i deltakervinduet |
| `/min-side` | **Mine kurs**: deltakerens oversikt etter innlogging med e-post: ett kort per kurs (Åpne Min side, Registrer oppmøte i dag) og «Min konto» (fakturaer, kursbevis og dokumenter, timer i spesialistløp) |
| `/innsjekk/<token>` | Innsjekk i kurslokalet: skann QR-koden (e-post, eller ett trykk når du er innlogget). Én registrering per kursdag. Skanning gir aldri innlogging. Reserve på nett: «Registrer oppmøte i dag» på Min side og under Mine kurs, med dagens kode (den gamle kodesiden `/innsjekk` er fjernet) |
| `/sporsmal` | «Spør oss» – velger bare blant godkjente svar. **Av som standard** (`ASSISTENT_AKTIV=1` slår den på, sammen med Kunnskapsbase i admin) |
| `/lever/<id>/<signatur>` | Kursholder laster opp materiell (signert lenke i purre-e-posten) |
| `/api/paamelding` | Mottak fra eksterne skjema (HMAC-signert). Krever `fornavn`, `etternavn`, `epost` og `kurs`. Deltakerens private adresse (`adresse`, `postnr` og `poststed`, også når arbeidsgiver betaler) **må være med**: mangler den eller noe av den, svarer webhooken 400 med feltene som mangler, og ingenting registreres (`ADRESSE_KREVES_I_WEBHOOK=1`, standard). `0` er en midlertidig nødbrems for overgangsperioden (av som standard): da går påmeldingen ikke tapt, men tas inn selv om adressen mangler (en delvis adresse tas vare på som ufullstendige data) og merkes «Privat adresse mangler» eller «Privat adresse er ufullstendig», og fakturaen holdes tilbake til alle tre feltene er lagt inn. Før produksjon **må** ipr.no-skjemaet sende de tre feltene: se `OPERATIONS.md` 2f (med curl-test). Fullt navn i ett felt avvises. De gamle feltene `faktura_adresse`/`faktura_postnr`/`faktura_sted` leses ikke lenger: betaler deltakeren selv, faktureres `adresse`; betaler firma (`org_nr` er sendt), faktureres firmaets adresse fra Enhetsregisteret |

**For administrasjonen** (`/admin`, roller: systemadministrator / kursadministrator / lesetilgang)

| Område | Hva |
|---|---|
| Startsiden `/` | Innloggingen til Admin og ingenting annet (ingen kursliste): den enkle adressen som kan deles med alle som bruker systemet. Allerede innlogget sendes man til Oversikten |
| Oversikt | Forsiden i Admin: deltakersøk og kurssøk side ved side, kurslisten med søk, filtre og sortering (Aktiviteter er slått sammen med Oversikten), og under den statuskortene (og det som trenger oppfølging) |
| **Deltakersøk** | Søkefeltet på Oversikten (og på søkesiden; «/» hopper dit fra alle adminsider): finn en person på tvers av alle kurs på navn (også uten æøå), e-post (også limt inn fra Outlook), telefon, HPR-nummer, påmeldingsnummer (`#12`) eller kursnummer/kurskode (fra fire tegn). Rullegardin mens du skriver (`/` hopper til feltet), resultatsiden `/admin/sok` viser alle påmeldinger med oppmøte og kursbevis – utstedt, eller hvorfor ikke |
| Kalender | Egen fane: kursene som kalender (kursoversikt, måned og uke), **årsplan** (hele året, planlagte aktiviteter/notater, overlapp, ledige perioder) og **planlagte kurs** (kursplanen som ikke er opprettet i systemet ennå, med én farge per kurs; ikke på Oversikten), med **sjekkliste per samling** på alle kurs unntatt IPRO, også kursene i systemet, fra **sjekklistemaler** (frister fra datoene, forfalt i rødt; et klikk på kurset folder den ut), **kursholdere** (som «Kommende kurs» i Excel: rolle per samling, rød = ikke i Psybase, dobbeltbooking i gult) og **påminnelse** (kort på Oversikten og morgen-e-post til kurs@ipr.no). «Nytt kurs +» står på Oversikten |
| Kurs | Oppsett, nettside (tekster), påmeldingsskjema, **Min side** (blokkeditor med utkast/publisering, filer og forhåndsvisning), deltakere, kommunikasjon. Permanent **kursnummer**. Duplisering. QR-plakat per kursdag (Oppsett → «Vis QR på skjerm») |
| Deltakere | Registrering (fornavn og etternavn hver for seg), import (CSV), bulkbehandling, e-post til utvalg med klikkbare flettefelt (`{fornavn}`, `{navn}`), oppmøte, **avslag**, **deltakerliste med kolonnevalg** (utskrift/PDF/CSV) |
| Rapporter | Kurs, deltakerregister, person, **økonomi og fakturaliste** |
| Uavklarte operasjoner | E-post/faktura med ukjent utfall – avklares etter kontroll i Outlook/Visma |
| E-postmaler | Redigerbare tekster. (Kunnskapsbase, godkjente svar til «Spør oss», vises bare når «Spør oss» er slått på) |
| Brukere, Daglig kjøring | Kun systemadministrator. Brukere har øverst «Lenke til innloggingen» (med «Kopier lenke») som deles med kolleger |

## Automatikk

**Ved påmelding:** bekreftelse eller ventelistebeskjed → faktura i Visma, med deltakerens adresse (samlet, per samling, eller utsatt når
første kursdag ligger lenger enn seks kalendermåneder fram: fakturaen lages da tidligst seks måneder før første kursdag).

**Hver morgen** (`python -m kurs.daglig`, idempotent – trygg å kjøre flere ganger):

1. Prøver på nytt å hente firmanavn og adresse fra Enhetsregisteret for påmeldinger merket «Firmaopplysninger må kontrolleres» (før fakturaene)
2. Plukker opp påmeldinger der noe feilet, delfakturaer som forfaller og holdte fakturaer
3. Oppretter Zoom-møte ~8 dager før digitale/hybride kurs
4. Velkomst uka før + innkalling dagen før hver kursdag
5. Setter kursstatus og importerer Zoom-oppmøte
6. Kursbevis til dem som har møtt
7. Purrer kursholdere på materiell og varsler admin ved fristbrudd
8. Sletter allergi-/tilretteleggingsopplysninger 14 dager etter kurset
9. Rydder utløpte import-forhåndsvisninger
10. Tømmer gruppelister (tabeller med navn) på Min side 30 dager etter siste kursdag

Hvert steg og kurs kjøres for seg – en feil stopper aldri resten. All e-post og fakturering går gjennom en
claim-/statusmodell: ingen får samme e-post to ganger, ingen påmelding faktureres to ganger, og uklare utfall prøves
aldri automatisk på nytt.

## Arkitektur

```
kurs/
  schema.sql, schema_postgres.sql   datamodellen (sluttresultatet) – speiler hverandre
  migreringer.py, migrer.py         versjonerte migreringer:  python -m kurs.migrer
  db.py                             databaselaget (SQLite lokalt, PostgreSQL når DATABASE_URL er satt)
  sveiper.py, behandling.py         «når noen melder seg på» (e-post + faktura)
  daglig.py, kjoring.py             morgenjobben og felles sende-/claim-motor
  kursbevis.py, aarsplan.py, deltakerliste.py, okonomi.py
  maltekster.py, skjemafelt.py, ekstrafelt.py, paameldingsside.py, import_deltakere.py
  sideinnhold.py, sidelager.py      Min side: innholdsmodell og lagring (utkast/publisert, filer, versjoner)
  deltakerside.py                   Min side: tilgang, landing etter innlogging, visningsdata og innsjekk
  minside.py                        Min side: den personlige lenken, tilgangsnivåene og «Mine opplysninger»
  tema.py                           Terapiakademiet-drakten for deltakersidene: logofiler (stilen: web/static/tema-terapiakademiet.css)
  lenker.py, feil.py, assistent.py, portsjekk.py, seed_demo.py
  integrasjoner/                    ALT som snakker med omverdenen – med demo-gren
    epost.py, sharepoint.py, m365.py (Microsoft Graph), zoom.py, visma.py, brreg.py (Brønnøysundregistrene)
  maler/epost/                      e-posttekstene
  web/                              Flask-app, sikkerhet (CSRF/CSP/takbegrensning), Entra ID, maler, static/app.js,
                                    kursside_admin_ruter.py og deltakerside_ruter.py (Min side)
tests/                              ~2 500 tester, kjøres mot både SQLite og PostgreSQL
```

## Dokumentasjon

| Fil | Innhold |
|---|---|
| `STATUS-PAMELDINGSSYSTEM.md` | Hvor prosjektet står, hva som gjenstår, eksterne blokkeringer |
| `DEPLOYMENT.md` | Miljøer, alle innstillinger, utrulling, morgenjobben |
| `AZURE-SETUP.md` | Azure/Microsoft 365/Zoom/Visma-oppsett for IT, med minste tilganger |
| `OPERATIONS.md` | Daglig drift, uavklarte operasjoner, nøkler, backup. Også: Min side (med den personlige lenken), innsjekk, lenke fra terapiakademiet.no |
| `dokumentasjon/menyer-forklart.md` | Enkel forklaring av menyene: Startsiden, Oversikt, Kalender, Kunnskapsbase, Daglig kjøring, Innsjekk |
| `dokumentasjon/Min side - slik gjør du.md` | Én side om å bygge og publisere Min side, og om den personlige lenken |
| `dokumentasjon/lenke-til-terapiakademiet.md` | Adresse og tekst til å lime inn på terapiakademiet.no |
| `MIGRATIONS.md` | Databasemigreringer og rollback |
| `SECURITY.md` | Sikkerhetstiltak og kjente begrensninger |
| `GDPR.md` | Personopplysninger, lagringstid, rettigheter (inkl. sletting) |
| `CLAUDE.md` | Regler for videreutvikling med Claude Code |
