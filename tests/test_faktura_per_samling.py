"""Delt faktura per samling (del 2 av «Opprett kurs»): én faktura per samling i stedet for én per kursdag.

Kravene:
  * «Én faktura per samling» gir én faktura per samling, N dager før samlingens første kursdag, knyttet til samlingens
    første kursdag (vernet mot dobbel faktura - én per påmelding og kursdag - er uendret)
  * beløpet deles på antall samlinger, med resten på den første; summen er alltid prisen
  * en samling er fakturert når en faktura er knyttet til en av dagene i den - også etter at dagene er endret
  * endres antallet samlinger underveis, deles det som gjenstår på samlingene som gjenstår (summen blir fortsatt prisen)
  * kurs med delt faktura fra før migrering 9 har én samling per kursdag og faktureres som før
  * tekstene på påmeldingssiden og i bekreftelsen teller samlinger, ikke kursdager
"""
from datetime import date, timedelta

import pytest

from kurs import config, daglig, db, sveiper
from kurs.integrasjoner import epost
from kurs.kjoring import Kjoring
from kurs.kursdatoer import Samling
from kurs.sveiper import PLAN_PER_SAMLING, FakturaPlan

START = date(2027, 9, 20)


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    return c


def _s(fra, dager=1, **kw):
    return Samling(fra=fra, til=fra + timedelta(days=dager - 1), start_kl="09:00", slutt_kl="16:00", timer=6, **kw)


def _tre_samlinger():
    """Samling 1 (4 dager), samling 2 (4 dager) og en veiledningsdag som egen samling."""
    return [_s(START, 4), _s(START + timedelta(days=28), 4), _s(START + timedelta(days=56))]


def _kurs(con, samlinger, pris=10000, betaling="per_samling", kode="DS1"):
    kid = db.opprett_kurs(con, kode=kode, navn="Delt kurs", datoer=[], samlinger=samlinger, idag=date(2027, 1, 1),
                          type="fysisk", sted="Bergen", pris_nok=pris, betaling=betaling, faktura_dager_for=14,
                          sharepoint_mappe=f"Kurs/{kode}")
    con.commit()
    return kid


def _meld_paa(con, kid, **paamelding):
    pid, _ = db.meld_paa(con, kid, epost="kari@example.no", fornavn="Kari", etternavn="Delt",
                         paamelding=paamelding or None)
    con.commit()
    return pid


def _fakturaer(con):
    return con.execute("""SELECT f.belop_nok, kd.dato FROM faktura f JOIN kursdag kd ON kd.id=f.kursdag_id
                          ORDER BY kd.dato""").fetchall()


def _kjor(con, idag):
    daglig.kjor(Kjoring(con, idag=idag))
    con.commit()


def test_en_faktura_per_samling_ikke_per_kursdag(con):
    kid = _kurs(con, _tre_samlinger())
    _meld_paa(con, kid)
    _kjor(con, START - timedelta(days=30))                                  # for tidlig
    assert _fakturaer(con) == []
    _kjor(con, START - timedelta(days=14))                                  # samling 1 forfaller
    assert [tuple(f) for f in _fakturaer(con)] == [(3334, START.isoformat())]
    _kjor(con, START + timedelta(days=60))                                  # resten forfaller
    _kjor(con, START + timedelta(days=60))                                  # to ganger: ingen dubletter
    assert [tuple(f) for f in _fakturaer(con)] == [
        (3334, START.isoformat()), (3333, (START + timedelta(days=28)).isoformat()),
        (3333, (START + timedelta(days=56)).isoformat())]                  # tre fakturaer - ikke ni
    assert sum(f["belop_nok"] for f in _fakturaer(con)) == 10000


def test_fakturaen_forfaller_n_dager_foer_samlingens_forste_dag(con):
    kid = _kurs(con, _tre_samlinger())
    _meld_paa(con, kid)
    _kjor(con, START - timedelta(days=14))
    _kjor(con, START + timedelta(days=13))                                  # 15 dager før samling 2: ikke ennå
    assert len(_fakturaer(con)) == 1
    _kjor(con, START + timedelta(days=14))                                  # 14 dager før samling 2
    assert len(_fakturaer(con)) == 2


def test_linjeteksten_sier_hvilken_samling_av_hvor_mange(con, monkeypatch):
    linjer = []
    ekte = sveiper._opprett
    monkeypatch.setattr(sveiper, "_opprett", lambda k, p, kd, belop, tekst: (linjer.append(tekst), ekte(k, p, kd, belop, tekst)))
    kid = _kurs(con, _tre_samlinger())
    _meld_paa(con, kid)
    _kjor(con, START - timedelta(days=14))
    assert linjer == [f"Delt kurs – Kari Delt – samling 1 av 3 ({START.isoformat()})"]


def test_deltaker_som_velger_delt_betaling_faar_en_faktura_per_samling(con):
    kid = _kurs(con, _tre_samlinger(), betaling="deltaker_velger")
    _meld_paa(con, kid, betaling="per_samling")
    _kjor(con, START + timedelta(days=60))
    assert len(_fakturaer(con)) == 3


def test_gamle_kurs_med_delt_faktura_faktureres_fortsatt_per_kursdag(con):
    datoer = [(START + timedelta(days=n)).isoformat() for n in range(3)]
    kid = db.opprett_kurs(con, kode="GML", navn="Gammelt", datoer=datoer, pris_nok=9001, betaling="per_samling",
                          faktura_dager_for=14)
    con.commit()
    _meld_paa(con, kid)
    _kjor(con, START + timedelta(days=5))
    assert [f["belop_nok"] for f in _fakturaer(con)] == [3001, 3000, 3000]  # som før migrering 9


def test_samlingen_er_fakturert_selv_om_en_dag_legges_til_foran(con):
    kid = _kurs(con, _tre_samlinger())
    _meld_paa(con, kid)
    _kjor(con, START - timedelta(days=14))
    s = db.lagrede_samlinger(con, kid)
    s[0].fra = START - timedelta(days=1)                                     # samling 1 starter en dag tidligere
    db.lagre_samlinger(con, kid, s, idag=START - timedelta(days=14))
    con.commit()
    _kjor(con, START - timedelta(days=13))
    assert len(_fakturaer(con)) == 1                                         # ingen ny faktura for samling 1


def test_nye_samlinger_underveis_deler_resten_saa_summen_blir_prisen(con):
    kid = _kurs(con, _tre_samlinger())
    _meld_paa(con, kid)
    _kjor(con, START - timedelta(days=14))                                  # samling 1: 3334
    s = db.lagrede_samlinger(con, kid)
    s.append(_s(START + timedelta(days=70)))                                 # en fjerde samling legges til
    db.lagre_samlinger(con, kid, s, idag=START)
    con.commit()
    _kjor(con, START + timedelta(days=80))
    belop = [f["belop_nok"] for f in _fakturaer(con)]
    assert belop == [3334, 2222, 2222, 2222] and sum(belop) == 10000


def test_fakturaforsoek_paa_en_annen_dag_i_samlingen_fullfores_der(con):
    kid = _kurs(con, _tre_samlinger())
    pid = _meld_paa(con, kid)
    dag2 = db.kursdager(con, kid)[1]                                         # andre dag i samling 1
    con.execute("INSERT INTO faktura_forsok (paamelding_id, kursdag_id, status) VALUES (?,?,'feilet')", (pid, dag2["id"]))
    con.commit()
    p = con.execute(sveiper.SQL_DELTAKER + " WHERE p.id=?", (pid,)).fetchone()
    gjenstaar = sveiper.delfakturaer_som_gjenstaar(con, p, sveiper.samlinger_til_fakturering(con, kid))
    assert [(nr, kd) for nr, _s_, kd, _b in gjenstaar][0] == (1, dag2["id"])   # samme dag - ikke et nytt forsøk


def test_fakturering_er_ferdig_forst_naar_forfalte_samlinger_har_faktura(con):
    kid = _kurs(con, _tre_samlinger())
    pid = _meld_paa(con, kid)
    k = Kjoring(con, idag=START - timedelta(days=14))
    p = con.execute(sveiper.SQL_DELTAKER + " WHERE p.id=?", (pid,)).fetchone()
    plan = FakturaPlan(PLAN_PER_SAMLING)
    assert not sveiper._fakturering_ferdig(k, p, plan)
    sveiper.fakturer(k, p, plan)
    con.commit()
    assert sveiper._fakturering_ferdig(k, p, plan)
    assert not sveiper._fakturering_ferdig(Kjoring(con, idag=START + timedelta(days=14)), p, plan)


def test_tekstene_teller_samlinger(con):
    kid = _kurs(con, _tre_samlinger(), betaling="deltaker_velger")
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    from kurs.web import app as webapp
    side = webapp.app.test_client().get(f"/kurs/{kurs['kode']}").get_data(as_text=True)
    assert "ca. 3 333 kr × 3" in side
    p = {"navn": "Kari Delt", "fornavn": "Kari", "betaling": "per_samling", "betaler": "person", "org_navn": None}
    _, html = epost.render("bekreftelse", p=p, kurs=kurs, dager=db.kursdager(con, kid),
                           faktura_plan=FakturaPlan(PLAN_PER_SAMLING))
    assert "Kursavgiften på 10000 kr deles på 3 samlinger" in " ".join(html.split())
