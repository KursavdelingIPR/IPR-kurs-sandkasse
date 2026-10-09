"""Rabattpriser (Camilla 09.10.2026, kurs/rabatter.py): rabattene velges per kurs, deltakeren velger én pris, prisen er kursets pris minus
rabatten (hele kroner), og en rabatt som må godkjennes holder fakturaen tilbake til en administrator har trykket «Godkjent» eller
«Ikke godkjent». Studentbevis lastes opp og slettes når rabatten er avgjort. Alt er oppdiktet, ingen nettverk."""
import io
from datetime import date, timedelta

import pytest

from adressehjelp import ADRESSE
from kurs import config, daglig, db, kursokonomi, rabatter
from kurs.integrasjoner import epost
from kurs.kjoring import Kjoring
from navnehjelp import gruppeskjema

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (120).to_bytes(4, "big") + (40).to_bytes(4, "big")
       + b"\x08\x02\x00\x00\x00" + b"\x00" * 30)
PDF = b"%PDF-1.4\n%oppdiktet studentbevis\n%%EOF\n"


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
    if brukernavn is not False:
        k.post("/admin/logg-inn", data={"brukernavn": brukernavn or config.ADMIN_BRUKERNAVN, "passord": passord or config.ADMIN_PASSORD})
    return k


def _offentlig():
    from kurs.web import app as webapp
    return webapp.app.test_client()


def _kurs(con, kode="R1", pris=9900, rabatter_=None, **kw):
    start = date.today() + timedelta(days=40)
    kid = db.opprett_kurs(con, kode=kode, navn="Rabattkurs", datoer=[start.isoformat()], sharepoint_mappe=f"Kurs/{kode}",
                          pris_nok=pris, type="fysisk", sted="Oslo", status="aapen", **kw)
    if rabatter_ is not None:
        rabatter.lagre_for_kurs(con, kid, rabatter_, aktor="admin:test")
    con.commit()
    return kid


ALLE = {"nieft": (30, True), "ipr_terapeut": (50, True), "student": (50, True), "psyflix": (25, True)}


def _skjema(**over):
    data = {"fornavn": "Kari", "etternavn": "Nordmann", "epost": "kari@eksempel.no", "samtykke": "on", "samtykke_lagring": "on",
            "betaler": "person", **ADRESSE}
    data.update(over)
    return data


def _p(con, epost_="kari@eksempel.no"):
    return con.execute("""SELECT p.* FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE d.epost=?""",
                       (epost_,)).fetchone()


def _fakturaer(con, pid):
    return con.execute("SELECT belop_nok FROM faktura WHERE paamelding_id=? ORDER BY id", (pid,)).fetchall()


# ============================ prisen ============================

def test_prisen_er_kursets_pris_minus_rabatten_avrundet_til_hele_kroner():
    assert rabatter.deltakerpris(9900, None) == 9900
    assert rabatter.deltakerpris(9900, 50) == 4950
    assert rabatter.deltakerpris(9900, 30) == 6930
    assert rabatter.deltakerpris(4999, 25) == 3749          # 3749,25
    assert rabatter.deltakerpris(4998, 25) == 3749          # 3748,5 rundes opp
    assert rabatter.deltakerpris(0, 50) == 0


def test_prisen_i_databasen_er_den_samme_som_i_python(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@eksempel.no", fornavn="A", etternavn="B")
    for pris in (9900, 4999, 4998, 1, 12345):
        for prosent in (None, 1, 25, 30, 33, 50, 99):
            con.execute("UPDATE kurs SET pris_nok=? WHERE id=?", (pris, kid))
            con.execute("UPDATE paamelding SET rabatt_prosent=? WHERE id=?", (prosent, pid))
            i_sql = con.execute(f"SELECT {rabatter.SQL_PRIS} FROM paamelding p JOIN kurs k ON k.id=p.kurs_id WHERE p.id=?",
                                (pid,)).fetchone()[0]
            assert i_sql == rabatter.deltakerpris(pris, prosent), (pris, prosent)


# ============================ oppsettet på kurset ============================

def test_rabattene_lagres_per_kurs_i_fast_rekkefolge(con):
    kid = _kurs(con)
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    assert rabatter.for_kurs(con, kurs) == []
    assert rabatter.lagre_for_kurs(con, kid, {"student": (50, True), "nieft": (30, False)}, aktor="admin:test")
    assert [(r.kategori, r.prosent, r.maa_godkjennes, r.pris) for r in rabatter.for_kurs(con, kurs)] == [
        ("nieft", 30, False, 6930), ("student", 50, True, 4950)]
    assert not rabatter.lagre_for_kurs(con, kid, {"student": (50, True), "nieft": (30, False)}, aktor="admin:test")
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='kurs_rabatter_endret'").fetchone()[0] == 1
    with pytest.raises(rabatter.Rabattfeil):
        rabatter.lagre_for_kurs(con, kid, {"student": (100, True)}, aktor="admin:test")
    gratis = _kurs(con, "R2", pris=0, rabatter_={"student": (50, True)})
    assert rabatter.for_kurs(con, con.execute("SELECT * FROM kurs WHERE id=?", (gratis,)).fetchone()) == []


def test_oppsett_siden_lagrer_rabattene(con):
    kid = _kurs(con)
    k = _klient()
    side = k.get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True)
    assert "Priser og rabatter" in side and 'name="prosent_psyflix"' in side and 'value="25"' in side
    r = k.post(f"/admin/kurs/{kid}/rabatter", data={"rabatt_student": "1", "prosent_student": "50", "godkjennes_student": "1",
                                                    "rabatt_psyflix": "1", "prosent_psyflix": "20", "prosent_nieft": "30"})
    assert r.status_code == 302 and r.headers["Location"].endswith("#rabatter")
    rader = {x["kategori"]: (x["prosent"], x["maa_godkjennes"]) for x in con.execute("SELECT * FROM kurs_rabatt WHERE kurs_id=?", (kid,))}
    assert rader == {"student": (50, 1), "psyflix": (20, 0)}
    k.post(f"/admin/kurs/{kid}/rabatter", data={"rabatt_student": "1", "prosent_student": "abc"})
    assert con.execute("SELECT COUNT(*) FROM kurs_rabatt WHERE kurs_id=?", (kid,)).fetchone()[0] == 2     # ugyldig: ingenting endret


def test_lesetilgang_kan_ikke_endre_rabattene(con):
    kid = _kurs(con)
    db.opprett_admin_bruker(con, "leser", "Leser", "passord-som-holder", rolle="lese")
    con.commit()
    lese = _klient("leser", "passord-som-holder")
    assert lese.post(f"/admin/kurs/{kid}/rabatter", data={"rabatt_student": "1", "prosent_student": "50"}).status_code == 403


def test_rabattene_folger_med_naar_kurset_kopieres(con):
    kid = _kurs(con, rabatter_={"student": (50, True), "psyflix": (20, True)})
    ny = _kurs(con, "R2")
    rabatter.kopier(con, kid, ny)
    assert {r["kategori"]: r["prosent"] for r in con.execute("SELECT * FROM kurs_rabatt WHERE kurs_id=?", (ny,))} == \
        {"student": 50, "psyflix": 20}


# ============================ påmeldingsskjemaet ============================

def test_skjemaet_uten_rabatter_er_som_for(con):
    _kurs(con)
    side = _offentlig().get("/kurs/R1").get_data(as_text=True)
    assert "priskategori" not in side and "multipart/form-data" not in side and "data-prisdel" not in side


def test_skjemaet_viser_prisene(con):
    _kurs(con, rabatter_=ALLE)
    side = _offentlig().get("/kurs/R1").get_data(as_text=True)
    assert 'enctype="multipart/form-data"' in side and 'name="priskategori"' in side
    for tekst in ("Ordinær pris", "Studentpris", "NIEFT-pris", "Psyflix-pris", 'name="studentbevis"', 'name="psyflix_org"',
                  "Bruk e-posten du er registrert med som NIEFT-medlem", 'data-pris="4950"',
                  # avkrysningene er krav, og det står ved dem (Camilla 09.10.2026: «bør ikke disse være krav?»)
                  'Samtykke <span class="pm-krav">(krav)</span>', 'Bekreftelse <span class="pm-krav">(krav)</span>'):
        assert tekst in side, tekst


def test_student_uten_bevis_avvises(con):
    _kurs(con, rabatter_=ALLE)
    r = _offentlig().post("/kurs/R1", data=_skjema(priskategori="student", rabatt_bekreftet="1"))
    assert r.status_code == 400 and "Last opp studentbeviset ditt" in r.get_data(as_text=True)
    assert _p(con) is None


def test_ugyldig_bevis_og_ukjent_pris_avvises(con):
    _kurs(con, rabatter_={"student": (50, True)})
    r = _offentlig().post("/kurs/R1", data=_skjema(priskategori="student", rabatt_bekreftet="1",
                                                   studentbevis=(io.BytesIO(b"MZ-ikke-et-bilde"), "bevis.exe")))
    assert r.status_code == 400 and "Studentbeviset må være et bilde" in r.get_data(as_text=True)
    r = _offentlig().post("/kurs/R1", data=_skjema(priskategori="psyflix", rabatt_bekreftet="1"))   # kurset gir ikke Psyflix-rabatt
    assert r.status_code == 400 and "Velg en av prisene" in r.get_data(as_text=True)
    assert _p(con) is None


def test_student_med_bevis_faar_plass_men_fakturaen_venter(con, sendt):
    kid = _kurs(con, rabatter_=ALLE)
    r = _offentlig().post("/kurs/R1", data=_skjema(priskategori="student", rabatt_bekreftet="1", rabatt_prosent="99",
                                                   studentbevis=(io.BytesIO(PNG), "studentbevis.png")))
    kvittering = r.get_data(as_text=True)
    assert r.status_code == 200 and "Vi sjekker" not in kvittering              # Camilla 09.10.2026: ikke på kvitteringssiden
    p = _p(con)
    assert (p["status"], p["priskategori"], p["rabatt_prosent"], p["rabatt_status"]) == ("bekreftet", "student", 50, "venter")
    assert rabatter.hent_bevis(con, p["id"]) == ("studentbevis.png", "image/png", PNG)
    assert _fakturaer(con, p["id"]) == []                                  # fakturaen holdes tilbake
    bekreftelse = next(html for til, emne, html in sendt if til == "kari@eksempel.no")
    assert "Faktura på 4950 kr (studentpris)" in bekreftelse
    assert "sjekket" not in bekreftelse                     # Camilla 09.10.2026: at rabatten sjekkes, står ikke i e-posten
    # morgenjobben lager heller ingen faktura mens rabatten venter
    daglig.kjor(Kjoring(con, idag=date.today()))
    con.commit()
    assert _fakturaer(con, p["id"]) == []


def test_godkjent_rabatt_gir_faktura_med_rabattpris(con, sendt):
    kid = _kurs(con, rabatter_=ALLE)
    _offentlig().post("/kurs/R1", data=_skjema(priskategori="student", rabatt_bekreftet="1",
                                               studentbevis=(io.BytesIO(PDF), "bevis.pdf")))
    pid = _p(con)["id"]
    k = _klient()
    vindu = k.get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
    assert "Sjekk studentbevis" in vindu and "Åpne studentbeviset" in vindu
    bevis = k.get(f"/admin/rabatter/{pid}/bevis")
    assert bevis.status_code == 200 and bevis.headers["Content-Type"] == "application/pdf" and bevis.data == PDF
    r = k.post(f"/admin/rabatter/{pid}", data={"beslutning": "godkjent", "tilbake": "vindu"})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/admin/kurs/{kid}/deltaker/{pid}")
    assert [f["belop_nok"] for f in _fakturaer(con, pid)] == [4950]
    assert _p(con)["rabatt_status"] == "godkjent" and rabatter.hent_bevis(con, pid) is None      # beviset er slettet
    assert k.get(f"/admin/rabatter/{pid}/bevis").status_code == 404
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling IN ('rabatt_godkjent','rabattbevis_vist')").fetchone()[0] == 2
    # en avgjort rabatt kan ikke avgjøres på nytt
    k.post(f"/admin/rabatter/{pid}", data={"beslutning": "avvist"})
    assert _p(con)["rabatt_status"] == "godkjent" and [f["belop_nok"] for f in _fakturaer(con, pid)] == [4950]


def test_ikke_godkjent_rabatt_gir_ordinaer_pris(con, sendt):
    kid = _kurs(con, rabatter_=ALLE)
    _offentlig().post("/kurs/R1", data=_skjema(priskategori="nieft", rabatt_bekreftet="1"))
    pid = _p(con)["id"]
    assert _p(con)["rabatt_status"] == "venter"
    _klient().post(f"/admin/rabatter/{pid}", data={"beslutning": "avvist"})
    p = _p(con)
    assert (p["priskategori"], p["rabatt_prosent"], p["rabatt_status"]) == ("nieft", None, "avvist")
    assert [f["belop_nok"] for f in _fakturaer(con, pid)] == [9900]


def test_rabatt_uten_godkjenning_faktureres_med_en_gang(con, sendt):
    _kurs(con, rabatter_={"ipr_terapeut": (50, False)})
    _offentlig().post("/kurs/R1", data=_skjema(priskategori="ipr_terapeut", rabatt_bekreftet="1"))
    p = _p(con)
    assert (p["priskategori"], p["rabatt_status"]) == ("ipr_terapeut", None)
    assert [f["belop_nok"] for f in _fakturaer(con, p["id"])] == [4950]


def test_prisnavnet_midt_i_en_setning():
    assert [rabatter.i_setning(rabatter.PRISNAVN[k]) for k in rabatter.KATEGORIER] ==         ["NIEFT-pris", "IPR-terapeutpris", "studentpris", "Psyflix-pris"]


def test_bekreftelsen_skriver_nieft_med_store_bokstaver(con, sendt):
    _kurs(con, rabatter_=ALLE)
    _offentlig().post("/kurs/R1", data=_skjema(priskategori="nieft", rabatt_bekreftet="1"))
    bekreftelse = next(html for til, emne, html in sendt if til == "kari@eksempel.no")
    assert "(NIEFT-pris)" in bekreftelse and "nieft-pris" not in bekreftelse


def test_rabatten_maa_bekreftes(con):
    _kurs(con, rabatter_=ALLE)
    r = _offentlig().post("/kurs/R1", data=_skjema(priskategori="nieft"))
    assert r.status_code == 400 and "Bekreft at du har rett på prisen" in r.get_data(as_text=True)


def test_psyflix_krever_organisasjon_og_samtykke(con):
    _kurs(con, rabatter_=ALLE)
    r = _offentlig().post("/kurs/R1", data=_skjema(priskategori="psyflix", rabatt_bekreftet="1"))
    tekst = r.get_data(as_text=True)
    assert r.status_code == 400 and "hvilken organisasjon" in tekst and "sjekke medlemskapet ditt hos Psyflix" in tekst
    r = _offentlig().post("/kurs/R1", data=_skjema(priskategori="psyflix", rabatt_bekreftet="1", psyflix_org="Solli DPS",
                                                   psyflix_epost="kari.psy@eksempel.no", psyflix_samtykke="1"))
    assert r.status_code == 200
    p = _p(con)
    assert (p["psyflix_org"], p["psyflix_epost"], p["rabatt_status"]) == ("Solli DPS", "kari.psy@eksempel.no", "venter")


def test_listen_til_psyflix_og_varsel_om_rabatt_siste_aar(con):
    eldre = _kurs(con, "R0", rabatter_=ALLE)
    kid = _kurs(con, rabatter_=ALLE)
    gammel, _ = db.meld_paa(con, eldre, epost="kari@eksempel.no", fornavn="Kari", etternavn="Nordmann",
                            paamelding={"priskategori": "psyflix", "rabatt_prosent": 25, "rabatt_status": "godkjent"})
    con.commit()
    _offentlig().post("/kurs/R1", data=_skjema(priskategori="psyflix", rabatt_bekreftet="1", psyflix_org="Solli DPS",
                                               psyflix_samtykke="1"))
    side = _klient().get("/admin/rabatter").get_data(as_text=True)
    assert "Liste til Psyflix" in side and "Kari Nordmann – kari@eksempel.no – Solli DPS (Rabattkurs)" in side
    assert "Har fått Psyflix-rabatt de siste 12 månedene" in side
    did = _p(con)["deltaker_id"]
    assert rabatter.psyflix_siste_aar(con, did, 0, date.today()) is True
    assert rabatter.psyflix_siste_aar(con, did, 0, date.today() + timedelta(days=400)) is False     # mer enn ett år siden
    assert rabatter.psyflix_siste_aar(con, did + 1000, 0, date.today()) is False                   # en annen person


def test_pris_kan_ikke_manipuleres(con, sendt):
    _kurs(con, rabatter_={"nieft": (30, False)})
    _offentlig().post("/kurs/R1", data=_skjema(priskategori="nieft", rabatt_bekreftet="1", rabatt_prosent="99",
                                               rabatt_status="godkjent", psyflix_org="X"))
    p = _p(con)
    assert (p["rabatt_prosent"], p["rabatt_status"], p["psyflix_org"]) == (30, None, None)


# ============================ bedriftspåmeldingen ============================

def _gruppe(**over):
    data = {"kontakt_navn": "Kari HR", "kontakt_epost": "kari.hr@firma.no", "org_nr": "999900003", "faktura_ref": "BEST-1",
            "deltaker_navn": ["Ola Nordmann"], "deltaker_epost": ["ola@firma.no"], "deltaker_telefon": [""],
            "deltaker_arbeidssted": [""], "samtykke": "on", "samtykke_lagring": "on"}
    data.update(over)
    return gruppeskjema(data)


def test_bedriftspaamelding_med_pris_per_deltaker(con, sendt):
    _kurs(con, rabatter_=ALLE)
    side = _offentlig().get("/kurs/R1/gruppe").get_data(as_text=True)
    assert 'name="deltaker_priskategori"' in side and 'name="deltaker_studentbevis"' in side and "multipart/form-data" in side
    data = _gruppe(deltaker_navn=["Ola Nordmann", "Per Hansen"], deltaker_epost=["ola@firma.no", "per@firma.no"],
                   deltaker_telefon=["", ""], deltaker_arbeidssted=["", ""], deltaker_priskategori=["", "nieft"])
    r = _offentlig().post("/kurs/R1/gruppe", data=data)
    assert r.status_code == 400 and "Bekreft at deltakerne har rett på prisene" in r.get_data(as_text=True)
    r = _offentlig().post("/kurs/R1/gruppe", data={**data, "rabatt_bekreftet": "1"})
    assert r.status_code == 302
    ola, per = _p(con, "ola@firma.no"), _p(con, "per@firma.no")
    assert (ola["priskategori"], ola["rabatt_status"]) == (None, None)
    assert (per["priskategori"], per["rabatt_prosent"], per["rabatt_status"]) == ("nieft", 30, "venter")
    kvittering = next(html for til, emne, html in sendt if til == "kari.hr@firma.no")
    assert "til prisen som er valgt for deltakeren" in kvittering


def test_bedriftspaamelding_student_maa_ha_bevis(con):
    _kurs(con, rabatter_=ALLE)
    data = _gruppe(deltaker_priskategori=["student"], rabatt_bekreftet="1")
    r = _offentlig().post("/kurs/R1/gruppe", data=data)
    assert r.status_code == 400 and "Deltaker 1: last opp studentbeviset" in r.get_data(as_text=True)
    r = _offentlig().post("/kurs/R1/gruppe", data={**data, "deltaker_studentbevis": [(io.BytesIO(PNG), "ola.png")]})
    assert r.status_code == 302
    p = _p(con, "ola@firma.no")
    assert (p["priskategori"], p["rabatt_status"]) == ("student", "venter") and rabatter.har_bevis(con, p["id"])


# ============================ administrator ============================

def test_legg_til_deltaker_med_rabattpris_er_godkjent(con):
    kid = _kurs(con, rabatter_=ALLE)
    k = _klient()
    assert 'name="priskategori"' in k.get(f"/admin/kurs/{kid}/deltaker/ny").get_data(as_text=True)
    r = k.post(f"/admin/kurs/{kid}/deltaker/ny", data={"fornavn": "Kari", "etternavn": "Nordmann", "epost": "kari@eksempel.no",
                                                         "betaler": "person", "priskategori": "student", **ADRESSE})
    assert r.status_code == 302
    p = _p(con)
    assert (p["priskategori"], p["rabatt_prosent"], p["rabatt_status"]) == ("student", 50, "godkjent")


def test_deltakervinduet_endrer_prisen_foer_fakturaen(con, sendt):
    kid = _kurs(con, rabatter_=ALLE)
    pid, _ = db.meld_paa(con, kid, epost="kari@eksempel.no", fornavn="Kari", etternavn="Nordmann",
                         paamelding={"sveiper_utsatt": 1})
    con.commit()
    k = _klient()
    vindu = k.get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
    assert 'id="f-priskategori"' in vindu and 'name="priskategori_for" value=""' in vindu
    data = {"betaler": "person", "priskategori": "psyflix", "priskategori_for": ""}
    assert k.post(f"/admin/kurs/{kid}/deltaker/{pid}/paamelding", data=data).status_code == 302
    p = _p(con)
    assert (p["priskategori"], p["rabatt_prosent"], p["rabatt_status"]) == ("psyflix", 25, "godkjent")
    # lagres vinduet uten at prisen er endret, står prisen urørt (også om kursets prosent er endret siden)
    rabatter.lagre_for_kurs(con, kid, {**ALLE, "psyflix": (20, True)}, aktor="admin:test")
    con.commit()
    k.post(f"/admin/kurs/{kid}/deltaker/{pid}/paamelding", data={"betaler": "person", "priskategori": "psyflix",
                                                                  "priskategori_for": "psyflix"})
    assert _p(con)["rabatt_prosent"] == 25


def test_prisen_kan_ikke_endres_etter_fakturaen(con, sendt):
    kid = _kurs(con, rabatter_=ALLE)
    pid, _ = db.meld_paa(con, kid, epost="kari@eksempel.no", fornavn="Kari", etternavn="Nordmann")
    con.commit()
    from kurs import sveiper
    sveiper.kjor(Kjoring(con, idag=date.today()), pid)
    con.commit()
    assert [f["belop_nok"] for f in _fakturaer(con, pid)] == [9900]
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    with pytest.raises(rabatter.Rabattfeil):
        rabatter.endre(con, kurs, pid, "student", aktor="admin:test")
    assert 'id="f-priskategori"' not in _klient().get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)


def test_lesetilgang_ser_men_kan_ikke_aapne_bevis_eller_avgjore(con):
    kid = _kurs(con, rabatter_=ALLE)
    _offentlig().post("/kurs/R1", data=_skjema(priskategori="student", rabatt_bekreftet="1",
                                               studentbevis=(io.BytesIO(PNG), "bevis.png")))
    pid = _p(con)["id"]
    db.opprett_admin_bruker(con, "leser", "Leser", "passord-som-holder", rolle="lese")
    con.commit()
    lese = _klient("leser", "passord-som-holder")
    side = lese.get("/admin/rabatter").get_data(as_text=True)
    assert "Sjekk studentbevis" in side and "Åpne studentbeviset" not in side and 'value="godkjent"' not in side
    assert lese.get(f"/admin/rabatter/{pid}/bevis").status_code == 403
    assert lese.post(f"/admin/rabatter/{pid}", data={"beslutning": "godkjent"}).status_code == 403
    assert _p(con)["rabatt_status"] == "venter"


def test_oversikten_listen_og_fakturastatus(con):
    kid = _kurs(con, rabatter_=ALLE)
    _offentlig().post("/kurs/R1", data=_skjema(priskategori="nieft", rabatt_bekreftet="1"))
    k = _klient()
    oversikt = k.get("/admin").get_data(as_text=True)
    assert "Rabatter som venter på godkjenning" in oversikt and "/admin/rabatter" in oversikt
    assert rabatter.antall_ventende(con) == 1
    liste = k.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    assert "Sjekk NIEFT-medlemskap" in liste and "Venter på rabattsjekk" in liste


# ============================ økonomi, personvern og rydding ============================

def test_inntekten_er_summen_av_prisene(con):
    kid = _kurs(con, rabatter_=ALLE)
    for n, (kategori, prosent) in enumerate(((None, None), ("student", 50), ("nieft", 30))):
        db.meld_paa(con, kid, epost=f"d{n}@eksempel.no", fornavn=f"D{n}", etternavn="Test",
                    paamelding={"priskategori": kategori, "rabatt_prosent": prosent})
    con.commit()
    rad = next(r for r in kursokonomi.kursoversikt(con)[0] if r["id"] == kid)
    assert (rad["forventet"], rad["rabatt"], rad["med_rabatt"]) == (9900 + 4950 + 6930, 4950 + 2970, 2)
    side = _klient().get("/admin/rapporter").get_data(as_text=True)
    assert "Rabatt" in side and "prisene til" in side


def test_anonymisering_sletter_bevis_og_psyflix(con):
    kid = _kurs(con, rabatter_=ALLE)
    pid, _ = db.meld_paa(con, kid, epost="kari@eksempel.no", fornavn="Kari", etternavn="Nordmann",
                         paamelding={"priskategori": "psyflix", "rabatt_prosent": 25, "psyflix_org": "Solli DPS",
                                     "psyflix_epost": "kari.psy@eksempel.no"})
    rabatter.lagre_bevis(con, pid, ("bevis.png", "image/png", PNG))
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()
    ut = db.anonymiser_deltaker(con, _p(con)["deltaker_id"], aktor="admin:test")
    con.commit()
    assert ut["rabattbevis"] == 1
    p = con.execute("SELECT psyflix_org, psyflix_epost FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert tuple(p) == (None, None)


def test_morgenjobben_sletter_bevis_som_ikke_trengs(con):
    kid = _kurs(con, rabatter_=ALLE)
    a, _ = db.meld_paa(con, kid, epost="a@eksempel.no", fornavn="A", etternavn="Test")
    b, _ = db.meld_paa(con, kid, epost="b@eksempel.no", fornavn="B", etternavn="Test")
    for pid in (a, b):
        rabatter.lagre_bevis(con, pid, ("bevis.png", "image/png", PNG))
    con.execute("UPDATE paamelding SET status='avmeldt' WHERE id=?", (b,))
    con.commit()
    assert rabatter.rydd(con) == 1 and rabatter.har_bevis(con, a) and not rabatter.har_bevis(con, b)
    assert rabatter.rydd(con) == 0                          # idempotent


def test_ny_paamelding_arver_aldri_en_gammel_rabatt(con):
    kid = _kurs(con, rabatter_=ALLE)
    pid, _ = db.meld_paa(con, kid, epost="kari@eksempel.no", fornavn="Kari", etternavn="Nordmann",
                         paamelding={"priskategori": "student", "rabatt_prosent": 50, "rabatt_status": "venter"})
    rabatter.lagre_bevis(con, pid, ("bevis.png", "image/png", PNG))
    con.execute("UPDATE paamelding SET status='avmeldt' WHERE id=?", (pid,))
    igjen, _ = db.meld_paa(con, kid, epost="kari@eksempel.no", fornavn="Kari", etternavn="Nordmann")
    assert igjen == pid
    p = _p(con)
    assert (p["priskategori"], p["rabatt_prosent"], p["rabatt_status"]) == (None, None, None)
    assert not rabatter.har_bevis(con, pid)


def test_fakturateksten_sier_hvilken_pris(con, sendt):
    _kurs(con, rabatter_={"student": (50, False)})
    from kurs.integrasjoner import visma
    linjer = []
    ekte = visma.fakturer
    visma.fakturer = lambda g: (linjer.append(g.linjetekst), ekte(g))[1]
    try:
        _offentlig().post("/kurs/R1", data=_skjema(priskategori="student", rabatt_bekreftet="1",
                                                   studentbevis=(io.BytesIO(PNG), "bevis.png")))
    finally:
        visma.fakturer = ekte
    assert linjer == ["Rabattkurs – Kari Nordmann – Studentpris"]


# ============================ kvitteringssiden (Camilla 09.10.2026) ============================

@pytest.mark.parametrize("merke, kontakt", [(None, "kurs@ipr.no"), ("terapiakademiet", "post@terapiakademiet.no")])
def test_kvitteringen_sier_hvor_bekreftelsen_er_sendt_og_hvem_de_kontakter(con, sendt, merke, kontakt):
    _kurs(con, merke=merke)
    r = _offentlig().post("/kurs/R1", data=_skjema())
    side = r.get_data(as_text=True)
    assert r.status_code == 200 and "Du er påmeldt!" in side
    assert "Vi har sendt påmeldingsbekreftelsen til e-postadressen du registrerte ved påmeldingen: <strong>kari@eksempel.no</strong>." in side
    assert f'heller ikke i søppelposten, ta kontakt med <a href="mailto:{kontakt}">{kontakt}</a>.' in side
    assert "Faktura kommer separat." in side
    assert f'Spørsmål? Skriv til <a href="mailto:{kontakt}">' in side                  # bunnen har samme adresse
    assert f'Spørsmål? Skriv til <a href="mailto:{kontakt}">' in _offentlig().get("/kurs/R1").get_data(as_text=True)


def test_kvitteringen_paa_venteliste_har_samme_hjelp(con, sendt):
    kid = _kurs(con, kapasitet=1)
    db.meld_paa(con, kid, epost="forst@eksempel.no", fornavn="Først", etternavn="Test")
    con.commit()
    side = _offentlig().post("/kurs/R1", data=_skjema()).get_data(as_text=True)
    assert "Du står på venteliste" in side and "bekreftelse på at du står på venteliste" in side
    assert 'ta kontakt med <a href="mailto:kurs@ipr.no">kurs@ipr.no</a>.' in side

