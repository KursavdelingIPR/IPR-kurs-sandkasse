"""Kursbevis for avsluttede kurs.

Lages som HTML (kan skrives ut / lagres som PDF fra nettleseren). Hvis weasyprint er installert
lages PDF direkte. Legges paa Min side og varsles paa e-post.

Terskel for kursbevis (andel dager med oppmote) er et spørsmål til kartleggingen – default 100 %.
For kurs i et spesialistlop vises akkumulert timetall paa tvers av alle samlinger.
"""
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import config, db
from .kjoring import Kjoring

MIN_ANDEL = 1.0
_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "maler"), autoescape=select_autoescape(["html"]))


def timer_i_lop(con, deltaker_id: int, lop: str) -> float:
    return con.execute(
        """SELECT COALESCE(SUM(k.timer_pr_dag),0) FROM oppmote o
           JOIN paamelding p ON p.id=o.paamelding_id JOIN kurs k ON k.id=p.kurs_id
           WHERE p.deltaker_id=? AND k.spesialistlop=?""", (deltaker_id, lop)).fetchone()[0]


def kjor(k: Kjoring) -> None:
    kandidater = k.con.execute(
        """SELECT k.*, p.id AS pid, p.deltaker_id, d.navn AS deltaker_navn, d.epost
           FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id JOIN kurs k ON k.id=p.kurs_id
           WHERE k.status='avsluttet' AND p.status='bekreftet'
             AND NOT EXISTS (SELECT 1 FROM dokument x WHERE x.kurs_id=k.id AND x.deltaker_id=p.deltaker_id AND x.type='kursbevis')
        """).fetchall()
    for r in kandidater:
        dager = db.kursdager(k.con, r["id"])
        mott = k.con.execute(
            "SELECT kd.dato FROM oppmote o JOIN kursdag kd ON kd.id=o.kursdag_id WHERE o.paamelding_id=? ORDER BY kd.dato",
            (r["pid"],)).fetchall()
        if not dager or len(mott) / len(dager) < MIN_ANDEL:
            continue
        if k.tor:
            k.si(f"  [TØRR] kursbevis -> {r['deltaker_navn']} ({r['kode']})")
            continue
        html = _env.get_template("kursbevis.html").render(
            navn=r["deltaker_navn"], kurs=r, dager=[m["dato"] for m in mott],
            timer=len(mott) * r["timer_pr_dag"],
            lop_timer=timer_i_lop(k.con, r["deltaker_id"], r["spesialistlop"]) if r["spesialistlop"] else None,
        )
        mappe = config.ROT / "data" / "kursbevis" / r["kode"]
        mappe.mkdir(parents=True, exist_ok=True)
        fil = mappe / f"{r['deltaker_id']}.html"
        fil.write_text(html, encoding="utf-8")
        k.con.execute(
            "INSERT INTO dokument (kurs_id, deltaker_id, type, tittel, url) VALUES (?,?,?,?,?)",
            (r["id"], r["deltaker_id"], "kursbevis", f"Kursbevis – {r['navn']}", f"lokal:{fil.relative_to(config.ROT).as_posix()}"))
        k.send_en_gang(f"kurs:{r['id']}", r["epost"], "kursbevis", "kursbevis_klar", paamelding_id=r["pid"],
                       navn=r["deltaker_navn"], kurs=r)
