"""Ekstradeltakere: rettingene etter kritikernes gjennomgang (hvert funn har sin egen test, så ingen av dem kommer tilbake).

  1. en mislykket manuell sending til en ekstradeltaker ender aldri i en blindvei (holdet står igjen til bekreftelsen er fullført);
  2. Påmeldt <-> Ekstradeltaker virker også på et avsluttet eller avlyst kurs (bare merkelappen), og steget for samlingsvalg tilbys ikke
     der en ny person uansett avvises;
  3. kapasitetssjekken er ikke et les-så-skriv-kappløp (kurset låses før plassene telles);
  4. «Lagre samlinger» har versjonskontroll som de andre skjemaene i vinduet;
  5. migrering 18 logger kursene der en ny påmelding kan ta plassen foran ventelisten;
  6. testhull fra mutasjonsprøven: kapasitetsendring og grensetilfeller ved kapasitet;
  7. samlingsutvalget krymper ikke i det stille når samlinger endres;
  8. kalender og årsplan viser ekstradeltakerne per samling;
  9. samlingsvalg uten avkryssing av «Registrer som ekstradeltaker» og motstridende valg avvises;
 10. egne samlingsnavn skrives som de står («EMDR», ikke «emdr»);
 11. samlingsteksten følger datorekkefølgen, ikke samlings-id;
 12. fullverdige deltakere er uendret (kursstatus «full», spesialistløp-timer);
 13. nedtonede dager i oppmøtematrisen sier det også i teksten;
 14. retur til Påmeldt: dialog, vindu og melding sier hva som skjer, og ingen faktura lages av seg selv (flere tester i
     test_ekstradeltaker_rest.py);
 16. «Send bekreftelse nå» sier ikke «sendt» når ingenting gikk ut;
 18. tekstene lover ikke en fakturafunksjon som ikke finnes.
Bare oppdiktede data.
"""
import csv
import io
import json
import random
import re
import sqlite3
import threading
from datetime import date
from html import unescape

import pytest
from adressehjelp import ADRESSE
from ekstrahjelp import (ADMIN, DAGER, S, S1, antall, dager_for, ekstra, epost_til, kurs_med_samlinger, meld, rad, samling_ider,
                         sett, status, synlig_tekst, typer, utvalg_rader)
from kurssidehjelp import admin_klient, csrf, fast_dato, ny_database

from kurs import behandling, daglig, db, kurskalender, kursbevis, kursdatoer, migreringer, sveiper
from kurs.integrasjoner import epost
from kurs.kjoring import Kjoring

IDAG = date(2031, 10, 1)


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    fast_dato(monkeypatch, IDAG)
    yield c
    c.close()


def _flash(html: str) -> list[str]:
    return [synlig_tekst(m) for m in re.findall(r'<div class="flash[^"]*"[^>]*>(.*?)</div>', html, re.S)]


def _meldinger(k, r) -> str:
    """Meldingene (flash) på siden en POST sender videre til."""
    return " ".join(_flash(k.get(r.headers["Location"]).get_data(as_text=True)))


def _tilstand(con, pid):
    r = rad(con, pid)
    return (r["sveiper_kjort"], r["sveiper_utsatt"])


def _faktura(con, pid: int) -> int:
    return antall(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", pid)


def _kursstatus(con, kid: int, ny: str | None = None) -> str:
    if ny:
        con.execute("UPDATE kurs SET status=? WHERE id=?", (ny, kid))
        con.commit()
    return con.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()[0]


# ============================ 1. blindvei etter mislykket manuell sending ============================

def _odelegg_bekreftelsesmalen(con):
    """Et ukjent flettefelt: malen kan ikke rendres (MalFeil), og ingenting reserveres eller sendes."""
    con.execute("INSERT INTO mal_tekst (mal, felt, tekst) VALUES ('bekreftelse', 'innledning', 'Hei {ukjent}')")
    con.commit()


def _reparer_bekreftelsesmalen(con):
    con.execute("DELETE FROM mal_tekst WHERE mal='bekreftelse'")
    con.commit()


def test_mislykket_manuell_sending_til_ekstradeltaker_beholder_holdet_og_kan_proves_paa_nytt(con):
    kid = kurs_med_samlinger(con, kapasitet=10)
    vera = meld(con, kid, "Vera", paamelding={"sveiper_utsatt": 1})               # vanlig Påmeldt, holdt tilbake som en manuell registrering
    ella = meld(con, kid, "Ella", ekstradeltaker=True)
    _odelegg_bekreftelsesmalen(con)
    k = admin_klient(con)
    r = k.post(f"/admin/kurs/{kid}/deltaker/{vera}/behandle")
    assert "fanges opp automatisk igjen ved neste daglige kjøring" in _meldinger(k, r)              # vanlig Påmeldt: som før
    assert _tilstand(con, vera) == (0, 0)                     # flagget nullstilles, morgenjobben tar den igjen (som før)
    r = k.post(f"/admin/kurs/{kid}/deltaker/{ella}/behandle")
    tekst = _meldinger(k, r)
    assert "fortsatt holdt tilbake" in tekst and "prøv igjen fra denne siden" in tekst and "behandles aldri av den daglige jobben" in tekst
    assert "fanges opp automatisk" not in tekst                # løftet om morgenjobben gjelder ikke en ekstradeltaker
    assert _tilstand(con, ella) == (0, 1)                      # holdet står igjen: ikke en blindvei
    assert "Behandling holdt tilbake" in k.get(f"/admin/kurs/{kid}/deltaker/{ella}").get_data(as_text=True)   # boksen og knappen er der
    _reparer_bekreftelsesmalen(con)
    sveiper.kjor(Kjoring(con, idag=IDAG))                      # morgenjobben: Vera får bekreftelsen, Ella ingenting
    con.commit()
    assert typer(con, "Vera") == ["bekreftelse"] and typer(con, "Ella") == [] and _tilstand(con, ella) == (0, 1)
    r = k.post(f"/admin/kurs/{kid}/deltaker/{ella}/behandle")  # administrator prøver igjen: nå virker det
    assert "Bekreftelsen er sendt" in _meldinger(k, r)
    con.commit()
    assert typer(con, "Ella") == ["bekreftelse"] and _tilstand(con, ella) == (1, 0) and _faktura(con, ella) == 0
    assert "Behandling holdt tilbake" not in k.get(f"/admin/kurs/{kid}/deltaker/{ella}").get_data(as_text=True)


def test_uavklart_sending_til_ekstradeltaker_kan_avklares_og_proves_paa_nytt(con, monkeypatch):
    """E-posttjenesten feiler UNDER sending (uavklart). Administrator kontrollerer i Outlook og registrerer «ikke sendt». Da sendes
    den av den daglige jobben for en vanlig Påmeldt, men en ekstradeltaker tas aldri av den: holdet må stå igjen, så knappen virker."""
    kid = kurs_med_samlinger(con, kapasitet=10)
    ella = meld(con, kid, "Ella", ekstradeltaker=True)
    sendt, nede = [], {"ja": True}

    def send(til, emne, html, *a, **kw):
        if nede["ja"]:
            raise RuntimeError("Graph svarte ikke")
        sendt.append(til)
    monkeypatch.setattr(epost, "send", send)
    k = admin_klient(con)
    k.post(f"/admin/kurs/{kid}/deltaker/{ella}/behandle")
    assert _tilstand(con, ella) == (0, 1) and sendt == []
    r = k.post("/admin/uavklart/epost", data={"nokkel": f"kurs:{kid}", "mottaker": epost_til("Ella"), "type": "bekreftelse", "utfall": "ikke_sendt"})
    assert r.status_code == 302
    nede["ja"] = False
    sveiper.kjor(Kjoring(con, idag=IDAG))
    con.commit()
    assert sendt == [] and _tilstand(con, ella) == (0, 1)        # morgenjobben tar ikke en ekstradeltaker ...
    assert "Behandling holdt tilbake" in k.get(f"/admin/kurs/{kid}/deltaker/{ella}").get_data(as_text=True)    # ... men knappen er der
    k.post(f"/admin/kurs/{kid}/deltaker/{ella}/behandle")
    con.commit()
    assert sendt == [epost_til("Ella")] and _tilstand(con, ella) == (1, 0)


def test_en_ekstradeltaker_uten_hold_som_ikke_er_behandlet_kan_fortsatt_sendes_manuelt(con):
    """Sikring: selv om holdet skulle mangle (sveiper_utsatt=0, sveiper_kjort=0), regnes den som holdt tilbake: boksen og knappen er der."""
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[1])
    con.execute("UPDATE paamelding SET sveiper_utsatt=0, sveiper_kjort=0 WHERE id=?", (eva,))
    con.commit()
    k = admin_klient()
    assert "Behandling holdt tilbake" in k.get(f"/admin/kurs/{kid}/deltaker/{eva}").get_data(as_text=True)
    assert behandling.klassifiser(con, con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone(),
                                  con.execute("SELECT p.*, d.epost FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE p.id=?", (eva,)).fetchone()) is None
    k.post(f"/admin/kurs/{kid}/deltaker/{eva}/behandle")
    con.commit()
    assert typer(con, "Eva") == ["bekreftelse"] and _tilstand(con, eva) == (1, 0)


@pytest.mark.parametrize("seed", range(8))
def test_en_ekstradeltaker_er_alltid_behandlet_eller_holdt_tilbake(tmp_path, monkeypatch, seed):
    """Tilfeldige sekvenser av registrering, statusendringer, manuell sending (også når malen eller e-posttjenesten feiler), morgenjobb og
    avklaring av uavklarte e-poster: en ekstradeltaker har alltid sveiper_kjort=1 ELLER sveiper_utsatt=1, og får aldri automatisk faktura."""
    rnd = random.Random(7000 + seed)
    con = ny_database(tmp_path, monkeypatch)
    kid = kurs_med_samlinger(con, kode=f"HLD{seed}", kapasitet=rnd.choice([2, 3, None]))
    modus = {"feil": None}

    def send(til, emne, html, *a, **kw):
        if modus["feil"] == "tjeneste":
            raise RuntimeError("Graph svarte ikke")
    monkeypatch.setattr(epost, "send", send)
    navn = [f"H{i}" for i in range(6)]
    pid: dict = {}

    def fakturaer():
        return {r["id"]: (bool(r["ekstradeltaker_ts"]), _faktura(con, r["id"]))
                for r in con.execute("SELECT id, ekstradeltaker_ts FROM paamelding WHERE kurs_id=?", (kid,))}

    for steg in range(60):
        for_ = fakturaer()
        h = rnd.choice(["ny", "ny_ekstra", "sett", "sett", "sett", "behandle", "behandle", "morgenjobb", "feilmodus", "avklar"])
        try:
            if h in ("ny", "ny_ekstra"):
                n = rnd.choice(navn)
                try:
                    kw = {"ekstradeltaker": True} if h == "ny_ekstra" else {"paamelding": {"sveiper_utsatt": rnd.choice([0, 1])}}
                    pid[n], _ = db.meld_paa(con, kid, epost=epost_til(n), fornavn=n, etternavn="Test", aktor="admin:k", **kw)
                    con.commit()
                except db.Paameldingsfeil:
                    con.rollback()
            elif h == "sett" and pid:
                n = rnd.choice(list(pid))
                try:
                    opp = db.sett_paamelding_status(con, pid[n], rnd.choice(list(db.PAAMELDINGSSTATUSER)), aktor="admin:k",
                                                    tillat_overbooking=rnd.random() < 0.5)
                    con.commit()
                    if opp:                        # slik ruten gjør det: den som fikk plass, kjøres med en gang
                        sveiper.kjor(Kjoring(con, idag=IDAG), opp)
                        con.commit()
                except db.Paameldingsfeil:
                    con.rollback()
            elif h == "behandle" and pid:
                behandling.behandle_holdt_paamelding(con, kid, pid[rnd.choice(list(pid))], IDAG, aktor="admin:k")
            elif h == "morgenjobb":
                sveiper.kjor(Kjoring(con, idag=IDAG))
                con.commit()
            elif h == "feilmodus":
                modus["feil"] = rnd.choice([None, None, "tjeneste", "mal"])
                con.execute("DELETE FROM mal_tekst WHERE mal='bekreftelse'")
                if modus["feil"] == "mal":
                    con.execute("INSERT INTO mal_tekst (mal, felt, tekst) VALUES ('bekreftelse', 'innledning', 'Hei {ukjent}')")
                con.commit()
            elif h == "avklar":
                n = rnd.choice(navn)
                for typ in ("bekreftelse", "venteliste"):
                    db.avklar_epost(con, f"kurs:{kid}", epost_til(n), typ, sendt=rnd.random() < 0.5, aktor="admin:k")
                con.commit()
        except Exception:  # noqa: BLE001 - en sekvens som ender i en feil, ruller tilbake som ruten ville gjort
            con.rollback()
        etter = fakturaer()
        for r in con.execute("SELECT d.fornavn, p.* FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE p.kurs_id=?", (kid,)):
            if r["ekstradeltaker_ts"]:
                assert r["sveiper_kjort"] or r["sveiper_utsatt"], (seed, steg, h, r["fornavn"], dict(r))       # aldri en blindvei
        for i, (var_ekstra, n_for) in for_.items():
            if var_ekstra and i in etter and etter[i][0]:                                                      # var og er ekstradeltaker
                assert etter[i][1] <= n_for, (seed, steg, h, i)                                                # ... og fikk aldri en ny faktura


# ============================ 2 og 17. Påmeldt <-> Ekstradeltaker på lukkede kurs ============================

@pytest.mark.parametrize("kursstatus", ["avsluttet", "avlyst"])
def test_paameldt_og_ekstradeltaker_bytter_merkelapp_ogsaa_paa_et_lukket_kurs(con, kursstatus):
    kid = kurs_med_samlinger(con, kapasitet=2)
    ola, pia = meld(con, kid, "Ola"), meld(con, kid, "Pia")                   # fullt: 2 av 2
    eva = ekstra(con, kid, "Eva", nr=[1])
    v = meld(con, kid, "Vera")                                                 # venteliste (Pia og Ola fyller kurset)
    assert status(con, v) == "venteliste"
    _kursstatus(con, kid, kursstatus)
    # Påmeldt -> Ekstradeltaker: bare merkelappen. Ingen rykker opp fra ventelisten, ingenting sendes.
    assert sett(con, ola, "ekstradeltaker", samlinger=[samling_ider(con, kid)[1]]) is None
    assert status(con, ola) == "ekstradeltaker" and dager_for(con, ola) == DAGER[2] and status(con, v) == "venteliste"
    # Ekstradeltaker -> Påmeldt: ingen kapasitetssjekk (ingen ny kommer på kurset), utvalget ryddes, ingenting kjøres
    assert sett(con, eva, "paameldt") is None
    assert status(con, eva) == "paameldt" and utvalg_rader(con, eva) == [] and dager_for(con, eva) == sum(DAGER.values(), [])
    assert db.antall_bekreftet(con, kid) == 2                                  # 2 av 2 (Pia og Eva), og det er greit: kurset er lukket
    assert typer(con, "Eva") == [] and _faktura(con, eva) == 0
    # ... men en NY person kommer ikke på kurset
    for gammel_ny in (("venteliste", "paameldt"), ("venteliste", "ekstradeltaker")):
        with pytest.raises(db.Paameldingsfeil, match=f"Kurset er {kursstatus} – kan ikke melde på flere"):
            sett(con, v, gammel_ny[1])
        con.rollback()
    assert status(con, v) == "venteliste"


def test_retur_fra_ekstradeltaker_paa_lukket_kurs_holder_en_ubehandlet_tilbake(con):
    """Et avsluttet kurs plukkes opp av morgenjobbens gjenoppretting: en ubehandlet ekstradeltaker som blir Påmeldt etter kursslutt, skal
    ikke få bekreftelse og faktura av seg selv."""
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[1])
    assert _tilstand(con, eva) == (0, 1)
    _kursstatus(con, kid, "avsluttet")
    assert sett(con, eva, "paameldt") is None
    assert _tilstand(con, eva) == (0, 1)
    sveiper.kjor(Kjoring(con, idag=date(2032, 1, 10)))
    daglig.kjor(Kjoring(con, idag=date(2032, 1, 10)))
    con.commit()
    assert typer(con, "Eva") == [] and _faktura(con, eva) == 0


@pytest.mark.parametrize("kursstatus", ["avsluttet", "avlyst"])
def test_listen_og_ruten_for_paameldt_og_ekstradeltaker_paa_lukket_kurs(con, kursstatus):
    kid = kurs_med_samlinger(con)
    ola, eva, tor = meld(con, kid, "Ola"), ekstra(con, kid, "Eva", nr=[1]), meld(con, kid, "Tor")
    sett(con, tor, "avmeldt")
    _kursstatus(con, kid, kursstatus)
    k = admin_klient()
    liste = k.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    tekster = re.findall(r'<option value="(paameldt|ekstradeltaker)" data-bekreft="([^"]+)"', liste)
    assert any("bare merkelappen endres" in unescape(t) for _, t in tekster)                        # dialogen sier det som skjer
    # Påmeldt -> Ekstradeltaker går via steget (kurset har flere samlinger), og virker
    r = k.post(f"/admin/kurs/{kid}/deltaker/{ola}/status", data={"status": "ekstradeltaker", "forventet": "paameldt"})
    assert r.headers["Location"].endswith(f"/deltaker/{ola}/ekstradeltaker")
    step = k.get(r.headers["Location"])
    assert step.status_code == 200 and "bare merkelappen endres" in synlig_tekst(step.get_data(as_text=True))
    r = k.post(f"/admin/kurs/{kid}/deltaker/{ola}/status", data={"status": "ekstradeltaker", "forventet": "paameldt", "samlingsvalg": "1", "utvalg": "hele"})
    assert status(con, ola) == "ekstradeltaker" and "Status endret til «ekstradeltaker»" in _meldinger(k, r)
    r = k.post(f"/admin/kurs/{kid}/deltaker/{eva}/status", data={"status": "paameldt", "forventet": "ekstradeltaker"})
    meldinger = _meldinger(k, r)
    assert status(con, eva) == "paameldt" and "Status endret til «påmeldt»" in meldinger
    assert f"Kurset er {kursstatus}: bare merkelappen er endret. Ingenting er sendt eller fakturert." in meldinger
    # En ny person (avmeldt -> ekstradeltaker) avvises FØR steget for samlingsvalg: ingen skjema som uansett feiler
    r = k.post(f"/admin/kurs/{kid}/deltaker/{tor}/status", data={"status": "ekstradeltaker", "forventet": "avmeldt"})
    assert not r.headers["Location"].endswith("/ekstradeltaker") and status(con, tor) == "avmeldt"
    assert f"Kurset er {kursstatus} – kan ikke melde på flere." in _meldinger(k, r)
    direkte = k.get(f"/admin/kurs/{kid}/deltaker/{tor}/ekstradeltaker")                                # også om steget åpnes direkte
    assert direkte.status_code == 302 and "/ekstradeltaker" not in direkte.headers["Location"]
    assert f"Kurset er {kursstatus} – kan ikke melde på flere." in _meldinger(k, direkte)


# ============================ 3. kapasitetssjekken er ikke et kappløp ============================

def _kjor_samtidig(sti, oppgaver):
    """Hver oppgave får sin egen tilkobling (som egne forespørsler), starter samtidig og committer selv. Gir [None | «FEIL: ...»]."""
    barriere = threading.Barrier(len(oppgaver))
    resultat: list = [None] * len(oppgaver)

    def arbeid(i, f):
        c = db.koble(sti)
        if not db.er_postgres(c):
            c.execute("PRAGMA busy_timeout = 60000")        # ventetid på skrivelåsen (en belastet maskin skal ikke gi falske feil)
        try:
            barriere.wait()
            try:
                f(c)
                c.commit()
            except db.Paameldingsfeil as e:
                c.rollback()
                resultat[i] = f"FEIL: {e}"
            except sqlite3.Error as e:
                c.rollback()
                resultat[i] = f"DATABASEFEIL: {e}"
        finally:
            c.close()

    trader = [threading.Thread(target=arbeid, args=(i, f)) for i, f in enumerate(oppgaver)]
    for t in trader:
        t.start()
    for t in trader:
        t.join()
    return resultat


@pytest.mark.parametrize("runde", range(5))
def test_samtidig_retur_fra_ekstradeltaker_overbooker_ikke_uten_spoersmaal(tmp_path, monkeypatch, runde):
    con = ny_database(tmp_path, monkeypatch)
    kid = kurs_med_samlinger(con, kode=f"SAM{runde}", kapasitet=3)
    meld(con, kid, "P0"), meld(con, kid, "P1")
    e = [ekstra(con, kid, f"E{i}") for i in range(6)]
    res = _kjor_samtidig(tmp_path / "test.db",
                         [(lambda pid: (lambda c: db.sett_paamelding_status(c, pid, "paameldt", aktor=ADMIN)))(pid) for pid in e])
    assert not [r for r in res if r and r.startswith("DATABASEFEIL")], res
    ny = db.koble(tmp_path / "test.db")
    assert db.antall_bekreftet(ny, kid) == 3, res                      # akkurat én fikk den siste plassen (2 + 1), de andre fikk spørsmålet
    assert sum(1 for r in res if r and "Kurset er fullt" in r) == 5
    ny.close()


@pytest.mark.parametrize("runde", range(5))
def test_samtidige_paameldinger_overbooker_ikke(tmp_path, monkeypatch, runde):
    """Åpningsdagen: flere melder seg på samtidig når det bare er to plasser igjen. Resten havner på ventelisten."""
    con = ny_database(tmp_path, monkeypatch)
    kid = kurs_med_samlinger(con, kode=f"PAA{runde}", kapasitet=3)
    meld(con, kid, "P0")
    res = _kjor_samtidig(tmp_path / "test.db", [
        (lambda n: (lambda c: db.meld_paa(c, kid, epost=epost_til(n), fornavn=n, etternavn="Test")))(f"N{i}") for i in range(6)])
    assert not [r for r in res if r], res
    ny = db.koble(tmp_path / "test.db")
    assert db.antall_bekreftet(ny, kid) == 3
    assert antall(ny, "SELECT COUNT(*) FROM paamelding WHERE kurs_id=? AND status='venteliste'", kid) == 4
    ny.close()


def test_kurset_laases_foer_plassene_telles(con):
    """Låsen er en UPDATE som ikke endrer noe: verken status eller andre felt på kurset røres."""
    kid = kurs_med_samlinger(con)
    for_ = tuple(con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone())
    db._laas_kurs(con, kid)
    assert tuple(con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()) == for_
    assert con.in_transaction                                          # SQLite har tatt skrivelåsen nå (til commit/rollback)
    con.rollback()


# ============================ 4. «Lagre samlinger» har versjonskontroll ============================

def test_lagre_samlinger_kontrollerer_versjonen_som_de_andre_skjemaene(con):
    kid = kurs_med_samlinger(con)
    s1, s2, s3 = samling_ider(con, kid)
    eva = ekstra(con, kid, "Eva", nr=[1])
    a, b = admin_klient(), admin_klient()
    side_a = a.get(f"/admin/kurs/{kid}/deltaker/{eva}").get_data(as_text=True)
    versjon_a = re.search(r'data-skjemanavn="Samlinger">.*?name="versjon" value="([0-9a-f]+)"', side_a, re.S).group(1)   # skjemaet HAR versjonsfelt
    side_b = b.get(f"/admin/kurs/{kid}/deltaker/{eva}").get_data(as_text=True)
    versjon_b = re.search(r'data-skjemanavn="Samlinger">.*?name="versjon" value="([0-9a-f]+)"', side_b, re.S).group(1)
    assert versjon_a == versjon_b
    # B lagrer «Samling 2 og 3» først ...
    r = b.post(f"/admin/kurs/{kid}/deltaker/{eva}/samlinger", data={"utvalg": "noen", "samling": [s2, s3], "versjon": versjon_b})
    assert utvalg_rader(con, eva) == sorted([s2, s3]) and "Samlingene er lagret" in _meldinger(b, r)
    # ... og A lagrer fra den gamle siden: ingenting lagres, og A får beskjed
    r = a.post(f"/admin/kurs/{kid}/deltaker/{eva}/samlinger", data={"utvalg": "noen", "samling": [s1], "versjon": versjon_a})
    assert utvalg_rader(con, eva) == sorted([s2, s3])                        # B sitt valg står
    assert "Samlingene ble ikke lagret: noen andre har endret dem mens du hadde siden åpen" in _meldinger(a, r)
    # Med den nye versjonen virker det; og et skjema uten versjonsfelt (eldre klient) kontrolleres ikke, som de andre skjemaene
    ny = re.search(r'data-skjemanavn="Samlinger">.*?name="versjon" value="([0-9a-f]+)"', a.get(f"/admin/kurs/{kid}/deltaker/{eva}").get_data(as_text=True), re.S).group(1)
    assert ny != versjon_a
    a.post(f"/admin/kurs/{kid}/deltaker/{eva}/samlinger", data={"utvalg": "noen", "samling": [s1], "versjon": ny})
    assert utvalg_rader(con, eva) == [s1]
    a.post(f"/admin/kurs/{kid}/deltaker/{eva}/samlinger", data={"utvalg": "hele"})
    assert utvalg_rader(con, eva) == []


def test_versjonen_endres_naar_ekstradeltakeren_byttes_eller_utvalget_endres(con):
    from kurs.web import app as webapp
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[1])
    v1 = webapp._utvalg_versjon(rad(con, eva), utvalg_rader(con, eva))
    assert v1 == webapp._utvalg_versjon(rad(con, eva), utvalg_rader(con, eva))                             # stabil
    sett(con, eva, "ekstradeltaker", samlinger=[samling_ider(con, kid)[0]])                                 # uendret
    db.sett_ekstradeltaker_samlinger(con, eva, [samling_ider(con, kid)[1]])
    assert webapp._utvalg_versjon(rad(con, eva), utvalg_rader(con, eva)) != v1


# ============================ 5. migrering 18 og ventelisten ============================

def test_migrering_18_logger_kurs_med_ekstradeltakere_og_venteliste_der_det_naa_er_ledige_plasser(con):
    kid = kurs_med_samlinger(con, kode="MIG1", kapasitet=3)
    meld(con, kid, "P0"), meld(con, kid, "P1"), meld(con, kid, "P2")                # fullt: 3 av 3
    v0, v1 = meld(con, kid, "V0"), meld(con, kid, "V1")
    assert status(con, v0) == status(con, v1) == "venteliste"
    # ekstradeltakere slik migrering 16 ga dem: en med plass som ikke teller lenger (de tok en plass før)
    con.execute("UPDATE paamelding SET ekstradeltaker_ts='2031-09-01 10:00:00' WHERE kurs_id=? AND id=?",
                (kid, con.execute("SELECT id FROM paamelding WHERE kurs_id=? AND status='bekreftet' ORDER BY id DESC", (kid,)).fetchone()[0]))
    andre = kurs_med_samlinger(con, kode="MIG2", kapasitet=3)                        # uten venteliste: ingenting å si fra om
    meld(con, andre, "Q0")
    ekstra(con, andre, "Q1")
    tredje = kurs_med_samlinger(con, kode="MIG3", kapasitet=2)                       # venteliste, men ingen ledig plass: fortsatt fullt
    meld(con, tredje, "R0"), meld(con, tredje, "R1"), meld(con, tredje, "R2")
    con.commit()
    con.execute("DROP TABLE ekstradeltaker_samling")
    con.execute("DELETE FROM schema_versjon WHERE versjon >= 18")
    con.execute("DELETE FROM hendelse WHERE handling='migrering_18_venteliste_maa_sjekkes'")
    con.commit()
    assert migreringer.kjor_manglende(con) == [n for n, _, _ in migreringer.MIGRERINGER if n >= 18]      # 18 og senere (ingen senere endrer dette)
    rader = [json.loads(r[0]) for r in con.execute("SELECT detaljer FROM hendelse WHERE handling='migrering_18_venteliste_maa_sjekkes'")]
    assert rader == [{"kurs_ider": [kid], "antall": 1}]                               # bare kurset med ledig plass og folk som venter
    assert "@" not in json.dumps(rader)                                                # bare kurs-id-er, aldri navn eller e-post
    assert db.antall_bekreftet(con, kid) == 2 and status(con, v0) == "venteliste"      # ingen er flyttet (det ville sendt e-post)


def test_migrering_18_er_dokumentert_med_konsekvensen_for_ventelisten():
    rad18 = next(linje for linje in (db._SCHEMA.parent.parent / "MIGRATIONS.md").read_text(encoding="utf-8").splitlines()
                 if linje.startswith("| 18 |"))
    assert "foran dem som venter" in rad18 and "migrering_18_venteliste_maa_sjekkes" in rad18 and "flytt opp" in rad18.lower()


# ============================ 6. testhull fra mutasjonsprøven: kapasitet ============================

def _lagre_oppsett(k, kid, kapasitet):
    r = k.post(f"/admin/kurs/{kid}/oppsett", data={"navn": "Veiledning i gruppe", "type": "fysisk", "sted": "Bergen", "kapasitet": str(kapasitet),
                                                     "pris_nok": "2500", "fakturering": "person", "betaling": "samlet", "faktura_dager_for": "14"})
    assert r.status_code == 302, r.get_data(as_text=True)[:300]


def _statuser(con, kid):
    return {x["fornavn"]: db.paameldingsstatus(x) for x in con.execute(
        "SELECT d.fornavn, p.* FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE p.kurs_id=? ORDER BY p.id", (kid,))}


def test_kapasitetsoekning_rykker_opp_noeyaktig_saa_mange_som_det_er_plass_til_og_ekstradeltakere_teller_ikke(con):
    kid = kurs_med_samlinger(con, kapasitet=3)
    for n in ("P0", "P1", "P2"):
        meld(con, kid, n)
    ekstra(con, kid, "E0"), ekstra(con, kid, "E1", nr=[1])
    meld(con, kid, "V0"), meld(con, kid, "V1")
    assert _kursstatus(con, kid) == "full"
    k = admin_klient(con)
    _lagre_oppsett(k, kid, 4)
    t = _statuser(con, kid)
    assert t["V0"] == "paameldt" and t["V1"] == "venteliste"                          # én plass: én rykker opp (ikke to: < og ikke <=)
    assert sum(1 for s in t.values() if s == "paameldt") == 4 and db.antall_bekreftet(con, kid) == 4
    assert _kursstatus(con, kid) == "full"                                            # 4 av 4 (>= og ikke >)
    _lagre_oppsett(k, kid, 6)
    assert _statuser(con, kid)["V1"] == "paameldt" and _kursstatus(con, kid) == "aapen"      # 5 av 6, og ekstradeltakerne er ikke med
    _lagre_oppsett(k, kid, 2)
    assert sum(1 for s in _statuser(con, kid).values() if s == "paameldt") == 5       # ingen fjernes automatisk
    assert _kursstatus(con, kid) == "full"


def test_kursstatus_full_eller_aapen_teller_ikke_ekstradeltakere(con):
    kid = kurs_med_samlinger(con, kapasitet=4)
    meld(con, kid, "P0"), meld(con, kid, "P1")
    ekstra(con, kid, "E0"), ekstra(con, kid, "E1", nr=[1])                            # 2 påmeldte + 2 ekstradeltakere
    assert _kursstatus(con, kid) == "aapen"
    k = admin_klient(con)
    _lagre_oppsett(k, kid, 3)
    assert _kursstatus(con, kid) == "aapen"                                           # 2 av 3: ledige plasser, selv om 4 er på kurset
    _lagre_oppsett(k, kid, 2)
    assert _kursstatus(con, kid) == "full"                                            # 2 av 2 (nøyaktig fullt)
    _lagre_oppsett(k, kid, 3)
    assert _kursstatus(con, kid) == "aapen"


def test_meldingen_om_overbooking_kommer_bare_naar_kurset_er_over_kapasitet_og_aldri_for_en_ekstradeltaker(con):
    kid = kurs_med_samlinger(con, kapasitet=3)
    meld(con, kid, "P0"), meld(con, kid, "P1")
    e = ekstra(con, kid, "E0")
    k = admin_klient()
    r = k.post(f"/admin/kurs/{kid}/deltaker/{e}/status", data={"status": "paameldt", "forventet": "ekstradeltaker"})
    meldinger = _meldinger(k, r)
    assert "Status endret til «påmeldt»" in meldinger and "Kurset har nå" not in meldinger           # 3 av 3: nøyaktig fullt er ikke over
    ny = meld(con, kid, "N0")                                                                          # venteliste
    r = k.post(f"/admin/kurs/{kid}/deltaker/{ny}/status", data={"status": "paameldt", "forventet": "venteliste", "overbooking": "1"})
    assert "Kurset har nå 4 påmeldte deltakere på 3 plasser" in _meldinger(k, r)                         # 4 av 3: over
    # Et kurs som er overbooket: å sette en annen til Ekstradeltaker gir ikke «fikk plass»-meldingen
    v = meld(con, kid, "V0")
    assert status(con, v) == "venteliste"
    r = k.post(f"/admin/kurs/{kid}/deltaker/{v}/status",
               data={"status": "ekstradeltaker", "forventet": "venteliste", "samlingsvalg": "1", "utvalg": "hele"})
    meldinger = _meldinger(k, r)
    assert "Tar ikke plass og teller ikke som påmeldt" in meldinger and "Kurset har nå" not in meldinger


def test_bare_paameldt_som_blir_ekstradeltaker_frigjoer_en_plass(con):
    """Mutasjonsprøven (overlevde): en hengende venteliste (ledig plass og noen som venter, for eksempel etter migrering 18) rykkes ikke opp
    av overganger der ingen plass ble ledig. Bare Påmeldt -> Ekstradeltaker frigjør en plass."""
    kid = kurs_med_samlinger(con, kapasitet=2)
    a, b = meld(con, kid, "A"), meld(con, kid, "B")
    v = meld(con, kid, "V")                                                    # venteliste (2 av 2)
    c = ekstra(con, kid, "C", nr=[1])                                          # en ekstradeltaker på hele tiden
    con.execute("UPDATE kurs SET kapasitet=3 WHERE id=?", (kid,))              # en ledig plass UTEN opprykk: hengende venteliste
    con.commit()
    assert status(con, v) == "venteliste" and db.antall_bekreftet(con, kid) == 2
    sett(con, c, "avmeldt")                                                    # en ekstradeltaker som forlater, tok ingen plass
    assert status(con, v) == "venteliste"
    sett(con, c, "ekstradeltaker")                                             # Avmeldt -> Ekstradeltaker: ingen plass ble ledig
    assert status(con, v) == "venteliste"
    sett(con, c, "venteliste")                                                 # Ekstradeltaker -> Venteliste: heller ingen
    assert status(con, v) == "venteliste"
    sett(con, a, "ekstradeltaker")                                             # Påmeldt -> Ekstradeltaker frigjør en plass: nå rykker V opp
    assert status(con, v) == "paameldt" and status(con, a) == "ekstradeltaker"
    assert status(con, b) == "paameldt" and db.antall_bekreftet(con, kid) == 2


def test_en_ny_ekstradeltaker_holdes_tilbake_allerede_i_meld_paa(con):
    """Holdet settes av db.meld_paa selv (ikke bare av ruten «Legg til deltaker»): ingen automatisk bekreftelse eller faktura uansett vei inn."""
    kid = kurs_med_samlinger(con)
    pid, status_ = db.meld_paa(con, kid, epost=epost_til("Eva"), fornavn="Eva", etternavn="Test", ekstradeltaker=True)
    con.commit()
    assert status_ == "bekreftet" and _tilstand(con, pid) == (0, 1)
    sveiper.kjor(Kjoring(con, idag=IDAG))
    daglig.kjor(Kjoring(con, idag=IDAG))
    con.commit()
    assert typer(con, "Eva") == [] and _faktura(con, pid) == 0


def _datoskjema(k, con, kid):
    """Kursdatoskjemaet slik siden sender det (uendret), med riktig versjon."""
    side = k.get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True)
    data = {"csrf_token": csrf(k), "kursdato_versjon": re.search(r'name="kursdato_versjon" value="([^"]*)"', side).group(1)}
    for i, s in enumerate(db.lagrede_samlinger(con, kid)):
        data.update({f"s-{i}-id": str(s.id), f"s-{i}-navn": s.navn or "", f"s-{i}-fra": s.fra.isoformat(), f"s-{i}-til": (s.til or s.fra).isoformat(),
                     f"s-{i}-start": s.start_kl, f"s-{i}-slutt": s.slutt_kl, f"s-{i}-timer": str(s.timer)})
    return data


def test_meldingen_etter_lagring_av_kursdatoer_teller_paameldte_og_ekstradeltakere_hver_for_seg(con):
    kid = kurs_med_samlinger(con)
    meld(con, kid, "P0")
    ekstra(con, kid, "E0"), ekstra(con, kid, "E1", nr=[1])
    k = admin_klient()
    r = k.post(f"/admin/kurs/{kid}/kursdatoer", data=_datoskjema(k, con, kid))
    meldinger = _meldinger(k, r)
    assert "Kurset har 1 påmeldte deltakere og 2 ekstradeltakere. De får ikke beskjed om endrede datoer automatisk" in meldinger


# ============================ 7. samlingsutvalget krymper ikke i det stille ============================

def _uten_id(samlinger, *indekser):
    from dataclasses import replace
    return [replace(s, id=None) if i in indekser else s for i, s in enumerate(samlinger)]


def test_en_gjenskapt_samling_med_samme_datoer_faar_utvalget_med_seg(con):
    """Administrator fjerner rad 1 og legger den inn igjen med nøyaktig de samme datoene (tom id, ny samling): ekstradeltakerne som var
    på samling 1 er fortsatt på den, og ingen får beskjed om noe som ikke er endret."""
    kid = kurs_med_samlinger(con)
    s1, s2, s3 = samling_ider(con, kid)
    eva, nina = ekstra(con, kid, "Eva", nr=[1, 3]), ekstra(con, kid, "Nina", nr=[2])
    res = db.lagre_samlinger(con, kid, _uten_id(db.lagrede_samlinger(con, kid), 0), aktor=ADMIN)
    con.commit()
    assert samling_ider(con, kid)[0] != s1 and s1 not in samling_ider(con, kid)                            # en ny samling
    assert res["utvalg_endret"] == []
    assert dager_for(con, eva) == DAGER[1] + DAGER[3] and dager_for(con, nina) == DAGER[2]
    assert len(utvalg_rader(con, eva)) == 2 and utvalg_rader(con, nina) == [s2]
    res = db.lagre_samlinger(con, kid, _uten_id(db.lagrede_samlinger(con, kid), 0, 1, 2), aktor=ADMIN)       # alle tre gjenskapt
    con.commit()
    assert res["utvalg_endret"] == [] and dager_for(con, eva) == DAGER[1] + DAGER[3] and dager_for(con, nina) == DAGER[2]


def test_gjenskapt_samling_der_ekstradeltakeren_bare_var_paa_den_stoppes_ikke_lenger(con):
    """Er samlingen gjenskapt med samme datoer, er utvalget bevart: da er det ingenting å stoppe (før ble dette avvist)."""
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[3])
    res = db.lagre_samlinger(con, kid, _uten_id(db.lagrede_samlinger(con, kid), 2), aktor=ADMIN)
    con.commit()
    assert res["utvalg_endret"] == [] and dager_for(con, eva) == DAGER[3]


def test_flyttede_dager_gir_beskjed_med_hvem_og_hvilke_dager_og_loggen_har_bare_antallet(con):
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[1, 3])
    ekstra(con, kid, "Una")                                                      # hele kurset: følger kurset, ingen beskjed
    k = admin_klient()
    data = _datoskjema(k, con, kid)
    data["s-0-fra"], data["s-0-til"] = "2031-10-15", "2031-10-16"               # samling 1 flyttes én dag tilbake: Eva mister 17.10. og får 15.10.
    r = k.post(f"/admin/kurs/{kid}/kursdatoer", data=data)
    meldinger = _meldinger(k, r)
    assert "Kursdatoene er lagret" in meldinger
    assert "Obs: 1 ekstradeltaker har fått andre kursdager fordi samlingene er endret. Eva Test: mistet 17.10.2031 og fikk 15.10.2031." in meldinger
    assert "Kontroller «Ekstradeltaker: deltar på» i deltakervinduet. Deltakeren får ikke beskjed om endringen automatisk." in meldinger
    assert "Una" not in meldinger
    assert dager_for(con, eva) == ["2031-10-15", "2031-10-16", "2031-12-04"]
    logg = con.execute("SELECT detaljer FROM hendelse WHERE handling='kursdager_endret' ORDER BY id DESC").fetchone()[0]
    assert json.loads(logg)["utvalg_endret_antall"] == 1 and "Eva" not in logg and "@" not in logg


def test_fjernes_en_samling_ekstradeltakeren_delvis_er_paa_faar_administrator_beskjed(con):
    kid = kurs_med_samlinger(con)
    eva, nina, una = ekstra(con, kid, "Eva", nr=[1, 3]), ekstra(con, kid, "Nina", nr=[1, 2]), ekstra(con, kid, "Una")
    k = admin_klient()
    data = _datoskjema(k, con, kid)
    for felt in [f for f in data if f.startswith("s-2-")]:                       # samling 3 fjernes
        del data[felt]
    r = k.post(f"/admin/kurs/{kid}/kursdatoer", data=data)
    meldinger = _meldinger(k, r)
    assert "Obs: 1 ekstradeltaker har fått andre kursdager" in meldinger and "Eva Test: mistet 04.12.2031." in meldinger
    assert "Nina" not in meldinger and "Una" not in meldinger                   # Nina: {1,2} = alle igjen = hele kurset, samme dager; Una: hele kurset
    assert dager_for(con, eva) == DAGER[1] and dager_for(con, nina) == DAGER[1] + DAGER[2] and utvalg_rader(con, nina) == []


def test_meldingen_om_endret_utvalg_nevner_hoeyst_fem_navn(con):
    kid = kurs_med_samlinger(con)
    for i in range(7):
        ekstra(con, kid, f"E{i}", nr=[1])
    k = admin_klient()
    data = _datoskjema(k, con, kid)
    data["s-0-fra"], data["s-0-til"] = "2031-10-15", "2031-10-16"
    meldinger = _meldinger(k, k.post(f"/admin/kurs/{kid}/kursdatoer", data=data))
    assert "Obs: 7 ekstradeltakere har fått andre kursdager" in meldinger and meldinger.count("mistet 17.10.2031") == 5 and "Og 2 til." in meldinger
    assert "Deltakerne får ikke beskjed om endringen automatisk." in meldinger


# ============================ 8. kalenderen viser ekstradeltakerne per samling ============================

def test_kalender_og_aarsplan_viser_ekstradeltakerne_som_er_paa_hver_samling(con):
    kid = kurs_med_samlinger(con, kapasitet=10)
    meld(con, kid, "Ola")
    ekstra(con, kid, "Eva", nr=[1, 3])
    ekstra(con, kid, "Nina", nr=[2])
    ekstra(con, kid, "Pia", nr=[2, 3])
    ekstra(con, kid, "Una")                                                      # hele kurset: er på alle tre
    hendelser = kurskalender.hent(con, date(2031, 10, 1), date(2031, 12, 31))
    assert [(h.nr, h.ekstradeltakere) for h in hendelser] == [(1, 2), (2, 3), (3, 3)]     # samling 1: Eva, Una / 2: Nina, Pia, Una / 3: Eva, Pia, Una
    assert [h.paameldte_tekst for h in hendelser] == ["1/10 påmeldte + 2 ekstradeltakere", "1/10 påmeldte + 3 ekstradeltakere",
                                                      "1/10 påmeldte + 3 ekstradeltakere"]
    k = admin_klient()
    november = synlig_tekst(k.get("/admin/aktiviteter/kalender?visning=maned&aar=2031&maned=11").get_data(as_text=True))
    assert "1/10 påmeldte + 3 ekstradeltakere" in november and "+ 2 ekstradeltakere" not in november
    oktober = synlig_tekst(k.get("/admin/aktiviteter/kalender?visning=maned&aar=2031&maned=10").get_data(as_text=True))
    assert "1/10 påmeldte + 2 ekstradeltakere" in oktober


def test_en_kursdag_uten_samling_telles_bare_for_ekstradeltakere_paa_hele_kurset(con):
    kid = kurs_med_samlinger(con, kapasitet=10)
    ekstra(con, kid, "Eva", nr=[1])
    ekstra(con, kid, "Una")
    con.execute("UPDATE kursdag SET samling_id=NULL WHERE kurs_id=? AND dato=?", (kid, DAGER[3][0]))              # eldre data: dagen har ingen samling
    con.commit()
    h = [x for x in kurskalender.hent(con, date(2031, 12, 1), date(2031, 12, 31))]
    assert [x.ekstradeltakere for x in h] == [1]                                 # Una, ikke Eva (som bare er på samling 1)


# ============================ 9 og 15. samlingsvalg som ikke hører sammen ============================

def _ny(k, kid, fornavn="Mia", **felt):
    return k.post(f"/admin/kurs/{kid}/deltaker/ny", follow_redirects=False, data={
        "fornavn": fornavn, "etternavn": "Test", "epost": epost_til(fornavn), "betaler": "person", "betaling": "samlet", **ADRESSE, **felt})


def _finnes(con, fornavn) -> bool:
    return bool(antall(con, "SELECT COUNT(*) FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE d.epost=?", epost_til(fornavn)))


def test_samlingsvalg_uten_avkryssing_av_ekstradeltaker_avvises_i_stedet_for_aa_ignoreres(con):
    kid = kurs_med_samlinger(con, kapasitet=1)
    meld(con, kid, "Ola")                                                        # kurset er fullt
    s1, s2, s3 = samling_ider(con, kid)
    k = admin_klient()
    r = _ny(k, kid, "Tor", utvalg="noen", samling=[s3])                         # samling avkrysset, men ikke «Registrer som ekstradeltaker»
    assert r.status_code == 400 and not _finnes(con, "Tor")                      # ingenting registrert (ikke en vanlig deltaker, ikke venteliste)
    side = r.get_data(as_text=True)
    assert "Du har valgt samlinger, men ikke krysset av for «Registrer som ekstradeltaker»" in synlig_tekst(side)
    assert re.search(rf'name="samling" value="{s3}" checked', side) and 'value="noen" checked' in side               # valget står igjen i skjemaet
    assert 'value="Tor"' in side
    assert _ny(k, kid, "Tor", utvalg="noen").status_code == 400 and not _finnes(con, "Tor")                          # «noen» uten avkryssing
    # Det vanlige (ingen samlinger valgt, «Hele kurset» står som standard) er uendret: en vanlig deltaker på et fullt kurs går på venteliste
    r = _ny(k, kid, "Kari", utvalg="hele")
    assert r.status_code == 302 and status(con, _pid(con, "Kari")) == "venteliste"
    # Med avkryssingen virker det
    r = _ny(k, kid, "Tor", ekstradeltaker="1", utvalg="noen", samling=[s3])
    assert r.status_code == 302 and status(con, _pid(con, "Tor")) == "ekstradeltaker" and utvalg_rader(con, _pid(con, "Tor")) == [s3]


def _pid(con, fornavn):
    return con.execute("SELECT p.id FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE d.epost=?", (epost_til(fornavn),)).fetchone()[0]


def test_motstridende_samlingsvalg_avvises_overalt(con):
    kid = kurs_med_samlinger(con)
    s1, s2, s3 = samling_ider(con, kid)
    eva = ekstra(con, kid, "Eva", nr=[1])
    k = admin_klient()
    # deltakervinduet: «Hele kurset» valgt, men en samling avkrysset
    r = k.post(f"/admin/kurs/{kid}/deltaker/{eva}/samlinger", data={"utvalg": "hele", "samling": [s2]})
    assert utvalg_rader(con, eva) == [s1]                                                                           # uendret
    assert "Du har krysset av samlinger, men «Hele kurset» er valgt" in _meldinger(k, r)
    # «Legg til deltaker»: som ekstradeltaker, «Hele kurset» valgt og en samling avkrysset
    r = _ny(k, kid, "Tor", ekstradeltaker="1", utvalg="hele", samling=[s2])
    assert r.status_code == 400 and not _finnes(con, "Tor")
    assert "Du har krysset av samlinger, men «Hele kurset» er valgt" in synlig_tekst(r.get_data(as_text=True))
    # det som er ment som «hele kurset» (ingen avkrysninger) virker som før
    r = _ny(k, kid, "Tor", ekstradeltaker="1", utvalg="hele")
    assert r.status_code == 302 and utvalg_rader(con, _pid(con, "Tor")) == []


def test_javascript_holder_samlingsvalget_ryddig_og_krysser_av_ekstradeltaker():
    js = (db._SCHEMA.parent / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'el.value === "hele"' in js and 'input[type=checkbox][name=samling]' in js and "b.checked = false" in js     # «Hele kurset» fjerner avkrysningene
    assert 'input[type=checkbox][name=ekstradeltaker]' in js and "ekstra.checked = true" in js                          # samlinger => ekstradeltaker avkrysset
    assert 'input[type=radio][name=utvalg][value=noen]' in js                                                           # en avkrysset samling => «Bare utvalgte»


# ============================ 10, 11 og 19. samlingsnavn og rekkefølge ============================

def _kurs_med_navn(con, kode="NAVN"):
    sam = [kursdatoer.Samling(date(2031, 10, 16), date(2031, 10, 17), "09:00", "16:00", 6.0, navn="Grunnkurs EMDR"),
           kursdatoer.Samling(date(2031, 11, 13), date(2031, 11, 14), "09:00", "16:00", 6.0, navn="Veiledning ved NTNU"),
           kursdatoer.Samling(date(2031, 12, 4), None, "10:00", "15:00", 5.0)]
    return kurs_med_samlinger(con, kode, samlinger=sam)


def test_i_setning_gjoer_bare_standardnavnene_smaa():
    assert db.i_setning("Samling 1 og 3") == "samling 1 og 3" and db.i_setning("Samling 2") == "samling 2"
    assert db.i_setning("Grunnkurs EMDR og Veiledning ved NTNU") == "Grunnkurs EMDR og Veiledning ved NTNU"
    assert db.i_setning("Hele kurset") == "Hele kurset" and db.i_setning("") == ""


def test_egne_samlingsnavn_beholdes_i_meldinger_og_vindu(con):
    kid = _kurs_med_navn(con)
    s1, s2, s3 = samling_ider(con, kid)
    k = admin_klient()
    pid = meld(con, kid, "Eva")
    r = k.post(f"/admin/kurs/{kid}/deltaker/{pid}/status",
               data={"status": "ekstradeltaker", "forventet": "paameldt", "samlingsvalg": "1", "utvalg": "noen", "samling": [s1, s2]})
    assert "Deltar på Grunnkurs EMDR og Veiledning ved NTNU." in _meldinger(k, r)
    vindu = k.get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
    assert "(Grunnkurs EMDR, Veiledning ved NTNU)" in synlig_tekst(vindu) and "grunnkurs emdr" not in vindu      # raw: ingen liten-bokstav-versjon
    r = k.post(f"/admin/kurs/{kid}/deltaker/{pid}/samlinger", data={"utvalg": "noen", "samling": [s3, s1]})
    assert "Deltar på Grunnkurs EMDR og Samling 3." in _meldinger(k, r)                                   # blandet: navnet først, ingen små bokstaver
    r = _ny(k, kid, "Tor", ekstradeltaker="1", utvalg="noen", samling=[s2])
    assert "deltar på Veiledning ved NTNU." in _meldinger(k, r)
    # standardnavnene skrives som før: «samling 1 og 3»
    kid2 = kurs_med_samlinger(con, "STD")
    ider = samling_ider(con, kid2)
    eva2 = meld(con, kid2, "Ann")
    r = k.post(f"/admin/kurs/{kid2}/deltaker/{eva2}/status",
               data={"status": "ekstradeltaker", "forventet": "paameldt", "samlingsvalg": "1", "utvalg": "noen", "samling": [ider[0], ider[2]]})
    assert "Deltar på samling 1 og 3." in _meldinger(k, r)


def test_kursbeviset_skriver_egne_samlingsnavn_som_de_staar(con):
    kid = _kurs_med_navn(con)
    eva = ekstra(con, kid, "Eva", nr=[1, 2])
    for d in (*DAGER[1], *DAGER[2]):
        db.registrer_oppmote(con, eva, next(x["id"] for x in db.kursdager(con, kid) if x["dato"] == d), "manuell")
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()
    kursbevis.kjor(Kjoring(con, idag=date(2031, 12, 20)))
    con.commit()
    html = con.execute("SELECT di.innhold FROM dokument x JOIN dokument_innhold di ON di.dokument_id=x.id WHERE x.kurs_id=? AND x.type='kursbevis'", (kid,)).fetchone()[0]
    html = html.decode("utf-8") if isinstance(html, bytes) else html
    assert "har deltatt på Grunnkurs EMDR og Veiledning ved NTNU i" in html and "emdr" not in html.replace("EMDR", "")
    assert "<td>Samlinger</td><td>Grunnkurs EMDR og Veiledning ved NTNU (av kursets 3)</td>" in html          # samme skrivemåte lenger ned
    kid2 = kurs_med_samlinger(con, "STD")                                                                      # standardnavn: som før
    ann = ekstra(con, kid2, "Ann", nr=[1, 3])
    for d in (*DAGER[1], *DAGER[3]):
        db.registrer_oppmote(con, ann, next(x["id"] for x in db.kursdager(con, kid2) if x["dato"] == d), "manuell")
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid2,))
    con.commit()
    kursbevis.kjor(Kjoring(con, idag=date(2031, 12, 20)))
    con.commit()
    html2 = con.execute("SELECT di.innhold FROM dokument x JOIN dokument_innhold di ON di.dokument_id=x.id WHERE x.kurs_id=? AND x.type='kursbevis'", (kid2,)).fetchone()[0]
    html2 = html2.decode("utf-8") if isinstance(html2, bytes) else html2
    assert "har deltatt på samling 1 og 3 i" in html2 and "<td>Samlinger</td><td>Samling 1 og 3 (av kursets 3)</td>" in html2


def test_samlingsteksten_foelger_datorekkefolgen_ogsaa_naar_en_tidligere_samling_faar_hoeyere_id(con):
    """En samling som legges til senere men ligger FØR de andre i tid, får høyest id men er «Samling 1». Alle steder skal si det samme."""
    kid = kurs_med_samlinger(con)
    lagrede = db.lagrede_samlinger(con, kid)
    lagrede.append(kursdatoer.Samling(date(2031, 9, 10), date(2031, 9, 11), "09:00", "16:00", 6.0))                # id 4, tidligst
    db.lagre_samlinger(con, kid, sorted(lagrede, key=lambda s: s.fra), aktor=ADMIN)
    con.commit()
    ider = {s["nr"]: s["id"] for s in db.kursets_samlinger(con, kid)}
    assert ider[1] > ider[2] > 0 and ider[1] == max(ider.values())                                                # ny samling 1 har høyest id
    eva = meld(con, kid, "Eva")
    sett(con, eva, "ekstradeltaker", samlinger=[ider[2], ider[1]])                                                  # gammel id 1 (nå «Samling 2») og ny id 4
    assert db.samlingsutvalg_tekst(con, eva) == "Samling 1, 2" and db.samlingsutvalg_tekst(con, eva, og=True) == "Samling 1 og 2"
    assert db.samlingsutvalg_for_kurs(con, kid)[eva] == [ider[1], ider[2]]                                          # datorekkefølge, ikke id
    k = admin_klient()
    liste = k.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    assert re.findall(r'utvalg-tekst"[^>]*>([^<]*)<', liste) == ["Samling 1, 2"]
    csv_ = list(csv.reader(io.StringIO(k.get(f"/admin/kurs/{kid}/deltakere.csv").get_data(as_text=True).lstrip("﻿")), delimiter=";"))
    hode = csv_[0]
    assert [r[hode.index("Samlinger")] for r in csv_[1:] if r[0].startswith("Eva")] == ["Samling 1, 2"]
    utskrift = list(csv.reader(io.StringIO(k.get(f"/admin/kurs/{kid}/deltakerliste.csv?valgt=1&kol=status&kol=samlinger&status=alle").get_data(as_text=True).lstrip("﻿")), delimiter=";"))
    assert any("Samling 1, 2" in r for r in utskrift)
    assert "Samling 2, 1" not in liste


# ============================ 12. fullverdige deltakere er uendret ============================

def test_kursstatus_full_settes_bare_naar_en_ekstradeltaker_tar_den_siste_plassen_tilbake(con):
    """Som før: Avmeldt/Venteliste -> Påmeldt på den siste plassen endrer ikke kursstatus (den blir «full» først når noen prøver å melde
    seg på et fullt kurs). Nytt er bare at en ekstradeltaker som blir Påmeldt igjen, setter kurset tilbake til «full»."""
    kid = kurs_med_samlinger(con, kapasitet=2)
    a, b = meld(con, kid, "A"), meld(con, kid, "B")
    assert db.antall_bekreftet(con, kid) == 2 and _kursstatus(con, kid) == "aapen"            # 2 av 2, men ingen har prøvd å melde seg på
    sett(con, b, "avmeldt")
    sett(con, b, "paameldt")                                                                    # siste plass tas via en statusendring: som før
    assert db.antall_bekreftet(con, kid) == 2 and _kursstatus(con, kid) == "aapen"
    sett(con, a, "ekstradeltaker")                                                              # en plass blir ledig (ingen venteliste)
    assert db.antall_bekreftet(con, kid) == 1 and _kursstatus(con, kid) == "aapen"
    sett(con, a, "paameldt")                                                                    # ekstradeltakeren tar plassen tilbake: nå er kurset fullt
    assert db.antall_bekreftet(con, kid) == 2 and _kursstatus(con, kid) == "full"
    v = meld(con, kid, "V")                                                                      # venteliste
    assert status(con, v) == "venteliste"
    sett(con, a, "ekstradeltaker")                                                              # plassen går til V, og kurset er fortsatt fullt
    assert status(con, v) == "paameldt" and _kursstatus(con, kid) == "full"


def test_timer_i_spesialistloep_bruker_dagens_egne_timer_som_kursbeviset(con):
    """Endret for ALLE (ikke bare ekstradeltakere): «Akkumulert i løpet» bruker dagens egne timer, samlingens, ellers kursets, slik
    kursbeviset selv gjør. Uten egne timer på samling eller dag er tallet det samme som før (antall dager × timer per dag)."""
    kid = kurs_med_samlinger(con, spesialistlop="EFT")
    ola = meld(con, kid, "Ola")
    for d in (DAGER[1][0], DAGER[3][0]):                                            # én dag med kursets timer (6) og samling 3 som har 5
        db.registrer_oppmote(con, ola, next(x["id"] for x in db.kursdager(con, kid) if x["dato"] == d), "manuell")
    con.commit()
    assert kursbevis.timer_i_lop(con, rad(con, ola)["deltaker_id"], "EFT") == 11.0
    kid2 = kurs_med_samlinger(con, "EFT2", spesialistlop="EFT", samlinger=[S(*S1, "09:00", "16:00", 6.0)])        # uten avvik: dager × timer
    ann = meld(con, kid2, "Ann")
    for d in DAGER[1]:
        db.registrer_oppmote(con, ann, next(x["id"] for x in db.kursdager(con, kid2) if x["dato"] == d), "manuell")
    con.commit()
    assert kursbevis.timer_i_lop(con, rad(con, ann)["deltaker_id"], "EFT") == 12.0


# ============================ 13. nedtonede dager sier det også i teksten ============================

def test_nedtonet_dag_i_oppmoetematrisen_har_tekst_paa_knappen(con):
    kid = kurs_med_samlinger(con)
    ekstra(con, kid, "Eva", nr=[1])
    meld(con, kid, "Ola")
    html = admin_klient().get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    matrise = html[html.index('<h2 id="oppmote">'):]
    rader = re.findall(r'<tr data-filtrer="oppmote".*?</tr>', matrise, re.S)
    eva, ola = (next(r for r in rader if navn in r) for navn in ("Eva", "Ola"))
    utenfor = re.findall(r'<td[^>]*class="dempet-dag"[^>]*>.*?</td>', eva, re.S)
    assert len(utenfor) == 3
    for celle in utenfor:
        knapp = re.search(r"<button[^>]*>(.*?)</button>", celle, re.S).group(1)
        assert "ikke registrert, utenfor utvalget – teller ikke" in synlig_tekst(knapp)                              # tekst, ikke bare tooltip
        assert 'class="skjul-visuelt"' in knapp
    assert "utenfor utvalget" not in ola and "skjul-visuelt" not in ola                                                # Ola er på alle dager
    inne = re.findall(r"<button[^>]*>(.*?)</button>", eva, re.S)
    assert sum("utenfor utvalget" in b for b in inne) == 3 and len(inne) == 5                                         # 2 dager i utvalget, 3 utenfor


# ============================ 16. «Send bekreftelse nå» sier ikke «sendt» når ingenting gikk ut ============================

def test_send_bekreftelse_naa_sier_fra_naar_bekreftelsen_alt_var_sendt(con):
    kid = kurs_med_samlinger(con)
    kari = meld(con, kid, "Kari")
    sveiper.kjor(Kjoring(con, idag=IDAG))                                          # vanlig påmelding: bekreftelse og faktura
    con.commit()
    assert typer(con, "Kari") == ["bekreftelse"]
    sett(con, kari, "avmeldt")
    pid, _ = db.meld_paa(con, kid, epost=epost_til("Kari"), fornavn="Kari", etternavn="Test", ekstradeltaker=True, samlinger=[samling_ider(con, kid)[0]],
                         aktor=ADMIN)
    con.commit()
    assert pid == kari and status(con, pid) == "ekstradeltaker" and _tilstand(con, pid) == (0, 1)
    antall_for = antall(con, "SELECT COUNT(*) FROM sendt_epost WHERE til=?", epost_til("Kari"))
    k = admin_klient()
    r = k.post(f"/admin/kurs/{kid}/deltaker/{pid}/behandle")
    meldinger = _meldinger(k, r)
    assert "Bekreftelsen er ikke sendt på nytt: en bekreftelse til denne e-postadressen for dette kurset er sendt tidligere" in meldinger
    assert "Bekreftelsen er sendt, og den viser bare" not in meldinger and "Send e-post til valgte" in meldinger
    assert "Ekstradeltakere faktureres ikke automatisk: fakturaen lager du selv, utenfor systemet" in meldinger
    con.commit()
    assert antall(con, "SELECT COUNT(*) FROM sendt_epost WHERE til=?", epost_til("Kari")) == antall_for          # ingenting gikk ut
    assert typer(con, "Kari") == ["bekreftelse"] and _tilstand(con, pid) == (1, 0)                                # men holdet er opphevet


def test_behandlingsresultatet_vet_om_meldingen_var_sendt_fra_for(con):
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[1])
    res = behandling.behandle_holdt_paamelding(con, kid, eva, IDAG, aktor=ADMIN)
    assert res.kode == behandling.FULLFORT and res.epost_var_sendt is False                                          # sendt nå
    ola = meld(con, kid, "Ola", paamelding={"sveiper_utsatt": 1})
    res = behandling.behandle_holdt_paamelding(con, kid, ola, IDAG, aktor=ADMIN)
    assert res.kode == behandling.FULLFORT and res.epost_var_sendt is False
    # en vanlig Påmeldt som meldes på igjen etter avmelding: bekreftelsen fra forrige gang dedupliseres
    sett(con, ola, "avmeldt")
    db.meld_paa(con, kid, epost=epost_til("Ola"), fornavn="Ola", etternavn="Test", paamelding={"sveiper_utsatt": 1}, aktor=ADMIN)
    con.commit()
    res = behandling.behandle_holdt_paamelding(con, kid, ola, IDAG, aktor=ADMIN)
    assert res.kode == behandling.FULLFORT and res.epost_var_sendt is True
    k = admin_klient()
    sett(con, ola, "avmeldt")
    db.meld_paa(con, kid, epost=epost_til("Ola"), fornavn="Ola", etternavn="Test", paamelding={"sveiper_utsatt": 1}, aktor=ADMIN)
    con.commit()
    r = k.post(f"/admin/kurs/{kid}/deltaker/{ola}/behandle")
    assert "Bekreftelsen var allerede sendt til denne e-postadressen, så den er ikke sendt på nytt. Fakturering er behandlet." in _meldinger(k, r)


# ============================ 18. tekstene lover ikke en fakturafunksjon som ikke finnes ============================

def test_tekstene_om_faktura_for_ekstradeltakere_sier_at_administrator_lager_den_selv(con):
    kid = kurs_med_samlinger(con)
    meld(con, kid, "Ola")
    k = admin_klient()
    skjema = synlig_tekst(k.get(f"/admin/kurs/{kid}/deltaker/ny").get_data(as_text=True))
    assert "bekreftelsen kan du sende fra deltakervinduet, og fakturaen lager du selv, utenfor systemet" in skjema
    r = _ny(k, kid, "Mia", ekstradeltaker="1")
    melding = _meldinger(k, r)
    assert "Bekreftelsen kan du sende fra deltakervinduet («Send bekreftelse nå»); fakturaen lager du selv, utenfor systemet." in melding
    assert "send manuelt ved behov fra deltakervinduet" not in melding                                          # ingen løfte om en fakturaknapp
    pid = meld(con, kid, "Pia")
    r = k.post(f"/admin/kurs/{kid}/deltaker/{pid}/status", data={"status": "ekstradeltaker", "forventet": "paameldt", "samlingsvalg": "1", "utvalg": "hele"})
    assert "fakturaen lager du selv, utenfor systemet" in _meldinger(k, r)
    ola_dialog = re.search(r'fra Påmeldt til Ekstradeltaker\?[^"]*', unescape(k.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)))
    assert ola_dialog and "fakturaen lager du selv, utenfor systemet" in ola_dialog.group(0)


def test_fakturastatus_for_en_ekstradeltaker_uten_faktura_heter_faktureres_manuelt(con):
    kid = kurs_med_samlinger(con)
    ola, eva = meld(con, kid, "Ola"), ekstra(con, kid, "Eva", nr=[1])
    k = admin_klient()
    liste = k.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    rader = {n: r for n, r in zip(("Ola", "Eva"), re.findall(r'<tr data-filtrer="deltaker".*?</tr>', liste, re.S))}
    assert "Ikke fakturert" in rader["Ola"] and "Faktureres manuelt" not in rader["Ola"]
    assert "Faktureres manuelt" in rader["Eva"] and "Ikke fakturert" not in rader["Eva"]
    assert "Faktureres manuelt" in k.get(f"/admin/kurs/{kid}/deltaker/{eva}").get_data(as_text=True)
    sveiper.kjor(Kjoring(con, idag=IDAG), ola)                                                                   # en med faktura viser fakturastatusen som før
    con.commit()
    assert "Fakturert" in k.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    csv_ = list(csv.reader(io.StringIO(k.get(f"/admin/kurs/{kid}/deltakerliste.csv?valgt=1&kol=fakturastatus&status=alle").get_data(as_text=True).lstrip("﻿")),
                           delimiter=";"))
    assert any(r[-1] == "Faktureres manuelt" for r in csv_[1:]) and not any(r[-1] == "Ikke fakturert" and "Eva" in r[0] for r in csv_[1:])


def test_dialogtekstene_i_vinduet_tar_ikke_med_seg_navn_som_bryter_ut_av_attributtet(con):
    """Navnet står i dialogteksten, som ligger som JSON i data-status-tekster. Tegn som kan bryte ut av attributtet eller siden
    (anførselstegn, apostrof, vinkelparenteser, &) kommer escapet ut, og JSON-en kan fortsatt leses."""
    kid = kurs_med_samlinger(con)
    ondt = "Ola\"'><script>alert(1)</script>&amp;"
    pid, _ = db.meld_paa(con, kid, epost=epost_til("Ola"), fornavn=ondt, etternavn="Test", aktor=ADMIN)
    con.commit()
    html = admin_klient().get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
    attributt = re.search(r"data-status-tekster='([^']*)'", html)
    assert attributt and "<script>alert(1)" not in attributt.group(1) and "</script>" not in attributt.group(1)
    tekster = json.loads(unescape(attributt.group(1)))
    assert ondt in tekster["avmeldt"]                                                            # navnet er intakt etter at attributtet er lest
    assert html.count("<script>alert(1)") == 0


def test_spoersmaalet_om_overbooking_sier_ogsaa_hva_som_skjer_med_bekreftelse_og_faktura_ved_retur(con):
    """Et fullt kurs spør om overbooking FØR statusen endres, og det spørsmålet erstatter dialogteksten. Setter du en ekstradeltaker tilbake
    til Påmeldt, skal spørsmålet likevel si hva som skjer med bekreftelse og faktura (i listen og i deltakervinduet)."""
    kid = kurs_med_samlinger(con, kapasitet=2)
    meld(con, kid, "Ola"), meld(con, kid, "Pia")                              # fullt: 2 av 2
    eva = ekstra(con, kid, "Eva", nr=[1])
    ny = meld(con, kid, "Nina")                                                 # venteliste
    k = admin_klient()
    liste = unescape(k.get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True))
    rad_eva = re.search(rf'<form class="radstatus"[^>]*action="[^"]*/deltaker/{eva}/status"[^>]*>', liste).group(0)
    rad_nina = re.search(rf'<form class="radstatus"[^>]*action="[^"]*/deltaker/{ny}/status"[^>]*>', liste).group(0)
    assert "Kurset er fullt (2 av 2 plasser er tatt). Bekreftelsen er ikke sendt ennå. Den sendes nå automatisk" in rad_eva
    assert re.search(r'data-full-bekreft="Er du sikker på at du vil melde Nina Test på\? Kurset er fullt \(2 av 2 plasser er tatt\)\."', rad_nina)    # andre: som før
    vindu = unescape(k.get(f"/admin/kurs/{kid}/deltaker/{eva}").get_data(as_text=True))
    assert "Kurset er fullt (2 av 2 plasser er tatt). Bekreftelsen er ikke sendt ennå." in re.search(r"data-full-bekreft=\"([^\"]*)\"", vindu).group(1)
    k.post(f"/admin/kurs/{kid}/deltaker/{eva}/behandle")                       # bekreftelsen sendes manuelt: ingen faktura i systemet
    con.commit()
    vindu = unescape(k.get(f"/admin/kurs/{kid}/deltaker/{eva}").get_data(as_text=True))
    assert "lager ingen av seg selv" in re.search(r"data-full-bekreft=\"([^\"]*)\"", vindu).group(1)
    vindu_nina = unescape(k.get(f"/admin/kurs/{kid}/deltaker/{ny}").get_data(as_text=True))
    assert re.search(r"data-full-bekreft=\"([^\"]*)\"", vindu_nina).group(1) ==         "Er du sikker på at du vil melde denne deltakeren på? Kurset er fullt (2 av 2 plasser er tatt)."


def test_forhandsvisningen_sier_fra_naar_bekreftelsen_alt_er_sendt(con):
    """«Hva skjer» i bulk-forhåndsvisningen skal ikke love en e-post som motoren ikke sender (bekreftelsen dedupliseres)."""
    kid = kurs_med_samlinger(con)
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    kari = meld(con, kid, "Kari")
    sveiper.kjor(Kjoring(con, idag=IDAG))
    con.commit()
    sett(con, kari, "avmeldt")
    db.meld_paa(con, kid, epost=epost_til("Kari"), fornavn="Kari", etternavn="Test", paamelding={"sveiper_utsatt": 1}, aktor=ADMIN)
    ola = meld(con, kid, "Ola", paamelding={"sveiper_utsatt": 1})                    # ny: bekreftelsen er ikke sendt
    eva = ekstra(con, kid, "Eva", nr=[1])
    con.commit()
    rad_ = lambda pid: con.execute("SELECT p.*, d.epost FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE p.id=?", (pid,)).fetchone()  # noqa: E731
    assert behandling.forventet_handling(kurs, rad_(ola), con) == "Bekreftelse på e-post og fakturering"
    assert behandling.forventet_handling(kurs, rad_(kari), con) == "Fakturering (bekreftelsen er sendt fra før og sendes ikke på nytt)"
    assert behandling.forventet_handling(kurs, rad_(kari)) == "Bekreftelse på e-post og fakturering"             # uten tilkobling: som før
    assert behandling.forventet_handling(kurs, rad_(eva), con) == "Bekreftelse på e-post (ingen faktura for ekstradeltakere)"
    gratis = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    gratis = {**dict(gratis), "pris_nok": 0}
    assert behandling.forventet_handling(gratis, rad_(kari), con) == "Ingenting å sende (bekreftelsen er sendt fra før)"
    # en ekstradeltaker som alt har fått bekreftelsen (meldt av og registrert på nytt)
    sett(con, kari, "ekstradeltaker", samlinger=[samling_ider(con, kid)[0]])
    assert behandling.forventet_handling(kurs, rad_(kari), con) == "Ingen ny e-post (bekreftelsen er sendt fra før), ingen faktura for ekstradeltakere"
    side = synlig_tekst(admin_klient().post(f"/admin/kurs/{kid}/deltakere/bulk/forhandsvis", data={"paamelding_id": [ola, kari]}).get_data(as_text=True))
    assert "Bekreftelse på e-post og fakturering" in side and "Ingen ny e-post (bekreftelsen er sendt fra før), ingen faktura for ekstradeltakere" in side
