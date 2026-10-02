"""Kursside: SQL-laget (lagring av utkast og publisert side, versjoner, filer). Kurssidens innhold og regler står i
kurs/sideinnhold.py; tilgang og visning i kurs/deltakerside.py.

Regler (SPEC §4):
  * Funksjonene her committer ALDRI (som øvrige db.*). Ruten committer, og operasjoner i flere steg (publiser, gjenopprett, mal,
    hent fra annet kurs) kalles inne i `with db.transaksjon(con):`.
  * Versjonskontroll: utkastet har et versjonsnummer. Lagring krever at kalleren kjenner riktig versjon
    (`UPDATE ... WHERE versjon=?`); ved konflikt returneres None og ingenting overskrives i det stille.
  * Aldri `SELECT *` mot kursside_fil: filinnholdet (base64) ligger i kursside_fil_innhold og hentes bare av `fil_innhold`.
  * Portabel SQL (SQLite og PostgreSQL): db.sett_inn, ON CONFLICT, ingen INSERT ... SELECT med parametere.
  * Alle tidspunkter lagres i UTC (db.naa_utc).
"""
import base64
import hashlib
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone

from . import config, db, signaturer
from . import sideinnhold as si
from .sideinnhold import Sidefeil

REDIGERER_SEKUNDER = 90                 # «noen redigerer nå» gjelder så lenge etter siste hjerteslag
MAKS_PUBLISERTE_VERSJONER = 30
MAKS_SIKKERHETSKOPIER = 20
MAKS_BILDE_PIKSLER = 6000
# Åpningstid etter kurset: antall dager etter siste kursdag siden er åpen for deltakerne (kursside.apen_dager)
APEN_DAGER_MIN, APEN_DAGER_MAKS = 1, 3650
UBEGRENSET = 0            # verdien i kursside.apen_dager for «Ingen tidsbegrensning»
STANDARD = ""             # sett_innstillinger(apen_dager=STANDARD): tilbake til standard (kolonnen settes til NULL)
_MB = 1024 * 1024
_TIDSFORMAT = "%Y-%m-%d %H:%M:%S"

# Tillatte filtyper: endelse -> (type, mimetype, innholdssjekk). Aldri SVG, HTML, skript, kjørbare filer eller makrofiler.
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_LIK = ("pptx", "docx", "xlsx", "odp", "odt", "ods", "key", "zip")
_OLE_LIK = ("ppt", "doc", "xls")
_BILDE_ENDELSER = ("png", "jpg", "jpeg", "gif", "webp")
_MIMETYPER = {
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "odp": "application/vnd.oasis.opendocument.presentation", "odt": "application/vnd.oasis.opendocument.text",
    "ods": "application/vnd.oasis.opendocument.spreadsheet", "key": "application/vnd.apple.keynote",
    "zip": "application/zip", "ppt": "application/vnd.ms-powerpoint", "doc": "application/msword",
    "xls": "application/vnd.ms-excel", "txt": "text/plain",
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp",
}
TILLATTE_ENDELSER = ("pdf", *_ZIP_LIK, *_OLE_LIK, "txt", *_BILDE_ENDELSER)
FILTYPE_TEKST = "PDF, PowerPoint, Word, Excel, ZIP, tekstfiler og bilder (PNG, JPEG, GIF, WEBP)"


def _utc(tid: datetime) -> str:
    return tid.astimezone(timezone.utc).strftime(_TIDSFORMAT)


def _naa() -> datetime:
    return datetime.now(timezone.utc)


def _dict(rad) -> dict:
    return {k: rad[k] for k in rad.keys()}


# ============================ siden ============================

def hent(con, kurs_id: int):
    """kursside-raden (utkast og publisert er JSON-tekst: bruk sideinnhold.les), eller None."""
    return con.execute("SELECT * FROM kursside WHERE kurs_id=?", (kurs_id,)).fetchone()


def hent_utkast(con, kurs_id: int) -> tuple[dict, int]:
    """(dokument, versjon). Ingen rad ennå: (tomt dokument, 0)."""
    rad = con.execute("SELECT utkast, versjon FROM kursside WHERE kurs_id=?", (kurs_id,)).fetchone()
    return (si.les(rad["utkast"]), int(rad["versjon"])) if rad else (si.tomt_dokument(), 0)


def hent_publisert(con, kurs_id: int) -> dict | None:
    rad = con.execute("SELECT publisert FROM kursside WHERE kurs_id=?", (kurs_id,)).fetchone()
    return si.les(rad["publisert"]) if rad and rad["publisert"] else None


def valideringsgrunnlag(con, kurs_id: int) -> tuple[dict[int, dict], set[int]]:
    """(fil_ider, kursdag_ider) til sideinnhold.valider: kursets filer ({id: {"type": ...}}) og kursets kursdager."""
    filer = {f["id"]: {"type": f["type"]} for f in fil_liste(con, kurs_id)}
    dager = {int(r["id"]) for r in con.execute("SELECT id FROM kursdag WHERE kurs_id=?", (kurs_id,))}
    return filer, dager


def lagre_utkast(con, kurs_id: int, dok: dict, forventet_versjon: int, aktor: str) -> int | None:
    """Lagrer utkastet hvis kalleren kjenner riktig versjon (0 = ingen rad ennå). Returnerer ny versjon, eller None ved
    konflikt (noen andre har lagret, eller raden finnes allerede). Skriver også «noen redigerer nå»-markeringen."""
    json_tekst = si.til_json(dok)
    naa = _naa()
    til = _utc(naa + timedelta(seconds=REDIGERER_SEKUNDER))
    if forventet_versjon == 0:
        cur = con.execute(
            """INSERT INTO kursside (kurs_id, utkast, versjon, endret, endret_av, redigerer_av, redigerer_til)
               VALUES (?,?,1,?,?,?,?) ON CONFLICT (kurs_id) DO NOTHING""",
            (kurs_id, json_tekst, _utc(naa), aktor, aktor, til))
        return 1 if cur.rowcount == 1 else None
    cur = con.execute(
        """UPDATE kursside SET utkast=?, versjon=versjon+1, endret=?, endret_av=?, redigerer_av=?, redigerer_til=?
           WHERE kurs_id=? AND versjon=?""",
        (json_tekst, _utc(naa), aktor, aktor, til, kurs_id, forventet_versjon))
    return forventet_versjon + 1 if cur.rowcount == 1 else None


def sikkerhetskopi(con, kurs_id: int, aktor: str, merknad: str) -> None:
    """Tar vare på dagens utkast (arsak «sikkerhetskopi») før noe destruktivt. Gjør ingenting hvis siden ikke finnes ennå."""
    rad = con.execute("SELECT utkast, versjon FROM kursside WHERE kurs_id=?", (kurs_id,)).fetchone()
    if not rad:
        return
    con.execute("""INSERT INTO kursside_versjon (kurs_id, innhold, arsak, merknad, versjon, opprettet, opprettet_av)
                   VALUES (?,?,'sikkerhetskopi',?,?,?,?)""",
                (kurs_id, rad["utkast"], merknad, rad["versjon"], db.naa_utc(), aktor))
    _beskjaer(con, kurs_id)


def erstatt_utkast(con, kurs_id: int, dok: dict, forventet_versjon: int, aktor: str, merknad: str) -> int | None:
    """Som lagre_utkast, men tar sikkerhetskopi av dagens utkast FØRST. Brukes av mal, hent fra annet kurs og gjenoppretting.
    Ved versjonskonflikt gjøres ingenting (heller ingen sikkerhetskopi)."""
    if forventet_versjon:
        rad = con.execute("SELECT versjon FROM kursside WHERE kurs_id=?", (kurs_id,)).fetchone()
        if not rad or int(rad["versjon"]) != forventet_versjon:
            return None
        sikkerhetskopi(con, kurs_id, aktor, merknad)
    return lagre_utkast(con, kurs_id, dok, forventet_versjon, aktor)


def publiser(con, kurs_id: int, forventet_versjon: int, aktor: str) -> int | None:
    """Gjør utkastet til den publiserte siden. Krever at siden finnes og at `forventet_versjon` er utkastets versjon (ellers
    None). Tar vare på den publiserte versjonen (arsak «publisert»), beskjærer gamle versjoner (siste 30 + siste 20 sikkerhetskopier)
    og rydder bort filer som ingen bruker lenger. Returnerer publisert versjon."""
    rad = con.execute("SELECT utkast, versjon FROM kursside WHERE kurs_id=?", (kurs_id,)).fetchone()
    if not rad or int(rad["versjon"]) != forventet_versjon:
        return None
    naa = db.naa_utc()
    cur = con.execute(
        """UPDATE kursside SET publisert=utkast, publisert_versjon=versjon, publisert_tid=?, publisert_av=?
           WHERE kurs_id=? AND versjon=?""", (naa, aktor, kurs_id, forventet_versjon))
    if cur.rowcount != 1:
        return None
    con.execute("""INSERT INTO kursside_versjon (kurs_id, innhold, arsak, merknad, versjon, opprettet, opprettet_av)
                   VALUES (?,?,'publisert',NULL,?,?,?)""", (kurs_id, rad["utkast"], forventet_versjon, naa, aktor))
    _beskjaer(con, kurs_id)
    rydd_foreldrelose_filer(con, kurs_id)
    return forventet_versjon


def _beskjaer(con, kurs_id: int) -> None:
    for arsak, maks in (("publisert", MAKS_PUBLISERTE_VERSJONER), ("sikkerhetskopi", MAKS_SIKKERHETSKOPIER)):
        ider = [int(r["id"]) for r in con.execute(
            "SELECT id FROM kursside_versjon WHERE kurs_id=? AND arsak=? ORDER BY id DESC", (kurs_id, arsak))]
        gamle = ider[maks:]
        if gamle:
            con.execute(f"DELETE FROM kursside_versjon WHERE id IN ({','.join('?' * len(gamle))})", gamle)


def sett_aktiv(con, kurs_id: int, aktiv: bool, aktor: str) -> bool:
    """Tar siden ned for deltakerne (nødbrems) eller åpner den igjen. False hvis siden ikke finnes ennå."""
    return con.execute("UPDATE kursside SET aktiv=? WHERE kurs_id=?", (1 if aktiv else 0, kurs_id)).rowcount == 1


def sett_innstillinger(con, kurs_id: int, *, innsjekk_krever_kode: bool | None = None, stenges: str | None = None,
                       apen_dager: int | str | None = None, aktor: str) -> bool:
    """None = uendret. Åpningstiden etter kurset kan settes på to måter, og bare én gjelder om gangen (den som settes sist fjerner den andre):
      * `stenges`: en bestemt dato (ISO). «» fjerner datoen.
      * `apen_dager`: så mange dager etter siste kursdag (1-3650), UBEGRENSET (0) = ingen tidsbegrensning, STANDARD («») = tilbake til
        standard (KURSSIDE_ETTERTILGANG_DAGER).
    En dato og et antall dager samtidig gir Sidefeil. False hvis siden ikke finnes ennå."""
    deler, params = [], []
    if innsjekk_krever_kode is not None:
        deler.append("innsjekk_krever_kode=?")
        params.append(1 if innsjekk_krever_kode else 0)
    sett_dato = stenges is not None and stenges != ""
    sett_dager = apen_dager is not None and not (isinstance(apen_dager, str) and apen_dager == STANDARD)
    if sett_dato and sett_dager:
        raise Sidefeil("Velg enten en bestemt dato eller et antall dager, ikke begge deler.")
    if stenges is not None:
        if stenges == "":
            deler.append("stenges=NULL")
        else:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", stenges):
                raise Sidefeil("«Åpen til» må være en gyldig dato.")
            try:
                date.fromisoformat(stenges)
            except ValueError:
                raise Sidefeil("«Åpen til» må være en gyldig dato.") from None
            deler.append("stenges=?")
            params.append(stenges)
            if apen_dager is None:
                deler.append("apen_dager=NULL")                                  # en bestemt dato erstatter dager
    if apen_dager is not None:
        if not sett_dager:
            deler.append("apen_dager=NULL")
        else:
            if isinstance(apen_dager, bool) or not isinstance(apen_dager, int) or not UBEGRENSET <= apen_dager <= APEN_DAGER_MAKS:
                raise Sidefeil(f"Antall dager må være et helt tall fra {APEN_DAGER_MIN} til {APEN_DAGER_MAKS}, eller «Ingen tidsbegrensning».")
            deler.append("apen_dager=?")
            params.append(apen_dager)
            if stenges is None:
                deler.append("stenges=NULL")                                     # dager erstatter en bestemt dato
    if not deler:
        return hent(con, kurs_id) is not None
    return con.execute(f"UPDATE kursside SET {', '.join(deler)} WHERE kurs_id=?", (*params, kurs_id)).rowcount == 1


def siste_kursdag(con, kurs_id: int) -> date | None:
    rad = con.execute("SELECT MAX(dato) AS siste FROM kursdag WHERE kurs_id=?", (kurs_id,)).fetchone()
    return date.fromisoformat(rad["siste"]) if rad and rad["siste"] else None


def _felt(side, navn: str):
    """Verdien i en kolonne i kursside-raden, eller None (ingen rad, eller en rad uten kolonnen)."""
    if side is None:
        return None
    try:
        return side[navn]
    except (KeyError, IndexError):
        return None


def gyldig_apen_dager(verdi) -> int | None:
    """Lagret verdi -> None (bruk standard), UBEGRENSET (0) eller 1-3650. Alt annet (tekst, desimaltall, ja/nei, utenfor grensene) gir
    None: en ødelagt lagret verdi skal aldri gi feil på siden, bare standard åpningstid."""
    if isinstance(verdi, bool) or not isinstance(verdi, int) or not UBEGRENSET <= verdi <= APEN_DAGER_MAKS:
        return None
    return verdi


def _bestemt_dato(verdi) -> date | None:
    """Den lagrede «Åpen til»-datoen, eller None (ingen, eller ikke en gyldig dato: da brukes dager/standard, ikke en feilside)."""
    try:
        return date.fromisoformat(verdi) if isinstance(verdi, str) and verdi else None
    except ValueError:
        return None


def standard_dager() -> int:
    """Standard åpningstid i dager (KURSSIDE_ETTERTILGANG_DAGER, 180), holdt innenfor 1-3650 så en feil i .env aldri velter siden."""
    return min(max(int(config.KURSSIDE_ETTERTILGANG_DAGER), APEN_DAGER_MIN), APEN_DAGER_MAKS)


def effektiv_stenges(con, kurs_id: int, side) -> date | None:
    """Siste dag siden er åpen for deltakerne. Rekkefølge: en bestemt «Åpen til»-dato hvis den er satt; ellers kursets egne dager etter
    siste kursdag (kursside.apen_dager), der UBEGRENSET gir None (aldri stengt); ellers siste kursdag + standard (180 dager).
    None også når kurset ikke har kursdager og ingen dato er satt: da er siden aldri stengt."""
    dato = _bestemt_dato(_felt(side, "stenges"))
    if dato is not None:
        return dato
    dager = gyldig_apen_dager(_felt(side, "apen_dager"))
    if dager == UBEGRENSET:
        return None
    siste = siste_kursdag(con, kurs_id)
    return siste + timedelta(days=dager or standard_dager()) if siste else None


def apningstid(con, kurs_id: int, side) -> dict:
    """Åpningstiden slik redigeringsvisningen viser den: modus («standard», «dager», «ubegrenset» eller «dato»), lagret dato og lagrede dager
    (bare de som gjelder), standardverdien, siste kursdag og den utregnede siste åpne dagen (ISO-tekst eller None)."""
    dato = _bestemt_dato(_felt(side, "stenges"))
    dager = gyldig_apen_dager(_felt(side, "apen_dager"))
    modus = "dato" if dato else "ubegrenset" if dager == UBEGRENSET else "dager" if dager else "standard"
    siste = siste_kursdag(con, kurs_id)
    effektiv = effektiv_stenges(con, kurs_id, side)
    return {"modus": modus, "stenges": dato.isoformat() if dato else None, "apen_dager": None if dato else dager,
            "standard_dager": standard_dager(), "siste_kursdag": siste.isoformat() if siste else None,
            "stenges_effektiv": effektiv.isoformat() if effektiv else None}


# ============================ versjoner ============================

def versjoner(con, kurs_id: int, grense: int = 20) -> list[dict]:
    """Tidligere versjoner, nyeste først: id, arsak, merknad, opprettet (UTC), opprettet_av, versjon. Aldri innholdet."""
    return [_dict(r) for r in con.execute(
        """SELECT id, arsak, merknad, versjon, opprettet, opprettet_av FROM kursside_versjon
           WHERE kurs_id=? ORDER BY id DESC LIMIT ?""", (kurs_id, grense))]


def hent_versjon(con, kurs_id: int, versjon_id: int):
    return con.execute("SELECT * FROM kursside_versjon WHERE id=? AND kurs_id=?", (versjon_id, kurs_id)).fetchone()


def gjenopprett(con, kurs_id: int, versjon_id: int, forventet_versjon: int, aktor: str) -> tuple[int, dict, list[str]] | None:
    """Legger en tidligere versjon inn som utkast (publiserer IKKE). Dagens utkast tas vare på først. Innholdet valideres på nytt
    mot kursets filer og kursdager (en fil som er slettet, fjernes med en merknad). Returnerer (ny versjon, dokument, merknader),
    eller None ved versjonskonflikt. Ukjent versjon: Sidefeil(404)."""
    v = hent_versjon(con, kurs_id, versjon_id)
    if not v:
        raise Sidefeil("Versjonen finnes ikke.", 404)
    filer, dager = valideringsgrunnlag(con, kurs_id)
    dok, merknader = si.valider(si.les(v["innhold"]), fil_ider=filer, kursdag_ider=dager)
    ny = erstatt_utkast(con, kurs_id, dok, forventet_versjon, aktor, "Før gjenoppretting")
    return (ny, dok, merknader) if ny is not None else None


# ============================ «noen redigerer nå» ============================

def sett_redigerer(con, kurs_id: int, aktor: str, sekunder: int = REDIGERER_SEKUNDER) -> None:
    con.execute("UPDATE kursside SET redigerer_av=?, redigerer_til=? WHERE kurs_id=?",
                (aktor, _utc(_naa() + timedelta(seconds=sekunder)), kurs_id))


def hent_redigerer(con, kurs_id: int, utenom_aktor: str) -> dict | None:
    """{"navn": ..., "sekunder_siden": ...} hvis en ANNEN aktør har en gyldig markering (siste hjerteslag for under 90 sekunder siden)."""
    rad = con.execute("SELECT redigerer_av, redigerer_til FROM kursside WHERE kurs_id=?", (kurs_id,)).fetchone()
    if not rad or not rad["redigerer_av"] or rad["redigerer_av"] == utenom_aktor or not rad["redigerer_til"]:
        return None
    if not rad["redigerer_av"].startswith("admin:"):          # systemet (demodata, morgenjobben) er aldri «noen som redigerer»
        return None
    naa = db.naa_utc()
    if rad["redigerer_til"] <= naa:
        return None
    brukernavn = rad["redigerer_av"].split(":", 1)[-1]
    navn = con.execute("SELECT navn FROM admin_bruker WHERE brukernavn=?", (brukernavn,)).fetchone()
    til = datetime.strptime(rad["redigerer_til"], _TIDSFORMAT)
    siden = max(0, int((datetime.strptime(naa, _TIDSFORMAT) - (til - timedelta(seconds=REDIGERER_SEKUNDER))).total_seconds()))
    return {"navn": navn["navn"] if navn else brukernavn, "sekunder_siden": siden}


# ============================ filer ============================

def _visningsnavn(navn: str) -> str:
    """Visningsnavn: æøå og mellomrom beholdes; sti, kontrolltegn og tegn som ikke er tillatt i filnavn byttes ut; maks 120 tegn
    (endelsen beholdes). Aldri tomt."""
    ren = unicodedata.normalize("NFC", navn or "")
    ren = re.sub(r"[\x00-\x1f\x7f\\/:*?\"<>|]+", "_", ren).strip(" .")
    if len(ren) > 120:
        stamme, punktum, endelse = ren.rpartition(".")
        ren = (stamme[:120 - len(endelse) - 1] + "." + endelse) if punktum and len(endelse) <= 10 else ren[:120]
    return ren or "fil"


def _endelse(navn: str) -> str:
    return navn.rsplit(".", 1)[-1].lower() if "." in navn else ""


def _webp_maal(data: bytes) -> tuple[int | None, int | None]:
    """Bredde og høyde for WEBP (kan mangle for uvanlige varianter)."""
    try:
        kind = data[12:16]
        if kind == b"VP8X" and len(data) >= 30:
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
        if kind == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF
        if kind == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    except (IndexError, ValueError):
        pass
    return None, None


def for_stor_tekst(maks_mb: int) -> str:
    """Rådet når en fil er for stor: noe administratoren faktisk kan gjøre (komprimere bildene, eller bruke kursmappen i SharePoint)."""
    return (f"Filen er for stor (maks {maks_mb} MB). Prøv å komprimere bildene i filen (i PowerPoint: Fil › Lagre som › Verktøy › Komprimer bilder), "
            "eller legg filen i kursmappen i SharePoint og velg «Vis også filene kursholder har lastet opp» i Filer-blokken.")


def kontroller_fil(navn: str, data: bytes) -> dict:
    """Kontrollerer en fil ut fra endelsen OG innholdet (aldri klientens Content-Type). Returnerer type, mimetype, bredde og
    høyde. Sidefeil(415) for filtype som ikke er tillatt eller innhold som ikke passer, Sidefeil(413) for for stor."""
    endelse = _endelse(navn)
    if endelse not in TILLATTE_ENDELSER:
        raise Sidefeil(f"Filtypen «.{endelse}» er ikke tillatt. Du kan laste opp {FILTYPE_TEKST}." if endelse
                       else f"Filen mangler filendelse. Du kan laste opp {FILTYPE_TEKST}.", 415)
    er_bilde = endelse in _BILDE_ENDELSER
    maks_mb = config.KURSSIDE_BILDE_MAKS_MB if er_bilde else config.KURSSIDE_FIL_MAKS_MB
    if len(data) > maks_mb * _MB:
        raise Sidefeil(for_stor_tekst(maks_mb), 413)
    passer = True
    bredde = hoyde = None
    if endelse == "pdf":
        passer = b"%PDF-" in data[:1024]
    elif endelse in _ZIP_LIK:
        passer = data[:4] == b"PK\x03\x04"
    elif endelse in _OLE_LIK:
        passer = data[:8] == _OLE
    elif endelse == "txt":
        passer = b"\x00" not in data
        if passer:
            for kode in ("utf-8", "cp1252"):
                try:
                    data.decode(kode)
                    break
                except UnicodeDecodeError:
                    passer = kode != "cp1252"
    elif endelse == "webp":
        passer = data[:4] == b"RIFF" and data[8:12] == b"WEBP"
        if passer:
            bredde, hoyde = _webp_maal(data)
    else:                                            # png, jpg, jpeg, gif
        try:
            mime, bredde, hoyde = signaturer.les_bilde(data)
        except signaturer.Signaturfeil:
            passer = False
        else:
            passer = (mime == "image/png") == (endelse == "png") and (mime == "image/gif") == (endelse == "gif") \
                and (mime == "image/jpeg") == (endelse in ("jpg", "jpeg"))
    if not passer:
        raise Sidefeil(f"Innholdet i filen passer ikke med filendelsen «.{endelse}». Filen ble ikke lastet opp.", 415)
    if er_bilde and bredde is not None and not (0 < bredde <= MAKS_BILDE_PIKSLER and 0 < hoyde <= MAKS_BILDE_PIKSLER):
        raise Sidefeil(f"Bildet er for stort eller har ugyldig størrelse (maks {MAKS_BILDE_PIKSLER} piksler).", 413)
    return {"type": "bilde" if er_bilde else "dokument", "mimetype": _MIMETYPER[endelse], "bredde": bredde, "hoyde": hoyde}


def kvote(con, kurs_id: int) -> dict:
    r = con.execute("SELECT COUNT(*) AS antall, COALESCE(SUM(storrelse), 0) AS bytes FROM kursside_fil WHERE kurs_id=?",
                    (kurs_id,)).fetchone()
    return {"bytes": int(r["bytes"]), "antall": int(r["antall"]),
            "maks_bytes": config.KURSSIDE_KURS_MAKS_MB * _MB, "maks_antall": config.KURSSIDE_MAKS_FILER}


def _sikre_kvote(con, kurs_id: int, ny_storrelse: int) -> None:
    k = kvote(con, kurs_id)
    if k["antall"] >= k["maks_antall"]:
        raise Sidefeil(f"Min side har nådd grensen på {k['maks_antall']} filer. Slett filer som ikke brukes.", 409)
    if k["bytes"] + ny_storrelse > k["maks_bytes"]:
        raise Sidefeil(f"Min side har nådd grensen på {config.KURSSIDE_KURS_MAKS_MB} MB. Slett filer som ikke brukes.", 409)


def _fil_ved_sha(con, kurs_id: int, sha: str) -> int | None:
    rad = con.execute("SELECT id FROM kursside_fil WHERE kurs_id=? AND sha256=?", (kurs_id, sha)).fetchone()
    return int(rad["id"]) if rad else None


def _sett_inn_fil(con, kurs_id: int, navn: str, meta: dict, storrelse: int, sha: str, b64: str, aktor: str) -> tuple[int, bool]:
    rader = con.execute(
        """INSERT INTO kursside_fil (kurs_id, filnavn, type, mimetype, storrelse, sha256, bredde, hoyde, opprettet, opprettet_av)
           VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT (kurs_id, sha256) DO NOTHING RETURNING id""",
        (kurs_id, navn, meta["type"], meta["mimetype"], storrelse, sha, meta["bredde"], meta["hoyde"], db.naa_utc(), aktor)).fetchall()
    if not rader:                                    # noen andre rakk det samme innholdet først
        return _fil_ved_sha(con, kurs_id, sha), True
    fil_id = int(rader[0]["id"])
    con.execute("INSERT INTO kursside_fil_innhold (fil_id, innhold) VALUES (?,?)", (fil_id, b64))
    return fil_id, False


def lagre_fil(con, kurs_id: int, filnavn: str, data: bytes, aktor: str) -> tuple[int, bool]:
    """Lagrer en fil på kurssiden: (fil_id, duplikat). Samme innhold to ganger i samme kurs blir én rad (duplikat=True).
    Kaster Sidefeil: 400 tom fil, 413 for stor, 415 filtype eller innhold som ikke er tillatt, 409 kvote."""
    if not data:
        raise Sidefeil("Filen er tom.", 400)
    navn = _visningsnavn(filnavn)
    meta = kontroller_fil(navn, data)
    sha = hashlib.sha256(data).hexdigest()
    eksisterende = _fil_ved_sha(con, kurs_id, sha)
    if eksisterende is not None:
        return eksisterende, True
    _sikre_kvote(con, kurs_id, len(data))
    return _sett_inn_fil(con, kurs_id, navn, meta, len(data), sha, base64.b64encode(data).decode("ascii"), aktor)


_FIL_KOLONNER = "id, filnavn, type, mimetype, storrelse, bredde, hoyde, opprettet, opprettet_av"


def fil_liste(con, kurs_id: int) -> list[dict]:
    """Kursets filer (aldri innholdet)."""
    return [_dict(r) for r in con.execute(f"SELECT {_FIL_KOLONNER} FROM kursside_fil WHERE kurs_id=? ORDER BY id", (kurs_id,))]


def fil_meta(con, kurs_id: int, fil_id: int) -> dict | None:
    r = con.execute(f"SELECT {_FIL_KOLONNER} FROM kursside_fil WHERE id=? AND kurs_id=?", (fil_id, kurs_id)).fetchone()
    return _dict(r) if r else None


def fil_innhold(con, kurs_id: int, fil_id: int) -> tuple[dict, bytes] | None:
    """(metadata, innhold) for en fil i DETTE kurset, ellers None."""
    r = con.execute(
        """SELECT f.id, f.filnavn, f.type, f.mimetype, f.storrelse, f.bredde, f.hoyde, f.opprettet, f.opprettet_av, c.innhold
           FROM kursside_fil f JOIN kursside_fil_innhold c ON c.fil_id=f.id WHERE f.id=? AND f.kurs_id=?""",
        (fil_id, kurs_id)).fetchone()
    if not r:
        return None
    meta = _dict(r)
    return {k: v for k, v in meta.items() if k != "innhold"}, base64.b64decode(meta["innhold"])


def slett_fil(con, kurs_id: int, fil_id: int, aktor: str) -> str | None:
    """Sletter en fil for godt. None = slettet. Brukes filen i utkastet eller den publiserte siden, gjøres ingenting og svaret er
    teksten «Brukes i blokken «X»»."""
    if fil_meta(con, kurs_id, fil_id) is None:
        return "Filen finnes ikke."
    rad = con.execute("SELECT utkast, publisert FROM kursside WHERE kurs_id=?", (kurs_id,)).fetchone()
    if rad:
        for json_tekst in (rad["utkast"], rad["publisert"]):
            if json_tekst:
                bruker = si.blokk_referanser(si.les(json_tekst)).get(fil_id)
                if bruker is not None:
                    return f"Brukes i blokken «{bruker}»"
    con.execute("DELETE FROM kursside_fil_innhold WHERE fil_id=?", (fil_id,))
    con.execute("DELETE FROM kursside_fil WHERE id=? AND kurs_id=?", (fil_id, kurs_id))
    return None


def kopier_fil(con, fra_kurs_id: int, til_kurs_id: int, fil_id: int, aktor: str) -> int | None:
    """Kopierer en fil til et annet kurs. Har målkurset allerede samme innhold, brukes den raden. None hvis kilden mangler."""
    r = con.execute(
        """SELECT f.filnavn, f.type, f.mimetype, f.storrelse, f.sha256, f.bredde, f.hoyde, c.innhold
           FROM kursside_fil f JOIN kursside_fil_innhold c ON c.fil_id=f.id WHERE f.id=? AND f.kurs_id=?""",
        (fil_id, fra_kurs_id)).fetchone()
    if not r:
        return None
    eksisterende = _fil_ved_sha(con, til_kurs_id, r["sha256"])
    if eksisterende is not None:
        return eksisterende
    _sikre_kvote(con, til_kurs_id, int(r["storrelse"]))
    meta = {"type": r["type"], "mimetype": r["mimetype"], "bredde": r["bredde"], "hoyde": r["hoyde"]}
    return _sett_inn_fil(con, til_kurs_id, r["filnavn"], meta, int(r["storrelse"]), r["sha256"], r["innhold"], aktor)[0]


def rydd_foreldrelose_filer(con, kurs_id: int, eldre_enn_dager: int = 30) -> int:
    """Sletter filer som ikke er referert i utkastet, den publiserte siden eller noen tidligere versjon OG er eldre enn grensen.
    Idempotent. Returnerer antall slettet."""
    grense = _utc(_naa() - timedelta(days=eldre_enn_dager))
    kandidater = {int(r["id"]) for r in con.execute(
        "SELECT id FROM kursside_fil WHERE kurs_id=? AND opprettet < ?", (kurs_id, grense))}
    if not kandidater:
        return 0
    rad = con.execute("SELECT utkast, publisert FROM kursside WHERE kurs_id=?", (kurs_id,)).fetchone()
    for json_tekst in (rad["utkast"], rad["publisert"]) if rad else ():
        if json_tekst:
            kandidater -= si.fil_ider_i(si.les(json_tekst))
    if kandidater:
        for v in con.execute("SELECT innhold FROM kursside_versjon WHERE kurs_id=?", (kurs_id,)):
            kandidater -= si.fil_ider_i(si.les(v["innhold"]))
            if not kandidater:
                break
    for fil_id in kandidater:
        con.execute("DELETE FROM kursside_fil_innhold WHERE fil_id=?", (fil_id,))
        con.execute("DELETE FROM kursside_fil WHERE id=? AND kurs_id=?", (fil_id, kurs_id))
    return len(kandidater)


# ============================ andre kurs, deltakere ============================

def kurs_med_side(con, utenom_kurs_id: int) -> list[dict]:
    """Kurs som har en kursside (til «Hent fra annet kurs»): id, navn, kursnr og første kursdag."""
    return [_dict(r) for r in con.execute(
        """SELECT k.id, k.navn, k.kursnr, MIN(kd.dato) AS start
           FROM kurs k JOIN kursside s ON s.kurs_id=k.id LEFT JOIN kursdag kd ON kd.kurs_id=k.id
           WHERE k.id<>? GROUP BY k.id, k.navn, k.kursnr ORDER BY MIN(kd.dato) DESC, k.id DESC""", (utenom_kurs_id,))]


def bekreftede_epost(con, kurs_id: int) -> set[str]:
    """Små bokstaver. Til publiseringskontrollen (en deltakers e-postadresse skal aldri stå på siden)."""
    return {r["epost"].lower() for r in con.execute(
        """SELECT d.epost FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id
           WHERE p.kurs_id=? AND p.status='bekreftet'""", (kurs_id,))}


def antall_bekreftede(con, kurs_id: int) -> int:
    return int(con.execute("SELECT COUNT(*) FROM paamelding WHERE kurs_id=? AND status='bekreftet'", (kurs_id,)).fetchone()[0])


def deltakernavn(con, kurs_id: int) -> list[str]:
    """Påmeldte deltakere som «Kari N.» (fornavn og forbokstav i etternavnet), sortert. Det eneste formatet som skal på siden."""
    ut = []
    for r in con.execute("""SELECT d.fornavn, d.etternavn FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id
                            WHERE p.kurs_id=? AND p.status='bekreftet'""", (kurs_id,)):
        fornavn, etternavn = (r["fornavn"] or "").strip(), (r["etternavn"] or "").strip()
        if fornavn or etternavn:
            ut.append(f"{fornavn} {etternavn[0].upper()}.".strip() if etternavn else fornavn)
    return sorted(ut, key=str.lower)


# ============================ gruppelister etter kurset ============================

def tom_gamle_tabeller(con, idag: date, dager: int, *, torr: bool = False) -> int:
    """Tømmer radene i tabell-blokkene (gruppelister med navn) på kurs der siste kursdag er mer enn `dager` dager siden: i utkastet,
    den publiserte siden og alle lagrede versjoner. Idempotent: en tabell uten rader røres ikke. Returnerer antall kurs som ble
    endret (torr=True: bare teller, ingenting skrives)."""
    grense = (idag - timedelta(days=dager)).isoformat()
    kurs = [int(r["kurs_id"]) for r in con.execute(
        """SELECT s.kurs_id FROM kursside s
           JOIN (SELECT kurs_id, MAX(dato) AS siste FROM kursdag GROUP BY kurs_id) x ON x.kurs_id=s.kurs_id
           WHERE x.siste < ? ORDER BY s.kurs_id""", (grense,))]
    endret = 0
    for kurs_id in kurs:
        rad = con.execute("SELECT utkast, publisert FROM kursside WHERE kurs_id=?", (kurs_id,)).fetchone()
        arbeid = []                                  # (tabell, nøkkel, nytt innhold)
        for kolonne in ("utkast", "publisert"):
            if rad[kolonne]:
                dok = si.les(rad[kolonne])
                if si.har_tabellrader(dok):
                    arbeid.append((kolonne, kurs_id, si.til_json(si.tom_tabeller(dok)[0])))
        for v in con.execute("SELECT id, innhold FROM kursside_versjon WHERE kurs_id=?", (kurs_id,)).fetchall():
            dok = si.les(v["innhold"])
            if si.har_tabellrader(dok):
                arbeid.append(("versjon", int(v["id"]), si.til_json(si.tom_tabeller(dok)[0])))
        if not arbeid:
            continue
        endret += 1
        if torr:
            continue
        for hva, nokkel, ny in arbeid:
            if hva == "utkast":
                con.execute("UPDATE kursside SET utkast=?, versjon=versjon+1, endret=?, endret_av='system' WHERE kurs_id=?",
                            (ny, db.naa_utc(), nokkel))
            elif hva == "publisert":
                con.execute("UPDATE kursside SET publisert=? WHERE kurs_id=?", (ny, nokkel))
            else:
                con.execute("UPDATE kursside_versjon SET innhold=? WHERE id=?", (ny, nokkel))
    return endret
