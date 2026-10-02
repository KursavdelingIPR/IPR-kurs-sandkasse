"""Firmaopplysninger (firmanavn og fakturaadresse) når firma betaler: ÉN felles regel for alle måter å melde på –
det offentlige skjemaet, adminregistrering, bedriftspåmelding, webhook fra nettsiden og filimport.

Regelen (brukerens beslutning 29.09.2026):
  * Firmanavn og fakturaadresse kommer ALLTID fra Enhetsregisteret (kurs/integrasjoner/brreg.py) – aldri fra den som melder
    på (deltaker, kontaktperson, nettsiden, filen eller administrator). Alle veier bygger firmafeltene med
    `paamelding_felter(slaa_opp(...))`; ingen andre steder skrives firmanavn eller adresse for firma.
  * Fakturaen lages ikke før organisasjonsnummeret er gyldig og firmaopplysningene er hentet (`mangler`, sperren i
    sveiper._opprett). Fakturaplanen er ellers uendret: når opplysningene er på plass, går fakturaen videre som vanlig.
  * Kan oppslaget ikke gjøres med en gang (registeret svarer ikke), beholdes påmeldingen. Den er merket «Firmaopplysninger
    må kontrolleres», og fakturaen venter. Skjemaer en person fyller ut (offentlig, admin, bedrift) avviser et nummer som
    er ugyldig eller ikke finnes; webhook og filimport beholder også slike påmeldinger, merket for oppfølging.
  * Nytt oppslag: `prov_igjen` (én påmelding) og `prov_alle` (alle, eventuelt for ett kurs). Administrator og morgenjobben
    (steg 0, før fakturaene) bruker dem. Kalleren committer.
  * Hendelsene loggføres uten organisasjonsnummer, navn eller adresse: bare påmeldingens id.

Merket er ikke en egen kolonne, men følger av dataene (`mangler`): firma betaler, og firmanavnet er tomt eller
organisasjonsnummeret er ugyldig. Det forsvinner av seg selv når opplysningene er hentet. Firmanavnet settes bare fra
registeret, så et navn som står der, er et navn registeret har gitt (eldre påmeldinger fra før regelen kan ha et navn
noen skrev selv; de holdes ikke tilbake).
"""
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from . import db
from .integrasjoner import brreg

MERKE = "Firmaopplysninger må kontrolleres"

HENTET, IKKE_FUNNET, UTILGJENGELIG, UGYLDIG_NUMMER, IKKE_AKTUELL = (
    "hentet", "ikke_funnet", "utilgjengelig", "ugyldig_nummer", "ikke_aktuell")
FUNNET = "funnet"                  # utfallet av slaa_opp når registeret har svart med en enhet
MAKS_NR_TEKST = 30                 # lengste tekst som lagres som organisasjonsnummer når det ikke er et gyldig tall

# Uten alias: brukes både i SELECT (med join) og i UPDATE. Kolonnene finnes bare i paamelding.
SQL_MANGLER = "betaler='organisasjon' AND (org_navn IS NULL OR TRIM(org_navn)='')"
# Aktive påmeldinger på kurs som ikke er avlyst - de andre trenger ingen kontroll
_SQL_AKTIVE = "p.status IN ('bekreftet','venteliste') AND k.status!='avlyst'"

# Tekster som alle veiene deler
TEKST_UGYLDIG = "Skriv et gyldig organisasjonsnummer (9 siffer)"
TEKST_IKKE_FUNNET = "Fant ikke organisasjonsnummeret i Brønnøysundregistrene. Kontroller nummeret."
TEKST_IKKE_HENTET = ("Firmaopplysningene kunne ikke hentes akkurat nå. Påmeldingen er registrert og merket "
                     f"«{MERKE}», og fakturaen venter til opplysningene er hentet.")


def nummer_gyldig(tekst) -> bool:
    """Gyldig organisasjonsnummer (9 siffer, riktig kontrollsiffer)? Ingen nettverkskall."""
    return brreg.gyldig_orgnr(brreg.normaliser_orgnr(tekst))


def mangler(p) -> bool:
    """Firma betaler, men firmaopplysningene er ikke på plass: firmanavnet mangler, eller organisasjonsnummeret er ugyldig.
    Dette er både merket for administrator og sperren for fakturaen. `p` er en påmeldingsrad (eller dict) med betaler,
    org_navn og org_nr."""
    if p["betaler"] != "organisasjon":
        return False
    return not (p["org_navn"] or "").strip() or not nummer_gyldig(p["org_nr"])


@dataclass(frozen=True)
class Oppslag:
    """Utfallet av ett oppslag for en ny eller endret påmelding. `orgnr` er sifrene når nummeret er gyldig, ellers
    teksten som ble skrevet (avkortet). `enhet` er bare satt når registeret har svart (utfall FUNNET)."""
    orgnr: str
    utfall: str                    # FUNNET | IKKE_FUNNET | UGYLDIG_NUMMER | UTILGJENGELIG
    enhet: "brreg.Enhet | None" = None


def slaa_opp(orgnr_tekst) -> Oppslag:
    """Slår opp organisasjonsnummeret i Enhetsregisteret. Ugyldig nummer gir ikke noe nettverkskall. Kaster aldri."""
    tekst = orgnr_tekst.strip() if isinstance(orgnr_tekst, str) else ""
    nr = brreg.normaliser_orgnr(tekst)
    if not brreg.gyldig_orgnr(nr):
        return Oppslag(tekst[:MAKS_NR_TEKST], UGYLDIG_NUMMER)
    try:
        enhet = brreg.hent(nr)
    except brreg.Utilgjengelig:
        return Oppslag(nr, UTILGJENGELIG)
    return Oppslag(nr, FUNNET, enhet) if enhet else Oppslag(nr, IKKE_FUNNET)


def ikke_slaatt_opp(orgnr_tekst) -> Oppslag:
    """For veier som ikke skal røre nettverket når påmeldingen lagres (filimport og forhåndsvisningen av den): bare
    organisasjonsnummeret lagres, og firmanavn og adresse hentes rett etterpå med prov_alle eller prov_igjen."""
    tekst = orgnr_tekst.strip() if isinstance(orgnr_tekst, str) else ""
    nr = brreg.normaliser_orgnr(tekst)
    return Oppslag(nr if brreg.gyldig_orgnr(nr) else tekst[:MAKS_NR_TEKST], UTILGJENGELIG)


def paamelding_felter(o: Oppslag) -> dict:
    """De ENESTE firmafeltene en påmelding får: organisasjonsnummeret og, bare når registeret har svart, firmanavn og
    adresse. Alt den som melder på har skrevet om firmanavn og adresse, forkastes – og felt uten svar tømmes, så en
    gjenåpnet eller endret påmelding aldri beholder opplysninger om et annet firma."""
    e = o.enhet
    return {"org_nr": (e.orgnr if e else o.orgnr) or None, "org_navn": e.navn if e else None,
            "faktura_adresse": (e.adresse or None) if e else None, "faktura_postnr": (e.postnr or None) if e else None,
            "faktura_sted": (e.poststed or None) if e else None}


def avvisning(o: Oppslag, *, med_soek: bool = False) -> str | None:
    """Teksten et skjema som en person fyller ut, viser når nummeret ikke kan brukes (ugyldig eller ikke funnet).
    None når registeret har svart, og også når det ikke svarer: da fortsetter påmeldingen, merket for kontroll."""
    if o.utfall == UGYLDIG_NUMMER:
        return TEKST_UGYLDIG + (", eller søk opp firmaet og velg det i listen." if med_soek else ".")
    if o.utfall == IKKE_FUNNET:
        return TEKST_IKKE_FUNNET
    return None


def logg_ikke_hentet(con, kurs_id: int, paamelding_id: int, aktor: str = "system") -> None:
    """Registeret svarte ikke da påmeldingen ble registrert: samme hendelse for alle veiene. Uten organisasjonsnummer,
    navn og adresse i loggen - bare hvilken påmelding."""
    db.logg(con, "enhetsoppslag_utilgjengelig", {"kurs_id": kurs_id, "paamelding_id": paamelding_id}, aktor=aktor)


def liste(con, kurs_id: int | None = None) -> list:
    """Aktive påmeldinger der firmaopplysningene må kontrolleres (eldste først), eventuelt bare for ett kurs."""
    rader = con.execute(
        f"""SELECT p.id, p.kurs_id, p.betaler, p.org_nr, p.org_navn, k.kursnr, k.kode, k.navn AS kursnavn, d.navn
            FROM paamelding p JOIN kurs k ON k.id=p.kurs_id JOIN deltaker d ON d.id=p.deltaker_id
            WHERE p.betaler='organisasjon' AND {_SQL_AKTIVE}""" + (" AND p.kurs_id=?" if kurs_id else "") + " ORDER BY p.id",
        (kurs_id,) if kurs_id else ()).fetchall()
    return [r for r in rader if mangler(r)]


def prov_igjen(con, paamelding_id: int, aktor: str, *, forste_forsok: bool = False) -> str:
    """Nytt oppslag i Enhetsregisteret for én påmelding der firmaopplysningene mangler. Returnerer HENTET, IKKE_FUNNET,
    UTILGJENGELIG (registeret svarer ikke), UGYLDIG_NUMMER (organisasjonsnummeret mangler eller er ugyldig) eller
    IKKE_AKTUELL (opplysningene er allerede på plass). Firmanavn og adresse fra registeret erstatter aldri noe en
    administrator har skrevet: adressen fylles bare inn der den er tom, og oppdateringen skjer bare hvis firmanavnet
    fortsatt mangler (en samtidig endring vinner). `forste_forsok`: oppslaget er det første for denne påmeldingen (rett
    etter filimport), ikke et nytt forsøk - i demo har det betydning for nummeret der registeret «svarer ikke»."""
    p = con.execute("SELECT id, betaler, org_nr, org_navn FROM paamelding WHERE id=?", (paamelding_id,)).fetchone()
    if p is None or not mangler(p):
        return IKKE_AKTUELL
    nr = brreg.normaliser_orgnr(p["org_nr"])
    if not brreg.gyldig_orgnr(nr):
        return UGYLDIG_NUMMER
    try:
        enhet = (brreg.hent if forste_forsok else brreg.hent_paa_nytt)(nr)
    except brreg.Utilgjengelig:
        return UTILGJENGELIG
    if enhet is None:
        return IKKE_FUNNET
    na = datetime.now().isoformat(timespec="seconds")
    oppdatert = con.execute(
        f"""UPDATE paamelding SET org_nr=?, org_navn=?,
                   faktura_adresse=COALESCE(NULLIF(TRIM(faktura_adresse), ''), ?),
                   faktura_postnr=COALESCE(NULLIF(TRIM(faktura_postnr), ''), ?),
                   faktura_sted=COALESCE(NULLIF(TRIM(faktura_sted), ''), ?), oppdatert=?
            WHERE id=? AND {SQL_MANGLER}""",
        (enhet.orgnr, enhet.navn, enhet.adresse or None, enhet.postnr or None, enhet.poststed or None, na,
         paamelding_id)).rowcount
    if not oppdatert:
        return IKKE_AKTUELL
    db.logg(con, "firmaopplysninger_hentet", {"paamelding_id": paamelding_id}, aktor=aktor)
    return HENTET


def prov_alle(con, aktor: str, kurs_id: int | None = None, *, forste_forsok: bool = False, ved_hentet=None) -> Counter:
    """prov_igjen for alle aktive påmeldinger som mangler firmaopplysninger (eventuelt bare ett kurs). Svarer ikke
    registeret, prøves ikke resten (de telles som UTILGJENGELIG). `ved_hentet(paamelding_id)` kalles for hver påmelding
    som fikk opplysningene (administrator lar den gå videre etter fakturaplanen). Returnerer antall per utfall."""
    rader = liste(con, kurs_id)
    utfall = Counter()
    for i, r in enumerate(rader):
        u = prov_igjen(con, r["id"], aktor, forste_forsok=forste_forsok)
        utfall[u] += 1
        if u == HENTET and ved_hentet:
            ved_hentet(r["id"])
        if u == UTILGJENGELIG:
            utfall[UTILGJENGELIG] += len(rader) - i - 1
            break
    return utfall
