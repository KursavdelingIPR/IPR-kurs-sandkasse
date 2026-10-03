"""Kursoversikten - standardvisningen i Kalender (bestillingen 28.09.2026): kursene kronologisk per måned, så man raskt
ser hva som er neste kurs og hva som kommer etter. Dato til venstre, kursnavn, samling, sted/online, ansvarlig,
påmeldte/kapasitet og status. «Neste kurs» er merket, gjennomførte kurs er tonet ned eller ligger under «Tidligere kurs»,
og lange utdanninger med samlinger fram i tid står som aktuelle. Fargen følger kursserien (kurs/kursfarger.py).
"""
import re
from datetime import date
from pathlib import Path

import pytest

from kurs import config, db, kursfarger, kurskalender

IDAG = date(2027, 3, 10)                                # en onsdag


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


def _side(idag=IDAG, **params) -> str:
    from kurs.web import app as webapp
    klient = webapp.app.test_client()
    klient.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    with klient.session_transaction() as s:
        s["demo_dato"] = idag.isoformat()
    r = klient.get("/admin/aktiviteter/kalender", query_string=params)
    assert r.status_code == 200
    return r.get_data(as_text=True)


def _h(navn, fra, til=None, **kw):
    return kurskalender.Hendelse(kurs_id=kw.pop("kurs_id", 1), kursnr=1, navn=navn, type=kw.pop("type", "fysisk"),
                                 sted=kw.pop("sted", "IPR, Bergen"), status=kw.pop("status", "aapen"), ansvarlig=None,
                                 fra=fra, til=til or fra, start_kl=None, slutt_kl=None, **kw)


def _rad(t: str, navn: str) -> str:
    """HTML-en for raden (samlingen) med dette kursnavnet i kursoversikten."""
    treff = [r for r in re.findall(r'<article class="oversiktsrad.*?</article>', t, re.S) if f">{navn}</a>" in r]
    assert len(treff) == 1, (navn, len(treff))
    return treff[0]


# ============================ kursserie og farge ============================

@pytest.mark.parametrize("navn,serie", [
    ("EFT spesialistutdanning – samling 1", "eft spesialistutdanning"),
    ("EFT spesialistutdanning - samling 2", "eft spesialistutdanning"),
    ("EFT 1-årig, 2. samling -Grunnbetingelser i EFT", "eft 1-årig"),
    ("EFST 1-årig, 2.samling", "efst 1-årig"),
    ("Modul 3, 2. samling (kull 13) Veiledningsdag", "modul 3"),
    ("Parterapi i praksis – samling 3 av 4", "parterapi i praksis"),
    ("Parterapi   i praksis", "parterapi i praksis"),
    ("Forsamlingsledelse – grunnkurs", "forsamlingsledelse – grunnkurs"),     # «samling» inne i et ord teller ikke
    ("Forsamling og ledelse", "forsamling og ledelse"),
    ("EFT – Samling 2", "eft"),                                                 # store bokstaver
    ("Samling", "samling"),
    ("", ""),
])
def test_kursserien_er_navnet_uten_samlingsnummeret(navn, serie):
    assert kursfarger.serie(navn) == serie


def test_spesialistloepet_bestemmer_serien_og_fargen():
    assert kursfarger.serie("Hva som helst", " EFT ") == kursfarger.serie("Noe annet", "eft") == "løp:eft"
    assert kursfarger.farge("EFT spesialistutdanning – samling 1") == kursfarger.farge("EFT spesialistutdanning, 2. samling")
    assert kursfarger.farge("Kurs A", "EFT") == kursfarger.farge("Kurs B", "EFT")
    assert kursfarger.serie("Kurs – samling 1", "   ") == "kurs"                   # tomt løp teller ikke


def test_fargen_er_fast_og_innenfor_paletten():
    farger = {kursfarger.farge(f"Kurs {i}") for i in range(200)}
    assert farger == set(range(kursfarger.ANTALL))                    # alle fargene brukes
    assert kursfarger.farge("Parterapi i praksis") == kursfarger.farge("parterapi  i praksis") == 8
    css = (Path(kursfarger.__file__).parent / "web" / "static" / "kursfarger.css").read_text(encoding="utf-8")
    assert all(f".kursfarge-{i} {{ --kf:#" in css for i in range(kursfarger.ANTALL))


# ============================ tekstene i raden ============================

@pytest.mark.parametrize("fra,til,periode,ukedager", [
    (date(2027, 3, 18), date(2027, 3, 18), "18. mar", "tor"),
    (date(2027, 3, 18), date(2027, 3, 19), "18.–19. mar", "tor–fre · 2 dager"),
    (date(2027, 3, 30), date(2027, 4, 2), "30. mar – 2. apr", "tir–fre · 4 dager"),
    (date(2026, 12, 30), date(2027, 1, 1), "30. des – 1. jan", "ons–fre · 3 dager")])
def test_dato_og_ukedager(fra, til, periode, ukedager):
    h = _h("X", fra, til)
    assert (h.kort_periode, h.ukedager) == (periode, ukedager)


@pytest.mark.parametrize("type_,sted,tekst", [
    ("fysisk", "IPR, Bergen", "IPR, Bergen"), ("fysisk", None, "Sted ikke satt"), ("digital", None, "Online"),
    ("digital", "Zoom", "Online · Zoom"), ("digital", "Online via Zoom", "Online via Zoom"),
    ("hybrid", "Zoom / IPR Bergen", "Hybrid · Zoom / IPR Bergen"), ("hybrid", "", "Hybrid")])
def test_sted_eller_online(type_, sted, tekst):
    assert _h("X", IDAG, type=type_, sted=sted).sted_visning == tekst


@pytest.mark.parametrize("bekreftet,kapasitet,tekst", [(6, 16, "6/16 påmeldte"), (5, None, "5 påmeldte"),
                                                       (1, None, "1 påmeldt"), (0, 0, "0 påmeldte")])
def test_paameldte_og_kapasitet(bekreftet, kapasitet, tekst):
    assert _h("X", IDAG, bekreftet=bekreftet, kapasitet=kapasitet).paameldte_tekst == tekst


def test_hent_teller_bare_bekreftede_og_tar_med_kapasitet_og_loep(con):
    kid = _kurs(con, "K", "Kurs", ["2027-03-18"], kapasitet=2, spesialistlop="EFT")
    for i in range(3):
        db.meld_paa(con, kid, epost=f"d{i}@example.no", fornavn=f"D{i}", etternavn="Test")   # den tredje: venteliste
    pid, _ = db.meld_paa(con, kid, epost="borte@example.no", fornavn="Borte", etternavn="Test")
    db.sett_paamelding_status(con, pid, "avmeldt")
    con.commit()
    (h,) = kurskalender.hent(con, date(2027, 3, 1), date(2027, 3, 31))
    assert (h.bekreftet, h.kapasitet, h.kode, h.spesialistlop, h.paameldte_tekst) == (2, 2, "K", "EFT", "2/2 påmeldte")
    assert h.farge == kursfarger.farge("Kurs", "EFT")


# ============================ oversikten ============================

def test_maanedene_fra_denne_maaneden_ogsaa_tomme_og_tidligere_for_seg():
    hendelser = [_h("Januar", date(2027, 1, 20)), _h("Februar", date(2027, 2, 3)), _h("Februar 2", date(2027, 2, 20)),
                 _h("Tidlig i mars", date(2027, 3, 2)), _h("Senere i mars", date(2027, 3, 18)), _h("Juni", date(2027, 6, 1))]
    o = kurskalender.oversikt(hendelser, IDAG)
    assert [(m.tittel, [r.h.navn for r in m.rader]) for m in o.maaneder] == [
        ("Mars 2027", ["Tidlig i mars", "Senere i mars"]), ("April 2027", []), ("Mai 2027", []), ("Juni 2027", ["Juni"])]
    assert [(m.tittel, [r.h.navn for r in m.rader]) for m in o.tidligere] == [("Februar 2027", ["Februar 2", "Februar"]),
                                                                              ("Januar 2027", ["Januar"])]
    mars = o.maaneder[0]
    assert ([r.h.navn for r in mars.gjennomforte], [r.h.navn for r in mars.aktuelle]) == (["Tidlig i mars"],
                                                                                          ["Senere i mars"])
    assert (mars.anker, mars.kort, mars.antall) == ("m-2027-03", "mar", 2)


def test_uten_kurs_vises_bare_denne_maaneden():
    o = kurskalender.oversikt([], IDAG)
    assert [m.tittel for m in o.maaneder] == ["Mars 2027"] and o.tidligere == [] and o.neste is None
    bare_tidligere = kurskalender.oversikt([_h("Januar", date(2027, 1, 5))], IDAG)
    assert [m.tittel for m in bare_tidligere.maaneder] == ["Mars 2027"]
    assert [m.tittel for m in bare_tidligere.tidligere] == ["Januar 2027"]


def test_neste_kurs_er_forste_kommende_som_verken_er_avlyst_eller_utkast():
    hendelser = [_h("I går", date(2027, 3, 9)), _h("I dag", IDAG), _h("Avlyst i dag", IDAG, status="avlyst"),
                 _h("Avlyst", date(2027, 3, 11), status="avlyst"),
                 _h("Utkast", date(2027, 3, 12), status="utkast"), _h("Neste", date(2027, 3, 16)),
                 _h("Etterpå", date(2027, 3, 20))]
    o = kurskalender.oversikt(hendelser, IDAG)
    assert (o.neste.h.navn, o.neste_om) == ("Neste", "om 6 dager")
    assert [r.h.navn for m in o.maaneder for r in m.rader if r.neste] == ["Neste"]
    assert [r.h.navn for r in o.i_dag] == ["I dag"]
    assert kurskalender.om_dager(date(2027, 3, 11), IDAG) == "i morgen"
    assert kurskalender.om_dager(IDAG, IDAG) == "i dag"


def test_samling_som_paagaar_staar_i_denne_maaneden_og_er_ikke_tidligere():
    lang = _h("Startet i februar", date(2027, 2, 26), date(2027, 3, 12))
    o = kurskalender.oversikt([lang], IDAG)
    (rad,) = o.maaneder[0].rader
    assert (rad.pagar, rad.tidligere, o.tidligere, [r.h.navn for r in o.i_dag]) == (True, False, [],
                                                                                     ["Startet i februar"])


def test_lang_utdanning_med_samlinger_fram_i_tid_er_aktuell():
    samlinger = [_h("EFT 1-årig", date(2027, 1, 18), nr=1, antall=3), _h("EFT 1-årig", date(2027, 3, 16), nr=2, antall=3),
                 _h("EFT 1-årig", date(2027, 5, 10), nr=3, antall=3)]
    o = kurskalender.oversikt(samlinger, IDAG)
    assert [(m.tittel, [r.h.samling_tekst for r in m.rader]) for m in o.maaneder if m.rader] == [
        ("Mars 2027", ["Samling 2 av 3"]), ("Mai 2027", ["Samling 3 av 3"])]
    assert o.neste.h.samling_tekst == "Samling 2 av 3"
    assert [r.h.samling_tekst for m in o.tidligere for r in m.rader] == ["Samling 1 av 3"]


def test_kurs_samme_dag_sier_fra_begge_veier_men_ikke_avlyste_eller_samme_kurs():
    a = _h("Kurs A", date(2027, 3, 16), date(2027, 3, 18), kurs_id=1)
    b = _h("Kurs B", date(2027, 3, 18), kurs_id=2)
    avlyst = _h("Avlyst kurs", date(2027, 3, 17), kurs_id=3, status="avlyst")
    a2 = _h("Kurs A", date(2027, 3, 19), kurs_id=1, nr=2, antall=2)      # neste samling i samme kurs
    rader = {r.h.navn + str(r.h.nr): r for r in kurskalender.rader([a, avlyst, b, a2], IDAG)}
    assert rader["Kurs A1"].samme_dag == [("Kurs B", "18. mar")]
    assert rader["Kurs B1"].samme_dag == [("Kurs A", "18. mar")]
    assert rader["Avlyst kurs1"].samme_dag == [] and rader["Kurs A2"].samme_dag == []


def test_avlyste_teller_ikke_i_maanedens_antall():
    o = kurskalender.oversikt([_h("A", date(2027, 3, 16)), _h("B", date(2027, 3, 17), status="avlyst")], IDAG)
    assert o.maaneder[0].antall == 1


# ============================ siden ============================

def test_kursoversikten_er_standardvisningen(con):
    t = _side()
    assert 'aria-current="page">Kursoversikt</a>' in t and 'class="maanedsgrid"' not in t
    assert '<section class="oversiktsmaaned" id="m-2027-03"' in t and "Ingen kurs denne måneden." in t
    assert 'href="/admin/aktiviteter/kalender?aar=2027&amp;maned=3">Måned</a>' in t      # samme måned i månedsvisningen
    assert 'href="/admin/aktiviteter/kalender?visning=uke&amp;dato=2027-03-10">Uke</a>' in t


def test_raden_har_dato_navn_samling_sted_ansvarlig_og_paameldte(con):
    aid = db.opprett_admin_bruker(con, "anne", "Anne Eksempel", "passord-som-holder", rolle="kursadmin")
    kid = _kurs(con, "EFT", "EFT 1-årig", ["2027-03-16", "2027-03-17", "2027-05-10"], type="fysisk", sted="IPR, Oslo",
                kapasitet=24, ansvarlig_admin_id=aid, start_kl="09:00", slutt_kl="16:00")
    for i in range(6):
        db.meld_paa(con, kid, epost=f"d{i}@example.no", fornavn=f"D{i}", etternavn="Test")
    con.commit()
    t = _side()
    rad = _rad(t.split('id="m-2027-03"')[1].split("</section>")[0], "EFT 1-årig")
    for tekst in ('<span class="odag">16.–17. mar</span>', '<span class="oukedag">tir–ons · 2 dager</span>',
                  f'href="/admin/kurs/{kid}/deltakere"', '<span class="osamling">Samling 1 av 2</span>',
                  '<span class="osted">IPR, Oslo</span>', '<span class="otid">kl. 09:00–16:00</span>',
                  '<span class="oansvarlig">Ansvarlig: Anne Eksempel</span>', '<span class="opaameldte">6/24 påmeldte</span>',
                  f"kursfarge-{kursfarger.farge('EFT 1-årig')}", 'id="neste"', '<span class="merke neste">Neste kurs</span>'):
        assert tekst in rad, tekst
    assert "Samling 2 av 2" in t.split('id="m-2027-05"')[1].split("</section>")[0]


def test_neste_kurs_og_i_dag_oeverst(con):
    _kurs(con, "IDAG", "Dagens kurs", ["2027-03-10"])
    _kurs(con, "NESTE", "Kommende kurs", ["2027-03-16"])
    t = _side()
    topp = t.split('<div class="nestelinje">')[1].split("</div>")[0]
    assert "<strong>I dag:</strong>" in topp and "Dagens kurs" in topp
    assert "<strong>Neste kurs:</strong>" in topp and '<a href="#neste">16. mar · Kommende kurs</a>' in topp
    assert "– om 6 dager" in topp
    assert '<span class="merke pagar">I dag</span>' in _rad(t, "Dagens kurs")


def test_gjennomforte_i_maaneden_er_sammenfoldet_og_eldre_under_tidligere_kurs(con):
    _kurs(con, "FEB", "Februarkurs", ["2027-02-10"])
    _kurs(con, "TIDLIG", "Tidlig i mars", ["2027-03-02"])
    _kurs(con, "SENERE", "Senere i mars", ["2027-03-18"])
    t = _side()
    mars = t.split('id="m-2027-03"')[1].split("</section>")[0]
    brettet = mars.split('<details class="gjennomforte">')[1].split("</details>")[0]
    assert "1 kurs gjennomført tidligere i mars" in brettet and "Tidlig i mars" in brettet
    assert "Senere i mars" not in brettet and "Senere i mars" in mars
    assert mars.count(">Tidlig i mars</a>") == 1                               # bare i den sammenfoldede delen
    assert 'class="oversiktsrad kursfarge-' in brettet and " tidligere" in _rad(brettet, "Tidlig i mars")
    tidligere = t.split('<details class="tidligere" id="tidligere">')[1].split("</details>")[0]
    assert "Februar 2027" in tidligere and "Februarkurs" in tidligere and "Tidlig i mars" not in tidligere
    assert t.index('id="m-2027-03"') < t.index('id="tidligere"')          # tidligere kurs nederst


def test_kollisjon_og_status_vises_i_raden(con):
    _kurs(con, "A", "Kurs A", ["2027-03-16", "2027-03-17"])
    _kurs(con, "B", "Kurs B", ["2027-03-17"], kapasitet=1)
    _kurs(con, "C", "Kurs C", ["2027-03-19"], status="avlyst")
    _kurs(con, "D", "Kurs D", ["2027-03-20"], status="utkast")
    kid = con.execute("SELECT id FROM kurs WHERE kode='B'").fetchone()[0]
    db.meld_paa(con, kid, epost="en@example.no", fornavn="En", etternavn="Test")
    db.meld_paa(con, kid, epost="to@example.no", fornavn="To", etternavn="Test")          # venteliste: kurset er fullt
    con.commit()
    t = _side()
    assert ('<span class="ovarsel" tabindex="0" role="img" aria-label="Advarsel: Samme dag som Kurs B (17. mar)" '
            'title="Samme dag som Kurs B (17. mar)" data-varsel>!</span>') in _rad(t, "Kurs A")      # rødt utropstegn, tekst i boble
    assert '<span class="merke gul">Fullt</span>' in _rad(t, "Kurs B")
    assert '<span class="merke gra">Avlyst</span>' in _rad(t, "Kurs C") and "oversiktsrad kursfarge-" in _rad(t, "Kurs C")
    assert re.search(r'class="oversiktsrad kursfarge-\d+ avlyst', _rad(t, "Kurs C"))
    assert '<span class="merke gra">Utkast</span>' in _rad(t, "Kurs D")


def test_varsel_bare_for_to_fysiske_kurs_i_samme_by_eller_to_nettkurs(con):
    """Camilla 03.10: ikke varsel når et fysisk kurs og et online-/hybridkurs går samtidig, eller ett i Bergen og ett i Oslo."""
    _kurs(con, "B1", "Bergen fysisk", ["2027-03-16"], type="fysisk", sted="IPR, Bergen")
    _kurs(con, "B2", "Bergen hybrid", ["2027-03-16"], type="hybrid", sted="Zoom / IPR Bergen")
    _kurs(con, "O1", "Oslo fysisk", ["2027-03-16"], type="fysisk", sted="Oslo")
    _kurs(con, "W1", "Webinar", ["2027-03-16"], type="digital", sted="Zoom")
    _kurs(con, "B3", "Bergen fysisk to", ["2027-03-17"], type="fysisk", sted="Bergen")
    _kurs(con, "B4", "Bergen fysisk tre", ["2027-03-17"], type="fysisk", sted="IPR, Bergen")
    t = _side()
    for navn in ("Bergen fysisk", "Oslo fysisk"):
        assert 'class="ovarsel"' not in _rad(t, navn), navn
    assert 'aria-label="Advarsel: Samme dag som Webinar (16. mar)"' in _rad(t, "Bergen hybrid")   # to med nettdel
    assert 'aria-label="Advarsel: Samme dag som Bergen fysisk tre (17. mar)"' in _rad(t, "Bergen fysisk to")


def test_filtrene_gjelder_kursoversikten_og_nullstill_gaar_til_oversikten(con):
    _kurs(con, "BGO", "Bergenskurs", ["2027-03-16"], type="fysisk", sted="IPR, Bergen")
    _kurs(con, "OSL", "Oslokurs", ["2027-03-16"], type="fysisk", sted="Oslo")
    t = _side(sted="bergen")
    assert "Bergenskurs" in t and "Oslokurs" not in t
    assert 'href="/admin/aktiviteter/kalender">Nullstill filtre</a>' in t
    assert '<input type="checkbox" id="vis-filtre" class="filterbryter" checked>' in t      # filteret er i bruk
    assert '<input type="checkbox" id="vis-filtre" class="filterbryter">' in _side()
    for filter_ in ({"status": "aapen"}, {"ansvarlig": 1}):
        assert '<input type="checkbox" id="vis-filtre" class="filterbryter" checked>' in _side(**filter_)


def test_gammel_agendalenke_gir_kursoversikten(con):
    t = _side(visning="agenda", aar=2027, maned=3)
    assert 'aria-current="page">Kursoversikt</a>' in t and "Agenda" not in t


def test_ingen_deltakerdata_og_navn_escapes(con):
    kid = _kurs(con, "S", "Kurs <script>alert(1)</script>", ["2027-03-16"])
    db.meld_paa(con, kid, epost="skal.ikke.vises@example.no", fornavn="Skal Ikke", etternavn="Vises")
    con.commit()
    t = _side()
    assert "skal.ikke.vises@example.no" not in t and "Skal Ikke" not in t and "1 påmeldt" in t
    assert "<script>alert(1)" not in t and "Kurs &lt;script&gt;alert(1)&lt;/script&gt;" in t
    for m in re.finditer(r"<script\b[^>]*>", t):
        assert "nonce=" in m.group(0)


def test_mobil_faar_kort_og_filtre_som_kan_foldes_inn(con):
    css = (Path(kurskalender.__file__).parent / "web" / "static" / "kalender.css").read_text(encoding="utf-8")
    mobil = "".join(css.split("@media (max-width: 760px)")[1:])
    assert ".oversiktsrad { grid-template-columns:1fr; }" in mobil
    assert ".filterbryter:not(:checked) ~ .filterlinje { display:none; }" in mobil
    bred = css.split("@media (min-width: 1100px)")[1]
    assert ".oversiktsrad .ometa { display:contents; }" in bred        # faste kolonner på brede skjermer
    t = _side()
    assert t.index('id="vis-filtre"') < t.index('<label for="vis-filtre" class="filterknapp">') < t.index(
        '<form method="get" class="filterlinje">')
