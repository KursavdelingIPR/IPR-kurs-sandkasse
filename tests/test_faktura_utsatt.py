"""Faktura tidligst seks kalendermaaneder foer foerste kursdag.

Steg 1: kalenderaritmetikken (ren funksjon, ingen DB).
Steg 2: KUN datamodell + migrering (faktura_onskes_na, faktura_tidligst_dato) - ren lagringskapasitet, ingen
        kobling til fakturabeslutningen. Fakturaflyten er i senere sjekkpunkter.
"""
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from kurs import config, db, sveiper
from kurs.kjoring import Kjoring
from kurs.sveiper import seks_maaneder_for


@pytest.mark.parametrize("kursdag,forventet", [
    (date(2027, 9, 20), date(2027, 3, 20)),    # vanlig tilfelle
    (date(2027, 8, 31), date(2027, 2, 28)),    # 31. finnes ikke i februar (ikke skaaar)
    (date(2028, 8, 31), date(2028, 2, 29)),    # skuddaar: siste dag i februar er 29.
    (date(2028, 2, 29), date(2027, 8, 29)),    # 29.02 -> august har 29.
    (date(2027, 10, 31), date(2027, 4, 30)),   # april har 30 dager
    (date(2027, 3, 31), date(2026, 9, 30)),    # september har 30 dager, og bakover over aarsskiftet
    (date(2027, 1, 31), date(2026, 7, 31)),    # aarsskifte, juli har 31
    (date(2027, 6, 30), date(2026, 12, 30)),   # desember
    (date(2027, 7, 15), date(2027, 1, 15)),    # januar
    (date(2027, 12, 31), date(2027, 6, 30)),   # juni har 30 dager
    (date(2027, 1, 1), date(2026, 7, 1)),
])
def test_seks_maaneder_for_gir_riktig_kalenderdato(kursdag, forventet):
    assert seks_maaneder_for(kursdag) == forventet


def test_det_er_kalendermaaneder_og_ikke_180_dager():
    kursdag = date(2027, 9, 20)
    assert seks_maaneder_for(kursdag) == date(2027, 3, 20)
    assert seks_maaneder_for(kursdag) != kursdag - timedelta(days=180)      # 180 dager tilbake ville gitt 24.03.2027
    assert kursdag - timedelta(days=180) == date(2027, 3, 24)
    # ende-av-maaned: heller ikke her tilsvarer resultatet 180 dager tilbake
    assert seks_maaneder_for(date(2027, 8, 31)) == date(2027, 2, 28)
    assert seks_maaneder_for(date(2027, 8, 31)) != date(2027, 8, 31) - timedelta(days=180)


def test_seks_maaneder_er_alltid_mellom_181_og_184_dager():
    """Aldri 180: derfor er faktura_dager_for <= 180 (per_samling) alltid innenfor seksmaanedersregelen."""
    for n in range(4 * 366):
        kursdag = date(2026, 1, 1) + timedelta(days=n)
        antall = (kursdag - seks_maaneder_for(kursdag)).days
        assert 181 <= antall <= 184, (kursdag, antall)


def test_resultatet_ligger_i_maaneden_seks_maaneder_tilbake_og_aldri_etter_kursdagen():
    for n in range(4 * 366):
        kursdag = date(2026, 1, 1) + timedelta(days=n)
        r = seks_maaneder_for(kursdag)
        assert (kursdag.year * 12 + kursdag.month) - (r.year * 12 + r.month) == 6, (kursdag, r)
        assert r.day == kursdag.day or r.day < kursdag.day               # bare klipping nedover, aldri oppover
        assert r < kursdag


def test_funksjonen_er_ren_og_deterministisk():
    """Tar og returnerer date, uten dagens dato eller database: samme svar hver gang, og input endres ikke."""
    kursdag = date(2027, 9, 20)
    r1, r2 = seks_maaneder_for(kursdag), seks_maaneder_for(kursdag)
    assert r1 == r2 == date(2027, 3, 20) and type(r1) is date
    assert kursdag == date(2027, 9, 20)


# ======================= STEG 2: datamodell + migrering (ren lagringskapasitet) =======================

@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    return c


def _kurs(con, kode="K1", start=date(2027, 6, 1), **kw):
    kid = db.opprett_kurs(con, kode=kode, navn="Kurs " + kode, datoer=[start.isoformat()],
                          sharepoint_mappe="Kurs/" + kode, **{"pris_nok": 0, **kw})
    con.commit()
    return kid


def _fakturafelt(con, pid):
    r = con.execute("SELECT faktura_onskes_na, faktura_tidligst_dato FROM paamelding WHERE id=?", (pid,)).fetchone()
    return r["faktura_onskes_na"], r["faktura_tidligst_dato"]


def _kolonner(con):
    return {r["name"]: r for r in con.execute("PRAGMA table_info(paamelding)")}


def _legacy_db(sti: Path):
    """En gammel database UTEN de nye kolonnene, med en eksisterende paameldingsrad."""
    gammel = sqlite3.connect(sti)
    gammel.execute("CREATE TABLE admin_bruker (id INTEGER PRIMARY KEY)")
    gammel.execute("CREATE TABLE kurs (id INTEGER PRIMARY KEY)")
    gammel.execute("CREATE TABLE deltaker (id INTEGER PRIMARY KEY)")
    gammel.execute("CREATE TABLE paamelding (id INTEGER PRIMARY KEY, kurs_id INTEGER, deltaker_id INTEGER, "
                   "status TEXT, opprettet TEXT)")
    gammel.execute("INSERT INTO paamelding (id, kurs_id, deltaker_id, status, opprettet) "
                   "VALUES (7, 3, 5, 'bekreftet', '2020-01-01T00:00:00')")
    gammel.commit()
    gammel.close()


# ---- A. ny database ----

def test_ny_database_har_begge_kolonnene_med_riktige_standardverdier(con):
    kol = _kolonner(con)
    assert "faktura_onskes_na" in kol and "faktura_tidligst_dato" in kol
    assert kol["faktura_onskes_na"]["notnull"] == 1 and kol["faktura_onskes_na"]["dflt_value"] == "0"
    assert kol["faktura_tidligst_dato"]["notnull"] == 0 and kol["faktura_tidligst_dato"]["dflt_value"] is None
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.commit()
    assert _fakturafelt(con, pid) == (0, None)


# ---- B. legacy-database ----

def test_legacy_database_faar_kolonnene_uten_datatap(tmp_path):
    sti = tmp_path / "gammel.db"
    _legacy_db(sti)
    con = sqlite3.connect(sti)
    con.row_factory = sqlite3.Row
    assert "faktura_onskes_na" not in _kolonner(con)                       # forutsetning: gammel skjema
    db._migrer(con)
    con.commit()
    kol = _kolonner(con)
    assert "faktura_onskes_na" in kol and "faktura_tidligst_dato" in kol
    rad = con.execute("SELECT * FROM paamelding WHERE id=7").fetchone()
    assert (rad["faktura_onskes_na"], rad["faktura_tidligst_dato"]) == (0, None)
    assert (rad["kurs_id"], rad["deltaker_id"], rad["status"], rad["opprettet"]) == (3, 5, "bekreftet", "2020-01-01T00:00:00")
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 1


# ---- C. idempotent: ingen feil, ingen datatap, lagrede verdier overskrives ALDRI ----

def test_migrering_kjort_paa_nytt_overskriver_ikke_lagrede_verdier(tmp_path):
    sti = tmp_path / "gammel.db"
    _legacy_db(sti)
    con = sqlite3.connect(sti)
    con.row_factory = sqlite3.Row
    db._migrer(con)
    con.commit()
    antall_kolonner = len(_kolonner(con))
    con.execute("UPDATE paamelding SET faktura_onskes_na=1, faktura_tidligst_dato='2027-03-20' WHERE id=7")
    con.commit()
    for _ in range(3):
        db._migrer(con)                                                    # ingen feil ved gjentatt kjoring
        con.commit()
    assert len(_kolonner(con)) == antall_kolonner                          # ingen kolonner lagt til flere ganger
    assert _fakturafelt(con, 7) == (1, "2027-03-20")                       # verdiene er IKKE nullstilt av _migrer
    rad = con.execute("SELECT kurs_id, deltaker_id, status FROM paamelding WHERE id=7").fetchone()
    assert tuple(rad) == (3, 5, "bekreftet")


def test_db_init_paa_eksisterende_database_beholder_lagrede_fakturafelt(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.execute("UPDATE paamelding SET faktura_onskes_na=1, faktura_tidligst_dato='2027-03-20' WHERE id=?", (pid,))
    con.commit()
    db.init(con)                                                           # samme som en ny oppstart av appen
    db.init(con)
    assert _fakturafelt(con, pid) == (1, "2027-03-20")


# ---- reaktivering nullstiller gamle fakturabeslutninger ----

def test_reaktivering_nullstiller_gammel_fakturabeslutning(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.execute("UPDATE paamelding SET faktura_onskes_na=1, faktura_tidligst_dato='2027-03-20' WHERE id=?", (pid,))
    con.commit()
    db.meld_av(con, pid)
    con.commit()
    assert _fakturafelt(con, pid) == (1, "2027-03-20")                     # avmelding alene roerer dem ikke
    pid2, status = db.meld_paa(con, kid, epost="a@x.no", navn="A")        # reaktivering via vanlig flyt
    con.commit()
    assert pid2 == pid and status == "bekreftet"
    assert _fakturafelt(con, pid) == (0, None)


# ---- eksplisitt valg kan lagres (seam), ogsaa paa venteliste ----

def test_eksplisitt_faktura_onskes_na_lagres(con):
    kid = _kurs(con)
    pid, status = db.meld_paa(con, kid, epost="a@x.no", navn="A", paamelding={"faktura_onskes_na": 1})
    con.commit()
    assert status == "bekreftet" and _fakturafelt(con, pid) == (1, None)


def test_eksplisitt_valg_bevares_paa_venteliste(con):
    kid = _kurs(con, kapasitet=1)
    db.meld_paa(con, kid, epost="a@x.no", navn="A")
    pid, status = db.meld_paa(con, kid, epost="b@x.no", navn="B", paamelding={"faktura_onskes_na": 1})
    con.commit()
    assert status == "venteliste" and _fakturafelt(con, pid) == (1, None)
    opp = db.endre_kapasitet(con, kid, 2)                                  # senere opprykk: valget ligger fortsatt der
    con.commit()
    assert opp == [pid] and _fakturafelt(con, pid) == (1, None)


def test_eksplisitt_valg_kan_ogsaa_settes_ved_reaktivering(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    db.meld_av(con, pid)
    con.commit()
    pid2, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A", paamelding={"faktura_onskes_na": 1})
    con.commit()
    assert pid2 == pid and _fakturafelt(con, pid) == (1, None)


# ---- INGEN fakturaatferd ennaa ----

@pytest.mark.parametrize("onskes_na", [0, 1])
def test_samlet_faktura_opprettes_fortsatt_umiddelbart_selv_langt_frem_i_tid(con, onskes_na):
    """Kurset starter 18 maaneder frem: uten steg 3 skal INGENTING holdes tilbake, og valget paavirker ikke noe."""
    kid = _kurs(con, start=date(2028, 9, 20), pris_nok=1000, fakturering="person")
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A", paamelding={"faktura_onskes_na": onskes_na},
                         idag=date(2027, 3, 1))
    con.commit()
    sveiper.kjor(Kjoring(con, idag=date(2027, 3, 1)), pid)
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", (pid,)).fetchone()[0] == 1
    assert con.execute("SELECT sveiper_kjort FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == 1
    assert _fakturafelt(con, pid) == (onskes_na, None)                     # faktura_tidligst_dato settes ikke av noe


def test_ingen_produksjonskode_bruker_de_nye_feltene_eller_seksmaanedersregelen_ennaa():
    """Steg 2-vakt: feltene er ren lagringskapasitet. OPPDATERES/FJERNES naar steg 3 (fakturabeslutningen) kobles inn."""
    rot = Path(__file__).resolve().parent.parent / "kurs"
    kilde = {f: (rot / f).read_text(encoding="utf-8") for f in
             ("sveiper.py", "daglig.py", "behandling.py", "kjoring.py", "web/app.py", "maltekster.py")}
    for fil, tekst in kilde.items():
        assert "faktura_tidligst_dato" not in tekst, fil
        assert "faktura_onskes_na" not in tekst, fil
    assert kilde["sveiper.py"].count("seks_maaneder_for") == 1              # kun selve definisjonen
    for fil in ("daglig.py", "behandling.py", "kjoring.py", "web/app.py", "maltekster.py"):
        assert "seks_maaneder_for" not in kilde[fil], fil
