"""«Mine e-poster» på Min side (Camilla 15.10.2026): deltakeren ser e-postene vi har sendt, nyeste først, og kan lese dem slik de ble
sendt, med vedlegg. Bare sendte e-poster for egne påmeldinger; innholdet bare innlogget med e-post (ikke med den personlige lenken alene).
Bare oppdiktede data."""
import pytest

from kurs import db, minside
from kurs.integrasjoner import epost
from kurs.kjoring import Kjoring

from kurssidehjelp import IDAG, deltaker_klient, dokument, fast_dato, lag_deltaker, lag_kurs, ny_database, skriv_side, tekstblokk


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    fast_dato(monkeypatch)
    monkeypatch.setattr(epost, "send", lambda *a, **kw: None)
    yield c
    c.close()


def _send(con, pid, kid, emne, tekst, nokkel, vedlegg=()):
    k = Kjoring(con, idag=IDAG, aktor="admin:test")
    til = con.execute("SELECT d.epost FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE p.id=?", (pid,)).fetchone()[0]
    assert k.send_ferdigrendret_en_gang(nokkel, til, "admin_epost", emne, f"<html><body><p>{tekst}</p></body></html>",
                                        paamelding_id=pid, kurs_id=kid, vedlegg=vedlegg)


def _klient(did):
    k = deltaker_klient(did)
    with k.session_transaction() as s:
        s["deltaker_niva"] = minside.NIVAA_EPOST            # innlogget med e-post
    return k


@pytest.fixture
def oppsett(con):
    kid = lag_kurs(con, "K1")
    did, pid = lag_deltaker(con, kid)
    _send(con, pid, kid, "Velkommen til kurset", "Vi gleder oss til å se deg i Oslo.", "adhoc:1:a")
    _send(con, pid, kid, "Program for dag 2", "Her er programmet.", "adhoc:1:b",
          vedlegg=[epost.Vedlegg("program.pdf", "application/pdf", b"%PDF-1.4 test")])
    andre_did, andre_pid = lag_deltaker(con, kid, epost="ola@example.no", fornavn="Ola")
    _send(con, andre_pid, kid, "Til Ola", "Bare for Ola.", "adhoc:1:c")
    con.commit()
    return kid, did, pid, andre_did


def test_listen_viser_egne_eposter_nyeste_foerst_med_utdrag_og_vedlegg(con, oppsett):
    kid, did, pid, _ = oppsett
    html = _klient(did).get("/min-side/e-poster").get_data(as_text=True)
    assert "<h1>Mine e-poster</h1>" in html and "Til Ola" not in html
    assert html.index("Program for dag 2") < html.index("Velkommen til kurset")
    assert "Vi gleder oss til å se deg i Oslo." in html and "1 vedlegg" in html


def test_e_posten_vises_slik_den_ble_sendt_og_vedlegget_kan_hentes(con, oppsett):
    kid, did, pid, _ = oppsett
    eid = con.execute("SELECT id FROM sendt_epost WHERE emne='Program for dag 2'").fetchone()[0]
    k = _klient(did)
    side = k.get(f"/min-side/e-poster/{eid}").get_data(as_text=True)
    assert '<h1 id="ms-ep-emne">Program for dag 2</h1>' in side and "<iframe" in side and "sandbox" in side and "program.pdf" in side
    r = k.get(f"/min-side/e-poster/{eid}/vedlegg/1")
    assert r.status_code == 200 and r.data.startswith(b"%PDF") and "sandbox" in r.headers["Content-Security-Policy"]


def test_andres_eposter_og_usendte_gir_404(con, oppsett):
    kid, did, pid, andre_did = oppsett
    olas = con.execute("SELECT id FROM sendt_epost WHERE emne='Til Ola'").fetchone()[0]
    k = _klient(did)
    assert k.get(f"/min-side/e-poster/{olas}").status_code == 404
    assert k.get(f"/min-side/e-poster/{olas}/vedlegg/1").status_code == 404
    egen = con.execute("SELECT id FROM sendt_epost WHERE emne='Velkommen til kurset'").fetchone()[0]
    con.execute("UPDATE sendt_epost SET status='ukjent' WHERE id=?", (egen,))
    con.commit()
    assert k.get(f"/min-side/e-poster/{egen}").status_code == 404
    assert "Velkommen til kurset" not in k.get("/min-side/e-poster").get_data(as_text=True)


def test_med_personlig_lenke_vises_bare_emnet_til_e_posten_er_bekreftet(con, oppsett):
    kid, did, pid, _ = oppsett
    eid = con.execute("SELECT id FROM sendt_epost WHERE emne='Program for dag 2'").fetchone()[0]
    from kurs import config
    k = deltaker_klient()
    assert k.get(minside.url(con, pid).removeprefix(config.BASE_URL)).status_code == 302      # den personlige lenken: nivå «lenke»
    liste = k.get("/min-side/e-poster").get_data(as_text=True)
    assert "Program for dag 2" in liste and "Her er programmet." not in liste and "Send meg en lenke på e-post" in liste
    side = k.get(f"/min-side/e-poster/{eid}").get_data(as_text=True)
    assert "Program for dag 2" in side and "<iframe" not in side and "Send meg en lenke på e-post" in side
    assert k.get(f"/min-side/e-poster/{eid}/vedlegg/1").status_code == 404


def test_lenkene_paa_mine_kurs_og_kurssiden(con, oppsett):
    kid, did, pid, _ = oppsett
    skriv_side(con, kid, dokument(tekstblokk()))
    k = _klient(did)
    assert 'href="/min-side/e-poster"><strong>E-poster fra oss</strong>' in k.get("/min-side").get_data(as_text=True)
    kursside = k.get("/kurs/K1/deltakerside").get_data(as_text=True)
    assert 'class="dp-epostknapp" href="/min-side/e-poster?kurs=K1"' in kursside and "Mine e-poster (2)" in kursside


def test_uten_innlogging_sendes_man_til_innloggingen(con, oppsett):
    from kurs.web import app as webapp
    r = webapp.app.test_client().get("/min-side/e-poster")
    assert r.status_code == 302 and "logg-inn" in r.headers["Location"]
