"""Innsjekk (K9) og «Registrer oppmøte i dag» på nett (K10). Bare oppdiktede data.

K9: QR-koden i kurslokalet, én registrering per kursdag (databasen sier nei til nr. 2), identifisering med innlogget økt eller e-post (aldri navn),
«husk meg» er borte (QR gir aldri innlogging), QR-GET skriver aldri, takbegrensning og nøytrale svar til uinnloggede.
K10: innlogget deltaker registrerer selv for dagens kursdag (kilde «kode»): bare i dag, bare bekreftet påmelding, kode kreves på fysiske og hybride
kurs, digitale kurs med Zoom-import registreres av Zoom, én gang per dag, ingen id fra skjemaet (IDOR), takbegrenset per deltaker.
Kodesiden /innsjekk (innsjekk med kode og e-post uten innlogging) er fjernet: uten QR-kode registrerer deltakeren seg på Min side (K10).
"""
import json
import re
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from kurs import config, db, deltakerside, hendelseslogg
from kurs.kursbevis import timer_i_lop
from kurs.web import sikkerhet

from kurssidehjelp import IDAG, admin_klient, deltaker_klient, dokument, fast_dato, lag_deltaker, ny_database, skriv_side, tekstblokk

STATIC = Path(__file__).resolve().parent.parent / "kurs" / "web" / "static"
NEUTRAL = "Vi kunne ikke registrere oppmøte med denne e-postadressen."


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    fast_dato(monkeypatch)
    yield c
    c.close()


def _kurs(con, kode="K1", type_="fysisk", status="aapen", zoom_id=None, dager=2, start=IDAG, **felt) -> int:
    datoer = [(start + timedelta(days=n)).isoformat() for n in range(dager)]
    kid = db.opprett_kurs(con, kode=kode, navn=f"Kurs {kode}", datoer=datoer, type=type_, sted="Bergen", status=status,
                          zoom_id=zoom_id, sharepoint_mappe=f"Kurs/{kode}", **felt)
    con.commit()
    return kid


def _sett_kursstatus(con, kid, status):
    """Påmelding er ikke mulig på utkast, avlyste og avsluttede kurs: kurset får statusen etter at deltakerne er meldt på."""
    con.execute("UPDATE kurs SET status=? WHERE id=?", (status, kid))
    con.commit()


def _dag(con, kid, nr=0):
    return db.kursdager(con, kid)[nr]


def _klient():
    from kurs.web import app as webapp
    return webapp.app.test_client()


def _oppmote(con, pid=None):
    sql, args = "SELECT paamelding_id, kursdag_id, kilde, ts FROM oppmote", ()
    if pid:
        sql, args = sql + " WHERE paamelding_id=?", (pid,)
    return [tuple(r) for r in con.execute(sql + " ORDER BY paamelding_id, kursdag_id", args)]


def _hendelser(con, handling="oppmote_selv"):
    return con.execute("SELECT aktor, handling, detaljer FROM hendelse WHERE handling=? ORDER BY id", (handling,)).fetchall()


def _tekst(r) -> str:
    return r.get_data(as_text=True)


def _uten_tegn(html: str) -> str:
    return re.sub(r"\s+", " ", html)


@pytest.fixture
def k1(con):
    """Fysisk kurs K1 med kursdag i dag og i morgen, og Kari bekreftet."""
    kid = _kurs(con)
    did, pid = lag_deltaker(con, kid, "kari@example.no", "Kari", "Nordmann")
    return {"kid": kid, "did": did, "pid": pid, "dag": _dag(con, kid), "dag2": _dag(con, kid, 1)}


# ============================ QR: GET skriver aldri, tre tilstander ============================

def test_qr_get_skriver_aldri_verken_uinnlogget_eller_innlogget(con, k1):
    foer = (_oppmote(con), con.execute("SELECT COUNT(*) FROM hendelse").fetchone()[0])
    assert _klient().get(f"/innsjekk/{k1['dag']['innsjekk_token']}").status_code == 200
    assert deltaker_klient(k1["did"]).get(f"/innsjekk/{k1['dag']['innsjekk_token']}").status_code == 200
    assert (_oppmote(con), con.execute("SELECT COUNT(*) FROM hendelse").fetchone()[0]) == foer


def test_qr_uinnlogget_ber_om_e_post_og_har_ingen_husk_meg(con, k1):
    html = _tekst(_klient().get(f"/innsjekk/{k1['dag']['innsjekk_token']}"))
    assert "Kurs K1" in html and "Dag 1 av 2" in html and 'name="epost"' in html and 'type="email"' in html and 'autocomplete="email"' in html
    assert "husk" not in html.lower() and "Du blir ikke logget inn på nettsiden av dette" in html
    assert 'name="csrf_token"' in html and "Registrer oppmøte" in html


def test_qr_innlogget_far_en_knapp_uten_epostfelt_og_maskert_adresse(con, k1):
    html = _tekst(deltaker_klient(k1["did"]).get(f"/innsjekk/{k1['dag']['innsjekk_token']}"))
    assert "Hei, Kari" in html and 'name="epost"' not in html and "Registrer oppmøte" in html
    assert "k***@example.no" in html and "kari@example.no" not in html and "Logg ut" in html


def test_qr_en_annen_dag_gir_bare_beskjeden_uten_skjema(con, k1):
    html = _tekst(_klient().get(f"/innsjekk/{k1['dag2']['innsjekk_token']}"))
    assert "Innsjekk for denne kursdagen er ikke åpen i dag." in html and "<form" not in html.split("<main", 1)[1]
    html = _tekst(deltaker_klient(k1["did"]).get(f"/innsjekk/{k1['dag2']['innsjekk_token']}"))
    assert "Innsjekk for denne kursdagen er ikke åpen i dag." in html and "Registrer oppmøte</button>" not in html


def test_qr_ukjent_token_gir_404(con, k1):
    assert _klient().get("/innsjekk/finnes-ikke").status_code == 404
    assert _klient().post("/innsjekk/finnes-ikke", data={"epost": "kari@example.no"}).status_code == 404


def test_qr_og_kode_står_aldri_i_html_til_deltakeren(con, k1):
    dag = k1["dag"]
    for k in (_klient(), deltaker_klient(k1["did"])):
        for r in (k.get(f"/innsjekk/{dag['innsjekk_token']}"), k.get("/innsjekk"),
                  k.post(f"/innsjekk/{dag['innsjekk_token']}", data={"epost": "kari@example.no"})):
            html = _tekst(r)
            assert dag["innsjekk_token"] not in html and dag["innsjekk_kode"] not in html


# ============================ QR: registrering uten innlogging (e-post) ============================

def test_qr_post_uinnlogget_registrerer_med_kilde_qr_og_gir_ikke_innlogging(con, k1):
    k = _klient()
    r = k.post(f"/innsjekk/{k1['dag']['innsjekk_token']}", data={"epost": " KARI@Example.no "})     # store bokstaver og mellomrom
    html = _tekst(r)
    assert r.status_code == 200 and "Oppmøte er registrert." in html
    assert [(o[0], o[1], o[2]) for o in _oppmote(con)] == [(k1["pid"], k1["dag"]["id"], "qr")]
    with k.session_transaction() as s:
        assert "deltaker_id" not in s
    assert k.get("/min-side").status_code == 302                                    # e-post alene gir aldri innlogging


def test_qr_post_husk_meg_finnes_ikke_lenger_og_gir_aldri_innlogging(con, k1):
    k = _klient()
    k.post(f"/innsjekk/{k1['dag']['innsjekk_token']}", data={"epost": "kari@example.no", "husk": "on"})
    with k.session_transaction() as s:
        assert "deltaker_id" not in s
    assert k.get("/min-side").status_code == 302


def test_qr_svaret_til_uinnlogget_har_ikke_fornavn_bare_maskert_adresse(con, k1):
    html = _tekst(_klient().post(f"/innsjekk/{k1['dag']['innsjekk_token']}", data={"epost": "kari@example.no"}))
    assert "Kari" not in html.replace("Kurs K1", "") and "Nordmann" not in html
    assert "k***@example.no" in html and "kari@example.no" not in html.replace('name="epost" value="kari@example.no"', "")


def test_qr_ikke_paameldt_gir_samme_tekst_uansett_arsak(con, k1):
    lag_deltaker(con, k1["kid"], "arne@example.no", "Arne", "Avmeldt", status="avmeldt")          # først: en avmelding rykker ellers opp ventelisten
    lag_deltaker(con, k1["kid"], "vera@example.no", "Vera", "Venteliste", status="venteliste")
    annet = _kurs(con, "K2")
    lag_deltaker(con, annet, "annen@example.no", "Anna", "Annerledes")
    tekster = set()
    for epost_ in ("finnes-ikke@example.no", "vera@example.no", "arne@example.no", "annen@example.no", ""):
        r = _klient().post(f"/innsjekk/{k1['dag']['innsjekk_token']}", data={"epost": epost_})
        html = _tekst(r)
        assert r.status_code == 200 and NEUTRAL in html
        tekster.add(re.search(r'<p class="inn-tekst"[^>]*>(.*?)</p>', html, re.S).group(1))
    assert len(tekster) == 1
    assert _oppmote(con) == []


def test_qr_en_gang_per_dag_andre_skann_gir_allerede_og_endrer_ingenting(con, k1):
    k = _klient()
    url = f"/innsjekk/{k1['dag']['innsjekk_token']}"
    k.post(url, data={"epost": "kari@example.no"})
    foer = _oppmote(con)
    con.execute("UPDATE oppmote SET ts='2027-03-10 07:47:00'")                       # 08:47 norsk tid
    con.commit()
    html = _tekst(k.post(url, data={"epost": "kari@example.no"}))
    # Uinnloggede får IKKE klokkeslettet til det første oppmøtet (det ville røpet overfor en fremmed med QR-koden når en annen kom)
    assert "Du er allerede registrert i dag." in html and "08:47" not in html and "Du kan bare registreres én gang per kursdag" in html
    assert len(_oppmote(con)) == 1 and _oppmote(con)[0][3] == "2027-03-10 07:47:00" and _oppmote(con)[0][:3] == foer[0][:3]


def test_qr_post_pa_en_annen_dag_registrerer_ingenting(con, k1):
    html = _tekst(_klient().post(f"/innsjekk/{k1['dag2']['innsjekk_token']}", data={"epost": "kari@example.no"}))
    assert "Innsjekk for denne kursdagen er ikke åpen i dag." in html and _oppmote(con) == []


@pytest.mark.parametrize("status", ["utkast", "avlyst", "avsluttet"])
def test_qr_er_ikke_apen_for_kurs_som_ikke_er_apent_fullt_eller_pagar(con, status):
    kid = _kurs(con, "S1")
    lag_deltaker(con, kid, "kari@example.no")
    _sett_kursstatus(con, kid, status)
    html = _tekst(_klient().post(f"/innsjekk/{_dag(con, kid)['innsjekk_token']}", data={"epost": "kari@example.no"}))
    assert "Innsjekk for denne kursdagen er ikke åpen i dag." in html and _oppmote(con) == []


@pytest.mark.parametrize("status", ["aapen", "full", "aktiv"])
def test_qr_virker_for_apne_fulle_og_pagaende_kurs(con, status):
    kid = _kurs(con, "S1")
    _, pid = lag_deltaker(con, kid, "kari@example.no")
    _sett_kursstatus(con, kid, status)
    assert "Oppmøte er registrert." in _tekst(_klient().post(f"/innsjekk/{_dag(con, kid)['innsjekk_token']}", data={"epost": "kari@example.no"}))
    assert len(_oppmote(con, pid)) == 1


def test_qr_post_uten_csrf_avvises(con, k1):
    k = _klient()
    k.injiser_csrf = False
    assert k.post(f"/innsjekk/{k1['dag']['innsjekk_token']}", data={"epost": "kari@example.no"}).status_code == 400
    assert _oppmote(con) == []


# ============================ QR: innlogget ============================

def test_qr_post_innlogget_registrerer_uten_e_post_og_bruker_okten_ikke_skjemaet(con, k1):
    """Den innloggede identifiserer seg med økten: en e-post i skjemaet (en annens) ignoreres helt."""
    annen, annen_pid = lag_deltaker(con, k1["kid"], "ola@example.no", "Ola", "Hansen")
    k = deltaker_klient(k1["did"])
    html = _tekst(k.post(f"/innsjekk/{k1['dag']['innsjekk_token']}", data={"epost": "ola@example.no"}))
    assert "Velkommen, Kari! Oppmøte er registrert." in html
    assert [(o[0], o[2]) for o in _oppmote(con)] == [(k1["pid"], "qr")] and _oppmote(con, annen_pid) == []


def test_qr_innlogget_allerede_registrert_vises_pa_forhand_uten_ny_rad(con, k1):
    k = deltaker_klient(k1["did"])
    k.post(f"/innsjekk/{k1['dag']['innsjekk_token']}")
    html = _tekst(k.get(f"/innsjekk/{k1['dag']['innsjekk_token']}"))
    assert "Du er allerede registrert i dag" in html and "Registrer oppmøte</button>" not in html
    assert len(_oppmote(con)) == 1
    html = _tekst(k.post(f"/innsjekk/{k1['dag']['innsjekk_token']}"))
    assert "Hei Kari – du var allerede sjekket inn i dag" in html and len(_oppmote(con)) == 1


def test_qr_innlogget_uten_paamelding_pa_kurset_far_beskjed_og_ingen_rad(con, k1):
    annet = _kurs(con, "K2")
    fremmed, _ = lag_deltaker(con, annet, "fremmed@example.no", "Frida", "Fremmed")
    html = _tekst(deltaker_klient(fremmed).post(f"/innsjekk/{k1['dag']['innsjekk_token']}"))
    assert "Vi finner ikke en bekreftet påmelding for deg på dette kurset. Kontakt kursansvarlig." in html and _oppmote(con) == []


def test_qr_stale_okt_for_slettet_person_behandles_som_uinnlogget(con, k1):
    html = _tekst(deltaker_klient(99999).get(f"/innsjekk/{k1['dag']['innsjekk_token']}"))
    assert 'name="epost"' in html


# ============================ QR: resultatsiden og «Send meg innloggingslenke» ============================

def test_qr_resultat_uinnlogget_tilbyr_innloggingslenke_bare_nar_kurset_har_en_apen_kursside(con, k1):
    url = f"/innsjekk/{k1['dag']['innsjekk_token']}"
    html = _tekst(_klient().post(url, data={"epost": "kari@example.no"}))
    assert "Send meg innloggingslenke" not in html                                   # ingen publisert side ennå
    skriv_side(con, k1["kid"], dokument(tekstblokk()))
    con.execute("DELETE FROM oppmote")
    con.commit()
    html = _tekst(_klient().post(url, data={"epost": "kari@example.no"}))
    assert "Vil du se program og presentasjoner?" in html and "Send meg innloggingslenke" in html
    assert 'action="/logg-inn"' in html and 'name="neste" value="/kurs/K1/deltakerside"' in html
    assert 'name="epost" value="kari@example.no"' in html and 'name="csrf_token"' in html
    assert "en engangslenke til k***@example.no" in _uten_tegn(html)
    assert "Send meg innloggingslenke" not in _tekst(_klient().post(url, data={"epost": "ingen@example.no"}))     # ikke ved feil


def test_qr_innloggingslenken_fra_resultatsiden_lander_pa_kurssiden(con, k1):
    skriv_side(con, k1["kid"], dokument(tekstblokk("Velkommen", "<p>Hei fra kurssiden</p>")))
    k = _klient()
    k.post(f"/innsjekk/{k1['dag']['innsjekk_token']}", data={"epost": "kari@example.no"})
    r = k.post("/logg-inn", data={"epost": "kari@example.no", "neste": "/kurs/K1/deltakerside"})
    lenke = re.search(r'href="([^"]*/logg-inn/[^"]+)"', _tekst(r)).group(1).replace("&amp;", "&")
    r = k.get(lenke.replace(config.BASE_URL, ""))
    assert r.status_code == 302 and r.headers["Location"].endswith("/kurs/K1/deltakerside")


def test_qr_resultat_innlogget_har_knapper_til_min_side_og_mine_kurs(con, k1):
    skriv_side(con, k1["kid"], dokument(tekstblokk()))
    html = _tekst(deltaker_klient(k1["did"]).post(f"/innsjekk/{k1['dag']['innsjekk_token']}"))
    assert 'href="/kurs/K1/deltakerside"' in html and "Gå til Min side" in html and 'href="/min-side"' in html and "Mine kurs" in html
    assert "Send meg innloggingslenke" not in html


def test_qr_resultat_viser_fremdrift_dag_n_av_m_og_klokkeslett(con, k1):
    html = _tekst(_klient().post(f"/innsjekk/{k1['dag']['innsjekk_token']}", data={"epost": "kari@example.no"}))
    assert "Dag 1 av 2" in html and html.count('class="inn-seg') == 2 and "inn-seg-na" in html and re.search(r"kl\. \d\d:\d\d", html)


# ============================ QR: takbegrensning ============================

def test_qr_takbegrenses_per_ip_og_token(con, k1):
    url = f"/innsjekk/{k1['dag']['innsjekk_token']}"
    k = _klient()
    for n in range(sikkerhet.GRENSER["innsjekk_qr"][0]):
        assert k.post(url, data={"epost": f"a{n}@example.no"}).status_code == 200
    assert k.post(url, data={"epost": "kari@example.no"}).status_code == 429
    assert _oppmote(con) == []
    assert _klient().get(url).status_code == 200                                     # GET telles ikke


def test_qr_takbegrenses_per_epost_og_ip(con, k1):
    url = f"/innsjekk/{k1['dag']['innsjekk_token']}"
    for _ in range(sikkerhet.GRENSER["innsjekk_qr_epost"][0]):
        assert _klient().post(url, data={"epost": "gjetter@example.no"}).status_code == 200
    assert _klient().post(url, data={"epost": "gjetter@example.no"}).status_code == 429
    assert _klient().post(url, data={"epost": "kari@example.no"}).status_code == 200      # en annen e-post har egen teller


# ============================ hendelse og Logger ============================

def test_innsjekk_logges_som_oppmote_selv_uten_persondata_og_vises_i_logger(con, k1):
    _klient().post(f"/innsjekk/{k1['dag']['innsjekk_token']}", data={"epost": "kari@example.no"})
    (aktor, handling, detaljer), = _hendelser(con)
    d = json.loads(detaljer)
    assert aktor == f"deltaker:{k1['did']}" and d == {"paamelding_id": k1["pid"], "kursdag_id": k1["dag"]["id"], "registrert": True, "kilde": "qr"}
    assert "kari" not in detaljer.lower() and "example" not in detaljer
    p = con.execute("SELECT * FROM paamelding WHERE id=?", (k1["pid"],)).fetchone()
    rad, = [r for r in hendelseslogg.for_paamelding(con, p, systemadmin=False).rader if r.hva.startswith("Oppmøte registrert")]
    assert rad.hva == "Oppmøte registrert av deltakeren selv for kursdagen 10.03.2027" and rad.hvem == "Deltakeren selv"
    assert rad.detaljer == ["Registrert med QR-koden i kurslokalet"]


def test_andre_skann_logger_ikke_en_gang_til(con, k1):
    url = f"/innsjekk/{k1['dag']['innsjekk_token']}"
    _klient().post(url, data={"epost": "kari@example.no"})
    _klient().post(url, data={"epost": "kari@example.no"})
    assert len(_hendelser(con)) == 1


# ============================ kodesiden /innsjekk er fjernet ============================

def test_kodesiden_innsjekk_er_fjernet_qr_og_min_side_er_veiene_inn(con, k1):
    """Før kunne man gå til /innsjekk, skrive dagens kode og e-post uten å logge inn. Den siden finnes ikke lenger (ingen GET, ingen POST, ingen
    lenke fra innloggingssiden): QR-koden (/innsjekk/<token>) og «Registrer oppmøte i dag» på Min side, med koden, er det som er igjen."""
    for klient in (_klient(), deltaker_klient(k1["did"])):
        assert klient.get("/innsjekk").status_code == 404
        assert klient.post("/innsjekk", data={"kode": k1["dag"]["innsjekk_kode"], "epost": "kari@example.no"}).status_code == 404
    assert _oppmote(con) == []
    assert 'href="/innsjekk"' not in _tekst(_klient().get("/logg-inn"))
    assert _klient().get(f"/innsjekk/{k1['dag']['innsjekk_token']}").status_code == 200                       # QR-ruten består
    assert "innsjekk" not in sikkerhet.GRENSER                                                                  # takgrensen for kodesiden er borte


def test_sjekk_inn_er_fortsatt_en_tynn_innpakning_som_gir_status_og_tekst(con, k1, monkeypatch):
    from kurs.web import app as webapp
    with webapp.app.test_request_context("/"):
        assert webapp._sjekk_inn(k1["dag"], epost_="kari@example.no") == ("ok", "Oppmøte er registrert.")
        assert webapp._sjekk_inn(k1["dag"], epost_="kari@example.no")[0] == "ok"
        assert webapp._sjekk_inn(k1["dag2"], epost_="kari@example.no")[0] == "feil"
        assert webapp._sjekk_inn(k1["dag"], deltaker_id=k1["did"], kilde="kode")[1].startswith("Hei Kari")


# ============================ enhetstester: maskert e-post og tekster ============================

@pytest.mark.parametrize("adresse,fasit", [("eva@example.no", "e***@example.no"), ("EVA@Example.NO", "e***@example.no"), ("x@a.no", "x***@a.no"),
                                            (" kari.nordmann@example.no ", "k***@example.no"), ("ingen-alfa", "***"), ("", "***")])
def test_maskert_epost(adresse, fasit):
    assert deltakerside.maskert_epost(adresse) == fasit


def test_tekster_folger_tabellen_i_spesifikasjonen():
    O = deltakerside.Oppmote
    assert deltakerside.tekst_for(O("ny", "Kari"), innlogget=True) == "Velkommen, Kari! Oppmøte er registrert."
    assert deltakerside.tekst_for(O("ny", "Kari"), innlogget=False) == "Oppmøte er registrert."
    assert deltakerside.tekst_for(O("ny", "Kari"), innlogget=True, nett=True) == "Oppmøte registrert. Velkommen, Kari!"
    assert deltakerside.tekst_for(O("allerede", "Kari", "08:47"), innlogget=True) == "Hei Kari – du var allerede sjekket inn i dag (kl. 08:47)."
    assert deltakerside.tekst_for(O("allerede", None, "08:47"), innlogget=False) == "Du er allerede registrert i dag (kl. 08:47)."
    assert deltakerside.tekst_for(O("allerede", "Kari", "08:47"), innlogget=True, nett=True) == "Du var allerede registrert i dag (kl. 08:47)."
    assert deltakerside.tekst_for(O("ikke_i_dag"), innlogget=False) == "Innsjekk for denne kursdagen er ikke åpen i dag."
    assert deltakerside.tekst_for(O("kode_feil"), innlogget=True).startswith("Koden stemmer ikke. Den står på skjermen i kurslokalet.")
    assert deltakerside.tekst_for(O("ingen_kursdag"), innlogget=True) == "Det er ingen kursdag i dag på dette kurset."
    assert deltakerside.tekst_for(O("ikke_tilbudt"), innlogget=True) == "Oppmøte på dette kurset registreres automatisk."
    assert deltakerside.tekst_for(O("ikke_paameldt"), innlogget=True).startswith("Vi finner ikke en bekreftet påmelding for deg")
    assert deltakerside.tekst_for(O("ikke_paameldt"), innlogget=False).startswith(NEUTRAL)
    for status in ("ny", "allerede", "ikke_i_dag", "ikke_paameldt", "ingen_kursdag", "kode_feil", "ikke_tilbudt"):
        assert "None" not in deltakerside.tekst_for(O(status), innlogget=False)


# ============================ K10: «Registrer oppmøte i dag» på nett ============================

def _selv(k, kode_url="K1", kode="", **felt):
    return k.post(f"/kurs/{kode_url}/deltakerside/oppmote", data={"kode": kode, **felt})


def test_selvregistrering_med_riktig_kode_gir_rad_kilde_kode_og_hendelse(con, k1):
    r = _selv(deltaker_klient(k1["did"]), kode=k1["dag"]["innsjekk_kode"])
    assert r.status_code == 302 and r.headers["Location"].endswith("/kurs/K1/deltakerside#i-dag")
    assert [(o[0], o[1], o[2]) for o in _oppmote(con)] == [(k1["pid"], k1["dag"]["id"], "kode")]
    (aktor, _, detaljer), = _hendelser(con)
    assert aktor == f"deltaker:{k1['did']}" and json.loads(detaljer) == {"paamelding_id": k1["pid"], "kursdag_id": k1["dag"]["id"],
                                                                        "registrert": True, "kilde": "kode"}
    assert "example" not in detaljer and "kari" not in detaljer.lower()


def test_selvregistrering_feil_kode_skriver_ingenting(con, k1):
    r = _selv(deltaker_klient(k1["did"]), kode="XXXXXX")
    assert r.status_code == 302 and _oppmote(con) == [] and _hendelser(con) == []


def test_selvregistrering_kode_med_smaa_bokstaver_mellomrom_og_bindestrek(con, k1):
    kode = k1["dag"]["innsjekk_kode"].lower()
    r = _selv(deltaker_klient(k1["did"]), kode=f" {kode[:3]}-{kode[3:]} ")
    assert len(_oppmote(con)) == 1 and r.status_code == 302


def test_selvregistrering_en_gang_per_dag_andre_forsok_endrer_ingenting(con, k1):
    k = deltaker_klient(k1["did"])
    _selv(k, kode=k1["dag"]["innsjekk_kode"])
    con.execute("UPDATE oppmote SET ts='2027-03-10 07:47:00'")
    con.commit()
    r = _selv(k, kode=k1["dag"]["innsjekk_kode"], retur="min_side")
    assert len(_oppmote(con)) == 1 and _oppmote(con)[0][3] == "2027-03-10 07:47:00" and len(_hendelser(con)) == 1
    assert "Du var allerede registrert i dag (kl. 08:47)." in _tekst(k.get(r.headers["Location"]))


def test_selvregistrering_etter_qr_samme_dag_er_allerede_registrert(con, k1):
    _klient().post(f"/innsjekk/{k1['dag']['innsjekk_token']}", data={"epost": "kari@example.no"})
    _selv(deltaker_klient(k1["did"]), kode=k1["dag"]["innsjekk_kode"])
    assert [o[2] for o in _oppmote(con)] == ["qr"]                                   # første registrering vinner


def test_selvregistrering_bare_pa_selve_kursdagen(con, k1, monkeypatch):
    fast_dato(monkeypatch, IDAG - timedelta(days=1))
    _selv(deltaker_klient(k1["did"]), kode=k1["dag"]["innsjekk_kode"])
    fast_dato(monkeypatch, IDAG + timedelta(days=5))
    _selv(deltaker_klient(k1["did"]), kode=k1["dag"]["innsjekk_kode"])
    assert _oppmote(con) == []
    fast_dato(monkeypatch, IDAG + timedelta(days=1))                                 # dag 2: dagens kode er en annen
    _selv(deltaker_klient(k1["did"]), kode=k1["dag"]["innsjekk_kode"])
    assert _oppmote(con) == []
    _selv(deltaker_klient(k1["did"]), kode=k1["dag2"]["innsjekk_kode"])
    assert [o[1] for o in _oppmote(con)] == [k1["dag2"]["id"]]


@pytest.mark.parametrize("status", ["venteliste", "avmeldt"])
def test_selvregistrering_bare_for_bekreftet_paamelding(con, k1, status):
    ny, _ = lag_deltaker(con, k1["kid"], "andre@example.no", "Andre", "Person", status=status)
    r = _selv(deltaker_klient(ny), kode=k1["dag"]["innsjekk_kode"])
    assert r.status_code == 404 and _oppmote(con) == []


def test_selvregistrering_for_et_kurs_man_ikke_er_pa_gir_404(con, k1):
    annet = _kurs(con, "K2")
    fremmed, _ = lag_deltaker(con, annet, "fremmed@example.no")
    r = _selv(deltaker_klient(fremmed), kode=k1["dag"]["innsjekk_kode"])
    assert r.status_code == 404 and _oppmote(con) == []
    assert _selv(deltaker_klient(k1["did"]), kode_url="FINNES-IKKE", kode="ABC123").status_code == 404


def test_selvregistrering_uinnlogget_sendes_til_innlogging_med_kursiden_som_mal(con, k1):
    r = _selv(_klient(), kode=k1["dag"]["innsjekk_kode"])
    lenke = urlsplit(r.headers["Location"])
    assert r.status_code == 302 and lenke.path == "/logg-inn" and parse_qs(lenke.query) == {"neste": ["/kurs/K1/deltakerside"]}
    assert _oppmote(con) == []


def test_selvregistrering_id_fra_skjemaet_ignoreres_ingen_idor(con, k1):
    """Påmeldingen slås opp fra økten og kurskoden i adressen. En paamelding_id, kursdag_id eller deltaker_id i skjemaet gjør ingenting."""
    annen, annen_pid = lag_deltaker(con, k1["kid"], "ola@example.no", "Ola", "Hansen")
    _selv(deltaker_klient(k1["did"]), kode=k1["dag"]["innsjekk_kode"], paamelding_id=str(annen_pid), deltaker_id=str(annen),
          kursdag_id=str(k1["dag2"]["id"]), kilde="manuell")
    assert [(o[0], o[1], o[2]) for o in _oppmote(con)] == [(k1["pid"], k1["dag"]["id"], "kode")]


def test_selvregistrering_kvoten_teller_foer_kodesjekken(con, k1):
    k = deltaker_klient(k1["did"])
    for _ in range(sikkerhet.GRENSER["oppmote_selv"][0]):
        assert _selv(k, kode="XXXXXX").status_code == 302
    assert _selv(k, kode=k1["dag"]["innsjekk_kode"]).status_code == 429               # riktig kode hjelper ikke etter taket
    assert _oppmote(con) == []
    andre, _ = lag_deltaker(con, k1["kid"], "ola@example.no", "Ola", "Hansen")
    assert _selv(deltaker_klient(andre), kode=k1["dag"]["innsjekk_kode"]).status_code == 302     # egen teller per deltaker
    assert len(_oppmote(con)) == 1


def test_selvregistrering_uten_csrf_avvises(con, k1):
    k = deltaker_klient(k1["did"])
    k.injiser_csrf = False
    assert _selv(k, kode=k1["dag"]["innsjekk_kode"]).status_code == 400 and _oppmote(con) == []


def test_selvregistrering_kan_slas_av_kodekravet_per_kurs(con, k1):
    skriv_side(con, k1["kid"], dokument(tekstblokk()))
    con.execute("UPDATE kursside SET innsjekk_krever_kode=0 WHERE kurs_id=?", (k1["kid"],))
    con.commit()
    k = deltaker_klient(k1["did"])
    html = _tekst(k.get("/kurs/K1/deltakerside"))
    assert 'name="kode"' not in html and "Registrer oppmøte" in html
    _selv(k)                                                                         # ingen kode
    assert [o[2] for o in _oppmote(con)] == ["kode"]


def test_selvregistrering_virker_uten_publisert_kursside(con, k1):
    assert con.execute("SELECT COUNT(*) FROM kursside").fetchone()[0] == 0
    _selv(deltaker_klient(k1["did"]), kode=k1["dag"]["innsjekk_kode"])
    assert len(_oppmote(con)) == 1


def test_selvregistrering_fysisk_uten_kursside_krever_kode(con, k1):
    _selv(deltaker_klient(k1["did"]), kode="")
    assert _oppmote(con) == []


def test_selvregistrering_hybrid_krever_kode_som_fysisk(con):
    kid = _kurs(con, "H1", type_="hybrid", zoom_id="123")
    did, pid = lag_deltaker(con, kid, "kari@example.no")
    dag = _dag(con, kid)
    _selv(deltaker_klient(did), kode_url="H1", kode="XXXXXX")
    assert _oppmote(con) == []
    _selv(deltaker_klient(did), kode_url="H1", kode=dag["innsjekk_kode"])
    assert len(_oppmote(con, pid)) == 1


def test_selvregistrering_digitalt_kurs_med_zoom_import_tilbys_ikke(con):
    kid = _kurs(con, "D1", type_="digital", zoom_id="123456")
    did, pid = lag_deltaker(con, kid, "kari@example.no")
    k = deltaker_klient(did)
    html = _tekst(k.get("/min-side"))
    assert "Oppmøte på digitale kurs registreres automatisk fra Zoom dagen etter." in html and "Registrer oppmøte</button>" not in html
    r = _selv(k, kode_url="D1", kode=_dag(con, kid)["innsjekk_kode"])
    assert _oppmote(con) == [] and r.status_code == 302
    assert "Oppmøte på dette kurset registreres automatisk." in _tekst(k.get("/min-side"))


def test_selvregistrering_digitalt_kurs_uten_zoom_import_er_en_knapp_uten_kode(con):
    kid = _kurs(con, "D2", type_="digital", zoom_id=None)
    did, pid = lag_deltaker(con, kid, "kari@example.no")
    k = deltaker_klient(did)
    html = _tekst(k.get("/min-side"))
    assert "Registrer oppmøte</button>" in html and 'name="kode"' not in html
    _selv(k, kode_url="D2")
    assert [o[2] for o in _oppmote(con, pid)] == ["kode"]


@pytest.mark.parametrize("status", ["utkast", "avlyst", "avsluttet"])
def test_selvregistrering_ikke_for_kurs_som_ikke_pagar(con, status):
    kid = _kurs(con, "S1")
    did, _ = lag_deltaker(con, kid, "kari@example.no")
    _sett_kursstatus(con, kid, status)
    _selv(deltaker_klient(did), kode_url="S1", kode=_dag(con, kid)["innsjekk_kode"])
    assert _oppmote(con) == []


def test_selvregistrering_retur_er_bare_kursside_eller_min_side_aldri_en_fri_adresse(con, k1):
    skriv_side(con, k1["kid"], dokument(tekstblokk()))
    k = deltaker_klient(k1["did"])
    assert _selv(k, kode="X", retur="min_side").headers["Location"].endswith("/min-side#mine-kurs")
    for retur in ("kursside", "", "https://ond.example", "//ond.example", "/admin"):
        assert _selv(k, kode="X", retur=retur).headers["Location"].endswith("/kurs/K1/deltakerside#i-dag")


def test_selvregistrering_teller_i_kursbevis_og_timer_i_spesialistlop(con):
    kid = _kurs(con, "E1", dager=1, spesialistlop="EFT", timer_pr_dag=7)
    did, pid = lag_deltaker(con, kid, "kari@example.no")
    assert timer_i_lop(con, did, "EFT") == 0
    _selv(deltaker_klient(did), kode_url="E1", kode=_dag(con, kid)["innsjekk_kode"])
    assert timer_i_lop(con, did, "EFT") == 7


def test_selvregistrert_oppmote_vises_som_K_i_matrisen_og_admin_kan_fjerne_det(con, k1):
    _selv(deltaker_klient(k1["did"]), kode=k1["dag"]["innsjekk_kode"])
    admin = admin_klient(con)
    html = _tekst(admin.get(f"/admin/kurs/{k1['kid']}/deltakere"))
    assert 'title="kode"' in html and ">K</button>" in html
    admin.post(f"/admin/kurs/{k1['kid']}/oppmote", data={"paamelding_id": str(k1["pid"]), "kursdag_id": str(k1["dag"]["id"])})
    assert _oppmote(con) == []                                                       # admin sin veksling sletter raden


def test_selvregistrering_vises_i_logger_som_deltakeren_selv(con, k1):
    _selv(deltaker_klient(k1["did"]), kode=k1["dag"]["innsjekk_kode"])
    p = con.execute("SELECT * FROM paamelding WHERE id=?", (k1["pid"],)).fetchone()
    rad, = [r for r in hendelseslogg.for_paamelding(con, p, systemadmin=False).rader if r.hva.startswith("Oppmøte registrert")]
    assert rad.hvem == "Deltakeren selv" and "10.03.2027" in rad.hva and rad.detaljer == ["Registrert med dagens innsjekk-kode"]


# ============================ meldingen etter registrering (i kortet, ikke øverst) ============================

def test_svaret_vises_i_kortet_pa_kurssiden_og_leses_bare_en_gang(con, k1):
    skriv_side(con, k1["kid"], dokument(tekstblokk()))
    k = deltaker_klient(k1["did"])
    r = _selv(k, kode="XXXXXX")
    html = _tekst(k.get(r.headers["Location"]))
    assert 'class="dp-svar dp-svar-feil"' in html and "Koden stemmer ikke." in html and 'role="alert"' in html
    assert 'class="flash' not in html                                                # ikke som melding øverst
    assert "dp-svar" not in _tekst(k.get("/kurs/K1/deltakerside"))                    # bare én gang
    r = _selv(k, kode=k1["dag"]["innsjekk_kode"])
    html = _tekst(k.get(r.headers["Location"]))
    assert 'dp-svar-ok' in html and "Oppmøte registrert. Velkommen, Kari!" in html and "Oppmøte registrert kl." in html


def test_svaret_vises_i_kurskortet_pa_min_side(con, k1):
    k = deltaker_klient(k1["did"])
    r = _selv(k, kode="XXXXXX", retur="min_side")
    html = _tekst(k.get(r.headers["Location"]))
    assert 'class="ms-svar ms-svar-feil"' in html and "Koden stemmer ikke." in html
    r = _selv(k, kode=k1["dag"]["innsjekk_kode"], retur="min_side")
    html = _tekst(k.get(r.headers["Location"]))
    assert "Oppmøte registrert. Velkommen, Kari!" in html and "Du er registrert for i dag · kl." in html
    assert "ms-svar" not in _tekst(k.get("/min-side"))


def test_meldingen_for_et_kurs_vises_ikke_pa_et_annet_kurs(con, k1):
    annet = _kurs(con, "K2", start=IDAG + timedelta(days=30))
    lag_deltaker(con, annet, "kari@example.no", "Kari", "Nordmann")
    k = deltaker_klient(k1["did"])
    _selv(k, kode="XXXXXX")
    assert "dp-svar" not in _tekst(k.get("/kurs/K2/deltakerside"))


# ============================ plakaten (admin_qr) ============================

def test_plakaten_har_qr_kursnavn_dato_dag_n_av_m_kode_og_instruks(con, k1):
    html = _tekst(admin_klient(con).get(f"/admin/kurs/{k1['kid']}/qr/{k1['dag']['id']}"))
    assert "<svg" in html and "Kurs K1" in html and "Dag 1 av 2".lower() in html.lower() and "onsdag 10. mars 2027" in html
    assert k1["dag"]["innsjekk_kode"] in html and "/innsjekk" in html
    assert "Åpne kameraet på telefonen" in html and "Trykk på lenken" in html and "Skriv e-postadressen du meldte deg på med" in html
    assert "Du registrerer deg én gang hver kursdag." in html and "✓ 0 sjekket inn" in html
    assert "Denne QR-koden gjelder ikke i dag" not in html
    assert "Skriv ut plakat" in html and "data-skriv-ut" in html and "@media print" in html and "@page" in html


def test_plakaten_for_en_annen_dag_advarer_pa_skjermen(con, k1):
    html = _tekst(admin_klient(con).get(f"/admin/kurs/{k1['kid']}/qr/{k1['dag2']['id']}"))
    assert "Denne QR-koden gjelder ikke i dag." in html and "torsdag 11. mars 2027" in html and "onsdag 10. mars 2027" in html


def test_plakaten_teller_registrerte_og_har_bare_skript_med_nonce_eller_fra_static(con, k1):
    _klient().post(f"/innsjekk/{k1['dag']['innsjekk_token']}", data={"epost": "kari@example.no"})
    r = admin_klient(con).get(f"/admin/kurs/{k1['kid']}/qr/{k1['dag']['id']}")
    html = _tekst(r)
    assert "✓ 1 sjekket inn" in html
    for tag in re.findall(r"<script\b[^>]*>", html):
        assert "nonce=" in tag and 'src="/static/' in tag
    assert not re.search(r"\son[a-z]+\s*=", html)
    assert "innsjekk-plakat.js" in html and '<noscript><meta http-equiv="refresh" content="20"></noscript>' in html


def test_plakaten_krever_admin_og_riktig_kurs(con, k1):
    assert _klient().get(f"/admin/kurs/{k1['kid']}/qr/{k1['dag']['id']}").status_code == 302
    annet = _kurs(con, "K2")
    assert admin_klient(con).get(f"/admin/kurs/{annet}/qr/{k1['dag']['id']}").status_code == 404


# ============================ statiske filer ============================

@pytest.mark.parametrize("fil,prefiks", [("innsjekk.css", "inn-"), ("min-side.css", "ms-")])
def test_css_har_prefiks_ingen_eksterne_ressurser_og_ingen_root(fil, prefiks):
    css = re.sub(r"/\*.*?\*/", "", (STATIC / fil).read_text(encoding="utf-8"), flags=re.S)         # uten kommentarer
    assert ":root" not in css and "url(" not in css and "@import" not in css and "http" not in css
    velgere = re.findall(r"(?m)^\s*([^@{}/][^{}]*)\{", css)
    for v in velgere:
        for del_ in v.split(","):
            klasser = re.findall(r"\.([a-z][\w-]*)", del_)
            assert not klasser or any(k.startswith(prefiks) or k in ("ik", "ik-sprite", "merke", "ok", "gul", "feil", "gra", "knapp", "sekundar") for k in klasser), del_


def test_plakat_js_er_liten_og_uten_farlige_kall():
    js = (STATIC / "innsjekk-plakat.js").read_text(encoding="utf-8")
    assert len(js.splitlines()) < 30
    for forbudt in (".innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "localStorage"):
        assert forbudt not in js
