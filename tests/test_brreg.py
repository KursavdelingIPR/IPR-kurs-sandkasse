"""Brønnøysundregistrene (kurs/integrasjoner/brreg.py) og kommandoen `python -m kurs.brreg_sjekk`.

Kontrollsiffer, demo UTEN nettverk, og driftsgrenen mot et etterlignet Enhetsregisteret - testene gjør aldri ekte
nettverkskall. Den ekte oppkoblingen kontrolleres med `python -m kurs.brreg_sjekk <orgnr>` (se OPERATIONS.md).
Virksomhetene her er oppdiktede."""
import json

import pytest
import requests

from kurs import brreg_sjekk, config
from kurs.integrasjoner import brreg


@pytest.fixture(autouse=True)
def _tomt_hurtigminne():
    brreg.hurtigminne.nullstill()
    yield
    brreg.hurtigminne.nullstill()


class FalsktRegister:
    """Etterligner requests.get mot Enhetsregisteret. `svar`: {sti etter API: (status eller unntak, json)}."""

    def __init__(self, svar=None):
        self.svar, self.kall = svar or {}, []

    def __call__(self, url, params=None, headers=None, timeout=None):
        assert url.startswith(brreg.API)
        sti = url[len(brreg.API):]
        self.kall.append((sti, params, headers, timeout))
        status, data = self.svar.get(sti, (404, {"feilmelding": "ikke funnet"}))
        if isinstance(status, Exception):
            raise status
        r = requests.Response()
        r.status_code = status
        r._content = data if isinstance(data, bytes) else json.dumps(data).encode()
        return r


@pytest.fixture
def drift(monkeypatch):
    monkeypatch.setattr(config, "DEMO", False)
    falsk = FalsktRegister()
    monkeypatch.setattr(requests, "get", falsk)
    return falsk


def _enhet(orgnr="912345688", navn="EKSEMPEL AS", **adresser):
    return {"organisasjonsnummer": orgnr, "navn": navn, **adresser}


def _adr(*linjer, postnr="1234", sted="EKSEMPELBY", landkode="NO", land="Norge"):
    return {"adresse": list(linjer), "postnummer": postnr, "poststed": sted, "landkode": landkode, "land": land}


# ============================ organisasjonsnummer ============================

@pytest.mark.parametrize("nr,gyldig", [
    ("974760673", True), ("999999999", True), ("999900003", True), ("912345688", True),
    ("912345675", False),                   # kontrollsiffer 10: finnes ikke
    ("974760672", False),                   # feil kontrollsiffer
    ("074760673", False),                   # organisasjonsnumre begynner med 8 eller 9 ...
    ("123456785", False), ("700000001", False),   # ... også når kontrollsifferet stemmer
    ("97476067", False), ("9747606730", False), ("97476067a", False), ("", False), (None, False), (974760673, False),
    ("٩٧٤٧٦٠٦٧٣", False),                   # andre sifre enn 0-9
])
def test_gyldig_orgnr(nr, gyldig):
    assert brreg.gyldig_orgnr(nr) is gyldig


def test_kontrollsiffer_10_er_aldri_gyldig():
    """Gir modulus 11 kontrollsiffer 10, finnes nummeret ikke - uansett siste siffer."""
    aatte = next(f"9{i:07d}" for i in range(10 ** 7)
                 if (11 - sum(int(t) * v for t, v in zip(f"9{i:07d}", brreg._VEKTER)) % 11) % 11 == 10)
    assert not any(brreg.gyldig_orgnr(aatte + str(s)) for s in range(10))


@pytest.mark.parametrize("tekst,forventet", [("974 760 673", "974760673"), ("974.760.673", "974760673"),
                                             (" 97476 0673 ", "974760673"), ("NO974760673MVA", "974760673"),
                                             (None, ""), (123, "")])
def test_normaliser_orgnr(tekst, forventet):
    assert brreg.normaliser_orgnr(tekst) == forventet


# ============================ demo: aldri nettverk ============================

def test_demo_gjor_ingen_nettverkskall(monkeypatch):
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(requests, "get", lambda *a, **kw: pytest.fail("nettverkskall i demo"))
    assert brreg.hent("999 900 003") == brreg.Enhet("999900003", "EKSEMPEL KOMMUNE", "Postboks 100", "1234", "EKSEMPELBY")
    assert brreg.hent("974760673") is None                       # ekte nummer: ikke oppdiktet -> «finnes ikke»
    assert brreg.hent("999900004") is None                       # ugyldig
    assert [e.orgnr for e in brreg.sok("EKSEMPEL")] == ["999900003", "999900011", "999900046"]
    assert brreg.sok("ek") == []


def test_demovirksomhetene_har_gyldige_organisasjonsnumre():
    assert all(brreg.gyldig_orgnr(e.orgnr) for e in brreg.DEMO_ENHETER)
    assert len({e.orgnr for e in brreg.DEMO_ENHETER}) == len(brreg.DEMO_ENHETER)


# ============================ drift: oppslag ============================

def test_enhet_med_postadresse(drift):
    drift.svar["/enheter/912345688"] = (200, _enhet(postadresse=_adr("Postboks 7"),
                                                    forretningsadresse=_adr("Storgata 1", postnr="0101", sted="OSLO")))
    assert brreg.hent("912 345 688") == brreg.Enhet("912345688", "EKSEMPEL AS", "Postboks 7", "1234", "EKSEMPELBY")
    sti, params, headers, timeout = drift.kall[0]
    assert sti == "/enheter/912345688" and headers["Accept"] == "application/json" and timeout == brreg.TIDSAVBRUDD_SEK


def test_forretningsadresse_naar_postadressen_mangler_gate(drift):
    drift.svar["/enheter/912345688"] = (200, _enhet(postadresse=_adr(postnr="9999", sted="POSTBY"),
                                                    forretningsadresse=_adr("Storgata 1", "2. etasje")))
    assert brreg.hent("912345688").adresse == "Storgata 1, 2. etasje"


def test_underenhet_naar_enheten_ikke_finnes(drift):
    drift.svar["/underenheter/912345688"] = (200, _enhet(navn="EKSEMPEL AS AVDELING NORD", postadresse=_adr(),
                                                         beliggenhetsadresse=_adr("Nordveien 2", postnr="8000", sted="NORD")))
    assert brreg.hent("912345688") == brreg.Enhet("912345688", "EKSEMPEL AS AVDELING NORD", "Nordveien 2", "8000", "NORD",
                                                  underenhet=True)
    assert [k[0] for k in drift.kall] == ["/enheter/912345688", "/underenheter/912345688"]


def test_utenlandsk_adresse_faar_land(drift):
    drift.svar["/enheter/912345688"] = (200, _enhet(postadresse=_adr("Storgatan 1", postnr="11122", sted="STOCKHOLM",
                                                                     landkode="SE", land="Sverige")))
    assert brreg.hent("912345688").adresse == "Storgatan 1, Sverige"


@pytest.mark.parametrize("enhet_svar", [(404, {}), (410, {"slettedato": "2020-01-01"}),
                                        (200, _enhet(slettedato="2020-01-01", postadresse=_adr("Gata 1")))])
def test_finnes_ikke_eller_slettet_gir_none_og_huskes(drift, enhet_svar):
    drift.svar["/enheter/912345688"] = enhet_svar
    assert brreg.hent("912345688") is None
    antall = len(drift.kall)
    assert brreg.hent("912345688") is None and len(drift.kall) == antall     # husket - ingen nye kall


def test_ugyldig_nummer_sendes_aldri_til_registeret(drift):
    assert brreg.hent("912345687") is None and brreg.hent("../enheter") is None and drift.kall == []


@pytest.mark.parametrize("svar", [(500, {}), (503, {}), (429, {}), (requests.Timeout("tidsavbrudd"), None),
                                  (requests.ConnectionError("nede"), None), (200, b"<html>ikke json</html>"),
                                  (200, {"navn": "UTEN NUMMER"}), (200, ["liste"]), (403, {})])
def test_registeret_svarer_ikke_gir_utilgjengelig_uten_detaljer(drift, svar):
    drift.svar["/enheter/912345688"] = svar
    with pytest.raises(brreg.Utilgjengelig) as e:
        brreg.hent("912345688")
    assert "912345688" not in str(e.value) and "data.brreg.no" not in str(e.value)


def test_utilgjengelig_huskes_ikke(drift):
    drift.svar["/enheter/912345688"] = (503, {})
    with pytest.raises(brreg.Utilgjengelig):
        brreg.hent("912345688")
    drift.svar["/enheter/912345688"] = (200, _enhet(postadresse=_adr("Gata 1")))
    assert brreg.hent("912345688").navn == "EKSEMPEL AS"


def test_funnet_huskes(drift):
    drift.svar["/enheter/912345688"] = (200, _enhet(postadresse=_adr("Gata 1")))
    brreg.hent("912345688")
    brreg.hent("912345688")
    assert len(drift.kall) == 1


# ============================ drift: søk ============================

def _liste(nokkel, *navn):
    return {"_embedded": {nokkel: [_enhet(orgnr=f"91234567{i}", navn=n, postadresse=_adr("Gata 1"))
                                   for i, n in enumerate(navn)]}, "page": {"totalElements": len(navn)}}


def test_sok_gir_enheter_foer_underenheter(drift):
    drift.svar["/enheter"] = (200, _liste("enheter", "EKSEMPEL AS", "EKSEMPEL BA"))
    drift.svar["/underenheter"] = (200, _liste("underenheter", "EKSEMPEL AS AVD NORD"))
    treff = brreg.sok("  eksempel  ")
    assert [(e.navn, e.underenhet) for e in treff] == [("EKSEMPEL AS", False), ("EKSEMPEL BA", False),
                                                       ("EKSEMPEL AS AVD NORD", True)]
    assert [(k[0], k[1]) for k in drift.kall] == [("/enheter", {"navn": "eksempel", "size": 8}),
                                                  ("/underenheter", {"navn": "eksempel", "size": 8})]


def test_sok_gir_hoyst_ti_treff(drift):
    drift.svar["/enheter"] = (200, _liste("enheter", *(f"FIRMA {i}" for i in range(8))))
    drift.svar["/underenheter"] = (200, _liste("underenheter", *(f"AVD {i}" for i in range(9))))
    treff = brreg.sok("firma")
    assert len(treff) == brreg.MAKS_TREFF and drift.kall[1][1]["size"] == 2


def test_kort_sok_gaar_aldri_til_registeret(drift):
    assert brreg.sok("ab") == [] and brreg.sok(None) == [] and drift.kall == []


def test_sok_uten_treff_og_400(drift):
    drift.svar["/enheter"] = (200, {"page": {"totalElements": 0}})
    drift.svar["/underenheter"] = (400, {"feilmelding": "ugyldig"})
    assert brreg.sok("finnes ikke") == []


def test_sok_huskes_og_feil_gir_utilgjengelig(drift):
    drift.svar["/enheter"] = (200, _liste("enheter", "EKSEMPEL AS"))
    drift.svar["/underenheter"] = (200, _liste("underenheter"))
    brreg.sok("Eksempel")
    brreg.sok("eksempel")
    assert len(drift.kall) == 2
    drift.svar["/enheter"] = (500, {})
    with pytest.raises(brreg.Utilgjengelig):
        brreg.sok("noe annet")


# ============================ python -m kurs.brreg_sjekk ============================

@pytest.fixture
def ekte(monkeypatch):
    """Kommandoen bruker ALLTID det ekte registeret - også i demo. Her etterlignet."""
    monkeypatch.setattr(config, "DEMO", True)
    falsk = FalsktRegister()
    monkeypatch.setattr(requests, "get", falsk)
    return falsk


def test_sjekk_viser_navn_og_fakturaadresse(ekte, capsys):
    ekte.svar["/enheter/912345688"] = (200, _enhet(postadresse=_adr("Postboks 7")))
    assert brreg_sjekk.main(["912 345 688"]) == 0
    ut = capsys.readouterr().out
    assert "EKSEMPEL AS" in ut and "Postboks 7, 1234 EKSEMPELBY" in ut and ekte.kall


@pytest.mark.parametrize("argv,svar,kode,tekst", [
    (["912345687"], None, 2, "Ugyldig organisasjonsnummer"),
    (["912345688"], (404, {}), 1, "Fant ikke 912345688"),
    (["912345688"], (503, {}), 3, "svarte ikke"),
    (["--sok", "ab"], None, 2, "minst 3 tegn"),
])
def test_sjekk_feil(ekte, capsys, argv, svar, kode, tekst):
    if svar:
        ekte.svar["/enheter/912345688"] = svar
    assert brreg_sjekk.main(argv) == kode
    assert tekst in capsys.readouterr().out


def test_sjekk_sok(ekte, capsys):
    ekte.svar["/enheter"] = (200, _liste("enheter", "EKSEMPEL AS"))
    ekte.svar["/underenheter"] = (200, _liste("underenheter", "EKSEMPEL AS AVD"))
    assert brreg_sjekk.main(["--sok", "eksempel"]) == 0
    ut = capsys.readouterr().out
    assert "2 treff" in ut and "(underenhet)" in ut


# ============================ BRREG_LIVE: ekte oppslag i demo (bare lokal prøving) ============================

@pytest.fixture
def demo_live(monkeypatch):
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "BRREG_LIVE", True)
    falsk = FalsktRegister({
        "/enheter/912345688": (200, _enhet("912345688", "EKSEMPEL FIRMA AS", forretningsadresse=_adr("Eksempelgata 4"))),
        "/enheter": (200, {"_embedded": {"enheter": [_enhet("912345688", "EKSEMPEL FIRMA AS",
                                                            forretningsadresse=_adr("Eksempelgata 4"))]}}),
        "/underenheter": (200, {"_embedded": {"underenheter": []}}),
    })
    monkeypatch.setattr(requests, "get", falsk)
    return falsk


def test_demo_uten_brreg_live_gjor_aldri_nettverkskall_og_kjenner_bare_de_oppdiktede(monkeypatch):
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "BRREG_LIVE", False)
    falsk = FalsktRegister({"/enheter/912345688": (200, _enhet())})
    monkeypatch.setattr(requests, "get", falsk)
    assert brreg.hent("912345688") is None and brreg.hent("999900003").navn == "EKSEMPEL KOMMUNE"
    assert [e.navn for e in brreg.sok("eksempel firma")] == []
    assert falsk.kall == []


@pytest.mark.parametrize("tekst", ["912345688", "912 345 688", " 91 23 45 68 8 ", "912.345.688", "NO912345688MVA"])
def test_brreg_live_i_demo_slaar_opp_ekte_nummer_uansett_mellomrom_og_tegnsetting(demo_live, tekst):
    e = brreg.hent(tekst)
    assert (e.orgnr, e.navn, e.adresse) == ("912345688", "EKSEMPEL FIRMA AS", "Eksempelgata 4")
    assert [k[0] for k in demo_live.kall] == ["/enheter/912345688"]


def test_brreg_live_i_demo_bruker_de_oppdiktede_uten_nettverk_og_nede_nummeret_er_fortsatt_nede(demo_live):
    assert brreg.hent("999900003").navn == "EKSEMPEL KOMMUNE"
    with pytest.raises(brreg.Utilgjengelig):
        brreg.hent(brreg.DEMO_NEDE_ORGNR)
    assert brreg.hent_paa_nytt(brreg.DEMO_NEDE_ORGNR) == brreg.DEMO_OPPE_IGJEN
    assert demo_live.kall == []


def test_brreg_live_i_demo_ugyldig_nummer_gjor_ikke_nettverkskall(demo_live):
    assert brreg.hent("912345675") is None and brreg.hent("12345") is None and brreg.hent("") is None
    assert demo_live.kall == []


def test_brreg_live_i_demo_ukjent_nummer_gir_ikke_funnet(demo_live):
    assert brreg.hent("999900070") is None                     # gyldig, finnes ikke: 404 fra det etterlignede registeret
    assert [k[0] for k in demo_live.kall] == ["/enheter/999900070", "/underenheter/999900070"]


def test_brreg_live_i_demo_soek_gir_oppdiktede_foerst_saa_ekte_uten_dubletter(demo_live):
    navn = [e.navn for e in brreg.sok("eksempel")]
    assert navn[-1] == "EKSEMPEL FIRMA AS" and "EKSEMPEL KOMMUNE" in navn and len(navn) == len(set(navn))
    assert navn.index("EKSEMPEL KOMMUNE") < navn.index("EKSEMPEL FIRMA AS")


def test_brreg_live_i_demo_uten_svar_gir_utilgjengelig(monkeypatch):
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "BRREG_LIVE", True)
    monkeypatch.setattr(requests, "get", FalsktRegister({"/enheter/912345688": (requests.ConnectionError("nede"), None)}))
    with pytest.raises(brreg.Utilgjengelig):
        brreg.hent("912345688")
