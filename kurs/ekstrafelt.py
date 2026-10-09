"""Kursets egne felt i paameldingsskjemaet (skjemabyggeren, migrering 9): modell, streng validering foer lagring,
defensiv lesing og tolking av deltakerens svar.

Standardfeltene (telefon, fakturafeltene, allergier osv.) ligger i kode-registeret i kurs/skjemafelt.py. Her er feltene
admin lager selv per kurs (tabellen kurs_ekstrafelt), og svarene paa dem (tabellen paamelding_svar).

Prinsipper (samme som skjemafelt.py):
  * Ren modul: ingen database, ingen Flask. SQL ligger i db.py, som bruker valideringen her FOER skriving.
  * Et eget felt har en TYPE (TYPER), et feltnavn, ev. hjelpetekst og svaralternativer, krav, vis, plassering («Om
    deg» eller «Til slutt»), rekkefolge og ev. «vis bare naar» et annet felt har en bestemt verdi.
  * «Vis bare naar» har ETT nivaa: feltet som styrer, er «Betale privat / firma» eller et av kursets egne felt med
    svaralternativer som selv vises alltid. Ingen kjeder og ingen sirkler.
  * All tekst er REN TEKST - Jinja escaper ved rendring. Svarene er ren tekst; flervalg lagres som JSON-liste.
  * LAGRING er streng (EkstrafeltFeil med forklaring til admin). LESING er defensiv: en ugyldig rad gir en PII-fri
    Advarsel og vises ikke. Er bare betingelsen ugyldig, vises feltet heller ikke (betingelse_ok=False) - ellers kunne
    et krav bli stilt til deltakere det ikke gjelder.
  * Aldri sensitive opplysninger i egne felt (admin faar beskjed om det i skjemabyggeren): allergier og tilrettelegging
    har egne felt som lagres i tabellen sensitivt og slettes automatisk.
"""
import json
import re
from dataclasses import dataclass, replace
from types import MappingProxyType

from . import skjemafelt
from .skjemafelt import BETALER, EKSTRA_REF, NEDERST, OM_DEG, ORGANISASJON, PERSON, TIL_SLUTT

TYPER = MappingProxyType({"tekst": "Kort tekst", "langtekst": "Lang tekst", "avkrysning": "Avkrysning",
                          "envalg": "Envalg", "flervalg": "Flervalg", "nedtrekk": "Nedtrekksliste"})
MED_VALG = frozenset({"envalg", "flervalg", "nedtrekk"})       # svaralternativene skrives av admin
KAN_STYRE = frozenset({"avkrysning", *MED_VALG})              # kan brukes i «vis bare naar»
PLASSERINGER = MappingProxyType({OM_DEG: "Om deg", TIL_SLUTT: "Til slutt", NEDERST: "Nederst (over Meld på-knappen)"})
BETALER_NAVN = "Betale privat / firma"
BETALER_VALG = MappingProxyType({PERSON: "Betale privat", ORGANISASJON: "Firma betaler"})
AVKRYSSET = "Ja"                    # verdien en avkrysset boks sender (og lagres som)

MAKS_LABEL = skjemafelt.MAKS_LABEL
MAKS_HJELPETEKST = 500              # egne felt baerer ofte samtykketekster (f.eks. «Deling av e-post»)
MAKS_VALG, MAKS_VALGTEKST = 30, 120
MAKS_SVAR = MappingProxyType({"tekst": 300, "langtekst": 2000})
MAKS_FELT = 40                      # per kurs

# Grunnkoder (PII-frie, trygge aa logge)
UKJENT_TYPE = "ukjent_type"
UGYLDIG_TYPE = "ugyldig_type"
MANGLER = "mangler"
FOR_LANG = "for_lang"
KONTROLLTEGN = "kontrolltegn"
UGYLDIGE_VALG = "ugyldige_valg"
UKJENT_PLASSERING = "ukjent_plassering"
UGYLDIG_BETINGELSE = "ugyldig_betingelse"
FOR_MANGE = "for_mange"
UKJENT_FELT = "ukjent_felt"
HAR_SVAR = "har_svar"
STYRER_ANDRE = "styrer_andre"

_EKSTRA_REF = re.compile(rf"{re.escape(EKSTRA_REF)}([1-9][0-9]{{0,17}})")


class EkstrafeltFeil(Exception):
    """Avvist lagring. str(e) inneholder KUN grunnkode, felt-id og egenskap - aldri tekst admin har skrevet.
    `forklaring` er norsk tekst til admin (kan nevne feltnavn) - skal ikke logges."""

    def __init__(self, grunn: str, egenskap: str | None = None, forklaring: str = "", felt_id: int | None = None):
        super().__init__(grunn, egenskap, felt_id)
        self.grunn, self.egenskap, self.forklaring, self.felt_id = grunn, egenskap, forklaring, felt_id

    def __str__(self) -> str:
        return f"{self.grunn} (ekstra:{self.felt_id or '?'}.{self.egenskap or '?'})"


@dataclass(frozen=True)
class Advarsel:
    """PII-fri beskjed om at en lagret rad (eller del av den) ble ignorert ved lesing."""
    grunn: str
    felt_id: int | None = None
    egenskap: str | None = None


@dataclass(frozen=True)
class Ekstrafelt:
    id: int
    type: str
    label: str
    hjelpetekst: str | None = None
    valg: tuple = ()                # MED_VALG: alternativene. avkrysning: ev. teksten ved boksen (ellers «Ja»)
    obligatorisk: bool = False
    synlig: bool = True
    plassering: str = OM_DEG
    rekkefolge: int = 0
    vis_naar_felt: str | None = None    # BETALER eller 'ekstra:<id>'
    vis_naar_verdi: str | None = None
    betingelse_ok: bool = True      # False: lagret betingelse er ugyldig -> feltet vises ikke (se les())

    @property
    def nokkel(self) -> str:
        """name i paameldingsskjemaet."""
        return f"ekstra_{self.id}"

    @property
    def referanse(self) -> str:
        """Slik andre felt viser til dette i vis_naar_felt."""
        return f"{EKSTRA_REF}{self.id}"

    @property
    def verdier(self) -> tuple:
        """Verdiene et svar kan ha - de et annet felt kan vises etter."""
        if self.type == "avkrysning":
            return (AVKRYSSET,)
        return self.valg if self.type in MED_VALG else ()

    @property
    def avkrysningstekst(self) -> str:
        return self.valg[0] if self.valg else AVKRYSSET


@dataclass(frozen=True)
class Leseresultat:
    felt: tuple                     # Ekstrafelt i id-rekkefolge (ogsaa skjulte og de med ugyldig betingelse)
    advarsler: tuple                # Advarsel, PII-frie

    @property
    def har_advarsler(self) -> bool:
        return bool(self.advarsler)

    def per_id(self) -> dict:
        return {e.id: e for e in self.felt}


# ============================ validering av enkeltverdier ============================

def _en_linje(verdi, egenskap: str, navn: str, maks: int, felt_id=None, *, paakrevd: bool = False) -> str | None:
    if not isinstance(verdi, str):
        raise EkstrafeltFeil(UGYLDIG_TYPE, egenskap, f"{navn} må være tekst.", felt_id)
    if skjemafelt._KONTROLL.search(verdi):
        raise EkstrafeltFeil(KONTROLLTEGN, egenskap, f"{navn} må være én linje uten spesialtegn.", felt_id)
    ren = verdi.strip()
    if len(ren) > maks:
        raise EkstrafeltFeil(FOR_LANG, egenskap, f"{navn} kan være maks {maks} tegn (er nå {len(ren)}).", felt_id)
    if paakrevd and not ren:
        raise EkstrafeltFeil(MANGLER, egenskap, f"{navn} må fylles ut.", felt_id)
    return ren or None


def _hjelpetekst(verdi, felt_id=None) -> str | None:
    if verdi is None:
        return None
    if not isinstance(verdi, str):
        raise EkstrafeltFeil(UGYLDIG_TYPE, "hjelpetekst", "Hjelpeteksten må være tekst.", felt_id)
    t = verdi.replace("\r\n", "\n").replace("\r", "\n")
    if skjemafelt._KONTROLL_UTEN_LF.search(t):
        raise EkstrafeltFeil(KONTROLLTEGN, "hjelpetekst", "Hjelpeteksten inneholder ugyldige tegn.", felt_id)
    ren = t.strip()
    if len(ren) > MAKS_HJELPETEKST:
        raise EkstrafeltFeil(FOR_LANG, "hjelpetekst",
                             f"Hjelpeteksten kan være maks {MAKS_HJELPETEKST} tegn (er nå {len(ren)}).", felt_id)
    return ren or None


def _valg(type_: str, verdi, felt_id=None) -> tuple:
    """Svaralternativene: en liste, eller tekst med ett alternativ per linje (fra skjemabyggeren). Tomme linjer
    hoppes over. Tekst- og langtekstfelt har ingen alternativer (det som sendes, ignoreres)."""
    if type_ not in KAN_STYRE:
        return ()
    if verdi is None:
        linjer = []
    elif isinstance(verdi, str):
        linjer = verdi.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    elif isinstance(verdi, (list, tuple)):
        linjer = list(verdi)
    else:
        raise EkstrafeltFeil(UGYLDIG_TYPE, "valg", "Svaralternativene må være tekst.", felt_id)
    ut, sett = [], set()
    for linje in linjer:
        v = _en_linje(linje, "valg", "Hvert svaralternativ", MAKS_VALGTEKST, felt_id)
        if v is None:
            continue
        if v.casefold() in sett:
            raise EkstrafeltFeil(UGYLDIGE_VALG, "valg", f"Svaralternativet «{v}» står to ganger.", felt_id)
        sett.add(v.casefold())
        ut.append(v)
    if type_ == "avkrysning":
        if len(ut) > 1:
            raise EkstrafeltFeil(UGYLDIGE_VALG, "valg",
                                 "En avkrysningsboks har bare én tekst ved boksen (skriv den på én linje).", felt_id)
        return tuple(ut)
    minst = 1 if type_ == "flervalg" else 2
    if len(ut) < minst:
        raise EkstrafeltFeil(UGYLDIGE_VALG, "valg", "Skriv minst ett svaralternativ (ett per linje)." if minst == 1
                             else "Skriv minst to svaralternativer (ett per linje).", felt_id)
    if len(ut) > MAKS_VALG:
        raise EkstrafeltFeil(UGYLDIGE_VALG, "valg", f"Maks {MAKS_VALG} svaralternativer.", felt_id)
    return tuple(ut)


def _bool(verdi, egenskap: str, felt_id=None) -> bool:
    if isinstance(verdi, bool):
        return verdi
    if type(verdi) is int and verdi in (0, 1):      # fra databasen (CHECK 0/1)
        return bool(verdi)
    raise EkstrafeltFeil(UGYLDIG_TYPE, egenskap, "Verdien må være ja eller nei.", felt_id)


def _betingelse(felt, verdi, felt_id=None) -> tuple:
    """(vis_naar_felt, vis_naar_verdi) med riktig form - begge None, eller BETALER/'ekstra:<id>' og en verdi. Om
    feltet og verdien finnes, kontrolleres mot hele settet i kontroller_betingelser()."""
    if felt is None and verdi is None:
        return None, None
    if not (isinstance(felt, str) and isinstance(verdi, str)) or not (felt == BETALER or _EKSTRA_REF.fullmatch(felt)):
        raise EkstrafeltFeil(UGYLDIG_BETINGELSE, "vis_naar", "Ugyldig «Vis bare når».", felt_id)
    return felt, _en_linje(verdi, "vis_naar", "Verdien i «Vis bare når»", MAKS_VALGTEKST, felt_id, paakrevd=True)


def normaliser(data: dict, felt_id: int | None = None) -> dict:
    """STRENG validering av ETT felt foer lagring. `data` har type, label, hjelpetekst, valg, obligatorisk, synlig,
    plassering, vis_naar_felt og vis_naar_verdi (utelatt: standardverdi). Om betingelsen passer med de ANDRE feltene,
    kontrolleres av kontroller_betingelser(). Returnerer normaliserte verdier (valg som tuple). Kaster EkstrafeltFeil."""
    type_ = data.get("type")
    if type_ not in TYPER:
        raise EkstrafeltFeil(UKJENT_TYPE, "type", "Velg en gyldig felttype.", felt_id)
    plassering = data.get("plassering", OM_DEG)
    if plassering not in PLASSERINGER:
        raise EkstrafeltFeil(UKJENT_PLASSERING, "plassering", "Velg hvor i skjemaet feltet skal stå.", felt_id)
    vis_naar_felt, vis_naar_verdi = _betingelse(data.get("vis_naar_felt"), data.get("vis_naar_verdi"), felt_id)
    return {
        "type": type_,
        "label": _en_linje(data.get("label"), "label", "Feltnavnet", MAKS_LABEL, felt_id, paakrevd=True),
        "hjelpetekst": _hjelpetekst(data.get("hjelpetekst"), felt_id),
        "valg": _valg(type_, data.get("valg"), felt_id),
        "obligatorisk": _bool(data.get("obligatorisk", False), "obligatorisk", felt_id),
        "synlig": _bool(data.get("synlig", True), "synlig", felt_id),
        "plassering": plassering,
        "vis_naar_felt": vis_naar_felt,
        "vis_naar_verdi": vis_naar_verdi,
    }


def som_felt(felt_id: int, verdier: dict, rekkefolge: int = 0) -> Ekstrafelt:
    """Et Ekstrafelt av normaliserte verdier (fra normaliser()), f.eks. for aa kontrollere betingelsene foer lagring."""
    return Ekstrafelt(felt_id, verdier["type"], verdier["label"], verdier["hjelpetekst"], tuple(verdier["valg"]),
                      verdier["obligatorisk"], verdier["synlig"], verdier["plassering"], rekkefolge,
                      verdier["vis_naar_felt"], verdier["vis_naar_verdi"])


def _styrende_id(referanse: str) -> int | None:
    m = _EKSTRA_REF.fullmatch(referanse or "")
    return int(m.group(1)) if m else None


def betingelsesfeil(e: Ekstrafelt, alle: dict) -> str | None:
    """Hvorfor betingelsen til `e` ikke kan brukes, som norsk tekst til admin - eller None naar den er gyldig.
    `alle`: {id: Ekstrafelt} for ALLE kursets egne felt (etter endringen)."""
    if e.vis_naar_felt is None:
        return None
    if e.vis_naar_felt == BETALER:
        return None if e.vis_naar_verdi in BETALER_VALG else f"«{BETALER_NAVN}» har ikke den verdien."
    styrer = alle.get(_styrende_id(e.vis_naar_felt))
    if styrer is None:
        return "Feltet det skal vises etter, finnes ikke lenger."
    if styrer.id == e.id:
        return "Et felt kan ikke vises etter sitt eget svar."
    if styrer.vis_naar_felt is not None:
        return (f"«{styrer.label}» vises selv bare av og til, og kan derfor ikke styre andre felt "
                "(bare ett nivå er mulig).")
    if styrer.type not in KAN_STYRE:
        return f"«{styrer.label}» har ingen svaralternativer å vise etter."
    if e.vis_naar_verdi not in styrer.verdier:
        return f"«{styrer.label}» har ikke svaralternativet «{e.vis_naar_verdi}»."
    return None


def kontroller_betingelser(alle: dict) -> None:
    """Kontrollerer «vis bare naar» for HELE settet (etter endringen). EkstrafeltFeil ved foerste feil."""
    for e in sorted(alle.values(), key=lambda x: x.id):
        feil = betingelsesfeil(e, alle)
        if feil:
            raise EkstrafeltFeil(UGYLDIG_BETINGELSE, "vis_naar", f"«{e.label}»: {feil}", e.id)


# ============================ defensiv lesing ============================

def _fra_rad(rad) -> tuple[Ekstrafelt, list]:
    """Ett Ekstrafelt fra en raa rad. Kaster EkstrafeltFeil naar raden ikke kan vises; en ugyldig hjelpetekst gir bare
    en advarsel (feltet vises uten den)."""
    felt_id = rad["id"]
    advarsler = []
    type_ = rad["type"]
    if type_ not in TYPER:
        raise EkstrafeltFeil(UKJENT_TYPE, "type", felt_id=felt_id)
    if type_ in KAN_STYRE:
        try:
            valg = json.loads(rad["valg"]) if rad["valg"] else []
        except (TypeError, ValueError) as e:
            raise EkstrafeltFeil(UGYLDIGE_VALG, "valg", felt_id=felt_id) from e
        if not isinstance(valg, list):
            raise EkstrafeltFeil(UGYLDIGE_VALG, "valg", felt_id=felt_id)
    else:
        valg = []
    rekkefolge = rad["rekkefolge"]
    if isinstance(rekkefolge, bool) or not isinstance(rekkefolge, int):
        raise EkstrafeltFeil(UGYLDIG_TYPE, "rekkefolge", felt_id=felt_id)
    try:
        hjelpetekst = _hjelpetekst(rad["hjelpetekst"], felt_id)
    except EkstrafeltFeil as feil:
        hjelpetekst = None
        advarsler.append(Advarsel(feil.grunn, felt_id, "hjelpetekst"))
    try:
        vis_naar = _betingelse(rad["vis_naar_felt"], rad["vis_naar_verdi"], felt_id)
        betingelse_ok = True
    except EkstrafeltFeil:
        vis_naar, betingelse_ok = (None, None), False
    verdier = normaliser({"type": type_, "label": rad["label"], "hjelpetekst": None, "valg": valg,
                          "obligatorisk": rad["obligatorisk"], "synlig": rad["synlig"],
                          "plassering": rad["plassering"]}, felt_id)
    e = som_felt(felt_id, {**verdier, "hjelpetekst": hjelpetekst, "vis_naar_felt": vis_naar[0],
                           "vis_naar_verdi": vis_naar[1]}, rekkefolge)
    if not betingelse_ok:
        advarsler.append(Advarsel(UGYLDIG_BETINGELSE, felt_id, "vis_naar"))
        e = replace(e, betingelse_ok=False)
    return e, advarsler


def les(rader) -> Leseresultat:
    """DEFENSIV tolkning av raa rader fra kurs_ekstrafelt. En rad som ikke kan vises (ukjent type, ugyldig feltnavn
    eller svaralternativer ...) ignoreres med en Advarsel. En ugyldig betingelse - ogsaa en som viser til et felt som
    ikke finnes eller ikke kan styre - gir betingelse_ok=False: feltet vises ikke i skjemaet, men skjemabyggeren viser
    det, slik at admin kan rette det."""
    felt, advarsler = {}, []
    for rad in rader:
        felt_id = rad["id"] if isinstance(rad["id"], int) and not isinstance(rad["id"], bool) else None
        if felt_id is None:
            advarsler.append(Advarsel(UKJENT_FELT))
            continue
        try:
            e, egne_advarsler = _fra_rad(rad)
        except EkstrafeltFeil as feil:
            advarsler.append(Advarsel(feil.grunn, felt_id, feil.egenskap))
            continue
        felt[felt_id] = e
        advarsler += egne_advarsler
    for felt_id, e in list(felt.items()):
        if e.betingelse_ok and betingelsesfeil(e, felt):
            felt[felt_id] = replace(e, betingelse_ok=False)
            advarsler.append(Advarsel(UGYLDIG_BETINGELSE, felt_id, "vis_naar"))
    return Leseresultat(tuple(felt.values()), tuple(advarsler))


# ============================ deltakerens svar ============================

def _renset(tekst: str, flere_linjer: bool) -> str:
    tekst = tekst.replace("\r\n", "\n").replace("\r", "\n")
    if not flere_linjer:
        return " ".join(tekst.split())
    tekst = skjemafelt._KONTROLL_UTEN_LF.sub(" ", tekst)
    return "\n".join(linje.rstrip() for linje in tekst.split("\n")).strip()


def _tolk_ett(felt, raa) -> tuple:
    """(verdi eller None, feilmelding eller None) for ett felt. `raa` er det som ble sendt inn (liste for flervalg)."""
    if felt.type in MAKS_SVAR:
        tekst = _renset(raa, felt.type == "langtekst") if isinstance(raa, str) else ""
        if len(tekst) > MAKS_SVAR[felt.type]:
            return None, f"«{felt.label}» kan være maks {MAKS_SVAR[felt.type]} tegn."
        return tekst or None, None
    if felt.type == "avkrysning":
        return (AVKRYSSET if raa == AVKRYSSET else None), None
    if felt.type == "flervalg":
        valgte = raa if isinstance(raa, list) else [raa] if isinstance(raa, str) else []
        return [v for v in felt.valg if v in valgte] or None, None
    return (raa if isinstance(raa, str) and raa in felt.valg else None), None


def _mangler(felt) -> str:
    if felt.type in MAKS_SVAR:
        return f"Fyll inn «{felt.label}»."
    if felt.type == "avkrysning":
        return f"Kryss av for «{felt.label}»."
    return f"Velg et svar i «{felt.label}»."


def betingelse_oppfylt(vis_naar, verdier: dict) -> bool:
    """Er betingelsen (name, verdi) oppfylt av `verdier` (name -> tekst, eller liste for flervalg)?"""
    if vis_naar is None:
        return True
    navn, verdi = vis_naar
    svar = verdier.get(navn)
    return verdi in svar if isinstance(svar, list) else svar == verdi


def tolk_svar(felter, verdier: dict) -> tuple[dict, dict]:
    """Deltakerens svar paa kursets egne felt -> ({felt_id: lagret tekst}, {name: feilmelding}).

    `felter`: de effektive egne feltene i skjemaet (skjemafelt.EffektivtSkjema.egne_felt). `verdier`: de TILLATTE
    innsendte verdiene (name -> tekst, liste for flervalg), inkludert 'betaler' naar fakturablokken vises. Et felt med
    betingelse som ikke er oppfylt, er ikke aktivt: det lagres ikke og kan ikke vaere obligatorisk. Ukjente
    svaralternativer regnes som ikke besvart. Feilmeldingene kommer i skjemaets rekkefolge (per name)."""
    tolket, svar, feil = {}, {}, {}
    rekkefolge = {f.nokkel: i for i, f in enumerate(felter)}
    for f in sorted(felter, key=lambda x: (x.vis_naar is not None, rekkefolge[x.nokkel])):   # styrende foer avhengige
        # Betingelsen vurderes mot «Betale privat / firma» og de TOLKEDE svarene - et ugyldig raasvar paa feltet som
        # styrer, kan aldri gjoere et avhengig felt aktivt.
        if f.vis_naar is not None and not betingelse_oppfylt(f.vis_naar, {BETALER: verdier.get(BETALER), **tolket}):
            continue
        verdi, melding = _tolk_ett(f, verdier.get(f.nokkel))
        if melding or (verdi is None and f.obligatorisk):
            feil[f.nokkel] = melding or _mangler(f)
            continue
        if verdi is None:
            continue
        tolket[f.nokkel] = verdi
        svar[f.ekstra_id] = json.dumps(verdi, ensure_ascii=False) if isinstance(verdi, list) else verdi
    return svar, {n: feil[n] for n in sorted(feil, key=rekkefolge.get)}


def vis_svar(verdi: str | None) -> str:
    """Et lagret svar slik det vises og eksporteres: flervalg (JSON-liste) som «A, B»."""
    if not verdi:
        return ""
    if verdi.startswith("["):
        try:
            liste = json.loads(verdi)
        except ValueError:
            return verdi
        if isinstance(liste, list) and all(isinstance(v, str) for v in liste):
            return ", ".join(liste)
    return verdi


# ============================ hurtigvalg i skjemabyggeren ============================

MALER = MappingProxyType({
    "land": ("Land", {"type": "tekst", "label": "Land", "plassering": OM_DEG}),
    "kommentar": ("Kommentar", {"type": "langtekst", "label": "Kommentar", "plassering": TIL_SLUTT}),
    "deling_epost": ("Deling av e-post", {
        "type": "envalg", "label": "Deling av e-post", "valg": ("Ja", "Nei"), "obligatorisk": True,
        "plassering": TIL_SLUTT,
        "hjelpetekst": "Jeg samtykker til at IPR deler e-postadressen min med de andre deltakerne på utdanningen. "
                       "Det gjør det lettere å komme i dialog med hverandre, organisere egne veiledningsgrupper og "
                       "andre grupper på sosiale medier."}),
    "nyhetsbrev": ("Nyhetsbrev", {"type": "avkrysning", "label": "Nyhetsbrev",
                                  "valg": ("Ja takk, jeg vil gjerne få nyhetsbrev fra IPR",), "plassering": NEDERST}),
})
