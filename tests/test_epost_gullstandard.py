"""Fase 12A: GULLSTANDARD for e-postmalene - dagens standardoutput laases FOER noe refaktoreres.

Malene er i dag Jinja-filer i kurs/maler/epost/. Fase 12B+ gjor tekstfelt redigerbare (standardtekst i kode +
databaseoverstyring). Disse testene beviser at STANDARDOPPFORSELEN ikke endres av det arbeidet.

Vi tester semantisk (emne, sentrale setninger, varianter, systemstyrte deler, escaping) med normalisert
mellomrom - ikke skjore byte-snapshots. To lag:
  1. direkte epost.render(...) med faste eksempeldata (alle varianter)
  2. ende-til-ende: den ekte koden som sender (sveiper, daglig, ruter) - fanger det som faktisk sendes
De tre LAASTE malene (innlogging, eskalering, admin_melding) er ikke redigerbare i v1; de har en kort baseline.
"""
import re
from datetime import date, timedelta

import pytest

from kurs import config, daglig, db, sveiper
from kurs.integrasjoner import epost
from kurs.kjoring import Kjoring
from kurs.sveiper import PLAN_INGEN, PLAN_MANGLER_KURSDAG, PLAN_NA, PLAN_PER_SAMLING, PLAN_UTSATT, FakturaPlan

BASE = config.BASE_URL


def _n(html: str) -> str:
    """Normaliserer mellomrom/linjeskift - malene har linjeskift midt i setninger."""
    return " ".join(html.split())


def _render(mal, **data):
    emne, html = epost.render(mal, **data)
    return emne, _n(html)


def _render_bek(**data):
    """bekreftelse krever faktura_plan (systemets laaste fakturablokk) - standard her: samlet faktura 'na'."""
    data.setdefault("faktura_plan", FakturaPlan(PLAN_NA))
    return _render("bekreftelse", **data)


KURS = {"navn": "Veiledning i praksis", "type": "fysisk", "sted": "Oslo", "start_kl": "09:00", "slutt_kl": "15:00",
        "pris_nok": 1500, "fakturering": "person", "faktura_dager_for": 14, "notat": None,
        "zoom_url": None, "zoom_id": None, "zoom_pw": None}
DAG1 = {"dato": "2027-03-01", "start_kl": None, "slutt_kl": None}
DAG2 = {"dato": "2027-03-02", "start_kl": "10:00", "slutt_kl": "12:30"}
DELTAKER = {"navn": "Ola Nordmann", "betaling": "samlet", "betaler": "person", "org_navn": None}


def _kurs(**over):
    return {**KURS, **over}


# ============================ felles: rammen ============================

@pytest.mark.parametrize("mal,data", [
    ("bekreftelse", dict(p=DELTAKER, kurs=KURS, dager=[DAG1], faktura_plan=FakturaPlan(PLAN_NA))),
    ("venteliste", dict(p=DELTAKER, kurs=KURS)),
    ("avlysning", dict(d=DELTAKER, kurs=KURS)),
    ("kursbevis_klar", dict(navn="Ola Nordmann", kurs=KURS)),
])
def test_alle_maler_har_felles_ramme_med_signatur_og_min_side_lenke(mal, data):
    emne, html = _render(mal, **data)
    assert "Vennlig hilsen<br>Kursadministrasjonen, Institutt for Psykologisk Rådgivning<br>" in html
    assert f'<a href="{BASE}/min-side">Min side</a> – her finner du kursmateriell, oppmøte og faktura.' in html
    assert "Emne:" not in html and html.startswith("<div")   # emne-linja er ikke med i selve kroppen


# ============================ bekreftelse ============================

def test_bekreftelse_emne_hilsen_og_kursdager():
    emne, html = _render_bek(p=DELTAKER, kurs=KURS, dager=[DAG1, DAG2])
    assert emne == "Bekreftelse: Veiledning i praksis"
    assert "<p>Hei Ola Nordmann,</p>" in html
    assert "Takk for påmeldingen! Du har fått plass på <strong>Veiledning i praksis</strong>." in html
    assert "<li>2027-03-01 kl. 09:00–15:00</li>" in html          # dagens tider arves fra kurset
    assert "<li>2027-03-02 kl. 10:00–12:30</li>" in html          # egne tider pa kursdagen
    assert ('Du kan når som helst logge inn på <a href="%s/min-side">Min side</a> med e-postadressen din.' % BASE) in html


@pytest.mark.parametrize("type_,forventet,ikke", [
    ("digital", "Kurset er digitalt. Zoom-lenke kommer på e-post dagen før hver kursdag.", "Sted:"),
    ("fysisk", "Sted: Oslo.", "Kurset er digitalt"),
    ("hybrid", "Sted: Oslo.", "Kurset er digitalt"),
])
def test_bekreftelse_digitalt_eller_sted(type_, forventet, ikke):
    _, html = _render_bek(p=DELTAKER, kurs=_kurs(type=type_), dager=[DAG1])
    assert forventet in html and ikke not in html


def test_bekreftelse_fysisk_uten_sted_sier_kommer():
    _, html = _render_bek(p=DELTAKER, kurs=_kurs(sted=None), dager=[DAG1])
    assert "Sted: kommer." in html


ORG = {"betaler": "organisasjon", "org_navn": "Firma AS"}


@pytest.mark.parametrize("p_over,plan,forventet", [
    ({}, FakturaPlan(PLAN_NA), "Faktura på 1500 kr sendes separat til deg."),
    (ORG, FakturaPlan(PLAN_NA), "Faktura på 1500 kr sendes separat til Firma AS."),
    ({}, FakturaPlan(PLAN_UTSATT, date(2027, 3, 20)), "Faktura på 1500 kr sendes separat til deg, tidligst 20.03.2027."),
    (ORG, FakturaPlan(PLAN_UTSATT, date(2027, 3, 20)), "Faktura på 1500 kr sendes separat til Firma AS, tidligst 20.03.2027."),
    ({}, FakturaPlan(PLAN_MANGLER_KURSDAG), "Faktura på 1500 kr sendes separat til deg."),
    (ORG, FakturaPlan(PLAN_MANGLER_KURSDAG), "Faktura på 1500 kr sendes separat til Firma AS."),
    ({"betaling": "per_samling"}, FakturaPlan(PLAN_PER_SAMLING),
     "Kursavgiften på 1500 kr deles på 2 samlinger, og faktura for hver samling sendes 14 dager før samlingen til deg."),
    ({"betaling": "per_samling", **ORG}, FakturaPlan(PLAN_PER_SAMLING),
     "faktura for hver samling sendes 14 dager før samlingen til Firma AS."),
])
def test_bekreftelse_faktureringstekst_varianter(p_over, plan, forventet):
    _, html = _render_bek(p={**DELTAKER, **p_over}, kurs=KURS, dager=[DAG1, DAG2], faktura_plan=plan)
    assert forventet in html
    assert "Faktura er sendt" not in html and "Faktura er opprettet" not in html      # mailen sendes FOER fakturaforsoket
    if plan.modus in (PLAN_NA, PLAN_MANGLER_KURSDAG):
        assert "tidligst" not in html and not re.search(r"\d\d\.\d\d\.\d{4}", html)      # ingen dato (mangler_kursdag: ingen oppdiktet dato)


@pytest.mark.parametrize("dager_for,forventet,ikke", [
    (0, "0 dager før samlingen", "0 dag før"),
    (1, "1 dag før samlingen", "1 dager"),
    (2, "2 dager før samlingen", "2 dag før"),
    (14, "14 dager før samlingen", "14 dag før"),
    (180, "180 dager før samlingen", "180 dag før"),
])
def test_bekreftelse_per_samling_dag_i_entall_kun_for_1(dager_for, forventet, ikke):
    _, html = _render_bek(p={**DELTAKER, "betaling": "per_samling"}, kurs=_kurs(faktura_dager_for=dager_for), dager=[DAG1, DAG2],
                          faktura_plan=FakturaPlan(PLAN_PER_SAMLING))
    assert f"faktura for hver samling sendes {forventet} til deg." in html and ikke not in html


def test_bekreftelse_utsatt_faktura_viser_dato_dd_mm_yyyy_og_ellers_aldri_tidligst():
    _, utsatt = _render_bek(p=DELTAKER, kurs=KURS, dager=[DAG1], faktura_plan=FakturaPlan(PLAN_UTSATT, date(2027, 3, 20)))
    assert "tidligst 20.03.2027" in utsatt and "2027-03-20" not in utsatt
    for plan in (FakturaPlan(PLAN_NA), FakturaPlan(PLAN_MANGLER_KURSDAG), FakturaPlan(PLAN_PER_SAMLING)):
        _, html = _render_bek(p=DELTAKER, kurs=KURS, dager=[DAG1], faktura_plan=plan)
        assert "tidligst" not in html


@pytest.mark.parametrize("kurs_over", [{"pris_nok": 0}, {"fakturering": "organisasjon"}, {"fakturering": "ingen"}])
def test_bekreftelse_uten_faktureringstekst_naar_planen_er_ingen(kurs_over):
    _, html = _render_bek(p=DELTAKER, kurs=_kurs(**kurs_over), dager=[DAG1], faktura_plan=FakturaPlan(PLAN_INGEN))
    assert "Faktura på" not in html and "sendes separat" not in html and "Kursavgiften" not in html
    assert "faktura for hver samling" not in html


def test_bekreftelse_escaper_deltaker_og_kursnavn():
    _, html = _render_bek(p={**DELTAKER, "navn": "<script>alert(1)</script> & Co"},
                      kurs=_kurs(navn="Kurs A & B <x>"), dager=[DAG1])
    assert "<script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt; &amp; Co" in html
    assert "<strong>Kurs A &amp; B &lt;x&gt;</strong>" in html


# ============================ venteliste ============================

def test_venteliste_emne_og_tekst():
    emne, html = _render("venteliste", p=DELTAKER, kurs=KURS)
    assert emne == "Venteliste: Veiledning i praksis"
    assert "<p>Hei Ola Nordmann,</p>" in html
    assert "<strong>Veiledning i praksis</strong> er dessverre fullt, men du står nå på venteliste." in html
    assert "Blir det en ledig plass, får du automatisk plassen og en bekreftelse på e-post." in html


# ============================ ukefor ============================

def test_ukefor_emne_kursdager_og_fysisk_info():
    emne, html = _render("ukefor", d=DELTAKER, kurs=KURS, dager=[DAG1, DAG2])
    assert emne == "Velkommen til Veiledning i praksis – praktisk informasjon"
    assert "Nå er det snart tid for <strong>Veiledning i praksis</strong>, som starter 2027-03-01." in html
    assert "<li>2027-03-01 kl. 09:00–15:00</li>" in html and "<li>2027-03-02 kl. 10:00–12:30</li>" in html
    assert "<strong>Sted:</strong> Oslo" in html
    assert f"taste koden på {BASE}/innsjekk" in html and "skanne QR-koden" in html
    assert "Kurset holdes på Zoom" not in html


def test_ukefor_digitalt_kurs_har_zoominfo_og_ikke_sted():
    _, html = _render("ukefor", d=DELTAKER, kurs=_kurs(type="digital"), dager=[DAG1])
    assert "Kurset holdes på Zoom. Du får lenken på e-post dagen før hver kursdag. Test gjerne lyd og bilde på forhånd." in html
    assert "Sted:" not in html and "QR-koden" not in html


def test_ukefor_kursnotat_tas_med_og_escapes_kun_naar_det_finnes():
    _, med = _render("ukefor", d=DELTAKER, kurs=_kurs(notat="Ta med <lue> & votter"), dager=[DAG1])
    assert "<p>Ta med &lt;lue&gt; &amp; votter</p>" in med
    _, uten = _render("ukefor", d=DELTAKER, kurs=_kurs(notat=None), dager=[DAG1])
    assert "Ta med" not in uten


# ============================ dagfor (tre varianter) ============================

@pytest.mark.parametrize("nr,antall,emne_forventet,setning", [
    (1, 3, "I morgen starter Veiledning i praksis", "I morgen starter <strong>Veiledning i praksis</strong>!"),
    (2, 3, "I morgen, dag 2: Veiledning i praksis", "I morgen er dag 2 av 3 på <strong>Veiledning i praksis</strong>."),
    (3, 3, "Siste kursdag i morgen: Veiledning i praksis",
     "I morgen er siste kursdag på <strong>Veiledning i praksis</strong>."),
    (1, 1, "I morgen starter Veiledning i praksis", "I morgen starter <strong>Veiledning i praksis</strong>!"),  # første vinner
])
def test_dagfor_emne_og_setning_per_variant(nr, antall, emne_forventet, setning):
    emne, html = _render("dagfor", d=DELTAKER, kurs=KURS, dag=DAG2, nr=nr, antall=antall)
    assert emne == emne_forventet
    assert "<p>Hei Ola Nordmann,</p>" in html and setning in html
    assert "Tid: 2027-03-02 kl. 10:00–12:30" in html


def test_dagfor_tid_arver_fra_kurset_naar_dagen_mangler_tider():
    _, html = _render("dagfor", d=DELTAKER, kurs=KURS, dag=DAG1, nr=1, antall=2)
    assert "Tid: 2027-03-01 kl. 09:00–15:00" in html


def test_dagfor_digitalt_har_zoomknapp_med_passord_og_ikke_sted():
    kurs = _kurs(type="digital", zoom_url="https://zoom.example/j/1", zoom_id="123 456", zoom_pw="pw1")
    _, html = _render("dagfor", d=DELTAKER, kurs=kurs, dag=DAG1, nr=1, antall=1)
    assert '<a href="https://zoom.example/j/1"' in html and ">Bli med på Zoom</a>" in html
    assert "Møte-ID: 123 456 · Passord: pw1" in html
    assert "Bruk samme navn og e-post som ved påmelding – da registreres oppmøtet ditt automatisk." in html
    assert "Sted:" not in html and "QR-koden" not in html


def test_dagfor_zoom_uten_passord_og_uten_lenke():
    kurs = _kurs(type="digital", zoom_url="https://zoom.example/j/1", zoom_id="123", zoom_pw=None)
    _, uten_pw = _render("dagfor", d=DELTAKER, kurs=kurs, dag=DAG1, nr=1, antall=1)
    assert "Møte-ID: 123" in uten_pw and "Passord" not in uten_pw
    _, uten_lenke = _render("dagfor", d=DELTAKER, kurs=_kurs(type="digital"), dag=DAG1, nr=1, antall=1)
    assert "Bli med på Zoom" not in uten_lenke


def test_dagfor_hybrid_har_baade_zoom_og_sted_og_fysisk_bare_sted():
    hybrid = _kurs(type="hybrid", zoom_url="https://zoom.example/j/2", zoom_id="9")
    _, h = _render("dagfor", d=DELTAKER, kurs=hybrid, dag=DAG1, nr=1, antall=1)
    assert "Bli med på Zoom" in h and "Sted: Oslo. Husk å registrere oppmøte med QR-koden når du kommer." in h
    _, f = _render("dagfor", d=DELTAKER, kurs=_kurs(zoom_url="https://zoom.example/j/3"), dag=DAG1, nr=1, antall=1)
    assert "Bli med på Zoom" not in f and "Sted: Oslo. Husk å registrere oppmøte" in f


def test_dagfor_escaper_kursnavn():
    emne, html = _render("dagfor", d=DELTAKER, kurs=_kurs(navn="A <b>&</b>"), dag=DAG1, nr=1, antall=2)
    assert "<b>" not in html and "A &lt;b&gt;&amp;&lt;/b&gt;" in html


# ============================ avlysning ============================

def test_avlysning_emne_og_tekst():
    emne, html = _render("avlysning", d=DELTAKER, kurs=KURS)
    assert emne == "Avlyst: Veiledning i praksis"
    assert "<p>Hei Ola Nordmann,</p>" in html
    assert "Vi må dessverre informere om at <strong>Veiledning i praksis</strong> er avlyst." in html
    assert "Har du allerede mottatt faktura, tar kursadministrasjonen kontakt med deg om det videre." in html
    assert f"bruke «Spør oss» på {BASE}/sporsmal." in html
    assert "Vi beklager ulempen dette medfører." in html


# ============================ kursbevis_klar ============================

def test_kursbevis_klar_emne_og_lenke():
    emne, html = _render("kursbevis_klar", navn="Ola Nordmann", kurs=KURS)
    assert emne == "Kursbevis: Veiledning i praksis"
    assert "<p>Hei Ola Nordmann,</p>" in html
    assert ("Takk for deltakelsen på <strong>Veiledning i praksis</strong>. Kursbeviset ditt ligger nå på "
            '<a href="%s/min-side">Min side</a>.' % BASE) in html


# ============================ firmapaamelding_kvittering ============================

KONTAKT = {"navn": "Kari HR", "firmanavn": "Firma AS"}


def _firma(**over):
    data = dict(kontakt=KONTAKT, kurs=KURS, antall_totalt=3, antall_bekreftet=3, antall_venteliste=0, antall_feilet=0,
                kvittering_url="/kurs/T1/gruppe/kvittering/tok")
    data.update(over)
    return _render("firmapaamelding_kvittering", **data)


def test_firma_emne_intro_og_oversiktslenke():
    emne, html = _firma()
    assert emne == "Bedriftspåmelding til Veiledning i praksis – kvittering"
    assert "<p>Hei Kari HR,</p>" in html
    assert "Takk for påmeldingen av 3 deltakere fra Firma AS til <strong>Veiledning i praksis</strong>." in html
    assert "<li>3 fikk bekreftet plass</li>" in html
    assert f'<a href="{BASE}/kurs/T1/gruppe/kvittering/tok">{BASE}/kurs/T1/gruppe/kvittering/tok</a>' in html


def test_firma_entall_og_valgfrie_punkter():
    _, en = _firma(antall_totalt=1, antall_bekreftet=1)
    assert "påmeldingen av 1 deltaker fra" in en and "deltakere fra" not in en
    assert "står på venteliste" not in en and "kunne ikke registreres" not in en
    _, alle = _firma(antall_totalt=4, antall_bekreftet=2, antall_venteliste=1, antall_feilet=1)
    assert "<li>1 står på venteliste og får plass automatisk hvis det blir ledig</li>" in alle
    assert "<li>1 kunne ikke registreres – se oversikten på lenken under for detaljer</li>" in alle


@pytest.mark.parametrize("kurs_over,forventet", [
    ({}, "Hver bekreftet deltaker faktureres for seg, 1 500 kr, til Firma AS."),
    ({"fakturering": "organisasjon"},
     "Dere faktureres samlet av kursadministrasjonen i etterkant – ikke automatisk gjennom dette skjemaet."),
    ({"fakturering": "ingen"}, "Dette kurset faktureres ikke automatisk."),
    ({"pris_nok": 0}, "Dette kurset faktureres ikke automatisk."),
])
def test_firma_faktureringstekst(kurs_over, forventet):
    _, html = _firma(kurs=_kurs(**kurs_over))
    assert forventet in html


def test_firma_escaper_firmanavn():
    _, html = _firma(kontakt={"navn": "Kari <i>HR</i>", "firmanavn": "A&B <AS>"})
    assert "<i>" not in html and "A&amp;B &lt;AS&gt;" in html


# ============================ purring ============================

M = {"ansvarlig_navn": "Per Psykolog", "ansvarlig_epost": "per@x.no", "beskrivelse": "Kurspresentasjon",
     "kursnavn": "Veiledning i praksis", "kode": "V1", "frist": "2027-02-20", "id": 7}


@pytest.mark.parametrize("igjen,emne_forventet", [
    (7, "Påminnelse: Kurspresentasjon til Veiledning i praksis – frist om 7 dager"),
    (2, "Påminnelse: Kurspresentasjon til Veiledning i praksis – frist om 2 dager"),
    (0, "Frist i dag: Kurspresentasjon til Veiledning i praksis"),
])
def test_purring_emne_per_variant(igjen, emne_forventet):
    emne, html = _render("purring", m=M, igjen=igjen)
    assert emne == emne_forventet
    assert "<p>Hei Per Psykolog,</p>" in html
    assert ("Vi minner om at <strong>kurspresentasjon</strong> til <strong>Veiledning i praksis</strong> "
            "skal leveres innen <strong>2027-02-20</strong>.") in html
    assert f'<a href="{BASE}/lever/7">Last opp her</a> – så blir materiellet automatisk tilgjengelig for deltakerne.' in html


# ============================ laaste maler (ikke redigerbare i v1): kort baseline ============================

def test_laast_innlogging():
    emne, html = _render("innlogging", navn="Ola Nordmann", lenke="https://x.no/logg-inn/tok")
    assert emne == "Logg inn på Min side – IPR Påmeldingssystem"
    assert "Lenken virker i 30 minutter og kan bare brukes én gang." in html
    assert '<a href="https://x.no/logg-inn/tok"' in html and ">Logg inn</a>" in html
    assert "Ba du ikke om dette? Da kan du se bort fra e-posten." in html


def test_laast_eskalering():
    emne, html = _render("eskalering", m=M, igjen=-3)
    assert emne == "Mangler materiell: Veiledning i praksis (3 dager over frist)"
    assert "Per Psykolog (per@x.no) har ikke levert <strong>kurspresentasjon</strong>" in html
    assert "Fristen var 2027-02-20. Automatiske påminnelser er sendt." in html


def test_laast_admin_melding_escaper_tekst():
    emne, html = _render("admin_melding", emne="Viktig", tekst="Hei <b>alle</b>\nNy linje", d=DELTAKER)
    assert emne == "Viktig"
    assert "<p>Hei Ola Nordmann,</p>" in html
    assert "Hei &lt;b&gt;alle&lt;/b&gt;" in html and "Ny linje" in html and "<b>" not in html


@pytest.mark.xfail(strict=True, reason="KJENT EKSISTERENDE FEIL (funnet i 12A, ikke rettet der): admin_melding.html bruker "
                                       "`replace('\n', '<br>\n')` paa en Markup-verdi, og Markup.replace escaper erstatningen - "
                                       "linjeskift vises som bokstavelig '<br>' i mottakers e-post. Fjern xfail naar det rettes.")
def test_kjent_feil_linjeskift_i_manuell_epost_blir_ekte_br():
    _, html = _render("admin_melding", emne="Viktig", tekst="Linje 1\nLinje 2", d=DELTAKER)
    assert "Linje 1<br>" in html and "&lt;br&gt;" not in html


# ====================================================================================
# LAG 2: ende-til-ende - det som FAKTISK sendes av den ekte koden
# ====================================================================================

@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "ROT", tmp_path)
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


@pytest.fixture
def sendt(monkeypatch):
    """Fanger (til, emne, html) fra epost.send - ingenting sendes."""
    ut = []
    monkeypatch.setattr(epost, "send", lambda til, emne, html, *a, **kw: ut.append((til, emne, _n(html))))
    return ut


def _opprett(con, start: date, kode="E1", **kw):
    kid = db.opprett_kurs(con, kode=kode, navn="Veiledning i praksis", datoer=[start.isoformat()],
                          sharepoint_mappe=f"Kurs/{kode}", **{"pris_nok": 1500, "fakturering": "person", "sted": "Oslo",
                                                              "type": "fysisk", **kw})
    con.commit()
    return kid


def test_e2e_bekreftelse_via_sveiper(con, sendt):
    kid = _opprett(con, date(2027, 3, 1))
    pid, _ = db.meld_paa(con, kid, epost="ola@x.no", navn="Ola Nordmann")
    con.commit()
    sveiper.kjor(Kjoring(con, idag=date(2027, 3, 1)), pid)
    (til, emne, html), = [m for m in sendt if m[1].startswith("Bekreftelse")]
    assert (til, emne) == ("ola@x.no", "Bekreftelse: Veiledning i praksis")
    assert "Du har fått plass på <strong>Veiledning i praksis</strong>." in html
    assert "<li>2027-03-01 kl. 09:00–16:00</li>" in html and "Sted: Oslo." in html   # standard sluttid er 16:00
    assert "Faktura på 1500 kr sendes separat til deg." in html and "tidligst" not in html


def test_e2e_venteliste_via_sveiper(con, sendt):
    kid = _opprett(con, date(2027, 3, 1), kapasitet=1)
    db.meld_paa(con, kid, epost="forst@x.no", navn="Forst")
    pid, status = db.meld_paa(con, kid, epost="ola@x.no", navn="Ola Nordmann")
    assert status == "venteliste"
    con.commit()
    sveiper.kjor(Kjoring(con, idag=date(2027, 3, 1)), pid)
    (til, emne, html), = [m for m in sendt if m[0] == "ola@x.no"]
    assert emne == "Venteliste: Veiledning i praksis" and "er dessverre fullt, men du står nå på venteliste." in html


def test_e2e_ukefor_og_dagfor_via_daglig(con, sendt):
    start = date(2027, 3, 4)
    kid = _opprett(con, start, kode="E2")
    db.meld_paa(con, kid, epost="ola@x.no", navn="Ola Nordmann")
    con.commit()
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    dager = db.kursdager(con, kid)
    daglig._innkallinger(Kjoring(con, idag=date(2027, 3, 1)), kurs, dager, start)        # 3 dager for -> ukefor
    (til, emne, html), = sendt
    assert (til, emne) == ("ola@x.no", "Velkommen til Veiledning i praksis – praktisk informasjon")
    assert "som starter 2027-03-04." in html and "<strong>Sted:</strong> Oslo" in html
    sendt.clear()
    daglig._innkallinger(Kjoring(con, idag=date(2027, 3, 3)), kurs, dager, start)        # dagen for -> dagfor
    (til, emne, html), = sendt
    assert (til, emne) == ("ola@x.no", "I morgen starter Veiledning i praksis")
    assert "Tid: 2027-03-04 kl. 09:00–16:00" in html and "Husk å registrere oppmøte med QR-koden" in html


def test_e2e_purring_via_daglig(con, sendt):
    kid = _opprett(con, date(2027, 4, 1), kode="E3")
    con.execute("INSERT INTO materiell_krav (kurs_id, ansvarlig_navn, ansvarlig_epost, frist) VALUES (?,?,?,?)",
                (kid, "Per Psykolog", "per@x.no", "2027-02-08"))
    con.commit()
    daglig._purring(Kjoring(con, idag=date(2027, 2, 1)))                                # 7 dager igjen
    (til, emne, html), = sendt
    assert (til, emne) == ("per@x.no", "Påminnelse: Kurspresentasjon til Veiledning i praksis – frist om 7 dager")
    assert "skal leveres innen <strong>2027-02-08</strong>" in html and "/lever/" in html


def _admin_klient():
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def test_e2e_avlysning_via_admin_rute(con, sendt):
    kid = _opprett(con, date.today() + timedelta(days=30), kode="E4")
    db.meld_paa(con, kid, epost="ola@x.no", navn="Ola Nordmann")
    con.commit()
    _admin_klient().post(f"/admin/kurs/{kid}/avlys")
    (til, emne, html), = [m for m in sendt if m[0] == "ola@x.no"]
    assert emne == "Avlyst: Veiledning i praksis"
    assert "er avlyst." in html and "Vi beklager ulempen dette medfører." in html


def test_e2e_firmakvittering_via_gruppe_rute(con, sendt):
    from kurs.web import app as webapp
    _opprett(con, date.today() + timedelta(days=30), kode="E5")
    webapp.app.test_client().post("/kurs/E5/gruppe", data={
        "kontakt_navn": "Kari HR", "kontakt_epost": "kari.hr@firma.no", "kontakt_telefon": "90000000",
        "firmanavn": "Firma AS", "org_nr": "999888777", "faktura_ref": "B1",
        "deltaker_navn": ["Ola Nordmann"], "deltaker_epost": ["ola@firma.no"],
        "deltaker_telefon": [""], "deltaker_arbeidssted": [""], "samtykke": "on"})
    (til, emne, html), = [m for m in sendt if m[0] == "kari.hr@firma.no"]
    assert emne == "Bedriftspåmelding til Veiledning i praksis – kvittering"
    assert "Takk for påmeldingen av 1 deltaker fra Firma AS" in html and "<li>1 fikk bekreftet plass</li>" in html
