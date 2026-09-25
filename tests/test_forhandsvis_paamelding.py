"""Fase 12B3B: aktivitetsliste (fjernet "Åpne", "Dupliser" som knapp) og forhåndsvisning av påmeldingsskjema.

Forhaandsvisningen gjenbruker noeyaktig samme mal (kurs.html), samme offentlige layout (base.html) og samme
rendringsvei som den ekte offentlige siden (kursside()), med forhandsvisning=True - ingen parallell skjemakopi
og ingen admin-layout. Skjemafeltene er INTERAKTIVE (fieldset er ikke disabled), slik at admin kan klikke rundt
og se dynamiske deler (f.eks. "Arbeidsgiver betaler") akkurat som en deltaker ville. "Meld meg på"-knappen ser
visuelt lik ut som paa den ekte siden, men er `type="button"` (ikke submit) i preview - ingen registrering kan
skje derfra. Skjemaet har i tillegg `onsubmit="return false"` som ekstra klientsperre, og selve preview-ruten
er GET-only bak admin-innlogging (POST gir 405).
"""
from datetime import date, timedelta

import pytest

from kurs import config, db


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
    html = _innlogget().get("/admin/aktiviteter").get_data(as_text=True)
    assert ">Åpne<" not in html


def test_b_kursnavnet_er_fortsatt_klikkbart_til_deltakerlisten(con):
    kid = _kurs(con)
    con.commit()
    html = _innlogget().get("/admin/aktiviteter").get_data(as_text=True)
    assert f'href="/admin/kurs/{kid}/deltakere"><strong>Eksempelkurs</strong></a>' in html


def test_c_dupliser_vises_som_knapp_med_eksisterende_knappestil(con):
    kid = _kurs(con)
    con.commit()
    html = _innlogget().get("/admin/aktiviteter").get_data(as_text=True)
    assert f'class="knapp liten sekundar" href="/admin/kurs/ny?fra={kid}"' in html


def test_redigeringshandlingen_er_beholdt_som_rediger_knapp_til_kursoppsett(con):
    kid = _kurs(con)
    con.commit()
    html = _innlogget().get("/admin/aktiviteter").get_data(as_text=True)
    assert f'class="knapp liten sekundar" href="/admin/kurs/{kid}/oppsett">Rediger</a>' in html
    assert "✎" not in html


def test_soek_filter_sortering_er_uendret(con):
    """Regresjon: selve tabellfunksjonaliteten (soek/filter/sortering/status/kapasitet) er urort."""
    _kurs(con, kode="A1", navn="Alfa")
    _kurs(con, kode="B1", navn="Beta")
    con.commit()
    k = _innlogget()
    html = k.get("/admin/aktiviteter?sok=Alfa").get_data(as_text=True)
    assert "Alfa" in html and "Kurs Beta" not in html   # soek fungerer
    html2 = k.get("/admin/aktiviteter?status=aapen").get_data(as_text=True)
    assert "Alfa" in html2 and "Beta" in html2


# ============================ D/E: preview-handlingen ============================

def test_d_forhandsvis_knapp_finnes_paa_nettsidefanen(con):
    kid = _kurs(con)
    con.commit()
    html = _innlogget().get(f"/admin/kurs/{kid}/nettside").get_data(as_text=True)
    assert f'href="/admin/kurs/{kid}/forhandsvis-paamelding"' in html
    assert "Forhåndsvis påmeldingsskjema" in html


def test_c_forhandsvis_knappen_aapner_i_ny_fane(con):
    kid = _kurs(con)
    con.commit()
    html = _innlogget().get(f"/admin/kurs/{kid}/nettside").get_data(as_text=True)
    assert (f'href="/admin/kurs/{kid}/forhandsvis-paamelding" target="_blank" rel="noopener">'
            "Forhåndsvis påmeldingsskjema</a>") in html


def test_e_preview_krever_admin_innlogging(con):
    kid = _kurs(con)
    con.commit()
    assert _klient().get(f"/admin/kurs/{kid}/forhandsvis-paamelding").status_code == 302


def test_a_preview_bruker_offentlig_layout_identisk_med_ekte_siden(con):
    """Preview og den ekte offentlige siden skal rendre med samme layout (base.html), ikke admin_base.html.
    Sammenligner header/nav-delen av dokumentet (foer <main>). Bruker en IKKE-innlogget klient for den
    offentlige siden, siden en deltaker aldri er admin-innlogget - da blir headeren byte-for-byte lik
    headeren en deltaker faktisk faar se i preview (der Admin-lenken ogsaa er skjult, se egen test)."""
    kid = _kurs(con)
    con.commit()
    forhandsvisning = _innlogget().get(f"/admin/kurs/{kid}/forhandsvis-paamelding").get_data(as_text=True)
    offentlig = _klient().get("/kurs/P1").get_data(as_text=True)
    import re as _re
    uten_nonce = lambda h: _re.sub(r'nonce="[^"]*"', 'nonce="<nonce>"', h.split("<main id=\"innhold\">")[0])   # noqa: E731
    header_fv, header_off = uten_nonce(forhandsvisning), uten_nonce(offentlig)
    assert header_fv == header_off
    assert "<style>" in header_fv   # samme felles CSS/stil som resten av det offentlige systemet


# ============================ F/G/H/I: innhold i forhaandsvisningen ============================

def test_f_preview_viser_riktig_kursnavn(con):
    kid = _kurs(con, navn="Veiledning i praksis")
    con.commit()
    html = _innlogget().get(f"/admin/kurs/{kid}/forhandsvis-paamelding").get_data(as_text=True)
    assert "<h1>Veiledning i praksis</h1>" in html


def test_g_preview_viser_dato_sted_og_pris(con):
    start = date(2027, 6, 1)
    kid = _kurs(con, type="fysisk", sted="IPR, Bergen", pris_nok=3900, start=start)
    con.commit()
    html = _innlogget().get(f"/admin/kurs/{kid}/forhandsvis-paamelding").get_data(as_text=True)
    assert "IPR, Bergen" in html and "2027-06-01" in html and "3 900 kr" in html


def test_h_preview_viser_registreringsfeltene(con):
    kid = _kurs(con)
    con.commit()
    html = _innlogget().get(f"/admin/kurs/{kid}/forhandsvis-paamelding").get_data(as_text=True)
    for felt in ('name="navn"', 'name="epost"', 'name="telefon"', 'name="arbeidssted"', 'name="samtykke"'):
        assert felt in html


def test_i_preview_viser_prisalternativer_naar_kurset_har_det(con):
    kid = _kurs(con, betaling="deltaker_velger")
    con.commit()
    html = _innlogget().get(f"/admin/kurs/{kid}/forhandsvis-paamelding").get_data(as_text=True)
    assert "Hele beløpet i én faktura" in html and "Delt opp" in html
    kid2 = _kurs(con, kode="P2", betaling="samlet")
    con.commit()
    html2 = _innlogget().get(f"/admin/kurs/{kid2}/forhandsvis-paamelding").get_data(as_text=True)
    assert "Delt opp" not in html2   # kurset har ikke det alternativet - vises ikke


def test_preview_viser_hpr_felt_kun_for_spesialistlop(con):
    kid = _kurs(con, spesialistlop="EFT")
    con.commit()
    html = _innlogget().get(f"/admin/kurs/{kid}/forhandsvis-paamelding").get_data(as_text=True)
    assert 'name="hpr_nr"' in html
    kid2 = _kurs(con, kode="P2")
    con.commit()
    html2 = _innlogget().get(f"/admin/kurs/{kid2}/forhandsvis-paamelding").get_data(as_text=True)
    assert 'name="hpr_nr"' not in html2


def test_preview_viser_plasser_igjen_men_ingen_deltaker_pii(con):
    kid = _kurs(con, kapasitet=5)
    db.meld_paa(con, kid, epost="hemmelig@x.no", navn="Hemmelig Deltaker")
    con.commit()
    html = _innlogget().get(f"/admin/kurs/{kid}/forhandsvis-paamelding").get_data(as_text=True)
    assert "4 plasser igjen" in html
    assert "Hemmelig Deltaker" not in html and "hemmelig@x.no" not in html


def test_preview_virker_uansett_kursstatus_ogsaa_utkast(con):
    kid = _kurs(con, status="utkast")
    con.commit()
    assert _innlogget().get(f"/admin/kurs/{kid}/forhandsvis-paamelding").status_code == 200


def test_preview_gir_404_for_ukjent_kurs(con):
    assert _innlogget().get("/admin/kurs/999999/forhandsvis-paamelding").status_code == 404


# ============================ Meld på-knapp / ingen innsending ============================

def test_meld_pa_knappen_vises_visuelt_men_er_ikke_submit_i_preview(con):
    kid = _kurs(con)
    con.commit()
    html = _innlogget().get(f"/admin/kurs/{kid}/forhandsvis-paamelding").get_data(as_text=True)
    assert '<button type="button">Meld meg på</button>' in html
    assert "Forhåndsvisning – påmelding er slått av" not in html   # ikke lenger knappeteksten


def test_offentlig_ekte_knapp_er_fortsatt_ekte_submit_ikke_type_button(con):
    kid = _kurs(con, pris_nok=0)
    con.commit()
    html = _klient().get("/kurs/P1").get_data(as_text=True)
    assert "<button>Meld meg på</button>" in html
    assert '<button type="button">Meld meg på</button>' not in html


def test_forhaandsvisningsbanner_vises_og_admin_navigasjon_vises_ikke(con):
    kid = _kurs(con)
    con.commit()
    html = _innlogget().get(f"/admin/kurs/{kid}/forhandsvis-paamelding").get_data(as_text=True)
    assert "Forhåndsvisning – dette er slik påmeldingssiden vil se ut." in html
    assert "Påmelding er deaktivert." in html
    # offentlig layout (base.html) - ikke admin_base.html sin adminmeny
    assert 'href="/admin/aktiviteter"' not in html
    assert 'href="/admin/rapporter"' not in html
    assert 'href="/admin/e-postmaler"' not in html
    assert 'href="/admin/brukere"' not in html
    assert 'href="/admin/logg-ut"' not in html
    assert ">Utboks<" not in html
    assert ">Kunnskapsbase<" not in html
    assert ">Daglig kjøring<" not in html
    # men den vanlige offentlige navigasjonen (fra base.html) er der, akkurat som paa den ekte siden
    assert '>Kurs</a>' in html
    assert '>Spør oss</a>' in html


def test_admin_lenken_skjules_kun_i_preview_ikke_paa_ordinaer_offentlig_side(con):
    """En deltaker skal aldri se Admin-lenken. Preview skal derfor vise siden noeyaktig slik en deltaker
    ser den - ogsaa naar admin er innlogget mens de forhaandsviser. Paa den ordinaere offentlige siden skal
    Admin-lenken derimot beholdes uendret naar en admin er innlogget (eksisterende oppforsel, urort)."""
    kid = _kurs(con)
    con.commit()
    k = _innlogget()
    forhandsvisning = k.get(f"/admin/kurs/{kid}/forhandsvis-paamelding").get_data(as_text=True)
    offentlig_for_admin = k.get("/kurs/P1").get_data(as_text=True)
    offentlig_for_besoekende = _klient().get("/kurs/P1").get_data(as_text=True)
    assert ">Admin</strong></a>" not in forhandsvisning
    assert ">Admin</strong></a>" in offentlig_for_admin       # uendret - innlogget admin ser fortsatt lenken
    assert ">Admin</strong></a>" not in offentlig_for_besoekende  # uendret - ikke innlogget, ingen lenke


def test_bedriftspaamelding_lenken_vises_ikke_i_preview(con):
    kid = _kurs(con)
    con.commit()
    html = _innlogget().get(f"/admin/kurs/{kid}/forhandsvis-paamelding").get_data(as_text=True)
    assert "Bruk bedriftspåmelding" not in html


def test_skjemafeltene_er_interaktive_ikke_globalt_disabled(con):
    kid = _kurs(con)
    con.commit()
    html = _innlogget().get(f"/admin/kurs/{kid}/forhandsvis-paamelding").get_data(as_text=True)
    assert "<fieldset>" in html
    assert "<fieldset disabled>" not in html
    assert "disabled" not in html.split("<fieldset>", 1)[1].split("</fieldset>", 1)[0]


def test_arbeidsgiver_betaler_finnes_og_er_koblet_til_samme_js_som_offentlig_side(con):
    kid = _kurs(con)
    con.commit()
    forhandsvisning = _innlogget().get(f"/admin/kurs/{kid}/forhandsvis-paamelding").get_data(as_text=True)
    offentlig = _klient().get("/kurs/P1").get_data(as_text=True)
    assert 'name="betaler" value="organisasjon"' in forhandsvisning
    assert 'data-vis="org"' in forhandsvisning
    assert 'id="org"' in forhandsvisning
    # samme radioknapp-markup (inkl. onchange-logikken) som paa den offentlige siden - ikke en forenklet kopi
    def _betaler_blokk(html):
        start = html.index('name="betaler" value="organisasjon"')
        return html[start - 40:start + 120]
    assert _betaler_blokk(forhandsvisning) == _betaler_blokk(offentlig)


def test_p_post_mot_preview_url_gir_405_ikke_paamelding(con):
    kid = _kurs(con)
    con.commit()
    k = _innlogget()
    r = k.post(f"/admin/kurs/{kid}/forhandsvis-paamelding",
              data={"navn": "Ola Nordmann", "epost": "ola@x.no", "samtykke": "on"})
    assert r.status_code == 405
    assert _antall(con, "paamelding") == 0


# ============================ J-O: ingen sideeffekter av preview ============================

def test_jklmno_preview_lager_ingen_sideeffekt_i_det_hele_tatt(con, monkeypatch):
    from kurs.integrasjoner import epost, visma
    epost_kalt, visma_kalt = [], []
    monkeypatch.setattr(epost, "send", lambda *a, **kw: epost_kalt.append(1))
    monkeypatch.setattr(visma, "fakturer", lambda *a, **kw: visma_kalt.append(1))
    kid = _kurs(con, betaling="deltaker_velger", spesialistlop="EFT")
    kid_gruppe = _kurs(con, kode="P2")
    con.commit()
    k = _innlogget()                              # innlogging logges - telles foer forhaandsvisningene
    hendelser_for = _antall(con, "hendelse")

    k.get(f"/admin/kurs/{kid}/forhandsvis-paamelding")
    k.get(f"/admin/kurs/{kid_gruppe}/forhandsvis-paamelding")

    assert _antall(con, "paamelding") == 0        # J
    assert _antall(con, "deltaker") == 0          # K
    assert _antall(con, "firmapaamelding") == 0   # L
    assert _antall(con, "firmapaamelding_rad") == 0
    assert _antall(con, "utsending_logg") == 0    # M
    assert epost_kalt == []                       # N
    assert visma_kalt == []                       # O
    assert _antall(con, "faktura") == 0 and _antall(con, "faktura_forsok") == 0
    assert _antall(con, "sensitivt") == 0
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
    r2 = k.post("/kurs/P1", data={"navn": "Ola Nordmann", "epost": "ola@x.no", "samtykke": "on"}, follow_redirects=True)
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
