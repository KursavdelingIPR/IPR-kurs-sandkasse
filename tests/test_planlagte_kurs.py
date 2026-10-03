"""Planlagte kurs (bestillingen 02.10.2026): kursene fra kursplanen («Kommende kurs» i Kurskalender-Excelen) vises i Kalender
og Årsplan med én lagret farge per kurs (alle samlingene har samme farge, kurs nær hverandre i tid får ulike farger), men de
står ikke på Oversikten og har ingen påmelding, e-post eller faktura. Alle kurs og navn her er oppdiktet."""
import json
from datetime import date, timedelta

import pytest
from werkzeug.datastructures import MultiDict

from kurs import aarsplan, config, db, kursfarger, kurskalender, migreringer, planlagte_kurs

FRAM = date.today() + timedelta(days=45)          # kursoversikten viser denne måneden og framover


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _klient(brukernavn=None, passord=None):
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": brukernavn or config.ADMIN_BRUKERNAVN,
                                    "passord": passord or config.ADMIN_PASSORD})
    return k


def _samling(fra, til=None, **felt):
    return planlagte_kurs.valider_samling(MultiDict({"fra_dato": fra.isoformat(), "til_dato": (til or fra).isoformat(),
                                                     **{k: str(v) for k, v in felt.items()}}))


def _plan(con, navn, *samlinger, **kurs):
    verdier = planlagte_kurs.valider_kurs(MultiDict({"navn": navn, **{k: str(v) for k, v in kurs.items()}}))
    pid = planlagte_kurs.opprett(con, verdier, list(samlinger), "admin:test")
    con.commit()
    return pid


def _farge(con, pid):
    return con.execute("SELECT farge FROM planlagt_kurs WHERE id=?", (pid,)).fetchone()[0]


# ============================ migrering 20 ============================

def test_migrering_20_lager_tabellene_og_kan_kjores_paa_ny(con):
    assert (20, "planlagte_kurs") in [(n, navn) for n, navn, _ in migreringer.MIGRERINGER]
    assert db.har_tabell(con, "planlagt_kurs") and db.har_tabell(con, "planlagt_samling")
    con.execute("DROP TABLE planlagt_samling")
    con.execute("DROP TABLE planlagt_kurs")
    con.execute("DELETE FROM schema_versjon WHERE versjon >= 20")
    con.commit()
    assert migreringer.kjor_manglende(con) == [n for n, _, _ in migreringer.MIGRERINGER if n >= 20]
    assert db.har_tabell(con, "planlagt_kurs") and db.har_tabell(con, "planlagt_samling")
    indekser = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"planlagt_samling_kurs", "planlagt_samling_dato"} <= indekser
    assert migreringer.kjor_manglende(con) == []                                  # ingenting igjen å kjøre


def test_migrering_20_star_i_migrations_md():
    tekst = (db._SCHEMA.parent.parent / "MIGRATIONS.md").read_text(encoding="utf-8")
    assert "planlagte_kurs" in tekst and "planlagt_samling" in tekst


# ============================ validering ============================

@pytest.mark.parametrize("skjema, feil", [
    ({"navn": ""}, "Navn må fylles ut."),
    ({"navn": "Kurs\x07"}, "Navn inneholder ugyldige tegn."),
    ({"navn": "To\nlinjer"}, "Navn inneholder ugyldige tegn."),
    ({"navn": "Kurs", "status": "ukjent"}, "Ukjent status."),
    ({"navn": "Kurs", "farge": "10"}, "Ugyldig farge."),
    ({"navn": "Kurs", "farge": "-1"}, "Ugyldig farge."),
    ({"navn": "Kurs", "antall_samlinger": "0"}, "Antall samlinger må være et tall fra 1 til 20."),
    ({"navn": "K" * 121}, "Navn kan være høyst 120 tegn."),
])
def test_kursets_felt_valideres(skjema, feil):
    with pytest.raises(planlagte_kurs.PlanFeil) as e:
        planlagte_kurs.valider_kurs(MultiDict(skjema))
    assert str(e.value) == feil


def test_kursets_felt_leses_riktig():
    v = planlagte_kurs.valider_kurs(MultiDict({"navn": " Modul 2 (kull 99) ", "prosjektnr": "999", "arrangor": "",
                                               "ekstern": "1", "farge": "3", "antall_samlinger": "3",
                                               "notat": "Linje 1\nLinje 2", "sjekk": "  "}))
    assert v == {"prosjektnr": "999", "navn": "Modul 2 (kull 99)", "arrangor": "IPR", "ekstern": 1, "status": "planlagt",
                 "farge": 3, "antall_samlinger": 3, "notat": "Linje 1\nLinje 2", "sjekk": None}
    assert planlagte_kurs.valider_kurs(MultiDict({"navn": "X", "farge": "auto"}))["farge"] is None
    assert planlagte_kurs.valider_kurs(MultiDict({"navn": "X"}))["ekstern"] == 0


@pytest.mark.parametrize("skjema, feil", [
    ({}, "Fra-dato må være en dato (ÅÅÅÅ-MM-DD eller DD.MM.ÅÅÅÅ)."),
    ({"fra_dato": "2031-02-30"}, "Fra-dato er ikke en gyldig dato."),
    ({"fra_dato": "2031-03-05", "til_dato": "2031-03-04"}, "Til-dato kan ikke være før fra-dato."),
    ({"fra_dato": "2031-03-01", "til_dato": "2031-04-15"}, "En samling kan vare høyst 31 dager."),
    ({"fra_dato": "2031-03-02", "til_dato": "2031-03-04", "veiledningsdager": "01.03.2031"},
     "Veiledningsdagen 01.03.2031 ligger utenfor samlingen."),
    ({"fra_dato": "2031-03-02", "nr": "21"}, "Samlingens nummer må være et tall fra 1 til 20."),
])
def test_samlingens_felt_valideres(skjema, feil):
    with pytest.raises(planlagte_kurs.PlanFeil) as e:
        planlagte_kurs.valider_samling(MultiDict(skjema))
    assert str(e.value) == feil


def test_samling_godtar_norske_datoer_og_sorterer_veiledningsdagene():
    s = planlagte_kurs.valider_samling(MultiDict({"fra_dato": "18.01.2031", "til_dato": "21.01.2031", "nr": "2",
                                                  "veiledningsdager": "19.01.2031; 2031-01-18 18.01.2031"}))
    assert (s["fra_dato"], s["til_dato"], s["nr"], s["veiledningsdager"]) == ("2031-01-18", "2031-01-21", 2,
                                                                              "2031-01-18,2031-01-19")
    en_dag = planlagte_kurs.valider_samling(MultiDict({"fra_dato": "2031-05-05"}))
    assert en_dag["til_dato"] == "2031-05-05" and en_dag["veiledningsdager"] is None


# ============================ farge ============================

def test_velg_er_forutsigbar_og_unngaar_kurs_naer_i_tid():
    jan = (date(2031, 1, 6), date(2031, 1, 9))
    assert kursfarger.velg([jan], []) == 0
    assert kursfarger.velg([jan], [(0, [jan])]) == 1                          # 0 er tatt samme uke
    assert kursfarger.velg([jan], [(0, [jan]), (1, [(date(2031, 1, 20), date(2031, 1, 21))])]) == 2
    langt_unna = [(0, [(date(2031, 9, 1), date(2031, 9, 2))])]
    nr = kursfarger.velg([jan], langt_unna)
    assert nr != 0 and kursfarger.velg([jan], langt_unna) == nr               # samme data gir samme farge
    alle_tatt = [(f, [jan]) for f in range(kursfarger.ANTALL)] + [(3, [(date(2031, 2, 3), date(2031, 2, 4))])]
    assert kursfarger.velg([jan], alle_tatt) != 3                             # likt i uken: den som er minst brukt


def test_samme_kurs_samme_farge_og_kurs_naer_hverandre_ulike_farger(con):
    a = _plan(con, "Utdanning A", _samling(date(2031, 1, 6), date(2031, 1, 9), nr=1),
              _samling(date(2031, 3, 3), date(2031, 3, 6), nr=2), _samling(date(2031, 5, 5), date(2031, 5, 8), nr=3))
    b = _plan(con, "Kurs B", _samling(date(2031, 1, 8), date(2031, 1, 9)))
    c = _plan(con, "Kurs C", _samling(date(2031, 3, 4)))
    assert len({_farge(con, a), _farge(con, b), _farge(con, c)}) == 3
    hendelser = kurskalender.hent(con, date(2031, 1, 1), date(2031, 12, 31), med_planlagte=True)
    assert {h.farge for h in hendelser if h.plan_id == a} == {_farge(con, a)}
    assert len([h for h in hendelser if h.plan_id == a]) == 3


def test_fargen_tar_hensyn_til_kurs_i_systemet(con):
    kid = db.opprett_kurs(con, kode="HASH-1", navn="Hashkurs i systemet", datoer=["2031-02-10", "2031-02-11"],
                          sharepoint_mappe="Kurs/HASH-1")
    con.commit()
    pid = _plan(con, "Planlagt samme uke", _samling(date(2031, 2, 10), date(2031, 2, 12)))
    assert _farge(con, pid) != kursfarger.farge("Hashkurs i systemet") and kid


def test_fargen_kan_velges_for_haand_og_paa_nytt_automatisk(con):
    pid = _plan(con, "Fargekurs", _samling(date(2031, 4, 7)), farge="7")
    assert _farge(con, pid) == 7
    planlagte_kurs.oppdater_kurs(con, pid, planlagte_kurs.valider_kurs(MultiDict({"navn": "Fargekurs", "farge": "auto"})),
                                 "admin:test")
    assert _farge(con, pid) == planlagte_kurs.velg_farge(con, [(date(2031, 4, 7), date(2031, 4, 7))], unntatt=pid)


# ============================ samlinger og nummer ============================

def test_samlingsnummeret_fra_kursplanen_beholdes(con):
    """Et kurs som er lagt inn fra 2. samling (1. samling var før) skal vise «Samling 2 av 3», ikke «1 av 2»."""
    _plan(con, "EFST testkull", _samling(date(2031, 1, 13), date(2031, 1, 16), nr=2, veiledningsdager="2031-01-13"),
          _samling(date(2031, 5, 19), date(2031, 5, 22), nr=3), antall_samlinger=3)
    _plan(con, "Uten nummer", _samling(date(2031, 2, 3)), _samling(date(2031, 2, 10)))
    _plan(con, "Én dato", _samling(date(2031, 2, 17)))
    h = {(x.navn, x.fra): x for x in kurskalender.hent(con, date(2031, 1, 1), date(2031, 12, 31), med_planlagte=True)}
    assert h[("EFST testkull", date(2031, 1, 13))].samling_tekst == "Samling 2 av 3"
    assert h[("EFST testkull", date(2031, 1, 13))].veiledning_tekst == "veiledningsdag 13. jan"
    assert h[("EFST testkull", date(2031, 5, 19))].samling_tekst == "Samling 3 av 3"
    assert [h[("Uten nummer", d)].samling_tekst for d in (date(2031, 2, 3), date(2031, 2, 10))] == ["Samling 1 av 2",
                                                                                                    "Samling 2 av 2"]
    assert h[("Én dato", date(2031, 2, 17))].samling_tekst == ""


def test_siste_samling_kan_ikke_slettes_men_kurset_kan(con):
    pid = _plan(con, "Slettekurs", _samling(date(2031, 6, 2)), _samling(date(2031, 6, 9)))
    s1, s2 = [s["id"] for s in planlagte_kurs.hent(con, pid)["samlinger"]]
    assert planlagte_kurs.slett_samling(con, pid, s1, "admin:test")["id"] == s1
    with pytest.raises(planlagte_kurs.PlanFeil):
        planlagte_kurs.slett_samling(con, pid, s2, "admin:test")
    assert planlagte_kurs.slett_samling(con, pid + 1, s2, "admin:test") is None            # feil kurs: ingenting
    assert planlagte_kurs.slett(con, pid, "admin:test")["navn"] == "Slettekurs"
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM planlagt_samling").fetchone()[0] == 0


def test_hendelsesloggen_faar_ingen_fritekst(con):
    pid = _plan(con, "Hemmelig kursnavn", _samling(date(2031, 6, 2), kursholdere="Navn Navnesen (T1)",
                                                  notat="Privat notat"), notat="Kursnotat", prosjektnr="12345")
    planlagte_kurs.slett(con, pid, "admin:test")
    con.commit()
    rader = con.execute("SELECT handling, detaljer FROM hendelse WHERE handling LIKE 'planlagt_kurs_%'").fetchall()
    assert [r["handling"] for r in rader] == ["planlagt_kurs_opprettet", "planlagt_kurs_slettet"]
    alt = " ".join(r["detaljer"] for r in rader)
    for fritekst in ("Hemmelig", "Navnesen", "Privat", "Kursnotat", "12345"):
        assert fritekst not in alt
    assert json.loads(rader[0]["detaljer"]) == {"id": pid, "samlinger": 1, "fra_dato": "2031-06-02",
                                                "til_dato": "2031-06-02"}


# ============================ Kalender ============================

def test_kalenderen_tar_med_planlagte_bare_naar_den_ber_om_det(con):
    pid = _plan(con, "Kalenderkurs", _samling(date(2031, 3, 3), date(2031, 3, 5), sted="Bergen", lokale="N58",
                                              kursholdere="Kursholder K (T)"), prosjektnr="901")
    _plan(con, "Online-kurs", _samling(date(2031, 3, 4), sted="Online"))
    _plan(con, "Avlyst kurs", _samling(date(2031, 3, 6)), status="avlyst")
    fra, til = date(2031, 3, 1), date(2031, 3, 31)
    assert kurskalender.hent(con, fra, til) == []                              # andre sider får dem ikke
    h = next(x for x in kurskalender.hent(con, fra, til, med_planlagte=True) if x.plan_id == pid)
    assert (h.navn, h.planlagt, h.paameldte_tekst, h.klasser, h.sted_visning) == (
        "901 Kalenderkurs Bergen", True, "", "planlagt", "Bergen · N58")          # byen i navnet (Camilla 02.10)
    assert "Planlagt kurs (ikke opprettet i systemet)" in h.beskrivelse and "Kursholder K (T)" in h.beskrivelse
    assert h.farge == _farge(con, pid)

    def navn(**filtre):
        return sorted(x.navn for x in kurskalender.hent(con, fra, til, med_planlagte=True, **filtre))
    assert navn(status="planlagt") == ["901 Kalenderkurs Bergen", "Online-kurs"]          # online: ingen by i navnet
    assert navn(status="avlyst") == ["Avlyst kurs"]
    assert navn(status="aapen") == [] and navn(ansvarlig=1) == []
    assert navn(sted="bergen") == ["901 Kalenderkurs Bergen"] and navn(sted="online") == ["Online-kurs"]
    avlyst = next(x for x in kurskalender.hent(con, fra, til, med_planlagte=True) if x.navn == "Avlyst kurs")
    assert avlyst.klasser == "avlyst planlagt"


def test_byen_staar_i_navnet():
    assert planlagte_kurs.visningsnavn("673", "EFST 1-årig", ["Oslo", "Oslo"]) == "673 EFST 1-årig Oslo"
    assert planlagte_kurs.visningsnavn("666", "EFT påbygging", ["Bergen", "Oslo", "Bergen"]) == "666 EFT påbygging Bergen/Oslo"
    assert planlagte_kurs.visningsnavn(None, "Kurs i Bergen", ["Bergen"]) == "Kurs i Bergen"      # står alt i navnet
    assert planlagte_kurs.visningsnavn("1", "Webinar", [None]) == "1 Webinar"
    assert planlagte_kurs.samlingens_by("Online", "Chr. Mich") is None                       # sendes bare derfra
    assert (planlagte_kurs.samlingens_by(None, "Paleet"), planlagte_kurs.samlingens_by("Bergen ", None)) == ("Oslo", "Bergen")
    assert planlagte_kurs.by_i("IPR, Bergen") == "bergen" and planlagte_kurs.by_i("Hybrid · Zoom") is None


# ============================ annen arrangør: dobbeltbooking bare fysisk på samme sted ============================

def test_annen_arrangor_kolliderer_bare_fysisk_paa_samme_sted():
    k = planlagte_kurs.kolliderer
    assert k(False, False, None, False, False, None)                 # egne kurs: to online-kurs samtidig
    assert not k(False, True, "bergen", False, False, None)          # fysisk + online: greit (03.10)
    assert not k(False, True, "bergen", False, True, "bergen", b_online=True)    # fysisk + hybrid: greit
    assert not k(False, True, "oslo", False, True, "bergen")         # Oslo + Bergen: greit (03.10)
    assert k(False, True, "bergen", False, True, "bergen") and k(False, True, None, False, True, "oslo")  # samme/ukjent by
    assert k(True, True, "oslo", False, True, "oslo")                # IPRO og vi fysisk i Oslo samtidig
    assert not k(True, True, "oslo", False, False, None)             # vårt kurs er online
    assert not k(True, True, "oslo", False, True, "bergen")          # vi er i Bergen
    assert not k(True, False, None, False, True, "oslo")             # IPRO online
    assert not k(True, True, "oslo", True, True, "oslo")             # to kurs med annen arrangør: ikke vår sak
    assert not k(True, True, None, False, True, None)                # ukjent sted: ingen dobbeltbooking


def test_kalenderen_sier_samme_dag_bare_ved_ekte_dobbeltbooking(con):
    _plan(con, "IPRO-kurs", _samling(FRAM, sted="Oslo", lokale="Paleet"), arrangor="IPRO", ekstern="1")
    _plan(con, "Vårt Oslo-kurs", _samling(FRAM, sted="Oslo", lokale="Paleet"))
    _plan(con, "Vårt webinar", _samling(FRAM, sted="Online"))
    _plan(con, "Vårt Bergen-kurs", _samling(FRAM, sted="Bergen", lokale="N58"))
    rader = {r.h.navn: r for r in kurskalender.rader(
        kurskalender.hent(con, FRAM, FRAM, med_planlagte=True), date.today())}
    assert [n for n, _ in rader["IPRO-kurs Oslo"].samme_dag] == ["Vårt Oslo-kurs"]           # byen står alt i navnet
    assert "IPRO-kurs Oslo" in [n for n, _ in rader["Vårt Oslo-kurs"].samme_dag]
    assert "IPRO-kurs Oslo" not in [n for n, _ in rader["Vårt webinar"].samme_dag]           # online: ingenting
    assert "IPRO-kurs Oslo" not in [n for n, _ in rader["Vårt Bergen-kurs"].samme_dag]
    assert rader["Vårt webinar"].samme_dag == [] and rader["Vårt Bergen-kurs"].samme_dag == []   # fysisk + online, Oslo + Bergen


def test_aarsplanen_teller_annen_arrangor_bare_ved_ekte_dobbeltbooking(con):
    _plan(con, "IPRO uke 10", _samling(date(2031, 3, 4), date(2031, 3, 6), sted="Oslo", lokale="Paleet"),
          arrangor="IPRO", ekstern="1")
    _plan(con, "Vårt webinar", _samling(date(2031, 3, 4), sted="Online"))
    _plan(con, "Vårt Oslo-kurs", _samling(date(2031, 3, 6), sted="Oslo"))
    plan = aarsplan.hent(con, 2031)
    dager = {d.dato.day: d for u in aarsplan.aarskalender(plan, date(2000, 1, 1))[2].uker for d in u.dager
             if d.i_maaneden}
    assert not dager[4].kollisjon and dager[4].opptatt == 1         # IPRO + vårt webinar: ingen dobbeltbooking
    assert not dager[5].kollisjon and dager[5].opptatt == 0         # bare IPRO: dagen er ledig for oss
    assert dager[6].kollisjon                                       # IPRO + vårt fysiske Oslo-kurs
    assert [o["tekst"] for o in aarsplan.overlapp_perioder(plan)] == ["6. mar"]
    assert any(p.fra <= date(2031, 3, 5) <= p.til for p in aarsplan.ledige(plan, 1))       # IPRO alene sperrer ikke


def test_oversikten_viser_ikke_planlagte_kurs_men_kalenderen_gjor(con):
    pid = _plan(con, "Planlagt testkurs Q", _samling(FRAM, FRAM + timedelta(days=2), nr=1),
                _samling(FRAM + timedelta(days=60), nr=2), sjekk="Usikker dato i Excel")
    k = _klient()
    assert "Planlagt testkurs Q" not in k.get("/admin").get_data(as_text=True)
    t = k.get("/admin/aktiviteter/kalender").get_data(as_text=True)
    assert "Planlagt testkurs Q" in t and f'href="/admin/aktiviteter/planlagte-kurs/{pid}"' in t
    assert '<span class="merke planlagt"' in t and "Må sjekkes" in t and 'id="farger"' in t
    assert "1. samling" in t and "2. samling" in t                             # fargeforklaringen: alle samlingene
    assert f"oversiktsrad kursfarge-{_farge(con, pid)} planlagt" in t


def test_maaned_og_uke_lenker_til_det_planlagte_kurset(con):
    pid = _plan(con, "Blokkurs", _samling(FRAM, FRAM + timedelta(days=1)))
    k = _klient()
    maaned = k.get(f"/admin/aktiviteter/kalender?aar={FRAM.year}&maned={FRAM.month}").get_data(as_text=True)
    assert f'class="hendelse kursfarge-{_farge(con, pid)} planlagt' in maaned
    assert f'href="/admin/aktiviteter/planlagte-kurs/{pid}"' in maaned
    uke = k.get(f"/admin/aktiviteter/kalender?visning=uke&dato={FRAM.isoformat()}").get_data(as_text=True)
    assert f'class="kalenderkort kursfarge-{_farge(con, pid)} planlagt"' in uke and "planlagt kurs" in uke
    assert k.get("/admin/aktiviteter/kalender?status=planlagt").status_code == 200


def test_annen_arrangor_er_graa_og_aldri_neste_kurs(con):
    _plan(con, "Ekstern først", _samling(FRAM), arrangor="IPRO", ekstern="1")
    egen = _plan(con, "Eget kurs etterpå", _samling(FRAM + timedelta(days=3)), _samling(FRAM + timedelta(days=40)))
    hendelser = kurskalender.hent(con, FRAM - timedelta(days=1), FRAM + timedelta(days=90), med_planlagte=True)
    o = kurskalender.oversikt(hendelser, date.today())
    assert o.neste.h.plan_id == egen
    ekstern = next(h for h in hendelser if h.navn == "Ekstern først")
    assert ekstern.klasser == "planlagt ekstern"
    assert [f.navn for f in kurskalender.fargeforklaring(hendelser, FRAM)] == ["Eget kurs etterpå"]
    t = _klient().get("/admin/aktiviteter/kalender").get_data(as_text=True)
    assert '<span class="merke gra" title="Annen arrangør">IPRO</span>' in t


# ============================ Årsplan ============================

def test_aarsplanen_viser_planlagte_som_kurs(con):
    pid = _plan(con, "Årskurs", _samling(date(2031, 3, 3), date(2031, 3, 5), nr=1, sted="Oslo"),
                _samling(date(2031, 9, 1), date(2031, 9, 3), nr=2, sted="Oslo"), prosjektnr="700")
    rod = _plan(con, "Kurs på 17. mai", _samling(date(2031, 5, 17)))
    _plan(con, "Avlyst årskurs", _samling(date(2031, 3, 4)), status="avlyst")
    db.opprett_kurs(con, kode="EKTE-1", navn="Ekte kurs", datoer=["2031-03-04"], sharepoint_mappe="Kurs/EKTE-1",
                    type="fysisk", sted="IPR, Oslo")
    con.commit()
    plan = aarsplan.hent(con, 2031)
    assert aarsplan.samling(plan, ("plan", pid), date(2031, 9, 2)) == (2, 2)
    assert not [a for akt in plan.per_dato.values() for a in akt if a.tittel == "Avlyst årskurs"]
    assert [o["navn"] for o in aarsplan.overlapp_perioder(plan)] == ["Ekte kurs og 700 Årskurs Oslo (planlagt kurs)"]
    (k,) = aarsplan.konflikter(plan)
    assert (k.alvor, k.kurs["plan_id"]) == ("rod", rod)
    (f,) = [x for x in aarsplan.fargeforklaring(plan) if x["plan_id"] == pid]
    assert f["samlinger"] == ["1. samling 3.–5. mar", "2. samling 1.–3. sep"] and f["farge"] == _farge(con, pid)
    mars = aarsplan.aarskalender(plan, date(2000, 1, 1))[2]
    blokker = [b for u in mars.uker for bane in u.baner for b in bane if b.plan_id == pid]
    assert blokker and blokker[0].tekst == "700 Årskurs Oslo 1/2" and blokker[0].klasser == "planlagt"
    t = _klient().get("/admin/aktiviteter/aarsplan?aar=2031").get_data(as_text=True)
    assert f'href="/admin/aktiviteter/planlagte-kurs/{pid}"' in t and "2 planlagte kurs" in t
    assert "1 kurs · 1 kursdag" in t
    uker = _klient().get("/admin/aktiviteter/aarsplan?aar=2031&vis=uker").get_data(as_text=True)
    assert f'href="/admin/aktiviteter/planlagte-kurs/{pid}">700 Årskurs Oslo</a>, 1. samling av 2' in uker


# ============================ sidene ============================

def test_nytt_planlagt_kurs_fra_skjemaet(con):
    k = _klient()
    assert k.get("/admin/aktiviteter/planlagte-kurs/ny").status_code == 200
    r = k.post("/admin/aktiviteter/planlagte-kurs/ny", data={
        "prosjektnr": "950", "navn": "Skjemakurs", "arrangor": "IPR", "status": "planlagt", "farge": "auto",
        "notat": "Kursnotat", "s-fra_dato": "2031-10-06", "s-til_dato": "2031-10-09", "s-nr": "1",
        "s-veiledningsdager": "06.10.2031", "s-sted": "Oslo", "s-lokale": "Paleet", "s-kursholdere": "Holder H (T1)",
        "s-notat": "Samlingsnotat"})
    assert r.status_code == 302
    pid = con.execute("SELECT id FROM planlagt_kurs WHERE navn='Skjemakurs'").fetchone()[0]
    assert r.headers["Location"].endswith(f"/admin/aktiviteter/planlagte-kurs/{pid}")
    kurs = planlagte_kurs.hent(con, pid)
    assert (kurs["prosjektnr"], kurs["notat"], len(kurs["samlinger"])) == ("950", "Kursnotat", 1)
    s = kurs["samlinger"][0]
    assert (s["fra_dato"], s["til_dato"], s["veiledningsdager_visning"], s["notat"]) == ("2031-10-06", "2031-10-09",
                                                                                         "06.10.2031", "Samlingsnotat")
    side = k.get(f"/admin/aktiviteter/planlagte-kurs/{pid}").get_data(as_text=True)
    assert "950 Skjemakurs" in side and "Holder H (T1)" in side and "Paleet" in side


def test_feil_i_skjemaet_viser_meldingen_og_beholder_feltene(con):
    r = _klient().post("/admin/aktiviteter/planlagte-kurs/ny", data={"navn": "", "s-fra_dato": "2031-10-06",
                                                                     "s-tema": "Beholdt tema"})
    t = r.get_data(as_text=True)
    assert r.status_code == 400 and "Navn må fylles ut." in t and "Beholdt tema" in t
    assert con.execute("SELECT COUNT(*) FROM planlagt_kurs").fetchone()[0] == 0


def test_endre_kurset_og_samlingene_paa_siden(con):
    pid = _plan(con, "Endrekurs", _samling(date(2031, 11, 3)), sjekk="Sjekk datoen")
    k = _klient()
    url = f"/admin/aktiviteter/planlagte-kurs/{pid}"
    assert "Sjekk datoen" in k.get(url).get_data(as_text=True)
    assert k.post(url, data={"navn": "Endret kurs", "status": "planlagt", "farge": "5", "sjekk": ""}).status_code == 302
    kurs = planlagte_kurs.hent(con, pid)
    assert (kurs["navn"], kurs["farge"], kurs["maa_sjekkes"]) == ("Endret kurs", 5, False)
    assert k.post(f"{url}/samling/ny", data={"fra_dato": "2031-12-01", "nr": "2"}).status_code == 302
    s1, s2 = planlagte_kurs.hent(con, pid)["samlinger"]
    r = k.post(f"{url}/samling/{s2['id']}", data={"fra_dato": "2031-12-08", "til_dato": "2031-12-09", "nr": "2"})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"#samling-{s2['id']}")
    assert planlagte_kurs.hent(con, pid)["samlinger"][1]["til_dato"] == "2031-12-09"
    feil = k.post(f"{url}/samling/{s2['id']}", data={"fra_dato": "2031-12-09", "til_dato": "2031-12-08"})
    assert feil.status_code == 400 and "Til-dato kan ikke være før fra-dato." in feil.get_data(as_text=True)
    assert k.post(f"{url}/samling/{s1['id']}/slett").status_code == 302
    assert k.post(f"{url}/samling/{s2['id']}/slett", follow_redirects=True).get_data(as_text=True).count(
        "Kurset må ha minst én samling") == 1
    assert len(planlagte_kurs.hent(con, pid)["samlinger"]) == 1
    annet = _plan(con, "Annet kurs", _samling(date(2031, 11, 10)))
    fremmed = planlagte_kurs.hent(con, annet)["samlinger"][0]["id"]
    assert k.post(f"{url}/samling/{fremmed}", data={"fra_dato": "2031-11-11"}).status_code == 404
    assert k.get("/admin/aktiviteter/planlagte-kurs/999999").status_code == 404
    assert k.post(f"{url}/slett").status_code == 302 and planlagte_kurs.hent(con, pid) is None
    handlinger = [r[0] for r in con.execute("SELECT handling FROM hendelse WHERE handling LIKE 'planlagt_%' ORDER BY id")]
    assert {"planlagt_kurs_endret", "planlagt_samling_lagt_til", "planlagt_samling_endret", "planlagt_samling_slettet",
            "planlagt_kurs_slettet"} <= set(handlinger)


def test_listen_skjuler_ferdige_kurs_til_man_ber_om_dem(con):
    _plan(con, "Ferdig kurs", _samling(date.today() - timedelta(days=400)))
    _plan(con, "Kommende kurs", _samling(FRAM), sjekk="Usikker")
    k = _klient()
    t = k.get("/admin/aktiviteter/planlagte-kurs").get_data(as_text=True)
    assert "Kommende kurs" in t and "Ferdig kurs" not in t and "vis også 1 som er ferdige" in t
    assert "1 må sjekkes" in t and "Nytt planlagt kurs +" in t
    assert "Ferdig kurs" in k.get("/admin/aktiviteter/planlagte-kurs?tidligere=1").get_data(as_text=True)


def test_lesetilgang_kan_se_men_ikke_endre(con):
    pid = _plan(con, "Lesekurs", _samling(FRAM))
    db.opprett_admin_bruker(con, "leser", "Leser", "passord-som-holder", rolle="lese")
    con.commit()
    k = _klient("leser", "passord-som-holder")
    liste = k.get("/admin/aktiviteter/planlagte-kurs").get_data(as_text=True)
    side = k.get(f"/admin/aktiviteter/planlagte-kurs/{pid}").get_data(as_text=True)
    assert "Lesekurs" in liste and "Nytt planlagt kurs +" not in liste and "<form" not in side.split("<main", 1)[-1]
    url = f"/admin/aktiviteter/planlagte-kurs/{pid}"
    for sti, data in ((url, {"navn": "X"}), (f"{url}/slett", {}), ("/admin/aktiviteter/planlagte-kurs/ny", {"navn": "X"}),
                      (f"{url}/samling/ny", {"fra_dato": "2031-01-01"})):
        assert k.post(sti, data=data).status_code == 403, sti
    assert planlagte_kurs.hent(con, pid)["navn"] == "Lesekurs"


def test_fanene_paa_kalendersidene_har_planlagte_kurs():
    from kurs.web import app as webapp
    mal = (webapp.app.root_path + "/templates/admin_aktivitet_faner.html")
    with open(mal, encoding="utf-8") as f:
        tekst = f.read()
    assert "url_for('admin_kalender_planlagte')" in tekst and ">Planlagte kurs</a>" in tekst
