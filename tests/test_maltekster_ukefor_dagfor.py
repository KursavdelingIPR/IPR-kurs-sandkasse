"""Fase 12B2C-2: `ukefor` og `dagfor` koblet til maltekstsystemet - med streng rekkefolge og feil-isolasjon i daglig jobb.

Redigerbar tekst: emne/innledning/avslutning. LAASTE systemblokker i malfilene: kursdager/tid, sted/QR, Zoom-lenke/ID/passord,
kursnotat. Sikker rekkefolge per kurs: (1) preflight av maltekst (ren lesing) -> (2) evt. NY Zoom-opprettelse -> (3) render ->
(4) claim -> (5) send. En MalFeil isoleres per utsending og kan aldri abortere daglig.kjor() (personvern-sletting m.m. kjorer).
"""
import ast
import json
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from kurs import config, daglig, db, maltekster
from kurs.integrasjoner import epost, zoom
from kurs.kjoring import Kjoring
from kurs.maltekster import MANGLER_VERDI, MalFeil

IDAG = date(2027, 3, 1)


def _n(html):
    return " ".join(html.split())


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


@pytest.fixture
def ut(monkeypatch):
    """Fanger e-post (til, emne, html) og Zoom-opprettelser."""
    kall = {"epost": [], "zoom": []}
    monkeypatch.setattr(epost, "send", lambda til, emne, html, *a, **kw: kall["epost"].append((til, emne, html)))
    ekte = zoom.opprett_mote

    def opprett(kursnavn):
        kall["zoom"].append(kursnavn)
        return ekte(kursnavn)
    monkeypatch.setattr(zoom, "opprett_mote", opprett)
    return kall


def _kurs(con, kode="U1", start=date(2027, 3, 4), ant_dager=1, **kw):
    datoer = [(start + timedelta(days=n)).isoformat() for n in range(ant_dager)]
    kid = db.opprett_kurs(con, kode=kode, navn="Veiledning i praksis", datoer=datoer, sharepoint_mappe="Kurs/" + kode,
                          **{"type": "fysisk", "sted": "Oslo", "pris_nok": 0, **kw})
    con.commit()
    return kid


def _deltaker(con, kid, epost_="ola@x.no", navn="Ola Nordmann"):
    pid, _ = db.meld_paa(con, kid, epost=epost_, navn=navn)
    con.execute("UPDATE paamelding SET sveiper_kjort=1 WHERE id=?", (pid,))     # bekreftelse er irrelevant her
    con.commit()
    return pid


def _daglig(con, idag=IDAG):
    daglig.kjor(Kjoring(con, idag=idag))


def _mail(ut, til="ola@x.no", start=None):
    m = [x for x in ut["epost"] if x[0] == til and (start is None or x[1].startswith(start))]
    (t, emne, html), = m
    return emne, _n(html)


def _lagre(con, mal, felt, tekst):
    maltekster.lagre_maltekst(con, mal, felt, tekst, aktor="admin:test")
    con.commit()


def _korrupt(con, mal, felt, tekst):
    con.execute("INSERT INTO mal_tekst (mal, felt, tekst) VALUES (?, ?, ?) "
                "ON CONFLICT (mal, felt) DO UPDATE SET tekst=excluded.tekst", (mal, felt, tekst))
    con.commit()


def _logg(con, mal_type="innkalling_feil"):
    return [json.loads(r[0]) for r in con.execute("SELECT detaljer FROM hendelse WHERE handling=?", (mal_type,))]


def _claims(con, type_):
    return con.execute("SELECT COUNT(*) FROM utsending_logg WHERE type LIKE ?", (type_ + "%",)).fetchone()[0]


# ================================== UKEFOR: standard, varianter, override ==================================

def test_ukefor_standardtekst_fysisk_og_lik_direkte_render(con, ut):
    kid = _kurs(con, ant_dager=2)
    _deltaker(con, kid)
    _daglig(con, IDAG)                                                       # 3 dager for start -> ukefor
    emne, html = _mail(ut, start="Velkommen")
    assert emne == "Velkommen til Veiledning i praksis – praktisk informasjon"
    assert "<p>Hei Ola Nordmann,</p>" in html
    assert "Nå er det snart tid for <strong>Veiledning i praksis</strong>, som starter 2027-03-04." in html
    assert "<li>2027-03-04 kl. 09:00–16:00</li>" in html and "<li>2027-03-05 kl. 09:00–16:00</li>" in html
    assert "<strong>Sted:</strong> Oslo" in html and f"taste koden på {config.BASE_URL}/innsjekk" in html
    assert "Kurset holdes på Zoom" not in html
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    d = {"navn": "Ola Nordmann"}
    direkte = epost.render("ukefor", d=d, kurs=kurs, dager=db.kursdager(con, kid))[1]
    assert _n(direkte) == html


def test_ukefor_digitalt_og_hybrid(con, ut):
    kd = _kurs(con, kode="D1", type="digital", zoom_url="https://zoom.example/j/1")
    kh = _kurs(con, kode="H1", type="hybrid", zoom_url="https://zoom.example/j/2")
    _deltaker(con, kd, "d@x.no")
    _deltaker(con, kh, "h@x.no")
    _daglig(con, IDAG)
    _, digital = _mail(ut, "d@x.no")
    _, hybrid = _mail(ut, "h@x.no")
    assert "Kurset holdes på Zoom. Du får lenken på e-post dagen før hver kursdag. Test gjerne lyd og bilde på forhånd." in digital
    assert "Sted:" not in digital and "QR-koden" not in digital
    assert "<strong>Sted:</strong> Oslo" in hybrid and "Kurset holdes på Zoom" not in hybrid      # dagens semantikk: hybrid faar sted


def test_ukefor_gyldig_override_brukes_og_systemblokkene_er_uendret(con, ut):
    _lagre(con, "ukefor", "emne", "Snart: {kursnavn}")
    _lagre(con, "ukefor", "innledning", "Hei {navn}!" + chr(10) + chr(10) + "{kursnavn} starter {startdato}.")
    _lagre(con, "ukefor", "avslutning", "Se {min_side}.")
    kid = _kurs(con, notat="Ta med votter")
    _deltaker(con, kid)
    _daglig(con, IDAG)
    emne, html = _mail(ut, start="Snart")
    assert emne == "Snart: Veiledning i praksis" and "<p>Hei Ola Nordmann!</p>" in html
    assert "<strong>Veiledning i praksis</strong> starter 2027-03-04." in html
    assert f'Se <a href="{config.BASE_URL}/min-side">Min side</a>.' in html
    assert "<li>2027-03-04 kl. 09:00–16:00</li>" in html and "<strong>Sted:</strong> Oslo" in html and "<p>Ta med votter</p>" in html


def test_ukefor_reset_gir_standard_igjen(con, ut):
    _lagre(con, "ukefor", "innledning", "Egen tekst {navn}.")
    kid = _kurs(con)
    _deltaker(con, kid, "a@x.no")
    _daglig(con, IDAG)
    assert "Egen tekst Ola Nordmann." in _mail(ut, "a@x.no")[1]
    assert maltekster.tilbakestill_maltekst(con, "ukefor", "innledning", aktor="admin:test") is True
    con.commit()
    _deltaker(con, kid, "b@x.no")
    _daglig(con, IDAG)
    _, html = _mail(ut, "b@x.no")
    assert "Nå er det snart tid for <strong>Veiledning i praksis</strong>" in html and "Egen tekst" not in html


def test_ukefor_laaste_blokker_kan_ikke_fjernes_med_tom_eller_minimal_tekst(con, ut):
    _lagre(con, "ukefor", "innledning", "X")
    _lagre(con, "ukefor", "avslutning", "")
    kid = _kurs(con, type="digital", zoom_url="https://zoom.example/j/1", notat="Ta med votter")
    _deltaker(con, kid)
    _daglig(con, IDAG)
    _, html = _mail(ut)
    assert "<p>X</p>" in html and "<li>2027-03-04 kl. 09:00–16:00</li>" in html
    assert "Kurset holdes på Zoom" in html and "<p>Ta med votter</p>" in html


def test_html_i_redigerbar_tekst_escapes_i_begge_maler(con, ut):
    _lagre(con, "ukefor", "innledning", "<script>a</script> {navn}")
    _lagre(con, "dagfor", "innledning_forste", "<b>{navn}</b> & Co")
    kid = _kurs(con)
    _deltaker(con, kid)
    _daglig(con, IDAG)
    _daglig(con, date(2027, 3, 3))
    for start in ("Velkommen", "I morgen"):
        _, html = _mail(ut, start=start)
        assert "<script>" not in html and "<b>" not in html


@pytest.mark.parametrize("mal,felt", [("ukefor", "innledning"), ("dagfor", "innledning_midt")])
@pytest.mark.parametrize("tekst", ["{zoom_pw}", "{zoom_url}", "{zoom_id}", "{sted}", "{belop}", "{{ kurs.zoom_pw }}", "{% if x %}"])
def test_zoom_hemmeligheter_og_andre_systemdata_er_aldri_placeholders(con, mal, felt, tekst):
    with pytest.raises(MalFeil):
        maltekster.lagre_maltekst(con, mal, felt, tekst)
    assert con.execute("SELECT COUNT(*) FROM mal_tekst").fetchone()[0] == 0


def test_registeret_for_ukefor_og_dagfor_har_ingen_zoom_eller_stedskoder():
    for mal in ("ukefor", "dagfor"):
        koder = set().union(*(f.kode for f in maltekster.MALER[mal].felt.values()))
        assert not [k for k in koder if "zoom" in k or "sted" in k or "pass" in k], koder
    assert set().union(*(f.kode for f in maltekster.MALER["ukefor"].felt.values())) == {"navn", "kursnavn", "startdato", "min_side"}


# ================================== UKEFOR: Zoom, tidsvindu, idempotens ==================================

def test_ukefor_med_eksisterende_zoom_oppretter_ikke_nytt_moete(con, ut):
    kid = _kurs(con, type="digital", zoom_url="https://zoom.example/j/9", zoom_id="9")
    _deltaker(con, kid)
    _daglig(con, IDAG)
    assert ut["zoom"] == [] and len(ut["epost"]) == 1


def test_ukefor_med_ny_zoom_oppretter_moetet_foerst_etter_gyldig_preflight_og_sender(con, ut):
    kid = _kurs(con, type="digital")
    _deltaker(con, kid)
    _daglig(con, IDAG)                                                       # 3 dager for: baade Zoom (<=8) og ukefor (<=7)
    assert ut["zoom"] == ["Veiledning i praksis"] and len(ut["epost"]) == 1
    assert con.execute("SELECT zoom_url FROM kurs WHERE id=?", (kid,)).fetchone()[0].startswith("https://zoom.us/j/")


@pytest.mark.parametrize("dager_til,forventet", [(8, 0), (7, 1), (1, 1), (0, 0)])
def test_ukefor_tidsvindu_er_uendret_7_til_1_dager_for_start(con, ut, dager_til, forventet):
    kid = _kurs(con, start=IDAG + timedelta(days=dager_til))
    _deltaker(con, kid)
    _daglig(con, IDAG)
    assert len([m for m in ut["epost"] if m[1].startswith("Velkommen")]) == forventet


def test_ukefor_sendes_kun_til_bekreftede_og_kun_en_gang(con, ut):
    kid = _kurs(con)
    _deltaker(con, kid, "a@x.no")
    p2 = _deltaker(con, kid, "b@x.no")
    db.meld_av(con, p2)
    con.commit()
    for _ in range(3):
        _daglig(con, IDAG)
    assert [m[0] for m in ut["epost"] if m[1].startswith("Velkommen")] == ["a@x.no"]


def test_ukefor_ukjent_epostutfall_gir_ingen_automatisk_retry(con, monkeypatch):
    forsok = []

    def feiler(til, emne, html, *a, **k):
        forsok.append(emne)
        raise RuntimeError("Graph nede")
    monkeypatch.setattr(epost, "send", feiler)
    kid = _kurs(con)
    _deltaker(con, kid)
    _daglig(con, IDAG)                                                         # Graph-feilen isoleres: jobben fortsetter
    assert con.execute("SELECT status FROM utsending_logg WHERE type='ukefor'").fetchone()[0] == "ukjent"
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='daglig_feil'").fetchone()[0] == 1
    _daglig(con, IDAG)
    _daglig(con, IDAG + timedelta(days=1))
    assert len(forsok) == 1                                                    # aldri nytt forsok


# ================================== DAGFOR ==================================

def test_dagfor_standardtekst_og_riktig_variant_per_kursdag(con, ut):
    kid = _kurs(con, start=date(2027, 3, 4), ant_dager=3)
    _deltaker(con, kid)
    forventet = [(date(2027, 3, 3), "I morgen starter Veiledning i praksis", "I morgen starter <strong>Veiledning i praksis</strong>!", "2027-03-04"),
                 (date(2027, 3, 4), "I morgen, dag 2: Veiledning i praksis", "I morgen er dag 2 av 3 på <strong>Veiledning i praksis</strong>.", "2027-03-05"),
                 (date(2027, 3, 5), "Siste kursdag i morgen: Veiledning i praksis", "I morgen er siste kursdag på <strong>Veiledning i praksis</strong>.", "2027-03-06")]
    for idag, emne, setning, dato in forventet:
        ut["epost"].clear()
        _daglig(con, idag)
        mails = [m for m in ut["epost"] if m[1] == emne]
        assert len(mails) == 1, (idag, [m[1] for m in ut["epost"]])
        h = _n(mails[0][2])
        assert "<p>Hei Ola Nordmann,</p>" in h and setning in h and f"Tid: {dato} kl. 09:00–16:00" in h
        assert "Sted: Oslo. Husk å registrere oppmøte med QR-koden når du kommer." in h
    typer = sorted(r[0] for r in con.execute("SELECT type FROM utsending_logg WHERE type LIKE 'dagfor-%'"))
    assert typer == ["dagfor-2027-03-04", "dagfor-2027-03-05", "dagfor-2027-03-06"]      # egen nokkel per kursdag (uendret)


def test_dagfor_enkeltdags_kurs_bruker_forste_variant(con, ut):
    kid = _kurs(con)
    _deltaker(con, kid)
    _daglig(con, date(2027, 3, 3))
    assert _mail(ut, start="I morgen")[0] == "I morgen starter Veiledning i praksis"


def test_dagfor_tider_og_sted_kommer_fra_kursdagen_og_kurset(con, ut):
    kid = _kurs(con)
    con.execute("UPDATE kursdag SET start_kl='10:00', slutt_kl='12:30' WHERE kurs_id=?", (kid,))
    con.commit()
    _deltaker(con, kid)
    _daglig(con, date(2027, 3, 3))
    _, html = _mail(ut, start="I morgen")
    assert "Tid: 2027-03-04 kl. 10:00–12:30" in html and "Sted: Oslo." in html


def test_dagfor_med_eksisterende_zoom_har_laast_zoomblokk_uten_ny_opprettelse(con, ut):
    kid = _kurs(con, type="digital", zoom_url="https://zoom.example/j/1", zoom_id="123 456", zoom_pw="pw1")
    _deltaker(con, kid)
    _daglig(con, date(2027, 3, 3))
    _, html = _mail(ut, start="I morgen")
    assert '<a href="https://zoom.example/j/1"' in html and "Møte-ID: 123 456 · Passord: pw1" in html
    assert "Sted:" not in html and ut["zoom"] == []


def test_dagfor_med_ny_zoom_har_den_nyopprettede_lenken_i_mailen(con, ut):
    kid = _kurs(con, type="digital")
    _deltaker(con, kid)
    _daglig(con, date(2027, 3, 3))                                             # dagen for start, Zoom mangler: opprettes foer mailen bygges
    assert len(ut["zoom"]) == 1
    url = con.execute("SELECT zoom_url FROM kurs WHERE id=?", (kid,)).fetchone()[0]
    _, html = _mail(ut, start="I morgen")
    assert f'<a href="{url}"' in html and "Bli med på Zoom" in html


def test_dagfor_laaste_zoom_og_stedsblokker_kan_ikke_fjernes_med_tom_eller_minimal_tekst(con, ut):
    _lagre(con, "dagfor", "innledning_forste", "X")
    _lagre(con, "dagfor", "avslutning", "")
    kid = _kurs(con, type="hybrid", zoom_url="https://zoom.example/j/1", zoom_id="123", zoom_pw="pw1")
    _deltaker(con, kid)
    _daglig(con, date(2027, 3, 3))
    _, html = _mail(ut, start="I morgen")
    assert "<p>X</p>" in html and "Tid: 2027-03-04 kl. 09:00–16:00" in html
    assert "Bli med på Zoom" in html and "Møte-ID: 123 · Passord: pw1" in html and "Sted: Oslo. Husk å registrere oppmøte" in html


def test_dagfor_gyldig_override_reset_og_laast_blokk(con, ut):
    _lagre(con, "dagfor", "emne_forste", "Snart: {kursnavn} ({dato})")
    _lagre(con, "dagfor", "innledning_forste", "Hei {navn}, i morgen er dag {dagnummer} av {antall_dager}.")
    _lagre(con, "dagfor", "avslutning", "Lykke til!")
    kid = _kurs(con)
    _deltaker(con, kid, "a@x.no")
    _daglig(con, date(2027, 3, 3))
    emne, html = _mail(ut, "a@x.no", start="Snart")
    assert emne == "Snart: Veiledning i praksis (2027-03-04)"
    assert "<p>Hei Ola Nordmann, i morgen er dag 1 av 1.</p>" in html and "Lykke til!" in html
    assert "Tid: 2027-03-04 kl. 09:00–16:00" in html and "Husk å registrere oppmøte med QR-koden" in html
    for felt in ("emne_forste", "innledning_forste", "avslutning"):
        maltekster.tilbakestill_maltekst(con, "dagfor", felt, aktor="admin:test")
    con.commit()
    _deltaker(con, kid, "b@x.no")
    _daglig(con, date(2027, 3, 3))
    emne_b, _ = _mail(ut, "b@x.no", start="I morgen")
    assert emne_b == "I morgen starter Veiledning i praksis"


@pytest.mark.parametrize("dager_til,forventet", [(2, 0), (1, 1), (0, 0)])
def test_dagfor_tidsvindu_er_uendret_kun_dagen_for(con, ut, dager_til, forventet):
    kid = _kurs(con, start=IDAG + timedelta(days=dager_til))
    _deltaker(con, kid)
    _daglig(con, IDAG)
    assert len([m for m in ut["epost"] if m[1].startswith("I morgen")]) == forventet


def test_dagfor_ingen_duplikat_og_ukjent_epostutfall_gir_ingen_retry(con, ut, monkeypatch):
    kid = _kurs(con, start=date(2027, 3, 4))
    _deltaker(con, kid)
    for _ in range(3):
        _daglig(con, date(2027, 3, 3))
    assert len([m for m in ut["epost"] if m[1].startswith("I morgen")]) == 1
    forsok = []
    monkeypatch.setattr(epost, "send", lambda til, emne, html, *a, **k: (forsok.append(emne), (_ for _ in ()).throw(RuntimeError("Graph nede")))[1])
    kid2 = _kurs(con, kode="U2", start=date(2027, 3, 4))
    _deltaker(con, kid2, "z@x.no")
    for _ in range(4):                                                         # ukefor feiler foerst, deretter dagfor; deretter ingenting
        try:
            _daglig(con, date(2027, 3, 3))
        except RuntimeError:
            pass
    assert len([e for e in forsok if e.startswith("I morgen")]) == 1 and len([e for e in forsok if e.startswith("Velkommen")]) == 1


# ================================== KORRUPT MAL: isolasjon, Zoom, resten av daglig ==================================

def _gammelt_kurs_med_sensitivt(con):
    """Deterministisk observerbar effekt av et SENERE daglig steg: personvern-sletting (steg 7)."""
    kid = db.opprett_kurs(con, kode="GAMMEL", navn="Gammelt kurs", datoer=["2026-01-10"], sharepoint_mappe="Kurs/G", type="fysisk", sted="Oslo")
    pid, _ = db.meld_paa(con, kid, epost="gammel@x.no", navn="Gammel", sensitivt={"allergier": "nøtter", "tilrettelegging": "rullestol"})
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM sensitivt").fetchone()[0] == 1
    return pid


KORRUPTE = [("ukefor", "innledning", "Hei {ukjent_kode}"), ("ukefor", "emne", "Hei {navn"), ("ukefor", "finnes_ikke", "Hei"),
            ("dagfor", "innledning_midt", "{zoom_pw}"), ("dagfor", "emne_siste", "Slutt {navn"), ("dagfor", "finnes_ikke", "Hei")]


@pytest.mark.parametrize("mal,felt,tekst", KORRUPTE)
def test_korrupt_mal_isoleres_resten_av_daglig_fortsetter_inkludert_personvern(con, ut, mal, felt, tekst, monkeypatch):
    _gammelt_kurs_med_sensitivt(con)
    _korrupt(con, mal, felt, tekst)
    # begge maltyper er "i dag": kurs 1 starter i morgen (ukefor + dagfor), kurs 2 er et digitalt kurs helt uten Zoom
    k1 = _kurs(con, kode="U1", start=IDAG + timedelta(days=1), ant_dager=3, type="fysisk")
    _deltaker(con, k1, "ola@x.no")
    k2 = _kurs(con, kode="U2", start=IDAG + timedelta(days=3), type="digital")
    _deltaker(con, k2, "vera@x.no", "Vera")
    ekte_res = db.reserver_sending
    claims = []
    monkeypatch.setattr(db, "reserver_sending", lambda con_, nokkel, til, type_: (claims.append(type_), ekte_res(con_, nokkel, til, type_))[1])
    _daglig(con, IDAG)                                                        # kaster IKKE
    tapt = "ukefor" if mal == "ukefor" else "dagfor"
    assert not [c for c in claims if c.startswith(tapt)]                      # ingen e-postclaim for den ugyldige maltypen
    assert con.execute("SELECT COUNT(*) FROM utsending_logg WHERE type LIKE ?", (tapt + "%",)).fetchone()[0] == 0
    assert not [m for m in ut["epost"] if (m[1].startswith("Velkommen") if mal == "ukefor" else m[1].startswith("I morgen") or m[1].startswith("Siste"))]
    assert con.execute("SELECT COUNT(*) FROM sensitivt").fetchone()[0] == 0   # SENERE steg (personvern) kjoerte likevel
    h = _logg(con)
    assert h and all(set(d) == {"kurs_id", "mal", "felt", "grunn", "antall"} and d["mal"] == mal for d in h)
    tekst_logg = json.dumps(h)
    assert "@" not in tekst_logg and "Ola" not in tekst_logg and "Hei" not in tekst_logg and "Slutt" not in tekst_logg   # ingen maltekst/navn/e-post
    assert not any("{" in str(v) for d in h for v in d.values())              # ingen maltekst i loggen


def test_korrupt_ukefor_paavirker_ikke_dagfor_og_andre_kurs(con, ut):
    _korrupt(con, "ukefor", "innledning", "Hei {ukjent_kode}")
    k1 = _kurs(con, kode="U1", start=IDAG + timedelta(days=1), type="digital", zoom_url="https://zoom.example/j/1")   # ukefor + dagfor i dag
    _deltaker(con, k1, "ola@x.no")
    k2 = _kurs(con, kode="U2", start=IDAG + timedelta(days=1), type="fysisk")
    _deltaker(con, k2, "vera@x.no", "Vera")
    _daglig(con, IDAG)
    assert [m[0] for m in ut["epost"] if m[1].startswith("I morgen")] == ["ola@x.no", "vera@x.no"]           # dagfor gikk ut
    assert not [m for m in ut["epost"] if m[1].startswith("Velkommen")]
    assert len(_logg(con)) == 2                                                                            # EN hendelse per kurs, ikke per mottaker


@pytest.mark.parametrize("mal,felt,tekst,dager_til", [("ukefor", "innledning", "Hei {ukjent_kode}", 3), ("dagfor", "innledning_forste", "Hei {ukjent_kode}", 1)])
def test_korrupt_mal_paa_digitalt_kurs_uten_zoom_kaller_aldri_zoom_opprett(con, ut, mal, felt, tekst, dager_til):
    _korrupt(con, mal, felt, tekst)
    kid = _kurs(con, type="digital", start=IDAG + timedelta(days=dager_til))
    _deltaker(con, kid)
    _daglig(con, IDAG)
    assert ut["zoom"] == []                                                                                # ZOOM-VAKT
    assert con.execute("SELECT zoom_url FROM kurs WHERE id=?", (kid,)).fetchone()[0] is None
    assert _claims(con, mal) == 0 and not [m for m in ut["epost"] if m[1].startswith("Velkommen" if mal == "ukefor" else "I morgen")]
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='zoom_opprettet'").fetchone()[0] == 0


UKEFOR_KORRUPT = [("ukefor", "innledning", "Hei {ukjent_kode}"), ("ukefor", "emne", "Hei {navn"), ("ukefor", "finnes_ikke", "Hei")]
DAGFOR_KORRUPT = [("dagfor", "innledning_midt", "{zoom_pw}"), ("dagfor", "emne_siste", "Slutt {navn"), ("dagfor", "finnes_ikke", "Hei")]


def _kryss_kurs(con, **kw):
    """Digitalt kurs som starter I MORGEN (ukefor 1-7 dager OG dagfor er begge aktuelle i dag), uten Zoom, med en bekreftet deltaker."""
    kid = _kurs(con, type="digital", start=IDAG + timedelta(days=1), **kw)
    _deltaker(con, kid)
    return kid


def _ukefor_mails(ut):
    return [m for m in ut["epost"] if m[1].startswith("Velkommen")]


def _dagfor_mails(ut):
    return [m for m in ut["epost"] if m[1].startswith("I morgen") or m[1].startswith("Siste")]


@pytest.mark.parametrize("mal,felt,tekst", UKEFOR_KORRUPT)
def test_kryss_a_korrupt_ukefor_og_gyldig_dagfor_zoom_opprettes_og_dagfor_sendes_med_lenke(con, ut, mal, felt, tekst):
    _gammelt_kurs_med_sensitivt(con)
    _korrupt(con, mal, felt, tekst)
    kid = _kryss_kurs(con)
    _daglig(con, IDAG)
    assert _ukefor_mails(ut) == [] and _claims(con, "ukefor") == 0                          # ukefor feiler lukket
    assert len(ut["zoom"]) == 1                                                             # Zoom opprettes noeyaktig en gang
    url = con.execute("SELECT zoom_url FROM kurs WHERE id=?", (kid,)).fetchone()[0]
    assert url and url.startswith("https://zoom.us/j/")
    (dagfor,) = _dagfor_mails(ut)
    assert f'<a href="{url}"' in _n(dagfor[2]) and "Bli med på Zoom" in _n(dagfor[2])       # dagfor har den nyopprettede lenken
    assert _claims(con, "dagfor") == 1
    assert con.execute("SELECT COUNT(*) FROM sensitivt").fetchone()[0] == 0                 # resten av daglig fortsatte
    h = _logg(con)
    assert [d["mal"] for d in h] == ["ukefor"]                                              # kun ukefor logget som feil


@pytest.mark.parametrize("mal,felt,tekst", DAGFOR_KORRUPT)
def test_kryss_b_gyldig_ukefor_og_korrupt_dagfor_dagfor_sendes_aldri_uten_lenke(con, ut, mal, felt, tekst):
    _gammelt_kurs_med_sensitivt(con)
    _korrupt(con, mal, felt, tekst)
    kid = _kryss_kurs(con)
    _daglig(con, IDAG)
    assert len(_ukefor_mails(ut)) == 1 and _claims(con, "ukefor") == 1                      # ukefor behandles etter normal semantikk
    assert _dagfor_mails(ut) == [] and _claims(con, "dagfor") == 0                          # ingen dagfor, ingen claim
    assert ut["zoom"] == []                                                                 # Zoom UTSATT (eneste som trenger lenken er korrupt)
    assert con.execute("SELECT zoom_url FROM kurs WHERE id=?", (kid,)).fetchone()[0] is None
    assert con.execute("SELECT COUNT(*) FROM sensitivt").fetchone()[0] == 0
    assert [d["mal"] for d in _logg(con)] == ["dagfor"]


def test_kryss_c_begge_maler_korrupte_ingen_utsending_ingen_claims_ingen_zoom_og_daglig_fortsetter(con, ut):
    _gammelt_kurs_med_sensitivt(con)
    _korrupt(con, "ukefor", "innledning", "Hei {ukjent_kode}")
    _korrupt(con, "dagfor", "innledning_midt", "{zoom_pw}")
    _kryss_kurs(con)
    _daglig(con, IDAG)
    assert _ukefor_mails(ut) == [] and _dagfor_mails(ut) == [] and ut["zoom"] == []
    assert _claims(con, "ukefor") == 0 and _claims(con, "dagfor") == 0
    assert con.execute("SELECT COUNT(*) FROM sensitivt").fetchone()[0] == 0
    assert sorted(d["mal"] for d in _logg(con)) == ["dagfor", "ukefor"]


@pytest.mark.parametrize("mal,felt,tekst", UKEFOR_KORRUPT)
def test_kryss_d_eksisterende_zoom_korrupt_ukefor_og_gyldig_dagfor_ingen_ny_zoom(con, ut, mal, felt, tekst):
    _korrupt(con, mal, felt, tekst)
    kid = _kurs(con, type="digital", start=IDAG + timedelta(days=1), zoom_url="https://zoom.example/j/77", zoom_id="777 888", zoom_pw="hemmelig1")
    _deltaker(con, kid)
    _daglig(con, IDAG)
    assert ut["zoom"] == []                                                                 # ingen ny opprettelse
    (dagfor,) = _dagfor_mails(ut)
    h = _n(dagfor[2])
    assert '<a href="https://zoom.example/j/77"' in h and "Møte-ID: 777 888 · Passord: hemmelig1" in h
    assert _ukefor_mails(ut) == [] and _claims(con, "ukefor") == 0 and _claims(con, "dagfor") == 1
    assert [d["mal"] for d in _logg(con)] == ["ukefor"]


def test_korrupt_ukefor_uten_dagfor_i_dag_utsetter_fortsatt_zoom_og_hybrid_oppfoerer_seg_likt(con, ut):
    """Bare ukefor er aktuell (3 dager for start): korrupt ukefor utsetter ny Zoom (dagens sikkerhetsregel er beholdt)."""
    _korrupt(con, "ukefor", "innledning", "Hei {ukjent_kode}")
    kid = _kurs(con, type="hybrid", start=IDAG + timedelta(days=3))
    _deltaker(con, kid)
    _daglig(con, IDAG)
    assert ut["zoom"] == [] and ut["epost"] == []


def test_etter_korrigert_mal_sendes_ukefor_og_zoom_opprettes_neste_kjoring(con, ut):
    _korrupt(con, "ukefor", "innledning", "Hei {ukjent_kode}")
    kid = _kurs(con, type="digital")
    _deltaker(con, kid)
    _daglig(con, IDAG)
    assert ut["epost"] == [] and ut["zoom"] == []
    con.execute("DELETE FROM mal_tekst")
    con.commit()
    _daglig(con, IDAG)                                                        # samme dag igjen (og ev. neste dag ville gitt samme resultat)
    _daglig(con, IDAG)
    assert len(ut["epost"]) == 1 and len(ut["zoom"]) == 1 and _claims(con, "ukefor") == 1


def test_kjent_begrensning_dagfor_med_korrupt_mal_paa_sendingsdagen_tas_ikke_igjen_dagen_etter(con, ut):
    """Tidsvinduet er BEVISST uendret (dagen for kursdagen). Rettes malen etter sendingsdagen, sendes dagfor ikke i ettertid."""
    _korrupt(con, "dagfor", "innledning_forste", "Hei {ukjent_kode}")
    kid = _kurs(con, start=date(2027, 3, 4))
    _deltaker(con, kid)
    _daglig(con, date(2027, 3, 3))
    assert not [m for m in ut["epost"] if m[1].startswith("I morgen")]
    con.execute("DELETE FROM mal_tekst")
    con.commit()
    _daglig(con, date(2027, 3, 4))                                            # kursdagen: utenfor vinduet
    assert not [m for m in ut["epost"] if m[1].startswith("I morgen")]
    _daglig(con, date(2027, 3, 3))                                            # men samme dag som feilen: kan tas igjen etter retting
    assert len([m for m in ut["epost"] if m[1].startswith("I morgen")]) == 1


def test_ukefor_med_korrupt_mal_kan_tas_igjen_dagen_etter_innenfor_det_eksisterende_vinduet(con, ut):
    _korrupt(con, "ukefor", "innledning", "Hei {ukjent_kode}")
    kid = _kurs(con, start=date(2027, 3, 8))
    _deltaker(con, kid)
    _daglig(con, date(2027, 3, 1))                                            # 7 dager for: ugyldig
    assert ut["epost"] == []
    con.execute("DELETE FROM mal_tekst")
    con.commit()
    _daglig(con, date(2027, 3, 2))                                            # 6 dager for: fortsatt innenfor 7-dagers vinduet
    assert len(ut["epost"]) == 1


# ================================== EN FEIL MOTTAKER STOPPER IKKE DE ANDRE ==================================

def test_en_mottaker_med_manglende_data_feiler_lukket_de_andre_faar_mail(con, ut, monkeypatch):
    kid = _kurs(con)
    for e in ("a@x.no", "b@x.no", "c@x.no"):
        _deltaker(con, kid, e)
    ekte = Kjoring.render_for_sending

    def rfs(self, mal, **data):
        if mal == "ukefor" and data["d"]["epost"] == "b@x.no":
            raise MalFeil(MANGLER_VERDI, "ukefor", None, "Data mangler")
        return ekte(self, mal, **data)
    monkeypatch.setattr(Kjoring, "render_for_sending", rfs)
    _daglig(con, IDAG)
    assert sorted(m[0] for m in ut["epost"]) == ["a@x.no", "c@x.no"]
    assert con.execute("SELECT COUNT(*) FROM utsending_logg WHERE mottaker='b@x.no'").fetchone()[0] == 0     # ingen claim
    (h,) = _logg(con)
    assert h["antall"] == 1 and h["grunn"] == MANGLER_VERDI and "@" not in json.dumps(h)


def test_malfeil_kan_aldri_escape_daglig_kjor_uansett_hvor_den_oppstaar(con, ut, monkeypatch):
    kid = _kurs(con, start=IDAG + timedelta(days=1))
    _deltaker(con, kid)
    _gammelt_kurs_med_sensitivt(con)

    def alltid(self, *a, **k):
        raise MalFeil("ukjent_kode", "ukefor", "innledning")
    monkeypatch.setattr(Kjoring, "send_en_gang", alltid)
    _daglig(con, IDAG)                                                        # returnerer normalt
    assert con.execute("SELECT COUNT(*) FROM sensitivt").fetchone()[0] == 0


def test_struktur_alle_utsendinger_i_innkallinger_er_beskyttet_av_except_malfeil():
    """Vakt: hvert kall til _send() i daglig.py ligger i en try med except maltekster.MalFeil (MalFeil kan ikke abortere jobben)."""
    tre = ast.parse((Path(daglig.__file__)).read_text(encoding="utf-8"))
    kall = [n for n in ast.walk(tre) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_send"]
    assert kall
    beskyttet = set()
    for tr in (n for n in ast.walk(tre) if isinstance(n, ast.Try)):
        fanger = any(isinstance(h.type, ast.Attribute) and h.type.attr == "MalFeil" for h in tr.handlers)
        if fanger:
            beskyttet |= {id(c) for stmt in tr.body for c in ast.walk(stmt)}
    assert all(id(c) in beskyttet for c in kall)


# ================================== CLAIM-REKKEFOLGE, DIREKTE RENDER, TIDSVINDU ==================================

def test_rekkefolge_zoom_og_render_foer_claim_foer_send(con, ut, monkeypatch):
    rekkefolge = []
    ekte_z, ekte_res = zoom.opprett_mote, db.reserver_sending
    monkeypatch.setattr(zoom, "opprett_mote", lambda n: (rekkefolge.append("zoom"), ekte_z(n))[1])
    monkeypatch.setattr(db, "reserver_sending", lambda *a, **k: (rekkefolge.append("claim"), ekte_res(*a, **k))[1])
    ekte_r = Kjoring.render_for_sending
    monkeypatch.setattr(Kjoring, "render_for_sending", lambda self, mal, **d: (rekkefolge.append("render:" + mal), ekte_r(self, mal, **d))[1])
    ekte_s = epost.send
    monkeypatch.setattr(epost, "send", lambda *a, **k: (rekkefolge.append("send"), ekte_s(*a, **k))[1])
    kid = _kurs(con, type="digital", start=IDAG + timedelta(days=1))
    _deltaker(con, kid)
    _daglig(con, IDAG)
    assert rekkefolge[0] == "render:ukefor"                                   # preflight (ren lesing) foer Zoom
    assert rekkefolge.index("zoom") < rekkefolge.index("claim") < rekkefolge.index("send")
    assert rekkefolge.count("zoom") == 1


@pytest.mark.parametrize("mal,data", [
    ("ukefor", dict(d={"navn": "Ola"}, kurs={"navn": "K", "type": "fysisk", "sted": "Oslo", "start_kl": "09:00", "slutt_kl": "16:00", "notat": None},
                    dager=[{"dato": "2027-03-04", "start_kl": None, "slutt_kl": None}])),
    ("dagfor", dict(d={"navn": "Ola"}, kurs={"navn": "K", "type": "fysisk", "sted": "Oslo", "start_kl": "09:00", "slutt_kl": "16:00", "zoom_url": None},
                    dag={"dato": "2027-03-04", "start_kl": None, "slutt_kl": None}, nr=1, antall=1)),
])
def test_direkte_render_uten_db_bruker_standardtekst(con, monkeypatch, mal, data):
    _korrupt(con, mal, "innledning" if mal == "ukefor" else "innledning_forste", "Hei {ukjent_kode}")     # ville feilet i en ekte utsending
    monkeypatch.setattr(db, "hent_overstyringer_for_mal", lambda *a, **k: (_ for _ in ()).throw(AssertionError("DB skal ikke leses")))
    emne, html = epost.render(mal, **data)
    assert "Hei Ola," in html and emne


def test_ukefor_og_dagfor_er_blant_de_aktive_malene(con):
    """Fullstendig liste over aktive/inaktive maler dekkes av tests/test_maltekster_integrasjon.py."""
    assert {"ukefor", "dagfor"} <= maltekster.AKTIVE_MALER
