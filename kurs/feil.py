"""Sikker teknisk feiltekst for logg, database og driftsutskrift.

En rå str(exception) kan inneholde persondata: requests.HTTPError tar med hele URL-en (Visma-
kundeoppslaget har deltakerens e-post i $filter), og andre biblioteker kan ta med request body,
tokens eller adresser. Alt som lagres (hendelse.detaljer, faktura_forsok.feilmelding) og skrives til
konsollen skal derfor kun bruke sikker_feiltekst() - aldri str(e) direkte.

Vi beholder bevisst KUN exception-typen og (hvis tilgjengelig) HTTP-statuskoden. Taksonomien for hva
som er "sikkert feilet" er ikke avklart for Graph/Visma - denne funksjonen klassifiserer ingenting.
"""


def sikker_feiltekst(e: BaseException) -> str:
    """F.eks. 'HTTPError (HTTP 500)' eller 'RuntimeError'. Aldri meldingsteksten."""
    tekst = type(e).__name__
    status = getattr(getattr(e, "response", None), "status_code", None)
    if isinstance(status, int):
        tekst += f" (HTTP {status})"
    return tekst
