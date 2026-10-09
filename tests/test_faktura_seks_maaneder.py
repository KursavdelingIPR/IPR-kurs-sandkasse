"""Fakturatidspunktet ved påmelding (brukerens regel 30.09.2026): «Faktura sendes tidligst ut 6 mnd i forkant av oppstart. Ellers skal
faktura sendes ut automatisk ved påmelding.»

Regelen i koden (kurs/sveiper.py, faktura_plan og seks_maaneder_for): «6 måneder» er seks KALENDERMÅNEDER før FØRSTE kursdag (ikke 180
dager: seks måneder er 181-184 dager). Er dagens dato FØR den datoen, venter fakturaen; er den PÅ datoen eller senere, lages fakturaen.

  * Påmelding dagen FØR grensen (eller tidligere): ingen faktura. Påmeldingen får faktura_tidligst_dato = grensen (og bekreftelsen
    sendes som vanlig), og morgenjobben (daglig steg 1c, sveiper.utsatte_fakturaer) lager fakturaen den dagen grensen nås.
  * Påmelding PÅ grensen eller senere: fakturaen lages i samme forespørsel som påmeldingen (sveiper.kjor).
  * Fakturaen har deltakerens adresse med, enten den lages ved påmeldingen eller av morgenjobben (adressen kopieres til påmeldingen
    ved registreringen, og er alltid påkrevd).

Den rene regelen og selve morgenjobben er testet i test_faktura_utsatt.py; her testes GRENSEN gjennom de to veiene som fakturerer ved
påmelding fra utsiden (det offentlige skjemaet og webhooken), sammen med adressen som følger med til Visma. Delfakturaer per samling har
en egen regel (faktura_dager_for, test_faktura_per_samling.py) og røres ikke. Alle testdata er fiktive."""
import hashlib
import hmac
import json
from datetime import date, timedelta

import pytest

from adressehjelp import ADRESSE
from kurs import config, daglig, db
from kurs.integrasjoner import visma
from kurs.kjoring import Kjoring
from kurs.sveiper import seks_maaneder_for

# (første kursdag, dagen fakturaen tidligst lages): en vanlig dato og en månedsslutt (31. august -> siste dag i februar)
KURSSTART = [(date(2099, 9, 20), date(2099, 3, 20)), (date(2099, 8, 31), date(2099, 2, 28))]
VEIER = ["skjema", "webhook"]


@pytest.fixture(autouse=True)
def _demo(monkeypatch):
    monkeypatch.setattr(config, "DEMO", True)


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    monkeypatch.setattr(config, "BASE_URL", "https://kurs.eksempel.no")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


@pytest.fixture
def grunnlag(monkeypatch):
    """Fakturagrunnlagene som sendes til Visma (i demo lages ingen ekte faktura)."""
    ut = []

    def fake(g):
        ut.append(g)
        return visma.Fakturaresultat(visma_id=f"v{len(ut)}", faktura_nr=f"F{len(ut)}", kunde_id="k")
    monkeypatch.setattr(visma, "fakturer", fake)
    return ut


def _kurs(con, start: date, kode="A1"):
    kid = db.opprett_kurs(con, kode=kode, navn="Eksempelkurs", datoer=[start.isoformat(), (start + timedelta(days=1)).isoformat()],
                          type="fysisk", sted="Eksempelsted", pris_nok=4500, fakturering="person", betaling="samlet",
                          kapasitet=20, sharepoint_mappe=f"K/{kode}")
    con.commit()
    return kid


def _klient(idag: date):
    """En klient som «lever» på `idag` (demo kan spole datoen): påmeldingen og fakturamotoren bruker den datoen."""
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    with k.session_transaction() as s:
        s["demo_dato"] = idag.isoformat()
    return k


def _etternavn(nr: int) -> str:
    return "Testperson" + "ABCDEFGH"[nr - 1]


def _meld_paa(vei: str, idag: date, nr: int, kode="A1"):
    """Melder person nr `nr` (egen e-post) på kurset slik ipr.no-skjemaet eller nettsiden gjør, med adressen. Returnerer e-posten."""
    epost = f"person{nr}@eksempel.no"
    if vei == "skjema":
        r = _klient(idag).post(f"/kurs/{kode}", data={"fornavn": "Test", "etternavn": _etternavn(nr), "epost": epost,
                                                       "samtykke": "on", "samtykke_lagring": "on", **ADRESSE})
        assert r.status_code == 200 and "påmeldt" in r.get_data(as_text=True).lower(), r.get_data(as_text=True)[:300]
    else:
        body = json.dumps({"fornavn": "Test", "etternavn": _etternavn(nr), "epost": epost, "kurs": kode, **ADRESSE}).encode()
        sig = hmac.new(config.WEBHOOK_HEMMELIG.encode(), body, hashlib.sha256).hexdigest()
        r = _klient(idag).post("/api/paamelding", data=body, content_type="application/json", headers={"X-IPR-Signatur": sig})
        assert r.status_code == 201, r.get_json()
    return epost


def _paamelding(con, epost):
    return con.execute("SELECT p.* FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE d.epost=?", (epost,)).fetchone()


def _antall_faktura(con, epost) -> int:
    return con.execute("SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", (_paamelding(con, epost)["id"],)).fetchone()[0]


def _sendt_adresse(g) -> dict:
    return {"adresse": g.adresse, "postnr": g.postnr, "poststed": g.sted}


def _lagret_fakturaadresse(p) -> dict:
    return {"adresse": p["faktura_adresse"], "postnr": p["faktura_postnr"], "poststed": p["faktura_sted"]}


# ============================ regelen: seks kalendermåneder før første kursdag ============================

@pytest.mark.parametrize("start, grense", KURSSTART)
def test_grensen_er_seks_kalendermaaneder_foer_foerste_kursdag_og_ikke_180_dager(start, grense):
    assert seks_maaneder_for(start) == grense
    assert 181 <= (start - grense).days <= 184                                   # 181-184 dager, aldri 180
    assert grense != start - timedelta(days=180)


# ============================ ved påmelding: før grensen ingen faktura, fra grensen faktura med adressen ============================

@pytest.mark.parametrize("vei", VEIER)
@pytest.mark.parametrize("start, grense", KURSSTART)
def test_paamelding_foer_grensen_gir_ingen_faktura_men_en_planlagt_dato(con, grunnlag, vei, start, grense):
    _kurs(con, start)
    for nr, idag in enumerate((grense - timedelta(days=200), grense - timedelta(days=30), grense - timedelta(days=1)), start=1):
        epost = _meld_paa(vei, idag, nr)
        p = _paamelding(con, epost)
        assert p["status"] == "bekreftet" and p["sveiper_kjort"] == 1               # bekreftelsen er behandlet som vanlig
        assert _antall_faktura(con, epost) == 0, idag
        assert p["faktura_tidligst_dato"] == grense.isoformat(), idag                 # planlagt: den dagen grensen nås
        assert _lagret_fakturaadresse(p) == ADRESSE                                   # adressen er klar til den dagen
    assert grunnlag == []                                                             # ingenting gikk til Visma


@pytest.mark.parametrize("vei", VEIER)
@pytest.mark.parametrize("start, grense", KURSSTART)
def test_paamelding_paa_grensen_og_senere_gir_faktura_med_adressen_med_en_gang(con, grunnlag, vei, start, grense):
    _kurs(con, start)
    dager = (grense, grense + timedelta(days=1), start - timedelta(days=1))          # på grensen, dagen etter, dagen før kursstart
    for nr, idag in enumerate(dager, start=1):
        epost = _meld_paa(vei, idag, nr)
        p = _paamelding(con, epost)
        assert _antall_faktura(con, epost) == 1, idag                                # faktura i samme forespørsel som påmeldingen
        assert p["faktura_tidligst_dato"] is None and p["sveiper_kjort"] == 1
        assert _sendt_adresse(grunnlag[-1]) == ADRESSE, idag                          # og den har adressen med
        assert grunnlag[-1].kunde_navn == f"Test {_etternavn(nr)}" and grunnlag[-1].er_privatperson
    assert len(grunnlag) == len(dager)


# ============================ morgenjobben tar den utsatte fakturaen på grensen, med adressen ============================

@pytest.mark.parametrize("start, grense", KURSSTART)
def test_utsatt_faktura_lages_av_morgenjobben_paa_grensen_med_adressen_og_bare_en_gang(con, grunnlag, start, grense):
    _kurs(con, start)
    epost = _meld_paa("skjema", grense - timedelta(days=40), 1)
    for dag in (grense - timedelta(days=30), grense - timedelta(days=1)):
        daglig.kjor(Kjoring(con, idag=dag))
        con.commit()
        assert _antall_faktura(con, epost) == 0 and grunnlag == [], dag             # dagen før grensen: fortsatt ingen faktura
    daglig.kjor(Kjoring(con, idag=grense))
    con.commit()
    assert _antall_faktura(con, epost) == 1 and len(grunnlag) == 1                    # på grensen: fakturaen lages
    assert _sendt_adresse(grunnlag[0]) == ADRESSE
    for dag in (grense, grense + timedelta(days=1), start):                           # kjøres den igjen: ingenting mer (idempotent)
        daglig.kjor(Kjoring(con, idag=dag))
        con.commit()
    assert _antall_faktura(con, epost) == 1 and len(grunnlag) == 1
    assert _paamelding(con, epost)["faktura_tidligst_dato"] == grense.isoformat()     # datoen beholdes som historikk
