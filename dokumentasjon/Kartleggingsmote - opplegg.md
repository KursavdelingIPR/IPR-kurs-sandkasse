# Kartleggingsmøte – ny kursadministrasjon for IPR

**Deltakere:** Jan + [navn], adm · **Varighet:** ca. 2 timer · **Arbeidsbok:** `IPR kurs – kartlegging og gevinstvurdering.xlsx`

## Mål for møtet

Når møtet er ferdig skal vi ha:

1. **Et bilde av hvordan kursadministrasjonen gjøres i dag**, steg for steg, med grove tidsanslag
2. **En liste over funksjoner** delt i Må / Bør / Kan
3. **En oversikt over integrasjoner**: hvilket Visma-produkt, SharePoint, e-post, Zoom og nettside
4. **Et første anslag på gevinst:** hvor mye tid kan spares, og hva koster det
5. **Enighet om neste steg:** IT-bestilling, hvem fyller ut resten, og neste møte

### Dette vet vi allerede fra nettsiden (sjekket 15.09)

- **Det finnes ikke noe påmeldingsskjema på ipr.no.** Hver kursside har en «Booking»-knapp som lenker til Pindena. Omleggingen på nettsiden er derfor enkel: rundt 45 lenker byttes. Den tunge jobben er å erstatte det Pindena gjør bak knappen.
- **Det er to Pindena-kontoer:** `ipr.pameldingssystem.no` og `iproslo.pameldingssystem.no`. Spør om det er to selskaper, med to regnskap eller Visma-klienter og ulike kontonumre.
- **Pindena brukes til mer enn kurs:** NIEFT-medlemskap, EFT-veiledningspakker, kursbevis og godkjenning av veiledere. Dette må med i kartleggingen.
- Nettsiden er laget i Craft CMS av byrået Bielke&Yang. Vurderingen av nettsiden ligger i en egen rapport.

Det skal **ikke** tas beslutninger om løsning i møtet. Prototypen er et samtaleverktøy og ikke et ferdig forslag.

---

## Før møtet

**Jan**
- [ ] Start prototypen og sjekk at demoen virker: `seed_demo` og deretter `web.app`. Se README.
- [ ] Ha arbeidsboka åpen, helst på delt skjerm
- [ ] Ha mobilen klar til QR-demo. PC og mobil må være på samme nett, ellers vis kode-innsjekk.

**Be adm om å ta med (send gjerne i dag)**
- [ ] Et nylig kurs som eksempel: helst ett spesialistløp-kurs og ett digitalt kurs
- [ ] Omtrentlig antall kurs og deltakere siste år, per kurstype
- [ ] Hva Pindena koster per år, og oppsigelsestid
- [ ] Eksempler på e-poster som sendes ut (bekreftelse, info, påminnelser)
- [ ] Hvordan fakturering gjøres i dag, og hvilket Visma-produkt som brukes
- [ ] En "irritasjonsliste": det som tar mest tid eller går oftest galt

---

## Agenda

| # | Tema | Min | Ark |
|---|---|---|---|
| 0 | Rammer og mål | 10 | – |
| 1 | Følg ett kurs fra idé til avsluttet | 35 | Prosess |
| 2 | Kurstyper og volum | 10 | Kurstyper |
| 3 | Funksjoner: bruk og behov | 20 | Funksjoner |
| 4 | Demo av prototypen | 15 | Funksjoner (kommentar) |
| 5 | Integrasjoner og IT | 15 | Integrasjoner, IT og drift |
| 6 | Rolle: vedlikehold med Claude Code | 10 | – |
| 7 | Grov gevinst og neste steg | 10 | Gevinst |

---

## 0 · Rammer (10 min)

Si noe om:
- Hvorfor: misnøye med Pindena, mye manuelt arbeid, ønske om mer automatikk og en bedre deltakeropplevelse
- Stians løsning som inspirasjon, og at IPR sin situasjon er større og annerledes
- At adm er eksperten på dagens arbeid. Jan og Claude bidrar med det tekniske.
- Tanken om at adm kan vedlikeholde løsningen med Claude Code, **uten** at det er et krav i dag

**Spør:** *Hva ville vært det beste som kunne komme ut av dette for deg?*

## 1 · Følg ett kurs (35 min): viktigste del

Ta utgangspunkt i et **konkret kurs** adm nylig har jobbet med. Gå kronologisk gjennom kursets livsløp, og fyll inn Prosess-arket underveis. Ett steg per rad.

Still de samme spørsmålene for hvert steg:
- **Hva** gjør du, og **i hvilket verktøy**? (Pindena, Outlook, Excel, Visma, SharePoint, telefon …)
- **Hvor ofte**, og **hvor lang tid** tar det? Grove tall holder.
- Hva **går galt** eller er **irriterende**?
- Er det **noen andre** som er involvert? (kursholder, regnskap, leder)
- Finnes det **unntak** som tar uforholdsmessig mye tid?

**Faser og spørsmål som ofte avdekker skjult arbeid:**

| Fase | Spørsmål |
|---|---|
| Oppsett | Hvem bestemmer datoer og pris? Hvordan kommer kurset på nettsiden? Lokale og catering? |
| Påmelding | Kommer påmeldinger andre veier enn skjemaet (e-post, telefon)? Hva skjer når kurset er fullt? Hvor mange avmeldinger, og hva er reglene? |
| Faktura | Hvem betaler oftest, deltaker eller arbeidsgiver? Hvor ofte er fakturainfo feil? EHF og bestillernummer? Delbetaling i spesialistløp? Hvem purrer? Kreditnotaer? |
| Før kurs | Hvilke e-poster sendes, og når? Kopieres noe manuelt (Zoom-lenker, adresser)? |
| Materiell | Hvor ofte kommer presentasjoner for sent? Hvordan sendes de til deltakerne i dag? |
| Kontrakter | Hvem har kontrakt? Hvordan signeres de, og hvor lagres de? |
| Kursdag | Hvordan registreres oppmøte i dag? Navneskilt? Allergilister? |
| Etter kurs | Kursbevis: hvordan lages de? Timer i spesialistløpet: hvem holder oversikt? Evaluering? Fravær? |
| Henvendelser | Hvilke spørsmål får du oftest? Hvor mange e-poster per uke om kurs? |

## 2 · Kurstyper og volum (10 min)

Fyll inn Kurstyper-arket: antall kurs per år, deltakere, kursdager, format og faktureringsmåte. Totalene hjelper med å anslå "Antall per år" i Prosess-arket. For eksempel er antall påmeldinger per år lik antall bekreftelser og fakturaer.

**Avklar spesielt:**
- Hvilke kurs faktureres **per person** og hvilke **samlet til en organisasjon**?
- Spesialistløpet: hvor mange samlinger, hvilke timekrav, og hvem godkjenner?
- Er det kurs der deltakerne ikke skal ha tilgang til Min side?

## 3 · Funksjoner (20 min)

Gå raskt gjennom Funksjoner-arket. Marker **Brukes i dag?** og **Behov** (Må/Bør/Kan/Ikke). Legg til det som mangler nederst.

Tips: Ikke diskuter *hvordan* noe skal løses. Bare *om* det trengs. Spør:
*Hva ville skjedd hvis denne funksjonen ikke fantes?*

## 4 · Demo (15 min)

Kort demo-manus. Admin-passord i demo er `demo`.

1. **Forside → et kurs → meld på en testperson** med arbeidsgiver som betaler. Vis kvitteringen.
2. **Admin → Utboks:** bekreftelses-e-posten er "sendt". Tilbake til kurset: fakturanummer er satt.
3. **Admin → Parterapi-kurset → "Vis QR på skjerm"** for dagens kursdag. Skann med mobilen, eller åpne Min side (lenken i e-posten), trykk «Registrer oppmøte» og tast koden. Oppmøtet dukker opp i matrisen.
4. **Veiledning i gruppe:** kurset er fullt. **Meld av** én deltaker og vis at første på ventelisten får plass og e-post automatisk.
5. **Min side:** logg inn som `kari.nordmann@example.no` (lenken vises direkte i demo). Vis kurs, oppmøte, materiell, kontrakt, kursbevis og EFT-timer.
6. **Admin → Daglig kjøring:** velg en dato om 5 dager og kjør tørt. Vis hvilke e-poster og påminnelser som ville gått ut.
7. **Spør oss (KI-assistenten):**
   - Spør «Hva er fristen for å melde seg av?». Svaret kommer ordrett fra kunnskapsbasen.
   - Spør «Kan jeg få refusjon?». Spørsmålet går til adm.
   - Innlogget som Kari, spør «Hvor holdes kurset?». Svaret hentes fra databasen.
   - Vis **Admin → Kunnskapsbase:** gjør et ubesvart spørsmål om til et godkjent svar.
   - **Spør adm:** Hvilke spørsmål får du oftest? Finnes det en e-posthistorikk vi kan bruke til å fylle kunnskapsbasen?

**Spør etter demoen:** *Hva mangler? Hva er feil? Hva ville gjort hverdagen din lettere?* Noter svarene i Funksjoner-arket.

## 5 · Integrasjoner og IT (15 min)

Gå gjennom Integrasjoner-arket. Det viktigste å få svar på:
- **Visma: hvilket produkt?** (eAccounting/eRegnskap, Visma.net eller Business NXT). Dette avgjør hvordan fakturakoblingen bygges.
- Hvem fører regnskapet: internt eller et regnskapsbyrå?
- **SharePoint:** hvilken site og mappestruktur brukes for kurs i dag?
- Egen **kurs-postboks**?
- **Zoom eller Teams?**
- **Nettside:** hvem drifter den, og kan vi lenke til påmeldingssider?
- **IT-tjenesten:** hvem er kontaktpersonen, og hvordan bestiller vi?

Forklar kort **hvorfor dette må på en server** (Min side, QR og automatikk må gå døgnet rundt) og **hvorfor vi foreslår Azure via IT**. Se IT-bestillingen.

## 6 · Rolle og vedlikehold med Claude Code (10 min)

Utforsk åpent:
- Er dette noe du **har lyst til**? Hva tenker du om å jobbe med kode sammen med en AI-assistent?
- **Hvor mye tid** kan settes av, både i innføringen og per uke senere?
- Hva slags **opplæring** trenger du? Forslag: 3–4 korte økter sammen med Jan. Første økt: endre en e-posttekst, kjøre testene og se endringen i demoen.
- **Hvem er backup** hvis du er borte?
- Hvilke endringer skal du kunne gjøre **selv**, og hva skal alltid gjennom Jan eller IT? (Forslag: tekster og maler selv; faktura, personvern og database sammen med Jan.)

## 7 · Gevinst og neste steg (10 min)

Se Gevinst-arket sammen. Tallene er grove, og det er greit. Avtal:

| Hva | Hvem | Frist |
|---|---|---|
| Fullføre Prosess og Kurstyper med tall | adm | |
| Sjekke Visma-produkt og API-lisens | adm / regnskap | |
| Kostnad og oppsigelsestid for Pindena | adm | |
| Kontakte IT og sende bestilling | Jan | |
| Oppdatere prototypen etter møtet | Jan (+ Claude) | |
| Neste møte: prioritere første versjon (MVP) | alle | |

---

## Beslutninger som må tas etter hvert (ikke i dette møtet)

1. **Hosting:** Azure via IT (anbefalt) eller ekstern server
2. **Omfang for første versjon.** Forslag: påmelding, faktura, e-poster, QR/Zoom-oppmøte og Min side med materiell. Kontrakter, signering, evaluering og AI-FAQ kommer senere.
3. **Overgang fra Pindena:** parallellkjøring på ett kurs, og hvilke data som migreres
4. **Personvern:** DPIA, databehandleravtaler, personvernerklæring og sletteregler
5. **Vedlikeholdsmodell:** hvem gjør hva, og hvordan endringer testes og publiseres
