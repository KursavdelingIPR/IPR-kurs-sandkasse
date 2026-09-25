"""Paameldingssidens tekster (fase 12C5): introduksjonstekst og tekst paa paameldingsknappen - per kurs.

Dette er INNHOLD paa den offentlige siden, ikke definisjon av deltakerfeltene (se skjemafelt.py). Verdiene ligger i
kurs.paamelding_intro og kurs.paamelding_knappetekst.

Prinsipper:
  * Ren modul: ingen database, ingen Flask. Leser kun den kursraden som sendes inn.
  * REN TEKST: ingen HTML, Markdown eller Jinja tolkes - Jinja escaper ved rendring. Linjeskift i introen vises med CSS
    (white-space: pre-line).
  * LAGRING er streng (SideFeil). VISNING er defensiv: en ugyldig lagret verdi gir aldri 500, men standard
    (ingen intro / «Meld meg på»). Standardknappeteksten finnes KUN her.
  * Lengde telles med len() etter normalisering (som i skjemafelt og maltekster). HTML-maxlength er bare hjelp.
"""
import re
from dataclasses import dataclass

STANDARD_KNAPPETEKST = "Meld meg på"
MAKS_INTRO = 1000
MAKS_KNAPPETEKST = 40

# Kolonnenavn paa kurs
INTRO = "paamelding_intro"
KNAPPETEKST = "paamelding_knappetekst"

# Grunnkoder (PII-frie, baerer aldri tekstinnhold)
UGYLDIG_TYPE = "ugyldig_type"
FOR_LANG = "for_lang"
KONTROLLTEGN = "kontrolltegn"


class SideFeil(Exception):
    """Avvist verdi. str(e) inneholder kun grunnkode og felt - aldri teksten. `forklaring` er norsk tekst til admin."""

    def __init__(self, grunn: str, felt: str, forklaring: str = ""):
        super().__init__(grunn, felt)
        self.grunn, self.felt, self.forklaring = grunn, felt, forklaring

    def __str__(self) -> str:
        return f"{self.grunn} ({self.felt})"


_KONTROLL = re.compile(r"[\x00-\x1f\x7f\x85  ]")             # alt, inkl. linjeskift og tab
_KONTROLL_UTEN_LF = re.compile(r"[\x00-\x09\x0b-\x1f\x7f\x85  ]")


def normaliser_intro(verdi) -> str | None:
    """CRLF/CR -> LF, tab -> mellomrom, ytre mellomrom fjernes, interne linjeskift beholdes. Tom -> None."""
    if verdi is None:
        return None
    if not isinstance(verdi, str):
        raise SideFeil(UGYLDIG_TYPE, INTRO, "Introduksjonsteksten må være tekst.")
    t = verdi.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    if _KONTROLL_UTEN_LF.search(t):
        raise SideFeil(KONTROLLTEGN, INTRO, "Introduksjonsteksten inneholder ugyldige tegn.")
    t = t.strip()
    if len(t) > MAKS_INTRO:
        raise SideFeil(FOR_LANG, INTRO, f"Introduksjonsteksten kan være maks {MAKS_INTRO} tegn (er nå {len(t)}).")
    return t or None


def normaliser_knappetekst(verdi) -> str | None:
    """Én linje: linjeskift, tab og andre kontrolltegn avvises. Ytre mellomrom fjernes. Tom -> None (= standard)."""
    if verdi is None:
        return None
    if not isinstance(verdi, str):
        raise SideFeil(UGYLDIG_TYPE, KNAPPETEKST, "Knappeteksten må være tekst.")
    if _KONTROLL.search(verdi):
        raise SideFeil(KONTROLLTEGN, KNAPPETEKST, "Knappeteksten må være én linje uten spesialtegn.")
    t = verdi.strip()
    if len(t) > MAKS_KNAPPETEKST:
        raise SideFeil(FOR_LANG, KNAPPETEKST,
                       f"Knappeteksten kan være maks {MAKS_KNAPPETEKST} tegn (er nå {len(t)}).")
    return t or None


_NORMALISERERE = ((INTRO, normaliser_intro), (KNAPPETEKST, normaliser_knappetekst))


@dataclass(frozen=True)
class EffektivSide:
    intro: str | None           # None = ingen introduksjonstekst (intet element rendres)
    knappetekst: str            # alltid en visbar tekst


def _gyldige_verdier(kurs) -> tuple[dict, bool]:
    """{kolonne: normalisert verdi eller None} + om noen lagret verdi var ugyldig (den gis da som None)."""
    ut, ugyldig = {}, False
    for kolonne, normaliser in _NORMALISERERE:
        try:
            ut[kolonne] = normaliser(kurs[kolonne])
        except SideFeil:
            ut[kolonne], ugyldig = None, True
    return ut, ugyldig


def effektiv_side(kurs) -> EffektivSide:
    """Det deltakeren ser. Defensivt: ugyldig lagret intro -> ingen intro, ugyldig knappetekst -> standard."""
    verdier, _ = _gyldige_verdier(kurs)
    return EffektivSide(verdier[INTRO], verdier[KNAPPETEKST] or STANDARD_KNAPPETEKST)


def kopi_av_side(kurs) -> tuple[dict, bool]:
    """Verdiene som skal kopieres ved kursduplisering: kun gyldige, normaliserte verdier (ugyldig -> None/standard).
    Returnerer ({kolonne: verdi}, minst en kildeverdi var ugyldig)."""
    return _gyldige_verdier(kurs)
