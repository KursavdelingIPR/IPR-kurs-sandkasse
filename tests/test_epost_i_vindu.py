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


def _skjema(k, kid, pid) -> str:
    return _innhold(k.get(f"/admin/kurs/{kid}/deltaker/{pid}/epost/ny").get_data(as_text=True))


def _skjemanokkel(skjema: str) -> str:
    return re.search(r'name="skjemanokkel" value="([0-9a-f]{16})"', skjema).group(1)


def test_fanen_e_poster_aapner_skjemaet_i_vinduet_med_signaturen_under_teksten(con):
    """Camilla 03.10.2026: ingen forhåndsvisning, signaturen nederst i skrivefeltet, Send ved flettefeltene."""
    kid, pid = _kurs_med_deltaker(con)
    k = _klient()
    fane = k.get(f"/admin/kurs/{kid}/deltaker/{pid}/kommunikasjon").get_data(as_text=True)
    assert f'href="/admin/kurs/{kid}/deltaker/{pid}/epost/ny" data-i-vindu>Send e-post til Tove Test</a>' in fane
    assert '<a class="knapp liten" href="' in fane                                    # fylt knapp, ikke hvit
    skjema = _skjema(k, kid, pid)
    assert f'action="/admin/kurs/{kid}/deltaker/{pid}/epost/send"' in skjema and "forhandsvis" not in skjema
    assert 'name="emne"' in skjema and 'name="tekst"' in skjema and 'name="signatur_valg"' in skjema
    assert "{fornavn}" in skjema                                                       # flettefeltene
    boks = skjema.split('class="epostboks"', 1)[1].split("</div>", 2)
    assert 'name="tekst"' in boks[0] and "Kursadministrasjonen" in boks[0]             # signaturen i samme boks
    linje = skjema.split('class="epostlinje"', 1)[1]
    assert linje.index("kodeknapp") < linje.index('class="sendknapp">Send e-post</button>')


def test_send_direkte_fra_vinduet_og_tilbake_til_e_poster(con):
    kid, pid = _kurs_med_deltaker(con)
    k = _klient()
    nokkel = _skjemanokkel(_skjema(k, kid, pid))
    r = k.post(f"/admin/kurs/{kid}/deltaker/{pid}/epost/send", data={
        "skjemanokkel": nokkel, "emne": "Velkommen", "tekst": "Hei {fornavn},\n\nVelkommen til kurset."})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/admin/kurs/{kid}/deltaker/{pid}/kommunikasjon")
    etter = k.get(r.headers["Location"]).get_data(as_text=True)
    assert "Velkommen" in _innhold(etter) and "E-post sendt til 1 mottaker(e)." in etter
    sendt = list((config.UTBOKS).glob("*"))
    assert len(sendt) == 1 and "Hei Tove" in sendt[0].read_text(encoding="utf-8", errors="replace")


def test_dobbeltklikk_sender_bare_en_gang(con):
    kid, pid = _kurs_med_deltaker(con)
    k = _klient()
    data = {"skjemanokkel": _skjemanokkel(_skjema(k, kid, pid)), "emne": "Hei", "tekst": "Hei {fornavn}"}
    k.post(f"/admin/kurs/{kid}/deltaker/{pid}/epost/send", data=data)
    andre = k.post(f"/admin/kurs/{kid}/deltaker/{pid}/epost/send", data=data, follow_redirects=True)
    assert "allerede sendt" in andre.get_data(as_text=True)
    assert con.execute("SELECT COUNT(*) FROM admin_utsending").fetchone()[0] == 1
    assert len(list(config.UTBOKS.glob("*"))) == 1


def test_feil_i_skjemaet_vises_i_vinduet_og_teksten_beholdes(con):
    kid, pid = _kurs_med_deltaker(con)
    k = _klient()
    nokkel = _skjemanokkel(_skjema(k, kid, pid))
    svar = k.post(f"/admin/kurs/{kid}/deltaker/{pid}/epost/send", data={"skjemanokkel": nokkel, "emne": "",
                                                                        "tekst": "Min tekst"})
    html = svar.get_data(as_text=True)
    assert "Fyll inn et emne." in html and "Min tekst" in _innhold(html)
    assert f'name="skjemanokkel" value="{nokkel}"' in html
    assert con.execute("SELECT COUNT(*) FROM admin_utsending").fetchone()[0] == 0


def test_vinduet_godtar_bare_en_deltaker_i_kurset(con):
    kid, pid = _kurs_med_deltaker(con)
    annet = db.opprett_kurs(con, kode="ANNET", navn="Annet kurs", datoer=["2031-05-04"], sharepoint_mappe="Kurs/ANNET",
                            pris_nok=0, type="fysisk", sted="Oslo")
    annen, _ = db.meld_paa(con, kid, epost="ola@example.no", fornavn="Ola", etternavn="Test")
    con.commit()
    k = _klient()
    assert k.get(f"/admin/kurs/{annet}/deltaker/{pid}/epost/ny").status_code == 404
    data = {"skjemanokkel": "0123456789abcdef", "emne": "Hei", "tekst": "Hei"}
    assert k.post(f"/admin/kurs/{annet}/deltaker/{pid}/epost/send", data=data).status_code == 404
    assert k.post(f"/admin/kurs/{kid}/deltaker/{pid}/epost/send", data={**data, "skjemanokkel": "x"}).status_code == 400
    # en nøkkel som alt er brukt for en annen deltaker, kan ikke gjenbrukes til å sende den samme e-posten videre
    k.post(f"/admin/kurs/{kid}/deltaker/{pid}/epost/send", data=data)
    assert k.post(f"/admin/kurs/{kid}/deltaker/{annen}/epost/send", data=data).status_code == 404


def test_lesetilgang_ser_ikke_knappen(con):
    kid, pid = _kurs_med_deltaker(con)
    db.opprett_admin_bruker(con, "leser", "Leser", "passord-som-holder", rolle="lese")
    con.commit()
    fane = _klient("leser", "passord-som-holder").get(f"/admin/kurs/{kid}/deltaker/{pid}/kommunikasjon").get_data(as_text=True)
    assert "Send e-post til" not in fane


def test_signaturen_kan_redigeres_i_skjemaet_og_endringen_gjelder_bare_denne_e_posten(con):
    """Camilla 03.10.2026: «jeg kan fortsatt ikke redigere signaturen i dette bildet»."""
    kid, pid = _kurs_med_deltaker(con)
    k = _klient()
    skjema = _skjema(k, kid, pid)
    assert 'data-signaturvisning contenteditable="true"' in skjema
    assert '<input type="hidden" name="signatur_html" value="" disabled>' in skjema   # fylles av JavaScript ved sending
    foer = con.execute("SELECT innhold FROM signatur").fetchall()
    k.post(f"/admin/kurs/{kid}/deltaker/{pid}/epost/send", data={
        "skjemanokkel": _skjemanokkel(skjema), "emne": "Hei", "tekst": "Hei {fornavn}",
        "signatur_html": 'Hilsen Kari<br>Tlf. 12 34 56 78<script>alert(1)</script>'})
    lagret = con.execute("SELECT signatur_html FROM admin_utsending").fetchone()[0]
    assert lagret.startswith("Hilsen Kari<br>Tlf. 12 34 56 78") and "script" not in lagret
    sendt = list(config.UTBOKS.glob("*"))[0].read_text(encoding="utf-8", errors="replace")
    assert "Hilsen Kari" in sendt and "alert" not in sendt
    assert con.execute("SELECT innhold FROM signatur").fetchall() == foer            # biblioteket er urørt


def test_redigert_signatur_blir_staaende_etter_en_feil(con):
    kid, pid = _kurs_med_deltaker(con)
    k = _klient()
    svar = k.post(f"/admin/kurs/{kid}/deltaker/{pid}/epost/send", data={
        "skjemanokkel": _skjemanokkel(_skjema(k, kid, pid)), "emne": "", "tekst": "Hei",
        "signatur_html": "Min egen hilsen"})
    vist = _innhold(svar.get_data(as_text=True))
    assert "Fyll inn et emne." in svar.get_data(as_text=True)
    assert 'contenteditable="true"' in vist and vist.split("data-signaturvisning", 1)[1].split("</div>")[0].endswith(
        "Min egen hilsen")


def test_tom_signatur_sendes_uten_signatur(con):
    kid, pid = _kurs_med_deltaker(con)
    k = _klient()
    k.post(f"/admin/kurs/{kid}/deltaker/{pid}/epost/send", data={
        "skjemanokkel": _skjemanokkel(_skjema(k, kid, pid)), "emne": "Hei", "tekst": "Hei {fornavn}",
        "signatur_html": ""})
    assert con.execute("SELECT signatur_html FROM admin_utsending").fetchone()[0] == ""
