"""Innsjekk mot brukerens beslutning (kontroll 30.09.2026): QR-koden er hovedløsningen. Er deltakeren innlogget, identifiserer innloggingen henne
(ett trykk); ellers brukes e-post. Får hun ikke skannet, kan hun registrere oppmøte på Min side (etter innlogging), og på fysiske og hybride kurs
kreves dagens kode, så ingen kan krysse seg av hjemmefra. Bare én registrering per dag. Ingen «husk meg». INNSJEKK_KREVER_INNLOGGING er av som
standard. Testene her fyller hull i de eksisterende innsjekk-testene (test_kursside_innsjekk.py); bare oppdiktede data."""
import re
from datetime import timedelta
from pathlib import Path

import pytest

from kurs import config, db

from kurssidehjelp import IDAG, deltaker_klient, fast_dato, lag_deltaker, ny_database

ROT = Path(__file__).resolve().parent.parent


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    fast_dato(monkeypatch)
    yield c
    c.close()


def _kurs(con, kode, type_, dager=(0,), **felt) -> int:
    datoer = [(IDAG + timedelta(days=d)).isoformat() for d in dager]
    kid = db.opprett_kurs(con, kode=kode, navn=f"Kurs {kode}", datoer=datoer, type=type_, sted=None if type_ == "digital" else "Bergen",
                          status="aapen", sharepoint_mappe=f"Kurs/{kode}", **felt)
    con.commit()
    return kid


def _oppmote(con) -> list:
    return [tuple(r) for r in con.execute("SELECT paamelding_id, kursdag_id, kilde FROM oppmote ORDER BY paamelding_id, kursdag_id")]


def _dag(con, kid, dato=None):
    return con.execute("SELECT id, dato, innsjekk_kode, innsjekk_token FROM kursdag WHERE kurs_id=? AND dato=?", (kid, (dato or IDAG).isoformat())).fetchone()


# ============================ hjemmefra: dagens kode kreves på fysiske og hybride kurs ============================

@pytest.mark.parametrize("type_", ["fysisk", "hybrid"])
def test_ingen_kan_krysse_seg_av_hjemmefra_paa_fysiske_og_hybride_kurs(con, type_):
    """Uten dagens kode registreres ingenting: feltet mangler, er tomt, er feil, er gårsdagens/morgendagens kode eller et annet kurs sin kode.
    Med riktig kode registreres oppmøtet én gang (kilde «kode»), og et nytt forsøk endrer ingenting."""
    kid = _kurs(con, "K1", type_, dager=(-1, 0, 1))
    annet = _kurs(con, "K2", "fysisk")
    did, pid = lag_deltaker(con, kid, "kari@example.no")
    k = deltaker_klient(did)
    idag_kode = _dag(con, kid)["innsjekk_kode"]
    forsok = [{}, {"kode": ""}, {"kode": "   "}, {"kode": "ZZZZZZ"}, {"kode": _dag(con, kid, IDAG - timedelta(days=1))["innsjekk_kode"]},
              {"kode": _dag(con, kid, IDAG + timedelta(days=1))["innsjekk_kode"]}, {"kode": _dag(con, annet)["innsjekk_kode"]}]
    for data in forsok:
        r = k.post("/kurs/K1/deltakerside/oppmote", data=data)
        assert r.status_code == 302, data
        assert _oppmote(con) == [], data
    assert k.post("/kurs/K1/deltakerside/oppmote", data={"kode": idag_kode}).status_code == 302
    assert _oppmote(con) == [(pid, _dag(con, kid)["id"], "kode")]
    k.post("/kurs/K1/deltakerside/oppmote", data={"kode": idag_kode})                        # samme dag en gang til
    assert len(_oppmote(con)) == 1


def test_kodekravet_gjelder_ogsaa_naar_kurset_ikke_har_en_lagret_kursside(con):
    """Standarden (ingen rad i kursside) er at koden kreves: kravet er ikke noe en administrator må huske å slå på."""
    kid = _kurs(con, "K1", "fysisk")
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    assert con.execute("SELECT COUNT(*) FROM kursside").fetchone()[0] == 0
    deltaker_klient(did).post("/kurs/K1/deltakerside/oppmote", data={})
    assert _oppmote(con) == []
    kort = deltaker_klient(did).get("/min-side").get_data(as_text=True)
    assert 'name="kode"' in kort                                                            # Mine kurs viser kodefeltet


def test_mine_kurs_og_min_side_krever_innlogging_for_nett_registrering(con):
    kid = _kurs(con, "K1", "fysisk")
    lag_deltaker(con, kid, "kari@example.no")
    anonym = deltaker_klient()
    r = anonym.post("/kurs/K1/deltakerside/oppmote", data={"kode": _dag(con, kid)["innsjekk_kode"]})
    assert r.status_code in (302, 400) and _oppmote(con) == []
    assert anonym.get("/kurs/K1/deltakerside").status_code == 302 and anonym.get("/min-side").status_code == 302


# ============================ QR: innlogget eller e-post, aldri «husk meg» ============================

def test_qr_med_e_post_gir_hverken_innlogging_eller_husk_meg(con):
    kid = _kurs(con, "K1", "fysisk")
    did, pid = lag_deltaker(con, kid, "kari@example.no")
    token = _dag(con, kid)["innsjekk_token"]
    anonym = deltaker_klient()
    r = anonym.post(f"/innsjekk/{token}", data={"epost": "kari@example.no", "husk": "1", "husk_meg": "on"})
    assert r.status_code == 200 and _oppmote(con) == [(pid, _dag(con, kid)["id"], "qr")]
    with anonym.session_transaction() as s:
        assert "deltaker_id" not in s                                                       # e-post gir aldri en økt
    assert anonym.get("/min-side").status_code == 302
    for mal in ("innsjekk.html", "innsjekk_resultat.html"):
        tekst = (ROT / "kurs" / "web" / "templates" / mal).read_text(encoding="utf-8").lower()
        assert "husk" not in tekst and 'type="checkbox"' not in tekst, mal


def test_qr_innlogget_identifiseres_av_innloggingen_ikke_av_tastet_e_post(con):
    kid = _kurs(con, "K1", "fysisk")
    did, pid = lag_deltaker(con, kid, "kari@example.no")
    andre, _ = lag_deltaker(con, kid, "ola@example.no", fornavn="Ola")
    token = _dag(con, kid)["innsjekk_token"]
    k = deltaker_klient(did)
    r = k.post(f"/innsjekk/{token}", data={"epost": "ola@example.no"})                      # en innlogget kan ikke sjekke inn en annen
    assert r.status_code == 200 and [o[0] for o in _oppmote(con)] == [pid]


# ============================ INNSJEKK_KREVER_INNLOGGING er av som standard ============================

def test_innsjekk_krever_innlogging_er_av_som_standard_i_koden_og_dokumentasjonen():
    kilde = (ROT / "kurs" / "config.py").read_text(encoding="utf-8")
    assert re.search(r'INNSJEKK_KREVER_INNLOGGING\s*=\s*get\("INNSJEKK_KREVER_INNLOGGING",\s*"0"\)\s*==\s*"1"', kilde)
    assert re.search(r"^INNSJEKK_KREVER_INNLOGGING=0\b", (ROT / ".env.example").read_text(encoding="utf-8"), re.M)
    assert re.search(r"\| `INNSJEKK_KREVER_INNLOGGING` \| `0` \| `0` \|", (ROT / "DEPLOYMENT.md").read_text(encoding="utf-8"))


def test_med_bryteren_paa_registreres_ingen_paa_e_post_alene(con, monkeypatch):
    monkeypatch.setattr(config, "INNSJEKK_KREVER_INNLOGGING", True)
    kid = _kurs(con, "K1", "fysisk")
    did, pid = lag_deltaker(con, kid, "kari@example.no")
    dag = _dag(con, kid)
    anonym = deltaker_klient()
    r1 = anonym.post(f"/innsjekk/{dag['innsjekk_token']}", data={"epost": "kari@example.no"})
    r2 = anonym.post("/kurs/K1/deltakerside/oppmote", data={"kode": dag["innsjekk_kode"], "epost": "kari@example.no"})      # Min side krever innlogging
    assert "Logg inn først" in r1.get_data(as_text=True) and r2.status_code in (302, 400)
    assert anonym.post("/innsjekk", data={"kode": dag["innsjekk_kode"], "epost": "kari@example.no"}).status_code == 404       # kodesiden er fjernet
    assert _oppmote(con) == []
    assert deltaker_klient(did).post(f"/innsjekk/{dag['innsjekk_token']}").status_code == 200 and len(_oppmote(con)) == 1
