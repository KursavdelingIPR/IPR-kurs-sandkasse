"""Innlogging hos Microsoft med sertifikat (kurs/integrasjoner/sertifikat.py)
(Serit 04.10.2026: sertifikat i stedet for client secret). Sertifikatet lages av testen selv
(selvsignert, oppdiktet). Ingen nettverkskall: token-endepunktet erstattes.
(06.10.2026: kursholder-lenken og SharePoint er tatt bort, og med dem testen av valgt SharePoint-bibliotek.)"""
import base64
import datetime
import json

import pytest

cryptography = pytest.importorskip("cryptography")
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402
from cryptography.hazmat.primitives.serialization import pkcs12  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

from kurs import config, m365_sjekk  # noqa: E402
from kurs.integrasjoner import m365, sertifikat  # noqa: E402
from kurs.web import entra, sikkerhet  # noqa: E402

TENANT, KLIENT = "11111111-2222-3333-4444-555555555555", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _b64url_dekod(tekst: str) -> bytes:
    return base64.urlsafe_b64decode(tekst + "=" * (-len(tekst) % 4))


@pytest.fixture(scope="module")
def sert():
    """(nøkkel, sertifikat, PEM-tekst, base64 PFX uten passord, base64 PFX med passordet «hemmelig-pw»)."""
    nokkel = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    navn = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ipr-kurssystem-test")])
    naa = datetime.datetime.now(datetime.timezone.utc)
    s = (x509.CertificateBuilder().subject_name(navn).issuer_name(navn).public_key(nokkel.public_key())
         .serial_number(x509.random_serial_number()).not_valid_before(naa - datetime.timedelta(days=1))
         .not_valid_after(naa + datetime.timedelta(days=30)).sign(nokkel, hashes.SHA256()))
    pem = (nokkel.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()) + s.public_bytes(serialization.Encoding.PEM)).decode()
    pfx = base64.b64encode(pkcs12.serialize_key_and_certificates(b"t", nokkel, s, None,
                                                                 serialization.NoEncryption())).decode()
    pfx_pw = base64.b64encode(pkcs12.serialize_key_and_certificates(
        b"t", nokkel, s, None, serialization.BestAvailableEncryption(b"hemmelig-pw"))).decode()
    return nokkel, s, pem, pfx, pfx_pw


@pytest.mark.parametrize("format_", ["pem", "pfx"])
def test_client_assertion_er_riktig_signert_og_kortlevd(sert, format_):
    nokkel, s, pem, pfx, _ = sert
    jwt = sertifikat.client_assertion(TENANT, KLIENT, pem if format_ == "pem" else pfx, naa=1_800_000_000)
    hode_b64, krav_b64, signatur_b64 = jwt.split(".")
    hode, krav = json.loads(_b64url_dekod(hode_b64)), json.loads(_b64url_dekod(krav_b64))
    assert hode["alg"] == "RS256" and hode["typ"] == "JWT"
    assert _b64url_dekod(hode["x5t"]) == s.fingerprint(hashes.SHA1())  # noqa: S303
    assert _b64url_dekod(hode["x5t#S256"]) == s.fingerprint(hashes.SHA256())
    assert krav["aud"] == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
    assert krav["iss"] == krav["sub"] == KLIENT
    assert krav["nbf"] == krav["iat"] == 1_800_000_000 and krav["exp"] - krav["iat"] == 300
    nokkel.public_key().verify(_b64url_dekod(signatur_b64), f"{hode_b64}.{krav_b64}".encode(),
                               padding.PKCS1v15(), hashes.SHA256())       # kaster hvis signaturen er feil


def test_hver_assertion_er_unik(sert):
    _, _, pem, _, _ = sert
    a, b = (json.loads(_b64url_dekod(sertifikat.client_assertion(TENANT, KLIENT, pem).split(".")[1]))["jti"]
            for _ in range(2))
    assert a != b


def test_pfx_med_passord(sert):
    _, _, _, _, pfx_pw = sert
    sertifikat.kontroller(pfx_pw, "hemmelig-pw")
    with pytest.raises(sertifikat.Sertifikatfeil) as e:
        sertifikat.kontroller(pfx_pw, "feil-passord")
    assert "hemmelig-pw" not in str(e.value) and "feil-passord" not in str(e.value)


@pytest.mark.parametrize("ugyldig", ["", "ikke et sertifikat", "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----"])
def test_ugyldig_sertifikat_gir_forklaring_uten_innhold(ugyldig):
    with pytest.raises(sertifikat.Sertifikatfeil) as e:
        sertifikat.kontroller(ugyldig)
    assert "AAAA" not in str(e.value)


def test_legitimasjon_velger_sertifikat_framfor_client_secret(sert):
    _, _, pem, _, _ = sert
    med = sertifikat.legitimasjon(TENANT, KLIENT, sertifikat=pem, hemmelighet="hemmelig")
    assert set(med) == {"client_assertion_type", "client_assertion"} and "hemmelig" not in json.dumps(med)
    assert sertifikat.legitimasjon(TENANT, KLIENT, hemmelighet="hemmelig") == {"client_secret": "hemmelig"}


class _Svar:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _token_med_roller(roller):
    del_ = base64.urlsafe_b64encode(json.dumps({"roles": roller}).encode()).rstrip(b"=").decode()
    return f"x.{del_}.y"


@pytest.fixture
def m365_oppsett(monkeypatch, sert):
    _, _, pem, _, _ = sert
    monkeypatch.setattr(config, "M365_TENANT_ID", TENANT)
    monkeypatch.setattr(config, "M365_CLIENT_ID", KLIENT)
    monkeypatch.setattr(config, "M365_CLIENT_SECRET", "")
    monkeypatch.setattr(config, "M365_SERTIFIKAT", pem)
    monkeypatch.setattr(config, "M365_SERTIFIKAT_PASSORD", "")
    monkeypatch.setattr(m365, "_cache", {})
    sendt = []

    def post(url, data=None, timeout=None):
        sendt.append((url, data))
        return _Svar({"access_token": _token_med_roller(["Mail.Send", "Sites.Selected"]), "expires_in": 3600})
    monkeypatch.setattr(m365.requests, "post", post)
    return sendt


def test_graph_token_hentes_med_sertifikat(m365_oppsett):
    m365.token()
    url, data = m365_oppsett[0]
    assert url.endswith(f"/{TENANT}/oauth2/v2.0/token")
    assert data["grant_type"] == "client_credentials" and data["client_id"] == KLIENT
    assert data["client_assertion_type"] == sertifikat.ASSERTION_TYPE and "client_secret" not in data


def test_graph_token_med_client_secret_som_foer(m365_oppsett, monkeypatch):
    monkeypatch.setattr(config, "M365_SERTIFIKAT", "")
    monkeypatch.setattr(config, "M365_CLIENT_SECRET", "hemmelig")
    m365.token()
    assert m365_oppsett[0][1]["client_secret"] == "hemmelig" and "client_assertion" not in m365_oppsett[0][1]


def test_entra_er_aktiv_med_bare_sertifikat(monkeypatch, sert):
    monkeypatch.setattr(config, "ENTRA_TENANT_ID", TENANT)
    monkeypatch.setattr(config, "ENTRA_CLIENT_ID", KLIENT)
    monkeypatch.setattr(config, "ENTRA_CLIENT_SECRET", "")
    monkeypatch.setattr(config, "ENTRA_SERTIFIKAT", "")
    assert not entra.aktiv()
    monkeypatch.setattr(config, "ENTRA_SERTIFIKAT", sert[2])
    assert entra.aktiv()


def test_ulesbart_sertifikat_stopper_oppstarten_i_drift(monkeypatch):
    monkeypatch.setattr(config, "DEMO", False)
    monkeypatch.setattr(config, "M365_SERTIFIKAT", "ødelagt")
    monkeypatch.setattr(config, "M365_SERTIFIKAT_PASSORD", "")
    monkeypatch.setattr(config, "ENTRA_SERTIFIKAT", "")
    feil = sikkerhet.produksjonsfeil()
    assert any(f.startswith("M365_SERTIFIKAT:") for f in feil) and not any("ødelagt" in f for f in feil)


def test_m365_sjekk_mangler_innstillinger(monkeypatch, capsys):
    monkeypatch.setattr(config, "M365_TENANT_ID", "")
    assert m365_sjekk.main([]) == 2
    assert "M365_TENANT_ID" in capsys.readouterr().out


def test_m365_sjekk_viser_tillatelser_uten_hemmeligheter(m365_oppsett, capsys, sert):
    assert m365_sjekk.main([]) == 0
    ut = capsys.readouterr().out
    assert "Innlogging hos Microsoft: OK" in ut and "Mail.Send, Sites.Selected" in ut
    assert sert[2][40:80] not in ut and "client_assertion" not in ut
