"""Fase 12B2C-4: `firmapaamelding_kvittering` migrert til maltekstsystemet - ALENE (`purring` er urort).

Kritisk regel: en ugyldig `firmapaamelding_kvittering`-mal skal ALDRI foere til at en bedriftspaamelding blir
delvis registrert (firmapaamelding-rad, deltaker-paameldinger og deltakerbekreftelser er alle uigjenkallelige
naar de foerst er committet/sendt). Derfor gjoeres en preflight av KUN det admin-redigerbare innholdet (emne/
innledning/avslutning - de eneste feltene som har {koder}) FOER noen registrering, med data som uansett er
kjent for hele innsendingen. De faktiske utfallstallene (bekreftet/venteliste/feilet) er ALDRI koder - de er en
laast systemblokk i malfilen, bygget fra det virkelige registreringsresultatet, og krever derfor intet nytt
DB-oppslag: den avsluttende sendingen gjenbruker det allerede rendrede maltekst-resultatet fra preflighten
gjennom epost.render(maltekst=...) (ren, lokal Jinja-rendring - ingen DB) + send_ferdigrendret_en_gang().
"""
import json
from datetime import date, timedelta

import pytest

from kurs import config, db, maltekster
from kurs.integrasjoner import epost
from kurs.kjoring import Kjoring
from kurs.maltekster import DB_LESEFEIL, MALER, TOM, UKJENT_FELT, UKJENT_KODE, MalFeil
from navnehjelp import gruppeskjema, navnedeler

STD_EMNE = "Bedriftspåmelding til Testkurs – kvittering"


def _n(html: str) -> str:
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


def _klient():
    from kurs.web import app as webapp
    return webapp.app.test_client()


def _kurs(con, kode="T1", start=None, **kw):
    start = start or (date.today() + timedelta(days=30))
    return db.opprett_kurs(con, kode=kode, datoer=[start.isoformat()], sharepoint_mappe=f"Kurs/{kode}",
                           **{"navn": "Testkurs", "pris_nok": 1000, **kw})


def _fersk(con):
    return db.koble(config.DB_STI)


def _grunnlag(**over):
    data = {
        "kontakt_navn": "Kari HR", "kontakt_epost": "kari.hr@firma.no", "kontakt_telefon": "90000000",
        "firmanavn": "Firma AS", "org_nr": "999900003", "faktura_ref": "BEST-1",       # firmanavnet fra registeret brukes
        "deltaker_navn": ["Ola Nordmann"], "deltaker_epost": ["ola@firma.no"],
        "deltaker_telefon": [""], "deltaker_arbeidssted": [""], "samtykke": "on",
    }
    data.update(over)
    return gruppeskjema(data)       # fullt navn i testdataene -> skjemaets egne fornavn-/etternavn-felt


def _post(klient, kode, follow_redirects=True, **over):
    return klient.post(f"/kurs/{kode}/gruppe", data=_grunnlag(**over), follow_redirects=follow_redirects)


def _lagre(con, felt, tekst):
    maltekster.lagre_maltekst(con, "firmapaamelding_kvittering", felt, tekst, aktor="admin:test")
    con.commit()


def _epost_utboks(config_):
    ut = []
    for f in sorted(config_.UTBOKS.glob("*.html")) if config_.UTBOKS.exists() else []:
        forste, _, html = f.read_text(encoding="utf-8").partition("\n")
        meta = json.loads(forste.removeprefix("<!--META ").removesuffix(" -->"))
        ut.append((meta["til"], meta["emne"], html))
    return ut


def _kvittering(config_, til="kari.hr@firma.no"):
    return [m for m in _epost_utboks(config_) if m[0] == til]


def _direkte_data(navn="Kari HR", kursnavn="Testkurs", firmanavn="Firma AS", antall_totalt=1,
                  antall_bekreftet=1, antall_venteliste=0, antall_feilet=0, kvittering_url="/x"):
    return dict(kontakt={"navn": navn, "fornavn": navnedeler(navn)["fornavn"], "firmanavn": firmanavn},
                kurs={"navn": kursnavn, "kursnr": 1001, "fakturering": "person",
                "pris_nok": 1000}, antall_totalt=antall_totalt, antall_bekreftet=antall_bekreftet,
                antall_venteliste=antall_venteliste, antall_feilet=antall_feilet, kvittering_url=kvittering_url)


# ============================ aktivering ============================

def test_firmapaamelding_kvittering_er_aktivert():
    assert "firmapaamelding_kvittering" in maltekster.AKTIVE_MALER
    assert maltekster._VERDIER["firmapaamelding_kvittering"] is maltekster._firmapaamelding_kvittering_verdier


def test_verdibygger_eksponerer_kun_navn_kursnavn_firmanavn_og_antall_deltakere():
    verdier = maltekster._firmapaamelding_kvittering_verdier(
        {"kontakt": {"navn": "Kari HR", "fornavn": "Kari", "firmanavn": "Firma AS", "epost": "kari@x.no"},
         "kurs": {"navn": "K", "pris_nok": 1500}, "antall_totalt": 3})
    assert verdier == {"fornavn": "Kari", "navn": "Kari HR", "kursnavn": "K", "firmanavn": "Firma AS",
                       "antall_deltakere": "3 deltakere"}
    tillatt = set().union(*(f.kode for f in MALER["firmapaamelding_kvittering"].felt.values())) - maltekster.SYSTEMKODER
    assert tillatt == set(verdier)


@pytest.mark.parametrize("antall,tekst", [(0, "0 deltakere"), (1, "1 deltaker"), (2, "2 deltakere")])
def test_antall_deltakere_bruker_norsk_entall_flertall(antall, tekst):
    verdier = maltekster._firmapaamelding_kvittering_verdier(
        {"kontakt": {"navn": "K", "fornavn": "K", "firmanavn": "F"}, "kurs": {"navn": "K"}, "antall_totalt": antall})
    assert verdier["antall_deltakere"] == tekst


def test_de_faktiske_utfallstallene_er_ikke_koder_kan_ikke_overstyres(con):
    with pytest.raises(MalFeil) as e:
        maltekster.lagre_maltekst(con, "firmapaamelding_kvittering", "innledning", "{antall_bekreftet} fikk plass")
    assert e.value.grunn == UKJENT_KODE


# ============================ A-D: standard og override gjennom ekte flyt ============================

def test_A_uten_override_er_identisk_med_dagens_standardtekst(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    klient = _klient()
    _post(klient, "T1")
    (til, emne, html), = _kvittering(config)
    assert emne == STD_EMNE
    token = _fersk(con).execute("SELECT kvittering_token FROM firmapaamelding").fetchone()[0]
    url = f"/kurs/T1/gruppe/kvittering/{token}"
    assert _n(html) == _n(epost.render("firmapaamelding_kvittering", **_direkte_data(kvittering_url=url, firmanavn="EKSEMPEL KOMMUNE"))[1])
    h = _n(html)
    assert "<p>Hei Kari,</p>" in h                                   # standardhilsenen bruker kontaktpersonens fornavn
    assert "Takk for påmeldingen av 1 deltaker fra EKSEMPEL KOMMUNE til <strong>Testkurs</strong>." in h
    assert "<li>1 fikk bekreftet plass</li>" in h
    assert "Full oversikt:" in h
    assert "Vennlig hilsen<br>Kursadministrasjonen, Institutt for Psykologisk Rådgivning<br>" in h


def test_B_override_kun_emne_gir_nytt_emne_og_standard_body(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    _lagre(con, "emne", "Kvittering: påmelding til {kursnavn}")
    klient = _klient()
    _post(klient, "T1")
    (til, emne, html), = _kvittering(config)
    assert emne == "Kvittering: påmelding til Testkurs"
    token = _fersk(con).execute("SELECT kvittering_token FROM firmapaamelding").fetchone()[0]
    url = f"/kurs/T1/gruppe/kvittering/{token}"
    standard = _n(epost.render("firmapaamelding_kvittering", **_direkte_data(kvittering_url=url, firmanavn="EKSEMPEL KOMMUNE"))[1])
    assert _n(html) == standard


def test_C_override_kun_innledning_gir_standard_emne_og_ny_body(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    _lagre(con, "innledning", "Hei {navn}!" + chr(10) + chr(10) + "{firmanavn} er paameldt {antall_deltakere} til {kursnavn}.")
    klient = _klient()
    _post(klient, "T1")
    (til, emne, html), = _kvittering(config)
    assert emne == STD_EMNE
    h = _n(html)
    assert "<p>Hei Kari HR!</p>" in h
    assert "EKSEMPEL KOMMUNE er paameldt 1 deltaker til <strong>Testkurs</strong>." in h
    assert "Takk for påmeldingen" not in h
    assert "<li>1 fikk bekreftet plass</li>" in h                          # laast blokk uendret
    assert "Vennlig hilsen" in h


def test_D_override_avslutning_legges_til_etter_den_laaste_blokken(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    _lagre(con, "avslutning", "Ta gjerne kontakt ved spørsmål, {navn}.")
    klient = _klient()
    _post(klient, "T1")
    (til, emne, html), = _kvittering(config)
    h = _n(html)
    assert "<li>1 fikk bekreftet plass</li>" in h
    assert "Ta gjerne kontakt ved spørsmål, Kari HR." in h
    assert h.index("bekreftet plass") < h.index("Ta gjerne kontakt")        # avslutning kommer ETTER den laaste blokken


# ============================ sikkerhet og verdier ============================

def test_html_i_override_vises_escapet_og_er_aldri_aktiv(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    _lagre(con, "innledning", "Hei <script>alert(1)</script> <b>fet</b> <a href='http://ond.no'>klikk</a>")
    _lagre(con, "emne", "Emne <script>alert(1)</script>")
    klient = _klient()
    _post(klient, "T1")
    (til, emne, html), = _kvittering(config)
    assert "<script>" not in html and "<b>" not in html and "<a href='http://ond.no'>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert emne == "Emne <script>alert(1)</script>"


def test_min_side_kan_brukes_og_er_systemets_lenke(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    _lagre(con, "avslutning", "Se ogsaa {min_side} for oversikt.")
    klient = _klient()
    _post(klient, "T1")
    (til, emne, html), = _kvittering(config)
    assert f'<a href="{config.BASE_URL}/min-side">Mine kurs</a>' in html


def test_placeholderverdi_med_spesialtegn_escapes_i_html_og_er_ren_tekst_i_emne(con, monkeypatch):
    from kurs.integrasjoner import brreg
    # Firmanavnet kommer fra registeret (felles regel): et registernavn med spesialtegn skal escapes på samme måte
    monkeypatch.setitem(brreg._DEMO_PER_NR, "999900003",
                        brreg.Enhet("999900003", "Firma <x> & Co", "Postboks 100", "1234", "EKSEMPELBY"))
    kid = _kurs(con, kapasitet=5, navn="Kurs A & B <x>")
    con.commit()
    _lagre(con, "emne", "Kvittering: {kursnavn} for {firmanavn}")
    _lagre(con, "innledning", "Hei {navn}, {kursnavn}")
    klient = _klient()
    _post(klient, "T1")
    (til, emne, html), = _kvittering(config)
    assert emne == "Kvittering: Kurs A & B <x> for Firma <x> & Co"
    h = _n(html)
    assert "<strong>Kurs A &amp; B &lt;x&gt;</strong>" in h and "<x>" not in html


# ============================ direkte epost.render: standard, aldri DB ============================

def test_direkte_epost_render_gir_standard_selv_naar_db_har_override(con):
    _lagre(con, "emne", "OVERRIDE")
    emne, html = epost.render("firmapaamelding_kvittering", **_direkte_data())
    assert emne == STD_EMNE and "OVERRIDE" not in html


# ============================ KRITISK: MalFeil FOER noen registrering ============================

def _korrupt_ugyldig_kode(con):
    con.execute("INSERT INTO mal_tekst (mal, felt, tekst) VALUES ('firmapaamelding_kvittering', 'innledning', 'Hei {ukjent}')")
    con.commit()


def _korrupt_ukjent_felt(con):
    con.execute("INSERT INTO mal_tekst (mal, felt, tekst) VALUES ('firmapaamelding_kvittering', 'finnes_ikke', 'Noe')")
    con.commit()


def _korrupt_tom_innledning(con):
    con.execute("INSERT INTO mal_tekst (mal, felt, tekst) VALUES ('firmapaamelding_kvittering', 'innledning', '   ')")
    con.commit()


def _db_lesefeil(con):
    con.execute("DROP TABLE mal_tekst")
    con.commit()


ALLE_SKADER = [(_korrupt_ugyldig_kode, UKJENT_KODE, "innledning"), (_korrupt_ukjent_felt, UKJENT_FELT, None),
              (_korrupt_tom_innledning, TOM, "innledning"), (_db_lesefeil, DB_LESEFEIL, None)]


@pytest.mark.parametrize("skade,grunn,felt", ALLE_SKADER)
def test_korrupt_override_gir_ingen_registrering_ingen_bekreftelser_ingen_kvittering(con, skade, grunn, felt):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    skade(con)
    klient = _klient()
    r = _post(klient, "T1")

    assert r.status_code == 400
    tekst = r.get_data(as_text=True)
    import re
    flash_tekster = [t.strip() for t in re.findall(r'class="flash[^"]*"[^>]*>\s*([^<]+)', tekst)]
    assert any("kunne ikke fullføres" in t.lower() for t in flash_tekster)
    for t in flash_tekster:                                                  # trygg melding: ingen intern info/PII
        for forbudt in ("MalFeil", grunn, "Ola Nordmann", "Kari HR", "Hei {"):
            assert forbudt not in t

    fersk = _fersk(con)
    assert fersk.execute("SELECT COUNT(*) FROM firmapaamelding").fetchone()[0] == 0
    assert fersk.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 0
    assert fersk.execute("SELECT COUNT(*) FROM utsending_logg").fetchone()[0] == 0
    assert _epost_utboks(config) == []
    hendelser = [json.loads(r["detaljer"]) for r in fersk.execute(
        "SELECT detaljer FROM hendelse WHERE handling='firmapaamelding_mal_feil'")]
    assert hendelser == [{"kurs_id": kid, "mal": "firmapaamelding_kvittering", "felt": felt, "grunn": grunn}]
    for h in fersk.execute("SELECT detaljer FROM hendelse"):                # ingen PII i NOEN hendelse
        for forbudt in ("Ola Nordmann", "ola@firma.no", "Kari HR", "kari.hr@firma.no", "Hei {"):
            assert forbudt not in (h["detaljer"] or "")
    fersk.close()


@pytest.mark.parametrize("skade,grunn,_felt", ALLE_SKADER)
def test_korrupt_override_ingen_claim_forsokt(con, monkeypatch, skade, grunn, _felt):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    skade(con)

    def forbudt(*a, **kw):
        raise AssertionError("claim ble forsokt selv om malen er ugyldig")
    monkeypatch.setattr(db, "reserver_sending", forbudt)
    monkeypatch.setattr(db, "reserver_sending_pa_nytt", forbudt)
    r = _post(_klient(), "T1")
    assert r.status_code == 400


def test_preflight_skjer_foer_firmapaamelding_finnes(con, monkeypatch):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    _korrupt_ugyldig_kode(con)

    def forbudt(*a, **kw):
        raise AssertionError("firmapaamelding ble opprettet selv om preflighten skulle stoppet det")
    monkeypatch.setattr(db, "finn_eller_opprett_firmapaamelding", forbudt)
    r = _post(_klient(), "T1")
    assert r.status_code == 400


# ============================ retry etter retting ============================

@pytest.mark.parametrize("rett", ["slett_overstyring", "erstatt_med_gyldig"])
def test_retry_etter_retting_registrerer_og_sender_normalt(con, rett):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    _korrupt_ugyldig_kode(con)
    klient = _klient()

    r1 = _post(klient, "T1")                                                # 1. forsok: MalFeil, ingen sideeffekt
    assert r1.status_code == 400
    assert _fersk(con).execute("SELECT COUNT(*) FROM firmapaamelding").fetchone()[0] == 0

    if rett == "slett_overstyring":
        con.execute("DELETE FROM mal_tekst")
    else:
        _lagre(con, "innledning", "Rettet tekst: {firmanavn} har meldt paa {antall_deltakere} til {kursnavn}.")
    con.commit()

    r2 = _post(klient, "T1", follow_redirects=True)                          # 2. nytt forsok: lykkes
    assert r2.status_code == 200
    fersk = _fersk(con)
    assert fersk.execute("SELECT COUNT(*) FROM firmapaamelding").fetchone()[0] == 1
    assert fersk.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 1
    (til, emne, html), = _kvittering(config)
    if rett == "erstatt_med_gyldig":
        assert "Rettet tekst: EKSEMPEL KOMMUNE har meldt paa 1 deltaker" in _n(html)
    assert len(_epost_utboks(config)) == 2                                   # 1 deltakerbekreftelse + 1 kvittering
    fersk.close()


# ============================ TOCTOU ============================

def test_toctou_malendring_mellom_preflight_og_send_paavirker_ikke_allerede_rendret_resultat(con, monkeypatch):
    """Selve TOCTOU-regresjonsvakten: overrideren endres til noe KORRUPT rett ETTER preflight-oppslaget (tekst A),
    men FOER registrering/send. Utsendingen skal likevel bruke A uendret - INGEN nytt DB-oppslag skal skje."""
    kid = _kurs(con, kapasitet=5)
    con.commit()
    _lagre(con, "innledning", "Tekst A: {firmanavn} har {antall_deltakere} til {kursnavn}.")

    ekte = maltekster.maltekst_for_utsending
    kall = []

    def spion(con_, mal, data):
        resultat = ekte(con_, mal, data)
        if mal == "firmapaamelding_kvittering":
            kall.append(resultat)
            con_.execute("UPDATE mal_tekst SET tekst='Hei {ukjent}' WHERE mal='firmapaamelding_kvittering' AND felt='innledning'")
            con_.commit()

            def sperre(con__, mal_, data_):
                if mal_ == "firmapaamelding_kvittering":
                    raise AssertionError("Nytt DB-avhengig maloppslag skjedde ETTER preflighten (TOCTOU)")
                return ekte(con__, mal_, data_)   # andre maler (f.eks. deltakerens "bekreftelse") uberort
            monkeypatch.setattr(maltekster, "maltekst_for_utsending", sperre)
        return resultat

    monkeypatch.setattr(maltekster, "maltekst_for_utsending", spion)
    r = _post(_klient(), "T1", follow_redirects=True)

    assert len(kall) == 1                                                    # kun ETT DB-avhengig oppslag i det hele tatt
    assert r.status_code == 200
    (til, emne, html), = _kvittering(config)
    assert "Tekst A: EKSEMPEL KOMMUNE har 1 deltaker" in _n(html)                    # det FOERSTE (gyldige) resultatet ble brukt
    fersk = _fersk(con)
    assert fersk.execute("SELECT COUNT(*) FROM firmapaamelding").fetchone()[0] == 1
    assert fersk.execute("SELECT COUNT(*) FROM hendelse WHERE handling='firmapaamelding_mal_feil'").fetchone()[0] == 0
    fersk.close()


def test_epost_render_med_eksplisitt_maltekst_gjor_ingen_db_lesing():
    import ast
    import inspect
    tre = ast.parse(inspect.getsource(epost))
    importert = {a.name for n in ast.walk(tre) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert "db" not in importert   # uendret fra tidligere faser - epost.py leser aldri DB selv


# ============================ Graph-feil: uendret fase-11-semantikk (dokumentert restrisiko) ============================

def test_graph_feil_etter_gyldig_registrering_gir_ukjent_og_ingen_automatisk_retry_ved_resubmit(con, monkeypatch):
    """Uendret fase-11-semantikk: en Graph-feil paa selve KVITTERINGEN (etter at deltakeren allerede er registrert
    og varslet) gir 'ukjent' og forplanter seg uendret (500). Deltakeren er UBERORT av dette - hen er allerede
    registrert og har allerede faatt SIN bekreftelse. MEN: siden resubmit-dedupen gjenkjenner firmapaameldingen
    og hopper rett til kvitteringssiden UTEN aa forsoke sendingen paa nytt, kan selve KVITTERINGEN til
    kontaktpersonen forbli permanent usendt etter en Graph-feil - dette er en kjent, EKSISTERENDE begrensning
    (uendret av denne migreringen) og loeses eventuelt av en separat retry-backlog, ikke her."""
    kid = _kurs(con, kapasitet=5)
    con.commit()
    ekte_send = epost.send

    def feiler_for_kontakt(til, *a, **kw):
        if til == "kari.hr@firma.no":
            raise RuntimeError("Graph nede")
        return ekte_send(til, *a, **kw)
    monkeypatch.setattr(epost, "send", feiler_for_kontakt)

    klient = _klient()
    r1 = _post(klient, "T1")
    assert r1.status_code == 500                                             # uendret: forplanter seg, ingen graceful 400

    fersk = _fersk(con)
    assert fersk.execute("SELECT COUNT(*) FROM firmapaamelding").fetchone()[0] == 1     # registreringen er GYLDIG og ekte
    assert fersk.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 1
    assert fersk.execute(
        "SELECT status FROM utsending_logg WHERE mottaker='kari.hr@firma.no'").fetchone()[0] == "ukjent"
    assert len(_kvittering(config)) == 0
    assert len(_epost_utboks(config)) == 1                                   # deltakerens EGEN bekreftelse gikk ut fint
    fersk.close()

    monkeypatch.setattr(epost, "send", ekte_send)                            # "Graph er oppe igjen"
    r2 = _post(klient, "T1", follow_redirects=False)                         # bruker sender skjemaet paa nytt
    assert r2.status_code == 302                                             # dedup: rett til kvitteringssiden
    assert len(_kvittering(config)) == 0                                     # ... og kvitteringen ble ALDRI sendt
    fersk2 = _fersk(con)
    assert fersk2.execute("SELECT COUNT(*) FROM utsending_logg WHERE type='firmapaamelding_kvittering'"
                          ).fetchone()[0] == 1                               # ingen duplikatforsok heller
    fersk2.close()


# ============================ resubmit / idempotens ============================

def test_normal_resubmit_gir_ingen_duplikater(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    klient = _klient()
    _post(klient, "T1")
    _post(klient, "T1")
    fersk = _fersk(con)
    assert fersk.execute("SELECT COUNT(*) FROM firmapaamelding").fetchone()[0] == 1
    assert fersk.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 1
    assert len(_epost_utboks(config)) == 2                                   # 1 bekreftelse + 1 kvittering, ikke 4


def test_override_paavirker_ikke_andre_maler(con):
    kid = _kurs(con, kapasitet=5)
    con.commit()
    _lagre(con, "emne", "FIRMA-EMNE")
    klient = _klient()
    _post(klient, "T1")
    bekreftelse = [m for m in _epost_utboks(config) if m[0] == "ola@firma.no"]
    assert bekreftelse and "Bekreftelse" in bekreftelse[0][1]                # deltakerens bekreftelse er standard
    assert _kvittering(config)[0][1] == "FIRMA-EMNE"
