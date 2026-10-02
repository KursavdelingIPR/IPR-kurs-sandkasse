"""Farger per kursserie i Kalender og Årsplan: alle samlinger og kurs i samme utdanning får samme farge i alle visningene,
slik kursplanen og kurshjulet i Excel har gjort det. Fargen er bare en hjelp til å se hva som hører sammen - navnet står
alltid som tekst ved siden av.

Serien er kursets spesialistløp når det er satt, ellers kursnavnet uten «– samling 2», «, 2. samling» og det som står
etter. «EFT spesialistutdanning – samling 1» og «EFT spesialistutdanning – samling 2» får dermed samme farge. Fargen velges
fra en fast palett ut fra serien, så samme serie får samme farge overalt og fra år til år. Paletten står i
static/kursfarger.css (.kursfarge-0 til .kursfarge-9).

Samme fil i Kalender- og Årsplan-grenen.
"""
import re
import zlib

ANTALL = 10
_SAMLING = re.compile(r"[\s,.:;(–—-]*\b(?:\d+\s*\.\s*)?samling\b.*$", re.IGNORECASE)


def serie(navn: str | None, spesialistlop: str | None = None) -> str:
    """Nøkkelen for kursserien: spesialistløpet, ellers navnet uten samlingsnummeret (små bokstaver)."""
    if spesialistlop and spesialistlop.strip():
        return "løp:" + " ".join(spesialistlop.split()).casefold()
    navn = " ".join((navn or "").split())
    return (_SAMLING.sub("", navn) or navn).casefold()


def farge(navn: str | None, spesialistlop: str | None = None) -> int:
    """Fargenummeret (0-9) for kursserien."""
    return zlib.crc32(serie(navn, spesialistlop).encode("utf-8")) % ANTALL
