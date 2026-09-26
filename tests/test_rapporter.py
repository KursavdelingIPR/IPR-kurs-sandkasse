"""Fase 6: rapporter og oversikter paa tvers av kurs."""
from datetime import date, timedelta

import pytest

from kurs import config, db


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
    return db.opprett_kurs(con, kode=kode, datoer=[start.isoformat()], sharepoint_mappe=f"Kurs/{kode}",
                           **{"navn": "Testkurs", "pris_nok": 1000, **kw})


def _fersk(con):
    return db.koble(config.DB_STI)


# ---------------- kurs-rapport ----------------

def test_kurs_rapport_viser_riktige_tall(con):
    kid = _kurs(con, kapasitet=1)
    pa, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")  # bekreftet
    db.meld_paa(con, kid, epost="b@x.no", fornavn="B", etternavn="Test")  # venteliste
    pc, _ = db.meld_paa(con, kid, epost="c@x.no", fornavn="C", etternavn="kapasitet")
    db.meld_av(con, pc)  # avmeldt
    con.execute("INSERT INTO faktura (paamelding_id, belop_nok, status) VALUES (?, 1000, 'sendt')", (pa,))
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get("/admin/rapporter/kurs").get_data(as_text=True)
    assert ">1</td>" in tekst or "1 / 1" in tekst  # bekreftet/kapasitet
    assert "100%" in tekst
    assert "1 000 kr" in tekst


def test_kurs_rapport_filtrerer_paa_status_og_ansvarlig(con):
    admin_id = con.execute("SELECT id FROM admin_bruker").fetchone()[0]
    annen_id = db.opprett_admin_bruker(con, "kari", "Kari Admin", "passord123")
    _kurs(con, "MITT", navn="Mitt kurs", ansvarlig_admin_id=admin_id)
    _kurs(con, "ANNET", navn="Annet kurs", ansvarlig_admin_id=annen_id, status="avlyst")
    con.commit()
    klient = _klient()
    _logg_inn(klient)

    tekst = klient.get(f"/admin/rapporter/kurs?ansvarlig={admin_id}").get_data(as_text=True)
    assert "Mitt kurs" in tekst and "Annet kurs" not in tekst

    tekst2 = klient.get("/admin/rapporter/kurs?status=avlyst").get_data(as_text=True)
    assert "Annet kurs" in tekst2 and "Mitt kurs" not in tekst2


def test_kurs_rapport_filtrerer_paa_periode(con):
    _kurs(con, "TIDLIG", navn="Tidlig kurs", start=date(2025, 1, 1))
    _kurs(con, "SENT", navn="Sent kurs", start=date(2030, 1, 1))
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get("/admin/rapporter/kurs?fra=2029-01-01").get_data(as_text=True)
    assert "Sent kurs" in tekst and "Tidlig kurs" not in tekst


def test_kurs_rapport_csv_har_riktige_kolonner_og_ingen_sensitivt(con):
    kid = _kurs(con, type="fysisk")
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test", sensitivt={"allergier": "Hemmelig-Notter-Info"})
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = klient.get("/admin/rapporter/kurs.csv")
    tekst = r.get_data(as_text=True)
    assert tekst.startswith("﻿")
    assert "Kursnr;Tittel;Start;Slutt;Status" in tekst
    assert "Hemmelig-Notter-Info" not in tekst


def test_kurs_rapport_eksport_logges_uten_soketekst(con):
    kid = _kurs(con, navn="Hemmelig Sokeord Kurs")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.get("/admin/rapporter/kurs.csv?sok=Hemmelig")
    rad = _fersk(con).execute("SELECT * FROM hendelse WHERE handling='rapport_eksportert' ORDER BY id DESC LIMIT 1").fetchone()
    assert rad is not None
    assert '"rapport": "kurs"' in rad["detaljer"]
    assert '"har_sok": true' in rad["detaljer"]
    assert "Hemmelig" not in rad["detaljer"]  # selve soketeksten skal ikke logges
    assert rad["aktor"].startswith("admin:")


def test_rapportsider_krever_innlogging(con):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    for sti in ("/admin/rapporter", "/admin/rapporter/kurs", "/admin/rapporter/kurs.csv",
               "/admin/rapporter/deltakere", "/admin/rapporter/deltakere.csv"):
        r = klient.get(sti)
        assert r.status_code == 302 and "logg-inn" in r.headers["Location"]


# ---------------- deltakerregister ----------------

def test_soek_paa_navn_epost_og_arbeidssted(con):
    kid = _kurs(con)
    db.meld_paa(con, kid, epost="kari.nordmann@example.no", fornavn="Kari", etternavn="Nordmann", deltaker={"arbeidssted": "Bergen kommune"})
    db.meld_paa(con, kid, epost="ola@example.no", fornavn="Ola", etternavn="Hansen", deltaker={"arbeidssted": "DPS Nord"})
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    assert "Kari Nordmann" in klient.get("/admin/rapporter/deltakere?sok=kari").get_data(as_text=True)
    assert "Ola Hansen" not in klient.get("/admin/rapporter/deltakere?sok=kari").get_data(as_text=True)
    assert "Kari Nordmann" in klient.get("/admin/rapporter/deltakere?sok=bergen").get_data(as_text=True)
    assert "Ola Hansen" not in klient.get("/admin/rapporter/deltakere?sok=bergen").get_data(as_text=True)


def test_flere_kurs_filter_og_telling(con):
    kid1 = _kurs(con, "K1")
    kid2 = _kurs(con, "K2")
    db.meld_paa(con, kid1, epost="a@x.no", fornavn="A Flere", etternavn="Kurs")
    db.meld_paa(con, kid2, epost="a@x.no", fornavn="A Flere", etternavn="Kurs")
    db.meld_paa(con, kid1, epost="b@x.no", fornavn="B Ett", etternavn="Kurs")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get("/admin/rapporter/deltakere?kun_flere_kurs=1").get_data(as_text=True)
    assert "A Flere Kurs" in tekst and "B Ett Kurs" not in tekst


def test_deltakerregister_csv_inneholder_avtalte_felt_ingen_sensitivt(con):
    kid = _kurs(con, type="fysisk")
    db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test", deltaker={"telefon": "99999999", "arbeidssted": "Firma AS"},
               sensitivt={"allergier": "Skal-Ikke-Vises"})
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get("/admin/rapporter/deltakere.csv").get_data(as_text=True)
    assert "Fornavn;Etternavn;E-post;Telefon;Arbeidssted" in tekst
    assert "99999999" in tekst and "Firma AS" in tekst
    assert "Skal-Ikke-Vises" not in tekst


def test_deltakerregister_eksport_logges_uten_soketekst(con):
    kid = _kurs(con)
    db.meld_paa(con, kid, epost="hemmelig.person@x.no", fornavn="Hemmelig", etternavn="Person")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.get("/admin/rapporter/deltakere.csv?sok=hemmelig")
    rad = _fersk(con).execute("SELECT * FROM hendelse WHERE handling='rapport_eksportert' ORDER BY id DESC LIMIT 1").fetchone()
    assert '"rapport": "deltakere"' in rad["detaljer"]
    assert "hemmelig" not in rad["detaljer"].lower()


# ---------------- person-detaljside ----------------

def test_person_detaljside_viser_alle_kurs_ingen_sensitivt(con):
    kid1 = _kurs(con, "K1", type="fysisk")
    kid2 = _kurs(con, "K2")
    db.meld_paa(con, kid1, epost="a@x.no", fornavn="A", etternavn="Person", sensitivt={"allergier": "Skjules-Her-Ogsaa"})
    db.meld_paa(con, kid2, epost="a@x.no", fornavn="A", etternavn="Person")
    con.commit()
    did = con.execute("SELECT id FROM deltaker WHERE epost='a@x.no'").fetchone()[0]
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get(f"/admin/rapporter/deltaker/{did}").get_data(as_text=True)
    assert "1001" in tekst and "1002" in tekst   # kursnumrene
    assert "Skjules-Her-Ogsaa" not in tekst


def test_person_detaljside_lenker_til_paameldingsprofil(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    did = con.execute("SELECT id FROM deltaker WHERE epost='a@x.no'").fetchone()[0]
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get(f"/admin/rapporter/deltaker/{did}").get_data(as_text=True)
    assert f"/admin/kurs/{kid}/deltaker/{pid}" in tekst


def test_ukjent_deltaker_gir_404(con):
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    assert klient.get("/admin/rapporter/deltaker/9999").status_code == 404


# ---------------- noekkeltall-dashbord ----------------

def test_dashbord_teller_bekreftede_paameldinger_i_perioden(con):
    kid_i = _kurs(con, "INNE", start=date(2027, 6, 1))
    kid_ute = _kurs(con, "UTE", start=date(2020, 1, 1))
    db.meld_paa(con, kid_i, epost="a@x.no", fornavn="A", etternavn="Test")
    db.meld_paa(con, kid_ute, epost="b@x.no", fornavn="B", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get("/admin/rapporter?fra=2027-01-01&til=2027-12-31").get_data(as_text=True)
    assert ">1<" in tekst  # kun én av de to bekreftede ligger i perioden


def test_dashbord_viser_avmeldt_fra_hendelseslogg(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    db.meld_av(con, pid)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get("/admin/rapporter").get_data(as_text=True)
    assert "Avmeldt i perioden" in tekst  # og selve telleren testes indirekte via _fersk under
    antall = _fersk(con).execute("SELECT COUNT(*) FROM hendelse WHERE handling='avmelding'").fetchone()[0]
    assert antall == 1


def test_dashbord_flere_kurs_lenke_gaar_til_filtrert_register(con):
    kid1 = _kurs(con, "K1")
    kid2 = _kurs(con, "K2")
    db.meld_paa(con, kid1, epost="a@x.no", fornavn="A", etternavn="Test")
    db.meld_paa(con, kid2, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get("/admin/rapporter").get_data(as_text=True)
    assert "kun_flere_kurs=1" in tekst


def test_dashbord_krever_innlogging(con):
    klient = _klient()
    r = klient.get("/admin/rapporter")
    assert r.status_code == 302 and "logg-inn" in r.headers["Location"]
