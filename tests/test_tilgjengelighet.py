"""Tilgjengelighet og UI-konsistens i malene (statisk kontroll av kildene + rendrede sider).

  * hvert skjemafelt (input/select/textarea, unntatt hidden) har en ledetekst: <label for=id>, er inni en <label>,
    eller har aria-label
  * tabeller har <th scope="col"> (eller scope="row" for radoverskrifter)
  * hopp-til-innhold-lenke og <main id="innhold">
  * flash-meldinger har role=alert/status
  * «Nytt kurs +» finnes paa Aktiviteter og Oversikt, men ikke i toppmenyen (ingen duplisert navigasjon)
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
    uten_scope = html.replace('<th scope="col"', "").replace('<th scope="row"', "")
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


def test_nytt_kurs_knapp_paa_aktiviteter_og_oversikt_men_ikke_i_toppmenyen(con):
    admin = _admin()
    for url in ("/admin", "/admin/aktiviteter"):
        html = admin.get(url).get_data(as_text=True)
        nav = html.split("<nav>")[1].split("</nav>")[0]
        assert "Nytt kurs" not in nav, url
        assert 'class="knapp plass" href="/admin/kurs/ny">Nytt kurs +</a>' in html, url


def test_lesetilgang_ser_ikke_nytt_kurs_knappen(con):
    from kurs.web import app as webapp
    db.opprett_admin_bruker(con, "lese", "Lese", "passord-som-holder", rolle="lese")
    con.commit()
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": "lese", "passord": "passord-som-holder"})
    assert "Nytt kurs +" not in k.get("/admin/aktiviteter").get_data(as_text=True)


def test_oversikten_viser_kurs_foerst_og_skjuler_avsluttede(con):
    start = date.today() + timedelta(days=10)
    db.opprett_kurs(con, kode="K1", navn="Kommende kurs", datoer=[start.isoformat()], sharepoint_mappe="K/1")
    db.opprett_kurs(con, kode="K2", navn="Gammelt kurs", datoer=["2020-01-01"], sharepoint_mappe="K/2", status="avsluttet")
    con.commit()
    html = _admin().get("/admin").get_data(as_text=True)
    assert "Kommende kurs" in html and "Gammelt kurs" not in html
    assert html.index("<h2 style=\"margin-top:14px\">Kurs</h2>") < html.index("<h2>Status</h2>")   # kurs foer statusbokser
    assert html.index("<h2>Status</h2>") < html.index("Siste hendelser")


def test_aktiv_side_er_markert_i_menyen(con):
    html = _admin().get("/admin/aktiviteter").get_data(as_text=True)
    assert 'aria-current="page">Aktiviteter</a>' in html
    assert 'aria-current="page">Oversikt</a>' not in html
