"""Oppmøtelisten for kursleder: en enkel side på mobilen der kursleder trykker på navnene når QR-koden ikke virker.

Deltakeren gjør ingenting selv. QR-koden (og koden på Min side) er fortsatt hovedløsningen; dette er reserven, og den
løser også tilfellet der noen ikke får skannet. Modulen er ren logikk (ingen Flask): rutene ligger i web/oppmoteliste_ruter.py.

Regler:
  * Hvem som vises for en kursdag bestemmes av EN funksjon, `deltakere_for_dag`. Alt annet (visning, «merk alle», «fjern alle»,
    enkeltrader) spør den, så listen og handlingene aldri kan bli uenige.
  * Handlingene sier hvilken tilstand som ønskes («til stede» / «ikke til stede»), ikke «bytt». Det gjør dem idempotente: to
    trykk på en gammel side, eller et gjentatt POST, gir samme sluttresultat. Det som allerede er registrert (QR, kode, Zoom)
    overskrives aldri av «til stede», og loggføres ikke på nytt.
  * Manuell registrering bruker db.registrer_oppmote med kilden «manuell». Fjerning sletter raden, som oppmøtematrisen gjør.
    Oppmøte fra Zoom fjernes bare med uttrykkelig bekreftelse (og aldri av «fjern alle»).
  * Loggen (hendelsestypen `oppmote_manuell`, som oppmøtematrisen bruker) får bare id-er og aktør, aldri navn eller e-post.
"""
from dataclasses import dataclass
from datetime import date

from . import db, deltakersok, norsk_tid
from .deltakerliste import norsk_sortering

MAKS_SOKEORD = 6            # flere ord enn dette i søket ignoreres (som i det globale deltakersøket)
MAKS_SOKETEGN = 100         # lengre søk avkortes

# Hvordan oppmøtet ble registrert, slik det står i den lille teksten i raden.
_KILDE_TEKST = {"qr": "QR-kode", "kode": "Innsjekk-kode", "zoom": "Zoom", "manuell": "Manuelt"}


# ---------------------------------------------------------------- hvem som vises

def deltakere_for_dag(con, kurs_id: int, kursdag_id: int) -> list[dict]:
    """Deltakerne som skal stå på oppmøtelisten for kursdagen, alfabetisk (norsk rekkefølge), med oppmøtet på den dagen.

    DETTE ER DEN ENESTE FUNKSJONEN SOM VELGER HVEM SOM VISES FOR EN DAG. Alle med plass på kurset vises (Påmeldt og
    Ekstradeltaker, se db.HAR_PLASS); de som ikke har plass vises ikke. En ekstradeltaker med samlingsutvalg vises bare på
    dagene i samlingene vedkommende er satt opp på (db.sql_egen_dag, samme regel som oppmøtematrisen og påminnelsene). Listen,
    «merk alle», «fjern alle» og enkeltradene følger med, siden alle spør denne funksjonen. En dag som ikke hører til kurset
    gir en tom liste.

    Hver rad: paamelding_id, navn, epost (bare til søk, vises aldri), kilde (qr/kode/zoom/manuell eller None), ts (UTC, eller
    None) og minutter (Zoom). Ingen andre personopplysninger leses.
    """
    rader = [dict(r) for r in con.execute(
        """SELECT p.id AS paamelding_id, p.status, p.avslatt_ts, p.utgatt_ts, p.forlatt_ts, p.ekstradeltaker_ts,
                  d.navn, d.epost, o.kilde, o.ts, o.minutter
           FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id
                JOIN kursdag kd ON kd.id=? AND kd.kurs_id=p.kurs_id
                LEFT JOIN oppmote o ON o.paamelding_id=p.id AND o.kursdag_id=kd.id
           WHERE p.kurs_id=? AND """ + db.sql_egen_dag("p", "kd"), (kursdag_id, kurs_id))]
    med_plass = [r for r in rader if db.paameldingsstatus(r) in db.HAR_PLASS]
    return sorted(med_plass, key=lambda r: (norsk_sortering(r["navn"]), r["paamelding_id"]))


# ---------------------------------------------------------------- kursdager

def hent_dag(con, kurs_id: int, dag_id: int):
    """Kursdagen hvis den hører til kurset, ellers None (en dag fra et annet kurs skal aldri kunne åpnes herfra)."""
    return con.execute("SELECT * FROM kursdag WHERE id=? AND kurs_id=?", (dag_id, kurs_id)).fetchone()


def standarddag(dager, idag: date):
    """Dagen oppmøtelisten åpner på: dagens kursdag, ellers den nærmeste (ved likt avstand: den tidligste, siden en dag som er
    over kan tas opp). None hvis kurset ikke har kursdager. `dager`: rader med `dato` (ISO) og `id`."""
    if not dager:
        return None
    return min(dager, key=lambda d: (abs((date.fromisoformat(d["dato"]) - idag).days), d["dato"], d["id"]))


def kan_ta_opp(dag, idag: date) -> bool:
    """Bare dager til og med i dag kan tas opp: en fremtidig kursdag har ikke noe oppmøte å registrere ennå."""
    return date.fromisoformat(dag["dato"]) <= idag


# ---------------------------------------------------------------- visning

def _klokkeslett(ts) -> str:
    return norsk_tid.klokkeslett(norsk_tid.fra_utc(ts)) if ts else ""


def hvordan_tekst(kilde: str | None, ts=None, minutter: int | None = None) -> str:
    """Den lille teksten i raden: «QR-kode kl. 08:47», «Manuelt kl. 09:02», «Zoom, 95 min». Tom når ingen oppmøte."""
    if not kilde:
        return ""
    navn = _KILDE_TEKST.get(kilde, kilde)
    if kilde == "zoom":                 # tidsstempelet er importtidspunktet, ikke når personen kom: vis minuttene i stedet
        return f"{navn}, {int(minutter)} min" if minutter else navn
    tid = _klokkeslett(ts)
    return f"{navn} kl. {tid}" if tid else navn


def sokenokkel(navn: str, epost: str) -> tuple[str, str]:
    """(nøkkel, nøkkel uten æøå) som søket leter i: navn og e-post, små bokstaver. Samme regler som deltakersøket
    (deltakersok._fold og _ascii): «solvi» finner «Sølvi», og «håkon» finner en som er registrert som «Hakon». E-posten ligger
    bare som skjult søkenøkkel i raden og vises aldri."""
    tekst = f"{navn} {epost}"
    return deltakersok._fold(tekst), deltakersok._ascii(tekst)


def sokeord(tekst) -> list[str]:
    """Ordene i søket (mellomrom skiller, maks MAKS_SOKEORD), med små bokstaver. Tomt søk gir en tom liste."""
    ord_ = deltakersok._fold(str(tekst or "")[:MAKS_SOKETEGN]).split()
    return ord_[:MAKS_SOKEORD]


def treffer(nokkel: tuple[str, str], tekst) -> bool:
    """Alle ordene i søket må treffe (delvis) i navnet eller e-posten; med eller uten æøå. Tomt søk treffer alle.
    static/oppmoteliste.js gjør det samme i nettleseren mens man skriver."""
    fold, uten = nokkel
    return all(o in fold or deltakersok._ascii(o) in uten for o in sokeord(tekst))


def rad_for_visning(r: dict) -> dict:
    """En rad fra deltakere_for_dag med det siden viser: til_stede, kilde, hvordan (liten tekst) og søkenøklene."""
    fold, uten = sokenokkel(r["navn"], r["epost"])
    return {"paamelding_id": r["paamelding_id"], "navn": r["navn"], "til_stede": bool(r["kilde"]), "kilde": r["kilde"] or "",
            "hvordan": hvordan_tekst(r["kilde"], r["ts"], r["minutter"]), "sok": fold, "sok_ascii": uten}


def tilstand(con, kurs_id: int, kursdag_id: int) -> dict:
    """Tilstanden til hele listen som ren data uten navn (til automatisk oppdatering av siden): antall, totalt og per påmelding
    {til_stede, kilde, hvordan}. Nøklene er påmeldings-id-er."""
    rader = [rad_for_visning(r) for r in deltakere_for_dag(con, kurs_id, kursdag_id)]
    return {"antall": sum(r["til_stede"] for r in rader), "totalt": len(rader),
            "rader": {str(r["paamelding_id"]): {"til_stede": r["til_stede"], "kilde": r["kilde"], "hvordan": r["hvordan"]}
                      for r in rader}}


def dagfaner(dager, valgt_id: int, idag: date) -> list[dict]:
    """Valget av kursdag: en fane per dag med nummer, dato og om den er i dag, i fortid eller i fremtiden."""
    faner = []
    for nr, d in enumerate(dager, start=1):
        dato = date.fromisoformat(d["dato"])
        faner.append({"id": d["id"], "nr": nr, "kort": f"{dato.day:02d}.{dato.month:02d}.", "er_idag": dato == idag,
                      "fremtid": dato > idag, "valgt": d["id"] == valgt_id})
    return faner


# ---------------------------------------------------------------- handlinger (committer ikke)

@dataclass(frozen=True)
class Utfall:
    """Utfallet av en enkeltrad. status: «endret» | «uendret» | «trenger_zoom_bekreftelse» | «ikke_i_listen»."""
    status: str
    til_stede: bool = False
    kilde: str | None = None


def _logg(con, paamelding_id: int, kursdag_id: int, registrert: bool, aktor: str) -> None:
    """Samme hendelse som oppmøtematrisen (`oppmote_manuell`): bare id-er, aldri navn eller e-post."""
    db.logg(con, "oppmote_manuell", {"paamelding_id": paamelding_id, "kursdag_id": kursdag_id, "registrert": registrert},
            aktor=aktor)


def _oppmote(con, paamelding_id: int, kursdag_id: int):
    return con.execute("SELECT kilde FROM oppmote WHERE paamelding_id=? AND kursdag_id=?", (paamelding_id, kursdag_id)).fetchone()


def _fjern(con, paamelding_id: int, kursdag_id: int) -> None:
    """Sletter oppmøteraden. Samme spørring som oppmøtematrisen (admin_oppmote i app.py) bruker: det finnes ingen egen funksjon for det."""
    con.execute("DELETE FROM oppmote WHERE paamelding_id=? AND kursdag_id=?", (paamelding_id, kursdag_id))


def sett_oppmote(con, kurs_id: int, kursdag_id: int, paamelding_id: int, til_stede: bool, aktor: str, *,
                 bekreft_zoom: bool = False) -> Utfall:
    """Setter oppmøtet til ønsket tilstand for én person på én dag. Idempotent: er tilstanden allerede slik, skjer ingenting
    (og ingenting loggføres). «Til stede» registrerer manuelt, men overskriver aldri et oppmøte som finnes fra før (QR, kode,
    Zoom). «Ikke til stede» sletter raden; er den fra Zoom kreves `bekreft_zoom`, ellers gjøres ingenting.
    Personen må stå på listen for dagen (`deltakere_for_dag`)."""
    if paamelding_id not in {r["paamelding_id"] for r in deltakere_for_dag(con, kurs_id, kursdag_id)}:
        return Utfall("ikke_i_listen")
    fra_for = _oppmote(con, paamelding_id, kursdag_id)
    if til_stede:
        if fra_for:
            return Utfall("uendret", True, fra_for["kilde"])
        db.registrer_oppmote(con, paamelding_id, kursdag_id, "manuell")
        _logg(con, paamelding_id, kursdag_id, True, aktor)
        return Utfall("endret", True, "manuell")
    if not fra_for:
        return Utfall("uendret", False, None)
    if fra_for["kilde"] == "zoom" and not bekreft_zoom:
        return Utfall("trenger_zoom_bekreftelse", True, "zoom")
    _fjern(con, paamelding_id, kursdag_id)
    _logg(con, paamelding_id, kursdag_id, False, aktor)
    return Utfall("endret", False, None)


def merk_alle(con, kurs_id: int, kursdag_id: int, aktor: str) -> int:
    """Registrerer manuelt oppmøte for alle på listen som ikke er registrert. Returnerer hvor mange som ble nye. De som allerede
    er registrert (uansett kilde) røres ikke."""
    nye = 0
    for r in deltakere_for_dag(con, kurs_id, kursdag_id):
        if not r["kilde"] and db.registrer_oppmote(con, r["paamelding_id"], kursdag_id, "manuell"):
            _logg(con, r["paamelding_id"], kursdag_id, True, aktor)
            nye += 1
    return nye


def fjern_alle(con, kurs_id: int, kursdag_id: int, aktor: str) -> tuple[int, int]:
    """Fjerner oppmøtet for alle på listen som er registrert, unntatt de fra Zoom (de fjernes bare én og én, med bekreftelse).
    Returnerer (fjernet, beholdt fra Zoom)."""
    fjernet = beholdt = 0
    for r in deltakere_for_dag(con, kurs_id, kursdag_id):
        if not r["kilde"]:
            continue
        if r["kilde"] == "zoom":
            beholdt += 1
            continue
        _fjern(con, r["paamelding_id"], kursdag_id)
        _logg(con, r["paamelding_id"], kursdag_id, False, aktor)
        fjernet += 1
    return fjernet, beholdt
