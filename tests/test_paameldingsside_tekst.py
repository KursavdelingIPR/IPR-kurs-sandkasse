"""Fase 12C5: introduksjonstekst og tekst paa paameldingsknappen (kurs.paamelding_intro / kurs.paamelding_knappetekst).

Laaste krav: ren tekst (escapes, aldri tolket), strenge grenser ved lagring (1000 / 40 tegn etter normalisering), defensiv
visning (ugyldig lagret verdi -> standard, aldri 500), EN effektiv modell (paameldingsside.effektiv_side) brukt av
offentlig GET, POST-feil og forhaandsvisning, standard = DOM-identisk med foer 12C5, kopieres ved duplisering.
Fiktive testdata."""
import json
import sqlite3

import pytest
from flask import render_template
from jinja2 import UndefinedError

from kurs import config, db
from kurs import paameldingsside as ps


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


@pytest.fixture
def admin():
    k = _klient()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def _kurs(con, kode="T1", intro=None, knapp=None, **kw):
    felter = {"navn": "Eksempelkurs", "type": "fysisk", "pris_nok": 4500, "fakturering": "person", "kapasitet": 10,
              "paamelding_intro": intro, "paamelding_knappetekst": knapp, **kw}
    kid = db.opprett_kurs(con, kode=kode, datoer=["2099-03-02"], sharepoint_mappe=f"K/{kode}", **felter)
    con.commit()
    return kid


def _rad(con, kid):
    return con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()


def _sett_raatt(con, kid, **kol):
    """Skriver forbi valideringen - simulerer en korrupt/ugyldig lagret verdi."""
    for k, v in kol.items():
        con.execute(f"UPDATE kurs SET {k}=? WHERE id=?", (v, kid))
    con.commit()


def _offentlig(kode="T1"):
    return _klient().get(f"/kurs/{kode}").get_data(as_text=True)


def _preview(admin, kid):
    return admin.get(f"/admin/kurs/{kid}/forhandsvis-paamelding").get_data(as_text=True)


INTRO_P = '<p class="intro" style="white-space:pre-line; max-width:760px; margin:0 0 22px">'


# ============================ ren modul ============================

def test_modul_er_ren_uten_db():
    assert "db" not in vars(ps) and "sqlite3" not in vars(ps) and "flask" not in vars(ps)


@pytest.mark.parametrize("verdi,forventet", [(None, None), ("", None), ("   \n\t ", None), ("  Hei  ", "Hei"),
                                             ("A\r\nB\rC\nD", "A\nB\nC\nD"), ("A\tB", "A B"),
                                             ("\n\nA\n\nB\n\n", "A\n\nB")])
def test_normaliser_intro(verdi, forventet):
    assert ps.normaliser_intro(verdi) == forventet


@pytest.mark.parametrize("verdi,forventet", [(None, None), ("", None), ("   ", None), ("  Send inn  ", "Send inn")])
def test_normaliser_knappetekst(verdi, forventet):
    assert ps.normaliser_knappetekst(verdi) == forventet


def test_intro_grense_1000_etter_normalisering():
    assert ps.normaliser_intro("x" * 1000) == "x" * 1000
    assert ps.normaliser_intro("  " + "x" * 1000 + "  ") == "x" * 1000
    raa = "ab\r\n" * 333 + "a"                                         # 1333 tegn raatt, 1000 etter CRLF->LF
    assert len(raa) == 1333 and len(ps.normaliser_intro(raa)) == 1000
    with pytest.raises(ps.SideFeil) as e:
        ps.normaliser_intro("x" * 1001)
    assert e.value.grunn == ps.FOR_LANG and "1000" in e.value.forklaring


def test_knapp_grense_40():
    assert ps.normaliser_knappetekst("x" * 40) == "x" * 40
    with pytest.raises(ps.SideFeil) as e:
        ps.normaliser_knappetekst("x" * 41)
    assert e.value.grunn == ps.FOR_LANG


@pytest.mark.parametrize("tegn", ["\n", "\r", "\t", "\x00", "\x1b", "\x7f", "\x85", " ", " "])
def test_knapp_avviser_linjeskift_tab_og_kontrolltegn(tegn):
    with pytest.raises(ps.SideFeil) as e:
        ps.normaliser_knappetekst(f"A{tegn}B")
    assert e.value.grunn == ps.KONTROLLTEGN


@pytest.mark.parametrize("tegn", ["\x00", "\x07", "\x0b", "\x0c", "\x1b", "\x7f", "\x85", " ", " "])
def test_intro_avviser_kontrolltegn(tegn):
    with pytest.raises(ps.SideFeil) as e:
        ps.normaliser_intro(f"A{tegn}B")
    assert e.value.grunn == ps.KONTROLLTEGN


@pytest.mark.parametrize("verdi", [5, b"x", ["x"]])
def test_feil_type_avvises(verdi):
    for f in (ps.normaliser_intro, ps.normaliser_knappetekst):
        with pytest.raises(ps.SideFeil):
            f(verdi)


def test_sidefeil_baerer_ikke_innhold():
    with pytest.raises(ps.SideFeil) as e:
        ps.normaliser_knappetekst("HEMMELIG" * 10)
    assert "HEMMELIG" not in str(e.value) and "HEMMELIG" not in e.value.forklaring


def test_effektiv_side_standard_og_defensiv():
    rad = {ps.INTRO: None, ps.KNAPPETEKST: None}
    assert ps.effektiv_side(rad) == ps.EffektivSide(None, "Meld meg på")
    assert ps.effektiv_side({ps.INTRO: "  Hei\r\n", ps.KNAPPETEKST: " Send "}) == ps.EffektivSide("Hei", "Send")
    assert ps.effektiv_side({ps.INTRO: "x" * 1001, ps.KNAPPETEKST: "A\nB"}) == ps.EffektivSide(None, "Meld meg på")
    assert ps.effektiv_side({ps.INTRO: "   ", ps.KNAPPETEKST: "   "}) == ps.EffektivSide(None, "Meld meg på")


# ============================ 1-10: offentlig side og preview ============================

def test_standard_ingen_intro_og_meld_meg_paa(con, admin):
    kid = _kurs(con)
    off, pre = _offentlig(), _preview(admin, kid)
    assert 'class="intro"' not in off and 'class="intro"' not in pre
    assert "<button>Meld meg på</button>" in off
    assert '<button type="button">Meld meg på</button>' in pre


def test_egen_intro_offentlig_og_preview_med_linjeskift(con, admin):
    kid = _kurs(con, intro="Velkommen!\nLinje to.")
    for html in (_offentlig(), _preview(admin, kid)):
        assert INTRO_P + "Velkommen!\nLinje to.</p>" in html
        assert html.index('class="sub"') < html.index('class="intro"') < html.index('class="rad"')


def test_egen_knapp_offentlig_og_preview(con, admin):
    kid = _kurs(con, knapp="Send påmelding")
    assert "<button>Send påmelding</button>" in _offentlig()
    assert '<button type="button">Send påmelding</button>' in _preview(admin, kid)
    assert "Meld meg på" not in _offentlig()


def test_ugyldig_lagret_verdi_gir_standard_ikke_500(con, admin):
    kid = _kurs(con)
    _sett_raatt(con, kid, paamelding_intro="x" * 1500, paamelding_knappetekst="A\nB")
    for html in (_offentlig(), _preview(admin, kid)):
        assert 'class="intro"' not in html and "Meld meg på" in html and "xxxxx" not in html


# ============================ 15-17: escaping ============================

FARLIG = "<script>alert(1)</script> <b>Hei</b> {{7*7}}"
ESCAPET = "&lt;script&gt;alert(1)&lt;/script&gt; &lt;b&gt;Hei&lt;/b&gt; {{7*7}}"


def test_html_og_jinja_vises_bokstavelig_overalt(con, admin):
    kid = _kurs(con, intro=FARLIG, knapp="<b>Hei</b> {{7*7}}")
    for html in (_offentlig(), _preview(admin, kid), admin.get(f"/admin/kurs/{kid}/nettside").get_data(as_text=True)):
        assert ESCAPET in html
        assert "&lt;b&gt;Hei&lt;/b&gt; {{7*7}}" in html
        assert "<script>alert(1)</script>" not in html and "<b>Hei</b>" not in html and ">49<" not in html


# ============================ admin: Nettside ============================

def _lagre(admin, kid, intro="", knappetekst=""):
    return admin.post(f"/admin/kurs/{kid}/nettside", data={"intro": intro, "knappetekst": knappetekst})


def test_nettside_viser_redigeringsfelt(con, admin):
    kid = _kurs(con, intro="Hei", knapp="Send")
    html = admin.get(f"/admin/kurs/{kid}/nettside").get_data(as_text=True)
    assert '<textarea name="intro" rows="5" maxlength="1000">Hei</textarea>' in html
    assert 'name="knappetekst" maxlength="40" value="Send" placeholder="Meld meg på"' in html
    assert "Introduksjonstekst" in html and "Tekst på påmeldingsknappen" in html and "Tom = «Meld meg på»" in html
    assert "kommer i en senere fase" not in html and "paamelding_intro" not in html
    assert f'href="/admin/kurs/{kid}/forhandsvis-paamelding"' in html and 'href="/kurs/T1"' in html


def test_lagring_lagrer_og_normaliserer(con, admin):
    kid = _kurs(con)
    r = _lagre(admin, kid, intro="  Linje 1\r\nLinje 2\tslutt  ", knappetekst="  Send inn  ")
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/admin/kurs/{kid}/nettside")
    rad = _rad(con, kid)
    assert (rad["paamelding_intro"], rad["paamelding_knappetekst"]) == ("Linje 1\nLinje 2 slutt", "Send inn")
    assert "Nettsideinnstillingene er lagret." in admin.get(f"/admin/kurs/{kid}/nettside").get_data(as_text=True)


def test_tomme_verdier_lagres_som_null_og_gir_standard(con, admin):
    kid = _kurs(con, intro="Hei", knapp="Send")
    _lagre(admin, kid, intro="  \n ", knappetekst="   ")
    rad = _rad(con, kid)
    assert (rad["paamelding_intro"], rad["paamelding_knappetekst"]) == (None, None)
    html = _offentlig()
    assert 'class="intro"' not in html and "<button>Meld meg på</button>" in html


@pytest.mark.parametrize("intro,knapp", [("x" * 1000, "x" * 40)])
def test_grenseverdier_via_admin_godtas(con, admin, intro, knapp):
    kid = _kurs(con)
    assert _lagre(admin, kid, intro=intro, knappetekst=knapp).status_code == 302
    assert (_rad(con, kid)["paamelding_intro"], _rad(con, kid)["paamelding_knappetekst"]) == (intro, knapp)


@pytest.mark.parametrize("intro,knapp,melding", [
    ("x" * 1001, "OK", "Introduksjonsteksten kan være maks 1000 tegn"),
    ("OK", "x" * 41, "Knappeteksten kan være maks 40 tegn"),
    ("OK", "A\nB", "Knappeteksten må være én linje"),
    ("OK", "A\rB", "Knappeteksten må være én linje"),
    ("OK", "A\tB", "Knappeteksten må være én linje"),
    ("A\x00B", "OK", "Introduksjonsteksten inneholder ugyldige tegn"),
    ("x" * 1001, "x" * 41, "Knappeteksten kan være maks 40 tegn"),
])
def test_ugyldig_lagring_gir_400_uten_delvis_lagring_og_beholder_verdier(con, admin, intro, knapp, melding):
    kid = _kurs(con, intro="Gammel intro", knapp="Gammel knapp")
    foer = (dict(_rad(con, kid)), con.execute("SELECT COUNT(*) FROM hendelse").fetchone()[0])
    r = _lagre(admin, kid, intro=intro, knappetekst=knapp)
    html = r.get_data(as_text=True)
    assert r.status_code == 400 and melding in html and "Ingen endringer er lagret." in html
    assert (dict(_rad(con, kid)), con.execute("SELECT COUNT(*) FROM hendelse").fetchone()[0]) == foer
    assert f">{intro}</textarea>" in html and f'value="{knapp}"' in html    # innsendte verdier vises igjen


def test_admin_kan_rette_korrupt_lagret_verdi(con, admin):
    kid = _kurs(con)
    _sett_raatt(con, kid, paamelding_intro="x" * 1500, paamelding_knappetekst="A\nB")
    html = admin.get(f"/admin/kurs/{kid}/nettside").get_data(as_text=True)
    assert html.count("Lagret tekst er ugyldig og vises ikke") == 2
    assert _lagre(admin, kid, intro="Rettet", knappetekst="Send").status_code == 302
    rad = _rad(con, kid)
    assert (rad["paamelding_intro"], rad["paamelding_knappetekst"]) == ("Rettet", "Send")


def test_uendret_lagring_gir_ingen_skriving(con, admin):
    kid = _kurs(con, intro="Hei", knapp="Send")
    antall = con.execute("SELECT COUNT(*) FROM hendelse").fetchone()[0]
    _lagre(admin, kid, intro=" Hei ", knappetekst="Send")
    assert con.execute("SELECT COUNT(*) FROM hendelse").fetchone()[0] == antall
    assert "Ingen endringer å lagre." in admin.get(f"/admin/kurs/{kid}/nettside").get_data(as_text=True)


def test_logg_inneholder_ikke_tekstinnhold(con, admin):
    kid = _kurs(con)
    _lagre(admin, kid, intro="HEMMELIG-INTRO", knappetekst="HEMMELIG-KNAPP")
    logg = [dict(r) for r in con.execute("SELECT * FROM hendelse WHERE handling='kurs_endret'")]
    assert logg and json.loads(logg[-1]["detaljer"])["felt"] == ["paamelding_intro", "paamelding_knappetekst"]
    assert logg[-1]["aktor"] == f"admin:{config.ADMIN_BRUKERNAVN}"
    assert "HEMMELIG" not in json.dumps([dict(r) for r in con.execute("SELECT * FROM hendelse")])


def test_nettside_krever_innlogging(con):
    kid = _kurs(con, intro="Hei")
    foer = dict(_rad(con, kid))
    k = _klient()
    for r in (k.get(f"/admin/kurs/{kid}/nettside"),
              k.post(f"/admin/kurs/{kid}/nettside", data={"intro": "HACK", "knappetekst": "HACK"})):
        assert r.status_code == 302 and "/admin/logg-inn" in r.headers["Location"]
    assert dict(_rad(con, kid)) == foer


def test_oppsett_lagring_overskriver_ikke_nettsidetekstene(con, admin):
    kid = _kurs(con, intro="Behold intro", knapp="Behold knapp")
    r = admin.post(f"/admin/kurs/{kid}/oppsett", data={
        "navn": "Nytt navn", "type": "fysisk", "fakturering": "person", "betaling": "samlet", "kapasitet": "10",
        "pris_nok": "4500", "faktura_dager_for": "14", "paameldingsfrist": "", "sted": "", "notat": "",
        "kursholder_epost": ""})
    assert r.status_code == 302
    rad = _rad(con, kid)
    assert rad["navn"] == "Nytt navn"
    assert (rad["paamelding_intro"], rad["paamelding_knappetekst"]) == ("Behold intro", "Behold knapp")


# ============================ 19: POST-feil paa offentlig side ============================

def test_offentlig_post_feil_beholder_egen_intro_og_knapp(con):
    kid = _kurs(con, intro="Egen intro", knapp="Send inn")
    k = _klient()
    r = k.post("/kurs/T1", data={"navn": "Test", "epost": "test@eksempel.no"})       # mangler samtykke
    html = r.get_data(as_text=True)
    assert r.status_code == 400 and INTRO_P + "Egen intro</p>" in html and "<button>Send inn</button>" in html
    db.meld_paa(con, kid, epost="finnes@eksempel.no", navn="Finnes")
    con.commit()
    r = k.post("/kurs/T1", data={"navn": "Finnes", "epost": "finnes@eksempel.no", "samtykke": "on"})
    html = r.get_data(as_text=True)
    assert r.status_code == 400 and "allerede påmeldt" in html
    assert INTRO_P + "Egen intro</p>" in html and "<button>Send inn</button>" in html


# ============================ duplisering ============================

def _dupliser(admin, kilde_id, **over):
    data = {"navn": "Kopi", "type": "fysisk", "start_kl": "09:00", "slutt_kl": "16:00", "fakturering": "person",
            "betaling": "samlet", "datoer": "2099-05-05", "pris_nok": "4500", "fra": str(kilde_id), **over}
    return admin.post(f"/admin/kurs/ny?fra={kilde_id}", data=data, follow_redirects=True)


def _nyeste(con):
    return con.execute("SELECT * FROM kurs ORDER BY id DESC LIMIT 1").fetchone()


def test_duplisering_kopierer_begge(con, admin):
    kid = _kurs(con, intro="Intro\nto", knapp="Send")
    r = _dupliser(admin, kid)
    ny = _nyeste(con)
    assert r.status_code == 200 and ny["id"] != kid
    assert (ny["paamelding_intro"], ny["paamelding_knappetekst"]) == ("Intro\nto", "Send")
    assert "kunne ikke kopieres" not in r.get_data(as_text=True)


def test_vanlig_nytt_kurs_faar_null(con, admin):
    _kurs(con, intro="Intro", knapp="Send")
    admin.post("/admin/kurs/ny", data={"navn": "Nytt", "type": "fysisk", "start_kl": "09:00", "slutt_kl": "16:00",
                                       "fakturering": "person", "datoer": "2099-05-05"})
    ny = _nyeste(con)
    assert ny["navn"] == "Nytt" and (ny["paamelding_intro"], ny["paamelding_knappetekst"]) == (None, None)


@pytest.mark.parametrize("korrupt,gyldig_kol,gyldig_verdi", [
    ({"paamelding_intro": "x" * 1500}, "paamelding_knappetekst", "Send"),
    ({"paamelding_knappetekst": "A\nB"}, "paamelding_intro", "Intro"),
])
def test_korrupt_kilde_kopieres_ikke_men_gyldig_verdi_folger_med(con, admin, korrupt, gyldig_kol, gyldig_verdi):
    kid = _kurs(con, intro="Intro", knapp="Send")
    _sett_raatt(con, kid, **korrupt)
    r = _dupliser(admin, kid)
    html = r.get_data(as_text=True)
    ny = _nyeste(con)
    assert ny["id"] != kid and ny[next(iter(korrupt))] is None and ny[gyldig_kol] == gyldig_verdi
    assert ("Noen nettsideinnstillinger på originalkurset kunne ikke kopieres. "
            "Kontroller Nettside på det nye kurset.") in html
    assert "xxxxxxxx" not in html and "A\nB" not in html


def test_begge_korrupte_gir_null_og_en_advarsel(con, admin):
    kid = _kurs(con)
    _sett_raatt(con, kid, paamelding_intro="\x01" if db.er_postgres(con) else "\x00", paamelding_knappetekst="x" * 99)
    html = _dupliser(admin, kid).get_data(as_text=True)
    ny = _nyeste(con)
    assert (ny["paamelding_intro"], ny["paamelding_knappetekst"]) == (None, None)
    assert html.count("Noen nettsideinnstillinger på originalkurset kunne ikke kopieres.") == 1


def test_kilde_og_kopi_er_uavhengige(con, admin):
    kid = _kurs(con, intro="Kilde", knapp="Kildeknapp")
    _dupliser(admin, kid)
    ny = _nyeste(con)["id"]
    _lagre(admin, ny, intro="Kopi endret", knappetekst="")
    assert (_rad(con, kid)["paamelding_intro"], _rad(con, kid)["paamelding_knappetekst"]) == ("Kilde", "Kildeknapp")
    _lagre(admin, kid, intro="Kilde endret", knappetekst="Ny")
    assert (_rad(con, ny)["paamelding_intro"], _rad(con, ny)["paamelding_knappetekst"]) == ("Kopi endret", None)


def test_duplisering_leser_ikke_kilden_paa_nytt(con, admin, monkeypatch):
    """Tekstene tas fra kilderaden som _kildekurs_fra allerede har lest - _hent_kurs kalles kun den ene gangen."""
    from kurs.web import app as webapp
    kid = _kurs(con, intro="Intro", knapp="Send")
    kall = []
    ekte = webapp._hent_kurs
    monkeypatch.setattr(webapp, "_hent_kurs", lambda k: (kall.append(k), ekte(k))[1])
    _dupliser(admin, kid)
    assert kall.count(kid) == 1


# ============================ bedriftspaamelding / mal / migrering ============================

def test_bedriftspaamelding_er_uendret(con):
    _kurs(con, intro="EGEN INTRO", knapp="EGEN KNAPP")
    html = _klient().get("/kurs/T1/gruppe").get_data(as_text=True)
    assert "EGEN" not in html and "<button>Meld på gruppen</button>" in html and 'class="intro"' not in html


def test_mal_uten_side_feiler_hoylytt(con):
    from kurs import skjemafelt
    from kurs.web import app as webapp
    kid = _kurs(con)
    kurs = _rad(con, kid)
    with webapp.app.test_request_context("/"):
        with pytest.raises(UndefinedError):
            render_template("kurs.html", kurs=kurs, dager=[], f={}, plasser_igjen=None,
                            skjema=skjemafelt.effektivt_skjema(kurs))


def test_eksisterende_database_migreres_med_null(tmp_path):
    sti = tmp_path / "gammel.db"
    c = db.koble(sti)
    db.init(c)
    kid = db.opprett_kurs(c, kode="G", navn="Gammelt", datoer=["2099-03-02"], sharepoint_mappe="K/G")
    c.commit()
    c.execute("ALTER TABLE kurs DROP COLUMN paamelding_intro")
    c.execute("ALTER TABLE kurs DROP COLUMN paamelding_knappetekst")
    c.commit()
    kolonner = set(db.kolonner(c, "kurs"))
    assert "paamelding_intro" not in kolonner
    db.init(c)
    db.init(c)                                         # idempotent
    rad = c.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    assert (rad["paamelding_intro"], rad["paamelding_knappetekst"], rad["navn"]) == (None, None, "Gammelt")
    c.close()


def test_opprett_kurs_kan_lagre_begge_kolonnene(con):
    kid = _kurs(con, kode="OK", intro="Intro", knapp="Send")
    rad = _rad(con, kid)
    assert (rad["paamelding_intro"], rad["paamelding_knappetekst"]) == ("Intro", "Send")


def test_standard_er_dom_identisk_med_gullstandard_via_egen_fil():
    """Gullstandarden (tests/test_skjemafelt_gullstandard.py) kjoeres uendret og beviser at standardoppsettet er
    DOM-identisk med foer 12C5. Her sikres at snapshotet fortsatt har standardknappen og ingen intro."""
    import pathlib
    snap = json.loads((pathlib.Path(__file__).parent / "gullstandard" / "paamelding_skjema.json").read_text("utf-8"))
    for navn, s in snap.items():
        assert not any("class" in t and "'intro'" in t for t in s["dom"]), navn
    assert sum("#Meld meg på" in s["dom"] for s in snap.values()) == 13
