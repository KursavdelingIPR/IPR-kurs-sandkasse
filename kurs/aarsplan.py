"""Årsplan (kurshjul): alle kursdager i et år sammen med planlagte aktiviteter og notater.

  * Året vises som kurshjul (Camilla 15.10.2026, som Excel-arket «Kurshjul»): tolv små månedskalendere med kursdagene fylt med
    kursseriens farge (kursfarger.py; flere kurs samme dag = ruten delt på skrå, annen arrangør svart) og «Uke for uke» ved siden
    av. De gamle visningene «Måneder» og «Uke for uke» er tatt bort (hennes ønske samme dag).
  * Kurs kommer med automatisk (fra kursdag), unntatt avlyste. Utkast vises, merket som utkast.
  * Planlagte kurs (kurs/planlagte_kurs.py, ikke opprettet i systemet) vises og opptar datoene som kurs, med lagret farge og
    lagret samlingsnummer; avlyste planlagte kurs vises ikke. Kurs med annen arrangør er grå, men opptar også datoene.
  * Planlagte aktiviteter (type 'plan') opptar datoene sine. Notater ('notat') er bare påminnelser og opptar ingenting.
  * Overlapp = en dato med minst to kurs/planlagte aktiviteter (vises som perioder: «15.–16. okt»).
  * Røde dager regnes ut (helligdager.py). Skoleferier legges inn av admin per område (Oslo, Vestland ...) -
    aldri faste, nasjonale datoer i koden. Egne perioder er enten en sperre (vises som konflikt) eller en advarsel.
    Ingen av dem hindrer at kurs opprettes; de vises bare her (tabellen kalenderperiode).
"""
import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import db, helligdager, kursfarger, planlagte_kurs

MAANEDER = ("Januar", "Februar", "Mars", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
            "November", "Desember")
_MND_KORT = ("jan", "feb", "mar", "apr", "mai", "jun", "jul", "aug", "sep", "okt", "nov", "des")
TYPER = {"plan": "Planlagt aktivitet", "notat": "Notat"}
PERIODETYPER = {"skoleferie": "Skoleferie", "sperre": "Sperret periode", "advarsel": "Advarsel"}
GJELDER = {"alle": "alle kurs", "fysisk": "fysiske kurs (også hybrid)", "online": "online kurs (også hybrid)"}
STANDARD_OMRAADER = ("Oslo", "Vestland")          # forslag i skjemaet - admin kan skrive andre områder
MAKS_NAVN, MAKS_OMRAADE = 80, 40
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
    plan_id: int | None = None      # planlagt kurs (kurs/planlagte_kurs.py): type 'kurs', men ikke opprettet i systemet
    fast_farge: int | None = None   # planlagte kurs har lagret farge
    ekstern: bool = False           # planlagt kurs med annen arrangør (grå)
    samling_id: int | None = None   # den planlagte samlingen (sjekklisten hører til den)

    @property
    def nokkel(self) -> tuple:
        """Hvilket kurs aktiviteten hører til: ('kurs', id) eller ('plan', id)."""
        return ("plan", self.plan_id) if self.plan_id is not None else ("kurs", self.kurs_id)

    @property
    def farge(self) -> int | None:
        """Kursseriens farge (kursfarger.py) - bare for kurs. Planlagte kurs har lagret farge."""
        if self.fast_farge is not None:
            return self.fast_farge
        return kursfarger.farge(self.tittel, self.spesialistlop) if self.type == "kurs" else None

    @property
    def opptar(self) -> bool:
        """Opptar datoen for oss (kollisjoner). Kurs med annen arrangør gjør det ikke: de teller bare som
        kollisjon med et eget fysisk kurs på samme sted (kollisjon())."""
        return self.type in ("kurs", "plan") and not self.ekstern

    @property
    def fysisk(self) -> bool:
        if self.type == "kurs":
            return self.kurstype in ("fysisk", "hybrid")
        return not planlagte_kurs.er_online(self.sted)

    @property
    def online(self) -> bool:
        """Har aktiviteten en nettdel (online- eller hybridkurs, eller en plan med «Online» som sted)?"""
        if self.type == "kurs":
            return self.kurstype in ("digital", "hybrid")
        return planlagte_kurs.er_online(self.sted)

    @property
    def by(self) -> str | None:
        return planlagte_kurs.by_i(self.sted)

    @property
    def visning(self) -> str:
        if self.plan_id is not None:
            return f"{self.tittel} (planlagt)"
        if self.type == "kurs":
            return f"{self.kursnr} {self.tittel}" if self.kursnr else self.tittel
        return f"{'Planlagt' if self.type == 'plan' else 'Notat'}: {self.tittel}"


@dataclass
class Aarsplan:
    aar: int
    per_dato: dict              # date -> [Aktivitet] (inkl. kantuker utenfor kalenderåret)
    kurs: list                  # per kurs: {id, kursnr, navn, status, sted, type, datoer}
    planer: list                # rader fra planlagt_aktivitet som berører året
    perioder: list = field(default_factory=list)    # rader fra kalenderperiode (skoleferier bare for valgt område)
    rode: dict = field(default_factory=dict)        # date -> navnet på den røde dagen (også kantukene)
    samlinger: dict = field(default_factory=dict)   # kurs_id -> [[datoene i samling 1], [samling 2], ...] (hele kurset)
    plan_samling: dict = field(default_factory=dict)    # (plan_id, dato) -> (nr, antall) for planlagte kurs
    planlagte: dict = field(default_factory=dict)   # plan_id -> kurset fra planlagte_kurs.hent (alle samlingene)
    kursholdere: dict = field(default_factory=dict)  # samling_id (planlagt samling, også den skjulte for et kurs i systemet) -> tekst


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
                      COALESCE(k.sted, CASE WHEN k.type='digital' THEN 'Online' END) AS sted,
                      (SELECT ps.id FROM planlagt_samling ps JOIN planlagt_kurs pk ON pk.id=ps.planlagt_kurs_id
                       WHERE pk.kurs_id=k.id AND ps.kurs_samling_id=kd.samling_id) AS sjekk_samling_id
               FROM kursdag kd JOIN kurs k ON k.id=kd.kurs_id
               WHERE kd.dato BETWEEN ? AND ? AND k.status != 'avlyst'
               ORDER BY kd.dato, k.navn, k.id""", (fra.isoformat(), til.isoformat())):
        d = date.fromisoformat(r["dato"])
        per_dato.setdefault(d, []).append(Aktivitet("kurs", r["navn"], r["sted"], r["id"], r["kursnr"], r["status"],
                                                    r["kurstype"], r["spesialistlop"],
                                                    samling_id=r["sjekk_samling_id"]))   # sjekklisten (rød prikk)
        if d.year == aar:
            kurs.setdefault(r["id"], {"id": r["id"], "plan_id": None, "kursnr": r["kursnr"], "navn": r["navn"],
                                      "status": r["status"], "sted": r["sted"], "type": r["kurstype"],
                                      "datoer": []})["datoer"].append(d)
    # Planlagte kurs (ikke opprettet i systemet) opptar datoene som kurs. Avlyste vises ikke (som avlyste kurs).
    plan_samling: dict = {}
    for s in planlagte_kurs.samlinger_i_perioden(con, fra, til, med_avlyste=False):
        kurstype = "digital" if planlagte_kurs.er_online(s["sted"], s["lokale"]) else "fysisk"
        sted = planlagte_kurs.sted_tekst(s["sted"], s["lokale"]) or None
        a = Aktivitet("kurs", s["visningsnavn"], sted, None, None, s["status"], kurstype, None, s["plan_id"], s["farge"],
                      s["ekstern"], s["samling_id"])
        nummer = (s["nr_vist"], s["antall"]) if s["nr_vist"] is not None and s["antall"] > 1 else (1, 1)
        for d in _dager(max(s["fra"], fra), min(s["til"], til)):
            per_dato.setdefault(d, []).append(a)
            plan_samling[(s["plan_id"], d)] = nummer
            if d.year == aar:
                kurs.setdefault(("plan", s["plan_id"]), {
                    "id": None, "plan_id": s["plan_id"], "kursnr": None, "navn": s["visningsnavn"], "status": s["status"],
                    "sted": sted, "type": kurstype, "ekstern": s["ekstern"], "datoer": []})["datoer"].append(d)
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
    kurs_ider = sorted({a.kurs_id for akt in per_dato.values() for a in akt if a.type == "kurs" and a.plan_id is None})
    plan_ider = sorted({pid for pid, _ in plan_samling})
    return Aarsplan(aar, per_dato, list(kurs.values()), planer, perioder, helligdager.i_perioden(fra, til),
                    _samlinger(con, kurs_ider), plan_samling, planlagte_kurs.hent_flere(con, plan_ider),
                    _kursholdere(con, {a.samling_id for akt in per_dato.values() for a in akt if a.samling_id is not None}))


def _kursholdere(con, samling_ider) -> dict:
    """{samling_id: «Bente (T1), Vanja (T2)»} fra kursholderoversikten og fritekstfeltet, for alle samlingene i året - også de skjulte
    planlagte samlingene som holder sjekklisten og kursholderne til et kurs i systemet (boksen i kurshjulet, Camilla 15.10.2026)."""
    ider = sorted(samling_ider)
    if not ider:
        return {}
    roller = planlagte_kurs.roller_per_samling(con, ider)
    fritekst = {r["id"]: r["kursholdere"] for r in con.execute(
        f"SELECT id, kursholdere FROM planlagt_samling WHERE id IN ({','.join('?' * len(ider))})", tuple(ider))}
    ut = {}
    for sid in ider:
        tekst = planlagte_kurs.rolletekst(roller.get(sid, []), veiledere=False, fritekst=fritekst.get(sid))
        if tekst:
            ut[sid] = tekst
    return ut


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


def samling(plan: "Aarsplan", kurs_id, dato: date) -> tuple[int, int]:
    """(nr, antall) for samlingen `dato` hører til i kurset - (1, 1) når kurset har én samling. `kurs_id` er kursets id
    eller nøkkelen ('kurs', id) / ('plan', id); planlagte kurs har lagret samlingsnummer."""
    if isinstance(kurs_id, tuple):
        if kurs_id[0] == "plan":
            return plan.plan_samling.get((kurs_id[1], dato), (1, 1))
        kurs_id = kurs_id[1]
    samlinger = plan.samlinger.get(kurs_id) or []
    for nr, datoer in enumerate(samlinger, start=1):
        if dato in datoer:
            return nr, len(samlinger)
    return 1, max(1, len(samlinger))


# ============================ visninger ============================

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


def kolliderende(aktiviteter: list) -> list:
    """Kursene/planene en dag som kolliderer med minst ett annet: egne planer alltid med egne kurs og planer, egne kurs når
    begge er fysiske i samme by eller begge online/hybrid (fysisk + online, og Oslo + Bergen, er greit), et kurs med annen
    arrangør bare med et eget fysisk kurs på samme sted (planlagte_kurs.kolliderer). Notater teller aldri."""
    kandidater = [a for a in aktiviteter if a.type in ("kurs", "plan")]
    return [a for a in kandidater if any(b is not a and _kolliderer(a, b) for b in kandidater)]


def _kolliderer(a, b) -> bool:
    if "plan" in (a.type, b.type) and not a.ekstern and not b.ekstern:
        return True
    return planlagte_kurs.kolliderer(a.ekstern, a.fysisk, a.by, b.ekstern, b.fysisk, b.by, a_online=a.online, b_online=b.online)


def overlapp(plan: Aarsplan) -> list[tuple[str, list]]:
    """(datotekst, aktiviteter) for hver dato i året med kurs/planlagte aktiviteter som kolliderer (kolliderende())."""
    ut = []
    for d in sorted(plan.per_dato):
        kollisjon = kolliderende(plan.per_dato[d])
        if d.year == plan.aar and kollisjon:
            ut.append((dato_tekst(d), kollisjon))
    return ut


def overlapp_perioder(plan: Aarsplan) -> list[dict]:
    """Overlappene som sammenhengende perioder med de samme aktivitetene: {tekst: «15.–16. okt», dager, aktiviteter}."""
    ut: list[dict] = []
    for d in sorted(plan.per_dato):
        kollisjon = kolliderende(plan.per_dato[d])
        if d.year != plan.aar or not kollisjon:
            continue
        nokkel = tuple(sorted((a.type, a.nokkel[0], a.nokkel[1] or 0, a.tittel) for a in kollisjon))
        if ut and ut[-1]["nokkel"] == nokkel and ut[-1]["til"] + timedelta(days=1) == d:
            ut[-1]["til"] = d
        else:
            ut.append({"fra": d, "til": d, "nokkel": nokkel, "aktiviteter": kollisjon})
    for o in ut:
        o["tekst"], o["dager"] = periode_tekst(o["fra"], o["til"]), (o["til"] - o["fra"]).days + 1
        o["navn"] = _og([(f"{a.tittel} (planlagt kurs)" if a.plan_id is not None else a.tittel) if a.type == "kurs"
                         else f"Planlagt: {a.tittel}" for a in o["aktiviteter"]])
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
        if k.get("ekstern"):                                # annen arrangør: ikke vår sak
            continue
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

def _lop(datoer: list[date]) -> list[tuple[date, date]]:
    """Sammenhengende datoer som (fra, til)."""
    ut: list[list[date]] = []
    for d in sorted(set(datoer)):
        if ut and ut[-1][1] + timedelta(days=1) == d:
            ut[-1][1] = d
        else:
            ut.append([d, d])
    return [(a, b) for a, b in ut]


# ============================ kurshjulet (Camilla 15.10.2026) ============================
# Som «Kurshjul»-arket i Excel: kalenderen til venstre med kursdagene fylt med kursets farge, og «Uke for uke» til høyre med
# kursnavnene i samme farge. To eller flere kurs samme dag deler ruten på skrå. Kurs med annen arrangør (IPRO, «Oslo») er
# svarte, som i Excel. Holder man over en dag, kommer en boks med kursene den dagen.

EKSTERN_FARGE = "#1d1d1d"          # annen arrangør (IPRO): svart, som i kurshjulet
PLAN_FARGE = "#c9d1d6"             # planlagt aktivitet uten kurs (nøytral)


@dataclass(frozen=True)
class Hjulkurs:
    """Én sammenhengende del av en samling (eller en planlagt aktivitet) i kurshjulet."""
    nokkel: tuple
    navn: str
    samling: str                    # «2. samling av 3» eller ""
    fra: date
    til: date
    sted: str | None
    farge: int | None
    ekstern: bool
    kurs_id: int | None
    plan_id: int | None
    samling_id: int | None
    planlagt: bool                  # planlagt kurs eller aktivitet (ikke opprettet i systemet)
    kursholdere: str | None = None
    utkast: bool = False

    @property
    def periode(self) -> str:
        return periode_tekst(self.fra, self.til)

    @property
    def fargeverdi(self) -> str:
        """CSS-fargen: kursseriens sterke farge (static/kursfarger.css), svart for annen arrangør."""
        if self.ekstern:
            return EKSTERN_FARGE
        return f"var(--kf-{self.farge})" if self.farge is not None else PLAN_FARGE

    @property
    def klasse(self) -> str:
        return "ekstern" if self.ekstern else (f"kursfarge-{self.farge}" if self.farge is not None else "plan")

    @property
    def merknad(self) -> str:
        if self.ekstern:
            return "Annen arrangør (Oslo/IPRO): bare til orientering"
        if self.kurs_id is None and self.plan_id is None:
            return "Planlagt aktivitet"
        if self.planlagt:
            return "Planlagt kurs (ikke opprettet i systemet ennå)"
        return "Utkast" if self.utkast else ""


@dataclass
class Hjuldag:
    dato: date
    i_maaneden: bool
    helg: bool = False
    idag: bool = False
    rod: str | None = None
    ferie: list = field(default_factory=list)       # navnene på skoleferier/perioder som dekker dagen
    kurs: list = field(default_factory=list)        # [Hjulkurs]
    skoleferie: bool = False
    sperre: bool = False                            # sperret periode (skravert)
    advarsel: bool = False
    kollisjon: bool = False                         # ekte dobbeltbooking (kolliderende): rød ramme, som før

    @property
    def bakgrunn(self) -> str:
        """Fargen i ruten: én farge, eller ruten delt på skrå i like deler (to kurs = to trekanter)."""
        farger = [k.fargeverdi for k in self.kurs]
        if not farger:
            return ""
        if len(farger) == 1:
            return f"background:{farger[0]}"
        andel = 100 / len(farger)
        deler = [f"{f} {i * andel:.2f}% {(i + 1) * andel:.2f}%" for i, f in enumerate(farger)]
        return f"background:linear-gradient(135deg, {', '.join(deler)})"

    @property
    def klasser(self) -> str:
        return " ".join(k for k, ja in (("helg", self.helg), ("idag", self.idag), ("rod", self.rod), ("ferie", self.skoleferie),
                                        ("sperre", self.sperre), ("advarsel", self.advarsel and not self.sperre),
                                        ("harkurs", self.kurs), ("flere", len(self.kurs) > 1), ("kollisjon", self.kollisjon)) if ja)


@dataclass
class Hjulmaaned:
    maaned: int
    uker: list                      # [(ukenr, [Hjuldag x 7])]

    @property
    def navn(self) -> str:
        return MAANEDER[self.maaned - 1]


@dataclass
class Hjuluke:
    fra_nr: int
    til_nr: int
    fra: date
    til: date
    kurs: list                      # [Hjulkurs] som berører uka
    ferie: list                     # navnene på skoleferier/perioder og røde dager i uka

    @property
    def tekst(self) -> str:
        return f"Uke {self.fra_nr}" if self.fra_nr == self.til_nr else f"Uke {self.fra_nr}–{self.til_nr}"


def _hjulkurs(plan: Aarsplan) -> list[Hjulkurs]:
    """Kursene (hver sammenhengende del av en samling) og de planlagte aktivitetene som Hjulkurs, sortert på dato."""
    datoer: dict[tuple, list[date]] = {}
    paa_dag: dict[tuple, Aktivitet] = {}
    for d, akt in plan.per_dato.items():
        for a in akt:
            if a.type == "kurs":
                datoer.setdefault(a.nokkel, []).append(d)
                paa_dag[(a.nokkel, d)] = a
    ut = []
    for n, dager in datoer.items():
        for fra, til in _lop(dager):
            a = paa_dag[(n, fra)]
            nr, antall = samling(plan, n, fra)
            holdere = plan.kursholdere.get(a.samling_id)
            ut.append(Hjulkurs(n, a.tittel, f"{nr}. samling av {antall}" if antall > 1 else "", fra, til, a.sted, a.farge,
                               a.ekstern, a.kurs_id, a.plan_id, a.samling_id, a.plan_id is not None, holdere,
                               a.status == "utkast"))
    for p in plan.planer:
        if p["type"] == "plan":
            ut.append(Hjulkurs(("aktivitet", p["id"]), p["tittel"], "", _periodedato(p, "fra_dato"),
                               _periodedato(p, "til_dato"), p["sted"], None, False, None, None, None, True))
    return sorted(ut, key=lambda k: (k.fra, k.ekstern, k.navn.casefold()))


def kurshjul(plan: Aarsplan, idag: date) -> tuple[list[Hjulmaaned], list[Hjuluke]]:
    """Kalenderen (tolv måneder, uker fra mandag) og «Uke for uke» for året. Uker uten kurs etter hverandre slås sammen («Uke 25–33»),
    som i kurshjulet; ferier og merknader i dem samles og vises i en nedtrekksmeny (Camilla 15.10.2026: «er ikke kurs»)."""
    alle = _hjulkurs(plan)
    paa_dag: dict[date, list] = {}
    for k in alle:
        for d in _dager(k.fra, k.til):
            paa_dag.setdefault(d, []).append(k)

    def ferie(d: date) -> list:
        return [periodenavn(p) for p in plan.perioder if _dekker(p, d)]

    maaneder = []
    for m in range(1, 13):
        uker_ = []
        for uke in calendar.Calendar(firstweekday=0).monthdatescalendar(plan.aar, m):
            dager = []
            for d in uke:
                if d.month != m:
                    dager.append(Hjuldag(d, False))
                    continue
                dekker = [p for p in plan.perioder if _dekker(p, d)]
                dager.append(Hjuldag(d, True, helg=d.weekday() >= 5, idag=d == idag, rod=plan.rode.get(d), ferie=ferie(d),
                                     kurs=paa_dag.get(d, []), skoleferie=any(p["type"] == "skoleferie" for p in dekker),
                                     sperre=any(p["type"] == "sperre" for p in dekker),
                                     advarsel=any(p["type"] == "advarsel" for p in dekker),
                                     kollisjon=bool(kolliderende(plan.per_dato.get(d, [])))))
            uker_.append((uke[0].isocalendar()[1], dager))
        maaneder.append(Hjulmaaned(m, uker_))

    rader: list[Hjuluke] = []
    for nr in range(1, antall_uker(plan.aar) + 1):
        fra = date.fromisocalendar(plan.aar, nr, 1)
        til = fra + timedelta(days=6)
        kurs = [k for k in alle if k.fra <= til and k.til >= fra]
        navn = list(dict.fromkeys([n for d in _dager(fra, til) for n in ferie(d)]
                                  + [plan.rode[d] for d in _dager(fra, til) if d in plan.rode]))
        forrige = rader[-1] if rader else None
        if not kurs and forrige and not forrige.kurs:       # uker uten kurs slås sammen; ferier og merknader samles (nedtrekk)
            forrige.til_nr, forrige.til = nr, til
            forrige.ferie = list(dict.fromkeys(forrige.ferie + navn))
            continue
        rader.append(Hjuluke(nr, nr, fra, til, kurs, navn))
    return maaneder, rader


def fargeforklaring(plan: Aarsplan) -> list[dict]:
    """Kursene med flere samlinger i året, med fargen og ALLE samlingene (også i andre år), så samme kurs er lett å finne
    igjen: {navn, farge, kurs_id, plan_id, samlinger: [«2. samling 18.–21. jan»]}. Kurs med annen arrangør er grå og
    står ikke her."""
    info: dict[tuple, Aktivitet] = {}
    for d in sorted(plan.per_dato):
        for a in plan.per_dato[d]:
            if a.type == "kurs" and not a.ekstern and d.year == plan.aar:
                info.setdefault(a.nokkel, a)
    ut = []
    for (art, nr_id), a in info.items():
        if art == "plan":
            k = plan.planlagte.get(nr_id)
            samlinger = [(s["nr_vist"] or i, s["fra"], s["til"]) for i, s in enumerate(k["samlinger"], 1)] if k else []
            if not k or k["antall"] < 2:
                continue
        else:
            grupper = plan.samlinger.get(nr_id) or []
            if len(grupper) < 2:
                continue
            samlinger = [(i, min(g), max(g)) for i, g in enumerate(grupper, 1)]
        tekster = [f"{nr}. samling {periode_tekst(fra, til)}" + (f" {fra.year}" if fra.year == til.year != plan.aar else "")
                   for nr, fra, til in samlinger]
        forste = min((fra for _, fra, til in samlinger if til.year >= plan.aar), default=samlinger[0][1])
        ut.append((forste, {"navn": a.tittel, "farge": a.farge, "kurs_id": a.kurs_id, "plan_id": a.plan_id,
                            "samlinger": tekster}))
    return [x for _, x in sorted(ut, key=lambda x: (x[0], x[1]["navn"].casefold()))]


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
