"""Deltakerens PRIVATE adresse (adresse, postnr, poststed) er ALLTID påkrevd, for ALLE deltakere, i ALLE veier inn.

Brukerens beslutning 29.09.2026: ikke alle deltakere får fakturaen betalt av arbeidsgiver, og da MÅ kursadministrasjonen
kunne sende fakturaen direkte til deltakerens adresse. Adressen kreves derfor også når arbeidsgiver betaler og på
gratiskurs. Låste krav som testes her:
  * adresse/postnr/poststed er LÅSTE standardfelt (alltid synlige og krav, kan ikke skjules eller gjøres valgfrie), rett
    etter e-post i skjemaet. De gamle private fakturafeltene (faktura_adresse/-postnr/-sted) er avviklet, og lagrede
    overstyringer for dem leses uten feil og uten advarsel.
  * Alle veier inn krever adressen: skjemaet, bedriftspåmelding (per deltaker), manuell registrering, CSV-import,
    webhooken og redigering i deltakervinduet. Mangler den, avvises innsendingen og ingenting registreres.
  * Adressen lagres på PERSONEN. Ny adresse ved ny påmelding overskriver den gamle; tom overskriver aldri.
  * Betaler deltakeren selv: personens adresse kopieres til fakturaadressen. Betaler firma: INGEN kopi - fakturaen går til
    firmaets adresse fra Enhetsregisteret, også når registeret ikke svarer (aldri automatisk over til privat adresse).
  * Personvern: aldri i hendelseslogg, e-post, CSV-eksport eller deltakerlisten; tømmes ved anonymisering.
  * Migrering 14 (deltaker_adresse) virker på tom og på eksisterende database.
Alle testdata er fiktive."""
import ast
import csv
import io
import json
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from adressehjelp import ADRESSE, ANNEN_ADRESSE, SPORBAR_ADRESSE, gruppeadresse
from kurs import config, daglig, db, hendelseslogg, migreringer, sveiper
from kurs import import_deltakere as imp
from kurs import skjemafelt as sf
from kurs.integrasjoner import brreg, visma
from kurs.kjoring import Kjoring
from navnehjelp import gruppeskjema

pytestmark = pytest.mark.uten_standardadresse   # registrerer bevisst uten adresse (se conftest.py)

ROT = Path(__file__).resolve().parent.parent
EPOST = "test.person@eksempel.no"
BASIS = {"fornavn": "Test", "etternavn": "Person", "epost": EPOST, "samtykke": "on"}
FIRMA = "999900003"          # EKSEMPEL KOMMUNE, Postboks 100, 1234 EKSEMPELBY (oppdiktet)
FIRMA_ADRESSE = ("Postboks 100", "1234", "EKSEMPELBY")
NEDE = brreg.DEMO_NEDE_ORGNR


@pytest.fixture(autouse=True)
def _demo(monkeypatch):
    monkeypatch.setattr(config, "DEMO", True)


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    monkeypatch.setattr(config, "BASE_URL", "https://kurs.eksempel.no")
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


def _kurs(con, kode="A1", frem=30, **kw):
    start = date.today() + timedelta(days=frem)
    felter = {"navn": "Eksempelkurs", "type": "fysisk", "sted": "Eksempelsted", "pris_nok": 4500,
              "fakturering": "person", "betaling": "samlet", "kapasitet": 10, **kw}
    kid = db.opprett_kurs(con, kode=kode, datoer=[start.isoformat(), (start + timedelta(days=1)).isoformat()],
                          sharepoint_mappe=f"K/{kode}", **felter)
    con.commit()
    return kid


def _post(kode="A1", **data):
    return _klient().post(f"/kurs/{kode}", data={**BASIS, "betaler": "person", **data})


def _person(con, epost=EPOST):
    return con.execute("SELECT * FROM deltaker WHERE epost=?", (epost,)).fetchone()


def _paamelding(con, epost=EPOST, kode="A1"):
    return con.execute(
        """SELECT p.* FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id JOIN kurs k ON k.id=p.kurs_id
           WHERE d.epost=? AND k.kode=?""", (epost, kode)).fetchone()


def _antall(con, tabell="deltaker"):
    return con.execute(f"SELECT COUNT(*) FROM {tabell}").fetchone()[0]


def _adresse_paa(rad, prefiks="") -> dict:
    return {"adresse": rad[f"{prefiks}adresse"], "postnr": rad[f"{prefiks}postnr"], "poststed": rad[f"{prefiks}poststed"]}


def _faktura_adresse(p) -> dict:
    return {"adresse": p["faktura_adresse"], "postnr": p["faktura_postnr"], "poststed": p["faktura_sted"]}


def _tekst(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def _hendelser_tekst(con) -> str:
    return " ".join(r["detaljer"] or "" for r in con.execute("SELECT detaljer FROM hendelse"))


def _sendt_tekst(con) -> str:
    """ALT som er sendt på e-post (filene i utboksen og kopiene i e-posthistorikken)."""
    filer = " ".join(f.read_text(encoding="utf-8") for f in sorted(config.UTBOKS.glob("*.html"))) \
        if config.UTBOKS.exists() else ""
    kopier = " ".join((r["html"] or "") + (r["emne"] or "") for r in con.execute("SELECT html, emne FROM sendt_epost"))
    return filer + " " + kopier


# ============================ registeret: låste felt, avviklede felt ============================

def test_adressefeltene_er_laaste_synlige_og_obligatoriske():
    assert sf.ADRESSEFELT == ("adresse", "postnr", "poststed")
    for n in sf.ADRESSEFELT:
        f = sf.REGISTER[n]
        assert (f.kategori, f.synlig, f.obligatorisk, f.overstyrbart) == (sf.LAAST, True, True, frozenset())
    assert [sf.REGISTER[n].label for n in sf.ADRESSEFELT] == ["Adresse", "Postnummer", "Poststed"]
    assert sf.REGISTER["postnr"].hjelpetekst == "4 siffer i Norge."


@pytest.mark.parametrize("felt", sf.ADRESSEFELT)
def test_adressefeltene_kan_ikke_skjules_eller_gjoeres_valgfrie(con, felt):
    for egenskaper in ({"synlig": False}, {"obligatorisk": False}, {"label": "Noe annet"}, {"rekkefolge": 0}):
        with pytest.raises(sf.SkjemafeltFeil) as e:
            sf.normaliser_overstyring(felt, egenskaper)
        assert e.value.grunn == sf.LAAST_FELT
    kid = _kurs(con)
    with pytest.raises(sf.SkjemafeltFeil):
        db.lagre_skjemafelt(con, kid, felt, {"synlig": False})
    assert _antall(con, "kurs_skjemafelt") == 0


@pytest.mark.parametrize("kw", [
    {}, {"pris_nok": 0, "fakturering": "ingen"}, {"type": "digital"}, {"fakturering": "organisasjon"},
    {"spesialistlop": "EFT"}])
def test_alle_kurs_faar_adressefeltene_rett_etter_epost(con, kw):
    kid = _kurs(con, **kw)
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    skjema = sf.effektivt_skjema(kurs)
    assert [(f.nokkel, f.label, f.obligatorisk) for f in skjema.adressefelt] == [
        ("adresse", "Adresse", True), ("postnr", "Postnummer", True), ("poststed", "Poststed", True)]
    assert not hasattr(skjema, "privatfelt")                      # de private fakturafeltene finnes ikke lenger
    html = _klient().get("/kurs/A1").get_data(as_text=True)
    navn = [m for m in re.findall(r'<input id="f-(\w+)"', html)]
    assert navn[:6] == ["fornavn", "etternavn", "epost", "adresse", "postnr", "poststed"]


def test_en_laast_rad_i_databasen_gjor_ikke_feltet_valgfritt(con):
    kid = _kurs(con)
    con.execute("INSERT INTO kurs_skjemafelt (kurs_id, felt, synlig, obligatorisk) VALUES (?, 'adresse', 0, 0)", (kid,))
    con.commit()
    les = db.hent_skjemaoverstyringer(con, kid)
    assert [(a.grunn, a.felt) for a in les.advarsler] == [(sf.LAAST_FELT, "adresse")]
    r = _post(**{k: v for k, v in ADRESSE.items() if k != "adresse"})            # uten adresse: fortsatt avvist
    assert r.status_code == 400 and _person(con) is None


def test_gamle_overstyringer_for_de_private_fakturafeltene_leses_uten_advarsel(con):
    kid = _kurs(con)
    for felt in ("faktura_adresse", "faktura_postnr", "faktura_sted"):
        con.execute("INSERT INTO kurs_skjemafelt (kurs_id, felt, synlig, obligatorisk, label) VALUES (?,?,0,1,'Gammel')",
                    (kid, felt))
    con.commit()
    assert set(sf.AVVIKLEDE_FELT) == {"faktura_adresse", "faktura_postnr", "faktura_sted", "vis_plasser"}
    les = db.hent_skjemaoverstyringer(con, kid)
    assert les.advarsler == () and dict(les.overstyringer) == {}                # ingen feil, ingen advarsel, ingen effekt
    # Hele veien: skjemaet vises, kan sendes inn, og skjemabyggeren viser ingen advarsel om raden
    assert _klient().get("/kurs/A1").status_code == 200
    assert _post(**ADRESSE).status_code == 200
    html = _admin().get(f"/admin/kurs/{kid}/paameldingsskjema").get_data(as_text=True)
    assert "Noen lagrede skjemainnstillinger kunne ikke brukes" not in html
    # Og et ukjent felt er fortsatt en advarsel (kun de avviklede er unntatt)
    con.execute("INSERT INTO kurs_skjemafelt (kurs_id, felt, synlig) VALUES (?, 'finnes_ikke', 0)", (kid,))
    con.commit()
    assert [a.grunn for a in db.hent_skjemaoverstyringer(con, kid).advarsler] == [sf.UKJENT_FELT]


def test_de_avviklede_feltene_kan_ikke_lenger_skrives(con):
    kid = _kurs(con)
    for felt in sf.AVVIKLEDE_FELT:
        assert felt not in sf.REGISTER and felt not in sf.FAKTURAFELT
        with pytest.raises(sf.SkjemafeltFeil) as e:
            db.lagre_skjemafelt(con, kid, felt, {"synlig": False})
        assert e.value.grunn == sf.UKJENT_FELT


# ============================ valideringen (én regel for alle veier) ============================

def test_valider_adresse_krever_alle_tre_og_lar_utenlandske_adresser_passere():
    assert sf.valider_adresse(ADRESSE) == {}
    assert sf.valider_adresse({"adresse": "Storgatan 1", "postnr": "114 55", "poststed": "Stockholm"}) == {}   # Sverige
    assert sf.valider_adresse({"adresse": "1 Example Road", "postnr": "SW1A 1AA", "poststed": "London"}) == {}
    assert sf.valider_adresse({}) == {"adresse": "Fyll inn «Adresse».", "postnr": "Fyll inn «Postnummer».",
                                     "poststed": "Fyll inn «Poststed»."}
    assert sf.valider_adresse({**ADRESSE, "adresse": "   ", "poststed": None}) == {
        "adresse": "Fyll inn «Adresse».", "poststed": "Fyll inn «Poststed»."}
    assert sf.valider_adresse({**ADRESSE, "postnr": "x" * (sf.MAKS_POSTNR + 1)}) == {
        "postnr": f"«Postnummer» kan være maks {sf.MAKS_POSTNR} tegn."}
    assert sf.valider_adresse({**ADRESSE, "adresse": "Linje 1\nLinje 2"}) == {"adresse": "«Adresse» inneholder ugyldige tegn."}
    assert sf.valider_adresse({"adresse": ""}, felt=("postnr",)) == {"postnr": "Fyll inn «Postnummer»."}   # bare valgte felt


def test_valideringsmeldingene_inneholder_aldri_verdien():
    for verdi in ("Hemmeligveien 9\n" + "x" * 300,):
        for m in sf.valider_adresse({"adresse": verdi, "postnr": verdi, "poststed": verdi}).values():
            assert "Hemmeligveien" not in m


def test_rens_adresse_trimmer_og_gir_none_for_tomt():
    assert sf.rens_adresse({"adresse": "  Vei 1 ", "postnr": "", "poststed": None, "telefon": "1"}) == {
        "adresse": "Vei 1", "postnr": None, "poststed": None}
    assert sf.rens_adresse({}) == {"adresse": None, "postnr": None, "poststed": None}


# ============================ det offentlige skjemaet ============================

def test_skjemaet_viser_adressefeltene_som_krav_med_riktige_attributter(con):
    _kurs(con)
    html = _klient().get("/kurs/A1").get_data(as_text=True)
    for navn, autocomplete in (("adresse", "street-address"), ("postnr", "postal-code"), ("poststed", "address-level2")):
        tag = re.search(rf'<input id="f-{navn}"[^>]*>', html).group(0)
        assert f'name="{navn}"' in tag and f'autocomplete="{autocomplete}"' in tag and " required" in tag, tag
    tekst = _tekst(html)
    assert "Adresse (krav)" in tekst and "Postnummer (krav)" in tekst and "Poststed (krav)" in tekst
    assert "4 siffer i Norge." in tekst
    assert "sendes fakturaen til adressen du har oppgitt over" in tekst


def test_privat_betaler_har_ikke_lenger_egne_adressefelt_i_fakturadelen(con):
    _kurs(con)
    html = _klient().get("/kurs/A1").get_data(as_text=True)
    for gammelt in ("faktura_adresse", "faktura_postnr", "faktura_sted"):
        assert gammelt not in html
    assert html.count('name="adresse"') == 1 and html.count('name="postnr"') == 1 and html.count('name="poststed"') == 1


@pytest.mark.parametrize("mangler", sf.ADRESSEFELT)
def test_skjemaet_avviser_manglende_adresse_med_norsk_feilmelding(con, mangler):
    _kurs(con)
    label = sf.REGISTER[mangler].label
    r = _post(**{k: v for k, v in ADRESSE.items() if k != mangler})
    assert r.status_code == 400
    assert f"Fyll inn «{label}»." in r.get_data(as_text=True)
    assert _person(con) is None and _antall(con, "paamelding") == 0


def test_skjemaet_avviser_tom_adresse_med_bare_mellomrom_og_beholder_det_som_er_skrevet(con):
    _kurs(con)
    r = _post(adresse="    ", postnr="0150", poststed="Oslo")
    html = r.get_data(as_text=True)
    assert r.status_code == 400 and "Fyll inn «Adresse»." in html
    assert 'value="0150"' in html and 'value="Oslo"' in html            # de andre verdiene står igjen i skjemaet
    assert _antall(con) == 0


def test_skjemaet_avviser_for_lang_adresse(con):
    _kurs(con)
    r = _post(**{**ADRESSE, "adresse": "A" * (sf.MAKS_ADRESSE + 1)})
    assert r.status_code == 400 and f"maks {sf.MAKS_ADRESSE} tegn" in r.get_data(as_text=True)
    assert _antall(con) == 0


@pytest.mark.parametrize("kw, data", [
    ({}, {"betaler": "person"}),
    ({}, {"betaler": "organisasjon", "org_nr": FIRMA}),
    ({"pris_nok": 0, "fakturering": "ingen"}, {}),                       # gratis kurs, ingen fakturablokk
    ({"fakturering": "organisasjon"}, {}),                                # kurs uten faktura til deltakeren
    ({"type": "digital"}, {"betaler": "person"}),
])
def test_adressen_kreves_paa_alle_kurs_og_uansett_hvem_betaler(con, kw, data):
    _kurs(con, **kw)
    r = _klient().post("/kurs/A1", data={**{k: v for k, v in BASIS.items()}, **data})
    assert r.status_code == 400 and "Fyll inn «Adresse»." in r.get_data(as_text=True)
    assert _antall(con) == 0
    r = _klient().post("/kurs/A1", data={**BASIS, **data, **ADRESSE})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert _adresse_paa(_person(con)) == ADRESSE


def test_adressen_lagres_paa_personen_ikke_i_faktura_for_firma(con):
    _kurs(con)
    assert _post(**ADRESSE, betaler="organisasjon", org_nr=FIRMA).status_code == 200
    assert _adresse_paa(_person(con)) == ADRESSE
    p = _paamelding(con)
    assert p["betaler"] == "organisasjon"
    assert _faktura_adresse(p) == dict(zip(("adresse", "postnr", "poststed"), FIRMA_ADRESSE))   # firmaets, ikke deltakerens
    assert ADRESSE["adresse"] not in " ".join(str(v) for v in dict(p).values())


def test_privat_betaler_faar_personens_adresse_som_fakturaadresse(con):
    _kurs(con)
    assert _post(**ADRESSE, betaler="person").status_code == 200
    p = _paamelding(con)
    assert p["betaler"] == "person" and _faktura_adresse(p) == ADRESSE == _adresse_paa(_person(con))


def test_firma_som_ikke_svarer_faar_aldri_privat_adresse_som_fakturaadresse(con):
    """Registeret er nede: firmaopplysningene mangler («Firmaopplysninger må kontrolleres»). Systemet går IKKE over til å
    fakturere deltakerens private adresse."""
    _kurs(con)
    assert _post(**ADRESSE, betaler="organisasjon", org_nr=NEDE).status_code == 200
    p = _paamelding(con)
    assert _faktura_adresse(p) == {"adresse": None, "postnr": None, "poststed": None}
    assert _adresse_paa(_person(con)) == ADRESSE
    # ... og fakturaen venter (ingen faktura uten firmanavn)
    assert _antall(con, "faktura") == 0 and _antall(con, "faktura_forsok") == 0


def test_fakturagrunnlaget_til_visma_bruker_riktig_adresse(con, monkeypatch):
    """Fakturamotoren er uendret og leser paamelding.faktura_*: deltakerens adresse for privat betaler, firmaets for firma."""
    grunnlag = []

    def fake(g):
        grunnlag.append(g)
        return visma.Fakturaresultat(visma_id=f"v{len(grunnlag)}", faktura_nr=f"F{len(grunnlag)}", kunde_id="k")
    monkeypatch.setattr(visma, "fakturer", fake)
    _kurs(con)
    assert _post(**ADRESSE, betaler="person").status_code == 200
    assert _post(**ANNEN_ADRESSE, epost="firma.person@eksempel.no", betaler="organisasjon", org_nr=FIRMA).status_code == 200
    privat, firma = grunnlag
    assert privat.er_privatperson and (privat.adresse, privat.postnr, privat.sted) == (
        ADRESSE["adresse"], ADRESSE["postnr"], ADRESSE["poststed"])
    assert not firma.er_privatperson and (firma.adresse, firma.postnr, firma.sted) == FIRMA_ADRESSE


def test_ny_paamelding_med_ny_adresse_overskriver_den_gamle(con):
    _kurs(con, "A1")
    _kurs(con, "A2")
    assert _post("A1", **ADRESSE).status_code == 200
    assert _post("A2", **ANNEN_ADRESSE).status_code == 200
    assert _adresse_paa(_person(con)) == ANNEN_ADRESSE and _antall(con) == 1        # samme person, nyeste adresse
    assert _faktura_adresse(_paamelding(con, kode="A1")) == ADRESSE      # påmeldingen på det første kurset er som den var
    assert _faktura_adresse(_paamelding(con, kode="A2")) == ANNEN_ADRESSE


def test_tom_adresse_overskriver_aldri_en_eksisterende(con):
    """På db-nivå: en tom (eller manglende) verdi endrer aldri adressen som står der fra før."""
    kid = _kurs(con)
    db.meld_paa(con, kid, epost=EPOST, fornavn="Test", etternavn="Person", deltaker=dict(ADRESSE))
    kid2 = _kurs(con, "A2")
    db.meld_paa(con, kid2, epost=EPOST, fornavn="Test", etternavn="Person",
                deltaker={"adresse": "", "postnr": None, "poststed": ""})
    assert _adresse_paa(_person(con)) == ADRESSE
    db.meld_paa(con, _kurs(con, "A3"), epost=EPOST, fornavn="Test", etternavn="Person", deltaker={"telefon": "99999999"})
    assert _adresse_paa(_person(con)) == ADRESSE                  # heller ikke når adressen ikke er nevnt


def test_meld_paa_kopierer_bare_for_privat_betaler_og_bare_uten_egen_fakturaadresse(con):
    kid = _kurs(con)
    # privat (også uten at betaler er oppgitt): kopi
    db.meld_paa(con, kid, epost="a@eksempel.no", fornavn="A", etternavn="Test", deltaker=dict(ADRESSE))
    assert _faktura_adresse(_paamelding(con, "a@eksempel.no")) == ADRESSE
    # privat med egen fakturaadresse fra kalleren: kalleren vinner
    db.meld_paa(con, kid, epost="b@eksempel.no", fornavn="B", etternavn="Test", deltaker=dict(ADRESSE),
                paamelding={"faktura_adresse": "Egen 5", "faktura_postnr": "9999", "faktura_sted": "Egenby"})
    assert _faktura_adresse(_paamelding(con, "b@eksempel.no")) == {"adresse": "Egen 5", "postnr": "9999", "poststed": "Egenby"}
    # firma: ingen kopi, heller ikke når firmaadressen er tom
    db.meld_paa(con, kid, epost="c@eksempel.no", fornavn="C", etternavn="Test", deltaker=dict(ADRESSE),
                paamelding={"betaler": "organisasjon", "org_nr": NEDE})
    assert _faktura_adresse(_paamelding(con, "c@eksempel.no")) == {"adresse": None, "postnr": None, "poststed": None}
    # uten adresse (eldre kall): ingenting å kopiere, og ingen feil
    db.meld_paa(con, kid, epost="d@eksempel.no", fornavn="D", etternavn="Test")
    assert _faktura_adresse(_paamelding(con, "d@eksempel.no")) == {"adresse": None, "postnr": None, "poststed": None}


def test_reaktivering_faar_personens_nyeste_adresse_som_fakturaadresse(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost=EPOST, fornavn="Test", etternavn="Person", deltaker=dict(ADRESSE))
    db.meld_av(con, pid)
    db.meld_paa(con, kid, epost=EPOST, fornavn="Test", etternavn="Person", deltaker=dict(ANNEN_ADRESSE))
    assert _faktura_adresse(_paamelding(con)) == ANNEN_ADRESSE


# ============================ grenser og kontrolltegn i alle tre feltene (kritikerrunden) ============================

@pytest.mark.parametrize("felt, maks", [("adresse", 200), ("postnr", 20), ("poststed", 100)])
def test_lengdegrensene_er_200_20_og_100_tegn_og_maks_er_lov(felt, maks):
    """Bokstavelige tall (ikke konstantene): en endret grense skal gi en rød test, ikke en stille endring."""
    assert sf.valider_adresse({**ADRESSE, felt: "x" * maks}) == {}
    assert list(sf.valider_adresse({**ADRESSE, felt: "x" * (maks + 1)})) == [felt]


@pytest.mark.parametrize("felt", sf.ADRESSEFELT)
@pytest.mark.parametrize("tegn", ["\n", "\r", "\t", "\x00"])
def test_kontrolltegn_avvises_i_alle_tre_feltene(felt, tegn):
    """Adressen havner i fakturafeltene i Visma: et linjeskift eller et kontrolltegn skal aldri slippe gjennom."""
    assert list(sf.valider_adresse({**ADRESSE, felt: f"ab{tegn}cd"})) == [felt]
