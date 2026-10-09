"""Kursside for deltakere: hvem som får se siden og filene (kurs/deltakerside.py, kurs/web/deltakerside_ruter.py).

Tilgangsmatrisen (SPEC §8.1): bare deltakere med BEKREFTET påmelding på akkurat det kurset, og bare når siden er publisert, åpen og ikke
stengt. Alle avslag der deltakeren ikke har tilgang gir samme 404-tekst. Filer: bare fra den publiserte siden, bare synlig innhold.
Bare oppdiktede data.
"""
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest

from kurs import config, db, deltakerside
from kurs import sideinnhold as si
from kurs import sidelager

from kurssidehjelp import (IDAG, admin_klient, dokument, deltaker_klient, fast_dato, gif, lag_deltaker, lag_kurs, ny_database, pdf, png, skriv_side,
                           tekstblokk)

NOYTRAL = ("Vi finner ikke siden, eller du har ikke tilgang til den. Min side er bare for deltakere med bekreftet påmelding. "
           "Sjekk at du er logget inn med e-postadressen du meldte deg på med.")


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    fast_dato(monkeypatch)
    yield c
    c.close()


@pytest.fixture
def kid(con):
    return lag_kurs(con, "K1")


def _side(con, kid, **kw):
    """Publisert side med én synlig tekstblokk."""
    return skriv_side(con, kid, dokument(tekstblokk("Velkommen", "<p>Hei og velkommen til kurset</p>"), tittel="Kurssiden vår"), **kw)


def _meld(con, kid, epost="kari@example.no", **kw):
    did, pid = lag_deltaker(con, kid, epost, **kw)
    return did


def _hent(klient, kode="K1"):
    return klient.get(f"/kurs/{kode}/deltakerside")


def _tekst_i_404(svar) -> str:
    treff = re.search(r"<h1>Min side</h1>\s*<p>(.*?)</p>", svar.get_data(as_text=True), re.S)
    return treff.group(1).strip() if treff else ""


# ============================ hvem som kommer inn ============================

def test_bekreftet_deltaker_paa_kurset_faar_siden(con, kid):
    _side(con, kid)
    did = _meld(con, kid)
    r = _hent(deltaker_klient(did))
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "Kurssiden vår" in html and "Hei og velkommen til kurset" in html and "Du er påmeldt" in html


@pytest.mark.parametrize("status", ["venteliste", "avmeldt", "avslatt", "utgatt", "forlatt"])
def test_alle_andre_paameldingsstatuser_gir_noytral_404(con, kid, status):
    _side(con, kid)
    _meld(con, kid, "kari@example.no", fornavn="Kari")                    # en annen deltaker er bekreftet, så kurset er ikke tomt
    did = _meld(con, kid, "vera@example.no", fornavn="Vera", status=status)
    r = _hent(deltaker_klient(did))
    assert r.status_code == 404 and _tekst_i_404(r) == NOYTRAL
    assert "Hei og velkommen til kurset" not in r.get_data(as_text=True)


def test_deltaker_som_bare_er_paameldt_et_annet_kurs_faar_noytral_404(con, kid):
    _side(con, kid)
    andre = lag_kurs(con, "K2")
    skriv_side(con, andre, dokument(tekstblokk("Andres side", "<p>Hemmelig for K2</p>")))
    did = _meld(con, andre)
    r = _hent(deltaker_klient(did), "K1")
    assert r.status_code == 404 and _tekst_i_404(r) == NOYTRAL
    assert _hent(deltaker_klient(did), "K2").status_code == 200


def test_ukjent_kurskode_gir_samme_404_som_ikke_paameldt(con, kid):
    _side(con, kid)
    did = _meld(con, lag_kurs(con, "K2"))
    a, b = _hent(deltaker_klient(did), "FINNES-IKKE"), _hent(deltaker_klient(did), "K1")
    assert a.status_code == b.status_code == 404 and _tekst_i_404(a) == _tekst_i_404(b) == NOYTRAL


@pytest.mark.parametrize("kursstatus, ok", [("aapen", True), ("full", True), ("aktiv", True), ("avsluttet", True), ("utkast", False), ("avlyst", False)])
def test_kursstatus_styrer_tilgangen(con, kid, kursstatus, ok):
    _side(con, kid)
    did = _meld(con, kid)
    con.execute("UPDATE kurs SET status=? WHERE id=?", (kursstatus, kid))
    con.commit()
    r = _hent(deltaker_klient(did))
    assert (r.status_code == 200) is ok
    if not ok:
        assert _tekst_i_404(r) == NOYTRAL


def test_alle_avslag_der_deltakeren_ikke_har_tilgang_har_samme_tekst(con, kid):
    _side(con, kid)
    andre = lag_kurs(con, "K2")
    skriv_side(con, andre, dokument(tekstblokk()))
    ok_did = _meld(con, kid, "kari@example.no")
    avmeldt = _meld(con, kid, "ola@example.no", status="avmeldt")
    venter = _meld(con, kid, "vera@example.no", status="venteliste")           # sist: en avmelding rykker ellers første på ventelisten opp
    annet = _meld(con, andre, "aisha@example.no")
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (andre,))
    con.commit()
    tekster = {_tekst_i_404(_hent(deltaker_klient(d), kode)) for d, kode in ((venter, "K1"), (avmeldt, "K1"), (annet, "K1"), (annet, "K2"), (ok_did, "K9"))}
    assert tekster == {NOYTRAL}


def test_publisert_men_ikke_aapnet_ennaa_og_nedtatt_gir_egne_tekster(con, kid):
    did = _meld(con, kid)
    skriv_side(con, kid, dokument(tekstblokk()), publiser=False)                                   # bare utkast
    r = _hent(deltaker_klient(did))
    assert r.status_code == 404 and "ikke åpnet ennå" in _tekst_i_404(r)
    sidelager.publiser(con, kid, 1, "admin:test")
    con.commit()
    assert _hent(deltaker_klient(did)).status_code == 200
    sidelager.sett_aktiv(con, kid, False, "admin:test")
    con.commit()
    r = _hent(deltaker_klient(did))
    assert r.status_code == 404 and _tekst_i_404(r) == "Min side er midlertidig stengt."
    sidelager.sett_aktiv(con, kid, True, "admin:test")
    con.commit()
    assert _hent(deltaker_klient(did)).status_code == 200


def test_utkast_uten_publisering_lekker_aldri_til_deltakere(con, kid):
    did = _meld(con, kid)
    skriv_side(con, kid, dokument(tekstblokk("Utkast", "<p>Bare utkast</p>")), publiser=False)
    assert "Bare utkast" not in _hent(deltaker_klient(did)).get_data(as_text=True)
    sidelager.publiser(con, kid, 1, "admin:test")
    sidelager.lagre_utkast(con, kid, dokument(tekstblokk("Utkast", "<p>Nytt uferdig utkast</p>")), 1, "admin:test")
    con.commit()
    html = _hent(deltaker_klient(did)).get_data(as_text=True)
    assert "Bare utkast" in html and "Nytt uferdig utkast" not in html


def test_avsluttet_kurs_er_aapent_til_siste_kursdag_pluss_180_dager(con, kid, monkeypatch):
    _side(con, kid)
    did = _meld(con, kid)
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()
    siste = sidelager.siste_kursdag(con, kid)
    fast_dato(monkeypatch, siste + timedelta(days=180))
    r = _hent(deltaker_klient(did))
    assert r.status_code == 200 and "Kurset er avsluttet" in r.get_data(as_text=True)
    fast_dato(monkeypatch, siste + timedelta(days=181))
    r = _hent(deltaker_klient(did))
    stengt = (siste + timedelta(days=180))
    assert r.status_code == 404 and _tekst_i_404(r) == f"Min side er stengt (den var åpen til {stengt.day}. {deltakerside._MAANEDER[stengt.month - 1]} {stengt.year})."


def test_aapen_til_dato_overstyrer_standarden(con, kid, monkeypatch):
    _side(con, kid)
    did = _meld(con, kid)
    sidelager.sett_innstillinger(con, kid, stenges="2027-03-12", aktor="admin:test")
    con.commit()
    fast_dato(monkeypatch, IDAG + timedelta(days=2))
    assert _hent(deltaker_klient(did)).status_code == 200
    fast_dato(monkeypatch, IDAG + timedelta(days=3))
    assert _hent(deltaker_klient(did)).status_code == 404


def test_innstilt_aapningstid_kan_endres_med_config(con, kid, monkeypatch):
    _side(con, kid)
    did = _meld(con, kid)
    monkeypatch.setattr(config, "KURSSIDE_ETTERTILGANG_DAGER", 5)
    fast_dato(monkeypatch, sidelager.siste_kursdag(con, kid) + timedelta(days=6))
    assert _hent(deltaker_klient(did)).status_code == 404


def test_anonymisert_deltaker_har_ikke_lenger_tilgang_selv_med_gammel_okt(con, kid):
    _side(con, kid)
    did = _meld(con, kid, "kari@example.no")
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()
    klient = deltaker_klient(did)
    assert _hent(klient).status_code == 200
    db.anonymiser_deltaker(con, did, "admin:test")
    con.commit()
    r = _hent(klient)
    assert r.status_code == 404 and _tekst_i_404(r) == NOYTRAL


def test_anonymisering_endrer_ikke_innholdet_paa_siden(con, kid):
    _side(con, kid)
    did = _meld(con, kid)
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()
    foer = tuple(sidelager.hent(con, kid))
    db.anonymiser_deltaker(con, did, "admin:test")
    con.commit()
    assert tuple(sidelager.hent(con, kid)) == foer and sidelager.hent_publisert(con, kid)["tittel"] == "Kurssiden vår"


# ============================ uinnlogget og admin ============================

def test_uinnlogget_sendes_til_innlogging_med_neste_uansett_om_kurset_finnes(con, kid):
    _side(con, kid)
    for kode in ("K1", "FINNES-IKKE"):
        r = deltaker_klient()
        svar = _hent(r, kode)
        assert svar.status_code == 302 and svar.headers["Location"] == f"/logg-inn?neste=/kurs/{kode}/deltakerside"


def test_admin_uten_deltakerokt_sendes_til_forhandsvisningen(con, kid):
    _side(con, kid)
    admin = admin_klient()
    r = _hent(admin)
    assert r.status_code == 302 and r.headers["Location"] == f"/admin/kurs/{kid}/kursside/forhandsvis?versjon=publisert"
    assert _hent(admin, "FINNES-IKKE").status_code == 404
    assert admin.get(r.headers["Location"]).status_code == 200


def test_utloept_eller_deaktivert_admin_okt_slipper_ikke_inn(con, kid):
    _side(con, kid)
    admin = admin_klient()
    with admin.session_transaction() as s:
        s["admin_sist"] = 0                                                                        # inaktiv i alle år: økten er utløpt
    r = _hent(admin)
    assert r.status_code == 302 and r.headers["Location"].startswith("/logg-inn?neste=")
    admin2 = admin_klient()
    con.execute("UPDATE admin_bruker SET aktiv=0")
    con.commit()
    r = _hent(admin2)
    assert r.status_code == 302 and r.headers["Location"].startswith("/logg-inn?neste=")


def test_admin_kan_aldri_hente_filer_via_deltakerruten(con, kid):
    fil_id, _ = sidelager.lagre_fil(con, kid, "a.pdf", pdf(), "admin:test")
    skriv_side(con, kid, dokument({"id": "b_filer001", "type": "filer", "tittel": "F", "data": {"filer": [{"fil_id": fil_id, "tittel": "A"}]}}))
    r = deltaker_klient().get(f"/kurs/K1/deltakerside/fil/{fil_id}")
    assert r.status_code == 302 and r.headers["Location"].startswith("/logg-inn")
    r = admin_klient().get(f"/kurs/K1/deltakerside/fil/{fil_id}")
    assert r.status_code == 302 and r.headers["Location"].startswith("/logg-inn")            # admin uten deltakerøkt: ingen fil


def test_deltakersiden_har_no_store_og_ingen_rammetillatelse(con, kid):
    _side(con, kid)
    did = _meld(con, kid)
    r = _hent(deltaker_klient(did))
    assert r.headers["Cache-Control"] == "no-store" and r.headers["X-Frame-Options"] == "DENY" and "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert _hent(deltaker_klient(did), "FINNES-IKKE").headers["X-Frame-Options"] == "DENY"


# ============================ filer ============================

def _side_med_filer(con, kid):
    """(publisert fil, utkast-fil, fil i skjult blokk, fil bak vis_fra, fil bak synlig_fra) - alle hentbare bare hvis de er synlige."""
    ider = {n: sidelager.lagre_fil(con, kid, f"{n}.pdf", pdf(n.encode()), "admin:test")[0] for n in ("synlig", "utkast", "skjult", "visfra", "synligfra")}
    dok = dokument(
        {"id": "b_filer001", "type": "filer", "tittel": "Presentasjoner", "data": {"filer": [
            {"fil_id": ider["synlig"], "tittel": "Øvelse på Åsane – ærlig talt"}, {"fil_id": ider["synligfra"], "tittel": "Kommer senere",
                                                                                   "synlig_fra": (IDAG + timedelta(days=5)).isoformat()}]}},
        {"id": "b_skjult01", "type": "filer", "tittel": "Skjult", "skjult": True, "data": {"filer": [{"fil_id": ider["skjult"], "tittel": "S"}]}},
        {"id": "b_visfra01", "type": "filer", "tittel": "Senere blokk", "vis_fra": (IDAG + timedelta(days=5)).isoformat(),
         "data": {"filer": [{"fil_id": ider["visfra"], "tittel": "V"}]}})
    skriv_side(con, kid, dok)
    filer, dager = sidelager.valideringsgrunnlag(con, kid)
    utkast = json.loads(json.dumps(dok))
    utkast["blokker"].append({"id": "b_utkast01", "type": "filer", "tittel": "Bare i utkastet", "data": {"filer": [{"fil_id": ider["utkast"], "tittel": "U"}]}})
    sidelager.lagre_utkast(con, kid, si.valider(utkast, fil_ider=filer, kursdag_ider=dager)[0], 1, "admin:test")     # ikke publisert
    con.commit()
    return ider


def _fil(klient, kid_kode, fil_id):
    return klient.get(f"/kurs/{kid_kode}/deltakerside/fil/{fil_id}")


def test_fil_fra_publisert_synlig_blokk_gir_200_med_riktige_headere(con, kid):
    ider = _side_med_filer(con, kid)
    did = _meld(con, kid)
    r = _fil(deltaker_klient(did), "K1", ider["synlig"])
    assert r.status_code == 200 and r.data == pdf(b"synlig")
    assert r.headers["Content-Type"].startswith("application/octet-stream") and r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Content-Disposition"].startswith("attachment; filename=\"") and r.headers["Cache-Control"] == "private, no-store"
    assert r.headers["Content-Security-Policy"] == "sandbox; default-src 'none'"


def test_alt_som_ikke_er_synlig_for_deltakere_gir_404(con, kid):
    ider = _side_med_filer(con, kid)
    k = deltaker_klient(_meld(con, kid))
    for navn in ("utkast", "skjult", "visfra", "synligfra"):
        assert _fil(k, "K1", ider[navn]).status_code == 404, navn
    assert _fil(k, "K1", 999_999).status_code == 404


def test_siden_viser_kommer_uten_lenke_og_skjuler_resten(con, kid):
    ider = _side_med_filer(con, kid)
    html = _hent(deltaker_klient(_meld(con, kid))).get_data(as_text=True)
    assert "Øvelse på Åsane – ærlig talt" in html and f"/kurs/K1/deltakerside/fil/{ider['synlig']}" in html
    assert "Kommer senere" in html and re.search(r"Kommer \d+\. \w+", html) and f"/fil/{ider['synligfra']}" not in html
    for skjult in ("Bare i utkastet", "Senere blokk"):
        assert skjult not in html
    for navn in ("utkast", "skjult", "visfra"):
        assert f"/fil/{ider[navn]}" not in html


def test_filer_bak_synlig_fra_blir_hentbare_naar_datoen_er_naadd(con, kid, monkeypatch):
    ider = _side_med_filer(con, kid)
    k = deltaker_klient(_meld(con, kid))
    fast_dato(monkeypatch, IDAG + timedelta(days=5))
    assert _fil(k, "K1", ider["synligfra"]).status_code == 200 and _fil(k, "K1", ider["visfra"]).status_code == 200
    assert _fil(k, "K1", ider["skjult"]).status_code == 404 and _fil(k, "K1", ider["utkast"]).status_code == 404


def test_fil_fra_annet_kurs_kan_ikke_hentes_selv_om_deltakeren_er_paameldt_begge(con, kid):
    _side_med_filer(con, kid)
    andre = lag_kurs(con, "K2")
    andres, _ = sidelager.lagre_fil(con, andre, "andres.pdf", pdf(b"andres"), "admin:test")
    skriv_side(con, andre, dokument({"id": "b_filer001", "type": "filer", "tittel": "F", "data": {"filer": [{"fil_id": andres, "tittel": "A"}]}}))
    did = _meld(con, kid)
    _meld(con, andre, "kari@example.no")                                                           # samme person
    k = deltaker_klient(did)
    assert _fil(k, "K2", andres).status_code == 200
    assert _fil(k, "K1", andres).status_code == 404                                                # via feil kurs-URL
    assert _fil(k, "K2", andres).data == pdf(b"andres")


def test_fil_krever_bekreftet_paamelding_paa_akkurat_kurset(con, kid):
    ider = _side_med_filer(con, kid)
    for status in ("venteliste", "avmeldt"):
        did = _meld(con, kid, f"{status}@example.no", status=status)
        assert _fil(deltaker_klient(did), "K1", ider["synlig"]).status_code == 404, status
    annet = _meld(con, lag_kurs(con, "K2"), "annet@example.no")
    assert _fil(deltaker_klient(annet), "K1", ider["synlig"]).status_code == 404
    assert _fil(deltaker_klient(), "K1", ider["synlig"]).status_code == 302


def test_fil_gir_404_naar_siden_er_nedtatt_eller_stengt(con, kid, monkeypatch):
    ider = _side_med_filer(con, kid)
    k = deltaker_klient(_meld(con, kid))
    sidelager.sett_aktiv(con, kid, False, "admin:test")
    con.commit()
    assert _fil(k, "K1", ider["synlig"]).status_code == 404
    sidelager.sett_aktiv(con, kid, True, "admin:test")
    con.commit()
    fast_dato(monkeypatch, sidelager.siste_kursdag(con, kid) + timedelta(days=200))
    assert _fil(k, "K1", ider["synlig"]).status_code == 404


def test_filnavn_med_aesoeaa_i_content_disposition(con, kid):
    fil_id, _ = sidelager.lagre_fil(con, kid, "Øvelse på Åsane – ærlig talt.pdf", pdf(), "admin:test")
    skriv_side(con, kid, dokument({"id": "b_filer001", "type": "filer", "tittel": "F", "data": {"filer": [{"fil_id": fil_id, "tittel": "Ø"}]}}))
    r = _fil(deltaker_klient(_meld(con, kid)), "K1", fil_id)
    disp = r.headers["Content-Disposition"]
    assert "filename*=UTF-8''%C3%98velse%20p%C3%A5%20%C3%85sane%20%E2%80%93%20%C3%A6rlig%20talt.pdf" in disp
    assert re.search(r'filename="[\x20-\x7e]+"', disp) and '"' not in disp.split('filename="')[1].split('"')[0]
    assert "\r" not in disp and "\n" not in disp


def test_bilder_vises_inline_med_sandbox_og_bare_naar_de_er_i_en_synlig_bildeblokk(con, kid):
    bilde, _ = sidelager.lagre_fil(con, kid, "rom.png", png(), "admin:test")
    skjult, _ = sidelager.lagre_fil(con, kid, "skjult.gif", gif(), "admin:test")
    skriv_side(con, kid, dokument({"id": "b_bilde001", "type": "bilde", "tittel": "Kurslokalet", "data": {"fil_id": bilde, "alt": "Rommet", "tekst": "Rom 3", "plassering": "bred"}},
                                  {"id": "b_bilde002", "type": "bilde", "tittel": "Skjult", "skjult": True, "data": {"fil_id": skjult, "alt": "x"}}))
    k = deltaker_klient(_meld(con, kid))
    r = _fil(k, "K1", bilde)
    assert r.status_code == 200 and r.headers["Content-Type"] == "image/png" and r.headers["Content-Disposition"] == "inline"
    assert r.headers["Content-Security-Policy"] == "sandbox; default-src 'none'; img-src 'self'" and r.headers["Cache-Control"] == "private, max-age=300"
    assert _fil(k, "K1", skjult).status_code == 404
    html = _hent(k).get_data(as_text=True)
    assert f'src="/kurs/K1/deltakerside/fil/{bilde}"' in html and 'alt="Rommet"' in html and "Rom 3" in html


# ============================ innholdet på siden ============================

def test_siden_viser_aldri_intern_informasjon_eller_andres_opplysninger(con, kid):
    """Aldri på siden: kursnotat, allergier og tilrettelegging, intern kommentar, innsjekk-token og -kode, og andre deltakeres opplysninger.
    Deltakerens EGNE opplysninger står bare i kortet «Mine opplysninger» (egne tester i test_min_side_kurs.py)."""
    con.execute("UPDATE kurs SET notat='HEMMELIG KURSNOTAT om Lunsj' WHERE id=?", (kid,))
    did, pid = lag_deltaker(con, kid, "kari@example.no")
    db.oppdater_sensitivt(con, pid, "HEMMELIG ALLERGI nøtter", "HEMMELIG TILRETTELEGGING rullestol", "admin:test")
    con.execute("UPDATE paamelding SET intern_kommentar='HEMMELIG INTERN', faktura_adresse='Egenveien 1', faktura_epost='egen-faktura@example.no' WHERE id=?", (pid,))
    con.execute("UPDATE deltaker SET telefon='000 11 222', arbeidssted='Eget Arbeidssted AS' WHERE id=?", (did,))
    andre_did, andre_pid = lag_deltaker(con, kid, "ola@example.no", "Ola", "Annenmann")
    con.execute("UPDATE paamelding SET faktura_adresse='Andreveien 9', faktura_epost='andre-faktura@example.no' WHERE id=?", (andre_pid,))
    con.execute("UPDATE deltaker SET telefon='999 88 777', arbeidssted='Andres Arbeidssted AS' WHERE id=?", (andre_did,))
    con.commit()
    _side(con, kid)
    kd = con.execute("SELECT innsjekk_token, innsjekk_kode FROM kursdag WHERE kurs_id=?", (kid,)).fetchall()
    r = _hent(deltaker_klient(did))
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    for hemmelig in ("HEMMELIG", "Annenmann", "ola@example.no", "Andreveien", "andre-faktura", "999 88 777", "Andres Arbeidssted"):
        assert hemmelig not in html, hemmelig
    for dag in kd:
        assert dag["innsjekk_token"] not in html and f'"{dag["innsjekk_kode"]}"' not in html and f">{dag['innsjekk_kode']}<" not in html
    kort = re.search(r'<section class="dp-blokk dp-person".*?</section>', html, re.S).group(0)
    utenom = html.replace(kort, "")
    for egen in ("Egenveien", "egen-faktura", "000 11 222", "Eget Arbeidssted"):          # hennes egne opplysninger: i kortet, ikke andre steder
        assert egen in kort and egen not in utenom, egen


def test_manipulert_publisert_json_med_script_vises_renset(con, kid):
    did = _meld(con, kid)
    ond = {"v": 1, "tittel": "<script>alert('tittel')</script>", "ingress": "<img src=x onerror=alert(1)>", "rom": "<b>Rom</b>",
           "melding": {"tekst": "<script>alert('melding')</script>", "niva": "info"}, "blokker": [
               {"id": "b_ond00001", "type": "tekst", "tittel": "<script>alert(1)</script>", "data": {"html": "<p>Fin</p><script>alert('html')</script><img src=x onerror=alert(2)><a href=\"javascript:alert(3)\" onclick=\"alert(4)\">klikk</a>"}},
               {"id": "b_ond00002", "type": "lenker", "tittel": "L", "data": {"lenker": [{"tittel": "Farlig", "url": "javascript:alert(5)", "tekst": "<script>x</script>"}]}},
               {"id": "b_ond00003", "type": "kontakt", "tittel": "K", "data": {"personer": [{"navn": "<i>Navn</i>", "telefon": "javascript:1", "epost": "\"><script>alert(6)</script>"}]}}]}
    con.execute("INSERT INTO kursside (kurs_id, utkast, publisert, versjon, publisert_versjon) VALUES (?,?,?,1,1)", (kid, si.til_json(ond), json.dumps(ond)))
    con.commit()
    html = _hent(deltaker_klient(did)).get_data(as_text=True)
    from html.parser import HTMLParser
    funnet = {"tagger": [], "attributter": [], "hrefs": [], "skript": [], "bilder": []}

    class Leser(HTMLParser):
        skript = False

        def handle_starttag(self, tag, attrs):
            funnet["tagger"].append(tag)
            if tag == "img":
                funnet["bilder"].append(dict(attrs))
            funnet["attributter"] += [a for a, _ in attrs]
            funnet["hrefs"] += [v for a, v in attrs if a in ("href", "src")]
            self.skript = tag == "script"

        def handle_data(self, data):
            if self.skript and data.strip():
                funnet["skript"].append(data)

        def handle_endtag(self, tag):
            self.skript = False

    Leser().feed(html)
    ukjente_bilder = [b for b in funnet["bilder"] if not (b.get("class") == "ta-logo" and (b.get("src") or "").startswith("/static/logo/"))]
    assert ukjente_bilder == [] and funnet["skript"] == [] and not [a for a in funnet["attributter"] if a.startswith("on")]      # bare systemets egen logo er et bilde
    assert not [h for h in funnet["hrefs"] if h.lower().startswith(("javascript:", "data:", "vbscript:"))]
    assert "&lt;script&gt;alert" in html and "Fin" in html and "klikk" in html                      # ren tekst er escapet, rik tekst renset


def test_skjulte_og_tomme_blokker_finnes_ikke_i_html_og_rekkefolgen_beholdes(con, kid):
    dok = dokument(tekstblokk("Foerste", "<p>en</p>", id="b_foerste1"), tekstblokk("Skjult", "<p>skjult tekst</p>", id="b_skjult01", skjult=True),
                   tekstblokk("Tom", "<p></p>", id="b_tomtom01"), tekstblokk("Senere", "<p>senere tekst</p>", id="b_senere01", vis_fra=(IDAG + timedelta(days=1)).isoformat()),
                   tekstblokk("Tredje", "<p>tre</p>", id="b_tredje01"), tekstblokk("Ikke i meny", "<p>x</p>", id="b_ikkemeny", i_meny=False))
    skriv_side(con, kid, dok)
    html = _hent(deltaker_klient(_meld(con, kid))).get_data(as_text=True)
    assert "skjult tekst" not in html and "senere tekst" not in html and 'id="blokk-b_tomtom01"' not in html and 'id="blokk-b_skjult01"' not in html
    assert html.index('id="blokk-b_foerste1"') < html.index('id="blokk-b_tredje01"')
    meny = html.split('aria-labelledby="dp-meny-h"')[1].split("</nav>")[0]
    assert "#blokk-b_foerste1" in meny and "#blokk-b_tredje01" in meny and "b_ikkemeny" not in meny


def test_side_uten_synlige_blokker_viser_ferdig_tekst(con, kid):
    skriv_side(con, kid, dokument(tekstblokk("Skjult", skjult=True)))
    assert "ikke lagt ut noe mer ennå" in _hent(deltaker_klient(_meld(con, kid))).get_data(as_text=True)


def test_siden_har_maler_for_mobil_og_pc_og_ingen_horisontal_styring_i_html(con, kid):
    _side(con, kid)
    html = _hent(deltaker_klient(_meld(con, kid))).get_data(as_text=True)
    assert 'class="dp-rot"' in html and 'class="dp-grid"' in html and 'class="dp-hoved"' in html and 'aria-label="Oppsummering"' in html
    tekst = (Path(deltakerside.__file__).parent / "web" / "static" / "kursside-delt.css").read_text(encoding="utf-8")
    assert "container-type: inline-size" in tekst and "@container dp (min-width: 880px)" in tekst and "@container dp (max-width: 720px)" in tekst


# ============================ gamle sider med «kursholders filer» ============================
# (06.10.2026: kursholder-lenken og SharePoint er tatt bort - testene av listen «Fra kursholder» er fjernet)

def test_gammel_lagret_side_med_sharepoint_valget_og_uten_egne_filer_viser_ingen_filblokk(con, kid):
    """En side lagret før 06.10.2026 kan ha «sharepoint»: true i Filer-blokken. Feltet leses ikke lenger: blokken uten egne
    filer vises ikke, og «Fra kursholder» kommer aldri tilbake."""
    skriv_side(con, kid, dokument(tekstblokk()))
    gammel = dokument({"id": "b_filer001", "type": "filer", "tittel": "Presentasjoner og dokumenter", "data": {"filer": [], "sharepoint": True}})
    con.execute("UPDATE kursside SET publisert=? WHERE kurs_id=?", (json.dumps(gammel), kid))
    con.commit()
    r = _hent(deltaker_klient(_meld(con, kid)))
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "Fra kursholder" not in html and 'id="blokk-b_filer001"' not in html


# ============================ landing og visningsdata ============================

def _kurs_med_side(con, kode, *, start=IDAG, dager=2, publiser=True):
    kid = lag_kurs(con, kode, start=start, dager=dager)
    skriv_side(con, kid, dokument(tekstblokk()), publiser=publiser)
    return kid


def _status(con, kid, status):
    """Kursstatus settes ETTER at deltakerne er påmeldt (et avlyst eller avsluttet kurs tar ikke imot påmeldinger)."""
    con.execute("UPDATE kurs SET status=? WHERE id=?", (status, kid))
    con.commit()


def test_landing_ett_kurs_flere_kurs_og_ingen(con):
    a = _kurs_med_side(con, "A", start=IDAG - timedelta(days=1), dager=3)                          # kursdag i dag
    b = _kurs_med_side(con, "B", start=IDAG + timedelta(days=30))
    did, _ = lag_deltaker(con, a, "kari@example.no")
    assert deltakerside.landing(con, did, IDAG) == "A"
    lag_deltaker(con, b, "kari@example.no")
    assert deltakerside.landing(con, did, IDAG) == "A"                                            # flere: det med kursdag i dag
    assert deltakerside.landing(con, did, IDAG + timedelta(days=10)) is None                      # ingen kursdag i dag i noen av dem
    assert deltakerside.landing(con, 9999, IDAG) is None


def test_landing_ignorerer_kurs_uten_tilgang(con):
    a = _kurs_med_side(con, "A")
    b = _kurs_med_side(con, "B", publiser=False)                                                   # ikke publisert
    c = _kurs_med_side(con, "C")
    sidelager.sett_aktiv(con, c, False, "admin:test")
    d = lag_kurs(con, "D")                                                                         # ingen side
    e = _kurs_med_side(con, "E")
    did, _ = lag_deltaker(con, a, "kari@example.no")
    for kurs in (b, c, d, e):
        lag_deltaker(con, kurs, "kari@example.no")
    _status(con, e, "avlyst")
    lag_deltaker(con, _kurs_med_side(con, "F"), "annen@example.no")                                # en annens kurs
    assert deltakerside.landing(con, did, IDAG) == "A"


def test_landing_venteliste_teller_ikke_og_avsluttet_kurs_brukes_bare_hvis_det_er_det_eneste(con):
    ferdig = _kurs_med_side(con, "FERDIG", start=IDAG - timedelta(days=30))
    venter = _kurs_med_side(con, "VENTER")
    did, _ = lag_deltaker(con, ferdig, "kari@example.no")
    lag_deltaker(con, venter, "kari@example.no", status="venteliste")
    _status(con, ferdig, "avsluttet")
    assert deltakerside.landing(con, did, IDAG) == "FERDIG"
    ferdig2 = _kurs_med_side(con, "FERDIG2", start=IDAG - timedelta(days=20))
    lag_deltaker(con, ferdig2, "kari@example.no")
    _status(con, ferdig2, "avsluttet")
    assert deltakerside.landing(con, did, IDAG) is None                                            # to avsluttede: Min side


def test_tilgang_gir_arsak_og_stengedato(con, kid):
    did = _meld(con, kid)
    assert deltakerside.tilgang(con, did, "K1", IDAG).arsak == "ikke_publisert"
    _side(con, kid)
    t = deltakerside.tilgang(con, did, "K1", IDAG)
    assert (t.ok, t.arsak, t.kurs["kode"]) == (True, "ok", "K1") and t.stenges == sidelager.siste_kursdag(con, kid) + timedelta(days=180)
    assert "notat" not in t.kurs.keys()                                                            # kursraden har aldri notatet
    assert deltakerside.tilgang(con, did, "K1", t.stenges + timedelta(days=1)).arsak == "stengt"
    assert deltakerside.tilgang(con, did, "X", IDAG).arsak == "ikke_funnet" and deltakerside.tilgang(con, 999, "K1", IDAG).arsak == "ikke_funnet"
    assert deltakerside.har_side(con, kid, IDAG) and not deltakerside.har_side(con, kid, t.stenges + timedelta(days=1)) and not deltakerside.har_side(con, 999, IDAG)


def _visning(con, kid, did, dok=None, **kw):
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    dok = dok or sidelager.hent_publisert(con, kid)
    return deltakerside.bygg_visning(con, kurs, dok, deltaker_id=did, idag=kw.pop("idag", IDAG), forhandsvisning=kw.pop("forhandsvisning", False),
                                     fil_url=lambda f: f"/fil/{f}", **kw)


def test_kursdager_faar_status_mott_ikke_og_kommer_og_dagens_dag_utheves(con):
    kid = lag_kurs(con, "K1", start=IDAG - timedelta(days=1), dager=4)                             # i går, i dag, i morgen, i overmorgen
    did, pid = lag_deltaker(con, kid)
    dager = db.kursdager(con, kid)
    db.registrer_oppmote(con, pid, dager[0]["id"], "qr")
    skriv_side(con, kid, dokument(tekstblokk()))
    v = _visning(con, kid, did)
    assert [d["status"] for d in v["kursdager"]] == ["mott", "ikke", "kommer", "kommer"] and [d["i_dag"] for d in v["kursdager"]] == [False, True, False, False]
    assert v["i_dag"]["nr"] == 2 and v["i_dag"]["av"] == 4 and v["i_dag"]["registrert"] is None and v["i_dag"]["tilbys"] is True
    assert {"Dag 2 av 4", "Du er påmeldt"} <= {p["tekst"] for p in v["pillene"]} and v["neste_dag"] is None
    db.registrer_oppmote(con, pid, dager[1]["id"], "kode")
    con.commit()
    assert _visning(con, kid, did)["i_dag"]["registrert"] is not None and _visning(con, kid, did)["kursdager"][1]["status"] == "mott"


def test_visningen_har_aldri_innsjekk_kode_eller_token(con, kid):
    did = _meld(con, kid)
    _side(con, kid)
    v = _visning(con, kid, did)
    tekst = json.dumps(v, default=str)
    for dag in con.execute("SELECT innsjekk_token, innsjekk_kode FROM kursdag"):
        assert dag["innsjekk_token"] not in tekst and dag["innsjekk_kode"] not in tekst
    assert not ({"innsjekk_kode", "innsjekk_token", "notat", "epost"} & set(json.loads(tekst)["kurs"]))


def test_neste_kursdag_og_avsluttet_kurs(con):
    kid = lag_kurs(con, "K1", start=IDAG + timedelta(days=10), dager=2)
    did, _ = lag_deltaker(con, kid)
    skriv_side(con, kid, dokument(tekstblokk()))
    v = _visning(con, kid, did)
    assert v["i_dag"] is None and v["neste_dag"]["dag_nr"] == 1 and v["neste_dag"]["av"] == 2 and v["avsluttet"] is False
    v = _visning(con, kid, did, idag=IDAG + timedelta(days=30))
    assert v["avsluttet"] is True and v["i_dag"] is None and v["neste_dag"] is None and "Kurset er avsluttet" in {p["tekst"] for p in v["pillene"]}
    assert all(d["status"] == "ikke" for d in v["kursdager"])


def test_programmet_apner_dagens_dag_ellers_neste_og_pauser_mister_sted_og_hvem(con):
    kid = lag_kurs(con, "K1", start=IDAG - timedelta(days=1), dager=3)
    did, _ = lag_deltaker(con, kid)
    dager = [d["id"] for d in db.kursdager(con, kid)]
    dok = dokument({"id": "b_program1", "type": "program", "tittel": "Program", "data": {"dager": [
        {"kursdag_id": dager[0], "punkter": [{"fra": "09:00", "til": "10:00", "tema": "Start", "sted": "Rom 1", "hvem": "Kari"}]},
        {"kursdag_id": dager[1], "punkter": [{"fra": "09:00", "til": "09:15", "tema": "Pause", "sted": "Kantina", "hvem": "Alle", "pause": True}]},
        {"kursdag_id": dager[2], "punkter": [{"tema": "Uten tid"}]}]}})
    skriv_side(con, kid, dok)
    v = _visning(con, kid, did)
    p = v["blokker"][0]["data"]["dager"]
    assert [d["apen"] for d in p] == [False, True, False] and [d["i_dag"] for d in p] == [False, True, False] and p[1]["tittel"] == "Dag 2"
    assert p[1]["punkter"][0] == {"tid": "09:00–09:15", "tema": "Pause", "sted": "", "hvem": "", "pause": True}
    assert p[0]["punkter"][0]["tid"] == "09:00–10:00" and p[2]["punkter"][0]["tid"] == "" and p[0]["dato_lang"].endswith("mars")
    v = _visning(con, kid, did, idag=IDAG - timedelta(days=5))                                    # før kurset: første kommende dag åpnes
    assert [d["apen"] for d in v["blokker"][0]["data"]["dager"]] == [True, False, False]
    v = _visning(con, kid, did, idag=IDAG + timedelta(days=30))                                   # etter kurset: alle lukket
    assert [d["apen"] for d in v["blokker"][0]["data"]["dager"]] == [False, False, False]


def test_filgrupper_i_kursdagsrekkefolge_ugrupperte_sist_og_norsk_metatekst(con, kid):
    did = _meld(con, kid)
    dager = [d["id"] for d in db.kursdager(con, kid)]
    a, _ = sidelager.lagre_fil(con, kid, "a.pdf", pdf(b"a"), "admin:test")
    b, _ = sidelager.lagre_fil(con, kid, "b.pdf", pdf(b"b" * 2000), "admin:test")
    c, _ = sidelager.lagre_fil(con, kid, "c.png", png(), "admin:test")
    dok = dokument({"id": "b_filer001", "type": "filer", "tittel": "F", "data": {"filer": [
        {"fil_id": c, "tittel": "Ugruppert"}, {"fil_id": b, "tittel": "Dag 2-fil", "gruppe": dager[1]}, {"fil_id": a, "tittel": "Dag 1-fil", "gruppe": dager[0],
                                                                                                               "synlig_fra": (IDAG - timedelta(days=1)).isoformat()}]}})
    skriv_side(con, kid, dok)
    g = _visning(con, kid, did)["blokker"][0]["data"]["grupper"]
    assert [x["tittel"] for x in g][:2] == [f"Dag 1 · {deltakerside.dato_lang(IDAG)}", f"Dag 2 · {deltakerside.dato_lang(IDAG + timedelta(days=1))}"]
    assert g[2]["tittel"] == "Øvrige filer" and g[2]["filer"][0]["tittel"] == "Ugruppert"       # egen overskrift, så de ikke ser ut til å høre til dag 2
    dag1 = g[0]["filer"][0]
    assert dag1["url"] == f"/fil/{a}" and dag1["type_kode"] == "PDF" and dag1["meta"].startswith("PDF · 1 kB · lagt ut i går") and dag1["ny"] is True
    assert g[1]["filer"][0]["meta"].startswith("PDF · 2 kB") and g[2]["filer"][0]["type_kode"] == "BILDE"


def test_lenker_zoom_loses_ved_visning_og_utelates_for_avsluttet_kurs(con, kid):
    did = _meld(con, kid)
    con.execute("UPDATE kurs SET zoom_url='https://zoom.example/j/123' WHERE id=?", (kid,))
    dok = dokument({"id": "b_lenker01", "type": "lenker", "tittel": "Lenker", "data": {"lenker": [
        {"tittel": "Zoom", "url": "", "kilde": "zoom"}, {"tittel": "Artikkel", "url": "https://example.com/a", "nivaa": "obligatorisk"},
        {"tittel": "Mer", "url": "https://www.example.com/b", "nivaa": "anbefalt", "tekst": "Leses etterpå"}, {"tittel": "Post", "url": "mailto:kurs@example.no"}]}})
    skriv_side(con, kid, dok)
    g = _visning(con, kid, did)["blokker"][0]["data"]["grupper"]
    assert [x["tittel"] for x in g] == [None, "Obligatorisk", "Anbefalt"]
    assert g[0]["lenker"][0]["url"] == "https://zoom.example/j/123" and g[0]["lenker"][0]["domene"] == "zoom.example" and g[0]["lenker"][1]["ekstern"] is False
    assert g[2]["lenker"][0]["domene"] == "example.com" and g[2]["lenker"][0]["tekst"] == "Leses etterpå"
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    g = _visning(con, kid, did, idag=IDAG + timedelta(days=30))["blokker"][0]["data"]["grupper"]
    assert all(l["url"] != "https://zoom.example/j/123" for x in g for l in x["lenker"])
    con.execute("UPDATE kurs SET zoom_url=NULL WHERE id=?", (kid,))
    g = _visning(con, kid, did)["blokker"][0]["data"]["grupper"]
    assert "Zoom" not in json.dumps(g)                                                              # ingen adresse: lenken vises ikke


def test_toppbilde_tas_ut_av_flyten_og_kontakt_faar_initialer_og_tel_lenke(con, kid):
    did = _meld(con, kid)
    b1, _ = sidelager.lagre_fil(con, kid, "a.png", png(3, 3), "admin:test")
    b2, _ = sidelager.lagre_fil(con, kid, "b.png", png(5, 5), "admin:test")
    dok = dokument({"id": "b_bilde001", "type": "bilde", "tittel": "Banner", "data": {"fil_id": b1, "alt": "Banner", "tekst": "Tekst", "plassering": "topp"}},
                   {"id": "b_bilde002", "type": "bilde", "tittel": "Andre topp", "data": {"fil_id": b2, "alt": "Andre", "plassering": "topp"}},
                   {"id": "b_kontakt1", "type": "kontakt", "tittel": "Kontakt", "data": {"personer": [
                       {"navn": "Marte Solberg", "rolle": "Kursleder", "telefon": "+47 555 12 345", "epost": "marte@example.no"}, {"navn": "", "rolle": "", "telefon": "", "epost": ""}]}})
    skriv_side(con, kid, dok)
    v = _visning(con, kid, did)
    assert v["topp_bilde"] == {"url": f"/fil/{b1}", "alt": "Banner", "tekst": "Tekst"}
    assert [(b["id"], b["data"]["plassering"]) for b in v["blokker"] if b["type"] == "bilde"] == [("b_bilde002", "bred")]
    p = next(b for b in v["blokker"] if b["type"] == "kontakt")["data"]["personer"]
    assert len(p) == 1 and p[0]["initialer"] == "MS" and p[0]["tel_url"] == "tel:+4755512345"


def test_bilde_blokk_med_fil_som_er_borte_vises_ikke(con, kid):
    did = _meld(con, kid)
    b1, _ = sidelager.lagre_fil(con, kid, "a.png", png(), "admin:test")
    skriv_side(con, kid, dokument({"id": "b_bilde001", "type": "bilde", "tittel": "B", "data": {"fil_id": b1, "alt": "x"}}, tekstblokk()))
    con.execute("DELETE FROM kursside_fil_innhold WHERE fil_id=?", (b1,))
    con.execute("DELETE FROM kursside_fil WHERE id=?", (b1,))
    con.commit()
    assert [b["type"] for b in _visning(con, kid, did)["blokker"]] == ["tekst"]


def test_tittel_faller_tilbake_til_kursnavn_og_meldingen_vises_bare_med_tekst(con, kid):
    did = _meld(con, kid)
    skriv_side(con, kid, dokument(tekstblokk(), melding={"tekst": "", "niva": "viktig"}))
    v = _visning(con, kid, did)
    assert v["kurs"]["tittel"] == "Kurs K1" and v["melding"] is None and v["kurs"]["navn"] == "Kurs K1"
    skriv_side(con, kid, dokument(tekstblokk(), tittel="Egen tittel", ingress="Ingress", rom="Rom 3", melding={"tekst": "Ta med kaffekopp", "niva": "viktig"}))
    v = _visning(con, kid, did)
    assert v["kurs"]["tittel"] == "Egen tittel" and v["kurs"]["rom"] == "Rom 3" and v["melding"] == {"tekst": "Ta med kaffekopp", "niva": "viktig"}
    assert v["kurs"]["sted"] == "Bergen" and v["kurs"]["periode"] and v["kurs"]["tid"] == "09:00–16:00"


def test_forhandsvisning_gir_deaktivert_innsjekk_og_ingen_deltakerdata(con, kid):
    _meld(con, kid)
    _side(con, kid)
    v = _visning(con, kid, None, forhandsvisning=True)
    assert v["forhandsvisning"] is True and v["i_dag"]["tilbys"] is False and "forhåndsvisningen" in v["i_dag"]["forklaring"]
    assert all(p["tekst"] != "Du er påmeldt" for p in v["pillene"]) and v["forh_tekst"]


def test_oppmote_i_dag_for_digitale_kurs_med_zoom_tilbyr_ikke_innsjekk(con):
    kid = lag_kurs(con, "K1")
    con.execute("UPDATE kurs SET type='digital', zoom_id='123456' WHERE id=?", (kid,))
    did, _ = lag_deltaker(con, kid)
    _side(con, kid)
    kort = _visning(con, kid, did)["i_dag"]
    assert kort["tilbys"] is False and "automatisk" in kort["forklaring"] and kort["krever_kode"] is False
    con.execute("UPDATE kurs SET zoom_id=NULL WHERE id=?", (kid,))
    kort = _visning(con, kid, did)["i_dag"]
    assert kort["tilbys"] is True and kort["krever_kode"] is False and kort["forklaring"] is None                # én knapp uten kode
    con.execute("UPDATE kurs SET type='fysisk' WHERE id=?", (kid,))
    assert _visning(con, kid, did)["i_dag"]["krever_kode"] is True
    sidelager.sett_innstillinger(con, kid, innsjekk_krever_kode=False, aktor="admin:test")
    assert _visning(con, kid, did)["i_dag"]["krever_kode"] is False


def test_norske_datoer_og_storrelser():
    assert deltakerside.dato_lang(IDAG) == "onsdag 10. mars" and deltakerside.dato_lang(IDAG, aar=True) == "onsdag 10. mars 2027"
    assert deltakerside.dato_kort(IDAG) == "ons 10. mars" and deltakerside.dag_maaned(IDAG) == "10. mars"
    assert (deltakerside.storrelse_tekst(412 * 1024), deltakerside.storrelse_tekst(int(8.1 * 1024 * 1024)), deltakerside.storrelse_tekst(10)) == ("412 kB", "8,1 MB", "1 kB")
    assert deltakerside.filtype("a.PPTX", "dokument") == ("PPT", "ppt", "PowerPoint") and deltakerside.filtype("a.odp", "dokument")[2] == "Presentasjon"
    assert deltakerside.filtype("a.docx", "dokument")[:2] == ("DOC", "doc") and deltakerside.filtype("a.xlsx", "dokument")[:2] == ("XLS", "xls")
    assert deltakerside.filtype("a.zip", "dokument")[0] == "ZIP" and deltakerside.filtype("a.txt", "dokument")[0] == "TXT" and deltakerside.filtype("a.png", "bilde")[2] == "Bilde"
