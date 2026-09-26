# Sikkerhet

Hva systemet gjør for å beskytte personopplysninger og admin-funksjoner, hva som forventes av driftsmiljøet, og hva som
er kjente begrensninger. Teknisk detaljnivå; personvern/GDPR-sjekklisten står i `GDPR.md`.

## Sammendrag av vernet

| Område | Løsning | Hvor |
|---|---|---|
| Innlogging admin | Egne brukere med hashet passord (Werkzeug/scrypt). Takbegrensning 10 forsøk/15 min per IP og 20 per brukernavn. Innlogging og avviste forsøk logges (brukernavn kun som hash). | `kurs/web/app.py` (`admin_login`), `kurs/web/sikkerhet.py` |
| Admin-økt | Ny sesjon ved innlogging (mot session fixation). Absolutt levetid 12 timer, utlogging etter 90 min inaktivitet, og brukeren kontrolleres mot databasen ved **hver** request – deaktivering virker straks. | `krever_admin`, `_admin_okt_gyldig` |
| Cookies | `HttpOnly`, `SameSite=Lax`, `Secure` i drift. | `app.config` |
| CSRF | Synchronizer token per sesjon i alle POST-skjema (`csrf_token`), kontrollert på alle state-endrende requests. Webhooken er unntatt (HMAC-signert, ingen cookie). | `sikkerhet.py`, alle maler |
| XSS | Jinja autoescape overalt. Ingen inline-JavaScript og ingen maldata i JS-kontekst: all klientlogikk er data-attributter + `static/app.js`. Content-Security-Policy med nonce blokkerer alt annet script. Lenker admin lagrer (Zoom, dokumenter) må være `http(s)://`. | `sikkerhet._csp`, `static/app.js`, `trygt_url_felt` |
| Sikkerhetshoder | `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`, CSP med `frame-ancestors 'none'`, `form-action 'self'`, `base-uri 'self'`. HSTS i drift over HTTPS. `Cache-Control: no-store` på innloggede sider. | `sikkerhet._sett_hoder` |
| SQL-injeksjon | Kun parametriserte spørringer. Dynamiske kolonnenavn kommer alltid fra faste lister i koden (whitelist), aldri fra input. Visma OData-filter escaper apostrof. | `db.py`, `visma.py`, `tests/test_db_portabilitet.py` |
| IDOR / eierskap | Alle kurs-/deltaker-/oppmøte-/e-post-ruter kontrollerer at raden hører til kurset i URL-en (404 ellers). Import-forhåndsvisninger er bundet til kurs **og** admin-bruker. | `app.py` |
| Takbegrensning | Glidende vindu per IP (og per e-post/brukernavn der det gir mening): admin-innlogging, innloggingslenker (Min side), «Spør oss», offentlig påmelding, innsjekk-kode, webhook, opplasting. 429 med rolig side. | `sikkerhet.GRENSER` |
| Tokens | `secrets.token_urlsafe(32)` for innloggingslenker (engangs, 30 min), kvitteringslenker og import-forhåndsvisning. Signerte opplastingslenker for kursholdere (HMAC av `HEMMELIG_NOKKEL`). Rå tokens logges aldri. | `app.py`, `kurs/lenker.py` |
| Filer | Opplasting: filnavn renses (`secure_filename`), filtype-allowlist, 40 MB tak. CSV-import: 2 MB, 300 rader, robust parsing. Nedlasting: filnavn uten sti-deler, `nosniff`. Lokale dokumenter kun under `data/`. | `lever`, `materiell`, `dokument` |
| CSV-eksport | Celler som begynner med `= + - @` får ledende apostrof (formel-injeksjon i Excel). | `sikkerhet.csv_trygg` |
| Feilhåndtering | Kontrollerte 404/403/405/413/429/503-sider. 500 gir bare en referanse (request-id) – aldri traceback. Loggen får referanse, rutemønster og feiltype – aldri skjemadata eller feilmeldingstekst (kan inneholde e-post/URL). | `_http_feil`, `_uventet_feil`, `kurs/feil.py` |
| Logging | Én linje per request i drift: metode, **rutemønster** (ikke konkret sti – den kan inneholde tokens), status, referanse. Ingen spørrestreng, ingen body. Hendelsesloggen i databasen inneholder aldri navn/e-post/sensitive verdier. | `_tilgangslogg`, `db.logg`-kall |
| Sensitive data | Allergi/tilrettelegging i egen tabell, vises kun til admin (visning logges), aldri i CSV/e-post, slettes 14 dager etter kurs. | `schema.sql`, `daglig._slett_sensitivt` |
| Produksjonskontroll | I drift nekter appen å svare (503 + logg) hvis `HEMMELIG_NOKKEL`/`WEBHOOK_HEMMELIG` er standard/kort, `BASE_URL` ikke er https, eller databasen har feil versjon. Werkzeug-debugger er kun mulig i demo og bare på 127.0.0.1. | `sikkerhet.produksjonsfeil`, `_krev_migrert_database` |
| Daglig jobb fra admin | I drift kan admin bare **tørrkjøre** den daglige jobben fra nettleseren. Ekte kjøring skjer kun via planlagt oppgave. | `admin_daglig` |
| Roller | Systemadministrator / kursadministrator / lesetilgang, håndhevet på serveren for hver forespørsel. Lesetilgang kan ikke endre eller eksportere. Sletting etter GDPR og brukeradministrasjon: kun systemadministrator. | `krever_admin`, `_har_rolle_for` |
| Samtidig redigering | Optimistisk kontroll: kursoppsett, nettside, personopplysninger og påmelding lagres ikke over en endring noen andre (eller morgenjobben) gjorde mens skjemaet var åpent. | `_versjon`, `_endret_av_andre` |
| Dobbel e-post/faktura | Claim/status-modell: reservasjon committes før det eksterne kallet; ukjent utfall prøves aldri automatisk på nytt, men avklares av admin («Uavklarte operasjoner»). Unik indeks hindrer to fakturaer per påmelding/samling. | `kjoring.py`, `sveiper.py`, `db.avklar_*` |
| Eksport | Deltakerlisten har bare nr. og navn som standard; allergier/tilrettelegging kan aldri velges. CSV-eksporter logges med kolonnenavn og antall – aldri innhold. | `kurs/deltakerliste.py`, `admin_*_csv` |
| Helsesjekk | `/helse` svarer bare «ok»/«ikke klar» – ingen versjoner, feilmeldinger eller konfigurasjon. | `helse` |
| KI-assistent | Skriver aldri svar selv (velger blant godkjente tekster). Kan slås av (`ASSISTENT_AKTIV=0`), har tidsavbrudd, maks ett nytt forsøk og dagsgrense (`ASSISTENT_MAKS_PER_DAG`). Nøkkel finnes kun på serveren. | `kurs/assistent.py` |

## Hva driftsmiljøet må sørge for

- HTTPS foran appen (Azure App Service gjør TLS-terminering; appen bruker `ProxyFix` for `X-Forwarded-Proto/For`).
- Hemmeligheter i App Settings / Key Vault – aldri i repo, aldri i loggen. Se `DEPLOYMENT.md` for listen.
- `HEMMELIG_NOKKEL`: minst 32 tilfeldige tegn. Bytt den hvis den kan ha lekket – alle økter og signerte lenker
  blir da ugyldige (kursholdere får ny lenke ved neste purring).
- Kun én prosess kjører migrering (`python -m kurs.migrer`), som eget steg.
- Backups av databasen med tilgangsstyring (inneholder personopplysninger).

## Kjente begrensninger (bevisst)

- **Takbegrensningen teller per prosess.** Med flere gunicorn-workere er den effektive grensen antall workere × grensen.
  Godt nok mot passordgjetting og spam for en liten tjeneste; trenger IPR strengere kontroll, legg en WAF/App Service
  Access Restriction foran, eller sett grensene lavere i `sikkerhet.GRENSER`.
- **Sesjoner ligger i signerte cookies** (ingen server-side sesjonslager). Utlogging «alle steder» skjer ved å
  deaktivere brukeren (virker straks) eller bytte `HEMMELIG_NOKKEL`.
- **Lokal brukernavn/passord er utviklingsmekanismen.** Produksjonsstrategien er Microsoft Entra ID – se `DEPLOYMENT.md`
  og `kurs/web/entra.py`. Lokale passord kan beholdes som nødbrukere.
- **CSP tillater `'unsafe-inline'` for stil** (base.html har inline `<style>` og maler bruker `style="..."`). Script er
  låst med nonce. Å flytte all CSS til en fil er en mulig senere opprydding.
- **E-post går som HTML** via Microsoft Graph. Mottakerens klient bestemmer rendering; innholdet er escaped.
- **Visma sitt refresh-token lagres i databasen** (tabellen `integrasjon_token`, i klartekst) fordi det roterer ved
  hvert bruk og må overleve omstart. Databasen er kryptert på disk i Azure og har tilgangsstyring, men en databasedump
  inneholder tokenet. Lekker en dump: trekk tilbake tilgangen i Visma og hent nytt token (`OPERATIONS.md`, avsnitt 6).
  Alternativet (Key Vault med skrivetilgang for appen) krever mer oppsett og er vurdert som en senere forbedring.
- **Visma Business NXT (fase 1) leser bare.** Tilgangstokenet bes om med scopet
  `business-graphql-service-api:access-group-based-readonly` (fast i koden), alle GraphQL-dokumentene er faste
  spørringer (en sperre avviser alt som ikke begynner med «query» eller inneholder «mutation»), og fakturanumre sendes
  bare som variabler. `BNXT_CLIENT_SECRET` ligger bare i miljøvariabel/Key Vault og sendes i Basic Auth-hodet;
  tilgangstokenet holdes bare i minnet (klient-legitimasjon har ikke refresh-token) og skrives aldri til databasen,
  loggen eller feilmeldinger. Tjenesten bør i tillegg ha en tilgangsgruppe i Business NXT som bare kan lese.
- **Samtidighetskontrollen** dekker vinduet mens et skjema står åpent (minutter), ikke de få millisekundene mellom
  kontroll og lagring i samme forespørsel.

## Rapportere sårbarheter

Send til kursadministrasjonen (kurs@ipr.no) merket «sikkerhet». Ikke legg ved personopplysninger.

## Testdekning

`tests/test_sikkerhet.py`, `tests/test_roller.py`, `tests/test_entra.py`, `tests/test_samtidighet.py`,
`tests/test_integrasjoner.py`, `tests/test_deltakerliste.py` og `tests/test_anonymisering.py` dekker punktene over (CSRF med/uten token, alle POST-skjema har token, ingen inline-JS,
hoder og nonce, HSTS kun i drift, deaktivering, utløp, session fixation, open redirect, takbegrensning, signerte
lenker, filnavn/sti, lenkevalidering, XSS via deltakernavn/kursnavn, CSV-injeksjon, feilsider uten traceback,
webhook-herding, tørrkjøring i drift, Visma-escaping og produksjonskontroll). Kjøres mot både SQLite og PostgreSQL.
