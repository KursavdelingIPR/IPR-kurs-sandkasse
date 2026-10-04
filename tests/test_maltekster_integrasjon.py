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
from navnehjelp import navnedeler

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
    db.meld_paa(con, kid, epost="forst@x.no", fornavn="Forst", etternavn="Test")
    pid, status = db.meld_paa(con, kid, epost="ola@x.no", **navnedeler(navn))
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
    return dict(p={"navn": navn, "fornavn": navnedeler(navn)["fornavn"]}, kurs={"navn": kursnavn, "kursnr": 1001})


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
    assert "<p>Hei Ola,</p>" in h                                                  # standardhilsenen bruker fornavnet
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


# ============================ alle 8 redigerbare maler er aktivert (12B2C-5) ============================

# AKTIVE_MALER er en EKSPLISITT liste i maltekster.py (IKKE frozenset(MALER)) - fail-closed: en fremtidig ny mal i
# MALER-registeret blir ALDRI automatisk aktivert av seg selv. Denne konstanten er derfor ogsaa eksplisitt her,
# og testen under sammenligner den mot frozenset(MALER) for AA FANGE akkurat det scenariet (noen legger en ny mal
# i registeret uten aa huske aa legge den til i AKTIVE_MALER og gi den en verdibygger).
AKTIVE = frozenset({"venteliste", "avlysning", "bekreftelse", "ukefor", "dagfor", "kursbevis_klar",
                    "firmapaamelding_kvittering", "purring"})
IKKE_AKTIVE: list[str] = []


def test_alle_8_redigerbare_maler_er_aktive_og_verdibyggerne_matcher():
    assert maltekster.AKTIVE_MALER == AKTIVE == frozenset(MALER)           # i dag: identisk innhold, men IKKE utledet
    assert set(maltekster._VERDIER) == set(maltekster.AKTIVE_MALER) == set(MALER)
    assert IKKE_AKTIVE == []


def test_ingen_redigerbar_mal_kan_lenger_vaere_registrert_men_inaktiv():
    """Vakt mot fremtidig drift: siden AKTIVE_MALER er en eksplisitt liste (fail-closed), oppdager testen over det
    AKTIVE_MALER == frozenset(MALER) ikke lenger stemmer dersom noen legger en ny mal i MALER uten aa aktivere den.
    Denne testen sikrer i tillegg at hver mal som FAKTISK er aktiv, ogsaa har en verdibygger."""
    assert set(MALER) == set(maltekster._VERDIER)


def test_ny_uaktivert_mal_i_registeret_blir_ikke_automatisk_aktiv_og_kan_ikke_lagres(con, monkeypatch):
    """Simulerer selve fail-closed-scenariet: en 9. mal legges i MALER, men (bevisst) IKKE i AKTIVE_MALER eller
    _VERDIER. Den skal forbli inaktiv (IKKE bli med av seg selv fordi den star i MALER), og lagre_maltekst() skal
    fortsatt avvise den - akkurat som for enhver annen ikke-koblet mal."""
    ny_mal = maltekster.Mal("Ny fremtidig mal", "Test.", {"tekst": maltekster.MALER["venteliste"].felt["tekst"]})
    monkeypatch.setitem(maltekster.MALER, "fremtidig_ny_mal", ny_mal)
    assert "fremtidig_ny_mal" not in maltekster.AKTIVE_MALER               # IKKE automatisk aktivert
    with pytest.raises(MalFeil) as e:
        maltekster.lagre_maltekst(con, "fremtidig_ny_mal", "tekst", "Egen tekst")
    assert e.value.grunn == IKKE_AKTIVERT and e.value.mal == "fremtidig_ny_mal"
    assert con.execute("SELECT COUNT(*) FROM mal_tekst").fetchone()[0] == 0


@pytest.mark.parametrize("mal", ["innlogging", "eskalering", "admin_melding"])
def test_laaste_maler_bruker_gammel_rendering_uten_db_lesing(con, monkeypatch, mal):
    def ikke_les(*a, **kw):
        raise AssertionError("DB skal ikke leses for maler som ikke er koblet")
    monkeypatch.setattr(db, "hent_overstyringer_for_mal", ikke_les)
    assert maltekster.maltekst_for_utsending(con, mal, {}) is None


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


# ====================================================================================
# FASE 12B2B: avlysning koblet til maltekstsystemet (ende-til-ende via den ekte admin-ruten)
# ====================================================================================

STD_AVL_EMNE = "Avlyst: Veiledning i praksis"


def _admin():
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def _avl_flyt(con, kursnavn="Veiledning i praksis", navn="Ola Nordmann", vent_navn="Vera Venter"):
    """Kurs med én bekreftet (ola) og én paa venteliste (vera). Returnerer (kurs_id, ola_pid, vera_pid)."""
    kid = db.opprett_kurs(con, kode="AV1", navn=kursnavn, datoer=["2027-03-01"], sharepoint_mappe="Kurs/AV1",
                          pris_nok=0, kapasitet=1)
    pid, st1 = db.meld_paa(con, kid, epost="ola@x.no", **navnedeler(navn))
    vpid, st2 = db.meld_paa(con, kid, epost="vera@x.no", **navnedeler(vent_navn))
    assert (st1, st2) == ("bekreftet", "venteliste")
    con.commit()
    return kid, pid, vpid


def _avlys(kid):
    return _admin().post(f"/admin/kurs/{kid}/avlys")


def _lagre_avl(con, felt, tekst):
    maltekster.lagre_maltekst(con, "avlysning", felt, tekst, aktor="admin:test")
    con.commit()


def _avl_direkte_data():
    return dict(d={"navn": "Ola Nordmann", "fornavn": "Ola"}, kurs={"navn": "Veiledning i praksis", "kursnr": 1001})


def _kall_avlysning_direkte(con, kid, pid):
    d = con.execute("""SELECT p.id, d.navn, d.fornavn, d.epost FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id
                       WHERE p.id=?""", (pid,)).fetchone()
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    return Kjoring(con, idag=IDAG).send_en_gang(f"kurs:{kid}", d["epost"], "avlysning", "avlysning",
                                                paamelding_id=pid, d=d, kurs=kurs)


# ---------- verdibygger ----------

def test_avlysning_verdibygger_eksponerer_kun_navn_og_kursnavn():
    verdier = maltekster._avlysning_verdier({"d": {"navn": "Ola Nordmann", "fornavn": "Ola", "epost": "ola@x.no",
                                                   "telefon": "9"},
                                             "kurs": {"navn": "K", "pris_nok": 1500, "zoom_pw": "hemmelig"}})
    assert verdier == {"fornavn": "Ola", "navn": "Ola Nordmann", "kursnavn": "K"}
    # alle ikke-system-koder registeret tillater for avlysning er dekket av byggeren, og ingen andre finnes
    tillatt = set().union(*(f.kode for f in MALER["avlysning"].felt.values())) - maltekster.SYSTEMKODER
    assert tillatt == set(verdier)


def test_avlysning_verdibygger_gir_malfeil_naar_data_mangler():
    with pytest.raises(MalFeil) as e:
        maltekster.standard_maltekst("avlysning", {"kurs": {"navn": "K"}})
    assert e.value.grunn == maltekster.MANGLER_VERDI


# ---------- A: standard, faktiske statusendringer, transaksjon ----------

def test_A_uten_override_er_avlysning_identisk_med_dagens_og_statusendringene_er_korrekte(con, sendt):
    kid, pid, vpid = _avl_flyt(con)
    klient = _admin()
    resp = klient.post(f"/admin/kurs/{kid}/avlys")
    assert resp.status_code == 302
    assert "Kurset er avlyst og 2 deltaker(e) er varslet" in klient.get(resp.headers["Location"]).get_data(as_text=True)

    (til, emne, html), = _mine(sendt)
    assert emne == STD_AVL_EMNE
    assert _n(html) == _n(epost.render("avlysning", **_avl_direkte_data())[1])            # samme som direkte standard-render
    h = _n(html)
    assert "<p>Hei Ola,</p>" in h                                                           # standardhilsenen: fornavn
    assert "Vi må dessverre informere om at <strong>Veiledning i praksis</strong> er avlyst." in h
    assert "Har du allerede mottatt faktura, tar kursadministrasjonen kontakt med deg om det videre." in h
    assert "Har du spørsmål, kan du svare på denne e-posten." in h and "Vi beklager ulempen dette medfører." in h
    assert "Spør oss" not in h and "/sporsmal" not in h                                     # «Spør oss» er av som standard
    assert "Vennlig hilsen<br>Kursadministrasjonen, Institutt for Psykologisk Rådgivning<br>" in h
    (vtil, vemne, vhtml), = [m for m in sendt if m[0] == "vera@x.no"]
    assert vemne == STD_AVL_EMNE and "<p>Hei Vera,</p>" in _n(vhtml)                       # egen hilsen til hver mottaker

    fersk = db.koble()                                                                      # ny forbindelse = kun committet
    assert fersk.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "avlyst"
    assert sorted(r[0] for r in fersk.execute("SELECT status FROM paamelding WHERE kurs_id=?", (kid,))) == ["bekreftet", "venteliste"]
    assert fersk.execute("SELECT COUNT(*) FROM hendelse WHERE handling='kurs_avlyst'").fetchone()[0] == 1
    assert [tuple(r) for r in fersk.execute(
        "SELECT mottaker, type, status FROM utsending_logg ORDER BY mottaker")] == [
        ("ola@x.no", "avlysning", "sendt"), ("vera@x.no", "avlysning", "sendt")]
    fersk.close()


def test_avlysning_har_aldri_aapen_transaksjon_ved_inngang_til_send_en_gang_og_status_er_committet_foer_sending(con, sendt, monkeypatch):
    kid, pid, vpid = _avl_flyt(con)
    observert = []
    ekte = Kjoring.send_ferdigrendret_en_gang          # selve claim-motoren (avlysning gaar via send_til_mange)

    def spion(self, nokkel, til, type_, emne, html, **kw):
        annen = db.koble()
        observert.append((type_, self.con.in_transaction, annen.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()[0]))
        annen.close()
        return ekte(self, nokkel, til, type_, emne, html, **kw)
    monkeypatch.setattr(Kjoring, "send_ferdigrendret_en_gang", spion)
    _avlys(kid)
    assert observert == [("avlysning", False, "avlyst")] * 2      # ingen aapen transaksjon; avlysningen er allerede committet


# ---------- B-D: override gjennom den faktiske avlysningsflyten ----------

def test_B_override_kun_emne_gir_nytt_emne_og_standard_body(con, sendt):
    kid, pid, vpid = _avl_flyt(con)
    standard = _n(epost.render("avlysning", **_avl_direkte_data())[1])
    _lagre_avl(con, "emne", "Viktig: {kursnavn} er stilt inn")
    _avlys(kid)
    (til, emne, html), = _mine(sendt)
    assert emne == "Viktig: Veiledning i praksis er stilt inn" and _n(html) == standard


def test_C_override_kun_tekst_gir_standard_emne_og_ny_body(con, sendt):
    kid, pid, vpid = _avl_flyt(con)
    _lagre_avl(con, "tekst", "Hei {navn}," + chr(10) + chr(10) + "Vi må stille inn {kursnavn}." + chr(10) + "Beklager!")
    _avlys(kid)
    (til, emne, html), = _mine(sendt)
    assert emne == STD_AVL_EMNE
    h = _n(html)
    assert "<p>Hei Ola Nordmann,</p> <p>Vi må stille inn <strong>Veiledning i praksis</strong>.<br> Beklager!</p>" in h
    assert "er avlyst" not in h and "Vennlig hilsen" in h                                   # ny tekst, samme systemramme


def test_D_override_begge_felt_brukes_for_alle_mottakere(con, sendt):
    kid, pid, vpid = _avl_flyt(con)
    _lagre_avl(con, "emne", "Stilt inn")
    _lagre_avl(con, "tekst", "Kurset er stilt inn.")
    _avlys(kid)
    for adresse in ("ola@x.no", "vera@x.no"):
        (til, emne, html), = [m for m in sendt if m[0] == adresse]
        assert emne == "Stilt inn" and "<p>Kurset er stilt inn.</p>" in _n(html)


# ---------- E-H: sikkerhet og verdier ----------

def test_E_html_fra_admin_vises_escapet_og_er_aldri_aktivt(con, sendt):
    kid, pid, vpid = _avl_flyt(con)
    _lagre_avl(con, "tekst", "<script>alert(1)</script>" + chr(10) + "<b>tekst</b> <a href='http://ond.no'>klikk</a>")
    _avlys(kid)
    (til, emne, html), = _mine(sendt)
    assert "<script>" not in html and "<b>" not in html and "<a href='http://ond.no'>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "&lt;b&gt;tekst&lt;/b&gt;" in html


def test_F_placeholders_navn_og_kursnavn_gir_riktige_verdier_per_mottaker(con, sendt):
    kid, pid, vpid = _avl_flyt(con)
    _lagre_avl(con, "emne", "{navn}: {kursnavn} avlyst")
    _lagre_avl(con, "tekst", "Kjære {navn}, {kursnavn} går ikke.")
    _avlys(kid)
    (til, emne, html), = [m for m in sendt if m[0] == "vera@x.no"]
    assert emne == "Vera Venter: Veiledning i praksis avlyst"
    assert "<p>Kjære Vera Venter, <strong>Veiledning i praksis</strong> går ikke.</p>" in _n(html)


def test_G_kursnavn_med_spesialtegn_er_escapet_i_html_og_ren_tekst_i_emne(con, sendt):
    kid, pid, vpid = _avl_flyt(con, kursnavn="Kurs A & B <x>", navn="<i>Ola</i> & Co")
    _lagre_avl(con, "emne", "Avlyst: {kursnavn} for {navn}")
    _lagre_avl(con, "tekst", "Hei {navn}, {kursnavn}")
    _avlys(kid)
    (til, emne, html), = _mine(sendt)
    assert emne == "Avlyst: Kurs A & B <x> for <i>Ola</i> & Co"
    assert "<p>Hei &lt;i&gt;Ola&lt;/i&gt; &amp; Co, <strong>Kurs A &amp; B &lt;x&gt;</strong></p>" in _n(html)
    assert "<x>" not in html and "<i>" not in html


def test_G2_standard_uten_override_med_spesialtegn_er_som_i_12a(con, sendt):
    kid, pid, vpid = _avl_flyt(con, kursnavn="Kurs A & B <x>")
    _avlys(kid)
    (til, emne, html), = _mine(sendt)
    assert emne == "Avlyst: Kurs A & B <x>"
    assert "<strong>Kurs A &amp; B &lt;x&gt;</strong> er avlyst." in _n(html)


def test_H_sporsmal_url_og_min_side_er_systemets_adresser_aldri_frie_url_fra_admin(con, sendt, monkeypatch):
    monkeypatch.setattr(config, "ASSISTENT_AKTIV", True)
    kid, pid, vpid = _avl_flyt(con)
    _lagre_avl(con, "tekst", "Spør oss: {sporsmal_url}" + chr(10) + "Se {min_side}. Ond: http://ond.no/sporsmal og <a href='http://ond.no'>x</a>")
    _avlys(kid)
    (til, emne, html), = _mine(sendt)
    assert f"Spør oss: {config.BASE_URL}/sporsmal" in html                                  # systemets adresse, ren tekst
    assert f'<a href="{config.BASE_URL}/min-side">Mine kurs</a>' in html                    # systembygd lenke
    assert html.count("<a href") == 2                                                       # rammens Mine kurs + {min_side} - ingen andre
    assert "http://ond.no/sporsmal" in html and 'href="http://ond.no' not in html           # fri URL er kun tekst, aldri lenke
    assert "&lt;a href=&#39;http://ond.no&#39;&gt;x&lt;/a&gt;" in html


# ---------- direkte epost.render ----------

def test_direkte_epost_render_for_avlysning_gir_standard_selv_med_override_i_db(con):
    _lagre_avl(con, "emne", "OVERRIDE")
    _lagre_avl(con, "tekst", "OVERRIDE-TEKST")
    emne, html = epost.render("avlysning", **_avl_direkte_data())
    assert emne == STD_AVL_EMNE and "OVERRIDE" not in html and "er avlyst." in html


def test_avlysning_html_bruker_maltekst_og_ingen_safe():
    from pathlib import Path
    kilde = (Path(maltekster.__file__).resolve().parent / "maler" / "epost" / "avlysning.html").read_text(encoding="utf-8")
    assert "safe" not in kilde and "{{ maltekst.tekst }}" in kilde and "{{ maltekst.emne }}" in kilde
    assert "Vi må dessverre" not in kilde                                                   # ingen hardkodet brukertekst lenger
    _, html = epost.render("avlysning", maltekst={"emne": "E", "tekst": "<b>x</b>"}, **_avl_direkte_data())
    assert "&lt;b&gt;x&lt;/b&gt;" in html and "<b>x</b>" not in html


# ---------- feil FOER claim (avlysning) ----------

def _avl_ugyldig_override(con):
    con.execute("INSERT INTO mal_tekst (mal, felt, tekst) VALUES ('avlysning', 'tekst', 'Hei {ukjent}')")
    con.commit()


def _avl_ukjent_felt(con):
    con.execute("INSERT INTO mal_tekst (mal, felt, tekst) VALUES ('avlysning', 'finnes_ikke', 'Noe')")
    con.commit()


def _avl_db_lesefeil(con):
    con.execute("DROP TABLE mal_tekst")
    con.commit()


@pytest.mark.parametrize("skade,grunn", [(_avl_ugyldig_override, UKJENT_KODE), (_avl_ukjent_felt, UKJENT_FELT),
                                         (_avl_db_lesefeil, DB_LESEFEIL)])
def test_avlysning_malfeil_foer_claim_uten_send_claim_ukjent_status_eller_fallback(con, sendt, monkeypatch, skade, grunn):
    kid, pid, vpid = _avl_flyt(con)
    skade(con)
    _ingen_claim(con, monkeypatch)
    with pytest.raises(MalFeil) as e:
        _kall_avlysning_direkte(con, kid, pid)
    assert e.value.grunn == grunn and e.value.mal == "avlysning"
    _ingen_spor(con, sendt)


AVLYS_FEILMELDING = ("Kurset ble ikke avlyst fordi avlysningsmeldingen ikke kunne klargjøres. "
                     "Kontroller e-postmalen og prøv igjen.")


def _flasher(klient, resp) -> list:
    import re
    side = klient.get(resp.headers["Location"]).get_data(as_text=True)
    return [t.strip() for t in re.findall(r'class="flash[^"]*"[^>]*>\s*([^<]+)', side)]


@pytest.mark.parametrize("skade", [_avl_ugyldig_override, _avl_ukjent_felt, _avl_db_lesefeil])
def test_avlysning_ved_malfeil_stopper_foer_statusendring_og_gir_trygg_melding_uten_500(con, sendt, skade):
    kid, pid, vpid = _avl_flyt(con)
    skade(con)
    status_for = con.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()[0]       # les - ikke hardkod
    deltakere_for = [tuple(r) for r in con.execute("SELECT id, status FROM paamelding WHERE kurs_id=? ORDER BY id", (kid,))]
    klient = _admin()
    hendelser_for = con.execute("SELECT COUNT(*) FROM hendelse").fetchone()[0]                 # etter innlogging
    resp = klient.post(f"/admin/kurs/{kid}/avlys")

    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/admin/kurs/{kid}/oppsett")   # redirect, IKKE 500
    flash_tekster = _flasher(klient, resp)
    assert AVLYS_FEILMELDING in flash_tekster
    for tekst in flash_tekster:                                                                 # trygg melding: ingen intern info/PII
        for forbudt in ("MalFeil", "ukjent_kode", "ukjent_felt", "db_lesefeil", "Hei {", "Ola", "Vera", "ola@x.no", "vera@x.no"):
            assert forbudt not in tekst, (forbudt, tekst)

    fersk = db.koble()
    assert fersk.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()[0] == status_for          # UENDRET
    assert [tuple(r) for r in fersk.execute("SELECT id, status FROM paamelding WHERE kurs_id=? ORDER BY id", (kid,))] == deltakere_for
    assert fersk.execute("SELECT COUNT(*) FROM hendelse WHERE handling='kurs_avlyst'").fetchone()[0] == 0
    assert fersk.execute("SELECT COUNT(*) FROM hendelse").fetchone()[0] == hendelser_for                   # ingen hendelser skrevet
    assert fersk.execute("SELECT COUNT(*) FROM utsending_logg").fetchone()[0] == 0                         # 0 claims
    assert fersk.execute("SELECT COUNT(*) FROM hendelse WHERE handling IN ('epost_ukjent','mal_feil')").fetchone()[0] == 0
    fersk.close()
    assert sendt == []                                                                                     # 0 epost.send


@pytest.mark.parametrize("rett", ["slett_overstyring", "erstatt_med_gyldig"])
def test_admin_kan_avlyse_paa_nytt_etter_at_den_ugyldige_malen_er_rettet(con, sendt, rett):
    kid, pid, vpid = _avl_flyt(con)
    status_for = con.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()[0]
    _avl_ugyldig_override(con)
    klient = _admin()

    forste = klient.post(f"/admin/kurs/{kid}/avlys")                                            # 1. forsok feiler trygt
    assert AVLYS_FEILMELDING in _flasher(klient, forste)
    fersk = db.koble()
    assert fersk.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()[0] == status_for
    assert fersk.execute("SELECT COUNT(*) FROM utsending_logg").fetchone()[0] == 0 and sendt == []
    fersk.close()

    if rett == "slett_overstyring":                                                              # 2. malen rettes
        con.execute("DELETE FROM mal_tekst")
        con.commit()
    else:
        _lagre_avl(con, "tekst", "Rettet tekst til {navn}.")

    andre = klient.post(f"/admin/kurs/{kid}/avlys")                                              # 3. nytt forsok lykkes
    assert "Kurset er avlyst og 2 deltaker(e) er varslet" in " ".join(_flasher(klient, andre))
    fersk = db.koble()
    assert fersk.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()[0] == "avlyst"
    assert sorted(m[0] for m in sendt) == ["ola@x.no", "vera@x.no"]                              # begge mottakerne fikk varsel
    assert [tuple(r) for r in fersk.execute("SELECT mottaker, type, status FROM utsending_logg ORDER BY mottaker")] == [
        ("ola@x.no", "avlysning", "sendt"), ("vera@x.no", "avlysning", "sendt")]                 # normale claims, ingen duplikater
    assert fersk.execute("SELECT COUNT(*) FROM hendelse WHERE handling='kurs_avlyst'").fetchone()[0] == 1
    fersk.close()
    if rett == "erstatt_med_gyldig":
        assert all("Rettet tekst til" in _n(m[2]) for m in sendt)

    klient.post(f"/admin/kurs/{kid}/avlys")                                                      # 4. ingen dobbel varsling
    assert len(sendt) == 2


def test_preflight_gjelder_alle_mottakere_ingen_statusendring_foer_hele_preflighten_er_gronn(con, sendt, monkeypatch):
    kid, pid, vpid = _avl_flyt(con)
    status_for = con.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()[0]
    ekte = Kjoring.render_for_sending
    rendret = []

    def render(self, mal, **data):
        rendret.append(data["d"]["epost"])
        if len(rendret) == 2:                                                                    # forste lykkes, SENERE mottaker feiler
            raise MalFeil(UKJENT_KODE, "avlysning", "tekst")
        return ekte(self, mal, **data)
    monkeypatch.setattr(Kjoring, "render_for_sending", render)
    avlys_kall = []
    ekte_avlys = db.avlys_kurs
    monkeypatch.setattr(db, "avlys_kurs", lambda *a, **k: (avlys_kall.append(1), ekte_avlys(*a, **k))[1])

    klient = _admin()
    resp = klient.post(f"/admin/kurs/{kid}/avlys")
    assert resp.status_code == 302 and AVLYS_FEILMELDING in _flasher(klient, resp)
    assert len(rendret) == 2 and avlys_kall == []                                                # db.avlys_kurs aldri kalt
    fersk = db.koble()
    assert fersk.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()[0] == status_for
    assert fersk.execute("SELECT COUNT(*) FROM utsending_logg").fetchone()[0] == 0               # 0 claims
    fersk.close()
    assert sendt == []                                                                           # heller ikke til FORSTE mottaker


def test_rekkefolge_alle_preflight_renders_foer_avlys_kurs_og_foer_forste_sending(con, monkeypatch):
    kid, pid, vpid = _avl_flyt(con)
    logg = []
    ekte_render, ekte_avlys = Kjoring.render_for_sending, db.avlys_kurs
    monkeypatch.setattr(Kjoring, "render_for_sending", lambda self, mal, **d: (logg.append("render"), ekte_render(self, mal, **d))[1])
    monkeypatch.setattr(db, "avlys_kurs", lambda *a, **k: (logg.append("avlys_kurs"), ekte_avlys(*a, **k))[1])
    monkeypatch.setattr(epost, "send", lambda *a, **k: logg.append("send"))
    _avlys(kid)
    # preflight (2 mottakere) -> avlys_kurs -> per mottaker: render (send_en_gang, foer claim) -> send
    assert logg == ["render", "render", "avlys_kurs", "render", "send", "render", "send"]


def test_preflight_committer_ikke_ruller_ikke_tilbake_og_skriver_ingenting(con, sendt, monkeypatch):
    kid, pid, vpid = _avl_flyt(con)
    observert = []
    ekte = Kjoring.render_for_sending

    def spion(self, mal, **data):
        for_ = (self.con.in_transaction, self.con.total_changes)
        ut = ekte(self, mal, **data)
        observert.append((for_, (self.con.in_transaction, self.con.total_changes)))
        return ut
    monkeypatch.setattr(Kjoring, "render_for_sending", spion)
    monkeypatch.setattr(db, "avlys_kurs", lambda *a, **k: (_ for _ in ()).throw(db.Paameldingsfeil("stopp etter preflight")))
    resp = _avlys(kid)                                                                            # ruten avslutter rett etter preflight
    assert resp.status_code == 302
    assert len(observert) == 2                                                                    # kun preflight (begge mottakere)
    for for_, etter in observert:
        assert for_ == etter and etter[0] is False                                                # ingen DML, ingen aapen transaksjon
    assert sendt == [] and con.execute("SELECT COUNT(*) FROM utsending_logg").fetchone()[0] == 0


def test_render_for_sending_har_ingen_sideeffekt_og_er_samme_sti_som_send_en_gang(con, sendt):
    kid, pid, vpid = _avl_flyt(con)
    _lagre_avl(con, "emne", "Eget: {kursnavn}")
    d = con.execute("SELECT p.id, d.navn, d.fornavn, d.epost FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE p.id=?",
                    (pid,)).fetchone()
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    k = Kjoring(con, idag=IDAG)
    endringer = con.total_changes
    emne, html = k.render_for_sending("avlysning", d=d, kurs=kurs)
    assert emne == "Eget: Veiledning i praksis" and "er avlyst" in html
    assert con.total_changes == endringer and not con.in_transaction and sendt == []
    assert con.execute("SELECT COUNT(*) FROM utsending_logg").fetchone()[0] == 0
    k.send_en_gang(f"kurs:{kid}", d["epost"], "avlysning", "avlysning", paamelding_id=pid, d=d, kurs=kurs)
    (til, sendt_emne, sendt_html), = sendt
    assert (sendt_emne, sendt_html) == (emne, html)                                               # nøyaktig samme output


def test_avlysning_av_allerede_avlyst_kurs_gir_fortsatt_allerede_avlyst_selv_med_ugyldig_mal(con, sendt):
    kid, pid, vpid = _avl_flyt(con)
    klient = _admin()
    klient.post(f"/admin/kurs/{kid}/avlys")
    sendt.clear()
    _avl_ugyldig_override(con)
    resp = klient.post(f"/admin/kurs/{kid}/avlys")
    tekster = _flasher(klient, resp)                                                              # flash konsumeres - les én gang
    assert "Kurset er allerede avlyst." in tekster and AVLYS_FEILMELDING not in tekster
    assert sendt == []


def test_avlysning_malfeil_ruller_verken_tilbake_eller_committer_ucommittet_arbeid(con, sendt, monkeypatch):
    kid, pid, vpid = _avl_flyt(con)
    _avl_ugyldig_override(con)
    _ingen_claim(con, monkeypatch)
    con.execute("UPDATE paamelding SET intern_kommentar='UCOMMITTET' WHERE id=?", (pid,))
    with pytest.raises(MalFeil):
        _kall_avlysning_direkte(con, kid, pid)
    assert con.in_transaction
    annen = db.koble()
    assert annen.execute("SELECT intern_kommentar FROM paamelding WHERE id=?", (pid,)).fetchone()[0] != "UCOMMITTET"
    annen.close()
    con.rollback()


# ---------- isolasjon venteliste <-> avlysning ----------

def test_override_for_avlysning_paavirker_ikke_venteliste_og_omvendt(con, sendt):
    kid, pid, vpid = _avl_flyt(con)
    _lagre_avl(con, "emne", "AVLYSNINGS-EMNE")
    _lagre(con, "emne", "VENTELISTE-EMNE")                                                  # venteliste-override (12B2A-hjelper)
    _kjor(con, vpid)                                                                        # vera faar ventelistebeskjed (sveiper)
    _avlys(kid)                                                                             # og avlysning (rute)
    (vent,) = [m for m in sendt if m[0] == "vera@x.no" and m[1] == "VENTELISTE-EMNE"]
    (avl,) = [m for m in sendt if m[0] == "ola@x.no"]
    assert avl[1] == "AVLYSNINGS-EMNE" and "Venteliste" not in avl[1]
    assert "er dessverre fullt" in _n(vent[2])                                              # venteliste-tekst uberort av avlysning


def test_kun_avlysning_override_lar_venteliste_staa_med_standard_og_omvendt(con, sendt):
    kid, pid, vpid = _avl_flyt(con)
    _lagre_avl(con, "emne", "AVLYSNINGS-EMNE")
    _kjor(con, vpid)
    assert [m[1] for m in sendt if m[0] == "vera@x.no"] == [STD_EMNE]                       # venteliste: standard
    con.execute("DELETE FROM mal_tekst")
    _lagre(con, "emne", "VENTELISTE-EMNE")
    sendt.clear()
    _avlys(kid)
    assert sorted(m[1] for m in sendt) == [STD_AVL_EMNE, STD_AVL_EMNE]                      # avlysning: standard


def test_korrupt_rad_for_den_ene_malen_stopper_ikke_den_andre(con, sendt):
    kid, pid, vpid = _avl_flyt(con)
    con.execute("INSERT INTO mal_tekst (mal, felt, tekst) VALUES ('avlysning', 'tekst', 'Hei {ukjent}')")
    con.commit()
    _kjor(con, vpid)                                                                        # venteliste leser kun sine egne rader
    assert [m[0] for m in sendt] == ["vera@x.no"]
    con.execute("DELETE FROM mal_tekst")
    con.execute("INSERT INTO mal_tekst (mal, felt, tekst) VALUES ('venteliste', 'tekst', 'Hei {ukjent}')")
    con.commit()
    sendt.clear()
    _avlys(kid)                                                                             # avlysning leser kun sine egne rader
    assert sorted(m[0] for m in sendt) == ["ola@x.no", "vera@x.no"]


# ---------- idempotens ----------

def test_dobbelt_avlysning_sender_ikke_to_ganger(con, sendt):
    kid, pid, vpid = _avl_flyt(con)
    _lagre_avl(con, "tekst", "Egen tekst")
    _avlys(kid)
    _avlys(kid)                                                                             # kurset er allerede avlyst
    assert len(sendt) == 2                                                                  # kun de to fra forste avlysning
    assert _kall_avlysning_direkte(con, kid, pid) is False                                  # og claimen er tatt/sendt
    assert len(sendt) == 2
