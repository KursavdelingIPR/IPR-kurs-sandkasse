"""Fase 9, trinn 4: samlet sluttkontroll og integrasjonstest.

Ingen nye funksjoner - dette er en ende-til-ende-kontroll av flyten bygget i trinn 1-3
(tests/test_manuell_paamelding.py, tests/test_manuell_registrering_admin.py,
tests/test_manuell_behandling_admin.py), via de faktiske HTTP-rutene, pluss en generell
personvernsveip av HELE hendelsesloggen etter en sammensatt scenarie-kjoring.
"""
from datetime import date, timedelta

import pytest

from adressehjelp import ADRESSE, SPORBAR_ADRESSE
from kurs import config, db
from navnehjelp import personskjema

NY ="/admin/kurs/{kid}/deltaker/ny"
BEHANDLE = "/admin/kurs/{kid}/deltaker/{pid}/behandle"
PROFIL = "/admin/kurs/{kid}/deltaker/{pid}"
DELTAKERE = "/admin/kurs/{kid}/deltakere"


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
    return personskjema({"navn": "Kari Nordmann", "epost": "kari@x.no", "telefon": "99999999",
                         "yrkestittel": "Psykolog", "arbeidssted": "Klinikk AS", "hpr_nr": "1234567",
                         "betaler": "person", **ADRESSE, **over})


def _hent_pid(con, epost="kari@x.no"):
    return con.execute(
        "SELECT p.id FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE d.epost=?", (epost,)
    ).fetchone()[0]


# ==================== punkt 1-9: hovedflyten, bekreftet deltaker ====================

def test_full_flyt_bekreftet_deltaker(con):
    kid = _kurs(con, kapasitet=5, pris_nok=1000, fakturering="person")
    con.commit()
    klient = _klient()
    _logg_inn(klient)

    # 1. Deltakere-fanen har knappen til skjemaet
    liste = klient.get(DELTAKERE.format(kid=kid)).get_data(as_text=True)
    assert NY.format(kid=kid) in liste

    # 2. Registrerer ny deltaker manuelt
    resp = klient.post(NY.format(kid=kid), data=_skjema())
    assert resp.status_code == 302
    pid = _hent_pid(con)

    # 3. kilde/aktor/sveiper_utsatt/ingen automatikk
    rad = con.execute(
        "SELECT kilde, status, sveiper_kjort, sveiper_utsatt FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert rad["kilde"] == "admin"
    assert rad["status"] == "bekreftet"
    assert rad["sveiper_kjort"] == 0
    assert rad["sveiper_utsatt"] == 1
    logg_rad = con.execute(
        "SELECT aktor FROM hendelse WHERE handling='paamelding' ORDER BY id DESC LIMIT 1").fetchone()
    assert logg_rad["aktor"].startswith("admin:")
    assert _antall_mail(con) == 0
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 0

    # 4. Redirect til deltakerprofilen
    assert resp.headers["Location"] == PROFIL.format(kid=kid, pid=pid)

    # 5. "Behandling holdt tilbake"-kortet vises
    profil = klient.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Behandling holdt tilbake" in profil
    assert BEHANDLE.format(kid=kid, pid=pid) in profil

    # 6-7. Admin behandler eksplisitt -> bekreftelse + fakturering
    behandle_resp = klient.post(BEHANDLE.format(kid=kid, pid=pid))
    assert behandle_resp.status_code == 302
    rad = con.execute(
        "SELECT sveiper_kjort, sveiper_utsatt FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert rad["sveiper_kjort"] == 1
    assert rad["sveiper_utsatt"] == 0
    assert _antall_mail(con) == 1
    assert con.execute("SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", (pid,)).fetchone()[0] == 1

    # 9. Dobbel behandling gir ikke dobbel e-post/faktura
    klient.post(BEHANDLE.format(kid=kid, pid=pid))
    assert _antall_mail(con) == 1
    assert con.execute("SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", (pid,)).fetchone()[0] == 1


# ==================== punkt 8: venteliste ====================

def test_full_flyt_venteliste_deltaker(con):
    kid = _kurs(con, kapasitet=1, pris_nok=1000, fakturering="person")
    db.meld_paa(con, kid, epost="forst@x.no", fornavn="Forst", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)

    klient.post(NY.format(kid=kid), data=_skjema())
    pid = _hent_pid(con)
    rad = con.execute("SELECT status FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert rad["status"] == "venteliste"

    profil = klient.get(PROFIL.format(kid=kid, pid=pid)).get_data(as_text=True)
    assert "Send ventelistebeskjed nå" in profil

    klient.post(BEHANDLE.format(kid=kid, pid=pid))
    rad = con.execute("SELECT sveiper_kjort, sveiper_utsatt, status FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert rad["status"] == "venteliste"
    assert rad["sveiper_kjort"] == 0  # ventelistelogikken setter aldri denne
    assert rad["sveiper_utsatt"] == 0
    assert con.execute(
        "SELECT 1 FROM utsending_logg WHERE mottaker='kari@x.no' AND type='venteliste'").fetchone()
    assert con.execute("SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", (pid,)).fetchone()[0] == 0

    # 9. Dobbel behandling gir ikke ny e-post
    forste_antall = _antall_mail(con)
    klient.post(BEHANDLE.format(kid=kid, pid=pid))
    assert _antall_mail(con) == forste_antall


# ==================== punkt 10-11: kursstatus-sperrer end-to-end ====================

@pytest.mark.parametrize("status", ["utkast", "avlyst"])
def test_behandling_fortsatt_sperret_for_utkast_og_avlyst(con, status):
    kid = _kurs(con, status="aapen" if status == "utkast" else "avlyst",
               pris_nok=1000, fakturering="person")
    # utkast-kurset maa faktisk vaere utkast for aa teste riktig registreringssperre; lag det direkte
    if status == "utkast":
        con.execute("UPDATE kurs SET status='utkast' WHERE id=?", (kid,))
    con.commit()
    klient = _klient()
    _logg_inn(klient)

    resp = klient.post(NY.format(kid=kid), data=_skjema())
    if status == "utkast":
        # utkast tillates for admin-registrering...
        assert resp.status_code == 302
        pid = _hent_pid(con)
        profil = klient.get(PROFIL.format(kid=kid, pid=pid)).get_data(as_text=True)
        assert BEHANDLE.format(kid=kid, pid=pid) not in profil  # ...men ingen knapp
        klient.post(BEHANDLE.format(kid=kid, pid=pid))
        rad = con.execute("SELECT sveiper_kjort, sveiper_utsatt FROM paamelding WHERE id=?", (pid,)).fetchone()
        assert rad["sveiper_utsatt"] == 1  # POST server-side avvist, ikke roert
        assert rad["sveiper_kjort"] == 0
    else:
        # avlyst blokkerer allerede selve registreringen
        assert resp.status_code == 400
        assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 0
    assert _antall_mail(con) == 0
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 0


def test_avsluttet_recovery_end_to_end(con):
    kid = _kurs(con, pris_nok=1000, fakturering="person")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(NY.format(kid=kid), data=_skjema())
    pid = _hent_pid(con)
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()

    resp = klient.post(BEHANDLE.format(kid=kid, pid=pid))
    assert resp.status_code == 302
    rad = con.execute("SELECT sveiper_kjort FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert rad["sveiper_kjort"] == 1
    assert _antall_mail(con) == 1


# ==================== punkt 12: paameldingsfrist ====================

def test_paameldingsfrist_blokkerer_offentlig_men_ikke_admin(con):
    kid = _kurs(con, kode="FRIST1", paameldingsfrist=(date.today() - timedelta(days=1)).isoformat())
    con.commit()
    klient = _klient()

    offentlig = klient.post(f"/kurs/FRIST1", data={
        "fornavn": "Ute", "etternavn": "Nordmann", "epost": "ute@x.no", "samtykke": "on", "samtykke_lagring": "on", **ADRESSE,
    })
    assert offentlig.status_code == 400
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 0

    _logg_inn(klient)
    admin_resp = klient.post(NY.format(kid=kid), data=_skjema())
    assert admin_resp.status_code == 302
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 1


# ==================== punkt 13-14: eksisterende person / reaktivering ====================

def test_eksisterende_person_gjenbrukes_og_tidligere_avmeldt_reaktiveres(con):
    kid1 = _kurs(con, "K1")
    kid2 = _kurs(con, "K2")
    pid_gammel, _ = db.meld_paa(con, kid1, epost="kari@x.no", fornavn="Kari", etternavn="Nordmann")
    con.execute("UPDATE paamelding SET sveiper_kjort=1, sveiper_utsatt=1, kilde='skjema' WHERE id=?", (pid_gammel,))
    db.meld_av(con, pid_gammel)  # tidligere avmeldt paa kurs 1
    antall_deltakere_foer = con.execute("SELECT COUNT(*) FROM deltaker").fetchone()[0]
    con.commit()

    klient = _klient()
    _logg_inn(klient)

    # gjenbruk av person paa et ANNET kurs (kid2)
    klient.post(NY.format(kid=kid2), data=_skjema())
    assert con.execute("SELECT COUNT(*) FROM deltaker").fetchone()[0] == antall_deltakere_foer

    # reaktivering PAA SAMME kurs (kid1) hvor personen tidligere ble avmeldt
    resp = klient.post(NY.format(kid=kid1), data=_skjema())
    assert resp.status_code == 302
    rad = con.execute(
        "SELECT id, status, kilde, sveiper_kjort, sveiper_utsatt FROM paamelding WHERE kurs_id=?", (kid1,)
    ).fetchone()
    assert rad["id"] == pid_gammel  # samme rad, reaktivert
    assert rad["status"] == "bekreftet"
    assert rad["kilde"] == "admin"
    assert rad["sveiper_kjort"] == 0
    assert rad["sveiper_utsatt"] == 1  # ny registrering - IKKE den gamle tilbakeholdelsen som "gjenbrukes automatisk"


# ==================== punkt 15 + samlet personvernkontroll ====================

def test_allergi_kun_i_sensitivt_aldri_i_loggen(con):
    kid = _kurs(con, type="fysisk", pris_nok=1000, fakturering="person")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post(NY.format(kid=kid), data=_skjema(allergier="Nøtteallergi", tilrettelegging="Rullestolrampe"))
    pid = _hent_pid(con)
    klient.post(BEHANDLE.format(kid=kid, pid=pid))

    sensitivt = con.execute(
        "SELECT allergier, tilrettelegging FROM sensitivt WHERE paamelding_id=?", (pid,)).fetchone()
    assert sensitivt["allergier"] == "Nøtteallergi"
    assert sensitivt["tilrettelegging"] == "Rullestolrampe"
    for r in con.execute("SELECT detaljer FROM hendelse"):
        d = (r["detaljer"] or "").lower()
        assert "nøtteallergi" not in d and "rullestolrampe" not in d


def test_samlet_personvernkontroll_hendelseslogg(con):
    """Kjorer et sammensatt scenario gjennom ALLE fase 9-rutene (offentlig paamelding, manuell
    admin-registrering inkl. organisasjon/faktura/allergi, eksplisitt behandling, avmelding,
    reaktivering) og sveiper deretter HELE hendelse-tabellen for personopplysninger."""
    kid = _kurs(con, kode="PV1", type="fysisk", kapasitet=1, pris_nok=1000, fakturering="person")
    con.commit()
    klient = _klient()

    # offentlig paamelding med sensitive data og organisasjon som betaler
    klient.post("/kurs/PV1", data={
        "fornavn": "Fyller", "etternavn": "Kapasitet", "epost": "fyller@sensitiv-domene.no", "telefon": "90000000",
        "samtykke": "on", "samtykke_lagring": "on", **SPORBAR_ADRESSE, "allergier": "Skalldyrallergi", "tilrettelegging": "Rullestol",
    })

    _logg_inn(klient)
    # manuell registrering (venteliste siden kapasitet=1 er fylt), med full faktura-/orginfo
    klient.post(NY.format(kid=kid), data=_skjema(
        navn="Hemmelig Deltaker", epost="hemmelig.deltaker@sensitiv-domene.no", telefon="91234567",
        betaler="organisasjon", org_navn="Sensitiv Bedrift AS", org_nr="999900003",
        faktura_adresse="Skjult Vei 1", faktura_postnr="0001", faktura_sted="Skjult By",
        faktura_ref="Konfidensiell ref", faktura_kommentar="Ikke vis dette noe sted i loggen",
        allergier="Peanøttallergi", tilrettelegging="Tegnspråktolk", **SPORBAR_ADRESSE,
    ))
    pid = _hent_pid(con, epost="hemmelig.deltaker@sensitiv-domene.no")
    klient.post(BEHANDLE.format(kid=kid, pid=pid))  # venteliste -> ventelistebeskjed
    klient.post(f"/admin/kurs/{kid}/deltaker/{pid}/status", data={"status": "avmeldt"})
    klient.post(NY.format(kid=kid), data=_skjema(
        navn="Hemmelig Deltaker", epost="hemmelig.deltaker@sensitiv-domene.no",
    ))  # reaktivering

    forbudte_tekster = [
        "fyller kapasitet", "fyller@sensitiv-domene.no", "90000000", "skalldyrallergi", "rullestol",
        "hemmelig deltaker", "hemmelig.deltaker@sensitiv-domene.no", "91234567",
        "sporbarveien 4711", "sporbarby",                       # deltakerens private adresse (også postnummeret: 4711)
        "sensitiv bedrift", "987654321", "skjult vei", "skjult by", "konfidensiell ref",
        "eksempel kommune", "999900003", "postboks 100",     # firmaopplysningene fra registeret hører heller ikke hjemme i loggen
        "ikke vis dette", "peanøttallergi", "tegnspråktolk",
    ]
    rader = con.execute("SELECT handling, detaljer FROM hendelse").fetchall()
    assert len(rader) > 5  # sanity: scenariet faktisk logget noe
    for r in rader:
        detaljer = (r["detaljer"] or "").lower()
        for tekst in forbudte_tekster:
            assert tekst not in detaljer, f"'{tekst}' funnet i hendelse.detaljer for handling={r['handling']!r}"
