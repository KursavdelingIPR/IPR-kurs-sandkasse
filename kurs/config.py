"""Konfigurasjon lest fra miljovariabler / .env.

MODUS=demo  -> ingen ekte integrasjoner. E-post havner i utboks/, Visma/Zoom/SharePoint logges.
MODUS=prod  -> ekte API-kall. Krever at nokler er satt (se .env.example).
"""
import os
from pathlib import Path

ROT = Path(__file__).resolve().parent.parent


def _last_env() -> None:
    env = ROT / ".env"
    if not env.exists():
        return
    for linje in env.read_text(encoding="utf-8").splitlines():
        linje = linje.strip()
        if linje and not linje.startswith("#") and "=" in linje:
            k, v = linje.split("=", 1)
            v = v.split(" #", 1)[0].strip()  # fjern kommentar på slutten av linjen
            os.environ.setdefault(k.strip(), v)


_last_env()


def get(navn: str, standard: str = "") -> str:
    return os.environ.get(navn, standard)


MODUS = get("MODUS", "demo")
DEMO = MODUS != "prod"
DB_STI = Path(get("DB_STI", str(ROT / "data" / "kurs.db")))
# Tom (standard) = SQLite i DB_STI, som i den lokale sandkassen. Satt = PostgreSQL, f.eks.
# postgresql://bruker:passord@server.postgres.database.azure.com:5432/ipr?sslmode=require (hemmelig: Key Vault i drift).
DATABASE_URL = get("DATABASE_URL")
UTBOKS = ROT / "utboks"
BASE_URL = get("BASE_URL", "http://127.0.0.1:5000")
HEMMELIG_NOKKEL = get("HEMMELIG_NOKKEL", "bytt-meg-i-prod")
# Brukes kun til aa opprette den aller forste admin-brukeren (se db.init). Etter det logger
# hver ansatt inn med sin egen bruker og sitt eget passord, opprettet under Admin -> Brukere.
ADMIN_BRUKERNAVN = get("ADMIN_BRUKERNAVN", "admin")
ADMIN_PASSORD = get("ADMIN_PASSORD", "demo")

AVSENDER_EPOST = get("AVSENDER_EPOST", "kurs@ipr.no")
AVSENDER_NAVN = get("AVSENDER_NAVN", "IPR Påmeldingssystem")
ADMIN_EPOST = get("ADMIN_EPOST", "admin@ipr.no")

# Microsoft 365 (e-post via Graph + SharePoint) – én app-registrering i Entra ID
M365_TENANT_ID = get("M365_TENANT_ID")
M365_CLIENT_ID = get("M365_CLIENT_ID")
M365_CLIENT_SECRET = get("M365_CLIENT_SECRET")
SHAREPOINT_SITE_ID = get("SHAREPOINT_SITE_ID")

# Zoom Server-to-Server OAuth
ZOOM_ACCOUNT_ID = get("ZOOM_ACCOUNT_ID")
ZOOM_CLIENT_ID = get("ZOOM_CLIENT_ID")
ZOOM_CLIENT_SECRET = get("ZOOM_CLIENT_SECRET")

# Visma (antatt Visma eAccounting / eRegnskap – MAA bekreftes i moetet)
VISMA_CLIENT_ID = get("VISMA_CLIENT_ID")
VISMA_CLIENT_SECRET = get("VISMA_CLIENT_SECRET")
VISMA_REFRESH_TOKEN = get("VISMA_REFRESH_TOKEN")
VISMA_API = get("VISMA_API", "https://eaccountingapi.vismaonline.com/v2")

# Mottak fra nettsidens skjema (erstatter innsending til Pindena)
WEBHOOK_HEMMELIG = get("WEBHOOK_HEMMELIG", "demo-webhook-hemmelighet")

# KI-assistent (Claude). Uten nokkel / i demo brukes enkel ordmatching mot kunnskapsbasen.
ANTHROPIC_API_KEY = get("ANTHROPIC_API_KEY")
ASSISTENT_MODELL = get("ASSISTENT_MODELL", "claude-opus-5")
ASSISTENT_AKTIV = get("ASSISTENT_AKTIV", "1") == "1"          # 0 = «Spør oss»-siden er skrudd av (404)
ASSISTENT_MAKS_PER_DAG = int(get("ASSISTENT_MAKS_PER_DAG", "300"))  # kostnadsgrense: KI-kall per dag, deretter kun til adm
ASSISTENT_TIDSAVBRUDD_SEK = int(get("ASSISTENT_TIDSAVBRUDD_SEK", "20"))

# Personvern
SLETT_SENSITIVT_ETTER_DAGER = int(get("SLETT_SENSITIVT_ETTER_DAGER", "14"))
