# IPR Kurs – prototype for kursautomasjon

Erstatning for Pindena: påmelding, fakturering (Visma), e-post, QR-innsjekk, kursbevis og «Min side» for deltakere.
Bygget etter samme mønster som Stians referanseløsning (små Python-moduler, én database, én daglig jobb),
utvidet med webapp, Microsoft 365-integrasjon og deltakerportal.

> **Status:** prototype i demomodus. Ingen ekte e-post, faktura eller Zoom. **Bruk kun oppdiktede data.**

## Kom i gang (Windows)

Enklest: pakk ut / legg mappen utenfor OneDrive, og **dobbeltklikk `oppsett.bat`** (én gang per PC).
Start deretter med **`start.bat`**. Se `LES MEG FORST.md`.

Manuelt i PowerShell:

```powershell
py -3.12 -m venv .venv                       # én gang
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

$py = ".\.venv\Scripts\python.exe"
& $py -m kurs.seed_demo        # nullstill og fyll med demodata
& $py kjor.py                  # åpne http://127.0.0.1:5000  (admin: brukernavn admin, passord demo)
& $py -m pytest tests          # kjør testene
& $py -m kurs.daglig --tor     # vis hva morgenjobben ville gjort
```

## Hva finnes

| Side | For hvem | Hva |
|---|---|---|
| `/` og `/kurs/<kode>` | Deltakere | Kursoversikt og påmeldingsskjema (faktura til seg selv/arbeidsgiver, EHF, allergier for fysiske kurs, venteliste) |
| `/innsjekk/<token>` | Deltakere | Målet for QR-koden. Husker telefonen etter første gang |
| `/innsjekk` | Deltakere | Taste kode + e-post (for de som ikke får skannet) |
| `/min-side` | Deltakere | Innlogging med e-postlenke. Kurs, oppmøte per dag, kursmateriell, kontrakter, kursbevis, faktura, timer i spesialistløp |
| `/sporsmal` | Deltakere | KI-assistent. Viser **kun godkjente svar ordrett** + fakta fra databasen. Alt usikkert eller sensitivt går til adm |
| `/api/paamelding` | Nettside/skjema | Mottakspunkt (webhook) for påmeldinger fra eksterne skjema. Signert med hemmelighet |
| `/lever/<id>` | Kursholdere | Laste opp presentasjon → blir tilgjengelig for deltakerne |
| `/admin` | Adm | Oversikt, deltakerlister, oppmøtematrise, QR på storskjerm, allergiliste, CSV-eksport, nytt kurs, daglig kjøring (med «spol dato» i demo), utboks |

## Automatikk

**Ved påmelding** (straks): bekreftelse eller venteliste-e-post → faktura i Visma.
**Hver morgen** (`kurs/daglig.py`, idempotent – trygg å kjøre flere ganger):

1. Plukker opp påmeldinger der noe feilet
2. Oppretter Zoom-møte ~8 dager før digitale/hybride kurs
3. Velkomst-e-post uka før + Zoom-lenke/info dagen før hver kursdag
4. Setter kursstatus og importerer Zoom-oppmøte
5. Kursbevis til de som har møtt
6. Purrer kursholdere på materiell (7 og 2 dager før, fristdag) og varsler adm ved fristbrudd
7. Sletter allergi-/helseopplysninger 14 dager etter kurset

All utsending logges i `utsending_logg` med unik nøkkel, så ingen får samme e-post to ganger.

## KI-assistenten – hvorfor den ikke kan gi feil svar

KI-en (Claude) skriver aldri svar selv. Den får bare **velge** blant godkjente svar i kunnskapsbasen
(`/admin/kunnskap`) og faktasetninger generert fra kursdatabasen (datoer, sted, egen påmelding, faktura).
Det deltakeren ser er de valgte tekstene ordrett. Sikringer: nøkkelord for sensitive tema går rett til adm,
utkast og utløpte svar brukes aldri, svar-ID-er valideres, og enhver feil eller usikkerhet → til adm.
Adm gjør ubesvarte spørsmål om til nye godkjente svar med ett klikk. I demo brukes ordmatching i stedet for KI.
Se `kurs/assistent.py`.

## Nettsiden ipr.no

Kurssidene på ipr.no (Craft CMS) har i dag en «Booking»-lenke til Pindena – ikke et skjema. Omkobling = bytte
~45 lenker til `/kurs/<kode>` i denne tjenesten. `/api/paamelding` finnes hvis dere senere vil ha skjema på selve nettsiden.

## Arkitektur

```
kurs/
  schema.sql            datamodell (les denne først)
  db.py                 all databaselogikk (påmelding, venteliste, oppmøte, dedup)
  sveiper.py            «når noen melder seg på»
  daglig.py             morgenjobben
  kursbevis.py
  kjoring.py            felles kontekst: dato-overstyring + tørrkjøring
  integrasjoner/        ALT som snakker med omverdenen – har demo- og prod-gren
    epost.py            Microsoft Graph (demo: utboks/)
    sharepoint.py       Microsoft Graph (demo: data/sharepoint_demo/)
    visma.py            Visma – produkt må bekreftes
    zoom.py             Zoom Server-to-Server OAuth
    m365.py             felles Microsoft-token
  maler/epost/          e-posttekstene (kan redigeres uten å kunne Python)
  web/                  Flask-app + HTML-maler
tests/
```

## Veien til drift (Azure)

- [ ] Bekrefte Visma-produkt og API-tilgang
- [ ] IT: Azure-ressurs (App Service/Container Apps), database (Azure SQL/PostgreSQL), Key Vault
- [ ] IT: App-registrering i Entra ID (Mail.Send begrenset til kurs-postboks, Sites.Selected på kurs-siten)
- [ ] Bytte SQLite → Azure SQL/PostgreSQL
- [x] Admin-innlogging: egne brukere med hashet passord (ikke koblet til Microsoft)
- [ ] Signerte lenker for kursholder-opplasting
- [ ] Planlagt kjøring av `kurs.daglig` kl 07:00
- [ ] Personvern: DPIA, databehandleravtaler (Zoom, Microsoft, Visma), personvernerklæring, sletterutiner
- [ ] Publiseringsløype GitHub → Azure
