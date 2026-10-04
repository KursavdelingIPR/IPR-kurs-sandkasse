"""Rettinger etter kritikernes gjennomgang av kursside-arbeidet (funn 1-40). Hver test bærer funnnummeret i kommentaren over seg.
Bare oppdiktede data.

Sikkerhet:  (1) stdlib-parseren brukte kvadratisk tid på uferdige tagger, (3) /dokument godtok utløpt admin-økt, (4) nødbrems stoppet ikke SharePoint-materiellet,
            (5) trygg_neste slapp gjennom linjeskift, (7) uhåndterte unntak (500), (8) ingen takgrense på filnedlasting, (2/12/13) innsjekk på e-post alene.
Utseende og bruk: (12) overskrifter fra Word, (10) filer uten dag, (14) toppbilde bak tittelen, (18) programdager uten dato, (21) «Kursside ↗», (22) kontrast, (23) 15 MB.
Testhull (24-35): mutantene fra kritikerne (gårsdagens QR, opprydding av nye filer, escaping i malen, dag n av m, har_side, data:-adresser, sniffet bildetype,
            tom kode, fornavn til uinnlogget, avmeldte på Min side, grenser og visningsdetaljer).
Rester av tidligere krav: (36/38) «Spør oss» i e-postmalene, (39) gamle «Min side»-ord, (37/40) Min side sortert etter kursdag i dag.
"""
import json
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kurs import config, db, deltakerside, hendelseslogg, maltekster, signaturer
from kurs import sideinnhold as si
from kurs import sidelager
from kurs.integrasjoner import epost, sharepoint
from kurs.web import sikkerhet

from kurssidehjelp import (IDAG, admin_klient, csrf, dokument, deltaker_klient, fast_dato, json_post, lag_deltaker, lag_kurs, ny_database, pdf, png,
                           skriv_side, tekstblokk)

STATIC = Path(__file__).resolve().parent.parent / "kurs" / "web" / "static"
TEMPLATES = Path(__file__).resolve().parent.parent / "kurs" / "web" / "templates"


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    fast_dato(monkeypatch)
    monkeypatch.setattr(sharepoint, "DEMO_ROT", tmp_path / "sharepoint_demo")
    deltakerside.nullstill_sp_cache()
    yield c
    c.close()


@pytest.fixture
def kid(con):
    return lag_kurs(con, "K1")


def _u(kid, hale=""):
    return f"/admin/kurs/{kid}/kursside{hale}"


def _klient():
    from kurs.web import app as webapp
    return webapp.app.test_client()


def _side_html(con, kid, did) -> str:
    r = deltaker_klient(did).get("/kurs/K1/deltakerside")
    assert r.status_code == 200
    return r.get_data(as_text=True)


# ============================================================ (1) patologisk HTML stanser ikke tjenesten ============================================================

PATOLOGISKE = ["<a/", "<a ", "<a b='", '<a b="', "<a b=", '<a href="', "<!--", "<![CDATA[", "<?", "<!DOCTYPE ", "</a", "</", "<", "<a b='>", '<a b=">',
               "<a/<b ", "<a <b/", "<p<p<p", "<a\t", "<a\n", "&#", "&#x", "&amp"]


@pytest.mark.parametrize("enhet", PATOLOGISKE)
def test_1_uferdige_tagger_paa_maks_lengde_renses_raskt(enhet):
    """Målt før rettingen: «<a/» × 6 600 (19 800 tegn) tok 32 s i én forespørsel, og 80 000 tegn i størrelsesorden 20 minutter."""
    tekst = (enhet * (si.MAKS_HTML_RA // len(enhet) + 1))[:si.MAKS_HTML_RA]
    t0 = time.perf_counter()
    try:
        si.rens_html(tekst)
    except si.Sidefeil:
        pass                                          # for lang tekst ut er en vanlig, rask avvisning
    assert time.perf_counter() - t0 < 5, enhet


def test_1_tilfeldige_blandinger_av_markup_renses_raskt_og_er_idempotente():
    deler = ["<a href=\"", "<a href='", "<p>", "</p>", "<!--", "-->", "<![CDATA[", "]]>", "<script>", "</script>", "<style>", "&#", "&amp", ";", "<a b='", '<a b="',
             "<b>", "</b>", "<a ", "<a/", "</", "<", ">", '"', "'", " ", "tekst ", "<br>", "<ul>", "<li>", "</li>", "<em>", "<?", "<!", "=", "/", "<svg>", "\x00", "&nbsp;"]
    rng = random.Random(4)
    for _ in range(120):
        tekst = "".join(rng.choice(deler) for _ in range(rng.choice([30, 400, 6000, 20000])))[:si.MAKS_HTML_RA]
        t0 = time.perf_counter()
        try:
            ren = si.rens_html(tekst)
        except si.Sidefeil:
            continue
        assert time.perf_counter() - t0 < 5
        assert si.rens_html(ren) == ren, tekst[:120]


def test_1_lagre_med_patologisk_tekst_gir_svar_paa_sekunder(con, kid):
    """«<a » × 4 000 tok 7 s før rettingen (og «<a/» × 6 600 32 s). Teksten blir for lang ut (422), men avvisningen må komme raskt."""
    admin = admin_klient()
    t0 = time.perf_counter()
    r = json_post(admin, _u(kid, "/lagre"), {"versjon": 0, "dokument": dokument(tekstblokk("T", "<a " * 4000))})
    assert time.perf_counter() - t0 < 3 and r.status_code in (200, 422)
    t0 = time.perf_counter()
    r = json_post(admin, _u(kid, "/lagre"), {"versjon": 0, "dokument": dokument(tekstblokk("T", "<a/" * 2500))})       # 15 000 tegn ut: lagres
    assert time.perf_counter() - t0 < 3 and r.status_code == 200
    assert sidelager.hent_utkast(con, kid)[0]["blokker"][0]["data"]["html"].startswith("<p>")


@pytest.mark.parametrize("inn, ut", [
    ("a<!-- kommentar -->b", "<p>ab</p>"),
    ("<p>x</p><!DOCTYPE html><p>y</p>", "<p>x</p><p>y</p>"),
    ("<p>1 < 2 og 3 > 2</p>", "<p>1 &lt; 2 og 3 &gt; 2</p>"),
    ("<p>uferdig <a href=\"https://x", "<p>uferdig</p>"),
    ("<p>a</p><!-- åpen", "<p>a</p>"),                                     # en uferdig tagg til slutt ignoreres (som før)
    ("<p>a</p><!-- åpen<p>b</p>", "<p>a</p><p>&lt;!-- åpen</p><p>b</p>"),
    ("<p>ok</p><a href=\"https://ipr.no/a>b\">lenke</a>", '<p>ok</p><p><a href="https://ipr.no/a&gt;b">lenke</a></p>'),
])
def test_1_vanlig_innhold_gir_samme_resultat_som_for(inn, ut):
    assert si.rens_html(inn) == ut


def test_1_forberedelsen_gir_parseren_bare_hele_tagger_og_fjerner_kommentarer():
    ut = si._forbered_html("<p>a</p><a b='<i>x</i>'>t</a> < b <!-- c --><![CDATA[x]]><?php ?><a href=\"u\"")
    assert "<!--" not in ut and "CDATA" not in ut and "<?php" not in ut
    assert ut.startswith("<p>a</p>") and "&lt;" in ut


# ============================================================ (7) uhåndterte unntak ============================================================

def test_7_enslige_surrogater_i_tekst_gir_ikke_unntak_og_fjernes():
    felter = {"tittel": "a\ud800b", "ingress": "\udfff", "rom": "x\ud83dy", "melding": {"tekst": "hei\ud800", "niva": "info"}}
    blokker = [
        {"id": "", "type": "tekst", "tittel": "T\ud800", "data": {"html": "<p>hei\ud800 <b>\udc00</b></p>"}},
        {"id": "", "type": "viktig", "tittel": "V", "data": {"niva": "info", "tekst": "v\ud800"}},
        {"id": "", "type": "kontakt", "tittel": "K", "data": {"personer": [{"navn": "N\ud800", "rolle": "R\ud800", "telefon": "22 33 44 55", "epost": "a@b.no"}]}},
        {"id": "", "type": "lenker", "tittel": "L", "data": {"lenker": [{"tittel": "\ud800", "url": "https://a.no/\ud800", "tekst": "\ud800"}]}},
        {"id": "", "type": "tabell", "tittel": "Ta", "data": {"kolonner": ["\ud800"], "rader": [["\ud800"]]}},
        {"id": "", "type": "program", "tittel": "P", "data": {"dager": [{"tittel": "\ud800", "punkter": [{"fra": "09:00", "til": "10:00", "tema": "\ud800", "sted": "\ud800", "hvem": "\ud800"}]}]}},
    ]
    dok, _m = si.valider(dokument(*blokker, **felter), fil_ider={}, kursdag_ider=set())
    tekst = si.til_json(dok)
    tekst.encode("utf-8")                                   # kastet UnicodeEncodeError før rettingen
    assert "\ud800" not in tekst and "\udfff" not in tekst and dok["tittel"] == "ab"


def test_7_surrogat_i_lagre_gir_200_ikke_500(con, kid):
    admin = admin_klient()
    rå = '{"versjon":0,"dokument":{"v":1,"tittel":"a\\ud800b","ingress":"","rom":"","melding":{"tekst":"x\\ud800","niva":"info"},"blokker":[]}}'
    r = admin.post(_u(kid, "/lagre"), data=rå.encode("utf-8"), content_type="application/json", headers={"X-CSRF-Token": csrf(admin)})
    assert r.status_code == 200 and sidelager.hent_utkast(con, kid)[0]["tittel"] == "ab"


def test_7_siste_sikkerhetsnett_i_valider_gir_sidefeil_ikke_unntak(monkeypatch):
    monkeypatch.setattr(si, "_ENSLIGE_SURROGATER", re.compile("(?!)"))          # som om noe slapp forbi rensingen
    monkeypatch.setattr(si, "_KONTROLLTEGN", re.compile(r"[\x00-\x08]"))
    with pytest.raises(si.Sidefeil, match="tegn som ikke kan lagres"):
        si.valider(dokument(tittel="a\ud800"), fil_ider={}, kursdag_ider=set())


def test_7_dypt_nestet_json_gir_400_ikke_500(con, kid):
    admin = admin_klient()
    for url in (_u(kid, "/lagre"), _u(kid, "/publiser"), _u(kid, "/hent-fra"), _u(kid, "/mal")):
        r = admin.post(url, data=("[" * 100_000 + "]" * 100_000).encode(), content_type="application/json", headers={"X-CSRF-Token": csrf(admin)})
        assert r.status_code == 400, url


@pytest.mark.parametrize("hale, kropp", [
    ("/lagre", {"versjon": 2 ** 63, "dokument": {"v": 1, "blokker": []}}),
    ("/lagre", {"versjon": 2 ** 100, "dokument": {"v": 1, "blokker": []}}),
    ("/publiser", {"versjon": 2 ** 63}),
    ("/hent-fra", {"versjon": 1, "fra_kurs_id": 2 ** 63, "blokk_ider": ["b_x"]}),
    ("/hent-fra", {"versjon": 2 ** 63, "fra_kurs_id": 2, "blokk_ider": ["b_x"]}),
    ("/mal", {"versjon": 2 ** 63, "mal": "standard"}),
])
def test_7_enorme_tall_i_forespoerselen_gir_400_ikke_500(con, kid, hale, kropp):
    r = json_post(admin_klient(), _u(kid, hale), kropp)
    assert r.status_code in (400, 409), (hale, r.status_code)                   # 409 = «ingen side ennå»: også kontrollert, aldri unntak


@pytest.mark.parametrize("adresse", [
    "/admin/kurs/99999999999999999999/kursside", "/admin/kurs/1/kursside/fil/99999999999999999999", "/admin/kurs/1/kursside/fil/9223372036854775808",
    "/admin/kurs/1/kursside/versjon/99999999999999999999/gjenopprett"])
def test_7_enorme_tall_i_adressen_gir_404(con, kid, adresse):
    admin = admin_klient()
    r = admin.get(adresse) if "gjenopprett" not in adresse else json_post(admin, adresse, {"versjon": 1})
    assert r.status_code == 404


def test_7_heltall_i_dokumentet_over_grensen_blir_ikke_en_id():
    assert si._heltall(2 ** 40) is None and si._heltall(2 ** 31 - 1) == 2 ** 31 - 1 and si._heltall(-1) is None and si._heltall("99999999999") is None
    dok, m = si.valider(dokument({"id": "", "type": "filer", "tittel": "F", "data": {"filer": [{"fil_id": 2 ** 70, "tittel": "x"}]}}),
                        fil_ider={1: {"type": "dokument"}}, kursdag_ider=set())
    assert dok["blokker"][0]["data"]["filer"] == [] and m


@pytest.mark.parametrize("dag", ["²", "%C2%B2", "١", "9" * 5000, "-1", "1e3", "٣"])
def test_7_forhandsvis_tal_med_rare_sifre_i_dag_gir_200(con, kid, dag):
    admin = admin_klient()
    json_post(admin, _u(kid, "/lagre"), {"versjon": 0, "dokument": dokument(tekstblokk())})
    r = admin.get(_u(kid, f"/forhandsvis?dag={dag}"))
    assert r.status_code == 200


def test_7_forhandsvis_med_en_ekte_kursdag_virker_fortsatt(con, kid):
    admin = admin_klient()
    json_post(admin, _u(kid, "/lagre"), {"versjon": 0, "dokument": dokument(tekstblokk())})
    dag2 = db.kursdager(con, kid)[1]["id"]
    assert admin.get(_u(kid, f"/forhandsvis?dag={dag2}")).status_code == 200


def test_7_lagret_json_endret_utenom_siden_gir_ikke_500_paa_deltakersiden(con, kid):
    """Feil datatyper (dager som objekt, rader som tall ...) og enslige surrogater lagt rett i databasen ga 500 i ca. 15 % av 400 fuzz-forsøk."""
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    skriv_side(con, kid, dokument(tekstblokk("Velkommen", "<p>Hei</p>")))
    odelagt = {"v": 1, "tittel": "Side\ud800", "ingress": "", "rom": "", "melding": {"tekst": "", "niva": "info"}, "blokker": [
        {"id": "b_tekst001", "type": "tekst", "tittel": "T", "data": {"html": "<p>Ok\ud800 tekst</p>"}},
        {"id": "b_progr001", "type": "program", "tittel": "P", "data": {"dager": {"a": 1}}},
        {"id": "b_progr002", "type": "program", "tittel": "P2", "data": {"dager": [7, None, "x"]}},
        {"id": "b_tabell01", "type": "tabell", "tittel": "T", "data": {"kolonner": ["a"], "rader": 5}},
        {"id": "b_tabell02", "type": "tabell", "tittel": "T", "data": {"kolonner": ["a"], "rader": [None, 3, ["x"]]}},
        {"id": "b_kontakt1", "type": "kontakt", "tittel": "K", "data": {"personer": [1, {"navn": ["x"]}]}},
        {"id": "b_lenke001", "type": "lenker", "tittel": "L", "data": {"lenker": [None, 4]}},
        {"id": "b_viktig01", "type": "viktig", "tittel": "V", "data": {"tekst": 5}},
        {"id": "b_filer001", "type": "filer", "tittel": "F", "data": {"filer": [9, {"fil_id": "x"}], "sharepoint": "ja"}}]}
    ren = json.dumps(odelagt, ensure_ascii=True)                       # «\ud800» skrives som escape i JSON-teksten, slik en manuell endring i databasen ville gjort
    con.execute("UPDATE kursside SET publisert=? WHERE kurs_id=?", (ren, kid))
    con.commit()
    r = deltaker_klient(did).get("/kurs/K1/deltakerside")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Ok tekst" in html and "Side" in html                     # det som er gyldig vises, resten hoppes over
    admin = admin_klient()
    assert admin.get(_u(kid, "/forhandsvis?versjon=publisert")).status_code == 200


def test_7_les_fjerner_enslige_surrogater_men_beholder_ekte_emoji():
    dok = si.les(json.dumps({"v": 1, "tittel": "a\ud800b\ud83d\ude00c", "blokker": []}, ensure_ascii=True))
    assert dok["tittel"] == "ab\U0001F600c"
    assert si.les('{"v":1,"tittel":"x\\udfffy","blokker":[]}')["tittel"] == "xy"


@pytest.mark.parametrize("blokk", [
    {"type": "program", "data": {"dager": {"a": 1}}}, {"type": "program", "data": {"dager": [None]}}, {"type": "tabell", "data": {"rader": 5}},
    {"type": "tabell", "data": {"rader": [3]}}, {"type": "kontakt", "data": {"personer": [1]}}, {"type": "lenker", "data": {"lenker": [None]}},
    {"type": "viktig", "data": {"tekst": 5}}, {"type": "tekst", "data": {"html": 5}}, {"type": "filer", "data": {"filer": 3, "sharepoint": None}}])
def test_7_blokk_med_feil_datatyper_regnes_som_tom_og_kaster_aldri(blokk):
    assert si.blokk_er_tom(blokk) in (True, False)


def test_7_blokk_er_tom_gir_uendret_svar_for_gode_blokker():
    assert si.blokk_er_tom({"type": "tekst", "data": {"html": "<p>x</p>"}}) is False
    assert si.blokk_er_tom({"type": "tekst", "data": {"html": ""}}) is True
    assert si.blokk_er_tom({"type": "tabell", "data": {"rader": [["a"]]}}) is False and si.blokk_er_tom({"type": "tabell", "data": {"rader": []}}) is True



# ============================================================ (5) trygg_neste ============================================================

@pytest.mark.parametrize("verdi", ["/a\r\nSet-Cookie: x=1", "/\n//evil.example", "/a\tb", "/\r//evil.example", "/a\x00b", "/a\x1fb", "/a\x7fb", "/a\x85b", "\n/x"])
def test_5_kontrolltegn_i_neste_avvises(verdi):
    assert sikkerhet.trygg_neste(verdi, "/standard") == "/standard"


@pytest.mark.parametrize("verdi", ["/kurs/K1/deltakerside", "/innsjekk/abc_DEF-123", "/materiell/3/Dag 1 – Grunnmodell.pdf", "/logg-inn?x=1&y=2", "/min-side#mine-kurs"])
def test_5_vanlige_neste_verdier_virker_fortsatt(verdi):
    assert sikkerhet.trygg_neste(verdi, "/standard") == verdi


@pytest.mark.parametrize("neste", ["/a%0d%0aSet-Cookie:%20x=1", "/%0a//evil.example", "/%09/x"])
def test_5_logg_inn_med_linjeskift_i_neste_gir_ikke_500_og_ingen_ekstra_header(con, kid, neste):
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    k = deltaker_klient(did)
    r = k.get(f"/logg-inn?neste={neste}")
    assert r.status_code == 302 and "evil" not in r.headers["Location"] and "Set-Cookie: x=1" not in str(r.headers)
    assert r.headers["Location"].startswith("/")


# ============================================================ (3) /dokument og admin-økten ============================================================

def _dokument_rad(con, kid, tittel="Kursbevis A"):
    dok = db.sett_inn(con, "INSERT INTO dokument (kurs_id, deltaker_id, type, tittel, url) VALUES (?,?,?,?,?)", (kid, None, "kursbevis", tittel, "db:"))
    con.execute("INSERT INTO dokument_innhold (dokument_id, mimetype, innhold) VALUES (?,?,?)", (dok, "text/html", b"<p>bevis</p>"))
    con.commit()
    return dok


def test_3_dokument_krever_gyldig_admin_okt_deaktivert_bruker_slipper_ikke_inn(con, kid):
    dok = _dokument_rad(con, kid)
    admin = admin_klient()
    assert admin.get(f"/dokument/{dok}").status_code == 200
    con.execute("UPDATE admin_bruker SET aktiv=0")
    con.commit()
    assert admin.get(f"/dokument/{dok}").status_code == 403
    assert admin.get("/admin").status_code == 302                       # samme økt er ugyldig også for resten av admin


def test_3_dokument_avviser_utloept_admin_okt(con, kid):
    dok = _dokument_rad(con, kid)
    admin = admin_klient()
    assert admin.get(f"/dokument/{dok}").status_code == 200
    with admin.session_transaction() as s:
        s["admin_sist"] = time.time() - 10 * 3600                       # inaktiv i 10 timer (grensen er 90 minutter)
    assert admin.get(f"/dokument/{dok}").status_code == 403
    assert admin.get("/admin").status_code == 302


def test_3_dokument_er_fortsatt_apent_for_deltakeren_selv_og_stengt_for_fremmede(con, kid):
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    dok = db.sett_inn(con, "INSERT INTO dokument (kurs_id, deltaker_id, type, tittel, url) VALUES (?,?,?,?,?)", (kid, did, "kursbevis", "Mitt", "db:"))
    con.execute("INSERT INTO dokument_innhold (dokument_id, mimetype, innhold) VALUES (?,?,?)", (dok, "text/html", b"<p>bevis</p>"))
    con.commit()
    assert deltaker_klient(did).get(f"/dokument/{dok}").status_code == 200
    annen, _ = lag_deltaker(con, kid, "ola@example.no", fornavn="Ola")
    assert deltaker_klient(annen).get(f"/dokument/{dok}").status_code == 403
    assert _klient().get(f"/dokument/{dok}").status_code == 403


# ============================================================ (4) nødbrems og stenging stopper også SharePoint-materiellet ============================================================

def _sp_fil(tmp_path, kode="K1", navn="slides.pdf"):
    mappe = tmp_path / "sharepoint_demo" / "Kurs" / kode / "Presentasjoner"
    mappe.mkdir(parents=True, exist_ok=True)
    (mappe / navn).write_bytes(b"demo")
    return navn


def test_4_sharepoint_materiell_er_apent_uten_publisert_side_som_for(con, kid, tmp_path):
    navn = _sp_fil(tmp_path)
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    k = deltaker_klient(did)
    assert navn in k.get("/min-side").get_data(as_text=True)
    assert k.get(f"/materiell/{kid}/{navn}").status_code == 200


def test_4_naadbrems_stopper_sharepoint_i_min_side_og_i_materiell(con, kid, tmp_path):
    navn = _sp_fil(tmp_path)
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    skriv_side(con, kid, dokument(tekstblokk()))
    k = deltaker_klient(did)
    assert k.get(f"/materiell/{kid}/{navn}").status_code == 200                               # siden er åpen: materiellet er åpent
    assert sidelager.sett_aktiv(con, kid, False, "admin:test")
    con.commit()
    assert k.get("/kurs/K1/deltakerside").status_code == 404
    assert navn not in k.get("/min-side").get_data(as_text=True)
    assert k.get(f"/materiell/{kid}/{navn}").status_code == 404
    assert sidelager.sett_aktiv(con, kid, True, "admin:test")
    con.commit()
    assert k.get(f"/materiell/{kid}/{navn}").status_code == 200                               # slås siden på igjen, er alt tilbake


def test_4_stengt_side_stopper_sharepoint_men_ikke_naar_datoen_ikke_er_naadd(con, kid, tmp_path):
    navn = _sp_fil(tmp_path)
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    skriv_side(con, kid, dokument(tekstblokk()))
    k = deltaker_klient(did)
    sidelager.sett_innstillinger(con, kid, stenges=(IDAG + timedelta(days=1)).isoformat(), aktor="admin:test")
    con.commit()
    assert k.get(f"/materiell/{kid}/{navn}").status_code == 200
    sidelager.sett_innstillinger(con, kid, stenges="2000-01-01", aktor="admin:test")
    con.commit()
    assert k.get(f"/materiell/{kid}/{navn}").status_code == 404
    assert navn not in k.get("/min-side").get_data(as_text=True)


def test_4_materiell_apent_i_logikken(con, kid):
    assert deltakerside.materiell_apent(con, kid, IDAG) is True                               # ingen side
    skriv_side(con, kid, dokument(tekstblokk()), publiser=False)
    assert deltakerside.materiell_apent(con, kid, IDAG) is True                               # ikke publisert
    skriv_side(con, kid, dokument(tekstblokk()))
    assert deltakerside.materiell_apent(con, kid, IDAG) is True
    sidelager.sett_aktiv(con, kid, False, "admin:test")
    assert deltakerside.materiell_apent(con, kid, IDAG) is False


# ============================================================ (8) takgrense på filnedlasting ============================================================

def _side_med_filer(con, kid):
    pdf_id, _ = sidelager.lagre_fil(con, kid, "a.pdf", pdf(b"x" * 500), "admin:test")
    bilde_id, _ = sidelager.lagre_fil(con, kid, "a.png", png(), "admin:test")
    skriv_side(con, kid, dokument(
        {"id": "", "type": "filer", "tittel": "F", "data": {"filer": [{"fil_id": pdf_id, "tittel": "A"}]}},
        {"id": "", "type": "bilde", "tittel": "B", "data": {"fil_id": bilde_id, "alt": "Bilde", "tekst": "", "plassering": "bred"}}))
    return pdf_id, bilde_id


def test_8_nedlasting_av_dokument_er_takbegrenset_per_deltaker(con, kid, monkeypatch):
    monkeypatch.setitem(sikkerhet.GRENSER, "kursside_fil", (3, 600))
    pdf_id, _bilde = _side_med_filer(con, kid)
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    annen, _ = lag_deltaker(con, kid, "ola@example.no", fornavn="Ola")
    k = deltaker_klient(did)
    koder = [k.get(f"/kurs/K1/deltakerside/fil/{pdf_id}").status_code for _ in range(5)]
    assert koder == [200, 200, 200, 429, 429]
    assert deltaker_klient(annen).get(f"/kurs/K1/deltakerside/fil/{pdf_id}").status_code == 200        # grensen er per deltaker


def test_8_bilder_har_egen_grense_og_stoppes_ikke_av_dokumentgrensen(con, kid, monkeypatch):
    monkeypatch.setitem(sikkerhet.GRENSER, "kursside_fil", (1, 600))
    monkeypatch.setitem(sikkerhet.GRENSER, "kursside_bilde", (4, 600))
    pdf_id, bilde_id = _side_med_filer(con, kid)
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    k = deltaker_klient(did)
    assert [k.get(f"/kurs/K1/deltakerside/fil/{bilde_id}").status_code for _ in range(5)] == [200, 200, 200, 200, 429]
    assert k.get(f"/kurs/K1/deltakerside/fil/{pdf_id}").status_code == 200


def test_8_avvisning_koster_ingen_kvote_og_standardgrensene_finnes():
    assert sikkerhet.GRENSER["kursside_fil"][0] >= 30 and sikkerhet.GRENSER["kursside_bilde"][0] > sikkerhet.GRENSER["kursside_fil"][0]


def test_8_filer_utenfor_siden_gir_404_uten_aa_bruke_kvote(con, kid, monkeypatch):
    monkeypatch.setitem(sikkerhet.GRENSER, "kursside_fil", (1, 600))
    pdf_id, _b = _side_med_filer(con, kid)
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    k = deltaker_klient(did)
    for _ in range(3):
        assert k.get("/kurs/K1/deltakerside/fil/999999").status_code == 404
    assert k.get(f"/kurs/K1/deltakerside/fil/{pdf_id}").status_code == 200


# ============================================================ (10) filer uten dag ============================================================

def _filblokk(con, kid, filer_med_gruppe):
    dager = [d["id"] for d in db.kursdager(con, kid)]
    ider = []
    for k, (tittel, dag_nr) in enumerate(filer_med_gruppe):
        fid, _ = sidelager.lagre_fil(con, kid, f"f{k}.pdf", pdf(bytes([70 + k]) * 100), "admin:test")
        ider.append({"fil_id": fid, "tittel": tittel, "gruppe": dager[dag_nr] if dag_nr is not None else None})
    skriv_side(con, kid, dokument({"id": "b_filer001", "type": "filer", "tittel": "F", "data": {"filer": ider}}))


def test_10_filer_uten_dag_far_egen_overskrift_naar_andre_har_dag(con, kid):
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    _filblokk(con, kid, [("Uten dag 1", None), ("Dag 1-fil", 0), ("Dag 2-fil", 1), ("Uten dag 2", None)])
    html = _side_html(con, kid, did)
    overskrifter = re.findall(r'<h3 class="dp-gruppe">([^<]*)</h3>', html)
    assert overskrifter == [f"Dag 1 · {deltakerside.dato_lang(IDAG)}", f"Dag 2 · {deltakerside.dato_lang(IDAG + timedelta(days=1))}", "Øvrige filer"]
    assert html.index("Uten dag 1") > html.index("Dag 2-fil") and html.index("Øvrige filer") < html.index("Uten dag 1") < html.index("Uten dag 2")


def test_10_ingen_overskrift_naar_ingen_filer_har_dag(con, kid):
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    _filblokk(con, kid, [("A", None), ("B", None)])
    html = _side_html(con, kid, did)
    assert "Øvrige filer" not in html and 'class="dp-gruppe"' not in html


# ============================================================ (12) overskrifter fra Word ============================================================

@pytest.mark.parametrize("inn, ut", [
    ("<h1>Kursets mål</h1><p>Tekst</p>", "<p><strong>Kursets mål</strong></p><p>Tekst</p>"),
    ("<h2><b>Fet</b> og vanlig</h2><p>x <b>fet</b> y</p>", "<p><strong>Fet og vanlig</strong></p><p>x <strong>fet</strong> y</p>"),
    ("<h2>Tittel<p>tekst <b>fet</b> mer</p>", "<p><strong>Tittel</strong></p><p>tekst <strong>fet</strong> mer</p>"),
    ("<h2>Læringsmål</h2><ul><li>a</li></ul>", "<p><strong>Læringsmål</strong></p><ul><li>a</li></ul>"),
    ("<h3>Praktisk</h3>", "<p><strong>Praktisk</strong></p>"),
    ("<h4>a</h4><h5>b</h5><h6>c</h6>", "<p><strong>a</strong></p><p><strong>b</strong></p><p><strong>c</strong></p>"),
    ("<h2><b>Allerede fet</b> og vanlig</h2>", "<p><strong>Allerede fet og vanlig</strong></p>"),
    ("<H1>Store bokstaver</H1>", "<p><strong>Store bokstaver</strong></p>"),
    ("<h2></h2><p>x</p>", "<p>x</p>"),
    ("<h2> </h2><p>x</p>", "<p>x</p>"),
    ("<p>før</p><h2>Midt</h2><p>etter</p>", "<p>før</p><p><strong>Midt</strong></p><p>etter</p>"),
    ("<ul><li><h2>I liste</h2></li></ul>", "<ul><li><strong>I liste</strong></li></ul>"),
    ("<h2 onclick=alert(1) style='x'>Ren</h2>", "<p><strong>Ren</strong></p>"),
    ("<h2>Med <a href=\"https://ipr.no\">lenke</a></h2>", '<p><strong>Med <a href="https://ipr.no">lenke</a></strong></p>'),
    ("<table><tr><td>A</td><td>B</td></tr></table>", "<p>A</p><p>B</p>"),
])
def test_12_overskrifter_blir_fet_tekst_og_tabeller_avsnitt(inn, ut):
    ren = si.rens_html(inn)
    assert ren == ut and si.rens_html(ren) == ren


def test_12_overskrifter_i_lagret_side_vises_fete_for_deltakerne(con, kid):
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    skriv_side(con, kid, dokument(tekstblokk("T", "<h2>Praktisk informasjon</h2><p>Ta med sko.</p>")))
    assert "<p><strong>Praktisk informasjon</strong></p><p>Ta med sko.</p>" in _side_html(con, kid, did)


def test_12_redigeringsvisningen_forklarer_hva_som_skjer_med_innlimte_overskrifter():
    kilde = (TEMPLATES / "_kursside_admin_maler.html").read_text(encoding="utf-8")
    assert "Overskrifter skriver du i tittelfeltet over" in kilde and "Innlimte overskrifter blir fet tekst" in kilde
    js = (STATIC / "kursside-admin-blokker.js").read_text(encoding="utf-8")
    assert "Overskrifter er gjort om til fet tekst" in js and "Tabeller er gjort om til vanlige avsnitt" in js
    assert re.search(r"H\[1-6\]\$/\.test\(t\) \? fet\(inl\(n\)\)", js)                     # klienten følger samme regel som serveren


# ============================================================ (6) innliming: klientrenseren avhenger ikke av CSP ============================================================

def test_6_innliming_kloner_aldri_noder_inn_i_den_levende_siden():
    """DOMParser-dokumentet har ingen nettleserkontekst (ingen bilder hentes, ingen hendelsesattributter kjører). Klonene må bli der:
    `document.createElement` i wrap/liste flyttet nodene inn i den levende siden, og et <img onerror> ble forsøkt kjørt (stoppet bare av CSP).
    Prøvd i Edge: ingen CSP-brudd og ingen forespørsler etter rettingen (se BYGG-notatene)."""
    js = re.sub(r"/\*.*?\*/", "", (STATIC / "kursside-admin-blokker.js").read_text(encoding="utf-8"), flags=re.S)         # uten kommentarer
    wrap = re.search(r"function wrap\(n\) \{[^\n]*\}", js).group(0)
    assert "n.ownerDocument.createElement" in wrap
    liste = js[js.index("function liste("):js.index("function blokker(")]
    assert "n.ownerDocument.createElement" in liste
    renser = js[js.index("function inl("):js.index("KS.renHtml = ")]
    levende = re.compile(r"(?<![\w.])document\.createElement")                                    # kun den levende siden, aldri nodens eget dokument
    assert not levende.search(renser) and not levende.search(liste)


# ============================================================ (14) toppbilde bak tittelen ============================================================

def test_14_toppbildet_ligger_i_toppfeltet_bak_tittelen(con, kid):
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    fil_id, _ = sidelager.lagre_fil(con, kid, "topp.png", png(), "admin:test")
    skriv_side(con, kid, dokument({"id": "", "type": "bilde", "tittel": "Topp", "data": {"fil_id": fil_id, "alt": "Kursrommet", "tekst": "Foto: IPR", "plassering": "topp"}},
                                  tekstblokk()))
    html = _side_html(con, kid, did)
    topp = html.index('<header class="dp-hero dp-hero-bilde">')
    slutt = html.index("</header>", topp)
    assert topp < html.index('<figure class="dp-toppbilde">') < html.index("<h1") < slutt
    assert "<figcaption>Foto: IPR</figcaption>" in html[topp:slutt] and 'alt="Kursrommet"' in html[topp:slutt]
    assert html.count('class="dp-toppbilde"') == 1                                          # ikke også som eget bilde under toppfeltet


def test_14_uten_toppbilde_er_toppfeltet_som_for(con, kid):
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    skriv_side(con, kid, dokument(tekstblokk()))
    html = _side_html(con, kid, did)
    assert '<header class="dp-hero">' in html and "dp-toppbilde" not in html


def test_14_stilen_har_slor_som_gir_lesbar_tekst_ogsaa_paa_lyse_bilder():
    css = (STATIC / "kursside-delt.css").read_text(encoding="utf-8")
    slor = re.search(r"\.dp-hero-bilde::after \{[^}]*linear-gradient\(180deg, rgba\((\d+),(\d+),(\d+),\.(\d+)\) 0%", css)
    assert slor, "sløret over bildet mangler"
    r, g, b = (int(slor.group(i)) for i in (1, 2, 3))
    a = float("0." + slor.group(4))
    bakgrunn = [c * a + 255 * (1 - a) for c in (r, g, b)]                                    # verste tilfelle: et helt hvitt bilde
    assert _kontrast((255, 255, 255), bakgrunn) >= 4.5
    assert ".dp-hero-bilde > .dp-toppbilde { position:absolute; inset:0;" in css
    assert ".dp-hero > .dp-toppbilde" in css.split("@media print")[1]                        # bildet vises ikke på utskrift


# ============================================================ (18) programdager uten dato ============================================================

def _kontroll(dok, kursdager=()):
    return si.publiseringssjekk(dok, filer={}, bekreftede_epost=set(), kursdager=list(kursdager), idag=IDAG)


def test_18_publiseringskontrollen_advarer_om_programdager_uten_dato_og_kursdag():
    program = {"id": "b_progr001", "type": "program", "tittel": "Program", "data": {"dager": [
        {"kursdag_id": 5, "dato": "2027-03-10", "tittel": "Dag 1", "punkter": [{"tema": "x", "fra": "", "til": ""}]},
        {"kursdag_id": None, "dato": None, "tittel": "Dag 2", "punkter": [{"tema": "y", "fra": "", "til": ""}]},
        {"kursdag_id": None, "dato": None, "tittel": "Dag 3", "punkter": []}]}}
    _feil, adv = _kontroll(dokument(program), [{"id": 5, "dato": "2027-03-10"}])
    tekster = [a["tekst"] for a in adv]
    assert any("«Dag 2», «Dag 3»" in t and "ingen dato" in t for t in tekster) and not any("«Dag 1»" in t for t in tekster)
    assert adv[0]["blokk_id"] == "b_progr001"


def test_18_en_udatert_dag_bruker_entall_og_dager_med_dato_gir_ingen_advarsel():
    dag = lambda n, **kw: {"kursdag_id": None, "dato": None, "tittel": n, "punkter": [], **kw}   # noqa: E731
    _f, adv = _kontroll(dokument({"id": "b_progr001", "type": "program", "tittel": "P", "data": {"dager": [dag("Dag 1", dato="2027-03-10"), dag("Dag 2")]}}))
    assert any("Programdagen «Dag 2» i «P» har ingen dato" in a["tekst"] for a in adv)
    _f, adv = _kontroll(dokument({"id": "b_progr001", "type": "program", "tittel": "P", "data": {"dager": [dag("Dag 1", dato="2027-03-10")]}}))
    assert not any("ingen dato" in a["tekst"] for a in adv)


def test_18_hent_fra_et_lengre_kurs_gir_advarsel_ved_publisering(con, kid):
    admin = admin_klient()
    kilde = lag_kurs(con, "K3D", dager=3)
    program = {"id": "b_progr001", "type": "program", "tittel": "Program", "data": {"dager": [
        {"kursdag_id": d["id"], "dato": d["dato"], "tittel": f"Dag {n}", "punkter": [{"fra": "09:00", "til": "10:00", "tema": "T", "sted": "", "hvem": "", "pause": False}]}
        for n, d in enumerate(db.kursdager(con, kilde), 1)]}}
    skriv_side(con, kilde, dokument(program))
    assert json_post(admin, _u(kid, "/lagre"), {"versjon": 0, "dokument": dokument(tekstblokk())}).status_code == 200
    r = json_post(admin, _u(kid, "/hent-fra"), {"versjon": 1, "fra_kurs_id": kilde, "blokk_ider": ["b_progr001"]})
    assert r.status_code == 200
    adv = admin.get(_u(kid, "/sjekk")).get_json()["advarsler"]
    assert any("har ingen dato" in a["tekst"] and "Dag 3" in a["tekst"] for a in adv)


# ============================================================ (21) «Kursside ↗» for kurs som aldri er publisert ============================================================

def test_21_lenken_til_ikke_publisert_side_sier_at_den_ikke_er_publisert(con, kid):
    admin = admin_klient()
    json_post(admin, _u(kid, "/lagre"), {"versjon": 0, "dokument": dokument(tekstblokk("Utkast", "<p>Bare utkast</p>"))})
    html = admin.get(_u(kid, "/forhandsvis?versjon=publisert")).get_data(as_text=True)
    assert "Bare utkast" in html and "Ikke publisert ennå: deltakerne ser «Min side er ikke åpnet ennå»" in html
    assert "Slik ser deltakerne siden nå" not in html


def test_21_nedtatt_og_stengt_side_forklares_i_forhandsvisningen(con, kid):
    admin = admin_klient()
    skriv_side(con, kid, dokument(tekstblokk("T", "<p>Innhold</p>")))
    assert "Slik ser deltakerne siden nå" in admin.get(_u(kid, "/forhandsvis?versjon=publisert")).get_data(as_text=True)
    sidelager.sett_aktiv(con, kid, False, "admin:test")
    con.commit()
    assert "Siden er tatt ned: deltakerne ser «Min side er midlertidig stengt»" in admin.get(_u(kid, "/forhandsvis?versjon=publisert")).get_data(as_text=True)
    sidelager.sett_aktiv(con, kid, True, "admin:test")
    sidelager.sett_innstillinger(con, kid, stenges="2000-01-01", aktor="admin:test")
    con.commit()
    assert "Siden er stengt: deltakerne ser «Min side er stengt»" in admin.get(_u(kid, "/forhandsvis?versjon=publisert")).get_data(as_text=True)


def test_21_teksten_deltakerne_far_er_den_samme_som_forhandsvisningen_lover(con, kid):
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    skriv_side(con, kid, dokument(tekstblokk()), publiser=False)
    r = deltaker_klient(did).get("/kurs/K1/deltakerside")
    assert r.status_code == 404 and deltakerside.TEKST_IKKE_PUBLISERT in r.get_data(as_text=True)


# ============================================================ (22) kontrast ============================================================

def _lum(rgb) -> float:
    def f(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (f(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _kontrast(a, b) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _hex(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _farger(kilde: str, velger: str) -> tuple[str, str]:
    """(bakgrunn, tekstfarge) fra regelen `velger { ... }` (bokstavelige #rrggbb)."""
    regel = re.search(re.escape(velger) + r"\s*\{([^}]*)\}", kilde).group(1)
    return re.search(r"background:(#[0-9a-fA-F]{6})", regel).group(1), re.search(r"(?<![-\w])color:(#[0-9a-fA-F]{6})", regel).group(1)


@pytest.mark.parametrize("fil, velger", [("kursside-delt.css", ".dp-filtype-xls"), ("kursside-admin.css", ".ks-fil .ft.xls"), ("kursside-admin.css", ".ks-filoversikt .ft.xls")])
def test_22_xls_merket_har_minst_4_5_til_1(fil, velger):
    bg, fg = _farger((STATIC / fil).read_text(encoding="utf-8"), velger)
    assert _kontrast(_hex(bg), _hex(fg)) >= 4.5, (fil, velger, bg, fg)


def test_22_demo_banneret_har_minst_4_5_til_1():
    kilde = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    bg, fg = _farger(kilde, ".demo-banner")
    assert _kontrast(_hex(bg), _hex(fg)) >= 4.5, (bg, fg)


def test_22_kontrastfunksjonen_stemmer_med_kjente_verdier():
    assert round(_kontrast(_hex("#2e7d4f"), _hex("#e5f3ea")), 2) == 4.41 and round(_kontrast((0, 0, 0), (255, 255, 255)), 1) == 21.0


# ============================================================ (23) 15 MB-grensen gir et råd som kan følges ============================================================

def test_23_for_stor_fil_gir_konkret_rad_ikke_del_filen_opp(con, kid, monkeypatch):
    tekst = sidelager.for_stor_tekst(15)
    assert "maks 15 MB" in tekst and "komprimere bildene" in tekst and "Komprimer bilder" in tekst and "SharePoint" in tekst and "Del " not in tekst
    monkeypatch.setattr(config, "KURSSIDE_FIL_MAKS_MB", 1)
    admin = admin_klient()
    r = admin.post(_u(kid, "/fil"), data=b"%PDF-1.4\n" + b"x" * (2 * 1024 * 1024), content_type="application/octet-stream",
                   headers={"X-CSRF-Token": csrf(admin), "X-Filnavn": "stor.pdf"})
    assert r.status_code == 413 and "komprimere bildene" in r.get_json()["melding"] and "maks 1 MB" in r.get_json()["melding"]


def test_23_klienten_gir_samme_rad_og_grensen_star_i_slippsonen():
    js = (STATIC / "kursside-admin-filer.js").read_text(encoding="utf-8")
    assert "Komprimer bilder" in js and "Del filen opp" not in js
    assert "opptil {{ data.grenser.fil_mb }} MB per fil" in (TEMPLATES / "_kursside_admin_maler.html").read_text(encoding="utf-8")


# ============================================================ (2/12/13) innsjekk på e-post alene ============================================================

def _innsjekk_oppsett(con):
    kid = lag_kurs(con, "K1", dager=2)
    did, pid = lag_deltaker(con, kid, "kari@example.no")
    return kid, did, pid, db.kursdager(con, kid)[0]


def _oppmote(con):
    return con.execute("SELECT COUNT(*) FROM oppmote").fetchone()[0]


def test_13_svaret_gir_ikke_klokkeslettet_til_en_annens_tidligere_registrering(con):
    kid, did, pid, dag = _innsjekk_oppsett(con)
    assert deltakerside.registrer_qr(con, dag, IDAG, epost="kari@example.no").tid            # ny: klokkeslettet er nå
    res = deltakerside.registrer_qr(con, dag, IDAG, epost="kari@example.no")
    assert res.status == "allerede" and res.tid is None and res.fornavn is None
    innlogget = deltakerside.registrer_qr(con, dag, IDAG, deltaker_id=did)
    assert innlogget.status == "allerede" and innlogget.tid and innlogget.fornavn == "Kari"
    assert "(kl." not in deltakerside.tekst_for(res, innlogget=False) and "(kl." in deltakerside.tekst_for(innlogget, innlogget=True)


def test_13_resultatsiden_til_uinnlogget_viser_ikke_klokkeslett_for_allerede_registrert(con):
    kid, did, pid, dag = _innsjekk_oppsett(con)
    url = f"/innsjekk/{dag['innsjekk_token']}"
    k = _klient()
    k.post(url, data={"epost": "kari@example.no"})
    con.execute("UPDATE oppmote SET ts='2027-03-10 07:47:00'")
    con.commit()
    html = k.post(url, data={"epost": "kari@example.no"}).get_data(as_text=True)
    assert "Du er allerede registrert" in html and "08:47" not in html and "kl." not in html.split("inn-under")[1].split("</p>")[0]


def test_2_standard_e_post_alene_holder_og_registrerer_uten_innlogging(con, monkeypatch):
    monkeypatch.setattr(config, "INNSJEKK_KREVER_INNLOGGING", False)
    kid, did, pid, dag = _innsjekk_oppsett(con)
    r = _klient().post(f"/innsjekk/{dag['innsjekk_token']}", data={"epost": "kari@example.no"})
    assert "Oppmøte er registrert" in r.get_data(as_text=True) and _oppmote(con) == 1


def test_2_bryter_innlogging_kreves_qr_registrerer_ikke_paa_e_post_alene(con, monkeypatch):
    monkeypatch.setattr(config, "INNSJEKK_KREVER_INNLOGGING", True)
    kid, did, pid, dag = _innsjekk_oppsett(con)
    url = f"/innsjekk/{dag['innsjekk_token']}"
    k = _klient()
    for svar in (k.get(url), k.post(url, data={"epost": "kari@example.no"}), k.post(url, data={"epost": "ukjent@example.no"})):
        html = svar.get_data(as_text=True)
        assert svar.status_code == 200 and "Logg inn først" in html and "Send meg innloggingslenke" in html
        assert f'name="neste" value="{url}"' in html and 'action="/logg-inn"' in html
        assert "Oppmøte er registrert" not in html and "Vi fant ikke" not in html and "Vi kunne ikke registrere" not in html
    assert _oppmote(con) == 0


def test_2_bryter_gir_samme_side_for_kjent_og_ukjent_adresse_ingen_orakel(con, monkeypatch):
    monkeypatch.setattr(config, "INNSJEKK_KREVER_INNLOGGING", True)
    kid, did, pid, dag = _innsjekk_oppsett(con)
    url = f"/innsjekk/{dag['innsjekk_token']}"
    a = _klient().post(url, data={"epost": "kari@example.no"}).get_data(as_text=True)
    b = _klient().post(url, data={"epost": "aldri-sett@example.no"}).get_data(as_text=True)
    strip = lambda h: re.sub(r'(name="csrf_token" value|nonce)="[^"]*"', "", h)      # noqa: E731
    assert strip(a) == strip(b)


def test_2_bryter_innlogget_deltaker_registrerer_med_ett_trykk(con, monkeypatch):
    monkeypatch.setattr(config, "INNSJEKK_KREVER_INNLOGGING", True)
    kid, did, pid, dag = _innsjekk_oppsett(con)
    url = f"/innsjekk/{dag['innsjekk_token']}"
    k = deltaker_klient(did)
    assert "Registrer oppmøte" in k.get(url).get_data(as_text=True) and _oppmote(con) == 0
    assert "Velkommen, Kari" in k.post(url).get_data(as_text=True) and _oppmote(con) == 1
    assert "Du er allerede registrert" in k.post(url).get_data(as_text=True) and _oppmote(con) == 1


def test_2_bryter_hele_flyten_engangslenke_til_innsjekksiden_og_ett_trykk(con, monkeypatch):
    monkeypatch.setattr(config, "INNSJEKK_KREVER_INNLOGGING", True)
    kid, did, pid, dag = _innsjekk_oppsett(con)
    url = f"/innsjekk/{dag['innsjekk_token']}"
    k = _klient()
    assert k.post("/logg-inn", data={"epost": "kari@example.no", "neste": url}).status_code == 200
    token = con.execute("SELECT token FROM innlogging_token WHERE deltaker_id=?", (did,)).fetchone()[0]
    r = k.get(f"/logg-inn/{token}?neste={url}")
    assert r.status_code == 302 and r.headers["Location"] == url
    assert "Registrer oppmøte" in k.get(url).get_data(as_text=True) and _oppmote(con) == 0
    assert "Velkommen, Kari" in k.post(url).get_data(as_text=True) and _oppmote(con) == 1


def test_2_bryter_paa_stengt_dag_forklares_som_for(con, monkeypatch):
    """Med «krever innlogging» på: en uinnlogget som skanner QR-koden får beskjed om å logge inn (ingen oppmøte), og en dag som ikke er åpen i dag
    forklares som før. (Kodesiden /innsjekk er fjernet: uten QR-kode registrerer deltakeren seg på Min side.)"""
    monkeypatch.setattr(config, "INNSJEKK_KREVER_INNLOGGING", True)
    kid, did, pid, dag = _innsjekk_oppsett(con)
    r = _klient().post(f"/innsjekk/{dag['innsjekk_token']}", data={"epost": "kari@example.no"})
    assert "Logg inn først" in r.get_data(as_text=True) and _oppmote(con) == 0
    r = deltaker_klient(did).post(f"/innsjekk/{dag['innsjekk_token']}")
    assert r.status_code == 200 and _oppmote(con) == 1
    ny_dag = db.kursdager(con, kid)[1]                                                          # i morgen: ikke åpen i dag, også med bryteren på
    assert "ikke åpen i dag" in _klient().get(f"/innsjekk/{ny_dag['innsjekk_token']}").get_data(as_text=True)


def test_2_plakaten_forklarer_innlogging_naar_bryteren_er_paa(con, monkeypatch):
    kid, did, pid, dag = _innsjekk_oppsett(con)
    admin = admin_klient()
    monkeypatch.setattr(config, "INNSJEKK_KREVER_INNLOGGING", False)
    html = admin.get(f"/admin/kurs/{kid}/qr/{dag['id']}").get_data(as_text=True)
    assert "Skriv e-postadressen du meldte deg på med, og trykk «Registrer oppmøte»" in html and "klikk på lenken du får på e-post" not in html
    monkeypatch.setattr(config, "INNSJEKK_KREVER_INNLOGGING", True)
    html = admin.get(f"/admin/kurs/{kid}/qr/{dag['id']}").get_data(as_text=True)
    assert "klikk på lenken du får på e-post, og trykk «Registrer oppmøte»" in html


def test_2_bryteren_er_av_som_standard_og_dokumentert():
    assert "INNSJEKK_KREVER_INNLOGGING=0" in (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    assert config.get("INNSJEKK_KREVER_INNLOGGING", "0") in ("0", "1")


# ============================================================ (36/38) «Spør oss» i e-postmalene ============================================================

def test_36_kodeknappen_for_sporsmal_url_vises_ikke_naar_assistenten_er_av(con, monkeypatch):
    monkeypatch.setattr(config, "ASSISTENT_AKTIV", False)
    html = admin_klient().get("/admin/e-postmaler/avlysning").get_data(as_text=True)
    assert "Spør oss" not in html and "sporsmal_url" not in html
    assert "{min_side}" in html                                                                # andre koder er der fortsatt


def test_36_kodeknappen_vises_naar_assistenten_er_paa_eller_malen_allerede_bruker_koden(con, monkeypatch):
    monkeypatch.setattr(config, "ASSISTENT_AKTIV", True)
    assert "{sporsmal_url}" in admin_klient().get("/admin/e-postmaler/avlysning").get_data(as_text=True)
    monkeypatch.setattr(config, "ASSISTENT_AKTIV", False)
    db.sett_maltekst(con, "avlysning", "tekst", "Spørsmål? {sporsmal_url}", aktor="admin:test")
    con.commit()
    html = admin_klient().get("/admin/e-postmaler/avlysning").get_data(as_text=True)
    assert 'data-sett-inn="{sporsmal_url}"' in html                                            # allerede i bruk: administratoren må kunne se og fjerne den


def test_36_koden_er_fortsatt_gyldig_og_data_beholdt():
    assert "sporsmal_url" in maltekster.KODER and maltekster.valider("avlysning", "tekst", "Se {sporsmal_url}")


def test_36_sporsmal_url_gir_forsiden_naar_assistenten_er_av_slik_at_gamle_tekster_ikke_far_en_doed_lenke(con, monkeypatch):
    monkeypatch.setattr(config, "ASSISTENT_AKTIV", False)
    verdier = {"kursnavn": "K", "fornavn": "Kari", "navn": "Kari N"}
    av = maltekster.felttekst_til_html("avlysning", "tekst", "Se {sporsmal_url}", verdier)
    assert f"Se {config.BASE_URL}" in av and "/sporsmal" not in av
    assert _klient().get("/sporsmal").status_code == 404                                          # siden finnes ikke, så lenken hadde vært død
    monkeypatch.setattr(config, "ASSISTENT_AKTIV", True)
    assert f"{config.BASE_URL}/sporsmal" in maltekster.felttekst_til_html("avlysning", "tekst", "Se {sporsmal_url}", verdier)


# ============================================================ (39) gamle «Min side»-ord ============================================================

def test_39_innloggings_e_posten_og_bunnteksten_sier_ikke_lenger_kursmateriell_paa_min_side(con):
    emne, html = epost.render("innlogging", fornavn="Kari", lenke="http://x/logg-inn/T?neste=/kurs/K1/deltakerside")
    assert emne == "Logg inn – IPR Påmeldingssystem" and "Min side" not in emne
    assert "kursmateriell, oppmøte og faktura" not in html
    ramme = (Path(__file__).resolve().parent.parent / "kurs" / "maler" / "epost" / "_ramme.html").read_text(encoding="utf-8")
    assert ramme.count("her finner du kursene dine, faktura og kursbevis") == 2 and "kursmateriell" not in ramme


def test_39_kvitteringen_sier_logg_inn_ikke_gaa_til_min_side():
    kvittering = (TEMPLATES / "kvittering.html").read_text(encoding="utf-8")
    assert ">Logg inn</a>" in kvittering and "Gå til Min side" not in kvittering


# ============================================================ (37/40) Min side ============================================================

def test_40_kurs_med_kursdag_i_dag_staar_oeverst_paa_min_side(con):
    gammel = lag_kurs(con, "GAMMEL", start=IDAG, dager=1)                                      # kursdag i dag, meldt på først
    did, _ = lag_deltaker(con, gammel, "kari@example.no")
    ny = lag_kurs(con, "NY", start=IDAG + timedelta(days=30), dager=1)                        # nyest, men ingen kursdag i dag
    lag_deltaker(con, ny, "kari@example.no")
    nyest = lag_kurs(con, "NYEST", start=IDAG + timedelta(days=40), dager=1)
    lag_deltaker(con, nyest, "kari@example.no")
    con.execute("UPDATE paamelding SET opprettet='2027-01-01 10:00:00' WHERE kurs_id=?", (gammel,))
    con.execute("UPDATE paamelding SET opprettet='2027-02-01 10:00:00' WHERE kurs_id=?", (ny,))
    con.execute("UPDATE paamelding SET opprettet='2027-03-01 10:00:00' WHERE kurs_id=?", (nyest,))
    con.commit()
    html = deltaker_klient(did).get("/min-side").get_data(as_text=True)
    ider = re.findall(r'<h3 id="kurs-\d+-h">([^<]*)</h3>', html)
    assert ider == ["Kurs GAMMEL", "Kurs NYEST", "Kurs NY"]                                    # i dag først, resten nyeste først


def test_40_uten_kursdag_i_dag_er_rekkefolgen_som_for_nyeste_paamelding_forst(con):
    a = lag_kurs(con, "A", start=IDAG + timedelta(days=10), dager=1)
    did, _ = lag_deltaker(con, a, "kari@example.no")
    b = lag_kurs(con, "B", start=IDAG + timedelta(days=20), dager=1)
    lag_deltaker(con, b, "kari@example.no")
    con.execute("UPDATE paamelding SET opprettet='2027-01-01 10:00:00' WHERE kurs_id=?", (a,))
    con.execute("UPDATE paamelding SET opprettet='2027-02-01 10:00:00' WHERE kurs_id=?", (b,))
    con.commit()
    html = deltaker_klient(did).get("/min-side").get_data(as_text=True)
    assert re.findall(r'<h3 id="kurs-\d+-h">([^<]*)</h3>', html) == ["Kurs B", "Kurs A"]


def test_37_krav_om_dagens_kode_er_fortsatt_standard_for_fysiske_kurs_og_kan_slaas_av_per_kurs(con, kid):
    """K10 (avklares med brukeren): standarden krever dagens kode. Én innstilling per kurs (Kursside › Innstillinger) gir ren avkryssing."""
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    skriv_side(con, kid, dokument(tekstblokk()))
    assert deltakerside.registrer_selv(con, did, "K1", "", IDAG).status == "kode_feil"
    sidelager.sett_innstillinger(con, kid, innsjekk_krever_kode=False, aktor="admin:test")
    con.commit()
    assert deltakerside.registrer_selv(con, did, "K1", "", IDAG).status == "ny"


# ============================================================ (24-35) testhull: mutantene fra kritikerne ============================================================

# ---- F08 (24): gårsdagens QR-plakat kan ikke registrere i dag ----
def test_24_qr_fra_i_gaar_registrerer_ingenting_i_dag(con, monkeypatch):
    kid = lag_kurs(con, dager=2)                                                                # dag 1 = IDAG, dag 2 = IDAG + 1
    lag_deltaker(con, kid, "kari@example.no")
    dag1 = db.kursdager(con, kid)[0]
    fast_dato(monkeypatch, IDAG + timedelta(days=1))                                            # dag 2: plakaten fra dag 1 henger fortsatt på veggen
    html = _klient().post(f"/innsjekk/{dag1['innsjekk_token']}", data={"epost": "kari@example.no"}).get_data(as_text=True)
    assert "ikke åpen i dag" in html and _oppmote(con) == 0
    res = deltakerside.registrer_qr(con, dag1, IDAG + timedelta(days=1), epost="kari@example.no")
    assert res.status == "ikke_i_dag" and _oppmote(con) == 0


def test_24_qr_for_i_morgen_registrerer_heller_ikke(con):
    kid = lag_kurs(con, dager=2)
    lag_deltaker(con, kid, "kari@example.no")
    dag2 = db.kursdager(con, kid)[1]
    assert deltakerside.registrer_qr(con, dag2, IDAG, epost="kari@example.no").status == "ikke_i_dag" and _oppmote(con) == 0


# ---- M06 (25): publisering sletter ikke en ny, ubrukt fil ----
def test_25_publisering_sletter_ikke_en_ny_fil_som_ikke_er_brukt_ennaa(con, kid):
    fil_id, _ = sidelager.lagre_fil(con, kid, "til-senere.pdf", pdf(), "admin:test")
    i_gaar = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    con.execute("UPDATE kursside_fil SET opprettet=? WHERE id=?", (i_gaar, fil_id))
    con.commit()
    skriv_side(con, kid, dokument(tekstblokk()))                                                # publiserer: rydder bare filer eldre enn 30 dager
    assert sidelager.fil_meta(con, kid, fil_id) is not None


def test_25_standardgrensen_er_30_dager_og_gamle_ubrukte_filer_ryddes(con, kid):
    ny, _ = sidelager.lagre_fil(con, kid, "ny.pdf", pdf(b"a"), "admin:test")
    gammel, _ = sidelager.lagre_fil(con, kid, "gammel.pdf", pdf(b"b"), "admin:test")
    for fil, dager in ((ny, 29), (gammel, 31)):
        con.execute("UPDATE kursside_fil SET opprettet=? WHERE id=?", ((datetime.now(timezone.utc) - timedelta(days=dager)).strftime("%Y-%m-%d %H:%M:%S"), fil))
    con.commit()
    assert sidelager.rydd_foreldrelose_filer(con, kid) == 1
    assert sidelager.fil_meta(con, kid, ny) is not None and sidelager.fil_meta(con, kid, gammel) is None


# ---- X09/X10/X12 (27): escaping i malen ----
ANGREP = '"><script>alert(9)</script><img src=x onerror=alert(8)>'


def test_27_filtittel_bilde_alt_og_bildetekst_med_html_vises_som_tekst(con, kid):
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    pdf_id, _ = sidelager.lagre_fil(con, kid, "a.pdf", pdf(), "admin:test")
    bilde_id, _ = sidelager.lagre_fil(con, kid, "a.png", png(), "admin:test")
    skriv_side(con, kid, dokument(
        {"id": "", "type": "filer", "tittel": "Filer", "data": {"filer": [{"fil_id": pdf_id, "tittel": ANGREP}]}},
        {"id": "", "type": "bilde", "tittel": "Bilde", "data": {"fil_id": bilde_id, "alt": ANGREP, "tekst": ANGREP, "plassering": "bred"}}))
    html = _side_html(con, kid, did)
    assert "alert(9)" in html                                                                   # teksten vises (escapet) ...
    assert "<script>alert(9)" not in html and "<img src=x onerror" not in html and '"><script' not in html      # ... men er aldri markup
    assert html.count("&lt;script&gt;alert(9)&lt;/script&gt;") >= 3 or html.count("&lt;script&gt;alert(9)") >= 3


def test_27_samme_for_toppbildets_alt_og_tekst(con, kid):
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    bilde_id, _ = sidelager.lagre_fil(con, kid, "topp.png", png(), "admin:test")
    skriv_side(con, kid, dokument({"id": "", "type": "bilde", "tittel": "T", "data": {"fil_id": bilde_id, "alt": ANGREP, "tekst": ANGREP, "plassering": "topp"}}))
    html = _side_html(con, kid, did)
    assert "<script>alert(9)" not in html and "<img src=x onerror" not in html and '"><script' not in html


# ---- T20 (28): dag n av m ----
def test_28_dagnummer_og_fremdrift_er_riktig_for_dag_to_og_tre(con, monkeypatch):
    kid = lag_kurs(con, dager=3)
    lag_deltaker(con, kid, "kari@example.no")
    dager = db.kursdager(con, kid)
    assert deltakerside.dagnummer(con, kid, dager[1]["id"]) == (2, 3) and deltakerside.dagnummer(con, kid, dager[2]["id"]) == (3, 3)
    fast_dato(monkeypatch, IDAG + timedelta(days=2))
    html = _klient().post(f"/innsjekk/{dager[2]['innsjekk_token']}", data={"epost": "kari@example.no"}).get_data(as_text=True)
    assert "Dag 3 av 3" in html and html.count("inn-seg-ferdig") == 2 and html.count("inn-seg-na") == 1
    fast_dato(monkeypatch, IDAG + timedelta(days=1))
    assert "Dag 2 av 3" in _klient().get(f"/innsjekk/{dager[1]['innsjekk_token']}").get_data(as_text=True)


# ---- A17/P06/P07 (29): har_side ----
def test_29_har_side_er_usann_for_upublisert_side(con, kid):
    skriv_side(con, kid, dokument(tekstblokk()), publiser=False)
    assert not deltakerside.har_side(con, kid, IDAG)


def test_29_har_side_er_sann_for_publisert_og_usann_naar_siden_er_nedtatt_eller_stengt(con, kid):
    skriv_side(con, kid, dokument(tekstblokk()))
    assert deltakerside.har_side(con, kid, IDAG)
    sidelager.sett_aktiv(con, kid, False, "admin:test")
    con.commit()
    assert not deltakerside.har_side(con, kid, IDAG)
    sidelager.sett_aktiv(con, kid, True, "admin:test")
    sidelager.sett_innstillinger(con, kid, stenges="2000-01-01", aktor="admin:test")
    con.commit()
    assert not deltakerside.har_side(con, kid, IDAG)


def test_29_innsjekkresultatet_tilbyr_kurssiden_bare_naar_den_er_apen(con, kid):
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    dag = db.kursdager(con, kid)[0]
    skriv_side(con, kid, dokument(tekstblokk()), publiser=False)
    html = _klient().post(f"/innsjekk/{dag['innsjekk_token']}", data={"epost": "kari@example.no"}).get_data(as_text=True)
    assert "Send meg innloggingslenke" not in html                                              # ikke publisert: ingenting å lenke til
    skriv_side(con, kid, dokument(tekstblokk("Ny")))
    html = _klient().post(f"/innsjekk/{dag['innsjekk_token']}", data={"epost": "kari@example.no"}).get_data(as_text=True)
    assert "Send meg innloggingslenke" in html
    sidelager.sett_aktiv(con, kid, False, "admin:test")
    con.commit()
    assert "Send meg innloggingslenke" not in _klient().post(f"/innsjekk/{dag['innsjekk_token']}", data={"epost": "kari@example.no"}).get_data(as_text=True)


# ---- B10 (30): data:-adresser ----
@pytest.mark.parametrize("adresse", ["data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==", "data:text/plain,hei", "DATA:text/html;base64,AAAA", "Data:,x"])
def test_30_data_adresser_avvises_som_lenke(adresse):
    assert si.trygg_url(adresse) is None
    assert signaturer.trygg_lenke(adresse) is None


# ---- C16 (31): bilder får typen fra innholdet ----
def test_31_bilde_serveres_med_sniffet_type_selv_om_lagret_mimetype_er_manipulert(con, kid):
    fil_id, _ = sidelager.lagre_fil(con, kid, "a.png", png(), "admin:test")
    con.execute("UPDATE kursside_fil SET mimetype='text/html' WHERE id=?", (fil_id,))
    con.commit()
    r = admin_klient().get(_u(kid, f"/fil/{fil_id}"))
    assert r.status_code == 200 and r.headers["Content-Type"] == "image/png" and r.headers["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in r.headers["Content-Security-Policy"]


def test_31_samme_for_deltakerens_nedlasting(con, kid):
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    _pdf, bilde_id = _side_med_filer(con, kid)
    con.execute("UPDATE kursside_fil SET mimetype='text/html' WHERE id=?", (bilde_id,))
    con.commit()
    r = deltaker_klient(did).get(f"/kurs/K1/deltakerside/fil/{bilde_id}")
    assert r.status_code == 200 and r.headers["Content-Type"] == "image/png"


# ---- G05 (32): tom kode ----
def test_32_selvregistrering_med_tom_kode_avvises_selv_om_kursdagen_mangler_kode(con):
    kid = lag_kurs(con, dager=1)
    did, _pid = lag_deltaker(con, kid, "kari@example.no")
    con.execute("UPDATE kursdag SET innsjekk_kode='' WHERE kurs_id=?", (kid,))
    con.commit()
    for kode in ("", "   ", "-", None):
        assert deltakerside.registrer_selv(con, did, "K1", kode, IDAG).status == "kode_feil"
    assert _oppmote(con) == 0


# ---- G12 (33): fornavn til uinnlogget ----
def test_33_registrer_qr_gir_ikke_fornavn_til_uinnlogget(con):
    kid = lag_kurs(con, dager=1)
    did, _pid = lag_deltaker(con, kid, "kari@example.no", fornavn="Kari")
    dag = db.kursdager(con, kid)[0]
    res = deltakerside.registrer_qr(con, dag, IDAG, epost="kari@example.no")
    assert res.status == "ny" and res.fornavn is None
    res2 = deltakerside.registrer_qr(con, dag, IDAG, deltaker_id=did)
    assert res2.status == "allerede" and res2.fornavn == "Kari"


# ---- M09 (34): Min side viser ikke avmeldte ----
def test_34_min_side_viser_ikke_avmeldte_paameldinger(con):
    kid = lag_kurs(con, "AVM", dager=1)
    did, _pid = lag_deltaker(con, kid, "kari@example.no", status="avmeldt")
    igjen = lag_kurs(con, "BEHOLD", dager=1)
    lag_deltaker(con, igjen, "kari@example.no")
    html = deltaker_klient(did).get("/min-side").get_data(as_text=True)
    assert "Kurs AVM" not in html and "Kurs BEHOLD" in html


# ---- W05, W06, W11, V06, V07, V09 (35): grenser og visningsdetaljer ----
def test_35_ugyldig_blokk_id_erstattes_med_ny_id_i_riktig_format():
    for tekst in ('"><script>alert(1)</script>', "b_", "b_ABC", "b_æøå123", "x" * 20, "", "b_abc"):
        dok, _ = si.valider(dokument({"id": tekst, "type": "tekst", "tittel": "T", "data": {"html": "<p>x</p>"}}), fil_ider={}, kursdag_ider=set())
        assert re.fullmatch(r"b_[a-z0-9]{4,12}", dok["blokker"][0]["id"]), tekst
    dok, _ = si.valider(dokument({"id": "b_gyldig01", "type": "tekst", "tittel": "T", "data": {"html": ""}}), fil_ider={}, kursdag_ider=set())
    assert dok["blokker"][0]["id"] == "b_gyldig01"


def test_35_over_60_blokker_gir_sidefeil():
    blokk = {"id": "", "type": "tekst", "tittel": "T", "data": {"html": "<p>x</p>"}}
    assert si.MAKS_BLOKKER == 60
    si.valider(dokument(*[dict(blokk) for _ in range(60)]), fil_ider={}, kursdag_ider=set())
    with pytest.raises(si.Sidefeil):
        si.valider(dokument(*[dict(blokk) for _ in range(61)]), fil_ider={}, kursdag_ider=set())


def test_35_lenkeadresse_over_2000_tegn_avvises():
    assert si.MAKS_URL == 2000
    assert si.trygg_url("https://a.no/" + "x" * (2000 - len("https://a.no/"))) is not None
    assert si.trygg_url("https://a.no/" + "x" * (2000 - len("https://a.no/") + 1)) is None


def test_35_digitalt_kurs_har_ingen_kartlenke_og_gamle_filer_har_ikke_ny_merke(con):
    kid = db.opprett_kurs(con, kode="D1", navn="Digitalt kurs", datoer=[IDAG.isoformat()], type="digital", sted="Nettkurs", status="aapen")
    con.commit()
    did, _pid = lag_deltaker(con, kid, "kari@example.no")
    fil_id, _ = sidelager.lagre_fil(con, kid, "gammel.pdf", pdf(), "admin:test")
    con.execute("UPDATE kursside_fil SET opprettet=? WHERE id=?", ((datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"), fil_id))
    con.commit()
    skriv_side(con, kid, dokument({"id": "", "type": "filer", "tittel": "Filer", "data": {"filer": [{"fil_id": fil_id, "tittel": "Gammel"}]}}))
    html = deltaker_klient(did).get("/kurs/D1/deltakerside").get_data(as_text=True)
    assert "Gammel" in html and "openstreetmap" not in html and 'class="dp-ny"' not in html


def test_35_fysisk_kurs_har_kartlenke_og_ny_fil_har_ny_merke(con, kid):
    did, _pid = lag_deltaker(con, kid, "kari@example.no")
    fil_id, _ = sidelager.lagre_fil(con, kid, "ny.pdf", pdf(), "admin:test")
    con.execute("UPDATE kursside_fil SET opprettet='2027-03-10 08:00:00' WHERE id=?", (fil_id,))                # lagt ut «i dag» (appens dato er fast)
    con.commit()
    skriv_side(con, kid, dokument({"id": "", "type": "filer", "tittel": "Filer", "data": {"filer": [{"fil_id": fil_id, "tittel": "Ny fil"}]}}))
    html = _side_html(con, kid, did)
    assert "openstreetmap.org/search?query=Bergen" in html and 'class="dp-ny"' in html


def test_35_storrelse_tekst_bruker_1024():
    assert deltakerside.storrelse_tekst(1_000_000) == "977 kB" and deltakerside.storrelse_tekst(1024 * 1024) == "1,0 MB"
    assert deltakerside.storrelse_tekst(1024) == "1 kB" and deltakerside.storrelse_tekst(1) == "1 kB" and deltakerside.storrelse_tekst(15 * 1024 * 1024) == "15,0 MB"


# ---- publisering med gammel versjon (kritikerens D03-D05, allerede godt dekket: låser det) ----
def test_publisering_med_gammel_versjon_gir_409_og_publiserer_ikke(con, kid):
    admin = admin_klient()
    v1 = json_post(admin, _u(kid, "/lagre"), {"versjon": 0, "dokument": dokument(tekstblokk("A"))}).get_json()["versjon"]
    assert json_post(admin, _u(kid, "/lagre"), {"versjon": v1, "dokument": dokument(tekstblokk("B"))}).status_code == 200
    r = json_post(admin, _u(kid, "/publiser"), {"versjon": v1})
    assert r.status_code == 409 and r.get_json()["status"] == "konflikt" and sidelager.hent(con, kid)["publisert"] is None


# ============================================================ dokumentasjon: det som er lovet stemmer med det som er bygget ============================================================

def test_dokumentasjonen_beskriver_innsjekkbryteren_og_de_rettede_svakhetene():
    rot = Path(__file__).resolve().parent.parent
    for fil in ("OPERATIONS.md", "SECURITY.md", "DEPLOYMENT.md", "GDPR.md"):
        assert "INNSJEKK_KREVER_INNLOGGING" in (rot / fil).read_text(encoding="utf-8"), fil
    sikkerhet_md = (rot / "SECURITY.md").read_text(encoding="utf-8")
    assert "Å rette `dokument()` er ikke gjort" not in sikkerhet_md and "_admin_okt_gyldig()" in sikkerhet_md and "lineær tid" in sikkerhet_md
    drift = (rot / "OPERATIONS.md").read_text(encoding="utf-8")
    assert "alle **avvisninger**" in drift and "(kl. 08:47)»" not in drift.split("**Bare én gang per dag:**")[1].split("Første registrering")[0].split("Er hun innlogget")[0]
    assert "SharePoint-filer stengt" in drift


# ===================== (14b) bilde til høyre i toppen, og bilde til høyre med tekst (Camilla 04.10.2026) =====================

def test_14b_bilde_til_hoeyre_i_toppen_ligger_i_toppfeltet_ved_teksten(con, kid):
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    fil_id, _ = sidelager.lagre_fil(con, kid, "rom.png", png(), "admin:test")
    skriv_side(con, kid, dokument({"id": "", "type": "bilde", "tittel": "Bilde", "data": {"fil_id": fil_id, "alt": "Kursrommet", "tekst": "", "plassering": "topp_side"}},
                                  tekstblokk()))
    html = _side_html(con, kid, did)
    topp = html.index('<header class="dp-hero dp-hero-side">')
    slutt = html.index("</header>", topp)
    assert topp < html.index('<figure class="dp-sidebilde">') < slutt and 'alt="Kursrommet"' in html[topp:slutt]
    assert html.count('class="dp-sidebilde"') == 1 and "dp-toppbilde" not in html


def test_14b_bilde_til_hoeyre_med_tekst_til_venstre_og_lengre_tekst(con, kid):
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    fil_id, _ = sidelager.lagre_fil(con, kid, "rom.png", png(), "admin:test")
    tekst = "Velkommen!\nVi møtes i Bergen. " + "x" * 500                         # lengre enn en vanlig bildetekst (300)
    skriv_side(con, kid, dokument({"id": "", "type": "bilde", "tittel": "Bilde", "data": {"fil_id": fil_id, "alt": "Rommet", "tekst": tekst, "plassering": "hoyre"}}))
    html = _side_html(con, kid, did)
    side = html[html.index('<div class="dp-bilde-side">'):]
    assert side.index('<div class="dp-bilde-sidetekst"><p>Velkommen!</p><p>Vi møtes i Bergen.') < side.index('<figure class="dp-bilde dp-bilde-hoyre">')
    for plass in ("bred", "liten"):                                                 # andre plasseringer: fortsatt maks 300 tegn
        with pytest.raises(si.Sidefeil):
            si.valider(dokument({"id": "", "type": "bilde", "tittel": "B", "data": {"fil_id": None, "alt": "a", "tekst": "y" * 301, "plassering": plass}}),
                       fil_ider=None, kursdag_ider=None)


def test_14b_sidebildet_forsvinner_ikke_naar_siden_ogsaa_har_toppbilde(con, kid):
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    topp, _ = sidelager.lagre_fil(con, kid, "topp.png", png(), "admin:test")
    side, _ = sidelager.lagre_fil(con, kid, "side.png", png(5, 3), "admin:test")         # et annet bilde enn toppbildet
    skriv_side(con, kid, dokument({"id": "", "type": "bilde", "tittel": "Topp", "data": {"fil_id": topp, "alt": "Topp", "tekst": "", "plassering": "topp"}},
                                  {"id": "", "type": "bilde", "tittel": "Side", "data": {"fil_id": side, "alt": "Sidebildet", "tekst": "", "plassering": "topp_side"}}))
    html = _side_html(con, kid, did)
    assert 'class="dp-toppbilde"' in html and 'alt="Sidebildet"' in html and "dp-sidebilde" not in html
