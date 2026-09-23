"""Paameldingsskjemaet (fase 12C1): kode-registeret for den ORDINAERE offentlige paameldingen (kurs.html).

Dette er fasit for hvilke felt skjemaet har, standard label/synlighet/obligatorisk/rekkefolge, og hvilke egenskaper
som (i en senere fase) kan overstyres per kurs. Se design-lock 12C0.

Prinsipper:
  * Registeret i KODE er fasit. Det finnes ingen egendefinerte felt - kun feltene under er gyldige.
  * 12C1 har INGEN databaseoverstyringer: effektivt_skjema() er en ren funksjon av kursdata + kodede standarder.
  * navn, epost og samtykke er LAAST (alltid synlige og obligatoriske, fast tekst) og rendres fast i kurs.html.
  * Kun telefon og arbeidssted er (fremtidig) konfigurerbare, og kun innenfor sin egen gruppe.
  * hpr_nr er et BETINGET fast felt: vises kun naar kurset er i et spesialistlop, er aldri obligatorisk.
  * Faktura-/betalingsblokken og de sensitive feltene er FASTE SYSTEMBLOKKER: struktur og betingelser ligger i
    kurs.html/kursinnstillingene og kan aldri styres av skjemakonfigurasjon. Her beskrives kun NAAR de vises.
  * Bedriftspaamelding (kurs_gruppe.html), manuell registrering og CSV-import er IKKE omfattet.
"""
from dataclasses import dataclass
from types import MappingProxyType

# Kategorier
LAAST = "laast"                     # alltid synlig + obligatorisk, ingen overstyring
KONFIGURERBAR = "konfigurerbar"     # konfigurerbar deltakerfelt-gruppe
BETINGET = "betinget"               # fast felt med fast visningsbetingelse fra kursdata

# Egenskaper som (fra 12C2+) KAN overstyres per kurs - kun der feltet tillater det
SYNLIG, OBLIGATORISK, LABEL, HJELPETEKST, REKKEFOLGE = "synlig", "obligatorisk", "label", "hjelpetekst", "rekkefolge"
EGENSKAPER = frozenset({SYNLIG, OBLIGATORISK, LABEL, HJELPETEKST, REKKEFOLGE})


@dataclass(frozen=True)
class Skjemafelt:
    nokkel: str                 # = name i skjemaet
    label: str                  # standard label
    kategori: str
    synlig: bool                # standard synlighet (for BETINGET: gjelder kun naar betingelsen er oppfylt)
    obligatorisk: bool          # standard obligatorisk
    overstyrbart: frozenset     # egenskaper som kan overstyres per kurs (tom = helt laast)


@dataclass(frozen=True)
class Systemblokk:
    nokkel: str
    felt: tuple                 # name-ene blokken sender inn (til senere server-side haandtering, 12C3)
    beskrivelse: str


# ============================ register ============================

_NAVN = Skjemafelt("navn", "Navn", LAAST, True, True, frozenset())
_EPOST = Skjemafelt("epost", "E-post", LAAST, True, True, frozenset())
_SAMTYKKE = Skjemafelt("samtykke", "Samtykke til vilkår og personvernerklæring", LAAST, True, True, frozenset())
_TELEFON = Skjemafelt("telefon", "Telefon", KONFIGURERBAR, True, False, EGENSKAPER)
_ARBEIDSSTED = Skjemafelt("arbeidssted", "Arbeidssted", KONFIGURERBAR, True, False, EGENSKAPER)
_HPR = Skjemafelt("hpr_nr", "HPR-nummer", BETINGET, True, False, frozenset({HJELPETEKST}))

# Alle gyldige felt. Uforanderlig: det finnes ingen maate aa legge til felt uten aa endre koden.
REGISTER = MappingProxyType({f.nokkel: f for f in (_NAVN, _EPOST, _TELEFON, _ARBEIDSSTED, _HPR, _SAMTYKKE)})

# Standardrekkefolgen i den konfigurerbare gruppen (rett etter e-post). HPR staar alltid fast etter gruppen.
KONFIGURERBAR_GRUPPE = ("telefon", "arbeidssted")

FAKTURABLOKK = Systemblokk(
    "faktura",
    ("betaler", "org_navn", "org_nr", "faktura_ref", "faktura_epost", "ehf", "betaling",
     "faktura_adresse", "faktura_postnr", "faktura_sted"),
    "Hvem betaler, organisasjon/EHF, betalingsmåte og fakturaadresse - styres av kursets faktureringsoppsett.")
SENSITIV_BLOKK = Systemblokk(
    "sensitivt", ("allergier", "tilrettelegging"),
    "Allergier og tilrettelegging - lagres kun i tabellen sensitivt og slettes automatisk etter kurset.")


# ============================ effektivt skjema ============================

@dataclass(frozen=True)
class EffektivtFelt:
    nokkel: str
    label: str
    obligatorisk: bool


@dataclass(frozen=True)
class EffektivtSkjema:
    deltakerfelt: tuple         # synlige felt etter e-post, i rekkefolge: konfigurerbar gruppe, deretter ev. HPR
    vis_fakturablokk: bool
    vis_sensitive_felt: bool


def vis_hpr(kurs) -> bool:
    return bool(kurs["spesialistlop"])


def vis_fakturablokk(kurs) -> bool:
    return kurs["fakturering"] == "person" and bool(kurs["pris_nok"])


def vis_sensitive_felt(kurs) -> bool:
    return kurs["type"] != "digital"


def _effektivt(felt: Skjemafelt) -> EffektivtFelt:
    return EffektivtFelt(felt.nokkel, felt.label, felt.obligatorisk)


def effektivt_skjema(kurs) -> EffektivtSkjema:
    """Det skjemaet deltakeren faktisk faar for dette kurset. Ren funksjon: leser KUN kursraden, aldri databasen."""
    felt = [_effektivt(REGISTER[n]) for n in KONFIGURERBAR_GRUPPE if REGISTER[n].synlig]
    if vis_hpr(kurs):
        felt.append(_effektivt(_HPR))
    return EffektivtSkjema(tuple(felt), vis_fakturablokk(kurs), vis_sensitive_felt(kurs))
