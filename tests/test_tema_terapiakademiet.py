"""Terapiakademiet-drakten for deltakersidene (static/tema-terapiakademiet.css, `tema_ta` i base.html, kurs/tema.py). Bare oppdiktede data.

Drakten gjelder Min side, Mine kurs, innlogging, «lenken virker ikke», «ikke tilgang», innsjekk og den offentlige påmeldingen (skjemaet,
bedriftspåmeldingen og kvitteringene), og ingen andre sider: ikke admin. Alt i CSS-filen står under body[data-tema="ta"] og i @media screen (utskrift er urørt), uten eksterne ressurser, og
fargene har kontrast som på kursdagene: tekst minst 4,5:1, kanter på felt minst 3:1.
"""
import re
from pathlib import Path

import pytest

from kurs import db, tema

from kurssidehjelp import admin_klient, deltaker_klient, dokument, fast_dato, lag_deltaker, lag_kurs, ny_database, skriv_side, tekstblokk

CSS = Path(__file__).resolve().parent.parent / "kurs" / "web" / "static" / "tema-terapiakademiet.css"


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    fast_dato(monkeypatch)
    monkeypatch.setattr(tema, "LOGO_MAPPE", tmp_path / "logo")             # aldri de ekte logofilene i static/logo
    yield c
    c.close()


def _klient():
    from kurs.web import app as webapp
    return webapp.app.test_client()


def _html(r) -> str:
    return r.get_data(as_text=True)


# ============================ selve CSS-filen ============================

def _uten_kommentarer() -> str:
    return re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)


def _regler(kilde: str):
    """[(velgerliste, blokk)] for alle stilregler, også inne i @media og @container (flat gjennomgang av klammene)."""
    ut, i = [], 0
    while i < len(kilde):
        j = kilde.find("{", i)
        if j < 0:
            break
        velger = kilde[i:j].strip()
        if velger.startswith("@"):                                           # @media / @container: gå inn i blokken
            i = j + 1
            continue
        k = kilde.find("}", j)
        ut.append((velger, kilde[j + 1:k]))
        i = k + 1
        while i < len(kilde) and kilde[i] in " \r\n\t}":
            i += 1
    return ut


def test_alt_i_draktfilen_er_avgrenset_til_sidene_med_drakten_og_til_skjerm():
    kilde = _uten_kommentarer().strip()
    assert kilde.startswith("@media screen {") and kilde.endswith("}")      # utskrift er urørt
    regler = _regler(kilde)
    assert len(regler) > 150
    for velger, _ in regler:
        for v in velger.split(","):
            assert v.strip().startswith('body[data-tema="ta"]'), v           # aldri :root, aldri en vid velger som kan lekke til admin
    assert ":root" not in kilde


def test_draktfilen_har_ingen_eksterne_ressurser_og_ingen_important():
    kilde = _uten_kommentarer()
    assert not re.search(r"https?:|//|@import|url\(", kilde)
    assert "!important" not in kilde


def _hex(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _lum(rgb) -> float:
    def f(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (f(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _kontrast(a, b) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _palett() -> dict:
    forste = _regler(_uten_kommentarer())[0][1]                              # blokken med variablene
    return dict(re.findall(r"--(ta-[a-z-]+):\s*(#[0-9a-fA-F]{6})", forste))


def _over(forgrunn_hvit_andel: float, bakgrunn):
    """Hvit med gitt dekning lagt over en bakgrunn (rgba(255,255,255,.5) på et bånd)."""
    return tuple(round(255 * forgrunn_hvit_andel + c * (1 - forgrunn_hvit_andel)) for c in bakgrunn)


def test_paletten_er_hentet_fra_terapiakademiet_no():
    p = _palett()
    assert p["ta-krem"] == "#f7e5d0" and p["ta-band"] == "#f1dac1" and p["ta-plomme"] == "#562a3e"
    assert p["ta-mauve"] == "#754e5a" and p["ta-baer"] == "#8a334e"
    assert p["ta-hvit"] == "#ffffff"                                                  # bare tekst- og lenkeblokkene er hvite


@pytest.mark.parametrize("tekst, bakgrunn", [
    ("ta-plomme", "ta-krem"), ("ta-plomme", "ta-band"), ("ta-mauve", "ta-krem"), ("ta-mauve", "ta-band"),
    ("ta-baer", "ta-krem"), ("ta-baer", "ta-band"), ("ta-krem", "ta-plomme"), ("ta-etikett-pa-plomme", "ta-plomme"),
    ("ta-lenke-pa-plomme", "ta-plomme"), ("ta-ok", "ta-ok-bg"), ("ta-gul", "ta-gul-bg"), ("ta-rod", "ta-rod-bg"),
    ("ta-ok", "ta-band"), ("ta-ok", "ta-krem"), ("ta-gul", "ta-krem"), ("ta-rod", "ta-krem"), ("ta-plomme", "ta-felt-bg"),
    ("ta-mauve", "ta-felt-bg"), ("ta-krem", "ta-baer"),
    ("ta-plomme", "ta-hvit"), ("ta-mauve", "ta-hvit"), ("ta-baer", "ta-hvit"), ("ta-ok", "ta-hvit"), ("ta-gul", "ta-hvit"), ("ta-rod", "ta-hvit"),
])
def test_teksten_har_minst_4_5_til_1(tekst, bakgrunn):
    p = _palett()
    assert _kontrast(_hex(p[tekst]), _hex(p[bakgrunn])) >= 4.5, (tekst, bakgrunn, p[tekst], p[bakgrunn])


def test_teksten_paa_halvhvite_flater_og_kantene_paa_felt_har_god_kontrast():
    p = _palett()
    for bunn in ("ta-band", "ta-krem"):
        glass = _over(0.5, _hex(p[bunn]))                                   # --ta-glass: rgba(255,255,255,.5)
        for tekst in ("ta-plomme", "ta-mauve", "ta-baer"):
            assert _kontrast(_hex(p[tekst]), glass) >= 4.5, (tekst, bunn)
    for flate in ("ta-felt-bg", "ta-krem", "ta-hvit"):                        # kanten på et felt: minst 3:1 mot feltet, mot siden og mot det hvite
        assert _kontrast(_hex(p["ta-felt"]), _hex(p[flate])) >= 3.0, flate
    assert _kontrast(_hex(p["ta-baer"]), _hex(p["ta-krem"])) >= 3.0           # fokus- og aksentlinjer


def _bakgrunn(velger: str) -> str:
    """Verdien etter «background:» i den første regelen som gjelder akkurat denne velgeren."""
    for liste, blokk in _regler(_uten_kommentarer()):
        if velger in [v.strip() for v in liste.split(",")]:
            m = re.search(r"(?<![-\w])background:\s*([^;]+);", blokk)
            if m:
                return m.group(1).strip()
    raise AssertionError(f"ingen bakgrunn for {velger}")


@pytest.mark.parametrize("klasse", [".dp-blokk-tekst", ".dp-blokk-filer", ".dp-blokk-lenker"])
def test_bare_tekst_fil_og_lenkeblokkene_er_hvite_saa_de_er_lette_a_lese(klasse):
    """Camilla (01.10.2026) ringet rundt «Velkommen» og «Litteratur og artikler» og ba om hvitt der (da alle kortene ble hvite, ble det for mye hvitt),
    og ba så om at «Presentasjoner og dokumenter» også er hvit."""
    assert _bakgrunn(f'body[data-tema="ta"] {klasse}') == "var(--ta-hvit)", klasse


def test_den_hvite_regelen_staar_etter_blokkregelen_ellers_vinner_beige():
    """Begge velgerne har samme spesifisitet: den siste i filen vinner. Står den hvite først, blir blokkene beige igjen."""
    velgere = [[v.strip() for v in liste.split(",")] for liste, _ in _regler(_uten_kommentarer())]
    blokk = next(i for i, v in enumerate(velgere) if 'body[data-tema="ta"] .dp-blokk' in v)
    hvit = next(i for i, v in enumerate(velgere) if 'body[data-tema="ta"] .dp-blokk-tekst' in v)
    assert blokk < hvit


@pytest.mark.parametrize("klasse", [".kort", ".dp-blokk", ".dp-idag", ".dp-dager", ".dp-meny", ".ms-kurs", ".ms-kontokort", ".inn-kort"])
def test_alle_andre_kort_og_blokker_er_beige_som_for(klasse):
    assert _bakgrunn(f'body[data-tema="ta"] {klasse}') == "var(--ta-band)", klasse


@pytest.mark.parametrize("klasse", [".dp-melding", ".dp-viktig-info", ".flash.info", ".dp-pr-dag", ".dp-kk", ".dp-samling", ".ms-idag", ".inn-boks"])
def test_meldinger_og_flater_inne_i_kortene_er_halvhvite_som_for(klasse):
    assert _bakgrunn(f'body[data-tema="ta"] {klasse}') == "var(--ta-glass)", klasse


def test_drakten_gjor_ikke_sidene_bredere_enn_for():
    """Camilla (01.10.2026): «hvorfor går alt av informasjon ... helt fra høyre til venstre ... litt mer midtstilt». Toppen, innholdet og bunnen er maks 1080 px,
    som resten av systemet og som før drakten (jeg hadde gjort dem 1180 px)."""
    velgere = ('body[data-tema="ta"] main', 'body[data-tema="ta"] header .inner', 'body[data-tema="ta"] .ta-bunn-inner')
    bredder = {}
    for liste, blokk in _regler(_uten_kommentarer()):
        for v in liste.split(","):
            if v.strip() in velgere and (m := re.search(r"max-width:\s*(\d+)px", blokk)):
                bredder[v.strip()] = int(m.group(1))
    assert set(bredder) == set(velgere) and max(bredder.values()) <= 1080, bredder


def test_skjemafeltene_har_runde_kanter_men_knappene_er_firkantede():
    """Camilla (01.10.2026): «de hvite feltene i påmeldingsskjema skal ha mer runde kanter». Knappene følger terapiakademiet.no (firkantede)."""
    regler = _regler(_uten_kommentarer())
    felt = next(blokk for liste, blokk in regler if 'body[data-tema="ta"] input' in [v.strip() for v in liste.split(",")])
    radius = int(re.search(r"border-radius:\s*(\d+)px", felt).group(1))
    assert radius >= 8, felt
    knapp = next(blokk for liste, blokk in regler if 'body[data-tema="ta"] button' in [v.strip() for v in liste.split(",")])
    assert "border-radius:0" in knapp.replace(" ", ""), knapp


def test_paameldingens_kurstittel_og_skjemaoverskrift_er_midtstilt():
    """Camilla (02.10.2026): «Påmelding» øverst i skjemaet må i hvert fall være midtstilt, og siden litt mer midtstilt."""
    regler = _regler(_uten_kommentarer())

    def blokk(velger):
        return next(b for liste, b in regler if velger in [v.strip() for v in liste.split(",")]).replace(" ", "")
    assert "text-align:center" in blokk('body[data-tema="ta"] .pm-skjema > h2')
    assert "text-align:center" in blokk('body[data-tema="ta"] .pm-topp')
    assert "margin-inline:auto" in blokk('body[data-tema="ta"] .pm-topp h1')              # tittelboksen (30 tegn bred) står midt på siden


def test_paameldingen_er_midtstilt_med_kursinfoen_til_hoeyre_og_feltnavnene_til_venstre():
    """Camilla (02.10.2026): skjemaet midtstilt, men feltnavnene (Fornavn, Etternavn ...) til venstre for de hvite feltene og kursinfoen til høyre for skjemaet."""
    kilde = _uten_kommentarer()
    regler = _regler(kilde)

    def blokk(velger):
        return "".join(b for liste, b in regler if velger in [v.strip() for v in liste.split(",")]).replace(" ", "")      # alle reglene for velgeren
    oppsett = blokk('body[data-tema="ta"] .pm-oppsett')
    assert "margin-inline:auto" in oppsett and re.search(r"max-width:\d+px", oppsett)                       # midt på siden
    assert 'grid-template-areas:"intro""info""skjema"' in oppsett                                           # smal skjerm: kursinfoen over skjemaet
    assert 'grid-template-columns:minmax(0,1fr)minmax(0,620px)minmax(0,1fr)' in oppsett                      # bred skjerm: skjemaet i midten ...
    assert 'grid-template-areas:".intro."".skjemainfo"' in oppsett                                           # ... og kursinfoen til høyre for det
    assert re.search(r"@media \(min-width: 1140px\) \{[^@]*main:has\(\.pm-oppsett\)", kilde)            # plass nok til boksen på siden
    assert "grid-area:skjema" in blokk('body[data-tema="ta"] .pm-skjema') and "grid-area:info" in blokk('body[data-tema="ta"] .pm-info')
    info = blokk('body[data-tema="ta"] .pm-info')
    assert "position:sticky" in info and "justify-self:start" in info                                         # følger med når man ruller, på høyre side
    rad = blokk('body[data-tema="ta"] .pm-skjema .pm-rad')
    assert "grid-template-columns:150pxminmax(0,1fr)" in rad and "justify-content" not in rad                  # navnet til venstre, feltet til høyre, raden fyller kortet kant i kant
    assert "text-align:left" in blokk('body[data-tema="ta"] .pm-skjema .pm-etikett')                           # venstrejustert i kanten: ujevn venstrekant fikk alt til å se skjevt ut
    for velger in ('body[data-tema="ta"] .pm-skjema .pm-etikett', 'body[data-tema="ta"] .pm-skjema .pm-felt'):
        assert "text-align:center" not in blokk(velger), velger                                              # navnene er ikke flyttet over feltene


def test_kontrastfunksjonen_stemmer_med_kjente_verdier():
    assert round(_kontrast((0, 0, 0), (255, 255, 255)), 1) == 21.0
    assert round(_kontrast(_hex("#562a3e"), _hex("#f7e5d0")), 1) == 9.5


def test_draktfilen_serveres_som_css():
    r = _klient().get("/static/tema-terapiakademiet.css")
    assert r.status_code == 200 and "css" in r.headers["Content-Type"]


def test_bunnen_skjules_ved_utskrift_og_drakten_rorer_ikke_utskriften():
    base = (Path(__file__).resolve().parent.parent / "kurs" / "web" / "templates" / "base.html").read_text(encoding="utf-8")
    utskrift = base[base.index("@media print"):]
    assert ".ta-bunn" in utskrift[:utskrift.index("}")]                    # første regel i utskriftsdelen skjuler det som ikke skal på papir
    assert "@media print" not in _uten_kommentarer()                        # selve drakten hører bare til skjermen


# ============================ hvilke sider som får drakten ============================

def _med_drakt(html: str) -> bool:
    return '<body data-tema="ta"' in html and "tema-terapiakademiet.css" in html and 'class="ta-bunn"' in html


def _uten_drakt(html: str) -> bool:
    return "data-tema" not in html and "tema-terapiakademiet.css" not in html and 'class="ta-bunn"' not in html and 'class="ta-logo"' not in html


def test_deltakersidene_har_drakten(con):
    kid = lag_kurs(con, "K1")
    skriv_side(con, kid, dokument(tekstblokk("Velkommen", "<p>Hei</p>")))
    did, pid = lag_deltaker(con, kid)
    dag = db.kursdager(con, kid)[0]
    k = deltaker_klient(did)
    sider = {"Min side": k.get("/kurs/K1/deltakerside"), "Mine kurs": k.get("/min-side"), "innlogging": _klient().get("/logg-inn"),
             "lenken virker ikke": _klient().get("/min/ugyldig"), "ikke tilgang": k.get("/kurs/ZZZ/deltakerside"),
             "innsjekk (QR)": _klient().get(f"/innsjekk/{dag['innsjekk_token']}")}
    for navn, r in sider.items():
        assert r.status_code in (200, 404), navn
        assert _med_drakt(_html(r)), navn


def test_adminsidene_har_ikke_drakten(con):
    kid = lag_kurs(con, "K1")
    skriv_side(con, kid, dokument(tekstblokk("Velkommen", "<p>Hei</p>")))
    did, pid = lag_deltaker(con, kid)
    admin = admin_klient(con)
    sider = {"oversikten": admin.get("/admin"), "deltakervinduet": admin.get(f"/admin/kurs/{kid}/deltaker/{pid}"), "kurs i admin": admin.get(f"/admin/kurs/{kid}/oppsett"),
             "Min side-fanen": admin.get(f"/admin/kurs/{kid}/kursside"), "påmeldingsskjemaet i admin": admin.get(f"/admin/kurs/{kid}/paameldingsskjema"),
             "admin-innlogging": _klient().get("/admin/logg-inn")}
    for navn, r in sider.items():
        assert r.status_code == 200, navn
        assert _uten_drakt(_html(r)), navn
        assert "IPR Påmeldingssystem" in _html(r), navn


def test_hele_den_offentlige_paameldingen_har_drakten(con, monkeypatch):
    """Camilla (01.10.2026): «Påmeldingssiden kan få samme stil som terapiakademiet. Det skal etter hvert være en annen stil på ipr sine kurs.»"""
    from kurs import skjemafelt
    from kurs.web import app as webapp
    lag_kurs(con, "K1")
    k = _klient()
    sider = {"skjemaet": k.get("/kurs/K1"), "bedriftspåmeldingen": k.get("/kurs/K1/gruppe"),
             "skjemaet med valideringsfeil": _klient().post("/kurs/K1", data={"fornavn": ""}),
             "kvitteringen": _klient().post("/kurs/K1", data={"fornavn": "Kari", "etternavn": "Test", "epost": "kari@example.no", "samtykke": "on", "betaler": "person",
                                                              "adresse": "Veien 1", "postnr": "0150", "poststed": "Oslo"})}
    for navn, r in sider.items():
        assert r.status_code in (200, 400), (navn, r.status_code)
        assert _med_drakt(_html(r)), navn
    assert "Du er påmeldt" in _html(sider["kvitteringen"])
    monkeypatch.setattr(webapp, "_skjema_snapshot", lambda kurs: (_ for _ in ()).throw(skjemafelt.SkjemaLesefeil("test")))
    utilgjengelig = _klient().get("/kurs/K1")
    assert utilgjengelig.status_code == 503 and _med_drakt(_html(utilgjengelig))


def test_pm_reglene_for_paameldingssiden_gjelder_bare_med_drakten():
    """Skjemaet har egne klasser (pm-): drakten bytter dem ut, men bare under body[data-tema], så admins skjemabygger (samme paamelding.css) er uberørt."""
    regler = [liste for liste, _ in _regler(_uten_kommentarer()) if ".pm-" in liste]
    assert len(regler) > 20
    assert all(v.strip().startswith('body[data-tema="ta"]') for liste in regler for v in liste.split(","))


def test_forhandsvisningen_av_min_side_i_admin_viser_drakten_men_ingen_bunn_eller_meny(con):
    kid = lag_kurs(con, "K1")
    skriv_side(con, kid, dokument(tekstblokk("Velkommen", "<p>Hei</p>")))
    html = _html(admin_klient(con).get(f"/admin/kurs/{kid}/kursside/forhandsvis?versjon=publisert"))
    assert '<body data-tema="ta" class="dp-side dp-forhandsvisning"' in html and "tema-terapiakademiet.css" in html
    assert "Mine kurs</a><a" not in html                                        # ingen deltakermeny i forhåndsvisningen (bunnen skjules av CSS)


def test_etikettene_i_den_morke_stripen_staar_i_min_side(con):
    kid = lag_kurs(con, "K1")
    skriv_side(con, kid, dokument(tekstblokk("Velkommen", "<p>Hei</p>")))
    did, pid = lag_deltaker(con, kid)
    html = _html(deltaker_klient(did).get("/kurs/K1/deltakerside"))
    # Camilla 04.10.2026: «Oppstartsdato» (flere dager) eller «Dato» (én dag) i stedet for «Datoer»
    assert '<li data-etikett="Sted">' in html and '<li data-etikett="Format">' in html
    assert '<li data-etikett="Oppstartsdato">' in html or '<li data-etikett="Dato">' in html


# ============================ logoen er valgfri ============================

def test_navnet_staar_som_tekst_til_logoen_er_lagt_inn_og_bunnen_har_kontaktopplysninger(con):
    html = _html(_klient().get("/logg-inn"))
    assert '<span class="logo">IPR Påmeldingssystem</span>' in html and "ta-logo" not in html
    assert "Kursadministrasjonen, Institutt for Psykologisk Rådgivning" in html and 'href="mailto:' in html


def test_logoen_vises_i_toppen_og_den_lyse_i_bunnen_naar_filene_er_lagt_inn(con, tmp_path):
    (tmp_path / "logo").mkdir()
    (tmp_path / "logo" / "terapiakademiet.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    html = _html(_klient().get("/logg-inn"))
    assert '<span class="logo"><img class="ta-logo" src="/static/logo/terapiakademiet.png" alt="Terapiakademiet"></span>' in html
    assert html.count('class="ta-logo"') == 1                                  # den burgunder logoen passer ikke på den mørke bunnen: ingen logo der
    (tmp_path / "logo" / "terapiakademiet-lys.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    html = _html(_klient().get("/logg-inn"))
    assert html.count('class="ta-logo"') == 2 and 'src="/static/logo/terapiakademiet-lys.svg"' in html
    (tmp_path / "logo" / "terapiakademiet.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    assert 'src="/static/logo/terapiakademiet.svg"' in _html(_klient().get("/logg-inn"))      # svg går foran png


def test_logoen_vises_aldri_i_admin(con, tmp_path):
    (tmp_path / "logo").mkdir()
    (tmp_path / "logo" / "terapiakademiet.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    assert 'class="ta-logo"' not in _html(_klient().get("/admin/logg-inn")) and "Terapiakademiet" not in _html(_klient().get("/admin/logg-inn"))


def test_de_ekte_logofilene_er_lagt_inn_som_png_med_gjennomsiktig_bakgrunn_og_riktig_format():
    """Camilla la ved logoen 01.10.2026 og ga lov til å bruke den på Min side: burgunder utgave til toppen og lys utgave til den mørke bunnen."""
    import struct
    for navn in ("terapiakademiet.png", "terapiakademiet-lys.png"):
        data = (Path(tema.__file__).resolve().parent / "web" / "static" / "logo" / navn).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", navn
        bredde, hoyde, _dyp, fargetype = struct.unpack(">IIBB", data[16:26])
        assert (bredde, hoyde) == (751, 186) and fargetype == 6, (navn, bredde, hoyde, fargetype)       # RGBA: gjennomsiktig bakgrunn


def test_logoen_i_bunnen_strekkes_ikke_over_hele_bredden():
    """Bunnen er en kolonne (flex): uten align-self ble bildet strukket til full bredde med fast høyde og så helt skjevt ut."""
    regel = next(blokk for liste, blokk in _regler(_uten_kommentarer()) if liste.strip() == 'body[data-tema="ta"] .ta-bunn img.ta-logo')
    assert "align-self:flex-start" in regel.replace(" ", "") and "width:auto" in regel.replace(" ", "")


def test_logo_funksjonen_kjenner_bare_de_to_plassene(con):
    assert tema.logo("topp") is None and tema.logo("bunn") is None and tema.logo("annet") is None          # (LOGO_MAPPE peker på en tom mappe i testen)


def test_toppfeltet_har_oppstartsdato_tid_alle_dager_og_godkjent_i_stedet_for_format(con):
    """Camilla 04.10.2026: Sted, Oppstartsdato, «09:00–16:00 alle dager», og «Godkjent: 64 timer vedlikeholdsaktivitet» i stedet
    for «Fysisk kurs»."""
    kid = lag_kurs(con, "K2")                                          # to kursdager
    dok = dokument(tekstblokk("Velkommen", "<p>Hei</p>"))
    dok["godkjent"] = "64 timer vedlikeholdsaktivitet"
    skriv_side(con, kid, dok)
    did, pid = lag_deltaker(con, kid)
    html = _html(deltaker_klient(did).get("/kurs/K2/deltakerside"))
    stripe = html.split('<ul class="dp-meta">')[1].split("</ul>")[0]
    assert 'data-etikett="Oppstartsdato"' in stripe and "alle dager</span>" in stripe
    assert 'data-etikett="Godkjent"' in stripe and "64 timer vedlikeholdsaktivitet" in stripe and 'data-etikett="Format"' not in stripe
