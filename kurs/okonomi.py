"""Økonomirapporter (fase 14): fakturaliste (ordre/bilag), økonomi per måned og per kurs, samlet og per deltaker.

Grunnlaget er fakturaene SYSTEMET har laget i Visma (tabellen faktura) - ingen nye tabeller. Perioden gjelder
fakturadato (faktura.opprettet).

Viktig begrensning: innbetalinger registreres i Visma og hentes ikke tilbake ennå (Visma-produktet er ikke avklart,
se kurs/integrasjoner/visma.py). Status «betalt»/«kreditert» settes derfor ikke automatisk, og rapportene viser
FAKTURERT beløp - ikke innbetalt. Fakturaer med status «kreditert» regnes ikke med i fakturert beløp.
"""
from dataclasses import dataclass, field
from datetime import date

STATUSNAVN = {"opprettet": "Opprettet", "sendt": "Sendt", "betalt": "Betalt", "kreditert": "Kreditert", "feil": "Feil"}
_TELLER_IKKE = ("kreditert", "feil")         # teller ikke i fakturert beløp


@dataclass
class Sum:
    antall: int = 0
    belop: int = 0
    kreditert: int = 0

    def legg_til(self, f: dict) -> None:
        if f["status"] in _TELLER_IKKE:
            if f["status"] == "kreditert":
                self.kreditert += f["belop_nok"]
            return
        self.antall += 1
        self.belop += f["belop_nok"]


@dataclass
class Rapport:
    fakturaer: list
    totalt: Sum = field(default_factory=Sum)
    per_maaned: dict = field(default_factory=dict)       # 'YYYY-MM' -> Sum
    per_kurs: dict = field(default_factory=dict)         # kurs_id -> {'kursnr', 'navn', 'sum': Sum}


def periode(args, idag: date) -> tuple[date, date]:
    """Fra/til fra adressen (ISO-dato). Standard: inneværende år. Ugyldig eller baklengs -> standard."""
    try:
        fra = date.fromisoformat(args.get("fra") or f"{idag.year}-01-01")
        til = date.fromisoformat(args.get("til") or f"{idag.year}-12-31")
    except ValueError:
        return date(idag.year, 1, 1), date(idag.year, 12, 31)
    return (fra, til) if fra <= til else (date(idag.year, 1, 1), date(idag.year, 12, 31))


def fakturaer(con, fra: date, til: date, kurs_id: int | None = None, deltaker_id: int | None = None) -> list[dict]:
    """Fakturaene i perioden (fakturadato), eldste først, med det rapportene trenger. Leser aldri sensitivt."""
    sql = """SELECT f.id, f.faktura_nr, f.visma_id, f.belop_nok, f.status, f.opprettet, f.kursdag_id,
                    kd.dato AS kursdag_dato, p.id AS paamelding_id, p.betaler, p.org_navn,
                    d.id AS deltaker_id, d.navn, k.id AS kurs_id, k.kursnr, k.navn AS kursnavn
             FROM faktura f JOIN paamelding p ON p.id=f.paamelding_id JOIN deltaker d ON d.id=p.deltaker_id
             JOIN kurs k ON k.id=p.kurs_id LEFT JOIN kursdag kd ON kd.id=f.kursdag_id
             WHERE substr(f.opprettet, 1, 10) BETWEEN ? AND ?"""
    args: list = [fra.isoformat(), til.isoformat()]
    if kurs_id:
        sql += " AND k.id=?"
        args.append(kurs_id)
    if deltaker_id:
        sql += " AND d.id=?"
        args.append(deltaker_id)
    sql += " ORDER BY f.opprettet, f.id"
    return [dict(r, dato=str(r["opprettet"])[:10], gjelder=_gjelder(r), statusnavn=STATUSNAVN.get(r["status"], r["status"]),
                 betaler_navn=r["org_navn"] if r["betaler"] == "organisasjon" and r["org_navn"] else r["navn"])
            for r in con.execute(sql, args)]


def _gjelder(f) -> str:
    return "Samlet" if f["kursdag_id"] is None else f"Samling {f['kursdag_dato']}"


def rapport(rader: list[dict]) -> Rapport:
    r = Rapport(rader)
    for f in rader:
        r.totalt.legg_til(f)
        r.per_maaned.setdefault(f["dato"][:7], Sum()).legg_til(f)
        r.per_kurs.setdefault(f["kurs_id"], {"kurs_id": f["kurs_id"], "kursnr": f["kursnr"], "navn": f["kursnavn"],
                                             "sum": Sum()})["sum"].legg_til(f)
    r.per_maaned = dict(sorted(r.per_maaned.items()))
    return r


CSV_KOLONNER = ["Fakturadato", "Fakturanr", "Kursnr", "Kurs", "Gjelder", "Deltaker", "Betaler", "Beløp (kr)", "Status"]


def csv_rader(rader: list[dict]) -> list[list]:
    return [[f["dato"], f["faktura_nr"] or "", f["kursnr"], f["kursnavn"], f["gjelder"], f["navn"], f["betaler_navn"],
             f["belop_nok"], f["statusnavn"]] for f in rader]
