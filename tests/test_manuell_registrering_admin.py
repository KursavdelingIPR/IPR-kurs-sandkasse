"""Fase 9, trinn 2: admin-ruten og skjemaet for manuell paamelding ('Legg til deltaker').

Ingen e-post/faktura skal opprettes i dette trinnet - kun registrering med sveiper_utsatt=1.
Den eksplisitte 'Send bekreftelse og behandle fakturering naa'-knappen kommer i trinn 3.
"""
from datetime import date, timedelta

import pytest

from adressehjelp import ADRESSE
from kurs import config, db

RUTE = "/admin/kurs/{kid}/deltaker/ny"


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    return c


def _klient():
    from kurs.web import app as webapp
    return webapp.app.test_client()


def _logg_inn(klient):
    klient.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})


def _kurs(con, kode="T1", start=None, **kw):
    start = start or (date.today() + timedelta(days=30))
    return db.opprett_kurs(con, kode=kode, navn="Testkurs", datoer=[start.isoformat()],
                           sharepoint_mappe=f"Kurs/{kode}", **{"pris_nok": 1000, **kw})


def _antall_mail(con):
    return len(list(config.UTBOKS.glob("*.html"))) if config.UTBOKS.exists() else 0


def _skjema(**over):
    return {"fornavn": "Kari", "etternavn": "Nordmann", "epost": "kari@x.no", "telefon": "99999999",
            "yrkestittel": "Psykolog", "arbeidssted": "Klinikk AS", "hpr_nr": "1234567",
            "betaler": "person", **ADRESSE, **over}


# ---------------- tilgang ----------------

def test_ruten_krever_admin_get(con):
    kid = _kurs(con)
    con.commit()
    resp = _klient().get(RUTE.format(kid=kid))
    assert resp.status_code == 302
    assert "/admin/logg-inn" in resp.headers["Location"]


def test_ruten_krever_admin_post(con):
    kid = _kurs(con)
    con.commit()
    resp = _klient().post(RUTE.format(kid=kid), data=_skjema())
    assert resp.status_code == 302
    assert "/admin/logg-inn" in resp.headers["Location"]
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 0


# ---------------- skjema vises ----------------

def test_skjema_vises_korrekt(con):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get(RUTE.format(kid=kid)).get_data(as_text=True)
    for felt in ("navn", "epost", "adresse", "postnr", "poststed", "telefon", "yrkestittel", "arbeidssted", "hpr_nr",
                 "org_nr", "faktura_ref", "faktura_kommentar"):
        assert felt in tekst
    # Deltakerens private adresse er krav (også når arbeidsgiver betaler) og er fakturaadressen for en privat betaler:
    # skjemaet har ingen egne fakturaadressefelt lenger
    for felt in ("adresse", "postnr", "poststed"):
        assert f'name="{felt}" required' in tekst
    assert "faktura_adresse" not in tekst and "faktura_postnr" not in tekst and "faktura_sted" not in tekst
    assert 'name="org_navn"' not in tekst            # felles regel: firmanavnet hentes fra registeret, skrives ikke


def test_utkast_viser_tydelig_advarsel_i_skjemaet(con):
    kid = _kurs(con, status="utkast")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get(RUTE.format(kid=kid)).get_data(as_text=True)
    assert "ikke publisert" in tekst.lower()


# ---------------- vanlig manuell paamelding ----------------

def test_vanlig_manuell_paamelding_lagres_bekreftet(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = klient.post(RUTE.format(kid=kid), data=_skjema(), follow_redirects=False)
    assert resp.status_code == 302
    rad = con.execute(
        "SELECT p.status, p.kilde, p.sveiper_kjort, p.sveiper_utsatt, d.navn, d.yrkestittel, d.hpr_nr "
        "FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE d.epost='kari@x.no'").fetchone()
    assert rad["status"] == "bekreftet"
    assert rad["kilde"] == "admin"
    assert rad["sveiper_kjort"] == 0
    assert rad["sveiper_utsatt"] == 1
    assert rad["navn"] == "Kari Nordmann"
    assert rad["yrkestittel"] == "Psykolog"
    assert rad["hpr_nr"] == "1234567"
    assert _antall_mail(con) == 0
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 0


def test_admin_er_aktor_i_loggen(con):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(RUTE.format(kid=kid), data=_skjema())
    rad = con.execute(
        "SELECT aktor FROM hendelse WHERE handling='paamelding' ORDER BY id DESC LIMIT 1").fetchone()
    assert rad["aktor"].startswith("admin:")


def test_redirect_gaar_til_deltakerprofilen_og_viser_status(con):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = klient.post(RUTE.format(kid=kid), data=_skjema())
    pid = con.execute("SELECT id FROM paamelding").fetchone()[0]
    assert resp.headers["Location"].endswith(f"/admin/kurs/{kid}/deltaker/{pid}")
    tekst = klient.get(resp.headers["Location"]).get_data(as_text=True)
    assert '<span class="merke ok">Påmeldt</span>' in tekst  # statusen «Påmeldt» vises paa profilsiden (databaseverdien er fortsatt «bekreftet»)
    assert "Bekreftet" not in tekst


# ---------------- dubletter / reaktivering / eksisterende person ----------------

def test_eksisterende_person_gjenbrukes(con):
    kid1 = _kurs(con, "K1")
    kid2 = _kurs(con, "K2")
    db.meld_paa(con, kid1, epost="kari@x.no", fornavn="Kari", etternavn="Nordmann")
    con.commit()
    antall_deltakere_foer = con.execute("SELECT COUNT(*) FROM deltaker").fetchone()[0]
    klient = _klient()
    _logg_inn(klient)
    klient.post(RUTE.format(kid=kid2), data=_skjema())
    assert con.execute("SELECT COUNT(*) FROM deltaker").fetchone()[0] == antall_deltakere_foer


def test_dublett_samme_kurs_avvises_tydelig(con):
    kid = _kurs(con)
    db.meld_paa(con, kid, epost="kari@x.no", fornavn="Kari", etternavn="Nordmann")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = klient.post(RUTE.format(kid=kid), data=_skjema())
    assert resp.status_code == 400
    assert "allerede påmeldt" in resp.get_data(as_text=True)
    assert con.execute("SELECT COUNT(*) FROM paamelding WHERE kurs_id=?", (kid,)).fetchone()[0] == 1


def test_tidligere_avmeldt_reaktiveres(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="kari@x.no", fornavn="Kari", etternavn="Nordmann")
    con.execute("UPDATE paamelding SET sveiper_kjort=1, sveiper_utsatt=1, kilde='skjema' WHERE id=?", (pid,))
    db.meld_av(con, pid)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(RUTE.format(kid=kid), data=_skjema())
    rad = con.execute(
        "SELECT id, status, kilde, sveiper_kjort, sveiper_utsatt FROM paamelding WHERE kurs_id=?", (kid,)
    ).fetchone()
    assert rad["id"] == pid  # samme rad, reaktivert - ikke en ny
    assert rad["status"] == "bekreftet"
    assert rad["kilde"] == "admin"
    assert rad["sveiper_kjort"] == 0
    assert rad["sveiper_utsatt"] == 1


# ---------------- kapasitet / frist / kursstatus ----------------

def test_fullt_kurs_gir_venteliste(con):
    kid = _kurs(con, kapasitet=1)
    db.meld_paa(con, kid, epost="forst@x.no", fornavn="Forst", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(RUTE.format(kid=kid), data=_skjema())
    rad = con.execute(
        "SELECT p.status FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE d.epost='kari@x.no'"
    ).fetchone()
    assert rad["status"] == "venteliste"


def test_etter_frist_tillates_for_admin(con):
    kid = _kurs(con, paameldingsfrist=(date.today() - timedelta(days=1)).isoformat())
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = klient.post(RUTE.format(kid=kid), data=_skjema())
    assert resp.status_code == 302
    assert con.execute("SELECT COUNT(*) FROM paamelding WHERE kurs_id=?", (kid,)).fetchone()[0] == 1


def test_utkast_tillates(con):
    kid = _kurs(con, status="utkast")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = klient.post(RUTE.format(kid=kid), data=_skjema())
    assert resp.status_code == 302
    assert con.execute("SELECT COUNT(*) FROM paamelding WHERE kurs_id=?", (kid,)).fetchone()[0] == 1
    assert _antall_mail(con) == 0


def test_avlyst_blokkeres(con):
    kid = _kurs(con)
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kid,))
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = klient.post(RUTE.format(kid=kid), data=_skjema())
    assert resp.status_code == 400
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 0


def test_avsluttet_blokkeres(con):
    kid = _kurs(con)
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = klient.post(RUTE.format(kid=kid), data=_skjema())
    assert resp.status_code == 400
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 0


# ---------------- validering (samme regler som offentlig paamelding) ----------------

def test_organisasjon_betaler_uten_nodvendige_felt_avvises(con):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    resp = klient.post(RUTE.format(kid=kid), data=_skjema(betaler="organisasjon", org_navn="", org_nr=""))
    assert resp.status_code == 400
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 0


def test_pris_og_betalingsvalg_lagres_korrekt(con):
    start = date.today() + timedelta(days=30)
    kid = db.opprett_kurs(con, kode="T1", navn="Testkurs", datoer=[start.isoformat(), (start + timedelta(days=30)).isoformat()],
                          sharepoint_mappe="Kurs/T1", pris_nok=3000, betaling="deltaker_velger")      # to samlinger
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    assert 'value="per_samling"' in klient.get(RUTE.format(kid=kid)).get_data(as_text=True)
    klient.post(RUTE.format(kid=kid), data=_skjema(betaling="per_samling"))
    rad = con.execute("SELECT betaling FROM paamelding").fetchone()
    assert rad["betaling"] == "per_samling"


def test_bare_en_samling_gir_ingen_betalingsvalg_og_en_vanlig_faktura(con):
    kid = _kurs(con, betaling="deltaker_velger", pris_nok=3000)                        # bare én samling
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    assert 'name="betaling"' not in klient.get(RUTE.format(kid=kid)).get_data(as_text=True)
    klient.post(RUTE.format(kid=kid), data=_skjema(betaling="per_samling"))              # sendt inn likevel
    assert con.execute("SELECT betaling FROM paamelding").fetchone()["betaling"] == "samlet"


# ---------------- personvern ----------------

def test_allergi_lagres_riktig_og_logges_ikke(con):
    kid = _kurs(con, type="fysisk")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(RUTE.format(kid=kid), data=_skjema(allergier="Nøtteallergi", tilrettelegging="Rullestolrampe"))
    pid = con.execute("SELECT id FROM paamelding").fetchone()[0]
    sensitivt = con.execute("SELECT allergier, tilrettelegging FROM sensitivt WHERE paamelding_id=?", (pid,)).fetchone()
    assert sensitivt["allergier"] == "Nøtteallergi"
    assert sensitivt["tilrettelegging"] == "Rullestolrampe"
    logger = con.execute("SELECT detaljer FROM hendelse").fetchall()
    assert not any("øtteallergi" in (r["detaljer"] or "") or "ullestolrampe" in (r["detaljer"] or "") for r in logger)


# ---------------- ingen automatikk i dette trinnet ----------------

def test_ingen_epost_eller_faktura_opprettes(con):
    kid = _kurs(con, pris_nok=1000, fakturering="person")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(RUTE.format(kid=kid), data=_skjema())
    assert _antall_mail(con) == 0
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 0
