"""Fase 3: redigering av deltaker (Person og Denne paameldingen), samt statusendring."""
from datetime import date

import pytest

from adressehjelp import ADRESSE
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


def _kurs(con, kode="T1", **kw):
    return db.opprett_kurs(con, kode=kode, navn="Testkurs", datoer=[date(2027, 5, 1).isoformat()],
                           sharepoint_mappe=f"Kurs/{kode}", **{"pris_nok": 1000, **kw})


def _fersk(con):
    return db.koble(config.DB_STI)


# ---------------- person ----------------

def test_person_endring_gjelder_paa_tvers_av_kurs(con):
    k1, k2 = _kurs(con, "K1"), _kurs(con, "K2")
    p1, _ = db.meld_paa(con, k1, epost="a@x.no", fornavn="A", etternavn="Test")
    db.meld_paa(con, k2, epost="a@x.no", fornavn="A", etternavn="Test")  # samme person, samme e-post
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = klient.post(f"/admin/kurs/{k1}/deltaker/{p1}/person",
                    data={"fornavn": "A", "etternavn": "Test", "epost": "a@x.no", "telefon": "99999999",
                          "yrkestittel": "", "arbeidssted": "", **ADRESSE}, follow_redirects=True)
    assert r.status_code == 200
    tekst_k2 = klient.get(f"/admin/kurs/{k2}/deltakere").get_data(as_text=True)
    assert "99999999" in tekst_k2


def test_person_endring_oppdaterer_alle_paameldinger_til_personen(con):
    k1, k2 = _kurs(con, "K1"), _kurs(con, "K2")
    p1, _ = db.meld_paa(con, k1, epost="a@x.no", fornavn="A", etternavn="Test")
    p2, _ = db.meld_paa(con, k2, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    for pid in (p1, p2):
        con.execute("UPDATE paamelding SET oppdatert='2020-01-01T00:00:00' WHERE id=?", (pid,))
    con.commit()

    klient = _klient()
    _logg_inn(klient)
    klient.post(f"/admin/kurs/{k1}/deltaker/{p1}/person",
               data={"fornavn": "A", "etternavn": "Test", "epost": "a@x.no", "telefon": "12345678", "yrkestittel": "", "arbeidssted": "", **ADRESSE})

    fersk = _fersk(con)
    for pid in (p1, p2):
        rad = fersk.execute("SELECT oppdatert FROM paamelding WHERE id=?", (pid,)).fetchone()
        assert rad["oppdatert"] != "2020-01-01T00:00:00"


def test_ugyldig_epost_avvises(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(f"/admin/kurs/{kid}/deltaker/{pid}/person",
               data={"fornavn": "A", "etternavn": "Test", "epost": "ikke-en-epost", "telefon": "", "yrkestittel": "", "arbeidssted": "", **ADRESSE})
    assert _fersk(con).execute("SELECT epost FROM deltaker WHERE id=(SELECT deltaker_id FROM paamelding WHERE id=?)",
                               (pid,)).fetchone()["epost"] == "a@x.no"


def test_epost_som_allerede_er_i_bruk_avvises(con):
    kid = _kurs(con, kapasitet=5)
    p1, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    db.meld_paa(con, kid, epost="b@x.no", fornavn="B", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = klient.post(f"/admin/kurs/{kid}/deltaker/{p1}/person",
                    data={"fornavn": "A", "etternavn": "Test", "epost": "b@x.no", "telefon": "", "yrkestittel": "", "arbeidssted": "", **ADRESSE},
                    follow_redirects=True)
    assert "allerede i bruk" in r.get_data(as_text=True)
    assert _fersk(con).execute("SELECT epost FROM deltaker WHERE id=(SELECT deltaker_id FROM paamelding WHERE id=?)",
                               (p1,)).fetchone()["epost"] == "a@x.no"


def test_person_endring_logges_uten_verdier(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test", deltaker={"telefon": "111"})
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(f"/admin/kurs/{kid}/deltaker/{pid}/person",
               data={"fornavn": "A", "etternavn": "Test", "epost": "a@x.no", "telefon": "999", "yrkestittel": "", "arbeidssted": "", **ADRESSE})
    rad = _fersk(con).execute(
        "SELECT detaljer FROM hendelse WHERE handling='deltaker_endret' ORDER BY id DESC LIMIT 1").fetchone()
    assert "telefon" in rad["detaljer"]
    assert "111" not in rad["detaljer"]
    assert "999" not in rad["detaljer"]


# ---------------- denne paameldingen ----------------

def test_paameldingsfelt_kan_oppdateres(con):
    kid = _kurs(con, type="fysisk")
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(f"/admin/kurs/{kid}/deltaker/{pid}/paamelding", data={
        "betaler": "organisasjon", "org_navn": "Firma AS", "org_nr": "999900003",
        "faktura_adresse": "Gate 1", "faktura_postnr": "5000", "faktura_sted": "Bergen",
        "faktura_ref": "REF-9", "faktura_kommentar": "Merknad", "intern_kommentar": "Internt",
        "allergier": "Nøtter", "tilrettelegging": "",
    })
    rad = _fersk(con).execute("SELECT * FROM paamelding WHERE id=?", (pid,)).fetchone()
    # Felles regel: firmanavn og adresse kommer fra registeret - det administrator skriver ("Firma AS", "Gate 1"), lagres ikke
    assert (rad["org_navn"], rad["org_nr"], rad["faktura_adresse"], rad["faktura_postnr"], rad["faktura_sted"],
            rad["faktura_ref"], rad["faktura_kommentar"], rad["intern_kommentar"]) == (
        "EKSEMPEL KOMMUNE", "999900003", "Postboks 100", "1234", "EKSEMPELBY", "REF-9", "Merknad", "Internt")
    sensitivt = _fersk(con).execute("SELECT allergier FROM sensitivt WHERE paamelding_id=?", (pid,)).fetchone()
    assert sensitivt["allergier"] == "Nøtter"


def test_firma_betaler_uten_org_info_avvises(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = klient.post(f"/admin/kurs/{kid}/deltaker/{pid}/paamelding",
                    data={"betaler": "organisasjon", "org_navn": "", "org_nr": ""}, follow_redirects=True)
    assert "Fyll inn organisasjonsnummer" in r.get_data(as_text=True)
    assert _fersk(con).execute("SELECT betaler FROM paamelding WHERE id=?", (pid,)).fetchone()["betaler"] == "person"


def test_allergi_endring_logges_uten_innhold(con):
    kid = _kurs(con, type="fysisk")
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(f"/admin/kurs/{kid}/deltaker/{pid}/paamelding", data={
        "betaler": "person", "allergier": "Hemmelig allergi mot peanøtter", "tilrettelegging": "",
    })
    rad = _fersk(con).execute(
        "SELECT detaljer FROM hendelse WHERE handling='sensitivt_endret' ORDER BY id DESC LIMIT 1").fetchone()
    assert rad is not None
    assert "peanøtter" not in rad["detaljer"]
    assert "allergi" not in rad["detaljer"].lower()


def test_prisvalg_ikke_redigerbart_naar_kurset_har_bare_en_samling_selv_om_kurset_er_samlet(con):
    # Med flere samlinger kan administrator gjøre et unntak for én deltaker (Camilla 02.10.2026, test_faktura_plan_unntak.py);
    # med én samling er det ikke noe å velge.
    kid = _kurs(con, betaling="samlet")  # én samling
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(f"/admin/kurs/{kid}/deltaker/{pid}/paamelding", data={"betaler": "person", "betaling": "per_samling"})
    assert _fersk(con).execute("SELECT betaling FROM paamelding WHERE id=?", (pid,)).fetchone()["betaling"] == "samlet"


def test_prisvalg_ikke_redigerbart_etter_fakturering(con):
    kid = _kurs(con, betaling="deltaker_velger", pris_nok=0)  # pris 0 -> ingen faktura opprettes av sveiper
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    con.execute("INSERT INTO faktura (paamelding_id, belop_nok, status) VALUES (?, 1000, 'sendt')", (pid,))
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(f"/admin/kurs/{kid}/deltaker/{pid}/paamelding", data={"betaler": "person", "betaling": "per_samling"})
    assert _fersk(con).execute("SELECT betaling FROM paamelding WHERE id=?", (pid,)).fetchone()["betaling"] == "samlet"


def test_prisvalg_redigerbart_naar_trygt(con):
    kid = db.opprett_kurs(con, kode="T1", navn="Testkurs", datoer=["2027-05-01", "2027-06-01"], sharepoint_mappe="Kurs/T1",
                          pris_nok=1000, betaling="deltaker_velger")                   # to samlinger
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    assert 'value="per_samling"' in klient.get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
    klient.post(f"/admin/kurs/{kid}/deltaker/{pid}/paamelding", data={"betaler": "person", "betaling": "per_samling"})
    assert _fersk(con).execute("SELECT betaling FROM paamelding WHERE id=?", (pid,)).fetchone()["betaling"] == "per_samling"


def test_prisvalg_finnes_ikke_naar_kurset_har_bare_en_samling(con):
    kid = _kurs(con, betaling="deltaker_velger")                                        # bare én samling
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test", paamelding={"betaling": "per_samling"})
    con.commit()
    assert _fersk(con).execute("SELECT betaling FROM paamelding WHERE id=?", (pid,)).fetchone()["betaling"] == "samlet"
    klient = _klient()
    _logg_inn(klient)
    assert 'value="per_samling"' not in klient.get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
    klient.post(f"/admin/kurs/{kid}/deltaker/{pid}/paamelding", data={"betaler": "person", "betaling": "per_samling"})
    assert _fersk(con).execute("SELECT betaling FROM paamelding WHERE id=?", (pid,)).fetchone()["betaling"] == "samlet"


# ---------------- statusendring ----------------

def test_venteliste_til_bekreftet_under_kapasitet(con):
    kid = _kurs(con, kapasitet=1)
    db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")  # bekreftet, fyller kapasitet
    pb, _ = db.meld_paa(con, kid, epost="b@x.no", fornavn="B", etternavn="Test")  # venteliste
    con.commit()
    con.execute("UPDATE kurs SET kapasitet=2 WHERE id=?", (kid,))  # gi plass til aa bekrefte B manuelt
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(f"/admin/kurs/{kid}/deltaker/{pb}/status", data={"status": "paameldt"})
    fersk = _fersk(con)
    assert fersk.execute("SELECT status FROM paamelding WHERE id=?", (pb,)).fetchone()["status"] == "bekreftet"
    # sveiper skal ha kjort for den nybekreftede -> bekreftelse-epost sendt
    assert fersk.execute("SELECT 1 FROM utsending_logg WHERE mottaker='b@x.no' AND type='bekreftelse'").fetchone()


def test_venteliste_til_bekreftet_avvises_naar_fullt(con):
    kid = _kurs(con, kapasitet=1)
    db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    pb, _ = db.meld_paa(con, kid, epost="b@x.no", fornavn="B", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = klient.post(f"/admin/kurs/{kid}/deltaker/{pb}/status", data={"status": "paameldt"}, follow_redirects=True)
    assert "fullt" in r.get_data(as_text=True).lower()
    assert _fersk(con).execute("SELECT status FROM paamelding WHERE id=?", (pb,)).fetchone()["status"] == "venteliste"


def test_avmelding_via_status_gir_riktig_opprykk(con):
    kid = _kurs(con, kapasitet=1)
    pa, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    pb, _ = db.meld_paa(con, kid, epost="b@x.no", fornavn="B", etternavn="Test")  # venteliste
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(f"/admin/kurs/{kid}/deltaker/{pa}/status", data={"status": "avmeldt"})
    fersk = _fersk(con)
    assert fersk.execute("SELECT status FROM paamelding WHERE id=?", (pa,)).fetchone()["status"] == "avmeldt"
    assert fersk.execute("SELECT status FROM paamelding WHERE id=?", (pb,)).fetchone()["status"] == "bekreftet"


def test_avmeldt_til_bekreftet_gjenaapning(con):
    kid = _kurs(con, kapasitet=5)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    db.meld_av(con, pid)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(f"/admin/kurs/{kid}/deltaker/{pid}/status", data={"status": "paameldt"})
    assert _fersk(con).execute("SELECT status FROM paamelding WHERE id=?", (pid,)).fetchone()["status"] == "bekreftet"


def test_bekreftet_til_venteliste_er_tillatt_og_personen_rykker_ikke_rett_opp_igjen(con):
    """IPRs beslutning 27.09.2026: admin kan sette en bekreftet deltaker på venteliste. Personen blir stående der og
    rykker ikke rett opp igjen i samme operasjon - selv om hun meldte seg på først og står først i køen. Står andre på
    ventelisten, får neste av dem plassen etter dagens regler (se også tests/test_paameldingsstatus.py)."""
    kid = _kurs(con, kapasitet=1)
    a, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")    # bekreftet
    b, _ = db.meld_paa(con, kid, epost="b@x.no", fornavn="B", etternavn="Test")    # venteliste
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = klient.post(f"/admin/kurs/{kid}/deltaker/{a}/status", data={"status": "venteliste"}, follow_redirects=True)
    tekst = r.get_data(as_text=True)
    assert "Status endret til «venteliste»." in tekst
    assert "Første på ventelisten har rykket opp og fått plassen." in tekst
    status = dict(_fersk(con).execute("SELECT id, status FROM paamelding WHERE kurs_id=?", (kid,)).fetchall())
    assert (status[a], status[b]) == ("venteliste", "bekreftet")


def test_bekreftet_til_venteliste_uten_andre_paa_ventelisten_blir_staaende(con):
    kid = _kurs(con, kapasitet=1)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")  # bekreftet
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = klient.post(f"/admin/kurs/{kid}/deltaker/{pid}/status", data={"status": "venteliste"}, follow_redirects=True)
    assert "Første på ventelisten har rykket opp" not in r.get_data(as_text=True)
    assert _fersk(con).execute("SELECT status FROM paamelding WHERE id=?", (pid,)).fetchone()["status"] == "venteliste"


def test_statusendring_logges_med_gammel_og_ny_status(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(f"/admin/kurs/{kid}/deltaker/{pid}/status", data={"status": "avmeldt"})
    rad = _fersk(con).execute(
        "SELECT detaljer FROM hendelse WHERE handling='status_endret' ORDER BY id DESC LIMIT 1").fetchone()
    assert '"fra": "paameldt"' in rad["detaljer"] and '"til": "avmeldt"' in rad["detaljer"]     # visningsstatusene lagres
