"""Sjekklister for planlagte kurs (bestillingen 02.10.2026, steg 2 og 3): maler per kurstype med regler for frist og hvilke
samlinger et punkt gjelder, én sjekkliste per samling (nedtrekk i kalenderen, full utgave på kursets side), avkryssing med
hvem og når, «Ikke aktuelt», frist for hånd, egne punkter, «Oppdater fra malen», forfalte punkter merket rødt, og
påminnelsen (kortet på Oversikten og morgen-e-posten til kurs@ipr.no). Alle
kurs, maler og tekster her er oppdiktet."""
import json
import re
from datetime import date, timedelta

import pytest
from werkzeug.datastructures import MultiDict

from kurs import config, daglig, db, migreringer, planlagte_kurs, sjekklister
from kurs.integrasjoner import epost
from kurs.kjoring import Kjoring

FRAM = date.today() + timedelta(days=60)


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _klient(brukernavn=None, passord=None, dag=None):
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": brukernavn or config.ADMIN_BRUKERNAVN,
                                    "passord": passord or config.ADMIN_PASSORD})
    if dag:
        with k.session_transaction() as s:
            s["demo_dato"] = dag.isoformat()
    return k


def _samling(fra, til=None, **felt):
    return planlagte_kurs.valider_samling(MultiDict({"fra_dato": fra.isoformat(), "til_dato": (til or fra).isoformat(),
                                                     **{k: str(v) for k, v in felt.items()}}))


def _plan(con, navn, *samlinger, **kurs):
    verdier = planlagte_kurs.valider_kurs(MultiDict({"navn": navn, **{k: str(v) for k, v in kurs.items()}}))
    pid = planlagte_kurs.opprett(con, verdier, list(samlinger), "admin:test")
    con.commit()
    return pid


def _mal(con, navn, *punkter):
    """punkter: (tekst, {felt: verdi}) - feltene som i skjemaet (frist_antall, frist_enhet, frist_retning, frist_fra, gjelder …)"""
    mid = sjekklister.opprett_mal(con, sjekklister.valider_mal(MultiDict({"navn": navn})), "admin:test")
    ider = []
    for tekst, felt in punkter:
        ider.append(sjekklister.opprett_malpunkt(con, mid, sjekklister.valider_malpunkt(
            MultiDict({"tekst": tekst, **{k: str(v) for k, v in felt.items()}})), "admin:test"))
    con.commit()
    return mid, ider


def _samlinger(con, pid):
    return [s["id"] for s in planlagte_kurs.hent(con, pid)["samlinger"]]


def _punkter(con, sid):
    return {r["tekst"]: dict(r) for r in con.execute("SELECT * FROM sjekkliste_punkt WHERE samling_id=? ORDER BY rekkefolge",
                                                     (sid,))}


MND = {"frist_antall": 1, "frist_enhet": "maaneder", "frist_retning": "for", "frist_fra": "start"}


# ============================ migrering 21 ============================

def test_migrering_21_lager_tabellene(con):
    assert (21, "sjekklister") in [(n, navn) for n, navn, _ in migreringer.MIGRERINGER]
    for t in ("sjekkliste_mal", "sjekkliste_malpunkt", "planlagt_kurs_sjekkliste", "sjekkliste_punkt"):
        assert db.har_tabell(con, t)
    for t in ("sjekkliste_punkt", "planlagt_kurs_sjekkliste", "sjekkliste_malpunkt", "sjekkliste_mal"):
        con.execute(f"DROP TABLE {t}")
    con.execute("DELETE FROM schema_versjon WHERE versjon >= 21")
    con.commit()
    assert migreringer.kjor_manglende(con) == [n for n, _, _ in migreringer.MIGRERINGER if n >= 21]
    assert db.har_tabell(con, "sjekkliste_punkt") and migreringer.kjor_manglende(con) == []
    tekst = (db._SCHEMA.parent.parent / "MIGRATIONS.md").read_text(encoding="utf-8")
    assert "sjekklister" in tekst and "sjekkliste_punkt" in tekst


# ============================ frister ============================

@pytest.mark.parametrize("fra, til, antall, enhet, anker, ventet", [
    (date(2027, 1, 18), date(2027, 1, 21), -1, "maaneder", "start", date(2026, 12, 18)),   # 1 måned før (samme dato)
    (date(2027, 1, 18), date(2027, 1, 21), -1, "uker", "start", date(2027, 1, 11)),
    (date(2027, 1, 18), date(2027, 1, 21), -2, "virkedager", "start", date(2027, 1, 14)),   # mandag -> torsdag før
    (date(2027, 1, 18), date(2027, 1, 21), 2, "uker", "slutt", date(2027, 2, 4)),
    (date(2027, 1, 18), date(2027, 1, 21), 3, "virkedager", "slutt", date(2027, 1, 26)),
    (date(2027, 3, 31), date(2027, 3, 31), -1, "maaneder", "start", date(2027, 2, 26)),     # 28. feb er søndag -> fredag
    (date(2027, 5, 18), date(2027, 5, 18), -1, "dager", "start", date(2027, 5, 14)),       # 17. mai (rød dag) -> fredag
    (date(2027, 5, 19), date(2027, 5, 19), -2, "virkedager", "start", date(2027, 5, 14)),  # hopper over 17. mai og helgen
    (date(2027, 1, 18), date(2027, 1, 21), 0, "dager", "slutt", date(2027, 1, 21)),
    (date(2027, 1, 18), date(2027, 1, 21), None, "dager", "start", None),
])
def test_fristen_regnes_ut_og_flyttes_bort_fra_helg_og_roede_dager(fra, til, antall, enhet, anker, ventet):
    assert sjekklister.regn_frist(fra, til, antall, enhet, anker) == ventet


def test_regel_og_gjelder_som_tekst():
    def mp(**kw):
        return {"frist_antall": None, "frist_enhet": "dager", "frist_fra": "start", "gjelder": "alle", "gjelder_nr": None,
                "gjelder_sted": None, "gjelder_navn": None, **kw}
    assert sjekklister.regel_tekst(mp(frist_antall=-1, frist_enhet="maaneder")) == "1 måned før start"
    assert sjekklister.regel_tekst(mp(frist_antall=3, frist_enhet="virkedager", frist_fra="slutt")) == "3 virkedager etter slutt"
    assert sjekklister.regel_tekst(mp()) == "Ingen frist"
    assert sjekklister.regel_tekst(mp(frist_antall=0)) == "Samme dag som start"
    assert sjekklister.gjelder_tekst(mp(gjelder="siste", gjelder_sted="Oslo", gjelder_navn="Modul 3")) == \
        "Bare siste samling · Bare Oslo · Bare kurs med «Modul 3» i navnet"
    assert sjekklister.gjelder_tekst(mp(gjelder="nr", gjelder_nr=2, gjelder_sted="fysisk")) == "Bare samling nr. 2 · Bare fysiske"


@pytest.mark.parametrize("skjema, feil", [
    ({"tekst": ""}, "Teksten må fylles ut."),
    ({"tekst": "X", "gjelder": "nr"}, "Skriv hvilken samling punktet gjelder (nummeret)."),
    ({"tekst": "X", "gjelder": "noe"}, "Ugyldig valg for hvilke samlinger punktet gjelder."),
    ({"tekst": "X", "frist_antall": "-3"}, "Fristen må være et tall fra 0 til 366."),
    ({"tekst": "X", "frist_enhet": "aar"}, "Ugyldig frist."),
    ({"tekst": "X", "type": "annet"}, "Ukjent type."),
])
def test_malpunkt_valideres(skjema, feil):
    with pytest.raises(sjekklister.SjekklisteFeil) as e:
        sjekklister.valider_malpunkt(MultiDict(skjema))
    assert str(e.value) == feil


def test_husk_har_aldri_frist():
    v = sjekklister.valider_malpunkt(MultiDict({"tekst": "Lenke", "type": "husk", **MND}))
    assert v["frist_antall"] is None and v["type"] == "husk"


# ============================ hvilke punkter gjelder hvilken samling ============================

def _tre_samlinger(con, navn="Modul 3 (kull 99)", sted="Oslo", **kurs):
    return _plan(con, navn, *[_samling(date(2031, m, 3), date(2031, m, 6), nr=i, sted=sted)
                              for i, m in ((1, 2), (2, 5), (3, 9))], **kurs)


def test_punktene_gjelder_riktige_samlinger(con):
    mid, _ = _mal(con, "Utdanning",
                  ("Alle", {}), ("Første", {"gjelder": "forste"}), ("Siste", {"gjelder": "siste"}),
                  ("Ikke første", {"gjelder": "ikke_forste"}), ("Ikke siste", {"gjelder": "ikke_siste"}),
                  ("Nr 2", {"gjelder": "nr", "gjelder_nr": 2}), ("Bare Bergen", {"gjelder_sted": "Bergen"}),
                  ("Bare Oslo", {"gjelder_sted": "oslo"}), ("Bare fysisk", {"gjelder_sted": "fysisk"}),
                  ("Bare online", {"gjelder_sted": "online"}), ("Bare Modul 3", {"gjelder_navn": "modul 3", "gjelder": "siste"}),
                  ("Bare Modul 2", {"gjelder_navn": "Modul 2"}))
    pid = _tre_samlinger(con)
    sjekklister.sett_mal(con, pid, mid, "admin:test")
    s1, s2, s3 = _samlinger(con, pid)
    assert sjekklister.lag_for_kurs(con, pid, "admin:test") == 3
    assert list(_punkter(con, s1)) == ["Alle", "Første", "Ikke siste", "Bare Oslo", "Bare fysisk"]
    assert list(_punkter(con, s2)) == ["Alle", "Ikke første", "Ikke siste", "Nr 2", "Bare Oslo", "Bare fysisk"]
    assert list(_punkter(con, s3)) == ["Alle", "Siste", "Ikke første", "Bare Oslo", "Bare fysisk", "Bare Modul 3"]


def test_kurs_lagt_inn_fra_andre_samling_faar_ikke_oppstartspunktene(con):
    """673-tilfellet: 1. samling var før kursplanen, så 2. samling er ikke «første», og 3. samling er siste av 3."""
    mid, _ = _mal(con, "EFST", ("Oppstart av nytt kurs", {"gjelder": "forste"}), ("3.samling: kursbevis", {"gjelder": "siste"}),
                  ("Felles", {}))
    pid = _plan(con, "EFST testkull", _samling(date(2031, 1, 13), date(2031, 1, 16), nr=2),
                _samling(date(2031, 5, 19), date(2031, 5, 22), nr=3), antall_samlinger=3)
    sjekklister.sett_mal(con, pid, mid, "admin:test")
    sjekklister.lag_for_kurs(con, pid, "admin:test")
    s2, s3 = _samlinger(con, pid)
    assert list(_punkter(con, s2)) == ["Felles"]
    assert list(_punkter(con, s3)) == ["3.samling: kursbevis", "Felles"]


def test_kurs_med_en_samling_er_baade_foerste_og_siste_og_underpunkt_foelger_forelderen(con):
    mid, _ = _mal(con, "Kortkurs", ("Første", {"gjelder": "forste"}), ("Siste", {"gjelder": "siste"}),
                  ("Ikke siste", {"gjelder": "ikke_siste"}), ("Underpunkt til ikke siste", {"nivaa": 1}),
                  ("Hotell", {"gjelder_sted": "fysisk"}), ("Under hotell", {"nivaa": 1}))
    online = _plan(con, "Webinar", _samling(date(2031, 3, 3), sted="Online"))
    sjekklister.sett_mal(con, online, mid, "admin:test")
    sjekklister.lag_for_kurs(con, online, "admin:test")
    assert list(_punkter(con, _samlinger(con, online)[0])) == ["Første", "Siste"]          # underpunktene følger forelderen
    fysisk = _plan(con, "Todager", _samling(date(2031, 3, 3), sted="Oslo"))
    sjekklister.sett_mal(con, fysisk, mid, "admin:test")
    sjekklister.lag_for_kurs(con, fysisk, "admin:test")
    assert list(_punkter(con, _samlinger(con, fysisk)[0])) == ["Første", "Siste", "Hotell", "Under hotell"]


def test_sjekklisten_faar_frister_og_lages_bare_en_gang(con):
    mid, _ = _mal(con, "Mal", ("Info 1 mnd før", MND), ("Uten frist", {}),
                  ("Etter", {"frist_antall": 2, "frist_enhet": "uker", "frist_retning": "etter", "frist_fra": "slutt"}))
    pid = _plan(con, "Kurs", _samling(date(2027, 1, 18), date(2027, 1, 21)), _samling(date(2027, 5, 24), date(2027, 5, 27)))
    sjekklister.sett_mal(con, pid, mid, "admin:test")
    s1, s2 = _samlinger(con, pid)
    assert sjekklister.lag_for_kurs(con, pid, "admin:test", fra=date(2027, 3, 1)) == 1          # bare samlinger fra 1. mars
    assert _punkter(con, s1) == {} and _punkter(con, s2)["Info 1 mnd før"]["frist"] == "2027-04-23"   # 24. apr er lørdag
    assert sjekklister.lag_for_samling(con, s2, "admin:test") == 0                               # finnes fra før
    assert sjekklister.lag_for_samling(con, s1, "admin:test") == 3
    p = _punkter(con, s1)
    assert (p["Info 1 mnd før"]["frist"], p["Uten frist"]["frist"], p["Etter"]["frist"]) == ("2026-12-18", None, "2027-02-04")


# ============================ endringer: malen, datoene, avkryssing ============================

def test_oppdater_fra_malen_roerer_aldri_det_som_er_gjort(con):
    mid, (a, b, c, d) = _mal(con, "Mal", ("Åpent punkt", MND), ("Utført punkt", MND), ("Fjernes", MND), ("Frist for hånd", MND))
    pid = _plan(con, "Kurs", _samling(date(2027, 1, 18), date(2027, 1, 21)))
    sjekklister.sett_mal(con, pid, mid, "admin:test")
    (sid,) = _samlinger(con, pid)
    sjekklister.lag_for_samling(con, sid, "admin:test")
    p = _punkter(con, sid)
    sjekklister.sett_status(con, p["Utført punkt"]["id"], "utfort", "admin:kari")
    sjekklister.sett_frist(con, p["Frist for hånd"]["id"], "2026-12-01", "admin:test")
    eget = sjekklister.legg_til_punkt(con, sid, MultiDict({"tekst": "Eget punkt"}), "admin:test")
    ny = dict(MND, frist_antall=2, frist_enhet="uker")
    for pid_, tekst in ((a, "Åpent punkt, ny tekst"), (b, "Utført punkt, ny tekst"), (d, "Frist for hånd, ny tekst")):
        sjekklister.oppdater_malpunkt(con, mid, pid_, sjekklister.valider_malpunkt(MultiDict({"tekst": tekst, **ny})), "admin:test")
    sjekklister.slett_malpunkt(con, mid, c, "admin:test")                     # blir et eget punkt i sjekklisten: står igjen
    sjekklister.opprett_malpunkt(con, mid, sjekklister.valider_malpunkt(MultiDict({"tekst": "Nytt i malen", **MND})), "admin:test")
    assert sjekklister.oppdater_fra_mal(con, sid, "admin:test") == {"lagt_til": 1, "endret": 2, "fjernet": 0}
    p = _punkter(con, sid)
    assert p["Åpent punkt, ny tekst"]["frist"] == "2027-01-04"                # 2 uker før start
    assert "Utført punkt" in p and p["Utført punkt"]["status"] == "utfort"   # gjort: ikke rørt
    assert p["Frist for hånd, ny tekst"]["frist"] == "2026-12-01"            # fristen for hånd beholdes
    assert {"Fjernes", "Eget punkt", "Nytt i malen"} <= set(p) and eget
    sjekklister.oppdater_malpunkt(con, mid, a, sjekklister.valider_malpunkt(MultiDict({"tekst": "Åpent", "gjelder": "nr",
                                                                                        "gjelder_nr": 5})), "admin:test")
    assert sjekklister.oppdater_fra_mal(con, sid, "admin:test")["fjernet"] == 1      # gjelder ikke lenger og er åpent


def test_nye_datoer_flytter_aapne_frister(con):
    mid, _ = _mal(con, "Mal", ("A", MND), ("B", MND), ("C", MND))
    pid = _plan(con, "Kurs", _samling(date(2027, 1, 18), date(2027, 1, 21)))
    sjekklister.sett_mal(con, pid, mid, "admin:test")
    (sid,) = _samlinger(con, pid)
    sjekklister.lag_for_samling(con, sid, "admin:test")
    p = _punkter(con, sid)
    sjekklister.sett_status(con, p["B"]["id"], "utfort", "admin:test")
    sjekklister.sett_frist(con, p["C"]["id"], "2026-12-01", "admin:test")
    planlagte_kurs.oppdater_samling(con, pid, sid, _samling(date(2027, 2, 15), date(2027, 2, 18)), "admin:test")
    assert sjekklister.flytt_frister(con, sid) == 1
    p = _punkter(con, sid)
    assert (p["A"]["frist"], p["B"]["frist"], p["C"]["frist"]) == ("2027-01-15", "2026-12-18", "2026-12-01")
    sjekklister.sett_frist(con, p["C"]["id"], "", "admin:test")                       # følg datoene igjen
    assert _punkter(con, sid)["C"]["frist"] == "2027-01-15"


def test_avkryssing_ikke_aktuelt_felt_og_egne_punkter(con):
    mid, _ = _mal(con, "Mal", ("Oppgave", MND), ("Lim inn", dict(MND, felt=1)), ("Lenke", {"type": "husk"}))
    pid = _plan(con, "Kurs", _samling(date(2027, 1, 18)))
    sjekklister.sett_mal(con, pid, mid, "admin:test")
    (sid,) = _samlinger(con, pid)
    sjekklister.lag_for_samling(con, sid, "admin:test")
    p = _punkter(con, sid)
    assert sjekklister.sett_status(con, p["Oppgave"]["id"], "utfort", "admin:kari")
    assert not sjekklister.sett_status(con, p["Lenke"]["id"], "utfort", "admin:kari")        # husk: ingen avkryssing
    assert sjekklister.sett_felt(con, p["Lim inn"]["id"], "  https://skjema.example/1  ", "admin:kari")
    assert not sjekklister.sett_felt(con, p["Oppgave"]["id"], "x", "admin:kari")             # har ikke felt
    with pytest.raises(sjekklister.SjekklisteFeil):
        sjekklister.sett_felt(con, p["Lim inn"]["id"], "to\nlinjer", "admin:kari")
    with pytest.raises(sjekklister.SjekklisteFeil):
        sjekklister.sett_frist(con, p["Oppgave"]["id"], "31.12.2026", "admin:kari")
    eget = sjekklister.legg_til_punkt(con, sid, MultiDict({"tekst": "OBS! Rommet er booket", "frist": "2026-12-01"}), "admin:test")
    assert sjekklister.slett_punkt(con, p["Oppgave"]["id"], "admin:test")["tekst"] == "Oppgave"   # fra malen: tas bort her
    assert _punkter(con, sid)["Oppgave"]["slettet"] == 1
    assert sjekklister.gjenopprett_punkt(con, p["Oppgave"]["id"], "admin:test") and _punkter(con, sid)["Oppgave"]["slettet"] == 0
    assert sjekklister.sett_notat(con, p["Oppgave"]["id"], " Sendt 12.10" + chr(10) + "Ref 4711 ", "admin:kari")
    assert _punkter(con, sid)["Oppgave"]["notat"] == "Sendt 12.10" + chr(10) + "Ref 4711"
    assert sjekklister.slett_punkt(con, eget, "admin:test")["tekst"] == "OBS! Rommet er booket"
    p = _punkter(con, sid)
    assert (p["Oppgave"]["status"], p["Oppgave"]["status_av"], p["Lim inn"]["felt_verdi"]) == (
        "utfort", "admin:kari", "https://skjema.example/1")
    sjekklister.sett_status(con, p["Oppgave"]["id"], "aapen", "admin:kari")
    assert _punkter(con, sid)["Oppgave"]["status_av"] is None
    con.commit()
    detaljer = " ".join(r[0] for r in con.execute("SELECT detaljer FROM hendelse WHERE handling LIKE 'sjekkliste%'"))
    for fritekst in ("Oppgave", "skjema.example", "OBS", "Rommet", "Lenke", "Sendt", "4711"):
        assert fritekst not in detaljer                                                   # hendelsesloggen: ingen fritekst


def test_tilstand_grupper_og_tellere(con):
    mid, _ = _mal(con, "Mal", ("Måned før", MND), ("Uke før", dict(MND, frist_enhet="uker")),
                  ("To dager før", dict(MND, frist_antall=2, frist_enhet="virkedager")),
                  ("Etter", dict(MND, frist_antall=3, frist_enhet="virkedager", frist_retning="etter", frist_fra="slutt")),
                  ("Uten frist", {}), ("Husk", {"type": "husk"}), ("Ikke aktuelt", MND))
    pid = _plan(con, "Kurs", _samling(date(2027, 1, 18), date(2027, 1, 21)))
    sjekklister.sett_mal(con, pid, mid, "admin:test")
    (sid,) = _samlinger(con, pid)
    sjekklister.lag_for_samling(con, sid, "admin:test")
    sjekklister.sett_status(con, _punkter(con, sid)["Ikke aktuelt"]["id"], "ikke_aktuelt", "admin:test")
    sk = sjekklister.for_samlinger(con, [sid], date(2027, 1, 5))[sid]
    tilstand = {p.tekst: p.tilstand for p in sk.punkter}
    assert tilstand == {"Måned før": "forfalt", "Uke før": "snart", "To dager før": "senere", "Etter": "senere",
                        "Uten frist": "uten_frist", "Husk": "husk", "Ikke aktuelt": "ikke_aktuelt"}
    assert [(f, [p.tekst for p in punkter]) for f, _, punkter in sk.grupper] == [
        ("forfalt", ["Måned før"]), ("maaned", ["Ikke aktuelt"]), ("uker", ["Uke før"]), ("dager", ["To dager før"]),
        ("etter", ["Etter"]), ("uten", ["Uten frist"])]                    # forfalte øverst, så etter når fristen er
    assert (sk.antall, sk.ferdige, sk.forfalt, sk.snart, sk.neste_frist) == (5, 0, 1, 1, date(2027, 1, 11))
    assert [p.tekst for p in sk.husk] == ["Husk"]
    assert sjekklister.forfalte_samlinger(con, date(2027, 1, 5)) == {sid}
    assert sjekklister.forfalte_samlinger(con, date(2026, 12, 1)) == set()


def test_underpunkt_i_en_annen_fase_enn_hovedpunktet_faar_hovedpunktet_graatt_over_seg(con):
    uke, to_dager = dict(MND, frist_enhet="uker"), dict(MND, frist_antall=2, frist_enhet="virkedager")
    mid, _ = _mal(con, "Mal", ("Veiledningsdagene", uke), ("Dele de i grupper", dict(uke, nivaa=1)),
                  ("Gjelder Oslo: rigge rommene", dict(to_dager, nivaa=1)),
                  ("Informer [TOMA](https://intra.example/toma)", uke), ("Bestille lunsj", dict(to_dager, nivaa=1)))
    pid = _plan(con, "Kurs", _samling(date(2027, 1, 18), date(2027, 1, 21), sted="Oslo"))
    sjekklister.sett_mal(con, pid, mid, "admin:test")
    (sid,) = _samlinger(con, pid)
    sjekklister.lag_for_samling(con, sid, "admin:test")
    sk = sjekklister.for_samlinger(con, [sid], date(2026, 12, 1))[sid]
    vist = {f: [(p.tekst, p.innrykk, p.forelder_kort) for p in punkter] for f, _, punkter in sk.grupper}
    assert vist["uker"] == [("Veiledningsdagene", False, ""), ("Dele de i grupper", True, ""),      # rett under: innrykk
                            ("Informer [TOMA](https://intra.example/toma)", False, "")]
    assert vist["dager"] == [("Gjelder Oslo: rigge rommene", True, "Veiledningsdagene"),    # hovedpunktet grått over
                             ("Bestille lunsj", True, "Informer TOMA")]                    # uten lenkesyntaks
    con.commit()
    t = _klient(dag=date(2026, 12, 1)).get(f"/admin/aktiviteter/planlagte-kurs/{pid}?apen={sid}").get_data(as_text=True)
    assert re.search(r'<li class="sk-kontekst">Veiledningsdagene</li>\s*<li class="sk-punkt sk-\w+ sk-under" '
                     r'id="skp-\d+">(?:(?!</li>).)*Gjelder Oslo: rigge rommene', t, re.S)
    assert t.count('<li class="sk-kontekst">') == 2 and '<li class="sk-kontekst">Informer TOMA</li>' in t
    assert re.search(r'class="sk-punkt sk-\w+ sk-under" id="skp-\d+">(?:(?!</li>).)*Dele de i grupper', t, re.S)
    assert sjekklister.kort_tekst("Oppstart av nytt kurs – Lage gruppene til samlingene og legg oversikten ut") == \
        "Oppstart av nytt kurs – Lage gruppene…"
    assert sjekklister.kort_tekst("Første linje\nAndre linje") == "Første linje"
    # et eget punkt er aldri hovedpunkt, heller ikke når et nytt underpunkt fra malen havner rett etter det
    sjekklister.legg_til_punkt(con, sid, MultiDict({"tekst": "Eget punkt"}), "admin:test")
    sjekklister.opprett_malpunkt(con, mid, sjekklister.valider_malpunkt(MultiDict(
        {"tekst": "Under TOMA", **{k: str(v) for k, v in dict(uke, nivaa=1).items()}})), "admin:test")
    assert sjekklister.oppdater_fra_mal(con, sid, "admin:test")["lagt_til"] == 1
    sk = sjekklister.for_samlinger(con, [sid], date(2026, 12, 1))[sid]
    rekke = [p.tekst for p in sk.punkter]
    assert rekke.index("Eget punkt") == rekke.index("Under TOMA") - 1                              # rett før i rekkefølgen
    assert next(p for p in sk.punkter if p.tekst == "Under TOMA").forelder_id == \
        next(p.id for p in sk.punkter if p.tekst.startswith("Informer"))


def test_lenker_vises_trygt():
    html = str(sjekklister.tekst_html('Se [TOMA](https://intra.example/toma?a=1&b=2) og <b>[x](javascript:alert(1))</b> '
                                      '[y](https://a.example/"onclick=x")'))
    assert '<a href="https://intra.example/toma?a=1&amp;b=2" target="_blank" rel="noopener noreferrer">TOMA</a>' in html
    assert re.findall(r'href="([^"]*)"', html) == ["https://intra.example/toma?a=1&amp;b=2"]   # bare http(s), uten anførselstegn
    assert "&lt;b&gt;[x](javascript:alert(1))&lt;/b&gt;" in html                            # står som tekst
    assert str(sjekklister.tekst_html("Bare <tekst>")) == "Bare &lt;tekst&gt;"


# ============================ sidene ============================

def _kurs_med_sjekkliste(con, fra=FRAM, **kurs):
    mid, _ = _mal(con, "Testmal", ("Send info til kursholder", MND), ("Booke hotell", dict(MND, gjelder_sted="fysisk")),
                  ("Registreringsskjema", dict(MND, felt=1)), ("Husk vasking", {"type": "husk"}))
    pid = _plan(con, "Sjekkurs", _samling(fra, fra + timedelta(days=2), sted="Oslo"), **kurs)
    sjekklister.sett_mal(con, pid, mid, "admin:test")
    sjekklister.lag_for_kurs(con, pid, "admin:test")
    con.commit()
    return pid, _samlinger(con, pid)[0], mid


def test_kalenderen_har_sjekklisten_som_nedtrekk_og_avkryssing_gaar_tilbake_dit(con):
    pid, sid, _ = _kurs_med_sjekkliste(con)
    k = _klient()
    t = k.get("/admin/aktiviteter/kalender").get_data(as_text=True)
    # pila til høyre på raden (uten JavaScript en lenke som åpner sjekklisten), status på raden, sjekklisten skjult under
    assert re.search(rf'<article class="oversiktsrad [^"]*har-sjekkliste"[^>]*data-sjekkrad>\s*<div class="odato" id="sk-{sid}">', t)
    assert re.search(rf'<a class="opil" href="/admin/aktiviteter/kalender\?apen={sid}#sk-{sid}" aria-controls="skpanel-{sid}"\s+'
                     r'aria-expanded="false" data-sjekk-veksle>', t)
    assert f'<div class="osjekk" id="skpanel-{sid}" hidden>' in t and "0/3 ferdig" in t and "Husk vasking" in t
    assert 'data-send-ved-endring' in t and "Mer på kursets side" in t
    p = _punkter(con, sid)["Send info til kursholder"]
    r = k.post(f"/admin/aktiviteter/sjekkliste/{p['id']}", data={"handling": "kryss", "utfort": "1",
                                                                  "neste": "/admin/aktiviteter/kalender?sted=oslo"})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/admin/aktiviteter/kalender?sted=oslo&apen={sid}#sk-{sid}")
    assert _punkter(con, sid)["Send info til kursholder"]["status"] == "utfort"
    etter = k.get(f"/admin/aktiviteter/kalender?sted=oslo&apen={sid}").get_data(as_text=True)
    assert f'<div class="osjekk" id="skpanel-{sid}">' in etter and "1/3 ferdig" in etter and 'aria-expanded="true"' in etter
    assert f'href="/admin/aktiviteter/kalender?sted=oslo#sk-{sid}"' in etter              # pila lukker igjen, tilbake til raden
    farlig = k.post(f"/admin/aktiviteter/sjekkliste/{p['id']}", data={"handling": "kryss", "neste": "https://ond.example/"})
    assert farlig.headers["Location"].endswith(f"/admin/aktiviteter/planlagte-kurs/{pid}?apen={sid}#sk-{sid}")
    assert _punkter(con, sid)["Send info til kursholder"]["status"] == "aapen"            # uten «utfort»: ikke gjort


def test_kursoversikten_har_pil_paa_planlagte_samlinger_ogsaa_uten_sjekkliste(con):
    mid, _ = _mal(con, "Mal", ("Info", MND))
    uten_liste = _plan(con, "Uten liste", _samling(FRAM, sted="Oslo"))
    sjekklister.sett_mal(con, uten_liste, mid, "admin:test")                # mal, men sjekklisten er ikke laget
    uten_mal = _plan(con, "Uten mal", _samling(FRAM + timedelta(days=3), sted="Oslo"))
    ekstern = _plan(con, "IPRO-kurs", _samling(FRAM + timedelta(days=5), sted="Oslo"), ekstern=1)
    con.commit()
    s1, s2, s3 = (_samlinger(con, p)[0] for p in (uten_liste, uten_mal, ekstern))
    t = _klient().get("/admin/aktiviteter/kalender").get_data(as_text=True)
    assert t.count("data-sjekkrad") == 2 and f"skpanel-{s3}" not in t                  # annen arrangør: ingen pil
    panel1 = re.search(rf'<div class="osjekk" id="skpanel-{s1}" hidden>(.*?)</div>\s*</article>', t, re.S).group(1)
    panel2 = re.search(rf'<div class="osjekk" id="skpanel-{s2}" hidden>(.*?)</div>\s*</article>', t, re.S).group(1)
    assert "Lag sjekkliste" in panel1 and "Kurset har ingen sjekklistemal" in panel2      # først synlig når raden åpnes
    assert "ikke laget ennå" not in t and "ingen mal" not in t                          # ingen status på raden (03.10)


def test_forfalte_punkter_er_roede_i_kalender_aarsplan_og_listen(con):
    pid, sid, _ = _kurs_med_sjekkliste(con, fra=date(2031, 3, 3))
    k = _klient(dag=date(2031, 2, 20))                                    # fristene (3. feb) er passert
    maaned = k.get("/admin/aktiviteter/kalender?aar=2031&maned=3").get_data(as_text=True)
    assert re.search(r'class="hendelse kursfarge-\d+ planlagt[^"]* forfalt"', maaned)
    assert '<span class="prove forfalt">&nbsp;</span> forfalt i sjekklisten' in maaned          # forklaringen
    aar = k.get("/admin/aktiviteter/aarsplan?aar=2031").get_data(as_text=True)
    assert re.search(r'class="miniblokk kursfarge-\d+ planlagt[^"]* forfalt"', aar)
    assert "3 forfalt i sjekklisten" in k.get("/admin/aktiviteter/planlagte-kurs").get_data(as_text=True)
    oversikt = k.get("/admin/aktiviteter/kalender").get_data(as_text=True)
    assert '<span class="merke feil">3 forfalt</span>' in oversikt


def test_kursets_side_har_full_sjekkliste(con):
    pid, sid, mid = _kurs_med_sjekkliste(con)
    k = _klient()
    url = f"/admin/aktiviteter/planlagte-kurs/{pid}"
    t = k.get(url).get_data(as_text=True)
    assert f'id="sk-{sid}" open' in t and "Ikke aktuelt" in t and "Legg til eget punkt" in t and "Oppdater fra malen" in t
    assert ">Testmal</a>" in t                                          # «Sjekkliste: Testmal» under Om kurset
    p = _punkter(con, sid)
    k.post(f"/admin/aktiviteter/sjekkliste/{p['Booke hotell']['id']}", data={"handling": "ikke_aktuelt", "neste": url})
    k.post(f"/admin/aktiviteter/sjekkliste/{p['Registreringsskjema']['id']}",
           data={"handling": "kryss", "felt": "https://skjema.example/9", "neste": url})
    r = k.post(f"/admin/aktiviteter/sjekkliste/{p['Send info til kursholder']['id']}",
               data={"handling": "frist", "frist": "2030-01-02", "neste": url})
    assert r.status_code == 302
    k.post(f"{url}/samling/{sid}/sjekkliste", data={"handling": "nytt_punkt", "tekst": "OBS! Rommet ved kantinen", "neste": url})
    p = _punkter(con, sid)
    assert (p["Booke hotell"]["status"], p["Registreringsskjema"]["felt_verdi"], p["Send info til kursholder"]["frist"],
            p["Send info til kursholder"]["frist_manuell"]) == ("ikke_aktuelt", "https://skjema.example/9", "2030-01-02", 1)
    assert "OBS! Rommet ved kantinen" in p
    # slett (03.10: «slette det som ikke er aktuelt»): et punkt fra malen tas bort her og kan tas tilbake
    k.post(f"/admin/aktiviteter/sjekkliste/{p['Booke hotell']['id']}", data={"handling": "slett", "neste": url})
    t = k.get(url).get_data(as_text=True)
    assert "Slettede punkter (1)" in t and "Ta tilbake" in t and _punkter(con, sid)["Booke hotell"]["slettet"] == 1
    k.post(f"/admin/aktiviteter/sjekkliste/{p['Booke hotell']['id']}", data={"handling": "gjenopprett", "neste": url})
    assert _punkter(con, sid)["Booke hotell"]["slettet"] == 0
    assert k.post(f"{url}/samling/{sid}/sjekkliste", data={"handling": "ukjent"}).status_code == 400
    assert k.post("/admin/aktiviteter/sjekkliste/999999", data={"handling": "kryss"}).status_code == 404


def test_mal_velges_paa_kurset_og_sjekklisten_lages_for_kommende_samlinger(con):
    mid, _ = _mal(con, "Kortkurs", ("Info", MND))
    k = _klient()
    r = k.post("/admin/aktiviteter/planlagte-kurs/ny", data={
        "navn": "Med mal", "status": "planlagt", "farge": "auto", "sjekkliste_mal": str(mid),
        "s-fra_dato": FRAM.isoformat(), "s-sted": "Oslo"})
    pid = int(r.headers["Location"].rsplit("/", 1)[1])
    (sid,) = _samlinger(con, pid)
    assert sjekklister.mal_for_kurs(con, pid) == mid and list(_punkter(con, sid)) == ["Info"]
    uten = _plan(con, "Uten mal", _samling(FRAM), _samling(date.today() - timedelta(days=30)))
    r = k.post(f"/admin/aktiviteter/planlagte-kurs/{uten}", data={"navn": "Uten mal", "status": "planlagt", "farge": "1",
                                                                   "sjekkliste_mal": str(mid)}, follow_redirects=True)
    assert "Sjekkliste laget for 1 samling." in r.get_data(as_text=True)                   # bare den som ikke er over
    gammel, ny = sorted(_samlinger(con, uten), key=lambda s: s)
    assert sum(bool(_punkter(con, s)) for s in (gammel, ny)) == 1
    k.post(f"/admin/aktiviteter/planlagte-kurs/{uten}/samling/ny", data={"fra_dato": (FRAM + timedelta(days=90)).isoformat()})
    assert len([s for s in _samlinger(con, uten) if _punkter(con, s)]) == 2                 # ny samling: sjekkliste med en gang
    assert k.post("/admin/aktiviteter/planlagte-kurs/ny", data={"navn": "X", "s-fra_dato": FRAM.isoformat(),
                                                                 "sjekkliste_mal": "abc"}).status_code == 400


def test_nye_datoer_paa_siden_flytter_fristene(con):
    pid, sid, _ = _kurs_med_sjekkliste(con)
    ny_fra = FRAM + timedelta(days=21)
    r = _klient().post(f"/admin/aktiviteter/planlagte-kurs/{pid}/samling/{sid}",
                       data={"fra_dato": ny_fra.isoformat(), "sted": "Oslo"}, follow_redirects=True)
    assert "i sjekklisten er flyttet etter de nye datoene" in r.get_data(as_text=True)
    assert _punkter(con, sid)["Send info til kursholder"]["frist"] == sjekklister.regn_frist(
        ny_fra, ny_fra, -1, "maaneder", "start").isoformat()


def test_malsidene(con):
    k = _klient()
    assert "Ingen maler ennå" in k.get("/admin/aktiviteter/sjekklistemaler").get_data(as_text=True)
    r = k.post("/admin/aktiviteter/sjekklistemaler/ny", data={"navn": "EFST testmal", "beskrivelse": "Oppdiktet"})
    mid = int(r.headers["Location"].rsplit("/", 1)[1])
    url = f"/admin/aktiviteter/sjekklistemaler/{mid}"
    for tekst in ("Første punkt", "Andre punkt"):
        assert k.post(f"{url}/punkt/ny", data={"tekst": tekst, "frist_antall": "1", "frist_enhet": "maaneder",
                                               "frist_retning": "for", "frist_fra": "start"}).status_code == 302
    feil = k.post(f"{url}/punkt/ny", data={"tekst": "", "frist_antall": "x"})
    assert feil.status_code == 400 and "Teksten må fylles ut." in feil.get_data(as_text=True)
    mal = sjekklister.hent_mal(con, mid)
    a, b = [p["id"] for p in mal["punkter"]]
    assert k.post(f"{url}/punkt/{b}/flytt", data={"retning": "opp"}).status_code == 302
    assert [p["tekst"] for p in sjekklister.hent_mal(con, mid)["punkter"]] == ["Andre punkt", "Første punkt"]
    assert k.post(f"{url}/punkt/{a}", data={"tekst": "Første, endret", "gjelder": "siste", "frist_antall": "3",
                                            "frist_enhet": "virkedager", "frist_retning": "etter",
                                            "frist_fra": "slutt"}).status_code == 302
    side = k.get(url).get_data(as_text=True)
    assert "Første, endret" in side and "3 virkedager etter slutt · Bare siste samling" in side
    assert k.post(f"{url}/punkt/{b}/slett").status_code == 302
    assert k.post(f"{url}/punkt/{b}/flytt", data={"retning": "opp"}).status_code == 404
    assert "EFST testmal" in k.get("/admin/aktiviteter/sjekklistemaler").get_data(as_text=True)
    assert k.post(f"{url}/slett").status_code == 302 and sjekklister.hent_mal(con, mid) is None


def test_slettet_mal_beholder_sjekklistene(con):
    pid, sid, mid = _kurs_med_sjekkliste(con)
    sjekklister.slett_mal(con, mid, "admin:test")
    con.commit()
    assert sjekklister.mal_for_kurs(con, pid) is None and len(_punkter(con, sid)) == 4
    assert all(p["malpunkt_id"] is None for p in _punkter(con, sid).values())


def test_lesetilgang_ser_sjekklisten_men_kan_ikke_endre(con):
    pid, sid, mid = _kurs_med_sjekkliste(con)
    db.opprett_admin_bruker(con, "leser", "Leser", "passord-som-holder", rolle="lese")
    con.commit()
    k = _klient("leser", "passord-som-holder")
    t = k.get(f"/admin/aktiviteter/planlagte-kurs/{pid}").get_data(as_text=True)
    assert "Send info til kursholder" in t and "data-send-ved-endring" not in t
    p = _punkter(con, sid)["Send info til kursholder"]
    for sti, data in ((f"/admin/aktiviteter/sjekkliste/{p['id']}", {"handling": "kryss", "utfort": "1"}),
                      (f"/admin/aktiviteter/planlagte-kurs/{pid}/samling/{sid}/sjekkliste", {"handling": "lag"}),
                      ("/admin/aktiviteter/sjekklistemaler/ny", {"navn": "X"}),
                      (f"/admin/aktiviteter/sjekklistemaler/{mid}/slett", {})):
        assert k.post(sti, data=data).status_code == 403, sti
    assert _punkter(con, sid)["Send info til kursholder"]["status"] == "aapen"


# ============================ påminnelsen (steg 3) ============================

def _kurs_med_frister(con, navn="Påminnelseskurs", fra=date(2027, 1, 18)):
    """Planlagt kurs i Oslo, 18.–21. januar 2027: frist 18. des (1 måned før), 11. jan (1 uke før), 14. jan (2 virkedager
    før) og 4. feb (2 uker etter slutt), og et Husk-punkt."""
    mid, _ = _mal(con, f"Mal for {navn}", ("Måned før", MND), ("Uke før", dict(MND, frist_enhet="uker")),
                  ("To dager før", dict(MND, frist_antall=2, frist_enhet="virkedager")),
                  ("Etter", dict(MND, frist_antall=2, frist_enhet="uker", frist_retning="etter", frist_fra="slutt")),
                  ("Husk noe", {"type": "husk"}))
    pid = _plan(con, navn, _samling(fra, fra + timedelta(days=3), sted="Oslo"))
    sjekklister.sett_mal(con, pid, mid, "admin:test")
    sjekklister.lag_for_kurs(con, pid, "admin:test")
    con.commit()
    return pid, _samlinger(con, pid)[0]


def test_paaminnelsen_tar_med_forfalte_og_de_neste_7_dagene_men_ikke_gjort_eller_avlyst(con):
    pid, sid = _kurs_med_frister(con)
    avlyst, _ = _kurs_med_frister(con, "Avlyst kurs")
    con.execute("UPDATE planlagt_kurs SET status='avlyst' WHERE id=?", (avlyst,))
    p = sjekklister.paaminnelser(con, date(2027, 1, 5))                    # tirsdag
    assert (p["forfalt"], p["snart"]) == (1, 1)                             # 14. jan og 4. feb er lenger fram
    (v,) = p["samlinger"]                                                   # det avlyste kurset er ikke med
    assert (v["plan_id"], v["samling_id"], v["kursnavn"], v["samling"]) == (pid, sid, "Påminnelseskurs Oslo",
                                                                            "18.–21. januar 2027")
    assert [(x["tekst"], x["frist_tekst"]) for x in v["forfalt"]] == [("Måned før", "18. des")]
    assert [(x["tekst"], x["frist_tekst"]) for x in v["snart"]] == [("Uke før", "11. jan")]
    pk = _punkter(con, sid)
    sjekklister.sett_status(con, pk["Måned før"]["id"], "utfort", "admin:test")
    sjekklister.sett_status(con, pk["Uke før"]["id"], "ikke_aktuelt", "admin:test")
    assert sjekklister.paaminnelser(con, date(2027, 1, 5)) == {"samlinger": [], "forfalt": 0, "snart": 0}


def test_morgenjobben_sender_en_samle_epost_til_kurs_ipr_no_hver_hverdag_og_er_idempotent(con):
    pid, sid = _kurs_med_frister(con)

    def sendt():
        return [tuple(r) for r in con.execute(
            "SELECT mottaker, type, nokkel FROM utsending_logg WHERE type='sjekkliste-paaminnelse' ORDER BY nokkel")]
    daglig.kjor(Kjoring(con, idag=date(2027, 1, 9)))                        # lørdag: ingen e-post
    daglig.kjor(Kjoring(con, idag=date(2027, 1, 5), tor=True))              # tørrkjøring: ingen e-post
    assert sendt() == []
    daglig.kjor(Kjoring(con, idag=date(2027, 1, 5)))
    daglig.kjor(Kjoring(con, idag=date(2027, 1, 5)))                        # samme dag igjen: ingenting nytt
    assert sendt() == [("kurs@ipr.no", "sjekkliste-paaminnelse", "sjekkliste:2027-01-05")]
    (melding,) = [m for m in epost.les_utboks() if m["til"] == "kurs@ipr.no"]
    assert melding["emne"] == "Sjekklister: 1 forfalt · 1 med frist de neste 7 dagene"
    assert f"/admin/aktiviteter/planlagte-kurs/{pid}?apen={sid}#sk-{sid}" in melding["html"]
    daglig.kjor(Kjoring(con, idag=date(2027, 1, 6)))                        # neste hverdag: ny e-post
    assert len(sendt()) == 2
    for nr in _punkter(con, sid).values():
        sjekklister.sett_status(con, nr["id"], "utfort", "admin:test")
    con.commit()
    daglig.kjor(Kjoring(con, idag=date(2027, 1, 7)))                        # alt er gjort: ingen e-post
    assert len(sendt()) == 2


def test_epostmalen_for_paaminnelsen(con):
    pid, sid = _kurs_med_frister(con)
    p = sjekklister.paaminnelser(con, date(2027, 1, 5))
    emne, html = epost.render("sjekkliste_paaminnelse", samlinger=p["samlinger"], antall_forfalt=1, antall_snart=1)
    tekst = " ".join(html.split())
    assert emne == "Sjekklister: 1 forfalt · 1 med frist de neste 7 dagene"
    assert html.startswith("<div") and "Mine kurs" not in html                        # intern: ikke deltakerrammen
    assert "<strong>1 punkt har passert fristen</strong>, og 1 punkt har frist de neste 7 dagene.</p>" in tekst
    assert f"{config.BASE_URL}/admin/aktiviteter/planlagte-kurs/{pid}?apen={sid}#sk-{sid}" in html
    assert ">Forfalt 18. des:</span> Måned før</li>" in tekst and ">Frist 11. jan:</span> Uke før</li>" in tekst
    emne, _ = epost.render("sjekkliste_paaminnelse", samlinger=[], antall_forfalt=0, antall_snart=3)
    assert emne == "Sjekklister: 3 med frist de neste 7 dagene"
    mange = [{"plan_id": 1, "samling_id": 2, "kursnavn": "K", "samling": "S",
              "punkter": [{"forfalt": True, "frist_tekst": "1. jan", "tekst_html": f"Punkt {i}"} for i in range(10)]}]
    html = epost.render("sjekkliste_paaminnelse", samlinger=mange, antall_forfalt=10, antall_snart=0)[1]
    assert "Punkt 7" in html and "Punkt 8" not in html and "… og 2 til på kursets side" in html   # de åtte første


def test_oversikten_viser_de_fem_forste_punktene_per_samling_og_resten_bak_vis_flere(con):
    mid, _ = _mal(con, "Mange", *[(f"Punkt {i}", MND) for i in range(7)])
    pid = _plan(con, "Mangekurs", _samling(date(2027, 1, 18), sted="Oslo"))
    sjekklister.sett_mal(con, pid, mid, "admin:test")
    sjekklister.lag_for_kurs(con, pid, "admin:test")
    con.commit()
    t = _klient(dag=date(2027, 1, 5)).get("/admin").get_data(as_text=True)
    synlig, _, skjult = t.partition('<details class="flere"><summary>Vis 2 til</summary>')
    assert skjult and "Punkt 4</li>" in synlig and "Punkt 5</li>" not in synlig and "Punkt 6</li>" in skjult


def test_oversikten_har_kort_og_liste_over_sjekklistene(con):
    pid, sid = _kurs_med_frister(con)
    t = _klient(dag=date(2027, 1, 5)).get("/admin").get_data(as_text=True)
    assert "Sjekklister: forfalt" in t and "1 med frist de neste 7 dagene" in t and '<h2 id="sjekklister">' in t
    assert '<span class="merke feil">Forfalt 18. des</span> Måned før' in t
    assert '<span class="merke gul">Frist 11. jan</span> Uke før' in t
    assert f'href="/admin/aktiviteter/planlagte-kurs/{pid}?apen={sid}#sk-{sid}">Påminnelseskurs Oslo</a>' in t
    tom = _klient(dag=date(2026, 10, 1)).get("/admin").get_data(as_text=True)          # ingen frister ennå
    assert "Sjekklister: forfalt" in tom and 'id="sjekklister"' not in tom


# ============================ kurs i systemet (03.10.2026: «sjekkliste på alle kurs») ============================

def test_kurs_i_systemet_faar_sjekkliste_som_folger_kurset(con):
    mid, _ = _mal(con, sjekklister.STANDARDMAL, ("Info til deltakerne", MND))
    kid = db.opprett_kurs(con, kode="SJK", navn="Todagerskurs i Bergen", datoer=["2027-03-01", "2027-03-02", "2027-04-05"],
                          sharepoint_mappe="Kurs/SJK")
    con.commit()
    idag = date(2027, 1, 5)
    assert sjekklister.synk_kurs(con, idag) > 0
    assert sjekklister.synk_kurs(con, idag) == 0                                      # idempotent
    plan = con.execute("SELECT * FROM planlagt_kurs WHERE kurs_id=?", (kid,)).fetchone()
    assert plan["navn"] == "Todagerskurs i Bergen" and sjekklister.mal_for_kurs(con, plan["id"]) == mid
    samlinger = con.execute("SELECT * FROM planlagt_samling WHERE planlagt_kurs_id=? ORDER BY fra_dato",
                            (plan["id"],)).fetchall()
    assert [(s["fra_dato"], s["til_dato"], s["nr"]) for s in samlinger] == [("2027-03-01", "2027-03-02", 1),
                                                                          ("2027-04-05", "2027-04-05", 2)]
    assert [_punkter(con, s["id"])["Info til deltakerne"]["frist"] for s in samlinger] == ["2027-02-01", "2027-03-05"]
    assert planlagte_kurs.liste(con) == []                                            # står ikke blant de planlagte
    con.execute("UPDATE kursdag SET dato='2027-04-12' WHERE kurs_id=? AND dato='2027-04-05'", (kid,))
    assert sjekklister.synk_kurs(con, idag) == 1                                      # ny dato i kurset
    assert _punkter(con, samlinger[1]["id"])["Info til deltakerne"]["frist"] == "2027-03-12"   # fristen følger med
    con.commit()
    t = _klient(dag=idag).get("/admin/aktiviteter/kalender").get_data(as_text=True)
    assert t.count("data-sjekkrad") == 2 and 'title="Fra kursplanen' not in t          # kursets rader, ingen ekstra rad
    assert f'id="skpanel-{samlinger[0]["id"]}" hidden' in t and "0/1 ferdig" in t
    assert t.count('<span class="opaameldte">0 påmeldte</span>') == 2                       # påmeldte står fortsatt
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kid,))
    sjekklister.synk_kurs(con, idag)
    assert con.execute("SELECT status FROM planlagt_kurs WHERE kurs_id=?", (kid,)).fetchone()[0] == "avlyst"
    assert sjekklister.paaminnelser(con, date(2027, 3, 1))["samlinger"] == []         # avlyst: ingen påminnelse


def test_malen_til_kurs_i_systemet_velges_etter_navnet(con):
    ider = {navn: _mal(con, navn)[0] for navn in ("EFST 1-årig", "Modul (M1–M3)", "EFT 1-årig", "EFT påbygg",
                                                   sjekklister.STANDARDMAL, "Enkel")}
    for kursnavn, mal in (("EFT spesialistutdanning – samling 2", "Modul (M1–M3)"), ("Modul 3 (kull 13)", "Modul (M1–M3)"),
                          ("EFST videreutdanning for terapeuter", "EFST 1-årig"), ("EFT 1-årig Oslo", "EFT 1-årig"),
                          ("EFT påbygging", "EFT påbygg"), ("Parterapi i praksis – digitalt", sjekklister.STANDARDMAL),
                          ("Når angsten styrer familien", "Enkel")):
        assert sjekklister.gjett_mal(con, kursnavn) == ider[mal], kursnavn
