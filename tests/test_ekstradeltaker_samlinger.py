"""Samlingsutvalget til ekstradeltakere (migrering 18, tabellen ekstradeltaker_samling).

En ekstradeltaker deltar på hele kurset (INGEN rader) eller bare på utvalgte samlinger (rader). db.paameldingens_kursdager er den
eneste veien til kursdagene en påmelding er på. Her: migreringen, lagring og normalisering av utvalget, opprydding når personen
slutter å være ekstradeltaker, samlinger som slettes, og «Samling N» som alltid er kursets eget nummer.
Bare oppdiktede data.
"""
import sqlite3
from datetime import date

import pytest
from ekstrahjelp import (ADMIN, DAGER, S, S1, S2, S3, antall, dager_for, ekstra, kurs_med_samlinger, meld, rad, samling_ider, sett,
                         status, utvalg_rader)
from kurssidehjelp import fast_dato, ny_database

from kurs import db, kursdatoer, migreringer


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    fast_dato(monkeypatch)
    yield c
    c.close()


# ============================ migrering 18 ============================

def test_migrering_18_finnes_og_tabellen_er_lik_i_begge_skjemafilene(con):
    nr18 = [(n, navn, f.__name__) for n, navn, f in migreringer.MIGRERINGER if n == 18]
    assert nr18 == [(18, "ekstradeltaker_samling", "_m18_ekstradeltaker_samling")]
    assert migreringer.KODEVERSJON >= 18 and migreringer.gjeldende_versjon(con) >= 18
    assert db.har_tabell(con, "ekstradeltaker_samling") and db.kolonner(con, "ekstradeltaker_samling") == ["paamelding_id", "samling_id"]
    for fil, type_ in (("schema.sql", "INTEGER"), ("schema_postgres.sql", "BIGINT")):
        tekst = (db._SCHEMA_POSTGRES if fil.endswith("postgres.sql") else db._SCHEMA).read_text(encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS ekstradeltaker_samling (" in tekst, fil
        assert f"paamelding_id   {type_} NOT NULL REFERENCES paamelding(id) ON DELETE CASCADE" in tekst, fil
        assert f"samling_id      {type_} NOT NULL REFERENCES samling(id) ON DELETE CASCADE" in tekst, fil
        assert "PRIMARY KEY (paamelding_id, samling_id)" in tekst, fil
    for fil in ("MIGRATIONS.md", "dokumentasjon/renummerering.md"):
        tekst = (db._SCHEMA.parent.parent / fil).read_text(encoding="utf-8")
        assert "ekstradeltaker_samling" in tekst, fil
    rad18 = next(linje for linje in (db._SCHEMA.parent.parent / "MIGRATIONS.md").read_text(encoding="utf-8").splitlines()
                 if linje.startswith("| 18 |"))
    assert "DROP TABLE ekstradeltaker_samling" in rad18 and "Ingen rader = hele kurset" in rad18       # rollback og regelen står der


def test_migrering_18_paa_eksisterende_database_beholder_alt_og_er_idempotent(con):
    kid = kurs_med_samlinger(con)
    ola, eva = meld(con, kid, "Ola"), ekstra(con, kid, "Eva")             # Eva er ekstradeltaker fra migrering 16: hele kurset
    for_ = [tuple(r) for r in con.execute("SELECT * FROM paamelding ORDER BY id")]
    # gjør databasen lik en som ikke har kjørt migrering 18 ennå
    con.execute("DROP TABLE ekstradeltaker_samling")
    con.execute("DELETE FROM schema_versjon WHERE versjon >= 18")
    con.commit()
    assert not db.har_tabell(con, "ekstradeltaker_samling") and migreringer.gjeldende_versjon(con) == 17
    assert migreringer.kjor_manglende(con) == [n for n, _, _ in migreringer.MIGRERINGER if n >= 18]
    assert db.har_tabell(con, "ekstradeltaker_samling")
    assert [tuple(r) for r in con.execute("SELECT * FROM paamelding ORDER BY id")] == for_          # ingen påmelding er endret
    assert utvalg_rader(con, eva) == [] and dager_for(con, eva) == dager_for(con, ola)               # hele kurset som før
    assert migreringer.kjor_manglende(con) == []                                                     # kjøres ikke to ganger
    migreringer._m18_ekstradeltaker_samling(con)                                                      # selve funksjonen er idempotent
    migreringer._m18_ekstradeltaker_samling(con)
    migreringer.kontroller(con)                                                                       # appen godtar databasen
    sett(con, eva, "paameldt")


def test_tabellen_har_primaernoekkel_og_cascade_paa_begge_fremmednoekler(con):
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[1, 3])
    s1, _s2, s3 = samling_ider(con, kid)
    assert utvalg_rader(con, eva) == sorted([s1, s3])
    con.commit()
    with pytest.raises(sqlite3.IntegrityError):                                                       # samme rad to ganger
        con.execute("INSERT INTO ekstradeltaker_samling (paamelding_id, samling_id) VALUES (?,?)", (eva, s1))
    con.rollback()
    con.commit()
    with pytest.raises(sqlite3.IntegrityError):                                                       # ukjent samling
        con.execute("INSERT INTO ekstradeltaker_samling (paamelding_id, samling_id) VALUES (?,?)", (eva, 999999))
    con.rollback()
    con.execute("DELETE FROM kursdag WHERE samling_id=?", (s3,))
    con.execute("DELETE FROM samling WHERE id=?", (s3,))                                              # CASCADE på samling
    assert utvalg_rader(con, eva) == [s1]
    con.execute("DELETE FROM oppmote WHERE paamelding_id=?", (eva,))
    con.execute("DELETE FROM paamelding WHERE id=?", (eva,))                                          # CASCADE på påmelding
    assert antall(con, "SELECT COUNT(*) FROM ekstradeltaker_samling") == 0


# ============================ paameldingens_kursdager ============================

def test_paameldt_og_ekstradeltaker_uten_utvalg_er_paa_alle_kursets_dager_med_samme_rader(con):
    kid = kurs_med_samlinger(con)
    ola, eva = meld(con, kid, "Ola"), ekstra(con, kid, "Eva")
    alle = db.kursdager(con, kid)
    assert [d["dato"] for d in alle] == sum(DAGER.values(), [])
    assert db.paameldingens_kursdager(con, ola, alle) is alle                                         # de samme radene: ingen ny form
    assert db.paameldingens_kursdager(con, rad(con, eva), alle) is alle
    assert db.paameldingens_kursdager(con, eva) == alle and db.paameldingens_kursdager(con, ola) == alle
    assert db.paameldingens_kursdager(con, 999999) == []                                               # ukjent påmelding
    assert db.samlingsutvalg_tekst(con, eva) == db.samlingsutvalg_tekst(con, ola) == "Hele kurset"


def test_ekstradeltaker_med_utvalg_faar_bare_dagene_i_de_valgte_samlingene(con):
    kid = kurs_med_samlinger(con)
    eva, nina = ekstra(con, kid, "Eva", nr=[1, 3]), ekstra(con, kid, "Nina", nr=[2])
    assert dager_for(con, eva) == DAGER[1] + DAGER[3] and dager_for(con, nina) == DAGER[2]
    rader = db.paameldingens_kursdager(con, eva)
    assert all(r["samling_flere"] is True for r in rader) and [r["samling_nr"] for r in rader] == [1, 1, 3]
    assert {"id", "dato", "start_kl", "slutt_kl", "samling_id", "samling_navn", "samling_start_kl", "timer"} <= set(rader[0])
    assert db.samlingsutvalg_tekst(con, eva) == "Samling 1, 3" and db.samlingsutvalg_tekst(con, eva, og=True) == "Samling 1 og 3"
    assert db.samlingsutvalg_tekst(con, nina) == "Samling 2"


def test_en_samling_alle_samlinger_og_tomt_utvalg(con):
    kid = kurs_med_samlinger(con)
    s1, s2, s3 = samling_ider(con, kid)
    eva = ekstra(con, kid, "Eva", nr=[3])
    assert dager_for(con, eva) == DAGER[3]                                                            # én samling (en enkeltdag)
    assert db.sett_ekstradeltaker_samlinger(con, eva, [s1, s2, s3]) == []                             # alle valgt = hele kurset
    assert utvalg_rader(con, eva) == [] and dager_for(con, eva) == sum(DAGER.values(), [])             # ingen rader
    assert db.sett_ekstradeltaker_samlinger(con, eva, [s2]) == [s2] and dager_for(con, eva) == DAGER[2]
    assert db.sett_ekstradeltaker_samlinger(con, eva, []) == [] and dager_for(con, eva) == sum(DAGER.values(), [])   # tomt = hele
    assert db.sett_ekstradeltaker_samlinger(con, eva, None) == []


def test_utvalget_kontrolleres_foer_noe_skrives(con):
    kid = kurs_med_samlinger(con)
    annet = kurs_med_samlinger(con, "ANNET", samlinger=[S(date(2032, 1, 12), date(2032, 1, 13), "09:00", "16:00", 6.0),
                                                        S(date(2032, 2, 9), None, "09:00", "16:00", 6.0)])
    s1, s2, _ = samling_ider(con, kid)
    eva = ekstra(con, kid, "Eva", nr=[1])
    fremmed = samling_ider(con, annet)[0]
    for ugyldig in ([fremmed], [s2, fremmed], [999999], ["abc"]):
        with pytest.raises(db.Paameldingsfeil):
            db.sett_ekstradeltaker_samlinger(con, eva, ugyldig)
    assert utvalg_rader(con, eva) == [s1]                                                             # uendret
    pia = meld(con, kid, "Pia")
    for ugyldig in ([fremmed], [999999]):                                                              # også ved statusendringen
        with pytest.raises(db.Paameldingsfeil):
            db.sett_paamelding_status(con, pia, "ekstradeltaker", aktor=ADMIN, samlinger=ugyldig)
    con.rollback()
    assert status(con, pia) == "paameldt" and utvalg_rader(con, pia) == []
    with pytest.raises(db.Paameldingsfeil, match="Bare en ekstradeltaker"):                           # bare en ekstradeltaker har utvalg
        db.sett_ekstradeltaker_samlinger(con, pia, [s1])
    with pytest.raises(db.Paameldingsfeil):
        db.sett_ekstradeltaker_samlinger(con, 999999, [s1])


def test_kurs_uten_samlingsrader_og_dager_uten_samling_er_bare_for_hele_kurset(con):
    kid = kurs_med_samlinger(con, "GAMMEL", samlinger=[S(date(2031, 10, 16), date(2031, 10, 17), "09:00", "16:00", 6.0)])
    con.execute("UPDATE kursdag SET samling_id=NULL WHERE kurs_id=?", (kid,))                          # eldre data uten samling
    con.commit()
    assert db.kursets_samlinger(con, kid) == []
    eva = ekstra(con, kid, "Eva")                                                                      # hele kurset går alltid
    assert dager_for(con, eva) == DAGER[1]
    with pytest.raises(db.Paameldingsfeil):
        db.sett_ekstradeltaker_samlinger(con, eva, [1])                                                # ingen samling å velge
    assert db.samlingsutvalg_tekst(con, eva) == "Hele kurset"
    # med to samlinger og en dag uten samling: den dagen er bare med for «hele kurset»
    kid2 = kurs_med_samlinger(con, "BLANDET")
    dag = db.kursdager(con, kid2)[-1]
    con.execute("UPDATE kursdag SET samling_id=NULL WHERE id=?", (dag["id"],))
    con.commit()
    pia = ekstra(con, kid2, "Pia", nr=[1])
    assert dager_for(con, pia) == DAGER[1]
    assert dag["dato"] in dager_for(con, ekstra(con, kid2, "Una"))                                     # hele kurset: dagen er med


def test_samling_uten_kursdager_kan_ikke_velges(con):
    kid = kurs_med_samlinger(con)
    tom = db.sett_inn(con, "INSERT INTO samling (kurs_id, navn, start_kl, slutt_kl, timer_pr_dag) VALUES (?,?,?,?,?)",
                      (kid, "Tom", "09:00", "16:00", 6))
    con.commit()
    assert tom not in samling_ider(con, kid)
    eva = ekstra(con, kid, "Eva", nr=[1])
    with pytest.raises(db.Paameldingsfeil):
        db.sett_ekstradeltaker_samlinger(con, eva, [tom])
    assert dager_for(con, eva) == DAGER[1]


def test_samling_n_er_kursets_eget_nummer_ogsaa_naar_bare_en_samling_vises(con):
    kid = kurs_med_samlinger(con)
    eva, nina = ekstra(con, kid, "Eva", nr=[3]), ekstra(con, kid, "Nina", nr=[2, 3])
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    grupper = kursdatoer.visning(db.paameldingens_kursdager(con, eva), kurs)
    assert [g["navn"] for g in grupper] == ["Samling 3"]                                              # ikke «Samling 1» eller uten navn
    assert [g["navn"] for g in kursdatoer.visning(db.paameldingens_kursdager(con, nina), kurs)] == ["Samling 2", "Samling 3"]
    hele = kursdatoer.visning(db.kursdager(con, kid), kurs)
    assert [g["navn"] for g in hele] == ["Samling 1", "Samling 2", "Samling 3"]                        # uendret for alle andre
    # samlinger med eget navn beholder navnet
    con.execute("UPDATE samling SET navn='Fordypning' WHERE id=?", (samling_ider(con, kid)[2],))
    con.commit()
    assert [g["navn"] for g in kursdatoer.visning(db.paameldingens_kursdager(con, eva), kurs)] == ["Fordypning"]
    assert db.samlingsutvalg_tekst(con, eva) == "Fordypning" and db.samlingsutvalg_tekst(con, nina) == "Samling 2, Fordypning"
    ett_kurs = kurs_med_samlinger(con, "EN", samlinger=[S(date(2031, 10, 16), None, "09:00", "16:00", 6.0)])
    pia = ekstra(con, ett_kurs, "Pia")
    assert kursdatoer.visning(db.paameldingens_kursdager(con, pia), con.execute("SELECT * FROM kurs WHERE id=?", (ett_kurs,)).fetchone())[0]["navn"] == ""


def test_samlingstekst():
    assert db.samlingstekst(["Samling 1", "Samling 3"]) == "Samling 1, 3"
    assert db.samlingstekst(["Samling 1", "Samling 2", "Samling 3"], og=True) == "Samling 1, 2 og 3"
    assert db.samlingstekst(["Samling 2"]) == "Samling 2" and db.samlingstekst([]) == ""
    assert db.samlingstekst(["Grunnkurs", "Samling 2"]) == "Grunnkurs, Samling 2"
    assert db.samlingstekst(["Grunnkurs", "Fordypning"], og=True) == "Grunnkurs og Fordypning"


# ============================ utvalget ryddes ============================

@pytest.mark.parametrize("til", ["paameldt", "venteliste", "avmeldt", "avslatt", "utgatt", "forlatt"])
def test_utvalget_ryddes_naar_personen_slutter_aa_vaere_ekstradeltaker_eller_forlater_plassen(con, til):
    kid = kurs_med_samlinger(con, kapasitet=5)
    eva = ekstra(con, kid, "Eva", nr=[1, 2])
    assert len(utvalg_rader(con, eva)) == 2
    sett(con, eva, til)
    assert utvalg_rader(con, eva) == [] and rad(con, eva)["ekstradeltaker_ts"] is None
    assert dager_for(con, eva) == sum(DAGER.values(), [])                                              # alle andre er på alle dager


def test_meld_av_og_avslag_rydder_utvalget_og_ny_paamelding_starter_uten(con):
    kid = kurs_med_samlinger(con, kapasitet=5)
    eva, una = ekstra(con, kid, "Eva", nr=[1]), ekstra(con, kid, "Una", nr=[2])
    db.meld_av(con, eva, aktor=ADMIN)
    db.avsla_paamelding(con, una, aktor=ADMIN)
    con.commit()
    assert utvalg_rader(con, eva) == [] and utvalg_rader(con, una) == []
    assert meld(con, kid, "Eva") == eva                                                                # melder seg på igjen selv
    assert status(con, eva) == "paameldt" and utvalg_rader(con, eva) == [] and rad(con, eva)["ekstradeltaker_ts"] is None


def test_rader_som_blir_hengende_paavirker_aldri_en_paamelding_som_ikke_er_ekstradeltaker(con):
    kid = kurs_med_samlinger(con)
    ola = meld(con, kid, "Ola")
    s1 = samling_ider(con, kid)[0]
    con.execute("INSERT INTO ekstradeltaker_samling (paamelding_id, samling_id) VALUES (?,?)", (ola, s1))   # feil tilstand
    con.commit()
    assert dager_for(con, ola) == sum(DAGER.values(), [])                                              # ekstradeltaker_ts styrer
    assert db.samlingsutvalg_tekst(con, ola) == "Hele kurset" and db.samlingsutvalg_for_kurs(con, kid) == {}


def test_hendelsene_har_bare_id_er_og_tall_aldri_navn(con):
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[1, 3])
    db.sett_ekstradeltaker_samlinger(con, eva, [samling_ider(con, kid)[1]], aktor=ADMIN)
    con.commit()
    hendelser = [(h["handling"], h["detaljer"]) for h in con.execute(
        "SELECT handling, detaljer FROM hendelse WHERE handling IN ('status_endret', 'ekstradeltaker_utvalg') ORDER BY id")]
    assert ("ekstradeltaker_utvalg", f'{{"paamelding_id": {eva}, "hele_kurset": false, "antall": 1}}') in hendelser
    tekst = " ".join(d for _, d in hendelser)
    assert "Eva" not in tekst and "eksempel.no" not in tekst
    from kurs import hendelseslogg
    rader = hendelseslogg.for_paamelding(con, rad(con, eva), systemadmin=False).rader
    assert [r.hva for r in rader][:2] == ["Samlingsutvalget endret", "Status endret fra påmeldt til ekstradeltaker"]
    assert rader[0].detaljer == ["Deltar på: 1 samling"] and "Deltar på: 2 samlinger" in rader[1].detaljer


# ============================ samlinger som endres eller slettes ============================

def _lagre(con, kid, samlinger):
    db.lagre_samlinger(con, kid, samlinger, aktor=ADMIN)
    con.commit()


def test_en_samling_som_ekstradeltakeren_bare_er_satt_opp_paa_kan_ikke_fjernes(con):
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[3])                                                              # bare samling 3
    lagrede = db.lagrede_samlinger(con, kid)
    uten_tre = [s for s in lagrede if s.fra != S3[0]]
    with pytest.raises(db.Kursdagfeil) as e:
        db.lagre_samlinger(con, kid, uten_tre, aktor=ADMIN)
    con.rollback()
    assert "1 ekstradeltaker er bare satt opp på samlinger som nå fjernes" in " ".join(e.value.meldinger)
    assert [d["dato"] for d in db.kursdager(con, kid)] == sum(DAGER.values(), []) and utvalg_rader(con, eva)     # ingenting endret
    assert dager_for(con, eva) == DAGER[3]                                                              # og ikke plutselig hele kurset
    db.sett_ekstradeltaker_samlinger(con, eva, [samling_ider(con, kid)[0], samling_ider(con, kid)[2]])   # utvalget endres først ...
    con.commit()
    _lagre(con, kid, uten_tre)                                                                          # ... så kan samlingen fjernes
    assert utvalg_rader(con, eva) == [samling_ider(con, kid)[0]] and dager_for(con, eva) == DAGER[1]   # samling 1 er igjen; 3 gikk med (CASCADE)


def test_fjernes_bare_noen_av_samlingene_i_utvalget_faar_de_resten_og_alle_igjen_blir_hele_kurset(con):
    kid = kurs_med_samlinger(con)
    s1, s2, s3 = samling_ider(con, kid)
    eva, nina = ekstra(con, kid, "Eva", nr=[1, 3]), ekstra(con, kid, "Nina", nr=[1, 2])
    lagrede = db.lagrede_samlinger(con, kid)
    _lagre(con, kid, [s for s in lagrede if s.fra != S3[0]])                                            # samling 3 fjernes
    assert utvalg_rader(con, eva) == [s1] and dager_for(con, eva) == DAGER[1]                          # samling 3 gikk med; {1} dekker ikke samling 2
    assert dager_for(con, nina) == DAGER[1] + DAGER[2]                                                  # {1, 2} = alle igjen = hele kurset
    assert utvalg_rader(con, nina) == []                                                                 # normalisert: ingen rader
    assert samling_ider(con, kid) == [s1, s2]


def test_ny_samling_etter_lagring_foelger_hele_kurset_men_ikke_et_utvalg(con):
    kid = kurs_med_samlinger(con)
    s1, s2, s3 = samling_ider(con, kid)
    hele, delvis = ekstra(con, kid, "Eva"), ekstra(con, kid, "Nina", nr=[1, 2])
    alle_valgt = ekstra(con, kid, "Una", nr=[1, 2, 3])                                                  # alle valgt lagres som hele kurset
    assert utvalg_rader(con, alle_valgt) == []
    ny = S(date(2032, 1, 15), date(2032, 1, 16), "09:00", "16:00", 6.0)
    _lagre(con, kid, [*db.lagrede_samlinger(con, kid), ny])
    assert dager_for(con, hele)[-2:] == ["2032-01-15", "2032-01-16"] and dager_for(con, alle_valgt)[-1] == "2032-01-16"
    assert dager_for(con, delvis) == DAGER[1] + DAGER[2]                                                # utvalget får ikke den nye samlingen
    assert [s["nr"] for s in db.kursets_samlinger(con, kid)] == [1, 2, 3, 4]


def test_kursdager_som_flyttes_mellom_samlinger_endrer_dagene_deltakeren_er_paa(con):
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[2])
    lagrede = db.lagrede_samlinger(con, kid)
    flyttet = [S(s.fra.replace(day=s.fra.day + 1), s.til.replace(day=s.til.day + 1) if s.til else None, s.start_kl, s.slutt_kl, s.timer,
                 navn=s.navn, id=s.id) if s.fra == S2[0] else s for s in lagrede]
    _lagre(con, kid, flyttet)
    assert dager_for(con, eva) == ["2031-11-14", "2031-11-15"]                                          # samme samling, nye datoer


def test_samling_uten_kursdager_etter_endring_gir_ingen_dager_og_ingen_feil(con):
    """Alle dagene i samlingen er tatt bort, men utvalget peker på en samling som fortsatt finnes uten dager: ingen dager, ingen krasj."""
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[3])
    dag = db.kursdager(con, kid)[-1]
    con.execute("DELETE FROM kursdag WHERE id=?", (dag["id"],))                                          # samlingen står igjen uten dager
    con.commit()
    assert dager_for(con, eva) == []
    assert db.kursets_samlinger(con, kid)[-1]["nr"] == 2
