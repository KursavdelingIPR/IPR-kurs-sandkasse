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

Unntak: EKSTRADELTAKER (ekstradeltaker_ts satt). Daglig jobb hopper alltid over en ekstradeltaker (ingen automatisk bekreftelse
eller faktura: prisen er ikke standardprisen), saa "daglig jobb henter den inn" stemmer ikke for dem. Flagget nullstilles derfor
BARE naar behandlingen er fullfort; ved feil eller uavklart utfall staar holdet igjen, slik at "Send bekreftelse nå" kan proves
paa nytt fra deltakervinduet. Ellers ville en ekstradeltaker staatt igjen uten hold og uten at noe kunne sende bekreftelsen.
"""
from dataclasses import dataclass, replace

from . import db, privatadresse, rabatter, sveiper
from .kjoring import Kjoring

# Resultatkoder fra behandle_holdt_paamelding()/klassifiser()
FULLFORT = "fullfort"
UAVKLART = "uavklart"
FEILET = "feilet"
ALLEREDE_BEHANDLET = "allerede_behandlet"
IKKE_BEHANDLINGSBAR = "ikke_behandlingsbar"
# Kun brukt av kallere som selv velger aa ikke starte en rad (bulk-stopp) - aldri returnert herfra
IKKE_FORSOKT = "ikke_forsokt"

# Årsakskode for FULLFORT når bare fakturaen gjenstår fordi privat adresse mangler (se privatadresse.py)
ARSAK_FAKTURA_VENTER = "faktura_venter_adresse"

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
    # True = meldingen (bekreftelse/ventelistebeskjed) til denne e-postadressen og dette kurset var ALLEREDE sendt foer behandlingen
    # startet (f.eks. ved en tidligere paamelding). Motoren dedupliserer da, og ingenting ble sendt naa: ruten maa ikke si at den er sendt.
    epost_var_sendt: bool = False


def _r(kode: str, arsak_kode: str, arsak: str, forsokt: bool = False) -> Behandlingsresultat:
    return Behandlingsresultat(kode, arsak_kode, arsak, forsokt)


def _epost_type(rad) -> str:
    """Hvilken e-posttype selve behandlingen sender (samme som sveiper._en): venteliste -> 'venteliste', ellers
    'bekreftelse'. Brukes til aa avgjore om AKKURAT DENNE behandlingen er uavklart - ikke urelaterte
    meldinger (ukefor, dagfor, kursbevis, avlysning) som deler nokkelen kurs:<id>."""
    return "venteliste" if rad["status"] == "venteliste" else "bekreftelse"


def klassifiser(con, kurs, rad) -> Behandlingsresultat | None:
    """Kan denne raden behandles NA? None = ja. Ellers resultatet (ren lesing - ingen skriving).

    `rad` maa ha: status, sveiper_kjort, sveiper_utsatt og epost (og ekstradeltaker_ts, hvis raden kan vaere en ekstradeltaker).
    Brukes baade av forhandsvisning, bulk-behandling og fase 9-ruten, slik at de aldri kan vurdere en rad ulikt."""
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
    # En ekstradeltaker som ikke er behandlet, er alltid "holdt tilbake": daglig jobb tar den aldri, og bare admin kan sende. (Holdet
    # staar normalt i sveiper_utsatt; dette er ekstra sikring hvis flagget skulle mangle, slik at knappen aldri blir borte.)
    holdt = rad["sveiper_utsatt"] or db.er_ekstradeltaker(rad)
    if not holdt:
        uavklart = db.uavklart_status_for_paamelding(con, kurs["id"], rad["epost"], rad["id"], _epost_type(rad))
        if uavklart:
            arsak_kode = "tidligere_uavklart" if uavklart == "ukjent" else "pagar"
            return _r(UAVKLART, arsak_kode, _UAVKLART_TEKST[arsak_kode])
        return _r(IKKE_BEHANDLINGSBAR, "ikke_holdt", "Ikke holdt tilbake - behandles automatisk som normalt")
    return None


def forventet_handling(kurs, rad, con=None) -> str:
    """Hva behandlingen VIL gjore for en behandlingsbar rad (kun tekst til forhandsvisningen). Gis `con`, sier teksten ogsaa fra naar
    bekreftelsen til denne e-postadressen alt er sendt (en tidligere paamelding): motoren dedupliserer da, og ingen ny e-post gaar ut."""
    if rad["status"] == "venteliste":
        return "Ventelistebeskjed på e-post"
    sendt_fra_for = con is not None and db.allerede_sendt(con, f"kurs:{kurs['id']}", rad["epost"], "bekreftelse")
    if db.er_ekstradeltaker(rad):      # ekstradeltaker: bekreftelse med bare deres samlinger, ingen faktura (ikke standardpris)
        ingen_faktura = f"ingen faktura for {db.PAAMELDINGSSTATUSER_FLERTALL['ekstradeltaker'].lower()}"
        if sendt_fra_for:
            return f"Ingen ny e-post (bekreftelsen er sendt fra før), {ingen_faktura}"
        return f"Bekreftelse på e-post ({ingen_faktura})"
    kunde = {"fakturering": kurs["fakturering"], "pris_nok": kurs["pris_nok"], "status": rad["status"]}
    faktureres = sveiper.skal_faktureres(kunde)
    if sendt_fra_for:
        tekst = ("Fakturering (bekreftelsen er sendt fra før og sendes ikke på nytt)" if faktureres
                 else "Ingenting å sende (bekreftelsen er sendt fra før)")
    else:
        tekst = "Bekreftelse på e-post og fakturering" if faktureres else "Bekreftelse på e-post"
    # En privat faktura lages aldri uten komplett privat adresse (privatadresse.py): si fra allerede i forhåndsvisningen
    venter = faktureres and con is not None and "id" in rad.keys() and privatadresse.venter(con, rad["id"])
    # ... og ingen faktura før rabatten er godkjent (kurs/rabatter.py)
    rabatt = faktureres and rabatter.venter(rad)
    return (tekst + (" – fakturaen holdes tilbake fordi privat adresse mangler" if venter else "")
            + (" – fakturaen venter til rabatten er godkjent" if rabatt else ""))


def teller_som_feil(res: Behandlingsresultat) -> bool:
    """Skal dette resultatet telle mot bulk-stoppregelen? Kun et faktisk forsok som endte uavklart
    ('ukjent') eller feilet. 'pagar' hos annen, allerede behandlet og ikke behandlingsbar teller IKKE."""
    return res.forsokt and (res.kode == FEILET or (res.kode == UAVKLART and res.arsak_kode == "ukjent"))


def _utfall(k: Kjoring, kurs_id: int, paamelding_id: int, rad) -> Behandlingsresultat:
    """Tolker utfallet av ETT gjennomloep av motoren fra TILSTAND (motoren returnerer ingenting). Ren lesing."""
    con = k.con
    if rad["status"] == "bekreftet":
        # Flagget, ELLER at sideeffektene beviselig er utfoert: en samtidig behandling (annen admin) kan ha sendt og
        # fakturert uten aa ha rukket aa committe sveiper_kjort=1 ennaa - det er fullfoert, ikke en teknisk feil.
        fullfort = (con.execute("SELECT sveiper_kjort FROM paamelding WHERE id=?", (paamelding_id,)).fetchone()[0] == 1
                    or sveiper.sideeffekter_ferdige(k, paamelding_id))
    else:  # venteliste: sveiper_kjort settes med vilje ikke - bruk utsendelsesloggen
        fullfort = db.allerede_sendt(con, f"kurs:{kurs_id}", rad["epost"], "venteliste")
    if fullfort:
        return _r(FULLFORT, "fullfort", "Ferdig behandlet", forsokt=True)
    if (rad["status"] == "bekreftet" and db.allerede_sendt(con, f"kurs:{kurs_id}", rad["epost"], "bekreftelse")
            and privatadresse.venter(con, paamelding_id)):
        # Bekreftelsen er sendt, men en privat faktura lages ikke uten komplett privat adresse (privatadresse.py). Ikke en teknisk
        # feil: holdet oppheves, og daglig jobb (eller lagring av adressen i deltakervinduet) lager fakturaen når adressen er på plass.
        return _r(FULLFORT, ARSAK_FAKTURA_VENTER, "Bekreftelsen er sendt. Fakturaen venter på privat adresse (adresse, postnummer "
                  "og poststed må fylles inn først).", forsokt=True)
    uavklart = db.uavklart_status_for_paamelding(con, kurs_id, rad["epost"], paamelding_id, _epost_type(rad))
    if uavklart:
        return _r(UAVKLART, uavklart, _UAVKLART_TEKST[uavklart], forsokt=True)
    # Ikke fullfort og ingenting uavklart: enten en teknisk feil foer noe ble reservert, eller raden
    # ble avmeldt/kurset avlyst mens vi holdt paa (motorens SQL-filter hoppet da over den).
    ny_kurs = con.execute(_SQL_KURS, (kurs_id,)).fetchone()
    ny_rad = con.execute(_SQL_RAD, (paamelding_id, kurs_id)).fetchone()
    endret = klassifiser(con, ny_kurs, ny_rad) if ny_kurs and ny_rad else None
    if endret and endret.kode == IKKE_BEHANDLINGSBAR:
        return endret
    if db.er_ekstradeltaker(rad):      # daglig jobb hopper over ekstradeltakere: holdet staar igjen, og admin proever paa nytt selv
        return _r(FEILET, "teknisk", "Teknisk feil - ikke fullført. Holdet står igjen: prøv igjen fra deltakervinduet.", forsokt=True)
    return _r(FEILET, "teknisk", "Teknisk feil - ikke fullført. Daglig jobb prøver igjen automatisk.", forsokt=True)


_SQL_KURS = "SELECT * FROM kurs WHERE id=?"
_SQL_RAD = """SELECT p.id, p.status, p.sveiper_kjort, p.sveiper_utsatt, p.ekstradeltaker_ts, d.epost
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

    # Var meldingen allerede sendt FOER vi startet (f.eks. en tidligere paamelding med samme e-post)? Da dedupliserer motoren og
    # sender ingenting, og kalleren maa ikke si at den er sendt naa.
    var_sendt = db.allerede_sendt(con, f"kurs:{kurs_id}", rad["epost"], _epost_type(rad))
    k = Kjoring(con, idag=idag, aktor=aktor)
    sveiper.kjor(k, paamelding_id, ignorer_utsatt=True)
    con.commit()
    resultat = _utfall(k, kurs_id, paamelding_id, rad)
    if resultat.kode == FEILET and rad["status"] == "bekreftet" and db.allerede_sendt(
            con, f"kurs:{kurs_id}", rad["epost"], "bekreftelse"):
        # Bekreftelsen ER sendt (av en samtidig behandling som tapte oss claimen, og som naa staar mellom e-post og
        # fakturaclaim), men fakturadelen er verken ferdig eller reservert. Det er ikke en teknisk feil: motoren er
        # idempotent (claims), saa ett nytt gjennomloep fullfoerer fakturadelen selv eller taper claimen til den andre.
        sveiper.fullfor_en(k, paamelding_id)
        con.commit()
        resultat = _utfall(k, kurs_id, paamelding_id, rad)
    if resultat.kode == IKKE_BEHANDLINGSBAR:
        return resultat  # endret underveis (avmeldt/avlyst) - flagget rores ikke
    resultat = replace(resultat, epost_var_sendt=var_sendt)

    # Betinget UPDATE: kun den som faktisk fjerner flagget skriver. Skjer ETTER behandlingen, aldri foer.
    if db.er_ekstradeltaker(rad):
        # Ekstradeltaker: daglig jobb tar dem aldri, saa holdet oppheves BARE naar bekreftelsen er fullfoert (og da er raden ferdig:
        # sveiper_kjort=1, som etter en vanlig behandling). Feilet eller uavklart: holdet staar igjen, og admin kan proeve paa nytt.
        if resultat.kode == FULLFORT:
            con.execute("UPDATE paamelding SET sveiper_utsatt=0, sveiper_kjort=1 WHERE id=? AND sveiper_utsatt=1", (paamelding_id,))
    else:
        con.execute("UPDATE paamelding SET sveiper_utsatt=0 WHERE id=? AND sveiper_utsatt=1", (paamelding_id,))
    detaljer = {"paamelding_id": paamelding_id, "kurs_id": kurs_id, "resultat": resultat.kode, "via": via}
    if bulk_id:
        detaljer["bulk_id"] = bulk_id
    db.logg(con, "manuell_behandling_utlost", detaljer, aktor=aktor)  # ingen navn/e-post
    con.commit()
    return resultat
