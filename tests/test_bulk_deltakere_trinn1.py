"""Fase 11, trinn 1: personvern-/vilkårslenker, filtrert eksport, eksport av valgte, avkrysnings-UI.

Ingen bulk send/behandle ennå - det kommer i trinn 2/3. Se dokumentasjon/pindena-ipr-gap-analyse.html
for helheten og planen for fase 11.
"""
from datetime import date, timedelta

import pytest

from kurs import config, db
from listehjelp import synlige_rader

RUTE_DELTAKERE = "/admin/kurs/{kid}/deltakere"
RUTE_CSV = "/admin/kurs/{kid}/deltakere.csv"
RUTE_VALGTE_CSV = "/admin/kurs/{kid}/deltakere/eksporter-valgte.csv"


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


def _csv_rader(resp) -> list[str]:
    tekst = resp.get_data(as_text=True)
    return [r for r in tekst.strip().splitlines()[1:] if r]  # uten header


# ==================== personvern-/vilkårslenker ====================

def test_personvernlenke_er_rettet_i_kurs_html(con):
    kid = _kurs(con)
    con.commit()
    tekst = _klient().get("/kurs/T1").get_data(as_text=True)
    assert 'href="#"' not in tekst
    assert "https://www.ipr.no/om-oss/personvernerkl%C3%A6ring" in tekst
    assert "https://www.ipr.no/kurs-og-utdanning/artikkel/kurs-salgsbetingelser" in tekst
    assert 'target="_blank"' in tekst
    assert 'rel="noopener"' in tekst


def test_personvernlenke_er_rettet_i_kurs_gruppe_html(con):
    kid = _kurs(con)
    con.commit()
    tekst = _klient().get("/kurs/T1/gruppe").get_data(as_text=True)
    assert 'href="#"' not in tekst
    assert "https://www.ipr.no/om-oss/personvernerkl%C3%A6ring" in tekst
    assert "https://www.ipr.no/kurs-og-utdanning/artikkel/kurs-salgsbetingelser" in tekst
    assert 'target="_blank"' in tekst
    assert 'rel="noopener"' in tekst


def test_samtykkelogikk_er_uendret(con):
    """Selve validering (kreves fortsatt) skal ikke ha endret seg - kun lenkemålet."""
    kid = _kurs(con)
    con.commit()
    resp = _klient().post("/kurs/T1", data={"fornavn": "Kari", "etternavn": "Test", "epost": "kari@x.no"})  # ingen samtykke
    assert resp.status_code == 400
    assert "godta vilkår" in resp.get_data(as_text=True).lower()


# ==================== filtrert eksport ====================

def test_eksport_uten_filter_gir_alle(con):
    kid = _kurs(con, kapasitet=5)
    db.meld_paa(con, kid, epost="a@x.no", fornavn="Kari", etternavn="Nordmann")
    db.meld_paa(con, kid, epost="b@x.no", fornavn="Ola", etternavn="Hansen")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = klient.get(RUTE_CSV.format(kid=kid))
    assert len(_csv_rader(resp)) == 2


def test_eksport_med_sok_filtrerer(con):
    kid = _kurs(con, kapasitet=5)
    db.meld_paa(con, kid, epost="a@x.no", fornavn="Kari", etternavn="Nordmann")
    db.meld_paa(con, kid, epost="b@x.no", fornavn="Ola", etternavn="Hansen")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = klient.get(RUTE_CSV.format(kid=kid) + "?sok=kari")
    rader = _csv_rader(resp)
    assert len(rader) == 1
    assert "Kari;Nordmann" in rader[0]


def test_eksport_med_status_filtrerer(con):
    kid = _kurs(con, kapasitet=1)
    db.meld_paa(con, kid, epost="a@x.no", fornavn="Forst", etternavn="Test")  # bekreftet
    db.meld_paa(con, kid, epost="b@x.no", fornavn="Andre", etternavn="Test")  # venteliste
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = klient.get(RUTE_CSV.format(kid=kid) + "?status=venteliste")
    rader = _csv_rader(resp)
    assert len(rader) == 1
    assert "Andre" in rader[0]


def test_eksport_matcher_deltakerlistens_eget_filter(con):
    """Filteret i eksporten skal vaere IDENTISK med det deltakerlisten selv bruker."""
    kid = _kurs(con, kapasitet=5)
    db.meld_paa(con, kid, epost="a@x.no", fornavn="Kari", etternavn="Nordmann")
    db.meld_paa(con, kid, epost="b@x.no", fornavn="Ola", etternavn="Hansen")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    liste = synlige_rader(klient.get(RUTE_DELTAKERE.format(kid=kid) + "?sok=kari").get_data(as_text=True))
    eksport = _csv_rader(klient.get(RUTE_CSV.format(kid=kid) + "?sok=kari"))
    assert "Kari Nordmann" in liste and "Ola Hansen" not in liste
    assert len(eksport) == 1 and "Kari;Nordmann" in eksport[0]


def test_eksport_har_cache_control_no_store(con):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = klient.get(RUTE_CSV.format(kid=kid))
    assert resp.headers.get("Cache-Control") == "no-store"


def test_eksport_krever_admin(con):
    kid = _kurs(con)
    con.commit()
    resp = _klient().get(RUTE_CSV.format(kid=kid))
    assert resp.status_code == 302
    assert "/admin/logg-inn" in resp.headers["Location"]


# ==================== eksport av valgte ====================

def test_eksporter_valgte_gir_kun_utvalget(con):
    kid = _kurs(con, kapasitet=5)
    p1, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="Kari", etternavn="Nordmann")
    db.meld_paa(con, kid, epost="b@x.no", fornavn="Ola", etternavn="Hansen")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = klient.post(RUTE_VALGTE_CSV.format(kid=kid), data={"paamelding_id": [str(p1)]})
    rader = _csv_rader(resp)
    assert len(rader) == 1
    assert "Kari;Nordmann" in rader[0]
    assert resp.headers.get("Cache-Control") == "no-store"


def test_eksporter_valgte_krever_admin(con):
    kid = _kurs(con)
    con.commit()
    resp = _klient().post(RUTE_VALGTE_CSV.format(kid=kid), data={"paamelding_id": ["1"]})
    assert resp.status_code == 302
    assert "/admin/logg-inn" in resp.headers["Location"]


def test_eksporter_valgte_uten_utvalg_gir_feilmelding(con):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = klient.post(RUTE_VALGTE_CSV.format(kid=kid), data={})
    assert resp.status_code == 302  # redirect tilbake, ingen fil
    assert resp.headers["Location"].endswith(f"/admin/kurs/{kid}/deltakere")


def test_eksporter_valgte_id_fra_annet_kurs_filtreres_bort(con):
    kid1 = _kurs(con, "K1", kapasitet=5)
    kid2 = _kurs(con, "K2", kapasitet=5)
    p1, _ = db.meld_paa(con, kid1, epost="a@x.no", fornavn="Kari", etternavn="Nordmann")  # tilhorer kid1
    p2, _ = db.meld_paa(con, kid2, epost="b@x.no", fornavn="Ola", etternavn="Hansen")  # tilhorer kid2
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    # forsoker aa eksportere BEGGE via kid1 sin rute - kun kid1 sin egen skal med
    resp = klient.post(RUTE_VALGTE_CSV.format(kid=kid1), data={"paamelding_id": [str(p1), str(p2)]})
    rader = _csv_rader(resp)
    assert len(rader) == 1
    assert "Kari;Nordmann" in rader[0]
    assert "Ola;Hansen" not in rader[0]


# ==================== avkrysnings-UI ====================

def test_deltakerliste_har_avkrysningsbokser_og_velg_alle(con):
    kid = _kurs(con, kapasitet=5)
    db.meld_paa(con, kid, epost="a@x.no", fornavn="Kari", etternavn="Nordmann")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get(RUTE_DELTAKERE.format(kid=kid)).get_data(as_text=True)
    assert 'name="paamelding_id"' in tekst
    assert 'id="velg-alle-synlige"' in tekst
    assert 'form="valgte-deltakere"' in tekst


def test_deltakerliste_har_eksporter_valgte_knapp(con):
    kid = _kurs(con, kapasitet=5)
    db.meld_paa(con, kid, epost="a@x.no", fornavn="Kari", etternavn="Nordmann")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get(RUTE_DELTAKERE.format(kid=kid)).get_data(as_text=True)
    assert "Eksporter valgte" in tekst
    assert RUTE_VALGTE_CSV.format(kid=kid) in tekst


def test_eksportknapp_i_deltakerliste_folger_aktivt_filter(con):
    kid = _kurs(con, kapasitet=5)
    db.meld_paa(con, kid, epost="a@x.no", fornavn="Kari", etternavn="Nordmann")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get(RUTE_DELTAKERE.format(kid=kid) + "?sok=kari").get_data(as_text=True)
    assert "sok=kari" in tekst
