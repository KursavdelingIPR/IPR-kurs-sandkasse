"""Kalenderfil med kursdagene i bekreftelsen (Camilla 05.10.2026). Oppdiktede data, ingen nettverk."""
from datetime import datetime, timezone

import pytest

from kurs import config, db, kalender, sveiper
from kurs.kjoring import Kjoring


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _kurs(con, **kw):
    kid = db.opprett_kurs(con, kode="KAL1", navn="Kurs i kalender, del 1; grunnkurs", datoer=["2099-03-02", "2099-03-03"],
                          sharepoint_mappe="K/KAL1", pris_nok=0, sted="IPR, Bergen", **{"type": "fysisk", **kw})
    return con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()


def test_en_hendelse_per_kursdag_med_norsk_tid(con):
    kurs = _kurs(con)
    ics = kalender.lag_ics(kurs, db.kursdager(con, kurs["id"]), naa=datetime(2099, 1, 1, tzinfo=timezone.utc)).decode()
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert ics.count("BEGIN:VEVENT") == 2 and "METHOD:PUBLISH" in ics and "METHOD:REQUEST" not in ics
    assert f"DTSTART;TZID=Europe/Oslo:20990302T{kurs['start_kl'].replace(':', '')}00" in ics
    assert f"UID:ipr-kurs-{kurs['id']}-20990303@ipr.no" in ics
    assert r"SUMMARY:Kurs i kalender\, del 1\; grunnkurs" in ics and r"LOCATION:IPR\, Bergen" in ics
    assert all(len(l.encode()) <= 75 for l in ics.split("\r\n"))


def test_digitalt_kurs_har_ingen_zoom_lenke(con):
    kurs = _kurs(con, type="digital", zoom_url="https://zoom.us/j/123")
    ics = kalender.lag_ics(kurs, db.kursdager(con, kurs["id"])).decode()
    assert "zoom.us" not in ics and "LOCATION:Zoom (lenken kommer på e-post før kurset)" in ics


def test_bekreftelsen_har_kalenderfilen_vedlagt(con):
    kurs = _kurs(con)
    db.meld_paa(con, kurs["id"], epost="kari@eksempel.no", fornavn="Kari", etternavn="Test")
    con.commit()
    sveiper.kjor(Kjoring(con))
    con.commit()
    rad = con.execute("""SELECT v.filnavn FROM sendt_epost se JOIN sendt_epost_vedlegg v ON v.sendt_epost_id=se.id
                         WHERE se.type='bekreftelse' AND v.innebygd_cid IS NULL""").fetchall()
    assert [r["filnavn"] for r in rad] == ["kursdager.ics"]


def test_ingen_fil_uten_kursdager(con):
    kurs = _kurs(con)
    assert kalender.vedlegg(kurs, []) == []
