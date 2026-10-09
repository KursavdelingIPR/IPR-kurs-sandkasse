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
    "fornavn": Kode("Mottakerens fornavn", "vanlig"),
    "navn": Kode("Mottakerens fulle navn", "vanlig"),
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
    "dager_igjen_tekst": Kode("Dager til frist, med riktig entall/flertall (f.eks. «1 dag» / «2 dager»)", "vanlig"),
    "min_side": Kode("Lenke til Min side", "system"),
    "sporsmal_url": Kode("Adressen til «Spør oss»", "system"),
}
SYSTEMKODER = frozenset(k for k, v in KODER.items() if v.stil == "system")  # min_side, sporsmal_url: kun i tekstfelt


def kodeliste(koder) -> list[str]:
    """Kodene i den rekkefoelgen de vises som knapper: {fornavn} og {navn} foerst, deretter alfabetisk."""
    return sorted(koder, key=lambda k: ({"fornavn": 0, "navn": 1}.get(k, 2), k))


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


# Standardhilsen i malene til deltakere (og kontaktpersoner): fornavnet. Den er vanlig, redigerbar tekst - admin bestemmer
# selv om og hvor {fornavn} skal staa.
_D = "Hei {fornavn},\n\n"
_PAAMELDTE = db.PAAMELDINGSSTATUSER_FLERTALL["paameldt"].lower()      # de som har plass (beskrivelsene av malene)
_EKSTRA = db.PAAMELDINGSSTATUSER_FLERTALL["ekstradeltaker"]           # er på kurset, men bare på samlingene de er satt opp på
_P = {"fornavn", "navn"}            # mottakerens navnekoder i alle maler til deltakere/kontaktpersoner

MALER: dict[str, Mal] = {
    "bekreftelse": Mal(
        "Bekreftelse på plass", "Sendes til deltakeren når hen har fått plass på et kurs.",
        {"emne": _emne("Emne", "Bekreftelse: {kursnavn}", _P | {"kursnavn", "startdato"}),
         "innledning": _hoved("Innledning", _D + "Takk for påmeldingen! Du har fått plass på {kursnavn}.",
                              _P | {"kursnavn", "startdato"}),
         "avslutning": _tillegg("Avslutning", "Du kan når som helst logge inn på {min_side} med e-postadressen din.",
                                _P | {"kursnavn", "startdato"})}),
    "venteliste": Mal(
        "Ventelistebeskjed", "Sendes til deltakeren når kurset er fullt og hen settes på venteliste.",
        {"emne": _emne("Emne", "Venteliste: {kursnavn}", _P | {"kursnavn"}),
         "tekst": _hoved("Tekst", _D + "{kursnavn} er dessverre fullt, men du står nå på venteliste. Blir det en ledig "
                                       "plass, får du automatisk plassen og en bekreftelse på e-post.",
                         _P | {"kursnavn"})}),
    "ukefor": Mal(
        "Praktisk informasjon (uken før)", f"Sendes til {_PAAMELDTE} deltakere en uke før kursstart. {_EKSTRA} får den en uke før "
                                          "den første samlingen de er satt opp på.",
        {"emne": _emne("Emne", "Velkommen til {kursnavn} – praktisk informasjon", _P | {"kursnavn", "startdato"}),
         "innledning": _hoved("Innledning", _D + "Nå er det snart tid for {kursnavn}, som starter {startdato}.",
                              _P | {"kursnavn", "startdato"}),
         "avslutning": _tillegg("Avslutning", "", _P | {"kursnavn", "startdato"})}),
    "dagfor": Mal(
        "Påminnelse dagen før kursdag", f"Sendes til {_PAAMELDTE} deltakere dagen før hver kursdag "
                                       "(egen tekst for første dag, dager midt i kurset og siste dag). "
                                       f"{_EKSTRA} får den bare dagen før kursdagene i samlingene de er satt opp på.",
        {"emne_forste": _emne("Emne – første kursdag", "I morgen starter {kursnavn}",
                              _P | {"kursnavn", "dato", "dagnummer", "antall_dager"}),
         "emne_midt": _emne("Emne – dag midt i kurset", "I morgen, dag {dagnummer}: {kursnavn}",
                            _P | {"kursnavn", "dato", "dagnummer", "antall_dager"}),
         "emne_siste": _emne("Emne – siste kursdag", "Siste kursdag i morgen: {kursnavn}",
                             _P | {"kursnavn", "dato", "dagnummer", "antall_dager"}),
         "innledning_forste": _hoved("Innledning – første kursdag", _D + "I morgen starter {kursnavn}!",
                                     _P | {"kursnavn", "dato", "dagnummer", "antall_dager"}),
         "innledning_midt": _hoved("Innledning – dag midt i kurset",
                                   _D + "I morgen er dag {dagnummer} av {antall_dager} på {kursnavn}.",
                                   _P | {"kursnavn", "dato", "dagnummer", "antall_dager"}),
         "innledning_siste": _hoved("Innledning – siste kursdag", _D + "I morgen er siste kursdag på {kursnavn}.",
                                    _P | {"kursnavn", "dato", "dagnummer", "antall_dager"}),
         "avslutning": _tillegg("Avslutning", "", _P | {"kursnavn", "dato", "dagnummer", "antall_dager"})}),
    "avlysning": Mal(
        "Avlysning", "Sendes til deltakere og ventelisten når et kurs avlyses.",
        {"emne": _emne("Emne", "Avlyst: {kursnavn}", _P | {"kursnavn"}),
         "tekst": Felt("Tekst", TEKST,
                       _D + "Vi må dessverre informere om at {kursnavn} er avlyst.\n\n"
                       "Har du allerede mottatt faktura, tar kursadministrasjonen kontakt med deg om det videre. "
                       "Har du spørsmål, kan du svare på denne e-posten.\n\n"
                       "Vi beklager ulempen dette medfører.",
                       frozenset(_P | {"kursnavn", "min_side", "sporsmal_url"}), _MAKS_TEKST, False)}),
    "kursbevis_klar": Mal(
        "Kursbevis klart", "Sendes til deltakeren når kursbeviset er lagt under Mine kurs.",
        {"emne": _emne("Emne", "Kursbevis: {kursnavn}", _P | {"kursnavn"}),
         "tekst": _hoved("Tekst", _D + "Takk for deltakelsen på {kursnavn}. Kursbeviset ditt ligger nå på {min_side}.",
                         _P | {"kursnavn"})}),
    "evaluering": Mal(
        "Evaluering etter kurset", "Sendes dagen etter siste kursdag til alle med plass på kurset, med en personlig lenke til "
        "et kort, anonymt spørreskjema.",
        {"emne": _emne("Emne", "Hva syntes du om {kursnavn}?", _P | {"kursnavn"}),
         "tekst": _hoved("Tekst", _D + "Takk for at du deltok på {kursnavn}. Vi blir glade om du bruker et par minutter på å "
                                       "fortelle oss hva du syntes. Svarene er anonyme og hjelper oss å gjøre kursene bedre.",
                         _P | {"kursnavn"})}),
    "firmapaamelding_kvittering": Mal(
        "Kvittering bedriftspåmelding", "Sendes til kontaktpersonen når en bedrift har meldt på flere deltakere.",
        {"emne": _emne("Emne", "Bedriftspåmelding til {kursnavn} – kvittering",
                       _P | {"kursnavn", "firmanavn", "antall_deltakere"}),
         "innledning": _hoved("Innledning", _D + "Takk for påmeldingen av {antall_deltakere} fra {firmanavn} til {kursnavn}.",
                              _P | {"kursnavn", "firmanavn", "antall_deltakere"}),
         "avslutning": _tillegg("Avslutning", "", _P | {"kursnavn", "firmanavn", "antall_deltakere"})}),
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
        # Lenken til DENNE mottakerens Min side (bygget av systemet, aldri fra admintekst) når e-posten gjelder et kurs med åpen Min side;
        # ellers oversikten «Mine kurs» (krever innlogging med e-post). format escaper verdien.
        personlig = verdier.get("min_side_url")
        if isinstance(personlig, str) and personlig.startswith(f"{config.BASE_URL}/min/"):
            return Markup('<a href="{}">Min side</a>').format(personlig)
        return Markup('<a href="{}/min-side">Mine kurs</a>').format(config.BASE_URL)
    if kode == "sporsmal_url":
        # «Spør oss» er av som standard (siden gir 404): en lagret tekst med koden peker da på innloggingen for deltakere i stedet for en
        # død adresse (startsiden / er innloggingen til Admin og er ikke noe sted å sende en deltaker)
        return escape(f"{config.BASE_URL}/sporsmal" if config.ASSISTENT_AKTIV else f"{config.BASE_URL}/logg-inn")
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


# ============================ egenskrevet e-post til deltakere (admin_melding) ============================

# Koder admin kan bruke i en egenskrevet e-post til én eller flere deltakere. Det legges ikke til noen automatisk hilsen -
# admin bestemmer selv om og hvor {fornavn} skal staa. Samme strenge tolker (whitelist) og escaping som de redigerbare malene.
MANUELLE_KODER = frozenset({"fornavn", "navn", "min_side"})


def valider_manuell_tekst(tekst: str) -> str:
    """Kontrollerer koder og klammer i en egenskrevet e-posttekst - ved forhaandsvisning, FOER noe lagres eller sendes.
    Kaster MalFeil med norsk `forklaring` (ukjent kode, loes klamme, ugyldige tegn). Returnerer teksten med CRLF -> LF."""
    ren = _normaliser_kropp(tekst)
    _parse(ren, MANUELLE_KODER)
    return ren


def manuell_tekst_til_html(tekst: str, mottaker) -> Markup:
    """Egenskrevet e-posttekst -> trygg HTML for ÉN mottaker: {fornavn}/{navn} byttes med mottakerens egne verdier, og {min_side} med
    mottakerens egen lenke til Min side (eller «Mine kurs» når mottakeren ikke har noen). All tekst
    og alle verdier escapes; blank linje -> avsnitt, linjeskift -> <br> (som i de redigerbare malene)."""
    verdier = _person(mottaker)
    try:
        personlig = mottaker["min_side_url"]       # satt av systemet (Kjoring.send_admin_utsending), aldri fra admintekst
    except (KeyError, IndexError, TypeError):
        personlig = None

    def linje(l: str) -> Markup:
        ut = Markup("")
        for slag, verdi in _parse(l, MANUELLE_KODER):
            if slag == "tekst":
                ut += escape(verdi)
            elif verdi == "min_side":
                ut += _kodeverdi_html("min_side", {"min_side_url": personlig}, "admin_melding", "tekst")
            else:
                ut += escape(_rens_verdi(verdier[verdi]))
        return ut
    return _avsnitt_til_html(valider_manuell_tekst(tekst), linje)


# ============================ overstyringer: oppslag og lagring ============================

def hent_overstyringer(con, mal: str) -> dict:
    """Alle overstyringer for en KJENT mal som {felt: tekst}. Ingen rader -> tom dict (bruk standardtekst).
    Hver rad valideres: ukjent felt, ugyldig tekst -> MalFeil. DB-feil -> MalFeil (db_lesefeil). ALDRI stille fallback."""
    if not isinstance(mal, str) or mal not in MALER:
        raise MalFeil(UKJENT_MAL, None, None, "Denne malen kan ikke redigeres.")
    try:
        with db.isolert(con, "maltekst_les"):   # en lesefeil skal ikke avbryte kallerens transaksjon (PostgreSQL)
            rader = db.hent_overstyringer_for_mal(con, mal)
    except db.DatabaseFeil as e:
        raise MalFeil(DB_LESEFEIL, mal, None, "Kunne ikke lese maltekstene fra databasen.") from e
    ut = {}
    for felt, tekst in rader.items():
        ut[felt] = valider(mal, felt, tekst)   # ukjent felt / ugyldig tekst -> MalFeil
    return ut


def effektive_tekster(con, mal: str, kurs_id: int | None = None) -> dict:
    """{felt: tekst} for ALLE felt i malen: kursets egen tekst (kurs_maltekst, når `kurs_id` er gitt og malen kan tilpasses per kurs),
    ellers fellesteksten (overstyring under E-postmaler), ellers standardteksten."""
    over = hent_overstyringer(con, mal)
    tekster = {felt: over.get(felt, f.standard) for felt, f in MALER[mal].felt.items()}
    if kurs_id and mal in KURSVISE_MALER:
        tekster |= hent_kurstekster(con, kurs_id, mal)
    return tekster


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


# ============================ kursets egne tekster (Camilla 09.10.2026) ============================
# «Veldig mange av kursene er forskjellige, med ulike tekster, ulike regler» (f.eks. godkjent som vedlikeholdsaktivitet eller ikke):
# påmeldingsbekreftelsen kan tilpasses per kurs i fanen Kommunikasjon. Et kurs uten egen tekst følger fellesteksten (et utkast som er
# klart fra kurset er opprettet). Bare felt som er ULIKE fellesteksten lagres, så et kurs som ikke er tilpasset, følger endringer i
# fellesteksten. Samme regler og flettefelt som fellesteksten (valider).

KURSVISE_MALER = ("bekreftelse",)


def _kursmal(mal: str) -> None:
    if mal not in KURSVISE_MALER:
        raise MalFeil(UKJENT_MAL, mal, None, "Denne malen kan ikke tilpasses per kurs.")


def hent_kurstekster(con, kurs_id: int, mal: str) -> dict:
    """{felt: tekst} for feltene kurset har tilpasset (validert). Ugyldig lagret tekst eller DB-feil gir MalFeil."""
    _kursmal(mal)
    try:
        with db.isolert(con, "kursmal_les"):
            rader = con.execute("SELECT felt, tekst FROM kurs_maltekst WHERE kurs_id=? AND mal=?", (kurs_id, mal)).fetchall()
    except db.DatabaseFeil as e:
        raise MalFeil(DB_LESEFEIL, mal, None, "Kunne ikke lese kursets tekster fra databasen.") from e
    return {r["felt"]: valider(mal, r["felt"], r["tekst"]) for r in rader}


def lagre_kurstekster(con, kurs_id: int, mal: str, tekster: dict, aktor: str = "system") -> list[str]:
    """Lagrer kursets tekster for malen (validert FØR noe skrives). Et felt som er likt fellesteksten, lagres ikke (raden fjernes):
    kurset følger da fellesteksten videre. Returnerer feltene som ble endret. Logger kursmal_endret (aldri teksten). Committer ikke."""
    _kursmal(mal)
    felles = effektive_tekster(con, mal)
    rene = {}
    for felt in MALER[mal].felt:
        if felt in tekster:
            rene[felt] = valider(mal, felt, tekster[felt])           # MalFeil før noe er skrevet
    naa = hent_kurstekster(con, kurs_id, mal)
    endret = []
    for felt, ren in rene.items():
        if ren == felles[felt]:
            if felt in naa:
                con.execute("DELETE FROM kurs_maltekst WHERE kurs_id=? AND mal=? AND felt=?", (kurs_id, mal, felt))
                endret.append(felt)
        elif naa.get(felt) != ren:
            con.execute("DELETE FROM kurs_maltekst WHERE kurs_id=? AND mal=? AND felt=?", (kurs_id, mal, felt))
            con.execute("""INSERT INTO kurs_maltekst (kurs_id, mal, felt, tekst, oppdatert, av) VALUES (?,?,?,?,?,?)""",
                        (kurs_id, mal, felt, ren, db.naa_utc(), aktor))
            endret.append(felt)
    if endret:
        db.logg(con, "kursmal_endret", {"kurs_id": kurs_id, "mal": mal, "felt": endret}, aktor=aktor)
    return endret


def tilbakestill_kurstekster(con, kurs_id: int, mal: str, aktor: str = "system") -> bool:
    """Kurset følger fellesteksten igjen. Logger kursmal_tilbakestilt bare når noe faktisk var tilpasset. Committer ikke."""
    _kursmal(mal)
    antall = con.execute("DELETE FROM kurs_maltekst WHERE kurs_id=? AND mal=?", (kurs_id, mal)).rowcount
    if antall:
        db.logg(con, "kursmal_tilbakestilt", {"kurs_id": kurs_id, "mal": mal}, aktor=aktor)
    return bool(antall)


def kopier_kurstekster(con, fra_kurs_id: int, til_kurs_id: int, aktor: str = "system") -> None:
    """Kursets egne tekster følger med når kurset dupliseres (samme transaksjon som kopien)."""
    for r in con.execute("SELECT mal, felt, tekst FROM kurs_maltekst WHERE kurs_id=?", (fra_kurs_id,)).fetchall():
        con.execute("""INSERT INTO kurs_maltekst (kurs_id, mal, felt, tekst, oppdatert, av) VALUES (?,?,?,?,?,?)""",
                    (til_kurs_id, r["mal"], r["felt"], r["tekst"], db.naa_utc(), aktor))


def _kurs_id(data) -> int | None:
    """Kursets id fra dataene til en utsending (eksempeldataene i forhåndsvisningen av fellesmalen har ingen)."""
    try:
        return int(data["kurs"]["id"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


# ============================ kobling til utsending (12B2A) ============================

# Alle 8 redigerbare maler er koblet til den faktiske utsendingen (fase 12B2A-12B2C-5). Kun de LAASTE malene
# (innlogging, admin_melding) rendres utenfor dette systemet. EKSPLISITT liste (IKKE frozenset(MALER)):
# en fremtidig ny mal i MALER-registeret skal ALDRI bli automatisk aktivert bare fordi den er registrert - den
# maa faa sin egen verdibygger og eksplisitt legges til her foerst (fail-closed). lagre_maltekst() nekter
# fortsatt aa lagre for enhver mal som ikke staar i denne listen, slik at en override aldri blir stille ignorert.
AKTIVE_MALER = frozenset({"venteliste", "avlysning", "bekreftelse", "ukefor", "dagfor", "kursbevis_klar",
                          "firmapaamelding_kvittering", "evaluering"})


def _person(rad) -> dict:
    """Mottakerens navnekoder: {fornavn} og {navn} (fullt navn)."""
    return {"fornavn": rad["fornavn"], "navn": rad["navn"]}


def _med_min_side(verdier: dict, data) -> dict:
    """Legger den personlige lenken til Min side (min_side_url, bygget av Kjoring) til verdiene når e-posten har en. Det er ikke en kode
    admin kan skrive: bare {min_side} bruker den, og bare når den er systembygd (se _kodeverdi_html)."""
    lenke = data.get("min_side_url") if hasattr(data, "get") else None
    return {**verdier, "min_side_url": lenke} if lenke else verdier


def _venteliste_verdier(data) -> dict:
    return {**_person(data["p"]), "kursnavn": data["kurs"]["navn"]}


def _avlysning_verdier(data) -> dict:
    """KUN det registeret tillater: mottakerens navn og kursnavn. Ingen e-post, telefon, adresse, faktura eller sensitivt.
    (Systemlenkene {min_side} og {sporsmal_url} bygges av maltekster fra BASE_URL, ikke fra data.)"""
    return {**_person(data["d"]), "kursnavn": data["kurs"]["navn"]}


def _bekreftelse_verdier(data) -> dict:
    """KUN det registeret tillater: mottakerens navn, kursnavn og foerste kursdag (ISO). Ingen e-post, telefon, adresse eller
    fakturadata. Fakturainformasjonen er en LAAST systemblokk i malfilen, bygget fra `faktura_plan` (samme FakturaPlan som
    fakturamotoren) - aldri en kode og aldri redigerbar. Mangler planen -> MalFeil (fail closed: aldri en bekreftelse uten
    sann fakturainformasjon). Uten kursdager utelates {startdato} (bruker admin den, blir det MalFeil - aldri en oppdiktet dato)."""
    data["faktura_plan"]
    verdier = {**_person(data["p"]), "kursnavn": data["kurs"]["navn"]}
    if data["dager"]:
        verdier["startdato"] = data["dager"][0]["dato"]
    return _med_min_side(verdier, data)


def _ukefor_verdier(data) -> dict:
    """KUN det registeret tillater: navn, kursnavn og foerste kursdag (ISO). Kursdager/klokkeslett, sted/QR-innsjekk og Zoom-
    informasjon er LAASTE systemblokker i malfilen - aldri koder, aldri redigerbare. (Kursets notat er en intern kommentar
    og er aldri med i e-post.) `dager` er dagene mottakeren er på (en ekstradeltaker: bare egne samlinger), så {startdato} er
    DERES første dag. Uten dager utelates {startdato} (bruker admin den, blir det MalFeil - aldri en oppdiktet dato)."""
    verdier = {**_person(data["d"]), "kursnavn": data["kurs"]["navn"]}
    if data["dager"]:
        verdier["startdato"] = data["dager"][0]["dato"]
    return _med_min_side(verdier, data)


def _dagfor_verdier(data) -> dict:
    """KUN det registeret tillater. Tid, sted/QR og Zoom-lenke/ID/passord er LAASTE systemblokker i malfilen (Zoom-hemmeligheter
    er aldri koder). Alle varianter (forste/midt/siste) faar verdier, saa en ugyldig variant feiler lukket uansett dag."""
    return _med_min_side({**_person(data["d"]), "kursnavn": data["kurs"]["navn"], "dato": data["dag"]["dato"],
                          "dagnummer": data["nr"], "antall_dager": data["antall"]}, data)


def _kursbevis_klar_verdier(data) -> dict:
    """KUN det registeret tillater: mottakerens navn og kursnavn. Ingen dokument-ID, filsti eller annen intern referanse -
    Min side-lenken er systemkoden {min_side} (bygget av maltekster fra BASE_URL), aldri en fri URL eller sti fra admin."""
    return {**_person(data), "kursnavn": data["kurs"]["navn"]}


def _evaluering_verdier(data) -> dict:
    """KUN det registeret tillater: mottakerens navn og kursnavn. Lenken til spørreskjemaet er en LAAST systemblokk i malfilen
    (personlig og signert, evaluering.lenke) - aldri en kode."""
    return {**_person(data["d"]), "kursnavn": data["kurs"]["navn"]}


def _firmapaamelding_kvittering_verdier(data) -> dict:
    """KUN det registeret tillater: kontaktpersonens navn, kursnavn, firmanavn og antall deltakere (som tekst,
    f.eks. "3 deltakere"). ALDRI e-post, telefon, org.nr, faktura-/betalingsdata. De faktiske utfallstallene
    (antall bekreftet/venteliste/feilet) og kvitteringslenken er LAASTE systemblokker i malfilen, bygget fra det
    virkelige registreringsresultatet ETTER at deltakerne er meldt paa - de er ALDRI koder her, og kan derfor
    ikke gjoeres om av admin. `antall_totalt` (antall deltakere i INNSENDINGEN) er derimot kjent FOER noen
    registrering skjer, saa {antall_deltakere} kan trygt rendres i preflight, foer varig sideeffekt."""
    antall = data["antall_totalt"]
    return {**_person(data["kontakt"]), "kursnavn": data["kurs"]["navn"], "firmanavn": data["kontakt"]["firmanavn"],
            "antall_deltakere": f"{antall} deltaker{'e' if antall != 1 else ''}"}


_VERDIER = {"venteliste": _venteliste_verdier, "avlysning": _avlysning_verdier, "bekreftelse": _bekreftelse_verdier,
            "ukefor": _ukefor_verdier, "dagfor": _dagfor_verdier, "kursbevis_klar": _kursbevis_klar_verdier,
            "firmapaamelding_kvittering": _firmapaamelding_kvittering_verdier,
            "evaluering": _evaluering_verdier,
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
    return _bygg(mal, effektive_tekster(con, mal, _kurs_id(data)), data)
