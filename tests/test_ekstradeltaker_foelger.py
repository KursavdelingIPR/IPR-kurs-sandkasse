"""Hva samlingsutvalget til en ekstradeltaker betyr for påminnelser, e-post, innsjekk, oppmøte, kursbevis, Min side, kursside, søk og CSV.

En ekstradeltaker deltar på hele kurset eller bare noen samlinger (db.paameldingens_kursdager). Alt som gjelder kursdager per deltaker
bruker den funksjonen. Her testes hver følge med grensetilfeller: én samling, alle samlinger, dagen og uken før nøyaktig, samme
jobb kjørt to ganger, oppmøte utenfor utvalget som ikke skal telle, og «Samling N» som alltid er kursets eget nummer.
Bare oppdiktede data.
"""
import csv
import io
import re
from datetime import date

import pytest
from ekstrahjelp import (ADMIN, DAGER, antall, dager_for, ekstra, epost_til, epostemner, kurs_med_samlinger, meld, rad, samling_ider,
                         sett, status, synlig_tekst, typer)
from kurssidehjelp import dokument, fast_dato, ny_database, admin_klient, deltaker_klient

from kurs import daglig, db, deltakerside, deltakersok, kursbevis, maltekster, sveiper, behandling
from kurs import sideinnhold as si
from kurs.kjoring import Kjoring

ALLE = sum(DAGER.values(), [])                       # kursets fem dager
KURSNAVN = "Veiledning i gruppe"


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    fast_dato(monkeypatch)
    yield c
    c.close()


def _kurs(con, kid):
    return con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()


def _innk(con, kid, idag: date):
    """Innkallingene som morgenjobben sender for kurset på en gitt dag (uka før og dagen før)."""
    dager = db.kursdager(con, kid)
    daglig._innkallinger(Kjoring(con, idag=idag), _kurs(con, kid), dager, date.fromisoformat(dager[0]["dato"]))
    con.commit()


def _html(con, fornavn: str, type_: str) -> str:
    return con.execute("SELECT html FROM sendt_epost WHERE til=? AND type=?", (epost_til(fornavn), type_)).fetchone()[0]


def _emne(con, fornavn: str, type_: str) -> str:
    return con.execute("SELECT emne FROM sendt_epost WHERE til=? AND type=?", (epost_til(fornavn), type_)).fetchone()[0]


def _fire(con, kid):
    """Ola (Påmeldt), Eva (ekstradeltaker samling 1 og 3), Nina (ekstradeltaker samling 2 og 3) og Una (ekstradeltaker, hele kurset)."""
    return (meld(con, kid, "Ola"), ekstra(con, kid, "Eva", nr=[1, 3]), ekstra(con, kid, "Nina", nr=[2, 3]), ekstra(con, kid, "Una"))


# ============================ (a) påminnelser og Zoom ============================

def test_dagen_foer_sendes_bare_for_kursdager_ekstradeltakeren_er_paa_og_nummereres_blant_deres_dager(con):
    kid = kurs_med_samlinger(con)
    _fire(con, kid)
    _innk(con, kid, date(2031, 11, 12))                    # dagen før 13.11. (samling 2, første dag)
    assert typer(con, "Ola") == ["dagfor-2031-11-13"] and typer(con, "Una") == ["dagfor-2031-11-13"]
    assert sorted(typer(con, "Nina")) == ["dagfor-2031-11-13", "ukefor"] and typer(con, "Eva") == []          # Eva er ikke på samling 2
    assert _emne(con, "Nina", "dagfor-2031-11-13") == f"I morgen starter {KURSNAVN}"                       # Ninas første dag ...
    assert _emne(con, "Ola", "dagfor-2031-11-13") == f"I morgen, dag 3: {KURSNAVN}"                          # ... er dag 3 for Ola
    _innk(con, kid, date(2031, 11, 13))                    # dagen før 14.11.
    assert _emne(con, "Nina", "dagfor-2031-11-14") == f"I morgen, dag 2: {KURSNAVN}" and _emne(con, "Ola", "dagfor-2031-11-14") == f"I morgen, dag 4: {KURSNAVN}"
    assert typer(con, "Eva") == []
    _innk(con, kid, date(2031, 12, 3))                     # dagen før 04.12. (samling 3, siste dag for alle)
    for navn in ("Ola", "Eva", "Nina", "Una"):
        assert f"dagfor-2031-12-04" in typer(con, navn) and _emne(con, navn, "dagfor-2031-12-04") == f"Siste kursdag i morgen: {KURSNAVN}", navn
    assert typer(con, "Eva") == ["dagfor-2031-12-04"]                                                        # bare den ene


def test_uka_foer_regnes_fra_deres_foerste_dag_nyaktig_syv_dager_foer(con):
    kid = kurs_med_samlinger(con)
    ola, eva, nina, una = _fire(con, kid)
    _innk(con, kid, date(2031, 10, 8))                     # 8 dager før 16.10.: ingen
    assert [typer(con, n) for n in ("Ola", "Eva", "Nina", "Una")] == [[], [], [], []]
    _innk(con, kid, date(2031, 10, 9))                     # nøyaktig 7 dager før 16.10.: Ola, Eva og Una (deres første dag er 16.10.)
    assert typer(con, "Ola") == typer(con, "Eva") == typer(con, "Una") == ["ukefor"] and typer(con, "Nina") == []
    _innk(con, kid, date(2031, 11, 5))                     # 8 dager før 13.11.: Nina får ingenting ennå
    assert typer(con, "Nina") == []
    _innk(con, kid, date(2031, 11, 6))                     # nøyaktig 7 dager før DERES første dag (13.11.)
    assert typer(con, "Nina") == ["ukefor"]
    html = _html(con, "Nina", "ukefor")                    # bare Ninas dager: samling 2 og 3
    assert "2031-11-13" in html and "2031-11-14" in html and "2031-12-04" in html and "2031-10-16" not in html
    assert "som starter 2031-11-13" in synlig_tekst(html)                                                     # {startdato} = Ninas første dag
    assert "2031-10-16" in _html(con, "Eva", "ukefor") and "2031-11-13" not in _html(con, "Eva", "ukefor")


def test_uka_foer_for_en_ekstradeltaker_paa_bare_siste_samling_kommer_naar_den_starter(con):
    kid = kurs_med_samlinger(con)
    meld(con, kid, "Ola")
    ekstra(con, kid, "Eva", nr=[3])
    _innk(con, kid, date(2031, 11, 26))                    # 8 dager før 04.12.
    assert typer(con, "Eva") == []
    _innk(con, kid, date(2031, 11, 27))                    # 7 dager før
    assert typer(con, "Eva") == ["ukefor"] and typer(con, "Ola") == []                                       # Ola har passert sin første dag
    html = synlig_tekst(_html(con, "Eva", "ukefor"))
    assert "2031-12-04" in _html(con, "Eva", "ukefor") and "2031-10-16" not in _html(con, "Eva", "ukefor")
    assert "som starter 2031-12-04" in html                                                                   # {startdato}: deres første dag


def test_daglig_jobb_er_idempotent_ogsaa_med_ekstradeltakere(con):
    kid = kurs_med_samlinger(con)
    _fire(con, kid)
    for dag in (date(2031, 10, 9), date(2031, 10, 15), date(2031, 11, 6), date(2031, 11, 12), date(2031, 12, 3)):
        _innk(con, kid, dag)
        etter = (antall(con, "SELECT COUNT(*) FROM utsending_logg"), antall(con, "SELECT COUNT(*) FROM sendt_epost"))
        _innk(con, kid, dag)                                                                                  # samme dag en gang til
        _innk(con, kid, dag)
        assert (antall(con, "SELECT COUNT(*) FROM utsending_logg"), antall(con, "SELECT COUNT(*) FROM sendt_epost")) == etter, dag
    assert sorted(typer(con, "Eva")) == ["dagfor-2031-10-16", "dagfor-2031-12-04", "ukefor"]                 # ukefor bare én gang
    assert antall(con, "SELECT COUNT(*) FROM utsending_logg WHERE mottaker=? AND type='ukefor'", epost_til("Eva")) == 1


def test_hele_daglig_kjoring_gir_de_samme_innkallingene_og_holder_ekstradeltakere_tilbake(con):
    """Hele morgenjobben (ikke bare innkallingene): ingen automatisk bekreftelse eller faktura til en ekstradeltaker, men påminnelsene kommer."""
    kid = kurs_med_samlinger(con)
    ola, eva, nina, una = _fire(con, kid)
    daglig.kjor(Kjoring(con, idag=date(2031, 11, 12)))
    con.commit()
    assert "bekreftelse" in typer(con, "Ola") and "dagfor-2031-11-13" in typer(con, "Ola")
    for navn in ("Eva", "Nina", "Una"):
        assert "bekreftelse" not in typer(con, navn), navn
    assert sorted(typer(con, "Nina")) == ["dagfor-2031-11-13", "ukefor"] and typer(con, "Eva") == []
    assert antall(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id IN (?,?,?)", eva, nina, una) == 0
    assert antall(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", ola) >= 1
    etter = (antall(con, "SELECT COUNT(*) FROM utsending_logg"), antall(con, "SELECT COUNT(*) FROM faktura"))
    daglig.kjor(Kjoring(con, idag=date(2031, 11, 12)))
    con.commit()
    assert (antall(con, "SELECT COUNT(*) FROM utsending_logg"), antall(con, "SELECT COUNT(*) FROM faktura")) == etter


def test_zoom_lenken_sendes_bare_for_deres_dager(con):
    kid = kurs_med_samlinger(con, "ZOOM", type="digital", sted=None, zoom_url="https://zoom.eksempel.no/j/1234", zoom_id="1234")
    meld(con, kid, "Ola")
    ekstra(con, kid, "Eva", nr=[1])
    _innk(con, kid, date(2031, 11, 12))                    # dagen før samling 2
    assert typer(con, "Eva") == [] and "Bli med på Zoom" in _html(con, "Ola", "dagfor-2031-11-13")
    _innk(con, kid, date(2031, 10, 15))                    # dagen før samling 1
    assert "Bli med på Zoom" in _html(con, "Eva", "dagfor-2031-10-16") and "zoom.eksempel.no/j/1234" in _html(con, "Eva", "dagfor-2031-10-16")


def test_forhandsvalideringen_proever_en_mottaker_som_faktisk_faar_meldingen_og_zoom_avgjoeres_per_mottaker(con):
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[1])                  # lavest id: først i listen, men får ingenting 12.11.
    ola = meld(con, kid, "Ola")
    k = Kjoring(con, idag=date(2031, 11, 12))
    dager = db.kursdager(con, kid)
    deltakere = daglig._deltakere(k, kid)
    mottakere = {d["id"]: [x["dato"] for x in egne] for d, egne in daglig._mottakere(k, deltakere, dager)}
    assert mottakere == {eva: DAGER[1], ola: ALLE}
    forste = date.fromisoformat(dager[0]["dato"])
    assert daglig._forhandsvalider_innkallinger(k, _kurs(con, kid), dager, forste, deltakere) == {}
    assert daglig._skal_utsette_zoom(k, _kurs(con, kid), dager, forste, {}, deltakere) is False
    egne = {d["id"]: e for d, e in daglig._mottakere(k, deltakere, dager)}                                     # innkallingene regnes per deltaker
    assert [m for m, _t, _f in daglig._innkallinger_for_dager(k, egne[eva])] == []
    assert [m for m, _t, _f in daglig._innkallinger_for_dager(k, egne[ola])] == ["dagfor"]
    assert daglig._innkallinger_for_dager(k, []) == []                                                       # ingen dager: ingen innkallinger


def test_ingen_dager_gir_ingen_krasj_i_malverdiene(con):
    d = {"navn": "Eva Test", "fornavn": "Eva"}
    kurs = {"navn": KURSNAVN}
    v = maltekster._ukefor_verdier({"d": d, "kurs": kurs, "dager": []})
    assert "startdato" not in v and v["kursnavn"] == KURSNAVN
    assert maltekster._ukefor_verdier({"d": d, "kurs": kurs, "dager": [{"dato": "2031-12-04"}]})["startdato"] == "2031-12-04"


def test_zoom_import_gir_ikke_oppmoete_paa_dager_utenfor_utvalget(con, monkeypatch):
    kid = kurs_med_samlinger(con, "ZOOMI", type="digital", sted=None, zoom_url="https://zoom.eksempel.no/j/9", zoom_id="9")
    ola, eva = meld(con, kid, "Ola"), ekstra(con, kid, "Eva", nr=[1])
    monkeypatch.setattr(daglig.zoom, "deltakere_for_dato", lambda zoom_id, dato: [
        {"epost": epost_til("Ola"), "navn": "Ola Test", "minutter": 90}, {"epost": epost_til("Eva"), "navn": "Eva Test", "minutter": 90}])
    dager = db.kursdager(con, kid)
    k = Kjoring(con, idag=date(2031, 12, 10))
    daglig._importer_zoom(k, _kurs(con, kid), dager[0])                                                       # 16.10.: samling 1, begge
    daglig._importer_zoom(k, _kurs(con, kid), dager[2])                                                       # 13.11.: samling 2, bare Ola
    con.commit()
    reg = {(r["paamelding_id"], r["kursdag_id"]) for r in con.execute("SELECT * FROM oppmote")}
    assert reg == {(ola, dager[0]["id"]), (eva, dager[0]["id"]), (ola, dager[2]["id"])}


# ============================ (b) bekreftelsen viser bare deres samlinger ============================

def test_bekreftelsen_som_sendes_manuelt_viser_bare_samlingene_og_ingen_pris(con):
    kid = kurs_med_samlinger(con)
    ola, eva = meld(con, kid, "Ola"), ekstra(con, kid, "Eva", nr=[1, 3])
    idag = date(2031, 10, 1)
    sveiper.kjor(Kjoring(con, idag=idag))                 # morgenjobbens sveiper: Ola får bekreftelse og faktura, Eva ingenting
    con.commit()
    assert "bekreftelse" in typer(con, "Ola") and typer(con, "Eva") == []
    assert (rad(con, eva)["sveiper_utsatt"], rad(con, eva)["sveiper_kjort"]) == (1, 0)
    res = behandling.behandle_holdt_paamelding(con, kid, eva, idag, aktor=ADMIN)
    assert res.kode == behandling.FULLFORT
    html = synlig_tekst(_html(con, "Eva", "bekreftelse"))
    assert "Samling 1:" in html and "Samling 3:" in html and "Samling 2" not in html                            # kursets egne nummer
    assert "16.–17.10.2031" in html and "04.12.2031" in html and "13.–14.11.2031" not in html
    assert "Faktura" not in html and "2500 kr" not in html                                                      # prisen er ikke standardprisen
    assert antall(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", eva) == 0
    assert (rad(con, eva)["sveiper_utsatt"], rad(con, eva)["sveiper_kjort"]) == (0, 1)
    ola_html = synlig_tekst(_html(con, "Ola", "bekreftelse"))
    assert "Samling 2:" in ola_html and "Faktura på 2500 kr" in ola_html                                        # Påmeldt: som før
    assert behandling.behandle_holdt_paamelding(con, kid, eva, idag, aktor=ADMIN).kode == behandling.ALLEREDE_BEHANDLET


def test_bekreftelsen_for_hele_kurset_viser_alle_samlingene_men_heller_ingen_faktura(con):
    kid = kurs_med_samlinger(con)
    una = ekstra(con, kid, "Una")
    assert behandling.behandle_holdt_paamelding(con, kid, una, date(2031, 10, 1), aktor=ADMIN).kode == behandling.FULLFORT
    html = synlig_tekst(_html(con, "Una", "bekreftelse"))
    assert all(f"Samling {n}:" in html for n in (1, 2, 3)) and "Faktura" not in html
    assert behandling.forventet_handling(_kurs(con, kid), rad(con, una)) == "Bekreftelse på e-post (ingen faktura for ekstradeltakere)"


# ============================ (c) innsjekk ============================

def _dag(con, kid, dato: str):
    return con.execute("SELECT id, kurs_id, dato, innsjekk_token, innsjekk_kode FROM kursdag WHERE kurs_id=? AND dato=?", (kid, dato)).fetchone()


def test_qr_innsjekk_bare_paa_egne_dager_ellers_vennlig_melding_til_innlogget_og_noeytral_til_uinnlogget(con):
    kid = kurs_med_samlinger(con)
    ola, eva = meld(con, kid, "Ola"), ekstra(con, kid, "Eva", nr=[1])
    did = rad(con, eva)["deltaker_id"]
    utenfor, innenfor = _dag(con, kid, "2031-11-13"), _dag(con, kid, "2031-10-17")
    res_ute = deltakerside.registrer_qr(con, utenfor, date(2031, 11, 13), epost=epost_til("Eva"))
    assert antall(con, "SELECT COUNT(*) FROM oppmote") == 0
    ukjent = deltakerside.registrer_qr(con, utenfor, date(2031, 11, 13), epost="ingen@eksempel.no")
    assert res_ute.status == ukjent.status == "ikke_paameldt"                                                         # uinnlogget: samme svar som for en ukjent e-post
    assert deltakerside.tekst_for(res_ute, innlogget=False) == deltakerside.tekst_for(ukjent, innlogget=False)      # ingen lekkasje
    inne = deltakerside.registrer_qr(con, utenfor, date(2031, 11, 13), deltaker_id=did)
    assert inne.status == "ikke_paa_samlingen" and inne.fornavn == "Eva"
    assert deltakerside.tekst_for(inne, innlogget=True) == "Du er ikke satt opp på denne samlingen. Snakk med kursansvarlig."
    assert deltakerside.registrer_qr(con, utenfor, date(2031, 11, 13), epost=epost_til("Ola")).status == "ny"        # Påmeldt: alle dager
    ny = deltakerside.registrer_qr(con, innenfor, date(2031, 10, 17), deltaker_id=did)
    assert ny.status == "ny" and (ny.kursdag_nr, ny.kursdag_av) == (2, 2)                                       # dag 2 av 2 blant DERES dager
    assert deltakerside.registrer_qr(con, _dag(con, kid, "2031-10-17"), date(2031, 10, 17), epost=epost_til("Ola")).kursdag_av == 5
    assert antall(con, "SELECT COUNT(*) FROM oppmote WHERE paamelding_id=?", eva) == 1
    assert deltakerside.registrer_qr(con, innenfor, date(2031, 10, 17), deltaker_id=did).status == "allerede"


def test_kode_innsjekk_og_registrer_selv_paa_nett_bare_paa_egne_dager(con):
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[2])
    did = rad(con, eva)["deltaker_id"]
    con.execute("UPDATE kurs SET kode='EKS3' WHERE id=?", (kid,))
    con.commit()
    ute, inne = _dag(con, kid, "2031-10-16"), _dag(con, kid, "2031-11-13")
    res = deltakerside.registrer_selv(con, did, "EKS3", ute["innsjekk_kode"], date(2031, 10, 16))
    assert res.status == "ikke_paa_samlingen" and antall(con, "SELECT COUNT(*) FROM oppmote") == 0
    assert deltakerside.tekst_for(res, innlogget=True, nett=True) == deltakerside.TEKST_IKKE_PAA_SAMLINGEN
    assert deltakerside.registrer_selv(con, did, "EKS3", "", date(2031, 12, 24)).status == "ingen_kursdag"        # ingen kursdag i dag
    assert deltakerside.registrer_selv(con, did, "EKS3", "feil", date(2031, 11, 13)).status == "kode_feil"
    ok = deltakerside.registrer_selv(con, did, "EKS3", inne["innsjekk_kode"], date(2031, 11, 13))
    assert (ok.status, ok.kursdag_nr, ok.kursdag_av) == ("ny", 1, 2)


def test_innsjekksidene_via_nettet_qr_og_kode(con, monkeypatch):
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[1])
    did = rad(con, eva)["deltaker_id"]
    fast_dato(monkeypatch, date(2031, 11, 13))             # kursdag, men samling 2
    dag = _dag(con, kid, "2031-11-13")
    k = deltaker_klient(did)
    r = k.post(f"/innsjekk/{dag['innsjekk_token']}", data={})
    assert r.status_code == 200 and "Du er ikke satt opp på denne samlingen. Snakk med kursansvarlig." in r.get_data(as_text=True)
    kode = con.execute("SELECT kode FROM kurs WHERE id=?", (kid,)).fetchone()["kode"]
    r = k.post(f"/kurs/{kode}/deltakerside/oppmote", data={"kode": dag["innsjekk_kode"], "retur": "min_side"}, follow_redirects=True)   # på nett: Mine kurs (kodesiden /innsjekk er fjernet)
    assert "Du er ikke satt opp på denne samlingen. Snakk med kursansvarlig." in r.get_data(as_text=True)
    assert antall(con, "SELECT COUNT(*) FROM oppmote") == 0
    uinnlogget = deltaker_klient().post(f"/innsjekk/{dag['innsjekk_token']}", data={"epost": epost_til("Eva")})
    ukjent = deltaker_klient().post(f"/innsjekk/{dag['innsjekk_token']}", data={"epost": "ingen@eksempel.no"})
    assert "samlingen" not in uinnlogget.get_data(as_text=True).lower()                                          # verken tekst eller overskrift røper noe
    assert synlig_tekst(uinnlogget.get_data(as_text=True)) == synlig_tekst(ukjent.get_data(as_text=True))       # siden er lik for en ukjent e-post
    innlogget = k.post(f"/innsjekk/{dag['innsjekk_token']}", data={})
    assert "Ikke satt opp på samlingen" in innlogget.get_data(as_text=True) and 'href=""' not in innlogget.get_data(as_text=True)   # ingen «Prøv igjen»


def test_admin_kan_fortsatt_registrere_oppmoete_manuelt_utenfor_utvalget_men_det_teller_ikke(con):
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[1])
    dag = _dag(con, kid, "2031-11-13")
    r = admin_klient().post(f"/admin/kurs/{kid}/oppmote", data={"paamelding_id": eva, "kursdag_id": dag["id"]})
    assert r.status_code == 302 and antall(con, "SELECT COUNT(*) FROM oppmote WHERE paamelding_id=?", eva) == 1
    assert dag["dato"] not in dager_for(con, eva)                                                            # lagret, men ikke en av deres dager


def test_min_side_og_kursside_forklarer_at_man_ikke_er_satt_opp_paa_dagens_samling(con, monkeypatch):
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[1])
    did = rad(con, eva)["deltaker_id"]
    kurs = deltakerside.hent_kurs_for_side(con, "EKS3") or deltakerside.hent_kurs_for_side(con, con.execute("SELECT kode FROM kurs").fetchone()[0])
    kort = deltakerside.oppmote_i_dag(con, did, kurs, None, date(2031, 11, 13))
    assert kort["tilbys"] is False and kort["registrert"] is None and kort["forklaring"] == deltakerside.TEKST_IKKE_PAA_SAMLINGEN
    assert kort["nr"] is None and kort["kursdag_id"] is None
    egen = deltakerside.oppmote_i_dag(con, did, kurs, None, date(2031, 10, 16))
    assert egen["nr"] == 1 and egen["av"] == 2 and egen["tilbys"] is True                                     # dag 1 av 2 blant deres
    assert deltakerside.oppmote_i_dag(con, did, kurs, None, date(2031, 11, 30)) is None                        # ikke kursdag i det hele tatt
    fast_dato(monkeypatch, date(2031, 11, 13))
    html = deltaker_klient(did).get("/min-side").get_data(as_text=True)
    assert "Du er ikke satt opp på denne samlingen. Snakk med kursansvarlig." in html and "Registrer oppmøte</button>" not in html
    assert "dag None" not in html


# ============================ (d) oppmøtematrisen ============================

def test_oppmoetematrisen_tones_ned_utenfor_utvalget_og_oppmoetet_der_teller_ikke(con):
    kid = kurs_med_samlinger(con)
    ola, eva, nina = meld(con, kid, "Ola"), ekstra(con, kid, "Eva", nr=[1]), ekstra(con, kid, "Nina", nr=[2])
    dag = _dag(con, kid, "2031-11-13")
    k = admin_klient()
    for pid in (eva, ola):                                                                                     # Eva er utenfor, Ola innenfor
        assert k.post(f"/admin/kurs/{kid}/oppmote", data={"paamelding_id": pid, "kursdag_id": dag["id"]}).status_code == 302
    html = k.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    matrise = html[html.index('<h2 id="oppmote">'):]
    rader = {navn: r for navn, r in zip(("Ola", "Eva", "Nina"), re.findall(r'<tr data-filtrer="oppmote".*?</tr>', matrise, re.S))}
    assert rader["Ola"].count("dempet-dag") == 0
    assert rader["Eva"].count("dempet-dag") == 3 and "Samling 1" in rader["Eva"]                                # tre dager utenfor
    assert rader["Nina"].count("dempet-dag") == 3
    assert "Nedtonede dager er samlinger en ekstradeltaker ikke er satt opp på" in matrise
    assert k.get(f"/admin/kurs/{kid}/oppsett").status_code == 200
    oppsett = k.get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True)
    assert "<td>1 / 2</td>" in oppsett                       # 13.11.: Ola og Nina er på dagen (Eva ikke), og bare Ola har møtt: Evas teller ikke
    assert "<td>0 / 2</td>" in oppsett                       # 16.10.: Ola og Eva (Nina ikke), ingen har møtt


# ============================ (e) kursbevis ============================

def _avslutt_og_kjor(con, kid, idag=date(2031, 12, 20)):
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()
    kursbevis.kjor(Kjoring(con, idag=idag))
    con.commit()


def _bevis(con, kid) -> dict[int, str]:
    return {r["deltaker_id"]: con.execute("SELECT innhold FROM dokument_innhold WHERE dokument_id=?", (r["id"],)).fetchone()[0]
            for r in con.execute("SELECT id, deltaker_id FROM dokument WHERE type='kursbevis' AND kurs_id=?", (kid,))}


def _moett(con, pid: int, *datoer):
    for d in datoer:
        db.registrer_oppmote(con, pid, next(x["id"] for x in db.kursdager(con, con.execute("SELECT kurs_id FROM paamelding WHERE id=?", (pid,)).fetchone()[0])
                                            if x["dato"] == d), "manuell")
    con.commit()


def test_kursbevis_krever_oppmoete_paa_alle_deres_dager_og_sier_hvilke_samlinger(con):
    kid = kurs_med_samlinger(con, spesialistlop=None)
    ola, eva, nina = meld(con, kid, "Ola"), ekstra(con, kid, "Eva", nr=[1, 3]), ekstra(con, kid, "Nina", nr=[1, 3])
    for pid in (ola, eva):
        _moett(con, pid, *DAGER[1], *DAGER[3])              # begge har møtt samling 1 og 3
    _moett(con, nina, *DAGER[1])                            # Nina mangler samling 3
    _avslutt_og_kjor(con, kid)
    bevis = _bevis(con, kid)
    assert set(bevis) == {rad(con, eva)["deltaker_id"]}                                                       # Ola mangler samling 2, Nina samling 3
    html = bevis[rad(con, eva)["deltaker_id"]]
    assert "har deltatt på samling 1 og 3 i" in html and "Samling 1 og 3 (av kursets 3)" in html
    assert "har gjennomført" not in html
    assert all(d in html for d in ("2031-10-16", "2031-10-17", "2031-12-04")) and "2031-11-13" not in html
    assert re.search(r"<td>Timer</td><td>17</td>", html)                                                      # 6 + 6 + 5 (samling 3 har 5 timer)


def test_oppmoete_utenfor_utvalget_teller_verken_i_hundre_prosent_eller_i_timene(con):
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[2])
    _moett(con, eva, *DAGER[2], *DAGER[1])                  # begge egne dager, og samling 1 (utenfor: manuelt eller fra Zoom)
    _avslutt_og_kjor(con, kid)
    html = _bevis(con, kid)[rad(con, eva)["deltaker_id"]]
    assert "har deltatt på samling 2 i" in html and "2031-10-16" not in html and re.search(r"<td>Timer</td><td>12</td>", html)
    kid2 = kurs_med_samlinger(con, "EKS4")
    una = ekstra(con, kid2, "Una", nr=[1, 3])
    _moett(con, una, *DAGER[1], *DAGER[2])                  # samling 3 mangler; samling 2 er utenfor og skal ikke fylle hullet
    _avslutt_og_kjor(con, kid2)
    assert _bevis(con, kid2) == {}


def test_kursbevis_for_hele_kurset_er_uendret_og_sier_har_gjennomfoert(con):
    kid = kurs_med_samlinger(con)
    una, ola = ekstra(con, kid, "Una"), meld(con, kid, "Ola")
    for pid in (una, ola):
        _moett(con, pid, *ALLE)
    _avslutt_og_kjor(con, kid)
    bevis = _bevis(con, kid)
    assert set(bevis) == {rad(con, una)["deltaker_id"], rad(con, ola)["deltaker_id"]}
    for html in bevis.values():
        assert "har gjennomført" in html and "har deltatt på" not in html and "Samlinger" not in html
        assert re.search(r"<td>Timer</td><td>29</td>", html)                                                  # 4 x 6 + 5


def test_timer_i_spesialistloep_regnes_fra_dagene_de_moette_og_bare_egne_dager(con):
    kid = kurs_med_samlinger(con, spesialistlop="EFT")
    ola, eva = meld(con, kid, "Ola"), ekstra(con, kid, "Eva", nr=[1])
    _moett(con, ola, *DAGER[3])                                                                               # samling 3 har 5 timer per dag
    _moett(con, eva, *DAGER[1], *DAGER[3])                                                                    # samling 3 er utenfor utvalget
    assert kursbevis.timer_i_lop(con, rad(con, ola)["deltaker_id"], "EFT") == 5.0
    assert kursbevis.timer_i_lop(con, rad(con, eva)["deltaker_id"], "EFT") == 12.0


# ============================ (f) Min side og kursside ============================

def test_min_side_viser_bare_deres_datoer_og_oppmoete(con, monkeypatch):
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[1, 3])
    _moett(con, eva, DAGER[1][0])
    did = rad(con, eva)["deltaker_id"]
    fast_dato(monkeypatch, date(2031, 12, 10))
    html = deltaker_klient(did).get("/min-side").get_data(as_text=True)
    tekst = synlig_tekst(html)
    assert "16.–17.10.2031 og 04.12.2031" in tekst and "13.–14.11.2031" not in tekst
    merker = re.findall(r'class="merke (?:ok|gra|feil)" title="(\d{4}-\d\d-\d\d) – ([^"]+)"', html)
    assert merker == [("2031-10-16", "registrert"), ("2031-10-17", "ikke registrert"), ("2031-12-04", "ikke registrert")]
    assert '<span class="merke ok">Påmeldt</span>' in html and "Ekstradeltaker" not in html                      # deltakeren ser «Påmeldt»


def _program(con, kid):
    """En programblokk med ett punkt for hver kursdag (knyttet til kursdagens id)."""
    dager = [{"kursdag_id": d["id"], "dato": d["dato"], "tittel": "", "punkter": [{"fra": "09:00", "til": "10:00", "tema": f"Tema {d['dato']}"}]}
             for d in db.kursdager(con, kid)]
    return dokument(si._ny_blokk("program", {"dager": dager}))


def _visning(con, kid, did, idag, **kw):
    kurs = deltakerside.hent_kurs_for_side(con, con.execute("SELECT kode FROM kurs WHERE id=?", (kid,)).fetchone()[0])
    return deltakerside.bygg_visning(con, kurs, _program(con, kid), deltaker_id=did, idag=idag, forhandsvisning=kw.pop("forhandsvisning", False),
                                     fil_url=lambda i: f"/fil/{i}", sp_url=lambda n: None, **kw)


def test_kursside_viser_bare_deres_dager_program_og_samlingsnummer(con):
    kid = kurs_med_samlinger(con)
    eva, ola = ekstra(con, kid, "Eva", nr=[3]), meld(con, kid, "Ola")
    v = _visning(con, kid, rad(con, eva)["deltaker_id"], date(2031, 12, 4))
    assert [(d["nr"], d["dato_kort"]) for d in v["kursdager"]] == [(1, "tor 4. desember")]
    assert [p["tekst"] for p in v["pillene"]] == ["Du er påmeldt", "Dag 1 av 1"]                               # ingen «Samling N av M»-merkelapp lenger
    assert [g["navn"] for g in v["samlinger"]] == ["Samling 3"]                                               # kursets eget samlingsnummer, også når bare den er med
    assert v["kursdager"][0]["samling"] == "Samling 3" and v["i_dag"]["samling"] == "Samling 3"
    program = next(b for b in v["blokker"] if b["type"] == "program")["data"]["dager"]
    assert [d["punkter"][0]["tema"] for d in program] == ["Tema 2031-12-04"]                                  # ingen andre dagers program
    assert v["i_dag"]["nr"] == 1 and v["i_dag"]["av"] == 1 and v["neste_dag"] is None
    v2 = _visning(con, kid, rad(con, ola)["deltaker_id"], date(2031, 12, 4))
    assert len(v2["kursdager"]) == 5 and "Dag 5 av 5" in [p["tekst"] for p in v2["pillene"]]                   # Påmeldt: uendret
    assert [g["navn"] for g in v2["samlinger"]] == ["Samling 1", "Samling 2", "Samling 3"]                    # og ser alle samlingene
    hele = _visning(con, kid, None, date(2031, 12, 4), forhandsvisning=True)
    assert len(hele["kursdager"]) == 5 and len(next(b for b in hele["blokker"] if b["type"] == "program")["data"]["dager"]) == 5


def test_kursside_neste_dag_og_dagens_kort_for_en_dag_utenfor_utvalget(con):
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[1, 3])
    v = _visning(con, kid, rad(con, eva)["deltaker_id"], date(2031, 11, 13))                                  # kursdag, men samling 2
    assert v["i_dag"]["forklaring"] == deltakerside.TEKST_IKKE_PAA_SAMLINGEN and v["i_dag"]["tilbys"] is False
    assert v["neste_dag"]["dag_nr"] == 3 and v["neste_dag"]["av"] == 3                                        # Evas neste dag: 04.12.


def test_filer_knyttet_til_dager_utenfor_utvalget_vises_hverken_paa_dagen_eller_som_ovrige(con):
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[1])
    kurs = deltakerside.hent_kurs_for_side(con, con.execute("SELECT kode FROM kurs WHERE id=?", (kid,)).fetchone()[0])
    alle = deltakerside.kursdager_for(con, kurs)
    egne = deltakerside.kursdager_for(con, kurs, eva)
    assert [d["dato"] for d in egne] == DAGER[1] and [d["samling"] for d in egne] == ["Samling 1", "Samling 1"]
    assert [d["samling"] for d in alle][2:4] == ["Samling 2", "Samling 2"]                                    # kursets eget nummer også her
    utenfor = {d["id"] for d in alle} - {d["id"] for d in egne}
    ute_id, inne_id = alle[2]["id"], alle[0]["id"]
    meta = {i: {"id": i, "filnavn": f"fil{i}.pdf", "type": "dokument", "storrelse": 2048, "opprettet": "2031-10-01 10:00:00"} for i in (1, 2, 3)}
    b = si._ny_blokk("filer", {"filer": [{"fil_id": 1, "tittel": "Bare samling 2", "gruppe": ute_id},
                                         {"fil_id": 2, "tittel": "Dag 1", "gruppe": inne_id},
                                         {"fil_id": 3, "tittel": "Uten dag", "gruppe": None}], "sharepoint": False})
    ut = deltakerside._filer_data(con, kurs, b, meta, egne, date(2031, 10, 1), lambda i: f"/fil/{i}", lambda n: None, frozenset(utenfor))
    tittel = [(g["tittel"], [f["tittel"] for f in g["filer"]]) for g in ut["grupper"]]
    assert [t for t in tittel if "Bare samling 2" in t[1]] == []                                              # aldri vist
    assert tittel[0][1] == ["Dag 1"] and tittel[0][0].startswith("Dag 1 · ") and tittel[-1] == ("Øvrige filer", ["Uten dag"])
    alt = deltakerside._filer_data(con, kurs, b, meta, alle, date(2031, 10, 1), lambda i: f"/fil/{i}", lambda n: None)     # Påmeldt: alle
    assert "Bare samling 2" in [f["tittel"] for g in alt["grupper"] for f in g["filer"]]


# ============================ (g) søk ============================

def test_soeket_viser_utvalget_og_regner_oppmoete_og_kursbevis_paa_deres_dager(con):
    kid = kurs_med_samlinger(con)
    ola, eva, una = meld(con, kid, "Ola"), ekstra(con, kid, "Eva", nr=[1, 3]), ekstra(con, kid, "Una")
    _moett(con, eva, DAGER[1][0], DAGER[1][1], DAGER[2][0])                                                   # 2 av deres 3 dager + 1 utenfor
    _moett(con, ola, DAGER[1][0])
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()
    idag = date(2031, 12, 10)

    def paamelding(navn):
        return deltakersok.sok(con, epost_til(navn), idag=idag).personer[0]["paameldinger"][0]

    p = paamelding("Eva")
    assert p["status_navn"] == "Ekstradeltaker – samling 1 og 3" and p["oppmote"] == "2 av 3 kursdager"
    assert p["kursbevis"] == "Ikke utstedt: oppmøte 2 av 3 dager (kursbevis utstedes ved fullt oppmøte)" and p["datoer"] == "16.10.2031 – 04.12.2031"
    assert paamelding("Una")["status_navn"] == "Ekstradeltaker" and paamelding("Una")["oppmote"] == "0 av 5 kursdager"
    assert paamelding("Ola")["status_navn"] == "Påmeldt" and paamelding("Ola")["oppmote"] == "1 av 5 kursdager"


def test_soeket_for_en_ekstradeltaker_paa_en_samling_bruker_bare_den_samlingens_datoer(con):
    kid = kurs_med_samlinger(con, "EKS5")
    nina = ekstra(con, kid, "Nina", nr=[2])
    _moett(con, nina, *DAGER[2])
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()
    p = deltakersok.sok(con, epost_til("Nina"), idag=date(2031, 11, 15)).personer[0]["paameldinger"][0]      # dagen etter DERES siste dag
    assert p["status_navn"] == "Ekstradeltaker – samling 2" and p["datoer"] == "13.11.2031 – 14.11.2031" and p["oppmote"] == "2 av 2 kursdager"
    assert p["kursbevis"] == "Ikke utstedt ennå: utstedes av morgenjobben"


# ============================ (h) rapporter og CSV ============================

def _csv(tekst: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(tekst.lstrip("﻿")), delimiter=";"))


def test_deltaker_csv_har_kolonnen_samlinger(con):
    kid = kurs_med_samlinger(con)
    ola, eva, una = meld(con, kid, "Ola"), ekstra(con, kid, "Eva", nr=[1, 3]), ekstra(con, kid, "Una")
    avmeldt = meld(con, kid, "Mia")
    sett(con, avmeldt, "avmeldt")
    k = admin_klient()
    r = k.get(f"/admin/kurs/{kid}/deltakere.csv")
    rader = _csv(r.get_data(as_text=True))
    hode = rader[0]
    assert hode[hode.index("Status") + 1] == "Samlinger"
    per_navn = {x[0]: (x[hode.index("Status")], x[hode.index("Samlinger")]) for x in rader[1:]}
    assert per_navn == {"Ola": ("Påmeldt", "Hele kurset"), "Eva": ("Ekstradeltaker", "Samling 1, 3"),
                        "Una": ("Ekstradeltaker", "Hele kurset"), "Mia": ("Avmeldt", "")}
    valgte = k.post(f"/admin/kurs/{kid}/deltakere/eksporter-valgte.csv", data={"paamelding_id": [eva, ola]})
    assert {x[0]: x[hode.index("Samlinger")] for x in _csv(valgte.get_data(as_text=True))[1:]} == {"Ola": "Hele kurset", "Eva": "Samling 1, 3"}
    assert "Samling" in r.get_data(as_text=True) and "Glutenfri" not in r.get_data(as_text=True)


def test_deltakerlisten_og_dens_csv_har_samlinger_og_oppmoete_bare_paa_egne_dager(con):
    kid = kurs_med_samlinger(con)
    ola, eva = meld(con, kid, "Ola"), ekstra(con, kid, "Eva", nr=[1])
    _moett(con, eva, DAGER[1][0], DAGER[2][0])                                                                # den andre er utenfor utvalget
    _moett(con, ola, DAGER[2][0])
    k = admin_klient()
    csv_ = _csv(k.get(f"/admin/kurs/{kid}/deltakerliste.csv?valgt=1&kol=samlinger&kol=oppmote&status=plass").get_data(as_text=True))
    hode = csv_[0]
    assert hode[:2] == ["Navn", "Samlinger"] and "Oppmøte 2031-11-13" in hode
    per = {x[0]: dict(zip(hode, x)) for x in csv_[1:]}
    assert per["Eva Test"]["Samlinger"] == "Samling 1" and per["Ola Test"]["Samlinger"] == "Hele kurset"
    assert per["Eva Test"]["Oppmøte 2031-10-16"] == "ja" and per["Eva Test"]["Oppmøte 2031-11-13"] == "" and per["Ola Test"]["Oppmøte 2031-11-13"] == "ja"
    side = k.get(f"/admin/kurs/{kid}/deltakerliste?valgt=1&kol=samlinger&kol=oppmote&status=plass").get_data(as_text=True)
    assert "1 påmeldt + 1 ekstradeltaker" in synlig_tekst(side)                                                # tallene hver for seg
    eva_rad = re.search(r"<tr><td class=\"\">Eva Test</td>.*?</tr>", side, re.S).group(0)
    assert eva_rad.count("–</td>") >= 3 and "Samling 1" in eva_rad                                             # dager utenfor: «–»


def test_allergilisten_viser_hvilke_samlinger_naar_noen_bare_er_paa_noen(con):
    kid = kurs_med_samlinger(con)
    meld(con, kid, "Ola", sensitivt={"allergier": "Nøtter", "tilrettelegging": None})
    ekstra(con, kid, "Eva", nr=[2], sensitivt={"allergier": "Skalldyr", "tilrettelegging": None})
    html = admin_klient().get(f"/admin/kurs/{kid}/allergiliste").get_data(as_text=True)
    assert "Deltar på" in html and "Samling 2" in html and "Hele kurset" in html and "Nøtter" in html and "Skalldyr" in html
    kid2 = kurs_med_samlinger(con, "EKS6")
    meld(con, kid2, "Pia", sensitivt={"allergier": "Egg", "tilrettelegging": None})
    assert "Deltar på" not in admin_klient().get(f"/admin/kurs/{kid2}/allergiliste").get_data(as_text=True)     # ingen delvis: ingen kolonne
