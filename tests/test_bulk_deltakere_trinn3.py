"""Fase 11, trinn 3: faktisk bulkbehandling (POST /bulk/behandle).

Bulk er IKKE atomisk: én paamelding om gangen med motorens egne korte claim-/resultat-commits, fersk
revalidering per rad, delvis suksess, stoppregel og tidsbudsjett. Samtidighetstestene bruker separate
requests/forbindelser (traader, events, barrierer) slik hardening-testene gjor - claims er hovedbeskyttelsen.
Graph/Visma-taksonomien er UAVGJORT: alle feil er 'ukjent' og proves aldri automatisk paa nytt.
"""
import inspect
import json
import re
import threading
from datetime import date, timedelta

import pytest
import requests

from kurs import behandling, config, db, sveiper
from kurs.integrasjoner import epost, visma
from kurs.kjoring import Kjoring
from kurs.web import app as webapp

FORHANDSVIS = "/admin/kurs/{kid}/deltakere/bulk/forhandsvis"
BEHANDLE = "/admin/kurs/{kid}/deltakere/bulk/behandle"
FASE9 = "/admin/kurs/{kid}/deltaker/{pid}/behandle"
_orig_visma = visma.fakturer

FULLFORT = "Fullført"
UAVKLART = "Uavklart – krever kontroll"
ALLEREDE = "Allerede behandlet"
IKKE_BEH = "Ikke lenger behandlingsbar"
FEIL = "Teknisk feil – ikke fullført"
IKKE_FORSOKT = "Ikke forsøkt (stoppet)"


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _klient(innlogget=True):
    k = webapp.app.test_client()
    if innlogget:
        k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def _kurs(con, kode="T1", **kw):
    start = date.today() + timedelta(days=30)
    kid = db.opprett_kurs(con, kode=kode, navn="Testkurs", datoer=[start.isoformat()],
                          sharepoint_mappe=f"Kurs/{kode}", **{"pris_nok": 1000, "fakturering": "person", **kw})
    con.commit()
    return kid


def _deltakere(con, kid, n, start=0, **paamelding):
    """n manuelt registrerte (holdte) deltakere: (pid, epost, navn)."""
    ut = []
    for i in range(start, start + n):
        epost_, navn = f"p{i:02d}@x.no", f"Person {i:02d}"
        pid, _ = db.meld_paa(con, kid, epost=epost_, navn=navn, aktor="admin:test", tillat_utkast=True,
                             paamelding={"kilde": "admin", "sveiper_utsatt": 1, **paamelding})
        ut.append((pid, epost_, navn))
    con.commit()
    return ut


def _ider(ut):
    return [u[0] for u in ut]


def _post(klient, kid, ider, rute=BEHANDLE):
    return klient.post(rute.format(kid=kid), data={"paamelding_id": [str(i) for i in ider]})


def _tekst(resp):
    return resp.get_data(as_text=True)


def _kategori(html, navn):
    m = re.search(re.escape(navn) + r'</td>.*?<span class="merke[^"]*">([^<]+)</span>', html, re.S)
    assert m, f"{navn} finnes ikke i resultatsiden"
    return m.group(1)


def _teller(monkeypatch, feil_epost=(), feil_visma=()):
    kall = {"epost": [], "visma": []}

    def send(til, *a, **kw):
        kall["epost"].append(til)
        if til in feil_epost:
            raise RuntimeError(f"Graph nede for {til}")
    monkeypatch.setattr(epost, "send", send)

    def fakturer(g):
        kall["visma"].append(g.kunde_epost)
        if g.kunde_epost in feil_visma:
            raise RuntimeError("Visma nede")
        return _orig_visma(g)
    monkeypatch.setattr(visma, "fakturer", fakturer)
    return kall


def _flagg(con, pid):
    r = con.execute("SELECT sveiper_kjort, sveiper_utsatt FROM paamelding WHERE id=?", (pid,)).fetchone()
    return r["sveiper_kjort"], r["sveiper_utsatt"]


def _tell(con, sql, *a):
    return con.execute(sql, a).fetchone()[0]


def _hendelser(con, handling, fra_id=0):
    return [json.loads(r["detaljer"]) for r in con.execute(
        "SELECT detaljer FROM hendelse WHERE handling=? AND id>? ORDER BY id", (handling, fra_id))]


def _siste_hendelse_id(con):
    return _tell(con, "SELECT COALESCE(MAX(id),0) FROM hendelse")


def _global_sveiper():
    c = db.koble()
    try:
        sveiper.kjor(Kjoring(c, idag=date.today()))
        c.commit()
    finally:
        c.close()


def _http_feil(epost_):
    r = requests.Response()
    r.status_code, r.reason = 500, "Server Error"
    r.url = requests.Request("GET", "https://visma.example/v2/customers",
                             params={"$filter": f"EmailAddress eq '{epost_}'"}).prepare().url
    with pytest.raises(requests.HTTPError) as u:
        r.raise_for_status()
    return u.value


# ==================== A-E: tilgang, GET, tomt utvalg, maks 50, fremmede ID-er ====================

def test_A_krever_admin_og_behandler_ingenting(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 2)
    kall = _teller(monkeypatch)
    resp = _post(_klient(innlogget=False), kid, _ider(ut))
    assert resp.status_code == 302 and "logg-inn" in resp.headers["Location"]
    assert kall == {"epost": [], "visma": []} and _flagg(con, ut[0][0]) == (0, 1)


def test_B_get_kan_ikke_behandle(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 1)
    kall = _teller(monkeypatch)
    resp = _klient().get(BEHANDLE.format(kid=kid) + f"?paamelding_id={ut[0][0]}")
    assert resp.status_code == 405
    assert kall == {"epost": [], "visma": []} and _flagg(con, ut[0][0]) == (0, 1)


def test_C_tomt_utvalg_gir_feilmelding_og_ingen_logg(con):
    kid = _kurs(con)
    klient = _klient()
    resp = klient.post(BEHANDLE.format(kid=kid), data={})
    assert resp.status_code == 302
    assert "Velg minst" in _tekst(klient.get(resp.headers["Location"]))
    assert _tell(con, "SELECT COUNT(*) FROM hendelse WHERE handling='bulk_behandling_utlost'") == 0


def test_D_over_50_avvises_server_side_og_ingenting_behandles(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 51)
    kall = _teller(monkeypatch)
    klient = _klient()
    resp = _post(klient, kid, _ider(ut))
    assert resp.status_code == 302
    assert "maks 50" in _tekst(klient.get(resp.headers["Location"]))
    assert kall == {"epost": [], "visma": []}
    assert all(_flagg(con, pid) == (0, 1) for pid in _ider(ut))


def test_D_noyaktig_50_raa_felt_tillates(con, monkeypatch):
    """Grensen er 50 RAA request-felt (se test_raa_*). Duplikater innenfor grensen dedupes, se test_raa_duplikater_...;
    50 unike doblet = 100 raa felt avvises, se test_raa_50_unike_doblet_er_100_raa_felt_og_avvises."""
    kid = _kurs(con)
    ut = _deltakere(con, kid, 50)
    kall = _teller(monkeypatch)
    html = _tekst(_post(_klient(), kid, _ider(ut)))                                # 50 raa felt, 50 unike
    assert len(kall["epost"]) == 50 and len(kall["visma"]) == 50
    assert html.count(FULLFORT) >= 50


def test_E_id_fra_annet_kurs_gir_ingen_lekkasje_og_ingen_behandling(con, monkeypatch):
    k1, k2 = _kurs(con, "T1"), _kurs(con, "T2")
    mine = _deltakere(con, k1, 1, start=0)
    fremmed = _deltakere(con, k2, 1, start=7)
    kall = _teller(monkeypatch)
    html = _tekst(_post(_klient(), k1, _ider(mine) + _ider(fremmed) + [999999]))
    assert "Person 07" not in html and "p07@x.no" not in html
    assert kall["epost"] == ["p00@x.no"]
    assert _flagg(con, fremmed[0][0]) == (0, 1)
    agg = _hendelser(con, "bulk_behandling_utlost")[0]
    assert agg["antall_valgt"] == 1  # fremmede/ugyldige ID-er er ikke en del av utvalget

    # kun fremmede ID-er: samme svar som tomt utvalg (ingen bekreftelse av at de finnes)
    klient = _klient()
    resp = _post(klient, k1, _ider(fremmed) + [999999])
    assert resp.status_code == 302 and "Velg minst" in _tekst(klient.get(resp.headers["Location"]))


# ==================== preview -> bekreft ====================

def test_X_preview_sender_kun_behandlingsbare_videre_og_har_aktiv_knapp(con):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 2)
    ferdig = _deltakere(con, kid, 1, start=5)
    con.execute("UPDATE paamelding SET sveiper_kjort=1 WHERE id=?", (ferdig[0][0],))
    con.commit()
    html = _tekst(_klient().post(FORHANDSVIS.format(kid=kid), data={"paamelding_id": [str(i) for i in _ider(ut + ferdig)]}))
    skjult = re.findall(r'<input type="hidden" name="paamelding_id" value="(\d+)"', html)
    assert sorted(map(int, skjult)) == sorted(_ider(ut))            # ikke den allerede behandlede
    assert "Person 05" in html and "Allerede behandlet" in html      # men den vises fortsatt, med forklaring
    assert f"/admin/kurs/{kid}/deltakere/bulk/behandle" in html
    skjema = re.search(r"<form[^>]*bulk/behandle.*?</form>", html, re.S).group(0)
    assert not re.search(r"<button[^>]*disabled", skjema)          # knappen er aktiv
    assert "this.dataset.sendt" in html                              # dobbeltklikk-beskyttelse i nettleseren
    assert "Bekreftelse på e-post og fakturering" in html and "Hva skjer" in html


def test_X_preview_uten_behandlingsbare_har_ingen_bekreftknapp(con):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 1)
    con.execute("UPDATE paamelding SET sveiper_kjort=1")
    con.commit()
    html = _tekst(_klient().post(FORHANDSVIS.format(kid=kid), data={"paamelding_id": [str(_ider(ut)[0])]}))
    assert "Ingen av de valgte deltakerne kan behandles nå" in html and "bulk/behandle" not in html


def test_K5_preview_tekster_for_venteliste_og_tidligere_uavklart(con):
    kid = _kurs(con, kapasitet=1)
    db.meld_paa(con, kid, epost="forst@x.no", navn="Forst Plass")
    vent = _deltakere(con, kid, 1)                     # venteliste
    assert _tell(con, "SELECT status FROM paamelding WHERE id=?", vent[0][0]) == "venteliste"
    klient = _klient()
    _post(klient, kid, _ider(vent), rute=BEHANDLE)      # ventelistebeskjed sendes (demo)
    html = _tekst(klient.post(FORHANDSVIS.format(kid=kid), data={"paamelding_id": [str(vent[0][0])]}))
    assert "Allerede behandlet" in html and "behandles automatisk" not in html

    kid2 = _kurs(con, "T2")
    uav = _deltakere(con, kid2, 1, start=3)
    con.execute("UPDATE paamelding SET sveiper_utsatt=0 WHERE id=?", (uav[0][0],))
    db.reserver_sending(con, f"kurs:{kid2}", uav[0][1], "bekreftelse")
    db.sett_sending_ukjent(con, f"kurs:{kid2}", uav[0][1], "bekreftelse")
    con.commit()
    html2 = _tekst(klient.post(FORHANDSVIS.format(kid=kid2), data={"paamelding_id": [str(uav[0][0])]}))
    assert "Uavklart fra tidligere forsøk" in html2 and "behandles automatisk" not in html2


# ==================== F-G: endret mellom preview og POST ====================

def test_F_behandlet_for_post_gir_allerede_behandlet_uten_nye_sideeffekter(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 2)
    kall = _teller(monkeypatch)
    klient = _klient()
    klient.post(FORHANDSVIS.format(kid=kid), data={"paamelding_id": [str(i) for i in _ider(ut)]})  # preview: begge klare
    klient.post(FASE9.format(kid=kid, pid=ut[0][0]))                                                 # annen admin tok den forste
    html = _tekst(_post(klient, kid, _ider(ut)))
    assert _kategori(html, ut[0][2]) == ALLEREDE and _kategori(html, ut[1][2]) == FULLFORT
    assert kall["epost"].count(ut[0][1]) == 1 and kall["visma"].count(ut[0][1]) == 1
    agg = _hendelser(con, "bulk_behandling_utlost")[0]
    assert (agg["fullfort"], agg["allerede_behandlet"]) == (1, 1)


def test_G_avmeldt_mellom_preview_og_post_gir_ikke_behandlingsbar_og_urort_flagg(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 2)
    kall = _teller(monkeypatch)
    klient = _klient()
    klient.post(FORHANDSVIS.format(kid=kid), data={"paamelding_id": [str(i) for i in _ider(ut)]})
    db.meld_av(con, ut[0][0])
    con.commit()
    html = _tekst(_post(klient, kid, _ider(ut)))
    assert _kategori(html, ut[0][2]) == IKKE_BEH and _kategori(html, ut[1][2]) == FULLFORT
    assert ut[0][1] not in kall["epost"] and _flagg(con, ut[0][0])[1] == 1  # flagget URORT for den avmeldte


# ==================== H-J: bekreftet, venteliste, blandet ====================

def test_H_bekreftet_gir_en_epost_og_en_faktura(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 3)
    kall = _teller(monkeypatch)
    resp = _post(_klient(), kid, _ider(ut))
    assert resp.status_code == 200
    for pid, e, navn in ut:
        assert kall["epost"].count(e) == 1 and kall["visma"].count(e) == 1
        assert _flagg(con, pid) == (1, 0)
        assert _tell(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", pid) == 1
        assert _kategori(_tekst(resp), navn) == FULLFORT
    assert _tell(con, "SELECT COUNT(*) FROM faktura_forsok") == 0


def test_I_venteliste_gir_en_beskjed_og_ingen_faktura(con, monkeypatch):
    kid = _kurs(con, kapasitet=1)
    db.meld_paa(con, kid, epost="forst@x.no", navn="Forst Plass")
    ut = _deltakere(con, kid, 2)
    assert {_tell(con, "SELECT status FROM paamelding WHERE id=?", p) for p in _ider(ut)} == {"venteliste"}
    kall = _teller(monkeypatch)
    html = _tekst(_post(_klient(), kid, _ider(ut)))
    assert sorted(kall["epost"]) == sorted(e for _, e, _ in ut) and kall["visma"] == []
    assert _tell(con, "SELECT COUNT(*) FROM faktura") == 0
    assert all(_kategori(html, n) == FULLFORT for _, _, n in ut)


def test_J_blandet_utvalg_riktige_kategorier_og_aggregat(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 5)
    (p_ok, e_ok, n_ok), (p_ukjent, e_ukjent, n_ukjent), (p_allerede, _, n_allerede), (p_tidl, e_tidl, n_tidl), (p_avm, _, n_avm) = ut
    con.execute("UPDATE paamelding SET sveiper_kjort=1, sveiper_utsatt=0 WHERE id=?", (p_allerede,))
    con.execute("UPDATE paamelding SET sveiper_utsatt=0 WHERE id=?", (p_tidl,))
    db.reserver_sending(con, f"kurs:{kid}", e_tidl, "bekreftelse")
    db.sett_sending_ukjent(con, f"kurs:{kid}", e_tidl, "bekreftelse")
    con.execute("UPDATE paamelding SET status='avmeldt' WHERE id=?", (p_avm,))
    con.commit()
    kall = _teller(monkeypatch, feil_epost=(e_ukjent,))
    html = _tekst(_post(_klient(), kid, _ider(ut)))
    assert _kategori(html, n_ok) == FULLFORT
    assert _kategori(html, n_ukjent) == UAVKLART            # ny ukjent (Graph)
    assert _kategori(html, n_allerede) == ALLEREDE
    assert _kategori(html, n_tidl) == UAVKLART              # tidligere uavklart (ingen forsok)
    assert _kategori(html, n_avm) == IKKE_BEH
    assert kall["epost"] == [e_ok, e_ukjent]                # tidligere uavklart/allerede/avmeldt: ingen forsok
    agg = _hendelser(con, "bulk_behandling_utlost")[0]
    assert (agg["antall_valgt"], agg["fullfort"], agg["uavklart"], agg["allerede_behandlet"],
            agg["ikke_behandlingsbar"], agg["feilet"], agg["ikke_forsokt"], agg["stoppet"]) == (5, 1, 2, 1, 1, 0, 0, False)


# ==================== K: delvis suksess, ingen rollback ====================

def test_K_en_rad_feiler_uten_at_tidligere_eller_senere_rulles_tilbake(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 5)
    kall = _teller(monkeypatch, feil_epost=(ut[2][1],))
    html = _tekst(_post(_klient(), kid, _ider(ut)))
    kat = [_kategori(html, n) for _, _, n in ut]
    assert kat == [FULLFORT, FULLFORT, UAVKLART, FULLFORT, FULLFORT]
    for i in (0, 1, 3, 4):  # ingen rollback av tidligere - og senere rader ble behandlet
        pid, e, _ = ut[i]
        assert _flagg(con, pid) == (1, 0) and kall["epost"].count(e) == 1
        assert _tell(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", pid) == 1
        assert _tell(con, "SELECT status FROM utsending_logg WHERE mottaker=?", e) == "sendt"
    assert _tell(con, "SELECT status FROM utsending_logg WHERE mottaker=?", ut[2][1]) == "ukjent"
    assert _flagg(con, ut[2][0]) == (0, 0)


def test_K_teknisk_feil_uten_reservasjon_gir_feilet_og_daglig_jobb_henter_inn(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 3)
    kall = _teller(monkeypatch)
    ekte = epost.render
    monkeypatch.setattr(epost, "render", lambda mal, **d: (_ for _ in ()).throw(RuntimeError("mal"))
                        if d.get("p") is not None and d["p"]["epost"] == ut[1][1] else ekte(mal, **d))
    html = _tekst(_post(_klient(), kid, _ider(ut)))
    assert [_kategori(html, n) for _, _, n in ut] == [FULLFORT, FEIL, FULLFORT]
    assert _flagg(con, ut[1][0]) == (0, 0) and _tell(con, "SELECT COUNT(*) FROM utsending_logg WHERE mottaker=?", ut[1][1]) == 0
    monkeypatch.setattr(epost, "render", ekte)
    _global_sveiper()  # daglig jobb: kjent teknisk feil uten reservasjon hentes inn
    assert kall["epost"].count(ut[1][1]) == 1 and _flagg(con, ut[1][0]) == (1, 0)


def test_K_unntak_i_orkestreringen_for_en_rad_gir_feilet_uten_a_stoppe_de_andre(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 3)
    kall = _teller(monkeypatch)
    ekte = behandling.behandle_holdt_paamelding

    def noen_feiler(c, kurs_id, pid, *a, **kw):
        if pid == ut[1][0]:
            raise RuntimeError(f"laast database for {ut[1][1]}")
        return ekte(c, kurs_id, pid, *a, **kw)
    monkeypatch.setattr(behandling, "behandle_holdt_paamelding", noen_feiler)
    fra = _siste_hendelse_id(con)
    html = _tekst(_post(_klient(), kid, _ider(ut)))
    assert [_kategori(html, n) for _, _, n in ut] == [FULLFORT, FEIL, FULLFORT]
    assert ut[1][1] not in " ".join(json.dumps(h) for h in _hendelser(con, "manuell_behandling_utlost", fra))  # ingen PII


# ==================== L-M: ukjent Graph/Visma - aldri automatisk retry ====================

def test_L_ukjent_graph_gir_ingen_ny_sending_ved_ny_bulk_eller_global_sveiper(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 2)
    kall = _teller(monkeypatch, feil_epost=(ut[0][1],))
    klient = _klient()
    _post(klient, kid, _ider(ut))
    antall = len(kall["epost"])
    html2 = _tekst(_post(klient, kid, _ider(ut)))          # ny bulk
    _global_sveiper()                                      # daglig jobb
    assert len(kall["epost"]) == antall
    assert _kategori(html2, ut[0][2]) == UAVKLART and "tidligere" in html2.lower()
    assert _kategori(html2, ut[1][2]) == ALLEREDE
    assert _tell(con, "SELECT status FROM utsending_logg WHERE mottaker=?", ut[0][1]) == "ukjent"


def test_M_ukjent_visma_gir_ingen_ny_faktura(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 2)
    kall = _teller(monkeypatch, feil_visma=(ut[0][1],))
    klient = _klient()
    html = _tekst(_post(klient, kid, _ider(ut)))
    assert _kategori(html, ut[0][2]) == UAVKLART and _kategori(html, ut[1][2]) == FULLFORT
    assert kall["epost"].count(ut[0][1]) == 1 and kall["visma"].count(ut[0][1]) == 1
    _post(klient, kid, _ider(ut))
    _global_sveiper()
    assert kall["visma"].count(ut[0][1]) == 1 and kall["epost"].count(ut[0][1]) == 1
    assert _tell(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", ut[0][0]) == 0
    assert db.uavklarte_operasjoner(con)["faktura"] == 1


# ==================== N: dobbeltklikk ====================

def test_N_dobbeltklikk_etter_hverandre_gir_ingen_doble_sideeffekter(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 4)
    kall = _teller(monkeypatch)
    klient = _klient()
    _post(klient, kid, _ider(ut))
    html2 = _tekst(_post(klient, kid, _ider(ut)))          # reload/POST paa nytt
    assert all(kall["epost"].count(e) == 1 and kall["visma"].count(e) == 1 for _, e, _ in ut)
    assert all(_kategori(html2, n) == ALLEREDE for _, _, n in ut)


def _parallell(monkeypatch, kid, ut, antall=2):
    """Starter `antall` bulk-requests samtidig (barriere foer den forste raden i hver)."""
    barriere = threading.Barrier(antall, timeout=15)
    ekte, lokal = behandling.behandle_holdt_paamelding, threading.local()

    def med_barriere(*a, **kw):
        if not getattr(lokal, "startet", False):
            lokal.startet = True
            barriere.wait()
        return ekte(*a, **kw)
    monkeypatch.setattr(behandling, "behandle_holdt_paamelding", med_barriere)
    klienter, svar, feil = [_klient() for _ in range(antall)], [None] * antall, []

    def klikk(i):
        try:
            svar[i] = _post(klienter[i], kid, _ider(ut))
        except Exception as e:  # noqa: BLE001
            feil.append(e)
    traader = [threading.Thread(target=klikk, args=(i,)) for i in range(antall)]
    for t in traader:
        t.start()
    for t in traader:
        t.join(60)
    assert not feil, feil
    return svar


@pytest.mark.parametrize("runde", range(3))
def test_N_samtidig_dobbeltklikk_gir_ingen_doble_sideeffekter(con, monkeypatch, runde):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 4)
    kall = _teller(monkeypatch)
    svar = _parallell(monkeypatch, kid, ut)
    assert [s.status_code for s in svar] == [200, 200]
    assert all(kall["epost"].count(e) == 1 and kall["visma"].count(e) == 1 for _, e, _ in ut)
    assert all(_flagg(con, p) == (1, 0) for p in _ider(ut))
    assert _tell(con, "SELECT COUNT(*) FROM faktura") == 4 and _tell(con, "SELECT COUNT(*) FROM faktura_forsok") == 0


# ==================== O-P: overlappende bulk / bulk + global sveiper (deterministisk overlapp) ====================

class _Blokk:
    """Lar den FORSTE epost.send-en (i tilfeldig traad) staa og vente - claimen er da allerede committet."""

    def __init__(self, kall):
        self.kall, self.i_send, self.slipp, self.lock, self.forste = kall, threading.Event(), threading.Event(), threading.Lock(), True

    def __call__(self, til, *a, **kw):
        self.kall["epost"].append(til)
        with self.lock:
            blokker, self.forste = self.forste, False
        if blokker:
            self.i_send.set()
            assert self.slipp.wait(30), "testen slapp aldri den blokkerte sendingen"


def _start_blokkert_bulk(monkeypatch, kid, ut):
    kall = {"epost": [], "visma": []}
    blokk = _Blokk(kall)
    monkeypatch.setattr(epost, "send", blokk)

    def fakturer(g):
        kall["visma"].append(g.kunde_epost)
        return _orig_visma(g)
    monkeypatch.setattr(visma, "fakturer", fakturer)
    klient, svar, feil = _klient(), [], []

    def a():
        try:
            svar.append(_post(klient, kid, _ider(ut)))
        except Exception as e:  # noqa: BLE001
            feil.append(e)
    tr = threading.Thread(target=a)
    tr.start()
    assert blokk.i_send.wait(30)
    return kall, blokk, tr, svar, feil


def test_O_to_overlappende_bulk_gir_ingen_doble_e_poster_eller_fakturaer(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 4)
    kall, blokk, tr, svar_a, feil = _start_blokkert_bulk(monkeypatch, kid, ut)  # A star midt i utsendingen til rad 1

    html_b = _tekst(_post(_klient(), kid, _ider(ut)))                            # B: samme utvalg, samtidig
    assert _kategori(html_b, ut[0][2]) == UAVKLART and "annen behandling" in html_b   # pagar hos A
    assert [_kategori(html_b, n) for _, _, n in ut[1:]] == [FULLFORT] * 3

    blokk.slipp.set()
    tr.join(60)
    assert not feil, feil
    html_a = _tekst(svar_a[0])
    assert _kategori(html_a, ut[0][2]) == FULLFORT
    assert [_kategori(html_a, n) for _, _, n in ut[1:]] == [ALLEREDE] * 3
    assert all(kall["epost"].count(e) == 1 and kall["visma"].count(e) == 1 for _, e, _ in ut)
    assert _tell(con, "SELECT COUNT(*) FROM faktura") == 4
    assert all(_flagg(con, p) == (1, 0) for p in _ider(ut))
    assert db.uavklarte_operasjoner(con)["totalt"] == 0


def test_O_bulk_samtidig_med_fase9_enkeltbehandling_gir_ingen_dobler(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 3)
    kall, blokk, tr, svar_a, feil = _start_blokkert_bulk(monkeypatch, kid, ut)
    klient = _klient()
    resp = klient.post(FASE9.format(kid=kid, pid=ut[0][0]))                       # enkeltbehandling av raden A holder
    assert "uavklart eller pågår" in _tekst(klient.get(resp.headers["Location"]))
    blokk.slipp.set()
    tr.join(60)
    assert not feil, feil
    assert all(kall["epost"].count(e) == 1 and kall["visma"].count(e) == 1 for _, e, _ in ut)
    assert all(_flagg(con, p) == (1, 0) for p in _ider(ut))


def test_P_bulk_samtidig_med_global_sveiper_gir_ingen_dobler_og_ubehandlede_holdes(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 4)
    kall, blokk, tr, svar_a, feil = _start_blokkert_bulk(monkeypatch, kid, ut)
    fer = (list(kall["epost"]), list(kall["visma"]))
    assert [_flagg(con, p) for p in _ider(ut)] == [(0, 1)] * 4          # alle fortsatt holdt mens rad 1 pagar
    _global_sveiper()                                                    # daglig jobb midt i bulk
    assert (kall["epost"], kall["visma"]) == fer                         # global rorte ingenting
    blokk.slipp.set()
    tr.join(60)
    assert not feil, feil
    _global_sveiper()                                                    # og etterpaa: heller ingenting
    assert all(kall["epost"].count(e) == 1 and kall["visma"].count(e) == 1 for _, e, _ in ut)
    assert all(_flagg(con, p) == (1, 0) for p in _ider(ut))


# ==================== stoppregel og tidsbudsjett ====================

def test_V_stopper_etter_tre_paafolgende_uavklarte_og_resten_beholder_holdt_flagg(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 6)
    kall = _teller(monkeypatch, feil_epost=tuple(e for _, e, _ in ut))    # Graph nede for alle
    html = _tekst(_post(_klient(), kid, _ider(ut)))
    assert [_kategori(html, n) for _, _, n in ut] == [UAVKLART] * 3 + [IKKE_FORSOKT] * 3
    assert kall["epost"] == [e for _, e, _ in ut[:3]]                       # kun 3 forsok - aldri retry
    assert all(_flagg(con, p) == (0, 0) for p in _ider(ut[:3]))
    assert all(_flagg(con, p) == (0, 1) for p in _ider(ut[3:]))            # holdt: global sveiper rorer dem ikke
    agg = _hendelser(con, "bulk_behandling_utlost")[0]
    assert (agg["stoppet"], agg["stoppgrunn"], agg["uavklart"], agg["ikke_forsokt"]) == (True, "uavklart_rad", 3, 3)
    assert "stoppet" in html.lower()
    _global_sveiper()
    assert len(kall["epost"]) == 3                                          # heller ikke av den globale
    monkeypatch.setattr(epost, "send", lambda til, *a, **kw: kall["epost"].append(til))  # Graph er oppe igjen
    _post(_klient(), kid, _ider(ut[3:]))                                    # admin velger resten paa nytt
    assert [_flagg(con, p) for p in _ider(ut[3:])] == [(1, 0)] * 3


def test_V_pagar_hos_annen_teller_ikke_som_feil(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 5)
    for _, e, _ in ut[:4]:
        db.reserver_sending(con, f"kurs:{kid}", e, "bekreftelse")           # fire rader pagar hos andre (ferske)
    con.commit()
    kall = _teller(monkeypatch)
    html = _tekst(_post(_klient(), kid, _ider(ut)))
    assert [_kategori(html, n) for _, _, n in ut] == [UAVKLART] * 4 + [FULLFORT]
    assert _hendelser(con, "bulk_behandling_utlost")[0]["stoppet"] is False   # 4 x pagar stopper ikke
    assert kall["epost"] == [ut[4][1]]


def test_V_fullfort_nullstiller_telleren(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 7)
    feil = {ut[i][1] for i in (0, 1, 3, 4)}                                   # fail, fail, OK, fail, fail, OK, OK
    _teller(monkeypatch, feil_epost=tuple(feil))
    html = _tekst(_post(_klient(), kid, _ider(ut)))
    assert [_kategori(html, n) for _, _, n in ut] == [UAVKLART, UAVKLART, FULLFORT, UAVKLART, UAVKLART, FULLFORT, FULLFORT]
    assert _hendelser(con, "bulk_behandling_utlost")[0]["stoppet"] is False


def test_V_hoppet_over_rader_teller_ikke_som_feil_og_nullstiller_ikke(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 6)
    for pid in _ider(ut[2:4]):
        con.execute("UPDATE paamelding SET sveiper_kjort=1, sveiper_utsatt=0 WHERE id=?", (pid,))
    con.commit()
    _teller(monkeypatch, feil_epost=(ut[0][1], ut[1][1], ut[4][1]))          # fail, fail, skip, skip, fail(=3.) -> stopp
    html = _tekst(_post(_klient(), kid, _ider(ut)))
    assert [_kategori(html, n) for _, _, n in ut] == [UAVKLART, UAVKLART, ALLEREDE, ALLEREDE, UAVKLART, IKKE_FORSOKT]
    assert _hendelser(con, "bulk_behandling_utlost")[0]["stoppgrunn"] == "uavklart_rad"


def test_W_tidsbudsjettet_stopper_nye_rader_men_avbryter_aldri_en_pagaaende(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 4)
    klokke = {"t": 0.0}
    monkeypatch.setattr(webapp, "_klokke", lambda: klokke["t"])
    kall = {"epost": [], "visma": []}

    def send(til, *a, **kw):
        kall["epost"].append(til)
        klokke["t"] += 100.0                                                 # hver sending "tar" 100 s
    monkeypatch.setattr(epost, "send", send)
    monkeypatch.setattr(visma, "fakturer", lambda g: (kall["visma"].append(g.kunde_epost), _orig_visma(g))[1])
    html = _tekst(_post(_klient(), kid, _ider(ut)))
    # rad 1 starter ved t=0, rad 2 ved t=100 (<120) og FULLFORES selv om den passerer grensen, rad 3 ved t=200 stoppes
    assert [_kategori(html, n) for _, _, n in ut] == [FULLFORT, FULLFORT, IKKE_FORSOKT, IKKE_FORSOKT]
    assert kall["epost"] == [ut[0][1], ut[1][1]] and kall["visma"] == [ut[0][1], ut[1][1]]
    assert [_flagg(con, p) for p in _ider(ut)] == [(1, 0), (1, 0), (0, 1), (0, 1)]
    agg = _hendelser(con, "bulk_behandling_utlost")[0]
    assert (agg["stoppet"], agg["stoppgrunn"], agg["ikke_forsokt"]) == (True, "tidsbudsjett", 2)


def test_W_brukt_opp_budsjett_fra_start_forsoker_ingen(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 2)
    kall = _teller(monkeypatch)
    monkeypatch.setattr(webapp, "BULK_TIDSBUDSJETT_SEK", 0)
    html = _tekst(_post(_klient(), kid, _ider(ut)))
    assert kall == {"epost": [], "visma": []} and all(_kategori(html, n) == IKKE_FORSOKT for _, _, n in ut)
    assert all(_flagg(con, p) == (0, 1) for p in _ider(ut))


def test_W_budsjettet_bruker_monotonic_tid_og_er_120_sekunder():
    import time
    assert webapp._klokke is time.monotonic and webapp.BULK_TIDSBUDSJETT_SEK == 120 and webapp.BULK_STOPP_ETTER == 3


# ==================== Q-S: resultatside, personvern, aggregat ====================

def test_Q_resultatsiden_er_no_store_admin_only_og_viser_navn_til_admin(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 1)
    _teller(monkeypatch)
    resp = _post(_klient(), kid, _ider(ut))
    assert resp.headers["Cache-Control"] == "no-store"
    html = _tekst(resp)
    assert ut[0][2] in html and ut[0][1] in html and "Unngå å laste siden på nytt" in html
    assert _post(_klient(innlogget=False), kid, _ider(ut)).status_code == 302   # uautentisert: ingenting vises


def test_R_ingen_persondata_i_hendelseslogg_ogsaa_ved_feil(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 4)
    _teller(monkeypatch)
    monkeypatch.setattr(epost, "send", lambda til, *a, **kw: (_ for _ in ()).throw(RuntimeError(f"Graph: {til} {ut[0][2]}"))
                        if til == ut[0][1] else None)
    monkeypatch.setattr(visma, "fakturer", lambda g: (_ for _ in ()).throw(_http_feil(g.kunde_epost))
                        if g.kunde_epost == ut[1][1] else _orig_visma(g))
    fra = _siste_hendelse_id(con)
    _post(_klient(), kid, _ider(ut))
    alt = " ".join(r["detaljer"] or "" for r in con.execute("SELECT detaljer FROM hendelse WHERE id>?", (fra,))).lower()
    alt += " ".join(r["feilmelding"] or "" for r in con.execute("SELECT feilmelding FROM faktura_forsok")).lower()
    for _, e, navn in ut:
        assert e.lower() not in alt and navn.lower() not in alt and e.split("@")[0] not in alt
    assert "person " not in alt and "@" not in alt and "customers" not in alt


def test_S_aggregert_hendelse_har_kun_ider_og_tellinger_og_riktig_aktor(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 3)
    _teller(monkeypatch, feil_epost=(ut[1][1],))
    fra = _siste_hendelse_id(con)
    _post(_klient(), kid, _ider(ut))
    rader = con.execute("SELECT detaljer, aktor FROM hendelse WHERE handling='bulk_behandling_utlost' AND id>?", (fra,)).fetchall()
    assert len(rader) == 1 and rader[0]["aktor"] == f"admin:{config.ADMIN_BRUKERNAVN}"
    agg = json.loads(rader[0]["detaljer"])
    assert set(agg) == {"kurs_id", "bulk_id", "antall_valgt", "fullfort", "uavklart", "allerede_behandlet",
                        "ikke_behandlingsbar", "feilet", "ikke_forsokt", "stoppet", "stoppgrunn"}
    assert re.fullmatch(r"[0-9a-f]{16}", agg["bulk_id"]) and agg["kurs_id"] == kid
    assert (agg["antall_valgt"], agg["fullfort"], agg["uavklart"], agg["stoppet"], agg["stoppgrunn"]) == (3, 2, 1, False, None)
    rad = _hendelser(con, "manuell_behandling_utlost", fra)
    assert len(rad) == 3 and {h["via"] for h in rad} == {"bulk"} and {h["bulk_id"] for h in rad} == {agg["bulk_id"]}
    assert sorted(h["resultat"] for h in rad) == ["fullfort", "fullfort", "uavklart"]
    assert all(set(h) == {"paamelding_id", "kurs_id", "resultat", "via", "bulk_id"} for h in rad)


def test_S_aggregatet_logges_ogsaa_ved_uventet_avbrudd(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 3)
    _teller(monkeypatch)
    ekte = behandling.behandle_holdt_paamelding

    class Krasj(BaseException):
        pass

    def krasj_paa_rad_2(c, kurs_id, pid, *a, **kw):
        if pid == ut[1][0]:
            raise Krasj()
        return ekte(c, kurs_id, pid, *a, **kw)
    monkeypatch.setattr(behandling, "behandle_holdt_paamelding", krasj_paa_rad_2)
    with pytest.raises(Krasj):
        _post(_klient(), kid, _ider(ut))
    agg = _hendelser(con, "bulk_behandling_utlost")[0]
    assert agg["antall_valgt"] == 3 and agg["fullfort"] == 1


# ==================== T, Z: samme motor som fase 9, ingen egen e-post-/fakturalogikk i ruten ====================

def test_T_fase9_og_bulk_bruker_samme_helper(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 2)
    _teller(monkeypatch)
    kall = []
    ekte = behandling.behandle_holdt_paamelding
    monkeypatch.setattr(behandling, "behandle_holdt_paamelding",
                        lambda *a, **kw: (kall.append(kw.get("via", "enkelt")), ekte(*a, **kw))[1])
    klient = _klient()
    klient.post(FASE9.format(kid=kid, pid=ut[0][0]))
    _post(klient, kid, [ut[1][0]])
    assert kall == ["enkelt", "bulk"]


def test_Z_bulkruten_kaller_aldri_epost_visma_faktura_eller_utsending_logg_direkte():
    """AST-sjekk av selve rute-funksjonen (uten docstring): ingen epost/visma-navn, ingen send/fakturer/
    send_en_gang/sveiper-kall, ingen SQL som skriver til faktura/utsending_logg - kun behandling.behandle_holdt_paamelding."""
    import ast
    import textwrap
    tre = ast.parse(textwrap.dedent(inspect.getsource(webapp.admin_bulk_behandle)))
    funksjon = tre.body[0]
    funksjon.body = funksjon.body[1:]  # fjern docstring
    navn = {n.id for n in ast.walk(funksjon) if isinstance(n, ast.Name)}
    attributter = {n.attr for n in ast.walk(funksjon) if isinstance(n, ast.Attribute)}
    tekster = [n.value for n in ast.walk(funksjon) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert not ({"epost", "visma", "sveiper", "Kjoring"} & navn)
    assert not ({"send", "fakturer", "send_en_gang", "kjor"} & attributter)
    assert not [s for s in tekster if re.search(r"(INSERT|UPDATE|DELETE)|utsending_logg|faktura", s, re.I)]
    assert "behandle_holdt_paamelding" in attributter


def test_Z_bulk_er_ikke_en_transaksjon_rundt_helheten(con, monkeypatch):
    """Hver rad committes for neste starter: mens rad 2 sender, er rad 1 allerede synlig for en annen forbindelse."""
    kid = _kurs(con)
    ut = _deltakere(con, kid, 2)
    sett = {}
    ekte_visma = _orig_visma

    def send(til, *a, **kw):
        if til == ut[1][1]:
            c2 = db.koble()
            sett["rad1"] = (c2.execute("SELECT sveiper_kjort FROM paamelding WHERE id=?", (ut[0][0],)).fetchone()[0],
                            c2.execute("SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", (ut[0][0],)).fetchone()[0])
            c2.execute("BEGIN IMMEDIATE")   # ingen skrivelaas holdes over eksternt kall
            c2.rollback()
            c2.close()
    monkeypatch.setattr(epost, "send", send)
    monkeypatch.setattr(visma, "fakturer", ekte_visma)
    _post(_klient(), kid, _ider(ut))
    assert sett["rad1"] == (1, 1)


# ====================================================================================
# Maksgrensen (50) gjelder ANTALL RAA request-felt - foer parsing, filtrering, deduplisering og DB-oppslag.
# Gjelder BAADE forhandsvisning og behandling (felles hjelper).
# ====================================================================================

RUTER = [FORHANDSVIS, BEHANDLE]
RUTE_NAVN = ["forhandsvis", "behandle"]
MALFORMED = ["abc", "", "1.5", "x1", "--2", " "]


def _raa(klient, rute, kid, raa) -> object:
    """Poster RAA feltverdier uten noen forhaandsfiltrering fra testen."""
    return klient.post(rute.format(kid=kid), data={"paamelding_id": [str(x) for x in raa]})


def _melding_side(klient, resp) -> str:
    assert resp.status_code == 302
    return _tekst(klient.get(resp.headers["Location"]))


def _avvist_som_for_mange(klient, resp, antall_raa):
    side = _melding_side(klient, resp)
    assert f"maks 50 deltakere om gangen (valgte {antall_raa})" in side, side[-400:]   # meldingen viser RAA antall


def _ingen_behandling(con, kall, ut):
    assert kall == {"epost": [], "visma": []}
    assert all(_flagg(con, pid) == (0, 1) for pid in _ider(ut))                        # ingen endring av noen paamelding
    assert _tell(con, "SELECT COUNT(*) FROM utsending_logg") == 0
    assert _tell(con, "SELECT COUNT(*) FROM hendelse WHERE handling IN ('bulk_behandling_utlost','manuell_behandling_utlost')") == 0


@pytest.mark.parametrize("rute", RUTER, ids=RUTE_NAVN)
def test_raa_A_51_kopier_av_samme_gyldige_id_avvises(con, monkeypatch, rute):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 1)
    kall = _teller(monkeypatch)
    klient = _klient()
    _avvist_som_for_mange(klient, _raa(klient, rute, kid, [ut[0][0]] * 51), 51)       # dedupe ville gitt 1 - irrelevant
    _ingen_behandling(con, kall, ut)


@pytest.mark.parametrize("rute", RUTER, ids=RUTE_NAVN)
def test_raa_B_51_malformede_verdier_avvises(con, monkeypatch, rute):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 1)
    kall = _teller(monkeypatch)
    klient = _klient()
    side = _melding_side(klient, _raa(klient, rute, kid, ["abc"] * 51))
    assert "maks 50 deltakere om gangen (valgte 51)" in side and "Velg minst" not in side   # parsing ville gitt 0 - irrelevant
    _ingen_behandling(con, kall, ut)


@pytest.mark.parametrize("rute", RUTER, ids=RUTE_NAVN)
def test_raa_C_51_blandede_verdier_avvises(con, monkeypatch, rute):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 10)
    gyldige = _ider(ut)
    blandet = gyldige + gyldige[:5] * 3 + [MALFORMED[i % len(MALFORMED)] for i in range(26)]   # 10 + 15 dup + 26 ugyldige
    assert len(blandet) == 51 and len(set(map(str, gyldige))) == 10
    kall = _teller(monkeypatch)
    klient = _klient()
    _avvist_som_for_mange(klient, _raa(klient, rute, kid, blandet), 51)
    _ingen_behandling(con, kall, ut)


@pytest.mark.parametrize("rute", RUTER, ids=RUTE_NAVN)
def test_raa_D_noyaktig_50_raa_felt_avvises_ikke_paa_grunn_av_grensen(con, monkeypatch, rute):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 10)
    _teller(monkeypatch)
    klient = _klient()
    gyldige = _ider(ut)

    # (i) 50 kopier av samme id: ikke avvist av grensen (normal dedupe -> 1 deltaker)
    resp = _raa(klient, rute, kid, [gyldige[0]] * 50)
    if rute == FORHANDSVIS:
        assert resp.status_code == 200 and "1 av 1" in _tekst(resp)
    else:
        assert resp.status_code == 200 and _kategori(_tekst(resp), ut[0][2]) == FULLFORT

    # (ii) 50 malformede: ikke avvist av grensen - vanlig "Velg minst én deltaker" (0 gyldige)
    side = _melding_side(klient, _raa(klient, rute, kid, ["abc"] * 50))
    assert "Velg minst" in side and "maks 50" not in side

    # (iii) 50 blandede (10 gyldige + 15 duplikater + 25 ugyldige): normal behandling av de gyldige
    blandet = gyldige + gyldige[:5] * 3 + [MALFORMED[i % len(MALFORMED)] for i in range(25)]
    assert len(blandet) == 50
    resp = _raa(klient, rute, kid, blandet)
    assert resp.status_code == 200
    assert "maks 50" not in _tekst(resp)


@pytest.mark.parametrize("rute", RUTER, ids=RUTE_NAVN)
@pytest.mark.parametrize("antall_raa,gyldige_unike_i_db,avvises", [
    (51, 0, True),      # 51 raa, ingen finnes i DB
    (51, 1, True),      # 51 raa, 1 unik gyldig
    (51, 3, True),      # 51 raa, 3 unike gyldige som finnes
    (52, 51, True),     # flere enn 50 unike ogsaa
    (50, 0, False),     # 50 raa, ingen finnes i DB -> IKKE avvist av grensen
    (50, 3, False),     # 50 raa, 3 unike -> ikke avvist
    (50, 50, False),    # 50 raa, 50 unike som alle finnes -> ikke avvist
    (1, 1, False),
])
def test_raa_E_grensen_er_antall_raa_felt_ikke_gyldige_unike_eller_deltakere_i_db(con, monkeypatch, rute, antall_raa, gyldige_unike_i_db, avvises):
    kid = _kurs(con)
    ut = _deltakere(con, kid, gyldige_unike_i_db) if gyldige_unike_i_db else []
    _teller(monkeypatch)
    klient = _klient()
    gyldige = _ider(ut)
    ikke_i_db = [900000 + i for i in range(antall_raa)]                                 # gyldige heltall som ikke finnes
    raa = (gyldige + gyldige * 20 + ikke_i_db)[:antall_raa] if gyldige else ikke_i_db[:antall_raa]
    assert len(raa) == antall_raa
    resp = _raa(klient, rute, kid, raa)
    if avvises:
        _avvist_som_for_mange(klient, resp, antall_raa)
    else:
        if resp.status_code == 302:
            assert "maks 50" not in _tekst(klient.get(resp.headers["Location"]))
        else:
            assert resp.status_code == 200 and "maks 50" not in _tekst(resp)


def test_raa_50_unike_doblet_er_100_raa_felt_og_avvises(con, monkeypatch):
    """Tidligere (gammel semantikk) ble dette akseptert fordi det ble dedupet FOER grensen."""
    kid = _kurs(con)
    ut = _deltakere(con, kid, 50)
    kall = _teller(monkeypatch)
    klient = _klient()
    for rute in RUTER:
        _avvist_som_for_mange(klient, _raa(klient, rute, kid, _ider(ut) + _ider(ut)), 100)
    _ingen_behandling(con, kall, ut)


def test_raa_duplikater_innenfor_50_raa_felt_dedupes_som_for(con, monkeypatch):
    kid = _kurs(con)
    ut = _deltakere(con, kid, 25)
    kall = _teller(monkeypatch)
    html = _tekst(_raa(_klient(), BEHANDLE, kid, _ider(ut) + _ider(ut)))                 # 50 raa, 25 unike
    assert len(kall["epost"]) == 25 and len(kall["visma"]) == 25 and html.count(FULLFORT) >= 25


def test_raa_avvisning_skjer_foer_db_oppslag(con, monkeypatch):
    """Ved for mange raa felt gjores INGEN spoerring mot paamelding/deltaker/kurs-deltakerdata (grensen kommer foerst)."""
    kid = _kurs(con)
    _deltakere(con, kid, 1)
    klient = _klient()
    sql = []
    ekte_koble = db.koble

    def sporende_koble(*a, **kw):
        c = ekte_koble(*a, **kw)
        c.set_trace_callback(lambda s: sql.append(s))
        return c
    monkeypatch.setattr(db, "koble", sporende_koble)
    for rute in RUTER:
        sql.clear()
        resp = _raa(klient, rute, kid, ["abc"] * 51)
        oppslag = [s for s in sql if "FROM paamelding" in s or "JOIN deltaker" in s]         # kun selve POST-en
        assert oppslag == [], oppslag
        _avvist_som_for_mange(klient, resp, 51)                                             # (redirect-siden spor selv, saa den leses etterpaa)
