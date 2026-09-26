"""Fase 4: redigering av kurs - trygge felt, laasing, kapasitet, avlysning og paameldingsfrist."""
from datetime import date, timedelta

import pytest

from kurs import config, daglig, db, sveiper
from kurs.kjoring import Kjoring


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


def _logg_inn(klient):
    klient.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})


def _kurs(con, kode="T1", start=None, **kw):
    start = start or (date.today() + timedelta(days=30))
    return db.opprett_kurs(con, kode=kode, navn="Testkurs", datoer=[start.isoformat()],
                           sharepoint_mappe=f"Kurs/{kode}", **{"pris_nok": 1000, **kw})


def _fersk(con):
    return db.koble(config.DB_STI)


def _oppsett_data(**over):
    data = {"navn": "Testkurs", "type": "fysisk", "sted": "Bergen", "zoom_url": "",
            "kapasitet": "", "paameldingsfrist": "", "pris_nok": "1000", "fakturering": "person",
            "betaling": "samlet", "faktura_dager_for": "14", "kursholder_epost": "", "notat": ""}
    data.update(over)
    return data


# ---------------- trygge felt ----------------

def test_trygge_felt_kan_redigeres(con):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(f"/admin/kurs/{kid}/oppsett", data=_oppsett_data(navn="Nytt navn", sted="Oslo", notat="Praktisk"))
    rad = _fersk(con).execute("SELECT navn, sted, notat FROM kurs WHERE id=?", (kid,)).fetchone()
    assert (rad["navn"], rad["sted"], rad["notat"]) == ("Nytt navn", "Oslo", "Praktisk")


def test_type_fysisk_nullstiller_zoom_felt(con):
    kid = _kurs(con, type="digital")
    con.execute("UPDATE kurs SET zoom_url='https://zoom/x', zoom_id='1', zoom_pw='pw' WHERE id=?", (kid,))
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(f"/admin/kurs/{kid}/oppsett", data=_oppsett_data(type="fysisk", zoom_url="https://zoom/skal-bort"))
    rad = _fersk(con).execute("SELECT type, zoom_url, zoom_id, zoom_pw FROM kurs WHERE id=?", (kid,)).fetchone()
    assert (rad["type"], rad["zoom_url"], rad["zoom_id"], rad["zoom_pw"]) == ("fysisk", None, None, None)


def test_kursholder_endring_synker_ikke_leverte_materiellkrav(con):
    kid = _kurs(con, kursholder_epost="gammel@ipr.no")
    con.execute("INSERT INTO materiell_krav (kurs_id, ansvarlig_navn, ansvarlig_epost, frist) VALUES (?,?,?,?)",
               (kid, "Gammel", "gammel@ipr.no", date.today().isoformat()))
    con.execute("INSERT INTO materiell_krav (kurs_id, ansvarlig_navn, ansvarlig_epost, frist, levert_ts) "
               "VALUES (?,?,?,?,'2020-01-01')", (kid, "Gammel", "gammel@ipr.no", date.today().isoformat()))
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(f"/admin/kurs/{kid}/oppsett", data=_oppsett_data(kursholder_epost="ny@ipr.no"))
    fersk = _fersk(con)
    assert fersk.execute("SELECT ansvarlig_epost FROM materiell_krav WHERE levert_ts IS NULL").fetchone()[0] == "ny@ipr.no"
    assert fersk.execute("SELECT ansvarlig_epost FROM materiell_krav WHERE levert_ts IS NOT NULL").fetchone()[0] == "gammel@ipr.no"


# ---------------- laasing av pris/fakturering ----------------

def test_pris_fakturering_redigerbart_for_faktura(con):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(f"/admin/kurs/{kid}/oppsett", data=_oppsett_data(pris_nok="2000", betaling="per_samling"))
    rad = _fersk(con).execute("SELECT pris_nok, betaling FROM kurs WHERE id=?", (kid,)).fetchone()
    assert (rad["pris_nok"], rad["betaling"]) == (2000, "per_samling")


def test_pris_fakturering_ikke_omgaas_via_direkte_post_etter_faktura(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.execute("INSERT INTO faktura (paamelding_id, belop_nok, status) VALUES (?, 1000, 'sendt')", (pid,))
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(f"/admin/kurs/{kid}/oppsett", data=_oppsett_data(
        pris_nok="99999", fakturering="ingen", betaling="per_samling", faktura_dager_for="1", navn="Endret navn"))
    rad = _fersk(con).execute(
        "SELECT navn, pris_nok, fakturering, betaling, faktura_dager_for FROM kurs WHERE id=?", (kid,)).fetchone()
    assert rad["navn"] == "Endret navn"  # ulaaste felt fungerer fortsatt
    assert (rad["pris_nok"], rad["fakturering"], rad["betaling"], rad["faktura_dager_for"]) == (1000, "person", "samlet", 14)


def test_laasing_haandheves_ogsaa_paa_db_niva(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.execute("INSERT INTO faktura (paamelding_id, belop_nok, status) VALUES (?, 1000, 'sendt')", (pid,))
    con.commit()
    db.oppdater_kurs_felter(con, kid, {"pris_nok": 5000, "navn": "Rett fra db"}, aktor="test")
    con.commit()
    rad = con.execute("SELECT navn, pris_nok FROM kurs WHERE id=?", (kid,)).fetchone()
    assert (rad["navn"], rad["pris_nok"]) == ("Rett fra db", 1000)


# ---------------- kapasitet ----------------

def test_kapasitetsokning_rykker_opp_venteliste_og_kjorer_sveiper(con):
    kid = _kurs(con, kapasitet=1)
    db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    pb, _ = db.meld_paa(con, kid, epost="b@x.no", fornavn="B", etternavn="Test")  # venteliste
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(f"/admin/kurs/{kid}/oppsett", data=_oppsett_data(kapasitet="2"))
    fersk = _fersk(con)
    assert fersk.execute("SELECT status FROM paamelding WHERE id=?", (pb,)).fetchone()["status"] == "bekreftet"
    assert fersk.execute("SELECT 1 FROM utsending_logg WHERE mottaker='b@x.no' AND type='bekreftelse'").fetchone()
    assert fersk.execute("SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", (pb,)).fetchone()[0] == 1


def test_kapasitetsreduksjon_fjerner_ingen_og_setter_full(con):
    kid = _kurs(con, kapasitet=5)
    pa, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    pb, _ = db.meld_paa(con, kid, epost="b@x.no", fornavn="B", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(f"/admin/kurs/{kid}/oppsett", data=_oppsett_data(kapasitet="1"))
    fersk = _fersk(con)
    assert fersk.execute("SELECT status FROM paamelding WHERE id=?", (pa,)).fetchone()["status"] == "bekreftet"
    assert fersk.execute("SELECT status FROM paamelding WHERE id=?", (pb,)).fetchone()["status"] == "bekreftet"
    assert fersk.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()["status"] == "full"


def test_endre_kapasitet_er_idempotent_paa_samme_verdi(con):
    kid = _kurs(con, kapasitet=3)
    con.commit()
    assert db.endre_kapasitet(con, kid, 3) == []  # ingen endring -> ingenting skjer


# ---------------- status (utkast <-> aapen) ----------------

def test_utkast_til_aapen_og_avvist_manuell_full(con):
    kid = _kurs(con, status="utkast")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(f"/admin/kurs/{kid}/status", data={"status": "aapen"})
    assert _fersk(con).execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "aapen"
    with pytest.raises(db.Paameldingsfeil):
        db.sett_kurs_status(_fersk(con), kid, "full")


# ---------------- paameldingsfrist ----------------

def test_paamelding_fungerer_normalt_for_frist(con):
    kid = _kurs(con)
    con.execute("UPDATE kurs SET paameldingsfrist=? WHERE id=?", ((date.today() + timedelta(days=5)).isoformat(), kid))
    con.commit()
    pid, status = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test", idag=date.today())
    assert status == "bekreftet"


def test_paamelding_stoppes_etter_frist(con):
    kid = _kurs(con)
    con.execute("UPDATE kurs SET paameldingsfrist=? WHERE id=?", ((date.today() - timedelta(days=1)).isoformat(), kid))
    con.commit()
    with pytest.raises(db.Paameldingsfeil, match="frist"):
        db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test", idag=date.today())


def test_paamelding_stoppes_etter_frist_via_web(con):
    kid = _kurs(con, kode="WEB1")
    con.execute("UPDATE kurs SET paameldingsfrist=? WHERE id=?", ((date.today() - timedelta(days=1)).isoformat(), kid))
    con.commit()
    klient = _klient()
    r = klient.post("/kurs/WEB1", data={"fornavn": "A", "etternavn": "Test", "epost": "a@x.no", "samtykke": "on",
                                        "betaler": "person"})
    assert r.status_code == 400
    assert "frist" in r.get_data(as_text=True).lower()


def test_avlyst_utkast_og_passert_frist_blokkerer_alle(con):
    for status, frist in (("avlyst", None), ("utkast", None), ("aapen", (date.today() - timedelta(days=1)).isoformat())):
        kid = _kurs(con, kode=f"BLOKK-{status}-{frist}", status=status, paameldingsfrist=frist)
        con.commit()
        with pytest.raises(db.Paameldingsfeil):
            db.meld_paa(con, kid, epost=f"x-{status}@x.no", fornavn="X", etternavn="Test", idag=date.today())


def test_venteliste_kan_rykke_opp_administrativt_etter_passert_frist(con):
    kid = _kurs(con, kapasitet=1)
    db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    pb, _ = db.meld_paa(con, kid, epost="b@x.no", fornavn="B", etternavn="Test")
    con.execute("UPDATE kurs SET paameldingsfrist=? WHERE id=?", ((date.today() - timedelta(days=1)).isoformat(), kid))
    con.commit()
    # kapasitetsokning (administrativt opprykk) skal fungere selv om fristen er passert
    opprykket = db.endre_kapasitet(con, kid, 2, aktor="test")
    assert opprykket == [pb]
    assert con.execute("SELECT status FROM paamelding WHERE id=?", (pb,)).fetchone()["status"] == "bekreftet"


# ---------------- avlysning ----------------

def test_avlysning_setter_status_og_sender_varsel_en_gang(con):
    kid = _kurs(con, kapasitet=5)
    db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    db.meld_paa(con, kid, epost="b@x.no", fornavn="B", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(f"/admin/kurs/{kid}/avlys")
    klient.post(f"/admin/kurs/{kid}/avlys")  # forsok nummer to skal avvises paent (allerede avlyst)
    fersk = _fersk(con)
    assert fersk.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()["status"] == "avlyst"
    antall = fersk.execute("SELECT COUNT(*) FROM utsending_logg WHERE type='avlysning'").fetchone()[0]
    assert antall == 2  # en til hver av de to - ikke duplikater


def test_avlysning_rorer_ikke_eksisterende_paameldinger(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(f"/admin/kurs/{kid}/avlys")
    assert _fersk(con).execute("SELECT status FROM paamelding WHERE id=?", (pid,)).fetchone()["status"] == "bekreftet"


def test_avlyst_kurs_stopper_sveiper_ingen_bekreftelse_ingen_faktura(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")  # sveiper_kjort fortsatt 0
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kid,))
    con.commit()
    sveiper.kjor(Kjoring(con, idag=date.today()))
    assert not con.execute("SELECT 1 FROM utsending_logg WHERE type='bekreftelse'").fetchone()
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 0
    assert con.execute("SELECT sveiper_kjort FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == 0


def test_avlyst_kurs_stopper_delfaktura(con):
    kid = _kurs(con, betaling="per_samling")
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.execute("UPDATE paamelding SET sveiper_kjort=1 WHERE id=?", (pid,))
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kid,))
    con.commit()
    sveiper.forfalte_delfakturaer(Kjoring(con, idag=date.today()))
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 0


def test_avlyst_kurs_faar_ingen_innkallinger_eller_nytt_zoom(con):
    i_morgen = date.today() + timedelta(days=1)
    kid = _kurs(con, start=i_morgen, type="digital")
    db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kid,))
    con.commit()
    daglig.kjor(Kjoring(con, idag=date.today()))
    assert not con.execute("SELECT 1 FROM utsending_logg WHERE type='dagfor'").fetchone()
    assert con.execute("SELECT zoom_url FROM kurs WHERE id=?", (kid,)).fetchone()["zoom_url"] is None


def test_avlyst_kurs_faar_ingen_materiellpurring(con):
    kid = _kurs(con)
    con.execute("INSERT INTO materiell_krav (kurs_id, ansvarlig_navn, ansvarlig_epost, frist) VALUES (?,?,?,?)",
               (kid, "Holder", "holder@ipr.no", date.today().isoformat()))
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kid,))
    con.commit()
    daglig._purring(Kjoring(con, idag=date.today()))
    assert not con.execute("SELECT 1 FROM utsending_logg WHERE mottaker='holder@ipr.no'").fetchone()


def test_avlyst_kurs_rykker_ikke_opp_venteliste_ved_kapasitetsokning(con):
    kid = _kurs(con, kapasitet=1)
    db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    pb, _ = db.meld_paa(con, kid, epost="b@x.no", fornavn="B", etternavn="Test")
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kid,))
    con.commit()
    opprykket = db.endre_kapasitet(con, kid, 5, aktor="test")
    assert opprykket == []
    assert con.execute("SELECT status FROM paamelding WHERE id=?", (pb,)).fetchone()["status"] == "venteliste"


def test_avlyst_kurs_kan_ikke_bekrefte_via_statusendring(con):
    kid = _kurs(con, kapasitet=5)
    pb, _ = db.meld_paa(con, kid, epost="b@x.no", fornavn="B", etternavn="Test")
    con.execute("UPDATE paamelding SET status='venteliste' WHERE id=?", (pb,))
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kid,))
    con.commit()
    with pytest.raises(db.Paameldingsfeil):
        db.sett_paamelding_status(con, pb, "bekreftet")


def test_gjenaapning_setter_status_tilbake(con):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(f"/admin/kurs/{kid}/avlys")
    klient.post(f"/admin/kurs/{kid}/gjenaapne")
    assert _fersk(con).execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()["status"] == "aapen"


# ---------------- faktura_dager_for: server-side 0..180 (grensene inklusive) ----------------

def _dager_for(con, kid):
    return _fersk(con).execute("SELECT faktura_dager_for FROM kurs WHERE id=?", (kid,)).fetchone()[0]


def _post_oppsett(klient, kid, **over):
    return klient.post(f"/admin/kurs/{kid}/oppsett", data=_oppsett_data(**over), follow_redirects=True).get_data(as_text=True)


@pytest.mark.parametrize("verdi", [0, 1, 14, 179, 180])
def test_valider_faktura_dager_for_godtar_gyldige_verdier_inkludert_grensene(verdi):
    assert db.valider_faktura_dager_for(verdi) == verdi


@pytest.mark.parametrize("verdi", [-1, 181, 999, -180, -999999])
def test_valider_faktura_dager_for_avviser_utenfor_0_til_180(verdi):
    with pytest.raises(db.Paameldingsfeil):
        db.valider_faktura_dager_for(verdi)


@pytest.mark.parametrize("verdi", [True, False, 14.0, 14.5, "14", "abc", "", None])
def test_valider_faktura_dager_for_er_ikke_mer_liberal_enn_heltall(verdi):
    with pytest.raises(db.Paameldingsfeil):
        db.valider_faktura_dager_for(verdi)


# ---- opprettelse (db-funksjonen som ALLE opprettelsesveier bruker) ----

@pytest.mark.parametrize("verdi", [-1, 181, 999])
def test_opprett_kurs_avviser_ugyldig_faktura_dager_for_og_skriver_ingenting(con, verdi):
    with pytest.raises(db.Paameldingsfeil):
        db.opprett_kurs(con, kode="UG1", navn="Ugyldig", datoer=[date.today().isoformat()], sharepoint_mappe="K/UG1",
                        faktura_dager_for=verdi)
    assert con.execute("SELECT COUNT(*) FROM kurs").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM kursdag").fetchone()[0] == 0


@pytest.mark.parametrize("verdi", [0, 180])
def test_opprett_kurs_godtar_grensene(con, verdi):
    kid = db.opprett_kurs(con, kode="OK1", navn="Grense", datoer=[date.today().isoformat()], sharepoint_mappe="K/OK1",
                          faktura_dager_for=verdi)
    assert con.execute("SELECT faktura_dager_for FROM kurs WHERE id=?", (kid,)).fetchone()[0] == verdi


def test_opprett_kurs_uten_feltet_beholder_default_14(con):
    kid = db.opprett_kurs(con, kode="DF1", navn="Default", datoer=[date.today().isoformat()], sharepoint_mappe="K/DF1")
    assert con.execute("SELECT faktura_dager_for FROM kurs WHERE id=?", (kid,)).fetchone()[0] == 14


def _ny_kurs_data(**over):
    data = {"navn": "Nytt kurs", "type": "fysisk", "sted": "Bergen", "kapasitet": "",
            "datoer": (date.today() + timedelta(days=90)).isoformat(),
            "start_kl": "09:00", "slutt_kl": "16:00", "timer_pr_dag": "6", "pris_nok": "1000", "fakturering": "person",
            "betaling": "samlet", "faktura_dager_for": "14", "ansvarlig_admin_id": "", "paameldingsfrist": "",
            "kursholder_navn": "", "kursholder_epost": "", "materiell_frist": "", "notat": ""}
    data.update(over)
    return data


@pytest.mark.parametrize("verdi", ["-1", "181", "999"])
def test_ny_kurs_rute_avviser_ugyldig_faktura_dager_for_uten_kurs_eller_sharepoint_mappe(con, monkeypatch, verdi):
    from kurs.integrasjoner import sharepoint
    kall = []
    monkeypatch.setattr(sharepoint, "opprett_kursmappe", lambda kode: kall.append(kode) or f"Kurs/{kode}")
    klient = _klient()
    _logg_inn(klient)
    side = klient.post("/admin/kurs/ny", data=_ny_kurs_data(faktura_dager_for=verdi)).get_data(as_text=True)
    assert "Kunne ikke opprette kurs" in side and "mellom 0 og 180" in side
    assert _fersk(con).execute("SELECT COUNT(*) FROM kurs").fetchone()[0] == 0
    assert kall == []                                                             # ingen ekstern sideeffekt


@pytest.mark.parametrize("verdi", ["0", "180"])
def test_ny_kurs_rute_godtar_grensene(con, verdi):
    klient = _klient()
    _logg_inn(klient)
    klient.post("/admin/kurs/ny", data=_ny_kurs_data(faktura_dager_for=verdi))
    assert _fersk(con).execute("SELECT faktura_dager_for FROM kurs").fetchone()[0] == int(verdi)


def test_ny_kurs_rute_tomt_felt_gir_fortsatt_default_14_og_ikke_tall_avvises_som_for(con):
    klient = _klient()
    _logg_inn(klient)
    klient.post("/admin/kurs/ny", data=_ny_kurs_data(faktura_dager_for=""))
    assert _fersk(con).execute("SELECT faktura_dager_for FROM kurs").fetchone()[0] == 14
    for ugyldig in ("abc", "14.5"):
        klient.post("/admin/kurs/ny", data=_ny_kurs_data(faktura_dager_for=ugyldig, navn="Annet"))
    assert _fersk(con).execute("SELECT COUNT(*) FROM kurs").fetchone()[0] == 1


# ---- redigering: direkte POST kan ikke omgaa regelen ----

@pytest.mark.parametrize("verdi", ["-1", "181", "999"])
def test_oppsett_direkte_post_avviser_ugyldig_og_ingenting_oppdateres(con, verdi):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    side = _post_oppsett(klient, kid, faktura_dager_for=verdi, navn="Skal ikke lagres", notat="Heller ikke dette",
                         pris_nok="5000")
    assert "mellom 0 og 180" in side
    rad = _fersk(con).execute("SELECT navn, notat, pris_nok, faktura_dager_for FROM kurs WHERE id=?", (kid,)).fetchone()
    assert (rad["navn"], rad["notat"], rad["pris_nok"], rad["faktura_dager_for"]) == ("Testkurs", None, 1000, 14)


@pytest.mark.parametrize("verdi", [0, 180])
def test_oppsett_godtar_grensene(con, verdi):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    _post_oppsett(klient, kid, faktura_dager_for=str(verdi))
    assert _dager_for(con, kid) == verdi


def test_oppsett_tomt_felt_gir_default_14_og_ikke_numerisk_avvises_som_for(con):
    kid = _kurs(con, faktura_dager_for=30)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    for ugyldig in ("abc", "14.5"):
        side = _post_oppsett(klient, kid, faktura_dager_for=ugyldig, navn="Nei")
        assert "må være tall" in side
    assert _dager_for(con, kid) == 30                                             # uendret etter avvisning
    _post_oppsett(klient, kid, faktura_dager_for="")
    assert _dager_for(con, kid) == 14                                             # dagens semantikk: tomt = default


def test_db_oppdater_kurs_felter_avviser_ugyldig_faktura_dager_for_foer_noe_skrives(con):
    kid = _kurs(con)
    con.commit()
    for verdi in (-1, 181):
        with pytest.raises(db.Paameldingsfeil):
            db.oppdater_kurs_felter(con, kid, {"notat": "skal ikke lagres", "faktura_dager_for": verdi})
    r = con.execute("SELECT notat, faktura_dager_for FROM kurs WHERE id=?", (kid,)).fetchone()
    assert (r["notat"], r["faktura_dager_for"]) == (None, 14)
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='kurs_endret'").fetchone()[0] == 0


# ---- oekonomilaasen har fortsatt prioritet ----

@pytest.mark.parametrize("binding", ["faktura", "forsok"])
def test_gyldig_ny_verdi_kan_ikke_endres_naar_oekonomien_er_laast(con, binding):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    if binding == "faktura":
        con.execute("INSERT INTO faktura (paamelding_id, belop_nok, status) VALUES (?, 1000, 'sendt')", (pid,))
    else:
        con.execute("INSERT INTO faktura_forsok (paamelding_id, status) VALUES (?, 'ukjent')", (pid,))
    con.commit()
    assert db.har_okonomisk_binding_for_kurs(con, kid) is True
    klient = _klient()
    _logg_inn(klient)
    for verdi in ("0", "30", "180"):                                             # alle GYLDIGE - men laast
        _post_oppsett(klient, kid, faktura_dager_for=verdi, navn="Endret")
        assert _dager_for(con, kid) == 14
    assert _fersk(con).execute("SELECT navn FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "Endret"   # ulaaste felt fungerer
    assert db.oppdater_kurs_felter(con, kid, {"faktura_dager_for": 30}) == []                       # ogsaa paa db-nivaa


def test_ugyldig_verdi_avvises_ogsaa_naar_oekonomien_er_laast_og_omgaar_ikke_laasen(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.execute("INSERT INTO faktura_forsok (paamelding_id, status) VALUES (?, 'feilet')", (pid,))
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    side = _post_oppsett(klient, kid, faktura_dager_for="-1", navn="Skal ikke lagres")
    assert "mellom 0 og 180" in side
    rad = _fersk(con).execute("SELECT navn, faktura_dager_for FROM kurs WHERE id=?", (kid,)).fetchone()
    assert (rad["navn"], rad["faktura_dager_for"]) == ("Testkurs", 14)
    with pytest.raises(db.Paameldingsfeil):
        db.oppdater_kurs_felter(con, kid, {"faktura_dager_for": 181})
