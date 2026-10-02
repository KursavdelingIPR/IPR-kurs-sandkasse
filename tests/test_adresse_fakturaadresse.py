"""Fakturaadressen til en PRIVAT betaler følger deltakerens adresse (kritikerrunden 29.09.2026).

Adressen kopieres fra personen til påmeldingens fakturaadresse når påmeldingen registreres (db.meld_paa), og fakturamotoren
leser kopien. Kopien er derfor en engangskopi, med mindre noe holder den i takt med personen. Regelen som testes her:

  * Rettes personens adresse i deltakervinduet (db.oppdater_deltaker), følger fakturaadressen med på privat betalers
    påmeldinger som ikke er ferdig fakturert - men bare når kopien er uendret siden registreringen (eller tom). En
    fakturaadresse administrator har skrevet selv, adressen fra en annen registrering og firmaets adresse røres ikke.
  * En NY adresse fra en offentlig påmelding (ingen innlogging) flytter aldri fakturaadressen på personens andre,
    eksisterende påmeldinger. Overskrivingen av personens adresse logges (bare feltnavn).
  * Byttes betaler fra firma til privat, eller står fakturaadressen tom, brukes personens adresse - både når
    administrator lagrer og når fakturaen lages (sveiperen). Firma faktureres aldri på en privat adresse.
  * Den private adressen kopieres på ALLE kurs, også gratiskurs (brukerens beslutning: adressen skal alltid legges til).
Alle testdata er fiktive."""
import json
from datetime import date, timedelta

import pytest

from adressehjelp import ADRESSE, ANNEN_ADRESSE, SPORBAR_ADRESSE
from kurs import config, db, hendelseslogg, sveiper
from kurs.integrasjoner import brreg, visma
from kurs.kjoring import Kjoring

pytestmark = pytest.mark.uten_standardadresse   # registrerer bevisst uten adresse (se conftest.py)

FIRMA = "999900003"          # EKSEMPEL KOMMUNE, Postboks 100, 1234 EKSEMPELBY (oppdiktet)
FIRMA_ADRESSE = {"adresse": "Postboks 100", "postnr": "1234", "poststed": "EKSEMPELBY"}
NEDE = brreg.DEMO_NEDE_ORGNR                   # registeret «svarer ikke» (demo): påmeldingen får bare organisasjonsnummeret
EPOST = "rita.retting@eksempel.no"
INGEN = {"adresse": None, "postnr": None, "poststed": None}


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


@pytest.fixture
def grunnlag(monkeypatch):
    """Fakturagrunnlagene som sendes til Visma (i demo lages ingen ekte faktura)."""
    ut = []

    def fake(g):
        ut.append(g)
        return visma.Fakturaresultat(visma_id=f"v{len(ut)}", faktura_nr=f"F{len(ut)}", kunde_id="k")
    monkeypatch.setattr(visma, "fakturer", fake)
    return ut


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


def _holdt(con, kid, epost=EPOST, deltaker=None, **paamelding):
    """En påmelding registrert manuelt av administrator: fakturaen holdes til administrator slipper den."""
    pid, _ = db.meld_paa(con, kid, epost=epost, fornavn="Rita", etternavn="Retting",
                         deltaker=dict(ADRESSE if deltaker is None else deltaker),
                         paamelding={"sveiper_utsatt": 1, **paamelding})
    con.commit()
    return pid


def _person(con, epost=EPOST):
    return con.execute("SELECT * FROM deltaker WHERE epost=?", (epost,)).fetchone()


def _p(con, pid):
    return con.execute("SELECT * FROM paamelding WHERE id=?", (pid,)).fetchone()


def _adr(rad) -> dict:
    return {"adresse": rad["adresse"], "postnr": rad["postnr"], "poststed": rad["poststed"]}


def _fakt(p) -> dict:
    return {"adresse": p["faktura_adresse"], "postnr": p["faktura_postnr"], "poststed": p["faktura_sted"]}


def _sendt_adresse(g) -> dict:
    return {"adresse": g.adresse, "postnr": g.postnr, "poststed": g.sted}


def _rett_adressen(k, kid, pid, adresse, **over):
    """Slik administrator gjør det: Personopplysninger i deltakervinduet."""
    data = {"fornavn": "Rita", "etternavn": "Retting", "epost": EPOST, "telefon": "", "yrkestittel": "", "arbeidssted": "",
            **adresse, **over}
    return k.post(f"/admin/kurs/{kid}/deltaker/{pid}/person", data=data, follow_redirects=True)


def _slipp(k, kid, pid):
    return k.post(f"/admin/kurs/{kid}/deltaker/{pid}/behandle", follow_redirects=True)


# ============================ rettelsen i deltakervinduet følger med til fakturaen ============================

def test_rettet_adresse_i_deltakervinduet_gaar_til_fakturaen_som_slippes_etterpaa(con, grunnlag):
    kid = _kurs(con)
    pid = _holdt(con, kid)
    k = _admin()
    assert _fakt(_p(con, pid)) == ADRESSE                        # kopien fra registreringen
    r = _rett_adressen(k, kid, pid, ANNEN_ADRESSE)
    assert "Personopplysninger oppdatert" in r.get_data(as_text=True)
    assert _adr(_person(con)) == ANNEN_ADRESSE and _fakt(_p(con, pid)) == ANNEN_ADRESSE
    _slipp(k, kid, pid)
    assert len(grunnlag) == 1 and grunnlag[0].er_privatperson
    assert _sendt_adresse(grunnlag[0]) == ANNEN_ADRESSE          # ikke den gamle adressen


def test_rettet_adresse_foelger_med_paa_alle_ikke_fakturerte_paameldinger_for_privat_betaler(con):
    a, b = _kurs(con, "A1"), _kurs(con, "A2")
    p1, p2 = _holdt(con, a), _holdt(con, b)
    db.oppdater_deltaker(con, _p(con, p1)["deltaker_id"], dict(ANNEN_ADRESSE), aktor="admin:test")
    assert _fakt(_p(con, p1)) == ANNEN_ADRESSE and _fakt(_p(con, p2)) == ANNEN_ADRESSE


def test_en_egen_fakturaadresse_fra_administrator_flyttes_ikke(con):
    kid = _kurs(con)
    pid = _holdt(con, kid)
    egen = {"faktura_adresse": "Regnskapsveien 9", "faktura_postnr": "0180", "faktura_sted": "Oslo"}
    db.oppdater_paamelding(con, pid, egen)
    db.oppdater_deltaker(con, _p(con, pid)["deltaker_id"], dict(ANNEN_ADRESSE), aktor="admin:test")
    assert _adr(_person(con)) == ANNEN_ADRESSE
    assert _fakt(_p(con, pid)) == {"adresse": "Regnskapsveien 9", "postnr": "0180", "poststed": "Oslo"}


def test_en_ferdig_fakturert_paamelding_beholder_adressen_den_ble_fakturert_paa(con, grunnlag):
    kid = _kurs(con)
    pid = _holdt(con, kid)
    _slipp(_admin(), kid, pid)
    assert len(grunnlag) == 1 and _sendt_adresse(grunnlag[0]) == ADRESSE
    db.oppdater_deltaker(con, _p(con, pid)["deltaker_id"], dict(ANNEN_ADRESSE), aktor="admin:test")
    assert _fakt(_p(con, pid)) == ADRESSE                         # historikk: fakturaen er sendt dit


def test_delfakturaer_som_gjenstaar_gaar_til_den_rettede_adressen(con):
    kid = _kurs(con, betaling="per_samling")
    pid = _holdt(con, kid)
    dag = db.kursdager(con, kid)[0]["id"]
    con.execute("INSERT INTO faktura (paamelding_id, kursdag_id, visma_id, faktura_nr, belop_nok, status) "
                "VALUES (?,?,?,?,?, 'sendt')", (pid, dag, "v0", "F0", 2250))
    con.commit()
    db.oppdater_deltaker(con, _p(con, pid)["deltaker_id"], dict(ANNEN_ADRESSE), aktor="admin:test")
    assert _fakt(_p(con, pid)) == ANNEN_ADRESSE                   # første delfaktura er sendt, neste er ikke


def test_firmaets_fakturaadresse_roeres_aldri_av_en_rettet_privatadresse(con):
    kid = _kurs(con)
    r = _admin().post(f"/admin/kurs/{kid}/deltaker/ny", data={
        "fornavn": "Rita", "etternavn": "Retting", "epost": EPOST, "betaler": "organisasjon", "org_nr": FIRMA, **ADRESSE})
    assert r.status_code == 302
    pid = con.execute("SELECT id FROM paamelding").fetchone()[0]
    assert _fakt(_p(con, pid)) == FIRMA_ADRESSE
    db.oppdater_deltaker(con, _p(con, pid)["deltaker_id"], dict(ANNEN_ADRESSE), aktor="admin:test")
    assert _adr(_person(con)) == ANNEN_ADRESSE and _fakt(_p(con, pid)) == FIRMA_ADRESSE


def test_en_firmapaamelding_uten_hentet_adresse_faar_aldri_privatadressen_naar_den_rettes(con):
    """Registeret svarte ikke: firmaets fakturaadresse er tom. Da er «tom kopi» ikke det samme som «følg personen»: den
    private adressen skal aldri havne på en firmafaktura."""
    kid = _kurs(con)
    r = _admin().post(f"/admin/kurs/{kid}/deltaker/ny", data={
        "fornavn": "Rita", "etternavn": "Retting", "epost": EPOST, "betaler": "organisasjon", "org_nr": NEDE, **ADRESSE})
    assert r.status_code == 302
    pid = con.execute("SELECT id FROM paamelding").fetchone()[0]
    assert _fakt(_p(con, pid)) == INGEN
    db.oppdater_deltaker(con, _p(con, pid)["deltaker_id"], dict(ANNEN_ADRESSE), aktor="admin:test")
    assert _adr(_person(con)) == ANNEN_ADRESSE and _fakt(_p(con, pid)) == INGEN


def test_eldre_paamelding_uten_adresse_faar_adressen_naar_administrator_legger_den_inn(con, grunnlag):
    """Slik en påmelding fra før migrering 14 ser ut: verken personen eller påmeldingen har adresse."""
    kid = _kurs(con)
    pid = _holdt(con, kid, deltaker={})
    assert _adr(_person(con)) == INGEN and _fakt(_p(con, pid)) == INGEN
    k = _admin()
    _rett_adressen(k, kid, pid, ADRESSE)
    assert _fakt(_p(con, pid)) == ADRESSE
    _slipp(k, kid, pid)
    assert _sendt_adresse(grunnlag[0]) == ADRESSE


def test_fakturaadressen_som_foelger_med_logges_bare_med_feltnavn(con):
    kid = _kurs(con)
    pid = _holdt(con, kid)
    db.oppdater_deltaker(con, _p(con, pid)["deltaker_id"], dict(SPORBAR_ADRESSE), aktor="admin:test")
    hendelser = con.execute("SELECT handling, detaljer FROM hendelse WHERE handling='paamelding_endret'").fetchall()
    assert len(hendelser) == 1 and json.loads(hendelser[0]["detaljer"])["felt"] == [
        "faktura_adresse", "faktura_postnr", "faktura_sted"]
    alt = " ".join(h["detaljer"] or "" for h in con.execute("SELECT detaljer FROM hendelse")).lower()
    assert "sporbar" not in alt and "4711" not in alt
    p = _p(con, pid)
    tekst = " ".join(r.hva + " " + " ".join(r.detaljer) for r in hendelseslogg.for_paamelding(con, p, systemadmin=True).rader)
    assert "Fakturaadresse" in tekst and "sporbar" not in tekst.lower()


# ============================ en offentlig påmelding kan ikke flytte andre fakturaer ============================

def test_ny_adresse_fra_en_offentlig_paamelding_flytter_ikke_fakturaadressen_paa_eksisterende_paameldinger(con):
    """Den offentlige påmeldingen har ingen innlogging: en ny adresse overskriver personens adresse (den nyeste er
    riktigst, som for telefon og arbeidssted), men fakturaen som venter på et annet kurs går fortsatt dit personen sa."""
    a, b = _kurs(con, "A1"), _kurs(con, "A2")
    holdt = _holdt(con, a)
    r = _klient().post("/kurs/A2", data={"fornavn": "Rita", "etternavn": "Retting", "epost": EPOST.upper(),
                                         "samtykke": "on", "betaler": "person", **ANNEN_ADRESSE})
    assert r.status_code == 200
    assert _adr(_person(con)) == ANNEN_ADRESSE
    assert _fakt(_p(con, holdt)) == ADRESSE                       # uendret: den ventende fakturaen på A1
    ny = con.execute("SELECT * FROM paamelding WHERE kurs_id=?", (b,)).fetchone()
    assert _fakt(ny) == ANNEN_ADRESSE                             # den nye påmeldingen: adressen som ble oppgitt nå


def test_en_overskrevet_adresse_logges_med_feltnavn_og_aldri_med_verdier(con):
    a, b = _kurs(con, "A1"), _kurs(con, "A2")
    holdt = _holdt(con, a)
    _klient().post("/kurs/A2", data={"fornavn": "Rita", "etternavn": "Retting", "epost": EPOST, "samtykke": "on",
                                     "betaler": "person", **SPORBAR_ADRESSE})
    hendelser = [h for h in con.execute("SELECT aktor, detaljer FROM hendelse WHERE handling='deltaker_endret'")]
    assert len(hendelser) == 1
    assert json.loads(hendelser[0]["detaljer"]) == {"deltaker_id": _person(con)["id"],
                                                    "felt": ["adresse", "postnr", "poststed"]}
    assert hendelser[0]["aktor"] == f"deltaker:{_person(con)['id']}"
    alt = " ".join(h["detaljer"] or "" for h in con.execute("SELECT detaljer FROM hendelse")).lower()
    assert "sporbar" not in alt and "4711" not in alt
    # ... og det står i Logger for personens påmeldinger, så administrator kan se at adressen ble byttet
    tekst = " ".join(r.hva + " " + " ".join(r.detaljer) + " " + r.hvem
                     for r in hendelseslogg.for_paamelding(con, _p(con, holdt), systemadmin=False).rader)
    assert "Endret: Adresse, Postnummer, Poststed" in tekst and "Deltakeren selv" in tekst


def test_en_overskriving_fra_administrators_manuelle_registrering_logges_paa_administratoren(con):
    a, b = _kurs(con, "A1"), _kurs(con, "A2")
    _holdt(con, a)
    r = _admin().post(f"/admin/kurs/{b}/deltaker/ny", data={
        "fornavn": "Rita", "etternavn": "Retting", "epost": EPOST, "betaler": "person", **ANNEN_ADRESSE})
    assert r.status_code == 302
    hendelser = con.execute("SELECT aktor FROM hendelse WHERE handling='deltaker_endret'").fetchall()
    assert len(hendelser) == 1 and hendelser[0]["aktor"].startswith("admin")


@pytest.mark.parametrize("ny", [dict(ADRESSE), {"adresse": "", "postnr": "", "poststed": ""}, {}])
def test_uendret_eller_tom_adresse_ved_paamelding_gir_ingen_logg(con, ny):
    a, b = _kurs(con, "A1"), _kurs(con, "A2")
    _holdt(con, a)
    db.meld_paa(con, b, epost=EPOST, fornavn="Rita", etternavn="Retting", deltaker=ny)
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='deltaker_endret'").fetchone()[0] == 0
    assert _adr(_person(con)) == ADRESSE


def test_en_adresse_som_fylles_inn_paa_en_eldre_person_er_ingen_overskriving(con):
    a, b = _kurs(con, "A1"), _kurs(con, "A2")
    _holdt(con, a, deltaker={})
    db.meld_paa(con, b, epost=EPOST, fornavn="Rita", etternavn="Retting", deltaker=dict(ADRESSE))
    assert _adr(_person(con)) == ADRESSE
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='deltaker_endret'").fetchone()[0] == 0


def test_csv_import_overskriver_ikke_og_logger_derfor_ingenting(con):
    a, b = _kurs(con, "A1"), _kurs(con, "A2")
    _holdt(con, a)
    db.meld_paa(con, b, epost=EPOST, fornavn="Rita", etternavn="Retting", deltaker=dict(ANNEN_ADRESSE),
                beskytt_eksisterende_felt=True)
    assert _adr(_person(con)) == ADRESSE
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='deltaker_endret'").fetchone()[0] == 0


# ============================ byttet fra firma til privat, eller tom fakturaadresse ============================

def _hold_org(con, kid):
    r = _admin().post(f"/admin/kurs/{kid}/deltaker/ny", data={
        "fornavn": "Rita", "etternavn": "Retting", "epost": EPOST, "betaler": "organisasjon", "org_nr": FIRMA, **ADRESSE})
    assert r.status_code == 302
    return con.execute("SELECT id FROM paamelding").fetchone()[0]


def test_byttet_fra_firma_til_privat_betaler_gir_personens_adresse_som_fakturaadresse(con, grunnlag):
    kid = _kurs(con)
    pid = _hold_org(con, kid)
    assert _fakt(_p(con, pid)) == FIRMA_ADRESSE
    k = _admin()
    r = k.post(f"/admin/kurs/{kid}/deltaker/{pid}/paamelding", data={"betaler": "person", "org_nr": FIRMA},
               follow_redirects=True)
    assert "Påmeldingen er oppdatert" in r.get_data(as_text=True)
    p = _p(con, pid)
    assert p["betaler"] == "person" and _fakt(p) == ADRESSE       # ikke tomt, og ikke firmaets adresse
    _slipp(k, kid, pid)
    assert len(grunnlag) == 1 and grunnlag[0].er_privatperson and _sendt_adresse(grunnlag[0]) == ADRESSE


def test_tomme_fakturaadressefelt_for_privat_betaler_gir_personens_adresse(con):
    kid = _kurs(con)
    pid = _holdt(con, kid)
    _admin().post(f"/admin/kurs/{kid}/deltaker/{pid}/paamelding", data={
        "betaler": "person", "faktura_adresse": "", "faktura_postnr": " ", "faktura_sted": ""}, follow_redirects=True)
    assert _fakt(_p(con, pid)) == ADRESSE


def test_en_egen_fakturaadresse_i_skjemaet_vinner_over_personens(con):
    kid = _kurs(con)
    pid = _holdt(con, kid)
    _admin().post(f"/admin/kurs/{kid}/deltaker/{pid}/paamelding", data={
        "betaler": "person", "faktura_adresse": "Regnskapsveien 9", "faktura_postnr": "0180", "faktura_sted": "Oslo"})
    assert _fakt(_p(con, pid)) == {"adresse": "Regnskapsveien 9", "postnr": "0180", "poststed": "Oslo"}
    assert _adr(_person(con)) == ADRESSE


# ============================ fakturamotoren: personens adresse når kopien mangler ============================

def test_sveiperen_bruker_personens_adresse_naar_privat_betaler_ikke_har_kopi(con, grunnlag):
    """Kopien er tom (eldre påmelding), men personen har en adresse: fakturaen går dit i stedet for uten adresse."""
    kid = _kurs(con)
    pid = _holdt(con, kid, deltaker={})
    con.execute("UPDATE deltaker SET adresse=?, postnr=?, poststed=?", tuple(ANNEN_ADRESSE.values()))    # utenom db-laget
    con.commit()
    assert _fakt(_p(con, pid)) == INGEN
    con.execute("UPDATE paamelding SET sveiper_utsatt=0")
    con.commit()
    sveiper.kjor(Kjoring(con, idag=date.today()), pid)
    assert len(grunnlag) == 1 and _sendt_adresse(grunnlag[0]) == ANNEN_ADRESSE


def test_sveiperen_bruker_kopien_paa_paameldingen_naar_den_finnes(con, grunnlag):
    kid = _kurs(con)
    pid = _holdt(con, kid)
    con.execute("UPDATE deltaker SET adresse=?, postnr=?, poststed=?", tuple(ANNEN_ADRESSE.values()))    # utenom db-laget
    con.execute("UPDATE paamelding SET sveiper_utsatt=0")
    con.commit()
    sveiper.kjor(Kjoring(con, idag=date.today()), pid)
    assert _sendt_adresse(grunnlag[0]) == ADRESSE                 # kopien er beslutningen som ble tatt ved registreringen


def test_sveiperen_bruker_aldri_privatadressen_til_en_firmafaktura(con, grunnlag):
    kid = _kurs(con)
    pid = _hold_org(con, kid)
    con.execute("UPDATE paamelding SET faktura_adresse=NULL, faktura_postnr=NULL, faktura_sted=NULL, sveiper_utsatt=0")
    con.commit()
    sveiper.kjor(Kjoring(con, idag=date.today()), pid)
    assert len(grunnlag) == 1 and not grunnlag[0].er_privatperson
    assert _sendt_adresse(grunnlag[0]) == INGEN                   # aldri automatisk over til deltakerens private adresse


def test_privat_betaler_uten_noen_adresse_faar_ingen_faktura_foer_adressen_er_lagt_inn(con, grunnlag):
    """JUSTERT 01.10.2026 (var: fakturaen gikk uten adresse): en privat faktura lages aldri uten komplett privat adresse. Eldre
    påmelding uten adresse noe sted: fakturaen holdes tilbake (se test_privat_adresse_faktura.py), og lages når adressen er inn."""
    kid = _kurs(con)
    pid = _holdt(con, kid, deltaker={})
    con.execute("UPDATE paamelding SET sveiper_utsatt=0")
    con.commit()
    sveiper.kjor(Kjoring(con, idag=date.today()), pid)
    assert grunnlag == []
    db.oppdater_deltaker(con, _p(con, pid)["deltaker_id"], dict(ADRESSE), aktor="admin:test")
    con.commit()
    sveiper.kjor(Kjoring(con, idag=date.today()), pid)
    assert len(grunnlag) == 1 and _sendt_adresse(grunnlag[0]) == ADRESSE


# ============================ gratiskurs: adressen kopieres også dit (brukerens beslutning) ============================

@pytest.mark.parametrize("kw", [dict(pris_nok=0, fakturering="ingen"), dict(pris_nok=0), dict(fakturering="ingen")])
def test_privatadressen_kopieres_ogsaa_naar_kurset_ikke_faktureres(con, kw):
    """Deltakerens private adresse skal ALLTID legges til - ikke bare på kurs der fakturablokken vises."""
    _kurs(con, **kw)
    r = _klient().post("/kurs/A1", data={"fornavn": "Rita", "etternavn": "Retting", "epost": EPOST, "samtykke": "on",
                                         **ADRESSE})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    p = con.execute("SELECT * FROM paamelding").fetchone()
    assert p["betaler"] == "person" and _fakt(p) == ADRESSE and _adr(_person(con)) == ADRESSE
