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

Planlagte kurs (kurs/planlagte_kurs.py) kommer med når kalendersiden ber om dem (med_planlagte=True): de er merket
«Planlagt», har sin lagrede farge, lenker til siden for det planlagte kurset og har ingen påmeldte. Kurs med annen arrangør
er grå. Andre sider som bruker hent(), får dem ikke (de står ikke på Oversikten).
"""
import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import db, helligdager, kursfarger, planlagte_kurs

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
    plan_id: int | None = None          # planlagt kurs (kurs/planlagte_kurs.py): ikke opprettet i systemet ennå
    fast_farge: int | None = None       # planlagte kurs har lagret farge
    ekstern: bool = False               # planlagt kurs med annen arrangør (grå)
    arrangor: str | None = None
    tema: str | None = None
    lokale: str | None = None
    kursholdere: str | None = None
    veiledningsdager: tuple = ()
    sjekk: str | None = None            # «Må sjekkes» (samlingens eller kursets)
    samling_id: int | None = None       # planlagt samling (sjekklisten hører til den)
    sjekkliste: object = None           # sjekklister.Sjekkliste, satt av sjekklister.legg_paa (kalendersiden)
    har_mal: bool = False               # kurset har en sjekklistemal (da kan sjekklisten lages)
    sjekk_plan_id: int | None = None    # planlagt kurs som holder sjekklisten: plan_id, eller det skjulte for et kurs i systemet
    kurs_samling_id: int | None = None  # samlingen i kurset (kurs i systemet)
    booking: dict = field(default_factory=dict)   # {type: rad} fra booking.for_samlinger, satt av sjekklister.legg_paa

    @property
    def planlagt(self) -> bool:
        return self.plan_id is not None

    @property
    def nokkel(self) -> tuple:
        """Hvilket kurs hendelsen hører til: ('kurs', id) eller ('plan', id)."""
        return ("plan", self.plan_id) if self.planlagt else ("kurs", self.kurs_id)

    @property
    def fysisk(self) -> bool:
        """Har kurset et fysisk sted (fysisk eller hybrid)?"""
        return self.type in ("fysisk", "hybrid")

    @property
    def online(self) -> bool:
        """Har kurset en nettdel (online eller hybrid)?"""
        return self.type in ("digital", "hybrid")

    @property
    def by(self) -> str | None:
        """Byen kurset går i (små bokstaver), når den står i sted/lokale."""
        return planlagte_kurs.by_i(self.sted, self.lokale)

    def kolliderer_med(self, annen: "Hendelse") -> bool:
        """Dobbeltbooking? Egne kurs når begge er fysiske i samme by eller begge online/hybrid; annen arrangør bare fysisk på
        samme sted (planlagte_kurs.kolliderer)."""
        return planlagte_kurs.kolliderer(self.ekstern, self.fysisk, self.by, annen.ekstern, annen.fysisk, annen.by,
                                         a_online=self.online, b_online=annen.online)

    @property
    def klasser(self) -> str:
        """CSS-klassene for status: «aapen», «avlyst planlagt», «planlagt ekstern» ..."""
        return " ".join([self.status] + (["planlagt"] if self.planlagt and self.status != "planlagt" else [])
                        + (["ekstern"] if self.ekstern else []))

    @property
    def veiledning_tekst(self) -> str:
        """«veiledningsdag 18. jan», «veiledningsdager 1. og 5. feb»"""
        if not self.veiledningsdager:
            return ""
        deler = [f"{d.day}." for d in self.veiledningsdager]
        mnd = {d.month for d in self.veiledningsdager}
        if len(mnd) > 1:
            deler = [f"{d.day}. {_MND_KORT[d.month - 1]}" for d in self.veiledningsdager]
        tekst = deler[0] if len(deler) == 1 else ", ".join(deler[:-1]) + " og " + deler[-1]
        if len(mnd) == 1:
            tekst += f" {_MND_KORT[self.veiledningsdager[0].month - 1]}"
        return f"veiledningsdag{'er' if len(deler) > 1 else ''} {tekst}"

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
        return self.fast_farge if self.fast_farge is not None else kursfarger.farge(self.navn, self.spesialistlop)

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
        """Sted slik kursoversikten viser det: «IPR, Bergen», «Online», «Online · Zoom», «Hybrid · Zoom / IPR Bergen».
        Planlagte kurs: by og lokale, «Oslo · Paleet»."""
        if self.planlagt:
            return planlagte_kurs.sted_tekst(self.sted, self.lokale) or "Sted ikke satt"
        sted = " ".join((self.sted or "").split())
        if self.type == "digital":
            return sted if "online" in sted.casefold() else "Online" + (f" · {sted}" if sted else "")
        if self.type == "hybrid":
            return "Hybrid" + (f" · {sted}" if sted else "")
        return sted or "Sted ikke satt"

    @property
    def paameldte_tekst(self) -> str:
        """«6/16 påmeldte», eller «5 påmeldte» når kurset ikke har kapasitet (ordet står i db.PAAMELDINGSSTATUSER).
        Planlagte kurs har ingen påmelding: tomt."""
        if self.planlagt:
            return ""
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
        if self.planlagt:
            deler = [self.navn, self.periode, "Planlagt kurs (ikke opprettet i systemet)", self.sted_visning]
            deler += [x for x in (self.samling_tekst, self.veiledning_tekst, self.kursholdere,
                                  f"Annen arrangør: {self.arrangor}" if self.ekstern else "",
                                  "Avlyst" if self.status == "avlyst" else "") if x]
            return " · ".join(deler)
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


def _planlagte(con, fra: date, til: date, *, ansvarlig: int, status: str, sted: str) -> list[Hendelse]:
    """De planlagte kursenes samlinger i perioden som hendelser. De har ingen ansvarlig (filteret utelater dem), og status
    er «planlagt» eller «avlyst» (andre statusfiltre utelater dem)."""
    if ansvarlig or status not in ("", "planlagt", "avlyst"):
        return []
    ut = []
    for s in planlagte_kurs.samlinger_i_perioden(con, fra, til, sted=sted):
        if status and s["status"] != status:
            continue
        nummerert = s["nr_vist"] is not None and s["antall"] > 1
        ut.append(Hendelse(
            kurs_id=0, kursnr=0, navn=s["visningsnavn"],
            type="digital" if planlagte_kurs.er_online(s["sted"], s["lokale"]) else "fysisk",
            sted=s["sted"], status=s["status"], ansvarlig=None, fra=s["fra"], til=s["til"], start_kl=None, slutt_kl=None,
            nr=s["nr_vist"] if nummerert else 1, antall=s["antall"] if nummerert else 1, samling_navn=s["navn"],
            plan_id=s["plan_id"], fast_farge=s["farge"], ekstern=s["ekstern"], arrangor=s["arrangor"], tema=s["tema"],
            lokale=s["lokale"], kursholdere=s["kursholdere_tekst"], veiledningsdager=s["veiledningsdager"],
            sjekk=s["sjekk"] or s["kurs_sjekk"], samling_id=s["samling_id"], sjekk_plan_id=s["plan_id"]))
    return ut


def _knytt_til_sjekklister(con, hendelser: list[Hendelse]) -> None:
    """Kursene i systemet med et skjult planlagt kurs (sjekklister.synk_kurs): sjekklisten og kursholderne fra det."""
    sids = [h.kurs_samling_id for h in hendelser if h.kurs_samling_id is not None]
    if not sids:
        return
    lenker = {r["kurs_samling_id"]: (r["plan_id"], r["id"], r["kursholdere"]) for r in con.execute(
        f"""SELECT ps.kurs_samling_id, ps.id, ps.kursholdere, pk.id AS plan_id FROM planlagt_samling ps
            JOIN planlagt_kurs pk ON pk.id=ps.planlagt_kurs_id
            WHERE pk.kurs_id IS NOT NULL AND ps.kurs_samling_id IN ({','.join('?' * len(sids))})""", tuple(sids))}
    roller = planlagte_kurs.roller_per_samling(con, [v[1] for v in lenker.values()])
    for h in hendelser:
        if h.kurs_samling_id in lenker:
            h.sjekk_plan_id, h.samling_id, fritekst = lenker[h.kurs_samling_id]
            h.kursholdere = planlagte_kurs.rolletekst(roller.get(h.samling_id, []), veiledere=False, fritekst=fritekst)


def hent(con, fra: date, til: date, *, ansvarlig: int = 0, status: str = "", sted: str = "",
         med_planlagte: bool = False) -> list[Hendelse]:
    """Hendelsene (samlingene) som har minst én dag fra og med `fra` til og med `til`, sortert på dato og navn.
    Samlingene nummereres ut fra ALLE kursets samlinger, så «Samling 2 av 3» stemmer også når bare én av dem vises.
    med_planlagte=True tar med de planlagte kursene (bare kalendersiden)."""
    planlagte = _planlagte(con, fra, til, ansvarlig=ansvarlig, status=status, sted=sted) if med_planlagte else []
    if status == "planlagt":
        return sorted(planlagte, key=lambda h: (h.fra, h.navn.casefold(), h.nokkel))
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
        return sorted(planlagte, key=lambda h: (h.fra, h.navn.casefold(), h.nokkel))
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
                                   samling_navn=f["samling_navn"], kurs_samling_id=sid))
    _knytt_til_sjekklister(con, ut)
    return sorted(ut + planlagte, key=lambda h: (h.fra, h.navn.casefold(), h.nokkel))


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
    """Hendelsene som rader i kursoversikten: tidligere/pågår, og andre kurs som går samme dag (avlyste teller ikke, et
    fysisk kurs og et online-/hybridkurs samtidig er greit, det samme er fysiske kurs i ulike byer, og et kurs med annen
    arrangør teller bare når begge er fysiske på samme sted: planlagte_kurs.kolliderer)."""
    ut = [Oversiktsrad(h, tidligere=h.til < idag, pagar=h.fra <= idag <= h.til) for h in hendelser]
    aktive = [r for r in ut if r.h.status != "avlyst"]
    for r in aktive:
        treff: dict[tuple, list] = {}          # et annet kurs med to samlinger samme dager nevnes én gang: «(11.–12. nov)»
        for a in aktive:
            if a.h.nokkel != r.h.nokkel and a.h.fra <= r.h.til and r.h.fra <= a.h.til and r.h.kolliderer_med(a.h):
                treff.setdefault(a.h.nokkel, [a.h.navn, []])[1].append((max(a.h.fra, r.h.fra), min(a.h.til, r.h.til)))
        for navn, perioder in treff.values():
            sammen: list[list[date]] = []
            for fra, til in sorted(perioder):
                if sammen and fra <= sammen[-1][1] + timedelta(days=1):
                    sammen[-1][1] = max(sammen[-1][1], til)
                else:
                    sammen.append([fra, til])
            r.samme_dag.append((navn, " og ".join(kort_periode(fra, til) for fra, til in sammen)))
    return ut


def oversikt(hendelser: list[Hendelse], idag: date) -> Kursoversikt:
    """Kursene kronologisk per måned: fra og med denne måneden og så langt det finnes kurs (tomme måneder er med, så
    hullene synes), og tidligere måneder for seg. En samling som pågår, står i denne måneden selv om den startet før.
    Lange utdanninger med samlinger fram i tid står som aktuelle: de kommende samlingene ligger i sine måneder."""
    alle = rader(hendelser, idag)
    neste = next((r for r in alle if r.h.fra > idag and r.h.status not in ("avlyst", "utkast") and not r.h.ekstern), None)
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


@dataclass
class Fargekurs:
    """Ett kurs i fargeforklaringen: navnet, fargen og alle samlingene, så samme kurs er lett å finne igjen."""
    navn: str
    farge: int
    planlagt: bool
    plan_id: int | None
    kurs_id: int
    samlinger: list             # [«2. samling 18.–21. jan», ...]


def fargeforklaring(hendelser: list[Hendelse], fra: date) -> list[Fargekurs]:
    """Kursene med flere samlinger som har en samling fra og med `fra`, med fargen og ALLE samlingene i listen (et år som
    ikke er `fra` sitt, står med). Avlyste kurs og kurs med annen arrangør er grå og står ikke her."""
    per: dict[tuple, list[Hendelse]] = {}
    for h in hendelser:
        if h.status != "avlyst" and not h.ekstern and h.antall > 1:
            per.setdefault(h.nokkel, []).append(h)
    ut = []
    for liste in per.values():
        if max(h.til for h in liste) < fra:
            continue
        samlinger: dict[int, list[Hendelse]] = {}
        for h in liste:
            samlinger.setdefault(h.nr, []).append(h)
        tekster = []
        for nr, deler in sorted(samlinger.items()):
            start, slutt = min(h.fra for h in deler), max(h.til for h in deler)
            tekster.append(f"{nr}. samling {kort_periode(start, slutt)}" + (f" {start.year}" if start.year != fra.year else ""))
        h0 = liste[0]
        ut.append((min(h.fra for h in liste if h.til >= fra),
                   Fargekurs(h0.navn, h0.farge, h0.planlagt, h0.plan_id, h0.kurs_id, tekster)))
    return [f for _, f in sorted(ut, key=lambda x: (x[0], x[1].navn.casefold()))]


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
