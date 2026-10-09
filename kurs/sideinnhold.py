"""Kursside: innholdsmodellen (ren logikk: ingen Flask og ingen SQL).

En kursside er ETT JSON-dokument (versjon 1) med sidetopp (tittel, ingress, rom, melding) og en ordnet liste av typede blokker:
tekst, viktig, program, filer, lenker, tabell, kontakt og bilde. Hele dokumentet lagres atomisk (kurs/sidelager.py), slik at
mange endringer er én operasjon, og rekkefølgen er rekkefølgen i listen.

Sikkerhet og personvern (se SPEC, §3 og §8):
  * All tekst er ren tekst og escapes av malene. Bare Tekst-blokken har rik tekst, og den går gjennom `rens_html` (en smal
    allowlist) både ved lagring og ved HVER visning (`til_visning`) - rå HTML fra dokumentet vises aldri direkte.
  * Lenker godtas bare som https, http, mailto og tel (`trygg_url`, `signaturer.trygg_lenke`).
  * Blokker som er skjult, ikke har nådd «vis fra»-dato eller er tomme finnes ikke i det deltakerne får (`synlige_blokker`).
  * Publiseringskontrollen (`publiseringssjekk`) stopper e-postadressen til en bekreftet deltaker og advarer om andre
    e-postadresser og telefonnumre i vanlig tekst. Gruppetabeller tømmes etter kurset (`tom_tabeller`).

Feil som kan vises for brukeren er `Sidefeil` med norsk, trygg tekst (aldri innholdet som feilet).
"""
import html as html_
import json
import re
import secrets
from datetime import date
from html.parser import HTMLParser

from . import signaturer

SKJEMA_VERSJON = 1
BLOKKTYPER = ("tekst", "viktig", "program", "filer", "lenker", "tabell", "kontakt", "bilde")
BLOKKTYPE_NAVN = {"tekst": "Tekst", "viktig": "Viktig", "program": "Program", "filer": "Filer", "lenker": "Lenker",
                  "tabell": "Tabell", "kontakt": "Kontakt", "bilde": "Bilde"}
STANDARDTITLER = {"tekst": "Ny tekst", "viktig": "Viktig", "program": "Program", "filer": "Presentasjoner og dokumenter",
                  "lenker": "Litteratur og lenker", "tabell": "Gruppeinndeling", "kontakt": "Kontakt", "bilde": "Bilde"}

MAKS_BLOKKER = 60
MAKS_TITTEL = 120
MAKS_TEKST_TEGN = 20_000
MAKS_DOK_BYTES = 300_000
MAKS_KROPP_BYTES = 400_000
MAKS_INGRESS = 300
MAKS_ROM = 80
MAKS_GODKJENT = 120          # «Godkjent» i toppfeltet, f.eks. «64 timer vedlikeholdsaktivitet» (Camilla 04.10.2026)
MAKS_MELDING = 400
MAKS_VIKTIG = 1000
MAKS_LISTE = 40
MAKS_FILER_PER_BLOKK = 100
MAKS_KOLONNER = 6
MAKS_KOLONNE_TEKST = 60
MAKS_RADER = 200
MAKS_CELLE = 300
MAKS_DAGER = 20
MAKS_PUNKTER = 40
MAKS_DAGTITTEL = 80
MAKS_TEMA = 200
MAKS_STED_HVEM = 80
MAKS_LENKE_TEKST = 200
MAKS_URL = 2000
MAKS_PERSONER = 20
MAKS_NAVN_ROLLE = 80
MAKS_ALT = 200
MAKS_BILDETEKST = 300
MAKS_SIDETEKST = 1200          # teksten til venstre for et bilde plassert til høyre (Camilla 04.10.2026)
MAKS_HTML_RA = MAKS_TEKST_TEGN * 4          # råtekst som gis til rensen (markup og innlimt søppel kan være mye lengre enn resultatet)

GRENSER = {   # sendes til redigeringsvisningen (maxlength på feltene)
    "blokker": MAKS_BLOKKER, "tittel": MAKS_TITTEL, "tekst_tegn": MAKS_TEKST_TEGN, "ingress": MAKS_INGRESS, "rom": MAKS_ROM, "godkjent": MAKS_GODKJENT,
    "melding": MAKS_MELDING, "viktig": MAKS_VIKTIG, "liste": MAKS_LISTE, "filer_per_blokk": MAKS_FILER_PER_BLOKK,
    "kolonner": MAKS_KOLONNER, "kolonne_tekst": MAKS_KOLONNE_TEKST, "rader": MAKS_RADER, "celle": MAKS_CELLE,
    "dager": MAKS_DAGER, "punkter": MAKS_PUNKTER, "dagtittel": MAKS_DAGTITTEL, "tema": MAKS_TEMA, "sted_hvem": MAKS_STED_HVEM,
    "lenke_tekst": MAKS_LENKE_TEKST, "url": MAKS_URL, "personer": MAKS_PERSONER, "navn_rolle": MAKS_NAVN_ROLLE,
    "alt": MAKS_ALT, "bildetekst": MAKS_BILDETEKST, "sidetekst": MAKS_SIDETEKST, "dok_bytes": MAKS_DOK_BYTES, "kropp_bytes": MAKS_KROPP_BYTES,
}

NIVAER = ("info", "viktig", "advarsel")
MELDING_NIVAER = ("info", "viktig")
PLASSERINGER = ("topp", "bred", "liten", "hoyre", "topp_side")   # hoyre: bildet til høyre, teksten (d.tekst) til venstre;
# topp_side: bildet til høyre i toppen, ved velkomstteksten og faktaraden (Camilla 04.10.2026)
LENKE_NIVAER = ("obligatorisk", "anbefalt")

_BLOKK_ID = re.compile(r"^b_[a-z0-9]{4,12}$")
_ISO_DATO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TID = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_TID_LOS = re.compile(r"^(\d{1,2})[:.]?(\d{2})$")
_TELEFON = re.compile(r"^[+0-9() -]{5,20}$")
_EPOST_ENKEL = re.compile(r"^[^@\s<>\"']+@[^@\s<>\"']+\.[^@\s<>\"']{2,}$")
_KONTROLLTEGN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")
_KONTROLLTEGN_LINJESKIFT_OK = re.compile(r"[\x00-\x09\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")
_ENSLIGE_SURROGATER = re.compile(r"[\ud800-\udfff]")      # en enslig surrogat (JSON «\ud800») kan ikke skrives som UTF-8 og ga unntak i lagringen
_TEGN_ID = "abcdefghijklmnopqrstuvwxyz0123456789"


class Sidefeil(ValueError):
    """Trygg norsk melding. `status` er HTTP-koden ruten bør bruke (standard 422)."""

    def __init__(self, melding: str, status: int = 422):
        super().__init__(melding)
        self.melding = melding
        self.status = status


# ============================ dokumentet ============================

def tomt_dokument() -> dict:
    return {"v": SKJEMA_VERSJON, "tittel": "", "ingress": "", "rom": "", "godkjent": "", "melding": {"tekst": "", "niva": "info"}, "blokker": []}


def ny_blokk_id() -> str:
    return "b_" + "".join(secrets.choice(_TEGN_ID) for _ in range(8))


def til_json(dok: dict) -> str:
    return json.dumps(dok, ensure_ascii=False, separators=(",", ":"))


_SURROGAT_ESKAPE = re.compile(r"\\u[dD][89a-fA-F][0-9a-fA-F]{2}")


def _uten_surrogater(x):
    """Fjerner enslige surrogater (fra en «\\ud800» i JSON som er lagt inn utenom siden) overalt i et innlest dokument."""
    if isinstance(x, str):
        return _ENSLIGE_SURROGATER.sub("", x)
    if isinstance(x, list):
        return [_uten_surrogater(v) for v in x]
    if isinstance(x, dict):
        return {(_ENSLIGE_SURROGATER.sub("", k) if isinstance(k, str) else k): _uten_surrogater(v) for k, v in x.items()}
    return x


def les(json_tekst: str | None) -> dict:
    """Tolerant lesing av lagret JSON: ugyldig eller ukjent versjon gir et tomt dokument, og en blokk som ikke er en
    blokk (feil type eller data) hoppes over. Kaster aldri. Renser IKKE innholdet (det gjøres ved lagring og ved visning)."""
    try:
        rå = json.loads(json_tekst) if json_tekst else None
    except (TypeError, ValueError, RecursionError):
        rå = None
    if isinstance(json_tekst, str) and _SURROGAT_ESKAPE.search(json_tekst):
        rå = _uten_surrogater(rå)                          # ellers ville visningen feilet ved utsending (UnicodeEncodeError)
    if not isinstance(rå, dict) or rå.get("v") != SKJEMA_VERSJON:
        return tomt_dokument()
    dok = tomt_dokument()
    for felt in ("tittel", "ingress", "rom", "godkjent"):
        if isinstance(rå.get(felt), str):
            dok[felt] = rå[felt]
    melding = rå.get("melding")
    if isinstance(melding, dict):
        dok["melding"] = {"tekst": melding.get("tekst") if isinstance(melding.get("tekst"), str) else "",
                          "niva": melding.get("niva") if melding.get("niva") in MELDING_NIVAER else "info"}
    blokker = rå.get("blokker")
    for b in blokker if isinstance(blokker, list) else []:
        if isinstance(b, dict) and b.get("type") in BLOKKTYPER and isinstance(b.get("data"), dict):
            dok["blokker"].append({
                "id": b.get("id") if isinstance(b.get("id"), str) else "", "type": b["type"],
                "tittel": b.get("tittel") if isinstance(b.get("tittel"), str) else "", "skjult": bool(b.get("skjult")),
                "vis_fra": b.get("vis_fra") if isinstance(b.get("vis_fra"), str) and _ISO_DATO.match(b["vis_fra"]) else None,
                "i_meny": b.get("i_meny") is not False, "data": b["data"]})
    return dok


# ============================ trygg adresse ============================

def trygg_url(tekst: str | None) -> str | None:
    """Trygg lenkeadresse eller None. Bare https, http, mailto og tel. En adresse uten skjema som ser ut som et domene
    (`ipr.no/kurs`) får https:// foran. Alt annet (javascript:, data:, vbscript:, kontrolltegn og mellomrom inni ...) gir None."""
    if not isinstance(tekst, str):
        return None
    t = tekst.strip()
    if not t or len(t) > MAKS_URL or re.search(r"[\x00-\x20\x7f-\x9f\ud800-\udfff<>\"\\]", t):
        return None
    skjema = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*):", t)
    if skjema:
        navn = skjema.group(1).lower()
        if navn in ("http", "https"):
            if not t[len(navn) + 1:].startswith("//"):
                return None
            vert = re.split(r"[/?#]", t[len(navn) + 3:], maxsplit=1)[0]
            return t if vert and "@" not in vert and not vert.startswith(".") and re.search(r"[A-Za-z0-9]", vert) else None
        if navn == "mailto":
            return t if len(t) > len("mailto:") and "<" not in t and ">" not in t and '"' not in t else None
        if navn == "tel":
            return t if re.match(r"^tel:[+\d()\-]{3,}$", t, re.I) else None
        return None
    if re.match(r"^[\w.\-]+\.[A-Za-z]{2,}(/\S*)?$", t):
        return "https://" + t
    return None


# ============================ rik tekst: renser ============================

_BLOKK_TAGGER = frozenset({"p", "ul", "ol", "li"})
_INLINE_TAGGER = frozenset({"strong", "em", "a"})
# b/i/div blir strong/em/p. Overskrifter (h1-h6) blir et avsnitt med fet skrift (se `_OVERSKRIFTER`), tabeller og andre blokker blir
# avsnitt (teksten beholdes, formen forsvinner).
_OVERSKRIFTER = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_TAGG_ALIAS = {"b": "strong", "i": "em", "div": "p", **{t: "p" for t in (
    "blockquote", "pre", "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption",
    "section", "article", "header", "footer", "main", "nav", "aside", "dl", "dt", "dd", "address", "figure", "figcaption",
    "center", "fieldset", "legend", "details", "summary")}}
_MAKS_DYBDE = 30


class _Node:
    __slots__ = ("tagg", "href", "barn")

    def __init__(self, tagg: str, href: str | None = None):
        self.tagg, self.href, self.barn = tagg, href, []


class _HtmlRenser(HTMLParser):
    """Bygger et lite tre av det som er tillatt (p, br, strong, em, ul, ol, li, a[href]) og ignorerer resten. Ukjente tagger
    fjernes og teksten beholdes; farlige tagger (signaturer._FJERN_MED_INNHOLD) fjernes MED innhold. Ingen attributter unntatt
    `href` på `a` (bare trygge adresser)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rot = _Node("rot")
        self.stabel: list[_Node] = [self.rot]
        self.hopp = 0
        self.i_overskrift = False        # inni en h1-h6: hele overskriften er fet, en indre </b> skal ikke avslutte den

    # ---- hjelpere ----
    def _topp(self) -> _Node:
        return self.stabel[-1]

    def _apne(self, node: _Node) -> None:
        self._topp().barn.append(node)
        self.stabel.append(node)

    def _lukk_til(self, tagger: frozenset) -> None:
        """Lukker åpne elementer til toppen ikke lenger er i `tagger`."""
        while len(self.stabel) > 1 and self._topp().tagg in tagger:
            self.stabel.pop()

    def _lukk_inline(self) -> None:
        self._lukk_til(_INLINE_TAGGER)

    def _sikre_beholder(self) -> None:
        """Tekst og inline-elementer må ligge i en p eller li: åpner en implisitt p (øverst) eller li (rett i en liste)."""
        topp = self._topp().tagg
        if topp == "rot":
            self._apne(_Node("p"))
        elif topp in ("ul", "ol"):
            self._apne(_Node("li"))

    def _i_liste_element(self) -> bool:
        return any(n.tagg == "li" for n in self.stabel)

    def _i_paragraf(self) -> bool:
        return any(n.tagg == "p" for n in self.stabel)

    # ---- HTMLParser ----
    def handle_starttag(self, tag, attrs):
        tagg = tag.lower()
        if self.hopp:
            if tagg not in signaturer._TOMME:
                self.hopp += 1
            return
        if tagg in signaturer._FJERN_MED_INNHOLD:
            if tagg not in signaturer._TOMME:
                self.hopp = 1
            return
        if tagg in _OVERSKRIFTER:                        # innlimt overskrift (Word m.m.): avsnitt med fet skrift, så strukturen ikke forsvinner
            self.handle_starttag("p", [])
            self.handle_starttag("strong", [])
            self.i_overskrift = True
            return
        tagg = _TAGG_ALIAS.get(tagg, tagg)
        if tagg in _BLOKK_TAGGER:
            self.i_overskrift = False                    # et nytt avsnitt eller en liste avslutter overskriften (også uten </h2>)
        if len(self.stabel) >= _MAKS_DYBDE and tagg not in ("br",):
            return
        if tagg == "br":
            self._sikre_beholder()
            self._topp().barn.append(_Node("br"))
        elif tagg == "p":
            self._lukk_inline()
            if self._topp().tagg in ("ul", "ol"):
                self._apne(_Node("li"))
            if self._i_liste_element():
                if self._topp().tagg == "li" and self._topp().barn:
                    self._topp().barn.append(_Node("br"))
                return                                   # avsnitt inni et listepunkt blir linjeskift i punktet
            while self._i_paragraf():                    # p inni p er ikke lov: lukk den forrige (som nettleseren)
                self.stabel.pop()
            self._apne(_Node("p"))
        elif tagg in ("ul", "ol"):
            self._lukk_inline()
            while self._topp().tagg == "p":
                self.stabel.pop()
            if self._topp().tagg in ("ul", "ol"):
                self._apne(_Node("li"))
            self._apne(_Node(tagg))
        elif tagg == "li":
            self._lukk_inline()
            while self._topp().tagg == "li" and len(self.stabel) > 1:
                self.stabel.pop()
            if self._topp().tagg in ("ul", "ol"):
                self._apne(_Node("li"))
            else:                                        # li utenfor en liste behandles som et vanlig avsnitt
                while self._i_paragraf():
                    self.stabel.pop()
                self._apne(_Node("p"))
        elif tagg in ("strong", "em"):
            if any(n.tagg == tagg for n in self.stabel):
                return                                   # strong i strong gjør ingenting
            self._sikre_beholder()
            self._apne(_Node(tagg))
        elif tagg == "a":
            href = signaturer.trygg_lenke(dict((k.lower(), v or "") for k, v in attrs).get("href", ""))
            if any(n.tagg == "a" for n in self.stabel):
                self._lukk_til(frozenset({"a"}))         # a i a: den første lukkes, teksten beholdes uten ny lenke
                if href is None:
                    return
            if href is None:
                return                                   # ingen trygg adresse: teksten beholdes uten lenke
            self._sikre_beholder()
            self._apne(_Node("a", href))
        # alle andre tagger: fjernes, teksten beholdes

    def handle_startendtag(self, tag, attrs):
        tagg = tag.lower()
        self.handle_starttag(tag, attrs)
        if tagg not in signaturer._TOMME and not self.hopp and _TAGG_ALIAS.get(tagg, tagg) in (_BLOKK_TAGGER | _INLINE_TAGGER):
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tagg = tag.lower()
        if self.hopp:
            if tagg not in signaturer._TOMME:
                self.hopp -= 1
            return
        if tagg in _OVERSKRIFTER:
            self.i_overskrift = False
            self.handle_endtag("strong")
            self.handle_endtag("p")
            return
        tagg = _TAGG_ALIAS.get(tagg, tagg)
        if tagg == "strong" and self.i_overskrift:
            return                                       # </b> inni en overskrift: den fete skriften gjelder hele overskriften
        if tagg in _INLINE_TAGGER:
            i = len(self.stabel) - 1
            while i > 0 and self.stabel[i].tagg in _INLINE_TAGGER and self.stabel[i].tagg != tagg:
                i -= 1
            if i > 0 and self.stabel[i].tagg == tagg:
                del self.stabel[i:]
        elif tagg in _BLOKK_TAGGER:
            if tagg == "p" and self._i_liste_element() and not self._i_paragraf():
                return
            if any(n.tagg == tagg for n in self.stabel):
                while self.stabel[-1].tagg != tagg:
                    self.stabel.pop()
                self.stabel.pop()

    def handle_data(self, data):
        if self.hopp:
            return
        if not data.strip():
            # mellomrom mellom tagger: beholdes bare inni en beholder som allerede har innhold
            if self._topp().tagg in ("p", "li", "strong", "em", "a") and self._topp().barn:
                self._topp().barn.append(" ")
            return
        self._sikre_beholder()
        self._topp().barn.append(data)


def _ren_tekst(s: str) -> str:
    return re.sub(r"[\s\xa0]+", " ", s)


def _rens_node(node: _Node) -> None:
    """Normaliserer et element på plass: nabotekst slås sammen og mellomrom kortes ned, tomme elementer fjernes, og i avsnitt og
    listepunkter fjernes ledende/etterfølgende linjeskift (høyst to på rad). Resultatet er stabilt: å rense det på nytt gir det samme."""
    ny: list = []
    for b in node.barn:
        if isinstance(b, str):
            if ny and isinstance(ny[-1], str):
                ny[-1] += b
            else:
                ny.append(b)
            continue
        if b.tagg != "br":
            _rens_node(b)
            if _er_tom(b):
                if b.tagg in _INLINE_TAGGER and any(isinstance(x, str) and x for x in b.barn):   # «a<em> </em>b» skal fortsatt ha et mellomrom
                    if ny and isinstance(ny[-1], str):
                        ny[-1] += " "
                    else:
                        ny.append(" ")
                continue
        ny.append(b)
    ny = [_ren_tekst(x) if isinstance(x, str) else x for x in ny]
    ny = [x for x in ny if x != ""]
    if node.tagg in ("p", "li"):
        ut: list = []
        for b in ny:
            if isinstance(b, _Node) and b.tagg == "br":
                while ut and isinstance(ut[-1], str) and not ut[-1].strip():
                    ut.pop()
                if not ut or (len(ut) >= 2 and all(isinstance(x, _Node) and x.tagg == "br" for x in ut[-2:])):
                    continue
            elif isinstance(b, str) and ut and isinstance(ut[-1], _Node) and ut[-1].tagg == "br" and not b.strip():
                continue
            ut.append(b)
        while ut and ((isinstance(ut[-1], str) and not ut[-1].strip()) or (isinstance(ut[-1], _Node) and ut[-1].tagg == "br")):
            ut.pop()
        while ut and isinstance(ut[0], str) and not ut[0].strip():
            ut.pop(0)
        if ut and isinstance(ut[0], str):
            ut[0] = ut[0].lstrip()
        if ut and isinstance(ut[-1], str):
            ut[-1] = ut[-1].rstrip()
        ny = ut
    node.barn = ny


def _er_tom(node: _Node) -> bool:
    if node.tagg == "br":
        return False
    if node.tagg in ("ul", "ol"):
        return not node.barn
    return not any((isinstance(b, str) and b.strip()) or (isinstance(b, _Node) and not _er_tom(b) and b.tagg != "br")
                   for b in node.barn)


def _ut(node: _Node) -> str:
    if node.tagg == "br":
        return "<br>"
    inn = "".join(html_.escape(b, quote=False) if isinstance(b, str) else _ut(b) for b in node.barn)
    if node.tagg == "a":
        return f'<a href="{html_.escape(node.href, quote=True)}">{inn}</a>'
    return f"<{node.tagg}>{inn}</{node.tagg}>"


# En hel tagg: navn, attributter (sitater kan inneholde > og <) og avsluttende >. Alternativene starter med hvert sitt tegn, så
# uttrykket kan ikke backtracke katastrofalt, og hvert forsøk stopper ved neste sitat av samme slag eller neste «<».
_HEL_TAGG = re.compile(r'</?[A-Za-z][^\s/<>"\'=\x00]*(?:"[^"]*"|\'[^\']*\'|[^<>"\'])*>')


def _forbered_html(html: str) -> str:
    """Gjør råtekst trygg å gi til stdlib-parseren i LINEÆR tid. `html.parser` bruker tid som vokser langt raskere enn lengden på
    uferdige tagger («<a » eller «<a/» gjentatt): 20 000 tegn kunne binde en tråd i et halvt minutt, og 80 000 tegn i timevis.
    Derfor får parseren bare hele tagger. Kommentarer, deklarasjoner (`<!...>`, `<?...>`) fjernes, og hver «<» som ikke begynner
    en hel tagg blir `&lt;` (vises som «<» i teksten, som før). Fullstendige tagger sendes uendret videre, så det parseren
    gjør med dem er uendret, og alt farlig fjernes fortsatt av `_HtmlRenser`."""
    if "<" not in html:
        return html
    ut: list[str] = []
    pos = 0
    ingen_kommentar_slutt = ingen_slutt = False      # finnes ingen «-->» / «>» lenger fram, finnes den ikke for senere «<» heller
    while True:
        i = html.find("<", pos)
        if i < 0:
            ut.append(html[pos:])
            break
        ut.append(html[pos:i])
        m = _HEL_TAGG.match(html, i)
        if m:
            ut.append(m.group(0))
            pos = m.end()
            continue
        pos = i + 1
        if html.startswith("<!--", i) and not ingen_kommentar_slutt:
            j = html.find("-->", i + 4)
            if j >= 0:
                pos = j + 3
                continue
            ingen_kommentar_slutt = True
        elif html[i + 1:i + 2] in ("!", "?") and not ingen_slutt:
            j = html.find(">", i + 2)
            if j >= 0:
                pos = j + 1
                continue
            ingen_slutt = True
        ut.append("&lt;")
    return "".join(ut)


def _rens_ra(html: str) -> str:
    r = _HtmlRenser()
    ra = _ENSLIGE_SURROGATER.sub("", (html or "")[:MAKS_HTML_RA])
    r.feed(_forbered_html(re.sub(r"<[a-zA-Z/!?][^<>]*$", "", ra)))   # en uferdig tagg til slutt ignoreres
    r.close()
    _rens_node(r.rot)
    r.rot.barn = [b for b in r.rot.barn if not (isinstance(b, str) and not b.strip())]
    return "".join(_ut(b) if isinstance(b, _Node) else html_.escape(b, quote=False) for b in r.rot.barn)


def rens_html(html: str) -> str:
    """Trygg HTML for Tekst-blokken (§3.6): p, br, strong, em, ul, ol, li og a[href]. Alt annet fjernes (teksten beholdes),
    farlige tagger fjernes med innholdet. Idempotent: rens_html(rens_html(x)) == rens_html(x). Kaster Sidefeil over 20 000 tegn."""
    if not isinstance(html, str):
        raise Sidefeil("Teksten har ugyldig innhold.")
    if len(html) > MAKS_HTML_RA:
        raise Sidefeil(f"Teksten er for lang (maks {MAKS_TEKST_TEGN:,} tegn).".replace(",", " "))
    ut = _rens_ra(html)
    if len(ut) > MAKS_TEKST_TEGN:
        raise Sidefeil(f"Teksten er for lang (maks {MAKS_TEKST_TEGN:,} tegn).".replace(",", " "))
    return ut


_EKSTERN_LENKE = re.compile(r'<a href="((?:https?)://[^"]*)">', re.I)


def til_visning(html: str) -> str:
    """Den ENESTE veien fra lagret HTML til visning: renser på nytt (lagret HTML kan være manipulert i databasen) og legger
    `rel="noopener noreferrer" target="_blank"` på http(s)-lenker (ikke mailto/tel). Kaster aldri."""
    ut = _rens_ra(html if isinstance(html, str) else "")
    return _EKSTERN_LENKE.sub(r'<a href="\1" rel="noopener noreferrer" target="_blank">', ut)


def tekst_uten_tagger(html: str) -> str:
    """Selve teksten i (renset) HTML, til tegnteller og søk. Tagger og lenkeadresser er ikke med."""
    return html_.unescape(re.sub(r"<[^>]+>", " ", html or "")).replace("\xa0", " ")


# ============================ validering ============================

class _Ctx:
    def __init__(self, fil_ider, kursdag_ider):
        self.fil_ider = fil_ider                # {id: {"type": ...}} eller None (ikke kontrollér)
        self.kursdag_ider = kursdag_ider        # {id} eller None
        self.merknader: list[str] = []

    def merk(self, tekst: str) -> None:
        if tekst not in self.merknader:
            self.merknader.append(tekst)


def _tekst(verdi, maks: int, hva: str, *, linjeskift: bool = False) -> str:
    """Ren tekst: trimmet, uten kontrolltegn, høyst `maks` tegn (ellers Sidefeil). Tall gjøres om til tekst."""
    if verdi is None:
        return ""
    if isinstance(verdi, bool) or not isinstance(verdi, (str, int, float)):
        raise Sidefeil(f"«{hva}» har ugyldig innhold.")
    s = str(verdi)
    if linjeskift:
        s = s.replace("\r\n", "\n").replace("\r", "\n")
        s = _KONTROLLTEGN_LINJESKIFT_OK.sub("", s)
        s = re.sub(r"\n{3,}", "\n\n", s).strip()
    else:
        s = _KONTROLLTEGN.sub("", s).replace("\n", " ").replace("\t", " ").replace("\r", " ").strip()
    if len(s) > maks:
        raise Sidefeil(f"«{hva}» er for lang (maks {maks} tegn).")
    return s


def _bool(verdi, standard: bool) -> bool:
    if verdi is None:
        return standard
    if isinstance(verdi, bool):
        return verdi
    if isinstance(verdi, int) and verdi in (0, 1):
        return bool(verdi)
    raise Sidefeil("Ugyldig verdi i en av avkrysningene.")


def _dato(verdi, hva: str) -> str | None:
    if verdi in (None, ""):
        return None
    if not isinstance(verdi, str) or not _ISO_DATO.match(verdi.strip()):
        raise Sidefeil(f"«{hva}» er ikke en gyldig dato.")
    try:
        date.fromisoformat(verdi.strip())
    except ValueError:
        raise Sidefeil(f"«{hva}» er ikke en gyldig dato.") from None
    return verdi.strip()


MAKS_ID = 2_147_483_647           # største id databasen (også PostgreSQL integer) kan ha; større tall er aldri en gyldig id


def _heltall(verdi) -> int | None:
    """En id (0 til MAKS_ID) som tall eller tekst, ellers None. Enorme tall ga OverflowError mot databasen og blir aldri en id."""
    if isinstance(verdi, bool):
        return None
    if isinstance(verdi, int):
        return verdi if 0 <= verdi <= MAKS_ID else None
    if isinstance(verdi, str) and re.fullmatch(r"[0-9]{1,12}", verdi.strip()):
        return int(verdi.strip()) if int(verdi.strip()) <= MAKS_ID else None
    return None


def normaliser_tid(verdi) -> str:
    """«915», «9.15», «9:15» og «09:15» blir 09:15. Tom eller ugyldig verdi gir tom tekst."""
    if not isinstance(verdi, str):
        return ""
    t = verdi.strip()
    if _TID.match(t):
        return t
    m = _TID_LOS.match(t)
    if m:
        kandidat = f"{int(m.group(1)):02d}:{m.group(2)}"
        if _TID.match(kandidat):
            return kandidat
    return ""


def _tid(verdi, ctx: _Ctx, hvor: str) -> str:
    """Klokkeslett. Et halvt skrevet klokkeslett skal aldri stoppe autolagringen: ugyldig verdi fjernes med en merknad."""
    if verdi in (None, ""):
        return ""
    ren = normaliser_tid(verdi)
    if not ren:
        ctx.merk(f"Klokkeslettet «{str(verdi)[:12]}» i {hvor} er ugyldig (skriv for eksempel 09:15) og ble fjernet.")
    return ren


def _liste(verdi, maks: int, hva: str) -> list:
    if verdi is None:
        return []
    if not isinstance(verdi, list):
        raise Sidefeil(f"«{hva}» har ugyldig innhold.")
    if len(verdi) > maks:
        raise Sidefeil(f"For mange elementer i «{hva}» (maks {maks}).")
    return verdi


def _objekt(verdi, hva: str) -> dict:
    if verdi is None:
        return {}
    if not isinstance(verdi, dict):
        raise Sidefeil(f"«{hva}» har ugyldig innhold.")
    return verdi


def _valider_tekst(d: dict, ctx: _Ctx, tittel: str) -> dict:
    return {"html": rens_html(d.get("html") or "")}


def _valider_viktig(d: dict, ctx: _Ctx, tittel: str) -> dict:
    niva = d.get("niva") if d.get("niva") in NIVAER else "info"
    return {"niva": niva, "tekst": _tekst(d.get("tekst"), MAKS_VIKTIG, "Viktig-teksten", linjeskift=True)}


def _valider_program(d: dict, ctx: _Ctx, tittel: str) -> dict:
    dager = []
    for i, dag in enumerate(_liste(d.get("dager"), MAKS_DAGER, "Program: dager"), 1):
        dag = _objekt(dag, "Programdag")
        kd = _heltall(dag.get("kursdag_id"))
        if kd is not None and ctx.kursdag_ider is not None and kd not in ctx.kursdag_ider:
            ctx.merk(f"En programdag i «{tittel or 'Program'}» var koblet til en kursdag som ikke finnes lenger. "
                     "Koblingen er fjernet, og den lagrede datoen brukes.")
            kd = None
        punkter = []
        for p in _liste(dag.get("punkter"), MAKS_PUNKTER, "Program: punkter"):
            p = _objekt(p, "Programpunkt")
            hvor = f"programmet «{tittel or 'Program'}»"
            punkter.append({"fra": _tid(p.get("fra"), ctx, hvor), "til": _tid(p.get("til"), ctx, hvor),
                            "tema": _tekst(p.get("tema"), MAKS_TEMA, "Tema"),
                            "sted": _tekst(p.get("sted"), MAKS_STED_HVEM, "Sted"),
                            "hvem": _tekst(p.get("hvem"), MAKS_STED_HVEM, "Hvem"),
                            "pause": _bool(p.get("pause"), False)})
        dager.append({"kursdag_id": kd, "dato": _dato(dag.get("dato"), f"Dato for dag {i}"),
                      "tittel": _tekst(dag.get("tittel"), MAKS_DAGTITTEL, "Dagtittel"), "punkter": punkter})
    return {"dager": dager}


def _valider_filer(d: dict, ctx: _Ctx, tittel: str) -> dict:
    elementer = []
    for e in _liste(d.get("filer"), MAKS_FILER_PER_BLOKK, "Filer"):
        e = _objekt(e, "Fil")
        fid = _heltall(e.get("fil_id"))
        if fid is None or (ctx.fil_ider is not None and fid not in ctx.fil_ider):
            ctx.merk(f"En fil i «{tittel or 'Filer'}» finnes ikke lenger og ble fjernet fra siden.")
            continue
        gruppe = _heltall(e.get("gruppe"))
        if gruppe is not None and ctx.kursdag_ider is not None and gruppe not in ctx.kursdag_ider:
            gruppe = None
        elementer.append({"fil_id": fid, "tittel": _tekst(e.get("tittel"), MAKS_TITTEL, "Filtittel"), "gruppe": gruppe,
                          "synlig_fra": _dato(e.get("synlig_fra"), "Synlig fra")})
    # «sharepoint» (kursholders filer) er tatt bort (Camilla 06.10.2026): et gammelt felt i lagrede sider leses ikke lenger
    return {"filer": elementer, "vis_kommende": _bool(d.get("vis_kommende"), True)}


def _valider_lenker(d: dict, ctx: _Ctx, tittel: str) -> dict:
    elementer = []
    for e in _liste(d.get("lenker"), MAKS_LISTE, "Lenker"):
        e = _objekt(e, "Lenke")
        lt = _tekst(e.get("tittel"), MAKS_TITTEL, "Lenketittel")
        kilde = "zoom" if e.get("kilde") == "zoom" else None
        rå_url = e.get("url")
        url = ""
        if kilde is None and isinstance(rå_url, str) and rå_url.strip():
            url = trygg_url(rå_url) or ""
            if not url:
                ctx.merk(f"Lenken «{lt or 'uten tittel'}» har ingen gyldig adresse og vises ikke. "
                         "Adressen må starte med https://.")
        elementer.append({"tittel": lt, "url": url, "tekst": _tekst(e.get("tekst"), MAKS_LENKE_TEKST, "Beskrivelse"),
                          "nivaa": e.get("nivaa") if e.get("nivaa") in LENKE_NIVAER else None, "kilde": kilde})
    return {"lenker": elementer}


def _valider_tabell(d: dict, ctx: _Ctx, tittel: str) -> dict:
    kolonner = [_tekst(k, MAKS_KOLONNE_TEKST, "Kolonneoverskrift") for k in _liste(d.get("kolonner"), MAKS_KOLONNER, "Kolonner")]
    rader = []
    for rad in _liste(d.get("rader"), MAKS_RADER, "Rader"):
        celler = [_tekst(c, MAKS_CELLE, "Celle") for c in _liste(rad, 50, "Celler")]
        celler = (celler + [""] * len(kolonner))[:len(kolonner)]
        rader.append(celler)
    return {"kolonner": kolonner, "rader": rader}


def _valider_kontakt(d: dict, ctx: _Ctx, tittel: str) -> dict:
    personer = []
    for p in _liste(d.get("personer"), MAKS_PERSONER, "Kontaktpersoner"):
        p = _objekt(p, "Kontaktperson")
        navn = _tekst(p.get("navn"), MAKS_NAVN_ROLLE, "Navn")
        tlf = _tekst(p.get("telefon"), 40, "Telefon")
        epost = _tekst(p.get("epost"), 200, "E-post")
        if tlf and not _TELEFON.match(tlf):
            ctx.merk(f"Telefonnummeret til «{navn or 'kontaktperson'}» er ugyldig og ble fjernet "
                     "(bruk bare tall, mellomrom, + og parenteser).")
            tlf = ""
        if epost and not _EPOST_ENKEL.match(epost):
            ctx.merk(f"E-postadressen til «{navn or 'kontaktperson'}» er ugyldig og ble fjernet.")
            epost = ""
        personer.append({"navn": navn, "rolle": _tekst(p.get("rolle"), MAKS_NAVN_ROLLE, "Rolle"), "telefon": tlf, "epost": epost})
    return {"personer": personer}


def _valider_bilde(d: dict, ctx: _Ctx, tittel: str) -> dict:
    fid = _heltall(d.get("fil_id"))
    if fid is not None and ctx.fil_ider is not None:
        info = ctx.fil_ider.get(fid)
        if info is None or info.get("type") != "bilde":
            ctx.merk(f"Bildet i «{tittel or 'Bilde'}» finnes ikke lenger og ble fjernet fra siden.")
            fid = None
    plassering = d.get("plassering") if d.get("plassering") in PLASSERINGER else "bred"
    tekst = (_tekst(d.get("tekst"), MAKS_SIDETEKST, "Teksten ved siden av bildet", linjeskift=True) if plassering == "hoyre"
             else _tekst(d.get("tekst"), MAKS_BILDETEKST, "Bildetekst"))
    return {"fil_id": fid, "alt": _tekst(d.get("alt"), MAKS_ALT, "Alternativ tekst"), "tekst": tekst, "plassering": plassering}


_VALIDERERE = {"tekst": _valider_tekst, "viktig": _valider_viktig, "program": _valider_program, "filer": _valider_filer,
               "lenker": _valider_lenker, "tabell": _valider_tabell, "kontakt": _valider_kontakt, "bilde": _valider_bilde}


def valider(dok, *, fil_ider: dict[int, dict] | None, kursdag_ider: set[int] | None) -> tuple[dict, list[str]]:
    """Validerer og renser et dokument. Returnerer (renset dokument, merknader). Strukturfeil (ikke et objekt, feil versjon,
    ukjent blokktype, for mange blokker, for stort dokument, for lang tekst, ugyldig dato) gir Sidefeil. Det som kan rettes uten å
    miste noe viktig (ukjent fil eller kursdag, ugyldig lenke, telefon, e-post eller klokkeslett) fjernes med en merknad, slik at
    autolagringen aldri feiler fordi noen har skrevet en halv adresse. `fil_ider` og `kursdag_ider` = None hopper over kontrollen."""
    if not isinstance(dok, dict):
        raise Sidefeil("Siden har ugyldig innhold.")
    if dok.get("v", SKJEMA_VERSJON) != SKJEMA_VERSJON:
        raise Sidefeil("Siden er lagret i et format denne versjonen ikke kjenner.")
    ctx = _Ctx(fil_ider, kursdag_ider)
    melding = _objekt(dok.get("melding"), "Melding øverst")
    ut = {"v": SKJEMA_VERSJON,
          "tittel": _tekst(dok.get("tittel"), MAKS_TITTEL, "Tittel"),
          "ingress": _tekst(dok.get("ingress"), MAKS_INGRESS, "Ingress"),
          "rom": _tekst(dok.get("rom"), MAKS_ROM, "Rom"),
          "godkjent": _tekst(dok.get("godkjent"), MAKS_GODKJENT, "Godkjent"),
          "melding": {"tekst": _tekst(melding.get("tekst"), MAKS_MELDING, "Melding øverst", linjeskift=True),
                      "niva": melding.get("niva") if melding.get("niva") in MELDING_NIVAER else "info"},
          "blokker": []}
    brukte: set[str] = set()
    for b in _liste(dok.get("blokker"), MAKS_BLOKKER, "Blokker"):
        b = _objekt(b, "Blokk")
        typ = b.get("type")
        if typ not in BLOKKTYPER:
            raise Sidefeil("Siden inneholder en blokk av en type som ikke finnes.")
        bid = b.get("id")
        if not isinstance(bid, str) or not _BLOKK_ID.match(bid) or bid in brukte:
            bid = ny_blokk_id()
            while bid in brukte:
                bid = ny_blokk_id()
        brukte.add(bid)
        tittel = _tekst(b.get("tittel"), MAKS_TITTEL, "Blokktittel")
        ut["blokker"].append({
            "id": bid, "type": typ, "tittel": tittel, "skjult": _bool(b.get("skjult"), False),
            "vis_fra": _dato(b.get("vis_fra"), f"Vises fra i «{tittel or BLOKKTYPE_NAVN[typ]}»"),
            "i_meny": _bool(b.get("i_meny"), True),
            "data": _VALIDERERE[typ](_objekt(b.get("data"), "Blokkinnhold"), ctx, tittel)})
    try:
        antall_byte = len(til_json(ut).encode("utf-8"))
    except UnicodeEncodeError:                   # et tegn som ikke kan lagres (bør være fanget tidligere; siste sikkerhetsnett)
        raise Sidefeil("Siden inneholder tegn som ikke kan lagres. Fjern uvanlige tegn og prøv igjen.") from None
    if antall_byte > MAKS_DOK_BYTES:
        raise Sidefeil("Siden er for stor. Fjern noe innhold og prøv igjen.")
    return ut, ctx.merknader


# ============================ hvilke filer og blokker som er referert ============================

def fil_ider_i(dok: dict) -> set[int]:
    """ALLE fil_id som er referert (filer-elementer og bilde-blokker), uansett synlighet."""
    ut: set[int] = set()
    for b in dok.get("blokker", []):
        d = b.get("data", {})
        if b.get("type") == "filer":
            ut |= {e["fil_id"] for e in d.get("filer", []) if isinstance(e.get("fil_id"), int)}
        elif b.get("type") == "bilde" and isinstance(d.get("fil_id"), int):
            ut.add(d["fil_id"])
    return ut


def blokk_referanser(dok: dict) -> dict[int, str]:
    """{fil_id: tittel på første blokk som bruker filen}, til «Brukes i blokken «X»»."""
    ut: dict[int, str] = {}
    for b in dok.get("blokker", []):
        d = b.get("data", {})
        ider = ([e.get("fil_id") for e in d.get("filer", [])] if b.get("type") == "filer" else
                [d.get("fil_id")] if b.get("type") == "bilde" else [])
        for i in ider:
            if isinstance(i, int):
                ut.setdefault(i, b.get("tittel") or BLOKKTYPE_NAVN.get(b.get("type"), "blokk"))
    return ut


# ============================ synlighet ============================

def _iso(verdi) -> date | None:
    try:
        return date.fromisoformat(verdi) if isinstance(verdi, str) and _ISO_DATO.match(verdi) else None
    except ValueError:
        return None


def blokk_er_tom(blokk: dict) -> bool:
    """Tom blokk = ingenting å vise (den vises ikke for deltakerne). Merk: Filer-blokken med «vis kursholders filer
    (SharePoint)» regnes som ikke tom, siden filene hentes først ved visning (visningen fjerner blokken hvis det viser seg tomt).
    En blokk med innhold som er endret utenom siden (feil datatyper i databasen) regnes som tom, så den ikke gir en 500-side."""
    try:
        return _blokk_er_tom(blokk)
    except (AttributeError, TypeError, KeyError, ValueError):
        return True


def _blokk_er_tom(blokk: dict) -> bool:
    d = blokk.get("data", {}) or {}
    typ = blokk.get("type")
    if typ == "tekst":
        return not tekst_uten_tagger(rens_html_trygt(d.get("html"))).strip()
    if typ == "viktig":
        return not (d.get("tekst") or "").strip()
    if typ == "program":
        return not any(dag.get("punkter") for dag in d.get("dager", []))
    if typ == "filer":
        return not d.get("filer")
    if typ == "lenker":
        return not any((e.get("url") or e.get("kilde") == "zoom") for e in d.get("lenker", []))
    if typ == "tabell":
        return not any(any((c or "").strip() for c in rad) for rad in d.get("rader", []))
    if typ == "kontakt":
        return not any(any((p.get(f) or "").strip() for f in ("navn", "rolle", "telefon", "epost")) for p in d.get("personer", []))
    if typ == "bilde":
        return d.get("fil_id") is None
    return True


def rens_html_trygt(html) -> str:
    """Som rens_html, men uten lengdegrense og uten feil (til lesing og visning)."""
    return _rens_ra(html if isinstance(html, str) else "")


def blokk_synlig(blokk: dict, idag: date) -> bool:
    """Skjult blokk, blokk som ikke har nådd «vis fra»-datoen, og tom blokk er aldri synlig for deltakerne."""
    if blokk.get("skjult"):
        return False
    fra = _iso(blokk.get("vis_fra"))
    if blokk.get("vis_fra") and (fra is None or fra > idag):
        return False
    return not blokk_er_tom(blokk)


def synlige_blokker(dok: dict, idag: date) -> list[dict]:
    """Blokkene deltakerne får, i rekkefølge. Filer-elementer filtreres IKKE her (se fil_synlig)."""
    return [b for b in dok.get("blokker", []) if blokk_synlig(b, idag)]


def fil_synlig(blokk: dict, element: dict, idag: date) -> str:
    """«ja» (synlig nå), «kommer» (dato i fremtiden og blokken viser «Kommer …») eller «nei» (skjules)."""
    fra = element.get("synlig_fra")
    if not fra:
        return "ja"
    dato = _iso(fra)
    if dato is None:
        return "nei"
    if dato <= idag:
        return "ja"
    return "kommer" if blokk.get("data", {}).get("vis_kommende", True) else "nei"


def synlige_fil_ider(dok: dict, idag: date, utenfor=frozenset()) -> set[int]:
    """fil_id en DELTAKER kan hente i dag: element i synlig Filer-blokk der fil_synlig er «ja», og bildet i en synlig Bilde-blokk.
    `utenfor` er kursdag-id-ene deltakeren ikke er på (en ekstradeltaker på noen samlinger): filer som bare er knyttet til dem hentes
    ikke herfra - samme regel som i listen på kurssiden (deltakerside._filer_data)."""
    ut: set[int] = set()
    for b in synlige_blokker(dok, idag):
        d = b.get("data", {})
        if b["type"] == "filer":
            ut |= {e["fil_id"] for e in d.get("filer", []) if isinstance(e.get("fil_id"), int) and fil_synlig(b, e, idag) == "ja"
                   and e.get("gruppe") not in utenfor}
        elif b["type"] == "bilde" and isinstance(d.get("fil_id"), int):
            ut.add(d["fil_id"])
    return ut


# ============================ hva er endret ============================

def _kort_dato(iso: str | None) -> str:
    d = _iso(iso)
    return d.strftime("%d.%m") if d else ""


def _flertall(n: int, entall: str, flertall: str) -> str:
    return f"{n} {entall if n == 1 else flertall}"


def _program_endring(a: dict, b: dict) -> list[str]:
    ut = []
    gd, nd = a.get("dager", []), b.get("dager", [])
    for i, ny in enumerate(nd):
        navn = ny.get("tittel") or f"Dag {i + 1}"
        if i >= len(gd):
            ut.append(f"{navn}: ny dag med {_flertall(len(ny.get('punkter', [])), 'punkt', 'punkter')}")
            continue
        gp, np_ = gd[i].get("punkter", []), ny.get("punkter", [])
        endret = sum(1 for x, y in zip(gp, np_) if x != y)
        lagt_til, fjernet = max(len(np_) - len(gp), 0), max(len(gp) - len(np_), 0)
        deler = []
        if endret:
            deler.append(f"{_flertall(endret, 'punkt', 'punkter')} endret")
        if lagt_til:
            deler.append(f"{lagt_til} lagt til")
        if fjernet:
            deler.append(f"{fjernet} fjernet")
        if not deler and (gd[i].get("tittel"), gd[i].get("dato"), gd[i].get("kursdag_id")) != (
                ny.get("tittel"), ny.get("dato"), ny.get("kursdag_id")):
            deler.append("dagen er endret")
        if deler:
            ut.append(f"{navn}: " + ", ".join(deler))
    if len(gd) > len(nd):
        ut.append(f"{_flertall(len(gd) - len(nd), 'dag', 'dager')} fjernet")
    return ut


def _endringer_i_blokk(a: dict, b: dict) -> list[str]:
    ut: list[str] = []
    if a.get("tittel") != b.get("tittel"):
        ut.append("Tittelen er endret")
    if a.get("skjult") != b.get("skjult"):
        ut.append("Skjult" if b.get("skjult") else "Vist")
    if a.get("vis_fra") != b.get("vis_fra"):
        ut.append(f"Vises fra {_kort_dato(b.get('vis_fra'))}" if b.get("vis_fra") else "Vises uten dato")
    if a.get("i_meny") != b.get("i_meny"):
        ut.append("Tatt med i «På denne siden»" if b.get("i_meny") else "Tatt ut av «På denne siden»")
    da, db_ = a.get("data", {}), b.get("data", {})
    if da == db_:
        return ut
    typ = b.get("type")
    if typ == "tekst":
        ut.append("Teksten er endret")
    elif typ == "viktig":
        if da.get("niva") != db_.get("niva"):
            ut.append("Nivået er endret")
        if da.get("tekst") != db_.get("tekst"):
            ut.append("Teksten er endret")
    elif typ == "program":
        ut += _program_endring(da, db_) or ["Programmet er endret"]
    elif typ == "filer":
        ga = {e.get("fil_id"): e for e in da.get("filer", [])}
        gb = {e.get("fil_id"): e for e in db_.get("filer", [])}
        nye = [e for i, e in gb.items() if i not in ga]
        borte = [i for i in ga if i not in gb]
        endret = [i for i in gb if i in ga and ga[i] != gb[i]]
        if len(nye) == 1:
            ut.append(f"1 ny fil: «{nye[0].get('tittel') or 'uten tittel'}»")
        elif nye:
            ut.append(f"{len(nye)} nye filer")
        if borte:
            ut.append(f"{_flertall(len(borte), 'fil', 'filer')} fjernet")
        if endret:
            ut.append(f"{_flertall(len(endret), 'fil', 'filer')} endret")
        if da.get("vis_kommende") != db_.get("vis_kommende"):
            ut.append("«Kommer»-visningen er endret")
    elif typ == "lenker":
        la, lb = da.get("lenker", []), db_.get("lenker", [])
        endret = sum(1 for x, y in zip(la, lb) if x != y)
        if len(lb) > len(la):
            ut.append(f"{_flertall(len(lb) - len(la), 'lenke', 'lenker')} lagt til")
        if len(la) > len(lb):
            ut.append(f"{_flertall(len(la) - len(lb), 'lenke', 'lenker')} fjernet")
        if endret:
            ut.append(f"{_flertall(endret, 'lenke', 'lenker')} endret")
    elif typ == "tabell":
        ra, rb = da.get("rader", []), db_.get("rader", [])
        if da.get("kolonner") != db_.get("kolonner"):
            ut.append("Kolonnene er endret")
        if ra != rb:
            ut.append(f"Rader endret ({len(ra)} → {len(rb)})" if len(ra) != len(rb) else f"{_flertall(sum(1 for x, y in zip(ra, rb) if x != y), 'rad', 'rader')} endret")
    elif typ == "kontakt":
        pa, pb = da.get("personer", []), db_.get("personer", [])
        if len(pb) != len(pa):
            ut.append(f"{_flertall(abs(len(pb) - len(pa)), 'person', 'personer')} {'lagt til' if len(pb) > len(pa) else 'fjernet'}")
        endret = sum(1 for x, y in zip(pa, pb) if x != y)
        if endret:
            ut.append(f"{_flertall(endret, 'person', 'personer')} endret")
    elif typ == "bilde":
        if da.get("fil_id") != db_.get("fil_id"):
            ut.append("Bildet er byttet")
        if da.get("alt") != db_.get("alt"):
            ut.append("Alternativ tekst er endret")
        if da.get("tekst") != db_.get("tekst"):
            ut.append("Bildeteksten er endret")
        if da.get("plassering") != db_.get("plassering"):
            ut.append("Plasseringen er endret")
    return ut


def _flyttede(gammel_ider: list[str], ny_ider: list[str]) -> set[str]:
    """Blokkene som er flyttet: de som ikke ligger i den lengste delrekkefølgen som er felles for begge listene."""
    g, n_ = set(gammel_ider), set(ny_ider)
    felles = [i for i in ny_ider if i in g]
    pos = {i: n for n, i in enumerate(i for i in gammel_ider if i in n_)}
    seq = [pos[i] for i in felles]
    n = len(seq)
    lengde, forrige = [1] * n, [-1] * n
    for i in range(n):
        for j in range(i):
            if seq[j] < seq[i] and lengde[j] + 1 > lengde[i]:
                lengde[i], forrige[i] = lengde[j] + 1, j
    if not n:
        return set()
    ende = max(range(n), key=lambda i: lengde[i])
    beholdt = set()
    while ende != -1:
        beholdt.add(felles[ende])
        ende = forrige[ende]
    return set(felles) - beholdt


def diff(gammel: dict | None, ny: dict) -> list[dict]:
    """Hva som er endret mellom to versjoner, per blokk (sammenlignet på blokk-id): ny, endret, fjernet og flyttet. `_side` er
    tittel/ingress/rom, `_melding` er meldingen øverst. gammel=None: alle blokkene er «ny». `tekst` er aldri tom."""
    ut: list[dict] = []
    gammel_blokker = {b["id"]: b for b in (gammel or {}).get("blokker", [])}
    if gammel is not None:
        if any(gammel.get(f) != ny.get(f) for f in ("tittel", "ingress", "rom", "godkjent")):
            ut.append({"id": "_side", "tittel": "Sidens topp", "type": "side", "art": "endret",
                       "tekst": "Tittel, ingress eller rom er endret"})
        if gammel.get("melding") != ny.get("melding"):
            g, n = (gammel.get("melding") or {}).get("tekst"), (ny.get("melding") or {}).get("tekst")
            ut.append({"id": "_melding", "tittel": "Melding øverst", "type": "melding", "art": "endret",
                       "tekst": "Meldingen er lagt til" if (n and not g) else "Meldingen er fjernet" if (g and not n) else "Meldingen er endret"})
    elif ny.get("tittel") or ny.get("ingress") or ny.get("rom") or ny.get("godkjent"):
        ut.append({"id": "_side", "tittel": "Sidens topp", "type": "side", "art": "ny", "tekst": "Tittel, ingress eller rom er lagt inn"})
    flyttet = _flyttede([b["id"] for b in (gammel or {}).get("blokker", [])], [b["id"] for b in ny.get("blokker", [])])
    for b in ny.get("blokker", []):
        navn = b.get("tittel") or BLOKKTYPE_NAVN.get(b["type"], "Blokk")
        g = gammel_blokker.get(b["id"])
        if g is None:
            ut.append({"id": b["id"], "tittel": navn, "type": b["type"], "art": "ny", "tekst": f"Ny blokk ({BLOKKTYPE_NAVN[b['type']].lower()})"})
            continue
        if g != b:
            ut.append({"id": b["id"], "tittel": navn, "type": b["type"], "art": "endret",
                       "tekst": ", ".join(_endringer_i_blokk(g, b)) or "Endret"})
        if b["id"] in flyttet:
            ut.append({"id": b["id"], "tittel": navn, "type": b["type"], "art": "flyttet", "tekst": "Flyttet til en annen plass"})
    ny_ider = {b["id"] for b in ny.get("blokker", [])}
    for bid, g in gammel_blokker.items():
        if bid not in ny_ider:
            ut.append({"id": bid, "tittel": g.get("tittel") or BLOKKTYPE_NAVN.get(g["type"], "Blokk"), "type": g["type"],
                       "art": "fjernet", "tekst": "Blokken er fjernet"})
    return ut


# ============================ publiseringskontroll ============================

_EPOST_I_TEKST = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# 8 sifre (evt. med mellomrom): 12 34 56 78 eller 123 45 678, evt. +47. Datoer (2026-11-27) og klokkeslett (09:00–16:00) har
# skilletegn som ikke er mellomrom, og treffer ikke.
_TELEFON_I_TEKST = re.compile(r"(?<![\d\-:./+])(?:\+47 ?)?(?:\d{2} ?\d{2} ?\d{2} ?\d{2}|\d{3} ?\d{2} ?\d{3})(?![\d\-:./])")


def _tekstfelt_i_blokk(b: dict, *, med_kontakt: bool) -> list[str]:
    """Alle tekstene i en blokk (rik tekst uten tagger, men med lenkeadresser). Kontaktfeltene er med bare når `med_kontakt`."""
    d = b.get("data", {}) or {}
    ut = [b.get("tittel", "")]
    typ = b.get("type")
    if typ == "tekst":
        ut += [tekst_uten_tagger(d.get("html", "")), *re.findall(r'href="([^"]*)"', d.get("html", ""))]
    elif typ == "viktig":
        ut.append(d.get("tekst", ""))
    elif typ == "program":
        for dag in d.get("dager", []):
            ut.append(dag.get("tittel", ""))
            for p in dag.get("punkter", []):
                ut += [p.get("tema", ""), p.get("sted", ""), p.get("hvem", "")]
    elif typ == "filer":
        ut += [e.get("tittel", "") for e in d.get("filer", [])]
    elif typ == "lenker":
        for e in d.get("lenker", []):
            ut += [e.get("tittel", ""), e.get("tekst", ""), e.get("url", "")]
    elif typ == "tabell":
        ut += list(d.get("kolonner", []))
        for rad in d.get("rader", []):
            ut += list(rad)
    elif typ == "kontakt" and med_kontakt:
        for p in d.get("personer", []):
            ut += [p.get("navn", ""), p.get("rolle", ""), p.get("telefon", ""), p.get("epost", "")]
    elif typ == "bilde":
        ut += [d.get("alt", ""), d.get("tekst", "")]
    return [x for x in ut if isinstance(x, str)]


def publiseringssjekk(dok: dict, *, filer: dict[int, dict], bekreftede_epost: set[str], kursdager: list[dict],
                      idag: date) -> tuple[list[dict], list[dict]]:
    """(feil, advarsler). Feil stopper publiseringen: e-postadressen til en bekreftet deltaker står i teksten (også i
    kontaktfelt og lenker), bilde med fil men uten alternativ tekst, og en fil siden viser som ikke finnes. Advarsler kan
    publiseres: tom blokk (vises ikke), e-postadresser og telefonnumre i vanlig tekst, tabell med rader (navn vises for alle
    på kurset), filer som er lastet opp men ikke brukt, programdag koblet til en kursdag som er borte, og lenke uten adresse."""
    feil: list[dict] = []
    advarsler: list[dict] = []
    epost_set = {e.lower() for e in bekreftede_epost}
    kursdag_ider = {k.get("id") for k in kursdager}

    def f(blokk_id, tekst):
        feil.append({"blokk_id": blokk_id, "tekst": tekst})

    def a(blokk_id, tekst):
        advarsler.append({"blokk_id": blokk_id, "tekst": tekst})

    toppfelt = [dok.get("tittel", ""), dok.get("ingress", ""), dok.get("rom", ""), dok.get("godkjent", ""), (dok.get("melding") or {}).get("tekst", "")]
    for t in toppfelt:
        if isinstance(t, str) and any(e.lower() in epost_set for e in _EPOST_I_TEKST.findall(t)):
            f(None, "E-postadressen til en deltaker står i sidens topp. Fjern den før du publiserer.")
            break
    for b in dok.get("blokker", []):
        bid, typ = b.get("id"), b.get("type")
        navn = b.get("tittel") or BLOKKTYPE_NAVN.get(typ, "Blokk")
        alle = _tekstfelt_i_blokk(b, med_kontakt=True)
        if any(e.lower() in epost_set for t in alle for e in _EPOST_I_TEKST.findall(t)):
            f(bid, f"«{navn}» inneholder e-postadressen til en deltaker på kurset. Fjern den før du publiserer.")
        if blokk_er_tom(b):
            a(bid, f"«{navn}» er tom og vises ikke for deltakerne.")
        if typ != "kontakt":
            vanlig = _tekstfelt_i_blokk(b, med_kontakt=False)
            if any(_EPOST_I_TEKST.search(t) for t in vanlig):
                a(bid, f"«{navn}» inneholder en e-postadresse. Kontaktopplysninger hører hjemme i en Kontakt-blokk.")
            if any(_TELEFON_I_TEKST.search(t) for t in vanlig):
                a(bid, f"«{navn}» ser ut til å inneholde et telefonnummer. Kontaktopplysninger hører hjemme i en Kontakt-blokk.")
        d = b.get("data", {}) or {}
        if typ == "tabell" and any(any((c or "").strip() for c in rad) for rad in d.get("rader", [])):
            a(bid, f"«{navn}» viser navn til alle som er påmeldt kurset. Bruk fornavn og forbokstav, og ikke e-post eller telefon.")
        if typ == "bilde" and d.get("fil_id") is not None and not (d.get("alt") or "").strip():
            f(bid, f"Bildet i «{navn}» mangler alternativ tekst. Skriv en kort beskrivelse.")
        if typ in ("bilde", "filer"):
            ider = [d.get("fil_id")] if typ == "bilde" else [e.get("fil_id") for e in d.get("filer", [])]
            if any(i is not None and i not in filer for i in ider):
                f(bid, f"«{navn}» viser en fil som ikke finnes lenger. Fjern den fra siden.")
        if typ == "program":
            program_dager = d.get("dager", [])
            for dag in program_dager:
                if dag.get("kursdag_id") is not None and dag["kursdag_id"] not in kursdag_ider:
                    a(bid, f"En programdag i «{navn}» er koblet til en kursdag som ikke finnes lenger.")
                    break
            udaterte = [dag.get("tittel") or f"Dag {n}" for n, dag in enumerate(program_dager, 1)
                        if dag.get("kursdag_id") is None and not dag.get("dato")]
            if udaterte:
                liste = ", ".join(f"«{t}»" for t in udaterte[:3]) + ("…" if len(udaterte) > 3 else "")
                a(bid, f"{'Programdagen' if len(udaterte) == 1 else 'Programdagene'} {liste} i «{navn}» har ingen dato og hører ikke til en kursdag. "
                       "Velg en dato, eller fjern dagen.")
        if typ == "lenker" and any(not e.get("url") and e.get("kilde") != "zoom" for e in d.get("lenker", [])):
            a(bid, f"En lenke i «{navn}» mangler gyldig adresse og vises ikke.")
    brukt = fil_ider_i(dok)
    ubrukte = [v.get("filnavn") or "fil" for i, v in filer.items() if i not in brukt]
    if ubrukte:
        a(None, f"{_flertall(len(ubrukte), 'fil er', 'filer er')} lastet opp, men ikke brukt på siden: "
                + ", ".join(f"«{n}»" for n in ubrukte[:5]) + ("…" if len(ubrukte) > 5 else "") + ".")
    return feil, advarsler


# ============================ tabeller etter kurset ============================

def tom_tabeller(dok: dict) -> tuple[dict, int]:
    """Tømmer radene i alle Tabell-blokker (gruppelister med navn). Returnerer (nytt dokument, antall rader fjernet).
    Kolonneoverskriftene beholdes."""
    ny = json.loads(til_json(dok))
    antall = 0
    for b in ny.get("blokker", []):
        if b.get("type") == "tabell":
            rader = b.get("data", {}).get("rader", [])
            antall += len(rader)
            b["data"]["rader"] = []
    return ny, antall


def har_tabellrader(dok: dict) -> bool:
    return any(b.get("type") == "tabell" and b.get("data", {}).get("rader") for b in dok.get("blokker", []))


# ============================ maler og kopiering ============================

def program_fra_kursdager(kursdager: list[dict]) -> list[dict]:
    """Én programdag per kursdag, koblet til kursdagen (datoen følger da kursets datoer)."""
    return [{"kursdag_id": kd.get("id"), "dato": kd.get("dato"), "tittel": f"Dag {n}", "punkter": []}
            for n, kd in enumerate(kursdager, 1)]


def _ny_blokk(typ: str, data: dict, tittel: str | None = None, **felt) -> dict:
    return {"id": ny_blokk_id(), "type": typ, "tittel": STANDARDTITLER[typ] if tittel is None else tittel, "skjult": False,
            "vis_fra": None, "i_meny": True, "data": data, **felt}


def ny_blokk(typ: str) -> dict:
    """En ny, tom blokk av typen (til redigeringsvisningen og testene)."""
    tom = {"tekst": {"html": ""}, "viktig": {"niva": "info", "tekst": ""}, "program": {"dager": []},
           "filer": {"filer": [], "vis_kommende": True}, "lenker": {"lenker": []},
           "tabell": {"kolonner": ["Gruppe", "Deltakere"], "rader": []}, "kontakt": {"personer": []},
           "bilde": {"fil_id": None, "alt": "", "tekst": "", "plassering": "bred"}}
    if typ not in BLOKKTYPER:
        raise Sidefeil("Ukjent blokktype.")
    return _ny_blokk(typ, tom[typ])


def standardmal(kursdager: list[dict]) -> dict:
    """Program (fra kursdatoene), Presentasjoner og dokumenter, Litteratur og lenker, Kontakt.
    Blokkene uten innhold vises ikke for deltakerne før de er fylt ut. Ingen «Velkommen»-tekstboks (Camilla 02.10.2026: «Velkommen-boksen går
    ut av malen»): den sto ved siden av velkomsten øverst på siden. Kursholder kan legge til en tekstblokk selv."""
    dok = tomt_dokument()
    dok["blokker"] = [
        _ny_blokk("program", {"dager": program_fra_kursdager(kursdager)}),
        _ny_blokk("filer", {"filer": [], "vis_kommende": True}),
        _ny_blokk("lenker", {"lenker": []}),
        _ny_blokk("kontakt", {"personer": []}),
    ]
    return dok


def mal(navn: str, kursdager: list[dict]) -> dict:
    """«standard», «program» (bare program fra kursdatoene) eller «tom»."""
    if navn == "standard":
        return standardmal(kursdager)
    dok = tomt_dokument()
    if navn == "program":
        dok["blokker"] = [_ny_blokk("program", {"dager": program_fra_kursdager(kursdager)})]
    elif navn != "tom":
        raise Sidefeil("Ukjent mal.")
    return dok


def kopier_blokker(dok: dict, blokk_ider: list[str], *, fil_kart: dict[int, int], kursdager: list[dict]) -> list[dict]:
    """Blokkene til et annet kurs: nye blokk-id-er, fil_id ommappet via `fil_kart` (ukjent fil droppes), Tabell-rader TØMMES
    (aldri personer med), programdagene knyttes til målkursets kursdager i rekkefølge (overskytende dager får ingen
    kobling), «vis fra» og «synlig fra» nullstilles, «skjult» beholdes."""
    valgt = set(blokk_ider)
    ut = []
    for b in dok.get("blokker", []):
        if b.get("id") not in valgt:
            continue
        ny = json.loads(json.dumps(b))
        ny["id"], ny["vis_fra"] = ny_blokk_id(), None
        d = ny["data"]
        if ny["type"] == "tabell":
            d["rader"] = []
        elif ny["type"] == "program":
            for n, dag in enumerate(d.get("dager", [])):
                mål = kursdager[n] if n < len(kursdager) else None
                dag["kursdag_id"], dag["dato"] = (mål.get("id"), mål.get("dato")) if mål else (None, None)
        elif ny["type"] == "filer":
            d["filer"] = [{**e, "fil_id": fil_kart[e["fil_id"]], "gruppe": None, "synlig_fra": None}
                          for e in d.get("filer", []) if e.get("fil_id") in fil_kart]
        elif ny["type"] == "bilde":
            d["fil_id"] = fil_kart.get(d.get("fil_id"))
        ut.append(ny)
    return ut
