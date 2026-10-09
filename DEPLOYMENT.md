# Utrulling og konfigurasjon

Hvordan systemet kjøres i de tre miljøene, hvilke innstillinger som trengs, og rekkefølgen ved en utrulling.
Oppsett av selve Azure-ressursene (for IT) står i `AZURE-SETUP.md`, drift i `OPERATIONS.md`, migreringer i
`MIGRATIONS.md`.

## 1. Miljøer

| | Lokalt (sandkasse) | Sandbox (Azure) | Produksjon (Azure) |
|---|---|---|---|
| Formål | Prøve og utvikle | Teste mot ekte tjenester med testdata | Ekte drift |
| `MODUS` | `demo` | `prod` | `prod` |
| Database | SQLite `data/kurs.db` (`DATABASE_URL` tom) | Azure Database for PostgreSQL i `pameldingssystem-sandbox-rg` | PostgreSQL i `pameldingssystem-prod-rg` |
| Webserver | `kjor.py` / `start.bat` (Flask, kun 127.0.0.1) | App Service Linux, gunicorn | App Service Linux, gunicorn |
| E-post / faktura / Zoom | Liksom (utboks, demo-grener) | Ekte, **mot testkonto/testpostboks/Visma-sandbox** | Ekte |
| Admin-innlogging | Brukernavn/passord (`admin`/`demo`) | Microsoft Entra ID (+ ev. nødbruker) | Microsoft Entra ID (+ ev. nødbruker) |
| Data | Oppdiktet (`kurs.seed_demo`) | Oppdiktet / testpersoner | Ekte |

**Sandbox først.** Ingenting settes opp i produksjon før sandbox har kjørt stabilt (se sjekklisten i 5).

## 2. Innstillinger (App Settings / miljøvariabler)

I Azure settes alt som App Settings på web-appen. Hemmelige verdier legges i Key Vault og refereres med
`@Microsoft.KeyVault(SecretUri=...)`. Lokalt kan `.env` brukes (se `.env.example`) – `.env` skal aldri i git.

| Navn | Lokalt | Sandbox/prod | Hemmelig | Forklaring |
|---|---|---|---|---|
| `MODUS` | `demo` | `prod` | | `prod` = ekte integrasjoner og strenge kontroller |
| `BASE_URL` | `http://127.0.0.1:5000` | **påkrevd**, `https://…` | | Brukes i lenker i e-post (den personlige lenken til Min side, Mine kurs, opplasting, Entra-retur). Må være https i drift |
| `HEMMELIG_NOKKEL` | valgfri | **påkrevd**, ≥ 32 tilfeldige tegn | ✔ | Signerer økter og lenker (også den personlige lenken til Min side i e-postene). Bytte = alle logges ut og gamle lenker blir ugyldige |
| `DATABASE_URL` | tom | **påkrevd** | ✔ | `postgresql://…?sslmode=require`. Tom = SQLite |
| `DB_STI` | valgfri | – | | SQLite-fil (bare lokalt) |
| `WEBHOOK_HEMMELIG` | valgfri | **påkrevd**, ≥ 32 tegn | ✔ | Signatur for `/api/paamelding` |
| `ADRESSE_KREVES_I_WEBHOOK` | `1` | `1` (standard) | | Deltakerens private adresse (`adresse`, `postnr`, `poststed`) **må være med** i webhooken: mangler den (eller noe av den), svarer webhooken 400, og ingenting registreres. Bare `0` slår kravet av, som en **nødbrems** som bare skal brukes midlertidig (av som standard; da tas påmeldingen inn selv om adressen mangler (en delvis adresse tas vare på), merket «Privat adresse mangler/er ufullstendig», og fakturaen holdes tilbake til alle tre feltene er lagt inn). Før produksjon **må** ipr.no-skjemaet/pluginen sende de tre feltene og være testet med curl: se `OPERATIONS.md` 2f |
| `ADRESSE_KREVES_I_CSV` | `1` | `1` (standard) | | Samme for CSV-importen: `1` (standard) = filen må ha kolonnene Adresse, Postnr og Poststed, og hver rad hele adressen (ellers avvises filen eller blokkeres raden). Bare `0` slår kravet av (nødbrems, midlertidig). Se `OPERATIONS.md` 2f |
| `ADMIN_BRUKERNAVN`, `ADMIN_PASSORD` | `admin`/`demo` | Første nødbruker, passord ≥ 12 tegn | ✔ (passord) | Brukes **bare** når databasen ikke har noen admin-brukere (første migrering) |
| `ADMIN_LOKAL_INNLOGGING` | `1` | `0` (anbefalt) eller `1` for nødbruker | | `0` = kun Microsoft-innlogging |
| `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID` | – | **påkrevd** hvis Entra | | App-registreringen for admin-innlogging |
| `ENTRA_CLIENT_SECRET` | – | påkrevd hvis Entra uten sertifikat | ✔ | |
| `ENTRA_ROLLER` | standard | valgfri | | App-rolle → rolle, standard `ipr.system=system,ipr.kursadmin=kursadmin,ipr.lese=lese` |
| `ENTRA_GRUPPER` | – | valgfri | | Alternativ: `<gruppe-objekt-id>=rolle,…` |
| `M365_TENANT_ID`, `M365_CLIENT_ID` | – | **påkrevd** | | App-registreringen for e-post (Graph, applikasjonstillatelser) |
| `M365_CLIENT_SECRET` | – | påkrevd uten sertifikat | ✔ | Erstattes av `M365_SERTIFIKAT` (anbefalt) |
| `M365_SERTIFIKAT` | – | **påkrevd** (anbefalt framfor secret) | ✔ | Key Vault-referanse til sertifikatet (PEM eller base64 PFX). Brukes framfor `M365_CLIENT_SECRET` når satt. `AZURE-SETUP.md` 5a |
| `M365_SERTIFIKAT_PASSORD` | – | valgfri | ✔ | Bare for en PFX med passord (Key Vault lager dem uten) |
| `ENTRA_SERTIFIKAT`, `ENTRA_SERTIFIKAT_PASSORD` | – | valgfri | ✔ | Sertifikat for innloggingen. Tom = M365-sertifikatet når innloggingen bruker samme registrering (ingen egen `ENTRA_CLIENT_ID`) |
| `AVSENDER_EPOST`, `AVSENDER_NAVN` | standard | **påkrevd** | | Postboksen e-post sendes fra (kurs@ipr.no i prod, en testpostboks i sandbox) |
| `ADMIN_EPOST` | standard | **påkrevd** | | Mottaker av «til adm»-henvendelser |
| `SJEKKLISTE_EPOST` | standard | valgfri | | Mottaker av morgen-e-posten om sjekklistene for planlagte kurs, standard `kurs@ipr.no` |
| `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID` | – | påkrevd for digitale kurs | | Zoom Server-to-Server OAuth |
| `ZOOM_CLIENT_SECRET` | – | påkrevd for digitale kurs | ✔ | |
| `VISMA_CLIENT_ID` | – | påkrevd for fakturering | | Visma-produktet er **ikke avklart** (se `kurs/integrasjoner/visma.py`) |
| `VISMA_CLIENT_SECRET`, `VISMA_REFRESH_TOKEN` | – | påkrevd for fakturering | ✔ | Refresh-tokenet er bare **startverdien**; det roterer og lagres deretter i databasen (`integrasjon_token`) |
| `VISMA_API` | standard | valgfri | | Standard: eAccounting v2 |
| `ANTHROPIC_API_KEY` | – | valgfri | ✔ | Uten nøkkel: ordmatching mot kunnskapsbasen |
| `ASSISTENT_AKTIV` | `0` | `0` | | `0` (standard) = «Spør oss» og Kunnskapsbase er av: menyvalgene er borte og `/sporsmal` gir 404. `1` slår begge på igjen. En `ASSISTENT_AKTIV=1` som står i `.env` eller i Azure-innstillingene holder dem i live, så fjern den hvis de skal være av |
| `ASSISTENT_MODELL`, `ASSISTENT_MAKS_PER_DAG`, `ASSISTENT_TIDSAVBRUDD_SEK` | standard | valgfri | | Kostnadsgrense og tidsavbrudd for KI |
| `INNSJEKK_KREVER_INNLOGGING` | `0` | `0` | | `0` (standard) = en deltaker kan sjekke inn med QR-koden og e-postadressen alene. `1` = uinnloggede må logge inn først (engangslenke på e-post, eller den personlige lenken til Min side fra e-postene) og trykker så én knapp: hindrer at noen registrerer en fraværende kollega, men er tregere i lokalet. Se `OPERATIONS.md` 2d |
| `SLETT_SENSITIVT_ETTER_DAGER` | `14` | `14` | | Allergier/tilrettelegging slettes så mange dager etter siste kursdag |
| `KURSSIDE_ETTERTILGANG_DAGER` | `180` | `180` | | Standard åpningstid: Min side er åpen for deltakerne til siste kursdag + så mange dager. Hvert kurs kan ha egen verdi (1-3650 dager), en bestemt dato eller ingen tidsbegrensning under Min side → Innstillinger for siden. (Den personlige lenken i e-postene virker uansett bare til 30 dager etter siste kursdag.) |
| `KURSSIDE_TOM_TABELLER_ETTER_DAGER` | `30` | `30` | | Gruppelister (tabeller med navn) på Min side tømmes så mange dager etter siste kursdag (morgenjobben, steg 9) |
| `KURSSIDE_FIL_MAKS_MB`, `KURSSIDE_BILDE_MAKS_MB`, `KURSSIDE_KURS_MAKS_MB`, `KURSSIDE_MAKS_FILER` | `15`, `5`, `100`, `200` | standard | | Grenser for filer på Min side: per dokument, per bilde, per kurs (MB) og antall filer per kurs. Filene lagres i databasen |

I drift nekter appen å svare (503 og en linje i loggen) hvis `HEMMELIG_NOKKEL`/`WEBHOOK_HEMMELIG` er svake, `BASE_URL`
ikke er https, det ikke finnes noen innloggingsvei for admin, eller databasen har feil versjon.

## 3. Web-appen

- Kjøretid: Python 3.12 på App Service **Linux**.
- Startkommando (App Service → Configuration → General settings):
  ```text
  gunicorn --bind=0.0.0.0 --timeout 600 startup:app
  ```
  `startup.py` importerer bare appen. **Gunicorn migrerer aldri** – det er et eget steg (4).
  Uten `--workers` kjører gunicorn **én** arbeidsprosess: en enkelt langvarig forespørsel (tidsgrensen er 600 s) blokkerer da hele tjenesten, også offentlig påmelding. Kjente treige kall på Min side
  (HTML-rensing, filnedlasting) er begrenset (se `SECURITY.md`), men vurder `--workers 2`. Takbegrensningen teller da per prosess (samme avsnitt).
- `requirements.txt` installeres av App Service ved utrulling (Oryx). `gunicorn` og `psycopg[binary]` er med.
- HTTPS og TLS-terminering gjøres av App Service. Appen bruker `ProxyFix`, så `BASE_URL` må være den offentlige
  https-adressen.
- Lokal disk brukes ikke til varige data i drift: kursbevis og filene på Min side ligger i databasen.

## 4. Rekkefølge ved utrulling

1. Kjør testene lokalt mot SQLite **og** PostgreSQL (`OPERATIONS.md`, «Tester»). Alt skal være grønt.
2. Ta sikkerhetskopi av databasen (`MIGRATIONS.md`, prosedyre steg 2).
3. Rull ut koden til App Service (GitHub → App Service, eller zip-utrulling fra `main`).
4. Kjør migreringen fra App Service-konsollen (SSH) eller som eget steg i utrullingen:
   ```text
   python -m kurs.migrer --status
   python -m kurs.migrer
   ```
   Appen svarer 503 fra koden er rullet ut til migreringen er kjørt – normalt sekunder.
5. Kontroller: `python -m kurs.migrer --status` gir 0, admin åpner, og `python -m kurs.daglig --tor` kjører uten feil.
6. Rollback: se `MIGRATIONS.md` (forrige kode **og** gjenopprett sikkerhetskopien hvis migreringen endret data).

## 5. Morgenjobben (planlagt kjøring)

`python -m kurs.daglig` skal kjøres **én gang daglig, kl. 07:00 norsk tid**, med samme kode og samme App Settings som
web-appen. Den er idempotent (trygg å kjøre to ganger) og gir avslutningskode **1** hvis ett eller flere steg feilet
(resten er likevel kjørt) – sett opp varsling på det.

App Service Linux har ingen innebygd timer for Python-kode. Anbefalt (IT velger):

| Alternativ | Vurdering |
|---|---|
| **Azure Container Apps Job** (cron `0 5 * * *` UTC om vinteren / `0 4 * * *` om sommeren, eller med tidssone) | Anbefalt. Samme kode/image, egen kjøring, logger og varsling. Krever container-bygg |
| Azure Functions (timer trigger) som kjører `kurs.daglig.main()` | Greit, men egen Functions-app med samme avhengigheter og innstillinger |
| Planlagt oppgave på en server/VM som kjører kommandoen mot databasen | Enkelt, men krever en maskin og nettverkstilgang til databasen |

Ikke kjør jobben inne i web-appen (flere gunicorn-workere/instanser → flere samtidige kjøringer; jobben tåler det,
men det gir unødvendig støy). Admin kan bare **tørrkjøre** jobben fra nettleseren i drift.

## 6. Sjekkliste før produksjon

- [ ] Sandbox: hele flyten testet med testpersoner (påmelding → bekreftelse → faktura i Visma-sandbox → innkalling →
      Zoom → oppmøte → kursbevis → avlysning)
- [ ] Alle hemmeligheter i Key Vault, ingen i App Settings i klartekst
- [ ] `MODUS=prod`, `BASE_URL=https://…`, `ADMIN_LOKAL_INNLOGGING=0` (eller bevisst nødbruker)
- [ ] Entra-app med app-roller tildelt riktige ansatte (`AZURE-SETUP.md`)
- [ ] Graph-tillatelser begrenset til kurs-postboksen og kurs-siten (`AZURE-SETUP.md`)
- [ ] Visma-produkt avklart og testet mot sandbox (`kurs/integrasjoner/visma.py`)
- [ ] Morgenjobben planlagt, med varsling ved avslutningskode ≠ 0
- [ ] Sikkerhetskopi: automatisk backup av PostgreSQL (minst 7 dager) og testet gjenoppretting
- [ ] Personvern: punktene i `GDPR.md` avsnitt 6
- [ ] Adressen: **ipr.no-skjemaet/pluginen MÅ sende `adresse`, `postnr` og `poststed`** (påkrevd: webhooken avviser påmeldinger uten dem med 400, og fakturaen sendes
      automatisk ved påmelding, tidligst seks måneder før første kursdag). Testet med curl mot testmiljøet, med oppdiktede data (full beskrivelse i `OPERATIONS.md` 2f):
      `curl.exe -i -X POST https://<testmiljø>/api/paamelding -H "Content-Type: application/json" -H "X-IPR-Token: <WEBHOOK_HEMMELIG>" -d "@test.json"` med
      `{"fornavn": "Test", "etternavn": "Person", "epost": "test.person@example.no", "kurs": "KURSKODE", "adresse": "Eksempelveien 1", "postnr": "0150", "poststed": "Oslo"}`
      i `test.json` (forventet `201`; uten adressefeltene `400`). `ADRESSE_KREVES_I_WEBHOOK` og `ADRESSE_KREVES_I_CSV` står på `1` (standard), ikke `0`
- [ ] **Deltakerinngang på terapiakademiet.no** (Camilla legger den inn selv, og først når påmeldingssystemet er helt klart; hun har bedt om å bli minnet på det). Egen adresse `minside.terapiakademiet.no` (hun har sagt ja til navnet 02.10):
      DNS-poster hos den som administrerer domenet (CNAME `minside` til appens azurewebsites-adresse, og TXT `asuid.minside` med bekreftelseskoden fra Azure), egendefinert domene og sertifikat (HTTPS) for adressen i App Service,
      `BASE_URL=https://minside.terapiakademiet.no`, og Entra-redirecten `https://minside.terapiakademiet.no/admin/logg-inn/entra/svar` lagt til i app-registreringen (`AZURE-SETUP.md` pkt. 4). Gjør dette FØR første ekte utsending:
      `BASE_URL` bygges inn i lenkene i e-postene (den gamle azurewebsites-adressen virker videre, så eldre e-poster fungerer). Deretter lenken «Logg inn på Min side» på terapiakademiet.no; ferdig tekst står i
      `dokumentasjon/lenke-til-terapiakademiet.md`. Hvor på nettsiden den skal stå, avgjør hun senere
