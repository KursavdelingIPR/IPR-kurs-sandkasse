"""Paameldingsskjemaet: kode-registeret for STANDARDFELTENE i den ordinaere offentlige paameldingen (kurs.html), og
det effektive skjemaet et kurs faar (standardfeltene + kursets egne felt fra kurs/ekstrafelt.py).

Dette er fasit for hvilke standardfelt skjemaet har, standard ledetekst/hjelpetekst/synlighet/obligatorisk/rekkefolge,
og hvilke egenskaper skjemabyggeren (fanen «Påmeldingsskjema») kan endre per kurs. Design-lock 12C0 («ingen
egendefinerte felt, kun telefon og arbeidssted kan endres») er erstattet av skjemabyggeren (migrering 9), med reglene
under.

Prinsipper:
  * Registeret i KODE er fasit for STANDARDFELTENE. Kursets EGNE felt (tekst, avkrysning, valg ...) ligger i tabellen
    kurs_ekstrafelt og valideres i kurs/ekstrafelt.py - de kan aldri erstatte, skjule eller endre et standardfelt, og
    aldri brukes til sensitive opplysninger.
  * Modulen er REN: ingen SQL, ingen database, ingen request-kontekst, ingen global tilstand. Lagring/lesing av
    overstyringer (tabellen kurs_skjemafelt) ligger i db.py, som bruker valideringen her FOER skriving.
  * fornavn, etternavn, epost og samtykke er LAAST (alltid synlige og obligatoriske, fast tekst) og rendres fast i kurs.html.
  * ADRESSE (adresse, postnr, poststed) er ogsaa LAAST: deltakerens PRIVATE adresse kreves av ALLE nye paameldinger i dette
    skjemaet (paa ALLE kurs, ogsaa naar arbeidsgiver betaler: ikke alle betaler selv, og fakturaen maa kunne sendes til
    deltakeren). Feltene staar rett etter e-post, kan aldri skjules eller gjoeres valgfrie, og lagres paa personen (tabellen
    deltaker). Kravet gjelder ogsaa bedriftspaameldingen og «Legg til deltaker», og webhooken og CSV-importen
    (config.ADRESSE_KREVES_I_WEBHOOK/_CSV er PAA som standard; bare «0» er en midlertidig noedbrems), og eldre personer
    kan mangle adressen (adresse_mangler, merket «Privat adresse mangler»). En privat faktura lages aldri uten komplett
    adresse (privatadresse.py).
  * «Om deg» (KONFIGURERBAR): telefon, arbeidssted og yrkestittel kan skjules, gjoeres obligatoriske, faa egen ledetekst
    og hjelpetekst og flyttes - ogsaa mellom kursets egne felt i samme del av skjemaet.
  * hpr_nr er et BETINGET fast felt: vises kun naar kurset er i et spesialistlop, er aldri obligatorisk. Kun
    hjelpeteksten kan overstyres. FRA 02.10.2026 ER HPR SLÅTT AV (HPR_I_SKJEMA = False): ingen skjema viser det.
  * FAKTURA: fakturablokken vises bare naar kurset faktureres deltakeren (vis_fakturablokk - uendret regel). Selve
    blokken er fast (FAKTURABLOKK: hvem betaler, organisasjonsnummer med firmanavn og adresse fra Enhetsregisteret,
    betalingsmaate). Feltene i den (faktura merkes med, e-post, kommentar og EHF naar firma betaler) kan skjules,
    gjoeres obligatoriske (ikke EHF) og faa egne tekster. Betaler deltakeren selv, er fakturaadressen deltakerens egen
    adresse (ADRESSE over) - den har ingen egne felt i skjemaet. De gamle private fakturafeltene (AVVIKLEDE_FELT) er
    fjernet; lagrede overstyringer for dem leses uten feil og uten advarsel, og de brukes aldri.
  * SENSITIV: allergier og tilrettelegging vises bare paa kurs med oppmoete (vis_sensitive_felt - uendret regel), er
    ALDRI obligatoriske og lagres kun i tabellen sensitivt (slettes automatisk). De kan skjules og faa egne tekster.
  * INFO: hva informasjonsboksen ved siden av skjemaet viser (dato og klokkeslett, sted, pris, frist, ledige plasser).
    Kun vis/skjul. At kurset er fullt (venteliste), vises alltid.
  * Bedriftspaamelding (kurs_gruppe.html), manuell registrering og CSV-import er IKKE omfattet.

Overstyringer (12C2):
  * Databasen lagrer KUN avvik fra kodet standard. Verdi lik standard / tom tekst -> ingen overstyring. Unntak: et felt
    med STANDARD hjelpetekst kan faa hjelpeteksten fjernet - det lagres som tom tekst ('').
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
KONFIGURERBAR = "konfigurerbar"     # «Om deg»: kan skjules, gjoeres obligatorisk, faa egne tekster og flyttes
BETINGET = "betinget"               # fast felt med fast visningsbetingelse fra kursdata
FAKTURA = "faktura"                 # felt i fakturablokken (vises bare naar blokken vises og riktig betaler er valgt)
SENSITIV = "sensitiv"               # allergier/tilrettelegging: aldri obligatoriske, bare paa kurs med oppmoete
INFO = "info"                       # informasjonsboksen ved siden av skjemaet: kun vis/skjul

# Egenskaper som kan overstyres per kurs - kun der feltet tillater det. Rekkefolgen her er ogsaa kolonnerekkefolgen i
# kurs_skjemafelt (db.py bruker denne som WHITELIST for kolonnenavn).
SYNLIG, OBLIGATORISK, REKKEFOLGE, LABEL, HJELPETEKST = "synlig", "obligatorisk", "rekkefolge", "label", "hjelpetekst"
EGENSKAPER_REKKEFOLGE = (SYNLIG, OBLIGATORISK, REKKEFOLGE, LABEL, HJELPETEKST)
EGENSKAPER = frozenset(EGENSKAPER_REKKEFOLGE)
_TEKSTFELT = frozenset({SYNLIG, OBLIGATORISK, LABEL, HJELPETEKST})     # fakturafelt: alt unntatt plass
_UTEN_KRAV = frozenset({SYNLIG, LABEL, HJELPETEKST})                  # kan aldri bli obligatoriske

MAKS_LABEL, MAKS_HJELPETEKST = 80, 300
# SQLite INTEGER er 64-bit. Ikke en produktgrense - kun det databasen faktisk kan lagre.
_MIN_HELTALL, _MAKS_HELTALL = -(2 ** 63), 2 ** 63 - 1

# Delene av skjemaet der kursets egne felt kan plasseres, og hvordan «vis bare når» viser til et annet felt.
OM_DEG, TIL_SLUTT = "om_deg", "til_slutt"
NEDERST = "nederst"          # egne felt helt nederst: etter vilkårene, rett over Meld på-knappen (f.eks. nyhetsbrev, 05.10.2026)
BETALER = "betaler"                 # «Betale privat / firma» i fakturablokken (verdiene PERSON / ORGANISASJON)
PERSON, ORGANISASJON = "person", "organisasjon"
EKSTRA_REF = "ekstra:"              # 'ekstra:<id>' = et av kursets egne felt (name i skjemaet: 'ekstra_<id>')


@dataclass(frozen=True)
class Skjemafelt:
    nokkel: str                 # = name i skjemaet
    label: str                  # standard label
    kategori: str
    synlig: bool                # standard synlighet (for BETINGET/SENSITIV: gjelder kun naar betingelsen er oppfylt)
    obligatorisk: bool          # standard obligatorisk
    overstyrbart: frozenset     # egenskaper som kan overstyres per kurs (tom = helt laast)
    rekkefolge: int | None = None   # standardplass i «Om deg» (None = fast plassering)
    hjelpetekst: str | None = None  # standard hjelpetekst (None = ingen)
    type: str = "tekst"         # hvordan feltet vises: tekst, langtekst eller avkrysning (input-type: se kurs.html)
    betaler: str | None = None  # FAKTURA: vises naar deltakeren betaler selv (PERSON) eller firma betaler (ORGANISASJON)


@dataclass(frozen=True)
class Systemblokk:
    nokkel: str
    felt: tuple                 # name-ene blokken sender inn (server-side haandtering i app.kursside)
    beskrivelse: str


# ============================ register ============================

_FORNAVN = Skjemafelt("fornavn", "Fornavn", LAAST, True, True, frozenset())
_ETTERNAVN = Skjemafelt("etternavn", "Etternavn", LAAST, True, True, frozenset())
_EPOST = Skjemafelt("epost", "E-post", LAAST, True, True, frozenset())
_SAMTYKKE = Skjemafelt("samtykke", "Samtykke til vilkår og personvernerklæring", LAAST, True, True, frozenset())
# Deltakerens private adresse: alltid synlig og alltid krav. Utenlandske adresser er lov, saa postnummeret kontrolleres
# bare for at det er fylt inn og ikke for langt (valider_adresse) - hjelpeteksten sier hva som gjelder i Norge.
_ADRESSE = Skjemafelt("adresse", "Adresse", LAAST, True, True, frozenset())
_POSTNR = Skjemafelt("postnr", "Postnummer", LAAST, True, True, frozenset(), hjelpetekst="4 siffer i Norge.")
_POSTSTED = Skjemafelt("poststed", "Poststed", LAAST, True, True, frozenset())
_TELEFON = Skjemafelt("telefon", "Telefon", KONFIGURERBAR, True, False, EGENSKAPER, rekkefolge=1)
_ARBEIDSSTED = Skjemafelt("arbeidssted", "Arbeidssted", KONFIGURERBAR, True, False, EGENSKAPER, rekkefolge=2)
_YRKESTITTEL = Skjemafelt("yrkestittel", "Yrkestittel", KONFIGURERBAR, True, False, EGENSKAPER, rekkefolge=3)
_HPR = Skjemafelt("hpr_nr", "HPR-nummer", BETINGET, True, False, frozenset({HJELPETEKST}))
_FAKTURA_REF = Skjemafelt("faktura_ref", "Faktura merkes med", FAKTURA, True, False, _TEKSTFELT,
                          hjelpetekst="NB! Bruk bare tall for ressurs nr. / merida nr. / avdelings nr.",
                          betaler=ORGANISASJON)
_FAKTURA_EPOST = Skjemafelt("faktura_epost", "E-post for faktura", FAKTURA, True, False, _TEKSTFELT,
                            betaler=ORGANISASJON)
_FAKTURA_KOMMENTAR = Skjemafelt("faktura_kommentar", "Kommentar til faktura", FAKTURA, True, False, _TEKSTFELT,
                                hjelpetekst="Legg inn eventuelle kommentarer her angående arbeidsgivers "
                                            "betalingsinformasjon.", betaler=ORGANISASJON)
_EHF = Skjemafelt("ehf", "Elektronisk faktura", FAKTURA, False, False, _UTEN_KRAV, type="avkrysning",
                  betaler=ORGANISASJON)
_ALLERGIER = Skjemafelt("allergier", "Allergier eller spesialkost", SENSITIV, True, False, _UTEN_KRAV,
                        hjelpetekst="Har du allergier eller intoleranser? Vi forhåndsbestiller mat og trenger derfor å "
                                    "vite om dette.", type="langtekst")
_TILRETTELEGGING = Skjemafelt("tilrettelegging", "Behov for tilrettelegging", SENSITIV, True, False, _UTEN_KRAV,
                              type="langtekst")
_INFO = tuple(Skjemafelt(n, label, INFO, True, False, frozenset({SYNLIG})) for n, label in (
    ("vis_tid", "Dato og klokkeslett"), ("vis_sted", "Sted"), ("vis_pris", "Pris"), ("vis_frist", "Påmeldingsfrist")))

# Alle gyldige standardfelt. Uforanderlig: det finnes ingen maate aa legge til et STANDARDFELT uten aa endre koden
# (kursets egne felt ligger i tabellen kurs_ekstrafelt - se kurs/ekstrafelt.py).
REGISTER = MappingProxyType({f.nokkel: f for f in (
    _FORNAVN, _ETTERNAVN, _EPOST, _ADRESSE, _POSTNR, _POSTSTED, _TELEFON, _ARBEIDSSTED, _YRKESTITTEL, _HPR,
    _FAKTURA_REF, _FAKTURA_EPOST, _FAKTURA_KOMMENTAR, _EHF, _ALLERGIER, _TILRETTELEGGING, *_INFO, _SAMTYKKE)})

# Standardrekkefolgen i «Om deg» (rett etter e-post). Kursets egne felt i «Om deg» sorteres inn blant disse. HPR staar
# alltid fast etter gruppen.
KONFIGURERBAR_GRUPPE = ("telefon", "arbeidssted", "yrkestittel")
ADRESSEFELT = ("adresse", "postnr", "poststed")      # deltakerens private adresse (LAAST, rett etter e-post)
FAKTURAFELT = ("faktura_ref", "faktura_epost", "faktura_kommentar", "ehf")
# Felt som fantes i tidligere utgaver og er FJERNET: den private fakturaadressen er nå deltakerens egen adresse (ADRESSEFELT),
# og «Ledige plasser» (vis_plasser) er ikke lenger et valg: antall ledige plasser vises aldri for deltakerne (brukerens
# beslutning 29.09.2026), bare at kurset er fullt. Gamle overstyringer i kurs_skjemafelt for dem leses uten feil og uten
# advarsel, og gjelder ikke lenger. Radene slettes ikke (ingen sletting av data uten avtale), de ignoreres bare.
AVVIKLEDE_FELT = frozenset({"faktura_adresse", "faktura_postnr", "faktura_sted", "vis_plasser"})
SENSITIVE_FELT = ("allergier", "tilrettelegging")
INFOFELT = tuple(f.nokkel for f in _INFO)
INFO_STANDARD = frozenset(INFOFELT)

FAKTURABLOKK = Systemblokk(
    "faktura",
    ("betaler", "org_nr", "org_navn", "org_adresse", "org_postnr", "org_sted", "betaling"),
    "Hvem betaler, organisasjonsnummer (firmanavn og adresse hentes fra Enhetsregisteret) og betalingsmåte - styres "
    "av kursets faktureringsoppsett. Fakturafeltene (FAKTURA i registeret) kan tilpasses per kurs.")


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

_KONTROLL = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]")            # alt, inkl. linjeskift og tab
_KONTROLL_UTEN_LF = re.compile(r"[\x00-\x09\x0b-\x1f\x7f\x85\u2028\u2029]")


MAKS_ADRESSE, MAKS_POSTNR, MAKS_POSTSTED = 200, 20, 100
_ADRESSE_MAKS = {"adresse": MAKS_ADRESSE, "postnr": MAKS_POSTNR, "poststed": MAKS_POSTSTED}


def rens_adresse(verdier) -> dict:
    """{adresse, postnr, poststed} -> rensede verdier: mellomrom foran og bak fjernet, tom eller manglende = None.
    `verdier`: mapping (skjemaet, en JSON-nyttelast, en CSV-rad eller en deltakerrad). Andre nokler roeres ikke."""
    ut = {}
    for n in ADRESSEFELT:
        v = verdier.get(n) if hasattr(verdier, "get") else None
        ut[n] = (v.strip() or None) if isinstance(v, str) else None
    return ut


def valider_adresse(verdier, felt=ADRESSEFELT) -> dict:
    """Den ENE regelen for deltakerens private adresse, brukt av alle veier inn (skjemaet, bedriftspaamelding, manuell
    registrering, CSV-import, webhook og redigering i deltakervinduet): {felt: norsk feilmelding} for de av `felt` som er
    tomme, for lange eller har kontrolltegn - tom dict naar alt er i orden. Meldingene inneholder aldri verdien.

    Bevisst ikke strengere: utenlandske adresser er lov, saa postnummeret maa bare vaere fylt inn (ikke nøyaktig 4
    siffer). Skjemaet sier «4 siffer i Norge» i hjelpeteksten."""
    feil = {}
    for n, v in rens_adresse(verdier).items():
        if n not in felt:
            continue
        label = REGISTER[n].label
        if v is None:
            feil[n] = f"Fyll inn «{label}»."
        elif len(v) > _ADRESSE_MAKS[n]:
            feil[n] = f"«{label}» kan være maks {_ADRESSE_MAKS[n]} tegn."
        elif _KONTROLL.search(v):
            feil[n] = f"«{label}» inneholder ugyldige tegn."
    return feil


def valider_adresse_valgfri(verdier) -> dict:
    """Adressen er VALGFRI (bare i CSV-import når noedbremsen er satt til 0, se config.ADRESSE_KREVES_I_CSV, som er PÅ som standard):
    er ingen av de tre feltene fylt ut, er alt i orden (tom dict). Er noen fylt ut, kreves alle tre og valideres som valider_adresse
    (delvis adresse = feil). Tomme og bare-mellomrom-felt teller som ikke utfylt."""
    if all(v is None for v in rens_adresse(verdier).values()):
        return {}
    return valider_adresse(verdier)


def valider_adresse_nodbrems(verdier) -> dict:
    """Webhookens NØDBREMS (config.ADRESSE_KREVES_I_WEBHOOK=0, av som standard): påmeldingen skal ikke gå tapt fordi adressen
    mangler. Ingen av de tre feltene er påkrevd, heller ikke alle tre samlet: en manglende eller delvis adresse tas inn, og personen
    merkes «Privat adresse mangler» (adresse_mangler) mens fakturaen holdes tilbake (privatadresse.py). Det som ER sendt, må likevel
    være gyldig (ikke for langt, ingen kontrolltegn). Tom dict = alt i orden."""
    sendt = {n for n, v in rens_adresse(verdier).items() if v is not None}
    return {n: m for n, m in valider_adresse(verdier).items() if n in sendt}


ADRESSE_MANGLER_MERKE = "Privat adresse mangler"      # merket i deltakerlisten og varselboksen i deltakervinduet: ingen del av adressen er lagt inn
ADRESSE_UFULLSTENDIG_MERKE = "Privat adresse er ufullstendig"      # samme steder: noen, men ikke alle tre feltene er lagt inn


def adresse_status(person) -> str:
    """'komplett' (adresse, postnr og poststed er alle fylt ut), 'ufullstendig' (noen, men ikke alle) eller 'mangler' (ingen).
    `person` som i adresse_mangler. En delvis adresse tas vare på (brukerens beslutning 01.10.2026), men gjelder aldri som adresse:
    fakturaen holdes tilbake til alle tre er komplette (privatadresse.py)."""
    fylt = sum(1 for k in ADRESSEFELT if isinstance(person[k], str) and person[k].strip())
    return "komplett" if fylt == len(ADRESSEFELT) else ("ufullstendig" if fylt else "mangler")


def adresse_merke(person) -> str | None:
    """Teksten på merket for personen: «Privat adresse mangler», «Privat adresse er ufullstendig», eller None når adressen er komplett."""
    return {"komplett": None, "ufullstendig": ADRESSE_UFULLSTENDIG_MERKE, "mangler": ADRESSE_MANGLER_MERKE}[adresse_status(person)]


def adresse_mangler(person) -> bool:
    """Mangler personen en komplett privat adresse (adresse, postnr og poststed må alle være fylt ut)? `person` er en
    deltakerrad, en påmeldingsrad med adressekolonnene eller en dict. Merket «Privat adresse mangler» følger av dataene (ingen egen
    kolonne): det forsvinner av seg selv når adressen er lagt inn."""
    return not all(isinstance(person[k], str) and person[k].strip() for k in ADRESSEFELT)


def _valider_label(verdi, felt: str) -> str | None:
    if not isinstance(verdi, str):
        raise SkjemafeltFeil(UGYLDIG_TYPE, felt, LABEL, "Feltnavnet må være tekst.")
    if _KONTROLL.search(verdi):
        raise SkjemafeltFeil(KONTROLLTEGN, felt, LABEL, "Feltnavnet må være én linje uten spesialtegn.")
    ren = verdi.strip()
    if len(ren) > MAKS_LABEL:
        raise SkjemafeltFeil(FOR_LANG, felt, LABEL, f"Feltnavnet kan være maks {MAKS_LABEL} tegn (er nå {len(ren)}).")
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
    v = _valider_hjelpetekst(verdi, f.nokkel)
    if f.hjelpetekst is None:
        return v                                    # ingen standard hjelpetekst: kun tom -> None
    if v is None:
        return ""                                   # standard har hjelpetekst: tom = bevisst ingen hjelpetekst
    return None if v == f.hjelpetekst else v


# ============================ overstyringsmodell ============================

@dataclass(frozen=True)
class Overstyring:
    """Gyldige, normaliserte avvik for ETT felt. None = kodet standard for den egenskapen. hjelpetekst '' = ingen
    hjelpetekst (kun for felt som har standard hjelpetekst)."""
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
        if isinstance(felt, str) and felt in AVVIKLEDE_FELT:
            continue                        # avviklet valg: ignoreres helt, uten advarsel
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
    nokkel: str                 # name i skjemaet: registernokkelen, eller 'ekstra_<id>' for kursets egne felt
    label: str
    obligatorisk: bool
    hjelpetekst: str | None = None
    type: str = "tekst"         # standardfelt: se Skjemafelt.type. Egne felt: typen fra ekstrafelt.TYPER
    valg: tuple = ()            # egne felt: svaralternativene (avkrysning: ev. teksten ved boksen)
    vis_naar: tuple | None = None   # egne felt: (name, verdi) - vises bare naar feltet `name` har verdien
    ekstra_id: int | None = None    # satt for kursets egne felt


@dataclass(frozen=True)
class EffektivtSkjema:
    deltakerfelt: tuple         # «Om deg»: synlige felt etter e-post i rekkefolge (standardfelt og egne), ev. HPR sist
    vis_fakturablokk: bool
    vis_sensitive_felt: bool    # minst ett av allergier/tilrettelegging vises (aldri paa digitale kurs)
    adressefelt: tuple = ()     # deltakerens private adresse: adresse, postnr, poststed (alltid med, alltid krav), rett etter e-post
    firmafelt: tuple = ()       # fakturablokken naar firma betaler (etter organisasjonsnummer og firmaopplysningene)
    sluttfelt: tuple = ()       # kursets egne felt «til slutt» (foer allergier og samtykke)
    sensitive_felt: tuple = ()
    nederstfelt: tuple = ()     # kursets egne felt «nederst» (etter samtykket, rett over Meld på-knappen)
    info: frozenset = INFO_STANDARD     # det informasjonsboksen viser

    @property
    def egne_felt(self) -> tuple:
        """Kursets egne felt i skjemaet, i visningsrekkefolge."""
        return tuple(f for f in (*self.deltakerfelt, *self.sluttfelt, *self.nederstfelt) if f.ekstra_id is not None)


# HPR-nummer samles ikke inn lenger (Camilla 02.10.2026: «ta bort HPR-nummer»). Feltet, lagringen og alt som finnes fra før er beholdt (deltakervinduet,
# importen, søket og eksisterende opplysninger), men verken det offentlige skjemaet, bedriftspåmeldingen eller skjemabyggeren viser det, selv om kurset er i
# et spesialistløp. Settes denne til True, virker alt som før: HPR vises bare når kurset er i et spesialistløp.
HPR_I_SKJEMA = False


def vis_hpr(kurs) -> bool:
    return HPR_I_SKJEMA and bool(kurs["spesialistlop"])


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
                         avvik.get(HJELPETEKST, f.hjelpetekst) or None, f.type)


def _som_overstyringer(overstyringer) -> dict:
    if isinstance(overstyringer, Leseresultat):
        overstyringer = overstyringer.overstyringer
    return overstyringer or {}


def plassrekkefolge(overstyringer, ekstrafelt, plassering: str) -> list[str]:
    """Nokklene i en del av skjemaet i den rekkefolgen deltakeren ser dem - OGSAA skjulte felt (til skjemabyggeren).
    OM_DEG: standardfeltene i KONFIGURERBAR_GRUPPE ('telefon' ...) og kursets egne felt ('ekstra:<id>'); TIL_SLUTT:
    kun egne felt. Stabil sortering paa (plass, standardfelt foer egne felt, standardindeks/id)."""
    overstyringer = _som_overstyringer(overstyringer)
    elementer = []
    if plassering == OM_DEG:
        for indeks, n in enumerate(KONFIGURERBAR_GRUPPE):
            avvik = _gyldige_avvik(REGISTER[n], overstyringer.get(n))
            elementer.append(((avvik.get(REKKEFOLGE, REGISTER[n].rekkefolge), 0, indeks), n))
    elementer += [((e.rekkefolge, 1, e.id), f"{EKSTRA_REF}{e.id}") for e in ekstrafelt if e.plassering == plassering]
    return [n for _, n in sorted(elementer, key=lambda x: x[0])]


def neste_plass(overstyringer, ekstrafelt, plassering: str) -> int:
    """Plassen et nytt eget felt faar: sist i sin del av skjemaet (etter standardfeltene og de andre egne feltene der)."""
    overstyringer = _som_overstyringer(overstyringer)
    plasser = [e.rekkefolge for e in ekstrafelt if e.plassering == plassering]
    if plassering == OM_DEG:
        plasser += [_gyldige_avvik(REGISTER[n], overstyringer.get(n)).get(REKKEFOLGE, REGISTER[n].rekkefolge)
                    for n in KONFIGURERBAR_GRUPPE]
    return max(plasser, default=0) + 1


def _egne_i_skjemaet(ekstrafelt, vis_blokk: bool) -> dict:
    """{'ekstra:<id>': EffektivtFelt} for kursets egne felt som faktisk kan vises i dette skjemaet: synlige, og med en
    gyldig betingelse som kan oppfylles her. Feltet som styrer, maa selv vises og vaere uten betingelse (ett nivaa);
    «Betale privat / firma» finnes bare naar fakturablokken vises."""
    synlige = [e for e in ekstrafelt if e.synlig and e.betingelse_ok]
    styrende = {f"{EKSTRA_REF}{e.id}" for e in synlige if e.vis_naar_felt is None}
    ut = {}
    for e in synlige:
        if e.vis_naar_felt is None:
            vis_naar = None
        elif e.vis_naar_felt == BETALER and vis_blokk:
            vis_naar = (BETALER, e.vis_naar_verdi)
        elif e.vis_naar_felt in styrende:
            vis_naar = ("ekstra_" + e.vis_naar_felt[len(EKSTRA_REF):], e.vis_naar_verdi)
        else:
            continue
        ut[f"{EKSTRA_REF}{e.id}"] = EffektivtFelt(f"ekstra_{e.id}", e.label, e.obligatorisk, e.hjelpetekst, e.type,
                                                   e.valg, vis_naar, e.id)
    return ut


def effektivt_skjema(kurs, overstyringer=None, ekstrafelt=()) -> EffektivtSkjema:
    """Det skjemaet deltakeren faktisk faar for dette kurset. Ren funksjon: leser KUN kursraden, de (allerede leste)
    overstyringene og kursets (allerede leste og validerte) egne felt - aldri databasen. Uten overstyringer og egne
    felt: kodet standard.

    HPR, fakturablokken og de sensitive feltene folger ALLTID kursdata - ingen overstyring kan vise dem der kursdata
    sier nei, gjoere HPR eller et sensitivt felt obligatorisk, eller flytte dem. Adressefeltene er alltid med.

    `overstyringer`: {felt: Overstyring} eller et Leseresultat (da brukes kun de gyldige overstyringene).
    `ekstrafelt`: kursets egne felt (ekstrafelt.Ekstrafelt), f.eks. ekstrafelt.Leseresultat.felt."""
    overstyringer = _som_overstyringer(overstyringer)
    avvik = {n: _gyldige_avvik(f, overstyringer.get(n)) for n, f in REGISTER.items()}

    def synlig(n: str) -> bool:
        return avvik[n].get(SYNLIG, REGISTER[n].synlig)

    blokk = vis_fakturablokk(kurs)
    egne = _egne_i_skjemaet(ekstrafelt, blokk)

    def del_av_skjemaet(plassering: str) -> list:
        ut = []
        for n in plassrekkefolge(overstyringer, ekstrafelt, plassering):
            if n in egne:
                ut.append(egne[n])
            elif n in REGISTER and synlig(n):
                ut.append(_effektivt(REGISTER[n], avvik[n]))
        return ut

    om_deg = del_av_skjemaet(OM_DEG)
    if vis_hpr(kurs):
        om_deg.append(_effektivt(_HPR, avvik["hpr_nr"]))

    def fakturafelt(betaler: str) -> tuple:
        if not blokk:
            return ()
        return tuple(_effektivt(REGISTER[n], avvik[n]) for n in FAKTURAFELT
                     if REGISTER[n].betaler == betaler and synlig(n))

    sensitive = tuple(_effektivt(REGISTER[n], avvik[n]) for n in SENSITIVE_FELT
                      if synlig(n)) if vis_sensitive_felt(kurs) else ()
    adresse = tuple(_effektivt(REGISTER[n], {}) for n in ADRESSEFELT)      # laast: ingen overstyring kan endre dem
    return EffektivtSkjema(tuple(om_deg), blokk, bool(sensitive), adresse, fakturafelt(ORGANISASJON),
                           tuple(del_av_skjemaet(TIL_SLUTT)), sensitive,
                           nederstfelt=tuple(del_av_skjemaet(NEDERST)),
                           info=frozenset(n for n in INFOFELT if synlig(n)))
