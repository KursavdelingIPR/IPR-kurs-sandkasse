"""Admin-innlogging med Microsoft Entra ID (kurs/web/entra.py). Token-endepunktet erstattes av en falsk requests.post;
id_token bygges av testen (ingen signatur - se modulens docstring for hvorfor det er riktig i denne flyten).
"""
import base64
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from kurs import config, db
from kurs.web import entra

TENANT, CLIENT = "11111111-2222-3333-4444-555555555555", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    monkeypatch.setattr(config, "ENTRA_TENANT_ID", TENANT)
    monkeypatch.setattr(config, "ENTRA_CLIENT_ID", CLIENT)
    monkeypatch.setattr(config, "ENTRA_CLIENT_SECRET", "hemmelig")
    monkeypatch.setattr(config, "ENTRA_GRUPPER", "g-system=system,g-lese=lese")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _klient():
    from kurs.web import app as webapp
    return webapp.app.test_client()


def _jwt(claims: dict) -> str:
    b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{b64({'alg': 'RS256'})}.{b64(claims)}.signatur"


def _claims(nonce, **ekstra):
    return {"iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "tid": TENANT, "aud": CLIENT,
            "exp": int(time.time()) + 600, "nbf": int(time.time()) - 60, "nonce": nonce,
            "oid": "oid-kari", "name": "Kari Ansatt", "preferred_username": "Kari.Ansatt@ipr.no",
            "roles": ["ipr.kursadmin"], **ekstra}


class _Svar:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(response=self)

    def json(self):
        return self._data


def _logg_inn_via_entra(monkeypatch, klient, lag_claims, token_svar=None):
    """Kjoerer hele flyten: start -> (Microsoft) -> svar. lag_claims(nonce) gir claims i id_token."""
    r = klient.get("/admin/logg-inn/entra")
    assert r.status_code == 302
    url = urlsplit(r.headers["Location"])
    q = parse_qs(url.query)
    assert url.netloc == "login.microsoftonline.com" and q["response_type"] == ["code"]
    assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"] == [f"{config.BASE_URL}/admin/logg-inn/entra/svar"]
    with klient.session_transaction() as s:
        nonce, state = s["entra"]["nonce"], s["entra"]["state"]
    kall = []

    def falsk_post(url, data=None, timeout=None):
        kall.append((url, data))
        if token_svar is not None:
            return token_svar
        return _Svar({"id_token": _jwt(lag_claims(nonce)), "access_token": "x"})
    monkeypatch.setattr(entra.requests, "post", falsk_post)
    r = klient.get(f"/admin/logg-inn/entra/svar?code=kode123&state={state}")
    return r, kall


def test_full_innlogging_oppretter_bruker_med_rolle_fra_entra(con, monkeypatch):
    k = _klient()
    r, kall = _logg_inn_via_entra(monkeypatch, k, _claims)
    assert r.status_code == 302 and r.headers["Location"] == "/admin"
    assert kall[0][0] == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
    assert kall[0][1]["grant_type"] == "authorization_code" and kall[0][1]["code"] == "kode123"
    assert kall[0][1]["client_secret"] == "hemmelig" and "code_verifier" in kall[0][1]
    rad = con.execute("SELECT * FROM admin_bruker WHERE entra_oid='oid-kari'").fetchone()
    assert (rad["brukernavn"], rad["navn"], rad["rolle"], rad["epost"]) == ("kari.ansatt@ipr.no", "Kari Ansatt", "kursadmin",
                                                                            "kari.ansatt@ipr.no")
    assert k.get("/admin").status_code == 200
    assert k.get("/admin/brukere").status_code == 403                         # kursadmin, ikke system
    with k.session_transaction() as s:
        assert "entra" not in s                                                # engangs-state er brukt opp
    hendelse = con.execute("SELECT detaljer FROM hendelse WHERE handling='admin_innlogget'").fetchone()[0]
    assert '"entra": true' in hendelse


def test_rolle_oppdateres_fra_entra_ved_neste_innlogging(con, monkeypatch):
    _logg_inn_via_entra(monkeypatch, _klient(), _claims)
    r, _ = _logg_inn_via_entra(monkeypatch, _klient(), lambda n: _claims(n, roles=["ipr.system"]))
    assert r.status_code == 302
    assert con.execute("SELECT rolle FROM admin_bruker WHERE entra_oid='oid-kari'").fetchone()[0] == "system"
    assert con.execute("SELECT COUNT(*) FROM admin_bruker WHERE entra_oid='oid-kari'").fetchone()[0] == 1


def test_gruppe_gir_rolle_og_hoeyeste_vinner(con, monkeypatch):
    r, _ = _logg_inn_via_entra(monkeypatch, _klient(), lambda n: _claims(n, roles=["ipr.lese"], groups=["g-system", "annen"]))
    assert r.status_code == 302
    assert con.execute("SELECT rolle FROM admin_bruker WHERE entra_oid='oid-kari'").fetchone()[0] == "system"


def test_uten_rolle_gir_ingen_tilgang_og_ingen_bruker(con, monkeypatch):
    r, _ = _logg_inn_via_entra(monkeypatch, _klient(), lambda n: _claims(n, roles=[], groups=["ukjent"]))
    assert r.status_code == 403 and "ikke gitt tilgang" in r.get_data(as_text=True)
    assert con.execute("SELECT COUNT(*) FROM admin_bruker WHERE entra_oid='oid-kari'").fetchone()[0] == 0


@pytest.mark.parametrize("endring", [{"aud": "annen-app"}, {"tid": "annen-tenant"}, {"iss": "https://ond.example/v2.0"},
                                     {"exp": int(time.time()) - 3600}, {"nonce": "feil"}, {"oid": ""}])
def test_ugyldige_claims_avvises(con, monkeypatch, endring):
    r, _ = _logg_inn_via_entra(monkeypatch, _klient(), lambda n: _claims(n) | endring)
    assert r.status_code == 403
    assert con.execute("SELECT COUNT(*) FROM admin_bruker WHERE entra_oid='oid-kari'").fetchone()[0] == 0


def test_feil_state_avvises(con, monkeypatch):
    k = _klient()
    k.get("/admin/logg-inn/entra")
    monkeypatch.setattr(entra.requests, "post", lambda *a, **kw: pytest.fail("token-endepunktet skal ikke kalles"))
    assert k.get("/admin/logg-inn/entra/svar?code=x&state=feil").status_code == 400
    assert _klient().get("/admin/logg-inn/entra/svar?code=x&state=y").status_code == 400   # ingen paabegynt innlogging


def test_feil_fra_microsoft_gir_rolig_melding(con, monkeypatch):
    k = _klient()
    k.get("/admin/logg-inn/entra")
    r = k.get("/admin/logg-inn/entra/svar?error=access_denied&error_description=hemmelig+detalj", follow_redirects=True)
    assert r.status_code == 200 and "avbrutt" in r.get_data(as_text=True) and "hemmelig" not in r.get_data(as_text=True)
    r, _ = _logg_inn_via_entra(monkeypatch, _klient(), _claims, token_svar=_Svar({"error": "x"}, 400))
    assert r.status_code == 403


def test_deaktivert_entra_bruker_slipper_ikke_inn(con, monkeypatch):
    _logg_inn_via_entra(monkeypatch, _klient(), _claims)
    con.execute("UPDATE admin_bruker SET aktiv=0 WHERE entra_oid='oid-kari'")
    con.commit()
    r, _ = _logg_inn_via_entra(monkeypatch, _klient(), _claims)
    assert r.status_code == 403 and "deaktivert" in r.get_data(as_text=True)


def test_lokal_bruker_med_samme_epost_knyttes_til_entra(con, monkeypatch):
    bid = db.opprett_admin_bruker(con, "kari.ansatt@ipr.no", "Kari", "passord-som-holder", rolle="lese")
    con.commit()
    r, _ = _logg_inn_via_entra(monkeypatch, _klient(), _claims)
    assert r.status_code == 302
    rad = con.execute("SELECT * FROM admin_bruker WHERE id=?", (bid,)).fetchone()
    assert rad["entra_oid"] == "oid-kari" and rad["rolle"] == "kursadmin"          # rollen kommer naa fra Entra
    assert con.execute("SELECT COUNT(*) FROM admin_bruker").fetchone()[0] == 2
    # lokalt passord virker ikke lenger for en Entra-bruker
    r = _klient().post("/admin/logg-inn", data={"brukernavn": "kari.ansatt@ipr.no", "passord": "passord-som-holder"})
    assert r.status_code == 200 and "Feil brukernavn" in r.get_data(as_text=True)


def test_innloggingssiden_viser_microsoft_knapp_kun_naar_entra_er_satt_opp(con, monkeypatch):
    assert "Logg inn med Microsoft" in _klient().get("/admin/logg-inn").get_data(as_text=True)
    monkeypatch.setattr(config, "ENTRA_CLIENT_SECRET", "")
    html = _klient().get("/admin/logg-inn").get_data(as_text=True)
    assert "Logg inn med Microsoft" not in html
    assert _klient().get("/admin/logg-inn/entra").status_code == 404


def test_neste_foelger_med_gjennom_entra_men_kun_relative(con, monkeypatch):
    k = _klient()
    k.get("/admin/logg-inn/entra?neste=https://ond.example")
    with k.session_transaction() as s:
        s["entra"]["neste"] = "https://ond.example"
        nonce, state = s["entra"]["nonce"], s["entra"]["state"]
    monkeypatch.setattr(entra.requests, "post", lambda *a, **kw: _Svar({"id_token": _jwt(_claims(nonce))}))
    r = k.get(f"/admin/logg-inn/entra/svar?code=k&state={state}")
    assert r.headers["Location"] == "/admin"


def test_rolle_fra_claims_og_kart():
    assert entra._kart("a=system, b=lese,c=tull,=x") == {"a": "system", "b": "lese"}
    assert entra.rolle_fra_claims({"roles": ["ipr.lese", "ipr.kursadmin"]}) == "kursadmin"
    assert entra.rolle_fra_claims({}) is None
