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
from dataclasses import dataclass
from datetime import date, timedelta

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


@dataclass
class Kursdaggruppe:
    """Kursdager som blir én samling ved migrering 9."""
    ider: list[int]
    datoer: list[str]
    start_kl: str
    slutt_kl: str


def grupper_eksisterende_kursdager(con, kurs) -> list[Kursdaggruppe]:
    """Hvordan et gammelt kurs sine kursdager (uten samling) grupperes i samlinger - konservativt, så kurset betyr det
    samme som før:
      * Kan kurset ha delt faktura (betaling per_samling eller deltaker_velger), har en påmelding delt betaling, eller er
        en faktura knyttet til en kursdag: hver kursdag blir sin egen samling. Delfakturaen var per kursdag, og det
        skal den fortsatt være for disse kursene.
      * Ellers blir sammenhengende datoer med samme klokkeslett én samling."""
    dager = con.execute("SELECT * FROM kursdag WHERE kurs_id=? AND samling_id IS NULL ORDER BY dato",
                        (kurs["id"],)).fetchall()
    delt = kurs["betaling"] in ("per_samling", "deltaker_velger") or con.execute(
        """SELECT 1 FROM paamelding WHERE kurs_id=? AND betaling='per_samling'
           UNION ALL SELECT 1 FROM faktura f JOIN kursdag kd ON kd.id=f.kursdag_id WHERE kd.kurs_id=?
           UNION ALL SELECT 1 FROM faktura_forsok fo JOIN kursdag kd ON kd.id=fo.kursdag_id WHERE kd.kurs_id=?""",
        (kurs["id"], kurs["id"], kurs["id"])).fetchone() is not None
    grupper: list[Kursdaggruppe] = []
    for d in dager:
        start, slutt = d["start_kl"] or kurs["start_kl"], d["slutt_kl"] or kurs["slutt_kl"]
        forrige = grupper[-1] if grupper else None
        if (not delt and forrige and (start, slutt) == (forrige.start_kl, forrige.slutt_kl)
                and date.fromisoformat(d["dato"]) == date.fromisoformat(forrige.datoer[-1]) + timedelta(days=1)):
            forrige.ider.append(d["id"])
            forrige.datoer.append(d["dato"])
        else:
            grupper.append(Kursdaggruppe([d["id"]], [d["dato"]], start, slutt))
    return grupper


def _m9_samlinger(con) -> None:
    """Samlinger: ny tabell samling, kursdag.samling_id/merknad/timer og kurs.paameldingsfrist_manuell. Ingen kursdag
    endres eller slettes - de beholder id, dato, klokkeslett og innsjekkode, og får bare en samling
    (grupper_eksisterende_kursdager). Samlingen får kursets timer per dag. Alle eksisterende kurs får
    paameldingsfrist_manuell=1, så en senere datoendring aldri overskriver fristen (eller mangelen på frist) de har i dag."""
    pg = db.er_postgres(con)
    _opprett_tabell_fra_skjema(con, "samling")
    for kolonne, definisjon in (("samling_id", f"{'BIGINT' if pg else 'INTEGER'} REFERENCES samling(id)"),
                                ("merknad", "TEXT"), ("timer", "DOUBLE PRECISION" if pg else "REAL")):
        if not db.har_kolonne(con, "kursdag", kolonne):
            con.execute(f"ALTER TABLE kursdag ADD COLUMN {kolonne} {definisjon}")
    if not db.har_kolonne(con, "kurs", "paameldingsfrist_manuell"):
        con.execute(f"ALTER TABLE kurs ADD COLUMN paameldingsfrist_manuell {'BIGINT' if pg else 'INTEGER'} NOT NULL DEFAULT 0")
        con.execute("UPDATE kurs SET paameldingsfrist_manuell=1")
    for kurs in con.execute("SELECT * FROM kurs ORDER BY id").fetchall():
        for gruppe in grupper_eksisterende_kursdager(con, kurs):
            sid = db.sett_inn(con, "INSERT INTO samling (kurs_id, navn, start_kl, slutt_kl, timer_pr_dag) VALUES (?,?,?,?,?)",
                              (kurs["id"], None, gruppe.start_kl, gruppe.slutt_kl, kurs["timer_pr_dag"]))
            con.execute(f"UPDATE kursdag SET samling_id=? WHERE id IN ({','.join('?' * len(gruppe.ider))})",
                        (sid, *gruppe.ider))


def _m8_utgatt_forlatt(con) -> None:
    """Statusene Utgått (påmeldingen ble aldri fullført) og Forlatt (deltakelsen avsluttet administrativt/ufrivillig):
    paamelding.utgatt_ts og forlatt_ts. Nye, tomme kolonner - eksisterende kolonner (også status og dens CHECK) er urørt.
    Som Avslått (migrering 6) er en utgått eller forlatt påmelding 'avmeldt' med en dato. CHECK-reglene gjør Avslått,
    Utgått og Forlatt gjensidig utelukkende og tillater dem bare på avmeldte påmeldinger (utgatt_ts må finnes før
    forlatt_ts, som viser til den)."""
    for kolonne, definisjon in _M8_KOLONNER:
        if not db.har_kolonne(con, "paamelding", kolonne):
            con.execute(f"ALTER TABLE paamelding ADD COLUMN {definisjon}")


def _m10_eposthistorikk(con) -> None:
    """E-posthistorikk: tabellene sendt_epost, epost_fil og sendt_epost_vedlegg (en uforanderlig kopi av hver e-post som
    sendes fra nå av). Bare nye tabeller - ingen eksisterende tabell eller kolonne endres. Gamle e-poster har ingen kopi,
    og det lages heller ingen i ettertid (de vises som «Innholdet ble ikke lagret for denne eldre e-posten»)."""
    for tabell in ("sendt_epost", "epost_fil", "sendt_epost_vedlegg"):
        _opprett_tabell_fra_skjema(con, tabell)
    con.execute("CREATE INDEX IF NOT EXISTS sendt_epost_paamelding ON sendt_epost (paamelding_id, opprettet)")


# Samme definisjoner som i skjemafilene (kolonnene står sist i tabellene, der ALTER TABLE legger dem)
_M11_KOLONNER = (
    ("admin_bruker", "hovedsignatur_id", "hovedsignatur_id INTEGER REFERENCES signatur(id)"),
    ("kurs", "signatur_id", "signatur_id INTEGER REFERENCES signatur(id)"),
    ("kurs", "signatur_html", "signatur_html TEXT"),
    ("admin_utsending", "signatur_html", "signatur_html TEXT"),
)


def _m11_signaturer(con) -> None:
    """Signaturbiblioteket: tabellene signatur og signatur_bilde, og nye, tomme kolonner for brukerens hovedsignatur
    (admin_bruker.hovedsignatur_id), kursets signatur (kurs.signatur_id / signatur_html) og signaturen i en manuell
    utsendelse (admin_utsending.signatur_html). Ingen eksisterende kolonne endres. Lager standardsignaturen
    «Kursadministrasjonen» med nøyaktig den hilsenen e-postene har i dag, så ingenting endrer seg før dere lager egne."""
    from . import signaturer
    for tabell in ("signatur", "signatur_bilde"):
        _opprett_tabell_fra_skjema(con, tabell)
    for tabell, kolonne, definisjon in _M11_KOLONNER:
        if not db.har_kolonne(con, tabell, kolonne):
            if db.er_postgres(con):
                definisjon = definisjon.replace("INTEGER", "BIGINT")
            con.execute(f"ALTER TABLE {tabell} ADD COLUMN {definisjon}")
    if not con.execute("SELECT 1 FROM signatur WHERE standard=1").fetchone():
        con.execute("INSERT INTO signatur (navn, innhold, standard, opprettet_av) VALUES (?,?,1,'system')",
                    (signaturer.STANDARD_NAVN, signaturer.STANDARD_INNHOLD))


def _m12_kalenderperioder(con) -> None:
    """Årsplanen: tabellen kalenderperiode (skoleferier per område, sperrede perioder og advarsler). Bare en ny tabell -
    ingen eksisterende tabell eller kolonne endres. Ingen skoleferier legges inn: datoene legges inn av admin."""
    _opprett_tabell_fra_skjema(con, "kalenderperiode")
def _m13_paameldingsskjema(con) -> None:
    """Skjemabyggeren: tabellene kurs_ekstrafelt (kursets egne felt i påmeldingsskjemaet) og paamelding_svar (svarene).
    Bare nye tabeller - ingen eksisterende tabell eller kolonne endres. Standardfeltenes oppsett ligger fortsatt i
    kurs_skjemafelt (samme tabell og kolonner som før)."""
    _opprett_tabell_fra_skjema(con, "kurs_ekstrafelt")
    _opprett_tabell_fra_skjema(con, "paamelding_svar")


_M14_KOLONNER = ("adresse", "postnr", "poststed")


def _m14_deltaker_adresse(con) -> None:
    """Deltakerens private adresse: nye, tomme kolonner deltaker.adresse, postnr og poststed (TEXT, NULL tillatt).
    Adressen er påkrevd i alle veier inn fra nå av (skjemaet, bedriftspåmelding, manuell registrering, CSV-import, webhook
    og redigering), men databasen krever den ikke: personer som er registrert før migreringen har ingen adresse og får
    heller ingen automatisk (den fylles inn ved neste påmelding, eller av administrator i deltakervinduet). Ingen
    eksisterende kolonne eller rad endres, og de private fakturaadressene på påmeldingene røres ikke."""
    for kolonne in _M14_KOLONNER:
        if not db.har_kolonne(con, "deltaker", kolonne):
            con.execute(f"ALTER TABLE deltaker ADD COLUMN {kolonne} TEXT")


def _m15_kursside(con) -> None:
    """Kursside: tabellene kursside (utkast/publisert JSON, versjon, innstillinger), kursside_versjon (tidligere versjoner og
    sikkerhetskopier), kursside_fil og kursside_fil_innhold (bilder og dokumenter). Bare nye tabeller - ingen eksisterende
    tabell eller kolonne endres. Ingen kursside opprettes for eksisterende kurs: raden lages første gang en admin lagrer siden."""
    for tabell in ("kursside", "kursside_versjon", "kursside_fil", "kursside_fil_innhold"):
        _opprett_tabell_fra_skjema(con, tabell)
    con.execute("CREATE INDEX IF NOT EXISTS kursside_versjon_kurs ON kursside_versjon (kurs_id, id)")
    con.execute("CREATE INDEX IF NOT EXISTS kursside_fil_kurs ON kursside_fil (kurs_id)")


# Samme definisjon som i skjemafilene, så ny og migrert database blir like
_M16_KOLONNE = "ekstradeltaker_ts TEXT CHECK (ekstradeltaker_ts IS NULL OR status='bekreftet')"


def _m16_ekstradeltaker(con) -> None:
    """Statusen for ekstradeltakere: en ny, tom kolonne paamelding.ekstradeltaker_ts (som avslatt_ts, utgatt_ts og forlatt_ts).
    Databaseverdien status='bekreftet' er fortsatt «har plass»-verdien for alle med plass, også ekstradeltakerne, så kapasitet,
    e-post, faktura, påminnelser, kursbevis og innsjekk virker uendret. NULL = den vanlige statusen, satt = ekstradeltaker.
    CHECK-regelen tillater tidsstempelet bare på en påmelding som har plass. Eksisterende kolonner - også status og dens
    CHECK - er urørt, og ingen eksisterende påmelding endres: alle som har plass i dag, har den vanlige statusen."""
    if not db.har_kolonne(con, "paamelding", "ekstradeltaker_ts"):
        con.execute(f"ALTER TABLE paamelding ADD COLUMN {_M16_KOLONNE}")


def _m17_kursside_apningstid(con) -> None:
    """Kursside: åpningstiden etter kurset kan settes per kurs. Én ny, tom kolonne kursside.apen_dager (dager etter siste kursdag;
    NULL = standard 180, 0 = ingen tidsbegrensning, 1-3650 = så mange dager). Ingen eksisterende kolonne eller rad endres: alle kurs
    beholder dagens åpningstid, og «Åpen til»-datoen (kursside.stenges) virker som før og går foran."""
    if not db.har_kolonne(con, "kursside", "apen_dager"):
        con.execute("ALTER TABLE kursside ADD COLUMN apen_dager INTEGER CHECK (apen_dager IS NULL OR apen_dager BETWEEN 0 AND 3650)")


def _m18_ekstradeltaker_samling(con) -> None:
    """Samlingsutvalg for ekstradeltakere: den nye tabellen ekstradeltaker_samling (paamelding_id, samling_id). Ingen rader = deltar på
    hele kurset, rader = bare de samlingene. Ingen eksisterende tabell eller kolonne endres, og alle som er ekstradeltakere i dag har
    «hele kurset» (ingen rader), som før migreringen. NB: reglene som beskrives i _m16_ekstradeltaker (en ekstradeltaker teller mot
    kapasiteten og får bekreftelse og faktura som en påmeldt) er endret av denne migreringen sammen med koden: se MIGRATIONS.md, rad 18.

    Én liten rad-endring følger med reglene: en ekstradeltaker fra migrering 16 som ikke er behandlet ennå (sveiper_kjort=0) får ikke
    lenger automatisk bekreftelse eller faktura (prisen er ikke kursets standardpris). Den holdes tilbake (sveiper_utsatt=1), så
    administrator ser «Behandling holdt tilbake» og kan sende manuelt. Ferdig behandlede påmeldinger røres ikke, og kjøres
    migreringen på nytt, er det ingenting mer å endre.

    En ekstradeltaker teller heller ikke lenger mot kapasiteten. Har et kurs både ekstradeltakere og folk på venteliste, kan det derfor
    være ledige plasser mens noen venter, og en ny påmelding ville tatt plassen foran dem. Migreringen flytter ingen opp (det ville
    sendt bekreftelser ved en oppgradering), men skriver kurs-id-ene til hendelsesloggen (migrering_18_venteliste_maa_sjekkes), og
    MIGRATIONS.md (rad 18) sier hva administrator gjør: flytt opp fra ventelisten i deltakerlisten."""
    _opprett_tabell_fra_skjema(con, "ekstradeltaker_samling")
    con.execute("UPDATE paamelding SET sveiper_utsatt=1 "
                "WHERE ekstradeltaker_ts IS NOT NULL AND sveiper_kjort=0 AND sveiper_utsatt=0")
    berorte = [r[0] for r in con.execute(
        """SELECT k.id FROM kurs k
           WHERE k.kapasitet IS NOT NULL AND k.status NOT IN ('avlyst', 'avsluttet')
             AND EXISTS (SELECT 1 FROM paamelding p WHERE p.kurs_id=k.id AND p.status='bekreftet' AND p.ekstradeltaker_ts IS NOT NULL)
             AND EXISTS (SELECT 1 FROM paamelding p WHERE p.kurs_id=k.id AND p.status='venteliste')
             AND (SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=k.id AND p.status='bekreftet'
                                                      AND p.ekstradeltaker_ts IS NULL) < k.kapasitet
           ORDER BY k.id""")]
    if berorte:
        db.logg(con, "migrering_18_venteliste_maa_sjekkes", {"kurs_ider": berorte, "antall": len(berorte)})


def _m19_min_side_lenke(con) -> None:
    """«Min side» for ett kurs: tabellen min_side_lenke (stengte eller fornyede personlige lenker; ingen rad = åpen, versjon 0). Bare en ny
    tabell - ingen eksisterende tabell eller kolonne endres, og ingen rader legges inn. Selve lenkene lagres ikke (de er signerte adresser)."""
    _opprett_tabell_fra_skjema(con, "min_side_lenke")


def _m20_planlagte_kurs(con) -> None:
    """Planlagte kurs i Kalender og Årsplan: tabellene planlagt_kurs og planlagt_samling (kursene fra kursplanleggingen som
    ikke er opprettet i systemet). Bare nye tabeller - ingen eksisterende tabell eller kolonne endres, og ingen rader legges
    inn (kursplanen leses inn av administrator)."""
    for tabell in ("planlagt_kurs", "planlagt_samling"):
        _opprett_tabell_fra_skjema(con, tabell)
    con.execute("CREATE INDEX IF NOT EXISTS planlagt_samling_kurs ON planlagt_samling (planlagt_kurs_id)")
    con.execute("CREATE INDEX IF NOT EXISTS planlagt_samling_dato ON planlagt_samling (fra_dato)")


def _m21_sjekklister(con) -> None:
    """Sjekklister for planlagte kurs: tabellene sjekkliste_mal, sjekkliste_malpunkt, planlagt_kurs_sjekkliste og
    sjekkliste_punkt. Bare nye tabeller - ingen eksisterende tabell eller kolonne endres, og ingen rader legges inn (malene
    legges inn av administrator)."""
    for tabell in ("sjekkliste_mal", "sjekkliste_malpunkt", "planlagt_kurs_sjekkliste", "sjekkliste_punkt"):
        _opprett_tabell_fra_skjema(con, tabell)
    con.execute("CREATE INDEX IF NOT EXISTS sjekkliste_malpunkt_mal ON sjekkliste_malpunkt (mal_id, rekkefolge)")
    con.execute("CREATE INDEX IF NOT EXISTS sjekkliste_punkt_samling ON sjekkliste_punkt (samling_id, rekkefolge)")
    con.execute("CREATE INDEX IF NOT EXISTS sjekkliste_punkt_frist ON sjekkliste_punkt (status, frist)")


def _m22_kursholdere(con) -> None:
    """Kursholderoversikten for planlagte kurs: tabellene kursholder og planlagt_rolle (rollen per samling og veiledningsdag,
    med Psybase-status). Bare nye tabeller - ingen eksisterende tabell eller kolonne endres, og ingen rader legges inn
    (kursholderne legges inn av administrator)."""
    for tabell in ("kursholder", "planlagt_rolle"):
        _opprett_tabell_fra_skjema(con, tabell)
    con.execute("CREATE INDEX IF NOT EXISTS planlagt_rolle_kursholder ON planlagt_rolle (kursholder_id)")


def _m23_sjekkliste_for_kurs(con) -> None:
    """Sjekklister (og kursholderroller) også for kurs som er opprettet i systemet: et kurs får et skjult planlagt kurs som
    holder dem. To nye, tomme kolonner: planlagt_kurs.kurs_id og planlagt_samling.kurs_samling_id (begge NULL for de planlagte
    kursene fra kursplanen), og indeksen planlagt_kurs_kurs. Ingen eksisterende kolonne eller rad endres."""
    heltall = "BIGINT" if db.er_postgres(con) else "INTEGER"
    if not db.har_kolonne(con, "planlagt_kurs", "kurs_id"):
        con.execute(f"ALTER TABLE planlagt_kurs ADD COLUMN kurs_id {heltall} REFERENCES kurs(id) ON DELETE SET NULL")
    if not db.har_kolonne(con, "planlagt_samling", "kurs_samling_id"):
        con.execute(f"ALTER TABLE planlagt_samling ADD COLUMN kurs_samling_id {heltall} "
                    "REFERENCES samling(id) ON DELETE SET NULL")
    con.execute("CREATE INDEX IF NOT EXISTS planlagt_kurs_kurs ON planlagt_kurs (kurs_id)")


def _m24_booking_og_notat(con) -> None:
    """Booking per samling (lokale, hotell, grupperom og lunsj) i den nye tabellen samling_booking, og to nye kolonner på
    sjekklistepunktene: notat (hva som er gjort) og slettet (et punkt fra malen som er tatt bort fra denne sjekklisten;
    0 for alle som finnes). Ingen eksisterende kolonne eller rad endres (Camilla 03.10.2026)."""
    _opprett_tabell_fra_skjema(con, "samling_booking")
    heltall = "BIGINT" if db.er_postgres(con) else "INTEGER"
    if not db.har_kolonne(con, "sjekkliste_punkt", "notat"):
        con.execute("ALTER TABLE sjekkliste_punkt ADD COLUMN notat TEXT")
    if not db.har_kolonne(con, "sjekkliste_punkt", "slettet"):
        con.execute(f"ALTER TABLE sjekkliste_punkt ADD COLUMN slettet {heltall} NOT NULL DEFAULT 0 CHECK (slettet IN (0,1))")


MIGRERINGER = [
    (1, "kursnummer", _m1_kursnummer),
    (2, "roller", _m2_roller),
    (3, "aarsplan", _m3_aarsplan),
    (4, "kursbevis_i_database", _m4_kursbevis_i_database),
    (5, "integrasjonstoken", _m5_integrasjonstoken),
    (6, "avslatt", _m6_avslatt),
    (7, "fornavn_etternavn", _m7_fornavn_etternavn),
    (8, "utgatt_forlatt", _m8_utgatt_forlatt),
    (9, "samlinger", _m9_samlinger),
    (10, "eposthistorikk", _m10_eposthistorikk),
    (11, "signaturer", _m11_signaturer),
    (12, "kalenderperioder", _m12_kalenderperioder),
    (13, "paameldingsskjema", _m13_paameldingsskjema),
    (14, "deltaker_adresse", _m14_deltaker_adresse),
    (15, "kursside", _m15_kursside),
    (16, "ekstradeltaker", _m16_ekstradeltaker),
    (17, "kursside_apningstid", _m17_kursside_apningstid),
    (18, "ekstradeltaker_samling", _m18_ekstradeltaker_samling),
    (19, "min_side_lenke", _m19_min_side_lenke),
    (20, "planlagte_kurs", _m20_planlagte_kurs),
    (21, "sjekklister", _m21_sjekklister),
    (22, "kursholdere", _m22_kursholdere),
    (23, "sjekkliste_for_kurs", _m23_sjekkliste_for_kurs),
    (24, "booking_og_notat", _m24_booking_og_notat),
]
KODEVERSJON = MIGRERINGER[-1][0]


def _skjemaets_kolonner(skjema: str) -> dict[str, list[str]]:
    """{tabell: [kolonner]} fra skjemafilen (én kolonne per linje, slik filene er skrevet)."""
    ut = {}
    for m in re.finditer(r"^CREATE TABLE IF NOT EXISTS (\w+) \((.*?)^\);", skjema, re.S | re.M):
        linjer = (re.sub(r"--.*", "", linje).strip() for linje in m.group(2).splitlines())
        ut[m.group(1)] = [linje.split()[0] for linje in linjer
                          if linje and not re.match(r"(PRIMARY KEY|UNIQUE|CHECK|FOREIGN KEY|CONSTRAINT)\b", linje)]
    return ut


def _kontroller_skjema(con, *, kolonner: bool) -> None:
    """Alle tabeller (og med kolonner=True: alle kolonner) i skjemafilen skal finnes. Mangler noe, er databasen migrert av
    en annen utgave av systemet - f.eks. en parallell gren som brukte samme migreringsnummer til noe annet - og da stopper
    vi med en tydelig feil i stedet for å kjøre videre med en database som mangler deler. Kolonnene kontrolleres når
    migreringene kjøres; tabellene også ved hver oppstart (kontroller)."""
    skjema = (db._SCHEMA_POSTGRES if db.er_postgres(con) else db._SCHEMA).read_text(encoding="utf-8")
    mangler = []
    for tabell, kol in _skjemaets_kolonner(skjema).items():
        if not db.har_tabell(con, tabell):
            mangler.append(tabell)
        elif kolonner:
            mangler += [f"{tabell}.{k}" for k in kol if not db.har_kolonne(con, tabell, k)]
    if mangler:
        raise VersjonsFeil(f"Databasen mangler {', '.join(mangler)}. Den er trolig migrert av en annen utgave av "
                           "systemet med samme migreringsnummer. Bruk riktig kode, eller en egen database.")


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
    _kontroller_skjema(con, kolonner=True)
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
    _kontroller_skjema(con, kolonner=False)
