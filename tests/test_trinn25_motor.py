"""Trinn 2.5: sikker maalrettet behandlingsarkitektur.

Atomisk claim/retry for e-post (utsending_logg.status) og faktura (egen faktura_forsok-tabell,
faktura selv er uendret), sporbarhet for ukjente/uavklarte forsok (hendelseslogg + forsidens
feilteller). Se dokumentasjon/pindena-ipr-gap-analyse.html for helheten.

Klassifiseringen "sikkert feilet" vs. "ukjent utfall" for Microsoft Graph/Visma er IKKE avklart enna
(se docstring i kjoring.py/sveiper.py) - testene under simulerer derfor en kjent, trygg feil ved aa
kalle db.sett_sending_feilet()/db.sett_faktura_forsok_feilet() direkte, ikke via en ekte unntakstype.
"""
import sqlite3
from datetime import date, timedelta

import pytest

from kurs import config, db, sveiper
from kurs.integrasjoner import epost, visma
from kurs.kjoring import Kjoring


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    return c


def _kurs(con, start: date, **kw):
    dager = kw.pop("dager", 1)
    datoer = [(start + timedelta(days=n * 30)).isoformat() for n in range(dager)]
    return db.opprett_kurs(con, kode=kw.pop("kode", "T1"), navn="Testkurs", datoer=datoer,
                           sharepoint_mappe="Kurs/T1", **{"pris_nok": 1000, **kw})


def _hendelser(con, handling):
    return con.execute("SELECT * FROM hendelse WHERE handling=?", (handling,)).fetchall()


# ==================== utsending_logg: atomisk claim (db.py-primitiver) ====================

def test_reserver_sending_vinner_kun_en_gang(con):
    assert db.reserver_sending(con, "kurs:1", "a@x.no", "bekreftelse") is True
    assert db.reserver_sending(con, "kurs:1", "a@x.no", "bekreftelse") is False


def test_reserver_sending_pa_nytt_krever_feilet(con):
    db.reserver_sending(con, "kurs:1", "a@x.no", "bekreftelse")
    assert db.reserver_sending_pa_nytt(con, "kurs:1", "a@x.no", "bekreftelse") is False  # fortsatt reservert
    db.sett_sending_feilet(con, "kurs:1", "a@x.no", "bekreftelse")
    assert db.reserver_sending_pa_nytt(con, "kurs:1", "a@x.no", "bekreftelse") is True
    assert db.reserver_sending_pa_nytt(con, "kurs:1", "a@x.no", "bekreftelse") is False  # allerede reservert paa nytt


def test_retry_etter_sendt_gjor_ingenting(con):
    db.reserver_sending(con, "kurs:1", "a@x.no", "bekreftelse")
    db.sett_sendt(con, "kurs:1", "a@x.no", "bekreftelse")
    assert db.reserver_sending(con, "kurs:1", "a@x.no", "bekreftelse") is False
    assert db.reserver_sending_pa_nytt(con, "kurs:1", "a@x.no", "bekreftelse") is False


def test_allerede_sendt_kun_sant_for_status_sendt(con):
    db.reserver_sending(con, "kurs:1", "a@x.no", "bekreftelse")
    assert db.allerede_sendt(con, "kurs:1", "a@x.no", "bekreftelse") is False
    db.sett_sending_feilet(con, "kurs:1", "a@x.no", "bekreftelse")
    assert db.allerede_sendt(con, "kurs:1", "a@x.no", "bekreftelse") is False
    db.sett_sending_ukjent(con, "kurs:1", "a@x.no", "bekreftelse")
    assert db.allerede_sendt(con, "kurs:1", "a@x.no", "bekreftelse") is False
    db.sett_sendt(con, "kurs:1", "a@x.no", "bekreftelse")
    assert db.allerede_sendt(con, "kurs:1", "a@x.no", "bekreftelse") is True


def test_gamle_rader_uten_statuskolonne_tolkes_som_sendt(tmp_path):
    """Migrering: en rad skrevet av den gamle, ubetingede marker_sendt() skal fortsatt telle som sendt."""
    sti = tmp_path / "gammel.db"
    gammel = sqlite3.connect(sti)
    gammel.execute("CREATE TABLE admin_bruker (id INTEGER PRIMARY KEY)")
    gammel.execute("CREATE TABLE kurs (id INTEGER PRIMARY KEY)")
    gammel.execute("CREATE TABLE deltaker (id INTEGER PRIMARY KEY)")
    gammel.execute("CREATE TABLE paamelding (id INTEGER PRIMARY KEY, kurs_id INTEGER, deltaker_id INTEGER, "
                   "status TEXT, opprettet TEXT)")
    gammel.execute(
        "CREATE TABLE utsending_logg (nokkel TEXT, mottaker TEXT COLLATE NOCASE, type TEXT, "
        "sendt_ts TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE (nokkel, mottaker, type))")
    gammel.execute("INSERT INTO utsending_logg (nokkel, mottaker, type) VALUES ('kurs:1','a@x.no','bekreftelse')")
    gammel.commit()
    gammel.close()

    con2 = sqlite3.connect(sti)
    con2.row_factory = sqlite3.Row
    db._migrer(con2)
    con2.commit()
    assert db.allerede_sendt(con2, "kurs:1", "a@x.no", "bekreftelse") is True


# ==================== faktura_forsok: atomisk claim (db.py-primitiver) ====================

def test_reserver_faktura_vinner_kun_en_gang(con):
    kid = _kurs(con, date(2027, 3, 1), pris_nok=1000, fakturering="person")
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.commit()
    assert db.reserver_faktura(con, pid, None) is True
    assert db.reserver_faktura(con, pid, None) is False


def test_reserver_faktura_pa_nytt_krever_feilet(con):
    kid = _kurs(con, date(2027, 3, 1), pris_nok=1000, fakturering="person")
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.commit()
    db.reserver_faktura(con, pid, None)
    assert db.reserver_faktura_pa_nytt(con, pid, None) is False
    db.sett_faktura_forsok_feilet(con, pid, None, "test")
    assert db.reserver_faktura_pa_nytt(con, pid, None) is True


def test_faktura_forsok_ulike_kursdager_er_uavhengige(con):
    kid = _kurs(con, date(2027, 3, 1), dager=2, pris_nok=1000, fakturering="person", betaling="per_samling")
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.commit()
    dager = db.kursdager(con, kid)
    assert db.reserver_faktura(con, pid, dager[0]["id"]) is True
    assert db.reserver_faktura(con, pid, dager[1]["id"]) is True  # annen samling - egen noekkel
    assert db.reserver_faktura(con, pid, dager[0]["id"]) is False


# ==================== e-post via sveiper: klassifisering, sporing, ingen auto-retry av ukjent ====================

def test_epost_feil_gir_ukjent_status_og_loggfores(con, monkeypatch):
    kid = _kurs(con, date(2027, 3, 1), pris_nok=0)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.commit()

    monkeypatch.setattr(epost, "send", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("Graph nede")))
    sveiper.kjor(Kjoring(con, idag=date(2027, 3, 1)), pid)
    con.commit()

    rad = con.execute("SELECT status FROM utsending_logg WHERE mottaker='a@x.no'").fetchone()
    assert rad["status"] == "ukjent"
    assert con.execute("SELECT sveiper_kjort FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == 0
    assert len(_hendelser(con, "epost_ukjent")) == 1
    assert len(_hendelser(con, "sveip_feil")) == 1  # eksisterende, generelle loggen skal fortsatt fyres


def test_ukjent_epost_forsokes_ikke_automatisk_pa_nytt(con, monkeypatch):
    kid = _kurs(con, date(2027, 3, 1), pris_nok=0)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.commit()

    kall = {"antall": 0}
    def _feil(*a, **kw):
        kall["antall"] += 1
        raise RuntimeError("Graph nede")
    monkeypatch.setattr(epost, "send", _feil)

    sveiper.kjor(Kjoring(con, idag=date(2027, 3, 1)), pid)
    con.commit()
    sveiper.kjor(Kjoring(con, idag=date(2027, 3, 1)), pid)  # kjores igjen - skal IKKE prove paa nytt
    con.commit()
    assert kall["antall"] == 1


def test_feilet_epost_kan_hentes_inn_igjen_ved_retry_claim(con):
    """Simulerer en KJENT TRYGG feil direkte (taksonomien er ikke koblet inn enna, se modulens docstring)."""
    kid = _kurs(con, date(2027, 3, 1), pris_nok=0)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.commit()
    nokkel = f"kurs:{kid}"
    db.reserver_sending(con, nokkel, "a@x.no", "bekreftelse")
    db.sett_sending_feilet(con, nokkel, "a@x.no", "bekreftelse")
    con.commit()

    sveiper.kjor(Kjoring(con, idag=date(2027, 3, 1)), pid)  # normal (vellykket) sending i denne testen
    con.commit()
    assert db.allerede_sendt(con, nokkel, "a@x.no", "bekreftelse") is True
    assert con.execute("SELECT sveiper_kjort FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == 1


# ==================== faktura via sveiper: klassifisering, sporing, ingen auto-retry ====================

def test_visma_feil_gir_ukjent_status_og_loggfores_ingen_faktura(con, monkeypatch):
    kid = _kurs(con, date(2027, 3, 1), pris_nok=1000, fakturering="person", betaling="samlet")
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.commit()

    monkeypatch.setattr(visma, "fakturer", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("Visma nede")))
    sveiper.kjor(Kjoring(con, idag=date(2027, 3, 1)), pid)
    con.commit()

    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 0
    rad = db.faktura_forsok_rad(con, pid, None)
    assert rad["status"] == "ukjent"
    assert con.execute("SELECT sveiper_kjort FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == 0
    assert len(_hendelser(con, "faktura_ukjent")) == 1


def test_ukjent_faktura_forsokes_ikke_automatisk_pa_nytt(con, monkeypatch):
    kid = _kurs(con, date(2027, 3, 1), pris_nok=1000, fakturering="person", betaling="samlet")
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.commit()

    kall = {"antall": 0}
    def _feil(*a, **kw):
        kall["antall"] += 1
        raise RuntimeError("Visma nede")
    monkeypatch.setattr(visma, "fakturer", _feil)

    sveiper.kjor(Kjoring(con, idag=date(2027, 3, 1)), pid)
    con.commit()
    sveiper.kjor(Kjoring(con, idag=date(2027, 3, 1)), pid)
    con.commit()
    assert kall["antall"] == 1
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 0


def test_feilet_faktura_kan_hentes_inn_igjen_ved_retry_claim(con):
    kid = _kurs(con, date(2027, 3, 1), pris_nok=1000, fakturering="person", betaling="samlet")
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.commit()
    db.reserver_faktura(con, pid, None)
    db.sett_faktura_forsok_feilet(con, pid, None, "test")
    con.commit()

    sveiper.kjor(Kjoring(con, idag=date(2027, 3, 1)), pid)  # normal (vellykket) fakturering i denne testen
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 1
    assert db.faktura_forsok_rad(con, pid, None) is None  # rydda etter suksess
    assert con.execute("SELECT sveiper_kjort FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == 1


def test_fersk_reservert_faktura_blokkerer_uten_a_logge_gammel(con, monkeypatch):
    kid = _kurs(con, date(2027, 3, 1), pris_nok=1000, fakturering="person", betaling="samlet")
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.commit()
    db.reserver_faktura(con, pid, None)  # fersk - "en annen prosess jobber trolig med den akkurat naa"
    con.commit()

    monkeypatch.setattr(visma, "fakturer", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("skal ikke kalles")))
    sveiper.kjor(Kjoring(con, idag=date(2027, 3, 1)), pid)
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 0
    assert len(_hendelser(con, "faktura_reservert_gammel")) == 0


def test_gammel_reservert_faktura_blokkerer_og_loggfores(con, monkeypatch):
    kid = _kurs(con, date(2027, 3, 1), pris_nok=1000, fakturering="person", betaling="samlet")
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.commit()
    db.reserver_faktura(con, pid, None)
    gammel_tid = (date.today() - timedelta(days=1)).isoformat() + "T00:00:00"
    con.execute("UPDATE faktura_forsok SET opprettet=? WHERE paamelding_id=?", (gammel_tid, pid))
    con.commit()

    monkeypatch.setattr(visma, "fakturer", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("skal ikke kalles")))
    sveiper.kjor(Kjoring(con, idag=date(2027, 3, 1)), pid)
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 0
    assert len(_hendelser(con, "faktura_reservert_gammel")) == 1


# ==================== sveiper_kjort: kun naar ALT obligatorisk er definitivt ferdig ====================

def test_sveiper_kjort_ikke_1_naar_faktura_ukjent_men_epost_ok(con, monkeypatch):
    kid = _kurs(con, date(2027, 3, 1), pris_nok=1000, fakturering="person", betaling="samlet")
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.commit()
    monkeypatch.setattr(visma, "fakturer", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("Visma nede")))
    sveiper.kjor(Kjoring(con, idag=date(2027, 3, 1)), pid)
    con.commit()
    assert db.allerede_sendt(con, f"kurs:{kid}", "a@x.no", "bekreftelse") is True  # e-posten GIKK ut
    assert con.execute("SELECT sveiper_kjort FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == 0


def test_sveiper_kjort_1_per_samling_naar_kun_forfalt_samling_er_ferdig(con):
    """Fremtidige (ikke-forfalte) delfakturaer skal IKKE kreves ferdige - forfalte_delfakturaer() henter dem senere."""
    kid = _kurs(con, date(2027, 6, 1), dager=2, pris_nok=2000, fakturering="person", betaling="per_samling",
               faktura_dager_for=14)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.commit()
    # kun forste samling (30 dager senere enn kursstart+0) er forfalt naar idag=kursstart
    sveiper.kjor(Kjoring(con, idag=date(2027, 6, 1)), pid)
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 1
    assert con.execute("SELECT sveiper_kjort FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == 1


# ==================== visning: en reservert/feilet/ukjent rad vises aldri som sendt ====================

def test_kommunikasjonsvisning_viser_aldri_uavklart_rad(con, monkeypatch):
    from kurs.web import app as webapp
    kid = _kurs(con, date(2027, 3, 1), pris_nok=0)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.commit()
    monkeypatch.setattr(epost, "send", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("Graph nede")))
    sveiper.kjor(Kjoring(con, idag=date(2027, 3, 1)), pid)
    con.commit()

    klient = webapp.app.test_client()
    klient.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    tekst = klient.get(f"/admin/kurs/{kid}/deltaker/{pid}/kommunikasjon").get_data(as_text=True)
    assert "Ingen e-post sendt ennå." in tekst  # den ukjente raden vises IKKE som om den var sendt


# Forsidens teller er fra rettelsesrunden en LIVE telling av uavklart tilstand (ikke kumulativ hendelsestelling) -
# se tests/test_trinn25_rettelser.py (test_teller_*).


# ==================== tørrkjøring: fortsatt ingen databaseendringer ====================

def test_tor_reserverer_men_ruller_alt_tilbake(con):
    kid = _kurs(con, date(2027, 3, 1), pris_nok=1000, fakturering="person", betaling="samlet")
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.commit()
    from kurs import daglig
    daglig.kjor(Kjoring(con, idag=date(2027, 3, 1), tor=True))
    assert con.execute("SELECT COUNT(*) FROM utsending_logg").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM faktura_forsok").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 0
