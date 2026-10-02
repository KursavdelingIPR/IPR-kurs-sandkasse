"""Kursgjennomføring: samlinger og kursdager fra skjemaet «Opprett/rediger kurs», og visning av dem.

En samling er én eller flere sammenhengende kursdager med felles klokkeslett og timetall, f.eks. «Samling 1»
(20.–23.09.2027, 09:00–16:00, 6 timer), et éndagskurs eller en veiledningsdag. En enkelt dag i samlingen kan avvike
(egne klokkeslett, timer eller merknad, f.eks. «Veiledningsdag») eller tas ut. Hver dato blir en kursdag (QR, oppmøte,
påminnelser og kursbevis er per kursdag).

Denne modulen er ren logikk uten database: lesing og kontroll av skjemaet, forslaget til påmeldingsfrist og tekstene
som viser datoene. Lagringen står i db.lagre_samlinger.
"""
import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

MAKS_DAGER_PER_SAMLING = 31          # lengre er nesten alltid en tastefeil - lange utdanninger er flere samlinger
_KLOKKESLETT = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_SAMLINGSFELT = re.compile(r"^s-(\d+)-(id|navn|fra|til|start|slutt|timer)$")
_DAGFELT = re.compile(r"^s-(\d+)-d-(\d{4}-\d{2}-\d{2})-(start|slutt|timer|merknad|fjern)$")


@dataclass
class Dagavvik:
    """Det én kursdag gjør annerledes enn samlingen sin. Tomme felt betyr «som samlingen»."""
    start_kl: str | None = None
    slutt_kl: str | None = None
    timer: float | None = None
    merknad: str | None = None
    fjernet: bool = False


@dataclass
class Samling:
    fra: date | None
    til: date | None
    start_kl: str
    slutt_kl: str
    timer: float | None
    navn: str | None = None
    id: int | None = None                                   # eksisterende samling (redigering)
    avvik: dict[date, Dagavvik] = field(default_factory=dict)

    def alle_datoer(self) -> list[date]:
        """Alle datoer fra og med fra-dato til og med til-dato (tom til-dato = én dag)."""
        if not self.fra:
            return []
        til = self.til or self.fra
        return [self.fra + timedelta(days=i) for i in range(max((til - self.fra).days + 1, 0))]

    def datoer(self) -> list[date]:
        """Kursdagene: alle datoene unntatt dem som er tatt ut."""
        return [d for d in self.alle_datoer() if not self.avvik.get(d, Dagavvik()).fjernet]


# ---------------- lesing av skjemaet ----------------

def _dato(verdi: str) -> date | None:
    verdi = (verdi or "").strip()
    return date.fromisoformat(verdi) if verdi else None


def _timer(verdi: str) -> float | None:
    verdi = (verdi or "").strip().replace(",", ".")
    return float(verdi) if verdi else None


def fra_skjema(skjema) -> tuple[list["Samling"], list[str]]:
    """Samlingene i skjemaet (feltene s-<rad>-…), i radrekkefølge, og feil i verdiene. Helt tomme rader (typisk den ekstra,
    tomme raden) hoppes over. Avvik for enkeltdager (s-<rad>-d-<dato>-…) følger raden sin."""
    rader: dict[int, dict] = {}
    dager: dict[int, dict[str, dict]] = {}
    for nokkel in skjema:
        if m := _SAMLINGSFELT.match(nokkel):
            rader.setdefault(int(m[1]), {})[m[2]] = skjema.get(nokkel, "")
        elif m := _DAGFELT.match(nokkel):
            dager.setdefault(int(m[1]), {}).setdefault(m[2], {})[m[3]] = skjema.get(nokkel, "")
    samlinger, feil = [], []
    for n in sorted(rader):
        rad = rader[n]
        if not any((rad.get(f) or "").strip() for f in ("navn", "fra", "til")):
            continue
        navn = (rad.get("navn") or "").strip() or None
        hvem = navn or f"Samling {len(samlinger) + 1}"
        try:
            fra, til = _dato(rad.get("fra")), _dato(rad.get("til"))
        except ValueError:
            feil.append(f"{hvem}: datoene må være gyldige.")
            continue
        try:
            timer = _timer(rad.get("timer"))
        except ValueError:
            feil.append(f"{hvem}: timer per dag må være et tall.")
            timer = None
        avvik = {}
        for iso, felt in dager.get(n, {}).items():
            try:
                timer_dag = _timer(felt.get("timer"))
            except ValueError:
                feil.append(f"{hvem}: timer for {kort_dato(date.fromisoformat(iso))} må være et tall.")
                timer_dag = None
            avvik[date.fromisoformat(iso)] = Dagavvik(
                start_kl=(felt.get("start") or "").strip() or None, slutt_kl=(felt.get("slutt") or "").strip() or None,
                timer=timer_dag, merknad=(felt.get("merknad") or "").strip() or None, fjernet=bool(felt.get("fjern")))
        id_ = (rad.get("id") or "").strip()
        samlinger.append(Samling(fra=fra, til=til, start_kl=(rad.get("start") or "").strip(),
                                 slutt_kl=(rad.get("slutt") or "").strip(), timer=timer, navn=navn,
                                 id=int(id_) if id_.isdigit() else None, avvik=avvik))
    return samlinger, feil


def fra_datoliste(datoer: list[str], start_kl: str, slutt_kl: str, timer: float, *,
                  dag_for_dag: bool = False) -> list[Samling]:
    """Den gamle formen (én ISO-dato per linje): sammenhengende datoer blir én samling med kursets klokkeslett. Med
    dag_for_dag blir hver dato sin egen samling - slik som kurs med delt faktura i den gamle formen, der delfakturaen
    var per kursdag (samme regel som migrering 9)."""
    samlinger: list[Samling] = []
    for d in sorted({date.fromisoformat(x) for x in datoer}):
        if not dag_for_dag and samlinger and samlinger[-1].til + timedelta(days=1) == d:
            samlinger[-1].til = d
        else:
            samlinger.append(Samling(fra=d, til=d, start_kl=start_kl, slutt_kl=slutt_kl, timer=timer))
    return samlinger


def datoer_iso(samlinger: list[Samling]) -> list[str]:
    """Alle kursdagene i samlingene, sortert (ISO)."""
    return sorted(d.isoformat() for s in samlinger for d in s.datoer())


# ---------------- rader i skjemaet (visning og ny visning etter en feil) ----------------

def _tall(t: float | None) -> str:
    return "" if t is None else (f"{t:g}")


def _dagrader(fra: date | None, til: date | None, avvik: dict[str, dict]) -> list[dict]:
    if not fra:
        return []
    til = til if til and til >= fra else fra
    antall = min((til - fra).days + 1, MAKS_DAGER_PER_SAMLING)
    ut = []
    for d in (fra + timedelta(days=i) for i in range(antall)):
        a = avvik.get(d.isoformat(), {})
        ut.append({"iso": d.isoformat(), "tekst": f"{ukedag(d)} {kort_dato(d)}", "start": a.get("start", ""),
                   "slutt": a.get("slutt", ""), "timer": a.get("timer", ""), "merknad": a.get("merknad", ""),
                   "fjern": bool(a.get("fjern"))})
    return ut


def rad(*, id_="", navn="", fra: date | None = None, til: date | None = None, start="09:00", slutt="16:00", timer="6",
        avvik: dict[str, dict] | None = None, varighet: int | None = None) -> dict:
    """Én samling som rad i skjemaet (strenger). `varighet` (antall dager i originalen) brukes ved duplisering."""
    dager = _dagrader(fra, til, avvik or {})
    return {"id": str(id_ or ""), "navn": navn or "", "fra": fra.isoformat() if fra else "",
            "til": til.isoformat() if til and fra and til != fra else "", "start": start, "slutt": slutt,
            "timer": timer, "varighet": varighet, "dager": dager,
            "har_avvik": any(d["start"] or d["slutt"] or d["timer"] or d["merknad"] or d["fjern"] for d in dager)}


def rader_for_samlinger(samlinger: list[Samling]) -> list[dict]:
    """Lagrede samlinger som rader i skjemaet, med avvikene for enkeltdager."""
    ut = []
    for s in samlinger:
        avvik = {d.isoformat(): {"start": a.start_kl or "", "slutt": a.slutt_kl or "", "timer": _tall(a.timer),
                                 "merknad": a.merknad or "", "fjern": a.fjernet} for d, a in s.avvik.items()}
        ut.append(rad(id_=s.id, navn=s.navn, fra=s.fra, til=s.til, start=s.start_kl, slutt=s.slutt_kl,
                      timer=_tall(s.timer), avvik=avvik))
    return ut


def rader_for_duplikat(samlinger: list[Samling]) -> list[dict]:
    """Samlingene i et kurs som dupliseres: navn, klokkeslett, timer og antall dager - uten datoer (de velges på nytt)."""
    return [rad(navn=s.navn, start=s.start_kl, slutt=s.slutt_kl, timer=_tall(s.timer), varighet=len(s.alle_datoer()))
            for s in samlinger]


def rader_fra_innsendt(skjema) -> list[dict]:
    """Det admin sendte inn, som rader - slik at skjemaet vises igjen etter en feil uten at noe går tapt."""
    rader: dict[int, dict] = {}
    avvik: dict[int, dict[str, dict]] = {}
    for nokkel in skjema:
        if m := _SAMLINGSFELT.match(nokkel):
            rader.setdefault(int(m[1]), {})[m[2]] = skjema.get(nokkel, "")
        elif m := _DAGFELT.match(nokkel):
            avvik.setdefault(int(m[1]), {}).setdefault(m[2], {})[m[3]] = skjema.get(nokkel, "")
    ut = []
    for n in sorted(rader):
        r = rader[n]
        try:
            fra, til = _dato(r.get("fra")), _dato(r.get("til"))
        except ValueError:
            fra = til = None
        ny = rad(id_=r.get("id", ""), navn=r.get("navn", ""), fra=fra, til=til, start=r.get("start", ""),
                 slutt=r.get("slutt", ""), timer=r.get("timer", ""), avvik=avvik.get(n, {}))
        ny["fra"], ny["til"] = r.get("fra", ""), r.get("til", "")          # nøyaktig det som ble skrevet
        ut.append(ny)
    return ut


# ---------------- kontroll ----------------

def kontroller(samlinger: list[Samling], *, krev_kursdag: bool = True) -> list[str]:
    """Norske feilmeldinger for det som ikke kan lagres. Avvik for datoer utenfor samlingen (datoene er endret etter at
    dagen fikk avvik) ignoreres."""
    feil: list[str] = []
    if krev_kursdag and not any(s.datoer() for s in samlinger):
        feil.append("Legg inn minst én kursdag (fra-dato på en samling).")
    brukt: dict[date, str] = {}
    for nr, s in enumerate(samlinger, 1):
        hvem = s.navn or f"Samling {nr}"
        if not s.fra:
            feil.append(f"{hvem}: fyll inn fra-dato.")
            continue
        if s.til and s.til < s.fra:
            feil.append(f"{hvem}: til-datoen er før fra-datoen.")
            continue
        if len(s.alle_datoer()) > MAKS_DAGER_PER_SAMLING:
            feil.append(f"{hvem}: en samling kan vare høyst {MAKS_DAGER_PER_SAMLING} dager. Del den opp i flere samlinger.")
            continue
        if not _gyldig_tid(s.start_kl, s.slutt_kl):
            feil.append(f"{hvem}: fyll inn gyldige klokkeslett, og «Til kl.» må være etter «Fra kl.».")
        if s.timer is None or not 0 <= s.timer <= 24:
            feil.append(f"{hvem}: timer per dag må være et tall mellom 0 og 24.")
        datoer = set(s.alle_datoer())
        for d, a in sorted(s.avvik.items()):
            if d not in datoer or a.fjernet:
                continue
            if not _gyldig_tid(a.start_kl or s.start_kl, a.slutt_kl or s.slutt_kl):
                feil.append(f"{hvem}, {kort_dato(d)}: fyll inn gyldige klokkeslett, og «Til kl.» må være etter «Fra kl.».")
            if a.timer is not None and not 0 <= a.timer <= 24:
                feil.append(f"{hvem}, {kort_dato(d)}: timer må være et tall mellom 0 og 24.")
        if s.alle_datoer() and not s.datoer():
            feil.append(f"{hvem}: alle dagene er tatt ut. Fjern samlingen i stedet.")
        for d in s.datoer():
            if d in brukt:
                feil.append(f"{kort_dato(d)} er med i både {brukt[d]} og {hvem}.")
            brukt[d] = hvem
    return feil


def _gyldig_tid(start: str, slutt: str) -> bool:
    return bool(_KLOKKESLETT.match(start or "") and _KLOKKESLETT.match(slutt or "") and start < slutt)


# ---------------- påmeldingsfrist ----------------

def en_kalendermaaned_for(d: date) -> date:
    """Samme dag én kalendermåned før (15.10 -> 15.09). Finnes ikke dagen i måneden før, brukes siste dag (31.03 -> 28/29.02)."""
    aar, maaned = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    return date(aar, maaned, min(d.day, calendar.monthrange(aar, maaned)[1]))


def foreslaatt_frist(forste_kursdag: date, idag: date) -> date:
    """Én kalendermåned før første kursdag. Er den allerede passert (kurset starter om mindre enn en måned), foreslås
    dagen før første kursdag, så påmeldingen ikke er stengt fra starten av."""
    frist = en_kalendermaaned_for(forste_kursdag)
    return frist if frist >= idag else max(idag, forste_kursdag - timedelta(days=1))


# ---------------- visning ----------------

_UKEDAGER = ("ma.", "ti.", "on.", "to.", "fr.", "lø.", "sø.")


def kort_dato(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def ukedag(d: date) -> str:
    return _UKEDAGER[d.weekday()]


def periode(fra: date, til: date) -> str:
    """15.10.2027 · 20.–23.09.2027 · 30.09.–02.10.2027 · 30.12.2027–02.01.2028"""
    if fra == til:
        return kort_dato(fra)
    if fra.year != til.year:
        return f"{kort_dato(fra)}–{kort_dato(til)}"
    if fra.month != til.month:
        return f"{fra.strftime('%d.%m.')}–{kort_dato(til)}"
    return f"{fra.strftime('%d.')}–{kort_dato(til)}"


def timer_tekst(t: float) -> str:
    """1 time · 6 timer · 7,5 timer"""
    return f"{t:g}".replace(".", ",") + (" time" if t == 1 else " timer")


def oppsummering(samlinger: list[Samling]) -> str:
    """«3 samlinger · 12 kursdager · 20.09.2027–11.05.2028 · 72 timer». Tom når det ikke er noen kursdager.
    Samme tekst som skjemaet viser mens admin skriver (app.js)."""
    med_dager = [s for s in samlinger if s.datoer()]
    if not med_dager:
        return ""
    datoer = sorted(d for s in med_dager for d in s.datoer())
    timer = 0.0
    for s in med_dager:
        for d in s.datoer():
            egne = s.avvik.get(d, Dagavvik()).timer
            timer += egne if egne is not None else (s.timer or 0)
    return " · ".join([f"{len(med_dager)} samling{'er' if len(med_dager) != 1 else ''}",
                       f"{len(datoer)} kursdag{'er' if len(datoer) != 1 else ''}",
                       periode(datoer[0], datoer[-1]), timer_tekst(timer)])


def _felt(rad, navn: str):
    try:
        return rad[navn]
    except (KeyError, IndexError):
        return None


def datoliste(datoer: list[date]) -> str:
    """Datoene som korte perioder: «20.–23.09.2027», eller med hull «20.–21.09.2027 og 24.09.2027»."""
    lop: list[list[date]] = []
    for d in sorted(datoer):
        if lop and lop[-1][-1] + timedelta(days=1) == d:
            lop[-1].append(d)
        else:
            lop.append([d])
    deler = [periode(x[0], x[-1]) for x in lop]
    return deler[0] if len(deler) == 1 else f"{', '.join(deler[:-1])} og {deler[-1]}"


def visning(dager, kurs) -> list[dict]:
    """Kursdagene (rader fra db.kursdager, med samlingen sin) gruppert per samling til visning på kurssiden og i e-post:
    navn, datoene, klokkeslett og dagene som avviker (egne klokkeslett eller merknad). En samling uten navn heter
    «Samling N» (N = plassen i rekkefølgen) når kurset har flere samlinger. Rader uten samling (eksempeldata) vises som
    enkeltdager uten navn. En ekstradeltaker som bare er på noen samlinger (db.paameldingens_kursdager), har `samling_nr` og
    `samling_flere` på radene: da er N samlingens nummer i HELE kurset, så «Samling 3» heter «Samling 3» også når bare den vises."""
    grupper: list[dict] = []
    per_samling: dict = {}
    for d in sorted(dager, key=lambda x: str(x["dato"])):
        dato = d["dato"] if isinstance(d["dato"], date) else date.fromisoformat(d["dato"])
        sid = _felt(d, "samling_id")
        start = d["start_kl"] or _felt(d, "samling_start_kl") or kurs["start_kl"]
        slutt = d["slutt_kl"] or _felt(d, "samling_slutt_kl") or kurs["slutt_kl"]
        g = per_samling.get(sid) if sid is not None else None
        if g is None:
            g = {"sid": sid, "navn": _felt(d, "samling_navn"), "nr": _felt(d, "samling_nr"),
                 "start": _felt(d, "samling_start_kl") or start, "slutt": _felt(d, "samling_slutt_kl") or slutt, "dager": []}
            grupper.append(g)
            if sid is not None:
                per_samling[sid] = g
        g["dager"].append({"dato": dato, "start": start, "slutt": slutt, "merknad": _felt(d, "merknad"), "id": _felt(d, "id")})
    flere = sum(1 for g in grupper if g["sid"] is not None) > 1 or any(_felt(d, "samling_flere") for d in dager)
    ut = []
    for nr, g in enumerate(grupper, 1):
        nr = g["nr"] or nr                  # kursets eget samlingsnummer når bare noen av samlingene vises
        avvik = [{"dato": f"{ukedag(x['dato'])} {kort_dato(x['dato'])}",
                  "tid": f"{x['start']}–{x['slutt']}" if (x["start"], x["slutt"]) != (g["start"], g["slutt"]) else "",
                  "merknad": x["merknad"] or ""}
                 for x in g["dager"] if x["merknad"] or (x["start"], x["slutt"]) != (g["start"], g["slutt"])]
        navn = g["navn"] or (f"Samling {nr}" if flere and g["sid"] is not None else "")
        ut.append({"navn": navn, "periode": datoliste([x["dato"] for x in g["dager"]]),
                   "tid": f"{g['start']}–{g['slutt']}", "antall_dager": len(g["dager"]), "avvik": avvik,
                   "sid": g["sid"], "ider": [x["id"] for x in g["dager"]]})
    return ut
