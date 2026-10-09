"""Ekstradeltakere: det som ble stående igjen etter de første delene (robusthet, innsjekkens dagnumre, Logger, retur til Påmeldt, filer).

  * kursdagene til en påmelding slås opp i databasen når raden mangler ekstradeltaker_ts (aldri «alle dager» ved en glemt kolonne),
    og en vakttest passer på at hver bruk av db.kursdager() er klassifisert: kursnivå (hele kurset) eller per deltaker;
  * innsjekk: resultatsiden og QR-siden bruker deltakerens egne dagnumre, QR-siden sier «ikke satt opp» med en gang (innlogget), og
    plakatens teller teller bare de som er satt opp på dagen;
  * Logger: «Registrert som ekstradeltaker» med hvor mye av kurset (bare tall);
  * retur fra Ekstradeltaker til Påmeldt: bekreftelsen sendes aldri to ganger, og en faktura lages aldri av seg selv når bekreftelsen
    alt er sendt manuelt (påmeldingen holdes tilbake, og administrator velger selv);
  * uke-før etter endret samlingsutvalg, filer utenfor utvalget, og tekstene som nevner ekstradeltakerne.
Bare oppdiktede data.
"""
import ast
import re
from datetime import date
from pathlib import Path

import pytest
from ekstrahjelp import (ADMIN, DAGER, S, antall, dager_for, ekstra, epost_til, kurs_med_samlinger, meld, rad, samling_ider, sett,
                         status, synlig_tekst, typer)
from kurssidehjelp import admin_klient, deltaker_klient, dokument, fast_dato, ny_database, pdf, skriv_side

from kurs import daglig, db, deltakerside, hendelseslogg, migreringer, sidelager, sveiper
from kurs import sideinnhold as si
from kurs.kjoring import Kjoring

ROT = Path(__file__).resolve().parent.parent
KURS = ROT / "kurs"
TEKST_IKKE_PAA = "Du er ikke satt opp på denne samlingen. Snakk med kursansvarlig."


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    fast_dato(monkeypatch)
    yield c
    c.close()


def _dag(con, kid: int, dato: str):
    return con.execute("SELECT * FROM kursdag WHERE kurs_id=? AND dato=?", (kid, dato)).fetchone()


def _did(con, pid: int) -> int:
    return rad(con, pid)["deltaker_id"]


def _innk(con, kid: int, idag: date):
    """Innkallingene morgenjobben sender for kurset på en gitt dag (uka før og dagen før)."""
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    dager = db.kursdager(con, kid)
    daglig._innkallinger(Kjoring(con, idag=idag), kurs, dager, date.fromisoformat(dager[0]["dato"]))
    con.commit()


def _faktura(con, pid: int) -> int:
    return antall(con, "SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", pid)


# ============================ robusthet: kursdagene til en påmelding ============================

def test_kursdagene_slaas_opp_i_databasen_naar_raden_mangler_ekstradeltaker_ts(con):
    kid = kurs_med_samlinger(con)
    eva, ola = ekstra(con, kid, "Eva", nr=[1]), meld(con, kid, "Ola")
    uten = con.execute("SELECT id, kurs_id FROM paamelding WHERE id=?", (eva,)).fetchone()        # en SELECT som glemte kolonnen
    assert "ekstradeltaker_ts" not in uten.keys()
    assert [d["dato"] for d in db.paameldingens_kursdager(con, uten)] == DAGER[1]                  # IKKE alle fem dagene
    assert [d["dato"] for d in db.paameldingens_kursdager(con, {"id": eva})] == DAGER[1]           # bare en id i en dict
    assert [d["dato"] for d in db.paameldingens_kursdager(con, eva)] == DAGER[1]                   # eller bare en id
    assert db.samlingsutvalg_tekst(con, uten) == "Samling 1"
    assert [d["dato"] for d in db.paameldingens_kursdager(con, {"id": ola})] == sum(DAGER.values(), [])       # Påmeldt: alle dager
    assert db.paameldingens_kursdager(con, 987654) == [] and db.paameldingens_kursdager(con, {"id": 987654}) == []   # ukjent påmelding


def test_bruken_av_kursdager_bekreftes_av_innsjekk_og_kursbevis_selv_om_raden_mangler_kolonnen(con):
    """De to stedene som slår opp dagene per deltaker med sin egen SELECT, gir riktig svar også uten kolonnen i raden."""
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[3])
    uten = con.execute("SELECT id, kurs_id FROM paamelding WHERE id=?", (eva,)).fetchone()
    assert dager_for(con, eva) == DAGER[3]
    assert [d["id"] for d in deltakerside.kursdager_for(con, {"id": kid, "start_kl": "09:00", "slutt_kl": "16:00"}, uten["id"])] == \
        [_dag(con, kid, DAGER[3][0])["id"]]


# ---- vakttesten: hver bruk av kursdagene er klassifisert ----

# Kall på db.kursdager() og deltakerside.kursdager_for(kurs) (uten påmelding) gjelder HELE kurset. Hver slik bruk står her med grunnen til at
# det er riktig. Nye bruk som gjelder en bestemt deltaker, skal bruke db.paameldingens_kursdager (eller kursdager_for med påmelding):
# ellers får en ekstradeltaker påminnelser, e-post, innsjekk, kursside og kursbevis for ALLE dagene.
KURSNIVAA = {
    ("kurs/web/app.py", "admin_qr_utskrift"): "QR-arkene til utskrift: én side per kursdag i hele kurset (05.10.2026)",
    ("kurs/evaluering.py", "runder"): "evalueringens runder: hele kurset eller hver samling; mottakerne på en samling filtreres per "
                                       "deltaker med db.paameldingens_kursdager (_er_paa_samling) (09.10.2026)",
    ("kurs/sveiper.py", "bekreftelse_eksempel"): "forhåndsvisningen av påmeldingsbekreftelsen i fanen Kommunikasjon: kursets dager med en "
                                                 "oppdiktet, vanlig deltaker (09.10.2026)",
    ("kurs/godkjenning.py", "_forste_dag"): "henter alle dagene og filtrerer for deltakeren med db.paameldingens_kursdager(..., alle): "
                                            "fristen for påminnelsen uka før i «Klar til sending» (05.10.2026)",
    ("kurs/daglig.py", "_kurs"): "morgenjobben: kursets første og siste dag (status, Zoom); mottakerne får sine egne dager via _mottakere",
    ("kurs/daglig.py", "_importer_zoom"): "henter alle dagene, og filtrerer per deltaker med db.paameldingens_kursdager(..., alle)",
    ("kurs/db.py", "kursets_samlinger"): "samlingene i hele kurset (utvalget velges blant dem)",
    ("kurs/db.py", "paameldingens_kursdager"): "selve funksjonen: alle dager for alle unntatt en ekstradeltaker med utvalg",
    ("kurs/deltakerliste.py", "hent"): "alle dagene, og hver rad får sine egne dager (egne_dager)",
    ("kurs/deltakerside.py", "dager_utenfor"): "alle dagene minus deltakerens egne (db.paameldingens_kursdager): dagene hen IKKE er på",
    ("kurs/deltakerside.py", "kursdager_for"): "grunnlaget: filtreres med db.paameldingens_kursdager når en påmelding er gitt",
    ("kurs/deltakerside.py", "oppmote_i_dag"): "kursets dager, til å avgjøre om det er kursdag i dag i det hele tatt; deltakerens egne dager brukes ved siden av",
    ("kurs/deltakerside.py", "registrer_selv"): "samme: er det kursdag i dag? Deltakerens egne dager avgjør om hen kan registrere seg",
    ("kurs/deltakerside.py", "bygg_visning"): "alle dager, til å finne dagene deltakeren IKKE er på (utenfor); forhåndsvisningen viser alle",
    ("kurs/kursbevis.py", "kjor"): "alle dagene, og hver deltaker får sine egne (db.paameldingens_kursdager(..., alle))",
    ("kurs/seed_demo.py", "_demo_kursside"): "demodata for hele kurset",
    ("kurs/seed_demo.py", "main"): "demodata for hele kurset",
    ("kurs/sveiper.py", "_lag_plan"): "fakturaplanen gjelder kursets dager; en ekstradeltaker får ingen automatisk faktura (PLAN_INGEN)",
    ("kurs/sveiper.py", "samlinger_til_fakturering"): "delfakturaene følger kursets samlinger",
    ("kurs/sveiper.py", "oppdater_faktura_hold_etter_kursdagendring"): "fakturadatoen følger kursets første dag",
    ("kurs/web/app.py", "kursside"): "den offentlige påmeldingssiden viser kurset, ikke en deltaker",
    ("kurs/web/app.py", "_foreslatt_belop"): "delbeløp per samling i kurset",
    ("kurs/web/app.py", "_render_oppsett"): "kursoppsettet (administrasjon): dagene i kurset",
    ("kurs/web/app.py", "admin_kurs_deltakere"): "kolonnene i oppmøtematrisen er kursets dager; hver rad nedtones utenfor sine egne",
    ("kurs/web/app.py", "admin_deltaker_ny"): "skjemaet viser kursets dager",
    ("kurs/web/app.py", "_deltakerliste"): "kolonnene i utskriften er kursets dager",
    ("kurs/web/app.py", "_render_ny_kurs"): "Opprett kurs: kopi av et kurs sine dager",
    ("kurs/web/kursside_admin_ruter.py", "_kontroll"): "kursside-editoren (administrasjon) ser hele kurset",
    ("kurs/web/kursside_admin_ruter.py", "_sidedata"): "kursside-editoren (administrasjon) ser hele kurset",
    ("kurs/web/kursside_admin_ruter.py", "admin_kursside_hent_fra"): "kursside-editoren (administrasjon) ser hele kurset",
    ("kurs/web/kursside_admin_ruter.py", "admin_kursside_mal"): "kursside-editoren (administrasjon) ser hele kurset",
    ("kurs/web/oppmoteliste_ruter.py", "oppmote_standard"): "oppmøtelisten: velger dagens dag blant KURSETS dager; hvem som står på listen styres av deltakere_for_dag",
    ("kurs/web/oppmoteliste_ruter.py", "oppmote_side"): "oppmøtelisten: dagfanene viser alle kursets dager; deltakerne på dagen velges av deltakere_for_dag (samlingsutvalget)",
}


class _Kursdagkall(ast.NodeVisitor):
    def __init__(self):
        self.stabel, self.funn = [], []

    def visit_FunctionDef(self, n):
        self.stabel.append(n.name)
        self.generic_visit(n)
        self.stabel.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, n):
        f = n.func
        navn = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
        per_deltaker = navn == "kursdager_for" and (len(n.args) >= 3 or any(k.arg == "paamelding_id" for k in n.keywords))
        if navn in ("kursdager", "kursdager_for") and not per_deltaker:
            self.funn.append((self.stabel[-1] if self.stabel else "<modul>", n.lineno))
        self.generic_visit(n)


def _kursdagkall(kilde: str) -> list[tuple[str, int]]:
    """(funksjon, linje) for alle kall som henter kursets dager uten å si hvilken deltaker det gjelder."""
    b = _Kursdagkall()
    b.visit(ast.parse(kilde))
    return b.funn


def test_vakttesten_finner_kall_som_henter_kursets_dager():
    kilde = ("def a(con, p):\n    return db.kursdager(con, 1)\n"
             "def b(con, p):\n    return db.paameldingens_kursdager(con, p)\n"
             "def c(con, k, p):\n    return deltakerside.kursdager_for(con, k, p)\n"
             "def d(con, k):\n    return deltakerside.kursdager_for(con, k)\n"
             "def e(con, k, p):\n    return kursdager_for(con, k, paamelding_id=p)\n")
    assert [f for f, _ in _kursdagkall(kilde)] == ["a", "d"]


def test_hver_bruk_av_kursets_dager_er_klassifisert_som_kursnivaa():
    funnet = {}
    for sti in sorted(KURS.rglob("*.py")):
        for funksjon, linje in _kursdagkall(sti.read_text(encoding="utf-8")):
            funnet.setdefault((sti.relative_to(ROT).as_posix(), funksjon), []).append(linje)
    nye = {n: linjer for n, linjer in funnet.items() if n not in KURSNIVAA}
    assert not nye, ("Nye kall som henter KURSETS dager: gjelder det en bestemt deltaker, bruk db.paameldingens_kursdager "
                     f"(ellers får en ekstradeltaker alle dager). Gjelder det hele kurset, legg det til i KURSNIVAA i denne testen: {nye}")
    borte = sorted(set(KURSNIVAA) - set(funnet))
    assert not borte, f"KURSNIVAA nevner kall som ikke finnes lenger (rydd opp): {borte}"


# ============================ innsjekk: dagnumre, QR-siden og plakaten ============================

def test_innsjekkresultatet_viser_deltakerens_egne_dagnumre_og_fremdrift(con, monkeypatch):
    kid = kurs_med_samlinger(con)
    ola, eva = meld(con, kid, "Ola"), ekstra(con, kid, "Eva", nr=[2])            # Eva: 13. og 14.11. = dag 1 og 2 av 2 blant hennes
    fast_dato(monkeypatch, date(2031, 11, 14))
    dag = _dag(con, kid, "2031-11-14")                                          # kursets dag 4 av 5
    url = f"/innsjekk/{dag['innsjekk_token']}"
    html_eva = deltaker_klient(_did(con, eva)).post(url, data={}).get_data(as_text=True)
    assert "Dag 2 av 2" in synlig_tekst(html_eva) and "Dag 4 av 5" not in synlig_tekst(html_eva)
    assert html_eva.count('<span class="inn-seg') == 2                            # fremdriftsstripen har én rute per av DERES dager
    html_ola = deltaker_klient(_did(con, ola)).post(url, data={}).get_data(as_text=True)
    assert "Dag 4 av 5" in synlig_tekst(html_ola) and html_ola.count('<span class="inn-seg') == 5      # Påmeldt: uendret
    fast_dato(monkeypatch, date(2031, 11, 13))
    ny = deltaker_klient().post(f"/innsjekk/{_dag(con, kid, '2031-11-13')['innsjekk_token']}", data={"epost": epost_til("Eva")})
    assert "Dag 1 av 2" in synlig_tekst(ny.get_data(as_text=True))                # også uinnlogget, når registreringen lykkes


def test_resultatsiden_for_en_dag_utenfor_utvalget_har_ingen_dagnummer(con, monkeypatch):
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[1])
    fast_dato(monkeypatch, date(2031, 11, 13))
    dag = _dag(con, kid, "2031-11-13")
    html = deltaker_klient(_did(con, eva)).post(f"/innsjekk/{dag['innsjekk_token']}", data={}).get_data(as_text=True)
    assert TEKST_IKKE_PAA in html and "Dag 3 av 5" not in html and "inn-fremdrift" not in html
    assert antall(con, "SELECT COUNT(*) FROM oppmote") == 0


def test_qr_siden_gir_innlogget_ekstradeltaker_beskjeden_med_en_gang_og_uten_knapp(con, monkeypatch):
    kid = kurs_med_samlinger(con)
    ola, eva = meld(con, kid, "Ola"), ekstra(con, kid, "Eva", nr=[1])
    fast_dato(monkeypatch, date(2031, 11, 13))                                   # kursdag, men samling 2
    url = f"/innsjekk/{_dag(con, kid, '2031-11-13')['innsjekk_token']}"
    r = deltaker_klient(_did(con, eva)).get(url)
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and TEKST_IKKE_PAA in synlig_tekst(html)
    assert "data-en-gang" not in html and "Dag 3 av 5" not in html                # ingen registreringsknapp, ingen dag hen ikke er på
    assert 'href="/min-side"' in html
    ola_html = deltaker_klient(_did(con, ola)).get(url).get_data(as_text=True)
    assert "data-en-gang" in ola_html and TEKST_IKKE_PAA not in ola_html and "Dag 3 av 5" in synlig_tekst(ola_html)
    uinnlogget = deltaker_klient().get(url).get_data(as_text=True)
    assert 'name="epost"' in uinnlogget and "samlingen" not in synlig_tekst(uinnlogget).lower()                # e-postskjema, ingen avsløring
    assert antall(con, "SELECT COUNT(*) FROM oppmote") == 0                       # GET skriver aldri


def test_qr_siden_paa_en_dag_i_utvalget_viser_deres_dagnummer_og_knappen(con, monkeypatch):
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[2])
    fast_dato(monkeypatch, date(2031, 11, 14))
    html = deltaker_klient(_did(con, eva)).get(f"/innsjekk/{_dag(con, kid, '2031-11-14')['innsjekk_token']}").get_data(as_text=True)
    assert "Dag 2 av 2" in synlig_tekst(html) and "data-en-gang" in html and TEKST_IKKE_PAA not in html


def test_plakatens_teller_teller_bare_de_som_er_satt_opp_paa_dagen(con):
    kid = kurs_med_samlinger(con)
    ola, eva, nina = meld(con, kid, "Ola"), ekstra(con, kid, "Eva", nr=[1]), ekstra(con, kid, "Nina", nr=[2])
    dag = _dag(con, kid, "2031-11-13")                                           # samling 2
    db.registrer_oppmote(con, ola, dag["id"], "qr")
    db.registrer_oppmote(con, nina, dag["id"], "qr")
    db.registrer_oppmote(con, eva, dag["id"], "manuell")                          # utenfor hennes samling: teller ikke
    con.commit()
    html = admin_klient().get(f"/admin/kurs/{kid}/qr/{dag['id']}").get_data(as_text=True)
    assert "✓ 2 sjekket inn" in synlig_tekst(html)
    assert "dag 3 av 5" in synlig_tekst(html)                                     # plakaten viser kursets eget dagnummer
    db.registrer_oppmote(con, eva, _dag(con, kid, "2031-10-16")["id"], "qr")
    con.commit()
    assert "✓ 1 sjekket inn" in synlig_tekst(admin_klient().get(f"/admin/kurs/{kid}/qr/{_dag(con, kid, '2031-10-16')['id']}").get_data(as_text=True))


# ============================ Logger ============================

def _logg(con, pid: int):
    return hendelseslogg.for_paamelding(con, rad(con, pid), systemadmin=False).rader


def test_registrering_som_ekstradeltaker_staar_i_logger_med_hvor_mye_av_kurset(con):
    kid = kurs_med_samlinger(con)
    ider = samling_ider(con, kid)
    ol = dict(aktor=ADMIN, tillat_utkast=True, ignorer_frist=True, ekstradeltaker=True)
    eva, _ = db.meld_paa(con, kid, epost=epost_til("Eva"), fornavn="Eva", etternavn="Test", samlinger=[ider[0], ider[2]], **ol)
    una, _ = db.meld_paa(con, kid, epost=epost_til("Una"), fornavn="Una", etternavn="Test", **ol)
    ola, _ = db.meld_paa(con, kid, epost=epost_til("Ola"), fornavn="Ola", etternavn="Test", aktor=ADMIN)
    con.commit()
    eva_rad, una_rad, ola_rad = _logg(con, eva)[-1], _logg(con, una)[-1], _logg(con, ola)[-1]
    assert eva_rad.hva == "Registrert som ekstradeltaker" and "Deltar på: 2 samlinger" in eva_rad.detaljer
    assert una_rad.hva == "Registrert som ekstradeltaker" and "Deltar på: hele kurset" in una_rad.detaljer
    assert ola_rad.hva == "Påmeldt" and not any(d.startswith("Deltar på") for d in ola_rad.detaljer)         # Påmeldt: som før
    bare = db.meld_paa(con, kid, epost=epost_til("Bo"), fornavn="Bo", etternavn="Test", samlinger=[ider[1]], **ol)[0]
    con.commit()
    assert "Deltar på: 1 samling" in _logg(con, bare)[-1].detaljer                                            # ental
    hendelse = con.execute("SELECT detaljer FROM hendelse WHERE handling='paamelding' ORDER BY id").fetchall()
    assert all("Test" not in h[0] and "@" not in h[0] for h in hendelse)                                      # bare id-er og tall, aldri navn


def test_ny_registrering_som_ekstradeltaker_etter_avmelding_heter_paa_nytt(con):
    kid = kurs_med_samlinger(con)
    eva, _ = db.meld_paa(con, kid, epost=epost_til("Eva"), fornavn="Eva", etternavn="Test", aktor=ADMIN, ekstradeltaker=True)
    db.meld_av(con, eva, aktor=ADMIN)
    db.meld_paa(con, kid, epost=epost_til("Eva"), fornavn="Eva", etternavn="Test", aktor=ADMIN, ekstradeltaker=True)
    con.commit()
    hva = [r.hva for r in _logg(con, eva)]
    assert "Registrert som ekstradeltaker på nytt" in hva and "Registrert som ekstradeltaker" in hva and "Avmeldt" in hva
    assert status(con, eva) == "ekstradeltaker" and dager_for(con, eva) == sum(DAGER.values(), [])


def test_legg_til_deltaker_som_ekstradeltaker_staar_i_logger_via_skjemaet(con):
    from adressehjelp import ADRESSE
    kid = kurs_med_samlinger(con)
    ider = samling_ider(con, kid)
    k = admin_klient()
    r = k.post(f"/admin/kurs/{kid}/deltaker/ny", data={"fornavn": "Eva", "etternavn": "Test", "epost": epost_til("Eva"), "betaler": "person",
                                                       "betaling": "samlet", **ADRESSE, "ekstradeltaker": "1", "utvalg": "noen",
                                                       "samling": [ider[0]]})
    assert r.status_code == 302
    pid = con.execute("SELECT id FROM paamelding").fetchone()[0]
    rader = _logg(con, pid)
    assert rader[-1].hva == "Registrert som ekstradeltaker" and "Deltar på: 1 samling" in rader[-1].detaljer
    logger_html = k.get(f"/admin/kurs/{kid}/deltaker/{pid}/logger").get_data(as_text=True)
    assert "Registrert som ekstradeltaker" in synlig_tekst(logger_html)


# ============================ retur fra Ekstradeltaker til Påmeldt ============================
#
# Regelen (db.sett_paamelding_status): bekreftelsen sendes aldri to ganger, og en faktura lages ALDRI av seg selv når bekreftelsen alt er
# sendt manuelt (systemet ser ikke fakturaer som er laget utenfor det, f.eks. i Visma). Da holdes påmeldingen tilbake igjen, og
# administrator velger selv «Behandle fakturering nå».

def _send_manuelt(con, kid: int, pid: int):
    r = admin_klient().post(f"/admin/kurs/{kid}/deltaker/{pid}/behandle")
    assert r.status_code == 302
    con.commit()


def _flash(html: str) -> list[str]:
    return [synlig_tekst(m) for m in re.findall(r'<div class="flash[^"]*"[^>]*>(.*?)</div>', html, re.S)]


def test_retur_etter_manuell_bekreftelse_uten_faktura_holder_tilbake_og_lager_ingen_faktura_av_seg_selv(con, monkeypatch):
    fast_dato(monkeypatch, date(2031, 10, 1))                                                                           # «i dag» for ruten
    kid = kurs_med_samlinger(con, betaling="samlet")
    eva = ekstra(con, kid, "Eva", nr=[1])
    _send_manuelt(con, kid, eva)
    assert typer(con, "Eva") == ["bekreftelse"] and _faktura(con, eva) == 0 and rad(con, eva)["sveiper_kjort"] == 1    # manuelt: ingen faktura
    assert sett(con, eva, "paameldt") is None                                                                           # ingenting å kjøre nå
    assert (rad(con, eva)["sveiper_kjort"], rad(con, eva)["sveiper_utsatt"]) == (0, 1)                                  # holdt tilbake: administrator bestemmer
    sveiper.kjor(Kjoring(con, idag=date(2031, 10, 1)), eva)                                                              # umiddelbar kjøring (som ruten)
    for _ in range(2):                                                                                                   # daglig jobb: idempotent, og rører den ikke
        daglig.kjor(Kjoring(con, idag=date(2031, 10, 1)))
        con.commit()
    assert _faktura(con, eva) == 0 and typer(con, "Eva") == ["bekreftelse"]                                              # INGEN faktura av seg selv
    # Vinduet sier hva som gjenstår, og knappen gjelder bare fakturaen
    k = admin_klient()
    html = k.get(f"/admin/kurs/{kid}/deltaker/{eva}").get_data(as_text=True)
    boks = re.search(r'<div class="varselboks">.*?</div>', html, re.S).group(0)
    assert "Bekreftelsen er allerede sendt til denne e-postadressen og sendes ikke på nytt" in boks
    assert "Har du allerede fakturert i Visma, skal du ikke trykke på den" in boks and "Behandle fakturering nå" in boks
    # Administrator velger selv: fakturaen lages, bekreftelsen sendes ikke på nytt, og meldingen sier det som skjedde
    r = k.post(f"/admin/kurs/{kid}/deltaker/{eva}/behandle")
    melding = _flash(k.get(r.headers["Location"]).get_data(as_text=True))
    assert any("Bekreftelsen var allerede sendt til denne e-postadressen, så den er ikke sendt på nytt. Fakturering er behandlet." in m
               for m in melding), melding
    con.commit()
    assert typer(con, "Eva") == ["bekreftelse"] and _faktura(con, eva) == 1
    assert (rad(con, eva)["sveiper_kjort"], rad(con, eva)["sveiper_utsatt"]) == (1, 0)
    r = k.post(f"/admin/kurs/{kid}/deltaker/{eva}/behandle")                                                             # dobbeltklikk: ingenting mer
    assert "ikke lenger holdt tilbake" in " ".join(_flash(k.get(r.headers["Location"]).get_data(as_text=True)))
    con.commit()
    assert _faktura(con, eva) == 1


def test_retur_via_ruten_sier_fra_om_holdet_og_om_at_ingen_faktura_er_laget(con):
    kid = kurs_med_samlinger(con, betaling="samlet")
    eva = ekstra(con, kid, "Eva", nr=[1])
    _send_manuelt(con, kid, eva)
    k = admin_klient()
    r = k.post(f"/admin/kurs/{kid}/deltaker/{eva}/status", data={"status": "paameldt", "forventet": "ekstradeltaker"})
    meldinger = " ".join(_flash(k.get(r.headers["Location"]).get_data(as_text=True)))
    assert "Bekreftelsen er allerede sendt, og systemet har ingen faktura til deltakeren. Ingen faktura lages av seg selv" in meldinger
    assert "Har du fakturert deltakeren i Visma, skal du ikke gjøre det" in meldinger
    con.commit()
    assert _faktura(con, eva) == 0 and (rad(con, eva)["sveiper_kjort"], rad(con, eva)["sveiper_utsatt"]) == (0, 1)


def test_retur_etter_manuell_bekreftelse_per_samling_venter_paa_administrators_valg(con, monkeypatch):
    fast_dato(monkeypatch, date(2031, 10, 5))                                                                           # «i dag» for ruten: samling 1 er forfalt
    kid = kurs_med_samlinger(con, betaling="per_samling", faktura_dager_for=14)
    eva = ekstra(con, kid, "Eva")
    _send_manuelt(con, kid, eva)
    daglig.kjor(Kjoring(con, idag=date(2031, 10, 5)))                                                                    # samling 1 er forfalt: ingenting for en ekstradeltaker
    con.commit()
    assert _faktura(con, eva) == 0
    assert sett(con, eva, "paameldt") is None
    for _ in range(2):
        daglig.kjor(Kjoring(con, idag=date(2031, 10, 5)))                                                                # holdt: heller ingen «alle forfalte på én gang» neste morgen
        con.commit()
    assert _faktura(con, eva) == 0 and typer(con, "Eva") == ["bekreftelse"]
    k = admin_klient()
    assert "alle delfakturaene som er forfalt, lages da på én gang" in k.get(f"/admin/kurs/{kid}/deltaker/{eva}").get_data(as_text=True)
    k.post(f"/admin/kurs/{kid}/deltaker/{eva}/behandle")                                                                 # administrator velger selv
    con.commit()
    assert _faktura(con, eva) == 1 and typer(con, "Eva") == ["bekreftelse"]                                              # bare samling 1 er forfalt
    for _ in range(2):
        daglig.kjor(Kjoring(con, idag=date(2031, 10, 5)))
        con.commit()
    assert _faktura(con, eva) == 1
    daglig.kjor(Kjoring(con, idag=date(2031, 11, 5)))                                                                    # samling 2 forfaller: vanlig delfaktura
    con.commit()
    assert _faktura(con, eva) == 2


def test_retur_naar_bekreftelsen_ikke_er_behandlet_gaar_som_for_alle_andre(con, monkeypatch):
    fast_dato(monkeypatch, date(2031, 10, 1))                                                                           # «i dag» for ruten
    kid = kurs_med_samlinger(con, betaling="samlet")
    eva = ekstra(con, kid, "Eva", nr=[1])
    assert (rad(con, eva)["sveiper_kjort"], rad(con, eva)["sveiper_utsatt"]) == (0, 1) and typer(con, "Eva") == []
    k = admin_klient()
    r = k.post(f"/admin/kurs/{kid}/deltaker/{eva}/status", data={"status": "paameldt", "forventet": "ekstradeltaker"})
    meldinger = " ".join(_flash(k.get(r.headers["Location"]).get_data(as_text=True)))
    assert "Bekreftelse og faktura er behandlet etter kursets vanlige regler, som for alle andre påmeldte" in meldinger
    con.commit()
    assert typer(con, "Eva") == ["bekreftelse"] and _faktura(con, eva) == 1 and rad(con, eva)["sveiper_kjort"] == 1      # bekreftelse og faktura nå


def test_retur_med_faktura_fra_for_endrer_ingenting_og_krediterer_ikke(con):
    kid = kurs_med_samlinger(con, betaling="samlet")
    pia = meld(con, kid, "Pia")
    sveiper.kjor(Kjoring(con, idag=date(2031, 10, 1)))
    con.commit()
    assert _faktura(con, pia) == 1 and rad(con, pia)["sveiper_kjort"] == 1
    sett(con, pia, "ekstradeltaker")
    sett(con, pia, "paameldt")
    assert rad(con, pia)["sveiper_kjort"] == 1 and rad(con, pia)["sveiper_utsatt"] == 0                                 # ikke gjort klar på nytt, ikke holdt
    daglig.kjor(Kjoring(con, idag=date(2031, 10, 1)))
    con.commit()
    assert _faktura(con, pia) == 1 and typer(con, "Pia") == ["bekreftelse"]
    k = admin_klient()
    sett(con, pia, "ekstradeltaker")
    r = k.post(f"/admin/kurs/{kid}/deltaker/{pia}/status", data={"status": "paameldt", "forventet": "ekstradeltaker"})
    assert "Bekreftelse og faktura var allerede behandlet: ingenting er sendt på nytt" in " ".join(_flash(k.get(r.headers["Location"]).get_data(as_text=True)))


def test_retur_naar_kurset_ikke_faktureres_holdes_ikke_tilbake(con):
    kid = kurs_med_samlinger(con, pris_nok=0)
    eva = ekstra(con, kid, "Eva", nr=[1])
    _send_manuelt(con, kid, eva)
    assert sett(con, eva, "paameldt") is None
    assert (rad(con, eva)["sveiper_kjort"], rad(con, eva)["sveiper_utsatt"]) == (1, 0)                                  # ingen faktura å lage: ferdig
    assert _faktura(con, eva) == 0


def test_retur_naar_fakturaen_er_utsatt_holdes_tilbake_og_planen_beholdes(con):
    """Mer enn seks måneder til kurset: fakturaen er planlagt (faktura_tidligst_dato), men ikke laget. Kom ekstradeltakeren av en
    påmeldt, har systemet ingen faktura: retur holder tilbake (administrator kan ha fakturert selv), og planen beholdes uendret."""
    kid = kurs_med_samlinger(con, samlinger=[S(date(2032, 6, 1), date(2032, 6, 2), "09:00", "16:00", 6.0)], betaling="samlet")
    pia = meld(con, kid, "Pia")
    daglig.kjor(Kjoring(con, idag=date(2031, 10, 1)))                                                                    # mer enn seks måneder før: utsatt
    con.commit()
    tidligst = rad(con, pia)["faktura_tidligst_dato"]
    assert tidligst and _faktura(con, pia) == 0
    sett(con, pia, "ekstradeltaker")
    assert sett(con, pia, "paameldt") is None
    assert (rad(con, pia)["sveiper_kjort"], rad(con, pia)["sveiper_utsatt"]) == (0, 1)
    daglig.kjor(Kjoring(con, idag=date(2031, 12, 5)))                                                                    # datoen er nådd: ikke av seg selv
    con.commit()
    assert rad(con, pia)["faktura_tidligst_dato"] == tidligst and _faktura(con, pia) == 0
    admin_klient().post(f"/admin/kurs/{kid}/deltaker/{pia}/behandle")                                                    # administrator velger selv
    con.commit()
    assert rad(con, pia)["faktura_tidligst_dato"] == tidligst and typer(con, "Pia") == ["bekreftelse"]
    daglig.kjor(Kjoring(con, idag=date(2031, 12, 5)))                                                                    # planen virker som vanlig
    con.commit()
    assert _faktura(con, pia) == 1


def test_dialogen_for_retur_sier_hva_som_sendes(con):
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[1])
    html = admin_klient().get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    tekst = re.search(r'<option value="paameldt" data-bekreft="([^"]+)"', html).group(1)
    assert "Bekreftelsen er ikke sendt ennå. Den sendes nå automatisk" in tekst                                          # ikke behandlet: som for alle andre
    assert "Samlingsutvalget fjernes" in tekst and "ledig plass" in tekst
    _send_manuelt(con, kid, eva)                                                                                          # bekreftelsen sendt manuelt, ingen faktura
    html = admin_klient().get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    tekst = re.search(r'<option value="paameldt" data-bekreft="([^"]+)"', html).group(1)
    assert "lager ingen av seg selv" in tekst and "skal du ikke trykke" in tekst and "Bekreftelsen er allerede sendt" in tekst


def test_deltakervinduet_har_samme_dialogtekster_som_listen_for_hver_ny_status(con):
    """Vinduet sier bare «Endre status til påmeldt?» hvis valgene mangler tekst: static/app.js bruker data-status-tekster (JSON)."""
    import json
    from html import unescape
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[1])
    _send_manuelt(con, kid, eva)
    html = admin_klient().get(f"/admin/kurs/{kid}/deltaker/{eva}").get_data(as_text=True)
    tekster = json.loads(unescape(re.search(r"data-status-tekster='([^']*)'", html).group(1)))
    assert set(tekster) == {"paameldt", "venteliste", "avmeldt", "avslatt", "utgatt", "forlatt"}                         # alle unntatt dagens status
    assert "lager ingen av seg selv" in tekster["paameldt"] and "Endre status for Eva Test fra Ekstradeltaker til Påmeldt?" in tekster["paameldt"]
    assert "tok ingen plass, så ingen rykker opp" in tekster["avmeldt"]
    js = (KURS / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'getAttribute("data-status-tekster")' in js                                                                   # og JavaScript bruker dem
    assert 'option value="paameldt">' in html                                                                             # valgene selv er uendret (ingen data-bekreft)


# ============================ uke-før etter endret samlingsutvalg ============================

def test_endres_utvalget_etter_uka_foer_sendes_ikke_en_ny_uke_foer_men_dagen_foer_foelger_det_nye_utvalget(con):
    """Valg: uka-før har én fast type per kurs og mottaker og sendes bare én gang. Flyttes en ekstradeltaker til en annen samling etterpå,
    kommer dagen-før for den nye samlingen som vanlig, men ingen ny uke-før (admin kan sende en egen e-post)."""
    kid = kurs_med_samlinger(con)
    eva = ekstra(con, kid, "Eva", nr=[1])
    _innk(con, kid, date(2031, 10, 9))                                            # 7 dager før samling 1
    assert typer(con, "Eva") == ["ukefor"]
    db.sett_ekstradeltaker_samlinger(con, eva, [samling_ider(con, kid)[1]], aktor=ADMIN)                # flyttet til samling 2
    con.commit()
    _innk(con, kid, date(2031, 11, 6))                                            # 7 dager før samling 2
    assert typer(con, "Eva") == ["ukefor"]                                        # ingen ny uke-før
    _innk(con, kid, date(2031, 11, 12))                                           # dagen før 13.11.
    assert sorted(typer(con, "Eva")) == ["dagfor-2031-11-13", "ukefor"]
    _innk(con, kid, date(2031, 10, 15))                                           # dagen før 16.10. (samling 1: ikke lenger hennes)
    assert "dagfor-2031-10-16" not in typer(con, "Eva")


# ============================ filer utenfor utvalget ============================

def test_fil_knyttet_til_en_dag_utenfor_utvalget_hentes_ikke_via_filruten(con, monkeypatch):
    kid = kurs_med_samlinger(con)
    eva, ola = ekstra(con, kid, "Eva", nr=[1]), meld(con, kid, "Ola")
    alle = db.kursdager(con, kid)
    ute_id, inne_id = alle[2]["id"], alle[0]["id"]                                 # samling 2 (utenfor) og samling 1
    f_ute, _ = sidelager.lagre_fil(con, kid, "samling2.pdf", pdf(b"a"), ADMIN)
    f_inne, _ = sidelager.lagre_fil(con, kid, "samling1.pdf", pdf(b"b"), ADMIN)
    f_fri, _ = sidelager.lagre_fil(con, kid, "felles.pdf", pdf(b"c"), ADMIN)
    f_begge, _ = sidelager.lagre_fil(con, kid, "begge.pdf", pdf(b"d"), ADMIN)
    skriv_side(con, kid, dokument({"id": "", "type": "filer", "tittel": "Filer", "data": {"filer": [
        {"fil_id": f_ute, "tittel": "Bare samling 2", "gruppe": ute_id}, {"fil_id": f_inne, "tittel": "Samling 1", "gruppe": inne_id},
        {"fil_id": f_fri, "tittel": "Felles"}, {"fil_id": f_begge, "tittel": "Begge 1", "gruppe": ute_id},
        {"fil_id": f_begge, "tittel": "Begge 2", "gruppe": inne_id}]}}))
    kode = con.execute("SELECT kode FROM kurs WHERE id=?", (kid,)).fetchone()[0]
    k_eva, k_ola = deltaker_klient(_did(con, eva)), deltaker_klient(_did(con, ola))
    koder = {n: [k_eva.get(f"/kurs/{kode}/deltakerside/fil/{i}").status_code for i in (f_ute, f_inne, f_fri, f_begge)] for n in ("eva",)}
    assert koder["eva"] == [404, 200, 200, 200]                                     # bare samling 2-filen er stengt; filen som også er på samling 1 er åpen
    assert [k_ola.get(f"/kurs/{kode}/deltakerside/fil/{i}").status_code for i in (f_ute, f_inne, f_fri, f_begge)] == [200, 200, 200, 200]
    html = k_eva.get(f"/kurs/{kode}/deltakerside").get_data(as_text=True)
    assert "Bare samling 2" not in html and "Samling 1" in html and "Felles" in html   # samme regel i listen


def test_synlige_fil_ider_uten_utenfor_er_uendret():
    dok = dokument({"id": "b1", "type": "filer", "tittel": "F", "data": {"filer": [{"fil_id": 1, "tittel": "A", "gruppe": 7}, {"fil_id": 2, "tittel": "B"}]}})
    assert si.synlige_fil_ider(dok, date(2031, 10, 1)) == {1, 2}
    assert si.synlige_fil_ider(dok, date(2031, 10, 1), frozenset({7})) == {2}
    assert si.synlige_fil_ider(dok, date(2031, 10, 1), frozenset({8})) == {1, 2}


# ============================ tekstene som nevner ekstradeltakerne ============================

def test_avsla_dialogen_sier_at_en_ekstradeltaker_ikke_frigjoer_en_plass(con):
    kid = kurs_med_samlinger(con)
    ola, eva = meld(con, kid, "Ola"), ekstra(con, kid, "Eva", nr=[1])
    k = admin_klient()
    ola_html, eva_html = (k.get(f"/admin/kurs/{kid}/deltaker/{p}").get_data(as_text=True) for p in (ola, eva))
    assert "Plassen går til den første på ventelisten" in ola_html and "Plassen frigjøres og går til den første på ventelisten" in ola_html
    assert "tar ingen plass, så ingen rykker opp fra ventelisten" in eva_html
    assert "Plassen går til den første på ventelisten" not in eva_html and "Plassen frigjøres" not in eva_html


def test_avlysningen_og_type_og_sted_advarselen_nevner_ekstradeltakerne(con):
    kid = kurs_med_samlinger(con)
    meld(con, kid, "Ola")
    k = admin_klient()
    uten = k.get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True)
    assert "Alle påmeldte og de på venteliste varsles" in uten and 'data-type-sted-ekstra="0"' in uten
    ekstra(con, kid, "Eva", nr=[1])
    ekstra(con, kid, "Una")
    med = k.get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True)
    assert "Alle påmeldte, ekstradeltakere og de på venteliste varsles" in med
    assert 'data-type-sted-paameldte="1"' in med and 'data-type-sted-ekstra="2"' in med and 'data-type-sted-ekstra-ord="ekstradeltakere"' in med
    assert 'data-type-sted-vakt="3"' in med                                         # vakten slår til for alle tre
    js = (KURS / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "data-type-sted-paameldte" in js and "data-type-sted-ekstra-ord" in js    # meldingen bygges av tallene hver for seg


def test_bulk_forhandsvisningen_sier_at_ekstradeltakere_ikke_faktureres(con):
    kid = kurs_med_samlinger(con)
    ola = meld(con, kid, "Ola", paamelding={"sveiper_utsatt": 1})
    eva = ekstra(con, kid, "Eva", nr=[1, 3])
    k = admin_klient()
    blandet = k.post(f"/admin/kurs/{kid}/deltakere/bulk/forhandsvis", data={"paamelding_id": [ola, eva]}).get_data(as_text=True)
    tekst = synlig_tekst(blandet)
    assert "Ekstradeltakere (1) får bekreftelse på e-post med bare samlingene de deltar på. De faktureres ikke" in tekst
    assert "Bekreftelse på e-post (ingen faktura for ekstradeltakere)" in tekst
    assert "E-post sendes og faktura opprettes." in blandet
    bare = k.post(f"/admin/kurs/{kid}/deltakere/bulk/forhandsvis", data={"paamelding_id": [eva]}).get_data(as_text=True)
    assert "E-post sendes (ingen faktura opprettes)." in bare and "faktura opprettes. Dette" not in bare


def test_malbeskrivelsene_sier_hvem_som_faar_uka_foer_og_dagen_foer(con):
    html = admin_klient().get("/admin/e-postmaler").get_data(as_text=True)
    assert "Ekstradeltakere får den en uke før den første samlingen de er satt opp på." in synlig_tekst(html)
    assert "Ekstradeltakere får den bare dagen før kursdagene i samlingene de er satt opp på." in synlig_tekst(html)


# ============================ migrering 18: ubehandlede ekstradeltakere fra migrering 16 ============================

def test_migrering_18_holder_ubehandlede_ekstradeltakere_tilbake_og_roerer_ingen_andre(con):
    kid = kurs_med_samlinger(con)
    gammel = ekstra(con, kid, "Gammel")                                            # ekstradeltaker som ikke er behandlet
    ferdig = meld(con, kid, "Ferdig")
    sveiper.kjor(Kjoring(con, idag=date(2031, 10, 1)))                              # Ferdig får bekreftelse og faktura ...
    sett(con, ferdig, "ekstradeltaker")                                             # ... og blir så ekstradeltaker (ingenting å holde)
    ola = meld(con, kid, "Ola")                                                     # en vanlig Påmeldt som ikke er behandlet ennå
    # gjør databasen lik en fra før migrering 18: Gammel er en ekstradeltaker uten hold, slik migrering 16 laget den
    con.execute("UPDATE paamelding SET sveiper_utsatt=0 WHERE id=?", (gammel,))
    con.execute("DROP TABLE ekstradeltaker_samling")
    con.execute("DELETE FROM schema_versjon WHERE versjon >= 18")
    con.commit()
    flagg = lambda p: (rad(con, p)["sveiper_kjort"], rad(con, p)["sveiper_utsatt"])       # noqa: E731
    for_ = {p: flagg(p) for p in (gammel, ferdig, ola)}
    assert for_[gammel] == (0, 0) and for_[ferdig] == (1, 0) and for_[ola] == (0, 0)
    assert migreringer.kjor_manglende(con) == [n for n, _, _ in migreringer.MIGRERINGER if n >= 18]
    assert flagg(gammel) == (0, 1)                                                  # holdt tilbake: morgenjobben plukker den ikke opp
    assert flagg(ferdig) == for_[ferdig] and flagg(ola) == for_[ola]                # ingen andre er endret
    assert migreringer.kjor_manglende(con) == []
    migreringer._m18_ekstradeltaker_samling(con)                                     # idempotent
    con.commit()
    assert flagg(gammel) == (0, 1)
    html = admin_klient().get(f"/admin/kurs/{kid}/deltaker/{gammel}").get_data(as_text=True)
    assert "Behandling holdt tilbake" in html and "Send bekreftelse nå" in html     # admin kan sende manuelt
