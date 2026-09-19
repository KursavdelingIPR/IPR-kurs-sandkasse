"""Behandling av EN eksplisitt holdt paamelding - felles for fase 9 (enkelt) og fase 11 (bulk).

Dette er et TYNT orkestreringslag over den eksisterende, hardenede motoren. Det har INGEN egen
e-postmotor, fakturamotor, Graph- eller Visma-logikk, og skriver aldri til utsending_logg/faktura selv:
selve sendingen og faktureringen gjores av sveiper.kjor()/Kjoring.send_en_gang(), med claim-/status-
modellen (utsending_logg.status, faktura_forsok) som eneste beskyttelse mot doble sideeffekter.

Det dette laget GJOR:
  1. Fersk revalidering rett foer behandling (aldri stol paa noe fra browseren).
  2. Kaller sveiper.kjor(k, pid, ignorer_utsatt=True) - flagget staar igjen (=1) mens behandlingen pagar, saa
     den globale sveiperen aldri kan overta raden.
  3. Tolker utfallet fra TILSTAND (sveiper.kjor() svelger unntak og returnerer ingenting) til ETT resultat.
  4. Nullstiller sveiper_utsatt ETTER behandling (betinget UPDATE) - men ikke naar raden ikke var behandlingsbar.
  5. Logger en teknisk hendelse uten persondata.

Sluttsemantikk for sveiper_utsatt (samme for enkelt og bulk):
  fullfort            -> flagg 0
  uavklart            -> flagg 0 (reservert/ukjent - proves ALDRI automatisk paa nytt; claimen hindrer effekt)
  feilet              -> flagg 0 (ingenting reservert - daglig jobb henter den inn)
  allerede_behandlet / ikke_behandlingsbar -> flagg URORT
"""
from dataclasses import dataclass

from . import db, sveiper
from .kjoring import Kjoring

# Resultatkoder fra behandle_holdt_paamelding()/klassifiser()
FULLFORT = "fullfort"
UAVKLART = "uavklart"
FEILET = "feilet"
ALLEREDE_BEHANDLET = "allerede_behandlet"
IKKE_BEHANDLINGSBAR = "ikke_behandlingsbar"
# Kun brukt av kallere som selv velger aa ikke starte en rad (bulk-stopp) - aldri returnert herfra
IKKE_FORSOKT = "ikke_forsokt"

# Visningsnavn (rolig norsk, ingen tekniske ord)
KATEGORI_NAVN = {
    FULLFORT: "Fullført",
    UAVKLART: "Uavklart – krever kontroll",
    FEILET: "Teknisk feil – ikke fullført",
    IKKE_FORSOKT: "Ikke forsøkt (stoppet)",
    ALLEREDE_BEHANDLET: "Allerede behandlet",
    IKKE_BEHANDLINGSBAR: "Ikke lenger behandlingsbar",
}

_UAVKLART_TEKST = {
    "ukjent": "Utfallet er ukjent – e-post eller faktura kan ha gått ut. Kontroller manuelt; det sendes eller "
              "faktureres ikke på nytt automatisk.",
    "pagar": "En annen behandling holder på med denne påmeldingen akkurat nå. Sjekk resultatet om litt.",
    "tidligere_uavklart": "Uavklart fra tidligere forsøk – krever manuell kontroll.",
}


@dataclass(frozen=True)
class Behandlingsresultat:
    kode: str        # fullfort | uavklart | feilet | allerede_behandlet | ikke_behandlingsbar
    arsak_kode: str  # maskinlesbar: se klassifiser() og behandle_holdt_paamelding()
    arsak: str       # kort, personvernsikker tekst som kan vises til admin
    forsokt: bool = False  # True = motoren ble faktisk kalt for denne raden (ikke avvist foer forsok)


def _r(kode: str, arsak_kode: str, arsak: str, forsokt: bool = False) -> Behandlingsresultat:
    return Behandlingsresultat(kode, arsak_kode, arsak, forsokt)


def _epost_type(rad) -> str:
    """Hvilken e-posttype selve behandlingen sender (samme som sveiper._en): venteliste -> 'venteliste', ellers
    'bekreftelse'. Brukes til aa avgjore om AKKURAT DENNE behandlingen er uavklart - ikke urelaterte
    meldinger (ukefor, dagfor, kursbevis, avlysning) som deler nokkelen kurs:<id>."""
    return "venteliste" if rad["status"] == "venteliste" else "bekreftelse"


def klassifiser(con, kurs, rad) -> Behandlingsresultat | None:
    """Kan denne raden behandles NA? None = ja. Ellers resultatet (ren lesing - ingen skriving).

    `rad` maa ha: status, sveiper_kjort, sveiper_utsatt og epost. Brukes baade av forhandsvisning, bulk-
    behandling og fase 9-ruten, slik at de aldri kan vurdere en rad ulikt."""
    if kurs["status"] in ("avlyst", "utkast"):
        return _r(IKKE_BEHANDLINGSBAR, f"kurs_{kurs['status']}",
                  f"Kurset har status «{kurs['status']}» og kan ikke behandles")
    if rad["status"] == "avmeldt":
        return _r(IKKE_BEHANDLINGSBAR, "avmeldt", "Avmeldt - ingenting å behandle")
    if rad["status"] not in ("bekreftet", "venteliste"):
        return _r(IKKE_BEHANDLINGSBAR, "uventet_status", f"Uventet status «{rad['status']}»")
    if rad["sveiper_kjort"]:
        return _r(ALLEREDE_BEHANDLET, "allerede_behandlet", "Allerede behandlet")
    # venteliste faar aldri sveiper_kjort=1 (den "kjores paa nytt naar de flyttes opp") - se utsendelsesloggen
    if rad["status"] == "venteliste" and db.allerede_sendt(con, f"kurs:{kurs['id']}", rad["epost"], "venteliste"):
        return _r(ALLEREDE_BEHANDLET, "allerede_behandlet", "Allerede behandlet")
    if not rad["sveiper_utsatt"]:
        uavklart = db.uavklart_status_for_paamelding(con, kurs["id"], rad["epost"], rad["id"], _epost_type(rad))
        if uavklart:
            arsak_kode = "tidligere_uavklart" if uavklart == "ukjent" else "pagar"
            return _r(UAVKLART, arsak_kode, _UAVKLART_TEKST[arsak_kode])
        return _r(IKKE_BEHANDLINGSBAR, "ikke_holdt", "Ikke holdt tilbake - behandles automatisk som normalt")
    return None


def forventet_handling(kurs, rad) -> str:
    """Hva behandlingen VIL gjore for en behandlingsbar rad (kun tekst til forhandsvisningen)."""
    if rad["status"] == "venteliste":
        return "Ventelistebeskjed på e-post"
    kunde = {"fakturering": kurs["fakturering"], "pris_nok": kurs["pris_nok"], "status": rad["status"]}
    return "Bekreftelse på e-post og faktura" if sveiper.skal_faktureres(kunde) else "Bekreftelse på e-post"


def teller_som_feil(res: Behandlingsresultat) -> bool:
    """Skal dette resultatet telle mot bulk-stoppregelen? Kun et faktisk forsok som endte uavklart
    ('ukjent') eller feilet. 'pagar' hos annen, allerede behandlet og ikke behandlingsbar teller IKKE."""
    return res.forsokt and (res.kode == FEILET or (res.kode == UAVKLART and res.arsak_kode == "ukjent"))


_SQL_KURS = "SELECT * FROM kurs WHERE id=?"
_SQL_RAD = """SELECT p.id, p.status, p.sveiper_kjort, p.sveiper_utsatt, d.epost
              FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE p.id=? AND p.kurs_id=?"""


def behandle_holdt_paamelding(con, kurs_id: int, paamelding_id: int, idag, *, aktor: str = "system",
                              via: str = "enkelt", bulk_id: str | None = None) -> Behandlingsresultat:
    """Behandler EN eksplisitt holdt paamelding og returnerer ETT tydelig resultat.

    Ingen transaksjon holdes aapen ved retur, og ingen holdes over eksterne kall (motorens claim-/resultat-
    commits gjelder). Unntak fra motoren fanges av sveiper.kjor() selv; databasefeil i selve orkestreringen
    (f.eks. laast database) boblet opp til kalleren, som maa behandle dem som 'feilet'."""
    kurs = con.execute(_SQL_KURS, (kurs_id,)).fetchone()
    rad = con.execute(_SQL_RAD, (paamelding_id, kurs_id)).fetchone()
    if not kurs or not rad:  # hører ikke til dette kurset (eller finnes ikke) - ingen detaljer avsloeres
        return _r(IKKE_BEHANDLINGSBAR, "finnes_ikke", "Finnes ikke lenger i dette kurset")
    avvist = klassifiser(con, kurs, rad)
    if avvist:
        return avvist  # ingenting er rort: hverken flagg, motor eller logg

    sveiper.kjor(Kjoring(con, idag=idag), paamelding_id, ignorer_utsatt=True)
    con.commit()

    if rad["status"] == "bekreftet":
        fullfort = con.execute("SELECT sveiper_kjort FROM paamelding WHERE id=?", (paamelding_id,)).fetchone()[0] == 1
    else:  # venteliste: sveiper_kjort settes med vilje ikke - bruk utsendelsesloggen
        fullfort = db.allerede_sendt(con, f"kurs:{kurs_id}", rad["epost"], "venteliste")

    if fullfort:
        resultat = _r(FULLFORT, "fullfort", "Ferdig behandlet", forsokt=True)
    else:
        uavklart = db.uavklart_status_for_paamelding(con, kurs_id, rad["epost"], paamelding_id, _epost_type(rad))
        if uavklart:
            resultat = _r(UAVKLART, uavklart, _UAVKLART_TEKST[uavklart], forsokt=True)
        else:
            # Ikke fullfort og ingenting uavklart: enten en teknisk feil foer noe ble reservert, eller raden
            # ble avmeldt/kurset avlyst mens vi holdt paa (motorens SQL-filter hoppet da over den).
            ny_kurs = con.execute(_SQL_KURS, (kurs_id,)).fetchone()
            ny_rad = con.execute(_SQL_RAD, (paamelding_id, kurs_id)).fetchone()
            endret = klassifiser(con, ny_kurs, ny_rad) if ny_kurs and ny_rad else None
            if endret and endret.kode == IKKE_BEHANDLINGSBAR:
                return endret  # endret underveis - flagget rores ikke
            resultat = _r(FEILET, "teknisk", "Teknisk feil - ikke fullført. Daglig jobb prøver igjen automatisk.",
                          forsokt=True)

    # Betinget UPDATE: kun den som faktisk fjerner flagget skriver. Skjer ETTER behandlingen, aldri foer.
    con.execute("UPDATE paamelding SET sveiper_utsatt=0 WHERE id=? AND sveiper_utsatt=1", (paamelding_id,))
    detaljer = {"paamelding_id": paamelding_id, "kurs_id": kurs_id, "resultat": resultat.kode, "via": via}
    if bulk_id:
        detaljer["bulk_id"] = bulk_id
    db.logg(con, "manuell_behandling_utlost", detaljer, aktor=aktor)  # ingen navn/e-post
    con.commit()
    return resultat
