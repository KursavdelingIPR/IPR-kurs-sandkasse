"""Kursside: migrering «kursside» (tabellene kursside, kursside_versjon, kursside_fil og kursside_fil_innhold).

Ny database, eksisterende database (med kurs, påmeldinger og data som ikke skal røres), kjørt to ganger, og en kopi av en ekte
demodatabase (hoppes over hvis den ikke finnes på maskinen). Bare oppdiktede data.
"""
import shutil
import sqlite3
from pathlib import Path

import pytest

from kurs import db, migreringer, sidelager

from kurssidehjelp import lag_deltaker, lag_kurs, ny_database, pdf, skriv_side, tekstblokk, dokument

TABELLER = ("kursside", "kursside_versjon", "kursside_fil", "kursside_fil_innhold")
SAMLET_DB = Path("C:/IPR-kurs-natt/samlet/data/kurs.db")           # forhåndsvisningens demodatabase (leses aldri direkte: bare en kopi brukes)


def _nr() -> int:
    return next(n for n, navn, _ in migreringer.MIGRERINGER if navn == "kursside")


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    yield c
    c.close()


def _fjern_kursside_tabellene(con) -> None:
    """Gjør databasen til en ny-1: slik den var før migreringen (tabellene finnes ikke, og versjonen står én lavere)."""
    for tabell in reversed(TABELLER):
        con.execute(f"DROP TABLE {tabell}")
    con.execute("DELETE FROM schema_versjon WHERE versjon >= ?", (_nr(),))
    con.commit()


def test_migreringen_er_registrert_med_navnet_kursside():
    """Kursside er migrering 15. Migrering 16 (ekstradeltaker) kommer etter den, så kursside er ikke lenger den siste."""
    assert migreringer.MIGRERINGER[_nr() - 1][:2] == (_nr(), "kursside") and migreringer.KODEVERSJON >= _nr()
    assert migreringer.MIGRERINGER[_nr() - 1][2].__name__ == "_m15_kursside"


def test_ny_database_har_tabellene_indeksene_og_versjonen(con):
    for tabell in TABELLER:
        assert db.har_tabell(con, tabell), tabell
    assert migreringer.gjeldende_versjon(con) == migreringer.KODEVERSJON
    assert con.execute("SELECT navn FROM schema_versjon WHERE versjon=?", (_nr(),)).fetchone()[0] == "kursside"
    assert set(db.kolonner(con, "kursside")) == {
        "kurs_id", "utkast", "publisert", "versjon", "publisert_versjon", "aktiv", "innsjekk_krever_kode", "stenges", "apen_dager", "endret", "endret_av",
        "publisert_tid", "publisert_av", "redigerer_av", "redigerer_til"}
    assert set(db.kolonner(con, "kursside_versjon")) == {"id", "kurs_id", "innhold", "arsak", "merknad", "versjon", "opprettet", "opprettet_av"}
    assert set(db.kolonner(con, "kursside_fil")) == {"id", "kurs_id", "filnavn", "type", "mimetype", "storrelse", "sha256", "bredde", "hoyde",
                                                     "opprettet", "opprettet_av"}
    assert db.kolonner(con, "kursside_fil_innhold") == ["fil_id", "innhold"]


@pytest.mark.kun_sqlite
def test_indekser_finnes_i_sqlite(con):
    navn = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"kursside_versjon_kurs", "kursside_fil_kurs"} <= navn


def test_gammel_database_faar_tabellene_uten_at_data_roeres(con):
    kid = lag_kurs(con, "GAMMEL")
    _did, pid = lag_deltaker(con, kid, "kari@example.no")
    con.execute("INSERT INTO sensitivt (paamelding_id, allergier) VALUES (?, 'noe')", (pid,))
    con.commit()
    foer = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("kurs", "kursdag", "deltaker", "paamelding", "sensitivt", "hendelse")}
    _fjern_kursside_tabellene(con)
    assert migreringer.gjeldende_versjon(con) == _nr() - 1 and not db.har_tabell(con, "kursside")
    assert migreringer.kjor_manglende(con) == [n for n, _, _ in migreringer.MIGRERINGER if n >= _nr()]   # kursside og de etter
    for tabell in TABELLER:
        assert db.har_tabell(con, tabell), tabell
    assert {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in foer} == {t: n for t, n in foer.items()}   # ingen data endret
    assert con.execute("SELECT COUNT(*) FROM kursside").fetchone()[0] == 0                # ingen kursside opprettes for eksisterende kurs
    assert migreringer.gjeldende_versjon(con) == migreringer.KODEVERSJON


def test_migreringen_kjort_to_ganger_endrer_ingenting(con):
    kid = lag_kurs(con)
    skriv_side(con, kid, dokument(tekstblokk()))
    fil_id, _ = sidelager.lagre_fil(con, kid, "a.pdf", pdf(), "admin:test")
    con.commit()
    assert migreringer.kjor_manglende(con) == []
    migreringer._m15_kursside(con)                                                        # selve funksjonen er idempotent
    migreringer._m15_kursside(con)
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM kursside").fetchone()[0] == 1
    assert sidelager.fil_meta(con, kid, fil_id)["filnavn"] == "a.pdf" and len(sidelager.versjoner(con, kid)) == 1
    assert migreringer.kjor_manglende(con) == []


def test_skjemaet_i_begge_filer_har_tabellene():
    for fil in ("schema.sql", "schema_postgres.sql"):
        tekst = (Path(migreringer.__file__).parent / fil).read_text(encoding="utf-8")
        for tabell in TABELLER:
            assert f"CREATE TABLE IF NOT EXISTS {tabell} (" in tekst, (fil, tabell)


@pytest.mark.kun_sqlite
def test_fremmednoeklene_stopper_rader_uten_kurs(con):
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO kursside (kurs_id) VALUES (9999)")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO kursside_fil_innhold (fil_id, innhold) VALUES (9999, 'x')")
    kid = lag_kurs(con)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO kursside_versjon (kurs_id, innhold, arsak) VALUES (?, '{}', 'ukjent')", (kid,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO kursside_fil (kurs_id, filnavn, type, mimetype, storrelse, sha256) VALUES (?, 'a', 'video', 'x', 1, 'h')", (kid,))
    con.rollback()


@pytest.mark.kun_sqlite
def test_kopi_av_forhandsvisningens_demodatabase_migreres_uten_tap_av_data(tmp_path, monkeypatch):
    """Skript-verifisering mot en KOPI av en ekte demodatabase (aldri originalen): alle rader finnes fortsatt etterpå, tabellene er opprettet,
    og en ny kursside kan lagres. Hoppes over når filen ikke finnes (f.eks. på en annen maskin)."""
    if not SAMLET_DB.exists():
        pytest.skip("demodatabasen finnes ikke her")
    kopi = tmp_path / "kopi.db"
    shutil.copyfile(SAMLET_DB, kopi)
    monkeypatch.setattr(db.config, "DB_STI", kopi)
    con = db.koble(kopi)
    tabeller = [r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    foer = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tabeller if t not in TABELLER + ("schema_versjon",)}
    kjort = db.init(con)
    assert all(n <= migreringer.KODEVERSJON for n in kjort) and migreringer.gjeldende_versjon(con) == migreringer.KODEVERSJON
    assert _nr() in kjort or migreringer.gjeldende_versjon(con) >= _nr()
    for tabell in TABELLER:
        assert db.har_tabell(con, tabell)
    for tabell, antall in foer.items():
        assert con.execute(f"SELECT COUNT(*) FROM {tabell}").fetchone()[0] >= antall, tabell     # hendelser kan komme til, ingenting forsvinner
    kurs_id = con.execute("SELECT id FROM kurs ORDER BY id LIMIT 1").fetchone()
    if kurs_id:
        skriv_side(con, kurs_id[0], dokument(tekstblokk()))
        assert sidelager.hent_publisert(con, kurs_id[0]) is not None
    assert db.init(con) == []
    con.close()
