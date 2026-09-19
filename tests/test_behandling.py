"""Fase 11, trinn 3A: kurs/behandling.py - felles, tynn orkestrering av EN holdt paamelding.

Testene her gaar rett mot helperen (uten Flask). Selve sendingen/faktureringen gjores av den eksisterende
motoren - her bevises tolkningen av utfall, flagg-semantikken og at helperen ikke har egen motor.
"""
import inspect
import json
from datetime import date, timedelta

import pytest

from kurs import behandling, config, db, sveiper
from kurs.behandling import (ALLEREDE_BEHANDLET, FEILET, FULLFORT, IKKE_BEHANDLINGSBAR, UAVKLART,
                             Behandlingsresultat, behandle_holdt_paamelding, klassifiser)
from kurs.integrasjoner import epost, visma

IDAG = date.today()
_orig_visma = visma.fakturer


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _kurs(con, kode="T1", **kw):
    return db.opprett_kurs(con, kode=kode, navn="Testkurs", datoer=[(IDAG + timedelta(days=30)).isoformat()],
                           sharepoint_mappe=f"Kurs/{kode}", **{"pris_nok": 1000, "fakturering": "person", **kw})


def _holdt(con, kid, epost_="a@x.no", navn="A Person", utsatt=1):
    pid, _ = db.meld_paa(con, kid, epost=epost_, navn=navn, aktor="admin:test", tillat_utkast=True,
                         paamelding={"kilde": "admin", "sveiper_utsatt": utsatt})
    con.commit()
    return pid


def _teller(monkeypatch):
    kall = {"epost": [], "visma": []}
    monkeypatch.setattr(epost, "send", lambda til, *a, **kw: kall["epost"].append(til))

    def _visma(g):
        kall["visma"].append(g.kunde_epost)
        return _orig_visma(g)
    monkeypatch.setattr(visma, "fakturer", _visma)
    return kall


def _rad_og_kurs(con, kid, pid):
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    rad = con.execute(behandling._SQL_RAD, (pid, kid)).fetchone()
    return kurs, rad


def _flagg(con, pid):
    r = con.execute("SELECT sveiper_kjort, sveiper_utsatt FROM paamelding WHERE id=?", (pid,)).fetchone()
    return r["sveiper_kjort"], r["sveiper_utsatt"]


def _tell(con, sql, *a):
    return con.execute(sql, a).fetchone()[0]


# ==================== klassifiser: samme regler for preview, bulk og fase 9 ====================

def test_klar_rad_gir_none(con):
    kid = _kurs(con)
    pid = _holdt(con, kid)
    assert klassifiser(con, *_rad_og_kurs(con, kid, pid)) is None


@pytest.mark.parametrize("oppsett,kode,arsak_kode,tekstbit", [
    ("kurs_utkast", IKKE_BEHANDLINGSBAR, "kurs_utkast", "utkast"),
    ("kurs_avlyst", IKKE_BEHANDLINGSBAR, "kurs_avlyst", "avlyst"),
    ("avmeldt", IKKE_BEHANDLINGSBAR, "avmeldt", "Avmeldt"),
    ("kjort", ALLEREDE_BEHANDLET, "allerede_behandlet", "Allerede behandlet"),
    ("venteliste_sendt", ALLEREDE_BEHANDLET, "allerede_behandlet", "Allerede behandlet"),
    ("ikke_holdt", IKKE_BEHANDLINGSBAR, "ikke_holdt", "Ikke holdt tilbake"),
    ("tidligere_ukjent", UAVKLART, "tidligere_uavklart", "Uavklart fra tidligere forsøk"),
    ("tidligere_gammel_reservert", UAVKLART, "tidligere_uavklart", "Uavklart fra tidligere forsøk"),
    ("annen_pagar", UAVKLART, "pagar", "annen behandling"),
])
def test_klassifiser_arsaker(con, oppsett, kode, arsak_kode, tekstbit):
    kid = _kurs(con)
    pid = _holdt(con, kid)
    nokkel = f"kurs:{kid}"
    if oppsett == "kurs_utkast":
        con.execute("UPDATE kurs SET status='utkast' WHERE id=?", (kid,))
    elif oppsett == "kurs_avlyst":
        con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kid,))
    elif oppsett == "avmeldt":
        con.execute("UPDATE paamelding SET status='avmeldt' WHERE id=?", (pid,))
    elif oppsett == "kjort":
        con.execute("UPDATE paamelding SET sveiper_kjort=1 WHERE id=?", (pid,))
    elif oppsett == "venteliste_sendt":
        con.execute("UPDATE paamelding SET status='venteliste' WHERE id=?", (pid,))
        db.marker_sendt(con, nokkel, "a@x.no", "venteliste")
    elif oppsett == "ikke_holdt":
        con.execute("UPDATE paamelding SET sveiper_utsatt=0 WHERE id=?", (pid,))
    elif oppsett == "tidligere_ukjent":
        con.execute("UPDATE paamelding SET sveiper_utsatt=0 WHERE id=?", (pid,))
        db.reserver_sending(con, nokkel, "a@x.no", "bekreftelse")
        db.sett_sending_ukjent(con, nokkel, "a@x.no", "bekreftelse")
    elif oppsett == "tidligere_gammel_reservert":
        con.execute("UPDATE paamelding SET sveiper_utsatt=0 WHERE id=?", (pid,))
        db.reserver_sending(con, nokkel, "a@x.no", "bekreftelse")
        con.execute("UPDATE utsending_logg SET sendt_ts=datetime('now', '-6 minutes')")
    elif oppsett == "annen_pagar":
        con.execute("UPDATE paamelding SET sveiper_utsatt=0 WHERE id=?", (pid,))
        db.reserver_sending(con, nokkel, "a@x.no", "bekreftelse")
    con.commit()
    res = klassifiser(con, *_rad_og_kurs(con, kid, pid))
    assert (res.kode, res.arsak_kode) == (kode, arsak_kode)
    assert tekstbit in res.arsak
    assert res.forsokt is False


def test_klassifiser_er_ren_lesing(con):
    kid = _kurs(con)
    pid = _holdt(con, kid)
    tabeller = ("hendelse", "utsending_logg", "faktura", "faktura_forsok", "paamelding")
    for_ = [_tell(con, f"SELECT COUNT(*) FROM {t}") for t in tabeller] + [_flagg(con, pid)]
    klassifiser(con, *_rad_og_kurs(con, kid, pid))
    etter = [_tell(con, f"SELECT COUNT(*) FROM {t}") for t in tabeller] + [_flagg(con, pid)]
    assert for_ == etter and not con.in_transaction


# ==================== behandle_holdt_paamelding: utfall og flagg-semantikk ====================

def test_fullfort_bekreftet_gir_en_epost_en_faktura_og_nullstiller_flagget(con, monkeypatch):
    kid = _kurs(con)
    pid = _holdt(con, kid)
    kall = _teller(monkeypatch)
    res = behandle_holdt_paamelding(con, kid, pid, IDAG, aktor="admin:test")
    assert (res.kode, res.forsokt) == (FULLFORT, True)
    assert kall == {"epost": ["a@x.no"], "visma": ["a@x.no"]}
    assert _flagg(con, pid) == (1, 0)
    assert not con.in_transaction


def test_fullfort_venteliste_gir_beskjed_ingen_faktura(con, monkeypatch):
    kid = _kurs(con, kapasitet=1)
    db.meld_paa(con, kid, epost="forst@x.no", navn="Forst")
    pid = _holdt(con, kid)
    assert con.execute("SELECT status FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == "venteliste"
    kall = _teller(monkeypatch)
    res = behandle_holdt_paamelding(con, kid, pid, IDAG)
    assert res.kode == FULLFORT
    assert kall == {"epost": ["a@x.no"], "visma": []}
    assert _flagg(con, pid) == (0, 0)  # venteliste faar aldri sveiper_kjort=1


def test_ukjent_epost_gir_uavklart_flagg_0_og_aldri_ny_sending(con, monkeypatch):
    kid = _kurs(con)
    pid = _holdt(con, kid)
    kall = {"epost": 0}

    def send(*a, **kw):
        kall["epost"] += 1
        raise RuntimeError("Graph nede")
    monkeypatch.setattr(epost, "send", send)
    res = behandle_holdt_paamelding(con, kid, pid, IDAG)
    assert (res.kode, res.arsak_kode, res.forsokt) == (UAVKLART, "ukjent", True)
    assert _flagg(con, pid) == (0, 0)
    # en ny behandling forsoker ikke noe: raden er ikke lenger holdt tilbake og er uavklart fra for
    res2 = behandle_holdt_paamelding(con, kid, pid, IDAG)
    assert (res2.kode, res2.arsak_kode, res2.forsokt) == (UAVKLART, "tidligere_uavklart", False)
    assert kall["epost"] == 1


def test_ukjent_visma_gir_uavklart_epost_gikk_ut_ingen_ny_faktura(con, monkeypatch):
    kid = _kurs(con)
    pid = _holdt(con, kid)
    kall = _teller(monkeypatch)
    monkeypatch.setattr(visma, "fakturer", lambda g: (kall["visma"].append(1), (_ for _ in ()).throw(RuntimeError("x")))[0])
    res = behandle_holdt_paamelding(con, kid, pid, IDAG)
    assert (res.kode, res.arsak_kode) == (UAVKLART, "ukjent")
    assert _flagg(con, pid) == (0, 0)
    assert len(kall["epost"]) == 1 and len(kall["visma"]) == 1
    behandle_holdt_paamelding(con, kid, pid, IDAG)
    assert len(kall["visma"]) == 1  # aldri automatisk ny faktura


def test_pagar_hos_annen_prosess_gir_uavklart_pagar_uten_sideeffekt(con, monkeypatch):
    kid = _kurs(con)
    pid = _holdt(con, kid)
    db.reserver_sending(con, f"kurs:{kid}", "a@x.no", "bekreftelse")  # en annen prosess har claimen (fersk)
    con.commit()
    kall = _teller(monkeypatch)
    res = behandle_holdt_paamelding(con, kid, pid, IDAG)
    assert (res.kode, res.arsak_kode, res.forsokt) == (UAVKLART, "pagar", True)
    assert kall == {"epost": [], "visma": []}
    assert _flagg(con, pid) == (0, 0)


def test_teknisk_feil_uten_reservasjon_gir_feilet_og_flagg_0(con, monkeypatch):
    kid = _kurs(con)
    pid = _holdt(con, kid)
    monkeypatch.setattr(epost, "render", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("mal")))
    res = behandle_holdt_paamelding(con, kid, pid, IDAG)
    assert (res.kode, res.arsak_kode, res.forsokt) == (FEILET, "teknisk", True)
    assert _flagg(con, pid) == (0, 0)
    assert _tell(con, "SELECT COUNT(*) FROM utsending_logg") == 0


@pytest.mark.parametrize("oppsett", ["kurs_avlyst", "avmeldt", "kjort", "ikke_holdt"])
def test_avvist_rad_rorer_ingenting_hverken_flagg_motor_eller_logg(con, monkeypatch, oppsett):
    kid = _kurs(con)
    pid = _holdt(con, kid)
    if oppsett == "kurs_avlyst":
        con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kid,))
    elif oppsett == "avmeldt":
        con.execute("UPDATE paamelding SET status='avmeldt' WHERE id=?", (pid,))
    elif oppsett == "kjort":
        con.execute("UPDATE paamelding SET sveiper_kjort=1 WHERE id=?", (pid,))
    else:
        con.execute("UPDATE paamelding SET sveiper_utsatt=0 WHERE id=?", (pid,))
    con.commit()
    for_flagg, for_hendelser = _flagg(con, pid), _tell(con, "SELECT COUNT(*) FROM hendelse")
    kall = _teller(monkeypatch)
    res = behandle_holdt_paamelding(con, kid, pid, IDAG)
    assert res.forsokt is False and res.kode in (IKKE_BEHANDLINGSBAR, ALLEREDE_BEHANDLET)
    assert kall == {"epost": [], "visma": []}
    assert _flagg(con, pid) == for_flagg
    assert _tell(con, "SELECT COUNT(*) FROM hendelse") == for_hendelser


def test_paamelding_fra_annet_kurs_avsloerer_ingenting(con, monkeypatch):
    k1, k2 = _kurs(con, kode="T1"), _kurs(con, kode="T2")
    pid = _holdt(con, k2)
    kall = _teller(monkeypatch)
    res = behandle_holdt_paamelding(con, k1, pid, IDAG)
    assert (res.kode, res.arsak_kode) == (IKKE_BEHANDLINGSBAR, "finnes_ikke")
    assert kall == {"epost": [], "visma": []} and _flagg(con, pid) == (0, 1)


def test_avmeldt_underveis_gir_ikke_behandlingsbar_og_rorer_ikke_flagget(con, monkeypatch):
    """Raden blir avmeldt mens motoren kjorer (motorens SQL-filter hopper over den)."""
    kid = _kurs(con)
    pid = _holdt(con, kid)
    ekte = sveiper.kjor

    def kjor_mens_avmeldt(k, *a, **kw):
        k.con.execute("UPDATE paamelding SET status='avmeldt' WHERE id=?", (pid,))
        k.con.commit()
        return ekte(k, *a, **kw)
    monkeypatch.setattr(sveiper, "kjor", kjor_mens_avmeldt)
    kall = _teller(monkeypatch)
    res = behandle_holdt_paamelding(con, kid, pid, IDAG)
    assert (res.kode, res.arsak_kode) == (IKKE_BEHANDLINGSBAR, "avmeldt")
    assert kall == {"epost": [], "visma": []}
    assert _flagg(con, pid) == (0, 1)
    assert _tell(con, "SELECT COUNT(*) FROM hendelse WHERE handling='manuell_behandling_utlost'") == 0


# ==================== logging: kun ID-er, via og bulk_id ====================

def test_hendelsen_har_via_og_bulk_id_og_ingen_persondata(con, monkeypatch):
    kid = _kurs(con)
    pid = _holdt(con, kid, epost_="hemmelig.person@x.no", navn="Hemmelig Person")
    _teller(monkeypatch)
    behandle_holdt_paamelding(con, kid, pid, IDAG, aktor="admin:test", via="bulk", bulk_id="abc123")
    rad = con.execute("SELECT detaljer, aktor FROM hendelse WHERE handling='manuell_behandling_utlost'").fetchone()
    assert json.loads(rad["detaljer"]) == {"paamelding_id": pid, "kurs_id": kid, "resultat": "fullfort",
                                          "via": "bulk", "bulk_id": "abc123"}
    assert rad["aktor"] == "admin:test"
    assert "hemmelig" not in rad["detaljer"].lower()


def test_enkeltbehandling_far_via_enkelt_uten_bulk_id(con, monkeypatch):
    kid = _kurs(con)
    pid = _holdt(con, kid)
    _teller(monkeypatch)
    behandle_holdt_paamelding(con, kid, pid, IDAG)
    d = json.loads(_tell(con, "SELECT detaljer FROM hendelse WHERE handling='manuell_behandling_utlost'"))
    assert d["via"] == "enkelt" and "bulk_id" not in d


# ==================== tynt lag: ingen egen motor ====================

def test_modulen_har_ingen_egen_epost_eller_fakturamotor():
    """Kildekode-sjekk (AST, ikke docstring): ingen import av integrasjoner/requests, ingen skriving til
    utsending_logg/faktura/faktura_forsok, ingen kall til epost.send/visma.fakturer - kun sveiper.kjor()."""
    import ast
    tre = ast.parse(inspect.getsource(behandling))
    importert = set()
    for node in ast.walk(tre):
        if isinstance(node, ast.Import):
            importert |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            importert |= {f"{'.' * node.level}{node.module or ''}", *(a.name for a in node.names)}
    assert not {i for i in importert if "integrasjoner" in i or "requests" in i or i in ("epost", "visma", "m365")}, importert
    attributter = {(n.value.id, n.attr) for n in ast.walk(tre)
                   if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    assert ("epost", "send") not in attributter and ("visma", "fakturer") not in attributter
    assert ("sveiper", "kjor") in attributter
    import re
    sql = [n.value for n in ast.walk(tre) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert not [s for s in sql if re.search(r"(INSERT|UPDATE|DELETE)\s+(INTO\s+|FROM\s+)?(faktura|utsending_logg)", s, re.I)]
    assert "ignorer_utsatt=True" in inspect.getsource(behandling)


# ==================== stoppregel-telling, forventet handling, uavklart-status ====================

@pytest.mark.parametrize("res,forventet", [
    (Behandlingsresultat(FEILET, "teknisk", "x", True), True),
    (Behandlingsresultat(UAVKLART, "ukjent", "x", True), True),
    (Behandlingsresultat(UAVKLART, "pagar", "x", True), False),
    (Behandlingsresultat(UAVKLART, "tidligere_uavklart", "x", False), False),
    (Behandlingsresultat(FULLFORT, "fullfort", "x", True), False),
    (Behandlingsresultat(ALLEREDE_BEHANDLET, "allerede_behandlet", "x", False), False),
    (Behandlingsresultat(IKKE_BEHANDLINGSBAR, "avmeldt", "x", False), False),
])
def test_teller_som_feil(res, forventet):
    assert behandling.teller_som_feil(res) is forventet


def test_forventet_handling(con):
    kid = _kurs(con)
    pid = _holdt(con, kid)
    kurs, rad = _rad_og_kurs(con, kid, pid)
    assert behandling.forventet_handling(kurs, rad) == "Bekreftelse på e-post og faktura"
    gratis = _kurs(con, kode="T2", pris_nok=0)
    kurs2, rad2 = _rad_og_kurs(con, gratis, _holdt(con, gratis))
    assert behandling.forventet_handling(kurs2, rad2) == "Bekreftelse på e-post"
    ventende = dict(rad, status="venteliste")
    assert behandling.forventet_handling(kurs, ventende) == "Ventelistebeskjed på e-post"


def test_uavklart_status_bruker_samme_grense_som_telleren(con):
    kid = _kurs(con)
    pid = _holdt(con, kid)
    nokkel = f"kurs:{kid}"
    status = lambda: db.uavklart_status_for_paamelding(con, kid, "a@x.no", pid, "bekreftelse")  # noqa: E731
    assert status() is None
    db.reserver_sending(con, nokkel, "a@x.no", "bekreftelse")
    assert status() == "pagar"                                      # fersk reservert
    con.execute("UPDATE utsending_logg SET sendt_ts=datetime('now', ?)", (f"-{db.UAVKLART_GRENSE_MIN + 1} minutes",))
    assert status() == "ukjent"                                     # gammel reservert = trolig forlatt
    con.execute("UPDATE utsending_logg SET status='feilet'")
    assert status() is None                                         # kjent, trygg feil hores ikke med
    con.execute("UPDATE utsending_logg SET status='sendt'")
    assert status() is None
    db.reserver_faktura(con, pid, None)
    assert status() == "pagar"
    db.sett_faktura_forsok_ukjent(con, pid, None, "RuntimeError")
    assert status() == "ukjent"


# ==================== typespesifikk e-postsjekk: urelaterte meldinger paavirker ikke behandlingen ====================
# Noekkelen kurs:<id> deles av bekreftelse, venteliste, ukefor, dagfor-*, kursbevis og avlysning.

URELATERTE = ["ukefor", "dagfor-2027-01-12", "kursbevis", "avlysning"]


def _uavklart_rad(con, kid, epost_, type_, status="ukjent"):
    nokkel = f"kurs:{kid}"
    db.reserver_sending(con, nokkel, epost_, type_)
    if status == "ukjent":
        db.sett_sending_ukjent(con, nokkel, epost_, type_)
    elif status == "gammel_reservert":
        con.execute("UPDATE utsending_logg SET sendt_ts=datetime('now', '-6 minutes') WHERE type=?", (type_,))
    con.commit()


@pytest.mark.parametrize("type_", URELATERTE)
def test_A_B_urelatert_uavklart_melding_gir_teknisk_feil_ikke_uavklart(con, monkeypatch, type_):
    """Uavklart ukefor/dagfor/kursbevis/avlysning for samme kurs + mottaker: en malfeil paa BEKREFTELSEN skal
    fortsatt vaere feilet/teknisk (daglig jobb henter inn) - ikke 'uavklart'."""
    kid = _kurs(con)
    pid = _holdt(con, kid)
    _uavklart_rad(con, kid, "a@x.no", type_)
    monkeypatch.setattr(epost, "render", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("mal")))
    res = behandle_holdt_paamelding(con, kid, pid, IDAG)
    assert (res.kode, res.arsak_kode, res.forsokt) == (FEILET, "teknisk", True)
    assert _flagg(con, pid) == (0, 0)


@pytest.mark.parametrize("type_", URELATERTE)
def test_urelatert_uavklart_melding_gjor_ikke_en_ikke_holdt_rad_til_tidligere_uavklart(con, type_):
    kid = _kurs(con)
    pid = _holdt(con, kid, utsatt=0)
    _uavklart_rad(con, kid, "a@x.no", type_)
    res = klassifiser(con, *_rad_og_kurs(con, kid, pid))
    assert (res.kode, res.arsak_kode) == (IKKE_BEHANDLINGSBAR, "ikke_holdt")


@pytest.mark.parametrize("type_", URELATERTE)
def test_urelatert_fersk_reservert_melding_gir_ikke_pagar(con, type_):
    kid = _kurs(con)
    _holdt(con, kid)
    _uavklart_rad(con, kid, "a@x.no", type_, status="reservert")
    assert db.uavklart_status_for_paamelding(con, kid, "a@x.no", 1, "bekreftelse") is None


def test_C_faktisk_uavklart_bekreftelse_gir_fortsatt_uavklart(con, monkeypatch):
    kid = _kurs(con)
    pid = _holdt(con, kid)
    monkeypatch.setattr(epost, "send", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("Graph nede")))
    res = behandle_holdt_paamelding(con, kid, pid, IDAG)          # forsok: bekreftelse blir ukjent
    assert (res.kode, res.arsak_kode) == (UAVKLART, "ukjent")
    # og ogsaa i klassifiseringen naar raden er frigitt (flagg 0) - selv med en urelatert melding ved siden av
    _uavklart_rad(con, kid, "a@x.no", "ukefor")
    res2 = klassifiser(con, *_rad_og_kurs(con, kid, pid))
    assert (res2.kode, res2.arsak_kode) == (UAVKLART, "tidligere_uavklart")


def test_D_faktisk_uavklart_venteliste_gir_fortsatt_uavklart_men_ikke_bekreftelse_for_venteliste(con, monkeypatch):
    kid = _kurs(con, kapasitet=1)
    db.meld_paa(con, kid, epost="forst@x.no", navn="Forst")
    pid = _holdt(con, kid)
    assert con.execute("SELECT status FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == "venteliste"
    # en (irrelevant) uavklart 'bekreftelse'-rad paavirker IKKE en ventelistedeltaker ...
    _uavklart_rad(con, kid, "a@x.no", "bekreftelse")
    monkeypatch.setattr(epost, "render", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("mal")))
    assert behandle_holdt_paamelding(con, kid, pid, IDAG).kode == FEILET
    monkeypatch.undo()
    # ... men en uavklart 'venteliste'-rad gjor det
    con.execute("DELETE FROM utsending_logg")
    con.execute("UPDATE paamelding SET sveiper_utsatt=1 WHERE id=?", (pid,))
    con.commit()
    monkeypatch.setattr(epost, "send", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("Graph nede")))
    res = behandle_holdt_paamelding(con, kid, pid, IDAG)
    assert (res.kode, res.arsak_kode) == (UAVKLART, "ukjent")
    assert _tell(con, "SELECT type FROM utsending_logg WHERE status='ukjent'") == "venteliste"


@pytest.mark.parametrize("type_,status_", [("bekreftelse", "bekreftet"), ("venteliste", "venteliste")])
def test_E_fersk_reservert_relevant_type_gir_pagar(con, type_, status_):
    kid = _kurs(con)
    pid = _holdt(con, kid, utsatt=0)
    con.execute("UPDATE paamelding SET status=? WHERE id=?", (status_, pid))
    _uavklart_rad(con, kid, "a@x.no", type_, status="reservert")
    res = klassifiser(con, *_rad_og_kurs(con, kid, pid))
    assert (res.kode, res.arsak_kode) == (UAVKLART, "pagar")
    assert db.uavklart_status_for_paamelding(con, kid, "a@x.no", pid, type_) == "pagar"
    assert db.uavklart_status_for_paamelding(con, kid, "a@x.no", pid, "ukefor") is None


def test_F_faktura_forsok_per_paamelding_id_er_uendret_og_uavhengig_av_epost_type(con):
    kid = _kurs(con)
    pid = _holdt(con, kid)
    andre = _holdt(con, kid, epost_="b@x.no", navn="B Person")
    for type_ in ("bekreftelse", "venteliste", "ukefor"):   # e-posttypen er irrelevant for fakturadelen
        assert db.uavklart_status_for_paamelding(con, kid, "a@x.no", pid, type_) is None
    db.reserver_faktura(con, pid, None)
    assert db.uavklart_status_for_paamelding(con, kid, "a@x.no", pid, "ukefor") == "pagar"
    db.sett_faktura_forsok_ukjent(con, pid, None, "RuntimeError")
    assert db.uavklart_status_for_paamelding(con, kid, "a@x.no", pid, "ukefor") == "ukjent"
    assert db.uavklart_status_for_paamelding(con, kid, "b@x.no", andre, "bekreftelse") is None  # annen paamelding urort


def test_G_forsidens_live_teller_teller_fortsatt_uavklart_ukefor_og_alle_andre_typer(con):
    from kurs.web import app as webapp
    kid = _kurs(con)
    _holdt(con, kid)
    for i, type_ in enumerate(["ukefor", "dagfor-2027-01-12", "kursbevis", "avlysning", "bekreftelse", "venteliste"]):
        _uavklart_rad(con, kid, f"m{i}@x.no", type_)
    con.execute("INSERT INTO faktura_forsok (paamelding_id, kursdag_id, status, opprettet) "
                "VALUES (1, NULL, 'ukjent', datetime('now'))")
    con.commit()
    assert db.uavklarte_operasjoner(con) == {"epost": 6, "faktura": 1, "totalt": 7}
    klient = webapp.app.test_client()
    klient.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    import re
    html = klient.get("/admin").get_data(as_text=True)
    m = re.search(r"Uavklarte operasjoner som krever manuell kontroll</div><div class=\"stat\"[^>]*>(\d+)<", html)
    assert m and int(m.group(1)) == 7
