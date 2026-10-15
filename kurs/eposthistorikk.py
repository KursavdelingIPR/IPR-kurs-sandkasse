"""E-poster-fanen i deltakervinduet: e-postene som er sendt for én påmelding, nyeste først.

Hver e-post som sendes, lagres som en uforanderlig kopi FØR sendingen (sendt_epost, se Kjoring.send_ferdigrendret_en_gang).
Kopien vises nøyaktig slik den ble sendt - den bygges aldri opp igjen fra dagens mal. E-postene finnes via påmeldingen,
ikke via adressen, så de er med selv om deltakeren har byttet e-postadresse.

Eldre e-poster (fra før kopiene fantes) har bare en rad i utsendingsloggen: de vises med et lesbart navn og merknaden
«Innholdet ble ikke lagret for denne eldre e-posten». De finnes, som før, via kurset og deltakerens nåværende adresse.

Innloggingslenker til Min side går aldri gjennom motoren og har aldri kopi (personlig token).
"""
import base64
import re
from dataclasses import dataclass, field
from datetime import date

from . import db, norsk_tid

# Lesbare navn på e-posttypene (utsending_logg.type), til eldre e-poster uten kopi
_TYPENAVN = {
    "bekreftelse": "Påmeldingsbekreftelse",
    "venteliste": "Du står på venteliste",
    "ukefor": "Praktisk informasjon (uken før)",
    "kursbevis": "Kursbeviset er klart",
    "avlysning": "Kurset er avlyst",
    "avslag": "Påmeldingen er avslått",
    "admin_epost": "Manuell e-post",
}
_STATUS = {"sendt": "Sendt", "feilet": "Feilet", "ukjent": "Uavklart", "sender": "Sendes nå", "reservert": "Sendes nå"}
IKKE_LAGRET = "Innholdet ble ikke lagret for denne eldre e-posten."


@dataclass
class Epostrad:
    id: int | None                      # sendt_epost.id - None for eldre e-poster uten kopi
    emne: str
    dato: str
    klokkeslett: str
    til: str
    fra: str
    sendt_av: str
    status: str                         # sendt / feilet / uavklart / sender
    status_tekst: str
    antall_vedlegg: int = 0
    tidspunkt: str = ""                 # UTC, til sortering
    utsending_id: int | None = None     # manuell e-post: utsendelsen den var en del av (alle mottakere)
    nokkel: str = ""                    # bare eldre e-poster uten kopi: raden i utsendingsloggen (nokkel og type) som lenken peker på
    type: str = ""


@dataclass
class Epostvisning:
    rad: Epostrad
    kopi: str | None
    klokkeslett_sekunder: str
    feilmelding: str | None
    html: str                           # til den låste rammen: bilder (cid:) byttet ut med de lagrede filene
    vedlegg: list = field(default_factory=list)


def typenavn(type_: str) -> str:
    """'dagfor-2031-10-16' -> 'Påminnelse dagen før (16.10.2031)'. Ukjente typer vises som de er."""
    if type_.startswith("dagfor-"):
        try:
            return f"Påminnelse dagen før ({date.fromisoformat(type_[7:]).strftime('%d.%m.%Y')})"
        except ValueError:
            return "Påminnelse dagen før"
    return _TYPENAVN.get(type_, type_)


def _status(status: str, tidspunkt: str) -> str:
    """'sender'/'reservert' som har stått lenger enn grensen for uavklarte operasjoner, er uavklart."""
    if status in ("sender", "reservert"):
        return "uavklart" if str(tidspunkt) < db.utc_minutter_siden(db.UAVKLART_GRENSE_MIN) else "sender"
    return "uavklart" if status == "ukjent" else status


class _Navn:
    """«Sendt av»: administratorens navn, eller «Systemet (automatisk)»."""

    def __init__(self, con):
        self._admin = {r["brukernavn"]: r["navn"] for r in con.execute("SELECT brukernavn, navn FROM admin_bruker")}

    def __call__(self, aktor: str | None) -> str:
        if not aktor or aktor == "system":
            return "Systemet (automatisk)"
        if aktor.startswith("admin:"):
            return self._admin.get(aktor[6:], aktor[6:])
        return aktor


def _rad_fra_kopi(r, navn: _Navn) -> Epostrad:
    tid = norsk_tid.fra_utc(r["sendt_ts"] or r["opprettet"])
    status = _status(r["status"], r["opprettet"])
    return Epostrad(id=r["id"], emne=r["emne"], dato=norsk_tid.dato(tid), klokkeslett=norsk_tid.klokkeslett(tid),
                    til=r["til"], fra=r["fra"], sendt_av=navn(r["sendt_av"]), status=status,
                    status_tekst=_STATUS.get(status, "Uavklart"), antall_vedlegg=r["antall_vedlegg"],
                    tidspunkt=str(r["sendt_ts"] or r["opprettet"]), utsending_id=r["utsending_id"])


# Eldre e-poster er bare en rad i utsendingsloggen (ingen kopi): via kurset og deltakerens nåværende adresse, og bare de som ikke har kopi.
_ELDRE_SQL = """SELECT ul.nokkel, ul.mottaker, ul.type, ul.status, ul.sendt_ts, au.emne AS manuelt_emne, au.id AS utsending_id
                FROM utsending_logg ul LEFT JOIN admin_utsending au ON au.nokkel=ul.nokkel
                WHERE ul.mottaker=? AND (ul.nokkel=? OR ul.nokkel LIKE ?)
                  AND NOT EXISTS (SELECT 1 FROM sendt_epost se WHERE se.nokkel=ul.nokkel AND se.type=ul.type
                                  AND LOWER(se.til)=LOWER(ul.mottaker))"""


def _eldre_rad(r) -> Epostrad:
    tid = norsk_tid.fra_utc(r["sendt_ts"])
    status = _status(r["status"], r["sendt_ts"])
    return Epostrad(id=None, emne=r["manuelt_emne"] or typenavn(r["type"]), dato=norsk_tid.dato(tid), klokkeslett=norsk_tid.klokkeslett(tid),
                    til=r["mottaker"], fra="", sendt_av="", status=status, status_tekst=_STATUS.get(status, "Uavklart"),
                    tidspunkt=str(r["sendt_ts"]), utsending_id=r["utsending_id"], nokkel=r["nokkel"], type=r["type"])


def for_paamelding(con, p) -> list[Epostrad]:
    """Alle e-poster for påmeldingen, nyeste først: kopiene (via påmeldingen) og eldre e-poster uten kopi."""
    navn = _Navn(con)
    rader = [_rad_fra_kopi(r, navn) for r in con.execute(
        """SELECT se.*, au.id AS utsending_id, (SELECT COUNT(*) FROM sendt_epost_vedlegg v
                 WHERE v.sendt_epost_id=se.id AND v.innebygd_cid IS NULL) AS antall_vedlegg
           FROM sendt_epost se LEFT JOIN admin_utsending au ON au.nokkel=se.nokkel
           WHERE se.paamelding_id=?""", (p["id"],))]
    # Eldre e-poster (uten kopi): som i den gamle fanen, via kurset og nåværende adresse - men bare de som ikke har kopi
    rader += [_eldre_rad(r) for r in con.execute(_ELDRE_SQL, (p["epost"], f"kurs:{p['kurs_id']}", f"adhoc:{p['kurs_id']}:%"))]
    return sorted(rader, key=lambda x: (x.tidspunkt, x.id or 0), reverse=True)


def eldre_visning(con, p, nokkel: str, type_: str) -> Epostvisning | None:
    """Én eldre e-post uten kopi: bare det utsendingsloggen vet (når den ble sendt, til hvem, hva slags e-post og utfallet). Innholdet finnes ikke,
    og gjettes aldri eller bygges opp fra dagens mal. None hvis den ikke finnes for denne påmeldingen, eller hvis den har fått en kopi (da åpnes kopien)."""
    r = con.execute(_ELDRE_SQL + " AND ul.nokkel=? AND ul.type=?",
                    (p["epost"], f"kurs:{p['kurs_id']}", f"adhoc:{p['kurs_id']}:%", nokkel, type_)).fetchone()
    if not r:
        return None
    tid = norsk_tid.fra_utc(r["sendt_ts"])
    return Epostvisning(rad=_eldre_rad(r), kopi=None, klokkeslett_sekunder=norsk_tid.klokkeslett(tid, sekunder=True), feilmelding=None, html="")


def _trygt_filnavn(navn: str) -> str:
    return re.sub(r'[\x00-\x1f"\\/]+', "_", navn)[:120] or "vedlegg"


def visning(con, p, epost_id: int) -> Epostvisning | None:
    """Én e-post for påmeldingen, slik den ble sendt. None hvis den ikke finnes eller gjelder en annen påmelding."""
    r = con.execute(
        """SELECT se.*, au.id AS utsending_id, (SELECT COUNT(*) FROM sendt_epost_vedlegg v
                 WHERE v.sendt_epost_id=se.id AND v.innebygd_cid IS NULL) AS antall_vedlegg
           FROM sendt_epost se LEFT JOIN admin_utsending au ON au.nokkel=se.nokkel
           WHERE se.id=? AND se.paamelding_id=?""", (epost_id, p["id"])).fetchone()
    if not r:
        return None
    filer = db.epostkopi_vedlegg(con, r["id"])
    html = r["html"]
    for f in filer:                     # bare til visning: den lagrede kopien endres aldri
        if f["innebygd_cid"]:
            html = html.replace(f"cid:{f['innebygd_cid']}", f"data:{f['mimetype']};base64,{f['innhold']}")
    tid = norsk_tid.fra_utc(r["sendt_ts"] or r["opprettet"])
    return Epostvisning(
        rad=_rad_fra_kopi(r, _Navn(con)), kopi=r["kopi"], klokkeslett_sekunder=norsk_tid.klokkeslett(tid, sekunder=True),
        feilmelding=r["feilmelding"], html=html,
        vedlegg=[{"nr": f["nr"], "filnavn": _trygt_filnavn(f["filnavn"]), "mimetype": f["mimetype"],
                  "storrelse": f["storrelse"]} for f in filer if not f["innebygd_cid"]])


# Vedlegg som kan åpnes direkte i nettleseren. Alt annet lastes bare ned (aldri tolket som HTML/skript på vårt domene).
KAN_AAPNES = ("image/png", "image/jpeg", "image/gif", "application/pdf", "text/plain")


def vedlegg(con, p, epost_id: int, nr: int) -> tuple[str, str, bytes] | None:
    """(filnavn, mimetype, innhold) for et vedlegg i en e-post til påmeldingen, ellers None."""
    r = con.execute(
        """SELECT v.filnavn, f.mimetype, f.innhold FROM sendt_epost se
           JOIN sendt_epost_vedlegg v ON v.sendt_epost_id=se.id JOIN epost_fil f ON f.sha256=v.sha256
           WHERE se.id=? AND se.paamelding_id=? AND v.nr=?""", (epost_id, p["id"], nr)).fetchone()
    if not r:
        return None
    return _trygt_filnavn(r["filnavn"]), r["mimetype"], base64.b64decode(r["innhold"])


# ============================ «Mine e-poster» på Min side (Camilla 15.10.2026) ============================
# Deltakeren ser e-postene vi har sendt for påmeldingene sine: bare de som faktisk er sendt (status «sendt»), aldri de som venter i
# «Klar til sending» eller er stoppet, og bare de med lagret innhold. Listen (emne, dato, kurs) vises på alle innloggingsnivåer; selve
# innholdet og vedleggene bare når deltakeren er innlogget med e-post (kan inneholde adresse og faktura), se app.py.

_STIL_OG_SKRIPT = re.compile(r"<(style|script|head)\b.*?</\1\s*>", re.S | re.I)
_TAGGER = re.compile(r"<[^>]+>")
_MELLOMROM = re.compile(r"\s+")


def utdrag(html: str, maks: int = 150) -> str:
    """De første ordene i e-posten som ren tekst (til listen)."""
    import html as htmlmodul
    tekst = _MELLOMROM.sub(" ", htmlmodul.unescape(_TAGGER.sub(" ", _STIL_OG_SKRIPT.sub(" ", html or "")))).strip()
    return tekst if len(tekst) <= maks else tekst[:maks].rsplit(" ", 1)[0] + " …"


@dataclass
class MinEpost:
    id: int
    emne: str
    dato: str                           # «28.09.2026»
    klokkeslett: str
    kursnavn: str
    kurskode: str
    utdrag: str
    antall_vedlegg: int
    tidspunkt: str


_MINE_SQL = """SELECT se.id, se.emne, se.html, se.sendt_ts, se.opprettet, se.paamelding_id, k.navn AS kursnavn, k.kode AS kurskode,
                      (SELECT COUNT(*) FROM sendt_epost_vedlegg v WHERE v.sendt_epost_id=se.id AND v.innebygd_cid IS NULL) AS antall_vedlegg
               FROM sendt_epost se JOIN paamelding p ON p.id=se.paamelding_id JOIN kurs k ON k.id=p.kurs_id
               WHERE p.deltaker_id=? AND se.status='sendt'"""


def for_deltaker(con, deltaker_id: int, *, med_utdrag: bool = True) -> list[MinEpost]:
    """E-postene deltakeren har fått fra oss (alle kursene), nyeste først."""
    ut = []
    for r in con.execute(_MINE_SQL, (deltaker_id,)):
        tid = norsk_tid.fra_utc(r["sendt_ts"] or r["opprettet"])
        ut.append(MinEpost(r["id"], r["emne"], norsk_tid.dato(tid), norsk_tid.klokkeslett(tid), r["kursnavn"], r["kurskode"],
                           utdrag(r["html"]) if med_utdrag else "", r["antall_vedlegg"], str(r["sendt_ts"] or r["opprettet"])))
    return sorted(ut, key=lambda e: (e.tidspunkt, e.id), reverse=True)


def antall_for_deltaker(con, deltaker_id: int, kurskode: str | None = None) -> int:
    """Antall e-poster i «Mine e-poster» (alle kursene, eller ett)."""
    sql = "SELECT COUNT(*) FROM (" + _MINE_SQL + (" AND k.kode=?" if kurskode else "") + ")"
    return con.execute(sql, (deltaker_id, kurskode) if kurskode else (deltaker_id,)).fetchone()[0]


def min_visning(con, deltaker_id: int, epost_id: int) -> tuple[MinEpost, Epostvisning] | None:
    """Én av deltakerens egne e-poster (slik den ble sendt, med vedlegg), ellers None - også for en e-post som ikke er sendt."""
    r = con.execute(_MINE_SQL + " AND se.id=?", (deltaker_id, epost_id)).fetchone()
    if not r:
        return None
    v = visning(con, {"id": r["paamelding_id"]}, epost_id)
    if not v:
        return None
    tid = norsk_tid.fra_utc(r["sendt_ts"] or r["opprettet"])
    return MinEpost(r["id"], r["emne"], norsk_tid.dato(tid), norsk_tid.klokkeslett(tid), r["kursnavn"], r["kurskode"], "",
                    r["antall_vedlegg"], str(r["sendt_ts"] or r["opprettet"])), v


def mitt_vedlegg(con, deltaker_id: int, epost_id: int, nr: int) -> tuple[str, str, bytes] | None:
    r = con.execute(_MINE_SQL + " AND se.id=?", (deltaker_id, epost_id)).fetchone()
    return vedlegg(con, {"id": r["paamelding_id"]}, epost_id, nr) if r else None
