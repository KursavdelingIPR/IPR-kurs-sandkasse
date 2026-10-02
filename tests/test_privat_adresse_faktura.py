"""Privat adresse og faktura (brukerens beslutning 01.10.2026).

  * Privat adresse, postnummer og poststed er ALLTID påkrevd for nye, reelle deltakere, også når arbeidsgiver/firma betaler.
    Firmaets adresse hentes fortsatt separat fra Enhetsregisteret.
  * En PRIVAT faktura opprettes og sendes ALDRI uten komplett privat adresse. Mangler den, holdes faktureringen tilbake (ingenting
    skrives, så morgenjobben er idempotent), administrator får tydelig beskjed, og fakturaen lages av seg selv når adressen er inn.
  * Eldre/test-deltakere uten adresse kan fortsatt redigeres uten at adressen må fylles inn først.
  * Webhook-NØDBREMSEN (ADRESSE_KREVES_I_WEBHOOK=0, av som standard): påmeldingen går ikke tapt. Den tas inn selv om adressen mangler,
    merkes «Privat adresse mangler», og fakturaen holdes tilbake til adressen er komplett.
Alle testdata er fiktive."""
import re
from datetime import date, timedelta

import pytest

from adressehjelp import ADRESSE, ANNEN_ADRESSE
from kurs import behandling, config, daglig, db, privatadresse, skjemafelt, sveiper
from kurs.integrasjoner import visma
from kurs.kjoring import Kjoring
from test_adresse_valgfri import _les_innstillinger
from test_adresse_veier import EPOST, FIRMA, _admin, _antall, _gruppe, _klient, _kurs, _ny, _nett, _webhook, con  # noqa: F401

pytestmark = pytest.mark.uten_standardadresse   # registrerer bevisst uten adresse (se conftest.py)

MERKE = "Privat adresse mangler"
MERKE_U = "Privat adresse er ufullstendig"
HOLDT_U = "Fakturering holdt tilbake: privat adresse er ufullstendig"
HOLDT = "Fakturering holdt tilbake: privat adresse mangler"
VENTER = "Venter på privat adresse"


@pytest.fixture(autouse=True)
def _demo(monkeypatch):
    monkeypatch.setattr(config, "DEMO", True)


@pytest.fixture
def grunnlag(monkeypatch):
    """Fakturagrunnlagene som sendes til Visma (i demo lages ingen ekte faktura)."""
    ut = []

    def fake(g):
        ut.append(g)
        return visma.Fakturaresultat(visma_id=f"v{len(ut)}", faktura_nr=f"F{len(ut)}", kunde_id="k")
    monkeypatch.setattr(visma, "fakturer", fake)
    return ut


def _reg(con, kid, adresse=None, epost=EPOST, **paamelding):
    """Registrerer Ola Nordmann direkte i databasen, med eller uten privat adresse (som eldre/test-data og nødbremsen gir)."""
    pid, _ = db.meld_paa(con, kid, epost=epost, fornavn="Ola", etternavn="Nordmann", deltaker=dict(adresse or {}),
                         paamelding=paamelding or None)
    con.commit()
    return pid


def _kjor(con, pid=None, idag=None):
    sveiper.kjor(Kjoring(con, idag=idag or date.today()), pid)
    con.commit()


def _fakturaer(con, pid=None) -> int:
    return con.execute("SELECT COUNT(*) FROM faktura" + (" WHERE paamelding_id=?" if pid else ""), (pid,) if pid else ()).fetchone()[0]


def _hendelser(con) -> int:
    return con.execute("SELECT COUNT(*) FROM hendelse").fetchone()[0]


def _p(con, pid):
    return con.execute("SELECT * FROM paamelding WHERE id=?", (pid,)).fetchone()


def _person(con, epost=EPOST):
    return con.execute("SELECT * FROM deltaker WHERE epost=?", (epost,)).fetchone()


def _adr(rad) -> dict:
    return {"adresse": rad["adresse"], "postnr": rad["postnr"], "poststed": rad["poststed"]}


def _side(k, adresse: str) -> str:
    return k.get(adresse, follow_redirects=True).get_data(as_text=True)


def _lagre_person(k, kid, pid, **adresse):
    data = {"fornavn": "Ola", "etternavn": "Nordmann", "epost": EPOST, "telefon": "", "yrkestittel": "", "arbeidssted": "", **adresse}
    return k.post(f"/admin/kurs/{kid}/deltaker/{pid}/person", data=data, follow_redirects=True).get_data(as_text=True)


# ============================ fakturamotoren: ingen privat faktura uten komplett adresse ============================

def test_privat_faktura_lages_ikke_uten_adresse_men_bekreftelsen_gaar_ut(con, grunnlag):
    kid = _kurs(con)
    pid = _reg(con, kid)
    _kjor(con, pid)
    assert grunnlag == [] and _fakturaer(con) == 0                                   # ingen faktura og ingen kall mot Visma
    assert con.execute("SELECT COUNT(*) FROM faktura_forsok").fetchone()[0] == 0     # heller ikke et forsøk
    assert db.allerede_sendt(con, f"kurs:{kid}", EPOST, "bekreftelse")               # bekreftelsen gikk ut som vanlig
    assert _p(con, pid)["sveiper_kjort"] == 0                                        # raden tas igjen når adressen er inn


def test_holdet_skriver_ingenting_og_er_idempotent(con, grunnlag):
    kid = _kurs(con)
    pid = _reg(con, kid)
    _kjor(con, pid)
    for _ in range(3):
        fore = (_hendelser(con), con.execute("SELECT COUNT(*) FROM utsending_logg").fetchone()[0])
        _kjor(con, pid)
        _kjor(con)
        assert (_hendelser(con), con.execute("SELECT COUNT(*) FROM utsending_logg").fetchone()[0]) == fore
    assert grunnlag == [] and _fakturaer(con) == 0


def test_fakturaen_lages_naar_adressen_er_lagt_inn(con, grunnlag):
    kid = _kurs(con)
    pid = _reg(con, kid)
    _kjor(con, pid)
    db.oppdater_deltaker(con, _p(con, pid)["deltaker_id"], dict(ADRESSE), aktor="admin:test")
    con.commit()
    _kjor(con, pid)
    assert len(grunnlag) == 1 and grunnlag[0].er_privatperson
    assert (grunnlag[0].adresse, grunnlag[0].postnr, grunnlag[0].sted) == tuple(ADRESSE.values())
    assert _fakturaer(con, pid) == 1 and _p(con, pid)["sveiper_kjort"] == 1


@pytest.mark.parametrize("delvis", [{"adresse": "Vei 1"}, {"adresse": "Vei 1", "postnr": "0150"},
                                    {"postnr": "0150", "poststed": "Oslo"}, {"adresse": "  ", "postnr": "0150", "poststed": "Oslo"}])
def test_en_delvis_adresse_er_ikke_nok(con, grunnlag, delvis):
    kid = _kurs(con)
    pid = _reg(con, kid)
    con.execute("UPDATE deltaker SET adresse=?, postnr=?, poststed=?", (delvis.get("adresse"), delvis.get("postnr"), delvis.get("poststed")))
    con.commit()
    _kjor(con, pid)
    assert grunnlag == [] and _fakturaer(con) == 0


def test_komplett_adresse_paa_personen_brukes_naar_kopien_er_delvis(con, grunnlag):
    kid = _kurs(con)
    pid = _reg(con, kid)
    con.execute("UPDATE paamelding SET faktura_adresse='Bare gata 5', faktura_postnr=NULL, faktura_sted=NULL")
    con.execute("UPDATE deltaker SET adresse=?, postnr=?, poststed=?", tuple(ANNEN_ADRESSE.values()))
    con.commit()
    _kjor(con, pid)
    assert len(grunnlag) == 1 and (grunnlag[0].adresse, grunnlag[0].postnr, grunnlag[0].sted) == tuple(ANNEN_ADRESSE.values())


def test_firma_som_betaler_faktureres_paa_firmaets_adresse_uten_privat_adresse(con, grunnlag):
    """Manglende PRIVAT adresse holder aldri tilbake en firmafaktura: den går til firmaets adresse fra Enhetsregisteret."""
    _kurs(con)
    r = _webhook(_nett(org_nr=FIRMA, **ADRESSE))
    assert r.status_code == 201
    pid = r.get_json()["paamelding_id"]
    con.execute("UPDATE deltaker SET adresse=NULL, postnr=NULL, poststed=NULL")        # eldre person uten privat adresse
    con.commit()
    assert len(grunnlag) == 1 and not grunnlag[0].er_privatperson and _fakturaer(con, pid) == 1
    assert privatadresse.venter(con, pid) is False


def test_delfaktura_per_samling_holdes_tilbake_uten_privat_adresse(con, grunnlag):
    kid = _kurs(con, frem=5, betaling="per_samling")                  # første samling er forfalt (innenfor faktura_dager_for)
    pid = _reg(con, kid)
    _kjor(con, pid)
    sveiper.forfalte_delfakturaer(Kjoring(con, idag=date.today()))
    con.commit()
    assert grunnlag == [] and _fakturaer(con) == 0
    db.oppdater_deltaker(con, _p(con, pid)["deltaker_id"], dict(ADRESSE), aktor="admin:test")
    con.commit()
    _kjor(con, pid)
    sveiper.forfalte_delfakturaer(Kjoring(con, idag=date.today()))
    con.commit()
    assert len(grunnlag) >= 1 and _fakturaer(con, pid) >= 1


def test_utsatt_samlet_faktura_holdes_tilbake_til_adressen_er_inn(con, grunnlag):
    """Kurs om mer enn seks måneder: fakturaen venter til seks måneder før. Når datoen er nådd, men adressen mangler, lages den
    ikke; den lages så snart adressen er lagt inn."""
    kid = _kurs(con, frem=300)
    pid = _reg(con, kid)
    _kjor(con, pid)
    tidligst = date.fromisoformat(_p(con, pid)["faktura_tidligst_dato"])
    sveiper.utsatte_fakturaer(Kjoring(con, idag=tidligst))
    con.commit()
    assert grunnlag == [] and _fakturaer(con) == 0                    # datoen er nådd, men ingen adresse
    db.oppdater_deltaker(con, _p(con, pid)["deltaker_id"], dict(ADRESSE), aktor="admin:test")
    con.commit()
    sveiper.utsatte_fakturaer(Kjoring(con, idag=tidligst))
    con.commit()
    assert len(grunnlag) == 1 and _fakturaer(con, pid) == 1


def test_morgenjobben_rapporterer_og_er_idempotent(con, grunnlag):
    kid = _kurs(con)
    pid = _reg(con, kid)
    daglig.kjor(Kjoring(con, idag=date.today()))
    con.commit()
    fore = _hendelser(con)
    k = Kjoring(con, idag=date.today())
    daglig.kjor(k)
    con.commit()
    assert _hendelser(con) == fore and grunnlag == [] and _fakturaer(con) == 0
    assert any("holdes tilbake" in str(linje) and MERKE.lower() in str(linje) for linje in k.utskrift), k.utskrift[-12:]
    assert privatadresse.venter(con, pid)


# ============================ regelen: motoren og visningen sier det samme ============================

@pytest.mark.parametrize("kopi", ["tom", "delvis", "komplett"])
@pytest.mark.parametrize("person", ["tom", "delvis", "komplett"])
@pytest.mark.parametrize("betaler", ["person", "organisasjon"])
def test_motoren_og_visningen_er_enige(con, grunnlag, kopi, person, betaler):
    kid = _kurs(con)
    pid = _reg(con, kid)
    felt = {"tom": (None, None, None), "delvis": ("Vei 1", "0150", None), "komplett": tuple(ADRESSE.values())}
    con.execute("UPDATE paamelding SET faktura_adresse=?, faktura_postnr=?, faktura_sted=?, betaler=?, org_navn=?, org_nr=?",
                (*felt[kopi], betaler, "EKSEMPEL KOMMUNE" if betaler == "organisasjon" else None, FIRMA if betaler == "organisasjon" else None))
    con.execute("UPDATE deltaker SET adresse=?, postnr=?, poststed=?", felt[person])
    con.commit()
    venter = privatadresse.venter(con, pid)
    _kjor(con, pid)
    forventet_venter = betaler == "person" and "komplett" not in (kopi, person)
    assert venter is forventet_venter
    assert (_fakturaer(con) == 0) is forventet_venter                  # holdt tilbake akkurat når visningen sier at den venter


def test_ekstradeltaker_gratiskurs_og_firmakurs_er_aldri_med_i_ventelisten(con):
    kid = _kurs(con)
    gratis = _kurs(con, "G1", pris_nok=0)
    ingen = _kurs(con, "I1", fakturering="ingen")
    p1 = _reg(con, gratis, epost="g@eksempel.no")
    p2 = _reg(con, ingen, epost="i@eksempel.no")
    e = _reg(con, kid, epost="e@eksempel.no")
    db.sett_paamelding_status(con, e, "ekstradeltaker")
    con.commit()
    assert privatadresse.liste(con) == [] and not privatadresse.venter(con, p1) and not privatadresse.venter(con, p2)


# ============================ administrator får tydelig beskjed ============================

def test_deltakervinduet_sier_at_fakturaen_holdes_tilbake_og_hva_som_maa_fylles_inn(con, grunnlag):
    kid = _kurs(con)
    pid = _reg(con, kid)
    _kjor(con, pid)
    html = _side(_admin(), f"/admin/kurs/{kid}/deltaker/{pid}")
    assert HOLDT in html and "privat adresse, postnummer og poststed" in html and MERKE in html
    assert VENTER in html and "Gå til Personopplysninger" in html
    assert "sendes den uten adresse" not in html


def test_deltakervinduet_har_ingen_holdt_faktura_naar_adressen_er_komplett(con, grunnlag):
    kid = _kurs(con)
    pid = _reg(con, kid, ADRESSE)
    _kjor(con, pid)
    html = _side(_admin(), f"/admin/kurs/{kid}/deltaker/{pid}")
    assert HOLDT not in html and MERKE not in html and VENTER not in html


def test_deltakerlisten_viser_merke_fakturastatus_og_melding(con, grunnlag):
    kid = _kurs(con)
    _reg(con, kid)
    html = _side(_admin(), f"/admin/kurs/{kid}/deltakere")
    assert MERKE in html and VENTER in html and HOLDT in html and "betaler selv, men har ikke komplett privat adresse" in html


def test_oversikten_teller_og_lister_fakturaer_som_venter_paa_adresse(con, grunnlag):
    kid = _kurs(con)
    pid = _reg(con, kid)
    html = _side(_admin(), "/admin")
    assert HOLDT in html and f'/admin/kurs/{kid}/deltaker/{pid}' in html and 'id="adressekontroll"' in html
    db.oppdater_deltaker(con, _p(con, pid)["deltaker_id"], dict(ADRESSE), aktor="admin:test")
    con.commit()
    assert 'id="adressekontroll"' not in _side(_admin(), "/admin")      # borte når adressen er lagt inn


def test_lesetilgang_ser_beskjeden_men_ikke_knappen(con, grunnlag):
    kid = _kurs(con)
    pid = _reg(con, kid)
    db.opprett_admin_bruker(con, "lese", "Lese", "passord-som-holder", rolle="lese")
    con.commit()
    k = _klient()
    assert k.post("/admin/logg-inn", data={"brukernavn": "lese", "passord": "passord-som-holder"}).status_code == 302
    html = _side(k, f"/admin/kurs/{kid}/deltaker/{pid}")
    assert HOLDT in html and "Gå til Personopplysninger" not in html


def test_avsluttet_kurs_og_anonymisert_person_gir_ingen_oppfolging_i_oversikten(con):
    kid = _kurs(con)
    _reg(con, kid)
    con.execute("UPDATE kurs SET status='avsluttet'")
    con.commit()
    assert privatadresse.liste(con, uten_avsluttede=True) == [] and len(privatadresse.liste(con)) == 1


# ============================ lagre adressen: fakturaen går videre med en gang ============================

def test_lagrer_administrator_adressen_lages_fakturaen_med_en_gang(con, grunnlag):
    kid = _kurs(con)
    pid = _reg(con, kid)
    _kjor(con, pid)
    assert grunnlag == []
    html = _lagre_person(_admin(), kid, pid, **ADRESSE)
    assert "Personopplysninger oppdatert" in html and "Fakturaen som ventet på adressen er opprettet." in html
    assert len(grunnlag) == 1 and _fakturaer(con, pid) == 1 and (grunnlag[0].adresse, grunnlag[0].postnr) == ("Eksempelveien 1", "0150")


def test_lagre_andre_felt_uten_adresse_blokkeres_ikke_og_gir_ingen_faktura(con, grunnlag):
    """Eldre/test-deltakere uten adresse kan redigeres uten at adressen må fylles inn først."""
    kid = _kurs(con)
    pid = _reg(con, kid)
    html = _lagre_person(_admin(), kid, pid, telefon="90000000")
    assert "Personopplysninger oppdatert" in html and "Fakturaen som ventet" not in html
    assert _person(con)["telefon"] == "90000000" and grunnlag == [] and _fakturaer(con) == 0


def test_delvis_adresse_i_vinduet_avvises_og_fakturaen_venter(con, grunnlag):
    kid = _kurs(con)
    pid = _reg(con, kid)
    html = _lagre_person(_admin(), kid, pid, adresse="Vei 1", postnr="0150", poststed="")
    assert "Fyll inn" in html and _adr(_person(con)) == {"adresse": None, "postnr": None, "poststed": None}
    assert grunnlag == [] and _fakturaer(con) == 0


def test_holdt_paamelding_roeres_ikke_naar_adressen_lagres(con, grunnlag):
    """En påmelding administrator holdt tilbake (sveiper_utsatt=1) behandles først når administrator ber om det."""
    kid = _kurs(con)
    pid = _reg(con, kid, sveiper_utsatt=1)
    _lagre_person(_admin(), kid, pid, **ADRESSE)
    assert grunnlag == [] and _fakturaer(con) == 0 and _p(con, pid)["sveiper_utsatt"] == 1


def test_behandle_nå_sender_bekreftelsen_men_holder_fakturaen_tilbake(con, grunnlag):
    kid = _kurs(con)
    pid = _reg(con, kid, sveiper_utsatt=1)
    res = behandling.behandle_holdt_paamelding(con, kid, pid, date.today(), aktor="admin:test")
    assert res.kode == behandling.FULLFORT and res.arsak_kode == behandling.ARSAK_FAKTURA_VENTER and res.forsokt
    assert "venter på privat adresse" in res.arsak
    assert _p(con, pid)["sveiper_utsatt"] == 0 and _fakturaer(con) == 0 and grunnlag == []
    assert db.allerede_sendt(con, f"kurs:{kid}", EPOST, "bekreftelse")


def test_behandle_nå_i_vinduet_sier_fra_om_holdet(con, grunnlag):
    kid = _kurs(con)
    pid = _reg(con, kid, sveiper_utsatt=1)
    html = _admin().post(f"/admin/kurs/{kid}/deltaker/{pid}/behandle", follow_redirects=True).get_data(as_text=True)
    assert "Bekreftelsen er sendt" in html and HOLDT in html and "Teknisk feil" not in html and "feil ved sending" not in html


# ============================ alle veier inn krever privat adresse, også når firma betaler ============================

@pytest.mark.parametrize("vei", ["skjema", "bedriftspaamelding", "legg_til_deltaker", "webhook", "csv"])
def test_firma_som_betaler_fritar_ikke_for_privat_adresse(con, monkeypatch, vei):
    monkeypatch.setattr(config, "ADRESSE_KREVES_I_WEBHOOK", True)
    monkeypatch.setattr(config, "ADRESSE_KREVES_I_CSV", True)
    kid = _kurs(con)
    if vei == "skjema":
        r = _klient().post("/kurs/A1", data={"fornavn": "Ola", "etternavn": "Nordmann", "epost": EPOST, "samtykke": "on",
                                             "betaler": "organisasjon", "org_nr": FIRMA})
        tatt_imot = r.status_code == 200
    elif vei == "bedriftspaamelding":
        tatt_imot = _gruppe(uten_adresse=True).status_code == 302
    elif vei == "legg_til_deltaker":
        tatt_imot = _ny(kid, betaler="organisasjon", org_nr=FIRMA).status_code == 302
    elif vei == "webhook":
        tatt_imot = _webhook(_nett(org_nr=FIRMA)).status_code == 201
    else:
        from test_adresse_veier import _csv, _importer
        tatt_imot = _importer(_admin(), kid, _csv(f"Ola;Nordmann;{EPOST};;;;organisasjon;{FIRMA}"))[1] is not None
    assert tatt_imot is False and _antall(con, "paamelding") == 0


# ============================ webhook-nødbremsen ============================

@pytest.fixture
def nodbrems(monkeypatch):
    monkeypatch.setattr(config, "ADRESSE_KREVES_I_WEBHOOK", False)


def test_nodbremsen_er_av_som_standard():
    """En ren installasjon (ingen ADRESSE_KREVES_* i miljøet) krever adressen i webhooken: nødbremsen er AV til noen slår den på."""
    webhook, _csv = (v == "True" for v in _les_innstillinger().split())
    assert webhook is True


def test_nodbrems_tar_inn_paameldingen_uten_adresse_og_merker_den(con, grunnlag, nodbrems):
    kid = _kurs(con)
    r = _webhook(_nett())
    assert r.status_code == 201, r.get_json()
    j = r.get_json()
    assert "nødbrems" in j["merknad"] and "holdes tilbake" in j["merknad"]
    pid = j["paamelding_id"]
    assert _adr(_person(con)) == {"adresse": None, "postnr": None, "poststed": None}
    assert skjemafelt.adresse_mangler(_person(con)) and privatadresse.venter(con, pid)
    assert grunnlag == [] and _fakturaer(con) == 0                         # fakturaen holdes tilbake, selv om kurset starter om 30 dager
    assert db.allerede_sendt(con, f"kurs:{kid}", EPOST, "bekreftelse")     # men påmeldingen er tatt inn og bekreftet
    html = _side(_admin(), f"/admin/kurs/{kid}/deltakere")
    assert MERKE in html and VENTER in html


@pytest.mark.parametrize("sendt", [{"adresse": "Vei 1"}, {"adresse": "Vei 1", "postnr": "0150"}, {"postnr": "0150", "poststed": "Oslo"}])
def test_nodbrems_tar_inn_en_delvis_adresse_og_tar_vare_paa_det_mottatte(con, grunnlag, nodbrems, sendt):
    """Brukerens beslutning 01.10.2026: informasjon som er mottatt, kastes ikke. Den lagres som ufullstendige data, men gjelder aldri
    som adresse: registreringen er merket «Privat adresse er ufullstendig» og fakturaen holdes tilbake til alle tre er komplette."""
    kid = _kurs(con)
    r = _webhook(_nett(**sendt))
    assert r.status_code == 201, r.get_json()
    pid = r.get_json()["paamelding_id"]
    forventet = {k: sendt.get(k) for k in ("adresse", "postnr", "poststed")}
    assert _adr(_person(con)) == forventet                                                  # det som kom inn, er lagret
    assert skjemafelt.adresse_status(_person(con)) == "ufullstendig" and privatadresse.venter(con, pid)
    assert grunnlag == [] and _fakturaer(con) == 0                                          # en halv adresse gir aldri faktura
    liste = _side(_admin(), f"/admin/kurs/{kid}/deltakere")
    assert MERKE_U in liste and MERKE not in liste and VENTER in liste
    vindu = _side(_admin(), f"/admin/kurs/{kid}/deltaker/{pid}")
    assert HOLDT_U in vindu and "ikke alle lagt inn" in vindu and MERKE_U in vindu
    oversikt = _side(_admin(), "/admin")
    assert MERKE_U in oversikt and "Fakturering holdt tilbake: privat adresse mangler eller er ufullstendig" in oversikt


def test_en_delvis_adresse_kan_fullfores_og_da_fortsetter_faktureringen_automatisk(con, grunnlag, nodbrems):
    kid = _kurs(con)
    pid = _webhook(_nett(adresse="Vei 1")).get_json()["paamelding_id"]
    assert grunnlag == []
    html = _lagre_person(_admin(), kid, pid, adresse="Vei 1", postnr="0150", poststed="")                  # fortsatt ikke komplett
    assert "Fyll inn" in html and grunnlag == [] and _person(con)["postnr"] is None
    html = _lagre_person(_admin(), kid, pid, adresse="Vei 1", postnr="0150", poststed="Oslo")
    assert "Fakturaen som ventet på adressen er opprettet." in html and len(grunnlag) == 1
    assert (grunnlag[0].adresse, grunnlag[0].postnr, grunnlag[0].sted) == ("Vei 1", "0150", "Oslo") and not privatadresse.venter(con, pid)


def test_nodbrems_med_delvis_adresse_blander_aldri_to_halve_adresser_til_en_komplett_en(con, nodbrems):
    """Personen har en halv adresse fra før («Vei 1»). En ny halv adresse (postnr og sted) fra en annen registrering røres ikke inn i den:
    de to ville blitt en komplett adresse som aldri har eksistert, og fakturaen ville gått dit."""
    kid = _kurs(con)
    _reg(con, kid, {"adresse": "Vei 1"}, epost=EPOST)
    con.execute("UPDATE paamelding SET status='avmeldt'")
    _kurs(con, "A2")
    con.commit()
    r = _webhook(_nett(kurs="A2", postnr="0150", poststed="Oslo"))
    assert r.status_code == 201, r.get_json()
    assert _adr(_person(con)) == {"adresse": "Vei 1", "postnr": None, "poststed": None}
    assert skjemafelt.adresse_status(_person(con)) == "ufullstendig"


def test_nodbrems_overskriver_ikke_en_komplett_adresse_med_en_delvis(con, nodbrems):
    kid = _kurs(con)
    _reg(con, kid, ADRESSE, epost=EPOST)
    con.execute("UPDATE paamelding SET status='avmeldt'")
    _kurs(con, "A2")
    con.commit()
    r = _webhook(_nett(kurs="A2", adresse="Nyveien 9"))                                      # bare gata er sendt
    assert r.status_code == 201, r.get_json()
    assert _adr(_person(con)) == ADRESSE and "merknad" not in r.get_json()                 # personen har komplett adresse fra før


def test_nodbrems_krever_fortsatt_gyldige_verdier(con, nodbrems):
    _kurs(con)
    assert _webhook(_nett(adresse="x" * 300)).status_code == 400
    assert _webhook(_nett(adresse="Vei 1\nlinje 2")).status_code == 400
    assert _antall(con, "paamelding") == 0


def test_nodbrems_med_komplett_adresse_lagres_som_vanlig(con, grunnlag, nodbrems):
    _kurs(con)
    r = _webhook(_nett(**ADRESSE))
    assert r.status_code == 201 and "merknad" not in r.get_json() and _adr(_person(con)) == ADRESSE
    assert len(grunnlag) == 1                                                                   # privat faktura med adresse som vanlig


def test_nodbrems_og_firma_som_betaler_faktureres_paa_firmaets_adresse(con, grunnlag, nodbrems):
    _kurs(con)
    r = _webhook(_nett(org_nr=FIRMA))
    assert r.status_code == 201 and "merknad" in r.get_json()          # privat adresse mangler (merket), men firmaet betaler
    assert len(grunnlag) == 1 and not grunnlag[0].er_privatperson


def test_naar_adressen_er_lagt_inn_etter_nodbremsen_lages_fakturaen(con, grunnlag, nodbrems):
    kid = _kurs(con)
    pid = _webhook(_nett()).get_json()["paamelding_id"]
    assert grunnlag == []
    html = _lagre_person(_admin(), kid, pid, **ADRESSE)
    assert "Fakturaen som ventet på adressen er opprettet." in html and len(grunnlag) == 1 and _fakturaer(con, pid) == 1


def test_uten_nodbrems_avvises_en_manglende_adresse_med_400(con):
    _kurs(con)
    r = _webhook(_nett())
    assert r.status_code == 400 and "deltakerens private adresse er påkrevd" in r.get_json()["melding"]
    assert _antall(con, "paamelding") == 0


def test_ipr_er_oppdatert_naar_nodbremsen_er_av_kreves_adressen_igjen(con, monkeypatch):
    """Overgangen: nødbrems på -> en uten adresse tas inn; nødbrems av -> samme kall avvises (kravet er tilbake som normalt)."""
    _kurs(con)
    monkeypatch.setattr(config, "ADRESSE_KREVES_I_WEBHOOK", False)
    assert _webhook(_nett()).status_code == 201
    monkeypatch.setattr(config, "ADRESSE_KREVES_I_WEBHOOK", True)
    assert _webhook(_nett(epost="neste@eksempel.no")).status_code == 400
    assert _webhook(_nett(epost="neste@eksempel.no", **ADRESSE)).status_code == 201


def test_nodbremsens_merknad_og_hold_logges_aldri_med_adresse(con, nodbrems):
    kid = _kurs(con)
    assert _webhook(_nett(adresse="Sporbarveien 4711")).status_code == 201
    tekst = " ".join(str(dict(h)) for h in con.execute("SELECT * FROM hendelse"))
    assert "Sporbarveien" not in tekst and kid


def test_dokumentasjonen_beskriver_holdet_og_nodbremsen():
    from pathlib import Path
    rot = Path(__file__).resolve().parent.parent
    drift = (rot / "OPERATIONS.md").read_text(encoding="utf-8")
    gdpr = (rot / "GDPR.md").read_text(encoding="utf-8")
    for ord_ in ("Privat adresse mangler", "holdes tilbake", "nødbrems", "ADRESSE_KREVES_I_WEBHOOK"):
        assert ord_ in drift, ord_
    assert "sendes den uten adresse" not in gdpr and "går den da uten adresse" not in drift
    assert re.search(r"privat faktura[^.]*aldri[^.]*uten[^.]*adresse", gdpr + drift, re.I)


# ============================ merkene, og ekstradeltaker som settes tilbake til Påmeldt ============================

@pytest.mark.parametrize("adresse, forventet", [
    ({}, "mangler"), ({"adresse": "Vei 1"}, "ufullstendig"), ({"postnr": "0150", "poststed": "Oslo"}, "ufullstendig"),
    ({"adresse": "  ", "postnr": "", "poststed": None}, "mangler"), (dict(ADRESSE), "komplett")])
def test_adresse_status_og_merke(adresse, forventet):
    person = {"adresse": None, "postnr": None, "poststed": None, **adresse}
    assert skjemafelt.adresse_status(person) == forventet
    assert skjemafelt.adresse_merke(person) == {"mangler": MERKE, "ufullstendig": MERKE_U, "komplett": None}[forventet]
    assert skjemafelt.adresse_mangler(person) is (forventet != "komplett")


def test_overskriften_i_varselboksen_folger_om_adressen_mangler_eller_er_ufullstendig():
    assert privatadresse.holdt_overskrift({"adresse": None, "postnr": None, "poststed": None}) == HOLDT
    assert privatadresse.holdt_overskrift({"adresse": "Vei 1", "postnr": None, "poststed": None}) == HOLDT_U


def test_ekstradeltaker_satt_tilbake_til_paameldt_faar_ikke_automatisk_faktura_naar_adressen_lagres(con, grunnlag):
    """Brukerens beslutning 01.10.2026: en ekstradeltaker som endres til vanlig deltaker, får fortsatt ikke faktura automatisk (bekreftelsen er sendt
    manuelt): administrator velger det selv. Å lagre en komplett privat adresse endrer ikke det, og meldingen sier det."""
    kid = _kurs(con)
    pid = _reg(con, kid)                                                        # privat betaler uten adresse
    db.sett_paamelding_status(con, pid, "ekstradeltaker")
    con.commit()
    res = behandling.behandle_holdt_paamelding(con, kid, pid, date.today(), aktor="admin:test")    # bekreftelsen sendes manuelt
    assert res.kode == behandling.FULLFORT and _p(con, pid)["sveiper_kjort"] == 1
    db.sett_paamelding_status(con, pid, "paameldt")                             # tilbake til vanlig deltaker
    con.commit()
    p = _p(con, pid)
    assert (p["sveiper_kjort"], p["sveiper_utsatt"]) == (0, 1) and p["ekstradeltaker_ts"] is None          # holdt tilbake av systemet
    assert _fakturaer(con) == 0 and privatadresse.venter(con, pid)
    html = _lagre_person(_admin(), kid, pid, **ADRESSE)
    assert "Personopplysninger oppdatert" in html and "fakturaen lages først når du selv velger" in html
    assert "Fakturaen som ventet på adressen er opprettet." not in html
    assert grunnlag == [] and _fakturaer(con) == 0 and _p(con, pid)["sveiper_utsatt"] == 1             # ingen automatisk faktura
    daglig.kjor(Kjoring(con, idag=date.today()))
    con.commit()
    assert grunnlag == [] and _fakturaer(con) == 0                              # heller ikke av morgenjobben
    vindu = _side(_admin(), f"/admin/kurs/{kid}/deltaker/{pid}")
    assert "Behandling holdt tilbake" in vindu and "Behandle fakturering nå" in vindu                    # administrator velger selv
