"""Årsplan (kurshjul): alle kursdager i et år sammen med planlagte aktiviteter og notater.

  * Kurs kommer med automatisk (fra kursdag), unntatt avlyste. Utkast vises, merket som utkast.
  * Planlagte aktiviteter (type 'plan') opptar datoene sine. Notater ('notat') er bare påminnelser og opptar ingenting.
  * Overlapp = en dato med minst to kurs/planlagte aktiviteter.
  * Ledige perioder = sammenhengende ISO-uker uten kursdager og uten planlagte aktiviteter.
"""
import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta

from . import db

MAANEDER = ("Januar", "Februar", "Mars", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
            "November", "Desember")
_MND_KORT = ("jan", "feb", "mar", "apr", "mai", "jun", "jul", "aug", "sep", "okt", "nov", "des")
TYPER = {"plan": "Planlagt aktivitet", "notat": "Notat"}
MAKS_TITTEL, MAKS_STED, MAKS_NOTAT, MAKS_DAGER = 120, 80, 1000, 366
FORSTE_AAR, SISTE_AAR = 2000, 2100

_KONTROLLTEGN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ISO_DATO = re.compile(r"\d{4}-\d{2}-\d{2}")


class AarsplanFeil(ValueError):
    """Ugyldig input. Meldingen er trygg å vise til admin."""


@dataclass(frozen=True)
class Aktivitet:
    type: str                   # 'kurs' | 'plan' | 'notat'
    tittel: str
    sted: str | None = None
    kurs_id: int | None = None
    kursnr: int | None = None
    status: str | None = None

    @property
    def opptar(self) -> bool:
        return self.type in ("kurs", "plan")

    @property
    def visning(self) -> str:
        if self.type == "kurs":
            return f"{self.kursnr} {self.tittel}" if self.kursnr else self.tittel
        return f"{'Planlagt' if self.type == 'plan' else 'Notat'}: {self.tittel}"


@dataclass(frozen=True)
class Celle:
    dag: int
    klasse: str                 # css: kurs / plan / notat / overlapp / helg / idag / utenfor
    tekst: str                  # K, P, N eller antall ved overlapp - aldri bare farge
    tittel: str                 # hover-tekst med navnene


@dataclass(frozen=True)
class Periode:
    fra_uke: int
    til_uke: int
    fra: date
    til: date

    @property
    def antall_uker(self) -> int:
        return self.til_uke - self.fra_uke + 1

    @property
    def tekst(self) -> str:
        return periode_tekst(self.fra, self.til)


@dataclass
class Aarsplan:
    aar: int
    per_dato: dict              # date -> [Aktivitet] (inkl. kantuker utenfor kalenderåret, for ledige uker)
    kurs: list                  # per kurs: {id, kursnr, navn, status, sted, datoer}
    planer: list                # rader fra planlagt_aktivitet som berører året


# ============================ datoer og tekst ============================

def antall_uker(aar: int) -> int:
    return date(aar, 12, 28).isocalendar()[1]


def _dager(fra: date, til: date):
    d = fra
    while d <= til:
        yield d
        d += timedelta(days=1)


def dato_tekst(d: date) -> str:
    return f"{d.day}. {_MND_KORT[d.month - 1]}"


def periode_tekst(fra: date, til: date) -> str:
    """«15. okt», «15.–17. okt», «28. okt – 3. nov» eller, over nyttår, «29. des 2025 – 4. jan 2026»."""
    if fra == til:
        return dato_tekst(fra)
    if (fra.year, fra.month) == (til.year, til.month):
        return f"{fra.day}.–{dato_tekst(til)}"
    if fra.year != til.year:
        return f"{dato_tekst(fra)} {fra.year} – {dato_tekst(til)} {til.year}"
    return f"{dato_tekst(fra)} – {dato_tekst(til)}"


def _dagliste(datoer: list[date]) -> str:
    """Datoene i én måned: «3., 4. og 10. sep»."""
    dager = [f"{d.day}." for d in datoer]
    liste = dager[0] if len(dager) == 1 else ", ".join(dager[:-1]) + " og " + dager[-1]
    return f"{liste} {_MND_KORT[datoer[0].month - 1]}"


# ============================ lesing ============================

def hent(con, aar: int) -> Aarsplan:
    """Kursdager (ikke avlyste kurs) og planer som berører året, inkludert ISO-kantukene."""
    fra = min(date(aar, 1, 1), date.fromisocalendar(aar, 1, 1))
    til = max(date(aar, 12, 31), date.fromisocalendar(aar, antall_uker(aar), 7))
    per_dato: dict = {}
    kurs: dict = {}
    for r in con.execute(
            """SELECT kd.dato, k.id, k.kursnr, k.navn, k.status,
                      COALESCE(k.sted, CASE WHEN k.type='digital' THEN 'Online' END) AS sted
               FROM kursdag kd JOIN kurs k ON k.id=kd.kurs_id
               WHERE kd.dato BETWEEN ? AND ? AND k.status != 'avlyst'
               ORDER BY kd.dato, k.navn, k.id""", (fra.isoformat(), til.isoformat())):
        d = date.fromisoformat(r["dato"])
        per_dato.setdefault(d, []).append(Aktivitet("kurs", r["navn"], r["sted"], r["id"], r["kursnr"], r["status"]))
        if d.year == aar:
            kurs.setdefault(r["id"], {"id": r["id"], "kursnr": r["kursnr"], "navn": r["navn"], "status": r["status"],
                                      "sted": r["sted"], "datoer": []})["datoer"].append(d)
    planer = [dict(r) for r in con.execute(
        "SELECT * FROM planlagt_aktivitet WHERE til_dato >= ? AND fra_dato <= ? ORDER BY fra_dato, id",
        (fra.isoformat(), til.isoformat()))]
    for p in planer:
        a = Aktivitet(p["type"], p["tittel"], p["sted"])
        for d in _dager(max(date.fromisoformat(p["fra_dato"]), fra), min(date.fromisoformat(p["til_dato"]), til)):
            per_dato.setdefault(d, []).append(a)
    planer = [p for p in planer if p["til_dato"] >= f"{aar}-01-01" and p["fra_dato"] <= f"{aar}-12-31"]
    return Aarsplan(aar, per_dato, list(kurs.values()), planer)


# ============================ visninger ============================

def _tittel(d: date, aktiviteter: list) -> str:
    if not aktiviteter:
        return ""
    return f"{d.day}.{d.month}.: " + "; ".join(a.visning for a in aktiviteter)


def rutenett(plan: Aarsplan, idag: date) -> list[tuple[str, list[Celle]]]:
    """Én rad per måned, 31 celler. Teksten i cellen (K/P/N/antall) bærer betydningen - fargen er bare en hjelp."""
    rader = []
    for m in range(1, 13):
        dager_i_mnd = calendar.monthrange(plan.aar, m)[1]
        celler = []
        for dag in range(1, 32):
            if dag > dager_i_mnd:
                celler.append(Celle(dag, "utenfor", "", ""))
                continue
            d = date(plan.aar, m, dag)
            akt = plan.per_dato.get(d, [])
            opptar = [a for a in akt if a.opptar]
            if len(opptar) >= 2:
                klasser, tekst = ["overlapp"], str(len(opptar))
            elif opptar:
                klasser, tekst = [opptar[0].type], "K" if opptar[0].type == "kurs" else "P"
            elif akt:
                klasser, tekst = ["notat"], "N"
            else:
                klasser, tekst = [], ""
            if d.weekday() >= 5:
                klasser.append("helg")
            if d == idag:
                klasser.append("idag")
            celler.append(Celle(dag, " ".join(klasser), tekst, _tittel(d, akt)))
        rader.append((MAANEDER[m - 1], celler))
    return rader


def overlapp(plan: Aarsplan) -> list[tuple[str, list]]:
    """(datotekst, aktiviteter) for hver dato i året med minst to kurs/planlagte aktiviteter."""
    ut = []
    for d in sorted(plan.per_dato):
        opptar = [a for a in plan.per_dato[d] if a.opptar]
        if d.year == plan.aar and len(opptar) >= 2:
            ut.append((dato_tekst(d), opptar))
    return ut


def ledige_perioder(plan: Aarsplan) -> list[Periode]:
    """Sammenhengende ISO-uker i året uten kursdager og uten planlagte aktiviteter."""
    opptatt = {d.isocalendar()[1] for d, akt in plan.per_dato.items()
               if d.isocalendar()[0] == plan.aar and any(a.opptar for a in akt)}
    siste = antall_uker(plan.aar)
    perioder, start = [], None
    for uke in range(1, siste + 2):                       # siste + 1 avslutter en åpen periode
        ledig = uke <= siste and uke not in opptatt
        if ledig and start is None:
            start = uke
        elif not ledig and start is not None:
            perioder.append(Periode(start, uke - 1, date.fromisocalendar(plan.aar, start, 1),
                                    date.fromisocalendar(plan.aar, uke - 1, 7)))
            start = None
    return perioder


def per_maaned(plan: Aarsplan) -> list[dict]:
    """Kurs og planer per måned, med ferdige datotekster til visningen."""
    ut = []
    for m in range(1, 13):
        mfra, mtil = date(plan.aar, m, 1), date(plan.aar, m, calendar.monthrange(plan.aar, m)[1])
        kurs = []
        for k in plan.kurs:
            datoer = [d for d in k["datoer"] if d.month == m]
            if datoer:
                kurs.append({**k, "datoer": datoer, "datotekst": _dagliste(datoer)})
        planer = [{**p, "periodetekst": periode_tekst(date.fromisoformat(p["fra_dato"]), date.fromisoformat(p["til_dato"]))}
                  for p in plan.planer if p["fra_dato"] <= mtil.isoformat() and p["til_dato"] >= mfra.isoformat()]
        ut.append({"navn": MAANEDER[m - 1], "kurs": sorted(kurs, key=lambda k: (k["datoer"][0], k["navn"])),
                   "planer": planer})
    return ut


# ============================ planlagte aktiviteter ============================

def _tekst(verdi, navn: str, maks: int, *, paakrevd: bool = False, flerlinjet: bool = False) -> str | None:
    tekst = (verdi or "").strip()
    if not tekst:
        if paakrevd:
            raise AarsplanFeil(f"{navn} må fylles ut.")
        return None
    if len(tekst) > maks:
        raise AarsplanFeil(f"{navn} kan være høyst {maks} tegn.")
    if _KONTROLLTEGN.search(tekst) or (not flerlinjet and ("\n" in tekst or "\r" in tekst)):
        raise AarsplanFeil(f"{navn} inneholder ugyldige tegn.")
    return tekst


def _dato(verdi, navn: str) -> date:
    tekst = (verdi or "").strip()
    if not _ISO_DATO.fullmatch(tekst):
        raise AarsplanFeil(f"{navn} må være en dato (ÅÅÅÅ-MM-DD).")
    try:
        d = date.fromisoformat(tekst)
    except ValueError:
        raise AarsplanFeil(f"{navn} er ikke en gyldig dato.") from None
    if not FORSTE_AAR <= d.year <= SISTE_AAR:
        raise AarsplanFeil(f"{navn} må være mellom år {FORSTE_AAR} og {SISTE_AAR}.")
    return d


def valider(skjema) -> dict:
    """Leser og validerer en ny planlagt aktivitet / et notat fra skjemaet. AarsplanFeil ved ugyldig input."""
    type_ = skjema.get("type") or "plan"
    if type_ not in TYPER:
        raise AarsplanFeil("Ukjent type.")
    tittel = _tekst(skjema.get("tittel"), "Tittel", MAKS_TITTEL, paakrevd=True)
    fra = _dato(skjema.get("fra_dato"), "Fra-dato")
    til = _dato(skjema.get("til_dato"), "Til-dato") if (skjema.get("til_dato") or "").strip() else fra
    if til < fra:
        raise AarsplanFeil("Til-dato kan ikke være før fra-dato.")
    if (til - fra).days >= MAKS_DAGER:
        raise AarsplanFeil("En planlagt aktivitet kan vare høyst ett år.")
    return {"type": type_, "tittel": tittel, "fra_dato": fra.isoformat(), "til_dato": til.isoformat(),
            "sted": _tekst(skjema.get("sted"), "Sted", MAKS_STED),
            "notat": _tekst(skjema.get("notat"), "Notat", MAKS_NOTAT, flerlinjet=True)}


def opprett(con, verdier: dict, aktor: str) -> int:
    plan_id = db.sett_inn(
        con, "INSERT INTO planlagt_aktivitet (type, tittel, fra_dato, til_dato, sted, notat, opprettet_av) "
             "VALUES (?,?,?,?,?,?,?)",
        (verdier["type"], verdier["tittel"], verdier["fra_dato"], verdier["til_dato"], verdier["sted"],
         verdier["notat"], aktor))
    # Hendelsesloggen får aldri fritekst (tittel/notat) - bare id, type og datoer.
    db.logg(con, "planlagt_aktivitet_opprettet", {"id": plan_id, "type": verdier["type"],
                                                  "fra_dato": verdier["fra_dato"], "til_dato": verdier["til_dato"]},
            aktor=aktor)
    return plan_id


def slett(con, plan_id: int, aktor: str) -> dict | None:
    """Sletter planen og returnerer raden (None hvis den ikke finnes)."""
    rad = con.execute("SELECT * FROM planlagt_aktivitet WHERE id=?", (plan_id,)).fetchone()
    if not rad:
        return None
    con.execute("DELETE FROM planlagt_aktivitet WHERE id=?", (plan_id,))
    db.logg(con, "planlagt_aktivitet_slettet", {"id": plan_id, "type": rad["type"], "fra_dato": rad["fra_dato"]},
            aktor=aktor)
    return dict(rad)
