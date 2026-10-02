# Drift

Hva som skal følges med på, og hva man gjør når noe går galt. Utrulling: `DEPLOYMENT.md`. Migreringer:
`MIGRATIONS.md`. Personvernhenvendelser: `GDPR.md`.

## 1. Hver dag

| Hva | Hvor | Hva du ser etter |
|---|---|---|
| Morgenjobben | Avslutningskoden til `python -m kurs.daglig` (planlagt kjøring) og hendelsesloggen | Kode **0** = alt kjørt. Kode **1** = ett eller flere steg feilet; resten er likevel kjørt. Feilene står som `daglig_feil` (steg, kurs-id, feiltype) |
| Uavklarte operasjoner | Admin → Oversikt → «Uavklarte operasjoner» → «Se og avklar» | Tallet skal være 0. Se avsnitt 3 |
| Materiell som mangler | Admin → Oversikt | Purres automatisk; admin får eskalering etter fristen |
| Henvendelser «til adm» | Admin → Kunnskapsbase (bare når «Spør oss» er slått på, se 2e) | Besvar, og gjør gode svar om til godkjente svar. Med «Spør oss» av kommer det ingen nye henvendelser |

## 2. Morgenjobben

- Kjøres kl. 07:00. Idempotent: en ekstra kjøring samme dag gjør ingenting nytt.
- Hvert steg og hvert kurs kjøres for seg. En feil (f.eks. at Microsoft ikke svarer) ruller bare tilbake det steget,
  logges, og stopper aldri resten – **slettingen av sensitive opplysninger kjører alltid**.
- Svarer e-posttjenesten med feil **tre ganger på rad**, utsettes resten av dagens e-poster til neste kjøring (ingenting
  reserveres, ingenting sendes dobbelt). Kursbevis utstedes ikke før e-post virker igjen.
- Se hva jobben ville gjort, uten å endre noe: `python -m kurs.daglig --tor [--dato ÅÅÅÅ-MM-DD]`
  (i drift kan admin også tørrkjøre fra Admin → Daglig kjøring).
- Steg 9 («Min side») tømmer gruppelistene (tabeller med navn) på Min side `KURSSIDE_TOM_TABELLER_ETTER_DAGER` (30) dager etter siste kursdag, i utkast,
  publisert side og alle lagrede versjoner. Idempotent: en tabell uten rader røres ikke.

## 2b. Min side – slik lager du den

Hvert kurs kan ha en **Min side**: siden deltakerne ser etter innlogging eller via sin personlige lenke (program, presentasjoner, gruppeinndeling, litteratur, praktisk
informasjon, innsjekk og deltakerens egne opplysninger fra påmeldingen). Den redigeres i admin, og innholdet ligger inne i kurset (tabellen `kursside`).
(Oversikten over alle kursene til en deltaker heter **Mine kurs**, adressen `/min-side`.)

1. Oversikt → kurset → fanen **Min side** (eller knappen «Min side» i kurslisten). En ny side starter tom: velg «Start fra mal» for en ferdig standardside.
2. Bygg siden av blokker (tekst, viktig, program, filer, lenker, gruppetabell, kontakt, bilde). Utkastet lagres av seg selv. Deltakerne ser ingenting før du trykker **Publiser …**.
3. Publiseringen viser hva som endres og kjører en kontroll (bilder uten alternativ tekst, en deltakers e-postadresse på siden). Røde feil må rettes.
4. **Ta siden ned** midlertidig: Endre ved «Åpen til» → fjern haken ved «Siden er åpen for deltakerne». **Når siden er tatt ned eller stengt, er også kursholders
   SharePoint-filer stengt** for deltakerne (både lenkene under Mine kurs og adressen `/materiell/...`), ikke bare det som ligger på selve siden. Kurs uten publisert
   Min side er som før.
   **Hvor lenge siden er åpen etter kurset:** standard er **180 dager** etter siste kursdag (`KURSSIDE_ETTERTILGANG_DAGER`), men hvert kurs kan ha sin egen tid.
   Endre ved «Åpen til» → «Hvor lenge er siden åpen etter siste kursdag?» og velg *Standard*, *Antall dager* (helt tall fra 1 til 3650), *Ingen tidsbegrensning*
   (for utdanninger og kurs som trenger tilgang lenger; siden er da åpen så lenge kurset finnes) eller *Til en bestemt dato*. Dialogen viser datoen som følger
   av valget («Åpen til 12.03.2027»), og samme tekst står i lenkeboksen. Valget gjelder med en gang, er ikke en del av utkastet (versjonene og «Publiser» rører
   det ikke) og påvirker bare dette kurset. **Det kopieres ikke** når du dupliserer et kurs eller bruker «Hent fra annet kurs» (et nytt kurs starter alltid med
   standarden, og siden må lagres første gang før innstillingen kan endres). Deltakeren ser «Siden er åpen til 12. mars 2027» på et avsluttet kurs, og «Min side
   er stengt (den var åpen til …)» når tiden er ute. Gruppelister med navn tømmes uansett 30 dager etter siste kursdag (`KURSSIDE_TOM_TABELLER_ETTER_DAGER`; teksten i dialogen følger samme tall). Valget logges (`kursside_innstillinger`:
   hvem, kurs og antall dager, aldri persondata).
5. Deltakerne varsles ikke automatisk. Gi beskjed via fanen Kommunikasjon.

Hvem ser siden: bare deltakere med **bekreftet** påmelding på akkurat det kurset. Ventelisten, avmeldte, avslåtte og andre kurs får en nøytral «finner ikke siden»-beskjed.
Kursholders filer i SharePoint (kursmappen, «Presentasjoner») kan vises på siden under «Fra kursholder».
En kort brukerveiledning: `dokumentasjon/Min side - slik gjør du.md`. Innstillinger og grenser: `DEPLOYMENT.md` (`KURSSIDE_*`).

**Personvern:** alle på kurset ser siden. Skriv aldri e-postadresser, private telefonnumre eller helseopplysninger om deltakere. Gruppelister har bare fornavn og forbokstav,
og tømmes automatisk 30 dager etter kurset. Skal en person fjernes fra en gruppeliste før det, redigerer og publiserer du siden på nytt.

### Den personlige lenken til Min side

Hver deltaker med plass har en **personlig lenke** til Min side for kurset (`<BASE_URL>/min/<lenke>`). Den står som knappen **«Åpne Min side»** i bekreftelsen og i påminnelsene
(uka før og dagen før), men bare når kurset har en publisert og åpen Min side og lenken ikke er stengt. Uten publisert side står bare den vanlige lenken til «Mine kurs» nederst.

- **Hva lenken er:** en signert adresse (HMAC av `HEMMELIG_NOKKEL`, 128 bit) bygget av påmeldingsnummer og versjon. Den **regnes ut hver gang og lagres ikke**. Tabellen `min_side_lenke`
  (migrering 19) har bare en rad når en administrator har stengt eller fornyet lenken: påmelding, versjon, stengt/åpen, når og av hvem. Ingen persondata.
- **Hvor lenge:** til **30 dager etter siste kursdag** (kurs uten kursdager: 90 dager etter påmeldingen). Deretter logger deltakeren inn med e-post (`/logg-inn`). Siden kan være åpen lenger enn lenken
  (standard 180 dager, se over). En lenke som ikke virker (feil kopiert, stengt, byttet ut, utløpt, ikke bekreftet eller anonymisert) gir **samme** «Lenken virker ikke» uten å si hvorfor.
  **Også en økt som allerede er åpnet med lenken avsluttes ved neste klikk** når lenken stenges, fornyes eller utløper (økten husker hvilken lenke den kom inn med og sjekkes ved hver forespørsel,
  `_avslutt_ugyldig_lenkeokt` i `app.py`): har noen fått e-posten videresendt, hjelper «Steng lenken» eller «Lag ny lenke» straks. En deltaker som har bekreftet e-posten (nivået «epost») berøres ikke.
  Utløpte lenker settes ikke inn i e-poster (`{min_side}` gir «Mine kurs»).
- **Hva den gir (nivået «lenke»):** det felles innholdet, innsjekk («Registrer oppmøte») og deltakerens egne opplysninger (navn, e-post, telefon, arbeidssted, fakturaopplysninger for firma). **Ikke** privatadresse,
  fakturaer, kursbevis og dokumenter: dem ser deltakeren først etter å ha trykket «Send meg en lenke på e-post» og klikket på den (innlogging med e-post, nivået «epost»). Den som allerede er innlogget med
  e-post som samme person, beholder det høyere nivået når hen åpner lenken.
- **Styring i deltakervinduet** (boksen **Min side**, ikke for lesetilgang): se og kopier lenken, **Lag ny lenke** (den gamle, også i sendte e-poster, slutter å virke) og **Steng lenken** (ingen lenke virker før en ny
  lages). Begge handlingene logges (`min_side_lenke_fornyet`, `min_side_lenke_stengt`, bare påmeldings-id) og vises i klartekst under Logger. **Rettes e-postadressen** til deltakeren (Personopplysninger),
  lages det automatisk en ny lenke til alle påmeldingene til deltakeren: lenken i en bekreftelse som gikk til en feilskrevet adresse slutter da å virke (en lenke du har stengt, forblir stengt).
  Er lenken utløpt, viser boksen det i stedet for lenken.
- **E-posthistorikken** lagrer aldri den personlige lenken: i kopien står `/min/skjult`. Det som ble sendt, har den virkelige lenken. I egenskrevne e-poster (fanen Kommunikasjon) setter `{min_side}` inn mottakerens egen lenke
  (eller «Mine kurs» når mottakeren ikke har en).
- **Bytte av `HEMMELIG_NOKKEL`** gjør alle personlige lenker i sendte e-poster ugyldige. Deltakerne kan logge inn med e-post, og nye e-poster får nye lenker.
- Lenken er takbegrenset (200 forsøk per 10 minutter per IP, `min_side_lenke`: kurslokaler og store arbeidsplasser deler IP), svaret har `Referrer-Policy: no-referrer` og `Cache-Control: no-store`, og adressen står ikke igjen på siden etter åpning.

### Utseendet på deltakersidene (Terapiakademiet-drakten)

Min side, Mine kurs, innlogging, «Lenken virker ikke», «Ikke tilgang», innsjekk og den offentlige påmeldingen (skjemaet `/kurs/<kode>`, bedriftspåmeldingen og kvitteringene) har utseendet til terapiakademiet.no (kremfarget side `#f7e5d0`, bånd og kort `#f1dac1` (bare tekst-, fil- og lenkeblokkene er hvite, `--ta-hvit`), plommefarget tekst `#562a3e`, mauve `#754e5a`,
bærfarget aksent `#8a334e`, firkantede knapper og kort, mørk plommestripe for praktisk informasjon, mørk bunn). Slik er det bygget:

- Sidene setter `{% set tema_ta = true %}` øverst i malen. `base.html` gir da `<body data-tema="ta">`, laster `static/tema-terapiakademiet.css` ETTER sidens egen CSS og viser bunnen (`.ta-bunn`) og logoen.
  Adminsidene setter den ikke, og er uberørt (skjemabyggeren i admin bruker samme `paamelding.css`, men uten drakten). Skal en ny deltakerside ha drakten, er det den ene linjen.
  **Merkevare per kurs kommer etter hvert:** vi er egentlig IPR, og Terapiakademiet er et eget merke som starter med de to testkursene. Nå har ALLE deltaker- og påmeldingssider Terapiakademiet-drakten. Kurs for IPR.no skal få en annen
  stil og en annen Min side (Camilla 01.10.2026: «kan ta det etter hvert»). Da må drakten velges per kurs (for eksempel et felt på kurset, som krever en databasemigrering) i stedet for å være slått på i malene.
- Hele CSS-filen står under `body[data-tema="ta"]` og i `@media screen` (utskrift er uendret), uten eksterne ressurser. Fargene står som `--ta-*` først i filen og overstyrer variablene fra `base.html`, så knapper, lenker og
  merker følger med. Skal fargene endres, er det der. Kontrasten (tekst minst 4,5:1) kontrolleres av `tests/test_tema_terapiakademiet.py`.
- **Logoen** er Terapiakademiets (som NIEFT-logoen til deltakerlisten). Camilla la ved filene og ga lov til å bruke den på Min side 01.10.2026: `kurs/web/static/logo/terapiakademiet.png` (burgunder, til toppen) og
  `terapiakademiet-lys.png` (lys, til den mørke bunnen), begge 751x186 med gjennomsiktig bakgrunn (`kurs/tema.py`). Ingen omstart trengs; fjernes filene, står navnet som tekst. Bunnen viser kontaktadressen `AVSENDER_EPOST` og «Kursadministrasjonen, Institutt for Psykologisk Rådgivning».
  Bildene hun la ved (portretter, ikke i kodemappen) ligger i `C:\IPR-kurs-natt\_bilder\terapiakademiet-2026-10-01` og i OneDrive `Bilder fra Camilla 2026-10-01`.
- Skrifttype som på terapiakademiet.no: systemfonter (Segoe UI, Arial). Ingen fonter lastes ned.

### Kurs med flere samlinger på Min side

`deltakerside.bygg_visning` regner `med_samling` (kurset har mer enn én samling) og gir malene: `samlinger` (kursdagene gruppert per samling med `periode` og `tag` «Pågår nå» / «Neste samling», se `_samlingsgrupper`), `samling` på hver kursdag,
hver programdag og i «I dag» / «Neste kursdag», og `merknad` per kursdag. Programmet får en overskrift per samling (makroen `program` i `_kursside_blokker.html`), filene «Samling 2 · Dag 4 · …». Toppfeltets klokkeslett er «Varierer – se kursdagene»
når dagene har ulike klokkeslett (`_felles_tid`). Navnene er de samme som i bekreftelsen og på kurssiden («Samling N» er kursets eget nummer, også for en ekstradeltaker på noen samlinger). Merkelappen «Samling N av M» finnes ikke lenger.
Tester: `tests/test_samlinger_paa_min_side.py`.

## 2c. Lenke fra terapiakademiet.no

Terapiakademiet.no legger inn en **vanlig lenke** (aldri skjema eller ramme – innloggingssiden kan ikke bygges inn):

- `https://<systemets adresse>/logg-inn` – én felles lenke for alle kurs (anbefalt). Systemet finner riktig kurs selv.
- `https://<systemets adresse>/logg-inn?neste=/kurs/<kode>/deltakerside` – rett til ett kurs (finnes ferdig utfylt øverst på fanen Min side).

`<systemets adresse>` er `BASE_URL`. Tekst og knapp til å lime inn: `dokumentasjon/lenke-til-terapiakademiet.md`. Deltakeren skriver e-postadressen hen meldte seg på med, får en
engangslenke (30 minutter, kan brukes én gang) og lander på Min side. Er innloggingslenkene mange fra samme sted (kurslokale), er grensen 30 per IP per 15 minutter
og 3 per e-postadresse per 15 minutter. Har deltakeren et kurs der Min side ikke er åpen (ikke publisert, tatt ned eller stengt), står kurset på «Mine kurs» med teksten «Min side er ikke åpen for dette kurset» i stedet for «Åpne Min side» (`deltakerside.TEKST_IKKE_APEN`).

**Status 02.10:** Camilla legger lenken inn selv (nettsiden er laget i VS Code og lastes opp med Cyberduck), og først når påmeldingssystemet er helt klart. Adressen `minside.terapiakademiet.no` er godkjent av henne; punktet står i sjekklisten i `DEPLOYMENT.md` avsnitt 6. Hvor lenken skal stå på nettsiden, avgjør hun senere.

## 2d. Innsjekk

Hver kursdag har en **QR-kode** og en **kode på 6 tegn**. De virker bare på selve kursdagen.

- **Vis dem i lokalet:** kurset → Oppsett → «Kursdager og innsjekk» → **Vis QR på skjerm**. Siden viser QR-koden stort, dagens kode og hvor mange som har sjekket inn (oppdateres hvert 20. sekund).
  **Skriv ut plakat** lager en A4-plakat med kursnavn, dato, «dag 2 av 4», QR-kode, tre korte steg og koden. Heng ikke plakaten opp før kursdagen.
- **Deltakeren skanner QR-koden** og skriver e-postadressen hen meldte seg på med (er hen innlogget, også med den personlige lenken til Min side, er det ett trykk). Får hen ikke skannet:
  **«Registrer oppmøte i dag»** på Min side og under Mine kurs etter innlogging (plakaten sier hvor). Den gamle kodesiden `/innsjekk` (kode + e-post uten innlogging) er **fjernet**.
  På **fysiske og hybride** kurs skriver deltakeren da inn dagens kode (kan slås av per kurs under Min side → Innstillinger);
  **digitale kurs med Zoom-import** har ingen knapp (Zoom registrerer oppmøtet dagen etter, minst 30 minutter); **digitale kurs uten Zoom-import** får én knapp uten kode.
- **Bare én gang per dag:** databasen tillater én registrering per deltaker og kursdag (også ved to samtidige skann). Skanner hen igjen, får hen «Du er allerede registrert i dag».
  Er hen innlogget, står også klokkeslettet («kl. 08:47»); en som ikke er innlogget får ikke klokkeslettet (det ville røpet overfor en fremmed når en annen kom).
  Første registrering vinner; kilde og klokkeslett endres aldri.
- **Se og rette:** Deltakere-fanen viser oppmøte per dag: **Q** = QR, **K** = kode (også når deltakeren selv registrerte på nett), **Z** = Zoom, **M** = for hånd. Klikk for å registrere eller fjerne
  (en fjernet dag kan deltakeren registrere på nytt). En registrering vises også i deltakerens Logger som «Oppmøte registrert av deltakeren selv».
- **Kursbevis** krever oppmøte alle kursdager, uansett hvordan det ble registrert, og timer i spesialistløp telles likt for alle kilder.

**Hvorfor e-post og ikke navn?** E-postadressen er allerede unik for hver person (og nøkkelen for innlogging og dublettvern). Navn er ikke unike, staves ulikt, og en «velg navnet ditt»-liste ville vist alle
som har QR-koden hvem som er påmeldt. Svakheten (e-post er ikke hemmelig) er **bare delvis dempet**: e-post gir **aldri innlogging** (det gamle «Husk meg» er fjernet), svaret til en som ikke er innlogget nevner
ikke navn (bare `k***@example.no`) og gir ikke klokkeslettet til en annens registrering, alle **avvisninger** (ukjent e-post, venteliste, avmeldt, annet kurs) har samme tekst, og forsøk er takbegrenset
(per e-post og IP, per QR-kode og IP). Innlogget økt er sterkest og gir ett trykk.

**Ærlig om svakheten (og valget ditt):** svaret ved en **vellykket** registrering skiller seg fra svaret ved en avvisning. Hvem som helst med QR-koden (et bilde av plakaten holder) kan derfor
(a) registrere en påmeldt kollega som er fraværende, bare ved å skrive kollegaens e-postadresse, og (b) forsøke seg fram til hvilke e-postadresser som er påmeldt kurset. Oppmøtet avgjør kursbevis og timer i spesialistløp.
Takbegrensningen (120 per 10 min per IP og QR-kode, 10 per e-post) stopper ikke en som bytter IP-adresse.
**Vil du lukke hullet** (kostnaden er at deltakeren må vente på en e-post i lokalet), sett `INNSJEKK_KREVER_INNLOGGING=1` (`.env` lokalt, App Settings i Azure) og start på nytt. Da vil ikke QR-siden
registrere noe på en e-postadresse alene: deltakeren skriver adressen, får en engangslenke på e-post (`/logg-inn`, samme svar uansett om adressen finnes), klikker den og kommer tilbake til innsjekksiden innlogget, der ett trykk registrerer oppmøtet.
Standard er **av** (raskest i lokalet, som avtalt); det er brukerens valg om standarden skal endres.

**Kjente begrensninger:** QR-kode og kode endres ikke i løpet av dagen (et bilde av dem kan brukes resten av dagen), det finnes ikke klokkeslettvindu, koden kan deles videre, og
«i dag» er serverens dato (`_idag()`; i Azure trolig UTC, så mellom kl. 00:00 og 01:00/02:00 norsk tid regnes gårsdagen). Roterende QR og klokkeslettvindu er ikke bygget.
En glemt dag rettes av administrator (M).

### Hvis noen ikke får skannet QR-koden

Kursleder åpner **oppmøtelisten** på mobilen og trykker på navnene. Deltakeren trenger ikke gjøre noe. QR-koden er fortsatt hovedløsningen, og alternativene finnes som før:
«Registrer oppmøte i dag» på **Min side** og under **Mine kurs** (med dagens kode).

- **Åpne den:** kurset → Oppsett → «Kursdager og innsjekk» → **Ta opp oppmøte** (fylt knapp på dagens dag). Lenken finnes også på QR-siden («Oppmøteliste (hvis noen ikke får skannet)»), øverst under Deltakere når kurset
  har kursdag i dag, og på Oversikt («Ta opp oppmøte i dag»). Adressen er `/admin/kurs/<kurs>/oppmote/<dag>` (og `/admin/kurs/<kurs>/oppmote` åpner dagens dag, ellers den nærmeste). Den som bruker listen må være innlogget i
  administrasjonen som kursadministrator eller systemadministrator. Lesetilgang ser listen, men uten knapper (POST gir 403).
- **Bruk:** alle med plass (Påmeldt og Ekstradeltaker; ikke venteliste, avmeldt, avslått, utgått eller forlatt) står alfabetisk, én stor rad per person. Trykk på raden for «Til stede», trykk igjen for å ta det bort.
  Øverst står «12 av 14 til stede», et søkefelt (navn eller e-post, æøå spiller ingen rolle) og «Ferdig». **Merk alle til stede** og **Fjern alle merkede** spør først, og er avslått mens søket er i bruk. Kursdagene velges med
  fanene øverst. Bare dager til og med i dag kan tas opp (en fremtidig dag viser bare en forklaring; «i dag» er serverens dato, som beskrevet over).
- **Hva som lagres:** oppmøtet får kilde **M** (manuell) og klokkeslett, og teller for kursbevis og timer i spesialistløp som annet oppmøte. Er personen allerede registrert (QR, kode, Zoom), endres ikke kilden. Oppmøte fra Zoom
  fjernes bare etter en ekstra bekreftelse, og aldri av «Fjern alle merkede». Hvert trykk skrives til hendelsesloggen (`oppmote_manuell`, bare id-er, aldri navn) og vises i deltakerens Logger som «Oppmøte registrert/fjernet».
  Handlingene ber om en tilstand («til stede» / «ikke til stede»), ikke «bytt», så samme forespørsel to ganger gir samme resultat.
- **Dårlig nett og uten JavaScript:** trykket lagres uten å laste siden på nytt. Feiler det, går raden tilbake og en rød melding står nederst. Listen henter tilstanden på nytt hvert 20. sekund (noen kan ha skannet QR-koden).
  Uten JavaScript er hver rad et vanlig skjema, og «Merk alle»/«Fjern alle» går via en egen bekreftelsesside.
- Ingen persondata utover navn vises (ikke adresse, telefon eller allergier), og adressene inneholder bare id-er. E-posten ligger bare som skjult søkenøkkel i siden.

## 2e. «Spør oss» og Kunnskapsbase er av som standard

Menyvalgene «Spør oss» (deltakermenyen) og «Kunnskapsbase» (adminmenyen) vises bare når `ASSISTENT_AKTIV=1`. Standard er **0**: `/sporsmal` gir 404 og avlysnings-e-posten nevner ikke «Spør oss».
Koden (`kurs/assistent.py`) og dataene (tabellene `kunnskap` og `henvendelse`) er beholdt, og `/admin/kunnskap` kan fortsatt åpnes på adressen (med en forklaring øverst).
**Sjekk at `ASSISTENT_AKTIV=1` ikke står i `.env` eller i Azure-innstillingene** hvis de skal være av: en eksplisitt `1` holder dem i live.
Slås de på igjen, kommer begge menyvalgene tilbake med en gang etter omstart. Se også `dokumentasjon/menyer-forklart.md`.

## 2f. Adressekravet i webhook og CSV-import (innstillinger)

Deltakerens **private adresse** (adresse, postnummer, poststed) **må være med** når noen melder seg på, uansett vei inn: det offentlige
påmeldingsskjemaet, bedriftspåmeldingen (per deltaker), «Legg til deltaker», **webhooken fra ipr.no** og **CSV-importen**. Grunnen er at
fakturaen sendes **automatisk ved påmelding** (tidligst seks måneder før første kursdag, se «Når sendes fakturaen?» under), og da må
fakturaadressen være klar. Det er ikke mulig å melde seg på uten adresse noe sted: adressefeltene i skjemabyggeren er **låst** (de kan ikke
skjules eller gjøres valgfrie), webhooken svarer 400, og CSV-importen avviser en fil uten kolonnene og blokkerer en rad uten adresse. Også når
arbeidsgiver betaler, er den private adressen påkrevd; firmaets adresse hentes separat fra Enhetsregisteret. En **privat faktura lages aldri uten
komplett privat adresse** (se «Faktura uten privat adresse» under): mangler adressen, holdes fakturaen tilbake og du får beskjed.

Webhooken og CSV-importen har hver sin innstilling. **Begge er på som standard.** De finnes som en **nødbrems** som bare skal settes til `0`
midlertidig hvis noe stopper (for eksempel at skjemaet på ipr.no ikke sender adressen):

| Innstilling (`.env` lokalt, App Setting i Azure) | Standard | På (`1`, standard) | Av (`0`, nødbrems) |
|---|---|---|---|
| `ADRESSE_KREVES_I_WEBHOOK` | `1` | Adressen er påkrevd: mangler den (eller noe av den), svarer webhooken 400 med hvilke felt som mangler, og ingenting registreres | **Påmeldingen går ikke tapt.** Den registreres (201) selv om adressen mangler, også når bare noen av `adresse`, `postnr`, `poststed` er sendt. **Det som kommer inn, tas vare på** som ufullstendige data (det kastes ikke), men bare når personen ikke har noen adresse fra før: en komplett adresse som står der, beholdes, og to halve adresser blandes aldri (de kunne blitt en komplett adresse som aldri har eksistert). Personen merkes «Privat adresse mangler» (ingenting mottatt) eller «Privat adresse er ufullstendig» (bare noe mottatt), bekreftelsen sendes som vanlig, og **fakturaen holdes tilbake til alle tre feltene er komplette**. En ufullstendig adresse gjelder aldri som adresse. Det som er sendt, må være gyldig (ikke for langt, ingen kontrolltegn), ellers 400. Svaret får feltet `merknad`. |
| `ADRESSE_KREVES_I_CSV` | `1` | Filen må ha de tre kolonnene Adresse, Postnr og Poststed, og hver rad må ha hele adressen (filen eller raden blokkeres) | Kolonnene kan mangle i filen, eller stå tomme på en rad. Er noe fylt ut på en rad, kreves alle tre (ellers blokkeres raden i forhåndsvisningen). Personer uten adresse importeres og merkes «Privat adresse mangler» (fakturaen holdes tilbake) |

Bare verdien `0` slår kravet av. En tom eller ukjent verdi (for eksempel `false` eller en skrivefeil) teller som **på**, så kravet aldri forsvinner ved en feil.
Innstillingene leses når appen starter (fra miljøvariabelen, i `kurs/config.py`): en endret App Setting i Azure trer først i kraft når appen er startet på nytt.
Lokalt: legg linjen i `.env` (se `.env.example`) og start på nytt. Sett innstillingen tilbake til `1` (eller fjern linjen) så snart nødbremsen ikke trengs.
**Webhook-nødbremsen er av som standard** og må slås på uttrykkelig; den er bare ment for overgangsperioden hvis ipr.no ennå ikke sender adressen. Når ipr.no er
oppdatert og testet (punkt 2 under), skal den være av igjen, og webhooken krever komplett adresse som normalt.
**Hvorfor er webhook og CSV ulike?** Webhooken kommer fra ipr.no uten at noen kan rette noe underveis, og en påmelding skal ikke gå tapt i overgangsperioden: derfor tar nødbremsen inn også en
ufullstendig adresse (tatt vare på, merket og med fakturaen tilbakeholdt). CSV-importen er en kontrollert administrativ import: der avvises en delvis adresse (brukerens beslutning 01.10.2026), og filen rettes før den importeres.

**Når sendes fakturaen?** Fakturaen sendes **automatisk ved påmelding**, men **tidligst seks kalendermåneder før første kursdag** (ikke 180 dager:
seks måneder er 181–184 dager). Første kursdag er den første datoen i kurset, også når kurset har flere samlinger. Samlet betaling:

- Første kursdag er om **mindre enn seks måneder** (eller dagens dato er nøyaktig seks måneder før første kursdag, eller senere): fakturaen lages straks
  påmeldingen er registrert og bekreftelsen er sendt. Betaler deltakeren selv, går den til hans adresse; firma faktureres etter firmaregelen
  (firmanavn og adresse fra Enhetsregisteret).
- Ligger første kursdag **lenger fram**: fakturaen lages ikke ennå. Påmeldingen får en planlagt dato (første kursdag minus seks kalendermåneder: kursstart
  20.09.2027 gir 20.03.2027, og 31.08.2027 gir 28.02.2027, siste dag i måneden når dagen ikke finnes), og morgenjobben (steg 1c) lager fakturaen den dagen datoen
  nås. Melder noen seg på dagen før datoen, venter fakturaen til morgenjobben på datoen; melder noen seg på **på** datoen, lages den straks.
- Betaling **per samling** har egen regel (én delfaktura per samling, som forfaller et bestemt antall dager før samlingen) og berøres ikke av seks-månedersregelen.
- Påmeldinger fra «Legg til deltaker» og CSV-import holdes tilbake til du behandler dem (deltakervinduet).

**«Privat adresse mangler» / «Privat adresse er ufullstendig».** Personer som er registrert uten komplett privat adresse (ingenting lagt inn = «mangler», bare noen av de tre feltene = «ufullstendig»; det som er mottatt, er tatt vare på) (eldre deltakere og testdata fra før adressen ble påkrevd, eller påmeldinger som kom inn mens nødbremsen var på) er
merket på to steder: en liten gul markering ved navnet i kursets deltakerliste og en rolig varselboks øverst i «Personopplysninger» i deltakervinduet.
Markeringen i listen står bare der det er noe å følge opp: for aktive påmeldinger (ikke Avmeldt, Avslått, Utgått eller Forlatt) på kurs som ikke er avsluttet
eller avlyst. Varselboksen står for alle som mangler adressen, også når kurset er avsluttet. Anonymiserte personer (retten til sletting) får aldri noen av dem.
Resten av vinduet er ikke låst: telefon, arbeidssted m.m. kan lagres som vanlig uten adresse, så eldre testdeltakere kan redigeres uten at adressen må fylles inn først.
Legg inn adressen når du har den: **alle tre feltene** (adresse, postnummer, poststed) må da fylles ut, ellers avvises lagringen. Er den først lagt inn, kan den
endres, men aldri tømmes. Markeringen forsvinner av seg selv. Adressen på en privat betalers fakturaadresse følger med (så lenge fakturaen ikke er sendt).

**Faktura uten privat adresse: holdes tilbake.** En **privat faktura (deltakeren betaler selv) opprettes og sendes aldri uten komplett privat adresse** (adresse, postnummer
og poststed). Mangler adressen, holdes faktureringen tilbake, og du får tydelig beskjed på fire steder:

- **Deltakerlisten:** merket «Privat adresse mangler» ved navnet, fakturastatus «Venter på privat adresse» og en melding øverst på listen.
- **Deltakervinduet:** en varselboks «Fakturering holdt tilbake: privat adresse mangler» øverst, med en knapp som tar deg til Personopplysninger.
- **Oversikten:** et kort og en liste over alle som venter (kurs som er avsluttet er ikke med).
- **«Send bekreftelse og behandle fakturering nå»:** bekreftelsen sendes, men meldingen sier at fakturaen er holdt tilbake (ikke «teknisk feil»).

Bekreftelsen til deltakeren går ut som vanlig; bare fakturaen venter. Når du har fylt inn **alle tre feltene** under Personopplysninger og lagret, lages fakturaen av seg selv:
med en gang for en vanlig påmelding (du ser meldingen «Fakturaen som ventet på adressen er opprettet»), ellers etter kursets vanlige regler (tidligst seks måneder før første
kursdag, eller når delfakturaen forfaller; morgenjobben tar den). Påmeldinger du har holdt tilbake («Behandling holdt tilbake») behandles først når du ber om det. Morgenjobben
melder hver dag hvor mange som venter, og endrer ingenting mens adressen mangler (kjøres den flere ganger, skjer det ikke mer enn første gang). **Ekstradeltaker som settes tilbake til Påmeldt** (bekreftelsen er sendt manuelt, ingen faktura i systemet): påmeldingen er holdt tilbake av systemet, og å lagre en komplett adresse endrer ikke det. Fakturaen lages **ikke** automatisk; du velger selv «Behandle fakturering nå» på deltakersiden (meldingen etter lagringen sier det). Fakturaen til **firma** berøres
ikke: den går til firmaets adresse fra Enhetsregisteret, selv om den private adressen mangler (adressen er likevel påkrevd for nye deltakere, og personen merkes). Starter kurset innen seks måneder
og adressen mangler, lages ikke fakturaen i samme forespørsel som påmeldingen, slik den ellers ville blitt.

**Før produksjon** (dette MÅ være på plass før systemet tas i bruk, se også sjekklisten i `DEPLOYMENT.md` avsnitt 6):

1. **ipr.no-skjemaet/pluginen MÅ sende** tre felt med deltakerens private adresse: `adresse`, `postnr` og `poststed`
   (systemet kjenner også `address`/`gateadresse`, `zip`/`postnummer`/`postal_code` og `city`/`sted`, se `FELTMAP` i `kurs/web/app.py`). Uten dem avvises alle
   påmeldinger fra nettsiden med 400, og ingen kan melde seg på derfra.
2. **Test i testmiljø** (aldri med ekte deltakere) med `curl`. Lag en fil `test.json` med oppdiktede data (kursets kode står i adressen til kurssiden, `/kurs/<kode>`):

   ```json
   {"fornavn": "Test", "etternavn": "Person", "epost": "test.person@example.no", "kurs": "KURSKODE",
    "adresse": "Eksempelveien 1", "postnr": "0150", "poststed": "Oslo"}
   ```

   ```text
   curl.exe -i -X POST https://<testmiljøets adresse>/api/paamelding -H "Content-Type: application/json" -H "X-IPR-Token: <WEBHOOK_HEMMELIG for testmiljøet>" -d "@test.json"
   ```

   Lokalt i demo er adressen `http://127.0.0.1:5000/api/paamelding` og hemmeligheten `demo-webhook-hemmelighet`. Forventet svar: `201` med `paamelding_id` (ny
   påmelding) eller `200 allerede påmeldt` (samme e-post på samme kurs en gang til). Prøv også (endre e-postadressen for hver gang): **uten** adressefeltene
   (forventet `400`, og ingenting registreres), og med **bare noen** av dem, f.eks. bare `adresse` (alltid `400`). Kontroller i deltakervinduet at adressen
   ligger under Personopplysninger. Skjemaet på nettsiden sender normalt selv, så test også selve skjemaet på ipr.no (ikke bare curl).
3. **Kontroller innstillingene:** `ADRESSE_KREVES_I_WEBHOOK` og `ADRESSE_KREVES_I_CSV` skal stå på `1` (standard) eller ikke være satt i Azure Portal → App Service →
   Konfigurasjon → Programinnstillinger (og ikke i `.env`). Står de på `0`, er kravet slått av.
4. **Filimport:** alle som importerer bruker den nye malen (Importer deltakere → «Last ned CSV-mal»): kolonnene Adresse, Postnr og Poststed må være med og fylt ut på hver rad.
5. **Nødbrems:** stopper noe (for eksempel at skjemaet på ipr.no ennå ikke sender adressen), kan `ADRESSE_KREVES_I_WEBHOOK=0` (og/eller `ADRESSE_KREVES_I_CSV=0`) settes
   **midlertidig** (og appen startes på nytt). Ingen data går tapt: påmeldingene tas inn selv om adressen mangler og merkes «Privat adresse mangler», og fakturaen holdes tilbake til
   adressen er lagt inn i deltakervinduet (Oversikten viser hvem som venter). En delvis adresse tas vare på som ufullstendige data (merket «Privat adresse er ufullstendig»). Nødbremsen er **av som standard**. Sett innstillingen tilbake til `1` (eller fjern linjen) så snart
   ipr.no sender adressen riktig og er testet, så webhooken igjen krever komplett adresse som normalt.

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
| Kurset er fullt, men en deltaker på venteliste skal likevel få plass | Åpne deltakeren (eller bruk statusvelgeren i deltakerlisten), velg «Påmeldt» og trykk «Endre status» / «Endre». Du får spørsmålet «Er du sikker … Kurset er fullt» – svarer du ja, meldes deltakeren på og får bekreftelsen. Kurset har da flere påmeldte enn plasser; ingen rykker opp fra ventelisten før det igjen er en ledig plass. Hvem som gjorde det, står i loggen |
| En deltaker kan ikke melde seg på («ikke godkjent») | Påmeldingen er avslått. Gjenopprett ved å endre status på deltakersiden hvis det var feil |
| Hvilken status skal jeg velge? | Det finnes syv statuser, i denne rekkefølgen: **Påmeldt** (den vanlige: alle som har plass står som Påmeldt), **Ekstradeltaker**, **Venteliste**, **Avmeldt**, **Avslått**, **Utgått** og **Forlatt**. **Ekstradeltaker**: deltakeren er **på kurset, men tar ikke plass og teller ikke som fullverdig deltaker**. Se raden «Legge til en ekstradeltaker» under. Du setter den selv, i nedtrekket i deltakerlisten, under «Velg ny status» i deltakervinduet eller med avkryssingen i «Legg til deltaker» – systemet setter den aldri. Påmeldt → Ekstradeltaker **frigjør en plass** (første på ventelisten rykker opp, og personen blir på kurset). Ekstradeltaker → Påmeldt **krever en ledig plass** (er kurset fullt, får du overbooking-spørsmålet). Fra Venteliste, Avmeldt, Avslått, Utgått og Forlatt til Ekstradeltaker går det alltid, også på et fullt kurs (Logger viser «Status endret fra påmeldt til ekstradeltaker» og hvilke samlinger). Nye påmeldinger og de som rykker opp fra ventelisten, blir alltid Påmeldt. **Avmeldt**: deltakeren har selv meldt seg av. **Avslått**: IPR har avslått påmeldingen (kan ikke melde seg på igjen selv). **Utgått**: påmeldingen ble aldri fullført. **Forlatt**: deltakelsen er avsluttet administrativt/ufrivillig, f.eks. ved manglende betaling. Ingen av de fire sender e-post. Forlater en **Påmeldt** plassen (også til Venteliste), rykker første på ventelisten opp og får bekreftelse og faktura som før – aldri den du nettopp satte på venteliste. En ekstradeltaker tok ingen plass, så da rykker ingen opp. Var deltakeren ekstradeltaker, fjernes den statusen og samlingsvalget: kommer vedkommende tilbake, er vedkommende Påmeldt. En sendt faktura krediteres ikke automatisk (gjøres i Visma). Alt står i Logger. Merk: alle som har plass står som «Påmeldt» eller «Ekstradeltaker» overalt i systemet – ordet «Bekreftet» brukes ikke lenger som status. I databasen heter begge fortsatt «bekreftet» (kolonnen `status`; ekstradeltakere skilles av kolonnen `ekstradeltaker_ts`), og e-posten deltakeren får heter fortsatt bekreftelsen. Ekstradeltakere teller **ikke** mot kapasiteten og ikke i «Påmeldt»-tallene (Oversikt, Aktiviteter, kalender, rapporter, deltakerlisten, «plasser igjen» og «Fullt – du settes på venteliste»): de vises ved siden av, f.eks. «12 / 16 + 2 ekstradeltakere». Deltakerne selv ser alltid «Påmeldt» på Min side. Deltakerlisten (utskrift/PDF) åpner med «Påmeldte og ekstradeltakere», så ingen som har plass mangler på lista |
| Legge til en ekstradeltaker (og hva det betyr) | En ekstradeltaker er en som er med på kurset, men ikke som en vanlig deltaker: for eksempel en kollega, en kursholder eller en gjest som bare skal delta på noen samlinger, med en annen pris enn kursets. **Ny person:** «Legg til deltaker» → kryss av «Registrer som ekstradeltaker», og velg «Hele kurset» eller hvilke samlinger (adressen er fortsatt påkrevd). **Allerede påmeldt:** velg «Ekstradeltaker» i statusvelgeren og velg samlinger. Du kan endre samlingene senere i deltakervinduet («Ekstradeltaker: deltar på»). **Hva det betyr:** (1) Den teller ikke som påmeldt og tar ikke plass – ikke mot kapasiteten, ikke i «plasser igjen», og aldri på venteliste, heller ikke når kurset er fullt. Oversikten, Aktiviteter, kalenderen, rapportene og deltakerlisten viser dem ved siden av («12 påmeldte + 2 ekstradeltakere»). (2) **Ingen automatisk bekreftelse eller faktura** – prisen er ikke kursets standardpris. Påmeldingen står som «Behandling holdt tilbake» i deltakervinduet; trykk «Send bekreftelse nå» når du vil sende bekreftelsen (den viser bare deres samlinger og har ingen pris). Fakturaen lager du selv. Er noe allerede sendt før personen ble ekstradeltaker, endres ingenting, og en faktura krediteres ikke. Mislykkes sendingen (for eksempel fordi e-posttjenesten er nede), står «Behandling holdt tilbake» igjen, og du kan prøve på nytt fra deltakervinduet: en ekstradeltaker plukkes aldri opp av den daglige jobben. Sier meldingen at bekreftelsen «ikke er sendt på nytt», er det fordi en bekreftelse til samme e-postadresse og kurs er sendt tidligere (for eksempel før personen ble meldt av): den sendes aldri to ganger. I deltakerlisten står fakturastatusen «Faktureres manuelt» for en ekstradeltaker som ikke har faktura i systemet. **Går personen tilbake til Påmeldt** (krever ledig plass), gjelder de vanlige reglene, men en faktura lages aldri av seg selv når bekreftelsen allerede er sendt. (a) Er bekreftelsen *ikke* sendt ennå, sendes bekreftelsen og fakturaen nå, som for alle andre påmeldte (dialogen sier det). (b) Er bekreftelsen sendt og **alle** fakturaene finnes i systemet (samlet betaling: fakturaen; per samling: alle delfakturaene), endres ingenting, og fakturaene krediteres ikke. (c) Er bekreftelsen sendt manuelt og en faktura gjenstår, holdes påmeldingen tilbake igjen. Det gjelder **uansett om det er én faktura eller flere delfakturaer, og også når personen alt har fått noen**: systemet begynner aldri å fakturere av seg selv bare fordi statusen endres fra Ekstradeltaker til Påmeldt, og bare de fakturaene som gjenstår, holdes tilbake (de som er laget, står og krediteres ikke). Dialogen og meldingen sier fra, og du velger selv «Behandle fakturering nå» i deltakervinduet hvis systemet skal lage de som gjenstår (bekreftelsen sendes ikke på nytt, og er kurset delt i samlinger, lages alle delfakturaene som er forfalt og gjenstår da på én gang; resten følger morgenjobben som vanlig etterpå). **Har du fakturert personen selv utenfor systemet (for eksempel direkte i Visma), skal du ikke trykke på den knappen:** systemet ser ikke fakturaer som er laget utenfor det. Påmeldt og Ekstradeltaker kan byttes også etter at kurset er avsluttet eller avlyst (bare merkelappen, for å rette rapporter og eksport: ingenting sendes, ingen rykker opp, og ingen kapasitetssjekk), men ingen ny person kommer på et lukket kurs. (3) **Samlingsvalget styrer alt som gjelder dager:** påminnelsene (uken før og dagen før, med Zoom-lenken) sendes bare for deres samlinger, bekreftelsen lister bare deres samlinger, innsjekk (QR, kode, Min side, kursside) virker bare på deres dager (ellers «Du er ikke satt opp på denne samlingen. Snakk med kursansvarlig.» – den står med en gang på QR-siden og uten registreringsknapp, og du kan fortsatt registrere oppmøte manuelt; meldingen vises bare når deltakeren er innlogget, en som ikke er innlogget får samme nøytrale svar som for en ukjent e-post, så ingen kan se hvem som er påmeldt). Resultatsiden viser deres egne dagnumre («Dag 1 av 2»), mens QR-plakaten viser kursets («dag 3 av 5») og teller bare de som er satt opp på dagen. Min side og kursside viser bare deres datoer, oppmøtematrisen tones ned utenfor utvalget, og **kursbeviset** krever oppmøte på alle DERES dager, teller timer fra dagene de møtte, og sier hvilke samlinger de deltok på. Kursbeviset utstedes først når hele kurset er avsluttet, også for en som bare var på første samling. **Uke-før-e-posten sendes bare én gang per person:** flytter du en ekstradeltaker til en annen samling etter at den er sendt, kommer det ingen ny uke-før, men dagen-før for den nye samlingen kommer som vanlig (send gjerne en egen e-post fra Deltakere-fanen). Zoom-lenken (ett møte for hele kurset) står på Min side og kurssiden for alle på kurset, men kommer i påminnelsene bare dagen før deres dager. Filer som bare er knyttet til en dag utenfor utvalget, vises ikke på kurssiden og kan ikke hentes. Uten valg er personen på hele kurset (også samlinger som legges til senere). Vil du fjerne en samling som en ekstradeltaker bare er satt opp på, stopper systemet og ber deg endre utvalget først. Endrer du kursdatoene slik at en ekstradeltaker får andre dager (en samling flyttes eller fjernes, eller en dag tas ut), sier meldingen etter lagring fra med navn og dager: utvalget krymper aldri i det stille. En samling du fjerner og legger inn igjen med nøyaktig de samme datoene, beholder utvalget. «Lagre samlinger» har samme versjonskontroll som de andre skjemaene i deltakervinduet: har noen andre endret valget mens siden sto åpen, lagres ingenting. Samlingsvalg som ikke hører sammen avvises med en forklaring: samlinger avkrysset uten «Registrer som ekstradeltaker», eller avkrysset mens «Hele kurset» er valgt (med JavaScript retter skjermbildet seg selv). Kalenderen og årsplanen viser ekstradeltakerne som er på hver samling, og egne samlingsnavn («Grunnkurs EMDR») skrives som de står i meldinger og på kursbeviset. «Akkumulert i løpet» på kursbeviset (spesialistløp) bruker dagens egne timer, samlingens eller kursets, slik selve kursbeviset gjør: har du ikke satt egne timer på en samling eller dag, er tallet det samme som før. (4) I Excel-eksport og deltakerlisten finnes kolonnen «Samlinger» («Hele kurset» eller «Samling 1, 3»). Allergilisten og e-post til alle påmeldte tar med ekstradeltakerne. |
| Endre status på en deltaker direkte i listen | I kolonnen Påmeldingsstatus i deltakerlisten velger du ny status i nedtrekket og trykker «Endre». Du får en dialog som sier hva som skjer (f.eks. at plassen blir ledig og første på ventelisten rykker opp, eller at bekreftelsen sendes). Etter OK kommer du tilbake til samme liste med samme søk og statusvalg, og en melding sier hva som skjedde eller hvorfor det ble stoppet. Siden hopper til raden du endret (meldingen står rett over den), og avkryssede rader beholdes. Er statusen endret av noen andre siden du åpnet listen, skjer ingenting: du får beskjed om det, og lister du på nytt, stemmer dialogen igjen. Et avlyst eller avsluttet kurs tar ikke imot flere: «Påmeldt» og «Ekstradeltaker» avvises (for de som ikke har plass fra før), og ingen rykker opp fra ventelisten. Mellom «Påmeldt» og «Ekstradeltaker» kan du likevel bytte: da endres bare merkelappen (og samlingsvalget), slik at rapporter og eksport kan rettes etter kursslutt. Velger du «Ekstradeltaker» på et kurs med flere samlinger, kommer du først til et eget steg der du velger «Hele kurset» eller hvilke samlinger deltakeren skal være på – ingenting endres før du bekrefter der. Er kurset overbooket (flere påmeldte enn plasser), blir plassen ikke ledig når en går, og dialogen sier det. Lesetilgang ser bare statusen. Reglene er de samme som i deltakervinduet |
| Kursdatoene må endres (flyttet samling, ny dag, ny tid) | Gå til kursets Oppsett → Kursgjennomføring, endre samlingen og trykk «Lagre kursdatoer». Dager som beholdes, beholder innsjekkode og QR. Påmeldingsfristen følger første kursdag, med mindre noen har satt den selv. Deltakerne får **ikke** beskjed automatisk – send dem en e-post fra Deltakere-fanen |
| «Datoene ble ikke lagret. Disse kursdagene kan ikke fjernes …» | Dagen har registrert oppmøte eller en faktura. Behold dagen, eller fjern oppmøtet først (Deltakere-fanen). En fakturert kursdag kan ikke fjernes |
| Prisen eller fakturaoppsettet må endres (også etter at fakturaer er laget) | Kurset → Oppsett → Betaling: endre pris, betalingsmåte, fakturaplan eller «Kurset faktureres samlet til organisasjonen», og trykk «Lagre kursinnstillinger». Har kurset allerede fakturaer, uavklarte fakturaforsøk eller planlagte fakturaer, står det en gul boks under feltene, og så snart du endrer noe, vises en avkryssing du må ta: «Jeg forstår dette og vil endre pris eller fakturaoppsett». Uten den lagres alt annet, men pris og fakturaoppsett står urørt (du får beskjed). **Fakturaer som allerede er laget, endres aldri** – må en av dem rettes, gjøres det i Visma. Ny pris, ny fakturering og nytt antall dager før samlingen gjelder bare fakturaer som ikke er laget ennå (også de som er planlagt til senere); har en deltaker fått faktura for noen av samlingene, deles resten (ny pris minus det som er fakturert) på samlingene som gjenstår. Ny fakturaplan gjelder bare nye påmeldinger – de som er påmeldt, beholder planen de har. En pris som ville gitt en faktura på 0 kr (en deltaker har allerede fått fakturert like mye eller mer og har samlinger igjen) avvises. Endringen skrives til loggen (`kurs_okonomi_endret`: verdiene før og etter, og antall fakturaer, forsøk og planlagte – aldri navn) |
| En deltaker ber om delt faktura (eller én samlet faktura), eller fakturaen må endres | Deltakerne endrer ikke fakturaen selv (Min side er bare lesing), og vilkårene sier at fakturamottakeren ikke kan endres etter påmelding. Men du kan gjøre et **unntak** for én deltaker: åpne deltakeren → «Påmelding og faktura» → **Betaling**, og velg «Samlet» eller «Per samling» (kurset må ha flere samlinger). Det går **så lenge ingenting er fakturert eller forsøkt fakturert**. Påmeldingen går da gjennom fakturaplanen på nytt: en samlet faktura holdes til seks måneder før første kursdag (og lages med en gang hvis kurset starter om mindre enn seks måneder), og delfakturaer lages så mange dager før hver samling som kurset sier (Oppsett → Betaling). Bekreftelsen sendes ikke på nytt: send en e-post under Kommunikasjon hvis du vil gi deltakeren beskjed. Endringen står i Logger (både før og etter). **Etter at en faktura er laget, kan planen ikke byttes** (deltakeren kunne blitt fakturert to ganger): rett det i Visma. Står det en gul boks «Det er laget … faktura» i vinduet, gjelder alt du endrer der, også hvem som betaler, bare fakturaer som ikke er laget ennå |
| Signaturen mangler en logo i en sendt e-post | Bildet ble slettet mens e-posten lå til forhåndsvisning (bilder som brukes i en signatur eller et kurs, kan ikke slettes). Last opp bildet igjen og sett det inn i signaturen |
| «epostkopi_feilet» i loggen | Kopien til e-posthistorikken kunne ikke lagres, og da sendes e-posten ikke. Ingenting er reservert – e-posten sendes ved neste kjøring. Gjentar det seg, sjekk databasen (plass/tilgang) |
| Skjemaet sier «Firmaopplysningene kunne ikke hentes akkurat nå», eller en påmelding er merket «Firmaopplysninger må kontrolleres» | Registeret svarte ikke da påmeldingen ble registrert (offentlig skjema, adminregistrering, bedriftspåmelding eller webhook), eller organisasjonsnummeret er ugyldig (filimport/webhook). Ingen skriver firmanavn eller adresse selv – samme regel for alle veier: påmeldingen er registrert med organisasjonsnummeret alene (hendelsen `enhetsoppslag_utilgjengelig` i loggen), og **fakturaen lages ikke** før firmanavnet er hentet og organisasjonsnummeret er gyldig. Rett et ugyldig nummer i deltakervinduet: da hentes opplysningene på nytt. Morgenjobben prøver oppslaget på nytt hver morgen (steg 0, før fakturaene), eller trykk «Hent firmaopplysninger på nytt» i deltakervinduet («Hent manglende firmaopplysninger» i oversikten og deltakerlisten gjør alle på en gang). Kontroller oppkoblingen med `python -m kurs.brreg_sjekk 974760673` – den viser navn og fakturaadresse fra det ekte registeret. I demo kan forløpet prøves med organisasjonsnummer 999 900 062: registeret «svarer ikke» i skjemaet, men er «oppe igjen» når administrator eller morgenjobben prøver på nytt |
| Nettsidens skjema (webhook) får 400 «deltakerens private adresse er påkrevd» (eller, når nødbremsen er på, «adressen er valgfri, men er noen av feltene sendt, må alle tre være med»), eller en CSV-fil stoppes med «Adresse mangler» / «Adressen er bare delvis utfylt» | Adressen **må være med** (`adresse`, `postnr` og `poststed`), og delvis adresse avvises alltid: er noen av feltene sendt (eller fylt ut på CSV-raden), må alle tre være med. Standard er at både webhooken og CSV-importen krever adressen (`ADRESSE_KREVES_I_WEBHOOK` og `ADRESSE_KREVES_I_CSV` står på `1`): rett skjemaet/filen. Bare verdien `0` slår kravet av, som en midlertidig nødbrems: se 2f. Personer som ble registrert uten adresse (eldre deltakere, eller mens nødbremsen var på) står merket **«Adresse mangler»** i deltakerlisten og i deltakervinduet, til du legger inn adressen der (Personopplysninger). Gamle CSV-filer med «Fakturaadresse» leses ikke som adresse. Last ned malen på nytt (Importer deltakere → «Last ned CSV-mal») |
| Forhåndsvisningen av en CSV-import sier «Adressen avviker fra den registrerte» | Personen finnes fra før med en annen adresse enn i filen. Importen beholder den registrerte adressen (filen kan være eldre). Sjekk adressen i deltakervinduet (Personopplysninger) etter importen, og rett den om den er feil: fakturaadressen på den holdte påmeldingen følger med, så lenge fakturaen ikke er sendt. Har adressen nylig blitt byttet av en offentlig påmelding, står det i Logger («Personopplysninger endret: Adresse») |
| «Feltet har svar … og kan ikke slettes» i skjemabyggeren | Svarene skal ikke gå tapt. Ta bort haken under «Vis», så vises feltet ikke lenger for nye deltakere |
| Noen mangler kursbevis («Kari sier hun ikke har fått kursbevis») | Søk etter henne i søkefeltet øverst på alle adminsider (trykk `/`): navn (med eller uten æøå), e-post, telefon, påmeldingsnummer eller kursnummer. Du kan lime inn avsenderlinjen fra Outlook («Kari Hansen <kari.hansen@…>») rett i feltet. Resultatsiden viser alle kursene hun er påmeldt, oppmøte og om kursbevis er **utstedt** (med dato, en lenke «Åpne kursbeviset» og om e-posten er sendt) – eller hvorfor ikke: «oppmøte 1 av 2 dager (kursbevis utstedes ved fullt oppmøte)», «påmeldingen er ikke bekreftet», «kurset er ikke avsluttet ennå», «kurset er ferdig, men ikke avsluttet i systemet ennå» eller «utstedes av morgenjobben» (kommer ved neste kjøring kl. 07:00, dagen etter siste kursdag). Rett oppmøtet i deltakerlisten hvis det er feil registrert – beviset utstedes da av morgenjobben. Står det **«Oppfyller vilkårene, men er ikke utstedt … sjekk Daglig kjøring»** (rød) eller «kurset sluttet …, men er ikke avsluttet i systemet», har morgenjobben ikke gjort jobben to dager eller mer etter siste kursdag: sjekk at e-post virker (kursbevis utstedes ikke mens e-posttjenesten er nede), se etter `kursbevis_mal_feil` og `daglig_feil` i hendelsesloggen, og at den planlagte oppgaven går. «Ingen e-postutsending registrert» ved et utstedt bevis betyr at beviset finnes, men e-posten til nåværende adresse ikke er logget som sendt: lever det ut manuelt. Søket viser aldri allergier og finner ikke anonymiserte personer |
| En side gir «Noe gikk galt» med en referanse | Søk etter referansen i App Service-loggen (linjen har rute, status og feiltype – aldri persondata) |

## 5. Brukere og tilgang

- Med Microsoft-innlogging styres tilgang med app-rollene i Entra ID (`AZURE-SETUP.md` avsnitt 4). Rolleendring virker
  ved neste innlogging; deaktivering av brukeren i systemet (Admin → Brukere) virker **straks**.
- Nødbruker (lokal innlogging): bare hvis `ADMIN_LOKAL_INNLOGGING=1`. Passord minst 12 tegn. Oppbevares i Key Vault.
- Det må alltid finnes minst én aktiv systemadministrator – systemet nekter å fjerne den siste.
- **Lenken til innloggingen** (systemets adresse, `BASE_URL`, uten noe etter skråstreken) står øverst under Admin → Brukere med «Kopier lenke». Den deles med dem som har fått en bruker: startsiden er innloggingen til Admin og ingenting annet.

## 5b. Årsplanen: skoleferier og egne perioder

- **Skoleferier legges inn hvert år, per område** (Kalender → Årsplan → «Legg inn skoleferie eller egen periode»).
  Datoene er ulike for Oslo og Vestland og hentes fra kommunen/fylkeskommunen – systemet har ingen faste, nasjonale
  skoleferier. Røde dager regnes ut automatisk og skal ikke legges inn.
- **Sperret periode** (for eksempel intern ferie) og **advarsel** (for eksempel «Ikke webinarer i juli») kan gjelde alle
  kurs, bare fysiske eller bare online. Kurs i perioden vises under «Konflikter og merknader» – ingenting stoppes.

## 5c. Kjente begrensninger: Min side og innsjekk (ikke testet av byggerne)

- Editoren for Min side og deltakersiden er prøvd i Edge/Chromium. **Firefox og Safari/iOS er ikke prøvd** (særlig rik tekst, dra med finger, `<dialog>`, opplasting fra telefon).
- **PostgreSQL er ikke kjørt** mot migreringene (`kursside`, `min_side_lenke`), filopplasting eller innsjekk: kjør migreringene og prøv filtrafikk, innsjekk og den personlige lenken mot Azure-testbasen før reell bruk.
- Opplysningene i kortet «Mine opplysninger» kan deltakeren ikke redigere selv (de rettes av administrasjonen), og svar på kursets egne spørsmål i påmeldingsskjemaet vises ikke der.
- Filene på Min side ligger i databasen (base64): 15 MB per dokument, 5 MB per bilde, 100 MB og 200 filer per kurs. Store presentasjoner kan ligge i SharePoint («Fra kursholder»).
- «Åpen til»-datoen og «i dag» bruker serverens dato (se 2d). Innsjekk-begrensningene står i 2d.

## 6. Nøkler og tokens

| Hva | Når | Slik |
|---|---|---|
| `HEMMELIG_NOKKEL` | Ved mistanke om lekkasje | Ny verdi i Key Vault → restart. Alle logges ut; gamle opplastings-/innloggingslenker og personlige lenker til Min side i sendte e-poster slutter å virke (kursholdere får ny lenke ved neste purring; deltakerne logger inn med e-post) |
| Client secrets (Entra, Graph, Zoom, Visma) | Før utløp (Entra: maks 24 mnd) | Ny hemmelighet i Key Vault → restart → slett den gamle |
| Visma-tilgang | Ved «invalid_grant» / fakturaer feiler med `IkkeSendt` | Hent nytt refresh-token (`AZURE-SETUP.md` avsnitt 7), legg det i Key Vault som `VISMA_REFRESH_TOKEN`, og slett det lagrede: `DELETE FROM integrasjon_token WHERE navn='visma_refresh_token';` Fakturaene prøves automatisk igjen neste kjøring |

## 7. Sikkerhetskopi og gjenoppretting

- Azure tar automatisk backup av PostgreSQL (se oppbevaringstid i `AZURE-SETUP.md`). Point-in-time restore gir en
  **ny** server; pek `DATABASE_URL` dit etter kontroll.
- Før hver migrering: manuell `pg_dump` (`MIGRATIONS.md`). Dumpen inneholder personopplysninger – oppbevar den med
  tilgangsstyring og slett den når den ikke trengs.
- Etter en gjenoppretting: gjenta «Retten til sletting» for personer som ble slettet etter at kopien ble tatt (ellers er de
  tilbake). Hold en liste over slettingene utenfor databasen – se `GDPR.md`, «Sikkerhetskopier».
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

## Prøve firmaoppslag med ekte organisasjonsnumre (lokalt)

Demo kjenner bare oppdiktede virksomheter (999 900 003 m.fl.) og gjør ingen nettverkskall, så et ekte organisasjonsnummer gir
«Fant ikke organisasjonsnummeret». Sett `BRREG_LIVE=1` (miljøvariabel eller `.env`, se `.env.example`) for å slå opp ekte
nummer og navn i Enhetsregisteret også i demo. Bare organisasjonsnummer eller søketekst sendes til det åpne registeret; alt
annet i demo (e-post, faktura, Zoom) er uendret. Skal ikke brukes i drift (der er oppslaget alltid ekte) og er av i testene.
`python -m kurs.brreg_sjekk <orgnr>` kontrollerer oppkoblingen uten å endre noe.
