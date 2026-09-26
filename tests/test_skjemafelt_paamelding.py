"""Fase 12C3: skjemaoverstyringer koblet til den offentlige paameldingen (GET/POST) og admin-forhaandsvisningen.

Laaste krav som testes her:
  * ETT skjema-snapshot per request (db.hent_skjemaoverstyringer kalles noyaktig en gang) - brukt til rendring,
    obligatorisk-validering, hvilke POST-felt som leses og hva som lagres. Ingen ny lesing midt i en POST (TOCTOU).
  * Reell DB-lesefeil -> kontrollert 503 (GET, POST og forhaandsvisning), ALDRI standardskjema, INGEN registrering og
    INGEN sideeffekter.
  * Server-side: skjulte felt, HPR uten spesialistlop, sensitive felt paa digitale kurs og hele faktura-/betalingsblokken
    naar den ikke vises, ignoreres fullstendig - ogsaa fra en manipulert POST - og overskriver/sletter aldri noe som
    allerede staar paa personen.
  * Dynamisk obligatorisk telefon/arbeidssted haandheves server-side kun naar feltet er synlig.
  * Hjelpetekst vises som ren, escapet tekst.
  * Korrupte overstyringer gjor ikke siden utilgjengelig; advarsler logges PII-fritt kun ved POST.
Alle testdata er fiktive.
"""
import json
from html.parser import HTMLParser

import pytest

from kurs import config, db, sveiper
from kurs import skjemafelt as sf
from kurs.kjoring import Kjoring


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
    return webapp.app.test_client()


def _admin():
    k = _klient()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def _kurs(con, kode="K1", **kw):
    felter = {"navn": "Eksempelkurs", "type": "fysisk", "sted": "Eksempelsted", "pris_nok": 4500,
              "fakturering": "person", "betaling": "samlet", "kapasitet": 10, **kw}
    kid = db.opprett_kurs(con, kode=kode, datoer=["2099-03-02", "2099-03-03"], sharepoint_mappe=f"K/{kode}", **felter)
    con.commit()
    return kid


def _lagre(con, kid, felt, **egenskaper):
    db.lagre_skjemafelt(con, kid, felt, egenskaper)
    con.commit()


EPOST = "test.person@eksempel.no"
BASIS = {"fornavn": "Test", "etternavn": "Person", "epost": EPOST, "samtykke": "on"}


def _post(kode, **data):
    return _klient().post(f"/kurs/{kode}", data={**BASIS, **data})


def _deltaker(con, epost=EPOST):
    return con.execute("SELECT * FROM deltaker WHERE epost=?", (epost,)).fetchone()


def _paamelding(con, kid, epost=EPOST):
    return con.execute("SELECT p.* FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE p.kurs_id=? "
                       "AND d.epost=?", (kid, epost)).fetchone()


def _sensitivt(con, kid, epost=EPOST):
    p = _paamelding(con, kid, epost)
    return con.execute("SELECT * FROM sensitivt WHERE paamelding_id=?", (p["id"],)).fetchone() if p else None


def _antall(con, tabell):
    return con.execute(f"SELECT COUNT(*) FROM {tabell}").fetchone()[0]


class _Inputs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs, self.labels, self._i_label = [], [], False

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            self.inputs.append(dict(attrs))
        self._i_label = tag == "label"

    def handle_data(self, data):
        if self._i_label and data.strip():
            self.labels.append(" ".join(data.split()))

    def handle_endtag(self, tag):
        if tag == "label":
            self._i_label = False


def _inputs(html) -> list[dict]:
    p = _Inputs()
    p.feed(html)
    return [a for a in p.inputs if a.get("name") != "csrf_token"]   # CSRF-feltet er ikke et skjemafelt


def _navn(html) -> list[str]:
    return [a.get("name") for a in _inputs(html)]


def _deltakerfelt(html) -> list[str]:
    return [n for n in _navn(html) if n in ("telefon", "arbeidssted", "hpr_nr")]


def _attrs(html, navn) -> dict:
    return next(a for a in _inputs(html) if a.get("name") == navn)


# ============================ GET og forhaandsvisning bruker overstyringene ============================

def _sett_opp_overstyrt(con, kode="OVR"):
    kid = _kurs(con, kode=kode, spesialistlop="EFT")
    _lagre(con, kid, "telefon", synlig=False)
    _lagre(con, kid, "arbeidssted", label="Arbeidsgiver", obligatorisk=True, rekkefolge=0,
           hjelpetekst="Brukes på kursbeviset.")
    _lagre(con, kid, "hpr_nr", hjelpetekst="Finnes i Helsepersonellregisteret.")
    return kid


def test_get_og_forhaandsvisning_viser_samme_overstyrte_skjema(con):
    kid = _sett_opp_overstyrt(con)
    sider = [_klient().get("/kurs/OVR"), _admin().get(f"/admin/kurs/{kid}/forhandsvis-paamelding")]
    for r in sider:
        html = r.get_data(as_text=True)
        assert r.status_code == 200
        assert _deltakerfelt(html) == ["arbeidssted", "hpr_nr"]
        assert "required" in _attrs(html, "arbeidssted")
        assert '<label for="f-arbeidssted">Arbeidsgiver *</label>' in html
        assert "Brukes på kursbeviset." in html and "Finnes i Helsepersonellregisteret." in html
        assert "required" not in _attrs(html, "hpr_nr")
    offentlig, preview = (r.get_data(as_text=True) for r in sider)
    assert _inputs(offentlig) == _inputs(preview)


def test_forhaandsvisning_er_fortsatt_get_only_og_uten_registrering(con):
    kid = _sett_opp_overstyrt(con)
    assert _admin().post(f"/admin/kurs/{kid}/forhandsvis-paamelding", data=BASIS).status_code == 405
    html = _admin().get(f"/admin/kurs/{kid}/forhandsvis-paamelding").get_data(as_text=True)
    assert "data-ingen-innsending" in html and '<button type="button">Meld meg på</button>' in html
    assert _antall(con, "paamelding") == 0


def test_standardskjema_uten_overstyringer_er_uendret(con):
    _kurs(con, kode="STD", spesialistlop="EFT")
    html = _klient().get("/kurs/STD").get_data(as_text=True)
    assert '<div><label for="f-telefon">Telefon</label><input id="f-telefon" name="telefon" value=""></div>' in html
    assert '<div><label for="f-arbeidssted">Arbeidssted</label><input id="f-arbeidssted" name="arbeidssted" value=""></div>' in html
    assert '<div><label for="f-hpr_nr">HPR-nummer</label><input id="f-hpr_nr" name="hpr_nr" value=""></div>' in html
    assert 'id="hjelp-' not in html and "aria-describedby" not in html


# ============================ hjelpetekst ============================

def test_hjelpetekst_er_escapet_ren_tekst_med_linjeskift(con):
    kid = _kurs(con, kode="HJ", spesialistlop="EFT")
    farlig = "<script>alert(1)</script>\n{{ 7*7 }} <b>fet</b> **md**"
    _lagre(con, kid, "telefon", hjelpetekst=farlig)
    _lagre(con, kid, "hpr_nr", hjelpetekst="Linje 1\nLinje 2")
    html = _klient().get("/kurs/HJ").get_data(as_text=True)
    assert ('<p id="hjelp-telefon" class="liten dempet" style="white-space:pre-line; margin:4px 0 0">'
            "&lt;script&gt;alert(1)&lt;/script&gt;\n{{ 7*7 }} &lt;b&gt;fet&lt;/b&gt; **md**</p>") in html
    assert "<script>alert(1)</script>" not in html and "<b>fet</b>" not in html
    assert '<p id="hjelp-hpr_nr" class="liten dempet" style="white-space:pre-line; margin:4px 0 0">Linje 1\nLinje 2</p>' in html
    assert _attrs(html, "telefon")["aria-describedby"] == "hjelp-telefon"
    assert "aria-describedby" not in _attrs(html, "arbeidssted")


def test_hjelpetekst_vises_ogsaa_i_forhaandsvisning(con):
    kid = _kurs(con, kode="HJ2")
    _lagre(con, kid, "arbeidssted", hjelpetekst="Hjelp <i>x</i>")
    html = _admin().get(f"/admin/kurs/{kid}/forhandsvis-paamelding").get_data(as_text=True)
    assert ">Hjelp &lt;i&gt;x&lt;/i&gt;</p>" in html


# ============================ dynamisk obligatorisk (G, H, I, J) ============================

@pytest.mark.parametrize("felt,verdi", [("telefon", ""), ("telefon", None), ("arbeidssted", "   "),
                                        ("arbeidssted", "\t \n")])
def test_synlig_obligatorisk_felt_tomt_gir_400_og_ingen_registrering(con, felt, verdi):
    kid = _kurs(con, kode="OB")
    _lagre(con, kid, felt, obligatorisk=True, label="Mitt <felt>")
    data = {"arbeidssted": "Eksempel AS", "telefon": "+47 000 00 000"}
    if verdi is None:
        data.pop(felt)
    else:
        data[felt] = verdi
    r = _post("OB", **data)
    html = r.get_data(as_text=True)
    assert r.status_code == 400
    assert "Fyll inn «Mitt &lt;felt&gt;»." in html                     # label escapes ogsaa i feilmeldingen
    assert _antall(con, "paamelding") == 0 and _deltaker(con) is None
    annet = "arbeidssted" if felt == "telefon" else "telefon"
    assert _attrs(html, annet)["value"] == data[annet]                  # tidligere innsendte synlige verdier beholdes
    assert (_attrs(html, "fornavn")["value"], _attrs(html, "etternavn")["value"]) == ("Test", "Person")
    assert "required" in _attrs(html, felt)                              # samme snapshot ved re-rendring


def test_obligatorisk_felt_med_verdi_registreres(con):
    kid = _kurs(con, kode="OB2")
    _lagre(con, kid, "telefon", obligatorisk=True)
    _lagre(con, kid, "arbeidssted", obligatorisk=True)
    assert _post("OB2", telefon=" 123 ", arbeidssted="Eksempel AS").status_code == 200
    d = _deltaker(con)
    assert (d["telefon"], d["arbeidssted"]) == (" 123 ", "Eksempel AS")   # lagres som foer (ingen ny trimming)


def test_skjult_men_obligatorisk_felt_valideres_ikke(con):
    kid = _kurs(con, kode="SO")
    _lagre(con, kid, "telefon", synlig=False, obligatorisk=True)
    r = _post("SO")
    assert r.status_code == 200 and _paamelding(con, kid) is not None


def test_synlig_valgfritt_felt_tomt_er_som_foer(con):
    kid = _kurs(con, kode="VF")
    assert _post("VF", telefon="", arbeidssted="").status_code == 200
    d = _deltaker(con)
    assert (d["telefon"], d["arbeidssted"]) == ("", "")          # dagens adferd: tom streng lagres for ny deltaker


def test_obligatorisk_bruker_ikke_gammel_verdi_paa_personen(con):
    kid = _kurs(con, kode="GV")
    db.finn_eller_opprett_deltaker(con, EPOST, "Test", "Person", telefon="99999999")
    con.commit()
    _lagre(con, kid, "telefon", obligatorisk=True)
    assert _post("GV", telefon="").status_code == 400
    assert _paamelding(con, kid) is None


# ============================ manipulert POST (A-F) ============================

def test_a_skjult_telefon_ignoreres(con):
    kid = _kurs(con, kode="MA")
    _lagre(con, kid, "telefon", synlig=False)
    assert _post("MA", telefon="MANIPULERT", arbeidssted="Eksempel AS").status_code == 200
    d = _deltaker(con)
    assert (d["telefon"], d["arbeidssted"]) == (None, "Eksempel AS")


def test_b_skjult_arbeidssted_ignoreres(con):
    kid = _kurs(con, kode="MB")
    _lagre(con, kid, "arbeidssted", synlig=False)
    assert _post("MB", telefon="123", arbeidssted="MANIPULERT").status_code == 200
    d = _deltaker(con)
    assert (d["telefon"], d["arbeidssted"]) == ("123", None)


def test_c_hpr_uten_spesialistlop_ignoreres(con):
    _kurs(con, kode="MC")
    assert _post("MC", hpr_nr="MANIPULERT").status_code == 200
    assert _deltaker(con)["hpr_nr"] is None


def test_hpr_med_spesialistlop_lagres_som_foer_og_er_aldri_obligatorisk(con):
    _kurs(con, kode="MC2", spesialistlop="EFT")
    assert _post("MC2").status_code == 200                          # uten HPR: fortsatt OK
    assert _post("MC2", epost="annen@eksempel.no", hpr_nr="1234567").status_code == 200
    assert _deltaker(con, "annen@eksempel.no")["hpr_nr"] == "1234567"


@pytest.mark.parametrize("data", [{"allergier": "MANIPULERT"}, {"tilrettelegging": "MANIPULERT"},
                                  {"allergier": "X", "tilrettelegging": "Y"}])
def test_d_e_sensitive_felt_paa_digitalt_kurs_ignoreres(con, data):
    kid = _kurs(con, kode="MD", type="digital", sted=None)
    assert _post("MD", **data).status_code == 200
    assert _sensitivt(con, kid) is None and _antall(con, "sensitivt") == 0


def test_sensitive_felt_paa_fysisk_kurs_lagres_som_foer(con):
    kid = _kurs(con, kode="ME")
    assert _post("ME", allergier="Eksempelallergi", tilrettelegging="Eksempelbehov").status_code == 200
    s = _sensitivt(con, kid)
    assert (s["allergier"], s["tilrettelegging"]) == ("Eksempelallergi", "Eksempelbehov")


MANIPULERT_FAKTURA = {"betaler": "organisasjon", "org_navn": "MANIP Org", "org_nr": "999999999",
                      "faktura_ref": "MANIP-REF", "faktura_epost": "manip@eksempel.no", "ehf": "on",
                      "betaling": "per_samling", "faktura_adresse": "Manipveien 1", "faktura_postnr": "9999",
                      "faktura_sted": "Manipby"}


@pytest.mark.parametrize("oppsett", [dict(fakturering="organisasjon"), dict(fakturering="ingen"), dict(pris_nok=0),
                                     dict(pris_nok=0, betaling="deltaker_velger"),
                                     dict(fakturering="organisasjon", betaling="deltaker_velger")])
def test_f_hele_fakturablokken_ignoreres_naar_den_ikke_vises(con, oppsett):
    kid = _kurs(con, kode="MF", **oppsett)
    r = _post("MF", **MANIPULERT_FAKTURA)
    assert r.status_code == 200                                     # ingen krav om org.nr. utloeses
    p = _paamelding(con, kid)
    assert (p["betaler"], p["ehf"], p["betaling"]) == ("person", 0, "samlet")
    for k in ("org_navn", "org_nr", "faktura_ref", "faktura_epost", "faktura_adresse", "faktura_postnr", "faktura_sted"):
        assert p[k] is None, k
    assert _antall(con, "faktura") == 0 and _antall(con, "faktura_forsok") == 0


def test_f_betaler_organisasjon_uten_org_nr_utloeser_ikke_krav_naar_blokken_er_skjult(con):
    _kurs(con, kode="MF2", type="digital", sted=None, fakturering="organisasjon")
    r = _post("MF2", betaler="organisasjon", org_nr="")
    assert r.status_code == 200 and "Fyll inn organisasjon" not in r.get_data(as_text=True)


def test_synlig_fakturablokk_har_uendret_validering_og_lagring(con):
    kid = _kurs(con, kode="FV", betaling="deltaker_velger")
    r = _post("FV", betaler="organisasjon", org_navn="Eksempel Org", org_nr="")
    assert r.status_code == 400 and "Fyll inn organisasjon og org.nr. når arbeidsgiver betaler." in r.get_data(as_text=True)
    assert _paamelding(con, kid) is None
    assert _post("FV", **{**MANIPULERT_FAKTURA, "org_navn": "Eksempel Org"}).status_code == 200
    p = _paamelding(con, kid)
    assert (p["betaler"], p["org_navn"], p["org_nr"], p["ehf"], p["betaling"], p["faktura_ref"], p["faktura_sted"]) == (
        "organisasjon", "Eksempel Org", "999999999", 1, "per_samling", "MANIP-REF", "Manipby")


def test_synlig_fakturablokk_person_betaler(con):
    kid = _kurs(con, kode="FP")
    assert _post("FP", betaler="person", faktura_adresse="Eksempelveien 1").status_code == 200
    p = _paamelding(con, kid)
    assert (p["betaler"], p["faktura_adresse"], p["org_navn"]) == ("person", "Eksempelveien 1", None)


def test_ukjente_ekstra_postfelt_ignoreres(con):
    kid = _kurs(con, kode="UK")
    assert _post("UK", status="avmeldt", kilde="admin", sveiper_utsatt="1", yrkestittel="MANIP",
                 intern_kommentar="MANIP").status_code == 200
    p = _paamelding(con, kid)
    assert (p["status"], p["kilde"], p["intern_kommentar"]) == ("bekreftet", "skjema", None)
    assert _deltaker(con)["yrkestittel"] is None


# ============================ eksisterende deltaker: profil overskrives/slettes aldri ============================

@pytest.mark.parametrize("felt,skjul", [("telefon", {"telefon": {"synlig": False}}),
                                        ("arbeidssted", {"arbeidssted": {"synlig": False}}),
                                        ("hpr_nr", {})])   # HPR skjules av manglende spesialistlop
def test_skjult_felt_overskriver_eller_sletter_ikke_eksisterende_profilverdi(con, felt, skjul):
    db.finn_eller_opprett_deltaker(con, EPOST, "Test", "Person", **{felt: "GAMMEL-VERDI"})
    con.commit()
    kid = _kurs(con, kode="EX")
    for f, egenskaper in skjul.items():
        _lagre(con, kid, f, **egenskaper)
    assert _post("EX", **{felt: "NY-MANIPULERT"}).status_code == 200
    assert _paamelding(con, kid) is not None
    assert _deltaker(con)[felt] == "GAMMEL-VERDI"
    # ... og heller ikke naar feltet sendes tomt
    kid2 = _kurs(con, kode="EX2")
    for f, egenskaper in skjul.items():
        _lagre(con, kid2, f, **egenskaper)
    assert _post("EX2", **{felt: ""}).status_code == 200
    assert _deltaker(con)[felt] == "GAMMEL-VERDI"


def test_synlig_felt_oppdaterer_eksisterende_profil_som_foer(con):
    db.finn_eller_opprett_deltaker(con, EPOST, "Test", "Person", telefon="GAMMEL", hpr_nr="GAMMEL")
    con.commit()
    _kurs(con, kode="EXS", spesialistlop="EFT")
    assert _post("EXS", telefon="NY", hpr_nr="NY").status_code == 200
    d = _deltaker(con)
    assert (d["telefon"], d["hpr_nr"]) == ("NY", "NY")


# ============================ DB-lesefeil: fail closed ============================

def _bryt_lesing(con):
    con.execute("DROP TABLE kurs_skjemafelt")       # ekte sqlite3.OperationalError ved lesing -> SkjemaLesefeil
    con.commit()


def _sjekk_feilside(r, kursnavn="Eksempelkurs"):
    html = r.get_data(as_text=True)
    assert r.status_code == 503
    assert "midlertidig utilgjengelig" in html and kursnavn in html
    for lekkasje in ("kurs_skjemafelt", "no such table", "Traceback", "sqlite", "OperationalError", "SkjemaLesefeil",
                     "SELECT", str(config.DB_STI), "test.db"):
        assert lekkasje not in html, lekkasje
    assert 'name="fornavn"' not in html and 'name="telefon"' not in html  # ALDRI standardskjema som fallback
    return html


def test_get_ved_lesefeil_gir_kontrollert_503(con):
    _kurs(con, kode="LF")
    _bryt_lesing(con)
    html = _sjekk_feilside(_klient().get("/kurs/LF"))
    assert "Ingen påmelding er registrert." in html


def test_forhaandsvisning_ved_lesefeil_gir_kontrollert_503(con):
    kid = _kurs(con, kode="LFP")
    _bryt_lesing(con)
    html = _sjekk_feilside(_admin().get(f"/admin/kurs/{kid}/forhandsvis-paamelding"))
    assert "forhåndsvisningen kan ikke vises" in html


def test_post_ved_lesefeil_gir_503_uten_noen_sideeffekter(con, monkeypatch):
    kid = _kurs(con, kode="LFS", spesialistlop="EFT")
    _bryt_lesing(con)
    kall = []
    monkeypatch.setattr(db, "meld_paa", lambda *a, **kw: kall.append("meld_paa"))
    monkeypatch.setattr(sveiper, "kjor", lambda *a, **kw: kall.append("sveiper"))
    monkeypatch.setattr(Kjoring, "send_en_gang", lambda *a, **kw: kall.append("epost"))
    monkeypatch.setattr(db, "finn_eller_opprett_deltaker", lambda *a, **kw: kall.append("deltaker"))
    tabeller = ("deltaker", "paamelding", "sensitivt", "utsending_logg", "faktura", "faktura_forsok", "hendelse",
                "firmapaamelding", "innlogging_token")
    foer = {t: _antall(con, t) for t in tabeller}
    r = _post("LFS", telefon="123", arbeidssted="X", hpr_nr="1", allergier="A", tilrettelegging="T",
              **MANIPULERT_FAKTURA)
    _sjekk_feilside(r)
    assert kall == []
    assert {t: _antall(con, t) for t in tabeller} == foer


def test_lesefeil_er_ikke_permanent(con):
    _kurs(con, kode="LFR")
    _bryt_lesing(con)
    assert _klient().get("/kurs/LFR").status_code == 503
    db.init(con)                                     # tabellen gjenopprettes
    assert _klient().get("/kurs/LFR").status_code == 200


def test_ikke_offentlig_kurs_gir_fortsatt_404_og_leser_ikke_skjemaet(con, monkeypatch):
    _kurs(con, kode="UT", status="utkast")
    kall = []
    monkeypatch.setattr(db, "hent_skjemaoverstyringer", lambda *a: kall.append(1))
    assert _klient().get("/kurs/UT").status_code == 404
    assert _klient().post("/kurs/UT", data=BASIS).status_code == 404
    assert kall == []


# ============================ eksakt EN lesing per request ============================

@pytest.fixture
def teller(monkeypatch):
    ekte = db.hent_skjemaoverstyringer
    kall = []

    def telle(c, kurs_id):
        kall.append(kurs_id)
        return ekte(c, kurs_id)
    monkeypatch.setattr(db, "hent_skjemaoverstyringer", telle)
    return kall


def test_noyaktig_en_lesing_per_request(con, teller):
    kid = _kurs(con, kode="EN", spesialistlop="EFT")
    _lagre(con, kid, "telefon", obligatorisk=True, hjelpetekst="H")
    admin = _admin()
    tilfeller = [
        ("GET", lambda: _klient().get("/kurs/EN"), 200),
        ("POST valideringsfeil", lambda: _post("EN", telefon=""), 400),
        ("POST suksess", lambda: _post("EN", telefon="123"), 200),
        ("POST Paameldingsfeil (allerede paameldt)", lambda: _post("EN", telefon="123"), 400),
        ("forhaandsvisning", lambda: admin.get(f"/admin/kurs/{kid}/forhandsvis-paamelding"), 200),
    ]
    for navn, kall, status in tilfeller:
        teller.clear()
        assert kall().status_code == status, navn
        assert teller == [kid], navn


def test_ved_lesefeil_ett_forsok_deretter_kontrollert_feil(con, monkeypatch):
    kid = _kurs(con, kode="EF")
    kall = []

    def feiler(c, kurs_id):
        kall.append(kurs_id)
        raise sf.SkjemaLesefeil(kurs_id)
    monkeypatch.setattr(db, "hent_skjemaoverstyringer", feiler)
    for r in (_klient().get("/kurs/EF"), _post("EF"), _admin().get(f"/admin/kurs/{kid}/forhandsvis-paamelding")):
        assert r.status_code == 503
    assert kall == [kid, kid, kid]
    assert _antall(con, "paamelding") == 0


# ============================ TOCTOU: POST fullfoeres etter sitt eget snapshot ============================

@pytest.fixture
def endre_etter_lesing(monkeypatch):
    """Etter FOERSTE lesing i neste request endres databasen (via en egen tilkobling) til override-sett B. Requesten har
    allerede lest A og skal fullfores etter A. Senere requester leser B."""
    ekte = db.hent_skjemaoverstyringer
    tilstand = {"b": None}

    def les_og_endre(c, kurs_id):
        res = ekte(c, kurs_id)
        if tilstand["b"]:
            annen = db.koble(config.DB_STI)
            db.lagre_skjemafelt(annen, kurs_id, "telefon", tilstand.pop("b"))
            annen.commit()
            annen.close()
            tilstand["b"] = None
        return res
    monkeypatch.setattr(db, "hent_skjemaoverstyringer", les_og_endre)
    return tilstand


def test_toctou_obligatorisk_i_a_skjult_i_b_gir_fortsatt_400(con, endre_etter_lesing):
    kid = _kurs(con, kode="TA")
    _lagre(con, kid, "telefon", obligatorisk=True)                       # A: synlig + obligatorisk
    endre_etter_lesing["b"] = {"synlig": False}                         # B: skjult
    r = _post("TA", telefon="")
    html = r.get_data(as_text=True)
    assert r.status_code == 400 and "Fyll inn «Telefon»." in html        # validert etter A
    assert "required" in _attrs(html, "telefon")                          # re-rendret etter A
    assert _paamelding(con, kid) is None
    assert db.hent_skjemaoverstyringer(con, kid).overstyringer["telefon"] == sf.Overstyring(synlig=False)
    assert "telefon" not in _navn(_klient().get("/kurs/TA").get_data(as_text=True))   # neste request: B
    assert _post("TA", telefon="").status_code == 200


def test_toctou_skjult_i_a_synlig_i_b_ignorerer_fortsatt_verdien(con, endre_etter_lesing):
    kid = _kurs(con, kode="TB")
    _lagre(con, kid, "telefon", synlig=False)                           # A: skjult
    endre_etter_lesing["b"] = {"synlig": True, "label": "Mobil"}        # B: synlig
    assert _post("TB", telefon="MANIPULERT").status_code == 200
    assert _deltaker(con)["telefon"] is None                              # lagret etter A
    html = _klient().get("/kurs/TB").get_data(as_text=True)              # neste request: B
    assert '<label for="f-telefon">Mobil</label>' in html


# ============================ korrupte overstyringer / advarsler ============================

def _korrupt(con, kid):
    con.execute("INSERT INTO kurs_skjemafelt (kurs_id, felt, label) VALUES (?, 'HEMMELIG_FELTNAVN', 'HEMMELIG-LABEL')",
                (kid,))
    con.execute("INSERT INTO kurs_skjemafelt (kurs_id, felt, synlig, label, hjelpetekst) "
                "VALUES (?, 'hpr_nr', 0, 'HEMMELIG-HPR-LABEL', 'Gyldig HPR-hjelp')", (kid,))
    con.execute("INSERT INTO kurs_skjemafelt (kurs_id, felt, obligatorisk, label) VALUES (?, 'telefon', 1, ?)",
                (kid, "HEMMELIG" + "x" * 100))
    con.commit()


def _skjemaadvarsler(con):
    return [json.loads(r["detaljer"]) for r in
            con.execute("SELECT detaljer FROM hendelse WHERE handling='skjemafelt_advarsel' ORDER BY id")]


def test_korrupt_overstyring_gir_fungerende_side_uten_logging_ved_visning(con):
    kid = _kurs(con, kode="KO", spesialistlop="EFT")
    _korrupt(con, kid)
    admin = _admin()                                 # innlogging logges (admin_innlogget) - telles foer visningene
    foer = _antall(con, "hendelse")
    for r in (_klient().get("/kurs/KO"), admin.get(f"/admin/kurs/{kid}/forhandsvis-paamelding")):
        html = r.get_data(as_text=True)
        assert r.status_code == 200
        assert _deltakerfelt(html) == ["telefon", "arbeidssted", "hpr_nr"]    # HPR kan ikke skjules
        assert '<label for="f-telefon">Telefon *</label>' in html                              # gyldig del av raden brukes
        assert "Gyldig HPR-hjelp" in html and '<label for="f-hpr_nr">HPR-nummer</label>' in html
        assert "HEMMELIG" not in html
    assert _antall(con, "hendelse") == foer                                     # ingen skriving ved visning


def test_korrupt_overstyring_post_registrerer_og_logger_pii_fritt(con):
    kid = _kurs(con, kode="KP", spesialistlop="EFT")
    _korrupt(con, kid)
    assert _post("KP", telefon="").status_code == 400                   # obligatorisk (gyldig del) haandheves
    assert _skjemaadvarsler(con) == []                                   # ingen registrering -> ingen logg
    assert _post("KP", telefon="123").status_code == 200
    logg = _skjemaadvarsler(con)
    assert len(logg) == 1 and logg[0]["kurs_id"] == kid
    assert sorted(logg[0]["advarsler"], key=json.dumps) == sorted([
        {"felt": None, "egenskap": None, "grunn": sf.UKJENT_FELT},
        {"felt": "hpr_nr", "egenskap": "synlig", "grunn": sf.IKKE_TILLATT},
        {"felt": "hpr_nr", "egenskap": "label", "grunn": sf.IKKE_TILLATT},
        {"felt": "telefon", "egenskap": "label", "grunn": sf.FOR_LANG}], key=json.dumps)
    alt = " ".join(r["detaljer"] or "" for r in con.execute("SELECT detaljer FROM hendelse"))
    assert "HEMMELIG" not in alt and "Gyldig HPR-hjelp" not in alt


def test_ingen_advarselslogg_uten_advarsler(con):
    kid = _kurs(con, kode="IA")
    _lagre(con, kid, "telefon", label="Mobil")
    assert _post("IA").status_code == 200
    assert _skjemaadvarsler(con) == []


def test_advarsel_logges_ikke_naar_meld_paa_feiler(con):
    kid = _kurs(con, kode="MPF")
    _korrupt(con, kid)
    assert _post("MPF", telefon="1").status_code == 200
    antall = len(_skjemaadvarsler(con))
    assert _post("MPF", telefon="1").status_code == 400                  # allerede paameldt -> rollback
    assert len(_skjemaadvarsler(con)) == antall
