"""Sikkerhetsmekanismer for webappen: CSRF, sikkerhetshoder, takbegrensning (rate limiting), trygge viderekoblinger,
signerte lenker, CSV-beskyttelse og produksjonskontroll av konfigurasjonen.

Ingen eksterne avhengigheter (CLAUDE.md: ingen nye rammeverk uten god grunn). Alt er små, lesbare funksjoner som
kobles paa Flask-appen i app.py via installer(app).
"""
import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
import uuid
from collections import deque
from urllib.parse import urlsplit

from flask import abort, g, render_template, request, session

from .. import config, lenker
from ..integrasjoner import sertifikat

logg = logging.getLogger("kurs.sikkerhet")

# ============================ CSRF ============================
# Synchronizer token: et tilfeldig token per sesjon (i den signerte sesjonscookien), som maa sendes med i hvert
# state-endrende skjema (skjult felt csrf_token) eller som hodet X-CSRF-Token. Sammenlignes med hmac.compare_digest.
# Unntak (CSRF_FRITATT): endepunkter uten cookie-basert tilgang - webhooken autentiseres med HMAC-signatur.

CSRF_FELT = "csrf_token"
CSRF_HODE = "X-CSRF-Token"
CSRF_FRITATT = frozenset({"api_paamelding"})
_TRYGGE_METODER = frozenset({"GET", "HEAD", "OPTIONS"})


def csrf_token() -> str:
    """Sesjonens CSRF-token (lages ved foerste bruk). Tilgjengelig i maler som csrf_token()."""
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def _csrf_ok() -> bool:
    forventet = session.get("csrf")
    mottatt = request.form.get(CSRF_FELT) or request.headers.get(CSRF_HODE) or ""
    return bool(forventet) and hmac.compare_digest(str(forventet), str(mottatt))


def _csrf_sjekk():
    if request.method in _TRYGGE_METODER or request.endpoint in CSRF_FRITATT:
        return None
    if _csrf_ok():
        return None
    logg.warning("CSRF avvist [%s] %s", getattr(g, "request_id", "-"), request.url_rule)
    return render_template("feil.html", tittel="Skjemaet kunne ikke sendes",
                           tekst="Siden hadde ligget for lenge åpen, eller innsendingen kom ikke fra dette systemet. "
                                 "Gå tilbake, last siden på nytt og prøv igjen."), 400


# ============================ takbegrensning ============================
# Enkel glidende-vindu-teller i prosessen (ingen ekstern tjeneste). Med flere gunicorn-workere gjelder grensen per
# worker - dokumentert i SECURITY.md. Formaalet er aa stoppe passordgjetting, masseutsending av innloggingslenker,
# misbruk av KI-assistenten og skjemaspam - ikke aa vaere en presis global kvote.

class Takbegrenser:
    def __init__(self):
        self._laas = threading.Lock()
        self._treff: dict[str, deque] = {}

    def tillat(self, nokkel: str, maks: int, sekunder: int, naa: float | None = None) -> bool:
        """True hvis nokkelen har hatt faerre enn `maks` treff de siste `sekunder` - og registrerer dette treffet."""
        naa = time.monotonic() if naa is None else naa
        with self._laas:
            ko = self._treff.setdefault(nokkel, deque())
            while ko and ko[0] <= naa - sekunder:
                ko.popleft()
            if len(ko) >= maks:
                return False
            ko.append(naa)
            if len(self._treff) > 20000:   # enkel opprydding: kast tomme koeer
                for k in [k for k, v in self._treff.items() if not v]:
                    del self._treff[k]
            return True

    def nullstill(self) -> None:
        with self._laas:
            self._treff.clear()


takbegrenser = Takbegrenser()

# (maks, sekunder) per formaal. Nokkelen bygges av formaal + klientens IP (og ev. et ekstra felt, f.eks. brukernavn).
GRENSER = {
    "admin_login": (10, 15 * 60),
    "admin_login_bruker": (20, 15 * 60),
    "innloggingslenke": (30, 15 * 60),         # var (5, 15 * 60): kurslokaler og arbeidsplasser deler IP-adresse
    "innloggingslenke_epost": (3, 15 * 60),
    "sporsmal": (10, 10 * 60),
    "paamelding": (20, 10 * 60),
    "evaluering": (30, 10 * 60),         # svar på evalueringen etter kurset (/evaluering/<lenke>)
    "enhetsoppslag": (120, 10 * 60),     # firmaoppslag i påmeldingsskjemaet (Enhetsregisteret)
    "min_side_lenke": (200, 10 * 60),    # den personlige lenken til Min side (/min/<lenke>), per IP: kurslokaler og store arbeidsplasser deler IP
    "innsjekk_qr": (120, 10 * 60),       # QR-skann: nøkkel = ip + «|» + token
    "innsjekk_qr_epost": (10, 10 * 60),  # nøkkel = e-post + «|» + ip
    "oppmote_selv": (10, 10 * 60),       # «Registrer oppmøte i dag» på nett: nøkkel = deltaker-id
    "kursside_opplasting": (60, 10 * 60),   # filopplasting på kurssiden: nøkkel = admin-id
    "kursside_fil": (60, 10 * 60),       # nedlasting av dokumenter (kan være 15 MB) fra kurssiden: nøkkel = deltaker-id
    "kursside_bilde": (600, 10 * 60),    # bilder på kurssiden (hver sidevisning henter dem): nøkkel = deltaker-id
    "webhook": (60, 60),
    "admin_sok": (120, 60),        # deltakersøket (rullegardin og resultatside, felles teller): 2 per sekund varig, mer enn tasting
}

# Endepunkter der adressen kan inneholde søketekst (navn, e-post, telefon) eller en personlig nøkkel (lenken til Min side, /min/<lenke>).
# Svarene sendes med Referrer-Policy: no-referrer, så ?q=... og lenken aldri følger med som Referer når man klikker seg videre.
REFERRER_INGEN = frozenset({"admin_sok", "admin_sok_json", "min_side_lenke"})


def klient_ip() -> str:
    return request.remote_addr or "ukjent"


def krev_kvote(formaal: str, ekstra: str = "") -> None:
    """Avbryter med 429 naar grensen for formaalet er naadd for denne klienten (IP) og ev. `ekstra` (f.eks. e-post,
    normalisert). Selve verdien i `ekstra` lagres kun som hash - aldri i klartekst."""
    maks, sekunder = GRENSER[formaal]
    nokkel = f"{formaal}:{klient_ip()}"
    if ekstra:
        nokkel = f"{formaal}:{hashlib.sha256(ekstra.strip().lower().encode()).hexdigest()[:24]}"
    if not takbegrenser.tillat(nokkel, maks, sekunder):
        logg.warning("Takbegrensning [%s] %s", getattr(g, "request_id", "-"), formaal)
        abort(429)


# ============================ sikkerhetshoder ============================

def _csp(nonce: str) -> str:
    # Inline <style> i base.html og style-attributter i malene -> 'unsafe-inline' for stil (ingen kjente stil-baserte
    # angrep uten script). Script: KUN egne filer og <script nonce=...>. Ingen inline-haandtere (onclick=...) - de er
    # erstattet av data-attributter i static/app.js, som ogsaa fjerner JS-kontekst-injeksjon fra maldata.
    return ("default-src 'self'; script-src 'self' 'nonce-" + nonce + "'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "form-action 'self'; base-uri 'self'; object-src 'none'")


def csp_for_egen_ramme(nonce: str) -> str:
    """Som _csp, men siden kan vises i en ramme (iframe) på SAMME nettsted. Brukes kun av forhåndsvisningen av kurssiden i
    redigeringsvisningen; alle andre sider har frame-ancestors 'none' (og X-Frame-Options DENY)."""
    return _csp(nonce).replace("frame-ancestors 'none'", "frame-ancestors 'self'")


def _sett_hoder(respons):
    respons.headers.setdefault("X-Content-Type-Options", "nosniff")
    respons.headers.setdefault("X-Frame-Options", "DENY")
    respons.headers.setdefault("Referrer-Policy",
                               "no-referrer" if request.endpoint in REFERRER_INGEN else "strict-origin-when-cross-origin")
    respons.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    respons.headers.setdefault("Content-Security-Policy", _csp(g.get("csp_nonce", "")))
    if request.is_secure and not config.DEMO:
        respons.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if "Cache-Control" not in respons.headers and (session.get("admin_id") or session.get("deltaker_id")):
        respons.headers["Cache-Control"] = "no-store"   # innloggede sider inneholder personopplysninger
    return respons


# ============================ viderekobling og signerte lenker ============================

def trygg_neste(verdi, standard: str) -> str:
    """Kun en relativ sti paa DETTE nettstedet (aldri //annet.sted eller https://...). Ellers standard."""
    if not verdi or not isinstance(verdi, str):
        return standard
    if re.search(r"[\x00-\x1f\x7f-\x9f]", verdi):      # linjeskift, tabulator m.m.: urlsplit fjerner dem før kontrollen, og en nettleser tolker dem annerledes
        return standard
    deler = urlsplit(verdi)
    if deler.scheme or deler.netloc or not verdi.startswith("/") or verdi.startswith("//") or "\\" in verdi:
        return standard
    return verdi


signatur, signatur_ok = lenker.signatur, lenker.signatur_ok   # se kurs/lenker.py (ren modul, deles med e-post)


def trygt_url_felt(verdi: str | None) -> str | None:
    """Lenker admin lagrer (Zoom-lenke, dokument-URL) maa vaere http(s) - aldri javascript:/data: som ville blitt en
    lenke deltakerne klikker paa. Returnerer verdien eller None hvis tom; ValueError hvis ugyldig."""
    if not verdi:
        return None
    verdi = verdi.strip()
    deler = urlsplit(verdi)
    if deler.scheme not in ("http", "https") or not deler.netloc or any(c in verdi for c in "\r\n\t\x00"):
        raise ValueError("Lenken må starte med https:// (eller http://).")
    return verdi


# ============================ CSV ============================

_FORMELTEGN = ("=", "+", "-", "@", "\t", "\r")


def csv_trygg(verdi):
    """Regneark tolker celler som begynner med = + - @ som formler (CSV-injeksjon). Slike verdier faar en ledende
    apostrof, som Excel viser som tekst. Tall og tomme verdier roeres ikke."""
    if isinstance(verdi, str) and verdi and verdi[0] in _FORMELTEGN:
        return "'" + verdi
    return verdi


# ============================ produksjonskontroll ============================

SVAKE_NOKLER = frozenset({"", "bytt-meg-i-prod", "lang-tilfeldig-streng", "demo-webhook-hemmelighet", "demo"})


def produksjonsfeil() -> list[str]:
    """Konfigurasjonsfeil som gjoer det utrygt aa kjoere i drift (MODUS=prod). Tom liste = ok. Tekstene er trygge aa
    logge (inneholder aldri verdiene)."""
    feil = []
    if config.DEMO:
        return feil
    if config.HEMMELIG_NOKKEL in SVAKE_NOKLER or len(config.HEMMELIG_NOKKEL) < 32:
        feil.append("HEMMELIG_NOKKEL maa vaere en tilfeldig streng paa minst 32 tegn")
    if config.WEBHOOK_HEMMELIG in SVAKE_NOKLER or len(config.WEBHOOK_HEMMELIG) < 32:
        feil.append("WEBHOOK_HEMMELIG maa vaere en tilfeldig streng paa minst 32 tegn")
    if not config.BASE_URL.startswith("https://"):
        feil.append("BASE_URL maa vaere https:// i drift")
    entra_ok = bool(config.ENTRA_TENANT_ID and config.ENTRA_CLIENT_ID and (config.ENTRA_SERTIFIKAT or config.ENTRA_CLIENT_SECRET))
    # Et sertifikat som ikke kan leses, stopper oppstarten med en tydelig linje i stedet for at innlogging og e-post
    # feiler senere (innholdet skrives aldri ut)
    for navn, sert, passord in (("M365_SERTIFIKAT", config.M365_SERTIFIKAT, config.M365_SERTIFIKAT_PASSORD),
                                ("ENTRA_SERTIFIKAT", config.ENTRA_SERTIFIKAT, config.ENTRA_SERTIFIKAT_PASSORD)):
        if sert:
            try:
                sertifikat.kontroller(sert, passord)
            except sertifikat.Sertifikatfeil as e:
                feil.append(f"{navn}: {e}")
    if not entra_ok and not config.ADMIN_LOKAL_INNLOGGING:
        feil.append("Ingen innloggingsvei for admin: sett opp Entra ID (ENTRA_*) eller ADMIN_LOKAL_INNLOGGING=1")
    return feil


# ============================ kobling til appen ============================

def installer(app) -> None:
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def _forbered():
        g.request_id = uuid.uuid4().hex[:12]
        g.csp_nonce = secrets.token_urlsafe(16)
        return _csrf_sjekk()

    @app.context_processor
    def _nonce():
        return {"csp_nonce": g.get("csp_nonce", "")}

    app.after_request(_sett_hoder)
