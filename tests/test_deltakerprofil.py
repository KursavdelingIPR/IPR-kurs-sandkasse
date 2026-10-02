"""Fase 2: deltakerprofil (Deltaker/Kommunikasjon/Logger), kun visning."""
import re
from datetime import date

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


def _kurs(con, kode="T1", **kw):
    return db.opprett_kurs(con, kode=kode, navn="Testkurs", datoer=[date(2027, 5, 1).isoformat()],
                           sharepoint_mappe=f"Kurs/{kode}", **{"pris_nok": 1000, **kw})


def test_deltakerprofil_viser_person_og_paameldingsfelt(con):
    kid = _kurs(con, type="fysisk")  # allergifelt vises kun for ikke-digitale kurs
    pid, _ = db.meld_paa(
        con, kid, epost="a@x.no", fornavn="A", etternavn="Test",
        deltaker={"telefon": "12345678", "arbeidssted": "Sted AS", "yrkestittel": "Psykolog"},
        paamelding={"betaler": "organisasjon", "org_navn": "Firma AS", "org_nr": "123456789",
                    "faktura_ref": "REF-1", "faktura_kommentar": "Betaler i to omganger",
                    "intern_kommentar": "Kjenner familien"},
        sensitivt={"allergier": "Nøtter"},
    )
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
    for felt in ("A Test", "Psykolog", "Sted AS", "Firma AS", "123456789", "REF-1",
                 "Betaler i to omganger", "Nøtter", "Kjenner familien", '<span class="merke ok">Påmeldt</span>'):
        assert felt in tekst


def test_feil_kurs_id_gir_404(con):
    kid1 = _kurs(con, "K1")
    kid2 = _kurs(con, "K2")
    pid, _ = db.meld_paa(con, kid1, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    assert klient.get(f"/admin/kurs/{kid2}/deltaker/{pid}").status_code == 404


def test_ukjent_paamelding_gir_404(con):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    assert klient.get(f"/admin/kurs/{kid}/deltaker/999").status_code == 404


def test_navn_i_deltakerliste_lenker_til_profil(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    assert f"/admin/kurs/{kid}/deltaker/{pid}" in tekst


def test_apning_av_profil_logges_som_sensitivt_vist(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    forste = con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='sensitivt_vist'").fetchone()[0]
    klient.get(f"/admin/kurs/{kid}/deltaker/{pid}")
    etter = con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='sensitivt_vist'").fetchone()[0]
    assert etter == forste + 1
    rad = con.execute("SELECT detaljer FROM hendelse WHERE handling='sensitivt_vist' ORDER BY id DESC LIMIT 1").fetchone()
    assert f'"paamelding_id": {pid}' in rad["detaljer"]


def test_kommunikasjonsfanen_viser_kun_denne_deltakerens_epost_for_dette_kurset(con):
    kid1 = _kurs(con, "K1")
    kid2 = _kurs(con, "K2")
    pid1, _ = db.meld_paa(con, kid1, epost="a@x.no", fornavn="A", etternavn="Test")
    db.meld_paa(con, kid2, epost="b@x.no", fornavn="B", etternavn="Test")
    con.commit()
    db.marker_sendt(con, f"kurs:{kid1}", "a@x.no", "bekreftelse")
    db.marker_sendt(con, f"kurs:{kid2}", "b@x.no", "bekreftelse")  # skal IKKE vises for A
    con.commit()

    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get(f"/admin/kurs/{kid1}/deltaker/{pid1}/kommunikasjon").get_data(as_text=True)
    assert "bekreftelse" in tekst


def test_loggerfanen_viser_hendelser_for_riktig_paamelding_ikke_andre(con):
    kid = _kurs(con, kapasitet=1)
    pid_a, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    pid_b, _ = db.meld_paa(con, kid, epost="b@x.no", fornavn="B", etternavn="Test")  # egen paamelding_id, egen logg
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst_a = klient.get(f"/admin/kurs/{kid}/deltaker/{pid_a}/logger").get_data(as_text=True)
    assert re.search(r"<td>Påmeldt\s", tekst_a)                 # A sin påmelding: «Påmeldt» (har plass)
    assert "Påmeldt – satt på venteliste" not in tekst_a      # B sin påmelding (på venteliste) hører ikke til A


def test_loggerfanen_skiller_paamelding_1_fra_10_og_11(con):
    """Regresjonstest: LIKE-sok maa ikke matche paamelding_id 1 mot 10/11 pga delstreng."""
    kid = _kurs(con, kapasitet=20)
    paameldinger = [db.meld_paa(con, kid, epost=f"p{n}@x.no", fornavn=f"P{n}", etternavn="Test")[0] for n in range(12)]
    con.commit()
    pid_1 = paameldinger[0]
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get(f"/admin/kurs/{kid}/deltaker/{pid_1}/logger").get_data(as_text=True)
    assert len(re.findall(r"<td>Påmeldt\s", tekst)) == 1     # bare påmelding 1 sin egen, ikke 10 og 11 sine
