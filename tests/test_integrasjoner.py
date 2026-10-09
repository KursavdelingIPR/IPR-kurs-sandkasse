"""Herding av integrasjonene (Zoom, Visma): driftsgrenene testes med etterlignede tjenester - ingen ekte nettverkskall.
Hva som IKKE er testet mot ekte tjenester, står i AZURE-SETUP.md.
(06.10.2026: kursholder-lenken og SharePoint er tatt bort, og med dem testene av SharePoint-mappene og leveringen.)"""
import json
from datetime import date, timedelta

import pytest
import requests

from kurs import config, daglig, db, migreringer
from kurs.integrasjoner import visma, zoom
from kurs.kjoring import Kjoring


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


# ============================ nytt kurs som ikke kan lagres ============================

def _admin(brukernavn=None, passord=None):
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    r = k.post("/admin/logg-inn", data={"brukernavn": brukernavn or config.ADMIN_BRUKERNAVN,
                                        "passord": passord or config.ADMIN_PASSORD})
    assert r.status_code == 302
    return k


def _nytt_kurs(k, **over):
    data = {"navn": "Integrasjonskurs", "type": "fysisk", "sted": "Bergen", "kapasitet": "",
            "datoer": (date.today() + timedelta(days=90)).isoformat(), "start_kl": "09:00", "slutt_kl": "16:00",
            "timer_pr_dag": "6", "pris_nok": "1000", "fakturering": "person", "betaling": "samlet",
            "faktura_dager_for": "14", "ansvarlig_admin_id": "", "paameldingsfrist": "", "kursholder_navn": "",
            "kursholder_epost": "", "materiell_frist": "", "notat": "", **over}
    return k.post("/admin/kurs/ny", data=data)


def test_kurs_som_ikke_kan_lagres_gir_feilmelding_og_ingen_rad(con):
    # (06.10.2026: kursholder-lenken og SharePoint er tatt bort - før sjekket testen også at ingen SharePoint-mappe ble laget)
    r = _nytt_kurs(_admin(), fakturering="ugyldig")                         # CHECK-feil i databasen
    assert r.status_code == 200 and "Kunne ikke opprette kurs" in r.get_data(as_text=True)
    assert con.execute("SELECT COUNT(*) FROM kurs").fetchone()[0] == 0


# ============================ Zoom ============================

def _digitalt_kurs(con, **felter):
    start = date(2031, 3, 10)
    kid = db.opprett_kurs(con, kode="ZOOM-2031", navn="Digitalt kurs", datoer=[start.isoformat()], type="digital",
                          **felter)
    con.commit()
    return kid, start


def test_zoom_lenken_lagres_straks_og_overlever_en_senere_feil(con, monkeypatch):
    kid, start = _digitalt_kurs(con)
    monkeypatch.setattr(zoom, "opprett_mote", lambda navn: {"id": 987, "join_url": "https://zoom.us/j/987",
                                                             "password": "pw"})
    k = Kjoring(con, idag=start - timedelta(days=5))
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    ny = daglig._zoom(k, kurs, start)
    assert ny["zoom_url"] == "https://zoom.us/j/987"
    con.execute("UPDATE kurs SET notat='ulagret' WHERE id=?", (kid,))
    con.rollback()                                                           # en senere feil i samme kjøring
    rad = con.execute("SELECT zoom_url, zoom_id, notat FROM kurs WHERE id=?", (kid,)).fetchone()
    assert (rad["zoom_url"], rad["zoom_id"], rad["notat"]) == ("https://zoom.us/j/987", "987", None)


def test_zoom_overskriver_aldri_en_lenke_som_kom_i_mellomtiden(con, monkeypatch):
    kid, start = _digitalt_kurs(con)
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()   # lest foer admin la inn lenken
    con.execute("UPDATE kurs SET zoom_url='https://zoom.us/j/admin' WHERE id=?", (kid,))
    con.commit()
    monkeypatch.setattr(zoom, "opprett_mote", lambda navn: {"id": 555, "join_url": "https://zoom.us/j/555"})
    daglig._zoom(Kjoring(con, idag=start - timedelta(days=5)), kurs, start)
    assert con.execute("SELECT zoom_url FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "https://zoom.us/j/admin"
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='zoom_mote_ubrukt'").fetchone()[0] == 1


# ============================ kursbevis i databasen (migrering 4) ============================

def _gammelt_kursbevis(con, rot, url, innhold=None):
    """Et kursbevis slik eldre kode lagret det: fil under data/kursbevis/ og url 'lokal:...'."""
    kid = db.opprett_kurs(con, kode="KB-2030", navn="Bevis", datoer=["2030-01-01"], status="avsluttet")
    did = db.finn_eller_opprett_deltaker(con, "kb@eksempel.no", "Kari", "Bevis")
    dok = db.sett_inn(con, "INSERT INTO dokument (kurs_id, deltaker_id, type, tittel, url) VALUES (?,?,?,?,?)",
                      (kid, did, "kursbevis", "Kursbevis – Bevis", url))
    if innhold is not None:
        fil = rot / url[len("lokal:"):]
        fil.parent.mkdir(parents=True, exist_ok=True)
        fil.write_text(innhold, encoding="utf-8")
    con.commit()
    return dok, did


def _kjor_migrering_4_paa_nytt(con):
    con.execute("DELETE FROM schema_versjon WHERE versjon >= 4")
    con.commit()
    return migreringer.kjor_manglende(con)


def test_migrering_4_flytter_kursbevisfiler_inn_i_databasen(con, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROT", tmp_path)
    dok, _ = _gammelt_kursbevis(con, tmp_path, "lokal:data/kursbevis/KB-2030/1.html", "<h1>Kursbevis Kari</h1>")
    assert _kjor_migrering_4_paa_nytt(con)[0] == 4
    assert con.execute("SELECT url FROM dokument WHERE id=?", (dok,)).fetchone()[0] == "db:"
    rad = con.execute("SELECT mimetype, innhold FROM dokument_innhold WHERE dokument_id=?", (dok,)).fetchone()
    assert (rad["mimetype"], rad["innhold"]) == ("text/html", "<h1>Kursbevis Kari</h1>")
    assert (tmp_path / "data/kursbevis/KB-2030/1.html").exists()             # filen slettes ikke automatisk
    assert _kjor_migrering_4_paa_nytt(con)[0] == 4                           # idempotent
    assert con.execute("SELECT COUNT(*) FROM dokument_innhold").fetchone()[0] == 1


@pytest.mark.parametrize("url,innhold", [
    ("lokal:data/kursbevis/KB-2030/mangler.html", None),                   # filen er borte
    ("lokal:data/kursbevis/../../hemmelig.txt", "HEMMELIG"),               # peker ut av data/
])
def test_migrering_4_lar_rader_uten_gyldig_fil_staa_uroert(con, tmp_path, monkeypatch, url, innhold):
    monkeypatch.setattr(config, "ROT", tmp_path / "rot")
    (tmp_path / "rot").mkdir()
    if innhold:
        (tmp_path / "hemmelig.txt").write_text(innhold, encoding="utf-8")
    dok, _ = _gammelt_kursbevis(con, tmp_path / "rot", url)
    _kjor_migrering_4_paa_nytt(con)
    assert con.execute("SELECT url FROM dokument WHERE id=?", (dok,)).fetchone()[0] == url
    assert con.execute("SELECT COUNT(*) FROM dokument_innhold").fetchone()[0] == 0


def test_kursbevis_fra_databasen_vises_bare_for_eieren(con, tmp_path, monkeypatch):
    from kurs.web import app as webapp
    monkeypatch.setattr(config, "ROT", tmp_path)
    dok, did = _gammelt_kursbevis(con, tmp_path, "lokal:data/kursbevis/KB-2030/1.html", "<h1>Kursbevis Kari</h1>")
    _kjor_migrering_4_paa_nytt(con)
    eier = webapp.app.test_client()
    with eier.session_transaction() as s:
        s["deltaker_id"] = did
    r = eier.get(f"/dokument/{dok}")
    assert r.status_code == 200 and r.mimetype == "text/html" and "Kursbevis Kari" in r.get_data(as_text=True)
    annen = webapp.app.test_client()
    with annen.session_transaction() as s:
        s["deltaker_id"] = db.finn_eller_opprett_deltaker(con, "annen@eksempel.no", "Annen", "Test")
    con.commit()
    assert annen.get(f"/dokument/{dok}").status_code == 403
    assert webapp.app.test_client().get(f"/dokument/{dok}").status_code == 403


# ============================ Visma: roterende refresh-token ============================

class FalskTokenserver:
    """Etterligner Visma sitt token-endepunkt. `svar` er en liste av (status, json) eller unntak, i rekkefølge."""

    def __init__(self, *svar, foer_svar=None):
        self.svar, self.brukt, self.foer_svar = list(svar), [], foer_svar

    def __call__(self, url, data=None, timeout=None):
        assert url == visma.TOKEN_URL and data["grant_type"] == "refresh_token" and timeout
        self.brukt.append(data["refresh_token"])
        if self.foer_svar:
            self.foer_svar(len(self.brukt))
        neste = self.svar.pop(0)
        if isinstance(neste, Exception):
            raise neste
        status, kropp = neste
        r = requests.Response()
        r.status_code, r._content = status, json.dumps(kropp).encode()
        return r


def _ok(access, refresh):
    return 200, {"access_token": access, "expires_in": 3600, "refresh_token": refresh, "token_type": "bearer"}


@pytest.fixture
def visma_drift(con, monkeypatch):
    monkeypatch.setattr(config, "DEMO", False)
    monkeypatch.setattr(config, "VISMA_CLIENT_ID", "klient-id")
    monkeypatch.setattr(config, "VISMA_CLIENT_SECRET", "klient-hemmelighet")
    monkeypatch.setattr(config, "VISMA_REFRESH_TOKEN", "start-token")
    monkeypatch.setattr(visma, "_cache", {})

    def server(*svar, **kw):
        falsk = FalskTokenserver(*svar, **kw)
        monkeypatch.setattr(requests, "post", falsk)
        return falsk
    return server


def _lagret(con):
    return db.hent_integrasjonstoken(con, visma.REFRESH_NOKKEL)


def test_rotert_refresh_token_lagres_straks_og_brukes_neste_gang(con, visma_drift):
    server = visma_drift(_ok("a1", "r2"), _ok("a2", "r3"))
    assert visma._token() == "a1" and _lagret(con) == "r2"
    assert visma._token() == "a1"                                          # gyldig tilgangstoken gjenbrukes
    visma._cache.clear()                                                   # «omstart»
    assert visma._token() == "a2" and _lagret(con) == "r3"
    assert server.brukt == ["start-token", "r2"]


def test_lagret_token_vinner_over_startverdien(con, visma_drift):
    db.lagre_integrasjonstoken(con, visma.REFRESH_NOKKEL, "lagret-token")
    con.commit()
    server = visma_drift(_ok("a1", "r2"))
    visma._token()
    assert server.brukt == ["lagret-token"]


def test_invalid_grant_leser_paa_nytt_naar_en_annen_prosess_har_fornyet(con, visma_drift):
    def annen_prosess_fornyer(forsok):
        if forsok == 1:
            c = db.koble()
            db.lagre_integrasjonstoken(c, visma.REFRESH_NOKKEL, "fornyet-av-annen")
            c.commit()
            c.close()
    server = visma_drift((400, {"error": "invalid_grant"}), _ok("a1", "r9"), foer_svar=annen_prosess_fornyer)
    assert visma._token() == "a1"
    assert server.brukt == ["start-token", "fornyet-av-annen"] and _lagret(con) == "r9"


def test_invalid_grant_uten_nytt_token_gir_ikke_sendt_etter_ett_forsoek(con, visma_drift):
    server = visma_drift((400, {"error": "invalid_grant"}))
    with pytest.raises(visma.IkkeSendt, match="invalid_grant") as feil:
        visma._token()
    assert server.brukt == ["start-token"] and "start-token" not in str(feil.value)


@pytest.mark.parametrize("sekunder_igjen, fornyes", [(61, False), (60, True), (59, True), (0, True)])
def test_tilgangstoken_fornyes_naar_det_har_ett_minutt_eller_mindre_igjen(con, visma_drift, monkeypatch,
                                                                        sekunder_igjen, fornyes):
    naa = 1_900_000_000.0
    monkeypatch.setattr(visma.time, "time", lambda: naa)
    visma._cache.update(token="gammelt", utloper=naa + sekunder_igjen)
    server = visma_drift(_ok("nytt", "r2"))
    assert visma._token() == ("nytt" if fornyes else "gammelt")
    assert server.brukt == (["start-token"] if fornyes else [])


def test_invalid_grant_gir_hoeyst_to_forsoek_selv_om_tokenet_stadig_byttes(con, visma_drift):
    teller = iter(range(100))

    def annen_prosess_fornyer_hver_gang(_forsok):
        c = db.koble()
        db.lagre_integrasjonstoken(c, visma.REFRESH_NOKKEL, f"fornyet-{next(teller)}")
        c.commit()
        c.close()
    server = visma_drift(*[(400, {"error": "invalid_grant"})] * 3, foer_svar=annen_prosess_fornyer_hver_gang)
    with pytest.raises(visma.IkkeSendt, match="invalid_grant"):
        visma._token()
    assert len(server.brukt) == 2


@pytest.mark.parametrize("svar", [requests.ConnectionError("nede"), requests.Timeout("treg"), (500, {}), (401, {})])
def test_nettverks_og_serverfeil_mot_token_endepunktet_gir_ikke_sendt(con, visma_drift, svar):
    visma_drift(svar)
    with pytest.raises(visma.IkkeSendt):
        visma._token()
    assert _lagret(con) is None


@pytest.mark.parametrize("felt", ["VISMA_CLIENT_ID", "VISMA_CLIENT_SECRET", "VISMA_REFRESH_TOKEN"])
def test_manglende_oppsett_gir_ikke_sendt_uten_nettverkskall(con, visma_drift, monkeypatch, felt):
    server = visma_drift()
    monkeypatch.setattr(config, felt, "")
    with pytest.raises(visma.IkkeSendt):
        visma._token()
    assert server.brukt == []


def test_tilgangsfeil_midt_i_en_fakturering_er_uavklart_ikke_trygg(con, visma_drift):
    visma_drift((503, {}))
    with pytest.raises(RuntimeError) as feil:
        visma._kall("POST", "/customerinvoicedrafts", json={})
    assert not isinstance(feil.value, visma.IkkeSendt)


def test_fakturer_sjekker_tilgangen_foer_noe_kall_mot_regnskapsdata(con, visma_drift, monkeypatch):
    visma_drift((503, {}))
    kall = []
    monkeypatch.setattr(requests, "request", lambda *a, **kw: kall.append(a))
    g = visma.Fakturagrunnlag("Kari", "kari@eksempel.no", True, None, None, None, None, False, None, "Kurs", None, 100)
    with pytest.raises(visma.IkkeSendt):
        visma.fakturer(g)
    assert kall == []


def test_demo_gjoer_aldri_token_kall(con, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **kw: pytest.fail("nettverkskall i demo"))
    assert visma._token() == "demo-token"
