"""Betalingsvilkårene på påmeldingssiden (Camilla 09.–10.2026): teksten hun godkjente, fakturagebyret på 89 kr ved «Delt opp», og at
fakturaen kan sendes tidligere enn seks måneder før kursstart etter e-post til kurs@ipr.no - eller post@terapiakademiet.no på
Terapiakademiet-kurs. Oppdiktede data, ingen nettverk."""
from datetime import date, timedelta

import pytest

from kurs import config, db


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _side(con, merke=None, pris=9000, dager=1, **kw) -> str:
    start = date.today() + timedelta(days=60)
    kid = db.opprett_kurs(con, kode="BV1", navn="Vilkårskurs", sharepoint_mappe="K/BV1", pris_nok=pris, type="fysisk", sted="Oslo",
                          status="aapen", datoer=[(start + timedelta(days=30 * n)).isoformat() for n in range(dager)], **kw)
    if merke:
        con.execute("UPDATE kurs SET merke=? WHERE id=?", (merke, kid))
    con.commit()
    from kurs.web import app as webapp
    return webapp.app.test_client().get("/kurs/BV1").get_data(as_text=True)


def test_vilkaarene_og_kontakten_for_ipr(con):
    side = _side(con)
    for tekst in ("Betalingsvilkår", "Fakturamottakeren kan ikke endres etter påmeldingen.",
                  "Fakturaen sendes tidligst seks måneder før kursstart.", "Ønsker du\n        fakturaen tidligere, send en e-post til",
                  'href="mailto:kurs@ipr.no"', "Fakturagebyr: 89 kr per faktura. Fakturaen må være betalt før kurset starter."):
        assert tekst in side, tekst
    assert "Deler du opp betalingen" not in side                  # én samling: ingenting å dele opp


def test_terapiakademiet_kurs_far_sin_egen_adresse(con):
    side = _side(con, merke="terapiakademiet")
    assert f'href="mailto:{config.TERAPIAKADEMIET_EPOST}"' in side and 'href="mailto:kurs@ipr.no"' not in side.split("betalingsvilkar")[1].split("</ul>")[0]


def test_flere_samlinger_gebyret_ved_delt_opp_og_en_maaned(con):
    side = _side(con, dager=3, betaling="deltaker_velger", faktura_dager_for=30)
    assert "– 89 kr i fakturagebyr per faktura" in side
    assert "Deler du opp betalingen, sendes fakturaen for hver samling ca. én måned før samlingen." in side
    assert "Fakturaen må være betalt før samlingen starter." in side


def test_gratis_kurs_har_ingen_betalingsvilkaar(con):
    assert "Betalingsvilkår" not in _side(con, pris=0)
