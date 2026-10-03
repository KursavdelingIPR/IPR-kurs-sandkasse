"""«Send e-post» i deltakervinduet (Camilla 03.10.2026: «Vil ikke hoppe til en annen side. Blir litt forvirrende.»):
fra fanen E-poster skrives, forhåndsvises og sendes e-posten uten å forlate vinduet, og etterpå står man i E-poster igjen.
Alle deltakere her er oppdiktet."""
import re

import pytest

from kurs import config, db


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _klient(brukernavn=None, passord=None):
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": brukernavn or config.ADMIN_BRUKERNAVN,
                                    "passord": passord or config.ADMIN_PASSORD})
    return k


def _kurs_med_deltaker(con):
    kid = db.opprett_kurs(con, kode="VIND", navn="Vinduskurs", datoer=["2031-03-04"], sharepoint_mappe="Kurs/VIND",
                          pris_nok=0, type="fysisk", sted="Oslo")
    pid, _ = db.meld_paa(con, kid, epost="tove@example.no", fornavn="Tove", etternavn="Test")
    con.commit()
    return kid, pid


def _innhold(html: str) -> str:
    """Det som vises i vinduet (static/app.js henter #deltaker-innhold)."""
    assert 'id="deltaker-innhold"' in html
    return html.split('id="deltaker-innhold"', 1)[1]


def test_fanen_e_poster_aapner_skjemaet_i_vinduet(con):
    kid, pid = _kurs_med_deltaker(con)
    k = _klient()
    fane = k.get(f"/admin/kurs/{kid}/deltaker/{pid}/kommunikasjon").get_data(as_text=True)
    assert f'href="/admin/kurs/{kid}/deltaker/{pid}/epost/ny" data-i-vindu>Send e-post til Tove Test</a>' in fane
    skjema = _innhold(k.get(f"/admin/kurs/{kid}/deltaker/{pid}/epost/ny").get_data(as_text=True))
    assert f'action="/admin/kurs/{kid}/epost/forhandsvis"' in skjema and f'name="i_vindu" value="{pid}"' in skjema
    assert 'name="emne"' in skjema and 'name="tekst"' in skjema and 'name="signatur_valg"' in skjema
    assert "{fornavn}" in skjema                                                    # flettefeltene


def test_forhandsvis_og_send_i_vinduet_og_tilbake_til_e_poster(con):
    kid, pid = _kurs_med_deltaker(con)
    k = _klient()
    svar = k.post(f"/admin/kurs/{kid}/epost/forhandsvis", data={
        "paamelding_id": pid, "i_vindu": pid, "emne": "Velkommen", "tekst": "Hei {fornavn},\n\nVelkommen til kurset."})
    vist = _innhold(svar.get_data(as_text=True))
    assert "Slik blir e-posten" in vist and "Hei Tove" in vist
    utsending = re.search(r'name="utsending_id" value="(\d+)"', vist).group(1)
    assert f'name="i_vindu" value="{pid}"' in vist and "Send til Tove Test" in vist
    r = k.post(f"/admin/kurs/{kid}/epost/send", data={"utsending_id": utsending, "i_vindu": pid})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/admin/kurs/{kid}/deltaker/{pid}/kommunikasjon")
    etter = k.get(r.headers["Location"]).get_data(as_text=True)
    assert "Velkommen" in _innhold(etter) and "E-post sendt til 1 mottaker(e)." in etter


def test_feil_i_skjemaet_vises_i_vinduet(con):
    kid, pid = _kurs_med_deltaker(con)
    svar = _klient().post(f"/admin/kurs/{kid}/epost/forhandsvis", data={"paamelding_id": pid, "i_vindu": pid,
                                                                       "emne": "", "tekst": "Noe"})
    html = svar.get_data(as_text=True)
    assert "Fyll inn et emne." in html and f'name="i_vindu" value="{pid}"' in _innhold(html)


def test_vinduet_godtar_bare_en_deltaker_i_kurset(con):
    kid, pid = _kurs_med_deltaker(con)
    annet = db.opprett_kurs(con, kode="ANNET", navn="Annet kurs", datoer=["2031-05-04"], sharepoint_mappe="Kurs/ANNET",
                            pris_nok=0, type="fysisk", sted="Oslo")
    con.commit()
    k = _klient()
    assert k.get(f"/admin/kurs/{annet}/deltaker/{pid}/epost/ny").status_code == 404
    r = k.post(f"/admin/kurs/{kid}/epost/forhandsvis", data={"paamelding_id": pid, "i_vindu": 999999,
                                                            "emne": "Hei", "tekst": "Hei"})
    assert r.status_code == 404
    assert k.post(f"/admin/kurs/{annet}/epost/send", data={"utsending_id": 1, "i_vindu": pid}).status_code == 404


def test_bare_deltakeren_i_vinduet_faar_e_posten(con):
    """Selv om skjemaet skulle ha flere paamelding_id, sendes e-posten bare til påmeldingen i vinduet."""
    kid, pid = _kurs_med_deltaker(con)
    annen, _ = db.meld_paa(con, kid, epost="ola@example.no", fornavn="Ola", etternavn="Test")
    con.commit()
    vist = _klient().post(f"/admin/kurs/{kid}/epost/forhandsvis", data={
        "paamelding_id": [pid, annen], "i_vindu": pid, "emne": "Hei", "tekst": "Hei {fornavn}"}).get_data(as_text=True)
    utsending = int(re.search(r'name="utsending_id" value="(\d+)"', vist).group(1))
    assert [m["id"] for m in db.admin_utsending_mottakere(con, utsending)] == [pid]


def test_lesetilgang_ser_ikke_knappen(con):
    kid, pid = _kurs_med_deltaker(con)
    db.opprett_admin_bruker(con, "leser", "Leser", "passord-som-holder", rolle="lese")
    con.commit()
    fane = _klient("leser", "passord-som-holder").get(f"/admin/kurs/{kid}/deltaker/{pid}/kommunikasjon").get_data(as_text=True)
    assert "Send e-post til" not in fane
