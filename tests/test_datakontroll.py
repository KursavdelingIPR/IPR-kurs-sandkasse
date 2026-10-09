"""Datakontrollen (Camilla 07.10.2026, kurs/datakontroll.py): opplysninger som ser feil ut, f.eks. gmal.com eller hotmail.no,
markeres på Oversikten, på /admin/datakontroll, i deltakerlisten og i deltakervinduet, og skjemaet foreslår «Mente du …?».
«Det stemmer» gjelder bare verdien som ble sjekket. Alt er oppdiktet, ingen nettverk."""
import json
from datetime import date, timedelta

import pytest

from kurs import config, datakontroll, db
from kurs.datakontroll import domene_forslag, sjekk_epost, sjekk_navn, sjekk_postnr, sjekk_telefon
from kurs.integrasjoner import epost


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    monkeypatch.setattr(epost, "send", lambda *a, **kw: None)
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _klient(brukernavn=None, passord=None):
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": brukernavn or config.ADMIN_BRUKERNAVN, "passord": passord or config.ADMIN_PASSORD})
    return k


def _kurs(con, kode="DK1", status="aapen"):
    start = date.today() + timedelta(days=40)
    kid = db.opprett_kurs(con, kode=kode, navn="Kontrollkurs", datoer=[start.isoformat()], sharepoint_mappe=f"Kurs/{kode}",
                          pris_nok=0, type="fysisk", sted="Oslo", status=status)
    con.commit()
    return kid


def _meld(con, kid, epost_, fornavn="Kari", etternavn="Nordmann", **deltaker):
    pid, _ = db.meld_paa(con, kid, epost=epost_, fornavn=fornavn, etternavn=etternavn, deltaker=deltaker or None)
    con.commit()
    return pid


def _koder(funn):
    return [f.kode for f in funn]


# ============================ e-post ============================

@pytest.mark.parametrize("domene, riktig", [
    ("gmal.com", "gmail.com"), ("gmial.com", "gmail.com"), ("gamil.com", "gmail.com"), ("gmail.co", "gmail.com"),
    ("gmail.con", "gmail.com"), ("GMAIL.CON", "gmail.com"), ("hotmail.no", "hotmail.com"), ("hotmial.com", "hotmail.com"),
    ("hotmai.com", "hotmail.com"), ("gmail.no", "gmail.com"), ("outlok.com", "outlook.com"), ("onlie.no", "online.no"),
    ("icloud.co", "icloud.com"), ("yaho.no", "yahoo.no"), ("firma.con", "firma.com"), ("klinikken.nett", "klinikken.net"),
])
def test_vanlige_skrivefeil_i_domenet_faar_forslag(domene, riktig):
    assert domene_forslag(domene) == riktig
    funn = sjekk_epost(f"kari@{domene}")
    assert funn.kode == "epost" and funn.forslag == f"kari@{riktig}" and funn.tekst == f"Mente du kari@{riktig}?"


@pytest.mark.parametrize("adresse", [
    "kari@gmail.com", "kari@hotmail.com", "kari@online.no", "kari@ipr.no", "kari@firma.no", "kari@uit.no", "kari@mail.com",
    "kari@frisurf.no", "kari@helse-bergen.no", "kari@psykologforeningen.no", "kari@live.no", "kari@proton.me",
    "kari.nordmann@student.uib.no", "", None,
])
def test_vanlige_og_ukjente_domener_er_i_orden(adresse):
    assert sjekk_epost(adresse) is None


@pytest.mark.parametrize("adresse", ["kari.gmail.com", "kari@@gmail.com", "kari@gmail", "kari @gmail.com", "kari@gmail..com",
                                     "@gmail.com", "kari@gmail.com, ola@gmail.com"])
def test_ufullstendige_adresser_markeres(adresse):
    funn = sjekk_epost(adresse)
    assert funn and funn.kode == "epost" and funn.forslag is None and "ufullstendig" in funn.tekst


def test_tastefeil_telles_riktig():
    assert datakontroll.avstand("gmail.com", "gmail.com") == 0
    assert datakontroll.avstand("gmial.com", "gmail.com") == 1          # to bokstaver har byttet plass = én feil
    assert datakontroll.avstand("gmal.com", "gmail.com") == 1
    assert datakontroll.avstand("ipr.no", "aol.com") > 1


# ============================ navn, telefon, postnummer ============================

def test_navn():
    assert sjekk_navn("Kari", "Nordmann") is None
    assert sjekk_navn("Kari-Anne", "Øvre-Ås") is None
    assert sjekk_navn("Ola", "") is None
    smaa = sjekk_navn("kari-anne", "nordmann")
    assert smaa.forslag == "Kari-Anne Nordmann" and "små bokstaver" in smaa.tekst
    store = sjekk_navn("KARI", "NORDMANN")
    assert store.forslag == "Kari Nordmann" and "store bokstaver" in store.tekst
    assert sjekk_navn("Kari", "NORDMANN") is None                       # bare etternavnet med store bokstaver er vanlig
    assert sjekk_navn("LI", "") is None                                  # for kort til å si noe om
    assert "e-postadresse" in sjekk_navn("kari@gmail.com", "").tekst
    assert "tall" in sjekk_navn("Kari2", "Nordmann").tekst
    assert "like" in sjekk_navn("Hansen", "hansen").tekst


@pytest.mark.parametrize("telefon", ["91234567", "912 34 567", "+47 912 34 567", "0047 91234567", "+46 70 123 45 67", "", None])
def test_telefon_i_orden(telefon):
    assert sjekk_telefon(telefon) is None


def test_telefon_som_ser_feil_ut():
    assert sjekk_telefon("9123456").tekst == "Telefonnummeret har 7 siffer (norske nummer har 8)."
    assert sjekk_telefon("+47 912 34 5678").tekst == "Telefonnummeret har 9 siffer (norske nummer har 8)."
    assert "bokstaver" in sjekk_telefon("ring meg").tekst
    assert sjekk_telefon("+1 23").tekst == "Telefonnummeret har 3 siffer."


def test_postnummer():
    assert sjekk_postnr("0150") is None and sjekk_postnr("") is None and sjekk_postnr(None) is None
    for feil in ("150", "01500", "O150", "0150 Oslo"):
        assert sjekk_postnr(feil).kode == "postnr", feil


# ============================ dubletter ============================

def test_samme_navn_eller_telefon_paa_samme_kurs_er_mulig_dublett(con):
    kid = _kurs(con)
    a = _meld(con, kid, "kari@firma.no", telefon="91234567")
    b = _meld(con, kid, "kari.nordmann@gmail.com", telefon="22334455")
    c = _meld(con, kid, "ola@firma.no", fornavn="Ola", etternavn="Hansen", telefon="+47 912 34 567")
    funn = datakontroll.for_kurs(con, kid)
    assert "samme navn" in next(f.tekst for f in funn[a] if f.kode == "dublett")
    assert "kari.nordmann@gmail.com" in next(f.tekst for f in funn[a] if f.kode == "dublett")
    assert "samme navn" in next(f.tekst for f in funn[b] if f.kode == "dublett")
    assert "samme telefonnummer" in next(f.tekst for f in funn[c] if f.kode == "dublett")


def test_samme_person_paa_to_kurs_er_ikke_dublett(con):
    k1, k2 = _kurs(con, "DK1"), _kurs(con, "DK2")
    _meld(con, k1, "kari@firma.no")
    _meld(con, k2, "kari@firma.no")
    assert datakontroll.for_kurs(con, k1) == {} and datakontroll.for_kurs(con, k2) == {}


def test_avmeldte_telles_ikke(con):
    kid = _kurs(con)
    _meld(con, kid, "kari@firma.no")
    b = _meld(con, kid, "kari@gmal.com")
    con.execute("UPDATE paamelding SET status='avmeldt' WHERE id=?", (b,))
    con.commit()
    assert datakontroll.for_kurs(con, kid) == {}


# ============================ Oversikten og listen ============================

def test_oversikten_viser_bare_kurs_som_ikke_er_avsluttet_eller_avlyst(con):
    aapen, avsluttet = _kurs(con, "DK1"), _kurs(con, "DK2")
    _meld(con, aapen, "kari@gmal.com")
    _meld(con, avsluttet, "ola@hotmail.no", fornavn="Ola")
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (avsluttet,))
    con.commit()
    grupper = datakontroll.oversikt(con)
    assert [g["kurs"]["id"] for g in grupper] == [aapen] and datakontroll.antall(con) == 1
    assert _koder(grupper[0]["rader"][0]["funn"]) == ["epost"]


def test_oversikten_og_siden_for_feil(con):
    kid = _kurs(con)
    pid = _meld(con, kid, "kari@gmal.com")
    k = _klient()
    oversikt = k.get("/admin").get_data(as_text=True)
    assert "Mulig feil i påmeldinger" in oversikt and "/admin/datakontroll" in oversikt
    side = k.get("/admin/datakontroll").get_data(as_text=True)
    assert "Mente du kari@gmail.com?" in side and "Kontrollkurs" in side and f"/deltaker/{pid}" in side
    assert 'action="/admin/datakontroll/ok"' in side


def test_ingen_funn_gir_rolig_side(con):
    _meld(con, _kurs(con), "kari@gmail.com")
    k = _klient()
    assert "Ingen opplysninger ser feil ut" in k.get("/admin/datakontroll").get_data(as_text=True)
    assert "Se og rett" not in k.get("/admin").get_data(as_text=True)


def test_deltakerlisten_og_vinduet_markerer(con):
    kid = _kurs(con)
    pid = _meld(con, kid, "kari@gmal.com")
    _meld(con, kid, "ola@firma.no", fornavn="Ola", etternavn="Hansen")
    k = _klient()
    liste = k.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    assert liste.count("Sjekk opplysningene") == 1 and "Mente du kari@gmail.com?" in liste
    vindu = k.get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
    assert "Ser dette riktig ut?" in vindu and "Mente du kari@gmail.com?" in vindu


# ============================ «Det stemmer» ============================

def test_det_stemmer_skjuler_funnet_til_verdien_endres(con):
    kid = _kurs(con)
    pid = _meld(con, kid, "kari@hotmail.no")
    did = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pid,)).fetchone()[0]
    k = _klient()
    r = k.post("/admin/datakontroll/ok", data={"paamelding_id": pid, "kode": "epost", "tilbake": "vindu"})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/admin/kurs/{kid}/deltaker/{pid}")
    assert datakontroll.for_paamelding(con, pid) == [] and datakontroll.antall(con) == 0
    rad = con.execute("SELECT * FROM datakontroll_ok WHERE deltaker_id=?", (did,)).fetchone()
    assert rad["kode"] == "epost" and "hotmail" not in rad["verdi_hash"]            # bare en hash, ingen kopi av adressen
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='datakontroll_ok'").fetchone()[0] == 1
    # samme verdi en gang til (store/små bokstaver og mellomrom spiller ingen rolle) gir ingen ny rad
    datakontroll.godta(con, did, "epost", " KARI@hotmail.no", aktor="admin:test")
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM datakontroll_ok").fetchone()[0] == 1
    # en ny verdi kontrolleres på nytt
    con.execute("UPDATE deltaker SET epost='kari@gmal.com' WHERE id=?", (did,))
    con.commit()
    assert _koder(datakontroll.for_paamelding(con, pid)) == ["epost"]


def test_det_stemmer_gjelder_bare_den_ene_kontrollen(con):
    kid = _kurs(con)
    pid = _meld(con, kid, "kari@gmal.com", telefon="1234")
    assert _koder(datakontroll.for_paamelding(con, pid)) == ["epost", "telefon"]
    _klient().post("/admin/datakontroll/ok", data={"paamelding_id": pid, "kode": "telefon"})
    assert _koder(datakontroll.for_paamelding(con, pid)) == ["epost"]


def test_det_stemmer_uten_funn_eller_med_ukjent_kode_gjoer_ingenting(con):
    kid = _kurs(con)
    pid = _meld(con, kid, "kari@gmail.com")
    k = _klient()
    for kode in ("epost", "finnes-ikke"):
        assert k.post("/admin/datakontroll/ok", data={"paamelding_id": pid, "kode": kode}).status_code == 302
    assert con.execute("SELECT COUNT(*) FROM datakontroll_ok").fetchone()[0] == 0
    assert k.post("/admin/datakontroll/ok", data={"paamelding_id": 99999, "kode": "epost"}).status_code == 404
    with pytest.raises(ValueError):
        datakontroll.godta(con, 1, "finnes-ikke", "x", aktor="admin:test")


def test_lesetilgang_ser_men_kan_ikke_merke(con):
    kid = _kurs(con)
    pid = _meld(con, kid, "kari@gmal.com")
    db.opprett_admin_bruker(con, "leser", "Leser", "passord-som-holder", rolle="lese")
    con.commit()
    lese = _klient("leser", "passord-som-holder")
    side = lese.get("/admin/datakontroll").get_data(as_text=True)
    assert "Mente du kari@gmail.com?" in side and 'action="/admin/datakontroll/ok"' not in side
    assert lese.post("/admin/datakontroll/ok", data={"paamelding_id": pid, "kode": "epost"}).status_code == 403
    assert con.execute("SELECT COUNT(*) FROM datakontroll_ok").fetchone()[0] == 0


def test_anonymisering_sletter_merkene(con):
    kid = _kurs(con)
    pid = _meld(con, kid, "kari@hotmail.no")
    did = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pid,)).fetchone()[0]
    datakontroll.godta(con, did, "epost", "kari@hotmail.no", aktor="admin:test")
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))      # aktive påmeldinger kan ikke anonymiseres
    con.commit()
    ut = db.anonymiser_deltaker(con, did, aktor="admin:test")
    con.commit()
    assert ut["datakontroll"] == 1
    assert con.execute("SELECT COUNT(*) FROM datakontroll_ok").fetchone()[0] == 0


# ============================ skjemaet: «Mente du …?» ============================

def test_skjemaene_faar_samme_domeneliste(con):
    regler = json.loads(datakontroll.for_skjema())
    assert regler["vanlige"] == list(datakontroll.VANLIGE_DOMENER)
    assert regler["sjeldne"]["hotmail.no"] == "hotmail.com" and regler["tld"]["con"] == "com"
    kid = _kurs(con)
    kode = con.execute("SELECT kode FROM kurs WHERE id=?", (kid,)).fetchone()[0]
    from kurs.web import app as webapp
    side = webapp.app.test_client().get(f"/kurs/{kode}").get_data(as_text=True)
    assert 'id="f-epost"' in side and "data-epost-sjekk=" in side and "hotmail.no" in side
    import re
    betalt = db.opprett_kurs(con, kode="DK9", navn="Kurs med pris", datoer=[(date.today() + timedelta(days=40)).isoformat()],
                             sharepoint_mappe="Kurs/DK9", pris_nok=1000, type="fysisk", sted="Oslo", status="aapen")
    con.commit()
    side = webapp.app.test_client().get("/kurs/DK9").get_data(as_text=True)
    assert re.search(r'<input id="f-faktura_epost"[^>]*data-epost-sjekk=', side), betalt     # E-post for faktura
    assert "data-epost-sjekk=" in _klient().get(f"/admin/kurs/{kid}/deltaker/ny").get_data(as_text=True)
    # bedriftspåmeldingen: kontaktpersonen og hver deltakerrad (også radene som legges til)
    from pathlib import Path
    gruppe = (Path(config.__file__).resolve().parent / "web" / "templates" / "kurs_gruppe.html").read_text(encoding="utf-8")
    assert gruppe.count('data-epost-sjekk="{{ EPOST_SJEKK }}"') == 3
    # ikke i innlogging og innsjekk: der må adressen være nøyaktig den som er registrert (også med en skrivefeil)
    maler = Path(config.__file__).resolve().parent / "web" / "templates"
    for navn in ("logg_inn.html", "innsjekk.html"):
        assert "data-epost-sjekk" not in (maler / navn).read_text(encoding="utf-8"), navn
    assert "data-epost-sjekk" in (maler / "sporsmal.html").read_text(encoding="utf-8")


def test_forslaget_i_nettleseren_er_bare_et_forslag():
    """Skriptet stopper aldri innsendingen (ingen setCustomValidity), og det bruker samme grense som Python."""
    from pathlib import Path
    js = (Path(config.__file__).resolve().parent / "web" / "static" / "app.js").read_text(encoding="utf-8")
    blokk = js[js.index("«Mente du …?» under e-postfeltene"):]
    assert "input[data-epost-sjekk]" in blokk and "setCustomValidity" not in blokk
    assert "domene.length <= 8 ? 1 : 2" in blokk
