"""Fase 14 (økonomirapporter) og fase 17 (avslått påmelding).

Fase 15 (produkt/rabatt) og 16 (spørreundersøkelser) er vurdert og bevisst IKKE bygget - se STATUS-PAMELDINGSSYSTEM.md.
"""
import csv
import io
import json
from datetime import date, timedelta

import pytest
from werkzeug.datastructures import MultiDict

from adressehjelp import ADRESSE
from kurs import config, db, migreringer, okonomi
from kurs.integrasjoner import epost
from listehjelp import synlige_rader
from navnehjelp import navnedeler

NBSP = " "


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


@pytest.fixture
def sendt(monkeypatch):
    ut = []
    monkeypatch.setattr(epost, "send", lambda til, emne, html, *a, **kw: ut.append((til, emne, html)))
    return ut


def _admin(brukernavn=None, passord=None):
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    r = k.post("/admin/logg-inn", data={"brukernavn": brukernavn or config.ADMIN_BRUKERNAVN,
                                        "passord": passord or config.ADMIN_PASSORD})
    assert r.status_code == 302
    return k


def _leser():
    return _admin("leser", "passord-som-holder")


def _kurs(con, kode, navn=None, start=date(2031, 9, 1), **kw):
    kid = db.opprett_kurs(con, kode=kode, navn=navn or f"Kurs {kode}", datoer=[start.isoformat()],
                          sharepoint_mappe=f"K/{kode}", **{"pris_nok": 4000, "kapasitet": 10, **kw})
    con.commit()
    return kid


def _paamelding(con, kid, epost_, navn=None, **kw):
    pid, status = db.meld_paa(con, kid, epost=epost_, **navnedeler(navn or epost_.split("@")[0].title()), **kw)
    con.commit()
    return pid


def _faktura(con, pid, belop, dato, status="sendt", nr=None, kursdag_id=None):
    db.sett_inn(con, """INSERT INTO faktura (paamelding_id, kursdag_id, visma_id, faktura_nr, belop_nok, status, opprettet)
                        VALUES (?,?,?,?,?,?,?)""", (pid, kursdag_id, f"v-{nr}", nr, belop, status, f"{dato} 10:00:00"))
    con.commit()


@pytest.fixture
def fakturaer(con):
    """To kurs, fakturaer i januar og februar 2031, én kreditert og én fra 2030 (utenfor standardperioden)."""
    a, b = _kurs(con, "OA", "Grunnkurs"), _kurs(con, "OB", "Parterapi")
    pa1, pa2 = _paamelding(con, a, "ola@eksempel.no", "Ola Test"), _paamelding(con, a, "kari@eksempel.no", "Kari Test")
    pb1 = _paamelding(con, b, "per@eksempel.no", "Per Test", paamelding={"betaler": "organisasjon",
                                                                       "org_navn": "Klinikk AS", "org_nr": "999888777"})
    _faktura(con, pa1, 4000, "2031-01-15", nr="1001")
    _faktura(con, pa2, 4000, "2031-01-20", nr="1002", status="kreditert")
    _faktura(con, pb1, 2500, "2031-02-03", nr="1003")
    tidligere = _kurs(con, "OC", "Høstkurs", start=date(2030, 11, 1))            # Kari sitt kurs året før
    _faktura(con, _paamelding(con, tidligere, "kari@eksempel.no", "Kari Test"), 1234, "2030-12-31", nr="0999")
    return {"a": a, "b": b, "ola": pa1, "kari": pa2, "per": pb1}


# ============================ fase 14: modell ============================

def test_periode_standard_er_inneverende_aar_og_ugyldig_gir_standard():
    idag = date(2031, 5, 5)
    assert okonomi.periode(MultiDict(), idag) == (date(2031, 1, 1), date(2031, 12, 31))
    assert okonomi.periode(MultiDict({"fra": "2031-02-01", "til": "2031-02-28"}), idag) == (date(2031, 2, 1), date(2031, 2, 28))
    for feil in ({"fra": "tull"}, {"fra": "2031-03-01", "til": "2031-02-01"}):
        assert okonomi.periode(MultiDict(feil), idag) == (date(2031, 1, 1), date(2031, 12, 31))


def test_periode_paa_en_enkelt_dag_godtas():
    idag = date(2031, 5, 5)
    en_dag = MultiDict({"fra": "2031-03-10", "til": "2031-03-10"})
    assert okonomi.periode(en_dag, idag) == (date(2031, 3, 10), date(2031, 3, 10))


def test_rapporten_summerer_uten_krediterte_per_maaned_og_per_kurs(con, fakturaer):
    r = okonomi.rapport(okonomi.fakturaer(con, date(2031, 1, 1), date(2031, 12, 31)))
    assert (r.totalt.antall, r.totalt.belop, r.totalt.kreditert) == (2, 6500, 4000)
    assert {m: (s.antall, s.belop) for m, s in r.per_maaned.items()} == {"2031-01": (1, 4000), "2031-02": (1, 2500)}
    assert {k["navn"]: k["sum"].belop for k in r.per_kurs.values()} == {"Grunnkurs": 4000, "Parterapi": 2500}
    assert [f["faktura_nr"] for f in r.fakturaer] == ["1001", "1002", "1003"]           # 2030 er utenfor perioden
    per = [f for f in r.fakturaer if f["faktura_nr"] == "1003"][0]
    assert per["betaler_navn"] == "Klinikk AS" and per["gjelder"] == "Samlet" and per["statusnavn"] == "Sendt"


def test_kursfilter_og_deltakerfilter(con, fakturaer):
    assert len(okonomi.fakturaer(con, date(2031, 1, 1), date(2031, 12, 31), kurs_id=fakturaer["b"])) == 1
    kari = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (fakturaer["kari"],)).fetchone()[0]
    assert len(okonomi.fakturaer(con, date(2000, 1, 1), date(2100, 12, 31), deltaker_id=kari)) == 2


# ============================ fase 14: sider og CSV ============================

def test_oekonomisiden_viser_tall_maaneder_og_forklaring(con, fakturaer):
    html = _admin().get("/admin/rapporter/okonomi?fra=2031-01-01&til=2031-12-31").get_data(as_text=True)
    assert f"6{NBSP}500{NBSP}kr" in html and f"4{NBSP}000{NBSP}kr" in html               # fakturert og kreditert
    assert "januar 2031" in html and "februar 2031" in html
    assert "hentes ikke" in html and "tilbake hit ennå" in html                          # ærlig om innbetalinger
    assert "Klinikk AS" in html and "0999" not in html
    assert 'href="/admin/rapporter/okonomi.csv?fra=2031-01-01&amp;til=2031-12-31"' in html


def test_rapportsiden_lenker_til_oekonomi(con):
    assert "Økonomi og fakturaliste →" in _admin().get("/admin/rapporter").get_data(as_text=True)


def test_csv_har_fakturalisten_og_logges_uten_personopplysninger(con, fakturaer):
    r = _admin().get("/admin/rapporter/okonomi.csv?fra=2031-01-01&til=2031-12-31")
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    rader = list(csv.reader(io.StringIO(r.get_data(as_text=True).lstrip("﻿")), delimiter=";"))
    assert rader[0] == okonomi.CSV_KOLONNER and len(rader) == 4
    assert rader[1] == ["2031-01-15", "1001", str(con.execute("SELECT kursnr FROM kurs WHERE id=?", (fakturaer["a"],)).fetchone()[0]),
                        "Grunnkurs", "Samlet", "Ola Test", "Ola Test", "4000", "Sendt"]
    logg = con.execute("SELECT detaljer FROM hendelse WHERE handling='rapport_eksportert'").fetchone()["detaljer"]
    assert json.loads(logg)["rapport"] == "okonomi" and "Ola" not in logg


def test_lesetilgang_ser_rapporten_men_kan_ikke_laste_ned(con, fakturaer):
    db.opprett_admin_bruker(con, "leser", "Leser", "passord-som-holder", rolle="lese")
    con.commit()
    k = _leser()
    html = k.get("/admin/rapporter/okonomi?fra=2031-01-01").get_data(as_text=True)
    assert "Grunnkurs" in html and "okonomi.csv" not in html
    assert k.get("/admin/rapporter/okonomi.csv").status_code == 403


def test_personrapporten_viser_deltakerens_fakturaer(con, fakturaer):
    kari = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (fakturaer["kari"],)).fetchone()[0]
    html = _admin().get(f"/admin/rapporter/deltaker/{kari}").get_data(as_text=True)
    assert "Fakturaer (2)" in html and f"1{NBSP}234{NBSP}kr" in html and "Kreditert" in html


# ============================ fase 17: avslått ============================

def test_avslag_frigjoer_plassen_rykker_opp_venteliste_og_varsler(con, sendt):
    kid = _kurs(con, "AV", kapasitet=1, pris_nok=0)
    pid = _paamelding(con, kid, "anne@eksempel.no", "Anne Test")
    vpid = _paamelding(con, kid, "vera@eksempel.no", "Vera Test")
    assert con.execute("SELECT status FROM paamelding WHERE id=?", (vpid,)).fetchone()[0] == "venteliste"
    k = _admin()
    r = k.post(f"/admin/kurs/{kid}/deltaker/{pid}/avsla", data={"melding": "Opptakskravet er\n<b>ikke</b> oppfylt.",
                                                                "send_epost": "1"})
    side = k.get(r.headers["Location"]).get_data(as_text=True)
    rad = con.execute("SELECT status, avslatt_ts FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert rad["status"] == "avmeldt" and rad["avslatt_ts"]
    assert con.execute("SELECT status FROM paamelding WHERE id=?", (vpid,)).fetchone()[0] == "bekreftet"
    (til, emne, html), = [m for m in sendt if m[0] == "anne@eksempel.no"]
    assert emne == "Påmeldingen til Kurs AV er ikke godkjent"
    assert "Opptakskravet er<br>\n&lt;b&gt;ikke&lt;/b&gt; oppfylt." in html                  # escapet, linjeskift bevart
    assert any(m[0] == "vera@eksempel.no" for m in sendt)                                   # opprykket fikk bekreftelse
    assert "Påmeldingen er avslått, og deltakeren har fått beskjed på e-post." in side and "avslått" in side
    logg = con.execute("SELECT detaljer FROM hendelse WHERE handling='paamelding_avslatt'").fetchone()["detaljer"]
    assert json.loads(logg) == {"paamelding_id": pid, "fra": "bekreftet"}                 # begrunnelsen logges aldri


def test_avslag_uten_epost(con, sendt):
    kid = _kurs(con, "AE", pris_nok=0)
    pid = _paamelding(con, kid, "bo@eksempel.no")
    _admin().post(f"/admin/kurs/{kid}/deltaker/{pid}/avsla", data={"melding": ""})
    assert sendt == [] and con.execute("SELECT avslatt_ts FROM paamelding WHERE id=?", (pid,)).fetchone()[0]


def test_avmeldt_kan_ikke_avslaas(con, sendt):
    kid = _kurs(con, "AM", pris_nok=0)
    pid = _paamelding(con, kid, "cato@eksempel.no")
    db.meld_av(con, pid)
    con.commit()
    k = _admin()
    r = k.post(f"/admin/kurs/{kid}/deltaker/{pid}/avsla", data={"send_epost": "1"})
    assert "Bare påmeldte og de som står på venteliste kan avslås." in k.get(r.headers["Location"]).get_data(as_text=True)
    assert sendt == [] and con.execute("SELECT avslatt_ts FROM paamelding WHERE id=?", (pid,)).fetchone()[0] is None


def test_avslaatt_deltaker_kan_ikke_melde_seg_paa_igjen_selv(con, sendt):
    kid = _kurs(con, "AR", pris_nok=0)
    pid = _paamelding(con, kid, "dag@eksempel.no", "Dag Test")
    db.avsla_paamelding(con, pid, aktor="admin:test")
    con.commit()
    with pytest.raises(db.Paameldingsfeil, match="ikke godkjent"):
        db.meld_paa(con, kid, epost="dag@eksempel.no", fornavn="Dag", etternavn="Test")
    con.rollback()
    from kurs.web import app as webapp
    r = webapp.app.test_client().post(f"/kurs/AR", data={"fornavn": "Dag", "etternavn": "Test", "epost": "dag@eksempel.no",
                                                          "samtykke": "1", "samtykke_lagring": "1", **ADRESSE})
    assert r.status_code == 400 and "ikke godkjent" in r.get_data(as_text=True)


def test_admin_kan_gjenopprette_et_avslag(con, sendt):
    kid = _kurs(con, "AG", pris_nok=0)
    pid = _paamelding(con, kid, "eva@eksempel.no")
    db.avsla_paamelding(con, pid, aktor="admin:test")
    con.commit()
    _admin().post(f"/admin/kurs/{kid}/deltaker/{pid}/status", data={"status": "paameldt"})
    rad = con.execute("SELECT status, avslatt_ts FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert (rad["status"], rad["avslatt_ts"]) == ("bekreftet", None)


def test_avslaatt_vises_telles_og_kan_filtreres(con, sendt):
    kid = _kurs(con, "AL", pris_nok=0)
    avslatt = _paamelding(con, kid, "frida@eksempel.no", "Frida Avslått")
    avmeldt = _paamelding(con, kid, "geir@eksempel.no", "Geir Avmeldt")
    _paamelding(con, kid, "hilde@eksempel.no", "Hilde Bekreftet")
    db.avsla_paamelding(con, avslatt, aktor="admin:test")
    db.meld_av(con, avmeldt)
    con.commit()
    k = _admin()
    html = k.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    assert '<div class="liten dempet">Avslått</div><div class="stat">1</div>' in html
    assert '<div class="liten dempet">Avmeldt</div><div class="stat">1</div>' in html
    bare_avslatt = synlige_rader(k.get(f"/admin/kurs/{kid}/deltakere?status=avslatt").get_data(as_text=True))
    assert "Frida Avslått" in bare_avslatt and "Geir Avmeldt" not in bare_avslatt and "Hilde" not in bare_avslatt
    bare_avmeldt = synlige_rader(k.get(f"/admin/kurs/{kid}/deltakere?status=avmeldt").get_data(as_text=True))
    assert "Geir Avmeldt" in bare_avmeldt and "Frida" not in bare_avmeldt
    liste = k.get(f"/admin/kurs/{kid}/deltakerliste?valgt=1&kol=status&status=alle").get_data(as_text=True)
    assert "<td class=\"\">Avslått</td>" in liste and "<td class=\"\">Avmeldt</td>" in liste
    tekst = k.get(f"/admin/kurs/{kid}/deltakere.csv").get_data(as_text=True)
    assert "Frida;Avslått;frida@eksempel.no;;;Avslått" in tekst                  # fornavn og etternavn i hver sin kolonne


def test_lesetilgang_kan_ikke_avslaa(con, sendt):
    kid = _kurs(con, "AS", pris_nok=0)
    pid = _paamelding(con, kid, "ivar@eksempel.no")
    db.opprett_admin_bruker(con, "leser", "Leser", "passord-som-holder", rolle="lese")
    con.commit()
    k = _leser()
    assert "Avslå påmelding" not in k.get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
    assert k.post(f"/admin/kurs/{kid}/deltaker/{pid}/avsla", data={"send_epost": "1"}).status_code == 403
    assert con.execute("SELECT avslatt_ts FROM paamelding WHERE id=?", (pid,)).fetchone()[0] is None


def test_migrering_6_legger_til_kolonnen_uten_aa_roere_data(con):
    kid = _kurs(con, "M6", pris_nok=0)
    pid = _paamelding(con, kid, "jon@eksempel.no")
    for kolonne in ("forlatt_ts", "utgatt_ts"):       # migrering 8 (reglene viser til avslatt_ts) fantes ikke før 6
        con.execute(f"ALTER TABLE paamelding DROP COLUMN {kolonne}")
    con.execute("ALTER TABLE paamelding DROP COLUMN avslatt_ts")
    con.execute("DELETE FROM schema_versjon WHERE versjon >= 6")
    con.commit()
    assert migreringer.kjor_manglende(con)[0] == 6
    rad = con.execute("SELECT status, avslatt_ts FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert (rad["status"], rad["avslatt_ts"]) == ("bekreftet", None)
    assert migreringer.kjor_manglende(con) == []
