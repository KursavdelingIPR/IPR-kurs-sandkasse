# Databasemigrering

Slik endres datamodellen trygt etter at systemet er i drift. Gjelder både SQLite (lokalt) og PostgreSQL (Azure).

## Kort versjon

```text
python -m kurs.migrer --status     # hva er databasens versjon, hva forventer koden?
python -m kurs.migrer              # kjør manglende migreringer (idempotent – trygt å kjøre flere ganger)
```

- Migrering er et **eget, eksplisitt steg**. Gunicorn (`startup.py`) migrerer aldri. Den daglige jobben migrerer aldri i drift.
- I drift nekter appen å kjøre mot en database med feil versjon: alle sider svarer **503 «Tjenesten er midlertidig
  utilgjengelig»** til `python -m kurs.migrer` er kjørt. Appen tar seg inn igjen uten omstart.
- Lokalt (MODUS=demo) holder `start.bat`/`kjor.py` databasen i takt automatisk.

## Hvordan det virker

| Del | Hva |
|---|---|
| `kurs/schema.sql`, `kurs/schema_postgres.sql` | Beskriver **sluttresultatet**. En ny database lages herfra (`CREATE TABLE IF NOT EXISTS`). De to filene speiler hverandre; `tests/test_skjema_speiling.py` feiler hvis de glir fra hverandre. |
| `kurs/migreringer.py` | Nummererte migreringer (`MIGRERINGER`). Hver er idempotent, additiv og kjører i **egen transaksjon** sammen med raden i `schema_versjon` – enten er hele migreringen kjørt og registrert, eller ingenting. |
| `schema_versjon`-tabellen | Én rad per kjørt migrering (versjon, navn, tidspunkt). Databasens versjon = høyeste rad. |
| `kurs/migrer.py` | Kommandoen. Kjører `db.init`: manglende tabeller, manglende kolonner i eldre databaser, manglende migreringer, første admin-bruker. |
| `migreringer.kontroller()` | Brukes av appen (drift) og daglig jobb: databasens versjon må være **nøyaktig** kodens versjon. Både eldre og nyere database gir tydelig feil. |

## Migreringer som finnes

| Nr | Navn | Hva | Rollback |
|---|---|---|---|
| 1 | `kursnummer` | Kolonnen `kurs.kursnr` (permanent numerisk kursnummer), telleren `teller('kursnr')`, nummer til alle eksisterende kurs i stigende id-rekkefølge (1001, 1002 …). | Ny kode er ikke avhengig av at raden i `schema_versjon` finnes for å vise sider, men **eldre kode** ignorerer kolonnen. Rollback = gjenopprett sikkerhetskopi tatt før migreringen. Kolonnen kan stå igjen ubrukt uten skade. |

## Prosedyre i drift (Azure, PostgreSQL)

1. **Sandbox først.** Kjør den nye koden og `python -m kurs.migrer` mot sandbox-databasen. Kjør testene mot PostgreSQL
   (`TEST_DATABASE_URL=… python -m pytest tests`, se `tests/conftest.py`). Prøv appen manuelt.
2. **Sikkerhetskopi av produksjonsdatabasen.** Azure Database for PostgreSQL tar automatiske backups (velg minst 7 dagers
   oppbevaring). Ta i tillegg en manuell dump rett før migrering:
   ```text
   pg_dump "$DATABASE_URL" --format=custom --file=ipr-for-migrering-<dato>.dump
   ```
   Oppbevar dumpen på et sted med tilgangsstyring (den inneholder personopplysninger). Slett den når den ikke trengs.
3. **Stopp trafikk / vedlikeholdsvindu** for migreringer som endrer eksisterende data. Additive migreringer (ny kolonne,
   ny tabell) kan kjøres mens appen går – den gamle koden ignorerer det nye.
4. **Deploy ny kode.** Appen svarer nå 503 til migreringen er kjørt (kort vindu).
5. **Kjør migreringen** fra App Service (SSH/Kudu-konsoll) eller fra deploy-løpet:
   ```text
   python -m kurs.migrer --status
   python -m kurs.migrer
   ```
6. **Kontroller**: `python -m kurs.migrer --status` gir avslutningskode 0. Åpne admin. Kjør `python -m kurs.daglig --tor`.
7. **Rollback** hvis noe er galt: deploy forrige kode-versjon **og** gjenopprett dumpen
   (`pg_restore --clean --if-exists --dbname="$DATABASE_URL" ipr-for-migrering-<dato>.dump`). Rull aldri tilbake bare
   koden hvis migreringen endret data den gamle koden misforstår.

## Slik lager du en ny migrering

1. Oppdater **begge** skjemafilene med sluttresultatet (samme kolonner, samme rekkefølge).
2. Legg til `(N, "navn", funksjon)` i `MIGRERINGER` i `kurs/migreringer.py`. Funksjonen må
   - sjekke selv om endringen allerede finnes (`db.har_kolonne`, `db.har_tabell`, `ON CONFLICT DO NOTHING`),
   - kun bruke SQL som virker i både SQLite og PostgreSQL (ingen `PRAGMA`, `datetime('now')`, `INSERT OR …`),
   - aldri slette/endre eksisterende kolonner (spør Jan først – CLAUDE.md regel 7).
3. Skriv tester i `tests/test_migreringer.py`: ny database, gammel database uten endringen (med data), kjørt to ganger.
4. Kjør hele testsuiten mot SQLite **og** PostgreSQL.
5. Beskriv migreringen og rollback-planen i tabellen over.

## SQLite lokalt

Samme kommando. `nullstill-demo.bat` lager alltid en ny database fra `schema.sql`. Vil du beholde lokale data ved
oppgradering: kjør `python -m kurs.migrer` (eller bare `start.bat`, som gjør det samme i demo).
