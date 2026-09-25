"""Kursmateriell i SharePoint.

Designvalg: deltakerne faar ALDRI direkte tilgang til SharePoint. Min side henter filen via appen
og sjekker at deltakeren er paameldt kurset. Da slipper IPR ekstern deling/gjestebrukere i M365,
og tilgang forsvinner automatisk naar noen avmeldes.

Struktur i dokumentbiblioteket:
  Kurs/<kurskode>/Presentasjoner/...   -> vises for alle paameldte
  Kurs/<kurskode>/Deltakere/<epost>/... -> personlige dokumenter (kontrakt o.l.)

Demo: samme struktur under data/sharepoint_demo/.

Microsoft Graph (drift) - dokumenterte kall, testet med etterligning (tests/test_integrasjoner.py), IKKE mot en ekte
tenant ennå:
  * mappe:  POST /sites/{site}/drive/root[:/{forelder}:]/children  {"name", "folder": {}, conflictBehavior: "fail"}
            409 (nameAlreadyExists) = mappen finnes - kallet er dermed idempotent og kan aldri erstatte en mappe.
            Graph lager ikke mellomnivåer selv, så Kurs, Kurs/<kode> og undermappene lages hver for seg.
  * liste:  GET  .../drive/root:/{mappe}:/children
  * hent:   GET  .../drive/root:/{sti}:/content          (404 -> FileNotFoundError)
  * last opp: PUT .../drive/root:/{sti}:/content         (enkel opplasting, filer opp til 4 MB i ett kall)
"""
import re
from urllib.parse import quote

import requests

from .. import config
from . import m365

DEMO_ROT = config.ROT / "data" / "sharepoint_demo"
_KURSKODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,79}")      # db.generer_kode lager f.eks. EFT-2027-2
UNDERMAPPER = ("Presentasjoner", "Deltakere")


def _sikre_mappe(forelder: str, navn: str) -> None:
    """Lager mappen `navn` under `forelder` ('' = rot) hvis den ikke finnes. Eksisterer den (409), er alt i orden."""
    sted = f":/{quote(forelder)}:" if forelder else ""
    try:
        m365.graph("POST", f"/sites/{config.SHAREPOINT_SITE_ID}/drive/root{sted}/children",
                   json={"name": navn, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})
    except requests.HTTPError as e:
        if e.response is None or e.response.status_code != 409:
            raise


def opprett_kursmappe(kurskode: str) -> str:
    """Kurs/<kode> med undermappene. Idempotent: kan kjøres på nytt (f.eks. etter en feil) uten å røre innholdet."""
    if not _KURSKODE.fullmatch(kurskode or ""):          # fullmatch: ogsaa et avsluttende linjeskift avvises
        raise ValueError("Ugyldig kurskode for SharePoint-mappe")
    mappe = f"Kurs/{kurskode}"
    if config.DEMO:
        for under in UNDERMAPPER:
            (DEMO_ROT / mappe / under).mkdir(parents=True, exist_ok=True)
        return mappe
    _sikre_mappe("", "Kurs")
    _sikre_mappe("Kurs", kurskode)
    for under in UNDERMAPPER:
        _sikre_mappe(mappe, under)
    return mappe


def list_filer(mappe: str) -> list[dict]:
    """[{'navn','sti','storrelse'}] – sti brukes senere i hent_fil()."""
    if config.DEMO:
        rot = DEMO_ROT / mappe
        return [{"navn": f.name, "sti": f"{mappe}/{f.name}", "storrelse": f.stat().st_size}
                for f in sorted(rot.glob("*")) if f.is_file()] if rot.exists() else []
    try:
        d = m365.graph("GET", f"/sites/{config.SHAREPOINT_SITE_ID}/drive/root:/{quote(mappe)}:/children").json()
    except Exception:
        return []
    return [{"navn": i["name"], "sti": f"{mappe}/{i['name']}", "storrelse": i.get("size", 0)}
            for i in d.get("value", []) if "file" in i]


def hent_fil(sti: str) -> bytes:
    if config.DEMO:
        full = (DEMO_ROT / sti).resolve()
        if DEMO_ROT.resolve() not in full.parents:
            raise PermissionError(sti)
        return full.read_bytes()
    try:
        return m365.graph("GET", f"/sites/{config.SHAREPOINT_SITE_ID}/drive/root:/{quote(sti)}:/content").content
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            raise FileNotFoundError(sti) from None
        raise


def last_opp(mappe: str, filnavn: str, innhold: bytes) -> str:
    sti = f"{mappe}/{filnavn}"
    if config.DEMO:
        mal = DEMO_ROT / sti
        mal.parent.mkdir(parents=True, exist_ok=True)
        mal.write_bytes(innhold)
        return sti
    m365.graph("PUT", f"/sites/{config.SHAREPOINT_SITE_ID}/drive/root:/{quote(sti)}:/content", data=innhold)
    return sti
