"""Kalenderen (egen fane i menyen): kursoversikt (standard), måned og uke.

En hendelse er én samling av et kurs (samlingene fra Opprett kurs, tabellen samling), merket «Samling 2 av 3» eller med
samlingens navn. Er en dag tatt ut av en samling (f.eks. helgen), vises samlingen som flere hendelser med samme nummer.
Kursdager uten samling (eldre data) grupperes som sammenhengende datoer.

  * Kursoversikten viser samlingene kronologisk per måned, fra og med denne måneden: dato til venstre, så kursnavn,
    samling, sted, ansvarlig og påmeldte. Den første kommende samlingen er merket «Neste kurs», og samlinger som går
    samme dag som et annet kurs, sier fra. Tidligere måneder ligger for seg under «Tidligere kurs».
  * I månedsvisningen vises en hendelse som én blokk over dagene den varer - også over flere uker - og blokkene fordeles
    på linjer (baner) så de aldri overlapper. På mobil vises måneden som en liste.

Fargen følger kursserien (kursfarger.py): samlinger i samme utdanning har samme farge i alle visningene og i årsplanen.
Avlyste kurs er grå og overstrøket, utkast har stiplet kant. Røde dager (helligdager.py) markeres i rutenettet.
"""
import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import db, helligdager, kursfarger

FORMAT = {"fysisk": "Fysisk", "digital": "Online", "hybrid": "Hybrid"}
UKEDAGER = ("Man", "Tir", "Ons", "Tor", "Fre", "Lør", "Søn")
UKEDAGER_LANGE = ("Mandag", "Tirsdag", "Onsdag", "Torsdag", "Fredag", "Lørdag", "Søndag")
MAANEDER = ("Januar", "Februar", "Mars", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November",
            "Desember")
STEDER = ("bergen", "oslo", "online")
_MND_KORT = ("jan", "feb", "mar", "apr", "mai", "jun", "jul", "aug", "sep", "okt", "nov", "des")
_UKEDAG_KORT = ("man", "tir", "ons", "tor", "fre", "lør", "søn")


@dataclass
class Hendelse:
    kurs_id: int
    kursnr: int
    navn: str
    type: str
    sted: str | None
    status: str
    ansvarlig: str | None
    fra: date
    til: date
    start_kl: str | None
    slutt_kl: str | None
    nr: int = 1                 # samling nr ...
    antall: int = 1             # ... av antall samlinger i kurset
    kode: str | None = None
    kapasitet: int | None = None
    bekreftet: int = 0          # fullverdige påmeldte (bare antallet - aldri hvem); ekstradeltakere er ikke med
    ekstradeltakere: int = 0    # ekstradeltakere satt opp PÅ DENNE SAMLINGEN: er på kurset, men tar ikke plass og teller ikke som påmeldte
    spesialistlop: str | None = None
    samling_navn: str | None = None     # samlingens navn fra Opprett kurs (valgfritt, f.eks. «Veiledningsdag»)

    @property
    def dager(self) -> int:
        return (self.til - self.fra).days + 1

    @property
    def samling_tekst(self) -> str:
        if self.antall <= 1:
            return ""
        return f"{self.samling_navn} ({self.nr} av {self.antall})" if self.samling_navn else f"Samling {self.nr} av {self.antall}"

    @property
    def tid(self) -> str:
        return f"{self.start_kl}–{self.slutt_kl}" if self.start_kl and self.slutt_kl else ""

    @property
    def format_tekst(self) -> str:
        return FORMAT.get(self.type, self.type)

    @property
    def sted_tekst(self) -> str:
        return (self.sted or "") if self.type != "digital" else ""

    @property
    def farge(self) -> int:
        return kursfarger.farge(self.navn, self.spesialistlop)

    @property
    def periode(self) -> str:
        """«18.09.2026» eller «18.–21.09.2026» / «30.09.–02.10.2026»"""
        if self.fra == self.til:
            return self.fra.strftime("%d.%m.%Y")
        if self.fra.month == self.til.month and self.fra.year == self.til.year:
            return f"{self.fra.strftime('%d.')}–{self.til.strftime('%d.%m.%Y')}"
        if self.fra.year == self.til.year:
            return f"{self.fra.strftime('%d.%m.')}–{self.til.strftime('%d.%m.%Y')}"
        return f"{self.fra.strftime('%d.%m.%Y')}–{self.til.strftime('%d.%m.%Y')}"

    @property
    def kort_periode(self) -> str:
        return kort_periode(self.fra, self.til)

    @property
    def ukedager(self) -> str:
        """«tor» eller «søn–man · 2 dager»"""
        if self.fra == self.til:
            return _UKEDAG_KORT[self.fra.weekday()]
        return f"{_UKEDAG_KORT[self.fra.weekday()]}–{_UKEDAG_KORT[self.til.weekday()]} · {self.dager} dager"

    @property
    def sted_visning(self) -> str:
        """Sted slik kursoversikten viser det: «IPR, Bergen», «Online», «Online · Zoom», «Hybrid · Zoom / IPR Bergen»."""
        sted = " ".join((self.sted or "").split())
        if self.type == "digital":
            return sted if "online" in sted.casefold() else "Online" + (f" · {sted}" if sted else "")
        if self.type == "hybrid":
            return "Hybrid" + (f" · {sted}" if sted else "")
        return sted or "Sted ikke satt"

    @property
    def paameldte_tekst(self) -> str:
        """«6/16 påmeldte», eller «5 påmeldte» når kurset ikke har kapasitet (ordet står i db.PAAMELDINGSSTATUSER)"""
        flertall = db.PAAMELDINGSSTATUSER_FLERTALL["paameldt"].lower()
        if self.kapasitet:
            tekst = f"{self.bekreftet}/{self.kapasitet} {flertall}"
        else:
            tekst = f"{self.bekreftet} {db.PAAMELDINGSSTATUSER['paameldt'].lower() if self.bekreftet == 1 else flertall}"
        if self.ekstradeltakere:       # vises ved siden av, aldri som en del av tallet: «6/16 påmeldte + 2 ekstradeltakere»
            navn = db.PAAMELDINGSSTATUSER if self.ekstradeltakere == 1 else db.PAAMELDINGSSTATUSER_FLERTALL
            tekst += f" + {self.ekstradeltakere} {navn['ekstradeltaker'].lower()}"
        return tekst

    @property
    def beskrivelse(self) -> str:
        """Kort tekst til verktøytips (title) og skjermlesere: alt i én linje."""
        deler = [self.navn, self.periode + (f" kl. {self.tid}" if self.tid else ""), self.format_tekst]
        if self.sted_tekst:
            deler.append(self.sted_tekst)
        if self.samling_tekst:
            deler.append(self.samling_tekst)
        if self.ansvarlig:
            deler.append(f"Ansvarlig: {self.ansvarlig}")
        if self.status in ("avlyst", "utkast"):
            deler.append(self.status.capitalize())
        return " · ".join(deler)


def periode_tekst(fra: date, til: date) -> str:
    """5.–11. oktober 2026 · 28. september – 4. oktober 2026 · 28. desember 2026 – 3. januar 2027"""
    maaned = lambda d: MAANEDER[d.month - 1].lower()  # noqa: E731
    if fra.year != til.year:
        return f"{fra.day}. {maaned(fra)} {fra.year} – {til.day}. {maaned(til)} {til.year}"
    if fra.month != til.month:
        return f"{fra.day}. {maaned(fra)} – {til.day}. {maaned(til)} {til.year}"
    return f"{fra.day}.–{til.day}. {maaned(til)} {til.year}"


def kort_periode(fra: date, til: date) -> str:
    """«18. sep», «21.–22. sep», «30. sep – 2. okt»"""
    if fra == til:
        return f"{fra.day}. {_MND_KORT[fra.month - 1]}"
    if (fra.year, fra.month) == (til.year, til.month):
        return f"{fra.day}.–{til.day}. {_MND_KORT[til.month - 1]}"
    return f"{fra.day}. {_MND_KORT[fra.month - 1]} – {til.day}. {_MND_KORT[til.month - 1]}"


def om_dager(dato: date, idag: date) -> str:
    """«i dag», «i morgen», «om 6 dager»"""
    n = (dato - idag).days
    return "i dag" if n == 0 else "i morgen" if n == 1 else f"om {n} dager"


def _vilkar(ansvarlig: int, status: str, sted: str) -> tuple[list[str], list]:
    """Samme filtre og sted-regel som kurslisten på Oversikten."""
    vilkar, parametre = [], []
    if ansvarlig:
        vilkar.append("k.ansvarlig_admin_id = ?")
        parametre.append(ansvarlig)
    if status:
        vilkar.append("k.status = ?")
        parametre.append(status)
    if sted == "bergen":
        vilkar.append("LOWER(COALESCE(k.sted,'')) LIKE '%bergen%'")
    elif sted == "oslo":
        vilkar.append("LOWER(COALESCE(k.sted,'')) LIKE '%oslo%'")
    elif sted == "online":
        vilkar.append("(k.type IN ('digital','hybrid') OR LOWER(COALESCE(k.sted,'')) LIKE '%zoom%' "
                      "OR LOWER(COALESCE(k.sted,'')) LIKE '%online%')")
    return vilkar, parametre


def hent(con, fra: date, til: date, *, ansvarlig: int = 0, status: str = "", sted: str = "") -> list[Hendelse]:
    """Hendelsene (samlingene) som har minst én dag fra og med `fra` til og med `til`, sortert på dato og navn.
    Samlingene nummereres ut fra ALLE kursets samlinger, så «Samling 2 av 3» stemmer også når bare én av dem vises."""
    vilkar, parametre = _vilkar(ansvarlig, status, sted)
    kurs = {r["id"]: r for r in con.execute(
        f"""SELECT k.id, k.kursnr, k.kode, k.navn, k.type, k.sted, k.status, k.start_kl, k.slutt_kl, k.kapasitet,
                   k.spesialistlop, ab.navn AS ansvarlig,
                   (SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=k.id AND p.status='bekreftet'
                                                       AND p.ekstradeltaker_ts IS NULL) AS bekreftet
            FROM kurs k LEFT JOIN admin_bruker ab ON ab.id=k.ansvarlig_admin_id
            WHERE EXISTS (SELECT 1 FROM kursdag kd WHERE kd.kurs_id=k.id AND kd.dato BETWEEN ? AND ?)
            {''.join(' AND ' + v for v in vilkar)}""", (fra.isoformat(), til.isoformat(), *parametre))}
    if not kurs:
        return []
    # Ekstradeltakere PER SAMLING: en ekstradeltaker uten utvalg er på hele kurset (teller på alle samlingene), en med utvalg bare på de
    # valgte. Hver samling har sin egen oppmøteliste, så kortet skal vise dem som faktisk er satt opp på akkurat den samlingen.
    ekstra_hele: dict[int, int] = {}
    ekstra_samling: dict[tuple[int, int], int] = {}
    for r in con.execute(
            f"""SELECT p.kurs_id, es.samling_id FROM paamelding p LEFT JOIN ekstradeltaker_samling es ON es.paamelding_id=p.id
                WHERE p.kurs_id IN ({','.join('?' * len(kurs))}) AND p.status='bekreftet' AND p.ekstradeltaker_ts IS NOT NULL""",
            tuple(kurs)):
        if r["samling_id"] is None:
            ekstra_hele[r["kurs_id"]] = ekstra_hele.get(r["kurs_id"], 0) + 1
        else:
            nokkel = (r["kurs_id"], r["samling_id"])
            ekstra_samling[nokkel] = ekstra_samling.get(nokkel, 0) + 1
    dager: dict[int, list] = {}
    for d in con.execute(
            f"""SELECT kd.kurs_id, kd.dato, kd.start_kl, kd.slutt_kl, kd.samling_id, s.navn AS samling_navn,
                       s.start_kl AS samling_start_kl, s.slutt_kl AS samling_slutt_kl
                FROM kursdag kd LEFT JOIN samling s ON s.id=kd.samling_id
                WHERE kd.kurs_id IN ({','.join('?' * len(kurs))}) ORDER BY kd.kurs_id, kd.dato""", tuple(kurs)):
        dager.setdefault(d["kurs_id"], []).append((date.fromisoformat(d["dato"]), d))
    ut = []
    for kurs_id, kd in dager.items():
        k = kurs[kurs_id]
        samlinger = _samlinger(kd)
        for nr, samling in enumerate(samlinger, start=1):
            for lop in _sammenhengende(samling):
                forste, siste = lop[0][0], lop[-1][0]
                if siste < fra or forste > til:
                    continue
                f = lop[0][1]
                sid = f["samling_id"]       # en kursdag uten samling (eldre data) har bare «hele kurset»-ekstradeltakerne
                ekstra = ekstra_hele.get(kurs_id, 0) + (ekstra_samling.get((kurs_id, sid), 0) if sid is not None else 0)
                ut.append(Hendelse(kurs_id=kurs_id, kursnr=k["kursnr"], navn=k["navn"], type=k["type"], sted=k["sted"],
                                   status=k["status"], ansvarlig=k["ansvarlig"], fra=forste, til=siste,
                                   start_kl=f["start_kl"] or f["samling_start_kl"] or k["start_kl"],
                                   slutt_kl=f["slutt_kl"] or f["samling_slutt_kl"] or k["slutt_kl"],
                                   nr=nr, antall=len(samlinger), kode=k["kode"], kapasitet=k["kapasitet"],
                                   bekreftet=k["bekreftet"] or 0, ekstradeltakere=ekstra,
                                   spesialistlop=k["spesialistlop"],
                                   samling_navn=f["samling_navn"]))
    return sorted(ut, key=lambda h: (h.fra, h.navn.casefold(), h.kurs_id))


def _samlinger(kd: list[tuple]) -> list[list[tuple]]:
    """Kursdagene [(dato, rad)] gruppert per lagret samling, i den rekkefølgen samlingene starter. Dager uten samling
    (eldre data) grupperes som sammenhengende datoer."""
    grupper: dict = {}
    forrige_uten = None
    for dato, d in kd:
        if d["samling_id"] is not None:
            nokkel = ("samling", d["samling_id"])
        elif forrige_uten and forrige_uten[1] + timedelta(days=1) == dato:
            nokkel = forrige_uten[0]
        else:
            nokkel = ("dager", dato)
        if d["samling_id"] is None:
            forrige_uten = (nokkel, dato)
        grupper.setdefault(nokkel, []).append((dato, d))
    return list(grupper.values())


def _sammenhengende(samling: list[tuple]) -> list[list[tuple]]:
    """En samling med dager tatt ut (f.eks. helgen) blir flere sammenhengende deler."""
    deler: list[list[tuple]] = []
    for dato, d in samling:
        if deler and deler[-1][-1][0] + timedelta(days=1) == dato:
            deler[-1].append((dato, d))
        else:
            deler.append([(dato, d)])
    return deler


# ---------------- kursoversikt (standard) ----------------

@dataclass
class Oversiktsrad:
    h: Hendelse
    tidligere: bool = False     # siste dag er passert
    pagar: bool = False         # i dag er en av dagene
    neste: bool = False         # den første kommende samlingen (verken avlyst eller utkast): «Neste kurs»
    samme_dag: list = field(default_factory=list)      # [(kursnavn, «5. okt»)] - andre kurs de samme dagene


@dataclass
class Oversiktsmaaned:
    aar: int
    maaned: int
    rader: list

    @property
    def tittel(self) -> str:
        return f"{MAANEDER[self.maaned - 1]} {self.aar}"

    @property
    def kort(self) -> str:
        return _MND_KORT[self.maaned - 1]

    @property
    def anker(self) -> str:
        return f"m-{self.aar}-{self.maaned:02}"

    @property
    def antall(self) -> int:
        """Samlinger i måneden som ikke er avlyst"""
        return sum(1 for r in self.rader if r.h.status != "avlyst")

    @property
    def gjennomforte(self) -> list:
        """Samlingene som er over (i denne måneden: vises sammenfoldet over resten)"""
        return [r for r in self.rader if r.tidligere]

    @property
    def aktuelle(self) -> list:
        return [r for r in self.rader if not r.tidligere]


@dataclass
class Kursoversikt:
    maaneder: list              # denne måneden og framover til siste måned med kurs - også måneder uten kurs
    tidligere: list             # månedene før denne, nyeste først (radene også nyeste først)
    neste: Oversiktsrad | None
    neste_om: str               # «om 6 dager»
    i_dag: list                 # samlingene som pågår i dag (ikke avlyste)


def rader(hendelser: list[Hendelse], idag: date) -> list[Oversiktsrad]:
    """Hendelsene som rader i kursoversikten: tidligere/pågår, og andre kurs som går samme dag (avlyste teller ikke)."""
    ut = [Oversiktsrad(h, tidligere=h.til < idag, pagar=h.fra <= idag <= h.til) for h in hendelser]
    aktive = [r for r in ut if r.h.status != "avlyst"]
    for r in aktive:
        for a in aktive:
            if a.h.kurs_id != r.h.kurs_id and a.h.fra <= r.h.til and r.h.fra <= a.h.til:
                r.samme_dag.append((a.h.navn, kort_periode(max(a.h.fra, r.h.fra), min(a.h.til, r.h.til))))
    return ut


def oversikt(hendelser: list[Hendelse], idag: date) -> Kursoversikt:
    """Kursene kronologisk per måned: fra og med denne måneden og så langt det finnes kurs (tomme måneder er med, så
    hullene synes), og tidligere måneder for seg. En samling som pågår, står i denne måneden selv om den startet før.
    Lange utdanninger med samlinger fram i tid står som aktuelle: de kommende samlingene ligger i sine måneder."""
    alle = rader(hendelser, idag)
    neste = next((r for r in alle if r.h.fra > idag and r.h.status not in ("avlyst", "utkast")), None)
    if neste:
        neste.neste = True
    denne = (idag.year, idag.month)
    per_maaned: dict[tuple, list] = {}
    for r in alle:
        nokkel = (r.h.fra.year, r.h.fra.month)
        if r.h.til >= idag and nokkel < denne:
            nokkel = denne
        per_maaned.setdefault(nokkel, []).append(r)
    maaneder, (aar, m) = [], denne
    siste = max([denne, *per_maaned])
    while (aar, m) <= siste:
        maaneder.append(Oversiktsmaaned(aar, m, per_maaned.get((aar, m), [])))
        aar, m = (aar + 1, 1) if m == 12 else (aar, m + 1)
    tidligere = [Oversiktsmaaned(a, mm, list(reversed(r))) for (a, mm), r in sorted(per_maaned.items(), reverse=True)
                 if (a, mm) < denne]
    return Kursoversikt(maaneder, tidligere, neste, om_dager(neste.h.fra, idag) if neste else "",
                        [r for r in alle if r.pagar and r.h.status != "avlyst"])


# ---------------- månedsvisning ----------------

@dataclass
class Segment:
    """Den delen av en hendelse som faller i én uke: kolonne (0 = mandag) og lengde i dager."""
    hendelse: Hendelse
    kolonne: int
    lengde: int
    fra_forrige: bool          # hendelsen startet uken før
    til_neste: bool            # hendelsen fortsetter uken etter


@dataclass
class Dag:
    dato: date
    i_maaneden: bool
    idag: bool
    helg: bool
    rod_dag: str | None          # navnet på helligdagen


@dataclass
class Uke:
    ukenr: int
    dager: list[Dag]
    baner: list[list[Segment]] = field(default_factory=list)


def _baner(segmenter: list[Segment]) -> list[list[Segment]]:
    """Fordeler segmentene på linjer slik at ingen overlapper: lengste først når de starter samme dag."""
    baner: list[list[Segment]] = []
    for s in sorted(segmenter, key=lambda s: (s.kolonne, -s.lengde, s.hendelse.navn.casefold())):
        for bane in baner:
            if bane[-1].kolonne + bane[-1].lengde <= s.kolonne:
                bane.append(s)
                break
        else:
            baner.append([s])
    return baner


def _dag(d: date, maaned: int | None, idag: date, rode: dict[date, str]) -> Dag:
    return Dag(dato=d, i_maaneden=maaned is None or d.month == maaned, idag=d == idag, helg=d.weekday() >= 5,
               rod_dag=rode.get(d))


def uker(aar: int, maaned: int, hendelser: list[Hendelse], idag: date) -> list[Uke]:
    """Månedens uker (mandag-søndag, med dager fra nabomånedene), med hendelsene fordelt på baner."""
    kalender = calendar.Calendar(firstweekday=0).monthdatescalendar(aar, maaned)
    rode = helligdager.i_perioden(kalender[0][0], kalender[-1][-1])
    ut = []
    for uke in kalender:
        segmenter = []
        for h in hendelser:
            start, slutt = max(h.fra, uke[0]), min(h.til, uke[-1])
            if start <= slutt:
                segmenter.append(Segment(h, (start - uke[0]).days, (slutt - start).days + 1, h.fra < uke[0],
                                         h.til > uke[-1]))
        ut.append(Uke(uke[0].isocalendar()[1], [_dag(d, maaned, idag, rode) for d in uke], _baner(segmenter)))
    return ut


# ---------------- ukevisning ----------------

@dataclass
class Ukedag:
    dag: Dag
    navn: str
    hendelser: list[tuple[Hendelse, int]]   # (hendelse, dagnummer i hendelsen)


def uke(dato: date, hendelser: list[Hendelse], idag: date) -> list[Ukedag]:
    """De sju dagene i uken som inneholder `dato`, med hendelsene hver dag («dag 2 av 4»)."""
    mandag = dato - timedelta(days=dato.weekday())
    rode = helligdager.i_perioden(mandag, mandag + timedelta(days=6))
    ut = []
    for i in range(7):
        d = mandag + timedelta(days=i)
        ut.append(Ukedag(_dag(d, None, idag, rode), UKEDAGER_LANGE[i],
                         [(h, (d - h.fra).days + 1) for h in hendelser if h.fra <= d <= h.til]))
    return ut
