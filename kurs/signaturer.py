"""Signaturbiblioteket (E-postmaler → Signaturer): signaturer med formatering, lenker og logo, som i Outlook.

* Hver bruker velger sin hovedsignatur. Én signatur er standard (reserve) og brukes når ingenting annet er valgt.
* Manuelle e-poster får avsenderens hovedsignatur ferdig utfylt. Den kan byttes eller redigeres for akkurat den e-posten
  (lagres på utsendelsen, admin_utsending.signatur_html) - signaturen i biblioteket endres ikke.
* Påminnelser (uken før og dagen før), kursbevis, avlysning og avslag får kursets signatur, ellers standardsignaturen.
  Bekreftelse, venteliste, innloggingslenke, bedriftskvittering og e-post til kursholdere har ingen signatur - der står den
  faste hilsenen som før (maler/epost/_ramme.html).
* Et kurs kan velge en signatur fra biblioteket, eller «Tilpass for dette kurset» (en egen versjon, kurs.signatur_html).
* En signatur som er i bruk (standard, noens hovedsignatur, et kurs), kan ikke slettes før den er byttet ut.

Sikkerhet: signaturen RENSES på serveren hver gang den lagres og hver gang den sendes (rens()). Bare en fast liste med
trygg formatering slipper gjennom - alt annet fjernes: skript, rammer, skjemaer, hendelser (onclick ...), stiler med url(),
lenker som ikke er https/http/mailto/tel, og alle bilder som ikke ligger i biblioteket (ingen eksterne bilder, ingen
sporingspiksler). Bildene er PNG, JPEG eller GIF, kontrollert ut fra innholdet (aldri SVG), og sendes som innebygde bilder
(cid) i selve e-posten. Ingen tredjepartsbibliotek: rensingen bruker Pythons egen HTML-leser.
"""
import base64
import hashlib
import html as html_
import re
from html.parser import HTMLParser
from pathlib import PurePath

from markupsafe import Markup

from . import db, maltekster
from .integrasjoner.epost import Vedlegg

STANDARD_NAVN = "Kursadministrasjonen"
# Nøyaktig den faste hilsenen e-postene har i dag (maler/epost/_ramme.html): ingenting endrer seg før dere lager egne
STANDARD_INNHOLD = "Vennlig hilsen<br>Kursadministrasjonen, Institutt for Psykologisk Rådgivning"
KURSETS_MALER = frozenset({"ukefor", "dagfor", "kursbevis_klar", "avlysning", "avslag", "evaluering"})
NAVN_MAKS = 80
TEGN_MAKS = 20_000
ALT_MAKS = 200
BILDE_MAKS_BYTE = 1_000_000
BILDE_MAKS_BREDDE = 2400     # økt fra 1200 (03.10.2026): innlimte bannere er ofte bredere; de vises nedskalert i e-posten
BILDER_MAKS_PER_EPOST = 2_000_000
BILDE_URL = "/admin/signaturer/bilde/{}"
_BILDE_SRC = re.compile(r"^(?:https?://[^/\s]+)?/admin/signaturer/bilde/(\d{1,9})$")
_CID = "signaturbilde-{}@ipr"
_CID_I_HTML = re.compile(r'src="cid:signaturbilde-(\d{1,9})@ipr"')
_URL_I_HTML = re.compile(r'src="/admin/signaturer/bilde/(\d{1,9})"')
# Et bilde som er limt rett inn i redigereren (f.eks. kopiert fra Outlook) kommer som en data:-adresse med hele bildet i
# teksten. Det flyttes til biblioteket før lengden sjekkes (Camilla 03.10.2026: «Må kunne skrive det vi vil i signaturen»).
_DATA_BILDE = re.compile(r"""(<img\b[^>]*?\bsrc\s*=\s*)(["'])\s*data:image/(?:png|jpe?g|gif);base64,([A-Za-z0-9+/=\s]+?)\2""",
                         re.IGNORECASE)
_IMG_SRC = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*(["'])(.*?)\1""", re.IGNORECASE | re.DOTALL)
INNLIMT_ALT = "Bilde i signaturen"

# Skriftene i verktøylinjen (navn -> CSS). Andre skrifter fjernes ved rensing.
SKRIFTER = {
    "Arial": "Arial, Helvetica, sans-serif",
    "Calibri": "Calibri, Arial, sans-serif",
    "Cambria": "Cambria, Georgia, serif",
    "Georgia": "Georgia, serif",
    "Segoe UI": "'Segoe UI', Arial, sans-serif",
    "Tahoma": "Tahoma, Verdana, sans-serif",
    "Times New Roman": "'Times New Roman', Times, serif",
    "Verdana": "Verdana, Geneva, sans-serif",
}
# Størrelsene i verktøylinjen: navn -> execCommand fontSize (Edge/Chrome gir small/medium/large/x-large, som rens() godtar)
STORRELSER = {"Liten": "2", "Normal": "3", "Stor": "4", "Større": "5"}
FARGER = {"Svart": "#1d2a33", "Grå": "#667788", "IPR-blå": "#1f5f7a", "Grønn": "#2e7d4f", "Rød": "#b3261e"}


class Signaturfeil(ValueError):
    """Signaturen eller bildet kan ikke lagres. Meldingen er norsk og trygg å vise (inneholder aldri innholdet)."""


# ============================ rensing ============================

_TILLATTE = frozenset({"p", "div", "span", "br", "b", "strong", "i", "em", "u", "a", "img", "ul", "ol", "li", "font"})
_TOMME = frozenset({"br", "img", "hr", "input", "meta", "link", "base", "wbr", "col", "area", "source", "track", "param",
                    "embed", "keygen"})
# Fjernes MED innholdet (teksten i et skript skal ikke dukke opp som tekst i signaturen)
_FJERN_MED_INNHOLD = frozenset({"script", "style", "iframe", "object", "embed", "svg", "math", "template", "noscript",
                                "textarea", "select", "button", "head", "title", "frame", "frameset", "noframes",
                                "applet", "audio", "video", "canvas", "form", "xml", "xmp", "plaintext"})
_MAKS_DYBDE = 30
_STORRELSESORD = frozenset({"xx-small", "x-small", "small", "medium", "large", "x-large", "xx-large"})
_FONT_SIZE = {"1": "x-small", "2": "small", "3": "medium", "4": "large", "5": "x-large", "6": "xx-large", "7": "xx-large"}
_NAVNGITTE_FARGER = {"black": "#000000", "white": "#ffffff", "gray": "#808080", "grey": "#808080", "red": "#ff0000",
                     "green": "#008000", "blue": "#0000ff", "navy": "#000080", "teal": "#008080", "maroon": "#800000",
                     "purple": "#800080", "orange": "#ffa500", "darkblue": "#00008b", "darkgreen": "#006400",
                     "darkred": "#8b0000", "dimgray": "#696969", "silver": "#c0c0c0"}


def _farge(verdi: str) -> str | None:
    v = verdi.strip().lower()
    if re.fullmatch(r"#[0-9a-f]{6}", v):
        return v
    if re.fullmatch(r"#[0-9a-f]{3}", v):
        return "#" + "".join(c * 2 for c in v[1:])
    m = re.fullmatch(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*(?:1|1\.0+|0?\.\d+)\s*)?\)", v)
    if m and all(int(x) <= 255 for x in m.groups()):
        return "#" + "".join(f"{int(x):02x}" for x in m.groups())
    return _NAVNGITTE_FARGER.get(v)


def _skrift(verdi: str) -> str | None:
    forste = verdi.split(",")[0].strip().strip("'\"").strip().lower()
    return next((css for navn, css in SKRIFTER.items() if navn.lower() == forste), None)


def _storrelse(verdi: str) -> str | None:
    v = verdi.strip().lower()
    if v in _STORRELSESORD:
        return v
    m = re.fullmatch(r"(\d{1,2}(?:\.\d{1,2})?)(px|pt)", v)
    if m and ((m[2] == "px" and 8 <= float(m[1]) <= 40) or (m[2] == "pt" and 6 <= float(m[1]) <= 30)):
        return v
    return None


_STILREGLER = {
    "color": _farge,
    "font-size": _storrelse,
    "font-family": _skrift,
    "font-weight": lambda v: v if v in ("bold", "normal", "bolder", "lighter", "400", "600", "700") else None,
    "font-style": lambda v: v if v in ("italic", "normal") else None,
    "text-decoration": lambda v: v if v in ("underline", "none", "line-through") else None,
    "text-decoration-line": lambda v: v if v in ("underline", "none", "line-through") else None,
    "text-align": lambda v: v if v in ("left", "center", "right", "justify") else None,
}


def _rens_stil(stil: str) -> str:
    """Bare kjente egenskaper med kontrollerte verdier: ingen url(), expression(), import eller ukjente egenskaper."""
    ut = []
    for erklaering in stil.split(";"):
        navn, kolon, verdi = erklaering.partition(":")
        navn = navn.strip().lower()
        verdi = re.sub(r"\s*!important\s*$", "", verdi.strip(), flags=re.I).strip()
        if not kolon or navn not in _STILREGLER:
            continue
        ren = _STILREGLER[navn](verdi if navn in ("color", "font-family") else verdi.lower())
        if ren:
            ut.append(f"{navn}: {ren}")
    return "; ".join(dict.fromkeys(ut))


def trygg_lenke(href: str) -> str | None:
    """https/http/mailto/tel - alt annet (javascript:, data:, relative adresser ...) avvises. Mellomrom og kontrolltegn
    fjernes først, fordi nettlesere hopper over dem («java\\tscript:»)."""
    ren = re.sub(r"[\x00-\x20\x7f-\x9f]", "", href or "")        # HTML-leseren har alt gjort om &amp; osv.
    if len(ren) > 2000:
        return None
    if re.match(r"(?i)(https?://[^/?#\s]+|mailto:[^\s]+|tel:[+\d() -]+$)", ren):
        return ren
    return None


def _attrtekst(attr: list[tuple[str, str]]) -> str:
    return "".join(f' {navn}="{html_.escape(verdi)}"' for navn, verdi in attr)


class _Renser(HTMLParser):
    def __init__(self, bilder: dict[int, str]):
        super().__init__(convert_charrefs=True)
        self.bilder = bilder
        self.ut: list[str] = []
        self.stabel: list[tuple[str, bool]] = []     # (tagg, skrevet ut)
        self.hopp = 0                                 # >0: inne i en tagg som fjernes med innholdet

    def _attr(self, tagg: str, attrs: list[tuple[str, str | None]]) -> list[tuple[str, str]] | None:
        a = {k.lower(): (v or "") for k, v in attrs}
        ut: list[tuple[str, str]] = []
        if tagg == "img":
            m = _BILDE_SRC.match(a.get("src", "").strip())
            if not m or int(m[1]) not in self.bilder:
                return None                           # bare bilder fra biblioteket - aldri eksterne
            ut.append(("src", BILDE_URL.format(int(m[1]))))
            alt = a.get("alt", "").strip()[:ALT_MAKS] or self.bilder[int(m[1])]
            ut.append(("alt", alt))
            for dim in ("width", "height"):
                if re.fullmatch(r"\d{1,4}", a.get(dim, "").strip()) and 0 < int(a[dim]) <= BILDE_MAKS_BREDDE:
                    ut.append((dim, str(int(a[dim]))))
            return ut
        if tagg == "a":
            lenke = trygg_lenke(a.get("href", ""))
            if not lenke:
                return None                           # lenken fjernes, teksten beholdes
            ut.append(("href", lenke))
        if tagg == "font":                            # <font> fra eldre redigering -> <span style>
            stil = []
            if (f := _farge(a.get("color", ""))):
                stil.append(f"color: {f}")
            if (s := _FONT_SIZE.get(a.get("size", "").strip())):
                stil.append(f"font-size: {s}")
            if (sk := _skrift(a.get("face", ""))):
                stil.append(f"font-family: {sk}")
            stil_tekst = "; ".join(stil + ([_rens_stil(a["style"])] if a.get("style") else []))
            return [("style", stil_tekst.strip("; "))] if stil_tekst.strip("; ") else []
        if tagg != "br" and a.get("style"):
            if (stil := _rens_stil(a["style"])):
                ut.append(("style", stil))
        return ut

    def handle_starttag(self, tag, attrs):
        tagg = tag.lower()
        if self.hopp:
            if tagg not in _TOMME:
                self.hopp += 1
            return
        if tagg in _FJERN_MED_INNHOLD:
            if tagg not in _TOMME:
                self.hopp = 1
            return
        if tagg not in _TILLATTE or len(self.stabel) >= _MAKS_DYBDE:
            return                                    # ukjent tagg: fjernes, teksten beholdes
        attr = self._attr(tagg, attrs)
        navn = "span" if tagg == "font" else tagg
        if tagg in _TOMME:
            if attr is not None:
                self.ut.append(f"<{navn}{_attrtekst(attr)}>")
            return
        if attr is None:
            self.stabel.append((tagg, False))
            return
        self.ut.append(f"<{navn}{_attrtekst(attr)}>")
        self.stabel.append((tagg, True))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _TOMME:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tagg = tag.lower()
        if self.hopp:
            if tagg not in _TOMME:
                self.hopp -= 1
            return
        if tagg in _TOMME or not any(t == tagg for t, _ in self.stabel):
            return
        while self.stabel:
            t, skrevet = self.stabel.pop()
            if skrevet:
                self.ut.append(f"</{'span' if t == 'font' else t}>")
            if t == tagg:
                break

    def handle_data(self, data):
        if not self.hopp:
            self.ut.append(html_.escape(data, quote=False))

    def resultat(self) -> str:
        self.close()
        while self.stabel:
            t, skrevet = self.stabel.pop()
            if skrevet:
                self.ut.append(f"</{'span' if t == 'font' else t}>")
        return "".join(self.ut)


def rens(html: str, bilder: dict[int, str]) -> str:
    """Trygg signatur-HTML. `bilder` er bildene i biblioteket ({id: alternativ tekst}); andre bilder fjernes.
    Tomme linjer og mellomrom til slutt fjernes, så signaturen ikke gir et stort tomrom i e-posten."""
    if len(html or "") > TEGN_MAKS * 3:
        raise Signaturfeil(f"Signaturen er for lang (maks {TEGN_MAKS} tegn).")
    r = _Renser(bilder)
    r.feed(re.sub(r"<[a-zA-Z/!?][^<>]*$", "", html or ""))   # en uferdig tagg til slutt ignoreres, som i nettleseren
    ut = r.resultat().strip()
    ut = re.sub(r"(?:\s|&nbsp;|\xa0|<br>|<(div|p|span)>\s*(?:<br>)?\s*</\1>)+$", "", ut)
    if len(ut) > TEGN_MAKS:
        raise Signaturfeil(f"Signaturen er for lang (maks {TEGN_MAKS} tegn).")
    return ut


def bildekart(con) -> dict[int, str]:
    """{id: alternativ tekst} for bildene i biblioteket - det rens() trenger for å godta et bilde."""
    return {int(r["id"]): r["alt"] for r in con.execute("SELECT id, alt FROM signatur_bilde")}


def bilde_ider(html: str) -> list[int]:
    return list(dict.fromkeys(int(m) for m in _URL_I_HTML.findall(html or "")))


def bildestorrelse(con, html: str) -> int:
    """Antall byte bildene i signaturen utgjør i e-posten (hvert bilde én gang)."""
    ider = bilde_ider(html)
    if not ider:
        return 0
    return con.execute(f"SELECT COALESCE(SUM(storrelse), 0) FROM signatur_bilde WHERE id IN ({','.join('?' * len(ider))})",
                       ider).fetchone()[0]


def kontroller_bildestorrelse(con, html: str) -> None:
    """Alle bildene i én e-post til sammen maks 2 MB (Microsoft 365 tillater 4 MB i én forespørsel)."""
    if bildestorrelse(con, html) > BILDER_MAKS_PER_EPOST:
        raise Signaturfeil("Bildene i signaturen er til sammen for store (maks 2 MB i én e-post).")


# ============================ bilder ============================

def les_bilde(data: bytes) -> tuple[str, int, int]:
    """(mimetype, bredde, høyde) ut fra selve innholdet - aldri ut fra filnavnet. Bare PNG, JPEG og GIF."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR" and len(data) >= 24:
        return "image/png", int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return "image/gif", int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:3] == b"\xff\xd8\xff":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                break
            markor = data[i + 1]
            lengde = int.from_bytes(data[i + 2:i + 4], "big")
            if 0xC0 <= markor <= 0xCF and markor not in (0xC4, 0xC8, 0xCC):
                return "image/jpeg", int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
            i += 2 + lengde
    raise Signaturfeil("Bare bilder i formatene PNG, JPEG og GIF kan brukes (SVG og andre formater er ikke tillatt).")


def _trygt_filnavn(navn: str) -> str:
    grunn = PurePath((navn or "").replace("\\", "/")).name
    return re.sub(r"[^\w.\- ]+", "_", grunn).strip(" .")[:120] or "bilde"


def lagre_bilde(con, filnavn: str, data: bytes, alt: str, *, aktor: str) -> int:
    alt = (alt or "").strip()
    if not alt:
        raise Signaturfeil("Skriv en alternativ tekst for bildet (for eksempel «IPR-logo»). Den vises hvis bildet ikke "
                           "lastes, og leses opp av skjermlesere.")
    if len(alt) > ALT_MAKS:
        raise Signaturfeil(f"Den alternative teksten kan være maks {ALT_MAKS} tegn.")
    if not data:
        raise Signaturfeil("Velg en bildefil.")
    if len(data) > BILDE_MAKS_BYTE:
        raise Signaturfeil("Bildet er for stort (maks 1 MB).")
    mimetype, bredde, hoyde = les_bilde(data)
    if not (0 < bredde <= BILDE_MAKS_BREDDE) or not (0 < hoyde <= 4000):
        raise Signaturfeil(f"Bildet er for bredt (maks {BILDE_MAKS_BREDDE} piksler) eller har ugyldig størrelse.")
    bilde_id = db.sett_inn(
        con, """INSERT INTO signatur_bilde (filnavn, mimetype, bredde, hoyde, storrelse, sha256, alt, innhold, opprettet_av)
                VALUES (?,?,?,?,?,?,?,?,?)""",
        (_trygt_filnavn(filnavn), mimetype, bredde, hoyde, len(data), hashlib.sha256(data).hexdigest(), alt,
         base64.b64encode(data).decode("ascii"), aktor))
    db.logg(con, "signaturbilde_lastet_opp", {"bilde_id": bilde_id, "storrelse": len(data)}, aktor=aktor)
    return bilde_id


def bilde(con, bilde_id: int):
    """(mimetype, bytes) eller None."""
    r = con.execute("SELECT mimetype, innhold FROM signatur_bilde WHERE id=?", (bilde_id,)).fetchone()
    return (r["mimetype"], base64.b64decode(r["innhold"])) if r else None


def bilder(con) -> list[dict]:
    """Bildene i biblioteket, nyeste først, med hvor de brukes (signaturer og kurs med egen versjon)."""
    bruk = _bildebruk(con)
    return [{**dict(r), "brukt_i": bruk.get(int(r["id"]), [])} for r in con.execute(
        "SELECT id, filnavn, mimetype, bredde, hoyde, storrelse, alt, opprettet FROM signatur_bilde ORDER BY id DESC")]


def _bildebruk(con) -> dict[int, list[str]]:
    bruk: dict[int, list[str]] = {}
    for r in con.execute("SELECT navn, innhold FROM signatur"):
        for i in bilde_ider(r["innhold"]):
            bruk.setdefault(i, []).append(f"signaturen «{r['navn']}»")
    for r in con.execute("SELECT navn, signatur_html FROM kurs WHERE signatur_html IS NOT NULL"):
        for i in bilde_ider(r["signatur_html"]):
            bruk.setdefault(i, []).append(f"kurset «{r['navn']}»")
    for r in con.execute("SELECT navn, signatur_bilde_id FROM kursbevis_design WHERE signatur_bilde_id IS NOT NULL"):
        bruk.setdefault(int(r["signatur_bilde_id"]), []).append(f"kursbevis-designet «{r['navn']}»")
    return bruk


def slett_bilde(con, bilde_id: int, *, aktor: str) -> None:
    if (bruk := _bildebruk(con).get(bilde_id)):
        raise Signaturfeil(f"Bildet brukes i {', '.join(bruk)}. Fjern det derfra først.")
    if con.execute("DELETE FROM signatur_bilde WHERE id=?", (bilde_id,)).rowcount:
        db.logg(con, "signaturbilde_slettet", {"bilde_id": bilde_id}, aktor=aktor)


# ============================ biblioteket ============================

def _kontroller_navn(con, navn: str, unntak: int | None = None) -> str:
    navn = " ".join((navn or "").split())
    if not navn:
        raise Signaturfeil("Gi signaturen et navn.")
    if len(navn) > NAVN_MAKS:
        raise Signaturfeil(f"Navnet kan være maks {NAVN_MAKS} tegn.")
    for r in con.execute("SELECT id, navn FROM signatur"):
        if r["navn"].casefold() == navn.casefold() and r["id"] != unntak:
            raise Signaturfeil("Det finnes allerede en signatur med dette navnet.")
    return navn


def flytt_innlimte_bilder(con, innhold: str, *, aktor: str, hopp_over_feil: bool = False) -> str:
    """Innlimte bilder (data:-adresser) lagres i biblioteket, og src byttes til bibliotekets adresse. Et bilde som alt
    ligger i biblioteket (samme innhold), gjenbrukes. Er bildet for stort eller i feil format, stoppes lagringen med
    samme forklaring som ved opplasting - eller, med hopp_over_feil (når skjemaet vises på nytt etter en feil), tas
    bare det bildet bort, så resten av signaturen blir stående."""
    def bytt(m: re.Match) -> str:
        try:
            try:
                data = base64.b64decode(re.sub(r"\s+", "", m[3]), validate=True)
            except ValueError:
                raise Signaturfeil("Et innlimt bilde i signaturen kunne ikke leses. Last det heller opp under «Bilder».")
            rad = con.execute("SELECT id FROM signatur_bilde WHERE sha256=? ORDER BY id LIMIT 1",
                              (hashlib.sha256(data).hexdigest(),)).fetchone()
            try:
                bilde_id = rad["id"] if rad else lagre_bilde(con, "innlimt-bilde", data, INNLIMT_ALT, aktor=aktor)
            except Signaturfeil as e:
                raise Signaturfeil(f"Et innlimt bilde i signaturen kan ikke brukes: {e}")
        except Signaturfeil:
            if hopp_over_feil:
                return f"{m[1]}{m[2]}{m[2]}"           # tom src: rens() fjerner bildet
            raise
        return f"{m[1]}{m[2]}{BILDE_URL.format(bilde_id)}{m[2]}"
    return _DATA_BILDE.sub(bytt, innhold or "")


def eksterne_bilder(innhold: str) -> int:
    """Antall bilder som verken er innlimt eller ligger i biblioteket: fra en annen nettside, eller en fil på PC-en
    (file:///…, typisk når man limer inn fra Outlook eller Word). rens() fjerner dem, og admin skal få vite det i stedet
    for at de bare forsvinner."""
    return sum(1 for m in _IMG_SRC.finditer(innhold or "")
               if not _BILDE_SRC.match(m[2].strip()) and not m[2].strip().lower().startswith("data:image/"))


def klargjor_innhold(con, innhold: str, *, aktor: str = "system") -> str:
    """Renset innhold som er klart til å lagres: innlimte bilder flyttet til biblioteket, kjente bilder, og ikke for store
    bilder til én e-post."""
    innhold = flytt_innlimte_bilder(con, innhold, aktor=aktor)
    ren = rens(innhold, bildekart(con))
    kontroller_bildestorrelse(con, ren)
    return ren


def alle(con) -> list[dict]:
    """Signaturene, standard først og deretter etter navn, med hvor de er i bruk."""
    ut = []
    for r in con.execute("SELECT * FROM signatur"):
        brukere = [b["navn"] for b in con.execute(
            "SELECT navn FROM admin_bruker WHERE hovedsignatur_id=? AND aktiv=1 ORDER BY navn", (r["id"],))]
        kurs = con.execute("SELECT COUNT(*) FROM kurs WHERE signatur_id=? AND signatur_html IS NULL",
                           (r["id"],)).fetchone()[0]
        ut.append({**dict(r), "hovedsignatur_for": brukere, "antall_kurs": kurs})
    return sorted(ut, key=lambda s: (not s["standard"], s["navn"].casefold()))


def hent(con, signatur_id):
    return con.execute("SELECT * FROM signatur WHERE id=?", (signatur_id,)).fetchone()


def standard(con):
    """Standardsignaturen (reserven). Finnes alltid etter migreringen; mangler den likevel, brukes dagens hilsen."""
    r = con.execute("SELECT * FROM signatur WHERE standard=1 ORDER BY id LIMIT 1").fetchone()
    return r or {"id": None, "navn": STANDARD_NAVN, "innhold": STANDARD_INNHOLD, "standard": 1, "versjon": 0}


def opprett(con, navn: str, innhold: str, *, aktor: str) -> int:
    navn = _kontroller_navn(con, navn)
    ren = klargjor_innhold(con, innhold, aktor=aktor)
    sid = db.sett_inn(con, "INSERT INTO signatur (navn, innhold, opprettet_av) VALUES (?,?,?)", (navn, ren, aktor))
    db.logg(con, "signatur_opprettet", {"signatur_id": sid, "navn": navn}, aktor=aktor)
    return sid


def oppdater(con, signatur_id: int, navn: str, innhold: str, versjon: int, *, aktor: str) -> None:
    """Lagrer navn og innhold. `versjon` er versjonen skjemaet ble åpnet med: har noen andre lagret i mellomtiden,
    stoppes lagringen i stedet for å overskrive deres endringer."""
    navn = _kontroller_navn(con, navn, unntak=signatur_id)
    ren = klargjor_innhold(con, innhold, aktor=aktor)
    rad = con.execute(
        """UPDATE signatur SET navn=?, innhold=?, versjon=versjon+1, endret=?, endret_av=? WHERE id=? AND versjon=?""",
        (navn, ren, db.naa_utc(), aktor, signatur_id, versjon))
    if rad.rowcount != 1:
        raise Signaturfeil("Signaturen er endret av noen andre mens du redigerte. Åpne den på nytt og gjør endringen igjen.")
    db.logg(con, "signatur_endret", {"signatur_id": signatur_id, "navn": navn}, aktor=aktor)


def i_bruk(con, signatur_id: int) -> list[str]:
    """Hvorfor signaturen ikke kan slettes (tom liste = kan slettes)."""
    grunner = []
    r = hent(con, signatur_id)
    if r and r["standard"]:
        grunner.append("den er standardsignatur (velg en annen standard først)")
    for b in con.execute("SELECT navn FROM admin_bruker WHERE hovedsignatur_id=? AND aktiv=1 ORDER BY navn",
                         (signatur_id,)):
        grunner.append(f"den er hovedsignaturen til {b['navn']}")
    for k in con.execute("SELECT navn FROM kurs WHERE signatur_id=? AND signatur_html IS NULL ORDER BY navn",
                         (signatur_id,)):
        grunner.append(f"kurset «{k['navn']}» bruker den")
    return grunner


def slett(con, signatur_id: int, *, aktor: str) -> None:
    r = hent(con, signatur_id)
    if not r:
        return
    if (grunner := i_bruk(con, signatur_id)):
        raise Signaturfeil("Signaturen kan ikke slettes fordi " + "; ".join(grunner) + ".")
    # Ikke i bruk: deaktiverte brukere og kurs med egen, tilpasset versjon viser fortsatt til den - koblingen fjernes
    con.execute("UPDATE admin_bruker SET hovedsignatur_id=NULL WHERE hovedsignatur_id=?", (signatur_id,))
    con.execute("UPDATE kurs SET signatur_id=NULL WHERE signatur_id=?", (signatur_id,))
    con.execute("DELETE FROM signatur WHERE id=?", (signatur_id,))
    db.logg(con, "signatur_slettet", {"signatur_id": signatur_id, "navn": r["navn"]}, aktor=aktor)


def sett_standard(con, signatur_id: int, *, aktor: str) -> None:
    if not hent(con, signatur_id):
        raise Signaturfeil("Fant ikke signaturen.")
    con.execute("UPDATE signatur SET standard=0 WHERE standard=1 AND id != ?", (signatur_id,))
    con.execute("UPDATE signatur SET standard=1 WHERE id=?", (signatur_id,))
    db.logg(con, "signatur_standard", {"signatur_id": signatur_id}, aktor=aktor)


def sett_hovedsignatur(con, admin_id: int, signatur_id: int | None, *, aktor: str) -> None:
    if signatur_id is not None and not hent(con, signatur_id):
        raise Signaturfeil("Fant ikke signaturen.")
    con.execute("UPDATE admin_bruker SET hovedsignatur_id=? WHERE id=?", (signatur_id, admin_id))
    db.logg(con, "hovedsignatur_valgt", {"admin_id": admin_id, "signatur_id": signatur_id}, aktor=aktor)


def for_admin(con, admin_id: int | None):
    """Hovedsignaturen til brukeren, ellers standardsignaturen."""
    r = con.execute("""SELECT s.* FROM admin_bruker a JOIN signatur s ON s.id=a.hovedsignatur_id WHERE a.id=?""",
                    (admin_id,)).fetchone() if admin_id else None
    return r or standard(con)


# ============================ kursets signatur ============================

def kursvalg(con, kurs_id: int) -> dict:
    """Hva kurset bruker: modus 'standard' | 'bibliotek' | 'egen', og den signaturen som faktisk brukes."""
    k = con.execute("SELECT signatur_id, signatur_html FROM kurs WHERE id=?", (kurs_id,)).fetchone()
    if k and k["signatur_html"] is not None:
        basis = hent(con, k["signatur_id"]) if k["signatur_id"] else None
        navn = "Tilpasset for dette kurset" + (f" (basert på «{basis['navn']}»)" if basis else "")
        return {"modus": "egen", "signatur_id": k["signatur_id"], "innhold": k["signatur_html"], "navn": navn}
    if k and k["signatur_id"] and (s := hent(con, k["signatur_id"])):
        return {"modus": "bibliotek", "signatur_id": s["id"], "innhold": s["innhold"], "navn": s["navn"]}
    s = standard(con)
    return {"modus": "standard", "signatur_id": None, "innhold": s["innhold"], "navn": f"Standard ({s['navn']})"}


def sett_for_kurs(con, kurs_id: int, modus: str, *, signatur_id: int | None = None, innhold: str | None = None,
                  aktor: str) -> None:
    if modus == "standard":
        verdier = (None, None)
    elif modus == "bibliotek":
        if not signatur_id or not hent(con, signatur_id):
            raise Signaturfeil("Velg en signatur fra biblioteket.")
        verdier = (signatur_id, None)
    elif modus == "egen":
        verdier = (signatur_id if signatur_id and hent(con, signatur_id) else None, klargjor_innhold(con, innhold or "", aktor=aktor))
    else:
        raise Signaturfeil("Ugyldig valg.")
    con.execute("UPDATE kurs SET signatur_id=?, signatur_html=? WHERE id=?", (*verdier, kurs_id))
    db.logg(con, "kurs_signatur_endret", {"kurs_id": kurs_id, "valg": modus, "signatur_id": verdier[0]}, aktor=aktor)


def for_kurs(con, kurs_id: int | None) -> str:
    """Kursets signatur (egen versjon, valgt signatur eller standard), renset på nytt i det den sendes."""
    innhold = kursvalg(con, kurs_id)["innhold"] if kurs_id else standard(con)["innhold"]
    return rens(innhold, bildekart(con))


# ============================ i e-posten ============================

def til_epost(html: str) -> str:
    """Bildene i biblioteket blir innebygde bilder (cid) - e-posten viser aldri til bilder på nett."""
    return _URL_I_HTML.sub(lambda m: f'src="cid:{_CID.format(m[1])}"', html or "")


def til_visning(html: str) -> str:
    """Motsatt vei, til forhåndsvisning i administrasjonen (bildene hentes fra biblioteket)."""
    return _CID_I_HTML.sub(lambda m: f'src="{BILDE_URL.format(m[1])}"', html or "")


class Signaturmarkup(Markup):
    """Renset signatur til malen (maler/epost/_ramme.html). `blokk`: signaturen har avsnitt eller lister og må stå i en
    <div>; ellers står den i samme <p> som den faste hilsenen alltid har stått i."""

    @property
    def blokk(self) -> bool:
        return bool(re.search(r"<(?:p|div|ul|ol|li)\b", self))


def som_markup(html: str) -> Signaturmarkup:
    """Bare for innhold som er renset med rens()."""
    return Signaturmarkup(html)


def innebygde_bilder(con, html: str, mal: str | None = None) -> list[Vedlegg]:
    """Signaturbildene en ferdig e-post viser til (src="cid:signaturbilde-<id>@ipr"), som innebygde vedlegg. Mangler et
    bilde, sendes ikke e-posten (MalFeil: samme håndtering som en malfeil - ingen reservasjon, prøves igjen senere)."""
    ider = list(dict.fromkeys(int(m) for m in _CID_I_HTML.findall(html or "")))
    if not ider:
        return []
    rader = {int(r["id"]): r for r in con.execute(
        f"SELECT id, filnavn, mimetype, innhold FROM signatur_bilde WHERE id IN ({','.join('?' * len(ider))})", ider)}
    if len(rader) != len(ider):
        raise maltekster.MalFeil("signaturbilde_mangler", mal, "signatur",
                                 "Et bilde i signaturen finnes ikke lenger. Velg eller lagre signaturen på nytt.")
    ut = [Vedlegg(rader[i]["filnavn"], rader[i]["mimetype"], base64.b64decode(rader[i]["innhold"]), cid=_CID.format(i))
          for i in ider]
    if sum(len(v.innhold) for v in ut) > BILDER_MAKS_PER_EPOST:
        raise maltekster.MalFeil("signaturbilder_for_store", mal, "signatur",
                                 "Bildene i signaturen er til sammen for store (maks 2 MB i én e-post).")
    return ut
