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

from . import config, db

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


def _m4_kursbevis_i_database(con) -> None:
    """Kursbevis lagres i databasen (tabellen dokument_innhold) i stedet for som filer under data/kursbevis/, som ikke
    overlever omstart/skalering i Azure og ikke er med i databasens sikkerhetskopi. Eksisterende kursbevisfiler flyttes
    inn (url blir 'db:'). Filene slettes IKKE (kan fjernes manuelt etter kontroll). Mangler en fil, staar raden uroert."""
    _opprett_tabell_fra_skjema(con, "dokument_innhold")
    rot = (config.ROT / "data").resolve()
    for rad in con.execute("SELECT id, url FROM dokument WHERE url LIKE 'lokal:data/kursbevis/%' ORDER BY id").fetchall():
        sti = (config.ROT / rad["url"][len("lokal:"):]).resolve()
        if rot not in sti.parents or not sti.is_file():
            continue
        con.execute("INSERT INTO dokument_innhold (dokument_id, mimetype, innhold) VALUES (?,?,?) "
                    "ON CONFLICT (dokument_id) DO NOTHING", (rad["id"], "text/html", sti.read_text(encoding="utf-8")))
        con.execute("UPDATE dokument SET url='db:' WHERE id=?", (rad["id"],))


def _m5_integrasjonstoken(con) -> None:
    """Tabellen integrasjon_token: varig lagring av tokens som roteres (Visma sitt refresh-token). Ren tilfoeyelse."""
    _opprett_tabell_fra_skjema(con, "integrasjon_token")


def _m6_avslatt(con) -> None:
    """Fase 17: paamelding.avslatt_ts (IPR har avslått påmeldingen). Ny, tom kolonne - eksisterende kolonner (ogsaa
    status og dens CHECK) er uroert: en avslått påmelding har status 'avmeldt' og behandles likt med den."""
    if not db.har_kolonne(con, "paamelding", "avslatt_ts"):
        con.execute("ALTER TABLE paamelding ADD COLUMN avslatt_ts TEXT")


def del_eksisterende_navn(navn: str) -> tuple[str, str]:
    """KUN for migrering 7: deler et fullt navn som allerede ligger i databasen. Siste ord blir etternavn, resten fornavn
    («Anne Marie Eksempelsen» -> «Anne Marie» + «Eksempelsen»), slik at doble fornavn blir riktige. Ett ord -> bare fornavn;
    etternavnet fylles inn i deltakervinduet. Nye paameldinger registrerer ALLTID fornavn og etternavn hver for seg - denne
    regelen brukes aldri paa dem."""
    ord_ = (navn or "").split()
    if len(ord_) < 2:
        return (ord_[0] if ord_ else ""), ""
    return " ".join(ord_[:-1]), ord_[-1]


def _m7_fornavn_etternavn(con) -> None:
    """Fornavn og etternavn som egne felt: deltaker.fornavn/etternavn og firmapaamelding.kontakt_fornavn/kontakt_etternavn.
    Kolonnene navn og kontakt_navn beholdes uendret og fylles fortsatt - naa med «fornavn etternavn» (db.fullt_navn).
    Eksisterende rader uten fornavn faar navnet delt (del_eksisterende_navn). Hendelsesloggen faar en PII-fri oppsummering
    (bare antall), saa navn med ett eller tre+ ord kan kontrolleres i deltakervinduet."""
    oppsummering = {}
    for tabell, navn, fornavn, etternavn, hva in (
            ("deltaker", "navn", "fornavn", "etternavn", "deltakere"),
            ("firmapaamelding", "kontakt_navn", "kontakt_fornavn", "kontakt_etternavn", "kontaktpersoner")):
        for kolonne in (fornavn, etternavn):
            if not db.har_kolonne(con, tabell, kolonne):
                con.execute(f"ALTER TABLE {tabell} ADD COLUMN {kolonne} TEXT NOT NULL DEFAULT ''")
        antall = {hva: 0, f"{hva}_ett_ord": 0, f"{hva}_tre_eller_flere_ord": 0}
        for rad in con.execute(f"SELECT id, {navn} AS navn FROM {tabell} WHERE {fornavn}='' ORDER BY id").fetchall():
            f, e = del_eksisterende_navn(rad["navn"])
            con.execute(f"UPDATE {tabell} SET {fornavn}=?, {etternavn}=? WHERE id=?", (f, e, rad["id"]))
            ord_ = len((rad["navn"] or "").split())
            antall[hva] += 1
            antall[f"{hva}_ett_ord"] += int(ord_ < 2)
            antall[f"{hva}_tre_eller_flere_ord"] += int(ord_ > 2)
        oppsummering.update(antall)
    if oppsummering["deltakere"] or oppsummering["kontaktpersoner"]:
        db.logg(con, "navn_delt_ved_migrering", oppsummering)


# Samme definisjoner som i skjemafilene, så ny og migrert database blir like
_M8_KOLONNER = (
    ("utgatt_ts", "utgatt_ts TEXT CHECK (utgatt_ts IS NULL OR (status='avmeldt' AND avslatt_ts IS NULL))"),
    ("forlatt_ts", "forlatt_ts TEXT CHECK (forlatt_ts IS NULL OR (status='avmeldt' AND avslatt_ts IS NULL AND "
                   "utgatt_ts IS NULL))"),
)


def _m8_utgatt_forlatt(con) -> None:
    """Statusene Utgått (påmeldingen ble aldri fullført) og Forlatt (deltakelsen avsluttet administrativt/ufrivillig):
    paamelding.utgatt_ts og forlatt_ts. Nye, tomme kolonner - eksisterende kolonner (også status og dens CHECK) er urørt.
    Som Avslått (migrering 6) er en utgått eller forlatt påmelding 'avmeldt' med en dato. CHECK-reglene gjør Avslått,
    Utgått og Forlatt gjensidig utelukkende og tillater dem bare på avmeldte påmeldinger (utgatt_ts må finnes før
    forlatt_ts, som viser til den)."""
    for kolonne, definisjon in _M8_KOLONNER:
        if not db.har_kolonne(con, "paamelding", kolonne):
            con.execute(f"ALTER TABLE paamelding ADD COLUMN {definisjon}")


MIGRERINGER = [
    (1, "kursnummer", _m1_kursnummer),
    (2, "roller", _m2_roller),
    (3, "aarsplan", _m3_aarsplan),
    (4, "kursbevis_i_database", _m4_kursbevis_i_database),
    (5, "integrasjonstoken", _m5_integrasjonstoken),
    (6, "avslatt", _m6_avslatt),
    (7, "fornavn_etternavn", _m7_fornavn_etternavn),
    (8, "utgatt_forlatt", _m8_utgatt_forlatt),
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
