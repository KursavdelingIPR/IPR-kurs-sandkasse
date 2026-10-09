"""Kursbevis for avsluttede kurs.

Lages som HTML (kan skrives ut / lagres som PDF fra nettleseren) og lagres i databasen (tabellen dokument_innhold).
Legges paa Min side og varsles paa e-post.

Terskel for kursbevis (andel dager med oppmote) er et spørsmål til kartleggingen – default 100 %.
For kurs i et spesialistlop vises akkumulert timetall paa tvers av alle samlinger.
"""
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import db, kursbevisdesign, kursbevismal, maltekster
from .kjoring import Kjoring

MIN_ANDEL = 1.0
_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "maler"), autoescape=select_autoescape(["html"]))
_env.filters["i_setning"] = db.i_setning      # «Samling 1 og 3» -> «samling 1 og 3», men et eget samlingsnavn («EMDR») beholdes som det står


def timer_i_lop(con, deltaker_id: int, lop: str) -> float:
    """Timene deltakeren faktisk har møtt i spesialistløpet: hver dag med oppmøte teller med dagens egne timer, ellers samlingens,
    ellers kursets (samme regel som kursbeviset). En ekstradeltaker teller bare dager i sitt samlingsutvalg."""
    return con.execute(
        f"""SELECT COALESCE(SUM(COALESCE(kd.timer, s.timer_pr_dag, k.timer_pr_dag)),0) FROM oppmote o
           JOIN paamelding p ON p.id=o.paamelding_id JOIN kurs k ON k.id=p.kurs_id
           JOIN kursdag kd ON kd.id=o.kursdag_id LEFT JOIN samling s ON s.id=kd.samling_id
           WHERE p.deltaker_id=? AND k.spesialistlop=? AND {db.sql_egen_dag('p', 'kd')}""", (deltaker_id, lop)).fetchone()[0]


def lag_html(con, kurs, v: dict, *, egen: str | None = None, forhandsvisning: bool = False) -> str:
    """Hele kursbeviset (A4) for én deltaker. Kurset kan ha en egen versjon (kurs.kursbevis_html, fanen Kursbevis) og en
    ramme (kurs.kursbevis_ramme); ellers standardbeviset. `egen` overstyrer kursets lagrede versjon (forhåndsvisning av det
    som står i redigereren, ikke lagret ennå). `v`: navn, fornavn, kurs, dager, timer, samlinger, antall_samlinger,
    lop_timer og dato."""
    if egen is None:
        egen = kurs["kursbevis_html"]
    innhold = kursbevismal.flett(con, egen, v, db.i_setning) if egen else None
    # Kursets design (kurs/kursbevisdesign.py): logo, illustrasjon, farge, skrift og signatur. Uten design: som før.
    design = kursbevisdesign.for_kursbevis(con, kurs)
    ramme = kursbevismal.ramme_farge(kurs)
    if design and ramme is not None:
        ramme = design["farge"]                 # designets farge, med mindre kurset har valgt «Uten ramme»
    return _env.get_template("kursbevis.html").render(**v, innhold=innhold, ramme_farge=ramme, design=design,
                                                      dager_tekst=", ".join(kursbevismal.norsk_dato(d) for d in v["dager"]),
                                                      forhandsvisning=forhandsvisning)


def _logg_kursbevis_feil(k: Kjoring, r, feil: maltekster.MalFeil) -> None:
    """Trygg logg (kurs_id, paamelding_id, mal, grunnkode - ALDRI navn, e-post eller maltekst)."""
    db.logg(k.con, "kursbevis_mal_feil", {"kurs_id": r["id"], "paamelding_id": r["pid"], **feil.detaljer()})
    k.si(f"  FEIL kursbevis_klar: {feil} - kursbevis for {r['kode']} ikke utstedt "
         "(rettes malen, tas kandidaten igjen neste kjøring)")


def kjor(k: Kjoring) -> None:
    kandidater = k.con.execute(
        """SELECT k.*, p.id AS pid, p.deltaker_id, p.ekstradeltaker_ts, d.navn AS deltaker_navn, d.fornavn AS deltaker_fornavn, d.epost
           FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id JOIN kurs k ON k.id=p.kurs_id
           WHERE k.status='avsluttet' AND p.status='bekreftet'
             AND NOT EXISTS (SELECT 1 FROM dokument x WHERE x.kurs_id=k.id AND x.deltaker_id=p.deltaker_id AND x.type='kursbevis')
        """).fetchall()
    for r in kandidater:
        if k.sending_stanset:       # e-posttjenesten er nede: ikke utsted bevis som ikke kan varsles - tas neste kjoering
            k.si("  Kursbevis utsatt: e-posttjenesten feilet flere ganger på rad i denne kjøringen")
            break
        alle = db.kursdager(k.con, r["id"])
        pm = {"id": r["pid"], "kurs_id": r["id"], "ekstradeltaker_ts": r["ekstradeltaker_ts"]}
        dager = db.paameldingens_kursdager(k.con, pm, alle)         # dagene DENNE deltakeren er på (ekstradeltaker: valgte samlinger)
        egne = {d["id"] for d in dager}
        # Timer per kursdag: dagens egne timer, ellers samlingens, ellers kursets (migrering 9). Uten avvik blir det
        # det samme som før: antall dager med oppmøte x timer per dag. Oppmøte på en dag utenfor deltakerens dager (manuelt
        # eller fra Zoom) teller verken i 100 %-regelen eller i timene.
        mott = [m for m in k.con.execute(
            """SELECT kd.id AS kursdag_id, kd.dato, COALESCE(kd.timer, s.timer_pr_dag, ?) AS timer FROM oppmote o
               JOIN kursdag kd ON kd.id=o.kursdag_id LEFT JOIN samling s ON s.id=kd.samling_id
               WHERE o.paamelding_id=? ORDER BY kd.dato""", (r["timer_pr_dag"], r["pid"])).fetchall() if m["kursdag_id"] in egne]
        if not dager or len(mott) / len(dager) < MIN_ANDEL:
            continue
        if k.tor:
            k.si(f"  [TØRR] kursbevis -> {r['deltaker_navn']} ({r['kode']})")
            continue
        # Preflight FOER noen varig sideeffekt (filskriving/dokumentrad): en ugyldig kursbevis_klar-maltekst skal
        # oppdages her, saa kandidaten forblir en kandidat (ingen fil, ingen dokumentrad) til malen er rettet.
        # Rendres KUN HER (én gang) - det ferdige resultatet gjenbrukes ordrett ved selve sendingen, saa en
        # eventuell SENERE malfeil (override endret, DB-lesefeil) aldri kan oppstaa etter at fil/dokumentrad
        # er opprettet (TOCTOU-vakt, se tests/test_maltekster_kursbevis.py).
        try:
            emne, epost_html = k.render_for_sending("kursbevis_klar", navn=r["deltaker_navn"],
                                                    fornavn=r["deltaker_fornavn"], kurs=r)
        except maltekster.MalFeil as e:
            _logg_kursbevis_feil(k, r, e)
            continue
        delvis = db.er_ekstradeltaker(pm) and bool(db.ekstradeltaker_samlinger(k.con, r["pid"]))
        bevis_html = lag_html(k.con, r, dict(
            navn=r["deltaker_navn"], fornavn=r["deltaker_fornavn"], kurs=r, dager=[m["dato"] for m in mott],
            timer=sum(m["timer"] for m in mott),
            # Kursbeviset skal ikke si at deltakeren har gjennomført hele kurset når hen bare var på noen samlinger
            samlinger=db.samlingsutvalg_tekst(k.con, pm, alle, og=True) if delvis else None,
            antall_samlinger=len(db.kursets_samlinger(k.con, r["id"], alle)),
            lop_timer=timer_i_lop(k.con, r["deltaker_id"], r["spesialistlop"]) if r["spesialistlop"] else None,
            dato=k.idag.isoformat(),
        ))
        # Lagres i databasen (dokument_innhold), ikke som fil: overlever omstart/skalering i Azure og er med i backup.
        dok_id = db.sett_inn(
            k.con, "INSERT INTO dokument (kurs_id, deltaker_id, type, tittel, url) VALUES (?,?,?,?,?)",
            (r["id"], r["deltaker_id"], "kursbevis", f"Kursbevis – {r['navn']}", "db:"))
        k.con.execute("INSERT INTO dokument_innhold (dokument_id, mimetype, innhold) VALUES (?,?,?)",
                      (dok_id, "text/html", bevis_html))
        k.send_ferdigrendret_en_gang(f"kurs:{r['id']}", r["epost"], "kursbevis", emne, epost_html,
                                     paamelding_id=r["pid"])
