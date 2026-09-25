"""Årsplan / kurshjul: kurs automatisk, planlagte aktiviteter og notater, overlapp, ledige perioder, roller og migrering."""
import json
from datetime import date

import pytest
from werkzeug.datastructures import MultiDict

from kurs import aarsplan, config, db, migreringer

AAR = 2031          # et år uten demodata; 1. januar 2031 er en onsdag


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _admin(brukernavn=None, passord=None):
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    r = k.post("/admin/logg-inn", data={"brukernavn": brukernavn or config.ADMIN_BRUKERNAVN,
                                        "passord": passord or config.ADMIN_PASSORD})
    assert r.status_code == 302
    return k


def _kurs(con, kode, navn, datoer, **felter):
    kid = db.opprett_kurs(con, kode=kode, navn=navn, datoer=datoer, sharepoint_mappe=f"Kurs/{kode}", **felter)
    con.commit()
    return kid


def _plan(con, tittel, fra, til=None, type_="plan", sted=None, notat=None):
    verdier = aarsplan.valider(MultiDict({"type": type_, "tittel": tittel, "fra_dato": fra, "til_dato": til or "",
                                          "sted": sted or "", "notat": notat or ""}))
    pid = aarsplan.opprett(con, verdier, "admin:test")
    con.commit()
    return pid


def _celle(plan, maaned, dag, idag=date(2000, 1, 1)):
    return aarsplan.rutenett(plan, idag)[maaned - 1][1][dag - 1]


# ============================ validering ============================

def test_valider_gir_normaliserte_verdier_og_en_dag_som_standard():
    v = aarsplan.valider(MultiDict({"tittel": "  Nytt grunnkurs ", "fra_dato": "2031-03-10", "til_dato": ""}))
    assert v == {"type": "plan", "tittel": "Nytt grunnkurs", "fra_dato": "2031-03-10", "til_dato": "2031-03-10",
                 "sted": None, "notat": None}


@pytest.mark.parametrize("felt,melding", [
    ({"tittel": ""}, "Tittel må fylles ut"),
    ({"tittel": "x" * 121}, "høyst 120 tegn"),
    ({"tittel": "to\nlinjer"}, "ugyldige tegn"),
    ({"tittel": "nul\x00"}, "ugyldige tegn"),
    ({"fra_dato": "10.03.2031"}, "må være en dato"),
    ({"fra_dato": "2031-02-30"}, "ikke en gyldig dato"),
    ({"fra_dato": "1999-12-31"}, "mellom år 2000 og 2100"),
    ({"til_dato": "2031-03-09"}, "kan ikke være før"),
    ({"til_dato": "2032-03-10"}, "høyst ett år"),
    ({"type": "ferie"}, "Ukjent type"),
    ({"sted": "s" * 81}, "høyst 80 tegn"),
    ({"notat": "n" * 1001}, "høyst 1000 tegn"),
])
def test_valider_avviser_ugyldig_input(felt, melding):
    skjema = {"tittel": "Kurs", "fra_dato": "2031-03-10", **felt}
    with pytest.raises(aarsplan.AarsplanFeil, match=melding):
        aarsplan.valider(MultiDict(skjema))


def test_notat_kan_ha_flere_linjer():
    assert aarsplan.valider(MultiDict({"tittel": "T", "fra_dato": "2031-01-05", "notat": "a\nb"}))["notat"] == "a\nb"


# ============================ modell ============================

def test_kurs_kommer_med_automatisk_men_ikke_avlyste(con):
    _kurs(con, "A1", "Grunnkurs", ["2031-03-10", "2031-03-11"])
    _kurs(con, "A2", "Avlyst kurs", ["2031-03-12"], status="avlyst")
    _kurs(con, "A3", "Utkastkurs", ["2031-04-01"], status="utkast")
    plan = aarsplan.hent(con, AAR)
    assert sorted(k["navn"] for k in plan.kurs) == ["Grunnkurs", "Utkastkurs"]
    assert _celle(plan, 3, 10).tekst == "K" and _celle(plan, 3, 12).tekst == ""


def test_rutenett_viser_plan_notat_overlapp_helg_og_dager_som_ikke_finnes(con):
    _kurs(con, "B1", "Kurs B", ["2031-05-06", "2031-05-07"])
    _plan(con, "Planlagt parkurs", "2031-05-07", "2031-05-08")
    _plan(con, "Husk søknadsfrist", "2031-05-20", type_="notat")
    plan = aarsplan.hent(con, AAR)
    assert (_celle(plan, 5, 6).tekst, _celle(plan, 5, 6).klasse) == ("K", "kurs")
    assert (_celle(plan, 5, 7).tekst, _celle(plan, 5, 7).klasse) == ("2", "overlapp")
    assert (_celle(plan, 5, 8).tekst, _celle(plan, 5, 8).klasse) == ("P", "plan")
    assert (_celle(plan, 5, 20).tekst, _celle(plan, 5, 20).klasse) == ("N", "notat")
    assert _celle(plan, 5, 3).klasse == "helg"                            # lørdag 3. mai 2031
    assert _celle(plan, 2, 30).klasse == "utenfor" and _celle(plan, 2, 30).tekst == ""
    assert "Kurs B" in _celle(plan, 5, 7).tittel and "Planlagt: Planlagt parkurs" in _celle(plan, 5, 7).tittel
    assert "idag" in aarsplan.rutenett(plan, date(2031, 5, 6))[4][1][5].klasse


def test_overlapp_teller_kurs_og_planer_men_ikke_notater(con):
    _kurs(con, "C1", "Kurs en", ["2031-09-01"])
    _kurs(con, "C2", "Kurs to", ["2031-09-01"])
    _kurs(con, "C3", "Kurs tre", ["2031-09-02"])
    _plan(con, "Notat samme dag", "2031-09-02", type_="notat")
    ut = aarsplan.overlapp(aarsplan.hent(con, AAR))
    assert [(d, [a.tittel for a in akt]) for d, akt in ut] == [("1. sep", ["Kurs en", "Kurs to"])]


def test_ledige_perioder_er_sammenhengende_uker_uten_kurs_eller_planer(con):
    _kurs(con, "D1", "Kurs uke 2", ["2031-01-08"])                         # onsdag i ISO-uke 2
    _plan(con, "Planlagt uke 5-6", "2031-01-31", "2031-02-03")               # fredag uke 5 til mandag uke 6
    _plan(con, "Notat uke 9", "2031-02-26", type_="notat")                   # notater opptar ikke
    perioder = aarsplan.ledige_perioder(aarsplan.hent(con, AAR))
    assert [(p.fra_uke, p.til_uke) for p in perioder] == [(1, 1), (3, 4), (7, aarsplan.antall_uker(AAR))]
    assert perioder[1].fra == date(2031, 1, 13) and perioder[1].til == date(2031, 1, 26)
    assert perioder[1].antall_uker == 2 and perioder[1].tekst == "13.–26. jan"


def test_kantuker_regnes_med_kursdager_i_naboaaret(con):
    # ISO-uke 1 i 2032 starter mandag 29.12.2031. Et kurs 30.12.2031 gjør uke 1 i 2032 opptatt.
    _kurs(con, "E1", "Romjulskurs", ["2031-12-30"])
    perioder = aarsplan.ledige_perioder(aarsplan.hent(con, 2032))
    assert perioder[0].fra_uke == 2
    assert aarsplan.hent(con, 2032).kurs == []                              # men kurset listes bare i 2031


def test_per_maaned_har_datotekster_og_planer_over_maanedsskifte(con):
    _kurs(con, "F1", "Samlingskurs", ["2031-10-06", "2031-10-07", "2031-11-03"])
    _plan(con, "Lang plan", "2031-10-30", "2031-11-02", sted="Oslo")
    m = aarsplan.per_maaned(aarsplan.hent(con, AAR))
    okt, nov = m[9], m[10]
    assert okt["kurs"][0]["datotekst"] == "6. og 7. okt" and nov["kurs"][0]["datotekst"] == "3. nov"
    assert okt["planer"][0]["periodetekst"] == "30. okt – 2. nov" and nov["planer"][0]["tittel"] == "Lang plan"
    assert m[0]["kurs"] == [] and m[0]["planer"] == []


def test_opprett_og_slett_logger_uten_fritekst(con):
    pid = _plan(con, "Hemmelig-tittel", "2031-06-01", notat="Hemmelig-notat")
    aarsplan.slett(con, pid, "admin:test")
    con.commit()
    rader = con.execute("SELECT handling, detaljer FROM hendelse WHERE handling LIKE 'planlagt_aktivitet_%' "
                        "ORDER BY id").fetchall()
    assert [r["handling"] for r in rader] == ["planlagt_aktivitet_opprettet", "planlagt_aktivitet_slettet"]
    assert all("Hemmelig" not in r["detaljer"] for r in rader)
    assert json.loads(rader[0]["detaljer"]) == {"id": pid, "type": "plan", "fra_dato": "2031-06-01",
                                                "til_dato": "2031-06-01"}
    assert aarsplan.slett(con, pid, "admin:test") is None


# ============================ sider ============================

def test_siden_viser_aaret_kurs_overlapp_og_ledige_perioder(con):
    _kurs(con, "G1", "Grunnkurs i EFT", ["2031-03-10"])
    _kurs(con, "G2", "Parterapi", ["2031-03-10"])
    html = _admin().get(f"/admin/aktiviteter/aarsplan?aar={AAR}").get_data(as_text=True)
    assert "<h1>Årsplan 2031</h1>" in html and 'aria-current="page">Aktiviteter</a>' in html
    assert 'class="aktiv" href="/admin/aktiviteter/aarsplan">Årsplan</a>' in html
    assert '<th scope="row" class="mnd">Mars</th>' in html
    assert "<strong>10. mar</strong>: " in html                               # overlapp-listen
    assert "Grunnkurs i EFT" in html and "Ledige perioder" in html
    assert "2 kurs · 2 kursdager · 0 planlagte aktiviteter" in html


@pytest.mark.parametrize("aar", ["1999", "abc", "2101"])
def test_ugyldig_aar_gir_inneverende_aar(con, aar):
    html = _admin().get(f"/admin/aktiviteter/aarsplan?aar={aar}").get_data(as_text=True)
    assert f"<h1>Årsplan {date.today().year}</h1>" in html


def test_legg_inn_og_fjern_planlagt_aktivitet(con):
    k = _admin()
    r = k.post("/admin/aktiviteter/aarsplan/ny", data={"type": "plan", "tittel": "Nytt grunnkurs", "aar": AAR,
                                                        "fra_dato": "2031-08-18", "til_dato": "2031-08-19",
                                                        "sted": "Bergen", "notat": "Venter på kursholder"})
    assert r.status_code == 302 and r.headers["Location"] == f"/admin/aktiviteter/aarsplan?aar={AAR}"
    html = k.get(r.headers["Location"]).get_data(as_text=True)
    assert "Nytt grunnkurs" in html and "18.–19. aug" in html and "Venter på kursholder" in html
    rad = con.execute("SELECT id, opprettet_av FROM planlagt_aktivitet").fetchone()
    assert rad["opprettet_av"] == f"admin:{config.ADMIN_BRUKERNAVN}"
    r = k.post(f"/admin/aktiviteter/aarsplan/{rad['id']}/slett")
    assert r.status_code == 302 and con.execute("SELECT COUNT(*) FROM planlagt_aktivitet").fetchone()[0] == 0
    assert k.post(f"/admin/aktiviteter/aarsplan/{rad['id']}/slett").status_code == 404


def test_ugyldig_skjema_gir_400_og_beholder_det_som_ble_skrevet(con):
    r = _admin().post("/admin/aktiviteter/aarsplan/ny", data={"tittel": "Beholdes", "aar": AAR,
                                                              "fra_dato": "2031-08-19", "til_dato": "2031-08-18"})
    html = r.get_data(as_text=True)
    assert r.status_code == 400 and "Til-dato kan ikke være før fra-dato." in html
    assert 'value="Beholdes"' in html and "<h1>Årsplan 2031</h1>" in html
    assert con.execute("SELECT COUNT(*) FROM planlagt_aktivitet").fetchone()[0] == 0


def test_tittel_og_notat_escapes(con):
    _plan(con, "<script>alert(1)</script>", "2031-02-03", notat="<b>fet</b>")
    html = _admin().get(f"/admin/aktiviteter/aarsplan?aar={AAR}").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<b>fet</b>" not in html


def test_lesetilgang_ser_aarsplanen_men_kan_ikke_endre(con):
    pid = _plan(con, "Plan", "2031-02-03")
    db.opprett_admin_bruker(con, "leser", "Leser", "passord-som-holder", rolle="lese")
    con.commit()
    k = _admin("leser", "passord-som-holder")
    html = k.get(f"/admin/aktiviteter/aarsplan?aar={AAR}").get_data(as_text=True)
    assert "Plan" in html and "Legg inn planlagt aktivitet" not in html and "/slett" not in html
    assert k.post("/admin/aktiviteter/aarsplan/ny", data={"tittel": "X", "fra_dato": "2031-02-03"}).status_code == 403
    assert k.post(f"/admin/aktiviteter/aarsplan/{pid}/slett").status_code == 403
    assert con.execute("SELECT COUNT(*) FROM planlagt_aktivitet").fetchone()[0] == 1


def test_krever_innlogging(con):
    from kurs.web import app as webapp
    anonym = webapp.app.test_client()
    assert anonym.get("/admin/aktiviteter/aarsplan").status_code == 302
    assert anonym.post("/admin/aktiviteter/aarsplan/ny", data={"tittel": "X", "fra_dato": "2031-02-03"}).status_code in (302, 400)
    assert con.execute("SELECT COUNT(*) FROM planlagt_aktivitet").fetchone()[0] == 0


# ============================ migrering ============================

def test_migrering_3_lager_tabellen_i_en_database_uten_den(con):
    con.execute("DROP TABLE planlagt_aktivitet")
    con.execute("DELETE FROM schema_versjon WHERE versjon=3")
    con.commit()
    assert not db.har_tabell(con, "planlagt_aktivitet") and migreringer.gjeldende_versjon(con) == 2
    assert migreringer.kjor_manglende(con) == [3]
    assert db.kolonner(con, "planlagt_aktivitet") == ["id", "type", "tittel", "fra_dato", "til_dato", "sted", "notat",
                                                      "opprettet_av", "opprettet"]
    assert migreringer.kjor_manglende(con) == []                             # idempotent
    _plan(con, "Etter migrering", "2031-01-06")
    assert con.execute("SELECT COUNT(*) FROM planlagt_aktivitet").fetchone()[0] == 1


def test_periodetekst():
    assert aarsplan.periode_tekst(date(2031, 10, 15), date(2031, 10, 15)) == "15. okt"
    assert aarsplan.periode_tekst(date(2031, 10, 15), date(2031, 10, 17)) == "15.–17. okt"
    assert aarsplan.periode_tekst(date(2031, 10, 28), date(2031, 11, 3)) == "28. okt – 3. nov"
    assert aarsplan.periode_tekst(date(2030, 12, 30), date(2031, 1, 5)) == "30. des 2030 – 5. jan 2031"


def test_skjemaet_er_lukket_til_det_trengs_og_aapent_ved_feil(con):
    k = _admin()
    assert '<details id="ny" class="kort ikke-utskrift">' in k.get("/admin/aktiviteter/aarsplan").get_data(as_text=True)
    r = k.post("/admin/aktiviteter/aarsplan/ny", data={"tittel": "", "fra_dato": "2031-01-06", "aar": AAR})
    assert r.status_code == 400 and '<details id="ny" class="kort ikke-utskrift" open>' in r.get_data(as_text=True)
