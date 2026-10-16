"""Fanen Webinarer (Camilla 15.10.2026: «mellom "årsplan" og "planlagte kurs" legge inn en fane som heter "webinarer". Her skal du
lage samme oppsett som det er under "kalender" … og legge inn oppgavene slik at jeg kan krysse ut»). Migrering 37: planlagt_kurs.webinar.
Alle kurs og navn her er oppdiktet."""
from datetime import date, timedelta

import pytest
from werkzeug.datastructures import MultiDict

from kurs import config, db, kurskalender, migreringer, planlagte_kurs, sjekklister

FRAM = date.today() + timedelta(days=45)


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
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def _plan(con, navn, dag=FRAM, **kurs):
    verdier = planlagte_kurs.valider_kurs(MultiDict({"navn": navn, **{k: str(v) for k, v in kurs.items()}}))
    samling = planlagte_kurs.valider_samling(MultiDict({"fra_dato": dag.isoformat(), "til_dato": dag.isoformat(), "sted": "Online"}))
    pid = planlagte_kurs.opprett(con, verdier, [samling], "admin:test")
    con.commit()
    return pid


def _mal(con, navn="Webinar"):
    mal_id = sjekklister.opprett_mal(con, {"navn": navn, "beskrivelse": None}, "admin:test")
    sjekklister.opprett_malpunkt(con, mal_id, sjekklister.valider_malpunkt(MultiDict(
        {"tekst": "Opprette kurs i Zoom", "frist_antall": "1", "frist_enhet": "maaneder", "frist_retning": "for"})), "admin:test")
    con.commit()
    return mal_id


def test_migrering_37_legger_til_kolonnen_og_kan_kjores_paa_nytt(con):
    assert (37, "webinarer") in [(n, navn) for n, navn, _ in migreringer.MIGRERINGER]
    pid = _plan(con, "Gammelt kurs")
    assert con.execute("SELECT webinar FROM planlagt_kurs WHERE id=?", (pid,)).fetchone()[0] == 0
    migreringer._m37_webinarer(con)                      # idempotent
    assert db.har_kolonne(con, "planlagt_kurs", "webinar")


def test_bare_webinarene_i_fanen_og_alle_i_kalenderen(con):
    _plan(con, "Oppdiktet webinar om skam", webinar=1)
    _plan(con, "Oppdiktet todagerskurs")
    db.opprett_kurs(con, kode="W1", navn="Når barn strever (gratis webinar)", datoer=[FRAM.isoformat()], sharepoint_mappe="Kurs/W1",
                    pris_nok=0, type="digital", sted="Zoom", status="aapen")
    con.commit()
    k = _klient()
    fane = k.get("/admin/aktiviteter/webinarer").get_data(as_text=True)
    assert "<h1>Webinarer</h1>" in fane and "Oppdiktet webinar om skam" in fane and "Når barn strever (gratis webinar)" in fane
    assert "Oppdiktet todagerskurs" not in fane
    kalender = k.get("/admin/aktiviteter/kalender").get_data(as_text=True)
    assert all(n in kalender for n in ("Oppdiktet webinar om skam", "Oppdiktet todagerskurs", "Når barn strever"))
    # visningene og filtrene blir i fanen
    assert 'href="/admin/aktiviteter/webinarer?visning=uke' in fane
    assert "Oppdiktet webinar om skam" in k.get(f"/admin/aktiviteter/webinarer?aar={FRAM.year}&maned={FRAM.month}").get_data(as_text=True)


def test_fanen_ligger_mellom_aarsplan_og_planlagte_kurs(con):
    side = _klient().get("/admin/aktiviteter/webinarer").get_data(as_text=True)
    i_aar, i_web, i_plan = (side.index(f">{n}</a>") for n in ("Årsplan", "Webinarer", "Planlagte kurs"))
    assert i_aar < i_web < i_plan
    assert 'class="aktiv" href="/admin/aktiviteter/webinarer"' in side


def test_sjekklisten_kan_krysses_av_i_fanen(con):
    mal_id = _mal(con)
    pid = _plan(con, "Oppdiktet webinar", webinar=1)
    sjekklister.sett_mal(con, pid, mal_id, "admin:test")
    sjekklister.lag_for_kurs(con, pid, "admin:test", fra=date.today())
    con.commit()
    side = _klient().get("/admin/aktiviteter/webinarer").get_data(as_text=True)
    assert "data-sjekkrad" in side and "Opprette kurs i Zoom" in side


def test_nytt_webinar_er_avkrysset_med_malen_valgt(con):
    mal_id = _mal(con)
    k = _klient()
    side = k.get("/admin/aktiviteter/planlagte-kurs/ny?webinar=1").get_data(as_text=True)
    assert 'name="webinar" value="1" checked' in side and f'<option value="{mal_id}" selected' in side and 'value="Online"' in side
    assert 'name="webinar" value="1" checked' not in k.get("/admin/aktiviteter/planlagte-kurs/ny").get_data(as_text=True)
    r = k.post("/admin/aktiviteter/planlagte-kurs/ny", data={"navn": "Oppdiktet webinar", "webinar": "1", "sjekkliste_mal": str(mal_id),
                                                            "s-fra_dato": FRAM.isoformat(), "s-sted": "Online", "farge": "auto"})
    assert r.status_code == 302
    pid = con.execute("SELECT id FROM planlagt_kurs WHERE navn='Oppdiktet webinar'").fetchone()[0]
    assert planlagte_kurs.hent(con, pid)["webinar"] is True
    assert "Webinar</a>" in k.get(f"/admin/aktiviteter/planlagte-kurs/{pid}").get_data(as_text=True)
    # avkrysningen kan tas bort igjen
    k.post(f"/admin/aktiviteter/planlagte-kurs/{pid}", data={"navn": "Oppdiktet webinar", "farge": "auto"})
    assert planlagte_kurs.hent(con, pid)["webinar"] is False


def test_er_webinar_paa_navnet():
    assert kurskalender.er_webinar("Ja takk, begge deler (Gratis Webinar)") and not kurskalender.er_webinar("SKAM – dagskurs")
    assert not kurskalender.er_webinar(None)
