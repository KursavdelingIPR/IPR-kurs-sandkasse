"""Overbooking: admin kan bekrefte en deltaker på et fullt kurs - men bare etter et eget spørsmål i deltakervinduet.

Uten det uttrykkelige valget (overbooking=1, satt av static/app.js når admin svarer ja) gjelder kapasiteten som før.
Påmeldingsskjemaet, importen og mottaket fra nettsiden overbooker aldri (de bruker ikke sett_paamelding_status).
"""
import json
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
    yield c
    c.close()


def _admin():
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    assert k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN,
                                            "passord": config.ADMIN_PASSORD}).status_code == 302
    return k


def _fullt_kurs(con, kapasitet=3):
    """Et fullt kurs: `kapasitet` bekreftede og én på venteliste. Returnerer (kurs_id, ventelistens paamelding_id)."""
    kid = db.opprett_kurs(con, kode="FULL1", navn="Veiledning i gruppe", datoer=[date(2031, 10, 16).isoformat()],
                          sharepoint_mappe="Kurs/FULL1", pris_nok=2500, kapasitet=kapasitet)
    for i in range(kapasitet):
        db.meld_paa(con, kid, epost=f"b{i}@eksempel.no", fornavn=f"Bekreftet{i}", etternavn="Test")
    pid, status = db.meld_paa(con, kid, epost="kari@eksempel.no", fornavn="Kari", etternavn="Venter")
    assert status == "venteliste"
    con.commit()
    return kid, pid


def _status(con, pid):
    return db.koble(config.DB_STI).execute("SELECT status FROM paamelding WHERE id=?", (pid,)).fetchone()[0]


# ============================ databaselaget ============================

def test_fullt_kurs_avviser_bekreftelse_uten_uttrykkelig_overstyring(con):
    _, pid = _fullt_kurs(con)
    with pytest.raises(db.Paameldingsfeil, match="Kurset er fullt"):
        db.sett_paamelding_status(con, pid, "bekreftet")
    assert con.execute("SELECT status FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == "venteliste"


def test_overstyring_bekrefter_og_loggfoerer_kapasiteten(con):
    kid, pid = _fullt_kurs(con)
    assert db.sett_paamelding_status(con, pid, "bekreftet", aktor="admin:test", tillat_overbooking=True) == pid
    con.commit()
    assert con.execute("SELECT status FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == "bekreftet"
    assert db.antall_bekreftet(con, kid) == 4
    (detaljer,) = con.execute("SELECT detaljer FROM hendelse WHERE handling='status_endret' ORDER BY id DESC").fetchone()
    assert json.loads(detaljer) == {"paamelding_id": pid, "fra": "venteliste", "til": "bekreftet", "over_kapasitet": 3}


def test_vanlig_bekreftelse_med_ledig_plass_loggfoeres_uten_overbooking(con):
    kid, pid = _fullt_kurs(con)
    con.execute("UPDATE kurs SET kapasitet=5 WHERE id=?", (kid,))
    db.sett_paamelding_status(con, pid, "bekreftet", tillat_overbooking=True)       # flagget alene overbooker ikke
    (detaljer,) = con.execute("SELECT detaljer FROM hendelse WHERE handling='status_endret' ORDER BY id DESC").fetchone()
    assert "over_kapasitet" not in json.loads(detaljer)


def test_overstyring_gjelder_ikke_avlyste_kurs(con):
    kid, pid = _fullt_kurs(con)
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kid,))
    with pytest.raises(db.Paameldingsfeil, match="avlyst"):
        db.sett_paamelding_status(con, pid, "bekreftet", tillat_overbooking=True)


def test_etter_overbooking_rykker_ingen_opp_foer_det_er_ledig_plass_igjen(con):
    kid, pid = _fullt_kurs(con, kapasitet=1)
    neste, _ = db.meld_paa(con, kid, epost="neste@eksempel.no", fornavn="Neste", etternavn="Venter")
    db.sett_paamelding_status(con, pid, "bekreftet", tillat_overbooking=True)       # 2 bekreftede på 1 plass
    con.commit()
    bekreftet = con.execute("SELECT id FROM paamelding WHERE kurs_id=? AND status='bekreftet' AND id!=?",
                            (kid, pid)).fetchone()[0]
    db.sett_paamelding_status(con, bekreftet, "avmeldt")                            # 1 bekreftet på 1 plass
    con.commit()
    assert con.execute("SELECT status FROM paamelding WHERE id=?", (neste,)).fetchone()[0] == "venteliste"


# ============================ ruten og deltakervinduet ============================

def test_uten_ja_paa_spoersmaalet_avvises_det_som_foer(con):
    kid, pid = _fullt_kurs(con)
    r = _admin().post(f"/admin/kurs/{kid}/deltaker/{pid}/status", data={"status": "bekreftet"}, follow_redirects=True)
    assert "Kurset er fullt – kan ikke bekrefte flere" in r.get_data(as_text=True)
    assert _status(con, pid) == "venteliste"


def test_ja_paa_spoersmaalet_melder_paa_og_sender_bekreftelsen(con):
    kid, pid = _fullt_kurs(con)
    r = _admin().post(f"/admin/kurs/{kid}/deltaker/{pid}/status", data={"status": "bekreftet", "overbooking": "1"},
                      follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "Status endret til «bekreftet». Kurset har nå 4 bekreftede deltakere på 3 plasser." in html
    fersk = db.koble(config.DB_STI)
    assert fersk.execute("SELECT status FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == "bekreftet"
    assert fersk.execute("SELECT 1 FROM utsending_logg WHERE mottaker='kari@eksempel.no' AND type='bekreftelse'").fetchone()


def test_admin_registrert_og_holdt_tilbake_kan_overbookes(con):
    """Saken fra sandkassen: «Legg til deltaker» på et fullt kurs gir venteliste og holdt behandling."""
    kid, _ = _fullt_kurs(con)
    k = _admin()
    r = k.post(f"/admin/kurs/{kid}/deltaker/ny", data={"fornavn": "Nina", "etternavn": "Ny", "epost": "nina@eksempel.no",
                                                       "betaler": "person"})
    assert r.status_code == 302
    pid = db.koble(config.DB_STI).execute(
        "SELECT p.id FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE d.epost='nina@eksempel.no'").fetchone()[0]
    rad = db.koble(config.DB_STI).execute("SELECT status, sveiper_utsatt FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert (rad["status"], rad["sveiper_utsatt"]) == ("venteliste", 1)
    k.post(f"/admin/kurs/{kid}/deltaker/{pid}/status", data={"status": "bekreftet", "overbooking": "1"})
    rad = db.koble(config.DB_STI).execute("SELECT status, sveiper_utsatt FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert (rad["status"], rad["sveiper_utsatt"]) == ("bekreftet", 0)


def test_deltakervinduet_har_spoersmaalet_naar_kurset_er_fullt(con):
    kid, pid = _fullt_kurs(con)
    html = _admin().get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
    assert ('data-full-bekreft="Er du sikker på at du vil melde denne deltakeren på? '
            'Kurset er fullt (3 av 3 plasser er tatt)."') in html
    assert '<input type="hidden" name="overbooking" value="">' in html
    assert "Du kan likevel melde deltakeren på – du får et eget spørsmål først." in html


def test_entall_naar_kurset_har_én_plass(con):
    kid, pid = _fullt_kurs(con, kapasitet=1)
    html = _admin().get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
    assert "Kurset er fullt (1 av 1 plass er tatt)." in html


def test_ingen_spoersmaal_naar_det_er_ledig_plass_eller_deltakeren_alt_er_bekreftet(con):
    kid, pid = _fullt_kurs(con)
    k = _admin()
    bekreftet = con.execute("SELECT id FROM paamelding WHERE kurs_id=? AND status='bekreftet'", (kid,)).fetchone()[0]
    html = k.get(f"/admin/kurs/{kid}/deltaker/{bekreftet}").get_data(as_text=True)     # kan bare meldes av
    assert "data-full-bekreft" not in html and 'name="overbooking"' not in html
    con.execute("UPDATE kurs SET kapasitet=10 WHERE id=?", (kid,))
    con.commit()
    html = k.get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
    assert "data-full-bekreft" not in html and 'name="overbooking"' not in html


def test_offentlig_paamelding_paa_fullt_kurs_gir_fortsatt_venteliste(con):
    kid, _ = _fullt_kurs(con)
    _, status = db.meld_paa(con, kid, epost="ny@eksempel.no", fornavn="Ny", etternavn="Person")
    assert status == "venteliste"
