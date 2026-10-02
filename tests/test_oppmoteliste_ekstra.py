"""Oppmøtelisten og ekstradeltakerens samlingsutvalg: en ekstradeltaker vises bare på dagene i samlingene vedkommende er satt opp på
(db.sql_egen_dag, samme regel som oppmøtematrisen). Listen, «merk alle», «fjern alle» og enkeltrader følger samme funksjon
(oppmoteliste.deltakere_for_dag). Alle data er oppdiktede.
"""
import pytest

from kurs import config, db, oppmoteliste

from ekstrahjelp import DAGER, ekstra, kurs_med_samlinger, meld, samling_ider

AKTOR = "admin:test"


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _dag(con, kid, dato):
    return next(d["id"] for d in db.kursdager(con, kid) if d["dato"] == dato)


def _paa_listen(con, kid, dag):
    return {r["paamelding_id"] for r in oppmoteliste.deltakere_for_dag(con, kid, dag)}


def _bygg(con):
    """Kurs med tre samlinger. Kari er Påmeldt, Ola ekstradeltaker på hele kurset, Eva bare på samling 2, Nils på 1 og 3."""
    kid = kurs_med_samlinger(con)
    kari = meld(con, kid, "Kari")
    ola = ekstra(con, kid, "Ola")
    eva = ekstra(con, kid, "Eva", nr=[2])
    nils = ekstra(con, kid, "Nils", nr=[1, 3])
    return kid, {"kari": kari, "ola": ola, "eva": eva, "nils": nils}


def test_ekstradeltaker_vises_bare_paa_dagene_i_sitt_utvalg(con):
    kid, p = _bygg(con)
    s1, s2, s3 = (_dag(con, kid, DAGER[n][0]) for n in (1, 2, 3))
    assert _paa_listen(con, kid, s1) == {p["kari"], p["ola"], p["nils"]}
    assert _paa_listen(con, kid, s2) == {p["kari"], p["ola"], p["eva"]}
    assert _paa_listen(con, kid, s3) == {p["kari"], p["ola"], p["nils"]}


def test_ekstradeltaker_paa_hele_kurset_og_paameldt_vises_alle_dager(con):
    kid, p = _bygg(con)
    for dag in db.kursdager(con, kid):
        assert {p["kari"], p["ola"]} <= _paa_listen(con, kid, dag["id"])


def test_siste_dag_i_en_samling_folger_samlingen(con):
    kid, p = _bygg(con)
    assert p["eva"] in _paa_listen(con, kid, _dag(con, kid, DAGER[2][1]))
    assert p["eva"] not in _paa_listen(con, kid, _dag(con, kid, DAGER[1][1]))


def test_merk_alle_merker_ikke_de_som_ikke_er_satt_opp_paa_dagen(con):
    kid, p = _bygg(con)
    s1 = _dag(con, kid, DAGER[1][0])
    assert oppmoteliste.merk_alle(con, kid, s1, AKTOR) == 3
    registrert = {r["paamelding_id"] for r in con.execute("SELECT paamelding_id FROM oppmote WHERE kursdag_id=?", (s1,))}
    assert registrert == {p["kari"], p["ola"], p["nils"]}


def test_enkeltrad_utenfor_utvalget_avvises_og_endrer_ingenting(con):
    kid, p = _bygg(con)
    s1 = _dag(con, kid, DAGER[1][0])
    utfall = oppmoteliste.sett_oppmote(con, kid, s1, p["eva"], True, AKTOR)
    assert utfall.status == "ikke_i_listen"
    assert con.execute("SELECT COUNT(*) FROM oppmote WHERE paamelding_id=?", (p["eva"],)).fetchone()[0] == 0


def test_fjern_alle_roerer_ikke_oppmote_utenfor_utvalget(con):
    """Administrator kan fortsatt registrere manuelt i oppmøtematrisen på en dag utenfor utvalget; «fjern alle» på
    oppmøtelisten for den dagen rører ikke den raden (personen står ikke på listen)."""
    kid, p = _bygg(con)
    s1 = _dag(con, kid, DAGER[1][0])
    db.registrer_oppmote(con, p["eva"], s1, "manuell")
    db.registrer_oppmote(con, p["kari"], s1, "manuell")
    con.commit()
    fjernet, _ = oppmoteliste.fjern_alle(con, kid, s1, AKTOR)
    assert fjernet == 1
    igjen = {r["paamelding_id"] for r in con.execute("SELECT paamelding_id FROM oppmote WHERE kursdag_id=?", (s1,))}
    assert igjen == {p["eva"]}


def test_utvalget_endres_og_listen_folger_med(con):
    kid, p = _bygg(con)
    s1 = _dag(con, kid, DAGER[1][0])
    assert p["eva"] not in _paa_listen(con, kid, s1)
    db.sett_ekstradeltaker_samlinger(con, p["eva"], samling_ider(con, kid)[:2], aktor="admin:test")
    con.commit()
    assert p["eva"] in _paa_listen(con, kid, s1)


def test_dag_fra_annet_kurs_gir_tom_liste(con):
    kid, _ = _bygg(con)
    annet = kurs_med_samlinger(con, kode="EKS4")
    dag_annet = db.kursdager(con, annet)[0]["id"]
    assert oppmoteliste.deltakere_for_dag(con, kid, dag_annet) == []
