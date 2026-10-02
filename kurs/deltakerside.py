"""Kursside for deltakerne: tilgang, landing etter innlogging og visningsdata (ren logikk: ingen Flask).

Tilgangsreglene (SPEC §8.1) sjekkes i selve ruten ved HVER forespørsel, aldri bare ved å skjule lenker:
  1. kurset finnes, 2. deltakeren har en BEKREFTET påmelding på akkurat dette kurset, 3. kurset er åpent, fullt, pågår eller
  avsluttet (ikke utkast eller avlyst), 4. siden er publisert, 5. siden er ikke tatt ned, 6. siden er ikke stengt.
  Alle avslag i 1-3 gir samme svar (siden avslører aldri om et kurs finnes eller om deltakeren er på venteliste).

Innsjekk (SPEC §10): `registrer_qr` (skann av QR-koden i kurslokalet, eller kode + e-post), `registrer_selv` («Registrer oppmøte i
dag» på nett for innlogget deltaker) og `tekst_for` (alle brukertekster). Deltakeren identifiseres med innlogget økt hvis den finnes,
ellers e-post, aldri navn. «Bare én gang per dag» er garantert av databasen (primærnøkkel paamelding + kursdag).

`bygg_visning` bygger malkonteksten (SPEC §7.2): eksplisitte dict med bare det siden skal vise. Skjulte blokker, blokker som ikke har
nådd «vis fra»-datoen og filer som ikke er synlige ennå FINNES IKKE i utdataene. Aldri `SELECT d.*`, aldri `kurs.notat`, aldri
allergier/tilrettelegging, aldri innsjekk-kode eller -token, aldri deltakerens adresse.
"""
import hmac
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable
from urllib.parse import quote, urlsplit

from markupsafe import Markup

from . import config, db, kursdatoer, norsk_tid, sidelager
from . import sideinnhold as si
from .integrasjoner import sharepoint

TEKST_IKKE_PUBLISERT = "Min side er ikke åpnet ennå. Du får beskjed fra kursadministrasjonen når den er klar."
TEKST_IKKE_APEN = "Min side er ikke åpen for dette kurset"        # kurskortet i Mine kurs når siden ikke er publisert, er tatt ned eller er stengt (stemmer i alle tre tilfellene)
TEKST_IKKE_PAA_SAMLINGEN = "Du er ikke satt opp på denne samlingen. Snakk med kursansvarlig."
OK_KURSSTATUS = ("aapen", "full", "aktiv", "avsluttet")     # kursstatuser som kan gi tilgang til siden (ikke utkast/avlyst)
ANONYM_DOMENE = "@" + db.ANONYM_DOMENE                      # anonymiserte personer har aldri tilgang
_KURS_KOLONNER = ("id, kursnr, kode, navn, type, status, sted, start_kl, slutt_kl, zoom_url, zoom_id, sharepoint_mappe")   # aldri notat


@dataclass(frozen=True)
class Tilgang:
    ok: bool
    arsak: str          # "ok" | "ikke_funnet" | "ikke_publisert" | "nedtatt" | "stengt"
    kurs: object        # kursraden (uten notat) eller None
    side: object        # kursside-raden eller None
    stenges: date | None


def hent_kurs_for_side(con, kode: str):
    """Kursraden med bare de kolonnene siden trenger (aldri kurs.notat)."""
    return con.execute(f"SELECT {_KURS_KOLONNER} FROM kurs WHERE kode=?", (kode,)).fetchone()


def tilgang(con, deltaker_id: int, kode: str, idag: date) -> Tilgang:
    """Rekkefølgen av sjekkene er bindende (§8.1): kurs finnes, bekreftet påmelding, kursstatus, publisert, aktiv, ikke stengt."""
    kurs = hent_kurs_for_side(con, kode or "")
    if not kurs:
        return Tilgang(False, "ikke_funnet", None, None, None)
    pm = con.execute(
        """SELECT d.epost FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id
           WHERE p.deltaker_id=? AND p.kurs_id=? AND p.status='bekreftet'""", (deltaker_id, kurs["id"])).fetchone()
    if not pm or pm["epost"].lower().endswith(ANONYM_DOMENE) or kurs["status"] not in OK_KURSSTATUS:
        return Tilgang(False, "ikke_funnet", None, None, None)
    side = sidelager.hent(con, kurs["id"])
    stenges = sidelager.effektiv_stenges(con, kurs["id"], side)
    if not side or not side["publisert"]:
        return Tilgang(False, "ikke_publisert", kurs, side, stenges)
    if not side["aktiv"]:
        return Tilgang(False, "nedtatt", kurs, side, stenges)
    if stenges is not None and idag > stenges:
        return Tilgang(False, "stengt", kurs, side, stenges)
    return Tilgang(True, "ok", kurs, side, stenges)


def har_side(con, kurs_id: int, idag: date | None = None) -> bool:
    """Har kurset en publisert, åpen kursside (uavhengig av hvem som spør)? Brukes av innsjekk-resultatet og Min side."""
    kurs = con.execute("SELECT status FROM kurs WHERE id=?", (kurs_id,)).fetchone()
    side = sidelager.hent(con, kurs_id)
    if not kurs or kurs["status"] not in OK_KURSSTATUS or not side or not side["publisert"] or not side["aktiv"]:
        return False
    stenges = sidelager.effektiv_stenges(con, kurs_id, side)
    return stenges is None or (idag or date.today()) <= stenges


def materiell_apent(con, kurs_id: int, idag: date | None = None) -> bool:
    """Kan deltakerne hente kursholders filer (SharePoint) via Min side og /materiell? Ja, unntatt når kurssiden er publisert og siden tatt
    ned (nødbrems) eller stengt: da er hele kurset stengt for deltakerne, ikke bare siden. Kurs uten publisert side: som før (åpent)."""
    side = sidelager.hent(con, kurs_id)
    if not side or not side["publisert"]:
        return True
    if not side["aktiv"]:
        return False
    stenges = sidelager.effektiv_stenges(con, kurs_id, side)
    return stenges is None or (idag or date.today()) <= stenges


def landing(con, deltaker_id: int, idag: date) -> str | None:
    """Hvilket kurs (kurs.kode) en deltaker sendes til rett etter innlogging uten `neste`, ellers None (= Min side).
    Kandidater = påmeldinger med plass der siden er publisert, åpen og ikke stengt. Ett kurs som pågår eller kommer: det. Flere:
    det som har kursdag i dag (nøyaktig ett). Ingen slike, og nøyaktig ett avsluttet kurs: det. Ellers None. /min-side sender aldri videre."""
    koder = [r["kode"] for r in con.execute(
        """SELECT k.kode FROM paamelding p JOIN kurs k ON k.id=p.kurs_id
           WHERE p.deltaker_id=? AND p.status='bekreftet' ORDER BY k.id""", (deltaker_id,))]
    kandidater = [t.kurs for t in (tilgang(con, deltaker_id, kode, idag) for kode in koder) if t.ok]
    pagar = [k for k in kandidater if k["status"] != "avsluttet"]
    if len(pagar) == 1:
        return pagar[0]["kode"]
    if len(pagar) > 1:
        idag_iso = idag.isoformat()
        i_dag = [k for k in pagar if any(d["dato"] == idag_iso for d in kursdager_for(con, k, _paamelding_id(con, deltaker_id, k["id"])))]
        return i_dag[0]["kode"] if len(i_dag) == 1 else None
    avsluttet = [k for k in kandidater if k["status"] == "avsluttet"]
    return avsluttet[0]["kode"] if len(avsluttet) == 1 else None


# ============================ norske datoer og tekster ============================

_UKEDAGER = ("mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag")
_UKEDAGER_KORT = ("man", "tir", "ons", "tor", "fre", "lør", "søn")
_MAANEDER = ("januar", "februar", "mars", "april", "mai", "juni", "juli", "august", "september", "oktober", "november", "desember")


def dato_lang(d: date, *, aar: bool = False) -> str:
    """fredag 27. november (med aar=True: fredag 27. november 2026)"""
    return f"{_UKEDAGER[d.weekday()]} {d.day}. {_MAANEDER[d.month - 1]}" + (f" {d.year}" if aar else "")


def dato_kort(d: date) -> str:
    """tor 26. november"""
    return f"{_UKEDAGER_KORT[d.weekday()]} {d.day}. {_MAANEDER[d.month - 1]}"


def dag_maaned(d: date) -> str:
    """14. januar"""
    return f"{d.day}. {_MAANEDER[d.month - 1]}"


def dag_maaned_aar(d: date) -> str:
    """14. januar 2027"""
    return f"{dag_maaned(d)} {d.year}"


def apen_tekst(stenges: date | None) -> str:
    """Setningen på et avsluttet kurs om hvor lenge siden er åpen: «Siden er åpen til 14. januar 2027, ...». Ingen sluttdato (kurset har
    «Ingen tidsbegrensning», eller ingen kursdager): siden blir liggende åpen."""
    if stenges is not None:
        return f"Siden er åpen til {dag_maaned_aar(stenges)}, så du kan laste ned det du trenger."
    return "Siden blir liggende åpen, så du kan laste ned det du trenger."


def storrelse_tekst(byte: int) -> str:
    """8,1 MB · 412 kB"""
    if byte >= 1024 * 1024:
        return f"{byte / (1024 * 1024):.1f}".replace(".", ",") + " MB"
    return f"{max(1, round(byte / 1024))} kB"


_FILTYPER = {"pdf": ("PDF", "pdf", "PDF"), "ppt": ("PPT", "ppt", "PowerPoint"), "pptx": ("PPT", "ppt", "PowerPoint"),
             "odp": ("PPT", "ppt", "Presentasjon"), "key": ("PPT", "ppt", "Presentasjon"),
             "doc": ("DOC", "doc", "Word"), "docx": ("DOC", "doc", "Word"), "odt": ("DOC", "doc", "Word"),
             "xls": ("XLS", "xls", "Excel"), "xlsx": ("XLS", "xls", "Excel"), "ods": ("XLS", "xls", "Excel"),
             "zip": ("ZIP", "zip", "ZIP"), "txt": ("TXT", "txt", "Tekst")}


def filtype(filnavn: str, type_: str) -> tuple[str, str, str]:
    """(kode, css-klasse, etikett): PDF/pdf/PDF, PPT/ppt/PowerPoint, ... Bilder: «BILDE»/«bilde»/«Bilde»."""
    if type_ == "bilde":
        return "BILDE", "bilde", "Bilde"
    endelse = filnavn.rsplit(".", 1)[-1].lower() if "." in filnavn else ""
    return _FILTYPER.get(endelse, ("FIL", "fil", "Fil"))


def _klokkeslett(fra: str, til: str) -> str:
    return f"{fra}–{til}" if fra and til else fra or til or ""


# ============================ kursdager ============================

def kursdager_for(con, kurs, paamelding_id: int | None = None) -> list[dict]:
    """Kursdagene til sideinnhold: [{"id", "dato", "start", "slutt", "samling", "merknad"}], i datorekkefølge. `samling` er samlingens navn
    (eller «Samling N» når kurset har flere samlinger uten navn), ellers None. Med `paamelding_id`: bare dagene den påmeldingen er
    på (db.paameldingens_kursdager: en ekstradeltaker med samlingsutvalg får bare sine samlinger). «Samling N» er alltid kursets
    eget nummer, også når bare noen av samlingene er med."""
    alle = db.kursdager(con, kurs["id"])
    samlinger: list = []
    for r in alle:
        if r["samling_id"] is not None and r["samling_id"] not in samlinger:
            samlinger.append(r["samling_id"])
    flere = len(samlinger) > 1
    rader = db.paameldingens_kursdager(con, paamelding_id, alle) if paamelding_id else alle
    ut = []
    for r in rader:
        navn = r["samling_navn"] or (f"Samling {samlinger.index(r['samling_id']) + 1}" if flere and r["samling_id"] is not None else None)
        ut.append({"id": int(r["id"]), "dato": r["dato"], "samling_id": r["samling_id"],
                   "start": r["start_kl"] or r["samling_start_kl"] or kurs["start_kl"],
                   "slutt": r["slutt_kl"] or r["samling_slutt_kl"] or kurs["slutt_kl"], "samling": navn,
                   "merknad": (r["merknad"] if "merknad" in r.keys() else None) or ""})
    return ut


def _dagens_kursdag(dager: list[dict], idag: date) -> dict | None:
    return next((d for d in dager if d["dato"] == idag.isoformat()), None)


def _effektiv_idag(dager: list[dict], idag: date, simuler_kursdag_id: int | None) -> date:
    """Forhåndsvisningens «Vis som dag …»: siden vises som om det var den kursdagen."""
    if simuler_kursdag_id is not None:
        d = next((d for d in dager if d["id"] == simuler_kursdag_id), None)
        if d:
            return date.fromisoformat(d["dato"])
    return idag


def dager_utenfor(con, kurs, deltaker_id: int | None) -> frozenset:
    """Kursdag-id-ene deltakeren IKKE er på (en ekstradeltaker på noen samlinger; ellers tom). Filer knyttet til disse vises ikke på
    kurssiden og hentes ikke via filruten."""
    pid = _paamelding_id(con, deltaker_id, kurs["id"])
    if not pid:
        return frozenset()
    egne = {int(d["id"]) for d in db.paameldingens_kursdager(con, pid)}
    return frozenset(int(d["id"]) for d in db.kursdager(con, kurs["id"])) - egne


def _paamelding_id(con, deltaker_id: int | None, kurs_id: int) -> int | None:
    if deltaker_id is None:
        return None
    rad = con.execute("SELECT id FROM paamelding WHERE deltaker_id=? AND kurs_id=? AND status='bekreftet'",
                      (deltaker_id, kurs_id)).fetchone()
    return int(rad["id"]) if rad else None


# ============================ innsjekk-kortet («I dag») ============================

def oppmote_i_dag(con, deltaker_id: int | None, kurs, side, idag: date, *, simuler_kursdag_id: int | None = None) -> dict | None:
    """Data til «I dag»-kortet: None når kurset ikke har kursdag i dag (eller er avsluttet). Ellers
    {"kursdag_id", "dato_lang", "nr", "av", "tid", "registrert": "08:47"|None, "tilbys": bool, "krever_kode": bool, "forklaring": str|None}.
    Aldri innsjekk-kode eller -token. `tilbys` følger tabellen i §10.5: fysiske og hybride kurs tilbyr innsjekk på nett (med kode
    hvis kurssiden krever det); digitale kurs med Zoom-import registrerer oppmøte automatisk; digitale kurs uten Zoom-import tilbyr én knapp."""
    pid = _paamelding_id(con, deltaker_id, kurs["id"])
    alle = kursdager_for(con, kurs)
    dager = kursdager_for(con, kurs, pid) if pid else alle          # deltakerens egne dager (ekstradeltaker: bare valgte samlinger)
    idag = _effektiv_idag(alle, idag, simuler_kursdag_id)
    dag = _dagens_kursdag(dager, idag)
    if kurs["status"] == "avsluttet" or not (dag or _dagens_kursdag(alle, idag)):
        return None
    if not dag:       # kursdag i dag, men ikke en av deltakerens dager (ekstradeltaker på andre samlinger)
        return {"kursdag_id": None, "dato_lang": dato_lang(idag), "nr": None, "av": None, "tid": "", "registrert": None,
                "tilbys": False, "krever_kode": False, "forklaring": TEKST_IKKE_PAA_SAMLINGEN}
    registrert = None
    if pid is not None:
        rad = con.execute("SELECT ts FROM oppmote WHERE paamelding_id=? AND kursdag_id=?", (pid, dag["id"])).fetchone()
        registrert = norsk_tid.klokkeslett(norsk_tid.fra_utc(rad["ts"])) if rad else None
    tilbys, forklaring = True, None
    if kurs["type"] == "digital" and kurs["zoom_id"]:
        tilbys, forklaring = False, "Oppmøte på digitale kurs registreres automatisk fra Zoom dagen etter."
    krever_kode = kurs["type"] in ("fysisk", "hybrid") and (side is None or bool(side["innsjekk_krever_kode"]))
    return {"kursdag_id": dag["id"], "dato_lang": dato_lang(date.fromisoformat(dag["dato"])),
            "nr": dager.index(dag) + 1, "av": len(dager), "tid": _klokkeslett(dag["start"], dag["slutt"]),
            "registrert": registrert, "tilbys": tilbys and pid is not None, "krever_kode": krever_kode, "forklaring": forklaring}


# ============================ innsjekk: QR, kode og «Registrer oppmøte» på nett ============================

INNSJEKK_KURSSTATUS = ("aapen", "full", "aktiv")     # innsjekk er bare åpent for kurs som er åpne, fulle eller pågår


@dataclass(frozen=True)
class Oppmote:
    """Utfallet av en innsjekk. status: «ny» | «allerede» | «ikke_i_dag» | «ikke_paameldt» | «ikke_paa_samlingen» | «ingen_kursdag» | «kode_feil» | «ikke_tilbudt»."""
    status: str
    fornavn: str | None = None       # bare satt når deltakeren er innlogget (uinnloggede får aldri vite navnet)
    tid: str | None = None           # «08:47» i norsk tid: ny = nå, allerede = da oppmøtet først ble registrert
    kursdag_nr: int | None = None    # «dag 2 ...»
    kursdag_av: int | None = None    # «... av 4»


def dagnummer(con, kurs_id: int, kursdag_id: int, paamelding_id: int | None = None) -> tuple[int | None, int | None]:
    """(dag nr, antall dager) for kursdagen i kurset, i datorekkefølge. Med `paamelding_id`: regnet blant dagene den påmeldingen er
    på (en ekstradeltaker med utvalg), og kursets tall når dagen ikke er blant dem."""
    ider = [int(r["id"]) for r in con.execute("SELECT id FROM kursdag WHERE kurs_id=? ORDER BY dato, id", (kurs_id,))]
    if paamelding_id:
        egne = [int(d["id"]) for d in db.paameldingens_kursdager(con, paamelding_id)]
        if int(kursdag_id) in egne:
            return egne.index(int(kursdag_id)) + 1, len(egne)
    return (ider.index(int(kursdag_id)) + 1, len(ider)) if int(kursdag_id) in ider else (None, len(ider) or None)


def _registrer(con, paamelding_id: int, deltaker_id: int, kursdag_id: int, kilde: str) -> tuple[bool, str]:
    """Registrerer oppmøtet (én gang per påmelding og kursdag: databasen sier nei til nr. 2) og logger nye registreringer.
    Returnerer (ny, klokkeslett i norsk tid). Klokkeslettet er alltid det FØRSTE oppmøtet ble registrert. Committer ikke."""
    ny = db.registrer_oppmote(con, paamelding_id, kursdag_id, kilde)
    rad = con.execute("SELECT ts FROM oppmote WHERE paamelding_id=? AND kursdag_id=?", (paamelding_id, kursdag_id)).fetchone()
    if ny:
        # Ingen persondata i loggen: bare id-er. Vises i Logger under deltakeren som «Deltakeren selv».
        db.logg(con, "oppmote_selv", {"paamelding_id": paamelding_id, "kursdag_id": kursdag_id, "registrert": True, "kilde": kilde},
                aktor=f"deltaker:{deltaker_id}")
    return ny, (norsk_tid.klokkeslett(norsk_tid.fra_utc(rad["ts"])) if rad else "")


def registrer_qr(con, kursdag, idag: date, *, deltaker_id: int | None = None, epost: str | None = None, kilde: str = "qr") -> Oppmote:
    """Innsjekk for en kursdag (QR-koden, eller dagens kode + e-post). `kursdag` er raden med id, kurs_id, dato. Innlogget deltaker
    (deltaker_id) identifiserer seg selv; ellers brukes e-posten. Bare i dag, bare for bekreftet påmelding på akkurat dette kurset,
    og bare når kurset er åpent, fullt eller pågår. Alle avslag for en uinnlogget gir samme tekst (tekst_for). Committer ikke."""
    tall = list(dagnummer(con, kursdag["kurs_id"], kursdag["id"]))

    def svar(status, **felt):
        return Oppmote(status, kursdag_nr=tall[0], kursdag_av=tall[1], **felt)

    kurs = con.execute("SELECT status FROM kurs WHERE id=?", (kursdag["kurs_id"],)).fetchone()
    if kursdag["dato"] != idag.isoformat() or not kurs or kurs["status"] not in INNSJEKK_KURSSTATUS:
        return svar("ikke_i_dag")
    sql = ("SELECT p.id, p.kurs_id, p.ekstradeltaker_ts, d.id AS deltaker_id, d.fornavn FROM paamelding p "
           "JOIN deltaker d ON d.id=p.deltaker_id WHERE p.kurs_id=? AND p.status='bekreftet' AND ")
    if deltaker_id:
        rad = con.execute(sql + "d.id=?", (kursdag["kurs_id"], deltaker_id)).fetchone()
    else:
        rad = con.execute(sql + "d.epost=?", (kursdag["kurs_id"], (epost or "").strip().lower())).fetchone()
    if not rad:
        return svar("ikke_paameldt")
    if db.er_ekstradeltaker(rad):        # ekstradeltaker: bare på egne samlinger (admin kan fortsatt registrere manuelt)
        egne = db.paameldingens_kursdager(con, rad)
        if int(kursdag["id"]) not in {int(x["id"]) for x in egne}:
            if not deltaker_id:       # uinnlogget: nøyaktig samme svar som for en e-post vi ikke finner (aldri røpe hvem som er på kurset)
                return svar("ikke_paameldt")
            return svar("ikke_paa_samlingen", fornavn=rad["fornavn"])
        tall[:] = dagnummer(con, kursdag["kurs_id"], kursdag["id"], rad["id"])
    ny, tid = _registrer(con, rad["id"], rad["deltaker_id"], kursdag["id"], kilde)
    # Klokkeslettet til en som ALLEREDE var registrert vises bare for henne selv (innlogget): ellers røper svaret til en fremmed med
    # QR-koden når en annen kom, og teksten skiller seg fra en ny registrering.
    return svar("ny" if ny else "allerede", fornavn=rad["fornavn"] if deltaker_id else None, tid=tid if (ny or deltaker_id) else None)


def registrer_selv(con, deltaker_id: int, kode: str, innsendt_kode: str, idag: date) -> Oppmote:
    """«Registrer oppmøte i dag» på nett (SPEC §10.5) for en innlogget deltaker. `kode` er kursets kode (fra adressen), `innsendt_kode`
    er dagens innsjekk-kode fra skjermen i lokalet. Påmeldingen slås opp fra deltaker-id og kurskode (aldri en id fra klienten).
    Kilde: «kode». Digitale kurs med Zoom-import tilbyr ikke dette (Zoom registrerer). Committer ikke."""
    kurs = hent_kurs_for_side(con, kode or "")
    pid = _paamelding_id(con, deltaker_id, kurs["id"]) if kurs else None
    if not pid:
        return Oppmote("ikke_paameldt")
    rad = con.execute("SELECT fornavn FROM deltaker WHERE id=?", (deltaker_id,)).fetchone()
    fornavn = rad["fornavn"] if rad else None
    dager = kursdager_for(con, kurs, pid)         # deltakerens egne dager
    dag = _dagens_kursdag(dager, idag)
    nr, av = (dager.index(dag) + 1, len(dager)) if dag else (None, len(dager) or None)
    if kurs["status"] not in INNSJEKK_KURSSTATUS or not (dag or _dagens_kursdag(kursdager_for(con, kurs), idag)):
        return Oppmote("ingen_kursdag", fornavn, None, nr, av)
    if not dag:                                   # kursdag i dag, men ikke en av dine (ekstradeltaker på andre samlinger)
        return Oppmote("ikke_paa_samlingen", fornavn, None, nr, av)
    if kurs["type"] == "digital" and kurs["zoom_id"]:
        return Oppmote("ikke_tilbudt", fornavn, None, nr, av)
    side = sidelager.hent(con, kurs["id"])
    if kurs["type"] in ("fysisk", "hybrid") and (side is None or side["innsjekk_krever_kode"]):
        fasit = con.execute("SELECT innsjekk_kode FROM kursdag WHERE id=?", (dag["id"],)).fetchone()["innsjekk_kode"] or ""
        if not fasit or not hmac.compare_digest(db.alfanumerisk(innsendt_kode or ""), db.alfanumerisk(fasit)):
            return Oppmote("kode_feil", fornavn, None, nr, av)         # ingenting skrives
    ny, tid = _registrer(con, pid, deltaker_id, dag["id"], "kode")
    return Oppmote("ny" if ny else "allerede", fornavn, tid, nr, av)


def kursinfo_for_innsjekk(con, kursdag, idag: date, deltaker_id: int | None = None) -> dict:
    """Det innsjekk-sidene får vite om kursdagen: kursnavn, kurskode, dato, «dag n av m» og om innsjekken er åpen nå (kursdagen er i dag
    og kurset er åpent, fullt eller pågår). Aldri innsjekk-kode eller -token. `kursdag` trenger id, kurs_id og dato; `idag` er appens dato (demo-spoling).
    Med `deltaker_id` (innlogget) regnes «dag n av m» blant dagene DEN PERSONEN er på (en ekstradeltaker på noen samlinger), og
    `ikke_paa_samlingen` sier at kursdagen ikke er en av dem. Uten: kursets egne tall (plakaten, og de som ikke er innlogget)."""
    kurs = con.execute("SELECT navn, kode, status FROM kurs WHERE id=?", (kursdag["kurs_id"],)).fetchone()
    pid = _paamelding_id(con, deltaker_id, kursdag["kurs_id"])
    nr, av = dagnummer(con, kursdag["kurs_id"], kursdag["id"], pid)
    ikke_paa = bool(pid) and int(kursdag["id"]) not in {int(x["id"]) for x in db.paameldingens_kursdager(con, pid)}
    d = date.fromisoformat(kursdag["dato"])
    return {"navn": kurs["navn"], "kode": kurs["kode"], "dato": kursdag["dato"], "dato_lang": dato_lang(d), "dag_nr": nr, "dag_av": av,
            "dag_tekst": f"Dag {nr} av {av}" if nr and av else "", "apen_i_dag": kurs["status"] in INNSJEKK_KURSSTATUS and d == idag,
            "ikke_paa_samlingen": ikke_paa}


def registrert_tid(con, deltaker_id: int, kursdag) -> str | None:
    """Klokkeslettet (norsk tid) deltakeren allerede er registrert for kursdagen, ellers None. Bare lesing."""
    pid = _paamelding_id(con, deltaker_id, kursdag["kurs_id"])
    rad = con.execute("SELECT ts FROM oppmote WHERE paamelding_id=? AND kursdag_id=?", (pid, kursdag["id"])).fetchone() if pid else None
    return norsk_tid.klokkeslett(norsk_tid.fra_utc(rad["ts"])) if rad else None


def maskert_epost(epost: str) -> str:
    """eva@example.no -> e***@example.no (første tegn i lokaldelen, tre stjerner og domenet, alt med små bokstaver)."""
    lokal, _, domene = (epost or "").strip().partition("@")
    return f"{lokal[:1].lower()}***@{domene.lower()}" if domene else "***"


def tekst_for(res: Oppmote, *, innlogget: bool, nett: bool = False) -> str:
    """Brukerteksten for et utfall (SPEC §10.6). Uinnloggede får nøytrale svar uten navn, og samme tekst for alle grunner til at
    e-posten ikke ble funnet (ukjent e-post, venteliste, avmeldt, annet kurs). `nett`: «Registrer oppmøte i dag» på Min side/kurssiden."""
    fn = res.fornavn if innlogget else None
    kl = f" (kl. {res.tid})" if res.tid else ""
    if res.status == "ny":
        if not fn:
            return "Oppmøte er registrert."
        return f"Oppmøte registrert. Velkommen, {fn}!" if nett else f"Velkommen, {fn}! Oppmøte er registrert."
    if res.status == "allerede":
        if not innlogget or not fn:
            return f"Du er allerede registrert i dag{kl}."
        return f"Du var allerede registrert i dag{kl}." if nett else f"Hei {fn} – du var allerede sjekket inn i dag{kl}."
    if res.status == "ikke_paameldt":
        if innlogget:
            return "Vi finner ikke en bekreftet påmelding for deg på dette kurset. Kontakt kursansvarlig."
        return ("Vi kunne ikke registrere oppmøte med denne e-postadressen. Sjekk at du bruker e-posten du meldte deg på med. "
                "Får du det ikke til, kontakt kursansvarlig.")
    if res.status == "ikke_paa_samlingen":
        # Bare innlogget deltaker får vite det: til en uinnlogget sier vi ikke noe om hvem som er på kurset (samme tekst som over)
        if innlogget:
            return TEKST_IKKE_PAA_SAMLINGEN
        return ("Vi kunne ikke registrere oppmøte med denne e-postadressen. Sjekk at du bruker e-posten du meldte deg på med. "
                "Får du det ikke til, kontakt kursansvarlig.")
    if res.status == "kode_feil":
        return "Koden stemmer ikke. Den står på skjermen i kurslokalet. Spør kursholder hvis du ikke ser den."
    if res.status == "ingen_kursdag":
        return "Det er ingen kursdag i dag på dette kurset."
    if res.status == "ikke_tilbudt":
        return "Oppmøte på dette kurset registreres automatisk."
    return "Innsjekk for denne kursdagen er ikke åpen i dag."          # ikke_i_dag (og alt ukjent)


# ============================ Min side ============================

def min_side_info(con, deltaker_id: int, paameldinger, idag: date) -> dict[int, dict]:
    """Det Min side trenger per kurs: {kurs_id: {"kan_apne": bool, "kode": str, "tekst": str | None, "i_dag": dict | None}}.
    kan_apne = deltakeren kommer inn på kurssiden akkurat nå (bekreftet, publisert, åpen, ikke stengt); tekst forklarer hvorfor ikke (venteliste,
    eller bekreftet påmelding på et kurs der Min side ikke er åpen: ikke publisert, tatt ned eller stengt); i_dag = «Registrer oppmøte i dag»-kortet (bare bekreftet påmelding på et kurs som har kursdag i dag). `paameldinger` er
    påmeldingsradene med kolonnene kurs_id, kode, status og kursstatus."""
    ut: dict[int, dict] = {}
    for p in paameldinger:
        info = {"kan_apne": False, "kode": p["kode"], "tekst": None, "i_dag": None}
        if p["status"] == "venteliste" and p["kursstatus"] in INNSJEKK_KURSSTATUS:
            info["tekst"] = "Min side åpnes når du har fått plass"
        elif p["status"] == "bekreftet":
            info["kan_apne"] = tilgang(con, deltaker_id, p["kode"], idag).ok
            if not info["kan_apne"] and p["kursstatus"] in OK_KURSSTATUS:        # ikke avlyst/utkast: «Avlyst» sier det allerede
                info["tekst"] = TEKST_IKKE_APEN                                   # uten forklaring ser det ut som noe mangler
            kurs = hent_kurs_for_side(con, p["kode"])
            if kurs and kurs["status"] in INNSJEKK_KURSSTATUS:
                info["i_dag"] = oppmote_i_dag(con, deltaker_id, kurs, sidelager.hent(con, kurs["id"]), idag)
        ut[p["kurs_id"]] = info
    return ut


# ============================ kursholders filer (SharePoint) ============================

_SP_CACHE: dict[str, tuple[float, tuple[list[dict], bool]]] = {}
_SP_LAAS = threading.Lock()
SP_CACHE_SEKUNDER = 60


def nullstill_sp_cache() -> None:
    with _SP_LAAS:
        _SP_CACHE.clear()


def list_sharepoint(kurs) -> tuple[list[dict], bool]:
    """(filer, ok) for «Fra kursholder»: filene i Kurs/<kode>/Presentasjoner, med 60 sekunders cache per mappe. ok=False betyr at
    henting feilet (siden viser da en vennlig tekst, ikke «ingen filer»). Bare kurs med SharePoint-mappe; aldri direkte tilgang
    for deltakerne (filene hentes via appen, som sjekker påmeldingen)."""
    if not kurs["sharepoint_mappe"]:
        return [], True
    mappe = f"{kurs['sharepoint_mappe']}/Presentasjoner"
    naa = time.monotonic()
    with _SP_LAAS:
        treff = _SP_CACHE.get(mappe)
        if treff and naa - treff[0] < SP_CACHE_SEKUNDER:
            return treff[1]
    filer, ok = sharepoint.list_filer_med_status(mappe)
    with _SP_LAAS:
        _SP_CACHE[mappe] = (naa, (filer, ok))
    return filer, ok


# ============================ visningsdata ============================

def _lagt_ut_tekst(dato: date | None, tid: datetime | None, idag: date) -> tuple[str, bool]:
    """(«i dag kl. 07:55» | «i går» | «26. november», ny). Ny = lagt ut i dag eller i går."""
    d = dato or (tid.date() if tid else None)
    if d is None:
        return "", False
    if d == idag:
        return ("i dag kl. " + norsk_tid.klokkeslett(tid)) if tid and not dato else "i dag", True
    if d == idag - timedelta(days=1):
        return "i går", True
    return dag_maaned(d), False


def _domene(url: str) -> str:
    deler = urlsplit(url)
    if deler.scheme in ("http", "https"):
        return (deler.hostname or "").removeprefix("www.")
    return url.split(":", 1)[-1]


def _initialer(navn: str) -> str:
    ord_ = [o for o in navn.replace(",", " ").split() if o]
    return ((ord_[0][0] + ord_[-1][0]) if len(ord_) > 1 else navn[:2]).upper()


def _avsnitt(tekst: str) -> list[str]:
    return [a.strip() for a in tekst.replace("\r", "").split("\n\n") if a.strip()]


def _program_data(b: dict, dager: list[dict], idag: date, utenfor=frozenset(), med_samling: bool = False) -> dict:
    """`dager` er dagene deltakeren er på; `utenfor` er id-ene til kursdager hen IKKE er på (en ekstradeltaker: andre samlinger).
    `med_samling` (kurs med flere samlinger): hver dag får samlingens navn og id, så malen kan sette en overskrift for hver samling."""
    per_id = {d["id"]: (i + 1, d) for i, d in enumerate(dager)}
    ut = []
    for i, dag in enumerate(b["data"].get("dager", [])):
        if dag.get("kursdag_id") in utenfor:
            continue
        nr, kd = per_id.get(dag.get("kursdag_id"), (None, None))
        dato = date.fromisoformat(kd["dato"]) if kd else si._iso(dag.get("dato"))
        punkter = []
        for p in dag.get("punkter", []):
            if not (p.get("tema") or p.get("fra") or p.get("til")):
                continue
            punkter.append({"tid": _klokkeslett(p.get("fra", ""), p.get("til", "")), "tema": p.get("tema", ""),
                            "sted": "" if p.get("pause") else p.get("sted", ""), "hvem": "" if p.get("pause") else p.get("hvem", ""),
                            "pause": bool(p.get("pause"))})
        ut.append({"nr": nr or i + 1, "tittel": dag.get("tittel") or f"Dag {nr or i + 1}",
                   "dato_lang": dato_lang(dato) if dato else "", "dato": dato, "tid": _klokkeslett(kd["start"], kd["slutt"]) if kd else "",
                   "samling": kd["samling"] if kd and med_samling else None, "samling_id": kd["samling_id"] if kd and med_samling else None,
                   "antall": len(punkter), "i_dag": dato == idag, "apen": False, "punkter": punkter})
    dagens = next((d for d in ut if d["i_dag"]), None)
    kommende = next((d for d in ut if d["dato"] and d["dato"] > idag), None)
    if dagens:
        dagens["apen"] = True
    elif kommende:
        kommende["apen"] = True
    for d in ut:
        d.pop("dato")
    antall = len(ut)
    return {"undertittel": f"{antall} kursdag{'er' if antall != 1 else ''}" if antall else "", "dager": ut}


def _filer_data(con, kurs, b: dict, filmeta: dict[int, dict], dager: list[dict], idag: date, fil_url, sp_url,
                utenfor=frozenset(), med_samling: bool = False) -> dict | None:
    d = b["data"]
    per_id = {kd["id"]: (i + 1, kd) for i, kd in enumerate(dager)}
    grupper: dict = {}
    for e in d.get("filer", []):
        meta = filmeta.get(e.get("fil_id"))
        synlig = si.fil_synlig(b, e, idag)
        if not meta or synlig == "nei" or e.get("gruppe") in utenfor:     # filer til dager deltakeren ikke er på, vises ikke
            continue
        fra = si._iso(e.get("synlig_fra"))
        tid = norsk_tid.fra_utc(meta["opprettet"])
        lagt_ut, ny = _lagt_ut_tekst(fra, tid, idag)
        kode, klasse, etikett = filtype(meta["filnavn"], meta["type"])
        tittel = e.get("tittel") or meta["filnavn"]
        oppforing = {"tittel": tittel, "url": fil_url(meta["id"]) if synlig == "ja" else None,
                     "kommer": dag_maaned(fra) if synlig == "kommer" and fra else None, "type_kode": kode, "type_klasse": klasse,
                     "meta": f"{etikett} · {storrelse_tekst(meta['storrelse'])}" + (f" · lagt ut {lagt_ut}" if lagt_ut and synlig == "ja" else ""),
                     "ny": bool(ny and synlig == "ja")}
        gruppe = e.get("gruppe") if e.get("gruppe") in per_id else None
        grupper.setdefault(gruppe, []).append(oppforing)
    dagsgrupper = [{"tittel": (f"{kd['samling']} · " if med_samling and kd["samling"] else "") + f"Dag {nr} · {dato_lang(date.fromisoformat(kd['dato']))}",
                    "filer": grupper[kdid]}
                   for kdid, (nr, kd) in per_id.items() if kdid in grupper]
    ut = list(dagsgrupper)
    if None in grupper:      # filer uten dag kommer sist, men med egen overskrift når det finnes dagsgrupper: ellers ser de ut til å høre til siste dag
        ut.append({"tittel": "Øvrige filer" if dagsgrupper else None, "filer": grupper[None]})
    sp = None
    if d.get("sharepoint") and kurs["sharepoint_mappe"]:
        filer, ok = list_sharepoint(kurs)
        sp = {"filer": [{"navn": f["navn"], "url": sp_url(f["navn"])} for f in filer], "ok": ok}
        if ok and not filer:
            sp = None                                     # ingenting å vise (og ingen feil)
    if not ut and sp is None:
        return None
    return {"grupper": ut, "sharepoint": sp}


def _lenker_data(kurs, b: dict, avsluttet: bool) -> dict | None:
    grupper: dict = {}
    for e in b["data"].get("lenker", []):
        # Adressen valideres på nytt ved hver visning (lagret innhold kan være endret utenom siden): bare https, http, mailto og tel
        url = si.trygg_url(e.get("url") or "" if e.get("kilde") != "zoom" else ("" if avsluttet else kurs["zoom_url"] or "")) or ""
        if not url:
            continue
        grupper.setdefault(e.get("nivaa"), []).append({
            "tittel": e.get("tittel") or _domene(url), "url": url, "tekst": e.get("tekst", ""), "domene": _domene(url),
            "ekstern": url.lower().startswith(("http://", "https://"))})
    ut = [{"tittel": {"obligatorisk": "Obligatorisk", "anbefalt": "Anbefalt", None: None}[n], "lenker": grupper[n]}
          for n in (None, "obligatorisk", "anbefalt") if n in grupper]
    return {"grupper": ut} if ut else None


def _blokkdata(con, kurs, b: dict, filmeta: dict, dager: list[dict], idag: date, avsluttet: bool, fil_url, sp_url,
               utenfor=frozenset(), med_samling: bool = False) -> dict | None:
    """Ferdig data til malen for én blokk, eller None hvis blokken viser seg tom (filer som mangler, lenker uten adresse ...)."""
    typ, d = b["type"], b["data"]
    if typ == "tekst":
        return {"html": Markup(si.til_visning(d.get("html", "")))}
    if typ == "viktig":
        return {"niva": d.get("niva", "info"), "avsnitt": _avsnitt(d.get("tekst", ""))}
    if typ == "program":
        data = _program_data(b, dager, idag, utenfor, med_samling)
        return data if any(dag["punkter"] for dag in data["dager"]) else None
    if typ == "filer":
        return _filer_data(con, kurs, b, filmeta, dager, idag, fil_url, sp_url, utenfor, med_samling)
    if typ == "lenker":
        return _lenker_data(kurs, b, avsluttet)
    if typ == "tabell":
        rader = [list(r) for r in d.get("rader", []) if any((c or "").strip() for c in r)]
        return {"kolonner": list(d.get("kolonner", [])), "rader": rader} if rader else None
    if typ == "kontakt":
        personer = []
        for p in d.get("personer", []):
            if not any((p.get(f) or "").strip() for f in ("navn", "rolle", "telefon", "epost")):
                continue
            tlf = p.get("telefon", "") if si._TELEFON.match(p.get("telefon", "") or "") else ""      # valideres på nytt ved visning
            epost = p.get("epost", "") if si._EPOST_ENKEL.match(p.get("epost", "") or "") else ""
            personer.append({"navn": p.get("navn", ""), "rolle": p.get("rolle", ""), "initialer": _initialer(p.get("navn", "")),
                             "telefon": tlf, "tel_url": "tel:" + "".join(c for c in tlf if c in "+0123456789") if tlf else "",
                             "epost": epost})
        return {"personer": personer} if personer else None
    if typ == "bilde":
        meta = filmeta.get(d.get("fil_id"))
        if not meta or meta["type"] != "bilde":
            return None
        return {"url": fil_url(meta["id"]), "alt": d.get("alt", ""), "tekst": d.get("tekst", ""),
                "plassering": d.get("plassering", "bred"), "bredde": meta.get("bredde"), "hoyde": meta.get("hoyde")}
    return None


def _samlingsgrupper(dager: list[dict], kursdager: list[dict], idag: date) -> list[dict]:
    """Kursdagene til «Samlinger og kursdager»-kortet, gruppert per samling (bare for kurs med flere samlinger), i datorekkefølge:
    [{"navn", "periode", "tag", "dager"}]. `periode` står bare når samlingen har flere dager (en enkelt dag har sin dato i raden).
    `tag` merker samlingen som pågår («Pågår nå»), ellers den som kommer først («Neste samling»); en ferdig samling har ingen.
    En kursdag uten samling i et kurs med samlinger får gruppen «Øvrige kursdager». `dager` og `kursdager` er parallelle lister."""
    per_samling: dict = {}
    for d, kd in zip(dager, kursdager):
        g = per_samling.setdefault(d["samling_id"], {"navn": d["samling"] or "Øvrige kursdager", "datoer": [], "dager": []})
        g["datoer"].append(date.fromisoformat(d["dato"]))
        g["dager"].append(kd)
    ut = []
    for g in per_samling.values():
        forst, sist = min(g["datoer"]), max(g["datoer"])
        ut.append({"navn": g["navn"], "periode": kursdatoer.datoliste(g["datoer"]) if len(g["datoer"]) > 1 else "",
                   "tag": "Pågår nå" if forst <= idag <= sist else None, "dager": g["dager"], "_forst": forst})
    if ut and not any(g["tag"] for g in ut):
        neste = next((g for g in ut if g["_forst"] > idag), None)
        if neste:
            neste["tag"] = "Neste samling"
    for g in ut:
        del g["_forst"]
    return ut


def _felles_tid(dager: list[dict]) -> str:
    """Klokkeslettet i toppfeltet: det samme på alle kursdagene, ellers «Varierer» (hver dag har sitt eget under Kursdager)."""
    tider = list(dict.fromkeys(_klokkeslett(d["start"], d["slutt"]) for d in dager))
    return tider[0] if len(tider) == 1 else ("Varierer – se kursdagene" if tider else "")


def bygg_visning(con, kurs, dok: dict, *, deltaker_id: int | None, idag: date, forhandsvisning: bool,
                 fil_url: Callable[[int], str], sp_url: Callable[[str], str | None],
                 simuler_kursdag_id: int | None = None, sist_publisert: str | None = None,
                 forh_tekst: str | None = None) -> dict:
    """Malkonteksten (SPEC §7.2) til kursside_deltaker.html. Filtrerer og slår opp alt: malen får bare ferdige, ufarlige tekster
    (rik tekst som Markup fra sideinnhold.til_visning). `min_side_url` og `oppmote_url` legges på av ruten."""
    pid = _paamelding_id(con, deltaker_id, kurs["id"]) if not forhandsvisning else None
    alle_dager = kursdager_for(con, kurs)
    dager = kursdager_for(con, kurs, pid) if pid else alle_dager      # deltakerens egne dager (forhåndsvisningen: alle)
    utenfor = {d["id"] for d in alle_dager} - {d["id"] for d in dager}
    idag = _effektiv_idag(alle_dager, idag, simuler_kursdag_id)
    avsluttet = kurs["status"] == "avsluttet" or bool(dager and date.fromisoformat(dager[-1]["dato"]) < idag)
    filmeta = {f["id"]: f for f in sidelager.fil_liste(con, kurs["id"])}
    side = sidelager.hent(con, kurs["id"])
    dagens = _dagens_kursdag(dager, idag)
    samlinger = list(dict.fromkeys(d["samling_id"] for d in alle_dager if d["samling_id"] is not None))   # kursets eget nummer
    med_samling = len(samlinger) > 1        # kurs med flere samlinger: dagene, programmet og filene står under sin samling

    blokker: list[dict] = []
    topp_bilde = None
    for b in si.synlige_blokker(dok, idag):
        try:
            data = _blokkdata(con, kurs, b, filmeta, dager, idag, avsluttet, fil_url, sp_url, utenfor, med_samling)
        except (TypeError, AttributeError, KeyError, ValueError, IndexError):     # innhold som er endret utenom siden: blokken vises ikke
            data = None
        if data is None:
            continue
        if b["type"] == "bilde" and data["plassering"] == "topp" and topp_bilde is None:
            topp_bilde = {"url": data["url"], "alt": data["alt"], "tekst": data["tekst"]}
            continue
        if b["type"] == "bilde" and data["plassering"] == "topp":
            data["plassering"] = "bred"
        blokker.append({"id": b["id"], "type": b["type"], "tittel": b["tittel"] or si.STANDARDTITLER.get(b["type"], ""),
                        "i_meny": bool(b.get("i_meny", True)), "data": data})

    oppmotte = {int(r["kursdag_id"]) for r in con.execute("SELECT kursdag_id FROM oppmote WHERE paamelding_id=?", (pid,))} if pid else set()
    kursdager = []
    for i, d in enumerate(dager, 1):
        dato = date.fromisoformat(d["dato"])
        status = "mott" if d["id"] in oppmotte else ("ikke" if dato <= idag else "kommer")
        kursdager.append({"nr": i, "dato_lang": dato_lang(dato), "dato_kort": dato_kort(dato),
                          "tid": _klokkeslett(d["start"], d["slutt"]), "status": status, "i_dag": dato == idag,
                          "samling": d["samling"] if med_samling else None, "merknad": d["merknad"]})

    i_dag = None
    if (dagens or _dagens_kursdag(alle_dager, idag)) and not avsluttet:
        i_dag = oppmote_i_dag(con, None if forhandsvisning else deltaker_id, kurs, side, idag, simuler_kursdag_id=simuler_kursdag_id)
        if i_dag is not None and forhandsvisning:
            i_dag.update(tilbys=False, registrert=None, forklaring="Innsjekk er avslått i forhåndsvisningen.")
        if i_dag is not None and med_samling and dagens:
            i_dag["samling"] = dagens["samling"]
    neste = next((d for d in dager if date.fromisoformat(d["dato"]) > idag), None)
    neste_dag = None
    if neste and not dagens and not avsluttet:
        neste_dag = {"dato_lang": dato_lang(date.fromisoformat(neste["dato"])), "dag_nr": dager.index(neste) + 1, "av": len(dager),
                     "tid": _klokkeslett(neste["start"], neste["slutt"]), "samling": neste["samling"] if med_samling else None}

    datoer = [date.fromisoformat(d["dato"]) for d in dager]
    pillene = []        # (Ingen «Samling N av M» her: alle samlingene står under Samlinger og kursdager, programmet og filene)
    if deltaker_id is not None and not forhandsvisning:
        pillene.append({"tekst": "Du er påmeldt", "stil": "ok"})
    if avsluttet:
        pillene.append({"tekst": "Kurset er avsluttet", "stil": "glass"})
    elif dagens:
        pillene.append({"tekst": f"Dag {dager.index(dagens) + 1} av {len(dager)}", "stil": "glass"})
    melding = dok.get("melding") or {}
    return {
        "kurs": {"id": kurs["id"], "kode": kurs["kode"], "navn": kurs["navn"], "tittel": dok.get("tittel") or kurs["navn"],
                 "ingress": dok.get("ingress", ""), "type": kurs["type"],
                 "type_tekst": {"fysisk": "Fysisk kurs", "digital": "Nettkurs (Zoom)", "hybrid": "Hybrid (fysisk og Zoom)"}.get(kurs["type"], ""),
                 "sted": kurs["sted"] or "", "rom": dok.get("rom", ""), "periode": kursdatoer.datoliste(datoer) if datoer else "",
                 "tid": _felles_tid(dager),
                 "kart_url": ("https://www.openstreetmap.org/search?query=" + quote(kurs["sted"])) if kurs["sted"] and kurs["type"] != "digital" else None},
        "forhandsvisning": forhandsvisning, "forh_tekst": forh_tekst or ("Forhåndsvisning – deltakerne ser ikke dette" if forhandsvisning else None),
        "pillene": pillene,
        "melding": {"tekst": melding["tekst"], "niva": melding.get("niva", "info")} if melding.get("tekst") else None,
        "topp_bilde": topp_bilde, "i_dag": i_dag, "neste_dag": neste_dag, "avsluttet": avsluttet, "kursdager": kursdager,
        "samlinger": _samlingsgrupper(dager, kursdager, idag) if med_samling else [],
        "apen_tekst": apen_tekst(sidelager.effektiv_stenges(con, kurs["id"], side)),
        "blokker": blokker, "meny": [{"id": b["id"], "tittel": b["tittel"], "type": b["type"]} for b in blokker if b["i_meny"]],
        "sist_oppdatert": _sist_oppdatert(sist_publisert), "min_side_url": "", "oppmote_url": "", "csrf": True,
    }


def _sist_oppdatert(utc: str | None) -> str | None:
    tid = norsk_tid.fra_utc(utc) if utc else None
    return f"{dato_lang(tid.date(), aar=True)} kl. {norsk_tid.klokkeslett(tid)}" if tid else None
