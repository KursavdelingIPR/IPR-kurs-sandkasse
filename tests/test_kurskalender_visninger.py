"""Kalenderen (egen fane i menyen): måned og uke, samlinger, røde dager og mobil (kurs/kurskalender.py).

Kravene (bestillingen 27.09.2026): kursnavn, dato/tid, format, sted og ansvarlig admin; kurs over flere dager og
samlinger; moderate farger; klikk på et kurs åpner det; måned- og ukevisning; en brukbar visning på mobil. Norske røde
dager regnes ut automatisk (kurs/helligdager.py) - ingen lister å vedlikeholde. Endret 28.09.2026: kursoversikten er
standardvisningen (tests/test_kurskalender_oversikt.py), agendaen er erstattet av den, og fargen følger kursserien.
"""
import re
from datetime import date
from pathlib import Path

import pytest

from kurs import config, db, helligdager, kursfarger, kurskalender

IKKE_IDAG = date(2000, 1, 1)


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    return c


def _kurs(con, kode, navn, datoer, **kw):
    kid = db.opprett_kurs(con, kode=kode, navn=navn, datoer=datoer, sharepoint_mappe=f"Kurs/{kode}", **kw)
    con.commit()
    return kid


def _side(**params) -> str:
    from kurs.web import app as webapp
    klient = webapp.app.test_client()
    klient.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    r = klient.get("/admin/aktiviteter/kalender", query_string=params)
    assert r.status_code == 200
    return r.get_data(as_text=True)


def _h(navn, fra, til, **kw):
    return kurskalender.Hendelse(kurs_id=kw.pop("kurs_id", 1), kursnr=1, navn=navn, type=kw.pop("type", "fysisk"),
                                 sted=None, status=kw.pop("status", "aapen"), ansvarlig=None, fra=fra, til=til,
                                 start_kl=None, slutt_kl=None, **kw)


# ============================ røde dager ============================

PAASKE = {1818: date(1818, 3, 22), 2000: date(2000, 4, 23), 2008: date(2008, 3, 23), 2011: date(2011, 4, 24),
          2019: date(2019, 4, 21), 2024: date(2024, 3, 31), 2025: date(2025, 4, 20), 2026: date(2026, 4, 5),
          2027: date(2027, 3, 28), 2028: date(2028, 4, 16), 2029: date(2029, 4, 1), 2030: date(2030, 4, 21),
          2038: date(2038, 4, 25), 2285: date(2285, 3, 22)}


@pytest.mark.parametrize("aar,dag", sorted(PAASKE.items()))
def test_paaskedag_regnes_ut(aar, dag):
    assert helligdager.paaskedag(aar) == dag


def test_alle_roede_dager_i_2026():
    assert helligdager.helligdager(2026) == {
        date(2026, 1, 1): "1. nyttårsdag", date(2026, 4, 2): "Skjærtorsdag", date(2026, 4, 3): "Langfredag",
        date(2026, 4, 5): "1. påskedag", date(2026, 4, 6): "2. påskedag",
        date(2026, 5, 1): "Offentlig høytidsdag (1. mai)", date(2026, 5, 14): "Kristi himmelfartsdag",
        date(2026, 5, 17): "Grunnlovsdag (17. mai)", date(2026, 5, 24): "1. pinsedag", date(2026, 5, 25): "2. pinsedag",
        date(2026, 12, 25): "1. juledag", date(2026, 12, 26): "2. juledag"}
    assert list(helligdager.helligdager(2026)) == sorted(helligdager.helligdager(2026))


def test_helligdager_paa_samme_dag_faar_begge_navnene():
    assert helligdager.helligdager(2027)[date(2027, 5, 17)] == "Grunnlovsdag (17. mai) og 2. pinsedag"
    assert helligdager.helligdager(2008)[date(2008, 5, 1)] == "Offentlig høytidsdag (1. mai) og Kristi himmelfartsdag"
    assert len(helligdager.helligdager(2027)) == 11


def test_roede_dager_over_aarsskiftet():
    assert helligdager.i_perioden(date(2026, 12, 20), date(2027, 1, 5)) == {
        date(2026, 12, 25): "1. juledag", date(2026, 12, 26): "2. juledag", date(2027, 1, 1): "1. nyttårsdag"}


def test_julaften_og_nyttaarsaften_er_ikke_roede_dager():
    assert date(2026, 12, 24) not in helligdager.helligdager(2026)
    assert date(2026, 12, 31) not in helligdager.helligdager(2026)
    assert helligdager.andre_fridager(2026) == {date(2026, 12, 24): "Julaften", date(2026, 12, 31): "Nyttårsaften"}


# ============================ samlinger ============================

def test_sammenhengende_dager_er_en_samling_og_opphold_gir_en_ny(con):
    kid = _kurs(con, "EFT", "EFT 1-årig", ["2027-03-01", "2027-03-02", "2027-03-03", "2027-04-12", "2027-04-13",
                                           "2027-05-10"])
    h = kurskalender.hent(con, date(2027, 3, 1), date(2027, 5, 31))
    assert [(x.fra, x.til, x.nr, x.antall, x.samling_tekst) for x in h] == [
        (date(2027, 3, 1), date(2027, 3, 3), 1, 3, "Samling 1 av 3"),
        (date(2027, 4, 12), date(2027, 4, 13), 2, 3, "Samling 2 av 3"),
        (date(2027, 5, 10), date(2027, 5, 10), 3, 3, "Samling 3 av 3")]
    assert {x.kurs_id for x in h} == {kid}


def test_en_dag_opphold_gir_ny_samling(con):
    """Samlingene utledes av datoene: bare dager som følger rett etter hverandre er samme samling."""
    _kurs(con, "OPP", "Kurs med en dags opphold", ["2027-03-01", "2027-03-03"])
    assert [(x.fra, x.til, x.nr) for x in kurskalender.hent(con, date(2027, 3, 1), date(2027, 3, 31))] == [
        (date(2027, 3, 1), date(2027, 3, 1), 1), (date(2027, 3, 3), date(2027, 3, 3), 2)]


def test_samlingen_nummereres_ut_fra_hele_kurset_ogsaa_naar_bare_en_vises(con):
    _kurs(con, "EFT", "EFT 1-årig", ["2027-03-01", "2027-04-12", "2027-05-10"])
    (h,) = kurskalender.hent(con, date(2027, 4, 1), date(2027, 4, 30))
    assert (h.nr, h.antall) == (2, 3)


def test_endagskurs_har_ingen_samlingstekst(con):
    _kurs(con, "EN", "Én dag", ["2027-03-10"])
    (h,) = kurskalender.hent(con, date(2027, 3, 1), date(2027, 3, 31))
    assert (h.samling_tekst, h.dager, h.periode) == ("", 1, "10.03.2027")


def test_samling_som_startet_foer_perioden_er_med_i_sin_helhet(con):
    _kurs(con, "LANG", "Langt kurs", ["2027-03-30", "2027-03-31", "2027-04-01", "2027-04-02"])
    (h,) = kurskalender.hent(con, date(2027, 4, 1), date(2027, 4, 30))
    assert (h.fra, h.til, h.dager) == (date(2027, 3, 30), date(2027, 4, 2), 4)
    assert kurskalender.hent(con, date(2027, 4, 3), date(2027, 4, 30)) == []


def test_format_tid_sted_og_ansvarlig(con):
    aid = db.opprett_admin_bruker(con, "anne", "Anne Eksempel", "passord-som-holder", rolle="kursadmin")
    _kurs(con, "H", "Hybridkurs", ["2027-03-10"], type="hybrid", sted="Paleet, Oslo / Zoom", ansvarlig_admin_id=aid,
          start_kl="09:00", slutt_kl="16:00")
    _kurs(con, "W", "Webinar", ["2027-03-10"], type="digital", sted="Zoom", start_kl="18:00", slutt_kl="20:00")
    hybrid, webinar = kurskalender.hent(con, date(2027, 3, 1), date(2027, 3, 31))
    assert (hybrid.format_tekst, hybrid.tid, hybrid.sted_tekst, hybrid.ansvarlig) == (
        "Hybrid", "09:00–16:00", "Paleet, Oslo / Zoom", "Anne Eksempel")
    assert (webinar.format_tekst, webinar.sted_tekst, webinar.tid) == ("Online", "", "18:00–20:00")  # Zoom er ikke et sted
    assert hybrid.beskrivelse == "Hybridkurs · 10.03.2027 kl. 09:00–16:00 · Hybrid · Paleet, Oslo / Zoom · Ansvarlig: Anne Eksempel"


def test_klokkeslett_paa_kursdagen_gaar_foran_kursets(con):
    kid = _kurs(con, "KL", "Kveldskurs", ["2027-03-10"], start_kl="09:00", slutt_kl="16:00")
    con.execute("UPDATE kursdag SET start_kl='18:00', slutt_kl='20:00' WHERE kurs_id=?", (kid,))
    con.commit()
    (h,) = kurskalender.hent(con, date(2027, 3, 1), date(2027, 3, 31))
    assert h.tid == "18:00–20:00"


@pytest.mark.parametrize("fra,til,tekst", [
    (date(2026, 9, 18), date(2026, 9, 18), "18.09.2026"),
    (date(2026, 9, 18), date(2026, 9, 21), "18.–21.09.2026"),
    (date(2026, 9, 30), date(2026, 10, 2), "30.09.–02.10.2026"),
    (date(2026, 12, 30), date(2027, 1, 2), "30.12.2026–02.01.2027")])
def test_periode_paa_kortet(fra, til, tekst):
    assert _h("X", fra, til).periode == tekst


@pytest.mark.parametrize("fra,til,tekst", [
    (date(2026, 10, 5), date(2026, 10, 11), "5.–11. oktober 2026"),
    (date(2026, 9, 28), date(2026, 10, 4), "28. september – 4. oktober 2026"),
    (date(2026, 12, 28), date(2027, 1, 3), "28. desember 2026 – 3. januar 2027")])
def test_periode_i_overskriften(fra, til, tekst):
    assert kurskalender.periode_tekst(fra, til) == tekst


# ============================ månedsvisning ============================

def test_maaneden_vises_som_hele_uker_fra_mandag():
    uker = kurskalender.uker(2026, 10, [], date(2026, 10, 15))
    assert (uker[0].dager[0].dato, uker[-1].dager[-1].dato) == (date(2026, 9, 28), date(2026, 11, 1))
    assert [u.ukenr for u in uker] == [40, 41, 42, 43, 44]
    assert [d.i_maaneden for d in uker[0].dager] == [False, False, False, True, True, True, True]
    assert [d.dato for u in uker for d in u.dager if d.idag] == [date(2026, 10, 15)]
    assert [d.helg for d in uker[1].dager] == [False] * 5 + [True] * 2


def test_overlappende_kurs_legges_paa_hver_sin_linje_og_ledig_linje_gjenbrukes():
    a = _h("A", date(2027, 3, 1), date(2027, 3, 4))     # man-tor
    b = _h("B", date(2027, 3, 3), date(2027, 3, 3))     # ons: samtidig med A
    c = _h("C", date(2027, 3, 5), date(2027, 3, 6))     # fre-lør: plass på A sin linje
    uke = kurskalender.uker(2027, 3, [a, b, c], IKKE_IDAG)[0]
    assert [[s.hendelse.navn for s in bane] for bane in uke.baner] == [["A", "C"], ["B"]]
    assert [(s.kolonne, s.lengde) for s in uke.baner[0]] == [(0, 4), (4, 2)]


def test_lengste_kurs_ligger_oeverst_naar_flere_starter_samme_dag():
    kort = _h("Kort", date(2027, 3, 1), date(2027, 3, 1))
    langt = _h("Langt", date(2027, 3, 1), date(2027, 3, 3))
    uke = kurskalender.uker(2027, 3, [kort, langt], IKKE_IDAG)[0]
    assert [[s.hendelse.navn for s in bane] for bane in uke.baner] == [["Langt"], ["Kort"]]


def test_samling_over_ukeskifte_blir_to_blokker_som_henger_sammen():
    h = _h("Grunnkurs", date(2026, 10, 30), date(2026, 11, 2))          # fre-man
    nov = kurskalender.uker(2026, 11, [h], IKKE_IDAG)
    (forste,), (andre,) = nov[0].baner[0], nov[1].baner[0]
    assert (forste.kolonne, forste.lengde, forste.fra_forrige, forste.til_neste) == (4, 3, False, True)
    assert (andre.kolonne, andre.lengde, andre.fra_forrige, andre.til_neste) == (0, 1, True, False)


def test_roede_dager_markeres_i_maaneden():
    uker = kurskalender.uker(2027, 5, [], IKKE_IDAG)
    assert {d.dato: d.rod_dag for u in uker for d in u.dager if d.rod_dag} == {
        date(2027, 5, 1): "Offentlig høytidsdag (1. mai)", date(2027, 5, 6): "Kristi himmelfartsdag",
        date(2027, 5, 16): "1. pinsedag", date(2027, 5, 17): "Grunnlovsdag (17. mai) og 2. pinsedag"}


def test_maanedssiden_viser_en_samling_som_en_blokk_over_dagene(con):
    kid = _kurs(con, "MOD", "Modul 2", ["2026-10-22", "2026-10-23", "2026-10-24", "2026-10-25"], type="fysisk")
    t = _side(aar=2026, maned=10)
    farge = kursfarger.farge("Modul 2")
    assert re.search(rf'<a class="hendelse kursfarge-{farge} [^"]*"\s+style="grid-column:4 / span 4; grid-row:2"\s+'
                     rf'href="/admin/kurs/{kid}/deltakere"', t)          # torsdag 22. til søndag 25. oktober


def test_maanedssiden_viser_piler_naar_samlingen_fortsetter_neste_uke(con):
    _kurs(con, "LANG", "Grunnkurs", ["2026-10-30", "2026-10-31", "2026-11-01", "2026-11-02"])
    assert "Grunnkurs ▸</a>" in _side(aar=2026, maned=10)
    assert "◂ Grunnkurs" in _side(aar=2026, maned=11)


def test_roed_dag_vises_med_navn_ogsaa_naar_et_kurs_gaar_den_dagen(con):
    _kurs(con, "MAI", "Modul 3", ["2027-05-03", "2027-05-04", "2027-05-05", "2027-05-06"])
    t = _side(aar=2027, maned=5)
    assert ('<div class="dagtopp"><span class="dagnr">6</span><span class="rodnavn">Kristi himmelfartsdag</span></div>'
            in t)
    assert "Grunnlovsdag (17. mai) og 2. pinsedag" in t


def test_avlyste_kurs_og_utkast_skilles_ut(con):
    _kurs(con, "AV", "Avlyst kurs", ["2027-03-10"], status="avlyst")
    _kurs(con, "UT", "Utkastkurs", ["2027-03-11"], status="utkast")
    t = _side(aar=2027, maned=3)
    assert re.search(r'<a class="hendelse kursfarge-\d+ avlyst"', t) and re.search(r'<a class="hendelse kursfarge-\d+ utkast"', t)


def test_fargeforklaringen_forklarer_kursseriene_og_formatet_staar_som_tekst(con):
    """Fargen følger kursserien (samme utdanning = samme farge); formatet står som tekst på hvert kurs."""
    _kurs(con, "F", "Fysisk kurs", ["2027-03-10"], type="fysisk", sted="IPR, Bergen")
    _kurs(con, "O", "Nettkurs", ["2027-03-11"], type="digital", sted="Zoom")
    _kurs(con, "H", "Blandet kurs", ["2027-03-12"], type="hybrid", sted="Paleet, Oslo / Zoom")
    t = _side(aar=2027, maned=3)
    assert '<span class="prove farge">&nbsp;</span> farge = kursserie (samme farge = samme utdanning)' in t
    liste = t.split('<div class="maanedsliste">')[1]
    for tekst in ('<span class="osted">IPR, Bergen</span>', '<span class="osted">Online · Zoom</span>',
                  '<span class="osted">Hybrid · Paleet, Oslo / Zoom</span>'):
        assert tekst in liste, tekst
    uke = _side(visning="uke", dato="2027-03-10")
    assert "<span>Online · Zoom</span>" in uke and "<span>Hybrid · Paleet, Oslo / Zoom</span>" in uke
    assert f"kalenderkort kursfarge-{kursfarger.farge('Nettkurs')} aapen" in uke


# ============================ uke ============================

def test_ukevisning_gir_dag_n_av_m():
    h = _h("Todagers", date(2026, 10, 15), date(2026, 10, 16))
    dager = kurskalender.uke(date(2026, 10, 14), [h], IKKE_IDAG)
    assert [d.navn for d in dager] == list(kurskalender.UKEDAGER_LANGE) and dager[0].dag.dato == date(2026, 10, 12)
    assert [(d.dag.dato.day, [(x.navn, nr) for x, nr in d.hendelser]) for d in dager if d.hendelser] == [
        (15, [("Todagers", 1)]), (16, [("Todagers", 2)])]


def test_ukesiden(con):
    aid = db.opprett_admin_bruker(con, "anne", "Anne Eksempel", "passord-som-holder", rolle="kursadmin")
    kid = _kurs(con, "TO", "Todagerskurs", ["2026-10-15", "2026-10-16"], type="fysisk", sted="IPR, Bergen",
                start_kl="09:00", slutt_kl="16:00", ansvarlig_admin_id=aid)
    t = _side(visning="uke", dato="2026-10-14")
    assert "Uke 42 · 12.–18. oktober 2026" in t and t.count('<section class="ukedagkolonne') == 7
    assert "Dag 1 av 2" in t and "Dag 2 av 2" in t
    assert "kl. 09:00–16:00" in t and "IPR, Bergen" in t and "Anne Eksempel" in t
    assert f'href="/admin/kurs/{kid}/deltakere"' in t
    assert 'href="/admin/aktiviteter/kalender?visning=uke&amp;dato=2026-10-05"' in t      # forrige uke
    assert 'href="/admin/aktiviteter/kalender?visning=uke&amp;dato=2026-10-19"' in t      # neste uke


def test_uke_over_aarsskiftet(con):
    t = _side(visning="uke", dato="2026-12-31")
    assert "Uke 53 · 28. desember 2026 – 3. januar 2027" in t and '<p class="rodnavn">1. nyttårsdag</p>' in t


# ============================ måneden som liste (mobil) - erstatter agendaen ============================

def test_maanedslisten_har_hvert_kurs_i_maaneden_en_gang(con):
    _kurs(con, "FOR", "Starter før", ["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"])   # startet måneden før
    _kurs(con, "MIDT", "Midt i", ["2026-10-14", "2026-10-15"])
    _kurs(con, "UTE", "Utenfor", ["2026-09-28", "2026-11-02"])          # dager i ukene rundt, men ikke i oktober
    liste = _side(aar=2026, maned=10).split('<div class="maanedsliste">')[1].split("</div>\n{% endif %}")[0]
    navn = re.findall(r'title="[^"]*">([^<]+)</a>', liste)
    assert navn == ["Starter før", "Midt i"]


def test_maanedslisten_paa_siden(con):
    kid = _kurs(con, "SK", "Skamkurs", ["2026-10-07"], type="digital", start_kl="12:00", slutt_kl="15:00")
    t = _side(aar=2026, maned=10)
    liste = t.split('<div class="maanedsliste">')[1]
    assert f'href="/admin/kurs/{kid}/deltakere"' in liste and '<span class="otid">kl. 12:00–15:00</span>' in liste
    assert '<span class="odag">7. okt</span>' in liste and 'class="maanedsgrid"' in t
    assert "Ingen kurs denne måneden." in _side(aar=2026, maned=8).split('<div class="maanedsliste">')[1]


# ============================ navigasjon, filtre og mobil ============================

def test_ukjent_visning_gir_maaned_og_ugyldig_dato_gaar_ikke_i_stykker(con):
    t = _side(visning="tull", aar=2027, maned=3)
    assert "Mars 2027" in t and 'class="maanedsgrid"' in t
    _side(visning="uke", dato="2027-02-30")
    _side(visning="uke", dato="<script>")


def test_visningsvalget_beholder_perioden_og_filtrene(con):
    t = _side(aar=2027, maned=3, sted="bergen")
    assert 'href="/admin/aktiviteter/kalender?visning=uke&amp;dato=2027-03-01&amp;sted=bergen"' in t
    assert 'href="/admin/aktiviteter/kalender?sted=bergen">Kursoversikt</a>' in t
    assert 'aria-current="page">Måned</a>' in t


def test_filtrene_gjelder_ogsaa_uke_og_kursoversikten(con):
    _kurs(con, "BGO", "Bergenskurs", ["2027-03-10"], type="fysisk", sted="IPR, Bergen")
    _kurs(con, "OSL", "Oslokurs", ["2027-03-10"], type="fysisk", sted="Oslo")
    for visning in ({"visning": "uke", "dato": "2027-03-10"}, {"visning": "oversikt"}, {"aar": 2027, "maned": 3}):
        t = _side(sted="bergen", **visning)
        assert "Bergenskurs" in t and "Oslokurs" not in t


def test_nullstill_beholder_visning_og_periode(con):
    t = _side(visning="uke", dato="2027-03-10", sted="bergen")
    assert 'href="/admin/aktiviteter/kalender?visning=uke&amp;aar=2027&amp;maned=3&amp;dato=2027-03-10">Nullstill' in t


def test_mobil_faar_maaneden_som_liste(con):
    _kurs(con, "M", "Mobilkurs", ["2027-03-10"])
    t = _side(aar=2027, maned=3)
    assert "Mobilkurs</a>" in t.split('<div class="maanedsliste">')[1] and 'class="maanedsgrid"' in t
    static = Path(kurskalender.__file__).parent / "web" / "static"
    mobil = "".join((static / "planlegging.css").read_text(encoding="utf-8").split("@media (max-width: 760px)")[1:])
    assert ".maanedsgrid { display:none; }" in mobil and ".ukevisning { grid-template-columns:1fr; }" in mobil
    kalender = (static / "kalender.css").read_text(encoding="utf-8")
    assert ".maanedsliste { display:none; }" in kalender.split("@media")[0]
    assert ".maanedsliste { display:block; }" in "".join(kalender.split("@media (max-width: 760px)")[1:])


def test_siden_har_ikke_innebygde_skript(con):
    """Sikkerhetspolicyen (CSP) tillater bare skript med nonce: ingen onclick eller javascript:-lenker."""
    _kurs(con, "S", "Skriptkurs <script>alert(1)</script>", ["2027-03-10"])
    t = _side(aar=2027, maned=3)
    assert "onclick" not in t and "javascript:" not in t and "<script>alert(1)" not in t
    for m in re.finditer(r"<script\b[^>]*>", t):
        assert "nonce=" in m.group(0)


# ============================ lagrede samlinger (Opprett kurs) ============================

def test_lagrede_samlinger_gir_nummer_navn_tider_og_blokker(con):
    """En samling med helgen tatt ut blir to blokker med samme nummer og navn. Tidene er samlingens."""
    from kurs import kursdatoer
    kid = _kurs(con, "S", "Samlingskurs", ["2027-03-04"])
    uten_helg = {date(2027, 3, 6): kursdatoer.Dagavvik(fjernet=True), date(2027, 3, 7): kursdatoer.Dagavvik(fjernet=True)}
    db.lagre_samlinger(con, kid, [
        kursdatoer.Samling(fra=date(2027, 3, 4), til=date(2027, 3, 8), start_kl="09:00", slutt_kl="16:00", timer=6,
                           navn="Grunnsamling", avvik=uten_helg),
        kursdatoer.Samling(fra=date(2027, 4, 12), til=None, start_kl="10:00", slutt_kl="15:00", timer=5)],
        aktor="admin:test")
    con.commit()
    h = kurskalender.hent(con, date(2027, 3, 1), date(2027, 4, 30))
    assert [(x.fra, x.til, x.nr, x.antall, x.samling_tekst, x.tid) for x in h] == [
        (date(2027, 3, 4), date(2027, 3, 5), 1, 2, "Grunnsamling (1 av 2)", "09:00–16:00"),
        (date(2027, 3, 8), date(2027, 3, 8), 1, 2, "Grunnsamling (1 av 2)", "09:00–16:00"),
        (date(2027, 4, 12), date(2027, 4, 12), 2, 2, "Samling 2 av 2", "10:00–15:00")]
    assert "Grunnsamling (1 av 2)" in _side(aar=2027, maned=3).split('<div class="maanedsliste">')[1]


def test_en_samling_per_dag_vises_som_egne_samlinger(con):
    """Kurs med delt faktura per dag (migrering 9): hver dag er sin egen samling - også når dagene følger hverandre."""
    _kurs(con, "D", "Delt faktura", ["2027-03-10", "2027-03-11"], betaling="per_samling")
    h = kurskalender.hent(con, date(2027, 3, 1), date(2027, 3, 31))
    assert [(x.fra, x.til, x.nr, x.antall) for x in h] == [(date(2027, 3, 10), date(2027, 3, 10), 1, 2),
                                                           (date(2027, 3, 11), date(2027, 3, 11), 2, 2)]


def test_kursdager_uten_samling_grupperes_som_sammenhengende_datoer(con):
    """Eldre data uten samling (kursdag.samling_id er tom) vises som før."""
    kid = _kurs(con, "E", "Eldre kurs", ["2027-03-01", "2027-03-02", "2027-03-05"])
    con.execute("UPDATE kursdag SET samling_id=NULL WHERE kurs_id=?", (kid,))
    con.commit()
    h = kurskalender.hent(con, date(2027, 3, 1), date(2027, 3, 31))
    assert [(x.fra, x.til, x.nr, x.antall) for x in h] == [(date(2027, 3, 1), date(2027, 3, 2), 1, 2),
                                                           (date(2027, 3, 5), date(2027, 3, 5), 2, 2)]

