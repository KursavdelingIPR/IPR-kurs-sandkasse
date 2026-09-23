"""Paameldingsskjemaet (fase 12C1/12C2): kode-registeret for den ORDINAERE offentlige paameldingen (kurs.html).

Dette er fasit for hvilke felt skjemaet har, standard label/synlighet/obligatorisk/rekkefolge, og hvilke egenskaper
som kan overstyres per kurs. Se design-lock 12C0.

Prinsipper:
  * Registeret i KODE er fasit. Det finnes ingen egendefinerte felt - kun feltene under er gyldige.
  * Modulen er REN: ingen SQL, ingen database, ingen request-kontekst, ingen global tilstand. Lagring/lesing av
    overstyringer (tabellen kurs_skjemafelt) ligger i db.py, som bruker valideringen her FOER skriving.
  * navn, epost og samtykke er LAAST (alltid synlige og obligatoriske, fast tekst) og rendres fast i kurs.html.
  * Kun telefon og arbeidssted er konfigurerbare, og kun innenfor sin egen gruppe.
  * hpr_nr er et BETINGET fast felt: vises kun naar kurset er i et spesialistlop, er aldri obligatorisk. Kun
    hjelpeteksten kan overstyres.
  * Faktura-/betalingsblokken og de sensitive feltene er FASTE SYSTEMBLOKKER: struktur og betingelser ligger i
    kurs.html/kursinnstillingene og kan aldri styres av skjemakonfigurasjon. Her beskrives kun NAAR de vises.
  * Bedriftspaamelding (kurs_gruppe.html), manuell registrering og CSV-import er IKKE omfattet.

Overstyringer (12C2):
  * Databasen lagrer KUN avvik fra kodet standard. Verdi lik standard / tom tekst -> ingen overstyring.
  * LAGRING er streng: ukjent felt, laast felt, ikke-tillatt egenskap, feil type, for lang tekst eller kontrolltegn
    -> SkjemafeltFeil, og ingenting skrives.
  * LESING er defensiv (ikke som maltekster, som kaster MalFeil): en ugyldig/ukjent rad eller egenskap ignoreres,
    kodet standard gjelder for den egenskapen, og kalleren faar en PII-fri Advarsel. Gyldige egenskaper i samme rad
    brukes fortsatt. Ingen DB-verdi kan laase opp en systemregel.
  * En reell DB-LESEFEIL er noe helt annet: db.py kaster SkjemaLesefeil og returnerer ALDRI et tomt sett.
  * Label/hjelpetekst er REN TEKST: ingen HTML, Markdown eller Jinja tolkes - Jinja escaper ved rendring.
    <, >, { og } er lovlige tegn; det viktige er at de aldri tolkes.
"""
import re
from dataclasses import dataclass
from types import MappingProxyType

# Kategorier
LAAST = "laast"                     # alltid synlig + obligatorisk, ingen overstyring
KONFIGURERBAR = "konfigurerbar"     # konfigurerbar deltakerfelt-gruppe
BETINGET = "betinget"               # fast felt med fast visningsbetingelse fra kursdata

# Egenskaper som kan overstyres per kurs - kun der feltet tillater det. Rekkefolgen her er ogsaa kolonnerekkefolgen i
# kurs_skjemafelt (db.py bruker denne som WHITELIST for kolonnenavn).
SYNLIG, OBLIGATORISK, REKKEFOLGE, LABEL, HJELPETEKST = "synlig", "obligatorisk", "rekkefolge", "label", "hjelpetekst"
EGENSKAPER_REKKEFOLGE = (SYNLIG, OBLIGATORISK, REKKEFOLGE, LABEL, HJELPETEKST)
EGENSKAPER = frozenset(EGENSKAPER_REKKEFOLGE)

MAKS_LABEL, MAKS_HJELPETEKST = 80, 300
# SQLite INTEGER er 64-bit. Ikke en produktgrense - kun det databasen faktisk kan lagre.
_MIN_HELTALL, _MAKS_HELTALL = -(2 ** 63), 2 ** 63 - 1


@dataclass(frozen=True)
class Skjemafelt:
    nokkel: str                 # = name i skjemaet
    label: str                  # standard label
    kategori: str
    synlig: bool                # standard synlighet (for BETINGET: gjelder kun naar betingelsen er oppfylt)
    obligatorisk: bool          # standard obligatorisk
    overstyrbart: frozenset     # egenskaper som kan overstyres per kurs (tom = helt laast)
    rekkefolge: int | None = None   # standardplass i den konfigurerbare gruppen (None = fast plassering)


@dataclass(frozen=True)
class Systemblokk:
    nokkel: str
    felt: tuple                 # name-ene blokken sender inn (til senere server-side haandtering, 12C3)
    beskrivelse: str


# ============================ register ============================

_NAVN = Skjemafelt("navn", "Navn", LAAST, True, True, frozenset())
_EPOST = Skjemafelt("epost", "E-post", LAAST, True, True, frozenset())
_SAMTYKKE = Skjemafelt("samtykke", "Samtykke til vilkår og personvernerklæring", LAAST, True, True, frozenset())
_TELEFON = Skjemafelt("telefon", "Telefon", KONFIGURERBAR, True, False, EGENSKAPER, rekkefolge=1)
_ARBEIDSSTED = Skjemafelt("arbeidssted", "Arbeidssted", KONFIGURERBAR, True, False, EGENSKAPER, rekkefolge=2)
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


# ============================ feil og advarsler ============================

# Grunnkoder (PII-frie, trygge aa logge). Bærer aldri tekstinnhold.
UKJENT_FELT = "ukjent_felt"
LAAST_FELT = "laast_felt"
UKJENT_EGENSKAP = "ukjent_egenskap"
IKKE_TILLATT = "ikke_tillatt"           # egenskapen finnes, men kan ikke overstyres for dette feltet
UGYLDIG_TYPE = "ugyldig_type"
FOR_LANG = "for_lang"
KONTROLLTEGN = "kontrolltegn"
UTENFOR_OMRAADE = "utenfor_omraade"


class SkjemafeltFeil(Exception):
    """Avvist lagring. str(e)/detaljer() inneholder KUN grunnkode, felt og egenskap - aldri tekstinnhold.
    `forklaring` er norsk tekst til admin (uten innholdet) - skal ikke logges."""

    def __init__(self, grunn: str, felt: str | None = None, egenskap: str | None = None, forklaring: str = ""):
        super().__init__(grunn, felt, egenskap)
        self.grunn, self.felt, self.egenskap, self.forklaring = grunn, felt, egenskap, forklaring

    def __str__(self) -> str:
        return f"{self.grunn} ({self.felt or '?'}.{self.egenskap or '?'})"

    def detaljer(self) -> dict:
        return {"felt": self.felt, "egenskap": self.egenskap, "grunn": self.grunn}


class SkjemaLesefeil(Exception):
    """Skjemakonfigurasjonen kunne IKKE leses fra databasen (reell DB-feil). Skal aldri tolkes som «ingen overstyringer».
    Bærer kun kurs_id - ingen SQL, sti eller innhold."""

    def __init__(self, kurs_id):
        super().__init__(kurs_id)
        self.kurs_id = kurs_id

    def __str__(self) -> str:
        return f"skjema_lesefeil (kurs {self.kurs_id})"


@dataclass(frozen=True)
class Advarsel:
    """PII-fri beskjed om at en lagret rad/egenskap ble ignorert ved lesing. `felt` settes KUN hvis det er et kjent
    felt fra registeret (et ukjent feltnavn fra databasen gjentas aldri), `egenskap` kun hvis den er kjent."""
    grunn: str
    felt: str | None = None
    egenskap: str | None = None


# ============================ validering av enkeltverdier ============================

_KONTROLL = re.compile(r"[\x00-\x1f\x7f\x85  ]")            # alt, inkl. linjeskift og tab
_KONTROLL_UTEN_LF = re.compile(r"[\x00-\x09\x0b-\x1f\x7f\x85  ]")


def _valider_label(verdi, felt: str) -> str | None:
    if not isinstance(verdi, str):
        raise SkjemafeltFeil(UGYLDIG_TYPE, felt, LABEL, "Ledeteksten må være tekst.")
    if _KONTROLL.search(verdi):
        raise SkjemafeltFeil(KONTROLLTEGN, felt, LABEL, "Ledeteksten må være én linje uten spesialtegn.")
    ren = verdi.strip()
    if len(ren) > MAKS_LABEL:
        raise SkjemafeltFeil(FOR_LANG, felt, LABEL, f"Ledeteksten kan være maks {MAKS_LABEL} tegn (er nå {len(ren)}).")
    return ren or None


def _valider_hjelpetekst(verdi, felt: str) -> str | None:
    if not isinstance(verdi, str):
        raise SkjemafeltFeil(UGYLDIG_TYPE, felt, HJELPETEKST, "Hjelpeteksten må være tekst.")
    t = verdi.replace("\r\n", "\n").replace("\r", "\n")
    if _KONTROLL_UTEN_LF.search(t):
        raise SkjemafeltFeil(KONTROLLTEGN, felt, HJELPETEKST, "Hjelpeteksten inneholder ugyldige tegn.")
    ren = t.strip()
    if len(ren) > MAKS_HJELPETEKST:
        raise SkjemafeltFeil(FOR_LANG, felt, HJELPETEKST,
                             f"Hjelpeteksten kan være maks {MAKS_HJELPETEKST} tegn (er nå {len(ren)}).")
    return ren or None


def _valider_bool(verdi, felt: str, egenskap: str) -> bool:
    if not isinstance(verdi, bool):
        raise SkjemafeltFeil(UGYLDIG_TYPE, felt, egenskap, "Verdien må være ja eller nei.")
    return verdi


def _valider_rekkefolge(verdi, felt: str) -> int:
    if isinstance(verdi, bool) or not isinstance(verdi, int):
        raise SkjemafeltFeil(UGYLDIG_TYPE, felt, REKKEFOLGE, "Rekkefølgen må være et helt tall.")
    if not _MIN_HELTALL <= verdi <= _MAKS_HELTALL:
        raise SkjemafeltFeil(UTENFOR_OMRAADE, felt, REKKEFOLGE, "Rekkefølgen er utenfor gyldig område.")
    return verdi


def _valider_egenskap(f: Skjemafelt, egenskap: str, verdi):
    """Validerer og normaliserer EN egenskap for et kjent felt. Returnerer verdien som skal lagres, eller None naar
    verdien er lik kodet standard / tom (= ingen overstyring). Kaster SkjemafeltFeil. `verdi` er aldri None her."""
    if egenskap not in EGENSKAPER:
        raise SkjemafeltFeil(UKJENT_EGENSKAP, f.nokkel, None, "Ukjent egenskap.")
    if egenskap not in f.overstyrbart:
        raise SkjemafeltFeil(IKKE_TILLATT, f.nokkel, egenskap, "Denne egenskapen kan ikke endres for dette feltet.")
    if egenskap in (SYNLIG, OBLIGATORISK):
        v = _valider_bool(verdi, f.nokkel, egenskap)
        return None if v == getattr(f, egenskap) else v
    if egenskap == REKKEFOLGE:
        v = _valider_rekkefolge(verdi, f.nokkel)
        return None if v == f.rekkefolge else v     # NB: 0 er en gyldig, eksplisitt rekkefolge - aldri "falsy = ingen"
    if egenskap == LABEL:
        v = _valider_label(verdi, f.nokkel)
        return None if v == f.label else v
    return _valider_hjelpetekst(verdi, f.nokkel)    # ingen standard hjelpetekst: kun tom -> None


# ============================ overstyringsmodell ============================

@dataclass(frozen=True)
class Overstyring:
    """Gyldige, normaliserte avvik for ETT felt. None = kodet standard for den egenskapen."""
    synlig: bool | None = None
    obligatorisk: bool | None = None
    rekkefolge: int | None = None
    label: str | None = None
    hjelpetekst: str | None = None

    @property
    def er_tom(self) -> bool:
        return all(getattr(self, e) is None for e in EGENSKAPER_REKKEFOLGE)

    def satte_egenskaper(self) -> tuple:
        return tuple(e for e in EGENSKAPER_REKKEFOLGE if getattr(self, e) is not None)


def _slaa_opp_konfigurerbart(felt) -> Skjemafelt:
    if not isinstance(felt, str) or felt not in REGISTER:
        raise SkjemafeltFeil(UKJENT_FELT, None, None, "Ukjent skjemafelt.")
    f = REGISTER[felt]
    if not f.overstyrbart:
        raise SkjemafeltFeil(LAAST_FELT, felt, None, "Dette feltet er låst og kan ikke endres.")
    return f


def normaliser_overstyring(felt: str, egenskaper: dict) -> Overstyring:
    """STRENG validering foer lagring. `egenskaper` er den KOMPLETTE overstyringen for feltet (utelatt/None = standard).
    Returnerer kun faktiske avvik. Kaster SkjemafeltFeil ved foerste feil - da skal ingenting skrives."""
    f = _slaa_opp_konfigurerbart(felt)
    if not isinstance(egenskaper, dict):
        raise SkjemafeltFeil(UGYLDIG_TYPE, felt, None, "Ugyldig format.")
    ukjente = [e for e in egenskaper if e not in EGENSKAPER]
    if ukjente:
        raise SkjemafeltFeil(UKJENT_EGENSKAP, felt, None, "Ukjent egenskap.")
    ut = {}
    for e in EGENSKAPER_REKKEFOLGE:
        if e not in egenskaper:
            continue
        if e not in f.overstyrbart:     # ogsaa naar verdien er None: forsoket i seg selv avvises
            raise SkjemafeltFeil(IKKE_TILLATT, felt, e, "Denne egenskapen kan ikke endres for dette feltet.")
        if egenskaper[e] is not None:
            ut[e] = _valider_egenskap(f, e, egenskaper[e])
    return Overstyring(**ut)


def les_overstyringer(rader) -> "Leseresultat":
    """DEFENSIV tolkning av raa rader fra kurs_skjemafelt (dict-lignende med felt + egenskapskolonner). Ren funksjon.

    Ukjent/laast felt -> hele raden ignoreres. Ikke-tillatt eller ugyldig egenskap -> KUN den egenskapen ignoreres
    (kodet standard gjelder), resten av raden brukes. Hver ignorering gir en PII-fri Advarsel. Verdier fra SQLite:
    synlig/obligatorisk er 0/1 (CHECK), og tolkes som bool; alt annet er ugyldig type."""
    ut, advarsler = {}, []
    for rad in rader:
        felt = rad["felt"]
        if not isinstance(felt, str) or felt not in REGISTER:
            advarsler.append(Advarsel(UKJENT_FELT))
            continue
        f = REGISTER[felt]
        if not f.overstyrbart:
            advarsler.append(Advarsel(LAAST_FELT, felt))
            continue
        gyldige = {}
        for e in EGENSKAPER_REKKEFOLGE:
            verdi = rad[e]
            if verdi is None:
                continue
            if e in (SYNLIG, OBLIGATORISK) and type(verdi) is int and verdi in (0, 1):
                verdi = bool(verdi)
            try:
                gyldige[e] = _valider_egenskap(f, e, verdi)
            except SkjemafeltFeil as feil:
                advarsler.append(Advarsel(feil.grunn, felt, e))
        o = Overstyring(**gyldige)
        if not o.er_tom:
            ut[felt] = o
    return Leseresultat(MappingProxyType(ut), tuple(advarsler))


@dataclass(frozen=True)
class Leseresultat:
    overstyringer: MappingProxyType     # felt -> Overstyring (kun gyldige, ikke-tomme)
    advarsler: tuple                    # Advarsel, PII-frie

    @property
    def har_advarsler(self) -> bool:
        return bool(self.advarsler)


# ============================ effektivt skjema ============================

@dataclass(frozen=True)
class EffektivtFelt:
    nokkel: str
    label: str
    obligatorisk: bool
    hjelpetekst: str | None = None


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


def _gyldige_avvik(f: Skjemafelt, o) -> dict:
    """Forsvar i dybden: selv en haandlaget Overstyring kan aldri laase opp noe registeret ikke tillater. Kun tillatte
    egenskaper med gyldig verdi tas med; alt annet ignoreres stille (standard gjelder)."""
    if not isinstance(o, Overstyring):
        return {}
    ut = {}
    for e in f.overstyrbart:
        verdi = getattr(o, e)
        if verdi is None:
            continue
        try:
            v = _valider_egenskap(f, e, verdi)
        except SkjemafeltFeil:
            continue
        if v is not None:
            ut[e] = v
    return ut


def _effektivt(f: Skjemafelt, avvik: dict) -> EffektivtFelt:
    return EffektivtFelt(f.nokkel, avvik.get(LABEL, f.label), avvik.get(OBLIGATORISK, f.obligatorisk),
                         avvik.get(HJELPETEKST))


def effektivt_skjema(kurs, overstyringer=None) -> EffektivtSkjema:
    """Det skjemaet deltakeren faktisk faar for dette kurset. Ren funksjon: leser KUN kursraden og de (allerede
    leste) overstyringene som sendes inn - aldri databasen. Uten overstyringer: noyaktig kodet standard (= 12C1).

    Rekkefolge i gruppen: stabil sortering paa (effektiv rekkefolge, standardindeks). HPR, fakturablokk og sensitive
    felt folger ALLTID kursdata - ingen overstyring kan skjule/vise/flytte dem eller gjore HPR obligatorisk.

    `overstyringer`: {felt: Overstyring} eller et Leseresultat (da brukes kun de gyldige overstyringene)."""
    if isinstance(overstyringer, Leseresultat):
        overstyringer = overstyringer.overstyringer
    overstyringer = overstyringer or {}
    gruppe = []
    for indeks, nokkel in enumerate(KONFIGURERBAR_GRUPPE):
        f = REGISTER[nokkel]
        avvik = _gyldige_avvik(f, overstyringer.get(nokkel))
        if not avvik.get(SYNLIG, f.synlig):
            continue
        gruppe.append((avvik.get(REKKEFOLGE, f.rekkefolge), indeks, _effektivt(f, avvik)))
    felt = [ef for _, _, ef in sorted(gruppe, key=lambda x: (x[0], x[1]))]
    if vis_hpr(kurs):
        felt.append(_effektivt(_HPR, _gyldige_avvik(_HPR, overstyringer.get(_HPR.nokkel))))
    return EffektivtSkjema(tuple(felt), vis_fakturablokk(kurs), vis_sensitive_felt(kurs))
