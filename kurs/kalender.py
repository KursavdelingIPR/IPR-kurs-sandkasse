"""Kalenderfil (.ics) med kursdagene, vedlagt bekreftelsen (Camilla 05.10.2026: «Deltakeren legger kursdagene inn i
kalenderen med ett klikk»).

Én hendelse per kursdag med dagens egne klokkeslett (kursdag -> samling -> kurs, som kursdatoer.visning). Ingen
METHOD:REQUEST: filen er en vanlig kalenderfil som legges til, ikke en møteinnkalling som skal besvares. UID-ene er faste
per kursdag, så samme dag importert to ganger blir én hendelse i de fleste kalendere. Ingen personopplysninger og ingen
Zoom-lenke (den kommer i egen e-post før kurset).
"""
from datetime import date, datetime, timezone

from .integrasjoner.epost import Vedlegg

FILNAVN = "kursdager.ics"

# Norsk tid med sommertid, slik Outlook og Google forventer den i filen
_VTIMEZONE = (
    "BEGIN:VTIMEZONE", "TZID:Europe/Oslo",
    "BEGIN:DAYLIGHT", "TZOFFSETFROM:+0100", "TZOFFSETTO:+0200", "TZNAME:CEST", "DTSTART:19700329T020000",
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU", "END:DAYLIGHT",
    "BEGIN:STANDARD", "TZOFFSETFROM:+0200", "TZOFFSETTO:+0100", "TZNAME:CET", "DTSTART:19701025T030000",
    "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU", "END:STANDARD",
    "END:VTIMEZONE")


def _felt(rad, navn):
    try:
        return rad[navn]
    except (KeyError, IndexError):
        return None


def _tekst(verdi) -> str:
    """Tekstverdi etter RFC 5545: \\ ; , og linjeskift escapes."""
    return (str(verdi or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _brett(linje: str) -> list[str]:
    """Linjer over 75 byte brettes (fortsettelse starter med mellomrom), uten å dele et tegn i to."""
    ut, del_ = [], ""
    for tegn in linje:
        if len((del_ + tegn).encode("utf-8")) > (75 if not ut else 74):
            ut.append(del_)
            del_ = ""
        del_ += tegn
    ut.append(del_)
    return [ut[0]] + [" " + d for d in ut[1:]]


def _tid(dag: date, kl: str) -> str:
    t, m = (kl or "09:00").split(":")[:2]
    return f"{dag:%Y%m%d}T{int(t):02d}{int(m):02d}00"


def lag_ics(kurs, dager, *, naa: datetime | None = None) -> bytes:
    """Kalenderfilen for kursdagene `dager` (rader fra db.kursdager / db.paameldingens_kursdager)."""
    stempel = (naa or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    sted = "Zoom (lenken kommer på e-post før kurset)" if kurs["type"] == "digital" else (kurs["sted"] or "")
    linjer = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Institutt for Psykologisk Radgivning//Kurs//NO",
              "CALSCALE:GREGORIAN", "METHOD:PUBLISH", *_VTIMEZONE]
    for d in sorted(dager, key=lambda x: str(x["dato"])):
        dag = d["dato"] if isinstance(d["dato"], date) else date.fromisoformat(str(d["dato"])[:10])
        start = d["start_kl"] or _felt(d, "samling_start_kl") or kurs["start_kl"]
        slutt = d["slutt_kl"] or _felt(d, "samling_slutt_kl") or kurs["slutt_kl"]
        beskrivelse = f"Kursnr. {kurs['kursnr']}." + (f" {_felt(d, 'merknad')}" if _felt(d, "merknad") else "")
        linjer += ["BEGIN:VEVENT", f"UID:ipr-kurs-{kurs['id']}-{dag:%Y%m%d}@ipr.no", f"DTSTAMP:{stempel}",
                   f"DTSTART;TZID=Europe/Oslo:{_tid(dag, start)}", f"DTEND;TZID=Europe/Oslo:{_tid(dag, slutt)}",
                   f"SUMMARY:{_tekst(kurs['navn'])}", f"LOCATION:{_tekst(sted)}", f"DESCRIPTION:{_tekst(beskrivelse)}",
                   "TRANSP:OPAQUE", "END:VEVENT"]
    linjer.append("END:VCALENDAR")
    return ("\r\n".join(l for linje in linjer for l in _brett(linje)) + "\r\n").encode("utf-8")


def vedlegg(kurs, dager) -> list[Vedlegg]:
    """[kalenderfilen] til bekreftelsen, eller [] når kurset ikke har kursdager ennå."""
    return [Vedlegg(FILNAVN, "text/calendar", lag_ics(kurs, dager))] if dager else []
