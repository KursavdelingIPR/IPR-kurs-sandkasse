# Personvern (GDPR) – teknisk sjekkliste

Hvilke personopplysninger systemet behandler, hvor de ligger, hvor lenge, og hvordan de registrertes rettigheter
ivaretas. Dette er den **tekniske** delen. Behandlingsprotokoll, DPIA, databehandleravtaler og personvernerklæring er
IPRs ansvar og står som åpne punkter nederst.

Sikkerhetstiltakene (tilgangsstyring, kryptering i transport, logging osv.) står i `SECURITY.md`.

## 1. Hvilke opplysninger, og hvor

| Hvor (tabell) | Opplysninger | Formål | Kommentar |
|---|---|---|---|
| `deltaker` | navn, e-post, **privat adresse (adresse, postnummer, poststed)**, telefon, arbeidssted, profesjon (yrkestittel), HPR-nummer, Visma kunde-id | Påmelding, kommunikasjon, fakturering, kursbevis | HPR kun for kurs i spesialistløp. Adressen er påkrevd for **nye påmeldinger** (se under tabellen); eldre deltakere kan mangle den |
| `paamelding` | status, betaler, fakturaadresse/-e-post/-referanse, samtykketidspunkt, kommentarer (intern/faktura), avslått-tidspunkt | Påmelding og fakturering | Kommentarfeltene er fritekst – skriv ikke mer enn nødvendig. Betaler deltakeren selv, er fakturaadressen en **kopi** av personens adresse; betaler firma, er den firmaets adresse fra Enhetsregisteret |
| `paamelding_svar` | svar på kursets egne spørsmål i påmeldingsskjemaet (f.eks. land, deling av e-post, nyhetsbrev) | Det kurset spør om | Settes opp per kurs i skjemabyggeren (`kurs_ekstrafelt`). **Skal aldri brukes til helseopplysninger** – skjemabyggeren sier fra. Slettes ved anonymisering |
| `ekstradeltaker_samling` | hvilke samlinger en ekstradeltaker deltar på: to id-er (påmelding og samling), ingen tekst | Sende påminnelser, bekreftelse og kursbevis for riktige samlinger | **Ingen personopplysninger i seg selv** (bare id-er; koblingen til personen går via påmeldingen). Ingen rader = hele kurset. Ryddes når personen slutter å være ekstradeltaker eller forlater plassen, og følger påmeldingen ved sletting. Loggen har bare id-er og antall, aldri navn |
| `sensitivt` | allergier, tilrettelegging | Servering og tilrettelegging på fysiske kurs | **Kan være helseopplysninger (art. 9).** Egen tabell, slettes automatisk (se 2) |
| `oppmote` | oppmøte per kursdag (kilde: QR, kode, Zoom eller manuelt; minutter fra Zoom) | Kursbevis, timer i spesialistløp | Registreres av deltakeren selv (QR-koden i lokalet, dagens kode, eller «Registrer oppmøte i dag» på nett), av Zoom-importen eller av administrator. Hendelsen `oppmote_selv` logger bare id-er |
| `kursside`, `kursside_versjon` | sideinnholdet til kursets Min side (JSON, utkast og publisert, tidligere versjoner): tekster, program, lenker, kontaktpersoner og gruppelister | Min side for deltakerne | Innholdet skrives av administrator. **Bare fornavn og forbokstav** i gruppelister, ingen e-post/telefon til deltakere, aldri sensitive opplysninger. Kontaktpersoner er ansatte (arbeidsopplysninger). Gruppelister tømmes automatisk (se 2) |
| `kursside_fil`, `kursside_fil_innhold` | filer og bilder til Min side (presentasjoner, dokumenter) med filnavn, størrelse og innhold | Min side for deltakerne | Innholdet er kursmateriell, ikke deltakerdata. Følger kurset (slettes med kurset) |
| `faktura` | beløp, fakturanummer, status | Oppfølging av fakturering | Fakturaen i Visma er regnskapsbilaget |
| `dokument`, `dokument_innhold` | kursbevis (navn, kurs, timer) og lenker til personlige dokumenter | Mine kurs (Min konto) | Kursbevis lagres i databasen. Vises bare når deltakeren har bekreftet e-posten (ikke med den personlige lenken alene) |
| `min_side_lenke` | påmeldings-id, versjon, om lenken er stengt, tidspunkt og hvem (en administrator) som stengte eller fornyet den | Den personlige lenken til Min side (knappen i e-postene) | **Ingen personopplysninger i seg selv** (bare id-er; ingen e-post, navn eller adresse). En rad finnes bare når en administrator har stengt eller fornyet lenken. Selve lenken er en signatur som regnes ut og **aldri lagres**. Følger påmeldingen ved sletting |
| `utsending_logg` | mottakeradresse, e-posttype, tidspunkt | Hindre dobbel e-post | **Ikke** innholdet i e-posten |
| `sendt_epost`, `sendt_epost_vedlegg`, `epost_fil` | en uforanderlig kopi av hver e-post systemet sender: emne, innhold, til, fra, hvem som sendte, tidspunkt, status, bilder og vedlegg | E-posthistorikken i deltakervinduet (fanen E-poster): se nøyaktig hva deltakeren fikk | Lagres før sendingen og endres aldri. Aldri allergier/tilrettelegging (sendes aldri på e-post), aldri innloggingslenker eller andre personlige tilgangslenker (token): den personlige lenken til Min side står som `/min/skjult` i kopien. Lagringstid: som deltakeropplysningene ellers |
| `signatur`, `signatur_bilde` | signaturer til e-post: ansattes navn, tittel, telefon og e-post, og logoer | Signatur i e-post fra IPR | Opplysninger om **ansatte** (jobbkontakt), ikke om deltakere. Slettes når signaturen slettes; e-poster som alt er sendt, beholder signaturen i kopien |
| `firmapaamelding(_rad)` | kontaktperson og deltakere ved bedriftspåmelding | Kvittering og påmelding | |
| `henvendelse` | spørsmål fra «Spør oss», e-post | Besvarelse | Fritekst. «Spør oss» er av som standard: ingen nye henvendelser lagres, og eksisterende ligger urørt |
| `innlogging_token` | engangslenker til Mine kurs og Min side (og «bekreft e-posten din») | Innlogging | Gyldig 30 min. Lenken kan ha `?neste=` (en sti på nettstedet, f.eks. `/kurs/PAR-DIG/deltakerside`): ingen persondata |
| `import_forhaandsvisning` | CSV-rader under import | Import av deltakere | Utløper etter minutter, ryddes av morgenjobben |
| `materiell_krav` | kursholders navn og e-post | Purring på materiell | |
| `admin_bruker` | ansattes navn, brukernavn, e-post, Entra-id | Tilgangsstyring | |
| `hendelse` | revisjonslogg | Sporbarhet | **Inneholder aldri navn, e-post eller sensitive verdier** – bare id-er og feltnavn |
| `planlagt_aktivitet` | interne planer i årsplanen | Planlegging | Skriv ikke personopplysninger her |
| `planlagt_kurs`, `planlagt_samling` | kursplanen: kurs, datoer, sted, kursholdernes og veiledernes fornavn (fritekst), interne notater | Planlegging (Kalender og Årsplan) | Ingen deltakerdata. Hendelsesloggen får bare id og datoer. Skriv ikke andre personopplysninger i notatene |
| `sjekkliste_mal`, `sjekkliste_malpunkt`, `planlagt_kurs_sjekkliste`, `sjekkliste_punkt` | sjekklistemaler og sjekklister for kursene (planlagte og kurs i systemet): oppgavetekster (kan ha fornavn på ansatte og kursholdere), frister, hvem som krysset av og når, innlimt tekst (for eksempel lenke til et registreringsskjema) | Oppfølging av kurs (Kalender) | Ingen deltakerdata. Hendelsesloggen får bare id-er og antall, aldri tekst. Punktene med frist sendes i morgen-e-posten til kurspostboksen (intern, `SJEKKLISTE_EPOST`). Lim ikke inn opplysninger om deltakere |
| `kursholder`, `planlagt_rolle` | kursholdernes korte og fulle navn, rollen deres per samling og veiledningsdag (trainer, facilitator, veileder …), om oppdraget er booket i Psybase, hvem som endret og når | Planlegging av kurs (Kalender → Kursholdere) | Ingen deltakerdata. Hendelsesloggen får bare id-er og koder, aldri navn. Ikke i e-post eller CSV. En kursholder som slutter, settes til «ikke aktiv»; navnet kan slettes når rollene ikke trengs lenger |
| `samling_booking`, `sjekkliste_punkt.notat` | booking per samling (kurslokale, hotell, grupperom, lunsj): status og fritekst med hvor, referansenummer og pris, som kan ha navnet på en kursholder som får hotellrom; notat på sjekklistepunktene om hva som er gjort | Oppfølging av kurs (Kalender) | Ingen deltakerdata. Hendelsesloggen får bare id-er, type og status, aldri teksten. Ikke i e-post eller CSV. Skriv ikke opplysninger om deltakere her |

Utenfor databasen: e-poster i kurs-postboksens «Sendte elementer» (Microsoft 365), kursmateriell og personlige mapper i
SharePoint, møter og deltakerrapporter i Zoom, kunder og fakturaer i Visma. I demo: `utboks/` på lokal disk (bare
oppdiktede data).

### Deltakerens private adresse

- **Hva:** adresse, postnummer og poststed. En vanlig personopplysning – **ikke** sensitiv etter art. 9, og ligger derfor
  på `deltaker`, ikke i tabellen `sensitivt`. Postnummeret kontrolleres bare for at det er fylt inn (utenlandske adresser
  er lov); skjemaet sier «4 siffer i Norge».
- **Hvorfor:** ikke alle deltakere får fakturaen betalt av arbeidsgiver, og da må kursadministrasjonen kunne sende
  fakturaen direkte til deltakeren. Adressen er derfor **påkrevd for alle nye påmeldinger på alle kurs**, også gratiskurs og
  når arbeidsgiver betaler, i påmeldingsskjemaet, bedriftspåmeldingen (per deltaker) og «Legg til deltaker». For
  **webhooken `/api/paamelding` og CSV-importen** (kolonnene Adresse, Postnr, Poststed) er kravet en innstilling
  (`ADRESSE_KREVES_I_WEBHOOK` og `ADRESSE_KREVES_I_CSV`) som er **på som standard**: adressen må være med (webhooken svarer 400, CSV-filen eller -raden
  blokkeres). Bare verdien `0` slår kravet av, som en midlertidig nødbrems i overgangsperioden (se `OPERATIONS.md` 2f; av som standard): påmeldingen går da ikke tapt,
  men tas inn og merkes «Privat adresse mangler» eller «Privat adresse er ufullstendig». En ufullstendig adresse fra webhooken tas vare på som ufullstendige data (informasjon som er mottatt, kastes ikke), men bare når personen ikke har adresse fra før, og den gjelder aldri som adresse: fakturaen holdes tilbake til alle tre feltene er komplette. I deltakervinduet
  kan en adresse som står der endres, men aldri tømmes eller gjøres ufullstendig.
- **Bruk:** betaler deltakeren selv, kopieres adressen til fakturaadressen på påmeldingen – på **alle** kurs, også
  gratiskurs (adressen skal alltid legges til) – og fakturaen sendes dit. En privat faktura sendes **aldri uten komplett adresse**: den holdes tilbake til adressen er lagt inn.
  Betaler firma, brukes bare firmaets adresse fra
  Enhetsregisteret. Systemet går **aldri** automatisk over til å fakturere deltakerens private adresse hvis firmaet ikke
  betaler – det avgjør administrasjonen i hvert tilfelle. Byttes betaler fra firma til privat i deltakervinduet, brukes
  deltakerens adresse, og er fakturaadressen tom når fakturaen lages, brukes også da personens adresse.
- **Retting:** rettes adressen i deltakervinduet (Personopplysninger), følger fakturaadressen med på personens påmeldinger
  der deltakeren betaler selv og fakturaen ikke er sendt (ved delt faktura: de delfakturaene som gjenstår), bortsett fra
  der administrator har skrevet en egen fakturaadresse. Firmaadresser og påmeldinger som allerede er fakturert, røres ikke. **Kundekortet i Visma oppdateres
  ikke automatisk** (adressen sendes bare når en ny kunde opprettes): har en eksisterende kunde fått ny adresse, rettes
  kundekortet i Visma.
- **Ny påmelding med samme e-post:** en ny adresse overskriver den gamle (den nyeste er riktigst – som for telefon og
  arbeidssted), og en tom verdi overskriver aldri en eksisterende adresse. CSV-import overskriver aldri en adresse som
  allerede står der; forhåndsvisningen sier fra («Adressen avviker fra den registrerte») når filen har en annen adresse.
  Den offentlige påmeldingen har ingen innlogging, så en ny adresse er ikke bekreftet av personen. Derfor logges hver
  overskriving som «Personopplysninger endret: Adresse» (bare feltnavnet, aldri verdien) og vises i Logger, og en
  overskriving flytter **ikke** fakturaadressen på personens andre, eksisterende påmeldinger – bare administrator gjør det,
  ved å rette adressen i deltakervinduet.
- **Ikke med:** aldri i hendelsesloggen (bare at feltet «Adresse» ble endret), aldri i e-poster (bekreftelser, påminnelser,
  kursbevis, kvittering), aldri i CSV-eksport av deltakere eller i deltakerlisten (ikke en valgbar kolonne). Den vises bare
  for administratorer: i deltakervinduet (Personopplysninger) og i personrapporten (Rapporter → Deltakerregister →
  personen), som er innsynsdokumentet.
- **Oppbevaring og sletting:** samme regel som de øvrige opplysningene om personen (se 2). Ved «Retten til sletting» tømmes
  adressen på personen (og fakturaadressen på påmeldingene), og en ventende forhåndsvisning av CSV-import med personen
  slettes. Adressen ligger igjen i sikkerhetskopiene til de utløper (se «Sikkerhetskopier» under 3). Fakturaen i Visma er
  regnskapsbilag (se manuelle punkter).
- **Åpen vurdering for IPR:** kravet om adresse gjelder også gratiskurs og kurs der arbeidsgiver betaler, og da brukes
  adressen ikke til noen faktura. Formålet «fakturering» dekker ikke det alene: beskriv formålet i personvernerklæringen
  (at kursadministrasjonen alltid skal kunne fakturere deltakeren direkte), og vurder dataminimering (art. 5 c). Ved
  bedriftspåmelding oppgir arbeidsgivers kontaktperson hver deltakers private adresse, ikke deltakeren selv: informasjonsplikten
  (art. 14) må dekkes, for eksempel med en tekst i personvernerklæringen og i kvitteringen til bedriften om at de må
  informere deltakerne. Se «Åpne punkter».
- **Personer registrert før adressen ble påkrevd (og personer registrert uten adresse mens nødbremsen var på)** har ingen komplett adresse. De blir
  **ikke** stoppet: resten av personopplysningene kan lagres som vanlig (eldre testdeltakere kan redigeres uten at adressen må fylles inn først), og systemet **merker** dem tydelig med «Privat adresse mangler» (ingenting lagt inn) eller «Privat adresse er ufullstendig» (bare noen av feltene)
  (en varselboks i Personopplysninger og et merke i kursets deltakerliste, bare for aktive påmeldinger på kurs som ikke er avsluttet eller avlyst).
  Anonymiserte personer får aldri merket eller varselboksen: de skal ikke bes om en ny adresse. Merket er ikke lagret som
  egne data, men følger av at adressen mangler, og forsvinner når adressen er lagt inn – ved neste påmelding (skjemaet krever den) eller av
  administrator i deltakervinduet (alle tre feltene samlet). Merket viser aldri adressen. **En privat faktura lages og sendes aldri uten komplett adresse:**
  er adressen ikke på plass, så fakturaen holdes tilbake (fakturastatus «Venter på privat adresse»), administrator får beskjed i deltakerlisten, deltakervinduet og på Oversikten, og
  fakturaen lages av seg selv når adressen er lagt inn (se `OPERATIONS.md` 2f). Det gjelder også påmeldinger via webhook-nødbremsen og for kurs som faktureres straks. Fakturaen til firma
  berøres ikke (den går til firmaets adresse fra Enhetsregisteret).

## 2. Lagringstid og sletting

| Hva | Regel | Hvordan |
|---|---|---|
| Allergier/tilrettelegging | Slettes 14 dager etter siste kursdag (`SLETT_SENSITIVT_ETTER_DAGER`) | Automatisk, morgenjobben steg 7. Kjøres selv om andre steg feiler |
| Import-forhåndsvisninger | Utløper etter minutter | Automatisk, morgenjobben steg 8 |
| Innloggingslenker | Gyldige i 30 minutter, engangs | Ved bruk |
| Gruppelister på Min side (tabeller med navn) | Tømmes **30 dager etter siste kursdag** (`KURSSIDE_TOM_TABELLER_ETTER_DAGER`), i utkast, publisert side og alle lagrede versjoner. Kopieres aldri til et annet kurs | Automatisk, morgenjobben steg 9 |
| Min side for deltakerne | Stenges for deltakerne som standard **180 dager etter siste kursdag** (`KURSSIDE_ETTERTILGANG_DAGER`). Hvert kurs kan i stedet ha 1-3650 dager, en bestemt dato eller ingen tidsbegrensning (Min side → Innstillinger for siden; valget logges). Gruppelister med navn tømmes uansett 30 dager etter siste kursdag. Innholdet (uten gruppelister) blir liggende med kurset | Automatisk (tilgangen), manuelt (innholdet) |
| Den personlige lenken til Min side | Virker til **30 dager etter siste kursdag** (kurs uten kursdager: 90 dager etter påmeldingen), uansett om siden er åpen lenger. Administrator kan stenge eller fornye den når som helst. Utløpte lenker gir samme «Lenken virker ikke» som ugyldige | Automatisk (utløp), manuelt (stenge/fornye) |
| Alt annet om en deltaker | **Ingen automatisk sletting ennå** | Manuelt per person (se 3). Generell lagringstid må besluttes av IPR |

**Åpen beslutning for IPR:** Hvor lenge skal påmeldinger, oppmøte og kursbevis ligge? Spesialistutdanning kan tilsi
lang lagring (dokumentasjon av timer); regnskapsopplysninger må ligge i 5 år (bokføringsloven § 13) – men de ligger i
Visma. Når regelen er bestemt, kan den legges inn i morgenjobben på samme måte som slettingen av sensitive data.

## 3. De registrertes rettigheter

| Rettighet | Hvordan |
|---|---|
| Innsyn (art. 15) | Deltakeren ser egne kurs, oppmøte, dokumenter og fakturaer under Mine kurs, og Min side for kurs hen er bekreftet på (felles innhold som administrator har lagt ut for alle på kurset, og kortet «Mine opplysninger» med det deltakeren selv ga ved påmeldingen: navn, e-post, telefon, arbeidssted, yrkestittel, hvem som betaler og fakturaopplysningene). Privatadresse, fakturaer og kursbevis vises først etter at deltakeren har bekreftet e-posten (se under). Fullstendig oversikt for admin: **søkefeltet i toppmenyen** (finner personen på tvers av alle kurs) eller Rapporter → Deltakerregister → personen (påmeldinger og fakturaer). Hva som har skjedd med en påmelding, og hvem som gjorde det: deltakervinduet → **Logger**. Hvem som har åpnet allergier/tilrettelegging: Logger → **Innsyn** (kun systemadministrator). Utlevering: skriv ut siden eller eksporter deltakerregisteret |
| Retting (art. 16) | Admin retter på deltakersiden («Personopplysninger»). Endringen gjelder alle kurs personen er meldt på. En rettet adresse følger også med til fakturaadressen på påmeldinger som ikke er fakturert (se 1); kundekortet i Visma må rettes der |
| Sletting (art. 17) | **Rapporter → Deltakerregister → personen → «Retten til sletting»** (kun systemadministrator, krever at man skriver ANONYMISER). Se under |
| Protest / begrensning | Meld av / avslå påmeldingen. Markedsføring gjøres ikke fra systemet |
| Dataportabilitet (art. 20) | CSV-eksport av deltakerregisteret / deltakerlisten. Den private adressen er ikke med i CSV (dataminimering): ber personen om alle opplysningene sine, tas adressen fra personrapporten eller deltakervinduet |

### Slik virker «Retten til sletting»

`db.anonymiser_deltaker` fjerner personopplysningene om personen i **hele** påmeldingssystemet:

- navn → «Anonymisert deltaker», e-post → `anonymisert-<id>@anonymisert.invalid`, telefon, **adresse (adresse, postnummer,
  poststed)**, arbeidssted, profesjon og HPR-nummer slettes
- fakturaadresser, fakturareferanse og kommentarer på påmeldingene slettes
- allergier/tilrettelegging, kursbevis og andre dokumentlenker, innloggingslenker slettes
- e-postadressen i utsendingsloggen, bedriftspåmeldinger og henvendelser erstattes/slettes
- en ventende forhåndsvisning av CSV-import (kortlevd: navn, e-post og adresse) som har personen, slettes
- e-posthistorikken: alle lagrede kopier av e-poster for personens påmeldinger og til personens adresse slettes, og bilder/vedlegg som ingen annen e-post bruker
- anonyme tall (påmelding, oppmøte, faktura) **beholdes**, slik at statistikk og økonomi stemmer

Nektes hvis personen har aktive påmeldinger (påmeldt/venteliste på kurs som ikke er avsluttet/avlyst) – meld av
først. Handlingen logges («deltaker_anonymisert», kun id og antall rader) og kan ikke angres. Personen kan senere melde
seg på igjen og blir da en ny deltaker.

Min side-innholdet endres ikke av anonymiseringen: det inneholder ikke identifiserbare felt utover det administrator selv har skrevet (kortet «Mine opplysninger» leses fra deltakeren og er tomt etter anonymisering,
og den personlige lenken gir da «Lenken virker ikke»). **Skal en person fjernes fra en gruppeliste
på Min side, redigerer og publiserer administrator siden på nytt** (fjern navnet i gruppetabellen under Min side). Uansett tømmes gruppelistene automatisk 30 dager etter kurset.

**Må gjøres manuelt i tillegg:**

1. **Visma:** kundekort og fakturaer er regnskapsbilag og skal normalt beholdes i 5 år. Vurder sammen med regnskap.
2. **Microsoft 365:** e-poster til personen i kurs-postboksens «Sendte elementer».
3. **SharePoint:** personlige mapper (`Kurs/<kode>/Deltakere/<e-post>`), hvis det er brukt.
4. **Zoom:** deltakerrapporter for digitale kurs.

### Sikkerhetskopier

Slettingen gjelder databasen som kjører. PostgreSQL-sikkerhetskopiene (`OPERATIONS.md` avsnitt 7, `AZURE-SETUP.md`: minst
7 dager, anbefalt 14–35) inneholder personen – også adressen – til de utløper av seg selv. De brukes bare til gjenoppretting
og leses ikke ellers. **Gjenopprettes databasen fra en kopi tatt før en sletting, må slettingen gjøres på nytt**, ellers er
personen tilbake. Hendelsen «deltaker_anonymisert» ligger i databasen og forsvinner ved gjenoppretting, så hold en enkel liste
utenfor databasen over slettingene (dato og deltaker-id, ikke navn) så lenge sikkerhetskopiene er innenfor oppbevaringstiden.
Dumper tatt for hånd (`pg_dump` før en migrering) slettes når de ikke trengs.

## 4. Innebygd personvern

- **Dataminimering i eksport:** deltakerlisten har bare nr. og navn som standard; alt annet må krysses av. Allergier og
  tilrettelegging kan aldri eksporteres (verken CSV eller utskriftslisten) – de har egen liste der hver visning logges.
  Deltakerens private adresse er ikke med i noen eksport (verken deltakerlisten, CSV av deltakere eller rapportene).
- **E-post:** allergier/tilrettelegging sendes aldri på e-post, og det gjør heller ikke deltakerens private adresse. En kopi av hver sendt e-post lagres i e-posthistorikken (samme tilgang som deltakervinduet), men aldri innloggingslenker eller andre personlige tilgangslenker: innloggingslenken går ikke via utsendingsmotoren, og purringen til kursholder og bedriftens kvittering sendes uten kopi. Kopien vises i en låst ramme der ingen skript kan kjøre. Svarene på kursets egne spørsmål i
  påmeldingsskjemaet sendes heller ikke på e-post.
- **Egne spørsmål i påmeldingsskjemaet:** svarene vises bare i deltakervinduet og i deltakerlisten når de krysses av.
  Samtykker (f.eks. «Deling av e-post») er aldri forhåndsvalgt, og «Nei» er alltid et reelt valg.
- **Firmaoppslag:** når firma betaler, hentes firmanavn og adresse fra Brønnøysundregistrene (åpne data). Det som
  sendes dit, er organisasjonsnummeret eller søketeksten deltakeren skriver – aldri noe om deltakeren.
  Firmanavn og adresse kommer bare fra registeret – aldri fra deltaker, kontaktperson, nettsiden, filimport eller
  administrator (samme regel for offentlig skjema, adminregistrering, bedriftspåmelding, webhook og filimport). Svarer
  registeret ikke, lagres bare organisasjonsnummeret, og påmeldingen merkes «Firmaopplysninger må kontrolleres» for
  administrator (opplysningene hentes på nytt senere). Fakturaen lages ikke før de er hentet.
- **Min side:** alle deltakere på kurset ser det felles innholdet, så administrator får en fast advarsel («ikke skriv navn, e-postadresser eller helseopplysninger om deltakere»), og publiseringen **blokkeres** hvis en
  bekreftet deltakers e-postadresse står på siden. Tilgangen sjekkes ved hver forespørsel (bare bekreftet påmelding på akkurat det kurset). Siden viser aldri allergier/tilrettelegging, intern kommentar, kursnotat,
  innsjekk-kode, HPR-nummer, rabattkode eller andres opplysninger (eksplisitte kolonnelister). Kortet «Mine opplysninger» viser bare det deltakeren selv ga, til deltakeren selv.
  Logger inneholder bare id-er og tellere.
- **Den personlige lenken til Min side:** lenken står i e-postene og gir tilgang uten innlogging, så den behandles som en hemmelig nøkkel: signert (128 bit) og aldri lagret, virker til 30 dager etter siste kursdag, kan stenges og fornyes (også økter som alt er åpnet med lenken avsluttes da, og en rettet e-postadresse gir automatisk en ny lenke),
  står aldri i e-posthistorikken (`/min/skjult`) og vises ikke for brukere med lesetilgang. Den gir **to nivåer**: med lenken alene ser deltakeren det felles innholdet, innsjekk og egne opplysninger (firmaets fakturaadresse
  vises, men **ikke privatadressen, fakturaer, kursbevis og dokumenter**); de krever at deltakeren først bekrefter e-posten (engangslenke til den registrerte adressen, aldri en adresse fra skjemaet). Slik er skaden begrenset hvis en
  lenke havner hos feil person (videresendt e-post, delt skjerm): privatadresse, fakturaer og kursbevis er beskyttet av den bekreftede e-posten.
- **Innsjekk:** deltakeren identifiseres med innlogget økt eller e-post, aldri navn. Svaret til en som ikke er innlogget nevner ikke navn (bare en delvis skjult adresse) og ikke klokkeslettet til en annens registrering;
  alle avvisninger har samme tekst. Svaret ved suksess kan likevel skilles fra en avvisning (en fremmed med QR-koden kan teste om en e-postadresse er påmeldt): `INNSJEKK_KREVER_INNLOGGING=1` fjerner det (se `OPERATIONS.md` 2d).
  E-post gir aldri innlogging. Loggen (`oppmote_selv`) har bare id-er.
- **Logger:** hendelsesloggen og applikasjonsloggen inneholder aldri navn, e-post, begrunnelser eller fritekst.
  Eksporter logges med kolonnenavn og antall, ikke innhold.
- **Tilgang:** rollene system / kursadministrator / lesetilgang. Lesetilgang kan ikke eksportere eller endre noe.
- **Deltakersøk** (søkefeltet i adminmenyen, `/admin/sok`): finner personer på tvers av alle kurs på navn, e-post, telefon,
  HPR-nummer, påmeldingsnummer og kursnummer/kurskode.
  - *Hva resultatet viser:* navn, e-post, telefon og arbeidssted, HPR-nummer bare når søket traff på det, og for hver
    påmelding: kurs, datoer, status, oppmøte (antall dager) og om kursbevis er utstedt (og ellers hvorfor ikke). Ikke mer enn
    deltakerregisteret og deltakersiden allerede viser til de samme rollene, så lesetilgang kan søke.
  - *Hva det aldri viser:* allergier/tilrettelegging (tabellen `sensitivt` leses ikke), innloggingslenker, fakturaadresser
    eller kommentarer. **Anonymiserte personer kan ikke finnes.**
  - *Søketeksten lagres ikke av appen:* verken i hendelsesloggen, applikasjonsloggen eller feilmeldinger (loggen har bare
    rutemønster, status og referanse). Svarene sendes med `Cache-Control: no-store`. Søket gjør ingen endringer i databasen.
  - *Rullegardinen* i søkefeltet sender teksten som POST (i kroppen, med CSRF-hode), ikke i adressen. Ellers ville hvert
    tastetrykk («ka», «kari», «kari.h», …) stått som en adresse i alle URL-logger utenfor appen.
  - *Referer:* søkesidene svarer med `Referrer-Policy: no-referrer`, så nettleseren ikke sender adressen med `?q=…` som
    `Referer` når man klikker seg videre fra resultatsiden (personside, deltakervindu, deltakerliste).
  - *Forbehold:* det ferdige søket (Enter, eller «Søk»-knappen) åpner resultatsiden, og der står søketeksten i adressen
    (`?q=…`), så den kan havne i nettleserens historikk. Det samme gjelder Rapporter → Deltakerregister (`?sok=…`).
    **HTTP-loggen (web server logging, `AppServiceHTTPLogs`) i Azure App Service skal stå av** (se AZURE-SETUP.md): den lagrer
    adressen med spørrestrengen og `Referer`, og appens egen loggdisiplin (ingen søketekst) rekker ikke dit. Det samme gjelder
    den personlige lenken til Min side (`/min/<lenke>`): den står i adressen og er en nøkkel til deltakerens side, så den skal heller ikke ligge i en URL-logg
    (svaret har `Referrer-Policy: no-referrer`, og åpningen er en viderekobling, så lenken står ikke i `Referer` fra siden). Står den på,
    må oppbevaringen kortes ned og tilgangen begrenses. Gunicorn startes uten `--access-logfile`.
- **Demo:** oppdiktede data (`kurs/seed_demo.py`), og testene bruker bare `@eksempel.no`-, `@x.no`- og lignende adresser.

## 5. Databehandlere (må ha avtale)

| Leverandør | Hva de får | Merknad |
|---|---|---|
| Microsoft (Azure, Microsoft 365) | Hele databasen (Azure Database for PostgreSQL), e-post, SharePoint | Norway East |
| Zoom | Navn/e-post til deltakere på digitale kurs (via møtet) | |
| Visma | Kundenavn, e-post, adresse, fakturaer | |
| Anthropic (kun hvis «Spør oss» med KI er slått på) | Spørsmålet og relevante fakta om kurset/deltakerens påmelding | «Spør oss» er **av som standard** (`ASSISTENT_AKTIV=0`); slås på med `ASSISTENT_AKTIV=1`. Uten nøkkel brukes ordmatching lokalt |

## 6. Åpne punkter for IPR

- [ ] Behandlingsprotokoll (art. 30) og DPIA for behandlingen av helseopplysninger (allergier/tilrettelegging)
- [ ] Databehandleravtaler: Microsoft, Zoom, Visma (og Anthropic hvis KI brukes)
- [ ] Personvernerklæring: påmeldingsskjemaene lenker til `https://www.ipr.no/om-oss/personvernerklæring` – kontroller at
      siden finnes og beskriver denne behandlingen (påmelding, allergier, Zoom, Visma, KI)
- [ ] Lagringstid for påmeldinger/oppmøte/kursbevis (se 2)
- [ ] Deltakerens private adresse kreves også på gratiskurs og når arbeidsgiver betaler: beskriv formålet i
      personvernerklæringen og vurder dataminimering (se «Deltakerens private adresse»)
- [ ] Bedriftspåmelding: kontaktpersonen oppgir deltakernes private adresser – dekk informasjonsplikten (art. 14) overfor
      deltakerne
- [ ] Skal en offentlig påmelding kunne overskrive en adresse som står der fra før? I dag ja (den nyeste er riktigst), og det
      logges – alternativet er at bare administrator kan endre en eksisterende adresse
- [ ] Rutine for henvendelser om innsyn/sletting (hvem gjør hva, innen 30 dager)
