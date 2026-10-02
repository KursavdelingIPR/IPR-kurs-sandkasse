"""Kurslisten på Oversikten (tidligere Aktiviteter → Liste): sorteringen av kurslisten og visningen av hver rad.

«Sorter etter»-valget og de klikkbare kolonneoverskriftene bruker de samme verdiene (f.eks. «start_asc»). Sorteringen
skjer her (i Python), slik at kursnavn sorteres norsk (æ, ø, å etter z) uavhengig av databasen. Søk og filtre virker som
før, og sidenummereringen skjer etter sorteringen.

Standard er «Kommende først»: kurs som ikke er ferdige (siste kursdag er i dag eller senere) med snarest oppstart først,
deretter tidligere kurs med det nyeste først. Tidligere brukte standarden kursstatus («avsluttet» sist) - datoene gir
samme rekkefølge, men fanger også kurs der statusen ikke er oppdatert ennå.
"""
from datetime import date

from .db import PAAMELDINGSSTATUSER_FLERTALL
from .deltakerliste import norsk_sortering

STANDARD = "kommende"
# Valgene i «Sorter etter» (verdi -> tekst), i den rekkefølgen de vises
SORTERINGER = {
    "kommende": "Kommende først",
    "start_asc": "Oppstart – tidligste først",
    "start_desc": "Oppstart – seneste først",
    "opprettet_desc": "Opprettet – sist opprettet først",
    "opprettet_asc": "Opprettet – eldste først",
    "navn_asc": "Kursnavn A–Å",
    "navn_desc": "Kursnavn Å–A",
}
# Kolonner som også kan sorteres ved å klikke på overskriften (felt -> tekst)
KOLONNER = {"kursnr": "Kursnr", "navn": "Tittel", "start": "Start", "status": "Status", "ansvarlig": "Ansvarlig"}
# Eldre lenker brukte sorter=<kolonne>&retning=asc|desc
_ELDRE = {"tittel": "navn", "start": "start", "kursnr": "kursnr", "status": "status", "ansvarlig": "ansvarlig",
          "paameldte": "paameldte"}
_GYLDIGE = set(SORTERINGER) | {f"{f}_{r}" for f in (*KOLONNER, "paameldte") for r in ("asc", "desc")}


def tolk(sorter: str | None, retning: str | None = None) -> str:
    """Sorteringen fra adressen. Ukjent eller tom verdi gir standarden."""
    sorter = (sorter or "").strip()
    if sorter in _ELDRE:
        sorter = f"{_ELDRE[sorter]}_{'desc' if retning == 'desc' else 'asc'}"
    return sorter if sorter in _GYLDIGE else STANDARD


def beskrivelse(sortering: str) -> str:
    """Teksten i «Sorter etter» - også for en sortering valgt ved å klikke på en kolonneoverskrift."""
    if sortering in SORTERINGER:
        return SORTERINGER[sortering]
    felt, _, retning = sortering.rpartition("_")
    navn = {**KOLONNER, "paameldte": PAAMELDINGSSTATUSER_FLERTALL["paameldt"]}.get(felt, felt)
    return f"{navn} {'↓' if retning == 'desc' else '↑'}"


def sorter(rader, sortering: str, idag: date) -> list:
    """Radene (med start, slutt, opprettet, navn, kursnr, status, ansvarlig_navn, bekreftet) i valgt rekkefølge."""
    rader = list(rader)
    if sortering == STANDARD:
        idag_iso = idag.isoformat()
        kommende = [r for r in rader if not r["slutt"] or r["slutt"] >= idag_iso]
        tidligere = [r for r in rader if r["slutt"] and r["slutt"] < idag_iso]
        kommende.sort(key=lambda r: (not r["start"], r["start"] or "", r["id"]))
        tidligere.sort(key=lambda r: (r["start"], r["id"]), reverse=True)
        return kommende + tidligere
    felt, _, retning = sortering.rpartition("_")
    omvendt = retning == "desc"
    verdi = {
        "start": lambda r: r["start"],
        "opprettet": lambda r: r["opprettet"],
        "navn": lambda r: norsk_sortering(r["navn"]),
        "kursnr": lambda r: r["kursnr"],
        "status": lambda r: r["status"],
        "ansvarlig": lambda r: norsk_sortering(r["ansvarlig_navn"]) if r["ansvarlig_navn"] else None,
        "paameldte": lambda r: r["bekreftet"],
    }[felt]
    # Stabil sortering: først på id (samme rekkefølge ved like verdier), så på feltet
    rader.sort(key=lambda r: r["id"])
    med = sorted((r for r in rader if verdi(r) not in (None, "")), key=verdi, reverse=omvendt)
    uten = [r for r in rader if verdi(r) in (None, "")]
    return med + uten


def dato_tekst(iso: str | None) -> str:
    """'2026-09-18' -> '18.09.2026'"""
    iso = str(iso or "")[:10]
    return f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}" if len(iso) == 10 else ""


FORMAT = {"fysisk": "Fysisk", "digital": "Online", "hybrid": "Hybrid"}
# Kursstatus slik den vises i kurslisten og statusvalget (lagret verdi -> tekst; «aapen» skrives «Åpen»)
STATUSNAVN = {"utkast": "Utkast", "aapen": "Åpen", "full": "Full", "aktiv": "Aktiv", "avsluttet": "Avsluttet", "avlyst": "Avlyst"}


def sted_tekst(r) -> str:
    """Sted for fysiske kurs og hybridkurs (tomt for online)."""
    return (r["sted"] or "") if r["type"] != "digital" else ""
