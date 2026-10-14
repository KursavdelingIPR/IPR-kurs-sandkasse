"""E-posten når en rabatt ikke kan godkjennes (Camilla 09.10.2026: «da må vi sende en e-post til deltakeren hvor vi forklarer at vi
enten trenger mer informasjon, rett informasjon, eller at de blir belastet for ordinær pris. Avhengig av situasjonen.»), og rettingene
fra gjennomgangen samme dag: første avgjørelse gjelder, store mobilbilder av studentbevis, «Behandle» med rabatt som venter og
foreslått beløp for uavklarte fakturaer. Alt er oppdiktet, ingen nettverk."""
import io
from datetime import date, timedelta

import pytest

from adressehjelp import ADRESSE
from kurs import behandling, config, db, maltekster, rabatter
from kurs.integrasjoner import epost

PDF = b"%PDF-1.4\n%oppdiktet studentbevis\n%%EOF\n"
NBSP = " "


def _png(bredde: int, hoyde: int, storrelse: int = 0) -> bytes:
    """Et PNG-hode med oppgitt bredde og høyde (det systemet leser), fylt opp til `storrelse` byte."""
    data = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + bredde.to_bytes(4, "big") + hoyde.to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00" + b"\x00" * 30)
    return data + b"\x00" * max(0, storrelse - len(data))


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
def sendt(monkeypatch):
    """Alle e-poster som går ut: [(til, emne, html)]."""
    ut = []
    monkeypatch.setattr(epost, "send", lambda til, emne, html, vedlegg=(): ut.append((til, emne, html)))
    return ut


def _klient(brukernavn=None, passord=None):
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": brukernavn or config.ADMIN_BRUKERNAVN, "passord": passord or config.ADMIN_PASSORD})
    return k


def _offentlig():
    from kurs.web import app as webapp
    return webapp.app.test_client()


ALLE = {"nieft": (30, True), "ipr_terapeut": (50, True), "student": (50, True), "psyflix": (25, True)}


def _kurs(con, pris=9900):
    start = date.today() + timedelta(days=40)
    kid = db.opprett_kurs(con, kode="R1", navn="Rabattkurs", datoer=[start.isoformat()], sharepoint_mappe="Kurs/R1",
                          pris_nok=pris, type="fysisk", sted="Oslo", status="aapen")
    rabatter.lagre_for_kurs(con, kid, ALLE, aktor="admin:test")
    con.commit()
    return kid


def _student(con, kid):
    """Kari melder seg på med studentpris og studentbevis: rabatten venter, og fakturaen holdes tilbake."""
    r = _offentlig().post("/kurs/R1", data={"fornavn": "Kari", "etternavn": "Nordmann", "epost": "kari@eksempel.no",
                                            "samtykke": "on", "samtykke_lagring": "on", "betaler": "person", **ADRESSE,
                                            "priskategori": "student", "rabatt_bekreftet": "1",
                                            "studentbevis": (io.BytesIO(PDF), "bevis.pdf")})
    assert r.status_code == 200
    return con.execute("SELECT p.id FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE d.epost='kari@eksempel.no'"
                       ).fetchone()["id"]


def _p(con, pid):
    return con.execute("SELECT * FROM paamelding WHERE id=?", (pid,)).fetchone()


def _fakturaer(con, pid):
    return [r["belop_nok"] for r in con.execute("SELECT belop_nok FROM faktura WHERE paamelding_id=? ORDER BY id", (pid,))]


def _til_kari(sendt):
    return [(emne, html) for til, emne, html in sendt if til == "kari@eksempel.no"]


def _send(k, kid, pid, situasjon, tekst, emne="Om studentprisen"):
    return k.post(f"/admin/kurs/{kid}/deltaker/{pid}/epost/send",
                  data={"skjemanokkel": "a1b2c3d4e5f60718", "emne": emne, "tekst": tekst, "rabatt": situasjon})


# ============================ knappene og forslaget ============================

def test_rabattboksen_har_godkjent_og_tre_valg_med_e_post(con):
    kid = _kurs(con)
    pid = _student(con, kid)
    vindu = _klient().get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
    assert 'value="godkjent"' in vindu and "Ikke godkjent (ordinær pris)" not in vindu
    for kode, navn in rabatter.SITUASJONER.items():
        assert f"rabatt={kode}" in vindu and navn in vindu


def test_vinduet_har_forslaget_med_navn_kurs_og_priser(con):
    kid = _kurs(con)
    pid = _student(con, kid)
    side = _klient().get(f"/admin/kurs/{kid}/deltaker/{pid}/epost/ny?rabatt=info").get_data(as_text=True)
    assert 'value="Vi trenger litt mer informasjon: Rabattkurs"' in side
    assert "Hei Kari," in side and f"Du har valgt studentpris (4{NBSP}950{NBSP}kr)" in side and "[Skriv hva som mangler]" in side
    assert 'name="rabatt" value="info"' in side and "Rabatten venter fortsatt" in side
    ordinaer = _klient().get(f"/admin/kurs/{kid}/deltaker/{pid}/epost/ny?rabatt=ordinaer").get_data(as_text=True)
    assert f"ordinær pris: 9{NBSP}900{NBSP}kr" in ordinaer and "Send og sett ordinær pris" in ordinaer
    assert _klient().get(f"/admin/kurs/{kid}/deltaker/{pid}/epost/ny?rabatt=noe-annet").status_code == 404


def test_teksten_kan_endres_i_e_postmaler(con):
    kid = _kurs(con)
    pid = _student(con, kid)
    k = _klient()
    oversikt = k.get("/admin/e-postmaler").get_data(as_text=True)
    assert "Rabatt som ikke kan godkjennes ennå" in oversikt
    forhandsvis = k.get("/admin/e-postmaler/rabatt_avklaring/forhandsvis")
    tekst = forhandsvis.get_data(as_text=True)
    assert forhandsvis.status_code == 200
    for variant in ("Trenger mer informasjon", "Trenger riktig informasjon", "Ordinær pris"):
        assert variant in tekst, variant
    maltekster.lagre_maltekst(con, "rabatt_avklaring", "emne_riktig", "Feil i opplysningene til {kursnavn}", aktor="admin:test")
    con.commit()
    side = k.get(f"/admin/kurs/{kid}/deltaker/{pid}/epost/ny?rabatt=riktig").get_data(as_text=True)
    assert 'value="Feil i opplysningene til Rabattkurs"' in side


# ============================ sendingen ============================

def test_skriv_maa_byttes_ut_foer_e_posten_sendes(con, sendt):
    kid = _kurs(con)
    pid = _student(con, kid)
    r = _send(_klient(), kid, pid, "info", "Hei Kari,\n\n[Skriv hva som mangler]")
    assert r.status_code == 200 and "der det står [Skriv …]" in r.get_data(as_text=True)
    assert _til_kari(sendt)[1:] == []                       # bare bekreftelsen fra påmeldingen
    assert con.execute("SELECT COUNT(*) FROM admin_utsending").fetchone()[0] == 0
    assert _p(con, pid)["rabatt_status"] == "venter"


def test_mer_informasjon_sendes_og_rabatten_venter_fortsatt(con, sendt):
    kid = _kurs(con)
    pid = _student(con, kid)
    k = _klient()
    r = _send(k, kid, pid, "info", "Hei Kari,\n\nStudentbeviset er utløpt. Send oss et gyldig bevis.")
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/admin/kurs/{kid}/deltaker/{pid}")
    (emne, html), = _til_kari(sendt)[1:]
    assert emne == "Om studentprisen" and "Studentbeviset er utløpt" in html
    p = _p(con, pid)
    assert (p["rabatt_status"], p["rabatt_prosent"]) == ("venter", 50) and _fakturaer(con, pid) == []
    vindu = k.get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
    assert "Du ba om mer" in vindu and "informasjon" in vindu
    assert "Ba om mer informasjon" in k.get("/admin/rabatter").get_data(as_text=True)
    logger = k.get(f"/admin/kurs/{kid}/deltaker/{pid}/logger").get_data(as_text=True)
    assert "Ba om mer informasjon om studentpris (e-post)" in logger
    # samme skjema sendt igjen (dobbeltklikk): ingen ny e-post og ingen ny rad i loggen
    _send(k, kid, pid, "info", "Hei Kari,\n\nStudentbeviset er utløpt. Send oss et gyldig bevis.")
    assert len(_til_kari(sendt)) == 2
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='rabatt_info_bedt'").fetchone()[0] == 1
    # så kommer svaret, og rabatten godkjennes
    k.post(f"/admin/rabatter/{pid}", data={"beslutning": "godkjent"})
    assert _fakturaer(con, pid) == [4950]
    assert "Studentpris godkjent" in k.get(f"/admin/kurs/{kid}/deltaker/{pid}/logger").get_data(as_text=True)


def test_ordinaer_pris_sendes_og_fakturaen_lages_med_ordinaer_pris(con, sendt):
    kid = _kurs(con)
    pid = _student(con, kid)
    k = _klient()
    side = k.get(f"/admin/kurs/{kid}/deltaker/{pid}/epost/ny?rabatt=ordinaer").get_data(as_text=True)
    assert "[Skriv" not in side
    _send(k, kid, pid, "ordinaer", "Hei Kari,\n\nVi kunne dessverre ikke godkjenne studentprisen.", emne="Om prisen for Rabattkurs")
    p = _p(con, pid)
    assert (p["rabatt_status"], p["rabatt_prosent"]) == ("avvist", None)
    assert _fakturaer(con, pid) == [9900]
    assert [emne for emne, _ in _til_kari(sendt)][1:] == ["Om prisen for Rabattkurs"]
    assert rabatter.hent_bevis(con, pid) is None


def test_ingenting_sendes_naar_rabatten_alt_er_avgjort(con, sendt):
    kid = _kurs(con)
    pid = _student(con, kid)
    k = _klient()
    k.post(f"/admin/rabatter/{pid}", data={"beslutning": "godkjent"})
    r = _send(k, kid, pid, "ordinaer", "Hei Kari,\n\nOrdinær pris.")
    assert r.status_code == 302
    assert _til_kari(sendt)[1:] == [] and _p(con, pid)["rabatt_status"] == "godkjent" and _fakturaer(con, pid) == [4950]
    # vinduet åpnes heller ikke med forslaget
    assert k.get(f"/admin/kurs/{kid}/deltaker/{pid}/epost/ny?rabatt=info").status_code == 302


def test_lesetilgang_ser_ikke_valgene(con):
    kid = _kurs(con)
    pid = _student(con, kid)
    db.opprett_admin_bruker(con, "leser", "Leser", "passord-som-holder", rolle="lese")
    con.commit()
    vindu = _klient("leser", "passord-som-holder").get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
    assert "Sjekk studentbevis" in vindu and "rabatt=info" not in vindu and 'value="godkjent"' not in vindu


# ============================ rettinger fra gjennomgangen 09.10.2026 ============================

def test_den_forste_avgjorelsen_gjelder(con):
    kid = _kurs(con)
    pid = _student(con, kid)
    rabatter.godkjenn(con, pid, aktor="admin:a")
    with pytest.raises(rabatter.Rabattfeil):            # den andre administratoren kom for sent: ingenting endres
        rabatter._avgjor(con, pid, {"rabatt_status": rabatter.AVVIST, "rabatt_prosent": None})
    p = _p(con, pid)
    assert (p["rabatt_status"], p["rabatt_prosent"]) == ("godkjent", 50)


def test_store_mobilbilder_av_studentbevis_godtas():
    navn, mimetype, _ = rabatter.kontroller_bevis("IMG_2041.png", _png(8064, 6048, 9 * 1024 * 1024))   # 48 MP, 9 MB
    assert (navn, mimetype) == ("IMG_2041.png", "image/png")
    with pytest.raises(rabatter.Rabattfeil, match="for stort"):
        rabatter.kontroller_bevis("bevis.png", _png(13000, 9000))
    with pytest.raises(rabatter.Rabattfeil, match="maks 15 MB"):
        rabatter.kontroller_bevis("bevis.png", _png(4000, 3000, 16 * 1024 * 1024))


def test_behandle_med_rabatt_som_venter_er_ikke_teknisk_feil(con, sendt):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="holdt@eksempel.no", fornavn="Holdt", etternavn="Test")
    con.execute("""UPDATE paamelding SET sveiper_utsatt=1, priskategori='student', rabatt_prosent=50, rabatt_status='venter'
                   WHERE id=?""", (pid,))
    con.execute("UPDATE deltaker SET adresse='Gate 1', postnr='0150', poststed='Oslo' WHERE epost='holdt@eksempel.no'")
    con.commit()
    res = behandling.behandle_holdt_paamelding(con, kid, pid, date.today(), aktor="admin:test")
    assert (res.kode, res.arsak_kode) == (behandling.FULLFORT, behandling.ARSAK_FAKTURA_VENTER_RABATT)
    assert "rabatten" in res.arsak and "Teknisk feil" not in res.arsak and _fakturaer(con, pid) == []


def test_uavklart_faktura_foreslaar_deltakerens_pris(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="u@eksempel.no", fornavn="U", etternavn="Test")
    con.execute("UPDATE paamelding SET sveiper_kjort=1, priskategori='student', rabatt_prosent=50, rabatt_status='godkjent' WHERE id=?",
                (pid,))
    db.reserver_faktura(con, pid, None)
    db.sett_faktura_forsok_ukjent(con, pid, None, "HTTPError (HTTP 500)")
    con.commit()
    assert 'value="4950"' in _klient().get("/admin/uavklart").get_data(as_text=True)
