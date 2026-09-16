"""Kursmateriell i SharePoint.

Designvalg: deltakerne faar ALDRI direkte tilgang til SharePoint. Min side henter filen via appen
og sjekker at deltakeren er paameldt kurset. Da slipper IPR ekstern deling/gjestebrukere i M365,
og tilgang forsvinner automatisk naar noen avmeldes.

Struktur i dokumentbiblioteket:
  Kurs/<kurskode>/Presentasjoner/...   -> vises for alle paameldte
  Kurs/<kurskode>/Deltakere/<epost>/... -> personlige dokumenter (kontrakt o.l.)

Demo: samme struktur under data/sharepoint_demo/.
"""
from pathlib import Path
from urllib.parse import quote

from .. import config
from . import m365

DEMO_ROT = config.ROT / "data" / "sharepoint_demo"


def opprett_kursmappe(kurskode: str) -> str:
    if config.DEMO:
        for under in ("Presentasjoner", "Deltakere"):
            (DEMO_ROT / "Kurs" / kurskode / under).mkdir(parents=True, exist_ok=True)
        return f"Kurs/{kurskode}"
    for under in ("Presentasjoner", "Deltakere"):
        m365.graph("POST", f"/sites/{config.SHAREPOINT_SITE_ID}/drive/root:/Kurs/{quote(kurskode)}:/children", json={
            "name": under, "folder": {}, "@microsoft.graph.conflictBehavior": "replace"})
    return f"Kurs/{kurskode}"


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
    return m365.graph("GET", f"/sites/{config.SHAREPOINT_SITE_ID}/drive/root:/{quote(sti)}:/content").content


def last_opp(mappe: str, filnavn: str, innhold: bytes) -> str:
    sti = f"{mappe}/{filnavn}"
    if config.DEMO:
        mal = DEMO_ROT / sti
        mal.parent.mkdir(parents=True, exist_ok=True)
        mal.write_bytes(innhold)
        return sti
    m365.graph("PUT", f"/sites/{config.SHAREPOINT_SITE_ID}/drive/root:/{quote(sti)}:/content", data=innhold)
    return sti
