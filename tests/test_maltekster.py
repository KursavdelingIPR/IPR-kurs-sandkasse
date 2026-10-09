"""Fase 12B1: fundament for redigerbare maltekster (kurs/maltekster.py + tabellen mal_tekst).

Testene gaar rett mot registeret, parseren, rendringsprimitivene og override-oppslaget. Fundamentet er IKKE koblet til den
ekte e-postmotoren ennaa (12B2) - 12A-gullstandarden er derfor uendret.
"""
import ast
import inspect
import json
import sqlite3

import pytest
from markupsafe import Markup

from kurs import config, db, maltekster
from kurs.maltekster import (DB_LESEFEIL, ENSLIG_KLAMME, FEIL_FELTTYPE, FOR_LANG, KONTROLLTEGN, KODER, MALER, MANGLER_VERDI,
                             TOM, UFERDIG_KLAMME, UGYLDIG_SYNTAKS, UKJENT_FELT, UKJENT_KODE, UKJENT_MAL, MalFeil)

BASE = config.BASE_URL


@pytest.fixture(autouse=True)
def _alle_maler_aktive(monkeypatch):
    """12B1-testene tester FUNDAMENTET generisk (alle 8 maler), dvs. slik det er naar alle er koblet til utsending (12B2B).
    I 12B2A er kun `venteliste` koblet - sperren mot ikke-koblede maler testes i test_maltekster_integrasjon.py."""
    monkeypatch.setattr(maltekster, "AKTIVE_MALER", frozenset(MALER))


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _feil(grunn, fn, *a, **kw) -> MalFeil:
    with pytest.raises(MalFeil) as e:
        fn(*a, **kw)
    assert e.value.grunn == grunn, (e.value.grunn, str(e.value))
    return e.value


def _rad(con, mal, felt, tekst):
    """Skriver en rad DIREKTE (forbi service-laget) - simulerer korrupt/manuelt endret data."""
    con.execute("INSERT INTO mal_tekst (mal, felt, tekst, oppdatert) VALUES (?,?,?,'2027-01-01T00:00:00') "
                "ON CONFLICT (mal, felt) DO UPDATE SET tekst=excluded.tekst, oppdatert=excluded.oppdatert",
                (mal, felt, tekst))
    con.commit()


def _hendelser(con):
    return [(r["handling"], json.loads(r["detaljer"]), r["aktor"]) for r in con.execute(
        "SELECT handling, detaljer, aktor FROM hendelse WHERE handling LIKE 'mal_%' ORDER BY id")]


# ============================ 1-3: registeret ============================

def test_registeret_har_noyaktig_de_8_godkjente_malene():
    # (06.10.2026: kursholder-lenken og SharePoint er tatt bort, og «purring» og «eskalering» med dem)
    assert sorted(MALER) == sorted(["bekreftelse", "venteliste", "ukefor", "dagfor", "avlysning", "kursbevis_klar",
                                    "firmapaamelding_kvittering", "evaluering"])


@pytest.mark.parametrize("last", ["innlogging", "admin_melding", "_ramme", "kursbevis"])
def test_laaste_maler_finnes_ikke_i_registeret_og_kan_ikke_lagres(con, last):
    assert last not in MALER
    _feil(UKJENT_MAL, maltekster.lagre_maltekst, con, last, "emne", "Hei")
    _feil(UKJENT_MAL, maltekster.hent_overstyringer, con, last)


def test_alle_felt_har_type_maks_og_tom_tillatt_og_menneskenavn():
    for mnavn, mal in MALER.items():
        assert mal.navn and mal.beskrivelse
        assert mal.felt, mnavn
        for fnavn, f in mal.felt.items():
            assert f.type in ("emne", "tekst"), (mnavn, fnavn)
            assert isinstance(f.maks, int) and f.maks > 0
            assert isinstance(f.tom_tillatt, bool)
            assert f.navn and f.kode <= set(KODER), (mnavn, fnavn)
            assert len(f.standard) <= f.maks


def test_tom_tillatt_regler_emne_og_hovedtekst_kan_ikke_vaere_tomme_avslutning_kan():
    for mnavn, mal in MALER.items():
        for fnavn, f in mal.felt.items():
            if f.type == "emne" or fnavn in ("tekst", "innledning") or fnavn.startswith("innledning_"):
                assert f.tom_tillatt is False, (mnavn, fnavn)
            if fnavn == "avslutning":
                assert f.tom_tillatt is True, (mnavn, fnavn)
    assert MALER["venteliste"].felt["tekst"].tom_tillatt is False
    assert MALER["avlysning"].felt["tekst"].tom_tillatt is False
    assert MALER["kursbevis_klar"].felt["tekst"].tom_tillatt is False


def test_ingen_koder_er_obligatoriske_og_alle_standardtekster_er_gyldige():
    """Ingen kode kreves: alle standardtekster valideres uendret, og en tekst UTEN koder er gyldig i alle ikke-tomme felt."""
    for mnavn, mal in MALER.items():
        for fnavn, f in mal.felt.items():
            assert maltekster.valider(mnavn, fnavn, f.standard) == f.standard
            if not f.tom_tillatt:
                assert maltekster.valider(mnavn, fnavn, "En tekst helt uten koder") == "En tekst helt uten koder"


def test_emnefelt_tillater_aldri_lenkekoder_og_kun_avlysning_har_sporsmal_url():
    for mnavn, mal in MALER.items():
        for fnavn, f in mal.felt.items():
            if f.type == "emne":
                assert not (f.kode & maltekster.SYSTEMKODER), (mnavn, fnavn)
            if "sporsmal_url" in f.kode:
                assert (mnavn, fnavn) == ("avlysning", "tekst")


def test_dagfor_har_tre_varianter_av_emne_og_innledning():
    assert set(MALER["dagfor"].felt) == {"emne_forste", "emne_midt", "emne_siste", "innledning_forste", "innledning_midt",
                                         "innledning_siste", "avslutning"}


# ============================ 4-8: mal/felt/tom ============================

def test_ukjent_mal_og_ukjent_felt_avvises(con):
    _feil(UKJENT_MAL, maltekster.valider, "finnes_ikke", "emne", "Hei")
    _feil(UKJENT_FELT, maltekster.valider, "bekreftelse", "finnes_ikke", "Hei")
    _feil(UKJENT_MAL, maltekster.standard_tekst, "finnes_ikke", "emne")
    _feil(UKJENT_FELT, maltekster.lagre_maltekst, con, "bekreftelse", "finnes_ikke", "Hei")
    assert con.execute("SELECT COUNT(*) FROM mal_tekst").fetchone()[0] == 0


@pytest.mark.parametrize("tom", ["", "   ", "  "])
def test_tomt_emne_avvises(tom):
    _feil(TOM, maltekster.valider, "bekreftelse", "emne", tom)


@pytest.mark.parametrize("mal,felt", [("venteliste", "tekst"), ("avlysning", "tekst"), ("kursbevis_klar", "tekst"),
                                      ("bekreftelse", "innledning"), ("dagfor", "innledning_midt")])
def test_tomt_hovedtekstfelt_avvises_naar_tom_tillatt_er_false(mal, felt):
    _feil(TOM, maltekster.valider, mal, felt, "")
    _feil(TOM, maltekster.valider, mal, felt, " " + chr(10) + " ")


def test_tom_avslutning_tillates_naar_tom_tillatt_er_true(con):
    assert maltekster.valider("bekreftelse", "avslutning", "") == ""
    assert maltekster.lagre_maltekst(con, "ukefor", "avslutning", "   ") == ""
    assert maltekster.effektiv_tekst(con, "ukefor", "avslutning") == ""       # bevisst tom overstyring, ikke fallback


def test_for_lang_tekst_avvises():
    f = MALER["bekreftelse"].felt
    _feil(FOR_LANG, maltekster.valider, "bekreftelse", "emne", "x" * (f["emne"].maks + 1))
    _feil(FOR_LANG, maltekster.valider, "bekreftelse", "avslutning", "x" * (f["avslutning"].maks + 1))
    assert maltekster.valider("bekreftelse", "emne", "x" * f["emne"].maks)


# ============================ 9-13: parser ============================

def test_gyldige_koder_aksepteres_og_bare_fra_feltets_whitelist():
    assert maltekster.valider("bekreftelse", "innledning", "Hei {navn}, velkommen til {kursnavn} som starter {startdato}")
    assert maltekster.valider("dagfor", "emne_midt", "Dag {dagnummer} av {antall_dager}: {kursnavn} ({dato})")


@pytest.mark.parametrize("mal,felt,tekst", [
    ("bekreftelse", "innledning", "Hei {ukjent}"),
    ("bekreftelse", "emne", "{min_side}"),              # lenkekoder ikke i emne
    ("venteliste", "tekst", "{frist}"),                  # gyldig kode, men ikke i DENNE malen
    ("bekreftelse", "innledning", "{sporsmal_url}"),     # kun avlysning
    ("ukefor", "emne", "{dagnummer}"),
])
def test_ukjent_eller_ikke_tillatt_kode_avvises(mal, felt, tekst):
    _feil(UKJENT_KODE, maltekster.valider, mal, felt, tekst)


@pytest.mark.parametrize("tekst", ["{navn.__class__}", "{navn[0]}", "{navn:20}", "{navn!r}", "{ navn }", "{}", "{Navn}",
                                   "{navn2}", "{navn.upper()}", "{0}", "{navn-1}", "{navn,kursnavn}"])
def test_attributt_index_format_og_conversion_syntaks_avvises(tekst):
    _feil(UGYLDIG_SYNTAKS, maltekster.valider, "bekreftelse", "innledning", "Hei " + tekst)


@pytest.mark.parametrize("tekst", ["{{ navn }}", "{{navn}}", "{% if x %}a{% endif %}", "{# kommentar #}", "{% raw %}",
                                   "{{ 7*7 }}", "{navn{kursnavn}}"])
def test_jinja_syntaks_avvises(tekst):
    feil = _feil(UGYLDIG_SYNTAKS, maltekster.valider, "bekreftelse", "innledning", tekst)
    assert feil.grunn == UGYLDIG_SYNTAKS


@pytest.mark.parametrize("tekst,grunn", [("Hei {navn", UFERDIG_KLAMME), ("{", UFERDIG_KLAMME), ("Hei navn}", ENSLIG_KLAMME),
                                         ("}", ENSLIG_KLAMME), ("{navn}}", ENSLIG_KLAMME), ("}{navn}", ENSLIG_KLAMME),
                                         ("{{navn", UFERDIG_KLAMME), ("a { b", UFERDIG_KLAMME)])
def test_enslig_og_ufullstendig_klamme_avvises(tekst, grunn):
    _feil(grunn, maltekster.valider, "bekreftelse", "innledning", tekst)


def test_kode_kan_ikke_spenne_over_linjeskift():
    _feil(UGYLDIG_SYNTAKS, maltekster.valider, "bekreftelse", "innledning", "Hei {navn" + chr(10) + "}")


def test_parseren_bruker_ikke_format_eval_exec_eller_jinja():
    """Kildekode-sjekk (AST): ingen str.format/format_map/eval/exec/Template/jinja paa admintekst, og ingen import av epost."""
    tre = ast.parse(inspect.getsource(maltekster))
    navn = {n.id for n in ast.walk(tre) if isinstance(n, ast.Name)}
    attr = {n.attr for n in ast.walk(tre) if isinstance(n, ast.Attribute)}
    assert not ({"eval", "exec", "compile", "__import__", "Template", "Environment"} & navn)
    assert "format_map" not in attr
    importert = {a.name for n in ast.walk(tre) if isinstance(n, ast.Import) for a in n.names}
    importert |= {(n.module or "") + "." + a.name for n in ast.walk(tre) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert not [i for i in importert if "jinja" in i.lower() or "epost" in i.lower() or "flask" in i.lower()], importert
    # .format brukes KUN paa Markup (markupsafe escaper argumentene) - aldri paa admintekst
    kilde = inspect.getsource(maltekster)
    format_kall = [n for n in ast.walk(tre) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "format"]
    assert len(format_kall) == 3 and all(isinstance(k.func.value, ast.Call) and getattr(k.func.value.func, "id", "") == "Markup" for k in format_kall)
    assert 'Markup(\'<a href' in kilde and 'Markup("<strong>{}</strong>").format' in kilde


# ============================ 14-19: rendring ============================

VERDIER = {"fornavn": "Ola", "navn": "Ola Nordmann", "kursnavn": "Veiledning i praksis", "startdato": "2027-03-01"}


def test_raa_html_i_admintekst_escapes():
    html = maltekster.felttekst_til_html("bekreftelse", "innledning", "Hei <script>alert(1)</script> & <b>fet</b>", VERDIER)
    assert "<script>" not in html and "<b>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;b&gt;fet&lt;/b&gt;" in html
    assert isinstance(html, Markup)


def test_placeholderverdi_escapes_og_kursnavn_er_fet():
    html = maltekster.felttekst_til_html("bekreftelse", "innledning", "Hei {navn}, kurs {kursnavn}",
                                         {"navn": "<b>Ola</b> & Co", "kursnavn": "Kurs A <x>", "startdato": "x"})
    assert "Hei &lt;b&gt;Ola&lt;/b&gt; &amp; Co, kurs <strong>Kurs A &lt;x&gt;</strong>" in html
    assert "<b>" not in html and "<x>" not in html


def test_verdier_kan_ikke_lage_avsnitt_linjeskift_eller_klammer_tolkning():
    html = maltekster.felttekst_til_html("bekreftelse", "innledning", "Hei {navn}.",
                                         {"navn": "Ola" + chr(13) + chr(10) + chr(10) + "<p>ond</p> {kursnavn}", "kursnavn": "K"})
    assert html.count("<p>") == 1 and "<br>" not in html and "&lt;p&gt;ond&lt;/p&gt; {kursnavn}" in html


def test_ren_tekst_med_linjeskift_gir_riktig_avsnitt_og_br():
    tekst = "Linje 1" + chr(10) + "Linje 2" + chr(10) + chr(10) + "Nytt avsnitt" + chr(10) + "  " + chr(10) + chr(10) + "Tredje"
    assert maltekster.ren_tekst_til_html(tekst) == Markup("<p>Linje 1<br>\nLinje 2</p>\n<p>Nytt avsnitt</p>\n<p>Tredje</p>")


def test_ren_tekst_normaliserer_crlf_cr_og_tab_og_er_tom_for_tom_tekst():
    assert maltekster.ren_tekst_til_html("A" + chr(13) + chr(10) + "B" + chr(13) + "C") == Markup("<p>A<br>\nB<br>\nC</p>")
    assert maltekster.ren_tekst_til_html("A" + chr(9) + "B") == Markup("<p>A B</p>")
    assert maltekster.ren_tekst_til_html("") == Markup("") and maltekster.ren_tekst_til_html("  " + chr(10) + " ") == Markup("")


def test_ren_tekst_evaluerer_aldri_klammer_og_tolker_aldri_html():
    ut = maltekster.ren_tekst_til_html("{navn} {{ 7*7 }} {% x %} <b>a</b> }")
    assert ut == Markup("<p>{navn} {{ 7*7 }} {% x %} &lt;b&gt;a&lt;/b&gt; }</p>")


@pytest.mark.parametrize("tegn", [0, 1, 7, 11, 12, 27, 127, 0x85, 0x2028, 0x2029])
def test_uonskede_kontrolltegn_avvises_i_ren_tekst_og_felt(tegn):
    _feil(KONTROLLTEGN, maltekster.ren_tekst_til_html, "Hei" + chr(tegn) + "du")
    _feil(KONTROLLTEGN, maltekster.valider, "bekreftelse", "innledning", "Hei" + chr(tegn) + "du")


def test_min_side_lenke_er_systembygd_og_sporsmal_url_er_ren_tekst(monkeypatch):
    monkeypatch.setattr(config, "ASSISTENT_AKTIV", True)
    html = maltekster.felttekst_til_html("bekreftelse", "avslutning", maltekster.standard_tekst("bekreftelse", "avslutning"),
                                         VERDIER)
    assert f'<a href="{BASE}/min-side">Mine kurs</a> med e-postadressen din.' in html           # uten personlig lenke: oversikten «Mine kurs»
    personlig = f"{BASE}/min/1.1.abcdef0123456789abcdef0123456789"
    html = maltekster.felttekst_til_html("bekreftelse", "avslutning", maltekster.standard_tekst("bekreftelse", "avslutning"),
                                         {**VERDIER, "min_side_url": personlig})
    assert f'<a href="{personlig}">Min side</a> med e-postadressen din.' in html                # med systembygd personlig lenke: hennes egen Min side
    ond = maltekster.felttekst_til_html("bekreftelse", "avslutning", maltekster.standard_tekst("bekreftelse", "avslutning"),
                                        {**VERDIER, "min_side_url": "http://ond.no/min/x"})
    assert "ond.no" not in ond and f'<a href="{BASE}/min-side">Mine kurs</a>' in ond           # en fremmed adresse godtas aldri
    standard = maltekster.felttekst_til_html("avlysning", "tekst", maltekster.standard_tekst("avlysning", "tekst"), VERDIER)
    assert "Har du spørsmål, kan du svare på denne e-posten." in standard
    assert "Spør oss" not in standard and "/sporsmal" not in standard            # standardteksten peker ikke lenger på «Spør oss»
    # koden {sporsmal_url} finnes fortsatt (lagrede overstyringer kan bruke den) og gir systemets adresse som REN tekst, aldri en lenke
    avl = maltekster.felttekst_til_html("avlysning", "tekst", "Spør oss: {sporsmal_url}", VERDIER)
    assert f"Spør oss: {BASE}/sporsmal" in avl and "<a href" not in avl
    monkeypatch.setattr(config, "ASSISTENT_AKTIV", False)                          # standard: siden finnes ikke, så koden gir forsiden (aldri en død adresse)
    av = maltekster.felttekst_til_html("avlysning", "tekst", "Spør oss: {sporsmal_url}", VERDIER)
    assert f"Spør oss: {BASE}" in av and "/sporsmal" not in av and "<a href" not in av


def test_verdi_som_mangler_gir_feil_ikke_stille_tom_tekst():
    _feil(MANGLER_VERDI, maltekster.felttekst_til_html, "bekreftelse", "innledning", "Hei {navn}", {"kursnavn": "K"})
    _feil(MANGLER_VERDI, maltekster.felttekst_til_emne, "bekreftelse", "emne", "{kursnavn}", {})


def test_emne_er_ren_tekst_uten_html_escaping():
    emne = maltekster.felttekst_til_emne("bekreftelse", "emne", "Bekreftelse: {kursnavn}", {"kursnavn": "Kurs A & B <x>"})
    assert emne == "Bekreftelse: Kurs A & B <x>" and not isinstance(emne, Markup)


@pytest.mark.parametrize("verdi", ["A" + chr(13) + chr(10) + "Bcc: ond@x.no", "A" + chr(10) + "Bcc: ond@x.no",
                                   "A" + chr(13) + "Bcc: ond@x.no", "A" + chr(0x2028) + "Bcc: ond@x.no",
                                   "A" + chr(0) + "Bcc: ond@x.no"])
def test_kontrolltegn_i_emneverdier_blir_mellomrom(verdi):
    emne = maltekster.felttekst_til_emne("bekreftelse", "emne", "Bekreftelse: {kursnavn}", {"kursnavn": verdi})
    assert emne == "Bekreftelse: A Bcc: ond@x.no"
    assert not any(t in emne for t in (chr(10), chr(13), chr(0), chr(0x2028)))


@pytest.mark.parametrize("tegn", [10, 13, 9, 0, 0x85, 0x2028])
def test_kontrolltegn_i_admin_emnetekst_avvises_ogsaa_tab(tegn):
    _feil(KONTROLLTEGN, maltekster.valider, "bekreftelse", "emne", "Hei" + chr(tegn) + "du")


def test_feil_felttype_avvises():
    _feil(FEIL_FELTTYPE, maltekster.felttekst_til_html, "bekreftelse", "emne", "x", VERDIER)
    _feil(FEIL_FELTTYPE, maltekster.felttekst_til_emne, "bekreftelse", "innledning", "x", VERDIER)


def test_standardtekstene_gir_dagens_setninger_fra_12a_naar_de_rendres():
    """Bro til 12B2: standardtekstene reproduserer setningene som er laast av 12A-gullstandarden."""
    v = {"fornavn": "Ola", "navn": "Ola Nordmann", "kursnavn": "Veiledning i praksis", "startdato": "2027-03-01",
         "dato": "2027-03-02",
         "dagnummer": "2", "antall_dager": "3", "firmanavn": "Firma AS", "antall_deltakere": "3 deltakere"}
    h = lambda m, f: " ".join(maltekster.felttekst_til_html(m, f, maltekster.standard_tekst(m, f), v).split())  # noqa: E731
    e = lambda m, f: maltekster.felttekst_til_emne(m, f, maltekster.standard_tekst(m, f), v)  # noqa: E731
    assert e("bekreftelse", "emne") == "Bekreftelse: Veiledning i praksis"
    assert h("bekreftelse", "innledning") == ("<p>Hei Ola,</p> <p>Takk for påmeldingen! Du har fått plass på "
                                              "<strong>Veiledning i praksis</strong>.</p>")          # hilsen med fornavn
    assert e("venteliste", "emne") == "Venteliste: Veiledning i praksis"
    assert "<strong>Veiledning i praksis</strong> er dessverre fullt, men du står nå på venteliste." in h("venteliste", "tekst")
    assert e("ukefor", "emne") == "Velkommen til Veiledning i praksis – praktisk informasjon"
    assert "Nå er det snart tid for <strong>Veiledning i praksis</strong>, som starter 2027-03-01." in h("ukefor", "innledning")
    assert e("dagfor", "emne_forste") == "I morgen starter Veiledning i praksis"
    assert e("dagfor", "emne_midt") == "I morgen, dag 2: Veiledning i praksis"
    assert e("dagfor", "emne_siste") == "Siste kursdag i morgen: Veiledning i praksis"
    assert "I morgen er dag 2 av 3 på <strong>Veiledning i praksis</strong>." in h("dagfor", "innledning_midt")
    assert e("avlysning", "emne") == "Avlyst: Veiledning i praksis"
    assert "Vi beklager ulempen dette medfører." in h("avlysning", "tekst")
    assert e("kursbevis_klar", "emne") == "Kursbevis: Veiledning i praksis"
    assert e("firmapaamelding_kvittering", "emne") == "Bedriftspåmelding til Veiledning i praksis – kvittering"
    assert "Takk for påmeldingen av 3 deltakere fra Firma AS til <strong>Veiledning i praksis</strong>." in h("firmapaamelding_kvittering", "innledning")
    # (06.10.2026: kursholder-lenken og SharePoint er tatt bort, og «purring» med dem)


def test_ingen_sensitive_koder_finnes():
    assert set(KODER) == {"fornavn", "navn", "kursnavn", "startdato", "dato", "dagnummer", "antall_dager", "firmanavn",
                          "antall_deltakere", "beskrivelse", "beskrivelse_liten", "frist", "dager_igjen",
                          "dager_igjen_tekst", "min_side", "sporsmal_url"}
    for forbudt in ("allergi", "tilrettelegging", "epost", "telefon", "adresse", "fodsel", "personnummer", "passord",
                    "token", "zoom", "org_nr", "faktura"):
        assert not [k for k in KODER if forbudt in k], forbudt
        assert not [k for m in MALER.values() for f in m.felt.values() for k in f.kode if forbudt in k]


# ============================ 20-27: override-oppslag ============================

def test_standard_hentes_naar_override_mangler_og_db_er_tom(con):
    assert con.execute("SELECT COUNT(*) FROM mal_tekst").fetchone()[0] == 0       # ingenting kopiert inn ved init
    for mnavn, mal in MALER.items():
        for fnavn, f in mal.felt.items():
            assert maltekster.effektiv_tekst(con, mnavn, fnavn) == f.standard
            assert maltekster.er_tilpasset(con, mnavn, fnavn) is False


def test_override_hentes_naar_den_finnes(con):
    lagret = maltekster.lagre_maltekst(con, "bekreftelse", "innledning", "  Velkommen, {navn}!  ", aktor="admin:kari")
    con.commit()
    assert lagret == "Velkommen, {navn}!"
    assert maltekster.effektiv_tekst(con, "bekreftelse", "innledning") == "Velkommen, {navn}!"
    assert maltekster.er_tilpasset(con, "bekreftelse", "innledning") is True
    rad = con.execute("SELECT * FROM mal_tekst").fetchone()
    assert (rad["mal"], rad["felt"], rad["oppdatert_av"]) == ("bekreftelse", "innledning", "admin:kari") and rad["oppdatert"]


def test_lagring_oppdaterer_eksisterende_rad_uten_duplikat(con):
    maltekster.lagre_maltekst(con, "venteliste", "tekst", "Versjon 1")
    maltekster.lagre_maltekst(con, "venteliste", "tekst", "Versjon 2", aktor="admin:ola")
    assert con.execute("SELECT COUNT(*) FROM mal_tekst").fetchone()[0] == 1
    assert db.hent_maltekst(con, "venteliste", "tekst") == "Versjon 2"
    assert con.execute("SELECT oppdatert_av FROM mal_tekst").fetchone()[0] == "admin:ola"


def test_ugyldig_tekst_lagres_ikke(con):
    _feil(UKJENT_KODE, maltekster.lagre_maltekst, con, "bekreftelse", "innledning", "Hei {ukjent}")
    _feil(TOM, maltekster.lagre_maltekst, con, "bekreftelse", "emne", "")
    assert con.execute("SELECT COUNT(*) FROM mal_tekst").fetchone()[0] == 0
    assert _hendelser(con) == []                                                # ingen hendelse ved avvist lagring


def test_slett_gir_standard_igjen(con):
    maltekster.lagre_maltekst(con, "venteliste", "tekst", "Egen tekst")
    assert maltekster.effektiv_tekst(con, "venteliste", "tekst") == "Egen tekst"
    assert maltekster.tilbakestill_maltekst(con, "venteliste", "tekst") is True
    assert maltekster.effektiv_tekst(con, "venteliste", "tekst") == maltekster.standard_tekst("venteliste", "tekst")
    assert con.execute("SELECT COUNT(*) FROM mal_tekst").fetchone()[0] == 0


@pytest.mark.parametrize("ugyldig", ["Hei {ukjent}", "Hei {{ navn }}", "Hei {navn", "Hei }", "Hei" + chr(0) + "x",
                                     "x" * 5000, "", "   "])
def test_ugyldig_lagret_override_gir_malfeil_ikke_fallback(con, ugyldig):
    if db.er_postgres(con) and chr(0) in ugyldig:
        pytest.skip("PostgreSQL kan ikke lagre NUL i tekst - denne korrupsjonen kan ikke oppstaa der")
    _rad(con, "venteliste", "tekst", ugyldig)                                   # korrupt rad, forbi service-laget
    feil = None
    with pytest.raises(MalFeil) as e:
        maltekster.effektiv_tekst(con, "venteliste", "tekst")
    feil = e.value
    assert feil.mal == "venteliste" and feil.felt == "tekst"
    with pytest.raises(MalFeil):
        maltekster.effektive_tekster(con, "venteliste")
    with pytest.raises(MalFeil):
        maltekster.hent_overstyringer(con, "venteliste")


def test_ugyldig_override_i_annet_felt_i_samme_mal_gir_ogsaa_malfeil(con):
    """Konservativ regel: alle rader for malen valideres. Et korrupt felt stopper hele malen - ingen stille fallback."""
    _rad(con, "dagfor", "emne_forste", "Hei {ukjent}")
    with pytest.raises(MalFeil):
        maltekster.effektiv_tekst(con, "dagfor", "innledning_midt")


def test_rad_med_ukjent_felt_for_kjent_mal_gir_malfeil_ikke_ignorert(con):
    _rad(con, "bekreftelse", "finnes_ikke", "Noe")
    feil = _feil(UKJENT_FELT, maltekster.effektiv_tekst, con, "bekreftelse", "emne")     # selv om vi ber om et gyldig felt
    assert feil.mal == "bekreftelse"
    _feil(UKJENT_FELT, maltekster.effektive_tekster, con, "bekreftelse")


def test_rad_for_ukjent_mal_paavirker_ikke_kjente_maler(con):
    """Rader for en mal som ikke finnes leses aldri av oppslag for kjente maler."""
    _rad(con, "gammel_mal", "emne", "Noe")
    assert maltekster.effektiv_tekst(con, "bekreftelse", "emne") == maltekster.standard_tekst("bekreftelse", "emne")


def test_db_lesefeil_gir_malfeil_ikke_fallback_tabell_borte(con):
    con.execute("DROP TABLE mal_tekst")
    con.commit()
    feil = _feil(DB_LESEFEIL, maltekster.effektiv_tekst, con, "bekreftelse", "emne")
    assert feil.mal == "bekreftelse"


def test_db_lesefeil_gir_malfeil_simulert_laast_database(con, monkeypatch):
    def laast(*a, **kw):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(db, "hent_overstyringer_for_mal", laast)
    _feil(DB_LESEFEIL, maltekster.effektive_tekster, con, "ukefor")             # (06.10.2026: før «purring», som er tatt bort)


def test_db_lesefeil_gir_malfeil_lukket_forbindelse(con):
    con.close()
    _feil(DB_LESEFEIL, maltekster.effektiv_tekst, con, "bekreftelse", "emne")


def test_malfeil_bar_kun_mal_felt_og_grunn_aldri_teksten(con):
    hemmelig = "HEMMELIG-TEKST-Ola-Nordmann-ola@x.no"
    feil = _feil(UKJENT_KODE, maltekster.valider, "bekreftelse", "innledning", hemmelig + " {ukjent_kode_her}")
    for uttrykk in (str(feil), repr(feil), json.dumps(feil.detaljer()), str(feil.args)):
        assert "HEMMELIG" not in uttrykk and "ola@x.no" not in uttrykk and "ukjent_kode_her" not in uttrykk
    assert feil.detaljer() == {"mal": "bekreftelse", "felt": "innledning", "grunn": UKJENT_KODE}
    assert "ukjent_kode_her" in feil.forklaring        # forklaringen er KUN til admin-visning, aldri til logg


def test_overstyring_i_ett_felt_paavirker_ikke_andre_felt(con):
    maltekster.lagre_maltekst(con, "bekreftelse", "innledning", "Egen innledning")
    tekster = maltekster.effektive_tekster(con, "bekreftelse")
    assert tekster["innledning"] == "Egen innledning"
    assert tekster["emne"] == maltekster.standard_tekst("bekreftelse", "emne")
    assert tekster["avslutning"] == maltekster.standard_tekst("bekreftelse", "avslutning")


def test_overstyring_i_en_mal_paavirker_ikke_en_annen_mal(con):
    maltekster.lagre_maltekst(con, "bekreftelse", "emne", "Egen: {kursnavn}")
    for annen in ("venteliste", "ukefor", "avlysning", "kursbevis_klar", "firmapaamelding_kvittering"):
        assert maltekster.effektiv_tekst(con, annen, "emne") == maltekster.standard_tekst(annen, "emne")
    assert maltekster.effektiv_tekst(con, "bekreftelse", "emne") == "Egen: {kursnavn}"


def test_samme_feltnavn_i_to_maler_er_uavhengige(con):
    maltekster.lagre_maltekst(con, "bekreftelse", "avslutning", "A")
    maltekster.lagre_maltekst(con, "ukefor", "avslutning", "B")
    assert maltekster.effektiv_tekst(con, "bekreftelse", "avslutning") == "A"
    assert maltekster.effektiv_tekst(con, "ukefor", "avslutning") == "B"
    assert maltekster.effektiv_tekst(con, "firmapaamelding_kvittering", "avslutning") == ""


# ============================ 28-30: hendelser ============================

def test_mal_endret_hendelse_er_pii_fri_og_har_mal_felt_aktor(con):
    hemmelig = "HEMMELIG-TEKST for {navn} ola@x.no"
    maltekster.lagre_maltekst(con, "bekreftelse", "innledning", hemmelig, aktor="admin:kari")
    (handling, detaljer, aktor), = _hendelser(con)
    assert handling == "mal_endret" and aktor == "admin:kari"
    assert detaljer == {"mal": "bekreftelse", "felt": "innledning"}
    assert "HEMMELIG" not in json.dumps(detaljer) and "ola@x.no" not in json.dumps(detaljer)
    alle = " ".join(r[0] or "" for r in con.execute("SELECT detaljer FROM hendelse"))
    assert "HEMMELIG" not in alle


def test_mal_tilbakestilt_hendelse_er_pii_fri(con):
    maltekster.lagre_maltekst(con, "venteliste", "tekst", "HEMMELIG-TEKST ola@x.no")
    assert maltekster.tilbakestill_maltekst(con, "venteliste", "tekst", aktor="admin:ola") is True
    hendelser = _hendelser(con)
    assert [h[0] for h in hendelser] == ["mal_endret", "mal_tilbakestilt"]
    handling, detaljer, aktor = hendelser[1]
    assert aktor == "admin:ola" and detaljer == {"mal": "venteliste", "felt": "tekst"}
    assert "HEMMELIG" not in " ".join(r[0] or "" for r in con.execute("SELECT detaljer FROM hendelse"))


def test_tilbakestilling_logges_ikke_naar_raden_ikke_fantes(con):
    assert maltekster.tilbakestill_maltekst(con, "venteliste", "tekst", aktor="admin:ola") is False
    assert _hendelser(con) == []
    _feil(UKJENT_FELT, maltekster.tilbakestill_maltekst, con, "venteliste", "finnes_ikke")
    assert _hendelser(con) == []


def test_ren_lesing_logger_ingen_hendelser(con):
    maltekster.effektive_tekster(con, "bekreftelse")
    maltekster.effektiv_tekst(con, "ukefor", "innledning")                     # (06.10.2026: før «purring», som er tatt bort)
    maltekster.er_tilpasset(con, "dagfor", "avslutning")
    assert _hendelser(con) == [] and not con.in_transaction


# ============================ database: kun overstyringer, migrering ============================

def test_db_laget_vet_ingenting_om_koder_og_lagrer_bare_tekst(con):
    db.sett_maltekst(con, "hvilken_som_helst", "felt", "{{ ikke validert her }}")     # DB validerer ikke - det gjor maltekster
    assert db.hent_maltekst(con, "hvilken_som_helst", "felt") == "{{ ikke validert her }}"
    assert db.hent_maltekst(con, "hvilken_som_helst", "annet") is None
    assert db.hent_overstyringer_for_mal(con, "hvilken_som_helst") == {"felt": "{{ ikke validert her }}"}
    assert db.slett_maltekst(con, "hvilken_som_helst", "felt") is True and db.slett_maltekst(con, "hvilken_som_helst", "felt") is False
    kilde = inspect.getsource(db.sett_maltekst) + inspect.getsource(db.hent_overstyringer_for_mal)
    assert "maltekster" not in kilde and "jinja" not in kilde.lower()


def test_gammel_database_faar_mal_tekst_ved_oppstart(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_STI", tmp_path / "gammel.db")
    c = db.koble()
    db.init(c)
    c.execute("DROP TABLE mal_tekst")
    c.commit()
    c.close()
    c = db.koble()
    db.init(c)
    db.init(c)                                                                     # idempotent
    assert c.execute("SELECT COUNT(*) FROM mal_tekst").fetchone()[0] == 0
    assert db.kolonner(c, "mal_tekst") == ["mal", "felt", "tekst", "oppdatert", "oppdatert_av"]
    c.close()


def test_avhengigheten_gaar_én_vei_epost_kan_importere_maltekster_men_ikke_omvendt():
    from kurs.integrasjoner import epost
    assert "maltekster" in inspect.getsource(epost)                 # epost.py bruker maltekster (12B2A)
    tre = ast.parse(inspect.getsource(maltekster))
    importert = {a.name for n in ast.walk(tre) if isinstance(n, ast.Import) for a in n.names}
    importert |= {(n.module or "") + "." + a.name for n in ast.walk(tre) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert not [i for i in importert if "epost" in i.lower() or "kjoring" in i.lower()], importert


# ============================ mal-ID = Jinja-filnavn = faktisk `mal` i send_en_gang (hindrer navnedrift) ============================

REDIGERBARE = ["bekreftelse", "venteliste", "ukefor", "dagfor", "avlysning", "kursbevis_klar", "firmapaamelding_kvittering",
               "evaluering"]          # (06.10.2026: «purring» er tatt bort med kursholder-lenken og SharePoint)


def test_de_8_redigerbare_malnoklene_er_noyaktig_disse():
    assert sorted(MALER) == sorted(REDIGERBARE)
    assert "firmapaamelding" not in MALER          # ingen forkortet alias - intern ID er identisk med Jinja-malen


@pytest.mark.parametrize("mal", REDIGERBARE)
def test_hver_redigerbar_mal_har_en_jinja_malfil_med_samme_navn(mal):
    from pathlib import Path
    fil = Path(maltekster.__file__).resolve().parent / "maler" / "epost" / f"{mal}.html"
    assert fil.is_file(), fil


def _send_en_gang_mal_argumenter() -> dict:
    """AST-scan av all kode som kaller send_en_gang(), render_for_sending() ELLER maltekster.maltekst_for_utsending()
    med et LITERAL mal-navn: {mal-navn: [filer]}. Tre kall-former fordi:
      - kursbevis_klar (TOCTOU-vakt, se kjoring.py/kursbevis.py) rendres ÉN gang via render_for_sending() og sendes
        videre med det allerede rendrede resultatet via send_ferdigrendret_en_gang() - IKKE send_en_gang(mal=...).
      - firmapaamelding_kvittering (TOCTOU-vakt, se web/app.py) kaller maltekster.maltekst_for_utsending() direkte i
        en tidlig preflight (FOER registrering), og fullforer senere med epost.render(maltekst=...) +
        send_ferdigrendret_en_gang() - heller ikke via send_en_gang(mal=...) eller render_for_sending().
    Alle tre kall-formene binder likevel malnavnet til en strengliteral, saa navnedrift fanges uansett hvilken som
    brukes. Variable mal-argumenter (f.eks. daglig.py sin preflight-loekke over flere maler) hopper vi over her -
    de dekkes av sine egne, literale send_en_gang-kall andre steder."""
    from pathlib import Path
    rot = Path(maltekster.__file__).resolve().parent
    funn = {}
    for fil in [rot / "sveiper.py", rot / "daglig.py", rot / "kursbevis.py", rot / "evaluering.py", rot / "web" / "app.py"]:
        for node in ast.walk(ast.parse(fil.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr == "send_en_gang":
                arg = node.args[3] if len(node.args) > 3 else next((k.value for k in node.keywords if k.arg == "mal"), None)
                assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), f"{fil.name}:{node.lineno} mal er ikke en streng"
                funn.setdefault(arg.value, []).append(f"{fil.name}:{node.lineno}")
            elif node.func.attr == "render_for_sending":
                arg = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "mal"), None)
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    funn.setdefault(arg.value, []).append(f"{fil.name}:{node.lineno}")
            elif node.func.attr == "maltekst_for_utsending":
                arg = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mal"), None)
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    funn.setdefault(arg.value, []).append(f"{fil.name}:{node.lineno}")
    return funn


def test_hver_redigerbar_mal_sendes_faktisk_via_send_en_gang_under_noyaktig_dette_navnet():
    brukt = _send_en_gang_mal_argumenter()
    for mal in REDIGERBARE:
        assert mal in brukt, f"{mal} sendes ikke under dette navnet (navnedrift?)"
    # alle andre maler som sendes via send_en_gang/render_for_sending er de LASTE (ikke redigerbare):
    # avslag (fase 17 - det viktige innholdet er begrunnelsen admin skriver i hver sak; kan gjoeres redigerbar senere) og
    # sjekkliste_paaminnelse (morgen-e-posten om sjekklistene til kurspostboksen, 02.10.2026).
    # (06.10.2026: kursholder-lenken og SharePoint er tatt bort, og «eskalering» til admin med dem)
    assert set(brukt) - set(MALER) == {"avslag", "sjekkliste_paaminnelse"}
