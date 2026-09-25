"""Herding av integrasjonene (SharePoint, Zoom): driftsgrenene testes med etterlignet Microsoft Graph / Zoom - ingen
ekte nettverkskall. Hva som IKKE er testet mot ekte tjenester, står i AZURE-SETUP.md."""
import io
import json
from datetime import date, timedelta
from urllib.parse import urlsplit

import pytest
import requests

from kurs import config, daglig, db, lenker, migreringer
from kurs.integrasjoner import m365, sharepoint, visma, zoom
from kurs.kjoring import Kjoring

EKTE_OPPRETT_KURSMAPPE = sharepoint.opprett_kursmappe


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    monkeypatch.setattr(sharepoint, "DEMO_ROT", tmp_path / "sharepoint_demo")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _http_feil(status: int) -> requests.HTTPError:
    svar = requests.Response()
    svar.status_code = status
    return requests.HTTPError(f"{status}", response=svar)


class FalskGraph:
    """Etterligner m365.graph: husker kallene og svarer med feil for utvalgte (metode, sti)-par."""

    def __init__(self, feil=None):
        self.kall, self.feil = [], feil or {}

    def __call__(self, metode, sti, **kw):
        self.kall.append((metode, sti, kw.get("json")))
        status = self.feil.get((metode, sti, (kw.get("json") or {}).get("name")))
        if status:
            raise _http_feil(status)
        svar = requests.Response()
        svar.status_code, svar._content = 200, b"innhold"
        return svar


@pytest.fixture
def drift_graph(monkeypatch):
    """Driftsgrenen til sharepoint.py med etterlignet Graph."""
    monkeypatch.setattr(config, "DEMO", False)
    monkeypatch.setattr(config, "SHAREPOINT_SITE_ID", "site-1")
    falsk = FalskGraph()
    monkeypatch.setattr(m365, "graph", falsk)
    return falsk


# ============================ SharePoint: driftsgrenen ============================

def test_kursmappe_lages_niva_for_niva_uten_aa_kunne_erstatte_noe(drift_graph):
    assert sharepoint.opprett_kursmappe("EFT-2027-2") == "Kurs/EFT-2027-2"
    rot = "/sites/site-1/drive/root"
    assert [(m, s, j["name"]) for m, s, j in drift_graph.kall] == [
        ("POST", f"{rot}/children", "Kurs"),
        ("POST", f"{rot}:/Kurs:/children", "EFT-2027-2"),
        ("POST", f"{rot}:/Kurs/EFT-2027-2:/children", "Presentasjoner"),
        ("POST", f"{rot}:/Kurs/EFT-2027-2:/children", "Deltakere"),
    ]
    assert all(j["@microsoft.graph.conflictBehavior"] == "fail" and j["folder"] == {} for _, _, j in drift_graph.kall)


def test_eksisterende_mapper_409_er_ok_saa_kallet_er_idempotent(drift_graph):
    rot = "/sites/site-1/drive/root"
    drift_graph.feil = {("POST", f"{rot}/children", "Kurs"): 409, ("POST", f"{rot}:/Kurs:/children", "EFT-2027"): 409,
                        ("POST", f"{rot}:/Kurs/EFT-2027:/children", "Presentasjoner"): 409}
    assert sharepoint.opprett_kursmappe("EFT-2027") == "Kurs/EFT-2027"
    assert len(drift_graph.kall) == 4


@pytest.mark.parametrize("status", [401, 403, 404, 500, 503])
def test_andre_graph_feil_sendes_videre(drift_graph, status):
    drift_graph.feil = {("POST", "/sites/site-1/drive/root:/Kurs:/children", "EFT-2027"): status}
    with pytest.raises(requests.HTTPError):
        sharepoint.opprett_kursmappe("EFT-2027")


@pytest.mark.parametrize("kode", ["", "../x", "Kurs/EFT", "EFT 2027", "-EFT", "a" * 81, "EFT-2027\n"])
def test_ugyldig_kurskode_avvises_foer_noe_kall(drift_graph, kode):
    with pytest.raises(ValueError):
        sharepoint.opprett_kursmappe(kode)
    assert drift_graph.kall == []


def test_hent_fil_404_fra_graph_blir_filenotfounderror(drift_graph):
    drift_graph.feil = {("GET", "/sites/site-1/drive/root:/Kurs/X/Presentasjoner/a.pdf:/content", None): 404}
    with pytest.raises(FileNotFoundError):
        sharepoint.hent_fil("Kurs/X/Presentasjoner/a.pdf")
    drift_graph.feil = {("GET", "/sites/site-1/drive/root:/Kurs/X/Presentasjoner/a.pdf:/content", None): 503}
    with pytest.raises(requests.HTTPError):
        sharepoint.hent_fil("Kurs/X/Presentasjoner/a.pdf")


# ============================ SharePoint: nytt kurs og levering ============================

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


def _feilende_sharepoint(monkeypatch):
    kall = []

    def feiler(kode):
        kall.append(kode)
        raise requests.ConnectionError("graph nede")
    monkeypatch.setattr(sharepoint, "opprett_kursmappe", feiler)
    return kall


def test_nytt_kurs_lagres_selv_om_sharepoint_feiler_og_mappen_kan_lages_senere(con, monkeypatch):
    kall = _feilende_sharepoint(monkeypatch)
    k = _admin()
    r = _nytt_kurs(k)
    kurs = con.execute("SELECT * FROM kurs").fetchone()
    assert r.status_code == 302 and kurs is not None and kurs["sharepoint_mappe"] is None and kall == [kurs["kode"]]
    side = k.get(r.headers["Location"]).get_data(as_text=True)
    assert "SharePoint-mappen kunne ikke lages akkurat nå" in side and "Lag SharePoint-mappe" in side
    rad = con.execute("SELECT detaljer FROM hendelse WHERE handling='sharepoint_mappe_feilet'").fetchone()
    assert rad and "graph nede" not in rad["detaljer"]                      # kun sikker feiltekst (type), ikke meldingen

    monkeypatch.setattr(sharepoint, "opprett_kursmappe", EKTE_OPPRETT_KURSMAPPE)   # SharePoint virker igjen (demo)
    r = k.post(f"/admin/kurs/{kurs['id']}/sharepoint-mappe")
    assert r.status_code == 302
    assert con.execute("SELECT sharepoint_mappe FROM kurs").fetchone()[0] == f"Kurs/{kurs['kode']}"
    assert "SharePoint-mappen er laget." in k.get(r.headers["Location"]).get_data(as_text=True)


def test_ingen_sharepoint_mappe_naar_kurset_ikke_blir_lagret(con, monkeypatch):
    kall = []
    monkeypatch.setattr(sharepoint, "opprett_kursmappe", lambda kode: kall.append(kode) or f"Kurs/{kode}")
    r = _nytt_kurs(_admin(), fakturering="ugyldig")                         # CHECK-feil i databasen
    assert r.status_code == 200 and "Kunne ikke opprette kurs" in r.get_data(as_text=True)
    assert kall == [] and con.execute("SELECT COUNT(*) FROM kurs").fetchone()[0] == 0


def test_lesetilgang_kan_ikke_lage_sharepoint_mappe(con):
    kid = db.opprett_kurs(con, kode="L1", navn="Kurs", datoer=["2099-01-01"])
    db.opprett_admin_bruker(con, "leser", "Leser", "passord-som-holder", rolle="lese")
    con.commit()
    k = _admin("leser", "passord-som-holder")
    assert k.post(f"/admin/kurs/{kid}/sharepoint-mappe").status_code == 403
    assert "Lag SharePoint-mappe" not in k.get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True)


def _krav(con, sharepoint_mappe=None):
    kid = db.opprett_kurs(con, kode="LEV-2099", navn="Leveringskurs", datoer=["2099-01-01"],
                          sharepoint_mappe=sharepoint_mappe)
    krav = db.sett_inn(con, "INSERT INTO materiell_krav (kurs_id, ansvarlig_navn, ansvarlig_epost, frist) "
                            "VALUES (?,?,?,?)", (kid, "Kursholder", "kh@eksempel.no", "2098-12-01"))
    con.commit()
    return kid, krav, urlsplit(lenker.lever_lenke(krav)).path


def _last_opp(sti):
    from kurs.web import app as webapp
    return webapp.app.test_client().post(sti, data={"fil": (io.BytesIO(b"%PDF-1.4 demo"), "slides.pdf")},
                                         content_type="multipart/form-data")


def test_levering_lager_mappen_ved_behov(con):
    kid, krav, sti = _krav(con)
    r = _last_opp(sti)
    assert r.status_code == 302
    assert con.execute("SELECT sharepoint_mappe FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "Kurs/LEV-2099"
    assert (sharepoint.DEMO_ROT / "Kurs/LEV-2099/Presentasjoner/slides.pdf").read_bytes() == b"%PDF-1.4 demo"
    assert con.execute("SELECT levert_ts FROM materiell_krav WHERE id=?", (krav,)).fetchone()[0]


def test_levering_gir_503_og_lagrer_ingenting_naar_sharepoint_feiler(con, monkeypatch):
    _, krav, sti = _krav(con, sharepoint_mappe="Kurs/LEV-2099")

    def feiler(*a):
        raise requests.Timeout("tidsavbrudd")
    monkeypatch.setattr(sharepoint, "last_opp", feiler)
    r = _last_opp(sti)
    assert r.status_code == 503 and "kunne ikke lastes opp akkurat nå" in r.get_data(as_text=True)
    assert con.execute("SELECT levert_ts FROM materiell_krav WHERE id=?", (krav,)).fetchone()[0] is None
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='materiell_opplasting_feilet'").fetchone()[0] == 1


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
    did = db.finn_eller_opprett_deltaker(con, "kb@eksempel.no", "Kari Bevis")
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
        s["deltaker_id"] = db.finn_eller_opprett_deltaker(con, "annen@eksempel.no", "Annen")
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
