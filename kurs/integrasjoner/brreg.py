"""Brønnøysundregistrene, Enhetsregisteret: firmanavn og fakturaadresse fra organisasjonsnummer, og søk på firmanavn.

Brukes av påmeldingsskjemaet når firma betaler: deltakeren skriver organisasjonsnummeret (eller søker på firmanavnet),
og navn og adresse hentes herfra - deltakeren skriver dem ikke selv, men ser dem i skjemaet. Åpne data (NLOD): ingen
nøkler og ingen innlogging.

  * Adressen er postadressen når den har gate eller postboks, ellers forretningsadressen (enheter) eller
    beliggenhetsadressen (underenheter).
  * Demo (MODUS=demo, også testene): oppdiktede virksomheter, INGEN nettverkskall (CLAUDE.md regel 3). Unntak: med
    BRREG_LIVE=1 (bare lokal prøving, standard av) slås andre nummer og navn enn de oppdiktede opp i det ekte registeret.
  * Drift: https://data.brreg.no/enhetsregisteret/api med kort tidsavbrudd. Svarene huskes en stund i minnet. Svarer
    ikke registeret, kastes Utilgjengelig. Skjemaet lar deltakeren da melde seg på med organisasjonsnummeret alene, og
    påmeldingen merkes «Firmaopplysninger må kontrolleres» (kurs/firmaopplysninger.py). Deltakeren skriver ALDRI
    firmanavn eller adresse selv. Ingenting logges med organisasjonsnummer eller svar.
  * `python -m kurs.brreg_sjekk <orgnr>` slår opp i det EKTE registeret (også i demo) for å kontrollere oppkoblingen.
"""
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass

import requests

from .. import config

API = "https://data.brreg.no/enhetsregisteret/api"
TIDSAVBRUDD_SEK = 5
MAKS_TREFF = 10
MIN_SOK = 3
_VEKTER = (3, 2, 7, 6, 5, 4, 3, 2)
_HUSK_FUNNET, _HUSK_ANNET = 3600, 600       # sekunder et svar huskes (funnet / ikke funnet og søk)


class Utilgjengelig(Exception):
    """Registeret svarte ikke som forventet (nettverk, tidsavbrudd, HTTP 429/5xx eller ugyldig svar). Ingen detaljer."""


@dataclass(frozen=True)
class Enhet:
    orgnr: str
    navn: str
    adresse: str        # gate/postboks (flere linjer skilt med komma), tom hvis registeret ikke har den
    postnr: str
    poststed: str
    underenhet: bool = False

    def som_dict(self) -> dict:
        return asdict(self)


def normaliser_orgnr(tekst) -> str:
    """Bare sifrene 0-9 (mellomrom og punktum som i «974 760 673» fjernes). Resultatet kan fortsatt være ugyldig."""
    if not isinstance(tekst, str):
        return ""
    return "".join(t for t in tekst if t in "0123456789")


def gyldig_orgnr(nr) -> bool:
    """9 siffer, begynner med 8 eller 9, og kontrollsifferet stemmer (modulus 11)."""
    if not (isinstance(nr, str) and len(nr) == 9 and all(t in "0123456789" for t in nr) and nr[0] in "89"):
        return False
    kontroll = (11 - sum(int(t) * v for t, v in zip(nr, _VEKTER)) % 11) % 11
    return kontroll != 10 and kontroll == int(nr[8])


# ============================ demo ============================

# Oppdiktede virksomheter (gyldige kontrollsifre). Brukes i demo og i testene - aldri ekte opplysninger.
DEMO_ENHETER = (
    Enhet("999999999", "DEMOVIRKSOMHET AS", "Demoveien 1", "0101", "DEMOBY"),
    Enhet("999900003", "EKSEMPEL KOMMUNE", "Postboks 100", "1234", "EKSEMPELBY"),
    Enhet("999900011", "EKSEMPEL KOMMUNE HELSE OG MESTRING", "Rådhusgata 1", "1234", "EKSEMPELBY", underenhet=True),
    Enhet("999900038", "DEMO SYKEHUS HF", "Postboks 7", "5678", "DEMOSTAD"),
    Enhet("999900046", "PSYKOLOGSENTERET EKSEMPEL AS", "Eksempelveien 12", "1234", "EKSEMPELBY"),
    Enhet("999900054", "TESTKLINIKKEN DA", "Prøvegata 3", "9012", "TESTVIK"),
)
_DEMO_PER_NR = {e.orgnr: e for e in DEMO_ENHETER}

# «Registeret svarer ikke»: i demo (og testene) kaster hent() Utilgjengelig for dette nummeret, slik at hele forløpet med
# «Firmaopplysninger må kontrolleres» kan prøves uten nettverk. Nummeret er bevisst IKKE med i DEMO_ENHETER, så det kommer
# ikke opp i søk. hent_paa_nytt() (administrator og morgenjobben) svarer med enheten under: registeret er «oppe igjen».
DEMO_NEDE_ORGNR = "999900062"
DEMO_OPPE_IGJEN = Enhet(DEMO_NEDE_ORGNR, "OPPE IGJEN DEMO AS", "Retteveien 2", "1234", "EKSEMPELBY")


# ============================ hurtigminne ============================

_MANGLER = object()


class _Hurtigminne:
    """Liten, trådsikker LRU med utløpstid. Per prosess (som takbegrensningen)."""

    def __init__(self, maks: int = 500):
        self._data: OrderedDict = OrderedDict()
        self._laas = threading.Lock()
        self.maks = maks

    def hent(self, nokkel):
        with self._laas:
            post = self._data.get(nokkel)
            if post is None:
                return _MANGLER
            utloper, verdi = post
            if utloper < time.monotonic():
                del self._data[nokkel]
                return _MANGLER
            self._data.move_to_end(nokkel)
            return verdi

    def lagre(self, nokkel, verdi, sekunder: int) -> None:
        with self._laas:
            self._data[nokkel] = (time.monotonic() + sekunder, verdi)
            self._data.move_to_end(nokkel)
            while len(self._data) > self.maks:
                self._data.popitem(last=False)

    def nullstill(self) -> None:
        with self._laas:
            self._data.clear()


hurtigminne = _Hurtigminne()


# ============================ det ekte registeret ============================

def _get(sti: str, params=None) -> requests.Response:
    try:
        svar = requests.get(API + sti, params=params, headers={"Accept": "application/json"}, timeout=TIDSAVBRUDD_SEK)
    except requests.RequestException as e:
        raise Utilgjengelig() from e
    if svar.status_code == 429 or svar.status_code >= 500:
        raise Utilgjengelig()
    return svar


def _json(svar: requests.Response):
    try:
        data = svar.json()
    except ValueError as e:
        raise Utilgjengelig() from e
    if not isinstance(data, dict):
        raise Utilgjengelig()
    return data


def _adresselinjer(a: dict) -> list:
    return [linje.strip() for linje in a.get("adresse") or [] if isinstance(linje, str) and linje.strip()]


def _adresse(data: dict, underenhet: bool) -> tuple:
    """(adresse, postnr, poststed): postadressen når den har gate/postboks, ellers forretnings-/beliggenhetsadressen."""
    kandidater = [a for a in (data.get("postadresse"), data.get("beliggenhetsadresse" if underenhet else
                                                                   "forretningsadresse")) if isinstance(a, dict)]
    valgt = next((a for a in kandidater if _adresselinjer(a)), None) or next(
        (a for a in kandidater if a.get("postnummer") or a.get("poststed")), None)
    if valgt is None:
        return "", "", ""
    tekst = ", ".join(_adresselinjer(valgt))
    land = valgt.get("land")
    if valgt.get("landkode") not in (None, "NO") and isinstance(land, str) and land.strip():
        tekst = f"{tekst}, {land.strip()}" if tekst else land.strip()
    return tekst, str(valgt.get("postnummer") or "").strip(), str(valgt.get("poststed") or "").strip()


def _enhet(data: dict, underenhet: bool) -> Enhet | None:
    orgnr, navn = data.get("organisasjonsnummer"), data.get("navn")
    if not (isinstance(orgnr, str) and isinstance(navn, str) and navn.strip()) or data.get("slettedato"):
        return None
    return Enhet(orgnr, navn.strip(), *_adresse(data, underenhet), underenhet=underenhet)


def hent_ekte(orgnr: str) -> Enhet | None:
    """Slår opp i det EKTE registeret: først enheter, så underenheter. None = finnes ikke (eller er slettet)."""
    for sti, underenhet in (("/enheter/", False), ("/underenheter/", True)):
        svar = _get(sti + orgnr)
        if svar.status_code in (400, 404, 410):
            continue
        if svar.status_code != 200:
            raise Utilgjengelig()
        data = _json(svar)
        if data.get("slettedato"):
            continue
        enhet = _enhet(data, underenhet)
        if enhet is None:
            raise Utilgjengelig()
        return enhet
    return None


def sok_ekte(navn: str) -> list:
    """Søker på navn i det EKTE registeret: inntil 8 enheter, deretter underenheter, til sammen MAKS_TREFF."""
    treff = []
    for sti, liste, underenhet, antall in (("/enheter", "enheter", False, 8),
                                           ("/underenheter", "underenheter", True, MAKS_TREFF)):
        rom = min(antall, MAKS_TREFF - len(treff))
        if rom <= 0:
            break
        svar = _get(sti, params={"navn": navn, "size": rom})
        if svar.status_code == 400:
            continue
        if svar.status_code != 200:
            raise Utilgjengelig()
        rader = (_json(svar).get("_embedded") or {}).get(liste) or []
        treff += [e for e in (_enhet(r, underenhet) for r in rader if isinstance(r, dict)) if e][:rom]
    return treff


# ============================ det skjemaet bruker ============================

def hent(orgnr) -> Enhet | None:
    """Firmanavn og adresse for et organisasjonsnummer. None = ugyldig nummer eller finnes ikke.
    Kaster Utilgjengelig når registeret ikke svarer (i demo bare for DEMO_NEDE_ORGNR, og med BRREG_LIVE=1 også ekte feil)."""
    nr = normaliser_orgnr(orgnr)
    if not gyldig_orgnr(nr):
        return None
    if config.DEMO:
        if nr == DEMO_NEDE_ORGNR:
            raise Utilgjengelig()
        if nr in _DEMO_PER_NR or not config.BRREG_LIVE:
            return _DEMO_PER_NR.get(nr)
        # BRREG_LIVE: ikke en oppdiktet virksomhet -> det ekte registeret (som i drift)
    husket = hurtigminne.hent(("hent", nr))
    if husket is not _MANGLER:
        return husket
    enhet = hent_ekte(nr)
    hurtigminne.lagre(("hent", nr), enhet, _HUSK_FUNNET if enhet else _HUSK_ANNET)
    return enhet


def hent_paa_nytt(orgnr) -> Enhet | None:
    """Nytt oppslag etter at det første ikke fikk svar (administrator eller morgenjobben, kurs/firmaopplysninger.py).
    Som hent(), men i demo er DEMO_NEDE_ORGNR «oppe igjen». Kaster Utilgjengelig når registeret fortsatt ikke svarer."""
    nr = normaliser_orgnr(orgnr)
    if config.DEMO and nr == DEMO_NEDE_ORGNR:
        return DEMO_OPPE_IGJEN
    return hent(nr)


def sok(navn) -> list:
    """Virksomheter med navn som ligner (inntil MAKS_TREFF). Under MIN_SOK tegn: ingen treff.
    Kaster Utilgjengelig når registeret ikke svarer (aldri i demo, bortsett fra med BRREG_LIVE=1)."""
    navn = " ".join(navn.split())[:100] if isinstance(navn, str) else ""
    if len(navn) < MIN_SOK:
        return []
    if config.DEMO:
        demo = [e for e in DEMO_ENHETER if navn.casefold() in e.navn.casefold()][:MAKS_TREFF]
        if not config.BRREG_LIVE:
            return demo
        kjente = {e.orgnr for e in demo}          # BRREG_LIVE: de oppdiktede først, så de ekte
        return (demo + [e for e in _sok_ekte_husket(navn) if e.orgnr not in kjente])[:MAKS_TREFF]
    return _sok_ekte_husket(navn)


def _sok_ekte_husket(navn: str) -> list:
    husket = hurtigminne.hent(("sok", navn.casefold()))
    if husket is not _MANGLER:
        return husket
    treff = sok_ekte(navn)
    hurtigminne.lagre(("sok", navn.casefold()), treff, _HUSK_ANNET)
    return treff
