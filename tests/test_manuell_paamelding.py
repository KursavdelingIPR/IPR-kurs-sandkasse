"""Fase 9, trinn 1: grunnlaget for manuell paamelding - meld_paa()-utvidelser, sveiper_utsatt,
og den presiserte statussjekken i sveiper.kjor(). Ingen admin-rute/UI ennaa (kommer i trinn 2-4)."""
import sqlite3
from datetime import date, timedelta

import pytest

from kurs import config, daglig, db, sveiper
from kurs.kjoring import Kjoring


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    return c


def _kurs(con, kode="T1", start=None, **kw):
    start = start or (date.today() + timedelta(days=30))
    return db.opprett_kurs(con, kode=kode, navn="Testkurs", datoer=[start.isoformat()],
                           sharepoint_mappe=f"Kurs/{kode}", **{"pris_nok": 1000, **kw})


def _antall_mail(con):
    return len(list(config.UTBOKS.glob("*.html"))) if config.UTBOKS.exists() else 0


# ---------------- meld_paa(): aktor ----------------

def test_meld_paa_uten_aktor_logger_deltaker_som_i_dag(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.commit()
    rad = con.execute("SELECT aktor FROM hendelse WHERE handling='paamelding' AND detaljer LIKE ?",
                      (f'%"paamelding_id": {pid}%',)).fetchone()
    assert rad["aktor"].startswith("deltaker:")


def test_meld_paa_med_aktor_logger_admin(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A", aktor="admin:kari")
    con.commit()
    rad = con.execute("SELECT aktor FROM hendelse WHERE handling='paamelding' AND detaljer LIKE ?",
                      (f'%"paamelding_id": {pid}%',)).fetchone()
    assert rad["aktor"] == "admin:kari"


# ---------------- meld_paa(): utkast-overstyring ----------------

def test_utkast_blokkeres_som_i_dag_uten_overstyring(con):
    kid = _kurs(con, status="utkast")
    con.commit()
    with pytest.raises(db.Paameldingsfeil):
        db.meld_paa(con, kid, epost="a@x.no", navn="A")


def test_utkast_tillates_med_eksplisitt_overstyring(con):
    kid = _kurs(con, status="utkast")
    con.commit()
    pid, status = db.meld_paa(con, kid, epost="a@x.no", navn="A", tillat_utkast=True)
    assert status == "bekreftet"


def test_avlyst_blokkeres_selv_med_alle_overstyringer(con):
    kid = _kurs(con, status="avlyst")
    con.commit()
    with pytest.raises(db.Paameldingsfeil):
        db.meld_paa(con, kid, epost="a@x.no", navn="A", tillat_utkast=True, ignorer_frist=True)


def test_avsluttet_blokkeres_selv_med_alle_overstyringer(con):
    kid = _kurs(con, status="avsluttet")
    con.commit()
    with pytest.raises(db.Paameldingsfeil):
        db.meld_paa(con, kid, epost="a@x.no", navn="A", tillat_utkast=True, ignorer_frist=True)


# ---------------- meld_paa(): frist-overstyring ----------------

def test_frist_blokkeres_som_i_dag_uten_overstyring(con):
    kid = _kurs(con)
    con.execute("UPDATE kurs SET paameldingsfrist=? WHERE id=?", ((date.today() - timedelta(days=1)).isoformat(), kid))
    con.commit()
    with pytest.raises(db.Paameldingsfeil, match="frist"):
        db.meld_paa(con, kid, epost="a@x.no", navn="A")


def test_frist_tillates_med_eksplisitt_overstyring(con):
    kid = _kurs(con)
    con.execute("UPDATE kurs SET paameldingsfrist=? WHERE id=?", ((date.today() - timedelta(days=1)).isoformat(), kid))
    con.commit()
    pid, status = db.meld_paa(con, kid, epost="a@x.no", navn="A", ignorer_frist=True)
    assert status == "bekreftet"


# ---------------- sveiper.kjor(): utvidet statussperre + sveiper_utsatt ----------------

def test_utkast_kurs_blir_ikke_behandlet_av_sveiper_selv_med_sveiper_kjort_0(con):
    kid = _kurs(con, status="utkast")
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A", tillat_utkast=True)
    con.commit()
    sveiper.kjor(Kjoring(con, idag=date.today()))
    assert _antall_mail(con) == 0
    assert con.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 0
    assert con.execute("SELECT sveiper_kjort FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == 0


def test_utkast_kurs_blir_ikke_behandlet_av_daglig_kjor(con):
    kid = _kurs(con, status="utkast")
    db.meld_paa(con, kid, epost="a@x.no", navn="A", tillat_utkast=True)
    con.commit()
    daglig.kjor(Kjoring(con, idag=date.today()))
    assert _antall_mail(con) == 0


def test_sveiper_utsatt_hindrer_automatisk_behandling_paa_normalt_kurs(con):
    kid = _kurs(con, kapasitet=5)  # helt vanlig, aapent kurs
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.execute("UPDATE paamelding SET sveiper_utsatt=1 WHERE id=?", (pid,))
    con.commit()
    sveiper.kjor(Kjoring(con, idag=date.today()))
    assert _antall_mail(con) == 0
    assert con.execute("SELECT sveiper_kjort FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == 0


def test_sveiper_utsatt_0_behandles_normalt(con):
    kid = _kurs(con, kapasitet=5)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.commit()
    sveiper.kjor(Kjoring(con, idag=date.today()))
    assert _antall_mail(con) == 1
    assert con.execute("SELECT sveiper_kjort FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == 1


def test_avsluttet_kurs_faar_fortsatt_recovery_av_sveiper_kjor(con):
    """Kritisk regresjonstest, samme prinsipp som forfalte_delfakturaer(): en rad som ikke ble
    behandlet (sveiper_kjort=0) fordi forrige forsok feilet (Visma nede/e-postfeil/driftsstans)
    mens kurset var aapent, maa fortsatt kunne fanges opp av sveiper.kjor() etter at kurset er
    blitt avsluttet (siste kursdag passert) - 'avsluttet' betyr ikke at behandlingen er ferdig."""
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()
    sveiper.kjor(Kjoring(con, idag=date.today()))
    assert _antall_mail(con) == 1
    assert con.execute("SELECT sveiper_kjort FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == 1


def test_utsatt_registrering_kan_fortsatt_utloses_manuelt_etter_at_kurset_er_avsluttet(con):
    """Simulerer den kommende 'Send bekreftelse og behandle fakturering naa'-knappen (trinn 3):
    admin registrerte og utsatte behandlingen mens kurset var aapent, men glemte aa trykke
    knappen for kurset ble avsluttet. Naar admin senere nullstiller sveiper_utsatt og kjorer
    sveiper for akkurat denne raden, skal bekreftelse/faktura fortsatt sendes - ikke stille
    forbli ubehandlet bare fordi kurset i mellomtiden har blitt avsluttet."""
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A", paamelding={"sveiper_utsatt": 1})
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()
    assert _antall_mail(con) == 0  # ingen automatikk har ruklet i mellomtiden

    # admin trykker "Send bekreftelse og behandle fakturering naa"
    con.execute("UPDATE paamelding SET sveiper_utsatt=0 WHERE id=?", (pid,))
    con.commit()
    sveiper.kjor(Kjoring(con, idag=date.today()), pid)
    assert _antall_mail(con) == 1
    assert con.execute("SELECT sveiper_kjort FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == 1


def test_forfalte_delfakturaer_behandler_fortsatt_avsluttet_kurs(con):
    """Kritisk regresjonstest: forfalte_delfakturaer() maa IKKE strammes inn til
    aapen/full/aktiv - en legitimt forfalt, mislykket delfaktura maa fortsatt kunne
    opprettes selv etter at kurset er blitt avsluttet (f.eks. etter driftsstans i Visma)."""
    start = date.today() - timedelta(days=5)
    kid = _kurs(con, start=start, betaling="per_samling", faktura_dager_for=14)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.execute("UPDATE paamelding SET sveiper_kjort=1 WHERE id=?", (pid,))  # simulerer at forstegangs-sveipen er gjort
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))  # kurset er na avsluttet
    con.commit()
    sveiper.forfalte_delfakturaer(Kjoring(con, idag=date.today()))
    assert con.execute("SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", (pid,)).fetchone()[0] == 1


def test_forfalte_delfakturaer_hopper_fortsatt_over_avlyst_kurs(con):
    start = date.today() - timedelta(days=5)
    kid = _kurs(con, start=start, betaling="per_samling", faktura_dager_for=14)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.execute("UPDATE paamelding SET sveiper_kjort=1 WHERE id=?", (pid,))
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kid,))
    con.commit()
    sveiper.forfalte_delfakturaer(Kjoring(con, idag=date.today()))
    assert con.execute("SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", (pid,)).fetchone()[0] == 0


# ---------------- sett_paamelding_status() / meld_av(): sveiper_utsatt nullstilles riktig ----------------

def test_bekreftelse_av_tilbakeholdt_venteliste_rad_nullstiller_sveiper_utsatt_og_sender(con):
    kid = _kurs(con, kapasitet=5)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.execute("UPDATE paamelding SET status='venteliste', sveiper_utsatt=1 WHERE id=?", (pid,))
    con.commit()
    kjor_for = db.sett_paamelding_status(con, pid, "bekreftet")
    con.commit()
    assert con.execute("SELECT sveiper_utsatt FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == 0
    assert kjor_for == pid
    sveiper.kjor(Kjoring(con, idag=date.today()), kjor_for)
    assert _antall_mail(con) == 1


def test_automatisk_opprykk_nullstiller_sveiper_utsatt(con):
    kid = _kurs(con, kapasitet=1)
    pa, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")  # bekreftet, fyller kapasitet
    pb, _ = db.meld_paa(con, kid, epost="b@x.no", navn="B")  # venteliste
    con.execute("UPDATE paamelding SET sveiper_utsatt=1 WHERE id=?", (pb,))
    con.commit()
    opprykket = db.meld_av(con, pa)
    con.commit()
    assert opprykket == pb
    assert con.execute("SELECT sveiper_utsatt FROM paamelding WHERE id=?", (pb,)).fetchone()[0] == 0
    sveiper.kjor(Kjoring(con, idag=date.today()), pb)
    assert con.execute("SELECT 1 FROM utsending_logg WHERE mottaker='b@x.no' AND type='bekreftelse'").fetchone()


# ---------------- meld_paa(): reaktivering av avmeldt rad nullstiller gammel behandlingsstatus ----------------

def test_reaktivering_nullstiller_stale_sveiper_utsatt_og_sveiper_kjort(con):
    """En rad som tidligere ble holdt tilbake (sveiper_utsatt=1) og deretter meldt av, skal IKKE
    dra den gamle tilbakeholdelsen med seg naar samme person melder seg paa samme kurs igjen -
    med mindre den nye paameldingen selv eksplisitt ber om det. Ellers ville en ny, vanlig
    paamelding kunne bli stille staaende ubehandlet pga. en rad som ikke lenger er relevant."""
    kid = _kurs(con, kapasitet=5)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.execute("UPDATE paamelding SET sveiper_kjort=1, sveiper_utsatt=1 WHERE id=?", (pid,))
    db.meld_av(con, pid)
    con.commit()

    pid2, status = db.meld_paa(con, kid, epost="a@x.no", navn="A", aktor="admin:kurs@ipr.no")
    con.commit()
    assert pid2 == pid  # samme rad reaktivert, ikke en ny
    assert status == "bekreftet"
    rad = con.execute(
        "SELECT sveiper_kjort, sveiper_utsatt, kilde FROM paamelding WHERE id=?", (pid,)
    ).fetchone()
    assert rad["sveiper_kjort"] == 0
    assert rad["sveiper_utsatt"] == 0
    sveiper.kjor(Kjoring(con, idag=date.today()))
    assert _antall_mail(con) == 1  # blir faktisk behandlet - ikke stille utsatt av gammel status


def test_reaktivering_med_admin_aktor_og_nye_fakturafelter_oppdaterer_alt(con):
    """Fase 9 sitt krav: naar admin reaktiverer en tidligere avmeldt paamelding manuelt med et
    fullt skjema (ny betaler/faktura-informasjon), skal den nye informasjonen faktisk overskrive
    det gamle, og kilde/behandlingsstatus skal reflektere den NYE registreringen, ikke den forrige."""
    kid = _kurs(con, kapasitet=5)
    pid, _ = db.meld_paa(
        con, kid, epost="a@x.no", navn="A",
        paamelding={"betaler": "organisasjon", "org_navn": "Gammel AS", "kilde": "web"},
    )
    con.execute("UPDATE paamelding SET sveiper_kjort=1, sveiper_utsatt=1 WHERE id=?", (pid,))
    db.meld_av(con, pid)
    con.commit()

    pid2, _ = db.meld_paa(
        con, kid, epost="a@x.no", navn="A", aktor="admin:kurs@ipr.no",
        paamelding={
            "betaler": "organisasjon", "org_navn": "Ny AS", "org_nr": "999888777",
            "faktura_ref": "Ny referanse", "kilde": "admin", "sveiper_utsatt": 1,
        },
    )
    con.commit()
    assert pid2 == pid
    rad = con.execute(
        "SELECT sveiper_kjort, sveiper_utsatt, kilde, org_navn, org_nr, faktura_ref "
        "FROM paamelding WHERE id=?", (pid,)
    ).fetchone()
    assert rad["sveiper_kjort"] == 0
    assert rad["sveiper_utsatt"] == 1  # eksplisitt bedt om aa vente denne gangen
    assert rad["kilde"] == "admin"
    assert rad["org_navn"] == "Ny AS"
    assert rad["org_nr"] == "999888777"
    assert rad["faktura_ref"] == "Ny referanse"


# ---------------- migrering ----------------

def test_migrering_legger_til_sveiper_utsatt_paa_gammel_database(tmp_path):
    sti = tmp_path / "gammel.db"
    gammel = sqlite3.connect(sti)
    gammel.execute("CREATE TABLE admin_bruker (id INTEGER PRIMARY KEY)")
    gammel.execute("CREATE TABLE kurs (id INTEGER PRIMARY KEY)")
    gammel.execute("CREATE TABLE deltaker (id INTEGER PRIMARY KEY)")
    gammel.execute("CREATE TABLE paamelding (id INTEGER PRIMARY KEY, kurs_id INTEGER, deltaker_id INTEGER, "
                   "status TEXT, opprettet TEXT)")
    gammel.execute("INSERT INTO paamelding (id, kurs_id, deltaker_id, status, opprettet) "
                   "VALUES (1, 1, 1, 'bekreftet', '2020-01-01T00:00:00')")
    gammel.commit()
    gammel.close()

    con = sqlite3.connect(sti)
    con.row_factory = sqlite3.Row
    db._migrer(con)
    con.commit()
    assert any(r["name"] == "sveiper_utsatt" for r in con.execute("PRAGMA table_info(paamelding)"))
    rad = con.execute("SELECT sveiper_utsatt FROM paamelding WHERE id=1").fetchone()
    assert rad["sveiper_utsatt"] == 0
    db._migrer(con)  # kjort to ganger -> ingen feil
    con.commit()
