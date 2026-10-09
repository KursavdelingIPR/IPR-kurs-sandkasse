"""Innlogging mot Microsoft (Entra ID) med sertifikat i stedet for client secret.

Serit (IT, 04.10.2026) anbefaler sertifikat, fordi Microsoft vil fase ut client secret. I stedet for å sende en hemmelig
tekst beviser appen hvem den er ved å signere en kort, engangs JWT («client assertion») med den private nøkkelen til
sertifikatet. Microsoft sjekker signaturen mot den offentlige delen som er lastet opp i app-registreringen. Den private
nøkkelen forlater aldri appen. Dokumentert av Microsoft: «Microsoft identity platform application authentication
certificate credentials».

Sertifikatet ligger i Key Vault og kommer inn som en innstilling (Key Vault-referanse til sertifikatets hemmelighet).
To formater godtas:
  * PEM-tekst med privat nøkkel og sertifikat (Key Vault med innholdstype PEM), eller
  * base64-kodet PKCS#12/PFX (Key Vault med innholdstype PKCS#12, standard), ev. med passord.

Ingenting herfra logges: verken nøkkel, sertifikat eller den signerte teksten. Feil gir bare en kort forklaring.
"""
import base64
import binascii
import json
import time
import uuid
from functools import lru_cache

MYNDIGHET = "https://login.microsoftonline.com"
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
LEVETID_SEK = 5 * 60          # Microsoft godtar inntil 10 minutter; kort er tryggest


class Sertifikatfeil(ValueError):
    """Sertifikatet kan ikke leses (feil format, feil passord eller mangler privat nøkkel). Teksten har aldri innholdet."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@lru_cache(maxsize=4)
def _les(sertifikat: str, passord: str):
    """(privat nøkkel, SHA-1-tommelavtrykk, SHA-256-tommelavtrykk) - avtrykkene base64url-kodet, slik Microsoft vil ha dem."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization import pkcs12
        from cryptography.x509 import load_pem_x509_certificate
    except ImportError:
        raise Sertifikatfeil("Programpakken «cryptography» mangler (står i requirements.txt).") from None
    tekst = (sertifikat or "").strip()
    if not tekst:
        raise Sertifikatfeil("Sertifikatet er tomt.")
    pw = passord.encode() if passord else None
    try:
        if "-----BEGIN" in tekst:
            data = tekst.encode()
            nokkel = serialization.load_pem_private_key(data, password=pw)
            sert = load_pem_x509_certificate(data)
        else:
            nokkel, sert, _ = pkcs12.load_key_and_certificates(base64.b64decode(tekst, validate=False), pw)
    except (ValueError, TypeError, binascii.Error):
        raise Sertifikatfeil("Sertifikatet kunne ikke leses: feil format eller feil passord.") from None
    if nokkel is None or sert is None:
        raise Sertifikatfeil("Sertifikatet mangler den private nøkkelen eller selve sertifikatet.")
    if not isinstance(nokkel, rsa.RSAPrivateKey):
        raise Sertifikatfeil("Sertifikatet må ha en RSA-nøkkel (standard i Key Vault).")
    return nokkel, _b64url(sert.fingerprint(hashes.SHA1())), _b64url(sert.fingerprint(hashes.SHA256()))  # noqa: S303


def kontroller(sertifikat: str, passord: str = "") -> None:
    """Sertifikatfeil hvis sertifikatet ikke kan brukes. For oppstartskontroll og m365_sjekk."""
    _les(sertifikat, passord or "")


def client_assertion(tenant_id: str, client_id: str, sertifikat: str, passord: str = "", *, naa: float | None = None) -> str:
    """Signert JWT (RS256) som beviser at forespørselen kommer fra appen. Gyldig i fem minutter, og bare én gang (jti)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    nokkel, sha1, sha256 = _les(sertifikat, passord or "")
    naa = int(time.time() if naa is None else naa)
    hode = {"alg": "RS256", "typ": "JWT", "x5t": sha1, "x5t#S256": sha256}
    krav = {"aud": f"{MYNDIGHET}/{tenant_id}/oauth2/v2.0/token", "iss": client_id, "sub": client_id,
            "jti": str(uuid.uuid4()), "iat": naa, "nbf": naa, "exp": naa + LEVETID_SEK}
    usignert = f"{_b64url(json.dumps(hode, separators=(',', ':')).encode())}." \
               f"{_b64url(json.dumps(krav, separators=(',', ':')).encode())}"
    signatur = nokkel.sign(usignert.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{usignert}.{_b64url(signatur)}"


def legitimasjon(tenant_id: str, client_id: str, *, sertifikat: str = "", passord: str = "", hemmelighet: str = "") -> dict:
    """Feltene som beviser appens identitet i et kall til token-endepunktet. Sertifikat brukes når det er satt (anbefalt),
    ellers client secret som før."""
    if sertifikat:
        return {"client_assertion_type": ASSERTION_TYPE,
                "client_assertion": client_assertion(tenant_id, client_id, sertifikat, passord)}
    return {"client_secret": hemmelighet}
