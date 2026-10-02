"""Årsplanen som tolv små månedskalendere (bestillingen 28.09.2026): mandag-søndag med ukenummer, riktige datoer, helger,
røde dager, skoleferier, sperrer og advarsler på dagene, kurs og samlinger som fargede blokker over dagene de varer
(navnet kan fortsette inn i ledige dager), kollisjoner markert, «Opptatt: …» under hver måned, og ledige perioder,
kollisjoner og konflikter som menneskelige tekster under kalenderen."""
import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from werkzeug.datastructures import MultiDict

from kurs import aarsplan, config, db, kursfarger

IKKE_IDAG = date(2000, 1, 1)


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _kurs(con, kode, navn, datoer, **felter):
    kid = db.opprett_kurs(con, kode=kode, navn=navn, datoer=datoer, sharepoint_mappe=f"Kurs/{kode}", **felter)
    con.commit()
    return kid


def _periode(con, type_, navn, fra, til=None, omraade=None, gjelder="alle"):
    v = aarsplan.valider_periode(MultiDict({"type": type_, "navn": navn, "fra_dato": fra, "til_dato": til or "",
                                            "omraade": omraade or "", "gjelder": gjelder, "notat": ""}))
    aarsplan.opprett_periode(con, v, "admin:test")
    con.commit()


def _side(idag=date(2027, 3, 10), **params) -> str:
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    with k.session_transaction() as s:
        s["demo_dato"] = idag.isoformat()
    r = k.get("/admin/aktiviteter/aarsplan", query_string=params)
    assert r.status_code == 200
    return r.get_data(as_text=True)


def _blokker(maaned):
    """[(uke, kolonne, lengde, ekstra, tekst, fra_forrige, til_neste)] for alle blokkene i måneden"""
    return [(u.nr, b.kolonne, b.lengde, b.ekstra, b.tekst, b.fra_forrige, b.til_neste)
            for u in maaned.uker for bane in u.baner for b in bane]


# ============================ månedene ============================

def test_tolv_maaneder_med_uker_fra_mandag_og_ukenummer():
    maaneder = aarsplan.aarskalender(aarsplan.Aarsplan(2027, {}, [], []), IKKE_IDAG)
    assert [m.navn for m in maaneder] == list(aarsplan.MAANEDER) and [m.anker for m in maaneder][:2] == ["mnd-1", "mnd-2"]
    januar, mars = maaneder[0], maaneder[2]
    assert [u.nr for u in januar.uker] == [53, 1, 2, 3, 4]                  # 1. januar 2027 er i uke 53 (2026)
    assert [d.dato.weekday() for d in januar.uker[0].dager] == list(range(7))
    assert [(d.dato.day, d.i_maaneden) for d in januar.uker[0].dager[3:6]] == [(31, False), (1, True), (2, True)]
    assert mars.uker[0].dager[0].dato == date(2027, 3, 1) and mars.uker[0].nr == 9       # 1. mars 2027 er en mandag
    assert [d.dato.day for u in maaneder[1].uker for d in u.dager if d.i_maaneden] == list(range(1, 29))
    assert sum(1 for u in maaneder[11].uker for d in u.dager if d.i_maaneden) == 31
    assert [d.helg for d in mars.uker[0].dager] == [False] * 5 + [True] * 2


def test_blokken_deles_ved_ukeskifte_og_maanedsskifte(con):
    _kurs(con, "L", "Langt kurs", ["2027-10-29", "2027-10-30", "2027-10-31", "2027-11-01", "2027-11-02"])  # fre-tir
    plan = aarsplan.hent(con, 2027)
    okt, nov = aarsplan.aarskalender(plan, IKKE_IDAG)[9:11]
    assert _blokker(okt) == [(43, 4, 3, 0, "Langt kurs", False, True)]
    assert _blokker(nov) == [(44, 0, 2, 3, "Langt kurs", True, False)]
    assert okt.opptatt_tekst == "Opptatt: 29.–31. okt" and nov.opptatt_tekst == "Opptatt: 1.–2. nov"
    _kurs(con, "K", "Torsdag-fredag", ["2027-09-30", "2027-10-01"])      # månedsskifte midt i uken
    sep = aarsplan.aarskalender(aarsplan.hent(con, 2027), IKKE_IDAG)[8]
    assert _blokker(sep) == [(39, 3, 1, 0, "Torsdag-fredag", False, True)]   # navnet fortsetter ikke inn i oktober


def test_samtidige_kurs_paa_hver_sin_linje_og_navnet_faar_ledige_dager_etter_blokken(con):
    _kurs(con, "A", "Mandag", ["2027-03-01"])
    _kurs(con, "B", "Onsdag", ["2027-03-03"])
    _kurs(con, "C", "Man-tir", ["2027-03-01", "2027-03-02"])
    _kurs(con, "D", "Lørdag", ["2027-03-06"])
    (uke,) = [u for u in aarsplan.aarskalender(aarsplan.hent(con, 2027), IKKE_IDAG)[2].uker if u.nr == 9]
    assert [[(b.tekst, b.kolonne, b.lengde, b.ekstra) for b in bane] for bane in uke.baner] == [
        [("Man-tir", 0, 2, 0), ("Onsdag", 2, 1, 2), ("Lørdag", 5, 1, 1)], [("Mandag", 0, 1, 3)]]
    onsdag, mandag = uke.baner[0][1], uke.baner[1][0]
    assert (onsdag.spenn, onsdag.andel, mandag.spenn, mandag.andel) == (3, "33.3333%", 4, "25%")
    assert uke.baner[0][0].andel == "100%"
    assert "kollisjon" in uke.dager[0].klasser.split() and "kollisjon" not in uke.dager[2].klasser.split()


def test_samlingene_nummereres_ut_fra_hele_kurset_ogsaa_over_aarsskiftet(con):
    kid = _kurs(con, "E", "EFT 1-årig", ["2026-11-23", "2026-11-24", "2027-01-18", "2027-01-19", "2027-03-08"])
    plan = aarsplan.hent(con, 2027)
    assert aarsplan.samling(plan, kid, date(2027, 1, 18)) == (2, 3)
    assert aarsplan.samling(plan, kid, date(2027, 3, 8)) == (3, 3)
    assert aarsplan.samling(plan, 99999, date(2027, 3, 8)) == (1, 1)
    januar = aarsplan.aarskalender(plan, IKKE_IDAG)[0]
    (b,) = [b for u in januar.uker for bane in u.baner for b in bane]
    assert (b.tekst, b.farge) == ("EFT 1-årig 2/3", kursfarger.farge("EFT 1-årig"))
    assert b.tittel.startswith("EFT 1-årig · 18.–19. jan · samling 2 av 3")
    uke3 = aarsplan.uker(plan, IKKE_IDAG)[3]
    (blokk,) = [b for bane in uke3.baner for b in bane]
    assert (blokk.samling_tekst, blokk.farge) == ("2. samling av 3", kursfarger.farge("EFT 1-årig"))


def test_kurs_i_samme_serie_har_samme_farge():
    a = aarsplan.Aktivitet("kurs", "EFT spesialistutdanning – samling 1", kurs_id=1)
    b = aarsplan.Aktivitet("kurs", "EFT spesialistutdanning – samling 2", kurs_id=2)
    c = aarsplan.Aktivitet("kurs", "Noe helt annet", kurs_id=3, spesialistlop="EFT")
    d = aarsplan.Aktivitet("kurs", "Enda et", kurs_id=4, spesialistlop="EFT")
    assert a.farge == b.farge and c.farge == d.farge and aarsplan.Aktivitet("plan", "X").farge is None


def test_dagene_markerer_ferie_sperre_advarsel_rod_dag_notat_og_i_dag(con):
    _periode(con, "skoleferie", "Vinterferie", "2027-02-22", "2027-02-26", omraade="Oslo")
    _periode(con, "sperre", "Seminar", "2027-02-24", "2027-02-24")
    _periode(con, "advarsel", "Travelt", "2027-02-23", "2027-02-25", gjelder="online")
    db.sett_inn(con, "INSERT INTO planlagt_aktivitet (type, tittel, fra_dato, til_dato) VALUES ('notat', ?, ?, ?)",
                ("Frist", "2027-02-26", "2027-02-26"))
    con.commit()
    feb = aarsplan.aarskalender(aarsplan.hent(con, 2027), date(2027, 2, 22))[1]
    dager = {d.dato.day: d for u in feb.uker for d in u.dager if d.i_maaneden}
    assert dager[22].klasser == "ferie idag"
    assert dager[23].klasser == "ferie advarsel"
    assert dager[24].klasser == "ferie sperre"                       # sperre går foran advarsel
    assert dager[26].klasser == "ferie notat" and dager[27].klasser == "helg"
    assert "Skoleferie Oslo: Vinterferie" in dager[24].tittel and "Sperret: Seminar" in dager[24].tittel
    assert "Advarsel: Travelt (online kurs)" in dager[23].tittel
    mai = aarsplan.aarskalender(aarsplan.hent(con, 2027), IKKE_IDAG)[4]
    (syttende,) = [d for u in mai.uker for d in u.dager if d.i_maaneden and d.dato.day == 17]
    assert (syttende.rod, syttende.klasser) == ("Grunnlovsdag (17. mai) og 2. pinsedag", "rod")


@pytest.mark.parametrize("dager,tekst", [
    ([], "Ingen kurs eller planer"), ([8], "Opptatt: 8. mar"), ([8, 9], "Opptatt: 8.–9. mar"),
    ([2, 8, 9, 16, 17, 18], "Opptatt: 2., 8.–9. og 16.–18. mar")])
def test_opptatte_dager_under_maaneden(dager, tekst):
    assert aarsplan.Minimaaned(2027, 3, [], [date(2027, 3, d) for d in dager]).opptatt_tekst == tekst


# ============================ ledige perioder og kollisjoner ============================

def test_ledig_periode_har_kort_oppsummering_i_stedet_for_alle_datoene(con):
    _kurs(con, "L1", "Før", ["2027-03-19"])
    _kurs(con, "L2", "Etter", ["2027-04-07"])
    _periode(con, "skoleferie", "Påskeferie", "2027-03-22", "2027-03-29", omraade="Oslo")
    _periode(con, "skoleferie", "Påskeferie", "2027-03-22", "2027-03-29", omraade="Vestland")
    (paaske,) = [p for p in aarsplan.ledige(aarsplan.hent(con, 2027), 5) if p.fra == date(2027, 3, 20)]
    assert (paaske.tekst, paaske.dager, paaske.rode) == ("20. mar – 6. apr", 18, 4)
    assert paaske.oppsummering == ("inkl. 4 røde dager · Skoleferie Oslo: Påskeferie · Skoleferie Vestland: Påskeferie")
    _kurs(con, "M1", "Pinse før", ["2027-05-16"])
    _kurs(con, "M2", "Pinse etter", ["2027-05-24"])
    (pinse,) = [p for p in aarsplan.ledige(aarsplan.hent(con, 2027), 5) if p.fra == date(2027, 5, 17)]
    assert (pinse.dager, pinse.rode) == (7, 1)                         # starter på en rød dag: 17. mai
    en = aarsplan.Ledig(date(2027, 5, 1), date(2027, 5, 7), [], 1, [])
    assert en.oppsummering == "inkl. 1 rød dag"
    mange = aarsplan.Ledig(date(2027, 5, 1), date(2027, 5, 7), [], 0, ["A", "B", "C", "D", "E", "A"])
    assert mange.oppsummering == "A · B · C · og 2 til" and aarsplan.Ledig(date(2027, 5, 1), date(2027, 5, 7), []).oppsummering == ""


def test_kollisjonene_har_navnene_i_en_setning(con):
    _kurs(con, "A", "Kurs A", ["2027-10-14"])
    _kurs(con, "B", "Kurs B", ["2027-10-14"])
    _kurs(con, "C", "Kurs C", ["2027-10-14"])
    db.sett_inn(con, "INSERT INTO planlagt_aktivitet (type, tittel, fra_dato, til_dato) VALUES ('plan', ?, ?, ?)",
                ("Fagdag", "2027-10-20", "2027-10-20"))
    _kurs(con, "D", "Kurs D", ["2027-10-20"])
    o = aarsplan.overlapp_perioder(aarsplan.hent(con, 2027))
    assert [(x["tekst"], x["navn"]) for x in o] == [("14. okt", "Kurs A, Kurs B og Kurs C"),
                                                    ("20. okt", "Kurs D og Planlagt: Fagdag")]


# ============================ siden ============================

def test_aaret_er_tolv_maanedskalendere_med_blokker_og_forklaring(con):
    kid = _kurs(con, "W", "Webinar: Skam", ["2027-03-03"], type="digital")
    _kurs(con, "S", "Samtidig kurs", ["2027-03-03"])
    t = _side(aar=2027)
    assert t.count('<section class="minimaaned"') == 12 and '<h2 id="mnd-3-tittel">Mars</h2>' in t
    farge = kursfarger.farge("Webinar: Skam")
    assert re.search(rf'<a class="miniblokk kursfarge-{farge} aapen" href="/admin/kurs/{kid}/deltakere"\s+'
                     r'style="grid-column:4 / span 4; grid-row:\d; --andel:25%" title="Webinar: Skam · 3\. mar · Online">'
                     r'Webinar: Skam</a>', t)
    assert re.search(r'<span class="minidag kollisjon" style="grid-column:4" title="3\.3\.: [^"]*">'
                     r'<span class="dnr">3</span></span>', t)
    assert "Opptatt: 3. mar" in t and "/static/kursfarger.css" in t and "/static/aarsplan.css" in t
    for tekst in ("kurs (farge = kursserie)", "kollisjon: flere samme dag", "rød dag", "skoleferie", "sperret",
                  "advarsel", "helg", "i dag"):
        assert tekst in t.split('<p class="forklaring">')[1].split("</p>")[0], tekst
    assert "repeat(0," not in t                                     # uker uten kurs: gyldig CSS
    topp = t.split('<p class="aarsoppsummering ikke-utskrift">')[1].split("</p>")[0]
    assert '<a href="#overlapp">1 kollisjon</a>' in topp and '<a href="#konflikter">0 konflikter og merknader</a>' in topp
    assert '<a href="#ledige">1 ledig periode</a> på minst 5 dager' in topp          # 4. mars - 31. desember


def test_bare_kommende_ledige_perioder_vises(con):
    _kurs(con, "A", "Vinterkurs", ["2027-02-15"])
    _kurs(con, "B", "Vårkurs", ["2027-04-12"])
    t = _side(idag=date(2027, 3, 10), aar=2027)
    ledige = t.split('<section class="kort" id="ledige">')[1].split("</section>")[0]
    assert "<strong>1. jan – 14. feb</strong>" not in ledige and "<strong>16. feb – 11. apr</strong>" in ledige
    assert "1 periode tidligere i år vises ikke." in ledige and "dager ledig" in ledige
    fram = _side(idag=date(2026, 9, 28), aar=2027).split('<section class="kort" id="ledige">')[1].split("</section>")[0]
    assert "<strong>1. jan – 14. feb</strong>" in fram and "vises ikke" not in fram      # neste år: alle er kommende


def test_lange_navn_escapes_i_blokkene(con):
    _kurs(con, "X", "Kurs <script>alert(1)</script>", ["2027-03-03"])
    t = _side(aar=2027)
    assert "<script>alert(1)" not in t and "Kurs &lt;script&gt;alert(1)&lt;/script&gt;</a>" in t


def test_skjermbredder_fire_tre_to_og_en_maaned_per_rad():
    css = (Path(aarsplan.__file__).parent / "web" / "static" / "aarsplan.css").read_text(encoding="utf-8")
    assert ".aarskalender { display:grid; grid-template-columns:repeat(4, minmax(0, 1fr));" in css
    assert "@media (max-width: 1500px) { .aarskalender { grid-template-columns:repeat(3, minmax(0, 1fr)); } }" in css
    assert "@media (max-width: 1050px) { .aarskalender { grid-template-columns:repeat(2, minmax(0, 1fr)); } }" in css
    assert ".aarskalender { grid-template-columns:1fr; }" in css.split("@media (max-width: 640px)")[1]
    farger = (Path(aarsplan.__file__).parent / "web" / "static" / "kursfarger.css").read_text(encoding="utf-8")
    assert all(f".kursfarge-{i} {{ --kf:#" in farger and f"--kf-mid:#" in farger for i in range(kursfarger.ANTALL))


# ============================ lagrede samlinger (Opprett kurs) ============================

def test_lagrede_samlinger_gir_samme_nummer_til_delene_av_en_samling(con):
    """Samling 1 med helgen tatt ut vises som to blokker, begge «1/2» - samling 2 er egen."""
    from kurs import kursdatoer
    kid = _kurs(con, "S", "Samlingskurs", ["2027-03-04"])
    uten_helg = {date(2027, 3, 6): kursdatoer.Dagavvik(fjernet=True), date(2027, 3, 7): kursdatoer.Dagavvik(fjernet=True)}
    db.lagre_samlinger(con, kid, [
        kursdatoer.Samling(fra=date(2027, 3, 4), til=date(2027, 3, 8), start_kl="09:00", slutt_kl="16:00", timer=6,
                           navn="Grunnsamling", avvik=uten_helg),
        kursdatoer.Samling(fra=date(2027, 4, 12), til=None, start_kl="10:00", slutt_kl="15:00", timer=5)],
        aktor="admin:test")
    con.commit()
    plan = aarsplan.hent(con, 2027)
    assert aarsplan.samling(plan, kid, date(2027, 3, 8)) == (1, 2) and aarsplan.samling(plan, kid, date(2027, 4, 12)) == (2, 2)
    mars = aarsplan.aarskalender(plan, IKKE_IDAG)[2]
    assert sorted((b.tekst, b.kolonne) for u in mars.uker for bane in u.baner for b in bane) == [
        ("Samlingskurs 1/2", 0), ("Samlingskurs 1/2", 3)]           # 4.-5. mars (tor-fre) og mandag 8. mars

