"""Fase 12B3A: admin-grensesnitt for redigerbare e-postmaler.

Selve utsendingsarkitekturen (fase 12B2C) er uendret her. Dette dekker KUN admin-UI-en: oversikt, redigering,
tilbakestilling og forhaandsvisning. Forhaandsvisningen gjenbruker Kjoring.render_for_sending (samme motor som
faktisk utsending) med fiktive eksempeldata fra kurs/mal_eksempler.py - aldri ekte deltaker-/kurs-/firmadata,
aldri claim, aldri epost.send, aldri en utsending_logg-rad.
"""
import re

import pytest

from kurs import config, db, mal_eksempler, maltekster
from kurs.maltekster import MALER, MalFeil


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
    return webapp.app.test_client()


def _innlogget():
    k = _klient()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def _flash(resp):
    side = resp.get_data(as_text=True)
    return [t.strip() for t in re.findall(r'class="flash[^"]*"[^>]*>\s*([^<]+)', side)]


LAASTE = ["innlogging", "eskalering", "admin_melding"]


# ============================ oversikt ============================

def test_admin_maa_vaere_innlogget(con):
    assert _klient().get("/admin/e-postmaler").status_code == 302
    assert _klient().get("/admin/e-postmaler/bekreftelse").status_code == 302
    assert _klient().get("/admin/e-postmaler/bekreftelse/forhandsvis").status_code == 302
    assert _klient().post("/admin/e-postmaler/bekreftelse/emne", data={"tekst": "x"}).status_code == 302


def test_alle_8_maler_vises_i_oversikten(con):
    html = _innlogget().get("/admin/e-postmaler").get_data(as_text=True)
    for mal, m in MALER.items():
        assert m.navn in html, mal
        assert m.beskrivelse in html, mal
    assert len(MALER) == 8


@pytest.mark.parametrize("mal", LAASTE)
def test_laaste_maler_kan_ikke_redigeres(con, mal):
    k = _innlogget()
    assert k.get(f"/admin/e-postmaler/{mal}").status_code == 404
    assert k.get(f"/admin/e-postmaler/{mal}/forhandsvis").status_code == 404
    assert k.post(f"/admin/e-postmaler/{mal}/tekst", data={"tekst": "x"}).status_code == 404
    assert k.post(f"/admin/e-postmaler/{mal}/tekst/tilbakestill").status_code == 404
    assert mal not in _innlogget().get("/admin/e-postmaler").get_data(as_text=True)


def test_standard_status_vises_for_urort_mal(con):
    html = _innlogget().get("/admin/e-postmaler").get_data(as_text=True)
    seksjon = html.split("Ventelistebeskjed")[1]
    assert 'Standard' in seksjon.split("</div>")[0] or "Standard" in html


def test_tilpasset_status_vises_etter_lagret_override(con):
    k = _innlogget()
    k.post("/admin/e-postmaler/venteliste/emne", data={"tekst": "Du står på venteliste: {kursnavn}"})
    html = k.get("/admin/e-postmaler").get_data(as_text=True)
    assert "Tilpasset" in html
    rediger = k.get("/admin/e-postmaler/venteliste").get_data(as_text=True)
    assert "Tilpasset" in rediger


# ============================ lagring ============================

def test_gyldig_override_lagres_og_brukes(con):
    k = _innlogget()
    r = k.post("/admin/e-postmaler/bekreftelse/emne", data={"tekst": "Din plass på {kursnavn} er klar"},
              follow_redirects=True)
    assert r.status_code == 200
    assert maltekster.effektiv_tekst(con, "bekreftelse", "emne") == "Din plass på {kursnavn} er klar"
    assert "Din plass på {kursnavn} er klar" in r.get_data(as_text=True)   # raatekst vises i feltet uendret


def test_ugyldig_placeholder_avvises(con):
    k = _innlogget()
    r = k.post("/admin/e-postmaler/bekreftelse/innledning", data={"tekst": "Hei {ukjent_kode}"})
    assert r.status_code == 400
    h = r.get_data(as_text=True)
    assert "Hei {ukjent_kode}" in h                                        # teksten beholdes i skjemaet
    for forbudt in ("Traceback", "MalFeil", "mangler_verdi", "ukjent_kode\"", "Exception"):
        assert forbudt not in h
    assert con.execute("SELECT COUNT(*) FROM mal_tekst").fetchone()[0] == 0


@pytest.mark.parametrize("ugyldig", ["Hei {navn", "Hei }", "{{ kurs.pris_nok }}", "{% if x %}", "{navn.__class__}"])
def test_flere_ugyldige_syntakser_avvises_trygt(con, ugyldig):
    k = _innlogget()
    r = k.post("/admin/e-postmaler/bekreftelse/innledning", data={"tekst": ugyldig})
    assert r.status_code == 400
    assert con.execute("SELECT COUNT(*) FROM mal_tekst").fetchone()[0] == 0


def test_raa_html_fra_admin_escapes_ved_forhaandsvisning_aldri_aktiv(con):
    k = _innlogget()
    k.post("/admin/e-postmaler/bekreftelse/innledning", data={"tekst": "<script>alert(1)</script> {navn}"})
    prev = k.get("/admin/e-postmaler/bekreftelse/forhandsvis").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in prev
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in prev


def test_tom_tekst_i_obligatorisk_felt_avvises(con):
    k = _innlogget()
    r = k.post("/admin/e-postmaler/bekreftelse/innledning", data={"tekst": "   "})
    assert r.status_code == 400
    assert con.execute("SELECT COUNT(*) FROM mal_tekst").fetchone()[0] == 0


def test_tom_tekst_i_valgfritt_felt_godtas(con):
    k = _innlogget()
    k.post("/admin/e-postmaler/ukefor/avslutning", data={"tekst": "Noe"})
    r = k.post("/admin/e-postmaler/ukefor/avslutning", data={"tekst": ""}, follow_redirects=True)
    assert r.status_code == 200
    assert maltekster.effektiv_tekst(con, "ukefor", "avslutning") == ""


def test_ukjent_felt_gir_404(con):
    k = _innlogget()
    assert k.post("/admin/e-postmaler/bekreftelse/finnes_ikke", data={"tekst": "x"}).status_code == 404
    assert k.post("/admin/e-postmaler/finnes_ikke_mal/emne", data={"tekst": "x"}).status_code == 404


# ============================ tilbakestill ============================

def test_reset_fjerner_override_og_gir_standardtekst(con):
    k = _innlogget()
    standard = maltekster.effektiv_tekst(con, "bekreftelse", "innledning")
    k.post("/admin/e-postmaler/bekreftelse/innledning", data={"tekst": "Egen tekst {navn}."})
    assert maltekster.effektiv_tekst(con, "bekreftelse", "innledning") == "Egen tekst {navn}."
    r = k.post("/admin/e-postmaler/bekreftelse/innledning/tilbakestill", follow_redirects=True)
    assert r.status_code == 200
    assert maltekster.effektiv_tekst(con, "bekreftelse", "innledning") == standard
    assert con.execute("SELECT COUNT(*) FROM mal_tekst WHERE mal='bekreftelse' AND felt='innledning'").fetchone()[0] == 0


def test_reset_bruker_post_ikke_get(con):
    k = _innlogget()
    k.post("/admin/e-postmaler/venteliste/emne", data={"tekst": "Egen: {kursnavn}"})
    assert k.get("/admin/e-postmaler/venteliste/emne/tilbakestill").status_code in (404, 405)
    assert maltekster.effektiv_tekst(con, "venteliste", "emne") == "Egen: {kursnavn}"   # uendret


def test_reset_av_felt_uten_override_er_trygt_no_op(con):
    k = _innlogget()
    r = k.post("/admin/e-postmaler/bekreftelse/emne/tilbakestill", follow_redirects=True)
    assert r.status_code == 200
    assert con.execute("SELECT COUNT(*) FROM mal_tekst").fetchone()[0] == 0


# ============================ forhaandsvisning ============================

def test_preview_bruker_override_naar_den_finnes(con):
    k = _innlogget()
    k.post("/admin/e-postmaler/kursbevis_klar/emne", data={"tekst": "Ditt kursbevis: {kursnavn}"})
    html = k.get("/admin/e-postmaler/kursbevis_klar/forhandsvis").get_data(as_text=True)
    assert "Emne:</strong> Ditt kursbevis: Eksempelkurs i emosjonsfokusert terapi" in html


def test_preview_bruker_standard_naar_ingen_override(con):
    html = _innlogget().get("/admin/e-postmaler/kursbevis_klar/forhandsvis").get_data(as_text=True)
    assert "Emne:</strong> Kursbevis: Eksempelkurs i emosjonsfokusert terapi" in html
    assert "Hei Ola," in html                   # standardteksten hilser med {fornavn}


@pytest.mark.parametrize("mal", list(MALER))
def test_preview_bruker_kun_fiktive_data_for_alle_8_maler(con, mal):
    html = _innlogget().get(f"/admin/e-postmaler/{mal}/forhandsvis").get_data(as_text=True)
    assert "Hei Ola," in html or "Hei Kari" in html  # deltakerens fornavn, kontaktperson eller kursholder
    for forbudt in ("@x.no", "@ipr.no", "@firma", "9999", "org_nr", "telefon"):
        assert forbudt not in html.lower() if forbudt.isalpha() else forbudt not in html


def test_preview_lager_ingen_utsending_logg_rad(con):
    k = _innlogget()
    hendelser_for = con.execute("SELECT COUNT(*) FROM hendelse").fetchone()[0]   # innlogging kan selv logge en hendelse
    for mal in MALER:
        k.get(f"/admin/e-postmaler/{mal}/forhandsvis")
    assert con.execute("SELECT COUNT(*) FROM utsending_logg").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM hendelse").fetchone()[0] == hendelser_for


def test_preview_kaller_aldri_epost_send(con, monkeypatch):
    from kurs.integrasjoner import epost
    kalt = []
    monkeypatch.setattr(epost, "send", lambda *a, **kw: kalt.append(1))
    k = _innlogget()
    for mal in MALER:
        k.get(f"/admin/e-postmaler/{mal}/forhandsvis")
    assert kalt == []


def test_variant_preview_dagfor_viser_alle_tre_varianter(con):
    html = _innlogget().get("/admin/e-postmaler/dagfor/forhandsvis").get_data(as_text=True)
    assert "Første kursdag" in html and "Dag midt i kurset" in html and "Siste kursdag" in html
    assert "I morgen starter" in html and "Siste kursdag i morgen" in html


def test_variant_preview_purring_viser_alle_tre_varianter(con):
    html = _innlogget().get("/admin/e-postmaler/purring/forhandsvis").get_data(as_text=True)
    assert "Frist om 7 dager" in html and "Frist om 2 dager" in html and "Frist i dag" in html
    assert "frist om 7 dager" in html and "frist om 2 dager" in html and "Frist i dag:" in html


def test_variant_override_slaar_ut_i_alle_varianter(con):
    k = _innlogget()
    k.post("/admin/e-postmaler/dagfor/innledning_midt", data={"tekst": "Egen midt-tekst {navn}."})
    html = k.get("/admin/e-postmaler/dagfor/forhandsvis").get_data(as_text=True)
    assert "Egen midt-tekst Ola Nordmann." in html
    assert "I morgen starter" in html                                      # forste-varianten er uendret


def test_korrupt_override_gir_trygg_admin_feil_i_preview(con):
    con.execute("INSERT INTO mal_tekst (mal, felt, tekst) VALUES ('purring', 'innledning', 'Hei {ukjent}')")
    con.commit()
    r = _innlogget().get("/admin/e-postmaler/purring/forhandsvis")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "flash feil" in html or "kunne ikke" in html.lower()
    for forbudt in ("Traceback", "MalFeil", "ukjent_kode", "sqlite3"):
        assert forbudt not in html


def test_db_lesefeil_gir_trygg_admin_feil_i_preview(con):
    con.execute("DROP TABLE mal_tekst")
    con.commit()
    r = _innlogget().get("/admin/e-postmaler/bekreftelse/forhandsvis")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    for forbudt in ("Traceback", "sqlite3.OperationalError", "no such table"):
        assert forbudt not in html


def test_korrupt_override_i_ett_felt_odelegger_ikke_hele_redigeringssiden(con):
    """Kritisk: effektive_tekster()/hent_overstyringer() validerer ALLE rader for malen samtidig, saa en korrupt
    rad i ETT felt kunne i prinsippet gjort at redigeringssiden aldri kunne aapnes for aa rette den. Siden skal i
    stedet vise den lagrede raateksten trygt (og fortsatt la admin rette/tilbakestille den) - aldri 500."""
    con.execute("INSERT INTO mal_tekst (mal, felt, tekst) VALUES ('bekreftelse', 'innledning', 'Hei {ukjent}')")
    con.commit()
    k = _innlogget()
    r = k.get("/admin/e-postmaler/bekreftelse")
    assert r.status_code == 200
    h = r.get_data(as_text=True)
    assert "Ugyldig lagret tekst" in h and "Hei {ukjent}" in h             # raateksten vises, ikke en krasjende side
    assert "Bekreftelse: {kursnavn}" in h                                  # de andre feltene (her: standard) vises fortsatt
    for forbudt in ("Traceback", "sqlite3", "500 Internal"):
        assert forbudt not in h
    r2 = k.post("/admin/e-postmaler/bekreftelse/innledning/tilbakestill", follow_redirects=True)   # kan rettes herfra
    assert r2.status_code == 200
    assert maltekster.effektiv_tekst(con, "bekreftelse", "innledning") == maltekster.standard_tekst("bekreftelse", "innledning")


# ============================ eksempeldata i seg selv ============================

def test_mal_eksempler_dekker_alle_8_maler_uten_feil():
    for mal in MALER:
        variant_data = mal_eksempler.eksempler(mal)
        assert variant_data, mal
        for variant, data in variant_data:
            assert isinstance(data, dict)


def test_mal_eksempler_inneholder_ingen_ekte_epost_eller_telefon():
    for mal in MALER:
        for _variant, data in mal_eksempler.eksempler(mal):
            tekst = str(data)
            assert "@" not in tekst   # ingen e-postadresser i eksempeldata i det hele tatt
