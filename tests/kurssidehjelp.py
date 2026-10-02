"""Felles hjelpere for kursside-testene (test_kursside_*.py). Bare oppdiktede data: ingen ekte personer, e-postadresser eller filer.

Filene er minimale, men gyldige for innholdssjekken (kurs/sidelager.py): riktig «signatur» først i filen, ellers tomt innhold.
"""
import atexit
import os
import shutil
import struct
import tempfile
import zlib
from datetime import date, timedelta
from pathlib import Path

from kurs import config, db
from kurs import sideinnhold as si
from kurs import sidelager

IDAG = date(2027, 3, 10)          # «i dag» i testene der datoen styres


# ============================ filer ============================

def png(bredde: int = 4, hoyde: int = 3) -> bytes:
    def bit(navn, d):
        return struct.pack(">I", len(d)) + navn + d + struct.pack(">I", zlib.crc32(navn + d) & 0xFFFFFFFF)
    rader = (b"\x00" + b"\x1f\x5c\x73" * bredde) * hoyde
    return (b"\x89PNG\r\n\x1a\n" + bit(b"IHDR", struct.pack(">IIBBBBB", bredde, hoyde, 8, 2, 0, 0, 0))
            + bit(b"IDAT", zlib.compress(rader)) + bit(b"IEND", b""))


def jpeg(bredde: int = 40, hoyde: int = 30) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, hoyde, bredde, 3) + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9" + b"\x00" * 16


def gif(bredde: int = 8, hoyde: int = 8) -> bytes:
    return b"GIF89a" + struct.pack("<HH", bredde, hoyde) + b"\x80\x00\x00" + b"\x00" * 30 + b";"


def webp(bredde: int = 100, hoyde: int = 50) -> bytes:
    """RIFF/WEBP med en VP8X-blokk (bredde og høyde minus 1 som 24-bit tall)."""
    vp8x = b"\x00\x00\x00\x00" + (bredde - 1).to_bytes(3, "little") + (hoyde - 1).to_bytes(3, "little")
    kropp = b"WEBP" + b"VP8X" + struct.pack("<I", len(vp8x)) + vp8x
    return b"RIFF" + struct.pack("<I", len(kropp)) + kropp


def pdf(ekstra: bytes = b"") -> bytes:
    return b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n" + ekstra


def zip_lik(ekstra: bytes = b"") -> bytes:
    return b"PK\x03\x04" + b"\x14\x00" + b"\x00" * 30 + ekstra


def ole(ekstra: bytes = b"") -> bytes:
    return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 40 + ekstra


# ============================ database og kurs ============================

_MAL_DATABASE: Path | None = None    # ferdig migrert SQLite-fil som kopieres til hver test (db.init er treg, og lik hver gang)


def _mal_database() -> Path:
    global _MAL_DATABASE
    if _MAL_DATABASE is None:
        mappe = Path(tempfile.mkdtemp(prefix="kursside-mal-"))
        atexit.register(shutil.rmtree, mappe, ignore_errors=True)
        con = db.koble(mappe / "mal.db")
        db.init(con)
        con.close()
        _MAL_DATABASE = mappe / "mal.db"
    return _MAL_DATABASE


def ny_database(tmp_path, monkeypatch):
    """En ny, migrert database for testen. SQLite: en kopi av en ferdig migrert mal (raskt). PostgreSQL-modus (TEST_DATABASE_URL):
    hver test får sitt eget skjema og db.init kjøres som vanlig."""
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    if os.environ.get("TEST_DATABASE_URL", "").strip():
        con = db.koble(tmp_path / "test.db")
        db.init(con)
        return con
    shutil.copyfile(_mal_database(), tmp_path / "test.db")
    return db.koble(tmp_path / "test.db")


def lag_kurs(con, kode="K1", start: date = IDAG, dager: int = 2, **felt) -> int:
    """Et fysisk kurs som er åpent, med `dager` sammenhengende kursdager fra og med `start`."""
    datoer = [(start + timedelta(days=n)).isoformat() for n in range(dager)]
    kid = db.opprett_kurs(con, kode=kode, navn=f"Kurs {kode}", datoer=datoer, type="fysisk", sted="Bergen", status="aapen",
                          sharepoint_mappe=f"Kurs/{kode}", **felt)
    con.commit()
    return kid


def lag_deltaker(con, kid: int, epost="kari@example.no", fornavn="Kari", etternavn="Nordmann", status="bekreftet"):
    """(deltaker_id, paamelding_id). `status` er en av påmeldingsstatusene i db.PAAMELDINGSSTATUSER (Påmeldt er standard;
    databaseverdien «bekreftet» tas også som Påmeldt)."""
    pid, _ = db.meld_paa(con, kid, epost=epost, fornavn=fornavn, etternavn=etternavn)
    if status not in ("bekreftet", "paameldt"):
        db.sett_paamelding_status(con, pid, status, aktor="admin:test")
    con.commit()
    return con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pid,)).fetchone()[0], pid


def tekstblokk(tittel="Velkommen", html="<p>Hei alle sammen</p>", **felt) -> dict:
    return {"id": felt.pop("id", ""), "type": "tekst", "tittel": tittel, "data": {"html": html}, **felt}


def dokument(*blokker, **topp) -> dict:
    return {"v": 1, "tittel": "", "ingress": "", "rom": "", "melding": {"tekst": "", "niva": "info"}, "blokker": list(blokker), **topp}


def skriv_side(con, kid: int, dok: dict, *, publiser: bool = True, aktor: str = "admin:test") -> int:
    """Validerer og lagrer siden (og publiserer den). Returnerer den nye versjonen. Committer."""
    filer, dager = sidelager.valideringsgrunnlag(con, kid)
    renset, _merknader = si.valider(dok, fil_ider=filer, kursdag_ider=dager)
    _gammel, versjon = sidelager.hent_utkast(con, kid)
    ny = sidelager.lagre_utkast(con, kid, renset, versjon, aktor)
    assert ny is not None
    if publiser:
        assert sidelager.publiser(con, kid, ny, aktor) == ny
    con.commit()
    return ny


# ============================ nettleser ============================

def csrf(klient) -> str:
    with klient.session_transaction() as s:
        return s.setdefault("csrf", "testtoken")


def admin_klient(con=None, rolle: str = "system", brukernavn: str | None = None):
    """Innlogget administrator. Andre roller enn standardbrukeren (system) lages i databasen først."""
    from kurs.web import app as webapp
    passord = config.ADMIN_PASSORD
    navn = config.ADMIN_BRUKERNAVN
    if rolle != "system":
        navn, passord = brukernavn or f"bruker_{rolle}", "passord-som-holder-1"
        db.opprett_admin_bruker(con, navn, f"Bruker {rolle}", passord, rolle=rolle)
        con.commit()
    k = webapp.app.test_client()
    assert k.post("/admin/logg-inn", data={"brukernavn": navn, "passord": passord}).status_code == 302
    return k


def deltaker_klient(deltaker_id: int | None = None):
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    if deltaker_id is not None:
        with k.session_transaction() as s:
            s["deltaker_id"] = deltaker_id
    return k


def fast_dato(monkeypatch, dato: date = IDAG) -> None:
    """Styrer «i dag» i appen (demo-spoling): rutene kaller webapp._idag ved hver forespørsel."""
    from kurs.web import app as webapp
    monkeypatch.setattr(webapp, "_idag", lambda: dato)


def json_post(klient, url: str, kropp: dict, **kw):
    return klient.post(url, json=kropp, headers={"X-CSRF-Token": csrf(klient)}, **kw)
