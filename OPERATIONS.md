# Drift

Hva som skal følges med på, og hva man gjør når noe går galt. Utrulling: `DEPLOYMENT.md`. Migreringer:
`MIGRATIONS.md`. Personvernhenvendelser: `GDPR.md`.

## 1. Hver dag

| Hva | Hvor | Hva du ser etter |
|---|---|---|
| Morgenjobben | Avslutningskoden til `python -m kurs.daglig` (planlagt kjøring) og hendelsesloggen | Kode **0** = alt kjørt. Kode **1** = ett eller flere steg feilet; resten er likevel kjørt. Feilene står som `daglig_feil` (steg, kurs-id, feiltype) |
| Uavklarte operasjoner | Admin → Oversikt → «Uavklarte operasjoner» → «Se og avklar» | Tallet skal være 0. Se avsnitt 3 |
| Materiell som mangler | Admin → Oversikt | Purres automatisk; admin får eskalering etter fristen |
| Henvendelser «til adm» | Admin → Kunnskapsbase | Besvar, og gjør gode svar om til godkjente svar |

## 2. Morgenjobben

- Kjøres kl. 07:00. Idempotent: en ekstra kjøring samme dag gjør ingenting nytt.
- Hvert steg og hvert kurs kjøres for seg. En feil (f.eks. at Microsoft ikke svarer) ruller bare tilbake det steget,
  logges, og stopper aldri resten – **slettingen av sensitive opplysninger kjører alltid**.
- Svarer e-posttjenesten med feil **tre ganger på rad**, utsettes resten av dagens e-poster til neste kjøring (ingenting
  reserveres, ingenting sendes dobbelt). Kursbevis utstedes ikke før e-post virker igjen.
- Se hva jobben ville gjort, uten å endre noe: `python -m kurs.daglig --tor [--dato ÅÅÅÅ-MM-DD]`
  (i drift kan admin også tørrkjøre fra Admin → Daglig kjøring).

## 3. Uavklarte operasjoner (e-post og faktura)

Når en e-post eller faktura feilet på en måte der systemet **ikke vet** om den gikk ut (f.eks. tidsavbrudd hos
Microsoft eller Visma), sendes/faktureres den **aldri automatisk på nytt** – det kunne gitt dobbel e-post eller faktura.

**E-post:** Sjekk «Sendte elementer» i kurs-postboksen.
- Finnes den → «Er sendt».
- Finnes den ikke → «Ble ikke sendt». Systemet prøver igjen neste gang samme utsending kjøres (morgenjobben for
  påminnelser, bekreftelser og purringer; knappen på kurset for avlysninger). Kursbevisvarsler og manuelle e-poster
  prøves ikke automatisk – send da en e-post manuelt.

**Faktura:** Søk opp kunden i Visma.
- Finnes fakturaen → fyll inn fakturanummeret (beløpet er foreslått) → «Finnes i Visma».
- Finnes den ikke → «Finnes ikke». Den lages ved neste daglige kjøring.
- Velg **aldri** «Finnes ikke» uten å ha sjekket.

Visma-tilgang som feiler **før** noe er sendt (utløpt token, feil oppsett), regnes som trygg feil og prøves automatisk
igjen – de dukker ikke opp her. Se avsnitt 6.

## 4. Vanlige hendelser

| Hendelse | Hva du gjør |
|---|---|
| «Kurset er opprettet … SharePoint-mappen kunne ikke lages» | Kurset er lagret. Trykk «Lag SharePoint-mappe» på kursets oppsett-side når SharePoint svarer. Opplasting fra kursholder lager mappen selv ved behov |
| Avlysning: «N deltaker(e) har ikke fått avlysningsvarselet» | Trykk «Send avlysningsvarsel til dem» på kurset når e-post virker. Ingen får det to ganger |
| Manuell e-post: «N mottaker(e) fikk ikke e-posten» | Send samme utsendelse på nytt senere – de som fikk den, får den ikke igjen |
| «Endringene ble ikke lagret: noen andre har endret dette …» | Noen andre (eller morgenjobben) lagret mens du hadde siden åpen. Siden viser nå de nyeste verdiene – gjør endringen på nytt |
| Zoom-møtet «ble ikke brukt» (`zoom_mote_ubrukt` i loggen) | Kurset fikk en lenke i mellomtiden. Det ekstra møtet kan slettes i Zoom |
| En deltaker kan ikke melde seg på («ikke godkjent») | Påmeldingen er avslått. Gjenopprett ved å endre status på deltakersiden hvis det var feil |
| En side gir «Noe gikk galt» med en referanse | Søk etter referansen i App Service-loggen (linjen har rute, status og feiltype – aldri persondata) |

## 5. Brukere og tilgang

- Med Microsoft-innlogging styres tilgang med app-rollene i Entra ID (`AZURE-SETUP.md` avsnitt 4). Rolleendring virker
  ved neste innlogging; deaktivering av brukeren i systemet (Admin → Brukere) virker **straks**.
- Nødbruker (lokal innlogging): bare hvis `ADMIN_LOKAL_INNLOGGING=1`. Passord minst 12 tegn. Oppbevares i Key Vault.
- Det må alltid finnes minst én aktiv systemadministrator – systemet nekter å fjerne den siste.

## 6. Nøkler og tokens

| Hva | Når | Slik |
|---|---|---|
| `HEMMELIG_NOKKEL` | Ved mistanke om lekkasje | Ny verdi i Key Vault → restart. Alle logges ut; gamle opplastings-/innloggingslenker slutter å virke (kursholdere får ny lenke ved neste purring) |
| Client secrets (Entra, Graph, Zoom, Visma) | Før utløp (Entra: maks 24 mnd) | Ny hemmelighet i Key Vault → restart → slett den gamle |
| `BNXT_CLIENT_SECRET` (Business NXT) | Før utløp, eller straks ved mistanke om lekkasje | Lag en ny hemmelighet for `Pameldingssystem-BNXT` i Visma Developer Portal → legg den i Key Vault (lokalt: miljøvariabelen) → restart → slett den gamle i portalen. Kontroller med `python -m kurs.bnxt_sjekk` |
| Visma-tilgang | Ved «invalid_grant» / fakturaer feiler med `IkkeSendt` | Hent nytt refresh-token (`AZURE-SETUP.md` avsnitt 7), legg det i Key Vault som `VISMA_REFRESH_TOKEN`, og slett det lagrede: `DELETE FROM integrasjon_token WHERE navn='visma_refresh_token';` Fakturaene prøves automatisk igjen neste kjøring |

### Visma Business NXT (fase 1: bare lesing)

Deltakervinduet har knappen «Hent status fra Business NXT» under «Status». Den viser fakturadato, forfall, beløp,
utestående, betaling, purringer og eventuell inkasso slik de står i Business NXT, med tidspunktet for hentingen.
Business NXT er fasiten: ingenting endres i påmeldingssystemet eller i Business NXT. «Avvik» betyr at beløpet i
Business NXT ikke er det samme som på påmeldingens faktura – kontroller at fakturanummeret er koblet til riktig
påmelding. I demo vises oppdiktede statuser merket «Demodata».

**Tilkoblingstest** (bare lesing, lagrer ingenting). Sett miljøvariablene `BNXT_CLIENT_ID`, `BNXT_CLIENT_SECRET`,
`BNXT_KUNDENR` og `BNXT_SELSKAP` for Windows-kontoen din (Start → «Rediger miljøvariabler for kontoen din»), åpne en ny
terminal i prosjektmappen og kjør:

```text
python -m kurs.bnxt_sjekk                    # innstillinger, tilgang (bare lesing), selskaper, lesetilgang
python -m kurs.bnxt_sjekk --faktura 12345    # statusfeltene for én faktura - sammenlign med Business NXT
```

Hemmeligheten og tilgangstokenet skrives aldri ut. Tilkoblingstesten er det eneste som leser ekte data fra Business NXT
mens sandkassen står i demo. Mangler `BNXT_SELSKAP`, viser testen selskapene tjenesten har tilgang til.

## 7. Sikkerhetskopi og gjenoppretting

- Azure tar automatisk backup av PostgreSQL (se oppbevaringstid i `AZURE-SETUP.md`). Point-in-time restore gir en
  **ny** server; pek `DATABASE_URL` dit etter kontroll.
- Før hver migrering: manuell `pg_dump` (`MIGRATIONS.md`). Dumpen inneholder personopplysninger – oppbevar den med
  tilgangsstyring og slett den når den ikke trengs.
- Kursbevis ligger i databasen og er med i backupen. Kursmateriell ligger i SharePoint (Microsoft sin oppbevaring).
- Test gjenoppretting minst én gang før produksjon, og etter større endringer.

## 8. Tester

```text
python -m pytest tests -q                                  # SQLite (lokalt)
TEST_DATABASE_URL=postgresql://bruker:passord@vert/testdb python -m pytest tests -q   # mot PostgreSQL
```

Testdatabasen må være tom (testene lager og sletter et eget skjema per test) og ha utvidelsen `citext`. Kjør aldri
testene mot en database med ekte data.

## 9. Ved feil i drift

1. Se helsesjekken (`/helse`) og App Service-loggen (referanse, rute, status).
2. Gir alle sider 503: sjekk App Service-loggen for «Utrygg produksjonskonfigurasjon» (svak nøkkel, `BASE_URL`,
   innloggingsvei) eller «Databasen har feil versjon» (kjør migreringen: `python -m kurs.migrer`).
3. Ved tvil om noe ble sendt/fakturert: se «Uavklarte operasjoner» – aldri kjør ting manuelt på nytt uten å sjekke.
4. Rollback av en utrulling: `MIGRATIONS.md`, prosedyre steg 7.
