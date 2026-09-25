"""Fase 10: import av deltakere til ett kurs fra CSV.

Bygger paa den eksisterende paameldingslogikken fra fase 9 (db.meld_paa()) - det finnes ingen
parallell paameldings-, e-post- eller faktureringslogikk her. Samme funksjon
(_kategoriser_og_meld_paa) brukes baade til forhaandsvisning og til selve importen, slik at de
aldri kan gaa ut av synk med hverandre:

  - forhaandsvis() kjorer den EKTE meld_paa()-logikken mot databasen, men innenfor en SAVEPOINT
    som ALLTID rulles tilbake (se test_forhaandsvisning_lar_ingen_spor_i_databasen) - admin ser
    et reelt resultat uten at noe faktisk lagres.
  - Resultatet lagres server-side (import_forhaandsvisning-tabellen) bak en tilfeldig, ugjettelig
    token - IKKE i en signert cookie/skjult felt. Radene kan inneholde allergi/tilrettelegging/
    fakturainformasjon, og itsdangerous-signering (som ble vurdert forst) er IKKE kryptering - en
    signert, men usynlig-for-serveren nyttelast ville uansett rundtripped sensitive data gjennom
    nettleseren TO ganger, og matte i tillegg hatt en egen, hoy request-storrelsesgrense (se
    forkastet tilnaerming nederst i git-historikken). Server-side previewing unngaar begge deler.
  - importer() slaar opp tokenet (hent_forhaandsvisning), kjorer SAMME funksjon paa nytt mot
    FERSK databasetilstand (kapasitet/kursstatus/andre paameldinger kan ha endret seg), og
    sammenligner resultatet mot det som ble forhaandsvist. Er noe vesentlig endret, rulles hele
    importen tilbake - admin maa forhaandsvise paa nytt. Er alt uendret, forblir de allerede
    utforte meld_paa()-kallene i transaksjonen, og den committes.
  - Forhaandsvisningsraden slettes etter forsoket (vellykket ELLER ikke) - den skal aldri kunne
    gjenbrukes/repeteres, og skal aldri bli staaende som permanent historikk.
"""
from __future__ import annotations

import csv
import io
import json
import secrets
from datetime import datetime, timedelta

from . import db

MAKS_FILSTORRELSE = 2 * 1024 * 1024  # 2 MB
MAKS_RADER = 300
MAKS_KOLONNER = 30
MAKS_CELLELENGDE = 500

FORHAANDSVISNING_LEVETID_MIN = 30

# En rad kan ha FLERE uavhengige egenskaper samtidig (f.eks. "personen finnes fra for" + "blir
# satt paa venteliste"), saa resultatet er IKKE en enkelt gjensidig utelukkende kategori, men en
# kombinasjon av felt:
#   handling            - hva som faktisk skjer med raden
#   resultatstatus       - bekreftet/venteliste (kun relevant naar handling er NY/REAKTIVER)
#   person_finnes_fra_for - informativt: fantes deltakeren (uavhengig av DETTE kurset) allerede?
# sammenlign_resultater() bruker (handling, resultatstatus) som de beslutningsrelevante feltene -
# person_finnes_fra_for er ren tilleggsinformasjon og skal ikke i seg selv trigge et avbrudd.
NY = "ny"
REAKTIVER = "reaktiver"
HOPP_OVER = "hopp_over"
BLOKKERT = "blokkert"

BEKREFTET = "bekreftet"
VENTELISTE = "venteliste"

_HANDLING_LABEL = {
    NY: "Klar til import",
    REAKTIVER: "Tidligere avmeldt - reaktiveres",
    HOPP_OVER: "Allerede påmeldt dette kurset - hoppes over",
    BLOKKERT: "Ugyldig / blokkert",
}


def beskriv(r: dict) -> str:
    """Lesbar visningstekst avledet fra de strukturerte beslutningsfeltene."""
    if r["handling"] == BLOKKERT:
        return r["melding"]
    tekst = _HANDLING_LABEL[r["handling"]]
    if r["resultatstatus"]:
        tekst += f" ({r['resultatstatus']})"
    if r["person_finnes_fra_for"] and r["handling"] == NY:
        tekst += " - personen finnes fra før i systemet"
    return tekst

# norsk kolonneoverskrift (normalisert til smaa bokstaver) -> internt feltnavn
KOLONNE_MAP = {
    "navn": "navn",
    "e-post": "epost", "epost": "epost",
    "telefon": "telefon",
    "yrkestittel": "yrkestittel",
    "arbeidssted": "arbeidssted",
    "hpr-nummer": "hpr_nr", "hpr-nr": "hpr_nr", "hpr": "hpr_nr",
    "betaler": "betaler",
    "firmanavn": "org_navn",
    "org.nr": "org_nr", "org nr": "org_nr", "org.nr.": "org_nr",
    "fakturaadresse": "faktura_adresse",
    "fakturareferanse": "faktura_ref",
    "fakturakommentar": "faktura_kommentar",
    "allergier": "allergier", "allergi": "allergier",
    "tilrettelegging": "tilrettelegging",
}
PAKREVDE_INTERNE_FELT = ("navn", "epost")

MALFIL_HEADER = ["Navn", "E-post", "Telefon", "Yrkestittel", "Arbeidssted", "HPR-nummer", "Betaler",
                 "Firmanavn", "Org.nr", "Fakturaadresse", "Fakturareferanse", "Fakturakommentar",
                 "Allergier", "Tilrettelegging"]
MALFIL_EKSEMPELRAD = ["Kari Eksempel", "kari.eksempel@eksempel.no", "99999999", "Psykolog",
                      "Eksempel Klinikk AS", "", "person", "", "", "", "", "", "", ""]


class ImportFeil(Exception):
    """Hele filen/importen kan ikke behandles - vises til admin, ingen rader lagres."""


class TokenFeil(Exception):
    """Forhaandsvisnings-tokenet er ukjent, utlopt, eller tilhorer et annet kurs/en annen admin -
    admin maa forhaandsvise paa nytt."""


class EndretTilstandFeil(Exception):
    """Databasetilstanden er endret siden forhaandsvisningen - importen er IKKE utfort.

    avvik: lesbar liste over hva som er endret, til visning for admin.
    """
    def __init__(self, avvik: list[str]):
        self.avvik = avvik
        super().__init__("Databasetilstanden er endret siden forhåndsvisningen.")


def _normaliser_header(header: str) -> str:
    return header.strip().lower()


def parse_csv(raw: bytes) -> list[dict]:
    """Leser CSV-bytes til en liste av normaliserte rad-dicts (interne feltnavn, strippet).

    Kaster ImportFeil hvis filen er for stor, ikke kan leses som tekst, mangler en obligatorisk
    kolonne, har urimelig mange kolonner, eller har flere datarader enn MAKS_RADER. Tomme linjer
    hoppes stille over. Radenes INNHOLD (f.eks. ugyldig e-post) valideres IKKE her - det gjor
    _kategoriser_og_meld_paa per rad, siden det kan involvere databasetilstand.
    """
    if len(raw) > MAKS_FILSTORRELSE:
        raise ImportFeil(f"Filen er for stor (maks {MAKS_FILSTORRELSE // (1024 * 1024)} MB).")

    tekst = None
    for enc in ("utf-8-sig", "cp1252"):
        try:
            tekst = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if tekst is None:
        raise ImportFeil("Kunne ikke lese filen som tekst. Lagre som CSV (UTF-8) fra Excel.")

    try:
        skilletegn = csv.Sniffer().sniff(tekst[:2000], delimiters=";,").delimiter
    except csv.Error:
        skilletegn = ";"

    leser = csv.reader(io.StringIO(tekst), delimiter=skilletegn)
    try:
        header_rad = next(leser)
    except StopIteration:
        raise ImportFeil("Filen er tom.")

    if len(header_rad) > MAKS_KOLONNER:
        raise ImportFeil(f"Filen har for mange kolonner (maks {MAKS_KOLONNER}).")

    header = [_normaliser_header(h) for h in header_rad]
    interne_kolonner = [KOLONNE_MAP.get(h) for h in header]
    for kravd in PAKREVDE_INTERNE_FELT:
        if kravd not in interne_kolonner:
            navn = "Navn" if kravd == "navn" else "E-post"
            raise ImportFeil(f"Filen mangler den obligatoriske kolonnen «{navn}».")

    rader = []
    for linje in leser:
        if not any((c or "").strip() for c in linje):
            continue
        if len(rader) >= MAKS_RADER:
            raise ImportFeil(f"Filen har flere enn {MAKS_RADER} rader. Del opp i flere filer.")
        rad = {}
        for felt, verdi in zip(interne_kolonner, linje):
            if felt:
                rad[felt] = (verdi or "").strip()
        rader.append(rad)
    return rader


def _valider_rad(rad: dict) -> list[str]:
    """Rene, databaseuavhengige sjekker - identiske krav som manuell paamelding (fase 9)."""
    problemer = []
    if not (rad.get("navn") or "").strip():
        problemer.append("Navn mangler")
    if "@" not in (rad.get("epost") or ""):
        problemer.append("Ugyldig eller manglende e-post")
    betaler = (rad.get("betaler") or "").strip().lower()
    if betaler and betaler not in ("person", "organisasjon"):
        problemer.append("Betaler må være «person» eller «organisasjon»")
    if betaler == "organisasjon" and not ((rad.get("org_navn") or "").strip() and (rad.get("org_nr") or "").strip()):
        problemer.append("Firmanavn og org.nr. må fylles ut når organisasjon betaler")
    for felt, verdi in rad.items():
        if verdi and len(verdi) > MAKS_CELLELENGDE:
            problemer.append(f"Feltet «{felt}» er for langt (maks {MAKS_CELLELENGDE} tegn)")
    return problemer


def _resultat(rad_nr: int, rad: dict, *, handling: str, melding: str, resultatstatus: str | None = None,
              person_finnes_fra_for: bool = False, paamelding_id: int | None = None) -> dict:
    return {
        "rad_nr": rad_nr, "navn": rad.get("navn", ""), "epost": rad.get("epost", ""),
        "handling": handling, "resultatstatus": resultatstatus,
        "person_finnes_fra_for": person_finnes_fra_for,
        "melding": melding, "paamelding_id": paamelding_id,
    }


def _kategoriser_og_meld_paa(con, kurs_id: int, rader: list[dict], aktor: str) -> list[dict]:
    """Kjorer HVER rad gjennom ekte validering/db.meld_paa(). Kalles baade av forhaandsvis()
    (som garantert ruller alt tilbake etterpaa) og av importer() (som lar det staa hvis alt
    stemmer med forhaandsvisningen). Ingen commit/rollback skjer her - det er kallerens ansvar."""
    kurs = con.execute("SELECT status FROM kurs WHERE id=?", (kurs_id,)).fetchone()
    if not kurs:
        raise ImportFeil("Ukjent kurs.")
    if kurs["status"] in ("avlyst", "avsluttet"):
        raise ImportFeil(f"Kurset har status «{kurs['status']}» og kan ikke ta imot nye påmeldinger.")

    sette_eposter: set[str] = set()
    resultater = []
    for i, rad in enumerate(rader, start=1):
        epost_norm = (rad.get("epost") or "").strip().lower()

        if epost_norm and epost_norm in sette_eposter:
            resultater.append(_resultat(
                i, rad, handling=BLOKKERT, melding="Samme e-post forekommer flere ganger i filen."))
            continue

        problemer = _valider_rad(rad)
        if problemer:
            resultater.append(_resultat(i, rad, handling=BLOKKERT, melding="; ".join(problemer)))
            continue
        sette_eposter.add(epost_norm)

        person_finnes = con.execute("SELECT 1 FROM deltaker WHERE epost=?", (epost_norm,)).fetchone() is not None
        eksisterende = con.execute(
            """SELECT p.status FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id
               WHERE d.epost=? AND p.kurs_id=?""", (epost_norm, kurs_id)).fetchone()
        if eksisterende and eksisterende["status"] != "avmeldt":
            resultater.append(_resultat(
                i, rad, handling=HOPP_OVER, person_finnes_fra_for=True,
                melding="Har allerede en aktiv påmelding på dette kurset - hoppes over."))
            continue

        try:
            # beskytt_eksisterende_felt=True: en CSV kan vaere en gammel/ufullstendig eksport og
            # skal ALDRI kunne overskrive nyere personopplysninger noen andre allerede har
            # registrert - se db.finn_eller_opprett_deltaker(). Standard (False) beholdes for
            # offentlig/gruppe/manuell paamelding - de skal fortsatt kunne oppdatere ved re-paamelding.
            pid, status = db.meld_paa(
                con, kurs_id, epost=rad["epost"], navn=rad["navn"],
                deltaker={"telefon": rad.get("telefon") or None, "yrkestittel": rad.get("yrkestittel") or None,
                         "arbeidssted": rad.get("arbeidssted") or None, "hpr_nr": rad.get("hpr_nr") or None},
                paamelding={
                    "betaler": "organisasjon" if (rad.get("betaler") or "").strip().lower() == "organisasjon" else "person",
                    "org_navn": rad.get("org_navn") or None, "org_nr": rad.get("org_nr") or None,
                    "faktura_adresse": rad.get("faktura_adresse") or None, "faktura_ref": rad.get("faktura_ref") or None,
                    "faktura_kommentar": rad.get("faktura_kommentar") or None,
                    "kilde": "admin_import", "sveiper_utsatt": 1,
                },
                sensitivt={"allergier": rad.get("allergier") or None, "tilrettelegging": rad.get("tilrettelegging") or None},
                aktor=aktor, tillat_utkast=True, ignorer_frist=True, beskytt_eksisterende_felt=True,
            )
        except db.Paameldingsfeil as e:
            resultater.append(_resultat(i, rad, handling=BLOKKERT, melding=str(e)))
            continue

        handling = REAKTIVER if eksisterende else NY  # eksisterende (avmeldt) paamelding -> reaktivert
        resultater.append(_resultat(
            i, rad, handling=handling, resultatstatus=status, person_finnes_fra_for=person_finnes,
            melding="", paamelding_id=pid))
        resultater[-1]["melding"] = beskriv(resultater[-1])
    return resultater


def forhaandsvis(con, kurs_id: int, rader: list[dict], aktor: str) -> list[dict]:
    """Kjorer _kategoriser_og_meld_paa() innenfor en SAVEPOINT som ALLTID rulles tilbake - uansett
    om noe kastet en exception. Garanterer at forhaandsvisning aldri etterlater spor i databasen
    (se test_forhaandsvisning_lar_ingen_spor_i_databasen)."""
    con.execute("SAVEPOINT fase10_forhaandsvisning")
    try:
        return _kategoriser_og_meld_paa(con, kurs_id, rader, aktor)
    finally:
        con.execute("ROLLBACK TO SAVEPOINT fase10_forhaandsvisning")
        con.execute("RELEASE SAVEPOINT fase10_forhaandsvisning")


def har_blokkerende_rader(resultater: list[dict]) -> bool:
    return any(r["handling"] == BLOKKERT for r in resultater)


def sammenlign_resultater(gamle: list[dict], nye: list[dict]) -> list[str]:
    """Sammenligner forhaandsvisningen (lagret server-side) med en fersk kjoring mot NAVAERENDE
    database. Sammenligner de BESLUTNINGSRELEVANTE feltene (handling + resultatstatus) - ikke bare
    en visningslabel, og ikke person_finnes_fra_for (som er ren tilleggsinformasjon og kan endre
    seg uskyldig uten at UTFALLET for denne raden er annerledes). Dekker alle eksemplene i planen:
    bekreftet<->venteliste, ny<->hopp_over, ny<->reaktiver, gyldig->blokkert."""
    avvik = []
    if len(gamle) != len(nye):
        avvik.append("Antall rader stemmer ikke lenger med forhåndsvisningen.")
        return avvik
    for g, n in zip(gamle, nye):
        if (g["handling"], g["resultatstatus"]) != (n["handling"], n["resultatstatus"]):
            avvik.append(f"Rad {g['rad_nr']} ({g.get('epost', '')}): var «{beskriv(g)}», er nå «{beskriv(n)}».")
    return avvik


def oppsummer(resultater: list[dict]) -> dict:
    """Tellinger til samleloggen og til flash-oppsummeringen etter import - ingen persondata,
    kun tall. Brukes av BAADE importer() (for hendelsesloggen) og admin-ruten (for flash), slik
    at tellemaaten aldri kan avvike mellom de to."""
    return {
        "antall_rader": len(resultater),
        "antall_importert": sum(1 for r in resultater if r["handling"] in (NY, REAKTIVER)),
        "antall_reaktivert": sum(1 for r in resultater if r["handling"] == REAKTIVER),
        "antall_bekreftet": sum(1 for r in resultater if r["resultatstatus"] == BEKREFTET),
        "antall_venteliste": sum(1 for r in resultater if r["resultatstatus"] == VENTELISTE),
        "antall_hoppet_over": sum(1 for r in resultater if r["handling"] == HOPP_OVER),
    }


def importer(con, kurs_id: int, rader: list[dict], forrige_resultater: list[dict], aktor: str) -> list[dict]:
    """Utforer den faktiske importen. Kjorer SAMME funksjon som forhaandsvisningen paa nytt mot
    fersk databasetilstand, og sammenligner resultatet mot det admin faktisk godkjente. Er noe
    vesentlig endret, kastes EndretTilstandFeil og db.transaksjon() ruller ALT tilbake - ingenting
    av det som ble skrevet i denne funksjonen blir liggende. Er alt uendret, forblir skrivingene
    og transaksjonen committes.

    Samleloggen (ett hendelsesinnslag, kun tellinger - se oppsummer()) skrives INNENFOR samme
    transaksjon som selve importen, slik at "import gjennomfort, men logging feilet" aldri kan
    bli to forskjellige databasetilstander - enten committes begge deler, eller ingen av dem.
    """
    with db.transaksjon(con):
        nye_resultater = _kategoriser_og_meld_paa(con, kurs_id, rader, aktor)
        avvik = sammenlign_resultater(forrige_resultater, nye_resultater)
        if avvik:
            raise EndretTilstandFeil(avvik)
        db.logg(con, "admin_import_utfort", {"kurs_id": kurs_id, **oppsummer(nye_resultater)}, aktor=aktor)
    return nye_resultater


def lagre_forhaandsvisning(con, kurs_id: int, admin_id: int, rader: list[dict], resultater: list[dict],
                           idag: datetime | None = None) -> str:
    """Lagrer radene + forhaandsvisningsresultatet server-side bak en tilfeldig, ugjettelig token
    (32 byte - samme sikkerhetsniva som innlogging_token/firmapaamelding.kvittering_token andre
    steder i kodebasen). Nettleseren far KUN token-strengen tilbake i et skjult felt - aldri
    radinnholdet (navn/e-post/allergi/faktura). Kalleren MA commit-e etterpa."""
    na = idag or datetime.now()
    token = secrets.token_urlsafe(32)
    con.execute(
        """INSERT INTO import_forhaandsvisning
               (token, kurs_id, admin_id, rader_json, resultater_json, opprettet, utloper)
           VALUES (?,?,?,?,?,?,?)""",
        (token, kurs_id, admin_id, json.dumps(rader, ensure_ascii=False), json.dumps(resultater, ensure_ascii=False),
         na.isoformat(timespec="seconds"),
         (na + timedelta(minutes=FORHAANDSVISNING_LEVETID_MIN)).isoformat(timespec="seconds")))
    return token


def hent_forhaandsvisning(con, token: str, kurs_id: int, admin_id: int, idag: datetime | None = None) -> dict:
    """Slar opp en lagret forhaandsvisning. Kaster TokenFeil (admin ma forhaandsvise paa nytt) hvis
    tokenet er ukjent, utlopt, eller ikke tilhorer NETTOPP dette kurset og NETTOPP denne admin-en -
    en admin skal aldri kunne bekrefte en annen admins eller et annet kurs sin forhaandsvisning
    ved a gjette/gjenbruke en token. Utlopte rader slettes med det samme de oppdages."""
    rad = con.execute("SELECT * FROM import_forhaandsvisning WHERE token=?", (token,)).fetchone()
    if not rad:
        raise TokenFeil("Ukjent eller allerede brukt forhåndsvisning. Last opp filen på nytt.")
    na = (idag or datetime.now()).isoformat(timespec="seconds")
    if na > rad["utloper"]:
        con.execute("DELETE FROM import_forhaandsvisning WHERE token=?", (token,))
        raise TokenFeil("Forhåndsvisningen er utløpt (maks 30 minutter). Last opp filen på nytt.")
    if rad["kurs_id"] != kurs_id or rad["admin_id"] != admin_id:
        raise TokenFeil("Forhåndsvisningen gjelder et annet kurs eller en annen administrator.")
    return {"rader": json.loads(rad["rader_json"]), "resultater": json.loads(rad["resultater_json"])}


def slett_forhaandsvisning(con, token: str) -> None:
    """Sletter en forhaandsvisning etter et bekreft-forsok - vellykket ELLER ikke. En brukt eller
    forkastet forhaandsvisning skal aldri kunne gjenbrukes, og skal aldri bli staaende som
    permanent historikk (radene kan inneholde allergi/tilrettelegging/fakturainformasjon)."""
    con.execute("DELETE FROM import_forhaandsvisning WHERE token=?", (token,))


def slett_forhaandsvisninger_for(con, kurs_id: int, admin_id: int) -> int:
    """Sletter eventuelle tidligere (ikke ennaa bekreftede) forhaandsvisninger fra SAMME admin paa
    SAMME kurs. Kalles foer en ny lagres, slik at gjentatte opplastinger (proving seg fram med
    forskjellige filversjoner) ikke bygger opp flere kopier av sensitive rader liggende samtidig."""
    cur = con.execute("DELETE FROM import_forhaandsvisning WHERE kurs_id=? AND admin_id=?", (kurs_id, admin_id))
    return cur.rowcount


def rydd_utlopte_forhaandsvisninger(con, idag: datetime | None = None) -> int:
    """Sletter alle utlopte forhaandsvisninger. Kan kalles periodisk (f.eks. fra daglig.kjor()) -
    ikke strengt nodvendig for korrekthet (hent_forhaandsvisning() rydder ogsa opp fortlopende
    naar en utlopt rad faktisk forsokes brukt), men holder tabellen fra a vokse ubegrenset med
    forhaandsvisninger ingen noensinne bekrefter."""
    na = (idag or datetime.now()).isoformat(timespec="seconds")
    cur = con.execute("DELETE FROM import_forhaandsvisning WHERE utloper < ?", (na,))
    return cur.rowcount


def malfil_csv() -> str:
    """Enkel importmal: header + én tydelig fiktiv eksempelrad. Ren streng - selve HTTP-svaret
    (Content-Disposition/BOM) settes av ruten i web-laget, som for de andre CSV-eksportene."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(MALFIL_HEADER)
    w.writerow(MALFIL_EKSEMPELRAD)
    return buf.getvalue()
