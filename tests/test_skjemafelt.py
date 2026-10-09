"""Fase 12C1 + skjemabyggeren: skjemafelt-registeret (kurs/skjemafelt.py) - kodede standarder og effektivt_skjema()
uten database. Kursets egne felt testes i test_ekstrafelt.py."""
import dataclasses
import inspect

import pytest
from flask import render_template
from jinja2 import UndefinedError

from kurs import skjemafelt as sf


# HPR-nummer er slått av i skjemaene (skjemafelt.HPR_I_SKJEMA = False, Camilla 02.10.2026). Disse testene gjelder koden som viser, validerer og lagrer det
# (den finnes fortsatt og kan slås på igjen), så de slår det på. Standarden (av) testes i test_hpr_nummer_samles_ikke_inn.py.
pytestmark = pytest.mark.usefixtures("hpr_i_skjema")


def _kurs(**over):
    """Minimal kursrad (samme nokler som tabellen kurs bruker her). Ingen database."""
    return {"type": "fysisk", "fakturering": "person", "pris_nok": 4500, "spesialistlop": None, **over}


def _felt(skjema, nokkel):
    return next((f for f in skjema.deltakerfelt if f.nokkel == nokkel), None)


# ============================ 1-2: telefon og arbeidssted ============================

@pytest.mark.parametrize("nokkel,label", [("telefon", "Telefon"), ("arbeidssted", "Arbeidssted")])
def test_kodet_standard_for_konfigurerbare_felt(nokkel, label):
    reg = sf.REGISTER[nokkel]
    assert (reg.label, reg.kategori, reg.synlig, reg.obligatorisk) == (label, sf.KONFIGURERBAR, True, False)
    assert _felt(sf.effektivt_skjema(_kurs()), nokkel) == sf.EffektivtFelt(nokkel, label, False)


def test_om_deg_feltene_er_konfigurerbare_med_alle_egenskaper():
    konfig = {n for n, f in sf.REGISTER.items() if f.kategori == sf.KONFIGURERBAR}
    assert konfig == set(sf.KONFIGURERBAR_GRUPPE) == {"telefon", "arbeidssted", "yrkestittel"}
    for n in konfig:
        assert sf.REGISTER[n].overstyrbart == sf.EGENSKAPER == {"synlig", "obligatorisk", "label", "hjelpetekst",
                                                               "rekkefolge"}
    assert [sf.REGISTER[n].rekkefolge for n in sf.KONFIGURERBAR_GRUPPE] == [1, 2, 3]


def test_yrkestittel_vises_som_standard():
    # yrkestittel vises som standard, men er ikke obligatorisk (Camilla 05.10.2026)
    y = sf.REGISTER["yrkestittel"]
    assert (y.label, y.synlig, y.obligatorisk) == ("Yrkestittel", True, False)
    assert _felt(sf.effektivt_skjema(_kurs()), "yrkestittel") == sf.EffektivtFelt("yrkestittel", "Yrkestittel", False)
    assert [f.nokkel for f in sf.effektivt_skjema(_kurs()).deltakerfelt] == ["telefon", "arbeidssted", "yrkestittel"]
    skjult = sf.effektivt_skjema(_kurs(), {"yrkestittel": sf.Overstyring(synlig=False)})
    assert [f.nokkel for f in skjult.deltakerfelt] == ["telefon", "arbeidssted"]     # kan fortsatt skjules per kurs


def test_navn_epost_adresse_samtykke_er_laast_synlige_obligatoriske_og_uten_overstyring():
    for n in ("fornavn", "etternavn", "epost", "adresse", "postnr", "poststed", "samtykke"):
        f = sf.REGISTER[n]
        assert (f.kategori, f.synlig, f.obligatorisk, f.overstyrbart) == (sf.LAAST, True, True, frozenset())
    assert sf.REGISTER["fornavn"].label == "Fornavn" and sf.REGISTER["etternavn"].label == "Etternavn"
    assert sf.REGISTER["epost"].label == "E-post" and "navn" not in sf.REGISTER     # fullt navn er ikke et skjemafelt


def test_laaste_felt_er_ikke_i_den_dynamiske_deltakerlisten():
    """fornavn/etternavn/epost/samtykke rendres fast i kurs.html, og adressefeltene har sin egen liste (adressefelt) - de kan
    aldri komme via registerets deltakerliste."""
    for kurs in (_kurs(), _kurs(spesialistlop="EFT"), _kurs(type="digital")):
        nokler = {f.nokkel for f in sf.effektivt_skjema(kurs).deltakerfelt}
        assert not nokler & {"fornavn", "etternavn", "epost", "samtykke", "adresse", "postnr", "poststed"}


# ============================ 3: HPR ============================

@pytest.mark.parametrize("lop", [None, ""])
def test_hpr_er_skjult_uten_spesialistlop(lop):
    assert _felt(sf.effektivt_skjema(_kurs(spesialistlop=lop)), "hpr_nr") is None


def test_hpr_er_synlig_med_spesialistlop():
    assert _felt(sf.effektivt_skjema(_kurs(spesialistlop="EFT")), "hpr_nr") == sf.EffektivtFelt(
        "hpr_nr", "HPR-nummer", False)


@pytest.mark.parametrize("oppsett", [{}, {"type": "digital"}, {"fakturering": "ingen"}, {"pris_nok": 0}])
def test_hpr_er_aldri_obligatorisk_og_kan_kun_faa_hjelpetekst(oppsett):
    assert not _felt(sf.effektivt_skjema(_kurs(spesialistlop="EFT", **oppsett)), "hpr_nr").obligatorisk
    hpr = sf.REGISTER["hpr_nr"]
    assert (hpr.kategori, hpr.obligatorisk, hpr.overstyrbart) == (sf.BETINGET, False, frozenset({sf.HJELPETEKST}))


# ============================ 4: sensitive felt ============================

@pytest.mark.parametrize("type_,vis", [("digital", False), ("fysisk", True), ("hybrid", True)])
def test_sensitive_felt_folger_eksisterende_kurstyperegel(type_, vis):
    assert sf.effektivt_skjema(_kurs(type=type_)).vis_sensitive_felt is vis


def test_sensitive_felt_kan_tilpasses_men_aldri_bli_obligatoriske():
    """Skjemabyggeren: allergier/tilrettelegging kan skjules og få egne tekster - aldri krav, aldri flyttes, og aldri
    vist på digitale kurs (kurstyperegelen over gjelder uansett)."""
    assert sf.SENSITIVE_FELT == ("allergier", "tilrettelegging")
    for n in sf.SENSITIVE_FELT:
        f = sf.REGISTER[n]
        assert (f.kategori, f.obligatorisk, f.overstyrbart, f.type) == (
            sf.SENSITIV, False, frozenset({"synlig", "label", "hjelpetekst"}), "langtekst")
    assert [f.nokkel for f in sf.effektivt_skjema(_kurs()).sensitive_felt] == ["allergier", "tilrettelegging"]
    tvang = {n: sf.Overstyring(synlig=True, obligatorisk=True, rekkefolge=0) for n in sf.SENSITIVE_FELT}
    assert sf.effektivt_skjema(_kurs(type="digital"), tvang).sensitive_felt == ()
    assert not any(f.obligatorisk for f in sf.effektivt_skjema(_kurs(), tvang).sensitive_felt)


# ============================ 5: fakturablokken ============================

@pytest.mark.parametrize("fakturering,pris,vis", [
    ("person", 4500, True), ("person", 1, True), ("person", 0, False),
    ("organisasjon", 4500, False), ("ingen", 4500, False), ("organisasjon", 0, False),
])
def test_fakturablokken_har_samme_visningsbetingelse_som_for(fakturering, pris, vis):
    # Tidligere i kurs.html: {% if kurs.fakturering == 'person' and kurs.pris_nok %}
    assert sf.effektivt_skjema(_kurs(fakturering=fakturering, pris_nok=pris)).vis_fakturablokk is vis


def test_fakturablokken_er_fast_systemblokk_og_aldri_skjemafelt():
    """Selve blokken (hvem betaler, organisasjonsnummer med firmaopplysningene fra registeret, betalingsmåte) er fast.
    Feltene i den som kan tilpasses, er egne registerfelt (FAKTURA) - aldri de samme navnene."""
    assert sf.FAKTURABLOKK.felt == ("betaler", "org_nr", "org_navn", "org_adresse", "org_postnr", "org_sted", "betaling")
    assert not set(sf.FAKTURABLOKK.felt) & set(sf.REGISTER)


def test_fakturafeltene_tilhorer_riktig_betaler():
    # Betaler deltakeren selv, er fakturaadressen hans egen adresse (adressefelt) - de private fakturafeltene er avviklet
    assert sf.FAKTURAFELT == ("faktura_ref", "faktura_epost", "faktura_kommentar", "ehf")
    assert {n: sf.REGISTER[n].betaler for n in sf.FAKTURAFELT} == {
        "faktura_ref": sf.ORGANISASJON, "faktura_epost": sf.ORGANISASJON, "faktura_kommentar": sf.ORGANISASJON,
        "ehf": sf.ORGANISASJON}
    assert "obligatorisk" not in sf.REGISTER["ehf"].overstyrbart and sf.REGISTER["ehf"].type == "avkrysning"
    s = sf.effektivt_skjema(_kurs())
    assert [f.nokkel for f in s.adressefelt] == ["adresse", "postnr", "poststed"]
    # EHF er skjult som standard (Camilla 05.10.2026), men kan vises per kurs
    assert [f.nokkel for f in s.firmafelt] == ["faktura_ref", "faktura_epost", "faktura_kommentar"]
    med_ehf = sf.effektivt_skjema(_kurs(), {"ehf": sf.Overstyring(synlig=True)})
    assert [f.nokkel for f in med_ehf.firmafelt] == ["faktura_ref", "faktura_epost", "faktura_kommentar", "ehf"]
    assert s.firmafelt[0] == sf.EffektivtFelt("faktura_ref", "Faktura merkes med", False,
                                              "NB! Bruk bare tall for ressurs nr. / merida nr. / avdelings nr.")
    uten = sf.effektivt_skjema(_kurs(pris_nok=0), {n: sf.Overstyring(synlig=True) for n in sf.FAKTURAFELT})
    assert uten.firmafelt == ()     # blokken følger kursdata uansett overstyring
    assert [f.nokkel for f in uten.adressefelt] == ["adresse", "postnr", "poststed"]     # adressen er alltid med


def test_informasjonsboksen_kan_bare_vises_eller_skjules():
    assert sf.INFOFELT == ("vis_tid", "vis_sted", "vis_pris", "vis_frist")     # «Ledige plasser» er avviklet
    for n in sf.INFOFELT:
        assert (sf.REGISTER[n].kategori, sf.REGISTER[n].overstyrbart) == (sf.INFO, frozenset({"synlig"}))
    assert sf.effektivt_skjema(_kurs()).info == sf.INFO_STANDARD == frozenset(sf.INFOFELT)
    s = sf.effektivt_skjema(_kurs(), {"vis_pris": sf.Overstyring(synlig=False)})
    assert s.info == frozenset(sf.INFOFELT) - {"vis_pris"} and s.deltakerfelt == sf.effektivt_skjema(_kurs()).deltakerfelt


def test_ledige_plasser_er_avviklet_gamle_rader_ignoreres_uten_advarsel_og_kan_ikke_settes_paa_nytt():
    rad = {"felt": "vis_plasser", **{e: None for e in sf.EGENSKAPER_REKKEFOLGE}, "synlig": 0}
    lest = sf.les_overstyringer([rad])
    assert lest.advarsler == () and dict(lest.overstyringer) == {}
    ukjent = sf.les_overstyringer([{**rad, "felt": "vis_noe_annet"}])
    assert len(ukjent.advarsler) == 1                     # andre ukjente felt gir fortsatt advarsel
    assert "vis_plasser" not in sf.REGISTER
    with pytest.raises(sf.SkjemafeltFeil):
        sf.normaliser_overstyring("vis_plasser", {"synlig": False})
    assert sf.effektivt_skjema(_kurs(), lest).info == sf.INFO_STANDARD


def test_visningsbetingelsene_er_uavhengige_av_hverandre():
    s = sf.effektivt_skjema(_kurs(type="digital", fakturering="person", pris_nok=100, spesialistlop="EFT"))
    assert (s.vis_sensitive_felt, s.vis_fakturablokk, _felt(s, "hpr_nr") is not None) == (False, True, True)


# ============================ 6: rekkefolge ============================

def test_stabil_standardrekkefolge():
    # yrkestittel vises som standard (Camilla 05.10.2026); HPR kommer fortsatt sist
    assert [f.nokkel for f in sf.effektivt_skjema(_kurs()).deltakerfelt] == ["telefon", "arbeidssted", "yrkestittel"]
    assert [f.nokkel for f in sf.effektivt_skjema(_kurs(spesialistlop="EFT")).deltakerfelt] == [
        "telefon", "arbeidssted", "yrkestittel", "hpr_nr"]
    # Samme input -> samme resultat, hver gang
    assert len({sf.effektivt_skjema(_kurs(spesialistlop="EFT")) for _ in range(20)}) == 1


# ============================ 7: ingen egendefinerte felt ============================

def test_registeret_har_noyaktig_de_kjente_feltene():
    assert set(sf.REGISTER) == {"fornavn", "etternavn", "epost", "adresse", "postnr", "poststed", "telefon", "arbeidssted",
                                "yrkestittel", "hpr_nr", "faktura_ref", "faktura_epost", "faktura_kommentar", "ehf",
                                "allergier", "tilrettelegging", "vis_tid", "vis_sted", "vis_pris", "vis_frist", "samtykke"}


def test_registeret_kan_ikke_utvides_eller_endres_i_kjoretid():
    with pytest.raises(TypeError):
        sf.REGISTER["eget_felt"] = sf.Skjemafelt("eget_felt", "Eget", sf.KONFIGURERBAR, True, False, frozenset())
    with pytest.raises(dataclasses.FrozenInstanceError):
        sf.REGISTER["telefon"].obligatorisk = True


def test_ukjente_kursdata_gir_ikke_nye_felt():
    """Ekstra nokler i kursraden (f.eks. noe som ligner skjemakonfigurasjon) har ingen effekt."""
    kurs = _kurs(skjemafelt=[{"nokkel": "eget_felt", "label": "Eget"}], eget_felt="x", telefon_obligatorisk=1)
    assert sf.effektivt_skjema(kurs) == sf.effektivt_skjema(_kurs())


def test_registeret_har_ingen_api_for_nye_standardfelt():
    offentlige = {n for n, o in inspect.getmembers(sf, inspect.isfunction)
                  if not n.startswith("_") and o.__module__ == sf.__name__}
    # normaliser_overstyring/les_overstyringer validerer kun overstyringer av KJENTE felt - ingen oppretter felt.
    # Kursets EGNE felt lages i kurs/ekstrafelt.py og ligger i en egen tabell - aldri i registeret.
    # rens_adresse/valider_adresse kontrollerer bare VERDIENE i deltakerens adresse (de låste feltene) - de lager ingen felt.
    # valider_adresse_valgfri (CSV med kravet av), valider_adresse_nodbrems (webhook-nødbremsen) og adresse_mangler (merket
    # «Privat adresse mangler») gjør det samme: leser bare verdier.
    assert offentlige == {"effektivt_skjema", "vis_hpr", "vis_fakturablokk", "vis_sensitive_felt",
                          "normaliser_overstyring", "les_overstyringer", "plassrekkefolge", "neste_plass",
                          "rens_adresse", "valider_adresse", "valider_adresse_valgfri", "valider_adresse_nodbrems",
                          "adresse_mangler", "adresse_status", "adresse_merke"}


def test_kategoriene_er_kjente_og_overstyrbare_egenskaper_er_gyldige():
    for f in sf.REGISTER.values():
        assert f.kategori in (sf.LAAST, sf.KONFIGURERBAR, sf.BETINGET, sf.FAKTURA, sf.SENSITIV, sf.INFO)
        assert f.overstyrbart <= sf.EGENSKAPER


# ============================ ren funksjon / ingen database ============================

def test_effektivt_skjema_leser_ikke_databasen():
    """12C1 har ingen DB-overstyringer: modulen importerer ikke db, og funksjonen fungerer paa en ren dict."""
    assert "db" not in vars(sf) and "sqlite3" not in vars(sf)
    assert sf.effektivt_skjema(_kurs()).deltakerfelt


def test_effektivt_skjema_er_uforanderlig():
    s = sf.effektivt_skjema(_kurs())
    assert isinstance(s.deltakerfelt, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.vis_fakturablokk = False


# ============================ malen ============================

MAL_KURS = {"navn": "K", "type": "digital", "sted": None, "pris_nok": 0, "fakturering": "ingen", "spesialistlop": None,
            "notat": None, "kode": "K1", "start_kl": "09:00", "slutt_kl": "16:00", "betaling": "samlet"}


def _render_kurs_html(**kw):
    from kurs import paameldingsside
    from kurs.web import app as webapp
    side = paameldingsside.EffektivSide(None, paameldingsside.STANDARD_KNAPPETEKST)   # 12C5: malen krever side
    with webapp.app.test_request_context("/"):
        return render_template("kurs.html", kurs=MAL_KURS, dager=[], f={}, plasser_igjen=None, side=side, **kw)


def test_mal_feiler_hoylytt_uten_skjema():
    """Fail-loud: rendres kurs.html uten skjema (en ny rendringsvei som ikke gaar via _render_paameldingsside),
    skal det feile - ikke stille gi et skjema uten deltakerfelt/fakturablokk/sensitive felt."""
    with pytest.raises(UndefinedError):
        _render_kurs_html()


def test_mal_rendrer_obligatorisk_felt_med_stjerne_og_required():
    """Grenen er ubrukt i 12C1 (ingen felt kan bli obligatoriske ennaa), men malens utforming for 12C2+ bevises her."""
    skjema = sf.EffektivtSkjema((sf.EffektivtFelt("telefon", "Mobil", True),), False, False)
    html = _render_kurs_html(skjema=skjema)
    assert '<label class="pm-etikett" for="f-telefon">Mobil <span class="pm-krav">(krav)</span></label>' in html
    assert '<input id="f-telefon" name="telefon" type="tel" autocomplete="tel" required value="">' in html


def test_mal_escaper_label():
    skjema = sf.EffektivtSkjema((sf.EffektivtFelt("telefon", "<b>Tlf</b> & {{ 7*7 }}", False),), False, False)
    html = _render_kurs_html(skjema=skjema)
    assert ('<label class="pm-etikett" for="f-telefon">&lt;b&gt;Tlf&lt;/b&gt; &amp; {{ 7*7 }}</label>' in html
            and "<b>Tlf</b>" not in html)
