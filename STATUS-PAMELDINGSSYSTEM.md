# STATUS – IPR Påmeldingssystem

Sist oppdatert: 25. september 2026

## Kort status

**Klassifisering: Produksjonsklar med eksterne blokkeringer.**

Koden er ferdig for utrulling til Azure **sandbox**: alle funksjoner i kravlisten er bygget, testsuiten er grønn mot
både SQLite og ekte PostgreSQL, sikkerhetsgjennomgang og røyktest i nettleser er gjort. Produksjon venter på ting som
må gjøres utenfor koden (Azure-ressurser, app-registreringer, Visma-produkt, personvernavklaringer) – se avsnitt 5.

| | |
|---|---|
| Teknologi | Python 3.12 / Flask, SQLite lokalt, PostgreSQL 16 i drift (Azure Database for PostgreSQL) |
| Planlagt drift | Azure App Service Linux + gunicorn, Norway East, sandbox først (`pameldingssystem-sandbox-rg`), så produksjon (`pameldingssystem-prod-rg`) |
| Repo / branch | `KursavdelingIPR/IPR-kurs-sandkasse`, arbeidsbranch `claude/ecstatic-cori-vcrl7p` (bygger på `postgresql-fase2-wip`) |
| Tester | SQLite: 2485 bestått, 0 feilet · PostgreSQL 16: 2415 bestått, 0 feilet (25.09.2026) |

## 1. Hva som er gjort

| Område | Resultat | Commit |
|---|---|---|
| PostgreSQL | Adapteren fullført; **hele testsuiten kjører mot ekte PostgreSQL 16** (eget skjema per test). SAVEPOINT-isolering, avbrutte transaksjoner, samtidighetstester | `8e5969d` |
| Migreringer | Versjonert (`schema_versjon`), idempotent, én transaksjon per migrering, `python -m kurs.migrer [--status]`. Appen og morgenjobben nekter å kjøre mot feil versjon i drift; gunicorn migrerer aldri. 6 migreringer | `7f1cbf3` m.fl. |
| Kursnummer | Permanent, numerisk (1001, 1002 …), fra teller, aldri gjenbrukt, nytt ved duplisering, søkbart. Offentlige lenker (`/kurs/<kode>`) uendret | `7f1cbf3` |
| Sikkerhet | CSRF overalt, CSP med nonce og ingen inline-JS, sikkerhetshoder, økter (12 t/90 min, sjekk per forespørsel), takbegrensning, open redirect, signerte opplastingslenker, filopplasting, CSV-injeksjon, feilsider uten traceback, produksjonskontroll | `330c168` |
| Roller (fase 13) | System / kursadmin / lesetilgang, håndhevet på serveren. **Microsoft Entra ID** (OIDC, PKCE, app-roller eller grupper). Lokal innlogging kun demo/nødbruker | `e7c6569` |
| Admin-UI | «Nytt kurs +», ingen duplisert meny, markert aktiv side, hover/fokus, hopp-til-innhold, ledetekster på alle felt, oversikt med kurs øverst | `0e30fe5` |
| Deltakerliste | Kolonnevalg (standard bare nr. og navn), utskrift/PDF via nettleseren, CSV, oppmøte/signatur-kolonner, norsk sortering. Allergier kan aldri velges | `33ffd4c` |
| Årsplan | Hele året, kurs automatisk, planlagte aktiviteter og notater, overlapp, ledige perioder | `5ae8229` |
| Integrasjoner | SharePoint (nivåvis, idempotent, etter lagring, «Lag mappe»-knapp), Zoom (lenke lagres straks), kursbevis i databasen, Visma (roterende token lagres; trygg feilklasse) | `daf064f` |
| Samtidighet | Optimistisk kontroll på redigering, manuelle utsendelser og avlysning via claim-motoren, morgenjobben isolert per steg/kurs med sendestopp, side for **uavklarte operasjoner** | `dbcfea9` |
| Fase 14 | Økonomi: fakturert per måned/kurs, fakturaliste, CSV, fakturaer per person | `9a84edf` |
| Fase 17 | «Avslå påmelding» med arbeidsflyt (plass frigjøres, venteliste rykker opp, avslag på e-post, kan ikke melde seg på igjen selv, gjenopprettes av admin) | `9a84edf` |
| GDPR | «Retten til sletting» (anonymisering, kun systemadministrator), GDPR.md | (denne runden) |
| Drift | `/helse` for App Service, dokumentasjon: DEPLOYMENT, AZURE-SETUP, OPERATIONS, GDPR, SECURITY, MIGRATIONS | (denne runden) |
| start.bat | Sjekker port 5000 først; starter aldri en server nummer to og dreper aldri prosesser | tidligere runde |

Rettet underveis (utvalg): SharePoint-mappe ville feilet i drift (Graph lager ikke mellomnivåer); linjeskift i manuelle
e-poster ble vist som «&lt;br&gt;» (kjent feil siden 12A); delspørring uten alias feilet på PostgreSQL < 16; en Graph-feil
stoppet hele morgenjobben (inkludert slettingen av sensitive data); feil midt i avlysning ga 500 og uvarslede deltakere;
kurskode med linjeskift slapp gjennom valideringen.

## 2. Testresultater

| Kontroll | Resultat |
|---|---|
| Hele testsuiten, SQLite | 2485 bestått, 17 hoppet over (tester som bare gjelder PostgreSQL), 0 feilet |
| Hele testsuiten, PostgreSQL 16 | 2415 bestått, 87 hoppet over (tester som bare gjelder SQLite-filer/Windows), 0 feilet |
| Røyktest i Chromium mot demoserver | 36 av 36 sider og flyter OK, ingen CSP-brudd, ingen 5xx |
| Mutasjonstesting (små feil plantet i 16 kritiske funksjoner: faktura, sikkerhet, roller, Entra, årsplan, økonomi, Visma) | Første runde: 179 av 199 oppdaget (90 %). 15 hull ble tettet med nye grensetester, og en ny kjøring bekreftet at de nå oppdages. De 5 som fortsatt overlever, endrer ikke oppførselen: 4 er likeverdige med originalen, og 1 endrer bare et tidsavbrudd fra 30 til 31 sekunder |
| Idempotens | Morgenjobben kjørt to ganger samme dag i tester: ingenting skjer andre gang |

Kjøre selv: `python -m pytest tests` (SQLite) og med `TEST_DATABASE_URL` mot PostgreSQL (`OPERATIONS.md` avsnitt 8).

## 3. Beslutninger (tatt i denne runden)

| Tema | Beslutning | Begrunnelse |
|---|---|---|
| Fase 15 – produkt/rabatt | **Ikke bygget** | Ingen dokumentert behov (én pris per kurs dekker dagens kurs). Kolonnen `paamelding.rabattkode` er ubrukt og **beholdes urørt** – å fjerne den er en endring av en eksisterende kolonne (krever Jan, CLAUDE.md regel 7). Bygg først når IPR har et konkret rabattopplegg |
| Fase 16 – spørreundersøkelser | **Ikke bygget** | Står som «vurder hvis behov bekreftes» i gap-analysen og som åpent spørsmål i kartleggingen. Anbefaling ved behov: lenke til Microsoft Forms i kursbevis-e-posten (malteksten kan redigeres) i stedet for et eget delsystem |
| Fase 17 – avslått | Ny kolonne `avslatt_ts`, status forblir `avmeldt` | Endrer ikke eksisterende kolonne/CHECK (regel 7) og gjenbruker all avmeldingslogikk (plass, faktura, e-post) |
| Venstremeny | **Ikke bygget** | Toppmenyen har åtte punkter og markerer aktiv side; kurssidene har faner. En venstremeny ville duplisert navigasjonen uten å forbedre den |
| PDF | Via nettleserens «Lagre som PDF» | Ingen tung PDF-avhengighet (weasyprint krever systembiblioteker på App Service). Samme løsning som kursbevis |
| NIEFT-logo | Ingen logo lagt inn | Ingen godkjent logofil i prosjektet. Legg en godkjent fil som `kurs/web/static/logo/deltakerliste.png` (eller .svg/.jpg), så vises den automatisk |
| Økonomi | Viser **fakturert**, ikke innbetalt | Innbetalinger hentes ikke fra Visma (produkt ikke avklart). Står tydelig i rapporten |
| Visma-token | Lagres i databasen | Må overleve rotasjon og omstart. Risiko og tiltak i SECURITY.md |
| Avslagsmalen | Låst (ikke redigerbar) i første versjon | Innholdet som varierer, er begrunnelsen admin skriver. Kan kobles til maltekst-systemet senere |

## 4. Kjente begrensninger

- Ingen automatisk sletting av påmeldinger/oppmøte/kursbevis ennå – lagringstid må besluttes (GDPR.md).
- Takbegrensningen teller per prosess (SECURITY.md).
- Innbetalingsstatus fra Visma synkroniseres ikke.
- Integrasjonene er testet med etterligning, ikke mot ekte tenant/konto (AZURE-SETUP.md avsnitt 9).

## 5. Eksterne blokkeringer (må gjøres utenfor koden)

| # | Hva | Hvem | Beskrivelse |
|---|---|---|---|
| 1 | Azure-ressurser i sandbox | IT | PostgreSQL Flexible Server (citext tillatt), App Service Linux, Key Vault, managed identity – `AZURE-SETUP.md` 1–3 |
| 2 | App-registrering for admin-innlogging | IT | Entra ID med app-rollene `ipr.system`, `ipr.kursadmin`, `ipr.lese` – `AZURE-SETUP.md` 4 |
| 3 | App-registrering for e-post/SharePoint | IT + SharePoint-admin | Mail.Send begrenset til kurs-postboksen, Sites.Selected med write på kurs-siten – `AZURE-SETUP.md` 5 |
| 4 | Visma-produkt og tilgang | IPR/regnskap | Hvilket produkt? Klient + første refresh-token, test mot Visma-sandbox – `AZURE-SETUP.md` 7 |
| 5 | Zoom S2S-app | IPR/IT | `AZURE-SETUP.md` 6 |
| 6 | Planlagt morgenjobb | IT | Container Apps Job eller Functions timer, varsling ved avslutningskode ≠ 0 – `DEPLOYMENT.md` 5 |
| 7 | Personvern | IPR | DPIA, databehandleravtaler, personvernerklæring, lagringstid – `GDPR.md` 6 |
| 8 | ipr.no | IPR/nettside | Kurssidene (Craft CMS) har «Booking»-lenker til Pindena; bytt ~45 lenker til `/kurs/<kode>` når systemet er i drift |
| 9 | Beslutninger hos Jan | Jan | Rabattkode-kolonnen (behold/fjern), ev. endringer av eksisterende kolonner |

## 6. Neste steg

1. IT oppretter sandbox (blokkering 1–3, 5–6). Sett App Settings etter `DEPLOYMENT.md` avsnitt 2.
2. Rull ut og kjør `python -m kurs.migrer` mot sandbox-databasen; kjør testsuiten mot den (`OPERATIONS.md` avsnitt 8)
   med en **tom** testdatabase.
3. Test hele flyten i sandbox med testpersoner og testpostboks (sjekkliste i `DEPLOYMENT.md` avsnitt 6).
4. Visma: avklar produkt, bytt ut/juster `kurs/integrasjoner/visma.py`, test mot Visma-sandbox.
5. Når sandbox er stabil: gjenta i `pameldingssystem-prod-rg`, bytt lenkene på ipr.no.

## 7. Git

Alt er committet og pushet til `claude/ecstatic-cori-vcrl7p` i små, grønne sjekkpunkter (ingen force-push, ingen
omskriving av historikk). Branchen bygger videre på `postgresql-fase2-wip` → `videreutvikling-ipr`.
Ny PC: `git clone …`, `git switch claude/ecstatic-cori-vcrl7p`, `oppsett.bat`, `nullstill-demo.bat`.
