"""Signerte lenker som skal virke UTEN innlogging (f.eks. kursholders opplasting av materiell).

Ren modul (ingen Flask): brukes baade av webappen og av e-postutsendingen i daglig.py. Signaturen er en kort HMAC av
HEMMELIG_NOKKEL over formaal + id - den kan ikke gjettes eller forfalskes uten noekkelen, og en lenke for ett
materiellkrav gir aldri tilgang til et annet. Bytter man HEMMELIG_NOKKEL, slutter gamle lenker aa virke (ny purring
sender ny lenke).
"""
import hashlib
import hmac

from . import config


def signatur(formaal: str, *deler) -> str:
    melding = ":".join([formaal, *map(str, deler)]).encode()
    return hmac.new(config.HEMMELIG_NOKKEL.encode(), melding, hashlib.sha256).hexdigest()[:32]


def signatur_ok(mottatt, formaal: str, *deler) -> bool:
    return bool(mottatt) and isinstance(mottatt, str) and hmac.compare_digest(mottatt, signatur(formaal, *deler))


def lever_lenke(krav_id: int) -> str:
    """Kursholders opplastingslenke for et materiellkrav."""
    return f"{config.BASE_URL}/lever/{int(krav_id)}/{signatur('lever', int(krav_id))}"
