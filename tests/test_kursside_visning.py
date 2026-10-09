"""Utseendet på kurssiden for deltakere (kursside_deltaker.html, _kursside_blokker.html, _kursside_ikoner.html, static/kursside-delt.css og kursside-delt.js).

Tilgangsreglene og innholdsfiltreringen har egne tester (test_kursside_tilgang.py); her testes markup, rekkefølge, tilgjengelighet, escaping, innsjekk-kortets
tilstander, kontrast, utskriftsstil og reglene for CSP (ingen inline stil eller skript). Utseendet i nettleseren (PC og mobil, 1440 til 320 px) er prøvd
med skjermbilder fra headless Edge (se BYGG-2.md). Bare oppdiktede data.
"""
import re
from datetime import timedelta
from pathlib import Path

import pytest
from flask import render_template

from kurs import deltakerside, sidelager

from kurssidehjelp import (IDAG, admin_klient, dokument, deltaker_klient, fast_dato, lag_deltaker, lag_kurs, ny_database, pdf, png, skriv_side, tekstblokk)

KODE = Path(__file__).resolve().parent.parent / "kurs"
MALER = KODE / "web" / "templates"
STATIC = KODE / "web" / "static"
CSS = STATIC / "kursside-delt.css"
JS = STATIC / "kursside-delt.js"
ANGREP = '"><script>alert(1)</script><img src=x onerror=alert(2)>'


def _les(sti: Path) -> str:
    return sti.read_bytes().decode("utf-8")


def _uten_kommentarer(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    fast_dato(monkeypatch)
    yield c
    c.close()


@pytest.fixture
def kid(con):
    return lag_kurs(con, "K1", dager=3)


def _fyll(con, kid, **topp) -> int:
    """Publiserer en side med alle åtte blokktyper (oppdiktet innhold) og returnerer fil-id-en til bildet."""
    pdf_id, _ = sidelager.lagre_fil(con, kid, "Presentasjon dag 1.pdf", pdf(), "admin:test")
    bilde_id, _ = sidelager.lagre_fil(con, kid, "kursrom.png", png(8, 4), "admin:test")
    dager = [{"kursdag_id": None, "dato": (IDAG + timedelta(days=n)).isoformat(), "tittel": f"Dag {n + 1}",
              "punkter": [{"fra": "09:00", "til": "10:00", "tema": "Innledning", "sted": "Rom 3", "hvem": "Marte", "pause": False},
                          {"fra": "10:00", "til": "10:15", "tema": "Pause", "sted": "", "hvem": "", "pause": True}]} for n in range(2)]
    blokker = [
        {"id": "b_topp0001", "type": "bilde", "tittel": "Bilde", "data": {"fil_id": bilde_id, "alt": "Kursrommet med stoler i sirkel", "tekst": "Kursrommet", "plassering": "topp"}},
        tekstblokk("Velkommen", "<p>Hei <strong>alle</strong> sammen</p><ul><li>Notatbok</li></ul>", id="b_tekst001"),
        {"id": "b_viktig01", "type": "viktig", "tittel": "Rombytte", "data": {"niva": "advarsel", "tekst": "Vi bytter rom.\n\nSe skjermen."}},
        {"id": "b_progr001", "type": "program", "tittel": "Program", "data": {"dager": dager}},
        {"id": "b_filer001", "type": "filer", "tittel": "Presentasjoner", "data": {"filer": [{"fil_id": pdf_id, "tittel": "Dag 1 – Innledning", "gruppe": None, "synlig_fra": None}],
                                                                                    "vis_kommende": True}},
        {"id": "b_lenke001", "type": "lenker", "tittel": "Litteratur", "data": {"lenker": [
            {"tittel": "Kompendium", "url": "https://bibliotek.example.no/k", "tekst": "Kapittel 1", "nivaa": "obligatorisk", "kilde": None},
            {"tittel": "Artikkel", "url": "https://tidsskrift.example.no/a", "tekst": "", "nivaa": "anbefalt", "kilde": None}]}},
        {"id": "b_tabell01", "type": "tabell", "tittel": "Grupper", "data": {"kolonner": ["Gruppe", "Deltakere"], "rader": [["Gruppe 1", "Kari N., Ola H."]]}},
        {"id": "b_kontakt1", "type": "kontakt", "tittel": "Kontakt", "data": {"personer": [{"navn": "Marte Solberg", "rolle": "Kursansvarlig", "telefon": "+47 555 55 501", "epost": "marte@example.no"}]}},
    ]
    skriv_side(con, kid, dokument(*blokker, tittel="Kurssiden vår", ingress="Alt du trenger under kurset.", rom="Rom 3, 2. etasje",
                                  melding={"tekst": "Husk notatbok.", "niva": "info"}, **topp))
    return bilde_id


def _side(con, kid, epost="kari@example.no") -> str:
    did, _pid = lag_deltaker(con, kid, epost)
    r = deltaker_klient(did).get("/kurs/K1/deltakerside")
    assert r.status_code == 200
    return r.get_data(as_text=True)


# ============================ oppbygging og rekkefølge ============================

def test_siden_har_toppfelt_aside_hopp_til_og_blokker_i_riktig_rekkefolge(con, kid):
    _fyll(con, kid)
    html = _side(con, kid)
    assert html.count("<h1") == 1 and '<header class="dp-hero dp-hero-bilde">' in html          # toppbildet ligger bak tittelen, i toppfeltet
    rekkefolge = [html.index(x) for x in ('class="dp-hero dp-hero-bilde"', 'class="dp-melding', 'class="dp-grid"', 'class="dp-aside"', 'class="dp-chips"', 'class="dp-hoved"',
                                            'class="dp-fot"')]
    assert rekkefolge == sorted(rekkefolge)
    assert html.index('id="i-dag"') < html.index('class="dp-dager"') < html.index('class="dp-meny"')            # I dag, Kursdager, På denne siden
    assert 'aria-label="Oppsummering"' in html and 'aria-label="Hopp til"' in html and 'aria-labelledby="dp-meny-h"' in html
    assert html.index('class="dp-toppbilde"') < html.index('class="dp-melding') < html.index('class="dp-grid"')


def test_blokkene_har_overskrift_ikon_og_id_i_riktig_rekkefolge_og_hopp_til_lenkene_treffer(con, kid):
    _fyll(con, kid)
    html = _side(con, kid)
    ider = re.findall(r'<section class="dp-(?:blokk|viktig)[^"]*" id="(blokk-b_[a-z0-9]+)"', html)
    assert ider == ["blokk-b_tekst001", "blokk-b_viktig01", "blokk-b_progr001", "blokk-b_filer001", "blokk-b_lenke001", "blokk-b_tabell01", "blokk-b_kontakt1"]   # topp-bildet står øverst
    for ident in ider:
        assert re.search(r'id="%s"[^>]*aria-labelledby="bt-%s"' % (ident, ident[6:]), html), ident
        assert f'id="bt-{ident[6:]}"' in html
    meny = html.split('aria-labelledby="dp-meny-h"')[1].split("</nav>")[0]
    chips = html.split('aria-label="Hopp til"')[1].split("</nav>")[0]
    assert re.findall(r'href="#(blokk-[^"]+)"', meny) == ider == re.findall(r'href="#(blokk-[^"]+)"', chips)
    assert html.count('class="dp-ikon dp-ikon-') - html.count('class="dp-ikon dp-ikon-person') == 6        # viktig-blokken har eget ikon (dp-viktig-ikon); «Mine opplysninger» er ikke en blokk
    assert 'class="dp-viktig dp-viktig-advarsel"' in html and 'role="note"' in html


def test_programmet_har_details_per_dag_med_dagens_dag_apen_og_pauser_dempet(con, kid):
    _fyll(con, kid)
    html = _side(con, kid)
    assert html.count('<details class="dp-pr-dag') == 2
    assert html.count('class="dp-pr-rad dp-pause"') == 2 and 'class="dp-idag-merke">I dag<' in html
    idag = html.split('<details class="dp-pr-dag dp-i-dag" open>')[1].split("</details>")[0]
    assert "Innledning" in idag and "09:00–10:00" in idag and "Rom 3" in idag


def test_filer_har_filtypeflis_tittel_meta_og_nedlastingsknapp_med_navn(con, kid):
    _fyll(con, kid)
    html = _side(con, kid)
    assert 'class="dp-filtype dp-filtype-pdf"' in html and ">PDF<" in html
    assert re.search(r'<a class="knapp sekundar dp-last-ned" href="/kurs/K1/deltakerside/fil/\d+" aria-label="Last ned Dag 1 – Innledning">', html)
    assert "Last ned til din egen enhet" in html and 'class="dp-tk">Last ned<' in html


def test_lenker_grupperes_og_apnes_trygt_i_ny_fane(con, kid):
    _fyll(con, kid)
    html = _side(con, kid)
    assert html.index("Obligatorisk") < html.index("Kompendium") < html.index("Anbefalt") < html.index("Artikkel")
    for a in re.findall(r'<a class="dp-lenke"[^>]*>', html):
        assert 'target="_blank"' in a and 'rel="noopener noreferrer"' in a
    assert "bibliotek.example.no" in html


def test_tabellen_har_th_scope_og_etiketter_til_mobilvisningen_og_kontakt_har_tel_og_mailto(con, kid):
    _fyll(con, kid)
    html = _side(con, kid)
    assert '<th scope="col">Gruppe</th>' in html and 'data-etikett="Deltakere"' in html
    assert 'href="tel:+4755555501"' in html and 'href="mailto:marte@example.no"' in html and ">MS<" in html


def test_toppbildet_har_alt_tekst_og_bildetekst(con, kid):
    _fyll(con, kid)
    html = _side(con, kid)
    assert re.search(r'<figure class="dp-toppbilde"><img src="/kurs/K1/deltakerside/fil/\d+" alt="Kursrommet med stoler i sirkel">', html)
    assert "<figcaption>Kursrommet</figcaption>" in html
    for img in re.findall(r"<img\b[^>]*>", html):
        assert ' alt="' in img


def test_tom_side_viser_vennlig_tekst_og_ingen_hopp_til(con, kid):
    skriv_side(con, kid, dokument(tekstblokk("Skjult", skjult=True)))
    html = _side(con, kid)
    assert "Min side er åpnet, men det er ikke lagt ut noe mer ennå." in html and 'class="dp-chips"' not in html and 'class="dp-meny"' not in html


def test_kursdager_viser_status_mott_ikke_registrert_og_kommer_og_dagsrekken_har_skjult_tekst(con, kid):
    _fyll(con, kid)
    did, pid = lag_deltaker(con, kid, "kari@example.no")
    kursdag = con.execute("SELECT id FROM kursdag WHERE kurs_id=? ORDER BY dato LIMIT 1", (kid,)).fetchone()[0]
    con.execute("INSERT INTO oppmote (paamelding_id, kursdag_id, kilde) VALUES (?,?,?)", (pid, kursdag, "kode"))
    con.commit()
    html = deltaker_klient(did).get("/kurs/K1/deltakerside").get_data(as_text=True)
    assert 'class="merke ok">Møtt<' in html and 'class="merke gra">Kommer<' in html
    assert '<ol class="dp-kompakt" aria-label="Kursdager">' in html
    assert "Dag 1, ons 10. mars, møtt" in html and "Dag 2, tor 11. mars" in html
    assert html.count('class="dp-b"') == 3


# ============================ «I dag»-kortet ============================

def _vis(con, kid, tilpass=None, *, forhandsvisning=False) -> str:
    """Rendrer deltakersiden med en tilpasset malkontekst (så innsjekk-tilstandene kan prøves uten å vente på innsjekk-ruten)."""
    from kurs.web import app as webapp
    skriv_side(con, kid, dokument(tekstblokk()))
    did, _pid = lag_deltaker(con, kid, "kari@example.no")
    kurs = deltakerside.hent_kurs_for_side(con, "K1")
    side = sidelager.hent(con, kid)
    from kurs import sideinnhold as si
    v = deltakerside.bygg_visning(con, kurs, si.les(side["publisert"]), deltaker_id=None if forhandsvisning else did, idag=IDAG, forhandsvisning=forhandsvisning,
                                  fil_url=lambda i: f"/f/{i}")
    v["min_side_url"] = "/min-side"
    if tilpass:
        tilpass(v)
    with webapp.app.test_request_context("/kurs/K1/deltakerside"):
        webapp.app.preprocess_request()
        return render_template("kursside_deltaker.html", v=v, forhandsvisning=forhandsvisning)


def test_uten_oppmote_url_vises_bare_en_kort_forklaring_og_ingen_skjema(con, kid):
    html = _vis(con, kid)
    kort = html.split('id="i-dag"')[1].split("</section>")[0]
    assert "<form" not in kort and "Skann QR-koden i kurslokalet når du kommer." in kort


def test_med_oppmote_url_vises_skjema_med_csrf_kodefelt_og_knapp(con, kid):
    def sett(v):
        v["oppmote_url"] = "/kurs/K1/deltakerside/oppmote"
    html = _vis(con, kid, sett)
    skjema = re.search(r'<form class="dp-innsjekk" method="post" action="/kurs/K1/deltakerside/oppmote">(.*?)</form>', html, re.S).group(1)
    assert 'name="csrf_token"' in skjema and 'name="retur" value="kursside"' in skjema
    assert '<label for="f-kode">Dagens kode</label>' in skjema and 'id="f-kode" name="kode"' in skjema
    assert 'maxlength="8"' in skjema and 'autocomplete="off"' in skjema and 'autocapitalize="characters"' in skjema and "required" in skjema
    assert "Registrer oppmøte" in skjema and "Koden står på skjermen i kurslokalet. Du kan registrere deg én gang per kursdag." in skjema
    assert "innsjekk_kode" not in html and "innsjekk_token" not in html


def test_uten_krav_om_kode_har_skjemaet_bare_knappen(con, kid):
    def sett(v):
        v["oppmote_url"] = "/x"
        v["i_dag"]["krever_kode"] = False
    html = _vis(con, kid, sett)
    skjema = re.search(r'<form class="dp-innsjekk".*?</form>', html, re.S).group(0)
    assert 'name="kode"' not in skjema and "Registrer oppmøte" in skjema


def test_registrert_oppmote_viser_hake_og_klokkeslett_uten_skjema(con, kid):
    def sett(v):
        v["oppmote_url"] = "/x"
        v["i_dag"]["registrert"] = "08:47"
    html = _vis(con, kid, sett)
    kort = html.split('id="i-dag"')[1].split("</section>")[0]
    assert "Oppmøte registrert kl. 08:47" in kort and "<form" not in kort and "dp-idag-ok" in html


def test_digitalt_kurs_med_zoom_viser_forklaring_i_stedet_for_skjema(con, kid):
    def sett(v):
        v["oppmote_url"] = "/x"
        v["i_dag"]["tilbys"] = False
        v["i_dag"]["forklaring"] = "Oppmøte på digitale kurs registreres automatisk fra Zoom dagen etter."
    html = _vis(con, kid, sett)
    kort = html.split('id="i-dag"')[1].split("</section>")[0]
    assert "registreres automatisk fra Zoom dagen etter" in kort and "<form" not in kort


def test_forhandsvisningen_har_ingen_innsjekk_ingen_meny_og_ingen_skript_men_med_toppfelt(con, kid):
    def sett(v):
        v["oppmote_url"] = "/x"
    html = _vis(con, kid, sett, forhandsvisning=True)
    assert "Innsjekk er avslått i forhåndsvisningen." in html and "<form" not in html
    assert 'class="dp-side dp-forhandsvisning"' in html and 'class="dp-forh" role="note"' in html and '<header class="dp-hero">' in html
    assert "kursside-delt.js" not in html and "data-skriv-ut" not in html and 'class="dp-brod"' not in html and "Faktura, kursbevis" not in html
    assert "Møtt" not in html and "Ikke registrert" not in html                                      # ingen personlig status i forhåndsvisningen


def test_neste_kursdag_og_avsluttet_kurs_har_egne_kort(con):
    kid = lag_kurs(con, "K1", start=IDAG + timedelta(days=5), dager=2)
    html = _vis(con, kid)
    assert "Neste kursdag" in html and 'id="i-dag"' not in html
    avsluttet = lag_kurs(con, "K2", start=IDAG - timedelta(days=10), dager=2)
    did, _ = lag_deltaker(con, avsluttet, "vera@example.no")
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (avsluttet,))
    con.commit()
    skriv_side(con, avsluttet, dokument(tekstblokk()))
    html = deltaker_klient(did).get("/kurs/K2/deltakerside").get_data(as_text=True)
    assert "Kurset er avsluttet" in html and "Siden er åpen til " in html and "Dag 1 av" not in html


# ============================ escaping, CSP og tilgjengelighet ============================

def test_all_tekst_fra_administrator_escapes_paa_deltakersiden(con):
    kid = lag_kurs(con, "K1")
    con.execute("UPDATE kurs SET navn=?, sted=? WHERE id=?", (ANGREP, ANGREP, kid))
    con.commit()
    skriv_side(con, kid, dokument(
        {"id": "b_a0000001", "type": "viktig", "tittel": ANGREP, "data": {"niva": "info", "tekst": ANGREP}},
        {"id": "b_a0000002", "type": "program", "tittel": ANGREP, "data": {"dager": [{"kursdag_id": None, "dato": IDAG.isoformat(), "tittel": ANGREP,
                                                                                        "punkter": [{"fra": "", "til": "", "tema": ANGREP, "sted": ANGREP, "hvem": ANGREP, "pause": False}]}]}},
        {"id": "b_a0000003", "type": "tabell", "tittel": "T", "data": {"kolonner": [ANGREP], "rader": [[ANGREP]]}},
        {"id": "b_a0000004", "type": "kontakt", "tittel": "K", "data": {"personer": [{"navn": ANGREP, "rolle": ANGREP, "telefon": "", "epost": ""}]}},
        {"id": "b_a0000005", "type": "lenker", "tittel": "L", "data": {"lenker": [{"tittel": ANGREP, "url": "https://example.com", "tekst": ANGREP, "nivaa": None, "kilde": None}]}},
        tittel=ANGREP, ingress=ANGREP, rom=ANGREP, melding={"tekst": ANGREP, "niva": "viktig"}))
    html = _side(con, kid)
    assert "<script>alert" not in html and "<img src=x" not in html
    assert not re.search(r"<(script|img)\b[^>]*(alert|onerror)", html)
    assert html.count("&lt;script&gt;alert(1)&lt;/script&gt;") >= 8


def test_ingen_inline_stil_handlere_eller_javascript_adresser_og_skript_har_nonce(con, kid):
    _fyll(con, kid)
    html = _side(con, kid)
    assert not re.search(r"\sstyle\s*=", html.replace('<svg class="ik-sprite"', "")), "style-attributt"
    assert not re.search(r"\son[a-z]+\s*=", html) and "javascript:" not in html.lower()
    for skript in re.findall(r"<script\b[^>]*>", html):
        assert "nonce=" in skript
    assert "kursside-delt.js" in html and "kursside-delt.css" in html
    for fil in ("kursside_deltaker.html", "_kursside_blokker.html", "_kursside_ikoner.html", "kursside_utilgjengelig.html"):
        tekst = _les(MALER / fil)
        assert not re.search(r"\sstyle\s*=", tekst.replace('<svg class="ik-sprite"', "")), fil


def test_alle_ikoner_er_definert_i_sprite_en_og_sprite_en_ligger_bare_en_gang(con, kid):
    _fyll(con, kid)
    html = _side(con, kid)
    symboler = set(re.findall(r'<symbol id="i-([a-z-]+)"', html))
    assert html.count('<svg class="ik-sprite"') == 1
    brukt = set(re.findall(r'<use href="#i-([a-z-]+)"', html))
    assert brukt and brukt <= symboler, brukt - symboler


def test_dekorative_ikoner_er_skjult_for_skjermlesere_og_meningsbaerende_felt_har_navn(con, kid):
    _fyll(con, kid)
    html = _side(con, kid)
    for svg in re.findall(r"<svg\b(?![^>]*ik-sprite)[^>]*>", html):
        assert 'aria-hidden="true"' in svg, svg
    assert 'class="dp-filtype dp-filtype-pdf" aria-hidden="true"' in html                 # flisen gjentar bare det tittelen og meta-linjen allerede sier
    for skjema_felt in re.findall(r"<(?:input|select|textarea)\b[^>]*>", html):
        assert 'type="hidden"' in skjema_felt or "aria-label=" in skjema_felt or "id=" in skjema_felt


def test_ikke_tilgang_siden_bruker_klasser_og_har_ingen_stil_i_html(con, kid):
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    r = deltaker_klient(did).get("/kurs/K1/deltakerside")
    html = r.get_data(as_text=True)
    assert r.status_code == 404 and 'class="kort dp-utilgjengelig"' in html and "kursside-delt.css" in html
    assert not re.search(r"\sstyle\s*=", html.split("<main", 1)[1])


def test_skriv_ut_knappen_finnes_for_deltakere_og_er_en_vanlig_knapp_uten_handler(con, kid):
    _fyll(con, kid)
    html = _side(con, kid)
    assert re.search(r'<button type="button" class="dp-skriv-ut lenkeknapp" data-skriv-ut>', html) and "Skriv ut siden" in html


# ============================ forhåndsvisningen i redigeringsvisningen ============================

def test_forhandsvisningsruten_viser_toppfeltet_og_er_den_eneste_som_kan_rammes(con, kid):
    _fyll(con, kid)
    admin = admin_klient()
    r = admin.get(f"/admin/kurs/{kid}/kursside/forhandsvis?versjon=publisert")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and '<header class="dp-hero dp-hero-bilde">' in html and "Kurssiden vår" in html and "Slik ser deltakerne siden nå" in html
    assert r.headers["X-Frame-Options"] == "SAMEORIGIN" and "frame-ancestors 'self'" in r.headers["Content-Security-Policy"]
    assert 'class="dp-side dp-forhandsvisning"' in html and "kursside-delt.js" not in html


# ============================ kildekontroll av CSS ============================

def test_css_bruker_containerspoerringer_med_de_riktige_bruddene_og_media_bare_til_marg_og_utskrift():
    css = _les(CSS)
    assert "container-type: inline-size" in css and "container-name: dp" in css
    assert "@container dp (min-width: 880px)" in css and "@container dp (max-width: 720px)" in css
    rent = _uten_kommentarer(css)
    media = re.findall(r"@media\s*([^{]+)\{", rent)
    assert sorted(m.strip() for m in media) == sorted(["(max-width: 600px)", "print", "(prefers-reduced-motion: no-preference)", "(prefers-reduced-motion: reduce)"]), media
    assert ":root" not in rent and "@import" not in rent and "url(" not in rent and not re.search(r"https?://", rent)
    assert ".dp-rot {" in rent and "--dp-radius" in rent


def test_forhandsvisningen_skjuler_bare_sidens_meny_ikke_toppfeltet():
    """Regresjon: toppfeltet er et <header>-element, og en regel mot alle <header> skjulte det i forhåndsvisningen og ved utskrift."""
    rent = _uten_kommentarer(_les(CSS))
    assert "body.dp-forhandsvisning > header" in rent and not re.search(r"body\.dp-forhandsvisning header", rent)
    trykk = rent[rent.index("@media print"):]
    assert "header.dp-hero { display:block !important;" in trykk


def test_utskriftsstilen_skjuler_alt_unntatt_toppfelt_og_blokker():
    trykk = _uten_kommentarer(_les(CSS))
    trykk = trykk[trykk.index("@media print"):]
    skjulregel = next(r for r in re.findall(r"([^{}]+)\{([^}]*)\}", trykk) if "display:none !important" in r[1])[0]
    for skjult in (".dp-brod", ".dp-forh", ".dp-aside", ".dp-chips", ".dp-last-ned", ".dp-skriv-ut"):
        assert skjult in skjulregel, skjult
    assert "display:none !important" in trykk and ".dp-grid { display:block; }" in trykk and "break-inside:avoid" in trykk


def test_alle_klasser_har_prefiks_dp_eller_er_gjenbrukt_fra_base_og_variablene_er_definert():
    rent = _uten_kommentarer(_les(CSS))
    klasser = set(re.findall(r"\.([a-zA-Z][\w-]*)", rent))
    gjenbrukt = {"ik", "ik-sprite", "kort", "knapp", "sekundar", "merke", "ok", "gul", "gra", "lenkeknapp", "skjul-visuelt", "demo-banner", "hopp"}
    assert sorted(k for k in klasser if not k.startswith("dp-") and k not in gjenbrukt) == []
    definert = set(re.findall(r"(--[a-z0-9-]+)\s*:", rent)) | set(re.findall(r"(--[a-z0-9-]+)\s*:", _les(MALER / "base.html")))
    assert set(re.findall(r"var\((--[a-z0-9-]+)", rent)) <= definert


# ============================ kontrast (WCAG) ============================

def _lum(hex_: str) -> float:
    r, g, b = (int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _kontrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# (forgrunn, bakgrunn, minst): tekst 4,5:1, stor tekst og ikoner 3:1. Fargene er de som står i kursside-delt.css og base.html.
KONTRASTPAR = [
    ("#ffffff", "#123f50", 4.5), ("#ffffff", "#1f5c73", 4.5), ("#ffffff", "#276e86", 4.5),           # hero: hvit tekst på gradientens tre punkter
    ("#eaf3f6", "#123f50", 4.5), ("#eaf3f6", "#276e86", 4.5),                                        # hero: ingress og meta på gradientens lyseste punkt
    ("#1f6a43", "#dff3e7", 4.5), ("#6d4c00", "#fbf1d9", 4.5), ("#8a1d15", "#fbe7e5", 4.5),          # ok-merke, viktig og advarsel
    ("#123f50", "#e7f0f3", 4.5), ("#6d4c00", "#fbf1d9", 4.5),                                        # blå melding, forhåndsvisningsbanner
    ("#1f5c73", "#e7f0f3", 4.5), ("#1d2a33", "#ffffff", 4.5), ("#5d6b75", "#ffffff", 4.5),          # lenker, brødtekst, dempet tekst på hvitt
    ("#5d6b75", "#f7f8f7", 4.5), ("#5d6b75", "#e7f0f3", 4.5), ("#174a5d", "#e7f0f3", 4.5),
    ("#8f3f0c", "#fbe9dc", 4.5), ("#b3261e", "#fbe7e5", 4.5), ("#1f5c73", "#e7f0f3", 4.5), ("#2e7d4f", "#e5f3ea", 4.4),   # filtypeflisene (11 px fet: 4,4 for XLS er kravet i base.html)
    ("#4b5a63", "#eceff1", 4.5),                                                                     # ZIP/TXT/FIL-fliser og «Kommer»
    ("#55428f", "#ece9f6", 4.5), ("#26704d", "#e1f1ea", 4.5), ("#96570f", "#fbeede", 4.5), ("#2b58a0", "#e4edf9", 4.5), ("#9b2f5c", "#f8e6ee", 4.5),
    ("#465661", "#e9edf0", 4.5),                                                                     # ikonfliser per blokktype (ikoner: 3:1 er nok, men tallet holder 4,5)
    ("#ffffff", "#1f5c73", 4.5),                                                                     # «I dag»-merke, valgt hopp-til-knapp, knapper
    ("#1f6a43", "#f5fbf7", 4.5),                                                                     # «Oppmøte registrert»
    ("#6b7a84", "#ffffff", 3.0), ("#77868f", "#ffffff", 3.0),                                        # plassholdere (ikke innhold)
]


@pytest.mark.parametrize("forgrunn,bakgrunn,minst", KONTRASTPAR)
def test_kontrasten_holder_wcag(forgrunn, bakgrunn, minst):
    assert _kontrast(forgrunn, bakgrunn) >= minst, (forgrunn, bakgrunn, round(_kontrast(forgrunn, bakgrunn), 2))


def test_fargene_i_kontrasttabellen_star_i_css_en_slik_at_testen_ikke_blir_foreldet():
    css = (_les(CSS) + _les(MALER / "base.html")).lower()
    for forgrunn, bakgrunn, _minst in KONTRASTPAR:
        if (forgrunn, bakgrunn) in [("#6b7a84", "#ffffff"), ("#77868f", "#ffffff")]:
            continue
        for farge in (forgrunn, bakgrunn):
            assert farge in css or farge in ("#ffffff",), farge


# ============================ kildekontroll av JavaScript ============================

def test_deltaker_javascript_er_liten_uten_farlige_api_er_og_apner_alle_dager_ved_utskrift():
    kilde = _les(JS)
    for forbudt in (r"\.innerHTML\s*=", r"outerHTML", r"insertAdjacentHTML", r"document\.write", r"\beval\s*\(", r"new\s+Function", r"setTimeout\s*\(\s*[\"']",
                    r"fetch\(", r"XMLHttpRequest", r"localStorage", r"\.style\.", r"cssText"):
        assert not re.search(forbudt, kilde), forbudt
    assert '"use strict"' in kilde and "beforeprint" in kilde and "afterprint" in kilde and "details.dp-pr-dag" in kilde
    assert "data-skriv-ut" in kilde and "window.print()" in kilde and "aria-current" in kilde
    assert len(kilde.split("\n")) < 80
