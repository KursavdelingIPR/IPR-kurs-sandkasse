"""Fakturering i Visma.

!!! MAA BEKREFTES I MOTET: hvilket Visma-produkt IPR bruker. !!!
  * Visma eAccounting / eRegnskap  -> REST API (implementert under som utgangspunkt)
  * Visma.net ERP                  -> annet REST API
  * Visma Business NXT             -> GraphQL API
Uansett produkt skal resten av systemet kun kalle `fakturer(...)` – bytt implementasjon her.

Prinsipp (fra referansen): databasen er fasit for HVEM som skal faktureres; Visma lager og sender fakturaen.
Feltnavn mot eAccounting-API-et maa verifiseres mot dokumentasjonen for produksjon.

Tilgang (OAuth 2.0, refresh-token): refresh-tokenet ROTERES - hvert bruk gir et nytt, og det gamle slutter aa virke.
Det nye lagres derfor straks i databasen (tabellen integrasjon_token) FOER tilgangstokenet brukes, saa det overlever
omstart og deles av alle instanser. VISMA_REFRESH_TOKEN (miljoevariabel/Key Vault) er bare startverdien.

Feil: IkkeSendt betyr at ingenting ble sendt til Visma sine regnskapsdata (tilgangen eller oppsettet feilet FOER
foerste kall) - fakturaforsoeket kan da trygt proeves paa nytt. Alle andre feil er uavklarte (se sveiper._opprett).
"""
import time
from dataclasses import dataclass

import requests

from .. import config, db

_cache: dict = {}
REFRESH_NOKKEL = "visma_refresh_token"


class IkkeSendt(RuntimeError):
    """Ingenting er sendt til Visma sine regnskapsdata - trygt aa proeve paa nytt. Meldingen inneholder aldri tokens."""


@dataclass
class Fakturagrunnlag:
    kunde_navn: str
    kunde_epost: str
    er_privatperson: bool
    org_nr: str | None
    adresse: str | None
    postnr: str | None
    sted: str | None
    ehf: bool
    deres_ref: str | None
    linjetekst: str
    artikkel: str | None
    belop_nok: int


@dataclass
class Fakturaresultat:
    visma_id: str
    faktura_nr: str | None
    kunde_id: str


def fakturer(g: Fakturagrunnlag) -> Fakturaresultat:
    if config.DEMO:
        _cache["demo_nr"] = n = _cache.get("demo_nr", int(time.time()) % 9000 + 1000) + 1
        return Fakturaresultat(visma_id=f"demo-{n}", faktura_nr=f"D{n}", kunde_id=f"demo-kunde-{g.kunde_epost}")
    _token()                  # tilgang FOER noe kall mot regnskapsdata: feiler dette, er ingenting sendt (IkkeSendt)
    kunde_id = _finn_eller_opprett_kunde(g)
    utkast = _kall("POST", "/customerinvoicedrafts", json={
        "CustomerId": kunde_id,
        "YourReference": g.deres_ref,
        "Rows": [{"ArticleNumber": g.artikkel, "Text": g.linjetekst, "UnitPrice": g.belop_nok, "Quantity": 1}],
        "EuThirdParty": False,
    })
    faktura = _kall("POST", f"/customerinvoicedrafts/{utkast['Id']}/convert")
    return Fakturaresultat(visma_id=faktura["Id"], faktura_nr=str(faktura.get("InvoiceNumber")), kunde_id=kunde_id)


TOKEN_URL = "https://identity.vismaonline.com/connect/token"


def _med_egen_tilkobling(funksjon):
    """Egen, kort databasetilkobling: tokenet skal lagres uavhengig av (og aldri rulles tilbake med) fakturaarbeidet."""
    con = db.koble()
    try:
        return funksjon(con)
    finally:
        con.close()


def _lagret_refresh() -> str | None:
    return _med_egen_tilkobling(lambda con: db.hent_integrasjonstoken(con, REFRESH_NOKKEL))


def _lagre_refresh(verdi: str) -> None:
    def lagre(con):
        db.lagre_integrasjonstoken(con, REFRESH_NOKKEL, verdi)
        con.commit()
    _med_egen_tilkobling(lagre)


def _ugyldig_grant(r: requests.Response) -> bool:
    """OAuth 2.0 (RFC 6749, 5.2): et brukt/utloept refresh-token gir HTTP 400 med error=invalid_grant."""
    try:
        return r.status_code == 400 and r.json().get("error") == "invalid_grant"
    except ValueError:
        return False


def _token() -> str:
    """Gyldig tilgangstoken. Kaster IkkeSendt hvis tilgangen ikke kan fornyes (ingenting er da sendt til Visma)."""
    if config.DEMO:
        return "demo-token"           # demo: aldri nettverkskall
    if _cache.get("utloper", 0) > time.time() + 60:
        return _cache["token"]
    if not (config.VISMA_CLIENT_ID and config.VISMA_CLIENT_SECRET):
        raise IkkeSendt("Visma er ikke satt opp (VISMA_CLIENT_ID / VISMA_CLIENT_SECRET mangler).")
    forrige = None
    for _ in range(2):
        refresh = _lagret_refresh() or config.VISMA_REFRESH_TOKEN
        if not refresh:
            raise IkkeSendt("Visma mangler refresh-token (VISMA_REFRESH_TOKEN).")
        if refresh == forrige:        # samme token som nettopp ble avvist: ingen annen prosess har fornyet det
            break
        forrige = refresh
        try:
            r = requests.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": refresh,
                                               "client_id": config.VISMA_CLIENT_ID,
                                               "client_secret": config.VISMA_CLIENT_SECRET}, timeout=30)
        except requests.RequestException as e:
            raise IkkeSendt(f"Visma-tilgangen kunne ikke fornyes ({type(e).__name__}).") from None
        if _ugyldig_grant(r):
            continue                  # en annen prosess kan akkurat ha fornyet og lagret et nytt token - les paa nytt
        if not r.ok:
            raise IkkeSendt(f"Visma-tilgangen kunne ikke fornyes (HTTP {r.status_code}).")
        d = r.json()
        if d.get("refresh_token"):
            _lagre_refresh(d["refresh_token"])   # FOER tilgangstokenet brukes: det gamle refresh-tokenet er naa ugyldig
        _cache.update(token=d["access_token"], utloper=time.time() + d["expires_in"])
        return _cache["token"]
    raise IkkeSendt("Visma avviste refresh-tokenet (invalid_grant). En administrator maa hente et nytt - se "
                    "AZURE-SETUP.md, «Visma».")


def _kall(metode: str, sti: str, **kw):
    try:
        token = _token()
    except IkkeSendt as e:        # midt i en fakturering kan tidligere kall ha endret noe hos Visma: da er det UAVKLART
        raise RuntimeError(f"Visma-tilgangen feilet midt i en fakturering: {e}") from None
    r = requests.request(metode, config.VISMA_API + sti, headers={"Authorization": f"Bearer {token}"}, timeout=30, **kw)
    r.raise_for_status()
    return r.json() if r.content else {}


def _finn_eller_opprett_kunde(g: Fakturagrunnlag) -> str:
    def odata(verdi: str) -> str:          # OData-strenglitteral: apostrof dobles - aldri raa brukerverdi i filteret
        return str(verdi).replace("'", "''")
    filter_ = (f"CorporateIdentityNumber eq '{odata(g.org_nr)}'" if g.org_nr
               else f"EmailAddress eq '{odata(g.kunde_epost)}'")
    treff = _kall("GET", "/customers", params={"$filter": filter_}).get("Data", [])
    if treff:
        return treff[0]["Id"]
    kunde = _kall("POST", "/customers", json={
        "Name": g.kunde_navn,
        "EmailAddress": g.kunde_epost,
        "IsPrivatePerson": g.er_privatperson,
        "CorporateIdentityNumber": g.org_nr,
        "InvoiceAddress1": g.adresse,
        "InvoicePostalCode": g.postnr,
        "InvoiceCity": g.sted,
        "InvoiceCountryCode": "NO",
        "ElectronicInvoiceAddress": g.org_nr if g.ehf else None,
    })
    return kunde["Id"]
