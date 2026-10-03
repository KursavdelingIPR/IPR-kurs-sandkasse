"""Kursholderoversikten for planlagte kurs (Kalender → Kursholdere), slik fanen «Kommende kurs» i Kurskalender-Excelen er
bygget: én rad per samling og per veiledningsdag, én kolonne per kursholder, og rollen i ruta (Camilla 02.10.2026).

  * Rollene som i Excel: T/T1/T2 trainer, F facilitator, B back-up, O opplæring (kursholder), V/v veileder, bv back-up
    veileder, «-» ikke med.
  * Psybase: rød = ikke lagt inn i Psybase, svart = bekreftet og booket (skriftfargen i Excel). Lagres per rute.
  * En rolle på selve samlingen har dag = ''; en rolle på en veiledningsdag har dag = datoen (en av samlingens
    veiledningsdager).
  * Dobbeltbooking: samme kursholder på to ulike samlinger som overlapper i tid (ikke «-»), markeres i begge rutene.
  * Bare planlagte kurs. Avlyste kurs er ikke med. Hendelsesloggen får bare id-er og koder, aldri navn.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from . import db, kurskalender, planlagte_kurs

ROLLER = (("T", "Trainer"), ("T1", "Trainer 1"), ("T2", "Trainer 2"), ("F", "Facilitator"), ("B", "Back-up"),
          ("O", "Opplæring (kursholder)"), ("V", "Veileder"), ("v", "Veileder"), ("bv", "Back-up veileder"),
          ("-", "Ikke med"))
KODER = frozenset(k for k, _ in ROLLER)
MAKS_NAVN, MAKS_FULLT_NAVN = 40, 120


class KursholderFeil(ValueError):
    """Ugyldig kursholder eller rolle (meldingen kan vises til brukeren)."""


def _tekst(verdi, navn: str, maks: int, *, paakrevd: bool = False) -> str | None:
    tekst = " ".join(str(verdi or "").split())
    if paakrevd and not tekst:
        raise KursholderFeil(f"{navn} må fylles ut.")
    if len(tekst) > maks:
        raise KursholderFeil(f"{navn} kan være høyst {maks} tegn.")
    return tekst or None


# ============================ kursholderne ============================

def liste(con, *, med_inaktive: bool = False) -> list[dict]:
    rader = con.execute("SELECT * FROM kursholder" + ("" if med_inaktive else " WHERE aktiv=1")
                        + " ORDER BY rekkefolge, navn, id").fetchall()
    return [dict(r) for r in rader]


def valider(skjema) -> dict:
    return {"navn": _tekst(skjema.get("navn"), "Navnet", MAKS_NAVN, paakrevd=True),
            "fullt_navn": _tekst(skjema.get("fullt_navn"), "Fullt navn", MAKS_FULLT_NAVN)}


def _navn_er_tatt(con, navn: str, unntatt: int | None = None) -> bool:
    return any(r["id"] != unntatt and r["navn"].casefold() == navn.casefold()
               for r in con.execute("SELECT id, navn FROM kursholder"))


def opprett(con, v: dict, aktor: str, *, rekkefolge: int | None = None) -> int:
    if _navn_er_tatt(con, v["navn"]):
        raise KursholderFeil(f"Det finnes alt en kursholder som heter «{v['navn']}».")
    if rekkefolge is None:
        rekkefolge = con.execute("SELECT COALESCE(MAX(rekkefolge), 0) + 10 FROM kursholder").fetchone()[0]
    kid = db.sett_inn(con, "INSERT INTO kursholder (navn, fullt_navn, rekkefolge) VALUES (?,?,?)",
                      (v["navn"], v["fullt_navn"], rekkefolge))
    db.logg(con, "kursholder_opprettet", {"id": kid}, aktor=aktor)
    return kid


def oppdater(con, kid: int, v: dict, aktiv: bool, aktor: str) -> bool:
    if not con.execute("SELECT 1 FROM kursholder WHERE id=?", (kid,)).fetchone():
        return False
    if _navn_er_tatt(con, v["navn"], unntatt=kid):
        raise KursholderFeil(f"Det finnes alt en kursholder som heter «{v['navn']}».")
    con.execute("UPDATE kursholder SET navn=?, fullt_navn=?, aktiv=? WHERE id=?",
                (v["navn"], v["fullt_navn"], 1 if aktiv else 0, kid))
    db.logg(con, "kursholder_endret", {"id": kid, "aktiv": 1 if aktiv else 0}, aktor=aktor)
    return True


# ============================ rollene ============================

def sett_rolle(con, samling_id: int, dag: str, kursholder_id: int, rolle: str | None, psybase: bool, aktor: str) -> str:
    """Setter (eller fjerner, rolle tom) rollen til en kursholder på en samling (dag = '') eller en av samlingens
    veiledningsdager (dag = ISO-dato). Returnerer «lagt_til», «endret», «fjernet» eller «uendret»."""
    samling = con.execute("SELECT veiledningsdager FROM planlagt_samling WHERE id=?", (samling_id,)).fetchone()
    if not samling:
        raise KursholderFeil("Samlingen finnes ikke.")
    dag = (dag or "").strip()
    if dag and dag not in (samling["veiledningsdager"] or "").split(","):
        raise KursholderFeil("Datoen er ikke en veiledningsdag i samlingen.")
    if not con.execute("SELECT 1 FROM kursholder WHERE id=?", (kursholder_id,)).fetchone():
        raise KursholderFeil("Kursholderen finnes ikke.")
    rolle = (rolle or "").strip()
    if rolle and rolle not in KODER:
        raise KursholderFeil("Ukjent rolle.")
    finnes = con.execute("SELECT rolle, psybase FROM planlagt_rolle WHERE samling_id=? AND dag=? AND kursholder_id=?",
                         (samling_id, dag, kursholder_id)).fetchone()
    psy = 1 if psybase else 0
    if not rolle:
        if not finnes:
            return "uendret"
        con.execute("DELETE FROM planlagt_rolle WHERE samling_id=? AND dag=? AND kursholder_id=?",
                    (samling_id, dag, kursholder_id))
        utfall = "fjernet"
    elif finnes and (finnes["rolle"], finnes["psybase"]) == (rolle, psy):
        return "uendret"
    else:
        naa = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if finnes:
            con.execute("UPDATE planlagt_rolle SET rolle=?, psybase=?, endret=?, endret_av=? "
                        "WHERE samling_id=? AND dag=? AND kursholder_id=?",
                        (rolle, psy, naa, aktor, samling_id, dag, kursholder_id))
            utfall = "endret"
        else:
            db.sett_inn(con, "INSERT INTO planlagt_rolle (samling_id, dag, kursholder_id, rolle, psybase, endret, endret_av) "
                             "VALUES (?,?,?,?,?,?,?)", (samling_id, dag, kursholder_id, rolle, psy, naa, aktor))
            utfall = "lagt_til"
    db.logg(con, "planlagt_rolle_endret", {"samling_id": samling_id, "dag": dag or None, "kursholder_id": kursholder_id,
                                           "rolle": rolle or None, "psybase": psy if rolle else None}, aktor=aktor)
    return utfall


# ============================ oversikten ============================

@dataclass
class Celle:
    rolle: str
    psybase: int
    kollisjon: list = field(default_factory=list)     # «Kursnavn (datoer)» for de andre samlingene samtidig


@dataclass
class Rad:
    plan_id: int
    samling_id: int
    dag: str                       # '' = samlingen, ellers veiledningsdagen
    fra: date
    til: date
    dato_tekst: str
    kursnavn: str
    samling: str                   # «2. samling» / «Veiledningsdag» / ...
    farge: int
    ekstern: bool
    kurs_id: int | None = None     # satt når samlingen er et kurs i systemet (lenken går til kurset)
    celler: dict = field(default_factory=dict)        # {kursholder_id: Celle}

    @property
    def anker(self) -> str:
        return f"rad-{self.samling_id}" + (f"-{self.dag}" if self.dag else "")

    @property
    def dager(self) -> set:
        return {self.fra + timedelta(days=i) for i in range((self.til - self.fra).days + 1)}


def _kort_dato(fra: date, til: date) -> str:
    return kurskalender.kort_periode(fra, til) if hasattr(kurskalender, "kort_periode") else fra.strftime("%d.%m")


def matrise(con, idag: date, *, fra: date | None = None, kursholder_id: int | None = None) -> dict:
    """Oversikten: {"rader": [Rad], "maaneder": [(tittel, [Rad])], "kolonner": [kursholder], "dager": {id: antall},
    "kollisjoner": antall ruter}. Radene er samlingene og veiledningsdagene fra og med `fra` (standard: første dag i
    inneværende måned), sortert på dato. Med kursholder_id bare radene der den personen har en rolle (ikke «-»)."""
    fra = fra or idag.replace(day=1)
    samlinger = planlagte_kurs.samlinger_i_perioden(con, fra, date(9999, 12, 31), med_avlyste=False, med_knyttede=True)
    rader: list[Rad] = []
    for s in samlinger:
        rader.append(Rad(s["plan_id"], s["samling_id"], "", s["fra"], s["til"], _kort_dato(s["fra"], s["til"]),
                         s["visningsnavn"], s["samlingsnavn"] or "", s["farge"], s["ekstern"], s["kurs_id"]))
        for d in s["veiledningsdager"]:
            if d >= fra:
                rader.append(Rad(s["plan_id"], s["samling_id"], d.isoformat(), d, d, _kort_dato(d, d),
                                 s["visningsnavn"], "Veiledningsdag", s["farge"], s["ekstern"], s["kurs_id"]))
    rader.sort(key=lambda r: (r.fra, r.dag != "", r.kursnavn.casefold(), r.samling_id))
    per_rad = {(r.samling_id, r.dag): r for r in rader}
    if per_rad:
        ider = sorted({r.samling_id for r in rader})
        for rr in con.execute(f"SELECT samling_id, dag, kursholder_id, rolle, psybase FROM planlagt_rolle "
                              f"WHERE samling_id IN ({','.join('?' * len(ider))})", tuple(ider)):
            rad = per_rad.get((rr["samling_id"], rr["dag"]))
            if rad:
                rad.celler[rr["kursholder_id"]] = Celle(rr["rolle"], rr["psybase"])
    _merk_kollisjoner(rader)
    if kursholder_id is not None:
        rader = [r for r in rader if kursholder_id in r.celler and r.celler[kursholder_id].rolle != "-"]
    brukt = {kid for r in rader for kid in r.celler}
    kolonner = [k for k in liste(con, med_inaktive=True) if k["aktiv"] or k["id"] in brukt]
    dager: dict[int, set] = {}
    for r in rader:
        for kid, c in r.celler.items():
            if c.rolle != "-":
                dager.setdefault(kid, set()).update(r.dager)
    maaneder: list = []
    for r in rader:
        tittel = f"{kurskalender.MAANEDER[r.fra.month - 1]} {r.fra.year}"
        if not maaneder or maaneder[-1][0] != tittel:
            maaneder.append((tittel, []))
        maaneder[-1][1].append(r)
    return {"rader": rader, "maaneder": maaneder, "kolonner": kolonner, "fra": fra,
            "dager": {kid: len(d) for kid, d in dager.items()},
            "kollisjoner": sum(1 for r in rader for c in r.celler.values() if c.kollisjon)}


def _merk_kollisjoner(rader: list) -> None:
    """Samme kursholder (ikke «-») på to ulike samlinger med minst én felles dag: begge rutene får den andre samlingen
    i c.kollisjon. Samlingen og dens egne veiledningsdager kolliderer ikke med hverandre."""
    per_person: dict[int, list] = {}
    for r in rader:
        for kid, c in r.celler.items():
            if c.rolle != "-":
                per_person.setdefault(kid, []).append((r, c))
    for liste_ in per_person.values():
        for i, (a, ca) in enumerate(liste_):
            for b, cb in liste_[i + 1:]:
                if a.samling_id != b.samling_id and a.fra <= b.til and b.fra <= a.til:
                    ca.kollisjon.append(f"{b.kursnavn} ({b.dato_tekst})")
                    cb.kollisjon.append(f"{a.kursnavn} ({a.dato_tekst})")
