"""Samtidighet og feilhåndtering:
  * sendemotoren: en feil hos én mottaker stopper ikke de andre, og etter STOPP_ETTER feil på rad utsettes resten
    (ingenting reserveres - tas neste gang)
  * manuelle utsendelser og avlysning bruker claim-motoren: aldri to ganger til samme person
  * morgenjobben: hvert steg og hvert kurs isoleres; personvern-slettingen kjører selv om e-post feiler
  * uavklarte operasjoner kan avklares av admin (e-post og faktura)
  * optimistisk samtidighetskontroll: lagring over noen andres endring avvises i stedet for å overskrive i det stille
"""
import json
from datetime import date, timedelta

import pytest

from kurs import config, daglig, db, kjoring, kursbevis
from kurs.integrasjoner import epost
from kurs.kjoring import Kjoring
from navnehjelp import navnedeler

IDAG = date(2031, 3, 3)                 # mandag; kurs som starter 10.03.2031 er i «uka før»-vinduet


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


@pytest.fixture
def post(monkeypatch):
    """Etterlignet e-posttjeneste: `nede` er et sett med mottakere (eller «alle») som gir feil."""
    tilstand = {"sendt": [], "nede": set()}

    def send(til, emne, html, *a, **kw):
        if "alle" in tilstand["nede"] or til in tilstand["nede"]:
            raise RuntimeError("Graph svarte ikke")
        tilstand["sendt"].append((til, emne))
    monkeypatch.setattr(epost, "send", send)
    return tilstand


def _kurs(con, kode, start=date(2031, 3, 10), **kw):
    kid = db.opprett_kurs(con, kode=kode, navn=f"Kurs {kode}", datoer=[start.isoformat()], sharepoint_mappe=f"K/{kode}",
                          **{"type": "fysisk", "sted": "Oslo", "pris_nok": 0, **kw})
    con.commit()
    return kid


def _deltaker(con, kid, epost_, **kw):
    pid, _ = db.meld_paa(con, kid, epost=epost_, **navnedeler(epost_.split("@")[0].title()), **kw)
    con.execute("UPDATE paamelding SET sveiper_kjort=1 WHERE id=?", (pid,))
    con.commit()
    return pid


def _status(con, mottaker, type_):
    rad = con.execute("SELECT status FROM utsending_logg WHERE mottaker=? AND type=?", (mottaker, type_)).fetchone()
    return rad[0] if rad else None


def _admin(brukernavn=None, passord=None):
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    r = k.post("/admin/logg-inn", data={"brukernavn": brukernavn or config.ADMIN_BRUKERNAVN,
                                        "passord": passord or config.ADMIN_PASSORD})
    assert r.status_code == 302
    return k


# ============================ sendemotoren ============================

def _mottakere(con, antall):
    kid = _kurs(con, "M1", kapasitet=50)
    return [{"id": _deltaker(con, kid, f"d{i}@eksempel.no"), "epost": f"d{i}@eksempel.no"} for i in range(antall)]


def test_en_feil_stopper_ikke_de_andre_mottakerne(con, post):
    mottakere = _mottakere(con, 4)
    post["nede"] = {"d1@eksempel.no"}
    ut = Kjoring(con).send_til_mange("adhoc:1", "admin_epost", mottakere, lambda m: ("Emne", "<p>x</p>"))
    assert len(ut.sendt) == 3 and ut.uavklart == 1 and ut.ikke_forsokt == 0
    assert _status(con, "d1@eksempel.no", "admin_epost") == "ukjent"


def test_etter_tre_feil_paa_rad_utsettes_resten_uten_reservasjon(con, post):
    mottakere = _mottakere(con, 6)
    post["nede"] = {"alle"}
    k = Kjoring(con)
    ut = k.send_til_mange("adhoc:1", "admin_epost", mottakere, lambda m: ("Emne", "<p>x</p>"))
    assert (ut.uavklart, ut.ikke_forsokt, len(ut.sendt)) == (kjoring.STOPP_ETTER, 6 - kjoring.STOPP_ETTER, 0)
    assert con.execute("SELECT COUNT(*) FROM utsending_logg").fetchone()[0] == kjoring.STOPP_ETTER
    with pytest.raises(kjoring.SendingStanset):
        k.send_ferdigrendret_en_gang("adhoc:1", "ny@eksempel.no", "admin_epost", "E", "<p>x</p>")
    post["nede"] = set()                                                   # tjenesten er tilbake - ny kjøring
    ut2 = Kjoring(con).send_til_mange("adhoc:1", "admin_epost", mottakere, lambda m: ("Emne", "<p>x</p>"))
    assert len(ut2.sendt) == 6 - kjoring.STOPP_ETTER and ut2.uavklart_fra_for == kjoring.STOPP_ETTER   # røres ikke
    assert ut2.uavklart == 0


def test_vellykket_sending_nullstiller_feiltelleren(con, post):
    mottakere = _mottakere(con, 5)
    post["nede"] = {"d0@eksempel.no", "d1@eksempel.no", "d3@eksempel.no", "d4@eksempel.no"}
    ut = Kjoring(con).send_til_mange("adhoc:1", "admin_epost", mottakere, lambda m: ("Emne", "<p>x</p>"))
    assert ut.uavklart == 4 and len(ut.sendt) == 1 and ut.ikke_forsokt == 0     # d2 brøt rekken


# ============================ manuell utsendelse (admin) ============================

def _utsendelse(con, kid, pider):
    uid, _ = db.opprett_admin_utsending(con, kid, "Beskjed", "Hei", pider, None)
    con.commit()
    return uid


def test_manuell_utsendelse_fortsetter_etter_feil_og_sender_aldri_dobbelt(con, post):
    kid = _kurs(con, "A1", kapasitet=10)
    pider = [_deltaker(con, kid, f"a{i}@eksempel.no") for i in range(3)]
    uid = _utsendelse(con, kid, pider)
    post["nede"] = {"a1@eksempel.no"}
    k = _admin()
    r = k.post(f"/admin/kurs/{kid}/epost/send", data={"utsending_id": uid})
    side = k.get(r.headers["Location"]).get_data(as_text=True)
    assert "E-post sendt til 2 mottaker(e)." in side and "1 e-post(er) fikk et uavklart utfall" in side
    post["nede"] = set()
    k.post(f"/admin/kurs/{kid}/epost/send", data={"utsending_id": uid})       # nytt trykk: ingen duplikater
    assert sorted(t for t, _ in post["sendt"]) == ["a0@eksempel.no", "a2@eksempel.no"]


# ============================ avlysning ============================

def test_avlysning_varsler_resten_ved_feil_og_kan_sende_til_de_som_mangler(con, post):
    kid = _kurs(con, "AV", kapasitet=10)
    for i in range(5):
        _deltaker(con, kid, f"v{i}@eksempel.no")
    post["nede"] = {"alle"}
    k = _admin()
    r = k.post(f"/admin/kurs/{kid}/avlys")
    side = k.get(r.headers["Location"]).get_data(as_text=True)
    assert con.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "avlyst"
    assert "0 deltaker(e) er varslet" in side and f"{kjoring.STOPP_ETTER} e-post(er) fikk et uavklart utfall" in side
    assert f"{5 - kjoring.STOPP_ETTER} deltaker(e) har ikke fått avlysningsvarselet" in side
    post["nede"] = set()
    r = k.post(f"/admin/kurs/{kid}/avlys/varsle")
    side = k.get(r.headers["Location"]).get_data(as_text=True)
    assert f"Avlysningsvarsel sendt til {5 - kjoring.STOPP_ETTER} deltaker(e)." in side
    assert "har ikke fått avlysningsvarselet" not in side
    assert len(post["sendt"]) == 5 - kjoring.STOPP_ETTER                      # de uavklarte røres ikke
    r = k.post(f"/admin/kurs/{kid}/avlys/varsle")
    side = k.get(r.headers["Location"]).get_data(as_text=True)
    assert f"{kjoring.STOPP_ETTER} mottaker(e) har et uavklart eller pågående forsøk fra før" in side
    assert len(post["sendt"]) == 5 - kjoring.STOPP_ETTER


def test_alle_varslet_gir_egen_beskjed(con, post):
    kid = _kurs(con, "AX", kapasitet=5)
    _deltaker(con, kid, "x@eksempel.no")
    k = _admin()
    k.post(f"/admin/kurs/{kid}/avlys")
    r = k.post(f"/admin/kurs/{kid}/avlys/varsle")
    assert "Alle har allerede fått avlysningsvarselet." in k.get(r.headers["Location"]).get_data(as_text=True)


def test_varsle_paa_nytt_krever_avlyst_kurs(con, post):
    kid = _kurs(con, "AW")
    r = _admin().post(f"/admin/kurs/{kid}/avlys/varsle")
    assert r.status_code == 302 and post["sendt"] == []


# ============================ morgenjobben ============================

def test_feil_i_ett_kurs_stopper_ikke_andre_kurs_eller_personvern(con, post):
    kid_a, kid_b = _kurs(con, "DA"), _kurs(con, "DB")
    _deltaker(con, kid_a, "a@eksempel.no")
    _deltaker(con, kid_b, "b@eksempel.no")
    gammelt = _kurs(con, "GML", start=IDAG - timedelta(days=40))
    pid_gml = _deltaker(con, gammelt, "g@eksempel.no", sensitivt={"allergier": "nøtter"})
    post["nede"] = {"a@eksempel.no"}
    k = Kjoring(con, idag=IDAG)
    daglig.kjor(k)
    assert _status(con, "a@eksempel.no", "ukefor") == "ukjent" and _status(con, "b@eksempel.no", "ukefor") == "sendt"
    assert con.execute("SELECT COUNT(*) FROM sensitivt WHERE paamelding_id=?", (pid_gml,)).fetchone()[0] == 0
    rad = con.execute("SELECT detaljer FROM hendelse WHERE handling='daglig_feil'").fetchone()
    assert json.loads(rad["detaljer"]) == {"steg": "kurs", "kurs_id": kid_a, "feil": "RuntimeError"}
    assert k.feil == ["kurs"]


def test_main_gir_avslutningskode_1_naar_et_steg_feiler(con, post, monkeypatch):
    kid = _kurs(con, "DC")
    _deltaker(con, kid, "c@eksempel.no")
    post["nede"] = {"alle"}
    assert daglig.main(["--dato", IDAG.isoformat()]) == 1
    post["nede"] = set()
    assert daglig.main(["--dato", (IDAG + timedelta(days=1)).isoformat()]) == 0


def test_mange_feil_i_en_kjoering_gir_hoyst_stopp_etter_uavklarte(con, post):
    for i in range(5):
        _deltaker(con, _kurs(con, f"E{i}"), f"e{i}@eksempel.no")
    post["nede"] = {"alle"}
    daglig.kjor(Kjoring(con, idag=IDAG))
    assert con.execute("SELECT COUNT(*) FROM utsending_logg WHERE status='ukjent'").fetchone()[0] == kjoring.STOPP_ETTER
    post["nede"] = set()
    daglig.kjor(Kjoring(con, idag=IDAG + timedelta(days=1)))                 # de utsatte sendes neste dag
    assert con.execute("SELECT COUNT(*) FROM utsending_logg WHERE status='sendt'").fetchone()[0] == 5 - kjoring.STOPP_ETTER


def test_feilet_steg_ruller_bare_tilbake_sitt_eget_arbeid(con):
    k = Kjoring(con, idag=IDAG)
    kid = _kurs(con, "RB")

    def ok_steg():
        con.execute("UPDATE kurs SET notat='fra steg 1' WHERE id=?", (kid,))

    def feilende_steg():
        con.execute("UPDATE kurs SET sted='Bergen' WHERE id=?", (kid,))
        raise ValueError("feil")
    daglig._steg(k, "en", ok_steg)
    daglig._steg(k, "to", feilende_steg)
    rad = con.execute("SELECT notat, sted FROM kurs WHERE id=?", (kid,)).fetchone()
    assert (rad["notat"], rad["sted"]) == ("fra steg 1", "Oslo") and k.feil == ["to"]


def test_kursbevis_utstedes_ikke_naar_e_posttjenesten_er_nede(con, post, monkeypatch):
    kid = _kurs(con, "KB", start=IDAG - timedelta(days=2))
    pid = _deltaker(con, kid, "k@eksempel.no")
    db.registrer_oppmote(con, pid, db.kursdager(con, kid)[0]["id"], "manuell")
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()
    k = Kjoring(con, idag=IDAG)
    k.sendefeil_paa_rad = kjoring.STOPP_ETTER                                 # tjenesten har feilet på rad
    kursbevis.kjor(k)
    assert con.execute("SELECT COUNT(*) FROM dokument").fetchone()[0] == 0
    kursbevis.kjor(Kjoring(con, idag=IDAG))                                  # neste kjøring: utstedes og varsles
    assert con.execute("SELECT COUNT(*) FROM dokument").fetchone()[0] == 1 and _status(con, "k@eksempel.no", "kursbevis") == "sendt"


# ============================ uavklarte operasjoner ============================

def _uavklart_epost(con, kid, mottaker="u@eksempel.no", type_="ukefor"):
    db.reserver_sending(con, f"kurs:{kid}", mottaker, type_)
    db.sett_sending_ukjent(con, f"kurs:{kid}", mottaker, type_)
    con.commit()


def test_uavklart_e_post_kan_registreres_som_sendt_eller_ikke_sendt(con):
    kid = _kurs(con, "U1")
    _uavklart_epost(con, kid, "u@eksempel.no")
    _uavklart_epost(con, kid, "v@eksempel.no")
    k = _admin()
    side = k.get("/admin/uavklart").get_data(as_text=True)
    assert "u@eksempel.no" in side and "Kurs U1" in side and "E-post (2)" in side
    felles = {"nokkel": f"kurs:{kid}", "type": "ukefor"}
    k.post("/admin/uavklart/epost", data={**felles, "mottaker": "u@eksempel.no", "utfall": "sendt"})
    k.post("/admin/uavklart/epost", data={**felles, "mottaker": "v@eksempel.no", "utfall": "ikke_sendt"})
    assert _status(con, "u@eksempel.no", "ukefor") == "sendt" and _status(con, "v@eksempel.no", "ukefor") == "feilet"
    assert db.reserver_sending_pa_nytt(con, f"kurs:{kid}", "v@eksempel.no", "ukefor")   # «feilet» prøves igjen
    con.rollback()
    r = k.post("/admin/uavklart/epost", data={**felles, "mottaker": "u@eksempel.no", "utfall": "ikke_sendt"})
    assert "ikke lenger uavklart" in k.get(r.headers["Location"]).get_data(as_text=True)
    assert _status(con, "u@eksempel.no", "ukefor") == "sendt"                 # allerede avklart - ikke endret
    logg = [r["detaljer"] for r in con.execute("SELECT detaljer FROM hendelse WHERE handling='uavklart_epost_avklart'")]
    assert len(logg) == 2 and not any("@" in x for x in logg)


def test_ferske_reservasjoner_vises_ikke_og_kan_ikke_avklares(con):
    kid = _kurs(con, "U2")
    db.reserver_sending(con, f"kurs:{kid}", "p@eksempel.no", "ukefor")          # pågår akkurat nå
    con.commit()
    k = _admin()
    assert "p@eksempel.no" not in k.get("/admin/uavklart").get_data(as_text=True)
    k.post("/admin/uavklart/epost", data={"nokkel": f"kurs:{kid}", "type": "ukefor", "mottaker": "p@eksempel.no",
                                          "utfall": "sendt"})
    assert _status(con, "p@eksempel.no", "ukefor") == "reservert"


def _uavklart_faktura(con, pris=3000, ant_dager=1):
    start = date(2031, 6, 2)
    kid = db.opprett_kurs(con, kode="F1", navn="Fakturakurs", sharepoint_mappe="K/F1", pris_nok=pris, kapasitet=5,
                          datoer=[(start + timedelta(days=7 * n)).isoformat() for n in range(ant_dager)])
    pid = _deltaker(con, kid, "f@eksempel.no")
    kursdag = db.kursdager(con, kid)[0]["id"] if ant_dager > 1 else None
    db.reserver_faktura(con, pid, kursdag)
    db.sett_faktura_forsok_ukjent(con, pid, kursdag, "HTTPError (HTTP 500)")
    con.commit()
    return pid, con.execute("SELECT id FROM faktura_forsok").fetchone()[0]


def test_uavklart_faktura_som_finnes_i_visma_registreres(con):
    pid, fid = _uavklart_faktura(con)
    k = _admin()
    side = k.get("/admin/uavklart").get_data(as_text=True)
    assert "Fakturakurs" in side and 'value="3000"' in side                   # foreslått beløp
    k.post(f"/admin/uavklart/faktura/{fid}", data={"utfall": "fakturert", "faktura_nr": "10042", "belop_nok": "3000"})
    rad = con.execute("SELECT faktura_nr, belop_nok, status FROM faktura WHERE paamelding_id=?", (pid,)).fetchone()
    assert tuple(rad) == ("10042", 3000, "sendt")
    assert con.execute("SELECT COUNT(*) FROM faktura_forsok").fetchone()[0] == 0
    r = k.post(f"/admin/uavklart/faktura/{fid}", data={"utfall": "fakturert", "faktura_nr": "10042", "belop_nok": "3000"})
    assert "ikke lenger uavklart" in k.get(r.headers["Location"]).get_data(as_text=True)
    assert con.execute("SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", (pid,)).fetchone()[0] == 1


def test_delfaktura_foreslaar_samlingens_andel(con):
    _uavklart_faktura(con, pris=1000, ant_dager=3)
    assert 'value="334"' in _admin().get("/admin/uavklart").get_data(as_text=True)   # 334 + 333 + 333


def test_uavklart_faktura_som_ikke_finnes_proeves_igjen(con):
    pid, fid = _uavklart_faktura(con)
    _admin().post(f"/admin/uavklart/faktura/{fid}", data={"utfall": "ikke_fakturert"})
    assert con.execute("SELECT status FROM faktura_forsok WHERE id=?", (fid,)).fetchone()[0] == "feilet"
    assert db.reserver_faktura_pa_nytt(con, pid, None)


@pytest.mark.parametrize("data", [{"faktura_nr": "", "belop_nok": "3000"}, {"faktura_nr": "1", "belop_nok": "0"},
                                  {"faktura_nr": "1", "belop_nok": "abc"}, {"faktura_nr": "x" * 41, "belop_nok": "1"}])
def test_registrering_av_faktura_krever_nummer_og_beloep(con, data):
    _, fid = _uavklart_faktura(con)
    k = _admin()
    r = k.post(f"/admin/uavklart/faktura/{fid}", data={"utfall": "fakturert", **data})
    assert "Fyll inn fakturanummeret" in k.get(r.headers["Location"]).get_data(as_text=True)
    assert con.execute("SELECT status FROM faktura_forsok WHERE id=?", (fid,)).fetchone()[0] == "ukjent"


def test_eksisterende_faktura_hindrer_dobbel_registrering(con):
    pid, fid = _uavklart_faktura(con)
    con.execute("INSERT INTO faktura (paamelding_id, kursdag_id, faktura_nr, belop_nok, status) VALUES (?,NULL,'9',?,'sendt')",
                (pid, 3000))
    con.commit()
    k = _admin()
    r = k.post(f"/admin/uavklart/faktura/{fid}", data={"utfall": "fakturert", "faktura_nr": "10042", "belop_nok": "3000"})
    assert "Det finnes allerede en faktura" in k.get(r.headers["Location"]).get_data(as_text=True)
    assert con.execute("SELECT status FROM faktura_forsok WHERE id=?", (fid,)).fetchone()[0] == "ukjent"


def test_lesetilgang_ser_men_kan_ikke_avklare(con):
    kid = _kurs(con, "U3")
    _uavklart_epost(con, kid)
    db.opprett_admin_bruker(con, "leser", "Leser", "passord-som-holder", rolle="lese")
    con.commit()
    k = _admin("leser", "passord-som-holder")
    side = k.get("/admin/uavklart").get_data(as_text=True)
    assert "u@eksempel.no" in side and 'value="sendt">Er sendt</button>' not in side
    assert k.post("/admin/uavklart/epost", data={"nokkel": f"kurs:{kid}", "type": "ukefor", "mottaker": "u@eksempel.no",
                                                 "utfall": "sendt"}).status_code == 403


def test_oversikten_lenker_til_uavklarte(con):
    _uavklart_epost(con, _kurs(con, "U4"))
    assert 'href="/admin/uavklart">Se og avklar →</a>' in _admin().get("/admin").get_data(as_text=True)


# ============================ optimistisk samtidighetskontroll ============================

def _versjon_fra(html: str) -> str:
    return html.split('name="versjon" value="')[1].split('"')[0]


def _kursdata(**over):
    return {"navn": "Kurs OS", "type": "fysisk", "sted": "Oslo", "zoom_url": "", "kapasitet": "", "pris_nok": "0",
            "fakturering": "person", "betaling": "samlet", "faktura_dager_for": "14", "kursholder_epost": "",
            "notat": "", "paameldingsfrist": "", **over}


def test_lagring_over_noen_andres_endring_avvises(con):
    kid = _kurs(con, "OS")
    kari, ola = _admin(), _admin()
    v_kari = _versjon_fra(kari.get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True))
    v_ola = _versjon_fra(ola.get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True))
    ola.post(f"/admin/kurs/{kid}/oppsett", data=_kursdata(sted="Bergen", versjon=v_ola))
    r = kari.post(f"/admin/kurs/{kid}/oppsett", data=_kursdata(notat="Kari sitt notat", versjon=v_kari))
    assert "Endringene ble ikke lagret: noen andre har endret dette" in kari.get(r.headers["Location"]).get_data(as_text=True)
    rad = con.execute("SELECT sted, notat FROM kurs WHERE id=?", (kid,)).fetchone()
    assert (rad["sted"], rad["notat"]) == ("Bergen", None)                   # Ola sin endring står, Karis ble avvist


def test_zoom_lenke_fra_morgenjobben_overskrives_ikke_av_et_gammelt_skjema(con):
    kid = _kurs(con, "OZ", type="digital", sted=None)
    admin = _admin()
    v = _versjon_fra(admin.get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True))
    con.execute("UPDATE kurs SET zoom_url='https://zoom.us/j/1' WHERE id=?", (kid,))    # morgenjobben
    con.commit()
    admin.post(f"/admin/kurs/{kid}/oppsett", data=_kursdata(type="digital", sted="", versjon=v))
    assert con.execute("SELECT zoom_url FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "https://zoom.us/j/1"


def test_uendret_rad_lagres_som_foer_og_skjema_uten_versjon_kontrolleres_ikke(con):
    kid = _kurs(con, "OU")
    admin = _admin()
    v = _versjon_fra(admin.get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True))
    admin.post(f"/admin/kurs/{kid}/oppsett", data=_kursdata(notat="Første", versjon=v))
    admin.post(f"/admin/kurs/{kid}/oppsett", data=_kursdata(notat="Uten versjon"))
    assert con.execute("SELECT notat FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "Uten versjon"


def test_deltaker_og_paamelding_har_samme_vern(con):
    kid = _kurs(con, "OD", kapasitet=5)
    pid = _deltaker(con, kid, "dora@eksempel.no")
    admin = _admin()
    side = admin.get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
    v_person, v_paamelding = [x.split('"')[0] for x in side.split('name="versjon" value="')[1:3]]
    con.execute("UPDATE deltaker SET telefon='99999999' WHERE epost='dora@eksempel.no'")
    con.execute("UPDATE paamelding SET intern_kommentar='annen admin' WHERE id=?", (pid,))
    con.commit()
    admin.post(f"/admin/kurs/{kid}/deltaker/{pid}/person", data={"fornavn": "Dora", "etternavn": "Test", "epost": "dora@eksempel.no",
                                                                 "telefon": "", "versjon": v_person})
    admin.post(f"/admin/kurs/{kid}/deltaker/{pid}/paamelding", data={"betaler": "person", "intern_kommentar": "min",
                                                                     "versjon": v_paamelding})
    assert con.execute("SELECT telefon FROM deltaker WHERE epost='dora@eksempel.no'").fetchone()[0] == "99999999"
    assert con.execute("SELECT intern_kommentar FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == "annen admin"


def test_nettsiden_har_samme_vern(con):
    kid = _kurs(con, "ON")
    admin = _admin()
    v = _versjon_fra(admin.get(f"/admin/kurs/{kid}/nettside").get_data(as_text=True))
    con.execute("UPDATE kurs SET paamelding_intro='Fra en annen admin' WHERE id=?", (kid,))
    con.commit()
    admin.post(f"/admin/kurs/{kid}/nettside", data={"intro": "Min tekst", "knappetekst": "", "versjon": v})
    assert con.execute("SELECT paamelding_intro FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "Fra en annen admin"


@pytest.mark.parametrize("fakturert", [True, False])
def test_avklar_faktura_svarer_true_foerste_gang_og_false_andre_gang(con, fakturert):
    _, fid = _uavklart_faktura(con)
    kw = {"faktura_nr": "10042", "belop_nok": 3000} if fakturert else {}
    assert db.avklar_faktura(con, fid, fakturert=fakturert, aktor="test", **kw) is True
    assert db.avklar_faktura(con, fid, fakturert=fakturert, aktor="test", **kw) is False
    assert db.avklar_faktura(con, 999999, fakturert=fakturert, aktor="test", **kw) is False
