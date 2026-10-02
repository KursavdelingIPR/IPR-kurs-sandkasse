"""Innlogging til kurssiden (K7): én innloggingsside `/logg-inn` som en vanlig lenke fra terapiakademiet.no.

`neste` (en sti på dette nettstedet) følger med hele veien: lenken -> skjult felt -> e-postlenken -> mottak, så deltakeren lander på riktig
kursside. Sikker omdirigering (aldri til et annet nettsted), admin-økten bevares, takbegrensning og at innloggingslenker aldri havner i
e-posthistorikken. Bare oppdiktede data.
"""
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from kurs import config, db
from kurs.integrasjoner import epost
from kurs.web import sikkerhet

from kurssidehjelp import IDAG, admin_klient, csrf, deltaker_klient, dokument, fast_dato, lag_deltaker, lag_kurs, ny_database, skriv_side, tekstblokk


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    fast_dato(monkeypatch)
    yield c
    c.close()


@pytest.fixture
def kurs(con):
    """Kurs K1 med publisert side og en bekreftet deltaker (kari@example.no)."""
    kid = lag_kurs(con, "K1")
    skriv_side(con, kid, dokument(tekstblokk("Velkommen", "<p>Hei fra kurssiden</p>"), tittel="Kurssiden vår"))
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    return kid, did


def _klient():
    from kurs.web import app as webapp
    return webapp.app.test_client()


def _be_om_lenke(k, epost_="kari@example.no", **kw):
    return k.post("/logg-inn", data={"epost": epost_, **kw})


def _lenke_i_flash(html: str) -> str:
    treff = re.search(r'href="([^"]*/logg-inn/[^"]+)"', html)
    assert treff, html[:1500]
    return treff.group(1).replace("&amp;", "&").replace("&#39;", "'").replace("&#34;", '"')


def _sti(lenke: str) -> str:
    deler = urlsplit(lenke)
    return deler.path + ("?" + deler.query if deler.query else "")


def _token_lenke(con) -> str:
    rad = con.execute("SELECT token FROM innlogging_token ORDER BY rowid DESC").fetchone()
    return f"/logg-inn/{rad[0]}"


# ============================ neste gjennom hele veien ============================

def test_neste_i_skjemaet_havner_i_innloggingslenken_i_e_post_og_demo_flash(con, kurs):
    k = _klient()
    r = _be_om_lenke(k, neste="/kurs/K1/deltakerside")
    lenke = _lenke_i_flash(r.get_data(as_text=True))
    assert parse_qs(urlsplit(lenke).query)["neste"] == ["/kurs/K1/deltakerside"]
    mail = epost.les_utboks()[0]
    assert mail["til"] == "kari@example.no" and mail["emne"] == "Logg inn – IPR Påmeldingssystem"      # emnet sier ikke lenger «Min side»: deltakeren lander på kurssiden
    treff = re.search(r'href="([^"]*/logg-inn/[^"]+)"', mail["html"])
    assert treff and parse_qs(urlsplit(treff.group(1).replace("&amp;", "&")).query)["neste"] == ["/kurs/K1/deltakerside"]


def test_neste_i_adressen_til_innloggingssiden_settes_inn_som_skjult_felt_og_bevares(con, kurs):
    k = _klient()
    html = k.get("/logg-inn?neste=/kurs/K1/deltakerside").get_data(as_text=True)
    assert '<input type="hidden" name="neste" value="/kurs/K1/deltakerside">' in html and "<h1>Logg inn</h1>" in html
    assert 'name="neste"' not in k.get("/logg-inn").get_data(as_text=True)
    assert 'name="neste"' not in k.get("/logg-inn?neste=https://ond.example").get_data(as_text=True)      # usikker neste forkastes


def test_hele_veien_fra_lenken_paa_terapiakademiet_til_riktig_kursside(con, kurs):
    k = _klient()
    side = k.get("/logg-inn?neste=/kurs/K1/deltakerside").get_data(as_text=True)                     # 1. deltakeren klikker lenken
    assert 'name="neste"' in side
    r = _be_om_lenke(k, neste="/kurs/K1/deltakerside")                                               # 2. skriver e-post (skjult felt følger med)
    lenke = _sti(_lenke_i_flash(r.get_data(as_text=True)))
    r = k.get(lenke)                                                                                 # 3. klikker lenken i e-posten
    assert r.status_code == 302 and r.headers["Location"] == "/kurs/K1/deltakerside"
    r = k.get(r.headers["Location"])                                                                 # 4. lander på kurssiden
    assert r.status_code == 200 and "Kurssiden vår" in r.get_data(as_text=True)


def test_uten_neste_lander_deltakeren_paa_kurssiden_naar_det_er_ett_tydelig_kurs(con, kurs):
    k = _klient()
    lenke = _sti(_lenke_i_flash(_be_om_lenke(k).get_data(as_text=True)))
    assert "neste" not in lenke
    r = k.get(lenke)
    assert r.status_code == 302 and r.headers["Location"] == "/kurs/K1/deltakerside"


def test_uten_kurssider_lander_deltakeren_paa_min_side(con):
    kid = lag_kurs(con, "K1")                                                                        # ingen kursside
    lag_deltaker(con, kid, "kari@example.no")
    k = _klient()
    r = k.get(_sti(_lenke_i_flash(_be_om_lenke(k).get_data(as_text=True))))
    assert r.status_code == 302 and r.headers["Location"] == "/min-side"


@pytest.mark.parametrize("neste", ["https://ond.example", "//ond.example/x", "/\\ond.example", "\\\\ond.example", "javascript:alert(1)", "http://ond.example/kurs/K1/deltakerside",
                                   "ond.example", "", "data:text/html,x", "  /med-mellomrom-foran-uten-skille"])
def test_usikker_neste_gir_landing_aldri_omdirigering_til_annet_nettsted(con, kurs, neste):
    con.execute("INSERT INTO innlogging_token (token, deltaker_id, utloper) VALUES ('tok', ?, '2099-01-01T00:00:00')", (kurs[1],))
    con.commit()
    r = _klient().get("/logg-inn/tok", query_string={"neste": neste})
    assert r.status_code == 302 and r.headers["Location"] in ("/kurs/K1/deltakerside", "/min-side") and "ond.example" not in r.headers["Location"]
    if neste.startswith("  /"):
        assert r.headers["Location"] != neste


def test_usikker_neste_fra_skjemaet_kommer_aldri_med_i_lenken(con, kurs):
    for neste in ("https://ond.example", "//ond.example", "javascript:alert(1)"):
        html = _be_om_lenke(_klient(), neste=neste).get_data(as_text=True)
        assert "neste" not in _lenke_i_flash(html) and "ond.example" not in _lenke_i_flash(html)


def test_relativ_neste_godtas_ogsaa_til_andre_sider_paa_nettstedet(con, kurs):
    con.execute("INSERT INTO innlogging_token (token, deltaker_id, utloper) VALUES ('tok', ?, '2099-01-01T00:00:00')", (kurs[1],))
    con.commit()
    r = _klient().get("/logg-inn/tok?neste=/min-side")
    assert r.headers["Location"] == "/min-side"


def test_utloept_eller_brukt_lenke_beholder_neste(con, kurs):
    con.execute("INSERT INTO innlogging_token (token, deltaker_id, utloper) VALUES ('gammel', ?, '2000-01-01T00:00:00')", (kurs[1],))
    con.execute("INSERT INTO innlogging_token (token, deltaker_id, utloper, brukt) VALUES ('brukt', ?, '2099-01-01T00:00:00', 1)", (kurs[1],))
    con.commit()
    for tok in ("gammel", "brukt", "finnes-ikke"):
        r = _klient().get(f"/logg-inn/{tok}?neste=/kurs/K1/deltakerside")
        assert r.status_code == 302 and r.headers["Location"] == "/logg-inn?neste=/kurs/K1/deltakerside", tok
        assert "Lenken er utløpt eller allerede brukt" in _klient().get("/logg-inn/gammel?neste=/x", follow_redirects=True).get_data(as_text=True)
    r = _klient().get("/logg-inn/gammel?neste=https://ond.example")
    assert r.headers["Location"] == "/logg-inn"                                                      # usikker neste tas ikke med


def test_innloggingslenken_kan_bare_brukes_en_gang(con, kurs):
    k = _klient()
    lenke = _sti(_lenke_i_flash(_be_om_lenke(k, neste="/kurs/K1/deltakerside").get_data(as_text=True)))
    assert _klient().get(lenke).status_code == 302
    assert _klient().get(lenke).headers["Location"].startswith("/logg-inn")


def test_allerede_innlogget_hoppes_videre_til_neste_eller_landing(con, kurs):
    k = deltaker_klient(kurs[1])
    r = k.get("/logg-inn?neste=/kurs/K1/deltakerside")
    assert r.status_code == 302 and r.headers["Location"] == "/kurs/K1/deltakerside"
    r = k.get("/logg-inn")
    assert r.status_code == 302 and r.headers["Location"] == "/kurs/K1/deltakerside"                # landing: ett tydelig kurs
    r = k.get("/logg-inn?neste=https://ond.example")
    assert r.status_code == 302 and r.headers["Location"] == "/kurs/K1/deltakerside"
    assert deltaker_klient(kurs[1]).get("/logg-inn?neste=/min-side").headers["Location"] == "/min-side"


def test_gammel_okt_for_en_slettet_person_gir_innloggingsskjema_og_fjerner_oekten(con, kurs):
    k = deltaker_klient(9999)
    r = k.get("/logg-inn")
    assert r.status_code == 200 and "Send innloggingslenke" in r.get_data(as_text=True)
    with k.session_transaction() as s:
        assert "deltaker_id" not in s


def test_min_side_sender_aldri_videre_selv_med_en_kursside(con, kurs):
    r = deltaker_klient(kurs[1]).get("/min-side")
    assert r.status_code == 200 and "Mine kurs" in r.get_data(as_text=True)


def test_krever_deltaker_sender_uinnloggede_med_neste(con, kurs):
    for sti in ("/min-side", "/kurs/K1/deltakerside", "/kurs/K1/deltakerside/fil/1"):
        r = _klient().get(sti)
        assert r.status_code == 302 and r.headers["Location"] == f"/logg-inn?neste={sti}", sti


# ============================ økten ============================

def test_innlogging_som_deltaker_logger_ikke_ut_en_admin_i_samme_nettleser(con, kurs):
    k = admin_klient()
    assert k.get("/admin").status_code == 200
    with k.session_transaction() as s:
        s["demo_dato"] = IDAG.isoformat()
    lenke = _sti(_lenke_i_flash(_be_om_lenke(k, neste="/kurs/K1/deltakerside").get_data(as_text=True)))
    r = k.get(lenke)
    assert r.status_code == 302 and r.headers["Location"] == "/kurs/K1/deltakerside"
    with k.session_transaction() as s:
        assert s["deltaker_id"] == kurs[1] and s["admin_id"] and s["admin_brukernavn"] == "admin" and s["demo_dato"] == IDAG.isoformat()
        assert s.permanent is True
    assert k.get("/admin").status_code == 200                                                        # admin-økten står


def test_ny_deltakerokt_fornyer_sesjonen_og_csrf_mot_fixation(con, kurs):
    k = _klient()
    with k.session_transaction() as s:
        s["csrf"] = "gammelt-token"
        s["noe_annet"] = "fra-en-annen-identitet"
    con.execute("INSERT INTO innlogging_token (token, deltaker_id, utloper) VALUES ('tok', ?, '2099-01-01T00:00:00')", (kurs[1],))
    con.commit()
    k.get("/logg-inn/tok")
    with k.session_transaction() as s:
        assert "noe_annet" not in s and s.get("csrf") != "gammelt-token" and s["deltaker_id"] == kurs[1]


def test_logg_ut_fjerner_bare_deltakerokten_naar_en_admin_er_innlogget(con, kurs):
    k = admin_klient()
    with k.session_transaction() as s:
        s["deltaker_id"] = kurs[1]
    r = k.get("/logg-ut")
    assert r.status_code == 302 and r.headers["Location"] == "/"
    with k.session_transaction() as s:
        assert "deltaker_id" not in s and s["admin_id"]
    assert k.get("/admin").status_code == 200


def test_logg_ut_uten_admin_toemmer_hele_sesjonen(con, kurs):
    k = deltaker_klient(kurs[1])
    with k.session_transaction() as s:
        s["demo_dato"] = "2027-01-01"
    assert k.get("/logg-ut").status_code == 302
    with k.session_transaction() as s:
        assert dict(s) == {}


def test_utloept_admin_okt_i_samme_nettleser_beholdes_ikke_som_admin(con, kurs):
    k = admin_klient()
    with k.session_transaction() as s:
        s["admin_sist"] = 0                                                                          # utløpt økt
    con.execute("INSERT INTO innlogging_token (token, deltaker_id, utloper) VALUES ('tok', ?, '2099-01-01T00:00:00')", (kurs[1],))
    con.commit()
    k.get("/logg-inn/tok")
    assert k.get("/admin").status_code == 302                                                        # fortsatt ikke admin (økten var utløpt)


# ============================ sikkerhet rundt innlogging ============================

def test_demo_flash_og_skjult_felt_escaper_neste(con, kurs):
    ond = '/x"><script>alert(1)</script>'
    html = _be_om_lenke(_klient(), neste=ond).get_data(as_text=True)
    assert "<script>alert(1)" not in html and ("&lt;script&gt;" in html or "%3Cscript%3E" in html)
    html = _klient().get("/logg-inn", query_string={"neste": ond}).get_data(as_text=True)
    assert "<script>alert(1)" not in html and 'value="/x&#34;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"' in html


def test_innloggingslenken_lagres_aldri_i_e_posthistorikken(con, kurs):
    _be_om_lenke(_klient(), neste="/kurs/K1/deltakerside")
    assert con.execute("SELECT COUNT(*) FROM sendt_epost").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM utsending_logg").fetchone()[0] == 0


def test_ukjent_epost_gir_samme_svar_og_ingen_lenke(con, kurs):
    r = _be_om_lenke(_klient(), "ukjent@example.no", neste="/kurs/K1/deltakerside")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "Hvis e-postadressen er registrert hos oss" in html and "/logg-inn/" not in html
    assert con.execute("SELECT COUNT(*) FROM innlogging_token").fetchone()[0] == 0


def test_sendt_tekst_forteller_at_lenken_virker_i_30_minutter(con, kurs):
    assert "30 minutter" in _be_om_lenke(_klient()).get_data(as_text=True)


def test_ip_grensen_for_innloggingslenker_er_30_og_epostgrensen_er_3(con, kurs):
    assert sikkerhet.GRENSER["innloggingslenke"] == (30, 15 * 60) and sikkerhet.GRENSER["innloggingslenke_epost"] == (3, 15 * 60)
    k = _klient()
    for n in range(sikkerhet.GRENSER["innloggingslenke"][0]):
        assert _be_om_lenke(k, f"person{n}@example.no").status_code == 200                          # kurslokale: mange fra samme IP
    assert _be_om_lenke(k, "en-til@example.no").status_code == 429
    sikkerhet.takbegrenser.nullstill()
    for _ in range(sikkerhet.GRENSER["innloggingslenke_epost"][0]):
        assert _be_om_lenke(k, "kari@example.no").status_code == 200
    assert _be_om_lenke(k, "kari@example.no").status_code == 429


def test_innloggingssiden_har_ingen_lenke_til_kodeinnsjekk_og_ingen_passordfelt(con):
    """Kodesiden /innsjekk er fjernet: innsjekk uten QR-kode gjøres på Min side etter innlogging."""
    html = _klient().get("/logg-inn").get_data(as_text=True)
    assert 'href="/innsjekk"' not in html and "Sjekk inn med kode" not in html
    assert "type=\"password\"" not in html and "Ingen passord" in html and "For deg som er påmeldt" in html


def test_innloggingssiden_kan_ikke_bygges_inn_i_en_ramme(con):
    r = _klient().get("/logg-inn?neste=/kurs/K1/deltakerside")
    assert r.headers["X-Frame-Options"] == "DENY" and "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert 'method="post"' in r.get_data(as_text=True) and "csrf_token" in r.get_data(as_text=True)     # skjema med CSRF: kan ikke postes fra et annet nettsted


def test_innlogging_uten_csrf_avvises(con, kurs):
    k = _klient()
    k.injiser_csrf = False
    assert k.post("/logg-inn", data={"epost": "kari@example.no"}).status_code == 400
    assert con.execute("SELECT COUNT(*) FROM innlogging_token").fetchone()[0] == 0
