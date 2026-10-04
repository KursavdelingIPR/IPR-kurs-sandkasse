"""Vedlegg i manuell e-post fra deltakervinduet (Camilla 03.10.2026: «legge til en knapp hvor det skal være mulig å legge
til vedlegg i e-postene som sendes»).

Bare vanlige dokument- og bildetyper, kontrollert ut fra innholdet og ikke bare filnavnet. Til sammen (med bildene i
signaturen) holdes e-posten under det Microsoft 365 tar imot i én forespørsel (4 MB etter base64-koding).
Filene sendes og lagres i e-posthistorikken (sendt_epost_vedlegg) nøyaktig slik de ble lastet opp.
"""
import re
from pathlib import PurePath

from .integrasjoner.epost import Vedlegg

MAKS_ANTALL = 10
MAKS_SUM = 3_000_000            # byte, vedlegg + signaturbilder (base64 gjør det til ca. 4 MB)

_PDF, _OOXML, _OLE = b"%PDF-", b"PK\x03\x04", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _tekst(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        try:
            data.decode("cp1252")                 # CSV fra Excel er ofte Windows-1252
            return True
        except UnicodeDecodeError:
            return False


# filendelse -> (mimetype, kontroll av innholdet). Mimetypen settes her, aldri ut fra det nettleseren oppgir.
TYPER = {
    "pdf": ("application/pdf", lambda d: d.startswith(_PDF)),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", lambda d: d.startswith(_OOXML)),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", lambda d: d.startswith(_OOXML)),
    "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", lambda d: d.startswith(_OOXML)),
    "odt": ("application/vnd.oasis.opendocument.text", lambda d: d.startswith(_OOXML)),
    "ods": ("application/vnd.oasis.opendocument.spreadsheet", lambda d: d.startswith(_OOXML)),
    "doc": ("application/msword", lambda d: d.startswith(_OLE)),
    "xls": ("application/vnd.ms-excel", lambda d: d.startswith(_OLE)),
    "ppt": ("application/vnd.ms-powerpoint", lambda d: d.startswith(_OLE)),
    "png": ("image/png", lambda d: d.startswith(b"\x89PNG\r\n\x1a\n")),
    "jpg": ("image/jpeg", lambda d: d.startswith(b"\xff\xd8\xff")),
    "jpeg": ("image/jpeg", lambda d: d.startswith(b"\xff\xd8\xff")),
    "gif": ("image/gif", lambda d: d[:6] in (b"GIF87a", b"GIF89a")),
    "txt": ("text/plain", _tekst),
    "csv": ("text/csv", _tekst),
    "ics": ("text/calendar", lambda d: _tekst(d) and d.lstrip(b"\xef\xbb\xbf \r\n").startswith(b"BEGIN:VCALENDAR")),
}
GODTATTE = ", ".join(sorted({e.upper() for e in TYPER} - {"JPEG"}))
ACCEPT = ",".join("." + e for e in TYPER)       # for <input type=file accept=...>


class Vedleggfeil(ValueError):
    pass


def trygt_filnavn(navn: str) -> str:
    grunn = PurePath((navn or "").replace("\\", "/")).name
    return re.sub(r'[\x00-\x1f"<>:|?*/\\]+', "_", grunn).strip(" .")[:120] or "vedlegg"


def les(filer, signaturbilder_byte: int = 0) -> list[Vedlegg]:
    """Opplastede filer (werkzeug FileStorage) -> Vedlegg. Tomme felt hoppes over. Stopper med en forklaring når en fil
    ikke kan brukes, eller når e-posten blir for stor."""
    ut: list[Vedlegg] = []
    for f in filer:
        if not f or not f.filename:
            continue
        navn = trygt_filnavn(f.filename)
        endelse = navn.rsplit(".", 1)[-1].lower() if "." in navn else ""
        if endelse not in TYPER:
            raise Vedleggfeil(f"«{navn}» kan ikke legges ved. Godtatte filtyper: {GODTATTE}.")
        data = f.read(MAKS_SUM + 1)
        if not data:
            raise Vedleggfeil(f"«{navn}» er tom.")
        mimetype, kontroll = TYPER[endelse]
        if not kontroll(data):
            raise Vedleggfeil(f"«{navn}» ser ikke ut til å være en ekte {endelse.upper()}-fil og kan ikke legges ved.")
        ut.append(Vedlegg(navn, mimetype, data))
    if len(ut) > MAKS_ANTALL:
        raise Vedleggfeil(f"Du kan legge ved maks {MAKS_ANTALL} filer i én e-post.")
    if sum(len(v.innhold) for v in ut) + signaturbilder_byte > MAKS_SUM:
        raise Vedleggfeil("Vedleggene er for store: e-posten kan være maks 3 MB til sammen (vedlegg og bildene i "
                          "signaturen). Del opp i flere e-poster, eller legg filen på Min side og send lenken.")
    return ut
