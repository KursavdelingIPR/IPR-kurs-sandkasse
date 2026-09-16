"""Zoom Server-to-Server OAuth – som i referansen.

Scopes: meeting:write, meeting:read, report:read:list_meeting_participants
NB: Etter aa ha lagt til scopes maa appen deaktiveres + reaktiveres for at token skal faa dem.
"""
import base64
import secrets
import time
import urllib.parse

import requests

from .. import config

API = "https://api.zoom.us/v2"
_cache: dict = {}


def _token() -> str:
    if _cache.get("utloper", 0) > time.time() + 60:
        return _cache["token"]
    auth = base64.b64encode(f"{config.ZOOM_CLIENT_ID}:{config.ZOOM_CLIENT_SECRET}".encode()).decode()
    r = requests.post(
        "https://zoom.us/oauth/token",
        params={"grant_type": "account_credentials", "account_id": config.ZOOM_ACCOUNT_ID},
        headers={"Authorization": f"Basic {auth}"},
        timeout=30,
    )
    r.raise_for_status()
    d = r.json()
    _cache.update(token=d["access_token"], utloper=time.time() + d["expires_in"])
    return _cache["token"]


def _kall(metode: str, sti: str, **kw) -> dict:
    r = requests.request(metode, API + sti, headers={"Authorization": f"Bearer {_token()}"}, timeout=30, **kw)
    r.raise_for_status()
    return r.json() if r.content else {}


def opprett_mote(kursnavn: str) -> dict:
    """Ett gjentakende mote (type 3, uten fast tid) pr kurs. Gjenbrukes alle kursdager."""
    if config.DEMO:
        mid = str(secrets.randbelow(10**10)).zfill(10)
        return {"id": mid, "join_url": f"https://zoom.us/j/{mid}?demo=1", "password": secrets.token_hex(3)}
    return _kall("POST", "/users/me/meetings", json={
        "topic": kursnavn,
        "type": 3,
        "settings": {"waiting_room": False, "join_before_host": True, "auto_recording": "none"},
    })


def deltakere_for_dato(mote_id: str, dato: str) -> list[dict]:
    """Deltakerliste for motet som ble holdt paa gitt dato: [{'navn','epost','minutter'}].

    For gjentakende moter maa vi forst finne riktig forekomst (UUID) via past_meetings/instances.
    """
    if config.DEMO:
        return []  # demo-oppmote legges inn av seed_demo.py
    forekomster = _kall("GET", f"/past_meetings/{mote_id}/instances").get("meetings", [])
    uuids = [m["uuid"] for m in forekomster if m.get("start_time", "").startswith(dato)]
    pr_epost: dict[str, dict] = {}
    for uuid in uuids:
        # UUID som starter med / eller inneholder // maa dobbel-URL-kodes
        kodet = urllib.parse.quote(urllib.parse.quote(uuid, safe=""), safe="")
        side = ""
        while True:
            d = _kall("GET", f"/report/meetings/{kodet}/participants", params={"page_size": 300, "next_page_token": side})
            for p in d.get("participants", []):
                nokkel = (p.get("user_email") or p.get("name", "")).lower()
                rad = pr_epost.setdefault(nokkel, {"navn": p.get("name"), "epost": p.get("user_email"), "minutter": 0})
                rad["minutter"] += round(p.get("duration", 0) / 60)  # samme person kan ha flere sesjoner
            side = d.get("next_page_token")
            if not side:
                break
    return list(pr_epost.values())
