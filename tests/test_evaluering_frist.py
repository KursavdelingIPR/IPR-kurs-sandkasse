"""Evaluerings-e-posten som e-postene kursavdelingen har sendt før (Camilla 15.10.2026): «Under finner du lenke til evalueringsskjemaet
for EFT 1-årig sin 3. samling …», frist én uke etter siste dag i kurset eller samlingen kl. 12.00, «Med vennlig hilsen Kursavdelingen»,
og etter hver samling som standard på utdanningene (Modul, 1-årig, påbygg). Oppdiktede data, ingen nettverk."""
from datetime import date, timedelta

import pytest

from kurs import config, db, evaluering
from kurs.kjoring import Kjoring

DAG1, DAG2, DAG3 = date(2099, 3, 2), date(2099, 3, 3), date(2099, 4, 6)


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _kurs(con, navn, datoer=(DAG1, DAG2, DAG3)):
    kid = db.opprett_kurs(con, kode="EV1", navn=navn, datoer=[d.isoformat() for d in datoer], sharepoint_mappe="K/EV1", pris_nok=0,
                          type="fysisk", sted="Oslo")
    db.meld_paa(con, kid, epost="d1@eksempel.no", fornavn="Ada", etternavn="Test")
    con.commit()
    return kid


def _eposter(con):
    return [(r["emne"], r["html"]) for r in con.execute("SELECT emne, html FROM sendt_epost WHERE type='evaluering' ORDER BY id")]


@pytest.mark.parametrize("navn,forventet", [
    ("EFT spesialistutdanning - Modul 2 (kull 14)", "samling"), ("Modul 1 (kull 15)", "samling"), ("EFT 1-årig", "samling"),
    ("EFST 1-årig Bergen", "samling"), ("EFT Påbygging", "samling"), ("EFT 1 årig påbygg", "samling"),
    ("Valgfritt program i humanistisk barne- og ungdomspsykologi – Bergen", "samling"),
    ("Veiledning - Obligatorisk pakke (kull 14)", "slutt"),
    ("Veilederprogram – modul 1", "slutt"), ("Sinte barn og sinte voksne", "slutt"), ("Grunnkurs i EFT-C", "slutt"),
    ("Modulær terapi", "slutt")])
def test_standard_for_naar_evalueringen_sendes(navn, forventet):
    assert evaluering.standard_modus({"navn": navn}) == forventet


def test_valget_for_kurset_overstyrer_standarden(con):
    kid = _kurs(con, "Modul 3 (kull 13)")
    rad = lambda: con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()     # noqa: E731
    assert evaluering.modus(rad()) == "samling"
    evaluering.sett_modus(con, kid, "slutt", aktor="admin:test")
    assert evaluering.modus(rad()) == "slutt"                         # lagres som eget valg, ikke tilbake til standarden


def test_e_posten_etter_en_samling_har_runden_fristen_og_kursavdelingen(con):
    _kurs(con, "EFT 1-årig")                                          # to samlinger: 2.–3. mars og 6. april 2099
    evaluering.kjor(Kjoring(con, idag=DAG2 + timedelta(days=1)))
    con.commit()
    ((emne, html),) = _eposter(con)
    assert emne == "Evaluering: EFT 1-årig sin 1. samling"
    assert "evalueringsskjemaet for <strong>EFT 1-årig sin 1. samling</strong>" in html
    assert "Frist for å svare på evalueringen er satt til <strong>10. mars 2099</strong>, kl. 12.00." in html     # én uke etter 3. mars
    assert "Ha en fin dag!" in html and "Med vennlig hilsen<br>Kursavdelingen" in html
    assert "Kursadministrasjonen" not in html                          # ikke den faste hilsenen eller kursets signatur
    evaluering.kjor(Kjoring(con, idag=DAG3 + timedelta(days=1)))
    con.commit()
    assert _eposter(con)[1][0] == "Evaluering: EFT 1-årig sin 2. samling" and "13. april 2099" in _eposter(con)[1][1]


def test_e_posten_etter_et_engangskurs_gjelder_kurset(con):
    _kurs(con, "Sinte barn og sinte voksne", datoer=(DAG1, DAG2))
    evaluering.kjor(Kjoring(con, idag=DAG2 + timedelta(days=1)))
    con.commit()
    ((emne, html),) = _eposter(con)
    assert emne == "Evaluering: Sinte barn og sinte voksne" and "sin " not in emne and "10. mars 2099" in html
