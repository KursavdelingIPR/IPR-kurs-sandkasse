"""Min side (for ett kurs): åpningstiden etter kurset kan settes per kurs (kursside.apen_dager). Standard er 180 dager etter siste kursdag
(config.KURSSIDE_ETTERTILGANG_DAGER); hvert kurs kan ha 1-3650 dager eller «Ingen tidsbegrensning». Håndhevet i tilgangssjekken (siden,
filnedlasting og Mine kurs) og forklart for deltakeren. En bestemt «Åpen til»-dato virker som før og går foran.
(06.10.2026: kursholder-lenken og SharePoint er tatt bort, og med dem kursholders filer.)
Bare oppdiktede data.
"""
import json
import re
import sqlite3
from datetime import date, timedelta

import pytest

from kurs import config, db, deltakerside, migreringer
from kurs import sidelager
from kurs.sideinnhold import Sidefeil

from kurssidehjelp import (IDAG, admin_klient, dokument, deltaker_klient, fast_dato, json_post, lag_deltaker, lag_kurs, ny_database, pdf,
                           skriv_side, tekstblokk)

ADMIN = "admin:test"
SISTE = IDAG + timedelta(days=1)             # siste kursdag i lag_kurs (to dager fra IDAG)


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    fast_dato(monkeypatch)
    yield c
    c.close()


@pytest.fixture
def kid(con):
    k = lag_kurs(con, "K1")
    skriv_side(con, k, dokument(tekstblokk("Velkommen", "<p>Hei og velkommen til kurset</p>")))
    return k


@pytest.fixture
def admin(con):
    return admin_klient()


def _sett(con, kid, **kw):
    assert sidelager.sett_innstillinger(con, kid, aktor=ADMIN, **kw) is True
    con.commit()


def _stenges(con, kid):
    return sidelager.effektiv_stenges(con, kid, sidelager.hent(con, kid))


def _u(kid, hale=""):
    return f"/admin/kurs/{kid}/kursside{hale}"


def _hent(klient, kode="K1"):
    return klient.get(f"/kurs/{kode}/deltakerside")


# ============================ lagring og grenser ============================

def test_standard_er_180_dager_og_kolonnen_er_tom(con, kid):
    rad = sidelager.hent(con, kid)
    assert rad["apen_dager"] is None and rad["stenges"] is None
    assert config.KURSSIDE_ETTERTILGANG_DAGER == 180 and sidelager.standard_dager() == 180
    assert _stenges(con, kid) == SISTE + timedelta(days=180)


@pytest.mark.parametrize("dager", [1, 2, 30, 365, 1000, 3649, 3650])
def test_egne_dager_gir_siste_kursdag_pluss_dagene(con, kid, dager):
    _sett(con, kid, apen_dager=dager)
    assert sidelager.hent(con, kid)["apen_dager"] == dager
    assert _stenges(con, kid) == SISTE + timedelta(days=dager)


@pytest.mark.parametrize("ugyldig", [-1, -365, 3651, 5000, 10 ** 30, "365", "abc", 12.5, 365.0, True, False, [], {}, (30,)])
def test_ugyldig_antall_dager_avvises_uten_at_noe_lagres(con, kid, ugyldig):
    _sett(con, kid, apen_dager=90)
    with pytest.raises(Sidefeil) as e:
        sidelager.sett_innstillinger(con, kid, apen_dager=ugyldig, aktor=ADMIN)
    assert "fra 1 til 3650" in e.value.melding and "Ingen tidsbegrensning" in e.value.melding
    con.rollback()
    assert sidelager.hent(con, kid)["apen_dager"] == 90                          # uendret
    assert _stenges(con, kid) == SISTE + timedelta(days=90)


def test_ingen_tidsbegrensning_er_null_i_kolonnen_og_gir_aldri_stengt(con, kid):
    _sett(con, kid, apen_dager=sidelager.UBEGRENSET)
    assert sidelager.hent(con, kid)["apen_dager"] == 0 and sidelager.UBEGRENSET == 0
    assert _stenges(con, kid) is None
    apning = sidelager.apningstid(con, kid, sidelager.hent(con, kid))
    assert apning["modus"] == "ubegrenset" and apning["stenges_effektiv"] is None and apning["apen_dager"] == 0


def test_tilbake_til_standard_og_none_er_uendret(con, kid):
    _sett(con, kid, apen_dager=45)
    _sett(con, kid, innsjekk_krever_kode=False)                                   # apen_dager utelatt = uendret
    assert sidelager.hent(con, kid)["apen_dager"] == 45
    _sett(con, kid, apen_dager=sidelager.STANDARD)
    assert sidelager.hent(con, kid)["apen_dager"] is None and _stenges(con, kid) == SISTE + timedelta(days=180)
    assert sidelager.hent(con, kid)["innsjekk_krever_kode"] == 0                   # kode-valget uendret av åpningstiden


def test_sett_innstillinger_uten_siden_gir_false(con):
    andre = lag_kurs(con, "K2")                                                    # ingen kursside ennå
    assert sidelager.sett_innstillinger(con, andre, apen_dager=30, aktor=ADMIN) is False


def test_dato_og_dager_er_gjensidig_utelukkende(con, kid):
    with pytest.raises(Sidefeil) as e:
        sidelager.sett_innstillinger(con, kid, stenges="2027-12-24", apen_dager=30, aktor=ADMIN)
    assert "enten en bestemt dato eller et antall dager" in e.value.melding
    con.rollback()
    assert (sidelager.hent(con, kid)["stenges"], sidelager.hent(con, kid)["apen_dager"]) == (None, None)
    _sett(con, kid, apen_dager=30)
    _sett(con, kid, stenges="2027-12-24")                                          # en bestemt dato erstatter dagene
    assert (sidelager.hent(con, kid)["stenges"], sidelager.hent(con, kid)["apen_dager"]) == ("2027-12-24", None)
    _sett(con, kid, apen_dager=500)                                                # dager erstatter datoen
    assert (sidelager.hent(con, kid)["stenges"], sidelager.hent(con, kid)["apen_dager"]) == (None, 500)
    _sett(con, kid, stenges="2027-12-24", apen_dager=sidelager.STANDARD)           # dato + «tilbake til standard for dager» er tillatt
    assert (sidelager.hent(con, kid)["stenges"], sidelager.hent(con, kid)["apen_dager"]) == ("2027-12-24", None)
    _sett(con, kid, stenges="", apen_dager=sidelager.UBEGRENSET)                   # datoen fjernes, ubegrenset settes
    assert (sidelager.hent(con, kid)["stenges"], sidelager.hent(con, kid)["apen_dager"]) == (None, 0)


def test_en_bestemt_dato_gaar_foran_dager_i_beregningen(con, kid):
    """Gamle rader kan ha begge (dato satt før migreringen, dager satt senere via direkte SQL): datoen gjelder, som før."""
    _sett(con, kid, apen_dager=30)
    con.execute("UPDATE kursside SET stenges='2027-12-24' WHERE kurs_id=?", (kid,))
    con.commit()
    assert _stenges(con, kid) == date(2027, 12, 24)
    assert sidelager.apningstid(con, kid, sidelager.hent(con, kid))["modus"] == "dato"


def test_kurs_uten_kursdager_har_ingen_sluttdato_uansett_dager(con):
    k = db.opprett_kurs(con, kode="TOM", navn="Uten dager", datoer=[], type="fysisk", sted="Bergen", status="aapen", sharepoint_mappe="Kurs/TOM")
    con.commit()
    skriv_side(con, k, dokument(tekstblokk()))
    for dager in (None, 30, 0):
        con.execute("UPDATE kursside SET apen_dager=? WHERE kurs_id=?", (dager, k))
        con.commit()
        assert _stenges(con, k) is None


def test_standarden_i_konfigurasjonen_gjelder_kurs_uten_egen_verdi(con, kid, monkeypatch):
    monkeypatch.setattr(config, "KURSSIDE_ETTERTILGANG_DAGER", 10)
    assert _stenges(con, kid) == SISTE + timedelta(days=10)
    _sett(con, kid, apen_dager=40)
    assert _stenges(con, kid) == SISTE + timedelta(days=40)                        # kursets egen verdi går foran standarden


@pytest.mark.parametrize("innstilt, brukt", [(0, 1), (-5, 1), (99999, 3650)])
def test_feil_standard_i_miljoet_velter_ikke_siden(con, kid, monkeypatch, innstilt, brukt):
    """En feil i .env (0, negativ, altfor stor) holdes innenfor 1-3650 i stedet for å gi en feilside."""
    monkeypatch.setattr(config, "KURSSIDE_ETTERTILGANG_DAGER", innstilt)
    assert sidelager.standard_dager() == brukt and _stenges(con, kid) == SISTE + timedelta(days=brukt)


# ============================ ødelagte lagrede verdier ============================

@pytest.mark.parametrize("verdi", ["abc", "365", "", None, 3651, -1, 12.5, True, False, [365], {"dager": 1}])
def test_odelagt_lagret_verdi_tolkes_som_standard(verdi):
    assert sidelager.gyldig_apen_dager(verdi) is None


def test_gyldige_lagrede_verdier_er_null_til_3650():
    assert [sidelager.gyldig_apen_dager(v) for v in (0, 1, 180, 3650)] == [0, 1, 180, 3650]


@pytest.mark.parametrize("verdi", ["abc", "365", 3651, -1, 12.5, True, [365]])
def test_odelagt_apen_dager_i_raden_gir_standard_beregning(con, kid, verdi):
    side = {"stenges": None, "apen_dager": verdi}
    assert sidelager.effektiv_stenges(con, kid, side) == SISTE + timedelta(days=180)
    assert sidelager.apningstid(con, kid, side)["modus"] == "standard"


@pytest.mark.parametrize("dato", ["ikke-en-dato", "2027-13-45", "12.03.2027", "", 20270312, ["2027-03-12"]])
def test_odelagt_aapen_til_dato_gir_dager_eller_standard_uten_feil(con, kid, dato):
    assert sidelager.effektiv_stenges(con, kid, {"stenges": dato, "apen_dager": None}) == SISTE + timedelta(days=180)
    assert sidelager.effektiv_stenges(con, kid, {"stenges": dato, "apen_dager": 30}) == SISTE + timedelta(days=30)
    assert sidelager.effektiv_stenges(con, kid, {"stenges": dato, "apen_dager": 0}) is None


def test_rad_uten_kolonnen_apen_dager_gir_standard(con, kid):
    """Et gammelt raduttrekk (eller en test-dobbel) uten kolonnen skal ikke gi KeyError."""
    assert sidelager.effektiv_stenges(con, kid, {"stenges": None}) == SISTE + timedelta(days=180)


@pytest.mark.kun_sqlite
@pytest.mark.parametrize("verdi", [-1, 3651, 99999])
def test_databasen_stopper_verdier_utenfor_grensene(con, kid, verdi):
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE kursside SET apen_dager=? WHERE kurs_id=?", (verdi, kid))
    con.rollback()
    for ok in (None, 0, 1, 3650):
        con.execute("UPDATE kursside SET apen_dager=? WHERE kurs_id=?", (ok, kid))
    con.rollback()


# ============================ tilgangen håndheves ============================

@pytest.mark.parametrize("dager", [None, 30, 365, 1])
def test_siste_apne_dag_er_nøyaktig_siste_kursdag_pluss_dagene(con, kid, monkeypatch, dager):
    """Åpen på siste dag (inklusive), stengt dagen etter: for standarden og for kursets egne dager."""
    _sett(con, kid, **({} if dager is None else {"apen_dager": dager}))
    forventet = dager or 180
    did = lag_deltaker(con, kid)[0]
    siste_apne = SISTE + timedelta(days=forventet)
    fast_dato(monkeypatch, siste_apne)
    assert _hent(deltaker_klient(did)).status_code == 200
    t = deltakerside.tilgang(con, did, "K1", siste_apne)
    assert (t.ok, t.arsak, t.stenges) == (True, "ok", siste_apne)
    fast_dato(monkeypatch, siste_apne + timedelta(days=1))
    r = _hent(deltaker_klient(did))
    assert r.status_code == 404 and "Min side er stengt" in r.get_data(as_text=True)
    assert deltakerside.tilgang(con, did, "K1", siste_apne + timedelta(days=1)).arsak == "stengt"


def test_egne_dager_kan_gjore_siden_aapen_lenger_enn_standarden(con, kid, monkeypatch):
    did = lag_deltaker(con, kid)[0]
    etter_standard = SISTE + timedelta(days=181)
    fast_dato(monkeypatch, etter_standard)
    assert _hent(deltaker_klient(did)).status_code == 404                           # standarden: stengt
    _sett(con, kid, apen_dager=730)
    assert _hent(deltaker_klient(did)).status_code == 200                           # to år: åpen
    fast_dato(monkeypatch, SISTE + timedelta(days=731))
    assert _hent(deltaker_klient(did)).status_code == 404


def test_egne_dager_kan_ogsa_stenge_tidligere_enn_standarden(con, kid, monkeypatch):
    did = lag_deltaker(con, kid)[0]
    _sett(con, kid, apen_dager=7)
    fast_dato(monkeypatch, SISTE + timedelta(days=7))
    assert _hent(deltaker_klient(did)).status_code == 200
    fast_dato(monkeypatch, SISTE + timedelta(days=8))
    assert _hent(deltaker_klient(did)).status_code == 404


def test_ingen_tidsbegrensning_er_aapen_ti_aar_etter(con, kid, monkeypatch):
    did = lag_deltaker(con, kid)[0]
    _sett(con, kid, apen_dager=sidelager.UBEGRENSET)
    for dager in (0, 180, 181, 3650, 3651, 20000):
        fast_dato(monkeypatch, SISTE + timedelta(days=dager))
        r = _hent(deltaker_klient(did))
        assert r.status_code == 200, dager
    t = deltakerside.tilgang(con, did, "K1", SISTE + timedelta(days=20000))
    assert (t.ok, t.arsak, t.stenges) == (True, "ok", None)


def test_andre_kurs_paavirkes_ikke_av_ett_kurs_sine_dager(con, kid, monkeypatch):
    k2 = lag_kurs(con, "K2")
    skriv_side(con, k2, dokument(tekstblokk("Side to", "<p>Kurs to</p>")))
    a = lag_deltaker(con, kid, "kari@example.no")[0]
    b = lag_deltaker(con, k2, "ola@example.no", fornavn="Ola")[0]
    _sett(con, kid, apen_dager=sidelager.UBEGRENSET)
    fast_dato(monkeypatch, SISTE + timedelta(days=181))
    assert _hent(deltaker_klient(a), "K1").status_code == 200                       # K1: ubegrenset
    assert _hent(deltaker_klient(b), "K2").status_code == 404                       # K2: standard, stengt
    assert sidelager.hent(con, k2)["apen_dager"] is None and _stenges(con, k2) == SISTE + timedelta(days=180)
    _sett(con, k2, apen_dager=10)
    assert sidelager.hent(con, kid)["apen_dager"] == 0                              # K1 uendret av at K2 endres


def test_tilgangsmatrisen_er_uendret_for_alle_aapningstider(con, kid, monkeypatch):
    """Bare «stengt» avhenger av åpningstiden. Alle andre avslag og innslipp er like for standard, egne dager og ingen tidsbegrensning."""
    andre = lag_kurs(con, "K2")
    skriv_side(con, andre, dokument(tekstblokk()))
    ok = lag_deltaker(con, kid, "kari@example.no")[0]
    venter = lag_deltaker(con, kid, "vera@example.no", fornavn="Vera", status="venteliste")[0]
    annet = lag_deltaker(con, andre, "aisha@example.no", fornavn="Aisha")[0]
    for innstilling in ({}, {"apen_dager": 400}, {"apen_dager": sidelager.UBEGRENSET}, {"apen_dager": sidelager.STANDARD}):
        _sett(con, kid, **innstilling)
        assert _hent(deltaker_klient(ok)).status_code == 200
        for did in (venter, annet):
            r = _hent(deltaker_klient(did))
            assert r.status_code == 404 and "Vi finner ikke siden" in r.get_data(as_text=True)
        assert _hent(deltaker_klient()).status_code == 302                          # uinnlogget: til innlogging
        con.execute("UPDATE kursside SET aktiv=0 WHERE kurs_id=?", (kid,)); con.commit()
        r = _hent(deltaker_klient(ok))
        assert r.status_code == 404 and "midlertidig stengt" in r.get_data(as_text=True)      # nødbremsen virker også ved ubegrenset
        con.execute("UPDATE kursside SET aktiv=1 WHERE kurs_id=?", (kid,)); con.commit()
        con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kid,)); con.commit()
        assert _hent(deltaker_klient(ok)).status_code == 404
        con.execute("UPDATE kurs SET status='aapen' WHERE id=?", (kid,)); con.commit()
        con.execute("UPDATE kursside SET publisert=NULL WHERE kurs_id=?", (kid,)); con.commit()
        assert "ikke åpnet ennå" in _hent(deltaker_klient(ok)).get_data(as_text=True)
        skriv_side(con, kid, dokument(tekstblokk("Velkommen", "<p>Hei og velkommen til kurset</p>")))          # publiser igjen


def test_filnedlasting_folger_samme_sluttdato(con, kid, monkeypatch):
    fil_id, _ = sidelager.lagre_fil(con, kid, "hefte.pdf", pdf(), ADMIN)
    dok = dokument({"id": "b_filer001", "type": "filer", "tittel": "Filer", "data": {"filer": [{"fil_id": fil_id, "tittel": "Hefte"}]}})
    skriv_side(con, kid, dok)
    did = lag_deltaker(con, kid)[0]
    _sett(con, kid, apen_dager=60)
    url = f"/kurs/K1/deltakerside/fil/{fil_id}"
    fast_dato(monkeypatch, SISTE + timedelta(days=60))
    assert deltaker_klient(did).get(url).status_code == 200
    fast_dato(monkeypatch, SISTE + timedelta(days=61))
    assert deltaker_klient(did).get(url).status_code == 404
    _sett(con, kid, apen_dager=sidelager.UBEGRENSET)
    assert deltaker_klient(did).get(url).status_code == 200
    fast_dato(monkeypatch, SISTE + timedelta(days=9999))
    assert deltaker_klient(did).get(url).status_code == 200


def test_lister_som_folger_noedbremsen_folger_ogsaa_kursets_dager(con, kid):
    """har_side (innsjekk-resultat og Mine kurs) og Mine kurs sin kan_apne bruker samme sluttdato.
    (06.10.2026: kursholder-lenken og SharePoint er tatt bort, og kontrollen av kursholders filer med dem)"""
    did = lag_deltaker(con, kid)[0]
    _sett(con, kid, apen_dager=20)
    siste_apne, dagen_etter = SISTE + timedelta(days=20), SISTE + timedelta(days=21)
    assert deltakerside.har_side(con, kid, siste_apne) and not deltakerside.har_side(con, kid, dagen_etter)
    p = [{"kurs_id": kid, "kode": "K1", "status": "bekreftet", "kursstatus": "aapen"}]
    assert deltakerside.min_side_info(con, did, p, siste_apne)[kid]["kan_apne"] is True
    assert deltakerside.min_side_info(con, did, p, dagen_etter)[kid]["kan_apne"] is False
    _sett(con, kid, apen_dager=sidelager.UBEGRENSET)
    ute = SISTE + timedelta(days=5000)
    assert deltakerside.har_side(con, kid, ute)
    assert deltakerside.min_side_info(con, did, p, ute)[kid]["kan_apne"] is True
    assert deltakerside.landing(con, did, ute) == "K1"                                # landing bruker samme tilgangssjekk


# ============================ det deltakeren får vite ============================

def _avsluttet(con, kid):
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()


def test_avsluttet_kurs_sier_naar_siden_stenger(con, kid, monkeypatch):
    did = lag_deltaker(con, kid)[0]
    _avsluttet(con, kid)
    fast_dato(monkeypatch, SISTE + timedelta(days=1))
    stenger = SISTE + timedelta(days=180)
    forventet = f"Siden er åpen til {stenger.day}. {deltakerside._MAANEDER[stenger.month - 1]} {stenger.year}, så du kan laste ned det du trenger."
    assert forventet in _hent(deltaker_klient(did)).get_data(as_text=True)
    _sett(con, kid, apen_dager=400)
    stenger = SISTE + timedelta(days=400)
    assert f"Siden er åpen til {stenger.day}. {deltakerside._MAANEDER[stenger.month - 1]} {stenger.year}, så du" in _hent(deltaker_klient(did)).get_data(as_text=True)


def test_ingen_tidsbegrensning_sier_at_siden_blir_liggende(con, kid, monkeypatch):
    did = lag_deltaker(con, kid)[0]
    _avsluttet(con, kid)
    _sett(con, kid, apen_dager=sidelager.UBEGRENSET)
    fast_dato(monkeypatch, SISTE + timedelta(days=1))
    html = _hent(deltaker_klient(did)).get_data(as_text=True)
    assert "Siden blir liggende åpen, så du kan laste ned det du trenger." in html and "Siden er åpen til" not in html


def test_stengt_side_navner_kursets_egen_sluttdato(con, kid, monkeypatch):
    did = lag_deltaker(con, kid)[0]
    _sett(con, kid, apen_dager=30)
    fast_dato(monkeypatch, SISTE + timedelta(days=31))
    stengt = SISTE + timedelta(days=30)
    r = _hent(deltaker_klient(did))
    assert r.status_code == 404
    assert f"Min side er stengt (den var åpen til {stengt.day}. {deltakerside._MAANEDER[stengt.month - 1]} {stengt.year})." in r.get_data(as_text=True)


def test_dagens_tekster_apen_tekst_uten_sluttdato():
    assert deltakerside.apen_tekst(None) == "Siden blir liggende åpen, så du kan laste ned det du trenger."
    assert deltakerside.apen_tekst(date(2027, 3, 12)) == "Siden er åpen til 12. mars 2027, så du kan laste ned det du trenger."
    assert deltakerside.dag_maaned_aar(date(2027, 1, 1)) == "1. januar 2027"


# ============================ redigeringsvisningen: ruten ============================

def _sidedata(html: str) -> dict:
    return json.loads(re.search(r'<script type="application/json" id="ks-data"[^>]*>(.*?)</script>', html, re.S).group(1))


def _innst(admin, kid):
    return _sidedata(admin.get(_u(kid)).get_data(as_text=True))["innstillinger"]


def test_sidedata_har_aapningstiden_med_standard_og_grenser(con, kid, admin):
    i = _innst(admin, kid)
    assert i == {"innsjekk_krever_kode": True, "stenges": None, "apen_dager": None, "standard_dager": 180, "apen_dager_min": 1,
                 "apen_dager_maks": 3650, "siste_kursdag": SISTE.isoformat(), "stenges_effektiv": (SISTE + timedelta(days=180)).isoformat()}
    _sett(con, kid, apen_dager=365)
    i = _innst(admin, kid)
    assert (i["apen_dager"], i["stenges"], i["stenges_effektiv"]) == (365, None, (SISTE + timedelta(days=365)).isoformat())
    _sett(con, kid, apen_dager=sidelager.UBEGRENSET)
    i = _innst(admin, kid)
    assert (i["apen_dager"], i["stenges_effektiv"]) == (0, None)
    _sett(con, kid, stenges="2027-12-24")
    i = _innst(admin, kid)
    assert (i["apen_dager"], i["stenges"], i["stenges_effektiv"]) == (None, "2027-12-24", "2027-12-24")


def test_sidedata_for_kurs_uten_side_eller_kursdager_har_standardverdiene(con, admin):
    ny = db.opprett_kurs(con, kode="TOM", navn="Uten dager", datoer=[], type="fysisk", sted="Bergen", status="aapen", sharepoint_mappe="Kurs/TOM")
    con.commit()
    i = _innst(admin, ny)
    assert (i["apen_dager"], i["siste_kursdag"], i["stenges_effektiv"], i["standard_dager"]) == (None, None, None, 180)


def test_lagre_dager_via_ruten_gir_effektiv_dato_og_logger_bare_tallet(con, kid, admin):
    r = json_post(admin, _u(kid, "/innstillinger"), {"apen_dager": 365})
    j = r.get_json()
    assert r.status_code == 200 and j["status"] == "ok" and j["apen_dager"] == 365 and j["stenges"] is None
    assert j["stenges_effektiv"] == (SISTE + timedelta(days=365)).isoformat() and j["siste_kursdag"] == SISTE.isoformat()
    assert sidelager.hent(con, kid)["apen_dager"] == 365
    j = json_post(admin, _u(kid, "/innstillinger"), {"apen_dager": 0}).get_json()
    assert (j["apen_dager"], j["stenges_effektiv"]) == (0, None) and sidelager.hent(con, kid)["apen_dager"] == 0
    j = json_post(admin, _u(kid, "/innstillinger"), {"apen_dager": None}).get_json()                      # null = tilbake til standard
    assert (j["apen_dager"], j["stenges_effektiv"]) == (None, (SISTE + timedelta(days=180)).isoformat())
    assert sidelager.hent(con, kid)["apen_dager"] is None
    j = json_post(admin, _u(kid, "/innstillinger"), {"apen_dager": 90}).get_json()
    j2 = json_post(admin, _u(kid, "/innstillinger"), {"apen_dager": ""}).get_json()                        # «» også
    assert j["apen_dager"] == 90 and j2["apen_dager"] is None
    logg = [json.loads(r[0]) for r in con.execute("SELECT detaljer FROM hendelse WHERE handling='kursside_innstillinger' ORDER BY id")]
    assert [(l["felt"], l["apen_dager"]) for l in logg] == [(["apen_dager"], 365), (["apen_dager"], 0), (["apen_dager"], None),
                                                            (["apen_dager"], 90), (["apen_dager"], None)]
    assert all(set(l) == {"kurs_id", "felt", "apen_dager"} for l in logg)                                   # ingen persondata i loggen


@pytest.mark.parametrize("ugyldig, status", [(-1, 422), (3651, 422), (10 ** 30, 422), ("365", 400), ("tull", 400), (12.5, 400), (365.0, 400),
                                             (True, 400), (False, 400), ([], 400), ({}, 400), ([30], 400)])
def test_ugyldig_apen_dager_via_ruten_avvises_uten_endring(con, kid, admin, ugyldig, status):
    json_post(admin, _u(kid, "/innstillinger"), {"apen_dager": 120})
    r = json_post(admin, _u(kid, "/innstillinger"), {"apen_dager": ugyldig})
    assert r.status_code == status
    assert sidelager.hent(con, kid)["apen_dager"] == 120                                                     # uendret
    if status == 422:
        assert "fra 1 til 3650" in r.get_json()["melding"]


def test_dato_og_dager_samtidig_gir_422_og_intet_lagres(con, kid, admin):
    r = json_post(admin, _u(kid, "/innstillinger"), {"stenges": "2027-12-24", "apen_dager": 30, "innsjekk_krever_kode": False})
    assert r.status_code == 422 and "enten en bestemt dato eller et antall dager" in r.get_json()["melding"]
    rad = sidelager.hent(con, kid)
    assert (rad["stenges"], rad["apen_dager"], rad["innsjekk_krever_kode"]) == (None, None, 1)               # ikke engang kode-valget ble lagret
    r = json_post(admin, _u(kid, "/innstillinger"), {"stenges": "2027-12-24", "apen_dager": None})           # dato + «standard for dager»: greit
    assert r.status_code == 200 and sidelager.hent(con, kid)["stenges"] == "2027-12-24"
    r = json_post(admin, _u(kid, "/innstillinger"), {"stenges": "", "apen_dager": 45})
    assert r.status_code == 200 and (sidelager.hent(con, kid)["stenges"], sidelager.hent(con, kid)["apen_dager"]) == (None, 45)


def test_innstillingene_krever_lagret_side_og_riktig_rolle(con, admin):
    ny = lag_kurs(con, "K7")                                                                                # kurs uten kursside
    r = json_post(admin, _u(ny, "/innstillinger"), {"apen_dager": 30})
    assert r.status_code == 409 and r.get_json()["status"] == "ingen_side"
    kid2 = lag_kurs(con, "K8")
    skriv_side(con, kid2, dokument(tekstblokk()))
    lese = admin_klient(con, "lese")
    assert json_post(lese, _u(kid2, "/innstillinger"), {"apen_dager": 30}).status_code == 403
    assert sidelager.hent(con, kid2)["apen_dager"] is None
    assert json_post(admin_klient(con, "kursadmin"), _u(kid2, "/innstillinger"), {"apen_dager": 30}).status_code == 200
    from kurs.web import app as webapp
    uinnlogget = webapp.app.test_client().post(_u(kid2, "/innstillinger"), json={"apen_dager": 60})
    assert uinnlogget.status_code in (302, 400, 401, 403) and sidelager.hent(con, kid2)["apen_dager"] == 30


# ============================ versjonskonflikt: innstillingen er ikke en del av utkastet ============================

def test_aapningstiden_endrer_ikke_utkastets_versjon_og_gir_ingen_konflikt(con, kid, admin):
    versjon = sidelager.hent_utkast(con, kid)[1]
    json_post(admin, _u(kid, "/innstillinger"), {"apen_dager": 500})
    assert sidelager.hent_utkast(con, kid)[1] == versjon                                                     # samme versjon
    dok = sidelager.hent_utkast(con, kid)[0]
    r = json_post(admin, _u(kid, "/lagre"), {"versjon": versjon, "dokument": dok})                           # en åpen editor lagrer som før
    assert r.status_code == 200 and r.get_json()["status"] == "ok"


def test_lagring_av_utkast_med_gammel_versjon_gir_konflikt_uten_a_rore_aapningstiden(con, kid, admin):
    versjon = sidelager.hent_utkast(con, kid)[1]
    dok = sidelager.hent_utkast(con, kid)[0]
    assert json_post(admin, _u(kid, "/lagre"), {"versjon": versjon, "dokument": dok}).status_code == 200      # noen andre lagret: versjon + 1
    _sett(con, kid, apen_dager=200)
    r = json_post(admin, _u(kid, "/lagre"), {"versjon": versjon, "dokument": dok})                            # min gamle versjon: konflikt
    assert r.status_code == 409 and r.get_json()["status"] == "konflikt"
    assert sidelager.hent(con, kid)["apen_dager"] == 200                                                     # innstillingen står
    assert sidelager.hent(con, kid)["stenges"] is None


def test_utkast_publisering_gjenoppretting_og_mal_endrer_aldri_innstillingen(con, kid, admin):
    _sett(con, kid, apen_dager=365)
    versjon = sidelager.hent_utkast(con, kid)[1]
    ny = sidelager.lagre_utkast(con, kid, dokument(tekstblokk("Ny", "<p>Ny tekst</p>")), versjon, ADMIN)
    assert sidelager.publiser(con, kid, ny, ADMIN) == ny
    v = sidelager.versjoner(con, kid)[-1]["id"]
    assert sidelager.gjenopprett(con, kid, v, ny, ADMIN) is not None
    con.commit()
    assert sidelager.hent(con, kid)["apen_dager"] == 365
    r = json_post(admin, _u(kid, "/mal"), {"versjon": sidelager.hent_utkast(con, kid)[1], "mal": "kurs"})
    assert sidelager.hent(con, kid)["apen_dager"] == 365 and r.status_code in (200, 422)
    sidelager.sett_aktiv(con, kid, False, ADMIN)                                                             # nødbremsen rører den heller ikke
    sidelager.sett_aktiv(con, kid, True, ADMIN)
    con.commit()
    assert sidelager.hent(con, kid)["apen_dager"] == 365


# ============================ kopiering ============================

def test_hent_fra_annet_kurs_kopierer_blokker_men_aldri_aapningstiden(con, kid, admin):
    """Regelen: «Hent fra annet kurs» henter valgte BLOKKER. Innstillingene (nedtatt, kode, «Åpen til», åpningstid) er per kurs og følger ikke med."""
    ny = lag_kurs(con, "K2")
    skriv_side(con, ny, dokument(tekstblokk("Egen", "<p>Egen blokk</p>")))
    _sett(con, kid, apen_dager=sidelager.UBEGRENSET)
    kilde_blokk = sidelager.hent_utkast(con, kid)[0]["blokker"][0]["id"]
    r = json_post(admin, _u(ny, "/hent-fra"), {"versjon": sidelager.hent_utkast(con, ny)[1], "fra_kurs_id": kid, "blokk_ider": [kilde_blokk]})
    assert r.status_code == 200
    assert sidelager.hent(con, ny)["apen_dager"] is None and _stenges(con, ny) == SISTE + timedelta(days=180)      # målkurset: uendret standard
    assert sidelager.hent(con, kid)["apen_dager"] == 0                                                           # kilden: uendret
    _sett(con, ny, apen_dager=45)
    r = json_post(admin, _u(ny, "/hent-fra"), {"versjon": sidelager.hent_utkast(con, ny)[1], "fra_kurs_id": kid, "blokk_ider": [kilde_blokk]})
    assert r.status_code == 200 and sidelager.hent(con, ny)["apen_dager"] == 45                                  # målkursets egen verdi står


def _duplikat_skjema(fra: int) -> dict:
    start = (IDAG + timedelta(days=400)).isoformat()
    return {"navn": "Kurs K1 (ny runde)", "type": "fysisk", "sted": "Bergen", "kapasitet": "16", "datoer": start, "start_kl": "09:00",
            "slutt_kl": "16:00", "timer_pr_dag": "6", "pris_nok": "0", "fakturering": "person", "betaling": "samlet", "faktura_dager_for": "14",
            "ansvarlig_admin_id": "", "paameldingsfrist": "", "kursholder_navn": "", "kursholder_epost": "", "materiell_frist": "", "notat": "",
            "fra": str(fra)}


def test_duplisering_av_kurs_lager_ingen_kursside_og_kopierer_ikke_aapningstiden(con, kid, admin):
    """Regelen: et duplisert kurs får verken Min side eller dens innstillinger (siden bygges på nytt; «Hent fra annet kurs» henter blokker).
    Det nye kurset starter derfor alltid med standarden, også når originalen har egne dager eller ingen tidsbegrensning."""
    _sett(con, kid, apen_dager=1500)
    r = admin.post("/admin/kurs/ny", data=_duplikat_skjema(kid))
    assert r.status_code == 302
    nytt = db.koble(config.DB_STI).execute("SELECT id FROM kurs WHERE id<>?", (kid,)).fetchone()["id"]
    assert con.execute("SELECT COUNT(*) FROM kursside WHERE kurs_id=?", (nytt,)).fetchone()[0] == 0
    skriv_side(con, nytt, dokument(tekstblokk()))                                                              # første lagring: raden lages
    rad = sidelager.hent(con, nytt)
    assert rad["apen_dager"] is None and rad["stenges"] is None
    assert _stenges(con, nytt) == sidelager.siste_kursdag(con, nytt) + timedelta(days=180)
    assert sidelager.hent(con, kid)["apen_dager"] == 1500                                                      # originalen uendret


# ============================ dialogen og lenkeboksen ============================

def test_dialogen_har_fire_valg_og_grensene_i_markeringen(con, kid, admin):
    html = admin.get(_u(kid)).get_data(as_text=True)
    for verdi in ("standard", "dager", "ingen", "dato"):
        assert re.search(rf'<input type="radio" name="ks-apning" value="{verdi}" data-felt="apning-{verdi}">', html), verdi
    assert 'data-felt="apen-dager" min="1" max="3650" step="1"' in html
    assert "Hvor lenge er siden åpen etter siste kursdag?" in html and "Ingen tidsbegrensning" in html
    assert 'data-felt="apning-hjelp" role="status"' in html
    assert 'for="ks-innst-stenges">Åpen til<' not in html                                                       # den gamle enkeltdatoen er erstattet av valgene


def test_lenkeboksen_viser_aapningstiden_i_klartekst_via_javascript_data(con, kid, admin):
    """Teksten «Åpen til 12.03.2027» bygges i nettleseren av dataene i siden (ks-data): datoen er ferdig utregnet på serveren."""
    _sett(con, kid, apen_dager=100)
    i = _innst(admin, kid)
    forventet = SISTE + timedelta(days=100)
    assert i["stenges_effektiv"] == forventet.isoformat() and i["apen_dager"] == 100
    assert f"{forventet.day:02d}.{forventet.month:02d}.{forventet.year}" == forventet.strftime("%d.%m.%Y")


# ============================ etter kritikerens gjennomgang ============================

def test_ruten_endrer_bare_det_som_sendes_kodekravet_rorer_ikke_aapningstiden(con, kid, admin):
    """Dialogen sender bare det som er endret (kursside-admin-dialoger.js). Ruten på sin side rører aldri det som ikke er med, og svaret
    gir hele tilstanden, så en utdatert side rettes opp: «Ingen tidsbegrensning» kan ikke nullstilles av en urelatert endring."""
    _sett(con, kid, apen_dager=sidelager.UBEGRENSET)
    r = json_post(admin, _u(kid, "/innstillinger"), {"innsjekk_krever_kode": False})
    j = r.get_json()
    assert r.status_code == 200 and j["innsjekk_krever_kode"] is False
    assert (j["apen_dager"], j["stenges"], j["stenges_effektiv"]) == (0, None, None)                          # svaret er fasit for siden
    rad = sidelager.hent(con, kid)
    assert (rad["apen_dager"], rad["stenges"], rad["innsjekk_krever_kode"]) == (0, None, 0)
    _sett(con, kid, stenges="2027-12-24")
    j = json_post(admin, _u(kid, "/innstillinger"), {"innsjekk_krever_kode": True}).get_json()
    assert (j["stenges"], j["apen_dager"], j["innsjekk_krever_kode"]) == ("2027-12-24", None, True)
    _sett(con, kid, apen_dager=90)
    j = json_post(admin, _u(kid, "/innstillinger"), {"apen_dager": 45}).get_json()                            # og omvendt: dager rører ikke kodekravet
    assert (j["apen_dager"], j["innsjekk_krever_kode"]) == (45, True)
    logg = [json.loads(r[0]) for r in con.execute("SELECT detaljer FROM hendelse WHERE handling='kursside_innstillinger' ORDER BY id")]
    assert [l["felt"] for l in logg] == [["innsjekk_krever_kode"], ["innsjekk_krever_kode"], ["apen_dager"]]
    assert all("apen_dager" not in l for l in logg[:2])                                                         # loggen sier ikke noe om det som ikke ble rørt


def test_landing_sender_ikke_til_en_stengt_side(con, kid):
    """Landing etter innlogging bruker tilgangssjekken: en side som er stengt er ingen kandidat (ellers ville deltakeren fått en 404-side)."""
    did = lag_deltaker(con, kid)[0]
    _sett(con, kid, apen_dager=30)
    siste_apne = SISTE + timedelta(days=30)
    assert deltakerside.landing(con, did, siste_apne) == "K1"
    assert deltakerside.landing(con, did, siste_apne + timedelta(days=1)) is None


def test_landing_velger_det_apne_kurset_naar_det_andre_er_stengt(con, kid):
    k2 = lag_kurs(con, "K2", start=IDAG + timedelta(days=200))
    skriv_side(con, k2, dokument(tekstblokk()))
    did, did2 = lag_deltaker(con, kid)[0], lag_deltaker(con, k2)[0]
    assert did == did2
    _sett(con, kid, apen_dager=30)
    stengt_dag = SISTE + timedelta(days=31)                                                                     # K1 er stengt, K2 har ikke startet
    assert deltakerside.landing(con, did, stengt_dag) == "K2"
    _sett(con, kid, apen_dager=sidelager.UBEGRENSET)                                                             # K1 åpent igjen: to kurs som ikke pågår i dag
    assert deltakerside.landing(con, did, stengt_dag) is None


def test_forhandsvisningens_stengt_banner_kommer_dagen_etter_siste_apne_dag(con, kid, admin, monkeypatch):
    _sett(con, kid, apen_dager=30)
    siste_apne = SISTE + timedelta(days=30)
    url = _u(kid, "/forhandsvis?versjon=publisert")
    fast_dato(monkeypatch, siste_apne)
    html = admin.get(url).get_data(as_text=True)
    assert "Slik ser deltakerne siden nå" in html and "Siden er stengt: deltakerne ser" not in html            # siste åpne dag er åpen
    fast_dato(monkeypatch, siste_apne + timedelta(days=1))
    assert "Siden er stengt: deltakerne ser «Min side er stengt»" in admin.get(url).get_data(as_text=True)
    _sett(con, kid, apen_dager=sidelager.UBEGRENSET)
    fast_dato(monkeypatch, SISTE + timedelta(days=9999))
    html = admin.get(url).get_data(as_text=True)
    assert "Slik ser deltakerne siden nå" in html and "Siden er stengt: deltakerne ser" not in html


def test_teksten_om_naar_lister_med_navn_tommes_folger_innstillingen(con, kid, admin, monkeypatch):
    """Tallet står ikke fast i malene: det er KURSSIDE_TOM_TABELLER_ETTER_DAGER (samme som morgenjobben bruker)."""
    html = admin.get(_u(kid)).get_data(as_text=True)
    assert "tømmes uansett 30 dager etter kurset" in html and "Tabellen tømmes automatisk 30 dager etter kurset." in html
    monkeypatch.setattr(config, "KURSSIDE_TOM_TABELLER_ETTER_DAGER", 60)
    html = admin.get(_u(kid)).get_data(as_text=True)
    assert "tømmes uansett 60 dager etter kurset" in html and "Tabellen tømmes automatisk 60 dager etter kurset." in html
    assert "30 dager etter kurset" not in html


# ============================ migreringen ============================

def _nr() -> int:
    """Migreringens nummer slås opp på navnet (tåler omnummerering når delene slås sammen)."""
    return next(n for n, navn, _ in migreringer.MIGRERINGER if navn == "kursside_apningstid")


def test_migreringen_legger_til_kolonnen_uten_a_rore_eksisterende_rader(con, kid):
    assert _nr() <= migreringer.KODEVERSJON and "apen_dager" in db.kolonner(con, "kursside")
    sidelager.sett_innstillinger(con, kid, apen_dager=77, stenges=None, aktor=ADMIN)
    con.commit()
    foer = [tuple(r) for r in con.execute("SELECT * FROM kursside ORDER BY kurs_id")]
    migreringer._m17_kursside_apningstid(con)                                                                  # idempotent: gjør ingenting
    migreringer._m17_kursside_apningstid(con)
    con.commit()
    assert [tuple(r) for r in con.execute("SELECT * FROM kursside ORDER BY kurs_id")] == foer
    assert migreringer.kjor_manglende(con) == []


@pytest.mark.kun_sqlite
def test_gammel_database_uten_kolonnen_faar_den_og_beholder_dagens_aapningstid(con, kid):
    """En database på versjon 15 (kursside uten apen_dager): migreringen legger til kolonnen som tom, og siden er åpen som før."""
    con.execute("ALTER TABLE kursside DROP COLUMN apen_dager")
    con.execute("DELETE FROM schema_versjon WHERE versjon >= ?", (_nr(),))
    con.commit()
    assert migreringer.gjeldende_versjon(con) == _nr() - 1 and "apen_dager" not in db.kolonner(con, "kursside")
    assert sidelager.effektiv_stenges(con, kid, sidelager.hent(con, kid)) == SISTE + timedelta(days=180)     # kode uten kolonnen: standard
    assert migreringer.kjor_manglende(con) == list(range(_nr(), migreringer.KODEVERSJON + 1))
    assert "apen_dager" in db.kolonner(con, "kursside")
    assert con.execute("SELECT apen_dager, stenges FROM kursside WHERE kurs_id=?", (kid,)).fetchone()[:] == (None, None)
    assert _stenges(con, kid) == SISTE + timedelta(days=180)
    con.execute("UPDATE kursside SET apen_dager=3650 WHERE kurs_id=?", (kid,))                                # CHECK er med: grensene gjelder også her
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE kursside SET apen_dager=3651 WHERE kurs_id=?", (kid,))
    con.rollback()


def test_skjemafilene_har_kolonnen_med_samme_regel():
    from pathlib import Path
    for fil in ("schema.sql", "schema_postgres.sql"):
        tekst = (Path(migreringer.__file__).parent / fil).read_text(encoding="utf-8")
        assert re.search(r"apen_dager\s+(INTEGER|BIGINT) CHECK \(apen_dager IS NULL OR apen_dager BETWEEN 0 AND 3650\)", tekst), fil
