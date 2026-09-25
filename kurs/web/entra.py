"""Admin-innlogging med Microsoft Entra ID (OpenID Connect, authorization code + PKCE) - fase 13.

Produksjonsmodellen for admin-tilgang: ansatte logger inn med IPR-kontoen sin, og ROLLEN i systemet kommer fra Entra
(app-roller i app-registreringen, eller sikkerhetsgrupper) ved hver innlogging. Ingen passord lagres for disse brukerne.
Lokal brukernavn/passord-innlogging beholdes som utviklingsmekanisme (demo) og som noedbrukere i drift
(ADMIN_LOKAL_INNLOGGING=1).

Flyt:
  1. /admin/logg-inn/entra        -> lager state, nonce og PKCE-verifier i sesjonen, sender brukeren til Microsoft
  2. /admin/logg-inn/entra/svar   -> kontrollerer state, bytter code mot tokens DIREKTE mot token-endepunktet (TLS +
                                     client_secret + code_verifier), leser id_token, kontrollerer iss/aud/tid/exp/nonce,
                                     finner rollen, henter/oppretter brukeren og logger inn.

Hvorfor id_token ikke signaturvalideres her: tokenet mottas i direkte, autentisert TLS-kommunikasjon med Microsofts
token-endepunkt (OIDC Core 3.1.3.7 tillater da TLS-validering i stedet for signaturkontroll). Vi stoler ALDRI paa
noe som kommer via nettleseren (kun `code` og `state`, som begge kontrolleres). Ingen ekstra avhengigheter.

Oppsett i Entra (IT), se AZURE-SETUP.md:
  * App-registrering «IPR Kurs admin» (web). Redirect URI: <BASE_URL>/admin/logg-inn/entra/svar
  * Delegerte tillatelser: openid, profile, email (ingen admin consent ut over standard)
  * App roles: ipr.system, ipr.kursadmin, ipr.lese - tildel brukere/grupper under Enterprise applications
  * (alternativ: «groups» claim i ID token, og ENTRA_GRUPPER=<oid>=rolle,...)
  * «Assignment required» = Yes, slik at bare tildelte brukere kan logge inn
"""
import base64
import hashlib
import json
import logging
import secrets
import time
from urllib.parse import urlencode

import requests
from flask import abort, flash, redirect, render_template, request, session, url_for

from .. import config, db

logg = logging.getLogger("kurs.entra")

MYNDIGHET = "https://login.microsoftonline.com"
SCOPE = "openid profile email"
STATE_LEVETID_SEK = 10 * 60


def aktiv() -> bool:
    return bool(config.ENTRA_TENANT_ID and config.ENTRA_CLIENT_ID and config.ENTRA_CLIENT_SECRET)


def _kart(tekst: str) -> dict:
    """'a=system,b=kursadmin' -> {'a': 'system', 'b': 'kursadmin'} (ukjente roller ignoreres)."""
    ut = {}
    for del_ in (tekst or "").split(","):
        if "=" in del_:
            nokkel, rolle = (x.strip() for x in del_.split("=", 1))
            if nokkel and rolle in db.ROLLER:
                ut[nokkel] = rolle
    return ut


def rolle_fra_claims(claims: dict) -> str | None:
    """Hoeyeste rolle brukeren har krav paa (system > kursadmin > lese) ut fra `roles` (app-roller) og `groups`.
    None = ingen tilgang."""
    kandidater = set()
    roller, grupper = _kart(config.ENTRA_ROLLER), _kart(config.ENTRA_GRUPPER)
    for verdi in claims.get("roles") or []:
        if verdi in roller:
            kandidater.add(roller[verdi])
    for verdi in claims.get("groups") or []:
        if verdi in grupper:
            kandidater.add(grupper[verdi])
    for rolle in db.ROLLER:               # rekkefoelgen er system, kursadmin, lese
        if rolle in kandidater:
            return rolle
    return None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def les_id_token(id_token: str) -> dict:
    """Payload-delen av et JWT (ingen signaturkontroll - se modulens docstring). Ugyldig format -> ValueError."""
    deler = id_token.split(".")
    if len(deler) != 3:
        raise ValueError("id_token har ikke JWT-format")
    payload = deler[1] + "=" * (-len(deler[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def kontroller_claims(claims: dict, nonce: str, naa: float | None = None) -> None:
    """iss/tid (vaar tenant), aud (vaar app), exp/nbf (gyldig naa), nonce (denne innloggingen). ValueError ellers."""
    naa = time.time() if naa is None else naa
    if claims.get("iss") != f"{MYNDIGHET}/{config.ENTRA_TENANT_ID}/v2.0" or claims.get("tid") != config.ENTRA_TENANT_ID:
        raise ValueError("feil utsteder")
    if claims.get("aud") != config.ENTRA_CLIENT_ID:
        raise ValueError("feil mottaker")
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < naa - 60:
        raise ValueError("utloept")
    if isinstance(claims.get("nbf"), (int, float)) and claims["nbf"] > naa + 60:
        raise ValueError("ikke gyldig ennaa")
    if not nonce or claims.get("nonce") != nonce:
        raise ValueError("feil nonce")
    if not claims.get("oid"):
        raise ValueError("mangler oid")


def _redirect_uri() -> str:
    return f"{config.BASE_URL}/admin/logg-inn/entra/svar"


def installer(app, con, logg_inn_admin) -> None:
    """Registrerer rutene. `con()` gir databasetilkoblingen for requesten, `logg_inn_admin(bruker)` setter opp sesjonen
    (samme funksjon som lokal innlogging bruker)."""

    @app.get("/admin/logg-inn/entra")
    def admin_entra_start():
        if not aktiv():
            abort(404)
        verifier = _b64url(secrets.token_bytes(48))
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        session["entra"] = {"state": state, "nonce": nonce, "verifier": verifier, "ts": time.time(),
                            "neste": request.args.get("neste") or ""}
        params = {
            "client_id": config.ENTRA_CLIENT_ID, "response_type": "code", "redirect_uri": _redirect_uri(),
            "response_mode": "query", "scope": SCOPE, "state": state, "nonce": nonce,
            "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256",
        }
        return redirect(f"{MYNDIGHET}/{config.ENTRA_TENANT_ID}/oauth2/v2.0/authorize?{urlencode(params)}")

    @app.get("/admin/logg-inn/entra/svar")
    def admin_entra_svar():
        if not aktiv():
            abort(404)
        ventet = session.pop("entra", None) or {}
        state, code = request.args.get("state", ""), request.args.get("code", "")
        if request.args.get("error"):
            logg.warning("Entra avviste innloggingen: %s", request.args.get("error"))   # kun feilkoden, aldri beskrivelsen
            flash("Innloggingen med Microsoft ble avbrutt.", "feil")
            return redirect(url_for("admin_login"))
        if (not ventet or not code or not secrets.compare_digest(state, ventet.get("state", ""))
                or time.time() - ventet.get("ts", 0) > STATE_LEVETID_SEK):
            flash("Innloggingen kunne ikke fullføres. Prøv igjen.", "feil")
            return redirect(url_for("admin_login")), 400
        try:
            svar = requests.post(
                f"{MYNDIGHET}/{config.ENTRA_TENANT_ID}/oauth2/v2.0/token",
                data={"client_id": config.ENTRA_CLIENT_ID, "client_secret": config.ENTRA_CLIENT_SECRET,
                      "grant_type": "authorization_code", "code": code, "redirect_uri": _redirect_uri(),
                      "code_verifier": ventet["verifier"], "scope": SCOPE},
                timeout=20)
            svar.raise_for_status()
            claims = les_id_token(svar.json()["id_token"])
            kontroller_claims(claims, ventet.get("nonce", ""))
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            logg.warning("Entra-innlogging feilet: %s", type(e).__name__)   # aldri tokens/claims i loggen
            flash("Innloggingen med Microsoft kunne ikke bekreftes. Prøv igjen, eller kontakt IT.", "feil")
            return redirect(url_for("admin_login")), 403
        rolle = rolle_fra_claims(claims)
        if not rolle:
            db.logg(con(), "admin_innlogging_avvist", {"grunn": "ingen_rolle", "entra": True})
            con().commit()
            return render_template("feil.html", tittel="Ingen tilgang",
                                   tekst="Kontoen din er ikke gitt tilgang til kursadministrasjonen. "
                                         "Be IT legge deg til i riktig rolle/gruppe."), 403
        bruker = db.finn_eller_opprett_entra_bruker(
            con(), claims["oid"], claims.get("name") or "", claims.get("preferred_username") or claims.get("email"), rolle)
        con().commit()
        if bruker is None:
            return render_template("feil.html", tittel="Ingen tilgang",
                                   tekst="Brukeren din er deaktivert i kursadministrasjonen."), 403
        return logg_inn_admin(bruker, ventet.get("neste"))
