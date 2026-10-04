"""Kursside: innholdsmodellen (kurs/sideinnhold.py) - validering, renser (XSS), synlighet, endringsoversikt, publiseringskontroll, maler.

Rene tester uten database og uten Flask. Bare oppdiktede data.
"""
import json
from datetime import date, timedelta

import pytest

from kurs import sideinnhold as si
from kurs.sideinnhold import Sidefeil

from kurssidehjelp import IDAG, dokument, tekstblokk

TOM = dict(fil_ider={}, kursdag_ider=set())


def _blokk(typ, data, **felt):
    return {"id": felt.pop("id", ""), "type": typ, "tittel": felt.pop("tittel", "Tittel"), **felt, "data": data}


def _valider(dok, fil_ider=None, kursdag_ider=None):
    return si.valider(dok, fil_ider=fil_ider or {}, kursdag_ider=kursdag_ider or set())


# ============================ dokumentet: les / til_json / tomt ============================

def test_tomt_dokument_er_gyldig_og_validerer_uendret():
    dok = si.tomt_dokument()
    ren, merknader = _valider(dok)
    assert ren == dok and merknader == [] and dok["v"] == si.SKJEMA_VERSJON == 1


@pytest.mark.parametrize("tekst", [None, "", "ikke json", "[]", "42", '{"v": 2, "blokker": []}', '{"blokker": "x"}', "null"])
def test_les_er_tolerant_og_kaster_aldri(tekst):
    dok = si.les(tekst)
    assert dok == si.tomt_dokument() or (dok["v"] == 1 and dok["blokker"] == [])


def test_les_hopper_over_ugyldige_blokker_og_beholder_de_gyldige():
    rå = {"v": 1, "tittel": "Kurs", "blokker": ["tekst", {"type": "finnesikke", "data": {}}, {"type": "tekst"},
                                               {"id": "b_abcd1234", "type": "tekst", "tittel": "Ok", "data": {"html": "<p>x</p>"}}]}
    dok = si.les(json.dumps(rå))
    assert dok["tittel"] == "Kurs" and [b["id"] for b in dok["blokker"]] == ["b_abcd1234"]


def test_til_json_bevarer_aesoeaa_og_rekkefolge():
    dok = dokument(tekstblokk("Ærlig talt", "<p>Øvelse på Åsane</p>", id="b_aaaa1111"), tekstblokk("Andre", id="b_bbbb2222"))
    tekst = si.til_json(dok)
    assert "Ærlig talt" in tekst and "\\u" not in tekst and " " not in tekst.split('"blokker"')[0]
    assert [b["id"] for b in si.les(tekst)["blokker"]] == ["b_aaaa1111", "b_bbbb2222"]


def test_ny_blokk_id_har_riktig_format_og_er_unik():
    ider = {si.ny_blokk_id() for _ in range(200)}
    assert len(ider) == 200 and all(si._BLOKK_ID.match(i) for i in ider)


# ============================ valider: struktur ============================

@pytest.mark.parametrize("dok", [None, [], "tekst", 5, {"v": 2}, {"v": "1"}])
def test_strukturfeil_i_dokumentet_gir_sidefeil(dok):
    with pytest.raises(Sidefeil):
        _valider(dok)


def test_ukjent_blokktype_gir_sidefeil():
    with pytest.raises(Sidefeil, match="type"):
        _valider(dokument(_blokk("video", {})))


def test_for_mange_blokker_gir_sidefeil():
    ok = dokument(*[tekstblokk(id=f"b_{n:08d}") for n in range(si.MAKS_BLOKKER)])
    assert len(_valider(ok)[0]["blokker"]) == si.MAKS_BLOKKER
    with pytest.raises(Sidefeil, match="For mange"):
        _valider(dokument(*[tekstblokk(id=f"b_{n:08d}") for n in range(si.MAKS_BLOKKER + 1)]))


def test_for_stort_dokument_gir_sidefeil():
    # 30 blokker à 12 000 tegn er ~360 000 byte: over grensen på 300 000, men hver blokk er under tekstgrensen
    dok = dokument(*[tekstblokk(html="<p>" + "a" * 12_000 + "</p>") for _ in range(30)])
    with pytest.raises(Sidefeil, match="for stor"):
        _valider(dok)


@pytest.mark.parametrize("felt, maks", [("tittel", si.MAKS_TITTEL), ("ingress", si.MAKS_INGRESS), ("rom", si.MAKS_ROM)])
def test_for_lange_toppfelt_gir_sidefeil(felt, maks):
    assert _valider(dokument(**{felt: "a" * maks}))[0][felt] == "a" * maks
    with pytest.raises(Sidefeil, match="for lang"):
        _valider(dokument(**{felt: "a" * (maks + 1)}))


def test_for_lang_blokktittel_melding_og_viktig_tekst():
    with pytest.raises(Sidefeil):
        _valider(dokument(tekstblokk(tittel="a" * (si.MAKS_TITTEL + 1))))
    with pytest.raises(Sidefeil):
        _valider(dokument(melding={"tekst": "a" * (si.MAKS_MELDING + 1), "niva": "info"}))
    with pytest.raises(Sidefeil):
        _valider(dokument(_blokk("viktig", {"niva": "info", "tekst": "a" * (si.MAKS_VIKTIG + 1)})))


def test_ugyldig_dato_gir_sidefeil_men_tom_dato_er_null():
    ren = _valider(dokument(tekstblokk(vis_fra="")))[0]
    assert ren["blokker"][0]["vis_fra"] is None
    for daarlig in ("27.11.2026", "2026-13-01", "2026-02-30", "20261127", "i morgen", 5):
        with pytest.raises(Sidefeil, match="dato"):
            _valider(dokument(tekstblokk(vis_fra=daarlig)))


def test_id_regenereres_naar_den_mangler_er_ugyldig_eller_duplisert():
    dok = dokument(tekstblokk(id=""), tekstblokk(id="UGYLDIG"), tekstblokk(id="b_aaaa1111"), tekstblokk(id="b_aaaa1111"))
    ider = [b["id"] for b in _valider(dok)[0]["blokker"]]
    assert len(set(ider)) == 4 and ider[2] == "b_aaaa1111" and all(si._BLOKK_ID.match(i) for i in ider)


def test_ukjente_noekler_kastes_og_standardverdier_fylles_inn():
    ren = _valider({"v": 1, "hemmelig": 1, "blokker": [{"type": "tekst", "ekstra": True, "data": {"html": "<p>x</p>", "annet": 1}}]})[0]
    b = ren["blokker"][0]
    assert set(ren) == {"v", "tittel", "ingress", "rom", "godkjent", "melding", "blokker"}     # «godkjent»: 04.10.2026
    assert set(b) == {"id", "type", "tittel", "skjult", "vis_fra", "i_meny", "data"} and set(b["data"]) == {"html"}
    assert (b["tittel"], b["skjult"], b["vis_fra"], b["i_meny"]) == ("", False, None, True)


def test_tekst_trimmes_og_kontrolltegn_fjernes():
    ren = _valider(dokument(tittel="  Kurs \x00med\x07 tegn \n", melding={"tekst": "Linje 1\r\nLinje 2\x00", "niva": "viktig"}))[0]
    assert ren["tittel"] == "Kurs med tegn" and ren["melding"] == {"tekst": "Linje 1\nLinje 2", "niva": "viktig"}


def test_ugyldig_nivaa_og_plassering_faar_standardverdi():
    ren = _valider(dokument(_blokk("viktig", {"niva": "farlig", "tekst": "x"}), _blokk("bilde", {"fil_id": None, "plassering": "midt"}),
                            melding={"tekst": "", "niva": "rar"}))[0]
    assert ren["blokker"][0]["data"]["niva"] == "info" and ren["blokker"][1]["data"]["plassering"] == "bred"
    assert ren["melding"]["niva"] == "info"


# ============================ valider: per blokktype ============================

def test_fil_fra_annet_kurs_fjernes_med_merknad_men_egne_beholdes():
    dok = dokument(_blokk("filer", {"filer": [{"fil_id": 1, "tittel": "Min"}, {"fil_id": 99, "tittel": "Andres"}]}))
    ren, merknader = _valider(dok, fil_ider={1: {"type": "dokument"}})
    assert [e["fil_id"] for e in ren["blokker"][0]["data"]["filer"]] == [1]
    assert len(merknader) == 1 and "finnes ikke lenger" in merknader[0]


def test_bilde_maa_vaere_en_bildefil_i_samme_kurs():
    fil_ider = {1: {"type": "bilde"}, 2: {"type": "dokument"}}
    ok, m1 = _valider(dokument(_blokk("bilde", {"fil_id": 1, "alt": "x"})), fil_ider=fil_ider)
    dok_fil, m2 = _valider(dokument(_blokk("bilde", {"fil_id": 2, "alt": "x"})), fil_ider=fil_ider)
    ukjent, m3 = _valider(dokument(_blokk("bilde", {"fil_id": 77, "alt": "x"})), fil_ider=fil_ider)
    assert ok["blokker"][0]["data"]["fil_id"] == 1 and not m1
    assert dok_fil["blokker"][0]["data"]["fil_id"] is None and ukjent["blokker"][0]["data"]["fil_id"] is None and m2 and m3


def test_ukjent_kursdag_settes_til_null_og_lagret_dato_beholdes():
    dok = dokument(_blokk("program", {"dager": [{"kursdag_id": 31, "dato": "2027-03-10", "tittel": "Dag 1", "punkter": []},
                                              {"kursdag_id": 999, "dato": "2027-03-11", "tittel": "Dag 2", "punkter": []}]}))
    ren, merknader = _valider(dok, kursdag_ider={31})
    dager = ren["blokker"][0]["data"]["dager"]
    assert [d["kursdag_id"] for d in dager] == [31, None] and dager[1]["dato"] == "2027-03-11" and len(merknader) == 1


def test_program_grenser_og_klokkeslett():
    def dag(punkter):
        return dokument(_blokk("program", {"dager": [{"kursdag_id": None, "dato": None, "tittel": "", "punkter": punkter}]}))
    ren, merknader = _valider(dag([{"fra": "915", "til": "10.45", "tema": "Tema", "sted": "Rom 3", "hvem": "Kari", "pause": False},
                                   {"fra": "09:00", "til": "", "tema": "Pause", "pause": True}]))
    p = ren["blokker"][0]["data"]["dager"][0]["punkter"]
    assert (p[0]["fra"], p[0]["til"]) == ("09:15", "10:45") and p[1]["pause"] is True and not merknader
    ren, merknader = _valider(dag([{"fra": "9:1x", "til": "25:00", "tema": "Halv tid"}]))       # halvferdig tid stopper aldri lagringen
    assert ren["blokker"][0]["data"]["dager"][0]["punkter"][0]["fra"] == "" and len(merknader) == 2
    with pytest.raises(Sidefeil):
        _valider(dag([{"tema": "x"}] * (si.MAKS_PUNKTER + 1)))
    with pytest.raises(Sidefeil):
        _valider(dokument(_blokk("program", {"dager": [{"punkter": []}] * (si.MAKS_DAGER + 1)})))
    with pytest.raises(Sidefeil, match="for lang"):
        _valider(dag([{"tema": "a" * (si.MAKS_TEMA + 1)}]))


def test_lenker_ugyldig_adresse_gir_merknad_ikke_feil():
    dok = dokument(_blokk("lenker", {"lenker": [
        {"tittel": "Bra", "url": "ipr.no/kurs", "nivaa": "obligatorisk"},
        {"tittel": "Farlig", "url": "javascript:alert(1)"},
        {"tittel": "Halv", "url": "htp:/feil"},
        {"tittel": "Zoom", "url": "https://skal-ikke-lagres.example", "kilde": "zoom"},
        {"tittel": "Ukjent nivå", "url": "https://a.no", "nivaa": "kjempeviktig"}]}))
    ren, merknader = _valider(dok)
    lenker = ren["blokker"][0]["data"]["lenker"]
    assert [e["url"] for e in lenker] == ["https://ipr.no/kurs", "", "", "", "https://a.no"]
    assert lenker[0]["nivaa"] == "obligatorisk" and lenker[3]["kilde"] == "zoom" and lenker[4]["nivaa"] is None
    assert len(merknader) == 2 and any("Farlig" in m for m in merknader)


def test_tabell_rader_tilpasses_kolonnene_og_grenser():
    dok = dokument(_blokk("tabell", {"kolonner": ["Gruppe", "Deltakere", "Rom"], "rader": [["1"], ["1", "2", "3", "4", "5"], ["a", "b", "c"]]}))
    ren = _valider(dok)[0]
    assert ren["blokker"][0]["data"]["rader"] == [["1", "", ""], ["1", "2", "3"], ["a", "b", "c"]]
    with pytest.raises(Sidefeil):
        _valider(dokument(_blokk("tabell", {"kolonner": ["k"] * (si.MAKS_KOLONNER + 1), "rader": []})))
    with pytest.raises(Sidefeil):
        _valider(dokument(_blokk("tabell", {"kolonner": ["a"], "rader": [["x"]] * (si.MAKS_RADER + 1)})))
    with pytest.raises(Sidefeil, match="for lang"):
        _valider(dokument(_blokk("tabell", {"kolonner": ["a"], "rader": [["x" * (si.MAKS_CELLE + 1)]]})))


def test_kontakt_telefon_og_epost_valideres_med_merknad():
    dok = dokument(_blokk("kontakt", {"personer": [
        {"navn": "Marte", "rolle": "Kursleder", "telefon": "+47 555 12 345", "epost": "marte@example.no"},
        {"navn": "Feil", "telefon": "ring meg", "epost": "ikke-en-adresse"}]}))
    ren, merknader = _valider(dok)
    p = ren["blokker"][0]["data"]["personer"]
    assert (p[0]["telefon"], p[0]["epost"]) == ("+47 555 12 345", "marte@example.no")
    assert (p[1]["telefon"], p[1]["epost"]) == ("", "") and len(merknader) == 2


def test_filelementer_faar_dato_og_synlighet_validert():
    dok = dokument(_blokk("filer", {"filer": [{"fil_id": 1, "synlig_fra": "2027-04-01"}], "sharepoint": True}))
    ren = _valider(dok, fil_ider={1: {"type": "dokument"}})[0]["blokker"][0]["data"]
    assert ren["filer"][0]["synlig_fra"] == "2027-04-01" and ren["sharepoint"] is True and ren["vis_kommende"] is True
    with pytest.raises(Sidefeil):
        _valider(dokument(_blokk("filer", {"filer": [{"fil_id": 1, "synlig_fra": "snart"}]})), fil_ider={1: {"type": "dokument"}})


# ============================ trygg_url ============================

@pytest.mark.parametrize("inn, ut", [
    ("https://ipr.no/kurs?a=1&b=2", "https://ipr.no/kurs?a=1&b=2"), ("http://ipr.no", "http://ipr.no"),
    ("  https://ipr.no  ", "https://ipr.no"), ("mailto:kari@example.no", "mailto:kari@example.no"),
    ("tel:+4755123456", "tel:+4755123456"), ("ipr.no", "https://ipr.no"), ("www.ipr.no/kurs", "https://www.ipr.no/kurs"),
    ("HTTPS://IPR.NO", "HTTPS://IPR.NO")])
def test_trygg_url_godtar_trygge_adresser(inn, ut):
    assert si.trygg_url(inn) == ut


@pytest.mark.parametrize("inn", [
    None, "", "   ", 5, "javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,<script>x</script>", "vbscript:x",
    "java\tscript:alert(1)", " javascript:alert(1)", "\x01javascript:alert(1)", "java script:x", "file:///etc/passwd", "ftp://a.no",
    "https://", "https:///a", "https://user@evil.no", "//evil.no", "/relativ/sti", "har mellomrom.no", "https://a.no/<x>", 'https://a.no/"x',
    "https://a.no\\evil", "tel:ring", "x" * 2001])
def test_trygg_url_avviser_alt_annet(inn):
    assert si.trygg_url(inn) is None


# ============================ rens_html: allowlist og XSS ============================

XSS = [
    "<script>alert(1)</script><p>ok</p>", "<p onclick=alert(1)>x</p>", "<img src=x onerror=alert(1)>", "<a href=\"javascript:alert(1)\">x</a>",
    "<a href=\"JaVaScRiPt:alert(1)\">x</a>", "<a href=\"java\tscript:alert(1)\">x</a>", "<a href=\" javascript:alert(1)\">x</a>",
    "<a href=\"data:text/html;base64,PHNjcmlwdD4=\">x</a>", "<a href=\"vbscript:x\">x</a>", "<iframe src=//evil.no></iframe>",
    "<style>body{background:url(//evil)}</style>", "<svg onload=alert(1)><a href=//evil>x</a></svg>", "<form action=//evil><input></form>",
    "<object data=x></object>", "<embed src=x>", "<video src=x onerror=alert(1)></video>", "<math><mi xlink:href=javascript:x>x</mi></math>",
    "<p style=\"background:url(javascript:x)\">x</p>", "<a href=\"https://ok.no\" onmouseover=alert(1)>x</a>", "<<script>script>alert(1)<</script>/script>",
    "<a href=\"https://ok.no\"><script>alert(1)</script></a>", "<base href=//evil.no>", "<meta http-equiv=refresh content=0;url=//evil.no>",
    "<link rel=stylesheet href=//evil.no>", "<template><script>x</script></template>", "<noscript><p title=\"</noscript><img src=x onerror=alert(1)>\">",
    "<textarea><script>x</script></textarea>", "<select><option>x</select>", "<button onclick=x>x</button>", "<xmp><script>x</script></xmp>",
    "<p>tekst <b onclick=x>fet</b></p>", "&lt;script&gt;alert(1)&lt;/script&gt;", "<a href=\"https://ok.no/\" target=\"_top\" rel=\"opener\">x</a>",
    "<img srcset=x onerror=alert(1)>", "<input autofocus onfocus=alert(1)>", "<details open ontoggle=alert(1)>x</details>", "<marquee onstart=alert(1)>x</marquee>",
]


@pytest.mark.parametrize("farlig", XSS)
def test_rens_html_fjerner_alt_farlig_og_er_idempotent(farlig):
    ren = si.rens_html(farlig)
    lav = ren.lower()
    for forbudt in ("<script", "<img", "<iframe", "<style", "<svg", "<form", "<object", "<embed", "<video", "<math", "<input", "<base",
                    "<meta", "<link", "<button", "<textarea", "<select", "<template", "<xmp", "<marquee", "<details", "onerror", "onclick",
                    "onload", "onmouseover", "onfocus", "javascript:", "vbscript:", "data:", "srcset", "style=", "target=", "rel="):
        assert forbudt not in lav, (farlig, ren)
    assert si.rens_html(ren) == ren
    assert si.til_visning(ren).count("<a ") == ren.count("<a ")


def _tagger_og_attributter(html: str) -> list[tuple[str, tuple]]:
    """Alle tagger med attributter slik en nettleser leser dem (uavhengig av hvordan rensen skriver dem)."""
    from html.parser import HTMLParser

    funnet = []

    class Leser(HTMLParser):
        def handle_starttag(self, tag, attrs):
            funnet.append((tag, tuple(a for a, _ in attrs)))

    Leser().feed(html)
    return funnet


@pytest.mark.parametrize("farlig", XSS + [
    "<a href='https://a.no/\" onclick=\"alert(1)'>x</a>", '<a href="https://a.no/&quot; onmouseover=&quot;alert(1)">x</a>',
    '<a href="https://a.no" href="javascript:alert(1)">x</a>', "<a href=https://a.no/onerror=alert(1)>x</a>",
    "<p title=\"x\" lang=no dir=rtl hidden>x</p>"])
def test_rens_html_gir_bare_tillatte_tagger_og_href_som_eneste_attributt(farlig):
    tillatt = {"p", "br", "strong", "em", "ul", "ol", "li", "a"}
    for tag, attrs in _tagger_og_attributter(si.rens_html(farlig)) + _tagger_og_attributter(si.til_visning(farlig)):
        assert tag in tillatt, (farlig, tag)
        assert set(attrs) <= ({"href", "rel", "target"} if tag == "a" else set()), (farlig, tag, attrs)


def test_rens_html_beholder_det_som_er_tillatt():
    html = '<p>Hei <strong>du</strong> og <em>jeg</em></p><ul><li>en</li><li>to</li></ul><ol><li>a</li></ol><p>Se <a href="https://ipr.no/a?x=1&amp;y=2">her</a><br>ny linje</p>'
    assert si.rens_html(html) == html


@pytest.mark.parametrize("inn, ut", [
    ("<b>fet</b> <i>kursiv</i>", "<p><strong>fet</strong> <em>kursiv</em></p>"),
    ("<div>en</div><div>to</div>", "<p>en</p><p>to</p>"),
    ("bare tekst", "<p>bare tekst</p>"),
    ("<li>utenfor liste</li>", "<p>utenfor liste</p>"),
    ("<ul><li>a<li>b</ul>", "<ul><li>a</li><li>b</li></ul>"),
    ("<ul><p>direkte</p></ul>", "<ul><li>direkte</li></ul>"),
    ("<p>a&nbsp;b c</p>", "<p>a b c</p>"),
    ("<p></p><p><br></p><p> </p><p>igjen</p><p>&nbsp;</p>", "<p>igjen</p>"),
    ("<p>en<br><br><br><br>fire</p>", "<p>en<br><br>fire</p>"),
    ("<p><br>først og sist<br></p>", "<p>først og sist</p>"),
    ("<h1>Overskrift</h1><p>tekst</p>", "<p><strong>Overskrift</strong></p><p>tekst</p>"),          # overskrifter fra Word: fet skrift, ikke flatet ut
    ("<table><tr><td>a</td><td>b</td></tr></table>", "<p>a</p><p>b</p>"),
    ('<p class="x" id="y" style="color:red" data-a="b">t</p>', "<p>t</p>"),
    ('<a href="/relativ">intern</a>', "<p>intern</p>"),
    ('<a href="#anker">anker</a>', "<p>anker</p>"),
    ('<a href="mailto:kari@example.no">skriv</a>', '<p><a href="mailto:kari@example.no">skriv</a></p>'),
    ("<p>uferdig <a href=\"https://x", "<p>uferdig</p>"),
    ("<p>1 &lt; 2 &amp; 3 &gt; 0</p>", "<p>1 &lt; 2 &amp; 3 &gt; 0</p>"),
    ("<strong>en <strong>to</strong></strong>", "<p><strong>en to</strong></p>"),
    ("<a href=\"https://a.no\">en <a href=\"https://b.no\">to</a></a>", '<p><a href="https://a.no">en </a><a href="https://b.no">to</a></p>'),
    ("", ""),
])
def test_rens_html_normaliserer(inn, ut):
    assert si.rens_html(inn) == ut


def test_rens_html_dybde_og_lengde():
    dyp = "<ul>" * 100 + "<li>dypt</li>" + "</ul>" * 100
    ren = si.rens_html(dyp)
    assert "dypt" in ren and ren.count("<ul>") <= si._MAKS_DYBDE and si.rens_html(ren) == ren
    assert len(si.rens_html("<p>" + "a" * 19_990 + "</p>")) <= si.MAKS_TEKST_TEGN
    with pytest.raises(Sidefeil, match="for lang"):
        si.rens_html("<p>" + "a" * si.MAKS_TEKST_TEGN + "</p>")
    with pytest.raises(Sidefeil):
        si.rens_html("x" * (si.MAKS_HTML_RA + 1))


def test_rens_html_tar_bare_tekst():
    with pytest.raises(Sidefeil):
        si.rens_html(None)
    with pytest.raises(Sidefeil):
        si.rens_html(["<p>x</p>"])


def test_rens_html_er_idempotent_paa_en_blanding():
    blanding = "".join(XSS) + "<p>Æøå <b>fet</b> <a href='https://ipr.no/?a=1&b=2'>lenke</a></p>" + "<ul><li>en</li></ul>" * 3
    ren = si.rens_html(blanding)
    assert si.rens_html(ren) == ren


# ============================ til_visning ============================

def test_til_visning_setter_rel_og_target_paa_http_men_ikke_mailto_tel():
    ut = si.til_visning('<p><a href="https://a.no">a</a> <a href="http://b.no">b</a> <a href="mailto:x@example.no">m</a> <a href="tel:+4755123456">t</a></p>')
    assert ut.count('rel="noopener noreferrer" target="_blank"') == 2
    assert '<a href="mailto:x@example.no">' in ut and '<a href="tel:+4755123456">' in ut


def test_til_visning_renser_paa_nytt_manipulert_lagret_html_slipper_ikke_gjennom():
    manipulert = '<p>Hei</p><script>alert(1)</script><img src=x onerror=alert(1)><a href="javascript:alert(1)" onclick="x">klikk</a>'
    ut = si.til_visning(manipulert)
    assert ut == "<p>Hei</p><p>klikk</p>"
    assert si.til_visning(None) == "" and si.til_visning(5) == ""
    assert "alert" not in si.til_visning("<p>" + "a" * 100_000 + "</p>" + "<script>alert(1)</script>")     # aldri unntak, selv over grensen


def test_til_visning_er_stabil_naar_den_kjoeres_to_ganger():
    ut = si.til_visning('<a href="https://a.no">a</a>')
    assert si.til_visning(ut) == ut


# ============================ synlighet ============================

def _iso(d):
    return d.isoformat()


def test_skjult_blokk_er_aldri_synlig():
    b = tekstblokk(skjult=True)
    assert not si.blokk_synlig(b, IDAG) and si.synlige_blokker(dokument(b), IDAG) == []


@pytest.mark.parametrize("vis_fra, synlig", [(_iso(IDAG - timedelta(days=1)), True), (_iso(IDAG), True), (_iso(IDAG + timedelta(days=1)), False),
                                             (None, True), ("ugyldig", False)])
def test_vis_fra_styrer_synligheten(vis_fra, synlig):
    assert si.blokk_synlig(tekstblokk(vis_fra=vis_fra), IDAG) is synlig


@pytest.mark.parametrize("blokk", [
    _blokk("tekst", {"html": ""}), _blokk("tekst", {"html": "<p> </p><p><br></p>"}), _blokk("tekst", {"html": "<script>x</script>"}),
    _blokk("viktig", {"niva": "advarsel", "tekst": "  "}), _blokk("program", {"dager": [{"punkter": []}]}), _blokk("program", {"dager": []}),
    _blokk("filer", {"filer": [], "sharepoint": False}), _blokk("lenker", {"lenker": []}), _blokk("lenker", {"lenker": [{"tittel": "x", "url": ""}]}),
    _blokk("tabell", {"kolonner": ["a"], "rader": []}), _blokk("tabell", {"kolonner": ["a"], "rader": [[""]]}),
    _blokk("kontakt", {"personer": []}), _blokk("kontakt", {"personer": [{"navn": "", "rolle": "", "telefon": "", "epost": ""}]}),
    _blokk("bilde", {"fil_id": None, "alt": "x"})])
def test_tomme_blokker_er_aldri_synlige(blokk):
    assert si.blokk_er_tom(blokk) and not si.blokk_synlig(blokk, IDAG)


@pytest.mark.parametrize("blokk", [
    _blokk("tekst", {"html": "<p>x</p>"}), _blokk("viktig", {"niva": "info", "tekst": "x"}),
    _blokk("program", {"dager": [{"punkter": [{"tema": "x"}]}]}), _blokk("filer", {"filer": [{"fil_id": 1}], "sharepoint": False}),
    _blokk("filer", {"filer": [], "sharepoint": True}), _blokk("lenker", {"lenker": [{"tittel": "x", "url": "https://a.no"}]}),
    _blokk("lenker", {"lenker": [{"tittel": "Zoom", "url": "", "kilde": "zoom"}]}), _blokk("tabell", {"kolonner": ["a"], "rader": [["x"]]}),
    _blokk("kontakt", {"personer": [{"navn": "Marte"}]}), _blokk("bilde", {"fil_id": 3})])
def test_blokker_med_innhold_er_ikke_tomme(blokk):
    assert not si.blokk_er_tom(blokk) and si.blokk_synlig(blokk, IDAG)


def test_synlige_blokker_beholder_rekkefolgen():
    dok = dokument(tekstblokk("A", id="b_aaaa1111"), tekstblokk("Skjult", id="b_bbbb2222", skjult=True), tekstblokk("C", id="b_cccc3333"),
                   tekstblokk("Fremtid", id="b_dddd4444", vis_fra=_iso(IDAG + timedelta(days=5))), tekstblokk("Tom", "<p></p>", id="b_eeee5555"))
    assert [b["id"] for b in si.synlige_blokker(dok, IDAG)] == ["b_aaaa1111", "b_cccc3333"]


@pytest.mark.parametrize("synlig_fra, vis_kommende, forventet", [
    (None, True, "ja"), (_iso(IDAG - timedelta(days=3)), True, "ja"), (_iso(IDAG), False, "ja"),
    (_iso(IDAG + timedelta(days=3)), True, "kommer"), (_iso(IDAG + timedelta(days=3)), False, "nei"), ("ugyldig", True, "nei")])
def test_fil_synlig(synlig_fra, vis_kommende, forventet):
    blokk = _blokk("filer", {"filer": [], "vis_kommende": vis_kommende})
    assert si.fil_synlig(blokk, {"fil_id": 1, "synlig_fra": synlig_fra}, IDAG) == forventet


def test_synlige_fil_ider_gjelder_bare_det_en_deltaker_kan_hente_i_dag():
    dok = dokument(
        _blokk("filer", {"filer": [{"fil_id": 1}, {"fil_id": 2, "synlig_fra": _iso(IDAG + timedelta(days=1))},
                                   {"fil_id": 3, "synlig_fra": _iso(IDAG - timedelta(days=1))}], "vis_kommende": True}),
        _blokk("filer", {"filer": [{"fil_id": 4}]}, skjult=True),
        _blokk("filer", {"filer": [{"fil_id": 5}]}, vis_fra=_iso(IDAG + timedelta(days=1))),
        _blokk("bilde", {"fil_id": 6, "alt": "x"}), _blokk("bilde", {"fil_id": 7, "alt": "x"}, skjult=True))
    assert si.synlige_fil_ider(dok, IDAG) == {1, 3, 6}
    assert si.fil_ider_i(dok) == {1, 2, 3, 4, 5, 6, 7}


def test_blokk_referanser_gir_tittel_paa_foerste_blokk():
    dok = dokument(_blokk("filer", {"filer": [{"fil_id": 1}]}, tittel="Presentasjoner"), _blokk("bilde", {"fil_id": 2}, tittel="Banner"))
    assert si.blokk_referanser(dok) == {1: "Presentasjoner", 2: "Banner"}


# ============================ diff ============================

def _med_ider(*blokker):
    return dokument(*[{**b, "id": f"b_{n:08d}"} for n, b in enumerate(blokker, 1)])


def test_diff_uten_gammel_er_alt_ny():
    ny = _med_ider(tekstblokk("A"), tekstblokk("B"))
    assert [(d["art"], d["tittel"]) for d in si.diff(None, ny)] == [("ny", "A"), ("ny", "B")]


def test_diff_ny_endret_fjernet_og_flyttet():
    gammel = _med_ider(tekstblokk("A"), tekstblokk("B"), tekstblokk("C"), tekstblokk("D"))
    ny = json.loads(json.dumps(gammel))
    ny["blokker"][1]["data"]["html"] = "<p>endret</p>"                          # B endret
    ny["blokker"].pop(2)                                                        # C fjernet
    ny["blokker"].insert(0, {**tekstblokk("Ny"), "id": "b_ny000001"})           # ny først
    ny["blokker"].append(ny["blokker"].pop(2))                                  # B flyttes sist (etter D)
    d = si.diff(gammel, ny)
    arter = {(x["tittel"], x["art"]) for x in d}
    assert ("Ny", "ny") in arter and ("B", "endret") in arter and ("C", "fjernet") in arter and ("B", "flyttet") in arter
    assert not any(x["tittel"] == "A" for x in d) and not any(x["tittel"] == "D" and x["art"] != "flyttet" for x in d)
    assert all(x["tekst"] for x in d)


def test_diff_flytting_av_en_blokk_flagger_ikke_alle_de_andre():
    gammel = _med_ider(*[tekstblokk(n) for n in "ABCDEF"])
    ny = json.loads(json.dumps(gammel))
    ny["blokker"].insert(0, ny["blokker"].pop(5))                               # F flyttes til toppen
    flyttet = [x["tittel"] for x in si.diff(gammel, ny) if x["art"] == "flyttet"]
    assert flyttet == ["F"]


def test_diff_side_og_melding():
    gammel = dokument(tittel="Gammel", melding={"tekst": "", "niva": "info"})
    ny = dokument(tittel="Ny", melding={"tekst": "Husk kaffekoppen", "niva": "info"})
    d = {x["id"]: x for x in si.diff(gammel, ny)}
    assert d["_side"]["art"] == "endret" and "Meldingen er lagt til" == d["_melding"]["tekst"]
    assert si.diff(ny, dokument(tittel="Ny")) == [{"id": "_melding", "tittel": "Melding øverst", "type": "melding", "art": "endret", "tekst": "Meldingen er fjernet"}]


def test_diff_gir_kort_norsk_sammendrag_per_type():
    gammel = _med_ider(
        _blokk("program", {"dager": [{"kursdag_id": 1, "dato": "2027-03-10", "tittel": "Dag 2", "punkter": [
            {"fra": "09:00", "til": "10:00", "tema": "A"}, {"fra": "10:00", "til": "11:00", "tema": "B"}]}]}, tittel="Program"),
        _blokk("filer", {"filer": [{"fil_id": 1, "tittel": "Gammel"}]}, tittel="Filer"), _blokk("lenker", {"lenker": []}, tittel="Lenker"),
        _blokk("tabell", {"kolonner": ["Gruppe"], "rader": [["1"]]}, tittel="Tabell"), tekstblokk("Tekst"),
        _blokk("kontakt", {"personer": []}, tittel="Kontakt"))
    ny = json.loads(json.dumps(gammel))
    ny["blokker"][0]["data"]["dager"][0]["punkter"][0]["tema"] = "Endret"
    ny["blokker"][0]["data"]["dager"][0]["punkter"].append({"fra": "", "til": "", "tema": "C"})
    ny["blokker"][1]["data"]["filer"].append({"fil_id": 2, "tittel": "Presentasjon dag 3"})
    ny["blokker"][2]["data"]["lenker"].append({"tittel": "L", "url": "https://a.no"})
    ny["blokker"][3]["data"]["rader"].append(["2"])
    ny["blokker"][4]["skjult"] = True
    ny["blokker"][5]["data"]["personer"].append({"navn": "M"})
    tekster = {x["tittel"]: x["tekst"] for x in si.diff(gammel, ny) if x["art"] == "endret"}
    assert tekster == {"Program": "Dag 2: 1 punkt endret, 1 lagt til", "Filer": "1 ny fil: «Presentasjon dag 3»", "Lenker": "1 lenke lagt til",
                       "Tabell": "Rader endret (1 → 2)", "Tekst": "Skjult", "Kontakt": "1 person lagt til"}


# ============================ publiseringskontroll ============================

def _sjekk(dok, *, filer=None, epost=(), kursdager=()):
    return si.publiseringssjekk(dok, filer=filer or {}, bekreftede_epost=set(epost), kursdager=list(kursdager), idag=IDAG)


def test_bekreftet_deltakers_epost_blokkerer_i_tekst_celle_kontakt_og_lenke_ogsaa_med_store_bokstaver():
    epost = {"kari@example.no"}
    for blokk in (tekstblokk(html="<p>Kontakt Kari: Kari@Example.no</p>"), tekstblokk(html='<p><a href="mailto:kari@example.no">skriv</a></p>'),
                  _blokk("tabell", {"kolonner": ["Gruppe", "Deltakere"], "rader": [["1", "KARI@EXAMPLE.NO"]]}),
                  _blokk("kontakt", {"personer": [{"navn": "Kari", "epost": "kari@example.no"}]}),
                  _blokk("lenker", {"lenker": [{"tittel": "Post", "url": "mailto:kari@example.no"}]}),
                  _blokk("viktig", {"niva": "info", "tekst": "Send til kari@example.no"})):
        feil, _adv = _sjekk(dokument(blokk), epost=epost)
        assert len(feil) == 1 and "e-postadressen" in feil[0]["tekst"], blokk["type"]
    feil, _adv = _sjekk(dokument(tittel="Kurs for kari@example.no"), epost=epost)
    assert feil and feil[0]["blokk_id"] is None


def test_annen_epost_og_telefon_i_vanlig_tekst_gir_advarsel_ikke_feil():
    feil, adv = _sjekk(dokument(tekstblokk(html="<p>Ring 555 12 345 eller skriv til fremmed@example.no</p>")), epost={"kari@example.no"})
    assert feil == [] and len(adv) == 2
    feil, adv = _sjekk(dokument(_blokk("kontakt", {"personer": [{"navn": "Marte", "telefon": "555 12 345", "epost": "marte@example.no"}]})))
    assert feil == [] and adv == []                                                # i en Kontakt-blokk er det meningen


@pytest.mark.parametrize("tekst", ["Kurset går 2026-11-27 til 2026-11-29", "Tid: 09:00–16:00 og 09:15-10:45", "Dag 1: 26.11.2027 kl. 09.00",
                                   "Kursnr. 1001, 4 dager, 12 500 kr", "Bergen, 5003", "Rom 3, 2. etasje", "2027-03-10T09:00", "ISBN 978-82-15"])
def test_datoer_og_klokkeslett_gir_ikke_telefontreff(tekst):
    feil, adv = _sjekk(dokument(tekstblokk(html=f"<p>{tekst}</p>")))
    assert feil == [] and adv == [], tekst


@pytest.mark.parametrize("tekst", ["Ring 55 12 34 56", "Ring 55123456", "Ring 551 23 456", "Ring +47 55 12 34 56"])
def test_telefonnumre_gir_advarsel(tekst):
    assert len(_sjekk(dokument(tekstblokk(html=f"<p>{tekst}</p>")))[1]) == 1


def test_bilde_uten_alt_er_feil_og_med_alt_ok():
    filer = {3: {"type": "bilde", "filnavn": "rom.jpg"}}
    feil, _ = _sjekk(dokument(_blokk("bilde", {"fil_id": 3, "alt": ""})), filer=filer)
    assert len(feil) == 1 and "alternativ tekst" in feil[0]["tekst"]
    assert _sjekk(dokument(_blokk("bilde", {"fil_id": 3, "alt": "Kurslokalet"})), filer=filer)[0] == []


def test_fil_som_ikke_finnes_er_feil():
    feil, _ = _sjekk(dokument(_blokk("filer", {"filer": [{"fil_id": 9}]})), filer={})
    assert len(feil) == 1 and "ikke finnes lenger" in feil[0]["tekst"]


def test_advarsler_tom_blokk_tabell_ubrukt_fil_kursdag_og_lenke():
    dok = dokument(tekstblokk("Tom", "<p></p>"), _blokk("tabell", {"kolonner": ["Gruppe"], "rader": [["Gruppe 1"]]}, tittel="Grupper"),
                   _blokk("program", {"dager": [{"kursdag_id": 999, "punkter": [{"tema": "x"}]}]}),
                   _blokk("lenker", {"lenker": [{"tittel": "Uten adresse", "url": ""}, {"tittel": "Zoom", "url": "", "kilde": "zoom"}]}))
    feil, adv = _sjekk(dok, filer={5: {"type": "dokument", "filnavn": "ubrukt.pdf"}}, kursdager=[{"id": 1}])
    tekster = " | ".join(a["tekst"] for a in adv)
    assert feil == []
    for ventet in ("tom og vises ikke", "viser navn til alle", "ikke brukt på siden", "kursdag som ikke finnes", "mangler gyldig adresse"):
        assert ventet in tekster, ventet


def test_ren_side_gir_ingen_feil_og_ingen_advarsler():
    dok = dokument(tekstblokk(), _blokk("program", {"dager": [{"kursdag_id": 1, "punkter": [{"fra": "09:00", "til": "10:00", "tema": "Start"}]}]}))
    assert _sjekk(dok, kursdager=[{"id": 1}]) == ([], [])


# ============================ tabeller etter kurset ============================

def test_tom_tabeller_toemmer_rader_men_beholder_kolonner_og_andre_blokker():
    dok = dokument(_blokk("tabell", {"kolonner": ["Gruppe", "Deltakere"], "rader": [["1", "Kari N."], ["2", "Ola H."]]}), tekstblokk())
    ny, antall = si.tom_tabeller(dok)
    assert antall == 2 and ny["blokker"][0]["data"] == {"kolonner": ["Gruppe", "Deltakere"], "rader": []} and ny["blokker"][1] == dok["blokker"][1]
    assert si.tom_tabeller(ny)[1] == 0 and dok["blokker"][0]["data"]["rader"]      # inndata er uendret, og tom tabell gir 0


# ============================ maler og kopiering ============================

KURSDAGER = [{"id": 31, "dato": "2027-03-10", "start": "09:00", "slutt": "16:00", "samling": None},
             {"id": 32, "dato": "2027-03-11", "start": "09:00", "slutt": "16:00", "samling": None}]


def test_program_fra_kursdager_lager_en_dag_per_kursdag():
    dager = si.program_fra_kursdager(KURSDAGER)
    assert [(d["kursdag_id"], d["dato"], d["tittel"], d["punkter"]) for d in dager] == [
        (31, "2027-03-10", "Dag 1", []), (32, "2027-03-11", "Dag 2", [])]


def test_standardmal_har_de_fire_blokkene_og_er_gyldig():
    dok = si.standardmal(KURSDAGER)
    assert [b["type"] for b in dok["blokker"]] == ["program", "filer", "lenker", "kontakt"]
    assert dok["blokker"][1]["data"]["sharepoint"] is True and dok["blokker"][0]["data"]["dager"][0]["kursdag_id"] == 31
    ren, merknader = _valider(dok, kursdag_ider={31, 32})
    assert merknader == [] and len({b["id"] for b in ren["blokker"]}) == 4
    assert len(si.synlige_blokker(dok, IDAG)) >= 1                                              # tomme blokker vises ikke
    assert len(si.synlige_blokker(dok, IDAG)) < 4


def test_standardmalen_har_ingen_velkommen_tekstboks():
    """Camilla (02.10.2026): «Velkommen-boksen går ut av malen» - den sto ved siden av velkomsten øverst på siden."""
    for navn in ("standard", "program", "tom"):
        dok = si.mal(navn, KURSDAGER)
        assert not any(b["type"] == "tekst" or (b.get("tittel") or "").strip().lower() == "velkommen" for b in dok["blokker"]), navn


def test_mal_program_og_tom():
    assert [b["type"] for b in si.mal("program", KURSDAGER)["blokker"]] == ["program"]
    assert si.mal("tom", KURSDAGER) == si.tomt_dokument()
    with pytest.raises(Sidefeil):
        si.mal("finnes-ikke", KURSDAGER)


def test_ny_blokk_lager_gyldig_tom_blokk_av_alle_typer():
    for typ in si.BLOKKTYPER:
        b = si.ny_blokk(typ)
        assert b["type"] == typ and si.blokk_er_tom(b) and _valider(dokument(b))[0]["blokker"][0]["type"] == typ
    with pytest.raises(Sidefeil):
        si.ny_blokk("video")


def test_kopier_blokker_tommer_tabellrader_omkobler_filer_og_program_og_nullstiller_datoer():
    kilde = dokument(
        _blokk("tabell", {"kolonner": ["Gruppe", "Deltakere"], "rader": [["1", "Kari N."]]}, id="b_tabell01", vis_fra="2027-03-01"),
        _blokk("program", {"dager": [{"kursdag_id": 1, "dato": "2027-03-10", "tittel": "Dag 1", "punkter": [{"tema": "x"}]},
                                     {"kursdag_id": 2, "dato": "2027-03-11", "tittel": "Dag 2", "punkter": []},
                                     {"kursdag_id": 3, "dato": "2027-03-12", "tittel": "Dag 3", "punkter": []}]}, id="b_progr001"),
        _blokk("filer", {"filer": [{"fil_id": 1, "tittel": "A", "gruppe": 1, "synlig_fra": "2027-04-01"}, {"fil_id": 2, "tittel": "B"}]}, id="b_filer001", skjult=True),
        _blokk("bilde", {"fil_id": 1, "alt": "x"}, id="b_bilde001"), tekstblokk(id="b_ikkevalgt"))
    ut = si.kopier_blokker(kilde, ["b_tabell01", "b_progr001", "b_filer001", "b_bilde001"], fil_kart={1: 101},
                           kursdager=[{"id": 31, "dato": "2028-01-10"}, {"id": 32, "dato": "2028-01-11"}])
    assert [b["type"] for b in ut] == ["tabell", "program", "filer", "bilde"]
    assert not ({"b_tabell01", "b_progr001", "b_filer001", "b_bilde001"} & {b["id"] for b in ut}) and len({b["id"] for b in ut}) == 4
    assert ut[0]["data"]["rader"] == [] and ut[0]["vis_fra"] is None
    dager = ut[1]["data"]["dager"]
    assert [(d["kursdag_id"], d["dato"]) for d in dager] == [(31, "2028-01-10"), (32, "2028-01-11"), (None, None)]
    assert ut[2]["data"]["filer"] == [{"fil_id": 101, "tittel": "A", "gruppe": None, "synlig_fra": None}] and ut[2]["skjult"] is True
    assert ut[3]["data"]["fil_id"] == 101
    assert kilde["blokker"][0]["data"]["rader"] == [["1", "Kari N."]]              # kilden er uendret
