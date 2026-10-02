"""Felles hjelpere for testene av ekstradeltakere og samlingsutvalg (test_ekstradeltaker*.py). Bare oppdiktede data.

Standardkurset har tre samlinger: samling 1 (16.-17.10.2031), samling 2 (13.-14.11.2031) og samling 3 (bare 04.12.2031, en
enkeltdag med egne klokkeslett og timer). Samling 3 har egne timer (5) så timer per dag kan skilles fra kursets (6).
"""
import re
from datetime import date

from kurs import config, db, kursdatoer

ADMIN = f"admin:{config.ADMIN_BRUKERNAVN}"
S = kursdatoer.Samling
S1 = (date(2031, 10, 16), date(2031, 10, 17))
S2 = (date(2031, 11, 13), date(2031, 11, 14))
S3 = (date(2031, 12, 4),)
DAGER = {1: ["2031-10-16", "2031-10-17"], 2: ["2031-11-13", "2031-11-14"], 3: ["2031-12-04"]}


def samlinger3() -> list:
    return [S(S1[0], S1[1], "09:00", "16:00", 6.0), S(S2[0], S2[1], "09:00", "16:00", 6.0),
            S(S3[0], None, "10:00", "15:00", 5.0)]


def kurs_med_samlinger(con, kode="EKS3", samlinger=None, **felt) -> int:
    """Kurs med tre samlinger (eller de gitte), åpent for påmelding. `felt` overstyrer standardfeltene."""
    samlinger = samlinger if samlinger is not None else samlinger3()
    datoer = [d.isoformat() for s in samlinger for d in s.datoer()]
    standard = dict(type="fysisk", sted="Bergen", pris_nok=2500, fakturering="person", kapasitet=10, status="aapen",
                    sharepoint_mappe=f"Kurs/{kode}", timer_pr_dag=6)
    kid = db.opprett_kurs(con, kode=kode, navn="Veiledning i gruppe", datoer=datoer, samlinger=samlinger, **{**standard, **felt})
    con.commit()
    return kid


def samling_ider(con, kid: int) -> list[int]:
    """Samlingenes id-er i den rekkefølgen de starter (samling 1, 2, 3 ...)."""
    return [s["id"] for s in db.kursets_samlinger(con, kid)]


def epost_til(fornavn: str) -> str:
    return f"{fornavn.lower()}@eksempel.no"


def meld(con, kid: int, fornavn: str, **kw) -> int:
    pid, _ = db.meld_paa(con, kid, epost=epost_til(fornavn), fornavn=fornavn, etternavn="Test", **kw)
    con.commit()
    return pid


def sett(con, pid: int, status: str, **kw):
    """db.sett_paamelding_status som administrator (committer). Returnerer det funksjonen returnerer."""
    resultat = db.sett_paamelding_status(con, pid, status, aktor=ADMIN, **kw)
    con.commit()
    return resultat


def ekstra(con, kid: int, fornavn: str, nr=None, **kw) -> int:
    """En påmeldt som administrator har satt til Ekstradeltaker. `nr`: samlingsnumrene (1, 2, 3), ingen = hele kurset."""
    pid = meld(con, kid, fornavn, **kw)
    ider = samling_ider(con, kid)
    sett(con, pid, "ekstradeltaker", samlinger=[ider[n - 1] for n in nr] if nr else None)
    return pid


def rad(con, pid: int):
    return con.execute("SELECT * FROM paamelding WHERE id=?", (pid,)).fetchone()


def status(con, pid: int) -> str:
    return db.paameldingsstatus(rad(con, pid))


def antall(con, sql: str, *args) -> int:
    return con.execute(sql, args).fetchone()[0]


def dager_for(con, pid: int) -> list[str]:
    """Datoene (ISO) påmeldingen er på."""
    return [d["dato"] for d in db.paameldingens_kursdager(con, pid)]


def utvalg_rader(con, pid: int) -> list[int]:
    return db.ekstradeltaker_samlinger(con, pid)


def epostemner(con, fornavn: str) -> list[str]:
    """Emnene på e-postene (kopiene i e-posthistorikken) sendt til personen, eldste først."""
    return [r[0] for r in con.execute("SELECT emne FROM sendt_epost WHERE til=? ORDER BY id", (epost_til(fornavn),))]


def typer(con, fornavn: str) -> list[str]:
    """Typene (utsending_logg) sendt til personen, i sendt-rekkefølge."""
    return [r[0] for r in con.execute("SELECT type FROM utsending_logg WHERE mottaker=? ORDER BY sendt_ts, type",
                                      (epost_til(fornavn),))]


def synlig_tekst(html: str) -> str:
    """Teksten på siden uten tagger og med enkle mellomrom (til sammenligning av det brukeren ser)."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
