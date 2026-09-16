"""Assistenten skal aldri vise noe annet enn godkjent tekst, og skal sende alt usikkert til adm."""
import hashlib
import hmac
import json
from datetime import date

import pytest

from kurs import assistent, config, db


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    c.execute("INSERT INTO kunnskap (kategori, sporsmal, svar, godkjent) VALUES ('avmelding', 'Hva er fristen for å melde seg av?', 'GODKJENT AVMELDING', 1)")
    c.execute("INSERT INTO kunnskap (kategori, sporsmal, svar, godkjent) VALUES ('x', 'Er lunsj inkludert?', 'UTKAST LUNSJ', 0)")
    c.execute("INSERT INTO kunnskap (kategori, sporsmal, svar, godkjent, gyldig_til) VALUES ('x', 'Hvor kan jeg parkere?', 'UTLØPT PARKERING', 1, '2020-01-01')")
    c.commit()
    return c


def test_godkjent_svar_vises_ordrett(con):
    s = assistent.svar(con, "Hva er fristen for å melde seg av kurset?")
    assert s.besvart and s.tekster == ["GODKJENT AVMELDING"]


def test_utkast_og_utlopt_brukes_aldri(con):
    for q in ("Er lunsj inkludert?", "Hvor kan jeg parkere?"):
        s = assistent.svar(con, q, idag=date(2027, 1, 1))
        assert not s.besvart


def test_sensitivt_gaar_til_adm_uten_ki(con):
    kalt = []
    s = assistent.svar(con, "Jeg vil ha refusjon", velger=lambda *a: kalt.append(1))
    assert not s.besvart and not kalt
    assert con.execute("SELECT status FROM henvendelse WHERE id=?", (s.henvendelse_id,)).fetchone()[0] == "til_adm"


def test_ki_kan_ikke_finne_paa_kilder(con):
    s = assistent.svar(con, "Hva er fristen?", velger=lambda q, k: {"kan_besvares": True, "krever_menneske": False, "kilder": ["K999"], "grunn": ""})
    assert not s.besvart


def test_ki_feil_gir_til_adm(con):
    def boom(q, k):
        raise TimeoutError
    assert not assistent.svar(con, "Hva er fristen?", velger=boom).besvart


def test_fakta_fra_databasen(con):
    kid = db.opprett_kurs(con, kode="F1", navn="Faktakurs", datoer=["2027-02-01"], type="fysisk", sted="Bergen")
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    did = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pid,)).fetchone()[0]
    s = assistent.svar(con, "Hvor holdes kurset?", deltaker_id=did, idag=date(2027, 1, 1))
    assert s.besvart and any("Bergen" in t for t in s.tekster)


def _klient(con):
    from kurs.web import app as webapp
    return webapp.app.test_client()


def test_webhook_signert(con):
    db.opprett_kurs(con, kode="WH1", navn="Webhook", datoer=["2027-02-01"], pris_nok=0)
    con.commit()
    body = json.dumps({"your-name": "Nett Skjema", "your-email": "nett@x.no", "kurskode": "wh1"}).encode()
    sig = hmac.new(config.WEBHOOK_HEMMELIG.encode(), body, hashlib.sha256).hexdigest()
    r = _klient(con).post("/api/paamelding", data=body, content_type="application/json", headers={"X-IPR-Signatur": sig})
    assert r.status_code == 201, r.get_json()
    r2 = _klient(con).post("/api/paamelding", data=body, content_type="application/json", headers={"X-IPR-Signatur": sig})
    assert r2.status_code == 200  # idempotent


def test_webhook_avviser_uten_signatur(con):
    r = _klient(con).post("/api/paamelding", json={"navn": "X", "epost": "x@x.no", "kurs": "WH1"})
    assert r.status_code == 401
