"""Faktura tidligst seks kalendermaaneder foer foerste kursdag.

Steg 1: kalenderaritmetikken (ren funksjon, ingen DB).
Steg 2: KUN datamodell + migrering (faktura_onskes_na, faktura_tidligst_dato) - ren lagringskapasitet, ingen
        kobling til fakturabeslutningen. Fakturaflyten er i senere sjekkpunkter.
"""
import ast
import dataclasses
import inspect
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
    """Kurset starter 18 maaneder frem. Steg 3A: faktura_plan() finnes, men er IKKE koblet inn - motoren fakturerer
    derfor fortsatt umiddelbart (med vilje 'feil' mot den nye regelen), og valget paavirker ikke noe."""
    plan = sveiper.faktura_plan(fakturering="person", pris_nok=1000, status="bekreftet", betaling="samlet",
                                faktura_onskes_na=onskes_na, kursdager=["2028-09-20"], idag=date(2027, 3, 1))
    assert plan.modus == (sveiper.PLAN_NA if onskes_na else sveiper.PLAN_UTSATT)   # planen VILLE utsatt - men er ikke koblet
    kid = _kurs(con, start=date(2028, 9, 20), pris_nok=1000, fakturering="person")
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A", paamelding={"faktura_onskes_na": onskes_na},
                         idag=date(2027, 3, 1))
    con.commit()
    sveiper.kjor(Kjoring(con, idag=date(2027, 3, 1)), pid)
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", (pid,)).fetchone()[0] == 1
    assert con.execute("SELECT sveiper_kjort FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == 1
    assert _fakturafelt(con, pid) == (onskes_na, None)                     # faktura_tidligst_dato settes ikke av noe


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


def test_faktura_plan_er_ikke_koblet_til_fakturaflyten_ennaa():
    """Steg 3A-vakt: planen og de nye feltene er IKKE i bruk i fakturabeslutningen. OPPDATERES naar planen kobles inn (3B)."""
    assert _kall_i_kodebasen("faktura_plan") == []                                            # ingen kaller planen
    assert _kall_i_kodebasen("seks_maaneder_for") == [("sveiper.py", "faktura_plan")]         # bare planen bruker regelen
    rot = Path(__file__).resolve().parent.parent / "kurs"
    for fil in ("daglig.py", "behandling.py", "kjoring.py", "web/app.py", "maltekster.py"):
        tekst = (rot / fil).read_text(encoding="utf-8")
        for navn in ("faktura_tidligst_dato", "faktura_onskes_na", "faktura_plan", "seks_maaneder_for"):
            assert navn not in tekst, (fil, navn)
    sveiper_tekst = (rot / "sveiper.py").read_text(encoding="utf-8")
    tre = ast.parse(sveiper_tekst)
    docstrenger = {id(n.body[0].value) for n in ast.walk(tre) if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))
                   and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    bruk = ([n.id for n in ast.walk(tre) if isinstance(n, ast.Name)] + [n.attr for n in ast.walk(tre) if isinstance(n, ast.Attribute)]
            + [n.arg for n in ast.walk(tre) if isinstance(n, (ast.arg, ast.keyword)) and n.arg]
            + [n.value for n in ast.walk(tre) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrenger])
    assert not [b for b in bruk if "faktura_tidligst_dato" in b]                              # lagret beslutning er aldri input (kun omtalt i docstring)
    plan_fn = next(n for n in tre.body if isinstance(n, ast.FunctionDef) and n.name == "faktura_plan")
    utenfor = sveiper_tekst.replace(ast.get_source_segment(sveiper_tekst, plan_fn), "")
    assert "faktura_onskes_na" not in utenfor                                                 # ingen annen sveiper-kode leser valget


# ======================= STEG 3A: ren fakturaplan (faktura_plan) =======================

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
