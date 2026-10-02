"""Svar med en fil fra kurssiden (SPEC §8.3). Brukes av både deltakerruten og adminruten, så begge har nøyaktig samme headere.

Sikkerhet: innholdstypen settes av SERVEREN ut fra selve innholdet (aldri klientens eller lagret mimetype), dokumenter sendes alltid
som `application/octet-stream` og lastes ned (`attachment`), `nosniff` og en streng sandbox-CSP hindrer at en fil kan kjøre noe
i nettleseren selv om den skulle inneholde HTML eller skript.
"""
from urllib.parse import quote

from flask import Response

from .. import signaturer

_BILDETYPER = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def sniffet_bildetype(data: bytes) -> str | None:
    """image/png, image/jpeg, image/gif eller image/webp ut fra innholdet; ellers None."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    try:
        return signaturer.les_bilde(data)[0]
    except signaturer.Signaturfeil:
        return None


def _ascii_reserve(navn: str) -> str:
    ren = navn.encode("ascii", "ignore").decode("ascii").replace('"', "").replace("\\", "")
    ren = "".join(c for c in ren if c >= " " and c != "\x7f").strip()
    if not ren or ren in (".", ".."):
        endelse = navn.rsplit(".", 1)[-1] if "." in navn else ""
        ren = "fil" + (("." + "".join(c for c in endelse if c.isascii() and c.isalnum())) if endelse else "")
    return ren


def fil_respons(meta: dict, data: bytes) -> Response:
    """Bilder vises i siden (inline, sniffet type); alt annet lastes ned. Se modulens docstring."""
    bildetype = sniffet_bildetype(data) if meta.get("type") == "bilde" else None
    if bildetype in _BILDETYPER:
        svar = Response(data, mimetype=bildetype)
        svar.headers["Content-Disposition"] = "inline"
        svar.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'"
        svar.headers["Cache-Control"] = "private, max-age=300"
    else:
        navn = meta.get("filnavn") or "fil"
        svar = Response(data, mimetype="application/octet-stream")
        svar.headers["Content-Disposition"] = (
            f'attachment; filename="{_ascii_reserve(navn)}"; filename*=UTF-8\'\'{quote(navn, safe="")}')
        svar.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
        svar.headers["Cache-Control"] = "private, no-store"
    svar.headers["X-Content-Type-Options"] = "nosniff"
    return svar
