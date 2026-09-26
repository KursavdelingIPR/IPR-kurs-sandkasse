"""Fase 12A: e-postemnet er REN TEKST (ikke HTML), og kan aldri inneholde linjeskift.

Eksisterende feil (rettet her): emnet ble rendret med HTML-autoescape, saa kursnavnet `Kurs A & B <x>` ga emnet
`Bekreftelse: Kurs A &amp; B &lt;x&gt;`. Kroppen SKAL fortsatt escapes. Se ogsaa gullstandard-testene.
"""
from datetime import date

import pytest

from kurs import config, db, sveiper
from kurs.integrasjoner import epost
from kurs.kjoring import Kjoring
from kurs.sveiper import PLAN_INGEN, FakturaPlan

FARLIG = "Kurs A & B <x>"


def _n(html):
    return " ".join(html.split())


KURS = {"navn": FARLIG, "type": "fysisk", "sted": "Oslo", "start_kl": "09:00", "slutt_kl": "16:00",
        "pris_nok": 0, "fakturering": "person", "faktura_dager_for": 14, "notat": None,
        "zoom_url": None, "zoom_id": None, "zoom_pw": None}
DELTAKER = {"navn": "Ola Nordmann", "fornavn": "Ola", "betaling": "samlet", "betaler": "person", "org_navn": None}
DAG = {"dato": "2027-03-01", "start_kl": None, "slutt_kl": None}
PLAN = FakturaPlan(PLAN_INGEN)      # bekreftelse krever faktura_plan (laast fakturablokk); irrelevant for emnetestene


# ============================ emnet er ren tekst ============================

def test_bekreftelse_emne_er_ren_tekst_men_kroppen_escapes():
    emne, html = epost.render("bekreftelse", p=DELTAKER, kurs=KURS, dager=[DAG], faktura_plan=PLAN)
    assert emne == "Bekreftelse: Kurs A & B <x>"                       # ikke &amp; / &lt;
    assert "&amp;" not in emne and "&lt;" not in emne
    assert "<strong>Kurs A &amp; B &lt;x&gt;</strong>" in html          # kroppen er fortsatt HTML-escapet
    assert "<x>" not in html                                            # ingen rå tag i kroppen


@pytest.mark.parametrize("mal,data,forventet", [
    ("venteliste", dict(p=DELTAKER, kurs=KURS), "Venteliste: Kurs A & B <x>"),
    ("avlysning", dict(d=DELTAKER, kurs=KURS), "Avlyst: Kurs A & B <x>"),
    ("kursbevis_klar", dict(navn="Ola Nordmann", fornavn="Ola", kurs=KURS), "Kursbevis: Kurs A & B <x>"),
    ("ukefor", dict(d=DELTAKER, kurs=KURS, dager=[DAG]), "Velkommen til Kurs A & B <x> – praktisk informasjon"),
    ("dagfor", dict(d=DELTAKER, kurs=KURS, dag=DAG, nr=1, antall=2), "I morgen starter Kurs A & B <x>"),
    ("dagfor", dict(d=DELTAKER, kurs=KURS, dag=DAG, nr=2, antall=3), "I morgen, dag 2: Kurs A & B <x>"),
    ("dagfor", dict(d=DELTAKER, kurs=KURS, dag=DAG, nr=3, antall=3), "Siste kursdag i morgen: Kurs A & B <x>"),
    ("firmapaamelding_kvittering",
     dict(kontakt={"navn": "K Test", "fornavn": "K", "firmanavn": "F"}, kurs=KURS, antall_totalt=1, antall_bekreftet=1, antall_venteliste=0,
          antall_feilet=0, kvittering_url="/x"),
     "Bedriftspåmelding til Kurs A & B <x> – kvittering"),
    ("purring", dict(m={"ansvarlig_navn": "P", "beskrivelse": "A & B <c>", "kursnavn": FARLIG, "frist": "2027-01-01", "id": 1},
                     igjen=7),
     "Påminnelse: A & B <c> til Kurs A & B <x> – frist om 7 dager"),
    ("eskalering", dict(m={"ansvarlig_navn": "P", "ansvarlig_epost": "p@x.no", "beskrivelse": "M", "kode": "K",
                           "kursnavn": FARLIG, "frist": "2027-01-01"}, igjen=-2),
     "Mangler materiell: Kurs A & B <x> (2 dager over frist)"),
])
def test_alle_maler_har_ren_tekst_emne_og_escapet_kropp(mal, data, forventet):
    emne, html = epost.render(mal, **data)
    assert emne == forventet
    assert "<x>" not in html and "<c>" not in html                     # kroppen har ingen raa tags fra data


def test_manuell_epost_emne_fra_admin_er_ren_tekst():
    emne, html = epost.render("admin_melding", emne="Tips & triks <2>", tekst="Hei", d=DELTAKER)
    assert emne == "Tips & triks <2>"
    assert "&lt;2&gt;" not in html                                       # emnet lekker ikke inn i kroppen heller


def test_vanlige_emner_er_uendret():
    emne, _ = epost.render("bekreftelse", p=DELTAKER, kurs={**KURS, "navn": "Veiledning i praksis"}, dager=[DAG], faktura_plan=PLAN)
    assert emne == "Bekreftelse: Veiledning i praksis"


# ============================ CR/LF kan aldri bli e-posthode ============================

@pytest.mark.parametrize("navn", [
    "Kurs" + chr(13) + chr(10) + "Bcc: ond@x.no",       # CRLF
    "Kurs" + chr(10) + "Bcc: ond@x.no",                 # LF
    "Kurs" + chr(13) + "Bcc: ond@x.no",                 # bare CR
    "Kurs" + chr(0x2028) + "Bcc: ond@x.no",             # Unicode linjeskille
    "Kurs" + chr(0x85) + "Bcc: ond@x.no",               # NEL
    "Kurs" + chr(0) + "Bcc: ond@x.no",                  # NUL
])
def test_linjeskift_i_kursnavn_gir_aldri_linjeskift_i_emnet_og_lekker_ikke_inn_i_kroppen(navn):
    emne, html = epost.render("bekreftelse", p=DELTAKER, kurs={**KURS, "navn": navn}, dager=[DAG], faktura_plan=PLAN)
    for tegn in (chr(13), chr(10), chr(0x2028), chr(0x85), chr(0)):
        assert tegn not in emne
    assert emne == "Bekreftelse: Kurs Bcc: ond@x.no"                    # erstattet med mellomrom - sendingen stoppes ikke
    assert html.lstrip().startswith("<div")                             # kroppen begynner fortsatt med rammen
    assert not _n(html).startswith("Bcc")                               # ingenting fra emnet ble flyttet inn i kroppen


def test_linjeskift_i_admin_emne_i_render_gir_ett_linjeskiftfritt_emne():
    emne, html = epost.render("admin_melding", emne="Hei" + chr(13) + chr(10) + "Bcc: ond@x.no", tekst="T", d=DELTAKER)
    assert chr(10) not in emne and chr(13) not in emne
    assert html.lstrip().startswith("<div") and "Bcc" not in html


def test_har_kontrolltegn():
    assert epost.har_kontrolltegn("A" + chr(10) + "B") and epost.har_kontrolltegn("A" + chr(13))
    assert not epost.har_kontrolltegn("Vanlig emne: æøå & <ok>")


# ============================ det som faktisk sendes / vises ============================

@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "ROT", tmp_path)
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _kurs_med_farlig_navn(con):
    kid = db.opprett_kurs(con, kode="EM1", navn=FARLIG, datoer=["2027-03-01"], sharepoint_mappe="Kurs/EM1", pris_nok=0)
    pid, _ = db.meld_paa(con, kid, epost="ola@x.no", fornavn="Ola", etternavn="Test")
    con.commit()
    return kid, pid


def test_epost_send_faar_ren_tekst_emne_fra_den_ekte_motoren(con, monkeypatch):
    kid, pid = _kurs_med_farlig_navn(con)
    sendt = []
    monkeypatch.setattr(epost, "send", lambda til, emne, html, *a, **kw: sendt.append((til, emne, html)))
    sveiper.kjor(Kjoring(con, idag=date(2027, 3, 1)), pid)
    (til, emne, html), = sendt
    assert emne == "Bekreftelse: Kurs A & B <x>"
    assert "Kurs A &amp; B &lt;x&gt;" in html and "<x>" not in html


def test_demo_utboks_lagrer_ren_tekst_emne_og_admin_visningen_escapes_det(con):
    kid, pid = _kurs_med_farlig_navn(con)
    sveiper.kjor(Kjoring(con, idag=date(2027, 3, 1)), pid)              # ekte demo-sending til utboks
    (melding,) = [m for m in epost.les_utboks() if m["til"] == "ola@x.no"]
    assert melding["emne"] == "Bekreftelse: Kurs A & B <x>"
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    side = k.get("/admin/utboks").get_data(as_text=True)
    assert "Bekreftelse: Kurs A &amp; B &lt;x&gt;" in side              # visningen er HTML-escapet (ingen XSS)
    assert "Bekreftelse: Kurs A & B <x>" not in side


# ============================ admin-skjemaet avviser linjeskift i emnet ============================

def _admin(con):
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


@pytest.mark.parametrize("emne", ["Hei" + chr(10) + "Bcc: ond@x.no", "Hei" + chr(13) + chr(10) + "Bcc: ond@x.no",
                                  "Hei" + chr(0x2028) + "x"])
def test_admin_skjema_avviser_linjeskift_i_emne(con, emne):
    kid, pid = _kurs_med_farlig_navn(con)
    resp = _admin(con).post(f"/admin/kurs/{kid}/epost/forhandsvis",
                            data={"emne": emne, "tekst": "Hei", "paamelding_id": [str(pid)]})
    assert "Emnet kan ikke inneholde linjeskift." in resp.get_data(as_text=True)
    assert con.execute("SELECT COUNT(*) FROM admin_utsending").fetchone()[0] == 0       # ingen utsendelse opprettet


def test_admin_skjema_godtar_vanlig_emne_med_spesialtegn(con):
    kid, pid = _kurs_med_farlig_navn(con)
    resp = _admin(con).post(f"/admin/kurs/{kid}/epost/forhandsvis",
                            data={"emne": "Tips & triks <2>", "tekst": "Hei", "paamelding_id": [str(pid)]})
    assert resp.status_code == 200 and "Emnet kan ikke inneholde" not in resp.get_data(as_text=True)
    assert con.execute("SELECT COUNT(*) FROM admin_utsending").fetchone()[0] == 1
