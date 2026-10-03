"""Booking per samling: kurslokale, hotell, grupperom og lunsj (Camilla 03.10.2026: «Når vi trykker på kurset kan det stå
nederst om hotell er booket, med ref. nr. hvor det er booket osv. Der kan det også stå om grupperom er booket eller ikke.
også må det stå på sjekklisten om lunsj er booket eller ikke.»).

  * Én rad per samling og type i samling_booking. Status: booket, ikke booket eller trengs ikke; ingen rad (eller status
    NULL) = ikke satt. Teksten er fri: hvor, referansenummer, hvem rommet er til, pris.
  * Gjelder planlagte samlinger, også de skjulte som holder sjekklisten til et kurs i systemet (sjekklister.synk_kurs).
  * Hendelsesloggen får bare id-er, type og status, aldri teksten (den kan ha navnet på en kursholder).
"""
from . import db

TYPER = (("lokale", "Kurslokale"), ("hotell", "Hotell"), ("grupperom", "Grupperom"), ("lunsj", "Lunsj"))
STATUSER = (("booket", "Booket"), ("ikke_booket", "Ikke booket"), ("trengs_ikke", "Trengs ikke"))
TYPENAVN, STATUSNAVN = dict(TYPER), dict(STATUSER)
MAKS_TEKST = 1000


class BookingFeil(ValueError):
    """Ugyldig type, status eller tekst (meldingen kan vises til brukeren)."""


def for_samlinger(con, samling_ider) -> dict[int, dict[str, dict]]:
    """{samling_id: {type: {status, tekst, endret, endret_av}}} for samlingene som har noe registrert."""
    ider = list(dict.fromkeys(samling_ider))
    if not ider:
        return {}
    ut: dict[int, dict] = {}
    for r in con.execute(f"SELECT * FROM samling_booking WHERE samling_id IN ({','.join('?' * len(ider))})", tuple(ider)):
        ut.setdefault(r["samling_id"], {})[r["type"]] = dict(r)
    return ut


def linjer(bookinger: dict | None) -> list[dict]:
    """Én linje per type i fast rekkefølge, til visningen: {type, navn, status, statusnavn, tekst}."""
    ut = []
    for type_, navn in TYPER:
        b = (bookinger or {}).get(type_) or {}
        status = b.get("status")
        ut.append({"type": type_, "navn": navn, "status": status, "statusnavn": STATUSNAVN.get(status, "Ikke satt"),
                   "tekst": b.get("tekst")})
    return ut


def _tekst(verdi) -> str | None:
    tekst = "\n".join(linje.rstrip() for linje in str(verdi or "").strip().splitlines()).strip()
    if len(tekst) > MAKS_TEKST:
        raise BookingFeil(f"Teksten kan være høyst {MAKS_TEKST} tegn.")
    return tekst or None


def sett(con, samling_id: int, type_: str, status: str | None, tekst, aktor: str) -> str:
    """Setter status og tekst for én type på samlingen. Tom status og tom tekst fjerner raden (ikke satt).
    Returnerer «lagret», «fjernet» eller «uendret»."""
    if type_ not in TYPENAVN:
        raise BookingFeil("Ukjent type.")
    status = (status or "").strip() or None
    if status is not None and status not in STATUSNAVN:
        raise BookingFeil("Ukjent status.")
    tekst = _tekst(tekst)
    if not con.execute("SELECT 1 FROM planlagt_samling WHERE id=?", (samling_id,)).fetchone():
        raise BookingFeil("Samlingen finnes ikke.")
    finnes = con.execute("SELECT status, tekst FROM samling_booking WHERE samling_id=? AND type=?",
                         (samling_id, type_)).fetchone()
    if status is None and tekst is None:
        if not finnes:
            return "uendret"
        con.execute("DELETE FROM samling_booking WHERE samling_id=? AND type=?", (samling_id, type_))
        utfall = "fjernet"
    elif finnes and (finnes["status"], finnes["tekst"]) == (status, tekst):
        return "uendret"
    else:
        if finnes:
            con.execute("UPDATE samling_booking SET status=?, tekst=?, endret=?, endret_av=? WHERE samling_id=? AND type=?",
                        (status, tekst, db.naa_utc(), aktor, samling_id, type_))
        else:
            db.sett_inn(con, "INSERT INTO samling_booking (samling_id, type, status, tekst, endret, endret_av) "
                             "VALUES (?,?,?,?,?,?)", (samling_id, type_, status, tekst, db.naa_utc(), aktor))
        utfall = "lagret"
    db.logg(con, "samling_booking_endret", {"samling_id": samling_id, "type": type_, "status": status}, aktor=aktor)
    return utfall
