"""Evaluering etter kurset (Camilla 05.10.2026, ny utgave 09.10.2026 etter Forms-skjemaet deres): spørsmål per kurs med standardsettet,
skala 1–6, anonyme svar, personlig lenke på e-post (én gang per runde) og delt lenke/QR-kode, sendt dagen etter siste kursdag eller
etter hver samling, og oversikten i fanen Evaluering. Oppdiktede data, ingen nettverk."""
import json
import re
from datetime import date, timedelta

import pytest

from kurs import config, db, evaluering, godkjenning
from kurs.kjoring import Kjoring

DAG1, DAG2, DAG3 = date(2099, 3, 2), date(2099, 3, 3), date(2099, 4, 6)
SISTE = DAG2


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _admin(brukernavn=None, passord=None):
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": brukernavn or config.ADMIN_BRUKERNAVN, "passord": passord or config.ADMIN_PASSORD})
    return k


def _klient():
    from kurs.web import app as webapp
    return webapp.app.test_client()


def _kurs(con, datoer=(DAG1, DAG2), **kw):
    kid = db.opprett_kurs(con, kode="EV1", navn="Evalueringskurs", datoer=[d.isoformat() for d in datoer],
                          sharepoint_mappe="K/EV1", pris_nok=0, **{"type": "fysisk", "sted": "Oslo", **kw})
    pids = [db.meld_paa(con, kid, epost=f"d{n}@eksempel.no", fornavn=f"D{n}", etternavn="Test")[0] for n in (1, 2)]
    con.commit()
    return kid, pids


def _kursrad(con, kid):
    return con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()


def _send(con, idag):
    evaluering.kjor(Kjoring(con, idag=idag))
    con.commit()


def _sendt(con, nokkel):
    return con.execute("SELECT COUNT(*) FROM utsending_logg WHERE nokkel=? AND type='evaluering' AND status='sendt'",
                       (nokkel,)).fetchone()[0]


def _gyldige_svar(**over):
    data = {"tilfreds": "6", "formidling": "5", "faglig": "6", "lokale": "4", "forslag": "Mer tid til øvelser"}
    data.update(over)
    return data


# ============================ utsending ============================

def test_sendes_dagen_etter_siste_kursdag_og_bare_en_gang(con):
    kid, _ = _kurs(con)
    _send(con, SISTE)
    assert _sendt(con, f"evaluering:{kid}") == 0
    _send(con, SISTE + timedelta(days=1))
    _send(con, SISTE + timedelta(days=2))
    assert _sendt(con, f"evaluering:{kid}") == 2


def test_gamle_kurs_og_kurs_med_evaluering_av_faar_ingenting(con):
    kid, _ = _kurs(con)
    _send(con, SISTE + timedelta(days=evaluering.DAGER_ETTER + 1))
    assert _sendt(con, f"evaluering:{kid}") == 0
    evaluering.sett_modus(con, kid, "av", aktor="admin:test")
    _send(con, SISTE + timedelta(days=1))
    assert _sendt(con, f"evaluering:{kid}") == 0


def test_etter_hver_samling(con):
    kid, (a, b) = _kurs(con, datoer=(DAG1, DAG2, DAG3))      # to samlinger: 2.–3. mars og 6. april
    evaluering.sett_modus(con, kid, "samling", aktor="admin:test")
    con.commit()
    runder = evaluering.runder(con, _kursrad(con, kid))
    assert [r["til"] for r in runder] == [DAG2, DAG3] and all(r["samling_id"] for r in runder)
    s1, s2 = (r["samling_id"] for r in runder)
    _send(con, DAG2 + timedelta(days=1))
    assert _sendt(con, f"evaluering:{kid}:s{s1}") == 2 and _sendt(con, f"evaluering:{kid}:s{s2}") == 0
    _send(con, DAG3 + timedelta(days=1))
    assert _sendt(con, f"evaluering:{kid}:s{s2}") == 2 and _sendt(con, f"evaluering:{kid}") == 0
    # lenken gjelder samlingen, og hver samling kan besvares én gang
    tok = evaluering.token(a, s1)
    assert evaluering.les_token(tok) == (a, s1) and evaluering.les_token(evaluering.token(a)) == (a, None)
    k = _klient()
    side = k.get(f"/evaluering/{tok}").get_data(as_text=True)
    assert "Evalueringskurs, samling 1" in side and "Hvor tilfreds er du med samlingen i sin helhet?" in side
    assert k.post(f"/evaluering/{tok}", data=_gyldige_svar()).status_code == 200
    assert "allerede svart" in k.post(f"/evaluering/{tok}", data=_gyldige_svar()).get_data(as_text=True)
    assert k.post(f"/evaluering/{evaluering.token(a, s2)}", data=_gyldige_svar(tilfreds="3")).status_code == 200
    rader = con.execute("SELECT samling_id FROM evaluering_svar WHERE kurs_id=? ORDER BY samling_id", (kid,)).fetchall()
    assert [r["samling_id"] for r in rader] == [s1, s2]
    # fristen i «Klar til sending» regnes fra samlingens siste dag
    assert godkjenning.utloper(con, "evaluering", f"evaluering:{kid}:s{s1}", a, kid) == (DAG2 + timedelta(days=14)).isoformat()


def test_e_posten_har_personlig_lenke(con, monkeypatch):
    kid, pids = _kurs(con)
    _send(con, SISTE + timedelta(days=1))
    html = con.execute("SELECT html FROM sendt_epost WHERE type='evaluering' ORDER BY id LIMIT 1").fetchone()
    tekst = html["html"] if html else ""
    assert "/evaluering/" in tekst or "skjult" in tekst      # lenken er personlig (kan være skjult i kopien)


# ============================ skjemaet og svarene ============================

def test_skjemaet_har_forms_oppsettet_og_standardspoersmaalene(con):
    kid, (a, _) = _kurs(con)
    side = _klient().get(f"/evaluering/{evaluering.token(a)}").get_data(as_text=True)
    assert "<h1>Evalueringskurs</h1>" in side and "Internnummer" in side and "Ved å svare på evalueringen samtykker du" in side
    for tekst in ("Hvor tilfreds er du med kurset i sin helhet?", "Hvordan vil du rangere kursholders formidlingsevne?",
                  "1 er lavest/dårligst, 6 er høyest/best", "Har du en tilbakemelding til kursavdelingen?",
                  "Har du lyst å gi oss en tilbakemelding som vi kan bruke anonymt i markedsføring?", 'placeholder="Skriv inn svaret"'):
        assert tekst in side, tekst
    assert side.count('name="tilfreds"') == 6                  # skala 1–6
    assert len(evaluering.STANDARD) == 11 and "EFT 2" not in side          # EFT 2. år legges til der det passer


def test_svaret_er_anonymt_og_kan_bare_gis_en_gang(con):
    kid, (a, _) = _kurs(con)
    k = _klient()
    r = k.post(f"/evaluering/{evaluering.token(a)}", data={**_gyldige_svar(), "startet": "0"})
    assert r.status_code == 200 and "Takk for svaret" in r.get_data(as_text=True)
    rad = con.execute("SELECT * FROM evaluering_svar").fetchone()
    assert json.loads(rad["svar"]) == {"tilfreds": 6, "formidling": 5, "faglig": 6, "lokale": 4, "forslag": "Mer tid til øvelser"}
    assert rad["kilde"] == "epost" and rad["sekunder"] is None and set(rad.keys()) == {
        "id", "kurs_id", "besvart", "svar", "samling_id", "kilde", "sekunder"}               # ingen kobling til hvem
    assert "allerede svart" in k.post(f"/evaluering/{evaluering.token(a)}", data=_gyldige_svar()).get_data(as_text=True)
    assert con.execute("SELECT COUNT(*) FROM evaluering_svar").fetchone()[0] == 1


@pytest.mark.parametrize("data", [{}, {"tilfreds": "7"}, {"tilfreds": "0"}, {"forslag": "   "}])
def test_ugyldige_eller_tomme_svar_avvises(con, data):
    kid, (a, _) = _kurs(con)
    assert _klient().post(f"/evaluering/{evaluering.token(a)}", data=data).status_code == 400
    assert con.execute("SELECT COUNT(*) FROM evaluering_svar").fetchone()[0] == 0


@pytest.mark.parametrize("token", ["1.abc", "999.0123456789abcdef0123456789abcdef", "x", "1.s2.0123456789abcdef0123456789abcdef"])
def test_forfalsket_lenke_virker_ikke(con, token):
    _kurs(con)
    assert _klient().get(f"/evaluering/{token}").status_code == 404


def test_paakrevd_sporsmal_maa_besvares(con):
    kid, (a, _) = _kurs(con)
    k = _admin()
    k.post(f"/admin/kurs/{kid}/evaluering/sporsmal", data={"ferdig": "eft2"})
    s = next(x for x in evaluering.sporsmal(con, _kursrad(con, kid)) if x["tekst"].startswith("Har du planer"))
    assert s["paakrevd"] and s["valg"] == ["Ja", "Nei", "Har ikke bestemt meg enda"]
    r = _klient().post(f"/evaluering/{evaluering.token(a)}", data=_gyldige_svar())
    assert r.status_code == 400 and "Svar på spørsmål 12" in r.get_data(as_text=True)
    assert _klient().post(f"/evaluering/{evaluering.token(a)}", data=_gyldige_svar(**{s["nokkel"]: "Ja"})).status_code == 200


def test_digitale_kurs_faar_zoom_sporsmaalet(con):
    kid, (a, _) = _kurs(con, type="digital", sted=None)
    side = _klient().get(f"/evaluering/{evaluering.token(a)}").get_data(as_text=True)
    assert "Hvordan opplevde du den tekniske gjennomføringen (Zoom)?" in side and "lokalet og omgivelsene" not in side


# ============================ den delte lenken ============================

def test_delt_lenke_kan_brukes_flere_ganger_og_kan_byttes(con):
    kid, _ = _kurs(con)
    kurs = _kursrad(con, kid)
    adresse = evaluering.delt_lenke(con, kurs).split(config.BASE_URL, 1)[1]
    k = _klient()
    assert "Hvor tilfreds" in k.get(adresse).get_data(as_text=True)
    for _ in range(2):
        assert k.post(adresse, data={**_gyldige_svar(), "startet": "1"}).status_code == 200
    assert [r["kilde"] for r in con.execute("SELECT kilde FROM evaluering_svar")] == ["delt", "delt"]
    _admin().post(f"/admin/kurs/{kid}/evaluering/ny-lenke")
    assert k.get(adresse).status_code == 404                      # den gamle lenken virker ikke lenger
    ny = evaluering.delt_lenke(con, _kursrad(con, kid)).split(config.BASE_URL, 1)[1]
    assert ny != adresse and k.get(ny).status_code == 200
    assert k.get("/evaluering/delt/k1.0.0.0123456789abcdef0123456789abcdef").status_code == 404


def test_stengt_evaluering_tar_ikke_imot_svar(con):
    kid, (a, _) = _kurs(con)
    _admin().post(f"/admin/kurs/{kid}/evaluering/stengt", data={"stengt": "1"})
    adresse = evaluering.delt_lenke(con, _kursrad(con, kid)).split(config.BASE_URL, 1)[1]
    for url in (f"/evaluering/{evaluering.token(a)}", adresse):
        assert "Evalueringen er stengt" in _klient().get(url).get_data(as_text=True)
        _klient().post(url, data=_gyldige_svar())
    assert con.execute("SELECT COUNT(*) FROM evaluering_svar").fetchone()[0] == 0


def test_qr_koden(con):
    kid, _ = _kurs(con)
    side = _admin().get(f"/admin/kurs/{kid}/evaluering/qr").get_data(as_text=True)
    assert "<svg" in side and "/evaluering/delt/k" in side and "Skann koden" in side


# ============================ spørsmålene i fanen ============================

def test_spoersmaalene_kan_endres_flyttes_og_slettes(con):
    kid, _ = _kurs(con)
    k = _admin()
    assert "(standardsettet)" in k.get(f"/admin/kurs/{kid}/evaluering").get_data(as_text=True)
    r = k.post(f"/admin/kurs/{kid}/evaluering/sporsmal/forslag", data={"type": "tekst", "tekst": "Hva kan vi gjøre bedre?"})
    assert r.status_code == 302
    kurs = _kursrad(con, kid)
    assert evaluering.har_egne_sporsmal(con, kid) and len(evaluering.sporsmal(con, kurs)) == 11
    assert evaluering.sporsmal(con, kurs)[6]["tekst"] == "Hva kan vi gjøre bedre?"
    k.post(f"/admin/kurs/{kid}/evaluering/sporsmal/forslag/flytt", data={"retning": "opp"})
    assert evaluering.sporsmal(con, kurs)[5]["nokkel"] == "forslag"
    k.post(f"/admin/kurs/{kid}/evaluering/sporsmal/markedsforing/slett")
    assert "markedsforing" not in [s["nokkel"] for s in evaluering.sporsmal(con, kurs)]
    r = k.post(f"/admin/kurs/{kid}/evaluering/sporsmal", data={"type": "valg", "tekst": "Hvor fikk du høre om kurset?",
                                                             "valg": "Nettsiden\nKollega\nNettsiden", "paakrevd": "1"})
    ny = evaluering.sporsmal(con, kurs)[-1]
    assert r.status_code == 302 and ny["valg"] == ["Nettsiden", "Kollega"] and ny["paakrevd"] and ny["nokkel"] == "s1"


def test_ugyldig_spoersmaal_avvises(con):
    kid, _ = _kurs(con)
    k = _admin()
    for data, feil in (({"type": "valg", "tekst": "X", "valg": "Bare ett"}, "mellom 2 og"),
                       ({"type": "skala", "tekst": ""}, "Skriv spørsmålet"),
                       ({"type": "tekst", "tekst": "X", "lenke": "javascript:alert(1)"}, "https://")):
        r = k.post(f"/admin/kurs/{kid}/evaluering/sporsmal", data=data)
        assert r.status_code == 400 and feil in r.get_data(as_text=True), data
    assert not evaluering.har_egne_sporsmal(con, kid)


def test_besvarte_spoersmaal_er_laast(con):
    kid, (a, _) = _kurs(con)
    _klient().post(f"/evaluering/{evaluering.token(a)}", data=_gyldige_svar())
    k = _admin()
    r = k.post(f"/admin/kurs/{kid}/evaluering/sporsmal/tilfreds", data={"type": "tekst", "tekst": "Ny type"})
    assert r.status_code == 400 and "kan ikke bytte type" in r.get_data(as_text=True)
    r = k.post(f"/admin/kurs/{kid}/evaluering/sporsmal/tilfreds/slett")
    assert r.status_code == 400 and "kan ikke slettes" in r.get_data(as_text=True)
    assert k.post(f"/admin/kurs/{kid}/evaluering/sporsmal/tilfreds",
                  data={"type": "skala", "tekst": "Hvor fornøyd er du alt i alt?"}).status_code == 302     # teksten kan rettes
    assert k.post(f"/admin/kurs/{kid}/evaluering/standard").status_code == 400


def test_spoersmaalene_folger_med_naar_kurset_kopieres(con):
    kid, _ = _kurs(con)
    _admin().post(f"/admin/kurs/{kid}/evaluering/sporsmal", data={"ferdig": "eft2"})
    evaluering.sett_modus(con, kid, "samling", aktor="admin:test")
    ny = db.opprett_kurs(con, kode="EV2", navn="Kopi", datoer=[DAG1.isoformat()], sharepoint_mappe="K/EV2", pris_nok=0)
    evaluering.kopier(con, kid, ny)
    kopi = _kursrad(con, ny)
    assert len(evaluering.sporsmal(con, kopi)) == 12 and evaluering.modus(kopi) == "samling"


def test_lesetilgang_ser_men_kan_ikke_endre_eller_laste_ned(con):
    kid, _ = _kurs(con)
    db.opprett_admin_bruker(con, "leser", "Leser", "passord-som-holder", rolle="lese")
    con.commit()
    lese = _admin("leser", "passord-som-holder")
    side = lese.get(f"/admin/kurs/{kid}/evaluering").get_data(as_text=True)
    assert "Lenke du kan dele" in side and "Legg til spørsmål" not in side
    assert lese.post(f"/admin/kurs/{kid}/evaluering/sporsmal", data={"ferdig": "eft2"}).status_code == 403
    assert lese.get(f"/admin/kurs/{kid}/evaluering.csv").status_code == 403


# ============================ oversikten ============================

def test_oversikten(con):
    kid, (a, b) = _kurs(con)
    _send(con, SISTE + timedelta(days=1))
    k = _klient()
    k.post(f"/evaluering/{evaluering.token(a)}", data=_gyldige_svar(tilfreds="6"))
    k.post(f"/evaluering/{evaluering.token(b)}", data=_gyldige_svar(tilfreds="5", forslag="Bedre kaffe"))
    k.post(evaluering.delt_lenke(con, _kursrad(con, kid)).split(config.BASE_URL, 1)[1], data=_gyldige_svar(tilfreds="4"))
    res = evaluering.resultater(con, _kursrad(con, kid), None, SISTE + timedelta(days=3))
    assert (res["antall_svar"], res["via_epost"], res["via_delt"], res["invitert"], res["svarprosent"]) == (3, 2, 1, 2, 100)
    tilfreds = res["sporsmal"][0]
    assert tilfreds["snitt"] == 5.0 and tilfreds["fordeling"][6] == 1 and tilfreds["fordeling"][4] == 1
    forslag = next(s for s in res["sporsmal"] if s["nokkel"] == "forslag")
    assert forslag["tekster"] == ["Bedre kaffe", "Mer tid til øvelser", "Mer tid til øvelser"]
    side = _admin().get(f"/admin/kurs/{kid}/evaluering").get_data(as_text=True)
    for tekst in ("Svarprosent", "100 %", "Gjennomsnittstid", "Nivå 6", "5,00", "Gjennomsnittlig vurdering", "1 via delt lenke"):
        assert tekst in side, tekst
    csv = _admin().get(f"/admin/kurs/{kid}/evaluering.csv")
    assert csv.status_code == 200 and "1. Hvor tilfreds er du med kurset i sin helhet?" in csv.get_data(as_text=True)


def test_tid_brukt_regnes_fra_siden_ble_vist():
    assert evaluering.sekunder_brukt("1000", 1580) == 580
    assert evaluering.sekunder_brukt("1000", 999) is None and evaluering.sekunder_brukt("abc", 5) is None
    assert evaluering.sekunder_brukt("0", 10 ** 9) is None            # siden sto åpen i dagevis
    assert evaluering.tid_tekst(580) == "09:40" and evaluering.tid_tekst(None) == "–"


def test_oversikten_per_samling(con):
    kid, (a, _) = _kurs(con, datoer=(DAG1, DAG2, DAG3))
    evaluering.sett_modus(con, kid, "samling", aktor="admin:test")
    con.commit()
    s1, s2 = (r["samling_id"] for r in evaluering.runder(con, _kursrad(con, kid)))
    _klient().post(f"/evaluering/{evaluering.token(a, s1)}", data=_gyldige_svar(tilfreds="6"))
    _klient().post(f"/evaluering/{evaluering.token(a, s2)}", data=_gyldige_svar(tilfreds="2"))
    kurs = _kursrad(con, kid)
    assert evaluering.resultater(con, kurs, s1)["sporsmal"][0]["snitt"] == 6.0
    assert evaluering.resultater(con, kurs, evaluering.ALLE)["sporsmal"][0]["snitt"] == 4.0
    side = _admin().get(f"/admin/kurs/{kid}/evaluering?runde={s2}").get_data(as_text=True)
    assert "Alle samlinger" in side and re.search(r"2,00", side)


# ============================ rettinger fra gjennomgangen 09.10.2026 ============================

def test_personlig_lenke_skjules_i_kopien_og_lages_paa_nytt(con):
    from kurs.integrasjoner import epost
    kid, (a, _) = _kurs(con)
    _send(con, SISTE + timedelta(days=1))
    tok = evaluering.token(a)
    kopi = con.execute("SELECT html FROM sendt_epost WHERE type='evaluering' AND paamelding_id=?", (a,)).fetchone()["html"]
    assert tok not in kopi and f"{config.BASE_URL}/evaluering/skjult" in kopi       # ingen kan se hvem som har svart
    (ut,) = [m for m in epost.les_utboks() if m["til"] == "d1@eksempel.no"]
    assert tok in ut["html"]                                                         # e-posten som gikk ut, har lenken
    # i «Klar til sending»: skjult i køen og laget på nytt for riktig samling når e-posten sendes
    s_tok = evaluering.token(a, 7)
    delt = evaluering.delt_lenke(con, _kursrad(con, kid))
    skjult = godkjenning.skjul_lenker(f'<a href="{config.BASE_URL}/evaluering/{s_tok}">Svar</a> {delt}')
    assert s_tok not in skjult and delt in skjult                                    # den delte lenken er ikke personlig
    k = Kjoring(con, idag=SISTE)
    assert f"{config.BASE_URL}/evaluering/{s_tok}" in godkjenning.gjenopprett_lenker(
        k, {"paamelding_id": a, "nokkel": f"evaluering:{kid}:s7"}, skjult)
    with pytest.raises(ValueError):
        godkjenning.gjenopprett_lenker(k, {"paamelding_id": None, "nokkel": f"evaluering:{kid}"}, skjult)


def test_en_hel_klasse_kan_svare_via_qr_koden(con):
    kid, _ = _kurs(con)
    url = evaluering.delt_lenke(con, _kursrad(con, kid)).split(config.BASE_URL, 1)[1]
    k = _klient()                       # alle fra samme nett (samme IP)
    for _ in range(40):
        assert k.post(url, data=_gyldige_svar()).status_code == 200
    assert evaluering.resultater(con, _kursrad(con, kid), evaluering.ALLE)["antall_svar"] == 40


def test_excel_filen_gjor_formler_ufarlige(con):
    kid, _ = _kurs(con)
    url = evaluering.delt_lenke(con, _kursrad(con, kid)).split(config.BASE_URL, 1)[1]
    _klient().post(url, data=_gyldige_svar(forslag="=SUM(A1:A2)"))
    csv = _admin().get(f"/admin/kurs/{kid}/evaluering.csv").get_data(as_text=True)
    assert "'=SUM(A1:A2)" in csv and ";=SUM" not in csv


def test_svarene_vises_etter_bytte_av_naar_evalueringen_sendes(con):
    kid, (a, _) = _kurs(con, datoer=(DAG1, DAG2, DAG3))
    evaluering.sett_modus(con, kid, "samling", aktor="admin:test")
    con.commit()
    s1 = evaluering.runder(con, _kursrad(con, kid))[0]["samling_id"]
    _klient().post(f"/evaluering/{evaluering.token(a, s1)}", data=_gyldige_svar(forslag="Svar etter samling 1"))
    evaluering.sett_modus(con, kid, "slutt", aktor="admin:test")
    con.commit()
    side = _admin().get(f"/admin/kurs/{kid}/evaluering").get_data(as_text=True)
    assert "Svar etter samling 1" in side
    assert "Svar etter samling 1" in _admin().get(f"/admin/kurs/{kid}/evaluering.csv").get_data(as_text=True)


def test_stengt_evaluering_sendes_ikke(con):
    kid, _ = _kurs(con)
    evaluering.sett_stengt(con, kid, True, aktor="admin:test")
    con.commit()
    _send(con, SISTE + timedelta(days=1))
    assert _sendt(con, f"evaluering:{kid}") == 0


def test_bytte_av_naar_gir_ingen_to_evalueringer(con):
    kid, _ = _kurs(con, datoer=(DAG1, DAG2, DAG3))
    evaluering.sett_modus(con, kid, "samling", aktor="admin:test")
    con.commit()
    s2 = evaluering.runder(con, _kursrad(con, kid))[1]["samling_id"]
    _send(con, DAG3 + timedelta(days=1))                     # etter siste samling
    assert _sendt(con, f"evaluering:{kid}:s{s2}") == 2
    evaluering.sett_modus(con, kid, "slutt", aktor="admin:test")
    con.commit()
    _send(con, DAG3 + timedelta(days=2))
    assert _sendt(con, f"evaluering:{kid}") == 0              # de fikk nettopp evalueringen etter siste samling


def test_bytte_av_naar_gir_ingen_to_evalueringer_ogsaa_motsatt_vei(con):
    kid, _ = _kurs(con, datoer=(DAG1, DAG2, DAG3))
    _send(con, DAG3 + timedelta(days=1))                     # etter siste kursdag (hele kurset)
    assert _sendt(con, f"evaluering:{kid}") == 2
    evaluering.sett_modus(con, kid, "samling", aktor="admin:test")
    con.commit()
    s2 = evaluering.runder(con, _kursrad(con, kid))[1]["samling_id"]
    _send(con, DAG3 + timedelta(days=2))
    assert _sendt(con, f"evaluering:{kid}:s{s2}") == 0


def test_evalueringssiden_viser_kontakten_til_merket(con):
    kid, (a, _) = _kurs(con)
    con.execute("UPDATE kurs SET merke='terapiakademiet' WHERE id=?", (kid,))
    con.commit()
    side = _klient().get(f"/evaluering/{evaluering.token(a)}").get_data(as_text=True)
    assert config.TERAPIAKADEMIET_EPOST in side and "kurs@ipr.no" not in side


# ============================ «har svart» uten påmelding og resultatlenken til kursholderne (09.10.2026) ============================

def test_har_svart_lagres_uten_paamelding_og_dato(con):
    kid, (a, b) = _kurs(con)
    k = _klient()
    assert k.post(f"/evaluering/{evaluering.token(a)}", data=_gyldige_svar()).status_code == 200
    assert con.execute("SELECT COUNT(*) FROM evaluering_besvart").fetchone()[0] == 0          # ikke lenger med påmeldingen
    (rad,) = con.execute("SELECT * FROM evaluering_besvart_kode").fetchall()
    assert rad["kurs_id"] == kid and set(rad.keys()) == {"kode", "kurs_id"} and str(a) not in rad["kode"]
    assert "allerede svart" in k.post(f"/evaluering/{evaluering.token(a)}", data=_gyldige_svar()).get_data(as_text=True)
    assert con.execute("SELECT COUNT(*) FROM evaluering_svar").fetchone()[0] == 1
    # svar gitt før endringen (med påmeldingen i den gamle tabellen) gjelder fortsatt
    assert not evaluering.har_svart(con, b)
    con.execute("INSERT INTO evaluering_besvart (paamelding_id, kurs_id, besvart) VALUES (?,?,?)", (b, kid, "2099-03-04"))
    con.commit()
    assert evaluering.har_svart(con, b)


def test_resultatlenken_viser_oversikten_uten_innlogging(con):
    kid, (a, _) = _kurs(con)
    _klient().post(f"/evaluering/{evaluering.token(a)}", data=_gyldige_svar(forslag="Mer rollespill"))
    adminside = _admin().get(f"/admin/kurs/{kid}/evaluering").get_data(as_text=True)
    lenke = evaluering.resultat_lenke(con, _kursrad(con, kid))
    assert "Del resultatene med kursholderne" in adminside and lenke in adminside
    sti = lenke.split(config.BASE_URL, 1)[1]
    r = _klient().get(sti)
    side = r.get_data(as_text=True)
    assert r.status_code == 200 and "Evalueringskurs" in side and "Mer rollespill" in side and "Gjennomsnittlig vurdering" in side
    assert r.headers["Cache-Control"] == "no-store" and "noindex" in r.headers["X-Robots-Tag"]
    assert "Last ned (Excel)" not in side and "Steng lenken" not in side and "/admin/kurs/" not in side     # bare lesing
    # stengt, åpnet igjen og ny lenke
    _admin().post(f"/admin/kurs/{kid}/evaluering/resultatlenke/stengt", data={"stengt": "1"})
    assert _klient().get(sti).status_code == 404
    _admin().post(f"/admin/kurs/{kid}/evaluering/resultatlenke/stengt", data={"stengt": "0"})
    assert _klient().get(sti).status_code == 200
    _admin().post(f"/admin/kurs/{kid}/evaluering/resultatlenke/ny")
    assert _klient().get(sti).status_code == 404
    ny = evaluering.resultat_lenke(con, _kursrad(con, kid)).split(config.BASE_URL, 1)[1]
    assert ny != sti and _klient().get(ny).status_code == 200
    # forfalsket lenke
    assert _klient().get(sti[:-4] + "0000").status_code == 404
    assert evaluering.les_resultat_token("r1.0.0." + "a" * 32) is None


def test_resultatlenken_for_en_samling_viser_bare_den(con):
    kid, (a, _) = _kurs(con, datoer=(DAG1, DAG2, DAG3))
    evaluering.sett_modus(con, kid, "samling", aktor="admin:test")
    con.commit()
    s1, s2 = (r["samling_id"] for r in evaluering.runder(con, _kursrad(con, kid)))
    _klient().post(f"/evaluering/{evaluering.token(a, s1)}", data=_gyldige_svar(forslag="Om samling 1"))
    _klient().post(f"/evaluering/{evaluering.token(a, s2)}", data=_gyldige_svar(forslag="Om samling 2"))
    adminside = _admin().get(f"/admin/kurs/{kid}/evaluering?runde={s2}").get_data(as_text=True)
    lenke = evaluering.resultat_lenke(con, _kursrad(con, kid), s2)
    assert lenke in adminside
    side = _klient().get(lenke.split(config.BASE_URL, 1)[1]).get_data(as_text=True)
    assert "Om samling 2" in side and "Om samling 1" not in side
    alle = _klient().get(evaluering.resultat_lenke(con, _kursrad(con, kid)).split(config.BASE_URL, 1)[1]).get_data(as_text=True)
    assert "Om samling 1" in alle and "Om samling 2" in alle


def test_lesetilgang_kan_ikke_stenge_eller_bytte_resultatlenken(con):
    kid, _ = _kurs(con)
    db.opprett_admin_bruker(con, "leser", "Leser", "passord-som-holder", rolle="lese")
    con.commit()
    lese = _admin("leser", "passord-som-holder")
    assert "Del resultatene med kursholderne" in lese.get(f"/admin/kurs/{kid}/evaluering").get_data(as_text=True)
    assert lese.post(f"/admin/kurs/{kid}/evaluering/resultatlenke/ny").status_code == 403
    assert lese.post(f"/admin/kurs/{kid}/evaluering/resultatlenke/stengt", data={"stengt": "1"}).status_code == 403
