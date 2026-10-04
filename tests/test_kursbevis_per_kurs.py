"""Fanen Kursbevis (Camilla 04.10.2026): kursbeviset kan redigeres per kurs (tekst, flettefelt, bilder, ramme i valgt farge),
forhåndsvises som A4-ark i et vindu, og brukes for kursbevis som utstedes etterpå. Alt er oppdiktet."""
from datetime import date

import pytest

from kurs import config, db, kursbevis, kursbevismal, signaturer
from kurs.integrasjoner import epost
from kurs.kjoring import Kjoring
from navnehjelp import navnedeler

IDAG = date(2027, 6, 1)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (120).to_bytes(4, "big") + (40).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00" + b"\x00" * 30


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "ROT", tmp_path)
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    monkeypatch.setattr(epost, "send", lambda *a, **kw: None)
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


def _kurs(con, kode="K1", status="avsluttet"):
    kid = db.opprett_kurs(con, kode=kode, navn="Veiledning i praksis", datoer=["2027-03-01", "2027-03-02"],
                          sharepoint_mappe=f"Kurs/{kode}", status=status, pris_nok=0, sted="Oslo", timer_pr_dag=6)
    con.commit()
    return kid


def _deltaker_med_oppmote(con, kid, navn="Kari Nordmann"):
    con.execute("UPDATE kurs SET status='aapen' WHERE id=?", (kid,))
    pid, _ = db.meld_paa(con, kid, epost="kari@example.no", **navnedeler(navn))
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    for dag in db.kursdager(con, kid):
        db.registrer_oppmote(con, pid, dag["id"], "manuell")
    con.commit()
    return pid


def _utstedt(con, kid):
    kursbevis.kjor(Kjoring(con, idag=IDAG))
    con.commit()
    return con.execute("""SELECT i.innhold FROM dokument_innhold i JOIN dokument d ON d.id=i.dokument_id
                          WHERE d.kurs_id=? AND d.type='kursbevis'""", (kid,)).fetchone()["innhold"]


def test_fanen_finnes_og_viser_utgangspunktet_med_flettefelt(con):
    kid = _kurs(con)
    html = _klient().get(f"/admin/kurs/{kid}/kursbevis").get_data(as_text=True)
    assert f'href="/admin/kurs/{kid}/kursbevis">Kursbevis</a>' in html
    assert "{navn}" in html and "{kursnavn}" in html and "{detaljer}" in html          # utgangspunktet
    assert 'data-sett-inn="{kursnr}"' in html and 'name="ramme"' in html and "Oransje (NIEFT)" in html
    assert 'data-forhandsvis-kursbevis="kursbevis-vindu"' in html and '<dialog id="kursbevis-vindu"' in html
    assert "Kurset bruker standardbeviset" in html


def test_uten_egen_versjon_er_utstedt_kursbevis_som_foer(con):
    kid = _kurs(con)
    _deltaker_med_oppmote(con, kid)
    html = _utstedt(con, kid)
    assert "<h1>Kursbevis</h1>" in html and '<p class="navn">Kari Nordmann</p>' in html and 'class="egen"' not in html


def test_lagret_versjon_og_ramme_brukes_naar_kursbeviset_utstedes(con):
    kid = _kurs(con)
    bid = signaturer.lagre_bilde(con, "nieft.png", PNG, "NIEFT-logo", aktor="test")
    con.commit()
    r = _klient().post(f"/admin/kurs/{kid}/kursbevis", data={
        "ramme": "oransje",
        "innhold": f'<img src="/admin/signaturer/bilde/{bid}"><div style="text-align: center">Kursbevis for {{navn}}</div>'
                   '<div>Kurs nr. {kursnr}: {kursnavn}, {timer} timer ({kursdager}), {deltakelse}.</div>{detaljer}'
                   '<script>alert(1)</script>'}, follow_redirects=True)
    assert "Kursbeviset er lagret" in r.get_data(as_text=True)
    lagret = con.execute("SELECT kursbevis_html, kursbevis_ramme FROM kurs WHERE id=?", (kid,)).fetchone()
    assert lagret["kursbevis_ramme"] == "oransje" and "script" not in lagret["kursbevis_html"]
    _deltaker_med_oppmote(con, kid)
    html = _utstedt(con, kid)
    kursnr = con.execute("SELECT kursnr FROM kurs WHERE id=?", (kid,)).fetchone()[0]
    assert "Kursbevis for Kari Nordmann" in html
    assert f"Kurs nr. {kursnr}: Veiledning i praksis, 12 timer (01.03.2027, 02.03.2027), har gjennomført." in html
    assert "border: 6px double #e8572a" in html and "<td" in html and "Kursdager" in html
    assert 'src="data:image/png;base64,' in html and "/admin/signaturer/bilde" not in html   # bildet er bakt inn


@pytest.mark.parametrize("innhold,feil", [
    ("Hei {ukjent}", "Ukjent flettefelt: {ukjent}"),
    ("<div><br></div>", "Kursbeviset er tomt"),
])
def test_ugyldig_innhold_lagres_ikke(con, innhold, feil):
    kid = _kurs(con)
    r = _klient().post(f"/admin/kurs/{kid}/kursbevis", data={"ramme": "blaa", "innhold": innhold})
    assert r.status_code == 400 and feil in r.get_data(as_text=True)
    assert con.execute("SELECT kursbevis_html FROM kurs WHERE id=?", (kid,)).fetchone()[0] is None


def test_forhandsvisning_viser_a4_med_eksempeldeltaker_og_kan_vises_i_vinduet(con):
    kid = _kurs(con)
    k = _klient()
    r = k.get(f"/admin/kurs/{kid}/kursbevis/forhandsvis")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "Ola Nordmann" in html and "@page { size: A4;" in html
    assert r.headers["X-Frame-Options"] == "SAMEORIGIN" and "script-src" not in r.headers["Content-Security-Policy"]
    # POST: det som står i redigereren nå, uten å lagre
    r = k.post(f"/admin/kurs/{kid}/kursbevis/forhandsvis", data={"ramme": "gronn", "innhold": "Utkast for {fornavn}"})
    html = r.get_data(as_text=True)
    assert "Utkast for Ola" in html and "#2e7d4f" in html
    assert con.execute("SELECT kursbevis_html FROM kurs WHERE id=?", (kid,)).fetchone()[0] is None


def test_tilbakestill_og_kopier_fra_annet_kurs(con):
    a, b = _kurs(con, "A"), _kurs(con, "B")
    k = _klient()
    k.post(f"/admin/kurs/{a}/kursbevis", data={"ramme": "lilla", "innhold": "Bevis for {navn}"})
    side = k.get(f"/admin/kurs/{b}/kursbevis").get_data(as_text=True)
    assert "Kopier fra et annet kurs" in side
    k.post(f"/admin/kurs/{b}/kursbevis", data={"handling": "kopier", "fra_kurs_id": a})
    assert tuple(con.execute("SELECT kursbevis_html, kursbevis_ramme FROM kurs WHERE id=?", (b,)).fetchone()) == (
        "Bevis for {navn}", "lilla")
    k.post(f"/admin/kurs/{b}/kursbevis", data={"handling": "tilbakestill"})
    assert tuple(con.execute("SELECT kursbevis_html, kursbevis_ramme FROM kurs WHERE id=?", (b,)).fetchone()) == (None, None)


def test_lesetilgang_kan_se_men_ikke_endre(con):
    kid = _kurs(con)
    db.opprett_admin_bruker(con, "leser", "Leser", "passord-som-holder", rolle="lese")
    con.commit()
    k = _klient("leser", "passord-som-holder")
    side = k.get(f"/admin/kurs/{kid}/kursbevis").get_data(as_text=True)
    assert "Se kursbeviset" in side and "Lagre kursbeviset" not in side
    assert k.post(f"/admin/kurs/{kid}/kursbevis", data={"ramme": "blaa", "innhold": "x"}).status_code == 403


def test_flettefelt_fylles_ut_og_escapes():
    kurs = {"navn": "Kurs <b>", "kursnr": 1005, "sted": "Oslo", "spesialistlop": None}
    v = dict(navn="Ola <script>", fornavn="Ola", kurs=kurs, dager=["2027-03-01"], timer=6, samlinger="Samling 1 og 3",
             antall_samlinger=3, lop_timer=None, dato="2027-06-01")
    ut = kursbevismal.flett(None, "{navn} – {kursnavn} – {deltakelse} – {dato}", v, db.i_setning)
    assert ut == "Ola &lt;script&gt; – Kurs &lt;b&gt; – har deltatt på samling 1 og 3 i – 01.06.2027"


def test_flettefelt_i_attributter_kan_ikke_lage_nye_attributter():
    kurs = {"navn": "K", "kursnr": 1, "sted": "", "spesialistlop": None}
    v = dict(navn='Ola" onmouseover="x', fornavn="Ola", kurs=kurs, dager=[], timer=0, samlinger=None, antall_samlinger=0,
             lop_timer=None, dato="2027-06-01")
    ut = kursbevismal.flett(None, '<a href="https://example.no/{navn}">lenke</a>', v, db.i_setning)
    assert 'onmouseover="x' not in ut and "&quot;" in ut


def test_feil_ved_kopier_viser_det_lagrede_kursbeviset_igjen(con):
    kid = _kurs(con)
    k = _klient()
    k.post(f"/admin/kurs/{kid}/kursbevis", data={"ramme": "blaa", "innhold": "Mitt bevis for {navn}"})
    r = k.post(f"/admin/kurs/{kid}/kursbevis", data={"handling": "kopier", "fra_kurs_id": 999999})
    assert r.status_code == 400 and "Mitt bevis for {navn}" in r.get_data(as_text=True)
