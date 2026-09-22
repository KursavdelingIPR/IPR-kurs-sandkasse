"""Redigerbare maltekster for e-post (fase 12B1: fundament).

Dette er den ENESTE sentrale definisjonen av hvilke e-postmaler som er redigerbare, hvilke felt de har, standardtekst,
tillatte {koder} per felt, felttype, maks lengde og om feltet kan vaere tomt.

Prinsipper:
  * STANDARDTEKSTENE ligger her i koden (git-versjonert). Databasen (mal_tekst) inneholder KUN overstyringer.
  * Admintekst er REN TEKST med kontrollerte {koder} fra en whitelist. Ingen HTML, ingen Jinja, ingen str.format/eval.
  * Systemkritisk innhold (kursdager, sted, faktura, lenker, ...) ligger i lasede systemblokker i malfilene, ikke her.
  * Ingen stille fallback: mangler overstyringen -> standardtekst. Finnes en UGYLDIG overstyring, en rad for et ukjent felt,
    eller feiler DB-lesingen -> MalFeil. Admin skal aldri tro én tekst sendes mens systemet stille sender en annen.
  * Dette modulen importerer IKKE integrasjoner/epost.py (avhengigheten gaar én vei). Jinja-rendringen av selve e-postmalen
    eier epost.py; her leveres kun trygge felttekster (senere, 12B2: epost.render(mal, maltekst=..., **data)).
  * MalFeil bærer kun mal, felt og en grunnkode - aldri teksten, kodeverdier, navn eller e-post.
"""
import re
import sqlite3
from dataclasses import dataclass

from markupsafe import Markup, escape

from . import config, db

# ============================ feil ============================

# Grunnkoder (PII-frie, trygge aa logge)
UKJENT_MAL = "ukjent_mal"
UKJENT_FELT = "ukjent_felt"
TOM = "tom"
FOR_LANG = "for_lang"
KONTROLLTEGN = "kontrolltegn"
UFERDIG_KLAMME = "uferdig_klamme"
ENSLIG_KLAMME = "enslig_klamme"
UGYLDIG_SYNTAKS = "ugyldig_syntaks"
UKJENT_KODE = "ukjent_kode"
MANGLER_VERDI = "mangler_verdi"
FEIL_FELTTYPE = "feil_felttype"
UGYLDIG_TYPE = "ugyldig_type"
DB_LESEFEIL = "db_lesefeil"
IKKE_AKTIVERT = "ikke_aktivert"


class MalFeil(Exception):
    """Ugyldig maltekst/mal-oppslag. str(e) og detaljer() inneholder KUN mal, felt og grunnkode.

    `forklaring` er en menneskelig norsk tekst til en admin-bruker (kan nevne den feilaktige koden) - den skal ALDRI
    logges eller lagres, og finnes ikke i str(e)."""

    def __init__(self, grunn: str, mal: str | None = None, felt: str | None = None, forklaring: str = ""):
        super().__init__(grunn, mal, felt)
        self.grunn, self.mal, self.felt, self.forklaring = grunn, mal, felt, forklaring

    def __str__(self) -> str:
        return f"{self.grunn} ({self.mal or '?'}.{self.felt or '?'})"

    def detaljer(self) -> dict:
        """PII-fri dict til hendelseslogg."""
        return {"mal": self.mal, "felt": self.felt, "grunn": self.grunn}


# ============================ koder (placeholders) ============================

@dataclass(frozen=True)
class Kode:
    navn: str    # menneskevennlig navn (til senere admin-UI)
    stil: str    # "vanlig" | "fet" (HTML-stil paa verdien) | "system" (verdien bygges av systemet, ikke fra malens data)


# ALLE koder som finnes. Ingen koder gir sensitive data (allergier, tilrettelegging, e-post, telefon, adresse ...).
KODER: dict[str, Kode] = {
    "navn": Kode("Mottakerens navn", "vanlig"),
    "kursnavn": Kode("Kursnavn", "fet"),
    "startdato": Kode("Første kursdag", "vanlig"),
    "dato": Kode("Kursdagens dato", "vanlig"),
    "dagnummer": Kode("Kursdagens nummer", "vanlig"),
    "antall_dager": Kode("Antall kursdager", "vanlig"),
    "firmanavn": Kode("Firmanavn", "vanlig"),
    "antall_deltakere": Kode("Antall deltakere (f.eks. «3 deltakere»)", "vanlig"),
    "beskrivelse": Kode("Hva som skal leveres", "vanlig"),
    "beskrivelse_liten": Kode("Hva som skal leveres (små bokstaver)", "fet"),
    "frist": Kode("Frist", "fet"),
    "dager_igjen": Kode("Dager til frist", "vanlig"),
    "min_side": Kode("Lenke til Min side", "system"),
    "sporsmal_url": Kode("Adressen til «Spør oss»", "system"),
}
SYSTEMKODER = frozenset(k for k, v in KODER.items() if v.stil == "system")  # min_side, sporsmal_url: kun i tekstfelt


# ============================ register ============================

EMNE, TEKST = "emne", "tekst"
_MAKS_EMNE, _MAKS_TEKST = 200, 2000


@dataclass(frozen=True)
class Felt:
    navn: str                 # menneskevennlig navn
    type: str                 # "emne" (ren tekst, ett enkelt linje) | "tekst" (avsnitt i HTML-kroppen)
    standard: str             # standardtekst (fallback naar ingen overstyring finnes)
    kode: frozenset           # tillatte {koder} i dette feltet
    maks: int                 # maks lengde (tegn)
    tom_tillatt: bool         # kan feltet lagres som tom tekst? (IKKE det samme som "obligatorisk kode")


@dataclass(frozen=True)
class Mal:
    navn: str
    beskrivelse: str
    felt: dict


def _emne(navn, standard, koder) -> Felt:
    return Felt(navn, EMNE, standard, frozenset(koder), _MAKS_EMNE, False)


def _hoved(navn, standard, koder) -> Felt:
    """Selve hovedinnholdet i e-posten: kan ikke lagres tomt."""
    return Felt(navn, TEKST, standard, frozenset(koder) | {"min_side"}, _MAKS_TEKST, False)


def _tillegg(navn, standard, koder) -> Felt:
    """Valgfri tilleggstekst (avslutning): kan vaere tom."""
    return Felt(navn, TEKST, standard, frozenset(koder) | {"min_side"}, _MAKS_TEKST, True)


_D = "Hei {navn},\n\n"

MALER: dict[str, Mal] = {
    "bekreftelse": Mal(
        "Bekreftelse på plass", "Sendes til deltakeren når hen har fått plass på et kurs.",
        {"emne": _emne("Emne", "Bekreftelse: {kursnavn}", {"navn", "kursnavn", "startdato"}),
         "innledning": _hoved("Innledning", _D + "Takk for påmeldingen! Du har fått plass på {kursnavn}.",
                              {"navn", "kursnavn", "startdato"}),
         "avslutning": _tillegg("Avslutning", "Du kan når som helst logge inn på {min_side} med e-postadressen din.",
                                {"navn", "kursnavn", "startdato"})}),
    "venteliste": Mal(
        "Ventelistebeskjed", "Sendes til deltakeren når kurset er fullt og hen settes på venteliste.",
        {"emne": _emne("Emne", "Venteliste: {kursnavn}", {"navn", "kursnavn"}),
         "tekst": _hoved("Tekst", _D + "{kursnavn} er dessverre fullt, men du står nå på venteliste. Blir det en ledig "
                                       "plass, får du automatisk plassen og en bekreftelse på e-post.",
                         {"navn", "kursnavn"})}),
    "ukefor": Mal(
        "Praktisk informasjon (uken før)", "Sendes til bekreftede deltakere en uke før kursstart.",
        {"emne": _emne("Emne", "Velkommen til {kursnavn} – praktisk informasjon", {"navn", "kursnavn", "startdato"}),
         "innledning": _hoved("Innledning", _D + "Nå er det snart tid for {kursnavn}, som starter {startdato}.",
                              {"navn", "kursnavn", "startdato"}),
         "avslutning": _tillegg("Avslutning", "", {"navn", "kursnavn", "startdato"})}),
    "dagfor": Mal(
        "Påminnelse dagen før kursdag", "Sendes til bekreftede deltakere dagen før hver kursdag "
                                       "(egen tekst for første dag, dager midt i kurset og siste dag).",
        {"emne_forste": _emne("Emne – første kursdag", "I morgen starter {kursnavn}",
                              {"navn", "kursnavn", "dato", "dagnummer", "antall_dager"}),
         "emne_midt": _emne("Emne – dag midt i kurset", "I morgen, dag {dagnummer}: {kursnavn}",
                            {"navn", "kursnavn", "dato", "dagnummer", "antall_dager"}),
         "emne_siste": _emne("Emne – siste kursdag", "Siste kursdag i morgen: {kursnavn}",
                             {"navn", "kursnavn", "dato", "dagnummer", "antall_dager"}),
         "innledning_forste": _hoved("Innledning – første kursdag", _D + "I morgen starter {kursnavn}!",
                                     {"navn", "kursnavn", "dato", "dagnummer", "antall_dager"}),
         "innledning_midt": _hoved("Innledning – dag midt i kurset",
                                   _D + "I morgen er dag {dagnummer} av {antall_dager} på {kursnavn}.",
                                   {"navn", "kursnavn", "dato", "dagnummer", "antall_dager"}),
         "innledning_siste": _hoved("Innledning – siste kursdag", _D + "I morgen er siste kursdag på {kursnavn}.",
                                    {"navn", "kursnavn", "dato", "dagnummer", "antall_dager"}),
         "avslutning": _tillegg("Avslutning", "", {"navn", "kursnavn", "dato", "dagnummer", "antall_dager"})}),
    "avlysning": Mal(
        "Avlysning", "Sendes til deltakere og ventelisten når et kurs avlyses.",
        {"emne": _emne("Emne", "Avlyst: {kursnavn}", {"navn", "kursnavn"}),
         "tekst": Felt("Tekst", TEKST,
                       _D + "Vi må dessverre informere om at {kursnavn} er avlyst.\n\n"
                       "Har du allerede mottatt faktura, tar kursadministrasjonen kontakt med deg om det videre. "
                       "Har du spørsmål, kan du svare på denne e-posten eller bruke «Spør oss» på {sporsmal_url}.\n\n"
                       "Vi beklager ulempen dette medfører.",
                       frozenset({"navn", "kursnavn", "min_side", "sporsmal_url"}), _MAKS_TEKST, False)}),
    "kursbevis_klar": Mal(
        "Kursbevis klart", "Sendes til deltakeren når kursbeviset er lagt på Min side.",
        {"emne": _emne("Emne", "Kursbevis: {kursnavn}", {"navn", "kursnavn"}),
         "tekst": _hoved("Tekst", _D + "Takk for deltakelsen på {kursnavn}. Kursbeviset ditt ligger nå på {min_side}.",
                         {"navn", "kursnavn"})}),
    "firmapaamelding_kvittering": Mal(
        "Kvittering bedriftspåmelding", "Sendes til kontaktpersonen når en bedrift har meldt på flere deltakere.",
        {"emne": _emne("Emne", "Bedriftspåmelding til {kursnavn} – kvittering",
                       {"navn", "kursnavn", "firmanavn", "antall_deltakere"}),
         "innledning": _hoved("Innledning", _D + "Takk for påmeldingen av {antall_deltakere} fra {firmanavn} til {kursnavn}.",
                              {"navn", "kursnavn", "firmanavn", "antall_deltakere"}),
         "avslutning": _tillegg("Avslutning", "", {"navn", "kursnavn", "firmanavn", "antall_deltakere"})}),
    "purring": Mal(
        "Purring på materiell", "Sendes til kursholder/ansvarlig som ikke har levert materiell innen fristen nærmer seg.",
        {"emne_frist_om_dager": _emne("Emne – frist om noen dager",
                                      "Påminnelse: {beskrivelse} til {kursnavn} – frist om {dager_igjen} dager",
                                      {"navn", "kursnavn", "beskrivelse", "beskrivelse_liten", "frist", "dager_igjen"}),
         "emne_frist_i_dag": _emne("Emne – frist i dag", "Frist i dag: {beskrivelse} til {kursnavn}",
                                   {"navn", "kursnavn", "beskrivelse", "beskrivelse_liten", "frist", "dager_igjen"}),
         "innledning": _hoved("Innledning", _D + "Vi minner om at {beskrivelse_liten} til {kursnavn} skal leveres innen {frist}.",
                              {"navn", "kursnavn", "beskrivelse", "beskrivelse_liten", "frist", "dager_igjen"}),
         "avslutning": _tillegg("Avslutning", "", {"navn", "kursnavn", "beskrivelse", "beskrivelse_liten", "frist",
                                                    "dager_igjen"})}),
}
# avlysning tillater i tillegg {sporsmal_url}; ingen av de andre malene gjor det.


def _slaa_opp(mal: str, felt: str) -> tuple[Mal, Felt]:
    if not isinstance(mal, str) or mal not in MALER:
        raise MalFeil(UKJENT_MAL, None, None, "Denne malen kan ikke redigeres.")
    m = MALER[mal]
    if not isinstance(felt, str) or felt not in m.felt:
        raise MalFeil(UKJENT_FELT, mal, None, "Dette feltet finnes ikke i denne malen.")
    return m, m.felt[felt]


def standard_tekst(mal: str, felt: str) -> str:
    return _slaa_opp(mal, felt)[1].standard


# ============================ tegnregler ============================

# Emne: ETT linjeskiftfritt felt - ALLE kontrolltegn (inkl. tab og linjeskift) avvises.
_KONTROLL_EMNE = re.compile(r"[\x00-\x1f\x7f\x85  ]")
# En hel REKKE kontrolltegn (f.eks. CRLF) i en kodeverdi blir ETT mellomrom (samme regel som e-postemnet i 12A).
_KONTROLL_REKKE = re.compile(_KONTROLL_EMNE.pattern + "+")
# Tekst (kropp): CRLF/CR normaliseres til LF og TAB til mellomrom; ALLE andre kontrolltegn avvises.
_KONTROLL_KROPP = re.compile(r"[\x00-\x09\x0b-\x1f\x7f\x85  ]")


def _normaliser_kropp(tekst, mal=None, felt=None) -> str:
    if not isinstance(tekst, str):
        raise MalFeil(UGYLDIG_TYPE, mal, felt, "Teksten må være tekst.")
    t = tekst.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    if _KONTROLL_KROPP.search(t):
        raise MalFeil(KONTROLLTEGN, mal, felt, "Teksten inneholder ugyldige tegn.")
    return t


def _rens_verdi(verdi) -> str:
    """Kodeverdier kommer fra systemets data (kursnavn, deltakernavn ...) og kan ikke avvises - kontrolltegn og
    linjeskift erstattes med mellomrom slik at ingen verdi kan lage avsnitt, linjeskift eller ekstra e-posthoder."""
    return _KONTROLL_REKKE.sub(" ", str(verdi))


# ============================ parser (whitelist) ============================

_KODETEGN = frozenset("abcdefghijklmnopqrstuvwxyz_")


def _parse(tekst: str, tillatt: frozenset, mal=None, felt=None) -> list:
    """Deler teksten i [("tekst", str) | ("kode", navn)]. Eneste syntaks er {kode} der `kode` er eksakt en av de
    tillatte (kun a-z og _). Alt annet med klammer avvises - ingen escaping, ingen smarthet:
    {navn.__class__}, {navn[0]}, {navn:20}, {navn!r}, { navn }, {}, {{ navn }}, {{navn}}, {% .. %}, enslig { eller }."""
    deler, buf, i, n = [], [], 0, len(tekst)
    while i < n:
        c = tekst[i]
        if c == "{":
            j = tekst.find("}", i + 1)
            if j == -1:
                raise MalFeil(UFERDIG_KLAMME, mal, felt, "Det finnes en «{» uten avsluttende «}».")
            kode = tekst[i + 1:j]
            if not kode or not set(kode) <= _KODETEGN:
                raise MalFeil(UGYLDIG_SYNTAKS, mal, felt,
                              f"«{{{kode}}}» er ikke en gyldig kode. Koder skrives slik: {{navn}} (kun små bokstaver).")
            if kode not in tillatt:
                raise MalFeil(UKJENT_KODE, mal, felt,
                              f"Koden {{{kode}}} kan ikke brukes her. Gyldige koder: "
                              + ", ".join("{" + k + "}" for k in sorted(tillatt)))
            if buf:
                deler.append(("tekst", "".join(buf)))
                buf = []
            deler.append(("kode", kode))
            i = j + 1
        elif c == "}":
            raise MalFeil(ENSLIG_KLAMME, mal, felt, "Det finnes en «}» uten «{».")
        else:
            buf.append(c)
            i += 1
    if buf:
        deler.append(("tekst", "".join(buf)))
    return deler


# ============================ validering ============================

def valider(mal: str, felt: str, tekst) -> str:
    """Enkelt inngangspunkt for ALL admintekst: lagring OG rendring. Returnerer den rensede teksten, eller kaster MalFeil.
    Rekkefolge: kjent mal/felt, tegn, tom/for lang, kodesyntaks."""
    _, f = _slaa_opp(mal, felt)
    if f.type == EMNE:
        if not isinstance(tekst, str):
            raise MalFeil(UGYLDIG_TYPE, mal, felt, "Teksten må være tekst.")
        if _KONTROLL_EMNE.search(tekst):
            raise MalFeil(KONTROLLTEGN, mal, felt, "Emnet kan ikke inneholde linjeskift eller andre kontrolltegn.")
        ren = tekst.strip()
    else:
        ren = _normaliser_kropp(tekst, mal, felt).strip()
    if not ren and not f.tom_tillatt:
        raise MalFeil(TOM, mal, felt, "Dette feltet kan ikke være tomt.")
    if len(ren) > f.maks:
        raise MalFeil(FOR_LANG, mal, felt, f"Teksten er for lang (maks {f.maks} tegn, er nå {len(ren)}).")
    _parse(ren, f.kode, mal, felt)
    return ren


# ============================ rendring ============================

def _avsnitt(tekst: str) -> list:
    """Blank linje (kun mellomrom) skiller avsnitt; enkle linjeskift beholdes innen et avsnitt."""
    avsnitt, naa = [], []
    for linje in tekst.split("\n"):
        if not linje.strip():
            if naa:
                avsnitt.append(naa)
                naa = []
        else:
            naa.append(linje.strip())
    if naa:
        avsnitt.append(naa)
    return avsnitt


def _avsnitt_til_html(tekst: str, linje_til_html) -> Markup:
    """Felles bygger: <p>linje<br>\\nlinje</p> per avsnitt. Bare systemet setter inn <p> og <br>."""
    deler = [Markup("<p>") + Markup("<br>\n").join(linje_til_html(linje) for linje in linjer) + Markup("</p>")
             for linjer in _avsnitt(tekst)]
    return Markup("\n").join(deler)


def ren_tekst_til_html(tekst: str) -> Markup:
    """Grunnprimitiv (kjenner INGEN koder): ren tekst -> trygg HTML. All tekst escapes, CRLF/CR -> LF, uonskede
    kontrolltegn avvises (MalFeil), blank linje -> avsnitt, enkelt linjeskift -> <br>. {} evalueres aldri og HTML tolkes aldri."""
    return _avsnitt_til_html(_normaliser_kropp(tekst), escape)


def _kodeverdi_html(kode: str, verdier: dict, mal: str, felt: str) -> Markup:
    if kode == "min_side":
        return Markup('<a href="{}/min-side">Min side</a>').format(config.BASE_URL)   # format escaper verdien
    if kode == "sporsmal_url":
        return escape(f"{config.BASE_URL}/sporsmal")
    if kode not in verdier:
        raise MalFeil(MANGLER_VERDI, mal, felt, f"Verdien for {{{kode}}} mangler.")
    ren = escape(_rens_verdi(verdier[kode]))
    return Markup("<strong>{}</strong>").format(ren) if KODER[kode].stil == "fet" else ren


def felttekst_til_html(mal: str, felt: str, tekst: str, verdier: dict) -> Markup:
    """Admin-/standardtekst for et TEKSTFELT -> trygg HTML. Bygger paa samme avsnittslogikk som ren_tekst_til_html, men
    tolker {koder} fra feltets whitelist. Admintekst og alle verdier escapes; kun <p>, <br>, <strong> og den systembygde
    Min side-lenken settes inn av systemet. `verdier`: kode -> ren tekstverdi (fra malens data)."""
    _, f = _slaa_opp(mal, felt)
    if f.type != TEKST:
        raise MalFeil(FEIL_FELTTYPE, mal, felt, "Dette er ikke et tekstfelt.")
    ren = valider(mal, felt, tekst)

    def linje(l: str) -> Markup:
        ut = Markup("")
        for slag, verdi in _parse(l, f.kode, mal, felt):
            ut += escape(verdi) if slag == "tekst" else _kodeverdi_html(verdi, verdier, mal, felt)
        return ut
    return _avsnitt_til_html(ren, linje)


def felttekst_til_emne(mal: str, felt: str, tekst: str, verdier: dict) -> str:
    """Admin-/standardtekst for et EMNEFELT -> ren tekst (ingen HTML-escaping). Verdier renses for kontrolltegn/linjeskift,
    saa emnet aldri kan bli flere linjer eller ekstra e-posthoder. Systemkoder (lenker) er ikke tillatt i emner."""
    _, f = _slaa_opp(mal, felt)
    if f.type != EMNE:
        raise MalFeil(FEIL_FELTTYPE, mal, felt, "Dette er ikke et emnefelt.")
    ren = valider(mal, felt, tekst)
    ut = []
    for slag, verdi in _parse(ren, f.kode, mal, felt):
        if slag == "tekst":
            ut.append(verdi)
        elif verdi in verdier:
            ut.append(_rens_verdi(verdier[verdi]))
        else:
            raise MalFeil(MANGLER_VERDI, mal, felt, f"Verdien for {{{verdi}}} mangler.")
    emne = "".join(ut).strip()
    if not emne:
        raise MalFeil(TOM, mal, felt, "Emnet ble tomt.")
    return emne


# ============================ overstyringer: oppslag og lagring ============================

def hent_overstyringer(con, mal: str) -> dict:
    """Alle overstyringer for en KJENT mal som {felt: tekst}. Ingen rader -> tom dict (bruk standardtekst).
    Hver rad valideres: ukjent felt, ugyldig tekst -> MalFeil. DB-feil -> MalFeil (db_lesefeil). ALDRI stille fallback."""
    if not isinstance(mal, str) or mal not in MALER:
        raise MalFeil(UKJENT_MAL, None, None, "Denne malen kan ikke redigeres.")
    try:
        rader = db.hent_overstyringer_for_mal(con, mal)
    except sqlite3.Error as e:
        raise MalFeil(DB_LESEFEIL, mal, None, "Kunne ikke lese maltekstene fra databasen.") from e
    ut = {}
    for felt, tekst in rader.items():
        ut[felt] = valider(mal, felt, tekst)   # ukjent felt / ugyldig tekst -> MalFeil
    return ut


def effektive_tekster(con, mal: str) -> dict:
    """{felt: tekst} for ALLE felt i malen: overstyring hvis den finnes og er gyldig, ellers standardtekst."""
    over = hent_overstyringer(con, mal)
    return {felt: over.get(felt, f.standard) for felt, f in MALER[mal].felt.items()}


def effektiv_tekst(con, mal: str, felt: str) -> str:
    _slaa_opp(mal, felt)
    return effektive_tekster(con, mal)[felt]


def er_tilpasset(con, mal: str, felt: str) -> bool:
    _slaa_opp(mal, felt)
    return felt in hent_overstyringer(con, mal)


def lagre_maltekst(con, mal: str, felt: str, tekst: str, aktor: str = "system") -> str:
    """Validerer FOER skriving, lagrer overstyringen og logger mal_endret (mal, felt, aktor - aldri teksten).
    Committer ikke (som resten av db-laget). Returnerer den lagrede, rensede teksten."""
    ren = valider(mal, felt, tekst)
    if mal not in AKTIVE_MALER:   # 12B2A: ellers ville overstyringen bli STILLE ignorert av utsendingen
        raise MalFeil(IKKE_AKTIVERT, mal, felt, "Denne malen er ikke koblet til utsending ennå, så teksten kan ikke lagres.")
    db.sett_maltekst(con, mal, felt, ren, aktor=aktor)
    return ren


def tilbakestill_maltekst(con, mal: str, felt: str, aktor: str = "system") -> bool:
    """Fjerner overstyringen (-> standardtekst). Logger mal_tilbakestilt KUN hvis en rad faktisk fantes. Committer ikke."""
    _slaa_opp(mal, felt)
    return db.slett_maltekst(con, mal, felt, aktor=aktor)


# ============================ kobling til utsending (12B2A) ============================

# MIDLERTIDIG (12B2A-12B2C): KUN `venteliste`, `avlysning`, `bekreftelse`, `ukefor`, `dagfor`, `kursbevis_klar` og
# `firmapaamelding_kvittering` er koblet til den faktiske utsendingen. `purring` rendres fortsatt med den gamle,
# hardkodede Jinja-teksten. For at en overstyring aldri skal bli STILLE ignorert, NEKTER lagre_maltekst() aa lagre
# for maler som ikke er i denne listen. Generaliseres/fjernes naar alle 8 er migrert
# (verdibyggerne under blir da en del av Mal-registeret).
AKTIVE_MALER = frozenset({"venteliste", "avlysning", "bekreftelse", "ukefor", "dagfor", "kursbevis_klar",
                          "firmapaamelding_kvittering"})


def _venteliste_verdier(data) -> dict:
    return {"navn": data["p"]["navn"], "kursnavn": data["kurs"]["navn"]}


def _avlysning_verdier(data) -> dict:
    """KUN det registeret tillater: mottakerens navn og kursnavn. Ingen e-post, telefon, adresse, faktura eller sensitivt.
    (Systemlenkene {min_side} og {sporsmal_url} bygges av maltekster fra BASE_URL, ikke fra data.)"""
    return {"navn": data["d"]["navn"], "kursnavn": data["kurs"]["navn"]}


def _bekreftelse_verdier(data) -> dict:
    """KUN det registeret tillater: mottakerens navn, kursnavn og foerste kursdag (ISO). Ingen e-post, telefon, adresse eller
    fakturadata. Fakturainformasjonen er en LAAST systemblokk i malfilen, bygget fra `faktura_plan` (samme FakturaPlan som
    fakturamotoren) - aldri en kode og aldri redigerbar. Mangler planen -> MalFeil (fail closed: aldri en bekreftelse uten
    sann fakturainformasjon). Uten kursdager utelates {startdato} (bruker admin den, blir det MalFeil - aldri en oppdiktet dato)."""
    data["faktura_plan"]
    verdier = {"navn": data["p"]["navn"], "kursnavn": data["kurs"]["navn"]}
    if data["dager"]:
        verdier["startdato"] = data["dager"][0]["dato"]
    return verdier


def _ukefor_verdier(data) -> dict:
    """KUN det registeret tillater: navn, kursnavn og foerste kursdag (ISO). Kursdager/klokkeslett, sted/QR-innsjekk, Zoom-
    informasjon og kursnotat er LAASTE systemblokker i malfilen - aldri koder, aldri redigerbare."""
    return {"navn": data["d"]["navn"], "kursnavn": data["kurs"]["navn"], "startdato": data["dager"][0]["dato"]}


def _dagfor_verdier(data) -> dict:
    """KUN det registeret tillater. Tid, sted/QR og Zoom-lenke/ID/passord er LAASTE systemblokker i malfilen (Zoom-hemmeligheter
    er aldri koder). Alle varianter (forste/midt/siste) faar verdier, saa en ugyldig variant feiler lukket uansett dag."""
    return {"navn": data["d"]["navn"], "kursnavn": data["kurs"]["navn"], "dato": data["dag"]["dato"],
            "dagnummer": data["nr"], "antall_dager": data["antall"]}


def _kursbevis_klar_verdier(data) -> dict:
    """KUN det registeret tillater: mottakerens navn og kursnavn. Ingen dokument-ID, filsti eller annen intern referanse -
    Min side-lenken er systemkoden {min_side} (bygget av maltekster fra BASE_URL), aldri en fri URL eller sti fra admin."""
    return {"navn": data["navn"], "kursnavn": data["kurs"]["navn"]}


def _firmapaamelding_kvittering_verdier(data) -> dict:
    """KUN det registeret tillater: kontaktpersonens navn, kursnavn, firmanavn og antall deltakere (som tekst,
    f.eks. "3 deltakere"). ALDRI e-post, telefon, org.nr, faktura-/betalingsdata. De faktiske utfallstallene
    (antall bekreftet/venteliste/feilet) og kvitteringslenken er LAASTE systemblokker i malfilen, bygget fra det
    virkelige registreringsresultatet ETTER at deltakerne er meldt paa - de er ALDRI koder her, og kan derfor
    ikke gjoeres om av admin. `antall_totalt` (antall deltakere i INNSENDINGEN) er derimot kjent FOER noen
    registrering skjer, saa {antall_deltakere} kan trygt rendres i preflight, foer varig sideeffekt."""
    antall = data["antall_totalt"]
    return {"navn": data["kontakt"]["navn"], "kursnavn": data["kurs"]["navn"], "firmanavn": data["kontakt"]["firmanavn"],
            "antall_deltakere": f"{antall} deltaker{'e' if antall != 1 else ''}"}


_VERDIER = {"venteliste": _venteliste_verdier, "avlysning": _avlysning_verdier, "bekreftelse": _bekreftelse_verdier,
            "ukefor": _ukefor_verdier, "dagfor": _dagfor_verdier, "kursbevis_klar": _kursbevis_klar_verdier,
            "firmapaamelding_kvittering": _firmapaamelding_kvittering_verdier,
            }    # mal -> funksjon(malens data) -> {kode: rå tekstverdi}


def _bygg(mal: str, tekster: dict, data) -> dict:
    """Ferdig rendrede felttekster for en aktiv mal: {felt: str | Markup}. Emnefelt er ren tekst, tekstfelt er trygg HTML.
    Ingen DB, ingen Jinja. Mangler malens data -> MalFeil (aldri stille tom tekst)."""
    try:
        verdier = _VERDIER[mal](data)
    except (KeyError, IndexError, TypeError) as e:
        raise MalFeil(MANGLER_VERDI, mal, None, "Data til malen mangler.") from e
    ut = {}
    for felt, f in MALER[mal].felt.items():
        render = felttekst_til_emne if f.type == EMNE else felttekst_til_html
        ut[felt] = render(mal, felt, tekster[felt], verdier)
    return ut


def standard_maltekst(mal: str, data) -> dict:
    """Standardtekstene for en aktiv mal, ferdig rendret. RENT - ingen DB (brukes av epost.render uten connection)."""
    if mal not in AKTIVE_MALER:
        raise MalFeil(IKKE_AKTIVERT, mal, None, "Denne malen er ikke koblet til maltekstsystemet ennå.")
    return _bygg(mal, {felt: f.standard for felt, f in MALER[mal].felt.items()}, data)


def maltekst_for_utsending(con, mal: str, data):
    """Effektiv maltekst (overstyring eller standard) for en faktisk utsending, ferdig rendret - eller None for maler som
    ikke (ennå) er koblet (-> gammel rendering, uendret, ingen DB-lesing). Kalles FOER claim. Ugyldig/korrupt overstyring
    og DB-feil gir MalFeil - aldri stille fallback til standard. Leser kun; committer/ruller aldri tilbake."""
    if mal not in AKTIVE_MALER:
        return None
    return _bygg(mal, effektive_tekster(con, mal), data)
