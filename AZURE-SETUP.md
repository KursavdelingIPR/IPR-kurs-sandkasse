# Azure- og Microsoft 365-oppsett (for IT)

Trinn for trinn: hva som må opprettes, med **minst mulige tilganger**. Gjør alt i **sandbox** først
(`pameldingssystem-sandbox-rg`), og gjenta for produksjon (`pameldingssystem-prod-rg`) når sandbox fungerer.
Region: **Norway East**. Hvilke innstillinger appen leser: `DEPLOYMENT.md`, avsnitt 2.

> Status 25.09.2026: Resource groups finnes. App Service, PostgreSQL, Key Vault og app-registreringer er **ikke**
> opprettet. Ingen av integrasjonene er testet mot ekte tenant/konto ennå (se avsnitt 9).

## 1. PostgreSQL (Azure Database for PostgreSQL – Flexible Server)

- Versjon **16** (testet lokalt mot 16; 14–15 skal også fungere).
- Sandbox: Burstable B1ms er nok. Produksjon: vurder General Purpose og zone-redundant HA.
- **Backup:** automatisk, minst 7 dager (anbefalt 14–35 i produksjon). Test gjenoppretting én gang.
- **Utvidelsen citext** må tillates: Server parameters → `azure.extensions` → huk av `CITEXT`. Skjemaet kjører selv
  `CREATE EXTENSION IF NOT EXISTS citext` ved første migrering.
- Krev TLS (`require_secure_transport = on`, standard). Tilkoblingsstrengen bruker `sslmode=require`.
- Nettverk: helst privat tilgang (VNet-integrasjon for web-appen + privat endepunkt). Alternativt offentlig tilgang
  med brannmur som bare slipper inn web-appens utgående IP-er (og ev. IT sin adresse for vedlikehold).
- Opprett en egen databasebruker for appen som eier skjemaet (migreringen lager tabeller). Ikke bruk serveradmin i appen.
- Legg tilkoblingsstrengen i Key Vault (`DATABASE_URL`).

## 2. Web-app (App Service, Linux)

- App Service Plan Linux (sandbox B1, produksjon P0v3/P1v3). Web App med runtime **Python 3.12**.
- Startkommando: `gunicorn --bind=0.0.0.0 --timeout 600 startup:app`
- HTTPS only = på, minimum TLS 1.2, FTP(S) = av, «Always on» = på i produksjon.
- **Health check path:** `/helse` (svarer 200 når databasen svarer og har riktig versjon, ellers 503).
- Systemtildelt **managed identity** = på (brukes mot Key Vault).
- App Settings: se `DEPLOYMENT.md` avsnitt 2. Hemmeligheter som Key Vault-referanser.
- Logging: **Application logging** (appens egen logg til stdout/stderr, som bare har rutemønster, status og referanse) →
  Log Analytics. **Web server logging / HTTP-logg (`AppServiceHTTPLogs`) skal stå av**, eller utelates i diagnostikkinnstillingen:
  den lagrer hele adressen med spørrestreng og `Referer`, og deltakersøket (`/admin/sok?q=…`) og deltakerregisteret
  (`?sok=…`) har navn, e-post og telefon der (se GDPR.md, avsnittet om deltakersøket). Den personlige lenken til Min side (`/min/<lenke>`) står også i adressen,
  og er en nøkkel til deltakerens side. Gunicorn startes uten `--access-logfile`.
  Må HTTP-loggen likevel være på: kort ned oppbevaringen og begrens hvem som kan lese loggen.
  Varsling på HTTP 5xx lages fra metrikken `Http5xx`, og på helsesjekken - ikke fra HTTP-loggen.

## 3. Key Vault

- Ett Key Vault per miljø. RBAC-modell.
- Gi web-appens managed identity rollen **Key Vault Secrets User** (bare lese hemmeligheter).
- Hemmeligheter: `DATABASE_URL`, `HEMMELIG_NOKKEL`, `WEBHOOK_HEMMELIG`, `ENTRA-CLIENT-SECRET`, `M365-CLIENT-SECRET`,
  `ZOOM-CLIENT-SECRET`, `VISMA-CLIENT-SECRET`, `VISMA-REFRESH-TOKEN`, ev. `ANTHROPIC-API-KEY` og nødbrukerens
  `ADMIN-PASSORD`. Lag `HEMMELIG_NOKKEL`/`WEBHOOK_HEMMELIG` med f.eks.
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

## 4. App-registrering 1: admin-innlogging (Microsoft Entra ID)

Brukes bare til å logge inn ansatte. Ingen Graph-tillatelser utover innlogging.

1. Entra ID → App registrations → New: «IPR Kurs admin (sandbox)», single tenant.
2. Platform **Web** (ikke «Single-page application»: da avviser Microsoft innloggingen fra serveren, AADSTS9002327),
   redirect URI: `https://<app-adresse>/admin/logg-inn/entra/svar`
3. Certificates & secrets → sertifikat (se 5a; `ENTRA_SERTIFIKAT`, eller samme som e-post når registreringen er felles)
   eller client secret → Key Vault (`ENTRA_CLIENT_SECRET`). Noter tenant-id og client-id.
4. API permissions: bare de delegerte `openid`, `profile`, `email` (standard, ingen admin consent nødvendig).
5. **App roles** (Allowed member types: Users/Groups), med disse verdiene:
   - `ipr.system` – Systemadministrator (brukere, daglig kjøring, sletting etter GDPR)
   - `ipr.kursadmin` – Kursadministrator (alt kursarbeid, eksport)
   - `ipr.lese` – Lesetilgang (ser, kan ikke endre eller eksportere)
6. Enterprise applications → appen → Properties → **Assignment required = Yes**. Users and groups → tildel roller.
   Den som ikke har en rolle, slipper ikke inn.
7. App Settings: `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET`, `ADMIN_LOKAL_INNLOGGING=0`.

Alternativ til app-roller: sikkerhetsgrupper (`ENTRA_GRUPPER=<objekt-id>=system,...`) – krever at gruppe-claims er slått
på i tokenet. App-roller anbefales.

## 5. App-registrering 2: e-post (Microsoft Graph, applikasjonstillatelser)

Brukes av tjenesten selv (ingen innlogget bruker). Tillatelsene begrenses til **kurspostboksene**. Kurssystemet bruker ikke SharePoint (Camilla 06.10.2026: kursholder-lenken og SharePoint er tatt bort; filene til deltakerne ligger i kurssystemet).

1. Ny app-registrering «IPR Kurs tjeneste (sandbox)». **Sertifikat** (anbefalt av Serit og Microsoft, se 5a) eller
   client secret → Key Vault (`M365_SERTIFIKAT` / `M365_CLIENT_SECRET`). (I IPRs tenant heter registreringen
   «IPR Kurssystem», laget av Serit 04.10.2026.)
2. API permissions (Application), **grant admin consent**:
   - `Mail.Send` – sende fra kurs-postboksen
3. **Begrens Mail.Send til kurs-postboksen** i Exchange Online (RBAC for Applications, eller Application Access Policy):
   appen skal bare kunne sende som `AVSENDER_EPOST` (i sandbox: en egen testpostboks).
### 5a. Sertifikat i stedet for client secret

Appen signerer en kortlevd JWT med sertifikatets private nøkkel (`kurs/integrasjoner/sertifikat.py`); den private nøkkelen
ligger bare i Key Vault og i appens minne.

1. Key Vault → **Certificates** → **Generate/Import**: Generate, navn `m365-sertifikat`, **Self-signed certificate**,
   subject `CN=ipr-kurssystem-test` (prod: `-prod`), gyldighet 12–24 måneder, **Content type: PKCS #12**,
   Lifetime action: **e-postvarsel** før utløp (ikke automatisk fornyelse: et nytt sertifikat må lastes opp i
   app-registreringen først).
2. Åpne sertifikatet → gjeldende versjon → **Download in CER format**. Det er bare den offentlige delen og kan sendes
   til IT, som laster den opp i app-registreringen (Certificates & secrets → Certificates → Upload).
3. App Setting `M365_SERTIFIKAT` = `@Microsoft.KeyVault(VaultName=<vault>;SecretName=m365-sertifikat)` (Key Vault lager en
   hemmelighet med samme navn som sertifikatet; web-appens identitet trenger bare **Key Vault Secrets User**).
   `M365_SERTIFIKAT_PASSORD` trengs ikke (Key Vault lager PFX uten passord).
4. Innloggingen bruker samme sertifikat når den bruker samme app-registrering (ingen egen `ENTRA_CLIENT_ID`); har den
   egen registrering, settes `ENTRA_SERTIFIKAT` på samme måte.
5. Kontroll fra App Service-konsollen (SSH): `python -m kurs.m365_sjekk` (sender ingenting, skriver aldri
   ut nøkler: viser om sertifikatet kan leses, om Microsoft godtar innloggingen og hvilke tillatelser appen har).
6. Fornyelse: lag ny versjon i Key Vault, last opp den nye CER-filen hos IT **før** den gamle utløper. Referansen uten
   versjon henter nyeste versjon (innen et døgn, eller straks ved omstart av appen).
   Et sertifikat som ikke kan leses, stopper oppstarten i drift med én linje i loggen (`M365_SERTIFIKAT: …`).

Kall appen gjør (alle dokumentert i Microsoft Graph v1.0):

| Hva | Kall | Tillatelse |
|---|---|---|
| Sende e-post | `POST /users/{AVSENDER_EPOST}/sendMail` | Mail.Send (begrenset) |


## 6. Zoom (Server-to-Server OAuth)

1. Zoom App Marketplace → Develop → Server-to-Server OAuth, på IPRs Zoom-konto (Pro eller høyere, rapporter må være på).
2. Scopes (kontroller navnene mot Zooms gjeldende liste): opprette møter, lese møter og lese deltakerrapporter
   (`meeting:write`, `meeting:read`, `report:read:list_meeting_participants` eller tilsvarende granulære scopes).
3. Aktiver appen. **Etter endring av scopes må appen deaktiveres og aktiveres på nytt.**
4. `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET` (Key Vault).

Møtet opprettes som et gjentakende møte uten fast tid under brukeren appen tilhører (`/users/me`) ~8 dager før kurset.

## 7. Visma

**Produktet er ikke avklart** (eAccounting/eRegnskap, Visma.net ERP eller Business NXT). Koden er skrevet mot
eAccounting v2 som utgangspunkt; resten av systemet kaller bare `visma.fakturer(...)`, så bare den filen byttes.

For eAccounting:

1. Opprett en integrasjon i Visma Developer Portal (klient-id/-hemmelighet, redirect-URI for engangssamtykke).
2. Test først mot Visma sitt **sandbox-selskap**.
3. Hent et første refresh-token med OAuth-samtykke (engangssteg, gjøres av en med tilgang til regnskapet), legg det i
   Key Vault som `VISMA_REFRESH_TOKEN`.
4. Refresh-tokenet **roteres** ved hvert bruk. Appen lagrer det nye straks i databasen (tabellen `integrasjon_token`),
   så miljøvariabelen er bare startverdien. Må et nytt token hentes (f.eks. etter «invalid_grant»): legg det nye i Key
   Vault og slett den lagrede raden (`OPERATIONS.md`, «Visma-tilgang»).

## 8. Utgående nettverk

Appen og morgenjobben må nå: `login.microsoftonline.com`, `graph.microsoft.com`, `zoom.us`, `api.zoom.us`,
`identity.vismaonline.com`, `eaccountingapi.vismaonline.com` (eller API-et for valgt Visma-produkt), `data.brreg.no`
(firmaoppslag i påmeldingsskjemaet), og – bare hvis KI er på – `api.anthropic.com`.

## 9. Hva er testet, og hva er ikke

| Del | Testet | Ikke testet |
|---|---|---|
| PostgreSQL | Hele testsuiten mot ekte PostgreSQL 16 lokalt (samme skjema og migreringer) | Azure Flexible Server (nettverk, TLS, citext-tillatelse) |
| Web-app | Røyktest i Chromium mot demoserver; alle sider og hovedflyter | App Service (gunicorn, ProxyFix bak Azure front end) |
| Brønnøysundregistrene | Driftsgrenen med etterlignet register (oppslag, underenheter, søk, feil og tidsavbrudd). Ekte oppslag kontrollert med `python -m kurs.brreg_sjekk` 28.09.2026 | Fra App Service (utgående nettverk) |
| Entra-innlogging | Hele flyten med etterlignet token-endepunkt (state, nonce, PKCE, claims, roller) | Mot ekte tenant |
| Graph e-post | Driftsgrenen med etterlignet Graph | Mot ekte tenant, postboksbegrensning |
| Zoom | Demo-gren og lagring/idempotens | Mot ekte Zoom-konto |
| Visma | Token-rotasjon og feilklassifisering med etterlignet token-endepunkt | Hele fakturaflyten mot Visma (produkt ikke avklart) |
