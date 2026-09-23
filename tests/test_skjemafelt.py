"""Fase 12C1: skjemafelt-registeret (kurs/skjemafelt.py) - kodede standarder og effektivt_skjema() uten database."""
import dataclasses
import inspect

import pytest
from flask import render_template
from jinja2 import UndefinedError

from kurs import skjemafelt as sf


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


def test_kun_telefon_og_arbeidssted_er_konfigurerbare_med_alle_egenskaper():
    konfig = {n for n, f in sf.REGISTER.items() if f.kategori == sf.KONFIGURERBAR}
    assert konfig == set(sf.KONFIGURERBAR_GRUPPE) == {"telefon", "arbeidssted"}
    for n in konfig:
        assert sf.REGISTER[n].overstyrbart == sf.EGENSKAPER == {"synlig", "obligatorisk", "label", "hjelpetekst",
                                                               "rekkefolge"}


def test_navn_epost_samtykke_er_laast_synlige_obligatoriske_og_uten_overstyring():
    for n in ("navn", "epost", "samtykke"):
        f = sf.REGISTER[n]
        assert (f.kategori, f.synlig, f.obligatorisk, f.overstyrbart) == (sf.LAAST, True, True, frozenset())
    assert sf.REGISTER["navn"].label == "Navn" and sf.REGISTER["epost"].label == "E-post"


def test_laaste_felt_er_ikke_i_den_dynamiske_deltakerlisten():
    """navn/epost/samtykke rendres fast i kurs.html - de kan aldri komme via registerets deltakerliste."""
    for kurs in (_kurs(), _kurs(spesialistlop="EFT"), _kurs(type="digital")):
        nokler = {f.nokkel for f in sf.effektivt_skjema(kurs).deltakerfelt}
        assert not nokler & {"navn", "epost", "samtykke"}


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


def test_sensitive_felt_er_egen_systemblokk_og_aldri_skjemafelt():
    assert sf.SENSITIV_BLOKK.felt == ("allergier", "tilrettelegging")
    assert not set(sf.SENSITIV_BLOKK.felt) & set(sf.REGISTER)


# ============================ 5: fakturablokken ============================

@pytest.mark.parametrize("fakturering,pris,vis", [
    ("person", 4500, True), ("person", 1, True), ("person", 0, False),
    ("organisasjon", 4500, False), ("ingen", 4500, False), ("organisasjon", 0, False),
])
def test_fakturablokken_har_samme_visningsbetingelse_som_for(fakturering, pris, vis):
    # Tidligere i kurs.html: {% if kurs.fakturering == 'person' and kurs.pris_nok %}
    assert sf.effektivt_skjema(_kurs(fakturering=fakturering, pris_nok=pris)).vis_fakturablokk is vis


def test_fakturablokken_er_fast_systemblokk_og_aldri_skjemafelt():
    assert sf.FAKTURABLOKK.felt == ("betaler", "org_navn", "org_nr", "faktura_ref", "faktura_epost", "ehf", "betaling",
                                    "faktura_adresse", "faktura_postnr", "faktura_sted")
    assert not set(sf.FAKTURABLOKK.felt) & set(sf.REGISTER)


def test_visningsbetingelsene_er_uavhengige_av_hverandre():
    s = sf.effektivt_skjema(_kurs(type="digital", fakturering="person", pris_nok=100, spesialistlop="EFT"))
    assert (s.vis_sensitive_felt, s.vis_fakturablokk, _felt(s, "hpr_nr") is not None) == (False, True, True)


# ============================ 6: rekkefolge ============================

def test_stabil_standardrekkefolge():
    assert [f.nokkel for f in sf.effektivt_skjema(_kurs()).deltakerfelt] == ["telefon", "arbeidssted"]
    assert [f.nokkel for f in sf.effektivt_skjema(_kurs(spesialistlop="EFT")).deltakerfelt] == [
        "telefon", "arbeidssted", "hpr_nr"]
    # Samme input -> samme resultat, hver gang
    assert len({sf.effektivt_skjema(_kurs(spesialistlop="EFT")) for _ in range(20)}) == 1


# ============================ 7: ingen egendefinerte felt ============================

def test_registeret_har_noyaktig_de_kjente_feltene():
    assert set(sf.REGISTER) == {"navn", "epost", "telefon", "arbeidssted", "hpr_nr", "samtykke"}


def test_registeret_kan_ikke_utvides_eller_endres_i_kjoretid():
    with pytest.raises(TypeError):
        sf.REGISTER["eget_felt"] = sf.Skjemafelt("eget_felt", "Eget", sf.KONFIGURERBAR, True, False, frozenset())
    with pytest.raises(dataclasses.FrozenInstanceError):
        sf.REGISTER["telefon"].obligatorisk = True


def test_ukjente_kursdata_gir_ikke_nye_felt():
    """Ekstra nokler i kursraden (f.eks. noe som ligner skjemakonfigurasjon) har ingen effekt."""
    kurs = _kurs(skjemafelt=[{"nokkel": "eget_felt", "label": "Eget"}], eget_felt="x", telefon_obligatorisk=1)
    assert sf.effektivt_skjema(kurs) == sf.effektivt_skjema(_kurs())


def test_ingen_api_for_egendefinerte_felt():
    offentlige = {n for n, o in inspect.getmembers(sf, inspect.isfunction)
                  if not n.startswith("_") and o.__module__ == sf.__name__}
    assert offentlige == {"effektivt_skjema", "vis_hpr", "vis_fakturablokk", "vis_sensitive_felt"}


def test_kategoriene_er_kjente_og_overstyrbare_egenskaper_er_gyldige():
    for f in sf.REGISTER.values():
        assert f.kategori in (sf.LAAST, sf.KONFIGURERBAR, sf.BETINGET)
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
    from kurs.web import app as webapp
    with webapp.app.test_request_context("/"):
        return render_template("kurs.html", kurs=MAL_KURS, dager=[], f={}, plasser_igjen=None, **kw)


def test_mal_feiler_hoylytt_uten_skjema():
    """Fail-loud: rendres kurs.html uten skjema (en ny rendringsvei som ikke gaar via _render_paameldingsside),
    skal det feile - ikke stille gi et skjema uten deltakerfelt/fakturablokk/sensitive felt."""
    with pytest.raises(UndefinedError):
        _render_kurs_html()


def test_mal_rendrer_obligatorisk_felt_med_stjerne_og_required():
    """Grenen er ubrukt i 12C1 (ingen felt kan bli obligatoriske ennaa), men malens utforming for 12C2+ bevises her."""
    skjema = sf.EffektivtSkjema((sf.EffektivtFelt("telefon", "Mobil", True),), False, False)
    html = _render_kurs_html(skjema=skjema)
    assert '<div><label>Mobil *</label><input name="telefon" required value=""></div>' in html


def test_mal_escaper_label():
    skjema = sf.EffektivtSkjema((sf.EffektivtFelt("telefon", "<b>Tlf</b> & {{ 7*7 }}", False),), False, False)
    html = _render_kurs_html(skjema=skjema)
    assert "<label>&lt;b&gt;Tlf&lt;/b&gt; &amp; {{ 7*7 }}</label>" in html and "<b>Tlf</b>" not in html
