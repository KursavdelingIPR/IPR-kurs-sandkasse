# Personvern (GDPR) – teknisk sjekkliste

Hvilke personopplysninger systemet behandler, hvor de ligger, hvor lenge, og hvordan de registrertes rettigheter
ivaretas. Dette er den **tekniske** delen. Behandlingsprotokoll, DPIA, databehandleravtaler og personvernerklæring er
IPRs ansvar og står som åpne punkter nederst.

Sikkerhetstiltakene (tilgangsstyring, kryptering i transport, logging osv.) står i `SECURITY.md`.

## 1. Hvilke opplysninger, og hvor

| Hvor (tabell) | Opplysninger | Formål | Kommentar |
|---|---|---|---|
| `deltaker` | navn, e-post, telefon, arbeidssted, profesjon (yrkestittel), HPR-nummer, Visma kunde-id | Påmelding, kommunikasjon, fakturering, kursbevis | HPR kun for kurs i spesialistløp |
| `paamelding` | status, betaler, fakturaadresse/-e-post/-referanse, samtykketidspunkt, kommentarer (intern/faktura), avslått-tidspunkt | Påmelding og fakturering | Kommentarfeltene er fritekst – skriv ikke mer enn nødvendig |
| `sensitivt` | allergier, tilrettelegging | Servering og tilrettelegging på fysiske kurs | **Kan være helseopplysninger (art. 9).** Egen tabell, slettes automatisk (se 2) |
| `oppmote` | oppmøte per kursdag (kilde, minutter fra Zoom) | Kursbevis, timer i spesialistløp | |
| `faktura` | beløp, fakturanummer, status | Oppfølging av fakturering | Fakturaen i Visma er regnskapsbilaget |
| `dokument`, `dokument_innhold` | kursbevis (navn, kurs, timer) og lenker til personlige dokumenter | Min side | Kursbevis lagres i databasen |
| `utsending_logg` | mottakeradresse, e-posttype, tidspunkt | Hindre dobbel e-post | **Ikke** innholdet i e-posten |
| `firmapaamelding(_rad)` | kontaktperson og deltakere ved bedriftspåmelding | Kvittering og påmelding | |
| `henvendelse` | spørsmål fra «Spør oss», e-post | Besvarelse | Fritekst |
| `innlogging_token` | engangslenker til Min side | Innlogging | Gyldig 30 min |
| `import_forhaandsvisning` | CSV-rader under import | Import av deltakere | Utløper etter minutter, ryddes av morgenjobben |
| `materiell_krav` | kursholders navn og e-post | Purring på materiell | |
| `admin_bruker` | ansattes navn, brukernavn, e-post, Entra-id | Tilgangsstyring | |
| `hendelse` | revisjonslogg | Sporbarhet | **Inneholder aldri navn, e-post eller sensitive verdier** – bare id-er og feltnavn |
| `planlagt_aktivitet` | interne planer i årsplanen | Planlegging | Skriv ikke personopplysninger her |

Utenfor databasen: e-poster i kurs-postboksens «Sendte elementer» (Microsoft 365), kursmateriell og personlige mapper i
SharePoint, møter og deltakerrapporter i Zoom, kunder og fakturaer i Visma. I demo: `utboks/` på lokal disk (bare
oppdiktede data).

## 2. Lagringstid og sletting

| Hva | Regel | Hvordan |
|---|---|---|
| Allergier/tilrettelegging | Slettes 14 dager etter siste kursdag (`SLETT_SENSITIVT_ETTER_DAGER`) | Automatisk, morgenjobben steg 7. Kjøres selv om andre steg feiler |
| Import-forhåndsvisninger | Utløper etter minutter | Automatisk, morgenjobben steg 8 |
| Innloggingslenker | Gyldige i 30 minutter, engangs | Ved bruk |
| Alt annet om en deltaker | **Ingen automatisk sletting ennå** | Manuelt per person (se 3). Generell lagringstid må besluttes av IPR |

**Åpen beslutning for IPR:** Hvor lenge skal påmeldinger, oppmøte og kursbevis ligge? Spesialistutdanning kan tilsi
lang lagring (dokumentasjon av timer); regnskapsopplysninger må ligge i 5 år (bokføringsloven § 13) – men de ligger i
Visma. Når regelen er bestemt, kan den legges inn i morgenjobben på samme måte som slettingen av sensitive data.

## 3. De registrertes rettigheter

| Rettighet | Hvordan |
|---|---|
| Innsyn (art. 15) | Deltakeren ser egne kurs, oppmøte, dokumenter og fakturaer på Min side. Fullstendig oversikt for admin: Rapporter → Deltakerregister → personen (påmeldinger og fakturaer). Utlevering: skriv ut siden eller eksporter deltakerregisteret |
| Retting (art. 16) | Admin retter på deltakersiden («Personopplysninger»). Endringen gjelder alle kurs personen er meldt på |
| Sletting (art. 17) | **Rapporter → Deltakerregister → personen → «Retten til sletting»** (kun systemadministrator, krever at man skriver ANONYMISER). Se under |
| Protest / begrensning | Meld av / avslå påmeldingen. Markedsføring gjøres ikke fra systemet |
| Dataportabilitet (art. 20) | CSV-eksport av deltakerregisteret / deltakerlisten |

### Slik virker «Retten til sletting»

`db.anonymiser_deltaker` fjerner personopplysningene om personen i **hele** påmeldingssystemet:

- navn → «Anonymisert deltaker», e-post → `anonymisert-<id>@anonymisert.invalid`, telefon, arbeidssted, profesjon og
  HPR-nummer slettes
- fakturaadresser, fakturareferanse og kommentarer på påmeldingene slettes
- allergier/tilrettelegging, kursbevis og andre dokumentlenker, innloggingslenker slettes
- e-postadressen i utsendingsloggen, bedriftspåmeldinger og henvendelser erstattes/slettes
- anonyme tall (påmelding, oppmøte, faktura) **beholdes**, slik at statistikk og økonomi stemmer

Nektes hvis personen har aktive påmeldinger (bekreftet/venteliste på kurs som ikke er avsluttet/avlyst) – meld av
først. Handlingen logges («deltaker_anonymisert», kun id og antall rader) og kan ikke angres. Personen kan senere melde
seg på igjen og blir da en ny deltaker.

**Må gjøres manuelt i tillegg:**

1. **Visma:** kundekort og fakturaer er regnskapsbilag og skal normalt beholdes i 5 år. Vurder sammen med regnskap.
2. **Microsoft 365:** e-poster til personen i kurs-postboksens «Sendte elementer».
3. **SharePoint:** personlige mapper (`Kurs/<kode>/Deltakere/<e-post>`), hvis det er brukt.
4. **Zoom:** deltakerrapporter for digitale kurs.

## 4. Innebygd personvern

- **Dataminimering i eksport:** deltakerlisten har bare nr. og navn som standard; alt annet må krysses av. Allergier og
  tilrettelegging kan aldri eksporteres (verken CSV eller utskriftslisten) – de har egen liste der hver visning logges.
- **E-post:** allergier/tilrettelegging sendes aldri på e-post. Innholdet i e-poster lagres ikke.
- **Logger:** hendelsesloggen og applikasjonsloggen inneholder aldri navn, e-post, begrunnelser eller fritekst.
  Eksporter logges med kolonnenavn og antall, ikke innhold.
- **Tilgang:** rollene system / kursadministrator / lesetilgang. Lesetilgang kan ikke eksportere eller endre noe.
- **Demo:** oppdiktede data (`kurs/seed_demo.py`), og testene bruker bare `@eksempel.no`-, `@x.no`- og lignende adresser.

## 5. Databehandlere (må ha avtale)

| Leverandør | Hva de får | Merknad |
|---|---|---|
| Microsoft (Azure, Microsoft 365) | Hele databasen (Azure Database for PostgreSQL), e-post, SharePoint | Norway East |
| Zoom | Navn/e-post til deltakere på digitale kurs (via møtet) | |
| Visma | Kundenavn, e-post, adresse, fakturaer | |
| Anthropic (kun hvis «Spør oss» med KI er slått på) | Spørsmålet og relevante fakta om kurset/deltakerens påmelding | Kan slås av med `ASSISTENT_AKTIV=0`; uten nøkkel brukes ordmatching lokalt |

## 6. Åpne punkter for IPR

- [ ] Behandlingsprotokoll (art. 30) og DPIA for behandlingen av helseopplysninger (allergier/tilrettelegging)
- [ ] Databehandleravtaler: Microsoft, Zoom, Visma (og Anthropic hvis KI brukes)
- [ ] Personvernerklæring: påmeldingsskjemaene lenker til `https://www.ipr.no/om-oss/personvernerklæring` – kontroller at
      siden finnes og beskriver denne behandlingen (påmelding, allergier, Zoom, Visma, KI)
- [ ] Lagringstid for påmeldinger/oppmøte/kursbevis (se 2)
- [ ] Rutine for henvendelser om innsyn/sletting (hvem gjør hva, innen 30 dager)
