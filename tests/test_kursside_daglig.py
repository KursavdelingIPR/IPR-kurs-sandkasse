"""Kursside i morgenjobben (kurs/daglig.py, steg 9): gruppelister (tabeller med navn) tømmes 30 dager etter siste kursdag.

Jobben er idempotent (CLAUDE.md regel 5): kjøres den to ganger samme dag, skjer ingenting andre gang. Tørrkjøring endrer ingenting.
"""
import json
from datetime import timedelta

import pytest

from kurs import config, daglig, db
from kurs import sideinnhold as si
from kurs import sidelager
from kurs.kjoring import Kjoring

from kurssidehjelp import IDAG, dokument, lag_kurs, ny_database, skriv_side, tekstblokk


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    yield c
    c.close()


def _tabellside(con, kode="K1"):
    kid = lag_kurs(con, kode)
    dok = dokument({"id": "b_tabell01", "type": "tabell", "tittel": "Grupper",
                    "data": {"kolonner": ["Gruppe", "Deltakere"], "rader": [["1", "Kari N., Ola H."]]}}, tekstblokk("Tekst"))
    skriv_side(con, kid, dok)
    return kid


def _rader(con, kid, kolonne="utkast"):
    dok = si.les(con.execute(f"SELECT {kolonne} FROM kursside WHERE kurs_id=?", (kid,)).fetchone()[0])
    return next(b["data"]["rader"] for b in dok["blokker"] if b["type"] == "tabell")


def _antall_hendelser(con):
    return con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='kursside_tabeller_tomt'").fetchone()[0]


def _siste(con, kid):
    return sidelager.siste_kursdag(con, kid)


def test_tabellene_tommes_ikke_foer_30_dager_etter_siste_kursdag(con):
    kid = _tabellside(con)
    k = Kjoring(con, idag=_siste(con, kid) + timedelta(days=30))
    daglig._kursside(k)
    assert _rader(con, kid) == [["1", "Kari N., Ola H."]] and _antall_hendelser(con) == 0 and k.utskrift == []


def test_tabellene_tommes_dagen_etter_30_dagers_grensen_i_utkast_publisert_og_versjoner(con):
    kid = _tabellside(con)
    k = Kjoring(con, idag=_siste(con, kid) + timedelta(days=31))
    daglig._kursside(k)
    con.commit()
    assert _rader(con, kid) == [] and _rader(con, kid, "publisert") == []
    for v in con.execute("SELECT innhold FROM kursside_versjon WHERE kurs_id=?", (kid,)):
        assert next(b["data"]["rader"] for b in si.les(v["innhold"])["blokker"] if b["type"] == "tabell") == []
    assert any("tømte gruppetabeller på Min side for 1 kurs" in linje for linje in k.utskrift)
    assert _antall_hendelser(con) == 1
    detaljer = json.loads(con.execute("SELECT detaljer FROM hendelse WHERE handling='kursside_tabeller_tomt'").fetchone()[0])
    assert detaljer == {"antall_kurs": 1}                                                 # bare et tall: aldri navn eller innhold


def test_kjort_to_ganger_samme_dag_skjer_ingenting_andre_gang(con):
    kid = _tabellside(con)
    idag = _siste(con, kid) + timedelta(days=45)
    daglig._kursside(Kjoring(con, idag=idag))
    con.commit()
    versjon = con.execute("SELECT versjon FROM kursside WHERE kurs_id=?", (kid,)).fetchone()[0]
    andre = Kjoring(con, idag=idag)
    daglig._kursside(andre)
    con.commit()
    assert andre.utskrift == [] and _antall_hendelser(con) == 1
    assert con.execute("SELECT versjon FROM kursside WHERE kurs_id=?", (kid,)).fetchone()[0] == versjon


def test_torrkjoering_teller_bare_og_endrer_ingenting(con):
    kid = _tabellside(con)
    foer = con.execute("SELECT utkast, publisert, versjon FROM kursside WHERE kurs_id=?", (kid,)).fetchone()
    k = Kjoring(con, idag=_siste(con, kid) + timedelta(days=60), tor=True)
    daglig._kursside(k)
    assert any("[TØRR] ville tømt gruppetabeller på Min side for 1 kurs" in linje for linje in k.utskrift)
    assert tuple(con.execute("SELECT utkast, publisert, versjon FROM kursside WHERE kurs_id=?", (kid,)).fetchone()) == tuple(foer)
    assert _antall_hendelser(con) == 0


def test_hele_morgenjobben_kjort_to_ganger_tommer_bare_en_gang(con, monkeypatch):
    kid = _tabellside(con)
    idag = _siste(con, kid) + timedelta(days=40)
    daglig.kjor(Kjoring(con, idag=idag))
    assert _rader(con, kid) == [] and _antall_hendelser(con) == 1
    andre = Kjoring(con, idag=idag)
    daglig.kjor(andre)
    assert _antall_hendelser(con) == 1 and _rader(con, kid) == []
    assert any(linje.startswith("9. Min side") for linje in andre.utskrift) and andre.feil == []


def test_morgenjobben_har_steg_9_og_feil_i_steget_stopper_ikke_resten(con, monkeypatch):
    _tabellside(con)

    def feiler(*a, **kw):
        raise RuntimeError("test")
    monkeypatch.setattr(sidelager, "tom_gamle_tabeller", feiler)
    k = Kjoring(con, idag=IDAG + timedelta(days=400))
    daglig.kjor(k)
    assert k.feil == ["kursside"] and any("FEIL i «kursside»" in linje for linje in k.utskrift)
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='daglig_feil'").fetchone()[0] == 1


def test_endringen_beroerer_ikke_kurs_der_kursdagene_ikke_er_eldre_enn_grensen(con):
    gammel = _tabellside(con, "GAMMEL")
    ny = lag_kurs(con, "NY", start=IDAG + timedelta(days=200))
    skriv_side(con, ny, dokument({"id": "b_tabell01", "type": "tabell", "tittel": "Grupper",
                                  "data": {"kolonner": ["Gruppe"], "rader": [["Gruppe 1"]]}}))
    daglig._kursside(Kjoring(con, idag=IDAG + timedelta(days=100)))
    assert _rader(con, gammel) == [] and _rader(con, ny) == [["Gruppe 1"]]
