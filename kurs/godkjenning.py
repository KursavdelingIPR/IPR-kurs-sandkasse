"""«Klar til sending»: automatiske e-poster som venter på godkjenning før de sendes (Camilla 05.10.2026).

Bekreftelse og venteliste går automatisk (fakturaen venter på bekreftelsen). De andre automatiske e-postene til deltakere og
bedrifter (avlysning, påminnelsene uka før og dagen før, kursbevis, evaluering og kvittering til bedrift) legges i tabellen epost_godkjenning i stedet for å sendes. En administrator ser dem gruppert per kurs
og type, kan åpne hver e-post og velger Send eller Ikke send. Sendingen går gjennom den vanlige motoren
(Kjoring.send_ferdigrendret_en_gang med godkjent=True), så dedupliseringen, tørrkjøringen og e-posthistorikken er som før.

Personlige lenker (Min side, bedriftens kvitteringslenke) lagres aldri: de skjules når e-posten legges i kø og lages på nytt i
det den sendes. En e-post som ikke lenger gir mening (deltakeren er ikke påmeldt lenger, kurset er gjenåpnet, fristen er
passert) kan ikke sendes, men får status «utgått».

Interne e-poster (til kurspostboksen), innloggingslenker, avslag og e-post en administrator skriver selv går som før.
config.GODKJENN_EPOSTER = False slår godkjenningen av (alt går automatisk, slik det var før).
"""
import base64
import re
from datetime import date, timedelta

from . import config, db, minside, norsk_tid

# Typene (utsending_logg.type) som venter på godkjenning. dagfor-<dato> er en familie (én type per kursdag).
TYPER = frozenset({"avlysning", "ukefor", "kursbevis", "evaluering", "firmapaamelding_kvittering"})
FAMILIER = ("dagfor-",)

TYPENAVN = {
    "avlysning": "Avlysning",
    "ukefor": "Påminnelse uka før",
    "dagfor": "Påminnelse dagen før",
    "kursbevis": "Kursbevis klart",
    "evaluering": "Evaluering",
    "firmapaamelding_kvittering": "Kvittering til bedrift",
}

STATUSNAVN = {"venter": "Venter", "sendt": "Sendt", "avvist": "Ikke sendt", "utgaatt": "Utgått", "uavklart": "Uavklart"}

EVALUERING_DAGER = 14       # som evaluering.DAGER_ETTER: lenger etter siste kursdag gir evalueringen ikke mening

_KVITTERING = re.compile(r"(/gruppe/kvittering/)[A-Za-z0-9_\-]+")
_CID = re.compile(r"cid:(signaturbilde-[0-9]+@ipr)")


def krever_godkjenning(type_: str) -> bool:
    """Om en e-post av denne typen skal vente i «Klar til sending» i stedet for å sendes med en gang."""
    if not config.GODKJENN_EPOSTER:
        return False
    return type_ in TYPER or any(type_.startswith(f) for f in FAMILIER)


def familie(type_: str) -> str:
    """dagfor-2026-10-12 -> dagfor, ellers typen selv."""
    for f in FAMILIER:
        if type_.startswith(f):
            return f[:-1]
    return type_


def typenavn(type_: str) -> str:
    navn = TYPENAVN.get(familie(type_), type_)
    if type_.startswith("dagfor-"):
        return f"{navn} ({_norsk_dato(type_[len('dagfor-'):])})"
    return navn


# ============================ personlige lenker ============================

def skjul_lenker(html: str) -> str:
    """Personlige tilgangslenker byttes med «skjult» før e-posten lagres i køen (de lages på nytt ved sendingen)."""
    html = minside.skjul_i_kopi(html)
    return _KVITTERING.sub(r"\1skjult", html)


def gjenopprett_lenker(k, rad, html: str) -> str:
    """Lager de skjulte lenkene på nytt, slik de er i dag. Min side uten gyldig lenke (stengt, utløpt) gir innloggingssiden."""
    skjult_min_side = f"{config.BASE_URL}/min/skjult"
    if skjult_min_side in html:
        lenke = k.min_side_lenke(rad["paamelding_id"]) if rad["paamelding_id"] else None
        html = html.replace(skjult_min_side, lenke or f"{config.BASE_URL}/logg-inn")
    if "/gruppe/kvittering/skjult" in html:
        token = _kvitteringstoken(k.con, rad["nokkel"])
        if token is None:
            raise ValueError("Bedriftspåmeldingen finnes ikke lenger.")
        html = html.replace("/gruppe/kvittering/skjult", f"/gruppe/kvittering/{token}")
    return html


def _kvitteringstoken(con, nokkel: str) -> str | None:
    try:
        firma_id = int(nokkel.split(":", 1)[1])
    except (IndexError, ValueError):
        return None
    rad = con.execute("SELECT kvittering_token FROM firmapaamelding WHERE id=?", (firma_id,)).fetchone()
    return rad["kvittering_token"] if rad else None


# ============================ legge i kø ============================

def legg_i_koe(con, *, nokkel: str, til: str, type_: str, emne: str, html: str, paamelding_id=None, mal=None, kurs_id=None,
               lagre_kopi: bool = True) -> bool:
    """Legger e-posten i «Klar til sending». Ligger den der fra før (samme nøkkel, mottaker og type), skjer ingenting: True bare
    når den ble lagt inn nå. Kalleren committer. Uten kurs (f.eks. avlysning via send_til_mange) hentes kurset fra påmeldingen."""
    if kurs_id is None and paamelding_id is not None:
        rad = con.execute("SELECT kurs_id FROM paamelding WHERE id=?", (paamelding_id,)).fetchone()
        kurs_id = rad["kurs_id"] if rad else None
    cur = con.execute(
        """INSERT INTO epost_godkjenning (nokkel, mottaker, type, mal, paamelding_id, kurs_id, emne, html, lagre_kopi, utloper,
                                          status, opprettet)
           VALUES (?,?,?,?,?,?,?,?,?,?,'venter',?) ON CONFLICT DO NOTHING""",
        (nokkel, til, type_, mal, paamelding_id, kurs_id, emne, skjul_lenker(html), 1 if lagre_kopi else 0,
         utloper(con, type_, nokkel, paamelding_id, kurs_id), db.naa_utc()))
    return cur.rowcount > 0


def utloper(con, type_: str, nokkel: str, paamelding_id, kurs_id) -> str | None:
    """Siste dag e-posten gir mening å sende (YYYY-MM-DD), eller None (ingen frist)."""
    if type_.startswith("dagfor-"):
        try:
            return (date.fromisoformat(type_[len("dagfor-"):]) - timedelta(days=1)).isoformat()
        except ValueError:
            return None
    if type_ == "ukefor" and paamelding_id is not None:
        forste = _forste_dag(con, paamelding_id)
        return (forste - timedelta(days=1)).isoformat() if forste else None
    if type_ == "evaluering" and kurs_id is not None:
        from . import evaluering     # importeres her: evaluering bruker db
        samling_id = evaluering.samling_fra_nokkel(nokkel)
        if samling_id:               # evalueringen etter en samling (kurs/evaluering.py): fristen regnes fra samlingens siste dag
            siste = con.execute("SELECT MAX(dato) FROM kursdag WHERE kurs_id=? AND samling_id=?", (kurs_id, samling_id)).fetchone()[0]
        else:
            siste = con.execute("SELECT MAX(dato) FROM kursdag WHERE kurs_id=?", (kurs_id,)).fetchone()[0]
        if siste:
            return (date.fromisoformat(str(siste)[:10]) + timedelta(days=EVALUERING_DAGER)).isoformat()
    return None


def _forste_dag(con, paamelding_id) -> date | None:
    p = con.execute("SELECT id, kurs_id, ekstradeltaker_ts FROM paamelding WHERE id=?", (paamelding_id,)).fetchone()
    if not p:
        return None
    dager = db.paameldingens_kursdager(con, p, db.kursdager(con, p["kurs_id"]))
    return date.fromisoformat(str(dager[0]["dato"])[:10]) if dager else None


# ============================ gyldighet ============================

def hvorfor_ikke(con, rad, idag: date) -> str | None:
    """Hvorfor e-posten ikke kan sendes nå (vises til administrator), eller None når den fortsatt gir mening."""
    if rad["utloper"] and idag.isoformat() > rad["utloper"]:
        return "Fristen for å sende den er passert."
    type_ = rad["type"]
    if type_ == "avlysning":
        kurs = con.execute("SELECT status FROM kurs WHERE id=?", (rad["kurs_id"],)).fetchone()
        if not kurs or kurs["status"] != "avlyst":
            return "Kurset er ikke lenger avlyst."
        if rad["paamelding_id"] is not None and not _paamelding_har_status(con, rad["paamelding_id"], ("bekreftet", "venteliste")):
            return "Deltakeren står ikke lenger på kurset."
        return None
    if type_ == "firmapaamelding_kvittering":
        return None if _kvitteringstoken(con, rad["nokkel"]) else "Bedriftspåmeldingen finnes ikke lenger."
    if rad["paamelding_id"] is not None and not _paamelding_har_status(con, rad["paamelding_id"], ("bekreftet",)):
        return "Deltakeren har ikke lenger plass på kurset."
    return None


def _paamelding_har_status(con, paamelding_id, statuser) -> bool:
    p = con.execute("SELECT status FROM paamelding WHERE id=?", (paamelding_id,)).fetchone()
    return bool(p) and p["status"] in statuser


# ============================ lese ============================

def antall_ventende(con) -> int:
    return con.execute("SELECT COUNT(*) FROM epost_godkjenning WHERE status='venter'").fetchone()[0]


def grupper(con, idag: date) -> list[dict]:
    """De ventende e-postene gruppert per kurs og type, eldste kurs først: [{kurs, type, navn, rader, kan_sendes}]. Hver rad har
    `grunn` (hvorfor den ikke kan sendes nå, eller None) og deltakerens navn når e-posten gjelder en påmelding."""
    rader = con.execute(
        """SELECT g.id, g.nokkel, g.mottaker, g.type, g.mal, g.paamelding_id, g.kurs_id, g.emne, g.utloper, g.status, g.opprettet,
                  d.navn AS deltaker_navn, k.navn AS kursnavn, k.kursnr
           FROM epost_godkjenning g
           LEFT JOIN paamelding p ON p.id=g.paamelding_id LEFT JOIN deltaker d ON d.id=p.deltaker_id
           LEFT JOIN kurs k ON k.id=g.kurs_id
           WHERE g.status='venter' ORDER BY g.kurs_id, g.type, d.navn, g.mottaker""").fetchall()
    ut: dict[tuple, dict] = {}
    for r in rader:
        gruppe = ut.setdefault((r["kurs_id"], r["type"]), {
            "kurs_id": r["kurs_id"], "kursnavn": r["kursnavn"], "kursnr": r["kursnr"], "type": r["type"],
            "navn": typenavn(r["type"]), "rader": []})
        gruppe["rader"].append({**dict(r), "grunn": hvorfor_ikke(con, r, idag)})
    for g in ut.values():
        g["kan_sendes"] = sum(1 for r in g["rader"] if not r["grunn"])
    return list(ut.values())


def sist_behandlet(con, antall: int = 30) -> list[dict]:
    """De sist behandlede (sendt, ikke sendt, utgått, uavklart), nyeste først, med norsk tid."""
    rader = con.execute(
        """SELECT g.id, g.mottaker, g.type, g.status, g.merknad, g.behandlet, g.behandlet_av, d.navn AS deltaker_navn, k.kursnr
           FROM epost_godkjenning g
           LEFT JOIN paamelding p ON p.id=g.paamelding_id LEFT JOIN deltaker d ON d.id=p.deltaker_id
           LEFT JOIN kurs k ON k.id=g.kurs_id
           WHERE g.status != 'venter' ORDER BY g.behandlet DESC, g.id DESC LIMIT ?""", (antall,)).fetchall()
    ut = []
    for r in rader:
        tid = norsk_tid.fra_utc(r["behandlet"])
        ut.append({**dict(r), "tid": f"{norsk_tid.dato(tid)} kl. {norsk_tid.klokkeslett(tid)}" if tid else ""})
    return ut


def hent(con, godkjenning_id: int):
    return con.execute(
        """SELECT g.*, d.navn AS deltaker_navn, k.navn AS kursnavn, k.kursnr FROM epost_godkjenning g
           LEFT JOIN paamelding p ON p.id=g.paamelding_id LEFT JOIN deltaker d ON d.id=p.deltaker_id
           LEFT JOIN kurs k ON k.id=g.kurs_id WHERE g.id=?""", (godkjenning_id,)).fetchone()


def forhandsvisning(con, rad) -> str:
    """E-posten slik den vil se ut, med signaturbildene som innebygde data (forhåndsvisningen kan ikke hente cid:-bilder).
    De personlige lenkene står som «skjult»: de lages først i det e-posten sendes."""
    html = rad["html"] or ""

    def bilde(m):
        try:
            bilde_id = int(m.group(1).split("-", 1)[1].split("@", 1)[0])
        except (IndexError, ValueError):
            return m.group(0)
        b = con.execute("SELECT mimetype, innhold FROM signatur_bilde WHERE id=?", (bilde_id,)).fetchone()
        return f"data:{b['mimetype']};base64,{b['innhold']}" if b else m.group(0)
    return _CID.sub(bilde, html)


# ============================ handlinger ============================

def send(k, ider) -> dict:
    """Sender de valgte ventende e-postene gjennom motoren (godkjent=True). En e-post som ikke lenger gir mening, sendes ikke, men
    får status «utgått». Returnerer {"sendt", "utgaatt", "uavklart", "ikke_forsokt"}."""
    from .kjoring import EpostkopiFeil, SendingStanset
    from .maltekster import MalFeil

    ut = {"sendt": 0, "utgaatt": 0, "uavklart": 0, "ikke_forsokt": 0}
    for godkjenning_id in ider:
        rad = k.con.execute("SELECT * FROM epost_godkjenning WHERE id=? AND status='venter'", (godkjenning_id,)).fetchone()
        if not rad:
            continue
        grunn = hvorfor_ikke(k.con, rad, k.idag)
        if grunn:
            _avslutt(k, rad["id"], "utgaatt", grunn)
            ut["utgaatt"] += 1
            continue
        if k.sending_stanset:
            ut["ikke_forsokt"] += 1
            continue
        try:
            html = gjenopprett_lenker(k, rad, rad["html"])
            sendt = k.send_ferdigrendret_en_gang(rad["nokkel"], rad["mottaker"], rad["type"], rad["emne"], html,
                                                 paamelding_id=rad["paamelding_id"], mal=rad["mal"], kurs_id=rad["kurs_id"],
                                                 lagre_kopi=bool(rad["lagre_kopi"]), godkjent=True)
        except (SendingStanset, EpostkopiFeil, MalFeil, ValueError):
            ut["ikke_forsokt"] += 1          # ingenting reservert eller sendt: kan prøves igjen
            continue
        except Exception:  # noqa: BLE001 - motoren har satt 'ukjent' og logget (uten persondata)
            _avslutt(k, rad["id"], "uavklart", "Utfallet er uavklart – se «Uavklarte operasjoner».")
            ut["uavklart"] += 1
            continue
        if sendt or db.allerede_sendt(k.con, rad["nokkel"], rad["mottaker"], rad["type"]):
            _avslutt(k, rad["id"], "sendt", None)
            ut["sendt"] += 1
        else:
            _avslutt(k, rad["id"], "uavklart", "Et annet forsøk på samme e-post pågår eller er uavklart.")
            ut["uavklart"] += 1
    if any(ut.values()):
        db.logg(k.con, "epost_godkjent", {k2: v for k2, v in ut.items() if v}, aktor=k.aktor)
    if not k.tor:
        k.con.commit()
    return ut


def avvis(con, ider, aktor: str) -> int:
    """«Ikke send»: e-postene sendes aldri (heller ikke av morgenjobben senere). Returnerer antallet."""
    antall = 0
    for godkjenning_id in ider:
        antall += con.execute(
            """UPDATE epost_godkjenning SET status='avvist', html=NULL, merknad=?, behandlet=?, behandlet_av=?
               WHERE id=? AND status='venter'""",
            ("Ikke sendt etter valg fra administrator.", db.naa_utc(), aktor, godkjenning_id)).rowcount
    if antall:
        db.logg(con, "epost_avvist", {"antall": antall}, aktor=aktor)
    return antall


def rydd(k) -> int:
    """Morgenjobben: ventende e-poster som ikke lenger gir mening, får status «utgått». Idempotent."""
    antall = 0
    for rad in k.con.execute("SELECT * FROM epost_godkjenning WHERE status='venter'").fetchall():
        grunn = hvorfor_ikke(k.con, rad, k.idag)
        if grunn:
            _avslutt(k, rad["id"], "utgaatt", grunn)
            antall += 1
    venter = antall_ventende(k.con)
    k.si(f"  {venter} e-post(er) venter i «Klar til sending»" + (f", {antall} utgått i dag" if antall else ""))
    return antall


def _avslutt(k, godkjenning_id: int, status: str, merknad: str | None) -> None:
    """Endelig status. Innholdet slettes (det som ble sendt, ligger i e-posthistorikken)."""
    k.con.execute("UPDATE epost_godkjenning SET status=?, html=NULL, merknad=?, behandlet=?, behandlet_av=? WHERE id=?",
                  (status, merknad, db.naa_utc(), k.aktor, godkjenning_id))


def _norsk_dato(iso: str) -> str:
    iso = str(iso or "")[:10]
    return f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}" if len(iso) == 10 else iso
