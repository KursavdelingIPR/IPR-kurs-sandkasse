"""Det som skjer automatisk naar noen melder seg paa (spec del B: 'automatiske sveiper').

Kjores umiddelbart fra nettskjemaet OG av den daglige jobben (fanger opp det som feilet).
Hver deltaker behandles for seg: feiler Visma for én, stopper det ikke de andre.

Fakturering styres av kurs.betaling / paamelding.betaling:
  samlet      -> én faktura paa hele belopet ved paamelding
  per_samling -> én faktura pr kursdag, opprettet `kurs.faktura_dager_for` dager for hver samling
"""
from datetime import date

from . import db
from .integrasjoner import visma
from .kjoring import Kjoring

SQL_DELTAKER = """SELECT p.*, d.navn, d.epost, d.id AS did, k.navn AS kursnavn, k.kode, k.pris_nok, k.fakturering,
                         k.visma_artikkel, k.type AS kurstype, k.faktura_dager_for
                  FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id JOIN kurs k ON k.id=p.kurs_id"""


def kjor(k: Kjoring, paamelding_id: int | None = None) -> None:
    rader = k.con.execute(
        SQL_DELTAKER + " WHERE p.sveiper_kjort=0 AND p.status IN ('bekreftet','venteliste')"
        + (" AND p.id=?" if paamelding_id else ""),
        (paamelding_id,) if paamelding_id else ()).fetchall()
    for p in rader:
        try:
            _en(k, p)
        except Exception as e:  # noqa: BLE001 – logg og fortsett med neste
            db.logg(k.con, "sveip_feil", {"paamelding_id": p["id"], "feil": str(e)})
            k.si(f"  FEIL for {p['epost']}: {e}")


def _en(k: Kjoring, p) -> None:
    nokkel = f"kurs:{p['kurs_id']}"
    kurs = k.con.execute("SELECT * FROM kurs WHERE id=?", (p["kurs_id"],)).fetchone()
    dager = db.kursdager(k.con, p["kurs_id"])

    if p["status"] == "venteliste":
        k.send_en_gang(nokkel, p["epost"], "venteliste", "venteliste", p=p, kurs=kurs)
        return  # ikke fakturer / ikke merk ferdig – kjores paa nytt naar de flyttes opp

    k.send_en_gang(nokkel, p["epost"], "bekreftelse", "bekreftelse", p=p, kurs=kurs, dager=dager)
    fakturer(k, p)
    k.con.execute("UPDATE paamelding SET sveiper_kjort=1 WHERE id=?", (p["id"],))


def skal_faktureres(p) -> bool:
    return p["fakturering"] == "person" and p["pris_nok"] > 0 and p["status"] == "bekreftet"


def delbelop(pris: int, antall: int) -> list[int]:
    """Deler belopet likt. Rest legges paa forste faktura, slik at summen alltid stemmer."""
    grunn, rest = divmod(pris, antall)
    return [grunn + rest] + [grunn] * (antall - 1)


def fakturer(k: Kjoring, p) -> None:
    """Oppretter de fakturaene som skal finnes for denne deltakeren akkurat naa."""
    if not skal_faktureres(p):
        return
    if p["betaling"] != "per_samling":
        _opprett(k, p, None, p["pris_nok"], f"{p['kursnavn']} – {p['navn']}")
        return
    dager = db.kursdager(k.con, p["kurs_id"])
    belop = delbelop(p["pris_nok"], len(dager))
    for nr, (dag, sum_) in enumerate(zip(dager, belop), start=1):
        if _forfalt(k.idag, dag["dato"], p["faktura_dager_for"]):
            _opprett(k, p, dag["id"], sum_,
                     f"{p['kursnavn']} – {p['navn']} – samling {nr} av {len(dager)} ({dag['dato']})")


def _forfalt(idag: date, dato: str, dager_for: int) -> bool:
    return (date.fromisoformat(dato) - idag).days <= dager_for


def forfalte_delfakturaer(k: Kjoring) -> None:
    """Daglig: lager delfakturaer som har blitt forfalt siden sist. Idempotent."""
    for p in k.con.execute(SQL_DELTAKER + """ WHERE p.status='bekreftet' AND p.betaling='per_samling'
                                              AND p.sveiper_kjort=1 AND k.fakturering='person' AND k.pris_nok > 0"""):
        try:
            fakturer(k, p)
        except Exception as e:  # noqa: BLE001
            db.logg(k.con, "faktura_feil", {"paamelding_id": p["id"], "feil": str(e)})
            k.si(f"  FEIL delfaktura for {p['epost']}: {e}")


def _opprett(k: Kjoring, p, kursdag_id: int | None, belop: int, linjetekst: str) -> None:
    if k.con.execute(
            "SELECT 1 FROM faktura WHERE paamelding_id=? AND COALESCE(kursdag_id,0)=?",
            (p["id"], kursdag_id or 0)).fetchone():
        return  # allerede fakturert – unik indeks i databasen er ekstra sikring
    org = p["betaler"] == "organisasjon"
    g = visma.Fakturagrunnlag(
        kunde_navn=p["org_navn"] if org else p["navn"],
        kunde_epost=p["faktura_epost"] or p["epost"],
        er_privatperson=not org,
        org_nr=p["org_nr"] if org else None,
        adresse=p["faktura_adresse"], postnr=p["faktura_postnr"], sted=p["faktura_sted"],
        ehf=bool(p["ehf"]),
        deres_ref=p["faktura_ref"] or (p["navn"] if org else None),
        linjetekst=linjetekst,
        artikkel=p["visma_artikkel"],
        belop_nok=belop,
    )
    if k.tor:
        k.si(f"  [TØRR] faktura {g.belop_nok} kr -> {g.kunde_navn} ({linjetekst})")
        return
    res = visma.fakturer(g)
    k.con.execute(
        """INSERT INTO faktura (paamelding_id, kursdag_id, visma_id, faktura_nr, belop_nok, status)
           VALUES (?,?,?,?,?, 'sendt')""",
        (p["id"], kursdag_id, res.visma_id, res.faktura_nr, g.belop_nok))
    k.con.execute("UPDATE deltaker SET visma_kunde_id=? WHERE id=? AND visma_kunde_id IS NULL", (res.kunde_id, p["did"]))
    db.logg(k.con, "fakturert", {"paamelding_id": p["id"], "kursdag_id": kursdag_id,
                                 "faktura_nr": res.faktura_nr, "belop": g.belop_nok})
    k.si(f"  faktura {res.faktura_nr} {g.belop_nok} kr -> {g.kunde_navn} ({linjetekst})")
