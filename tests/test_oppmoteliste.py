"""Oppmøtelisten for kursleder (reserve når QR-koden ikke virker): hvem som vises, søk, «til stede»/«fjern» (idempotent, kilde
manuell), «merk alle» og «fjern alle», Zoom-bekreftelse, rutene (roller, CSRF, ugyldige id-er, IDOR mellom kurs, fremtidig dag),
kursbevis-effekt, personvern i logg og adresser, tilgjengelighet og lenkene til siden. Alle data er oppdiktede.
"""
import json
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from kurs import config, db, hendelseslogg, kursbevis, oppmoteliste
from kurs.kjoring import Kjoring

MAPPE = Path(__file__).resolve().parent.parent / "kurs" / "web"
JSON = {"Accept": "application/json"}


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


def _logg_inn(klient):
    r = klient.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    assert r.status_code == 302
    return klient


def _bruker(con, rolle):
    db.opprett_admin_bruker(con, rolle, rolle.title(), "passord-som-holder", rolle=rolle)
    con.commit()
    k = _klient()
    r = k.post("/admin/logg-inn", data={"brukernavn": rolle, "passord": "passord-som-holder"})
    assert r.status_code == 302, rolle
    return k


def _kurs(con, kode, dager, **kw):
    return db.opprett_kurs(con, kode=kode, navn=kw.pop("navn", f"Kurs {kode}"), datoer=[d.isoformat() for d in dager],
                           sharepoint_mappe=f"Kurs/{kode}", **{"pris_nok": 1000, **kw})


def _meld(con, kid, fornavn, etternavn, epost=None, **kw):
    epost = epost or f"{fornavn}.{etternavn}@eksempel.no".lower().replace("ø", "o").replace("å", "a").replace("æ", "ae")
    pid, _ = db.meld_paa(con, kid, epost=epost, fornavn=fornavn, etternavn=etternavn, **kw)
    return pid


IDAG = date.today()
NAVN = ("Kari Hansen", "Ola Nordmann", "Sølvi Tørresen", "Åse Ødegård")


def _bygg(con):
    """Kurs A med tre kursdager (i går, i dag, i morgen) og fire deltakere; kurs B (et annet kurs) med én deltaker og en dag i dag."""
    kid = _kurs(con, "A1", [IDAG - timedelta(days=1), IDAG, IDAG + timedelta(days=1)], navn="Kurs A")
    pids = [_meld(con, kid, *n.split()) for n in NAVN]
    dager = [d["id"] for d in db.kursdager(con, kid)]
    kb = _kurs(con, "B1", [IDAG], navn="Kurs B")
    pb = _meld(con, kb, "Bertil", "Berg")
    dag_b = db.kursdager(con, kb)[0]["id"]
    con.commit()
    return {"kid": kid, "pids": pids, "i_gaar": dager[0], "i_dag": dager[1], "i_morgen": dager[2], "kb": kb, "pb": pb, "dag_b": dag_b}


def _side(kid, dag):
    return f"/admin/kurs/{kid}/oppmote/{dag}"


def _rad(klient, kid, dag, pid, onsket, **felt):
    return klient.post(_side(kid, dag) + "/rad", data={"paamelding_id": pid, "onsket": onsket, **felt}, headers=JSON)


def _alle(klient, kid, dag, handling, bekreft=True):
    data = {"handling": handling, **({"bekreft": "1"} if bekreft else {})}
    return klient.post(_side(kid, dag) + "/alle", data=data, headers=JSON)


def _oppmote(con, dag):
    return {r["paamelding_id"]: r["kilde"] for r in con.execute("SELECT * FROM oppmote WHERE kursdag_id=?", (dag,))}


def _uten_sokenokler(html: str) -> str:
    """Siden uten de skjulte søkenøklene (navn og e-post med små bokstaver): det som faktisk vises og lenkes til."""
    return re.sub(r'data-sok(?:-ascii)?="[^"]*"', "", html)


# ================================================================ hvem som vises

def test_bare_de_med_plass_vises(con):
    kid = _kurs(con, "S1", [IDAG])
    dag = db.kursdager(con, kid)[0]["id"]
    ider = {s: _meld(con, kid, s.title(), "Test") for s in ("paameldt", "ekstradeltaker", "avmeldt", "avslatt", "utgatt", "forlatt", "venteliste")}
    # Ekstradeltaker først og venteliste sist: Påmeldt -> Ekstradeltaker frigjør en plass, og da rykker første på ventelisten opp.
    db.sett_paamelding_status(con, ider["ekstradeltaker"], "ekstradeltaker")
    for s in ("avmeldt", "avslatt", "utgatt", "forlatt", "venteliste"):
        db.sett_paamelding_status(con, ider[s], s)
    vist = {r["paamelding_id"] for r in oppmoteliste.deltakere_for_dag(con, kid, dag)}
    assert vist == {ider["paameldt"], ider["ekstradeltaker"]}


def test_sortert_alfabetisk_i_norsk_rekkefolge(con):
    kid = _kurs(con, "S2", [IDAG])
    dag = db.kursdager(con, kid)[0]["id"]
    for n in ("Åse Ødegård", "Øystein Berg", "Zara Zed", "Ola Nordmann", "Kari Hansen", "Sølvi Tørresen"):
        _meld(con, kid, *n.split())
    navn = [r["navn"] for r in oppmoteliste.deltakere_for_dag(con, kid, dag)]
    assert navn == ["Kari Hansen", "Ola Nordmann", "Sølvi Tørresen", "Zara Zed", "Øystein Berg", "Åse Ødegård"]


def test_listen_har_ingen_personopplysninger_ut_over_det_som_trengs(con):
    d = _bygg(con)
    for r in oppmoteliste.deltakere_for_dag(con, d["kid"], d["i_dag"]):
        assert set(r) == {"paamelding_id", "status", "avslatt_ts", "utgatt_ts", "forlatt_ts", "ekstradeltaker_ts", "navn", "epost",
                          "kilde", "ts", "minutter"}


# ================================================================ søk (samme regler som deltakersøket)

@pytest.mark.parametrize("sok, navn, epost, forventet", [
    ("solvi", "Sølvi Tørresen", "solvi.torresen@eksempel.no", True),
    ("sølvi", "Sølvi Tørresen", "solvi.torresen@eksempel.no", True),
    ("SØLVI", "Sølvi Tørresen", "solvi.torresen@eksempel.no", True),
    ("hakon", "Håkon Åsen", "hakon@eksempel.no", True),
    ("håkon", "Hakon Asen", "hakon@eksempel.no", True),          # skrevet med æøå, registrert uten
    ("torresen", "Sølvi Tørresen", "s@eksempel.no", True),
    ("tørresen", "Solvi Torresen", "s@eksempel.no", True),
    ("kari han", "Kari Hansen", "k@eksempel.no", True),          # alle ord må treffe, delvis
    ("hansen kari", "Kari Hansen", "k@eksempel.no", True),       # i vilkårlig rekkefølge
    ("kari ola", "Kari Hansen", "k@eksempel.no", False),
    ("kari.hansen@", "Kari Hansen", "kari.hansen@eksempel.no", True),   # del av e-posten
    ("eksempel", "Kari Hansen", "kari.hansen@eksempel.no", True),
    ("xyz", "Kari Hansen", "kari.hansen@eksempel.no", False),
    ("", "Kari Hansen", "k@eksempel.no", True),                  # tomt søk viser alle
    ("   ", "Kari Hansen", "k@eksempel.no", True),
    ("æ", "Ærlig Åsen", "a@eksempel.no", True),
])
def test_soketreff(sok, navn, epost, forventet):
    assert oppmoteliste.treffer(oppmoteliste.sokenokkel(navn, epost), sok) is forventet


def test_sok_i_javascript_bruker_samme_bokstavtabell_som_python():
    js = (MAPPE / "static" / "oppmoteliste.js").read_text(encoding="utf-8")
    tabell = dict(re.findall(r'"(.)": "([a-z]+)"', re.search(r"var ASCII = \{(.*?)\};", js, re.S).group(1)))
    from kurs import deltakersok
    assert tabell == deltakersok._ASCII_BOKSTAVER


# ================================================================ kursdager

def test_standarddag_er_dagens_ellers_nærmeste():
    dager = [{"id": 1, "dato": "2027-01-10"}, {"id": 2, "dato": "2027-01-11"}, {"id": 3, "dato": "2027-03-01"}]
    assert oppmoteliste.standarddag(dager, date(2027, 1, 11))["id"] == 2                # i dag
    assert oppmoteliste.standarddag(dager, date(2027, 1, 1))["id"] == 1                 # før kurset: den første
    assert oppmoteliste.standarddag(dager, date(2027, 6, 1))["id"] == 3                 # etter kurset: den siste
    assert oppmoteliste.standarddag(dager, date(2027, 2, 20))["id"] == 3                # nærmest
    assert oppmoteliste.standarddag(dager, date(2027, 1, 12))["id"] == 2                # nærmest, gårsdagen
    assert oppmoteliste.standarddag([{"id": 1, "dato": "2027-01-10"}, {"id": 2, "dato": "2027-01-12"}], date(2027, 1, 11))["id"] == 1  # likt: den tidligste
    assert oppmoteliste.standarddag([], date(2027, 1, 1)) is None


def test_bare_dager_til_og_med_i_dag_kan_tas_opp():
    assert oppmoteliste.kan_ta_opp({"dato": "2027-01-10"}, date(2027, 1, 10))
    assert oppmoteliste.kan_ta_opp({"dato": "2027-01-09"}, date(2027, 1, 10))
    assert not oppmoteliste.kan_ta_opp({"dato": "2027-01-11"}, date(2027, 1, 10))


def test_dag_fra_annet_kurs_hentes_ikke(con):
    d = _bygg(con)
    assert oppmoteliste.hent_dag(con, d["kid"], d["i_dag"]) is not None
    assert oppmoteliste.hent_dag(con, d["kid"], d["dag_b"]) is None
    assert oppmoteliste.hent_dag(con, d["kid"], 99999) is None


# ================================================================ logikken: trykk, merk alle, fjern alle

def test_til_stede_er_idempotent_og_kilden_er_manuell(con):
    d = _bygg(con)
    pid = d["pids"][0]
    a = oppmoteliste.sett_oppmote(con, d["kid"], d["i_dag"], pid, True, "admin:test")
    b = oppmoteliste.sett_oppmote(con, d["kid"], d["i_dag"], pid, True, "admin:test")
    assert (a.status, b.status) == ("endret", "uendret") and a.kilde == b.kilde == "manuell"
    assert _oppmote(con, d["i_dag"]) == {pid: "manuell"}
    # bare den første registreringen loggføres
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='oppmote_manuell'").fetchone()[0] == 1


def test_fjern_er_idempotent(con):
    d = _bygg(con)
    pid = d["pids"][0]
    oppmoteliste.sett_oppmote(con, d["kid"], d["i_dag"], pid, True, "admin:test")
    a = oppmoteliste.sett_oppmote(con, d["kid"], d["i_dag"], pid, False, "admin:test")
    b = oppmoteliste.sett_oppmote(con, d["kid"], d["i_dag"], pid, False, "admin:test")
    assert (a.status, b.status) == ("endret", "uendret")
    assert _oppmote(con, d["i_dag"]) == {}
    loggede = [json.loads(r["detaljer"])["registrert"] for r in con.execute("SELECT detaljer FROM hendelse WHERE handling='oppmote_manuell' ORDER BY id")]
    assert loggede == [True, False]


def test_til_stede_overskriver_aldri_qr_kode_eller_zoom(con):
    d = _bygg(con)
    for pid, kilde in zip(d["pids"][:3], ("qr", "kode", "zoom")):
        db.registrer_oppmote(con, pid, d["i_dag"], kilde, 90 if kilde == "zoom" else None)
        u = oppmoteliste.sett_oppmote(con, d["kid"], d["i_dag"], pid, True, "admin:test")
        assert (u.status, u.til_stede, u.kilde) == ("uendret", True, kilde)
    assert _oppmote(con, d["i_dag"]) == dict(zip(d["pids"][:3], ("qr", "kode", "zoom")))
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='oppmote_manuell'").fetchone()[0] == 0


def test_oppmote_fra_qr_kan_fjernes_uten_ekstra_bekreftelse(con):
    d = _bygg(con)
    db.registrer_oppmote(con, d["pids"][0], d["i_dag"], "qr")
    assert oppmoteliste.sett_oppmote(con, d["kid"], d["i_dag"], d["pids"][0], False, "a").status == "endret"
    assert _oppmote(con, d["i_dag"]) == {}


def test_zoom_kan_bare_fjernes_med_bekreftelse(con):
    d = _bygg(con)
    pid = d["pids"][0]
    db.registrer_oppmote(con, pid, d["i_dag"], "zoom", 95)
    u = oppmoteliste.sett_oppmote(con, d["kid"], d["i_dag"], pid, False, "admin:test")
    assert u.status == "trenger_zoom_bekreftelse" and _oppmote(con, d["i_dag"]) == {pid: "zoom"}
    assert oppmoteliste.sett_oppmote(con, d["kid"], d["i_dag"], pid, False, "admin:test", bekreft_zoom=True).status == "endret"
    assert _oppmote(con, d["i_dag"]) == {}


def test_en_som_ikke_har_plass_kan_ikke_merkes(con):
    d = _bygg(con)
    pid = _meld(con, d["kid"], "Vera", "Venter")
    db.sett_paamelding_status(con, pid, "venteliste")
    assert oppmoteliste.sett_oppmote(con, d["kid"], d["i_dag"], pid, True, "a").status == "ikke_i_listen"
    assert oppmoteliste.sett_oppmote(con, d["kid"], d["i_dag"], d["pb"], True, "a").status == "ikke_i_listen"    # fra et annet kurs
    assert _oppmote(con, d["i_dag"]) == {}


def test_merk_alle_merker_bare_de_uregistrerte_og_er_idempotent(con):
    d = _bygg(con)
    db.registrer_oppmote(con, d["pids"][0], d["i_dag"], "qr")
    db.registrer_oppmote(con, d["pids"][1], d["i_dag"], "zoom", 60)
    venter = _meld(con, d["kid"], "Vera", "Venter")
    db.sett_paamelding_status(con, venter, "venteliste")
    assert oppmoteliste.merk_alle(con, d["kid"], d["i_dag"], "admin:test") == 2
    assert oppmoteliste.merk_alle(con, d["kid"], d["i_dag"], "admin:test") == 0
    assert _oppmote(con, d["i_dag"]) == {d["pids"][0]: "qr", d["pids"][1]: "zoom", d["pids"][2]: "manuell", d["pids"][3]: "manuell"}
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='oppmote_manuell'").fetchone()[0] == 2
    assert _oppmote(con, d["i_gaar"]) == {}                                     # andre dager røres ikke


def test_fjern_alle_beholder_zoom_og_andre_dager(con):
    d = _bygg(con)
    for pid, kilde in zip(d["pids"], ("qr", "kode", "zoom", "manuell")):
        db.registrer_oppmote(con, pid, d["i_dag"], kilde)
    db.registrer_oppmote(con, d["pids"][0], d["i_gaar"], "qr")
    assert oppmoteliste.fjern_alle(con, d["kid"], d["i_dag"], "admin:test") == (3, 1)
    assert _oppmote(con, d["i_dag"]) == {d["pids"][2]: "zoom"}
    assert oppmoteliste.fjern_alle(con, d["kid"], d["i_dag"], "admin:test") == (0, 1)
    assert _oppmote(con, d["i_gaar"]) == {d["pids"][0]: "qr"}


def test_hvordan_tekst(con):
    assert oppmoteliste.hvordan_tekst(None) == ""
    assert oppmoteliste.hvordan_tekst("qr", "2026-09-30 06:47:00") == "QR-kode kl. 08:47"          # sommertid: UTC + 2
    assert oppmoteliste.hvordan_tekst("kode", "2027-01-12 08:05:00") == "Innsjekk-kode kl. 09:05"    # vintertid: UTC + 1
    assert oppmoteliste.hvordan_tekst("manuell", "2026-09-30 06:47:00") == "Manuelt kl. 08:47"
    assert oppmoteliste.hvordan_tekst("zoom", "2026-09-30 06:47:00", 95) == "Zoom, 95 min"
    assert oppmoteliste.hvordan_tekst("zoom", "2026-09-30 06:47:00", None) == "Zoom"


# ================================================================ siden

def test_siden_viser_kurs_dato_dag_teller_og_navn(con):
    d = _bygg(con)
    db.registrer_oppmote(con, d["pids"][0], d["i_dag"], "qr")
    con.commit()
    r = _logg_inn(_klient()).get(_side(d["kid"], d["i_dag"]))
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Kurs A" in html and "dag 2 av 3" in html and "i dag" in html
    assert re.search(r'<strong id="opp-antall">1</strong> av <span id="opp-totalt">4</span> til stede', html)
    navn = re.findall(r'<span class="opp-navn"[^>]*>([^<]*)</span>', html)
    assert navn == ["Kari Hansen", "Ola Nordmann", "Sølvi Tørresen", "Åse Ødegård"]
    assert "Bertil" not in html                                  # deltakere fra et annet kurs vises aldri
    assert 'aria-pressed="true"' in html and 'aria-pressed="false"' in html
    assert html.count('data-rad-skjema') == 4                    # hver rad er et vanlig skjema (virker uten JavaScript)


def test_raden_forteller_hvordan_det_er_registrert(con):
    d = _bygg(con)
    for pid, kilde, min_ in zip(d["pids"], ("qr", "kode", "zoom", "manuell"), (None, None, 95, None)):
        db.registrer_oppmote(con, pid, d["i_dag"], kilde, min_)
    con.execute("UPDATE oppmote SET ts='2026-09-30 06:47:00'")
    con.commit()
    html = _logg_inn(_klient()).get(_side(d["kid"], d["i_dag"])).get_data(as_text=True)
    assert "QR-kode kl. 08:47" in html and "Innsjekk-kode kl. 08:47" in html and "Zoom, 95 min" in html and "Manuelt kl. 08:47" in html
    assert 'data-kilde="zoom"' in html


def test_ekstradeltaker_vises_og_kan_merkes(con):
    d = _bygg(con)
    db.sett_paamelding_status(con, d["pids"][2], "ekstradeltaker")
    con.commit()
    klient = _logg_inn(_klient())
    assert "Sølvi Tørresen" in klient.get(_side(d["kid"], d["i_dag"])).get_data(as_text=True)
    assert _rad(klient, d["kid"], d["i_dag"], d["pids"][2], "til").get_json()["til_stede"] is True


def test_ingen_persondata_utover_navn_i_siden(con):
    d = _bygg(con)
    con.execute("""UPDATE deltaker SET telefon='91234567', adresse='Eksempelveien 1', postnr='0150', poststed='Oslo',
                   arbeidssted='Eksempelklinikken', hpr_nr='1234567' WHERE epost='kari.hansen@eksempel.no'""")
    pid = d["pids"][0]
    con.execute("INSERT INTO sensitivt (paamelding_id, allergier, tilrettelegging) VALUES (?,?,?)", (pid, "Nøtter", "Rullestol"))
    con.commit()
    html = _logg_inn(_klient()).get(_side(d["kid"], d["i_dag"])).get_data(as_text=True)
    synlig = _uten_sokenokler(html)
    for hemmelig in ("91234567", "Eksempelveien", "0150", "Eksempelklinikken", "1234567", "Nøtter", "Rullestol", "eksempel.no"):
        assert hemmelig not in synlig, hemmelig
    # e-posten ligger bare som skjult søkenøkkel, og den er ikke i noen lenke eller noe skjemafelt
    assert "kari.hansen@eksempel.no" in html and "kari.hansen@" not in synlig


def test_adressene_har_bare_id_er(con):
    d = _bygg(con)
    db.registrer_oppmote(con, d["pids"][2], d["i_dag"], "zoom", 30)
    con.commit()
    klient = _logg_inn(_klient())
    for adresse in ("", "?bekreft=merk_alle", "?bekreft=fjern_alle", f"?bekreft=zoom&p={d['pids'][2]}"):
        html = klient.get(_side(d["kid"], d["i_dag"]) + adresse).get_data(as_text=True)
        for verdi in re.findall(r'(?:href|action)="([^"]*)"', html):
            assert re.fullmatch(r"[/a-z0-9_.\-?=&#;%]*", verdi.replace("&amp;", "&")), verdi
            for navn in ("Kari", "Hansen", "Sølvi", "kari", "hansen", "@"):
                assert navn not in verdi


def test_fremtidig_dag_viser_forklaring_uten_knapper_og_uten_liste(con):
    d = _bygg(con)
    klient = _logg_inn(_klient())
    r = klient.get(_side(d["kid"], d["i_morgen"]))
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "Kursdagen er ikke startet ennå" in html and "kan bare tas opp fra og med kursdagen" in html
    assert "data-rad-skjema" not in html and "Merk alle" not in html and "Kari Hansen" not in html and 'id="opp-liste"' not in html
    # og selv et direkte POST avvises
    r = _rad(klient, d["kid"], d["i_morgen"], d["pids"][0], "til")
    assert r.status_code == 409 and r.get_json()["status"] == "feil"
    assert _alle(klient, d["kid"], d["i_morgen"], "merk_alle").status_code == 409
    assert _oppmote(con, d["i_morgen"]) == {}


def test_dag_som_er_over_kan_tas_opp(con):
    d = _bygg(con)
    klient = _logg_inn(_klient())
    assert "data-rad-skjema" in klient.get(_side(d["kid"], d["i_gaar"])).get_data(as_text=True)
    assert _rad(klient, d["kid"], d["i_gaar"], d["pids"][0], "til").status_code == 200


def test_dagfaner_bare_naar_kurset_har_flere_dager(con):
    d = _bygg(con)
    klient = _logg_inn(_klient())
    html = klient.get(_side(d["kid"], d["i_dag"])).get_data(as_text=True)
    assert 'aria-label="Velg kursdag"' in html and html.count("Dag ") >= 3 and html.count('aria-current="page"') >= 1
    assert 'aria-label="Velg kursdag"' not in klient.get(_side(d["kb"], d["dag_b"])).get_data(as_text=True)


def test_ingen_deltakere_gir_tydelig_tekst(con):
    kid = _kurs(con, "T0", [IDAG])
    dag = db.kursdager(con, kid)[0]["id"]
    con.commit()
    html = _logg_inn(_klient()).get(_side(kid, dag)).get_data(as_text=True)
    assert "Ingen deltakere har plass på kurset ennå." in html and "Merk alle" not in html


def test_standardruten_apner_dagens_dag_ellers_naermeste(con):
    d = _bygg(con)
    klient = _logg_inn(_klient())
    r = klient.get(f"/admin/kurs/{d['kid']}/oppmote")
    assert r.status_code == 302 and r.headers["Location"].endswith(_side(d["kid"], d["i_dag"]))
    fremtid = _kurs(con, "F1", [IDAG + timedelta(days=10), IDAG + timedelta(days=11)])
    con.commit()
    forste = db.kursdager(con, fremtid)[0]["id"]
    assert klient.get(f"/admin/kurs/{fremtid}/oppmote").headers["Location"].endswith(_side(fremtid, forste))
    tom = db.opprett_kurs(con, kode="TOM", navn="Uten dager", datoer=[], sharepoint_mappe="Kurs/TOM", pris_nok=0)
    con.commit()
    r = klient.get(f"/admin/kurs/{tom}/oppmote")
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/admin/kurs/{tom}/oppsett")
    assert klient.get("/admin/kurs/99999/oppmote").status_code == 404


def test_gammel_oppmotematrise_virker_uendret(con):
    d = _bygg(con)
    klient = _logg_inn(_klient())
    r = klient.post(f"/admin/kurs/{d['kid']}/oppmote", data={"paamelding_id": d["pids"][0], "kursdag_id": d["i_dag"]})
    assert r.status_code == 302 and _oppmote(con, d["i_dag"]) == {d["pids"][0]: "manuell"}
    klient.post(f"/admin/kurs/{d['kid']}/oppmote", data={"paamelding_id": d["pids"][0], "kursdag_id": d["i_dag"]})
    assert _oppmote(con, d["i_dag"]) == {}


# ================================================================ rutene: rad

def test_rad_til_og_fra_med_json_svar(con):
    d = _bygg(con)
    klient = _logg_inn(_klient())
    pid = d["pids"][1]
    r = _rad(klient, d["kid"], d["i_dag"], pid, "til")
    j = r.get_json()
    assert r.status_code == 200 and j["status"] == "ok" and j["til_stede"] is True and j["kilde"] == "manuell" and j["endret"] is True
    assert re.fullmatch(r"Manuelt kl\. \d\d:\d\d", j["hvordan"]) and (j["antall"], j["totalt"]) == (1, 4)
    j2 = _rad(klient, d["kid"], d["i_dag"], pid, "til").get_json()                 # samme forespørsel igjen: samme sluttresultat
    assert j2["til_stede"] is True and j2["endret"] is False and j2["antall"] == 1 and j2["hvordan"] == j["hvordan"]
    j3 = _rad(klient, d["kid"], d["i_dag"], pid, "fra").get_json()
    assert j3["til_stede"] is False and j3["kilde"] == "" and j3["hvordan"] == "" and j3["antall"] == 0
    assert _rad(klient, d["kid"], d["i_dag"], pid, "fra").get_json()["endret"] is False
    assert _oppmote(con, d["i_dag"]) == {}


def test_rad_uten_javascript_gir_viderekobling_tilbake_til_raden(con):
    d = _bygg(con)
    klient = _logg_inn(_klient())
    r = klient.post(_side(d["kid"], d["i_dag"]) + "/rad", data={"paamelding_id": d["pids"][0], "onsket": "til"})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"{_side(d['kid'], d['i_dag'])}#rad-{d['pids'][0]}")
    assert _oppmote(con, d["i_dag"]) == {d["pids"][0]: "manuell"}


def test_rad_ugyldig_input(con):
    d = _bygg(con)
    klient = _logg_inn(_klient())
    for felt in ({"paamelding_id": d["pids"][0], "onsket": "bytt"}, {"paamelding_id": d["pids"][0]}, {"paamelding_id": "abc", "onsket": "til"},
                 {"onsket": "til"}, {"paamelding_id": "", "onsket": "til"}):
        r = klient.post(_side(d["kid"], d["i_dag"]) + "/rad", data=felt, headers=JSON)
        assert r.status_code == 400 and r.get_json()["status"] == "feil", felt
    assert _oppmote(con, d["i_dag"]) == {}


def test_rad_med_enorm_paamelding_id_gir_404_ikke_500(con):
    d = _bygg(con)
    klient = _logg_inn(_klient())
    for pid in ("9" * 30, "0", "-5", str(2**63)):
        r = _rad(klient, d["kid"], d["i_dag"], pid, "til")
        assert r.status_code == 404, pid
    assert _oppmote(con, d["i_dag"]) == {}


def test_rad_for_paamelding_uten_plass_gir_409(con):
    d = _bygg(con)
    pid = _meld(con, d["kid"], "Vera", "Venter")
    db.sett_paamelding_status(con, pid, "venteliste")
    con.commit()
    r = _rad(_logg_inn(_klient()), d["kid"], d["i_dag"], pid, "til")
    assert r.status_code == 409 and "plass" in r.get_json()["melding"]
    assert _oppmote(con, d["i_dag"]) == {}


def test_zoom_i_ruten_krever_ekstra_bekreftelse(con):
    d = _bygg(con)
    pid = d["pids"][0]
    db.registrer_oppmote(con, pid, d["i_dag"], "zoom", 95)
    con.commit()
    klient = _logg_inn(_klient())
    r = _rad(klient, d["kid"], d["i_dag"], pid, "fra")
    assert r.status_code == 409 and r.get_json()["status"] == "trenger_bekreftelse" and _oppmote(con, d["i_dag"]) == {pid: "zoom"}
    r = _rad(klient, d["kid"], d["i_dag"], pid, "fra", bekreft_zoom="0")
    assert r.status_code == 409 and _oppmote(con, d["i_dag"]) == {pid: "zoom"}
    r = _rad(klient, d["kid"], d["i_dag"], pid, "fra", bekreft_zoom="1")
    assert r.status_code == 200 and r.get_json()["til_stede"] is False and _oppmote(con, d["i_dag"]) == {}


def test_zoom_uten_javascript_gar_via_bekreftelsesside(con):
    d = _bygg(con)
    pid = d["pids"][2]
    db.registrer_oppmote(con, pid, d["i_dag"], "zoom", 95)
    con.commit()
    klient = _logg_inn(_klient())
    r = klient.post(_side(d["kid"], d["i_dag"]) + "/rad", data={"paamelding_id": pid, "onsket": "fra"})
    assert r.status_code == 302 and f"bekreft=zoom&p={pid}" in r.headers["Location"] and r.headers["Location"].endswith("#bekreft")
    assert _oppmote(con, d["i_dag"]) == {pid: "zoom"}
    side = klient.get(_side(d["kid"], d["i_dag"]) + f"?bekreft=zoom&p={pid}").get_data(as_text=True)
    assert "Fjerne oppmøte som er hentet fra Zoom?" in side and 'name="bekreft_zoom" value="1"' in side and "Sølvi Tørresen" in side
    assert _oppmote(con, d["i_dag"]) == {pid: "zoom"}                       # å vise spørsmålet endrer ingenting
    tekst = re.search(r'<form method="post" action="([^"]*)">(.*?)</form>', side[side.index('id="bekreft"'):], re.S)
    assert tekst.group(1).endswith("/rad")
    r = klient.post(tekst.group(1), data={"paamelding_id": pid, "onsket": "fra", "bekreft_zoom": "1"})
    assert r.status_code == 302 and _oppmote(con, d["i_dag"]) == {}


def test_bekreftelsesside_ignorerer_ukjente_verdier(con):
    d = _bygg(con)
    klient = _logg_inn(_klient())
    for adresse in ("?bekreft=slett_alt", "?bekreft=%3Cscript%3E", f"?bekreft=zoom&p={d['pids'][0]}", "?bekreft=zoom&p=abc", "?bekreft=zoom"):
        html = klient.get(_side(d["kid"], d["i_dag"]) + adresse).get_data(as_text=True)
        assert 'id="bekreft"' not in html and "<script>" not in html, adresse       # ingen Zoom-oppmøte på p -> ingen bekreftelse


# ================================================================ rutene: merk alle / fjern alle

def test_alle_krever_bekreftelse_paa_tjeneren(con):
    d = _bygg(con)
    klient = _logg_inn(_klient())
    r = _alle(klient, d["kid"], d["i_dag"], "merk_alle", bekreft=False)
    assert r.status_code == 409 and r.get_json()["status"] == "trenger_bekreftelse" and _oppmote(con, d["i_dag"]) == {}
    r = klient.post(_side(d["kid"], d["i_dag"]) + "/alle", data={"handling": "merk_alle"})           # uten JavaScript
    assert r.status_code == 302 and "bekreft=merk_alle" in r.headers["Location"] and r.headers["Location"].endswith("#bekreft")
    assert _oppmote(con, d["i_dag"]) == {}
    r = _alle(klient, d["kid"], d["i_dag"], "merk_alle")
    j = r.get_json()
    assert r.status_code == 200 and j["status"] == "ok" and j["endret"] == 4 and (j["antall"], j["totalt"]) == (4, 4)
    assert set(_oppmote(con, d["i_dag"]).values()) == {"manuell"}
    assert _alle(klient, d["kid"], d["i_dag"], "merk_alle").get_json()["endret"] == 0                    # idempotent


def test_fjern_alle_og_zoom_beholdes(con):
    d = _bygg(con)
    for pid, kilde in zip(d["pids"], ("qr", "kode", "zoom", "manuell")):
        db.registrer_oppmote(con, pid, d["i_dag"], kilde)
    con.commit()
    klient = _logg_inn(_klient())
    assert _alle(klient, d["kid"], d["i_dag"], "fjern_alle", bekreft=False).status_code == 409
    assert len(_oppmote(con, d["i_dag"])) == 4
    j = _alle(klient, d["kid"], d["i_dag"], "fjern_alle").get_json()
    assert j["endret"] == 3 and "fra Zoom er beholdt" in j["melding"] and j["antall"] == 1
    assert _oppmote(con, d["i_dag"]) == {d["pids"][2]: "zoom"}


def test_bekreftelsespanelet_uten_javascript_viser_riktige_tall(con):
    d = _bygg(con)
    db.registrer_oppmote(con, d["pids"][0], d["i_dag"], "qr")
    db.registrer_oppmote(con, d["pids"][1], d["i_dag"], "zoom", 30)
    con.commit()
    klient = _logg_inn(_klient())
    merk = klient.get(_side(d["kid"], d["i_dag"]) + "?bekreft=merk_alle").get_data(as_text=True)
    assert "Merke alle som til stede?" in merk and "2 deltakere som ikke er registrert ennå" in merk and 'name="handling" value="merk_alle"' in merk
    assert 'name="bekreft" value="1"' in merk
    fjern = klient.get(_side(d["kid"], d["i_dag"]) + "?bekreft=fjern_alle").get_data(as_text=True)
    assert "Oppmøtet fjernes for 1 deltaker." in fjern and "1 registrering fra Zoom beholdes" in fjern
    assert len(_oppmote(con, d["i_dag"])) == 2                                # å vise spørsmålet endrer ingenting
    r = klient.post(_side(d["kid"], d["i_dag"]) + "/alle", data={"handling": "merk_alle", "bekreft": "1"})
    assert r.status_code == 302 and len(_oppmote(con, d["i_dag"])) == 4
    ferdig = klient.get(_side(d["kid"], d["i_dag"]) + "?bekreft=merk_alle").get_data(as_text=True)
    assert "Alle er allerede registrert som til stede." in ferdig and "Ja, merk alle" not in ferdig       # ingenting å gjøre


def test_alle_ugyldig_handling(con):
    d = _bygg(con)
    klient = _logg_inn(_klient())
    for h in ("slett", "", "MERK_ALLE"):
        assert klient.post(_side(d["kid"], d["i_dag"]) + "/alle", data={"handling": h, "bekreft": "1"}, headers=JSON).status_code == 400
    assert _oppmote(con, d["i_dag"]) == {}


def test_status_json_har_ingen_navn_eller_personopplysninger(con):
    d = _bygg(con)
    db.registrer_oppmote(con, d["pids"][0], d["i_dag"], "qr")
    con.commit()
    r = _logg_inn(_klient()).get(_side(d["kid"], d["i_dag"]) + "/status", headers=JSON)
    j = r.get_json()
    assert r.status_code == 200 and (j["antall"], j["totalt"]) == (1, 4) and set(j["rader"]) == {str(p) for p in d["pids"]}
    assert j["rader"][str(d["pids"][0])]["kilde"] == "qr" and j["rader"][str(d["pids"][1])]["til_stede"] is False
    tekst = r.get_data(as_text=True)
    for hemmelig in ("Kari", "Hansen", "eksempel", "@"):
        assert hemmelig not in tekst
    assert "no-store" in r.headers["Cache-Control"]


# ================================================================ tilgang: innlogging, roller, CSRF, IDOR

def test_uten_innlogging(con):
    d = _bygg(con)
    k = _klient()
    for url in (_side(d["kid"], d["i_dag"]), f"/admin/kurs/{d['kid']}/oppmote"):
        r = k.get(url)
        assert r.status_code == 302 and "/admin/logg-inn" in r.headers["Location"]
    r = k.post(_side(d["kid"], d["i_dag"]) + "/rad", data={"paamelding_id": d["pids"][0], "onsket": "til"})
    assert r.status_code == 302 and "/admin/logg-inn" in r.headers["Location"]
    r = _rad(k, d["kid"], d["i_dag"], d["pids"][0], "til")                   # fra siden (fetch): JSON, aldri innloggingssiden
    assert r.status_code == 401 and r.get_json()["feil"] == "ikke_innlogget"
    assert _alle(k, d["kid"], d["i_dag"], "merk_alle").status_code == 401
    assert k.get(_side(d["kid"], d["i_dag"]) + "/status", headers=JSON).status_code == 401
    assert _oppmote(con, d["i_dag"]) == {}


@pytest.mark.parametrize("rolle", ["system", "kursadmin"])
def test_system_og_kursadmin_kan_endre(con, rolle):
    d = _bygg(con)
    klient = _bruker(con, rolle)
    html = klient.get(_side(d["kid"], d["i_dag"])).get_data(as_text=True)
    assert "data-rad-skjema" in html and "Merk alle til stede" in html and "Fjern alle merkede" in html
    assert _rad(klient, d["kid"], d["i_dag"], d["pids"][0], "til").status_code == 200
    assert _alle(klient, d["kid"], d["i_dag"], "merk_alle").status_code == 200


def test_lesetilgang_ser_listen_uten_knapper_og_far_403(con):
    d = _bygg(con)
    db.registrer_oppmote(con, d["pids"][0], d["i_dag"], "qr")
    con.commit()
    klient = _bruker(con, "lese")
    r = klient.get(_side(d["kid"], d["i_dag"]))
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "Kari Hansen" in html and "QR-kode" in html and "lesetilgang" in html
    assert "<button" not in html.split('id="opp-liste"')[1] and "data-rad-skjema" not in html
    assert "Merk alle" not in html and "Fjern alle" not in html and "data-oppmote-alle" not in html
    assert klient.get(_side(d["kid"], d["i_dag"]) + "?bekreft=merk_alle").status_code == 200
    assert 'id="bekreft"' not in klient.get(_side(d["kid"], d["i_dag"]) + "?bekreft=merk_alle").get_data(as_text=True)
    assert klient.get(_side(d["kid"], d["i_dag"]) + "/status", headers=JSON).status_code == 200
    # POST: 403 (HTML og JSON), og ingenting endres
    for hodere in ({}, JSON):
        assert klient.post(_side(d["kid"], d["i_dag"]) + "/rad", data={"paamelding_id": d["pids"][1], "onsket": "til"}, headers=hodere).status_code == 403
        assert klient.post(_side(d["kid"], d["i_dag"]) + "/alle", data={"handling": "merk_alle", "bekreft": "1"}, headers=hodere).status_code == 403
        assert klient.post(_side(d["kid"], d["i_dag"]) + "/rad", data={"paamelding_id": d["pids"][0], "onsket": "fra"}, headers=hodere).status_code == 403
    assert _oppmote(con, d["i_dag"]) == {d["pids"][0]: "qr"}


def test_csrf_er_paakrevd(con):
    d = _bygg(con)
    klient = _logg_inn(_klient())
    klient.injiser_csrf = False
    r = klient.post(_side(d["kid"], d["i_dag"]) + "/rad", data={"paamelding_id": d["pids"][0], "onsket": "til"}, headers=JSON)
    assert r.status_code == 400
    r = klient.post(_side(d["kid"], d["i_dag"]) + "/alle", data={"handling": "merk_alle", "bekreft": "1"}, headers=JSON)
    assert r.status_code == 400 and _oppmote(con, d["i_dag"]) == {}
    r = klient.post(_side(d["kid"], d["i_dag"]) + "/rad", data={"paamelding_id": d["pids"][0], "onsket": "til", "csrf_token": "feil"}, headers=JSON)
    assert r.status_code == 400
    html = klient.get(_side(d["kid"], d["i_dag"])).get_data(as_text=True)          # siden gir tokenet, som fetch sender som hodet X-CSRF-Token
    token = re.search(r'data-csrf="([^"]+)"', html).group(1)
    r = klient.post(_side(d["kid"], d["i_dag"]) + "/rad", data={"paamelding_id": d["pids"][0], "onsket": "til"},
                    headers={**JSON, "X-CSRF-Token": token})
    assert r.status_code == 200 and _oppmote(con, d["i_dag"]) == {d["pids"][0]: "manuell"}


def test_ugyldige_kurs_og_dag_id_er(con):
    d = _bygg(con)
    klient = _logg_inn(_klient())
    for url in ("/admin/kurs/99999/oppmote/1", f"/admin/kurs/{d['kid']}/oppmote/99999", "/admin/kurs/abc/oppmote/1", f"/admin/kurs/{d['kid']}/oppmote/abc",
                f"/admin/kurs/{d['kid']}/oppmote/-1", f"/admin/kurs/{d['kid']}/oppmote/0"):
        assert klient.get(url).status_code == 404, url
        assert klient.get(url + "/status", headers=JSON).status_code == 404, url
    for url in ("/admin/kurs/99999/oppmote/1/rad", f"/admin/kurs/{d['kid']}/oppmote/99999/rad"):
        assert klient.post(url, data={"paamelding_id": d["pids"][0], "onsket": "til"}, headers=JSON).status_code == 404, url
    assert klient.post(f"/admin/kurs/{d['kid']}/oppmote/99999/alle", data={"handling": "merk_alle", "bekreft": "1"}, headers=JSON).status_code == 404


def test_idor_dag_fra_annet_kurs(con):
    d = _bygg(con)
    klient = _logg_inn(_klient())
    # kurs A sin adresse med kurs B sin kursdag: 404 på alt, og B sitt oppmøte røres ikke
    dag_b = d["dag_b"]
    assert klient.get(_side(d["kid"], dag_b)).status_code == 404
    assert klient.get(_side(d["kid"], dag_b) + "/status", headers=JSON).status_code == 404
    assert _rad(klient, d["kid"], dag_b, d["pids"][0], "til").status_code == 404
    assert _alle(klient, d["kid"], dag_b, "merk_alle").status_code == 404
    assert _alle(klient, d["kb"], d["i_dag"], "merk_alle").status_code == 404          # og motsatt vei
    assert _oppmote(con, dag_b) == {} and _oppmote(con, d["i_dag"]) == {}


def test_idor_paamelding_fra_annet_kurs(con):
    d = _bygg(con)
    klient = _logg_inn(_klient())
    r = _rad(klient, d["kid"], d["i_dag"], d["pb"], "til")                           # Bertil (kurs B) på kurs A sin dag
    assert r.status_code == 404
    r = _rad(klient, d["kb"], d["dag_b"], d["pids"][0], "til")                       # Kari (kurs A) på kurs B sin dag
    assert r.status_code == 404
    assert con.execute("SELECT COUNT(*) FROM oppmote").fetchone()[0] == 0 and con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='oppmote_manuell'").fetchone()[0] == 0


# ================================================================ kursbevis og logg

def test_oppmote_fra_listen_gir_kursbevis_som_annet_oppmote(con):
    kid = _kurs(con, "KB1", [IDAG - timedelta(days=2), IDAG - timedelta(days=1)], navn="Bevis-kurs")
    a, b = _meld(con, kid, "Anne", "Alfa"), _meld(con, kid, "Bjørn", "Beta")
    dager = [d["id"] for d in db.kursdager(con, kid)]
    con.commit()
    klient = _logg_inn(_klient())
    for dag in dager:                                              # Anne registreres begge dagene, Bjørn bare den første
        assert _rad(klient, kid, dag, a, "til").status_code == 200
    assert _rad(klient, kid, dager[0], b, "til").status_code == 200
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()
    kursbevis.kjor(Kjoring(con, idag=IDAG))
    mottakere = [r["deltaker_id"] for r in con.execute("SELECT deltaker_id FROM dokument WHERE type='kursbevis'")]
    assert mottakere == [con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (a,)).fetchone()[0]]
    # Bjørn mangler en dag: får han den registrert (og oppmøtet er fjernet igjen fra Anne), snur det
    assert _rad(klient, kid, dager[1], b, "til").status_code == 200
    kursbevis.kjor(Kjoring(con, idag=IDAG))
    assert con.execute("SELECT COUNT(*) FROM dokument WHERE type='kursbevis'").fetchone()[0] == 2


def test_ingen_persondata_i_hendelseslogg_eller_applikasjonslogg(con, caplog):
    d = _bygg(con)
    klient = _logg_inn(_klient())
    with caplog.at_level("DEBUG"):
        _rad(klient, d["kid"], d["i_dag"], d["pids"][0], "til")
        _rad(klient, d["kid"], d["i_dag"], d["pids"][0], "fra")
        _rad(klient, d["kid"], d["i_dag"], d["pids"][1], "til")
        _alle(klient, d["kid"], d["i_dag"], "merk_alle")
        _alle(klient, d["kid"], d["i_dag"], "fjern_alle")
        klient.get(_side(d["kid"], d["i_dag"]) + "?bekreft=merk_alle")
    rader = con.execute("SELECT aktor, handling, detaljer FROM hendelse WHERE handling='oppmote_manuell'").fetchall()
    assert len(rader) >= 6
    alt = " ".join(f"{r['aktor']} {r['handling']} {r['detaljer']}" for r in rader) + " " + caplog.text
    for hemmelig in ("Kari", "Hansen", "Ola", "Nordmann", "Sølvi", "Tørresen", "Åse", "Ødegård", "eksempel", "@eksempel"):
        assert hemmelig not in alt, hemmelig
    for r in rader:
        assert set(json.loads(r["detaljer"])) == {"paamelding_id", "kursdag_id", "registrert"}      # bare id-er og om det ble registrert


def test_handlingene_vises_lesbart_i_deltakerens_logger(con):
    d = _bygg(con)
    klient = _logg_inn(_klient())
    _rad(klient, d["kid"], d["i_dag"], d["pids"][0], "til")
    _rad(klient, d["kid"], d["i_dag"], d["pids"][0], "fra")
    html = klient.get(f"/admin/kurs/{d['kid']}/deltaker/{d['pids'][0]}/logger").get_data(as_text=True)
    assert "Oppmøte registrert for kursdagen" in html and "Oppmøte fjernet for kursdagen" in html


# ================================================================ tilgjengelighet og stil

def test_tilgjengelighet_i_siden(con):
    d = _bygg(con)
    db.registrer_oppmote(con, d["pids"][0], d["i_dag"], "qr")
    con.commit()
    html = _logg_inn(_klient()).get(_side(d["kid"], d["i_dag"])).get_data(as_text=True)
    assert '<html lang="no">' in html and html.count("<h1>") == 1
    assert re.search(r'<p class="opp-teller" id="opp-teller" aria-live="polite" aria-atomic="true">', html)      # telleren leses opp
    assert re.search(r'<label class="skjul-visuelt" for="opp-sok">[^<]+</label>\s*<input id="opp-sok" type="search"', html)
    assert 'id="opp-melding" role="status"' in html and 'id="opp-feil" role="alert"' in html
    assert re.search(r'<p class="opp-sokstatus" id="opp-sokstatus" role="status">', html)
    for pid in re.findall(r'aria-describedby="(opp-info-\d+)"', html) + re.findall(r'aria-labelledby="(opp-navn-\d+)"', html):
        assert f'id="{pid}"' in html                                    # hver knapp peker på tekster som finnes (navnet, og hvordan det er registrert)
    assert len(re.findall(r'aria-labelledby="opp-navn-\d+"', html)) == 4          # navnet er knappens tilgjengelige navn, uten den lille teksten
    assert html.count("aria-pressed=") == 4                             # av/på-tilstanden er også tilgjengelig for skjermlesere
    assert 'aria-label="Velg kursdag"' in html and 'aria-label="Deltakere"' in html
    assert 'id="opp-sok-boks" hidden' in html                            # søket vises først når JavaScript virker
    assert '<a class="knapp sekundar opp-ferdig" href="/admin/kurs/' in html and ">Ferdig</a>" in html
    assert re.search(r'<script nonce="[^"]+" src="/static/oppmoteliste.js" defer>', html)          # CSP: skriptet lastes med nonce, ingen inline-skript
    assert "<script>" not in html and " onclick=" not in html


def _lys(hexfarge: str) -> float:
    r, g, b = (int(hexfarge[i:i + 2], 16) / 255 for i in (1, 3, 5))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _kontrast(a: str, b: str) -> float:
    la, lb = sorted((_lys(a), _lys(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_kontrast_og_stoerrelse_i_stilarket():
    """Fargeparene i stilarket (skrevet inn her; testen sjekker at de faktisk står i stilarket) oppfyller WCAG AA."""
    css = (MAPPE / "static" / "oppmoteliste.css").read_text(encoding="utf-8").lower()
    tekst = [("#ffffff", "#1f6a43"), ("#1d2a33", "#ffffff"), ("#1d2a33", "#e6f3ea"), ("#4a5761", "#ffffff"), ("#4a5761", "#e6f3ea"),
             ("#33404a", "#ffffff"), ("#8f1d16", "#fdf0ef"), ("#ffffff", "#8f1d16"), ("#ffffff", "#1d2a33"), ("#5f4300", "#fbf1d9"),
             ("#1f6a43", "#f7f8f7")]                                      # den siste: telleren på sidens bakgrunn
    for forgrunn, bakgrunn in tekst:
        finnes = lambda farge: farge in css or (farge == "#ffffff" and "#fff" in css)          # hvitt kan stå som #fff
        assert finnes(forgrunn) and (finnes(bakgrunn) or bakgrunn == "#f7f8f7"), (forgrunn, bakgrunn)
        assert _kontrast(forgrunn, bakgrunn) >= 4.5, (forgrunn, bakgrunn, _kontrast(forgrunn, bakgrunn))
    # kanten rundt søkefeltet og merkelappen skiller seg fra bakgrunnen (3:1 for kontroller)
    assert "#6f7f8a" in css and _kontrast("#6f7f8a", "#ffffff") >= 3.0 and "#8a98a1" not in css
    blokk = re.search(r"\.opp-knapp, button\.opp-knapp \{([^}]*)\}", css, re.S).group(1)
    assert int(re.search(r"min-height:(\d+)px", blokk).group(1)) >= 56           # hele raden er trykkflaten, minst 56 px
    assert "position:sticky" in css and "focus-visible" in css


def test_javascript_bruker_ikke_innerhtml_og_sender_csrf_hodet():
    js = (MAPPE / "static" / "oppmoteliste.js").read_text(encoding="utf-8")
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)                    # kommentarene får gjerne nevne innerHTML
    assert "innerHTML" not in js and "eval(" not in js and "document.write" not in js
    assert "X-CSRF-Token" in js and '"Accept": "application/json"' in js and "credentials" in js
    assert "textContent" in js


# ================================================================ lenkene til siden

def test_lenke_i_oppsett_per_dag_tydeligst_for_dagens(con):
    d = _bygg(con)
    html = _logg_inn(_klient()).get(f"/admin/kurs/{d['kid']}/oppsett").get_data(as_text=True)
    for dag in (d["i_gaar"], d["i_dag"], d["i_morgen"]):
        assert f'href="{_side(d["kid"], dag)}"' in html
    dagens = re.search(r'<a class="([^"]*)" href="%s">Ta opp oppmøte</a>' % re.escape(_side(d["kid"], d["i_dag"])), html)
    andre = re.search(r'<a class="([^"]*)" href="%s">Ta opp oppmøte</a>' % re.escape(_side(d["kid"], d["i_morgen"])), html)
    assert "sekundar" not in dagens.group(1) and "sekundar" in andre.group(1)
    assert "Vis QR på skjerm" in html                                     # QR-lenken er uendret


def test_lenke_fra_qr_plakaten(con):
    d = _bygg(con)
    html = _logg_inn(_klient()).get(f"/admin/kurs/{d['kid']}/qr/{d['i_dag']}").get_data(as_text=True)
    assert html.count(f'href="{_side(d["kid"], d["i_dag"])}"') == 2
    assert "hvis noen ikke får skannet" in html and "sjekket inn" in html and "Skriv ut plakat" in html


def test_knapp_i_deltakerfanen_bare_naar_kurset_har_kursdag_i_dag(con):
    d = _bygg(con)
    klient = _logg_inn(_klient())
    html = klient.get(f"/admin/kurs/{d['kid']}/deltakere").get_data(as_text=True)
    assert f'href="{_side(d["kid"], d["i_dag"])}">Ta opp oppmøte i dag</a>' in html
    fremtid = _kurs(con, "F2", [IDAG + timedelta(days=5)])
    con.commit()
    html = klient.get(f"/admin/kurs/{fremtid}/deltakere").get_data(as_text=True)
    assert "Ta opp oppmøte i dag" not in html and f"/admin/kurs/{fremtid}/oppmote" in html         # bare lenken ved oppmøtematrisen
    assert "Ta opp oppmøte i dag" not in _logg_inn(_klient()).get(f"/admin/kurs/{_kurs(con, 'F3', [IDAG - timedelta(days=3)])}/deltakere").get_data(as_text=True)


def test_lenke_i_oversikten_for_kurs_med_kursdag_i_dag(con):
    d = _bygg(con)
    fremtid = _kurs(con, "F4", [IDAG + timedelta(days=5)], navn="Fremtidskurs")
    con.commit()
    klient = _logg_inn(_klient())
    html = klient.get("/admin").get_data(as_text=True)
    assert html.count("Ta opp oppmøte i dag") == 2                        # kurs A og kurs B har kursdag i dag, fremtidskurset ikke
    assert f'href="{_side(d["kid"], d["i_dag"])}"' in html and f'href="{_side(d["kb"], d["dag_b"])}"' in html
    assert f"/admin/kurs/{fremtid}/oppmote" not in html
    con.execute("UPDATE kurs SET status='utkast' WHERE id=?", (d["kb"],))
    con.commit()
    assert klient.get("/admin").get_data(as_text=True).count("Ta opp oppmøte i dag") == 1     # utkast: ikke åpent for innsjekk


def test_lesetilgang_far_lenker_med_lesetekst(con):
    d = _bygg(con)
    klient = _bruker(con, "lese")
    assert "Se oppmøteliste i dag" in klient.get("/admin").get_data(as_text=True)
    assert "Se oppmøteliste i dag" in klient.get(f"/admin/kurs/{d['kid']}/deltakere").get_data(as_text=True)
    assert "Se oppmøteliste" in klient.get(f"/admin/kurs/{d['kid']}/oppsett").get_data(as_text=True)


def test_qr_og_innsjekk_paa_min_side_er_uendret_og_oppmoete_vises_i_listen(con):
    from kurs import deltakerside
    d = _bygg(con)
    kd = con.execute("SELECT * FROM kursdag WHERE id=?", (d["i_dag"],)).fetchone()
    res = deltakerside.registrer_qr(con, kd, IDAG, epost="ola.nordmann@eksempel.no")
    assert res.status == "ny"
    con.commit()
    html = _logg_inn(_klient()).get(_side(d["kid"], d["i_dag"])).get_data(as_text=True)
    assert "QR-kode kl." in html and re.search(r'<strong id="opp-antall">1</strong>', html)
    # og trykk fra listen på den som allerede er registrert med QR endrer ikke kilden
    assert _rad(_logg_inn(_klient()), d["kid"], d["i_dag"], d["pids"][1], "til").get_json()["kilde"] == "qr"


def test_navn_med_html_og_anforselstegn_er_trygge(con):
    kid = _kurs(con, "X1", [IDAG])
    dag = db.kursdager(con, kid)[0]["id"]
    pid = _meld(con, kid, "<b>Fet</b>", '"Sitat" <script>x()</script>', epost='a"b<i>@eksempel.no')
    con.commit()
    html = _logg_inn(_klient()).get(_side(kid, dag)).get_data(as_text=True)
    assert "<b>Fet</b>" not in html and "<script>x()</script>" not in html and '"Sitat" <' not in html
    assert "&lt;b&gt;Fet&lt;/b&gt;" in html and f'id="rad-{pid}"' in html
    j = _rad(_logg_inn(_klient()), kid, dag, pid, "til").get_json()
    assert j["status"] == "ok" and "Fet" not in json.dumps(j)                      # svaret fra tjeneren har aldri navn
