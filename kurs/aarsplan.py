"""Årsplan (kurshjul): alle kursdager i et år sammen med planlagte aktiviteter og notater.

  * Året vises som tolv små månedskalendere (mandag-søndag, med ukenummer): kurs og planlagte aktiviteter som blokker
    over dagene de varer (hver samling for seg, farget etter kursserien - kursfarger.py), helger, røde dager,
    skoleferier, sperrer og advarsler markert på dagene, og dager med flere kurs/planer som kollisjoner.
  * Kurs kommer med automatisk (fra kursdag), unntatt avlyste. Utkast vises, merket som utkast.
  * Planlagte aktiviteter (type 'plan') opptar datoene sine. Notater ('notat') er bare påminnelser og opptar ingenting.
  * Overlapp = en dato med minst to kurs/planlagte aktiviteter (vises som perioder: «15.–16. okt»).
  * Ledige perioder = sammenhengende dager uten kursdager, planlagte aktiviteter og sperrer («8.–14. mar · 7 dager»).
    Helt ledige ISO-uker vises i tillegg som ukenumre.
  * Røde dager regnes ut (helligdager.py). Skoleferier legges inn av admin per område (Oslo, Vestland ...) -
    aldri faste, nasjonale datoer i koden. Egne perioder er enten en sperre (vises som konflikt) eller en advarsel.
    Ingen av dem hindrer at kurs opprettes; de vises bare her (tabellen kalenderperiode).
"""
import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import db, helligdager, kursfarger

MAANEDER = ("Januar", "Februar", "Mars", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
            "November", "Desember")
_MND_KORT = ("jan", "feb", "mar", "apr", "mai", "jun", "jul", "aug", "sep", "okt", "nov", "des")
TYPER = {"plan": "Planlagt aktivitet", "notat": "Notat"}
PERIODETYPER = {"skoleferie": "Skoleferie", "sperre": "Sperret periode", "advarsel": "Advarsel"}
GJELDER = {"alle": "alle kurs", "fysisk": "fysiske kurs (også hybrid)", "online": "online kurs (også hybrid)"}
STANDARD_OMRAADER = ("Oslo", "Vestland")          # forslag i skjemaet - admin kan skrive andre områder
MAKS_NAVN, MAKS_OMRAADE = 80, 40
LEDIG_MIN_DAGER = 5
UKEDAGER = ("Man", "Tir", "Ons", "Tor", "Fre", "Lør", "Søn")
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
    kurstype: str | None = None     # fysisk / digital / hybrid (kurs)
    spesialistlop: str | None = None

    @property
    def farge(self) -> int | None:
        """Kursseriens farge (kursfarger.py) - bare for kurs"""
        return kursfarger.farge(self.tittel, self.spesialistlop) if self.type == "kurs" else None

    @property
    def opptar(self) -> bool:
        return self.type in ("kurs", "plan")

    @property
    def visning(self) -> str:
        if self.type == "kurs":
            return f"{self.kursnr} {self.tittel}" if self.kursnr else self.tittel
        return f"{'Planlagt' if self.type == 'plan' else 'Notat'}: {self.tittel}"


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
    kurs: list                  # per kurs: {id, kursnr, navn, status, sted, type, datoer}
    planer: list                # rader fra planlagt_aktivitet som berører året
    perioder: list = field(default_factory=list)    # rader fra kalenderperiode (skoleferier bare for valgt område)
    rode: dict = field(default_factory=dict)        # date -> navnet på den røde dagen (også kantukene)
    samlinger: dict = field(default_factory=dict)   # kurs_id -> [[datoene i samling 1], [samling 2], ...] (hele kurset)


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


def _og(deler: list[str]) -> str:
    """«A», «A og B», «A, B og C»"""
    return deler[0] if len(deler) == 1 else ", ".join(deler[:-1]) + " og " + deler[-1]


# ============================ lesing ============================

def hent(con, aar: int, omraade: str = "") -> Aarsplan:
    """Kursdager (ikke avlyste kurs), planer og perioder som berører året, inkludert ISO-kantukene. `omraade` avgrenser
    skoleferiene til ett område (tom = alle områder)."""
    nyttaar, nyttaarsaften = date(aar, 1, 1), date(aar, 12, 31)
    fra = min(nyttaar - timedelta(days=nyttaar.weekday()), date.fromisocalendar(aar, 1, 1))
    til = max(nyttaarsaften + timedelta(days=6 - nyttaarsaften.weekday()), date.fromisocalendar(aar, antall_uker(aar), 7))
    per_dato: dict = {}
    kurs: dict = {}
    for r in con.execute(
            """SELECT kd.dato, k.id, k.kursnr, k.navn, k.status, k.type AS kurstype, k.spesialistlop,
                      COALESCE(k.sted, CASE WHEN k.type='digital' THEN 'Online' END) AS sted
               FROM kursdag kd JOIN kurs k ON k.id=kd.kurs_id
               WHERE kd.dato BETWEEN ? AND ? AND k.status != 'avlyst'
               ORDER BY kd.dato, k.navn, k.id""", (fra.isoformat(), til.isoformat())):
        d = date.fromisoformat(r["dato"])
        per_dato.setdefault(d, []).append(Aktivitet("kurs", r["navn"], r["sted"], r["id"], r["kursnr"], r["status"],
                                                    r["kurstype"], r["spesialistlop"]))
        if d.year == aar:
            kurs.setdefault(r["id"], {"id": r["id"], "kursnr": r["kursnr"], "navn": r["navn"], "status": r["status"],
                                      "sted": r["sted"], "type": r["kurstype"], "datoer": []})["datoer"].append(d)
    planer = [dict(r) for r in con.execute(
        "SELECT * FROM planlagt_aktivitet WHERE til_dato >= ? AND fra_dato <= ? ORDER BY fra_dato, id",
        (fra.isoformat(), til.isoformat()))]
    for p in planer:
        a = Aktivitet(p["type"], p["tittel"], p["sted"])
        for d in _dager(max(date.fromisoformat(p["fra_dato"]), fra), min(date.fromisoformat(p["til_dato"]), til)):
            per_dato.setdefault(d, []).append(a)
    planer = [p for p in planer if p["til_dato"] >= f"{aar}-01-01" and p["fra_dato"] <= f"{aar}-12-31"]
    perioder = [dict(r) for r in con.execute(
        "SELECT * FROM kalenderperiode WHERE til_dato >= ? AND fra_dato <= ? ORDER BY fra_dato, id",
        (fra.isoformat(), til.isoformat()))]
    if omraade:
        perioder = [p for p in perioder
                    if p["type"] != "skoleferie" or (p["omraade"] or "").casefold() == omraade.casefold()]
    kurs_ider = sorted({a.kurs_id for akt in per_dato.values() for a in akt if a.type == "kurs"})
    return Aarsplan(aar, per_dato, list(kurs.values()), planer, perioder, helligdager.i_perioden(fra, til),
                    _samlinger(con, kurs_ider))


def _samlinger(con, kurs_ider: list[int]) -> dict:
    """Samlingene i hvert kurs (ALLE kursets dager, også i andre år): de lagrede samlingene fra Opprett kurs (tabellen
    samling), i den rekkefølgen de starter. Kursdager uten samling (eldre data) grupperes som sammenhengende datoer."""
    if not kurs_ider:
        return {}
    dager: dict[int, list] = {}
    for r in con.execute(f"SELECT kurs_id, dato, samling_id FROM kursdag WHERE kurs_id IN "
                         f"({','.join('?' * len(kurs_ider))}) ORDER BY kurs_id, dato", tuple(kurs_ider)):
        dager.setdefault(r["kurs_id"], []).append((date.fromisoformat(r["dato"]), r["samling_id"]))
    ut = {}
    for kid, liste in dager.items():
        grupper: dict = {}
        forrige_uten = None
        for dato, samling_id in liste:
            if samling_id is not None:
                nokkel = ("samling", samling_id)
            elif forrige_uten and forrige_uten[1] + timedelta(days=1) == dato:
                nokkel = forrige_uten[0]
            else:
                nokkel = ("dager", dato)
            if samling_id is None:
                forrige_uten = (nokkel, dato)
            grupper.setdefault(nokkel, []).append(dato)
        ut[kid] = list(grupper.values())
    return ut


def samling(plan: "Aarsplan", kurs_id: int, dato: date) -> tuple[int, int]:
    """(nr, antall) for samlingen `dato` hører til i kurset - (1, 1) når kurset har én samling."""
    samlinger = plan.samlinger.get(kurs_id) or []
    for nr, datoer in enumerate(samlinger, start=1):
        if dato in datoer:
            return nr, len(samlinger)
    return 1, max(1, len(samlinger))


# ============================ visninger ============================

def _tittel(d: date, aktiviteter: list, ekstra: list[str] = ()) -> str:
    deler = list(ekstra) + [a.visning for a in aktiviteter]
    if not deler:
        return ""
    return f"{d.day}.{d.month}.: " + "; ".join(deler)


def _periodedato(p, felt: str) -> date:
    return date.fromisoformat(p[felt])


def _dekker(p, d: date) -> bool:
    return p["fra_dato"] <= d.isoformat() <= p["til_dato"]


def periodenavn(p) -> str:
    """«Skoleferie Oslo: Høstferie», «Sperret: Intern ferie», «Advarsel: Ikke webinarer i juli (online kurs)»."""
    if p["type"] == "skoleferie":
        return f"Skoleferie {p['omraade']}: {p['navn']}"
    hva = "Sperret" if p["type"] == "sperre" else "Advarsel"
    return f"{hva}: {p['navn']}" + (f" ({GJELDER[p['gjelder']].split(' (')[0]})" if p["gjelder"] != "alle" else "")


def gjelder_kurs(gjelder: str, kurstype: str | None) -> bool:
    """Omfatter en sperre/advarsel med `gjelder` et kurs av denne typen? Hybrid er både fysisk og online."""
    return (gjelder == "alle" or (gjelder == "fysisk" and kurstype in ("fysisk", "hybrid"))
            or (gjelder == "online" and kurstype in ("digital", "hybrid")))


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


def overlapp_perioder(plan: Aarsplan) -> list[dict]:
    """Overlappene som sammenhengende perioder med de samme aktivitetene: {tekst: «15.–16. okt», dager, aktiviteter}."""
    ut: list[dict] = []
    for d in sorted(plan.per_dato):
        opptar = [a for a in plan.per_dato[d] if a.opptar]
        if d.year != plan.aar or len(opptar) < 2:
            continue
        nokkel = tuple(sorted((a.type, a.kurs_id or 0, a.tittel) for a in opptar))
        if ut and ut[-1]["nokkel"] == nokkel and ut[-1]["til"] + timedelta(days=1) == d:
            ut[-1]["til"] = d
        else:
            ut.append({"fra": d, "til": d, "nokkel": nokkel, "aktiviteter": opptar})
    for o in ut:
        o["tekst"], o["dager"] = periode_tekst(o["fra"], o["til"]), (o["til"] - o["fra"]).days + 1
        o["navn"] = _og([a.tittel if a.type == "kurs" else f"Planlagt: {a.tittel}" for a in o["aktiviteter"]])
    return ut


@dataclass
class Ledig:
    fra: date
    til: date
    merknader: list[str]
    rode: int = 0                                   # røde dager i perioden
    periodenavn: list = field(default_factory=list)  # skoleferier, advarsler og sperrer for bare noen kurs

    @property
    def dager(self) -> int:
        return (self.til - self.fra).days + 1

    @property
    def oppsummering(self) -> str:
        """«inkl. 2 røde dager · Skoleferie Oslo: Vinterferie» - kort, i stedet for alle datoene"""
        flere = self.rode > 1
        deler = [f"inkl. {self.rode} rød{'e' if flere else ''} dag{'er' if flere else ''}"] if self.rode else []
        navn = list(dict.fromkeys(self.periodenavn))
        deler += navn[:3] + ([f"og {len(navn) - 3} til"] if len(navn) > 3 else [])
        return " · ".join(deler)

    @property
    def uker(self) -> str:
        """«uke 10», «uke 10–12» - og over årsskiftet «uke 53 (2026) – uke 3»."""
        (aar_a, a, _), (aar_b, b, _) = self.fra.isocalendar(), self.til.isocalendar()
        if aar_a != aar_b:
            return f"uke {a} ({aar_a}) – uke {b}"
        return f"uke {a}" if a == b else f"uke {a}–{b}"

    @property
    def tekst(self) -> str:
        return periode_tekst(self.fra, self.til)


def ledige(plan: Aarsplan, min_dager: int = LEDIG_MIN_DAGER) -> list[Ledig]:
    """Konkrete ledige perioder i året: sammenhengende dager uten kursdager, planlagte aktiviteter og sperrer som
    gjelder alle kurs - minst `min_dager` dager lange. Røde dager, skoleferier, advarsler og sperrer for bare
    online/fysiske kurs står som merknad: de hindrer ikke kurs."""
    def opptatt(d: date) -> bool:
        return (any(a.opptar for a in plan.per_dato.get(d, []))
                or any(p["type"] == "sperre" and p["gjelder"] == "alle" and _dekker(p, d) for p in plan.perioder))

    ut, start = [], None
    d, slutt = date(plan.aar, 1, 1), date(plan.aar, 12, 31)
    while d <= slutt + timedelta(days=1):
        ledig = d <= slutt and not opptatt(d)
        if ledig and start is None:
            start = d
        elif not ledig and start is not None:
            til = d - timedelta(days=1)
            if (til - start).days + 1 >= min_dager:
                ut.append(Ledig(start, til, _merknader(plan, start, til),
                                sum(1 for dag in plan.rode if start <= dag <= til),
                                [periodenavn(p) for p in plan.perioder
                                 if p["fra_dato"] <= til.isoformat() and p["til_dato"] >= start.isoformat()]))
            start = None
        d += timedelta(days=1)
    return ut


def _merknader(plan: Aarsplan, fra: date, til: date) -> list[str]:
    ut = [f"{dato_tekst(d)}: {navn}" for d, navn in sorted(plan.rode.items()) if fra <= d <= til]
    ut += [periodenavn(p) for p in plan.perioder
           if p["fra_dato"] <= til.isoformat() and p["til_dato"] >= fra.isoformat()]
    return ut


@dataclass
class Konflikt:
    alvor: str                  # 'sperre' | 'advarsel' | 'rod'
    kurs: dict
    datoer: list
    hva: str                    # periodens navn, eller navnet på de røde dagene

    @property
    def datotekst(self) -> str:
        """«6.–7. jul» for sammenhengende dager, ellers «6. jul, 9. jul»."""
        if len(self.datoer) > 1 and (self.datoer[-1] - self.datoer[0]).days + 1 == len(self.datoer):
            return periode_tekst(self.datoer[0], self.datoer[-1])
        return ", ".join(dato_tekst(d) for d in self.datoer)


def konflikter(plan: Aarsplan) -> list[Konflikt]:
    """Kurs i en sperret periode eller en periode med advarsel (som omfatter kursets type), og kurs på røde dager.
    Bare til orientering - ingenting hindrer at kurs opprettes. Sortert på dato, sperrer først."""
    ut = []
    for k in plan.kurs:
        for p in plan.perioder:
            if p["type"] in ("sperre", "advarsel") and gjelder_kurs(p["gjelder"], k["type"]):
                treff = [d for d in k["datoer"] if _dekker(p, d)]
                if treff:
                    ut.append(Konflikt(p["type"], k, treff, periodenavn(p)))
        rode = [d for d in k["datoer"] if d in plan.rode]
        if rode:
            ut.append(Konflikt("rod", k, rode, ", ".join(dict.fromkeys(plan.rode[d] for d in rode))))
    rekkefolge = {"sperre": 0, "advarsel": 1, "rod": 2}
    return sorted(ut, key=lambda x: (x.datoer[0], rekkefolge[x.alvor], x.kurs["navn"]))


# ---------------- uke for uke ----------------

@dataclass
class Blokk:
    """Et kurs (én samling: sammenhengende kursdager) eller en planlagt aktivitet, slik den vises i uken `mandag`."""
    type: str                   # 'kurs' | 'plan'
    tittel: str
    fra: date                   # hele samlingen/aktiviteten - også dagene i andre uker
    til: date
    mandag: date
    kurs_id: int | None = None
    kursnr: int | None = None
    status: str | None = None
    kurstype: str | None = None
    sted: str | None = None
    farge: int | None = None
    samling_nr: int = 1
    samling_antall: int = 1

    @property
    def samling_tekst(self) -> str:
        """«2. samling av 4» (som i kurshjulet) - tom når kurset har én samling"""
        return f"{self.samling_nr}. samling av {self.samling_antall}" if self.samling_antall > 1 else ""

    @property
    def kolonne(self) -> int:               # 0 = mandag
        return (max(self.fra, self.mandag) - self.mandag).days

    @property
    def lengde(self) -> int:                # dager i denne uken
        return (min(self.til, self.mandag + timedelta(days=6)) - max(self.fra, self.mandag)).days + 1

    @property
    def fra_forrige(self) -> bool:
        return self.fra < self.mandag

    @property
    def til_neste(self) -> bool:
        return self.til > self.mandag + timedelta(days=6)

    @property
    def periode(self) -> str:
        dager = (self.til - self.fra).days + 1
        return periode_tekst(self.fra, self.til) + (f" ({dager} dager)" if dager > 1 else "")


@dataclass
class Ukerad:
    nr: int
    mandag: date
    dager: list                 # [{dato, helg, rod, i_aaret, idag}]
    baner: list                 # [[Blokk]] - blokkene fordelt på linjer så de aldri overlapper
    merknader: list             # [(klasse, tekst)]
    iso_aar: int = 0            # ISO-året uken hører til (et annet enn årsplanens for uken med 1. januar/31. desember)

    @property
    def sondag(self) -> date:
        return self.mandag + timedelta(days=6)

    @property
    def tekst(self) -> str:
        return periode_tekst(self.mandag, self.sondag)

    @property
    def ledig(self) -> bool:
        return not self.baner


def _lop(datoer: list[date]) -> list[tuple[date, date]]:
    """Sammenhengende datoer som (fra, til)."""
    ut: list[list[date]] = []
    for d in sorted(set(datoer)):
        if ut and ut[-1][1] + timedelta(days=1) == d:
            ut[-1][1] = d
        else:
            ut.append([d, d])
    return [(a, b) for a, b in ut]


def uker(plan: Aarsplan, idag: date) -> list[Ukerad]:
    """Ukene i året - fra uken med 1. januar til uken med 31. desember, så alle datoene i året er med - med kursene
    (hver samling som én blokk over dagene den varer, også over ukeskifter), planlagte aktiviteter, røde dager,
    skoleferier, sperrer, advarsler og notater."""
    kursdatoer: dict[int, list[date]] = {}
    info: dict[int, object] = {}
    for d, akt in plan.per_dato.items():
        for a in akt:
            if a.type == "kurs":
                kursdatoer.setdefault(a.kurs_id, []).append(d)
                info[a.kurs_id] = a
    lop = [("kurs", info[kid].tittel, fra, til, info[kid])
           for kid, datoer in kursdatoer.items() for fra, til in _lop(datoer)]
    lop += [("plan", p["tittel"], _periodedato(p, "fra_dato"), _periodedato(p, "til_dato"), p)
            for p in plan.planer if p["type"] == "plan"]
    ut = []
    mandag = date(plan.aar, 1, 1) - timedelta(days=date(plan.aar, 1, 1).weekday())
    while mandag <= date(plan.aar, 12, 31):
        iso_aar, nr, _ = mandag.isocalendar()
        sondag = mandag + timedelta(days=6)
        blokker = []
        for type_, tittel, fra, til, kilde in lop:
            if fra <= sondag and til >= mandag:
                if type_ == "kurs":
                    samling_nr, samling_antall = samling(plan, kilde.kurs_id, fra)
                    blokker.append(Blokk("kurs", tittel, fra, til, mandag, kilde.kurs_id, kilde.kursnr, kilde.status,
                                         kilde.kurstype, kilde.sted, kilde.farge, samling_nr, samling_antall))
                else:
                    blokker.append(Blokk("plan", tittel, fra, til, mandag, sted=kilde["sted"]))
        baner: list[list[Blokk]] = []
        for b in sorted(blokker, key=lambda b: (b.kolonne, -b.lengde, b.tittel.casefold())):
            for bane in baner:
                if bane[-1].kolonne + bane[-1].lengde <= b.kolonne:
                    bane.append(b)
                    break
            else:
                baner.append([b])
        dager = [{"dato": mandag + timedelta(days=i), "helg": i >= 5, "rod": plan.rode.get(mandag + timedelta(days=i)),
                  "i_aaret": (mandag + timedelta(days=i)).year == plan.aar, "idag": mandag + timedelta(days=i) == idag}
                 for i in range(7)]
        merknader = [("rod", f"{dato_tekst(d)}: {navn}")
                     for d, navn in sorted(plan.rode.items()) if mandag <= d <= sondag]
        for p in plan.perioder:
            if p["fra_dato"] <= sondag.isoformat() and p["til_dato"] >= mandag.isoformat():
                tidsrom = periode_tekst(_periodedato(p, "fra_dato"), _periodedato(p, "til_dato"))
                merknader.append((p["type"], f"{periodenavn(p)} ({tidsrom})"))
        for p in plan.planer:
            if p["type"] == "notat" and p["fra_dato"] <= sondag.isoformat() and p["til_dato"] >= mandag.isoformat():
                merknader.append(("notat", f"Notat: {p['tittel']}"))
        ut.append(Ukerad(nr, mandag, dager, baner, merknader, iso_aar))
        mandag += timedelta(days=7)
    return ut


# ---------------- året: tolv små månedskalendere ----------------

@dataclass
class Minidag:
    dato: date
    i_maaneden: bool
    helg: bool = False
    idag: bool = False
    rod: str | None = None          # navnet på den røde dagen
    ferie: bool = False             # skoleferie (for valgt område)
    sperre: bool = False
    advarsel: bool = False
    notat: bool = False
    opptatt: int = 0                # kurs og planlagte aktiviteter denne dagen
    tittel: str = ""                # verktøytips: alt som skjer

    @property
    def kollisjon(self) -> bool:
        return self.opptatt >= 2

    @property
    def klasser(self) -> str:
        if not self.i_maaneden:
            return "utenfor"
        return " ".join(navn for navn, ja in (
            ("helg", self.helg), ("ferie", self.ferie), ("sperre", self.sperre),
            ("advarsel", self.advarsel and not self.sperre), ("rod", self.rod), ("notat", self.notat),
            ("kollisjon", self.kollisjon), ("idag", self.idag)) if ja)


@dataclass
class Miniblokk:
    """Et kurs (én sammenhengende del av en samling) eller en planlagt aktivitet, slik den vises i én uke i én måned."""
    type: str                       # 'kurs' | 'plan'
    tekst: str                      # teksten i blokken (kursnavnet, «2/3» for samling 2 av 3)
    tittel: str                     # hele teksten: navn, datoer, samling, sted (verktøytips og skjermleser)
    kolonne: int                    # 0 = mandag
    lengde: int                     # dager i denne uken i denne måneden
    farge: int | None = None
    kurs_id: int | None = None
    status: str | None = None
    fra_forrige: bool = False       # fortsetter fra uken eller måneden før
    til_neste: bool = False         # fortsetter i uken eller måneden etter
    ekstra: int = 0                 # ledige dager etter blokken på samme linje, som navnet kan fortsette inn i (høyst 3)

    @property
    def spenn(self) -> int:
        """Dagene elementet dekker: blokken pluss plassen til navnet"""
        return self.lengde + self.ekstra

    @property
    def andel(self) -> str:
        """Hvor stor del av elementet som er selve blokken (fargen), til CSS: «40%»"""
        return f"{100 * self.lengde / self.spenn:g}%"


@dataclass
class Miniuke:
    nr: int
    dager: list
    baner: list                     # [[Miniblokk]] - fordelt på linjer så de aldri overlapper


@dataclass
class Minimaaned:
    aar: int
    maaned: int
    uker: list
    opptatt: list                   # datoene i måneden med kurs eller planlagte aktiviteter

    @property
    def navn(self) -> str:
        return MAANEDER[self.maaned - 1]

    @property
    def anker(self) -> str:
        return f"mnd-{self.maaned}"

    @property
    def opptatt_tekst(self) -> str:
        """«Opptatt: 4.–5., 18. og 28. okt» - eller «Ingen kurs eller planer»"""
        if not self.opptatt:
            return "Ingen kurs eller planer"
        deler = [f"{a.day}." if a == b else f"{a.day}.–{b.day}." for a, b in _lop(self.opptatt)]
        return f"Opptatt: {_og(deler)} {_MND_KORT[self.maaned - 1]}"


def _aarslop(plan: Aarsplan) -> list[tuple]:
    """Kursene (hver sammenhengende del av en samling) og de planlagte aktivitetene som (mal, fra, til), der malen er
    (type, tekst, tittel, farge, kurs_id, status)."""
    kursdatoer: dict[int, list[date]] = {}
    info: dict[int, Aktivitet] = {}
    for d, akt in plan.per_dato.items():
        for a in akt:
            if a.type == "kurs":
                kursdatoer.setdefault(a.kurs_id, []).append(d)
                info[a.kurs_id] = a
    ut = []
    for kid, datoer in kursdatoer.items():
        a = info[kid]
        for fra, til in _lop(datoer):
            nr, antall = samling(plan, kid, fra)
            tittel = " · ".join(x for x in (a.tittel, periode_tekst(fra, til),
                                            f"samling {nr} av {antall}" if antall > 1 else "", a.sted or "",
                                            "utkast" if a.status == "utkast" else "") if x)
            tekst = a.tittel + (f" {nr}/{antall}" if antall > 1 else "")
            ut.append((("kurs", tekst, tittel, a.farge, kid, a.status), fra, til))
    for p in plan.planer:
        if p["type"] == "plan":
            fra, til = _periodedato(p, "fra_dato"), _periodedato(p, "til_dato")
            tittel = f"Planlagt: {p['tittel']} · {periode_tekst(fra, til)}" + (f" · {p['sted']}" if p["sted"] else "")
            ut.append((("plan", f"Planlagt: {p['tittel']}", tittel, None, None, None), fra, til))
    return sorted(ut, key=lambda x: (x[1], x[0][1].casefold()))


def _minidag(plan: Aarsplan, d: date, maaned: int, idag: date) -> Minidag:
    if d.month != maaned:
        return Minidag(d, False)
    akt = plan.per_dato.get(d, [])
    dekker = [p for p in plan.perioder if _dekker(p, d)]
    rod = plan.rode.get(d)
    ekstra = ([f"Rød dag: {rod}"] if rod else []) + [periodenavn(p) for p in dekker]
    return Minidag(d, True, helg=d.weekday() >= 5, idag=d == idag, rod=rod,
                   ferie=any(p["type"] == "skoleferie" for p in dekker),
                   sperre=any(p["type"] == "sperre" for p in dekker),
                   advarsel=any(p["type"] == "advarsel" for p in dekker),
                   notat=any(a.type == "notat" for a in akt), opptatt=sum(1 for a in akt if a.opptar),
                   tittel=_tittel(d, akt, ekstra))


def _miniblokkbaner(blokker: list[Miniblokk]) -> list[list[Miniblokk]]:
    """Fordeler blokkene på linjer slik at ingen overlapper: lengste først når de starter samme dag. Hver blokk får vite
    hvor mange ledige dager som følger etter den på linjen (navnet kan fortsette dit)."""
    baner: list[list[Miniblokk]] = []
    for b in sorted(blokker, key=lambda b: (b.kolonne, -b.lengde, b.tekst.casefold())):
        for bane in baner:
            if bane[-1].kolonne + bane[-1].lengde <= b.kolonne:
                bane.append(b)
                break
        else:
            baner.append([b])
    for bane in baner:
        for b, neste in zip(bane, bane[1:] + [None]):
            b.ekstra = 0 if b.til_neste else min(3, (neste.kolonne if neste else 7) - (b.kolonne + b.lengde))
    return baner


def aarskalender(plan: Aarsplan, idag: date) -> list[Minimaaned]:
    """De tolv månedene som små kalendere: uker fra mandag med ukenummer, bare månedens egne datoer, og kursene og de
    planlagte aktivitetene som blokker over dagene de varer. En blokk deles ved ukeskifte og ved månedsskifte."""
    lop = _aarslop(plan)
    ut = []
    for m in range(1, 13):
        forste, siste = date(plan.aar, m, 1), date(plan.aar, m, calendar.monthrange(plan.aar, m)[1])
        uker_ = []
        for uke in calendar.Calendar(firstweekday=0).monthdatescalendar(plan.aar, m):
            blokker = []
            for (type_, tekst, tittel, farge, kurs_id, status), fra, til in lop:
                start, slutt = max(fra, uke[0], forste), min(til, uke[-1], siste)
                if start <= slutt:
                    blokker.append(Miniblokk(type_, tekst, tittel, (start - uke[0]).days, (slutt - start).days + 1,
                                             farge, kurs_id, status, fra < start, til > slutt))
            uker_.append(Miniuke(uke[0].isocalendar()[1], [_minidag(plan, d, m, idag) for d in uke],
                                 _miniblokkbaner(blokker)))
        opptatt = [d for d in _dager(forste, siste) if any(a.opptar for a in plan.per_dato.get(d, []))]
        ut.append(Minimaaned(plan.aar, m, uker_, opptatt))
    return ut


def planliste(plan: Aarsplan) -> list[dict]:
    """De planlagte aktivitetene og notatene som berører året, med ferdig periodetekst - til listen med «Fjern»."""
    return [{**p, "periodetekst": periode_tekst(_periodedato(p, "fra_dato"), _periodedato(p, "til_dato"))}
            for p in plan.planer]


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


# ============================ perioder: skoleferier, sperrer og advarsler ============================

def omraader(con) -> list[str]:
    """Områdene i skjemaet og filteret: Oslo og Vestland, pluss områder admin har brukt (sortert)."""
    brukt = {r["omraade"] for r in con.execute(
        "SELECT DISTINCT omraade FROM kalenderperiode WHERE omraade IS NOT NULL")}
    return sorted(set(STANDARD_OMRAADER) | brukt, key=str.casefold)


def valider_periode(skjema) -> dict:
    """Leser og validerer en periode fra skjemaet. AarsplanFeil ved ugyldig input."""
    type_ = skjema.get("type") or ""
    if type_ not in PERIODETYPER:
        raise AarsplanFeil("Velg hva slags periode det er.")
    navn = _tekst(skjema.get("navn"), "Navn", MAKS_NAVN, paakrevd=True)
    fra = _dato(skjema.get("fra_dato"), "Fra-dato")
    til = _dato(skjema.get("til_dato"), "Til-dato") if (skjema.get("til_dato") or "").strip() else fra
    if til < fra:
        raise AarsplanFeil("Til-dato kan ikke være før fra-dato.")
    if (til - fra).days >= MAKS_DAGER:
        raise AarsplanFeil("En periode kan vare høyst ett år.")
    omraade = _tekst(skjema.get("omraade"), "Område", MAKS_OMRAADE)
    gjelder = skjema.get("gjelder") or "alle"
    if type_ == "skoleferie":
        if not omraade:
            raise AarsplanFeil("Skriv hvilket område skoleferien gjelder (for eksempel Oslo eller Vestland).")
        gjelder = "alle"
    else:
        omraade = None
        if gjelder not in GJELDER:
            raise AarsplanFeil("Ugyldig valg for hvilke kurs perioden gjelder.")
    return {"type": type_, "navn": navn, "omraade": omraade, "fra_dato": fra.isoformat(), "til_dato": til.isoformat(),
            "gjelder": gjelder, "notat": _tekst(skjema.get("notat"), "Notat", MAKS_NOTAT, flerlinjet=True)}


def opprett_periode(con, v: dict, aktor: str) -> int:
    pid = db.sett_inn(
        con, "INSERT INTO kalenderperiode (type, navn, omraade, fra_dato, til_dato, gjelder, notat, opprettet_av) "
             "VALUES (?,?,?,?,?,?,?,?)",
        (v["type"], v["navn"], v["omraade"], v["fra_dato"], v["til_dato"], v["gjelder"], v["notat"], aktor))
    # Hendelsesloggen får aldri fritekst (navn/notat) - bare id, type og datoer.
    db.logg(con, "kalenderperiode_opprettet", {"id": pid, "type": v["type"], "fra_dato": v["fra_dato"],
                                               "til_dato": v["til_dato"]}, aktor=aktor)
    return pid


def hent_periode(con, periode_id: int):
    rad = con.execute("SELECT * FROM kalenderperiode WHERE id=?", (periode_id,)).fetchone()
    return dict(rad) if rad else None


def oppdater_periode(con, periode_id: int, v: dict, aktor: str) -> bool:
    ok = con.execute(
        "UPDATE kalenderperiode SET type=?, navn=?, omraade=?, fra_dato=?, til_dato=?, gjelder=?, notat=? WHERE id=?",
        (v["type"], v["navn"], v["omraade"], v["fra_dato"], v["til_dato"], v["gjelder"], v["notat"],
         periode_id)).rowcount
    if ok:
        db.logg(con, "kalenderperiode_endret", {"id": periode_id, "type": v["type"], "fra_dato": v["fra_dato"],
                                                "til_dato": v["til_dato"]}, aktor=aktor)
    return bool(ok)


def slett_periode(con, periode_id: int, aktor: str) -> dict | None:
    rad = hent_periode(con, periode_id)
    if not rad:
        return None
    con.execute("DELETE FROM kalenderperiode WHERE id=?", (periode_id,))
    db.logg(con, "kalenderperiode_slettet", {"id": periode_id, "type": rad["type"], "fra_dato": rad["fra_dato"]},
            aktor=aktor)
    return rad


def perioder_i_aaret(con, aar: int) -> list[dict]:
    """Alle perioder som berører året (alle områder), til listen med «Endre» og «Slett»."""
    return [{**dict(r), "tekst": periode_tekst(date.fromisoformat(r["fra_dato"]), date.fromisoformat(r["til_dato"])),
             "visning": periodenavn(r)}
            for r in con.execute("SELECT * FROM kalenderperiode WHERE til_dato >= ? AND fra_dato <= ? "
                                 "ORDER BY type, omraade, fra_dato, id", (f"{aar}-01-01", f"{aar}-12-31"))]
