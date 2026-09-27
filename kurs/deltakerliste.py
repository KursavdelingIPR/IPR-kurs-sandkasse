"""Deltakerliste med kolonnevalg: utskrift (og PDF via nettleserens «Lagre som PDF») og CSV.

Personvern (CLAUDE.md regel 1):
  * Standard er bare løpenummer og navn. Kontaktinformasjon, arbeid og faktura må krysses av eksplisitt.
  * Allergier/tilrettelegging (tabellen `sensitivt`) finnes ikke i katalogen og kan aldri velges - de har egen liste
    med tilgangslogg (admin_allergiliste). Ukjente kolonnenavn i adressen ignoreres.
  * Tomme utfyllingskolonner (signatur, merknad) er bare for papir og tas ikke med i CSV.

PDF lages bevisst i nettleseren (utskrift -> «Lagre som PDF»), samme løsning som for kursbevis: ingen tung
PDF-avhengighet i drift. Logo vises bare når en godkjent fil er lagt i static/logo/ (se logo_fil()).
"""
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from . import db

GRUNNLEGGENDE, KONTAKT, ARBEID, FAKTURA, UTFYLLING = (
    "Grunnleggende", "Kontaktinformasjon", "Arbeid og autorisasjon", "Faktura", "Oppmøte og utfylling")
GRUPPER = (GRUNNLEGGENDE, KONTAKT, ARBEID, FAKTURA, UTFYLLING)


@dataclass(frozen=True)
class Kolonne:
    nokkel: str
    tittel: str
    gruppe: str
    personopplysning: bool = False   # merkes i skjermbildet
    standard: bool = False           # valgt når siden åpnes første gang
    laast: bool = False              # alltid med (navn)
    kun_utskrift: bool = False       # tom kolonne til utfylling for hånd - ikke med i CSV


KOLONNER = (
    Kolonne("nr", "Nr.", GRUNNLEGGENDE, standard=True),
    Kolonne("navn", "Navn", GRUNNLEGGENDE, laast=True),
    Kolonne("status", "Status", GRUNNLEGGENDE),
    Kolonne("paameldt", "Påmeldt", GRUNNLEGGENDE),
    Kolonne("epost", "E-post", KONTAKT, personopplysning=True),
    Kolonne("telefon", "Telefon", KONTAKT, personopplysning=True),
    Kolonne("arbeidssted", "Arbeidssted", ARBEID, personopplysning=True),
    Kolonne("yrkestittel", "Profesjon", ARBEID, personopplysning=True),        # feltet heter yrkestittel i skjemaet
    Kolonne("hpr_nr", "HPR-nummer", ARBEID, personopplysning=True),
    Kolonne("betaler", "Betaler", FAKTURA),
    Kolonne("org_navn", "Organisasjon", FAKTURA),
    Kolonne("fakturastatus", "Fakturastatus", FAKTURA, personopplysning=True),
    Kolonne("oppmote", "Oppmøte per kursdag", UTFYLLING),
    Kolonne("signatur", "Signatur", UTFYLLING, kun_utskrift=True),
    Kolonne("merknad", "Merknad", UTFYLLING, kun_utskrift=True),
)

STATUSVALG = {"bekreftet": "Bekreftede", "venteliste": "Venteliste", "avmeldt": "Avmeldte", "avslatt": "Avslåtte",
              "utgatt": "Utgåtte", "forlatt": "Forlatte", "alle": "Alle"}
_BETALER = {"person": "Deltaker", "organisasjon": "Organisasjon"}

LOGO_MAPPE = Path(__file__).resolve().parent / "web" / "static" / "logo"
_LOGO_FILER = ("deltakerliste.svg", "deltakerliste.png", "deltakerliste.jpg")


@dataclass(frozen=True)
class Valg:
    kolonner: tuple
    status: str

    @property
    def nokler(self) -> list[str]:
        return [k.nokkel for k in self.kolonner]

    def som_args(self) -> dict:
        """Samme valg som query-parametre (brukes av CSV-lenken)."""
        return {"valgt": "1", "status": self.status, "kol": [k.nokkel for k in self.kolonner if not k.laast]}


@dataclass(frozen=True)
class Tabell:
    overskrifter: list
    klasser: list       # css-klasse per kolonne (bredde/justering i utskriften)
    rader: list


def les_valg(args) -> Valg:
    """Valgene fra adressen. Første besøk (uten `valgt`) gir standardkolonnene; ellers nøyaktig det som er krysset
    av. Ukjente kolonner og statuser ignoreres."""
    status = args.get("status", "bekreftet")
    if status not in STATUSVALG:
        status = "bekreftet"
    valgte = set(args.getlist("kol")) if args.get("valgt") else {k.nokkel for k in KOLONNER if k.standard}
    return Valg(tuple(k for k in KOLONNER if k.laast or k.nokkel in valgte), status)


def fakturastatus(kurs, p) -> str:
    """Utleder en lesbar fakturastatus fra fakturaradene til paameldingen. Ingen egen kolonne trengs.

    NB: "Fakturert" betyr bare at fakturaen er sendt, ikke at den er betalt.
    """
    if kurs["fakturering"] != "person" or not kurs["pris_nok"]:
        return "–"
    if p["faktura_antall"] == 0:
        return "Ikke fakturert"
    if p["faktura_ubetalt"] == 0:
        return "Betalt"
    if p["faktura_ubetalt"] == p["faktura_antall"]:
        return "Fakturert"
    return "Delvis betalt"


_NORSKE_BOKSTAVER = str.maketrans({"æ": "{", "ä": "{", "ø": "|", "ö": "|", "å": "}"})   # { | } kommer etter z


def _sortering(navn: str) -> str:
    """Norsk alfabetisk rekkefølge (æ, ø, å etter z; é som e) - uavhengig av databasens sortering."""
    tekst = unicodedata.normalize("NFD", (navn or "").casefold().translate(_NORSKE_BOKSTAVER))
    return "".join(c for c in tekst if unicodedata.category(c) != "Mn")


def hent(con, kurs_id: int, status: str) -> list[dict]:
    """Påmeldingene til kurset med valgt status, sortert på navn. Leser aldri tabellen sensitivt."""
    sql = """SELECT p.id, p.status, p.avslatt_ts, p.utgatt_ts, p.forlatt_ts, p.betaler, p.org_navn, p.opprettet, d.navn,
                    d.epost, d.telefon, d.arbeidssted,
                    d.yrkestittel, d.hpr_nr,
                    (SELECT COUNT(*) FROM faktura WHERE paamelding_id=p.id) AS faktura_antall,
                    (SELECT COUNT(*) FROM faktura WHERE paamelding_id=p.id AND status!='betalt') AS faktura_ubetalt
             FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE p.kurs_id=?"""
    # Avslått, utgått og forlatt er avmeldte påmeldinger med en dato (db.paameldingsstatus)
    rader = [dict(r) for r in con.execute(sql, (kurs_id,))]
    valgte = [r for r in rader if status == "alle" or db.paameldingsstatus(r) == status]
    return sorted(valgte, key=lambda r: (_sortering(r["navn"]), r["id"]))


def oppmote(con, kurs_id: int) -> set[tuple[int, int]]:
    """(paamelding_id, kursdag_id) for alt registrert oppmøte på kurset."""
    return {(r["paamelding_id"], r["kursdag_id"]) for r in con.execute(
        "SELECT o.paamelding_id, o.kursdag_id FROM oppmote o JOIN kursdag kd ON kd.id=o.kursdag_id WHERE kd.kurs_id=?",
        (kurs_id,))}


def _norsk_dato(iso: str) -> str:
    iso = str(iso or "")[:10]
    return f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}" if len(iso) == 10 else iso


def datoer_tekst(dager) -> str:
    """«10.01.2027» eller «10.01.2027 – 14.03.2027 (4 kursdager)» til toppen av utskriften."""
    if not dager:
        return ""
    forste, siste = _norsk_dato(dager[0]["dato"]), _norsk_dato(dager[-1]["dato"])
    return forste if len(dager) == 1 else f"{forste} – {siste} ({len(dager)} kursdager)"


def _verdi(nokkel: str, nr: int, r: dict, kurs) -> str:
    if nokkel == "nr":
        return str(nr)
    if nokkel == "status":
        return db.PAAMELDINGSSTATUSER[db.paameldingsstatus(r)]
    if nokkel == "paameldt":
        return _norsk_dato(r["opprettet"])
    if nokkel == "betaler":
        return _BETALER.get(r["betaler"], r["betaler"] or "")
    if nokkel == "fakturastatus":
        return fakturastatus(kurs, r)
    if nokkel in ("signatur", "merknad"):
        return ""
    return r.get(nokkel) or ""


def tabell(kurs, rader, valg: Valg, dager, oppmott: set, *, csv: bool = False) -> Tabell:
    """Overskrifter og celler for valgte kolonner. «Oppmøte per kursdag» blir én kolonne per kursdag."""
    kolonner = [k for k in valg.kolonner if not (csv and k.kun_utskrift)]
    overskrifter, klasser = [], []
    for k in kolonner:
        if k.nokkel == "oppmote":
            overskrifter += [f"Oppmøte {d['dato']}" if csv else _norsk_dato(d["dato"])[:6] for d in dager]
            klasser += ["avkrysning"] * len(dager)
        else:
            overskrifter.append(k.tittel)
            klasser.append(k.nokkel if k.nokkel in ("nr", "signatur", "merknad") else "")
    celler = []
    for nr, r in enumerate(rader, 1):
        rad = []
        for k in kolonner:
            if k.nokkel == "oppmote":
                rad += [("ja" if csv else "✓") if (r["id"], d["id"]) in oppmott else "" for d in dager]
            else:
                rad.append(_verdi(k.nokkel, nr, r, kurs))
        celler.append(rad)
    return Tabell(overskrifter, klasser, celler)


def logo_fil() -> str | None:
    """Sti under static/ til godkjent logo for utskriften, eller None. Systemet leveres UTEN logo: en godkjent fil
    (f.eks. NIEFT-logoen, når bruken er avklart) legges som static/logo/deltakerliste.png (eller .svg/.jpg)."""
    for navn in _LOGO_FILER:
        if (LOGO_MAPPE / navn).is_file():
            return f"logo/{navn}"
    return None
