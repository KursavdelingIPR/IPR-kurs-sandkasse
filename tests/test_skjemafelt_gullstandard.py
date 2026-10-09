"""Fase 12C1: GULLSTANDARD for den offentlige paameldingssiden (kurs.html) - laast FOER skjemafelt-registeret ble innfort.

Refaktoreringen i 12C1 (kurs/skjemafelt.py) skal IKKE endre det deltakeren ser eller det serveren gjor. Beviset er
et DOM-oyeblikksbilde (tests/gullstandard/paamelding_skjema.json) tatt av den UENDREDE koden, for representative
kurs- og innsendingskombinasjoner. Testen sammenligner den naavaerende siden mot dette bildet.

Hvorfor DOM og ikke byte-for-byte: Jinja-kontrollstrukturer ({% for %} / {% if %}) etterlater ulik mengde mellomrom og
linjeskift, uten at det har noen betydning i nettleseren. DOM-bildet tar med ALT som betyr noe: hver tag i
dokumentrekkefolge, ALLE attributter med verdi og i kilderekkefolge (name, required, value, checked, onchange, class,
id, type, href ...) og all synlig tekst (mellomrom normalisert). Kun rene mellomrom mellom tagger faller bort.

I tillegg er det egne, lesbare semantiske tester (feltrekkefolge, required, gjenutfylte verdier) som ikke er avhengige
av oyeblikksbildet.

Oyeblikksbildet genereres KUN med _generer() - aldri automatisk fra en test.

Skjemabyggeren (migrering 9) endret siden med hensikt (feltnavn til venstre, «(krav)», informasjonsboks, fakturadelen
med organisasjonsnummer fra Enhetsregisteret, forhåndsvisning = den ekte siden). Bildet ble derfor laget på nytt med
_generer() 28.09.2026, og de semantiske testene under beskriver den nye siden.

Omleggingen 01.10.2026 tok «Kurs» og «Innsjekk» ut av menyen på deltakersidene og gjorde logoen til vanlig tekst (startsiden er
nå innloggingen til Admin). Bildet ble laget på nytt med _generer() samme dag; de eneste forskjellene mot forrige bilde var disse
linjene, i alle 15 scenarier, og ingenting i selve påmeldingsskjemaet.

Terapiakademiet-drakten kom på påmeldingssiden samme kveld (Camilla: «Påmeldingssiden kan få samme stil som terapiakademiet»). Bildet ble laget på
nytt med _generer() KJØRT INNE I PYTEST (en vanlig python-kjøring mangler testklientens CSRF-nøkkel og standardadressen fra conftest.py, og gir da et
helt annet bilde av POST-scenariene). De eneste forskjellene mot forrige bilde, i alle 15 scenarier: `<body data-tema='ta'>`, lenken til
tema-terapiakademiet.css, logoen (to bilder) i stedet for teksten «IPR Påmeldingssystem» i toppen, og bunnen (`footer.ta-bunn`). Ingenting i selve
skjemaet og ingen statuskoder er endret.

Senere samme kveld ba Camilla om at administratorbanneret «Dette er det ekte påmeldingsskjemaet» (lenke, «Kopier lenke», «Rediger skjemaet») skulle bort
fra påmeldingssiden. Bildet ble laget på nytt (igjen inne i pytest); eneste forskjell er at banneret er borte i scenarioet «forhandsvisning» (en administrator
som ser et åpent kurs). Banneret for et kurs som ikke er åpent («forhandsvisning_utkast») står fortsatt.

05.10.2026 (Camilla) ble bildet laget på nytt (inne i pytest) for fire tilsiktede endringer: «Bekreft e-post» etter e-post, lagrings-samtykket
(«Jeg aksepterer at Institutt for Psykologisk Rådgivning lagrer …») etter vilkårene, Yrkestittel synlig som standard, EHF-avkrysningen skjult som
standard, og kursdagene som «1. samling: dato» med ett felles «Tidspunkt: 09.00–16.00 alle dager». Ingen statuskoder er endret.
07.10.2026 (Camilla: «fange opp ting som kan virke feil, som feks hvis noen skriver gmal.com») ble bildet laget på nytt (inne i pytest, med
HPR slått på som i testene). Eneste forskjell, i 14 av 15 scenarier: e-postfeltet og (der fakturadelen finnes) «E-post for faktura» har fått
attributtet `data-epost-sjekk` (domenelistene til «Mente du …?», kurs/datakontroll.py). Ingenting annet i skjemaet og ingen statuskoder er endret.

09.10.2026 (Camilla: kvitteringen skal si at bekreftelsen er sendt til e-postadressen som ble registrert, og hvem de kontakter hvis den ikke
kommer) ble bildet laget på nytt (inne i pytest, med HPR). Eneste forskjell: kvitteringsteksten i scenarioet
«post_skjult_fakturablokk_ignorerer_manipulerte_felt» (det eneste som viser kvitteringen). Påmeldingsskjemaet og statuskodene er uendret.
"""
import json
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

import pytest

from kurs import config, db

SNAPSHOT = Path(__file__).parent / "gullstandard" / "paamelding_skjema.json"

DAG1, DAG2 = "2099-03-02", "2099-03-03"   # faste kursdatoer
# Siden ER datoavhengig: demo-banneret i base.html viser «Systemdato: {{ idag }}» (og kursside() bruker _idag()).
# Oyeblikksbildet ble tatt 2026-09-23, saa baade testene og _generer() fryser app-datoen dit via seamen
# kurs.web.app._idag - ellers feiler gullstandarden bare fordi kalenderen har gaatt.
FAST_DATO = date(2026, 9, 23)

# 12C3: tidligere "post_feil_org_uten_blokk" (400). Tilsiktet endret til normal suksess - se lag_sider().
SKJULT_FAKTURABLOKK = "post_skjult_fakturablokk_ignorerer_manipulerte_felt"


# ============================ DOM-uttrekk ============================

# HPR-nummer er slått av i skjemaene (skjemafelt.HPR_I_SKJEMA = False, Camilla 02.10.2026). Disse testene gjelder koden som viser, validerer og lagrer det
# (den finnes fortsatt og kan slås på igjen), så de slår det på. Standarden (av) testes i test_hpr_nummer_samles_ikke_inn.py.
pytestmark = pytest.mark.usefixtures("hpr_i_skjema")


class _Dom(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tokens: list[str] = []
        self.inputs: list[tuple[str, dict]] = []
        self._tekstfelt: dict | None = None

    def handle_starttag(self, tag, attrs):
        # Tilfeldige sikkerhetsverdier (CSRF-token, CSP-nonce) er ulike per request og maskeres - alt annet tas med.
        a = dict(attrs)
        if tag == "input" and a.get("name") == "csrf_token":
            attrs = [(k, "<csrf>" if k == "value" else v) for k, v in attrs]
        if tag == "script":
            attrs = [(k, "<nonce>" if k == "nonce" else v) for k, v in attrs]
        if tag == "style":
            self._i_stil = True
        if tag in ("input", "textarea", "select"):         # alle skjemafelt (textarea: verdien er innholdet)
            self.inputs.append((a.get("name"), dict(attrs)))
            if tag == "textarea":
                self._tekstfelt = self.inputs[-1][1]
                self._tekstfelt["value"] = ""
        self.tokens.append("<" + " ".join([tag, *(f"{k}={v!r}" if v is not None else k for k, v in attrs)]) + ">")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    _i_stil = False

    def handle_endtag(self, tag):
        if tag == "style":
            self._i_stil = False
        if tag == "textarea":
            self._tekstfelt = None
        self.tokens.append(f"</{tag}>")

    def handle_data(self, data):
        if self._i_stil:           # felles CSS i base.html er ikke en del av skjemaets gullstandard
            return
        if self._tekstfelt is not None:
            self._tekstfelt["value"] += data
        tekst = " ".join(data.split())
        if tekst:
            self.tokens.append("#" + tekst)


def _parse(html: str) -> _Dom:
    p = _Dom()
    p.feed(html)
    p.close()
    return p


def dom(html: str) -> list[str]:
    return _parse(html).tokens


def skjemafelt_i_rekkefolge(html: str) -> list[tuple[str, dict]]:
    """(name, attributter) for alle skjemafelt (input, textarea, select) paa siden, i dokumentrekkefolge - uten
    CSRF-feltet og felt uten name (lenkefeltet i adminlinjen), som ikke er skjemafelt deltakeren fyller ut."""
    return [(n, a) for n, a in _parse(html).inputs if n and n != "csrf_token"]


# ============================ scenarier ============================

def _kurs(con, kode, **kw):
    felter = {"navn": "Eksempelkurs", "type": "fysisk", "sted": "Eksempelsted", "pris_nok": 4500,
              "fakturering": "person", "betaling": "samlet", "kapasitet": 10, **kw}
    return db.opprett_kurs(con, kode=kode, datoer=[DAG1, DAG2], sharepoint_mappe=f"K/{kode}", **felter)


# Fiktive testdata (ingen ekte personer). Spesialtegn for aa bevise at gjenutfylte verdier fortsatt escapes.
INNSENDT = {
    "fornavn": 'Test "Person" <Eksempel>', "etternavn": "Eksempelsen", "epost": "test.person@eksempel.no",
    "telefon": "+47 000 00 000",
    "arbeidssted": "Eksempel & Co AS", "hpr_nr": "0000000", "betaler": "organisasjon",
    # 999999999: oppdiktet demovirksomhet (kurs/integrasjoner/brreg.py) - gyldig kontrollsiffer, ingen nettverkskall
    "org_navn": "Eksempel Org", "org_nr": "999999999", "faktura_ref": "REF-1", "faktura_epost": "faktura@eksempel.no",
    "ehf": "on", "betaling": "per_samling", "adresse": "Eksempelveien 1", "postnr": "0000",
    "poststed": "Eksempelby", "allergier": "Eksempelallergi", "tilrettelegging": "Eksempelbehov",
}

GET_SCENARIER = {
    # navn -> kursoppsett
    "fysisk_uten_spesialistlop": dict(),
    "fysisk_med_spesialistlop": dict(spesialistlop="EFT"),
    "digitalt": dict(type="digital", sted=None),
    "hybrid": dict(type="hybrid"),
    "person_pris_deltaker_velger": dict(betaling="deltaker_velger"),
    "person_pris_per_samling": dict(betaling="per_samling"),
    "uten_fakturablokk_organisasjon": dict(fakturering="organisasjon"),
    "uten_fakturablokk_ingen": dict(fakturering="ingen"),
    "uten_fakturablokk_gratis": dict(pris_nok=0),
    "ubegrenset_kapasitet": dict(kapasitet=None),
}


def _klient():
    from kurs.web import app as webapp
    return webapp.app.test_client()


def _innlogget():
    k = _klient()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def lag_sider(con) -> dict[str, tuple[int, str]]:
    """Alle scenarier -> (statuskode, html). Brukes baade av testene og av _generer()."""
    sider = {}
    for i, (navn, oppsett) in enumerate(GET_SCENARIER.items()):
        _kurs(con, f"G{i}", **oppsett)
    kid_preview = _kurs(con, "PV", spesialistlop="EFT", betaling="deltaker_velger")
    kid_utkast = _kurs(con, "PU", spesialistlop="EFT", betaling="deltaker_velger", status="utkast")
    _kurs(con, "PF1", spesialistlop="EFT", betaling="deltaker_velger")
    kid_pf2 = _kurs(con, "PF2", spesialistlop="EFT")
    _kurs(con, "PF3", type="digital", sted=None, fakturering="organisasjon")
    db.meld_paa(con, kid_pf2, epost=INNSENDT["epost"], fornavn="Tidligere", etternavn="Test", paamelding={}, sensitivt={})
    con.commit()

    k = _klient()
    for i, navn in enumerate(GET_SCENARIER):
        r = k.get(f"/kurs/G{i}")
        sider[navn] = (r.status_code, r.get_data(as_text=True))

    # Forhåndsvisningen er den ekte siden: et offentlig kurs sett av admin, og et utkast (bare admin, uten innsending)
    admin = _innlogget()
    r = admin.get(f"/admin/kurs/{kid_preview}/forhandsvis-paamelding", follow_redirects=True)
    sider["forhandsvisning"] = (r.status_code, r.get_data(as_text=True))
    r = admin.get(f"/admin/kurs/{kid_utkast}/forhandsvis-paamelding", follow_redirects=True)
    sider["forhandsvisning_utkast"] = (r.status_code, r.get_data(as_text=True))

    # POST-feil 1: valideringsfeil (mangler samtykke) - alle felt fylt ut, verdiene skal vises igjen
    r = _klient().post("/kurs/PF1", data=INNSENDT)
    sider["post_feil_mangler_samtykke"] = (r.status_code, r.get_data(as_text=True))
    # POST-feil 2: Paameldingsfeil fra meld_paa (allerede paameldt) - andre feilsti i kursside()
    r = _klient().post("/kurs/PF2", data={**INNSENDT, "samtykke": "on", "samtykke_lagring": "on", "betaling": "samlet"})
    sider["post_feil_allerede_paameldt"] = (r.status_code, r.get_data(as_text=True))
    # Manipulert POST mot et digitalt kurs UTEN synlig fakturablokk: betaler=organisasjon uten org.nr. (+ alle andre
    # faktura-/sensitive felt). Fram til 12C3 het dette scenariet "post_feil_org_uten_blokk" og ga 400 (org.nr.-krav).
    # 12C3 (tilsiktet sikkerhetsherding, godkjent): hele den skjulte blokken ignoreres server-side -> normal kvittering.
    r = _klient().post("/kurs/PF3", data={**INNSENDT, "samtykke": "on", "samtykke_lagring": "on", "org_nr": ""})
    sider[SKJULT_FAKTURABLOKK] = (r.status_code, r.get_data(as_text=True))
    return sider


@pytest.fixture
def con(tmp_path, monkeypatch):
    from kurs.web import app as webapp
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    monkeypatch.setattr(webapp, "_idag", lambda: FAST_DATO)
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


@pytest.fixture
def sider(con):
    return lag_sider(con)


def _generer(sti: Path = SNAPSHOT) -> None:
    """Skriver oyeblikksbildet paa nytt. KUN manuelt, og KUN naar en endring i paameldingssiden er tilsiktet."""
    import tempfile

    from kurs.web import app as webapp
    ekte_idag = webapp._idag
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        config.UTBOKS, config.DEMO, config.DB_STI = tmp / "utboks", True, tmp / "test.db"
        webapp._idag = lambda: FAST_DATO       # samme faste dato som testene
        c = db.koble(config.DB_STI)
        db.init(c)
        try:
            data = {navn: {"status": s, "dom": dom(html)} for navn, (s, html) in lag_sider(c).items()}
        finally:
            c.close()
            webapp._idag = ekte_idag
    sti.parent.mkdir(parents=True, exist_ok=True)
    sti.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


# ============================ gullstandard ============================

def test_oyeblikksbildet_dekker_alle_scenarier():
    lagret = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert set(lagret) == set(GET_SCENARIER) | {"forhandsvisning", "forhandsvisning_utkast",
                                                "post_feil_mangler_samtykke", "post_feil_allerede_paameldt",
                                                SKJULT_FAKTURABLOKK}


@pytest.mark.parametrize("scenario", [*GET_SCENARIER, "forhandsvisning", "forhandsvisning_utkast",
                                      "post_feil_mangler_samtykke", "post_feil_allerede_paameldt",
                                      SKJULT_FAKTURABLOKK])
def test_siden_er_dom_identisk_med_gullstandard(sider, scenario):
    lagret = json.loads(SNAPSHOT.read_text(encoding="utf-8"))[scenario]
    status, html = sider[scenario]
    assert status == lagret["status"]
    assert dom(html) == lagret["dom"]


def test_gullstandarden_fanger_endret_feltrekkefolge(con, monkeypatch):
    """Kontroll av selve testen: en endring i registeret SKAL gi avvik mot oyeblikksbildet."""
    import dataclasses
    from types import MappingProxyType

    from kurs import skjemafelt
    # 12C2: rekkefolgen styres av standard `rekkefolge` i registeret (tuppelindeksen er kun tie-breaker) - muter den.
    reg = dict(skjemafelt.REGISTER)
    reg["telefon"] = dataclasses.replace(reg["telefon"], rekkefolge=3)
    monkeypatch.setattr(skjemafelt, "REGISTER", MappingProxyType(reg))
    lagret = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert dom(lag_sider(con)["fysisk_uten_spesialistlop"][1]) != lagret["fysisk_uten_spesialistlop"]["dom"]


def test_gullstandarden_fanger_endret_blokkbetingelse(con, monkeypatch):
    from kurs import skjemafelt
    monkeypatch.setattr(skjemafelt, "vis_fakturablokk", lambda kurs: True)
    lagret = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert dom(lag_sider(con)["uten_fakturablokk_gratis"][1]) != lagret["uten_fakturablokk_gratis"]["dom"]


# ============================ semantiske regresjonstester ============================

# Fakturadelen: hvem betaler, organisasjonsnummer og firmaopplysningene (fra Enhetsregisteret) og fakturafeltene når
# firma betaler - deretter ev. valg av betalingsmåte. Betaler deltakeren selv, er fakturaadressen hans egen adresse
# (ADRESSE under «Om deg»), så den har ingen egne felt her.
FAKTURA = ["betaler", "betaler", "org_nr", "org_navn",
           "org_adresse", "org_postnr", "org_sted", "faktura_ref", "faktura_epost", "faktura_kommentar"]
FAKTURA_VELGER = FAKTURA + ["betaling", "betaling"]
SENSITIVT = ["allergier", "tilrettelegging"]


NAVN = ["fornavn", "etternavn"]       # egne felt (migrering 7) - tidligere ett "navn"-felt
ADRESSE = ["adresse", "postnr", "poststed"]     # deltakerens private adresse: alltid med, rett etter e-post
# 05.10.2026 (Camilla): «Bekreft e-post» rett etter e-post, lagrings-samtykket rett etter vilkårene, og yrkestittel vises
# som standard. EHF-avkrysningen er skjult som standard (sendes automatisk når mottakeren kan ta imot EHF).
EPOST = ["epost", "epost_bekreft"]
SAMTYKKE = ["samtykke", "samtykke_lagring"]


@pytest.mark.parametrize("scenario,forventet", [
    ("fysisk_uten_spesialistlop", [*NAVN, *EPOST, *ADRESSE, "telefon", "arbeidssted", "yrkestittel", *FAKTURA, *SENSITIVT, *SAMTYKKE]),
    ("fysisk_med_spesialistlop", [*NAVN, *EPOST, *ADRESSE, "telefon", "arbeidssted", "yrkestittel", "hpr_nr", *FAKTURA, *SENSITIVT,
                                  *SAMTYKKE]),
    ("digitalt", [*NAVN, *EPOST, *ADRESSE, "telefon", "arbeidssted", "yrkestittel", *FAKTURA, *SAMTYKKE]),
    ("hybrid", [*NAVN, *EPOST, *ADRESSE, "telefon", "arbeidssted", "yrkestittel", *FAKTURA, *SENSITIVT, *SAMTYKKE]),
    ("person_pris_deltaker_velger", [*NAVN, *EPOST, *ADRESSE, "telefon", "arbeidssted", "yrkestittel", *FAKTURA_VELGER, *SENSITIVT,
                                     *SAMTYKKE]),
    ("person_pris_per_samling", [*NAVN, *EPOST, *ADRESSE, "telefon", "arbeidssted", "yrkestittel", *FAKTURA, *SENSITIVT, *SAMTYKKE]),
    ("uten_fakturablokk_organisasjon", [*NAVN, *EPOST, *ADRESSE, "telefon", "arbeidssted", "yrkestittel", *SENSITIVT, *SAMTYKKE]),
    ("uten_fakturablokk_ingen", [*NAVN, *EPOST, *ADRESSE, "telefon", "arbeidssted", "yrkestittel", *SENSITIVT, *SAMTYKKE]),
    ("uten_fakturablokk_gratis", [*NAVN, *EPOST, *ADRESSE, "telefon", "arbeidssted", "yrkestittel", *SENSITIVT, *SAMTYKKE]),
    ("forhandsvisning", [*NAVN, *EPOST, *ADRESSE, "telefon", "arbeidssted", "yrkestittel", "hpr_nr", *FAKTURA_VELGER, *SENSITIVT,
                         *SAMTYKKE]),
    ("forhandsvisning_utkast", [*NAVN, *EPOST, *ADRESSE, "telefon", "arbeidssted", "yrkestittel", "hpr_nr", *FAKTURA_VELGER, *SENSITIVT,
                                *SAMTYKKE]),
])
def test_feltrekkefolge_per_kursoppsett(sider, scenario, forventet):
    assert [n for n, _ in skjemafelt_i_rekkefolge(sider[scenario][1])] == forventet


def test_kun_navn_epost_adresse_betaler_og_samtykke_er_required(sider):
    """Paa ALLE sider som viser paameldingsskjemaet (alle scenarier unntatt suksessflyten) er fornavn, etternavn, epost,
    adresse, postnummer, poststed og samtykke - og «Betale privat / firma» naar fakturadelen vises - de eneste
    required-feltene naar det ikke finnes overstyringer. Felt i deler som er skjult (f.eks. firmadelen foer «Firma betaler» er valgt), kreves aldri."""
    skjemasider = {s for s, (_, html) in sider.items() if any(n == "fornavn" for n, _ in skjemafelt_i_rekkefolge(html))}
    assert skjemasider == set(sider) - {SKJULT_FAKTURABLOKK}      # testen svekkes ikke: kun kvitteringen er unntatt
    for scenario in skjemasider:
        felt = skjemafelt_i_rekkefolge(sider[scenario][1])
        betaler = ["betaler", "betaler"] if any(n == "betaler" for n, _ in felt) else []
        required = [n for n, a in felt if "required" in a]
        assert required == [*NAVN, *EPOST, *ADRESSE, *betaler, *SAMTYKKE], scenario


def test_skjult_fakturablokk_ignorerer_manipulerte_felt_og_gir_normal_kvittering(sider):
    """12C3: manipulerte faktura-/organisasjonsfelt mot et kurs uten synlig fakturablokk utloser INGEN org.nr.-validering;
    en ellers gyldig paamelding gjennomfores som normal suksess (kvittering, ikke skjema)."""
    status, html = sider[SKJULT_FAKTURABLOKK]
    assert status == 200
    assert "Du er påmeldt!" in html and "Takk, Test &#34;Person&#34; &lt;Eksempel&gt;." in html
    assert "Fyll inn organisasjon" not in html and skjemafelt_i_rekkefolge(html) == []


def test_deltakerfeltene_har_feltnavn_til_venstre_og_riktig_felttype(sider):
    html = sider["fysisk_med_spesialistlop"][1]
    t = dom(html)
    for label, navn, ekstra in [("Telefon", "telefon", " type='tel' autocomplete='tel'"),
                                ("Arbeidssted", "arbeidssted", " autocomplete='organization'"),
                                ("HPR-nummer", "hpr_nr", "")]:
        i = t.index("#" + label)
        assert t[i - 2:i + 6] == ["<div class='pm-rad'>", f"<label class='pm-etikett' for='f-{navn}'>", "#" + label,
                                  "</label>", "<div class='pm-felt'>", f"<input id='f-{navn}' name={navn!r}{ekstra} value=''>",
                                  "</div>", "</div>"]


def test_post_feil_viser_innsendte_verdier_igjen_escaped(sider):
    status, html = sider["post_feil_mangler_samtykke"]
    assert status == 400
    verdier = {n: a.get("value") for n, a in skjemafelt_i_rekkefolge(html) if a.get("type") not in ("radio", "checkbox")}
    for felt in ("fornavn", "etternavn", "epost", "telefon", "arbeidssted", "hpr_nr", "org_navn", "org_nr", "faktura_ref",
                 "faktura_epost", "adresse", "postnr", "poststed", "allergier", "tilrettelegging"):
        assert verdier[felt] == INNSENDT[felt], felt
    assert "Eksempel &amp; Co AS" in html and "&lt;Eksempel&gt;" in html and "<Eksempel>" not in html
    assert "Du må godta vilkår og personvernerklæring." in html


def test_post_feil_fra_meld_paa_viser_verdier_igjen(sider):
    status, html = sider["post_feil_allerede_paameldt"]
    assert status == 400
    verdier = {n: a.get("value") for n, a in skjemafelt_i_rekkefolge(html) if a.get("type") not in ("radio", "checkbox")}
    assert verdier["telefon"] == INNSENDT["telefon"] and verdier["arbeidssted"] == INNSENDT["arbeidssted"]
    assert verdier["hpr_nr"] == INNSENDT["hpr_nr"]
    assert "Du er allerede påmeldt dette kurset" in html
