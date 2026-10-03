"""Kursholderoversikten (Camilla 02.10.2026: «Vi må ha en tydelig oversikt over kursholderne, slik det er i kommende kurs
fanen»): rollen per kursholder på hver samling og veiledningsdag, Psybase-status (rød = ikke lagt inn), dobbeltbooking og
redigering i ruta. Alle kurs og navn her er oppdiktet."""
import re
from datetime import date

import pytest
from werkzeug.datastructures import MultiDict

from kurs import config, db, kursholdere, planlagte_kurs, sjekklister


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _klient(brukernavn=None, passord=None, dag=None):
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": brukernavn or config.ADMIN_BRUKERNAVN,
                                    "passord": passord or config.ADMIN_PASSORD})
    if dag:
        with k.session_transaction() as s:
            s["demo_dato"] = dag.isoformat()
    return k


def _plan(con, navn, fra, til, **samling):
    s = planlagte_kurs.valider_samling(MultiDict({"fra_dato": fra.isoformat(), "til_dato": til.isoformat(),
                                                  **{k: str(v) for k, v in samling.items()}}))
    pid = planlagte_kurs.opprett(con, planlagte_kurs.valider_kurs(MultiDict({"navn": navn})), [s], "admin:test")
    sid = planlagte_kurs.hent(con, pid)["samlinger"][0]["id"]
    return pid, sid


def _person(con, navn, fullt=None):
    return kursholdere.opprett(con, kursholdere.valider(MultiDict({"navn": navn, "fullt_navn": fullt or ""})), "admin:test")


def test_rollen_settes_endres_og_fjernes_og_bare_gyldige_verdier_godtas(con):
    pid, sid = _plan(con, "Kurs A", date(2027, 1, 18), date(2027, 1, 21), sted="Oslo", veiledningsdager="18.01.2027")
    kid = _person(con, "Testa T.", "Testa Testesen")
    assert kursholdere.sett_rolle(con, sid, "", kid, "T1", False, "admin:test") == "lagt_til"
    assert kursholdere.sett_rolle(con, sid, "", kid, "T1", False, "admin:test") == "uendret"
    assert kursholdere.sett_rolle(con, sid, "", kid, "T1", True, "admin:test") == "endret"        # booket i Psybase
    assert kursholdere.sett_rolle(con, sid, "2027-01-18", kid, "v", False, "admin:test") == "lagt_til"
    rader = con.execute("SELECT dag, rolle, psybase FROM planlagt_rolle ORDER BY dag").fetchall()
    assert [tuple(r) for r in rader] == [("", "T1", 1), ("2027-01-18", "v", 0)]
    for feil_dag, feil_rolle in (("2027-01-19", "v"), ("", "X")):
        with pytest.raises(kursholdere.KursholderFeil):
            kursholdere.sett_rolle(con, sid, feil_dag, kid, feil_rolle, False, "admin:test")
    assert kursholdere.sett_rolle(con, sid, "", kid, "", False, "admin:test") == "fjernet"
    detaljer = " ".join(r[0] for r in con.execute("SELECT detaljer FROM hendelse WHERE handling='planlagt_rolle_endret'"))
    assert "Testa" not in detaljer                                                     # loggen har bare id-er
    with pytest.raises(kursholdere.KursholderFeil):
        _person(con, "testa t.")                                                       # samme navn (store/små bokstaver)


def test_oversikten_har_rader_for_samlinger_og_veiledningsdager_og_finner_dobbeltbooking(con):
    _, a = _plan(con, "Kurs A", date(2027, 1, 18), date(2027, 1, 21), sted="Oslo", veiledningsdager="18.01.2027")
    _, b = _plan(con, "Kurs B", date(2027, 1, 20), date(2027, 1, 22), sted="Bergen")
    _, c = _plan(con, "Kurs C", date(2027, 2, 1), date(2027, 2, 2), sted="Oslo")
    marit, anne = _person(con, "Marit X."), _person(con, "Anne Y.")
    kursholdere.sett_rolle(con, a, "", marit, "F", False, "admin:test")
    kursholdere.sett_rolle(con, b, "", marit, "T", True, "admin:test")                # overlapper A: dobbeltbooket
    kursholdere.sett_rolle(con, a, "2027-01-18", anne, "v", True, "admin:test")
    kursholdere.sett_rolle(con, a, "", anne, "T1", True, "admin:test")                 # egen veiledningsdag: ikke kollisjon
    kursholdere.sett_rolle(con, c, "", anne, "-", False, "admin:test")
    m = kursholdere.matrise(con, date(2027, 1, 5))
    assert [(r.kursnavn.split()[1], r.dag) for r in m["rader"]] == [("A", ""), ("A", "2027-01-18"), ("B", ""), ("C", "")]
    rad_a, rad_veil, rad_b, rad_c = m["rader"]
    assert rad_a.celler[marit].kollisjon and rad_b.celler[marit].kollisjon and m["kollisjoner"] == 2
    assert not rad_a.celler[anne].kollisjon and not rad_veil.celler[anne].kollisjon
    assert rad_c.celler[anne].rolle == "-" and m["dager"] == {marit: 5, anne: 4}       # ulike datoer; «-» teller ikke
    bare_anne = kursholdere.matrise(con, date(2027, 1, 5), kursholder_id=anne)
    assert [(r.kursnavn.split()[1], r.dag) for r in bare_anne["rader"]] == [("A", ""), ("A", "2027-01-18")]


def test_siden_viser_rollene_og_redigering_i_ruta_lagrer(con):
    pid, sid = _plan(con, "Kurs A", date(2027, 1, 18), date(2027, 1, 21), sted="Oslo")
    kid = _person(con, "Marit X.", "Marit Xeno")
    kursholdere.sett_rolle(con, sid, "", kid, "T1", False, "admin:test")
    con.commit()
    k = _klient(dag=date(2027, 1, 5))
    t = k.get("/admin/aktiviteter/kursholdere").get_data(as_text=True)
    assert f'<th scope="col" class="kh-navn" data-kh="{kid}" title="Marit Xeno">Marit X.</th>' in t
    assert re.search(rf'<tr id="rad-{sid}"[^>]*data-samling="{sid}" data-dag=""', t)
    assert re.search(r'<td class="kh-celle psy-0">\s*<button type="button" aria-label="Marit X.: T1">T1</button>', t)  # rød
    assert 'id="rolle-dialog"' in t and 'value="bv"' in t
    r = k.post("/admin/aktiviteter/kursholdere/rolle", data={"samling_id": sid, "dag": "", "kursholder_id": kid, "rolle": "T2",
                                                            "psybase": "1", "neste": "/admin/aktiviteter/kursholdere?kursholder=" + str(kid)})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/admin/aktiviteter/kursholdere?kursholder={kid}#rad-{sid}")
    assert tuple(con.execute("SELECT rolle, psybase FROM planlagt_rolle").fetchone()) == ("T2", 1)
    farlig = k.post("/admin/aktiviteter/kursholdere/rolle", data={"samling_id": sid, "kursholder_id": kid, "rolle": "B",
                                                                 "neste": "https://ond.example/"})
    assert farlig.headers["Location"].endswith(f"/admin/aktiviteter/kursholdere#rad-{sid}")
    kalender = k.get("/admin/aktiviteter/kalender").get_data(as_text=True)
    assert '<span class="oholdere">Kursholder: Marit X. (B)</span>' in kalender                      # teksten på kursraden


def test_kursholderne_legges_til_og_endres_og_lesetilgang_kan_bare_se(con):
    k = _klient()
    k.post("/admin/aktiviteter/kursholdere/ny", data={"navn": "Ny P.", "fullt_navn": "Ny Person"})
    (kid,) = [r[0] for r in con.execute("SELECT id FROM kursholder WHERE navn='Ny P.'")]
    k.post(f"/admin/aktiviteter/kursholdere/{kid}", data={"navn": "Ny P.", "fullt_navn": "Ny Person", "aktiv": ""})
    assert con.execute("SELECT aktiv FROM kursholder WHERE id=?", (kid,)).fetchone()[0] == 0
    dobbel = k.post("/admin/aktiviteter/kursholdere/ny", data={"navn": "ny p."}, follow_redirects=True)
    assert "Det finnes alt en kursholder som heter" in dobbel.get_data(as_text=True)
    db.opprett_admin_bruker(con, "leser", "Leser", "lese-passord-1", rolle="lese")
    con.commit()
    les = _klient("leser", "lese-passord-1")
    assert les.post("/admin/aktiviteter/kursholdere/ny", data={"navn": "X"}).status_code == 403
    t = les.get("/admin/aktiviteter/kursholdere").get_data(as_text=True)
    assert 'id="rolle-dialog"' not in t and "Legg til kursholder" not in t


def test_kurs_i_systemet_er_med_og_lenken_gaar_til_kurset(con):
    kid = db.opprett_kurs(con, kode="KHX", navn="Todagerskurs i Oslo", datoer=["2027-01-20", "2027-01-21"],
                          sharepoint_mappe="Kurs/KHX")
    sjekklister.opprett_mal(con, sjekklister.valider_mal(MultiDict({"navn": sjekklister.STANDARDMAL})), "admin:test")
    sjekklister.synk_kurs(con, date(2027, 1, 5))
    (sid,) = [r[0] for r in con.execute("SELECT ps.id FROM planlagt_samling ps JOIN planlagt_kurs pk "
                                        "ON pk.id=ps.planlagt_kurs_id WHERE pk.kurs_id=?", (kid,))]
    kursholdere.sett_rolle(con, sid, "", _person(con, "Marit X."), "T", True, "admin:test")
    con.commit()
    t = _klient(dag=date(2027, 1, 5)).get("/admin/aktiviteter/kursholdere").get_data(as_text=True)
    assert re.search(rf'<tr id="rad-{sid}".*?<a href="/admin/kurs/{kid}">Todagerskurs i Oslo</a>', t, re.S)
    kalender = _klient(dag=date(2027, 1, 5)).get("/admin/aktiviteter/kalender").get_data(as_text=True)
    assert '<span class="oholdere">Kursholder: Marit X. (T)</span>' in kalender                     # vises på kursets rad
