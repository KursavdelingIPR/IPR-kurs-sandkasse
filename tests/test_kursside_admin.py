"""Redigeringsvisningen for kurssiden (kursside_admin.html + _kursside_admin_*.html, static/kursside-admin*.js og kursside-admin.css): markup, faste tekster,
tilgjengelighet, escaping av tekst fra databasen, skrivebeskyttet visning og kildekontroller av JavaScript og CSS (CSP-reglene i kurs/web/sikkerhet.py).

Selve oppførselen i nettleseren (redigere, flytte, laste opp, angre, konflikt, publisere ...) er prøvd med scenarioer i headless Edge (se BYGG-2.md);
her testes alt som lar seg teste uten nettleser. Rutene og JSON-svarene har egne tester i test_kursside_admin_ruter.py. Bare oppdiktede data.
"""
import json
import re
from pathlib import Path

import pytest

from kurs import sideinnhold as si

from kurssidehjelp import admin_klient, dokument, fast_dato, lag_deltaker, lag_kurs, ny_database, skriv_side, tekstblokk

KODE = Path(__file__).resolve().parent.parent / "kurs"
MALER = KODE / "web" / "templates"
STATIC = KODE / "web" / "static"
ADMIN_MALER = [MALER / "kursside_admin.html", MALER / "_kursside_admin_maler.html", MALER / "_kursside_admin_dialoger.html"]
JS_FILER = sorted(STATIC.glob("kursside-admin*.js"))
CSS = STATIC / "kursside-admin.css"


def _les(sti: Path) -> str:
    return sti.read_bytes().decode("utf-8")


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    fast_dato(monkeypatch)
    yield c
    c.close()


@pytest.fixture
def kid(con):
    return lag_kurs(con, "K1")


@pytest.fixture
def admin(con):
    return admin_klient()


def _side(admin, kid) -> str:
    r = admin.get(f"/admin/kurs/{kid}/kursside")
    assert r.status_code == 200
    return r.get_data(as_text=True)


# ============================ fast innhold i siden ============================

def test_siden_har_alle_delene_og_testkrokene(con, kid, admin):
    html = _side(admin, kid)
    krok = set(re.findall(r'data-ks="([^"]+)"', html))
    forventet = {"lagre-status", "status", "blokkliste", "blokk", "legg-til", "tittel", "fold", "meny", "dupliser", "opp", "ned", "rask", "velg", "handlingsstolpe",
                 "angre", "gjor-om", "forhandsvis", "publiser", "publiser-nå", "filslipp", "filrad", "melding", "hent-fra", "mal", "finn", "versjoner",
                 "innstillinger", "kopier-direkte", "kopier-generell", "toast-omrade", "forh", "forh-mobil", "forh-pc", "forh-dag"}
    # «slett» og «toast» settes av JavaScript (menypost og melding), så de står ikke i malen
    assert forventet - krok == set(), forventet - krok
    assert len(re.findall(r'data-ks="legg-til"', html)) == 8


def test_alle_dialoger_og_alle_skjelett_finnes_og_dialogene_har_overskrift(con, kid, admin):
    html = _side(admin, kid)
    for navn in ("publiser", "konflikt", "lenke", "tekst", "excel", "navn", "mal", "hent", "versjoner", "innstillinger", "finn", "filer"):
        assert f'<dialog id="ks-dialog-{navn}"' in html, navn
    for d in re.finditer(r'<dialog id="([^"]+)"[^>]*aria-labelledby="([^"]+)"', html):
        assert f'id="{d.group(2)}"' in html, d.group(1)
    assert len(re.findall(r"<dialog\b", html)) == len(re.findall(r'<dialog id="[^"]+"[^>]*aria-labelledby=', html))
    for mal in ("blokk", "synlighet", "tekst", "viktig", "program", "program-dag", "program-rad", "filer", "fil-rad", "fil-opplaster", "lenker", "lenke-rad",
                "tabell", "kontakt", "person", "bilde"):
        assert f'<template id="ks-mal-{mal}">' in html, mal


def test_faste_tekster_fra_spesifikasjonen_star_i_siden(con, kid, admin):
    html = _side(admin, kid)
    for tekst in ("siden deltakerne ser etter at de har logget inn", "Direkte til Min side for dette kurset", "Innlogging for alle kurs", "Kopier lenke",
                  "Legg lenken på terapiakademiet.no", "Sidens topp", "Melding øverst", "Bruk den til det som endrer seg fra dag til dag",
                  "Rask omorganisering", "Hent fra annet kurs", "Start fra mal", "Finn og erstatt", "Tidligere versjoner",
                  "Ikke skriv navn, e-postadresser eller helseopplysninger om deltakere. Alle på kurset ser siden.", "Legg til blokk",
                  "Tekst med fet, kursiv, lister og lenker", "Gruppeinndeling eller annen tabell", "Slik ser deltakerne det", "Oppdateres etter hver lagring",
                  "Utkastet lagres automatisk. Deltakerne ser ikke noe før du publiserer.", "flytter blokken", "lagrer nå", "angrer",
                  "Lim inn fra Word: formateringen ryddes", "Overskrifter skriver du i tittelfeltet over",
                  "Datoene kobles til kursdagene, så «I dag» markeres automatisk for deltakerne.", "Hent dager fra kursdatoene", "Kopier fra dag 1",
                  "Slipp filene her", "Vis også filene kursholder har lastet opp i kursmappen", "Vis «Kommer …» for filer som ikke er synlige ennå",
                  "Bare vanlige nettadresser (https)", "Sett inn kursets Zoom-lenke", "Lim inn fra Excel", "Legg til deltakere fra påmeldingslisten",
                  "Navn vises for alle som er påmeldt kurset. Bruk fornavn og forbokstav. Ikke e-post eller telefon. Tabellen tømmes automatisk 30 dager etter kurset.",
                  "Bare arbeidsopplysninger", "Alternativ tekst", "Toppbilde bak tittelen",
                  "Publisere Min side?", "Dette endres", "Kontroll", "Deltakerne varsles ikke automatisk. Vil du gi beskjed, bruk fanen Kommunikasjon.",
                  "Se som deltaker", "Publiser nå", "Behold mine endringer", "Hent deres versjon", "Last ned min kopi",
                  "Utkastet ditt erstattes. Vi tar en sikkerhetskopi først, du finner den under Tidligere versjoner.",
                  "Filer kopieres. Tabeller kommer uten navn. Programdager kobles til dette kursets kursdager i rekkefølge. Blokkene legges sist.",
                  "Siden er åpen for deltakerne", "Åpen til", "Innsjekk på nett krever dagens kode", "Ikke skill store og små bokstaver"):
        assert tekst in html, tekst
    assert "Gjenopprett som utkast" in _les(STATIC / "kursside-admin-dialoger.js")           # knappen lages av JavaScript for hver rad i versjonslisten


def test_maxlength_kommer_fra_sideinnhold_og_ingen_verdi_er_tom(con, kid, admin):
    html = _side(admin, kid)
    assert 'maxlength=""' not in html and 'maxlength="None"' not in html and ">None<" not in html
    for nokkel in ("tittel", "ingress", "melding", "tema", "viktig", "sted_hvem", "url", "lenke_tekst", "navn_rolle", "alt", "bildetekst", "dagtittel"):
        assert f'maxlength="{si.GRENSER[nokkel]}"' in html, nokkel
    assert "{{" not in html and "{%" not in html
    # hver grense malene bruker finnes i sideinnhold.GRENSER (en manglende nøkkel gir stille tom maxlength i Jinja)
    for fil in ADMIN_MALER:
        for nokkel in set(re.findall(r"\bg\.([a-z_]+)", _les(fil))) | set(re.findall(r"data\.grenser\.([a-z_]+)", _les(fil))):
            assert nokkel in si.GRENSER or nokkel in ("fil_mb", "bilde_mb", "kurs_mb", "filer", "endelser"), (fil.name, nokkel)


def test_lenkeboksen_har_begge_innloggingslenkene_og_kopieringsknapper(con, kid, admin):
    html = _side(admin, kid)
    assert 'value="http' in html and "/logg-inn?neste=/kurs/K1/deltakerside" in html
    assert html.count("data-kopier=") == 2 and 'id="ks-lenke-direkte"' in html and 'id="ks-lenke-generell"' in html


def test_siden_har_live_omraade_og_bannere_for_skjermlesere(con, kid, admin):
    html = _side(admin, kid)
    assert 'id="ks-live"' in html and 'aria-live="polite"' in html
    assert 'id="ks-bannere"' in html and 'role="region" aria-label="Status og handlinger"' in html


def test_forhandsvisningen_er_en_sandkasse_uten_skript_og_har_tittel(con, kid, admin):
    html = _side(admin, kid)
    ramme = re.search(r"<iframe[^>]*>", html).group(0)
    assert 'sandbox="allow-same-origin"' in ramme and 'title="Forhåndsvisning av Min side"' in ramme and "allow-scripts" not in ramme
    assert f"/admin/kurs/{kid}/kursside/forhandsvis?versjon=utkast" in ramme


def test_skript_har_nonce_ingen_inline_handlere_og_alle_fem_js_filer_lastes_i_rekkefolge(con, kid, admin):
    html = _side(admin, kid)
    for skript in re.findall(r"<script\b[^>]*>", html):
        assert "nonce=" in skript
    assert not re.search(r"\son(click|change|submit|load|input|keyup|keydown|pointerdown)=", html) and "javascript:" not in html.lower()
    rekkefolge = [html.index(f"static/{navn}") for navn in ("kursside-admin.js", "kursside-admin-liste.js", "kursside-admin-blokker.js", "kursside-admin-filer.js",
                                                              "kursside-admin-dialoger.js")]
    assert rekkefolge == sorted(rekkefolge)
    assert "kursside-admin.css" in html and " defer" in html


def test_ingen_style_attributt_og_ingen_inline_javascript_i_malene(con, kid, admin):
    for fil in ADMIN_MALER + [MALER / "_kursside_ikoner.html"]:
        tekst = _les(fil)
        assert not re.search(r"\sstyle\s*=", tekst), fil.name
        assert not re.search(r"\son[a-z]+\s*=\s*[\"']", tekst), fil.name
    html = _side(admin, kid)
    assert not re.search(r"\sstyle\s*=", html.split("</header>", 1)[-1].split('<script type="application/json"')[0].replace('style="position:absolute"', "")), "style-attributt i siden"


# ============================ ikoner og skjelett som JavaScript-en bruker ============================

def test_alle_ikoner_som_brukes_finnes_i_sprite_en(con, kid, admin):
    symboler = set(re.findall(r'<symbol id="i-([a-z-]+)"', _les(MALER / "_kursside_ikoner.html")))
    brukt = set()
    for fil in ADMIN_MALER + [MALER / "kursside_deltaker.html", MALER / "_kursside_blokker.html"]:
        brukt |= set(re.findall(r"ikon\(\s*['\"]([a-z-]+)['\"]", _les(fil)))
        brukt |= set(re.findall(r"href=\"#i-([a-z-]+)\"", _les(fil)))
        brukt |= set(re.findall(r"'(\w[\w-]*)'\)\]", _les(fil)))
    for fil in JS_FILER:
        kilde = _les(fil)
        brukt |= set(re.findall(r"KS\.ikon\(\s*\"([a-z-]+)\"", kilde)) | set(re.findall(r"ikon:\s*\"([a-z-]+)\"", kilde))
    kjerne = _les(STATIC / "kursside-admin.js")
    for kart in re.findall(r"blokkIkon = \{([^}]*)\}", kjerne):
        brukt |= set(re.findall(r":\s*\"([a-z-]+)\"", kart))
    for grunn in ("lenke", "kal", "tekst", "info"):
        assert grunn in symboler
    assert brukt - symboler == set(), sorted(brukt - symboler)
    html = _side(admin, kid)
    assert set(re.findall(r'<use href="#i-([a-z-]+)"', html)) <= symboler


def test_hvert_skjelett_og_hver_dialog_javascript_ber_om_finnes(con):
    maler = _les(MALER / "_kursside_admin_maler.html")
    dialoger = _les(MALER / "_kursside_admin_dialoger.html")
    kilde = "\n".join(_les(f) for f in JS_FILER)
    for navn in set(re.findall(r'KS\.klon\("(ks-mal-[a-z-]+)"\)', kilde)):
        assert f'<template id="{navn}">' in maler, navn
    navn_dialoger = set(re.findall(r'dlg\("([a-z-]+)"\)', kilde))
    for liste in re.findall(r"\[((?:\"[a-z]+\",?\s*)+)\]\.forEach\(function \(n\) \{ forbered", kilde):
        navn_dialoger |= set(re.findall(r'"([a-z]+)"', liste))
    assert navn_dialoger, "fant ingen dialoger i JavaScript"
    for navn in navn_dialoger:
        assert f'<dialog id="ks-dialog-{navn}"' in dialoger, navn


def test_alle_data_felt_som_er_bundet_i_javascript_finnes_i_skjelettene(con):
    maler = _les(MALER / "_kursside_admin_maler.html")
    felt_i_maler = set(re.findall(r'data-felt="([^"]+)"', maler))
    bundet = set()
    for m in re.finditer(r"bind\([^,]+,[^,]+,\s*\[([^\]]*)\]", "\n".join(_les(f) for f in JS_FILER)):
        bundet |= set(re.findall(r'"([a-z_]+)"', m.group(1)))
    assert bundet - felt_i_maler == set(), sorted(bundet - felt_i_maler)
    for navn in set(re.findall(r'KS\.felt\("([a-z_-]+)",\s*(?:r|kort|el|rad|d|innholdEl|kropp)\b', "\n".join(_les(f) for f in JS_FILER))):
        assert navn in felt_i_maler or navn in _les(MALER / "_kursside_admin_dialoger.html") or navn in _les(MALER / "kursside_admin.html"), navn


def test_hvert_kall_til_kursside_api_er_definert_et_sted(con):
    """Fanger skrivefeil mellom de fem JavaScript-filene: hver KS.navn(...) som kalles må være tilordnet (KS.navn = ...) i en av dem."""
    kilde = "\n".join(_les(f) for f in JS_FILER)
    kalt = set(re.findall(r"\bKS\.([A-Za-z_]\w*)\s*\(", kilde))
    definert = set(re.findall(r"\bKS\.([A-Za-z_]\w*)\s*=", kilde))
    assert kalt - definert == set(), sorted(kalt - definert)


# ============================ tilgjengelighet i selve siden ============================

def test_alle_felt_i_den_ferdige_siden_har_ledetekst_og_tabellene_har_scope(con, kid, admin):
    html = _side(admin, kid)
    for felt in re.finditer(r"<(input|select|textarea)\b([^>]*)>", html):
        attrs = felt.group(2)
        if 'type="hidden"' in attrs:
            continue
        ident = re.search(r'\bid="([^"]+)"', attrs)
        foran = html[:felt.start()]
        assert ("aria-label=" in attrs or (ident and re.search(r'<label\b[^>]*\bfor="%s"' % re.escape(ident.group(1)), html))
                or foran.rfind("<label") > foran.rfind("</label>")), felt.group(0)
    for th in re.findall(r"<th(?:\s[^>]*)?>", html):
        assert "scope=" in th, th
    # knapper med bare et ikon har navn
    for knapp in re.finditer(r"<button\b([^>]*)>(.*?)</button>", html, re.S):
        tekst = re.sub(r"<[^>]+>", "", knapp.group(2)).strip()
        assert tekst or "aria-label=" in knapp.group(1) or "data-felt=" in knapp.group(2), knapp.group(0)[:150]      # data-felt = teksten fylles av JavaScript


def test_rik_tekst_flaten_har_rolle_navn_og_verktoylinjen_har_knappenavn(con):
    maler = _les(MALER / "_kursside_admin_maler.html")
    assert 'contenteditable="true" role="textbox" aria-multiline="true" aria-label="Tekst"' in maler
    assert 'role="toolbar" aria-label="Tekstformatering"' in maler
    for kommando in ("bold", "italic", "insertUnorderedList", "insertOrderedList", "lenke", "unlink", "removeFormat"):
        assert re.search(r'data-kommando="%s"[^>]*aria-label="[^"]+"' % kommando, maler), kommando


def test_flyttehandtak_og_ikonknapper_har_norske_navn(con):
    maler = _les(MALER / "_kursside_admin_maler.html")
    assert 'aria-label="Flytt blokken (dra, eller bruk Alt og piltastene)"' in maler
    assert 'aria-haspopup="menu"' in maler and 'aria-label="Flere valg for blokken"' in maler
    assert maler.count("data-endrer") >= 25            # alt som endrer siden kan skjules for rollen «lese»


# ============================ skrivebeskyttet (rollen lese) ============================

def test_lesetilgang_faar_ingen_redigeringsknapper(con, kid):
    skriv_side(con, kid, dokument(tekstblokk()))
    lese = admin_klient(con, "lese")
    html = lese.get(f"/admin/kurs/{kid}/kursside").get_data(as_text=True)
    assert 'data-skrivebeskyttet="1"' in html and "Du har lesetilgang – du kan se siden, men ikke endre den." in html
    for skjult in ("publiser", "angre", "gjor-om", "legg-til", "rask", "mal", "finn", "hent-fra", "innstillinger", "handlingsstolpe", "mal-tom"):
        assert f'data-ks="{skjult}"' not in html, skjult
    for synlig in ("versjoner", "forhandsvis", "kopier-direkte", "blokkliste", "fold-alle"):
        assert f'data-ks="{synlig}"' in html, synlig
    js = _les(STATIC / "kursside-admin.js")
    assert "KS.laas = function" in js and "if (!KS.skriv)" in js       # JavaScript-en låser alle felt og starter ikke autolagring


def test_skrivetilgang_gir_publiser_og_angre(con, kid, admin):
    html = _side(admin, kid)
    assert 'data-skrivebeskyttet="0"' in html
    for knapp in ("publiser", "angre", "gjor-om", "rask", "mal", "finn", "hent-fra"):
        assert f'data-ks="{knapp}"' in html, knapp


# ============================ tekst fra databasen escapes ============================

ANGREP = '"><script>alert(1)</script><img src=x onerror=alert(2)>'


def test_tekst_fra_databasen_vises_aldri_som_html_i_redigeringssiden(con):
    kid = lag_kurs(con, "XSS")
    con.execute("UPDATE kurs SET navn=?, sted=? WHERE id=?", (ANGREP, ANGREP, kid))
    con.commit()
    dok = dokument(
        tekstblokk(ANGREP, "<p>" + ANGREP.replace("<", "&lt;") + "</p>", id="b_angrep01"),
        {"id": "b_angrep02", "type": "viktig", "tittel": ANGREP, "data": {"niva": "info", "tekst": ANGREP}},
        {"id": "b_angrep03", "type": "tabell", "tittel": "T", "data": {"kolonner": [ANGREP], "rader": [[ANGREP]]}},
        {"id": "b_angrep04", "type": "kontakt", "tittel": "K", "data": {"personer": [{"navn": ANGREP, "rolle": ANGREP, "telefon": "", "epost": ""}]}},
        {"id": "b_angrep05", "type": "lenker", "tittel": "L", "data": {"lenker": [{"tittel": ANGREP, "url": "https://example.com", "tekst": ANGREP}]}},
        tittel=ANGREP, ingress=ANGREP, rom=ANGREP, melding={"tekst": ANGREP, "niva": "info"})
    skriv_side(con, kid, dok)
    admin = admin_klient()
    html = _side(admin, kid)
    utenom_data = re.sub(r'<script type="application/json" id="ks-data".*?</script>', "", html, flags=re.S)
    assert "<script>alert" not in utenom_data and "<img src=x" not in utenom_data
    assert not re.search(r"<(script|img)\b[^>]*(alert|onerror)", utenom_data)
    rå = re.search(r'<script type="application/json" id="ks-data"[^>]*>(.*?)</script>', html, re.S).group(1)
    assert "</script" not in rå.lower() and "<script" not in rå.lower()                       # |tojson lager <, aldri < i JSON-en
    data = json.loads(rå)
    assert data["dokument"]["tittel"] == ANGREP and data["dokument"]["blokker"][0]["tittel"] == ANGREP       # dataene når JavaScript-en uskadet
    assert data["dokument"]["blokker"][2]["data"]["rader"] == [[ANGREP]]


# ============================ kildekontroll av JavaScript ============================

FORBUDT = (r"\.innerHTML\s*=", r"outerHTML", r"insertAdjacentHTML", r"document\.write", r"\beval\s*\(", r"new\s+Function", r"setTimeout\s*\(\s*[\"']",
           r"setInterval\s*\(\s*[\"']", r"\.cssText", r"setAttribute\(\s*[\"']style[\"']", r"javascript:", r"\.srcdoc\s*=", r"createContextualFragment",
           r"\bdocument\.domain\b", r"postMessage\(")


def test_de_fem_javascript_filene_bruker_ingen_farlige_api_er():
    assert [f.name for f in JS_FILER] == ["kursside-admin-blokker.js", "kursside-admin-dialoger.js", "kursside-admin-filer.js", "kursside-admin-liste.js", "kursside-admin.js"]
    for fil in JS_FILER:
        kilde = _les(fil)
        for forbudt in FORBUDT:
            assert not re.search(forbudt, kilde), (fil.name, forbudt)


def test_javascript_filene_har_ingen_tredjepartsbibliotek_ingen_eksterne_adresser_og_bruker_strict_mode():
    for fil in JS_FILER:
        kilde = _les(fil)
        assert '"use strict"' in kilde and kilde.lstrip().startswith("/*"), fil.name
        uten_kommentarer = re.sub(r"/\*.*?\*/", "", kilde, flags=re.S)
        eksterne = [a for a in re.findall(r"https?://[a-z0-9.-]+\.[a-z]{2,}", uten_kommentarer) if a != "http://www.w3.org"]      # SVG-navnerommet er en identifikator
        assert eksterne == [], (fil.name, eksterne)
        assert not re.search(r"\b(import|require)\s*\(|^\s*import\s", kilde, re.M), fil.name
        assert "XMLHttpRequest" not in kilde or fil.name == "kursside-admin-filer.js"
        assert not re.search(r"\bfetch\(\s*[\"']https?:", kilde), fil.name


def test_all_lagring_i_nettleseren_skjer_i_try_catch_og_bare_kladd_og_ingenting_annet():
    for fil in JS_FILER:
        linjer = _les(fil).split("\n")
        for i, linje in enumerate(linjer):
            if re.search(r"\b(localStorage|sessionStorage|indexedDB)\b", linje) and not linje.lstrip().startswith(("/*", "*", "//")):
                omkrets = "\n".join(linjer[max(0, i - 3):i + 1])
                assert "try" in omkrets, (fil.name, i + 1, linje.strip()[:100])
    kilde = "\n".join(_les(f) for f in JS_FILER)
    assert set(re.findall(r"(?:localStorage|sessionStorage)\.(\w+)\(", kilde)) <= {"setItem", "getItem", "removeItem"}
    assert 'kladdNokkel = "ks-kladd-" + D.kurs.id' in kilde


def test_alle_kall_mot_tjeneren_bruker_adressene_fra_siden_og_csrf_hodet():
    kilde = _les(STATIC / "kursside-admin.js") + _les(STATIC / "kursside-admin-filer.js")
    assert '"X-CSRF-Token": KS.csrf' in kilde and 'xhr.setRequestHeader("X-CSRF-Token", KS.csrf)' in kilde
    assert "X-Filnavn" in kilde and "encodeURIComponent(o.navn)" in kilde and 'credentials: "same-origin"' in kilde
    adresser = set(re.findall(r"D\.urls\.([a-z_]+)", "\n".join(_les(f) for f in JS_FILER)))
    fra_tjener = {"side", "lagre", "redigerer", "sjekk", "publiser", "aktiv", "innstillinger", "forhandsvis", "fil_opp", "fil", "fil_slett", "gjenopprett",
                  "kilder", "hent_fra", "mal", "deltakernavn", "token", "logg_inn"}
    assert adresser <= fra_tjener, adresser - fra_tjener


def test_javascript_bruker_de_tidene_spesifikasjonen_krever():
    kilde = _les(STATIC / "kursside-admin.js")
    assert "setInterval(hjerteslag, 30000)" in kilde or "window.setInterval(hjerteslag, 30000)" in kilde              # hjerteslag hvert 30. sekund
    assert re.search(r"ms === undefined \? 4000", kilde) and "26000" in kilde                                        # autolagring 4 s, senest 30 s
    assert "800" in kilde and "50" in kilde                                                                         # tastepause 800 ms, høyst 50 øyeblikksbilder
    assert "beforeunload" in kilde and "visibilitychange" in kilde and "focusout" in kilde


# ============================ kildekontroll av CSS ============================

def test_css_bruker_prefiks_ks_og_ingen_ekstern_ressurs_eller_rot_variabel():
    css = re.sub(r"/\*.*?\*/", "", _les(CSS), flags=re.S)
    assert ":root" not in css and "@import" not in css and "url(" not in css
    assert not re.search(r"https?://", css)
    klasser = set(re.findall(r"\.([a-zA-Z][\w-]*)", re.sub(r"/\*.*?\*/", "", css, flags=re.S)))
    tillatt = {"knapp", "sekundar", "fare", "liten", "faner", "lenkeknapp", "skjul-visuelt", "dempet", "valg", "valg-inline", "datofelt", "ik", "pil", "hodetekst",
               "grep", "tn", "aktiv", "pa", "ok", "i", "v", "a", "feil", "ny", "fra", "endret", "gul", "info", "over", "treff", "valgt", "apen", "skjult", "flyttes",
               "dras", "pause", "opplaster", "mobil", "pc", "hode", "plass", "ft", "mid", "hn", "pdf", "ppt", "doc", "xls", "zip", "av", "sym", "end", "bort", "fl",
               "adv", "fei", "tom", "kun-en", "har-bilde", "ks-rask", "kol-celle", "grep-celle", "slett-celle", "kol-knapper", "dras-rad", "hjelp", "ikon", "to", "to2",
               "hode", "ks-t-tekst", "sammendrag", "ik-sprite", "kompakt", "demo-banner", "kort", "merke", "bred", "ks-side-kropp", "ks-info", "ks-dialog-stor",
               "ks-dialog-liten", "ks-finn", "ks-dialog-f-kolonne", "ks-h3", "gra", "inner", "rett", "skille"}      # modifikatorer og barn under .ks-...; inner = menyen i base.html
    fremmede = sorted(k for k in klasser if not k.startswith("ks-") and k not in tillatt)
    assert fremmede == [], fremmede


def test_css_variabler_er_definert_og_brytepunktene_finnes():
    css = _les(CSS)
    definert = set(re.findall(r"(--[a-z0-9-]+)\s*:", css)) | set(re.findall(r"(--[a-z0-9-]+)\s*:", _les(MALER / "base.html")))
    brukt = set(re.findall(r"var\((--[a-z0-9-]+)", css))
    assert brukt <= definert, sorted(brukt - definert)
    assert "@media (max-width:1240px)" in css and "@media (max-width:760px)" in css
    assert "--ks-blaa-kant" in css and ".ks-rot {" in css
    assert "prefers-reduced-motion" in css and "@media print" in css


def test_css_fokusring_fjernes_aldri_uten_erstatning():
    css = re.sub(r"/\*.*?\*/", "", _les(CSS), flags=re.S)
    for regel in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
        if re.search(r"outline\s*:\s*(none|0)\b", regel.group(2)):
            assert ":focus" in regel.group(1) and ("border-color" in regel.group(2) or "box-shadow" in regel.group(2)), regel.group(0)[:160]


# ============================ ingen endringer i det som ikke hører til ============================

def test_editoren_lager_aldri_data_eller_blob_adresser_i_dokumentet():
    kilde = "\n".join(_les(f) for f in JS_FILER)
    brukere = {f.name for f in JS_FILER if "createObjectURL" in _les(f)}
    assert brukere == {"kursside-admin.js", "kursside-admin-filer.js"}        # nedlasting av kopi/kladd, og lesing av bilde som skal minskes
    assert kilde.count("revokeObjectURL") >= kilde.count("createObjectURL") >= 2
    assert not re.search(r"toDataURL|readAsDataURL|\.src\s*=\s*[\"']data:", kilde)


def test_deltakernavn_hentes_bare_naar_brukeren_ber_om_det():
    kilde = "\n".join(_les(f) for f in JS_FILER)
    assert kilde.count("D.urls.deltakernavn") == 1 and "KS.navnDialog = function" in kilde


def test_nettside_fanen_forklarer_forskjellen_paa_nettside_og_min_side(con, kid, admin):
    html = admin.get(f"/admin/kurs/{kid}/nettside").get_data(as_text=True)
    assert "Siden deltakerne ser etter innlogging (program, presentasjoner, grupper, litteratur) finner du under" in html
    assert f'href="/admin/kurs/{kid}/kursside">Min side</a>' in html
