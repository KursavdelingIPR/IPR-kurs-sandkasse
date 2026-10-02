"""Årsplan / kurshjul: kurs automatisk, planlagte aktiviteter og notater, overlapp, ledige perioder, roller og migrering."""
import json
from datetime import date, timedelta

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


def _maaned(plan, maaned, idag=date(2000, 1, 1)):
    return aarsplan.aarskalender(plan, idag)[maaned - 1]


def _dag(plan, maaned, dag, idag=date(2000, 1, 1)):
    """Dagen i månedskalenderen (bare månedens egne datoer)"""
    return next(d for u in _maaned(plan, maaned, idag).uker for d in u.dager if d.i_maaneden and d.dato.day == dag)


def _blokker(plan, maaned):
    """{(type, tekst, første dag, siste dag)} for blokkene i måneden (en blokk deles ved ukeskifte)"""
    ut = set()
    for u in _maaned(plan, maaned).uker:
        mandag = u.dager[0].dato
        for b in (b for bane in u.baner for b in bane):
            ut.add((b.type, b.tekst, (mandag + timedelta(days=b.kolonne)).day,
                    (mandag + timedelta(days=b.kolonne + b.lengde - 1)).day))
    return ut


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
    assert _dag(plan, 3, 10).opptatt == 1 and _dag(plan, 3, 12).opptatt == 0
    assert _blokker(plan, 3) == {("kurs", "Grunnkurs", 10, 11)}                  # det avlyste kurset er ikke med
    (utkast,) = [b for u in _maaned(plan, 4).uker for bane in u.baner for b in bane]
    assert (utkast.tekst, utkast.status) == ("Utkastkurs", "utkast")


def test_aarskalender_viser_kurs_plan_notat_kollisjon_helg_og_bare_maanedens_dager(con):
    _kurs(con, "B1", "Kurs B", ["2031-05-06", "2031-05-07"])
    _plan(con, "Planlagt parkurs", "2031-05-07", "2031-05-08")
    _plan(con, "Husk søknadsfrist", "2031-05-20", type_="notat")
    plan = aarsplan.hent(con, AAR)
    assert (_dag(plan, 5, 6).opptatt, _dag(plan, 5, 6).klasser) == (1, "")
    assert (_dag(plan, 5, 7).opptatt, _dag(plan, 5, 7).klasser) == (2, "kollisjon")
    assert (_dag(plan, 5, 8).opptatt, _dag(plan, 5, 8).klasser) == (1, "")
    assert (_dag(plan, 5, 20).opptatt, _dag(plan, 5, 20).klasser) == (0, "notat")     # notater opptar ingenting
    assert _dag(plan, 5, 3).klasser == "helg"                               # lørdag 3. mai 2031
    assert _blokker(plan, 5) == {("kurs", "Kurs B", 6, 7), ("plan", "Planlagt: Planlagt parkurs", 7, 8)}
    februar = _maaned(plan, 2)
    assert [d.dato.day for u in februar.uker for d in u.dager if d.i_maaneden] == list(range(1, 29))
    assert {d.klasser for u in februar.uker for d in u.dager if not d.i_maaneden} == {"utenfor"}
    assert "Kurs B" in _dag(plan, 5, 7).tittel and "Planlagt: Planlagt parkurs" in _dag(plan, 5, 7).tittel
    assert _maaned(plan, 5).opptatt_tekst == "Opptatt: 6.–8. mai"             # notatet 20. mai opptar ingenting
    assert "idag" in _dag(plan, 5, 6, idag=date(2031, 5, 6)).klasser.split()


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


def test_maanedene_har_opptatte_dager_samlinger_og_planer_over_maanedsskifte(con):
    _kurs(con, "F1", "Samlingskurs", ["2031-10-06", "2031-10-07", "2031-11-03"])
    _plan(con, "Lang plan", "2031-10-30", "2031-11-02", sted="Oslo")
    plan = aarsplan.hent(con, AAR)
    okt, nov = _maaned(plan, 10), _maaned(plan, 11)
    assert okt.opptatt_tekst == "Opptatt: 6.–7. og 30.–31. okt" and nov.opptatt_tekst == "Opptatt: 1.–3. nov"
    assert _blokker(plan, 10) == {("kurs", "Samlingskurs 1/2", 6, 7), ("plan", "Planlagt: Lang plan", 30, 31)}
    assert _blokker(plan, 11) == {("kurs", "Samlingskurs 2/2", 3, 3), ("plan", "Planlagt: Lang plan", 1, 2)}
    (okt_plan,) = [b for u in okt.uker for bane in u.baner for b in bane if b.type == "plan"]
    (nov_plan,) = [b for u in nov.uker for bane in u.baner for b in bane if b.type == "plan"]
    assert (okt_plan.til_neste, nov_plan.fra_forrige) == (True, True)            # samme plan fortsetter
    assert "30. okt – 2. nov" in okt_plan.tittel and "Oslo" in okt_plan.tittel
    assert [p["periodetekst"] for p in aarsplan.planliste(plan)] == ["30. okt – 2. nov"]
    assert _maaned(plan, 1).opptatt_tekst == "Ingen kurs eller planer"


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
    assert "<h1>Årsplan 2031</h1>" in html and 'aria-current="page">Kalender</a>' in html
    assert 'class="aktiv" href="/admin/aktiviteter/aarsplan">Årsplan</a>' in html
    assert '<h2 id="mnd-3-tittel">Mars</h2>' in html and html.count('<section class="minimaaned"') == 12
    assert "<strong>10. mar</strong>: Grunnkurs i EFT og Parterapi</li>" in html        # kollisjonslisten
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
    con.execute("DELETE FROM schema_versjon WHERE versjon >= 3")             # databasen slik den var foer migrering 3
    con.commit()
    assert not db.har_tabell(con, "planlagt_aktivitet") and migreringer.gjeldende_versjon(con) == 2
    assert migreringer.kjor_manglende(con) == list(range(3, migreringer.KODEVERSJON + 1))
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
