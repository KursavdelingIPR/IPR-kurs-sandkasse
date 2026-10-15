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


def test_kurshjulet_er_aarsplanen_med_kalender_og_uke_for_uke(con):
    db.opprett_kurs(con, kode="M1", navn="Modul 1", datoer=["2031-03-10", "2031-03-11"], sharepoint_mappe="Kurs/M1")
    con.commit()
    html = _side()
    assert 'class="visningsvalg"' not in html and ">Måneder</a>" not in html          # bare kurshjulet (15.10.2026)
    assert html.count('<section class="hjulmaaned"') == 12 and '<section class="hjuluker"' in html
    assert re.search(r'<td class="harkurs" style="background:var\(--kf-\d\)" tabindex="0">\s*<span class="dnr">10</span>', html)
    assert re.search(r'class="hjulnavn kursfarge-\d" style="background:var\(--kf-\d\)" title="Modul 1 · 10\.–11\. mar[^"]*">Modul 1</a>', html)   # kort navn, datoene i title
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


def test_gamle_visninger_finnes_ikke_lenger(con):
    """«Måneder», «Uke for uke» og «Ledige perioder» er tatt bort (Camilla 15.10.2026): ?vis= og ?ledig= gir bare kurshjulet."""
    for sporring in ("vis=maaneder", "vis=uker", "ledig=3"):
        html = _side(f"aar=2031&{sporring}")
        assert html.count('<section class="hjulmaaned"') == 12 and "minimaaned" not in html and "Ledige perioder" not in html


def test_kursholdere_vises_ogsaa_for_kurs_i_systemet(con):
    """Kurset i systemet har en skjult planlagt samling med kursholderne (sjekklistene, kurs/sjekklister.synk_kurs)."""
    kid = db.opprett_kurs(con, kode="V1", navn="Valgfritt program", datoer=["2031-06-16", "2031-06-17"], sharepoint_mappe="Kurs/V1",
                          status="aapen")
    samling = con.execute("SELECT samling_id FROM kursdag WHERE kurs_id=? LIMIT 1", (kid,)).fetchone()[0]
    pid = db.sett_inn(con, "INSERT INTO planlagt_kurs (navn, arrangor, status, farge, kilde, opprettet_av, kurs_id) VALUES (?,?,?,?,?,?,?)",
                      ("Valgfritt program", "IPR", "planlagt", 0, "Kurs i systemet", "test", kid))
    sid = db.sett_inn(con, "INSERT INTO planlagt_samling (planlagt_kurs_id, fra_dato, til_dato, kursholdere, kurs_samling_id) "
                           "VALUES (?,?,?,?,?)", (pid, "2031-06-16", "2031-06-17", "Bente B.", samling))
    con.commit()
    plan = aarsplan.hent(con, 2031)
    assert plan.kursholdere.get(sid) == "Bente B."
    assert "Kursholdere: Bente B." in _side()


def test_ferie_og_merknader_ligger_i_nedtrekk_og_uker_uten_kurs_slaas_sammen(con):
    """Camilla 15.10.2026: ferier er ikke kurs, så de skal ikke ta plass i «Uke for uke»."""
    v = aarsplan.valider_periode(MultiDict({"type": "skoleferie", "navn": "Sommerferie", "fra_dato": "2031-06-23",
                                            "til_dato": "2031-08-15", "omraade": "Oslo", "gjelder": "alle", "notat": ""}))
    aarsplan.opprett_periode(con, v, "admin:test")
    _plan(con, "Høstkurs", {"fra_dato": "2031-09-01", "til_dato": "2031-09-02"})
    _, uker = aarsplan.kurshjul(aarsplan.hent(con, 2031), date(2031, 1, 1))
    sommer = next(u for u in uker if any("Sommerferie" in f for f in u.ferie))
    assert not sommer.kurs and sommer.til_nr - sommer.fra_nr >= 7 and sommer.ferie.count("Skoleferie Oslo: Sommerferie") == 1
    html = _side()
    assert '<details class="hjulferie-valg">' in html and "Ingen kurs · Ferie og merknader (" in html
    assert '<span class="hjulferie">' not in html
