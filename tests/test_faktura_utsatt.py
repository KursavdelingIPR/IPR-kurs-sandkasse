"""Faktura tidligst seks kalendermaaneder foer foerste kursdag.

Kalenderaritmetikk (seks_maaneder_for), datamodell + migrering (faktura_onskes_na, faktura_tidligst_dato), den rene
fakturaplanen (faktura_plan), og den operative flyten: hold, daglig og maalrettet utloesning (utsatte_fakturaer),
oekonomilaas, oppdatering av hold ved kursdatoendring og statusoverganger.
"""
import ast
import dataclasses
import inspect
import json
import re
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


# ======================= datamodell + migrering =======================

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


def _kall_i_kodebasen(navn: str) -> list:
    """Alle (fil, omsluttende funksjon) der `navn` KALLES i produksjonskoden (kurs/**/*.py) - AST, ikke tekstsok."""
    rot = Path(__file__).resolve().parent.parent / "kurs"
    funnet = []
    for fil in sorted(rot.rglob("*.py")):
        tre = ast.parse(fil.read_text(encoding="utf-8"))
        for fn in (n for n in ast.walk(tre) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            for k in (n for n in ast.walk(fn) if isinstance(n, ast.Call)):
                f = k.func
                if (isinstance(f, ast.Name) and f.id == navn) or (isinstance(f, ast.Attribute) and f.attr == navn):
                    funnet.append((fil.relative_to(rot).as_posix(), fn.name))
    return funnet


# ======================= ren fakturaplan (faktura_plan) =======================

def _plan(**over):
    """Standard: samlet, 1000 kr, fakturering=person, bekreftet, kursstart 20.09.2027, idag 19.03.2027."""
    arg = dict(fakturering="person", pris_nok=1000, status="bekreftet", betaling="samlet", faktura_onskes_na=0,
               kursdager=["2027-09-20"], idag=date(2027, 3, 19))
    arg.update(over)
    return sveiper.faktura_plan(**arg)


def test_plan_1_samlet_mer_enn_seks_maaneder_frem_er_utsatt_med_tidligst_dato():
    assert _plan(idag=date(2027, 3, 19)) == sveiper.FakturaPlan(sveiper.PLAN_UTSATT, date(2027, 3, 20))


def test_plan_2_paa_grensedatoen_er_na():
    assert _plan(idag=date(2027, 3, 20)) == sveiper.FakturaPlan(sveiper.PLAN_NA)      # noyaktig seks kalendermaaneder foer


def test_plan_3_etter_grensedatoen_er_na():
    assert _plan(idag=date(2027, 3, 21)).modus == sveiper.PLAN_NA
    assert _plan(idag=date(2027, 9, 20)).modus == sveiper.PLAN_NA                    # paa kursdagen
    assert _plan(idag=date(2027, 10, 1)).modus == sveiper.PLAN_NA                    # etter kursstart


def test_plan_4_faktura_onskes_na_gir_na_selv_langt_frem_i_tid():
    p = _plan(idag=date(2027, 1, 1), faktura_onskes_na=1)
    assert p == sveiper.FakturaPlan(sveiper.PLAN_NA) and p.tidligst_dato is None
    assert _plan(idag=date(2027, 1, 1), faktura_onskes_na=0).modus == sveiper.PLAN_UTSATT   # kontroll: uten ønsket ville den ventet


@pytest.mark.parametrize("kursdager", [[], None, ()])
def test_plan_5_samlet_uten_kursdager_er_mangler_kursdag(kursdager):
    assert _plan(kursdager=kursdager, faktura_onskes_na=0) == sveiper.FakturaPlan(sveiper.PLAN_MANGLER_KURSDAG)


@pytest.mark.parametrize("kursdager", [[], None, ()])
def test_plan_6_faktura_onskes_na_omgaar_ikke_manglende_kursdag(kursdager):
    p = _plan(kursdager=kursdager, faktura_onskes_na=1)
    assert p.modus == sveiper.PLAN_MANGLER_KURSDAG and p.modus != sveiper.PLAN_NA


def test_plan_7_per_samling_18_maaneder_frem_er_per_samling():
    p = _plan(betaling="per_samling", kursdager=["2028-09-20"], idag=date(2027, 3, 1))
    assert p == sveiper.FakturaPlan(sveiper.PLAN_PER_SAMLING)                        # seksmaanedersregelen gjelder ikke


def test_plan_8_faktura_onskes_na_endrer_ikke_per_samling():
    assert _plan(betaling="per_samling", faktura_onskes_na=1, kursdager=["2028-09-20"]).modus == sveiper.PLAN_PER_SAMLING
    assert _plan(betaling="per_samling", faktura_onskes_na=0, kursdager=["2028-09-20"]).modus == sveiper.PLAN_PER_SAMLING


def test_plan_per_samling_paavirkes_ikke_av_manglende_kursdager():
    """per_samling har egen forfallslogikk (uendret, ogsaa dens eksisterende feil ved null kursdager) - planen sier bare per_samling."""
    assert _plan(betaling="per_samling", kursdager=[]).modus == sveiper.PLAN_PER_SAMLING


def test_plan_9_gratis_kurs_er_ingen():
    assert _plan(pris_nok=0) == sveiper.FakturaPlan(sveiper.PLAN_INGEN)
    assert _plan(pris_nok=0, faktura_onskes_na=1, kursdager=[]).modus == sveiper.PLAN_INGEN    # ingen relevant faktura -> foerst


def test_plan_10_fakturering_organisasjon_er_ingen():
    assert _plan(fakturering="organisasjon").modus == sveiper.PLAN_INGEN
    assert _plan(fakturering="organisasjon", faktura_onskes_na=1, betaling="per_samling").modus == sveiper.PLAN_INGEN


def test_plan_11_fakturering_ingen_er_ingen():
    assert _plan(fakturering="ingen").modus == sveiper.PLAN_INGEN


def test_plan_12_venteliste_er_ingen():
    assert _plan(status="venteliste").modus == sveiper.PLAN_INGEN
    assert _plan(status="venteliste", faktura_onskes_na=1).modus == sveiper.PLAN_INGEN


def test_plan_13_avmeldt_er_ingen():
    assert _plan(status="avmeldt").modus == sveiper.PLAN_INGEN


def test_plan_14_hvem_som_betaler_paavirker_ikke_planen():
    """Arbeidsgiver betaler + fakturering=person = samme automatiske faktura som vanlig: planen kjenner ikke betaler."""
    assert "betaler" not in inspect.signature(sveiper.faktura_plan).parameters
    for kw in ({}, {"idag": date(2027, 3, 20)}, {"faktura_onskes_na": 1}):
        assert _plan(**kw) == _plan(**kw)                                            # deterministisk, ingen betaler-input
    assert _plan().modus == sveiper.PLAN_UTSATT and _plan(idag=date(2027, 3, 20)).modus == sveiper.PLAN_NA


def test_plan_15_maanedsslutt_31_august_gir_28_februar():
    kd = ["2027-08-31"]
    assert _plan(kursdager=kd, idag=date(2027, 2, 27)) == sveiper.FakturaPlan(sveiper.PLAN_UTSATT, date(2027, 2, 28))
    assert _plan(kursdager=kd, idag=date(2027, 2, 28)) == sveiper.FakturaPlan(sveiper.PLAN_NA)
    kd_skudd = ["2028-08-31"]
    assert _plan(kursdager=kd_skudd, idag=date(2028, 2, 28)) == sveiper.FakturaPlan(sveiper.PLAN_UTSATT, date(2028, 2, 29))
    assert _plan(kursdager=kd_skudd, idag=date(2028, 2, 29)).modus == sveiper.PLAN_NA


def test_plan_bruker_foerste_kursdag_ikke_siste_og_tar_date_eller_iso_tekst():
    flere = ["2027-09-20", "2027-10-20", "2027-11-20"]
    assert _plan(kursdager=flere).tidligst_dato == date(2027, 3, 20)                 # foerste kursdag
    assert _plan(kursdager=[date(2027, 9, 20), date(2027, 10, 20)]).tidligst_dato == date(2027, 3, 20)
    assert _plan(kursdager=list(reversed(flere))).tidligst_dato == date(2027, 3, 20)  # ogsaa hvis rekkefolgen skulle vaere feil


def test_plan_det_lagrede_tidligst_dato_feltet_er_ikke_input():
    parametre = set(inspect.signature(sveiper.faktura_plan).parameters)
    assert "faktura_tidligst_dato" not in parametre and "tidligst_dato" not in parametre
    assert parametre == {"fakturering", "pris_nok", "status", "betaling", "faktura_onskes_na", "kursdager", "idag"}


def test_plan_er_ren_deterministisk_og_endrer_ikke_input():
    kursdager = ["2027-09-20", "2027-10-20"]
    kopi = list(kursdager)
    r1, r2 = _plan(kursdager=kursdager), _plan(kursdager=kursdager)
    assert r1 == r2 and kursdager == kopi
    with pytest.raises(dataclasses.FrozenInstanceError):
        r1.modus = "na"                                                              # uforanderlig
    assert [f.name for f in dataclasses.fields(sveiper.FakturaPlan)] == ["modus", "tidligst_dato"]   # ingen persondata


def test_plan_bruker_ikke_db_visma_epost_eller_dagens_dato():
    """Strukturtest (AST): funksjonen kaller bare et lite, kjent sett funksjoner og refererer aldri til DB/Visma/e-post/klokke."""
    kilde = (Path(__file__).resolve().parent.parent / "kurs" / "sveiper.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.parse(kilde).body if isinstance(n, ast.FunctionDef) and n.name == "faktura_plan")
    kall = set()
    for k in (n for n in ast.walk(fn) if isinstance(n, ast.Call)):
        kall.add(k.func.id if isinstance(k.func, ast.Name) else k.func.attr)
    assert kall <= {"skal_faktureres", "FakturaPlan", "isinstance", "fromisoformat", "min", "seks_maaneder_for"}, kall
    navn = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    for forbudt in ("db", "visma", "epost", "Kjoring", "k", "con", "today", "now", "datetime", "time", "logg",
                    "commit", "execute", "send", "fakturer", "_opprett"):
        assert forbudt not in navn, forbudt
    assert not [n for n in ast.walk(fn) if isinstance(n, (ast.Global, ast.Nonlocal))]


def test_faktura_plan_er_koblet_inn_kun_via_lag_plan_og_ingen_andre_moduler_bruker_regelen():
    """Vakt: planen kalles kun fra _lag_plan, regelen kun fra planen, og de nye feltene ligger ikke i UI/e-post/daglig."""
    assert _kall_i_kodebasen("faktura_plan") == [("sveiper.py", "_lag_plan")]
    assert sorted(_kall_i_kodebasen("seks_maaneder_for")) == [("sveiper.py", "faktura_plan"),
                                                             ("sveiper.py", "oppdater_faktura_hold_etter_kursdagendring")]
    rot = Path(__file__).resolve().parent.parent / "kurs"
    for fil in ("daglig.py", "behandling.py", "kjoring.py", "web/app.py", "maltekster.py"):
        tekst = (rot / fil).read_text(encoding="utf-8")
        for navn in ("faktura_tidligst_dato", "faktura_onskes_na", "faktura_plan", "seks_maaneder_for"):
            assert navn not in tekst, (fil, navn)
    sveiper_tekst = (rot / "sveiper.py").read_text(encoding="utf-8")
    tre = ast.parse(sveiper_tekst)
    plan_fn = next(n for n in tre.body if isinstance(n, ast.FunctionDef) and n.name == "faktura_plan")
    docstreng = plan_fn.body[0].value
    bruk = ([n.id for n in ast.walk(plan_fn) if isinstance(n, ast.Name)] + [n.attr for n in ast.walk(plan_fn) if isinstance(n, ast.Attribute)]
            + [n.value for n in ast.walk(plan_fn) if isinstance(n, ast.Constant) and isinstance(n.value, str) and n is not docstreng])
    assert not [b for b in bruk if "faktura_tidligst_dato" in b]       # den rene planen bruker aldri den lagrede datoen som input


# ======================= operativ flyt (hold, utloesning, oekonomilaas) =======================

from kurs import daglig  # noqa: E402
from kurs.integrasjoner import epost, visma  # noqa: E402

FORSTE = date(2027, 9, 20)        # kursstart; seks kalendermaaneder foer = 2027-03-20
GRENSE = date(2027, 3, 20)


@pytest.fixture
def ute(monkeypatch):
    """Fanger utgaaende e-post og Visma-kall (Visma kjoerer ellers sin vanlige demo-gren)."""
    kall = {"epost": [], "visma": []}
    monkeypatch.setattr(epost, "send", lambda til, emne, html, *a, **kw: kall["epost"].append((til, emne)))
    ekte = visma.fakturer

    def visma_fakturer(g):
        kall["visma"].append(g.belop_nok)
        return ekte(g)
    monkeypatch.setattr(visma, "fakturer", visma_fakturer)
    return kall


def _betalt(con, kode="B1", start=FORSTE, ant_dager=1, **kw):
    datoer = [(start + timedelta(days=30 * n)).isoformat() for n in range(ant_dager)]
    kid = db.opprett_kurs(con, kode=kode, navn="Kurs " + kode, datoer=datoer, sharepoint_mappe="Kurs/" + kode,
                          **{"pris_nok": 1000, "fakturering": "person", **kw})
    con.commit()
    return kid


def _paamelding(con, kid, epost_="a@x.no", **pm):
    pid, _ = db.meld_paa(con, kid, epost=epost_, navn="Navn " + epost_.split("@")[0], paamelding=pm or None)
    con.commit()
    return pid


def _kjor(con, idag, pid=None):
    sveiper.kjor(Kjoring(con, idag=idag), pid)
    con.commit()


def _tilstand(con, pid):
    r = con.execute("SELECT sveiper_kjort, faktura_tidligst_dato, faktura_onskes_na FROM paamelding WHERE id=?", (pid,)).fetchone()
    return tuple(r)


def _antall(con, tabell, pid=None):
    sql = f"SELECT COUNT(*) FROM {tabell}" + (" WHERE paamelding_id=?" if pid else "")
    return con.execute(sql, (pid,) if pid else ()).fetchone()[0]


def _hold(con, ute, idag=date(2027, 3, 19), **kw):
    """En holdt samlet faktura: bekreftet, behandlet foer grensen. Returnerer (kurs_id, paamelding_id)."""
    kid = _betalt(con, **kw)
    pid = _paamelding(con, kid)
    _kjor(con, idag, pid)
    assert _tilstand(con, pid) == (1, "2027-03-20", 0)
    return kid, pid


# ---- A. hold ----

def test_a_hold_bekreftelse_ferdig_ingen_faktura_dato_lagret_sveiper_kjort_1(con, ute):
    kid = _betalt(con)
    pid = _paamelding(con, kid)
    _kjor(con, date(2027, 3, 19), pid)
    assert [t for t, _ in ute["epost"]] == ["a@x.no"] and ute["epost"][0][1].startswith("Bekreftelse")   # bekreftelsen er sendt
    assert ute["visma"] == []                                                                            # ingen Visma
    assert _antall(con, "faktura") == 0 and _antall(con, "faktura_forsok") == 0
    assert _tilstand(con, pid) == (1, "2027-03-20", 0)
    assert db.allerede_sendt(con, f"kurs:{kid}", "a@x.no", "bekreftelse")


def test_a2_hold_dato_og_sveiper_kjort_lagres_i_samme_transaksjon(con, ute):
    kid = _betalt(con)
    pid = _paamelding(con, kid)
    p = con.execute(sveiper.SQL_DELTAKER + " WHERE p.id=?", (pid,)).fetchone()
    sveiper._en(Kjoring(con, idag=date(2027, 3, 19)), p)
    fersk = db.koble(config.DB_STI)
    assert tuple(fersk.execute("SELECT sveiper_kjort, faktura_tidligst_dato FROM paamelding WHERE id=?", (pid,)).fetchone()) == (0, None)
    con.rollback()                                                      # begge tapes sammen ...
    assert _tilstand(con, pid)[:2] == (0, None)
    sveiper._en(Kjoring(con, idag=date(2027, 3, 19)), p)
    con.commit()                                                        # ... og begge lagres sammen
    assert tuple(fersk.execute("SELECT sveiper_kjort, faktura_tidligst_dato FROM paamelding WHERE id=?", (pid,)).fetchone()) == (1, "2027-03-20")
    fersk.close()


def test_a3_epostfeil_hindrer_at_hold_og_sveiper_kjort_lagres(con, monkeypatch):
    monkeypatch.setattr(epost, "send", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Graph nede")))
    kid = _betalt(con)
    pid = _paamelding(con, kid)
    _kjor(con, date(2027, 3, 19), pid)
    assert _tilstand(con, pid)[:2] == (0, None) and _antall(con, "faktura") == 0     # ingen ferdig fakturaplan uten ferdig bekreftelse


# ---- B. grensedato, C. faktura naa ----

def test_b_paa_grensedatoen_opprettes_faktura_umiddelbart(con, ute):
    kid = _betalt(con)
    pid = _paamelding(con, kid)
    _kjor(con, GRENSE, pid)
    assert ute["visma"] == [1000] and _antall(con, "faktura", pid) == 1
    assert _tilstand(con, pid) == (1, None, 0)


def test_c_faktura_naa_gir_faktura_umiddelbart_selv_langt_frem(con, ute):
    kid = _betalt(con)
    pid = _paamelding(con, kid, faktura_onskes_na=1)
    _kjor(con, date(2027, 1, 1), pid)
    assert ute["visma"] == [1000] and _antall(con, "faktura", pid) == 1
    assert _tilstand(con, pid) == (1, None, 1)


# ---- D. mangler kursdag ----

def test_d_mangler_kursdag_er_konfigurasjonsfeil_uten_faktura_og_uten_ferdigmerking(con, ute):
    kid = db.opprett_kurs(con, kode="U1", navn="Uten dager", datoer=[], sharepoint_mappe="Kurs/U1", pris_nok=1000, fakturering="person")
    con.commit()
    for onskes in (0, 1):
        pid = _paamelding(con, kid, f"u{onskes}@x.no", faktura_onskes_na=onskes)
        _kjor(con, date(2027, 1, 1), pid)
        assert db.allerede_sendt(con, f"kurs:{kid}", f"u{onskes}@x.no", "bekreftelse")     # bekreftelsen kan vaere ferdig
        assert ute["visma"] == [] and _antall(con, "faktura", pid) == 0 and _antall(con, "faktura_forsok", pid) == 0
        assert _tilstand(con, pid) == (0, None, onskes)                                       # IKKE ferdig, ingen dato, ogsaa med "naa"
    hendelser = [r["detaljer"] for r in con.execute("SELECT detaljer FROM hendelse WHERE handling='faktura_feil'")]
    assert len(hendelser) == 2
    for h in hendelser:
        d = json.loads(h)
        assert set(d) == {"paamelding_id", "kurs_id", "arsak"} and d["arsak"] == "mangler_kursdag" and d["kurs_id"] == kid
        assert "@" not in h and "Navn" not in h                                               # ingen PII


def test_d2_mangler_kursdag_paavirker_ikke_andre_paameldinger(con, ute):
    u = db.opprett_kurs(con, kode="U1", navn="Uten dager", datoer=[], sharepoint_mappe="Kurs/U1", pris_nok=1000, fakturering="person")
    ok = _betalt(con)
    con.commit()
    pu, po = _paamelding(con, u, "u@x.no"), _paamelding(con, ok, "o@x.no")
    _kjor(con, date(2027, 3, 19))
    assert _tilstand(con, pu)[0] == 0 and _tilstand(con, po) == (1, "2027-03-20", 0)


# ---- E. ingen automatisk faktura ----

@pytest.mark.parametrize("kw", [{"pris_nok": 0}, {"fakturering": "organisasjon"}, {"fakturering": "ingen"}],
                         ids=["gratis", "organisasjon", "ingen"])
def test_e_ingen_automatisk_faktura_er_uendret_ingen_hold_sveiper_kjort_1(con, ute, kw):
    kid = _betalt(con, **kw)
    pid = _paamelding(con, kid)
    _kjor(con, date(2027, 1, 1), pid)
    assert _tilstand(con, pid) == (1, None, 0) and ute["visma"] == [] and _antall(con, "faktura") == 0


# ---- F. per_samling uendret ----

def test_f_per_samling_er_uendret_ogsaa_med_faktura_naa(con, ute):
    kid = _betalt(con, ant_dager=2, betaling="per_samling")
    pid = _paamelding(con, kid, faktura_onskes_na=1)
    _kjor(con, date(2027, 1, 1), pid)                                               # samlingene er langt frem: ingenting forfalt
    assert ute["visma"] == [] and _antall(con, "faktura") == 0
    assert _tilstand(con, pid) == (1, None, 1)                                      # sveiper_kjort=1 som foer, aldri hold-dato
    sveiper.forfalte_delfakturaer(Kjoring(con, idag=FORSTE - timedelta(days=14)))   # egen forfallslogikk: 14 dager foer samling 1
    con.commit()
    assert ute["visma"] == [500] and _antall(con, "faktura", pid) == 1
    assert _tilstand(con, pid)[1] is None


# ---- G/H/I. daglig utloesning ----

def test_g_dagen_for_hold_dato_ingen_faktura_h_paa_datoen_noyaktig_en(con, ute):
    kid, pid = _hold(con, ute)
    daglig.kjor(Kjoring(con, idag=date(2027, 3, 19)))
    assert ute["visma"] == [] and _antall(con, "faktura") == 0                      # G: dagen foer
    daglig.kjor(Kjoring(con, idag=GRENSE))
    assert ute["visma"] == [1000] and _antall(con, "faktura", pid) == 1              # H: paa datoen
    assert _tilstand(con, pid) == (1, "2027-03-20", 0)                              # datoen beholdes som historikk


def test_i_flere_kjoringer_samme_dag_gir_fortsatt_en_faktura_og_ett_visma_forsok(con, ute):
    kid, pid = _hold(con, ute)
    for _ in range(3):
        daglig.kjor(Kjoring(con, idag=GRENSE))
    sveiper.utsatte_fakturaer(Kjoring(con, idag=GRENSE))
    con.commit()
    assert ute["visma"] == [1000] and _antall(con, "faktura") == 1 and _antall(con, "faktura_forsok") == 0
    daglig.kjor(Kjoring(con, idag=GRENSE + timedelta(days=40)))                      # og senere dager
    assert ute["visma"] == [1000] and _antall(con, "faktura") == 1


# ---- J. maalrettet utloesning ----

def test_j_faktura_naa_paa_allerede_holdt_rad_maalrettet_utloesning(con, ute):
    kid, pid = _hold(con, ute)
    annen = _paamelding(con, kid, "b@x.no")
    _kjor(con, date(2027, 3, 19), annen)
    assert _tilstand(con, annen)[1] == "2027-03-20"
    con.execute("UPDATE paamelding SET faktura_onskes_na=1 WHERE id=?", (pid,))    # seam: settes direkte i DB (ingen UI ennaa)
    con.commit()
    sveiper.utsatte_fakturaer(Kjoring(con, idag=date(2027, 3, 19)), paamelding_id=pid)
    con.commit()
    assert ute["visma"] == [1000] and _antall(con, "faktura", pid) == 1 and _antall(con, "faktura", annen) == 0   # bare den ene
    sveiper.utsatte_fakturaer(Kjoring(con, idag=date(2027, 3, 19)), paamelding_id=pid)
    con.commit()
    assert ute["visma"] == [1000]                                                    # idempotent


def test_j2_global_daglig_kjoring_plukker_ogsaa_faktura_naa_foer_datoen(con, ute):
    kid, pid = _hold(con, ute)
    con.execute("UPDATE paamelding SET faktura_onskes_na=1 WHERE id=?", (pid,))
    con.commit()
    daglig.kjor(Kjoring(con, idag=date(2027, 3, 19)))
    assert ute["visma"] == [1000] and _antall(con, "faktura", pid) == 1


# ---- K/L. avmelding og avlysning ----

def test_k_avmeldt_foer_datoen_gir_aldri_faktura(con, ute):
    kid, pid = _hold(con, ute)
    db.meld_av(con, pid)
    con.commit()
    daglig.kjor(Kjoring(con, idag=GRENSE))
    daglig.kjor(Kjoring(con, idag=GRENSE + timedelta(days=30)))
    assert ute["visma"] == [] and _antall(con, "faktura") == 0


def test_l_avlyst_foer_datoen_gir_ingen_faktura_og_gjenaapning_gir_faktura_naar_datoen_er_naadd(con, ute):
    kid, pid = _hold(con, ute)
    db.avlys_kurs(con, kid)
    con.commit()
    daglig.kjor(Kjoring(con, idag=GRENSE))
    sveiper.utsatte_fakturaer(Kjoring(con, idag=GRENSE), paamelding_id=pid)
    con.commit()
    assert ute["visma"] == [] and _antall(con, "faktura") == 0
    db.gjenaapne_kurs(con, kid)
    con.commit()
    daglig.kjor(Kjoring(con, idag=GRENSE))
    assert ute["visma"] == [1000] and _antall(con, "faktura", pid) == 1              # ikke krediteringer, bare eksisterende statussemantikk


def test_l2_kurs_i_utkast_utloeser_ikke(con, ute):
    kid, pid = _hold(con, ute)
    con.execute("UPDATE kurs SET status='utkast' WHERE id=?", (kid,))
    con.commit()
    sveiper.utsatte_fakturaer(Kjoring(con, idag=GRENSE))
    con.commit()
    assert ute["visma"] == []


def test_utsatte_fakturaer_plukker_ikke_andre_typer_rader(con, ute):
    """Kun bekreftet + sveiper_kjort=1 + samlet + lagret dato + person/pris: ingen annen rad kan faa faktura herfra."""
    kid, pid = _hold(con, ute)
    ikke_holdt = _paamelding(con, kid, "c@x.no")                                     # sveiper_kjort=0, ingen dato
    gratis = _betalt(con, kode="G1", pris_nok=0)
    pg = _paamelding(con, gratis, "g@x.no")
    _kjor(con, date(2027, 3, 19), pg)
    con.execute("UPDATE paamelding SET faktura_tidligst_dato='2027-01-01' WHERE id=?", (pg,))   # dato satt paa gratis kurs
    con.commit()
    sveiper.utsatte_fakturaer(Kjoring(con, idag=GRENSE))
    con.commit()
    assert _antall(con, "faktura", pid) == 1 and _antall(con, "faktura", ikke_holdt) == 0 and _antall(con, "faktura", pg) == 0
    assert ute["visma"] == [1000]


# ---- M. Visma ukjent ----

def test_m_visma_ukjent_gir_ingen_automatisk_nytt_forsok_og_ingen_dobbeltfaktura(con, ute, monkeypatch):
    kid, pid = _hold(con, ute)
    forsok = []

    def nede(g):
        forsok.append(1)
        raise RuntimeError("Visma nede")
    monkeypatch.setattr(visma, "fakturer", nede)
    daglig.kjor(Kjoring(con, idag=GRENSE))
    assert forsok == [1] and _antall(con, "faktura") == 0
    assert con.execute("SELECT status FROM faktura_forsok WHERE paamelding_id=?", (pid,)).fetchone()[0] == "ukjent"
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling IN ('faktura_ukjent','faktura_feil')").fetchone()[0] == 2
    daglig.kjor(Kjoring(con, idag=GRENSE + timedelta(days=1)))
    daglig.kjor(Kjoring(con, idag=GRENSE + timedelta(days=2)))
    sveiper.utsatte_fakturaer(Kjoring(con, idag=GRENSE + timedelta(days=2)), paamelding_id=pid)
    con.commit()
    assert forsok == [1] and _antall(con, "faktura") == 0                              # aldri nytt forsok
    assert _tilstand(con, pid)[0] == 1                                                  # sveiper_kjort uendret (hold-kjeden var ferdig)


def test_en_feil_paa_en_utsatt_faktura_stopper_ikke_de_andre(con, ute, monkeypatch):
    kid, pid = _hold(con, ute)
    andre = _paamelding(con, kid, "b@x.no")
    _kjor(con, date(2027, 3, 19), andre)
    ekte = visma.fakturer
    kall = []

    def hakkete(g):
        kall.append(1)
        if len(kall) == 1:
            raise RuntimeError("feil")
        return ekte(g)
    monkeypatch.setattr(visma, "fakturer", hakkete)
    sveiper.utsatte_fakturaer(Kjoring(con, idag=GRENSE))
    con.commit()
    assert len(kall) == 2 and _antall(con, "faktura") == 1                              # den ene feilet, den andre gikk gjennom


# ---- N. oekonomilaas ----

def _laast(con, kid):
    return db.har_okonomisk_binding_for_kurs(con, kid)


def test_n1_ingen_binding_er_ikke_laast(con):
    kid = _betalt(con)
    _paamelding(con, kid)
    assert _laast(con, kid) is False


def test_n2_ekte_faktura_laaser(con):
    kid = _betalt(con)
    pid = _paamelding(con, kid)
    con.execute("INSERT INTO faktura (paamelding_id, belop_nok, status) VALUES (?, 1000, 'sendt')", (pid,))
    con.commit()
    assert _laast(con, kid) is True


@pytest.mark.parametrize("status", ["reservert", "feilet", "ukjent"])
def test_n3_5_faktura_forsok_laaser_uansett_status(con, status):
    kid = _betalt(con)
    pid = _paamelding(con, kid)
    con.execute("INSERT INTO faktura_forsok (paamelding_id, status) VALUES (?, ?)", (pid, status))
    con.commit()
    assert _laast(con, kid) is True


def test_n3_faktura_forsok_laaser_ogsaa_naar_deltakeren_senere_er_avmeldt(con):
    kid = _betalt(con)
    pid = _paamelding(con, kid)
    con.execute("INSERT INTO faktura_forsok (paamelding_id, status) VALUES (?, 'ukjent')", (pid,))
    db.meld_av(con, pid)
    con.commit()
    assert _laast(con, kid) is True


def test_n6_aktiv_bekreftet_holdt_faktura_laaser(con, ute):
    kid, pid = _hold(con, ute)
    assert _laast(con, kid) is True


def test_n7_kun_avmeldt_historisk_hold_uten_faktura_eller_forsok_laaser_ikke(con, ute):
    kid, pid = _hold(con, ute)
    db.meld_av(con, pid)
    con.commit()
    assert _tilstand(con, pid)[1] == "2027-03-20" and _laast(con, kid) is False


def test_n_laasen_er_per_kurs(con, ute):
    kid, pid = _hold(con, ute)
    annet = _betalt(con, kode="B2")
    assert _laast(con, kid) is True and _laast(con, annet) is False


@pytest.mark.parametrize("binding", ["faktura", "forsok", "hold"])
def test_n8_okonomifelt_kan_ikke_endres_serverside_naar_laasen_er_aktiv(con, ute, binding):
    kid = _betalt(con)
    pid = _paamelding(con, kid)
    if binding == "faktura":
        con.execute("INSERT INTO faktura (paamelding_id, belop_nok, status) VALUES (?, 1000, 'sendt')", (pid,))
    elif binding == "forsok":
        con.execute("INSERT INTO faktura_forsok (paamelding_id, status) VALUES (?, 'feilet')", (pid,))
    else:
        con.execute("UPDATE paamelding SET faktura_tidligst_dato='2027-03-20' WHERE id=?", (pid,))
    con.commit()
    endret = db.oppdater_kurs_felter(con, kid, {"pris_nok": 99999, "fakturering": "ingen", "betaling": "per_samling",
                                                "faktura_dager_for": 1, "notat": "ok"})
    assert endret == ["notat"]                                                                # ulaaste felt endres fortsatt
    r = con.execute("SELECT pris_nok, fakturering, betaling, faktura_dager_for FROM kurs WHERE id=?", (kid,)).fetchone()
    assert tuple(r) == (1000, "person", "samlet", 14)


def test_n8_okonomifelt_kan_endres_naar_ingen_binding(con):
    kid = _betalt(con)
    _paamelding(con, kid)
    endret = db.oppdater_kurs_felter(con, kid, {"pris_nok": 2000, "faktura_dager_for": 30})
    assert sorted(endret) == ["faktura_dager_for", "pris_nok"]


def test_n8_direkte_post_kan_ikke_omga_hold_laasen_og_siden_viser_laast(con, ute):
    kid, pid = _hold(con, ute)
    from kurs.web import app as webapp
    klient = webapp.app.test_client()
    klient.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    data = {"navn": "Kurs B1", "type": "fysisk", "sted": "Bergen", "zoom_url": "", "kapasitet": "", "paameldingsfrist": "",
            "pris_nok": "99999", "fakturering": "ingen", "betaling": "per_samling", "faktura_dager_for": "1",
            "kursholder_epost": "", "notat": "Endret"}
    klient.post(f"/admin/kurs/{kid}/oppsett", data=data)
    r = db.koble(config.DB_STI).execute("SELECT pris_nok, fakturering, betaling, faktura_dager_for, notat FROM kurs WHERE id=?", (kid,)).fetchone()
    assert tuple(r) == (1000, "person", "samlet", 14, "Endret")
    side = klient.get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True)
    assert "kan ikke endres" in side and "opprettet, forsøkt eller planlagt" in side


def test_n9_antall_faktura_for_kurs_beholder_count_semantikk(con, ute):
    kid, pid = _hold(con, ute)
    assert db.antall_faktura_for_kurs(con, kid) == 0                                          # hold og forsok teller IKKE som faktura
    con.execute("INSERT INTO faktura_forsok (paamelding_id, status) VALUES (?, 'ukjent')", (pid,))
    con.commit()
    assert db.antall_faktura_for_kurs(con, kid) == 0
    con.execute("DELETE FROM faktura_forsok")
    con.execute("INSERT INTO faktura (paamelding_id, belop_nok, status) VALUES (?, 1000, 'sendt')", (pid,))
    con.commit()
    assert db.antall_faktura_for_kurs(con, kid) == 1


# ---- venteliste og reaktivering ----

def test_venteliste_faar_ikke_hold_og_faar_plan_ved_opprykk(con, ute):
    kid = _betalt(con, kapasitet=1)
    forste = _paamelding(con, kid, "a@x.no")
    venter = _paamelding(con, kid, "b@x.no")
    _kjor(con, date(2027, 1, 1))
    assert _tilstand(con, venter)[:2] == (0, None) and _tilstand(con, forste)[:2] == (1, "2027-03-20")
    db.meld_av(con, forste)
    con.commit()
    _kjor(con, date(2027, 3, 20), venter)                                                     # opprykk: beslutningen tas opprykksdagen
    assert _antall(con, "faktura", venter) == 1 and _tilstand(con, venter)[1] is None


def test_venteliste_opprykk_langt_frem_gir_hold_og_faktura_naa_bevares(con, ute):
    kid = _betalt(con, kapasitet=1)
    forste = _paamelding(con, kid, "a@x.no")
    venter = _paamelding(con, kid, "b@x.no", faktura_onskes_na=1)
    _kjor(con, date(2027, 1, 1))
    db.meld_av(con, forste)
    con.commit()
    _kjor(con, date(2027, 1, 2), venter)
    assert _antall(con, "faktura", venter) == 1                                                # "faktura naa" fulgte med gjennom ventelisten



# ======================= kursdatoendring og statusoverganger (state-integritet) =======================

def test_kursdager_skrives_kun_av_opprett_kurs_dvs_ingen_editor_finnes():
    """Vakt: i dag skriver KUN db.opprett_kurs til kursdag (ingen rute/funksjon endrer eller sletter kursdager). Legges det til
    kode som gjoer det, MAA den kalle sveiper.oppdater_faktura_hold_etter_kursdagendring() i samme transaksjon - og denne testen
    oppdateres bevisst."""
    rot = Path(__file__).resolve().parent.parent / "kurs"
    moenster = re.compile(r"\b(insert(\s+or\s+\w+)?\s+into|update|delete\s+from|replace\s+into)\s+kursdag\b", re.I)
    funnet = set()
    for fil in sorted(rot.rglob("*.py")):
        tre = ast.parse(fil.read_text(encoding="utf-8"))
        for fn in (n for n in ast.walk(tre) if isinstance(n, ast.FunctionDef)):
            for c in (n for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)):
                if moenster.search(c.value):
                    funnet.add((fil.relative_to(rot).as_posix(), fn.name))
    assert funnet == {("db.py", "opprett_kurs")}, funnet


def _flytt_forste_kursdag(con, kid, ny_dato: str, kall_hjelper=True):
    """Simulerer en fremtidig kursdag-editor: endrer datoen og kaller hold-oppdateringen i SAMME transaksjon."""
    con.execute("UPDATE kursdag SET dato=? WHERE kurs_id=?", (ny_dato, kid))
    antall = sveiper.oppdater_faktura_hold_etter_kursdagendring(con, kid) if kall_hjelper else None
    con.commit()
    return antall


def _dato(con, pid):
    return con.execute("SELECT faktura_tidligst_dato FROM paamelding WHERE id=?", (pid,)).fetchone()[0]


# ---- J1. flyttet senere ----

def test_j1_kurs_flyttes_senere_hold_dato_folger_med_uten_ekstern_sideeffekt(con, ute):
    kid, pid = _hold(con, ute)                                                    # 20.09.2027 -> hold 20.03.2027
    assert _flytt_forste_kursdag(con, kid, "2027-12-20") == 1
    assert _dato(con, pid) == "2027-06-20"                                        # seks kalendermaaneder foer 20.12.2027, IKKE 20.03
    assert ute["visma"] == [] and _antall(con, "faktura") == 0 and _antall(con, "faktura_forsok") == 0
    assert _tilstand(con, pid)[0] == 1                                            # fortsatt lovlig ventende
    sveiper.utsatte_fakturaer(Kjoring(con, idag=GRENSE))                          # gammel dato 20.03 utloeser ikke lenger
    con.commit()
    assert ute["visma"] == [] and _antall(con, "faktura") == 0
    log = [json.loads(r[0]) for r in con.execute("SELECT detaljer FROM hendelse WHERE handling='faktura_hold_oppdatert'")]
    assert log == [{"kurs_id": kid, "antall": 1}]                                 # ingen persondata


def test_j1_bruker_kalendermaaneder_ikke_180_dager(con, ute):
    kid, pid = _hold(con, ute)
    _flytt_forste_kursdag(con, kid, "2027-08-31")
    assert _dato(con, pid) == "2027-02-28"                                        # 180 dager ville gitt 2027-03-04


def test_helperen_er_idempotent(con, ute):
    kid, pid = _hold(con, ute)
    assert _flytt_forste_kursdag(con, kid, "2027-12-20") == 1
    assert sveiper.oppdater_faktura_hold_etter_kursdagendring(con, kid) == 0
    assert _dato(con, pid) == "2027-06-20"


# ---- J2. flyttet naermere: ny dato allerede passert ----

def test_j2_kurs_flyttes_naermere_ny_dato_passert_utsatte_fakturaer_lager_noyaktig_en(con, ute):
    kid, pid = _hold(con, ute, idag=date(2027, 3, 19))                            # hold 20.03.2027
    _flytt_forste_kursdag(con, kid, "2027-05-20")                                 # ny grense 20.11.2026 - allerede passert
    assert _dato(con, pid) == "2026-11-20"
    assert ute["visma"] == [] and _antall(con, "faktura") == 0 and _antall(con, "faktura_forsok") == 0   # ingen Visma i redigeringen
    daglig.kjor(Kjoring(con, idag=date(2027, 3, 19)))                             # dagen FOER den gamle datoen: plukkes likevel
    assert ute["visma"] == [1000] and _antall(con, "faktura", pid) == 1
    daglig.kjor(Kjoring(con, idag=date(2027, 3, 19)))
    assert ute["visma"] == [1000] and _antall(con, "faktura") == 1


# ---- J3/J4. aldri omskriv historikk etter ekstern oekonomisk aktivitet ----

def test_j3_ekte_faktura_finnes_datoendring_omskriver_ikke_historisk_dato(con, ute):
    kid, pid = _hold(con, ute)
    sveiper.utsatte_fakturaer(Kjoring(con, idag=GRENSE))
    con.commit()
    assert _antall(con, "faktura", pid) == 1
    assert _flytt_forste_kursdag(con, kid, "2027-12-20") == 0
    assert _dato(con, pid) == "2027-03-20"


@pytest.mark.parametrize("status", ["reservert", "feilet", "ukjent"])
def test_j4_faktura_forsok_finnes_datoendring_omskriver_ikke_historisk_dato(con, ute, status):
    kid, pid = _hold(con, ute)
    con.execute("INSERT INTO faktura_forsok (paamelding_id, status) VALUES (?, ?)", (pid, status))
    con.commit()
    assert _flytt_forste_kursdag(con, kid, "2027-12-20") == 0
    assert _dato(con, pid) == "2027-03-20"


def test_j4_ukjent_visma_forsok_fra_ekte_flyt_omskrives_ikke(con, ute, monkeypatch):
    kid, pid = _hold(con, ute)
    monkeypatch.setattr(visma, "fakturer", lambda g: (_ for _ in ()).throw(RuntimeError("Visma nede")))
    sveiper.utsatte_fakturaer(Kjoring(con, idag=GRENSE))
    con.commit()
    assert con.execute("SELECT status FROM faktura_forsok WHERE paamelding_id=?", (pid,)).fetchone()[0] == "ukjent"
    assert _flytt_forste_kursdag(con, kid, "2027-12-20") == 0
    assert _dato(con, pid) == "2027-03-20"


# ---- J5. avmeldt historisk hold ----

def test_j5_avmeldt_historisk_hold_oppdateres_ikke_og_blir_ikke_fakturakandidat(con, ute):
    kid, pid = _hold(con, ute)
    db.meld_av(con, pid)
    con.commit()
    assert _flytt_forste_kursdag(con, kid, "2027-05-20") == 0
    assert _dato(con, pid) == "2027-03-20"                                        # ikke roert
    daglig.kjor(Kjoring(con, idag=date(2027, 6, 1)))
    assert ute["visma"] == [] and _antall(con, "faktura") == 0                    # ikke aktiv kandidat
    assert db.har_okonomisk_binding_for_kurs(con, kid) is False


# ---- J6. flere aktive holds ----

def test_j6_alle_kvalifiserte_holds_paa_kurset_oppdateres_andre_roeres_ikke(con, ute):
    kid = _betalt(con)
    holds = [_paamelding(con, kid, f"h{i}@x.no") for i in range(3)]
    avmeldt = _paamelding(con, kid, "av@x.no")
    ikke_behandlet = _paamelding(con, kid, "ny@x.no")
    _kjor(con, date(2027, 3, 19), holds[0])
    for pid in holds[1:] + [avmeldt]:
        _kjor(con, date(2027, 3, 19), pid)
    db.meld_av(con, avmeldt)
    con.commit()
    annet = _betalt(con, kode="B2")
    p_annet = _paamelding(con, annet, "x@x.no")
    _kjor(con, date(2027, 3, 19), p_annet)
    assert _flytt_forste_kursdag(con, kid, "2027-12-20") == 3
    assert [_dato(con, p) for p in holds] == ["2027-06-20"] * 3
    assert _dato(con, avmeldt) == "2027-03-20" and _dato(con, ikke_behandlet) is None
    assert _dato(con, p_annet) == "2027-03-20"                                    # annet kurs er uberoert


# ---- J7. per_samling og ikke-automatisk fakturering ----

def test_j7_per_samling_faar_aldri_seksmaaneders_hold_ved_datoendring(con, ute):
    kid = _betalt(con, ant_dager=2, betaling="per_samling")
    pid = _paamelding(con, kid)
    _kjor(con, date(2027, 1, 1), pid)
    assert _dato(con, pid) is None
    con.execute("UPDATE kursdag SET dato='2027-12-20' WHERE id=(SELECT MIN(id) FROM kursdag WHERE kurs_id=?)", (kid,))
    assert sveiper.oppdater_faktura_hold_etter_kursdagendring(con, kid) == 0
    con.commit()
    assert _dato(con, pid) is None and _antall(con, "faktura") == 0
    con.execute("UPDATE paamelding SET faktura_tidligst_dato='2020-01-01' WHERE id=?", (pid,))   # selv med en tilfeldig verdi: per_samling roeres ikke
    assert sveiper.oppdater_faktura_hold_etter_kursdagendring(con, kid) == 0
    assert _dato(con, pid) == "2020-01-01"


@pytest.mark.parametrize("kw", [{"pris_nok": 0}, {"fakturering": "organisasjon"}, {"fakturering": "ingen"}],
                         ids=["gratis", "organisasjon", "ingen"])
def test_j7_ikke_automatisk_fakturering_roeres_ikke(con, ute, kw):
    kid = _betalt(con, **kw)
    pid = _paamelding(con, kid)
    _kjor(con, date(2027, 1, 1), pid)
    con.execute("UPDATE paamelding SET faktura_tidligst_dato='2020-01-01' WHERE id=?", (pid,))    # tilfeldig gammel verdi
    con.commit()
    assert _flytt_forste_kursdag(con, kid, "2027-12-20") == 0 and _dato(con, pid) == "2020-01-01"


# ---- D. manglende kursdag etter redigering ----

def test_d_alle_kursdager_borte_holdt_faktura_kan_ikke_utloese_gammel_dato(con, ute):
    """Kan bare skje med en fremtidig editor/SQL (ingen finnes i dag): gammel dato skal ikke overleve, og motoren gaar til mangler_kursdag."""
    kid, pid = _hold(con, ute)
    con.execute("DELETE FROM kursdag WHERE kurs_id=?", (kid,))
    assert sveiper.oppdater_faktura_hold_etter_kursdagendring(con, kid) == 1
    con.commit()
    assert _tilstand(con, pid) == (0, None, 0)                                    # dato NULL og sveiper_kjort=0
    daglig.kjor(Kjoring(con, idag=date(2028, 1, 1)))
    assert ute["visma"] == [] and _antall(con, "faktura") == 0 and _antall(con, "faktura_forsok") == 0
    assert _tilstand(con, pid)[0] == 0                                            # fortsatt uferdig: konfigurasjonsfeilen skjules ikke
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='faktura_feil'").fetchone()[0] >= 1


# ---- K. statusoverganger ----

def _venteliste_med_gammel_dato(con, onskes_na, kapasitet=1):
    """En venteliste-rad som (unormalt) har en gammel faktura_tidligst_dato og ev. eksplisitt faktura_onskes_na."""
    kid = _betalt(con, kapasitet=kapasitet)
    forste = _paamelding(con, kid, "a@x.no")
    venter = _paamelding(con, kid, "b@x.no", faktura_onskes_na=onskes_na)
    assert con.execute("SELECT status FROM paamelding WHERE id=?", (venter,)).fetchone()[0] == "venteliste"
    con.execute("UPDATE paamelding SET faktura_tidligst_dato='2020-01-01' WHERE id=?", (venter,))
    con.commit()
    return kid, forste, venter


def _opprykk(con, kid, forste, venter, vei):
    if vei == "sett_status":
        con.execute("UPDATE kurs SET kapasitet=2 WHERE id=?", (kid,))
        db.sett_paamelding_status(con, venter, "bekreftet")
    elif vei == "meld_av":
        assert db.meld_av(con, forste) == venter
    else:
        assert db.endre_kapasitet(con, kid, 2) == [venter]
    con.commit()


@pytest.mark.parametrize("vei", ["sett_status", "meld_av", "kapasitet"])
def test_k_venteliste_til_bekreftet_bevarer_faktura_onskes_na_og_ingen_gammel_dato_brukes(con, ute, vei):
    kid, forste, venter = _venteliste_med_gammel_dato(con, onskes_na=1)
    _opprykk(con, kid, forste, venter, vei)
    assert _tilstand(con, venter) == (0, None, 1)                                 # onskes_na=1 bevart, gammel dato borte, sveiper_kjort=0
    _kjor(con, date(2027, 1, 1), venter)                                          # ny plan fra dagens oppsett: "faktura naa" -> na
    assert _antall(con, "faktura", venter) == 1 and _dato(con, venter) is None


@pytest.mark.parametrize("vei", ["sett_status", "meld_av", "kapasitet"])
def test_k_venteliste_til_bekreftet_uten_onskes_na_faar_ny_hold_fra_dagens_kursdato(con, ute, vei):
    kid, forste, venter = _venteliste_med_gammel_dato(con, onskes_na=0)
    _opprykk(con, kid, forste, venter, vei)
    assert _tilstand(con, venter) == (0, None, 0)
    _kjor(con, date(2027, 3, 19), venter)
    assert _dato(con, venter) == "2027-03-20" and _antall(con, "faktura", venter) == 0    # ny dato fra kursoppsettet, ikke 2020-01-01


def test_k_avmeldt_til_bekreftet_gammel_hold_kan_ikke_bli_aktiv_uten_ny_planberegning(con, ute):
    kid, pid = _hold(con, ute)
    db.sett_paamelding_status(con, pid, "avmeldt")
    con.commit()
    assert _dato(con, pid) == "2027-03-20"                                        # avmeldt: historisk, roeres ikke av avmeldingen
    kjor_for = db.sett_paamelding_status(con, pid, "bekreftet")
    con.commit()
    assert kjor_for == pid
    assert _tilstand(con, pid)[:2] == (0, None)                                   # dato NULL og sveiper_kjort=0 - ny planberegning kreves
    assert db.har_okonomisk_binding_for_kurs(con, kid) is False
    sveiper.utsatte_fakturaer(Kjoring(con, idag=date(2027, 6, 1)))                # sveiper_kjort=0: kan ikke plukkes som gammel hold
    con.commit()
    assert ute["visma"] == []
    _kjor(con, date(2027, 3, 19), pid)                                            # normal sveiper beregner NY plan
    assert _tilstand(con, pid)[:2] == (1, "2027-03-20")


def test_k_avmeldt_til_venteliste_fjerner_ogsaa_gammel_hold_dato(con, ute):
    kid, pid = _hold(con, ute)
    db.sett_paamelding_status(con, pid, "avmeldt")
    db.sett_paamelding_status(con, pid, "venteliste")
    con.commit()
    assert _tilstand(con, pid)[:2] == (0, None)


@pytest.mark.parametrize("status", ["ukjent", "reservert", "feilet"])
def test_k_gammel_dato_beholdes_som_historikk_naar_faktura_forsok_finnes(con, ute, status):
    kid, pid = _hold(con, ute)
    con.execute("INSERT INTO faktura_forsok (paamelding_id, status) VALUES (?, ?)", (pid, status))
    db.sett_paamelding_status(con, pid, "avmeldt")
    db.sett_paamelding_status(con, pid, "bekreftet")
    con.commit()
    assert _dato(con, pid) == "2027-03-20"                                        # ekstern oekonomisk aktivitet: historikk roeres ikke


def test_k_gammel_dato_beholdes_som_historikk_naar_ekte_faktura_finnes(con, ute):
    kid, pid = _hold(con, ute)
    sveiper.utsatte_fakturaer(Kjoring(con, idag=GRENSE))
    db.sett_paamelding_status(con, pid, "avmeldt")
    db.sett_paamelding_status(con, pid, "bekreftet")
    con.commit()
    assert _antall(con, "faktura", pid) == 1 and _dato(con, pid) == "2027-03-20"


def test_k_meld_paa_reaktivering_nullstiller_fortsatt_hold_og_onskes_na(con, ute):
    kid, pid = _hold(con, ute)
    con.execute("UPDATE paamelding SET faktura_onskes_na=1 WHERE id=?", (pid,))
    db.meld_av(con, pid)
    con.commit()
    pid2, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.commit()
    assert pid2 == pid and _tilstand(con, pid) == (0, None, 0)


# ---- Produktregel: gjenaapning av samme registrering (status) vs NY registreringsrunde (meld_paa) ----

def _hold_med_onske(con, ute):
    """Holdt faktura (20.03.2027) der deltakeren ETTERPAA har bedt om faktura naa (satt direkte i DB, ingen UI ennaa)."""
    kid, pid = _hold(con, ute)
    con.execute("UPDATE paamelding SET faktura_onskes_na=1 WHERE id=?", (pid,))
    con.commit()
    assert _tilstand(con, pid) == (1, "2027-03-20", 1)
    return kid, pid


def test_regel_a_bekreftet_avmeldt_bekreftet_beholder_faktura_onskes_na_og_bruker_ny_plan(con, ute):
    kid, pid = _hold_med_onske(con, ute)
    db.sett_paamelding_status(con, pid, "avmeldt")
    kjor_for = db.sett_paamelding_status(con, pid, "bekreftet")
    con.commit()
    assert kjor_for == pid
    assert _tilstand(con, pid) == (0, None, 1)                       # onskes_na BEHOLDT, gammel hold-dato frigitt, sveiper_kjort=0
    assert _antall(con, "faktura") == 0 and ute["visma"] == []       # statusendringen kontakter aldri Visma
    _kjor(con, date(2027, 1, 1), pid)                                # normal sveiper: dagens plan, langt for grensen
    assert ute["visma"] == [1000] and _antall(con, "faktura", pid) == 1     # "faktura naa" -> PLAN_NA -> umiddelbar faktura
    assert _tilstand(con, pid) == (1, None, 1)                       # ingen ny hold, og gammel dato ble ikke gjenbrukt


def test_regel_a_planen_som_brukes_er_dagens_plan_ikke_den_gamle_hold_datoen(con, ute):
    kid, pid = _hold_med_onske(con, ute)
    db.sett_paamelding_status(con, pid, "avmeldt")
    db.sett_paamelding_status(con, pid, "bekreftet")
    con.commit()
    p = con.execute(sveiper.SQL_DELTAKER + " WHERE p.id=?", (pid,)).fetchone()
    plan = sveiper._lag_plan(Kjoring(con, idag=date(2027, 1, 1)), p)
    assert plan == sveiper.FakturaPlan(sveiper.PLAN_NA)              # onskes_na=1 og kursdag finnes


def test_regel_b_avmeldt_til_venteliste_beholder_onske_frigir_hold_ingen_faktura_paa_venteliste(con, ute):
    kid, pid = _hold_med_onske(con, ute)
    db.sett_paamelding_status(con, pid, "avmeldt")
    db.sett_paamelding_status(con, pid, "venteliste")
    con.commit()
    assert _tilstand(con, pid) == (0, None, 1)                       # onskes_na beholdt, gammel hold-dato frigitt
    _kjor(con, date(2027, 1, 1), pid)                                # status venteliste: ventelistemail, ALDRI faktura
    assert _antall(con, "faktura") == 0 and ute["visma"] == []
    assert _tilstand(con, pid) == (0, None, 1)
    assert db.allerede_sendt(con, f"kurs:{kid}", "a@x.no", "venteliste")
    daglig.kjor(Kjoring(con, idag=date(2027, 3, 25)))                # heller ikke etter gammel hold-dato
    assert _antall(con, "faktura") == 0 and ute["visma"] == []


def test_regel_b_venteliste_til_bekreftet_beholder_onske_og_normal_sveiper_kan_fakturere(con, ute):
    kid, pid = _hold_med_onske(con, ute)
    db.sett_paamelding_status(con, pid, "avmeldt")
    db.sett_paamelding_status(con, pid, "venteliste")
    _kjor(con, date(2027, 1, 1), pid)
    kjor_for = db.sett_paamelding_status(con, pid, "bekreftet")
    con.commit()
    assert kjor_for == pid and _tilstand(con, pid) == (0, None, 1)   # onskes_na fortsatt 1, sveiper_kjort=0, ingen gammel dato
    _kjor(con, date(2027, 1, 2), pid)
    assert ute["visma"] == [1000] and _antall(con, "faktura", pid) == 1


def test_regel_c_kontrast_ny_registreringsrunde_via_meld_paa_nullstiller_med_mindre_eksplisitt_faktura_naa(con, ute):
    kid, pid = _hold_med_onske(con, ute)
    db.meld_av(con, pid)
    con.commit()
    pid2, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")                                   # NY runde uten eksplisitt valg
    con.commit()
    assert pid2 == pid and _tilstand(con, pid) == (0, None, 0)                                  # nullstilt: 0 / NULL
    con.execute("UPDATE paamelding SET faktura_onskes_na=1, faktura_tidligst_dato='2027-03-20' WHERE id=?", (pid,))
    db.meld_av(con, pid)
    con.commit()
    pid3, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A", paamelding={"faktura_onskes_na": 1})   # NY runde MED eksplisitt valg
    con.commit()
    assert pid3 == pid and _tilstand(con, pid) == (0, None, 1)                                  # onske fra den nye callen, hold-dato alltid NULL
