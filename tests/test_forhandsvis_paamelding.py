"""Fase 12B3B: aktivitetsliste (fjernet "Åpne", "Dupliser" som knapp) og forhåndsvisning av påmeldingsskjema.

Skjemabyggeren: forhåndsvisningen ER den ekte offentlige siden (/kurs/<kode>), slik at adressen i nettleseren er den som
kan sendes til deltakerne og legges på ipr.no. Den gamle forhåndsvisningsadressen (GET, bak admin-innlogging) sender
videre dit, og POST mot den gir fortsatt 405. For et kurs som ikke er offentlig (utkast, avsluttet, avlyst) ser bare en
innlogget administrator siden: med en tydelig merknad, «Meld meg på» som `type="button"`, skjemaet merket
data-ingen-innsending og uten Admin-lenke og bedriftspåmelding - og POST er 404 for alle. Skjemafeltene er interaktive;
bare felt i deler av skjemaet som er skjult (f.eks. «Firma betaler» før det er valgt), er deaktivert - akkurat som på
den ekte siden.
"""
import re
from datetime import date, timedelta
from html.parser import HTMLParser

import pytest

from adressehjelp import ADRESSE
from kurs import config, db


# HPR-nummer er slått av i skjemaene (skjemafelt.HPR_I_SKJEMA = False, Camilla 02.10.2026). Disse testene gjelder koden som viser, validerer og lagrer det
# (den finnes fortsatt og kan slås på igjen), så de slår det på. Standarden (av) testes i test_hpr_nummer_samles_ikke_inn.py.
pytestmark = pytest.mark.usefixtures("hpr_i_skjema")


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _klient():
    from kurs.web import app as webapp
    return webapp.app.test_client()


def _innlogget():
    k = _klient()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def _kurs(con, kode="P1", start=None, **kw):
    start = start or (date.today() + timedelta(days=30))
    felter = {"navn": "Eksempelkurs", "pris_nok": 4500, "fakturering": "person", "kapasitet": 10, **kw}
    return db.opprett_kurs(con, kode=kode, datoer=[start.isoformat()], sharepoint_mappe=f"K/{kode}", **felter)


def _antall(con, tabell):
    return con.execute(f"SELECT COUNT(*) FROM {tabell}").fetchone()[0]


# ============================ A/B/C: aktivitetslisten ============================

def test_a_aapne_er_fjernet_fra_aktivitetslisten(con):
    _kurs(con)
    con.commit()
    html = _innlogget().get("/admin").get_data(as_text=True)
    assert ">Åpne<" not in html


def test_b_kursnavnet_er_fortsatt_klikkbart_til_deltakerlisten(con):
    kid = _kurs(con)
    con.commit()
    html = _innlogget().get("/admin").get_data(as_text=True)
    assert f'href="/admin/kurs/{kid}/deltakere"><strong>Eksempelkurs</strong></a>' in html


def test_c_dupliser_vises_som_knapp_med_eksisterende_knappestil(con):
    kid = _kurs(con)
    con.commit()
    html = _innlogget().get("/admin").get_data(as_text=True)
    assert f'class="ikonknapp" href="/admin/kurs/ny?fra={kid}"' in html                       # ikon (to ark) siden 04.10.2026


def test_redigeringshandlingen_er_beholdt_som_rediger_knapp_til_kursoppsett(con):
    kid = _kurs(con)
    con.commit()
    html = _innlogget().get("/admin").get_data(as_text=True)
    assert f'class="ikonknapp" href="/admin/kurs/{kid}/oppsett" title="Rediger kurset"' in html   # penn-ikon siden 04.10.2026
    assert "✎" not in html


def test_soek_filter_sortering_er_uendret(con):
    """Regresjon: selve tabellfunksjonaliteten (soek/filter/sortering/status/kapasitet) er urort."""
    _kurs(con, kode="A1", navn="Alfa")
    _kurs(con, kode="B1", navn="Beta")
    con.commit()
    k = _innlogget()
    html = k.get("/admin?sok=Alfa").get_data(as_text=True)
    assert "Alfa" in html and "Kurs Beta" not in html   # soek fungerer
    html2 = k.get("/admin?status=aapen").get_data(as_text=True)
    assert "Alfa" in html2 and "Beta" in html2


# ============================ D/E: preview-handlingen ============================

ADMIN_LENKE = '<a href="/admin"><strong>Admin</strong></a>'


def _forhandsvis(kid, klient=None):
    return (klient or _innlogget()).get(f"/admin/kurs/{kid}/forhandsvis-paamelding", follow_redirects=True)


def test_d_forhandsvis_lenker_til_den_ekte_siden_fra_begge_fanene(con):
    kid = _kurs(con)
    con.commit()
    k = _innlogget()
    nettside = k.get(f"/admin/kurs/{kid}/nettside").get_data(as_text=True)
    skjema = k.get(f"/admin/kurs/{kid}/paameldingsskjema").get_data(as_text=True)
    assert 'href="/kurs/P1" target="_blank" rel="noopener">Åpne påmeldingssiden ↗</a>' in nettside
    assert 'href="/kurs/P1" target="_blank" rel="noopener">Forhåndsvis påmeldingsskjema ↗</a>' in skjema


def test_c_den_gamle_forhandsvisningsadressen_sender_til_den_ekte_siden(con):
    kid = _kurs(con)
    con.commit()
    r = _innlogget().get(f"/admin/kurs/{kid}/forhandsvis-paamelding")
    assert r.status_code == 302 and r.headers["Location"].endswith("/kurs/P1")


def test_e_preview_krever_admin_innlogging(con):
    kid = _kurs(con)
    con.commit()
    assert _klient().get(f"/admin/kurs/{kid}/forhandsvis-paamelding").status_code == 302


def test_a_forhandsvisningen_er_den_ekte_offentlige_siden(con):
    """Samme adresse og samme offentlige layout (base.html) som deltakerne får. Eneste forskjell i toppen er
    Admin-lenken, som en innlogget administrator alltid ser på de offentlige sidene (eksisterende oppførsel)."""
    kid = _kurs(con)
    con.commit()
    r = _forhandsvis(kid)
    assert r.status_code == 200 and r.request.path == "/kurs/P1"
    offentlig = _klient().get("/kurs/P1").get_data(as_text=True)
    uten_nonce = lambda h: re.sub(r'nonce="[^"]*"', 'nonce="<nonce>"', h.split("<main id=\"innhold\">")[0])   # noqa: E731
    assert uten_nonce(r.get_data(as_text=True)).replace(ADMIN_LENKE, "") == uten_nonce(offentlig)
    assert "<style>" in uten_nonce(offentlig)   # samme felles CSS/stil som resten av det offentlige systemet


def test_a2_utkast_forhandsvises_med_samme_layout_uten_admin_lenke(con):
    kid = _kurs(con, status="utkast")
    _kurs(con, kode="P2")                          # samme kurs, offentlig
    con.commit()
    utkast = _forhandsvis(kid).get_data(as_text=True)
    offentlig = _klient().get("/kurs/P2").get_data(as_text=True)
    topp = lambda h: re.sub(r'nonce="[^"]*"', "", h.split("<main id=\"innhold\">")[0])   # noqa: E731
    assert topp(utkast) == topp(offentlig) and ADMIN_LENKE not in utkast


# ============================ F/G/H/I: innhold i forhaandsvisningen ============================

def test_f_preview_viser_riktig_kursnavn(con):
    kid = _kurs(con, navn="Veiledning i praksis")
    con.commit()
    assert "<h1>Veiledning i praksis</h1>" in _forhandsvis(kid).get_data(as_text=True)


def test_g_preview_viser_dato_sted_og_pris(con):
    start = date(2027, 6, 1)
    kid = _kurs(con, type="fysisk", sted="IPR, Bergen", pris_nok=3900, start=start)
    con.commit()
    html = _forhandsvis(kid).get_data(as_text=True)
    assert "IPR, Bergen" in html and "01.06.2027" in html and "3 900 kr" in html


def test_h_preview_viser_registreringsfeltene(con):
    kid = _kurs(con)
    con.commit()
    html = _forhandsvis(kid).get_data(as_text=True)
    for felt in ('name="fornavn"', 'name="etternavn"', 'name="epost"', 'name="telefon"', 'name="arbeidssted"',
                 'name="samtykke"'):
        assert felt in html


def test_i_preview_viser_prisalternativer_naar_kurset_har_det(con):
    start = date.today() + timedelta(days=30)
    kid = db.opprett_kurs(con, kode="P1", navn="Eksempelkurs", datoer=[start.isoformat(), (start + timedelta(days=30)).isoformat()],
                          sharepoint_mappe="K/P1", pris_nok=4500, fakturering="person", kapasitet=10,
                          betaling="deltaker_velger")                                   # to samlinger
    con.commit()
    html = _forhandsvis(kid).get_data(as_text=True)
    assert "Hele beløpet i én faktura" in html and "Delt opp" in html and "kr × 2)" in html
    en = _kurs(con, kode="P3", betaling="deltaker_velger")                             # bare én samling
    con.commit()
    html_en = _forhandsvis(en).get_data(as_text=True)
    assert "Hele beløpet i én faktura" not in html_en and "Delt opp" not in html_en      # ikke noe meningsløst valg
    assert 'name="betaling"' not in html_en
    kid2 = _kurs(con, kode="P2", betaling="samlet")
    con.commit()
    assert "Delt opp" not in _forhandsvis(kid2).get_data(as_text=True)   # kurset har ikke det alternativet


def test_preview_viser_hpr_felt_kun_for_spesialistlop(con):
    kid = _kurs(con, spesialistlop="EFT")
    con.commit()
    assert 'name="hpr_nr"' in _forhandsvis(kid).get_data(as_text=True)
    kid2 = _kurs(con, kode="P2")
    con.commit()
    assert 'name="hpr_nr"' not in _forhandsvis(kid2).get_data(as_text=True)


def test_preview_viser_ikke_plasser_igjen_og_ingen_deltaker_pii(con):
    kid = _kurs(con, kapasitet=5)
    db.meld_paa(con, kid, epost="hemmelig@x.no", fornavn="Hemmelig", etternavn="Deltaker")
    con.commit()
    html = _forhandsvis(kid).get_data(as_text=True)
    assert "plasser igjen" not in html and "Ledige plasser" not in html
    assert "Hemmelig Deltaker" not in html and "hemmelig@x.no" not in html


@pytest.mark.parametrize("status", ["utkast", "avsluttet", "avlyst"])
def test_preview_virker_uansett_kursstatus(con, status):
    kid = _kurs(con, status=status)
    con.commit()
    assert _forhandsvis(kid).status_code == 200
    assert _klient().get("/kurs/P1").status_code == 404                 # men bare for innlogget admin


def test_preview_gir_404_for_ukjent_kurs(con):
    assert _innlogget().get("/admin/kurs/999999/forhandsvis-paamelding").status_code == 404


# ============================ Meld på-knapp / ingen innsending ============================

def test_meld_pa_knappen_er_ikke_submit_naar_kurset_ikke_er_offentlig(con):
    kid = _kurs(con, status="utkast")
    kid2 = _kurs(con, kode="P2")
    con.commit()
    html = _forhandsvis(kid).get_data(as_text=True)
    assert '<button type="button">Meld meg på</button>' in html and "data-ingen-innsending" in html
    offentlig_for_admin = _forhandsvis(kid2).get_data(as_text=True)     # offentlig kurs: den ekte knappen
    assert "<button>Meld meg på</button>" in offentlig_for_admin and "data-ingen-innsending" not in offentlig_for_admin


def test_offentlig_ekte_knapp_er_fortsatt_ekte_submit_ikke_type_button(con):
    kid = _kurs(con, pris_nok=0)
    con.commit()
    html = _klient().get("/kurs/P1").get_data(as_text=True)
    assert "<button>Meld meg på</button>" in html
    assert '<button type="button">Meld meg på</button>' not in html


def test_forhaandsvisningsbanner_vises_og_admin_navigasjon_vises_ikke(con):
    kid = _kurs(con, status="utkast")
    con.commit()
    html = _forhandsvis(kid).get_data(as_text=True)
    assert "Forhåndsvisning – kurset er ikke åpent for påmelding (utkast)." in html
    assert "Bare innloggede administratorer ser denne siden, og skjemaet kan ikke sendes." in html
    # offentlig layout (base.html) - ikke admin_base.html sin adminmeny
    assert 'href="/admin"' not in html
    assert 'href="/admin/rapporter"' not in html
    assert 'href="/admin/e-postmaler"' not in html
    assert 'href="/admin/brukere"' not in html
    assert 'href="/admin/logg-ut"' not in html
    assert ">Utboks<" not in html
    assert ">Kunnskapsbase<" not in html
    assert ">Daglig kjøring<" not in html
    assert "data-deltakersok" not in html and 'name="q"' not in html       # deltakersøket hører til adminsidene
    # men den vanlige offentlige navigasjonen (fra base.html) er der, akkurat som paa den ekte siden. «Spør oss» er av som standard,
    # og uinnlogget står «Logg inn» (ikke «Min side») i kontogruppen til høyre. «Kurs» og «Innsjekk» er tatt ut av menyen.
    assert '>Kurs</a>' not in html and '>Innsjekk</a>' not in html
    assert '>Spør oss</a>' not in html
    assert '>Logg inn</a>' in html and '>Min side</a>' not in html


def test_admin_lenken_skjules_bare_naar_kurset_ikke_er_offentlig(con):
    """Deltakere ser aldri Admin-lenken. På et kurs som ikke er offentlig, vises siden slik en deltaker vil se den -
    uten lenken. På en offentlig side beholdes lenken for innlogget admin (eksisterende oppførsel, urørt)."""
    kid = _kurs(con, status="utkast")
    _kurs(con, kode="P2")
    con.commit()
    k = _innlogget()
    assert ADMIN_LENKE not in _forhandsvis(kid, k).get_data(as_text=True)
    assert ADMIN_LENKE in k.get("/kurs/P2").get_data(as_text=True)
    assert ADMIN_LENKE not in _klient().get("/kurs/P2").get_data(as_text=True)


def test_bedriftspaamelding_lenken_vises_ikke_naar_kurset_ikke_er_offentlig(con):
    kid = _kurs(con, status="utkast")
    kid2 = _kurs(con, kode="P2")
    con.commit()
    assert "Bruk bedriftspåmelding" not in _forhandsvis(kid).get_data(as_text=True)
    assert "Bruk bedriftspåmelding" in _forhandsvis(kid2).get_data(as_text=True)


class _Deaktivert(HTMLParser):
    """Finner deaktiverte skjemafelt som IKKE står i et skjult element (hidden)."""

    TOMME = {"input", "br", "img", "meta", "link", "hr"}

    def __init__(self):
        super().__init__()
        self.stakk, self.synlige_deaktiverte = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        skjult = "hidden" in a or (self.stakk and self.stakk[-1])
        if tag in ("input", "select", "textarea") and "disabled" in a and not skjult and a.get("type") != "hidden":
            self.synlige_deaktiverte.append(a.get("name"))
        if tag not in self.TOMME:
            self.stakk.append(skjult)

    def handle_endtag(self, tag):
        if tag not in self.TOMME and self.stakk:
            self.stakk.pop()


def test_skjemafeltene_er_interaktive_ikke_globalt_disabled(con):
    start = date.today() + timedelta(days=30)
    kid = db.opprett_kurs(con, kode="P1", navn="Eksempelkurs", datoer=[start.isoformat(), (start + timedelta(days=30)).isoformat()],
                          sharepoint_mappe="K/P1", pris_nok=4500, fakturering="person", kapasitet=10, status="utkast",
                          spesialistlop="EFT", betaling="deltaker_velger")   # to samlinger, så betalingsvalget vises
    con.commit()
    html = _forhandsvis(kid).get_data(as_text=True)
    skjema = html.split('<form method="post" class="kort pm-skjema"', 1)[1].split("</form>", 1)[0]
    p = _Deaktivert()
    p.feed(skjema)
    assert p.synlige_deaktiverte == []                        # bare felt i skjulte deler er deaktivert
    for navn in ("fornavn", "etternavn", "epost", "telefon", "arbeidssted", "hpr_nr", "betaler", "betaling", "samtykke"):
        tag = re.search(rf'<input[^>]*name="{navn}"[^>]*>', skjema).group(0)
        assert "disabled" not in tag, navn


def test_arbeidsgiver_betaler_finnes_og_er_koblet_til_samme_js_som_offentlig_side(con):
    kid = _kurs(con, status="utkast")
    _kurs(con, kode="P2")
    con.commit()
    forhandsvisning = _forhandsvis(kid).get_data(as_text=True)
    offentlig = _klient().get("/kurs/P2").get_data(as_text=True)
    for html in (forhandsvisning, offentlig):
        assert 'name="betaler" value="organisasjon"' in html
        assert 'data-vis-naar="betaler" data-vis-verdi="organisasjon" hidden' in html     # firmadelen, styrt av app.js
        # betaler deltakeren selv, er fakturaadressen hans egen adresse (øverst i skjemaet): ingen egen del for «privat»
        assert 'data-vis-verdi="person"' not in html
        assert "sendes fakturaen til adressen du har oppgitt over" in html
    # samme radioknapp-markup som paa den offentlige siden - ikke en forenklet kopi
    def _betaler_blokk(html):
        start = html.index('name="betaler" value="organisasjon"')
        return html[start - 200:start + 120]
    assert _betaler_blokk(forhandsvisning) == _betaler_blokk(offentlig)


def test_p_post_mot_preview_url_gir_405_ikke_paamelding(con):
    kid = _kurs(con)
    con.commit()
    k = _innlogget()
    r = k.post(f"/admin/kurs/{kid}/forhandsvis-paamelding",
              data={"fornavn": "Ola", "etternavn": "Nordmann", "epost": "ola@x.no", "samtykke": "on", **ADRESSE})
    assert r.status_code == 405
    assert _antall(con, "paamelding") == 0


def test_p2_post_mot_et_kurs_som_ikke_er_offentlig_gir_404_ogsaa_for_admin(con):
    _kurs(con, status="utkast")
    con.commit()
    for k in (_innlogget(), _klient()):
        r = k.post("/kurs/P1", data={"fornavn": "Ola", "etternavn": "Nordmann", "epost": "ola@x.no", "samtykke": "on", **ADRESSE})
        assert r.status_code == 404
    assert _antall(con, "paamelding") == 0


# ============================ J-O: ingen sideeffekter av preview ============================

def test_jklmno_preview_lager_ingen_sideeffekt_i_det_hele_tatt(con, monkeypatch):
    from kurs.integrasjoner import epost, visma
    epost_kalt, visma_kalt = [], []
    monkeypatch.setattr(epost, "send", lambda *a, **kw: epost_kalt.append(1))
    monkeypatch.setattr(visma, "fakturer", lambda *a, **kw: visma_kalt.append(1))
    kid = _kurs(con, betaling="deltaker_velger", spesialistlop="EFT")
    kid_gruppe = _kurs(con, kode="P2", status="utkast")
    con.commit()
    k = _innlogget()                              # innlogging logges - telles foer forhaandsvisningene
    hendelser_for = _antall(con, "hendelse")

    _forhandsvis(kid, k)
    _forhandsvis(kid_gruppe, k)

    assert _antall(con, "paamelding") == 0        # J
    assert _antall(con, "deltaker") == 0          # K
    assert _antall(con, "firmapaamelding") == 0   # L
    assert _antall(con, "firmapaamelding_rad") == 0
    assert _antall(con, "utsending_logg") == 0    # M
    assert epost_kalt == []                       # N
    assert visma_kalt == []                       # O
    assert _antall(con, "faktura") == 0 and _antall(con, "faktura_forsok") == 0
    assert _antall(con, "sensitivt") == 0 and _antall(con, "paamelding_svar") == 0
    assert _antall(con, "hendelse") == hendelser_for   # ingen hendelse later som noe skjedde


# ============================ Q/R: eksisterende funksjonalitet uendret ============================

def test_q_offentlig_ordinaer_paamelding_fungerer_fortsatt_uendret(con, monkeypatch):
    from kurs.integrasjoner import epost
    monkeypatch.setattr(epost, "send", lambda *a, **kw: None)
    kid = _kurs(con, pris_nok=0)
    con.commit()
    k = _klient()
    r = k.get("/kurs/P1")
    assert r.status_code == 200
    h = r.get_data(as_text=True)
    assert "<button>Meld meg på</button>" in h
    assert "Forhåndsvisning – dette er slik påmeldingssiden vil se ut." not in h
    assert "fieldset disabled" not in h
    r2 = k.post("/kurs/P1", data={"fornavn": "Ola", "etternavn": "Nordmann", "epost": "ola@x.no", "samtykke": "on", **ADRESSE}, follow_redirects=True)
    assert r2.status_code == 200
    assert _antall(con, "paamelding") == 1
    assert con.execute("SELECT navn FROM deltaker").fetchone()[0] == "Ola Nordmann"


def test_r_kursduplisering_fungerer_fortsatt_uendret(con):
    kid = _kurs(con, kode="EFT-S1", betaling="per_samling", spesialistlop="EFT")
    con.commit()
    k = _innlogget()
    r = k.get(f"/admin/kurs/ny?fra={kid}")
    assert r.status_code == 200
    h = r.get_data(as_text=True)
    assert 'value="Eksempelkurs"' in h
    r2 = k.post("/admin/kurs/ny", data={
        "navn": "Eksempelkurs", "type": "fysisk", "sted": "IPR, Bergen", "kapasitet": "10",
        "datoer": (date.today() + timedelta(days=400)).isoformat(), "start_kl": "09:00", "slutt_kl": "16:00",
        "timer_pr_dag": "6", "pris_nok": "4500", "fakturering": "person", "betaling": "per_samling",
        "faktura_dager_for": "14", "spesialistlop": "EFT", "ansvarlig_admin_id": "", "paameldingsfrist": "",
        "kursholder_navn": "", "kursholder_epost": "", "materiell_frist": "", "notat": "", "fra": str(kid)})
    assert r2.status_code == 302
    assert _antall(con, "kurs") == 2
