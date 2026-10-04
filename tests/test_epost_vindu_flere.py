"""E-post til flere i et vindu over deltakerlisten, og vedlegg i e-post fra vinduet (Camilla 03.10.2026: «det skal ikke komme
til en ny side, men det skal komme opp et vindu på siden», «legge til en knapp hvor det skal være mulig å legge til vedlegg»).
Alle deltakere og filer her er oppdiktet."""
import io
import re

import pytest

from kurs import config, db, vedlegg

PDF = b"%PDF-1.4\n% oppdiktet\n" + b"x" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _klient():
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def _kurs(con):
    kid = db.opprett_kurs(con, kode="FLER", navn="Flerekurs", datoer=["2031-03-04"], sharepoint_mappe="Kurs/FLER",
                          pris_nok=0, type="fysisk", sted="Oslo")
    a, _ = db.meld_paa(con, kid, epost="tove@example.no", fornavn="Tove", etternavn="Test")
    b, _ = db.meld_paa(con, kid, epost="ola@example.no", fornavn="Ola", etternavn="Test")
    con.commit()
    return kid, a, b


def _innhold(html: str) -> str:
    assert 'id="deltaker-innhold"' in html
    return html.split('id="deltaker-innhold"', 1)[1]


def _nokkel(html: str) -> str:
    return re.search(r'name="skjemanokkel" value="([0-9a-f]{16})"', html).group(1)


def _utboks():
    return sorted(config.UTBOKS.glob("*"))


# ---------------- vindu for flere ----------------

def test_listen_aapner_e_post_til_valgte_i_vinduet(con):
    kid, a, b = _kurs(con)
    liste = _klient().get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    assert f'action="/admin/kurs/{kid}/epost/vindu"' in liste
    assert 'class="liten sekundar" data-deltaker-vindu>Send e-post til valgte</button>' in liste
    assert f'href="/admin/kurs/{kid}/epost/vindu?gruppe=bekreftet" data-deltaker-vindu>' in liste


def test_vinduet_for_flere_har_blaa_topp_og_samme_skjema(con):
    kid, a, b = _kurs(con)
    vist = _innhold(_klient().post(f"/admin/kurs/{kid}/epost/vindu", data={"paamelding_id": [a, b]}).get_data(as_text=True))
    assert 'class="deltakerkort-topp"' in vist and "Send e-post" in vist and "2 mottakere" in vist
    assert "Tove Test" in vist and "Ola Test" in vist
    assert f'action="/admin/kurs/{kid}/epost/vindu/send"' in vist and 'class="sendknapp">Send e-post</button>' in vist
    assert 'data-signaturvisning contenteditable="true"' in vist and 'name="vedlegg"' in vist
    assert vist.count('name="paamelding_id"') == 2


def test_send_til_flere_fra_vinduet_en_e_post_hver_og_kvittering(con):
    kid, a, b = _kurs(con)
    k = _klient()
    nokkel = _nokkel(k.post(f"/admin/kurs/{kid}/epost/vindu", data={"paamelding_id": [a, b]}).get_data(as_text=True))
    r = k.post(f"/admin/kurs/{kid}/epost/vindu/send", data={"skjemanokkel": nokkel, "paamelding_id": [a, b],
                                                            "emne": "Velkommen", "tekst": "Hei {fornavn}"})
    assert r.status_code == 302
    kvittering = k.get(r.headers["Location"]).get_data(as_text=True)
    assert "E-post sendt til 2 mottaker(e)." in kvittering and "E-post sendt" in _innhold(kvittering)
    assert "data-lukk-vindu>Lukk</button>" in kvittering
    tekster = [f.read_text(encoding="utf-8", errors="replace") for f in _utboks()]
    assert len(tekster) == 2 and any("Hei Tove" in t for t in tekster) and any("Hei Ola" in t for t in tekster)
    # dobbeltklikk: ingenting sendes på nytt
    k.post(f"/admin/kurs/{kid}/epost/vindu/send", data={"skjemanokkel": nokkel, "paamelding_id": [a, b],
                                                        "emne": "Velkommen", "tekst": "Hei {fornavn}"})
    assert len(_utboks()) == 2


def test_gruppe_og_ingen_valgt(con):
    kid, a, b = _kurs(con)
    k = _klient()
    tom = _innhold(k.post(f"/admin/kurs/{kid}/epost/vindu", data={}).get_data(as_text=True))
    assert 'name="emne"' not in tom and "data-lukk-vindu" in tom
    assert "Kryss av foran navnet" in k.post(f"/admin/kurs/{kid}/epost/vindu", data={}).get_data(as_text=True).replace("kryss", "Kryss")


def test_bare_deltakere_i_kurset_kan_faa_e_posten(con):
    kid, a, b = _kurs(con)
    annet = db.opprett_kurs(con, kode="ANNET", navn="Annet", datoer=["2031-05-04"], sharepoint_mappe="Kurs/ANNET",
                            pris_nok=0, type="fysisk", sted="Oslo")
    fremmed, _ = db.meld_paa(con, annet, epost="kari@example.no", fornavn="Kari", etternavn="Test")
    con.commit()
    k = _klient()
    k.post(f"/admin/kurs/{kid}/epost/vindu/send", data={"skjemanokkel": "0123456789abcdef",
                                                        "paamelding_id": [a, fremmed], "emne": "Hei", "tekst": "Hei"})
    assert [m["id"] for m in db.admin_utsending_mottakere(con, con.execute("SELECT id FROM admin_utsending").fetchone()[0])] == [a]


# ---------------- vedlegg ----------------

def test_vedlegg_sendes_og_lagres_i_historikken(con):
    kid, a, b = _kurs(con)
    k = _klient()
    nokkel = _nokkel(k.get(f"/admin/kurs/{kid}/deltaker/{a}/epost/ny").get_data(as_text=True))
    k.post(f"/admin/kurs/{kid}/deltaker/{a}/epost/send", content_type="multipart/form-data", data={
        "skjemanokkel": nokkel, "emne": "Program", "tekst": "Hei {fornavn}",
        "vedlegg": [(io.BytesIO(PDF), "Program for kurset.pdf"), (io.BytesIO(PNG), "kart.png")]})
    meta = _utboks()[0].read_text(encoding="utf-8", errors="replace")
    assert "Program for kurset.pdf" in meta and "kart.png" in meta
    rader = con.execute("""SELECT v.filnavn, f.mimetype FROM sendt_epost_vedlegg v JOIN epost_fil f ON f.sha256=v.sha256
                           WHERE v.innebygd_cid IS NULL ORDER BY v.nr""").fetchall()
    assert [tuple(r) for r in rader] == [("Program for kurset.pdf", "application/pdf"), ("kart.png", "image/png")]


def test_vedlegg_til_flere(con):
    kid, a, b = _kurs(con)
    k = _klient()
    nokkel = _nokkel(k.post(f"/admin/kurs/{kid}/epost/vindu", data={"paamelding_id": [a, b]}).get_data(as_text=True))
    k.post(f"/admin/kurs/{kid}/epost/vindu/send", content_type="multipart/form-data", data={
        "skjemanokkel": nokkel, "paamelding_id": [a, b], "emne": "Program", "tekst": "Hei",
        "vedlegg": [(io.BytesIO(PDF), "program.pdf")]})
    assert all("program.pdf" in f.read_text(encoding="utf-8", errors="replace") for f in _utboks()) and len(_utboks()) == 2


@pytest.mark.parametrize("navn,data,feil", [
    ("virus.exe", b"MZ" + b"\x00" * 50, "kan ikke legges ved"),
    ("side.html", b"<html></html>", "kan ikke legges ved"),
    ("falsk.pdf", b"<script>alert(1)</script>", "ikke ut til å være en ekte PDF"),
    ("tom.pdf", b"", "er tom"),
])
def test_ugyldige_vedlegg_stoppes_og_ingenting_sendes(con, navn, data, feil):
    kid, a, b = _kurs(con)
    k = _klient()
    nokkel = _nokkel(k.get(f"/admin/kurs/{kid}/deltaker/{a}/epost/ny").get_data(as_text=True))
    svar = k.post(f"/admin/kurs/{kid}/deltaker/{a}/epost/send", content_type="multipart/form-data", data={
        "skjemanokkel": nokkel, "emne": "Hei", "tekst": "Min tekst", "vedlegg": [(io.BytesIO(data), navn)]})
    html = svar.get_data(as_text=True)
    assert feil in html and "Vedleggene må velges på nytt." in html and "Min tekst" in html
    assert _utboks() == [] and con.execute("SELECT COUNT(*) FROM admin_utsending").fetchone()[0] == 0


def test_for_store_vedlegg_stoppes(con):
    kid, a, b = _kurs(con)
    k = _klient()
    nokkel = _nokkel(k.get(f"/admin/kurs/{kid}/deltaker/{a}/epost/ny").get_data(as_text=True))
    stor = PDF + b"x" * (vedlegg.MAKS_SUM // 2)
    svar = k.post(f"/admin/kurs/{kid}/deltaker/{a}/epost/send", content_type="multipart/form-data", data={
        "skjemanokkel": nokkel, "emne": "Hei", "tekst": "Hei",
        "vedlegg": [(io.BytesIO(stor), "a.pdf"), (io.BytesIO(stor), "b.pdf")]})
    assert "Vedleggene er for store" in svar.get_data(as_text=True) and _utboks() == []


def test_filnavn_renses():
    assert vedlegg.trygt_filnavn('..\\..\\C:\\hemmelig/"rapport".pdf') == "_rapport_.pdf"


# ---------------- kursnummer i alle e-poster (Camilla 03.10.2026) ----------------

def test_kursnummeret_staar_oeverst_i_e_post_fra_vinduet(con):
    kid, a, b = _kurs(con)
    kursnr = con.execute("SELECT kursnr FROM kurs WHERE id=?", (kid,)).fetchone()[0]
    k = _klient()
    nokkel = _nokkel(k.get(f"/admin/kurs/{kid}/deltaker/{a}/epost/ny").get_data(as_text=True))
    k.post(f"/admin/kurs/{kid}/deltaker/{a}/epost/send", data={"skjemanokkel": nokkel, "emne": "Hei", "tekst": "Hei"})
    html = _utboks()[0].read_text(encoding="utf-8", errors="replace")
    assert f'Kurs nr. <span style="font-weight:700">{kursnr}</span> · Flerekurs' in html


def test_kursnummeret_i_rammen_bare_naar_e_posten_gjelder_et_kurs():
    from kurs.integrasjoner import epost
    _, med = epost.render("admin_melding", emne="Hei", tekst="Hei", d={"navn": "A", "fornavn": "A"},
                          kurs={"kursnr": 686, "navn": "EFT"})
    assert "Kurs nr. <span" in med and ">686</span> · EFT" in med
    _, uten = epost.render("innlogging", fornavn="A", lenke="http://x")
    assert "Kurs nr." not in uten


# ---------------- rettelser etter gjennomgangen 04.10.2026 ----------------

def test_samme_skjema_sendt_igjen_tar_med_vedleggene(con, monkeypatch):
    """Blir skjemaet sendt på nytt (f.eks. etter en nettverksfeil), skal de som ikke fikk e-posten få den med vedleggene."""
    from kurs.kjoring import Kjoring
    kid, a, b = _kurs(con)
    k = _klient()
    nokkel = _nokkel(k.get(f"/admin/kurs/{kid}/deltaker/{a}/epost/ny").get_data(as_text=True))
    data = lambda: {"skjemanokkel": nokkel, "emne": "Program", "tekst": "Hei", "vedlegg": [(io.BytesIO(PDF), "program.pdf")]}
    k.post(f"/admin/kurs/{kid}/deltaker/{a}/epost/send", content_type="multipart/form-data", data=data())
    sett = []
    monkeypatch.setattr(Kjoring, "send_admin_utsending", lambda self, uid, vedlegg=(): sett.append([v.filnavn for v in vedlegg]) or
                        type("U", (), {"sendt": [], "uavklart": 0, "ikke_forsokt": 0, "uavklart_fra_for": 0})())
    k.post(f"/admin/kurs/{kid}/deltaker/{a}/epost/send", content_type="multipart/form-data", data=data())
    assert sett == [["program.pdf"]]


@pytest.mark.parametrize("innhold", [b"tekst\x01binaer", b"\x1b[31mfarge"])
def test_tekstfil_med_styretegn_avvises(innhold):
    from werkzeug.datastructures import FileStorage
    with pytest.raises(vedlegg.Vedleggfeil, match="ekte TXT"):
        vedlegg.les([FileStorage(io.BytesIO(innhold), filename="notat.txt")])
    assert vedlegg.les([FileStorage(io.BytesIO("Navn;Sted\nÅse;Bergen\n".encode("cp1252")), filename="liste.csv")])
