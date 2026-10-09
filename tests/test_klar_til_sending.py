"""«Klar til sending» (Camilla 05.10.2026, kurs/godkjenning.py): automatiske e-poster utenom bekreftelse og venteliste venter på
godkjenning før de sendes. Personlige lenker lagres aldri i køen. Oppdiktede data, ingen nettverk."""
from datetime import date, timedelta

import pytest

from kurs import config, daglig, db, evaluering, godkjenning, lenker
from kurs.integrasjoner import epost
from kurs.kjoring import Kjoring

START = date(2099, 3, 2)
SISTE = date(2099, 3, 3)


@pytest.fixture(autouse=True)
def _godkjenning_paa(monkeypatch):
    monkeypatch.setattr(config, "GODKJENN_EPOSTER", True)     # conftest slår den av for de eldre testene


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
    """Alle e-poster som faktisk går ut: [(til, emne, html)]."""
    ut = []
    monkeypatch.setattr(epost, "send", lambda til, emne, html, vedlegg=(): ut.append((til, emne, html)))
    return ut


def _kurs(con, kode="KS1"):
    kid = db.opprett_kurs(con, kode=kode, navn="Godkjenningskurs", datoer=[START.isoformat(), SISTE.isoformat()],
                          sharepoint_mappe=f"K/{kode}", pris_nok=0, type="fysisk", sted="Oslo")
    pids = [db.meld_paa(con, kid, epost=f"g{n}@eksempel.no", fornavn=f"G{n}", etternavn="Test")[0] for n in (1, 2)]
    con.commit()
    return kid, pids


def _koe(con, type_=None):
    sql, arg = "SELECT * FROM epost_godkjenning", ()
    if type_:
        sql, arg = sql + " WHERE type=?", (type_,)
    return con.execute(sql + " ORDER BY id", arg).fetchall()


def _sendte_typer(con) -> list[str]:
    return sorted(r["type"] for r in con.execute("SELECT type FROM utsending_logg WHERE status='sendt'"))


def _admin():
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


# ============================ hvilke e-poster venter ============================

def test_bare_bekreftelse_og_venteliste_og_interne_e_poster_gaar_automatisk(monkeypatch):
    # (06.10.2026: kursholder-lenken og SharePoint er tatt bort, og purringene til kursholdere og «eskalering» med dem)
    for t in ("avlysning", "ukefor", "dagfor-2099-03-02", "kursbevis", "evaluering", "firmapaamelding_kvittering"):
        assert godkjenning.krever_godkjenning(t), t
    for t in ("bekreftelse", "venteliste", "admin_epost", "sjekkliste-paaminnelse", "avslag", "innlogging"):
        assert not godkjenning.krever_godkjenning(t), t
    monkeypatch.setattr(config, "GODKJENN_EPOSTER", False)
    assert not godkjenning.krever_godkjenning("ukefor")


def test_evalueringens_frist_er_den_samme_som_utsendingsvinduet():
    assert godkjenning.EVALUERING_DAGER == evaluering.DAGER_ETTER


# ============================ morgenjobben ============================

def test_morgenjobben_sender_bekreftelsen_men_legger_paaminnelsen_i_koe(con, sendt):
    _kurs(con)
    daglig.kjor(Kjoring(con, idag=START - timedelta(days=5)))
    assert _sendte_typer(con) == ["bekreftelse", "bekreftelse"]
    koe = _koe(con, "ukefor")
    assert len(koe) == 2 and {r["status"] for r in koe} == {"venter"}
    assert not [s for s in sendt if "g1@" in s[0] and s[1] == koe[0]["emne"]]
    assert all(r["utloper"] == (START - timedelta(days=1)).isoformat() for r in koe)


def test_morgenjobben_to_ganger_samme_dag_legger_ingenting_nytt_i_koen(con, sendt):
    _kurs(con)
    daglig.kjor(Kjoring(con, idag=START - timedelta(days=5)))
    for_ = (len(_koe(con)), len(sendt))
    daglig.kjor(Kjoring(con, idag=START - timedelta(days=5)))
    daglig.kjor(Kjoring(con, idag=START - timedelta(days=4)))
    assert (len(_koe(con)), len(sendt)) == for_


def test_torrkjoring_lagrer_ingenting_i_koen(con, sendt):
    _kurs(con)
    daglig.kjor(Kjoring(con, idag=START - timedelta(days=5), tor=True))
    con.rollback()
    assert _koe(con) == [] and sendt == []


def test_uten_godkjenning_sendes_paaminnelsen_med_en_gang(con, sendt, monkeypatch):
    monkeypatch.setattr(config, "GODKJENN_EPOSTER", False)
    _kurs(con)
    daglig.kjor(Kjoring(con, idag=START - timedelta(days=5)))
    assert _sendte_typer(con).count("ukefor") == 2 and _koe(con) == []


# ============================ godkjenne ============================

def test_godkjent_e_post_sendes_en_gang_og_innholdet_i_koen_slettes(con, sendt):
    _kurs(con)
    idag = START - timedelta(days=5)
    daglig.kjor(Kjoring(con, idag=idag))
    ider = [r["id"] for r in _koe(con, "ukefor")]
    foer = len(sendt)
    ut = godkjenning.send(Kjoring(con, idag=idag, aktor="admin:test"), ider)
    assert ut == {"sendt": 2, "utgaatt": 0, "uavklart": 0, "ikke_forsokt": 0}
    assert len(sendt) == foer + 2
    assert _sendte_typer(con).count("ukefor") == 2
    assert all(r["status"] == "sendt" and r["html"] is None and r["behandlet_av"] == "admin:test" for r in _koe(con, "ukefor"))
    # på nytt: ingenting skjer, og morgenjobben verken legger den i kø igjen eller sender den
    assert godkjenning.send(Kjoring(con, idag=idag), ider)["sendt"] == 0
    daglig.kjor(Kjoring(con, idag=idag))
    assert len(sendt) == foer + 2 and len(_koe(con, "ukefor")) == 2
    # kopien i e-posthistorikken finnes, som for alle andre e-poster
    assert con.execute("SELECT COUNT(*) FROM sendt_epost WHERE type='ukefor' AND status='sendt'").fetchone()[0] == 2


def test_ikke_send_betyr_aldri(con, sendt):
    _kurs(con)
    idag = START - timedelta(days=5)
    daglig.kjor(Kjoring(con, idag=idag))
    ider = [r["id"] for r in _koe(con, "ukefor")]
    assert godkjenning.avvis(con, ider, "admin:test") == 2
    con.commit()
    daglig.kjor(Kjoring(con, idag=idag + timedelta(days=1)))
    assert godkjenning.send(Kjoring(con, idag=idag), ider)["sendt"] == 0
    assert "ukefor" not in _sendte_typer(con)
    assert {r["status"] for r in _koe(con, "ukefor")} == {"avvist"} and len(_koe(con, "ukefor")) == 2


def test_avmeldt_deltaker_faar_ikke_paaminnelsen_selv_om_den_godkjennes(con, sendt):
    _, pids = _kurs(con)
    idag = START - timedelta(days=5)
    daglig.kjor(Kjoring(con, idag=idag))
    con.execute("UPDATE paamelding SET status='avmeldt' WHERE id=?", (pids[0],))
    con.commit()
    ut = godkjenning.send(Kjoring(con, idag=idag), [r["id"] for r in _koe(con, "ukefor")])
    assert ut["sendt"] == 1 and ut["utgaatt"] == 1
    utgaatt = [r for r in _koe(con, "ukefor") if r["status"] == "utgaatt"]
    assert utgaatt[0]["paamelding_id"] == pids[0] and "plass" in utgaatt[0]["merknad"]


def test_paaminnelsen_dagen_foer_utgaar_naar_kursdagen_er_kommet(con, sendt):
    _kurs(con)
    daglig.kjor(Kjoring(con, idag=START - timedelta(days=1)))
    dagfor = _koe(con, f"dagfor-{START.isoformat()}")
    assert len(dagfor) == 2
    ut = godkjenning.send(Kjoring(con, idag=START), [r["id"] for r in dagfor])
    assert ut["sendt"] == 0 and ut["utgaatt"] == 2
    assert f"dagfor-{START.isoformat()}" not in _sendte_typer(con)


def test_morgenjobben_merker_utgaatte_e_poster(con, sendt):
    _kurs(con)
    daglig.kjor(Kjoring(con, idag=START - timedelta(days=1)))
    daglig.kjor(Kjoring(con, idag=START + timedelta(days=1)))
    assert {r["status"] for r in _koe(con, f"dagfor-{START.isoformat()}")} == {"utgaatt"}
    assert all(r["html"] is None for r in _koe(con, f"dagfor-{START.isoformat()}"))


# ============================ personlige lenker ============================

def test_min_side_lenken_skjules_og_lages_paa_nytt(con, monkeypatch):
    kid, pids = _kurs(con)
    lenke = f"{config.BASE_URL}/min/{lenker.min_side_token(pids[0])}"
    skjult = godkjenning.skjul_lenker(f'<p><a href="{lenke}">Min side</a> {lenke}</p>')
    assert lenke not in skjult and skjult.count(f"{config.BASE_URL}/min/skjult") == 2
    k = Kjoring(con, idag=START)
    rad = {"paamelding_id": pids[0], "nokkel": f"kurs:{kid}"}
    assert f"{config.BASE_URL}/logg-inn" in godkjenning.gjenopprett_lenker(k, rad, skjult)    # Min side ikke åpen: innloggingen
    monkeypatch.setattr(k, "min_side_lenke", lambda pid: lenke if pid == pids[0] else None)
    assert godkjenning.gjenopprett_lenker(k, rad, skjult).count(lenke) == 2


def test_kvitteringslenken_skjules_og_lages_paa_nytt(con):
    kid, _ = _kurs(con)
    cur = con.execute("""INSERT INTO firmapaamelding (kurs_id, innsendingsnokkel, kvittering_token, kontakt_navn, kontakt_epost,
                                                      firmanavn) VALUES (?,?,?,?,?,?)""",
                      (kid, "n1", "abcDEF_123-xyz", "Kontakt Person", "kontakt@eksempel.no", "Eksempel AS"))
    con.commit()
    url = f"{config.BASE_URL}/kurs/KS1/gruppe/kvittering/abcDEF_123-xyz"
    skjult = godkjenning.skjul_lenker(f'<a href="{url}">{url}</a>')
    assert "abcDEF_123-xyz" not in skjult
    k = Kjoring(con, idag=START)
    assert godkjenning.gjenopprett_lenker(k, {"paamelding_id": None, "nokkel": f"firmapaamelding:{cur.lastrowid}"}, skjult).count(url) == 2
    with pytest.raises(ValueError):
        godkjenning.gjenopprett_lenker(k, {"paamelding_id": None, "nokkel": "firmapaamelding:999"}, skjult)


def test_e_post_med_vedlegg_legges_ikke_i_koen_uten_vedlegget(con, sendt):
    _kurs(con)
    k = Kjoring(con, idag=START)
    with pytest.raises(ValueError):
        k.send_ferdigrendret_en_gang("kurs:1", "g1@eksempel.no", "ukefor", "Emne", "<p>Hei</p>",
                                     vedlegg=[epost.Vedlegg("a.txt", "text/plain", b"x")])
    assert _koe(con) == [] and sendt == []


# ============================ avlysning og adminsiden ============================

def test_avlysning_venter_paa_godkjenning_og_utgaar_hvis_kurset_gjenaapnes(con, sendt):
    kid, _ = _kurs(con)
    k = _admin()
    r = k.post(f"/admin/kurs/{kid}/avlys", follow_redirects=True)
    assert "Klar til sending" in r.get_data(as_text=True)
    koe = _koe(con, "avlysning")
    assert len(koe) == 2 and all(r["kurs_id"] == kid for r in koe)
    assert not [s for s in sendt if s[1] == koe[0]["emne"]]
    k.post(f"/admin/kurs/{kid}/gjenaapne")
    ut = godkjenning.send(Kjoring(con, idag=START - timedelta(days=30)), [r["id"] for r in koe])
    assert ut["utgaatt"] == 2 and ut["sendt"] == 0


def test_adminsiden_viser_koen_og_sender_etter_godkjenning(con, sendt):
    _kurs(con)
    idag = START - timedelta(days=5)
    daglig.kjor(Kjoring(con, idag=idag))
    ider = [r["id"] for r in _koe(con, "ukefor")]
    k = _admin()
    with k.session_transaction() as s:
        s["demo_dato"] = idag.isoformat()
    side = k.get("/admin/klar-til-sending").get_data(as_text=True)
    assert "Påminnelse uka før" in side and "G1 Test" in side and "Send alle (2)" in side
    oversikt = k.get("/admin").get_data(as_text=True)
    assert "E-poster klar til sending" in oversikt and "Se og godkjenn" in oversikt
    en = k.get(f"/admin/klar-til-sending/{ider[0]}").get_data(as_text=True)
    assert "<iframe" in en and _koe(con, "ukefor")[0]["emne"] in en
    r = k.post("/admin/klar-til-sending/send", data={"id": [str(i) for i in ider]}, follow_redirects=True)
    assert "2 e-post(er) er sendt" in r.get_data(as_text=True)
    assert {r_["status"] for r_ in _koe(con, "ukefor")} == {"sendt"}
    assert "Ingen e-poster venter på godkjenning" in k.get("/admin/klar-til-sending").get_data(as_text=True)


def test_lesetilgang_kan_se_men_ikke_sende(con, sendt):
    _kurs(con)
    daglig.kjor(Kjoring(con, idag=START - timedelta(days=5)))
    ider = [r["id"] for r in _koe(con, "ukefor")]
    db.opprett_admin_bruker(con, "leser", "Lese Leser", "lese-passord-123", rolle="lese")
    con.commit()
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": "leser", "passord": "lese-passord-123"})
    side = k.get("/admin/klar-til-sending")
    assert side.status_code == 200 and "Send alle" not in side.get_data(as_text=True)
    assert k.post("/admin/klar-til-sending/send", data={"id": [str(i) for i in ider]}).status_code == 403
    assert {r["status"] for r in _koe(con, "ukefor")} == {"venter"}


def test_anonymisering_sletter_personens_e_poster_i_koen(con, sendt):
    _, pids = _kurs(con)
    daglig.kjor(Kjoring(con, idag=START - timedelta(days=5)))
    con.execute("UPDATE kurs SET status='avsluttet'")            # anonymisering krever at personen ikke har aktive påmeldinger
    did = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pids[0],)).fetchone()["deltaker_id"]
    ut = db.anonymiser_deltaker(con, did, "admin:test")
    con.commit()
    assert ut["epostkoe"] == 1
    assert [r["paamelding_id"] for r in _koe(con, "ukefor")] == [pids[1]]
