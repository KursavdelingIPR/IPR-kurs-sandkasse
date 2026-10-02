"""Statusvelgeren i deltakerlisten: admin endrer påmeldingsstatus direkte i hver rad.

Velgeren bruker den EKSISTERENDE statusruten (admin_deltaker_status, db.sett_paamelding_status): ingen ny statuslogikk.
Nytt er bare at skjemaet i listen sender med `neste` (listen med søk og statusvalg), slik at admin kommer tilbake dit med
en melding som starter med navnet på deltakeren, og at dialogen sier hva som skjer. Databasen er uendret.
"""
import re
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode

import pytest

from kurs import config, db, statustekster
from listehjelp import skjulte_rader, synlige_rader

ADMIN = f"admin:{config.ADMIN_BRUKERNAVN}"
STATUSER = list(db.PAAMELDINGSSTATUSER)
OPPRETTELSE = ["avmeldt", "avslatt", "utgatt", "forlatt", "paameldt", "ekstradeltaker", "venteliste"]   # venteliste sist: ellers rykker den opp
NAVN = db.PAAMELDINGSSTATUSER


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


# ============================ hjelpere ============================

def _klient(con, rolle=None):
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    data = {"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD}
    if rolle:
        db.opprett_admin_bruker(con, f"test.{rolle}", f"Test {rolle.capitalize()}", "passord-som-holder", rolle=rolle)
        con.commit()
        data = {"brukernavn": f"test.{rolle}", "passord": "passord-som-holder"}
    assert k.post("/admin/logg-inn", data=data).status_code == 302
    return k


def _kurs(con, kapasitet=10, kode="LIST1", **felter) -> int:
    felter = {"pris_nok": 2500, "fakturering": "person", **felter}
    kid = db.opprett_kurs(con, kode=kode, navn="Veiledning i gruppe", datoer=[date(2031, 10, 16).isoformat()],
                          sharepoint_mappe=f"Kurs/{kode}", kapasitet=kapasitet, **felter)
    con.commit()
    return kid


def _meld_paa(con, kid, fornavn, etternavn="Test") -> int:
    pid, _ = db.meld_paa(con, kid, epost=f"{fornavn.lower()}@eksempel.no", fornavn=fornavn, etternavn=etternavn)
    con.commit()
    return pid


def _i_status(con, kid, fornavn, status) -> int:
    """En påmelding som har fått `status` av admin (kurset har ledig plass)."""
    pid = _meld_paa(con, kid, fornavn)
    if status != "paameldt":
        db.sett_paamelding_status(con, pid, status, aktor=ADMIN)
        con.commit()
    return pid


def _status(pid) -> str:
    fersk = db.koble(config.DB_STI)
    try:
        return db.paameldingsstatus(fersk.execute("SELECT * FROM paamelding WHERE id=?", (pid,)).fetchone())
    finally:
        fersk.close()


def _eposter(fornavn) -> list[str]:
    fersk = db.koble(config.DB_STI)
    try:
        return [r[0] for r in fersk.execute("SELECT type FROM utsending_logg WHERE mottaker=? ORDER BY sendt_ts",
                                            (f"{fornavn.lower()}@eksempel.no",))]
    finally:
        fersk.close()


def _antall(sql, *args) -> int:
    fersk = db.koble(config.DB_STI)
    try:
        return fersk.execute(sql, args).fetchone()[0]
    finally:
        fersk.close()


def _ute(k, kid, pid, status, neste=None, **ekstra):
    """POST fra statusvelgeren i listen (med `neste`) - uten å følge omdirigeringen."""
    data = {"status": status, **ekstra}
    if neste is not None:
        data["neste"] = neste
    return k.post(f"/admin/kurs/{kid}/deltaker/{pid}/status", data=data)


class _Velgere(HTMLParser):
    """Trekker ut statusskjemaene (form.radstatus) fra listen: skjulte felt, nedtrekket, alternativene og knappen."""

    def __init__(self):
        super().__init__()
        self.skjemaer, self._s, self._alt, self._knapp = [], None, None, False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and "radstatus" in (a.get("class") or "").split():
            self._s = {"attr": a, "skjult": {}, "valg": [], "select": None, "knapp": None, "knapptekst": ""}
            self.skjemaer.append(self._s)
        elif self._s is not None:
            if tag == "input" and a.get("type") == "hidden":
                self._s["skjult"][a["name"]] = a.get("value", "")
            elif tag == "select":
                self._s["select"] = a
            elif tag == "option":
                self._alt = {"attr": a, "tekst": ""}
                self._s["valg"].append(self._alt)
            elif tag == "button":
                self._s["knapp"], self._knapp = a, True

    def handle_data(self, data):
        if self._alt is not None:
            self._alt["tekst"] += data
        if self._knapp:
            self._s["knapptekst"] += data

    def handle_endtag(self, tag):
        if tag == "option":
            self._alt = None
        elif tag == "button":
            self._knapp = False
        elif tag == "form":
            self._s = None


def _velgere(html) -> dict:
    """{paamelding_id: skjema} for alle radene i listen."""
    p = _Velgere()
    p.feed(html)
    return {int(re.search(r"/deltaker/(\d+)/status", s["attr"]["action"]).group(1)): s for s in p.skjemaer}


def _liste(k, kid, sporring="") -> str:
    return k.get(f"/admin/kurs/{kid}/deltakere{sporring}").get_data(as_text=True)


def _liste_adresse(kid, *par) -> str:
    return f"/admin/kurs/{kid}/deltakere" + ("?" + urlencode(par) if par else "")


def _flasher(html) -> list[str]:
    return [m.group(2).strip() for m in re.finditer(r'<div class="flash (\w+)" role="[a-z]+">(.*?)</div>', html, re.S)]


# ============================ hver rad har en statusvelger ============================

def test_hver_rad_har_statusvelger_med_alle_syv_og_gjeldende_valgt(con):
    kid = _kurs(con, kapasitet=10)
    pid = {s: _i_status(con, kid, s.capitalize(), s) for s in OPPRETTELSE}
    velgere = _velgere(_liste(_klient(con), kid))
    assert set(velgere) == set(pid.values())
    for status, p in pid.items():
        v = velgere[p]
        assert [a["attr"]["value"] for a in v["valg"]] == STATUSER                                # alle syv, i rekkefølge
        assert [a["tekst"] for a in v["valg"]] == list(NAVN.values())                              # med navnet som vises
        assert [a["attr"]["value"] for a in v["valg"] if "selected" in a["attr"]] == [status]      # nåværende valgt
        assert v["select"]["name"] == "status" and v["select"]["class"].startswith("statusvelger ")
        assert v["select"]["aria-label"] == f"Påmeldingsstatus for {status.capitalize()} Test"      # skjermleser
        assert v["attr"]["method"] == "post" and v["attr"]["action"] == f"/admin/kurs/{kid}/deltaker/{p}/status"
        assert v["skjult"]["csrf_token"] and v["skjult"]["neste"] == _liste_adresse(kid)
        assert "data-status-bekreft" in v["attr"] and v["attr"]["data-naa"] == status
        # uten JavaScript fungerer skjemaet med nedtrekket og «Endre» (knappen skjules bare av static/app.js)
        assert v["knapptekst"].strip() == "Endre" and "hidden" not in v["knapp"]
        assert v["knapp"]["aria-label"] == f"Endre status for {status.capitalize()} Test"      # skiller «Endre» i radene


def test_fargen_paa_velgeren_er_som_statusmerket(con):
    kid = _kurs(con)
    farger = {"paameldt": "ok", "ekstradeltaker": "bla", "venteliste": "gul", "avmeldt": "gra", "avslatt": "feil",
              "utgatt": "gra", "forlatt": "feil"}
    pid = {s: _i_status(con, kid, s.capitalize(), s) for s in OPPRETTELSE}
    velgere = _velgere(_liste(_klient(con), kid))
    for status, p in pid.items():
        assert velgere[p]["select"]["class"] == f"statusvelger {farger[status]}"


def test_neste_i_skjemaet_er_listen_med_soek_og_statusvalg(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid, "Mari")
    v = _velgere(_liste(_klient(con), kid, "?" + urlencode([("sok", "Øyvind"), ("status", "venteliste")])))[pid]
    assert v["skjult"]["neste"] == f"/admin/kurs/{kid}/deltakere?sok=%C3%98yvind&status=venteliste"


def test_skjulte_rader_har_ogsaa_velger_og_soket_virker_som_for(con):
    """Søket og statusfilteret skjuler bare rader (hidden): statusvelgeren rører ikke data-sok/data-status."""
    kid = _kurs(con)
    mari = _meld_paa(con, kid, "Mari")
    stig = _i_status(con, kid, "Stig", "venteliste")
    html = _liste(_klient(con), kid, "?status=venteliste")
    assert "Stig Test" in synlige_rader(html) and "Mari Test" in skjulte_rader(html)
    assert set(_velgere(html)) == {mari, stig}
    assert 'data-status="paameldt"' in html and 'data-status="venteliste"' in html


# ============================ dialogen sier hva som skjer ============================

def test_hvert_alternativ_har_egen_dialogtekst_bortsett_fra_gjeldende_status(con):
    kid = _kurs(con)
    pid = {s: _i_status(con, kid, s.capitalize(), s) for s in OPPRETTELSE}
    velgere = _velgere(_liste(_klient(con), kid))
    for status, p in pid.items():
        for a in velgere[p]["valg"]:
            verdi = a["attr"]["value"]
            if verdi == status:
                assert "data-bekreft" not in a["attr"]
            else:
                assert a["attr"]["data-bekreft"].startswith(
                    f"Endre status for {status.capitalize()} Test fra {NAVN[status]} til {NAVN[verdi]}?")


def test_dialogtekstene_beskriver_plass_venteliste_e_post_og_faktura():
    kurs = {"fakturering": "person", "pris_nok": 2500, "status": "aapen", "kapasitet": 10}
    gratis = {"fakturering": "ingen", "pris_nok": 0, "status": "aapen", "kapasitet": 10}
    t = statustekster.dialogtekst("Kari Test", "paameldt", "avmeldt", kurs=kurs)
    assert t.startswith("Endre status for Kari Test fra Påmeldt til Avmeldt?")
    assert "Plassen blir ledig" in t and "rykker den første opp" in t and "Deltakeren får ingen e-post." in t
    t = statustekster.dialogtekst("Kari Test", "venteliste", "utgatt", kurs=kurs)
    assert "tas av ventelisten" in t and "Plassen blir ledig" not in t and "ingen e-post" in t
    t = statustekster.dialogtekst("Kari Test", "avmeldt", "forlatt", kurs=kurs)
    assert "Plassen blir ledig" not in t and "ventelisten" not in t and "ingen e-post" in t
    assert "Avslå påmeldingen" in statustekster.dialogtekst("Kari Test", "paameldt", "avslatt", kurs=kurs)   # avslag med e-post
    assert "Avslå påmeldingen" not in statustekster.dialogtekst("Kari Test", "paameldt", "forlatt", kurs=kurs)
    t = statustekster.dialogtekst("Kari Test", "venteliste", "paameldt", kurs=kurs)
    assert "bekreftelse på e-post" in t and "fakturaen behandles" in t
    t = statustekster.dialogtekst("Kari Test", "venteliste", "paameldt", kurs=gratis)
    assert "bekreftelse på e-post" in t and "faktura" not in t
    t = statustekster.dialogtekst("Kari Test", "paameldt", "venteliste", kurs=kurs)
    assert "Plassen blir ledig" in t and "ventelistebeskjed" in t
    t = statustekster.dialogtekst("Kari Test", "avmeldt", "venteliste", kurs=kurs)
    assert "Plassen blir ledig" not in t and "ventelistebeskjed" in t
    t = statustekster.dialogtekst("Kari Test", "paameldt", "avmeldt", kurs=kurs, fakturert=True)
    assert "allerede fakturert" in t and "krediteres ikke automatisk" in t
    assert "fakturert" not in statustekster.dialogtekst("Kari Test", "paameldt", "avmeldt", kurs=kurs)


def test_dialogtekstene_bruker_navnet_paa_statusene_fra_db():
    """Ordene kommer fra db.PAAMELDINGSSTATUSER: ingen egne navn i statustekster.py."""
    t = statustekster.dialogtekst("Kari Test", "venteliste", "paameldt", kurs={"fakturering": "ingen", "pris_nok": 0,
                                                                                     "status": "aapen", "kapasitet": 10})
    assert f"fra {NAVN['venteliste']} til {NAVN['paameldt']}?" in t
    assert statustekster.fullt_kurs_tekst("Kari Test", 1, 1) == \
        "Er du sikker på at du vil melde Kari Test på? Kurset er fullt (1 av 1 plass er tatt)."
    assert "(24 av 24 plasser er tatt)" in statustekster.fullt_kurs_tekst("Kari Test", 24, 24)


def test_overbooking_fullt_kurs_spoersmaal_paa_alle_rader_som_ikke_er_paameldt(con):
    kid = _kurs(con, kapasitet=1)
    ola = _meld_paa(con, kid, "Ola")                                       # fyller kurset
    stig = _meld_paa(con, kid, "Stig")                                     # venteliste
    tone = _i_status(con, kid, "Tone", "avmeldt")
    velgere = _velgere(_liste(_klient(con), kid))
    sporsmaal = "Er du sikker på at du vil melde {} på? Kurset er fullt (1 av 1 plass er tatt)."
    for pid, navn in [(stig, "Stig Test"), (tone, "Tone Test")]:
        assert velgere[pid]["attr"]["data-full-bekreft"] == sporsmaal.format(navn)
        assert velgere[pid]["skjult"]["overbooking"] == ""                # settes til 1 av static/app.js etter «ja»
    assert "data-full-bekreft" not in velgere[ola]["attr"] and "overbooking" not in velgere[ola]["skjult"]


def test_ingen_overbookingsporsmaal_naar_kurset_har_ledig_plass(con):
    kid = _kurs(con, kapasitet=5)
    pid = _i_status(con, kid, "Stig", "venteliste")
    v = _velgere(_liste(_klient(con), kid))[pid]
    assert "data-full-bekreft" not in v["attr"] and "overbooking" not in v["skjult"]


# ============================ roller og vern ============================

def test_lesetilgang_ser_bare_statusmerket_og_kan_ikke_endre(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid, "Mari")
    k = _klient(con, "lese")
    html = _liste(k, kid)
    assert _velgere(html) == {} and 'class="statusvelger' not in html and 'name="neste"' not in html
    assert re.search(r'<td class="statuscelle"><span class="merke ok">Påmeldt</span></td>', html)
    r = _ute(k, kid, pid, "avmeldt", neste=_liste_adresse(kid))
    assert r.status_code == 403 and _status(pid) == "paameldt"


def test_kursadmin_kan_endre_og_ikke_innlogget_sendes_til_innlogging(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid, "Mari")
    assert _velgere(_liste(_klient(con, "kursadmin"), kid))
    from kurs.web import app as webapp
    r = _ute(webapp.app.test_client(), kid, pid, "avmeldt", neste=_liste_adresse(kid))
    assert r.status_code == 302 and "/admin/logg-inn" in r.headers["Location"] and _status(pid) == "paameldt"


def test_post_uten_csrf_token_avvises_og_ingenting_endres(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid, "Mari")
    k = _klient(con)
    k.injiser_csrf = False
    r = _ute(k, kid, pid, "avmeldt", neste=_liste_adresse(kid))
    assert r.status_code == 400 and _status(pid) == "paameldt"
    r = _ute(k, kid, pid, "avmeldt", neste=_liste_adresse(kid), csrf_token="feil-token")
    assert r.status_code == 400 and _status(pid) == "paameldt"
    k.get(f"/admin/kurs/{kid}/deltakere")                                 # siden med skjemaene lager sesjonens token
    with k.session_transaction() as s:
        token = s["csrf"]
    assert _ute(k, kid, pid, "avmeldt", neste=_liste_adresse(kid), csrf_token=token).status_code == 302
    assert _status(pid) == "avmeldt"


def test_get_paa_statusruten_er_ikke_tillatt(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid, "Mari")
    r = _klient(con).get(f"/admin/kurs/{kid}/deltaker/{pid}/status?status=avmeldt")
    assert r.status_code == 405 and _status(pid) == "paameldt"


# ============================ alle overganger fra listen ============================

def test_alle_42_overganger_fra_listen_gir_ny_status_tilbake_til_listen_og_melding(con):
    """Hver av de 42 overgangene mellom de syv statusene (ett eget kurs per overgang, ledig plass): riktig status,
    tilbake til listen med søk og filter, melding med navn - og e-post bare for den som FÅR PLASS SOM PÅMELDT. En ekstradeltaker
    får ingen automatisk bekreftelse eller faktura (E5: prisen er ikke kursets standardpris), og har en egen, lengre melding
    (samlinger, tar ikke plass). Tilbake fra Ekstradeltaker til Påmeldt gjelder de vanlige reglene: bekreftelsen som ikke var
    sendt (holdt tilbake), sendes nå."""
    k = _klient(con)
    n = 0
    for fra in STATUSER:
        for til in STATUSER:
            if fra == til:
                continue
            n += 1
            kid = _kurs(con, kapasitet=20, kode=f"OVG{n}")
            pid = _i_status(con, kid, "Kari", fra)
            for_epost = _eposter("Kari")
            liste = _liste_adresse(kid, ("sok", "Kari"), ("status", fra))
            r = k.post(f"/admin/kurs/{kid}/deltaker/{pid}/status", data={"status": til, "neste": liste})
            hva = f"{fra} -> {til}"
            assert r.status_code == 302 and r.headers["Location"] == f"{liste}#rad-{pid}", hva   # tilbake til raden i listen med søk og filter
            assert _status(pid) == til, hva
            html = k.get(liste).get_data(as_text=True)
            melding = f"Kari Test: Status endret til «{NAVN[til].lower()}»."
            if til == "ekstradeltaker":           # egen melding: hvilke samlinger, tar ikke plass, ingenting sendes automatisk
                assert any(m.startswith(melding + " Deltar på hele kurset.") and "Tar ikke plass" in m
                           and "Ingen bekreftelse eller faktura sendes automatisk" in m for m in _flasher(html)), hva
            else:
                assert melding in _flasher(html), hva
            if til == "paameldt":                   # Påmeldt igjen (også fra Ekstradeltaker: holdet oppheves, bekreftelsen var aldri sendt)
                assert "bekreftelse" in _eposter("Kari")[len(for_epost):], hva
            else:                       # avmeldt, avslått, utgått, forlatt, venteliste og Ekstradeltaker: ingen automatisk e-post
                assert _eposter("Kari") == for_epost, hva
    assert n == 42


# ============================ stoppet: riktig melding, samme liste ============================

def test_fullt_kurs_uten_overbooking_avvises_med_melding_og_ingenting_endres(con):
    kid = _kurs(con, kapasitet=1)
    _meld_paa(con, kid, "Ola")
    stig = _meld_paa(con, kid, "Stig")
    k = _klient(con)
    liste = _liste_adresse(kid, ("status", "venteliste"))
    r = _ute(k, kid, stig, "paameldt", neste=liste)
    assert r.headers["Location"] == f"{liste}#rad-{stig}"
    html = k.get(liste).get_data(as_text=True)
    assert "Stig Test: Kurset er fullt – kan ikke melde på flere uten å melde av noen først." in _flasher(html)
    assert 'class="flash feil"' in html and _status(stig) == "venteliste"
    assert "Stig Test" in synlige_rader(html)                                # står fortsatt på ventelisten i filteret


def test_overbooking_fra_listen_med_ja_melder_paa_og_sier_hvor_mange_plasser(con):
    kid = _kurs(con, kapasitet=1)
    _meld_paa(con, kid, "Ola")
    stig = _meld_paa(con, kid, "Stig")
    k = _klient(con)
    r = _ute(k, kid, stig, "paameldt", neste=_liste_adresse(kid), overbooking="1")
    assert r.headers["Location"] == f"{_liste_adresse(kid)}#rad-{stig}" and _status(stig) == "paameldt"
    html = k.get(r.headers["Location"]).get_data(as_text=True)
    assert "Stig Test: Status endret til «påmeldt». Kurset har nå 2 påmeldte deltakere på 1 plass." in _flasher(html)
    assert "bekreftelse" in _eposter("Stig")
    assert _antall("SELECT COUNT(*) FROM hendelse WHERE handling='status_endret' AND detaljer LIKE '%over_kapasitet%'") == 1


def test_avlyst_kurs_avviser_paamelding_fra_listen(con):
    kid = _kurs(con)
    pid = _i_status(con, kid, "Tone", "avmeldt")
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kid,))
    con.commit()
    k = _klient(con)
    r = _ute(k, kid, pid, "paameldt", neste=_liste_adresse(kid))
    assert "Tone Test: Kurset er avlyst – kan ikke melde på flere." in _flasher(k.get(r.headers["Location"]).get_data(as_text=True))
    assert _status(pid) == "avmeldt"


def test_uendret_status_gir_info_og_ingen_hendelse(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid, "Mari")
    k = _klient(con)
    for_hendelser = _antall("SELECT COUNT(*) FROM hendelse")
    r = _ute(k, kid, pid, "paameldt", neste=_liste_adresse(kid))
    html = k.get(r.headers["Location"]).get_data(as_text=True)
    assert "Mari Test: Statusen er allerede «påmeldt». Ingenting er endret." in _flasher(html)
    assert 'class="flash info"' in html and _status(pid) == "paameldt"
    assert _antall("SELECT COUNT(*) FROM hendelse") == for_hendelser


def test_ugyldig_status_avvises_og_ingenting_endres(con):
    kid = _kurs(con)
    k = _klient(con)
    for n, verdi in enumerate(["tull", "", "BEKREFTET", "bekreftet ", "bekreftet", "avslått", "Påmeldt", "Ekstradeltaker"]):
        pid = _i_status(con, kid, f"Mari{n}", "venteliste")
        for_hendelser = _antall("SELECT COUNT(*) FROM hendelse")
        r = _ute(k, kid, pid, verdi, neste=_liste_adresse(kid))
        assert r.headers["Location"] == f"{_liste_adresse(kid)}#rad-{pid}", verdi
        assert f"Mari{n} Test: Ugyldig statusendring." in _flasher(k.get(r.headers["Location"]).get_data(as_text=True)), verdi
        assert _status(pid) == "venteliste" and _antall("SELECT COUNT(*) FROM hendelse") == for_hendelser, verdi


def test_paameldt_til_avmeldt_rykker_opp_fra_venteliste_og_sier_fra(con):
    kid = _kurs(con, kapasitet=1)
    ola = _meld_paa(con, kid, "Ola")
    stig = _meld_paa(con, kid, "Stig")
    k = _klient(con)
    r = _ute(k, kid, ola, "avmeldt", neste=_liste_adresse(kid))
    html = k.get(r.headers["Location"]).get_data(as_text=True)
    flasher = _flasher(html)
    assert "Ola Test: Status endret til «avmeldt»." in flasher
    assert "Første på ventelisten har rykket opp og fått plassen." in flasher
    assert (_status(ola), _status(stig)) == ("avmeldt", "paameldt")
    assert "bekreftelse" in _eposter("Stig") and _eposter("Ola") == []          # den avmeldte får ingen e-post
    assert _velgere(html)[stig]["attr"]["data-naa"] == "paameldt"              # listen viser ny status


def test_endringen_vises_i_listen_chips_og_kort_etterpaa(con):
    kid = _kurs(con)
    mari, marie = _meld_paa(con, kid, "Mari"), _meld_paa(con, kid, "Marie")
    k = _klient(con)
    r = _ute(k, kid, marie, "forlatt", neste=_liste_adresse(kid, ("sok", "Mari")))
    html = k.get(r.headers["Location"]).get_data(as_text=True)
    assert dict(re.findall(r'data-antall="([a-z]+)">\((\d+)\)<', html)) == {
        "paameldt": "1", "ekstradeltaker": "0", "venteliste": "0", "avmeldt": "0", "avslatt": "0", "utgatt": "0", "forlatt": "1"}
    assert '<div class="liten dempet">Forlatt</div><div class="stat">1</div>' in html
    velgere = _velgere(html)
    assert velgere[marie]["attr"]["data-naa"] == "forlatt" and velgere[mari]["attr"]["data-naa"] == "paameldt"
    assert 'value="Mari"' in html                                              # søket står igjen i søkefeltet


# ============================ tilbake til listen: bare til DENNE kursets liste ============================

def test_neste_bygges_paa_nytt_fra_soek_og_gyldige_statuser(con):
    kid = _kurs(con)
    k = _klient(con)
    liste = f"/admin/kurs/{kid}/deltakere"
    tilfeller = [
        (liste, liste),
        (f"{liste}?sok=Mari&status=venteliste&status=avmeldt", f"{liste}?sok=Mari&status=venteliste&status=avmeldt"),
        # statusene kommer i fast rekkefølge; ukjente statuser og andre parametre droppes; tomt søk forsvinner
        (f"{liste}?status=forlatt&status=tull&status=paameldt&sok=&x=1&y[]=2", f"{liste}?status=paameldt&status=forlatt"),
        (f"{liste}?sok=%C3%98yvind%20%C3%85s%40x.no", f"{liste}?sok=%C3%98yvind+%C3%85s@x.no"),
        (f"{liste}?sok=a#oppmote", f"{liste}?sok=a"),
        (f"{liste}?sok={'x' * 500}", f"{liste}?sok={'x' * 200}"),
        (f"{liste}?sok=<script>alert(1)</script>", f"{liste}?sok=%3Cscript%3Ealert(1)%3C/script%3E"),
    ]
    for n, (neste, forventet) in enumerate(tilfeller):
        pid = _meld_paa(con, kid, f"Mari{n}")
        r = _ute(k, kid, pid, "avmeldt", neste=neste)
        assert r.status_code == 302 and r.headers["Location"] == f"{forventet}#rad-{pid}", neste
        assert _status(pid) == "avmeldt"


def test_manipulert_neste_gir_aldri_omdirigering_bort_fra_kurset(con):
    """En ugyldig `neste` behandles som om den manglet: endringen skjer, og admin havner på deltakeren som før."""
    kid = _kurs(con)
    annen = _kurs(con, kode="LIST2")
    k = _klient(con)
    ugyldige = [
        "https://evil.example/admin/kurs/{kid}/deltakere", "http://evil.example", "//evil.example/",
        "//evil.example/admin/kurs/{kid}/deltakere", "/\\evil.example", "\\\\evil.example", "javascript:alert(1)",
        "data:text/html,x", "evil.example/admin/kurs/{kid}/deltakere", "admin/kurs/{kid}/deltakere", "/admin",
        "/admin/kurs/{kid}", "/admin/kurs/{kid}/deltakere/", "/admin/kurs/{kid}/deltakere/x",
        "/admin/kurs/{kid}/deltakere/../deltaker/1", "/admin/kurs/{annen}/deltakere", "/admin/kurs/{annen}/deltakere?sok=x",
        "/admin/kurs/99999/deltakere", "/admin/kurs/{kid}/deltaker/1", "/admin/logg-ut", " /admin/kurs/{kid}/deltakere", "",
        "%2F%2Fevil.example", "/admin/kurs/{kid}/deltakere\r\nLocation: https://evil.example",
    ]
    for n, neste in enumerate(ugyldige):
        pid = _meld_paa(con, kid, f"Mari{n}")
        r = _ute(k, kid, pid, "avmeldt", neste=neste.format(kid=kid, annen=annen))
        assert r.status_code == 302, neste
        assert r.headers["Location"] == f"/admin/kurs/{kid}/deltaker/{pid}", neste
        assert "evil" not in r.headers["Location"] and "\n" not in r.headers["Location"], neste
        assert _status(pid) == "avmeldt", neste


def test_uten_neste_er_ruten_som_for_med_melding_uten_navn(con):
    """Deltakervinduet sender ikke `neste`: samme omdirigering og samme melding som før."""
    kid = _kurs(con)
    pid = _meld_paa(con, kid, "Mari")
    k = _klient(con)
    r = _ute(k, kid, pid, "avmeldt")
    assert r.headers["Location"] == f"/admin/kurs/{kid}/deltaker/{pid}"
    assert "Status endret til «avmeldt»." in _flasher(k.get(r.headers["Location"]).get_data(as_text=True))


def test_annen_kurs_id_eller_paamelding_id_gir_404_og_endrer_ingenting(con):
    kid = _kurs(con)
    annen = _kurs(con, kode="LIST2")
    pid = _meld_paa(con, kid, "Mari")
    k = _klient(con)
    liste = _liste_adresse(kid)
    assert _ute(k, annen, pid, "avmeldt", neste=liste).status_code == 404             # påmeldingen hører til et annet kurs
    assert _ute(k, kid, 99999, "avmeldt", neste=liste).status_code == 404
    assert _ute(k, 99999, pid, "avmeldt", neste=liste).status_code == 404
    assert _status(pid) == "paameldt"


# ============================ knappene som var der fra før ============================

def test_meld_av_og_behandle_valgte_og_live_soket_finnes_fortsatt(con):
    kid = _kurs(con)
    _meld_paa(con, kid, "Mari")
    html = _liste(_klient(con), kid)
    assert "Melde av Mari Test?" in html and ">Meld av</button>" in html
    assert ">Behandle valgte</button>" in html and ">Send e-post til valgte</button>" in html
    assert 'data-deltakerfilter' in html and 'name="sok"' in html and 'data-antall="paameldt"' in html
    assert 'data-filtrer="deltaker" data-status="paameldt" data-sok="mari test mari@eksempel.no"' in html


def test_javascript_og_stil_for_statusvelgeren_er_paa_plass():
    """Uten JavaScript virker skjemaet; app.js skjuler «Endre» til noe er valgt og sender `neste` fra adressen."""
    kilde = Path(__file__).resolve().parent.parent / "kurs" / "web"
    js = (kilde / "static" / "app.js").read_text(encoding="utf-8")
    assert 'form[data-radstatus]' in js and 'input[name=neste]' in js and 'data-naa' in js
    assert "window.location.pathname + window.location.search" in js
    assert 'getAttribute("data-bekreft")' in js                       # dialogteksten per alternativ
    assert "requestSubmit" not in js                                  # ingen automatisk innsending fra statusvelgeren
    assert "#rad-" in js and "lagreValgte" in js and "sessionStorage" in js   # tilbake til raden, og avkrysningene huskes
    css = (kilde / "templates" / "base.html").read_text(encoding="utf-8")
    assert ".radstatus button[hidden] { display:none; }" in css and "select.statusvelger" in css
    assert 'tr[id^="rad-"]:target' in css and "tr.radmelding" in css


# ============================ raden: id, forventet status og tilbake til raden ============================

def test_hver_rad_har_id_og_sender_med_statusen_den_hadde_da_listen_ble_lastet(con):
    kid = _kurs(con, kapasitet=10)
    pid = {s: _i_status(con, kid, s.capitalize(), s) for s in OPPRETTELSE}
    html = _liste(_klient(con), kid)
    velgere = _velgere(html)
    for status, p in pid.items():
        assert velgere[p]["skjult"]["forventet"] == status                       # utgangspunktet for dialogteksten
        assert re.search(rf'<tr data-filtrer="deltaker" data-status="{status}" data-sok="[^"]*"[^>]* id="rad-{p}">', html)   # målet for «tilbake til raden»


def test_fra_listen_gaar_admin_tilbake_til_raden_ogsaa_ved_avvisning(con):
    """Siden lastes på nytt etter endringen: adressen slutter på #rad-<id>, så nettleseren hopper til raden i stedet for
    til toppen (static/app.js legger meldingene over raden). Også når endringen avvises."""
    kid = _kurs(con, kapasitet=1)
    _meld_paa(con, kid, "Ola")
    stig = _meld_paa(con, kid, "Stig")
    k = _klient(con)
    liste = _liste_adresse(kid, ("sok", "Stig"))
    assert _ute(k, kid, stig, "paameldt", neste=liste).headers["Location"] == f"{liste}#rad-{stig}"      # avvist: fullt
    assert _ute(k, kid, stig, "tull", neste=liste).headers["Location"] == f"{liste}#rad-{stig}"           # avvist: ugyldig
    assert _ute(k, kid, stig, "avmeldt", neste=liste).headers["Location"] == f"{liste}#rad-{stig}"        # utført
    # deltakervinduet (uten neste) går til deltakeren, uten anker
    assert _ute(k, kid, stig, "venteliste").headers["Location"] == f"/admin/kurs/{kid}/deltaker/{stig}"


# ============================ foreldet liste: dialogen beskrev noe annet ============================

def _dialog(velger, ny_status) -> str:
    return next(a["attr"]["data-bekreft"] for a in velger["valg"] if a["attr"]["value"] == ny_status)


def test_foreldet_liste_gjor_ingenting_og_sier_fra(con):
    """Fane A viser Kari på venteliste og dialogen «Deltakeren tas av ventelisten. Ingen e-post». Før A sender, melder en
    annen Ola av, og Kari rykker opp. A sender så: da skal ingenting skje (ellers ville Kari blitt avmeldt, plassen ledig, og
    Vera fått plassen med bekreftelse og faktura, uten at dialogen sa det)."""
    kid = _kurs(con, kapasitet=1)
    ola, kari, vera = _meld_paa(con, kid, "Ola"), _meld_paa(con, kid, "Kari"), _meld_paa(con, kid, "Vera")
    k = _klient(con)
    liste = _liste_adresse(kid)
    v = _velgere(_liste(k, kid))[kari]
    assert v["skjult"]["forventet"] == "venteliste" and "tas av ventelisten" in _dialog(v, "avmeldt")
    _ute(k, kid, ola, "avmeldt", neste=liste, forventet="paameldt")             # fane B: Kari rykker opp
    assert (_status(kari), _status(vera)) == ("paameldt", "venteliste")
    for_eposter, for_fakturaer = _eposter("Vera"), _antall("SELECT COUNT(*) FROM faktura")
    for_hendelser = _antall("SELECT COUNT(*) FROM hendelse")

    r = _ute(k, kid, kari, "avmeldt", neste=liste, forventet=v["skjult"]["forventet"])    # fane A sender det gamle valget
    assert r.headers["Location"] == f"{liste}#rad-{kari}"
    html = k.get(liste).get_data(as_text=True)
    assert ("Kari Test: Statusen er endret siden listen ble lastet (nå «påmeldt»). Ingenting er endret. "
            "Kontroller statusen og prøv på nytt.") in _flasher(html)
    assert 'class="flash feil"' in html
    assert (_status(kari), _status(vera)) == ("paameldt", "venteliste")           # ingenting endret
    assert _eposter("Vera") == for_eposter and _antall("SELECT COUNT(*) FROM faktura") == for_fakturaer
    assert _antall("SELECT COUNT(*) FROM hendelse") == for_hendelser

    # Lastes listen på nytt, stemmer dialogen igjen - og da gjøres endringen (med opprykk som dialogen nå sier)
    v = _velgere(html)[kari]
    assert v["skjult"]["forventet"] == "paameldt" and "rykker den første opp" in _dialog(v, "avmeldt")
    _ute(k, kid, kari, "avmeldt", neste=liste, forventet=v["skjult"]["forventet"])
    assert (_status(kari), _status(vera)) == ("avmeldt", "paameldt")
    assert "bekreftelse" in _eposter("Vera")


def test_ukjent_eller_tom_forventet_status(con):
    """Uten `forventet` (deltakervinduet, eldre skjema) virker ruten som før. Er den satt til noe annet enn statusen
    påmeldingen har, gjøres ingenting - også for en verdi som ikke er en status."""
    kid = _kurs(con)
    k = _klient(con)
    liste = _liste_adresse(kid)
    pid = _meld_paa(con, kid, "Mari")
    for forventet in ("tull", "venteliste", "BEKREFTET", "bekreftet", "ekstradeltaker", "avmeldt"):
        r = _ute(k, kid, pid, "avmeldt", neste=liste, forventet=forventet)
        assert "Statusen er endret siden listen ble lastet" in " ".join(_flasher(k.get(r.headers["Location"]).get_data(as_text=True))), forventet
        assert _status(pid) == "paameldt", forventet
    _ute(k, kid, pid, "venteliste", neste=liste, forventet="")                       # tom = ingen kontroll
    assert _status(pid) == "venteliste"
    _ute(k, kid, pid, "avmeldt")                                                     # deltakervinduet: ingen kontroll
    assert _status(pid) == "avmeldt"


# ============================ dialogen sier det som faktisk skjer: overbooket, avlyst og avsluttet kurs ============================

def test_dialogen_lover_ikke_opprykk_naar_kurset_er_overbooket(con):
    """Etter overbooking (3 av 2) blir ikke plassen ledig når en går: ingen rykker opp (db._rykk_opp). Dialogen sier det."""
    kid = _kurs(con, kapasitet=2)
    ada, bo, vi, wu = (_meld_paa(con, kid, n) for n in ("Ada", "Bo", "Vi", "Wu"))         # Vi og Wu på venteliste
    db.sett_paamelding_status(con, vi, "paameldt", aktor=ADMIN, tillat_overbooking=True)  # 3 av 2
    con.commit()
    k = _klient(con)
    velgere = _velgere(_liste(k, kid))
    for ny in ("avmeldt", "avslatt", "utgatt", "forlatt", "venteliste"):
        t = _dialog(velgere[ada], ny)
        assert "Kurset er overbooket (3 påmeldte på 2 plasser), så plassen blir ikke ledig og ingen rykker opp fra ventelisten." in t, ny
        assert "Plassen blir ledig" not in t and "rykker den første opp" not in t, ny
    r = _ute(k, kid, ada, "avmeldt", neste=_liste_adresse(kid), forventet="paameldt")
    assert (_status(ada), _status(bo), _status(vi), _status(wu)) == ("avmeldt", "paameldt", "paameldt", "venteliste")
    assert "Første på ventelisten har rykket opp" not in " ".join(_flasher(k.get(r.headers["Location"]).get_data(as_text=True)))
    # Bo går også (nå 2 av 2 -> 1 av 2): da blir plassen ledig og Wu rykker opp, slik dialogen sier
    velgere = _velgere(_liste(k, kid))
    assert "Plassen blir ledig" in _dialog(velgere[bo], "avmeldt") and "overbooket" not in _dialog(velgere[bo], "avmeldt")
    _ute(k, kid, bo, "avmeldt", neste=_liste_adresse(kid), forventet="paameldt")
    assert (_status(bo), _status(vi), _status(wu)) == ("avmeldt", "paameldt", "paameldt")


@pytest.mark.parametrize("kursstatus", ["avlyst", "avsluttet"])
def test_avlyst_og_avsluttet_kurs_rykker_ingen_opp_og_tar_ikke_imot_nye_fra_listen(con, kursstatus):
    """Listen kan endre status også der «Meld av» er skjult (avsluttet). Et kurs som ikke holdes eller er ferdig, får aldri
    nye påmeldte: ingen rykker opp fra ventelisten, ingen får bekreftelse eller faktura, og «Påmeldt» avvises."""
    kid = _kurs(con, kapasitet=1)
    ole, lise, vera = (_meld_paa(con, kid, n) for n in ("Ole", "Lise", "Vera"))
    con.execute("UPDATE kurs SET status=? WHERE id=?", (kursstatus, kid))
    con.commit()
    k = _klient(con)
    liste = _liste_adresse(kid)
    html = _liste(k, kid)
    velgere = _velgere(html)
    # dialogene sier det som skjer
    t = _dialog(velgere[ole], "avmeldt")
    assert f"Kurset er {kursstatus}, så ingen rykker opp fra ventelisten." in t and "Plassen blir ledig" not in t
    t = _dialog(velgere[lise], "paameldt")
    assert f"Kurset er {kursstatus}, så deltakeren kan ikke settes til påmeldt." in t and "bekreftelse" not in t
    assert all("data-full-bekreft" not in v["attr"] and "overbooking" not in v["skjult"] for v in velgere.values())
    # Ole ut: Lise blir stående på ventelisten
    for_fakturaer = _antall("SELECT COUNT(*) FROM faktura")
    r = _ute(k, kid, ole, "avmeldt", neste=liste, forventet="paameldt")
    flasher = _flasher(k.get(r.headers["Location"]).get_data(as_text=True))
    assert "Ole Test: Status endret til «avmeldt»." in flasher
    assert not any("rykket opp" in f for f in flasher)
    assert (_status(ole), _status(lise), _status(vera)) == ("avmeldt", "venteliste", "venteliste")
    # Venteliste -> Påmeldt (også med overbooking=1) avvises: hverken bekreftelse eller faktura
    for pid, fornavn in ((lise, "Lise"), (vera, "Vera"), (ole, "Ole")):
        r = _ute(k, kid, pid, "paameldt", neste=liste, overbooking="1")
        assert f"{fornavn} Test: Kurset er {kursstatus} – kan ikke melde på flere." in \
            _flasher(k.get(r.headers["Location"]).get_data(as_text=True)), fornavn
    assert (_status(ole), _status(lise), _status(vera)) == ("avmeldt", "venteliste", "venteliste")
    assert all(_eposter(n) == [] for n in ("Ole", "Lise", "Vera")) and _antall("SELECT COUNT(*) FROM faktura") == for_fakturaer


@pytest.mark.parametrize("kursstatus", ["avlyst", "avsluttet"])
def test_meld_av_og_avslag_rykker_heller_ikke_opp_paa_avlyst_eller_avsluttet_kurs(con, kursstatus):
    """Samme db-regel (db._rykk_opp) gjelder «Meld av» i listen og «Avslå påmeldingen» i deltakervinduet."""
    kid, kid2 = _kurs(con, kapasitet=1), _kurs(con, kapasitet=1, kode="LUKK2")
    ole, lise = _meld_paa(con, kid, "Ole"), _meld_paa(con, kid, "Lise")
    dag, eva = _meld_paa(con, kid2, "Dag"), _meld_paa(con, kid2, "Eva")
    con.execute("UPDATE kurs SET status=? WHERE id IN (?, ?)", (kursstatus, kid, kid2))
    con.commit()
    k = _klient(con)
    r = k.post(f"/admin/paamelding/{ole}/meld-av", follow_redirects=True)
    assert _flasher(r.get_data(as_text=True)) == ["Avmeldt."]                    # ikke «Første på ventelisten har fått plassen»
    assert (_status(ole), _status(lise)) == ("avmeldt", "venteliste")
    assert db.avsla_paamelding(con, dag) is None
    con.commit()
    assert (_status(dag), _status(eva)) == ("avslatt", "venteliste")
    assert _eposter("Lise") == [] and _eposter("Eva") == []


def test_aapent_kurs_er_uendret_dialog_og_opprykk_som_for(con):
    """Kontroll: en åpen kurs med nøyaktig full kapasitet - plassen blir ledig, og første på ventelisten rykker opp."""
    kid = _kurs(con, kapasitet=1)
    ola, stig = _meld_paa(con, kid, "Ola"), _meld_paa(con, kid, "Stig")
    k = _klient(con)
    t = _dialog(_velgere(_liste(k, kid))[ola], "avmeldt")
    assert "Plassen blir ledig. Står noen på ventelisten, rykker den første opp og får plassen (med bekreftelse på e-post)." in t
    _ute(k, kid, ola, "avmeldt", neste=_liste_adresse(kid), forventet="paameldt")
    assert (_status(ola), _status(stig)) == ("avmeldt", "paameldt")


def test_db_avsluttet_kurs_avviser_paamelding_men_tillater_ovrige_statuser(con):
    """Regelen ligger i db, ikke bare i listen: også deltakervinduet (og alt annet som kaller db) er sperret. Andre
    statusendringer (Utgått, Forlatt ...) er fortsatt mulig på et avsluttet kurs."""
    kid = _kurs(con, kapasitet=5)
    pid = _i_status(con, kid, "Tone", "avmeldt")
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()
    with pytest.raises(db.Paameldingsfeil, match="Kurset er avsluttet – kan ikke melde på flere."):
        db.sett_paamelding_status(con, pid, "paameldt", aktor=ADMIN)
    for status in ("forlatt", "utgatt", "avslatt", "venteliste", "avmeldt"):
        db.sett_paamelding_status(con, pid, status, aktor=ADMIN)
        assert db.paameldingsstatus(con.execute("SELECT * FROM paamelding WHERE id=?", (pid,)).fetchone()) == status
