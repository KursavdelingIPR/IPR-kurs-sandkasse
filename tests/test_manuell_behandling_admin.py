"""Fase 9, trinn 3: eksplisitt behandling (POST) av manuelt registrerte paameldinger som har
sveiper_utsatt=1. Bruker eksisterende sveiper.kjor() ubeskaaret - ingen ny utsendelses- eller
fakturalogikk. Se ogsaa tests/test_manuell_registrering_admin.py (trinn 2) og
tests/test_manuell_paamelding.py (trinn 1)."""
from datetime import date, timedelta

import pytest

from kurs import config, db
from kurs.integrasjoner import epost, visma

RUTE = "/admin/kurs/{kid}/deltaker/{pid}/behandle"


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    return c


def _klient():
    from kurs.web import app as webapp
    return webapp.app.test_client()


def _logg_inn(klient):
    klient.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})


def _kurs(con, kode="T1", start=None, **kw):
    start = start or (date.today() + timedelta(days=30))
    return db.opprett_kurs(con, kode=kode, navn="Testkurs", datoer=[start.isoformat()],
                           sharepoint_mappe=f"Kurs/{kode}", **{"pris_nok": 1000, **kw})


def _antall_mail(con):
    return len(list(config.UTBOKS.glob("*.html"))) if config.UTBOKS.exists() else 0


def _manuell(con, kid, epost="kari@x.no", navn="Kari Nordmann", **paamelding):
    return db.meld_paa(con, kid, epost=epost, navn=navn, aktor="admin:test", tillat_utkast=True,
                       paamelding={"kilde": "admin", "sveiper_utsatt": 1, **paamelding})


# ---------------- tilgang ----------------

def test_ruten_krever_admin(con):
    kid = _kurs(con)
    pid, _ = _manuell(con, kid)
    con.commit()
    resp = _klient().post(RUTE.format(kid=kid, pid=pid))
    assert resp.status_code == 302
    assert "/admin/logg-inn" in resp.headers["Location"]
    assert con.execute("SELECT sveiper_utsatt FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == 1


def test_get_kan_ikke_utlose_behandling(con):
    kid = _kurs(con)
    pid, _ = _manuell(con, kid)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = klient.get(RUTE.format(kid=kid, pid=pid))
    assert resp.status_code == 405
    assert con.execute("SELECT sveiper_utsatt FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == 1
    assert _antall_mail(con) == 0


# ---------------- bekreftet + utsatt: hovedscenario ----------------

def test_bekreftet_utsatt_gir_bekreftelse_og_faktura(con):
    kid = _kurs(con, pris_nok=1000, fakturering="person")
    pid, status = _manuell(con, kid)
    assert status == "bekreftet"
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = klient.post(RUTE.format(kid=kid, pid=pid))
    assert resp.status_code == 302
    rad = con.execute("SELECT sveiper_kjort, sveiper_utsatt FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert rad["sveiper_utsatt"] == 0
    assert rad["sveiper_kjort"] == 1
    assert _antall_mail(con) == 1
    assert con.execute("SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", (pid,)).fetchone()[0] == 1


def test_flashmelding_viser_hva_som_skjedde(con):
    kid = _kurs(con, pris_nok=1000, fakturering="person")
    pid, _ = _manuell(con, kid)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = klient.post(RUTE.format(kid=kid, pid=pid))
    tekst = klient.get(resp.headers["Location"]).get_data(as_text=True)
    assert "sendt" in tekst.lower() and "fakturering" in tekst.lower()


# ---------------- venteliste + utsatt ----------------

def test_venteliste_utsatt_gir_ventelistebeskjed_ingen_faktura(con):
    kid = _kurs(con, kapasitet=1, pris_nok=1000, fakturering="person")
    db.meld_paa(con, kid, epost="forst@x.no", navn="Forst")
    pid, status = _manuell(con, kid)
    assert status == "venteliste"
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(RUTE.format(kid=kid, pid=pid))
    rad = con.execute("SELECT sveiper_kjort, sveiper_utsatt, status FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert rad["sveiper_utsatt"] == 0
    assert rad["status"] == "venteliste"
    assert rad["sveiper_kjort"] == 0  # ventelistelogikken setter aldri denne
    assert con.execute("SELECT 1 FROM utsending_logg WHERE mottaker='kari@x.no' AND type='venteliste'").fetchone()
    assert con.execute("SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", (pid,)).fetchone()[0] == 0


def test_venteliste_knapp_har_riktig_tekst_ikke_fakturaordlyd(con):
    kid = _kurs(con, kapasitet=1)
    db.meld_paa(con, kid, epost="forst@x.no", navn="Forst")
    pid, _ = _manuell(con, kid)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
    assert "Send ventelistebeskjed nå" in tekst
    assert "behandle fakturering" not in tekst.lower()


# ---------------- utkast: ingen knapp, POST avvises ----------------

def test_utkast_viser_ingen_knapp(con):
    kid = _kurs(con, status="utkast")
    pid, _ = _manuell(con, kid)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
    assert RUTE.format(kid=kid, pid=pid) not in tekst
    assert "ikke publisert" in tekst.lower()


def test_utkast_post_avvises_og_nullstiller_ikke_sveiper_utsatt(con):
    kid = _kurs(con, status="utkast")
    pid, _ = _manuell(con, kid)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(RUTE.format(kid=kid, pid=pid))
    rad = con.execute("SELECT sveiper_kjort, sveiper_utsatt FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert rad["sveiper_utsatt"] == 1
    assert rad["sveiper_kjort"] == 0
    assert _antall_mail(con) == 0
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 0


# ---------------- avlyst: ingen knapp, POST avvises ----------------

def test_avlyst_viser_ingen_knapp(con):
    kid = _kurs(con)
    pid, _ = _manuell(con, kid)
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kid,))
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
    assert RUTE.format(kid=kid, pid=pid) not in tekst
    assert "avlyst" in tekst.lower()


def test_avlyst_post_avvises(con):
    kid = _kurs(con)
    pid, _ = _manuell(con, kid)
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kid,))
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(RUTE.format(kid=kid, pid=pid))
    rad = con.execute("SELECT sveiper_kjort, sveiper_utsatt FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert rad["sveiper_utsatt"] == 1
    assert rad["sveiper_kjort"] == 0
    assert _antall_mail(con) == 0


# ---------------- avsluttet + utsatt: recovery-regelen ----------------

def test_avsluttet_utsatt_kan_behandles_eksplisitt(con):
    kid = _kurs(con, pris_nok=1000, fakturering="person")
    pid, _ = _manuell(con, kid)
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = klient.post(RUTE.format(kid=kid, pid=pid))
    assert resp.status_code == 302
    rad = con.execute("SELECT sveiper_kjort, sveiper_utsatt FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert rad["sveiper_kjort"] == 1
    assert rad["sveiper_utsatt"] == 0
    assert _antall_mail(con) == 1
    assert con.execute("SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", (pid,)).fetchone()[0] == 1


# ---------------- aapen/full/aktiv ----------------

@pytest.mark.parametrize("status", ["aapen", "full", "aktiv"])
def test_aapen_full_aktiv_fungerer(con, status):
    kid = _kurs(con, pris_nok=1000, fakturering="person")
    pid, _ = _manuell(con, kid)
    con.execute("UPDATE kurs SET status=? WHERE id=?", (status, kid))
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(RUTE.format(kid=kid, pid=pid))
    assert con.execute("SELECT sveiper_kjort FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == 1


# ---------------- idempotens: samme handling to ganger ----------------

def test_dobbelt_klikk_gir_ikke_dobbel_epost_eller_faktura(con):
    kid = _kurs(con, pris_nok=1000, fakturering="person")
    pid, _ = _manuell(con, kid)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(RUTE.format(kid=kid, pid=pid))
    resp2 = klient.post(RUTE.format(kid=kid, pid=pid))
    assert _antall_mail(con) == 1
    assert con.execute("SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", (pid,)).fetchone()[0] == 1
    tekst2 = klient.get(resp2.headers["Location"]).get_data(as_text=True)
    assert "ikke lenger holdt tilbake" in tekst2.lower()


# ---------------- feilhaandtering: feil skal ikke fremstaas som vellykket ----------------

def test_fakturafeil_presenteres_ikke_som_vellykket(con, monkeypatch):
    kid = _kurs(con, pris_nok=1000, fakturering="person")
    pid, _ = _manuell(con, kid)
    con.commit()

    def _feil(g):
        raise RuntimeError("Visma nede")
    monkeypatch.setattr(visma, "fakturer", _feil)

    klient = _klient()
    _logg_inn(klient)
    resp = klient.post(RUTE.format(kid=kid, pid=pid))
    rad = con.execute("SELECT sveiper_kjort, sveiper_utsatt FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert rad["sveiper_kjort"] == 0  # hele kjeden ble IKKE fullfort
    assert rad["sveiper_utsatt"] == 0  # forblir 0 - eksisterende recovery kan forsoke igjen
    assert con.execute("SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", (pid,)).fetchone()[0] == 0
    tekst = klient.get(resp.headers["Location"]).get_data(as_text=True)
    assert "ikke fullført" in tekst.lower()
    assert "er sendt og fakturering er behandlet" not in tekst.lower()


def test_epostfeil_for_venteliste_presenteres_ikke_som_vellykket(con, monkeypatch):
    kid = _kurs(con, kapasitet=1)
    db.meld_paa(con, kid, epost="forst@x.no", navn="Forst")
    pid, _ = _manuell(con, kid)
    con.commit()

    def _feil(til, emne, html, vedlegg=None, kopi=None):
        raise RuntimeError("SMTP nede")
    monkeypatch.setattr(epost, "send", _feil)

    klient = _klient()
    _logg_inn(klient)
    resp = klient.post(RUTE.format(kid=kid, pid=pid))
    rad = con.execute("SELECT sveiper_utsatt FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert rad["sveiper_utsatt"] == 0
    assert not con.execute(
        "SELECT 1 FROM utsending_logg WHERE mottaker='kari@x.no' AND type='venteliste'").fetchone()
    tekst = klient.get(resp.headers["Location"]).get_data(as_text=True)
    assert "ikke fullført" in tekst.lower() or "ikke sendt" in tekst.lower()


# ---------------- personvern ----------------

def test_ingen_personopplysninger_i_hendelsesloggen(con):
    kid = _kurs(con, pris_nok=1000, fakturering="person")
    pid, _ = _manuell(con, kid, epost="hemmelig.person@x.no", navn="Hemmelig Person")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(RUTE.format(kid=kid, pid=pid))
    rad = con.execute(
        "SELECT detaljer, aktor FROM hendelse WHERE handling='manuell_behandling_utlost' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert rad is not None
    assert "hemmelig" not in (rad["detaljer"] or "").lower()
    assert rad["aktor"].startswith("admin:")
