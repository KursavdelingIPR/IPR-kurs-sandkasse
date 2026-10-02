"""Årsplanen, utvidet (bestillingen 27.09.2026): røde dager, skoleferier per område, sperrede perioder og advarsler,
konflikter, konkrete ledige perioder («8.–14. mar – 7 dager»), overlapp som perioder og uke for uke med kursene som
blokker. Skoleferier er aldri faste, nasjonale datoer i koden - de legges inn av admin per område. Ingen periode hindrer
at kurs opprettes."""
import json
import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from werkzeug.datastructures import MultiDict

from kurs import aarsplan, config, db, kursfarger, migreringer


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
    k.post("/admin/logg-inn", data={"brukernavn": brukernavn or config.ADMIN_BRUKERNAVN,
                                    "passord": passord or config.ADMIN_PASSORD})
    return k


def _kurs(con, kode, navn, datoer, **felter):
    kid = db.opprett_kurs(con, kode=kode, navn=navn, datoer=datoer, sharepoint_mappe=f"Kurs/{kode}", **felter)
    con.commit()
    return kid


def _periode(con, type_, navn, fra, til=None, omraade=None, gjelder="alle", notat=None):
    v = aarsplan.valider_periode(MultiDict({"type": type_, "navn": navn, "fra_dato": fra, "til_dato": til or "",
                                            "omraade": omraade or "", "gjelder": gjelder, "notat": notat or ""}))
    pid = aarsplan.opprett_periode(con, v, "admin:test")
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


# ============================ røde dager ============================

def test_roede_dager_regnes_ut_og_markeres_i_aaret(con):
    plan = aarsplan.hent(con, 2027)
    c = _dag(plan, 5, 17)
    assert "rod" in c.klasser.split() and "Rød dag: Grunnlovsdag (17. mai) og 2. pinsedag" in c.tittel
    assert "rod" in _dag(plan, 5, 6).klasser.split()             # Kristi himmelfartsdag 2027
    assert "rod" not in _dag(plan, 5, 5).klasser.split()
    assert con.execute("SELECT COUNT(*) FROM kalenderperiode").fetchone()[0] == 0    # ingenting lagret


def test_kurs_paa_roed_dag_beholder_markeringen(con):
    kid = _kurs(con, "R1", "Kurs på 17. mai", ["2027-05-17"], type="fysisk")
    plan = aarsplan.hent(con, 2027)
    c = _dag(plan, 5, 17)
    assert c.opptatt == 1 and "rod" in c.klasser.split()
    (b,) = [b for u in _maaned(plan, 5).uker for bane in u.baner for b in bane]
    assert (b.kurs_id, b.farge, b.tekst) == (kid, kursfarger.farge("Kurs på 17. mai"), "Kurs på 17. mai")


# ============================ perioder: validering ============================

@pytest.mark.parametrize("felt,melding", [
    ({"type": ""}, "Velg hva slags periode"),
    ({"type": "tull"}, "Velg hva slags periode"),
    ({"navn": ""}, "Navn må fylles ut"),
    ({"navn": "x" * 81}, "høyst 80 tegn"),
    ({"navn": "a\nb"}, "ugyldige tegn"),
    ({"omraade": ""}, "hvilket område"),
    ({"fra_dato": "2027-02-30"}, "ikke en gyldig dato"),
    ({"til_dato": "2027-02-01"}, "før fra-dato"),
    ({"til_dato": "2028-03-01"}, "høyst ett år"),
])
def test_ugyldige_perioder_avvises(felt, melding):
    skjema = {"type": "skoleferie", "navn": "Vinterferie", "omraade": "Oslo", "fra_dato": "2027-02-22",
              "til_dato": "2027-02-26", **felt}
    with pytest.raises(aarsplan.AarsplanFeil, match=melding):
        aarsplan.valider_periode(MultiDict(skjema))


def test_sperre_har_ikke_omraade_og_skoleferie_gjelder_alle():
    v = aarsplan.valider_periode(MultiDict({"type": "sperre", "navn": "Intern ferie", "omraade": "Oslo",
                                            "fra_dato": "2027-07-05", "gjelder": "online"}))
    assert (v["omraade"], v["gjelder"], v["til_dato"]) == (None, "online", "2027-07-05")
    v = aarsplan.valider_periode(MultiDict({"type": "skoleferie", "navn": "Høstferie", "omraade": "Vestland",
                                            "fra_dato": "2027-10-04", "til_dato": "2027-10-08", "gjelder": "online"}))
    assert (v["omraade"], v["gjelder"]) == ("Vestland", "alle")
    with pytest.raises(aarsplan.AarsplanFeil, match="Ugyldig valg"):
        aarsplan.valider_periode(MultiDict({"type": "advarsel", "navn": "X", "fra_dato": "2027-07-05", "gjelder": "tull"}))


# ============================ skoleferier per område ============================

def test_skoleferier_vises_per_omraade(con):
    _periode(con, "skoleferie", "Høstferie", "2027-10-04", "2027-10-08", omraade="Oslo")
    _periode(con, "skoleferie", "Høstferie", "2027-10-11", "2027-10-15", omraade="Vestland")
    alle = aarsplan.hent(con, 2027)
    assert "ferie" in _dag(alle, 10, 5).klasser.split() and "ferie" in _dag(alle, 10, 12).klasser.split()
    assert "Skoleferie Oslo: Høstferie" in _dag(alle, 10, 5).tittel
    oslo = aarsplan.hent(con, 2027, "oslo")
    assert "ferie" in _dag(oslo, 10, 5).klasser.split() and "ferie" not in _dag(oslo, 10, 12).klasser.split()
    assert aarsplan.omraader(con) == ["Oslo", "Vestland"]


def test_nye_omraader_kan_brukes(con):
    _periode(con, "skoleferie", "Vinterferie", "2027-02-15", "2027-02-19", omraade="Trøndelag")
    assert aarsplan.omraader(con) == ["Oslo", "Trøndelag", "Vestland"]
    html = _admin().get("/admin/aktiviteter/aarsplan?aar=2027&omraade=Trøndelag").get_data(as_text=True)
    assert '<option value="Trøndelag" selected>Trøndelag</option>' in html


# ============================ sperrer, advarsler og konflikter ============================

def test_kurs_i_sperret_periode_og_advarsel_vises_som_konflikt_men_stoppes_ikke(con):
    _periode(con, "sperre", "Intern ferie", "2027-07-05", "2027-07-30")
    _periode(con, "advarsel", "Ikke webinarer i juli", "2027-07-01", "2027-07-31", gjelder="online")
    fysisk = _kurs(con, "S1", "Sommerkurs", ["2027-07-06", "2027-07-07"], type="fysisk")
    webinar = _kurs(con, "S2", "Sommerwebinar", ["2027-07-02"], type="digital")
    hybrid = _kurs(con, "S3", "Hybridkurs", ["2027-07-29"], type="hybrid")
    assert con.execute("SELECT COUNT(*) FROM kurs").fetchone()[0] == 3          # ingenting hindret opprettelsen
    k = aarsplan.konflikter(aarsplan.hent(con, 2027))
    assert [(x.alvor, x.kurs["id"], x.datotekst) for x in k] == [
        ("advarsel", webinar, "2. jul"),
        ("sperre", fysisk, "6.–7. jul"),
        ("sperre", hybrid, "29. jul"),
        ("advarsel", hybrid, "29. jul")]
    assert k[0].hva == "Advarsel: Ikke webinarer i juli (online kurs)" and k[1].hva == "Sperret: Intern ferie"
    dag = _dag(aarsplan.hent(con, 2027), 7, 6)
    assert "sperre" in dag.klasser.split() and "advarsel" not in dag.klasser.split()    # sperre går foran advarsel
    assert "advarsel" in _dag(aarsplan.hent(con, 2027), 7, 2).klasser.split()        # bare advarsel


def test_sperre_for_online_gjelder_ikke_fysiske_kurs(con):
    _periode(con, "sperre", "Ingen webinarer", "2027-08-01", "2027-08-31", gjelder="online")
    _kurs(con, "F1", "Fysisk kurs", ["2027-08-10"], type="fysisk")
    assert aarsplan.konflikter(aarsplan.hent(con, 2027)) == []


def test_kurs_paa_roed_dag_er_en_merknad(con):
    kid = _kurs(con, "M1", "Maikurs", ["2027-05-05", "2027-05-06"], type="fysisk")
    (k,) = aarsplan.konflikter(aarsplan.hent(con, 2027))
    assert (k.alvor, k.kurs["id"], k.datotekst, k.hva) == ("rod", kid, "6. mai", "Kristi himmelfartsdag")


# ============================ ledige perioder ============================

def test_konkrete_ledige_perioder_med_antall_dager(con):
    _kurs(con, "L1", "Før", ["2027-03-07"])
    _kurs(con, "L2", "Etter", ["2027-03-15"])
    ledige = aarsplan.ledige(aarsplan.hent(con, 2027), 5)
    mars = next(p for p in ledige if p.fra == date(2027, 3, 8))
    assert (mars.tekst, mars.dager, mars.uker, mars.til) == ("8.–14. mar", 7, "uke 10", date(2027, 3, 14))
    assert ledige[0].fra == date(2027, 1, 1) and ledige[-1].til == date(2027, 12, 31)
    assert ledige[0].uker == "uke 53 (2026) – uke 9"             # 1. januar 2027 er i ISO-uke 53 i 2026


def test_korte_luker_vises_bare_naar_man_ber_om_det(con):
    _kurs(con, "L1", "A", ["2027-03-01"])
    _kurs(con, "L2", "B", ["2027-03-05"])        # 2.-4. mars ledig: 3 dager
    tre = [p for p in aarsplan.ledige(aarsplan.hent(con, 2027), 3) if p.fra == date(2027, 3, 2)]
    assert len(tre) == 1 and tre[0].dager == 3
    assert not [p for p in aarsplan.ledige(aarsplan.hent(con, 2027), 5) if p.fra == date(2027, 3, 2)]


def test_sperre_opptar_datoene_men_advarsel_og_skoleferie_er_merknader(con):
    _kurs(con, "L1", "A", ["2027-06-01"])
    _kurs(con, "L2", "B", ["2027-06-30"])
    _periode(con, "sperre", "Konferanse", "2027-06-14", "2027-06-18")
    _periode(con, "advarsel", "Mange på ferie", "2027-06-21", "2027-06-29")
    _periode(con, "skoleferie", "Sommerferie", "2027-06-19", "2027-06-29", omraade="Oslo")
    juni = [p for p in aarsplan.ledige(aarsplan.hent(con, 2027), 3) if p.fra.month == 6]
    assert [(p.fra.day, p.til.day) for p in juni] == [(2, 13), (19, 29)]
    assert "Advarsel: Mange på ferie" in juni[1].merknader and "Skoleferie Oslo: Sommerferie" in juni[1].merknader


def test_roede_dager_i_ledig_periode_staar_som_merknad(con):
    ledige = aarsplan.ledige(aarsplan.hent(con, 2027), 5)
    (hele,) = ledige
    assert (hele.fra, hele.til, hele.dager) == (date(2027, 1, 1), date(2027, 12, 31), 365)
    assert "17. mai: Grunnlovsdag (17. mai) og 2. pinsedag" in hele.merknader


# ============================ overlapp ============================

def test_overlapp_vises_som_perioder(con):
    _kurs(con, "O1", "Todagers A", ["2027-10-14", "2027-10-15"])
    _kurs(con, "O2", "Todagers B", ["2027-10-14", "2027-10-15"])
    _kurs(con, "O3", "Endags C", ["2027-10-15"])
    o = aarsplan.overlapp_perioder(aarsplan.hent(con, 2027))
    assert [(x["tekst"], x["dager"], sorted(a.tittel for a in x["aktiviteter"])) for x in o] == [
        ("14. okt", 1, ["Todagers A", "Todagers B"]), ("15. okt", 1, ["Endags C", "Todagers A", "Todagers B"])]
    _kurs(con, "O4", "Lang D", ["2027-11-01", "2027-11-02", "2027-11-03"])
    _kurs(con, "O5", "Lang E", ["2027-11-01", "2027-11-02", "2027-11-03"])
    o = aarsplan.overlapp_perioder(aarsplan.hent(con, 2027))
    assert (o[-1]["tekst"], o[-1]["dager"]) == ("1.–3. nov", 3)


# ============================ uke for uke ============================

def test_uke_for_uke_viser_samlinger_som_blokker_over_ukeskifte(con):
    kid = _kurs(con, "U1", "Grunnkurs", ["2027-10-29", "2027-10-30", "2027-10-31", "2027-11-01"], type="hybrid")
    _kurs(con, "U2", "Samtidig", ["2027-10-29"], type="digital")
    uker = aarsplan.uker(aarsplan.hent(con, 2027), date(2027, 10, 29))
    assert len(uker) == 53 and (uker[0].mandag, uker[0].nr, uker[0].iso_aar) == (date(2026, 12, 28), 53, 2026)
    assert (uker[-1].nr, uker[-1].iso_aar, uker[-1].sondag) == (52, 2027, date(2028, 1, 2))
    u43, u44 = uker[43], uker[44]
    assert (u43.nr, u43.tekst) == (43, "25.–31. okt")
    assert [[(b.tittel, b.kolonne, b.lengde, b.fra_forrige, b.til_neste) for b in bane] for bane in u43.baner] == [
        [("Grunnkurs", 4, 3, False, True)], [("Samtidig", 4, 1, False, False)]]
    (b,), = u44.baner
    assert (b.kurs_id, b.kolonne, b.lengde, b.fra_forrige, b.til_neste, b.kurstype) == (kid, 0, 1, True, False, "hybrid")
    assert b.periode == "29. okt – 1. nov (4 dager)"
    assert [d["idag"] for d in u43.dager] == [False] * 4 + [True, False, False]


def test_uke_for_uke_har_merknader_og_ledige_uker(con):
    _periode(con, "skoleferie", "Vinterferie", "2027-02-22", "2027-02-26", omraade="Oslo")
    db.sett_inn(con, "INSERT INTO planlagt_aktivitet (type, tittel, fra_dato, til_dato) VALUES ('notat', ?, ?, ?)",
                ("Søknadsfrist", "2027-02-24", "2027-02-24"))
    con.commit()
    uker = aarsplan.uker(aarsplan.hent(con, 2027), date(2000, 1, 1))
    u8, u20 = uker[8], uker[20]
    assert u8.ledig and ("skoleferie", "Skoleferie Oslo: Vinterferie (22.–26. feb)") in u8.merknader
    assert ("notat", "Notat: Søknadsfrist") in u8.merknader
    assert ("rod", "17. mai: Grunnlovsdag (17. mai) og 2. pinsedag") in u20.merknader


def test_ukeblokker_bruker_ledig_linje_og_slutter_paa_sondag_uten_pil(con):
    _kurs(con, "A", "A mandag-tirsdag", ["2027-03-01", "2027-03-02"])
    _kurs(con, "B", "B onsdag", ["2027-03-03"])
    _kurs(con, "C", "C mandag", ["2027-03-01"])
    _kurs(con, "D", "D fredag-søndag", ["2027-03-05", "2027-03-06", "2027-03-07"])
    uke9 = aarsplan.uker(aarsplan.hent(con, 2027), date(2000, 1, 1))[9]
    assert [[b.tittel for b in bane] for bane in uke9.baner] == [["A mandag-tirsdag", "B onsdag", "D fredag-søndag"],
                                                                 ["C mandag"]]
    d = uke9.baner[0][-1]
    assert (d.kolonne, d.lengde, d.fra_forrige, d.til_neste) == (4, 3, False, False)    # slutter søndag: ingen pil


# ============================ sidene ============================

def test_aarssiden_har_filter_konflikter_ledige_og_perioder(con):
    _periode(con, "skoleferie", "Høstferie", "2027-10-04", "2027-10-08", omraade="Oslo", notat="Fra kommunens nettside")
    _periode(con, "sperre", "Intern ferie", "2027-07-05", "2027-07-30")
    _kurs(con, "S1", "Sommerkurs", ["2027-07-06"], type="fysisk")
    html = _admin().get("/admin/aktiviteter/aarsplan?aar=2027").get_data(as_text=True)
    assert '<body class="bred">' in html and "/static/planlegging.css" in html
    assert '<option value="Oslo">Oslo</option>' in html and '<option value="Vestland">Vestland</option>' in html
    assert "Konflikter og merknader" in html and "Sperret: Intern ferie" in html
    assert "Skoleferie" in html and "Høstferie" in html and "Fra kommunens nettside" in html
    assert re.search(r'<span class="minidag [^"]*\bsperre\b[^"]*"', html)
    assert re.search(r'<span class="minidag [^"]*\bferie\b', html)
    assert "Helt ledige uker:" in html and 'aria-current="page">Året</a>' in html


def test_uke_for_uke_siden(con):
    kid = _kurs(con, "U1", "Grunnkurs", ["2027-10-29", "2027-11-01"], type="fysisk")
    html = _admin().get("/admin/aktiviteter/aarsplan?aar=2027&vis=uker").get_data(as_text=True)
    assert html.count('<section class="ukerad') == 53 and 'aria-current="page">Uke for uke</a>' in html
    assert "<strong>Uke 53 (2026)</strong>" in html
    assert f'href="/admin/kurs/{kid}/deltakere">Grunnkurs</a>, 1. samling av 2' in html
    assert 'class="aarskalender"' not in html
    assert 'href="/admin/aktiviteter/aarsplan?aar=2028&amp;vis=uker"' in html       # neste år beholder visningen


@pytest.mark.parametrize("sporring", ["vis=tull", "omraade=Mars", "ledig=0", "ledig=abc", "omraade=<script>"])
def test_ugyldige_valg_gir_standardvisning(con, sporring):
    r = _admin().get(f"/admin/aktiviteter/aarsplan?aar=2027&{sporring}")
    assert r.status_code == 200 and "<script>" not in r.get_data(as_text=True).split("<main")[1].split("</main>")[0]


def test_ukjent_omraade_viser_alle_skoleferier(con):
    _periode(con, "skoleferie", "Høstferie", "2027-10-04", "2027-10-08", omraade="Oslo")
    html = _admin().get("/admin/aktiviteter/aarsplan?aar=2027&omraade=Mars").get_data(as_text=True)
    assert re.search(r'<span class="minidag [^"]*\bferie\b', html) and '<option value="" >Alle områder' not in html
    assert '<option value="Oslo">Oslo</option>' in html                      # ingen er valgt: «Alle områder»


def test_legg_inn_endre_og_fjern_periode(con):
    k = _admin()
    r = k.post("/admin/aktiviteter/aarsplan/periode/ny", data={"type": "skoleferie", "navn": "Vinterferie", "aar": 2027,
                                                                "omraade": "Vestland", "fra_dato": "2027-03-01",
                                                                "til_dato": "2027-03-05"})
    assert r.status_code == 302 and r.headers["Location"] == "/admin/aktiviteter/aarsplan?aar=2027#perioder"
    pid = con.execute("SELECT id FROM kalenderperiode").fetchone()[0]
    skjema = k.get(f"/admin/aktiviteter/aarsplan/periode/{pid}").get_data(as_text=True)
    assert 'value="Vinterferie"' in skjema and 'value="Vestland"' in skjema
    r = k.post(f"/admin/aktiviteter/aarsplan/periode/{pid}", data={"type": "skoleferie", "navn": "Vinterferie",
                                                                    "omraade": "Vestland", "fra_dato": "2027-02-22",
                                                                    "til_dato": "2027-02-26"})
    assert r.status_code == 302
    assert tuple(con.execute("SELECT fra_dato, til_dato FROM kalenderperiode").fetchone()) == ("2027-02-22", "2027-02-26")
    assert k.post(f"/admin/aktiviteter/aarsplan/periode/{pid}/slett").status_code == 302
    assert k.post(f"/admin/aktiviteter/aarsplan/periode/{pid}/slett").status_code == 404
    hendelser = [(r["handling"], json.loads(r["detaljer"])) for r in con.execute(
        "SELECT handling, detaljer FROM hendelse WHERE handling LIKE 'kalenderperiode_%' ORDER BY id")]
    assert [h for h, _ in hendelser] == ["kalenderperiode_opprettet", "kalenderperiode_endret", "kalenderperiode_slettet"]
    assert all("Vinterferie" not in json.dumps(d) and "Vestland" not in json.dumps(d) for _, d in hendelser)


def test_ugyldig_periode_gir_400_og_beholder_det_som_ble_skrevet(con):
    r = _admin().post("/admin/aktiviteter/aarsplan/periode/ny", data={"type": "skoleferie", "navn": "Beholdes", "aar": 2027,
                                                                       "fra_dato": "2027-03-01"})
    html = r.get_data(as_text=True)
    assert r.status_code == 400 and "hvilket område" in html and 'value="Beholdes"' in html
    assert '<details id="ny-periode" class="kort ikke-utskrift" open>' in html
    assert con.execute("SELECT COUNT(*) FROM kalenderperiode").fetchone()[0] == 0


def test_lesetilgang_ser_periodene_men_kan_ikke_endre(con):
    pid = _periode(con, "sperre", "Intern ferie", "2027-07-05", "2027-07-30")
    db.opprett_admin_bruker(con, "leser", "Leser", "passord-som-holder", rolle="lese")
    con.commit()
    k = _admin("leser", "passord-som-holder")
    html = k.get("/admin/aktiviteter/aarsplan?aar=2027").get_data(as_text=True)
    assert "Intern ferie" in html and "Legg inn skoleferie eller egen periode" not in html and "/periode/" not in html
    assert k.post("/admin/aktiviteter/aarsplan/periode/ny", data={"type": "sperre", "navn": "X",
                                                                   "fra_dato": "2027-01-01"}).status_code == 403
    assert k.post(f"/admin/aktiviteter/aarsplan/periode/{pid}/slett").status_code == 403
    assert con.execute("SELECT COUNT(*) FROM kalenderperiode").fetchone()[0] == 1


def test_ingen_deltakerdata_i_aarsplanen(con):
    kid = _kurs(con, "P1", "Personvernkurs", ["2027-03-10"])
    db.meld_paa(con, kid, epost="skal.ikke.vises@example.no", fornavn="Skal Ikke", etternavn="Vises")
    con.commit()
    for vis in ("", "&vis=uker"):
        html = _admin().get(f"/admin/aktiviteter/aarsplan?aar=2027{vis}").get_data(as_text=True)
        assert "skal.ikke.vises" not in html and "Skal Ikke Vises" not in html


def test_uke_for_uke_er_en_liste_og_mobil_faar_en_kolonne(con):
    """Som kurshjulet i Excel: «Uke 5 – Webinar: Skam i terapi · 3. feb», på alle skjermstørrelser."""
    static = Path(aarsplan.__file__).parent / "web" / "static"
    felles = "".join((static / "planlegging.css").read_text(encoding="utf-8").split("@media (max-width: 760px)")[1:])
    assert ".ukerad { grid-template-columns:1fr;" in felles and ".aarsplankort { grid-template-columns:1fr; }" in felles
    css = (static / "aarsplan.css").read_text(encoding="utf-8")
    assert "@media (max-width: 760px) { .ukeliste .ukerad { grid-template-columns:1fr; } }" in css
    kid = _kurs(con, "W1", "Webinar: Skam i terapi", ["2027-02-03"], type="digital")
    html = _admin().get("/admin/aktiviteter/aarsplan?aar=2027&vis=uker").get_data(as_text=True)
    uke5 = html.split('aria-label="Uke 5, 1.–7. feb"')[1].split("</section>")[0]
    liste = uke5.split('<ul class="ukeaktiviteter">')[1].split("</ul>")[0]
    assert f'<a href="/admin/kurs/{kid}/deltakere">Webinar: Skam i terapi</a>' in liste and "3. feb" in liste
    assert f'<li class="kursfarge-{kursfarger.farge("Webinar: Skam i terapi")}">' in liste
    uke6 = html.split('aria-label="Uke 6, 8.–14. feb"')[1].split("</section>")[0]
    assert '<li class="ledig">Ingen kurs</li>' in uke6


# ============================ migrering ============================

def test_migreringen_lager_tabellen_og_kan_kjores_to_ganger(con):
    nr = next(n for n, navn, _ in migreringer.MIGRERINGER if navn == "kalenderperioder")
    con.execute("DROP TABLE kalenderperiode")
    con.commit()
    with pytest.raises(migreringer.VersjonsFeil, match="kalenderperiode"):
        migreringer.kontroller(con)                  # vakten: riktig versjon, men tabellen mangler (en annen gren?)
    con.execute("DELETE FROM schema_versjon WHERE versjon>=?", (nr,))
    con.commit()
    assert migreringer.kjor_manglende(con) == list(range(nr, migreringer.KODEVERSJON + 1))
    assert db.har_tabell(con, "kalenderperiode") and migreringer.kjor_manglende(con) == []
    migreringer.kontroller(con)


def test_uke_for_uke_tar_med_dagene_i_kantukene(con):
    """1. januar 2027 er i ISO-uke 53 i 2026, og kurs de dagene skal også vises."""
    _kurs(con, "N1", "Romjulskurs", ["2026-12-30", "2026-12-31", "2027-01-02"])
    uke = aarsplan.uker(aarsplan.hent(con, 2027), date(2000, 1, 1))[0]
    (bane,) = uke.baner                                          # to samlinger: 30.-31. desember og lørdag 2. januar
    assert [(x.fra, x.til, x.kolonne, x.lengde) for x in bane] == [(date(2026, 12, 30), date(2026, 12, 31), 2, 2),
                                                                   (date(2027, 1, 2), date(2027, 1, 2), 5, 1)]
    assert [d["i_aaret"] for d in uke.dager] == [False] * 4 + [True] * 3
    assert ("rod", "1. jan: 1. nyttårsdag") in uke.merknader

