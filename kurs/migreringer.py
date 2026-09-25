"""Versjonerte databasemigreringer.

Hver endring av datamodellen ETTER at systemet er i drift er en nummerert migrering her - aldri en endring av eksisterende
tabeller «paa direkten». Regler:

  * Migreringene kjoeres KUN av `python -m kurs.migrer` (og av db.init i den lokale sandkassen). Aldri av gunicorn.
  * Hver migrering kjoerer i sin egen transaksjon sammen med raden i schema_versjon: enten er hele migreringen kjoert og
    registrert, eller ingenting (baade SQLite og PostgreSQL har transaksjonell DDL).
  * Hver migrering er IDEMPOTENT (sjekker selv om kolonnen/raden finnes), slik at en ny database opprettet fra schema.sql
    trygt kan kjoere alle migreringene - da gjoer de ingenting annet enn aa registrere seg.
  * Migreringer er ADDITIVE: legger til tabeller/kolonner/indekser og fyller inn data. Kolonner slettes eller endres aldri
    uten avtale (CLAUDE.md, regel 7). Rollback-plan per migrering staar i MIGRATIONS.md.
  * schema.sql / schema_postgres.sql beskriver alltid SLUTTRESULTATET (en ny database er lik en migrert gammel).
  * Appen og den daglige jobben nekter aa kjoere mot en database med en annen versjon enn koden forventer
    (kontroller()) - baade en umigrert og en NYERE database gir en tydelig feil, aldri stille feil oppfoersel.
"""
import re

from . import db

KURSNR_START = 1000   # foerste kursnummer blir 1001


class VersjonsFeil(RuntimeError):
    """Databasens versjon passer ikke koden. Meldingen er trygg aa vise/logge (ingen tilkoblingsdetaljer)."""


# ---------------------------------------------------------------------------------------------------------------------
# Migreringene, i rekkefoelge. (nummer, navn, funksjon). Nummeret oekes med 1 for hver ny migrering. Endre ALDRI en
# migrering som kan vaere kjoert i drift - legg til en ny.
# ---------------------------------------------------------------------------------------------------------------------

def _m1_kursnummer(con) -> None:
    """Permanent, numerisk kursnummer: kolonnen kurs.kursnr, telleren 'kursnr' og nummer til alle eksisterende kurs
    (stigende etter id, saa eldste kurs faar lavest nummer). Kjoeres den mot en ny database gjoer den ingenting annet
    enn aa opprette telleren."""
    if not db.har_kolonne(con, "kurs", "kursnr"):
        con.execute("ALTER TABLE kurs ADD COLUMN kursnr INTEGER")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS kurs_kursnr_unik ON kurs (kursnr)")
    hoyeste = con.execute("SELECT COALESCE(MAX(kursnr), ?) FROM kurs", (KURSNR_START,)).fetchone()[0]
    con.execute("INSERT INTO teller (navn, verdi) VALUES ('kursnr', ?) ON CONFLICT (navn) DO NOTHING",
                (max(int(hoyeste), KURSNR_START),))
    for rad in con.execute("SELECT id FROM kurs WHERE kursnr IS NULL ORDER BY id").fetchall():
        con.execute("UPDATE kurs SET kursnr=? WHERE id=?", (db.neste_teller(con, "kursnr"), rad["id"]))


def _m2_roller(con) -> None:
    """Rollebasert admin-tilgang (fase 13): admin_bruker.rolle, entra_oid (Microsoft Entra ID) og epost. ALLE eksisterende
    brukere faar rollen 'system' - de hadde full tilgang fra foer, og ingen skal miste tilgang av en migrering."""
    if not db.har_kolonne(con, "admin_bruker", "rolle"):
        con.execute("ALTER TABLE admin_bruker ADD COLUMN rolle TEXT NOT NULL DEFAULT 'kursadmin' "
                    "CHECK (rolle IN ('system','kursadmin','lese'))")
        con.execute("UPDATE admin_bruker SET rolle='system'")
    if not db.har_kolonne(con, "admin_bruker", "entra_oid"):
        con.execute("ALTER TABLE admin_bruker ADD COLUMN entra_oid TEXT")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS admin_bruker_entra_oid_unik ON admin_bruker (entra_oid)")
    if not db.har_kolonne(con, "admin_bruker", "epost"):
        con.execute("ALTER TABLE admin_bruker ADD COLUMN epost TEXT")


def _opprett_tabell_fra_skjema(con, tabell: str) -> None:
    """Oppretter en NY tabell med definisjonen fra riktig skjemafil (schema.sql / schema_postgres.sql), slik at
    migreringen og en ny database alltid faar noeyaktig samme tabell. Gjoer ingenting hvis tabellen finnes."""
    if db.har_tabell(con, tabell):
        return
    skjema = (db._SCHEMA_POSTGRES if db.er_postgres(con) else db._SCHEMA).read_text(encoding="utf-8")
    treff = re.search(rf"^CREATE TABLE IF NOT EXISTS {re.escape(tabell)} \(.*?^\);", skjema, re.S | re.M)
    if not treff:
        raise VersjonsFeil(f"Tabellen {tabell} mangler i skjemafilen.")
    con.execute(treff.group(0).rstrip(";"))


def _m3_aarsplan(con) -> None:
    """Aarsplan / kurshjul: tabellen planlagt_aktivitet (planlagte aktiviteter og notater). Ren tilfoeyelse - ingen
    eksisterende tabell endres."""
    _opprett_tabell_fra_skjema(con, "planlagt_aktivitet")


MIGRERINGER = [
    (1, "kursnummer", _m1_kursnummer),
    (2, "roller", _m2_roller),
    (3, "aarsplan", _m3_aarsplan),
]
KODEVERSJON = MIGRERINGER[-1][0]


def gjeldende_versjon(con) -> int:
    """Databasens versjon: hoeyeste registrerte migrering, 0 for en database uten schema_versjon-tabell."""
    if not db.har_tabell(con, "schema_versjon"):
        return 0
    return int(con.execute("SELECT COALESCE(MAX(versjon), 0) FROM schema_versjon").fetchone()[0])


def kjor_manglende(con, skriv=None) -> list[int]:
    """Kjoerer alle migreringer med nummer over databasens versjon, hver i egen transaksjon. Returnerer numrene som
    ble kjoert. En database som er NYERE enn koden gir VersjonsFeil - da maa koden oppdateres, ikke databasen."""
    versjon = gjeldende_versjon(con)
    if versjon > KODEVERSJON:
        raise VersjonsFeil(f"Databasen har versjon {versjon}, men denne koden kjenner bare til versjon {KODEVERSJON}. "
                           "Oppdater koden (eller bruk riktig database).")
    kjort = []
    for nummer, navn, funksjon in MIGRERINGER:
        if nummer <= versjon:
            continue
        with db.transaksjon(con):
            funksjon(con)
            con.execute("INSERT INTO schema_versjon (versjon, navn, kjort) VALUES (?,?,?)", (nummer, navn, db.naa_utc()))
        kjort.append(nummer)
        if skriv:
            skriv(f"  migrering {nummer} ({navn}) kjoert")
    return kjort


def kontroller(con) -> None:
    """Nekter (VersjonsFeil) hvis databasen ikke har noeyaktig den versjonen koden forventer."""
    versjon = gjeldende_versjon(con)
    if versjon < KODEVERSJON:
        raise VersjonsFeil(f"Databasen har versjon {versjon}, koden krever {KODEVERSJON}. "
                           "Kjoer migreringen foerst:  python -m kurs.migrer")
    if versjon > KODEVERSJON:
        raise VersjonsFeil(f"Databasen har versjon {versjon}, men denne koden kjenner bare til versjon {KODEVERSJON}. "
                           "Oppdater koden (eller bruk riktig database).")
