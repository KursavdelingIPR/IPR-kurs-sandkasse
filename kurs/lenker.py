"""Signerte lenker som skal virke UTEN innlogging (f.eks. kursholders opplasting av materiell).

Ren modul (ingen Flask): brukes baade av webappen og av e-postutsendingen i daglig.py. Signaturen er en kort HMAC av
HEMMELIG_NOKKEL over formaal + id - den kan ikke gjettes eller forfalskes uten noekkelen, og en lenke for ett
materiellkrav gir aldri tilgang til et annet. Bytter man HEMMELIG_NOKKEL, slutter gamle lenker aa virke (ny purring
sender ny lenke).
"""
import hashlib
import hmac
import re

from . import config


def signatur(formaal: str, *deler) -> str:
    melding = ":".join([formaal, *map(str, deler)]).encode()
    return hmac.new(config.HEMMELIG_NOKKEL.encode(), melding, hashlib.sha256).hexdigest()[:32]


def signatur_ok(mottatt, formaal: str, *deler) -> bool:
    return bool(mottatt) and isinstance(mottatt, str) and hmac.compare_digest(mottatt, signatur(formaal, *deler))


def lever_lenke(krav_id: int) -> str:
    """Kursholders opplastingslenke for et materiellkrav."""
    return f"{config.BASE_URL}/lever/{int(krav_id)}/{signatur('lever', int(krav_id))}"


_MIN_SIDE_TOKEN = re.compile(r"(0|[1-9][0-9]{0,11})\.(0|[1-9][0-9]{0,5})\.([0-9a-f]{32})")      # bare ASCII-sifre, ingen ledende nuller: én lenke har én skrivemåte


def min_side_token(paamelding_id: int, versjon: int = 0) -> str:
    """Den personlige lenken til «Min side» for ÉN påmelding: «<påmelding>.<versjon>.<signatur>». Signaturen (128 biter) kan ikke
    gjettes eller forfalskes uten HEMMELIG_NOKKEL, og en lenke gir aldri tilgang til en annen påmelding. Ingenting lagres: lenken
    regnes ut hver gang. `versjon` økes når en administrator lager en ny lenke (eldre lenker slutter da å virke). Se kurs/minside.py."""
    return f"{int(paamelding_id)}.{int(versjon)}.{signatur('minside', int(paamelding_id), int(versjon))}"


def les_min_side_token(token) -> tuple[int, int] | None:
    """(påmelding, versjon) når lenken er riktig utformet OG riktig signert, ellers None."""
    if not isinstance(token, str):
        return None
    treff = _MIN_SIDE_TOKEN.fullmatch(token)
    if not treff:
        return None
    pid, versjon = int(treff.group(1)), int(treff.group(2))
    return (pid, versjon) if signatur_ok(treff.group(3), "minside", pid, versjon) else None
