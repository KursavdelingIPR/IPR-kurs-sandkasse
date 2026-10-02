"""Kontrollerer oppslaget i Enhetsregisteret (Brønnøysundregistrene) mot det EKTE registeret - også når systemet står
i demo. Leser bare åpne data og lagrer ingenting. Et bevisst unntak fra demo-regelen (CLAUDE.md regel 3): kommandoen
kjøres bare når noen skriver den selv.

    python -m kurs.brreg_sjekk 974760673
    python -m kurs.brreg_sjekk --sok "brønnøysundregistrene"
"""
import argparse

from .integrasjoner import brreg


def _skriv(e: brreg.Enhet) -> None:
    print(f"  {e.orgnr}  {e.navn}{'  (underenhet)' if e.underenhet else ''}")
    print(f"      Fakturaadresse: {e.adresse or '(ingen gate/postboks)'}, {e.postnr} {e.poststed}".rstrip())


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m kurs.brreg_sjekk",
                                description="Slår opp i det ekte Enhetsregisteret (Brønnøysundregistrene).")
    p.add_argument("orgnr", nargs="?", help="organisasjonsnummer (9 siffer)")
    p.add_argument("--sok", help="søk på firmanavn (minst 3 tegn)")
    a = p.parse_args(argv)
    if not a.orgnr and not a.sok:
        p.error("oppgi et organisasjonsnummer eller --sok «navn»")
    try:
        if a.orgnr:
            nr = brreg.normaliser_orgnr(a.orgnr)
            if not brreg.gyldig_orgnr(nr):
                print("Ugyldig organisasjonsnummer: det skal være 9 siffer med riktig kontrollsiffer.")
                return 2
            enhet = brreg.hent_ekte(nr)
            if enhet is None:
                print(f"Fant ikke {nr} i Enhetsregisteret (finnes ikke, eller er slettet).")
                return 1
            print("Funnet i Enhetsregisteret:")
            _skriv(enhet)
        if a.sok:
            if len(a.sok.strip()) < brreg.MIN_SOK:
                print(f"Skriv minst {brreg.MIN_SOK} tegn for å søke.")
                return 2
            treff = brreg.sok_ekte(a.sok.strip())
            print(f"{len(treff)} treff på «{a.sok.strip()}»:")
            for e in treff:
                _skriv(e)
    except brreg.Utilgjengelig:
        print("Enhetsregisteret svarte ikke (nettverk, tidsavbrudd eller feil hos registeret). Prøv igjen senere.")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
