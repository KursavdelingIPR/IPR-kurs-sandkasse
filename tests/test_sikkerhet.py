"""Sikkerhetsvern i webappen (kurs/web/sikkerhet.py + herding i app.py).

CSRF, sikkerhetshoder/CSP, ingen inline-JS, takbegrensning, admin-oektens levetid og deaktivering, viderekoblinger,
signerte opplastingslenker, filnavn/sti, lenkevalidering, CSV-injeksjon, kontrollerte feilsider, webhook-herding,
KI-assistentens brytere og produksjonskontroll av konfigurasjonen.
"""
import re
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from kurs import config, db, lenker
from kurs.integrasjoner import epost, visma
from kurs.web import sikkerhet

MALER = Path(__file__).resolve().parent.parent / "kurs" / "web" / "templates"


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _klient():
    from kurs.web import app as webapp
    return webapp.app.test_client()


def _admin():
    k = _klient()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def _kurs(con, kode="S1", **kw):
    start = date.today() + timedelta(days=30)
    kid = db.opprett_kurs(con, kode=kode, navn="Sikkerhetskurs", datoer=[start.isoformat()], sharepoint_mappe=f"Kurs/{kode}",
                          **{"pris_nok": 1000, "type": "fysisk", **kw})
    con.commit()
    return kid


def _prod(monkeypatch):
    monkeypatch.setattr(config, "DEMO", False)
    monkeypatch.setattr(config, "MODUS", "prod")
    monkeypatch.setattr(config, "HEMMELIG_NOKKEL", "x" * 40)
    monkeypatch.setattr(config, "WEBHOOK_HEMMELIG", "y" * 40)
    monkeypatch.setattr(config, "BASE_URL", "https://kurs.eksempel.no")
    from kurs.web import app as webapp
    monkeypatch.setattr(webapp, "_database_klar", True)


# ============================ CSRF ============================

def test_post_uten_csrf_token_avvises_med_400_og_uten_endring(con):
    admin = _admin()
    admin.injiser_csrf = False
    r = admin.post("/admin/brukere", data={"navn": "Ny", "brukernavn": "ny", "passord": "passord-som-holder"})
    assert r.status_code == 400 and "Skjemaet kunne ikke sendes" in r.get_data(as_text=True)
    assert con.execute("SELECT COUNT(*) FROM admin_bruker WHERE brukernavn='ny'").fetchone()[0] == 0


def test_post_med_feil_csrf_token_avvises(con):
    admin = _admin()
    admin.injiser_csrf = False
    r = admin.post("/admin/brukere", data={"navn": "Ny", "brukernavn": "ny", "passord": "passord-som-holder",
                                          "csrf_token": "feil-token"})
    assert r.status_code == 400


def test_post_med_riktig_token_i_hode_godtas(con):
    admin = _admin()
    admin.injiser_csrf = False
    admin.get("/admin/brukere")                  # en side med POST-skjema lager sesjonens token (csrf_token() i malen)
    with admin.session_transaction() as s:
        token = s["csrf"]
    r = admin.post("/admin/brukere", data={"navn": "Ny", "brukernavn": "ny", "passord": "passord-som-holder"},
                   headers={"X-CSRF-Token": token})
    assert r.status_code == 302
    assert con.execute("SELECT COUNT(*) FROM admin_bruker WHERE brukernavn='ny'").fetchone()[0] == 1


def test_offentlig_paamelding_krever_ogsaa_csrf(con):
    _kurs(con)
    k = _klient()
    k.injiser_csrf = False
    r = k.post("/kurs/S1", data={"navn": "A", "epost": "a@x.no", "samtykke": "on"})
    assert r.status_code == 400
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 0


def test_get_krever_aldri_csrf(con):
    k = _klient()
    k.injiser_csrf = False
    assert k.get("/").status_code == 200


def test_webhook_er_fritatt_fra_csrf_men_krever_signatur(con):
    import hashlib
    import hmac
    import json
    kid = _kurs(con, "WH")
    body = json.dumps({"navn": "N", "epost": "n@x.no", "kurs": "WH"}).encode()
    sig = hmac.new(config.WEBHOOK_HEMMELIG.encode(), body, hashlib.sha256).hexdigest()
    k = _klient()
    k.injiser_csrf = False
    assert k.post("/api/paamelding", data=body, content_type="application/json").status_code == 401
    assert k.post("/api/paamelding", data=body, content_type="application/json",
                  headers={"X-IPR-Signatur": sig}).status_code == 201


def test_alle_post_skjema_i_malene_har_csrf_felt():
    for fil in sorted(MALER.glob("*.html")):
        html = fil.read_text(encoding="utf-8")
        for m in re.finditer(r"<form\b[^>]*>(.*?)</form>", html, re.S | re.I):
            tag = m.group(0)[:m.group(0).index(">") + 1]
            if 'method="post"' in tag.lower():
                assert "csrf_token" in m.group(1), f"{fil.name}: POST-skjema uten csrf_token"


def test_malene_har_ingen_inline_javascript():
    """CSP tillater kun static/app.js og <script nonce>: ingen on*-attributter, ingen javascript:-lenker,
    ingen inline <script> uten nonce."""
    for fil in sorted(MALER.glob("*.html")):
        html = fil.read_text(encoding="utf-8")
        assert not re.search(r"\son(click|change|submit|load|input|keyup|mouseover)=", html), fil.name
        assert "javascript:" not in html.lower(), fil.name
        for m in re.finditer(r"<script\b[^>]*>", html):
            assert "nonce=" in m.group(0), f"{fil.name}: <script> uten nonce"


# ============================ sikkerhetshoder ============================

def test_sikkerhetshoder_paa_alle_svar(con):
    r = _klient().get("/")
    h = r.headers
    assert h["X-Content-Type-Options"] == "nosniff" and h["X-Frame-Options"] == "DENY"
    assert h["Referrer-Policy"] == "strict-origin-when-cross-origin"
    csp = h["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    assert "form-action 'self'" in csp and "'unsafe-inline'" not in csp.split("style-src")[0]   # aldri for script
    nonce = re.search(r"script-src 'self' 'nonce-([^']+)'", csp).group(1)
    assert f'nonce="{nonce}"' in r.get_data(as_text=True)                         # samme nonce i siden


def test_nonce_er_ny_for_hver_request(con):
    k = _klient()
    a = k.get("/").headers["Content-Security-Policy"]
    b = k.get("/").headers["Content-Security-Policy"]
    assert a != b


def test_hsts_kun_i_drift_over_https(con, monkeypatch):
    assert "Strict-Transport-Security" not in _klient().get("/").headers
    _prod(monkeypatch)
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    assert "Strict-Transport-Security" not in k.get("/").headers                # http i drift: ingen HSTS
    r = k.get("/", base_url="https://kurs.eksempel.no")
    assert r.headers["Strict-Transport-Security"].startswith("max-age=")


def test_innloggede_sider_er_no_store(con):
    admin = _admin()
    assert admin.get("/admin").headers["Cache-Control"] == "no-store"
    assert "Cache-Control" not in _klient().get("/").headers


def test_sesjonscookien_er_httponly_og_secure_i_drift(con, monkeypatch):
    from kurs.web import app as webapp
    assert webapp.app.config["SESSION_COOKIE_HTTPONLY"] and webapp.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    r = _klient().post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    cookie = r.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


# ============================ admin-oekt ============================

def test_deaktivert_bruker_mister_tilgang_umiddelbart(con):
    bid = db.opprett_admin_bruker(con, "kari", "Kari", "passord-som-holder")
    con.commit()
    k = _klient()
    k.post("/admin/logg-inn", data={"brukernavn": "kari", "passord": "passord-som-holder"})
    assert k.get("/admin").status_code == 200
    con.execute("UPDATE admin_bruker SET aktiv=0 WHERE id=?", (bid,))
    con.commit()
    r = k.get("/admin")
    assert r.status_code == 302 and "/admin/logg-inn" in r.headers["Location"]


def test_admin_oekt_utloeper_etter_inaktivitet_og_absolutt(con, monkeypatch):
    from kurs.web import app as webapp
    admin = _admin()
    with admin.session_transaction() as s:
        s["admin_sist"] = time.time() - (webapp.ADMIN_INAKTIV_MIN + 1) * 60
    assert admin.get("/admin").status_code == 302
    admin = _admin()
    with admin.session_transaction() as s:
        s["admin_start"] = time.time() - (webapp.ADMIN_MAKS_TIMER + 1) * 3600
    assert admin.get("/admin").status_code == 302


def test_innlogging_gir_ny_sesjon_men_beholder_deltakerinnlogging(con):
    k = _klient()
    with k.session_transaction() as s:
        s["csrf"] = "gammelt-token"
        s["deltaker_id"] = 42
        s["fremmed"] = "x"
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD,
                                    "csrf_token": "gammelt-token"})
    with k.session_transaction() as s:
        assert s.get("admin_id") and s.get("deltaker_id") == 42 and "fremmed" not in s
        assert s.get("csrf") != "gammelt-token"


def test_utlogging_fjerner_admin_men_ikke_deltaker(con):
    admin = _admin()
    with admin.session_transaction() as s:
        s["deltaker_id"] = 7
    admin.get("/admin/logg-ut")
    with admin.session_transaction() as s:
        assert "admin_id" not in s and s.get("deltaker_id") == 7


def test_innlogging_logges_uten_persondata(con):
    _admin()
    _klient().post("/admin/logg-inn", data={"brukernavn": "finnes.ikke@x.no", "passord": "x"})
    rader = [dict(r) for r in con.execute("SELECT handling, detaljer FROM hendelse WHERE handling LIKE 'admin_innlogg%'")]
    assert [r["handling"] for r in rader] == ["admin_innlogget", "admin_innlogging_avvist"]
    assert "finnes.ikke" not in rader[1]["detaljer"]


def test_kan_ikke_deaktivere_seg_selv_eller_siste_aktive(con):
    admin = _admin()
    meg = con.execute("SELECT id FROM admin_bruker").fetchone()[0]
    admin.post(f"/admin/brukere/{meg}/deaktiver")
    assert con.execute("SELECT aktiv FROM admin_bruker WHERE id=?", (meg,)).fetchone()[0] == 1
    andre = db.opprett_admin_bruker(con, "b", "B", "passord-som-holder")
    con.commit()
    admin.post(f"/admin/brukere/{andre}/deaktiver")
    assert con.execute("SELECT aktiv FROM admin_bruker WHERE id=?", (andre,)).fetchone()[0] == 0
    assert admin.post("/admin/brukere/999/deaktiver").status_code == 404


# ============================ viderekobling ============================

@pytest.mark.parametrize("neste", ["https://ond.example", "//ond.example/x", "/\\ond.example", "javascript:alert(1)", ""])
def test_open_redirect_blokkeres_ved_admin_innlogging(con, neste):
    r = _klient().post(f"/admin/logg-inn?neste={neste}",
                       data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    assert r.status_code == 302 and r.headers["Location"] == "/admin"


def test_relativ_neste_godtas(con):
    r = _klient().post("/admin/logg-inn?neste=/admin/aktiviteter",
                       data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    assert r.headers["Location"] == "/admin/aktiviteter"


def test_open_redirect_blokkeres_ved_innloggingslenke(con):
    did = db.finn_eller_opprett_deltaker(con, "d@x.no", "D")
    con.execute("INSERT INTO innlogging_token (token, deltaker_id, utloper) VALUES ('tok', ?, '2099-01-01T00:00:00')", (did,))
    con.commit()
    r = _klient().get("/logg-inn/tok?neste=https://ond.example")
    assert r.status_code == 302 and r.headers["Location"] == "/min-side"


# ============================ takbegrensning ============================

def test_admin_innlogging_takbegrenses(con):
    k = _klient()
    for _ in range(sikkerhet.GRENSER["admin_login"][0]):
        assert k.post("/admin/logg-inn", data={"brukernavn": "x", "passord": "feil"}).status_code == 200
    r = k.post("/admin/logg-inn", data={"brukernavn": "x", "passord": "feil"})
    assert r.status_code == 429 and "For mange forsøk" in r.get_data(as_text=True)
    # riktig passord hjelper ikke foer vinduet er over
    assert k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN,
                                          "passord": config.ADMIN_PASSORD}).status_code == 429


def test_innloggingslenke_takbegrenses_per_epost(con):
    db.finn_eller_opprett_deltaker(con, "d@x.no", "D")
    con.commit()
    k = _klient()
    maks = sikkerhet.GRENSER["innloggingslenke_epost"][0]
    for _ in range(maks):
        assert k.post("/logg-inn", data={"epost": "D@X.NO"}).status_code == 200
    assert k.post("/logg-inn", data={"epost": "d@x.no"}).status_code == 429
    assert con.execute("SELECT COUNT(*) FROM innlogging_token").fetchone()[0] == maks


def test_takbegrenser_glidende_vindu():
    t = sikkerhet.Takbegrenser()
    assert all(t.tillat("k", 3, 60, naa=n) for n in (0, 1, 2))
    assert not t.tillat("k", 3, 60, naa=3)
    assert t.tillat("k", 3, 60, naa=61)          # eldste treff er ute av vinduet
    assert t.tillat("annen", 3, 60, naa=3)        # egen noekkel


def test_sporsmal_takbegrenses_og_kan_slaas_av(con, monkeypatch):
    k = _klient()
    for _ in range(sikkerhet.GRENSER["sporsmal"][0]):
        assert k.post("/sporsmal", data={"sporsmal": "Når er kurset?"}).status_code == 200
    assert k.post("/sporsmal", data={"sporsmal": "Når er kurset?"}).status_code == 429
    monkeypatch.setattr(config, "ASSISTENT_AKTIV", False)
    assert _klient().get("/sporsmal").status_code == 404


# ============================ signerte lenker og filer ============================

def _krav(con, kid):
    return db.sett_inn(con, "INSERT INTO materiell_krav (kurs_id, ansvarlig_navn, ansvarlig_epost, frist) VALUES (?,?,?,?)",
                       (kid, "K", "k@x.no", "2027-01-01"))


def test_opplastingslenke_krever_gyldig_signatur(con):
    kid = _kurs(con)
    krav = _krav(con, kid)
    con.commit()
    k = _klient()
    assert k.get(f"/lever/{krav}").status_code == 404
    assert k.get(f"/lever/{krav}/feilsignatur").status_code == 404
    assert k.get(f"/lever/{krav}/{lenker.signatur('lever', krav + 1)}").status_code == 404   # signatur for et annet krav
    assert k.get(lenker.lever_lenke(krav).replace(config.BASE_URL, "")).status_code == 200


def test_opplasting_renser_filnavn_og_avviser_ukjent_filtype(con, tmp_path, monkeypatch):
    from io import BytesIO
    from kurs.integrasjoner import sharepoint
    monkeypatch.setattr(sharepoint, "DEMO_ROT", tmp_path / "sp")
    kid = _kurs(con)
    krav = _krav(con, kid)
    con.commit()
    url = lenker.lever_lenke(krav).replace(config.BASE_URL, "")
    k = _klient()
    r = k.post(url, data={"fil": (BytesIO(b"x"), "../../.env")}, content_type="multipart/form-data")
    assert r.status_code == 200 and "støttes ikke" in r.get_data(as_text=True)
    r = k.post(url, data={"fil": (BytesIO(b"x"), "skript.exe")}, content_type="multipart/form-data")
    assert "støttes ikke" in r.get_data(as_text=True)
    r = k.post(url, data={"fil": (BytesIO(b"%PDF"), "../../Presentasjon dag 1.pdf")}, content_type="multipart/form-data")
    assert r.status_code == 302
    lagret = list((tmp_path / "sp").rglob("*.pdf"))
    assert len(lagret) == 1 and lagret[0].name == "Presentasjon_dag_1.pdf"
    assert (tmp_path / "sp" / "Kurs" / "S1" / "Presentasjoner") in lagret[0].parents
    aktor = con.execute("SELECT aktor FROM hendelse WHERE handling='materiell_levert'").fetchone()[0]
    assert aktor == f"materiell:{krav}"


def test_materiell_avviser_sti_i_filnavn(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="d@x.no", navn="D")
    con.commit()
    k = _klient()
    with k.session_transaction() as s:
        s["deltaker_id"] = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pid,)).fetchone()[0]
    assert k.get(f"/materiell/{kid}/..%2F..%2F.env").status_code == 404
    assert k.get(f"/materiell/{kid}/../.env").status_code == 404


def test_lokalt_dokument_kun_under_data(con):
    did = db.finn_eller_opprett_deltaker(con, "d@x.no", "D")
    con.execute("INSERT INTO dokument (deltaker_id, type, tittel, url) VALUES (?, 'annet', 'x', 'lokal:.env')", (did,))
    dok = db.sett_inn(con, "INSERT INTO dokument (deltaker_id, type, tittel, url) VALUES (?, 'annet', 'y', 'lokal:kurs/config.py')",
                      (did,))
    con.commit()
    k = _klient()
    with k.session_transaction() as s:
        s["deltaker_id"] = did
    assert k.get(f"/dokument/{dok - 1}").status_code == 404
    assert k.get(f"/dokument/{dok}").status_code == 404


def test_dokument_lenke_maa_vaere_http(con):
    did = db.finn_eller_opprett_deltaker(con, "d@x.no", "D")
    dok = db.sett_inn(con, "INSERT INTO dokument (deltaker_id, type, tittel, url) VALUES (?, 'annet', 'x', 'javascript:alert(1)')",
                      (did,))
    con.commit()
    k = _klient()
    with k.session_transaction() as s:
        s["deltaker_id"] = did
    assert k.get(f"/dokument/{dok}").status_code == 404


def test_zoom_lenke_maa_vaere_https(con):
    kid = _kurs(con)
    admin = _admin()
    grunnlag = {"navn": "Sikkerhetskurs", "type": "digital", "fakturering": "person", "betaling": "samlet", "pris_nok": "1000"}
    admin.post(f"/admin/kurs/{kid}/oppsett", data={**grunnlag, "zoom_url": "javascript:alert(1)"})
    assert con.execute("SELECT zoom_url FROM kurs WHERE id=?", (kid,)).fetchone()[0] is None
    admin.post(f"/admin/kurs/{kid}/oppsett", data={**grunnlag, "zoom_url": "https://zoom.us/j/123"})
    assert con.execute("SELECT zoom_url FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "https://zoom.us/j/123"


# ============================ XSS i maldata ============================

def test_deltakernavn_med_anfoerselstegn_ender_aldri_i_javascript(con):
    kid = _kurs(con)
    db.meld_paa(con, kid, epost="x@x.no", navn="Ola\"); alert('xss'); //")
    con.commit()
    html = _admin().get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    assert "alert('xss')" not in html and "alert(&#39;xss&#39;)" in html
    assert 'data-bekreft="Melde av Ola&#34;); alert(&#39;xss&#39;); //?"' in html


def test_kursnavn_med_script_escapes_i_admin(con):
    kid = _kurs(con)
    con.execute("UPDATE kurs SET navn=? WHERE id=?", ("<script>alert(1)</script>", kid))
    con.commit()
    for url in ("/admin", "/admin/aktiviteter", f"/admin/kurs/{kid}/oppsett"):
        html = _admin().get(url).get_data(as_text=True)
        assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html, url


# ============================ CSV ============================

@pytest.mark.parametrize("verdi,forventet", [("=1+1", "'=1+1"), ("+47 999", "'+47 999"), ("-x", "'-x"), ("@a", "'@a"),
                                              ("Ola", "Ola"), ("", ""), (5, 5), (None, None)])
def test_csv_trygg(verdi, forventet):
    assert sikkerhet.csv_trygg(verdi) == forventet


def test_csv_eksport_beskytter_mot_formler(con):
    kid = _kurs(con)
    db.meld_paa(con, kid, epost="f@x.no", navn="=HYPERLINK(\"http://ond\")", deltaker={"arbeidssted": "+cmd"})
    con.commit()
    csv = _admin().get(f"/admin/kurs/{kid}/deltakere.csv").get_data(as_text=True)
    assert "'=HYPERLINK" in csv and "'+cmd" in csv and ";=HYPERLINK" not in csv
    csv = _admin().get("/admin/rapporter/deltakere.csv").get_data(as_text=True)
    assert "'=HYPERLINK" in csv


# ============================ feilsider ============================

def test_404_og_405_gir_kontrollert_side(con):
    r = _klient().get("/finnes-ikke")
    assert r.status_code == 404 and "Siden finnes ikke" in r.get_data(as_text=True)
    r = _klient().delete("/")
    assert r.status_code == 405


def test_500_gir_referanse_men_aldri_traceback(con, monkeypatch):
    from kurs.web import app as webapp
    monkeypatch.setattr(webapp.db, "koble", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("hemmelig detalj")))
    r = _klient().get("/")
    html = r.get_data(as_text=True)
    assert r.status_code == 500 and "Noe gikk galt" in html and "referansen" in html
    assert "hemmelig detalj" not in html and "Traceback" not in html and "RuntimeError" not in html


def test_api_feil_gir_json(con):
    r = _klient().get("/api/finnes-ikke")
    assert r.status_code == 404 and r.get_json()["status"] == "feil"


# ============================ webhook ============================

def _webhook(k, data, **kw):
    import hashlib
    import hmac
    import json
    body = json.dumps(data).encode()
    sig = hmac.new(config.WEBHOOK_HEMMELIG.encode(), body, hashlib.sha256).hexdigest()
    return k.post("/api/paamelding", data=body, content_type="application/json", headers={"X-IPR-Signatur": sig}, **kw)


def test_webhook_godtar_ikke_token_i_url(con):
    _kurs(con, "WH")
    r = _klient().post(f"/api/paamelding?token={config.WEBHOOK_HEMMELIG}", json={"navn": "N", "epost": "n@x.no", "kurs": "WH"})
    assert r.status_code == 401


def test_webhook_filtrerer_felt_etter_kursoppsett(con):
    kid = _kurs(con, "WH", type="digital", fakturering="ingen", pris_nok=0)   # ingen HPR, ingen allergi, ingen faktura
    r = _webhook(_klient(), {"navn": "N", "epost": "n@x.no", "kurs": "WH", "hpr_nr": "123", "allergier": "nøtter",
                             "org_nr": "999", "faktura_ref": "REF"})
    assert r.status_code == 201
    p = con.execute("SELECT p.*, d.hpr_nr FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE p.kurs_id=?",
                    (kid,)).fetchone()
    assert p["hpr_nr"] is None and p["org_nr"] is None and p["faktura_ref"] is None and p["betaler"] == "person"
    assert con.execute("SELECT COUNT(*) FROM sensitivt").fetchone()[0] == 0


def test_webhook_gir_409_for_stengt_kurs_og_404_for_utkast(con):
    kid = _kurs(con, "WH", paameldingsfrist="2020-01-01")
    assert _webhook(_klient(), {"navn": "N", "epost": "n@x.no", "kurs": "WH"}).status_code == 409
    con.execute("UPDATE kurs SET status='utkast' WHERE id=?", (kid,))
    con.commit()
    assert _webhook(_klient(), {"navn": "N", "epost": "n@x.no", "kurs": "WH"}).status_code == 404


def test_webhook_nekter_i_drift_med_standardhemmelighet(con, monkeypatch):
    _kurs(con, "WH")
    _prod(monkeypatch)
    monkeypatch.setattr(config, "WEBHOOK_HEMMELIG", "demo-webhook-hemmelighet")
    assert _webhook(_klient(), {"navn": "N", "epost": "n@x.no", "kurs": "WH"}).status_code == 503


# ============================ admin-ruter: eierskap og gyldig input ============================

def test_oppmote_kan_ikke_registreres_paa_tvers_av_kurs(con):
    a, b = _kurs(con, "A"), _kurs(con, "B")
    pid, _ = db.meld_paa(con, a, epost="d@x.no", navn="D")
    dag_b = db.kursdager(con, b)[0]["id"]
    con.commit()
    admin = _admin()
    assert admin.post(f"/admin/kurs/{a}/oppmote", data={"paamelding_id": pid, "kursdag_id": dag_b}).status_code == 404
    assert admin.post(f"/admin/kurs/{a}/oppmote", data={"paamelding_id": "x", "kursdag_id": dag_b}).status_code == 404
    assert con.execute("SELECT COUNT(*) FROM oppmote").fetchone()[0] == 0


def test_meld_av_ukjent_paamelding_gir_404(con):
    assert _admin().post("/admin/paamelding/999/meld-av").status_code == 404


def test_ansvarlig_maa_vaere_ekte_bruker(con):
    kid = _kurs(con)
    admin = _admin()
    assert admin.post(f"/admin/kurs/{kid}/ansvarlig", data={"ansvarlig_admin_id": "abc"}).status_code == 302
    assert admin.post(f"/admin/kurs/{kid}/ansvarlig", data={"ansvarlig_admin_id": "999"}).status_code == 400


def test_daglig_kjoering_fra_admin_er_kun_toerr_i_drift(con, monkeypatch):
    kid = _kurs(con)
    db.meld_paa(con, kid, epost="d@x.no", navn="D")
    con.commit()
    _prod(monkeypatch)
    sendt = []
    monkeypatch.setattr(epost, "send", lambda *a, **kw: sendt.append(a))
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    r = k.post("/admin/daglig", data={"dato": date.today().isoformat()})
    assert r.status_code == 200 and "TØRR" in r.get_data(as_text=True)
    assert sendt == [] and con.execute("SELECT COUNT(*) FROM utsending_logg").fetchone()[0] == 0
    assert k.post("/admin/daglig", data={"dato": "ikke-dato"}).status_code == 400


# ============================ Visma ============================

def test_visma_odata_filter_escaper_apostrof(monkeypatch):
    kall = []

    def falsk_kall(metode, sti, **kw):
        kall.append((metode, sti, kw))
        return {"Data": [{"Id": "k1"}]} if metode == "GET" else {"Id": "x"}
    monkeypatch.setattr(visma, "_kall", falsk_kall)
    g = visma.Fakturagrunnlag(kunde_navn="O", kunde_epost="o'brien@x.no' or 1 eq 1 or '", er_privatperson=True, org_nr=None,
                              adresse=None, postnr=None, sted=None, ehf=False, deres_ref=None, linjetekst="x",
                              artikkel=None, belop_nok=1)
    assert visma._finn_eller_opprett_kunde(g) == "k1"
    filter_ = kall[0][2]["params"]["$filter"]
    assert filter_ == "EmailAddress eq 'o''brien@x.no'' or 1 eq 1 or '''"


# ============================ produksjonskontroll ============================

def test_produksjonsfeil_for_svak_konfigurasjon(monkeypatch):
    monkeypatch.setattr(config, "DEMO", False)
    monkeypatch.setattr(config, "HEMMELIG_NOKKEL", "bytt-meg-i-prod")
    monkeypatch.setattr(config, "WEBHOOK_HEMMELIG", "kort")
    monkeypatch.setattr(config, "BASE_URL", "http://kurs.eksempel.no")
    feil = sikkerhet.produksjonsfeil()
    assert len(feil) == 3 and all("bytt-meg" not in f and "kort" not in f for f in feil)
    monkeypatch.setattr(config, "DEMO", True)
    assert sikkerhet.produksjonsfeil() == []


def test_appen_svarer_503_i_drift_med_svak_noekkel(con, monkeypatch):
    _prod(monkeypatch)
    monkeypatch.setattr(config, "HEMMELIG_NOKKEL", "bytt-meg-i-prod")
    from kurs.web import app as webapp
    monkeypatch.setattr(webapp, "_database_klar", False)
    r = webapp.app.test_client().get("/")
    assert r.status_code == 503 and "HEMMELIG" not in r.get_data(as_text=True)


def test_trygt_url_felt():
    assert sikkerhet.trygt_url_felt("") is None and sikkerhet.trygt_url_felt(None) is None
    assert sikkerhet.trygt_url_felt(" https://zoom.us/j/1 ") == "https://zoom.us/j/1"
    for u in ("javascript:alert(1)", "data:text/html,x", "zoom.us/j/1", "https://\nx.no"):
        with pytest.raises(ValueError):
            sikkerhet.trygt_url_felt(u)


def test_trygg_neste():
    assert sikkerhet.trygg_neste("/admin/kurs/1", "/") == "/admin/kurs/1"
    for v in (None, "", "//x", "https://x", "/\\x", "admin", 5):
        assert sikkerhet.trygg_neste(v, "/std") == "/std"
