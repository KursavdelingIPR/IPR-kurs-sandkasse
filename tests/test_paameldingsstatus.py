"""Påmeldingsstatus: syv statuser admin kan sette manuelt - Påmeldt, Ekstradeltaker, Venteliste, Avmeldt, Avslått, Utgått
og Forlatt.

Avslått, Utgått og Forlatt er avmeldte påmeldinger med hver sin dato (migrering 6 og 8), og høyst én av dem kan være
satt - både koden og CHECK-reglene i databasen passer på det. Ekstradeltaker er en påmelding med plass (status
«bekreftet») og ekstradeltaker_ts (migrering 16). Plassen og ventelisten følger dagens regler. Selve statusendringen
sender aldri e-post: bare den som får plass eller rykker opp fra ventelisten, behandles (bekreftelse og faktura) som før.
Ekstradeltaker har egne tester i tests/test_ekstradeltaker.py.
"""
import sqlite3
from datetime import date

import pytest

from kurs import config, db, hendelseslogg, migreringer, sveiper
from kurs.kjoring import Kjoring

ADMIN = f"admin:{config.ADMIN_BRUKERNAVN}"
STATUSER = list(db.PAAMELDINGSSTATUSER)
AVSLUTTET = ["avmeldt", "avslatt", "utgatt", "forlatt"]


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _kurs(con, kapasitet=2, kode="STAT1") -> int:
    kid = db.opprett_kurs(con, kode=kode, navn="Veiledning i gruppe", datoer=[date(2031, 10, 16).isoformat()],
                          sharepoint_mappe=f"Kurs/{kode}", pris_nok=2500, kapasitet=kapasitet)
    con.commit()
    return kid


def _meld_paa(con, kid, fornavn) -> int:
    pid, _ = db.meld_paa(con, kid, epost=f"{fornavn.lower()}@eksempel.no", fornavn=fornavn, etternavn="Test")
    con.commit()
    return pid


def _rad(con, pid):
    return db.koble(config.DB_STI).execute("SELECT * FROM paamelding WHERE id=?", (pid,)).fetchone()


def _status(con, pid) -> str:
    return db.paameldingsstatus(_rad(con, pid))


def _datoer(con, pid) -> list:
    rad = _rad(con, pid)
    return [rad["avslatt_ts"], rad["utgatt_ts"], rad["forlatt_ts"]]


def _eposter(con, fornavn) -> list[str]:
    return [r[0] for r in db.koble(config.DB_STI).execute(
        "SELECT type FROM utsending_logg WHERE mottaker=? ORDER BY sendt_ts", (f"{fornavn.lower()}@eksempel.no",))]


def _admin():
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    assert k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN,
                                            "passord": config.ADMIN_PASSORD}).status_code == 302
    return k


def _sett(k, kid, pid, status, **ekstra):
    return k.post(f"/admin/kurs/{kid}/deltaker/{pid}/status", data={"status": status, **ekstra}, follow_redirects=True)


def _i_status(con, kid, fornavn, status) -> int:
    """En påmelding (med ledig plass) som har fått `status` av admin."""
    pid = _meld_paa(con, kid, fornavn)
    if status != "paameldt":
        db.sett_paamelding_status(con, pid, status, aktor=ADMIN)
        con.commit()
    return pid


# ============================ databasen ============================

def test_ny_database_har_kolonnene_og_migrering_8_legger_dem_til_i_en_gammel(con):
    assert db.har_kolonne(con, "paamelding", "utgatt_ts") and db.har_kolonne(con, "paamelding", "forlatt_ts")
    kid = _kurs(con)
    pid = _meld_paa(con, kid, "Kari")
    for kolonne in ("forlatt_ts", "utgatt_ts"):
        con.execute(f"ALTER TABLE paamelding DROP COLUMN {kolonne}")
    con.execute("DELETE FROM schema_versjon WHERE versjon >= 8")
    con.commit()
    assert migreringer.kjor_manglende(con) == list(range(8, migreringer.KODEVERSJON + 1))      # 9 og senere kjøres også (endrer ingenting her)
    rad = con.execute("SELECT status, utgatt_ts, forlatt_ts FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert (rad["status"], rad["utgatt_ts"], rad["forlatt_ts"]) == ("bekreftet", None, None)
    assert migreringer.kjor_manglende(con) == []                                   # kan kjøres flere ganger


@pytest.mark.parametrize("sql", [
    "UPDATE paamelding SET avslatt_ts='x', utgatt_ts='x' WHERE id=?",
    "UPDATE paamelding SET avslatt_ts='x', forlatt_ts='x' WHERE id=?",
    "UPDATE paamelding SET utgatt_ts='x', forlatt_ts='x' WHERE id=?",
])
def test_databasen_stopper_to_av_avslatt_utgatt_og_forlatt_samtidig(con, sql):
    kid = _kurs(con)
    pid = _i_status(con, kid, "Kari", "avmeldt")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(sql, (pid,))


@pytest.mark.parametrize("kolonne", ["utgatt_ts", "forlatt_ts"])
def test_databasen_tillater_utgatt_og_forlatt_bare_paa_avmeldte(con, kolonne):
    kid = _kurs(con)
    pid = _meld_paa(con, kid, "Kari")                                              # bekreftet
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(f"UPDATE paamelding SET {kolonne}='x' WHERE id=?", (pid,))


# ============================ alle overganger ============================

@pytest.mark.parametrize("fra, til", [(a, b) for a in STATUSER for b in STATUSER if a != b])
def test_alle_overganger_gir_riktig_status_hoyst_en_dato_og_logges(con, fra, til):
    kid = _kurs(con, kapasitet=5)
    pid = _i_status(con, kid, "Kari", fra)
    db.sett_paamelding_status(con, pid, til, aktor=ADMIN)
    con.commit()
    assert _status(con, pid) == til
    assert sum(1 for d in _datoer(con, pid) if d) == (1 if til in ("avslatt", "utgatt", "forlatt") else 0)
    assert bool(_rad(con, pid)["ekstradeltaker_ts"]) == (til == "ekstradeltaker")     # bare Ekstradeltaker har tidsstempelet
    siste = con.execute("SELECT detaljer FROM hendelse WHERE handling='status_endret' ORDER BY id DESC").fetchone()[0]
    assert f'"fra": "{fra}", "til": "{til}"' in siste


def test_ukjent_status_avvises(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid, "Kari")
    with pytest.raises(db.Paameldingsfeil, match="Ugyldig"):
        db.sett_paamelding_status(con, pid, "slettet", aktor=ADMIN)


# ============================ plassen og ventelisten ============================

@pytest.mark.parametrize("til", AVSLUTTET)
def test_bekreftet_til_avsluttet_gir_plassen_til_forste_paa_ventelisten(con, til):
    kid = _kurs(con, kapasitet=1)
    kari, nina, ola = (_meld_paa(con, kid, n) for n in ("Kari", "Nina", "Ola"))
    assert db.sett_paamelding_status(con, kari, til, aktor=ADMIN) == nina
    con.commit()
    assert (_status(con, kari), _status(con, nina), _status(con, ola)) == (til, "paameldt", "venteliste")


@pytest.mark.parametrize("til", AVSLUTTET)
def test_venteliste_til_avsluttet_flytter_ingen(con, til):
    kid = _kurs(con, kapasitet=1)
    _meld_paa(con, kid, "Kari")
    nina, ola = _meld_paa(con, kid, "Nina"), _meld_paa(con, kid, "Ola")
    assert db.sett_paamelding_status(con, nina, til, aktor=ADMIN) is None
    con.commit()
    assert _status(con, ola) == "venteliste"


def test_bekreftet_til_venteliste_rykker_ikke_rett_opp_igjen(con):
    """Kari er først i køen (meldte seg på først), men velges ikke i samme operasjon - Nina får plassen."""
    kid = _kurs(con, kapasitet=1)
    kari, nina = _meld_paa(con, kid, "Kari"), _meld_paa(con, kid, "Nina")
    assert db.sett_paamelding_status(con, kari, "venteliste", aktor=ADMIN) == nina
    con.commit()
    assert (_status(con, kari), _status(con, nina)) == ("venteliste", "paameldt")


def test_bekreftet_til_venteliste_uten_andre_paa_ventelisten_blir_staaende(con):
    kid = _kurs(con, kapasitet=1)
    kari = _meld_paa(con, kid, "Kari")
    assert db.sett_paamelding_status(con, kari, "venteliste", aktor=ADMIN) is None
    con.commit()
    assert _status(con, kari) == "venteliste"


@pytest.mark.parametrize("til", ["venteliste", *AVSLUTTET])
def test_etter_overbooking_rykker_ingen_opp(con, til):
    kid = _kurs(con, kapasitet=1)
    kari, nina, ola = (_meld_paa(con, kid, n) for n in ("Kari", "Nina", "Ola"))
    db.sett_paamelding_status(con, nina, "paameldt", aktor=ADMIN, tillat_overbooking=True)   # 2 på 1 plass
    assert db.sett_paamelding_status(con, kari, til, aktor=ADMIN) is None
    con.commit()
    assert _status(con, ola) == "venteliste"


# ============================ e-post og faktura (ruten i deltakervinduet) ============================

@pytest.mark.parametrize("til", AVSLUTTET)
def test_ingen_epost_til_den_som_endres_men_vanlig_bekreftelse_til_den_som_rykker_opp(con, til):
    kid = _kurs(con, kapasitet=1)
    kari, nina = _meld_paa(con, kid, "Kari"), _meld_paa(con, kid, "Nina")
    sveiper.kjor(Kjoring(con, idag=date.today()))                                  # vanlig behandling ved påmelding
    con.commit()
    assert (_eposter(con, "Kari"), _eposter(con, "Nina")) == (["bekreftelse"], ["venteliste"])
    html = _sett(_admin(), kid, kari, til).get_data(as_text=True)
    assert f"Status endret til «{db.PAAMELDINGSSTATUSER[til].lower()}»." in html
    assert "Første på ventelisten har rykket opp og fått plassen." in html
    assert _eposter(con, "Kari") == ["bekreftelse"]                                 # ingen ny e-post til Kari
    assert _eposter(con, "Nina") == ["venteliste", "bekreftelse"]                   # Nina: bekreftelse som i dag
    assert _rad(con, nina)["sveiper_kjort"] == 1


def test_avslatt_i_nedtrekkslisten_sender_ikke_avslag(con):
    kid = _kurs(con)
    kari = _meld_paa(con, kid, "Kari")
    _sett(_admin(), kid, kari, "avslatt")
    assert _status(con, kari) == "avslatt" and "avslag" not in _eposter(con, "Kari")


def test_til_bekreftet_fra_utgatt_behandles_som_i_dag(con):
    kid = _kurs(con)
    una = _i_status(con, kid, "Una", "utgatt")
    _sett(_admin(), kid, una, "paameldt")
    assert _status(con, una) == "paameldt" and _datoer(con, una) == [None, None, None]
    assert _eposter(con, "Una") == ["bekreftelse"]


def test_til_venteliste_gir_ventelistebeskjed_forst_ved_morgenjobben(con):
    kid = _kurs(con)
    finn = _i_status(con, kid, "Finn", "forlatt")
    _sett(_admin(), kid, finn, "venteliste")
    assert _status(con, finn) == "venteliste" and _eposter(con, "Finn") == []       # ikke med en gang
    c = db.koble(config.DB_STI)
    sveiper.kjor(Kjoring(c, idag=date.today()))                                     # morgenjobben
    c.commit()
    assert _eposter(con, "Finn") == ["venteliste"]


def test_fakturert_deltaker_faar_beskjed_om_kreditering(con):
    kid = _kurs(con)
    kari = _meld_paa(con, kid, "Kari")
    con.execute("INSERT INTO faktura (paamelding_id, faktura_nr, belop_nok, status) VALUES (?, 'D1', 2500, 'sendt')",
                (kari,))
    con.commit()
    html = _sett(_admin(), kid, kari, "forlatt").get_data(as_text=True)
    assert "Fakturaen krediteres ikke automatisk – det må gjøres i Visma." in html


# ============================ påmelding på nytt ============================

@pytest.mark.parametrize("status", ["utgatt", "forlatt", "avmeldt"])
def test_utgatt_og_forlatt_kan_melde_seg_paa_igjen_og_historikken_beholdes(con, status):
    kid = _kurs(con)
    pid = _i_status(con, kid, "Kari", status)
    assert _meld_paa(con, kid, "Kari") == pid                                       # samme påmelding tas i bruk igjen
    assert _status(con, pid) == "paameldt" and _datoer(con, pid) == [None, None, None]
    rader = hendelseslogg.for_paamelding(con, _rad(con, pid), systemadmin=False).rader
    assert [r.hva for r in rader][:2] == ["Påmeldt på nytt",
                                         {"utgatt": "Satt til utgått", "forlatt": "Satt til forlatt",
                                          "avmeldt": "Avmeldt"}[status]]


def test_avslatt_kan_fortsatt_ikke_melde_seg_paa_selv(con):
    kid = _kurs(con)
    _i_status(con, kid, "Kari", "avslatt")
    with pytest.raises(db.Paameldingsfeil, match="ikke godkjent"):
        _meld_paa(con, kid, "Kari")


# ============================ deltakervinduet og Logger ============================

@pytest.mark.parametrize("status", STATUSER)
def test_velg_ny_status_viser_alle_andre_statuser_med_norske_navn(con, status):
    kid = _kurs(con, kapasitet=5)
    pid = _i_status(con, kid, "Kari", status)
    html = _admin().get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
    valg = html.split('id="f-ny-status"')[1].split("</select>")[0]
    for s, navn in db.PAAMELDINGSSTATUSER.items():
        assert (f'<option value="{s}">{navn}</option>' in valg) == (s != status)


def test_deltakervinduet_forklarer_utgatt_og_forlatt(con):
    kid = _kurs(con)
    k = _admin()
    una, finn = _i_status(con, kid, "Una", "utgatt"), _i_status(con, kid, "Finn", "forlatt")
    assert "påmeldingen ble ikke fullført" in k.get(f"/admin/kurs/{kid}/deltaker/{una}").get_data(as_text=True)
    assert "deltakelsen er avsluttet administrativt" in k.get(f"/admin/kurs/{kid}/deltaker/{finn}").get_data(as_text=True)


@pytest.mark.parametrize("til, overskrift", [("venteliste", "Satt på venteliste"), ("avmeldt", "Avmeldt"),
                                             ("avslatt", "Avslått"), ("utgatt", "Satt til utgått"),
                                             ("forlatt", "Satt til forlatt")])
def test_logger_viser_status_foer_og_ny_status_i_en_rad(con, til, overskrift):
    kid = _kurs(con)
    kari = _meld_paa(con, kid, "Kari")
    _sett(_admin(), kid, kari, til)
    rader = hendelseslogg.for_paamelding(con, _rad(con, kari), systemadmin=False).rader
    assert [(r.hva, r.detaljer) for r in rader[:1]] == [
        (overskrift, ["Status før: Påmeldt", f"Ny status: {db.PAAMELDINGSSTATUSER[til]}"])]
    assert [r.hva for r in rader[1:]] == ["Påmeldt"]                    # avmeldingen er samme handling


# ============================ lister, rapporter og eksport ============================

def _kurs_med_alle_statuser(con) -> int:
    """Én påmelding med hver status (unntatt Ekstradeltaker, som har egne tester). Venteliste settes sist: ellers rykker
    Vera opp når en plass blir ledig."""
    kid = _kurs(con, kapasitet=10)
    for navn, status in [("Arne", "avmeldt"), ("Siri", "avslatt"), ("Una", "utgatt"), ("Finn", "forlatt"),
                         ("Bente", "paameldt"), ("Vera", "venteliste")]:
        _i_status(con, kid, navn, status)
    return kid


def test_kursrapporten_teller_bare_frivillige_avmeldinger_som_avmeldt(con):
    kid = _kurs_med_alle_statuser(con)
    tekst = _admin().get("/admin/rapporter/kurs.csv").get_data(as_text=True).lstrip("﻿").splitlines()
    hode = tekst[0].split(";")
    rad = dict(zip(hode, next(r for r in tekst[1:] if "Veiledning i gruppe" in r).split(";")))
    assert hode[9:15] == ["Ekstradeltaker", "Venteliste", "Avmeldt", "Avslått", "Utgått", "Forlatt"]
    assert (rad["Påmeldt"], rad["Venteliste"], rad["Avmeldt"], rad["Avslått"], rad["Utgått"], rad["Forlatt"]) == \
        ("1", "1", "1", "1", "1", "1")
    html = _admin().get("/admin/rapporter/kurs").get_data(as_text=True)
    assert '<th scope="col">Utgått</th><th scope="col">Forlatt</th>' in html


def test_eksport_og_utskriftslisten_viser_de_nye_statusene(con):
    kid = _kurs_med_alle_statuser(con)
    k = _admin()
    csv = k.get(f"/admin/kurs/{kid}/deltakere.csv").get_data(as_text=True)
    for fornavn, status in [("Una", "Utgått"), ("Finn", "Forlatt"), ("Siri", "Avslått"), ("Arne", "Avmeldt")]:   # navnet som vises
        assert f"{fornavn};Test;{fornavn.lower()}@eksempel.no;;;{status}" in csv
    liste = k.get(f"/admin/kurs/{kid}/deltakerliste?valgt=1&kol=status&status=utgatt").get_data(as_text=True)
    assert "Una Test" in liste and "Finn Test" not in liste and "Utgått" in liste
    assert '<option value="forlatt"' in k.get(f"/admin/kurs/{kid}/deltakerliste").get_data(as_text=True)


def test_tallene_og_merkene_i_deltakerlisten(con):
    kid = _kurs_med_alle_statuser(con)
    html = _admin().get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    for navn in ("Utgått", "Forlatt", "Avslått"):
        assert f'<div class="liten dempet">{navn}</div><div class="stat">1</div>' in html
    # statusen står som valgt alternativ i radens statusvelger, i samme farge som statusmerket før
    assert '<option value="utgatt" selected>Utgått</option>' in html and '<option value="forlatt" selected>Forlatt</option>' in html
    assert 'class="statusvelger gra"' in html and 'class="statusvelger feil"' in html


def test_utgatt_og_forlatt_kan_anonymiseres(con):
    kid = _kurs(con)
    for navn, status in [("Una", "utgatt"), ("Finn", "forlatt")]:
        pid = _i_status(con, kid, navn, status)
        db.anonymiser_deltaker(con, _rad(con, pid)["deltaker_id"], aktor=ADMIN)       # ikke aktive påmeldinger
