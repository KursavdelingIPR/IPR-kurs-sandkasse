"""Fase 12B2A: maltekstfundamentet koblet til den ekte utsendingen - KUN `venteliste`.

Flyt: sveiper -> Kjoring.send_en_gang -> maltekster.maltekst_for_utsending (override eller standard, FOER claim)
      -> epost.render (Jinja: systemramme + ferdig rendret maltekst) -> claim -> epost.send.
De 7 andre redigerbare malene og de laaste malene rendres fortsatt med gammel rendering (uendret).
"""
import ast
import inspect
import json
from datetime import date

import pytest
from markupsafe import Markup

from kurs import config, db, maltekster, sveiper
from kurs.integrasjoner import epost
from kurs.kjoring import Kjoring
from kurs.maltekster import DB_LESEFEIL, IKKE_AKTIVERT, MALER, TOM, UKJENT_FELT, UKJENT_KODE, MalFeil

IDAG = date(2027, 3, 1)
STD_EMNE = "Venteliste: Veiledning i praksis"


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
def sendt(monkeypatch):
    ut = []
    monkeypatch.setattr(epost, "send", lambda til, emne, html, *a, **kw: ut.append((til, emne, html)))
    return ut


def _flyt(con, kursnavn="Veiledning i praksis", navn="Ola Nordmann"):
    """Kurs med plass til én: 'forst' faar plass, `navn` havner paa venteliste. Returnerer (kurs_id, paamelding_id)."""
    kid = db.opprett_kurs(con, kode="V1", navn=kursnavn, datoer=["2027-03-01"], sharepoint_mappe="Kurs/V1",
                          pris_nok=0, kapasitet=1)
    db.meld_paa(con, kid, epost="forst@x.no", navn="Forst")
    pid, status = db.meld_paa(con, kid, epost="ola@x.no", navn=navn)
    assert status == "venteliste"
    con.commit()
    return kid, pid


def _kjor(con, pid):
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)
    con.commit()


def _mine(sendt):
    return [m for m in sendt if m[0] == "ola@x.no"]


def _lagre(con, felt, tekst):
    maltekster.lagre_maltekst(con, "venteliste", felt, tekst, aktor="admin:test")
    con.commit()


def _direkte_data(navn="Ola Nordmann", kursnavn="Veiledning i praksis"):
    return dict(p={"navn": navn}, kurs={"navn": kursnavn})


def _kall_send_en_gang(con, kid, pid):
    p = con.execute(sveiper.SQL_DELTAKER + " WHERE p.id=?", (pid,)).fetchone()
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    return Kjoring(con, idag=IDAG).send_en_gang(f"kurs:{kid}", "ola@x.no", "venteliste", "venteliste",
                                                paamelding_id=pid, p=p, kurs=kurs)


# ============================ A-D: standard og override gjennom ekte flyt ============================

def test_A_uten_override_er_venteliste_identisk_med_dagens_standard(con, sendt):
    kid, pid = _flyt(con)
    _kjor(con, pid)
    (til, emne, html), = _mine(sendt)
    assert emne == STD_EMNE
    assert _n(html) == _n(epost.render("venteliste", **_direkte_data())[1])     # samme som direkte standard-render
    h = _n(html)
    assert "<p>Hei Ola Nordmann,</p>" in h
    assert "<strong>Veiledning i praksis</strong> er dessverre fullt, men du står nå på venteliste." in h
    assert "Blir det en ledig plass, får du automatisk plassen og en bekreftelse på e-post." in h
    assert "Vennlig hilsen<br>Kursadministrasjonen, Institutt for Psykologisk Rådgivning<br>" in h    # systemramme uendret


def test_B_override_kun_emne_gir_nytt_emne_og_standard_body(con, sendt):
    kid, pid = _flyt(con)
    standard = _n(epost.render("venteliste", **_direkte_data())[1])
    _lagre(con, "emne", "Du står på venteliste: {kursnavn}")
    _kjor(con, pid)
    (til, emne, html), = _mine(sendt)
    assert emne == "Du står på venteliste: Veiledning i praksis"
    assert _n(html) == standard


def test_C_override_kun_tekst_gir_standard_emne_og_ny_body(con, sendt):
    kid, pid = _flyt(con)
    _lagre(con, "tekst", "Hei {navn}!" + chr(10) + chr(10) + "Det er fullt på {kursnavn}." + chr(10) + "Vi gir beskjed.")
    _kjor(con, pid)
    (til, emne, html), = _mine(sendt)
    assert emne == STD_EMNE
    h = _n(html)
    assert "<p>Hei Ola Nordmann!</p> <p>Det er fullt på <strong>Veiledning i praksis</strong>.<br> Vi gir beskjed.</p>" in h
    assert "er dessverre fullt" not in h                                   # standardteksten er erstattet
    assert "Vennlig hilsen" in h                                            # men systemrammen er der


def test_D_override_begge_felt_brukes(con, sendt):
    kid, pid = _flyt(con)
    _lagre(con, "emne", "Ventelisteplass")
    _lagre(con, "tekst", "Ny tekst")
    _kjor(con, pid)
    (til, emne, html), = _mine(sendt)
    assert emne == "Ventelisteplass" and "<p>Ny tekst</p>" in _n(html)


# ============================ E-G: sikkerhet og verdier ============================

def test_E_html_i_override_vises_escapet_og_er_aldri_aktiv(con, sendt):
    kid, pid = _flyt(con)
    _lagre(con, "tekst", "Hei <script>alert(1)</script> <b>fet</b> <a href='http://ond.no'>klikk</a>")
    _lagre(con, "emne", "Emne <script>alert(1)</script>")
    _kjor(con, pid)
    (til, emne, html), = _mine(sendt)
    assert "<script>" not in html and "<b>" not in html and "<a href='http://ond.no'>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "&lt;b&gt;fet&lt;/b&gt;" in html
    assert emne == "Emne <script>alert(1)</script>"                          # emnet er ren tekst - ingen HTML-tolkning


def test_F_placeholders_navn_og_kursnavn_gir_riktige_verdier(con, sendt):
    kid, pid = _flyt(con)
    _lagre(con, "emne", "{navn} – {kursnavn}")
    _lagre(con, "tekst", "Hei {navn}, du venter på {kursnavn}.")
    _kjor(con, pid)
    (til, emne, html), = _mine(sendt)
    assert emne == "Ola Nordmann – Veiledning i praksis"
    assert "<p>Hei Ola Nordmann, du venter på <strong>Veiledning i praksis</strong>.</p>" in _n(html)


def test_G_placeholderverdi_med_spesialtegn_escapes_i_html_og_er_ren_tekst_i_emne(con, sendt):
    kid, pid = _flyt(con, kursnavn="Kurs A & B <x>", navn="<i>Ola</i> & Co")
    _lagre(con, "emne", "Venteliste: {kursnavn} for {navn}")
    _lagre(con, "tekst", "Hei {navn}, {kursnavn}")
    _kjor(con, pid)
    (til, emne, html), = _mine(sendt)
    assert emne == "Venteliste: Kurs A & B <x> for <i>Ola</i> & Co"          # ren tekst i emnet
    h = _n(html)
    assert "<p>Hei &lt;i&gt;Ola&lt;/i&gt; &amp; Co, <strong>Kurs A &amp; B &lt;x&gt;</strong></p>" in h
    assert "<x>" not in html and "<i>" not in html


def test_G2_standard_uten_override_med_spesialtegn_er_som_i_12a(con, sendt):
    kid, pid = _flyt(con, kursnavn="Kurs A & B <x>")
    _kjor(con, pid)
    (til, emne, html), = _mine(sendt)
    assert emne == "Venteliste: Kurs A & B <x>"
    assert "<strong>Kurs A &amp; B &lt;x&gt;</strong> er dessverre fullt" in _n(html)


def test_malfilen_bruker_ikke_safe_og_plain_str_i_maltekst_escapes_av_jinja():
    """Forsvar i dybden: selv om noen ga malen en VANLIG streng (ikke Markup), ville Jinja escapet den."""
    from pathlib import Path
    kilde = (Path(maltekster.__file__).resolve().parent / "maler" / "epost" / "venteliste.html").read_text(encoding="utf-8")
    assert "safe" not in kilde and "{{ maltekst.tekst }}" in kilde and "{{ maltekst.emne }}" in kilde
    _, html = epost.render("venteliste", maltekst={"emne": "E", "tekst": "<b>x</b>"}, **_direkte_data())
    assert "&lt;b&gt;x&lt;/b&gt;" in html and "<b>x</b>" not in html
    _, html2 = epost.render("venteliste", maltekst={"emne": "E", "tekst": Markup("<p>system</p>")}, **_direkte_data())
    assert "<p>system</p>" in html2                                       # kun ferdig rendret Markup slipper gjennom


def test_kontrolltegn_i_kursnavn_gir_aldri_linjeskift_i_emnet(con, sendt):
    kid, pid = _flyt(con, kursnavn="Kurs" + chr(13) + chr(10) + "Bcc: ond@x.no")
    _lagre(con, "emne", "Venteliste: {kursnavn}")
    _kjor(con, pid)
    (til, emne, html), = _mine(sendt)
    assert emne == "Venteliste: Kurs Bcc: ond@x.no" and chr(10) not in emne and chr(13) not in emne


# ============================ direkte epost.render: standard, aldri DB ============================

def test_direkte_epost_render_gir_standardtekst_selv_naar_db_har_override(con):
    """epost.render leser ALDRI DB. Override virker kun via Kjoring/maltekster med connection."""
    _lagre(con, "emne", "OVERRIDE")
    emne, html = epost.render("venteliste", **_direkte_data())
    assert emne == STD_EMNE and "OVERRIDE" not in html


def test_epost_render_med_eksplisitt_maltekst_bruker_den():
    emne, html = epost.render("venteliste", maltekst={"emne": "Eget", "tekst": Markup("<p>Egen</p>")}, **_direkte_data())
    assert emne == "Eget" and "<p>Egen</p>" in html


def test_epost_py_importerer_ikke_db_og_gjor_ingen_override_oppslag():
    tre = ast.parse(inspect.getsource(epost))
    importert = {a.name for n in ast.walk(tre) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert "db" not in importert and "hent_overstyringer" not in inspect.getsource(epost)
    assert "maltekst_for_utsending" not in inspect.getsource(epost)      # oppslaget skjer i Kjoring/maltekster


# ============================ kun venteliste er aktivert ============================

def test_kun_venteliste_er_aktiv_og_verdibyggerne_matcher():
    assert maltekster.AKTIVE_MALER == frozenset({"venteliste"})
    assert set(maltekster._VERDIER) == set(maltekster.AKTIVE_MALER) <= set(MALER)


@pytest.mark.parametrize("mal", [m for m in MALER if m != "venteliste"])
def test_ovrige_syv_maler_kan_ikke_lagres_saa_override_aldri_blir_stille_ignorert(con, mal):
    felt = next(iter(MALER[mal].felt))
    with pytest.raises(MalFeil) as e:
        maltekster.lagre_maltekst(con, mal, felt, "Egen tekst")
    assert e.value.grunn == IKKE_AKTIVERT and e.value.mal == mal
    assert con.execute("SELECT COUNT(*) FROM mal_tekst").fetchone()[0] == 0


@pytest.mark.parametrize("mal", [m for m in MALER if m != "venteliste"] + ["innlogging", "eskalering", "admin_melding"])
def test_ovrige_og_laaste_maler_bruker_gammel_rendering_uten_db_lesing(con, monkeypatch, mal):
    def ikke_les(*a, **kw):
        raise AssertionError("DB skal ikke leses for maler som ikke er koblet")
    monkeypatch.setattr(db, "hent_overstyringer_for_mal", ikke_les)
    assert maltekster.maltekst_for_utsending(con, mal, {}) is None


def test_bekreftelse_sendes_uendret_via_sveiper_uavhengig_av_maltekst_systemet(con, sendt, monkeypatch):
    """En (manuelt innsatt) rad for bekreftelse paavirker ingenting - og DB leses ikke for den."""
    con.execute("INSERT INTO mal_tekst (mal, felt, tekst) VALUES ('bekreftelse', 'emne', 'IGNORERES {ukjent}')")
    kid = db.opprett_kurs(con, kode="B1", navn="Veiledning i praksis", datoer=["2027-03-01"], sharepoint_mappe="K/B1", pris_nok=0)
    pid, _ = db.meld_paa(con, kid, epost="ola@x.no", navn="Ola Nordmann")
    con.commit()
    monkeypatch.setattr(db, "hent_overstyringer_for_mal", lambda *a, **k: (_ for _ in ()).throw(AssertionError("skal ikke leses")))
    _kjor(con, pid)
    (til, emne, html), = _mine(sendt)
    assert emne == "Bekreftelse: Veiledning i praksis" and "Takk for påmeldingen!" in _n(html)


# ============================ feil FOER claim ============================

def _ingen_claim(con, monkeypatch):
    """Sikrer at reserver_* aldri kalles (bevis for at MalFeil skjer FOER claim)."""
    def forbudt(*a, **kw):
        raise AssertionError("claim ble forsokt selv om malteksten er ugyldig")
    monkeypatch.setattr(db, "reserver_sending", forbudt)
    monkeypatch.setattr(db, "reserver_sending_pa_nytt", forbudt)


def _ingen_spor(con, sendt, pid=None):
    assert sendt == []                                                                   # 0 epost.send
    assert con.execute("SELECT COUNT(*) FROM utsending_logg").fetchone()[0] == 0         # 0 claims
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling IN ('epost_ukjent','mal_feil')").fetchone()[0] == 0


def _korrupt_ugyldig_kode(con):
    con.execute("INSERT INTO mal_tekst (mal, felt, tekst) VALUES ('venteliste', 'tekst', 'Hei {ukjent}')")
    con.commit()


def _korrupt_ukjent_felt(con):
    con.execute("INSERT INTO mal_tekst (mal, felt, tekst) VALUES ('venteliste', 'finnes_ikke', 'Noe')")
    con.commit()


def _korrupt_tom_hovedtekst(con):
    con.execute("INSERT INTO mal_tekst (mal, felt, tekst) VALUES ('venteliste', 'tekst', '   ')")
    con.commit()


def _db_lesefeil(con):
    con.execute("DROP TABLE mal_tekst")
    con.commit()


@pytest.mark.parametrize("skade,grunn", [(_korrupt_ugyldig_kode, UKJENT_KODE), (_korrupt_ukjent_felt, UKJENT_FELT),
                                         (_korrupt_tom_hovedtekst, TOM), (_db_lesefeil, DB_LESEFEIL)])
def test_ugyldig_override_ukjent_felt_og_db_feil_gir_malfeil_foer_claim_uten_send_og_uten_fallback(con, sendt, monkeypatch, skade, grunn):
    kid, pid = _flyt(con)
    skade(con)
    _ingen_claim(con, monkeypatch)
    with pytest.raises(MalFeil) as e:
        _kall_send_en_gang(con, kid, pid)
    assert e.value.grunn == grunn and e.value.mal == "venteliste"
    _ingen_spor(con, sendt)                                                              # ingen stille fallback til standard


@pytest.mark.parametrize("skade", [_korrupt_ugyldig_kode, _korrupt_ukjent_felt, _db_lesefeil])
def test_feil_via_sveiper_gir_sveip_feil_uten_claim_send_eller_ukjent_status(con, sendt, skade):
    kid, pid = _flyt(con)
    skade(con)
    _kjor(con, pid)
    assert _mine(sendt) == []
    assert con.execute("SELECT COUNT(*) FROM utsending_logg WHERE mottaker='ola@x.no'").fetchone()[0] == 0
    assert con.execute("SELECT sveiper_kjort FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == 0
    feil = [json.loads(r["detaljer"]) for r in con.execute("SELECT detaljer FROM hendelse WHERE handling='sveip_feil'")]
    assert feil == [{"paamelding_id": pid, "feil": "MalFeil"}]                             # kun typenavn (sikker feiltekst)


def test_mal_feil_hendelse_er_bevisst_ikke_persistert_i_12b2a(con, sendt):
    kid, pid = _flyt(con)
    _korrupt_ugyldig_kode(con)
    _kjor(con, pid)
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='mal_feil'").fetchone()[0] == 0


def test_malfeil_ruller_verken_tilbake_eller_committer_callerens_ucommitterte_arbeid(con, sendt, monkeypatch):
    """Audit: callere (sveiper-loekke, daglig, kursbevis, meld_av) kan ha ucommittede skrivinger ved inngang. En MalFeil
    foer claim skal ikke committe dem (claim-committen gjor det ved suksess) og ikke rulle dem tilbake."""
    kid, pid = _flyt(con)
    _korrupt_ugyldig_kode(con)
    _ingen_claim(con, monkeypatch)
    con.execute("UPDATE paamelding SET intern_kommentar='UCOMMITTET' WHERE id=?", (pid,))    # callerens pending arbeid
    assert con.in_transaction
    with pytest.raises(MalFeil):
        _kall_send_en_gang(con, kid, pid)
    assert con.in_transaction                                                              # ikke committet, ikke rullet tilbake
    assert con.execute("SELECT intern_kommentar FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == "UCOMMITTET"
    annen = db.koble()
    assert annen.execute("SELECT intern_kommentar FROM paamelding WHERE id=?", (pid,)).fetchone()[0] != "UCOMMITTET"
    annen.close()
    con.rollback()


def test_gyldig_utsending_committer_fortsatt_callerens_pending_arbeid_via_claim_som_for(con, sendt):
    """Uendret oppforsel: ved vellykket render committer claim (og resultat) alt - som i 12A."""
    kid, pid = _flyt(con)
    con.execute("UPDATE paamelding SET intern_kommentar='PENDING' WHERE id=?", (pid,))
    assert _kall_send_en_gang(con, kid, pid) is True
    assert not con.in_transaction
    annen = db.koble()
    assert annen.execute("SELECT intern_kommentar FROM paamelding WHERE id=?", (pid,)).fetchone()[0] == "PENDING"
    annen.close()


# ============================ H: claim og idempotens ============================

def test_H_dobbelt_send_en_gang_med_samme_claim_gir_kun_en_epost(con, sendt):
    kid, pid = _flyt(con)
    _lagre(con, "tekst", "Egen tekst for {navn}")
    assert _kall_send_en_gang(con, kid, pid) is True
    assert _kall_send_en_gang(con, kid, pid) is False                                     # claim er tatt/sendt
    assert len(_mine(sendt)) == 1
    assert con.execute("SELECT status FROM utsending_logg WHERE mottaker='ola@x.no'").fetchone()[0] == "sendt"


def test_H_sveiper_to_ganger_gir_kun_en_epost_ogsaa_med_override(con, sendt):
    kid, pid = _flyt(con)
    _lagre(con, "emne", "Eget emne")
    _kjor(con, pid)
    _kjor(con, pid)
    assert len(_mine(sendt)) == 1 and _mine(sendt)[0][1] == "Eget emne"


def test_ukjent_send_gir_ukjent_status_og_ingen_retry_ogsaa_med_override(con, monkeypatch):
    kid, pid = _flyt(con)
    _lagre(con, "tekst", "Egen tekst")
    kall = []
    monkeypatch.setattr(epost, "send", lambda *a, **kw: (kall.append(1), (_ for _ in ()).throw(RuntimeError("Graph nede")))[1])
    _kjor(con, pid)
    _kjor(con, pid)
    assert len(kall) == 1                                                                 # aldri automatisk retry
    assert con.execute("SELECT status FROM utsending_logg WHERE mottaker='ola@x.no'").fetchone()[0] == "ukjent"


def test_tor_modus_bruker_override_uten_claim_committ(con, sendt):
    kid, pid = _flyt(con)
    _lagre(con, "emne", "Toer-emne")
    k = Kjoring(con, idag=IDAG, tor=True)
    p = con.execute(sveiper.SQL_DELTAKER + " WHERE p.id=?", (pid,)).fetchone()
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    assert k.send_en_gang(f"kurs:{kid}", "ola@x.no", "venteliste", "venteliste", p=p, kurs=kurs)
    assert any("Toer-emne" in linje for linje in k.utskrift) and sendt == []
    k.avslutt()
    assert con.execute("SELECT COUNT(*) FROM utsending_logg").fetchone()[0] == 0          # tor ruller alt tilbake
