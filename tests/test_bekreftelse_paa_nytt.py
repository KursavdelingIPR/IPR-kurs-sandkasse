"""«Send påmeldingsbekreftelse» i deltakervinduet (Camilla 14.10.2026), i fanen E-poster ved siden av «Send e-post» (15.10.2026):
den samme bekreftelsen sendes på nytt, uten ny faktura."""
import re
from datetime import date, timedelta

import pytest

from kurs import config, daglig, db, sveiper
from kurs.integrasjoner import epost
from kurs.kjoring import Kjoring

IDAG = date(2027, 1, 10)


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    return c


@pytest.fixture
def sendt(monkeypatch):
    ut = []
    monkeypatch.setattr(epost, "send", lambda til, emne, html, *a, **kw: ut.append((til, emne, kw.get("vedlegg") or (a[0] if a else ()))))
    from kurs.web import app as webapp
    monkeypatch.setattr(webapp, "_idag", lambda: IDAG)
    return ut


def _klient():
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def _oppsett(con):
    kid = db.opprett_kurs(con, kode="B1", navn="Testkurs", datoer=[(IDAG + timedelta(days=20)).isoformat()],
                          sharepoint_mappe="Kurs/B1", pris_nok=1000, fakturering="person", betaling="samlet")
    con.commit()
    pid, _ = db.meld_paa(con, kid, epost="kari@example.no", fornavn="Kari", etternavn="Test")
    con.commit()
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)
    con.commit()
    return kid, pid


def _fakturaer(pid):
    return db.koble(config.DB_STI).execute("SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", (pid,)).fetchone()[0]


def _nokkel(side):
    return re.search(r'name="skjemanokkel" value="([0-9a-f]{16})"', side).group(1)


def test_bekreftelsen_sendes_paa_nytt_uten_ny_faktura_og_dobbeltklikk_sender_en_gang(con, sendt):
    kid, pid = _oppsett(con)
    bekreftelser = [s for s in sendt if s[1].startswith("Bekreftelse")]
    assert len(bekreftelser) == 1 and _fakturaer(pid) == 1
    klient = _klient()
    side = klient.get(f"/admin/kurs/{kid}/deltaker/{pid}/kommunikasjon").get_data(as_text=True)
    assert '<button class="vinduknapp">Send påmeldingsbekreftelse</button>' in side and "på nytt? Det lages ingen ny faktura." in side
    assert 'class="knapp vinduknapp"' in side                                        # «Send e-post» med samme stil
    nokkel = _nokkel(side)
    url = f"/admin/kurs/{kid}/deltaker/{pid}/bekreftelse"
    svar = klient.post(url, data={"skjemanokkel": nokkel}, follow_redirects=True).get_data(as_text=True)
    assert "Påmeldingsbekreftelsen er sendt til Kari Test (kari@example.no)" in svar and "Send påmeldingsbekreftelse" in svar   # tilbake i E-poster
    nye = [s for s in sendt if s[1].startswith("Bekreftelse")]
    assert len(nye) == 2 and nye[1][0] == "kari@example.no" and nye[1][1] == nye[0][1]      # samme e-post, samme emne
    assert any(getattr(v, "filnavn", "").endswith(".ics") for v in nye[1][2])               # kalenderfilen følger med
    klient.post(url, data={"skjemanokkel": nokkel})                                          # dobbeltklikk: ingen ny e-post
    assert len([s for s in sendt if s[1].startswith("Bekreftelse")]) == 2
    klient.post(url, data={"skjemanokkel": _nokkel(klient.get(f"/admin/kurs/{kid}/deltaker/{pid}/kommunikasjon").get_data(as_text=True))})
    assert len([s for s in sendt if s[1].startswith("Bekreftelse")]) == 3                    # nytt trykk senere sender igjen
    daglig.kjor(Kjoring(con, idag=IDAG))
    assert _fakturaer(pid) == 1 and len([s for s in sendt if s[1].startswith("Bekreftelse")]) == 3
    logger = klient.get(f"/admin/kurs/{kid}/deltaker/{pid}/logger").get_data(as_text=True)
    assert "Påmeldingsbekreftelsen ble sendt på nytt" in logger
    eposter = klient.get(f"/admin/kurs/{kid}/deltaker/{pid}/kommunikasjon").get_data(as_text=True)
    assert eposter.count("Bekreftelse") >= 3


def test_ingen_knapp_og_ingen_sending_for_venteliste_eller_avmeldt(con, sendt):
    kid, pid = _oppsett(con)
    klient = _klient()
    nokkel = _nokkel(klient.get(f"/admin/kurs/{kid}/deltaker/{pid}/kommunikasjon").get_data(as_text=True))
    klient.post(f"/admin/meld-av/{pid}")
    db.koble(config.DB_STI).execute("UPDATE paamelding SET status='avmeldt' WHERE id=?", (pid,)).connection.commit()
    assert "Send påmeldingsbekreftelse" not in klient.get(f"/admin/kurs/{kid}/deltaker/{pid}/kommunikasjon").get_data(as_text=True)
    foer = len(sendt)
    svar = klient.post(f"/admin/kurs/{kid}/deltaker/{pid}/bekreftelse", data={"skjemanokkel": nokkel}, follow_redirects=True)
    assert "kan bare sendes til en påmeldt deltaker" in svar.get_data(as_text=True)
    assert len([s for s in sendt[foer:] if s[1].startswith("Bekreftelse")]) == 0


def test_lesetilgang_ser_ikke_knappen(con, sendt):
    kid, pid = _oppsett(con)
    klient = _klient()
    db.koble(config.DB_STI).execute("UPDATE admin_bruker SET rolle='lese'").connection.commit()
    side = klient.get(f"/admin/kurs/{kid}/deltaker/{pid}/kommunikasjon").get_data(as_text=True)
    assert "Send påmeldingsbekreftelse" not in side
