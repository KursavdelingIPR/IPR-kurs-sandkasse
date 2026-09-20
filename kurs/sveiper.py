"""Det som skjer automatisk naar noen melder seg paa (spec del B: 'automatiske sveiper').

Kjores umiddelbart fra nettskjemaet OG av den daglige jobben (fanger opp det som feilet).
Hver deltaker behandles for seg: feiler Visma for én, stopper det ikke de andre.

Fakturering styres av kurs.betaling / paamelding.betaling:
  samlet      -> én faktura paa hele belopet ved paamelding
  per_samling -> én faktura pr kursdag, opprettet `kurs.faktura_dager_for` dager for hver samling
"""
import calendar
from dataclasses import dataclass
from datetime import date

from . import db
from .feil import sikker_feiltekst
from .integrasjoner import visma
from .kjoring import Kjoring

# Staleness-grensen for 'reservert' (db.UAVKLART_GRENSE_MIN) deles med forsidens teller - aldri automatisk
# retry uansett alder, se schema.sql/trinn 2.5-designet.

SQL_DELTAKER = """SELECT p.*, d.navn, d.epost, d.id AS did, k.navn AS kursnavn, k.kode, k.pris_nok, k.fakturering,
                         k.visma_artikkel, k.type AS kurstype, k.faktura_dager_for
                  FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id JOIN kurs k ON k.id=p.kurs_id"""


def kjor(k: Kjoring, paamelding_id: int | None = None, *, ignorer_utsatt: bool = False) -> None:
    """Behandler paameldinger som ikke er ferdig ("ved paamelding"-kjeden).

    Global (paamelding_id=None): hopper over sveiper_utsatt=1 - admin har bevisst bedt om aa VENTE.
    Malrettet (paamelding_id=X): behandler kun X. Med ignorer_utsatt=True behandles X ogsaa om den er
    holdt tilbake (sveiper_utsatt=1) - det er slik admin-"Behandle" utloeser en holdt paamelding UTEN aa
    maatte nullstille flagget foerst (det aapnet et tidsvindu der den globale sveiperen kunne overta).
    Flagget nullstilles av kalleren ETTER behandlingen. Uten ignorer_utsatt respekteres flagget ogsaa
    malrettet (trygg standard for alle andre kallere). ignorer_utsatt uten paamelding_id er ikke tillatt.

    Beskyttelsen mot doble sideeffekter er claim/status-modellen (Kjoring.send_en_gang, _opprett) - ikke flagget.
    """
    if ignorer_utsatt and not paamelding_id:
        raise ValueError("ignorer_utsatt krever en bestemt paamelding_id")
    utsatt_vilkar = "" if ignorer_utsatt else " AND p.sveiper_utsatt=0"
    # avlyst: skal aldri faa e-post/faktura. utkast: ikke publisert ennaa, skal aldri behandles
    # automatisk (fase 9 - admin kan registrere med tillat_utkast, men det skal ikke sendes noe
    # for kurset er aapnet). sveiper_utsatt=1: admin har bevisst bedt om AA VENTE (fase 9) - skal
    # ikke plukkes opp automatisk uansett kursstatus.
    #
    # "avsluttet" er BEVISST IKKE utelatt her, av samme grunn som i forfalte_delfakturaer(): en
    # rad med sveiper_kjort=0 kan skyldes at forrige forsok feilet (Visma nede, e-postfeil,
    # driftsstans) mens kurset fortsatt var aapen/aktiv - kurset kan naa rekke aa bli avsluttet
    # (siste kursdag passert) FOR feilen er rettet. "avsluttet" betyr bare at kursdatoene er
    # passert, ikke at all behandling er ferdig. Utkast kan derimot IKKE naa "avsluttet" uten aa
    # ha vaert aapen forst (daglig.kjor() sin statusovergang gjelder bare aapen/full/aktiv-kurs),
    # saa et rent utkast-kurs har aldri en legitim "recovery"-rad aa ta igjen paa denne maaten.
    rader = k.con.execute(
        SQL_DELTAKER + f""" WHERE p.sveiper_kjort=0{utsatt_vilkar} AND p.status IN ('bekreftet','venteliste')
                           AND k.status NOT IN ('avlyst','utkast')"""
        + (" AND p.id=?" if paamelding_id else ""),
        (paamelding_id,) if paamelding_id else ()).fetchall()
    for p in rader:
        try:
            _en(k, p)
        except Exception as e:  # noqa: BLE001 – logg og fortsett med neste
            feil = sikker_feiltekst(e)  # aldri str(e): kan inneholde e-post/URL/persondata
            db.logg(k.con, "sveip_feil", {"paamelding_id": p["id"], "feil": feil})
            k.si(f"  FEIL for påmelding {p['id']}: {feil}")


def _en(k: Kjoring, p) -> None:
    nokkel = f"kurs:{p['kurs_id']}"
    kurs = k.con.execute("SELECT * FROM kurs WHERE id=?", (p["kurs_id"],)).fetchone()
    dager = db.kursdager(k.con, p["kurs_id"])

    if p["status"] == "venteliste":
        k.send_en_gang(nokkel, p["epost"], "venteliste", "venteliste", paamelding_id=p["id"], p=p, kurs=kurs)
        return  # ikke fakturer / ikke merk ferdig – kjores paa nytt naar de flyttes opp

    k.send_en_gang(nokkel, p["epost"], "bekreftelse", "bekreftelse", paamelding_id=p["id"],
                   p=p, kurs=kurs, dager=dager)
    if not db.allerede_sendt(k.con, nokkel, p["epost"], "bekreftelse"):
        return  # bekreftelsen er ikke trygt bekreftet sendt enda (reservert/feilet/ukjent hos noen -
                # kanskje denne kjoringen selv) - ikke fakturer, ikke marker ferdig. Tas igjen senere,
                # siden sveiper_kjort fortsatt er 0.
    plan = _lag_plan(k, p, dager)  # EN plan, sendt videre til fakturer() og _fakturering_ferdig() (aldri regnet flere steder)
    fakturer(k, p, plan)
    if not _fakturering_ferdig(k, p, plan):
        return  # faktura ikke definitivt ferdig (reservert/feilet/ukjent) enna, eller kursdato mangler (konfigurasjonsfeil)
    # PLAN_UTSATT: faktura_tidligst_dato (skrevet i fakturer()) og sveiper_kjort=1 ligger i SAMME uavsluttede transaksjon -
    # ingenting committes mellom dem, saa den ene kan aldri lagres uten den andre.
    k.con.execute("UPDATE paamelding SET sveiper_kjort=1 WHERE id=?", (p["id"],))


def _lag_plan(k: Kjoring, p, dager=None) -> "FakturaPlan":
    """Bygger FakturaPlan for en paamelding fra dens egne felt + den autoritative kursdaglisten (db.kursdager)."""
    dager = db.kursdager(k.con, p["kurs_id"]) if dager is None else dager
    return faktura_plan(fakturering=p["fakturering"], pris_nok=p["pris_nok"], status=p["status"], betaling=p["betaling"],
                        faktura_onskes_na=p["faktura_onskes_na"], kursdager=[d["dato"] for d in dager], idag=k.idag)


def _fakturering_ferdig(k: Kjoring, p, plan: "FakturaPlan") -> bool:
    """Er fakturadelen av "ved paamelding"-kjeden ferdig, gitt planen?

      ingen            -> ferdig
      utsatt           -> ferdig (lovlig ventetilstand: bekreftelse = ferdig, faktura = planlagt senere)
      mangler_kursdag  -> IKKE ferdig (konfigurasjonsfeil - skal ikke skjules)
      na               -> ferdig naar den samlede fakturaen finnes som en ekte, bekreftet faktura-rad (ingen uavklart forsok)
      per_samling      -> hver ENDA FORFALT delfaktura finnes som ekte faktura-rad. Fremtidige (ikke-forfalte)
                          delfakturaer kreves IKKE ferdige - de hentes av forfalte_delfakturaer() naar de forfaller.
    """
    if plan.modus in (PLAN_INGEN, PLAN_UTSATT):
        return True
    if plan.modus == PLAN_MANGLER_KURSDAG:
        return False
    if plan.modus == PLAN_NA:
        return k.con.execute(
            "SELECT 1 FROM faktura WHERE paamelding_id=? AND kursdag_id IS NULL", (p["id"],)).fetchone() is not None
    for dag in db.kursdager(k.con, p["kurs_id"]):
        if _forfalt(k.idag, dag["dato"], p["faktura_dager_for"]) and not k.con.execute(
                "SELECT 1 FROM faktura WHERE paamelding_id=? AND kursdag_id=?", (p["id"], dag["id"])).fetchone():
            return False
    return True


def _faktura_finnes(con, paamelding_id: int, kursdag_id: int | None) -> bool:
    return con.execute("SELECT 1 FROM faktura WHERE paamelding_id=? AND COALESCE(kursdag_id,0)=?",
                       (paamelding_id, kursdag_id or 0)).fetchone() is not None


def skal_faktureres(p) -> bool:
    return p["fakturering"] == "person" and p["pris_nok"] > 0 and p["status"] == "bekreftet"


def delbelop(pris: int, antall: int) -> list[int]:
    """Deler belopet likt. Rest legges paa forste faktura, slik at summen alltid stemmer."""
    grunn, rest = divmod(pris, antall)
    return [grunn + rest] + [grunn] * (antall - 1)


def fakturer(k: Kjoring, p, plan: "FakturaPlan | None" = None) -> None:
    """Gjennomfoerer fakturaplanen for denne deltakeren akkurat naa (planen beregnes her hvis den ikke er gitt).

      ingen           -> ingenting
      mangler_kursdag -> ingen faktura, ingen Visma, ingen faktura_forsok; kun en trygg hendelse (konfigurasjonsfeil)
      utsatt          -> ingen faktura/Visma/faktura_forsok; lagrer faktura_tidligst_dato (ikke committet her)
      na              -> samlet faktura via _opprett (uendret claim/Visma-sikkerhet)
      per_samling     -> uendret: delfaktura for hver forfalt samling via _opprett
    """
    plan = plan or _lag_plan(k, p)
    if plan.modus == PLAN_INGEN:
        return
    if plan.modus == PLAN_MANGLER_KURSDAG:
        db.logg(k.con, "faktura_feil", {"paamelding_id": p["id"], "kurs_id": p["kurs_id"], "arsak": PLAN_MANGLER_KURSDAG})
        k.si(f"  FEIL for påmelding {p['id']}: kurset mangler kursdager - faktura kan ikke planlegges")
        return
    if plan.modus == PLAN_UTSATT:
        k.con.execute("UPDATE paamelding SET faktura_tidligst_dato=? WHERE id=?", (plan.tidligst_dato.isoformat(), p["id"]))
        k.si(f"  faktura utsatt til {plan.tidligst_dato.isoformat()} (påmelding {p['id']})")
        return
    if plan.modus == PLAN_NA:
        _opprett(k, p, None, p["pris_nok"], f"{p['kursnavn']} – {p['navn']}")
        return
    dager = db.kursdager(k.con, p["kurs_id"])
    belop = delbelop(p["pris_nok"], len(dager))
    for nr, (dag, sum_) in enumerate(zip(dager, belop), start=1):
        if _forfalt(k.idag, dag["dato"], p["faktura_dager_for"]):
            _opprett(k, p, dag["id"], sum_,
                     f"{p['kursnavn']} – {p['navn']} – samling {nr} av {len(dager)} ({dag['dato']})")


def _forfalt(idag: date, dato: str, dager_for: int) -> bool:
    return (date.fromisoformat(dato) - idag).days <= dager_for


def seks_maaneder_for(dato: date) -> date:
    """`dato` minus seks KALENDERMAANEDER (ikke 180 dager - seks maaneder er 181-184 dager).

    Finnes ikke samme dagnummer i maalmaaneden, brukes maanedens siste dag:
    31.08.2027 -> 28.02.2027, 31.08.2028 -> 29.02.2028, 29.02.2028 -> 29.08.2027.
    Ren funksjon: leser/skriver ikke databasen og bruker ikke dagens dato.
    """
    aar, maaned = divmod(dato.year * 12 + (dato.month - 1) - 6, 12)
    maaned += 1
    return date(aar, maaned, min(dato.day, calendar.monthrange(aar, maaned)[1]))


# Fakturaplan: en REN beslutning om NAAR en faktura skal opprettes. Brukes via _lag_plan av _en/fakturer/_fakturering_ferdig;
# utsatte samlede fakturaer utloeses av utsatte_fakturaer (daglig steg 1c), og oekonomifeltene laases av
# db.har_okonomisk_binding_for_kurs.
PLAN_INGEN = "ingen"                      # ingen automatisk faktura (gratis, organisasjon/ingen-fakturering, ikke bekreftet)
PLAN_PER_SAMLING = "per_samling"          # delfakturaer med egen forfallslogikk (faktura_dager_for) - seksmaanedersregelen gjelder ikke
PLAN_NA = "na"                            # samlet faktura opprettes naa
PLAN_UTSATT = "utsatt"                    # samlet faktura venter til `tidligst_dato`
PLAN_MANGLER_KURSDAG = "mangler_kursdag"  # samlet faktura ville vaert aktuell, men foerste kursdag finnes ikke: ALDRI faktura


@dataclass(frozen=True)
class FakturaPlan:
    """Resultatet av faktura_plan(). Ingen persondata. `tidligst_dato` er kun satt for modus 'utsatt'."""
    modus: str
    tidligst_dato: date | None = None


def faktura_plan(*, fakturering: str, pris_nok: int, status: str, betaling: str, faktura_onskes_na,
                 kursdager, idag: date) -> FakturaPlan:
    """Ren beslutning om samlet faktura skal opprettes naa, utsettes eller ikke skje automatisk.

    Alt kommer inn som argumenter (ingen DB, ingen dagens dato, ingen sideeffekter). `kursdager` er den autoritative,
    sorterte kursdaglisten (db.kursdager) som ISO-datoer eller date; foerste element er foerste kursdag.
    Det lagrede feltet faktura_tidligst_dato brukes IKKE som input (det er systemets senere, lagrede beslutning).

    Rekkefoelge (viktig - manglende kursdag vinner over faktura_onskes_na):
      1. skal_faktureres()? Ellers 'ingen' (samme fasit som resten av motoren; hvem som betaler spiller ingen rolle)
      2. betaling per_samling -> 'per_samling' (egen forfallslogikk, uberoert av 6-maanedersregelen og faktura_onskes_na)
      3. ingen foerste kursdag -> 'mangler_kursdag' (konfigurasjonsfeil, aldri 'na')
      4. faktura_onskes_na -> 'na' (ogsaa naar kurset er mer enn seks maaneder frem)
      5. idag foer seks_maaneder_for(foerste kursdag) -> 'utsatt' (med tidligst_dato), ellers 'na'
    """
    if not skal_faktureres({"fakturering": fakturering, "pris_nok": pris_nok, "status": status}):
        return FakturaPlan(PLAN_INGEN)
    if betaling == "per_samling":
        return FakturaPlan(PLAN_PER_SAMLING)
    datoer = [d if isinstance(d, date) else date.fromisoformat(d) for d in (kursdager or ())]
    if not datoer:
        return FakturaPlan(PLAN_MANGLER_KURSDAG)
    if faktura_onskes_na:
        return FakturaPlan(PLAN_NA)
    tidligst = seks_maaneder_for(min(datoer))
    if idag < tidligst:
        return FakturaPlan(PLAN_UTSATT, tidligst)
    return FakturaPlan(PLAN_NA)


def forfalte_delfakturaer(k: Kjoring) -> None:
    """Daglig: lager delfakturaer som har blitt forfalt siden sist. Idempotent.

    Statussjekken her er bevisst BREDERE enn i kjor() (kun "!= avlyst", ikke begrenset til
    aapen/full/aktiv): en delfaktura for en samling som allerede har vaert, forblir "forfalt"
    (se _forfalt) inntil den faktisk opprettes - ogsaa etter at kurset er blitt avsluttet. Det
    er den eneste eksisterende gjenopprettingsveien etter f.eks. driftsstans i Visma, siden
    "avsluttet" kun betyr at siste kursdag har passert, ikke at all fakturering er ferdig.
    IKKE stram inn til IN ('aapen','full','aktiv') her - se undersokelsen i fase 9.
    (sveiper_utsatt er ikke relevant her: en tilbakeholdt rad naar aldri sveiper_kjort=1
    i utgangspunktet, siden det kravet haandheves i kjor().)
    """
    for p in k.con.execute(SQL_DELTAKER + """ WHERE p.status='bekreftet' AND p.betaling='per_samling'
                                              AND p.sveiper_kjort=1 AND k.fakturering='person' AND k.pris_nok > 0
                                              AND k.status!='avlyst'"""):
        try:
            fakturer(k, p)
        except Exception as e:  # noqa: BLE001
            feil = sikker_feiltekst(e)
            db.logg(k.con, "faktura_feil", {"paamelding_id": p["id"], "feil": feil})
            k.si(f"  FEIL delfaktura for påmelding {p['id']}: {feil}")


def utsatte_fakturaer(k: Kjoring, paamelding_id: int | None = None) -> None:
    """Daglig: oppretter SAMLEDE fakturaer som lovlig har ventet (PLAN_UTSATT) og naa er kvalifisert. Idempotent.

    Utvalg: bekreftet, sveiper_kjort=1, betaling samlet, faktura_tidligst_dato satt, kurset fakturerer per person med pris,
    kursstatus ikke avlyst/utkast, og ingen samlet faktura finnes. Kvalifisert naar den LAGREDE datoen er naadd
    (faktura_tidligst_dato <= idag) ELLER faktura_onskes_na=1. Den lagrede datoen er beslutningen systemet tok da
    bekreftelsen ble behandlet - seks maaneder regnes IKKE paa nytt her. Datoen beholdes som historikk etter fakturering.

    Selve fakturaen gaar gjennom samme motor som ellers (fakturer -> _opprett: claim, dobbeltsjekk, faktura_forsok,
    Visma, unik indeks, ukjent-semantikk uten automatisk retry). En feil paa en rad stopper ikke de andre.

    `paamelding_id` gir maalrettet utloesning av akkurat en holdt paamelding (seam for et fremtidig admin-"faktura naa").
    Kurs som avlyses mellom SELECT og Visma-kallet er samme eksisterende (ikke-utvidede) race som resten av motoren.
    """
    rader = k.con.execute(
        SQL_DELTAKER + """ WHERE p.status='bekreftet' AND p.sveiper_kjort=1 AND p.betaling='samlet'
                           AND p.faktura_tidligst_dato IS NOT NULL
                           AND (p.faktura_onskes_na=1 OR p.faktura_tidligst_dato <= ?)
                           AND k.fakturering='person' AND k.pris_nok > 0 AND k.status NOT IN ('avlyst','utkast')
                           AND NOT EXISTS (SELECT 1 FROM faktura f WHERE f.paamelding_id=p.id AND f.kursdag_id IS NULL)"""
        + (" AND p.id=?" if paamelding_id else ""),
        (k.idag.isoformat(), *((paamelding_id,) if paamelding_id else ()))).fetchall()
    for p in rader:
        try:
            fakturer(k, p, FakturaPlan(PLAN_NA))
        except Exception as e:  # noqa: BLE001
            feil = sikker_feiltekst(e)  # aldri str(e): kan inneholde e-post/URL/persondata
            db.logg(k.con, "faktura_feil", {"paamelding_id": p["id"], "feil": feil})
            k.si(f"  FEIL utsatt faktura for påmelding {p['id']}: {feil}")


def oppdater_faktura_hold_etter_kursdagendring(con, kurs_id: int) -> int:
    """SKAL kalles i SAMME transaksjon som enhver endring av kursets kursdager (ingen slik editor finnes ennaa - en vakttest
    feiler hvis ny kode skriver til kursdag uten aa gaa via denne). Holder lagrede faktura_tidligst_dato i takt med foerste
    kursdag, slik at en gammel seksmaanedersdato aldri brukes mot et nytt kursoppsett.

    Berorer KUN aktive, ennaa ufakturerte hold: bekreftet, samlet, fakturering=person med pris, faktura_tidligst_dato satt,
    og INGEN ekte faktura og INGEN faktura_forsok (da har ekstern oekonomisk aktivitet skjedd/kan ha skjedd - historikk roeres ikke).
      ny foerste kursdag finnes -> faktura_tidligst_dato = seks_maaneder_for(ny foerste kursdag)
      ingen kursdager igjen    -> faktura_tidligst_dato=NULL og sveiper_kjort=0: normal motor gaar da til mangler_kursdag
    INGEN Visma/e-post og ingen commit her. Er ny dato allerede passert plukkes raden av utsatte_fakturaer() etterpaa.
    Returnerer antall oppdaterte paameldinger."""
    dager = db.kursdager(con, kurs_id)
    ny_dato = seks_maaneder_for(date.fromisoformat(dager[0]["dato"])).isoformat() if dager else None
    rader = con.execute(
        """SELECT p.id, p.faktura_tidligst_dato FROM paamelding p JOIN kurs k ON k.id=p.kurs_id
           WHERE p.kurs_id=? AND p.status='bekreftet' AND p.betaling='samlet' AND p.faktura_tidligst_dato IS NOT NULL
             AND k.fakturering='person' AND k.pris_nok > 0
             AND NOT EXISTS (SELECT 1 FROM faktura f WHERE f.paamelding_id=p.id)
             AND NOT EXISTS (SELECT 1 FROM faktura_forsok fo WHERE fo.paamelding_id=p.id)""", (kurs_id,)).fetchall()
    antall = 0
    for r in rader:
        if ny_dato is None:
            con.execute("UPDATE paamelding SET faktura_tidligst_dato=NULL, sveiper_kjort=0 WHERE id=?", (r["id"],))
        elif r["faktura_tidligst_dato"] != ny_dato:
            con.execute("UPDATE paamelding SET faktura_tidligst_dato=? WHERE id=?", (ny_dato, r["id"]))
        else:
            continue
        antall += 1
    if antall:
        db.logg(con, "faktura_hold_oppdatert", {"kurs_id": kurs_id, "antall": antall})
    return antall


def _opprett(k: Kjoring, p, kursdag_id: int | None, belop: int, linjetekst: str) -> None:
    """Oppretter EN faktura (samlet eller for en samling). Transaksjonsforlop (ingen laas over Visma-kallet):

      1. rask sjekk: finnes den ekte fakturaen allerede?                     -> ferdig
      2. claim i faktura_forsok (atomisk; nytt forsok eller retry etter kjent, trygg feil)
      3. sjekk faktura PAA NYTT, i claim-transaksjonen (lukker racet der en annen prosess rakk aa
         opprette fakturaen - og slette sitt forsok - mellom 1. og 2.)        -> rydd forsoket, commit, ferdig
      4. commit claim
      5. visma.fakturer                   (eksternt kall, ingen laas)
      6. EN kort lokal transaksjon: INSERT faktura + oppdater kunde-id + DELETE forsok + logg, commit

    Ekte faktura-rad opprettes ALDRI foer Visma har bekreftet. Taksonomien for Visma-feil er uavklart -
    ALLE feil er 'ukjent' og proves aldri automatisk paa nytt.
    """
    if _faktura_finnes(k.con, p["id"], kursdag_id):
        return  # allerede fakturert – unik indeks i databasen er ekstra sikring
    org = p["betaler"] == "organisasjon"
    g = visma.Fakturagrunnlag(
        kunde_navn=p["org_navn"] if org else p["navn"],
        kunde_epost=p["faktura_epost"] or p["epost"],
        er_privatperson=not org,
        org_nr=p["org_nr"] if org else None,
        adresse=p["faktura_adresse"], postnr=p["faktura_postnr"], sted=p["faktura_sted"],
        ehf=bool(p["ehf"]),
        deres_ref=p["faktura_ref"] or (p["navn"] if org else None),
        linjetekst=linjetekst,
        artikkel=p["visma_artikkel"],
        belop_nok=belop,
    )
    if k.tor:
        k.si(f"  [TØRR] faktura {g.belop_nok} kr -> {g.kunde_navn} ({linjetekst})")
        return

    # Reserver forsoket atomisk FOR Visma-kallet - se schema.sql/trinn 2.5-designet for hvorfor dette
    # er en egen tabell (faktura_forsok) og ikke en ny status paa `faktura` selv. Taper claimen (noen
    # andre har et uavklart forsok paa akkurat denne fakturaen fra for), gjor denne kallen ingenting.
    vant = db.reserver_faktura(k.con, p["id"], kursdag_id) or db.reserver_faktura_pa_nytt(k.con, p["id"], kursdag_id)
    if not vant:
        rad = db.faktura_forsok_rad(k.con, p["id"], kursdag_id)
        if rad and rad["status"] == "reservert" and db.forsok_er_gammel(rad):
            db.logg(k.con, "faktura_reservert_gammel", {"paamelding_id": p["id"], "kursdag_id": kursdag_id})
            k.si(f"  OBS: gammelt uavklart fakturaforsøk for påmelding {p['id']} - krever manuell kontroll")
        k.con.commit()  # ingen no-op-transaksjon (med skrivelaas) staaende - og evt. logg lagres
        return

    # Sjekk paa nytt NAA som vi eier claimen: en annen prosess kan ha fullfoert (ekte faktura + slettet
    # forsok) mellom sjekken over og claimen. Uten dette ville vi fakturert en gang til i Visma.
    if _faktura_finnes(k.con, p["id"], kursdag_id):
        db.fjern_faktura_forsok(k.con, p["id"], kursdag_id)
        k.con.commit()
        return
    k.con.commit()  # gjor reservasjonen synlig for andre FOR det evt. lange Visma-kallet

    try:
        res = visma.fakturer(g)
    except Exception as e:  # noqa: BLE001 - klassifisering "feilet"/"ukjent" ikke avklart enna, se send_en_gang()
        # Taksonomien for Visma-feil er IKKE avklart enna (se trinn 2.5-designet / visma.py). Inntil
        # videre klassifiseres ALT konservativt som ukjent - ALDRI automatisk retry, siden vi ikke kan
        # utelukke at Visma faktisk opprettet fakturaen selv om kallet feilet lokalt.
        feil = sikker_feiltekst(e)  # aldri str(e): Visma-URL-en kan inneholde kundens e-post
        db.sett_faktura_forsok_ukjent(k.con, p["id"], kursdag_id, feil)
        db.logg(k.con, "faktura_ukjent", {"paamelding_id": p["id"], "kursdag_id": kursdag_id, "feil": feil})
        k.con.commit()
        raise

    # Resultat: kort lokal transaksjon, INGEN eksterne kall. Feiler den, ruller vi tilbake alt her og lar
    # forsoket staa som 'reservert' (aldri automatisk retry) - Visma HAR fakturaen, saa det er riktig.
    try:
        k.con.execute(
            """INSERT INTO faktura (paamelding_id, kursdag_id, visma_id, faktura_nr, belop_nok, status)
               VALUES (?,?,?,?,?, 'sendt')""",
            (p["id"], kursdag_id, res.visma_id, res.faktura_nr, g.belop_nok))
        k.con.execute("UPDATE deltaker SET visma_kunde_id=? WHERE id=? AND visma_kunde_id IS NULL",
                      (res.kunde_id, p["did"]))
        db.fjern_faktura_forsok(k.con, p["id"], kursdag_id)
        db.logg(k.con, "fakturert", {"paamelding_id": p["id"], "kursdag_id": kursdag_id,
                                     "faktura_nr": res.faktura_nr, "belop": g.belop_nok})
        k.con.commit()
    except Exception:
        k.con.rollback()
        raise
    k.si(f"  faktura {res.faktura_nr} {g.belop_nok} kr (påmelding {p['id']})")  # ingen navn i driftsutskriften
