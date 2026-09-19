"""Trinn 2.5, rettelsesrunde etter audit: samtidighet, krasj, personvern, resultat-commit, render foer
claim, sveiper_utsatt-race og live "krever kontroll"-teller.

Alle samtidighetstester bruker ekte, separate database-forbindelser mot samme fil (ikke én forbindelse
etter hverandre) og simulerer interleaving ved aa la en annen forbindelse fullfoere hele jobben
akkurat mellom to steg i den forste. Klassifiseringen "sikkert feilet" vs. "ukjent" for Graph/Visma er
fortsatt UAVGJORT - alle feil her er derfor 'ukjent'.
"""
import json
import sqlite3
import threading
from datetime import date, datetime, timedelta

import pytest
import requests

from kurs import config, db, sveiper
from kurs.feil import sikker_feiltekst
from kurs.integrasjoner import epost, visma
from kurs.kjoring import Kjoring

IDAG = date(2027, 3, 1)
EPOST = "kari.nordmann@example.no"
NAVN = "Kari Nordmann"
_orig_visma = visma.fakturer


class Krasj(BaseException):
    """Simulerer at prosessen dor (ikke fanget av `except Exception`)."""


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _kurs(con, start=IDAG, **kw):
    dager = kw.pop("dager", 1)
    datoer = [(start + timedelta(days=n * 30)).isoformat() for n in range(dager)]
    return db.opprett_kurs(con, kode=kw.pop("kode", "T1"), navn="Testkurs", datoer=datoer,
                           sharepoint_mappe="Kurs/T1", **{"pris_nok": 1000, "fakturering": "person", **kw})


def _meld(con, kid, epost_="a@x.no", navn="A", **kw):
    pid, _ = db.meld_paa(con, kid, epost=epost_, navn=navn, **kw)
    con.commit()
    return pid


def _p_rad(con, pid):
    return con.execute(sveiper.SQL_DELTAKER + " WHERE p.id=?", (pid,)).fetchone()


def _tell(con, sql, *args):
    return con.execute(sql, args).fetchone()[0]


def _kan_skrive(sti) -> bool:
    """Kan en ANNEN forbindelse ta skrivelaasen naa? (False = noen holder en skrivelaas.)"""
    c2 = sqlite3.connect(sti, timeout=0.2)
    try:
        c2.execute("BEGIN IMMEDIATE")
        c2.rollback()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        c2.close()


def _teller(monkeypatch):
    """Erstatter epost.send og visma.fakturer med tellende varianter (visma kaller demo-implementasjonen)."""
    kall = {"epost": [], "visma": []}
    monkeypatch.setattr(epost, "send", lambda til, *a, **kw: kall["epost"].append(til))

    def _visma(g):
        kall["visma"].append(g.linjetekst)
        return _orig_visma(g)
    monkeypatch.setattr(visma, "fakturer", _visma)
    return kall


def _samtidig(funksjoner):
    """Kjorer hver funksjon i egen traad med EGEN forbindelse, startet samtidig. Returnerer resultatene."""
    barriere = threading.Barrier(len(funksjoner), timeout=10)
    resultater, feil = [], []

    def kjor(f):
        c = db.koble()
        try:
            barriere.wait()
            r = f(c)
            c.commit()
            resultater.append(r)
        except Exception as e:  # noqa: BLE001
            feil.append(e)
        finally:
            c.close()
    traader = [threading.Thread(target=kjor, args=(f,)) for f in funksjoner]
    for t in traader:
        t.start()
    for t in traader:
        t.join(20)
    assert not feil, feil
    return resultater


# ==================== RETTELSE 1 / T2: faktura-race (interleaving fra auditen) ====================

@pytest.mark.parametrize("betaling,dager,dager_for,forventet", [
    ("samlet", 1, 14, 1),
    ("per_samling", 1, 14, 1),
    ("per_samling", 2, 60, 2),
])
def test_faktura_interleaving_gir_ett_visma_kall_per_faktura(con, monkeypatch, betaling, dager, dager_for, forventet):
    """A sjekker 'finnes faktura?' (nei) -> B fullforer ALT og committer -> A claimer. A skal oppdage at
    fakturaen finnes og IKKE kalle Visma, ryddet, uten IntegrityError."""
    kid = _kurs(con, dager=dager, betaling=betaling, faktura_dager_for=dager_for, pris_nok=2000)
    pid = _meld(con, kid)
    kall = _teller(monkeypatch)
    con_b = db.koble()
    b_kjort = []
    ekte = db.reserver_faktura

    def reserver_etter_b(c, p_id, kd):
        if c is con and not b_kjort:
            b_kjort.append(1)
            sveiper.fakturer(Kjoring(con_b, idag=IDAG), _p_rad(con_b, pid))
            con_b.commit()
        return ekte(c, p_id, kd)
    monkeypatch.setattr(db, "reserver_faktura", reserver_etter_b)

    sveiper.fakturer(Kjoring(con, idag=IDAG), _p_rad(con, pid))  # skal IKKE kaste (ingen IntegrityError)
    con.commit()
    con_b.close()

    assert b_kjort, "testen ma faktisk ha kjort B midt i As sjekk/claim"
    assert len(kall["visma"]) == forventet
    assert _tell(con, "SELECT COUNT(*) FROM faktura") == forventet
    assert _tell(con, "SELECT COUNT(*) FROM faktura_forsok") == 0  # ingen foreldreloes aktiv reservasjon
    assert _tell(con, "SELECT COUNT(*) FROM hendelse WHERE handling IN ('sveip_feil','faktura_ukjent','faktura_feil')") == 0


# ==================== T3: e-post-interleaving ====================

def test_epost_interleaving_gir_kun_en_epost_og_en_faktura(con, monkeypatch):
    kid = _kurs(con)
    pid = _meld(con, kid)
    kall = _teller(monkeypatch)
    con_b = db.koble()
    b_kjort = []
    ekte = db.reserver_sending

    def reserver_etter_b(c, *a):
        if c is con and not b_kjort:
            b_kjort.append(1)
            sveiper.kjor(Kjoring(con_b, idag=IDAG), pid)
            con_b.commit()
        return ekte(c, *a)
    monkeypatch.setattr(db, "reserver_sending", reserver_etter_b)

    sveiper.kjor(Kjoring(con, idag=IDAG), pid)
    con.commit()
    con_b.close()

    assert b_kjort
    assert len(kall["epost"]) == 1
    assert len(kall["visma"]) == 1
    assert _tell(con, "SELECT COUNT(*) FROM faktura") == 1
    assert _tell(con, "SELECT sveiper_kjort FROM paamelding WHERE id=?", pid) == 1


# ==================== T4: retry-/nytt claim fra flere forbindelser -> kun én vinner ====================

def test_nytt_epost_claim_fra_mange_forbindelser_gir_en_vinner(con):
    resultater = _samtidig([lambda c: db.reserver_sending(c, "kurs:1", "a@x.no", "bekreftelse")] * 6)
    assert resultater.count(True) == 1


def test_epost_retry_claim_fra_mange_forbindelser_gir_en_vinner(con):
    db.reserver_sending(con, "kurs:1", "a@x.no", "bekreftelse")
    db.sett_sending_feilet(con, "kurs:1", "a@x.no", "bekreftelse")
    con.commit()
    claim = lambda c: (db.reserver_sending(c, "kurs:1", "a@x.no", "bekreftelse")  # noqa: E731
                       or db.reserver_sending_pa_nytt(c, "kurs:1", "a@x.no", "bekreftelse"))
    assert _samtidig([claim] * 6).count(True) == 1


@pytest.mark.parametrize("kursdag", ["samlet", "dag"])
def test_faktura_retry_claim_fra_mange_forbindelser_gir_en_vinner(con, kursdag):
    kid = _kurs(con, betaling="per_samling")
    pid = _meld(con, kid)
    kd = None if kursdag == "samlet" else db.kursdager(con, kid)[0]["id"]
    db.reserver_faktura(con, pid, kd)
    db.sett_faktura_forsok_feilet(con, pid, kd, "x")
    con.commit()
    claim = lambda c: db.reserver_faktura(c, pid, kd) or db.reserver_faktura_pa_nytt(c, pid, kd)  # noqa: E731
    assert _samtidig([claim] * 6).count(True) == 1


def test_nytt_faktura_claim_med_null_kursdag_fra_mange_forbindelser_gir_en_vinner(con):
    """SQLite: NULL != NULL i vanlig UNIQUE - COALESCE-indeksen skal hindre flere 'samlet'-claims."""
    kid = _kurs(con)
    pid = _meld(con, kid)
    assert _samtidig([lambda c: db.reserver_faktura(c, pid, None)] * 6).count(True) == 1
    assert _tell(con, "SELECT COUNT(*) FROM faktura_forsok") == 1


# ==================== T1: ingen skrivelaas under eksterne kall ====================

def test_ingen_skrivelaas_under_epost_og_visma_kall(con, monkeypatch):
    kid = _kurs(con)
    pid = _meld(con, kid)
    obs = {}

    def send(*a, **kw):
        obs["epost"] = _kan_skrive(config.DB_STI)

    def fakturer(g):
        obs["visma"] = _kan_skrive(config.DB_STI)
        return _orig_visma(g)
    monkeypatch.setattr(epost, "send", send)
    monkeypatch.setattr(visma, "fakturer", fakturer)
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)
    assert obs == {"epost": True, "visma": True}


def test_ingen_skrivelaas_under_visma_etter_tapt_epost_claim(con, monkeypatch):
    """E-posten er allerede sendt -> e-post-claimen er en no-op (tapt). Den skal ikke etterlate en aapen
    skrivetransaksjon som holdes over det paafolgende Visma-kallet."""
    kid = _kurs(con)
    pid = _meld(con, kid)
    nokkel = f"kurs:{kid}"
    db.reserver_sending(con, nokkel, "a@x.no", "bekreftelse")
    db.sett_sendt(con, nokkel, "a@x.no", "bekreftelse")
    con.commit()
    obs = {}

    def fakturer(g):
        obs["visma"] = _kan_skrive(config.DB_STI)
        return _orig_visma(g)
    monkeypatch.setattr(visma, "fakturer", fakturer)
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)
    assert obs == {"visma": True}


def test_ingen_skrivelaas_under_epost_etter_tapt_claim_for_forrige_mottaker(con, monkeypatch):
    kid = _kurs(con, pris_nok=0)
    db.reserver_sending(con, "kurs:1", "forst@x.no", "ukefor")
    db.sett_sendt(con, "kurs:1", "forst@x.no", "ukefor")
    con.commit()
    obs = []
    monkeypatch.setattr(epost, "send", lambda *a, **kw: obs.append(_kan_skrive(config.DB_STI)))
    k = Kjoring(con, idag=IDAG)
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    p = _p_rad(con, _meld(con, kid))
    assert k.send_en_gang("kurs:1", "forst@x.no", "ukefor", "venteliste", p=p, kurs=kurs) is False
    assert not con.in_transaction  # tapt claim etterlater ingen aapen skrivetransaksjon
    assert k.send_en_gang("kurs:1", "neste@x.no", "ukefor", "venteliste", p=p, kurs=kurs) is True
    assert obs == [True]


def test_tapt_faktura_claim_etterlater_ingen_aapen_transaksjon(con):
    kid = _kurs(con)
    pid = _meld(con, kid)
    db.reserver_faktura(con, pid, None)
    db.sett_faktura_forsok_ukjent(con, pid, None, "RuntimeError")
    con.commit()
    sveiper.fakturer(Kjoring(con, idag=IDAG), _p_rad(con, pid))
    assert not con.in_transaction


# ==================== T5: krasj foer/etter e-post og foer/etter Visma ====================

@pytest.mark.parametrize("vindu,epost_status,forsok_status,epost_kall,visma_kall,faktura", [
    ("for_epost", "reservert", None, 1, 0, 0),      # A: krasj i selve send-kallet (claim er committet)
    ("etter_epost", "reservert", None, 1, 0, 0),    # B: e-post gikk ut, resultat ble aldri lagret
    ("for_visma", "sendt", "reservert", 1, 1, 0),   # C: krasj i Visma-kallet
    ("etter_visma", "sendt", "reservert", 1, 1, 0),  # D: Visma OK, lokal faktura-rad aldri committet
])
def test_krasjvinduer_gir_uavklart_uten_automatisk_ny_sideeffekt(
        con, monkeypatch, vindu, epost_status, forsok_status, epost_kall, visma_kall, faktura):
    kid = _kurs(con)
    pid = _meld(con, kid)
    kall = _teller(monkeypatch)
    with monkeypatch.context() as m:
        if vindu == "for_epost":
            m.setattr(epost, "send", lambda til, *a, **kw: (kall["epost"].append(til), (_ for _ in ()).throw(Krasj()))[0])
        elif vindu == "etter_epost":
            m.setattr(db, "sett_sendt", lambda *a: (_ for _ in ()).throw(Krasj()))
        elif vindu == "for_visma":
            m.setattr(visma, "fakturer", lambda g: (kall["visma"].append(1), (_ for _ in ()).throw(Krasj()))[0])
        else:
            m.setattr(db, "fjern_faktura_forsok", lambda *a: (_ for _ in ()).throw(Krasj()))
        with pytest.raises(Krasj):
            sveiper.kjor(Kjoring(con, idag=IDAG), pid)
    con.close()  # prosessen doer: alt ucommittet rulles tilbake

    ny = db.koble()
    sveiper.kjor(Kjoring(ny, idag=IDAG), pid)  # neste kjoring (f.eks. daglig jobb)
    ny.commit()

    assert len(kall["epost"]) == epost_kall
    assert len(kall["visma"]) == visma_kall
    assert _tell(ny, "SELECT status FROM utsending_logg WHERE type='bekreftelse'") == epost_status
    rad = ny.execute("SELECT status FROM faktura_forsok").fetchone()
    assert (rad["status"] if rad else None) == forsok_status
    assert _tell(ny, "SELECT COUNT(*) FROM faktura") == faktura
    assert _tell(ny, "SELECT sveiper_kjort FROM paamelding WHERE id=?", pid) == 0
    ny.close()


# ==================== T6: personvern - ingen persondata i lagrede feil eller driftsutskrift ====================

def _http_feil(epost_: str) -> requests.HTTPError:
    """Ekte requests.HTTPError slik Visma-kundeoppslaget gir den: URL-en inneholder kundens e-post."""
    r = requests.Response()
    r.status_code, r.reason = 500, "Server Error"
    r.url = requests.Request("GET", "https://visma.example/v2/customers",
                             params={"$filter": f"EmailAddress eq '{epost_}'"}).prepare().url
    with pytest.raises(requests.HTTPError) as unntak:
        r.raise_for_status()
    return unntak.value


def _lagret_feiltekst(con, utskrift) -> str:
    rader = con.execute(
        """SELECT detaljer FROM hendelse WHERE handling IN
           ('sveip_feil','faktura_feil','faktura_ukjent','epost_ukjent','faktura_reservert_gammel')""").fetchall()
    forsok = con.execute("SELECT feilmelding FROM faktura_forsok").fetchall()
    return " ".join([r[0] or "" for r in rader] + [r[0] or "" for r in forsok] + list(utskrift)).lower()


def _ingen_persondata(tekst: str):
    for forbudt in ("kari", "nordmann", "example.no", "@", "%40", "customers", "filter"):
        assert forbudt not in tekst, forbudt


def test_hjelperen_beholder_kun_type_og_statuskode():
    feil = _http_feil(EPOST)
    assert EPOST.split("@")[0] in str(feil)  # forutsetning: rå str(e) LEKKER e-posten
    assert sikker_feiltekst(feil) == "HTTPError (HTTP 500)"
    assert sikker_feiltekst(RuntimeError(f"til {EPOST}")) == "RuntimeError"


def test_epostfeil_lagres_uten_persondata_men_med_paamelding_id(con, monkeypatch):
    kid = _kurs(con, pris_nok=0)
    pid = _meld(con, kid, epost_=EPOST, navn=NAVN)
    monkeypatch.setattr(epost, "send", lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError(f"Kunne ikke sende til {EPOST} ({NAVN})")))
    k = Kjoring(con, idag=IDAG)
    sveiper.kjor(k, pid)
    con.commit()
    _ingen_persondata(_lagret_feiltekst(con, k.utskrift))
    detaljer = json.loads(_tell(con, "SELECT detaljer FROM hendelse WHERE handling='epost_ukjent'"))
    assert detaljer["paamelding_id"] == pid
    assert detaljer["feil"] == "RuntimeError"


def test_vismafeil_lagres_uten_persondata(con, monkeypatch):
    kid = _kurs(con)
    pid = _meld(con, kid, epost_=EPOST, navn=NAVN)
    monkeypatch.setattr(visma, "fakturer", lambda g: (_ for _ in ()).throw(_http_feil(EPOST)))
    k = Kjoring(con, idag=IDAG)
    sveiper.kjor(k, pid)
    con.commit()
    tekst = _lagret_feiltekst(con, k.utskrift)
    _ingen_persondata(tekst)
    assert _tell(con, "SELECT feilmelding FROM faktura_forsok") == "HTTPError (HTTP 500)"
    assert "httperror (http 500)" in tekst  # nyttig teknisk info er beholdt


def test_faktura_feil_fra_forfalte_delfakturaer_lagres_uten_persondata(con, monkeypatch):
    kid = _kurs(con, dager=2, betaling="per_samling", faktura_dager_for=14, pris_nok=2000)
    pid = _meld(con, kid, epost_=EPOST, navn=NAVN)
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)  # dag 1 fakturert, sveiper_kjort=1
    con.commit()
    monkeypatch.setattr(visma, "fakturer", lambda g: (_ for _ in ()).throw(_http_feil(EPOST)))
    k = Kjoring(con, idag=IDAG + timedelta(days=30))
    sveiper.forfalte_delfakturaer(k)
    con.commit()
    assert _tell(con, "SELECT COUNT(*) FROM hendelse WHERE handling='faktura_feil'") == 1
    _ingen_persondata(_lagret_feiltekst(con, k.utskrift))


def test_vellykket_kjoring_skriver_ikke_mottaker_eller_navn_i_driftsutskrift(con, monkeypatch):
    kid = _kurs(con)
    pid = _meld(con, kid, epost_=EPOST, navn=NAVN)
    k = Kjoring(con, idag=IDAG)
    sveiper.kjor(k, pid)
    _ingen_persondata(" ".join(k.utskrift).lower())
    assert any("sendt" in linje for linje in k.utskrift) and any("faktura" in linje for linje in k.utskrift)


# ==================== RETTELSE 3 / T7: resultatet er committet umiddelbart ====================

def test_ny_forbindelse_ser_sendt_status_og_ekte_faktura_uten_ekstra_commit(con):
    kid = _kurs(con)
    pid = _meld(con, kid)
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)  # ingen con.commit() her - resultatet skal allerede vaere lagret
    ny = db.koble()
    assert _tell(ny, "SELECT status FROM utsending_logg WHERE type='bekreftelse'") == "sendt"
    assert _tell(ny, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", pid) == 1
    assert _tell(ny, "SELECT COUNT(*) FROM faktura_forsok") == 0
    assert _tell(ny, "SELECT COUNT(*) FROM hendelse WHERE handling='fakturert'") == 1
    ny.close()


def test_send_en_gang_committer_resultatet_og_holder_ingen_transaksjon(con):
    kid = _kurs(con, pris_nok=0)
    p = _p_rad(con, _meld(con, kid))
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    assert Kjoring(con, idag=IDAG).send_en_gang("kurs:1", "a@x.no", "bekreftelse", "venteliste", p=p, kurs=kurs)
    assert not con.in_transaction
    ny = db.koble()
    assert db.allerede_sendt(ny, "kurs:1", "a@x.no", "bekreftelse")
    ny.close()


def test_sendt_ts_settes_til_faktisk_sendetid(con, monkeypatch):
    kid = _kurs(con, pris_nok=0)
    p = _p_rad(con, _meld(con, kid))
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    db.reserver_sending(con, "kurs:1", "a@x.no", "bekreftelse")
    con.execute("UPDATE utsending_logg SET sendt_ts='2000-01-01 00:00:00'")
    con.commit()
    db.sett_sending_feilet(con, "kurs:1", "a@x.no", "bekreftelse")
    con.commit()
    Kjoring(con, idag=IDAG).send_en_gang("kurs:1", "a@x.no", "bekreftelse", "venteliste", p=p, kurs=kurs)
    assert _tell(con, "SELECT sendt_ts FROM utsending_logg") > "2000-01-01 00:00:00"


def test_feil_i_resultatlagring_ruller_tilbake_og_lar_forsoket_staa_reservert(con, monkeypatch):
    """Visma har fakturaen, men lokal lagring feiler: ingenting delvis committet, ingen automatisk retry."""
    kid = _kurs(con)
    pid = _meld(con, kid)
    kall = _teller(monkeypatch)
    ekte_logg = db.logg
    monkeypatch.setattr(db, "logg", lambda con_, handling, *a, **kw: (_ for _ in ()).throw(
        RuntimeError("disk full")) if handling == "fakturert" else ekte_logg(con_, handling, *a, **kw))
    with pytest.raises(RuntimeError):
        sveiper._opprett(Kjoring(con, idag=IDAG), _p_rad(con, pid), None, 1000, "linje")
    assert _tell(con, "SELECT COUNT(*) FROM faktura") == 0
    assert _tell(con, "SELECT status FROM faktura_forsok") == "reservert"
    assert len(kall["visma"]) == 1

    monkeypatch.setattr(db, "logg", ekte_logg)
    sveiper._opprett(Kjoring(con, idag=IDAG), _p_rad(con, pid), None, 1000, "linje")
    assert len(kall["visma"]) == 1  # ingen automatisk nytt Visma-kall
    assert _tell(con, "SELECT COUNT(*) FROM faktura") == 0


# ==================== RETTELSE 4 / T8: render foer claim ====================

def test_malfeil_gir_ingen_reservert_rad_og_kan_proves_igjen(con, monkeypatch):
    kid = _kurs(con, pris_nok=0)
    pid = _meld(con, kid)
    kall = _teller(monkeypatch)
    ekte = epost.render
    monkeypatch.setattr(epost, "render", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("feil i mal")))
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)
    con.commit()
    assert _tell(con, "SELECT COUNT(*) FROM utsending_logg") == 0  # ingen hengende reservert rad
    assert kall["epost"] == []

    monkeypatch.setattr(epost, "render", ekte)  # malen er rettet
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)
    con.commit()
    assert kall["epost"] == ["a@x.no"]
    assert _tell(con, "SELECT status FROM utsending_logg") == "sendt"


# ==================== T9: per_samling ====================

def _per_samling(con, **kw):
    kid = _kurs(con, dager=2, betaling="per_samling", faktura_dager_for=60, pris_nok=2000, **kw)
    return kid, _meld(con, kid), db.kursdager(con, kid)


def test_ukjent_forfalt_delfaktura_holder_sveiper_kjort_0_og_provs_aldri_automatisk_paa_nytt(con, monkeypatch):
    kid, pid, dager = _per_samling(con)
    kall = {"antall": 0}

    def visma_forste_feiler(g):
        kall["antall"] += 1
        if kall["antall"] == 1:
            raise RuntimeError("Visma nede")
        return _orig_visma(g)
    monkeypatch.setattr(visma, "fakturer", visma_forste_feiler)

    sveiper.kjor(Kjoring(con, idag=IDAG), pid)
    con.commit()
    assert db.faktura_forsok_rad(con, pid, dager[0]["id"])["status"] == "ukjent"
    assert _tell(con, "SELECT sveiper_kjort FROM paamelding WHERE id=?", pid) == 0

    sveiper.kjor(Kjoring(con, idag=IDAG), pid)  # dag 2 (aldri forsokt) fakturerer, dag 1 (ukjent) berores IKKE
    con.commit()
    assert kall["antall"] == 2
    assert db.faktura_forsok_rad(con, pid, dager[0]["id"])["status"] == "ukjent"
    assert _tell(con, "SELECT COUNT(*) FROM faktura WHERE kursdag_id=?", dager[0]["id"]) == 0
    assert _tell(con, "SELECT COUNT(*) FROM faktura WHERE kursdag_id=?", dager[1]["id"]) == 1
    assert _tell(con, "SELECT sveiper_kjort FROM paamelding WHERE id=?", pid) == 0  # fortsatt ikke ferdig


def test_feilet_delfaktura_kan_retries_og_gir_ferdig_status(con, monkeypatch):
    kid, pid, dager = _per_samling(con)
    kall = _teller(monkeypatch)
    db.reserver_faktura(con, pid, dager[0]["id"])
    db.sett_faktura_forsok_feilet(con, pid, dager[0]["id"], "test")
    con.commit()
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)
    con.commit()
    assert len(kall["visma"]) == 2
    assert _tell(con, "SELECT COUNT(*) FROM faktura") == 2
    assert _tell(con, "SELECT COUNT(*) FROM faktura_forsok") == 0
    assert _tell(con, "SELECT sveiper_kjort FROM paamelding WHERE id=?", pid) == 1


@pytest.mark.parametrize("status,forventet_kall", [("ukjent", 0), ("feilet", 1)])
def test_forfalte_delfakturaer_retryer_feilet_men_aldri_ukjent(con, monkeypatch, status, forventet_kall):
    kurs_id = _kurs(con, dager=2, betaling="per_samling", faktura_dager_for=14, pris_nok=2000)
    pid = _meld(con, kurs_id)
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)  # dag 1 fakturert, dag 2 ikke forfalt enna
    con.commit()
    dag2 = db.kursdager(con, kurs_id)[1]["id"]
    db.reserver_faktura(con, pid, dag2)
    (db.sett_faktura_forsok_ukjent(con, pid, dag2, "RuntimeError") if status == "ukjent"
     else db.sett_faktura_forsok_feilet(con, pid, dag2, "RuntimeError"))
    con.commit()
    kall = _teller(monkeypatch)
    sveiper.forfalte_delfakturaer(Kjoring(con, idag=IDAG + timedelta(days=30)))
    con.commit()
    assert len(kall["visma"]) == forventet_kall
    assert _tell(con, "SELECT COUNT(*) FROM faktura WHERE kursdag_id=?", dag2) == forventet_kall


# ==================== RETTELSE 5 / T10: sveiper_utsatt ====================

def _holdt(con, kid, **kw):
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A", paamelding={"kilde": "admin", "sveiper_utsatt": 1},
                         aktor="admin:test", tillat_utkast=True, **kw)
    con.commit()
    return pid


def test_global_respekterer_utsatt_malrettet_gjor_det_kun_med_eksplisitt_valg(con, monkeypatch):
    kid = _kurs(con)
    pid = _holdt(con, kid)
    kall = _teller(monkeypatch)

    sveiper.kjor(Kjoring(con, idag=IDAG))                 # global
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)            # malrettet, standard: respekterer flagget
    assert kall == {"epost": [], "visma": []}

    with pytest.raises(ValueError):
        sveiper.kjor(Kjoring(con, idag=IDAG), ignorer_utsatt=True)  # aldri global bypass

    sveiper.kjor(Kjoring(con, idag=IDAG), pid, ignorer_utsatt=True)
    assert len(kall["epost"]) == 1 and len(kall["visma"]) == 1
    assert _tell(con, "SELECT sveiper_utsatt FROM paamelding WHERE id=?", pid) == 1  # nullstilling er kallerens ansvar


RUTE = "/admin/kurs/{kid}/deltaker/{pid}/behandle"


def _klient():
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def _admin_kurs(con, **kw):
    return _kurs(con, start=date.today() + timedelta(days=30), **kw)


def _flash(klient, resp) -> str:
    return klient.get(resp.headers["Location"]).get_data(as_text=True).lower()


def test_admin_behandling_samtidig_med_global_sveiper_gir_kun_en_behandling(con, monkeypatch):
    """Global sveiper kjorer midt i admin-behandlingen (mens e-post og Visma pagar). Flagget staar igjen
    til ETTER behandlingen, saa den globale ser aldri raden - og claim/status hindrer dobler uansett."""
    kid = _admin_kurs(con)
    pid = _holdt(con, kid)
    kall = {"epost": [], "visma": [], "flagg": [], "global": []}
    con_b = db.koble()

    def global_kjoring():
        kall["flagg"].append(_tell(con_b, "SELECT sveiper_utsatt FROM paamelding WHERE id=?", pid))
        before = (len(kall["epost"]), len(kall["visma"]))
        sveiper.kjor(Kjoring(con_b, idag=date.today()))
        con_b.commit()
        kall["global"].append((len(kall["epost"]), len(kall["visma"])) == before)

    def send(til, *a, **kw):
        kall["epost"].append(til)
        global_kjoring()

    def fakturer(g):
        kall["visma"].append(1)
        global_kjoring()
        return _orig_visma(g)
    monkeypatch.setattr(epost, "send", send)
    monkeypatch.setattr(visma, "fakturer", fakturer)

    klient = _klient()
    resp = klient.post(RUTE.format(kid=kid, pid=pid))
    con_b.close()

    assert kall["flagg"] == [1, 1] and kall["global"] == [True, True]
    assert len(kall["epost"]) == 1 and len(kall["visma"]) == 1
    assert _tell(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", pid) == 1
    rad = con.execute("SELECT sveiper_kjort, sveiper_utsatt FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert (rad["sveiper_kjort"], rad["sveiper_utsatt"]) == (1, 0)
    tekst = _flash(klient, resp)
    assert "bekreftelse er sendt og fakturering er behandlet" in tekst
    assert "ikke fullført" not in tekst  # ingen misvisende "ikke fullfort"
    assert json.loads(_tell(con, "SELECT detaljer FROM hendelse WHERE handling='manuell_behandling_utlost'"))[
        "resultat"] == "fullfort"


def test_admin_behandling_under_pagaaende_annen_prosess_sier_uavklart_ikke_feilet(con, monkeypatch):
    kid = _admin_kurs(con, pris_nok=0)
    pid = _holdt(con, kid)
    db.reserver_sending(con, f"kurs:{kid}", "a@x.no", "bekreftelse")  # en annen prosess har claimen (fersk)
    con.commit()
    kall = _teller(monkeypatch)
    klient = _klient()
    resp = klient.post(RUTE.format(kid=kid, pid=pid))
    assert kall == {"epost": [], "visma": []}  # ingenting ble sendt to ganger
    tekst = _flash(klient, resp)
    assert "ikke fullført" in tekst and "uavklart" in tekst and "automatisk på nytt" in tekst
    assert "fanges opp automatisk igjen" not in tekst  # det stemmer ikke for uavklarte
    assert json.loads(_tell(con, "SELECT detaljer FROM hendelse WHERE handling='manuell_behandling_utlost'"))[
        "resultat"] == "uavklart"
    assert _tell(con, "SELECT sveiper_utsatt FROM paamelding WHERE id=?", pid) == 0


def test_sluttsemantikk_ukjent_resultat_frigir_flagget_men_provs_ikke_automatisk_paa_nytt(con, monkeypatch):
    kid = _admin_kurs(con, pris_nok=0)
    pid = _holdt(con, kid)
    antall = []
    monkeypatch.setattr(epost, "send", lambda *a, **kw: (antall.append(1), (_ for _ in ()).throw(RuntimeError("x")))[0])
    klient = _klient()
    resp = klient.post(RUTE.format(kid=kid, pid=pid))
    assert "uavklart" in _flash(klient, resp)
    assert _tell(con, "SELECT sveiper_utsatt FROM paamelding WHERE id=?", pid) == 0
    sveiper.kjor(Kjoring(con, idag=date.today()))  # global (daglig) - skal IKKE prove ukjent paa nytt
    assert antall == [1]


def test_sluttsemantikk_teknisk_feil_uten_reservasjon_hentes_inn_av_daglig(con, monkeypatch):
    kid = _admin_kurs(con, pris_nok=0)
    pid = _holdt(con, kid)
    kall = _teller(monkeypatch)
    ekte = epost.render
    monkeypatch.setattr(epost, "render", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("feil i mal")))
    klient = _klient()
    resp = klient.post(RUTE.format(kid=kid, pid=pid))
    tekst = _flash(klient, resp)
    assert "ikke fullført" in tekst and "fanges opp automatisk igjen" in tekst
    assert json.loads(_tell(con, "SELECT detaljer FROM hendelse WHERE handling='manuell_behandling_utlost'"))[
        "resultat"] == "feilet"
    assert _tell(con, "SELECT sveiper_utsatt FROM paamelding WHERE id=?", pid) == 0

    monkeypatch.setattr(epost, "render", ekte)
    sveiper.kjor(Kjoring(con, idag=date.today()))  # daglig jobb: kjent, ikke-reservert feil -> hentes inn
    assert kall["epost"] == ["a@x.no"]


def test_blokkert_behandling_rorer_ikke_flagget(con):
    kid = _admin_kurs(con)
    pid = _holdt(con, kid)
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kid,))
    con.commit()
    klient = _klient()
    klient.post(RUTE.format(kid=kid, pid=pid))
    assert _tell(con, "SELECT sveiper_utsatt FROM paamelding WHERE id=?", pid) == 1


# ==================== RETTELSE 6: live "krever kontroll"-teller ====================

def _stat(klient) -> int:
    import re
    html = klient.get("/admin").get_data(as_text=True)
    m = re.search(r"Uavklarte operasjoner som krever manuell kontroll</div><div class=\"stat\"[^>]*>(\d+)<", html)
    assert m, "forsiden mangler den nye tellerteksten"
    return int(m.group(1))


def test_teller_teller_kun_uavklarte_og_gamle_reserverte_i_begge_tabeller(con):
    kid = _kurs(con, dager=5, betaling="per_samling")
    pid = _meld(con, kid)
    dager = [d["id"] for d in db.kursdager(con, kid)]
    for nokkel, status in [("a", "sendt"), ("b", "ukjent"), ("c", "reservert"), ("d", "reservert"), ("e", "feilet")]:
        con.execute("INSERT INTO utsending_logg (nokkel, mottaker, type, status) VALUES (?,?,?,?)",
                    (f"kurs:{nokkel}", "a@x.no", "bekreftelse", status))
    con.execute("UPDATE utsending_logg SET sendt_ts=datetime('now', '-6 minutes') WHERE nokkel='kurs:d'")   # gammel
    con.execute("UPDATE utsending_logg SET sendt_ts=datetime('now', '-2 minutes') WHERE nokkel='kurs:c'")   # fersk
    for kd, status in zip([None, *dager[:4]], ["ukjent", "reservert", "reservert", "feilet", "ukjent"]):
        con.execute("INSERT INTO faktura_forsok (paamelding_id, kursdag_id, status, opprettet) VALUES (?,?,?,?)",
                    (pid, kd, status, datetime.now().isoformat(timespec="seconds")))
    gammel = (datetime.now() - timedelta(minutes=db.UAVKLART_GRENSE_MIN + 1)).isoformat(timespec="seconds")
    con.execute("UPDATE faktura_forsok SET opprettet=? WHERE kursdag_id=?", (gammel, dager[0]))  # gammel reservert
    con.commit()
    # e-post: ukjent(b) + gammel reservert(d) = 2. faktura: ukjent x2 + gammel reservert(dag0) = 3.
    assert db.uavklarte_operasjoner(con) == {"epost": 2, "faktura": 3, "totalt": 5}
    assert _stat(_klient()) == 5


def test_teller_bruker_samme_staleness_som_motoren(con):
    kid = _kurs(con)
    pid = _meld(con, kid)
    for minutter, forventet in [(db.UAVKLART_GRENSE_MIN - 1, False), (db.UAVKLART_GRENSE_MIN + 1, True)]:
        con.execute("DELETE FROM faktura_forsok")
        tid = (datetime.now() - timedelta(minutes=minutter)).isoformat(timespec="seconds")
        con.execute("INSERT INTO faktura_forsok (paamelding_id, kursdag_id, status, opprettet) VALUES (?,NULL,'reservert',?)",
                    (pid, tid))
        con.commit()
        assert db.forsok_er_gammel(db.faktura_forsok_rad(con, pid, None)) is forventet
        assert (db.uavklarte_operasjoner(con)["faktura"] == 1) is forventet


def test_teller_er_live_og_ikke_kumulativ(con):
    for handling in ("sveip_feil", "epost_ukjent", "faktura_ukjent", "faktura_reservert_gammel"):
        db.logg(con, handling, {"paamelding_id": 1})
    con.commit()
    klient = _klient()
    assert _stat(klient) == 0  # gamle hendelser teller ikke

    con.execute("INSERT INTO utsending_logg (nokkel, mottaker, type, status) VALUES ('kurs:1','a@x.no','bekreftelse','ukjent')")
    con.commit()
    assert _stat(klient) == 1
    con.execute("UPDATE utsending_logg SET status='sendt'")  # manuelt avklart
    con.commit()
    assert _stat(klient) == 0


# ==================== T11: gammel database migreres ====================

def test_gammel_database_faar_faktura_forsok_og_status_uten_a_miste_data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_STI", tmp_path / "gammel.db")
    c = db.koble()
    db.init(c)
    fersk_faktura_kolonner = [r["name"] for r in c.execute("PRAGMA table_info(faktura)")]
    kid = _kurs(c)
    pid = _meld(c, kid)
    c.execute("INSERT INTO faktura (paamelding_id, kursdag_id, belop_nok, status) VALUES (?,NULL,1000,'sendt')", (pid,))
    # Gjor om til slik databasen saa ut FOR trinn 2.5: ingen faktura_forsok, utsending_logg uten status.
    c.execute("DROP TABLE faktura_forsok")
    c.execute("DROP TABLE utsending_logg")
    c.execute("CREATE TABLE utsending_logg (nokkel TEXT NOT NULL, mottaker TEXT NOT NULL COLLATE NOCASE, "
              "type TEXT NOT NULL, sendt_ts TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE (nokkel, mottaker, type))")
    c.execute("INSERT INTO utsending_logg (nokkel, mottaker, type) VALUES ('kurs:1','gammel@x.no','bekreftelse')")
    c.commit()
    c.close()

    c = db.koble()
    db.init(c)   # oppstart av ny kode mot gammel database
    db.init(c)   # og idempotent ved neste oppstart
    assert db.allerede_sendt(c, "kurs:1", "gammel@x.no", "bekreftelse") is True  # gammel rad = sendt
    assert db.reserver_sending(c, "kurs:1", "ny@x.no", "bekreftelse") is True
    assert c.execute("SELECT status FROM utsending_logg WHERE mottaker='ny@x.no'").fetchone()[0] == "reservert"
    assert db.reserver_faktura(c, pid, None) is True and db.reserver_faktura(c, pid, None) is False  # NULL-unikhet
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO faktura_forsok (paamelding_id, kursdag_id, status) VALUES (999999, NULL, 'reservert')")
    assert [r["name"] for r in c.execute("PRAGMA table_info(faktura)")] == fersk_faktura_kolonner  # faktura uendret
    assert _tell(c, "SELECT COUNT(*) FROM faktura") == 1  # eksisterende data intakt
    c.close()


# ==================== sluttkontroll: samtidige admin-klikk paa samme holdte paamelding ====================

def _endelig_tilstand(con, pid, kall):
    """Felles krav etter to samtidige behandlingsforsok: nøyaktig én av hver sideeffekt, konsistent sluttstatus."""
    assert len(kall["epost"]) == 1, kall["epost"]
    assert len(kall["visma"]) == 1, kall["visma"]
    assert _tell(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", pid) == 1
    assert _tell(con, "SELECT COUNT(*) FROM utsending_logg") == 1
    assert _tell(con, "SELECT status FROM utsending_logg") == "sendt"
    assert _tell(con, "SELECT COUNT(*) FROM faktura_forsok") == 0
    rad = con.execute("SELECT sveiper_kjort, sveiper_utsatt FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert (rad["sveiper_kjort"], rad["sveiper_utsatt"]) == (1, 0)
    assert _tell(con, "SELECT COUNT(*) FROM hendelse WHERE handling IN "
                      "('sveip_feil','faktura_ukjent','faktura_feil','epost_ukjent','faktura_reservert_gammel')") == 0
    assert db.uavklarte_operasjoner(con)["totalt"] == 0


def _forstaelig_melding(tekst: str):
    """Hver request skal ha faatt EN av to korrekte, forstaaelige meldinger - aldri 'feilet'/'fanges opp igjen'."""
    fullfort = "bekreftelse er sendt og fakturering er behandlet" in tekst
    pagaar = "uavklart eller pågår" in tekst and "ikke fullført" in tekst
    assert fullfort or pagaar, tekst[-600:]
    assert "fanges opp automatisk igjen" not in tekst
    return "fullfort" if fullfort else "pagaar"


@pytest.mark.parametrize("runde", range(5))
def test_to_samtidige_admin_klikk_gir_kun_en_behandling(con, monkeypatch, runde):
    """To ekte forbindelser/requests starter behandlingen samtidig (barriere etter at begge har passert
    'er den holdt tilbake?'-sjekken). Kun claims skal avgjore - ingen 500, ingen IntegrityError."""
    kid = _admin_kurs(con)
    pid = _holdt(con, kid)
    kall = _teller(monkeypatch)
    barriere = threading.Barrier(2, timeout=10)
    ekte_kjor = sveiper.kjor

    def kjor_etter_barriere(*a, **kw):
        barriere.wait()
        return ekte_kjor(*a, **kw)
    monkeypatch.setattr(sveiper, "kjor", kjor_etter_barriere)

    klienter = [_klient(), _klient()]
    svar, feil = [None, None], []

    def klikk(i):
        try:
            svar[i] = klienter[i].post(RUTE.format(kid=kid, pid=pid))
        except Exception as e:  # noqa: BLE001
            feil.append(e)
    traader = [threading.Thread(target=klikk, args=(i,)) for i in range(2)]
    for t in traader:
        t.start()
    for t in traader:
        t.join(30)
    assert not feil, feil
    assert [s.status_code for s in svar] == [302, 302]  # ingen 500

    meldinger = [_forstaelig_melding(_flash(klienter[i], svar[i])) for i in range(2)]
    assert "fullfort" in meldinger  # minst en av dem (vinneren, eller den som kom sist) meldte fullfort
    _endelig_tilstand(con, pid, kall)


def test_admin_klikk_mens_forste_behandling_pagaar_og_global_sveiper_kjorer_gir_ingen_dobler(con, monkeypatch):
    """Kontrollert overlapp: forste behandling star midt i epost.send (claim reservert). Da klikker en
    ANNEN admin, og til slutt kjorer den globale sveiperen (flagget er da allerede nullstilt av den andre
    requesten). Ingen av dem skal sende/fakturere noe - og meldingen til den andre skal vaere "pagaar"."""
    kid = _admin_kurs(con)
    pid = _holdt(con, kid)
    kall = {"epost": [], "visma": []}
    i_send, slipp = threading.Event(), threading.Event()

    def send(til, *a, **kw):
        kall["epost"].append(til)
        i_send.set()
        assert slipp.wait(15), "testen slapp aldri forste behandling videre"

    def fakturer(g):
        kall["visma"].append(1)
        return _orig_visma(g)
    monkeypatch.setattr(epost, "send", send)
    monkeypatch.setattr(visma, "fakturer", fakturer)

    a, b = _klient(), _klient()
    svar_a, feil = [], []

    def klikk_a():
        try:
            svar_a.append(a.post(RUTE.format(kid=kid, pid=pid)))
        except Exception as e:  # noqa: BLE001
            feil.append(e)
    tr = threading.Thread(target=klikk_a)
    tr.start()
    assert i_send.wait(15)

    svar_b = b.post(RUTE.format(kid=kid, pid=pid))          # samtidig klikk mens A er midt i utsendingen
    assert svar_b.status_code == 302
    assert _forstaelig_melding(_flash(b, svar_b)) == "pagaar"
    assert _tell(con, "SELECT sveiper_utsatt FROM paamelding WHERE id=?", pid) == 0  # B frigjorde flagget

    con_g = db.koble()
    sveiper.kjor(Kjoring(con_g, idag=date.today()))         # global: raden er na formelt ikke holdt tilbake
    con_g.commit()
    con_g.close()
    assert kall == {"epost": [til for til in kall["epost"]], "visma": []} and len(kall["epost"]) == 1

    slipp.set()
    tr.join(30)
    assert not feil, feil
    assert svar_a[0].status_code == 302
    assert _forstaelig_melding(_flash(a, svar_a[0])) == "fullfort"
    _endelig_tilstand(con, pid, kall)


# ==================== sluttkontroll: tidsbase for staleness (ingen lokal/UTC-miks) ====================

def test_ferske_claims_fra_motoren_telles_ikke_som_uavklarte_uansett_tidssone(con):
    """Rader skrevet av motoren selv (UTC i utsending_logg, lokal Python-tid i faktura_forsok) skal ikke
    ligge foran/bak 5-minuttersgrensen pga. tidssone. Ville en tabell vaert sammenlignet mot feil klokke,
    ville en helt fersk rad (1-2 timers skjevhet) blitt talt som uavklart."""
    kid = _kurs(con)
    pid = _meld(con, kid)
    assert db.reserver_sending(con, "kurs:1", "a@x.no", "bekreftelse")
    assert db.reserver_faktura(con, pid, None)
    con.commit()
    assert db.uavklarte_operasjoner(con) == {"epost": 0, "faktura": 0, "totalt": 0}
    assert db.forsok_er_gammel(db.faktura_forsok_rad(con, pid, None)) is False


def test_staleness_grensen_ved_fire_og_seks_minutter_i_begge_tabeller(con):
    kid = _kurs(con)
    pid = _meld(con, kid)
    db.reserver_sending(con, "kurs:1", "a@x.no", "bekreftelse")
    db.reserver_faktura(con, pid, None)
    for minutter, forventet in [(db.UAVKLART_GRENSE_MIN - 1, 0), (db.UAVKLART_GRENSE_MIN + 1, 1)]:
        con.execute("UPDATE utsending_logg SET sendt_ts=datetime('now', ?)", (f"-{minutter} minutes",))  # UTC
        con.execute("UPDATE faktura_forsok SET opprettet=?",
                    ((datetime.now() - timedelta(minutes=minutter)).isoformat(timespec="seconds"),))  # lokal
        con.commit()
        assert db.uavklarte_operasjoner(con) == {"epost": forventet, "faktura": forventet, "totalt": 2 * forventet}
