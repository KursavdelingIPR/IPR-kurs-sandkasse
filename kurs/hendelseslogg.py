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

from . import behandling, db, norsk_tid
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
# Overskriften når admin setter en status (til påmeldt eller ekstradeltaker: «Status endret fra … til påmeldt» eller over
# kapasitet). Eldre hendelser har databaseverdien «bekreftet» i stedet for «paameldt» (db.statusnavn tar begge)
_SATT_TIL = {"venteliste": "Satt på venteliste", "avmeldt": "Avmeldt", "avslatt": "Avslått", "utgatt": "Satt til utgått",
             "forlatt": "Satt til forlatt"}
_AVSLUTTET = {"avmeldt", "avslatt", "utgatt", "forlatt"}      # db.meld_av logger «avmelding» som del av disse
_PERSONFELT = {"fornavn": "Fornavn", "etternavn": "Etternavn", "navn": "Navn", "epost": "E-post", "telefon": "Telefon",
               "yrkestittel": "Yrkestittel", "arbeidssted": "Arbeidssted", "hpr_nr": "HPR-nummer",
               "adresse": "Adresse", "postnr": "Postnummer", "poststed": "Poststed",
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
    """Visninger av kursets allergiliste etter at påmeldingen ble registrert (den viser alle påmeldte deltakere)."""
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
                             or (h["handling"] == "status_endret" and _detaljer(h).get("til") in _AVSLUTTET)]

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
    """Statusnavnet med små bokstaver midt i en setning. Ordene står bare i db.PAAMELDINGSSTATUSER. `verdi` er en
    visningsstatus (paameldt, ekstradeltaker ...) eller, i hendelser fra før migrering 16, databaseverdien bekreftet."""
    return str(db.statusnavn(verdi)).lower()


def _status_for_og_ny(d, ny: str | None = None) -> list[str]:
    """«Status før» og «Ny status» - admin skal alltid kunne se begge i detaljene."""
    ny = ny or d.get("til")
    for_ = [f"Status før: {_status(d['fra']).capitalize()}"] if d.get("fra") else []
    return for_ + ([f"Ny status: {_status(ny).capitalize()}"] if ny else [])


def _felt(felt, navn: dict) -> str:
    return ", ".join(navn.get(f, str(f).replace("_", " ").capitalize()) for f in felt)


def _kr(belop) -> str:
    return f"{int(belop or 0):,}".replace(",", " ") + " kr"


def _samling(d, o) -> list[str]:
    return [f"Gjelder samlingen {o.kursdag(d['kursdag_id'])}"] if d.get("kursdag_id") else []


def _deltar_paa(d) -> list[str]:
    """«Deltar på: hele kurset» / «Deltar på: 2 samlinger» (bare antallet: hendelsen har aldri navn, bare id-er og tall)."""
    if "hele_kurset" not in d:
        return []
    antall = int(d.get("antall_samlinger", d.get("antall", 0)) or 0)
    return ["Deltar på: hele kurset" if d["hele_kurset"] else f"Deltar på: {antall} {'samling' if antall == 1 else 'samlinger'}"]


def _paamelding(d, o, h):
    nytt = h["id"] != o.forste_paamelding
    if d.get("ekstradeltaker"):        # «Legg til deltaker» med avkrysning: på kurset uten å ta plass, med utvalget av samlinger
        hva = "Registrert som ekstradeltaker" + (" på nytt" if nytt else "")
    else:
        start = "Påmeldt på nytt" if nytt else "Påmeldt"
        hva = {"venteliste": f"{start} – satt på venteliste"}.get(d.get("status"), start)   # «Påmeldt» = har plass
    kilde = KILDER.get(o.p["kilde"]) if h["id"] == o.siste_paamelding else None
    return [(hva, _deltar_paa(d) + ([f"Kilde: {kilde}"] if kilde else []))]


def _utvalg_endret(d, o, h):
    return [("Samlingsutvalget endret", _deltar_paa(d))]


def _status_endret(d, o, h):
    til = d.get("til")
    detaljer = _status_for_og_ny(d) + (_deltar_paa(d) if til == "ekstradeltaker" else [])
    if til in (*db.HAR_PLASS, db.DATABASEVERDI_PLASS) and "over_kapasitet" in d:
        plasser = d["over_kapasitet"]
        return [(f"{db.statusnavn(til)} over kapasitet",
                 [f"Kurset hadde {plasser} {'plass' if plasser == 1 else 'plasser'}.", *detaljer])]
    if til in _SATT_TIL:
        return [(_SATT_TIL[til], detaljer)]
    fra = f"fra {_status(d['fra'])} " if d.get("fra") else ""
    return [(f"Status endret {fra}til {_status(til)}", detaljer)]


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


def _oppmote_selv(d, o, h):
    """Deltakeren registrerte selv oppmøtet (QR-koden i lokalet, innsjekk med kode, eller «Registrer oppmøte i dag» på nett). Aktøren er
    deltakeren selv, så «Hvem» blir «Deltakeren selv». Detaljene sier hvordan (kilde i hendelsen: qr eller kode)."""
    hvordan = {"qr": "Registrert med QR-koden i kurslokalet", "kode": "Registrert med dagens innsjekk-kode"}.get(d.get("kilde"))
    return [(f"Oppmøte registrert av deltakeren selv for kursdagen {o.kursdag(d.get('kursdag_id'))}", [hvordan] if hvordan else [])]


def _deltaker_endret(d, o, h):
    felt = list(d.get("felt") or [])
    if "navn" in felt and ("fornavn" in felt or "etternavn" in felt):
        felt.remove("navn")                  # fullt navn følger automatisk av fornavn og etternavn
    detaljer = [f"Endret: {_felt(felt, _PERSONFELT)}"] if felt else []
    return [("Personopplysninger endret", detaljer + ["Gjelder personen, ikke bare denne påmeldingen."])]


def _plan_endret(d, o, h):
    plan = {"samlet": "Samlet – hele beløpet i én faktura", "per_samling": "Per samling – én faktura før hver samling"}
    return [("Fakturaplan endret (unntak fra kursets plan)",
             [f"Før: {plan.get(d.get('fra'), d.get('fra') or 'ukjent')}", f"Ny: {plan.get(d.get('til'), d.get('til') or 'ukjent')}"])]


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
    "ekstradeltaker_utvalg": _utvalg_endret,
    "avmelding": lambda d, o, h: [("Avmeldt", [])],
    "paamelding_avslatt": lambda d, o, h: [("Avslått", _status_for_og_ny(d, "avslatt"))],
    "flyttet_fra_venteliste": _flyttet_opp,
    "manuell_behandling_utlost": _manuell_behandling,
    "fakturert": _fakturert,
    "faktura_feil": _faktura_feil,
    "faktura_feilet": lambda d, o, h: [("Fakturering feilet – prøves igjen automatisk", _samling(d, o))],
    "faktura_ukjent": lambda d, o, h: [("Fakturering uavklart – må kontrolleres i Visma", _samling(d, o))],
    "faktura_reservert_gammel": lambda d, o, h: [("Fakturering uavklart – må kontrolleres i Visma", _samling(d, o))],
    "uavklart_faktura_avklart": _faktura_avklart,
    "oppmote_manuell": _oppmote,
    "oppmote_selv": _oppmote_selv,
    "deltaker_endret": _deltaker_endret,
    "paamelding_endret": _paamelding_endret,
    "faktura_plan_endret": _plan_endret,
    "sensitivt_endret": lambda d, o, h: [("Allergier/tilrettelegging endret", [])],
    "deltaker_anonymisert": lambda d, o, h: [("Personopplysninger slettet (retten til sletting)", [])],
    "sveip_feil": lambda d, o, h: [("Automatisk behandling feilet", [])],
    "min_side_lenke_stengt": lambda d, o, h: [("Lenken til Min side ble stengt", ["Ingen lenke virker før en ny lages."])],
    "min_side_lenke_fornyet": lambda d, o, h: [(
        "Ny lenke til Min side ble laget",
        ["Lenken som var sendt før, virker ikke lenger." + (" E-postadressen ble rettet." if d.get("grunn") == "epost_endret" else "")])],
    "enhetsoppslag_utilgjengelig": lambda d, o, h: [(
        "Firmaopplysningene kunne ikke hentes fra Enhetsregisteret ved påmeldingen",
        ["Organisasjonsnummeret er lagret slik det ble oppgitt. Firmaopplysninger må kontrolleres."])],
    "firmaopplysninger_hentet": lambda d, o, h: [("Firmaopplysninger hentet fra Enhetsregisteret", [])],
    "kursbevis_mal_feil": lambda d, o, h: [("Kursbevis ble ikke laget – feil i e-postmalen for kursbevis", [])],
}
