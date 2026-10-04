"""Kursoversikten øverst i Rapporter (Camilla 04.10.2026): påmeldte og inntekter per kurs og til sammen. Alt er oppdiktet."""
import pytest

from kurs import config, db, kursokonomi


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _kurs(con, kode, dato, pris, **kw):
    return db.opprett_kurs(con, kode=kode, navn=f"Kurs {kode}", datoer=[dato], sharepoint_mappe=f"Kurs/{kode}",
                           pris_nok=pris, type="fysisk", sted="Oslo", **kw)


def _meld(con, kid, n, start=0):
    for i in range(start, start + n):
        db.meld_paa(con, kid, epost=f"d{kid}-{i}@example.no", fornavn=f"D{i}", etternavn="Test")


def test_inntekt_er_pris_ganger_paameldte_og_summeres(con):
    a = _kurs(con, "A", "2031-03-04", 1000)
    b = _kurs(con, "B", "2031-04-04", 2500)
    _meld(con, a, 3)
    _meld(con, b, 2)
    con.commit()
    rader, t = kursokonomi.kursoversikt(con)
    assert [(r["kursnr"], r["paameldte"], r["forventet"]) for r in rader] == [
        (r["kursnr"], 3, 3000) if r["id"] == a else (r["kursnr"], 2, 5000) for r in rader]
    assert (t.kurs, t.paameldte, t.forventet) == (2, 5, 8000)


def test_avlyst_teller_null_og_utkast_er_ikke_med(con):
    a = _kurs(con, "A", "2031-03-04", 1000)
    u = _kurs(con, "U", "2031-03-05", 1000)
    _meld(con, a, 2)
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (a,))
    con.execute("UPDATE kurs SET status='utkast' WHERE id=?", (u,))
    con.commit()
    rader, t = kursokonomi.kursoversikt(con)
    assert [r["id"] for r in rader] == [a] and rader[0]["forventet"] == 0 and t.forventet == 0


def test_perioden_gjelder_kursdatoene(con):
    _kurs(con, "A", "2031-03-04", 1000)
    b = _kurs(con, "B", "2031-09-04", 1000)
    con.commit()
    rader, _ = kursokonomi.kursoversikt(con, "2031-06-01", "2031-12-31")
    assert [r["id"] for r in rader] == [b]


def test_rapporter_viser_kursoversikten(con):
    a = _kurs(con, "A", "2031-03-04", 14500)
    _meld(con, a, 2)
    con.commit()
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    html = k.get("/admin/rapporter").get_data(as_text=True)
    assert "Kursoversikt" in html and "Kurs A" in html and "29 000 kr" in html and "14 500 kr" in html


def test_krediterte_og_feilede_fakturaer_teller_ikke_som_fakturert(con):
    a = _kurs(con, "A", "2031-03-04", 1000)
    _meld(con, a, 3)
    pider = [r[0] for r in con.execute("SELECT id FROM paamelding WHERE kurs_id=?", (a,))]
    for pid, status in zip(pider, ("sendt", "kreditert", "feil")):
        con.execute("INSERT INTO faktura (paamelding_id, belop_nok, status) VALUES (?, 1000, ?)", (pid, status))
    con.commit()
    rader, t = kursokonomi.kursoversikt(con)
    assert rader[0]["fakturert"] == 1000 and t.fakturert == 1000
