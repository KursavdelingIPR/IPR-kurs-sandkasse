"""Testdata: mange tester gir en deltaker som ett fullt navn («Ola Nordmann»). Hjelperne her gjoer det om til fornavn og
etternavn med samme regel som migrering 7 (siste ord blir etternavn); ett ord faar etternavnet «Test». KUN for testdata -
appen tar alltid imot fornavn og etternavn hver for seg og gjetter aldri."""


def navnedeler(navn: str) -> dict:
    *fornavn, etternavn = navn.split()
    if not fornavn:
        return {"fornavn": etternavn, "etternavn": "Test"}
    return {"fornavn": " ".join(fornavn), "etternavn": etternavn}


def _del(navn: str) -> tuple[str, str]:
    """Som navnedeler, men et tomt navn blir to tomme felt (for tester av validering)."""
    if not (navn or "").strip():
        return "", ""
    d = navnedeler(navn)
    return d["fornavn"], d["etternavn"]


def personskjema(data: dict) -> dict:
    """Skjemadata med "navn" (fullt navn) -> "fornavn"/"etternavn", slik paameldings- og deltakerskjemaene naa krever."""
    if "navn" not in data:
        return data
    ut = {k: v for k, v in data.items() if k != "navn"}
    ut["fornavn"], ut["etternavn"] = _del(data["navn"])
    return ut


def gruppeskjema(data: dict) -> dict:
    """Bedriftspaamelding: kontakt_navn og deltaker_navn (fullt navn, ett eller en liste) -> egne fornavn-/etternavn-felt."""
    ut = dict(data)
    if "kontakt_navn" in ut:
        ut["kontakt_fornavn"], ut["kontakt_etternavn"] = _del(ut.pop("kontakt_navn"))
    if "deltaker_navn" in ut:
        navn = ut.pop("deltaker_navn")
        deler = [_del(n) for n in (navn if isinstance(navn, list) else [navn])]
        ut["deltaker_fornavn"] = [f for f, _ in deler]
        ut["deltaker_etternavn"] = [e for _, e in deler]
    return ut
