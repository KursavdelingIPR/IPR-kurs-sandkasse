"""Deltakerliste med kolonnevalg: utskrift (og PDF via nettleserens «Lagre som PDF») og CSV.

Personvern (CLAUDE.md regel 1):
  * Standard er bare løpenummer og navn. Kontaktinformasjon, arbeid og faktura må krysses av eksplisitt.
  * Allergier/tilrettelegging (tabellen `sensitivt`) finnes ikke i katalogen og kan aldri velges - de har egen liste
    med tilgangslogg (admin_allergiliste). Ukjente kolonnenavn i adressen ignoreres.
  * Den tomme utfyllingskolonnen (merknad) er bare for papir og tas ikke med i CSV. Signatur er ikke med: registrering
    av oppmøte har sitt eget skjema (Camilla 03.10.2026: «dette er en deltakerliste … Vi skal ha et annet skjema til
    registrering»).
  * Svarene på kursets egne felt i påmeldingsskjemaet kan velges som egne kolonner (katalog()). Egne felt skal aldri
    brukes til sensitive opplysninger (skjemabyggeren sier fra om det).

PDF lages bevisst i nettleseren (utskrift -> «Lagre som PDF»), samme løsning som for kursbevis: ingen tung
PDF-avhengighet i drift. Utskriften er et A4-ark med logo øverst, kursnavnet og listen midtstilt (Camilla 03.10.2026).
Logoen velges blant filene som er lagt i static/logo/ (se logoer() og velg_logo()).
"""
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from . import db, ekstrafelt, privatadresse

GRUNNLEGGENDE, KONTAKT, ARBEID, FAKTURA, SVAR, UTFYLLING = (
    "Grunnleggende", "Kontaktinformasjon", "Arbeid og autorisasjon", "Faktura", "Svar i påmeldingsskjemaet",
    "Oppmøte og utfylling")
GRUPPER = (GRUNNLEGGENDE, KONTAKT, ARBEID, FAKTURA, SVAR, UTFYLLING)
SVAR_PREFIKS = "svar_"          # kolonnen for svaret på eget felt nr. 5 heter «svar_5»
FAKTURERES_MANUELT = "Faktureres manuelt"      # fakturastatus for en ekstradeltaker som ikke har faktura i systemet


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
    Kolonne("samlinger", "Samlinger", GRUNNLEGGENDE),         # «Hele kurset» eller «Samling 1, 3» (ekstradeltakere kan være på noen)
    Kolonne("paameldt", "Påmeldingsdato", GRUNNLEGGENDE),    # datoen påmeldingen kom (ikke statusen «Påmeldt»)
    Kolonne("epost", "E-post", KONTAKT, personopplysning=True),
    Kolonne("telefon", "Telefon", KONTAKT, personopplysning=True),
    Kolonne("arbeidssted", "Arbeidssted", ARBEID, personopplysning=True),
    Kolonne("yrkestittel", "Profesjon", ARBEID, personopplysning=True),        # feltet heter yrkestittel i skjemaet
    Kolonne("hpr_nr", "HPR-nummer", ARBEID, personopplysning=True),
    Kolonne("betaler", "Betaler", FAKTURA),
    Kolonne("org_navn", "Organisasjon", FAKTURA),
    Kolonne("fakturastatus", "Fakturastatus", FAKTURA, personopplysning=True),
    Kolonne("oppmote", "Oppmøte per kursdag", UTFYLLING),
    Kolonne("merknad", "Merknad", UTFYLLING, kun_utskrift=True),
)

def katalog(egne=()) -> tuple:
    """Kolonnene som kan velges for kurset: de faste (KOLONNER) og ett svar per eget felt i påmeldingsskjemaet (`egne`,
    i skjemaets rekkefølge). Svarene regnes som personopplysninger og er aldri valgt som standard. Utfyllingskolonnene
    står fortsatt sist."""
    svar = tuple(Kolonne(f"{SVAR_PREFIKS}{e.id}", e.label, SVAR, personopplysning=True) for e in egne)
    return (*(k for k in KOLONNER if k.gruppe != UTFYLLING), *svar, *(k for k in KOLONNER if k.gruppe == UTFYLLING))


# Valgene for utskriften: «Påmeldte og ekstradeltakere» (alle som har plass), statusene i flertall (ordene står bare i
# db.PAAMELDINGSSTATUSER_FLERTALL) og «Alle». En liste over de som skal være på kurset skal ikke mangle ekstradeltakerne,
# så utskriften åpner med alle som har plass.
PLASS = "plass"
STATUSVALG = {PLASS: f"{db.PAAMELDINGSSTATUSER_FLERTALL['paameldt']} og {db.PAAMELDINGSSTATUSER_FLERTALL['ekstradeltaker'].lower()}",
              **db.PAAMELDINGSSTATUSER_FLERTALL, "alle": "Alle"}
STANDARD_STATUS = PLASS
_BETALER = {"person": "Deltaker", "organisasjon": "Organisasjon"}

LOGO_MAPPE = Path(__file__).resolve().parent / "web" / "static" / "logo"
_LOGO_FILER = ("deltakerliste.svg", "deltakerliste.png", "deltakerliste.jpg")
# Logoene utskriften kan ha øverst (Camilla 03.10: «terapiakademiet-logo (NIEFT-logo, eller IPR-logo på IPR sine kurs)»).
# En logo kan velges når filen er lagt i static/logo/; den første som finnes er standard (alle deltakersidene har
# Terapiakademiet-drakten til kursene får et eget merke). «egen» er den gamle filen deltakerliste.png.
LOGOER = (("terapiakademiet", "Terapiakademiet", ("terapiakademiet.svg", "terapiakademiet.png")),
          ("nieft", "NIEFT", ("nieft.svg", "nieft.png", "nieft.jpg")),
          ("ipr", "IPR", ("ipr.svg", "ipr.png", "ipr.jpg")),
          ("egen", "Egen logo", _LOGO_FILER))
INGEN_LOGO = "ingen"


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


def les_valg(args, katalog_=KOLONNER) -> Valg:
    """Valgene fra adressen. Første besøk (uten `valgt`) gir standardkolonnene; ellers nøyaktig det som er krysset
    av. Ukjente kolonner og statuser ignoreres. `katalog_`: kolonnene som finnes for kurset (se katalog())."""
    status = args.get("status", STANDARD_STATUS)
    if status not in STATUSVALG:
        status = STANDARD_STATUS
    valgte = set(args.getlist("kol")) if args.get("valgt") else {k.nokkel for k in katalog_ if k.standard}
    return Valg(tuple(k for k in katalog_ if k.laast or k.nokkel in valgte), status)


def _har(p, nokkel: str) -> bool:
    """Er nøkkelen satt (og sann) på raden? Fungerer for både dict og databaseradene."""
    try:
        return bool(p[nokkel])
    except (KeyError, IndexError):
        return False


def fakturastatus(kurs, p) -> str:
    """Utleder en lesbar fakturastatus fra fakturaradene til paameldingen. Ingen egen kolonne trengs.

    NB: "Fakturert" betyr bare at fakturaen er sendt, ikke at den er betalt.
    En ekstradeltaker uten faktura i systemet er ikke «Ikke fakturert» (det høres ut som noe som mangler): den faktureres aldri
    automatisk, og administrator lager fakturaen selv, utenfor systemet (Visma).
    En privat betaler uten komplett privat adresse får «Venter på privat adresse»: fakturaen holdes tilbake til adressen er lagt
    inn (privatadresse.py). Kalleren som vet det, setter nøkkelen faktura_venter_adresse på raden.
    """
    if kurs["fakturering"] != "person" or not kurs["pris_nok"]:
        return "–"
    if p["faktura_antall"] == 0:
        if db.er_ekstradeltaker(p):
            return FAKTURERES_MANUELT
        return privatadresse.FAKTURA_VENTER if _har(p, "faktura_venter_adresse") else "Ikke fakturert"
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


norsk_sortering = _sortering     # brukes også av kurslisten (aktivitetsliste.py)


def hent(con, kurs_id: int, status: str) -> list[dict]:
    """Påmeldingene til kurset med valgt status, sortert på navn. Leser aldri tabellen sensitivt."""
    sql = """SELECT p.id, p.status, p.avslatt_ts, p.utgatt_ts, p.forlatt_ts, p.ekstradeltaker_ts, p.betaler, p.org_navn,
                    p.opprettet, d.navn,
                    d.epost, d.telefon, d.arbeidssted,
                    d.yrkestittel, d.hpr_nr,
                    (SELECT COUNT(*) FROM faktura WHERE paamelding_id=p.id) AS faktura_antall,
                    (SELECT COUNT(*) FROM faktura WHERE paamelding_id=p.id AND status!='betalt') AS faktura_ubetalt
             FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE p.kurs_id=?"""
    # Avslått, utgått og forlatt er avmeldte påmeldinger med en dato, og Ekstradeltaker en påmelding med plass og en dato
    # (db.paameldingsstatus)
    rader = [dict(r) for r in con.execute(sql, (kurs_id,))]
    valgte = [r for r in rader if status == "alle" or db.paameldingsstatus(r) == status
              or (status == PLASS and db.paameldingsstatus(r) in db.HAR_PLASS)]
    # Samlingene hver med plass er på: «Hele kurset» eller de valgte (ekstradeltaker). `egne_dager` er id-ene til kursdagene
    # deltakeren er på, eller None = alle dager (oppmøtekolonnene viser de andre dagene som «–»).
    alle_dager = db.kursdager(con, kurs_id)
    utvalg = db.samlingsutvalg_for_kurs(con, kurs_id)
    titler = {s["id"]: s["tittel"] for s in db.kursets_samlinger(con, kurs_id, alle_dager)}
    for r in valgte:
        valgt = utvalg.get(r["id"]) if db.er_ekstradeltaker(r) else None
        har_plass = db.paameldingsstatus(r) in db.HAR_PLASS
        r["samlinger"] = (db.samlingstekst([titler[i] for i in valgt if i in titler]) if valgt else "Hele kurset") if har_plass else ""
        r["egne_dager"] = {d["id"] for d in alle_dager if d["samling_id"] in valgt} if valgt else None
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
    if nokkel == "merknad":
        return ""
    if nokkel.startswith(SVAR_PREFIKS):
        felt_id = nokkel[len(SVAR_PREFIKS):]
        return ekstrafelt.vis_svar((r.get("svar") or {}).get(int(felt_id))) if felt_id.isdigit() else ""
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
            klasser.append(k.nokkel if k.nokkel in ("nr", "merknad") else "")
    celler = []
    for nr, r in enumerate(rader, 1):
        rad = []
        for k in kolonner:
            if k.nokkel == "oppmote":
                egne = r.get("egne_dager")     # ekstradeltaker på noen samlinger: de andre dagene er «–» (teller ikke)
                rad += [("" if csv else "–") if egne is not None and d["id"] not in egne
                        else (("ja" if csv else "✓") if (r["id"], d["id"]) in oppmott else "") for d in dager]
            else:
                rad.append(_verdi(k.nokkel, nr, r, kurs))
        celler.append(rad)
    return Tabell(overskrifter, klasser, celler)


def logoer() -> list[dict]:
    """Logoene som kan velges (filen finnes i static/logo/): [{nokkel, navn, fil}] med fil = sti under static/."""
    ut = []
    for nokkel, navn, filer in LOGOER:
        fil = next((f for f in filer if (LOGO_MAPPE / f).is_file()), None)
        if fil:
            ut.append({"nokkel": nokkel, "navn": navn, "fil": f"logo/{fil}"})
    return ut


def velg_logo(onsket: str | None) -> dict | None:
    """Logoen øverst på utskriften: den valgte (?logo=...) når filen finnes, None for «ingen», ellers den første som
    finnes (standard). Ukjente verdier gir standard."""
    finnes = logoer()
    if onsket == INGEN_LOGO:
        return None
    return next((l for l in finnes if l["nokkel"] == onsket), finnes[0] if finnes else None)
