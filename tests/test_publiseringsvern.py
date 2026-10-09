"""Hardening-checkpoint: publiseringsvern for offentlig kursside (foer skjemabygger).

GET/POST /kurs/<kode> skal kun vaere offentlig tilgjengelig naar kurset har status 'aapen', 'full' eller
'aktiv' (OFFENTLIG_SYNLIGE_KURSSTATUSER i web/app.py). 'utkast', 'avsluttet' og 'avlyst' skal gi 404 - baade
for GET (vis skjema) og POST (forsokt paamelding) - uten aa lekke noe om at kurset finnes utover det en
ukjent kode allerede ville gitt. Regelen er en allow-list (fail-closed): en fremtidig/ukjent statusverdi skal
ALDRI automatisk bli offentlig. Admin sin forhaandsvisning (admin_forhandsvis_paamelding) er UPAAVIRKET og
skal fortsatt fungere for alle statuser - den bruker en helt annen kodesti (_hent_kurs, ikke kursside()).
"""
from datetime import date, timedelta

import pytest

from adressehjelp import ADRESSE
from adressehjelp import gruppeadresse
from kurs import config, db
from kurs.web import app as webapp


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _klient():
    return webapp.app.test_client()


def _innlogget():
    k = _klient()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def _kurs(con, kode, status, **kw):
    start = date.today() + timedelta(days=30)
    felter = {"navn": f"Kurs {status}", "pris_nok": 0, "fakturering": "person", "kapasitet": 10, **kw}
    kid = db.opprett_kurs(con, kode=kode, datoer=[start.isoformat()], sharepoint_mappe=f"K/{kode}", **felter)
    con.execute("UPDATE kurs SET status=? WHERE id=?", (status, kid))
    con.commit()
    return kid


def _antall(con, tabell):
    return con.execute(f"SELECT COUNT(*) FROM {tabell}").fetchone()[0]


# ============================ ikke-offentlige statuser: 404 baade GET og POST ============================

@pytest.mark.parametrize("status", ["utkast", "avsluttet", "avlyst"])
def test_ikke_offentlig_status_gir_404_paa_get(con, status, monkeypatch):
    from kurs.integrasjoner import epost
    monkeypatch.setattr(epost, "send", lambda *a, **kw: None)
    _kurs(con, "K1", status)
    con.commit()
    r = _klient().get("/kurs/K1")
    assert r.status_code == 404


@pytest.mark.parametrize("status", ["utkast", "avsluttet", "avlyst"])
def test_offentlig_post_kan_ikke_omgaa_get_vernet(con, status, monkeypatch):
    """POST med gyldige skjemadata mot en ikke-offentlig status skal ogsaa gi 404 - ikke 400 med flash,
    og aller viktigst: ingen paamelding skal opprettes."""
    from kurs.integrasjoner import epost, visma
    epost_kalt, visma_kalt = [], []
    monkeypatch.setattr(epost, "send", lambda *a, **kw: epost_kalt.append(1))
    monkeypatch.setattr(visma, "fakturer", lambda *a, **kw: visma_kalt.append(1))
    _kurs(con, "K1", status)
    con.commit()
    r = _klient().post("/kurs/K1", data={"fornavn": "Ola", "etternavn": "Nordmann", "epost": "ola@x.no", "samtykke": "on", "samtykke_lagring": "on", **ADRESSE})
    assert r.status_code == 404
    assert _antall(con, "paamelding") == 0
    assert _antall(con, "deltaker") == 0
    assert epost_kalt == []
    assert visma_kalt == []


# ============================ offentlige statuser: uendret oppforsel ============================

def test_aapen_fungerer_som_for(con, monkeypatch):
    from kurs.integrasjoner import epost
    monkeypatch.setattr(epost, "send", lambda *a, **kw: None)
    _kurs(con, "K1", "aapen")
    con.commit()
    r = _klient().get("/kurs/K1")
    assert r.status_code == 200
    assert "Meld meg på" in r.get_data(as_text=True)
    r2 = _klient().post("/kurs/K1", data={"fornavn": "Ola", "etternavn": "Nordmann", "epost": "ola@x.no", "samtykke": "on", "samtykke_lagring": "on", **ADRESSE})
    assert r2.status_code == 200
    assert _antall(con, "paamelding") == 1
    assert con.execute("SELECT status FROM paamelding").fetchone()[0] == "bekreftet"


def test_full_fungerer_som_for_inkl_venteliste(con, monkeypatch):
    """'full' skal fortsatt vaere offentlig GET+POST, og en ny paamelding gaar fortsatt til venteliste
    naar kurset faktisk er fullt - dette steget skal IKKE endre kapasitets-/ventelisteregelen."""
    from kurs.integrasjoner import epost
    monkeypatch.setattr(epost, "send", lambda *a, **kw: None)
    kid = _kurs(con, "K1", "aapen", kapasitet=1)
    con.commit()
    r1 = _klient().post("/kurs/K1", data={"fornavn": "Forste", "etternavn": "Deltaker", "epost": "forste@x.no", "samtykke": "on", "samtykke_lagring": "on", **ADRESSE})
    assert r1.status_code == 200

    # naa er kapasiteten (1) faktisk fylt - andre paamelding utloeser meld_paa() sin egen 'full'-/venteliste-logikk
    r2 = _klient().post("/kurs/K1", data={"fornavn": "Andre", "etternavn": "Deltaker", "epost": "andre@x.no", "samtykke": "on", "samtykke_lagring": "on", **ADRESSE})
    assert r2.status_code == 200
    assert con.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "full"   # auto-satt av meld_paa

    r_get = _klient().get("/kurs/K1")
    assert r_get.status_code == 200   # fortsatt offentlig synlig naar full

    statuser = {r["status"] for r in con.execute("SELECT status FROM paamelding")}
    assert statuser == {"bekreftet", "venteliste"}


def test_aktiv_folger_eksisterende_forretningsregel_fortsatt_offentlig(con, monkeypatch):
    """Audit viste at systemet i dag faktisk tillater registrering til et 'aktiv' (paagaaende) kurs -
    denne oppforselen er BEVISST beholdt uendret av dette checkpointet, ikke fjernet."""
    from kurs.integrasjoner import epost
    monkeypatch.setattr(epost, "send", lambda *a, **kw: None)
    _kurs(con, "K1", "aktiv")
    con.commit()
    r = _klient().get("/kurs/K1")
    assert r.status_code == 200
    r2 = _klient().post("/kurs/K1", data={"fornavn": "Ola", "etternavn": "Nordmann", "epost": "ola@x.no", "samtykke": "on", "samtykke_lagring": "on", **ADRESSE})
    assert r2.status_code == 200
    assert _antall(con, "paamelding") == 1


def test_forsiden_er_innloggingen_til_admin_og_viser_aldri_kurs(con):
    """Startsiden er bare innloggingen til Admin: kurslisten er tatt bort, og ingen kurs vises uansett status."""
    _kurs(con, "K1", "utkast")
    _kurs(con, "K2", "aapen")
    _kurs(con, "K3", "full")
    _kurs(con, "K4", "aktiv")
    _kurs(con, "K5", "avsluttet")
    _kurs(con, "K6", "avlyst")
    con.commit()
    html = _klient().get("/").get_data(as_text=True)
    assert "Logg inn til Admin" in html
    for kurs in ("Kurs utkast", "Kurs aapen", "Kurs full", "Kurs aktiv", "Kurs avsluttet", "Kurs avlyst"):
        assert kurs not in html


# ============================ admin-preview: upaavirket, fungerer for ALLE statuser ============================

@pytest.mark.parametrize("status", ["utkast", "avlyst", "avsluttet"])
def test_admin_preview_fungerer_fortsatt_for_ikke_offentlige_statuser(con, status):
    kid = _kurs(con, "K1", status)
    con.commit()
    r = _innlogget().get(f"/admin/kurs/{kid}/forhandsvis-paamelding", follow_redirects=True)
    assert r.status_code == 200 and r.request.path == "/kurs/K1"
    html = r.get_data(as_text=True)
    assert '<button type="button">Meld meg på</button>' in html and "data-ingen-innsending" in html


# ============================ fail-closed for ukjent/fremtidig status ============================

def test_ukjent_fremtidig_status_er_ikke_offentlig_som_standard():
    """Testet paa selve hjelpefunksjonen (ikke via ekte DB-rad), siden schema.sql sin CHECK-constraint i dag
    hindrer at en faktisk ukjent statusverdi noensinne kan lagres i databasen - allow-list-designet i
    _kurs_offentlig_tilgjengelig() er likevel det som garanterer fail-closed oppforsel den dagen en ny
    statusverdi eventuelt legges til i schema.sql uten at noen husker aa oppdatere denne regelen."""
    assert webapp._kurs_offentlig_tilgjengelig({"status": "en_helt_ny_status_ingen_har_hort_om"}) is False
    assert webapp._kurs_offentlig_tilgjengelig({"status": "aapen"}) is True
    assert webapp._kurs_offentlig_tilgjengelig({"status": "full"}) is True
    assert webapp._kurs_offentlig_tilgjengelig({"status": "aktiv"}) is True
    assert webapp._kurs_offentlig_tilgjengelig({"status": "utkast"}) is False
    assert webapp._kurs_offentlig_tilgjengelig({"status": "avlyst"}) is False
    assert webapp._kurs_offentlig_tilgjengelig({"status": "avsluttet"}) is False


def test_ukjent_kurskode_gir_fortsatt_404_uendret(con):
    assert _klient().get("/kurs/FINNES-IKKE").status_code == 404


# ============================ bedriftspaamelding (kurs_gruppe) - samme sentrale regel ============================

def _gruppedata():
    return {
        "kontakt_fornavn": "Kari", "kontakt_etternavn": "Kontakt", "kontakt_epost": "kari@x.no", "firmanavn": "Firma AS",
        "org_nr": "999900003", "samtykke": "on", "samtykke_lagring": "on",
        "deltaker_fornavn": ["Ola"], "deltaker_etternavn": ["Nordmann"], "deltaker_epost": ["ola@x.no"],
        "deltaker_telefon": [""], "deltaker_arbeidssted": [""], "deltaker_hpr": [""], **gruppeadresse(),
    }


def _sideeffekttall(con):
    return {t: _antall(con, t) for t in ("firmapaamelding", "firmapaamelding_rad", "paamelding", "deltaker",
                                          "utsending_logg", "faktura", "faktura_forsok")}


@pytest.mark.parametrize("status", ["utkast", "avsluttet", "avlyst"])
def test_gruppe_ikke_offentlig_status_gir_404_paa_get(con, status):
    _kurs(con, "K1", status)
    con.commit()
    assert _klient().get("/kurs/K1/gruppe").status_code == 404


@pytest.mark.parametrize("status", ["utkast", "avsluttet", "avlyst"])
def test_gruppe_offentlig_post_kan_ikke_omgaa_get_vernet(con, status, monkeypatch):
    from kurs.integrasjoner import epost, visma
    epost_kalt, visma_kalt = [], []
    monkeypatch.setattr(epost, "send", lambda *a, **kw: epost_kalt.append(1))
    monkeypatch.setattr(visma, "fakturer", lambda *a, **kw: visma_kalt.append(1))
    _kurs(con, "K1", status)
    con.commit()
    for_ = _sideeffekttall(con)
    r = _klient().post("/kurs/K1/gruppe", data=_gruppedata())
    assert r.status_code == 404
    assert _sideeffekttall(con) == for_   # helt uendret - ingen firmapaamelding/paamelding/deltaker/utsending/faktura
    assert epost_kalt == []
    assert visma_kalt == []


def test_gruppe_aapen_get_virker(con):
    _kurs(con, "K1", "aapen")
    con.commit()
    r = _klient().get("/kurs/K1/gruppe")
    assert r.status_code == 200
    assert "Bedriftspåmelding" in r.get_data(as_text=True)


def test_gruppe_full_get_virker(con):
    _kurs(con, "K1", "full")
    con.commit()
    assert _klient().get("/kurs/K1/gruppe").status_code == 200


def test_gruppe_aktiv_get_virker_iht_eksisterende_regel(con):
    _kurs(con, "K1", "aktiv")
    con.commit()
    assert _klient().get("/kurs/K1/gruppe").status_code == 200


@pytest.mark.parametrize("status", ["aapen", "full", "aktiv"])
def test_gruppe_tillatte_statuser_beholder_eksisterende_post_funksjon(con, status, monkeypatch):
    from kurs.integrasjoner import epost
    monkeypatch.setattr(epost, "send", lambda *a, **kw: None)
    _kurs(con, "K1", status)
    con.commit()
    r = _klient().post("/kurs/K1/gruppe", data=_gruppedata())
    assert r.status_code == 302   # uendret - redirect til kvittering, som foer dette checkpointet
    assert _antall(con, "firmapaamelding") == 1
    assert _antall(con, "paamelding") == 1


def test_begge_offentlige_ruter_bruker_samme_helper_ikke_dupliserte_statussett(con):
    """kursside() og kurs_gruppe() skal begge kalle den ENE _kurs_offentlig_tilgjengelig()-funksjonen -
    ikke ha sin egen kopi av statuslista. Verifiseres ved at aa endre allow-listen paavirker BEGGE ruter."""
    _kurs(con, "K1", "aapen")
    con.commit()
    assert _klient().get("/kurs/K1").status_code == 200
    assert _klient().get("/kurs/K1/gruppe").status_code == 200
    original = webapp.OFFENTLIG_SYNLIGE_KURSSTATUSER
    try:
        webapp.OFFENTLIG_SYNLIGE_KURSSTATUSER = frozenset()   # ingenting er offentlig lenger
        assert _klient().get("/kurs/K1").status_code == 404
        assert _klient().get("/kurs/K1/gruppe").status_code == 404
    finally:
        webapp.OFFENTLIG_SYNLIGE_KURSSTATUSER = original
