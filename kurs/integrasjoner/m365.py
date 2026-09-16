"""Felles token for Microsoft Graph (brukes av epost.py og sharepoint.py).

Oppsett i Entra ID (Azure AD):
  1. App-registrering "IPR Kurs" -> Certificates & secrets -> ny client secret
  2. API permissions (Application): Mail.Send, Sites.Selected  -> Grant admin consent
  3. Begrens Mail.Send til kurs@-postboksen med en Application Access Policy (Exchange)
  4. Gi appen tilgang til KUN kurs-SharePoint-siten (Sites.Selected)
"""
import time

import requests

from .. import config

_cache: dict = {}


def token() -> str:
    if _cache.get("utloper", 0) > time.time() + 60:
        return _cache["token"]
    r = requests.post(
        f"https://login.microsoftonline.com/{config.M365_TENANT_ID}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": config.M365_CLIENT_ID,
            "client_secret": config.M365_CLIENT_SECRET,
            "scope": "https://graph.microsoft.com/.default",
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    _cache.update(token=data["access_token"], utloper=time.time() + data["expires_in"])
    return _cache["token"]


def graph(metode: str, sti: str, **kwargs) -> requests.Response:
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token()}"
    r = requests.request(metode, f"https://graph.microsoft.com/v1.0{sti}", headers=headers, timeout=60, **kwargs)
    r.raise_for_status()
    return r
