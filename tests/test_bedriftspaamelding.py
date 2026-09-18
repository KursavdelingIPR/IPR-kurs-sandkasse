"""Fase 7: bedriftspaamelding / flere deltakere samtidig."""
from datetime import date, timedelta

import pytest

from kurs import config, db


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    return c


def _klient():
    from kurs.web import app as webapp
    return webapp.app.test_client()


def _kurs(con, kode="T1", start=None, **kw):
    start = start or (date.today() + timedelta(days=30))
    return db.opprett_kurs(con, kode=kode, datoer=[start.isoformat()], sharepoint_mappe=f"Kurs/{kode}",
                           **{"navn": "Testkurs", "pris_nok": 1000, **kw})


def _fersk(con):
    return db.koble(config.DB_STI)


def _grunnlag(**over):
    data = {
        "kontakt_navn": "Kari HR", "kontakt_epost": "kari.hr@firma.no", "kontakt_telefon": "90000000",
        "firmanavn": "Firma AS", "org_nr": "999888777", "faktura_ref": "BEST-1",
        "deltaker_navn": ["Ola Nordmann"], "deltaker_epost": ["ola@firma.no"],
        "deltaker_telefon": [""], "deltaker_arbeidssted": [""], "samtykke": "on",
    }
    data.update(over)
    return data


def _post(klient, kode, **over):
    return klient.post(f"/kurs/{kode}/gruppe", data=_grunnlag(**over), follow_redirects=True)


# ---------------- grunnleggende flyt ----------------

def test_gruppe_faar_plass_naar_kapasitet_holder(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    klient = _klient()
    r = _post(klient, "T1", deltaker_navn=["Ola", "Per"], deltaker_epost=["ola@firma.no", "per@firma.no"],
             deltaker_telefon=["", ""], deltaker_arbeidssted=["", ""])
    assert r.status_code == 200
    tekst = r.get_data(as_text=True)
    assert tekst.count("Bekreftet") == 2
    fersk = _fersk(con)
    assert fersk.execute("SELECT COUNT(*) FROM paamelding WHERE status='bekreftet'").fetchone()[0] == 2


def test_gruppe_stoerre_enn_kapasitet_deles_riktig(con):
    kid = _kurs(con, kapasitet=1)
    con.commit()
    klient = _klient()
    r = _post(klient, "T1", deltaker_navn=["Først", "Sist"], deltaker_epost=["forst@firma.no", "sist@firma.no"],
             deltaker_telefon=["", ""], deltaker_arbeidssted=["", ""])
    fersk = _fersk(con)
    forst = fersk.execute("SELECT status FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE d.epost='forst@firma.no'").fetchone()[0]
    sist = fersk.execute("SELECT status FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE d.epost='sist@firma.no'").fetchone()[0]
    assert (forst, sist) == ("bekreftet", "venteliste")


def test_individuell_bekreftelse_og_kontaktperson_kvittering_sendes_begge(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    klient = _klient()
    _post(klient, "T1")
    filer = list(config.UTBOKS.glob("*.html"))
    innhold = [f.read_text(encoding="utf-8") for f in filer]
    import json
    mottakere = []
    for t in innhold:
        forste, _, _ = t.partition("\n")
        mottakere.append(json.loads(forste.removeprefix("<!--META ").removesuffix(" -->"))["til"])
    assert "ola@firma.no" in mottakere  # individuell bekreftelse til deltakeren
    assert "kari.hr@firma.no" in mottakere  # samlet kvittering til kontaktpersonen


def test_billettinfo_kopieres_til_hver_paamelding(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    klient = _klient()
    _post(klient, "T1")
    rad = _fersk(con).execute(
        "SELECT betaler, org_navn, org_nr, faktura_ref, faktura_epost FROM paamelding").fetchone()
    assert rad["betaler"] == "organisasjon"
    assert rad["org_navn"] == "Firma AS"
    assert rad["org_nr"] == "999888777"
    assert rad["faktura_ref"] == "BEST-1"
    assert rad["faktura_epost"] == "kari.hr@firma.no"


def test_eksisterende_deltaker_gjenkjennes_ikke_duplisert(con):
    kid1 = _kurs(con, "K1")
    kid2 = _kurs(con, "K2")
    db.meld_paa(con, kid1, epost="ola@firma.no", navn="Ola Nordmann")
    con.commit()
    klient = _klient()
    _post(klient, "K2")
    fersk = _fersk(con)
    assert fersk.execute("SELECT COUNT(*) FROM deltaker WHERE epost='ola@firma.no'").fetchone()[0] == 1
    assert fersk.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 2  # begge kursene


def test_faktura_opprettes_kun_naar_kurset_fakturerer_per_person(con):
    kid_person = _kurs(con, "PERS", fakturering="person", kapasitet=5)
    kid_org = _kurs(con, "ORG", fakturering="organisasjon", kapasitet=5)
    con.commit()
    klient = _klient()
    _post(klient, "PERS")
    _post(klient, "ORG")
    fersk = _fersk(con)
    assert fersk.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 1  # kun for PERS-kurset


# ---------------- delvis vellykket / feilhaandtering ----------------

def test_duplikat_epost_i_samme_innsending_avvises_med_tydelig_feil(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    klient = _klient()
    r = _post(klient, "T1", deltaker_navn=["A", "B"], deltaker_epost=["dup@firma.no", "DUP@firma.no"],
             deltaker_telefon=["", ""], deltaker_arbeidssted=["", ""])
    assert "flere ganger" in r.get_data(as_text=True).lower()
    assert _fersk(con).execute("SELECT COUNT(*) FROM firmapaamelding").fetchone()[0] == 0
    assert _fersk(con).execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 0


def test_en_allerede_paameldt_stopper_ikke_resten_av_gruppa(con):
    kid = _kurs(con, kapasitet=5)
    db.meld_paa(con, kid, epost="ola@firma.no", navn="Ola Nordmann")  # alt paameldt fra for
    con.commit()
    klient = _klient()
    r = _post(klient, "T1", deltaker_navn=["Ola Nordmann", "Ny Person"],
             deltaker_epost=["ola@firma.no", "ny@firma.no"], deltaker_telefon=["", ""], deltaker_arbeidssted=["", ""])
    tekst = r.get_data(as_text=True)
    assert "Bekreftet" in tekst  # Ny Person gikk gjennom
    assert "Ikke registrert" in tekst  # Ola feilet paent
    fersk = _fersk(con)
    assert fersk.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 2  # Ola sin gamle + Ny Person


def test_feilet_deltaker_faar_ingen_individuell_epost(con):
    kid = _kurs(con, kapasitet=5)
    db.meld_paa(con, kid, epost="ola@firma.no", navn="Ola Nordmann")
    con.commit()
    klient = _klient()
    _post(klient, "T1", deltaker_navn=["Ola Nordmann", "Ny Person"],
         deltaker_epost=["ola@firma.no", "ny@firma.no"], deltaker_telefon=["", ""], deltaker_arbeidssted=["", ""])
    import json
    mottakere = []
    for f in config.UTBOKS.glob("*.html"):
        forste, _, _ = f.read_text(encoding="utf-8").partition("\n")
        meta = json.loads(forste.removeprefix("<!--META ").removesuffix(" -->"))
        mottakere.append((meta["til"], meta["emne"]))
    bekreftelser_til_ola = [m for m in mottakere if m[0] == "ola@firma.no" and "Bekreftelse" in m[1]]
    assert bekreftelser_til_ola == []  # Ola feilet -> ingen (ny) bekreftelse-epost til ham


def test_kvitteringen_gjenspeiler_faktisk_sluttresultat(con):
    kid = _kurs(con, kapasitet=1)
    con.commit()
    klient = _klient()
    r = _post(klient, "T1", deltaker_navn=["Først", "Sist"], deltaker_epost=["forst@firma.no", "sist@firma.no"],
             deltaker_telefon=["", ""], deltaker_arbeidssted=["", ""])
    tekst = r.get_data(as_text=True)
    assert tekst.count("Bekreftet") == 1
    assert tekst.count("Venteliste") == 1


# ---------------- kvitteringsside: personvern ----------------

def test_kvitteringsside_viser_ikke_epost_telefon_eller_hpr(con):
    kid = _kurs(con, kapasitet=5, spesialistlop="EFT")
    con.commit()
    klient = _klient()
    r = _post(klient, "T1", deltaker_navn=["Ola Nordmann"], deltaker_epost=["ola@firma.no"],
             deltaker_telefon=["99999999"], deltaker_arbeidssted=["Sykehuset"],
             deltaker_hpr=["1234567"])
    tekst = r.get_data(as_text=True)
    assert "Ola Nordmann" in tekst
    assert "ola@firma.no" not in tekst
    assert "99999999" not in tekst
    assert "1234567" not in tekst
    assert "Sykehuset" not in tekst


def test_kvitteringstoken_er_langt_og_tilfeldig(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    klient = _klient()
    _post(klient, "T1")
    token = _fersk(con).execute("SELECT kvittering_token FROM firmapaamelding").fetchone()[0]
    assert len(token) >= 32


def test_feil_token_gir_404(con):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    assert klient.get("/kurs/T1/gruppe/kvittering/feil-token").status_code == 404


def test_token_fra_annet_kurs_gir_404(con):
    kid1 = _kurs(con, "K1")
    kid2 = _kurs(con, "K2")
    con.commit()
    klient = _klient()
    _post(klient, "K1")
    faktisk_token = _fersk(con).execute("SELECT kvittering_token FROM firmapaamelding").fetchone()[0]
    assert klient.get(f"/kurs/K2/gruppe/kvittering/{faktisk_token}").status_code == 404


# ---------------- dobbel innsending ----------------

def test_dobbel_innsending_lager_ikke_ny_firmapaamelding(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    klient = _klient()
    r1 = klient.post("/kurs/T1/gruppe", data=_grunnlag(), follow_redirects=False)
    r2 = klient.post("/kurs/T1/gruppe", data=_grunnlag(), follow_redirects=False)  # identisk, "dobbeltklikk"
    assert r1.headers["Location"] == r2.headers["Location"]
    fersk = _fersk(con)
    assert fersk.execute("SELECT COUNT(*) FROM firmapaamelding").fetchone()[0] == 1
    assert fersk.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 1


def test_dobbel_innsending_sender_ikke_dobbel_epost(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    klient = _klient()
    klient.post("/kurs/T1/gruppe", data=_grunnlag())
    klient.post("/kurs/T1/gruppe", data=_grunnlag())
    assert len(list(config.UTBOKS.glob("*.html"))) == 2  # 1 bekreftelse + 1 kvittering, ikke 4


def test_ulik_deltakerliste_gir_ny_innsending_selv_med_samme_kontakt(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    klient = _klient()
    klient.post("/kurs/T1/gruppe", data=_grunnlag())
    klient.post("/kurs/T1/gruppe", data=_grunnlag(deltaker_navn=["Per Hansen"], deltaker_epost=["per@firma.no"]))
    assert _fersk(con).execute("SELECT COUNT(*) FROM firmapaamelding").fetchone()[0] == 2


# ---------------- server-side validering ----------------

def test_maks_deltakere_haandheves_paa_serveren(con):
    kid = _kurs(con, kapasitet=100)
    con.commit()
    klient = _klient()
    mange = [f"p{n}@firma.no" for n in range(31)]
    r = _post(klient, "T1", deltaker_navn=[f"P{n}" for n in range(31)], deltaker_epost=mange,
             deltaker_telefon=[""] * 31, deltaker_arbeidssted=[""] * 31)
    assert "maks" in r.get_data(as_text=True).lower()
    assert _fersk(con).execute("SELECT COUNT(*) FROM firmapaamelding").fetchone()[0] == 0


def test_org_nr_paakrevd(con):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    r = _post(klient, "T1", org_nr="")
    assert "organisasjonsnummer" in r.get_data(as_text=True).lower()
    assert _fersk(con).execute("SELECT COUNT(*) FROM firmapaamelding").fetchone()[0] == 0


def test_ingen_deltakere_avvises(con):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    r = _post(klient, "T1", deltaker_navn=[""], deltaker_epost=[""], deltaker_telefon=[""], deltaker_arbeidssted=[""])
    assert "minst én deltaker" in r.get_data(as_text=True).lower()


# ---------------- personvern/logging ----------------

def test_hendelse_logges_uten_personopplysninger(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    klient = _klient()
    _post(klient, "T1")
    rad = _fersk(con).execute(
        "SELECT * FROM hendelse WHERE handling='firmapaamelding_opprettet' ORDER BY id DESC LIMIT 1").fetchone()
    assert rad is not None
    assert "ola@firma.no" not in rad["detaljer"]
    assert "Kari" not in rad["detaljer"]
    assert '"antall": 1' in rad["detaljer"]
