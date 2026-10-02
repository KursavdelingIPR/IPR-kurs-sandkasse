"""Ekstradeltakere: faktura og bekreftelse holdes tilbake (E5), skjermbildene for samlingsvalg (E6) og tellingen på alle sider (E1).

En ekstradeltaker faktureres ikke automatisk og får ikke automatisk bekreftelse (prisen er ikke kursets standardpris): påmeldingen holdes
tilbake som «Behandling holdt tilbake», og administrator sender manuelt ved behov. Samlingsvalget gjøres i et eget steg fra
statusvelgeren, i deltakervinduet og i «Legg til deltaker». Tallene viser påmeldte og ekstradeltakere hver for seg.
Bare oppdiktede data.
"""
import csv
import io
import re
from datetime import date

import pytest
from adressehjelp import ADRESSE
from ekstrahjelp import (ADMIN, DAGER, S, antall, dager_for, ekstra, epost_til, kurs_med_samlinger, meld, rad, samling_ider, sett,
                         status, synlig_tekst, typer, utvalg_rader)
from kurssidehjelp import admin_klient, deltaker_klient, fast_dato, ny_database

from kurs import behandling, daglig, db, kurskalender, sveiper
from kurs.kjoring import Kjoring


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    fast_dato(monkeypatch)
    yield c
    c.close()


# ============================ E5: faktura og bekreftelse holdes tilbake ============================

def test_paameldt_som_ikke_er_behandlet_holdes_tilbake_naar_den_blir_ekstradeltaker_og_slippes_ved_retur(con):
    kid = kurs_med_samlinger(con, betaling="samlet")
    pia = meld(con, kid, "Pia")
    assert (rad(con, pia)["sveiper_kjort"], rad(con, pia)["sveiper_utsatt"]) == (0, 0)
    assert sett(con, pia, "ekstradeltaker") is None
    assert (rad(con, pia)["sveiper_kjort"], rad(con, pia)["sveiper_utsatt"]) == (0, 1)                      # holdt tilbake
    sveiper.kjor(Kjoring(con, idag=date(2031, 10, 1)))
    con.commit()
    assert typer(con, "Pia") == [] and antall(con, "SELECT COUNT(*) FROM faktura") == 0                    # ingenting automatisk
    assert sett(con, pia, "paameldt") == pia                                                               # tilbake til Påmeldt: dagens regler
    assert (rad(con, pia)["sveiper_kjort"], rad(con, pia)["sveiper_utsatt"]) == (0, 0)
    sveiper.kjor(Kjoring(con, idag=date(2031, 10, 1)), pia)
    con.commit()
    assert typer(con, "Pia") == ["bekreftelse"] and antall(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", pia) == 1


def test_er_bekreftelsen_alt_behandlet_endres_ingenting_og_retur_til_paameldt_sender_ikke_paa_nytt(con):
    kid = kurs_med_samlinger(con, betaling="samlet")
    pia = meld(con, kid, "Pia")
    sveiper.kjor(Kjoring(con, idag=date(2031, 10, 1)))
    con.commit()
    assert typer(con, "Pia") == ["bekreftelse"] and rad(con, pia)["sveiper_kjort"] == 1
    assert sett(con, pia, "ekstradeltaker") is None
    assert (rad(con, pia)["sveiper_kjort"], rad(con, pia)["sveiper_utsatt"]) == (1, 0)                      # ingen hold, ingen endring
    assert sett(con, pia, "paameldt") is None                                                              # alt er behandlet: ingenting å kjøre
    assert rad(con, pia)["sveiper_kjort"] == 1
    sveiper.kjor(Kjoring(con, idag=date(2031, 10, 1)), pia)
    daglig.kjor(Kjoring(con, idag=date(2031, 10, 1)))
    con.commit()
    assert typer(con, "Pia") == ["bekreftelse"] and antall(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", pia) == 1   # ingen ny


def test_delfakturaer_for_kommende_samlinger_lages_ikke_for_en_ekstradeltaker_men_det_som_er_sendt_staar(con):
    kid = kurs_med_samlinger(con, betaling="per_samling", faktura_dager_for=14)
    ola, eva = meld(con, kid, "Ola"), meld(con, kid, "Eva")
    daglig.kjor(Kjoring(con, idag=date(2031, 10, 5)))                                                      # samling 1 forfalt: én delfaktura hver
    con.commit()
    assert [antall(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", p) for p in (ola, eva)] == [1, 1]
    sett(con, eva, "ekstradeltaker")                                                                       # allerede behandlet: ingenting endres
    assert antall(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", eva) == 1                     # og ingen kreditering
    sveiper.forfalte_delfakturaer(Kjoring(con, idag=date(2031, 11, 5)))                                    # samling 2 forfalt
    con.commit()
    assert antall(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", ola) == 2                     # Påmeldt: som før
    assert antall(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", eva) == 1                     # ekstradeltaker: ingen flere automatisk
    sveiper.forfalte_delfakturaer(Kjoring(con, idag=date(2031, 11, 5)))                                    # idempotent
    con.commit()
    assert [antall(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", p) for p in (ola, eva)] == [2, 1]


def test_utsatt_samlet_faktura_lages_ikke_for_en_ekstradeltaker(con):
    kid = kurs_med_samlinger(con, samlinger=[S(date(2032, 6, 1), date(2032, 6, 2), "09:00", "16:00", 6.0)])
    ola, eva = meld(con, kid, "Ola"), meld(con, kid, "Eva")
    daglig.kjor(Kjoring(con, idag=date(2031, 10, 1)))                                                      # mer enn seks måneder før: utsatt
    con.commit()
    assert antall(con, "SELECT COUNT(*) FROM faktura") == 0 and all(rad(con, p)["faktura_tidligst_dato"] for p in (ola, eva))
    sett(con, eva, "ekstradeltaker")
    sveiper.utsatte_fakturaer(Kjoring(con, idag=date(2031, 12, 5)))                                        # tidligst-datoen er nådd
    con.commit()
    assert antall(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", ola) == 1
    assert antall(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", eva) == 0


def test_sveiperen_plukker_aldri_opp_en_ekstradeltaker_selv_om_holdet_mangler(con):
    """Sikring: selv om sveiper_utsatt skulle stå på 0, får en ekstradeltaker ingen automatisk bekreftelse eller faktura."""
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva")
    con.execute("UPDATE paamelding SET sveiper_utsatt=0, sveiper_kjort=0 WHERE id=?", (eva,))
    con.commit()
    sveiper.kjor(Kjoring(con, idag=date(2031, 10, 1)))
    daglig.kjor(Kjoring(con, idag=date(2031, 10, 1)))
    con.commit()
    assert "bekreftelse" not in typer(con, "Eva") and antall(con, "SELECT COUNT(*) FROM faktura") == 0


def test_faktura_plan_og_skal_faktureres_kjenner_ekstradeltaker():
    plan = sveiper.faktura_plan(fakturering="person", pris_nok=2500, status="bekreftet", betaling="samlet", faktura_onskes_na=1,
                                kursdager=["2031-10-16"], idag=date(2031, 10, 1))
    assert plan.modus == sveiper.PLAN_NA
    # planen som sveiperen bygger for en påmelding (_lag_plan): ingen automatisk faktura for en ekstradeltaker, uansett betalingsmåte
    rad_ = {"kurs_id": 1, "fakturering": "person", "pris_nok": 2500, "status": "bekreftet", "betaling": "per_samling", "faktura_onskes_na": 1,
            "ekstradeltaker_ts": "2031-10-01 10:00:00"}
    dager_ = [{"dato": "2031-10-16"}]
    assert sveiper._lag_plan(Kjoring(None, idag=date(2031, 10, 1)), rad_, dager_).modus == sveiper.PLAN_INGEN
    assert sveiper._lag_plan(Kjoring(None, idag=date(2031, 10, 1)), {**rad_, "ekstradeltaker_ts": None}, dager_).modus == sveiper.PLAN_PER_SAMLING
    assert sveiper.skal_faktureres({"fakturering": "person", "pris_nok": 1, "status": "bekreftet"})
    assert not sveiper.skal_faktureres({"fakturering": "person", "pris_nok": 1, "status": "bekreftet", "ekstradeltaker_ts": "2031-10-01 10:00:00"})


def test_manuell_behandling_i_deltakervinduet_for_en_ekstradeltaker(con):
    kid = kurs_med_samlinger(con)
    ola = meld(con, kid, "Ola", paamelding={"sveiper_utsatt": 1})                                          # holdt tilbake som en manuell registrering
    eva = ekstra(con, kid, "Eva", nr=[1, 3])
    k = admin_klient()
    html = k.get(f"/admin/kurs/{kid}/deltaker/{eva}").get_data(as_text=True)
    assert "Behandling holdt tilbake" in html and "Ekstradeltaker: ingen automatisk bekreftelse eller faktura. Send manuelt ved behov." in html
    assert "Send bekreftelse nå" in html and "behandle fakturering" not in html
    assert "samling 1, 3" in html                                                                          # hva bekreftelsen viser
    assert "lager du selv, utenfor systemet" in html                                                       # ingen fakturafunksjon: sier det rett ut
    ola_html = k.get(f"/admin/kurs/{kid}/deltaker/{ola}").get_data(as_text=True)
    ola_boks = re.search(r'<div class="varselboks">.*?</div>', ola_html, re.S).group(0)                    # boksen (dialogtekstene ligger også i vinduet)
    assert "Send bekreftelse og behandle fakturering nå" in ola_boks and "Ekstradeltaker: ingen automatisk" not in ola_boks
    r = k.post(f"/admin/kurs/{kid}/deltaker/{eva}/behandle")
    assert r.status_code == 302
    melding = k.get(r.headers["Location"]).get_data(as_text=True)
    assert ("Bekreftelsen er sendt, og den viser bare samlingene deltakeren deltar på. Ekstradeltakere faktureres ikke automatisk: "
            "fakturaen lager du selv, utenfor systemet.") in synlig_tekst(melding)
    assert typer(con, "Eva") == ["bekreftelse"] and antall(con, "SELECT COUNT(*) FROM faktura") == 0
    assert "Behandling holdt tilbake" not in k.get(f"/admin/kurs/{kid}/deltaker/{eva}").get_data(as_text=True)     # ikke lenger holdt
    assert behandling.behandle_holdt_paamelding(con, kid, eva, date(2031, 10, 1)).kode == behandling.ALLEREDE_BEHANDLET


# ============================ E6: samlingsvalg i listen, vinduet og «Legg til deltaker» ============================

def _post(k, kid, pid, status_, **data):
    return k.post(f"/admin/kurs/{kid}/deltaker/{pid}/status", data={"status": status_, **data})


def _scenario(con):
    kid = kurs_med_samlinger(con)
    return kid, meld(con, kid, "Ola"), samling_ider(con, kid)


def test_statusvelgeren_i_listen_sender_til_samlingsvalget_naar_kurset_har_flere_samlinger(con):
    kid, ola, (s1, s2, s3) = _scenario(con)
    k = admin_klient()
    liste = k.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    assert "data-ekstra-steg" in liste                                                                     # JavaScript hopper over dialogen
    neste = f"/admin/kurs/{kid}/deltakere?status=paameldt"
    r = _post(k, kid, ola, "ekstradeltaker", neste=neste, forventet="paameldt")
    assert r.status_code == 302 and r.headers["Location"].startswith(f"/admin/kurs/{kid}/deltaker/{ola}/ekstradeltaker")
    assert status(con, ola) == "paameldt" and utvalg_rader(con, ola) == []                                   # ingenting er endret ennå
    side = k.get(r.headers["Location"])
    assert side.status_code == 200
    html = side.get_data(as_text=True)
    assert "Sett Ola Test til ekstradeltaker" in html and "Ekstradeltaker: ingen automatisk bekreftelse eller faktura" in html
    assert re.search(r'name="utvalg" value="hele" checked', html) and 'value="noen" checked' not in html      # forhåndsvalg: Hele kurset
    for sid, tittel in zip((s1, s2, s3), ("Samling 1", "Samling 2", "Samling 3")):
        assert re.search(rf'name="samling" value="{sid}"\s*>', html) and tittel in html
    assert "16.–17.10.2031" in html and "04.12.2031" in html and "2 dager" in html
    assert 'name="forventet" value="paameldt"' in html and 'name="samlingsvalg" value="1"' in html
    assert f'name="neste" value="{neste}"' in html and 'name="csrf_token"' in html
    assert '<fieldset class="samlingsvalg"' in html and "<legend" in html and "Hele kurset" in html          # tilgjengelig: fieldset/legend/label
    assert "Avbryt" in html and f'href="{neste}#rad-{ola}"' in html.replace("&amp;", "&")


def test_samlingsvalget_gir_utvalg_hele_kurset_eller_avvises(con):
    kid, ola, (s1, s2, s3) = _scenario(con)
    k = admin_klient()
    r = _post(k, kid, ola, "ekstradeltaker", samlingsvalg="1", utvalg="noen", samling=[s1, s3], forventet="paameldt")
    assert r.status_code == 302 and status(con, ola) == "ekstradeltaker" and utvalg_rader(con, ola) == sorted([s1, s3])
    assert dager_for(con, ola) == DAGER[1] + DAGER[3]
    melding = synlig_tekst(k.get(r.headers["Location"]).get_data(as_text=True))
    assert "Deltar på samling 1 og 3" in melding and "teller ikke som påmeldt" in melding
    # «Hele kurset» uten avkrysninger: ingen rader
    pia = meld(con, kid, "Pia")
    assert _post(k, kid, pia, "ekstradeltaker", samlingsvalg="1", utvalg="hele").status_code == 302
    assert status(con, pia) == "ekstradeltaker" and utvalg_rader(con, pia) == []
    # «Hele kurset» MED en avkrysset samling er motstridende (uten JavaScript flytter ikke valget seg): avvises, aldri tolkes i det stille
    tor = meld(con, kid, "Tor")
    r = _post(k, kid, tor, "ekstradeltaker", samlingsvalg="1", utvalg="hele", samling=[s2], forventet="paameldt")
    assert r.status_code == 302 and r.headers["Location"].startswith(f"/admin/kurs/{kid}/deltaker/{tor}/ekstradeltaker")
    assert status(con, tor) == "paameldt" and utvalg_rader(con, tor) == []                                     # ingenting er endret
    assert "Du har krysset av samlinger, men «Hele kurset» er valgt" in synlig_tekst(k.get(r.headers["Location"]).get_data(as_text=True))
    # alle samlinger avkrysset = hele kurset
    nina = meld(con, kid, "Nina")
    _post(k, kid, nina, "ekstradeltaker", samlingsvalg="1", utvalg="noen", samling=[s1, s2, s3])
    assert status(con, nina) == "ekstradeltaker" and utvalg_rader(con, nina) == []
    # «Bare utvalgte samlinger» uten noen avkrysset: tilbake til steget, ingenting endres
    una = meld(con, kid, "Una")
    r = _post(k, kid, una, "ekstradeltaker", samlingsvalg="1", utvalg="noen", forventet="paameldt")
    assert r.status_code == 302 and r.headers["Location"].startswith(f"/admin/kurs/{kid}/deltaker/{una}/ekstradeltaker")
    assert status(con, una) == "paameldt" and "Velg minst én samling" in synlig_tekst(k.get(r.headers["Location"]).get_data(as_text=True))


def test_samlingsvalget_avviser_samlinger_fra_et_annet_kurs_og_foreldet_status(con):
    kid, ola, _ = _scenario(con)
    annet = kurs_med_samlinger(con, "ANNET")
    fremmed = samling_ider(con, annet)[0]
    k = admin_klient()
    r = _post(k, kid, ola, "ekstradeltaker", samlingsvalg="1", utvalg="noen", samling=[fremmed], forventet="paameldt")
    assert r.status_code == 302 and status(con, ola) == "paameldt" and utvalg_rader(con, ola) == []
    assert "Velg samlinger som hører til dette kurset" in synlig_tekst(k.get(r.headers["Location"]).get_data(as_text=True))
    r = _post(k, kid, ola, "ekstradeltaker", samlingsvalg="1", utvalg="hele", forventet="avmeldt")            # listen var foreldet
    assert status(con, ola) == "paameldt" and "Statusen er endret siden listen ble lastet" in k.get(r.headers["Location"]).get_data(as_text=True)


def test_samlingsvalget_gir_ingen_aapen_omdirigering_og_krever_riktig_kurs(con):
    kid, ola, _ = _scenario(con)
    annet = kurs_med_samlinger(con, "ANNET")
    k = admin_klient()
    for ond in ("https://evil.example/liste", "//evil.example/x", "/admin/kurs/999/deltakere", f"/admin/kurs/{annet}/deltakere", "javascript:alert(1)"):
        r = _post(k, kid, ola, "ekstradeltaker", neste=ond, forventet="paameldt")
        assert r.status_code == 302 and "evil" not in r.headers["Location"] and r.headers["Location"].startswith(f"/admin/kurs/{kid}/deltaker/{ola}/ekstradeltaker")
        side = k.get(f"/admin/kurs/{kid}/deltaker/{ola}/ekstradeltaker", query_string={"neste": ond}).get_data(as_text=True)
        assert "evil.example" not in side and 'name="neste" value=""' in side and f'href="/admin/kurs/{kid}/deltaker/{ola}"' in side
    assert k.get(f"/admin/kurs/{annet}/deltaker/{ola}/ekstradeltaker").status_code == 404                     # påmeldingen hører ikke til kurset
    assert k.get(f"/admin/kurs/{kid}/deltaker/999999/ekstradeltaker").status_code == 404
    assert status(con, ola) == "paameldt"


def test_samlingsvalget_for_roller_og_csrf(con):
    kid, ola, (s1, _s2, _s3) = _scenario(con)
    lese = admin_klient(con, "lese")
    assert _post(lese, kid, ola, "ekstradeltaker", samlingsvalg="1", utvalg="hele").status_code == 403
    side = lese.get(f"/admin/kurs/{kid}/deltaker/{ola}/ekstradeltaker")                                       # lesetilgang kan se steget ...
    assert side.status_code == 200 and "<form" not in side.get_data(as_text=True).split("<main", 1)[-1].split("</main>")[0].replace('<form method="get"', "")
    assert "lesetilgang" in side.get_data(as_text=True)                                                       # ... men ikke bekrefte det
    assert status(con, ola) == "paameldt"
    k = admin_klient()
    k.injiser_csrf = False
    assert _post(k, kid, ola, "ekstradeltaker", samlingsvalg="1", utvalg="noen", samling=[s1]).status_code == 400
    assert status(con, ola) == "paameldt"
    assert deltaker_klient().get(f"/admin/kurs/{kid}/deltaker/{ola}/ekstradeltaker").status_code in (302, 401, 403)


def test_kurs_med_en_samling_har_ikke_noe_aa_velge_og_ekstradeltaker_blir_satt_direkte(con):
    kid = kurs_med_samlinger(con, "EN", samlinger=[S(date(2031, 10, 16), date(2031, 10, 17), "09:00", "16:00", 6.0)])
    ola = meld(con, kid, "Ola")
    k = admin_klient()
    assert "data-ekstra-steg" not in k.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    r = _post(k, kid, ola, "ekstradeltaker", forventet="paameldt")
    assert r.status_code == 302 and "/ekstradeltaker" not in r.headers["Location"]
    assert status(con, ola) == "ekstradeltaker" and utvalg_rader(con, ola) == []
    vindu = k.get(f"/admin/kurs/{kid}/deltaker/{ola}").get_data(as_text=True)
    assert "Ekstradeltaker: deltar på" in vindu and "Kurset har bare én samling" in vindu and 'name="samling"' not in vindu


def test_listen_viser_hvilke_samlinger_en_ekstradeltaker_er_paa_under_statusmerket(con):
    kid = kurs_med_samlinger(con)
    meld(con, kid, "Ola")
    ekstra(con, kid, "Eva", nr=[1, 3])
    ekstra(con, kid, "Una")
    html = admin_klient().get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    celler = {navn: re.search(rf'<td class="statuscelle">.*?</td>', blokk, re.S).group(0)
              for navn, blokk in zip(("Ola", "Eva", "Una"), re.findall(r'<tr data-filtrer="deltaker".*?</tr>', html, re.S))}
    assert "utvalg-tekst" not in celler["Ola"]                                                               # Påmeldt: ingenting under merket
    assert 'utvalg-tekst" title="Samlingene ekstradeltakeren deltar på">Samling 1, 3<' in celler["Eva"]
    assert 'utvalg-tekst" title="Samlingene ekstradeltakeren deltar på">Hele kurset<' in celler["Una"]
    assert 'data-filtrer="deltaker" data-status="ekstradeltaker"' in html                                     # filterbrikken er uendret


def test_deltakervinduet_har_seksjon_for_ekstradeltaker_med_lagring(con):
    kid, ola, (s1, s2, s3) = _scenario(con)
    eva = ekstra(con, kid, "Eva", nr=[1])
    k = admin_klient()
    assert "Ekstradeltaker: deltar på" not in k.get(f"/admin/kurs/{kid}/deltaker/{ola}").get_data(as_text=True)   # bare for ekstradeltakere
    html = k.get(f"/admin/kurs/{kid}/deltaker/{eva}").get_data(as_text=True)
    assert "Ekstradeltaker: deltar på" in html and '<span class="merke">Samling 1</span>' in html
    assert re.search(rf'name="utvalg" value="noen" checked', html) and re.search(rf'name="samling" value="{s1}" checked', html)
    assert f'/deltaker/{eva}/samlinger' in html and "Lagre samlinger" in html and "data-ekstra-steg" in html
    r = k.post(f"/admin/kurs/{kid}/deltaker/{eva}/samlinger", data={"utvalg": "noen", "samling": [s2, s3]})
    assert r.status_code == 302 and utvalg_rader(con, eva) == sorted([s2, s3]) and dager_for(con, eva) == DAGER[2] + DAGER[3]
    assert "Deltar på samling 2 og 3" in synlig_tekst(k.get(r.headers["Location"]).get_data(as_text=True))
    k.post(f"/admin/kurs/{kid}/deltaker/{eva}/samlinger", data={"utvalg": "hele"})
    assert utvalg_rader(con, eva) == [] and dager_for(con, eva) == sum(DAGER.values(), [])
    k.post(f"/admin/kurs/{kid}/deltaker/{eva}/samlinger", data={"utvalg": "noen"})                             # ingen valgt: avvises
    assert utvalg_rader(con, eva) == []
    r = k.post(f"/admin/kurs/{kid}/deltaker/{ola}/samlinger", data={"utvalg": "noen", "samling": [s1]})       # Ola er ikke ekstradeltaker
    assert utvalg_rader(con, ola) == [] and "Bare en ekstradeltaker kan velge samlinger" in synlig_tekst(k.get(r.headers["Location"]).get_data(as_text=True))
    hendelser = con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='ekstradeltaker_utvalg'").fetchone()[0]
    assert hendelser == 2                                                                                    # {1} -> {2,3} -> hele (statusendringen logges som status_endret)


def test_samlingslagringen_avviser_lesetilgang_csrf_og_ukjente_samlinger(con):
    kid, ola, (s1, _s2, _s3) = _scenario(con)
    eva = ekstra(con, kid, "Eva", nr=[1])
    annet = kurs_med_samlinger(con, "ANNET")
    lese = admin_klient(con, "lese")
    assert lese.post(f"/admin/kurs/{kid}/deltaker/{eva}/samlinger", data={"utvalg": "hele"}).status_code == 403
    assert "Lagre samlinger" not in lese.get(f"/admin/kurs/{kid}/deltaker/{eva}").get_data(as_text=True)      # ingen lagreknapp for lesetilgang
    k = admin_klient()
    r = k.post(f"/admin/kurs/{kid}/deltaker/{eva}/samlinger", data={"utvalg": "noen", "samling": [samling_ider(con, annet)[0]]})
    assert r.status_code == 302 and utvalg_rader(con, eva) == [s1]                                            # fremmed samling: uendret
    k.injiser_csrf = False
    assert k.post(f"/admin/kurs/{kid}/deltaker/{eva}/samlinger", data={"utvalg": "hele"}).status_code == 400
    assert utvalg_rader(con, eva) == [s1]
    assert admin_klient(con, "kursadmin").post(f"/admin/kurs/{kid}/deltaker/{eva}/samlinger", data={"utvalg": "hele"}).status_code == 302
    assert utvalg_rader(con, eva) == []


# ---------- Legg til deltaker ----------

def _ny(k, kid, fornavn="Mia", **ekstra_):
    return k.post(f"/admin/kurs/{kid}/deltaker/ny", follow_redirects=False, data={
        "fornavn": fornavn, "etternavn": "Test", "epost": epost_til(fornavn), "betaler": "person", "betaling": "samlet",
        **ADRESSE, **ekstra_})


def _pid(con, fornavn):
    return con.execute("SELECT p.id FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE d.epost=?", (epost_til(fornavn),)).fetchone()[0]


def test_legg_til_deltaker_som_ekstradeltaker_paa_et_fullt_kurs(con):
    kid = kurs_med_samlinger(con, kapasitet=1)
    ola = meld(con, kid, "Ola")                                                                              # kurset er fullt
    s1, s2, s3 = samling_ider(con, kid)
    k = admin_klient()
    skjema = k.get(f"/admin/kurs/{kid}/deltaker/ny").get_data(as_text=True)
    assert 'name="ekstradeltaker" value="1"' in skjema and "Registrer som ekstradeltaker" in skjema and "teller ikke som fullverdig deltaker" in skjema
    assert 'name="samling"' in skjema and "Kurset er fullt – deltakeren settes på venteliste (bortsett fra ekstradeltakere)" in skjema
    r = _ny(k, kid, "Mia", ekstradeltaker="1", utvalg="noen", samling=[s2, s3])
    assert r.status_code == 302
    mia = _pid(con, "Mia")
    assert status(con, mia) == "ekstradeltaker" and utvalg_rader(con, mia) == sorted([s2, s3])                # ikke venteliste, selv om kurset er fullt
    assert (rad(con, mia)["sveiper_utsatt"], rad(con, mia)["sveiper_kjort"]) == (1, 0)                       # holdt tilbake: bekreftelse og faktura manuelt
    assert db.antall_bekreftet(con, kid) == 1 and status(con, ola) == "paameldt"
    tekst = synlig_tekst(k.get(r.headers["Location"]).get_data(as_text=True))
    assert "registrert som «ekstradeltaker» og deltar på samling 2 og 3" in tekst and "Ingen bekreftelse eller faktura er sendt" in tekst
    daglig.kjor(Kjoring(con, idag=date(2031, 10, 1)))
    con.commit()
    assert "bekreftelse" not in typer(con, "Mia") and antall(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", mia) == 0
    # uten avkryssingen: som før (venteliste på et fullt kurs)
    assert _ny(k, kid, "Pia").status_code == 302 and status(con, _pid(con, "Pia")) == "venteliste"
    # hele kurset (standard): ingen rader
    assert _ny(k, kid, "Una", ekstradeltaker="1").status_code == 302
    assert status(con, _pid(con, "Una")) == "ekstradeltaker" and utvalg_rader(con, _pid(con, "Una")) == []


def test_legg_til_deltaker_som_ekstradeltaker_kontrolleres_foer_noe_lagres(con):
    kid = kurs_med_samlinger(con)
    annet = kurs_med_samlinger(con, "ANNET")
    k = admin_klient()
    r = _ny(k, kid, "Mia", ekstradeltaker="1", utvalg="noen")                                                # ingen samling valgt
    assert r.status_code == 400 and "Velg minst én samling" in synlig_tekst(r.get_data(as_text=True))
    assert 'name="ekstradeltaker" value="1" checked' in r.get_data(as_text=True) or "checked" in r.get_data(as_text=True)
    r = _ny(k, kid, "Mia", ekstradeltaker="1", utvalg="noen", samling=[samling_ider(con, annet)[0]])          # fremmed samling
    assert r.status_code == 400 and "Velg samlinger som hører til dette kurset" in synlig_tekst(r.get_data(as_text=True))
    ingen_adresse = k.post(f"/admin/kurs/{kid}/deltaker/ny", data={"fornavn": "Mia", "etternavn": "Test", "epost": epost_til("Mia"),
                                                                     "betaler": "person", "ekstradeltaker": "1"})
    assert ingen_adresse.status_code == 400                                                                   # adressen er fortsatt påkrevd
    assert antall(con, "SELECT COUNT(*) FROM deltaker WHERE epost=?", epost_til("Mia")) == 0                 # ingenting ble lagret
    assert antall(con, "SELECT COUNT(*) FROM paamelding WHERE ekstradeltaker_ts IS NOT NULL") == 0


def test_legg_til_deltaker_som_ekstradeltaker_paa_avlyst_kurs_avvises_og_roller_og_csrf_gjelder(con):
    kid = kurs_med_samlinger(con)
    k = admin_klient()
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kid,))
    con.commit()
    assert _ny(k, kid, "Mia", ekstradeltaker="1").status_code == 400
    con.execute("UPDATE kurs SET status='aapen' WHERE id=?", (kid,))
    con.commit()
    assert _ny(admin_klient(con, "lese"), kid, "Mia", ekstradeltaker="1").status_code == 403
    k.injiser_csrf = False
    assert _ny(k, kid, "Mia", ekstradeltaker="1").status_code == 400
    assert antall(con, "SELECT COUNT(*) FROM paamelding") == 0


# ============================ E1: tellingen på alle sider ============================

def _kurs_med_fire(con, kapasitet=3):
    """Kapasitet 3: Ola og Pia er påmeldte, Eva og Una er ekstradeltakere. To påmeldte + to ekstradeltakere."""
    kid = kurs_med_samlinger(con, kapasitet=kapasitet)
    meld(con, kid, "Ola"), meld(con, kid, "Pia")
    ekstra(con, kid, "Eva", nr=[1]), ekstra(con, kid, "Una")
    return kid


def test_oversikt_og_aktiviteter_viser_paameldte_og_ekstradeltakere_hver_for_seg(con):
    _kurs_med_fire(con)
    k = admin_klient()
    oversikt = synlig_tekst(k.get("/admin").get_data(as_text=True))
    assert "2 / 3 + 2 ekstradeltakere" in oversikt                                                            # tall + ekstra ved siden av
    assert re.search(r"Påmeldte \(åpne kurs\) 2 ", oversikt) and re.search(r"Ekstradeltakere \(åpne kurs\) 2 ", oversikt)
    aktiviteter = synlig_tekst(k.get("/admin").get_data(as_text=True))
    assert "2 / 3 + 2 ekstradeltakere" in aktiviteter and "4 registrert" in aktiviteter                     # «registrert» teller alle rader
    ett = kurs_med_samlinger(con, "ETT")
    meld(con, ett, "Kari")
    ekstra(con, ett, "Tor")
    assert "1 / 10 + 1 ekstradeltaker" in synlig_tekst(k.get("/admin").get_data(as_text=True))                # entall


def test_kalenderen_og_rapportene_teller_bare_paameldte_og_viser_ekstradeltakere_ved_siden_av(con):
    kid = _kurs_med_fire(con)
    hendelser = kurskalender.hent(con, date(2031, 10, 1), date(2031, 12, 31))
    # Ekstradeltakerne PER SAMLING: Eva er bare på samling 1, Una på hele kurset. Påmeldte er de samme på alle samlingene.
    assert [(h.bekreftet, h.ekstradeltakere) for h in hendelser] == [(2, 2), (2, 1), (2, 1)]
    assert hendelser[0].paameldte_tekst == "2/3 påmeldte + 2 ekstradeltakere"
    assert hendelser[1].paameldte_tekst == "2/3 påmeldte + 1 ekstradeltaker"
    assert kurskalender.hent(con, date(2031, 10, 1), date(2031, 12, 31))[0].kapasitet == 3
    k = admin_klient()
    html = synlig_tekst(k.get("/admin/rapporter/kurs").get_data(as_text=True))
    assert "2 / 3" in html and "67%" in html                                                                  # fyllingsgrad uten ekstradeltakerne
    assert "er på kurset, men tar ikke plass og er ikke regnet med i påmeldt" in k.get("/admin/rapporter/kurs").get_data(as_text=True)
    rader = list(csv.reader(io.StringIO(k.get("/admin/rapporter/kurs.csv").get_data(as_text=True).lstrip("﻿")), delimiter=";"))
    hode, rad_ = rader[0], rader[1]
    assert "inngår i" not in ";".join(hode) and hode[6:10] == ["Påmeldt", "Kapasitet", "Fyllingsgrad (%)", "Ekstradeltaker"]
    assert rad_[6:10] == ["2", "3", "67", "2"]
    oversikt = synlig_tekst(k.get("/admin/rapporter").get_data(as_text=True))
    assert re.search(r"Påmeldte i perioden 2 \+ 2 ekstradeltakere", oversikt)
    assert kid


def test_deltakerlisten_toppen_og_tellerkortene_viser_tallene_hver_for_seg(con):
    kid = _kurs_med_fire(con)
    k = admin_klient()
    utskrift = synlig_tekst(k.get(f"/admin/kurs/{kid}/deltakerliste").get_data(as_text=True))
    assert "2 påmeldte + 2 ekstradeltakere" in utskrift
    alle = synlig_tekst(k.get(f"/admin/kurs/{kid}/deltakerliste?status=alle").get_data(as_text=True))
    assert "Alle: 4" in alle                                                                                  # andre valg: ett tall som før
    listen = k.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    assert re.search(r'Påmeldt</div><div class="stat">2</div>', listen) and re.search(r'Ekstradeltaker</div><div class="stat">2</div>', listen)
    assert "Totalt registrert</div><div class=\"stat\">4<" in listen


def test_plasser_fullt_og_overbooking_teller_bare_paameldte(con):
    kid = _kurs_med_fire(con, kapasitet=3)
    kode = con.execute("SELECT kode FROM kurs WHERE id=?", (kid,)).fetchone()[0]
    from kurs.web import app as webapp
    offentlig = webapp.app.test_client()
    assert "Fullt – du settes på venteliste" not in offentlig.get(f"/kurs/{kode}").get_data(as_text=True)   # 2 påmeldte på 3 plasser (+ 2 ekstra)
    ny = admin_klient().get(f"/admin/kurs/{kid}/deltaker/ny").get_data(as_text=True)
    assert "1 plasser igjen." in ny
    meld(con, kid, "Nina")                                                                                    # tredje påmeldte: nå er det fullt
    assert "Fullt – du settes på venteliste" in offentlig.get(f"/kurs/{kode}").get_data(as_text=True)
    assert "Kurset er fullt" in synlig_tekst(admin_klient().get(f"/admin/kurs/{kid}/deltaker/ny").get_data(as_text=True))
    assert status(con, meld(con, kid, "Tor")) == "venteliste"                                                 # ekstradeltakerne fylte ikke plassene
    assert db.antall_bekreftet(con, kid) == 3 and db.antall_med_plass(con, kid) == 5


def test_advarslene_om_endret_format_sted_og_datoer_teller_alle_som_er_paa_kurset(con):
    kid = _kurs_med_fire(con)
    html = admin_klient().get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True)
    assert 'data-type-sted-vakt="4"' in html                                                                   # alle med plass: påmeldte og ekstradeltakere
    assert "Kurset har 2 påmeldte deltakere og 2 ekstradeltakere. Endres format eller sted" in html
    assert 'data-bekreft="Kurset har 2 påmeldte deltakere og 2 ekstradeltakere. De får ikke beskjed om endrede datoer automatisk.' in html
    ingen = kurs_med_samlinger(con, "TOM")
    assert "data-type-sted-vakt=\"0\"" in admin_klient().get(f"/admin/kurs/{ingen}/oppsett").get_data(as_text=True)


def test_avlysning_og_e_post_til_alle_paameldte_naar_ogsaa_ekstradeltakerne(con):
    kid = _kurs_med_fire(con)
    k = admin_klient()
    epost = k.get(f"/admin/kurs/{kid}/epost/ny?gruppe=bekreftet").get_data(as_text=True)
    assert all(navn in epost for navn in ("Ola Test", "Pia Test", "Eva Test", "Una Test"))
    knapp = k.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    assert "Send e-post til alle påmeldte og ekstradeltakere" in knapp
    assert k.post(f"/admin/kurs/{kid}/avlys").status_code == 302
    for navn in ("Ola", "Pia", "Eva", "Una"):
        assert "avlysning" in typer(con, navn), navn


def test_rapport_over_deltakere_teller_ikke_kurs_der_personen_bare_er_ekstradeltaker(con):
    k1, k2 = kurs_med_samlinger(con, "A1"), kurs_med_samlinger(con, "A2")
    meld(con, k1, "Kari")
    ekstra(con, k2, "Kari")                                                                                   # Kari er ekstradeltaker på det andre kurset
    meld(con, k1, "Tor")
    meld(con, k2, "Tor")                                                                                      # Tor er påmeldt på begge
    html = synlig_tekst(admin_klient().get("/admin/rapporter/deltakere?kun_flere_kurs=1").get_data(as_text=True))
    assert "Tor Test" in html and "Kari Test" not in html
    assert "Deltakere med flere kurs hos oss 1" in synlig_tekst(admin_klient().get("/admin/rapporter").get_data(as_text=True))
