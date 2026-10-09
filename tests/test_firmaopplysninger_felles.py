"""Den felles regelen for firmaopplysninger (brukerens beslutning 29.09.2026): når firma betaler, er reglene like uansett
hvor påmeldingen kommer fra – offentlig skjema, adminregistrering, bedriftspåmelding, webhook fra nettsiden og filimport.

  * Firmanavn og fakturaadresse kommer bare fra Enhetsregisteret. Det som skrives inn (også når registeret er nede), lagres aldri.
  * Svarer ikke registeret: påmeldingen beholdes med organisasjonsnummeret alene, merket «Firmaopplysninger må kontrolleres»,
    og fakturaen venter. Når opplysningene er hentet, går fakturaen videre.
  * Skjemaer en person fyller ut (offentlig, admin, bedrift) avviser et ugyldig eller ukjent nummer; webhook beholder
    påmeldingen, merket for oppfølging; filimport stopper raden med ugyldig nummer i forhåndsvisningen.
Testene kjører de samme scenarioene mot alle veiene. Alle data er fiktive (brreg.DEMO_ENHETER)."""
import hashlib
import hmac
import json
from datetime import date, timedelta

import pytest

from adressehjelp import ADRESSE
from kurs import config, db, firmaopplysninger, import_deltakere as imp, sveiper
from kurs.integrasjoner import brreg
from kurs.kjoring import Kjoring
from navnehjelp import gruppeskjema, navnedeler

FIRMA, NEDE, UKJENT, UGYLDIG = "999900003", brreg.DEMO_NEDE_ORGNR, "999900070", "12345"
REGISTER = ("EKSEMPEL KOMMUNE", "Postboks 100", "1234", "EKSEMPELBY")       # navn, adresse, postnr, sted for FIRMA
EPOST = "ny.person@eksempel.no"
# Det som skrives inn om firmanavn og adresse på de ulike veiene - skal aldri lagres
SKREVET = dict(firmanavn="MANIP AS", org_navn="MANIP AS", org_adresse="Manipveien 1", org_postnr="9999",
               org_sted="Manipby", faktura_adresse="Manipveien 1", faktura_postnr="9999", faktura_sted="Manipby")


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


def _kurs(con, kode="S1"):
    start = date.today() + timedelta(days=30)
    kid = db.opprett_kurs(con, kode=kode, navn="Eksempelkurs", type="fysisk", sted="Eksempelsted", pris_nok=4500,
                          fakturering="person", betaling="samlet", kapasitet=10, sharepoint_mappe=f"K/{kode}",
                          datoer=[start.isoformat(), (start + timedelta(days=1)).isoformat()])
    con.commit()
    return kid


def _paamelding(con):
    return con.execute("SELECT p.* FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE d.epost=?",
                       (EPOST,)).fetchone()


def _firmafelter(p):
    return (p["org_navn"], p["faktura_adresse"], p["faktura_postnr"], p["faktura_sted"])


# ============================ de fem veiene ============================
# Hver funksjon melder på EPOST med firma som betaler og med «skrevet» firmanavn/adresse der veien tillater det.

def _offentlig(con, kid, nr):
    _klient().post("/kurs/S1", data={"fornavn": "Ny", "etternavn": "Person", "epost": EPOST, "samtykke": "on", "samtykke_lagring": "on", **ADRESSE,
                                     "betaler": "organisasjon", "org_nr": nr, **SKREVET})


def _adminregistrering(con, kid, nr):
    _admin().post(f"/admin/kurs/{kid}/deltaker/ny", data={
        "fornavn": "Ny", "etternavn": "Person", "epost": EPOST, "betaler": "organisasjon", "org_nr": nr, **ADRESSE,
        **SKREVET})


def _bedriftspaamelding(con, kid, nr):
    _klient().post("/kurs/S1/gruppe", data=gruppeskjema({
        "kontakt_navn": "Kari Kontakt", "kontakt_epost": "kontakt@eksempel.no", "org_nr": nr, "faktura_ref": "REF-1",
        "deltaker_navn": ["Ny Person"], "deltaker_epost": [EPOST], "samtykke": "on", "samtykke_lagring": "on", **SKREVET}))


def _webhook(con, kid, nr):
    body = json.dumps({"fornavn": "Ny", "etternavn": "Person", "epost": EPOST, "kurs": "S1", **ADRESSE, "org_nr": nr,
                       "arbeidssted": "MANIP AS", "faktura_adresse": "Manipveien 1", "faktura_postnr": "9999",
                       "faktura_sted": "Manipby", "faktura_ref": "REF-1"}).encode()
    sig = hmac.new(config.WEBHOOK_HEMMELIG.encode(), body, hashlib.sha256).hexdigest()
    _klient().post("/api/paamelding", data=body, content_type="application/json", headers={"X-IPR-Signatur": sig})


def _filimport(con, kid, nr):
    rader = [{**navnedeler("Ny Person"), "epost": EPOST, "betaler": "organisasjon", "org_nr": nr,
              "org_navn": "MANIP AS", "faktura_adresse": "Manipveien 1", "faktura_ref": "REF-1", **ADRESSE}]
    resultater = imp.forhaandsvis(con, kid, rader, aktor="admin:test")
    con.commit()
    if imp.har_blokkerende_rader(resultater):
        return                                                  # stoppet i forhåndsvisningen: admin må rette filen
    admin_id = con.execute("SELECT id FROM admin_bruker WHERE brukernavn=?", (config.ADMIN_BRUKERNAVN,)).fetchone()[0]
    token = imp.lagre_forhaandsvisning(con, kid, admin_id, rader, resultater)
    con.commit()
    _admin().post(f"/admin/kurs/{kid}/deltakere/importer/bekreft", data={"forhaandsvisning_token": token})


VEIER = {"offentlig": _offentlig, "adminregistrering": _adminregistrering, "bedriftspåmelding": _bedriftspaamelding,
         "webhook": _webhook, "filimport": _filimport}


# ============================ scenarioene ============================

@pytest.mark.parametrize("vei", VEIER)
def test_registeret_svarer_firmaopplysningene_kommer_bare_fra_registeret(con, vei):
    kid = _kurs(con)
    VEIER[vei](con, kid, FIRMA)
    p = _paamelding(con)
    assert p is not None and (p["betaler"], p["org_nr"]) == ("organisasjon", FIRMA)
    assert _firmafelter(p) == REGISTER                        # aldri det som ble skrevet inn («MANIP»)
    assert not firmaopplysninger.mangler(p)


@pytest.mark.parametrize("vei", VEIER)
def test_registeret_svarer_ikke_paameldingen_beholdes_merkes_og_fakturaen_venter(con, vei):
    kid = _kurs(con)
    VEIER[vei](con, kid, NEDE)
    p = _paamelding(con)
    assert p is not None and (p["betaler"], p["org_nr"]) == ("organisasjon", NEDE)
    assert _firmafelter(p) == (None, None, None, None)        # ingenting skrevet av noen lagres
    assert firmaopplysninger.mangler(p)
    assert [r["id"] for r in firmaopplysninger.liste(con)] == [p["id"]]         # merket for oppfølging
    logg = [json.loads(h["detaljer"]) for h in con.execute(
        "SELECT detaljer FROM hendelse WHERE handling='enhetsoppslag_utilgjengelig'")]
    assert logg == ([] if vei == "filimport" else [{"kurs_id": kid, "paamelding_id": p["id"]}])   # uten org.nr., navn og adresse
    k = Kjoring(con, idag=date.today())
    sveiper.kjor(k, p["id"], ignorer_utsatt=True)             # samme behandling for alle veiene (admin og fil er holdt tilbake)
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 0 and \
        con.execute("SELECT COUNT(*) FROM faktura_forsok").fetchone()[0] == 0
    assert firmaopplysninger.prov_igjen(con, p["id"], "admin:test") == firmaopplysninger.HENTET
    con.commit()
    assert _firmafelter(_paamelding(con)) == ("OPPE IGJEN DEMO AS", "Retteveien 2", "1234", "EKSEMPELBY")
    sveiper.kjor(Kjoring(con, idag=date.today()), p["id"], ignorer_utsatt=True)
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 1       # etter vanlig fakturaplan
    assert firmaopplysninger.liste(con) == []


# Forventet utfall når nummeret er ugyldig eller ukjent: True = påmeldingen beholdes (merket for oppfølging),
# False = den avvises/stoppes slik at den som fyller ut, kan rette nummeret med en gang
UGYLDIG_BEHOLDES = {"offentlig": False, "adminregistrering": False, "bedriftspåmelding": False, "webhook": True,
                    "filimport": False}
UKJENT_BEHOLDES = {"offentlig": False, "adminregistrering": False, "bedriftspåmelding": False, "webhook": True,
                   "filimport": True}          # filimport slår ikke opp i forhåndsvisningen; det gjøres rett etter importen


@pytest.mark.parametrize("vei", VEIER)
@pytest.mark.parametrize("nr,beholdes", [(UGYLDIG, UGYLDIG_BEHOLDES), (UKJENT, UKJENT_BEHOLDES)], ids=["ugyldig", "ukjent"])
def test_ugyldig_eller_ukjent_nummer(con, vei, nr, beholdes):
    kid = _kurs(con)
    VEIER[vei](con, kid, nr)
    p = _paamelding(con)
    if not beholdes[vei]:
        assert p is None                                       # ingenting registrert - den som fyller ut ser en feilmelding
        return
    assert p is not None and _firmafelter(p) == (None, None, None, None)      # aldri det som ble skrevet inn
    assert firmaopplysninger.mangler(p) and [r["id"] for r in firmaopplysninger.liste(con)] == [p["id"]]
    sveiper.kjor(Kjoring(con, idag=date.today()), p["id"], ignorer_utsatt=True)
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 0     # aldri faktura uten gyldig og hentet firma
    assert firmaopplysninger.prov_igjen(con, p["id"], "admin:test") in (
        firmaopplysninger.UGYLDIG_NUMMER, firmaopplysninger.IKKE_FUNNET)


@pytest.mark.parametrize("vei", ["offentlig", "adminregistrering", "bedriftspåmelding"])
def test_skjemaene_sier_fra_om_ugyldig_og_ukjent_nummer(con, vei):
    kid = _kurs(con)
    tekster = {}
    for navn, nr in (("ugyldig", UGYLDIG), ("ukjent", UKJENT)):
        if vei == "offentlig":
            r = _klient().post("/kurs/S1", data={"fornavn": "Ny", "etternavn": "Person", "epost": EPOST, "samtykke": "on", "samtykke_lagring": "on", **ADRESSE,
                                                 "betaler": "organisasjon", "org_nr": nr})
        elif vei == "adminregistrering":
            r = _admin().post(f"/admin/kurs/{kid}/deltaker/ny", data={
                "fornavn": "Ny", "etternavn": "Person", "epost": EPOST, "betaler": "organisasjon", "org_nr": nr,
                **ADRESSE})
        else:
            r = _klient().post("/kurs/S1/gruppe", data=gruppeskjema({
                "kontakt_navn": "Kari Kontakt", "kontakt_epost": "kontakt@eksempel.no", "org_nr": nr,
                "deltaker_navn": ["Ny Person"], "deltaker_epost": [EPOST], "samtykke": "on", "samtykke_lagring": "on"}))
        assert r.status_code == 400
        tekster[navn] = r.get_data(as_text=True)
    assert firmaopplysninger.TEKST_UGYLDIG in tekster["ugyldig"] and firmaopplysninger.TEKST_IKKE_FUNNET not in tekster["ugyldig"]
    assert "Fant ikke organisasjonsnummeret i Brønnøysundregistrene" in tekster["ukjent"]


def test_alle_veiene_gir_samme_merke_og_samme_tekst_i_oversikten(con):
    kid = _kurs(con)
    for i, vei in enumerate(VEIER):
        global EPOST
        gammel, EPOST = EPOST, f"ny{i}@eksempel.no"
        try:
            VEIER[vei](con, kid, NEDE)
        finally:
            EPOST = gammel
    assert len(firmaopplysninger.liste(con)) == len(VEIER)            # alle fem er merket for oppfølging
    html = _admin().get("/admin").get_data(as_text=True)
    assert html.count(firmaopplysninger.MERKE) >= 2 and html.count("Firmaopplysninger må kontrolleres") >= 2
    for r in firmaopplysninger.liste(con):
        assert f"/admin/kurs/{kid}/deltaker/{r['id']}" in html
    assert _admin().post("/admin/firmaopplysninger").status_code == 302
    assert firmaopplysninger.liste(con) == []                          # ett trykk henter for alle, uansett vei


# ============================ bedriftspåmeldingens kontaktpost og kvittering ============================

def _mailtekst():
    """Synlig tekst i alle e-postene som er sendt (uten HTML-merkelapper)."""
    import re
    return " ".join(" ".join(re.sub(r"<[^>]+>", " ", f.read_text(encoding="utf-8")).split())
                    for f in sorted(config.UTBOKS.glob("*.html"))) if config.UTBOKS.exists() else ""


def test_bedriftspaamelding_lagrer_registerets_firmanavn_paa_kontaktposten(con):
    kid = _kurs(con)
    _bedriftspaamelding(con, kid, FIRMA)
    rad = con.execute("SELECT firmanavn, org_nr, faktura_adresse, faktura_postnr, faktura_sted FROM firmapaamelding").fetchone()
    assert tuple(rad) == ("EKSEMPEL KOMMUNE", FIRMA, "Postboks 100", "1234", "EKSEMPELBY")     # ikke «MANIP AS»
    assert "fra EKSEMPEL KOMMUNE til Eksempelkurs" in _mailtekst() and "MANIP" not in _mailtekst()


def test_bedriftspaamelding_uten_svar_fra_registeret_sier_firmaet_og_kvitteringen_forklarer(con):
    kid = _kurs(con)
    _bedriftspaamelding(con, kid, NEDE)
    rad = con.execute("SELECT firmanavn, org_nr, faktura_adresse, faktura_postnr, faktura_sted, kvittering_token "
                      "FROM firmapaamelding").fetchone()
    assert tuple(rad)[:5] == ("", NEDE, None, None, None)                # ingenting skrevet av kontaktpersonen lagres
    mail = _mailtekst()
    assert "fra firmaet til Eksempelkurs" in mail and "faktureres for seg, 4 500 kr, til firmaet" in mail
    assert "MANIP" not in mail and "None" not in mail
    side = f"/kurs/S1/gruppe/kvittering/{rad['kvittering_token']}"
    html = _klient().get(side).get_data(as_text=True)
    assert "Vi kunne ikke hente firmaopplysningene akkurat nå" in html and f"Organisasjonsnummer {NEDE}" in html
    assert firmaopplysninger.prov_alle(con, "admin:test")[firmaopplysninger.HENTET] == 1
    con.commit()
    assert "Vi kunne ikke hente firmaopplysningene" not in _klient().get(side).get_data(as_text=True)   # er hentet nå


# ============================ ingen felt å skrive firmanavn og adresse i ============================

def test_skjemaene_har_ingen_felt_for_firmanavn_og_adresse(con):
    kid = _kurs(con)
    gruppe = _klient().get("/kurs/S1/gruppe").get_data(as_text=True)
    for navn in ("firmanavn", "org_navn", "faktura_adresse", "faktura_postnr", "faktura_sted"):
        assert f'name="{navn}"' not in gruppe, navn                       # bedriftspåmeldingen: bare organisasjonsnummer
    assert 'name="org_nr"' in gruppe
    ny = _admin().get(f"/admin/kurs/{kid}/deltaker/ny").get_data(as_text=True)
    assert 'name="org_navn"' not in ny and 'name="org_nr"' in ny            # adminregistreringen: bare organisasjonsnummer


def test_deltakervinduet_viser_firmaopplysningene_uten_felt_aa_skrive_i(con):
    kid = _kurs(con)
    _offentlig(con, kid, FIRMA)
    p = _paamelding(con)
    html = _admin().get(f"/admin/kurs/{kid}/deltaker/{p['id']}").get_data(as_text=True)
    for navn in ("org_navn", "faktura_adresse", "faktura_postnr", "faktura_sted"):
        assert f'name="{navn}"' not in html, navn
    assert 'name="org_nr"' in html and "EKSEMPEL KOMMUNE" in html and "Postboks 100" in html
    privat, _ = db.meld_paa(con, kid, epost="p@eksempel.no", fornavn="P", etternavn="P")     # betaler selv: egen adresse
    con.commit()
    assert 'name="faktura_adresse"' in _admin().get(f"/admin/kurs/{kid}/deltaker/{privat}").get_data(as_text=True)


# ============================ når administrator har hentet opplysningene ============================

def _som(epost, vei, con, kid, nr):
    """Kjører en vei med en annen e-postadresse (de fem veiene bruker EPOST)."""
    global EPOST
    gammel, EPOST = EPOST, epost
    try:
        VEIER[vei](con, kid, nr)
    finally:
        EPOST = gammel


def _antall_fakturaer_for(con, epost):
    return con.execute("SELECT COUNT(*) FROM faktura f JOIN paamelding p ON p.id=f.paamelding_id JOIN deltaker d ON d.id=p.deltaker_id "
                       "WHERE d.epost=?", (epost,)).fetchone()[0]


def test_nytt_oppslag_i_admin_lar_fakturaen_gaa_videre_med_en_gang(con):
    kid = _kurs(con)
    _offentlig(con, kid, NEDE)                           # registeret svarer ikke: merket, og ingen faktura
    p = _paamelding(con)
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 0
    html = _admin().post(f"/admin/kurs/{kid}/deltaker/{p['id']}/firmaopplysninger",
                         follow_redirects=True).get_data(as_text=True)
    assert "Firmaopplysningene er hentet" in html
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 1       # etter vanlig fakturaplan, uten å vente til i morgen


def test_samlet_oppslag_i_admin_lar_fakturaene_gaa_videre_men_roerer_ikke_holdte_paameldinger(con):
    kid = _kurs(con)
    _som("a@eksempel.no", "offentlig", con, kid, NEDE)
    _som("b@eksempel.no", "bedriftspåmelding", con, kid, NEDE)
    _som("c@eksempel.no", "adminregistrering", con, kid, NEDE)       # holdt tilbake (sveiper_utsatt=1) til admin behandler den
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 0 and len(firmaopplysninger.liste(con)) == 3
    assert _admin().post("/admin/firmaopplysninger").status_code == 302
    assert firmaopplysninger.liste(con) == []
    assert (_antall_fakturaer_for(con, "a@eksempel.no"), _antall_fakturaer_for(con, "b@eksempel.no"),
            _antall_fakturaer_for(con, "c@eksempel.no")) == (1, 1, 0)


# ============================ oppslaget gjøres bare når det trengs ============================

def test_slaa_opp_gir_ugyldig_nummer_uten_nettverkskall(monkeypatch):
    monkeypatch.setattr(brreg, "hent", lambda nr: pytest.fail("oppslag for ugyldig nummer"))
    for tekst in ("12345", "abc", "", None, "999900004", "0" * 9):
        o = firmaopplysninger.slaa_opp(tekst)
        assert o.utfall == firmaopplysninger.UGYLDIG_NUMMER and o.enhet is None, tekst
    assert firmaopplysninger.slaa_opp("12345").orgnr == "12345"          # teksten som ble skrevet
    assert firmaopplysninger.slaa_opp("x" * 50).orgnr == "x" * 30        # avkortet


def test_slaa_opp_utfall_for_gyldige_nummer():
    o = firmaopplysninger.slaa_opp(" 999 900 003 ")
    assert (o.utfall, o.orgnr, o.enhet.navn) == (firmaopplysninger.FUNNET, FIRMA, "EKSEMPEL KOMMUNE")
    assert firmaopplysninger.slaa_opp("999900070").utfall == firmaopplysninger.IKKE_FUNNET
    assert firmaopplysninger.slaa_opp(NEDE).utfall == firmaopplysninger.UTILGJENGELIG


def test_ingen_oppslag_naar_skjemaet_har_andre_feil(con, monkeypatch):
    kid = _kurs(con)
    monkeypatch.setattr(brreg, "hent", lambda nr: pytest.fail("oppslag ved andre feil"))
    assert _klient().post("/kurs/S1", data={"fornavn": "Ny", "etternavn": "Person", "epost": EPOST, "betaler": "organisasjon",
                                            "org_nr": FIRMA, **ADRESSE}).status_code == 400          # mangler samtykke
    assert _admin().post(f"/admin/kurs/{kid}/deltaker/ny", data={
        "etternavn": "Person", "epost": EPOST, "betaler": "organisasjon", "org_nr": FIRMA,
        **ADRESSE}).status_code == 400   # mangler fornavn
    assert _klient().post("/kurs/S1/gruppe", data=gruppeskjema({
        "kontakt_navn": "Kari Kontakt", "kontakt_epost": "kontakt@eksempel.no", "org_nr": FIRMA,
        "deltaker_navn": ["Ny Person"], "deltaker_epost": [EPOST]})).status_code == 400                  # mangler samtykke
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 0


def test_ugyldig_nummer_vises_sammen_med_andre_feil(con):
    kid = _kurs(con)
    html = _klient().post("/kurs/S1", data={"fornavn": "Ny", "etternavn": "Person", "epost": EPOST, "betaler": "organisasjon",
                                            "org_nr": UGYLDIG, **ADRESSE}).get_data(as_text=True)
    assert "Du må godta vilkår" in html and firmaopplysninger.TEKST_UGYLDIG in html
    html = _admin().post(f"/admin/kurs/{kid}/deltaker/ny", data={
        "etternavn": "Person", "epost": EPOST, "betaler": "organisasjon", "org_nr": UGYLDIG,
        **ADRESSE}).get_data(as_text=True)
    assert "Fyll inn fornavn" in html and firmaopplysninger.TEKST_UGYLDIG in html
    html = _klient().post("/kurs/S1/gruppe", data=gruppeskjema({
        "kontakt_navn": "Kari Kontakt", "kontakt_epost": "kontakt@eksempel.no", "org_nr": UGYLDIG,
        "deltaker_navn": ["Ny Person"], "deltaker_epost": [EPOST]})).get_data(as_text=True)
    assert "Dere må godta vilkår" in html and firmaopplysninger.TEKST_UGYLDIG in html
