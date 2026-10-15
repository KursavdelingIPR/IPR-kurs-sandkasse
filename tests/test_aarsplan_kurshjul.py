"""Årsplanen som kurshjul (Camilla 15.10.2026): kalenderen til venstre med kursdagene fylt med kursets farge, «Uke for uke» til høyre
med kursnavnene i samme farge, ruten delt på skrå når flere kurs er samme dag, svart for annen arrangør (Oslo/IPRO), og en boks med
kursene når man holder over en dag."""
import re
from datetime import date

import pytest
from werkzeug.datastructures import MultiDict

from kurs import aarsplan, config, db, planlagte_kurs


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _plan(con, navn, *samlinger, **kurs):
    v = planlagte_kurs.valider_kurs(MultiDict({"navn": navn, **{k: str(x) for k, x in kurs.items()}}))
    s = [planlagte_kurs.valider_samling(MultiDict(x)) for x in samlinger]
    pid = planlagte_kurs.opprett(con, v, s, "admin:test")
    con.commit()
    return pid


def _side(params="aar=2031"):
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    r = k.get(f"/admin/aktiviteter/aarsplan?{params}")
    assert r.status_code == 200
    return r.get_data(as_text=True)


def test_kurshjulet_er_standard_med_kalender_og_uke_for_uke(con):
    db.opprett_kurs(con, kode="M1", navn="Modul 1", datoer=["2031-03-10", "2031-03-11"], sharepoint_mappe="Kurs/M1")
    con.commit()
    html = _side()
    assert 'aria-current="page">Kurshjul</a>' in html and ">Måneder</a>" in html and ">Uke for uke</a>" in html
    assert html.count('<section class="hjulmaaned"') == 12 and '<section class="hjuluker"' in html
    assert re.search(r'<td class="harkurs" style="background:var\(--kf-\d\)" tabindex="0">\s*<span class="dnr">10</span>', html)
    assert re.search(r'class="hjulnavn kursfarge-\d" style="background:var\(--kf-\d\)"[^>]*>Modul 1 <span class="hjulperiode">', html)
    assert "<strong>Modul 1</strong>" in html                                     # boksen ved dagen


def test_to_kurs_samme_dag_deler_ruten_paa_skraa_og_annen_arrangor_er_svart(con):
    _plan(con, "Grunnkurs", {"fra_dato": "2031-05-05", "til_dato": "2031-05-06", "sted": "Oslo", "kursholdere": "Kari K."})
    _plan(con, "Oslo-kurs", {"fra_dato": "2031-05-06", "til_dato": "2031-05-06", "sted": "Oslo"}, arrangor="IPRO", ekstern="1")
    html = _side()
    assert re.search(r'style="background:linear-gradient\(135deg, var\(--kf-\d\) 0\.00% 50\.00%, #1d1d1d 50\.00% 100\.00%\)"', html)
    assert 'class="hjulnavn ekstern" style="background:#1d1d1d"' in html
    assert "Annen arrangør (Oslo/IPRO): bare til orientering" in html and "Kursholdere: Kari K." in html


def test_uker_uten_noe_slaas_sammen_og_samling_vises(con):
    _plan(con, "EFT", {"nr": "1", "fra_dato": "2031-01-06", "til_dato": "2031-01-07"},
          {"nr": "2", "fra_dato": "2031-12-01", "til_dato": "2031-12-02"}, antall_samlinger="2")
    maaneder, uker = aarsplan.kurshjul(aarsplan.hent(con, 2031), date(2031, 1, 1))
    assert len(maaneder) == 12
    tekster = [u.tekst for u in uker]
    assert tekster[0] == "Uke 1" or tekster[0].startswith("Uke 1")
    assert any(re.fullmatch(r"Uke \d+–\d+", t) for t in tekster)                  # tomme uker etter hverandre: én linje
    forste = next(u for u in uker if u.kurs)
    assert forste.kurs[0].samling == "1. samling av 2" and forste.kurs[0].periode


def test_gamle_visningen_finnes_under_maaneder(con):
    html = _side("aar=2031&vis=maaneder")
    assert html.count('<section class="minimaaned"') == 12 and 'aria-current="page">Måneder</a>' in html
    assert 'href="/admin/aktiviteter/aarsplan?aar=2032&amp;vis=maaneder"' in html
