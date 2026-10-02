"""Min side for ett kurs: den personlige lenken, tilgangsnivåene, kortet «Mine opplysninger», e-postknappen og administrasjonen.
Bare oppdiktede data.

Min side er siden deltakeren ser for ett kurs (felles innhold + innsjekk + opplysninger om seg selv). Hver deltaker har en personlig lenke
(`/min/<lenke>`) som står i e-postene. Lenken er signert og lagres ikke, virker til 30 dager etter siste kursdag, og gir nivået «lenke»:
alt unntatt privatadresse, fakturaer og kursbevis. Dem ser deltakeren først etter å ha bekreftet e-posten (nivå «epost»). Se kurs/minside.py.
"""
import json
import re
from datetime import date, timedelta

import pytest

from kurs import config, daglig, db, lenker, maltekster, minside
from kurs.integrasjoner import epost
from kurs.kjoring import Kjoring
from kurs.web import sikkerhet

from kurssidehjelp import (IDAG, admin_klient, deltaker_klient, dokument, fast_dato, lag_deltaker, lag_kurs, ny_database, skriv_side,
                           tekstblokk)

BASE = config.BASE_URL
PRIVAT_ADRESSE = "Standardveien 1"            # standardadressen testoppsettet (conftest) gir alle som registreres uten egen adresse


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    fast_dato(monkeypatch)
    yield c
    c.close()


@pytest.fixture
def kid(con):
    return lag_kurs(con, "K1")                 # kursdager 10. og 11. mars 2027; «i dag» er 10. mars


@pytest.fixture
def sendt(monkeypatch):
    """Fanger (til, emne, html) fra epost.send: ingenting sendes."""
    ut = []
    monkeypatch.setattr(epost, "send", lambda til, emne, html, *a, **kw: ut.append((til, emne, html)))
    return ut


def _side(con, kid, **kw):
    return skriv_side(con, kid, dokument(tekstblokk("Litteratur", "<p>Les kapittel 3 før kurset</p>")), **kw)


def _klient():
    from kurs.web import app as webapp
    return webapp.app.test_client()


def _sti(con, pid) -> str:
    """Den personlige lenken til påmeldingen som en sti (uten BASE_URL)."""
    return minside.url(con, pid).removeprefix(BASE)


def _lenkeklient(con, pid):
    """En nettleser som har åpnet den personlige lenken: nivå «lenke»."""
    k = _klient()
    assert k.get(_sti(con, pid)).status_code == 302
    return k


def _niva(klient):
    with klient.session_transaction() as s:
        return s.get("deltaker_niva")


def _html(klient, kode="K1") -> str:
    r = klient.get(f"/kurs/{kode}/deltakerside")
    assert r.status_code == 200
    return r.get_data(as_text=True)


def _kort(html: str) -> str:
    """Bare kortet «Mine opplysninger»."""
    treff = re.search(r'<section class="dp-blokk dp-person".*?</section>', html, re.S)
    assert treff, "kortet «Mine opplysninger» mangler"
    return treff.group(0)


def _tekst_i_404(svar) -> str:
    treff = re.search(r"<h1>(.*?)</h1>\s*<p>(.*?)</p>", svar.get_data(as_text=True), re.S)
    return re.sub(r"\s+", " ", f"{treff.group(1)}|{treff.group(2)}") if treff else ""


def _firma(con, pid):
    con.execute("""UPDATE paamelding SET betaler='organisasjon', org_navn='Eksempelfirma AS', org_nr='999888777', faktura_epost='regnskap@firma.example',
                   faktura_adresse='Firmaveien 2', faktura_postnr='5003', faktura_sted='Bergen', faktura_ref='REF-77', ehf=1 WHERE id=?""", (pid,))
    con.commit()


# ============================ den personlige lenken ============================

def test_lenken_er_signert_stabil_og_lagres_ikke(con, kid):
    did, pid = lag_deltaker(con, kid)
    _, pid2 = lag_deltaker(con, kid, "ola@example.no", "Ola", "Hansen")
    a = minside.url(con, pid)
    assert re.fullmatch(re.escape(BASE) + r"/min/\d+\.0\.[0-9a-f]{32}", a)
    assert a == minside.url(con, pid) and minside.url(con, pid2) != a              # regnes ut: samme hver gang, ulik per påmelding
    assert con.execute("SELECT COUNT(*) FROM min_side_lenke").fetchone()[0] == 0   # ingenting lagres før en administrator stenger eller fornyer


def test_lenken_aapner_min_side_uten_innlogging_paa_nivaa_lenke(con, kid):
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    k = _klient()
    assert k.get("/kurs/K1/deltakerside").status_code == 302                       # uten lenke eller innlogging: til innlogging
    r = k.get(_sti(con, pid))
    assert r.status_code == 302 and r.headers["Location"].endswith("/kurs/K1/deltakerside")
    assert r.headers["Referrer-Policy"] == "no-referrer" and r.headers["Cache-Control"] == "no-store"
    with k.session_transaction() as s:
        assert s["deltaker_id"] == did and s["deltaker_niva"] == minside.NIVAA_LENKE and s.permanent
    html = _html(k)
    assert "Les kapittel 3 før kurset" in html and 'id="mine-opplysninger"' in html
    assert "/min/" not in html                                                      # lenken står ikke igjen på siden (Referer, skjermbilder)


def test_alle_lenker_som_ikke_virker_gir_samme_404_uten_a_si_hvorfor(con, kid, monkeypatch):
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    _, venter = lag_deltaker(con, kid, "vera@example.no", "Vera", "Venteliste", status="venteliste")
    gyldig = minside.url(con, pid).removeprefix(f"{BASE}/min/")
    forsok = {"søppel": "abc", "tom signatur": f"{pid}.0.{'0' * 32}", "endret signatur": gyldig[:-1] + ("0" if gyldig[-1] != "0" else "1"),
              "annen påmelding med min signatur": gyldig.replace(f"{pid}.", f"{venter}.", 1),
              "påmelding som ikke finnes": lenker.min_side_token(99999, 0), "ikke bekreftet": lenker.min_side_token(venter, 0),
              "gammel versjon": lenker.min_side_token(pid, 5)}
    klienter = {navn: _klient() for navn in forsok}
    svar = {navn: klienter[navn].get(f"/min/{t}") for navn, t in forsok.items()}
    minside.steng(con, pid, "admin:test")
    con.commit()
    klienter["stengt"] = _klient()
    svar["stengt"] = klienter["stengt"].get(f"/min/{gyldig}")
    assert minside.url(con, pid) is None
    minside.ny_lenke(con, pid, "admin:test")
    con.commit()
    klienter["byttet ut med en nyere"] = _klient()
    svar["byttet ut med en nyere"] = klienter["byttet ut med en nyere"].get(f"/min/{gyldig}")
    fast_dato(monkeypatch, date(2027, 4, 11))                                       # dagen etter siste gyldige dag
    klienter["utløpt"] = _klient()
    svar["utløpt"] = klienter["utløpt"].get(_sti(con, pid))
    for navn, r in svar.items():
        assert r.status_code == 404, navn
        with klienter[navn].session_transaction() as s:
            assert "deltaker_id" not in s, navn                                     # en lenke som ikke virker, gir aldri en økt
    assert len({_tekst_i_404(r) for r in svar.values()}) == 1                       # samme tekst: lenken avslører ikke hvorfor den ikke virker
    assert "Lenken virker ikke" in _tekst_i_404(svar["søppel"])


def test_lenken_virker_til_30_dager_etter_siste_kursdag(con, kid, monkeypatch):
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    assert minside.utloper(con, pid) == date(2027, 4, 10)                           # siste kursdag 11. mars + 30 dager
    fast_dato(monkeypatch, date(2027, 4, 10))
    assert _klient().get(_sti(con, pid)).status_code == 302
    fast_dato(monkeypatch, date(2027, 4, 11))
    assert _klient().get(_sti(con, pid)).status_code == 404


def test_kurs_uten_kursdager_gir_en_reservefrist_fra_paameldingen(con):
    kid = lag_kurs(con, "K2", dager=0)
    did, pid = lag_deltaker(con, kid)
    opprettet = con.execute("SELECT opprettet FROM paamelding WHERE id=?", (pid,)).fetchone()[0]
    assert minside.utloper(con, pid) == date.fromisoformat(opprettet[:10]) + timedelta(days=minside.RESERVE_DAGER)


def test_anonymisert_deltaker_og_deltaker_uten_plass_har_ingen_virkende_lenke(con, kid):
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    gaar, gaar_pid = lag_deltaker(con, kid, "gunnar@example.no", "Gunnar", "Gaar", status="avmeldt")
    assert _klient().get(_sti(con, gaar_pid)).status_code == 404
    con.execute("UPDATE deltaker SET epost=? WHERE id=?", (f"anonymisert-{did}@{db.ANONYM_DOMENE}", did))
    con.commit()
    assert _klient().get(_sti(con, pid)).status_code == 404


def test_lenke_til_kurs_uten_publisert_side_sier_at_siden_ikke_er_aapnet(con, kid):
    did, pid = lag_deltaker(con, kid)
    k = _lenkeklient(con, pid)
    r = k.get("/kurs/K1/deltakerside")
    assert r.status_code == 404 and "Min side er ikke åpnet ennå" in r.get_data(as_text=True)


def test_den_som_alt_er_innlogget_med_e_post_beholder_det_hoye_nivaaet(con, kid):
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    k = deltaker_klient(did)                                                        # innlogget med e-post (økter uten nivå regnes som e-post)
    assert k.get(_sti(con, pid)).status_code == 302
    assert _niva(k) is None and PRIVAT_ADRESSE in _kort(_html(k))
    annen, annen_pid = lag_deltaker(con, kid, "ola@example.no", "Ola", "Hansen")
    k.get(_sti(con, annen_pid))                                                     # en annen persons lenke bytter økt, og nivået blir «lenke»
    assert _niva(k) == minside.NIVAA_LENKE


def test_lenken_er_takbegrenset_per_ip(con):
    k = _klient()
    for _ in range(sikkerhet.GRENSER["min_side_lenke"][0]):
        assert k.get("/min/ugyldig").status_code == 404
    assert k.get("/min/ugyldig").status_code == 429


# ============================ nivåene ============================

def test_mine_opplysninger_viser_det_deltakeren_selv_ga(con, kid):
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    con.execute("UPDATE deltaker SET telefon='900 11 222', arbeidssted='Eksempelklinikken', yrkestittel='Psykolog' WHERE id=?", (did,))
    con.commit()
    kort = _kort(_html(deltaker_klient(did)))
    for forventet in ("<dt>Navn</dt><dd>Kari Nordmann</dd>", "<dt>E-post</dt><dd>kari@example.no</dd>", "<dt>Telefon</dt><dd>900 11 222</dd>",
                      "<dt>Arbeidssted</dt><dd>Eksempelklinikken</dd>", "<dt>Yrkestittel</dt><dd>Psykolog</dd>", f"Nr. {pid} · registrert",
                      "<dt>Betaler</dt><dd>Deg selv</dd>", f"<dd>{PRIVAT_ADRESSE}<br>0150 Oslo</dd>"):
        assert forventet in kort, forventet
    assert "bekreftet e-posten" not in kort and "/bekreft-epost" not in kort        # innlogget med e-post: ingenting mer å bekrefte


def test_lenkenivaaet_skjuler_privatadressen_og_ber_om_a_bekrefte_e_posten(con, kid):
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    kort = _kort(_html(_lenkeklient(con, pid)))
    assert PRIVAT_ADRESSE not in kort and "0150" not in kort
    assert "Registrert. Vises når du har bekreftet e-posten din." in kort
    assert 'action="/bekreft-epost"' in kort and "Send meg en lenke på e-post" in kort and 'name="csrf_token"' in kort
    assert "<dt>Navn</dt><dd>Kari Nordmann</dd>" in kort                            # resten av hennes opplysninger vises


def test_firmaets_fakturaadresse_vises_direkte_men_aldri_privatadressen(con, kid):
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    _firma(con, pid)
    for klient in (_lenkeklient(con, pid), deltaker_klient(did)):
        kort = _kort(_html(klient))
        for forventet in ("<dt>Betaler</dt><dd>Eksempelfirma AS<br>Org.nr. 999888777</dd>", "<dd>Firmaveien 2<br>5003 Bergen</dd>",
                          "<dt>Faktura sendes til</dt><dd>regnskap@firma.example</dd>", "<dt>Referanse</dt><dd>REF-77</dd>",
                          "<dt>EHF-faktura</dt><dd>Ja</dd>"):
            assert forventet in kort, forventet
        assert PRIVAT_ADRESSE not in kort and "/bekreft-epost" not in kort          # firma: ingen privatadresse å skjule, ingenting å bekrefte


def test_kortet_viser_aldri_allergier_interne_kommentarer_hpr_rabatt_eller_kursnotat(con, kid):
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    db.oppdater_sensitivt(con, pid, "ALLERGI-HEMMELIG nøtter", "TILRETTELEGGING-HEMMELIG rullestol", "admin:test")
    con.execute("UPDATE paamelding SET intern_kommentar='INTERN-HEMMELIG', rabattkode='RABATT-HEMMELIG' WHERE id=?", (pid,))
    con.execute("UPDATE deltaker SET hpr_nr='HPR-HEMMELIG' WHERE id=?", (did,))
    con.execute("UPDATE kurs SET notat='KURSNOTAT-HEMMELIG' WHERE id=?", (kid,))
    con.commit()
    for klient in (deltaker_klient(did), _lenkeklient(con, pid)):
        assert "HEMMELIG" not in _html(klient)
    admin_forhandsvisning = admin_klient(con).get(f"/admin/kurs/{kid}/kursside/forhandsvis?versjon=publisert").get_data(as_text=True)
    assert "HEMMELIG" not in admin_forhandsvisning and "Kari Eksempel" in admin_forhandsvisning       # forhåndsvisningen har oppdiktet eksempel


def test_mine_kurs_skjuler_fakturaer_og_kursbevis_til_e_posten_er_bekreftet(con, kid):
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    con.execute("INSERT INTO faktura (paamelding_id, belop_nok, faktura_nr, status) VALUES (?,?,?,?)", (pid, 1500, "F-1001", "sendt"))
    con.commit()
    lenke = _lenkeklient(con, pid).get("/min-side")
    html = lenke.get_data(as_text=True)
    assert lenke.status_code == 200 and "Kurs K1" in html                           # selve kursene og «Åpne Min side» vises
    assert 'id="fakturaer"' not in html and "F-1001" not in html and "1500" not in html
    assert "vises først når du har bekreftet e-posten din" in html and 'action="/bekreft-epost"' in html
    full = deltaker_klient(did).get("/min-side").get_data(as_text=True)
    assert 'id="fakturaer"' in full and "F-1001" in full and "vises først når du har bekreftet" not in full


def test_dokumenter_og_kursbevis_krever_bekreftet_e_post(con, kid):
    did, pid = lag_deltaker(con, kid)
    cur = con.execute("INSERT INTO dokument (kurs_id, deltaker_id, type, tittel, url, publisert) VALUES (?,?,?,?,?,1)",
                      (kid, did, "kursbevis", "Kursbevis Kurs K1", "db:"))
    con.execute("INSERT INTO dokument_innhold (dokument_id, mimetype, innhold) VALUES (?,?,?)", (cur.lastrowid, "text/html", "<p>Bevis</p>"))
    con.commit()
    assert _lenkeklient(con, pid).get(f"/dokument/{cur.lastrowid}").status_code == 403
    assert deltaker_klient(did).get(f"/dokument/{cur.lastrowid}").status_code == 200
    assert _klient().get(f"/dokument/{cur.lastrowid}").status_code == 403


def test_innsjekk_og_felles_innhold_virker_paa_lenkenivaa(con, kid):
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    kode = con.execute("SELECT innsjekk_kode FROM kursdag WHERE kurs_id=? AND dato=?", (kid, IDAG.isoformat())).fetchone()[0]
    k = _lenkeklient(con, pid)
    assert "Les kapittel 3 før kurset" in _html(k)
    assert k.post("/kurs/K1/deltakerside/oppmote", data={"kode": kode}).status_code == 302
    assert [tuple(r) for r in con.execute("SELECT paamelding_id, kilde FROM oppmote")] == [(pid, "kode")]


def test_assistenten_regner_ikke_en_lenkeokt_som_identifisert(con, kid, monkeypatch):
    """«Spør oss» er av som standard. Slås den på, svarer den om faktura og betaling bare til en deltaker som har bekreftet e-posten."""
    from kurs import assistent
    monkeypatch.setattr(config, "ASSISTENT_AKTIV", True)
    sett = []
    ekte = assistent.svar

    def spion(con_, sporsmal, **kw):
        sett.append(kw.get("deltaker_id"))
        return ekte(con_, sporsmal, **kw)

    monkeypatch.setattr(assistent, "svar", spion)
    did, pid = lag_deltaker(con, kid)
    _lenkeklient(con, pid).post("/sporsmal", data={"sporsmal": "Hvor er fakturaen min?"})
    deltaker_klient(did).post("/sporsmal", data={"sporsmal": "Hvor er fakturaen min?"})
    assert sett == [None, did]


def test_admin_innlogging_gjor_ikke_en_lenkeokt_til_en_e_postokt(con, kid):
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    k = _lenkeklient(con, pid)
    assert k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD}).status_code == 302
    with k.session_transaction() as s:
        assert s["deltaker_id"] == did and s["deltaker_niva"] == minside.NIVAA_LENKE and s["admin_id"]
    assert PRIVAT_ADRESSE not in _kort(_html(k))


# ============================ økten følger lenken ============================

_OKTNOKLER = {"deltaker_id", "deltaker_niva", "min_side_pid", "min_side_ver"}


def test_lenkeokta_avsluttes_ved_neste_forespoersel_naar_lenken_stenges_fornyes_eller_utloeper(con, kid, monkeypatch):
    """Cookien ligger hos nettleseren og kan ikke trekkes tilbake, så en økt som kom inn med lenken sjekkes mot lenken ved HVER forespørsel.
    Det samme gjelder for den som fikk lenken videresendt: «Steng lenken» og «Lag ny lenke» virker straks, ikke bare for nye åpninger."""
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    admin = admin_klient(con)
    url = _adm_url(kid, pid, "/min-side-lenke")
    side = "/kurs/K1/deltakerside"

    def avsluttet(klient) -> bool:
        r = klient.get(side)
        with klient.session_transaction() as s:
            tom = not (_OKTNOKLER & set(s))
        return r.status_code == 302 and "/logg-inn" in r.headers["Location"] and tom

    k = _lenkeklient(con, pid)
    assert k.get(side).status_code == 200
    admin.post(url, data={"handling": "steng"})                                    # 1. stengt
    assert avsluttet(k) and k.get("/min-side").status_code == 302
    admin.post(url, data={"handling": "ny"})                                       # åpnes igjen med en ny lenke (versjon 1)
    k = _lenkeklient(con, pid)
    assert k.get(side).status_code == 200
    admin.post(url, data={"handling": "ny"})                                       # 2. fornyet (versjon 2): økta fra versjon 1 avsluttes
    assert avsluttet(k)
    k = _lenkeklient(con, pid)
    assert k.get(side).status_code == 200
    fast_dato(monkeypatch, date(2027, 4, 11))                                      # 3. dagen etter siste gyldige dag
    assert avsluttet(k)


def test_epostokta_paavirkes_ikke_av_at_lenken_stenges(con, kid, sendt):
    """Deltakeren som har bekreftet e-posten har vist at hen når den, og trenger ikke lenken lenger."""
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    k = _lenkeklient(con, pid)
    k.post("/bekreft-epost", data={"neste": "/kurs/K1/deltakerside"})
    k.get(_logg_inn_lenke_fra(sendt[0][2]))
    assert _niva(k) == minside.NIVAA_EPOST
    minside.steng(con, pid, "admin:test")
    con.commit()
    r = k.get("/kurs/K1/deltakerside")
    assert r.status_code == 200 and PRIVAT_ADRESSE in _kort(r.get_data(as_text=True))


def test_lenkeokt_uten_riktig_id_og_versjon_avvises(con, kid):
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    ola, ola_pid = lag_deltaker(con, kid, "ola@example.no", "Ola", "Hansen")
    ugyldige = [{}, {"min_side_pid": ola_pid, "min_side_ver": 0}, {"min_side_pid": str(pid), "min_side_ver": 0}, {"min_side_pid": pid, "min_side_ver": 7},
                {"min_side_pid": 99999, "min_side_ver": 0}]
    for ekstra in ugyldige:
        k = _klient()
        with k.session_transaction() as s:
            s["deltaker_id"], s["deltaker_niva"] = did, minside.NIVAA_LENKE
            s.update(ekstra)
        assert k.get("/kurs/K1/deltakerside").status_code == 302, ekstra
    k = _klient()                                                                  # kontroll: riktig id og versjon slipper inn
    with k.session_transaction() as s:
        s["deltaker_id"], s["deltaker_niva"], s["min_side_pid"], s["min_side_ver"] = did, minside.NIVAA_LENKE, pid, 0
    assert k.get("/kurs/K1/deltakerside").status_code == 200


def test_rettet_epostadresse_gir_ny_lenke_men_aapner_ikke_en_stengt_lenke(con, kid):
    """Er registreringen skrevet med en feil adresse, gikk bekreftelsen med lenken til en fremmed. Rettes adressen, slutter den lenken å virke."""
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    ola, ola_pid = lag_deltaker(con, kid, "ola@example.no", "Ola", "Hansen")
    gammel, olas = _sti(con, pid), _sti(con, ola_pid)
    fremmed = _lenkeklient(con, pid)                                               # den som fikk e-posten ved en feil
    assert db.oppdater_deltaker(con, did, {"epost": "kari.rettet@example.no"}, "admin:test") == ["epost"]
    con.commit()
    assert _klient().get(gammel).status_code == 404 and _sti(con, pid) != gammel and _klient().get(_sti(con, pid)).status_code == 302
    assert fremmed.get("/kurs/K1/deltakerside").status_code == 302                  # også økta hen allerede hadde, er avsluttet
    assert _klient().get(olas).status_code == 302                                   # andres lenker er urørt
    db.oppdater_deltaker(con, did, {"telefon": "900 00 000"}, "admin:test")         # andre endringer enn e-posten rører ikke lenken
    ny = _sti(con, pid)
    db.oppdater_deltaker(con, did, {"epost": "kari.tre@example.no"}, "admin:test")
    assert _sti(con, pid) != ny
    minside.steng(con, pid, "admin:test")
    db.oppdater_deltaker(con, did, {"epost": "kari.fire@example.no"}, "admin:test")
    con.commit()
    assert minside.er_stengt(con, pid) and minside.url(con, pid) is None            # en stengt lenke forblir stengt
    grunner = [json.loads(r[0]).get("grunn") for r in con.execute("SELECT detaljer FROM hendelse WHERE handling='min_side_lenke_fornyet' ORDER BY id")]
    assert grunner == ["epost_endret"] * 3


def test_logger_viser_lenkehendelsene_i_klartekst(con, kid):
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    admin = admin_klient(con)
    admin.post(_adm_url(kid, pid, "/min-side-lenke"), data={"handling": "steng"})
    admin.post(_adm_url(kid, pid, "/min-side-lenke"), data={"handling": "ny"})
    db.oppdater_deltaker(con, did, {"epost": "kari.rettet@example.no"}, "admin:test")
    con.commit()
    html = admin.get(f"/admin/kurs/{kid}/deltaker/{pid}/logger").get_data(as_text=True)
    assert "Lenken til Min side ble stengt" in html and html.count("Ny lenke til Min side ble laget") == 2
    assert "E-postadressen ble rettet." in html and "Annen hendelse" not in html


def test_utlopt_lenke_settes_ikke_inn_i_e_post_og_admin_ser_at_den_er_utloept(con, kid, monkeypatch, sendt):
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    assert Kjoring(con, idag=date(2027, 4, 10)).min_side_lenke(pid) == minside.url(con, pid)       # siste gyldige dag
    assert Kjoring(con, idag=date(2027, 4, 11)).min_side_lenke(pid) is None
    fast_dato(monkeypatch, date(2027, 4, 20))                                                       # siden står åpen i 180 dager, lenken ikke
    uid, _ = db.opprett_admin_utsending(con, kid, "Hei", "Se {min_side}", [pid], sendt_av_admin_id=None, signatur_html="")
    con.commit()
    Kjoring(con, idag=date(2027, 4, 20), aktor="admin:test").send_admin_utsending(uid)
    (til, emne, html), = sendt
    assert "/min/" not in html and f'<a href="{BASE}/min-side">Mine kurs</a>' in html
    boks = admin_klient(con).get(_adm_url(kid, pid)).get_data(as_text=True)
    assert "Lenken til Min side utløp 10.04.2027 (30 dager etter siste kursdag)" in boks and 'id="min-side-lenke"' not in boks


def test_reservefristen_forklares_riktig_for_kurs_uten_kursdager(con):
    kid = lag_kurs(con, "K2", dager=0)
    did, pid = lag_deltaker(con, kid)
    assert minside.utloper_forklaring(con, pid) == "90 dager etter påmeldingen"
    kid2 = lag_kurs(con, "K3")
    _, pid2 = lag_deltaker(con, kid2, "ola@example.no", "Ola", "Hansen")
    assert minside.utloper_forklaring(con, pid2) == "30 dager etter siste kursdag"
    assert "(90 dager etter påmeldingen)" in admin_klient(con).get(_adm_url(kid, pid)).get_data(as_text=True)


def test_en_lenke_har_bare_en_skrivemaate():
    token = lenker.min_side_token(12, 3)
    assert lenker.les_min_side_token(token) == (12, 3)
    assert lenker.les_min_side_token(token.replace("12.", "012.", 1)) is None                     # ledende null
    assert lenker.les_min_side_token(token.replace("12.", "١٢.", 1)) is None                       # andre sifre enn 0-9
    assert lenker.les_min_side_token(token.replace(".3.", ".03.", 1)) is None
    assert lenker.les_min_side_token(lenker.min_side_token(0, 0)) == (0, 0)


def test_deltakerruter_uten_oppgitt_nivaa_viser_aldri_privatadressen():
    """Installeringen av deltakerrutene er lukket som standard: glemmer noen å oppgi nivået, vises privatadressen ikke."""
    import inspect
    from kurs.web import deltakerside_ruter
    assert inspect.signature(deltakerside_ruter.installer).parameters["deltaker_full"].default() is False


# ============================ bekreft e-posten ============================

def _logg_inn_lenke_fra(html: str) -> str:
    treff = re.search(r'href="([^"]*/logg-inn/[^"]+)"', html)
    assert treff, "ingen innloggingslenke i e-posten"
    return treff.group(1).replace("&amp;", "&").removeprefix(BASE)


def test_bekreft_epost_sender_lenken_bare_til_deltakerens_egen_adresse_og_gir_full_tilgang(con, kid, sendt):
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    k = _lenkeklient(con, pid)
    r = k.post("/bekreft-epost", data={"neste": "/kurs/K1/deltakerside", "epost": "annen@example.no"})   # et adressefelt finnes ikke: ignoreres
    assert r.status_code == 302 and r.headers["Location"].endswith("/kurs/K1/deltakerside")
    (til, emne, html), = sendt
    assert til == "kari@example.no"
    side = _html(k)                                                                 # beskjeden står på siden hen sendes til, og nevner bare en delvis skjult adresse
    assert "Vi har sendt en lenke til k***@example.no" in side
    assert PRIVAT_ADRESSE not in _kort(side) and _niva(k) == minside.NIVAA_LENKE            # ikke bekreftet før lenken er klikket
    r = k.get(_logg_inn_lenke_fra(html))
    assert r.status_code == 302 and r.headers["Location"].endswith("/kurs/K1/deltakerside")
    assert _niva(k) == minside.NIVAA_EPOST and PRIVAT_ADRESSE in _kort(_html(k))
    assert k.get(_logg_inn_lenke_fra(html)).status_code == 302 and _niva(k) == minside.NIVAA_EPOST   # engangslenke: andre klikk gir bare ny innlogging


def test_bekreft_epost_krever_innlogging_og_sender_ikke_til_anonymisert(con, kid, sendt):
    did, pid = lag_deltaker(con, kid)
    r = _klient().post("/bekreft-epost")
    assert r.status_code == 302 and "/logg-inn" in r.headers["Location"] and sendt == []
    con.execute("UPDATE deltaker SET epost=? WHERE id=?", (f"anonymisert-{did}@{db.ANONYM_DOMENE}", did))
    con.commit()
    assert deltaker_klient(did).post("/bekreft-epost").status_code == 404 and sendt == []


def test_bekreft_epost_sender_aldri_videre_til_en_fremmed_adresse(con, kid, sendt):
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    r = _lenkeklient(con, pid).post("/bekreft-epost", data={"neste": "https://ond.example/fangst"})
    assert r.status_code == 302 and "ond.example" not in r.headers["Location"] and r.headers["Location"].endswith("/min-side")


def test_bekreft_epost_er_takbegrenset_per_adresse(con, kid, sendt):
    did, pid = lag_deltaker(con, kid)
    k = deltaker_klient(did)
    kvote = sikkerhet.GRENSER["innloggingslenke_epost"][0]
    for _ in range(kvote):
        assert k.post("/bekreft-epost").status_code == 302
    assert k.post("/bekreft-epost").status_code == 429 and len(sendt) == kvote


# ============================ e-postene ============================

def _bekreftelse(con, pid):
    from kurs import sveiper
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)
    con.commit()


def _kopi(con, pid, mal):
    return con.execute("SELECT html FROM sendt_epost WHERE paamelding_id=? AND mal=?", (pid, mal)).fetchone()["html"]


def test_bekreftelsen_har_knapp_med_den_personlige_lenken_naar_min_side_er_aapen(con, kid, sendt):
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    _bekreftelse(con, pid)
    (til, emne, html), = [m for m in sendt if m[1].startswith("Bekreftelse")]
    lenke = minside.url(con, pid)
    assert til == "kari@example.no" and f'<a href="{lenke}"' in html and ">Åpne Min side</a>" in html and "Lenken er personlig" in html
    assert _klient().get(lenke.removeprefix(BASE)).status_code == 302               # lenken i e-posten virker


def test_e_posthistorikken_lagrer_aldri_den_personlige_lenken(con, kid, sendt):
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    _bekreftelse(con, pid)
    token = minside.url(con, pid).removeprefix(f"{BASE}/min/")
    kopi = _kopi(con, pid, "bekreftelse")
    assert token not in kopi and f"{BASE}/min/skjult" in kopi and ">Åpne Min side</a>" in kopi      # resten av e-posten er lik
    ant = con.execute("SELECT COUNT(*) FROM sendt_epost WHERE html LIKE ?", (f"%{token}%",)).fetchone()[0]
    assert ant == 0


@pytest.mark.parametrize("tilstand", ["ikke_publisert", "nedtatt", "stengt_lenke"])
def test_ingen_knapp_naar_min_side_ikke_er_aapen_eller_lenken_er_stengt(con, kid, sendt, tilstand):
    if tilstand != "ikke_publisert":
        _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    if tilstand == "nedtatt":
        con.execute("UPDATE kursside SET aktiv=0 WHERE kurs_id=?", (kid,))
    if tilstand == "stengt_lenke":
        minside.steng(con, pid, "admin:test")
    con.commit()
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    daglig._innkallinger(Kjoring(con, idag=IDAG - timedelta(days=3)), kurs, db.kursdager(con, kid), IDAG)
    (til, emne, html), = sendt
    assert "/min/" not in html and "Åpne Min side" not in html
    assert "Mine kurs" in html                                                      # fotlinjen peker fortsatt til oversikten


def test_bare_den_som_har_plass_far_en_personlig_lenke_i_e_post(con, kid):
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    _, venter = lag_deltaker(con, kid, "vera@example.no", "Vera", "Venteliste", status="venteliste")
    k = Kjoring(con, idag=IDAG)
    assert k.min_side_lenke(pid) == minside.url(con, pid) and k.min_side_lenke(venter) is None and k.min_side_lenke(99999) is None


@pytest.mark.parametrize("dagerfor,mal,emneprefiks", [(3, "ukefor", "Velkommen til"), (1, "dagfor", "I morgen starter")])
def test_paminnelsene_har_knappen_og_kopien_skjuler_lenken(con, kid, sendt, dagerfor, mal, emneprefiks):
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    daglig._innkallinger(Kjoring(con, idag=IDAG - timedelta(days=dagerfor)), kurs, db.kursdager(con, kid), IDAG)
    con.commit()
    (til, emne, html), = [m for m in sendt if m[1].startswith(emneprefiks)]
    lenke = minside.url(con, pid)
    assert f'<a href="{lenke}"' in html and ">Åpne Min side</a>" in html
    assert lenke.removeprefix(f"{BASE}/min/") not in _kopi(con, pid, mal)


def test_flettefeltet_min_side_i_egenskrevet_e_post(con, kid):
    did, pid = lag_deltaker(con, kid)
    mottaker = {"fornavn": "Kari", "navn": "Kari Nordmann", "min_side_url": minside.url(con, pid)}
    html = str(maltekster.manuell_tekst_til_html("Hei {fornavn}, se {min_side}.", mottaker))
    assert f'Hei Kari, se <a href="{mottaker["min_side_url"]}">Min side</a>.' in html
    uten = str(maltekster.manuell_tekst_til_html("Se {min_side}.", {"fornavn": "Kari", "navn": "Kari Nordmann"}))
    assert f'Se <a href="{BASE}/min-side">Mine kurs</a>.' in uten                   # ingen personlig lenke: oversikten
    ond = str(maltekster.manuell_tekst_til_html("Se {min_side}.", {"fornavn": "K", "navn": "K N", "min_side_url": "http://ond.example/min/x"}))
    assert "ond.example" not in ond                                                 # bare systemets egen adresse godtas
    assert "min_side" in maltekster.MANUELLE_KODER


def test_egenskrevet_utsending_gir_hver_mottaker_sin_egen_lenke(con, kid, sendt):
    _side(con, kid)
    kari, kari_pid = lag_deltaker(con, kid)
    ola, ola_pid = lag_deltaker(con, kid, "ola@example.no", "Ola", "Hansen")
    vera, vera_pid = lag_deltaker(con, kid, "vera@example.no", "Vera", "Venteliste", status="venteliste")
    uid, _ = db.opprett_admin_utsending(con, kid, "Litteratur", "Hei {fornavn}, her er siden din: {min_side}", [kari_pid, ola_pid, vera_pid],
                                       sendt_av_admin_id=None, signatur_html="")
    con.commit()
    Kjoring(con, idag=IDAG, aktor="admin:test").send_admin_utsending(uid)
    per_mottaker = {til: html for til, emne, html in sendt}
    assert set(per_mottaker) == {"kari@example.no", "ola@example.no", "vera@example.no"}
    assert f'<a href="{minside.url(con, kari_pid)}">Min side</a>' in per_mottaker["kari@example.no"]
    assert f'<a href="{minside.url(con, ola_pid)}">Min side</a>' in per_mottaker["ola@example.no"]
    assert minside.url(con, ola_pid) not in per_mottaker["kari@example.no"]        # aldri en annen sin lenke
    assert "/min/" not in per_mottaker["vera@example.no"] and f"{BASE}/min-side" in per_mottaker["vera@example.no"]   # venteliste: ingen Min side
    lagret = " ".join(r["html"] for r in con.execute("SELECT html FROM sendt_epost WHERE mal='admin_melding'"))
    assert minside.url(con, kari_pid).removeprefix(f"{BASE}/min/") not in lagret


# ============================ administrasjonen ============================

def _adm_url(kid, pid, ende="") -> str:
    return f"/admin/kurs/{kid}/deltaker/{pid}{ende}"


def test_deltakervinduet_viser_lenken_med_kopier_knapp(con, kid):
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    html = admin_klient(con).get(_adm_url(kid, pid)).get_data(as_text=True)
    assert '<h2 id="minside-tittel">Min side</h2>' in html and f'value="{minside.url(con, pid)}"' in html
    assert 'data-kopier="min-side-lenke"' in html and "Lag ny lenke" in html and "Steng lenken" in html
    assert "Lenken virker til 10.04.2027" in html and "står som en knapp i bekreftelsen" in html


def test_deltakervinduet_sier_fra_naar_kurset_ikke_har_en_aapen_min_side(con, kid):
    did, pid = lag_deltaker(con, kid)
    html = admin_klient(con).get(_adm_url(kid, pid)).get_data(as_text=True)
    assert "Kurset har ingen åpen Min side ennå" in html


def test_lesetilgang_ser_ikke_den_personlige_lenken(con, kid):
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    html = admin_klient(con, "lese").get(_adm_url(kid, pid)).get_data(as_text=True)
    assert "minside-tittel" not in html and "/min/" not in html


def test_administrator_kan_stenge_og_lage_ny_lenke(con, kid):
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    gammel = _sti(con, pid)
    admin = admin_klient(con)
    r = admin.post(_adm_url(kid, pid, "/min-side-lenke"), data={"handling": "steng"})
    assert r.status_code == 302 and minside.url(con, pid) is None and minside.er_stengt(con, pid)
    assert _klient().get(gammel).status_code == 404
    html = admin.get(_adm_url(kid, pid)).get_data(as_text=True)
    assert "Lenken til Min side er stengt" in html and 'id="min-side-lenke"' not in html and "Lag ny lenke og åpne igjen" in html
    r = admin.post(_adm_url(kid, pid, "/min-side-lenke"), data={"handling": "ny"})
    ny = _sti(con, pid)
    assert r.status_code == 302 and ny != gammel and not minside.er_stengt(con, pid)
    assert _klient().get(gammel).status_code == 404 and _klient().get(ny).status_code == 302
    rad = con.execute("SELECT versjon, stengt, endret_av FROM min_side_lenke WHERE paamelding_id=?", (pid,)).fetchone()
    assert (rad["versjon"], rad["stengt"], rad["endret_av"].startswith("admin:")) == (1, 0, True)
    handlinger = [r[0] for r in con.execute("SELECT handling FROM hendelse WHERE handling LIKE 'min_side_lenke%' ORDER BY id")]
    assert handlinger == ["min_side_lenke_stengt", "min_side_lenke_fornyet"]


def test_lenke_endepunktet_krever_skriveadgang_riktig_kurs_og_gyldig_handling(con, kid):
    _side(con, kid)
    did, pid = lag_deltaker(con, kid)
    andre_kurs = lag_kurs(con, "K2")
    url = _adm_url(kid, pid, "/min-side-lenke")
    r = _klient().post(url, data={"handling": "steng"})
    assert r.status_code in (302, 401, 403) and not minside.er_stengt(con, pid)         # uten innlogging
    assert admin_klient(con, "lese").post(url, data={"handling": "steng"}).status_code in (302, 403) and not minside.er_stengt(con, pid)
    admin = admin_klient(con)
    assert admin.post(_adm_url(andre_kurs, pid, "/min-side-lenke"), data={"handling": "steng"}).status_code == 404     # påmeldingen hører til K1
    assert admin.post(url, data={"handling": "slett"}).status_code == 400 and admin.post(url, data={}).status_code == 400
    assert not minside.er_stengt(con, pid)
    admin.injiser_csrf = False
    assert admin.post(url, data={"handling": "steng"}).status_code in (400, 403) and not minside.er_stengt(con, pid)
    assert admin.get(url).status_code == 405                                            # bare POST


def test_forhandsvisningen_viser_kortet_med_et_oppdiktet_eksempel_og_ingen_skjema(con, kid):
    _side(con, kid)
    html = admin_klient(con).get(f"/admin/kurs/{kid}/kursside/forhandsvis?versjon=publisert").get_data(as_text=True)
    kort = _kort(html)
    assert "Eksempel: slik ser kortet ut for deltakeren." in kort and "Kari Eksempel" in kort and "Eksempelveien 1" in kort
    assert "/bekreft-epost" not in html


# ============================ databasen ============================

def test_migrering_19_lager_tabellen_og_sletting_av_paamelding_fjerner_raden(con, kid):
    from kurs import migreringer
    assert any(nr == 19 and navn == "min_side_lenke" for nr, navn, *_ in migreringer.MIGRERINGER)
    did, pid = lag_deltaker(con, kid)
    minside.ny_lenke(con, pid, "admin:test")
    con.commit()
    kolonner = {r["name"] for r in con.execute("PRAGMA table_info(min_side_lenke)")}
    assert kolonner == {"paamelding_id", "versjon", "stengt", "endret", "endret_av"}
    con.execute("DELETE FROM paamelding WHERE id=?", (pid,))
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM min_side_lenke").fetchone()[0] == 0


def test_lenketabellen_har_aldri_personopplysninger():
    """Bare påmelding, versjon, om lenken er stengt, når og av hvem (en administrator): ingen e-post, navn eller adresse."""
    for fil in ("schema.sql", "schema_postgres.sql"):
        kilde = (config.ROT / "kurs" / fil).read_text(encoding="utf-8")
        tabell = re.search(r"CREATE TABLE IF NOT EXISTS min_side_lenke \((.*?)\);", kilde, re.S).group(1)
        assert not re.search(r"epost|navn\b|adresse|telefon|token", re.sub(r"--[^\n]*", "", tabell)), fil
