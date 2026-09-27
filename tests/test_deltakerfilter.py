"""Deltakerlisten: live-søk og statusvalg som avkrysning i stedet for nedtrekksmeny.

Serveren viser alle påmeldingene og skjuler (hidden) det som ikke passer med søket og statusvalgene i adressen. Det er
samme regel som live-søket i static/app.js bruker mens man skriver: delvis treff i navn eller e-post uten hensyn til
store og små bokstaver (også æøå), og én av de valgte statusene (ingen valgt = alle). Excel-eksporten av treff følger den
samme regelen. Statuslogikken og databasen er uendret.
"""
import re
from datetime import date
from urllib.parse import urlencode

import pytest

from kurs import config, db
from listehjelp import skjulte_rader, synlige_rader


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _admin():
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    assert k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN,
                                            "passord": config.ADMIN_PASSORD}).status_code == 302
    return k


def _kurs_med_deltakere(con) -> int:
    """To bekreftede (Mari, Marie), to på venteliste (Stig, Øyvind), én avmeldt (Tone) og én avslått (Frida)."""
    kid = db.opprett_kurs(con, kode="SOK1", navn="Veiledning i gruppe", datoer=[date(2031, 10, 16).isoformat()],
                          sharepoint_mappe="Kurs/SOK1", pris_nok=2500, kapasitet=2)
    pid = {}
    for fornavn, etternavn, epost in [("Mari", "Hansen", "mari.hansen@eksempel.no"), ("Marie", "Olsen", "mo@eksempel.no"),
                                      ("Stig", "Berg", "stig@eksempel.no"), ("Øyvind", "Åsheim", "oa@eksempel.no"),
                                      ("Tone", "Nilsen", "tone@eksempel.no"), ("Frida", "Lie", "frida@eksempel.no")]:
        pid[fornavn], _ = db.meld_paa(con, kid, epost=epost, fornavn=fornavn, etternavn=etternavn)
    db.meld_av(con, pid["Tone"])                                   # fra venteliste: ingen rykker opp
    db.avsla_paamelding(con, pid["Frida"], aktor="admin:test")
    con.commit()
    return kid


def _sporring(*par) -> str:
    """?sok=...&status=... kodet slik nettleseren gjør (æøå, @ og mellomrom)."""
    return "?" + urlencode(par) if par else ""


def _liste(k, kid, sporring=""):
    return k.get(f"/admin/kurs/{kid}/deltakere{sporring}").get_data(as_text=True)


def _antall(html) -> dict:
    return dict(re.findall(r'data-antall="([a-z]+)">\((\d+)\)<', html))


def _navn(rader: str) -> set:
    return set(re.findall(r"<strong>([^<]+)</strong>", rader))


ALLE = {"Mari Hansen", "Marie Olsen", "Stig Berg", "Øyvind Åsheim", "Tone Nilsen", "Frida Lie"}


# ============================ statusvalgene ============================

def test_statusvalg_som_avkrysning_med_antall_i_stedet_for_nedtrekksmeny(con):
    kid = _kurs_med_deltakere(con)
    html = _liste(_admin(), kid)
    assert 'id="f-status"' not in html and "<select" not in html.split('id="deltakerfilter"')[1].split("</form>")[0]
    for verdi, navn in [("bekreftet", "Bekreftet"), ("venteliste", "Venteliste"), ("avmeldt", "Avmeldt"),
                        ("avslatt", "Avslått")]:
        assert f'<input type="checkbox" name="status" value="{verdi}" >' in html
        assert f'{navn} <span class="antall" data-antall="{verdi}">' in html
    assert _antall(html) == {"bekreftet": "2", "venteliste": "2", "avmeldt": "1", "avslatt": "1"}
    assert 'id="velg-alle-synlige"' in html
    assert '<noscript><button class="sekundar liten">Søk</button></noscript>' in html     # bare uten JavaScript


def test_uten_filter_vises_alle_og_nullstill_er_skjult(con):
    kid = _kurs_med_deltakere(con)
    html = _liste(_admin(), kid)
    assert _navn(synlige_rader(html)) == ALLE and skjulte_rader(html) == ""
    assert "Viser 6 deltakere." in html
    assert re.search(r'data-nullstill-filter\s+hidden>Nullstill filter', html)


def test_flere_statuser_kan_velges_samtidig(con):
    kid = _kurs_med_deltakere(con)
    html = _liste(_admin(), kid, "?status=bekreftet&status=venteliste")
    assert _navn(synlige_rader(html)) == {"Mari Hansen", "Marie Olsen", "Stig Berg", "Øyvind Åsheim"}
    assert _navn(skjulte_rader(html)) == {"Tone Nilsen", "Frida Lie"}
    assert 'value="bekreftet" checked>' in html and 'value="venteliste" checked>' in html
    assert 'value="avmeldt" >' in html
    assert "Viser 4 av 6 deltakere." in html
    assert re.search(r'data-nullstill-filter\s*>Nullstill filter', html)                  # synlig når filteret er aktivt


def test_avmeldt_og_avslatt_er_hver_sin_status(con):
    kid = _kurs_med_deltakere(con)
    k = _admin()
    assert _navn(synlige_rader(_liste(k, kid, "?status=avmeldt"))) == {"Tone Nilsen"}
    assert _navn(synlige_rader(_liste(k, kid, "?status=avslatt"))) == {"Frida Lie"}


def test_ukjente_statuser_ignoreres(con):
    kid = _kurs_med_deltakere(con)
    html = _liste(_admin(), kid, "?status=utgatt&status=forlatt")
    assert _navn(synlige_rader(html)) == ALLE
    assert "checked" not in html.split('id="deltakerfilter"')[1].split("</form>")[0]


# ============================ søket ============================

@pytest.mark.parametrize("sok", ["Mar", "mar", "MAR", "  mAr "])
def test_sok_er_delvis_og_uavhengig_av_store_og_smaa_bokstaver(con, sok):
    kid = _kurs_med_deltakere(con)
    html = _liste(_admin(), kid, _sporring(("sok", sok)))
    assert _navn(synlige_rader(html)) == {"Mari Hansen", "Marie Olsen"}
    assert "Stig Berg" in skjulte_rader(html)


@pytest.mark.parametrize("sok, treff", [("øyv", "Øyvind Åsheim"), ("ØYV", "Øyvind Åsheim"), ("åsh", "Øyvind Åsheim"),
                                        ("stig@", "Stig Berg"), ("mo@eksempel", "Marie Olsen")])
def test_sok_i_navn_og_epost_ogsaa_med_aeoeaa(con, sok, treff):
    kid = _kurs_med_deltakere(con)
    assert _navn(synlige_rader(_liste(_admin(), kid, _sporring(("sok", sok))))) == {treff}


def test_sok_og_status_kombineres_og_tallene_folger_sokeet(con):
    kid = _kurs_med_deltakere(con)
    k = _admin()
    html = _liste(k, kid, "?sok=mar&status=venteliste")
    assert synlige_rader(html) == ""
    assert re.search(r'<tr data-ingen-treff="deltaker"\s*>', html)                         # «Ingen deltakere matcher»
    assert _antall(html) == {"bekreftet": "2", "venteliste": "0", "avmeldt": "0", "avslatt": "0"}
    assert "Viser 0 av 6 deltakere." in html
    html = _liste(k, kid, "?sok=mar&status=bekreftet")
    assert _navn(synlige_rader(html)) == {"Mari Hansen", "Marie Olsen"}
    assert re.search(r'<tr data-ingen-treff="deltaker"\s+hidden>', html)


def test_radene_har_det_live_soket_trenger(con):
    kid = _kurs_med_deltakere(con)
    html = _liste(_admin(), kid)
    assert 'data-status="avslatt" data-sok="frida lie frida@eksempel.no"' in html
    assert 'data-status="venteliste" data-sok="øyvind åsheim oa@eksempel.no"' in html     # små bokstaver, også Ø og Å


def test_oppmotetabellen_folger_filteret(con):
    kid = _kurs_med_deltakere(con)
    html = _liste(_admin(), kid, "?sok=hansen")
    assert "Mari Hansen" in synlige_rader(html, "oppmote")
    assert "Marie Olsen" in skjulte_rader(html, "oppmote")
    assert "Stig Berg" not in synlige_rader(html, "oppmote") + skjulte_rader(html, "oppmote")    # bare bekreftede


# ============================ handlingene og eksporten ============================

def test_handlingene_for_valgte_er_uendret(con):
    kid = _kurs_med_deltakere(con)
    html = _liste(_admin(), kid, "?sok=mar")
    assert html.count('class="valgt-deltaker" name="paamelding_id"') == 6                 # alle rader kan velges
    assert html.count('form="valgte-deltakere"') >= 6 + 3
    for knapp in ("Send e-post til valgte", "Eksporter valgte", "Behandle valgte"):
        assert knapp in html


@pytest.mark.parametrize("par, antall", [((), 6), ((("sok", "øyv"),), 1), ((("sok", "MAR"),), 2),
                                         ((("status", "bekreftet"), ("status", "venteliste")), 4),
                                         ((("sok", "mar"), ("status", "venteliste")), 0), ((("status", "avslatt"),), 1)])
def test_eksport_av_treff_folger_samme_regel_som_listen(con, par, antall):
    kid = _kurs_med_deltakere(con)
    k = _admin()
    sporring = _sporring(*par)
    rader = k.get(f"/admin/kurs/{kid}/deltakere.csv{sporring}").get_data(as_text=True).lstrip("﻿").splitlines()
    assert len(rader) - 1 == antall                                                          # minus overskriften
    assert antall == len(synlige_rader(_liste(k, kid, sporring)).split("<strong>")) - 1


def test_eksportlenken_tar_med_filteret(con):
    kid = _kurs_med_deltakere(con)
    html = _liste(_admin(), kid, "?sok=mar&status=bekreftet&status=venteliste")
    assert (f'href="/admin/kurs/{kid}/deltakere.csv?sok=mar&amp;status=bekreftet&amp;status=venteliste">'
            "Eksporter treff (Excel)</a>") in html
    assert f'data-grunnadresse="/admin/kurs/{kid}/deltakere.csv"' in html
