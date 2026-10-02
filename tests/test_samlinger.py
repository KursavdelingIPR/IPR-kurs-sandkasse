"""«Opprett kurs» med samlinger (migrering 9): kursdatoer.py, db.opprett_kurs/lagre_samlinger, migreringen av gamle
kurs, skjemaene «Opprett kurs» og Oppsett, forespørsel om presentasjon, og visningen av datoer og intern kommentar.

Kravene (planen godkjent 27.09.2026):
  * en samling er én eller flere sammenhengende kursdager med felles klokkeslett og timer; en dag kan avvike eller tas ut
  * hver dato er fortsatt en kursdag (QR, oppmøte, påminnelser, kursbevis) - og en kursdag som beholdes ved redigering,
    beholder id og innsjekkode
  * en kursdag med oppmøte eller faktura kan ikke fjernes, og admin får en tydelig forklaring
  * påmeldingsfristen foreslås en kalendermåned før første kursdag og følger første kursdag til admin setter den selv -
    en passert frist åpner aldri påmeldingen igjen av seg selv
  * ansvarlig er den innloggede brukeren ved opprettelse; fakturering per deltaker som standard
  * sted er påkrevd for fysisk og hybrid; online har ikke sted
  * intern kommentar (kurs.notat) vises bare for administratorer
  * migreringen er konservativ: ingen kursdag endres, og kurs som kan ha delt faktura får én samling per kursdag
"""
import re
import sqlite3
from datetime import date, timedelta

import pytest
from werkzeug.datastructures import MultiDict

from kurs import config, db, kursbevis, kursdatoer, migreringer, migrer
from kurs.integrasjoner import epost
from kurs.kjoring import Kjoring
from kurs.kursdatoer import Dagavvik, Samling
from kurs.sveiper import PLAN_NA, FakturaPlan

IDAG = date.today()


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    return c


def _fersk(con):
    return db.koble(config.DB_STI)


def _klient(logg_inn=True):
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    if logg_inn:
        k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def _admin_id(con):
    return con.execute("SELECT id FROM admin_bruker WHERE brukernavn=?", (config.ADMIN_BRUKERNAVN,)).fetchone()[0]


def _d(n: int) -> date:
    return IDAG + timedelta(days=n)


def _s(fra, til=None, start="09:00", slutt="16:00", timer=6.0, **kw) -> Samling:
    return Samling(fra=fra, til=til, start_kl=start, slutt_kl=slutt, timer=timer, **kw)


def _dager(con, kid):
    return con.execute("SELECT * FROM kursdag WHERE kurs_id=? ORDER BY dato", (kid,)).fetchall()


def _samlinger(con, kid):
    return con.execute("SELECT * FROM samling WHERE kurs_id=? ORDER BY id", (kid,)).fetchall()


def _kurs(con, samlinger, kode="T1", **felter):
    felter.setdefault("sted", "Bergen")
    felter.setdefault("type", "fysisk")
    kid = db.opprett_kurs(con, kode=kode, navn="Testkurs", datoer=[], samlinger=samlinger, idag=IDAG,
                          sharepoint_mappe=f"Kurs/{kode}", **felter)
    con.commit()
    return kid


def _paamelding(con, kid, n=1):
    pid, _ = db.meld_paa(con, kid, epost=f"deltaker{n}@example.no", fornavn="Test", etternavn=f"Deltaker{n}")
    con.commit()
    return pid


# ============================ kursdatoer: skjemaet ============================

def test_fra_skjema_leser_samlinger_i_radrekkefolge_og_hopper_over_tomme_rader():
    skjema = MultiDict({
        "s-2-navn": "Samling 2", "s-2-fra": "2027-10-18", "s-2-til": "2027-10-19", "s-2-start": "09:00",
        "s-2-slutt": "16:00", "s-2-timer": "6", "s-2-id": "",
        "s-0-navn": "", "s-0-fra": "2027-09-20", "s-0-til": "", "s-0-start": "10:00", "s-0-slutt": "15:30",
        "s-0-timer": "5,5", "s-0-id": "7",
        "s-1-navn": "", "s-1-fra": "", "s-1-til": "", "s-1-start": "09:00", "s-1-slutt": "16:00", "s-1-timer": "6",
        "s-2-d-2027-10-19-merknad": "Veiledningsdag", "s-2-d-2027-10-19-slutt": "12:00", "s-2-d-2027-10-19-timer": "3",
        "s-2-d-2027-10-18-fjern": "",
    })
    samlinger, feil = kursdatoer.fra_skjema(skjema)
    assert feil == []
    assert [(s.fra, s.til, s.start_kl, s.slutt_kl, s.timer, s.navn, s.id) for s in samlinger] == [
        (date(2027, 9, 20), None, "10:00", "15:30", 5.5, None, 7),
        (date(2027, 10, 18), date(2027, 10, 19), "09:00", "16:00", 6.0, "Samling 2", None)]
    avvik = samlinger[1].avvik[date(2027, 10, 19)]
    assert (avvik.slutt_kl, avvik.timer, avvik.merknad, avvik.fjernet) == ("12:00", 3.0, "Veiledningsdag", False)
    assert samlinger[1].avvik[date(2027, 10, 18)].fjernet is False
    assert samlinger[0].datoer() == [date(2027, 9, 20)]                        # tom til-dato = én dag


def test_fra_skjema_gir_norske_feil_for_ugyldige_verdier():
    _, feil = kursdatoer.fra_skjema(MultiDict({"s-0-fra": "20.09.2027", "s-0-start": "09:00", "s-0-slutt": "16:00"}))
    assert feil == ["Samling 1: datoene må være gyldige."]
    _, feil = kursdatoer.fra_skjema(MultiDict({"s-0-navn": "Modul A", "s-0-fra": "2027-09-20", "s-0-timer": "seks"}))
    assert feil == ["Modul A: timer per dag må være et tall."]


@pytest.mark.parametrize("samlinger,forventet", [
    ([], "Legg inn minst én kursdag"),
    ([_s(None)], "fyll inn fra-dato"),
    ([_s(date(2027, 9, 20), date(2027, 9, 19))], "til-datoen er før fra-datoen"),
    ([_s(date(2027, 9, 1), date(2027, 10, 5))], "høyst 31 dager"),
    ([_s(date(2027, 9, 20), start="16:00", slutt="09:00")], "«Til kl.» må være etter «Fra kl.»"),
    ([_s(date(2027, 9, 20), start="9", slutt="16:00")], "gyldige klokkeslett"),
    ([_s(date(2027, 9, 20), timer=None)], "timer per dag må være et tall mellom 0 og 24"),
    ([_s(date(2027, 9, 20), timer=25)], "timer per dag må være et tall mellom 0 og 24"),
    ([_s(date(2027, 9, 20), date(2027, 9, 22)), _s(date(2027, 9, 22))], "22.09.2027 er med i både Samling 1 og Samling 2"),
    ([_s(date(2027, 9, 20), avvik={date(2027, 9, 20): Dagavvik(fjernet=True)})], "alle dagene er tatt ut"),
    ([_s(date(2027, 9, 20), date(2027, 9, 21), avvik={date(2027, 9, 21): Dagavvik(slutt_kl="08:00")})],
     "Samling 1, 21.09.2027: fyll inn gyldige klokkeslett"),
    ([_s(date(2027, 9, 20), avvik={date(2027, 9, 20): Dagavvik(timer=30)})], "timer må være et tall mellom 0 og 24"),
])
def test_kontroller_forklarer_hva_som_er_galt(samlinger, forventet):
    feil = kursdatoer.kontroller(samlinger)
    assert any(forventet in f for f in feil), feil


def test_kontroller_godtar_gyldige_samlinger_og_ignorerer_avvik_utenfor_datoene():
    samlinger = [_s(date(2027, 9, 20), date(2027, 9, 23), avvik={date(2027, 9, 30): Dagavvik(slutt_kl="01:00"),
                                                                   date(2027, 9, 21): Dagavvik(fjernet=True)}),
                 _s(date(2027, 10, 18), timer=0)]
    assert kursdatoer.kontroller(samlinger) == []
    assert samlinger[0].datoer() == [date(2027, 9, 20), date(2027, 9, 22), date(2027, 9, 23)]


# ============================ kursdatoer: frist, tekster og visning ============================

@pytest.mark.parametrize("forste,idag,forventet", [
    (date(2027, 10, 15), date(2027, 1, 1), date(2027, 9, 15)),       # én kalendermåned før
    (date(2027, 3, 31), date(2027, 1, 1), date(2027, 2, 28)),        # finnes ikke dagen, brukes siste dag
    (date(2028, 3, 31), date(2027, 1, 1), date(2028, 2, 29)),        # skuddår
    (date(2027, 1, 15), date(2026, 1, 1), date(2026, 12, 15)),       # over nyttår
    (date(2027, 10, 15), date(2027, 10, 1), date(2027, 10, 14)),     # måneden er passert: dagen før første kursdag
    (date(2027, 10, 15), date(2027, 10, 15), date(2027, 10, 15)),    # aldri før i dag
    (date(2027, 10, 15), date(2027, 9, 15), date(2027, 9, 15)),      # akkurat en måned igjen
])
def test_foreslaatt_frist(forste, idag, forventet):
    assert kursdatoer.foreslaatt_frist(forste, idag) == forventet


@pytest.mark.parametrize("fra,til,forventet", [
    (date(2027, 10, 15), date(2027, 10, 15), "15.10.2027"),
    (date(2027, 9, 20), date(2027, 9, 23), "20.–23.09.2027"),
    (date(2027, 9, 30), date(2027, 10, 2), "30.09.–02.10.2027"),
    (date(2027, 12, 30), date(2028, 1, 2), "30.12.2027–02.01.2028"),
])
def test_periode(fra, til, forventet):
    assert kursdatoer.periode(fra, til) == forventet


def test_datoliste_viser_hull():
    assert kursdatoer.datoliste([date(2027, 9, 24), date(2027, 9, 20), date(2027, 9, 21)]) == "20.–21.09.2027 og 24.09.2027"
    assert kursdatoer.datoliste([date(2027, 9, 20), date(2027, 9, 22), date(2027, 9, 24)]) == \
        "20.09.2027, 22.09.2027 og 24.09.2027"


def test_oppsummering_teller_samlinger_dager_og_timer_med_avvik():
    samlinger = [_s(date(2027, 9, 20), date(2027, 9, 23), avvik={date(2027, 9, 23): Dagavvik(timer=3),
                                                                   date(2027, 9, 22): Dagavvik(fjernet=True)}),
                 _s(date(2028, 5, 10), timer=7.5)]
    assert kursdatoer.oppsummering(samlinger) == "2 samlinger · 4 kursdager · 20.09.2027–10.05.2028 · 22,5 timer"
    assert kursdatoer.oppsummering([_s(date(2027, 9, 20), timer=1)]) == "1 samling · 1 kursdag · 20.09.2027 · 1 time"
    assert kursdatoer.oppsummering([_s(None)]) == ""


def test_visning_grupperer_per_samling_med_navn_og_avvik(con):
    kid = _kurs(con, [
        _s(_d(40), _d(43), navn=None, avvik={_d(43): Dagavvik(slutt_kl="12:00", merknad="Veiledningsdag")}),
        _s(_d(70), _d(71), start="10:00", slutt="15:00", navn="Fordypning"),
        _s(_d(100), navn=None)])
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    g = kursdatoer.visning(db.kursdager(con, kid), kurs)
    assert [x["navn"] for x in g] == ["Samling 1", "Fordypning", "Samling 3"]
    assert [x["tid"] for x in g] == ["09:00–16:00", "10:00–15:00", "09:00–16:00"]
    assert g[0]["periode"] == kursdatoer.periode(_d(40), _d(43)) and g[0]["antall_dager"] == 4
    assert g[0]["avvik"] == [{"dato": f"{kursdatoer.ukedag(_d(43))} {kursdatoer.kort_dato(_d(43))}",
                              "tid": "09:00–12:00", "merknad": "Veiledningsdag"}]
    assert g[1]["avvik"] == [] and g[2]["avvik"] == []


def test_visning_av_en_enkelt_samling_har_ikke_navn_og_dager_uten_samling_vises_enkeltvis():
    kurs = {"start_kl": "09:00", "slutt_kl": "15:00"}
    dager = [{"dato": "2027-03-01", "start_kl": None, "slutt_kl": None},
             {"dato": "2027-03-02", "start_kl": "10:00", "slutt_kl": "12:30"}]
    g = kursdatoer.visning(dager, kurs)
    assert [(x["navn"], x["periode"], x["tid"]) for x in g] == [("", "01.03.2027", "09:00–15:00"),
                                                                 ("", "02.03.2027", "10:00–12:30")]


# ============================ db: opprette og lagre ============================

def test_opprett_kurs_med_samlinger_lager_samling_og_kursdager_med_klokkeslett(con):
    kid = _kurs(con, [_s(_d(40), _d(42), start="10:00", slutt="15:00", timer=5, navn="Samling 1",
                         avvik={_d(42): Dagavvik(slutt_kl="13:00", timer=3, merknad="Veiledningsdag")}),
                      _s(_d(70))])
    s = _samlinger(con, kid)
    assert [(r["navn"], r["start_kl"], r["slutt_kl"], r["timer_pr_dag"]) for r in s] == [
        ("Samling 1", "10:00", "15:00", 5.0), (None, "09:00", "16:00", 6.0)]
    dager = _dager(con, kid)
    assert [(r["dato"], r["samling_id"], r["start_kl"], r["slutt_kl"], r["timer"], r["merknad"]) for r in dager] == [
        (_d(40).isoformat(), s[0]["id"], "10:00", "15:00", None, None),
        (_d(41).isoformat(), s[0]["id"], "10:00", "15:00", None, None),
        (_d(42).isoformat(), s[0]["id"], "10:00", "13:00", 3.0, "Veiledningsdag"),
        (_d(70).isoformat(), s[1]["id"], "09:00", "16:00", None, None)]
    assert all(r["innsjekk_token"] and len(r["innsjekk_kode"]) == 6 for r in dager)
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    assert (kurs["start_kl"], kurs["slutt_kl"], kurs["timer_pr_dag"]) == ("10:00", "15:00", 5.0)   # første samling


def test_ny_frist_foreslaas_naar_den_ikke_er_satt_selv(con):
    kid = _kurs(con, [_s(_d(100))], paameldingsfrist_manuell=0)
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    assert kurs["paameldingsfrist"] == kursdatoer.en_kalendermaaned_for(_d(100)).isoformat()
    assert kurs["paameldingsfrist_manuell"] == 0
    kid2 = _kurs(con, [_s(_d(100))], kode="T2", paameldingsfrist=_d(50).isoformat(), paameldingsfrist_manuell=1)
    assert con.execute("SELECT paameldingsfrist FROM kurs WHERE id=?", (kid2,)).fetchone()[0] == _d(50).isoformat()


def test_gammel_datoliste_gir_samlinger_og_delt_faktura_gir_en_samling_per_dag(con):
    datoer = [_d(40).isoformat(), _d(41).isoformat(), _d(45).isoformat()]
    k1 = db.opprett_kurs(con, kode="A", navn="A", datoer=datoer, timer_pr_dag=7, start_kl="10:00", slutt_kl="15:00")
    assert [(r["start_kl"], r["slutt_kl"], r["timer_pr_dag"]) for r in _samlinger(con, k1)] == [
        ("10:00", "15:00", 7.0), ("10:00", "15:00", 7.0)]                  # 40+41 sammen, 45 for seg
    assert [r["start_kl"] for r in _dager(con, k1)] == [None, None, None]  # arver fortsatt fra kurset, som før
    for betaling in ("per_samling", "deltaker_velger"):
        k = db.opprett_kurs(con, kode=betaling, navn="B", datoer=datoer, betaling=betaling)
        assert len(_samlinger(con, k)) == 3                                 # delfakturaen er per kursdag, som før
    k3 = db.opprett_kurs(con, kode="C", navn="C", datoer=datoer)
    assert tuple(con.execute("SELECT paameldingsfrist, paameldingsfrist_manuell FROM kurs WHERE id=?", (k3,)).fetchone()) \
        == (None, 1)                                                       # den gamle formen: fristen er som gitt


def test_lagre_samlinger_beholder_kursdager_som_er_med_og_bytter_resten(con):
    kid = _kurs(con, [_s(_d(40), _d(42))])
    for_ = {r["dato"]: (r["id"], r["innsjekk_token"], r["innsjekk_kode"]) for r in _dager(con, kid)}
    lagrede = db.lagrede_samlinger(con, kid)
    lagrede[0].fra, lagrede[0].til = _d(41), _d(43)                         # flyttet én dag fram
    lagrede[0].start_kl = "08:30"
    res = db.lagre_samlinger(con, kid, lagrede, aktor="admin:test", idag=IDAG)
    con.commit()
    etter = {r["dato"]: r for r in _dager(con, kid)}
    assert sorted(etter) == [_d(41).isoformat(), _d(42).isoformat(), _d(43).isoformat()]
    for dato in (_d(41).isoformat(), _d(42).isoformat()):                  # de samme kursdagene: samme id, QR og kode
        assert (etter[dato]["id"], etter[dato]["innsjekk_token"], etter[dato]["innsjekk_kode"]) == for_[dato]
        assert etter[dato]["start_kl"] == "08:30"
    assert (res["lagt_til"], res["fjernet"], res["kursdager"], res["samlinger"]) == (1, 1, 3, 1)
    logg = con.execute("SELECT * FROM hendelse WHERE handling='kursdager_endret'").fetchone()
    assert logg["aktor"] == "admin:test" and "@" not in logg["detaljer"]


def test_kopiert_rad_med_samme_id_blir_ny_samling_og_foreldrelose_samlinger_fjernes(con):
    kid = _kurs(con, [_s(_d(40)), _s(_d(50))])
    a, b = db.lagrede_samlinger(con, kid)
    kopi = _s(_d(60), id=a.id)                                              # samme id to ganger: bare den første gjelder
    db.lagre_samlinger(con, kid, [a, kopi], idag=IDAG)
    con.commit()
    s = _samlinger(con, kid)
    assert len(s) == 2 and s[0]["id"] == a.id and b.id not in [r["id"] for r in s]
    assert [r["samling_id"] for r in _dager(con, kid)] == [a.id, s[1]["id"]]


@pytest.mark.parametrize("hva", ["oppmote", "faktura", "faktura_forsok"])
def test_kursdag_med_oppmote_eller_faktura_kan_ikke_fjernes(con, hva):
    kid = _kurs(con, [_s(_d(40), _d(41))], pris_nok=1000)
    pid = _paamelding(con, kid)
    dag2 = _dager(con, kid)[1]
    if hva == "oppmote":
        db.registrer_oppmote(con, pid, dag2["id"], "manuell")
    elif hva == "faktura":
        con.execute("INSERT INTO faktura (paamelding_id, kursdag_id, belop_nok) VALUES (?,?,?)", (pid, dag2["id"], 500))
    else:
        con.execute("INSERT INTO faktura_forsok (paamelding_id, kursdag_id, status) VALUES (?,?,'ukjent')",
                    (pid, dag2["id"]))
    con.commit()
    with pytest.raises(db.Kursdagfeil) as feil:
        db.lagre_samlinger(con, kid, [_s(_d(40), id=_samlinger(con, kid)[0]["id"])], idag=IDAG)
    con.rollback()
    tekst = " ".join(feil.value.meldinger)
    assert "Datoene ble ikke lagret" in tekst and kursdatoer.kort_dato(_d(41)) in tekst
    assert ("oppmøte" if hva == "oppmote" else "faktura") in tekst
    assert len(_dager(con, kid)) == 2                                      # ingenting er endret


def test_fristen_folger_forste_kursdag_men_ikke_andre_endringer(con):
    kid = _kurs(con, [_s(_d(100), _d(101))], paameldingsfrist_manuell=0)
    frist = lambda: _fersk(con).execute("SELECT paameldingsfrist FROM kurs WHERE id=?", (kid,)).fetchone()[0]  # noqa: E731
    assert frist() == kursdatoer.en_kalendermaaned_for(_d(100)).isoformat()
    s = db.lagrede_samlinger(con, kid)
    s[0].fra, s[0].til = _d(120), _d(121)                                   # første kursdag flyttes: fristen følger
    db.lagre_samlinger(con, kid, s, idag=IDAG)
    con.commit()
    assert frist() == kursdatoer.en_kalendermaaned_for(_d(120)).isoformat()
    # En frist som er passert, flyttes ikke når noe annet endres (merknad, klokkeslett, en senere dag).
    con.execute("UPDATE kurs SET paameldingsfrist=? WHERE id=?", (_d(-3).isoformat(), kid))
    con.commit()
    s = db.lagrede_samlinger(con, kid)
    s[0].start_kl = "10:00"
    s[0].avvik[_d(121)] = Dagavvik(merknad="Veiledningsdag")
    s.append(_s(_d(150)))
    res = db.lagre_samlinger(con, kid, s, idag=IDAG)
    con.commit()
    assert frist() == _d(-3).isoformat() and res["paameldingsfrist"] is None
    # Satt av admin: flyttes aldri.
    con.execute("UPDATE kurs SET paameldingsfrist_manuell=1, paameldingsfrist=? WHERE id=?", (_d(30).isoformat(), kid))
    con.commit()
    s = db.lagrede_samlinger(con, kid)
    s[0].fra, s[0].til = _d(90), _d(91)
    db.lagre_samlinger(con, kid, s, idag=IDAG)
    con.commit()
    assert frist() == _d(30).isoformat()


def test_utsatt_samlet_faktura_folger_ny_forste_kursdag_i_samme_lagring(con):
    """En planlagt, utsatt samlet faktura (seks måneder før første kursdag) må flytte seg når datoene endres - ellers kunne
    fakturaen blitt sendt ut fra den gamle datoen (sveiper.oppdater_faktura_hold_etter_kursdagendring)."""
    from kurs.sveiper import seks_maaneder_for
    kid = _kurs(con, [_s(_d(400))], pris_nok=1000)
    pid = _paamelding(con, kid)
    con.execute("UPDATE paamelding SET faktura_tidligst_dato=?, sveiper_kjort=1 WHERE id=?",
                (seks_maaneder_for(_d(400)).isoformat(), pid))
    con.commit()
    s = db.lagrede_samlinger(con, kid)
    s[0].fra = s[0].til = _d(450)
    db.lagre_samlinger(con, kid, s, idag=IDAG)
    con.commit()
    assert _fersk(con).execute("SELECT faktura_tidligst_dato FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == \
        seks_maaneder_for(_d(450)).isoformat()


def test_lagrede_samlinger_gir_samme_skjema_tilbake(con):
    samlinger = [_s(_d(40), _d(43), navn="Samling 1", avvik={_d(41): Dagavvik(fjernet=True),
                                                             _d(43): Dagavvik(slutt_kl="12:00", timer=3, merknad="Veil.")}),
                 _s(_d(60), start="12:00", slutt="15:00", timer=3)]
    kid = _kurs(con, samlinger)
    lagrede = db.lagrede_samlinger(con, kid)
    assert [(s.fra, s.til, s.start_kl, s.slutt_kl, s.timer, s.navn) for s in lagrede] == [
        (_d(40), _d(43), "09:00", "16:00", 6.0, "Samling 1"), (_d(60), _d(60), "12:00", "15:00", 3.0, None)]
    assert lagrede[0].avvik[_d(41)].fjernet
    a = lagrede[0].avvik[_d(43)]
    assert (a.start_kl, a.slutt_kl, a.timer, a.merknad) == (None, "12:00", 3.0, "Veil.")
    rader = kursdatoer.rader_for_samlinger(lagrede)
    skjema = MultiDict()
    for n, r in enumerate(rader):
        for felt in ("id", "navn", "fra", "til", "start", "slutt", "timer"):
            skjema.add(f"s-{n}-{felt}", r[felt])
        for d in r["dager"]:
            for felt in ("start", "slutt", "timer", "merknad"):
                skjema.add(f"s-{n}-d-{d['iso']}-{felt}", d[felt])
            if d["fjern"]:
                skjema.add(f"s-{n}-d-{d['iso']}-fjern", "1")
    tilbake, feil = kursdatoer.fra_skjema(skjema)
    assert feil == [] and [x.datoer() for x in tilbake] == [x.datoer() for x in lagrede]
    res = db.lagre_samlinger(con, kid, tilbake, idag=IDAG)                 # lagres uendret: ingenting lagt til/fjernet
    assert (res["lagt_til"], res["fjernet"]) == (0, 0)


# ============================ migrering 9 ============================

def _database_foer_migrering_9(sti):
    """En database slik koden før migrering 9 laget den: dagens schema.sql uten samling og de nye kolonnene, på versjon 8."""
    skjema = re.sub(r"--[^\n]*", "", (config.ROT / "kurs" / "schema.sql").read_text(encoding="utf-8"))
    skjema = re.sub(r"CREATE TABLE IF NOT EXISTS ekstradeltaker_samling \(.*?\);", "", skjema, flags=re.S)     # migrering 18: peker på samling
    skjema = re.sub(r"CREATE TABLE IF NOT EXISTS samling \(.*?\);", "", skjema, flags=re.S)
    skjema = re.sub(r",\s*paameldingsfrist_manuell INTEGER NOT NULL DEFAULT 0", "", skjema)
    # Kolonnene fjernes bare fra kursdag-tabellen: andre tabeller (f.eks. kursside_versjon) har også en kolonne «merknad»
    kursdag = re.search(r"CREATE TABLE IF NOT EXISTS kursdag \(.*?\n\);", skjema, flags=re.S).group(0)
    gammel_kursdag = kursdag
    for kolonne in (r"samling_id\s+INTEGER REFERENCES samling\(id\),", r"merknad\s+TEXT,", r"timer\s+REAL,"):
        gammel_kursdag, antall = re.subn(r"\n\s+" + kolonne, "", gammel_kursdag)
        assert antall == 1, kolonne
    skjema = skjema.replace(kursdag, gammel_kursdag)
    assert "TABLE IF NOT EXISTS samling" not in skjema and "samling_id" not in skjema
    assert "paameldingsfrist_manuell" not in skjema
    raa = sqlite3.connect(sti)
    raa.executescript(skjema)
    for nr in range(1, 9):
        raa.execute("INSERT INTO schema_versjon (versjon, navn) VALUES (?, ?)", (nr, f"m{nr}"))
    raa.execute("INSERT INTO admin_bruker (brukernavn, navn, passord_hash, rolle) VALUES (?, 'A', 'x', 'system')",
                (config.ADMIN_BRUKERNAVN,))
    return raa


def _gammelt_kurs(raa, kode, datoer, *, betaling="samlet", tider=None, timer=6, frist=None):
    kid = raa.execute("INSERT INTO kurs (kursnr, kode, navn, betaling, timer_pr_dag, start_kl, slutt_kl, paameldingsfrist) "
                      "VALUES ((SELECT COALESCE(MAX(kursnr), 1000) + 1 FROM kurs), ?, ?, ?, ?, '09:00', '16:00', ?)",
                      (kode, f"Kurs {kode}", betaling, timer, frist)).lastrowid
    for i, dato in enumerate(datoer):
        start, slutt = (tider or {}).get(dato, (None, None))
        raa.execute("INSERT INTO kursdag (kurs_id, dato, start_kl, slutt_kl, innsjekk_token, innsjekk_kode) "
                    "VALUES (?,?,?,?,?,?)", (kid, dato, start, slutt, f"tok-{kode}-{i}", f"K{i}{kode}"[:6]))
    return kid


@pytest.mark.kun_sqlite
def test_migrering_9_grupperer_gamle_kursdager_konservativt(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_STI", tmp_path / "gammel.db")
    monkeypatch.setattr(config, "DEMO", True)
    raa = _database_foer_migrering_9(tmp_path / "gammel.db")
    # A: fire sammenhengende dager + en dag senere -> to samlinger. Siste dag har egne tider -> egen samling.
    a = _gammelt_kurs(raa, "A", ["2027-09-20", "2027-09-21", "2027-09-22", "2027-09-23", "2027-10-18"],
                      tider={"2027-09-23": ("09:00", "12:00")}, timer=7, frist="2027-08-20")
    # B: delt faktura (per_samling) -> én samling per kursdag, også når datoene henger sammen
    b = _gammelt_kurs(raa, "B", ["2027-09-20", "2027-09-21"], betaling="per_samling")
    # C: samlet, men én deltaker har valgt delt betaling -> én per dag
    c = _gammelt_kurs(raa, "C", ["2027-11-01", "2027-11-02"], betaling="samlet")
    deltaker = raa.execute("INSERT INTO deltaker (epost, navn) VALUES ('d@example.no', 'D')").lastrowid
    raa.execute("INSERT INTO paamelding (kurs_id, deltaker_id, betaling) VALUES (?,?,'per_samling')", (c, deltaker))
    # D: en faktura er knyttet til en kursdag -> én per dag
    d = _gammelt_kurs(raa, "D", ["2027-12-01", "2027-12-02"])
    pid = raa.execute("INSERT INTO paamelding (kurs_id, deltaker_id) VALUES (?,?)", (d, deltaker)).lastrowid
    raa.execute("INSERT INTO faktura (paamelding_id, kursdag_id, belop_nok) VALUES "
                "(?, (SELECT id FROM kursdag WHERE kurs_id=? ORDER BY dato LIMIT 1), 100)", (pid, d))
    for_ = [tuple(r) for r in raa.execute("SELECT id, kurs_id, dato, start_kl, slutt_kl, innsjekk_token, innsjekk_kode "
                                          "FROM kursdag ORDER BY id")]
    raa.commit()
    raa.close()

    assert migrer.kjor([]) == 0
    c2 = db.koble()
    assert migreringer.gjeldende_versjon(c2) == migreringer.KODEVERSJON
    etter = [tuple(r) for r in c2.execute("SELECT id, kurs_id, dato, start_kl, slutt_kl, innsjekk_token, innsjekk_kode "
                                          "FROM kursdag ORDER BY id")]
    assert etter == for_                                                    # ingen kursdag er endret
    antall = {k: c2.execute("SELECT COUNT(*) FROM samling WHERE kurs_id=?", (k,)).fetchone()[0] for k in (a, b, c, d)}
    assert antall == {a: 3, b: 2, c: 2, d: 2}
    sa = c2.execute("SELECT * FROM samling WHERE kurs_id=? ORDER BY id", (a,)).fetchall()
    assert [(s["start_kl"], s["slutt_kl"], s["timer_pr_dag"], s["navn"]) for s in sa] == [
        ("09:00", "16:00", 7.0, None), ("09:00", "12:00", 7.0, None), ("09:00", "16:00", 7.0, None)]
    assert [len(c2.execute("SELECT 1 FROM kursdag WHERE samling_id=?", (s["id"],)).fetchall()) for s in sa] == [3, 1, 1]
    assert c2.execute("SELECT COUNT(*) FROM kursdag WHERE samling_id IS NULL").fetchone()[0] == 0
    assert {r[0] for r in c2.execute("SELECT paameldingsfrist_manuell FROM kurs")} == {1}   # fristene flyttes aldri
    assert c2.execute("SELECT paameldingsfrist FROM kurs WHERE id=?", (a,)).fetchone()[0] == "2027-08-20"
    # Visningen og redigeringsskjemaet ser de samme dagene og tidene som før.
    kurs_a = c2.execute("SELECT * FROM kurs WHERE id=?", (a,)).fetchone()
    assert [(g["periode"], g["tid"]) for g in kursdatoer.visning(db.kursdager(c2, a), kurs_a)] == [
        ("20.–22.09.2027", "09:00–16:00"), ("23.09.2027", "09:00–12:00"), ("18.10.2027", "09:00–16:00")]
    # Kjøres den igjen, skjer ingenting.
    assert migrer.kjor([]) == 0
    assert c2.execute("SELECT COUNT(*) FROM samling").fetchone()[0] == 9
    c2.close()


def test_migrering_9_er_ferdig_for_nye_databaser(con):
    assert migreringer.gjeldende_versjon(con) == migreringer.KODEVERSJON >= 9
    kid = db.opprett_kurs(con, kode="N", navn="Ny", datoer=[_d(40).isoformat()])
    assert migreringer.grupper_eksisterende_kursdager(con, con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()) == []


# ============================ Opprett kurs (skjemaet) ============================

def _ny_kurs(**over):
    data = {"navn": "Emosjonsfokusert terapi – grunnkurs", "type": "fysisk", "sted": "IPR, Bergen", "kapasitet": "20",
            "pris_nok": "4500", "betalingsmaater": "1", "betalingsmaate": ["faktura"], "betaling": "samlet",
            "faktura_dager_for": "14", "fakturering": "person", "paameldingsfrist": "", "paameldingsfrist_manuell": "0",
            "ansvarlig_admin_id": "", "notat": "",
            "s-0-id": "", "s-0-navn": "", "s-0-fra": _d(90).isoformat(), "s-0-til": "", "s-0-start": "09:00",
            "s-0-slutt": "16:00", "s-0-timer": "6"}
    data.update(over)
    return {k: v for k, v in data.items() if v is not None}


def _nyeste_kurs(con):
    return _fersk(con).execute("SELECT * FROM kurs ORDER BY id DESC LIMIT 1").fetchone()


def test_skjemaet_viser_de_nye_delene_og_ikke_de_fjernede_feltene(con):
    side = _klient().get("/admin/kurs/ny").get_data(as_text=True)
    for tekst in ("Kursgjennomføring", "+ Legg til samling", "Kopier samling", "Vis dager", "Betalingsmåter",
                  "Hele kursavgiften i én faktura", "Én faktura per samling", "La deltakeren velge", "Intern kommentar",
                  "Flere innstillinger", "Kurset faktureres samlet til organisasjonen",
                  "Ingen individuelle fakturaer opprettes for deltakerne", "> Online</label>", "Foreslås automatisk"):
        assert tekst in side, tekst
    for borte in ("spesialistlop", "Spesialistløp", "kursholder", "Kursholder navn", "materiell_frist", "Praktisk info",
                  'name="datoer"', "HPR"):
        assert borte not in side, borte
    assert re.search(r'<input type="checkbox" disabled[^>]*> Kort', side)                  # kort er grået ut
    assert re.search(r'name="betalingsmaate" value="faktura" checked', side)               # faktura er valgt
    assert re.search(r'name="betaling" value="deltaker_velger" checked', side)             # deltakeren velger (standard)
    assert re.search(r'<input type="checkbox" name="fakturering" value="organisasjon" aria-describedby', side)  # av
    assert "Hvem faktureres" not in side and 'name="fakturering" value="person"' not in side
    admin = _admin_id(con)
    assert re.search(rf'<option value="{admin}" selected>', side)                         # ansvarlig = innlogget bruker


def test_endagskurs_opprettes_med_foreslaatt_frist_og_innlogget_ansvarlig(con):
    admin = _admin_id(con)
    r = _klient().post("/admin/kurs/ny", data=_ny_kurs(ansvarlig_admin_id=str(admin)))
    assert r.status_code == 302 and "/oppsett" in r.headers["Location"]
    k = _nyeste_kurs(con)
    assert (k["navn"], k["type"], k["sted"], k["kapasitet"], k["pris_nok"], k["fakturering"], k["betaling"]) == (
        "Emosjonsfokusert terapi – grunnkurs", "fysisk", "IPR, Bergen", 20, 4500, "person", "samlet")
    assert k["ansvarlig_admin_id"] == admin and k["status"] == "aapen"
    assert k["paameldingsfrist"] == kursdatoer.foreslaatt_frist(_d(90), IDAG).isoformat()
    assert k["paameldingsfrist_manuell"] == 0 and k["spesialistlop"] is None and k["kursholder_epost"] is None
    assert [d["dato"] for d in _dager(_fersk(con), k["id"])] == [_d(90).isoformat()]
    assert k["kode"].endswith(str(_d(90).year)) or str(_d(90).year) in k["kode"]


def test_flere_samlinger_med_avvikende_dag_og_egen_frist(con):
    data = _ny_kurs(paameldingsfrist=_d(30).isoformat(), paameldingsfrist_manuell="1", **{
        "s-0-navn": "Samling 1", "s-0-til": _d(93).isoformat(),
        f"s-0-d-{_d(93).isoformat()}-slutt": "12:00", f"s-0-d-{_d(93).isoformat()}-merknad": "Veiledningsdag",
        f"s-0-d-{_d(93).isoformat()}-timer": "3", f"s-0-d-{_d(91).isoformat()}-fjern": "1",
        "s-3-id": "", "s-3-navn": "Samling 2", "s-3-fra": _d(120).isoformat(), "s-3-til": _d(121).isoformat(),
        "s-3-start": "10:00", "s-3-slutt": "15:00", "s-3-timer": "5"})
    assert _klient().post("/admin/kurs/ny", data=data).status_code == 302
    k = _nyeste_kurs(con)
    assert k["paameldingsfrist"] == _d(30).isoformat() and k["paameldingsfrist_manuell"] == 1
    dager = _dager(_fersk(con), k["id"])
    assert [d["dato"] for d in dager] == [x.isoformat() for x in (_d(90), _d(92), _d(93), _d(120), _d(121))]
    siste = [d for d in dager if d["dato"] == _d(93).isoformat()][0]
    assert (siste["slutt_kl"], siste["merknad"], siste["timer"]) == ("12:00", "Veiledningsdag", 3.0)
    assert [s["navn"] for s in _samlinger(_fersk(con), k["id"])] == ["Samling 1", "Samling 2"]


@pytest.mark.parametrize("over,melding", [
    ({"sted": ""}, "Fyll inn sted"),
    ({"type": "hybrid", "sted": " "}, "Fyll inn sted"),
    ({"navn": ""}, "Fyll inn kursnavn"),
    ({"type": "annet"}, "Velg format"),
    ({"kapasitet": "0"}, "Kapasitet må være et helt tall"),
    ({"pris_nok": "-5"}, "Pris må være et helt tall"),
    ({"betalingsmaate": None}, "Kurset har en pris, men ingen betalingsmåte"),
    ({"betalingsmaate": ["kort"]}, "Kurset har en pris, men ingen betalingsmåte"),        # kortbetaling finnes ikke ennå
    ({"betaling": "kort"}, "Ugyldig fakturaplan"),
    ({"faktura_dager_for": "400"}, "mellom 0 og 180"),
    ({"s-0-fra": ""}, "Legg inn minst én kursdag"),
    ({"s-0-til": "2020-01-01"}, "til-datoen er før fra-datoen"),
    ({"s-0-slutt": "08:00"}, "«Til kl.» må være etter «Fra kl.»"),
    ({"paameldingsfrist": "31.12.2027", "paameldingsfrist_manuell": "1"}, "Påmeldingsfristen må være en gyldig dato"),
    ({"ansvarlig_admin_id": "999999"}, "Velg en ansvarlig fra listen"),
])
def test_feil_i_skjemaet_vises_uten_at_noe_lagres_og_det_som_er_skrevet_beholdes(con, over, melding):
    data = _ny_kurs(**{"s-0-navn": "Min samling", "notat": "Husk rom 3", **over})
    r = _klient().post("/admin/kurs/ny", data=data)
    side = r.get_data(as_text=True)
    assert r.status_code == 400 and "Kurset er ikke opprettet ennå" in side and melding in side
    assert _fersk(con).execute("SELECT COUNT(*) FROM kurs").fetchone()[0] == 0
    assert 'value="Min samling"' in side and "Husk rom 3" in side             # ingenting av det admin skrev, går tapt


def test_online_kurs_har_ikke_sted_og_gratis_kurs_uten_faktura_faar_fakturering_ingen(con):
    k = _klient()
    assert k.post("/admin/kurs/ny", data=_ny_kurs(type="digital", sted="Glemt sted", pris_nok="0",
                                                  betalingsmaate=None)).status_code == 302
    kurs = _nyeste_kurs(con)
    assert (kurs["type"], kurs["sted"], kurs["fakturering"]) == ("digital", None, "ingen")


def test_samlet_til_organisasjon_under_flere_innstillinger(con):
    assert _klient().post("/admin/kurs/ny", data=_ny_kurs(fakturering="organisasjon")).status_code == 302
    assert _nyeste_kurs(con)["fakturering"] == "organisasjon"


def test_intern_kommentar_lagres_i_notat(con):
    assert _klient().post("/admin/kurs/ny", data=_ny_kurs(notat="Bestill lunsj")).status_code == 302
    assert _nyeste_kurs(con)["notat"] == "Bestill lunsj"


def test_duplisering_kopierer_samlingene_uten_datoer_og_innlogget_bruker_blir_ansvarlig(con):
    admin = _admin_id(con)
    annen = db.sett_inn(con, "INSERT INTO admin_bruker (brukernavn, navn, passord_hash, rolle) VALUES "
                             "('annen', 'Annen Admin', 'x', 'kursadmin')", ())
    kid = _kurs(con, [_s(_d(40), _d(43), navn="Samling 1", start="10:00", slutt="15:00", timer=5), _s(_d(70))],
                ansvarlig_admin_id=annen, notat="Intern merknad")
    side = _klient().get(f"/admin/kurs/ny?fra={kid}").get_data(as_text=True)
    assert 'value="Samling 1"' in side and 'value="10:00"' in side and 'value="5"' in side
    assert "Samlingen i originalkurset varte 4 dager" in side
    assert _d(40).isoformat() not in side and _d(70).isoformat() not in side    # datoene fylles ikke inn
    assert re.search(rf'<option value="{admin}" selected>', side) and not re.search(rf'<option value="{annen}" selected>', side)
    data = _ny_kurs(fra=str(kid), **{"s-0-navn": "Samling 1", "s-0-fra": _d(400).isoformat(),
                                     "s-0-til": _d(403).isoformat()})
    assert _klient().post(f"/admin/kurs/ny?fra={kid}", data=data).status_code == 302
    kopi = _nyeste_kurs(con)
    assert kopi["id"] != kid and kopi["status"] == "utkast" and kopi["kursnr"] != _fersk(con).execute(
        "SELECT kursnr FROM kurs WHERE id=?", (kid,)).fetchone()[0]


# ============================ Oppsett: kursinnstillinger ============================

def _innstillinger(klient, kid, **over):
    side = klient.get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True)
    versjon = re.search(r'name="versjon" value="([^"]+)"', side)[1]
    data = {"versjon": versjon, "navn": "Testkurs", "type": "fysisk", "sted": "Bergen", "zoom_url": "", "kapasitet": "",
            "paameldingsfrist": "", "paameldingsfrist_manuell": "1", "pris_nok": "1000", "betalingsmaater": "1",
            "betalingsmaate": ["faktura"], "betaling": "samlet", "faktura_dager_for": "14", "fakturering": "person",
            "notat": ""}
    data.update(over)
    return klient.post(f"/admin/kurs/{kid}/oppsett", data={k: v for k, v in data.items() if v is not None})


def test_innstillinger_kan_lagres_selv_om_kurset_er_fakturert(con):
    kid = _kurs(con, [_s(_d(40))], pris_nok=1000)
    pid = _paamelding(con, kid)
    con.execute("INSERT INTO faktura (paamelding_id, belop_nok) VALUES (?, 1000)", (pid,))
    con.commit()
    k = _klient()
    side = k.get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True)
    # Pris og fakturaoppsett kan endres også her (Camilla 01.10.2026), men må bekreftes (test_pris_redigerbar.py); en side uten feltene,
    # slik som nedenfor, endrer dem ikke.
    assert 'name="betalingsmaate"' in side and 'name="bekreft_okonomi"' in side and "kan ikke endres" not in side
    data = {"navn": "Nytt navn", "pris_nok": None, "betalingsmaater": None, "betalingsmaate": None, "betaling": None,
            "faktura_dager_for": None, "fakturering": None}
    r = _innstillinger(k, kid, **data)
    assert r.status_code == 302
    rad = _fersk(con).execute("SELECT navn, pris_nok, fakturering FROM kurs WHERE id=?", (kid,)).fetchone()
    assert tuple(rad) == ("Nytt navn", 1000, "person")                     # lagret - og økonomien er urørt


def test_innstillinger_uten_kursholderfelt_rorer_ikke_kursholder(con):
    kid = _kurs(con, [_s(_d(40))], kursholder_epost="kursholder@example.no")
    assert _innstillinger(_klient(), kid, navn="Endret").status_code == 302
    assert _fersk(con).execute("SELECT kursholder_epost FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "kursholder@example.no"


def test_frist_satt_selv_i_oppsett_og_tilbake_til_forslag(con):
    kid = _kurs(con, [_s(_d(100))], paameldingsfrist_manuell=0)
    k = _klient()
    _innstillinger(k, kid, paameldingsfrist=_d(20).isoformat(), paameldingsfrist_manuell="1")
    rad = _fersk(con).execute("SELECT paameldingsfrist, paameldingsfrist_manuell FROM kurs WHERE id=?", (kid,)).fetchone()
    assert tuple(rad) == (_d(20).isoformat(), 1)
    forslag = kursdatoer.foreslaatt_frist(_d(100), IDAG).isoformat()
    _innstillinger(k, kid, paameldingsfrist=forslag, paameldingsfrist_manuell="0")     # «Bruk forslaget»
    rad = _fersk(con).execute("SELECT paameldingsfrist, paameldingsfrist_manuell FROM kurs WHERE id=?", (kid,)).fetchone()
    assert tuple(rad) == (forslag, 0)


def test_fysisk_krever_sted_i_oppsett_og_online_fjerner_sted(con):
    kid = _kurs(con, [_s(_d(40))])
    k = _klient()
    r = _innstillinger(k, kid, sted="")
    assert r.status_code == 302 and "Fyll inn sted" in k.get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True)
    _innstillinger(k, kid, type="digital", sted="Bergen")
    assert tuple(_fersk(con).execute("SELECT type, sted FROM kurs WHERE id=?", (kid,)).fetchone()) == ("digital", None)


# ============================ Oppsett: kursdatoer ============================

def _datoskjema(klient, kid, **over):
    side = klient.get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True)
    data = {"kursdato_versjon": re.search(r'name="kursdato_versjon" value="([^"]+)"', side)[1]}
    for m in re.finditer(r'<input[^>]*\bname="(s-\d+-[a-z]+)"[^>]*>', side):
        data[m[1]] = re.search(r'value="([^"]*)"', m[0])[1]
    data.update(over)
    return data


def test_datoene_kan_endres_i_oppsett_og_dagene_beholder_qr(con):
    kid = _kurs(con, [_s(_d(40), _d(41))])
    for_ = {d["dato"]: d["innsjekk_token"] for d in _dager(con, kid)}
    k = _klient()
    data = _datoskjema(k, kid, **{"s-0-fra": _d(41).isoformat(), "s-0-til": _d(42).isoformat()})
    assert data["s-0-id"]
    r = k.post(f"/admin/kurs/{kid}/kursdatoer", data=data)
    assert r.status_code == 302
    etter = {d["dato"]: d["innsjekk_token"] for d in _dager(_fersk(con), kid)}
    assert sorted(etter) == [_d(41).isoformat(), _d(42).isoformat()]
    assert etter[_d(41).isoformat()] == for_[_d(41).isoformat()]
    side = k.get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True)
    assert "Kursdatoene er lagret (1 ny kursdag, 1 kursdag fjernet)." in side


def test_dag_med_oppmote_kan_ikke_fjernes_i_oppsett_og_skjemaet_vises_igjen(con):
    kid = _kurs(con, [_s(_d(40), _d(41))])
    pid = _paamelding(con, kid)
    db.registrer_oppmote(con, pid, _dager(con, kid)[1]["id"], "qr")
    con.commit()
    k = _klient()
    r = k.post(f"/admin/kurs/{kid}/kursdatoer", data=_datoskjema(k, kid, **{"s-0-til": "", "s-0-navn": "Ny tekst"}))
    side = r.get_data(as_text=True)
    assert r.status_code == 400 and "Datoene er ikke lagret" in side and "har registrert oppmøte" in side
    assert 'value="Ny tekst"' in side                                       # det admin skrev, står igjen
    assert len(_dager(_fersk(con), kid)) == 2


def test_datoene_lagres_ikke_hvis_noen_andre_har_endret_dem(con):
    kid = _kurs(con, [_s(_d(40))])
    k = _klient()
    data = _datoskjema(k, kid, **{"s-0-fra": _d(50).isoformat()})
    s = db.lagrede_samlinger(con, kid)
    s[0].start_kl = "07:00"
    db.lagre_samlinger(con, kid, s, idag=IDAG)
    con.commit()
    r = k.post(f"/admin/kurs/{kid}/kursdatoer", data=data)
    assert r.status_code == 302 and "noen andre har endret kursdatoene" in k.get(r.headers["Location"]).get_data(as_text=True)
    assert [d["dato"] for d in _dager(_fersk(con), kid)] == [_d(40).isoformat()]


def test_innsjekktabellen_viser_samlinger_og_merknad(con):
    kid = _kurs(con, [_s(_d(40), _d(41), avvik={_d(41): Dagavvik(merknad="Veiledningsdag")}), _s(_d(60), navn="Eksamen")])
    side = _klient().get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True)
    assert f"Samling 1 · {kursdatoer.periode(_d(40), _d(41))} · kl. 09:00–16:00" in side
    assert f"Eksamen · {kursdatoer.kort_dato(_d(60))}" in side
    assert '<span class="merke">Veiledningsdag</span>' in side


# ============================ Oppsett: presentasjon fra kursholder ============================

def test_be_om_presentasjon_lagrer_forespørsel_og_logger_uten_personopplysninger(con):
    kid = _kurs(con, [_s(_d(40))])
    k = _klient()
    r = k.post(f"/admin/kurs/{kid}/materiell", data={"ansvarlig_navn": "Kari Kursholder",
                                                    "ansvarlig_epost": "kari@example.no", "frist": _d(30).isoformat()})
    assert r.status_code == 302 and r.headers["Location"].endswith("#kursmateriell")
    krav = _fersk(con).execute("SELECT * FROM materiell_krav WHERE kurs_id=?", (kid,)).fetchall()
    assert [(m["ansvarlig_navn"], m["ansvarlig_epost"], m["frist"]) for m in krav] == [
        ("Kari Kursholder", "kari@example.no", _d(30).isoformat())]
    logg = _fersk(con).execute("SELECT detaljer FROM hendelse WHERE handling='materiell_bedt_om'").fetchone()[0]
    assert "kari" not in logg.lower() and "Kari" not in logg
    side = k.get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True)
    assert "Kari Kursholder" in side and "venter" in side


@pytest.mark.parametrize("data,melding", [
    ({"ansvarlig_navn": "", "ansvarlig_epost": "k@example.no", "frist": "2027-01-01"}, "Fyll inn navnet"),
    ({"ansvarlig_navn": "K", "ansvarlig_epost": "ikke-epost", "frist": "2027-01-01"}, "gyldig e-postadresse"),
    ({"ansvarlig_navn": "K", "ansvarlig_epost": "k@example.no", "frist": ""}, "gyldig frist"),
])
def test_be_om_presentasjon_kontrollerer_feltene(con, data, melding):
    kid = _kurs(con, [_s(_d(40))])
    k = _klient()
    k.post(f"/admin/kurs/{kid}/materiell", data=data)
    assert melding in k.get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True)
    assert _fersk(con).execute("SELECT COUNT(*) FROM materiell_krav").fetchone()[0] == 0


def test_forespørsel_kan_fjernes_bare_foer_levering(con):
    kid = _kurs(con, [_s(_d(40))])
    apen = db.be_om_materiell(con, kid, navn="A", epost="a@example.no", frist=_d(30).isoformat())
    levert = db.be_om_materiell(con, kid, navn="B", epost="b@example.no", frist=_d(30).isoformat())
    con.execute("UPDATE materiell_krav SET levert_ts='2026-01-01' WHERE id=?", (levert,))
    con.commit()
    k = _klient()
    k.post(f"/admin/kurs/{kid}/materiell/{apen}/fjern")
    k.post(f"/admin/kurs/{kid}/materiell/{levert}/fjern")
    assert [r[0] for r in _fersk(con).execute("SELECT id FROM materiell_krav")] == [levert]


def test_lesebruker_kan_ikke_endre_datoer_eller_be_om_materiell(con):
    kid = _kurs(con, [_s(_d(40))])
    db.opprett_admin_bruker(con, "leser", "Leser", "passord-som-holder", rolle="lese")
    con.commit()
    k = _klient(logg_inn=False)
    k.post("/admin/logg-inn", data={"brukernavn": "leser", "passord": "passord-som-holder"})
    assert k.post(f"/admin/kurs/{kid}/kursdatoer", data={"s-0-fra": _d(50).isoformat()}).status_code == 403
    assert k.post(f"/admin/kurs/{kid}/materiell", data={"ansvarlig_navn": "X", "ansvarlig_epost": "x@example.no",
                                                       "frist": _d(30).isoformat()}).status_code == 403
    side = k.get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True)
    assert "Lagre kursdatoer" not in side and "Be kursholder om presentasjon" not in side


# ============================ visning for deltakere ============================

def test_paameldingssiden_viser_datoene_gruppert_og_aldri_intern_kommentar(con):
    kid = _kurs(con, [_s(_d(40), _d(42), navn="Samling 1"), _s(_d(70))], notat="HEMMELIG-INTERNT")
    kode = con.execute("SELECT kode FROM kurs WHERE id=?", (kid,)).fetchone()[0]
    side = _klient(logg_inn=False).get(f"/kurs/{kode}").get_data(as_text=True)
    tekst = " ".join(re.sub(r"<[^>]+>", " ", side).split())   # synlig tekst: klokkeslettet står på egen linje
    assert f"Samling 1: {kursdatoer.periode(_d(40), _d(42))} kl. 09:00–16:00" in tekst
    assert f"Samling 2: {kursdatoer.kort_dato(_d(70))} kl. 09:00–16:00" in tekst
    assert side.count('<span class="dempet">kl. 09:00–16:00</span>') == 2
    assert "HEMMELIG-INTERNT" not in side


def test_epostene_viser_gruppert_dato_og_aldri_intern_kommentar(con):
    kid = _kurs(con, [_s(_d(40), _d(41), avvik={_d(41): Dagavvik(slutt_kl="12:00", merknad="Veiledningsdag")})],
                notat="HEMMELIG-INTERNT")
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    dager = db.kursdager(con, kid)
    p = {"navn": "Ola Nordmann", "fornavn": "Ola", "betaling": "samlet", "betaler": "person", "org_navn": None}
    _, html = epost.render("bekreftelse", p=p, kurs=kurs, dager=dager, faktura_plan=FakturaPlan(PLAN_NA))
    tekst = " ".join(html.split())
    assert f"<li>{kursdatoer.periode(_d(40), _d(41))} kl. 09:00–16:00" in tekst
    assert f"{kursdatoer.ukedag(_d(41))} {kursdatoer.kort_dato(_d(41))}: kl. 09:00–12:00 – Veiledningsdag" in tekst
    _, ukefor = epost.render("ukefor", d=p, kurs=kurs, dager=dager)
    assert "HEMMELIG-INTERNT" not in html and "HEMMELIG-INTERNT" not in ukefor


def test_intern_kommentar_vises_aldri_paa_min_side_eller_i_epost_dagen_foer(con):
    kid = _kurs(con, [_s(_d(1)), _s(_d(2))], notat="HEMMELIG-INTERNT")
    pid = _paamelding(con, kid)
    k = _klient(logg_inn=False)
    with k.session_transaction() as s:
        s["deltaker_id"] = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pid,)).fetchone()[0]
    side = k.get("/min-side").get_data(as_text=True)
    assert "Testkurs" in side and "HEMMELIG-INTERNT" not in side
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    d = {"navn": "Test Deltaker1", "fornavn": "Test", "betaling": "samlet", "betaler": "person", "org_navn": None}
    for nr, dag in enumerate(db.kursdager(con, kid), 1):
        _, html = epost.render("dagfor", d=d, kurs=kurs, dag=dag, nr=nr, antall=2)
        assert "Testkurs" in html and "HEMMELIG-INTERNT" not in html


def test_kursbevis_summerer_timer_per_kursdag(con):
    kid = _kurs(con, [_s(_d(-10), _d(-8), timer=6, avvik={_d(-8): Dagavvik(timer=3)}), _s(_d(-2), timer=7.5)],
                notat="HEMMELIG-INTERNT")
    pid = _paamelding(con, kid)
    for dag in _dager(con, kid):
        db.registrer_oppmote(con, pid, dag["id"], "qr")
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()
    k = Kjoring(con, idag=IDAG)
    kursbevis.kjor(k)
    con.commit()
    innhold = con.execute("SELECT innhold FROM dokument_innhold").fetchone()[0]
    assert re.search(r"<td>Timer</td><td>22,5|<td>Timer</td><td>22.5", innhold), innhold      # 6 + 6 + 3 + 7,5
    assert "HEMMELIG-INTERNT" not in innhold                                                # intern kommentar
    assert all("HEMMELIG-INTERNT" not in f.read_text(encoding="utf-8") for f in config.UTBOKS.glob("*.html"))


# ============================ Betaling (tilbakemelding 28.09.2026) ============================

def test_samlet_til_organisasjonen_er_en_avkrysningsboks_som_er_av_som_standard(con):
    k = _klient()
    uten = {key: v for key, v in _ny_kurs().items() if key != "fakturering"}                # boksen ikke krysset av
    assert k.post("/admin/kurs/ny", data=uten).status_code == 302
    assert _nyeste_kurs(con)["fakturering"] == "person"
    assert k.post("/admin/kurs/ny", data=_ny_kurs(navn="Bedriftsinternt kurs", fakturering="organisasjon")).status_code == 302
    kurs = _nyeste_kurs(con)
    assert kurs["fakturering"] == "organisasjon"
    side = k.get(f"/admin/kurs/{kurs['id']}/oppsett").get_data(as_text=True)
    assert re.search(r'<details class="flere-innstillinger" open>', side)                  # vises åpent når den er på
    assert re.search(r'name="fakturering" value="organisasjon" checked', side)


def test_nytt_kurs_lar_deltakeren_velge_bare_naar_det_er_flere_samlinger(con):
    k = _klient()
    assert k.post("/admin/kurs/ny", data=_ny_kurs(betaling="deltaker_velger", **{
        "s-1-id": "", "s-1-navn": "", "s-1-fra": _d(120).isoformat(), "s-1-til": "", "s-1-start": "09:00",
        "s-1-slutt": "16:00", "s-1-timer": "6"})).status_code == 302
    to = _nyeste_kurs(con)
    assert (to["betaling"], db.antall_samlinger(_fersk(con), to["id"])) == ("deltaker_velger", 2)
    assert "Delt opp" in _klient(logg_inn=False).get(f"/kurs/{to['kode']}").get_data(as_text=True)
    assert k.post("/admin/kurs/ny", data=_ny_kurs(navn="Endagskurs", betaling="deltaker_velger")).status_code == 302
    en = _nyeste_kurs(con)
    assert db.antall_samlinger(_fersk(con), en["id"]) == 1
    offentlig = _klient(logg_inn=False).get(f"/kurs/{en['kode']}").get_data(as_text=True)
    assert "Delt opp" not in offentlig and 'name="betaling"' not in offentlig

