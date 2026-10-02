"""Adressekravet justert (brukerens beslutninger 30.09.2026):

  1. ELDRE DELTAKERE UTEN ADRESSE blokkeres ikke fra å lagre andre endringer i deltakervinduet. De er merket «Privat adresse mangler» (en
     varselboks øverst i Personopplysninger og et merke i kursets deltakerliste), og administrator fyller adressen inn når hun har den.
     Alle tre feltene tomme og ingen adresse fra før: lagres som vanlig. Noen fylt ut: alle tre kreves. En adresse som står der, kan
     ikke tømmes.
  2. NYE PÅMELDINGER: adressen er fortsatt alltid påkrevd i det offentlige skjemaet, bedriftspåmeldingen og «Legg til deltaker»
     (testes i test_adresse.py og test_adresse_veier.py).
  3. WEBHOOK OG CSV: kravet er en INNSTILLING (config.ADRESSE_KREVES_I_WEBHOOK og config.ADRESSE_KREVES_I_CSV). JUSTERT IGJEN 30.09.2026
     (brukerens ord: reglene krever fakturaadresse ved påmelding, så adressen MÅ være med og det skal ikke være mulig å utelate den):
     begge er PÅ som standard, og bare verdien «0» slår dem av. AV er en NØDBREMS (midlertidig) og testes her med fixturen `av`.
     JUSTERT 01.10.2026 (webhooken): påmeldingen skal ikke gå tapt. Den tas inn selv om adressen mangler (også delvis; en ufullstendig
     adresse lagres ikke), merkes «Privat adresse mangler», og fakturaen holdes tilbake til adressen er komplett (test_privat_adresse_faktura.py).
     CSV-nødbremsen er uendret: adressen er valgfri, men er noe fylt ut, kreves alle tre. PÅ (standard): som skjemaet, testes med
     innstillingen på i test_adresse_veier.py og test_adresse_paakrevd_overalt.py.

Alle testdata er fiktive."""
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from adressehjelp import ADRESSE, ANNEN_ADRESSE, SPORBAR_ADRESSE
from kurs import config, db, hendelseslogg, skjemafelt
from kurs import import_deltakere as imp
from test_adresse_veier import (EPOST, HEADER, INGEN, _admin, _adresse, _antall, _csv, _faktura, _importer, _klient,  # noqa: F401
                                _kurs, _last_opp, _lagre_person, _nett, _paamelding, _person, _webhook, con)

pytestmark = pytest.mark.uten_standardadresse   # registrerer bevisst uten adresse (se conftest.py)

ROT = Path(__file__).resolve().parent.parent
MERKE = "Privat adresse mangler"
MERKE_U = "Privat adresse er ufullstendig"


@pytest.fixture(autouse=True)
def _demo(monkeypatch):
    monkeypatch.setattr(config, "DEMO", True)


@pytest.fixture
def av(monkeypatch):
    """NØDBREMSEN: begge innstillingene AV (settes uttrykkelig, så standarden og en lokal .env ikke kan påvirke testen)."""
    monkeypatch.setattr(config, "ADRESSE_KREVES_I_WEBHOOK", False)
    monkeypatch.setattr(config, "ADRESSE_KREVES_I_CSV", False)


@pytest.fixture
def paa(monkeypatch):
    monkeypatch.setattr(config, "ADRESSE_KREVES_I_WEBHOOK", True)
    monkeypatch.setattr(config, "ADRESSE_KREVES_I_CSV", True)


def _liste(kid) -> str:
    return _admin().get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)


def _vindu(kid, pid) -> str:
    return _admin().get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)


def _personskjema(html: str) -> str:
    return html.split('id="skjema-person"')[1].split("</form>")[0]


def _uten_adresse(con, kode="A1", epost=EPOST, **kw):
    """En eldre person som mangler adressen (registrert før den ble påkrevd)."""
    kid = _kurs(con, kode)
    pid, _ = db.meld_paa(con, kid, epost=epost, fornavn="Ola", etternavn="Nordmann", **kw)
    con.commit()
    return kid, pid, f"/admin/kurs/{kid}/deltaker/{pid}"


# ============================ innstillingene ============================

def _les_innstillinger(**miljo) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ADRESSE_KREVES_")}
    env.update({"PYTHONUTF8": "1", **miljo})
    r = subprocess.run([sys.executable, "-c", "from kurs import config; print(config.ADRESSE_KREVES_I_WEBHOOK, config.ADRESSE_KREVES_I_CSV)"],
                       cwd=ROT, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_innstillingene_er_paa_som_standard_i_koden():
    kilde = (ROT / "kurs" / "config.py").read_text(encoding="utf-8")
    assert 'ADRESSE_KREVES_I_WEBHOOK = get("ADRESSE_KREVES_I_WEBHOOK", "1").strip() != "0"' in kilde
    assert 'ADRESSE_KREVES_I_CSV = get("ADRESSE_KREVES_I_CSV", "1").strip() != "0"' in kilde


def test_uten_innstilling_i_miljoet_er_adressen_paakrevd_i_webhook_og_csv():
    """Standarden slik en ren installasjon ser den (ingen ADRESSE_KREVES_* i miljøet): begge på."""
    assert _les_innstillinger() == "True True"


@pytest.mark.parametrize("verdi, forventet", [("1", "True True"), ("0", "False False"), (" 0 ", "False False"),
                                              ("", "True True"), ("true", "True True"), ("ja", "True True"),
                                              ("false", "True True"), ("nei", "True True"), ("00", "True True")])
def test_bare_0_slaar_innstillingene_av_alt_annet_er_paa(verdi, forventet):
    """Nødbremsen er bare verdien «0». En tom, ukjent eller feilskrevet verdi teller som PÅ: kravet skal aldri forsvinne ved en feil."""
    assert _les_innstillinger(ADRESSE_KREVES_I_WEBHOOK=verdi, ADRESSE_KREVES_I_CSV=verdi) == forventet


def test_innstillingene_kan_settes_hver_for_seg():
    assert _les_innstillinger(ADRESSE_KREVES_I_WEBHOOK="1", ADRESSE_KREVES_I_CSV="0") == "True False"
    assert _les_innstillinger(ADRESSE_KREVES_I_WEBHOOK="0", ADRESSE_KREVES_I_CSV="1") == "False True"


def test_env_eksemplet_forklarer_innstillingene_og_har_dem_paa():
    tekst = (ROT / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^ADRESSE_KREVES_I_WEBHOOK=1\b", tekst, re.M) and re.search(r"^ADRESSE_KREVES_I_CSV=1\b", tekst, re.M)
    forklaring = tekst.split("ADRESSE_KREVES_I_WEBHOOK=1")[0]
    for ord_ in ("MÅ være med", "PÅ som standard", "NØDBREMS", "går ikke tapt", "holdes tilbake", "Privat adresse mangler", "ipr.no",
                 "Bare 0 slår"):
        assert ord_ in forklaring, ord_
    assert "ADRESSE_KREVES_I_WEBHOOK=0" not in tekst and "ADRESSE_KREVES_I_CSV=0" not in tekst      # eksemplet slår aldri kravet av


# ============================ småfunksjonene ============================

def test_adresse_mangler_krever_alle_tre_feltene_og_teller_mellomrom_som_tomt():
    assert skjemafelt.adresse_mangler(INGEN)
    assert not skjemafelt.adresse_mangler(ADRESSE)
    assert skjemafelt.adresse_mangler({**ADRESSE, "postnr": None})
    assert skjemafelt.adresse_mangler({**ADRESSE, "poststed": "   "})
    assert skjemafelt.adresse_mangler({"adresse": "Vei 1", "postnr": "", "poststed": ""})


def test_valider_adresse_valgfri_godtar_ingen_adresse_men_krever_alle_tre_naar_noe_er_fylt_ut():
    v = skjemafelt.valider_adresse_valgfri
    assert v({}) == {} and v(INGEN) == {} and v({"adresse": "  ", "postnr": "", "poststed": None}) == {}
    assert v(ADRESSE) == {}
    assert set(v({"adresse": "Vei 1"})) == {"postnr", "poststed"}
    assert set(v({"postnr": "0150", "poststed": "Oslo"})) == {"adresse"}
    assert set(v({**ADRESSE, "adresse": "A" * 201})) == {"adresse"}                      # for lang er fortsatt feil
    assert v({"adresse": "Vei 1", "postnr": "0150", "poststed": "Oslo\x00"}) == {"poststed": "«Poststed» inneholder ugyldige tegn."}


# ============================ webhook: innstillingen AV ============================

def test_webhook_uten_adresse_gir_201_og_personen_faar_merket(con, av):
    kid = _kurs(con)
    r = _webhook(_nett())
    assert r.status_code == 201, r.get_json()
    p = _paamelding(con)
    assert _adresse(_person(con)) == INGEN and _faktura(p) == INGEN and p["kilde"] == "nettside"
    assert MERKE in _liste(kid)
    vindu = _vindu(kid, p["id"])
    assert 'id="adressekontroll"' in vindu and MERKE in _personskjema(vindu)


def test_webhook_med_tomme_adressefelt_er_det_samme_som_uten(con, av):
    _kurs(con)
    assert _webhook(_nett(adresse="", postnr="  ", poststed=None)).status_code == 201
    assert _adresse(_person(con)) == INGEN


def test_webhook_med_hele_adressen_lagrer_den_og_personen_har_ikke_merket(con, av):
    kid = _kurs(con)
    assert _webhook(_nett(**ADRESSE)).status_code == 201
    assert _adresse(_person(con)) == ADRESSE and _faktura(_paamelding(con)) == ADRESSE
    assert MERKE not in _liste(kid)


@pytest.mark.parametrize("data, mangler", [
    ({"adresse": "Vei 1"}, ["postnr", "poststed"]),
    ({"postnr": "0150"}, ["adresse", "poststed"]),
    ({"adresse": "Vei 1", "poststed": "Oslo"}, ["postnr"]),
    ({"adresse": "  ", "postnr": "0150", "poststed": "Oslo"}, ["adresse"]),
])
def test_webhook_med_delvis_adresse_tas_inn_og_det_mottatte_tas_vare_paa_men_personen_merkes(con, av, data, mangler):
    """JUSTERT 01.10.2026 (to ganger): nødbremsen skal ikke la en påmelding gå tapt, og informasjon som faktisk er mottatt kastes ikke. En
    delvis adresse tas inn og lagres som ufullstendige data; personen er merket «Privat adresse er ufullstendig», og fakturaen holdes
    tilbake til alle tre feltene er komplette (test_privat_adresse_faktura.py)."""
    kid = _kurs(con)
    r = _webhook(_nett(**data))
    assert r.status_code == 201 and r.get_json()["status"] == "ok" and "nødbrems" in r.get_json()["merknad"], r.get_json()
    mottatt = {k: (data.get(k) or "").strip() or None for k in ("adresse", "postnr", "poststed")}
    assert _adresse(_person(con)) == mottatt and _faktura(_paamelding(con)) == mottatt and mangler
    liste = _liste(kid)
    assert MERKE_U in liste and MERKE not in liste
    assert _antall(con, "faktura") == 0


def test_webhook_med_for_lang_adresse_avvises_ogsaa_naar_kravet_er_av(con, av):
    _kurs(con)
    assert _webhook(_nett(adresse="A" * 201, postnr="0150", poststed="Oslo")).status_code == 400 and _antall(con) == 0


def test_webhook_uten_adresse_overskriver_aldri_en_adresse_som_staar_der(con, av):
    kid = _kurs(con)
    db.meld_paa(con, _kurs(con, "A2"), epost=EPOST, fornavn="Ola", etternavn="Nordmann", deltaker=dict(ADRESSE))
    con.commit()
    assert _webhook(_nett()).status_code == 201
    assert _adresse(_person(con)) == ADRESSE and _faktura(_paamelding(con)) == ADRESSE      # personens adresse kopieres som før
    assert MERKE not in _liste(kid)


def test_webhook_uten_adresse_for_firma_registreres_ogsaa(con, av):
    _kurs(con)
    assert _webhook(_nett(org_nr="999900003")).status_code == 201
    assert _adresse(_person(con)) == INGEN and _paamelding(con)["betaler"] == "organisasjon"


def test_webhook_er_idempotent_uten_adresse(con, av):
    _kurs(con)
    assert _webhook(_nett()).status_code == 201
    assert _webhook(_nett()).status_code == 200 and _antall(con, "paamelding") == 1


# ============================ webhook: innstillingen PÅ (som før) ============================

def test_webhook_med_innstillingen_paa_krever_adressen(con, paa):
    _kurs(con)
    r = _webhook(_nett())
    assert r.status_code == 400 and "deltakerens private adresse er påkrevd" in r.get_json()["melding"]
    assert r.get_json()["melding"].endswith("adresse, postnr, poststed") and _antall(con) == 0
    assert _webhook(_nett(**ADRESSE)).status_code == 201


def test_webhook_innstillingen_leses_for_hver_foresporsel(con, monkeypatch):
    _kurs(con)
    monkeypatch.setattr(config, "ADRESSE_KREVES_I_WEBHOOK", True)
    assert _webhook(_nett()).status_code == 400
    monkeypatch.setattr(config, "ADRESSE_KREVES_I_WEBHOOK", False)
    assert _webhook(_nett()).status_code == 201


def test_webhook_kommentaren_forklarer_hva_som_maa_endres_foer_produksjon():
    kilde = (ROT / "kurs" / "web" / "app.py").read_text(encoding="utf-8")
    dok = kilde.split("def api_paamelding")[1].split('"""')[1]
    for ord_ in ("ADRESSE_KREVES_I_WEBHOOK", "FØR PRODUKSJON", "ipr.no", "adresse, postnr og poststed", "curl", "PÅ som standard",
                 "MÅ være med", "NØDBREMS", "Privat adresse mangler", "HOLDES TILBAKE", "merknad", "går ikke tapt"):
        assert ord_ in dok, ord_
    assert "AV som standard" not in dok and "holdes IKKE tilbake" not in dok and "går da uten adresse" not in dok


# ============================ CSV-import: innstillingen AV ============================

MIN = "Fornavn;Etternavn;E-post"


def test_csv_uten_adressekolonner_importeres_og_personen_merkes(con, av):
    kid = _kurs(con)
    rader = imp.parse_csv(_csv("Kari;Test;kari@x.no", header=MIN))
    assert rader == [{"fornavn": "Kari", "etternavn": "Test", "epost": "kari@x.no"}]
    resultat = imp.forhaandsvis(con, kid, rader, aktor="admin:test")
    assert resultat[0]["handling"] == imp.NY and resultat[0]["adresse_mangler"] is True
    html, r = _importer(_admin(), kid, _csv("Kari;Test;kari@x.no", header=MIN))
    assert MERKE in html and r is not None and r.status_code == 302
    assert _adresse(_person(con, "kari@x.no")) == INGEN
    assert MERKE in _liste(kid)


def test_csv_med_tomme_adressekolonner_importeres(con, av):
    kid = _kurs(con)
    html, r = _importer(_admin(), kid, _csv("Kari;Test;kari@x.no;;;;person;", "Ola;Test;ola@x.no;Vei 1;0150;Oslo;person;"))
    assert r is not None and r.status_code == 302, html[:300]
    assert _adresse(_person(con, "kari@x.no")) == INGEN and _adresse(_person(con, "ola@x.no")) == {
        "adresse": "Vei 1", "postnr": "0150", "poststed": "Oslo"}
    liste = _liste(kid)
    assert liste.count(MERKE) == 1 and liste.index("Kari Test") < liste.index(MERKE) < liste.index("Ola Test")


def test_csv_forhaandsvisningen_merker_bare_de_som_faar_manglende_adresse(con, av):
    kid = _kurs(con)
    db.meld_paa(con, _kurs(con, "A2"), epost="kari@x.no", fornavn="Kari", etternavn="Test", deltaker=dict(ANNEN_ADRESSE))
    con.commit()
    rader = imp.parse_csv(_csv("Kari;Test;kari@x.no;;;;person;", "Ola;Test;ola@x.no;;;;person;",
                               "Per;Test;per@x.no;Vei 1;0150;Oslo;person;"))
    resultat = imp.forhaandsvis(con, kid, rader, aktor="admin:test")
    assert [r["adresse_mangler"] for r in resultat] == [False, True, False]      # Kari har adresse fra før, Ola mangler, Per har
    assert [r["handling"] for r in resultat] == [imp.NY] * 3


@pytest.mark.parametrize("rad, melding", [
    ("Kari;Test;kari@x.no;Vei 1;;;person;", "Postnr mangler; Poststed mangler"),
    ("Kari;Test;kari@x.no;;0150;;person;", "Adresse mangler; Poststed mangler"),
    ("Kari;Test;kari@x.no;Vei 1;0150;;person;", "Poststed mangler"),
    (f"Kari;Test;kari@x.no;{'A' * 201};0150;Oslo;person;", "«Adresse» kan være maks 200 tegn."),
])
def test_csv_rad_med_delvis_adresse_blokkeres_ogsaa_naar_kravet_er_av(con, av, rad, melding):
    kid = _kurs(con)
    resultat = imp.forhaandsvis(con, kid, imp.parse_csv(_csv(rad)), aktor="admin:test")
    assert resultat[0]["handling"] == imp.BLOKKERT and melding in resultat[0]["melding"]
    if "maks" not in melding:
        assert "delvis utfylt" in resultat[0]["melding"]
    assert imp.har_blokkerende_rader(resultat) and _antall(con) == 0


def test_csv_med_bare_noen_av_adressekolonnene_er_lov_naar_kravet_er_av(con, av):
    kid = _kurs(con)
    rader = imp.parse_csv(_csv("Kari;Test;kari@x.no;", "Ola;Test;ola@x.no;Vei 1", header="Fornavn;Etternavn;E-post;Adresse"))
    resultat = imp.forhaandsvis(con, kid, rader, aktor="admin:test")
    assert resultat[0]["handling"] == imp.NY                                    # tom kolonne: ingen adresse
    assert resultat[1]["handling"] == imp.BLOKKERT and "Postnr mangler" in resultat[1]["melding"]     # delvis: feil


def test_csv_mangler_fortsatt_navn_og_epost_uten_adressekravet(con, av):
    with pytest.raises(imp.ImportFeil) as e:
        imp.parse_csv(_csv("Kari", header="Fornavn"))
    assert str(e.value) == "Filen mangler de obligatoriske kolonnene «Etternavn» og «E-post». Last ned malfilen og bruk kolonnenavnene der."
    with pytest.raises(imp.ImportFeil, match="obligatoriske kolonnen «E-post»"):
        imp.parse_csv(_csv("Kari;Test", header="Fornavn;Etternavn;Adresse"))


def test_csv_uten_adressekolonner_gir_forhaandsvisning_med_bekreftelse_og_merke(con, av):
    kid = _kurs(con)
    r = _last_opp(_admin(), kid, _csv("Kari;Test;kari@x.no", header=MIN))
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "Bekreft import" in html and "Blokkert" not in html
    assert MERKE in html and "får ikke noen privat adresse av denne importen" in html


def test_csv_forhaandsvisning_som_ble_laget_med_kravet_av_stoppes_hvis_det_settes_paa_for_bekreftelsen(con, monkeypatch):
    """Innstillingen slås opp for hvert kall, og importen kjører radene på nytt mot fersk tilstand: en rad som var «klar» uten adresse, er
    blokkert når kravet er slått på i mellomtiden, og da importeres ingenting (samme vern som for kapasitet og status)."""
    monkeypatch.setattr(config, "ADRESSE_KREVES_I_CSV", False)
    kid = _kurs(con)
    k = _admin()
    html = _last_opp(k, kid, _csv("Kari;Test;kari@x.no", header=MIN)).get_data(as_text=True)
    token = re.search(r'name="forhaandsvisning_token" value="([^"]+)"', html).group(1)
    monkeypatch.setattr(config, "ADRESSE_KREVES_I_CSV", True)
    r = k.post(f"/admin/kurs/{kid}/deltakere/importer/bekreft", data={"forhaandsvisning_token": token}, follow_redirects=True)
    assert "Ingenting er importert" in r.get_data(as_text=True)
    assert _antall(con) == 0 and _antall(con, "paamelding") == 0


# ============================ CSV-import: innstillingen PÅ (som før), mal og hjelpetekst ============================

def test_csv_med_innstillingen_paa_krever_kolonnene_og_adressen_paa_hver_rad(con, paa):
    kid = _kurs(con)
    with pytest.raises(imp.ImportFeil) as e:
        imp.parse_csv(_csv("Kari;Test;kari@x.no", header=MIN))
    assert "«Adresse», «Postnr» og «Poststed»" in str(e.value) and "malfilen" in str(e.value)
    resultat = imp.forhaandsvis(con, kid, imp.parse_csv(_csv("Kari;Test;kari@x.no;;;;person;")), aktor="admin:test")
    assert resultat[0]["handling"] == imp.BLOKKERT and "Adresse mangler; Postnr mangler; Poststed mangler" in resultat[0]["melding"]
    assert "delvis utfylt" not in resultat[0]["melding"]


@pytest.mark.parametrize("krav", [False, True])
def test_malen_har_adressekolonnene_og_kan_lastes_opp_som_den_er_med_begge_innstillinger(con, monkeypatch, krav):
    monkeypatch.setattr(config, "ADRESSE_KREVES_I_CSV", krav)
    tekst = _admin().get("/admin/deltaker-import-mal.csv").get_data(as_text=True)
    assert tekst.lstrip("﻿").splitlines()[0].split(";")[:6] == ["Fornavn", "Etternavn", "E-post", "Adresse", "Postnr", "Poststed"]
    kid = _kurs(con)
    resultat = imp.forhaandsvis(con, kid, imp.parse_csv(tekst.encode("utf-8")), aktor="admin:test")
    assert [r["handling"] for r in resultat] == [imp.NY] and resultat[0]["adresse_mangler"] is False


def test_importsiden_og_malen_sier_om_adressekolonnene_er_paakrevd(con, monkeypatch):
    kid = _kurs(con)
    monkeypatch.setattr(config, "ADRESSE_KREVES_I_CSV", False)
    av_tekst = _admin().get(f"/admin/kurs/{kid}/deltakere/importer").get_data(as_text=True)
    assert "ikke påkrevd" in av_tekst and "Adressekolonnene er valgfrie" in av_tekst and "Rader uten adresse stoppes" not in av_tekst
    assert "fylle ut alle tre" in av_tekst and MERKE in av_tekst
    monkeypatch.setattr(config, "ADRESSE_KREVES_I_CSV", True)
    paa_tekst = _admin().get(f"/admin/kurs/{kid}/deltakere/importer").get_data(as_text=True)
    assert "Rader uten adresse stoppes i forhåndsvisningen" in paa_tekst and "Adressekolonnene er påkrevd" in paa_tekst
    assert "ikke påkrevd" not in paa_tekst and "Adressekolonnene er valgfrie" not in paa_tekst


# ============================ deltakervinduet ============================

def test_vinduet_viser_varselboks_og_uten_krav_naar_personen_mangler_adresse(con, av):
    kid, pid, url = _uten_adresse(con)
    html = _vindu(kid, pid)
    skjema = _personskjema(html)
    assert 'id="adressekontroll"' in skjema and MERKE in skjema and "Mangler – legg den inn" in skjema
    assert skjema.index("adressekontroll") < skjema.index('id="f-fornavn"')            # øverst i Personopplysninger
    assert "data-adressegruppe" in skjema and skjema.count("data-adressefelt") == 3
    for felt in ("adresse", "postnr", "poststed"):
        tagg = re.search(rf'<input id="f-{felt}"[^>]*>', skjema).group(0)
        assert " required" not in tagg, felt
    assert "fylles ut samlet" in skjema


def test_vinduet_har_ingen_varselboks_og_beholder_kravet_naar_adressen_er_komplett(con, av):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost=EPOST, fornavn="Ola", etternavn="Nordmann", deltaker=dict(ADRESSE))
    con.commit()
    skjema = _personskjema(_vindu(kid, pid))
    assert "adressekontroll" not in skjema and MERKE not in skjema and "data-adressegruppe" not in skjema
    for felt in ("adresse", "postnr", "poststed"):
        assert " required" in re.search(rf'<input id="f-{felt}"[^>]*>', skjema).group(0), felt


def test_vinduet_varsler_ogsaa_ved_ufullstendig_adresse(con, av):
    kid, pid, _ = _uten_adresse(con, deltaker={"adresse": "Vei 1", "poststed": "Oslo"})
    assert 'id="adressekontroll"' in _vindu(kid, pid)


def test_telefon_m_m_kan_lagres_uten_at_adressen_fylles_inn(con, av):
    kid, pid, url = _uten_adresse(con)
    r = _lagre_person(_admin(), url, telefon="98765432", yrkestittel="Psykolog", arbeidssted="Eksempel Klinikk")
    assert "Personopplysninger oppdatert" in r.get_data(as_text=True)
    p = _person(con)
    assert (p["telefon"], p["yrkestittel"], p["arbeidssted"]) == ("98765432", "Psykolog", "Eksempel Klinikk")
    assert _adresse(p) == INGEN and MERKE in r.get_data(as_text=True)                  # fortsatt merket
    hendelse = con.execute("SELECT detaljer FROM hendelse WHERE handling='deltaker_endret' ORDER BY id DESC").fetchone()
    assert json.loads(hendelse["detaljer"])["felt"] == ["telefon", "yrkestittel", "arbeidssted"]      # adressen er ikke endret


def test_alle_tre_adressefelt_tomme_og_ingen_adresse_fra_for_endrer_ikke_adressen(con, av):
    _, _, url = _uten_adresse(con)
    _lagre_person(_admin(), url, telefon="98765432", adresse="", postnr="  ", poststed="")
    assert _adresse(_person(con)) == INGEN and _person(con)["telefon"] == "98765432"


def test_hele_adressen_kan_legges_inn_naar_administrator_har_den(con, av):
    kid, pid, url = _uten_adresse(con)
    assert MERKE in _liste(kid)
    r = _lagre_person(_admin(), url, telefon="98765432", **ADRESSE)
    assert "Personopplysninger oppdatert" in r.get_data(as_text=True)
    assert _adresse(_person(con)) == ADRESSE and _person(con)["telefon"] == "98765432"
    assert MERKE not in _liste(kid) and "adressekontroll" not in _personskjema(_vindu(kid, pid))
    # Fakturaadressen på privat betalers påmelding var tom og følger med (så lenge fakturaen ikke er sendt)
    assert _faktura(_paamelding(con)) == ADRESSE
    hendelse = con.execute("SELECT detaljer FROM hendelse WHERE handling='deltaker_endret' ORDER BY id DESC").fetchone()
    assert json.loads(hendelse["detaljer"])["felt"] == ["telefon", "adresse", "postnr", "poststed"] and "Eksempelveien" not in hendelse["detaljer"]


@pytest.mark.parametrize("fylt, mangler", [
    ({"adresse": "Vei 1"}, ["Postnummer", "Poststed"]),
    ({"postnr": "0150"}, ["Adresse", "Poststed"]),
    ({"adresse": "Vei 1", "poststed": "Oslo"}, ["Postnummer"]),
])
def test_delvis_utfylt_adresse_krever_alle_tre_og_ingenting_lagres(con, av, fylt, mangler):
    _, _, url = _uten_adresse(con)
    r = _lagre_person(_admin(), url, telefon="98765432", **fylt)
    html = r.get_data(as_text=True)
    for label in mangler:
        assert f"Fyll inn «{label}»." in html, label
    assert "Personopplysninger oppdatert" not in html
    p = _person(con)
    assert _adresse(p) == INGEN and p["telefon"] is None                              # heller ikke telefonnummeret ble lagret


def test_en_eksisterende_adresse_kan_ikke_tommes_og_ingenting_lagres(con, av):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost=EPOST, fornavn="Ola", etternavn="Nordmann", deltaker=dict(ADRESSE))
    con.commit()
    r = _lagre_person(_admin(), f"/admin/kurs/{kid}/deltaker/{pid}", telefon="98765432", adresse="", postnr="", poststed="")
    html = r.get_data(as_text=True)
    assert "Fyll inn «Adresse»." in html and "Personopplysninger oppdatert" not in html
    p = _person(con)
    assert _adresse(p) == ADRESSE and p["telefon"] is None


def test_en_eksisterende_adresse_kan_fortsatt_endres(con, av):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost=EPOST, fornavn="Ola", etternavn="Nordmann", deltaker=dict(ADRESSE))
    con.commit()
    _lagre_person(_admin(), f"/admin/kurs/{kid}/deltaker/{pid}", **SPORBAR_ADRESSE)
    assert _adresse(_person(con)) == SPORBAR_ADRESSE


def test_ufullstendig_adresse_fra_foer_stopper_ikke_lagring_av_andre_felt_naar_adressen_ikke_endres(con, av):
    kid, pid, url = _uten_adresse(con, deltaker={"adresse": "Vei 1", "poststed": "Oslo"})
    r = _lagre_person(_admin(), url, telefon="98765432", adresse="Vei 1", postnr="", poststed="Oslo")
    assert "Personopplysninger oppdatert" in r.get_data(as_text=True)
    assert _person(con)["telefon"] == "98765432" and _person(con)["postnr"] is None       # urørt: fortsatt merket
    r = _lagre_person(_admin(), url, adresse="Vei 1", postnr="0150", poststed="Oslo")     # og kan fullføres
    assert _adresse(_person(con)) == {"adresse": "Vei 1", "postnr": "0150", "poststed": "Oslo"}


def test_ufullstendig_adresse_fra_foer_kan_ikke_tommes(con, av):
    kid, pid, url = _uten_adresse(con, deltaker={"adresse": "Vei 1"})
    r = _lagre_person(_admin(), url, telefon="98765432", adresse="", postnr="", poststed="")
    assert "Fyll inn «Adresse»." in r.get_data(as_text=True) and _person(con)["adresse"] == "Vei 1"


def test_versjonsvakten_virker_fortsatt_for_en_person_uten_adresse(con, av):
    _, _, url = _uten_adresse(con)
    a, b = _admin(), _admin()
    va = re.search(r'name="versjon" value="([^"]+)"', a.get(url).get_data(as_text=True)).group(1)
    vb = re.search(r'name="versjon" value="([^"]+)"', b.get(url).get_data(as_text=True)).group(1)
    _lagre_person(b, url, telefon="11111111", versjon=vb)
    r = _lagre_person(a, url, telefon="22222222", versjon=va)
    assert "noen andre har endret dette" in r.get_data(as_text=True) and _person(con)["telefon"] == "11111111"


def test_db_oppdater_deltaker_uten_adresse(con, av):
    _, pid, _ = _uten_adresse(con)
    did = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pid,)).fetchone()[0]
    assert db.oppdater_deltaker(con, did, {"adresse": "", "postnr": "", "poststed": ""}) == []          # ingenting å endre
    assert db.oppdater_deltaker(con, did, {"telefon": "11223344", "adresse": None, "postnr": None, "poststed": None}) == ["telefon"]
    with pytest.raises(db.DeltakerFeil, match="Fyll inn «Postnummer»."):
        db.oppdater_deltaker(con, did, {"adresse": "Vei 1"})
    with pytest.raises(db.DeltakerFeil, match="Fyll inn «Adresse»."):
        db.oppdater_deltaker(con, did, {"postnr": "0150", "poststed": "Oslo"})
    assert _adresse(_person(con)) == INGEN
    assert db.oppdater_deltaker(con, did, dict(ADRESSE)) == ["adresse", "postnr", "poststed"]
    with pytest.raises(db.DeltakerFeil):
        db.oppdater_deltaker(con, did, {"adresse": "", "postnr": "", "poststed": ""})               # aldri tomt igjen
    assert _adresse(_person(con)) == ADRESSE


def test_javascriptet_gjor_adressen_paakrevd_bare_naar_noe_skrives_i_den():
    js = (ROT / "kurs" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "form[data-adressegruppe]" in js and "input[data-adressefelt]" in js and "f.required = endret && noenFylt" in js
    assert "data-adressegruppe" in js.split("*/")[0]                                       # dokumentert øverst i filen


# ============================ deltakerlisten ============================

@pytest.mark.skipif(shutil.which("node") is None, reason="Node er ikke installert")
def test_javascriptet_for_adressegruppen_kjorer_riktig_i_en_vm_med_falskt_dom():
    """tests/js/adressegruppe_test.js kjører selve lytteren fra app.js (ikke bare tekstsammenligning): påkrevd først når noe endres og er
    fylt ut, ikke for uendret eller bare-mellomrom, og andre felt i vinduet røres ikke."""
    r = subprocess.run([shutil.which("node"), str(ROT / "tests" / "js" / "adressegruppe_test.js")], capture_output=True, text=True,
                       timeout=120, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip().startswith("OK")


def _to_paameldte(con):
    kid = _kurs(con)
    db.meld_paa(con, kid, epost="uten@x.no", fornavn="Uten", etternavn="Adresse")
    db.meld_paa(con, kid, epost="med@x.no", fornavn="Med", etternavn="Adresse", deltaker=dict(SPORBAR_ADRESSE))
    con.commit()
    return kid


def test_listen_merker_bare_aktive_paameldte_uten_privat_adresse(con):
    kid = _to_paameldte(con)
    html = _liste(kid)
    assert html.count(MERKE) == 1
    rad = html.split('id="rad-')
    uten = next(r for r in rad if "Uten Adresse" in r)
    med = next(r for r in rad if "Med Adresse" in r)
    assert MERKE in uten and MERKE not in med
    assert "Sporbarveien" not in html and "4711" not in html and "Sporbarby" not in html      # aldri selve adressen i listen


def test_listen_merker_ogsaa_de_paa_venteliste(con):
    kid = _kurs(con, kapasitet=1)
    db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Foerste", deltaker=dict(ADRESSE))
    pid, status = db.meld_paa(con, kid, epost="b@x.no", fornavn="B", etternavn="Andre")
    con.commit()
    assert status == "venteliste" and _liste(kid).count(MERKE) == 1


def test_listen_merker_ikke_avmeldte_eller_avlyste_kurs(con):
    kid = _to_paameldte(con)
    pid = con.execute("SELECT p.id FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE d.epost='uten@x.no'").fetchone()[0]
    con.execute("UPDATE paamelding SET status='avmeldt' WHERE id=?", (pid,))
    con.commit()
    assert MERKE not in _liste(kid)                                                          # avmeldt: ikke lenger aktiv
    con.execute("UPDATE paamelding SET status='bekreftet' WHERE id=?", (pid,))
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kid,))
    con.commit()
    assert MERKE not in _liste(kid)                                                          # avlyst kurs: ingenting å følge opp


def test_listen_merker_ikke_naar_adressen_er_lagt_inn(con):
    kid = _to_paameldte(con)
    pid = con.execute("SELECT p.id FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE d.epost='uten@x.no'").fetchone()[0]
    _lagre_person(_admin(), f"/admin/kurs/{kid}/deltaker/{pid}", fornavn="Uten", etternavn="Adresse", epost="uten@x.no", **ADRESSE)
    assert MERKE not in _liste(kid)


def test_lesetilgang_ser_merket_i_listen_og_varselboksen_i_vinduet(con):
    kid = _to_paameldte(con)
    pid = con.execute("SELECT p.id FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE d.epost='uten@x.no'").fetchone()[0]
    db.opprett_admin_bruker(con, "lese", "Lese", "passord-som-holder", rolle="lese")
    con.commit()
    k = _klient()
    assert k.post("/admin/logg-inn", data={"brukernavn": "lese", "passord": "passord-som-holder"}).status_code == 302
    assert MERKE in k.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    assert 'id="adressekontroll"' in k.get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)


# ============================ rettinger etter kritikken ============================
# Listens SQL og vinduets skjemafelt.adresse_mangler skal alltid gi samme svar (også ved delvis adresse), og merket skal bare
# stå der det er noe å følge opp: aldri for anonymiserte personer, og ikke for påmeldinger som ikke er aktive.

def _sett_adresse(con, pid, adresse, postnr, poststed):
    """Skriver adressen rett i databasen (eldre, delvis eller bare-mellomrom-adresse som vinduet aldri lager selv)."""
    con.execute("UPDATE deltaker SET adresse=?, postnr=?, poststed=? WHERE id=(SELECT deltaker_id FROM paamelding WHERE id=?)",
                (adresse, postnr, poststed, pid))
    con.commit()


_FELT = [None, "   ", "x"]           # ikke satt, bare mellomrom, utfylt


def test_listen_og_vinduet_er_enige_om_hva_som_er_manglende_adresse_ogsaa_ved_delvis_adresse(con):
    """Alle 27 kombinasjonene (også bare mellomrom): merket i listen (SQL) = skjemafelt.adresse_mangler = varselboksen i vinduet.
    En mutant som bare ser på ett av feltene i listens SQL, eller som krever at alle tre er tomme, feiler her."""
    kid, pid, _ = _uten_adresse(con)
    k = _admin()
    for adresse, postnr, poststed in itertools.product(_FELT, _FELT, _FELT):
        _sett_adresse(con, pid, adresse, postnr, poststed)
        forventet = not all((v or "").strip() for v in (adresse, postnr, poststed))
        assert skjemafelt.adresse_mangler({"adresse": adresse, "postnr": postnr, "poststed": poststed}) == forventet
        liste = k.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
        vindu = k.get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
        fylt = sum(1 for v in (adresse, postnr, poststed) if (v or "").strip())
        forventet_merke = None if fylt == 3 else (MERKE_U if fylt else MERKE)           # ingen del: «mangler»; noen: «ufullstendig»
        assert skjemafelt.adresse_merke({"adresse": adresse, "postnr": postnr, "poststed": poststed}) == forventet_merke
        for merke in (MERKE, MERKE_U):
            assert (merke in liste) == (merke == forventet_merke), (adresse, postnr, poststed, merke)
        assert ('id="adressekontroll"' in vindu) == forventet, (adresse, postnr, poststed)


def test_listen_merker_en_person_med_delvis_adresse_og_ikke_med_komplett_adresse(con):
    kid, pid, _ = _uten_adresse(con)
    for adresse, postnr, poststed in [("Vei 1", "", ""), ("", "0150", ""), ("", "", "Oslo"), ("Vei 1", "0150", None),
                                      (None, "0150", "Oslo"), ("Vei 1", "  ", "Oslo")]:
        _sett_adresse(con, pid, adresse, postnr, poststed)
        liste = _liste(kid)
        assert liste.count(MERKE_U) == 1 and MERKE not in liste, (adresse, postnr, poststed)         # en delvis adresse er «ufullstendig»
    _sett_adresse(con, pid, "Vei 1", "0150", "Oslo")
    assert MERKE not in _liste(kid) and MERKE_U not in _liste(kid)


def _anonymiser(con, kid, pid):
    """Den ekte anonymiseringen (retten til sletting). Kurset må være avsluttet for at den skal kunne kjøres."""
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    did = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pid,)).fetchone()[0]
    db.anonymiser_deltaker(con, did, aktor="admin:test")
    con.commit()


def test_anonymisert_person_faar_aldri_merket_eller_varselboksen(con):
    kid, pid, _ = _uten_adresse(con)
    _anonymiser(con, kid, pid)
    liste, vindu = _liste(kid), _vindu(kid, pid)
    assert "Anonymisert" in liste and MERKE not in liste
    assert "adressekontroll" not in vindu and "Mangler – legg den inn" not in vindu


def test_anonymisert_person_faar_ikke_merket_selv_om_kurset_er_aapent(con):
    """Uavhengig av kursstatusen: en anonymisert person (e-post på @anonymisert.invalid) skal aldri bes om en ny adresse."""
    kid, pid, _ = _uten_adresse(con)
    con.execute("UPDATE deltaker SET epost='anonymisert-1@anonymisert.invalid', navn='Anonymisert Deltaker' WHERE id="
                "(SELECT deltaker_id FROM paamelding WHERE id=?)", (pid,))
    con.commit()
    assert con.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "aapen"
    assert MERKE not in _liste(kid)
    assert "adressekontroll" not in _vindu(kid, pid)


def test_en_vanlig_person_paa_samme_kurs_faar_fortsatt_merket_ved_siden_av_en_anonymisert(con):
    kid, pid, _ = _uten_adresse(con)
    db.meld_paa(con, kid, epost="vanlig@x.no", fornavn="Vanlig", etternavn="Person")
    con.execute("UPDATE deltaker SET epost='anonymisert-1@anonymisert.invalid' WHERE id=(SELECT deltaker_id FROM paamelding WHERE id=?)",
                (pid,))
    con.commit()
    assert _liste(kid).count(MERKE) == 1


def test_listen_merker_bare_paa_kurs_som_ikke_er_avsluttet_eller_avlyst(con):
    kid, pid, _ = _uten_adresse(con)
    k = _admin()
    for kursstatus, merket in [("utkast", True), ("aapen", True), ("full", True), ("aktiv", True),
                               ("avsluttet", False), ("avlyst", False)]:
        con.execute("UPDATE kurs SET status=? WHERE id=?", (kursstatus, kid))
        con.commit()
        assert (MERKE in k.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)) == merket, kursstatus


def test_listen_merker_ikke_avslaatte_utgaatte_eller_forlatte(con):
    """Avslått, Utgått og Forlatt er ikke aktive påmeldinger (samme som Avmeldt): ingenting å følge opp."""
    kid, pid, _ = _uten_adresse(con)
    k = _admin()
    assert MERKE in k.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    for kolonne in ("avslatt_ts", "utgatt_ts", "forlatt_ts"):          # alle tre krever status avmeldt (CHECK i schema.sql)
        con.execute(f"UPDATE paamelding SET status='avmeldt', {kolonne}=? WHERE id=?", (db.naa_utc(), pid))
        con.commit()
        assert MERKE not in k.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True), kolonne
        con.execute(f"UPDATE paamelding SET status='bekreftet', {kolonne}=NULL WHERE id=?", (pid,))
        con.commit()


def test_vinduet_beholder_varselboksen_for_en_person_paa_et_avsluttet_kurs(con):
    """Listen merker ikke avsluttede kurs, men vinduet er personens egne opplysninger: boksen står der (ingen støy i listen)."""
    kid, pid, _ = _uten_adresse(con)
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()
    assert MERKE not in _liste(kid) and 'id="adressekontroll"' in _vindu(kid, pid)


# ---- faktura uten adresse: dokumentasjonen skal si hvordan det faktisk er ----

def _fanget_faktura(monkeypatch):
    fanget = []
    from kurs.integrasjoner import visma
    ekte = visma.fakturer
    monkeypatch.setattr(visma, "fakturer", lambda g: (fanget.append(g), ekte(g))[1])
    return fanget


def test_webhook_uten_adresse_paa_kurs_som_faktureres_straks_holder_fakturaen_tilbake(con, av, monkeypatch):
    """JUSTERT 01.10.2026 (var: fakturaen gikk uten adresse i samme forespørsel): en privat faktura lages aldri uten komplett privat adresse.
    Kurset starter om 30 dager (faktureres straks), men fakturaen holdes tilbake til adressen er lagt inn."""
    fanget = _fanget_faktura(monkeypatch)
    _kurs(con)                                                    # starter om 30 dager: faktureres straks
    assert _webhook(_nett()).status_code == 201
    assert fanget == [] and _antall(con, "faktura") == 0


def test_faktura_til_kurs_langt_frem_venter_saa_adressen_rekker_aa_bli_lagt_inn(con, av, monkeypatch):
    """Samlet betaling faktureres tidligst seks måneder før første kursdag: da rekker administrator å legge inn adressen."""
    fanget = _fanget_faktura(monkeypatch)
    kid = _kurs(con, frem=400)
    assert _webhook(_nett()).status_code == 201 and fanget == []
    pid = _paamelding(con)["id"]
    r = _lagre_person(_admin(), f"/admin/kurs/{kid}/deltaker/{pid}", **ADRESSE)
    assert "Personopplysninger oppdatert" in r.get_data(as_text=True)
    assert _adresse(_person(con)) == ADRESSE and MERKE not in _liste(kid)
    assert _faktura(_paamelding(con)) == ADRESSE            # adressen følger med til fakturaen når den lages


def test_dokumentasjonen_sier_at_fakturaen_holdes_tilbake_uten_privat_adresse():
    ops = (ROT / "OPERATIONS.md").read_text(encoding="utf-8")
    del_2f = ops.split("## 2f.")[1].split("## 3.")[0]
    for ord_ in ("seks måneder", "holdes tilbake", "aldri uten komplett privat adresse", "Venter på privat adresse", "nødbrems"):
        assert ord_ in del_2f, ord_
    assert "sendes **uten adresse**" not in del_2f and "Det er **ikke bygget**" not in del_2f
    gdpr = (ROT / "GDPR.md").read_text(encoding="utf-8")
    avsnitt = gdpr.split("Personer registrert før adressen ble påkrevd")[1].split("## 2.")[0]
    assert "aldri uten komplett adresse" in avsnitt and "holdes tilbake" in avsnitt and "går uten adresse" not in avsnitt


# ---- dokumentasjonen skal ikke påstå noe som ikke er sant lenger ----

def test_security_md_sier_ikke_at_adressen_er_paakrevd_i_alle_veier_inn():
    tekst = (ROT / "SECURITY.md").read_text(encoding="utf-8")
    rad = next(r for r in tekst.splitlines() if r.startswith("| Deltakerens private adresse"))
    assert "alle veier inn" not in rad
    for ord_ in ("ADRESSE_KREVES_I_WEBHOOK", "ADRESSE_KREVES_I_CSV", "Privat adresse mangler", "anonymis"):
        assert ord_ in rad, ord_


def test_innstillingene_beskrives_som_lest_ved_oppstart_ikke_hver_gang():
    """config.py leser miljøvariabelen én gang ved import; bare attributtet slås opp for hvert kall. En endret App Setting
    virker derfor først etter omstart, og ingen tekst skal si «leses hver gang»."""
    for sti in ("OPERATIONS.md", "kurs/import_deltakere.py", "kurs/web/app.py", "SECURITY.md", "DEPLOYMENT.md", "README.md"):
        tekst = (ROT / sti).read_text(encoding="utf-8")
        assert "leses hver gang" not in tekst, sti
    ops = (ROT / "OPERATIONS.md").read_text(encoding="utf-8").split("## 2f.")[1].split("## 3.")[0]
    assert "leses når appen starter" in ops and "startet på nytt" in ops
