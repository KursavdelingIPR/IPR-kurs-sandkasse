"""E-posthistorikk (fanen E-poster i deltakervinduet): en uforanderlig kopi av hver e-post, lagret FØR sendingen.

Kravene (planen fra 26.09.2026 og brukerens presiseringer 27.09.2026):
  * hver e-post får en kopi med nøyaktig emne, innhold, bilder og vedlegg - lagret før sendingen, i samme transaksjon som
    reservasjonen. Kan kopien ikke lagres, sendes e-posten ikke
  * kopien endres aldri: en mal eller logo som endres senere, endrer ikke e-poster som alt er sendt
  * e-postene finnes via påmeldingen (ikke adressen), med emne, dato, klokkeslett i norsk tid, mottaker, avsender, hvem som
    sendte og status (sendt/feilet/uavklart), nyeste først - også mislykkede sendinger
  * eldre e-poster uten kopi vises med «Innholdet ble ikke lagret for denne eldre e-posten» og rekonstrueres aldri
  * innloggingslenker og andre personlige tilgangslenker (token) lagres aldri
  * ingen kan se en annen påmeldings e-poster; skript i innholdet kjører ikke; «retten til sletting» sletter kopiene
"""
import html as html_lib
import re
import sqlite3
from datetime import date, timedelta

import pytest

from kurs import config, daglig, db, eposthistorikk, kalender, kursbevis, maltekster, migreringer, sveiper
from kurs.integrasjoner import epost
from kurs.kjoring import EpostkopiFeil, Kjoring, SendingStanset

IDAG = date(2027, 3, 1)
LOGO = b"\x89PNG\r\n\x1a\n" + b"logo-1" * 20
LOGO_NY = b"\x89PNG\r\n\x1a\n" + b"logo-2" * 20


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    return c


def _fersk(con):
    return db.koble(config.DB_STI)


def _klient(logg_inn=True):
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    if logg_inn:
        k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def _kurs(con, kode="E1", start=IDAG + timedelta(days=20), dager=2, **kw):
    datoer = [(start + timedelta(days=n)).isoformat() for n in range(dager)]
    kid = db.opprett_kurs(con, kode=kode, navn="Veiledning i praksis", datoer=datoer, sharepoint_mappe=f"Kurs/{kode}",
                          **{"pris_nok": 0, "type": "fysisk", "sted": "Oslo", **kw})
    con.commit()
    return kid


def _meld_paa(con, kid, epost_="kari@example.no", fornavn="Kari", **kw):
    pid, _ = db.meld_paa(con, kid, epost=epost_, fornavn=fornavn, etternavn="Eksempel", **kw)
    con.commit()
    return pid


def _kopier(con, **hvor):
    sql = "SELECT * FROM sendt_epost" + (" WHERE " + " AND ".join(f"{k}=?" for k in hvor) if hvor else "") + " ORDER BY id"
    return _fersk(con).execute(sql, tuple(hvor.values())).fetchall()


def _utboks():
    return sorted(config.UTBOKS.glob("*.html")) if config.UTBOKS.exists() else []


# ============================ kopien lagres for hver e-posttype ============================

def test_bekreftelse_faar_kopi_med_noyaktig_det_som_ble_sendt(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)
    con.commit()
    (k,) = _kopier(con, type="bekreftelse")
    assert (k["paamelding_id"], k["kurs_id"], k["til"], k["fra"], k["sendt_av"], k["status"], k["mal"]) == (
        pid, kid, "kari@example.no", config.AVSENDER_EPOST, "system", "sendt", "bekreftelse")
    sendt = _utboks()[0].read_text(encoding="utf-8").split("\n", 1)[1]
    assert k["html"] == sendt and k["emne"] == "Bekreftelse: Veiledning i praksis"      # nøyaktig det som gikk ut
    assert k["sendt_ts"] and k["opprettet"] <= k["sendt_ts"]


def test_alle_deltakereposter_faar_kopi_knyttet_til_paameldingen(con):
    kid = _kurs(con, kapasitet=1, start=IDAG + timedelta(days=6))
    p1 = _meld_paa(con, kid)
    p2 = _meld_paa(con, kid, epost_="ola@example.no", fornavn="Ola")                  # venteliste
    daglig.kjor(Kjoring(con, idag=IDAG))                                                # bekreftelse, venteliste, ukefor
    daglig.kjor(Kjoring(con, idag=IDAG + timedelta(days=5)))                            # dagfor
    con.commit()
    typer = {(r["paamelding_id"], r["mal"]) for r in _kopier(con)}
    assert {(p1, "bekreftelse"), (p2, "venteliste"), (p1, "ukefor"), (p1, "dagfor")} <= typer
    k = _klient()
    k.post(f"/admin/kurs/{kid}/deltaker/{p2}/avsla", data={"send_epost": "1", "melding": "Fullt"})
    k.post(f"/admin/kurs/{kid}/avlys")
    admin = f"admin:{config.ADMIN_BRUKERNAVN}"
    assert [(r["mal"], r["sendt_av"]) for r in _kopier(con, paamelding_id=p2) if r["mal"] == "avslag"] == [("avslag", admin)]
    assert [r["sendt_av"] for r in _kopier(con, paamelding_id=p1) if r["mal"] == "avlysning"] == [admin]


def test_manuell_epost_faar_kopi_med_flettet_navn_og_hvem_som_sendte(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    k = _klient()
    side = k.post(f"/admin/kurs/{kid}/epost/forhandsvis", data={"emne": "Husk bok", "tekst": "Hei {fornavn}!",
                                                                "paamelding_id": str(pid)}).get_data(as_text=True)
    utsending_id = re.search(r'name="utsending_id" value="(\d+)"', side)[1]
    k.post(f"/admin/kurs/{kid}/epost/send", data={"utsending_id": utsending_id})
    (kopi,) = _kopier(con, paamelding_id=pid, mal="admin_melding")
    assert kopi["emne"] == "Husk bok" and "Hei Kari!" in kopi["html"]
    assert kopi["sendt_av"] == f"admin:{config.ADMIN_BRUKERNAVN}"


def test_manuell_epost_lenker_fortsatt_til_hele_utsendelsen(con):
    """Den gamle fanen lenket en manuell e-post til utsendelsen (alle mottakere). Det beholdes: fra kopien, og fra eldre
    e-poster uten kopi."""
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    k = _klient()
    side = k.post(f"/admin/kurs/{kid}/epost/forhandsvis", data={"emne": "Husk bok", "tekst": "Hei {fornavn}!",
                                                                "paamelding_id": str(pid)}).get_data(as_text=True)
    utsending_id = re.search(r'name="utsending_id" value="(\d+)"', side)[1]
    k.post(f"/admin/kurs/{kid}/epost/send", data={"utsending_id": utsending_id})
    (kopi,) = _kopier(con, paamelding_id=pid, mal="admin_melding")
    utsendelse = f'href="/admin/kurs/{kid}/epost/{utsending_id}"'
    liste = k.get(f"/admin/kurs/{kid}/deltaker/{pid}/kommunikasjon").get_data(as_text=True)
    assert f'href="/admin/kurs/{kid}/deltaker/{pid}/epost/{kopi["id"]}"' in liste and utsendelse not in liste
    assert utsendelse in k.get(f"/admin/kurs/{kid}/deltaker/{pid}/epost/{kopi['id']}").get_data(as_text=True)

    con.execute("DELETE FROM sendt_epost")          # som en e-post sendt før kopiene fantes
    con.commit()
    liste = k.get(f"/admin/kurs/{kid}/deltaker/{pid}/kommunikasjon").get_data(as_text=True)
    assert eposthistorikk.IKKE_LAGRET in liste
    eldre = html_lib.unescape(re.search(r'href="([^"]*/epost-eldre\?[^"]*)"[^>]*>Husk bok</a>', liste)[1])      # klikkbar som alle andre rader
    assert utsendelse in k.get(eldre).get_data(as_text=True)                                                    # og lenker videre til utsendelsen


def test_kursbevis_faar_kopi(con):
    kid = _kurs(con, start=IDAG - timedelta(days=5), dager=1)
    pid = _meld_paa(con, kid)
    db.registrer_oppmote(con, pid, db.kursdager(con, kid)[0]["id"], "qr")
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()
    kursbevis.kjor(Kjoring(con, idag=IDAG))
    con.commit()
    assert [r["mal"] for r in _kopier(con, paamelding_id=pid)] == [None]                # kursbevis: ferdig rendret
    assert _kopier(con, paamelding_id=pid)[0]["type"] == "kursbevis"


def test_personlige_lenker_lagres_aldri(con):
    """Innloggingslenker (Min side) og bedriftens kvitteringslenke er tokens.
    (06.10.2026: kursholder-lenken og SharePoint er tatt bort - kursholderens opplastingslenke og purringen finnes ikke lenger)"""
    kid = _kurs(con)
    _meld_paa(con, kid)
    _klient(logg_inn=False).post("/logg-inn", data={"epost": "kari@example.no"})
    daglig.kjor(Kjoring(con, idag=IDAG))
    con.commit()
    alt = " ".join(r["html"] for r in _kopier(con))
    assert "/logg-inn/" not in alt and "token" not in alt.lower()
    tokens = [r[0] for r in _fersk(con).execute("SELECT token FROM innlogging_token")]
    assert tokens and all(t not in alt for t in tokens)


def test_varsel_til_admin_faar_kopi_uten_paamelding(con):
    # (06.10.2026: kursholder-lenken og SharePoint er tatt bort, og «eskalering» med dem; varselet til administrasjonen her er
    # påminnelsen om sjekklistene, sendt slik morgenjobben sender den)
    k = Kjoring(con, idag=IDAG)
    assert k.send_en_gang(f"sjekkliste:{IDAG.isoformat()}", config.SJEKKLISTE_EPOST, "sjekkliste-paaminnelse",
                          "sjekkliste_paaminnelse", samlinger=[], antall_forfalt=1, antall_snart=0)
    con.commit()
    (kopi,) = _kopier(con, type="sjekkliste-paaminnelse")
    assert (kopi["paamelding_id"], kopi["kurs_id"], kopi["til"]) == (None, None, config.SJEKKLISTE_EPOST)


def test_allergier_og_tilrettelegging_er_aldri_i_en_kopi(con):
    kid = _kurs(con, start=IDAG + timedelta(days=6))
    _meld_paa(con, kid, sensitivt={"allergier": "HEMMELIG-ALLERGI", "tilrettelegging": "HEMMELIG-BEHOV"})
    daglig.kjor(Kjoring(con, idag=IDAG))
    daglig.kjor(Kjoring(con, idag=IDAG + timedelta(days=5)))
    con.commit()
    alt = " ".join(r["html"] + r["emne"] for r in _kopier(con))
    assert _kopier(con) and "HEMMELIG" not in alt


# ============================ kopien lagres FØR sendingen ============================

def test_kopien_er_lagret_og_committet_foer_sendingen(con, monkeypatch):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    sett = []
    ekte = epost.send

    def send(til, emne, html, vedlegg=None, kopi=None):
        sett.append([tuple(r) for r in db.koble(config.DB_STI).execute(
            "SELECT status, emne FROM sendt_epost WHERE paamelding_id=?", (pid,))])     # en annen tilkobling: committet
        ekte(til, emne, html, vedlegg, kopi)
    monkeypatch.setattr(epost, "send", send)
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)
    assert sett == [[("sender", "Bekreftelse: Veiledning i praksis")]]


def test_ingen_sending_uten_kopi(con, monkeypatch):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    sendt = []
    monkeypatch.setattr(epost, "send", lambda *a, **kw: sendt.append(a))
    monkeypatch.setattr(db, "lagre_epostkopi", lambda *a, **kw: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")))
    db.logg(con, "ventende_arbeid", {})                                                # kallerens ventende arbeid
    k = Kjoring(con, idag=IDAG)
    with pytest.raises(EpostkopiFeil) as feil:
        k.send_en_gang(f"kurs:{kid}", "kari@example.no", "bekreftelse", "bekreftelse", paamelding_id=pid,
                       p=con.execute(sveiper.SQL_DELTAKER + " WHERE p.id=?", (pid,)).fetchone(),
                       kurs=con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone(), dager=[],
                       faktura_plan=sveiper.FakturaPlan(sveiper.PLAN_INGEN))
    assert isinstance(feil.value, SendingStanset) and sendt == []
    f = _fersk(con)
    assert f.execute("SELECT COUNT(*) FROM utsending_logg").fetchone()[0] == 0          # ingen reservasjon
    assert f.execute("SELECT COUNT(*) FROM sendt_epost").fetchone()[0] == 0
    assert f.execute("SELECT COUNT(*) FROM hendelse WHERE handling='ventende_arbeid'").fetchone()[0] == 1
    logg = f.execute("SELECT detaljer FROM hendelse WHERE handling='epostkopi_feilet'").fetchone()[0]
    assert "OperationalError" in logg and "kari" not in logg


def test_krasj_under_sending_gir_kopi_med_status_uavklart(con, monkeypatch):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    monkeypatch.setattr(epost, "send", lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("graph nede")))
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)
    (k,) = _kopier(con, paamelding_id=pid)
    assert (k["status"], k["feilmelding"], k["sendt_ts"]) == ("ukjent", "ConnectionError", None)
    rad = eposthistorikk.for_paamelding(_fersk(con), _paamelding(con, kid, pid))[0]
    assert (rad.status, rad.status_tekst) == ("uavklart", "Uavklart")


def test_avklart_uavklart_epost_oppdaterer_kopien_og_nytt_forsok_faar_ny_kopi(con, monkeypatch):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    ekte = epost.send
    monkeypatch.setattr(epost, "send", lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("graph nede")))
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)
    con.commit()
    monkeypatch.setattr(epost, "send", ekte)
    k = _klient()
    k.post("/admin/uavklart/epost", data={"nokkel": f"kurs:{kid}", "mottaker": "kari@example.no", "type": "bekreftelse",
                                          "utfall": "ikke_sendt"})
    assert [r["status"] for r in _kopier(con, paamelding_id=pid)] == ["feilet"]
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)                                           # nytt forsøk
    con.commit()
    assert [r["status"] for r in _kopier(con, paamelding_id=pid)] == ["feilet", "sendt"]
    side = k.get(f"/admin/kurs/{kid}/deltaker/{pid}/kommunikasjon").get_data(as_text=True)
    assert side.index(">Sendt<") < side.index(">Feilet<")                                # nyeste først, feilet vises


def test_torrkjoring_lagrer_ingen_kopi(con):
    kid = _kurs(con)
    _meld_paa(con, kid)
    k = Kjoring(con, idag=IDAG, tor=True)
    daglig.kjor(k)
    k.avslutt()
    assert _kopier(con) == []


# ============================ kopien endres aldri ============================

def test_mal_endret_etter_sending_endrer_ikke_sendt_epost(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)
    con.commit()
    maltekster.lagre_maltekst(con, "bekreftelse", "innledning", "Helt ny tekst {fornavn}.")
    con.commit()
    side = _klient().get(f"/admin/kurs/{kid}/deltaker/{pid}/epost/{_kopier(con)[0]['id']}").get_data(as_text=True)
    assert "Takk for påmeldingen!" in side and "Helt ny tekst" not in side


def _send_med_logo(con, kid, pid, logo, nokkel="test:1", pdf=b"%PDF-1.4 vedlegg", til="kari@example.no"):
    html = '<p>Hei</p><img src="cid:logo1" alt="IPR">'
    Kjoring(con, idag=IDAG).send_ferdigrendret_en_gang(
        nokkel, til, "manuell_test", "Med logo", html, paamelding_id=pid,
        vedlegg=[epost.Vedlegg("logo.png", "image/png", logo, cid="logo1"),
                 epost.Vedlegg("Program.pdf", "application/pdf", pdf)])
    con.commit()
    return _fersk(con).execute("SELECT id FROM sendt_epost ORDER BY id DESC").fetchone()[0]


def test_bilder_og_vedlegg_lagres_en_gang_og_vises_slik_de_ble_sendt(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    e1 = _send_med_logo(con, kid, pid, LOGO)
    _send_med_logo(con, kid, pid, LOGO, nokkel="test:2")
    f = _fersk(con)
    assert f.execute("SELECT COUNT(*) FROM epost_fil").fetchone()[0] == 2               # logo + pdf, ikke fire
    assert f.execute("SELECT COUNT(*) FROM sendt_epost_vedlegg").fetchone()[0] == 4
    assert 'src="cid:logo1"' in f.execute("SELECT html FROM sendt_epost WHERE id=?", (e1,)).fetchone()[0]   # uendret
    k = _klient()
    side = k.get(f"/admin/kurs/{kid}/deltaker/{pid}/epost/{e1}").get_data(as_text=True)
    assert "data:image/png;base64," in side and "cid:logo1" not in side and "Program.pdf" in side
    r = k.get(f"/admin/kurs/{kid}/deltaker/{pid}/epost/{e1}/vedlegg/2?last_ned=1")
    assert r.data == b"%PDF-1.4 vedlegg" and "attachment" in r.headers["Content-Disposition"]
    assert r.headers["X-Content-Type-Options"] == "nosniff" and "sandbox" in r.headers["Content-Security-Policy"]


def test_ny_logo_endrer_ikke_gamle_eposter(con):
    import base64
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    gammel = _send_med_logo(con, kid, pid, LOGO)
    ny = _send_med_logo(con, kid, pid, LOGO_NY, nokkel="test:2")
    k = _klient()
    for eid, logo in ((gammel, LOGO), (ny, LOGO_NY)):
        side = k.get(f"/admin/kurs/{kid}/deltaker/{pid}/epost/{eid}").get_data(as_text=True)
        assert base64.b64encode(logo).decode() in side


def test_html_vedlegg_aapnes_aldri_i_nettleseren(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    Kjoring(con, idag=IDAG).send_ferdigrendret_en_gang(
        "test:1", "kari@example.no", "manuell_test", "Emne", "<p>x</p>", paamelding_id=pid,
        vedlegg=[epost.Vedlegg("side.html", "text/html", b"<script>alert(1)</script>")])
    con.commit()
    eid = _kopier(con)[0]["id"]
    r = _klient().get(f"/admin/kurs/{kid}/deltaker/{pid}/epost/{eid}/vedlegg/1")
    assert r.headers["Content-Type"] == "application/octet-stream" and "attachment" in r.headers["Content-Disposition"]


# ============================ visning ============================

def _paamelding(con, kid, pid):
    return _fersk(con).execute(
        "SELECT p.*, d.epost, d.navn FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE p.id=?", (pid,)).fetchone()


def test_fanen_heter_eposter_og_viser_kolonnene(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)
    con.commit()
    side = _klient().get(f"/admin/kurs/{kid}/deltaker/{pid}/kommunikasjon").get_data(as_text=True)
    assert ">E-poster</a>" in side and ">Kommunikasjon</a>" not in side.split('class="faner"', 1)[1].split("</nav>")[0]
    for kolonne in ("Emne", "Dato", "Kl.", "Til", "Fra", "Sendt av", "Status"):
        assert f'<th scope="col">{kolonne}</th>' in side
    assert "Bekreftelse: Veiledning i praksis" in side and "Systemet (automatisk)" in side
    assert config.AVSENDER_EPOST.replace("@", "@<wbr>") in side and "kari@<wbr>example.no" in side and ">Sendt<" in side


@pytest.mark.parametrize("utc,dato,kl", [("2027-01-15 08:30:05", "15.01.2027", "09:30"),      # vintertid
                                         ("2027-07-15 08:30:05", "15.07.2027", "10:30")])     # sommertid
def test_tidspunktet_vises_i_norsk_tid(con, utc, dato, kl):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)
    con.commit()
    con.execute("UPDATE sendt_epost SET opprettet=?, sendt_ts=?", (utc, utc))
    con.commit()
    (rad,) = eposthistorikk.for_paamelding(_fersk(con), _paamelding(con, kid, pid))
    assert (rad.dato, rad.klokkeslett) == (dato, kl)
    side = _klient().get(f"/admin/kurs/{kid}/deltaker/{pid}/epost/{rad.id}").get_data(as_text=True)
    assert f"{dato} kl. {kl}:05" in side


def test_eposter_folger_paameldingen_ikke_adressen(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)
    con.execute("UPDATE deltaker SET epost='ny.adresse@example.no'")
    con.commit()
    rader = eposthistorikk.for_paamelding(_fersk(con), _paamelding(con, kid, pid))
    assert [(r.emne, r.til) for r in rader] == [("Bekreftelse: Veiledning i praksis", "kari@example.no")]


def test_eldre_epost_uten_kopi_vises_med_merknad_og_rekonstrueres_ikke(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    con.execute("INSERT INTO utsending_logg (nokkel, mottaker, type, status, sendt_ts) VALUES (?,?,?,?,?)",
                (f"kurs:{kid}", "kari@example.no", "dagfor-2027-03-21", "sendt", "2027-03-20 07:00:00"))
    con.commit()
    (rad,) = eposthistorikk.for_paamelding(_fersk(con), _paamelding(con, kid, pid))
    assert (rad.id, rad.emne, rad.dato, rad.klokkeslett) == (None, "Påminnelse dagen før (21.03.2027)", "20.03.2027", "08:00")
    side = _klient().get(f"/admin/kurs/{kid}/deltaker/{pid}/kommunikasjon").get_data(as_text=True)
    assert "Innholdet ble ikke lagret for denne eldre e-posten." in side


def _eldre_epost(con, kid, til="kari@example.no", type_="ukefor", ts="2027-03-20 07:00:00"):
    con.execute("INSERT INTO utsending_logg (nokkel, mottaker, type, status, sendt_ts) VALUES (?,?,?,?,?)", (f"kurs:{kid}", til, type_, "sendt", ts))
    con.commit()


def test_alle_rader_i_fanen_er_klikkbare_ogsaa_eldre_e_poster_uten_kopi(con):
    """Camilla (02.10.2026): «jeg ønsker at man skal kunne trykke på den e-posten som er sendt»."""
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)                                           # en ny e-post, med kopi
    _eldre_epost(con, kid)                                                               # og en eldre, uten kopi
    liste = _klient().get(f"/admin/kurs/{kid}/deltaker/{pid}/kommunikasjon").get_data(as_text=True)
    tbody = liste.split("<tbody>")[1].split("</tbody>")[0]
    emnerader = [re.search(r"<td>(.*?)</td>", rad, re.S)[1] for rad in re.findall(r"<tr>(.*?)</tr>", tbody, re.S)]
    assert len(emnerader) == 2 and all("<a href=" in rad for rad in emnerader), emnerader
    assert "/epost/" in emnerader[0] + emnerader[1] and "/epost-eldre?" in emnerader[0] + emnerader[1]


def test_eldre_epost_viser_naar_til_hvem_og_at_innholdet_ikke_er_lagret_uten_aa_gjette(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    _eldre_epost(con, kid)
    (rad,) = eposthistorikk.for_paamelding(_fersk(con), _paamelding(con, kid, pid))
    assert (rad.nokkel, rad.type) == (f"kurs:{kid}", "ukefor")
    side = _klient().get(f"/admin/kurs/{kid}/deltaker/{pid}/epost-eldre", query_string={"nokkel": rad.nokkel, "type": rad.type}).get_data(as_text=True)
    assert "Praktisk informasjon (uken før)" in side and "20.03.2027 kl. 08:00:00" in side and "(norsk tid)" in side
    assert "<b>Til</b> kari@example.no" in side and ">Sendt<" in side
    assert eposthistorikk.IKKE_LAGRET in side and "aldri gjettet" in side
    assert "<iframe" not in side and 'id="vedlegg"' not in side                          # ingen innhold å vise, og ikke påstått noe om vedlegg
    assert "Vedlegg</b>" not in side


def test_eldre_epost_tilhorer_bare_sin_egen_paamelding_og_sitt_eget_kurs(con):
    kid = _kurs(con)
    annet = _kurs(con, kode="E2")
    p1 = _meld_paa(con, kid)
    p2 = _meld_paa(con, kid, epost_="ola@example.no", fornavn="Ola")
    _eldre_epost(con, kid)
    k = _klient()
    q = {"nokkel": f"kurs:{kid}", "type": "ukefor"}
    assert k.get(f"/admin/kurs/{kid}/deltaker/{p1}/epost-eldre", query_string=q).status_code == 200
    assert k.get(f"/admin/kurs/{kid}/deltaker/{p2}/epost-eldre", query_string=q).status_code == 404         # Olas påmelding har ikke den e-posten
    assert k.get(f"/admin/kurs/{annet}/deltaker/{p1}/epost-eldre", query_string=q).status_code == 404       # påmeldingen hører ikke til det kurset
    assert k.get(f"/admin/kurs/{kid}/deltaker/{p1}/epost-eldre", query_string={**q, "type": "bekreftelse"}).status_code == 404
    assert k.get(f"/admin/kurs/{kid}/deltaker/{p1}/epost-eldre").status_code == 404
    assert _klient(logg_inn=False).get(f"/admin/kurs/{kid}/deltaker/{p1}/epost-eldre", query_string=q).status_code in (302, 401, 403)


def test_eldre_epost_som_har_en_kopi_vises_ikke_som_eldre(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)
    kopi = _kopier(con, paamelding_id=pid)[0]
    assert eposthistorikk.eldre_visning(_fersk(con), _paamelding(con, kid, pid), kopi["nokkel"], kopi["type"]) is None


def test_detaljen_har_kompakt_topp_med_sendt_til_fra_og_vedlegg_og_innholdet_under(con):
    """Camilla (02.10.2026): som Pindenas «Melding» (sendt, til, innhold og vedlegg), men med toppen mer komprimert."""
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    eid = _send_med_logo(con, kid, pid, LOGO)                                            # logo i teksten og vedlegget Program.pdf
    side = _klient().get(f"/admin/kurs/{kid}/deltaker/{pid}/epost/{eid}").get_data(as_text=True)
    topp = side.split('class="epostoverskrift"', 1)[1].split("<h3", 1)[0]
    for fakta in ("<b>Sendt</b>", "<b>Til</b>", "<b>Fra</b>", "<b>Sendt av</b>", "<b>Vedlegg</b>"):
        assert fakta in topp, fakta
    assert "kari@example.no" in topp and 'href="#vedlegg"' in topp and "<dl" not in topp and "<ul" not in topp
    assert side.index(">Innhold</h3>") < side.index("<iframe") < side.index('id="vedlegg"') and "Program.pdf" in side


def test_epost_uten_vedlegg_sier_at_det_ikke_er_noen(con, monkeypatch):
    # Bekreftelsen har kalenderfilen vedlagt (Camilla 05.10.2026); her skal e-posten være uten vedlegg
    monkeypatch.setattr(kalender, "vedlegg", lambda kurs, dager: [])
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)
    eid = _kopier(con, paamelding_id=pid)[0]["id"]
    side = _klient().get(f"/admin/kurs/{kid}/deltaker/{pid}/epost/{eid}").get_data(as_text=True)
    assert "<b>Vedlegg</b> ingen" in side and 'id="vedlegg"' not in side


def test_ingen_kan_se_en_annen_paameldings_eposter(con):
    kid = _kurs(con)
    annet = _kurs(con, kode="E2")
    p1 = _meld_paa(con, kid)
    p2 = _meld_paa(con, kid, epost_="ola@example.no", fornavn="Ola")
    p3 = _meld_paa(con, annet, epost_="per@example.no", fornavn="Per")
    eid = _send_med_logo(con, kid, p1, LOGO)
    k = _klient()
    assert k.get(f"/admin/kurs/{kid}/deltaker/{p1}/epost/{eid}").status_code == 200
    assert k.get(f"/admin/kurs/{kid}/deltaker/{p2}/epost/{eid}").status_code == 404
    assert k.get(f"/admin/kurs/{annet}/deltaker/{p3}/epost/{eid}").status_code == 404
    assert k.get(f"/admin/kurs/{annet}/deltaker/{p1}/epost/{eid}").status_code == 404
    assert k.get(f"/admin/kurs/{kid}/deltaker/{p2}/epost/{eid}/vedlegg/2").status_code == 404
    assert _klient(logg_inn=False).get(f"/admin/kurs/{kid}/deltaker/{p1}/epost/{eid}").status_code == 302


def test_skript_i_eposten_kjorer_ikke(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    Kjoring(con, idag=IDAG).send_ferdigrendret_en_gang(
        "test:1", "kari@example.no", "manuell_test", "Emne", '<p>Hei</p><script>alert("x")</script>', paamelding_id=pid)
    con.commit()
    side = _klient().get(f"/admin/kurs/{kid}/deltaker/{pid}/epost/{_kopier(con)[0]['id']}").get_data(as_text=True)
    ramme = re.search(r"<iframe[^>]*>", side)[0]
    assert re.search(r"\bsandbox\b(?!=)", ramme) and 'sandbox="' not in ramme      # sandbox uten unntak: ingen skript
    assert "<script>alert" not in side and "&lt;script&gt;alert" in ramme            # bare som tekst i srcdoc


# ============================ sletting og migrering ============================

def test_retten_til_sletting_fjerner_kopiene_og_filene(con):
    kid = _kurs(con)
    kari = _meld_paa(con, kid)
    ola = _meld_paa(con, kid, epost_="ola@example.no", fornavn="Ola")
    _send_med_logo(con, kid, kari, LOGO, pdf=b"%PDF bare kari")
    _send_med_logo(con, kid, ola, LOGO, nokkel="test:2", pdf=b"%PDF bare ola", til="ola@example.no")
    did = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (kari,)).fetchone()[0]
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()
    ut = db.anonymiser_deltaker(con, did, aktor="admin:test")
    con.commit()
    f = _fersk(con)
    assert ut["epostkopier"] == 1 and [r["paamelding_id"] for r in _kopier(con)] == [ola]
    filer = {r[0] for r in f.execute("SELECT innhold FROM epost_fil")}
    import base64
    assert base64.b64encode(b"%PDF bare kari").decode() not in filer                   # bare Karis fil er borte
    assert base64.b64encode(b"%PDF bare ola").decode() in filer and base64.b64encode(LOGO).decode() in filer


def test_migreringen_lager_tabellene_og_kan_kjores_to_ganger(con):
    nr = next(n for n, navn, _ in migreringer.MIGRERINGER if navn == "eposthistorikk")   # tåler omnummerering
    for tabell in ("sendt_epost_vedlegg", "epost_fil", "sendt_epost"):
        con.execute(f"DROP TABLE {tabell}")
    con.execute("DELETE FROM schema_versjon WHERE versjon>=?", (nr,))
    con.commit()
    assert migreringer.kjor_manglende(con) == list(range(nr, migreringer.KODEVERSJON + 1))
    assert all(db.har_tabell(con, t) for t in ("sendt_epost", "epost_fil", "sendt_epost_vedlegg"))
    assert migreringer.kjor_manglende(con) == []
    migreringer.kontroller(con)


def test_database_migrert_av_en_annen_gren_stoppes_med_tydelig_feil(con):
    """Vakten for parallelle grener med samme migreringsnummer: mangler tabeller eller kolonner, stopper systemet."""
    con.execute("DROP TABLE sendt_epost_vedlegg")
    con.commit()
    with pytest.raises(migreringer.VersjonsFeil, match="sendt_epost_vedlegg"):
        migreringer.kontroller(con)
    con.execute("ALTER TABLE sendt_epost DROP COLUMN kopi")
    con.commit()
    with pytest.raises(migreringer.VersjonsFeil, match="sendt_epost.kopi"):
        migreringer.kjor_manglende(con)
