"""Tester de viktigste garantiene: ingen dobbeltsending, ingen dobbeltfakturering, venteliste, innsjekk."""
from datetime import date, timedelta

import pytest

from kurs import config, daglig, db
from kurs.kjoring import Kjoring


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    return c


def _kurs(con, start: date, **kw):
    datoer = [(start + timedelta(days=n)).isoformat() for n in range(kw.pop("dager", 2))]
    return db.opprett_kurs(con, kode=kw.pop("kode", "T1"), navn="Testkurs", datoer=datoer,
                           sharepoint_mappe="Kurs/T1", **{"pris_nok": 1000, **kw})


def _antall_mail():
    return len(list(config.UTBOKS.glob("*.html"))) if config.UTBOKS.exists() else 0


def test_daglig_er_idempotent(con):
    idag = date(2027, 3, 1)
    kid = _kurs(con, idag + timedelta(days=5))
    db.meld_paa(con, kid, epost="a@x.no", navn="A")
    daglig.kjor(Kjoring(con, idag=idag))
    etter_forste = _antall_mail()
    daglig.kjor(Kjoring(con, idag=idag))
    assert etter_forste == 2  # bekreftelse + ukefor
    assert _antall_mail() == etter_forste
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 1


def test_sen_paamelding_faar_ukefor(con):
    idag = date(2027, 3, 1)
    kid = _kurs(con, idag + timedelta(days=5))
    daglig.kjor(Kjoring(con, idag=idag))
    db.meld_paa(con, kid, epost="sen@x.no", navn="Sen")
    daglig.kjor(Kjoring(con, idag=idag + timedelta(days=1)))
    typer = {r[0] for r in con.execute("SELECT type FROM utsending_logg WHERE mottaker='sen@x.no'")}
    assert typer == {"bekreftelse", "ukefor"}


def test_tor_endrer_ingenting(con):
    idag = date(2027, 3, 1)
    kid = _kurs(con, idag + timedelta(days=1))
    db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.commit()
    daglig.kjor(Kjoring(con, idag=idag, tor=True))
    assert _antall_mail() == 0
    assert con.execute("SELECT COUNT(*) FROM utsending_logg").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 0


def test_venteliste_rykker_opp(con):
    kid = _kurs(con, date(2027, 5, 1), kapasitet=1)
    p1, s1 = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    p2, s2 = db.meld_paa(con, kid, epost="b@x.no", navn="B")
    assert (s1, s2) == ("bekreftet", "venteliste")
    assert db.meld_av(con, p1) == p2
    assert con.execute("SELECT status FROM paamelding WHERE id=?", (p2,)).fetchone()[0] == "bekreftet"


def test_dobbel_paamelding_avvises(con):
    kid = _kurs(con, date(2027, 5, 1))
    db.meld_paa(con, kid, epost="a@x.no", navn="A")
    with pytest.raises(db.Paameldingsfeil):
        db.meld_paa(con, kid, epost="A@X.no ", navn="A")


def test_oppmote_registreres_en_gang(con):
    kid = _kurs(con, date(2027, 5, 1))
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    dag = db.kursdager(con, kid)[0]
    assert db.registrer_oppmote(con, pid, dag["id"], "qr") is True
    assert db.registrer_oppmote(con, pid, dag["id"], "kode") is False


def test_kursbevis_etter_fullfort_kurs(con):
    start = date(2027, 5, 1)
    kid = _kurs(con, start, spesialistlop="EFT", timer_pr_dag=7)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    for dag in db.kursdager(con, kid):
        db.registrer_oppmote(con, pid, dag["id"], "qr")
    daglig.kjor(Kjoring(con, idag=start + timedelta(days=5)))
    assert con.execute("SELECT COUNT(*) FROM dokument WHERE type='kursbevis'").fetchone()[0] == 1


def test_delbetaling_en_faktura_per_samling(con):
    start = date(2027, 6, 1)
    kid = _kurs(con, start, dager=3, pris_nok=9001, betaling="per_samling", faktura_dager_for=14)
    db.meld_paa(con, kid, epost="a@x.no", navn="A")

    daglig.kjor(Kjoring(con, idag=start - timedelta(days=30)))  # for tidlig – ingen faktura ennå
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 0

    daglig.kjor(Kjoring(con, idag=start - timedelta(days=14)))  # kun første samling forfalt
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 1

    daglig.kjor(Kjoring(con, idag=start + timedelta(days=2)))
    daglig.kjor(Kjoring(con, idag=start + timedelta(days=2)))  # kjøres to ganger: ingen dubletter
    antall, sum_ = con.execute("SELECT COUNT(*), SUM(belop_nok) FROM faktura").fetchone()
    assert (antall, sum_) == (3, 9001)  # summen stemmer alltid med kursprisen
    assert con.execute("SELECT COUNT(DISTINCT kursdag_id) FROM faktura").fetchone()[0] == 3


def test_samlet_betaling_gir_en_faktura(con):
    start = date(2027, 6, 1)
    kid = _kurs(con, start, dager=3, pris_nok=9000)
    db.meld_paa(con, kid, epost="a@x.no", navn="A")
    daglig.kjor(Kjoring(con, idag=start - timedelta(days=30)))
    assert tuple(con.execute("SELECT COUNT(*), SUM(belop_nok) FROM faktura").fetchone()) == (1, 9000)


def test_deltakerens_valg_gjelder_bare_naar_kurset_tillater_det(con):
    valgfritt = _kurs(con, date(2027, 6, 1), kode="VALG", betaling="deltaker_velger")
    fast = _kurs(con, date(2027, 6, 1), kode="FAST")  # standard: samlet
    p1, _ = db.meld_paa(con, valgfritt, epost="a@x.no", navn="A", paamelding={"betaling": "per_samling"})
    p2, _ = db.meld_paa(con, fast, epost="b@x.no", navn="B", paamelding={"betaling": "per_samling"})
    hent = lambda pid: con.execute("SELECT betaling FROM paamelding WHERE id=?", (pid,)).fetchone()[0]  # noqa: E731
    assert hent(p1) == "per_samling"
    assert hent(p2) == "samlet"  # kurset styrer – skjemaet kan ikke overstyre


def test_webflyt_paamelding_og_kodeinnsjekk(con, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    from kurs.web import app as webapp
    kid = _kurs(con, date.today(), kode="WEB1", status="aapen")
    con.commit()
    klient = webapp.app.test_client()
    r = klient.post("/kurs/WEB1", data={"navn": "Web Test", "epost": "web@x.no", "samtykke": "on", "betaler": "person"})
    assert r.status_code == 200 and "påmeldt" in r.get_data(as_text=True)
    kode = db.kursdager(con, kid)[0]["innsjekk_kode"]
    r = klient.post("/innsjekk", data={"kode": kode.lower(), "epost": "web@x.no"})
    assert "Oppmøte er registrert" in r.get_data(as_text=True)
