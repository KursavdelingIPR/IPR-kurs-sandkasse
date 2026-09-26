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
| E-post / faktura / Zoom / SharePoint | Liksom (utboks, demo-grener) | Ekte, **mot testkonto/testpostboks/Visma-sandbox** | Ekte |
| Admin-innlogging | Brukernavn/passord (`admin`/`demo`) | Microsoft Entra ID (+ ev. nødbruker) | Microsoft Entra ID (+ ev. nødbruker) |
| Data | Oppdiktet (`kurs.seed_demo`) | Oppdiktet / testpersoner | Ekte |

**Sandbox først.** Ingenting settes opp i produksjon før sandbox har kjørt stabilt (se sjekklisten i 5).

## 2. Innstillinger (App Settings / miljøvariabler)

I Azure settes alt som App Settings på web-appen. Hemmelige verdier legges i Key Vault og refereres med
`@Microsoft.KeyVault(SecretUri=...)`. Lokalt kan `.env` brukes (se `.env.example`) – `.env` skal aldri i git.

| Navn | Lokalt | Sandbox/prod | Hemmelig | Forklaring |
|---|---|---|---|---|
| `MODUS` | `demo` | `prod` | | `prod` = ekte integrasjoner og strenge kontroller |
| `BASE_URL` | `http://127.0.0.1:5000` | **påkrevd**, `https://…` | | Brukes i lenker i e-post (Min side, opplasting, Entra-retur). Må være https i drift |
| `HEMMELIG_NOKKEL` | valgfri | **påkrevd**, ≥ 32 tilfeldige tegn | ✔ | Signerer økter og lenker. Bytte = alle logges ut og gamle lenker blir ugyldige |
| `DATABASE_URL` | tom | **påkrevd** | ✔ | `postgresql://…?sslmode=require`. Tom = SQLite |
| `DB_STI` | valgfri | – | | SQLite-fil (bare lokalt) |
| `WEBHOOK_HEMMELIG` | valgfri | **påkrevd**, ≥ 32 tegn | ✔ | Signatur for `/api/paamelding` |
| `ADMIN_BRUKERNAVN`, `ADMIN_PASSORD` | `admin`/`demo` | Første nødbruker, passord ≥ 12 tegn | ✔ (passord) | Brukes **bare** når databasen ikke har noen admin-brukere (første migrering) |
| `ADMIN_LOKAL_INNLOGGING` | `1` | `0` (anbefalt) eller `1` for nødbruker | | `0` = kun Microsoft-innlogging |
| `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID` | – | **påkrevd** hvis Entra | | App-registreringen for admin-innlogging |
| `ENTRA_CLIENT_SECRET` | – | **påkrevd** hvis Entra | ✔ | |
| `ENTRA_ROLLER` | standard | valgfri | | App-rolle → rolle, standard `ipr.system=system,ipr.kursadmin=kursadmin,ipr.lese=lese` |
| `ENTRA_GRUPPER` | – | valgfri | | Alternativ: `<gruppe-objekt-id>=rolle,…` |
| `M365_TENANT_ID`, `M365_CLIENT_ID` | – | **påkrevd** | | App-registreringen for e-post og SharePoint (Graph, applikasjonstillatelser) |
| `M365_CLIENT_SECRET` | – | **påkrevd** | ✔ | |
| `SHAREPOINT_SITE_ID` | – | **påkrevd** | | Kurs-siten (Sites.Selected) |
| `AVSENDER_EPOST`, `AVSENDER_NAVN` | standard | **påkrevd** | | Postboksen e-post sendes fra (kurs@ipr.no i prod, en testpostboks i sandbox) |
| `ADMIN_EPOST` | standard | **påkrevd** | | Mottaker av eskaleringer og «til adm»-henvendelser |
| `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID` | – | påkrevd for digitale kurs | | Zoom Server-to-Server OAuth |
| `ZOOM_CLIENT_SECRET` | – | påkrevd for digitale kurs | ✔ | |
| `VISMA_CLIENT_ID` | – | påkrevd for fakturering | | Visma-produktet er **ikke avklart** (se `kurs/integrasjoner/visma.py`) |
| `VISMA_CLIENT_SECRET`, `VISMA_REFRESH_TOKEN` | – | påkrevd for fakturering | ✔ | Refresh-tokenet er bare **startverdien**; det roterer og lagres deretter i databasen (`integrasjon_token`) |
| `VISMA_API` | standard | valgfri | | Standard: eAccounting v2 |
| `BNXT_CLIENT_ID`, `BNXT_SELSKAP` | – | påkrevd for status fra Business NXT | | Visma Business NXT, fase 1: **bare lesing** av fakturastatus. `BNXT_SELSKAP` er Visma.net-selskaps-ID-en |
| `BNXT_CLIENT_SECRET` | – | påkrevd for status fra Business NXT | ✔ | Klient-legitimasjon; tilgangstokenet holdes bare i minnet (ingenting i databasen) |
| `BNXT_KUNDENR` | – | valgfri | | Visma.net-kundenummer – brukes bare av `python -m kurs.bnxt_sjekk` for å liste selskapene |
| `ANTHROPIC_API_KEY` | – | valgfri | ✔ | Uten nøkkel: ordmatching mot kunnskapsbasen |
| `ASSISTENT_AKTIV` | `1` | `1`/`0` | | `0` = «Spør oss» er av (404) |
| `ASSISTENT_MODELL`, `ASSISTENT_MAKS_PER_DAG`, `ASSISTENT_TIDSAVBRUDD_SEK` | standard | valgfri | | Kostnadsgrense og tidsavbrudd for KI |
| `SLETT_SENSITIVT_ETTER_DAGER` | `14` | `14` | | Allergier/tilrettelegging slettes så mange dager etter siste kursdag |

I drift nekter appen å svare (503 og en linje i loggen) hvis `HEMMELIG_NOKKEL`/`WEBHOOK_HEMMELIG` er svake, `BASE_URL`
ikke er https, det ikke finnes noen innloggingsvei for admin, eller databasen har feil versjon.

## 3. Web-appen

- Kjøretid: Python 3.12 på App Service **Linux**.
- Startkommando (App Service → Configuration → General settings):
  ```text
  gunicorn --bind=0.0.0.0 --timeout 600 startup:app
  ```
  `startup.py` importerer bare appen. **Gunicorn migrerer aldri** – det er et eget steg (4).
- `requirements.txt` installeres av App Service ved utrulling (Oryx). `gunicorn` og `psycopg[binary]` er med.
- HTTPS og TLS-terminering gjøres av App Service. Appen bruker `ProxyFix`, så `BASE_URL` må være den offentlige
  https-adressen.
- Lokal disk brukes ikke til varige data i drift: kursbevis ligger i databasen, kursmateriell i SharePoint.

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
