# Menyer forklart

For deg som administrerer kurs i IPR Påmeldingssystem. Kort, og uten fagord.

Menyen øverst i administrasjonen heter: **Oversikt · Kalender · Rapporter · E-postmaler · Daglig kjøring · Brukere · Logg ut**.
«Daglig kjøring» og «Brukere» ser bare systemadministratorer. «Utboks» finnes bare i demoen. «Kunnskapsbase» vises bare hvis
«Spør oss» er slått på (det er den ikke fra start). Se lenger ned om hva som er tatt bort.

## Startsiden

Startsiden (adressen til systemet, uten noe etter skråstreken) er **innloggingen til Admin og ingenting annet**. Det er den enkle adressen du kan gi til alle som skal bruke systemet.
Er du allerede innlogget, kommer du rett til Oversikten. Kurslisten og «Innsjekk» som sto her før, er tatt bort.
Adressen står ferdig til å kopiere under **Brukere** → «Lenke til innloggingen» (bare systemadministrator ser den siden).
De som skal inn, må ha en bruker der (eller en Microsoft-konto, hvis dere bruker det) og logger inn med sin egen bruker.

## Oversikt (forsiden i Admin)

Aktiviteter er slått sammen med Oversikten, så det finnes ikke lenger to steder å lete etter kurs. Oversikten svarer både på «Hva må jeg gjøre i dag?» og «Hvilke kurs har vi?».
Fra toppen:

1. **To søkefelt side ved side.** *Søk etter deltaker* finner en person på tvers av alle kurs (navn, e-post, telefon, påmeldingsnummer eller kursnummer) og viser påmeldinger, oppmøte og kursbevis;
   mens du skriver, kommer de beste treffene i en liste. *Søk etter kurs* søker på kursnummer eller kursnavn, og treffene vises i kurslisten under. Trykk **/** på en hvilken som helst side i Admin
   for å hoppe til deltakersøket.
2. **Kurs.** Kurslisten med filtre: sted, ansvarlig, status, sortering, antall rader og «Vis bare mine kurs». Klikk på en kolonneoverskrift for å sortere. Per kurs: Rediger, Min side og Dupliser,
   og «Ta opp oppmøte i dag» når kurset har kursdag i dag. Under «Påmeldte / kapasitet» står påmeldte av plasser («12 / 16 + 2 ekstradeltakere»), venteliste, hvor mange som er registrert og hvor mange som er fakturert.
   Uten valg viser listen kurs som kommer eller pågår (utkast, åpne, fulle og aktive). **Avsluttede og avlyste kurs er skjult.** Du ser dem når du søker (kurssøket leter i alle kurs),
   eller velger «Alle kurs» eller en bestemt status.
3. **Status.** Tellerkort: aktive/åpne kurs og påmeldte (**ekstradeltakere** teller ikke som påmeldt; de vises for seg). Under «Trenger oppfølging»: kurs som mangler materiell, påmeldinger der
   firmaopplysninger må kontrolleres, fakturering som holdes tilbake fordi privat adresse mangler, og uavklarte operasjoner (e-post eller faktura som må sjekkes for hånd). Kortene lenker til listene lenger ned.
4. **Listene under kurslisten** for det som trenger deg: firmaopplysninger som må kontrolleres, fakturering holdt tilbake (privat adresse) og materiell som mangler.

«Siste hendelser» (den tekniske loggen nederst) er tatt bort. Historikken for en påmelding finner du i fanen Logger i deltakervinduet.

## Kalender

**Kalender** er en egen fane i menyen. Den har to underfaner: **Kalender** (kursene som kalender: kursoversikt, per måned eller uke) og **Årsplan** (hele året: kurs, planlagte aktiviteter som ennå ikke er kurs,
skoleferier, sperrede perioder og røde dager – og hvor kurs overlapper).

Når du åpner ett kurs (klikk på tittelen i kurslisten), får du fanene **Oppsett**, **Nettside**, **Påmeldingsskjema**, **Min side**, **Deltakere** og **Kommunikasjon**.

- **Nettside** gjelder *påmeldingssiden* som alle kan se (innledningstekst og knappetekst).
- **Min side** er siden *deltakerne ser etter at de har logget inn eller åpnet sin personlige lenke*: program, presentasjoner, grupper, litteratur, praktisk informasjon, innsjekk og deltakerens egne opplysninger fra påmeldingen. Bare de som har bekreftet påmelding på kurset får se den. Siden er åpen i 180 dager etter siste kursdag; det kan endres per kurs (et antall dager, eller «Ingen tidsbegrensning» for utdanninger som trenger det) under Min side → «Innstillinger for siden». Se «Min side – slik gjør du».

## Kunnskapsbase

En «bokhylle» med ferdig godkjente svar på det deltakerne ofte spør om (avmelding, lunsj, parkering, Zoom-trøbbel, faktura). Den gjør ingenting på egen hånd: den brukes bare av
**«Spør oss»**, en side der deltakere kan skrive et spørsmål. «Spør oss» skriver aldri selv: den velger et godkjent svar fra Kunnskapsbasen og viser det ordrett, og alt den er usikker på
går til administrasjonen («Til adm»).

**Både «Spør oss» og Kunnskapsbase er slått av fra start.** Menyvalgene er borte, og adressen `/sporsmal` gir «Siden finnes ikke». Alt er beholdt (koden og de godkjente svarene), så
en systemadministrator kan slå dem på igjen med innstillingen `ASSISTENT_AKTIV=1`. Skriver du inn adressen `/admin/kunnskap` mens de er av, kommer du fortsatt inn, og øverst står en
forklaring om at «Spør oss» er slått av.

## Daglig kjøring

Systemets **morgenjobb**. I drift kjører den av seg selv **hver morgen kl. 07**. Siden «Daglig kjøring» er bare en manuell knapp for den samme jobben. I drift kan du bare **tørrkjøre**
(se hva som ville skjedd; ingenting sendes eller lagres). I demoen kan du kjøre for ekte og «spole» til en annen dato.

Jobben gjør, i denne rekkefølgen:

1. Henter manglende firmaopplysninger fra Enhetsregisteret.
2. Behandler nye påmeldinger: sender bekreftelse og lager faktura.
3. Sender delfakturaer som forfaller, og utsatte samlefakturaer.
4. For hvert kurs som er åpent, fullt eller pågår: oppretter Zoom-møte ca. 8 dager før (digitale og hybride kurs), sender «uka før»-e-post (siste 7 dagene før start) og «dagen før»-e-post for hver kursdag,
   setter kurset til «aktiv» når det starter og «avsluttet» etter siste dag, og henter oppmøte fra Zoom for dagene som er passert.
5. Lager kursbevis til dem som har møtt alle kursdagene.
6. Purrer kursholdere som ikke har levert materiell (7, 2 og 0 dager før fristen; etter fristen går det til administrasjonen).
7. Sletter allergier og tilrettelegging 14 dager etter siste kursdag.
8. Rydder bort utløpte forhåndsvisninger fra filimport.
9. Tømmer gruppelister på Min side 30 dager etter kurset.

Den kan kjøres flere ganger samme dag uten at noe blir sendt to ganger.

## Innsjekk

Innsjekk er måten deltakerne **registrerer at de er på kurs, én gang per kursdag**. Oppmøtet lagres i systemet og bestemmer bl.a. om deltakeren får kursbevis (det krever oppmøte alle dager) og hvor mange timer som teller i spesialistløp.

**Slik gjør deltakeren det**

- **Skanner QR-koden** som vises på skjermen i kurslokalet (eller henger som plakat), og skriver e-postadressen hen meldte seg på med (telefonen fyller den ut). Er hen logget inn (også med den personlige lenken til Min side), er det ett trykk. Skanningen gjør ikke deltakeren innlogget.
- **Får hen ikke skannet?** Hen åpner **Min side** (knappen i e-posten) eller logger inn (adressen står på QR-plakaten) og trykker **«Registrer oppmøte»** i kortet «I dag». På fysiske og hybride kurs skriver hen da inn dagens kode fra skjermen. Den gamle siden `/innsjekk`, der man kunne skrive kode og e-post uten å logge inn, er fjernet.
- Hver kursdag har sin egen QR-kode og kode, og de virker bare på selve dagen. Man kan bare registreres én gang per dag – skanner man igjen, får man beskjed om at man allerede er registrert (innloggede får også klokkeslettet).
- Systemet bruker **e-postadressen** til å finne påmeldingen (den er unik for hver person; navn er det ikke, og en navneliste ville vist alle som er påmeldt til alle som har QR-koden). Svaret til den som ikke er innlogget nevner aldri navn, bare en delvis skjult adresse (`k***@example.no`). Ulempen: e-postadressen er ikke hemmelig, så en kollega med QR-koden kan i prinsippet registrere en fraværende. Vil dere ha det sikrere (men tregere i lokalet), kan innsjekk kreve innlogging først, se `OPERATIONS.md` 2d (`INNSJEKK_KREVER_INNLOGGING`).

**Slik gjør du det som administrator**

- Åpne kurset → **Oppsett** → «Kursdager og innsjekk». Der står hver kursdag med koden og hvor mange som har møtt. **Vis QR på skjerm** åpner en stor side du kan vise i lokalet, eller skrive ut som plakat (knappen **Skriv ut plakat**): kursnavn, dato, «dag 2 av 4», QR-kode, tre korte steg og dagens kode.
- Oppmøte per deltaker og dag ligger under **Deltakere**. Bokstavene viser hvordan det ble registrert: **Q** = QR, **K** = kode (også når deltakeren selv krysset seg inn på nett), **Z** = Zoom, **M** = du gjorde det for hånd. Klikk for å registrere eller fjerne. Har noen glemt å sjekke inn, retter du det her.
- **Får noen ikke skannet QR-koden? Bruk oppmøtelisten.** Åpne den på mobilen og trykk på navnene: deltakeren trenger ikke gjøre noe. Du finner den under Oppsett → «Kursdager og innsjekk» (knappen **Ta opp oppmøte**, tydeligst på dagens dag), på QR-siden, øverst under Deltakere når kurset har kursdag i dag, og på Oversikt («Ta opp oppmøte i dag»).
  Listen viser alle som har plass (påmeldte og ekstradeltakere) i alfabetisk rekkefølge, én stor rad per person. Trykk på raden for å sette **Til stede** (grønn hake), trykk igjen for å ta det bort. Øverst står «12 av 14 til stede» og et søkefelt (navn eller e-post; æ, ø og å spiller ingen rolle).
  **Merk alle til stede** og **Fjern alle merkede** spør først. Oppmøte du trykker inn her er «M» (for hånd) og teller for kursbeviset som annet oppmøte. Oppmøte som er hentet fra Zoom fjernes bare etter en ekstra bekreftelse. Bare dager til og med i dag kan tas opp. Har du bare lesetilgang, ser du listen men kan ikke endre. QR-koden er fortsatt hovedløsningen; «Registrer oppmøte» på Min side og koden virker som før.
- Kursene kan velge at nett-registreringen ikke krever kode (Min side → Innstillinger for siden → «Innsjekk på nett krever dagens kode»).
- Digitale kurs med Zoom: oppmøtet hentes fra Zoom dagen etter (deltakeren må ha vært med i minst 30 minutter), så deltakeren har ingen egen knapp. Digitale kurs uten Zoom-import får én knapp uten kode.
- **Ekstradeltakere** (status «Ekstradeltaker», se OPERATIONS.md) kan være på hele kurset eller bare noen samlinger. Innsjekken virker bare på dagene i samlingene de er satt opp på: skanner en ekstradeltaker QR-koden en dag utenfor utvalget, får hen («Du er ikke satt opp på denne samlingen. Snakk med kursansvarlig.» – bare når hen er innlogget, og da står den med en gang i stedet for registreringsknappen; en uinnlogget får samme nøytrale tekst som ellers, så ingen kan se hvem som er påmeldt). Resultatsiden viser deres egne dagnumre, og QR-plakaten teller bare de som er satt opp på dagen. Du kan fortsatt registrere oppmøte manuelt under **Deltakere** (dagene utenfor utvalget er nedtonet, teller ikke, og knappen sier «utenfor utvalget» også for skjermleser). Kursbeviset krever oppmøte på alle DERES dager og sier hvilke samlinger hen deltok på.

## Min side og Mine kurs (deltakerens sider)

Deltakeren har to sider, uten passord:

- **Min side** er siden til ETT kurs: program, presentasjoner, grupper, litteratur, innsjekk («I dag») og kortet **«Mine opplysninger»** (det deltakeren selv ga ved påmeldingen: navn, e-post, telefon, arbeidssted, hvem som betaler og fakturaadresse). Hver deltaker har en **personlig lenke** dit, som står som knappen «Åpne Min side» i bekreftelsen og påminnelsene. Lenken virker uten innlogging til 30 dager etter siste kursdag.
  Den gir det meste, men **privatadresse, fakturaer og kursbevis** vises først etter at deltakeren har bekreftet e-posten (en knapp sender en engangslenke). Du kopierer, fornyer eller stenger lenken i deltakervinduet.
- **Mine kurs** er oversikten (adressen `/min-side`): ett kort per kurs med «Åpne Min side» og «Registrer oppmøte i dag» på kursdager, og «Min konto» med fakturaer, kursbevis og dokumenter og timer i spesialistløp. Deltakeren kommer hit etter innlogging med e-post.

I menyen står «Logg inn» for den som ikke er innlogget, og «Mine kurs» og «Logg ut» for den som er det, i en egen gruppe til høyre.
Innholdet i kurset (program, presentasjoner, grupper, litteratur) ligger på Min side for hvert kurs, ikke under Mine kurs.

## Det som er tatt bort

- **«Offentlig side»** i admin-menyen er borte, og det finnes ikke lenger en offentlig kursliste: startsiden er innloggingen til Admin. Vil du se påmeldingssiden til ett kurs, bruker du «Forhåndsvis påmeldingsskjema» under fanen Påmeldingsskjema på kurset.
- **«Kurs» og «Innsjekk»** i menyen på deltakersidene er borte. Selve innsjekk-siden der deltakeren tastet dagens kode (`/innsjekk`) er også fjernet: får deltakeren ikke skannet QR-koden, registrerer hen oppmøtet på Min side.
- **«Aktiviteter»** i admin-menyen er borte: søkefeltene, filtrene og kurslisten ligger nå på Oversikten, og kalenderen har fått egen fane. Gamle bokmerker til Aktiviteter sendes til Oversikten.
- **«Siste hendelser»** nederst på Oversikten, og **søkefeltet i toppmenyen** (deltakersøket ligger på Oversikten, så det ikke står to søkefelt i samme bilde).
- **«Spør oss»** i den offentlige menyen er borte (se «Kunnskapsbase» over). Avlysnings-e-posten sier ikke lenger at deltakeren kan bruke «Spør oss».
- **«Kunnskapsbase»** i admin-menyen er borte så lenge «Spør oss» er av.

## De andre menyvalgene

- **Rapporter:** tall på tvers av alle kurs for en periode (fra dato – til dato), med underrapporter for økonomi og fakturaliste, deltakerregister og rapport per kurs. Lister kan lastes ned som CSV (ikke med lesetilgang).
- **E-postmaler:** rediger teksten i de automatiske e-postene og signaturene. Kursdager, klokkeslett, Zoom-info og faktura er alltid styrt av systemet.
- **Brukere:** hvem som kan logge inn i administrasjonen, og rolle: systemadministrator (alt), kursadministrator (daglig kursarbeid) eller lesetilgang (se, men ikke endre). Øverst står **«Lenke til innloggingen»** med «Kopier lenke»: den deler du med kolleger som har fått en bruker.
- **Utboks:** bare i demoen. E-poster som «sendes» legges her i stedet for å bli sendt.

## Slik kan du kontrollere at dette stemmer (kodested per påstand)

| Påstand | Hvor i koden |
|---|---|
| Menyen i admin: Oversikt, Kalender, Rapporter, E-postmaler, [Kunnskapsbase], Daglig kjøring, Brukere, [Utboks], Logg ut | `kurs/web/templates/admin_base.html` |
| «Daglig kjøring» og «Brukere» bare for systemadministrator; «Utboks» bare i demo | `admin_base.html` (`admin_rolle == 'system'`, `demo`) |
| «Kunnskapsbase» og «Spør oss» vises bare når `ASSISTENT_AKTIV=1`; standard er av | `kurs/config.py` (`ASSISTENT_AKTIV`), `admin_base.html`, `base.html`, `_globale` i `kurs/web/app.py` |
| `/sporsmal` gir 404 når av; `/admin/kunnskap` nås fortsatt, med forklaring | `sporsmal_side` og `admin_kunnskap` i `app.py`, `admin_kunnskap.html` |
| Bare «Spør oss» leser Kunnskapsbasen | `kurs/assistent.py` er eneste leser av tabellen `kunnskap` |
| «Offentlig side» finnes ikke i admin-menyen | `admin_base.html` (linjen er fjernet), test `tests/test_kursside_meny.py` |
| Startsiden er innloggingen til Admin (innlogget: videre til Oversikten) | `forside()` og `_admin_innloggingsside()` i `app.py`, `admin_login.html`, test `tests/test_oversikt_omlegging.py` |
| Oversikten: to søkefelt side ved side, tellerkort, kursliste med søk, filter, sortering, 10–100 rader; Rediger, Min side, Dupliser | `admin.html`, `_sokefelt.html`, `admin()` og `_kursliste()` i `app.py`, `planlegging.css` |
| Uten valgt status skjules avsluttede og avlyste kurs; et søk leter i alle kurs | `_kursliste()` i `app.py` (`KURS_ALLE`), test `tests/test_oversikt_omlegging.py` |
| Gamle lenker til Aktiviteter sendes til Oversikten | `admin_aktiviteter()` i `app.py` (omdirigering) |
| Kalender: egen fane, med underfanene Kalender og Årsplan | `admin_base.html`, `admin_aktivitet_faner.html`, `admin_kalender.html`, `admin_aarsplan.html` |
| «/» hopper til deltakersøket (fra andre adminsider til Oversikten) | `data-sok-adresse` i `admin_base.html`, `static/app.js` |
| Rekkefølgen på Oversikten: søk, Kurs, Status (alle boksene), så listene under | `admin.html` |
| «Lenke til innloggingen» med «Kopier lenke» under Brukere | `admin_brukere.html`, `admin_brukere()` i `app.py`, test `tests/test_oversikt_omlegging.py` |
| Litt luft ved høyre og venstre kant på PC og nettbrett (36 px, mobil 20 px) | `base.html` (`header .inner, main`) |
| Fanene på ett kurs | `admin_kurs_faner.html` |
| Morgenjobben kl. 07, tørrkjøring i drift, ni steg, idempotent | `kurs/daglig.py` (docstring og `kjor`), `admin_daglig.html`, `OPERATIONS.md` |
| Zoom-møte ca. 8 dager før; uka før-e-post siste 7 dager; dagen før-e-post; status aktiv/avsluttet | `daglig._trenger_zoom`, `_dagens_innkallinger`, `_status_og_oppmote` |
| Purring 7, 2 og 0 dager før frist, deretter til administrasjonen | `daglig._purring` |
| Sensitive opplysninger slettes 14 dager etter siste kursdag | `daglig._slett_sensitivt`, `config.SLETT_SENSITIVT_ETTER_DAGER` |
| Gruppelister på Min side tømmes 30 dager etter kurset | `daglig._kursside`, `config.KURSSIDE_TOM_TABELLER_ETTER_DAGER` |
| QR og 6-tegns kode per kursdag; bare på selve dagen | `kursdag.innsjekk_token` og `innsjekk_kode` (`schema.sql`), `deltakerside.registrer_qr` |
| Én registrering per deltaker og kursdag | `oppmote` har primærnøkkel (påmelding, kursdag) i `schema.sql`; `db.registrer_oppmote` |
| Identifisering med e-post eller innlogget økt, aldri navn; skanning gir ikke innlogging | `innsjekk_qr` i `app.py`, `deltakerside.registrer_qr`, `tests/test_kursside_innsjekk.py` |
| Nett-registrering på Min side og under Mine kurs, med kode på fysiske og hybride kurs | `deltakerside.registrer_selv`, `deltakerside_oppmote` i `deltakerside_ruter.py` |
| Q, K, Z og M i oppmøtematrisen | `admin_kurs_deltakere.html` |
| Oppmøtelisten for kursleder (trykk på navnene), «Merk alle» og «Fjern alle merkede», søk, Zoom-bekreftelse, bare til og med i dag | `kurs/oppmoteliste.py` (hvem som vises: `deltakere_for_dag`), `kurs/web/oppmoteliste_ruter.py`, `admin_oppmoteliste.html`, `static/oppmoteliste.js` og `.css`, `tests/test_oppmoteliste.py` |
| Knappene «Ta opp oppmøte» (Oppsett, QR-siden, Deltakere, Oversikt) | `admin_kurs_oppsett.html`, `admin_qr.html`, `admin_kurs_deltakere.html`, `admin.html` |
| Kursbevis krever oppmøte alle kursdager; Zoom-import krever 30 minutter | `kurs/kursbevis.py` (`MIN_ANDEL`), `daglig._importer_zoom` |
| Plakat med «Skriv ut plakat» | `admin_qr.html`, `static/innsjekk-plakat.js`, `data-skriv-ut` i `static/app.js` |
| «Logg inn» / «Mine kurs» + «Logg ut» i egen gruppe til høyre | `base.html` (`nav-konto`) |
| Den personlige lenken til Min side: signert, lagres ikke, 30 dager etter siste kursdag, kan stenges og fornyes | `kurs/lenker.py` (`min_side_token`), `kurs/minside.py`, `min_side_lenke` i `app.py`, `tests/test_min_side_kurs.py` |
| Nivåene «lenke» og «epost»: privatadresse, fakturaer og kursbevis krever bekreftet e-post | `kurs/minside.py` (`mine_opplysninger`), `_deltaker_full` og `bekreft_epost` i `app.py` |
| Knappen «Åpne Min side» i bekreftelsen og påminnelsene; lenken skjules i e-posthistorikken | `Kjoring.min_side_lenke`, `minside.skjul_i_kopi`, `kurs/maler/epost/` |
