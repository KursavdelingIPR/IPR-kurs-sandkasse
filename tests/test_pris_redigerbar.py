"""Pris og fakturaoppsett i Oppsett kan endres også når kurset har fakturaer (Camilla 01.10.2026: «feltene, blant annet prisen, må være
redigerbare» - den som bruker systemet kan ikke være avhengig av en utvikler for å rette en pris).

Reglene: endringen må bekreftes (avkryssingen bekreft_okonomi), fakturaer som er laget endres aldri (og ingen påmelding får to), ny pris
gjelder bare fakturaer som ikke er laget ennå, en pris som ville gitt en faktura på 0 kr avvises, og alt skrives til loggen
(kurs_okonomi_endret). Uten bekreftelse er pris og fakturaoppsett urørt, som før."""
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from kurs import config, daglig, db, sveiper
from kurs.integrasjoner import epost, visma
from kurs.kjoring import Kjoring

START = date(2027, 2, 1)
IDAG = date(2027, 1, 10)         # mindre enn seks måneder før kursstart: samlet faktura lages med en gang


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


def _klient():
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def _fersk(con):
    return db.koble(config.DB_STI)


def _kurs(con, kode="P1", ant_dager=1, **kw):
    datoer = [(START + timedelta(days=30 * n)).isoformat() for n in range(ant_dager)]
    kid = db.opprett_kurs(con, kode=kode, navn="Testkurs", datoer=datoer, sharepoint_mappe=f"Kurs/{kode}",
                          **{"pris_nok": 1000, "fakturering": "person", **kw})
    con.commit()
    return kid


def _paamelding(con, kid, epost_="a@x.no"):
    pid, _ = db.meld_paa(con, kid, epost=epost_, fornavn="Navn", etternavn=epost_.split("@")[0])
    con.commit()
    return pid


def _faktura(con, pid, belop=1000, kursdag_id=None):
    con.execute("INSERT INTO faktura (paamelding_id, kursdag_id, belop_nok, status) VALUES (?, ?, ?, 'sendt')", (pid, kursdag_id, belop))
    con.commit()


def _data(**over):
    data = {"navn": "Testkurs", "type": "fysisk", "sted": "Bergen", "zoom_url": "", "kapasitet": "", "paameldingsfrist": "",
            "pris_nok": "1000", "fakturering": "person", "betaling": "samlet", "faktura_dager_for": "14",
            "kursholder_epost": "", "notat": ""}
    data.update(over)
    return data


def _lagre(klient, kid, **over):
    """Sender Oppsett og returnerer siden etter viderekoblingen (der meldingene vises)."""
    return klient.post(f"/admin/kurs/{kid}/oppsett", data=_data(**over), follow_redirects=True).get_data(as_text=True)


def _kurs_rad(con, kid):
    return tuple(_fersk(con).execute("SELECT pris_nok, fakturering, betaling, faktura_dager_for FROM kurs WHERE id=?", (kid,)).fetchone())


def _hendelser(con, handling):
    return [json.loads(r["detaljer"]) for r in _fersk(con).execute("SELECT detaljer FROM hendelse WHERE handling=? ORDER BY id", (handling,))]


# ---------------- siden ----------------

def test_siden_viser_redigerbare_felt_og_advarsel_naar_kurset_har_fakturaer(con):
    kid = _kurs(con)
    _faktura(con, _paamelding(con, kid))
    side = _klient().get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True)
    assert 'name="pris_nok"' in side and 'value="1000"' in side and 'name="betalingsmaate"' in side and 'name="betaling"' in side
    assert "Kurset har allerede 1 faktura." in side
    assert "Fakturaer som allerede er laget, endres ikke" in side and "gjelder bare nye påmeldinger" in side
    assert 'name="bekreft_okonomi"' in side and "data-okonomi-bekreft" in side and "data-okonomi>" in side
    assert "kan ikke endres" not in side                                                     # den gamle «låst»-teksten er borte


def test_advarselen_nevner_uavklarte_fakturaforsok_bare_naar_de_finnes(con):
    kid = _kurs(con)
    pid = _paamelding(con, kid)
    _faktura(con, pid)
    assert "uavklarte fakturaforsøk" not in _klient().get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True)
    con.execute("INSERT INTO faktura_forsok (paamelding_id, status) VALUES (?, 'ukjent')", (_paamelding(con, kid, "b@x.no"),))
    con.commit()
    side = _klient().get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True)
    assert "Du har uavklarte fakturaforsøk" in side and "Kurset har allerede 1 faktura og 1 uavklart fakturaforsøk." in side


def test_siden_for_kurs_uten_fakturaer_har_felt_men_ingen_advarsel(con):
    kid = _kurs(con)
    side = _klient().get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True)
    assert 'name="pris_nok"' in side and 'name="betalingsmaate"' in side
    assert "bekreft_okonomi" not in side and "Kurset har allerede" not in side and "data-okonomi-varsel" not in side


def test_javascript_henger_sammen_med_malen():
    js = (Path(__file__).resolve().parent.parent / "kurs" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "visOkonomiBekreftelse" in js and "[data-okonomi-bekreft]" in js and "[data-okonomi]" in js
    assert "defaultChecked" in js and "defaultValue" in js                                   # sammenlignes med det som er lagret


def test_binding_tekst_beskriver_tallene_paa_norsk():
    from kurs.web.app import _binding_tekst
    assert _binding_tekst({"fakturaer": 3, "forsok": 1, "planlagt": 2}) == "3 fakturaer, 1 uavklart fakturaforsøk og 2 planlagte fakturaer"
    assert _binding_tekst({"fakturaer": 1, "forsok": 0, "planlagt": 0}) == "1 faktura"
    assert _binding_tekst({"fakturaer": 0, "forsok": 2, "planlagt": 1}) == "2 uavklarte fakturaforsøk og 1 planlagt faktura"
    assert _binding_tekst({"fakturaer": 0, "forsok": 0, "planlagt": 0}) == ""


def test_oversikten_teller_fakturaer_forsok_og_planlagte_hver_for_seg(con):
    kid = _kurs(con)
    med_faktura, med_forsok, planlagt, hold_og_faktura, avmeldt = (_paamelding(con, kid, f"{n}@x.no") for n in "abcde")
    _faktura(con, med_faktura)
    con.execute("INSERT INTO faktura_forsok (paamelding_id, status) VALUES (?, 'ukjent')", (med_forsok,))
    for pid in (planlagt, hold_og_faktura, avmeldt):
        con.execute("UPDATE paamelding SET faktura_tidligst_dato='2027-03-20' WHERE id=?", (pid,))
    _faktura(con, hold_og_faktura)                                                           # har både hold og faktura: telles som faktura
    db.meld_av(con, avmeldt)                                                                 # avmeldt: et gammelt hold teller ikke
    con.commit()
    assert db.okonomisk_binding_oversikt(con, kid) == {"fakturaer": 2, "forsok": 1, "planlagt": 1}


# ---------------- bekreftet endring ----------------

def test_bekreftet_endring_lagres_og_fakturaen_som_finnes_er_urort(con):
    kid = _kurs(con)
    pid = _paamelding(con, kid)
    _faktura(con, pid, 1000)
    side = _lagre(_klient(), kid, pris_nok="1500", bekreft_okonomi="1")
    assert _kurs_rad(con, kid)[0] == 1500
    assert "Pris og fakturaoppsett er endret. Fakturaer som allerede er laget, er ikke endret." in side
    fakturaer = _fersk(con).execute("SELECT belop_nok FROM faktura WHERE paamelding_id=?", (pid,)).fetchall()
    assert [r["belop_nok"] for r in fakturaer] == [1000]                                      # aldri endret, aldri en til
    assert "pris_nok" in _hendelser(con, "kurs_endret")[-1]["felt"]


def test_endringen_skrives_til_loggen_uten_persondata(con):
    kid = _kurs(con)
    _faktura(con, _paamelding(con, kid))
    con.execute("INSERT INTO faktura_forsok (paamelding_id, status) VALUES (?, 'feilet')", (_paamelding(con, kid, "b@x.no"),))
    con.commit()
    _lagre(_klient(), kid, pris_nok="1500", betaling="per_samling", faktura_dager_for="30", bekreft_okonomi="1")
    (h,) = _hendelser(con, "kurs_okonomi_endret")
    assert h == {"kurs_id": kid, "endringer": {"pris_nok": [1000, 1500], "betaling": ["samlet", "per_samling"],
                                                "faktura_dager_for": [14, 30]}, "fakturaer": 1, "forsok": 1, "planlagt": 0}


def test_alle_fire_felt_kan_endres_med_bekreftelse(con):
    kid = _kurs(con)
    _faktura(con, _paamelding(con, kid))
    _lagre(_klient(), kid, pris_nok="2000", fakturering="organisasjon", betaling="per_samling", faktura_dager_for="30",
           bekreft_okonomi="1")
    assert _kurs_rad(con, kid) == (2000, "organisasjon", "per_samling", 30)
    assert set(_hendelser(con, "kurs_okonomi_endret")[0]["endringer"]) == {"pris_nok", "fakturering", "betaling", "faktura_dager_for"}


def test_det_ekte_skjemaet_med_betalingsmaate_og_fakturaplan_kan_bekreftes(con):
    """Slik nettleseren sender det: avkrysset Faktura (betalingsmaate), fakturaplan som radioknapp, bekreftelsen avkrysset."""
    kid = _kurs(con)
    _faktura(con, _paamelding(con, kid))
    klient = _klient()
    data = {k: v for k, v in _data(pris_nok="1200", betaling="per_samling", faktura_dager_for="21", bekreft_okonomi="1").items()
            if k != "fakturering"}
    klient.post(f"/admin/kurs/{kid}/oppsett", data={**data, "betalingsmaater": "1", "betalingsmaate": "faktura"})
    assert _kurs_rad(con, kid) == (1200, "person", "per_samling", 21)


def test_uendret_pris_med_bekreftelse_gir_ingen_okonomihendelse(con):
    kid = _kurs(con)
    _faktura(con, _paamelding(con, kid))
    _lagre(_klient(), kid, navn="Nytt navn", bekreft_okonomi="1")
    assert _hendelser(con, "kurs_okonomi_endret") == [] and _kurs_rad(con, kid) == (1000, "person", "samlet", 14)


# ---------------- uten bekreftelse ----------------

def test_uten_bekreftelse_er_pris_og_fakturaoppsett_urort_og_administratoren_faar_beskjed(con):
    kid = _kurs(con)
    _faktura(con, _paamelding(con, kid))
    side = _lagre(_klient(), kid, navn="Annet navn", pris_nok="1500", betaling="per_samling")
    assert _kurs_rad(con, kid) == (1000, "person", "samlet", 14)                              # urørt
    assert _fersk(con).execute("SELECT navn FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "Annet navn"   # resten lagres
    assert "Kursoppsett oppdatert." in side and "Pris og fakturaoppsett er IKKE endret" in side
    assert _hendelser(con, "kurs_okonomi_endret") == []


def test_uten_endring_av_pris_og_fakturaoppsett_er_ingen_bekreftelse_nodvendig(con):
    kid = _kurs(con)
    _faktura(con, _paamelding(con, kid))
    side = _lagre(_klient(), kid, navn="Bare navnet")
    assert "IKKE endret" not in side and "Kursoppsett oppdatert." in side


def test_gammel_side_uten_okonomifelt_endrer_ikke_noe_selv_med_bekreftelse(con):
    kid = _kurs(con)
    _faktura(con, _paamelding(con, kid))
    _klient().post(f"/admin/kurs/{kid}/oppsett", data={"navn": "Fra gammel side", "type": "fysisk", "sted": "Bergen",
                                                        "bekreft_okonomi": "1"})
    assert _kurs_rad(con, kid) == (1000, "person", "samlet", 14)
    assert _fersk(con).execute("SELECT navn FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "Fra gammel side"
    assert _hendelser(con, "kurs_okonomi_endret") == []


def test_kurs_uten_fakturaer_endres_som_for_uten_bekreftelse_og_uten_okonomihendelse(con):
    kid = _kurs(con)
    side = _lagre(_klient(), kid, pris_nok="2500", betaling="per_samling")
    assert _kurs_rad(con, kid) == (2500, "person", "per_samling", 14)
    assert "IKKE endret" not in side and _hendelser(con, "kurs_okonomi_endret") == []


# ---------------- på db-nivå ----------------

def test_db_endrer_bare_med_uttrykkelig_tillatelse(con):
    kid = _kurs(con)
    _faktura(con, _paamelding(con, kid))
    assert db.oppdater_kurs_felter(con, kid, {"pris_nok": 1800}) == []                       # som før: strippet
    assert db.oppdater_kurs_felter(con, kid, {"pris_nok": 1800}, tillat_okonomi=True) == ["pris_nok"]
    con.commit()
    assert _kurs_rad(con, kid)[0] == 1800 and len(_hendelser(con, "kurs_okonomi_endret")) == 1


# ---------------- hva endringen betyr for fakturaene ----------------

def test_ny_pris_gjelder_fakturaer_som_ikke_er_laget_ennaa_og_ingen_faar_to(con, ute):
    kid = _kurs(con)
    a = _paamelding(con, kid, "a@x.no")
    sveiper.kjor(Kjoring(con, idag=IDAG), a)
    con.commit()
    assert ute["visma"] == [1000]
    _lagre(_klient(), kid, pris_nok="1500", bekreft_okonomi="1")
    b = _paamelding(con, kid, "b@x.no")
    sveiper.kjor(Kjoring(con, idag=IDAG), b)
    con.commit()
    assert ute["visma"] == [1000, 1500]                                                       # A beholdt sin, B fikk den nye prisen
    for _ in range(2):                                                                        # morgenjobben: ingenting skjer andre gang
        daglig.kjor(Kjoring(con, idag=IDAG + timedelta(days=1)))
    rader = _fersk(con).execute("SELECT paamelding_id, belop_nok FROM faktura ORDER BY paamelding_id").fetchall()
    assert [(r["paamelding_id"], r["belop_nok"]) for r in rader] == [(a, 1000), (b, 1500)]


def _kurs_med_delfaktura(con):
    """To samlinger, én deltaker som fakturaes per samling og har fått 600 kr av 1000 for den første."""
    kid = _kurs(con, ant_dager=2)
    pid = _paamelding(con, kid)
    con.execute("UPDATE paamelding SET betaling='per_samling' WHERE id=?", (pid,))
    _faktura(con, pid, 600, kursdag_id=db.kursdager(con, kid)[0]["id"])
    return kid


def test_pris_som_gir_nullkroners_delfaktura_avvises(con):
    kid = _kurs_med_delfaktura(con)
    for for_lav in (500, 600):                                                                # resten ville blitt 0 kr
        feil = db.prisendring_feil(con, kid, for_lav, "person")
        assert feil and "for lav" in feil and "1 deltaker har" in feil
    for greit in (601, 700, 1500):
        assert db.prisendring_feil(con, kid, greit, "person") is None
    assert db.prisendring_feil(con, kid, 0, "person") is None                                 # gratis: ingen flere fakturaer
    assert db.prisendring_feil(con, kid, 500, "ingen") is None and db.prisendring_feil(con, kid, 500, "organisasjon") is None


def test_for_lav_pris_avvises_i_skjemaet_og_ingenting_lagres(con):
    kid = _kurs_med_delfaktura(con)
    side = _lagre(_klient(), kid, navn="Skal ikke lagres", pris_nok="500", bekreft_okonomi="1")
    assert "Prisen er for lav" in side
    assert _kurs_rad(con, kid)[0] == 1000
    assert _fersk(con).execute("SELECT navn FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "Testkurs"
    _lagre(_klient(), kid, pris_nok="700", bekreft_okonomi="1")                               # 100 kr gjenstår: greit
    assert _kurs_rad(con, kid)[0] == 700


def test_for_lav_pris_avvises_ogsaa_paa_db_nivaa_foer_noe_skrives(con):
    kid = _kurs_med_delfaktura(con)
    with pytest.raises(db.Paameldingsfeil, match="for lav"):
        db.oppdater_kurs_felter(con, kid, {"pris_nok": 500, "notat": "skal ikke lagres"}, tillat_okonomi=True)
    r = con.execute("SELECT pris_nok, notat FROM kurs WHERE id=?", (kid,)).fetchone()
    assert (r["pris_nok"], r["notat"]) == (1000, None) and _hendelser(con, "kurs_okonomi_endret") == []
