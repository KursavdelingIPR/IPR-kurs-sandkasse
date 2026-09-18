"""Databasetilgang (SQLite). Én fil, ingen server – samme prinsipp som referansen."""
import json
import re
import secrets
import sqlite3
import string
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

from . import config

_SCHEMA = Path(__file__).with_name("schema.sql")
_KODE_TEGN = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # uten 0/O/1/I – lett aa taste
_TRANSLIT_NORSK = str.maketrans({"æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O", "å": "aa", "Å": "AA"})


def koble(sti: Path | None = None) -> sqlite3.Connection:
    sti = Path(sti or config.DB_STI)
    sti.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(sti)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init(con: sqlite3.Connection) -> None:
    con.executescript(_SCHEMA.read_text(encoding="utf-8"))
    _migrer(con)
    if not con.execute("SELECT 1 FROM admin_bruker").fetchone():
        opprett_admin_bruker(con, config.ADMIN_BRUKERNAVN, "Standardbruker", config.ADMIN_PASSORD)
    con.commit()


def _migrer(con: sqlite3.Connection) -> None:
    """Legger til kolonner fra nyere versjoner av schema.sql i eksisterende databaser, uten aa miste data.

    SQLite tillater ikke ALTER TABLE ... ADD COLUMN med en ikke-konstant DEFAULT (som datetime('now'))
    naar tabellen har rader fra for. Vi legger derfor til kolonnen uten default og etterfyller (backfill)
    med en vanlig UPDATE i stedet.
    """
    def har_kolonne(tabell: str, kolonne: str) -> bool:
        return any(r["name"] == kolonne for r in con.execute(f"PRAGMA table_info({tabell})"))

    if not har_kolonne("kurs", "ansvarlig_admin_id"):
        con.execute("ALTER TABLE kurs ADD COLUMN ansvarlig_admin_id INTEGER REFERENCES admin_bruker(id)")
    if not har_kolonne("deltaker", "yrkestittel"):
        con.execute("ALTER TABLE deltaker ADD COLUMN yrkestittel TEXT")
    if not har_kolonne("paamelding", "oppdatert"):
        con.execute("ALTER TABLE paamelding ADD COLUMN oppdatert TEXT")
        con.execute("UPDATE paamelding SET oppdatert = opprettet WHERE oppdatert IS NULL")
    if not har_kolonne("paamelding", "faktura_kommentar"):
        con.execute("ALTER TABLE paamelding ADD COLUMN faktura_kommentar TEXT")
    if not har_kolonne("paamelding", "intern_kommentar"):
        con.execute("ALTER TABLE paamelding ADD COLUMN intern_kommentar TEXT")


@contextmanager
def transaksjon(con: sqlite3.Connection):
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise


def logg(con: sqlite3.Connection, handling: str, detaljer: dict | str | None = None, aktor: str = "system") -> None:
    if isinstance(detaljer, dict):
        detaljer = json.dumps(detaljer, ensure_ascii=False)
    con.execute("INSERT INTO hendelse (aktor, handling, detaljer) VALUES (?,?,?)", (aktor, handling, detaljer))


def ny_kode(lengde: int = 6) -> str:
    return "".join(secrets.choice(_KODE_TEGN) for _ in range(lengde))


# ---------- admin-brukere ----------

def opprett_admin_bruker(con, brukernavn: str, navn: str, passord: str, aktor: str = "system") -> int:
    brukernavn = brukernavn.strip().lower()
    cur = con.execute(
        "INSERT INTO admin_bruker (brukernavn, navn, passord_hash) VALUES (?,?,?)",
        (brukernavn, navn.strip(), generate_password_hash(passord)),
    )
    logg(con, "admin_bruker_opprettet", {"brukernavn": brukernavn}, aktor=aktor)
    return cur.lastrowid


def verifiser_admin(con, brukernavn: str, passord: str) -> sqlite3.Row | None:
    rad = con.execute(
        "SELECT * FROM admin_bruker WHERE brukernavn=? AND aktiv=1", (brukernavn.strip().lower(),)
    ).fetchone()
    if rad and check_password_hash(rad["passord_hash"], passord):
        return rad
    return None


# ---------- kurs ----------

def generer_kode(con, navn: str, datoer: list[str]) -> str:
    """Lager en unik kurskode automatisk, ut fra forste ord i kursnavnet og aarstallet til forste kursdag."""
    forste_ord = re.split(r"[\s\-–—(),.:;/]+", navn.strip())[0] if navn.strip() else ""
    bokstaver = "".join(c for c in forste_ord.translate(_TRANSLIT_NORSK) if c.isascii() and c.isalnum())
    prefiks = (bokstaver[:4] or "KURS").upper()
    aar = date.fromisoformat(datoer[0]).year if datoer else date.today().year
    basis = f"{prefiks}-{aar}"
    kode, n = basis, 2
    while con.execute("SELECT 1 FROM kurs WHERE kode=?", (kode,)).fetchone():
        kode = f"{basis}-{n}"
        n += 1
    return kode


def opprett_kurs(con, *, kode: str, navn: str, datoer: list[str], aktor: str = "system", **felter) -> int:
    kolonner = ["kode", "navn", *felter.keys()]
    cur = con.execute(
        f"INSERT INTO kurs ({','.join(kolonner)}) VALUES ({','.join('?' * len(kolonner))})",
        [kode, navn, *felter.values()],
    )
    kurs_id = cur.lastrowid
    for d in datoer:
        con.execute(
            "INSERT INTO kursdag (kurs_id, dato, innsjekk_token, innsjekk_kode) VALUES (?,?,?,?)",
            (kurs_id, d, secrets.token_urlsafe(24), ny_kode()),
        )
    logg(con, "kurs_opprettet", {"kurs_id": kurs_id, "kode": kode}, aktor=aktor)
    return kurs_id


def kursdager(con, kurs_id: int) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM kursdag WHERE kurs_id=? ORDER BY dato", (kurs_id,)).fetchall()


def antall_bekreftet(con, kurs_id: int) -> int:
    return con.execute(
        "SELECT COUNT(*) FROM paamelding WHERE kurs_id=? AND status='bekreftet'", (kurs_id,)
    ).fetchone()[0]


# ---------- deltakere / paamelding ----------

def finn_eller_opprett_deltaker(con, epost: str, navn: str, **felter) -> int:
    epost = epost.strip().lower()
    rad = con.execute("SELECT id FROM deltaker WHERE epost=?", (epost,)).fetchone()
    if rad:
        oppdater = {k: v for k, v in felter.items() if v}
        if oppdater:
            con.execute(
                f"UPDATE deltaker SET {','.join(f'{k}=?' for k in oppdater)} WHERE id=?",
                [*oppdater.values(), rad["id"]],
            )
        return rad["id"]
    kolonner = ["epost", "navn", *felter.keys()]
    cur = con.execute(
        f"INSERT INTO deltaker ({','.join(kolonner)}) VALUES ({','.join('?' * len(kolonner))})",
        [epost, navn.strip(), *felter.values()],
    )
    return cur.lastrowid


class Paameldingsfeil(Exception):
    pass


class DeltakerFeil(Exception):
    pass


# ---------- admin: redigering av deltaker/paamelding (fase 3) ----------

def oppdater_deltaker(con, deltaker_id: int, felter: dict, aktor: str = "admin") -> list[str]:
    """Oppdaterer person-opplysninger. Disse deles paa tvers av alle kurs personen er meldt paa,
    saa paamelding.oppdatert settes for ALLE deres paameldinger, ikke bare den man sto paa.

    Returnerer navnene paa feltene som faktisk ble endret. Hendelsesloggen faar kun feltnavnene,
    aldri gammel/ny verdi (personopplysninger skal ikke ligge i loggen).
    """
    gammel = con.execute("SELECT * FROM deltaker WHERE id=?", (deltaker_id,)).fetchone()
    if not gammel:
        raise DeltakerFeil("Ukjent deltaker")

    if "epost" in felter:
        epost = (felter["epost"] or "").strip().lower()
        if not epost or "@" not in epost:
            raise DeltakerFeil("Ugyldig e-postadresse.")
        opptatt = con.execute("SELECT 1 FROM deltaker WHERE epost=? AND id!=?", (epost, deltaker_id)).fetchone()
        if opptatt:
            raise DeltakerFeil("E-postadressen er allerede i bruk av en annen deltaker.")
        felter = {**felter, "epost": epost}
    if "navn" in felter and not (felter["navn"] or "").strip():
        raise DeltakerFeil("Navn kan ikke være tomt.")

    endret = [felt for felt, verdi in felter.items() if (gammel[felt] or "") != (verdi or "")]
    if not endret:
        return []
    con.execute(f"UPDATE deltaker SET {','.join(f'{f}=?' for f in felter)} WHERE id=?",
               [*felter.values(), deltaker_id])
    na = datetime.now().isoformat(timespec="seconds")
    con.execute("UPDATE paamelding SET oppdatert=? WHERE deltaker_id=?", (na, deltaker_id))
    logg(con, "deltaker_endret", {"deltaker_id": deltaker_id, "felt": endret}, aktor=aktor)
    return endret


def oppdater_paamelding(con, paamelding_id: int, felter: dict, aktor: str = "admin") -> list[str]:
    """Oppdaterer feltene som kun gjelder denne paameldingen (ikke status, se sett_paamelding_status).

    Hendelsesloggen faar kun feltnavnene som ble endret, aldri verdiene.
    """
    gammel = con.execute("SELECT * FROM paamelding WHERE id=?", (paamelding_id,)).fetchone()
    if not gammel:
        raise Paameldingsfeil("Ukjent påmelding")
    endret = [felt for felt, verdi in felter.items() if (gammel[felt] or "") != (verdi or "")]
    if not endret:
        return []
    na = datetime.now().isoformat(timespec="seconds")
    con.execute(f"UPDATE paamelding SET {','.join(f'{f}=?' for f in felter)}, oppdatert=? WHERE id=?",
               [*felter.values(), na, paamelding_id])
    logg(con, "paamelding_endret", {"paamelding_id": paamelding_id, "felt": endret}, aktor=aktor)
    return endret


def oppdater_sensitivt(con, paamelding_id: int, allergier: str | None, tilrettelegging: str | None,
                       aktor: str = "admin") -> bool:
    """Oppdaterer allergi/tilrettelegging. Innholdet logges ALDRI i hendelsesloggen, kun at det ble endret."""
    gammel = con.execute("SELECT * FROM sensitivt WHERE paamelding_id=?", (paamelding_id,)).fetchone()
    ny = ((allergier or "").strip() or None, (tilrettelegging or "").strip() or None)
    old = ((gammel["allergier"] if gammel else None), (gammel["tilrettelegging"] if gammel else None))
    if ny == old:
        return False
    if any(ny):
        con.execute("INSERT OR REPLACE INTO sensitivt (paamelding_id, allergier, tilrettelegging) VALUES (?,?,?)",
                   (paamelding_id, *ny))
    else:
        con.execute("DELETE FROM sensitivt WHERE paamelding_id=?", (paamelding_id,))
    con.execute("UPDATE paamelding SET oppdatert=? WHERE id=?",
               (datetime.now().isoformat(timespec="seconds"), paamelding_id))
    logg(con, "sensitivt_endret", {"paamelding_id": paamelding_id}, aktor=aktor)
    return True


def sett_paamelding_status(con, paamelding_id: int, ny_status: str, aktor: str = "admin") -> int | None:
    """Endrer paameldingsstatus fra admin. Bruker eksisterende meld_av() for avmelding, slik at
    ventelisteopprykk skjer riktig. Bekreftet -> venteliste tilbys bevisst ikke (bruk avmelding).

    Returnerer paamelding_id som boer kjores gjennom sveiper.kjor() naa (nylig bekreftet), ellers None.
    """
    p = con.execute("SELECT * FROM paamelding WHERE id=?", (paamelding_id,)).fetchone()
    if not p:
        raise Paameldingsfeil("Ukjent påmelding")
    gammel_status = p["status"]
    if ny_status == gammel_status:
        return None
    na = datetime.now().isoformat(timespec="seconds")

    if ny_status == "avmeldt":
        opprykket = meld_av(con, paamelding_id, aktor=aktor)
        logg(con, "status_endret", {"paamelding_id": paamelding_id, "fra": gammel_status, "til": "avmeldt"}, aktor=aktor)
        return opprykket

    if ny_status == "bekreftet":
        if gammel_status == "bekreftet":
            raise Paameldingsfeil("Ugyldig statusendring.")
        kurs = con.execute("SELECT * FROM kurs WHERE id=?", (p["kurs_id"],)).fetchone()
        if kurs["kapasitet"] is not None and antall_bekreftet(con, p["kurs_id"]) >= kurs["kapasitet"]:
            raise Paameldingsfeil("Kurset er fullt – kan ikke bekrefte flere uten å melde av noen først.")
        con.execute("UPDATE paamelding SET status='bekreftet', sveiper_kjort=0, oppdatert=? WHERE id=?",
                   (na, paamelding_id))
        logg(con, "status_endret", {"paamelding_id": paamelding_id, "fra": gammel_status, "til": "bekreftet"}, aktor=aktor)
        return paamelding_id

    if ny_status == "venteliste":
        if gammel_status == "bekreftet":
            raise Paameldingsfeil("Kan ikke sette en bekreftet deltaker til venteliste direkte – meld av i stedet.")
        con.execute("UPDATE paamelding SET status='venteliste', sveiper_kjort=0, oppdatert=? WHERE id=?",
                   (na, paamelding_id))
        logg(con, "status_endret", {"paamelding_id": paamelding_id, "fra": gammel_status, "til": "venteliste"}, aktor=aktor)
        return None

    raise Paameldingsfeil("Ugyldig statusendring.")


def meld_paa(con, kurs_id: int, *, epost: str, navn: str, deltaker: dict | None = None,
             paamelding: dict | None = None, sensitivt: dict | None = None) -> tuple[int, str]:
    """Registrer paamelding. Returnerer (paamelding_id, status). Setter venteliste naar kurset er fullt."""
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kurs_id,)).fetchone()
    if not kurs:
        raise Paameldingsfeil("Ukjent kurs")
    if kurs["status"] not in ("aapen", "full", "aktiv"):
        raise Paameldingsfeil("Kurset er ikke åpent for påmelding")

    deltaker_id = finn_eller_opprett_deltaker(con, epost, navn, **(deltaker or {}))
    finnes = con.execute(
        "SELECT id, status FROM paamelding WHERE kurs_id=? AND deltaker_id=?", (kurs_id, deltaker_id)
    ).fetchone()
    if finnes and finnes["status"] != "avmeldt":
        raise Paameldingsfeil("Du er allerede påmeldt dette kurset")

    fullt = kurs["kapasitet"] is not None and antall_bekreftet(con, kurs_id) >= kurs["kapasitet"]
    status = "venteliste" if fullt else "bekreftet"
    na = datetime.now().isoformat(timespec="seconds")
    felter = {"status": status, "samtykke_ts": na, "oppdatert": na, **(paamelding or {})}
    # Betalingsmåte: deltakeren bestemmer bare når kurset er satt til 'deltaker_velger'
    if kurs["betaling"] == "deltaker_velger":
        felter["betaling"] = "per_samling" if felter.get("betaling") == "per_samling" else "samlet"
    else:
        felter["betaling"] = kurs["betaling"]

    if finnes:  # reaktiver tidligere avmelding
        con.execute(
            f"UPDATE paamelding SET {','.join(f'{k}=?' for k in felter)}, sveiper_kjort=0 WHERE id=?",
            [*felter.values(), finnes["id"]],
        )
        pid = finnes["id"]
    else:
        kolonner = ["kurs_id", "deltaker_id", *felter.keys()]
        cur = con.execute(
            f"INSERT INTO paamelding ({','.join(kolonner)}) VALUES ({','.join('?' * len(kolonner))})",
            [kurs_id, deltaker_id, *felter.values()],
        )
        pid = cur.lastrowid

    if sensitivt and any(sensitivt.values()):
        con.execute(
            "INSERT OR REPLACE INTO sensitivt (paamelding_id, allergier, tilrettelegging) VALUES (?,?,?)",
            (pid, sensitivt.get("allergier"), sensitivt.get("tilrettelegging")),
        )
    if fullt and kurs["status"] == "aapen":
        con.execute("UPDATE kurs SET status='full' WHERE id=?", (kurs_id,))
    logg(con, "paamelding", {"paamelding_id": pid, "kurs_id": kurs_id, "status": status}, aktor=f"deltaker:{deltaker_id}")
    return pid, status


def meld_av(con, paamelding_id: int, aktor: str = "admin") -> int | None:
    """Avmelder og flytter forste paa ventelisten opp. Returnerer paamelding_id som ble flyttet opp."""
    p = con.execute("SELECT * FROM paamelding WHERE id=?", (paamelding_id,)).fetchone()
    na = datetime.now().isoformat(timespec="seconds")
    con.execute("UPDATE paamelding SET status='avmeldt', oppdatert=? WHERE id=?", (na, paamelding_id))
    logg(con, "avmelding", {"paamelding_id": paamelding_id}, aktor=aktor)
    if p["status"] != "bekreftet":
        return None
    neste = con.execute(
        "SELECT id FROM paamelding WHERE kurs_id=? AND status='venteliste' ORDER BY opprettet, id LIMIT 1",
        (p["kurs_id"],),
    ).fetchone()
    if neste:
        con.execute("UPDATE paamelding SET status='bekreftet', sveiper_kjort=0, oppdatert=? WHERE id=?", (na, neste["id"]))
        logg(con, "flyttet_fra_venteliste", {"paamelding_id": neste["id"]})
        return neste["id"]
    con.execute("UPDATE kurs SET status='aapen' WHERE id=? AND status='full'", (p["kurs_id"],))
    return None


# ---------- utsending (dedup) ----------

def allerede_sendt(con, nokkel: str, mottaker: str, type_: str) -> bool:
    return con.execute(
        "SELECT 1 FROM utsending_logg WHERE nokkel=? AND mottaker=? AND type=?", (nokkel, mottaker, type_)
    ).fetchone() is not None


def marker_sendt(con, nokkel: str, mottaker: str, type_: str) -> None:
    con.execute(
        "INSERT OR IGNORE INTO utsending_logg (nokkel, mottaker, type) VALUES (?,?,?)", (nokkel, mottaker, type_)
    )


# ---------- oppmote ----------

def registrer_oppmote(con, paamelding_id: int, kursdag_id: int, kilde: str, minutter: int | None = None) -> bool:
    """True hvis ny registrering, False hvis allerede registrert."""
    cur = con.execute(
        "INSERT OR IGNORE INTO oppmote (paamelding_id, kursdag_id, kilde, minutter) VALUES (?,?,?,?)",
        (paamelding_id, kursdag_id, kilde, minutter),
    )
    return cur.rowcount == 1


def iso(d: date) -> str:
    return d.isoformat()


def alfanumerisk(s: str) -> str:
    return "".join(c for c in s.upper() if c in string.ascii_uppercase + string.digits)
