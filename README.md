# IPR Påmeldingssystem

Kurssystem for Institutt for Psykologisk Rådgivning (erstatter Pindena): påmelding, venteliste, fakturering i Visma,
e-post, QR-/Zoom-oppmøte, kursbevis, Min side for deltakere – og et enkelt adminverktøy med roller, årsplan,
deltakerlister og rapporter.

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
| `/` og `/kurs/<kode>` | Kursoversikt og påmeldingsskjema (faktura til seg selv/arbeidsgiver, EHF, venteliste, allergier på fysiske kurs, konfigurerbare felt per kurs) |
| `/kurs/<kode>/gruppe` | Bedriftspåmelding av flere deltakere, med kvittering |
| `/innsjekk/<token>`, `/innsjekk` | QR-innsjekk og innsjekk med kode |
| `/min-side` | Innlogging med e-postlenke: kurs, oppmøte, kursmateriell, kursbevis, fakturaer |
| `/sporsmal` | «Spør oss» – velger bare blant godkjente svar (kan slås av) |
| `/lever/<id>/<signatur>` | Kursholder laster opp materiell (signert lenke i purre-e-posten) |
| `/api/paamelding` | Mottak fra eksterne skjema (HMAC-signert) |

**For administrasjonen** (`/admin`, roller: systemadministrator / kursadministrator / lesetilgang)

| Område | Hva |
|---|---|
| Oversikt | Kommende kurs øverst, status og uavklarte operasjoner under |
| Aktiviteter | Liste med søk (navn eller kursnummer), kalender og **årsplan** (hele året, planlagte aktiviteter/notater, overlapp, ledige perioder). «Nytt kurs +» |
| Kurs | Oppsett, nettside (tekster), påmeldingsskjema, deltakere, kommunikasjon. Permanent **kursnummer**. Duplisering |
| Deltakere | Registrering, import (CSV), bulkbehandling, e-post til utvalg, oppmøte, **avslag**, **deltakerliste med kolonnevalg** (utskrift/PDF/CSV) |
| Rapporter | Kurs, deltakerregister, person, **økonomi og fakturaliste** |
| Uavklarte operasjoner | E-post/faktura med ukjent utfall – avklares etter kontroll i Outlook/Visma |
| E-postmaler, Kunnskapsbase | Redigerbare tekster, godkjente svar |
| Brukere, Daglig kjøring | Kun systemadministrator |

## Automatikk

**Ved påmelding:** bekreftelse eller ventelistebeskjed → faktura i Visma (samlet, per samling, eller holdt tilbake når
kurset er mer enn seks måneder frem).

**Hver morgen** (`python -m kurs.daglig`, idempotent – trygg å kjøre flere ganger):

1. Plukker opp påmeldinger der noe feilet, delfakturaer som forfaller og holdte fakturaer
2. Oppretter Zoom-møte ~8 dager før digitale/hybride kurs
3. Velkomst uka før + innkalling dagen før hver kursdag
4. Setter kursstatus og importerer Zoom-oppmøte
5. Kursbevis til dem som har møtt
6. Purrer kursholdere på materiell og varsler admin ved fristbrudd
7. Sletter allergi-/tilretteleggingsopplysninger 14 dager etter kurset
8. Rydder utløpte import-forhåndsvisninger

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
  maltekster.py, skjemafelt.py, paameldingsside.py, import_deltakere.py
  lenker.py, feil.py, assistent.py, portsjekk.py, seed_demo.py
  integrasjoner/                    ALT som snakker med omverdenen – med demo-gren
    epost.py, sharepoint.py, m365.py (Microsoft Graph), zoom.py, visma.py
  maler/epost/                      e-posttekstene
  web/                              Flask-app, sikkerhet (CSRF/CSP/takbegrensning), Entra ID, maler, static/app.js
tests/                              ~2 500 tester, kjøres mot både SQLite og PostgreSQL
```

## Dokumentasjon

| Fil | Innhold |
|---|---|
| `STATUS-PAMELDINGSSYSTEM.md` | Hvor prosjektet står, hva som gjenstår, eksterne blokkeringer |
| `DEPLOYMENT.md` | Miljøer, alle innstillinger, utrulling, morgenjobben |
| `AZURE-SETUP.md` | Azure/Microsoft 365/Zoom/Visma-oppsett for IT, med minste tilganger |
| `OPERATIONS.md` | Daglig drift, uavklarte operasjoner, nøkler, backup |
| `MIGRATIONS.md` | Databasemigreringer og rollback |
| `SECURITY.md` | Sikkerhetstiltak og kjente begrensninger |
| `GDPR.md` | Personopplysninger, lagringstid, rettigheter (inkl. sletting) |
| `CLAUDE.md` | Regler for videreutvikling med Claude Code |
