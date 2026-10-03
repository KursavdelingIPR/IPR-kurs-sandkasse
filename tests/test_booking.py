"""Booking per samling (kurs/booking.py: lokale, hotell, grupperom, lunsj) og det man gjør med sjekklistepunktene rett i
kalenderen: notat, slette, ta tilbake og legge til (Camilla 03.10.2026). Alle kurs og tekster her er oppdiktet."""
import re
from datetime import date, timedelta

import pytest
from werkzeug.datastructures import MultiDict

from kurs import booking, config, db, migreringer, planlagte_kurs, sjekklister

FRAM = date.today() + timedelta(days=60)
NL = chr(10)


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _klient(brukernavn=None, passord=None):
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": brukernavn or config.ADMIN_BRUKERNAVN,
                                    "passord": passord or config.ADMIN_PASSORD})
    return k


def _plan_med_mal(con, navn="Bookingkurs", fra=FRAM):
    """Planlagt kurs med én samling og en mal med to punkter (sjekklisten er laget). (plan_id, samling_id, {tekst: id})"""
    mid = sjekklister.opprett_mal(con, sjekklister.valider_mal(MultiDict({"navn": "Mal " + navn})), "admin:test")
    for tekst in ("Har vi booket hotellrom?", "Send info til kursholder"):
        sjekklister.opprett_malpunkt(con, mid, sjekklister.valider_malpunkt(MultiDict(
            {"tekst": tekst, "frist_antall": "1", "frist_enhet": "maaneder", "frist_retning": "for", "frist_fra": "start"})),
            "admin:test")
    s = planlagte_kurs.valider_samling(MultiDict({"fra_dato": fra.isoformat(), "til_dato": fra.isoformat(), "sted": "Oslo"}))
    pid = planlagte_kurs.opprett(con, planlagte_kurs.valider_kurs(MultiDict({"navn": navn})), [s], "admin:test")
    sid = planlagte_kurs.hent(con, pid)["samlinger"][0]["id"]
    sjekklister.sett_mal(con, pid, mid, "admin:test")
    sjekklister.lag_for_samling(con, sid, "admin:test")
    con.commit()
    return pid, sid, {r["tekst"]: r["id"] for r in con.execute("SELECT id, tekst FROM sjekkliste_punkt WHERE samling_id=?",
                                                               (sid,))}


def _panel(t, sid):
    return re.search(rf'<div class="osjekk" id="skpanel-{sid}"[^>]*>(.*?)</div>\s*</article>', t, re.S).group(1)


def test_migrering_24_lager_tabellen_og_kolonnene(con):
    assert (24, "booking_og_notat") in [(n, navn) for n, navn, _ in migreringer.MIGRERINGER]
    assert db.har_tabell(con, "samling_booking")
    assert db.har_kolonne(con, "sjekkliste_punkt", "notat") and db.har_kolonne(con, "sjekkliste_punkt", "slettet")


def test_booking_lagres_endres_og_fjernes_og_loggen_har_ikke_teksten(con):
    _, sid, _ = _plan_med_mal(con)
    assert booking.sett(con, sid, "hotell", "booket", " Thon Cecil" + NL + "Ref. 4711 ", "admin:test") == "lagret"
    assert booking.sett(con, sid, "hotell", "booket", "Thon Cecil" + NL + "Ref. 4711", "admin:test") == "uendret"
    assert booking.sett(con, sid, "grupperom", "ikke_booket", "", "admin:test") == "lagret"
    rader = booking.for_samlinger(con, [sid])[sid]
    assert rader["hotell"]["tekst"] == "Thon Cecil" + NL + "Ref. 4711" and rader["hotell"]["endret_av"] == "admin:test"
    assert [(b["type"], b["statusnavn"]) for b in booking.linjer(rader)] == [
        ("lokale", "Ikke satt"), ("hotell", "Booket"), ("grupperom", "Ikke booket"), ("lunsj", "Ikke satt")]
    for type_, status, tekst in (("hotell", "kanskje", ""), ("parkering", "booket", ""), ("hotell", "booket", "x" * 1001)):
        with pytest.raises(booking.BookingFeil):
            booking.sett(con, sid, type_, status, tekst, "admin:test")
    with pytest.raises(booking.BookingFeil):
        booking.sett(con, 999999, "hotell", "booket", "", "admin:test")
    assert booking.sett(con, sid, "hotell", "", "  ", "admin:test") == "fjernet"
    assert "hotell" not in booking.for_samlinger(con, [sid])[sid]
    con.commit()
    detaljer = " ".join(r[0] for r in con.execute("SELECT detaljer FROM hendelse WHERE handling='samling_booking_endret'"))
    assert "Thon" not in detaljer and "4711" not in detaljer


def test_kalenderen_har_booking_nederst_notat_slett_og_nytt_punkt(con):
    pid, sid, punkter = _plan_med_mal(con)
    k = _klient()
    panel = _panel(k.get("/admin/aktiviteter/kalender").get_data(as_text=True), sid)
    assert f'id="skb-{sid}"' in panel and "Kurslokale" in panel and "Hotell" in panel and "Grupperom" in panel
    assert "Lunsj" in panel and "Ikke satt" in panel and "Legg til punkt" in panel and "Lagre notat" in panel
    assert panel.index("Har vi booket hotellrom?") < panel.index(f'id="skb-{sid}"')          # bookingen står nederst
    r = k.post(f"/admin/aktiviteter/booking/{sid}", data={"type": "hotell", "status": "booket",
                                                         "tekst": "Thon Cecil, ref. 4711", "neste": "/admin/aktiviteter/kalender"})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/admin/aktiviteter/kalender?apen={sid}#sk-{sid}")
    hotell = punkter["Har vi booket hotellrom?"]
    k.post(f"/admin/aktiviteter/sjekkliste/{hotell}", data={"handling": "notat", "notat": "Booket 03.10 av Kari",
                                                           "neste": "/admin/aktiviteter/kalender"})
    k.post(f"/admin/aktiviteter/planlagte-kurs/{pid}/samling/{sid}/sjekkliste",
           data={"handling": "nytt_punkt", "tekst": "Bestill blomster", "neste": "/admin/aktiviteter/kalender"})
    k.post(f"/admin/aktiviteter/sjekkliste/{punkter['Send info til kursholder']}",
           data={"handling": "slett", "neste": "/admin/aktiviteter/kalender"})
    panel = _panel(k.get(f"/admin/aktiviteter/kalender?apen={sid}").get_data(as_text=True), sid)
    assert 'class="skb skb-booket"' in panel and "Thon Cecil, ref. 4711" in panel
    assert '<p class="sk-notat">Booket 03.10 av Kari</p>' in panel and "Bestill blomster" in panel
    assert "Send info til kursholder" in panel.split("Slettede punkter (1)")[1]               # bare under «Slettede punkter»
    side = k.get(f"/admin/aktiviteter/planlagte-kurs/{pid}").get_data(as_text=True)
    assert f'id="skb-{sid}"' in side and "Thon Cecil, ref. 4711" in side                    # også på kursets side


def test_slettede_punkter_er_ikke_med_i_paaminnelser_og_kommer_ikke_tilbake_fra_malen(con):
    _, sid, punkter = _plan_med_mal(con, fra=date.today() + timedelta(days=10))          # fristene er passert
    idag = date.today()
    assert sjekklister.paaminnelser(con, idag)["forfalt"] == 2 and sid in sjekklister.forfalte_samlinger(con, idag)
    sjekklister.slett_punkt(con, punkter["Har vi booket hotellrom?"], "admin:test")
    sjekklister.slett_punkt(con, punkter["Send info til kursholder"], "admin:test")
    assert sjekklister.paaminnelser(con, idag)["forfalt"] == 0 and sid not in sjekklister.forfalte_samlinger(con, idag)
    assert sjekklister.oppdater_fra_mal(con, sid, "admin:test")["lagt_til"] == 0             # malen legger dem ikke inn igjen
    sk = sjekklister.for_samlinger(con, [sid], idag)[sid]
    assert sk.antall == 0 and [p.tekst for p in sk.slettede] == ["Har vi booket hotellrom?", "Send info til kursholder"]


def test_lesetilgang_ser_bookingen_men_kan_ikke_endre(con):
    _, sid, _ = _plan_med_mal(con)
    booking.sett(con, sid, "lunsj", "ikke_booket", "Paleet", "admin:test")
    db.opprett_admin_bruker(con, "leser", "Leser", "lese-passord-1", rolle="lese")
    con.commit()
    les = _klient("leser", "lese-passord-1")
    panel = _panel(les.get("/admin/aktiviteter/kalender").get_data(as_text=True), sid)
    assert 'class="skb skb-ikke_booket"' in panel and "Paleet" in panel
    assert "Lagre notat" not in panel and ">Endre<" not in panel and "Legg til punkt" not in panel
    assert les.post(f"/admin/aktiviteter/booking/{sid}", data={"type": "lunsj", "status": "booket"}).status_code == 403
