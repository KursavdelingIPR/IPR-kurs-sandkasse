# IPR Kurs – sandkasse

Hei Camilla!

Dette er en **sandkasse** for det nye kurssystemet. En sandkasse er en kopi der du trygt kan prøve,
endre og ødelegge ting. Alt kjører på din egen PC. **Ingenting sendes på ordentlig:**
e-poster havner i en «utboks» du kan se i admin, og fakturaer og Zoom-møter er bare liksom.

Alle personer og kurs i sandkassen er oppdiktet.


## 1. Før du begynner (én gang per PC)

Du trenger to programmer:

1. **Python 3.12**
   Last ned fra https://www.python.org/downloads/
   VIKTIG: huk av **«Add python.exe to PATH»** nederst i første bilde av installasjonen.
   Får du ikke lov til å installere, be IT om hjelp.

2. **Claude (desktop-appen)** med Claude Code
   Last ned fra https://claude.ai/download og logg inn med IPR-kontoen din.


## 2. Sett opp sandkassen (én gang per PC)

1. Pakk ut zip-fila til en mappe som **ikke** ligger i OneDrive, for eksempel:
   `C:\IPR\ipr-kurs`
   (OneDrive synkroniserer filer mens programmet bruker dem, og det gir rare feil.)
2. Åpne mappen og **dobbeltklikk `oppsett.bat`**.
3. Vent til det står **«Ferdig! Sandkassen er klar.»** Det tar et par minutter første gang.

Gjør det samme på den andre PC-en.


## 3. Bruke sandkassen

| Hva du vil gjøre | Slik gjør du det |
|---|---|
| Starte | Dobbeltklikk **`start.bat`**. Nettleseren åpner seg av seg selv. |
| Stoppe | Lukk det svarte vinduet som åpnet seg. |
| Begynne på nytt med ferske demodata | Stopp først, og dobbeltklikk så **`nullstill-demo.bat`**. |

Sider du kan prøve (mens sandkassen kjører):

- **http://127.0.0.1:5000** – kursoversikt og påmelding (det deltakerne ser)
- **http://127.0.0.1:5000/admin** – admin. Brukernavn: **admin**, passord: **demo**
- **http://127.0.0.1:5000/sporsmal** – «Spør oss», KI-assistenten
- **http://127.0.0.1:5000/logg-inn** – Min side. Skriv `kari.nordmann@example.no`, og klikk lenken som vises.
- **http://127.0.0.1:5000/innsjekk** – innsjekk med kode

Nytt i admin: **Aktiviteter → Årsplan** (hele året, ledige uker), **Deltakerliste** på hvert kurs (velg kolonner,
skriv ut / lagre som PDF / CSV), **Avslå** på deltakersiden, **Rapporter → Økonomi** og **Uavklarte operasjoner**
på oversikten. Hva som gjenstår før drift: `STATUS-PAMELDINGSSYSTEM.md`.

Tips: I admin under **Daglig kjøring** kan du «spole» til en annen dato og se hvilke e-poster,
påminnelser og fakturaer systemet sender automatisk. Se resultatet under **Utboks**.


## 4. Jobbe med Claude Code

1. Åpne Claude-appen og velg **Code**.
2. Velg mappen `C:\IPR\ipr-kurs` som prosjekt.
3. Skriv hva du vil på vanlig norsk. Claude leser fila `CLAUDE.md`, som forklarer regler og hvordan prosjektet henger sammen.

Gode første oppgaver å prøve:

- «Forklar meg hvordan en påmelding fungerer, fra skjema til faktura.»
- «Start sandkassen for meg.»
- «Endre teksten i bekreftelses-e-posten slik at den også sier …»
- «Legg til et felt for telefonnummer til kontaktperson i påmeldingsskjemaet.»
- «Kjør testene og fortell meg om noe er feil.»

Be gjerne Claude om å **forklare hva den har endret** før du går videre.


## 5. To PC-er og GitHub

Hver PC har **sin egen kopi**. Endringer du gjør hjemme, finnes **ikke** automatisk på jobb-PC-en.
Løsningen er **GitHub**: en felles, privat kopi på nett som begge PC-ene henter fra og sender til.

### Legge prosjektet på GitHub (gjøres én gang, fra én PC)

Du trenger:
- En konto på https://github.com (gratis). Bruk gjerne jobb-e-posten.
- **Git** installert: https://git-scm.com/download/win (standardvalgene i installasjonen er fine).

Deretter, fra PC-en der du har pakket ut og kjørt `oppsett.bat`:

1. Opprett et **nytt, privat** repository på GitHub, for eksempel `ipr-kurs`. Ikke huk av for README eller .gitignore.
2. Åpne prosjektet i Claude Code og skriv:
   «Gjør denne mappen til et Git-repo, lag første innsjekking, og send den til GitHub-repoet mitt
   https://github.com/BRUKERNAVN/ipr-kurs. Forklar hvert steg.»
3. Første gang ber Git deg logge inn på GitHub i nettleseren. Gjør det.

Demodata, `.venv` og eventuelle nøkler blir **ikke** sendt til GitHub (det styres av fila `.gitignore`).

### Hente prosjektet på den andre PC-en

1. Installer Python, Git og Claude som i punkt 1.
2. I Claude Code: «Klon https://github.com/BRUKERNAVN/ipr-kurs til C:\IPR\ipr-kurs.»
3. Dobbeltklikk `oppsett.bat` i den nye mappen.

### Daglig bruk med to PC-er

- **Når du begynner:** «Hent siste versjon fra GitHub.»
- **Når du er ferdig:** «Send endringene mine til GitHub med en kort beskrivelse.»

Da har begge PC-ene alltid det samme. Glemmer du å sende før du bytter PC, er det ikke farlig –
Claude Code hjelper deg å slå sammen endringene.

Inntil GitHub er på plass: gjør endringer på **én** PC, og bruk den andre bare til å se og prøve.


## 6. Regler i sandkassen

- **Aldri ekte personopplysninger** – bare oppdiktede navn og e-poster.
- **Aldri passord eller nøkler** inn i filer eller i chatten med Claude.
- Ødelegger du noe: be Claude Code om hjelp, eller pakk ut zip-fila på nytt.


## 7. Hva ligger i mappen

| Fil / mappe | Hva det er |
|---|---|
| `LES MEG FORST.md` | Denne veiledningen |
| `oppsett.bat` / `start.bat` / `nullstill-demo.bat` | Dobbeltklikk-filer for oppsett, start og nullstilling |
| `CLAUDE.md` | Regler og forklaringer for Claude Code |
| `README.md` | Teknisk oversikt |
| `kurs/` | Selve programmet |
| `kurs/maler/epost/` | Tekstene i e-postene som sendes ut – enkle å endre |
| `tests/` | Automatiske tester som sjekker at alt virker |
| `dokumentasjon/` | Bakgrunn: møteopplegg, kartleggingsark, IT-henvendelse og Stians referanseløsning |

Spørsmål? Ta kontakt med Jan.
