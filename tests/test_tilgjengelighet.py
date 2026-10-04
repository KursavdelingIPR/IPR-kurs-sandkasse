"""Tilgjengelighet og UI-konsistens i malene (statisk kontroll av kildene + rendrede sider).

  * hvert skjemafelt (input/select/textarea, unntatt hidden) har en ledetekst: <label for=id>, er inni en <label>,
    eller har aria-label
  * tabeller har <th scope="col"> (eller scope="row"/"rowgroup" for rad- og gruppeoverskrifter)
  * hopp-til-innhold-lenke og <main id="innhold">
  * flash-meldinger har role=alert/status
  * «Nytt kurs +» finnes paa Oversikten, men ikke i toppmenyen (ingen duplisert navigasjon)
  * deltakersoeket i adminmenyen: etikett, role=search, combobox/listbox-attributter og opplesing av antall treff
"""
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from kurs import config, db

MALER = Path(__file__).resolve().parent.parent / "kurs" / "web" / "templates"
FELT = re.compile(r"<(input|select|textarea)\b([^>]*)>", re.I)


def _har_ledetekst(html: str, m: re.Match) -> bool:
    attrs = m.group(2)
    if 'type="hidden"' in attrs or "aria-label=" in attrs or 'type="submit"' in attrs:
        return True
    ident = re.search(r'\bid="([^"]+)"', attrs)
    if ident and re.search(r'<label\b[^>]*\bfor="' + re.escape(ident.group(1)) + '"', html):
        return True
    foran = html[:m.start()]
    return foran.rfind("<label") > foran.rfind("</label>")     # inni en <label>...</label>


@pytest.mark.parametrize("fil", sorted(p.name for p in MALER.glob("*.html")))
def test_alle_skjemafelt_har_ledetekst(fil):
    html = (MALER / fil).read_text(encoding="utf-8")
    mangler = [m.group(0)[:80] for m in FELT.finditer(html) if not _har_ledetekst(html, m)]
    assert not mangler, mangler


@pytest.mark.parametrize("fil", sorted(p.name for p in MALER.glob("admin*.html")))
def test_tabelloverskrifter_har_scope(fil):
    html = (MALER / fil).read_text(encoding="utf-8")
    uten_scope = html.replace('<th scope="col"', "").replace('<th scope="rowgroup"', "").replace('<th scope="row"', "")
    assert not re.search(r"<th(\s[^>]*)?>", uten_scope), fil


def test_ingen_inline_stil_for_fargekoding_alene():
    """Status vises alltid med tekst (merke med tekst), aldri bare som farge."""
    for fil in MALER.glob("admin*.html"):
        html = (MALER / fil).read_text(encoding="utf-8")
        for m in re.finditer(r'<span class="merke[^"]*">\s*</span>', html):
            pytest.fail(f"{fil.name}: tomt statusmerke {m.group(0)}")


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _admin():
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def test_hopp_lenke_main_og_flash_roller(con):
    admin = _admin()
    html = admin.get("/admin").get_data(as_text=True)
    assert 'class="hopp" href="#innhold"' in html and '<main id="innhold">' in html
    r = admin.post("/admin/kurs/ny", data={"navn": "", "type": "fysisk", "datoer": "x", "start_kl": "09:00",
                                           "slutt_kl": "16:00", "fakturering": "person"})
    assert 'role="alert"' in r.get_data(as_text=True)


def test_nytt_kurs_knapp_paa_oversikten_men_ikke_i_toppmenyen(con):
    html = _admin().get("/admin").get_data(as_text=True)
    nav = html.split("<nav>")[1].split("</nav>")[0]
    assert "Nytt kurs" not in nav
    assert 'class="knapp nytt-kurs" href="/admin/kurs/ny">Nytt kurs +</a>' in html


def test_nytt_kurs_knappen_staar_paa_linjen_med_bruk_filtre(con):
    """Camilla (04.10.2026): tettere oppsett - «Nytt kurs +» til høyre på linjen med «Vis bare mine kurs» og «Bruk filtre»,
    under filtrene (før: i overskriftsraden «Kurs», 02.10.2026)."""
    html = _admin().get("/admin").get_data(as_text=True)
    topp, soek, kurs, filter_, knapp = (html.index(x) for x in ('<h1>Oversikt</h1>', 'class="sokepanel"', '<h2 id="kurs">Kurs</h2>',
                                                                 'id="kursfilter"', 'class="knapp nytt-kurs" href="/admin/kurs/ny">Nytt kurs +</a>'))
    assert topp < soek < kurs < filter_ < knapp
    rad = html[html.rindex('<div class="handlinger">', 0, knapp):knapp]
    assert "Bruk filtre" in rad and "Nullstill filtre" in rad


def test_lesetilgang_ser_ikke_nytt_kurs_knappen(con):
    from kurs.web import app as webapp
    db.opprett_admin_bruker(con, "lese", "Lese", "passord-som-holder", rolle="lese")
    con.commit()
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": "lese", "passord": "passord-som-holder"})
    assert "Nytt kurs +" not in k.get("/admin").get_data(as_text=True)


def test_oversikten_viser_kurslisten_foer_statuskortene_og_skjuler_avsluttede(con):
    start = date.today() + timedelta(days=10)
    db.opprett_kurs(con, kode="K1", navn="Kommende kurs", datoer=[start.isoformat()], sharepoint_mappe="K/1")
    db.opprett_kurs(con, kode="K2", navn="Gammelt kurs", datoer=["2020-01-01"], sharepoint_mappe="K/2", status="avsluttet")
    con.commit()
    html = _admin().get("/admin").get_data(as_text=True)
    assert "Kommende kurs" in html and "Gammelt kurs" not in html
    assert html.index('<h2 id="kurs">Kurs</h2>') < html.index("<h2>Status</h2>")      # kurslisten først, så statuskortene
    assert "Siste hendelser" not in html


def test_aktiv_side_er_markert_i_menyen(con):
    admin = _admin()
    html = admin.get("/admin").get_data(as_text=True)
    assert 'aria-current="page">Oversikt</a>' in html and 'aria-current="page">Kalender</a>' not in html
    assert "Aktiviteter</a>" not in html                                               # Aktiviteter er slått sammen med Oversikten
    kalender = admin.get("/admin/aktiviteter/kalender").get_data(as_text=True)
    assert 'aria-current="page">Kalender</a>' in kalender and 'aria-current="page">Oversikt</a>' not in kalender


def test_deltakersoek_paa_oversikten_har_etikett_rolle_og_tastaturstoette(con):
    html = _admin().get("/admin").get_data(as_text=True)
    hode, innhold = html.split("</header>")[0], html.split("</header>")[1]
    assert "data-deltakersok" not in hode                                           # ikke lenger i toppmenyen: ett søkefelt i bildet
    assert '<form class="sokefelt" role="search" aria-label="Deltakersøk på siden"' in innhold
    assert '<label class="sok-etikett" for="sok-deltaker">Søk etter deltaker</label>' in innhold
    assert 'role="combobox" aria-expanded="false" aria-controls="sok-deltaker-liste" aria-autocomplete="list"' in innhold
    assert 'id="sok-deltaker-liste" role="listbox"' in innhold and 'role="status" aria-live="polite" data-sok-status' in innhold
    assert '<nav class="bruker" aria-label="Bruker">' in hode                       # to menyer: den ene har navn
    assert hode.index('class="hopp"') < hode.index("<nav")                          # hopp-lenken er fortsatt først
    assert 'aria-hidden="true"' in innhold.split('<kbd class="sok-hurtigtast"')[1].split(">")[0]   # hurtigtast-merket leses ikke opp
    assert 'data-sok-adresse="/admin#sok-deltaker"' in hode                         # «/» fra andre sider tar deg hit
    # kurssøket ved siden av: eget felt med etikett som hører til filterskjemaet under
    assert '<label class="sok-etikett" for="sok-kurs">Søk etter kurs</label>' in innhold
    assert 'role="search" aria-label="Kurssøk"' in innhold and 'name="sok" form="kursfilter"' in innhold
    assert html.count('for="sok-deltaker"') == 1 and html.count('for="sok-kurs"') == 1
