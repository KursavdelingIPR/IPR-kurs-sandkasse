# STATUS – IPR Påmeldingssystem

Sist oppdatert: 24. september 2026

## 1. Prosjekt og teknologi

**Prosjekt:** IPR påmeldingssystem  
**Teknologi:** Python / Flask  
**Lokal utvikling:** Windows + SQLite  
**Planlagt drift:** Azure App Service Linux + Azure Database for PostgreSQL  
**GitHub-repo:** `KursavdelingIPR/IPR-kurs-sandkasse`

Målet er å ha et tydelig skille mellom:
- lokal utvikling
- Azure sandbox
- Azure produksjon

---

## 2. Azure-oppsett

Azure-abonnementet er opprettet og aktivt.

Det er opprettet to separate Resource Groups:

- `pameldingssystem-sandbox-rg`
- `pameldingssystem-prod-rg`

Begge skal ligge i **Norway East**.

Tags:

- Sandbox: `Environment = Sandbox`
- Production: `Environment = Production`

Planen er å bygge og teste **sandbox først**. Produksjonsmiljøet skal ikke fylles med tjenester før sandbox fungerer.

**Det er ennå ikke opprettet App Service eller PostgreSQL-server i Azure.**

---

## 3. Flask-appen er klargjort for Azure App Service

Prosjektet er bekreftet som en Flask/Python-applikasjon.

Det er opprettet en fil i prosjektroten:

`startup.py`

med:

```python
from kurs.web.app import app
```

`gunicorn` er lagt til i `requirements.txt` for Linux.

Planlagt Azure Startup Command:

```text
gunicorn --bind=0.0.0.0 --timeout 600 startup:app
```

Gunicorn skal **bare starte appen**. Databasemigrering skal ikke kjøres automatisk ved hver oppstart.

---

## 4. Databasestrategi

SQLite beholdes lokalt på Windows.

I Azure skal systemet bruke PostgreSQL.

Prinsippet er:

```text
DATABASE_URL mangler
        ↓
      SQLite
   lokal Windows

DATABASE_URL satt
        ↓
    PostgreSQL
       Azure
```

Dette gjør at dagens lokale utviklingsoppsett kan beholdes samtidig som Azure bruker PostgreSQL.

---

## 5. PostgreSQL fase 1 – ferdig

Fase 1 gjorde SQL-koden mer databaseuavhengig uten å koble til PostgreSQL ennå.

Blant annet ble:

- `sqlite3.Error` / `sqlite3.IntegrityError` erstattet utenfor `db.py` med felles databaseunntak
- `lastrowid` erstattet med løsning basert på `RETURNING id`
- `INSERT OR IGNORE` gjort om til `ON CONFLICT DO NOTHING`
- `INSERT OR REPLACE` gjort om til PostgreSQL-kompatibel upsert-form
- `datetime('now')` flyttet ut av SQLite-spesifikk SQL
- `date(...)` erstattet med mer portabel SQL
- SQLite-spesifikk databasehåndtering samlet i `kurs/db.py`

Etter fase 1 var hele testsuiten grønn:

**1923 bestått, 1 xfailed, 0 feilet.**

---

## 6. PostgreSQL fase 2 – påbegynt og lagret som egen WIP-branch

Fase 2 inneholder den faktiske PostgreSQL-støtten.

Det er blant annet lagt til:

- `DATABASE_URL` i `kurs/config.py`
- PostgreSQL-adapter i `kurs/db.py`
- oversetting av SQLite-placeholders `?` til PostgreSQL `%s`
- PostgreSQL-kompatibel radtype som støtter både `row["navn"]` og `row[0]`
- felles databaseunntak for SQLite og PostgreSQL
- støtte for `RETURNING id`
- `psycopg[binary]` i `requirements.txt`
- `kurs/schema_postgres.sql`
- `kurs/migrer.py`
- backend-spesifikk løsning for tekstaggregat (`GROUP_CONCAT` / `string_agg`)
- PostgreSQL-kompatible `GROUP BY` / `HAVING`-spørringer
- tester for PostgreSQL-adapter
- tester som kontrollerer at SQLite- og PostgreSQL-skjemaene speiler hverandre

PostgreSQL-skjemaet bruker blant annet `CITEXT` for case-insensitive felter som:
- deltakerens e-post
- admin-brukernavn
- mottaker i utsendingslogg

Migrering skal kjøres separat:

```text
python -m kurs.migrer
```

Dette skal **aldri kjøres fra Gunicorn**.

---

## 7. Viktig begrensning i fase 2

Fase 2 er ennå **ikke testet mot en ekte PostgreSQL-server**.

På PC-en fantes ikke:
- Docker
- lokal PostgreSQL
- `psql`

Derfor ble ingenting installert.

Adapteren er testet med falsk PostgreSQL-tilkobling, og hele den lokale SQLite-suiten er kjørt.

Siste komplette testresultat på `postgresql-fase2-wip`:

**2062 bestått, 1 xfailed, 0 feilet.**

---

## 8. Git/GitHub-status

Alt arbeid er sikret på GitHub.

### Stabil branch

`videreutvikling-ipr`

står på:

```text
21a96c4 PostgreSQL-klargjøring fase 1: portabel SQL og felles databaseunntak
```

Denne branchen er i synk med GitHub.

### PostgreSQL fase 2

Ligger separat på:

`postgresql-fase2-wip`

med commit:

```text
4316931 WIP PostgreSQL fase 2: adapter, schema og migrering
```

Denne branchen er også i synk med GitHub.

### Viktige commits

```text
e7d7382 Fase 12C5: introduksjonstekst og knappetekst på påmeldingssiden
cf62403 Azure App Service: startup.py og gunicorn
21a96c4 PostgreSQL-klargjøring fase 1: portabel SQL og felles databaseunntak
4316931 WIP PostgreSQL fase 2: adapter, schema og migrering
```

Fase 2 er med vilje **ikke** committet på `videreutvikling-ipr`.

---

## 9. Ny PC – slik fortsetter vi

Klon repoet:

```text
git clone https://github.com/KursavdelingIPR/IPR-kurs-sandkasse.git
```

For stabil utvikling:

```text
git switch videreutvikling-ipr
```

For å fortsette PostgreSQL/Azure-arbeidet:

```text
git switch postgresql-fase2-wip
```

Kjør deretter:

```text
oppsett.bat
nullstill-demo.bat
```

Sett Git-identitet lokalt:

```text
git config --local user.name "KursavdelingIPR"
git config --local user.email "329932272+KursavdelingIPR@users.noreply.github.com"
```

`data/` og `utboks/` ligger ikke på GitHub, men kan genereres på nytt.

Det finnes ingen `.env` i prosjektet som må flyttes manuelt.

---

## 10. Neste tekniske steg

Neste naturlige steg er:

1. Åpne branchen `postgresql-fase2-wip` på den nye PC-en.
2. Opprette en ekte PostgreSQL sandbox-database.
3. Mest naturlig nå: opprette Azure Database for PostgreSQL i:
   `pameldingssystem-sandbox-rg`
4. Aktivere `citext`.
5. Sette sandbox `DATABASE_URL`.
6. Kjøre:
   `python -m kurs.migrer`
7. Kjøre reelle tester mot PostgreSQL.
8. Kontrollere spesielt:
   - e-post-deduplisering
   - faktura-claims
   - firmapåmelding
   - case-insensitive e-postadresser
   - samtidige operasjoner
9. Når dette fungerer: opprette Azure App Service i sandbox.
10. Deploye Flask-appen til sandbox.
11. Først når sandbox fungerer stabilt: bygge tilsvarende produksjonsmiljø i `pameldingssystem-prod-rg`.

---

## 11. Drift og hemmeligheter

Planlagte Azure App Settings / miljøvariabler inkluderer blant annet:

- `MODUS`
- `DATABASE_URL`
- `HEMMELIG_NOKKEL`
- `BASE_URL`
- `ADMIN_BRUKERNAVN`
- `ADMIN_PASSORD`
- `WEBHOOK_HEMMELIG`
- M365 / SharePoint-innstillinger
- Zoom-innstillinger
- Visma-innstillinger
- `ANTHROPIC_API_KEY` hvis KI-funksjonen brukes
- e-postrelaterte innstillinger

Hemmeligheter bør ikke legges i kode eller filer i repoet.

---

## 12. Ting som fortsatt ligger i backlog

UI:
- «Nytt kurs +»-knapp på Aktiviteter
- venstremeny
- flytte statusboksene
- søkbart kursnummer
- deltakerliste/PDF med NIEFT-logo
- hovereffekter

Sikkerhet og drift:
- CSRF i hele admin
- håndtering av samtidig redigering («den som lagrer sist vinner»)
- validere `type` / `fakturering` / `betaling` før SharePoint
- rydde foreldreløse SharePoint-mapper

---

## Kort status

**Stabil lokal versjon:** `videreutvikling-ipr`  
**Azure/PostgreSQL arbeid:** `postgresql-fase2-wip`  
**Alt er pushet til GitHub.**  
**SQLite fungerer lokalt.**  
**PostgreSQL-støtte er implementert som WIP, men må testes mot ekte PostgreSQL.**  
**Azure Resource Groups er klare, men App Service og PostgreSQL er ikke opprettet ennå.**
