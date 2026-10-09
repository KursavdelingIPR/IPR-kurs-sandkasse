"""Kontrollerer koblingen til Microsoft 365 (e-post) - for testmiljøet i Azure når Serit er ferdig.
Et bevisst unntak fra demo-regelen (CLAUDE.md regel 3), som brreg_sjekk: kommandoen kjøres bare når noen skriver den selv.
Den sender ingen e-post og endrer ingenting, og skriver aldri ut nøkler, sertifikat eller token.

    python -m kurs.m365_sjekk                 innstillinger, sertifikat og innlogging hos Microsoft (tillatelsene appen har)
"""
import argparse
import base64
import json

import requests

from . import config
from .integrasjoner import m365, sertifikat


def _tillatelser(token: str) -> list[str]:
    """Tillatelsene (roles) i tokenet Microsoft ga appen, f.eks. Mail.Send. Leser bare innholdet, ingen signaturkontroll."""
    try:
        del_ = token.split(".")[1]
        return sorted(json.loads(base64.urlsafe_b64decode(del_ + "=" * (-len(del_) % 4))).get("roles") or [])
    except (IndexError, ValueError):
        return []


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m kurs.m365_sjekk",
                                description="Kontrollerer koblingen til Microsoft 365 uten å sende eller endre noe.")
    a = p.parse_args(argv)

    mangler = [n for n in ("M365_TENANT_ID", "M365_CLIENT_ID") if not getattr(config, n)]
    if not (config.M365_SERTIFIKAT or config.M365_CLIENT_SECRET):
        mangler.append("M365_SERTIFIKAT (eller M365_CLIENT_SECRET)")
    if mangler:
        print("Mangler innstillinger: " + ", ".join(mangler))
        return 2
    if config.M365_SERTIFIKAT:
        try:
            sertifikat.kontroller(config.M365_SERTIFIKAT, config.M365_SERTIFIKAT_PASSORD)
        except sertifikat.Sertifikatfeil as e:
            print(f"Sertifikatet: {e}")
            return 1
        print("Sertifikatet kan leses.")
    else:
        print("Bruker client secret (sertifikat anbefales).")

    try:
        token = m365.token()
    except requests.HTTPError as e:
        kode = e.response.status_code if e.response is not None else "?"
        print(f"Microsoft avviste innloggingen (HTTP {kode}). Sjekk tenant, klient-id og at sertifikatet er lastet opp "
              "i app-registreringen.")
        return 1
    except requests.RequestException as e:
        print(f"Fikk ikke kontakt med Microsoft: {type(e).__name__}")
        return 1
    tillatelser = _tillatelser(token)
    print("Innlogging hos Microsoft: OK")
    print("Tillatelser: " + (", ".join(tillatelser) if tillatelser else "(ingen - be IT gi admin consent)"))
    print(f"Sender e-post fra: {config.AVSENDER_EPOST}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
