"""Årsplanens dager og samlinger (bestillingen 28.09.2026), nå i kurshjulet (15.10.2026: de tolv månedskalenderne med blokker,
«Uke for uke» og «Ledige perioder» er tatt bort): helger, røde dager, skoleferier, sperrer og advarsler på dagene, samlingene
nummerert ut fra hele kurset, og kollisjonene som menneskelige tekster under kalenderen."""
import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from werkzeug.datastructures import MultiDict

from kurs import aarsplan, config, db, kursfarger

IKKE_IDAG = date(2000, 1, 1)


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _kurs(con, kode, navn, datoer, **felter):
    kid = db.opprett_kurs(con, kode=kode, navn=navn, datoer=datoer, sharepoint_mappe=f"Kurs/{kode}", **felter)
    con.commit()
    return kid


def _periode(con, type_, navn, fra, til=None, omraade=None, gjelder="alle"):
    v = aarsplan.valider_periode(MultiDict({"type": type_, "navn": navn, "fra_dato": fra, "til_dato": til or "",
                                            "omraade": omraade or "", "gjelder": gjelder, "notat": ""}))
    aarsplan.opprett_periode(con, v, "admin:test")
    con.commit()


def _side(idag=date(2027, 3, 10), **params) -> str:
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    with k.session_transaction() as s:
        s["demo_dato"] = idag.isoformat()
    r = k.get("/admin/aktiviteter/aarsplan", query_string=params)
    assert r.status_code == 200
    return r.get_data(as_text=True)


def _hjuldager(plan, maaned, idag):
    maaneder, _ = aarsplan.kurshjul(plan, idag)
    return {d.dato.day: d for _, dager in maaneder[maaned - 1].uker for d in dager if d.i_maaneden}


# ============================ månedene ============================


def test_samlingene_nummereres_ut_fra_hele_kurset_ogsaa_over_aarsskiftet(con):
    kid = _kurs(con, "E", "EFT 1-årig", ["2026-11-23", "2026-11-24", "2027-01-18", "2027-01-19", "2027-03-08"])
    plan = aarsplan.hent(con, 2027)
    assert aarsplan.samling(plan, kid, date(2027, 1, 18)) == (2, 3)
    assert aarsplan.samling(plan, kid, date(2027, 3, 8)) == (3, 3)
    assert aarsplan.samling(plan, 99999, date(2027, 3, 8)) == (1, 1)
    (k,) = _hjuldager(plan, 1, IKKE_IDAG)[18].kurs
    assert (k.samling, k.periode, k.farge) == ("2. samling av 3", "18.–19. jan", kursfarger.farge("EFT 1-årig"))


def test_kurs_i_samme_serie_har_samme_farge():
    a = aarsplan.Aktivitet("kurs", "EFT spesialistutdanning – samling 1", kurs_id=1)
    b = aarsplan.Aktivitet("kurs", "EFT spesialistutdanning – samling 2", kurs_id=2)
    c = aarsplan.Aktivitet("kurs", "Noe helt annet", kurs_id=3, spesialistlop="EFT")
    d = aarsplan.Aktivitet("kurs", "Enda et", kurs_id=4, spesialistlop="EFT")
    assert a.farge == b.farge and c.farge == d.farge and aarsplan.Aktivitet("plan", "X").farge is None


def test_dagene_markerer_ferie_sperre_advarsel_rod_dag_og_i_dag(con):
    _periode(con, "skoleferie", "Vinterferie", "2027-02-22", "2027-02-26", omraade="Oslo")
    _periode(con, "sperre", "Seminar", "2027-02-24", "2027-02-24")
    _periode(con, "advarsel", "Travelt", "2027-02-23", "2027-02-25", gjelder="online")
    dager = _hjuldager(aarsplan.hent(con, 2027), 2, date(2027, 2, 22))
    assert dager[22].klasser == "idag ferie"
    assert dager[23].klasser == "ferie advarsel"
    assert dager[24].klasser == "ferie sperre"                       # sperre går foran advarsel
    assert dager[27].klasser == "helg"
    assert "Skoleferie Oslo: Vinterferie" in dager[24].ferie and "Sperret: Seminar" in dager[24].ferie
    assert "Advarsel: Travelt (online kurs)" in dager[23].ferie
    syttende = _hjuldager(aarsplan.hent(con, 2027), 5, IKKE_IDAG)[17]
    assert (syttende.rod, syttende.klasser) == ("Grunnlovsdag (17. mai) og 2. pinsedag", "rod")


# ============================ ledige perioder og kollisjoner ============================


def test_kollisjonene_har_navnene_i_en_setning(con):
    _kurs(con, "A", "Kurs A", ["2027-10-14"])
    _kurs(con, "B", "Kurs B", ["2027-10-14"])
    _kurs(con, "C", "Kurs C", ["2027-10-14"])
    db.sett_inn(con, "INSERT INTO planlagt_aktivitet (type, tittel, fra_dato, til_dato) VALUES ('plan', ?, ?, ?)",
                ("Fagdag", "2027-10-20", "2027-10-20"))
    _kurs(con, "D", "Kurs D", ["2027-10-20"])
    o = aarsplan.overlapp_perioder(aarsplan.hent(con, 2027))
    assert [(x["tekst"], x["navn"]) for x in o] == [("14. okt", "Kurs A, Kurs B og Kurs C"),
                                                    ("20. okt", "Kurs D og Planlagt: Fagdag")]


# ============================ siden ============================


def test_lange_navn_escapes_i_kurshjulet(con):
    _kurs(con, "X", "Kurs <script>alert(1)</script>", ["2027-03-03"])
    t = _side(aar=2027)
    assert "<script>alert(1)" not in t and "<strong>Kurs &lt;script&gt;alert(1)&lt;/script&gt;</strong>" in t


# ============================ lagrede samlinger (Opprett kurs) ============================

def test_lagrede_samlinger_gir_samme_nummer_til_delene_av_en_samling(con):
    """Samling 1 med helgen tatt ut er to deler, begge «1. samling av 2» - samling 2 er egen."""
    from kurs import kursdatoer
    kid = _kurs(con, "S", "Samlingskurs", ["2027-03-04"])
    uten_helg = {date(2027, 3, 6): kursdatoer.Dagavvik(fjernet=True), date(2027, 3, 7): kursdatoer.Dagavvik(fjernet=True)}
    db.lagre_samlinger(con, kid, [
        kursdatoer.Samling(fra=date(2027, 3, 4), til=date(2027, 3, 8), start_kl="09:00", slutt_kl="16:00", timer=6,
                           navn="Grunnsamling", avvik=uten_helg),
        kursdatoer.Samling(fra=date(2027, 4, 12), til=None, start_kl="10:00", slutt_kl="15:00", timer=5)],
        aktor="admin:test")
    con.commit()
    plan = aarsplan.hent(con, 2027)
    assert aarsplan.samling(plan, kid, date(2027, 3, 8)) == (1, 2) and aarsplan.samling(plan, kid, date(2027, 4, 12)) == (2, 2)
    mars = _hjuldager(plan, 3, IKKE_IDAG)
    assert [k.samling for d in (4, 5, 8) for k in mars[d].kurs] == ["1. samling av 2"] * 3 and mars[6].kurs == []

