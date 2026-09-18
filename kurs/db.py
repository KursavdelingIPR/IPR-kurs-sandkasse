"""Databasetilgang (SQLite). Én fil, ingen server – samme prinsipp som referansen."""
import hashlib
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
    if not har_kolonne("kurs", "paameldingsfrist"):
        con.execute("ALTER TABLE kurs ADD COLUMN paameldingsfrist TEXT")


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
        if kurs["status"] == "avlyst":
            raise Paameldingsfeil("Kurset er avlyst – kan ikke bekrefte flere.")
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


# ---------- admin: redigering av kurs (fase 4) ----------

KURS_LAASTE_FELT = ("pris_nok", "fakturering", "betaling", "faktura_dager_for")  # laast etter forste faktura


def antall_faktura_for_kurs(con, kurs_id: int) -> int:
    return con.execute(
        "SELECT COUNT(*) FROM faktura f JOIN paamelding p ON p.id=f.paamelding_id WHERE p.kurs_id=?",
        (kurs_id,)).fetchone()[0]


def oppdater_kurs_felter(con, kurs_id: int, felter: dict, aktor: str = "admin") -> list[str]:
    """Oppdaterer kursfelt (ikke kapasitet/status, se egne funksjoner for dem).

    Saa snart minst en faktura finnes for kurset, blir de fakturarelaterte feltene (pris,
    fakturering, betaling, dager-for-faktura) STRIPPET her ogsaa - ikke bare skjult i UI -
    slik at de ikke kan endres via en direkte POST selv om skjemaet skulle tillate det.
    """
    gammel = con.execute("SELECT * FROM kurs WHERE id=?", (kurs_id,)).fetchone()
    if not gammel:
        raise Paameldingsfeil("Ukjent kurs")
    if antall_faktura_for_kurs(con, kurs_id) > 0:
        felter = {k: v for k, v in felter.items() if k not in KURS_LAASTE_FELT}
    endret = [f for f, v in felter.items() if (gammel[f] if gammel[f] is not None else "") != (v if v is not None else "")]
    if not endret:
        return []
    con.execute(f"UPDATE kurs SET {','.join(f'{f}=?' for f in felter)} WHERE id=?", [*felter.values(), kurs_id])
    logg(con, "kurs_endret", {"kurs_id": kurs_id, "felt": endret}, aktor=aktor)
    return endret


def synk_materiell_ansvarlig(con, kurs_id: int, ny_epost: str | None, aktor: str = "admin") -> int:
    """Naar kursholder endres i Oppsett, oppdaterer vi ogsaa e-posten purringer gaar til - materiell_krav
    er en egen tabell som ellers ville fortsatt aa peke paa den gamle kursholderen. Rorer aldri et krav
    som allerede er levert (det er historikk)."""
    if not ny_epost:
        return 0
    cur = con.execute(
        "UPDATE materiell_krav SET ansvarlig_epost=? WHERE kurs_id=? AND levert_ts IS NULL",
        (ny_epost, kurs_id))
    if cur.rowcount:
        logg(con, "materiell_ansvarlig_endret", {"kurs_id": kurs_id, "antall": cur.rowcount}, aktor=aktor)
    return cur.rowcount


def endre_kapasitet(con, kurs_id: int, ny_kapasitet: int | None, aktor: str = "admin") -> list[int]:
    """Endrer kapasitet. Okes den, rykkes venteliste automatisk opp saa langt det er plass, i samme
    rekkefolge som ved avmelding. Senkes den under antall bekreftede, fjernes INGEN automatisk -
    status settes bare til 'full' saa ingen nye slipper inn. Et avlyst kurs rykker aldri opp venteliste.

    Returnerer paamelding_id-ene som ble rykket opp - kalleren boer kjore disse gjennom sveiper.kjor().
    """
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kurs_id,)).fetchone()
    if not kurs:
        raise Paameldingsfeil("Ukjent kurs")
    if ny_kapasitet == kurs["kapasitet"]:
        return []
    con.execute("UPDATE kurs SET kapasitet=? WHERE id=?", (ny_kapasitet, kurs_id))
    logg(con, "kurs_endret", {"kurs_id": kurs_id, "felt": ["kapasitet"]}, aktor=aktor)

    opprykket = []
    if kurs["status"] != "avlyst":
        while ny_kapasitet is None or antall_bekreftet(con, kurs_id) < ny_kapasitet:
            neste = con.execute(
                "SELECT id FROM paamelding WHERE kurs_id=? AND status='venteliste' ORDER BY opprettet, id LIMIT 1",
                (kurs_id,)).fetchone()
            if not neste:
                break
            con.execute("UPDATE paamelding SET status='bekreftet', sveiper_kjort=0, oppdatert=? WHERE id=?",
                       (datetime.now().isoformat(timespec="seconds"), neste["id"]))
            logg(con, "flyttet_fra_venteliste", {"paamelding_id": neste["id"]}, aktor=aktor)
            opprykket.append(neste["id"])

    if kurs["status"] in ("aapen", "full"):
        na_fullt = ny_kapasitet is not None and antall_bekreftet(con, kurs_id) >= ny_kapasitet
        if na_fullt and kurs["status"] == "aapen":
            con.execute("UPDATE kurs SET status='full' WHERE id=?", (kurs_id,))
        elif not na_fullt and kurs["status"] == "full":
            con.execute("UPDATE kurs SET status='aapen' WHERE id=?", (kurs_id,))
    return opprykket


KURS_STATUS_OVERGANGER = {"utkast": ("aapen",), "aapen": ("utkast",)}


def sett_kurs_status(con, kurs_id: int, ny_status: str, aktor: str = "admin") -> None:
    """Kun utkast <-> aapen tilbys her. 'full'/'aktiv'/'avsluttet' styres utelukkende automatisk
    (kapasitet/dato), og 'avlyst' haandteres av avlys_kurs()/gjenaapne_kurs() med egen bekreftelse."""
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kurs_id,)).fetchone()
    if not kurs:
        raise Paameldingsfeil("Ukjent kurs")
    if ny_status not in KURS_STATUS_OVERGANGER.get(kurs["status"], ()):
        raise Paameldingsfeil("Ugyldig statusendring.")
    con.execute("UPDATE kurs SET status=? WHERE id=?", (ny_status, kurs_id))
    logg(con, "kurs_status_endret", {"kurs_id": kurs_id, "fra": kurs["status"], "til": ny_status}, aktor=aktor)


def avlys_kurs(con, kurs_id: int, aktor: str = "admin") -> None:
    """Setter kurset til avlyst. Dette alene stopper: nye paameldinger (meld_paa avviser status
    utenfor aapen/full/aktiv), nye fakturaer og paaminnelser/innkallinger (daglig.py plukker kun opp
    kurs med status aapen/full/aktiv), nytt Zoom-moete (samme grunn) og ventelisteopprykk (sjekket i
    endre_kapasitet og sett_paamelding_status). Eksisterende paameldinger roeres ikke - de er historikk.
    Fakturaer krediteres IKKE automatisk. Selve varselet til deltakerne sendes av den som kaller dette."""
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kurs_id,)).fetchone()
    if not kurs:
        raise Paameldingsfeil("Ukjent kurs")
    if kurs["status"] == "avlyst":
        raise Paameldingsfeil("Kurset er allerede avlyst.")
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kurs_id,))
    logg(con, "kurs_avlyst", {"kurs_id": kurs_id, "fra": kurs["status"]}, aktor=aktor)


def gjenaapne_kurs(con, kurs_id: int, aktor: str = "admin") -> None:
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kurs_id,)).fetchone()
    if not kurs:
        raise Paameldingsfeil("Ukjent kurs")
    if kurs["status"] != "avlyst":
        raise Paameldingsfeil("Kurset er ikke avlyst.")
    con.execute("UPDATE kurs SET status='aapen' WHERE id=?", (kurs_id,))
    logg(con, "kurs_gjenaapnet", {"kurs_id": kurs_id}, aktor=aktor)


def meld_paa(con, kurs_id: int, *, epost: str, navn: str, deltaker: dict | None = None,
             paamelding: dict | None = None, sensitivt: dict | None = None, idag: date | None = None) -> tuple[int, str]:
    """Registrer paamelding. Returnerer (paamelding_id, status). Setter venteliste naar kurset er fullt."""
    idag = idag or date.today()
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kurs_id,)).fetchone()
    if not kurs:
        raise Paameldingsfeil("Ukjent kurs")
    if kurs["status"] not in ("aapen", "full", "aktiv"):
        raise Paameldingsfeil("Kurset er ikke åpent for påmelding")
    if kurs["paameldingsfrist"] and idag > date.fromisoformat(kurs["paameldingsfrist"]):
        raise Paameldingsfeil(f"Påmeldingsfristen ({kurs['paameldingsfrist']}) er passert.")

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


# ---------- bedriftspaamelding (fase 7) ----------

def _innsendingsnokkel(kurs_id: int, kontakt_epost: str, deltaker_eposter: list[str]) -> str:
    """Stabil, INNHOLDSBASERT nokkel (ikke tilfeldig) - identisk gjeninnsending (dobbeltklikk/refresh)
    gir samme nokkel, slik at den gjenkjennes i stedet for aa behandles paa nytt."""
    unike = ",".join(sorted({e.strip().lower() for e in deltaker_eposter}))
    grunnlag = f"{kurs_id}:{kontakt_epost.strip().lower()}:{unike}"
    return hashlib.sha256(grunnlag.encode()).hexdigest()


def finn_eller_opprett_firmapaamelding(con, kurs_id: int, kontakt: dict, deltaker_eposter: list[str]) -> tuple[sqlite3.Row, bool]:
    """Oppretter en bedriftspaamelding, eller gjenkjenner en identisk gjeninnsending.

    Committer med en gang ved oppretting - det er det som gjor gjenkjenningen paalitelig ogsaa ved to
    tilnaermet samtidige forsok (det andre forsoket treffer da UNIQUE-indeksen og faar samme rad tilbake
    i stedet for aa opprette en ny). Returnerer (rad, True hvis nyopprettet / False hvis den fantes fra for -
    kalleren skal da IKKE behandle deltakerne paa nytt eller sende flere e-poster).
    """
    nokkel = _innsendingsnokkel(kurs_id, kontakt["epost"], deltaker_eposter)
    finnes = con.execute("SELECT * FROM firmapaamelding WHERE innsendingsnokkel=?", (nokkel,)).fetchone()
    if finnes:
        return finnes, False
    try:
        cur = con.execute(
            """INSERT INTO firmapaamelding (kurs_id, innsendingsnokkel, kvittering_token, kontakt_navn, kontakt_epost,
               kontakt_telefon, firmanavn, org_nr, faktura_ref, faktura_adresse, faktura_postnr, faktura_sted, ehf)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (kurs_id, nokkel, secrets.token_urlsafe(32), kontakt["navn"], kontakt["epost"], kontakt.get("telefon"),
             kontakt["firmanavn"], kontakt.get("org_nr"), kontakt.get("faktura_ref"), kontakt.get("faktura_adresse"),
             kontakt.get("faktura_postnr"), kontakt.get("faktura_sted"), 1 if kontakt.get("ehf") else 0))
        con.commit()
    except sqlite3.IntegrityError:
        con.rollback()
        return con.execute("SELECT * FROM firmapaamelding WHERE innsendingsnokkel=?", (nokkel,)).fetchone(), False
    return con.execute("SELECT * FROM firmapaamelding WHERE id=?", (cur.lastrowid,)).fetchone(), True


def registrer_firmapaamelding_rad(con, firmapaamelding_id: int, navn: str, epost: str,
                                  paamelding_id: int | None, feilmelding: str | None) -> None:
    con.execute(
        "INSERT INTO firmapaamelding_rad (firmapaamelding_id, navn, epost, paamelding_id, feilmelding) VALUES (?,?,?,?,?)",
        (firmapaamelding_id, navn, epost, paamelding_id, feilmelding))


# ---------- utsending (dedup) ----------

def allerede_sendt(con, nokkel: str, mottaker: str, type_: str) -> bool:
    return con.execute(
        "SELECT 1 FROM utsending_logg WHERE nokkel=? AND mottaker=? AND type=?", (nokkel, mottaker, type_)
    ).fetchone() is not None


def marker_sendt(con, nokkel: str, mottaker: str, type_: str) -> None:
    con.execute(
        "INSERT OR IGNORE INTO utsending_logg (nokkel, mottaker, type) VALUES (?,?,?)", (nokkel, mottaker, type_)
    )


# ---------- admin: manuell e-post (fase 5) ----------

def opprett_admin_utsending(con, kurs_id: int, emne: str, tekst: str, mottaker_paamelding_ider: list[int],
                            sendt_av_admin_id: int | None) -> tuple[int, str]:
    """Forbereder en manuell utsendelse (kalles ved forhaandsvisning) - sender IKKE noe selv.

    Lager en unik, stabil nokkel som skal folge med til selve sendingen (se Kjoring.send_admin_utsending),
    slik at et dobbeltklikk/refresh paa "Send" gjenbruker samme nokkel og dedupliseres riktig.
    Mottakerne er allerede validert av kalleren (tilhorer kurset) foer denne kalles.
    """
    nokkel = f"adhoc:{kurs_id}:{secrets.token_hex(8)}"
    cur = con.execute(
        "INSERT INTO admin_utsending (nokkel, kurs_id, emne, tekst, sendt_av_admin_id) VALUES (?,?,?,?,?)",
        (nokkel, kurs_id, emne, tekst, sendt_av_admin_id))
    utsending_id = cur.lastrowid
    con.executemany(
        "INSERT INTO admin_utsending_mottaker (utsending_id, paamelding_id) VALUES (?,?)",
        [(utsending_id, pid) for pid in mottaker_paamelding_ider])
    return utsending_id, nokkel


def admin_utsending_mottakere(con, utsending_id: int) -> list[sqlite3.Row]:
    """Den FASTSATTE mottakerlisten for en utsendelse - uavhengig av hva som evt. postes inn senere."""
    return con.execute(
        """SELECT p.id, d.navn, d.epost FROM admin_utsending_mottaker m
           JOIN paamelding p ON p.id=m.paamelding_id JOIN deltaker d ON d.id=p.deltaker_id
           WHERE m.utsending_id=?""", (utsending_id,)).fetchall()


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
