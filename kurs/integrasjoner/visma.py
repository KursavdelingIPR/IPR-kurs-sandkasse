"""Fakturering i Visma.

!!! MAA BEKREFTES I MOTET: hvilket Visma-produkt IPR bruker. !!!
  * Visma eAccounting / eRegnskap  -> REST API (implementert under som utgangspunkt)
  * Visma.net ERP                  -> annet REST API
  * Visma Business NXT             -> GraphQL API
Uansett produkt skal resten av systemet kun kalle `fakturer(...)` – bytt implementasjon her.

Prinsipp (fra referansen): databasen er fasit for HVEM som skal faktureres; Visma lager og sender fakturaen.
Feltnavn mot eAccounting-API-et maa verifiseres mot dokumentasjonen for produksjon.
"""
import time
from dataclasses import dataclass

import requests

from .. import config

_cache: dict = {}


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
    kunde_id = _finn_eller_opprett_kunde(g)
    utkast = _kall("POST", "/customerinvoicedrafts", json={
        "CustomerId": kunde_id,
        "YourReference": g.deres_ref,
        "Rows": [{"ArticleNumber": g.artikkel, "Text": g.linjetekst, "UnitPrice": g.belop_nok, "Quantity": 1}],
        "EuThirdParty": False,
    })
    faktura = _kall("POST", f"/customerinvoicedrafts/{utkast['Id']}/convert")
    return Fakturaresultat(visma_id=faktura["Id"], faktura_nr=str(faktura.get("InvoiceNumber")), kunde_id=kunde_id)


def _token() -> str:
    if _cache.get("utloper", 0) > time.time() + 60:
        return _cache["token"]
    r = requests.post("https://identity.vismaonline.com/connect/token", data={
        "grant_type": "refresh_token",
        "refresh_token": _cache.get("refresh") or config.VISMA_REFRESH_TOKEN,
        "client_id": config.VISMA_CLIENT_ID,
        "client_secret": config.VISMA_CLIENT_SECRET,
    }, timeout=30)
    r.raise_for_status()
    d = r.json()
    # NB: refresh-token roteres – maa lagres varig i prod (TODO: lagre i database/Key Vault)
    _cache.update(token=d["access_token"], utloper=time.time() + d["expires_in"], refresh=d.get("refresh_token"))
    return _cache["token"]


def _kall(metode: str, sti: str, **kw):
    r = requests.request(metode, config.VISMA_API + sti, headers={"Authorization": f"Bearer {_token()}"}, timeout=30, **kw)
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
