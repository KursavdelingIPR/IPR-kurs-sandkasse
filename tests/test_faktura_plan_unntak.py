"""Unntak fra kursets fakturaplan for én deltaker (Camilla 02.10.2026).

Hun vil helst ikke at deltakerne endrer på fakturaen (det gir ekstra arbeid; fakturamottakeren kan ikke endres etter påmelding, og delt faktura
ønskes oppgitt ved påmeldingen), «men det må selvfølgelig være mulig i enkelte tilfeller». Kurset bestemmer standarden, og administrator kan i
deltakervinduet velge samlet eller per samling for akkurat én deltaker, så lenge ingenting er fakturert eller forsøkt fakturert.

Etter en faktura låses planen: en samlet faktura ville kommet i tillegg til delfakturaer (eller omvendt), og deltakeren kunne blitt fakturert
to ganger. Bytter administrator plan, må påmeldingen gjennom fakturaplanen på nytt, ellers ville morgenjobben aldri laget fakturaen."""
from datetime import date, timedelta
from pathlib import Path

import pytest

from kurs import config, daglig, db, hendelseslogg
from kurs.integrasjoner import epost, visma
from kurs.kjoring import Kjoring

IDAG = date(2027, 1, 10)
NAER = date(2027, 2, 1)          # under seks måneder fram: en samlet faktura lages med en gang
LANGT = date(2027, 9, 20)        # over seks måneder fram: en samlet faktura holdes til 2027-03-20


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    return c


@pytest.fixture
def ute(monkeypatch):
    """Fanger utgående e-post og Visma-kall (Visma kjører ellers sin vanlige demo-gren)."""
    kall = {"epost": [], "visma": []}
    monkeypatch.setattr(epost, "send", lambda til, emne, html, *a, **kw: kall["epost"].append((til, emne)))
    ekte = visma.fakturer

    def visma_fakturer(g):
        kall["visma"].append(g.belop_nok)
        return ekte(g)
    monkeypatch.setattr(visma, "fakturer", visma_fakturer)
    return kall


@pytest.fixture
def fast_dato(monkeypatch):
    """Styrer «i dag» i appen (ruten sender påmeldingen gjennom fakturaplanen med denne datoen)."""
    from kurs.web import app as webapp
    monkeypatch.setattr(webapp, "_idag", lambda: IDAG)


def _klient():
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def _fersk(con):
    return db.koble(config.DB_STI)


def _kurs(con, start, betaling="samlet", antall=2, kode="U1", **kw):
    datoer = [(start + timedelta(days=30 * n)).isoformat() for n in range(antall)]
    kid = db.opprett_kurs(con, kode=kode, navn="Testkurs", datoer=datoer, sharepoint_mappe=f"Kurs/{kode}",
                          **{"pris_nok": 1000, "fakturering": "person", "betaling": betaling, **kw})
    con.commit()
    return kid


def _paamelding(con, kid, epost_="a@x.no"):
    pid, _ = db.meld_paa(con, kid, epost=epost_, fornavn="Navn", etternavn=epost_.split("@")[0])
    con.commit()
    return pid


def _kjor(con, idag, pid=None):
    from kurs import sveiper
    sveiper.kjor(Kjoring(con, idag=idag), pid)
    con.commit()


def _tilstand(con, pid):
    r = _fersk(con).execute("SELECT betaling, sveiper_kjort, faktura_tidligst_dato FROM paamelding WHERE id=?", (pid,)).fetchone()
    return tuple(r)


def _fakturaer(con, pid):
    return [(r["kursdag_id"] is not None, r["belop_nok"]) for r in
            _fersk(con).execute("SELECT kursdag_id, belop_nok FROM faktura WHERE paamelding_id=? ORDER BY id", (pid,))]


def _side(klient, kid, pid):
    return klient.get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)


def _velg(klient, kid, pid, plan):
    return klient.post(f"/admin/kurs/{kid}/deltaker/{pid}/paamelding", data={"betaler": "person", "betaling": plan},
                       follow_redirects=True).get_data(as_text=True)


def _hendelser(con, handling):
    return [r["detaljer"] for r in _fersk(con).execute("SELECT detaljer FROM hendelse WHERE handling=? ORDER BY id", (handling,))]


def test_kursene_i_testene_har_to_samlinger(con):
    assert db.antall_samlinger(con, _kurs(con, NAER)) == 2 and db.antall_samlinger(con, _kurs(con, LANGT, kode="U2")) == 2


# ---------------- unntaket ----------------

def test_samlet_kurs_unntak_til_delt_faktura_fjerner_holdet_og_delfakturaene_kommer_foer_hver_samling(con, ute, fast_dato):
    kid = _kurs(con, LANGT, "samlet")
    pid = _paamelding(con, kid)
    _kjor(con, IDAG, pid)
    assert _tilstand(con, pid) == ("samlet", 1, "2027-03-20")                    # holdt til seks måneder før første kursdag
    bekreftelser = [e for e in ute["epost"] if e[1].startswith("Bekreftelse")]
    klient = _klient()
    side = _side(klient, kid, pid)
    assert 'name="betaling" value="per_samling"' in side and "Kurset er satt til én samlet faktura" in side
    side = _velg(klient, kid, pid, "per_samling")
    assert "Fakturaplanen for" in side and "én faktura per samling" in side and "14 dager før samlingen" in side
    assert "ikke ny bekreftelse" in side
    assert _tilstand(con, pid) == ("per_samling", 1, None)                       # holdet er borte, og behandlingen gikk om igjen
    assert [e for e in ute["epost"] if e[1].startswith("Bekreftelse")] == bekreftelser    # ingen ny bekreftelse
    assert _fersk(con).execute("SELECT betaling FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "samlet"   # kurset er urørt
    daglig.kjor(Kjoring(con, idag=date(2027, 3, 20)))                            # den samlede fakturaen ville kommet nå
    assert ute["visma"] == [] and _fakturaer(con, pid) == []
    for dag in (date(2027, 9, 6), date(2027, 9, 6), date(2027, 9, 7)):           # 14 dager før første samling, flere kjøringer
        daglig.kjor(Kjoring(con, idag=dag))
    assert ute["visma"] == [500] and _fakturaer(con, pid) == [(True, 500)]
    for dag in (date(2027, 10, 6), date(2027, 10, 6)):                           # 14 dager før den andre
        daglig.kjor(Kjoring(con, idag=dag))
    assert ute["visma"] == [500, 500] and _fakturaer(con, pid) == [(True, 500), (True, 500)]


def test_delt_kurs_unntak_til_en_faktura_blir_fakturert_av_morgenjobben_og_aldri_to_ganger(con, ute, fast_dato):
    """Uten at påmeldingen går gjennom planen på nytt, ville ingen laget fakturaen: den samlede fakturaen holdes bare for påmeldinger med hold-dato."""
    kid = _kurs(con, LANGT, "per_samling")
    pid = _paamelding(con, kid)
    _kjor(con, IDAG, pid)
    assert _tilstand(con, pid) == ("per_samling", 1, None)
    _velg(_klient(), kid, pid, "samlet")
    assert _tilstand(con, pid) == ("samlet", 1, "2027-03-20")                    # nå holdt til seks måneder før første kursdag
    daglig.kjor(Kjoring(con, idag=date(2027, 3, 19)))
    assert ute["visma"] == []
    for dag in (date(2027, 3, 20), date(2027, 3, 20), date(2027, 4, 1)):
        daglig.kjor(Kjoring(con, idag=dag))
    assert ute["visma"] == [1000] and _fakturaer(con, pid) == [(False, 1000)]    # én samlet faktura
    for dag in (date(2027, 9, 6), date(2027, 10, 6)):                            # og ingen delfakturaer i tillegg
        daglig.kjor(Kjoring(con, idag=dag))
    assert ute["visma"] == [1000] and _fakturaer(con, pid) == [(False, 1000)]


def test_unntak_til_samlet_naer_kursstart_lager_fakturaen_med_en_gang(con, ute, fast_dato):
    kid = _kurs(con, NAER, "per_samling")
    pid = _paamelding(con, kid)
    _kjor(con, IDAG, pid)
    assert ute["visma"] == [] and _tilstand(con, pid) == ("per_samling", 1, None)
    _velg(_klient(), kid, pid, "samlet")
    assert ute["visma"] == [1000] and _fakturaer(con, pid) == [(False, 1000)]
    daglig.kjor(Kjoring(con, idag=IDAG + timedelta(days=1)))
    assert ute["visma"] == [1000]


def test_unntak_gjelder_bare_denne_deltakeren(con, ute, fast_dato):
    kid = _kurs(con, LANGT, "samlet")
    a, b = _paamelding(con, kid, "a@x.no"), _paamelding(con, kid, "b@x.no")
    _kjor(con, IDAG)
    _velg(_klient(), kid, a, "per_samling")
    assert _tilstand(con, a)[0] == "per_samling" and _tilstand(con, b) == ("samlet", 1, "2027-03-20")


def test_samme_plan_en_gang_til_er_ingen_endring(con, ute, fast_dato):
    kid = _kurs(con, LANGT, "samlet")
    pid = _paamelding(con, kid)
    _kjor(con, IDAG, pid)
    _velg(_klient(), kid, pid, "samlet")
    assert _tilstand(con, pid) == ("samlet", 1, "2027-03-20") and _hendelser(con, "faktura_plan_endret") == []


def test_planbyttet_logges_og_vises_i_loggen_til_deltakeren(con, ute, fast_dato):
    kid = _kurs(con, LANGT, "samlet")
    pid = _paamelding(con, kid)
    _kjor(con, IDAG, pid)
    _velg(_klient(), kid, pid, "per_samling")
    assert _hendelser(con, "faktura_plan_endret") == [f'{{"paamelding_id": {pid}, "kurs_id": {kid}, "fra": "samlet", "til": "per_samling"}}']
    p = _fersk(con).execute("SELECT * FROM paamelding WHERE id=?", (pid,)).fetchone()
    rad = next(r for r in hendelseslogg.for_paamelding(_fersk(con), p, systemadmin=False).rader if r.hva.startswith("Fakturaplan endret"))
    assert rad.hva == "Fakturaplan endret (unntak fra kursets plan)"
    assert rad.detaljer == ["Før: Samlet – hele beløpet i én faktura", "Ny: Per samling – én faktura før hver samling"]


def test_deltaker_paa_venteliste_kan_faa_unntak_uten_at_noe_behandles(con, ute, fast_dato):
    kid = _kurs(con, LANGT, "samlet", kapasitet=1)
    _paamelding(con, kid, "a@x.no")
    venter = _paamelding(con, kid, "b@x.no")
    assert _fersk(con).execute("SELECT status FROM paamelding WHERE id=?", (venter,)).fetchone()[0] == "venteliste"
    _velg(_klient(), kid, venter, "per_samling")
    assert _tilstand(con, venter) == ("per_samling", 0, None)                    # ikke behandlet ennå; planen gjelder når hen rykker opp


# ---------------- reglene ligger i db, ikke i nettsiden ----------------

def test_db_bytter_plan_og_nullstiller_behandlingen_for_en_bekreftet_paamelding(con):
    kid = _kurs(con, LANGT, "samlet")
    pid = _paamelding(con, kid)
    _kjor(con, IDAG, pid)
    assert _tilstand(con, pid) == ("samlet", 1, "2027-03-20")
    assert db.bytt_fakturaplan(con, pid, "per_samling", aktor="admin:test") is True
    con.commit()
    assert _tilstand(con, pid) == ("per_samling", 0, None)                           # klar for å gå gjennom planen på nytt
    assert db.bytt_fakturaplan(con, pid, "per_samling") is False                      # samme plan: ingen endring
    assert len(_hendelser(con, "faktura_plan_endret")) == 1


def test_db_avviser_planbytte_etter_faktura_forsoek_ugyldig_plan_og_ukjent_paamelding(con):
    kid = _kurs(con, NAER, "samlet")
    pid = _paamelding(con, kid)
    con.execute("INSERT INTO faktura (paamelding_id, belop_nok, status) VALUES (?, 1000, 'sendt')", (pid,))
    con.commit()
    with pytest.raises(db.Paameldingsfeil, match="allerede fakturert"):
        db.bytt_fakturaplan(con, pid, "per_samling")
    annen = _paamelding(con, kid, "b@x.no")
    con.execute("INSERT INTO faktura_forsok (paamelding_id, status) VALUES (?, 'feilet')", (annen,))
    con.commit()
    with pytest.raises(db.Paameldingsfeil, match="fakturaforsøk"):
        db.bytt_fakturaplan(con, annen, "per_samling")
    tredje = _paamelding(con, kid, "c@x.no")
    for ugyldig in ("deltaker_velger", "", "annet"):
        with pytest.raises(db.Paameldingsfeil, match="Ugyldig"):
            db.bytt_fakturaplan(con, tredje, ugyldig)
    with pytest.raises(db.Paameldingsfeil, match="Ukjent"):
        db.bytt_fakturaplan(con, 99999, "per_samling")
    assert [_tilstand(con, x)[0] for x in (pid, annen, tredje)] == ["samlet"] * 3 and _hendelser(con, "faktura_plan_endret") == []


def test_nettsiden_kjenner_ikke_fakturamotorens_felt(con):
    """Vakten i test_faktura_utsatt.py krever at web/app.py ikke nevner hold-dato eller planfunksjonene: tilstanden endres i db."""
    kilde = (Path(__file__).resolve().parent.parent / "kurs" / "web" / "app.py").read_text(encoding="utf-8")
    assert "faktura_tidligst_dato" not in kilde and "sveiper_kjort=0" not in kilde and "faktura_plan_endret" not in kilde


# ---------------- låsene ----------------

def test_planen_kan_ikke_endres_etter_faktura_og_siden_forklarer_hvorfor(con, ute, fast_dato):
    kid = _kurs(con, NAER, "samlet")
    pid = _paamelding(con, kid)
    _kjor(con, IDAG, pid)
    assert ute["visma"] == [1000]
    klient = _klient()
    side = _side(klient, kid, pid)
    assert 'name="betaling" value="per_samling"' not in side
    assert "allerede fakturert – planen kan ikke endres etter at en faktura er laget" in side
    assert "Det er laget 1 faktura til" in side and "står i Visma og må rettes der" in side
    _velg(klient, kid, pid, "per_samling")
    assert _tilstand(con, pid)[0] == "samlet" and _fakturaer(con, pid) == [(False, 1000)]
    for dag in (date(2027, 1, 18), date(2027, 2, 1), date(2027, 3, 3)):
        daglig.kjor(Kjoring(con, idag=dag))
    assert ute["visma"] == [1000] and _hendelser(con, "faktura_plan_endret") == []


def test_gammel_side_med_plan_paa_laast_paamelding_lagrer_resten_uten_feilmelding(con, ute, fast_dato):
    """Radioknappene vises ikke for en påmelding som er fakturert. Kommer planen likevel med (en side som var åpen fra før), ignoreres den, og resten lagres."""
    kid = _kurs(con, NAER, "samlet")
    pid = _paamelding(con, kid)
    _kjor(con, IDAG, pid)
    side = _klient().post(f"/admin/kurs/{kid}/deltaker/{pid}/paamelding", follow_redirects=True,
                          data={"betaler": "person", "betaling": "per_samling", "faktura_ref": "REF-1"}).get_data(as_text=True)
    assert "Påmeldingen er oppdatert." in side and "Fakturaplanen kan ikke endres" not in side
    assert _tilstand(con, pid)[0] == "samlet"
    assert _fersk(con).execute("SELECT faktura_ref FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == "REF-1"


def test_faktura_som_rekker_aa_bli_laget_mellom_visning_og_lagring_gir_feilmelding_og_ingenting_lagres(con, ute, fast_dato, monkeypatch):
    from kurs.web import app as webapp
    kid = _kurs(con, NAER, "samlet")
    pid = _paamelding(con, kid)
    _kjor(con, IDAG, pid)                                                        # fakturert
    monkeypatch.setattr(webapp, "_prisvalg", lambda kurs, p: (True, ""))        # som om fakturaen ble laget etter at siden ble vist
    side = _klient().post(f"/admin/kurs/{kid}/deltaker/{pid}/paamelding", follow_redirects=True,
                          data={"betaler": "person", "betaling": "per_samling", "faktura_ref": "REF-2"}).get_data(as_text=True)
    assert "Fakturaplanen kan ikke endres: allerede fakturert" in side
    assert _tilstand(con, pid)[0] == "samlet" and _fakturaer(con, pid) == [(False, 1000)]
    assert _fersk(con).execute("SELECT faktura_ref FROM paamelding WHERE id=?", (pid,)).fetchone()[0] is None      # ingenting ble lagret


def test_delfaktura_laaser_ogsaa_planen(con, ute, fast_dato):
    kid = _kurs(con, NAER, "per_samling")
    pid = _paamelding(con, kid)
    _kjor(con, date(2027, 1, 18), pid)                                           # 14 dager før første samling: første delfaktura
    assert _fakturaer(con, pid) == [(True, 500)]
    _velg(_klient(), kid, pid, "samlet")
    assert _tilstand(con, pid)[0] == "per_samling" and _fakturaer(con, pid) == [(True, 500)]


def test_uavklart_fakturaforsoek_stopper_planbytte(con, ute, fast_dato):
    kid = _kurs(con, LANGT, "samlet")
    pid = _paamelding(con, kid)
    con.execute("INSERT INTO faktura_forsok (paamelding_id, status) VALUES (?, 'ukjent')", (pid,))
    con.commit()
    klient = _klient()
    assert "et fakturaforsøk er ikke avklart" in _side(klient, kid, pid)
    _velg(klient, kid, pid, "per_samling")
    assert _tilstand(con, pid)[0] == "samlet" and _hendelser(con, "faktura_plan_endret") == []


def test_ekstradeltaker_faktureres_manuelt_og_har_ingen_plan_aa_velge(con, ute, fast_dato):
    kid = _kurs(con, LANGT, "samlet")
    pid = _paamelding(con, kid)
    con.execute("UPDATE paamelding SET ekstradeltaker_ts='2027-01-05T10:00:00' WHERE id=?", (pid,))
    con.commit()
    klient = _klient()
    assert "faktureres manuelt (ekstradeltaker)" in _side(klient, kid, pid)
    _velg(klient, kid, pid, "per_samling")
    assert _tilstand(con, pid)[0] == "samlet"


def test_kurs_med_bare_en_samling_har_ikke_noe_aa_velge_selv_om_kurset_er_samlet(con, ute, fast_dato):
    kid = _kurs(con, LANGT, "samlet", antall=1)
    pid = _paamelding(con, kid)
    klient = _klient()
    assert "kurset har bare én samling" in _side(klient, kid, pid) and 'name="betaling" value="per_samling"' not in _side(klient, kid, pid)
    _velg(klient, kid, pid, "per_samling")
    assert _tilstand(con, pid)[0] == "samlet"
