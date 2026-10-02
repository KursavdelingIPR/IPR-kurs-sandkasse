"""Skjemabyggeren: kursets egne felt (kurs/ekstrafelt.py) og det effektive skjemaet med dem (kurs/skjemafelt.py).

Rene funksjoner - ingen database. Laaste krav:
  * LAGRING er streng: ukjent type, tomt/for langt feltnavn, kontrolltegn, for faa/like/for mange svaralternativer,
    ukjent plassering og feil form paa «vis bare naar» avvises med norsk forklaring - og feilen baerer aldri teksten.
  * «Vis bare naar» har ETT nivaa, og feltet som styrer maa finnes, ha svaralternativer og ha verdien.
  * LESING er defensiv: en ugyldig rad vises ikke (advarsel), en ugyldig betingelse gjor at feltet ikke vises.
  * Svar: kun aktive felt (betingelsen oppfylt) lagres og kan kreves; ukjente svaralternativer regnes som ubesvart.
Alle testdata er fiktive."""
import json

import pytest

from kurs import ekstrafelt as ef
from kurs import skjemafelt as sf


# HPR-nummer er slått av i skjemaene (skjemafelt.HPR_I_SKJEMA = False, Camilla 02.10.2026). Disse testene gjelder koden som viser, validerer og lagrer det
# (den finnes fortsatt og kan slås på igjen), så de slår det på. Standarden (av) testes i test_hpr_nummer_samles_ikke_inn.py.
pytestmark = pytest.mark.usefixtures("hpr_i_skjema")


def _kurs(**over):
    return {"type": "fysisk", "fakturering": "person", "pris_nok": 4500, "spesialistlop": None, **over}


def _rad(**over):
    rad = {"id": 1, "type": "tekst", "label": "Land", "hjelpetekst": None, "valg": None, "obligatorisk": 0,
           "synlig": 1, "plassering": "om_deg", "rekkefolge": 4, "vis_naar_felt": None, "vis_naar_verdi": None}
    return {**rad, **over}


def _felt(felt_id=1, **over):
    return ef.les([_rad(id=felt_id, **over)]).felt[0]


# ============================ normaliser: streng lagring ============================

@pytest.mark.parametrize("type_,valg,forventet", [
    ("tekst", None, ()), ("langtekst", "a\nb", ()),               # tekstfelt har aldri alternativer
    ("avkrysning", "", ()), ("avkrysning", "Ja takk", ("Ja takk",)),
    ("envalg", "Ja\r\nNei\n\n", ("Ja", "Nei")), ("nedtrekk", ["Nord", " Sør "], ("Nord", "Sør")),
    ("flervalg", "Bare ett", ("Bare ett",)),
])
def test_gyldige_felt_normaliseres(type_, valg, forventet):
    v = ef.normaliser({"type": type_, "label": "  Spørsmål  ", "valg": valg})
    assert v["label"] == "Spørsmål" and v["valg"] == forventet and v["type"] == type_
    assert (v["obligatorisk"], v["synlig"], v["plassering"], v["vis_naar_felt"], v["vis_naar_verdi"]) == (
        False, True, "om_deg", None, None)


@pytest.mark.parametrize("data,grunn", [
    ({"type": "fil", "label": "Studentbevis"}, ef.UKJENT_TYPE),              # filopplasting finnes bevisst ikke
    ({"type": None, "label": "X"}, ef.UKJENT_TYPE),
    ({"type": "tekst", "label": "   "}, ef.MANGLER),
    ({"type": "tekst"}, ef.UGYLDIG_TYPE),
    ({"type": "tekst", "label": "x" * 81}, ef.FOR_LANG),
    ({"type": "tekst", "label": "To\nlinjer"}, ef.KONTROLLTEGN),
    ({"type": "tekst", "label": "A B"}, ef.KONTROLLTEGN),
    ({"type": "envalg", "label": "X", "valg": "Bare ett"}, ef.UGYLDIGE_VALG),
    ({"type": "nedtrekk", "label": "X", "valg": ""}, ef.UGYLDIGE_VALG),
    ({"type": "flervalg", "label": "X", "valg": ""}, ef.UGYLDIGE_VALG),
    ({"type": "envalg", "label": "X", "valg": "Ja\nja"}, ef.UGYLDIGE_VALG),   # like alternativer
    ({"type": "envalg", "label": "X", "valg": "ja\nJA"}, ef.UGYLDIGE_VALG),   # ... uansett store og små bokstaver
    ({"type": "envalg", "label": "X", "valg": "\n".join(f"v{i}" for i in range(31))}, ef.UGYLDIGE_VALG),
    ({"type": "envalg", "label": "X", "valg": "Ja\n" + "x" * 121}, ef.FOR_LANG),
    ({"type": "avkrysning", "label": "X", "valg": "En\nTo"}, ef.UGYLDIGE_VALG),
    ({"type": "tekst", "label": "X", "hjelpetekst": "x" * 501}, ef.FOR_LANG),
    ({"type": "tekst", "label": "X", "hjelpetekst": "a\x00b"}, ef.KONTROLLTEGN),
    ({"type": "tekst", "label": "X", "plassering": "midten"}, ef.UKJENT_PLASSERING),
    ({"type": "tekst", "label": "X", "obligatorisk": "on"}, ef.UGYLDIG_TYPE),
    ({"type": "tekst", "label": "X", "vis_naar_felt": "ekstra:0", "vis_naar_verdi": "Ja"}, ef.UGYLDIG_BETINGELSE),
    ({"type": "tekst", "label": "X", "vis_naar_felt": "telefon", "vis_naar_verdi": "Ja"}, ef.UGYLDIG_BETINGELSE),
    ({"type": "tekst", "label": "X", "vis_naar_felt": "ekstra:5", "vis_naar_verdi": None}, ef.UGYLDIG_BETINGELSE),
    ({"type": "tekst", "label": "X", "vis_naar_felt": "ekstra:5", "vis_naar_verdi": " "}, ef.MANGLER),
])
def test_ugyldige_felt_avvises_uten_aa_gjenta_teksten(data, grunn):
    with pytest.raises(ef.EkstrafeltFeil) as e:
        ef.normaliser(data, felt_id=7)
    assert e.value.grunn == grunn and e.value.forklaring
    assert str(e.value).startswith(grunn) and "x" * 20 not in str(e.value) and "Studentbevis" not in str(e.value)


def test_hjelpetekst_beholder_linjeskift_og_er_ren_tekst():
    v = ef.normaliser({"type": "tekst", "label": "X", "hjelpetekst": " <b>Linje 1</b>\r\n{{ 7*7 }} "})
    assert v["hjelpetekst"] == "<b>Linje 1</b>\n{{ 7*7 }}"


# ============================ «vis bare når»: ett nivå ============================

def _sett(*felt):
    return {f.id: f for f in felt}


def test_betingelse_paa_betaler_og_paa_eget_valgfelt_er_gyldig():
    deling = ef.Ekstrafelt(1, "envalg", "Deling", valg=("Ja", "Nei"))
    nyhetsbrev = ef.Ekstrafelt(2, "avkrysning", "Nyhetsbrev")
    alle = _sett(deling, nyhetsbrev,
                 ef.Ekstrafelt(3, "tekst", "Hvorfor", vis_naar_felt="ekstra:1", vis_naar_verdi="Ja"),
                 ef.Ekstrafelt(4, "tekst", "Hvilke", vis_naar_felt="ekstra:2", vis_naar_verdi=ef.AVKRYSSET),
                 ef.Ekstrafelt(5, "tekst", "Ressursnr.", vis_naar_felt="betaler", vis_naar_verdi="organisasjon"))
    ef.kontroller_betingelser(alle)


@pytest.mark.parametrize("styrer,betingelse,tekst", [
    (ef.Ekstrafelt(1, "tekst", "Fritekst"), ("ekstra:1", "Ja"), "ingen svaralternativer"),
    (ef.Ekstrafelt(1, "envalg", "Deling", valg=("Ja", "Nei")), ("ekstra:1", "Kanskje"), "har ikke svaralternativet"),
    (ef.Ekstrafelt(1, "envalg", "Deling", valg=("Ja", "Nei"), vis_naar_felt="betaler", vis_naar_verdi="person"),
     ("ekstra:1", "Ja"), "bare ett nivå"),
    (None, ("ekstra:1", "Ja"), "finnes ikke"),
    (None, ("betaler", "kanskje"), "har ikke den verdien"),
])
def test_ugyldige_betingelser_avvises(styrer, betingelse, tekst):
    avhengig = ef.Ekstrafelt(2, "tekst", "Avhengig", vis_naar_felt=betingelse[0], vis_naar_verdi=betingelse[1])
    alle = _sett(avhengig, *([styrer] if styrer else []))
    with pytest.raises(ef.EkstrafeltFeil) as e:
        ef.kontroller_betingelser(alle)
    assert e.value.grunn == ef.UGYLDIG_BETINGELSE and e.value.felt_id == 2 and tekst in e.value.forklaring


def test_et_felt_kan_ikke_styre_seg_selv():
    selv = ef.Ekstrafelt(1, "envalg", "Selv", valg=("Ja", "Nei"), vis_naar_felt="ekstra:1", vis_naar_verdi="Ja")
    with pytest.raises(ef.EkstrafeltFeil) as e:
        ef.kontroller_betingelser(_sett(selv))
    assert e.value.grunn == ef.UGYLDIG_BETINGELSE and "kan ikke vises etter sitt eget svar" in e.value.forklaring


# ============================ defensiv lesing ============================

def test_gyldig_rad_leses_uten_advarsler():
    les = ef.les([_rad(), _rad(id=2, type="envalg", label="Deling", valg='["Ja", "Nei"]', obligatorisk=1,
                               plassering="til_slutt", rekkefolge=1)])
    assert not les.har_advarsler
    assert les.felt[1] == ef.Ekstrafelt(2, "envalg", "Deling", None, ("Ja", "Nei"), True, True, "til_slutt", 1)


@pytest.mark.parametrize("over,grunn", [
    ({"type": "fil"}, ef.UKJENT_TYPE), ({"label": ""}, ef.MANGLER), ({"label": "x" * 81}, ef.FOR_LANG),
    ({"type": "envalg", "valg": "ikke json"}, ef.UGYLDIGE_VALG), ({"type": "envalg", "valg": '{"a": 1}'}, ef.UGYLDIGE_VALG),
    ({"type": "envalg", "valg": '["Bare ett"]'}, ef.UGYLDIGE_VALG), ({"synlig": 2}, ef.UGYLDIG_TYPE),
    ({"plassering": "midten"}, ef.UKJENT_PLASSERING), ({"rekkefolge": "1"}, ef.UGYLDIG_TYPE),
])
def test_rad_som_ikke_kan_vises_ignoreres_med_pii_fri_advarsel(over, grunn):
    les = ef.les([_rad(**over), _rad(id=2, label="Annet")])
    assert [f.id for f in les.felt] == [2]
    assert les.advarsler == (ef.Advarsel(grunn, 1, les.advarsler[0].egenskap),)
    assert "x" * 20 not in repr(les.advarsler)


def test_ugyldig_hjelpetekst_fjernes_men_feltet_vises():
    les = ef.les([_rad(hjelpetekst="a\x00b")])
    assert les.felt[0].hjelpetekst is None and les.felt[0].betingelse_ok
    assert les.advarsler == (ef.Advarsel(ef.KONTROLLTEGN, 1, "hjelpetekst"),)


@pytest.mark.parametrize("betingelse", [("ekstra:99", "Ja"), ("ekstra:2", "Kanskje"), ("betaler", "alle"),
                                        ("telefon", "Ja")])
def test_ugyldig_lagret_betingelse_gjor_at_feltet_ikke_vises(betingelse):
    les = ef.les([_rad(vis_naar_felt=betingelse[0], vis_naar_verdi=betingelse[1]),
                  _rad(id=2, type="envalg", label="Deling", valg='["Ja", "Nei"]')])
    assert les.felt[0].betingelse_ok is False
    assert ef.Advarsel(ef.UGYLDIG_BETINGELSE, 1, "vis_naar") in les.advarsler
    skjema = sf.effektivt_skjema(_kurs(), None, les.felt)
    assert "ekstra_1" not in [f.nokkel for f in skjema.egne_felt] and "ekstra_2" in [f.nokkel for f in skjema.egne_felt]


# ============================ det effektive skjemaet ============================

def test_egne_felt_sorteres_inn_blant_standardfeltene_i_om_deg():
    egne = [ef.Ekstrafelt(1, "tekst", "Land", rekkefolge=2), ef.Ekstrafelt(2, "tekst", "Kommentar",
                                                                         plassering="til_slutt", rekkefolge=1),
            ef.Ekstrafelt(3, "tekst", "Først", rekkefolge=0)]
    s = sf.effektivt_skjema(_kurs(spesialistlop="EFT"), None, egne)
    assert [f.nokkel for f in s.deltakerfelt] == ["ekstra_3", "telefon", "arbeidssted", "ekstra_1", "hpr_nr"]
    assert [f.nokkel for f in s.sluttfelt] == ["ekstra_2"]
    assert s.deltakerfelt[3] == sf.EffektivtFelt("ekstra_1", "Land", False, None, "tekst", (), None, 1)


def test_skjulte_egne_felt_vises_ikke_men_finnes_i_rekkefolgen():
    egne = [ef.Ekstrafelt(1, "tekst", "Land", synlig=False, rekkefolge=9)]
    assert sf.effektivt_skjema(_kurs(), None, egne).egne_felt == ()
    assert sf.plassrekkefolge(None, egne, sf.OM_DEG) == ["telefon", "arbeidssted", "yrkestittel", "ekstra:1"]
    assert sf.neste_plass(None, egne, sf.OM_DEG) == 10 and sf.neste_plass(None, egne, sf.TIL_SLUTT) == 1


def test_betingelse_blir_navn_og_verdi_i_skjemaet():
    egne = [ef.Ekstrafelt(1, "envalg", "Deling", valg=("Ja", "Nei")),
            ef.Ekstrafelt(2, "tekst", "Hvorfor", vis_naar_felt="ekstra:1", vis_naar_verdi="Ja"),
            ef.Ekstrafelt(3, "tekst", "Ressursnr.", vis_naar_felt="betaler", vis_naar_verdi="organisasjon")]
    felt = {f.nokkel: f for f in sf.effektivt_skjema(_kurs(), None, egne).egne_felt}
    assert felt["ekstra_2"].vis_naar == ("ekstra_1", "Ja") and felt["ekstra_3"].vis_naar == ("betaler", "organisasjon")


@pytest.mark.parametrize("kurs", [_kurs(pris_nok=0), _kurs(fakturering="organisasjon")])
def test_betingelse_paa_betaler_vises_ikke_uten_fakturablokk(kurs):
    egne = [ef.Ekstrafelt(3, "tekst", "Ressursnr.", vis_naar_felt="betaler", vis_naar_verdi="organisasjon")]
    assert sf.effektivt_skjema(kurs, None, egne).egne_felt == ()


def test_felt_som_vises_etter_et_skjult_felt_vises_ikke():
    egne = [ef.Ekstrafelt(1, "envalg", "Deling", valg=("Ja", "Nei"), synlig=False),
            ef.Ekstrafelt(2, "tekst", "Hvorfor", vis_naar_felt="ekstra:1", vis_naar_verdi="Ja")]
    assert sf.effektivt_skjema(_kurs(), None, egne).egne_felt == ()


def test_egne_felt_kan_aldri_erstatte_eller_skjule_standardfelt():
    egne = [ef.Ekstrafelt(1, "tekst", "Telefon", rekkefolge=5), ef.Ekstrafelt(2, "tekst", "E-post", rekkefolge=6)]
    s = sf.effektivt_skjema(_kurs(), None, egne)
    assert [f.nokkel for f in s.deltakerfelt] == ["telefon", "arbeidssted", "ekstra_1", "ekstra_2"]


# ============================ svarene ============================

def _egne_i(egne, kurs=None):
    return sf.effektivt_skjema(kurs or _kurs(), None, egne).egne_felt


EGNE = [ef.Ekstrafelt(1, "envalg", "Deling", valg=("Ja", "Nei"), obligatorisk=True, rekkefolge=1),
        ef.Ekstrafelt(2, "tekst", "Hvorfor", obligatorisk=True, rekkefolge=2, vis_naar_felt="ekstra:1",
                      vis_naar_verdi="Ja"),
        ef.Ekstrafelt(3, "flervalg", "Tema", valg=("A", "B", "C"), rekkefolge=3),
        ef.Ekstrafelt(4, "avkrysning", "Nyhetsbrev", rekkefolge=4),
        ef.Ekstrafelt(5, "langtekst", "Kommentar", rekkefolge=5, plassering="til_slutt"),
        ef.Ekstrafelt(6, "nedtrekk", "Klinikk", valg=("Nord", "Sør"), obligatorisk=True, rekkefolge=6)]


def test_gyldige_svar_lagres_som_ren_tekst():
    svar, feil = ef.tolk_svar(_egne_i(EGNE), {"ekstra_1": "Ja", "ekstra_2": "  fordi\tdet ", "ekstra_3": ["C", "A", "X"],
                                               "ekstra_4": "Ja", "ekstra_5": " Linje 1\r\nLinje 2 ", "ekstra_6": "Sør"})
    assert feil == {}
    assert svar == {1: "Ja", 2: "fordi det", 3: json.dumps(["A", "C"]), 4: "Ja", 5: "Linje 1\nLinje 2", 6: "Sør"}


def test_obligatoriske_felt_gir_meldinger_i_skjemaets_rekkefolge():
    svar, feil = ef.tolk_svar(_egne_i(EGNE), {"ekstra_1": "Ja", "ekstra_6": "Øst"})
    assert svar == {1: "Ja"}
    assert feil == {"ekstra_2": "Fyll inn «Hvorfor».", "ekstra_6": "Velg et svar i «Klinikk»."}
    assert list(feil) == ["ekstra_2", "ekstra_6"]


def test_felt_med_betingelse_som_ikke_er_oppfylt_lagres_ikke_og_kreves_ikke():
    svar, feil = ef.tolk_svar(_egne_i(EGNE), {"ekstra_1": "Nei", "ekstra_2": "MANIPULERT", "ekstra_6": "Nord"})
    assert svar == {1: "Nei", 6: "Nord"} and feil == {}


def test_ukjente_verdier_og_feil_typer_regnes_som_ubesvart():
    svar, feil = ef.tolk_svar(_egne_i(EGNE), {"ekstra_1": ["Ja"], "ekstra_3": "X", "ekstra_4": "on", "ekstra_5": ["a"],
                                               "ekstra_6": "Sør", "ekstra_99": "x"})
    assert svar == {6: "Sør"}
    assert feil == {"ekstra_1": "Velg et svar i «Deling»."}


def test_for_lange_svar_avvises():
    _, feil = ef.tolk_svar(_egne_i(EGNE), {"ekstra_1": "Nei", "ekstra_5": "x" * 2001, "ekstra_6": "Nord"})
    assert feil == {"ekstra_5": "«Kommentar» kan være maks 2000 tegn."}


def test_obligatorisk_avkrysning_maa_krysses_av():
    egne = [ef.Ekstrafelt(1, "avkrysning", "Bekreftelse", valg=("Jeg har lest kursbeskrivelsen",), obligatorisk=True)]
    assert ef.tolk_svar(_egne_i(egne), {})[1] == {"ekstra_1": "Kryss av for «Bekreftelse»."}
    assert ef.tolk_svar(_egne_i(egne), {"ekstra_1": "Ja"}) == ({1: "Ja"}, {})


def test_betingelse_paa_betaler():
    egne = [ef.Ekstrafelt(1, "tekst", "Ressursnr.", obligatorisk=True, vis_naar_felt="betaler",
                          vis_naar_verdi="organisasjon")]
    assert ef.tolk_svar(_egne_i(egne), {"betaler": "person", "ekstra_1": "x"}) == ({}, {})
    assert ef.tolk_svar(_egne_i(egne), {"betaler": "organisasjon"})[1] == {"ekstra_1": "Fyll inn «Ressursnr.»."}


def test_betingelse_paa_flervalg():
    egne = [ef.Ekstrafelt(1, "flervalg", "Tema", valg=("A", "B")),
            ef.Ekstrafelt(2, "tekst", "Om B", vis_naar_felt="ekstra:1", vis_naar_verdi="B")]
    assert ef.tolk_svar(_egne_i(egne), {"ekstra_1": ["A", "B"], "ekstra_2": "ja"})[0] == {1: '["A", "B"]', 2: "ja"}
    assert ef.tolk_svar(_egne_i(egne), {"ekstra_1": ["A"], "ekstra_2": "ja"})[0] == {1: '["A"]'}


def test_vis_svar():
    assert ef.vis_svar('["A", "C"]') == "A, C" and ef.vis_svar("Ja") == "Ja" and ef.vis_svar(None) == ""
    assert ef.vis_svar("[ikke json") == "[ikke json" and ef.vis_svar('[1, 2]') == "[1, 2]"


def test_hurtigvalgene_er_gyldige_og_uten_forhaandsvalgt_samtykke():
    for navn, (_, data) in ef.MALER.items():
        v = ef.normaliser(dict(data))
        assert v["label"], navn
    assert ef.MALER["deling_epost"][1]["valg"] == ("Ja", "Nei")      # Nei er et reelt valg (ingenting er forhåndsvalgt)
    assert not ef.MALER["nyhetsbrev"][1].get("obligatorisk")         # nyhetsbrev er frivillig
