"""Tidspunkter til visning i norsk tid.

Databasen lagrer tidspunkter i UTC ('YYYY-MM-DD HH:MM:SS', se db.naa_utc). Uten omregning viser siden klokkeslett som er
én time feil om vinteren og to timer feil om sommeren. Omregningen bruker tidssonen Europe/Oslo, så sommer- og vintertid
blir riktig (tzdata i requirements.txt: Windows har ingen tidssonedatabase selv).
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

OSLO = ZoneInfo("Europe/Oslo")


def fra_utc(ts) -> datetime | None:
    """'2026-09-26 18:15:00' (UTC) -> 2026-09-26 20:15:00+02:00. None hvis verdien ikke er et tidspunkt."""
    try:
        tid = datetime.fromisoformat(str(ts).strip())
    except ValueError:
        return None
    if tid.tzinfo is None:
        tid = tid.replace(tzinfo=timezone.utc)
    return tid.astimezone(OSLO)


def dato(tid: datetime | None) -> str:
    """26.09.2026"""
    return tid.strftime("%d.%m.%Y") if tid else ""


def klokkeslett(tid: datetime | None, *, sekunder: bool = False) -> str:
    """20:15 (eller 20:15:42 med sekunder=True)"""
    return tid.strftime("%H:%M:%S" if sekunder else "%H:%M") if tid else ""
