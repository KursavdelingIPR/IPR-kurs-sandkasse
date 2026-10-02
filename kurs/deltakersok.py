"""Globalt deltakersøk for administratorer: finn en person på tvers av ALLE kurs.

Bruksområdet: «Kari sier hun mangler kursbevis på kurset hun deltok på». Da må administrasjonen raskt finne henne uten å
vite hvilket kurs det gjelder, og se om kursbeviset er utstedt (og hvorfor ikke, hvis det mangler). Brukes av søkefeltet i
toppmenyen (rullegardin), Oversikt og resultatsiden /admin/sok. Modulen er ren logikk: den tar en databasetilkobling og
kjenner ikke Flask.

Hva det søkes i (flere ord = ALLE ord må treffe, hvert ord i minst ett av feltene):
  * navn (delvis, fornavn og etternavn i begge rekkefølger), e-post (delvis). æøå og aksenter spiller ingen rolle: «solvi»
    finner «Sølvi», og «håkon» finner «Hakon» (se _ascii)
  * telefon: mellomrom, bindestrek, parentes og landskode (+47 / 0047) ignoreres; delvis treff fra 4 sifre
  * HPR-nummer (fra 4 sifre), påmeldingsnummer (tallet som vises som #N i deltakervinduet, «#12» eller bare «12»)
  * kursnummer og kurskode (delvis fra MIN_KURSKODE tegn, aldri rene tall): da treffer alle som er påmeldt kurset (uansett
    status)
  * innliming fra e-postprogram: «Kari Hansen <kari.hansen@eksempel.no>», «(adresse)», «[adresse]» og «mailto:adresse» virker
    (tegnsetting og parenteser rundt hvert ord fjernes); enkeltbokstaver som egne ord ignoreres

Regler (personvern og likhet mellom demo og drift):
  * Sammenligningen gjøres i Python (casefold) på rader fra databasen, ikke med SQL LIKE/LOWER: SQLite sin LOWER() virker
    bare på ASCII, så «Ørjan» og «Åse» ville feilet i demo, mens PostgreSQL oppfører seg annerledes. Slik blir alt likt.
  * Søketeksten går aldri inn i SQL (bare tall som parametre) og aldri i noen logg. %, _, \\ og anførselstegn er vanlig tekst.
  * Anonymiserte personer (db.anonymiser_deltaker) finnes ikke i søket.
  * Tabellen `sensitivt` (allergier/tilrettelegging) og innloggingslenker leses aldri her.
  * Kursbevis-forklaringen bruker de samme reglene og tabellene som kursbevis.kjor (kursbevis.MIN_ANDEL, oppmote, kursdag,
    dokument), men endrer ingenting og sender ingenting. Fra utsending_logg leses bare status og dato for kursbevis-e-posten
    (aldri innholdet), slik at «Kari sier hun ikke fikk det» kan besvares.
"""
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from . import db, kursbevis
from .deltakerliste import _sortering as norsk_sortering

MIN_TEGN = 2                 # kortere søk gir bare hjelpetekst
MAKS_TEGN = 100              # lengre søk avkortes (og sies fra om)
MAKS_ORD = 6                 # flere ord enn dette ignoreres
GRENSE_SIDE = 50             # personer på resultatsiden
GRENSE_LISTE = 8             # personer i rullegardinen
MIN_SIFFER = 4               # delvis treff på telefon og HPR-nummer krever så mange sifre
MIN_ORD = 2                  # ord kortere enn dette (unntatt tall) ignoreres: «k h» skal ikke gi tilfeldige treff
MIN_KURSKODE = 4             # delvis treff på kurskode krever så mange tegn (tre ga støy: «vig» traff både Vigdis og VIG-kursene)
FORSINKET_DAGER = 2          # så mange dager etter siste kursdag skal morgenjobben ha avsluttet kurset og utstedt kursbevis
MAKS_NR_SIFRE = 9            # lengre tall er aldri påmeldings- eller kursnummer (og ville sprengt databasens heltall)
MAKS_KURSTREFF = 3           # «Åpne deltakerlisten»-snarveien vises bare når søket peker på få kurs
BIT_STORRELSE = 400          # så mange id-er i hver IN (...)-spørring (databasene har en grense for antall parametre)

_TELEFONTEGN = re.compile(r"[0-9\s+().\-]+", re.ASCII)
_KANTTEGN = ",;:\"'«»“”„<>()[]"      # rundt hvert ord: «Hansen,» «<adresse>» «(adresse)» «[adresse]» (fra e-postprogram)
_ORDSKILLE = re.compile(r"[\s\-]+")
_ANONYM_SLUTT = "@" + db.ANONYM_DOMENE

GRUNN_REKKEFOLGE = ("pnr", "telefon", "epost", "hpr", "kurs", "navn")


def _fold(tekst) -> str:
    """Små bokstaver for hele Unicode (også Æ, Ø, Å) og sammensatte tegn: «Å» skrevet som A + ring over blir vanlig «å»."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", str(tekst or "")).casefold())


_ASCII_BOKSTAVER = {"ø": "o", "æ": "ae", "å": "a", "ð": "d", "þ": "th", "ł": "l", "đ": "d"}


def _ascii_tegn(tegn: str) -> str:
    """Ett tegn (allerede med små bokstaver) uten aksent: «ø» blir «o», «æ» «ae», «å» «a», «é» «e», «ü» «u»."""
    tegn = _ASCII_BOKSTAVER.get(tegn, tegn)
    if tegn.isascii():
        return tegn
    return "".join(c for c in unicodedata.normalize("NFD", tegn) if not unicodedata.combining(c))


def _ascii(tekst) -> str:
    """Som _fold, men uten æøå og aksenter («Sølvi Tørresen» blir «solvi torresen»). Søket sammenligner både med _fold og med
    _ascii, så «solvi» finner «Sølvi» og «håkon» finner en som er registrert som «Hakon» (skjemaet tar imot begge deler)."""
    return _ascii_fra_fold(_fold(tekst))


def _ascii_fra_fold(folded: str) -> str:
    return folded if folded.isascii() else "".join(_ascii_tegn(t) for t in folded)


def _siffer(tekst: str) -> str:
    return "".join(c for c in tekst if "0" <= c <= "9")


def normaliser_telefon(tekst: str | None) -> str:
    """Bare sifre, uten landskode: «+47 900 12 345», «0047 90012345» og «900-12-345» blir alle «90012345». Brukes på både
    lagrede numre og søk. Et tall på ti sifre som starter med 47 regnes også å ha norsk landskode."""
    t = (tekst or "").strip()
    if t.startswith("+47"):
        t = t[3:]
    elif t.startswith("0047"):
        t = t[4:]
    s = _siffer(t)
    return s[2:] if len(s) == 10 and s.startswith("47") else s


@dataclass(frozen=True)
class Term:
    tekst: str                   # ordet, med små bokstaver
    siffer: str = ""             # bare sifre når ordet ligner et telefon-/HPR-nummer, ellers ""
    pnr: int | None = None       # mulig påmeldingsnummer: «#12» eller bare tall
    kursnr: int | None = None    # mulig kursnummer: bare tall
    kun_pnr: bool = False        # «#12»: skal bare tolkes som påmeldingsnummer
    ascii: str = ""              # ordet uten æøå og aksenter (se _ascii)


def _term(ord_: str) -> Term:
    t = _fold(ord_)
    m = re.fullmatch(r"#([0-9]{1,%d})" % MAKS_NR_SIFRE, t)
    if m:
        return Term(t, pnr=int(m.group(1)), kun_pnr=True, ascii=t)
    siffer = normaliser_telefon(t) if _TELEFONTEGN.fullmatch(t) and _siffer(t) else ""
    tall = int(t) if re.fullmatch(r"[0-9]{1,%d}" % MAKS_NR_SIFRE, t) else None
    return Term(t, siffer, pnr=tall, kursnr=tall, ascii=_ascii_fra_fold(t))


@dataclass(frozen=True)
class Sok:
    """Et tolket søk. `tekst` er renset og avkortet (det som vises tilbake i søkefeltet)."""
    tekst: str
    termer: tuple = ()
    avkortet: bool = False

    @property
    def tom(self) -> bool:
        return not self.tekst

    @property
    def for_kort(self) -> bool:
        return bool(self.tekst) and (len(self.tekst) < MIN_TEGN or not self.termer)

    @property
    def gyldig(self) -> bool:
        return bool(self.termer) and not self.for_kort


def tolk(tekst) -> Sok:
    """Renser og tolker søketeksten. Kontrolltegn blir mellomrom, tekster over MAKS_TEGN avkortes, og ord ut over MAKS_ORD
    ignoreres. Et telefonnummer skrevet med mellomrom («+47 900 12 345») er ett søkeord, ikke flere. Tegnsetting og parenteser
    rundt ordene fjernes, så «Kari Hansen <kari.hansen@eksempel.no>» (limt inn fra Outlook) søker på tre ord."""
    rå = "" if tekst is None else str(tekst)
    rå = re.sub(r"[\s\x00-\x1f\x7f-\x9f]+", " ", rå[:MAKS_TEGN * 4]).strip()
    avkortet = len(rå) > MAKS_TEGN
    rå = rå[:MAKS_TEGN].rstrip()
    if not rå:
        return Sok("")
    ord_ = _slaa_sammen_nummer([o for o in (_rens_ord(o) for o in rå.split(" ")) if o])
    ord_ = [o for o in ord_ if len(o) >= MIN_ORD or o.isdigit()]     # enkeltbokstaver (initialer) ignoreres, tall er tall
    avkortet = avkortet or len(ord_) > MAKS_ORD
    return Sok(rå, tuple(_term(o) for o in ord_[:MAKS_ORD]), avkortet)


def _rens_ord(ord_: str) -> str:
    """Fjerner tegnsetting, vinkel- og andre parenteser rundt ordet, «mailto:» foran en adresse og punktum til slutt
    («Hansen,» -> «Hansen», «<kari@x.no>» -> «kari@x.no», «mailto:kari@x.no» -> «kari@x.no», «kari.hansen@x.no.» -> uten siste punktum)."""
    o = ord_.strip(_KANTTEGN)
    if o[:7].lower() == "mailto:":
        o = o[7:]
    return o.strip(_KANTTEGN).rstrip(".")


def _er_nummerbit(ord_: str) -> bool:
    return bool(_TELEFONTEGN.fullmatch(ord_)) and bool(_siffer(ord_))


def _slaa_sammen_nummer(ord_: list[str]) -> list[str]:
    """Et telefonnummer skrevet i grupper («kari 900 12 345», «+47 411 22 333», «0047 90012345») er ett søkeord, ikke flere.
    Rader av tall slås sammen når de til sammen har minst MIN_SIFFER sifre og enten begynner med landskode (+47, 0047) eller
    har minst ett kort ledd (1-3 sifre). Tall som alle har fire sifre eller mer («1001 2027») er egne ord."""
    ut, gruppe = [], []

    def tom_gruppe():
        sifre = sum(len(_siffer(o)) for o in gruppe)
        landskode = gruppe and gruppe[0].lstrip("(").startswith(("+47", "0047"))
        if len(gruppe) > 1 and sifre >= MIN_SIFFER and (landskode or any(len(_siffer(o)) <= 3 for o in gruppe)):
            ut.append(" ".join(gruppe))
        else:
            ut.extend(gruppe)
        gruppe.clear()

    for o in ord_:
        if _er_nummerbit(o):
            gruppe.append(o)
        else:
            tom_gruppe()
            ut.append(o)
    tom_gruppe()
    return ut


# ============================ treff ============================

def _markeringer(tekst: str | None, ord_: list[str]) -> list[tuple[str, bool]]:
    """Deler teksten i biter (tekst, treff) slik at siden kan utheve det søket traff. Ingen HTML lages her - malen og
    JavaScript skriver bitene som ren tekst. Sammenligningen er den samme som i søket (casefold, og uten æøå/aksenter)."""
    tekst = unicodedata.normalize("NFC", tekst or "")
    spenn = []
    for per_tegn, ordene in ((lambda t: t.casefold(), ord_), (lambda t: _ascii_tegn(t.casefold()), [_ascii(o) for o in ord_])):
        folded, kart = "", []
        for i, tegn in enumerate(tekst):
            f = per_tegn(tegn)
            folded += f
            kart.extend([i] * len(f))
        for o in ordene:                    # første runde: som skrevet (æøå), andre runde: uten æøå og aksenter
            start = 0
            while o and (pos := folded.find(o, start)) >= 0:
                spenn.append((kart[pos], kart[pos + len(o) - 1] + 1))
                start = pos + len(o)
    sammen: list[list[int]] = []
    for a, b in sorted(spenn):
        if sammen and a <= sammen[-1][1]:
            sammen[-1][1] = max(sammen[-1][1], b)
        else:
            sammen.append([a, b])
    deler, pos = [], 0
    for a, b in sammen:
        if a > pos:
            deler.append((tekst[pos:a], False))
        deler.append((tekst[a:b], True))
        pos = b
    if pos < len(tekst):
        deler.append((tekst[pos:], False))
    return deler or [(tekst, False)]


def _i_biter(ider, storrelse: int | None = None):
    storrelse = storrelse or BIT_STORRELSE
    ider = list(ider)
    for i in range(0, len(ider), storrelse):
        yield ider[i:i + storrelse]


def _plasser(ider) -> str:
    return ",".join("?" * len(ider))


def _kandidater(con) -> list[dict]:
    """Alle personer som kan finnes (anonymiserte er utelatt), bare feltene søket bruker. Ingen allergier, ingen lenker."""
    rader = con.execute(
        """SELECT id, navn, fornavn, etternavn, epost, telefon, arbeidssted, hpr_nr FROM deltaker
           WHERE LOWER(epost) NOT LIKE ?""", ("%" + _ANONYM_SLUTT,)).fetchall()
    return [dict(r) for r in rader if not str(r["epost"]).lower().endswith(_ANONYM_SLUTT)]


def _slaa_opp_paameldinger(con, termer) -> tuple[list, dict, list]:
    """Treff som ikke ligger på selve personen: påmeldingsnummer og kurs (kursnummer/kurskode). Returnerer
    (via, kurs_for_paamelding, eksakte_kurs), der via[i] = {"pnr": {deltaker_id: {paamelding_id}}, "kurs": {...}} for
    ord nr. i. Bare tall går som parametre til databasen."""
    via = [{"pnr": {}, "kurs": {}} for _ in termer]
    kurs_per_ord: list[dict] = [{} for _ in termer]
    eksakte: dict = {}
    for k in (dict(r) for r in con.execute("SELECT id, kursnr, kode, navn FROM kurs")):
        kode = _fold(k["kode"])
        for i, t in enumerate(termer):
            eksakt = (t.kursnr is not None and k["kursnr"] == t.kursnr) or (not t.kun_pnr and t.tekst == kode)
            delvis = (not t.kun_pnr and not t.tekst.isdigit() and len(t.tekst) >= MIN_KURSKODE and t.tekst in kode)
            if eksakt or delvis:
                kurs_per_ord[i][k["id"]] = k
            if eksakt:
                eksakte[k["id"]] = k
    kurs_for_paamelding: dict[int, dict] = {}
    for kurs_ider in _i_biter({kid for d in kurs_per_ord for kid in d}):
        for r in con.execute(f"SELECT id, deltaker_id, kurs_id FROM paamelding WHERE kurs_id IN ({_plasser(kurs_ider)})",
                             kurs_ider):
            for i, d in enumerate(kurs_per_ord):
                if r["kurs_id"] in d:
                    via[i]["kurs"].setdefault(r["deltaker_id"], set()).add(r["id"])
                    kurs_for_paamelding[r["id"]] = d[r["kurs_id"]]
    pnr = sorted({t.pnr for t in termer if t.pnr is not None})
    if pnr:
        for r in con.execute(f"SELECT id, deltaker_id FROM paamelding WHERE id IN ({_plasser(pnr)})", pnr):
            for i, t in enumerate(termer):
                if t.pnr == r["id"]:
                    via[i]["pnr"].setdefault(r["deltaker_id"], set()).add(r["id"])
    return via, kurs_for_paamelding, sorted(eksakte.values(), key=lambda k: k["id"])[:MAKS_KURSTREFF + 1]


def _treffer_person(p: dict, termer, via) -> tuple[set, bool, set, set] | None:
    """(grunner, eksakt, pnr_ider, kurs_ider) hvis ALLE ord treffer personen, ellers None. `eksakt` = nøyaktig e-post,
    telefon, HPR- eller påmeldingsnummer. De to siste er påmeldingene som traff på nummer og via kurset."""
    navn, epost = _fold(p["navn"]), _fold(p["epost"])
    navn_a, epost_a = _ascii_fra_fold(navn), _ascii_fra_fold(epost)     # samme tekst uten æøå og aksenter (ofte identisk)
    tlf_norm = tlf_full = hpr = None
    grunner, eksakt, pnr_ider, kurs_ider = set(), False, set(), set()
    for i, t in enumerate(termer):
        treff = set()
        if not t.kun_pnr:
            if t.tekst in navn or t.ascii in navn_a:
                treff.add("navn")
            if t.tekst in epost or t.ascii in epost_a:
                treff.add("epost")
                eksakt = eksakt or epost == t.tekst
            if len(t.siffer) >= MIN_SIFFER:
                if tlf_norm is None:
                    tlf_norm, tlf_full = normaliser_telefon(p["telefon"]), _siffer(p["telefon"] or "")
                    hpr = _siffer(p["hpr_nr"] or "")
                if t.siffer in tlf_norm or t.siffer in tlf_full:
                    treff.add("telefon")
                    eksakt = eksakt or t.siffer in (tlf_norm, tlf_full)
                if t.siffer in hpr:
                    treff.add("hpr")
                    eksakt = eksakt or t.siffer == hpr
        if "navn" in treff:
            treff.discard("epost")          # e-postadresser er ofte laget av navnet: da er navnet grunnen som vises
        if p["id"] in via[i]["pnr"]:
            treff.add("pnr")
            eksakt = True
            pnr_ider |= via[i]["pnr"][p["id"]]
        if p["id"] in via[i]["kurs"]:
            treff.add("kurs")
            kurs_ider |= via[i]["kurs"][p["id"]]
        if not treff:
            return None
        grunner |= treff
    return grunner, eksakt, pnr_ider, kurs_ider


def _navn_starter_med(navn: str, termer) -> bool:
    """Alle ord i søket er starten på hvert sitt ord i navnet (fornavn, etternavn eller mellomnavn, i hvilken som helst
    rekkefølge): «kari han» og «han kari» starter begge «Kari Hansen»."""
    ord_ = _ORDSKILLE.split(_fold(navn))
    ord_a = _ORDSKILLE.split(_ascii(navn))
    return all(any(o.startswith(t.tekst) for o in ord_) or any(o.startswith(t.ascii) for o in ord_a) for t in termer)


def grunn_tekst(grunner: list[str]) -> str:
    """«Treff på navn», «Treff på telefon og e-post», «Treff på telefon, kurs EFT-2027-1 og navn» (rekkefølgen er GRUNN_REKKEFOLGE)."""
    if not grunner:
        return ""
    if len(grunner) == 1:
        return f"Treff på {grunner[0]}"
    return f"Treff på {', '.join(grunner[:-1])} og {grunner[-1]}"


def _grunn_liste(grunner: set, pnr_ider: set, kurs_ider: set, kurs_for_paamelding: dict) -> list[str]:
    ut = []
    for g in GRUNN_REKKEFOLGE:
        if g not in grunner:
            continue
        if g == "pnr":
            ut.append("påmeldingsnr. " + ", ".join(f"#{i}" for i in sorted(pnr_ider)[:3]))
        elif g == "kurs":
            koder = sorted({k["kode"] for pid, k in kurs_for_paamelding.items() if pid in kurs_ider})
            ut.append("kurs " + ", ".join(koder[:2]) + (f" og {len(koder) - 2} til" if len(koder) > 2 else ""))
        else:
            ut.append({"telefon": "telefon", "epost": "e-post", "hpr": "HPR-nummer", "navn": "navn"}[g])
    return ut


# ============================ påmeldinger og kursbevis ============================

def _dato(iso) -> str:
    iso = str(iso or "")[:10]
    return f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}" if len(iso) == 10 else ""


def oppmote_tekst(mott: int, dager: int, startet: bool, bekreftet: bool) -> str:
    """«1 av 2 kursdager» når kurset har startet (og personen er bekreftet eller har møtt), ellers «–»."""
    if not dager or not startet or not (bekreftet or mott):
        return "–"
    return f"{mott} av {dager} kursdag{'er' if dager != 1 else ''}"


def _dager_siden(iso, idag: date | None) -> int | None:
    """Hvor mange dager siden datoen (ISO) det er, eller None når datoen eller dagens dato mangler."""
    try:
        return (idag - date.fromisoformat(str(iso)[:10])).days if idag and iso else None
    except ValueError:
        return None


def kursbevis_forklaring(*, utstedt: str | None, kurs_status: str, paamelding_status: str, dager: int, mott: int,
                         siste_dato: str | None = None, idag: date | None = None) -> tuple[str, str]:
    """Hva som står om kursbeviset for én påmelding: (tekst, farge). Samme regler som kursbevis.kjor: bevis utstedes for
    påmeldte på avsluttede kurs når andelen kursdager med oppmøte er minst kursbevis.MIN_ANDEL - av
    morgenjobben, en gang. Morgenjobben avslutter kurset og utsteder beviset dagen etter siste kursdag; er det gått
    FORSINKET_DAGER eller mer (`siste_dato` og `idag` oppgitt) uten at det har skjedd, sier teksten fra om at noe er galt
    i stedet for å love at beviset «kommer»."""
    if utstedt:
        return f"Utstedt {_dato(utstedt)}", "ok"
    if kurs_status == "avlyst":
        return "Kurset er avlyst – kursbevis utstedes ikke", "gra"
    siden = _dager_siden(siste_dato, idag)          # dager siden siste kursdag (None = ukjent, 0 eller mindre = ikke ferdig)
    forsinket = siden is not None and siden >= FORSINKET_DAGER
    if kurs_status != "avsluttet":
        if siden is None or siden < 1:
            return "Kurset er ikke avsluttet ennå", "gra"
        if forsinket:
            return (f"Kurset sluttet {_dato(siste_dato)}, men er ikke avsluttet i systemet – sjekk Daglig kjøring og "
                    "hendelsesloggen"), "feil"
        lofte = " – morgenjobben avslutter det og utsteder kursbevis" if paamelding_status == "bekreftet" else ""
        return f"Kurset er ferdig, men ikke avsluttet i systemet ennå{lofte}", "gra"
    if paamelding_status != "bekreftet":
        return "Ikke utstedt: påmeldingen er ikke bekreftet", "gra"
    if not dager:
        return "Ikke utstedt: kurset har ingen kursdager", "gra"
    if mott / dager < kursbevis.MIN_ANDEL:
        krav = "fullt oppmøte" if kursbevis.MIN_ANDEL >= 1 else f"minst {round(kursbevis.MIN_ANDEL * 100)} % oppmøte"
        return (f"Ikke utstedt: oppmøte {mott} av {dager} dag{'er' if dager != 1 else ''} (kursbevis utstedes ved {krav})",
                "gul")
    if forsinket:
        return (f"Oppfyller vilkårene, men er ikke utstedt (kurset sluttet {_dato(siste_dato)}) – sjekk Daglig kjøring og "
                "hendelsesloggen"), "feil"
    return "Ikke utstedt ennå: utstedes av morgenjobben", "gul"


def epost_om_kursbevis(rad) -> str:
    """Hva loggen sier om e-posten som følger med et utstedt kursbevis: `rad` er raden i utsending_logg (eller None)."""
    if rad is None:
        return "Ingen e-postutsending registrert til nåværende adresse"
    if rad["status"] == "sendt":
        return f"E-post sendt {_dato(rad['sendt_ts'])}"
    return "E-posten er ikke bekreftet sendt – se hendelsesloggen"


def _paameldinger(con, deltakere: dict[int, str], idag: date, treff_pid: dict[int, set]) -> dict[int, list[dict]]:
    """Alle påmeldingene til personene (`deltakere`: id -> e-post), nyeste kurs først, med kursdatoer, oppmøte og kursbevis.
    Radene har feltene status/avslatt_ts/utgatt_ts/forlatt_ts/ekstradeltaker_ts slik at malens filtre `paameldingsstatus` og `statusmerke`
    virker på dem."""
    deltaker_ider = list(deltakere)
    if not deltaker_ider:
        return {}
    ph = _plasser(deltaker_ider)
    rader = con.execute(
        f"""SELECT p.id AS paamelding_id, p.deltaker_id, p.kurs_id, p.status, p.avslatt_ts, p.utgatt_ts, p.forlatt_ts,
                   p.ekstradeltaker_ts, p.opprettet, k.navn AS kurs_navn, k.kode, k.kursnr, k.status AS kurs_status
            FROM paamelding p JOIN kurs k ON k.id=p.kurs_id WHERE p.deltaker_id IN ({ph})""", deltaker_ider).fetchall()
    dager = {r["kurs_id"]: r for r in con.execute(
        f"""SELECT kd.kurs_id, COUNT(*) AS antall, MIN(kd.dato) AS forste, MAX(kd.dato) AS siste FROM kursdag kd
            WHERE kd.kurs_id IN (SELECT kurs_id FROM paamelding WHERE deltaker_id IN ({ph})) GROUP BY kd.kurs_id""",
        deltaker_ider)}
    mott = {r["paamelding_id"]: r["antall"] for r in con.execute(
        f"""SELECT o.paamelding_id, COUNT(*) AS antall FROM oppmote o JOIN paamelding p ON p.id=o.paamelding_id
            JOIN kursdag kd ON kd.id=o.kursdag_id WHERE p.deltaker_id IN ({ph}) GROUP BY o.paamelding_id""",
        deltaker_ider)}
    bevis = {}                                     # (deltaker, kurs) -> (utstedt, dokument-id): den siste utstedelsen
    for r in con.execute(f"""SELECT id, deltaker_id, kurs_id, opprettet FROM dokument
                             WHERE type='kursbevis' AND deltaker_id IN ({ph}) ORDER BY opprettet, id""", deltaker_ider):
        bevis[(r["deltaker_id"], r["kurs_id"])] = (r["opprettet"], r["id"])
    utsendt = {}                                   # (nøkkel, e-post) -> raden i utsending_logg for kursbeviset
    for nokler in _i_biter({f"kurs:{r['kurs_id']}" for r in rader if (r["deltaker_id"], r["kurs_id"]) in bevis}):
        for r in con.execute(f"SELECT nokkel, mottaker, status, sendt_ts FROM utsending_logg WHERE type='kursbevis' "
                             f"AND nokkel IN ({_plasser(nokler)})", nokler):
            utsendt[(r["nokkel"], str(r["mottaker"]).lower())] = r
    ut: dict[int, list[dict]] = {}
    for r in rader:
        p = dict(r)
        d = dager.get(p["kurs_id"])
        antall, forste, siste = (d["antall"], d["forste"], d["siste"]) if d else (0, None, None)
        m = mott.get(p["paamelding_id"], 0)
        status = db.paameldingsstatus(p)
        p["status_navn"] = db.PAAMELDINGSSTATUSER[status]
        pm = {"id": p["paamelding_id"], "kurs_id": p["kurs_id"], "ekstradeltaker_ts": p["ekstradeltaker_ts"]}
        if db.er_ekstradeltaker(pm) and db.ekstradeltaker_samlinger(con, pm["id"]):
            # Ekstradeltaker på noen samlinger: kursdatoer, oppmøte og kursbevis regnes på DERES dager, og statusen sier hvilke
            egne = db.paameldingens_kursdager(con, pm)
            egne_ider = {x["id"] for x in egne}
            antall = len(egne)
            forste, siste = (egne[0]["dato"], egne[-1]["dato"]) if egne else (None, None)
            m = sum(1 for r2 in con.execute("SELECT kursdag_id FROM oppmote WHERE paamelding_id=?", (pm["id"],))
                    if r2["kursdag_id"] in egne_ider)
            tekst = db.samlingsutvalg_tekst(con, pm, og=True)
            p["status_navn"] += " – " + (tekst[0].lower() + tekst[1:] if tekst.startswith("Samling ") else tekst)
        p["datoer"] = _dato(forste) if forste == siste else f"{_dato(forste)} – {_dato(siste)}"
        p["oppmote"] = oppmote_tekst(m, antall, bool(forste) and forste <= idag.isoformat(), p["status"] == "bekreftet")
        utstedt, dok_id = bevis.get((p["deltaker_id"], p["kurs_id"]), (None, None))
        p["kursbevis"], p["kursbevis_farge"] = kursbevis_forklaring(
            utstedt=utstedt, kurs_status=p["kurs_status"], paamelding_status=p["status"],
            dager=antall, mott=m, siste_dato=siste, idag=idag)
        p["kursbevis_dok"] = dok_id
        p["kursbevis_epost"] = epost_om_kursbevis(
            utsendt.get((f"kurs:{p['kurs_id']}", str(deltakere[p["deltaker_id"]]).lower()))) if dok_id else ""
        p["treff"] = p["paamelding_id"] in treff_pid.get(p["deltaker_id"], ())
        p["_sortering"] = (forste or str(p["opprettet"] or "")[:10], p["paamelding_id"])
        ut.setdefault(p["deltaker_id"], []).append(p)
    for lista in ut.values():
        lista.sort(key=lambda p: p["_sortering"], reverse=True)
    return ut


# ============================ søket ============================

@dataclass
class Resultat:
    sok: Sok
    personer: list            # de beste treffene (maks `grense`), med påmeldinger
    totalt: int = 0           # antall personer som traff
    kurs: list = field(default_factory=list)   # kurs søket peker rett på (kursnummer/kurskode): snarvei til deltakerlisten

    @property
    def flere(self) -> bool:
        return self.totalt > len(self.personer)


def sok(con, tekst, grense: int = GRENSE_SIDE, idag: date | None = None) -> Resultat:
    """Søker etter personer. Rangering: nøyaktig e-post/telefon/HPR/påmeldingsnummer først, så navn som starter med
    søket, så øvrige treff; innen hver gruppe alfabetisk (norsk). Bare de `grense` beste får påmeldinger og kursbevis."""
    s = tolk(tekst)
    if not s.gyldig:
        return Resultat(s, [], 0, [])
    idag = idag or date.today()
    via, kurs_for_paamelding, eksakte_kurs = _slaa_opp_paameldinger(con, s.termer)
    funnet = []
    for p in _kandidater(con):
        treff = _treffer_person(p, s.termer, via)
        if treff is None:
            continue
        grunner, eksakt, pnr_ider, kurs_ider = treff
        rang = 0 if eksakt else 1 if _navn_starter_med(p["navn"], s.termer) else 2
        funnet.append((rang, norsk_sortering(p["navn"]), p["id"], p, grunner, pnr_ider, kurs_ider))
    funnet.sort(key=lambda x: x[:3])
    beste = funnet[:grense]
    paameldinger = _paameldinger(con, {x[3]["id"]: x[3]["epost"] for x in beste}, idag,
                                 {x[3]["id"]: x[5] | x[6] for x in beste})
    ord_ = [t.tekst for t in s.termer if not t.kun_pnr]
    personer = []
    for _rang, _sortnokkel, pid, p, grunner, pnr_ider, kurs_ider in beste:
        liste = _grunn_liste(grunner, pnr_ider, kurs_ider, kurs_for_paamelding)
        personer.append({
            "id": pid, "navn": p["navn"], "epost": p["epost"], "telefon": p["telefon"] or "",
            "arbeidssted": p["arbeidssted"] or "", "hpr_nr": p["hpr_nr"] or "", "vis_hpr": "hpr" in grunner,
            "grunner": liste, "grunn": grunn_tekst(liste),
            "navn_deler": _markeringer(p["navn"], ord_), "epost_deler": _markeringer(p["epost"], ord_),
            "paameldinger": paameldinger.get(pid, [])})
    return Resultat(s, personer, len(funnet), eksakte_kurs if len(eksakte_kurs) <= MAKS_KURSTREFF else [])


def liste_data(res: Resultat) -> dict:
    """Det rullegardinen trenger (JSON): kort per person, uten påmeldingsdetaljer utover antall og siste kurs."""
    return {
        "totalt": res.totalt, "for_kort": res.sok.for_kort,
        "treff": [{"id": p["id"], "navn_deler": p["navn_deler"], "epost_deler": p["epost_deler"], "grunn": p["grunn"],
                   "antall": len(p["paameldinger"]),
                   "siste_kurs": p["paameldinger"][0]["kode"] if p["paameldinger"] else ""} for p in res.personer]}
