"""«Firmaopplysninger må kontrolleres»: registeret svarer ikke når firma betaler.

Laaste krav (brukerens beslutning 29.09.2026):
  * Deltakeren skriver ALDRI firmanavn eller adresse selv - heller ikke når Enhetsregisteret ikke svarer.
  * Svarer ikke registeret: organisasjonsnummeret beholdes, deltakeren faar en tydelig melding, paameldingen gaar
    gjennom, og den er merket «Firmaopplysninger maa kontrolleres» for administrator.
  * Administrator og morgenjobben kan proeve oppslaget paa nytt. Firmanavn og adresse kommer da fra registeret.
  * Fakturaen lages ikke uten firmanavn - den tas igjen naar opplysningene er paa plass.
  * Ingenting logges med organisasjonsnummer, navn eller adresse.
Alle testdata er fiktive (oppdiktede virksomheter fra brreg.DEMO_ENHETER; brreg.DEMO_NEDE_ORGNR er «registeret er nede»)."""
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from adressehjelp import ADRESSE
from kurs import config, daglig, db, firmaopplysninger, hendelseslogg, sveiper
from kurs.integrasjoner import brreg, epost
from kurs.kjoring import Kjoring
from kurs.sveiper import PLAN_NA, PLAN_PER_SAMLING, FakturaPlan

MERKE = "Firmaopplysninger må kontrolleres"
MELDING = "Vi kunne ikke hente firmaopplysningene akkurat nå"
NEDE = brreg.DEMO_NEDE_ORGNR                      # registeret svarer ikke for dette nummeret (demo)
FIRMA = "999900003"                               # EKSEMPEL KOMMUNE, Postboks 100, 1234 EKSEMPELBY
BASIS = {"fornavn": "Test", "etternavn": "Person", "epost": "test.person@eksempel.no", "samtykke": "on", "samtykke_lagring": "on", **ADRESSE}


@pytest.fixture(autouse=True)
def _demo(monkeypatch):
    monkeypatch.setattr(config, "DEMO", True)


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    monkeypatch.setattr(config, "BASE_URL", "https://kurs.eksempel.no")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _klient():
    from kurs.web import app as webapp
    return webapp.app.test_client()


def _admin(brukernavn=None, passord=None):
    k = _klient()
    k.post("/admin/logg-inn", data={"brukernavn": brukernavn or config.ADMIN_BRUKERNAVN,
                                    "passord": passord or config.ADMIN_PASSORD})
    return k


@pytest.fixture
def admin():
    return _admin()


def _kurs(con, kode="S1", frem=30, **kw):
    start = date.today() + timedelta(days=frem)
    felter = {"navn": "Eksempelkurs", "type": "fysisk", "sted": "Eksempelsted", "pris_nok": 4500,
              "fakturering": "person", "betaling": "samlet", "kapasitet": 10, **kw}
    kid = db.opprett_kurs(con, kode=kode, datoer=[start.isoformat(), (start + timedelta(days=1)).isoformat()],
                          sharepoint_mappe=f"K/{kode}", **felter)
    con.commit()
    return kid


def _firma(**over):
    return {**BASIS, "betaler": "organisasjon", "org_nr": "999 900 062", **over}


def _flagget(con, kid, epost_="a@eksempel.no", **over):
    """En firmapåmelding der registeret ikke svarte: bare organisasjonsnummeret er lagret."""
    pid, _ = db.meld_paa(con, kid, epost=epost_, fornavn="Test", etternavn="Person",
                         paamelding={"betaler": "organisasjon", "org_nr": NEDE, **over})
    con.commit()
    return pid


def _paamelding(con, pid=None):
    return con.execute("SELECT * FROM paamelding" + (" WHERE id=?" if pid else " ORDER BY id DESC"),
                       (pid,) if pid else ()).fetchone()


def _hendelser(con, handling=None):
    rader = [dict(r) for r in con.execute("SELECT handling, detaljer FROM hendelse ORDER BY id")]
    return [r for r in rader if handling is None or r["handling"] == handling]


def _fakturaer(con):
    return con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0]


def _forsok(con):
    return con.execute("SELECT COUNT(*) FROM faktura_forsok").fetchone()[0]


def _mailtekst():
    return " ".join(f.read_text(encoding="utf-8") for f in sorted(config.UTBOKS.glob("*.html"))) \
        if config.UTBOKS.exists() else ""


def _vindu(kid, pid):
    return f"/admin/kurs/{kid}/deltaker/{pid}"


# ============================ oppslaget (demo) ============================

def test_demonummeret_for_registeret_nede_er_gyldig_og_kommer_ikke_opp_i_soket():
    assert brreg.gyldig_orgnr(NEDE) and NEDE not in {e.orgnr for e in brreg.DEMO_ENHETER}
    assert brreg.sok("oppe igjen") == [] and all(e.orgnr != NEDE for e in brreg.sok("demo"))


def test_hent_svarer_ikke_for_demonummeret_men_hent_paa_nytt_gjor():
    with pytest.raises(brreg.Utilgjengelig):
        brreg.hent(NEDE)
    with pytest.raises(brreg.Utilgjengelig):
        brreg.hent(" 999 900 062 ")
    enhet = brreg.hent_paa_nytt(NEDE)
    assert (enhet.navn, enhet.adresse, enhet.postnr, enhet.poststed) == (
        "OPPE IGJEN DEMO AS", "Retteveien 2", "1234", "EKSEMPELBY")
    assert brreg.hent_paa_nytt(FIRMA) == brreg.hent(FIRMA) and brreg.hent_paa_nytt("999900004") is None


def test_hent_paa_nytt_i_drift_er_vanlig_oppslag_uten_demoenhet(monkeypatch):
    monkeypatch.setattr(config, "DEMO", False)
    kall = []

    def nede(nr):
        kall.append(nr)
        raise brreg.Utilgjengelig()
    monkeypatch.setattr(brreg, "hent", nede)
    with pytest.raises(brreg.Utilgjengelig):                       # ingen demoenhet utenfor demo
        brreg.hent_paa_nytt(NEDE)
    assert kall == [NEDE]
    monkeypatch.setattr(brreg, "hent", lambda nr: brreg.DEMO_ENHETER[0])
    assert brreg.hent_paa_nytt("974 760 673") is brreg.DEMO_ENHETER[0]


def test_oppslag_fra_skjemaet_gir_utilgjengelig_for_demonummeret(con):
    _kurs(con)
    r = _klient().get(f"/kurs/S1/enhet?orgnr={NEDE}")
    assert r.status_code == 503 and r.get_json() == {"status": "utilgjengelig"}
    assert _klient().get("/kurs/S1/enhet?sok=oppe igjen").get_json() == {"status": "ok", "treff": []}


# ============================ deltakeren ============================

@pytest.mark.parametrize("skrevet", [
    {}, {"org_navn": " Eget Firma AS ", "org_adresse": "Egen gate 1", "org_postnr": "9999", "org_sted": "Egenby",
         "faktura_adresse": "Egen gate 2", "faktura_postnr": "8888", "faktura_sted": "Egenby 2"}])
def test_registeret_nede_organisasjonsnummeret_beholdes_og_paameldingen_merkes(con, skrevet):
    kid = _kurs(con)
    r = _klient().post("/kurs/S1", data=_firma(**skrevet))
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "Du er påmeldt!" in html and MELDING in html
    p = _paamelding(con)
    assert (p["betaler"], p["org_nr"], p["status"]) == ("organisasjon", NEDE, "bekreftet")
    # ALDRI det deltakeren skrev: bare registeret er kilde til firmanavn og adresse
    assert (p["org_navn"], p["faktura_adresse"], p["faktura_postnr"], p["faktura_sted"]) == (None, None, None, None)
    assert firmaopplysninger.mangler(p)
    logg = [json.loads(h["detaljer"]) for h in _hendelser(con, "enhetsoppslag_utilgjengelig")]
    assert logg == [{"kurs_id": kid, "paamelding_id": p["id"]}]
    assert NEDE not in json.dumps(_hendelser(con))                 # ingen organisasjonsnummer i loggen


def test_registeret_nede_paa_venteliste_gir_ogsaa_meldingen(con):
    _kurs(con, kapasitet=1)
    assert _klient().post("/kurs/S1", data={**BASIS, "epost": "forst@eksempel.no"}).status_code == 200
    html = _klient().post("/kurs/S1", data=_firma()).get_data(as_text=True)
    assert "Du står på venteliste" in html and MELDING in html
    assert _paamelding(con)["status"] == "venteliste" and firmaopplysninger.mangler(_paamelding(con))


def test_registeret_svarer_ingen_melding_og_registerets_opplysninger_lagres(con):
    _kurs(con)
    html = _klient().post("/kurs/S1", data=_firma(org_nr=FIRMA, org_navn="MANIP AS", faktura_adresse="Manipveien 1",
                                                  faktura_postnr="9999", faktura_sted="Manipby")).get_data(as_text=True)
    assert "Du er påmeldt!" in html and MELDING not in html
    p = _paamelding(con)
    assert (p["org_navn"], p["faktura_adresse"], p["faktura_postnr"], p["faktura_sted"]) == (
        "EKSEMPEL KOMMUNE", "Postboks 100", "1234", "EKSEMPELBY")
    assert not firmaopplysninger.mangler(p) and _hendelser(con, "enhetsoppslag_utilgjengelig") == []


def test_ny_paamelding_etter_avmelding_beholder_ikke_gamle_firmaopplysninger(con):
    _kurs(con)
    assert _klient().post("/kurs/S1", data=_firma(org_nr=FIRMA)).status_code == 200
    con.execute("UPDATE paamelding SET status='avmeldt'")
    con.commit()
    assert _klient().post("/kurs/S1", data=_firma()).status_code == 200
    p = _paamelding(con)
    assert p["status"] == "bekreftet" and (p["org_nr"], p["org_navn"], p["faktura_adresse"], p["faktura_postnr"],
                                            p["faktura_sted"]) == (NEDE, None, None, None, None)


def test_skjemaets_firmafelt_er_skrivebeskyttet_og_javascriptet_gjor_dem_aldri_redigerbare(con):
    import re
    from kurs.web import app as webapp
    _kurs(con)
    html = _klient().get("/kurs/S1").get_data(as_text=True)
    for navn in ("org_navn", "org_adresse", "org_postnr", "org_sted"):
        tag = re.search(rf'<input[^>]*name="{navn}"[^>]*>', html).group(0)
        assert " readonly" in tag
    js = (Path(webapp.app.static_folder) / "app.js").read_text(encoding="utf-8")
    blokk = js.split("// Organisasjonsnummer:", 1)[1].split("// «Kopier lenke»", 1)[0]
    assert "readOnly" not in blokk and "skrivSelv" not in blokk and "manuelt" not in blokk
    assert "Skriv firmanavn og adresse selv" not in js
    assert "Firmaopplysningene kunne ikke hentes akkurat nå" in blokk       # meldingen deltakeren får i skjemaet


def test_bekreftelsen_sier_firmaet_naar_firmanavnet_mangler(con):
    kid = _kurs(con)
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    dager = db.kursdager(con, kid)
    for plan in (PLAN_NA, PLAN_PER_SAMLING):                       # begge fakturateksten: samlet og delt opp
        for navn, forventet in ((None, "til firmaet"), ("Eksempel AS", "til Eksempel AS")):
            p = {"navn": "Ola Nordmann", "fornavn": "Ola", "betaling": "samlet", "betaler": "organisasjon", "org_navn": navn}
            _, html = epost.render("bekreftelse", p=p, kurs=kurs, dager=dager, faktura_plan=FakturaPlan(plan))
            tekst = " ".join(html.split())
            assert forventet in tekst and "None" not in tekst, (plan, navn)


# ============================ fakturaen venter ============================

def test_faktura_venter_paa_firmanavnet_og_lages_naar_det_er_hentet(con):
    _kurs(con)
    assert _klient().post("/kurs/S1", data=_firma()).status_code == 200
    p = _paamelding(con)
    assert (_fakturaer(con), _forsok(con), p["sveiper_kjort"]) == (0, 0, 0)         # ingen faktura uten kundenavn
    tekst = _mailtekst()
    assert "sendes separat til firmaet" in " ".join(tekst.split()) and "None" not in tekst
    sveiper.kjor(Kjoring(con, idag=date.today()))
    assert (_fakturaer(con), _forsok(con)) == (0, 0)                                # fortsatt ingen, og ingen forsøk
    assert firmaopplysninger.prov_igjen(con, p["id"], "test") == firmaopplysninger.HENTET
    con.commit()
    sveiper.kjor(Kjoring(con, idag=date.today()))
    assert _fakturaer(con) == 1 and _paamelding(con)["sveiper_kjort"] == 1
    sveiper.kjor(Kjoring(con, idag=date.today()))
    assert _fakturaer(con) == 1                                                     # aldri to


def test_delfakturaer_venter_ogsaa(con):
    _kurs(con, frem=5, betaling="per_samling")                    # begge kursdagene har forfalt (14 dager før)
    assert _klient().post("/kurs/S1", data=_firma()).status_code == 200
    assert _fakturaer(con) == 0
    assert firmaopplysninger.prov_igjen(con, _paamelding(con)["id"], "test") == firmaopplysninger.HENTET
    con.commit()
    sveiper.kjor(Kjoring(con, idag=date.today()))
    assert _fakturaer(con) == 2


def test_utsatt_faktura_sperres_ogsaa(con):
    _kurs(con, frem=240)                                          # samlet faktura utsettes til seks måneder før
    assert _klient().post("/kurs/S1", data=_firma()).status_code == 200
    p = _paamelding(con)
    tidligst = date.fromisoformat(p["faktura_tidligst_dato"])
    sveiper.utsatte_fakturaer(Kjoring(con, idag=tidligst))
    assert _fakturaer(con) == 0
    assert firmaopplysninger.prov_igjen(con, p["id"], "test") == firmaopplysninger.HENTET
    con.commit()
    sveiper.utsatte_fakturaer(Kjoring(con, idag=tidligst))
    assert _fakturaer(con) == 1


def test_toerrkjoring_viser_at_fakturaen_venter(con):
    kid = _kurs(con)
    _flagget(con, kid)
    p = con.execute(sveiper.SQL_DELTAKER).fetchone()
    k = Kjoring(con, idag=date.today(), tor=True)
    sveiper._opprett(k, p, None, 4500, "Eksempelkurs – Test Person")
    assert any("faktura venter" in linje for linje in k.utskrift) and not any("[TØRR] faktura" in x for x in k.utskrift)
    assert (_fakturaer(con), _forsok(con)) == (0, 0)


# ============================ nytt oppslag ============================

@pytest.mark.parametrize("betaler,navn,nr,forventet", [
    ("person", None, None, False), ("person", None, "123", False),
    ("organisasjon", "Firma AS", FIRMA, False), ("organisasjon", "Firma AS", " 999 900 003 ", False),
    ("organisasjon", None, FIRMA, True), ("organisasjon", "", FIRMA, True), ("organisasjon", "   ", FIRMA, True),
    ("organisasjon", "Firma AS", None, True), ("organisasjon", "Firma AS", "", True),
    ("organisasjon", "Firma AS", "12345", True), ("organisasjon", "Firma AS", "999900004", True)])   # ugyldig nummer
def test_mangler(betaler, navn, nr, forventet):
    assert firmaopplysninger.mangler({"betaler": betaler, "org_navn": navn, "org_nr": nr}) is forventet


def test_prov_igjen_henter_navnet_og_fyller_bare_tom_adresse(con):
    kid = _kurs(con)
    pid = _flagget(con, kid, faktura_adresse="Egen adresse 1", faktura_postnr=" ")     # en administrator har skrevet adressen
    assert firmaopplysninger.prov_igjen(con, pid, "admin:test") == firmaopplysninger.HENTET
    p = _paamelding(con, pid)
    assert (p["org_navn"], p["faktura_adresse"], p["faktura_postnr"], p["faktura_sted"]) == (
        "OPPE IGJEN DEMO AS", "Egen adresse 1", "1234", "EKSEMPELBY")               # adressen erstattes ikke
    assert not firmaopplysninger.mangler(p)
    logg = [(h["detaljer"], ) for h in _hendelser(con, "firmaopplysninger_hentet")]
    assert logg == [(json.dumps({"paamelding_id": pid}),)]
    assert con.execute("SELECT aktor FROM hendelse WHERE handling='firmaopplysninger_hentet'").fetchone()[0] == "admin:test"
    assert firmaopplysninger.prov_igjen(con, pid, "admin:test") == firmaopplysninger.IKKE_AKTUELL
    assert len(_hendelser(con, "firmaopplysninger_hentet")) == 1                      # ingen ny hendelse


def test_prov_igjen_utfall(con, monkeypatch):
    kid = _kurs(con)
    pid = _flagget(con, kid)
    monkeypatch.setattr(brreg, "hent_paa_nytt", lambda nr: (_ for _ in ()).throw(brreg.Utilgjengelig()))
    assert firmaopplysninger.prov_igjen(con, pid, "t") == firmaopplysninger.UTILGJENGELIG
    monkeypatch.setattr(brreg, "hent_paa_nytt", lambda nr: None)
    assert firmaopplysninger.prov_igjen(con, pid, "t") == firmaopplysninger.IKKE_FUNNET
    for nr in (None, "", "12345", "999900004"):                                      # mangler, kort, feil kontrollsiffer
        con.execute("UPDATE paamelding SET org_nr=? WHERE id=?", (nr, pid))
        assert firmaopplysninger.prov_igjen(con, pid, "t") == firmaopplysninger.UGYLDIG_NUMMER
    privat, _ = db.meld_paa(con, kid, epost="p@eksempel.no", fornavn="P", etternavn="P")
    assert firmaopplysninger.prov_igjen(con, privat, "t") == firmaopplysninger.IKKE_AKTUELL
    assert firmaopplysninger.prov_igjen(con, 99999, "t") == firmaopplysninger.IKKE_AKTUELL
    assert _paamelding(con, pid)["org_navn"] is None and _hendelser(con, "firmaopplysninger_hentet") == []


def test_prov_igjen_overskriver_ikke_et_navn_som_kom_samtidig(con, monkeypatch):
    kid = _kurs(con)
    pid = _flagget(con, kid)

    def samtidig(nr):                                             # en administrator rakk å skrive navnet under oppslaget
        con.execute("UPDATE paamelding SET org_navn='Skrevet av admin AS' WHERE id=?", (pid,))
        return brreg.DEMO_OPPE_IGJEN
    monkeypatch.setattr(brreg, "hent_paa_nytt", samtidig)
    assert firmaopplysninger.prov_igjen(con, pid, "t") == firmaopplysninger.IKKE_AKTUELL
    assert _paamelding(con, pid)["org_navn"] == "Skrevet av admin AS" and _hendelser(con, "firmaopplysninger_hentet") == []


def test_prov_alle_stopper_naar_registeret_ikke_svarer(con, monkeypatch):
    kid = _kurs(con)
    ider = [_flagget(con, kid, f"p{i}@eksempel.no") for i in range(3)]
    kall = []

    def svar(nr):
        kall.append(nr)
        if len(kall) == 2:
            raise brreg.Utilgjengelig()
        return brreg.DEMO_OPPE_IGJEN
    monkeypatch.setattr(brreg, "hent_paa_nytt", svar)
    u = firmaopplysninger.prov_alle(con, "t")
    assert (u[firmaopplysninger.HENTET], u[firmaopplysninger.UTILGJENGELIG], len(kall)) == (1, 2, 2)    # den siste ikke prøvd
    assert [firmaopplysninger.mangler(_paamelding(con, i)) for i in ider] == [False, True, True]


def test_listen_har_bare_aktive_paameldinger_paa_kurs_som_ikke_er_avlyst(con):
    k1, k2, k3 = _kurs(con, "S1"), _kurs(con, "S2"), _kurs(con, "S3")
    a, b, c, d = (_flagget(con, k1, "a@eksempel.no"), _flagget(con, k1, "b@eksempel.no"),
                  _flagget(con, k2, "c@eksempel.no"), _flagget(con, k3, "d@eksempel.no"))
    db.meld_paa(con, k1, epost="privat@eksempel.no", fornavn="P", etternavn="P")
    db.meld_paa(con, k1, epost="navn@eksempel.no", fornavn="N", etternavn="N",
                paamelding={"betaler": "organisasjon", "org_nr": FIRMA, "org_navn": "Firma AS"})
    con.execute("UPDATE paamelding SET status='avmeldt' WHERE id=?", (b,))
    con.execute("UPDATE paamelding SET status='venteliste' WHERE id=?", (c,))
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (k3,))
    con.commit()
    assert [r["id"] for r in firmaopplysninger.liste(con)] == [a, c]
    assert [r["id"] for r in firmaopplysninger.liste(con, k1)] == [a]
    rad = firmaopplysninger.liste(con)[0]
    assert (rad["kurs_id"], rad["kursnavn"], rad["org_nr"], rad["navn"]) == (k1, "Eksempelkurs", NEDE, "Test Person")
    assert d not in [r["id"] for r in firmaopplysninger.liste(con)]


def test_bare_mellomrom_i_firmanavnet_teller_som_manglende(con):
    kid = _kurs(con)
    pid = _flagget(con, kid, org_navn="   ")
    assert [r["id"] for r in firmaopplysninger.liste(con)] == [pid]
    assert firmaopplysninger.prov_igjen(con, pid, "t") == firmaopplysninger.HENTET
    assert _paamelding(con, pid)["org_navn"] == "OPPE IGJEN DEMO AS"


# ============================ administrator ============================

def test_deltakervinduet_merker_paamelding_uten_firmaopplysninger(con, admin):
    kid = _kurs(con)
    pid = _flagget(con, kid)
    html = admin.get(_vindu(kid, pid)).get_data(as_text=True)
    assert MERKE in html and "<button>Hent firmaopplysninger på nytt</button>" in html
    assert f"{_vindu(kid, pid)}/firmaopplysninger" in html and NEDE in html
    assert "Fakturaen opprettes ikke før firmaopplysningene er på plass" in html


def test_deltakervinduet_har_ikke_merket_for_andre(con, admin):
    kid = _kurs(con)
    privat, _ = db.meld_paa(con, kid, epost="p@eksempel.no", fornavn="P", etternavn="P")
    med_navn, _ = db.meld_paa(con, kid, epost="n@eksempel.no", fornavn="N", etternavn="N",
                              paamelding={"betaler": "organisasjon", "org_nr": FIRMA, "org_navn": "Firma AS"})
    avmeldt = _flagget(con, kid, "av@eksempel.no")
    con.execute("UPDATE paamelding SET status='avmeldt' WHERE id=?", (avmeldt,))
    con.commit()
    for pid in (privat, med_navn, avmeldt):
        assert MERKE not in admin.get(_vindu(kid, pid)).get_data(as_text=True), pid


def test_adressen_sier_ingen_adresse_lagret_naar_navnet_er_der_men_ikke_naar_opplysningene_mangler(con, admin):
    """Eldre påmeldinger (fra før den felles regelen) kan ha firmanavn uten adresse: de er ikke merket, og adressen står ikke
    som «Ikke hentet ennå» (det ville sagt at noe mangler, uten at det finnes noe å trykke på). Merkede påmeldinger sier det."""
    kid = _kurs(con)
    med_navn, _ = db.meld_paa(con, kid, epost="n@eksempel.no", fornavn="N", etternavn="N",
                              paamelding={"betaler": "organisasjon", "org_nr": FIRMA, "org_navn": "Firma AS"})
    con.commit()
    html = admin.get(_vindu(kid, med_navn)).get_data(as_text=True)
    assert MERKE not in html and "Ingen adresse lagret" in html and "Ikke hentet ennå" not in html
    html = admin.get(_vindu(kid, _flagget(con, kid, "f@eksempel.no"))).get_data(as_text=True)
    assert MERKE in html and "Ikke hentet ennå" in html and "Ingen adresse lagret" not in html


def test_lesetilgang_ser_merket_men_ikke_knappene_og_kan_ikke_proeve_paa_nytt(con):
    kid = _kurs(con)
    pid = _flagget(con, kid)
    db.opprett_admin_bruker(con, "leser", "Leser", "passord-som-holder", rolle="lese")
    con.commit()
    leser = _admin("leser", "passord-som-holder")
    html = leser.get(_vindu(kid, pid)).get_data(as_text=True)
    assert MERKE in html and "<button>Hent firmaopplysninger på nytt</button>" not in html
    assert leser.post(f"{_vindu(kid, pid)}/firmaopplysninger").status_code == 403
    assert leser.post("/admin/firmaopplysninger").status_code == 403
    assert firmaopplysninger.mangler(_paamelding(con, pid))
    assert "<button>Hent manglende firmaopplysninger</button>" not in leser.get("/admin").get_data(as_text=True)
    assert "<button>Hent manglende firmaopplysninger</button>" not in leser.get(
        f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)


def test_admin_kan_proeve_oppslaget_paa_nytt(con, admin):
    kid = _kurs(con)
    pid = _flagget(con, kid)
    r = admin.post(f"{_vindu(kid, pid)}/firmaopplysninger", follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "Firmaopplysningene er hentet fra Enhetsregisteret" in html and MERKE not in html
    p = _paamelding(con, pid)
    assert (p["org_navn"], p["faktura_adresse"], p["faktura_postnr"], p["faktura_sted"]) == (
        "OPPE IGJEN DEMO AS", "Retteveien 2", "1234", "EKSEMPELBY")
    assert "Firmaopplysningene er allerede på plass" in admin.post(
        f"{_vindu(kid, pid)}/firmaopplysninger", follow_redirects=True).get_data(as_text=True)


@pytest.mark.parametrize("oppslag,melding", [
    (lambda nr: (_ for _ in ()).throw(brreg.Utilgjengelig()), "Enhetsregisteret svarer ikke akkurat nå"),
    (lambda nr: None, "Fant ikke organisasjonsnummeret i Enhetsregisteret"),
])
def test_oppslaget_feiler_merket_staar_og_admin_faar_beskjed(con, admin, monkeypatch, oppslag, melding):
    kid = _kurs(con)
    pid = _flagget(con, kid)
    monkeypatch.setattr(brreg, "hent_paa_nytt", oppslag)
    html = admin.post(f"{_vindu(kid, pid)}/firmaopplysninger", follow_redirects=True).get_data(as_text=True)
    assert melding in html and MERKE in html and _paamelding(con, pid)["org_navn"] is None
    assert _hendelser(con, "firmaopplysninger_hentet") == []


def test_ugyldig_organisasjonsnummer_gir_beskjed_om_aa_rette_det(con, admin):
    kid = _kurs(con)
    pid = _flagget(con, kid, org_nr=None)
    html = admin.post(f"{_vindu(kid, pid)}/firmaopplysninger", follow_redirects=True).get_data(as_text=True)
    assert "Organisasjonsnummeret mangler eller er ugyldig" in html and MERKE in html


def test_nytt_oppslag_gjelder_bare_paameldinger_paa_kurset(con, admin):
    k1, k2 = _kurs(con, "S1"), _kurs(con, "S2")
    pid = _flagget(con, k1)
    assert admin.post(f"{_vindu(k2, pid)}/firmaopplysninger").status_code == 404
    assert admin.post(f"{_vindu(9999, pid)}/firmaopplysninger").status_code == 404
    assert admin.get(f"{_vindu(k1, pid)}/firmaopplysninger").status_code == 405
    assert firmaopplysninger.mangler(_paamelding(con, pid))


def test_samlet_oppslag_for_ett_kurs_eller_alle(con, admin):
    k1, k2 = _kurs(con, "S1"), _kurs(con, "S2")
    p1, p2, p3 = (_flagget(con, k1, "a@eksempel.no"), _flagget(con, k1, "b@eksempel.no"),
                  _flagget(con, k2, "c@eksempel.no"))
    r = admin.post("/admin/firmaopplysninger", data={"kurs_id": k1})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/admin/kurs/{k1}/deltakere")
    assert [firmaopplysninger.mangler(_paamelding(con, i)) for i in (p1, p2, p3)] == [False, False, True]
    r = admin.post("/admin/firmaopplysninger")
    assert r.status_code == 302 and r.headers["Location"].endswith("/admin")
    assert not firmaopplysninger.mangler(_paamelding(con, p3))
    html = admin.post("/admin/firmaopplysninger", follow_redirects=True).get_data(as_text=True)
    assert "Ingen påmeldinger mangler firmaopplysninger." in html


def test_samlet_oppslag_melding_og_feil(con, admin, monkeypatch):
    kid = _kurs(con)
    _flagget(con, kid, "a@eksempel.no"), _flagget(con, kid, "b@eksempel.no")
    monkeypatch.setattr(brreg, "hent_paa_nytt", lambda nr: (_ for _ in ()).throw(brreg.Utilgjengelig()))
    html = admin.post("/admin/firmaopplysninger", data={"kurs_id": kid}, follow_redirects=True).get_data(as_text=True)
    assert "Firmaopplysninger hentet for 0 av 2 påmeldinger." in html and "svarer ikke akkurat nå" in html
    monkeypatch.setattr(brreg, "hent_paa_nytt", lambda nr: None)
    html = admin.post("/admin/firmaopplysninger", follow_redirects=True).get_data(as_text=True)
    assert "2 organisasjonsnumre ble ikke funnet i Enhetsregisteret" in html
    assert admin.post("/admin/firmaopplysninger", data={"kurs_id": "abc"}).status_code == 400
    assert admin.post("/admin/firmaopplysninger", data={"kurs_id": "9999"}).status_code == 404


def test_deltakerlisten_merker_og_har_samlet_knapp(con, admin):
    kid, tomt = _kurs(con, "S1"), _kurs(con, "S2")
    _flagget(con, kid)
    avmeldt = _flagget(con, kid, "av@eksempel.no")                  # en avmeldt påmelding trenger ingen kontroll
    con.execute("UPDATE paamelding SET status='avmeldt' WHERE id=?", (avmeldt,))
    con.commit()
    html = admin.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    assert html.count(MERKE) == 2                                  # merket ved navnet og boksen over listen
    assert "1 påmelding på dette kurset har ikke fått firmanavn" in html
    assert f'name="kurs_id" value="{kid}"' in html and "<button>Hent manglende firmaopplysninger</button>" in html
    assert MERKE not in admin.get(f"/admin/kurs/{tomt}/deltakere").get_data(as_text=True)


def test_oversikten_teller_og_lister_paameldingene(con, admin):
    kid = _kurs(con)
    pid = _flagget(con, kid)
    html = admin.get("/admin").get_data(as_text=True)
    assert f'id="firmakontroll"' in html and f"/admin/kurs/{kid}/deltaker/{pid}" in html and NEDE in html
    assert "<button>Hent manglende firmaopplysninger</button>" in html
    assert admin.post("/admin/firmaopplysninger").status_code == 302
    html = admin.get("/admin").get_data(as_text=True)
    assert 'id="firmakontroll"' not in html and "Hent manglende firmaopplysninger" not in html


def test_adminskjemaet_kan_lagres_uten_firmanavn_naar_paameldingen_venter_paa_det(con, admin):
    kid = _kurs(con)
    pid = _flagget(con, kid)
    data = {"betaler": "organisasjon", "org_nr": NEDE, "org_navn": "", "intern_kommentar": "Ring firmaet"}
    assert admin.post(f"{_vindu(kid, pid)}/paamelding", data=data).status_code == 302
    p = _paamelding(con, pid)
    assert (p["intern_kommentar"], p["org_navn"]) == ("Ring firmaet", None) and firmaopplysninger.mangler(p)
    # Felles regel: ikke heller administrator kan skrive firmanavn eller adresse - det ignoreres, og merket står
    data |= {"org_navn": "Skrevet av admin AS", "faktura_adresse": "Egen gate 1", "faktura_postnr": "9999",
             "faktura_sted": "Egenby"}
    assert admin.post(f"{_vindu(kid, pid)}/paamelding", data=data).status_code == 302
    p = _paamelding(con, pid)
    assert _firmafelter(p) == (None, None, None, None) and firmaopplysninger.mangler(p)
    # Endres nummeret, hentes firmaopplysningene fra registeret (samme regel som ved påmelding)
    data |= {"org_nr": FIRMA}
    assert admin.post(f"{_vindu(kid, pid)}/paamelding", data=data).status_code == 302
    p = _paamelding(con, pid)
    assert (p["org_nr"], _firmafelter(p)) == (FIRMA, ("EKSEMPEL KOMMUNE", "Postboks 100", "1234", "EKSEMPELBY"))
    assert not firmaopplysninger.mangler(p)


def _firmafelter(p):
    return (p["org_navn"], p["faktura_adresse"], p["faktura_postnr"], p["faktura_sted"])


def test_adminskjemaet_krever_organisasjonsnummer_og_avviser_ugyldig_og_ukjent(con, admin):
    kid = _kurs(con)
    flagget = _flagget(con, kid)
    med_navn, _ = db.meld_paa(con, kid, epost="n@eksempel.no", fornavn="N", etternavn="N",
                              paamelding={"betaler": "organisasjon", "org_nr": FIRMA, "org_navn": "Firma AS"})
    privat, _ = db.meld_paa(con, kid, epost="p@eksempel.no", fornavn="P", etternavn="P")
    con.commit()

    def lagre(pid, **data):
        return admin.post(f"{_vindu(kid, pid)}/paamelding", data={"betaler": "organisasjon", "intern_kommentar": "X", **data},
                          follow_redirects=True).get_data(as_text=True)
    assert "Fyll inn organisasjonsnummer når firma betaler." in lagre(flagget, org_nr="")           # kreves alltid
    assert firmaopplysninger.TEKST_UGYLDIG in lagre(flagget, org_nr="123")
    assert "Fant ikke organisasjonsnummeret" in lagre(flagget, org_nr="999900070")
    assert _paamelding(con, flagget)["intern_kommentar"] is None and _paamelding(con, flagget)["org_nr"] == NEDE
    # samme firma som før: navnet står som det er (fjernes ikke, og skrives ikke om)
    assert "Påmeldingen er oppdatert." in lagre(med_navn, org_nr=FIRMA, org_navn="")
    assert _paamelding(con, med_navn)["org_navn"] == "Firma AS"
    # betaler selv -> firma: firmaopplysningene hentes fra registeret; nummeret avvises hvis det ikke finnes
    assert "Fant ikke organisasjonsnummeret" in lagre(privat, org_nr="999900070")
    assert _paamelding(con, privat)["betaler"] == "person"
    assert "Påmeldingen er oppdatert." in lagre(privat, org_nr=FIRMA, org_navn="MANIP AS", faktura_adresse="Manipveien 1")
    p = _paamelding(con, privat)
    assert (p["betaler"], p["org_nr"], _firmafelter(p)) == (
        "organisasjon", FIRMA, ("EKSEMPEL KOMMUNE", "Postboks 100", "1234", "EKSEMPELBY"))
    # ... og svarer ikke registeret: lagres, merket for kontroll, og administrator får beskjed
    ny, _ = db.meld_paa(con, kid, epost="r@eksempel.no", fornavn="R", etternavn="R")
    con.commit()
    html = lagre(ny, org_nr=NEDE)
    assert firmaopplysninger.TEKST_IKKE_HENTET in html and firmaopplysninger.mangler(_paamelding(con, ny))


def test_loggen_forteller_hva_som_skjedde(con):
    _kurs(con)
    assert _klient().post("/kurs/S1", data=_firma()).status_code == 200
    p = _paamelding(con)

    def tekster():
        return [rad.hva for rad in hendelseslogg.for_paamelding(con, _paamelding(con, p["id"]), systemadmin=False).rader]
    assert "Firmaopplysningene kunne ikke hentes fra Enhetsregisteret ved påmeldingen" in tekster()
    assert firmaopplysninger.prov_igjen(con, p["id"], "admin:test") == firmaopplysninger.HENTET
    con.commit()
    assert "Firmaopplysninger hentet fra Enhetsregisteret" in tekster()


# ============================ morgenjobben ============================

def _antall_mail():
    return len(list(config.UTBOKS.glob("*.html"))) if config.UTBOKS.exists() else 0


def test_morgenjobben_henter_firmaopplysningene_foer_fakturaene_og_er_idempotent(con):
    _kurs(con)
    assert _klient().post("/kurs/S1", data=_firma()).status_code == 200
    assert _fakturaer(con) == 0
    idag = date.today()
    daglig.kjor(Kjoring(con, idag=idag))
    p = _paamelding(con)
    assert p["org_navn"] == "OPPE IGJEN DEMO AS" and _fakturaer(con) == 1          # opplysningene først, så fakturaen - samme dag

    def tilstand():
        return (len(_hendelser(con, "firmaopplysninger_hentet")), _fakturaer(con), _forsok(con), _antall_mail(),
                con.execute("SELECT COUNT(*) FROM utsending_logg").fetchone()[0])
    foer = tilstand()
    assert foer[0] == 1
    daglig.kjor(Kjoring(con, idag=idag))
    assert tilstand() == foer                                     # andre kjøring samme dag: ingenting skjer


def test_toerrkjoring_gjor_ingen_oppslag_og_skriver_ingenting(con, monkeypatch):
    _kurs(con)
    assert _klient().post("/kurs/S1", data=_firma()).status_code == 200
    monkeypatch.setattr(brreg, "hent_paa_nytt", lambda nr: pytest.fail("oppslag i tørrkjøring"))
    k = Kjoring(con, idag=date.today(), tor=True)
    daglig.kjor(k)
    assert any("[TØRR] ville forsøkt å hente firmaopplysninger for 1 påmelding(er)" in x for x in k.utskrift)
    assert firmaopplysninger.mangler(_paamelding(con)) and _hendelser(con, "firmaopplysninger_hentet") == []
    assert _fakturaer(con) == 0


def test_morgenjobben_proever_igjen_naar_registeret_ikke_svarer(con, monkeypatch):
    _kurs(con)
    assert _klient().post("/kurs/S1", data=_firma()).status_code == 200
    ekte = brreg.hent_paa_nytt
    monkeypatch.setattr(brreg, "hent_paa_nytt", lambda nr: (_ for _ in ()).throw(brreg.Utilgjengelig()))
    k = Kjoring(con, idag=date.today())
    daglig.kjor(k)
    assert not k.feil and any("registeret svarer ikke, prøver igjen neste kjøring" in x for x in k.utskrift)
    p = _paamelding(con)
    assert (p["org_navn"], p["sveiper_kjort"], _fakturaer(con), _forsok(con)) == (None, 0, 0, 0)
    assert _hendelser(con, "firmaopplysninger_hentet") == [] and _hendelser(con, "daglig_feil") == []
    monkeypatch.setattr(brreg, "hent_paa_nytt", ekte)                                # registeret er tilbake
    daglig.kjor(Kjoring(con, idag=date.today() + timedelta(days=1)))
    p = _paamelding(con)
    assert (p["org_navn"], p["sveiper_kjort"], _fakturaer(con)) == ("OPPE IGJEN DEMO AS", 1, 1)


def test_morgenjobben_uten_paameldinger_som_mangler_gjor_ingenting_og_sier_ingenting(con):
    _kurs(con)
    assert _klient().post("/kurs/S1", data=_firma(org_nr=FIRMA)).status_code == 200
    k = Kjoring(con, idag=date.today())
    daglig.kjor(k)
    assert not any("firmaopplysninger" in x.lower() and "hentet" in x for x in k.utskrift)
    assert _hendelser(con, "firmaopplysninger_hentet") == []
