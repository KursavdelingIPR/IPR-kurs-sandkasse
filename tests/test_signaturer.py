"""Signaturbiblioteket (E-postmaler → Signaturer) og signaturene i e-postene (kurs/signaturer.py).

Kravene (planen 26.09.2026, brukerens beslutninger og bestillingen 27.09.2026):
  * mange lagrede signaturer; én hovedsignatur per bruker; én standard (reserve) som brukes når ingenting annet er valgt
  * rik tekst, lenker og logo/bilder - trygge, innebygde bilder, aldri eksterne bilder eller sporingspiksler
  * manuell e-post får avsenderens hovedsignatur automatisk; den kan byttes, og redigeres for én e-post uten at den lagrede
    endres
  * et kurs kan velge signatur, eller «Tilpass for dette kurset»; påminnelser, kursbevis, avlysning og avslag bruker kursets
    signatur, ellers standard; bekreftelser har ingen
  * en signatur som er i bruk, kan ikke slettes før den er byttet ut
  * signaturen slik den faktisk ble sendt, er en del av kopien i e-posthistorikken
  * signaturen renses på serveren når den lagres OG når den sendes
"""
import base64
import io
import re
import struct
import zlib
from datetime import date, timedelta

import pytest

from kurs import config, daglig, db, maltekster, migreringer, signaturer, sveiper
from kurs.integrasjoner import epost
from kurs.kjoring import Kjoring

IDAG = date(2027, 3, 1)
BILDER = {1: "IPR-logo"}


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    return c


def _fersk():
    return db.koble(config.DB_STI)


def _klient(brukernavn=None, passord=None):
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": brukernavn or config.ADMIN_BRUKERNAVN,
                                    "passord": passord or config.ADMIN_PASSORD})
    return k


def _admin_id(con):
    return con.execute("SELECT id FROM admin_bruker WHERE brukernavn=?", (config.ADMIN_BRUKERNAVN,)).fetchone()[0]


def png(bredde=120, hoyde=40, farge=b"\x1f\x5f\x7a") -> bytes:
    def bit(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    rader = (b"\x00" + farge * bredde) * hoyde
    return (b"\x89PNG\r\n\x1a\n" + bit(b"IHDR", struct.pack(">IIBBBBB", bredde, hoyde, 8, 2, 0, 0, 0))
            + bit(b"IDAT", zlib.compress(rader)) + bit(b"IEND", b""))


def jpeg(bredde=300, hoyde=80) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, hoyde, bredde, 3) + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9" + b"\x00" * 16


def gif(bredde=64, hoyde=64) -> bytes:
    return b"GIF89a" + struct.pack("<HH", bredde, hoyde) + b"\x80\x00\x00" + b"\x00" * 30 + b";"


def _bilde(con, data=None, alt="IPR-logo", filnavn="logo.png") -> int:
    bid = signaturer.lagre_bilde(con, filnavn, data or png(), alt, aktor="admin:test")
    con.commit()
    return bid


def _signatur(con, navn="Camilla", innhold="<b>Camilla Eksempel</b><br>Kursadministrasjonen") -> int:
    sid = signaturer.opprett(con, navn, innhold, aktor="admin:test")
    con.commit()
    return sid


def _kurs(con, kode="S1", start=IDAG + timedelta(days=7), dager=2, **kw):
    datoer = [(start + timedelta(days=n)).isoformat() for n in range(dager)]
    kid = db.opprett_kurs(con, kode=kode, navn="Veiledning i praksis", datoer=datoer, sharepoint_mappe=f"Kurs/{kode}",
                          **{"pris_nok": 0, "type": "fysisk", "sted": "Oslo", **kw})
    con.commit()
    return kid


def _meld_paa(con, kid, epost_="kari@example.no", fornavn="Kari"):
    pid, _ = db.meld_paa(con, kid, epost=epost_, fornavn=fornavn, etternavn="Eksempel")
    con.commit()
    return pid


def _kopier(**hvor):
    sql = "SELECT * FROM sendt_epost" + (" WHERE " + " AND ".join(f"{k}=?" for k in hvor) if hvor else "") + " ORDER BY id"
    return _fersk().execute(sql, tuple(hvor.values())).fetchall()


def _manuell(k, kid, pid, **data):
    side = k.post(f"/admin/kurs/{kid}/epost/forhandsvis", data={"emne": "Husk boka", "tekst": "Hei {fornavn}!",
                                                                "paamelding_id": str(pid), **data})
    treff = re.search(r'name="utsending_id" value="(\d+)"', side.get_data(as_text=True))
    assert treff, side.get_data(as_text=True)[:2000]
    k.post(f"/admin/kurs/{kid}/epost/send", data={"utsending_id": treff[1]})
    return treff[1]


# ============================ rensing: kjente angrepsmønstre ============================

@pytest.mark.parametrize("farlig", [
    '<script>alert(1)</script>',
    '<SCRIPT SRC="https://evil.example/x.js"></SCRIPT>',
    '<style>body{background:url(https://evil.example/t.gif)}</style>',
    '<iframe src="https://evil.example"></iframe>',
    '<object data="x.swf"></object><embed src="x.swf">',
    '<svg onload="alert(1)"><circle r="1"/></svg>',
    '<img src="x" onerror="alert(1)">',
    '<img src="https://evil.example/sporing.gif" alt="">',
    '<img src="data:image/png;base64,iVBORw0KGgo=" alt="">',
    '<img src="/admin/signaturer/bilde/999" alt="finnes ikke">',
    '<a href="javascript:alert(1)">klikk</a>',
    '<a href="JaVaScRiPt:alert(1)">klikk</a>',
    '<a href="java&#x09;script:alert(1)">klikk</a>',
    '<a href=" javascript:alert(1)">klikk</a>',
    '<a href="data:text/html,<script>alert(1)</script>">klikk</a>',
    '<a href="//evil.example">klikk</a>',
    '<a href="/admin/brukere">klikk</a>',
    '<b onclick="alert(1)" onmouseover="alert(2)">fet</b>',
    '<span style="background:url(https://evil.example/t.gif)">x</span>',
    '<span style="width:expression(alert(1))">x</span>',
    '<span style="position:fixed;top:0;left:0">x</span>',
    '<div style="-moz-binding:url(x.xml#xss)">x</div>',
    '<form action="https://evil.example"><input name="passord"><button>OK</button></form>',
    '<meta http-equiv="refresh" content="0;url=https://evil.example">',
    '<base href="https://evil.example/">',
    '<link rel="stylesheet" href="https://evil.example/x.css">',
    '<math><mi xlink:href="javascript:alert(1)">x</mi></math>',
    '<template><img src=x onerror=alert(1)></template>',
    '<noscript><img src=x onerror=alert(1)></noscript>',
    '<!--<img src=x onerror=alert(1)>-->',
    '<![CDATA[<script>alert(1)</script>]]>',
    '<div><script>alert(1)',
    '<img src=x onerror=alert(1)//',
    '"><script>alert(1)</script>',
])
def test_farlig_innhold_fjernes(farlig):
    ren = signaturer.rens("Hei " + farlig + " hilsen", BILDER)
    lav = ren.lower()
    for spor in ("<script", "<style", "<iframe", "<object", "<embed", "<svg", "<form", "<input", "<meta", "<base",
                 "<link", "<math", "<template", "<noscript", "javascript", "onerror", "onclick", "onload", "onmouseover",
                 "expression", "url(", "evil.example", "data:", "position", "binding", "<!--", "/admin/brukere"):
        assert spor not in lav, (spor, ren)
    assert "Hei" in ren
    if farlig not in UAVSLUTTET:            # teksten etter et fjernet element beholdes (uavsluttet: som i nettleseren)
        assert "hilsen" in ren


UAVSLUTTET = ('<div><script>alert(1)', '<img src=x onerror=alert(1)//')


def test_innholdet_i_skript_stiler_og_rammer_fjernes_helt():
    assert signaturer.rens("A<script>alert(1)</script><style>b{}</style><iframe>skjult</iframe>"
                           "<noscript>n</noscript><template>t</template>B", {}) == "AB"


def test_trygg_formatering_beholdes_og_normaliseres():
    ren = signaturer.rens(
        '<div style="text-align: center;"><b>Camilla</b> <i>Eksempel</i> <u>IPR</u></div>'
        '<span style="color: rgb(31, 95, 122); font-size: large; font-family: Georgia;">Tittel</span>'
        '<font color="#b3261e" size="2" face="Verdana">Gammel</font>'
        '<a href="https://ipr.no">ipr.no</a> <a href="mailto:kurs@example.no">E-post</a> <a href="tel:+47 55 00 00 00">Ring</a>'
        '<img src="/admin/signaturer/bilde/1" width="120">', BILDER)
    assert '<div style="text-align: center"><b>Camilla</b> <i>Eksempel</i> <u>IPR</u></div>' in ren
    assert '<span style="color: #1f5f7a; font-size: large; font-family: Georgia, serif">Tittel</span>' in ren
    assert '<span style="color: #b3261e; font-size: small; font-family: Verdana, Geneva, sans-serif">Gammel</span>' in ren
    assert '<a href="https://ipr.no">ipr.no</a>' in ren and '<a href="mailto:kurs@example.no">' in ren
    assert '<a href="tel:+4755000000">Ring</a>' in ren
    assert '<img src="/admin/signaturer/bilde/1" alt="IPR-logo" width="120">' in ren     # alternativ tekst fra biblioteket


def test_tekst_og_attributter_escapes():
    ren = signaturer.rens('3 < 4 & "sitat" <a href="https://ipr.no/?a=1&b=2">x</a><img src="/admin/signaturer/bilde/1" '
                          'alt="Logo &quot;IPR&quot; <b>">', BILDER)
    assert "3 &lt; 4 &amp;" in ren and 'href="https://ipr.no/?a=1&amp;b=2"' in ren
    assert 'alt="Logo &quot;IPR&quot; &lt;b&gt;"' in ren


@pytest.mark.parametrize("src", ["/admin/signaturer/bilde/999", "https://evil.example/pixel/1", "/admin/signaturer/bilde/1x",
                                 "/static/../admin/signaturer/bilde/1"])
def test_bare_bilder_som_finnes_i_biblioteket(src):
    assert signaturer.rens(f'Hilsen<img src="{src}" alt="x">', BILDER) == "Hilsen"


def test_bildeadresse_fra_kopiering_blir_bibliotekets_adresse():
    assert signaturer.rens('<img src="https://kurs.ipr.no/admin/signaturer/bilde/1">', BILDER) == \
        '<img src="/admin/signaturer/bilde/1" alt="IPR-logo">'


def test_skrift_utenfor_listen_og_ukjente_stiler_fjernes():
    assert signaturer.rens('<span style="font-family: Comic Sans MS; color: #zzz; font-size: 200px">x</span>', {}) == \
        "<span>x</span>"


def test_tomme_linjer_til_slutt_fjernes_og_for_lang_signatur_avvises():
    assert signaturer.rens("Hilsen<br><div><br></div><br>&nbsp; ", {}) == "Hilsen"
    with pytest.raises(signaturer.Signaturfeil, match="for lang"):
        signaturer.rens("x" * (signaturer.TEGN_MAKS + 1), {})


# ============================ bilder ============================

def test_png_jpeg_og_gif_godtas_ut_fra_innholdet(con):
    assert signaturer.les_bilde(png(120, 40)) == ("image/png", 120, 40)
    assert signaturer.les_bilde(jpeg(300, 80)) == ("image/jpeg", 300, 80)
    assert signaturer.les_bilde(gif(64, 32)) == ("image/gif", 64, 32)
    bid = _bilde(con, jpeg(), filnavn="../../hemmelig/logo.png")    # filnavnet bestemmer aldri typen
    r = con.execute("SELECT filnavn, mimetype, bredde, alt FROM signatur_bilde WHERE id=?", (bid,)).fetchone()
    assert tuple(r) == ("logo.png", "image/jpeg", 300, "IPR-logo")


@pytest.mark.parametrize("data,feil", [
    (b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>', "PNG, JPEG og GIF"),
    (b"%PDF-1.4 ...", "PNG, JPEG og GIF"),
    (b"<html><script>alert(1)</script></html>", "PNG, JPEG og GIF"),
    (png(signaturer.BILDE_MAKS_BREDDE + 1, 10), "for bredt"),
    (png(10, 10) + b"\x00" * signaturer.BILDE_MAKS_BYTE, "for stort"),
], ids=["svg", "pdf", "html", "for-bredt", "for-stort"])
def test_farlige_og_for_store_bilder_avvises(con, data, feil):
    with pytest.raises(signaturer.Signaturfeil, match=feil):
        signaturer.lagre_bilde(con, "logo.png", data, "Logo", aktor="admin:test")


def test_bilde_maa_ha_alternativ_tekst(con):
    with pytest.raises(signaturer.Signaturfeil, match="alternativ tekst"):
        signaturer.lagre_bilde(con, "logo.png", png(), "  ", aktor="admin:test")


def test_bildet_vises_bare_for_innloggede_og_aldri_som_side(con):
    bid = _bilde(con)
    from kurs.web import app as webapp
    assert webapp.app.test_client().get(f"/admin/signaturer/bilde/{bid}").status_code == 302      # ikke innlogget
    r = _klient().get(f"/admin/signaturer/bilde/{bid}")
    assert r.status_code == 200 and r.data == png() and r.headers["Content-Type"] == "image/png"
    assert r.headers["X-Content-Type-Options"] == "nosniff" and "sandbox" in r.headers["Content-Security-Policy"]


def test_opplasting_via_siden(con):
    k = _klient()
    r = k.post("/admin/signaturer/bilder", data={"alt": "IPR-logo", "bilde": (io.BytesIO(png()), "logo.png")},
               content_type="multipart/form-data")
    assert r.status_code == 302 and _fersk().execute("SELECT COUNT(*) FROM signatur_bilde").fetchone()[0] == 1
    r = k.post("/admin/signaturer/bilder", data={"alt": "Ond", "bilde": (io.BytesIO(b"<svg onload=alert(1)>"), "x.png")},
               content_type="multipart/form-data")
    assert "PNG, JPEG og GIF" in k.get(r.headers["Location"]).get_data(as_text=True)
    assert _fersk().execute("SELECT COUNT(*) FROM signatur_bilde").fetchone()[0] == 1


def test_bilde_i_bruk_kan_ikke_slettes(con):
    bid = _bilde(con)
    sid = _signatur(con, innhold=f'<img src="/admin/signaturer/bilde/{bid}">')
    with pytest.raises(signaturer.Signaturfeil, match="Camilla"):
        signaturer.slett_bilde(con, bid, aktor="admin:test")
    signaturer.oppdater(con, sid, "Camilla", "Uten logo", 1, aktor="admin:test")
    signaturer.slett_bilde(con, bid, aktor="admin:test")
    assert con.execute("SELECT COUNT(*) FROM signatur_bilde").fetchone()[0] == 0


def test_bildene_i_en_signatur_maa_vaere_under_to_mb_til_sammen(con, monkeypatch):
    a, b = _bilde(con, png(100, 100, b"\x01\x02\x03")), _bilde(con, png(100, 90, b"\x07\x08\x09"), alt="Banner")
    storrelse = con.execute("SELECT SUM(storrelse) FROM signatur_bilde").fetchone()[0]
    monkeypatch.setattr(signaturer, "BILDER_MAKS_PER_EPOST", storrelse - 1)     # hvert bilde er under grensen
    signaturer.opprett(con, "Ett bilde", f'<img src="/admin/signaturer/bilde/{a}">', aktor="admin:test")
    with pytest.raises(signaturer.Signaturfeil, match="2 MB"):
        signaturer.opprett(con, "Stor", f'<img src="/admin/signaturer/bilde/{a}"><img src="/admin/signaturer/bilde/{b}">',
                           aktor="admin:test")


# ============================ biblioteket ============================

def test_migreringen_lager_standardsignaturen_med_dagens_hilsen(con):
    (s,) = con.execute("SELECT navn, innhold, standard FROM signatur").fetchall()
    assert tuple(s) == ("Kursadministrasjonen", "Vennlig hilsen<br>Kursadministrasjonen, Institutt for Psykologisk Rådgivning", 1)


def test_migreringen_kan_kjores_paa_nytt_uten_ny_standardsignatur(con):
    nr = next(n for n, navn, _ in migreringer.MIGRERINGER if navn == "signaturer")
    con.execute("DELETE FROM schema_versjon WHERE versjon>=?", (nr,))
    con.commit()
    assert migreringer.kjor_manglende(con) == list(range(nr, migreringer.KODEVERSJON + 1))
    assert con.execute("SELECT COUNT(*) FROM signatur WHERE standard=1").fetchone()[0] == 1
    for tabell, kolonne in (("admin_bruker", "hovedsignatur_id"), ("kurs", "signatur_id"), ("kurs", "signatur_html"),
                            ("admin_utsending", "signatur_html")):
        assert db.har_kolonne(con, tabell, kolonne)


def test_standardsignaturen_gir_noyaktig_samme_epost_som_for():
    """Ingenting endrer seg før dere lager egne signaturer: samme HTML med og uten standardsignaturen."""
    data = dict(d={"navn": "Kari Eksempel", "fornavn": "Kari"}, kurs={"navn": "Veiledning", "type": "fysisk"})
    uten = epost.render("avlysning", **data)
    med = epost.render("avlysning", signatur=signaturer.som_markup(signaturer.STANDARD_INNHOLD), **data)
    assert med == uten


def test_opprett_rediger_navn_og_versjonsvern(con):
    sid = _signatur(con)
    with pytest.raises(signaturer.Signaturfeil, match="finnes allerede"):
        signaturer.opprett(con, " camilla ", "x", aktor="admin:test")
    signaturer.oppdater(con, sid, "Camilla E.", "<i>Ny</i>", 1, aktor="admin:test")
    with pytest.raises(signaturer.Signaturfeil, match="endret av noen andre"):
        signaturer.oppdater(con, sid, "Camilla", "<b>Gammel</b>", 1, aktor="admin:test")   # skjemaet var åpnet i v1
    r = signaturer.hent(con, sid)
    assert (r["navn"], r["innhold"], r["versjon"], r["endret_av"]) == ("Camilla E.", "<i>Ny</i>", 2, "admin:test")


def test_bare_en_standard(con):
    sid = _signatur(con)
    signaturer.sett_standard(con, sid, aktor="admin:test")
    assert [r["navn"] for r in con.execute("SELECT navn FROM signatur WHERE standard=1")] == ["Camilla"]
    assert signaturer.standard(con)["id"] == sid


def test_signatur_i_bruk_kan_ikke_slettes_for_den_er_byttet_ut(con):
    sid = _signatur(con)
    kid = _kurs(con)
    anne = db.opprett_admin_bruker(con, "anne", "Anne Eksempel", "passord-som-holder")
    signaturer.sett_hovedsignatur(con, anne, sid, aktor="admin:test")
    signaturer.sett_for_kurs(con, kid, "bibliotek", signatur_id=sid, aktor="admin:test")
    with pytest.raises(signaturer.Signaturfeil, match="hovedsignaturen til Anne Eksempel.*Veiledning i praksis"):
        signaturer.slett(con, sid, aktor="admin:test")
    signaturer.sett_hovedsignatur(con, anne, None, aktor="admin:test")
    signaturer.sett_for_kurs(con, kid, "standard", aktor="admin:test")
    signaturer.slett(con, sid, aktor="admin:test")
    assert signaturer.hent(con, sid) is None
    with pytest.raises(signaturer.Signaturfeil, match="standardsignatur"):
        signaturer.slett(con, signaturer.standard(con)["id"], aktor="admin:test")


def test_sletting_rydder_koblinger_som_ikke_er_i_bruk(con):
    """En deaktivert bruker og et kurs med egen, tilpasset versjon bruker den ikke - de hindrer ikke sletting."""
    sid = _signatur(con)
    kid = _kurs(con)
    gammel = db.opprett_admin_bruker(con, "gammel", "Gammel Bruker", "passord-som-holder")
    signaturer.sett_hovedsignatur(con, gammel, sid, aktor="admin:test")
    con.execute("UPDATE admin_bruker SET aktiv=0 WHERE id=?", (gammel,))
    signaturer.sett_for_kurs(con, kid, "egen", signatur_id=sid, innhold="<b>Kursets egen</b>", aktor="admin:test")
    signaturer.slett(con, sid, aktor="admin:test")
    assert con.execute("SELECT hovedsignatur_id FROM admin_bruker WHERE id=?", (gammel,)).fetchone()[0] is None
    assert tuple(con.execute("SELECT signatur_id, signatur_html FROM kurs WHERE id=?", (kid,)).fetchone()) == (
        None, "<b>Kursets egen</b>")


def test_endringer_logges_med_hvem(con):
    sid = _signatur(con)
    signaturer.oppdater(con, sid, "Camilla", "Ny", 1, aktor="admin:camilla")
    handlinger = [(r["handling"], r["aktor"]) for r in con.execute("SELECT handling, aktor FROM hendelse ORDER BY id")
                  if r["handling"].startswith("signatur")]
    assert handlinger == [("signatur_opprettet", "admin:test"), ("signatur_endret", "admin:camilla")]


def test_lesetilgang_kan_se_men_ikke_endre(con):
    sid = _signatur(con)
    db.opprett_admin_bruker(con, "leser", "Lise Leser", "passord-som-holder", rolle="lese")
    con.commit()
    k = _klient("leser", "passord-som-holder")
    side = k.get("/admin/signaturer").get_data(as_text=True)
    assert "Camilla" in side and "Ny signatur" not in side and "contenteditable" not in k.get(
        f"/admin/signaturer/{sid}").get_data(as_text=True)
    for url, data in ((f"/admin/signaturer/{sid}", {"navn": "x", "innhold": "x", "versjon": "1"}),
                      (f"/admin/signaturer/{sid}/slett", {}), ("/admin/signaturer/ny", {"navn": "x", "innhold": "x"}),
                      ("/admin/signaturer/hovedsignatur", {"signatur_id": str(sid)})):
        assert k.post(url, data=data).status_code == 403
    assert signaturer.hent(con, sid)["innhold"] == "<b>Camilla Eksempel</b><br>Kursadministrasjonen"


# ============================ sidene ============================

def test_signatursiden_viser_biblioteket_bruk_og_bilder(con):
    sid = _signatur(con)
    signaturer.sett_hovedsignatur(con, _admin_id(con), sid, aktor="admin:test")
    _bilde(con)
    con.commit()
    side = _klient().get("/admin/signaturer").get_data(as_text=True)
    assert "Standard (reserve)" in side and "hovedsignatur for Standardbruker" in side
    assert '<b>Camilla Eksempel</b><br>Kursadministrasjonen' in side and "IPR-logo" in side
    assert 'aria-current="page">E-postmaler</a>' in side and '<a class="aktiv" href="/admin/signaturer">Signaturer</a>' in side


def test_editoren_har_verktoylinje_og_ingen_innebygd_skript(con):
    sid = _signatur(con, innhold='<b>Camilla</b><script>alert(1)</script>')
    side = _klient().get(f"/admin/signaturer/{sid}").get_data(as_text=True)
    assert 'data-kommando="bold"' in side and 'contenteditable="true"' in side and 'name="innhold"' in side
    assert "alert(1)" not in side and "onclick" not in side
    for m in re.finditer(r"<script\b[^>]*>", side):
        assert "nonce=" in m.group(0)
    assert '/static/signatur.js' in side and '/static/signatur.css' in side


def test_lagring_fra_siden_renser_innholdet(con):
    k = _klient()
    r = k.post("/admin/signaturer/ny", data={"navn": "Camilla", "innhold": '<b>Camilla</b><img src=x onerror=alert(1)>'
                                                                           '<a href="javascript:alert(1)">lenke</a>'})
    assert r.status_code == 302
    (s,) = _fersk().execute("SELECT innhold FROM signatur WHERE navn='Camilla'").fetchall()
    assert s["innhold"] == "<b>Camilla</b>lenke"


# ============================ manuell e-post ============================

def test_manuell_epost_har_hovedsignaturen_ferdig_utfylt(con):
    sid = _signatur(con)
    signaturer.sett_hovedsignatur(con, _admin_id(con), sid, aktor="admin:test")
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    con.commit()
    side = _klient().post(f"/admin/kurs/{kid}/epost/ny", data={"paamelding_id": str(pid)}).get_data(as_text=True)
    assert f'<option value="{sid}" selected>Camilla</option>' in side
    assert 'value="&lt;b&gt;Camilla Eksempel&lt;/b&gt;&lt;br&gt;Kursadministrasjonen"' in side     # skjult felt
    assert "<b>Camilla Eksempel</b><br>Kursadministrasjonen</div>" in side                        # i editoren


def test_uten_hovedsignatur_brukes_standard(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    side = _klient().post(f"/admin/kurs/{kid}/epost/ny", data={"paamelding_id": str(pid)}).get_data(as_text=True)
    assert f'<option value="{signaturer.standard(con)["id"]}" selected>Kursadministrasjonen (standard)</option>' in side


def test_redigert_signatur_gjelder_bare_denne_eposten(con):
    sid = _signatur(con)
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    k = _klient()
    _manuell(k, kid, pid, signatur_valg=str(sid), signatur_html="<b>Camilla</b> – bare i dag<script>x</script>")
    (kopi,) = _kopier(paamelding_id=pid, mal="admin_melding")
    assert "<b>Camilla</b> – bare i dag<br>" in kopi["html"] and "<script" not in kopi["html"]
    assert signaturer.hent(con, sid)["innhold"] == "<b>Camilla Eksempel</b><br>Kursadministrasjonen"   # biblioteket urørt


def test_annen_signatur_kan_velges_uten_javascript(con):
    sid = _signatur(con, "Kurs", "<i>Kursavdelingen</i>")
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    _manuell(_klient(), kid, pid, signatur_valg=str(sid))
    (kopi,) = _kopier(paamelding_id=pid, mal="admin_melding")
    assert "<i>Kursavdelingen</i><br>" in kopi["html"] and "Vennlig hilsen" not in kopi["html"]


def test_logo_sendes_som_innebygd_bilde_og_lagres_i_kopien(con):
    bid = _bilde(con)
    sid = _signatur(con, innhold=f'<div><b>Camilla</b></div><img src="/admin/signaturer/bilde/{bid}" width="120">')
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    _manuell(_klient(), kid, pid, signatur_valg=str(sid))
    (kopi,) = _kopier(paamelding_id=pid, mal="admin_melding")
    assert f'src="cid:signaturbilde-{bid}@ipr"' in kopi["html"] and "/admin/signaturer/bilde" not in kopi["html"]
    assert re.search(r'<div style="margin-top:28px[^"]*">\s*<div><b>Camilla</b></div>', kopi["html"])   # blokk: <div>
    (v,) = _fersk().execute("SELECT v.innebygd_cid, f.innhold FROM sendt_epost_vedlegg v JOIN epost_fil f "
                            "ON f.sha256=v.sha256 WHERE v.sendt_epost_id=?", (kopi["id"],)).fetchall()
    assert v["innebygd_cid"] == f"signaturbilde-{bid}@ipr" and base64.b64decode(v["innhold"]) == png()
    utboks = next(config.UTBOKS.glob("*.html")).read_text(encoding="utf-8")
    assert "data:image/png;base64," in utboks                          # sandkassens utboks viser logoen


def test_signaturen_i_sendt_epost_endres_ikke_naar_biblioteket_endres(con):
    sid = _signatur(con)
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    _manuell(_klient(), kid, pid, signatur_valg=str(sid))
    signaturer.oppdater(con, sid, "Camilla", "<b>Helt ny signatur</b>", 1, aktor="admin:test")
    con.commit()
    (kopi,) = _kopier(paamelding_id=pid, mal="admin_melding")
    assert "Camilla Eksempel" in kopi["html"] and "Helt ny signatur" not in kopi["html"]
    detalj = _klient().get(f"/admin/kurs/{kid}/deltaker/{pid}/epost/{kopi['id']}").get_data(as_text=True)
    assert "Camilla Eksempel" in detalj


def test_signaturen_renses_ogsaa_naar_den_sendes(con):
    """Selv om noe farlig skulle ha kommet inn i databasen utenom rensingen, sendes det aldri."""
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    uid, _ = db.opprett_admin_utsending(con, kid, "Emne", "Tekst", [pid], None,
                                        signatur_html='<b>Hilsen</b><img src="https://evil.example/t.gif"><script>x</script>')
    con.commit()
    Kjoring(con, idag=IDAG).send_admin_utsending(uid)
    (kopi,) = _kopier(paamelding_id=pid)
    assert "<b>Hilsen</b>" in kopi["html"] and "evil.example" not in kopi["html"] and "<script" not in kopi["html"]


def test_kursets_signatur_renses_ogsaa_naar_den_sendes(con):
    kid = _kurs(con)
    con.execute("UPDATE kurs SET signatur_html=? WHERE id=?",
                ('<b>Kursleder</b><iframe src="https://evil.example"></iframe><a href="javascript:x()">lenke</a>', kid))
    con.commit()
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    _, html = Kjoring(con, idag=IDAG).render_for_sending("avlysning", d={"navn": "Kari", "fornavn": "Kari"}, kurs=kurs)
    assert "<b>Kursleder</b>lenke<br>" in html and "iframe" not in html and "javascript" not in html


def test_bilde_som_er_borte_utelates_i_stedet_for_aa_sende_en_brutt_lenke(con):
    bid = _bilde(con)
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    uid, _ = db.opprett_admin_utsending(con, kid, "Emne", "Tekst", [pid], None,
                                        signatur_html=f'Hilsen<img src="/admin/signaturer/bilde/{bid}">')
    con.execute("DELETE FROM signatur_bilde")         # f.eks. slettet mens e-posten lå til forhåndsvisning
    con.commit()
    Kjoring(con, idag=IDAG).send_admin_utsending(uid)
    (kopi,) = _kopier(paamelding_id=pid)
    assert "Hilsen<br>" in kopi["html"] and "signaturbilde" not in kopi["html"]


def test_eldre_utsendelse_uten_signatur_har_den_faste_hilsenen(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    uid, _ = db.opprett_admin_utsending(con, kid, "Emne", "Tekst", [pid], None)       # signatur_html = NULL
    con.commit()
    Kjoring(con, idag=IDAG).send_admin_utsending(uid)
    (kopi,) = _kopier(paamelding_id=pid)
    assert "Vennlig hilsen<br>Kursadministrasjonen, Institutt for Psykologisk Rådgivning<br>" in kopi["html"]


def test_manglende_bilde_stopper_sendingen_foer_noe_er_reservert(con):
    bid = _bilde(con)
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    uid, nokkel = db.opprett_admin_utsending(con, kid, "Emne", "Tekst", [pid], None,
                                             signatur_html=f'<img src="/admin/signaturer/bilde/{bid}">')
    con.execute("DELETE FROM signatur_bilde")        # f.eks. slettet i et kappløp
    con.commit()
    with pytest.raises(maltekster.MalFeil):
        Kjoring(con, idag=IDAG).send_ferdigrendret_en_gang(nokkel, "kari@example.no", "admin_epost", "Emne",
                                                          f'<img src="cid:signaturbilde-{bid}@ipr">', paamelding_id=pid)
    assert con.execute("SELECT COUNT(*) FROM utsending_logg").fetchone()[0] == 0 and _kopier() == []


# ============================ automatiske e-poster og kursets signatur ============================

def test_kursets_signatur_i_paaminnelse_kursbevis_avlysning_og_avslag(con):
    sid = _signatur(con, "Kursleder", "<b>Kari Kursleder</b>")
    kid = _kurs(con)
    signaturer.sett_for_kurs(con, kid, "bibliotek", signatur_id=sid, aktor="admin:test")
    con.commit()
    k = Kjoring(con, idag=IDAG)
    kurs = db.hent_kurs(con, kid) if hasattr(db, "hent_kurs") else con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    d = {"navn": "Kari Eksempel", "fornavn": "Kari"}
    for mal, data in (("avlysning", dict(d=d, kurs=kurs)), ("avslag", dict(d=d, kurs=kurs, melding="")),
                      ("kursbevis_klar", dict(navn="Kari Eksempel", fornavn="Kari", kurs=kurs))):
        _, html = k.render_for_sending(mal, **data)
        assert "<b>Kari Kursleder</b><br>" in html and "Vennlig hilsen" not in html, mal


def test_paaminnelsen_fra_morgenjobben_har_kursets_egen_signatur(con):
    kid = _kurs(con, start=IDAG + timedelta(days=1))
    pid = _meld_paa(con, kid)
    sveiper.kjor(Kjoring(con, idag=IDAG), pid)
    signaturer.sett_for_kurs(con, kid, "egen", innhold="<i>Hilsen kurslederne</i>", aktor="admin:test")
    con.commit()
    daglig.kjor(Kjoring(con, idag=IDAG))
    kopier = {r["mal"]: r["html"] for r in _kopier(paamelding_id=pid)}
    assert "<i>Hilsen kurslederne</i><br>" in kopier["dagfor"]
    assert "Vennlig hilsen<br>Kursadministrasjonen" in kopier["bekreftelse"]     # bekreftelsen har ingen signatur


def test_kurs_uten_valg_bruker_standardsignaturen_ogsaa_naar_den_endres(con):
    kid = _kurs(con)
    signaturer.oppdater(con, signaturer.standard(con)["id"], "Kursadministrasjonen", "<b>IPR Kurs</b>", 1,
                        aktor="admin:test")
    con.commit()
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    _, html = Kjoring(con, idag=IDAG).render_for_sending("avlysning", d={"navn": "Kari", "fornavn": "Kari"}, kurs=kurs)
    assert "<b>IPR Kurs</b><br>" in html


def test_tilpass_for_kurset_endrer_ikke_biblioteket(con):
    sid = _signatur(con)
    kid = _kurs(con)
    k = _klient()
    r = k.post(f"/admin/kurs/{kid}/signatur", data={"modus": "egen", "signatur_id": str(sid),
                                                     "innhold": "<b>Camilla</b> – for dette kurset"})
    assert r.status_code == 302
    assert signaturer.kursvalg(_fersk(), kid)["innhold"] == "<b>Camilla</b> – for dette kurset"
    assert signaturer.hent(_fersk(), sid)["innhold"] == "<b>Camilla Eksempel</b><br>Kursadministrasjonen"
    side = k.get(f"/admin/kurs/{kid}/kommunikasjon").get_data(as_text=True)
    assert "Tilpasset for dette kurset (basert på «Camilla»)" in side


def test_kurssiden_viser_valgene_og_forhandsvisning_av_maler_viser_signaturen(con):
    bid = _bilde(con)
    signaturer.oppdater(con, signaturer.standard(con)["id"], "Kursadministrasjonen",
                        f'Hilsen IPR<img src="/admin/signaturer/bilde/{bid}">', 1, aktor="admin:test")
    kid = _kurs(con)
    con.commit()
    k = _klient()
    side = k.get(f"/admin/kurs/{kid}/signatur").get_data(as_text=True)
    assert 'value="standard"' in side and 'value="bibliotek"' in side and 'value="egen"' in side
    assert "Tilpass for dette kurset" in side
    forhandsvis = k.get("/admin/e-postmaler/avlysning/forhandsvis").get_data(as_text=True)
    assert f'src="/admin/signaturer/bilde/{bid}"' in forhandsvis and "cid:" not in forhandsvis
    assert "standardsignaturen" in forhandsvis


# ---------- innlimte bilder (Camilla 03.10.2026: «Må kunne skrive det vi vil i signaturen») ----------

def _data_url(data: bytes, mime="image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def test_innlimte_bilder_flyttes_til_biblioteket_og_teller_ikke_mot_lengden(con):
    """Standardsignaturen med innlimt logo, logo nr. 2 og et bredt banner: hundrevis av kB tekst som data:-adresser.
    Bildene lagres i biblioteket, og signaturen blir kort nok."""
    logo, logo2, banner = png(300, 80), jpeg(260, 90), png(1800, 300, farge=b"\x10\x20\x30")
    stor_logo = logo + b"\x00" * (signaturer.TEGN_MAKS * 3)          # gjør data:-adressen lengre enn den gamle grensen
    innhold = (f'Vennlig hilsen<br><img src="{_data_url(stor_logo)}" alt="IPR"> '
               f"<img src='{_data_url(logo2, 'image/jpeg')}'><br><img width=\"600\" src=\"{_data_url(banner)}\">")
    sid = signaturer.opprett(con, "Med bilder", innhold, aktor="test")
    lagret = signaturer.hent(con, sid)["innhold"]
    assert "data:" not in lagret and len(lagret) < 500
    ider = signaturer.bilde_ider(lagret)
    assert len(ider) == 3
    rader = {r["id"]: r for r in con.execute("SELECT id, alt, bredde FROM signatur_bilde")}
    assert rader[ider[0]]["alt"] == signaturer.INNLIMT_ALT and 'alt="IPR"' in lagret       # bildets egen alt beholdes
    assert rader[ider[2]]["bredde"] == 1800 and 'width="600"' in lagret


def test_samme_innlimte_bilde_gjenbrukes(con):
    logo = png(200, 60)
    bid = _bilde(con, logo)
    sid = signaturer.opprett(con, "A", f'<img src="{_data_url(logo)}"><img src="{_data_url(logo)}">', aktor="test")
    assert signaturer.bilde_ider(signaturer.hent(con, sid)["innhold"]) == [bid]
    assert con.execute("SELECT COUNT(*) FROM signatur_bilde").fetchone()[0] == 1


@pytest.mark.parametrize("data,mime,feil", [
    (png(signaturer.BILDE_MAKS_BREDDE + 1, 10), "image/png", "for bredt"),
    (b"<svg/>", "image/png", "PNG, JPEG og GIF"),
])
def test_innlimt_bilde_som_ikke_kan_brukes_stopper_med_forklaring(con, data, mime, feil):
    with pytest.raises(signaturer.Signaturfeil, match=feil) as e:
        signaturer.opprett(con, "A", f'Hei<img src="{_data_url(data, mime)}">', aktor="test")
    assert "innlimt bilde" in str(e.value)


def test_bilde_fra_annen_nettside_fjernes_og_admin_faar_beskjed(con):
    innhold = 'Hei<img src="https://example.com/logo.png"><img src="http://example.com/b.gif">'
    assert signaturer.eksterne_bilder(innhold) == 2
    assert signaturer.eksterne_bilder(f'<img src="{_data_url(png())}"><img src="/admin/signaturer/bilde/1">') == 0
    svar = _klient().post("/admin/signaturer/ny", data={"navn": "Ekstern", "innhold": innhold}, follow_redirects=True)
    tekst = svar.get_data(as_text=True)
    assert "2 bilder i signaturen kunne ikke brukes og ble fjernet" in tekst
    lagret = _fersk().execute("SELECT innhold FROM signatur WHERE navn='Ekstern'").fetchone()["innhold"]
    assert lagret == "Hei"


def test_innlimt_bilde_i_send_e_post_vises_i_forhandsvisningen(con):
    kid = db.opprett_kurs(con, kode="SIGB", navn="Signaturkurs", datoer=["2031-03-04"], sharepoint_mappe="Kurs/SIGB",
                          pris_nok=0, type="fysisk", sted="Oslo")
    pid, _ = db.meld_paa(con, kid, epost="tove@example.no", fornavn="Tove", etternavn="Test")
    con.commit()
    svar = _klient().post(f"/admin/kurs/{kid}/epost/forhandsvis", data={
        "paamelding_id": pid, "emne": "Hei", "tekst": "Hei {fornavn}",
        "signatur_html": f'Hilsen<br><img src="{_data_url(png(400, 100))}" alt="Logo">'})
    tekst = svar.get_data(as_text=True)
    assert svar.status_code == 200 and "for lang" not in tekst
    assert re.search(r'<img src="/admin/signaturer/bilde/\d+" alt="Logo"', tekst)


def test_innlimte_bilder_blir_staaende_naar_skjemaet_vises_paa_nytt_etter_en_feil(con):
    """Feiler noe annet (her: navnet finnes fra før), skal de innlimte bildene ikke forsvinne fra skjemaet - heller ikke
    når de gjør innholdet lengre enn den gamle grensen."""
    signaturer.opprett(con, "Finnes", "Hei", aktor="test")
    con.commit()
    stor = png(300, 80) + b"\x00" * (signaturer.TEGN_MAKS * 3)
    svar = _klient().post("/admin/signaturer/ny", data={
        "navn": "Finnes", "innhold": f'Vennlig hilsen<br><img src="{_data_url(stor)}" alt="IPR">'})
    tekst = svar.get_data(as_text=True)
    assert svar.status_code == 400 and "finnes allerede" in tekst
    bid = _fersk().execute("SELECT id FROM signatur_bilde").fetchone()[0]             # lagret, ikke rullet tilbake
    assert f'src="/admin/signaturer/bilde/{bid}"' in tekst and "Vennlig hilsen" in tekst


def test_et_ubrukelig_innlimt_bilde_tas_bort_men_resten_av_signaturen_blir_staaende(con):
    svar = _klient().post("/admin/signaturer/ny", data={
        "navn": "Bred", "innhold": f'Vennlig hilsen<img src="{_data_url(png(signaturer.BILDE_MAKS_BREDDE + 1, 5))}">'})
    tekst = svar.get_data(as_text=True)
    assert svar.status_code == 400 and "for bredt" in tekst and "Vennlig hilsen" in tekst and "data:image" not in tekst


def test_feil_i_send_e_post_beholder_det_innlimte_bildet(con):
    kid = db.opprett_kurs(con, kode="SIGC", navn="Signaturkurs", datoer=["2031-03-04"], sharepoint_mappe="Kurs/SIGC",
                          pris_nok=0, type="fysisk", sted="Oslo")
    pid, _ = db.meld_paa(con, kid, epost="tove@example.no", fornavn="Tove", etternavn="Test")
    con.commit()
    svar = _klient().post(f"/admin/kurs/{kid}/epost/forhandsvis", data={
        "paamelding_id": pid, "emne": "", "tekst": "Hei",
        "signatur_html": f'Hilsen<br><img src="{_data_url(png(400, 100))}" alt="Logo">'})
    tekst = svar.get_data(as_text=True)
    assert "Fyll inn et emne." in tekst
    bid = _fersk().execute("SELECT id FROM signatur_bilde").fetchone()[0]
    assert f'src="/admin/signaturer/bilde/{bid}"' in tekst


def test_bilde_fra_fil_paa_pc_en_gir_ogsaa_beskjed():
    assert signaturer.eksterne_bilder('<img src="file:///C:/Users/x/clip_image001.png">') == 1
