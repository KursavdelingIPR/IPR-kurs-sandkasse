"""«Send fakturaen nå» i deltakervinduet (Camilla 14.10.2026).

En samlet faktura holdes til seks måneder før første kursdag. Deltakerne kan be om å få den tidligere (vilkårene på påmeldingssiden), og da
kan administrator sende den med en gang. Den går gjennom samme motor som ellers, så det blir aldri to fakturaer."""
from datetime import date, timedelta

import pytest

from kurs import config, daglig, db
from kurs.integrasjoner import epost, visma
from kurs.kjoring import Kjoring

IDAG = date(2027, 1, 10)
NAER = date(2027, 2, 1)          # under seks måneder fram: fakturaen lages med en gang
LANGT = date(2027, 9, 20)        # over seks måneder fram: fakturaen holdes til 2027-03-20


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    return c


@pytest.fixture
def ute(monkeypatch):
    kall = {"visma": []}
    monkeypatch.setattr(epost, "send", lambda *a, **kw: None)
    ekte = visma.fakturer

    def visma_fakturer(g):
        kall["visma"].append(g.belop_nok)
        return ekte(g)
    monkeypatch.setattr(visma, "fakturer", visma_fakturer)
    return kall


@pytest.fixture(autouse=True)
def fast_dato(monkeypatch):
    from kurs.web import app as webapp
    monkeypatch.setattr(webapp, "_idag", lambda: IDAG)


def _klient():
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def _kurs_og_paamelding(con, start, betaling="samlet"):
    datoer = [(start + timedelta(days=30 * n)).isoformat() for n in range(2)]
    kid = db.opprett_kurs(con, kode="F1", navn="Testkurs", datoer=datoer, sharepoint_mappe="Kurs/F1",
                          pris_nok=1000, fakturering="person", betaling=betaling)
    con.commit()
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="Navn", etternavn="Navnesen")
    con.commit()
    from kurs import sveiper
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)
    con.commit()
    return kid, pid


def _fakturaer(pid):
    return [r[0] for r in db.koble(config.DB_STI).execute("SELECT belop_nok FROM faktura WHERE paamelding_id=?", (pid,))]


def _side(klient, kid, pid):
    return klient.get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)


def test_planlagt_faktura_kan_sendes_naa_og_aldri_to_ganger(con, ute):
    kid, pid = _kurs_og_paamelding(con, LANGT)
    assert _fakturaer(pid) == []
    klient = _klient()
    side = _side(klient, kid, pid)
    assert "Fakturaen er planlagt sendt 20.03.2027" in side and "Send fakturaen nå" in side
    assert 'form="skjema-faktura-naa"' in side and 'id="skjema-faktura-naa"' in side and "data-bekreft=" in side
    svar = klient.post(f"/admin/kurs/{kid}/deltaker/{pid}/faktura-naa", follow_redirects=True).get_data(as_text=True)
    assert "er laget og sendt (den var planlagt 20.03.2027)" in svar
    assert _fakturaer(pid) == [1000] and ute["visma"] == [1000]
    assert "Send fakturaen nå" not in svar                                    # boksen er borte når fakturaen er laget
    svar = klient.post(f"/admin/kurs/{kid}/deltaker/{pid}/faktura-naa", follow_redirects=True).get_data(as_text=True)
    assert "ingen planlagt faktura" in svar and _fakturaer(pid) == [1000]       # dobbeltklikk: ingen ny faktura
    for dag in (IDAG, date(2027, 3, 20)):                                       # morgenjobben lager heller ingen ny
        daglig.kjor(Kjoring(con, idag=dag))
    assert _fakturaer(pid) == [1000] and ute["visma"] == [1000]
    hendelse = db.koble(config.DB_STI).execute("SELECT detaljer FROM hendelse WHERE handling='faktura_tidligere'").fetchall()
    assert len(hendelse) == 1 and "2027-03-20" in hendelse[0][0]
    logger = klient.get(f"/admin/kurs/{kid}/deltaker/{pid}/logger").get_data(as_text=True)
    assert "Fakturaen ble sendt tidligere enn planlagt" in logger and "Planlagt dato var 20.03.2027." in logger


def test_ingen_knapp_naar_fakturaen_alt_er_laget(con, ute):
    kid, pid = _kurs_og_paamelding(con, NAER)
    assert _fakturaer(pid) == [1000]
    assert "Send fakturaen nå" not in _side(_klient(), kid, pid)


def test_ingen_knapp_ved_faktura_per_samling(con, ute):
    kid, pid = _kurs_og_paamelding(con, LANGT, "per_samling")
    assert "Send fakturaen nå" not in _side(_klient(), kid, pid)
    svar = _klient().post(f"/admin/kurs/{kid}/deltaker/{pid}/faktura-naa", follow_redirects=True).get_data(as_text=True)
    assert "ingen planlagt faktura" in svar and _fakturaer(pid) == []


def test_lesetilgang_ser_datoen_men_ikke_knappen(con, ute):
    kid, pid = _kurs_og_paamelding(con, LANGT)
    klient = _klient()
    db.koble(config.DB_STI).execute("UPDATE admin_bruker SET rolle='lese'").connection.commit()
    side = _side(klient, kid, pid)
    assert "Fakturaen er planlagt sendt 20.03.2027" in side and "Send fakturaen nå" not in side
    klient.post(f"/admin/kurs/{kid}/deltaker/{pid}/faktura-naa")
    assert _fakturaer(pid) == []
