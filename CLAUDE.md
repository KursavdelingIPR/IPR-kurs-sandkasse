# CLAUDE.md – IPR Kurs

Kurssystem for Institutt for Psykologisk Rådgivning (erstatter Pindena). Vedlikeholdes av administrasjonen
med Claude Code. **Brukeren er ikke nødvendigvis utvikler:** forklar endringer på norsk, i klartekst,
og vis hva som endres før du gjør større ting.

## Kommandoer (Windows / PowerShell)

```powershell
$py = ".\.venv\Scripts\python.exe"   # lages av oppsett.bat
& $py -m pytest tests            # ALLTID etter en endring
& $py -m kurs.seed_demo          # nullstill demodata
& $py kjor.py                    # start lokalt (eller dobbeltklikk start.bat) på http://127.0.0.1:5000 (admin: demo)
& $py -m kurs.daglig --tor --dato 2027-01-10   # se hva morgenjobben gjør en gitt dag
```

## Faste regler

1. **Personvern først.** Aldri ekte deltakerdata i demo, tester eller eksempler. Allergier/tilrettelegging ligger
   i tabellen `sensitivt` og skal aldri logges, eksporteres i CSV eller sendes på e-post.
2. **Ingen hemmeligheter i koden.** Nøkler ligger i `.env` (lokalt) eller Key Vault (drift). Aldri skriv dem inn i filer.
3. **All kontakt med omverdenen går via `kurs/integrasjoner/`**, og hver funksjon der må ha en demo-gren
   (`if config.DEMO:`) som ikke gjør nettverkskall.
4. **All automatisk utsending skal gå via `Kjoring.send_en_gang()`** slik at den dedupliseres og respekterer tørrkjøring.
   Ny type e-post = ny mal i `kurs/maler/epost/` + unik `type`-streng.
5. **Daglig jobb skal være idempotent.** Kjøres den to ganger samme dag, skal ingenting skje andre gang. Skriv test for det.
6. **Fakturering:** aldri to fakturaer per påmelding (`faktura.paamelding_id` er UNIQUE). Endringer i `visma.py`
   testes i demo og mot Visma sandbox før prod.
7. **Databaseendringer:** oppdater `schema.sql`. Når systemet er i drift må endringer skje som migrering
   (ikke slette/lage tabeller på nytt) – spør Jan før du endrer eksisterende kolonner.
8. **Ikke kjør noe mot prod** (`MODUS=prod`) uten at brukeren uttrykkelig ber om det.

## Stil

- Norsk i domenenavn, brukertekster og kommentarer (æøå i tekst til brukere; ASCII er ok i kodenavn, f.eks. `paamelding`).
- Små, lesbare funksjoner. Ingen nye rammeverk/avhengigheter uten god grunn.
- HTML-maler bruker felles stil i `web/templates/base.html`.

## Vanlige oppgaver

- **Endre tekst i en e-post:** rediger fila i `kurs/maler/epost/`. Første linje er `Emne: ...`.
- **Nytt felt i påmeldingsskjema:** `schema.sql` → `db.meld_paa`-kall i `web/app.py` (`kursside`) → `web/templates/kurs.html` → evt. `admin_kurs_deltakere.html`.
- **Ny påminnelse:** legg til i `daglig.py` etter samme mønster som `_innkallinger` / `_purring`, og skriv test i `tests/test_flyt.py`.
