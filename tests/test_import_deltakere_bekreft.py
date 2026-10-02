"""Fase 10, trinn 3: bekreft-ruten (atomisk import) og CSV-mal-nedlasting.

Forhaandsvisninger settes opp direkte via import_deltakere-modulen (som i trinn 1-testene) - selve
opplastingsruten er allerede dekket av tests/test_import_deltakere_rute.py (trinn 2).
"""
from datetime import date, datetime, timedelta

import pytest

from adressehjelp import ADRESSE
from kurs import config, db, import_deltakere as imp
from kurs.integrasjoner import epost, visma
from navnehjelp import navnedeler

RUTE_BEKREFT = "/admin/kurs/{kid}/deltakere/importer/bekreft"
RUTE_IMPORT = "/admin/kurs/{kid}/deltakere/importer"
RUTE_MAL = "/admin/deltaker-import-mal.csv"


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


def _admin_id(con):
    return con.execute("SELECT id FROM admin_bruker WHERE brukernavn=?", (config.ADMIN_BRUKERNAVN,)).fetchone()[0]


def _antall_mail(con):
    return len(list(config.UTBOKS.glob("*.html"))) if config.UTBOKS.exists() else 0


def _rad(navn="Kari Nordmann", epost="kari@x.no", **over):
    r = {**navnedeler(navn), "epost": epost, **ADRESSE}
    r.update(over)
    return r


def _lag_preview(con, kid, rader, admin_id=None):
    """Setter opp en gyldig forhaandsvisning akkurat slik opplastingsruten ville gjort det, og
    returnerer token-en."""
    admin_id = admin_id if admin_id is not None else _admin_id(con)
    resultater = imp.forhaandsvis(con, kid, rader, aktor="admin:test")
    con.commit()
    token = imp.lagre_forhaandsvisning(con, kid, admin_id, rader, resultater)
    con.commit()
    return token


def _bekreft(klient, kid, token):
    return klient.post(RUTE_BEKREFT.format(kid=kid), data={"forhaandsvisning_token": token})


# ==================== tilgang ====================

def test_bekreft_ruten_krever_admin(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    token = _lag_preview(con, kid, [_rad()])
    resp = _bekreft(_klient(), kid, token)
    assert resp.status_code == 302
    assert "/admin/logg-inn" in resp.headers["Location"]
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 0


def test_get_kan_ikke_bekrefte(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    token = _lag_preview(con, kid, [_rad()])
    klient = _klient()
    _logg_inn(klient)
    resp = klient.get(RUTE_BEKREFT.format(kid=kid))
    assert resp.status_code == 405
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 0


# ==================== hovedscenario ====================

def test_gyldig_token_uendret_db_gir_vellykket_import(con):
    kid = _kurs(con, kapasitet=5, pris_nok=1000, fakturering="person")
    con.commit()
    token = _lag_preview(con, kid, [_rad()])
    klient = _klient()
    _logg_inn(klient)
    resp = _bekreft(klient, kid, token)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/admin/kurs/{kid}/deltakere")
    rad = con.execute(
        "SELECT status, kilde, sveiper_kjort, sveiper_utsatt FROM paamelding WHERE kurs_id=?", (kid,)).fetchone()
    assert rad["status"] == "bekreftet"
    assert rad["kilde"] == "admin_import"
    assert rad["sveiper_kjort"] == 0
    assert rad["sveiper_utsatt"] == 1


def test_flashmelding_viser_tellinger_uten_persondata(con):
    kid = _kurs(con, kapasitet=1)
    db.meld_paa(con, kid, epost="forst@x.no", fornavn="Forst", etternavn="Test")  # fyller kapasitet
    con.commit()
    token = _lag_preview(con, kid, [_rad(epost="hemmelig.person@x.no", navn="Hemmelig Person")])
    klient = _klient()
    _logg_inn(klient)
    resp = _bekreft(klient, kid, token)
    tekst = klient.get(resp.headers["Location"]).get_data(as_text=True)
    start = tekst.index('class="flash ok"')
    flash_tekst = tekst[start:tekst.index("</div>", start)]
    assert "Import fullført" in flash_tekst
    assert "1" in flash_tekst  # antall
    assert "hemmelig" not in flash_tekst.lower()
    # personen VIL legitimt vises nede i selve deltakerlisten - det er IKKE et personvernbrudd,
    # bare selve flash-oppsummeringen skal vaere fri for persondata


def test_flashmelding_entall_ved_en_deltaker(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    token = _lag_preview(con, kid, [_rad()])
    klient = _klient()
    _logg_inn(klient)
    resp = _bekreft(klient, kid, token)
    tekst = klient.get(resp.headers["Location"]).get_data(as_text=True)
    assert "1 deltaker registrert" in tekst
    assert "(e)" not in tekst


def test_flashmelding_flertall_ved_flere_deltakere(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    token = _lag_preview(con, kid, [_rad(epost="a@x.no"), _rad(epost="b@x.no")])
    klient = _klient()
    _logg_inn(klient)
    resp = _bekreft(klient, kid, token)
    tekst = klient.get(resp.headers["Location"]).get_data(as_text=True)
    assert "2 deltakere registrert" in tekst
    assert "(e)" not in tekst


def test_ingen_epost_faktura_eller_visma_kalles(con, monkeypatch):
    kid = _kurs(con, pris_nok=1000, fakturering="person")
    con.commit()
    token = _lag_preview(con, kid, [_rad()])

    def _skal_ikke_kalles(*a, **kw):
        raise AssertionError("visma.fakturer() skal ALDRI kalles av import")
    monkeypatch.setattr(visma, "fakturer", _skal_ikke_kalles)

    def _skal_ikke_kalles_epost(*a, **kw):
        raise AssertionError("epost.send() skal ALDRI kalles av import")
    monkeypatch.setattr(epost, "send", _skal_ikke_kalles_epost)

    klient = _klient()
    _logg_inn(klient)
    resp = _bekreft(klient, kid, token)
    assert resp.status_code == 302
    assert _antall_mail(con) == 0
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 0


def test_eksisterende_paamelding_hoppes_over_og_telles(con):
    kid = _kurs(con, kapasitet=5)
    db.meld_paa(con, kid, epost="kari@x.no", fornavn="Kari", etternavn="Nordmann")
    con.commit()
    antall_foer = con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0]
    token = _lag_preview(con, kid, [_rad()])
    klient = _klient()
    _logg_inn(klient)
    resp = _bekreft(klient, kid, token)
    tekst = klient.get(resp.headers["Location"]).get_data(as_text=True)
    assert "1" in tekst  # hoppet over
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == antall_foer


def test_ventelisteberegning_korrekt(con):
    kid = _kurs(con, kapasitet=1)
    con.commit()
    token = _lag_preview(con, kid, [_rad(epost="a@x.no"), _rad(epost="b@x.no")])
    klient = _klient()
    _logg_inn(klient)
    _bekreft(klient, kid, token)
    statuser = {r["status"] for r in con.execute("SELECT status FROM paamelding WHERE kurs_id=?", (kid,))}
    assert statuser == {"bekreftet", "venteliste"}


def test_beskytt_eksisterende_felt_virker_ved_import(con):
    kid1 = _kurs(con, "K1")
    kid2 = _kurs(con, "K2", kapasitet=5)
    db.meld_paa(con, kid1, epost="kari@x.no", fornavn="Kari", etternavn="Nordmann", deltaker={"telefon": "11111111"})
    con.commit()
    token = _lag_preview(con, kid2, [_rad(telefon="99999999")])
    klient = _klient()
    _logg_inn(klient)
    _bekreft(klient, kid2, token)
    rad = con.execute("SELECT telefon FROM deltaker WHERE epost='kari@x.no'").fetchone()
    assert rad["telefon"] == "11111111"  # ikke overskrevet


# ==================== token-validering ====================

def test_token_for_annen_admin_avvises(con):
    kid = _kurs(con, kapasitet=5)
    con.execute("INSERT INTO admin_bruker (brukernavn, navn, passord_hash) VALUES ('annen', 'Annen', 'x')")
    con.commit()
    annen_id = con.execute("SELECT id FROM admin_bruker WHERE brukernavn='annen'").fetchone()[0]
    token = _lag_preview(con, kid, [_rad()], admin_id=annen_id)
    klient = _klient()
    _logg_inn(klient)
    resp = _bekreft(klient, kid, token)
    tekst = klient.get(resp.headers["Location"]).get_data(as_text=True)
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 0


def test_token_for_annet_kurs_avvises(con):
    kid1 = _kurs(con, "K1", kapasitet=5)
    kid2 = _kurs(con, "K2", kapasitet=5)
    con.commit()
    token = _lag_preview(con, kid1, [_rad()])
    klient = _klient()
    _logg_inn(klient)
    _bekreft(klient, kid2, token)  # feil kurs_id i URL-en
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 0


def test_utlopt_token_avvises(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    resultater = imp.forhaandsvis(con, kid, [_rad()], aktor="admin:test")
    con.commit()
    token = imp.lagre_forhaandsvisning(con, kid, _admin_id(con), [_rad()], resultater,
                                       idag=datetime.now() - timedelta(hours=1))
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = _bekreft(klient, kid, token)
    tekst = klient.get(resp.headers["Location"]).get_data(as_text=True)
    assert "utløpt" in tekst.lower()
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 0


def test_ukjent_token_avvises(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = _bekreft(klient, kid, "finnes-ikke-i-det-hele-tatt")
    tekst = klient.get(resp.headers["Location"]).get_data(as_text=True)
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 0


# ==================== endret databasetilstand ====================

def test_endret_kapasitet_gir_ingen_import(con):
    kid = _kurs(con, kapasitet=1)
    con.commit()
    token = _lag_preview(con, kid, [_rad()])  # forhaandsvist som "bekreftet"
    db.meld_paa(con, kid, epost="annen@x.no", fornavn="Annen", etternavn="Test")  # fyller kapasiteten i mellomtiden
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = _bekreft(klient, kid, token)
    tekst = klient.get(resp.headers["Location"]).get_data(as_text=True)
    assert "endret seg" in tekst.lower()
    assert "teknisk" not in tekst.lower()
    assert "1 rad berørt" in tekst
    assert "(e)" not in tekst
    # kun den ene ("annen") som allerede var der - ingenting fra token-importen ble lagt til
    assert con.execute("SELECT COUNT(*) FROM paamelding WHERE kurs_id=?", (kid,)).fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM import_forhaandsvisning").fetchone()[0] == 0  # ugyldiggjort


def test_endret_kursstatus_gir_ingen_import(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    token = _lag_preview(con, kid, [_rad()])
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kid,))
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = _bekreft(klient, kid, token)
    tekst = klient.get(resp.headers["Location"]).get_data(as_text=True)
    assert "avlyst" in tekst.lower()
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 0


def test_allerede_paameldt_i_mellomtiden_gir_ingen_import(con):
    """Raden ble forhaandsvist som "ny", men noen andre rakk aa melde paa samme e-post foer
    admin bekreftet - handling skifter fra 'ny' til 'hopp_over', som skal fanges opp."""
    kid = _kurs(con, kapasitet=5)
    con.commit()
    token = _lag_preview(con, kid, [_rad()])
    db.meld_paa(con, kid, epost="kari@x.no", fornavn="Kari", etternavn="Nordmann")  # noen andre meldte paa i mellomtiden
    con.commit()
    antall_foer = con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0]
    klient = _klient()
    _logg_inn(klient)
    resp = _bekreft(klient, kid, token)
    tekst = klient.get(resp.headers["Location"]).get_data(as_text=True)
    assert "endret seg" in tekst.lower()
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == antall_foer


def test_reaktivering_som_har_endret_karakter_gir_ingen_import(con):
    """Raden ble forhaandsvist som reaktivering (bekreftet), men kapasiteten ble fylt opp i
    mellomtiden slik at reaktiveringen na ville blitt venteliste - avvik skal fanges opp."""
    kid = _kurs(con, kapasitet=1)
    pid, _ = db.meld_paa(con, kid, epost="kari@x.no", fornavn="Kari", etternavn="Nordmann")
    db.meld_av(con, pid)
    con.commit()
    token = _lag_preview(con, kid, [_rad()])  # forhaandsvist: reaktiver -> bekreftet
    db.meld_paa(con, kid, epost="annen@x.no", fornavn="Annen", etternavn="Test")  # fyller na kapasiteten
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = _bekreft(klient, kid, token)
    tekst = klient.get(resp.headers["Location"]).get_data(as_text=True)
    assert "endret seg" in tekst.lower()
    rad = con.execute("SELECT status FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert rad["status"] == "avmeldt"  # fortsatt avmeldt - IKKE reaktivert


# ==================== atomisk rollback ====================

def test_atomisk_rollback_ved_feil_paa_rad_n(con, monkeypatch):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    rader = [_rad(epost="a@x.no"), _rad(epost="b@x.no"), _rad(epost="c@x.no")]
    token = _lag_preview(con, kid, rader)

    ekte_meld_paa = db.meld_paa
    kall = {"n": 0}

    def _feiler_paa_rad_2(con_, kurs_id, **kw):
        kall["n"] += 1
        if kall["n"] == 2:
            raise RuntimeError("Uventet databasefeil")
        return ekte_meld_paa(con_, kurs_id, **kw)
    monkeypatch.setattr(db, "meld_paa", _feiler_paa_rad_2)

    klient = _klient()
    _logg_inn(klient)
    resp = _bekreft(klient, kid, token)
    assert resp.status_code == 500  # uventet feil - ikke stille "vellykket"
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 0  # INGEN av de tre ble staaende
    # previewen er IKKE slettet ved en uventet feil - admin kan forsoke igjen
    assert con.execute("SELECT COUNT(*) FROM import_forhaandsvisning WHERE token=?", (token,)).fetchone()[0] == 1


# ==================== logging ====================

def test_per_paamelding_logg_uten_persondata(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    token = _lag_preview(con, kid, [_rad(epost="hemmelig@x.no", navn="Hemmelig Navn")])
    klient = _klient()
    _logg_inn(klient)
    _bekreft(klient, kid, token)
    for r in con.execute("SELECT detaljer FROM hendelse WHERE handling='paamelding'"):
        d = (r["detaljer"] or "").lower()
        assert "hemmelig" not in d


def test_samlet_importlogg_med_riktige_tellinger_uten_persondata(con):
    kid = _kurs(con, kapasitet=1)
    con.commit()
    token = _lag_preview(con, kid, [_rad(epost="a@x.no", navn="Navn A"), _rad(epost="b@x.no", navn="Navn B")])
    klient = _klient()
    _logg_inn(klient)
    _bekreft(klient, kid, token)
    rad = con.execute(
        "SELECT detaljer FROM hendelse WHERE handling='admin_import_utfort' ORDER BY id DESC LIMIT 1").fetchone()
    assert rad is not None
    detaljer = rad["detaljer"]
    assert "navn a" not in detaljer.lower() and "navn b" not in detaljer.lower()
    assert '"antall_importert": 2' in detaljer
    assert '"antall_bekreftet": 1' in detaljer
    assert '"antall_venteliste": 1' in detaljer
    assert f'"kurs_id": {kid}' in detaljer


# ==================== preview slettes / kan ikke gjenbrukes ====================

def test_preview_slettes_etter_vellykket_import(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    token = _lag_preview(con, kid, [_rad()])
    klient = _klient()
    _logg_inn(klient)
    _bekreft(klient, kid, token)
    assert con.execute("SELECT COUNT(*) FROM import_forhaandsvisning WHERE token=?", (token,)).fetchone()[0] == 0


def test_samme_token_kan_ikke_brukes_to_ganger(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    token = _lag_preview(con, kid, [_rad()])
    klient = _klient()
    _logg_inn(klient)
    _bekreft(klient, kid, token)
    antall_etter_forste = con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0]
    resp2 = _bekreft(klient, kid, token)  # samme token igjen (f.eks. dobbeltklikk/refresh)
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == antall_etter_forste  # ikke dobbelt


# ==================== CSV-mal ====================

def test_mal_krever_admin(con):
    resp = _klient().get(RUTE_MAL)
    assert resp.status_code == 302
    assert "/admin/logg-inn" in resp.headers["Location"]


def test_mal_har_riktig_encoding_header_og_eksempelrad(con):
    klient = _klient()
    _logg_inn(klient)
    resp = klient.get(RUTE_MAL)
    assert resp.status_code == 200
    assert resp.mimetype == "text/csv"
    assert "deltaker-import-mal.csv" in resp.headers["Content-Disposition"]
    raw = resp.get_data()
    assert raw.startswith("﻿".encode("utf-8"))  # UTF-8 BOM
    tekst = raw.decode("utf-8-sig")
    linjer = tekst.strip().splitlines()
    assert linjer[0].split(";") == imp.MALFIL_HEADER
    assert len(linjer) == 2
    rader = imp.parse_csv(raw)
    assert len(rader) == 1
    assert "@" in rader[0]["epost"]


def test_mal_lenke_vises_pa_opplastingssiden(con):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get(RUTE_IMPORT.format(kid=kid)).get_data(as_text=True)
    assert RUTE_MAL in tekst
