"""Ekstradeltaker som settes tilbake til Påmeldt: ingen faktura eller delfaktura lages av seg selv (brukerens beslutning 01.10.2026).

Samme regel for én faktura og for flere delfakturaer, også når deltakeren alt har fått noen: bare de som gjenstår, holdes tilbake, og
administrator må aktivt velge «Behandle fakturering nå». De fakturaene som er laget, står og krediteres ikke. Er alt fakturert fra før,
endres ingenting. Bare oppdiktede data."""
import re
from datetime import date

import pytest
from ekstrahjelp import S, antall, ekstra, kurs_med_samlinger, meld, rad, sett, synlig_tekst, typer
from kurssidehjelp import admin_klient, fast_dato, ny_database

from kurs import daglig, sveiper
from kurs.kjoring import Kjoring


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    fast_dato(monkeypatch)
    yield c
    c.close()


def _faktura(con, pid: int) -> int:
    return antall(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", pid)


def _flash(html: str) -> list[str]:
    return [synlig_tekst(m) for m in re.findall(r'<div class="flash[^"]*"[^>]*>(.*?)</div>', html, re.S)]


def _morgen(con, dato: date, ganger: int = 2):
    for _ in range(ganger):
        daglig.kjor(Kjoring(con, idag=dato))
        con.commit()


def _per_samling_med_en_delfaktura(con, monkeypatch):
    """Eva er Påmeldt og har fått delfaktura 1 (samling 1 forfalt 05.10.2031); samling 2 gjenstår. Returnerer (kurs, Eva)."""
    fast_dato(monkeypatch, date(2031, 10, 5))
    kid = kurs_med_samlinger(con, betaling="per_samling", faktura_dager_for=14)
    eva = meld(con, kid, "Eva")
    _morgen(con, date(2031, 10, 5), 1)
    assert _faktura(con, eva) == 1 and rad(con, eva)["sveiper_kjort"] == 1 and typer(con, "Eva") == ["bekreftelse"]
    return kid, eva


def test_resterende_delfakturaer_holdes_tilbake_naar_en_ekstradeltaker_settes_tilbake_til_paameldt(con, monkeypatch):
    kid, eva = _per_samling_med_en_delfaktura(con, monkeypatch)
    assert sett(con, eva, "ekstradeltaker") is None
    assert (rad(con, eva)["sveiper_kjort"], rad(con, eva)["sveiper_utsatt"]) == (1, 0)                # alt behandlet: ingenting endres
    assert sett(con, eva, "paameldt") is None
    r = rad(con, eva)
    assert (r["sveiper_kjort"], r["sveiper_utsatt"], r["ekstradeltaker_ts"]) == (0, 1, None)           # delfaktura 2 gjenstår: holdt tilbake
    fast_dato(monkeypatch, date(2031, 11, 5))                                                          # samling 2 er nå forfalt
    _morgen(con, date(2031, 11, 5))                                                                    # morgenjobben to ganger
    assert _faktura(con, eva) == 1 and typer(con, "Eva") == ["bekreftelse"]                           # ingenting automatisk


def test_administrator_velger_selv_og_da_lages_bare_det_som_gjenstaar(con, monkeypatch):
    kid, eva = _per_samling_med_en_delfaktura(con, monkeypatch)
    sett(con, eva, "ekstradeltaker")
    sett(con, eva, "paameldt")
    fast_dato(monkeypatch, date(2031, 11, 5))
    k = admin_klient()
    vindu = k.get(f"/admin/kurs/{kid}/deltaker/{eva}").get_data(as_text=True)
    t = synlig_tekst(vindu)
    assert "Behandling holdt tilbake" in t and "Behandle fakturering nå" in t
    assert "resten av fakturaene (delfakturaene) lager systemet ikke av seg selv" in t and "Systemet har ingen faktura" not in t
    r = k.post(f"/admin/kurs/{kid}/deltaker/{eva}/behandle")
    assert r.status_code == 302
    con.commit()
    assert _faktura(con, eva) == 2 and typer(con, "Eva") == ["bekreftelse"]                           # delfaktura 2; bekreftelsen ikke på nytt
    assert (rad(con, eva)["sveiper_kjort"], rad(con, eva)["sveiper_utsatt"]) == (1, 0)                # holdet er opphevet
    _morgen(con, date(2031, 11, 5))
    assert _faktura(con, eva) == 2                                                                     # idempotent


def test_meldingen_etter_retur_sier_at_resten_ikke_lages_av_seg_selv(con, monkeypatch):
    kid, eva = _per_samling_med_en_delfaktura(con, monkeypatch)
    sett(con, eva, "ekstradeltaker")
    k = admin_klient()
    r = k.post(f"/admin/kurs/{kid}/deltaker/{eva}/status", data={"status": "paameldt", "forventet": "ekstradeltaker"})
    meldinger = " ".join(_flash(k.get(r.headers["Location"]).get_data(as_text=True)))
    assert "fakturaene som er laget, står og krediteres ikke" in meldinger and "Resten av fakturaene" in meldinger
    assert "lages ikke av seg selv" in meldinger and "Behandle fakturering nå" in meldinger
    assert "systemet har ingen faktura til deltakeren" not in meldinger
    con.commit()
    assert _faktura(con, eva) == 1


def test_dialogen_forklarer_regelen_for_delfakturaer_foer_statusen_endres(con, monkeypatch):
    kid, eva = _per_samling_med_en_delfaktura(con, monkeypatch)
    sett(con, eva, "ekstradeltaker")
    k = admin_klient()
    liste = k.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    assert "Systemet lager IKKE resten av fakturaene (delfakturaene) av seg selv" in liste
    assert "fakturaene som er laget, endres ikke og krediteres ikke" in liste
    vindu = k.get(f"/admin/kurs/{kid}/deltaker/{eva}").get_data(as_text=True)
    assert "Systemet lager IKKE resten av fakturaene (delfakturaene) av seg selv" in vindu


def test_er_alle_delfakturaene_laget_endres_ingenting_ved_retur(con, monkeypatch):
    fast_dato(monkeypatch, date(2031, 12, 3))
    kid = kurs_med_samlinger(con, betaling="per_samling", faktura_dager_for=60)                       # alle tre samlinger forfalt 03.12.2031
    eva = meld(con, kid, "Eva")
    _morgen(con, date(2031, 12, 3), 1)
    assert _faktura(con, eva) == 3 and rad(con, eva)["sveiper_kjort"] == 1
    sett(con, eva, "ekstradeltaker")
    sett(con, eva, "paameldt")
    assert (rad(con, eva)["sveiper_kjort"], rad(con, eva)["sveiper_utsatt"]) == (1, 0)                # ingenting å holde tilbake
    _morgen(con, date(2031, 12, 3))
    assert _faktura(con, eva) == 3 and typer(con, "Eva").count("bekreftelse") == 1          # påminnelsen dagen før kursslutt er en annen sak
    assert "Behandling holdt tilbake" not in synlig_tekst(admin_klient().get(f"/admin/kurs/{kid}/deltaker/{eva}").get_data(as_text=True))


def test_samlet_faktura_som_ikke_er_laget_holdes_ogsaa_tilbake(con, monkeypatch):
    """Samme regel for én faktura: et kurs om mer enn seks måneder (faktura planlagt, ikke laget), ekstradeltaker tilbake til Påmeldt."""
    fast_dato(monkeypatch, date(2031, 10, 1))
    kid = kurs_med_samlinger(con, samlinger=[S(date(2032, 6, 1), date(2032, 6, 2), "09:00", "16:00", 6.0)], betaling="samlet")
    eva = meld(con, kid, "Eva")
    _morgen(con, date(2031, 10, 1), 1)
    assert _faktura(con, eva) == 0 and rad(con, eva)["faktura_tidligst_dato"] and rad(con, eva)["sveiper_kjort"] == 1
    sett(con, eva, "ekstradeltaker")
    sett(con, eva, "paameldt")
    assert (rad(con, eva)["sveiper_kjort"], rad(con, eva)["sveiper_utsatt"]) == (0, 1)
    sveiper.utsatte_fakturaer(Kjoring(con, idag=date(2031, 12, 5)))                                   # tidligst-datoen er nådd
    con.commit()
    _morgen(con, date(2031, 12, 5))
    assert _faktura(con, eva) == 0                                                                     # ingenting automatisk
    k = admin_klient()
    fast_dato(monkeypatch, date(2031, 12, 5))
    k.post(f"/admin/kurs/{kid}/deltaker/{eva}/behandle")
    con.commit()
    assert _faktura(con, eva) == 1                                                                     # administrator valgte


def test_ekstradeltaker_som_aldri_er_behandlet_gaar_som_for_alle_andre(con, monkeypatch):
    """Uendret: er bekreftelsen ikke behandlet, oppheves holdet ved retur og vanlige regler gjelder (ingenting er laget fra før)."""
    kid = kurs_med_samlinger(con, betaling="per_samling", faktura_dager_for=14)
    eva = ekstra(con, kid, "Eva")
    assert sett(con, eva, "paameldt") == eva
    assert (rad(con, eva)["sveiper_kjort"], rad(con, eva)["sveiper_utsatt"]) == (0, 0)
