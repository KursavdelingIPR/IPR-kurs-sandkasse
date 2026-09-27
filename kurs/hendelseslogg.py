"""Lesbar logg for én påmelding: fanen «Logger» i deltakervinduet.

Hendelsestabellen lagrer tekniske navn og id-er, aldri navn, e-post eller sensitive verdier (GDPR.md). Her blir de til
tekst som er lett å lese: hva som skjedde, hvem som gjorde det, og dato og klokkeslett i norsk tid. Detaljer (f.eks.
status før endringen) kan åpnes per rad. Rå tekniske data vises bare for systemadministrator.

E-poster vises ikke her, de hører til e-postfanen. Innsyn (hvem som har åpnet opplysninger der allergier og
tilrettelegging vises) er en egen liste som bare systemadministrator får.
"""
import json
from dataclasses import dataclass, field
from datetime import date

from . import behandling, norsk_tid
from .sveiper import PLAN_MANGLER_KURSDAG

# Vises ikke i Logger: e-post har egen fane, og innsyn har egen liste (bare for systemadministrator)
_EPOST = {"admin_epost_sendt", "epost_ukjent", "uavklart_epost_avklart"}
_INNSYN = "sensitivt_vist"
# Gjelder personen og dermed alle påmeldingene til personen (detaljer har deltaker_id, ikke paamelding_id)
_PERSONHENDELSER = ("deltaker_endret", "deltaker_anonymisert")
# db.meld_av logger «avmelding» i samme lagring som statusendringen eller avslaget som kalte den - det er én handling
_SAMME_HANDLING_SEK = 5

# Samme ord som i Deltaker-fanen (admin_deltaker.html)
KILDER = {"skjema": "Påmeldingsskjemaet", "nettside": "Skjemaet på nettsiden", "gruppe": "Bedriftspåmelding",
          "admin": "Manuelt av administrator", "admin_import": "Import fra fil"}
_STATUS = {"bekreftet": "bekreftet", "venteliste": "venteliste", "avmeldt": "avmeldt"}
_PERSONFELT = {"fornavn": "Fornavn", "etternavn": "Etternavn", "navn": "Navn", "epost": "E-post", "telefon": "Telefon",
               "yrkestittel": "Yrkestittel", "arbeidssted": "Arbeidssted", "hpr_nr": "HPR-nummer",
               "visma_kunde_id": "Kundenummer i Visma"}
_PAAMELDINGSFELT = {"betaler": "Hvem betaler", "betaling": "Betaling", "org_nr": "Organisasjonsnummer",
                    "org_navn": "Firma-/avdelingsnavn", "faktura_epost": "E-post for faktura", "ehf": "EHF",
                    "faktura_adresse": "Fakturaadresse", "faktura_postnr": "Postnummer", "faktura_sted": "Poststed",
                    "faktura_ref": "Faktura merkes med", "faktura_kommentar": "Kommentar til faktura",
                    "intern_kommentar": "Intern kommentar"}
_INTERNE_FELT = {"intern_kommentar"}


@dataclass
class Rad:
    hva: str
    hvem: str
    dato: str
    klokkeslett: str
    detaljer: list[str] = field(default_factory=list)
    teknisk: str = ""               # rå hendelse til feilsøking - vises bare for systemadministrator


@dataclass
class Logg:
    rader: list[Rad]
    innsyn: list[Rad] | None        # None: ikke systemadministrator, og delen vises ikke i det hele tatt


def for_paamelding(con, p, *, systemadmin: bool) -> Logg:
    """Logger-radene for påmeldingen `p` (en paamelding-rad), nyeste først."""
    hendelser = _hendelser(con, p)
    o = _Oppslag(con, p, hendelser, systemadmin=systemadmin)
    rader = []
    for h in hendelser:
        if h["handling"] in _EPOST or h["handling"] == _INNSYN or o.er_del_av_annen_handling(h):
            continue
        rader += [o.rad(h, hva, detaljer) for hva, detaljer in _tekster(h, o)]
    innsyn = None
    if systemadmin:
        apnet = [(h, "Åpnet deltakeren") for h in hendelser if h["handling"] == _INNSYN]
        apnet += [(h, "Åpnet allergilisten for kurset") for h in _allergilister(con, p)]
        innsyn = [o.rad(h, hva, []) for h, hva in sorted(apnet, key=lambda x: x[0]["id"], reverse=True)]
    return Logg(rader, innsyn)


def _hendelser(con, p) -> list:
    """Hendelsene for påmeldingen og for personen, nyeste først. `detaljer` er JSON fra db.logg (json.dumps med vanlige
    skilletegn), så '"paamelding_id": 12,' eller '"paamelding_id": 12}' treffer nøyaktig 12, ikke 1 eller 120."""
    pid, did = p["id"], p["deltaker_id"]
    return con.execute(
        f"""SELECT id, ts, aktor, handling, detaljer FROM hendelse
            WHERE detaljer LIKE ? OR detaljer LIKE ?
               OR (handling IN ({','.join('?' * len(_PERSONHENDELSER))}) AND (detaljer LIKE ? OR detaljer LIKE ?))
            ORDER BY id DESC""",
        (f'%"paamelding_id": {pid},%', f'%"paamelding_id": {pid}}}%', *_PERSONHENDELSER,
         f'%"deltaker_id": {did},%', f'%"deltaker_id": {did}}}%')).fetchall()


def _allergilister(con, p) -> list:
    """Visninger av kursets allergiliste etter at påmeldingen ble registrert (den viser alle bekreftede deltakere)."""
    return con.execute(
        "SELECT id, ts, aktor, handling, detaljer FROM hendelse WHERE handling=? AND detaljer=? AND ts >= ?",
        (_INNSYN, json.dumps({"kurs_id": p["kurs_id"]}), str(p["opprettet"]).replace("T", " "))).fetchall()


def _detaljer(h) -> dict:
    try:
        d = json.loads(h["detaljer"] or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


class _Oppslag:
    """Det radene trenger fra databasen utover selve hendelsen: navn på administratorer og datoer for kursdager."""

    def __init__(self, con, p, hendelser, *, systemadmin: bool):
        self.p = p
        self.systemadmin = systemadmin
        self._admin = {r["brukernavn"].lower(): r["navn"]
                       for r in con.execute("SELECT brukernavn, navn FROM admin_bruker")}
        self._kursdager = {r["id"]: r["dato"] for r in con.execute("SELECT id, dato FROM kursdag WHERE kurs_id=?",
                                                                    (p["kurs_id"],))}
        paameldinger = [h["id"] for h in hendelser if h["handling"] == "paamelding"]
        self.forste_paamelding = min(paameldinger, default=None)
        self.siste_paamelding = max(paameldinger, default=None)
        # Statusendringer og avslag som selv kaller db.meld_av (og dermed også logger «avmelding»)
        self._avmeldinger = [(h["aktor"], norsk_tid.fra_utc(h["ts"])) for h in hendelser
                             if h["handling"] == "paamelding_avslatt"
                             or (h["handling"] == "status_endret" and _detaljer(h).get("til") == "avmeldt")]

    def er_del_av_annen_handling(self, h) -> bool:
        if h["handling"] != "avmelding":
            return False
        tid = norsk_tid.fra_utc(h["ts"])
        return any(aktor == h["aktor"] and tid and annen and abs((tid - annen).total_seconds()) <= _SAMME_HANDLING_SEK
                   for aktor, annen in self._avmeldinger)

    def kursdag(self, kursdag_id) -> str:
        dato = self._kursdager.get(kursdag_id)
        return date.fromisoformat(dato).strftime("%d.%m.%Y") if dato else "(en kursdag som er slettet)"

    def hvem(self, h) -> str:
        aktor = h["aktor"] or ""
        if aktor == "system":
            return "Systemet (automatisk)"
        if aktor == "admin":
            return "Administrator"
        type_, _, verdi = aktor.partition(":")
        if type_ == "admin":
            return self._admin.get(verdi.lower(), verdi)
        if type_ == "deltaker":
            gruppe = h["handling"] == "paamelding" and self.p["kilde"] == "gruppe"
            return "Bedriftens kontaktperson" if gruppe else "Deltakeren selv"
        if type_ == "materiell":
            return "Kursholder"
        return "Ukjent"

    def rad(self, h, hva: str, detaljer: list[str]) -> Rad:
        tid = norsk_tid.fra_utc(h["ts"])
        teknisk = f"Hendelse nr. {h['id']} · {h['handling']} · {h['aktor']} · {h['detaljer'] or ''}"
        return Rad(hva=hva, hvem=self.hvem(h), dato=norsk_tid.dato(tid), klokkeslett=norsk_tid.klokkeslett(tid),
                   detaljer=detaljer, teknisk=teknisk if self.systemadmin else "")


# ---------- tekstene: hver hendelse gir én eller flere (hva, detaljer) ----------

def _tekster(h, o: _Oppslag) -> list[tuple[str, list[str]]]:
    lag = _TEKSTER.get(h["handling"])
    return lag(_detaljer(h), o, h) if lag else [("Annen hendelse", [])]


def _status(verdi) -> str:
    return _STATUS.get(verdi, str(verdi))


def _status_for(d) -> list[str]:
    return [f"Status før: {_status(d['fra']).capitalize()}"] if d.get("fra") else []


def _felt(felt, navn: dict) -> str:
    return ", ".join(navn.get(f, str(f).replace("_", " ").capitalize()) for f in felt)


def _kr(belop) -> str:
    return f"{int(belop or 0):,}".replace(",", " ") + " kr"


def _samling(d, o) -> list[str]:
    return [f"Gjelder samlingen {o.kursdag(d['kursdag_id'])}"] if d.get("kursdag_id") else []


def _paamelding(d, o, h):
    start = "Påmeldt på nytt" if h["id"] != o.forste_paamelding else "Påmeldt"
    hva = {"bekreftet": f"{start} – bekreftet",
           "venteliste": f"{start} – satt på venteliste"}.get(d.get("status"), start)
    kilde = KILDER.get(o.p["kilde"]) if h["id"] == o.siste_paamelding else None
    return [(hva, [f"Kilde: {kilde}"] if kilde else [])]


def _status_endret(d, o, h):
    til = d.get("til")
    if til == "bekreftet" and "over_kapasitet" in d:
        plasser = d["over_kapasitet"]
        return [("Bekreftet over kapasitet",
                 [f"Kurset hadde {plasser} {'plass' if plasser == 1 else 'plasser'}.", *_status_for(d)])]
    if til == "venteliste":
        return [("Satt på venteliste", _status_for(d))]
    if til == "avmeldt":
        return [("Avmeldt", _status_for(d))]
    fra = f"fra {_status(d['fra'])} " if d.get("fra") else ""
    return [(f"Status endret {fra}til {_status(til)}", [])]


def _flyttet_opp(d, o, h):
    aarsak = "Kurset fikk flere plasser." if str(h["aktor"]).startswith("admin") else "En plass ble ledig."
    return [("Flyttet opp fra venteliste", [aarsak])]


def _manuell_behandling(d, o, h):
    resultat = d.get("resultat")
    detaljer = [f"Resultat: {behandling.KATEGORI_NAVN.get(resultat, resultat or 'ukjent')}"]
    if d.get("via") == "bulk":
        detaljer.append("Behandlet sammen med flere påmeldinger fra deltakerlisten.")
    return [("Påmeldingen ble behandlet manuelt", detaljer)]


def _fakturert(d, o, h):
    detaljer = [f"Fakturanummer: {d['faktura_nr']}"] if d.get("faktura_nr") else []
    if d.get("belop") is not None:
        detaljer.append(f"Beløp: {_kr(d['belop'])}")
    return [("Faktura opprettet", detaljer + _samling(d, o))]


def _faktura_feil(d, o, h):
    if d.get("arsak") == PLAN_MANGLER_KURSDAG:
        return [("Faktura ble ikke laget", ["Kurset mangler kursdager."])]
    return [("Fakturering feilet", [])]


def _faktura_avklart(d, o, h):
    if d.get("utfall") == "fakturert":
        nr = [f"Fakturanummer: {d['faktura_nr']}"] if d.get("faktura_nr") else []
        return [("Uavklart faktura avklart: fakturaen finnes", nr + _samling(d, o))]
    return [("Uavklart faktura avklart: ikke fakturert – prøves igjen", _samling(d, o))]


def _oppmote(d, o, h):
    endring = {True: "registrert", False: "fjernet"}.get(d.get("registrert"), "endret")
    return [(f"Oppmøte {endring} for kursdagen {o.kursdag(d.get('kursdag_id'))}", [])]


def _deltaker_endret(d, o, h):
    felt = list(d.get("felt") or [])
    if "navn" in felt and ("fornavn" in felt or "etternavn" in felt):
        felt.remove("navn")                  # fullt navn følger automatisk av fornavn og etternavn
    detaljer = [f"Endret: {_felt(felt, _PERSONFELT)}"] if felt else []
    return [("Personopplysninger endret", detaljer + ["Gjelder personen, ikke bare denne påmeldingen."])]


def _paamelding_endret(d, o, h):
    felt = list(d.get("felt") or [])
    andre = [f for f in felt if f not in _INTERNE_FELT]
    interne = [f for f in felt if f in _INTERNE_FELT]
    rader = []
    if andre or not interne:
        rader.append(("Påmeldingsopplysninger endret", [f"Endret: {_felt(andre, _PAAMELDINGSFELT)}"] if andre else []))
    if interne:
        rader.append(("Interne opplysninger endret", [f"Endret: {_felt(interne, _PAAMELDINGSFELT)}"]))
    return rader


_TEKSTER = {
    "paamelding": _paamelding,
    "status_endret": _status_endret,
    "avmelding": lambda d, o, h: [("Avmeldt", [])],
    "paamelding_avslatt": lambda d, o, h: [("Avslått", _status_for(d))],
    "flyttet_fra_venteliste": _flyttet_opp,
    "manuell_behandling_utlost": _manuell_behandling,
    "fakturert": _fakturert,
    "faktura_feil": _faktura_feil,
    "faktura_feilet": lambda d, o, h: [("Fakturering feilet – prøves igjen automatisk", _samling(d, o))],
    "faktura_ukjent": lambda d, o, h: [("Fakturering uavklart – må kontrolleres i Visma", _samling(d, o))],
    "faktura_reservert_gammel": lambda d, o, h: [("Fakturering uavklart – må kontrolleres i Visma", _samling(d, o))],
    "uavklart_faktura_avklart": _faktura_avklart,
    "oppmote_manuell": _oppmote,
    "deltaker_endret": _deltaker_endret,
    "paamelding_endret": _paamelding_endret,
    "sensitivt_endret": lambda d, o, h: [("Allergier/tilrettelegging endret", [])],
    "deltaker_anonymisert": lambda d, o, h: [("Personopplysninger slettet (retten til sletting)", [])],
    "sveip_feil": lambda d, o, h: [("Automatisk behandling feilet", [])],
    "kursbevis_mal_feil": lambda d, o, h: [("Kursbevis ble ikke laget – feil i e-postmalen for kursbevis", [])],
}
