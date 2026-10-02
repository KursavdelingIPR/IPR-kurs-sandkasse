"""Status «Ekstradeltaker» og «Påmeldt» som den vanlige statusen (migrering 16, paamelding.ekstradeltaker_ts).

Databaseverdien status='bekreftet' er «har plass på kurset»-verdien for BÅDE Påmeldt og Ekstradeltaker, og ekstradeltaker_ts skiller dem:
NULL = Påmeldt, satt = Ekstradeltaker. En ekstradeltaker er PÅ KURSET (utsendinger, påminnelser, innsjekk, kursside, kursbevis,
allergiliste) men er ikke en fullverdig deltaker: den TAR IKKE PLASS og teller ikke mot kapasiteten (db.antall_bekreftet teller bare
påmeldte), og den får ikke automatisk bekreftelse eller faktura (holdes tilbake). Administrator setter Ekstradeltaker manuelt; ingen
automatikk gjør det. Påmeldt -> Ekstradeltaker frigjør en plass (første på ventelisten rykker opp), Ekstradeltaker -> Påmeldt krever en
ledig plass, og fra/til alle andre statuser rykker ingen opp for en ekstradeltaker. Forlater noen plassen, nullstilles merkelappen.

Etikettene som vises overalt (liste, filter, vindu, rapporter, CSV, Logger) står i tests/test_status_etiketter.py, og
overgangene mellom alle syv statusene i tests/test_paameldingsstatus.py. Samlingsutvalget (hele kurset eller noen samlinger) og hva det
betyr for påminnelser, innsjekk, kursbevis m.m. står i tests/test_ekstradeltaker_samlinger.py og tests/test_ekstradeltaker_foelger.py;
faktura/bekreftelse, tellingen overalt og skjermbildene i tests/test_ekstradeltaker_faktura_ui.py. Her: migreringen, utledningen av
statusen, hver overgang, kapasitet og opprykk, dialogtekstene og roller/CSRF for statusvelgeren.
Bare oppdiktede data.
"""
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from kurs import config, daglig, db, deltakerside, deltakersok, hendelseslogg, kursbevis, migreringer, statustekster, sveiper
from kurs.kjoring import Kjoring
from kurssidehjelp import IDAG, admin_klient, deltaker_klient, dokument, fast_dato, lag_kurs, ny_database, skriv_side, tekstblokk

ADMIN = f"admin:{config.ADMIN_BRUKERNAVN}"
TS = "2031-10-01 10:00:00"
KOLONNER = ("status", "avslatt_ts", "utgatt_ts", "forlatt_ts", "ekstradeltaker_ts")


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    fast_dato(monkeypatch)
    yield c
    c.close()


def _kurs(con, kapasitet=10, kode="EKS1", start=date(2031, 10, 16), dager=1, **felt) -> int:
    datoer = [(start + timedelta(days=n)).isoformat() for n in range(dager)]
    kid = db.opprett_kurs(con, kode=kode, navn="Veiledning i gruppe", datoer=datoer, sharepoint_mappe=f"Kurs/{kode}",
                          pris_nok=2500, kapasitet=kapasitet, fakturering="person", **felt)
    con.commit()
    return kid


def _meld(con, kid, fornavn, **kw) -> int:
    pid, _ = db.meld_paa(con, kid, epost=f"{fornavn.lower()}@eksempel.no", fornavn=fornavn, etternavn="Test", **kw)
    con.commit()
    return pid


def _rad(con, pid):
    return con.execute("SELECT * FROM paamelding WHERE id=?", (pid,)).fetchone()


def _status(con, pid) -> str:
    return db.paameldingsstatus(_rad(con, pid))


def _sett(con, pid, status, **kw):
    """db.sett_paamelding_status som administrator (committer). Returnerer det funksjonen returnerer."""
    resultat = db.sett_paamelding_status(con, pid, status, aktor=ADMIN, **kw)
    con.commit()
    return resultat


def _ekstra(con, kid, fornavn) -> int:
    """Påmelding med plass som administrator har satt til Ekstradeltaker."""
    pid = _meld(con, kid, fornavn)
    _sett(con, pid, "ekstradeltaker")
    return pid


def _i_status(con, kid, fornavn, status) -> int:
    pid = _meld(con, kid, fornavn)
    if status != "paameldt":
        _sett(con, pid, status)
    return pid


def _eposter(con, fornavn) -> list[str]:
    return [r[0] for r in con.execute("SELECT type FROM utsending_logg WHERE mottaker=? ORDER BY sendt_ts",
                                      (f"{fornavn.lower()}@eksempel.no",))]


def _antall(con, sql, *args) -> int:
    return con.execute(sql, args).fetchone()[0]


# ============================ migrering 16 ============================

def test_ny_database_har_migrering_16_og_kolonnen_sist_i_tabellen(con):
    nr16 = [(n, f.__name__) for n, navn, f in migreringer.MIGRERINGER if n == 16]
    assert nr16 == [(16, "_m16_ekstradeltaker")] and migreringer.MIGRERINGER[15][1] == "ekstradeltaker"
    assert migreringer.gjeldende_versjon(con) >= 16 and migreringer.KODEVERSJON >= 16
    assert db.kolonner(con, "paamelding")[-1] == "ekstradeltaker_ts"
    for fil in ("schema.sql", "schema_postgres.sql"):
        tekst = (db._SCHEMA_POSTGRES if fil.endswith("postgres.sql") else db._SCHEMA).read_text(encoding="utf-8")
        assert migreringer._skjemaets_kolonner(tekst)["paamelding"][-1] == "ekstradeltaker_ts", fil
        assert "CHECK (ekstradeltaker_ts IS NULL OR status='bekreftet')" in tekst, fil
        assert "CHECK (status IN ('bekreftet','venteliste','avmeldt'))" in tekst, fil            # status er urørt
    assert con.execute("SELECT COUNT(*) FROM paamelding WHERE ekstradeltaker_ts IS NOT NULL").fetchone()[0] == 0


def test_migrering_16_paa_eksisterende_database_beholder_alle_rader_og_gjor_alle_paameldte(con):
    kid = _kurs(con, kapasitet=2)
    kari, nina, ola = (_meld(con, kid, n) for n in ("Kari", "Nina", "Ola"))               # Ola havner på venteliste
    _sett(con, nina, "avslatt")
    con.execute("UPDATE paamelding SET oppdatert='2031-01-01T10:00:00' WHERE id=?", (kari,))
    # gjør databasen lik en som ikke har kjørt migrering 16 ennå
    con.execute("ALTER TABLE paamelding DROP COLUMN ekstradeltaker_ts")
    con.execute("DELETE FROM schema_versjon WHERE versjon >= 16")
    con.commit()
    assert not db.har_kolonne(con, "paamelding", "ekstradeltaker_ts") and migreringer.gjeldende_versjon(con) == 15
    for_ = [tuple(r) for r in con.execute("SELECT * FROM paamelding ORDER BY id")]

    assert migreringer.kjor_manglende(con) == [n for n, _, _ in migreringer.MIGRERINGER if n >= 16]     # 16 og senere (åpningstid)
    assert db.kolonner(con, "paamelding")[-1] == "ekstradeltaker_ts"
    etter = [tuple(r) for r in con.execute("SELECT * FROM paamelding ORDER BY id")]
    assert [r[:-1] for r in etter] == for_ and all(r[-1] is None for r in etter)             # ingen rad er endret
    assert [_status(con, p) for p in (kari, nina, ola)] == ["paameldt", "avslatt", "paameldt"]   # alle som har plass er Påmeldt
    assert migreringer.kjor_manglende(con) == []                                             # kjøres ikke to ganger
    migreringer._m16_ekstradeltaker(con)                                                     # selve funksjonen er idempotent
    migreringer._m16_ekstradeltaker(con)
    assert db.kolonner(con, "paamelding").count("ekstradeltaker_ts") == 1
    con.commit()
    with pytest.raises(sqlite3.IntegrityError):                                              # CHECK gjelder også en migrert database
        con.execute("UPDATE paamelding SET ekstradeltaker_ts=? WHERE id=?", (TS, nina))
    con.rollback()                                                                           # PostgreSQL avbryter transaksjonen ved en feil
    migreringer.kontroller(con)                                                              # appen godtar databasen


def test_rollback_i_migrations_md_nullstiller_merkene_slik_at_forrige_kode_kan_forlate_plassen(con):
    """Forrige kode kjenner ikke ekstradeltaker_ts og nullstiller den ikke når noen forlater plassen (meld av, venteliste,
    avslag, utgått, forlatt). CHECK-regelen stopper da en påmelding som fortsatt er merket, og deltakeren får 500. Derfor må
    rollback-beskrivelsen for migrering 16 i MIGRATIONS.md si at merkene nullstilles først. Her kjøres SQL-en derfra."""
    tekst = (Path(__file__).resolve().parent.parent / "MIGRATIONS.md").read_text(encoding="utf-8")
    rad = next(linje for linje in tekst.splitlines() if linje.startswith("| 16 | `ekstradeltaker` |"))
    sql = re.search(r"`(UPDATE paamelding SET ekstradeltaker_ts=NULL[^`]*)`", rad)
    assert sql, "rollback-beskrivelsen for migrering 16 må ha SQL-en som nullstiller ekstradeltaker_ts"
    assert "først" in rad.split("| Eldre kode")[1] and "forrige kode" in rad                    # rekkefølgen står der: først nullstille
    kid = _kurs(con)
    eva, ola = _ekstra(con, kid, "Eva"), _meld(con, kid, "Ola")
    con.commit()
    forrige_kodes_update = "UPDATE paamelding SET status='avmeldt' WHERE id=?"                # rører ikke ekstradeltaker_ts
    with pytest.raises(sqlite3.IntegrityError):                                              # uten steget feiler forrige kode
        con.execute(forrige_kodes_update, (eva,))
    con.rollback()
    con.execute(sql.group(1).strip().rstrip(";"))
    con.commit()
    assert (_status(con, eva), _status(con, ola)) == ("paameldt", "paameldt")                # ingen mister plassen
    con.execute(forrige_kodes_update, (eva,))                                                 # nå går forrige kode gjennom
    con.commit()
    assert _status(con, eva) == "avmeldt" and _antall(con, "SELECT COUNT(*) FROM paamelding WHERE ekstradeltaker_ts IS NOT NULL") == 0


# ============================ utledet status ============================

@pytest.mark.parametrize("verdier, forventet", [
    (("bekreftet", None, None, None, None), "paameldt"),
    (("bekreftet", None, None, None, TS), "ekstradeltaker"),
    (("venteliste", None, None, None, None), "venteliste"),
    (("avmeldt", None, None, None, None), "avmeldt"),
    (("avmeldt", TS, None, None, None), "avslatt"),
    (("avmeldt", None, TS, None, None), "utgatt"),
    (("avmeldt", None, None, TS, None), "forlatt"),
])
def test_utledet_status_for_alle_kombinasjoner(verdier, forventet):
    rad = dict(zip(KOLONNER, verdier))
    assert db.paameldingsstatus(rad) == forventet
    assert forventet in db.PAAMELDINGSSTATUSER and (forventet in db.HAR_PLASS) == (verdier[0] == "bekreftet")


@pytest.mark.parametrize("rad, forventet", [
    ({"status": "bekreftet"}, "paameldt"),                                   # SELECT uten tidsstemplene: som før
    ({"status": "avmeldt", "utgatt_ts": TS}, "utgatt"),
    ({"status": "bekreftet", "avslatt_ts": None}, "paameldt"),
    ({"status": "venteliste"}, "venteliste"),
])
def test_utledningen_taaler_rader_uten_tidsstempelkolonnene(rad, forventet):
    assert db.paameldingsstatus(rad) == forventet


def test_utledningen_taaler_sqlite_rader_uten_kolonnen(con):
    kid = _kurs(con)
    pid = _ekstra(con, kid, "Eva")
    med = con.execute("SELECT status, ekstradeltaker_ts FROM paamelding WHERE id=?", (pid,)).fetchone()
    uten = con.execute("SELECT status FROM paamelding WHERE id=?", (pid,)).fetchone()               # IndexError: ingen kolonne
    assert db.paameldingsstatus(med) == "ekstradeltaker" and db.paameldingsstatus(uten) == "paameldt"


def test_sql_uttrykket_gir_samme_status_som_funksjonen_for_alle_gyldige_kombinasjoner(con):
    kid = _kurs(con, kapasitet=20)
    oppsett = {"paameldt": {}, "ekstradeltaker": {"ekstradeltaker_ts": TS}, "venteliste": {"status": "venteliste"},
               "avmeldt": {"status": "avmeldt"}, "avslatt": {"status": "avmeldt", "avslatt_ts": TS},
               "utgatt": {"status": "avmeldt", "utgatt_ts": TS}, "forlatt": {"status": "avmeldt", "forlatt_ts": TS}}
    pid = {}
    for navn, sett in oppsett.items():
        pid[navn] = _meld(con, kid, navn.capitalize())
        for kolonne, verdi in sett.items():
            con.execute(f"UPDATE paamelding SET {kolonne}=? WHERE id=?", (verdi, pid[navn]))
    con.commit()
    sql = f"SELECT p.id, {db.sql_visningsstatus('p')} AS visning FROM paamelding p WHERE p.kurs_id=?"
    fra_sql = {r["id"]: r["visning"] for r in con.execute(sql, (kid,))}
    assert fra_sql == {p: navn for navn, p in pid.items()}
    assert {p: _status(con, p) for p in pid.values()} == fra_sql
    assert list(oppsett) == list(db.PAAMELDINGSSTATUSER)                                     # alle syv, i rekkefølge


def test_databasen_tillater_ekstradeltaker_bare_paa_en_paamelding_med_plass(con):
    kid = _kurs(con)
    pid = _meld(con, kid, "Eva")

    def avvises(sql, *args):
        con.commit()                                       # PostgreSQL avbryter hele transaksjonen ved en feil: behold det som gikk
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(sql, args)
        con.rollback()

    for status in ("venteliste", "avmeldt"):
        con.execute("UPDATE paamelding SET status=?, ekstradeltaker_ts=NULL WHERE id=?", (status, pid))
        avvises("UPDATE paamelding SET ekstradeltaker_ts=? WHERE id=?", TS, pid)
    con.execute("UPDATE paamelding SET status='bekreftet', ekstradeltaker_ts=? WHERE id=?", (TS, pid))       # med plass: greit
    avvises("UPDATE paamelding SET status='avmeldt' WHERE id=?", pid)                          # forlate plassen uten å fjerne merket


# ============================ Påmeldt -> Ekstradeltaker: frigjør en plass ============================

def test_paameldt_til_ekstradeltaker_frigjor_plassen_og_foerste_paa_ventelisten_rykker_opp(con):
    kid = _kurs(con, kapasitet=1, start=date.today() + timedelta(days=30))
    ola, nina = _meld(con, kid, "Ola"), _meld(con, kid, "Nina")                              # Nina på venteliste
    daglig.kjor(Kjoring(con, idag=date.today()))                                             # bekreftelse, faktura, ventelistebeskjed
    con.commit()
    assert _eposter(con, "Ola") == ["bekreftelse"] and _eposter(con, "Nina") == ["venteliste"]
    faktura = _antall(con, "SELECT COUNT(*) FROM faktura")
    assert (_rad(con, ola)["sveiper_kjort"], _rad(con, ola)["sveiper_utsatt"]) == (1, 0)     # Ola er ferdig behandlet

    assert _sett(con, ola, "ekstradeltaker") == nina                                          # Nina rykker opp: skal kjøres gjennom sveiper
    rad = _rad(con, ola)
    assert (rad["status"], _status(con, ola)) == ("bekreftet", "ekstradeltaker") and rad["ekstradeltaker_ts"]
    assert (_status(con, nina), db.antall_bekreftet(con, kid), db.antall_ekstradeltakere(con, kid)) == ("paameldt", 1, 1)
    assert (rad["sveiper_kjort"], rad["sveiper_utsatt"]) == (1, 0)                            # allerede behandlet: ingenting endres
    assert _antall(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", ola) == 1          # ingen kreditering, ingen ny faktura
    sveiper.kjor(Kjoring(con, idag=date.today()), nina)
    con.commit()
    assert "bekreftelse" in _eposter(con, "Nina") and _eposter(con, "Ola") == ["bekreftelse"]   # Nina får plassen; Ola får ingenting mer
    daglig.kjor(Kjoring(con, idag=date.today()))                                              # morgenjobben gjør ingenting mer for Ola
    con.commit()
    assert _eposter(con, "Ola") == ["bekreftelse"] and _antall(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", ola) == 1
    assert _antall(con, "SELECT COUNT(*) FROM faktura") == faktura + 1                       # bare Ninas faktura er ny


def test_paameldt_til_ekstradeltaker_uten_venteliste_aapner_et_fullt_kurs_igjen(con):
    kid = _kurs(con, kapasitet=1)
    ola, nina = _meld(con, kid, "Ola"), _meld(con, kid, "Nina")
    _sett(con, nina, "avmeldt")                                                              # ingen på ventelisten, men kurset står som fullt
    assert con.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "full"
    assert _sett(con, ola, "ekstradeltaker") is None                                          # ingen å rykke opp
    assert con.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "aapen" and db.antall_bekreftet(con, kid) == 0
    ny = _meld(con, kid, "Pia")
    assert _status(con, ny) == "paameldt"                                                     # plassen var ledig, og Ola er på kurset i tillegg


def test_ekstradeltaker_til_paameldt_krever_en_ledig_plass_som_venteliste_til_paameldt(con):
    kid = _kurs(con, kapasitet=1)
    ola = _meld(con, kid, "Ola")
    eva = _meld(con, kid, "Eva")                                                              # venteliste
    _sett(con, eva, "ekstradeltaker")                                                         # ingen kapasitetssjekk for en ekstradeltaker
    with pytest.raises(db.Paameldingsfeil, match="Kurset er fullt"):
        db.sett_paamelding_status(con, eva, "paameldt", aktor=ADMIN)                          # men Påmeldt tar en plass, og den er tatt
    con.rollback()
    assert _status(con, eva) == "ekstradeltaker"
    assert _sett(con, eva, "paameldt", tillat_overbooking=True) == eva                        # overbooking: samme spørsmål som fra venteliste
    assert (_status(con, eva), db.antall_bekreftet(con, kid)) == ("paameldt", 2)
    lagret = con.execute("SELECT detaljer FROM hendelse WHERE handling='status_endret' ORDER BY id DESC").fetchone()[0]
    assert '"fra": "ekstradeltaker"' in lagret and '"til": "paameldt"' in lagret and '"over_kapasitet": 1' in lagret
    assert _status(con, ola) == "paameldt"


def test_ekstradeltaker_til_paameldt_paa_siste_ledige_plass_gjor_kurset_fullt(con):
    kid = _kurs(con, kapasitet=2)
    _meld(con, kid, "Ola")
    eva = _ekstra(con, kid, "Eva")
    assert con.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "aapen"
    assert _sett(con, eva, "paameldt") == eva and db.antall_bekreftet(con, kid) == 2          # siste plass er tatt
    assert con.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "full"
    assert _status(con, _meld(con, kid, "Nina")) == "venteliste"


@pytest.mark.parametrize("til", ["ekstradeltaker", "paameldt"])
def test_et_avlyst_eller_avsluttet_kurs_tar_ikke_imot_ekstradeltakere_eller_paameldte(con, til):
    kid = _kurs(con, kapasitet=5)
    ola = _meld(con, kid, "Ola")
    _sett(con, ola, "avmeldt")
    for kursstatus in ("avlyst", "avsluttet"):
        con.execute("UPDATE kurs SET status=? WHERE id=?", (kursstatus, kid))
        con.commit()
        with pytest.raises(db.Paameldingsfeil, match=f"Kurset er {kursstatus}"):
            db.sett_paamelding_status(con, ola, til, aktor=ADMIN)
        con.rollback()
        assert _status(con, ola) == "avmeldt"


# ============================ fra en status uten plass: på kurset uten å ta plass ============================

@pytest.mark.parametrize("fra", ["venteliste", "avmeldt", "avslatt", "utgatt", "forlatt"])
def test_fra_en_status_uten_plass_gir_ekstradeltaker_plass_paa_kurset_uten_kapasitetssjekk_og_uten_utsending(con, fra):
    idag = date.today()
    kid = _kurs(con, kapasitet=1, start=idag + timedelta(days=5))
    pia = _meld(con, kid, "Pia")                                                              # kurset er fullt
    nina = _meld(con, kid, "Nina")                                                            # venteliste: står i køen hele tiden
    pid = _i_status(con, kid, "Eva", fra)
    assert _sett(con, pid, "ekstradeltaker") is None                                          # tar ingen plass: ingenting å kjøre gjennom sveiper
    rad = _rad(con, pid)
    assert rad["status"] == "bekreftet" and rad["ekstradeltaker_ts"] and _status(con, pid) == "ekstradeltaker"
    assert [rad["avslatt_ts"], rad["utgatt_ts"], rad["forlatt_ts"]] == [None, None, None]
    assert (rad["sveiper_kjort"], rad["sveiper_utsatt"]) == (0, 1)                            # holdt tilbake: ingen automatisk bekreftelse eller faktura
    assert db.antall_bekreftet(con, kid) == 1 and _status(con, nina) == "venteliste"           # køen og plassene er urørt
    lagret = con.execute("SELECT detaljer FROM hendelse WHERE handling='status_endret' ORDER BY id DESC").fetchone()[0]
    assert "over_kapasitet" not in lagret                                                      # ikke en overbooking
    daglig.kjor(Kjoring(con, idag=idag))
    con.commit()
    assert "bekreftelse" in _eposter(con, "Pia") and "bekreftelse" not in _eposter(con, "Eva")
    assert _antall(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", pid) == 0
    assert _antall(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", pia) == 1


def test_ekstradeltaker_faar_paaminnelser_men_ikke_bekreftelse_og_faktura_automatisk(con):
    idag = date.today()
    kid = _kurs(con, start=idag + timedelta(days=5))
    pia, eva = _meld(con, kid, "Pia"), _ekstra(con, kid, "Eva")
    assert (_status(con, pia), _status(con, eva)) == ("paameldt", "ekstradeltaker")
    daglig.kjor(Kjoring(con, idag=idag))
    con.commit()
    assert sorted(_eposter(con, "Pia")) == ["bekreftelse", "ukefor"] and _eposter(con, "Eva") == ["ukefor"]     # påminnelsen kommer
    faktura = {p: _antall(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", p) for p in (pia, eva)}
    assert faktura == {pia: 1, eva: 0}
    assert _status(con, eva) == "ekstradeltaker"                                              # merkelappen røres ikke av morgenjobben
    etter = (_antall(con, "SELECT COUNT(*) FROM faktura"), _antall(con, "SELECT COUNT(*) FROM utsending_logg"))
    daglig.kjor(Kjoring(con, idag=idag))                                                      # idempotent også med ekstradeltakere
    con.commit()
    assert (_antall(con, "SELECT COUNT(*) FROM faktura"), _antall(con, "SELECT COUNT(*) FROM utsending_logg")) == etter


# ============================ kapasitet, opprykk og overbooking ============================

def test_ekstradeltaker_teller_ikke_mot_kapasiteten(con):
    kid = _kurs(con, kapasitet=2)
    _meld(con, kid, "Ola")
    eva = _ekstra(con, kid, "Eva")
    assert (db.antall_bekreftet(con, kid), db.antall_ekstradeltakere(con, kid), db.antall_med_plass(con, kid)) == (1, 1, 2)
    nina = _meld(con, kid, "Nina")                                                            # det er fortsatt en ledig plass
    assert _status(con, nina) == "paameldt" and con.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "aapen"
    pia = _meld(con, kid, "Pia")                                                              # nå er kurset fullt (to påmeldte)
    assert _status(con, pia) == "venteliste" and con.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "full"
    assert _sett(con, pia, "ekstradeltaker") is None                                          # aldri overbooking for en ekstradeltaker
    assert (db.antall_bekreftet(con, kid), _status(con, pia)) == (2, "ekstradeltaker")
    assert '"over_kapasitet"' not in con.execute("SELECT detaljer FROM hendelse WHERE handling='status_endret' ORDER BY id DESC").fetchone()[0]
    assert _status(con, eva) == "ekstradeltaker" and db.antall_med_plass(con, kid) == 4


def test_kapasitet_oekt_rykker_opp_fra_venteliste_uten_aa_telle_ekstradeltakere(con):
    kid = _kurs(con, kapasitet=1)
    _meld(con, kid, "Ola")
    _ekstra(con, kid, "Eva")
    nina = _meld(con, kid, "Nina")                                                            # Ola tar den ene plassen: Nina står på venteliste
    assert _status(con, nina) == "venteliste"
    assert db.endre_kapasitet(con, kid, 2) == [nina]                                          # 1 påmeldt på 2 plasser: Nina rykker opp
    con.commit()
    assert _status(con, nina) == "paameldt" and _rad(con, nina)["ekstradeltaker_ts"] is None
    assert db.endre_kapasitet(con, kid, 1) == [] and db.antall_bekreftet(con, kid) == 2       # senkes: ingen fjernes


@pytest.mark.parametrize("til", ["avmeldt", "avslatt", "utgatt", "forlatt", "venteliste"])
def test_ekstradeltaker_som_forlater_kurset_frigjoer_ingen_plass_og_ingen_rykker_opp(con, til):
    kid = _kurs(con, kapasitet=1)
    _meld(con, kid, "Ola")
    eva = _ekstra(con, kid, "Eva")
    nina = _meld(con, kid, "Nina")                                                            # venteliste: Ola har plassen
    assert _sett(con, eva, til) is None                                                       # Eva tok ingen plass: ingen opprykk
    assert _status(con, eva) == til and _rad(con, eva)["ekstradeltaker_ts"] is None            # merket er nullstilt
    assert _status(con, nina) == "venteliste" and db.antall_bekreftet(con, kid) == 1
    if til == "venteliste":                                                                    # Eva har ikke plass lenger: hun er i køen
        daglig.kjor(Kjoring(con, idag=date.today()))
        con.commit()
        assert _eposter(con, "Eva") == ["venteliste"]


def test_paameldt_som_forlater_plassen_rykker_fortsatt_opp_og_en_ekstradeltaker_foerst_i_koeen_blir_paameldt(con):
    kid = _kurs(con, kapasitet=1)
    ola = _meld(con, kid, "Ola")
    eva = _meld(con, kid, "Eva")                                                              # venteliste
    _sett(con, eva, "ekstradeltaker")                                                         # forlater køen og er på kurset
    nina = _meld(con, kid, "Nina")
    assert _sett(con, ola, "avmeldt") == nina                                                 # Nina er den første i køen (Eva er ikke lenger der)
    assert (_status(con, nina), _status(con, eva)) == ("paameldt", "ekstradeltaker")


def test_avslag_og_avmelding_nullstiller_ekstradeltaker(con):
    kid = _kurs(con, kapasitet=5)
    eva, una = _ekstra(con, kid, "Eva"), _ekstra(con, kid, "Una")
    db.avsla_paamelding(con, eva, aktor=ADMIN)
    db.meld_av(con, una, aktor=ADMIN)
    con.commit()
    assert [(_status(con, p), _rad(con, p)["ekstradeltaker_ts"]) for p in (eva, una)] == [("avslatt", None), ("avmeldt", None)]


@pytest.mark.parametrize("via", ["deltakeren", "admin"])
def test_kommer_ekstradeltakeren_tilbake_blir_paameldt(con, via):
    kid = _kurs(con, kapasitet=5)
    eva = _ekstra(con, kid, "Eva")
    _sett(con, eva, "avmeldt")
    if via == "deltakeren":                                                                   # melder seg på igjen selv
        assert _meld(con, kid, "Eva") == eva
    else:
        _sett(con, eva, "paameldt")
    assert (_status(con, eva), _rad(con, eva)["ekstradeltaker_ts"]) == ("paameldt", None)


def test_nye_paameldinger_fra_alle_veier_er_paameldt_bortsett_fra_administrators_avkryssing(con):
    """Offentlig påmelding (db.meld_paa), administrators manuelle registrering og opprykk: aldri Ekstradeltaker - unntatt når administrator
    uttrykkelig krysser av for det i «Legg til deltaker»."""
    from adressehjelp import ADRESSE
    kid = _kurs(con, kapasitet=1)
    ola = _meld(con, kid, "Ola")
    k = admin_klient()
    r = k.post(f"/admin/kurs/{kid}/deltaker/ny", follow_redirects=False, data={
        "fornavn": "Mia", "etternavn": "Test", "epost": "mia@eksempel.no", "betaler": "person", "betaling": "samlet", **ADRESSE})
    assert r.status_code == 302
    mia = con.execute("SELECT id FROM paamelding WHERE deltaker_id=(SELECT id FROM deltaker WHERE epost='mia@eksempel.no')").fetchone()[0]
    assert (_status(con, ola), _status(con, mia)) == ("paameldt", "venteliste")               # Mia havnet på ventelisten
    _sett(con, ola, "avmeldt")
    assert (_status(con, mia), _rad(con, mia)["ekstradeltaker_ts"]) == ("paameldt", None)
    assert _antall(con, "SELECT COUNT(*) FROM paamelding WHERE ekstradeltaker_ts IS NOT NULL") == 0


# ============================ ekstradeltaker er på kurset: utsendinger, innsjekk, kursside, kursbevis og allergiliste ============================

def test_ekstradeltaker_er_med_i_allergiliste_og_epostgruppen_for_paameldte(con):
    kid = _kurs(con, kapasitet=5)
    _meld(con, kid, "Pia", sensitivt={"allergier": "Nøtter", "tilrettelegging": None})
    eva = _meld(con, kid, "Eva", sensitivt={"allergier": "Skalldyr", "tilrettelegging": None})
    _sett(con, eva, "ekstradeltaker")
    k = admin_klient()
    allergi = k.get(f"/admin/kurs/{kid}/allergiliste").get_data(as_text=True)
    assert "Pia Test" in allergi and "Eva Test" in allergi and "Nøtter" in allergi and "Skalldyr" in allergi
    epost = k.get(f"/admin/kurs/{kid}/epost/ny?gruppe=bekreftet").get_data(as_text=True)
    assert "Pia Test" in epost and "Eva Test" in epost
    deltakere = k.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    assert "Send e-post til alle påmeldte" in deltakere                                        # teksten om «påmeldte» beholdes
    csv = k.get(f"/admin/kurs/{kid}/deltakere.csv").get_data(as_text=True)
    assert "Skalldyr" not in csv and "Nøtter" not in csv                                        # sensitivt er aldri med i CSV
    assert re.search(r"Eva;Test;eva@eksempel.no;;;Ekstradeltaker;", csv) and re.search(r"Pia;Test;pia@eksempel.no;;;Påmeldt;", csv)


def test_ekstradeltaker_kan_sjekke_inn_og_faar_kursbevis_som_paameldt(con):
    kid = _kurs(con, kapasitet=5, start=date(2031, 10, 16), dager=2)
    pia, eva = _meld(con, kid, "Pia"), _ekstra(con, kid, "Eva")
    dager = db.kursdager(con, kid)
    resultat = {n: deltakerside.registrer_qr(con, dager[0], date(2031, 10, 16), epost=f"{n.lower()}@eksempel.no")
                for n in ("Pia", "Eva")}
    assert resultat["Pia"].status == resultat["Eva"].status == "ny"                            # innsjekk virker likt
    for p in (pia, eva):
        db.registrer_oppmote(con, p, dager[1]["id"], "kode")
    con.commit()
    assert deltakerside.registrer_qr(con, dager[0], date(2031, 10, 16), epost="eva@eksempel.no").status == "allerede"
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()
    kursbevis.kjor(Kjoring(con, idag=date(2031, 10, 20)))
    con.commit()
    bevis = {r["deltaker_id"] for r in con.execute("SELECT deltaker_id FROM dokument WHERE type='kursbevis' AND kurs_id=?", (kid,))}
    assert bevis == {_rad(con, pia)["deltaker_id"], _rad(con, eva)["deltaker_id"]}
    assert _status(con, eva) == "ekstradeltaker"


def test_ekstradeltaker_ser_kurssiden_og_min_side_som_paameldt(con):
    kid = lag_kurs(con, "K1")
    eva = _meld(con, kid, "Eva")
    _sett(con, eva, "ekstradeltaker")
    did = _rad(con, eva)["deltaker_id"]
    skriv_side(con, kid, dokument(tekstblokk("Velkommen", "<p>Hei og velkommen til kurset</p>"), tittel="Kurssiden vår"))
    assert deltakerside.tilgang(con, did, "K1", IDAG).ok
    html = deltaker_klient(did).get("/min-side").get_data(as_text=True)
    assert '<span class="merke ok">Påmeldt</span>' in html                                     # deltakeren ser alltid «Påmeldt»
    assert "Ekstradeltaker" not in html                                                         # ...og kommer inn på kurssiden
    assert deltaker_klient(did).get("/kurs/K1/deltakerside").status_code == 200


def test_soket_viser_ekstradeltaker_men_kursbevisforklaringen_er_som_for_paameldt(con):
    kid = _kurs(con, kapasitet=5, start=date(2031, 10, 16))
    pia, eva = _meld(con, kid, "Pia"), _ekstra(con, kid, "Eva")
    idag = date(2031, 10, 1)
    forklaring, status = {}, {}
    for navn in ("Pia", "Eva"):
        person = deltakersok.sok(con, f"{navn.lower()}@eksempel.no", idag=idag).personer[0]
        p = person["paameldinger"][0]
        forklaring[navn], status[navn] = (p["kursbevis"], p["kursbevis_farge"], p["oppmote"]), p["status_navn"]
    assert status == {"Pia": "Påmeldt", "Eva": "Ekstradeltaker"}                                # søket viser statusnavnet
    assert forklaring["Pia"] == forklaring["Eva"]                                              # forklaringen bruker databaseverdien


# ============================ dialogtekstene ============================

def _tekst(gammel, ny, **kw):
    kurs = {"fakturering": "person", "pris_nok": 2500, "status": "aapen", "kapasitet": 10, **kw.pop("kurs", {})}
    return statustekster.dialogtekst("Eva Test", gammel, ny, kurs=kurs, **kw)


def test_dialogtekst_for_ekstradeltaker():
    n = db.PAAMELDINGSSTATUSER
    t = _tekst("paameldt", "ekstradeltaker")                                                   # frigjør en plass
    assert t.startswith(f"Endre status for Eva Test fra {n['paameldt']} til {n['ekstradeltaker']}?")
    assert "tar ikke plass og teller ikke som påmeldt" in t and "Plassen blir ledig" in t and "rykker den første opp" in t
    assert "Ekstradeltaker: ingen automatisk bekreftelse eller faktura. Send manuelt ved behov." in t
    assert "hvilke samlinger" not in t and "hvilke samlinger" in _tekst("paameldt", "ekstradeltaker", flere_samlinger=True)
    assert "Kurset er overbooket (3 påmeldte på 2 plasser)" in _tekst("paameldt", "ekstradeltaker", antall_paameldt=3, kurs={"kapasitet": 2})
    t = _tekst("ekstradeltaker", "paameldt")                                                   # tar en plass: krever en ledig
    assert t.startswith(f"Endre status for Eva Test fra {n['ekstradeltaker']} til {n['paameldt']}?")
    assert "krever en ledig plass" in t and "Samlingsutvalget fjernes" in t
    # Hva som sendes avhenger av om bekreftelsen er behandlet (se db.sett_paamelding_status): aldri to ganger, og aldri en faktura av seg selv
    assert "Bekreftelsen er ikke sendt ennå. Den sendes nå automatisk" in t and "sammen med fakturaen til kursets vanlige pris" in t
    assert "Har du allerede fakturert deltakeren selv, bør du ikke gjøre dette" in t
    t = _tekst("ekstradeltaker", "paameldt", behandlet=True, fakturert=True)                     # alt er behandlet: ingenting endres
    assert "Bekreftelsen er allerede behandlet, så ingenting sendes på nytt" in t and "endres ikke og krediteres ikke" in t
    assert "holdes tilbake" not in t
    t = _tekst("ekstradeltaker", "paameldt", behandlet=True)                                   # sendt manuelt, ingen faktura i systemet
    assert "Bekreftelsen er allerede sendt og sendes ikke på nytt" in t and "lager ingen av seg selv" in t
    assert "Behandle fakturering nå" in t and "skal du ikke trykke" in t and "alle forfalte delfakturaer" in t
    assert "holdes tilbake" in t
    t = _tekst("ekstradeltaker", "paameldt", behandlet=True, kurs={"fakturering": "ingen"})     # kurset faktureres ikke: ingenting å lage
    assert "så ingenting sendes på nytt" in t and "holdes tilbake" not in t
    for gammel in ("venteliste", "avmeldt", "avslatt", "utgatt", "forlatt"):                    # på kurset uten plass, også på et fullt kurs
        t = _tekst(gammel, "ekstradeltaker")
        assert "tar ikke plass og teller ikke som påmeldt – heller ikke når kurset er fullt" in t, gammel
        assert "Plassen blir ledig" not in t and "Ekstradeltaker: ingen automatisk bekreftelse eller faktura" in t, gammel
    assert "Deltakeren tas av ventelisten." in _tekst("venteliste", "ekstradeltaker")
    t = _tekst("ekstradeltaker", "avmeldt")                                                    # forlater kurset: ingen plass å frigjøre
    assert "tok ingen plass, så ingen rykker opp" in t and "Plassen blir ledig" not in t and "ingen e-post" in t
    assert f"blir statusen {n['paameldt']}" in t                                               # kommer tilbake som Påmeldt
    assert "blir statusen" not in _tekst("paameldt", "avmeldt")
    assert "krediteres ikke automatisk" in _tekst("ekstradeltaker", "avmeldt", fakturert=True)
    assert "krediteres ikke automatisk" in _tekst("paameldt", "avmeldt", fakturert=True)
    t = _tekst("ekstradeltaker", "venteliste")
    assert "ingen rykker opp" in t and "ventelistebeskjed" in t and "Plassen blir ledig" not in t
    assert "Avslå påmeldingen" in _tekst("ekstradeltaker", "avslatt")
    for gammel, ny in (("avmeldt", "ekstradeltaker"), ("avmeldt", "paameldt"), ("venteliste", "ekstradeltaker")):
        assert f"kan ikke settes til {n[ny].lower()}" in _tekst(gammel, ny, kurs={"status": "avlyst"}), (gammel, ny)    # ny person på kurset
    for kursstatus in ("avlyst", "avsluttet"):                                                # Påmeldt <-> Ekstradeltaker: bare merkelappen
        for gammel, ny in (("paameldt", "ekstradeltaker"), ("ekstradeltaker", "paameldt")):
            t = _tekst(gammel, ny, kurs={"status": kursstatus})
            assert "bare merkelappen endres" in t and "kan ikke settes" not in t and f"Kurset er {kursstatus}" in t, (kursstatus, gammel, ny)
            assert "Plassen blir ledig" not in t and "sendes nå automatisk" not in t and "krever en ledig plass" not in t
    assert statustekster.fullt_kurs_tekst("Eva Test", 2, 2).startswith("Er du sikker på at du vil melde Eva Test på?")


# ============================ deltakerlisten: statusvelger, roller og CSRF ============================

def _liste(k, kid):
    return k.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)


def _post(k, kid, pid, status, **ekstra):
    return k.post(f"/admin/kurs/{kid}/deltaker/{pid}/status", data={"status": status, **ekstra})


def test_statusvelgeren_i_listen_har_overbooking_bare_for_den_som_faar_plass_som_paameldt(con):
    kid = _kurs(con, kapasitet=2)
    pia, ola = _meld(con, kid, "Pia"), _meld(con, kid, "Ola")
    eva = _meld(con, kid, "Eva")                                                               # venteliste, kurset er fullt
    _sett(con, eva, "ekstradeltaker")
    nina = _meld(con, kid, "Nina")                                                             # venteliste
    html = _liste(admin_klient(), kid)
    rad = lambda pid: re.search(rf'<form class="radstatus"[^>]*action="[^"]*/deltaker/{pid}/status"[^>]*>', html).group(0)  # noqa: E731
    assert "data-full-bekreft" not in rad(pia)                                                 # Påmeldt har plass fra før
    for pid in (eva, nina):                                                                    # Påmeldt ville tatt en plass, og kurset er fullt
        assert "data-full-bekreft" in rad(pid) and "Kurset er fullt (2 av 2 plasser er tatt)" in rad(pid)
    assert "data-ekstra-steg" not in html                                                      # bare én samling: ingenting å velge
    assert 'data-naa="ekstradeltaker"' in html and 'data-naa="paameldt"' in html
    assert 'value="ekstradeltaker" selected>Ekstradeltaker</option>' in html
    assert ola and 'value="ekstradeltaker" data-bekreft="Endre status for Pia Test fra Påmeldt til Ekstradeltaker?' in html
    js = (Path(__file__).resolve().parent.parent / "kurs" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'valg.value === "paameldt"' in js and 'valg.value === "paameldt" || valg.value === "ekstradeltaker"' not in js
    assert 'data-ekstra-steg' in js                                                            # samlingsvalg-steget hoppes over i dialogen


def test_statusruten_frigjor_plassen_med_melding_og_ekstradeltaker_gir_ingen_kapasitetsmelding(con):
    kid = _kurs(con, kapasitet=1)
    ola = _meld(con, kid, "Ola")
    nina = _meld(con, kid, "Nina")                                                             # fullt kurs
    k = admin_klient()
    r = _post(k, kid, ola, "ekstradeltaker", neste=f"/admin/kurs/{kid}/deltakere", forventet="paameldt")
    assert r.status_code == 302 and _status(con, ola) == "ekstradeltaker" and _status(con, nina) == "paameldt"
    html = k.get(r.headers["Location"]).get_data(as_text=True)
    assert "Ola Test: Status endret til «ekstradeltaker». Deltar på hele kurset." in html
    assert "Ingen bekreftelse eller faktura sendes automatisk" in html and "Kurset har nå" not in html
    assert "Første på ventelisten har rykket opp og fått plassen." in html                       # plassen ble ledig
    r = _post(k, kid, ola, "avmeldt", forventet="ekstradeltaker")                               # forlater kurset: ingen plass å frigjøre
    assert _status(con, ola) == "avmeldt"
    html = k.get(r.headers["Location"]).get_data(as_text=True)
    assert "Status endret til «avmeldt»." in html and "Første på ventelisten har rykket opp" not in html


def test_forventet_status_ekstradeltaker_beskytter_mot_en_foreldet_liste(con):
    kid = _kurs(con)
    eva = _ekstra(con, kid, "Eva")
    k = admin_klient()
    _post(k, kid, eva, "avmeldt", neste=f"/admin/kurs/{kid}/deltakere", forventet="paameldt")   # listen trodde deltakeren var Påmeldt
    assert _status(con, eva) == "ekstradeltaker"                                                # ingenting endret
    _post(k, kid, eva, "avmeldt", neste=f"/admin/kurs/{kid}/deltakere", forventet="ekstradeltaker")
    assert _status(con, eva) == "avmeldt"


def test_ekstradeltaker_som_valg_avvises_for_lesetilgang_uten_csrf_og_uten_innlogging(con):
    kid = _kurs(con)
    pia = _meld(con, kid, "Pia")
    lese = admin_klient(con, "lese")
    assert _post(lese, kid, pia, "ekstradeltaker").status_code == 403 and _status(con, pia) == "paameldt"
    assert 'class="statusvelger' not in _liste(lese, kid)                                        # lesetilgang ser ingen velger
    assert '<span class="merke ok">Påmeldt</span>' in _liste(lese, kid)
    k = admin_klient()
    k.injiser_csrf = False
    assert _post(k, kid, pia, "ekstradeltaker").status_code == 400 and _status(con, pia) == "paameldt"
    assert _post(k, kid, pia, "ekstradeltaker", csrf_token="feil-token").status_code == 400
    ukjent = _post(deltaker_klient(), kid, pia, "ekstradeltaker")                                # ikke innlogget
    assert ukjent.status_code in (302, 401, 403) and _status(con, pia) == "paameldt"
    kursadmin = admin_klient(con, "kursadmin")
    assert _post(kursadmin, kid, pia, "ekstradeltaker").status_code == 302 and _status(con, pia) == "ekstradeltaker"
    assert admin_klient(con, "lese", "lese2").get(f"/admin/kurs/{kid}/deltaker/{pia}").status_code == 200   # les er tillatt


def _meldinger(k) -> list[str]:
    """Meldingene (flash) klienten har fått og ikke sett ennå. Tømmer dem."""
    with k.session_transaction() as s:
        return [tekst for _kategori, tekst in s.pop("_flashes", [])]


def test_deltakervinduet_har_overbooking_sporsmaal_bare_for_den_som_faar_plass_som_paameldt(con):
    """Ekstradeltaker tar ingen plass, så «Ekstradeltaker» er aldri en overbooking. Men fra Ekstradeltaker til Påmeldt trengs en ledig
    plass: da spør vinduet (som for den som står på venteliste). Påmeldt har plass fra før og får ikke spørsmålet."""
    kid = _kurs(con, kapasitet=2)
    pia, ola = _meld(con, kid, "Pia"), _meld(con, kid, "Ola")
    eva = _meld(con, kid, "Eva")                                                               # venteliste, kurset er fullt
    _sett(con, eva, "ekstradeltaker")
    nina = _meld(con, kid, "Nina")                                                             # venteliste, kurset er fullt
    k = admin_klient()
    html = k.get(f"/admin/kurs/{kid}/deltaker/{pia}").get_data(as_text=True)
    assert '<form class="statusskjema"' in html and '<option value="ekstradeltaker">' in html   # velgeren finnes
    assert "data-full-bekreft" not in html and 'name="overbooking"' not in html and "Kurset er fullt" not in html
    assert ola
    for pid in (eva, nina):
        html = k.get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
        assert "data-full-bekreft" in html and 'name="overbooking"' in html
        assert "Kurset er fullt (2 av 2 plasser er tatt)" in html


def test_fakturert_paamelding_faar_ikke_kreditt_melding_naar_den_blir_ekstradeltaker_men_faar_beskjed_om_at_fakturaen_staar(con):
    idag = date.today()
    kid = _kurs(con, start=idag + timedelta(days=5))
    pia = _meld(con, kid, "Pia")
    daglig.kjor(Kjoring(con, idag=idag))                                                      # faktura til Pia
    con.commit()
    assert _antall(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", pia) == 1
    kreditt = "Deltakeren er allerede fakturert. Fakturaen krediteres ikke automatisk"
    ekstra_kreditt = "Deltakeren er allerede fakturert. Fakturaen endres ikke og krediteres ikke automatisk"
    k = admin_klient()
    _meldinger(k)
    for ny in ("ekstradeltaker", "paameldt", "ekstradeltaker"):
        assert _post(k, kid, pia, ny).status_code == 302 and _status(con, pia) == ny
        meldinger = _meldinger(k)
        assert meldinger[0].startswith(f"Status endret til «{db.PAAMELDINGSSTATUSER[ny].lower()}»."), (ny, meldinger)
        assert any(m.startswith(ekstra_kreditt) for m in meldinger) == (ny == "ekstradeltaker"), (ny, meldinger)
        assert not any(m.startswith(kreditt) for m in meldinger), (ny, meldinger)             # ingenting krediteres: bare en merkelapp
    assert _antall(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", pia) == 1        # og aldri en ny faktura
    assert _post(k, kid, pia, "avmeldt").status_code == 302 and _status(con, pia) == "avmeldt"   # forlater kurset: melding som før
    assert any(m.startswith(kreditt) for m in _meldinger(k))
    eva = _ekstra(con, kid, "Eva")                                                             # Ekstradeltaker: får ingen faktura automatisk
    daglig.kjor(Kjoring(con, idag=idag))
    con.commit()
    assert _antall(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", eva) == 0


def test_ekstradeltaker_i_deltakerlistens_utskrift_og_filter(con):
    """Utskriften åpner med alle som har plass; brikken og filteret «Ekstradeltaker» viser bare dem."""
    kid = _kurs(con)
    _meld(con, kid, "Pia")
    _ekstra(con, kid, "Eva")
    k = admin_klient()
    standard = k.get(f"/admin/kurs/{kid}/deltakerliste?valgt=1&kol=status").get_data(as_text=True)
    assert "Pia Test" in standard and "Eva Test" in standard
    bare = k.get(f"/admin/kurs/{kid}/deltakerliste?valgt=1&kol=status&status=ekstradeltaker").get_data(as_text=True)
    assert "Eva Test" in bare and "Pia Test" not in bare
    filtrert = k.get(f"/admin/kurs/{kid}/deltakere?status=ekstradeltaker").get_data(as_text=True)
    assert re.search(r'<tr data-filtrer="deltaker" data-status="ekstradeltaker"[^>]*id="rad-\d+">', filtrert)
    assert re.search(r'<tr data-filtrer="deltaker" data-status="paameldt"[^>]* hidden[^>]*id="rad-\d+">', filtrert)   # skjult i filteret
