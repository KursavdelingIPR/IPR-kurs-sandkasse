"""Fase 11, trinn 2: bulk-forhåndsvisning (behandlingsbar / ikke + årsak).

INGEN databaseendring, INGEN e-post/Visma/faktura/sveiper.kjor() i dette trinnet - kun lesing og
klassifisering. Selve behandlingen bygges i trinn 3.
"""
from datetime import date, timedelta

import pytest

from kurs import config, db
from kurs.integrasjoner import epost, visma
from kurs.web import app as app_modul

RUTE = "/admin/kurs/{kid}/deltakere/bulk/forhandsvis"


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


def _manuell(con, kid, epost="kari@x.no", navn="Kari Nordmann", **paamelding):
    return db.meld_paa(con, kid, epost=epost, navn=navn, aktor="admin:test", tillat_utkast=True,
                       paamelding={"kilde": "admin", "sveiper_utsatt": 1, **paamelding})


def _antall_mail(con):
    return len(list(config.UTBOKS.glob("*.html"))) if config.UTBOKS.exists() else 0


def _snapshot(con):
    return (
        con.execute("SELECT COUNT(*) FROM deltaker").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM hendelse").fetchone()[0],
    )


def _forhandsvis(klient, kid, ider):
    return klient.post(RUTE.format(kid=kid), data={"paamelding_id": [str(i) for i in ider]})


# ==================== tilgang ====================

def test_krever_admin(con):
    kid = _kurs(con)
    pid, _ = _manuell(con, kid)
    con.commit()
    resp = _forhandsvis(_klient(), kid, [pid])
    assert resp.status_code == 302
    assert "/admin/logg-inn" in resp.headers["Location"]


def test_get_ikke_tillatt(con):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    assert klient.get(RUTE.format(kid=kid)).status_code == 405


# ==================== utvalgsstørrelse ====================

def test_tomt_utvalg_gir_feilmelding(con):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = _forhandsvis(klient, kid, [])
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/admin/kurs/{kid}/deltakere")


def test_maks_50_tillatt(con):
    kid = _kurs(con, kapasitet=100)
    con.commit()
    ider = [_manuell(con, kid, epost=f"p{i}@x.no", navn=f"Person {i}")[0] for i in range(50)]
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = _forhandsvis(klient, kid, ider)
    assert resp.status_code == 200
    tekst = resp.get_data(as_text=True)
    assert "50 av 50" in tekst


def test_over_50_avvises(con):
    kid = _kurs(con, kapasitet=100)
    con.commit()
    ider = [_manuell(con, kid, epost=f"p{i}@x.no", navn=f"Person {i}")[0] for i in range(51)]
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = _forhandsvis(klient, kid, ider)
    assert resp.status_code == 302
    tekst = klient.get(resp.headers["Location"]).get_data(as_text=True)
    assert "maks 50" in tekst.lower()


# ==================== id fra annet kurs ====================

def test_id_fra_annet_kurs_ignoreres_uten_lekkasje(con):
    kid1 = _kurs(con, "K1")
    kid2 = _kurs(con, "K2")
    pid1, _ = _manuell(con, kid1, epost="a@x.no", navn="I Kurs 1")
    pid2, _ = _manuell(con, kid2, epost="b@x.no", navn="I Kurs 2")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = _forhandsvis(klient, kid1, [pid1, pid2])
    tekst = resp.get_data(as_text=True)
    assert "I Kurs 1" in tekst
    assert "I Kurs 2" not in tekst  # tilhorer kid2 - stille utelatt
    assert "1 av 1" in tekst  # ingen feilmelding om at raden "finnes men ikke her"


# ==================== klassifisering ====================

def test_behandlingsbar_bekreftet(con):
    kid = _kurs(con, kapasitet=5)
    pid, status = _manuell(con, kid)
    assert status == "bekreftet"
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = _forhandsvis(klient, kid, [pid]).get_data(as_text=True)
    assert "1 av 1" in tekst
    assert "Klar til behandling" in tekst


def test_behandlingsbar_venteliste(con):
    kid = _kurs(con, kapasitet=1)
    db.meld_paa(con, kid, epost="forst@x.no", navn="Forst")
    pid, status = _manuell(con, kid)
    assert status == "venteliste"
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = _forhandsvis(klient, kid, [pid]).get_data(as_text=True)
    assert "1 av 1" in tekst
    assert "venteliste" in tekst.lower()


def test_sveiper_utsatt_0_ikke_behandlingsbar(con):
    kid = _kurs(con, kapasitet=5)
    pid, _ = db.meld_paa(con, kid, epost="kari@x.no", navn="Kari Nordmann")  # vanlig, ikke utsatt
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = _forhandsvis(klient, kid, [pid]).get_data(as_text=True)
    assert "0 av 1" in tekst
    assert "ikke holdt tilbake" in tekst.lower()


def test_sveiper_kjort_1_ikke_behandlingsbar(con):
    kid = _kurs(con, kapasitet=5)
    pid, _ = _manuell(con, kid)
    con.execute("UPDATE paamelding SET sveiper_kjort=1 WHERE id=?", (pid,))
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = _forhandsvis(klient, kid, [pid]).get_data(as_text=True)
    assert "0 av 1" in tekst
    assert "allerede behandlet" in tekst.lower()


def test_avmeldt_ikke_behandlingsbar(con):
    kid = _kurs(con, kapasitet=5)
    pid, _ = _manuell(con, kid)
    db.meld_av(con, pid)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = _forhandsvis(klient, kid, [pid]).get_data(as_text=True)
    assert "0 av 1" in tekst
    assert "avmeldt" in tekst.lower()


@pytest.mark.parametrize("status", ["avlyst", "utkast"])
def test_feil_kursstatus_ikke_behandlingsbar(con, status):
    kid = _kurs(con, kapasitet=5)
    pid, _ = _manuell(con, kid)
    con.execute("UPDATE kurs SET status=? WHERE id=?", (status, kid))
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = _forhandsvis(klient, kid, [pid]).get_data(as_text=True)
    assert "0 av 1" in tekst
    assert status in tekst.lower()


def test_blandet_utvalg_viser_riktig_telling(con):
    kid = _kurs(con, kapasitet=5)
    pid_ok, _ = _manuell(con, kid, epost="ok@x.no", navn="Ok Person")
    pid_ferdig, _ = _manuell(con, kid, epost="ferdig@x.no", navn="Ferdig Person")
    con.execute("UPDATE paamelding SET sveiper_kjort=1 WHERE id=?", (pid_ferdig,))
    pid_vanlig, _ = db.meld_paa(con, kid, epost="vanlig@x.no", navn="Vanlig Person")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = _forhandsvis(klient, kid, [pid_ok, pid_ferdig, pid_vanlig]).get_data(as_text=True)
    assert "1 av 3" in tekst
    assert "Ok Person" in tekst and "Ferdig Person" in tekst and "Vanlig Person" in tekst


# ==================== ingen sideeffekter ====================

def test_ingen_databaseendring_ved_forhaandsvisning(con):
    kid = _kurs(con, kapasitet=5)
    pid, _ = _manuell(con, kid)
    con.commit()
    klient = _klient()
    _logg_inn(klient)                             # innlogging logges - foer tilstandsbildet
    foer = _snapshot(con)
    rad_foer = dict(con.execute("SELECT sveiper_kjort, sveiper_utsatt FROM paamelding WHERE id=?", (pid,)).fetchone())
    _forhandsvis(klient, kid, [pid])
    assert _snapshot(con) == foer
    rad_etter = dict(con.execute("SELECT sveiper_kjort, sveiper_utsatt FROM paamelding WHERE id=?", (pid,)).fetchone())
    assert rad_foer == rad_etter


def test_ingen_epost_ingen_visma_ingen_faktura(con, monkeypatch):
    kid = _kurs(con, kapasitet=5, pris_nok=1000, fakturering="person")
    pid, _ = _manuell(con, kid)
    con.commit()

    def _skal_ikke_kalles(*a, **kw):
        raise AssertionError("skal ALDRI kalles av forhaandsvisning")
    monkeypatch.setattr(epost, "send", _skal_ikke_kalles)
    monkeypatch.setattr(visma, "fakturer", _skal_ikke_kalles)
    monkeypatch.setattr(app_modul.sveiper, "kjor", _skal_ikke_kalles)

    klient = _klient()
    _logg_inn(klient)
    resp = _forhandsvis(klient, kid, [pid])
    assert resp.status_code == 200
    assert _antall_mail(con) == 0
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 0


def test_cache_control_no_store(con):
    kid = _kurs(con, kapasitet=5)
    pid, _ = _manuell(con, kid)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = _forhandsvis(klient, kid, [pid])
    assert resp.headers.get("Cache-Control") == "no-store"


def test_ingen_persondata_i_hendelseslogg(con):
    kid = _kurs(con, kapasitet=5)
    pid, _ = _manuell(con, kid, epost="hemmelig.person@x.no", navn="Hemmelig Person")
    con.commit()
    klient = _klient()
    _logg_inn(klient)                             # innlogging logges - foer tellingen
    foer = con.execute("SELECT COUNT(*) FROM hendelse").fetchone()[0]
    _forhandsvis(klient, kid, [pid])
    assert con.execute("SELECT COUNT(*) FROM hendelse").fetchone()[0] == foer  # ingen ny logg i det hele tatt
    for r in con.execute("SELECT detaljer FROM hendelse"):
        assert "hemmelig" not in (r["detaljer"] or "").lower()


def test_navn_epost_vises_til_autentisert_admin(con):
    kid = _kurs(con, kapasitet=5)
    pid, _ = _manuell(con, kid, epost="synlig.person@x.no", navn="Synlig Person")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = _forhandsvis(klient, kid, [pid]).get_data(as_text=True)
    assert "Synlig Person" in tekst
    assert "synlig.person@x.no" in tekst
