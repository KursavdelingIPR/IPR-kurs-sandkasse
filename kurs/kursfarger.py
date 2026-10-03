"""Farger per kursserie i Kalender og Årsplan: alle samlinger og kurs i samme utdanning får samme farge i alle visningene,
slik kursplanen og kurshjulet i Excel har gjort det. Fargen er bare en hjelp til å se hva som hører sammen - navnet står
alltid som tekst ved siden av.

Serien er kursets spesialistløp når det er satt, ellers kursnavnet uten «– samling 2», «, 2. samling» og det som står
etter. «EFT spesialistutdanning – samling 1» og «EFT spesialistutdanning – samling 2» får dermed samme farge. Fargen velges
fra en fast palett ut fra serien, så samme serie får samme farge overalt og fra år til år. Paletten står i
static/kursfarger.css (.kursfarge-0 til .kursfarge-9).

Planlagte kurs (kurs/planlagte_kurs.py) har i stedet en LAGRET farge, valgt én gang ut fra datoene med velg() nederst.

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


# ---------------- lagret farge (planlagte kurs, kurs/planlagte_kurs.py) ----------------
# Et planlagt kurs får fargen én gang, ut fra datoene, og den lagres: alle samlingene har samme farge, og kurs som går nær
# hverandre i tid får ulike farger. Paletten er fortsatt de 10 fargene over (flere farger ble for like hverandre).

def _avstand(a: tuple, b: tuple) -> int:
    """Dager mellom to perioder (fra, til) - 0 når de overlapper."""
    (a_fra, a_til), (b_fra, b_til) = a, b
    if a_fra <= b_til and b_fra <= a_til:
        return 0
    return (b_fra - a_til).days if b_fra > a_til else (a_fra - b_til).days


def naerhet(dager: int) -> float:
    """Hvor «nær» to samlinger er: stor når de overlapper eller ligger i samme uke, og faller raskt med avstanden."""
    return 1000.0 if dager == 0 else 1000.0 / (1 + (dager / 7) ** 2)


def velg(perioder: list[tuple], andre: list[tuple[int, list[tuple]]], antall: int = ANTALL) -> int:
    """Fargen (0 til antall-1) til et kurs med disse periodene (samlingene, som (fra, til)). `andre` er (farge, perioder) for
    kursene som allerede har farge. Velger fargen med minst samlet nærhet til kursene som har den. Likt: fargen som brukes
    minst, så laveste nummer - samme data gir alltid samme farge."""
    kost, brukt = [0.0] * antall, [0] * antall
    for nr, deres in andre:
        if 0 <= nr < antall:
            brukt[nr] += 1
            kost[nr] += sum(naerhet(_avstand(a, b)) for a in perioder for b in deres)
    return min(range(antall), key=lambda nr: (round(kost[nr], 6), brukt[nr], nr))
