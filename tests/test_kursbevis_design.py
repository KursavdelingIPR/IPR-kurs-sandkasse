"""Kursbevis-design (Camilla 06.10.2026): designene lages én gang (farge, logo, illustrasjon, skrift, overskrift, signatur) og
velges per kurs under fanen Kursbevis. Kurs uten design får kursbeviset som før. Alt er oppdiktet."""
import re
from datetime import date

import pytest

from kurs import config, db, kursbevis, kursbevisdesign, kursbevismal, signaturer
from kurs.integrasjoner import epost
from kurs.kjoring import Kjoring
from navnehjelp import navnedeler

IDAG = date(2027, 6, 1)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (120).to_bytes(4, "big") + (40).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00" + b"\x00" * 30


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
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
    k.post("/admin/logg-inn", data={"brukernavn": brukernavn or config.ADMIN_BRUKERNAVN, "passord": passord or config.ADMIN_PASSORD})
    return k


def _kurs(con, kode="K1"):
    kid = db.opprett_kurs(con, kode=kode, navn="Veiledning i praksis", datoer=["2027-03-01", "2027-03-02"],
                          sharepoint_mappe=f"Kurs/{kode}", status="avsluttet", pris_nok=0, sted="Oslo", timer_pr_dag=6)
    con.commit()
    return kid


def _utstedt(con, kid):
    con.execute("UPDATE kurs SET status='aapen' WHERE id=?", (kid,))
    pid, _ = db.meld_paa(con, kid, epost="kari@example.no", **navnedeler("Kari Nordmann"))
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    for dag in db.kursdager(con, kid):
        db.registrer_oppmote(con, pid, dag["id"], "manuell")
    con.commit()
    kursbevis.kjor(Kjoring(con, idag=IDAG))
    con.commit()
    return con.execute("""SELECT i.innhold FROM dokument_innhold i JOIN dokument d ON d.id=i.dokument_id
                          WHERE d.kurs_id=? AND d.type='kursbevis'""", (kid,)).fetchone()["innhold"]


def _design(con, navn):
    kursbevisdesign.sikre_standard(con)
    con.commit()                                # ellers holder testens tilkobling skrivelåsen når appen skal lagre
    return con.execute("SELECT * FROM kursbevis_design WHERE navn=?", (navn,)).fetchone()


# ============================ designene ============================

def test_standarddesignene_lages_en_gang(con):
    assert [d["navn"] for d in kursbevisdesign.alle(con)] == ["EFST", "EFT", "IPR", "Terapiakademiet"]
    kursbevisdesign.sikre_standard(con)
    assert con.execute("SELECT COUNT(*) FROM kursbevis_design").fetchone()[0] == 4
    eft, efst = _design(con, "EFT"), _design(con, "EFST")
    assert (eft["logo"], eft["illustrasjon"]) == ("nieft", "stoler") and (efst["logo"], efst["illustrasjon"]) == ("nieft", "strutser")


@pytest.mark.parametrize("felt, feil", [
    ({"navn": ""}, "navn"), ({"farge": "rød"}, "farge"), ({"logo": "ukjent"}, "Ugyldig"), ({"skrift": "comic"}, "Ugyldig"),
    ({"navn": "EFST"}, "finnes allerede"), ({"signatur_bilde_id": "999"}, "Signaturbildet"),
])
def test_ugyldige_verdier_avvises(con, felt, feil):
    eft = _design(con, "EFT")
    with pytest.raises(kursbevisdesign.Designfeil, match=feil):
        kursbevisdesign.lagre(con, eft["id"], {**dict(eft), **felt}, aktor="admin:test")


def test_alle_bildefilene_finnes():
    for _navn, fil in kursbevisdesign.LOGOER.values():
        assert fil is None or (kursbevisdesign.STATIC / fil).is_file(), fil
    for _navn, filer in kursbevisdesign.ILLUSTRASJONER.values():
        assert all((kursbevisdesign.STATIC / f).is_file() for f in filer), filer


# ============================ kursbeviset ============================

def test_uten_design_er_kursbeviset_som_foer_men_med_norske_datoer(con):
    html = _utstedt(con, _kurs(con))
    assert '<div class="liten">Institutt for Psykologisk Rådgivning</div>' in html and "<h1>Kursbevis</h1>" in html
    assert "designtittel" not in html and "data:image/png" not in html
    assert "<td>01.03.2027, 02.03.2027</td>" in html


def test_eft_designet_gir_logo_to_stoler_farge_og_signatur(con):
    kid = _kurs(con)
    bilde = signaturer.lagre_bilde(con, "signatur.png", PNG, "Signatur, daglig leder", aktor="admin:test")
    eft = _design(con, "EFT")
    kursbevisdesign.lagre(con, eft["id"], {**dict(eft), "signatur_bilde_id": str(bilde), "signatur_navn": "Ola Leder",
                                            "signatur_tittel": "Daglig leder, IPR"}, aktor="admin:test")
    kursbevisdesign.velg_for_kurs(con, kid, eft["id"], aktor="admin:test")
    con.commit()
    html = _utstedt(con, kid)
    assert '<div class="logo"><img src="data:image/png;base64,' in html and 'alt="NIEFT"' in html
    figurer = re.search(r'<div class="figurer">(.*?)</div>', html, re.S).group(1)
    assert figurer.count("data:image/png") == 2 and 'class="designtittel">Kursbevis</h1>' in figurer
    assert "h1.designtittel { color: #2f9e44" in html and "border: 6px double #2f9e44" in html
    assert 'alt="Signatur"' in html and "Ola Leder<br>Daglig leder, IPR" in html
    assert "Dette bekrefter at" in html and '<p class="navn">Kari Nordmann</p>' in html
    assert "Institutt for Psykologisk Rådgivning</div>" not in html            # designets logo erstatter den gamle toppteksten


def test_efst_designet_har_strutsene_som_ett_bilde_med_overskriften_i_midten(con):
    kid = _kurs(con)
    kursbevisdesign.velg_for_kurs(con, kid, _design(con, "EFST")["id"], aktor="admin:test")
    con.commit()
    html = _utstedt(con, kid)
    assert re.search(r'<div class="figurpar"><img src="data:image/png;base64,[^"]+" alt=""><div class="midt"><h1 class="designtittel">', html)
    assert "______________________________<br>Daglig leder, IPR</div>" in html     # standardtittelen, uten navn


def test_uten_ramme_holdes_ogsaa_med_design(con):
    kid = _kurs(con)
    con.execute("UPDATE kurs SET kursbevis_ramme='ingen' WHERE id=?", (kid,))
    kursbevisdesign.velg_for_kurs(con, kid, _design(con, "IPR")["id"], aktor="admin:test")
    con.commit()
    assert "border: 0;" in _utstedt(con, kid)


def test_terapiakademiet_bruker_moderne_skrift(con):
    kid = _kurs(con)
    kursbevisdesign.velg_for_kurs(con, kid, _design(con, "Terapiakademiet")["id"], aktor="admin:test")
    con.commit()
    assert "font-family: Calibri" in _utstedt(con, kid)


# ============================ adminsidene ============================

def test_fanen_kan_velge_og_fjerne_design(con):
    kid = _kurs(con)
    k = _klient()
    side = k.get(f"/admin/kurs/{kid}/kursbevis").get_data(as_text=True)
    assert 'name="design_id"' in side and "Uten design (som før)" in side and "Se og endre designene" in side
    efst = _design(con, "EFST")
    r = k.post(f"/admin/kurs/{kid}/kursbevis", data={"handling": "design", "design_id": str(efst["id"])}, follow_redirects=True)
    assert "Designet er valgt" in r.get_data(as_text=True)
    assert con.execute("SELECT kursbevis_design_id FROM kurs WHERE id=?", (kid,)).fetchone()[0] == efst["id"]
    forhandsvis = k.get(f"/admin/kurs/{kid}/kursbevis/forhandsvis").get_data(as_text=True)
    assert "figurpar" in forhandsvis and "width: 21cm" in forhandsvis
    k.post(f"/admin/kurs/{kid}/kursbevis", data={"handling": "design", "design_id": ""})
    assert con.execute("SELECT kursbevis_design_id FROM kurs WHERE id=?", (kid,)).fetchone()[0] is None


def test_designsiden_viser_designene_og_lagrer_endringer(con):
    k = _klient()
    side = k.get("/admin/kursbevis-design").get_data(as_text=True)
    assert all(f">{n}</h2>" in side.replace('</span> ', '>') for n in ("EFT", "EFST", "IPR", "Terapiakademiet"))
    eft = _design(con, "EFT")
    r = k.post("/admin/kursbevis-design", data={**{k_: (v or "") for k_, v in dict(eft).items() if k_ not in ("id", "opprettet", "endret")},
                                                 "design_id": str(eft["id"]), "farge": "#123456", "tittel": "Kursbevis EFT"},
               follow_redirects=True)
    assert "er lagret" in r.get_data(as_text=True)
    assert (_design(con, "EFT")["farge"], _design(con, "EFT")["tittel"]) == ("#123456", "Kursbevis EFT")
    forhandsvis = k.get(f"/admin/kursbevis-design/{eft['id']}/forhandsvis")
    assert forhandsvis.status_code == 200 and "Kursbevis EFT" in forhandsvis.get_data(as_text=True)
    assert "default-src 'none'" in forhandsvis.headers["Content-Security-Policy"]


def test_nytt_signaturbilde_lastes_opp_i_designet_og_kan_ikke_slettes_mens_det_brukes(con):
    from io import BytesIO
    k = _klient()
    efst = _design(con, "EFST")
    data = {**{k_: (v or "") for k_, v in dict(efst).items() if k_ not in ("id", "opprettet", "endret", "signatur_bilde_id")},
            "design_id": str(efst["id"]), "signatur_alt": "Signatur, daglig leder", "signatur_fil": (BytesIO(PNG), "signatur.png")}
    r = k.post("/admin/kursbevis-design", data=data, content_type="multipart/form-data", follow_redirects=True)
    assert "er lagret" in r.get_data(as_text=True)
    bilde = _design(con, "EFST")["signatur_bilde_id"]
    assert bilde and signaturer.bilde(con, bilde)
    with pytest.raises(signaturer.Signaturfeil, match="kursbevis-designet «EFST»"):
        signaturer.slett_bilde(con, bilde, aktor="admin:test")


def test_lesetilgang_kan_se_men_ikke_endre(con):
    db.opprett_admin_bruker(con, "leser", "Lese Leser", "lese-passord-123", rolle="lese")
    con.commit()
    k = _klient("leser", "lese-passord-123")
    side = k.get("/admin/kursbevis-design")
    assert side.status_code == 200 and "Lagre designet" not in side.get_data(as_text=True)
    assert k.post("/admin/kursbevis-design", data={"navn": "Nytt", "farge": "#000000"}).status_code == 403


def test_kopier_fra_annet_kurs_tar_med_designet(con):
    a, b = _kurs(con, "A1"), _kurs(con, "B1")
    con.execute("UPDATE kurs SET kursbevis_html=? WHERE id=?", (kursbevismal.STANDARD_HTML, a))
    kursbevisdesign.velg_for_kurs(con, a, _design(con, "EFT")["id"], aktor="admin:test")
    con.commit()
    kursbevismal.kopier(con, b, a, aktor="admin:test")
    assert con.execute("SELECT kursbevis_design_id FROM kurs WHERE id=?", (b,)).fetchone()[0] == _design(con, "EFT")["id"]


def test_redigereren_starter_med_bare_midten_naar_kurset_har_design(con):
    kid = _kurs(con)
    k = _klient()
    assert "INSTITUTT FOR PSYKOLOGISK" in k.get(f"/admin/kurs/{kid}/kursbevis").get_data(as_text=True)
    kursbevisdesign.velg_for_kurs(con, kid, _design(con, "EFT")["id"], aktor="admin:test")
    con.commit()
    side = k.get(f"/admin/kurs/{kid}/kursbevis").get_data(as_text=True)
    assert "INSTITUTT FOR PSYKOLOGISK" not in side and "Dette bekrefter at" in side
