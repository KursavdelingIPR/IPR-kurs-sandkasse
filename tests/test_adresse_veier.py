"""Deltakerens PRIVATE adresse er påkrevd i ALLE veier inn (brukerens beslutning 29.09.2026): bedriftspåmelding, manuell
registrering (admin), CSV-import, webhooken og redigering i deltakervinduet. (Det offentlige skjemaet og selve registeret
testes i test_adresse.py; personvern, anonymisering og migrering i test_adresse_personvern.py.)

JUSTERT 30.09.2026 (se test_adresse_valgfri.py og test_adresse_paakrevd_overalt.py): for webhooken og CSV-importen er kravet en INNSTILLING
(config.ADRESSE_KREVES_I_WEBHOOK og config.ADRESSE_KREVES_I_CSV), PÅ som standard (adressen MÅ være med; «0» er en midlertidig nødbrems).
Testene her kjører med innstillingen PÅ uttrykkelig (fixturene `krav_webhook` og `krav_csv`), så en lokal .env ikke kan påvirke dem. I deltakervinduet kan en person som mangler adressen lagres uten den (test_adresse_valgfri.py);
her gjelder fortsatt at en adresse som er fylt ut må være komplett og ikke kan tømmes.

Felles for alle veiene:
  * mangler adressen (eller noen av delene), avvises innsendingen med en tydelig norsk melding, og INGENTING registreres
  * adressen lagres på personen
  * betaler deltakeren selv: personens adresse kopieres til fakturaadressen (fakturamotoren er uendret)
  * betaler firma: INGEN kopi - fakturaen går til firmaets adresse fra Enhetsregisteret
Alle testdata er fiktive."""
import hashlib
import hmac
import io
import json
import re
from datetime import date, timedelta

import pytest

from adressehjelp import ADRESSE, ANNEN_ADRESSE, SPORBAR_ADRESSE, gruppeadresse
from kurs import config, db, hendelseslogg
from kurs import import_deltakere as imp
from kurs.integrasjoner import brreg
from navnehjelp import gruppeskjema

pytestmark = pytest.mark.uten_standardadresse   # registrerer bevisst uten adresse (se conftest.py)

FIRMA = "999900003"          # EKSEMPEL KOMMUNE, Postboks 100, 1234 EKSEMPELBY (oppdiktet)
FIRMA_ADRESSE = {"adresse": "Postboks 100", "postnr": "1234", "poststed": "EKSEMPELBY"}
NEDE = brreg.DEMO_NEDE_ORGNR
EPOST = "ola@firma.no"
INGEN = {"adresse": None, "postnr": None, "poststed": None}


@pytest.fixture(autouse=True)
def _demo(monkeypatch):
    monkeypatch.setattr(config, "DEMO", True)


@pytest.fixture
def krav_webhook(monkeypatch):
    monkeypatch.setattr(config, "ADRESSE_KREVES_I_WEBHOOK", True)


@pytest.fixture
def krav_csv(monkeypatch):
    monkeypatch.setattr(config, "ADRESSE_KREVES_I_CSV", True)


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


def _person(con, epost=EPOST):
    return con.execute("SELECT * FROM deltaker WHERE epost=?", (epost,)).fetchone()


def _paamelding(con, epost=EPOST):
    return con.execute("SELECT p.* FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE d.epost=?",
                       (epost,)).fetchone()


def _adresse(rad, prefiks="") -> dict:
    return {"adresse": rad[f"{prefiks}adresse"], "postnr": rad[f"{prefiks}postnr"], "poststed": rad[f"{prefiks}poststed"]}


def _faktura(p) -> dict:
    return {"adresse": p["faktura_adresse"], "postnr": p["faktura_postnr"], "poststed": p["faktura_sted"]}


def _antall(con, tabell="deltaker"):
    return con.execute(f"SELECT COUNT(*) FROM {tabell}").fetchone()[0]


# ============================ bedriftspåmelding: adresse per deltaker ============================

def _gruppe(kode="A1", **over):
    data = gruppeskjema({
        "kontakt_navn": "Kari HR", "kontakt_epost": "kari.hr@firma.no", "kontakt_telefon": "90000000", "org_nr": FIRMA,
        "faktura_ref": "BEST-1", "deltaker_navn": ["Ola Nordmann"], "deltaker_epost": [EPOST],
        "deltaker_telefon": [""], "deltaker_arbeidssted": [""], "samtykke": "on", **over})
    return _klient().post(f"/kurs/{kode}/gruppe", data=data)


def test_bedriftspaamelding_viser_adressefeltene_for_hver_deltaker_ogsaa_i_malen_for_nye_rader(con):
    _kurs(con)
    html = _klient().get("/kurs/A1/gruppe").get_data(as_text=True)
    for felt in ("deltaker_adresse", "deltaker_postnr", "deltaker_poststed"):
        assert html.count(f'name="{felt}"') == 2, felt                # første deltakerrad + malen (<template>) for nye rader
    assert "Adresse *" in html and "Postnr. *" in html and "Poststed *" in html
    assert "private adresse" in html and "4 siffer i Norge" in html
    mal = html.split('<template id="rad-mal">')[1].split("</template>")[0]
    assert 'name="deltaker_adresse"' in mal and "value=" not in mal.split('name="deltaker_adresse"')[1].split(">")[0]


def test_bedriftspaamelding_avviser_deltaker_uten_adresse_og_registrerer_ingenting(con):
    _kurs(con)
    r = _gruppe(uten_adresse=True)
    assert r.status_code == 400
    assert "Deltaker 1: fyll inn adresse, postnummer og poststed." in r.get_data(as_text=True)
    assert _antall(con) == 0 and _antall(con, "paamelding") == 0 and _antall(con, "firmapaamelding") == 0


def test_bedriftspaamelding_nevner_riktig_deltaker_og_riktig_felt_og_alle_avvises_sammen(con):
    _kurs(con)
    r = _gruppe(deltaker_navn=["Ola Nordmann", "Per Hansen"], deltaker_epost=[EPOST, "per@firma.no"],
                deltaker_telefon=["", ""], deltaker_arbeidssted=["", ""],
                deltaker_adresse=["Vei 1", "Vei 2"], deltaker_postnr=["0150", ""], deltaker_poststed=["Oslo", "Oslo"])
    html = r.get_data(as_text=True)
    assert r.status_code == 400 and "Deltaker 2: fyll inn postnummer." in html and "Deltaker 1" not in html
    assert _antall(con) == 0                                            # heller ikke deltaker 1 (alt eller ingenting)
    assert 'value="Vei 1"' in html and 'value="Vei 2"' in html         # det som er skrevet står igjen i skjemaet


def test_bedriftspaamelding_avviser_for_lang_adresse(con):
    _kurs(con)
    r = _gruppe(deltaker_adresse=["A" * 201], deltaker_postnr=["0150"], deltaker_poststed=["Oslo"])
    assert r.status_code == 400 and "«Adresse» kan være maks 200 tegn." in r.get_data(as_text=True)
    assert _antall(con) == 0


def test_bedriftspaamelding_lagrer_hver_deltakers_egen_adresse_paa_personen_og_firma_faar_firmaadressen(con):
    _kurs(con)
    r = _gruppe(deltaker_navn=["Ola Nordmann", "Per Hansen"], deltaker_epost=[EPOST, "per@firma.no"],
                deltaker_telefon=["", ""], deltaker_arbeidssted=["", ""],
                deltaker_adresse=[ADRESSE["adresse"], ANNEN_ADRESSE["adresse"]],
                deltaker_postnr=[ADRESSE["postnr"], ANNEN_ADRESSE["postnr"]],
                deltaker_poststed=[ADRESSE["poststed"], ANNEN_ADRESSE["poststed"]])
    assert r.status_code == 302, r.get_data(as_text=True)[:300]
    assert _adresse(_person(con)) == ADRESSE and _adresse(_person(con, "per@firma.no")) == ANNEN_ADRESSE
    for epost in (EPOST, "per@firma.no"):        # firma betaler: fakturaen går til firmaets adresse, aldri privat
        p = _paamelding(con, epost)
        assert p["betaler"] == "organisasjon" and _faktura(p) == FIRMA_ADRESSE
    # Kontaktpersonens firmaopplysninger inneholder ingen privat adresse
    firma = con.execute("SELECT * FROM firmapaamelding").fetchone()
    assert ADRESSE["adresse"] not in " ".join(str(v) for v in dict(firma).values())
    assert (firma["faktura_adresse"], firma["faktura_postnr"], firma["faktura_sted"]) == tuple(FIRMA_ADRESSE.values())


def test_bedriftspaamelding_med_registeret_nede_kopierer_aldri_privat_adresse_til_faktura(con):
    _kurs(con)
    assert _gruppe(org_nr=NEDE).status_code == 302
    assert _adresse(_person(con)) == ADRESSE and _faktura(_paamelding(con)) == INGEN


def test_bedriftspaamelding_hopper_over_helt_tomme_rader_og_krever_adresse_paa_de_utfylte(con):
    _kurs(con)
    r = _gruppe(deltaker_navn=["Ola Nordmann", ""], deltaker_epost=[EPOST, ""], deltaker_telefon=["", ""],
                deltaker_arbeidssted=["", ""], deltaker_adresse=["Vei 1", ""], deltaker_postnr=["0150", ""],
                deltaker_poststed=["Oslo", ""])
    assert r.status_code == 302 and _antall(con) == 1


# ============================ manuell registrering (admin) ============================

def _ny(kid, **over):
    data = {"fornavn": "Ola", "etternavn": "Nordmann", "epost": EPOST, "betaler": "person", **over}
    return _admin().post(f"/admin/kurs/{kid}/deltaker/ny", data=data)


def test_manuell_registrering_viser_adressefeltene_som_krav(con):
    kid = _kurs(con)
    html = _admin().get(f"/admin/kurs/{kid}/deltaker/ny").get_data(as_text=True)
    for felt in ("adresse", "postnr", "poststed"):
        assert f'id="f-{felt}" name="{felt}" required' in html, felt
    assert "Adresse *" in html and "Postnummer *" in html and "Poststed *" in html
    assert "faktura_adresse" not in html and "faktura_postnr" not in html and "faktura_sted" not in html


@pytest.mark.parametrize("mangler", ("adresse", "postnr", "poststed"))
def test_manuell_registrering_avviser_manglende_adresse(con, mangler):
    kid = _kurs(con)
    label = {"adresse": "Adresse", "postnr": "Postnummer", "poststed": "Poststed"}[mangler]
    r = _ny(kid, **{k: v for k, v in ADRESSE.items() if k != mangler})
    assert r.status_code == 400 and f"Fyll inn «{label}»." in r.get_data(as_text=True)
    assert _antall(con) == 0 and _antall(con, "paamelding") == 0


def test_manuell_registrering_krever_adressen_ogsaa_naar_arbeidsgiver_betaler_og_paa_gratiskurs(con):
    kid = _kurs(con)
    assert _ny(kid, betaler="organisasjon", org_nr=FIRMA).status_code == 400
    gratis = _kurs(con, "G1", pris_nok=0, fakturering="ingen")
    assert _ny(gratis).status_code == 400 and _antall(con) == 0


def test_manuell_registrering_lagrer_adressen_paa_personen_og_kopierer_for_privat_betaler(con):
    kid = _kurs(con)
    assert _ny(kid, **ADRESSE).status_code == 302
    p = _paamelding(con)
    assert _adresse(_person(con)) == ADRESSE and _faktura(p) == ADRESSE
    assert p["betaler"] == "person" and p["sveiper_utsatt"] == 1 and p["kilde"] == "admin"


def test_manuell_registrering_for_firma_kopierer_ikke_adressen(con):
    kid = _kurs(con)
    assert _ny(kid, betaler="organisasjon", org_nr=FIRMA, **ADRESSE).status_code == 302
    p = _paamelding(con)
    assert _adresse(_person(con)) == ADRESSE and _faktura(p) == FIRMA_ADRESSE and p["betaler"] == "organisasjon"


def test_manuell_registrering_med_registeret_nede_gir_ingen_fakturaadresse(con):
    kid = _kurs(con)
    assert _ny(kid, betaler="organisasjon", org_nr=NEDE, **ADRESSE).status_code == 302
    assert _faktura(_paamelding(con)) == INGEN and _adresse(_person(con)) == ADRESSE


def test_manuell_registrering_ignorerer_gamle_faktura_adressefelt(con):
    kid = _kurs(con)
    assert _ny(kid, **ADRESSE, faktura_adresse="Manipveien 1", faktura_postnr="9999", faktura_sted="Manipby").status_code == 302
    assert _faktura(_paamelding(con)) == ADRESSE


# ============================ CSV-import ============================

HEADER = "Fornavn;Etternavn;E-post;Adresse;Postnr;Poststed;Betaler;Org.nr"
RUTE = "/admin/kurs/{kid}/deltakere/importer"
BEKREFT = "/admin/kurs/{kid}/deltakere/importer/bekreft"


def _csv(*rader, header=HEADER) -> bytes:
    return "\n".join([header, *rader]).encode("utf-8-sig")


def _last_opp(k, kid, innhold: bytes):
    return k.post(RUTE.format(kid=kid), data={"fil": (io.BytesIO(innhold), "import.csv")},
                  content_type="multipart/form-data")


def _importer(k, kid, innhold: bytes):
    """Forhåndsvis og bekreft. Returnerer (forhåndsvisningens tekst, bekreftelsens respons eller None)."""
    r = _last_opp(k, kid, innhold)
    html = r.get_data(as_text=True)
    m = re.search(r'name="forhaandsvisning_token" value="([^"]+)"', html)
    return html, (k.post(BEKREFT.format(kid=kid), data={"forhaandsvisning_token": m.group(1)}) if m else None)


def test_csv_uten_adressekolonnene_avvises_med_tydelig_melding(con, krav_csv):
    with pytest.raises(imp.ImportFeil) as e:
        imp.parse_csv(_csv("Kari;Test;kari@x.no", header="Fornavn;Etternavn;E-post"))
    tekst = str(e.value)
    assert "«Adresse», «Postnr» og «Poststed»" in tekst and "malfilen" in tekst
    with pytest.raises(imp.ImportFeil) as e:
        imp.parse_csv(_csv("Kari;Test;kari@x.no;Vei 1;0150", header="Fornavn;Etternavn;E-post;Adresse;Postnr"))
    assert str(e.value) == "Filen mangler den obligatoriske kolonnen «Poststed»."


def test_gammel_fil_med_fakturaadresse_virker_ikke_som_adresse(con, krav_csv):
    """«Fakturaadresse» er avviklet: den leses ikke, og kan derfor ikke gjøre at adressen ser ut til å finnes."""
    with pytest.raises(imp.ImportFeil, match="obligatoriske kolonnene"):
        imp.parse_csv(_csv("Kari;Test;kari@x.no;Gate 1", header="Fornavn;Etternavn;E-post;Fakturaadresse"))


def test_csv_kolonnenavnene_kan_skrives_med_alternative_navn(con):
    rader = imp.parse_csv(_csv("Kari;Test;kari@x.no;Vei 1;0150;Oslo", header="Fornavn;Etternavn;E-post;Adresse;Postnummer;Poststed"))
    assert rader == [{"fornavn": "Kari", "etternavn": "Test", "epost": "kari@x.no", "adresse": "Vei 1", "postnr": "0150",
                      "poststed": "Oslo"}]


@pytest.mark.parametrize("rad, melding", [
    ("Kari;Test;kari@x.no;;0150;Oslo;person;", "Adresse mangler"),
    ("Kari;Test;kari@x.no;Vei 1;;Oslo;person;", "Postnr mangler"),
    ("Kari;Test;kari@x.no;Vei 1;0150;;person;", "Poststed mangler"),
    ("Kari;Test;kari@x.no;;;;person;", "Adresse mangler; Postnr mangler; Poststed mangler"),
    (f"Kari;Test;kari@x.no;{'A' * 201};0150;Oslo;person;", "«Adresse» kan være maks 200 tegn."),
    ("Kari;Test;kari@x.no;;0150;Oslo;organisasjon;" + FIRMA, "Adresse mangler"),       # også når arbeidsgiver betaler
])
def test_csv_rad_uten_adresse_stoppes_i_forhaandsvisningen(con, krav_csv, rad, melding):
    kid = _kurs(con)
    resultat = imp.forhaandsvis(con, kid, imp.parse_csv(_csv(rad)), aktor="admin:test")
    assert resultat[0]["handling"] == imp.BLOKKERT and melding in resultat[0]["melding"]
    assert imp.har_blokkerende_rader(resultat)
    assert _antall(con) == 0                                            # forhåndsvisningen etterlater ingen spor


def test_csv_forhaandsvisning_viser_feilen_og_gir_ingen_bekreftelse(con, krav_csv):
    kid = _kurs(con)
    k = _admin()
    html, bekreft = _importer(k, kid, _csv("Kari;Test;kari@x.no;;0150;Oslo;person;", "Ola;Test;ola@x.no;Vei 1;0150;Oslo;person;"))
    assert "Adresse mangler" in html and bekreft is None                # ingen token: hele filen må rettes først
    assert _antall(con) == 0 and _antall(con, "paamelding") == 0


def test_csv_uten_adressekolonner_gir_400_paa_ruten(con, krav_csv):
    kid = _kurs(con)
    r = _last_opp(_admin(), kid, _csv("Kari;Test;kari@x.no", header="Fornavn;Etternavn;E-post"))
    assert r.status_code == 400 and "«Adresse», «Postnr» og «Poststed»" in r.get_data(as_text=True)


def test_csv_importerer_adressen_paa_personen_og_kopierer_for_privat_betaler_men_ikke_for_firma(con):
    kid = _kurs(con)
    html, r = _importer(_admin(), kid, _csv("Kari;Test;kari@x.no;Vei 1;0150;Oslo;person;",
                                            f"Firma;Ansatt;ansatt@firma.no;Sti 2;5003;Bergen;organisasjon;{FIRMA}"))
    assert r is not None and r.status_code == 302, html[:300]
    privat = _paamelding(con, "kari@x.no")
    assert _adresse(_person(con, "kari@x.no")) == {"adresse": "Vei 1", "postnr": "0150", "poststed": "Oslo"}
    assert _faktura(privat) == _adresse(_person(con, "kari@x.no")) and privat["kilde"] == "admin_import"
    firma = _paamelding(con, "ansatt@firma.no")
    assert _adresse(_person(con, "ansatt@firma.no")) == {"adresse": "Sti 2", "postnr": "5003", "poststed": "Bergen"}
    assert _faktura(firma) == FIRMA_ADRESSE                            # firmaets adresse fra registeret, hentet etter importen


def test_csv_overskriver_aldri_en_adresse_som_staar_fra_for_men_fyller_inn_en_tom(con):
    """En CSV-fil kan være eldre enn det som er registrert: en eksisterende adresse beholdes, en manglende fylles inn.
    Fakturaadressen er den adressen personen har etter importen."""
    kid = _kurs(con)
    db.meld_paa(con, _kurs(con, "A2"), epost="kari@x.no", fornavn="Kari", etternavn="Test", deltaker=dict(ANNEN_ADRESSE))
    db.meld_paa(con, _kurs(con, "A3"), epost="ola@x.no", fornavn="Ola", etternavn="Test")          # ingen adresse fra før
    con.commit()
    _, r = _importer(_admin(), kid, _csv("Kari;Test;kari@x.no;Vei 1;0150;Oslo;person;", "Ola;Test;ola@x.no;Vei 2;0151;Oslo;person;"))
    assert r.status_code == 302
    assert _adresse(_person(con, "kari@x.no")) == ANNEN_ADRESSE                  # beholdt
    assert _adresse(_person(con, "ola@x.no")) == {"adresse": "Vei 2", "postnr": "0151", "poststed": "Oslo"}   # fylt inn
    for epost in ("kari@x.no", "ola@x.no"):
        p = con.execute("SELECT p.* FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id JOIN kurs k ON k.id=p.kurs_id "
                        "WHERE d.epost=? AND k.id=?", (epost, kid)).fetchone()
        assert _faktura(p) == _adresse(_person(con, epost))


def test_malfilen_har_adressekolonnene_og_eksempelraden_er_gyldig(con):
    tekst = imp.malfil_csv()
    header = tekst.splitlines()[0].split(";")
    assert header[:6] == ["Fornavn", "Etternavn", "E-post", "Adresse", "Postnr", "Poststed"] and "Fakturaadresse" not in header
    assert len(imp.MALFIL_EKSEMPELRAD) == len(imp.MALFIL_HEADER)
    kid = _kurs(con)
    resultat = imp.forhaandsvis(con, kid, imp.parse_csv(tekst.encode("utf-8-sig")), aktor="admin:test")
    assert [r["handling"] for r in resultat] == [imp.NY]                # malen kan lastes opp som den er (bortsett fra navnet)
    r = _admin().get("/admin/deltaker-import-mal.csv")
    assert "Adresse;Postnr;Poststed" in r.get_data(as_text=True)


def test_importsiden_forklarer_at_adressen_er_paakrevd(con, krav_csv):
    kid = _kurs(con)
    tekst = _admin().get(RUTE.format(kid=kid)).get_data(as_text=True)
    for ord_ in ("Adresse", "Postnr", "Poststed", "private", "også når arbeidsgiver betaler", "stoppes i forhåndsvisningen"):
        assert ord_ in tekst, ord_


# ============================ webhooken /api/paamelding ============================

def _webhook(data: dict, **kw):
    body = json.dumps(data).encode()
    sig = hmac.new(config.WEBHOOK_HEMMELIG.encode(), body, hashlib.sha256).hexdigest()
    return _klient().post("/api/paamelding", data=body, content_type="application/json",
                          headers={"X-IPR-Signatur": sig}, **kw)


def _nett(**over):
    return {"fornavn": "Ola", "etternavn": "Nordmann", "epost": EPOST, "kurs": "A1", **over}


@pytest.mark.parametrize("data, mangler", [
    ({}, ["adresse", "postnr", "poststed"]),
    ({"adresse": "Vei 1", "postnr": "0150"}, ["poststed"]),
    ({"adresse": "  ", "postnr": "0150", "poststed": "Oslo"}, ["adresse"]),
    ({"postnr": "0150", "poststed": "Oslo"}, ["adresse"]),
])
def test_webhook_avviser_uten_adresse_med_400_og_sier_hvilke_felt_som_mangler(con, krav_webhook, data, mangler):
    _kurs(con)
    r = _webhook(_nett(**data))
    assert r.status_code == 400 and r.get_json()["status"] == "feil"
    melding = r.get_json()["melding"]
    assert "deltakerens private adresse er påkrevd" in melding and melding.endswith(", ".join(mangler)), melding
    assert _antall(con) == 0 and _antall(con, "paamelding") == 0
    logg = [json.loads(h["detaljer"]) for h in con.execute("SELECT detaljer FROM hendelse WHERE handling='webhook_avvist'")]
    assert logg and logg[-1]["adresse_mangler"] == mangler and "Vei 1" not in json.dumps(logg)     # bare feltnavn


def test_webhook_avviser_for_lang_adresse(con, krav_webhook):
    _kurs(con)
    r = _webhook(_nett(adresse="A" * 201, postnr="0150", poststed="Oslo"))
    assert r.status_code == 400 and "adresse" in r.get_json()["melding"] and _antall(con) == 0


def test_webhook_lagrer_adressen_paa_personen_og_kopierer_for_privat_betaler(con):
    _kurs(con)
    r = _webhook(_nett(**ADRESSE))
    assert r.status_code == 201, r.get_json()
    assert _adresse(_person(con)) == ADRESSE and _faktura(_paamelding(con)) == ADRESSE


def test_webhook_kjenner_alternative_feltnavn_fra_skjema_plugins(con):
    _kurs(con)
    r = _webhook(_nett(address="Vei 1", zip="0150", city="Oslo"))
    assert r.status_code == 201, r.get_json()
    assert _adresse(_person(con)) == {"adresse": "Vei 1", "postnr": "0150", "poststed": "Oslo"}


def test_webhook_for_firma_kopierer_ikke_adressen_og_ignorerer_gamle_fakturafelt(con):
    _kurs(con)
    r = _webhook(_nett(**ADRESSE, org_nr=FIRMA, faktura_adresse="Manipveien 1", faktura_postnr="9999",
                       faktura_sted="Manipby"))
    assert r.status_code == 201, r.get_json()
    p = _paamelding(con)
    assert _adresse(_person(con)) == ADRESSE and p["betaler"] == "organisasjon" and _faktura(p) == FIRMA_ADRESSE


def test_webhook_de_gamle_fakturafeltene_erstatter_ikke_adressen(con, krav_webhook):
    """Nettsider som ennå sender faktura_adresse/-postnr/-sted (uten adresse/postnr/poststed) får 400: de gamle feltene
    er avviklet, og en privat betaler faktureres på den private adressen."""
    _kurs(con)
    r = _webhook(_nett(faktura_adresse="Vei 1", faktura_postnr="0150", faktura_sted="Oslo"))
    assert r.status_code == 400 and _antall(con) == 0


def test_webhook_er_fortsatt_idempotent_med_adresse(con):
    _kurs(con)
    assert _webhook(_nett(**ADRESSE)).status_code == 201
    assert _webhook(_nett(**ADRESSE)).status_code == 200 and _antall(con, "paamelding") == 1


# ============================ deltakervinduet: personopplysninger ============================

def _deltakervindu(con, **over):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost=EPOST, fornavn="Ola", etternavn="Nordmann", **over)
    con.commit()
    return kid, pid, f"/admin/kurs/{kid}/deltaker/{pid}"


def _lagre_person(k, url, **over):
    data = {"fornavn": "Ola", "etternavn": "Nordmann", "epost": EPOST, "telefon": "", "yrkestittel": "", "arbeidssted": "",
            **over}
    return k.post(f"{url}/person", data=data, follow_redirects=True)


def test_deltakervinduet_viser_adressefeltene_med_verdi_og_som_krav(con):
    _, _, url = _deltakervindu(con, deltaker=dict(ADRESSE))
    html = _admin().get(url).get_data(as_text=True)
    for felt, verdi in ADRESSE.items():
        assert re.search(rf'<input id="f-{felt}" name="{felt}"[^>]* required[^>]* value="{verdi}"', html), felt
    assert "Mangler – legg den inn" not in html


def test_deltakervinduet_sier_fra_naar_en_eldre_person_mangler_adresse(con):
    _, _, url = _deltakervindu(con)
    assert "Mangler – legg den inn" in _admin().get(url).get_data(as_text=True)


@pytest.mark.parametrize("mangler, label", [("adresse", "Adresse"), ("postnr", "Postnummer"), ("poststed", "Poststed")])
def test_adressen_kan_ikke_lagres_tom_i_deltakervinduet(con, mangler, label):
    _, pid, url = _deltakervindu(con, deltaker=dict(ADRESSE))
    r = _lagre_person(_admin(), url, telefon="98765432", **{**ADRESSE, mangler: ""})
    assert f"Fyll inn «{label}»." in r.get_data(as_text=True)
    p = _person(con)
    assert _adresse(p) == ADRESSE and p["telefon"] is None            # ingenting ble lagret, heller ikke telefonnummeret


def test_adressen_kan_endres_i_deltakervinduet_og_loggen_faar_bare_feltnavnet(con):
    kid, pid, url = _deltakervindu(con, deltaker=dict(ADRESSE))
    r = _lagre_person(_admin(), url, **SPORBAR_ADRESSE)
    assert "Personopplysninger oppdatert" in r.get_data(as_text=True)
    assert _adresse(_person(con)) == SPORBAR_ADRESSE
    hendelse = con.execute("SELECT detaljer FROM hendelse WHERE handling='deltaker_endret' ORDER BY id DESC").fetchone()
    assert json.loads(hendelse["detaljer"])["felt"] == ["adresse", "postnr", "poststed"]
    assert "Sporbar" not in hendelse["detaljer"] and "4711" not in hendelse["detaljer"]
    # Den lesbare loggen i deltakervinduet: «Endret: Adresse, Postnummer, Poststed» - aldri verdiene
    p = con.execute("SELECT * FROM paamelding WHERE id=?", (pid,)).fetchone()
    tekst = " ".join(rad.hva + " " + " ".join(rad.detaljer) for rad in hendelseslogg.for_paamelding(con, p, systemadmin=True).rader)
    assert "Endret: Adresse, Postnummer, Poststed" in tekst and "Sporbar" not in tekst and "4711" not in tekst


def test_en_uendret_adresse_gir_ingen_endring_og_ingen_loggfoering(con):
    _, _, url = _deltakervindu(con, deltaker=dict(ADRESSE))
    _lagre_person(_admin(), url, **ADRESSE)
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='deltaker_endret'").fetchone()[0] == 0


def test_fakturaadressen_for_privat_betaler_kan_fortsatt_endres_uten_at_personens_adresse_endres(con):
    kid, pid, url = _deltakervindu(con, deltaker=dict(ADRESSE))
    assert _faktura(_paamelding(con)) == ADRESSE
    html = _admin().get(url).get_data(as_text=True)
    assert 'name="faktura_adresse"' in html and "Fylles inn fra deltakerens adresse" in html
    r = _admin().post(f"{url}/paamelding", data={"betaler": "person", "faktura_adresse": "Regnskapsveien 9",
                                                 "faktura_postnr": "0180", "faktura_sted": "Oslo"}, follow_redirects=True)
    assert "Påmeldingen er oppdatert" in r.get_data(as_text=True)
    assert _faktura(_paamelding(con)) == {"adresse": "Regnskapsveien 9", "postnr": "0180", "poststed": "Oslo"}
    assert _adresse(_person(con)) == ADRESSE


def test_db_oppdater_deltaker_krever_adresse_naar_den_er_med_og_roerer_den_ikke_ellers(con):
    _, pid, _ = _deltakervindu(con, deltaker=dict(ADRESSE))
    did = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pid,)).fetchone()[0]
    with pytest.raises(db.DeltakerFeil, match="Fyll inn «Adresse»."):
        db.oppdater_deltaker(con, did, {"adresse": " ", "postnr": "0150", "poststed": "Oslo"})
    assert db.oppdater_deltaker(con, did, {"telefon": "11223344"}) == ["telefon"]         # adressen er ikke nevnt: urørt
    assert _adresse(_person(con)) == ADRESSE
    assert db.oppdater_deltaker(con, did, {"adresse": "  Ny vei 3  "}) == ["adresse"]        # trimmes
    assert _person(con)["adresse"] == "Ny vei 3"


# ============================ kritikerrunden 29.09.2026: vinduene, veiene og grensene ============================
# (Tester for det kritikerne fant: nettleserforslag i adminskjemaene, advarsel i import-forhåndsvisningen, webhooken med
#  firma, alternative feltnavn, mellomrom, verdier som står igjen og versjonsvakten for personopplysninger.)

def _inputs(html: str, navn: str) -> list[str]:
    return re.findall(rf'<input[^>]*name="{navn}"[^>]*>', html)


def test_adminskjemaene_og_bedriftsskjemaet_bruker_ikke_nettleserens_adresseforslag(con):
    """Admin og kontaktpersonen skriver en ANNEN persons adresse. Nettleseren skal ikke foreslå sin egen lagrede adresse
    (som ellers havner på deltakeren og kopieres til fakturaadressen), og ikke tilby å lagre deltakerens adresse."""
    kid, pid, url = _deltakervindu(con, deltaker=dict(ADRESSE))
    a = _admin()
    sider = {
        "manuell registrering": (a.get(f"/admin/kurs/{kid}/deltaker/ny"), ("adresse", "postnr", "poststed")),
        "deltakervinduet": (a.get(url), ("adresse", "postnr", "poststed")),
        "bedriftspåmelding": (_klient().get("/kurs/A1/gruppe"), ("deltaker_adresse", "deltaker_postnr", "deltaker_poststed")),
    }
    for side, (r, navn) in sider.items():
        assert r.status_code == 200, side
        html = r.get_data(as_text=True)
        for n in navn:
            tagger = _inputs(html, n)
            assert tagger, (side, n)
            for tagg in tagger:
                assert 'autocomplete="off"' in tagg, (side, tagg)


def test_csv_forhaandsvisningen_sier_fra_naar_adressen_i_filen_avviker_fra_den_registrerte(con):
    """Filen overskriver aldri en adresse som står der (den kan være eldre), så den registrerte beholdes. Har noen byttet
    adressen i mellomtiden, skal administrator få se at filen og registeret er uenige."""
    kid = _kurs(con)
    db.meld_paa(con, _kurs(con, "A2"), epost="kari@x.no", fornavn="Kari", etternavn="Test", deltaker=dict(ANNEN_ADRESSE))
    db.meld_paa(con, _kurs(con, "A3"), epost="ola@x.no", fornavn="Ola", etternavn="Test", deltaker=dict(ADRESSE))
    con.commit()
    rader = ("Kari;Test;kari@x.no;Vei 1;0150;Oslo;person;",
             f"Ola;Test;ola@x.no;{ADRESSE['adresse']};{ADRESSE['postnr']};{ADRESSE['poststed']};person;",
             "Per;Test;per@x.no;Vei 3;0152;Oslo;person;")
    resultat = imp.forhaandsvis(con, kid, imp.parse_csv(_csv(*rader)), aktor="admin:test")
    assert [r["adresse_avviker"] for r in resultat] == [True, False, False]     # avvik / lik adresse / ny person
    assert [r["handling"] for r in resultat] == [imp.NY] * 3                       # advarselen endrer ingen beslutning
    html = _last_opp(_admin(), kid, _csv(*rader)).get_data(as_text=True)
    assert "Adressen avviker fra den registrerte" in html and "Den registrerte adressen beholdes" in html
    assert "Vei 1" not in html and ANNEN_ADRESSE["adresse"] not in html            # aldri selve adressen
    ren = _last_opp(_admin(), kid, _csv(*rader[1:])).get_data(as_text=True)
    assert "Adressen avviker" not in ren


@pytest.mark.parametrize("kolonne", ["Postnr.", "Postnr", "Postnummer", "POSTNR"])
def test_csv_kjenner_alle_skrivemaatene_for_postnummer(kolonne):
    rader = imp.parse_csv(_csv("Kari;Test;kari@x.no;Vei 1;0150;Oslo", header=f"Fornavn;Etternavn;E-post;Adresse;{kolonne};Poststed"))
    assert rader[0]["postnr"] == "0150"


def test_webhook_krever_adressen_ogsaa_naar_firma_betaler(con, krav_webhook):
    _kurs(con)
    r = _webhook(_nett(org_nr=FIRMA))                      # firma betaler, men ingen privat adresse
    assert r.status_code == 400 and "deltakerens private adresse er påkrevd" in r.get_json()["melding"]
    assert _antall(con) == 0 and _antall(con, "paamelding") == 0


def test_webhook_kjenner_alle_alternative_feltnavn_for_adressen(con):
    _kurs(con)
    r = _webhook(_nett(gateadresse="Vei 1", postnummer="0150", sted="Oslo"))
    assert r.status_code == 201, r.get_json()
    assert _adresse(_person(con)) == {"adresse": "Vei 1", "postnr": "0150", "poststed": "Oslo"}
    r = _webhook(_nett(epost="b@firma.no", address="Vei 2", postal_code="0151", city="Oslo"))
    assert r.status_code == 201, r.get_json()
    assert _adresse(_person(con, "b@firma.no")) == {"adresse": "Vei 2", "postnr": "0151", "poststed": "Oslo"}


def test_webhook_legger_ikke_adressen_i_logg_eller_utsendinger(con):
    _kurs(con)
    assert _webhook(_nett(**SPORBAR_ADRESSE)).status_code == 201
    assert _person(con)["adresse"] == SPORBAR_ADRESSE["adresse"]
    tabeller = ("hendelse", "utsending_logg", "sendt_epost", "firmapaamelding", "firmapaamelding_rad", "faktura",
                "faktura_forsok", "import_forhaandsvisning", "sensitivt")
    tekst = "\n".join(str([tuple(r) for r in con.execute(f"SELECT * FROM {t}")]) for t in tabeller
                      if db.har_tabell(con, t)).lower()
    for spor in ("sporbarveien", "sporbarby", "4711"):
        assert spor not in tekst, f"«{spor}» står i logg/utsending"


PADDET = {"adresse": "  Vei 1  ", "postnr": " 0150 ", "poststed": "  Oslo "}
RENT = {"adresse": "Vei 1", "postnr": "0150", "poststed": "Oslo"}


def test_skjemaet_lagrer_adressen_uten_mellomrom_foran_og_bak(con):
    _kurs(con)
    r = _klient().post("/kurs/A1", data={"fornavn": "Ola", "etternavn": "Nordmann", "epost": EPOST, "samtykke": "on",
                                         "betaler": "person", **PADDET})
    assert r.status_code == 200
    assert _adresse(_person(con)) == RENT and _faktura(_paamelding(con)) == RENT


def test_bedriftspaamelding_lagrer_adressen_uten_mellomrom_foran_og_bak(con):
    _kurs(con)
    r = _gruppe(deltaker_adresse=[PADDET["adresse"]], deltaker_postnr=[PADDET["postnr"]],
                deltaker_poststed=[PADDET["poststed"]])
    assert r.status_code == 302
    assert _adresse(_person(con)) == RENT


def test_manuell_registrering_lagrer_adressen_uten_mellomrom_foran_og_bak(con):
    kid = _kurs(con)
    assert _ny(kid, **PADDET).status_code == 302
    assert _adresse(_person(con)) == RENT and _faktura(_paamelding(con)) == RENT


def test_manuell_registrering_beholder_adressen_som_er_skrevet_naar_noe_annet_mangler(con):
    kid = _kurs(con)
    r = _ny(kid, adresse="Sporveien 7", postnr="", poststed="Oslo")
    html = r.get_data(as_text=True)
    assert r.status_code == 400 and "Fyll inn «Postnummer»." in html
    assert 'value="Sporveien 7"' in html and 'value="Oslo"' in html


def test_deltakervinduet_sier_fra_ogsaa_naar_bare_postnummer_mangler(con):
    _, _, url = _deltakervindu(con, deltaker={"adresse": "Eksempelveien 1", "poststed": "Oslo"})
    assert "Mangler – legg den inn" in _admin().get(url).get_data(as_text=True)


def test_en_adresseendring_fra_en_annen_administrator_avvises_som_samtidig_endring(con):
    """Versjonsvakten for Personopplysninger dekker adressen: A åpner vinduet, B retter bare adressen, A lagrer det
    gamle skjemaet og skal få «noen andre har endret dette» i stedet for å overskrive Bs rettelse."""
    _, _, url = _deltakervindu(con, deltaker=dict(ADRESSE))
    a, b = _admin(), _admin()
    versjon = re.search(r'name="versjon" value="([^"]+)"', a.get(url).get_data(as_text=True)).group(1)
    vb = re.search(r'name="versjon" value="([^"]+)"', b.get(url).get_data(as_text=True)).group(1)
    _lagre_person(b, url, **ANNEN_ADRESSE, versjon=vb)
    assert _adresse(_person(con)) == ANNEN_ADRESSE
    r = _lagre_person(a, url, telefon="98765432", **ADRESSE, versjon=versjon)
    assert "noen andre har endret dette" in r.get_data(as_text=True)
    assert _adresse(_person(con)) == ANNEN_ADRESSE and _person(con)["telefon"] is None
