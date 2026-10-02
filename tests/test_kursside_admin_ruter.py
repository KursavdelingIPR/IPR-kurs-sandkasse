"""Kursside i administrasjonen: rutene (kurs/web/kursside_admin_ruter.py) - lagre med versjonskontroll, konflikt, publisering, kontroll,
innstillinger, forhåndsvisning (rammeheadere), filer (rå opplasting, servering, sletting), mal, hent fra annet kurs, versjoner, roller.

Backend-tester: selve redigeringsvisningen er foreløpig en enkel, midlertidig mal (det ferdige utseendet lages i et eget byggetrinn).
Bare oppdiktede data.
"""
import json
import re
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

import pytest

from kurs import config, db
from kurs import sideinnhold as si
from kurs import sidelager
from kurs.web import sikkerhet

from kurssidehjelp import (IDAG, admin_klient, csrf, dokument, fast_dato, gif, jpeg, json_post, lag_deltaker, lag_kurs, ny_database, ole, pdf,
                           png, skriv_side, tekstblokk, webp, zip_lik)

MAKS_FIL = max(config.KURSSIDE_FIL_MAKS_MB, config.KURSSIDE_BILDE_MAKS_MB) * 1024 * 1024


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    fast_dato(monkeypatch)
    yield c
    c.close()


@pytest.fixture
def kid(con):
    return lag_kurs(con)


@pytest.fixture
def admin(con):
    return admin_klient()


def _u(kid, hale=""):
    return f"/admin/kurs/{kid}/kursside{hale}"


def _tilstand(con):
    """Alt kursside-testene ikke skal endre ved GET: kursside-tabellene og hendelsene."""
    rader = {t: [tuple(r) for r in con.execute(f"SELECT * FROM {t} ORDER BY 1")] for t in ("kursside", "kursside_versjon", "kursside_fil", "kursside_fil_innhold")}
    return rader, [tuple(r) for r in con.execute("SELECT aktor, handling, detaljer FROM hendelse ORDER BY id")]


def _lagre(klient, kid, dok, versjon=0):
    return json_post(klient, _u(kid, "/lagre"), {"versjon": versjon, "dokument": dok})


def _fil(klient, kid, navn, data, **hoder):
    """Rå opplasting slik nettleseren sender den. En tom fil sendes med «Content-Length: 0» (testklienten utelater lengden ellers)."""
    ekstra = {"environ_overrides": {"CONTENT_LENGTH": "0"}} if not data else {}
    return klient.post(_u(kid, "/fil"), data=data, content_type="application/octet-stream",
                       headers={"X-CSRF-Token": csrf(klient), "X-Filnavn": quote(navn), **hoder}, **ekstra)


# ============================ fane og lenker ============================

def test_fanen_kursside_finnes_paa_alle_kurssidene_og_peker_til_siden(con, kid, admin):
    for side in ("oppsett", "nettside", "paameldingsskjema", "deltakere", "kommunikasjon", "kursside"):
        html = admin.get(f"/admin/kurs/{kid}/{side}").get_data(as_text=True)
        assert f'href="/admin/kurs/{kid}/kursside">Min side</a>' in html, side
    assert 'class="aktiv" href="/admin/kurs/%d/kursside">Min side' % kid in admin.get(_u(kid)).get_data(as_text=True)


def test_kursoverskriften_har_lenke_til_forhandsvisningen_og_aktiviteter_har_knapp(con, kid, admin):
    html = admin.get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True)
    assert f'href="/admin/kurs/{kid}/kursside/forhandsvis?versjon=publisert" target="_blank" rel="noopener"' in html and "Min side ↗" in html
    aktiviteter = admin.get("/admin").get_data(as_text=True)
    assert re.search(rf'href="/admin/kurs/{kid}/kursside"[^>]*>Min side</a>', aktiviteter)
    assert re.search(rf'href="/admin/kurs/{kid}/kursside"[^>]*>Min side</a>',
                     admin_klient(con, "lese").get("/admin").get_data(as_text=True))       # knappen vises for alle roller


def test_siden_gir_lenke_til_terapiakademiet_og_forklarer_forskjellen_paa_nettside(con, kid, admin):
    data = _sidedata(admin.get(_u(kid)).get_data(as_text=True))
    assert data["lenker"] == {"direkte": f"{config.BASE_URL}/logg-inn?neste=/kurs/K1/deltakerside", "generell": f"{config.BASE_URL}/logg-inn"}
    html = admin.get(_u(kid)).get_data(as_text=True)
    assert "siden deltakerne ser etter at de har logget inn" in html and f"/admin/kurs/{kid}/nettside" in html


def _sidedata(html: str) -> dict:
    return json.loads(re.search(r'<script type="application/json" id="ks-data"[^>]*>(.*?)</script>', html, re.S).group(1))


def test_sidedata_har_det_redigeringsvisningen_trenger(con, kid, admin):
    lag_deltaker(con, kid)
    data = _sidedata(admin.get(_u(kid)).get_data(as_text=True))
    assert data["kurs"] == {"id": kid, "navn": "Kurs K1", "kode": "K1", "type": "fysisk", "sted": "Bergen", "har_zoom": False, "status": "aapen"}
    assert data["versjon"] == 0 and data["publisert"] is None and data["publisert_versjon"] is None and data["aktiv"] is True
    assert data["dokument"] == si.tomt_dokument() and data["antall_deltakere"] == 1 and data["rolle"] == "system" and data["redigerer"] is None
    assert data["innstillinger"] == {"innsjekk_krever_kode": True, "stenges": None, "apen_dager": None, "standard_dager": 180,
                                     "apen_dager_min": 1, "apen_dager_maks": 3650, "siste_kursdag": (IDAG + timedelta(days=1)).isoformat(),
                                     "stenges_effektiv": (IDAG + timedelta(days=1 + 180)).isoformat()}
    assert [d["dato"] for d in data["kursdager"]] == [IDAG.isoformat(), (IDAG + timedelta(days=1)).isoformat()]
    assert set(data["urls"]) >= {"lagre", "publiser", "sjekk", "fil_opp", "fil", "forhandsvis", "mal", "hent_fra", "kilder", "gjenopprett", "token"}
    assert data["grenser"]["fil_mb"] == 15 and "pdf" in data["grenser"]["endelser"] and "svg" not in data["grenser"]["endelser"]


# ============================ GET skriver aldri, tilgang ============================

def test_get_skriver_aldri_til_databasen(con, kid, admin):
    uten_side = lag_kurs(con, "K2")
    skriv_side(con, kid, dokument(tekstblokk()))
    lag_deltaker(con, kid)
    foer = _tilstand(con)
    for hale in ("", "/sjekk", "/forhandsvis", "/forhandsvis?versjon=publisert", "/kilder", "/deltakernavn", "/token"):
        assert admin.get(_u(kid, hale)).status_code == 200, hale
    assert _tilstand(con) == foer
    assert admin.get(_u(uten_side)).status_code == 200 and admin.get(_u(uten_side, "/forhandsvis")).status_code == 200
    assert _tilstand(con) == foer                                                                # kurs uten side: heller ingen rad


def test_ukjent_kurs_gir_404_paa_alle_ruter(con, admin):
    for hale in ("", "/sjekk", "/forhandsvis", "/kilder", "/deltakernavn", "/token", "/fil/1"):
        assert admin.get(_u(9999, hale)).status_code == 404, hale
    for hale in ("/lagre", "/redigerer", "/publiser", "/aktiv", "/innstillinger", "/mal", "/hent-fra", "/fil", "/fil/1/slett", "/versjon/1/gjenopprett"):
        assert json_post(admin, _u(9999, hale), {}).status_code == 404, hale


def test_uinnlogget_sendes_til_innlogging_ogsaa_for_json_kall(con, kid):
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    foer = _tilstand(con)
    for r in (k.get(_u(kid)), k.get(_u(kid, "/forhandsvis")), k.get(_u(kid, "/fil/1")), json_post(k, _u(kid, "/lagre"), {"versjon": 0, "dokument": {}}),
              k.post(_u(kid, "/fil"), data=b"x", content_type="application/octet-stream", headers={"X-CSRF-Token": csrf(k)})):
        assert r.status_code == 302 and "/admin/logg-inn" in r.headers["Location"] and not r.is_json
    assert _tilstand(con) == foer


def test_lesetilgang_ser_siden_skrivebeskyttet_og_faar_403_paa_alle_endringer(con, kid):
    lese = admin_klient(con, "lese")
    skriv_side(con, kid, dokument(tekstblokk()))
    html = lese.get(_u(kid)).get_data(as_text=True)
    assert 'data-skrivebeskyttet="1"' in html and "Du har lesetilgang" in html
    assert lese.get(_u(kid, "/forhandsvis")).status_code == 200 and lese.get(_u(kid, "/sjekk")).status_code == 200
    assert lese.get(_u(kid, "/kilder")).status_code == 200 and lese.get(_u(kid, "/token")).status_code == 200
    assert lese.get(_u(kid, "/deltakernavn")).status_code == 403                                  # personnavn: bare kursadmin og system
    foer = _tilstand(con)
    for hale in ("/lagre", "/redigerer", "/publiser", "/aktiv", "/innstillinger", "/mal", "/hent-fra", "/versjon/1/gjenopprett", "/fil/1/slett"):
        assert json_post(lese, _u(kid, hale), {"versjon": 1, "dokument": si.tomt_dokument()}).status_code == 403, hale
    assert _fil(lese, kid, "a.pdf", pdf()).status_code == 403
    assert _tilstand(con) == foer
    assert 'data-skrivebeskyttet="0"' in admin_klient().get(_u(kid)).get_data(as_text=True)


def test_kursadmin_kan_endre_og_se_navn(con, kid):
    k = admin_klient(con, "kursadmin")
    assert _lagre(k, kid, dokument(tekstblokk())).status_code == 200 and k.get(_u(kid, "/deltakernavn")).status_code == 200


# ============================ lagre ============================

def test_foerste_lagring_versjon_null_gir_versjon_en_og_oppdatering_pluss_en(con, kid, admin):
    r = _lagre(admin, kid, dokument(tekstblokk("Velkommen", "<p>Hei</p>")))
    assert r.status_code == 200 and r.get_json()["status"] == "ok" and r.get_json()["versjon"] == 1 and r.get_json()["endringer"] is None
    assert re.fullmatch(r"\d\d:\d\d", r.get_json()["endret"])
    assert sidelager.hent_utkast(con, kid)[1] == 1
    r = _lagre(admin, kid, dokument(tekstblokk("Velkommen", "<p>Hei igjen</p>")), 1)
    assert r.get_json()["versjon"] == 2 and sidelager.hent(con, kid)["endret_av"] == "admin:admin"


def test_lagring_renser_og_gir_nye_id_er_og_merknader(con, kid, admin):
    dok = dokument(tekstblokk("Hei", "<p>ok</p><script>alert(1)</script><img src=x onerror=alert(1)>"),
                   {"id": "", "type": "lenker", "tittel": "Lenker", "data": {"lenker": [{"tittel": "Halv", "url": "https://"}]}})
    r = _lagre(admin, kid, dok)
    assert r.status_code == 200 and len(r.get_json()["merknader"]) == 1 and "Halv" in r.get_json()["merknader"][0]     # autolagring feiler ikke
    lagret, _ = sidelager.hent_utkast(con, kid)
    assert lagret["blokker"][0]["data"]["html"] == "<p>ok</p>" and all(si._BLOKK_ID.match(b["id"]) for b in lagret["blokker"])
    assert lagret["blokker"][1]["data"]["lenker"][0]["url"] == ""


def test_konflikt_gir_409_med_diff_og_siste_dokument_og_lagrer_ingenting(con, kid, admin):
    _lagre(admin, kid, dokument(tekstblokk("A", id="b_aaaa1111")))
    andre = admin_klient(con, "kursadmin", "marte")
    assert _lagre(andre, kid, dokument(tekstblokk("A", id="b_aaaa1111"), tekstblokk("Fra Marte", id="b_bbbb2222")), 1).status_code == 200
    r = _lagre(admin, kid, dokument(tekstblokk("A", "<p>mitt</p>", id="b_aaaa1111")), 1)                      # jeg kjenner fortsatt versjon 1
    j = r.get_json()
    assert r.status_code == 409 and j["status"] == "konflikt" and j["versjon"] == 2 and j["endret_av"] == "Bruker kursadmin"
    assert re.fullmatch(r"\d\d:\d\d", j["endret"])
    assert [b["tittel"] for b in j["dokument"]["blokker"]] == ["A", "Fra Marte"]                              # siste utkast, så ingenting går tapt
    assert any(d["art"] == "ny" and d["tittel"] == "Fra Marte" for d in j["diff"]) and all(d["tekst"] for d in j["diff"])
    assert sidelager.hent_utkast(con, kid)[1] == 2 and len(sidelager.hent_utkast(con, kid)[0]["blokker"]) == 2
    assert _lagre(admin, kid, dokument(tekstblokk("Ny")), 2).status_code == 200                                # med riktig versjon går det


def test_versjon_null_naar_siden_finnes_er_konflikt(con, kid, admin):
    _lagre(admin, kid, dokument(tekstblokk()))
    assert _lagre(admin_klient(con, "kursadmin", "marte"), kid, dokument(tekstblokk("Overskriver ikke")), 0).status_code == 409


@pytest.mark.parametrize("dok, delstreng", [
    (5, None), ({"v": 2}, "format"), ({"v": 1, "blokker": [{"type": "video", "data": {}}]}, "type"), ({"v": 1, "tittel": "a" * 200}, "for lang"),
    ({"v": 1, "blokker": [{"type": "tekst", "data": {"html": "<p>" + "a" * 21_000 + "</p>"}}]}, "for lang"),
    ({"v": 1, "blokker": [{"type": "tekst", "vis_fra": "i morgen", "data": {}}]}, "dato")])
def test_ugyldig_struktur_gir_422_uten_a_lagre(con, kid, admin, dok, delstreng):
    foer = _tilstand(con)
    r = json_post(admin, _u(kid, "/lagre"), {"versjon": 0, "dokument": dok})
    assert r.status_code in (422, 400) and r.get_json()["status"] == "feil" and r.get_json()["melding"]
    if delstreng:
        assert delstreng in r.get_json()["melding"]
    assert _tilstand(con) == foer


@pytest.mark.parametrize("kropp", [{}, {"versjon": "1", "dokument": {}}, {"versjon": -1, "dokument": {}}, {"versjon": True, "dokument": {}},
                                   {"versjon": 0}, {"versjon": 0, "dokument": []}])
def test_lagre_krever_versjon_og_dokument(con, kid, admin, kropp):
    assert json_post(admin, _u(kid, "/lagre"), kropp).status_code == 400


def test_for_stor_kropp_gir_413_og_manglende_lengde_gir_411(con, kid, admin):
    stor = {"versjon": 0, "dokument": {"v": 1, "tittel": "a" * (si.MAKS_KROPP_BYTES + 10)}}
    r = json_post(admin, _u(kid, "/lagre"), stor)
    assert r.status_code == 413 and r.get_json()["status"] == "feil"
    r = admin.post(_u(kid, "/lagre"), content_type="application/json", headers={"X-CSRF-Token": csrf(admin)})          # ingen Content-Length
    assert r.status_code == 411 and r.get_json()["status"] == "feil"
    assert sidelager.hent(con, kid) is None


def test_manglende_eller_feil_csrf_gir_400_og_lagrer_ingenting(con, kid, admin):
    foer = _tilstand(con)
    dok = {"versjon": 0, "dokument": si.tomt_dokument()}
    assert admin.post(_u(kid, "/lagre"), json=dok).status_code == 400
    assert admin.post(_u(kid, "/lagre"), json=dok, headers={"X-CSRF-Token": "feil"}).status_code == 400
    assert admin.post(_u(kid, "/fil"), data=pdf(), content_type="application/octet-stream", headers={"X-Filnavn": "a.pdf"}).status_code == 400
    assert _tilstand(con) == foer


def test_lagre_forer_ikke_til_hendelse_med_innhold(con, kid, admin):
    _lagre(admin, kid, dokument(tekstblokk("Hemmelig tittel", "<p>Hemmelig innhold</p>")))
    assert not [r for r in con.execute("SELECT detaljer FROM hendelse") if "Hemmelig" in (r[0] or "")]


def test_fil_referanser_til_andres_filer_fjernes_ved_lagring(con, kid, admin):
    andre = lag_kurs(con, "K2")
    andres, _ = sidelager.lagre_fil(con, andre, "andres.pdf", pdf(b"a"), "admin:test")
    con.commit()
    dok = dokument({"id": "b_filer001", "type": "filer", "tittel": "F", "data": {"filer": [{"fil_id": andres, "tittel": "Andres"}]}})
    r = _lagre(admin, kid, dok)
    assert r.status_code == 200 and r.get_json()["merknader"]
    assert sidelager.hent_utkast(con, kid)[0]["blokker"][0]["data"]["filer"] == []


# ============================ hjerteslag ============================

def test_redigerer_viser_en_annen_som_redigerer_og_utloper(con, kid, admin):
    assert json_post(admin, _u(kid, "/redigerer"), {}).get_json() == {"status": "ok", "versjon": 0, "annen": None}        # ingen side ennå
    _lagre(admin, kid, dokument(tekstblokk()))
    marte = admin_klient(con, "kursadmin", "marte")
    r = json_post(marte, _u(kid, "/redigerer"), {}).get_json()
    assert r["versjon"] == 1 and r["annen"]["navn"] == "Standardbruker" and r["annen"]["sekunder_siden"] < 60
    assert json_post(admin, _u(kid, "/redigerer"), {}).get_json()["annen"]["navn"] == "Bruker kursadmin"                 # nå er det Marte som redigerer
    sidelager.sett_redigerer(con, kid, "admin:marte", sekunder=-5)
    con.commit()
    assert json_post(admin, _u(kid, "/redigerer"), {}).get_json()["annen"] is None
    assert 'data-skrivebeskyttet="0"' in marte.get(_u(kid)).get_data(as_text=True)


# ============================ kontroll og publisering ============================

def test_sjekk_uten_side_gir_ingen_side_og_med_side_diff_feil_og_advarsler(con, kid, admin):
    assert admin.get(_u(kid, "/sjekk")).status_code == 409 and admin.get(_u(kid, "/sjekk")).get_json()["status"] == "ingen_side"
    lag_deltaker(con, kid, "kari@example.no")
    lag_deltaker(con, kid, "ola@example.no", "Ola", "Hansen")
    _lagre(admin, kid, dokument(tekstblokk("Ny", "<p>Ring 555 12 345</p>")))
    j = admin.get(_u(kid, "/sjekk")).get_json()
    assert j["status"] == "ok" and j["versjon"] == 1 and j["feil"] == [] and len(j["advarsler"]) == 1 and j["antall_deltakere"] == 2
    assert j["forrige_publisert"] is None and j["ingen_endringer"] is False and any(d["art"] == "ny" for d in j["diff"])


def test_publisering_lager_versjonsrad_hendelse_uten_innhold_og_publisert_side(con, kid, admin):
    _lagre(admin, kid, dokument(tekstblokk("Hemmelig blokktittel", "<p>Hemmelig tekst</p>")))
    r = json_post(admin, _u(kid, "/publiser"), {"versjon": 1})
    j = r.get_json()
    assert r.status_code == 200 and j["status"] == "ok" and j["publisert_versjon"] == 1
    assert [(v["arsak"], v["opprettet_av"]) for v in j["versjoner"]] == [("publisert", "Standardbruker")]
    rad = sidelager.hent(con, kid)
    assert rad["publisert"] == rad["utkast"] and rad["publisert_versjon"] == 1 and rad["publisert_av"] == "admin:admin"
    hendelse = con.execute("SELECT aktor, detaljer FROM hendelse WHERE handling='kursside_publisert'").fetchone()
    assert hendelse["aktor"] == "admin:admin" and json.loads(hendelse["detaljer"]) == {"kurs_id": kid, "versjon": 1, "antall_blokker": 1}
    assert "Hemmelig" not in hendelse["detaljer"]
    assert "publisert" in admin.get(_u(kid, "/sjekk")).get_json()["forrige_publisert"] or admin.get(_u(kid, "/sjekk")).get_json()["forrige_publisert"]


def test_publisering_med_gammel_versjon_gir_409_og_ingenting_publiseres(con, kid, admin):
    _lagre(admin, kid, dokument(tekstblokk("Ett")))
    _lagre(admin, kid, dokument(tekstblokk("To")), 1)
    r = json_post(admin, _u(kid, "/publiser"), {"versjon": 1})
    assert r.status_code == 409 and r.get_json()["status"] == "konflikt" and r.get_json()["versjon"] == 2
    assert sidelager.hent_publisert(con, kid) is None


def test_publisering_uten_side_gir_409_ingen_side(con, kid, admin):
    r = json_post(admin, _u(kid, "/publiser"), {"versjon": 1})
    assert r.status_code == 409 and r.get_json()["status"] == "ingen_side"


def test_blokkerende_feil_hindrer_publisering_og_advarsler_tillater_den(con, kid, admin):
    lag_deltaker(con, kid, "kari@example.no")
    _lagre(admin, kid, dokument(tekstblokk("Gruppe", "<p>Kontakt Kari@example.no for øvelsen</p>")))
    r = json_post(admin, _u(kid, "/publiser"), {"versjon": 1})
    assert r.status_code == 422 and r.get_json()["feil"] and "e-postadressen" in r.get_json()["feil"][0]["tekst"]
    assert sidelager.hent_publisert(con, kid) is None
    _lagre(admin, kid, dokument(tekstblokk("Gruppe", "<p>Ring 555 12 345 eller skriv til fremmed@example.no</p>")), 1)
    assert json_post(admin, _u(kid, "/publiser"), {"versjon": 2}).status_code == 200                                   # advarsler stopper ikke


def test_bilde_uten_alt_tekst_blokkerer_publisering(con, kid, admin):
    fil_id = _fil(admin, kid, "rom.png", png()).get_json()["fil"]["id"]
    _lagre(admin, kid, dokument({"id": "b_bilde001", "type": "bilde", "tittel": "Bilde", "data": {"fil_id": fil_id, "alt": "", "plassering": "topp"}}))
    r = json_post(admin, _u(kid, "/publiser"), {"versjon": 1})
    assert r.status_code == 422 and "alternativ tekst" in r.get_json()["feil"][0]["tekst"]


def test_publisere_uten_endringer_lager_ingen_ny_versjon(con, kid, admin):
    _lagre(admin, kid, dokument(tekstblokk()))
    json_post(admin, _u(kid, "/publiser"), {"versjon": 1})
    r = json_post(admin, _u(kid, "/publiser"), {"versjon": 1})
    assert r.status_code == 200 and r.get_json()["ingen_endringer"] is True and len(sidelager.versjoner(con, kid)) == 1
    assert admin.get(_u(kid, "/sjekk")).get_json()["ingen_endringer"] is True
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='kursside_publisert'").fetchone()[0] == 1


@pytest.mark.parametrize("kropp", [{}, {"versjon": 0}, {"versjon": "1"}, {"versjon": True}])
def test_publiser_krever_gyldig_versjon(con, kid, admin, kropp):
    _lagre(admin, kid, dokument(tekstblokk()))
    assert json_post(admin, _u(kid, "/publiser"), kropp).status_code == 400


# ============================ aktiv og innstillinger ============================

def test_aktiv_og_innstillinger_krever_lagret_side(con, kid, admin):
    for hale, kropp in (("/aktiv", {"aktiv": False}), ("/innstillinger", {"innsjekk_krever_kode": False})):
        r = json_post(admin, _u(kid, hale), kropp)
        assert r.status_code == 409 and r.get_json()["status"] == "ingen_side", hale


def test_aktiv_tar_siden_ned_og_logger_uten_innhold(con, kid, admin):
    _lagre(admin, kid, dokument(tekstblokk()))
    r = json_post(admin, _u(kid, "/aktiv"), {"aktiv": False})
    assert r.get_json() == {"status": "ok", "aktiv": False} and sidelager.hent(con, kid)["aktiv"] == 0
    assert json_post(admin, _u(kid, "/aktiv"), {"aktiv": True}).get_json()["aktiv"] is True and sidelager.hent(con, kid)["aktiv"] == 1
    assert json.loads(con.execute("SELECT detaljer FROM hendelse WHERE handling='kursside_aktiv' ORDER BY id").fetchone()[0]) == {"kurs_id": kid, "aktiv": False}
    assert json_post(admin, _u(kid, "/aktiv"), {"aktiv": "nei"}).status_code == 400


def test_innstillinger_lagres_valideres_og_gir_effektiv_dato(con, kid, admin):
    _lagre(admin, kid, dokument(tekstblokk()))
    standard = (IDAG + timedelta(days=1 + 180)).isoformat()
    r = json_post(admin, _u(kid, "/innstillinger"), {"innsjekk_krever_kode": False, "stenges": "2027-12-24"})
    assert r.get_json()["stenges_effektiv"] == "2027-12-24" and r.get_json()["status"] == "ok"
    rad = sidelager.hent(con, kid)
    assert (rad["innsjekk_krever_kode"], rad["stenges"]) == (0, "2027-12-24")
    assert json_post(admin, _u(kid, "/innstillinger"), {"stenges": ""}).get_json()["stenges_effektiv"] == standard            # tilbake til standard
    assert sidelager.hent(con, kid)["stenges"] is None and sidelager.hent(con, kid)["innsjekk_krever_kode"] == 0            # kode-valget uendret
    assert json_post(admin, _u(kid, "/innstillinger"), {"stenges": None}).get_json()["stenges_effektiv"] == standard
    for daarlig in ("24.12.2027", "2027-02-30", 5):
        assert json_post(admin, _u(kid, "/innstillinger"), {"stenges": daarlig}).status_code in (400, 422), daarlig
    assert json_post(admin, _u(kid, "/innstillinger"), {"innsjekk_krever_kode": "ja"}).status_code == 400
    felt = [json.loads(r[0])["felt"] for r in con.execute("SELECT detaljer FROM hendelse WHERE handling='kursside_innstillinger' ORDER BY id")]
    assert felt[0] == ["innsjekk_krever_kode", "stenges"] and felt[1] == ["stenges"]


# ============================ forhåndsvisning ============================

def _side_med_alt(con, kid):
    dok = dokument(tekstblokk("Synlig", "<p>Se meg</p>", id="b_synlig01"),
                   tekstblokk("Skjult blokk", "<p>Skjult tekst</p>", id="b_skjult01", skjult=True),
                   tekstblokk("Kommer senere", "<p>Fremtid tekst</p>", id="b_fremtid1", vis_fra=(IDAG + timedelta(days=3)).isoformat()),
                   tekstblokk("Tom", "<p></p>", id="b_tom00001"))
    skriv_side(con, kid, dok)


def test_forhandsvisning_er_eneste_rute_som_kan_vises_i_ramme(con, kid, admin):
    _side_med_alt(con, kid)
    r = admin.get(_u(kid, "/forhandsvis"))
    assert r.headers["X-Frame-Options"] == "SAMEORIGIN" and "frame-ancestors 'self'" in r.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" not in r.headers["Content-Security-Policy"] and "script-src 'self' 'nonce-" in r.headers["Content-Security-Policy"]
    nonce = re.search(r"'nonce-([^']+)'", r.headers["Content-Security-Policy"]).group(1)
    assert f'nonce="{nonce}"' in r.get_data(as_text=True)
    for url in (_u(kid), _u(kid, "/sjekk"), _u(kid, "/token"), "/admin", "/admin", f"/admin/kurs/{kid}/oppsett", "/", "/logg-inn"):
        h = admin.get(url).headers
        assert h["X-Frame-Options"] == "DENY" and "frame-ancestors 'none'" in h["Content-Security-Policy"], url


def test_forhandsvisningen_viser_bare_det_deltakerne_ville_sett(con, kid, admin):
    _side_med_alt(con, kid)
    for versjon in ("utkast", "publisert"):
        html = admin.get(_u(kid, f"/forhandsvis?versjon={versjon}")).get_data(as_text=True)
        assert "Se meg" in html and "Skjult tekst" not in html and "Fremtid tekst" not in html and "Skjult blokk" not in html
        assert 'id="blokk-b_skjult01"' not in html and 'id="blokk-b_tom00001"' not in html


def test_forhandsvisning_utkast_versjon_publisert_og_fallback(con, kid, admin):
    _lagre(admin, kid, dokument(tekstblokk("Utkast", "<p>Bare utkast</p>")))
    html = admin.get(_u(kid, "/forhandsvis?versjon=publisert")).get_data(as_text=True)          # ikke publisert ennå: viser utkastet
    assert "Bare utkast" in html
    json_post(admin, _u(kid, "/publiser"), {"versjon": 1})
    _lagre(admin, kid, dokument(tekstblokk("Utkast", "<p>Nytt utkast</p>")), 1)
    utkast = admin.get(_u(kid, "/forhandsvis?versjon=utkast")).get_data(as_text=True)
    publisert = admin.get(_u(kid, "/forhandsvis?versjon=publisert")).get_data(as_text=True)
    assert "Nytt utkast" in utkast and "deltakerne ser ikke dette" in utkast
    assert "Bare utkast" in publisert and "Nytt utkast" not in publisert and "Slik ser deltakerne siden nå" in publisert
    assert "Bare utkast" not in admin.get(_u(kid, "/forhandsvis")).get_data(as_text=True)          # standard = utkast


def test_forhandsvisning_uten_side_viser_tom_side_og_innsjekk_er_avslatt(con, kid, admin):
    html = admin.get(_u(kid, "/forhandsvis")).get_data(as_text=True)
    assert "ikke lagt ut noe mer ennå" in html
    dok = dokument(tekstblokk())
    _lagre(admin, kid, dok)
    html = admin.get(_u(kid, "/forhandsvis")).get_data(as_text=True)              # kursdag i dag: «I dag»-kortet finnes, men uten innsjekk
    assert 'id="i-dag"' in html and "Innsjekk er avslått i forhåndsvisningen" in html and "Registrer oppmøte" not in html


def test_forhandsvisning_kan_vise_som_en_bestemt_kursdag(con, kid, admin):
    _lagre(admin, kid, dokument(tekstblokk()))
    dag2 = con.execute("SELECT id FROM kursdag WHERE kurs_id=? ORDER BY dato DESC", (kid,)).fetchone()[0]
    html = admin.get(_u(kid, f"/forhandsvis?dag={dag2}")).get_data(as_text=True)
    assert "Dag 2 av 2" in html


def test_forhandsvisningen_bruker_adminruten_for_filer_ikke_deltakerruten(con, kid, admin):
    fil_id = _fil(admin, kid, "rom.png", png()).get_json()["fil"]["id"]
    _lagre(admin, kid, dokument({"id": "b_bilde001", "type": "bilde", "tittel": "Bilde", "data": {"fil_id": fil_id, "alt": "Rommet", "plassering": "bred"}}))
    html = admin.get(_u(kid, "/forhandsvis")).get_data(as_text=True)
    assert f'src="/admin/kurs/{kid}/kursside/fil/{fil_id}"' in html and "/deltakerside/fil/" not in html


# ============================ filer: opplasting ============================

def test_opplasting_lagrer_fil_med_riktig_svar_og_hendelse(con, kid, admin):
    r = _fil(admin, kid, "Dag 1 – Grunnmodell.pdf", pdf(b"x"))
    j = r.get_json()
    assert r.status_code == 200 and j["status"] == "ok" and j["duplikat"] is False
    assert j["fil"]["filnavn"] == "Dag 1 – Grunnmodell.pdf" and j["fil"]["type"] == "dokument" and j["fil"]["storrelse"] == len(pdf(b"x"))
    assert set(j["fil"]) == {"id", "filnavn", "type", "mimetype", "storrelse", "bredde", "hoyde", "opprettet", "opprettet_av"}
    assert j["kvote"]["antall"] == 1 and j["kvote"]["maks_bytes"] == 100 * 1024 * 1024
    h = json.loads(con.execute("SELECT detaljer FROM hendelse WHERE handling='kursside_fil_lastet_opp'").fetchone()[0])
    assert h == {"kurs_id": kid, "fil_id": j["fil"]["id"], "storrelse": len(pdf(b"x"))} and "Grunnmodell" not in json.dumps(h)


def test_x_filnavn_med_aesoeaa_og_prosentkoding(con, kid, admin):
    for navn in ("Øvelse på Åsane – ærlig talt.pdf", "mellom rom og %20 prosent.pdf"):
        j = _fil(admin, kid, navn, pdf(navn.encode("utf-8"))).get_json()
        assert j["fil"]["filnavn"] == navn


def test_samme_fil_to_ganger_gir_duplikat_uten_ny_rad_eller_hendelse(con, kid, admin):
    a = _fil(admin, kid, "a.pdf", pdf()).get_json()
    b = _fil(admin, kid, "annet-navn.pdf", pdf()).get_json()
    assert b["duplikat"] is True and b["fil"]["id"] == a["fil"]["id"] and b["kvote"]["antall"] == 1
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='kursside_fil_lastet_opp'").fetchone()[0] == 1


@pytest.mark.parametrize("navn, data, status", [
    ("skript.html", b"<script>alert(1)</script>", 415), ("bilde.svg", b"<svg onload=alert(1)>", 415), ("program.exe", b"MZ", 415),
    ("makro.docm", zip_lik(), 415), ("falsk.pdf", b"<html>", 415), ("falsk.png", jpeg(), 415), ("tom.pdf", b"", 400)])
def test_feil_type_eller_innhold_gir_json_med_riktig_status(con, kid, admin, navn, data, status):
    r = _fil(admin, kid, navn, data)
    assert r.status_code == status and r.get_json()["status"] == "feil" and r.get_json()["melding"]
    assert sidelager.fil_liste(con, kid) == []


def test_for_stor_fil_avvises_med_413_foer_kroppen_leses(con, kid, admin):
    r = admin.post(_u(kid, "/fil"), data=b"x", content_type="application/octet-stream", headers={"X-CSRF-Token": csrf(admin), "X-Filnavn": "stor.pdf"},
                   environ_overrides={"CONTENT_LENGTH": str(MAKS_FIL + 2)})
    assert r.status_code == 413 and r.get_json()["status"] == "feil" and "maks 15 MB" in r.get_json()["melding"]
    assert sidelager.fil_liste(con, kid) == []


def test_faktisk_for_stor_fil_avvises(con, kid, admin):
    r = _fil(admin, kid, "stor.pdf", pdf(b"0" * (config.KURSSIDE_FIL_MAKS_MB * 1024 * 1024)))
    assert r.status_code == 413 and "maks 15 MB" in r.get_json()["melding"] and sidelager.fil_liste(con, kid) == []


def test_bilde_over_bildegrensen_men_under_dokumentgrensen_avvises(con, kid, admin):
    r = _fil(admin, kid, "for-stort.png", png() + b"\x00" * (config.KURSSIDE_BILDE_MAKS_MB * 1024 * 1024 + 10))
    assert r.status_code == 413 and "maks 5 MB" in r.get_json()["melding"]


def test_manglende_lengde_gir_411(con, kid, admin):
    r = admin.post(_u(kid, "/fil"), content_type="application/octet-stream", headers={"X-CSRF-Token": csrf(admin), "X-Filnavn": "a.pdf"})   # ingen kropp, ingen lengde
    assert r.status_code == 411 and r.get_json()["status"] == "feil"


def test_kursets_kvote_gir_409(con, kid, admin, monkeypatch):
    monkeypatch.setattr(config, "KURSSIDE_MAKS_FILER", 2)
    _fil(admin, kid, "a.pdf", pdf(b"1"))
    _fil(admin, kid, "b.pdf", pdf(b"2"))
    r = _fil(admin, kid, "c.pdf", pdf(b"3"))
    assert r.status_code == 409 and "grensen" in r.get_json()["melding"]


def test_opplasting_takbegrenses_per_administrator_med_json_svar(con, kid, admin):
    maks = sikkerhet.GRENSER["kursside_opplasting"][0]
    for n in range(maks):
        assert _fil(admin, kid, f"f{n}.txt", f"innhold {n}".encode()).status_code == 200
    r = _fil(admin, kid, "en-til.txt", b"for mye")
    assert r.status_code == 429 and r.get_json()["status"] == "feil" and "For mange" in r.get_json()["melding"]
    assert sidelager.kvote(con, kid)["antall"] == maks


def test_alle_tillatte_filtyper_kan_lastes_opp_via_ruten(con, kid, admin):
    for navn, data in (("a.pdf", pdf()), ("a.pptx", zip_lik()), ("a.ppt", ole()), ("a.docx", zip_lik(b"1")), ("a.xlsx", zip_lik(b"2")), ("a.zip", zip_lik(b"3")),
                       ("a.txt", b"tekst"), ("a.png", png()), ("a.jpg", jpeg()), ("a.gif", gif()), ("a.webp", webp())):
        assert _fil(admin, kid, navn, data).status_code == 200, navn


# ============================ filer: servering til admin ============================

def test_admin_faar_filer_med_riktige_headere(con, kid, admin):
    d = _fil(admin, kid, "Øvelse – ærlig.pdf", pdf(b"hemmelig")).get_json()["fil"]
    r = admin.get(_u(kid, f"/fil/{d['id']}"))
    assert r.status_code == 200 and r.data == pdf(b"hemmelig") and r.headers["Content-Type"].startswith("application/octet-stream")
    assert r.headers["Content-Disposition"].startswith("attachment; filename=\"") and "filename*=UTF-8''%C3%98velse%20%E2%80%93%20%C3%A6rlig.pdf" in r.headers["Content-Disposition"]
    assert r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["Cache-Control"] == "private, no-store"
    assert r.headers["Content-Security-Policy"] == "sandbox; default-src 'none'"
    assert '"' not in r.headers["Content-Disposition"].split("filename=\"")[1].split('"')[0] and r.headers["X-Frame-Options"] == "DENY"


def test_bilder_vises_inline_med_sniffet_type_og_sandbox(con, kid, admin):
    for navn, data, type_ in (("a.png", png(), "image/png"), ("a.jpg", jpeg(), "image/jpeg"), ("a.gif", gif(), "image/gif"), ("a.webp", webp(), "image/webp")):
        d = _fil(admin, kid, navn, data).get_json()["fil"]
        r = admin.get(_u(kid, f"/fil/{d['id']}"))
        assert r.headers["Content-Type"] == type_ and r.headers["Content-Disposition"] == "inline", navn
        assert r.headers["Content-Security-Policy"] == "sandbox; default-src 'none'; img-src 'self'" and r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["Cache-Control"] == "private, max-age=300"


def test_filtypen_settes_av_serveren_aldri_fra_lagret_mimetype(con, kid, admin):
    d = _fil(admin, kid, "a.pdf", pdf()).get_json()["fil"]
    con.execute("UPDATE kursside_fil SET mimetype='text/html', type='dokument' WHERE id=?", (d["id"],))
    con.commit()
    assert admin.get(_u(kid, f"/fil/{d['id']}")).headers["Content-Type"].startswith("application/octet-stream")
    con.execute("UPDATE kursside_fil SET type='bilde' WHERE id=?", (d["id"],))              # feil type + innhold som ikke er et bilde
    con.commit()
    r = admin.get(_u(kid, f"/fil/{d['id']}"))
    assert r.headers["Content-Type"].startswith("application/octet-stream") and r.headers["Content-Disposition"].startswith("attachment")


def test_fil_fra_annet_kurs_gir_404_og_lese_kan_hente_filer(con, kid, admin):
    andre = lag_kurs(con, "K2")
    d = _fil(admin, andre, "a.pdf", pdf()).get_json()["fil"]
    assert admin.get(_u(kid, f"/fil/{d['id']}")).status_code == 404 and admin.get(_u(andre, f"/fil/{d['id']}")).status_code == 200
    assert admin_klient(con, "lese").get(_u(andre, f"/fil/{d['id']}")).status_code == 200


# ============================ filer: sletting ============================

def test_fil_i_bruk_kan_ikke_slettes_og_ubrukt_kan(con, kid, admin):
    d = _fil(admin, kid, "a.pdf", pdf()).get_json()["fil"]
    _lagre(admin, kid, dokument({"id": "b_filer001", "type": "filer", "tittel": "Presentasjoner", "data": {"filer": [{"fil_id": d["id"], "tittel": "A"}]}}))
    r = json_post(admin, _u(kid, f"/fil/{d['id']}/slett"), {})
    assert r.status_code == 409 and r.get_json() == {"status": "i_bruk", "melding": "Brukes i blokken «Presentasjoner»"}
    assert sidelager.fil_meta(con, kid, d["id"]) is not None
    _lagre(admin, kid, dokument(tekstblokk()), 1)
    r = json_post(admin, _u(kid, f"/fil/{d['id']}/slett"), {})
    assert r.status_code == 200 and r.get_json()["status"] == "ok" and r.get_json()["kvote"]["antall"] == 0
    assert sidelager.fil_meta(con, kid, d["id"]) is None
    assert json.loads(con.execute("SELECT detaljer FROM hendelse WHERE handling='kursside_fil_slettet'").fetchone()[0]) == {"kurs_id": kid, "fil_id": d["id"]}


def test_slette_fil_fra_annet_kurs_gir_404(con, kid, admin):
    andre = lag_kurs(con, "K2")
    d = _fil(admin, andre, "a.pdf", pdf()).get_json()["fil"]
    assert json_post(admin, _u(kid, f"/fil/{d['id']}/slett"), {}).status_code == 404 and sidelager.fil_meta(con, andre, d["id"]) is not None


# ============================ mal, hent fra annet kurs, versjoner ============================

def test_mal_erstatter_utkastet_tar_sikkerhetskopi_og_logger(con, kid, admin):
    _lagre(admin, kid, dokument(tekstblokk("Mitt utkast")))
    r = json_post(admin, _u(kid, "/mal"), {"versjon": 1, "mal": "standard"})
    j = r.get_json()
    assert r.status_code == 200 and j["versjon"] == 2 and [b["type"] for b in j["dokument"]["blokker"]] == ["program", "filer", "lenker", "kontakt"]
    assert [d["kursdag_id"] for d in j["dokument"]["blokker"][0]["data"]["dager"]] == [d["id"] for d in db.kursdager(con, kid)]
    kopi = sidelager.versjoner(con, kid)
    assert len(kopi) == 1 and kopi[0]["arsak"] == "sikkerhetskopi" and "mal" in kopi[0]["merknad"]
    assert json.loads(sidelager.hent_versjon(con, kid, kopi[0]["id"])["innhold"])["blokker"][0]["tittel"] == "Mitt utkast"
    assert json.loads(con.execute("SELECT detaljer FROM hendelse WHERE handling='kursside_mal'").fetchone()[0]) == {"kurs_id": kid, "mal": "standard"}
    assert json_post(admin, _u(kid, "/mal"), {"versjon": 2, "mal": "program"}).get_json()["dokument"]["blokker"][0]["type"] == "program"
    assert json_post(admin, _u(kid, "/mal"), {"versjon": 3, "mal": "tom"}).get_json()["dokument"]["blokker"] == []


def test_mal_konflikt_ukjent_mal_og_uten_side(con, kid, admin):
    assert json_post(admin, _u(kid, "/mal"), {"versjon": 1, "mal": "standard"}).get_json()["status"] == "ingen_side"
    _lagre(admin, kid, dokument(tekstblokk()))
    assert json_post(admin, _u(kid, "/mal"), {"versjon": 1, "mal": "finnes-ikke"}).status_code == 422
    r = json_post(admin, _u(kid, "/mal"), {"versjon": 7, "mal": "standard"})
    assert r.status_code == 409 and r.get_json()["status"] == "konflikt"
    assert sidelager.hent_utkast(con, kid)[1] == 1 and sidelager.versjoner(con, kid) == []


def test_hent_fra_annet_kurs_kopierer_valgte_blokker_filer_og_tomme_tabellrader(con, kid, admin):
    kilde = lag_kurs(con, "KILDE", start=IDAG - timedelta(days=100), dager=3)
    fil_id, _ = sidelager.lagre_fil(con, kilde, "kilde.pdf", pdf(b"kilde"), "admin:test")
    dok = dokument({"id": "b_tabell01", "type": "tabell", "tittel": "Grupper", "data": {"kolonner": ["Gruppe", "Deltakere"], "rader": [["1", "Kari N."]]}},
                   {"id": "b_filer001", "type": "filer", "tittel": "Presentasjoner", "data": {"filer": [{"fil_id": fil_id, "tittel": "K"}]}},
                   {"id": "b_progr001", "type": "program", "tittel": "Program", "data": {"dager": [
                       {"kursdag_id": None, "dato": "2026-01-01", "tittel": "Dag 1", "punkter": [{"tema": "T"}]}, {"tittel": "Dag 2", "punkter": []},
                       {"tittel": "Dag 3", "punkter": []}]}}, tekstblokk("Ikke valgt", id="b_ikkevalgt"))
    skriv_side(con, kilde, dok)
    _lagre(admin, kid, dokument(tekstblokk("Eksisterende", id="b_egen0001")))
    kilder = admin.get(_u(kid, "/kilder")).get_json()["kurs"]
    assert [k["navn"] for k in kilder] == ["Kurs KILDE"] and [b["id"] for b in kilder[0]["blokker"]] == ["b_tabell01", "b_filer001", "b_progr001", "b_ikkevalgt"]
    r = json_post(admin, _u(kid, "/hent-fra"), {"versjon": 1, "fra_kurs_id": kilde, "blokk_ider": ["b_tabell01", "b_filer001", "b_progr001"]})
    j = r.get_json()
    assert r.status_code == 200 and j["versjon"] == 2 and [b["type"] for b in j["dokument"]["blokker"]] == ["tekst", "tabell", "filer", "program"]
    tabell, filer, program = j["dokument"]["blokker"][1:]
    assert tabell["data"]["rader"] == [] and tabell["data"]["kolonner"] == ["Gruppe", "Deltakere"]        # aldri personer med
    ny_fil = filer["data"]["filer"][0]["fil_id"]
    assert ny_fil != fil_id and sidelager.fil_innhold(con, kid, ny_fil)[1] == pdf(b"kilde") and [f["id"] for f in j["filer"]] == [ny_fil]
    kursdager = [d["id"] for d in db.kursdager(con, kid)]
    assert [d["kursdag_id"] for d in program["data"]["dager"]] == [kursdager[0], kursdager[1], None]        # mappes i rekkefølge til dette kursets dager
    assert not ({"b_tabell01", "b_filer001", "b_progr001"} & {b["id"] for b in j["dokument"]["blokker"]})
    assert sidelager.versjoner(con, kid)[0]["arsak"] == "sikkerhetskopi"
    assert json.loads(con.execute("SELECT detaljer FROM hendelse WHERE handling='kursside_hentet_fra'").fetchone()[0]) == {"kurs_id": kid, "fra_kurs_id": kilde, "antall": 3}


def test_hent_fra_feilsituasjoner(con, kid, admin):
    andre = lag_kurs(con, "K2")
    assert json_post(admin, _u(kid, "/hent-fra"), {"versjon": 1, "fra_kurs_id": andre, "blokk_ider": ["b_x"]}).get_json()["status"] == "ingen_side"
    _lagre(admin, kid, dokument(tekstblokk("Egen", id="b_egen0001")))
    assert json_post(admin, _u(kid, "/hent-fra"), {"versjon": 1, "fra_kurs_id": andre, "blokk_ider": ["b_x"]}).status_code == 422      # kilden har ingen side
    assert json_post(admin, _u(kid, "/hent-fra"), {"versjon": 1, "fra_kurs_id": kid, "blokk_ider": ["b_egen0001"]}).status_code == 422  # ikke fra seg selv
    skriv_side(con, andre, dokument(tekstblokk("Andres", id="b_andre001")))
    assert json_post(admin, _u(kid, "/hent-fra"), {"versjon": 1, "fra_kurs_id": andre, "blokk_ider": []}).status_code == 422
    assert json_post(admin, _u(kid, "/hent-fra"), {"versjon": 1, "fra_kurs_id": andre, "blokk_ider": ["b_finnes_ikke"]}).status_code == 422
    assert json_post(admin, _u(kid, "/hent-fra"), {"versjon": 9, "fra_kurs_id": andre, "blokk_ider": ["b_andre001"]}).status_code == 409
    assert json_post(admin, _u(kid, "/hent-fra"), {"versjon": 1, "fra_kurs_id": "x", "blokk_ider": []}).status_code == 400
    assert [b["tittel"] for b in sidelager.hent_utkast(con, kid)[0]["blokker"]] == ["Egen"]                                               # ingenting lagret


def test_hent_fra_som_gir_for_mange_blokker_gir_422_og_ruller_tilbake_filene(con, kid, admin):
    andre = lag_kurs(con, "K2")
    fil_id, _ = sidelager.lagre_fil(con, andre, "a.pdf", pdf(b"a"), "admin:test")
    dok = dokument({"id": "b_filer001", "type": "filer", "tittel": "F", "data": {"filer": [{"fil_id": fil_id, "tittel": "A"}]}})
    skriv_side(con, andre, dok)
    _lagre(admin, kid, dokument(*[tekstblokk(id=f"b_{n:08d}") for n in range(si.MAKS_BLOKKER)]))
    r = json_post(admin, _u(kid, "/hent-fra"), {"versjon": 1, "fra_kurs_id": andre, "blokk_ider": ["b_filer001"]})
    assert r.status_code == 422 and sidelager.fil_liste(con, kid) == []


def test_gjenopprett_lager_utkast_av_versjonen_og_tar_sikkerhetskopi(con, kid, admin):
    _lagre(admin, kid, dokument(tekstblokk("Versjon A")))
    json_post(admin, _u(kid, "/publiser"), {"versjon": 1})
    _lagre(admin, kid, dokument(tekstblokk("Versjon B")), 1)
    vid = sidelager.versjoner(con, kid)[0]["id"]
    r = json_post(admin, _u(kid, f"/versjon/{vid}/gjenopprett"), {"versjon": 2})
    j = r.get_json()
    assert r.status_code == 200 and j["versjon"] == 3 and j["dokument"]["blokker"][0]["tittel"] == "Versjon A" and j["merknader"] == []
    assert sidelager.hent(con, kid)["publisert_versjon"] == 1                                    # ikke publisert
    assert [v["arsak"] for v in sidelager.versjoner(con, kid)] == ["sikkerhetskopi", "publisert"]
    assert json.loads(con.execute("SELECT detaljer FROM hendelse WHERE handling='kursside_gjenopprettet'").fetchone()[0]) == {"kurs_id": kid, "versjon_id": vid}
    assert json_post(admin, _u(kid, f"/versjon/{vid}/gjenopprett"), {"versjon": 2}).status_code == 409                    # gammel versjon


def test_gjenopprett_versjon_fra_annet_kurs_gir_404(con, kid, admin):
    andre = lag_kurs(con, "K2")
    skriv_side(con, andre, dokument(tekstblokk()))
    vid = sidelager.versjoner(con, andre)[0]["id"]
    _lagre(admin, kid, dokument(tekstblokk()))
    assert json_post(admin, _u(kid, f"/versjon/{vid}/gjenopprett"), {"versjon": 1}).status_code == 404


def test_deltakernavn_gir_bare_bekreftede_med_fornavn_og_forbokstav(con, kid, admin):
    lag_deltaker(con, kid, "kari@example.no", "Kari", "Nordmann")
    lag_deltaker(con, kid, "ola@example.no", "Ola", "Hansen")
    lag_deltaker(con, kid, "borte@example.no", "Bo", "Borte", status="avmeldt")
    j = admin.get(_u(kid, "/deltakernavn")).get_json()
    assert j == {"status": "ok", "navn": ["Kari N.", "Ola H."]}
    assert "@" not in json.dumps(j)


def test_token_gir_csrf_uten_a_skrive(con, kid, admin):
    foer = _tilstand(con)
    j = admin.get(_u(kid, "/token")).get_json()
    assert j["status"] == "ok" and j["csrf"] == csrf(admin) and _tilstand(con) == foer


# ============================ kopiering og sletting av kurs ============================

def test_duplisering_av_kurs_kopierer_ikke_kurssiden_men_kilder_viser_kurset(con, kid, admin):
    """Bevisst valg (SPEC 0.2 nr. 15): «Dupliser kurs» kopierer ikke kurssiden, og i hvert fall aldri gruppelister med navn. I stedet henter
    administratoren blokkene fra det gamle kurset med «Hent fra annet kurs»."""
    fil_id, _ = sidelager.lagre_fil(con, kid, "a.pdf", pdf(), "admin:test")
    dok = dokument(tekstblokk("Original"), {"id": "b_tabell01", "type": "tabell", "tittel": "Grupper",
                                            "data": {"kolonner": ["Gruppe", "Deltakere"], "rader": [["1", "Kari N."]]}},
                   {"id": "b_filer001", "type": "filer", "tittel": "F", "data": {"filer": [{"fil_id": fil_id, "tittel": "A"}]}})
    skriv_side(con, kid, dok)
    ny_start = IDAG + timedelta(days=400)
    r = admin.post("/admin/kurs/ny", data={
        "fra": str(kid), "navn": "Kopi av kurset", "type": "fysisk", "sted": "Bergen", "kapasitet": "16", "datoer": ny_start.isoformat(),
        "start_kl": "09:00", "slutt_kl": "16:00", "timer_pr_dag": "6", "pris_nok": "0", "fakturering": "person", "betaling": "samlet",
        "faktura_dager_for": "14", "ansvarlig_admin_id": "", "paameldingsfrist": "", "kursholder_navn": "", "kursholder_epost": "",
        "materiell_frist": "", "notat": ""})
    assert r.status_code == 302
    ny_id = con.execute("SELECT id FROM kurs WHERE navn='Kopi av kurset'").fetchone()[0]
    assert ny_id != kid and sidelager.hent(con, ny_id) is None and sidelager.fil_liste(con, ny_id) == []
    assert con.execute("SELECT COUNT(*) FROM kursside_versjon WHERE kurs_id=?", (ny_id,)).fetchone()[0] == 0
    kilder = admin.get(_u(ny_id, "/kilder")).get_json()["kurs"]
    assert [k["id"] for k in kilder] == [kid]                                                     # originalen kan hentes fra (etter at kopien har en side)
    assert sidelager.hent_publisert(con, kid)["blokker"][1]["data"]["rader"] == [["1", "Kari N."]]  # originalen er uendret


def test_hent_fra_gir_kopien_en_side_uten_gruppelistens_navn(con, kid, admin):
    fil_id, _ = sidelager.lagre_fil(con, kid, "a.pdf", pdf(), "admin:test")
    skriv_side(con, kid, dokument({"id": "b_tabell01", "type": "tabell", "tittel": "Grupper", "data": {"kolonner": ["Gruppe"], "rader": [["Kari N."]]}}))
    kopi = lag_kurs(con, "KOPI", start=IDAG + timedelta(days=400))
    _lagre(admin, kopi, dokument(tekstblokk("Ny side")))
    r = json_post(admin, _u(kopi, "/hent-fra"), {"versjon": 1, "fra_kurs_id": kid, "blokk_ider": ["b_tabell01"]})
    assert r.status_code == 200 and r.get_json()["dokument"]["blokker"][1]["data"]["rader"] == []
    assert fil_id and "Kari" not in json.dumps(sidelager.hent_utkast(con, kopi)[0])


def test_overgang_til_avsluttet_kurs_gjor_ingenting_med_siden_som_stenges_foerst_etter_180_dager(con, kid, admin):
    skriv_side(con, kid, dokument(tekstblokk("Ferdig kurs")))
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()
    assert admin.get(_u(kid)).status_code == 200 and sidelager.hent(con, kid)["aktiv"] == 1
    assert _lagre(admin, kid, dokument(tekstblokk("Kan fortsatt redigeres")), 1).status_code == 200        # også etter kurset
    assert json_post(admin, _u(kid, "/publiser"), {"versjon": 2}).status_code == 200
    assert sidelager.effektiv_stenges(con, kid, sidelager.hent(con, kid)) == sidelager.siste_kursdag(con, kid) + timedelta(days=180)


def test_sletting_av_kurs_i_databasen_fjerner_siden_og_filene(con, admin):
    ny = lag_kurs(con, "SLETTES")
    d = _fil(admin, ny, "a.pdf", pdf()).get_json()["fil"]
    _lagre(admin, ny, dokument({"id": "b_filer001", "type": "filer", "tittel": "F", "data": {"filer": [{"fil_id": d["id"], "tittel": "A"}]}}))
    json_post(admin, _u(ny, "/publiser"), {"versjon": 1})
    con.execute("DELETE FROM kurs WHERE id=?", (ny,))
    con.commit()
    for tabell in ("kursside", "kursside_versjon", "kursside_fil"):
        assert con.execute(f"SELECT COUNT(*) FROM {tabell} WHERE kurs_id=?", (ny,)).fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM kursside_fil_innhold").fetchone()[0] == 0
    assert admin.get(_u(ny)).status_code == 404


# ============================ statiske kilder ============================

STATIC = Path(__file__).resolve().parent.parent / "kurs" / "web" / "static"
JS_FILER = sorted(STATIC.glob("kursside-admin*.js"))          # redigeringsvisningen er delt i flere filer (kjernen, listen, blokkene, filene, dialogene)


def test_javascript_filene_bruker_ingen_farlige_api_er():
    assert len(JS_FILER) >= 5, JS_FILER
    for fil in JS_FILER:
        kilde = fil.read_text(encoding="utf-8")
        for forbudt in (r"\.innerHTML\s*=", r"outerHTML", r"insertAdjacentHTML", r"document\.write", r"\beval\s*\(", r"new\s+Function", r"setTimeout\s*\(\s*[\"']"):
            assert not re.search(forbudt, kilde), (fil.name, forbudt)


def test_malen_har_nonce_paa_skript_ingen_inline_handlere_og_alle_felt_har_ledetekst(con, kid, admin):
    html = admin.get(_u(kid)).get_data(as_text=True)
    for skript in re.findall(r"<script\b[^>]*>", html):
        assert "nonce=" in skript
    assert not re.search(r"\son(click|change|submit|load|input|keyup)=", html) and "javascript:" not in html.lower()
    for felt in re.finditer(r"<(input|select|textarea)\b([^>]*)>", html):
        attrs = felt.group(2)
        if 'type="hidden"' in attrs:
            continue
        ident = re.search(r'\bid="([^"]+)"', attrs)
        foran = html[:felt.start()]
        assert ("aria-label=" in attrs or (ident and re.search(r'<label\b[^>]*\bfor="%s"' % re.escape(ident.group(1)), html))
                or foran.rfind("<label") > foran.rfind("</label>")), felt.group(0)
