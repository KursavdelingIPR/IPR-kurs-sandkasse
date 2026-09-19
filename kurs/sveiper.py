"""Det som skjer automatisk naar noen melder seg paa (spec del B: 'automatiske sveiper').

Kjores umiddelbart fra nettskjemaet OG av den daglige jobben (fanger opp det som feilet).
Hver deltaker behandles for seg: feiler Visma for én, stopper det ikke de andre.

Fakturering styres av kurs.betaling / paamelding.betaling:
  samlet      -> én faktura paa hele belopet ved paamelding
  per_samling -> én faktura pr kursdag, opprettet `kurs.faktura_dager_for` dager for hver samling
"""
from datetime import date, datetime

from . import db
from .integrasjoner import visma
from .kjoring import Kjoring

# Hvor lenge en 'reservert' faktura_forsok-rad kan staa uavklart for den regnes som trolig forlatt
# (prosessen doede midt i et Visma-kall) og flagges for admin - godt over Vismas eget 30s-tidsavbrudd.
# Aldri automatisk retry uansett alder - se schema.sql/trinn 2.5-designet.
_FORSOK_ALDERSGRENSE_MIN = 5

SQL_DELTAKER = """SELECT p.*, d.navn, d.epost, d.id AS did, k.navn AS kursnavn, k.kode, k.pris_nok, k.fakturering,
                         k.visma_artikkel, k.type AS kurstype, k.faktura_dager_for
                  FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id JOIN kurs k ON k.id=p.kurs_id"""


def kjor(k: Kjoring, paamelding_id: int | None = None) -> None:
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
        SQL_DELTAKER + """ WHERE p.sveiper_kjort=0 AND p.sveiper_utsatt=0 AND p.status IN ('bekreftet','venteliste')
                           AND k.status NOT IN ('avlyst','utkast')"""
        + (" AND p.id=?" if paamelding_id else ""),
        (paamelding_id,) if paamelding_id else ()).fetchall()
    for p in rader:
        try:
            _en(k, p)
        except Exception as e:  # noqa: BLE001 – logg og fortsett med neste
            db.logg(k.con, "sveip_feil", {"paamelding_id": p["id"], "feil": str(e)})
            k.si(f"  FEIL for {p['epost']}: {e}")


def _en(k: Kjoring, p) -> None:
    nokkel = f"kurs:{p['kurs_id']}"
    kurs = k.con.execute("SELECT * FROM kurs WHERE id=?", (p["kurs_id"],)).fetchone()
    dager = db.kursdager(k.con, p["kurs_id"])

    if p["status"] == "venteliste":
        k.send_en_gang(nokkel, p["epost"], "venteliste", "venteliste", p=p, kurs=kurs)
        return  # ikke fakturer / ikke merk ferdig – kjores paa nytt naar de flyttes opp

    k.send_en_gang(nokkel, p["epost"], "bekreftelse", "bekreftelse", p=p, kurs=kurs, dager=dager)
    if not db.allerede_sendt(k.con, nokkel, p["epost"], "bekreftelse"):
        return  # bekreftelsen er ikke trygt bekreftet sendt enda (reservert/feilet/ukjent hos noen -
                # kanskje denne kjoringen selv) - ikke fakturer, ikke marker ferdig. Tas igjen senere,
                # siden sveiper_kjort fortsatt er 0.
    fakturer(k, p)
    if skal_faktureres(p) and not _fakturering_ferdig(k, p):
        return  # minst én forfalt faktura er ikke definitivt ferdig (reservert/feilet/ukjent) enna
    k.con.execute("UPDATE paamelding SET sveiper_kjort=1 WHERE id=?", (p["id"],))


def _fakturering_ferdig(k: Kjoring, p) -> bool:
    """True naar hver ENDA FORFALT faktura for paameldingen finnes som en ekte, bekreftet faktura-
    rad - dvs. ingen uavklart Visma-forsok igjen. Kalles kun naar skal_faktureres(p) er True.

    Fremtidige (ikke-forfalte) delfakturaer skal IKKE kreves ferdige her - de hentes opp av
    forfalte_delfakturaer() naar de forfaller, akkurat som i dag.
    """
    if p["betaling"] != "per_samling":
        return k.con.execute(
            "SELECT 1 FROM faktura WHERE paamelding_id=? AND kursdag_id IS NULL", (p["id"],)).fetchone() is not None
    for dag in db.kursdager(k.con, p["kurs_id"]):
        if _forfalt(k.idag, dag["dato"], p["faktura_dager_for"]) and not k.con.execute(
                "SELECT 1 FROM faktura WHERE paamelding_id=? AND kursdag_id=?", (p["id"], dag["id"])).fetchone():
            return False
    return True


def skal_faktureres(p) -> bool:
    return p["fakturering"] == "person" and p["pris_nok"] > 0 and p["status"] == "bekreftet"


def delbelop(pris: int, antall: int) -> list[int]:
    """Deler belopet likt. Rest legges paa forste faktura, slik at summen alltid stemmer."""
    grunn, rest = divmod(pris, antall)
    return [grunn + rest] + [grunn] * (antall - 1)


def fakturer(k: Kjoring, p) -> None:
    """Oppretter de fakturaene som skal finnes for denne deltakeren akkurat naa."""
    if not skal_faktureres(p):
        return
    if p["betaling"] != "per_samling":
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
            db.logg(k.con, "faktura_feil", {"paamelding_id": p["id"], "feil": str(e)})
            k.si(f"  FEIL delfaktura for {p['epost']}: {e}")


def _opprett(k: Kjoring, p, kursdag_id: int | None, belop: int, linjetekst: str) -> None:
    if k.con.execute(
            "SELECT 1 FROM faktura WHERE paamelding_id=? AND COALESCE(kursdag_id,0)=?",
            (p["id"], kursdag_id or 0)).fetchone():
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
        if rad and rad["status"] == "reservert" and _forsok_er_gammel(rad):
            db.logg(k.con, "faktura_reservert_gammel", {"paamelding_id": p["id"], "kursdag_id": kursdag_id})
            k.si(f"  OBS: gammelt uavklart fakturaforsok for {p['epost']} - krever manuell kontroll")
        return
    k.con.commit()  # gjor reservasjonen synlig for andre FOR det evt. lange Visma-kallet

    try:
        res = visma.fakturer(g)
    except Exception as e:  # noqa: BLE001 - klassifisering "feilet"/"ukjent" ikke avklart enna, se send_en_gang()
        # Taksonomien for Visma-feil er IKKE avklart enna (se trinn 2.5-designet / visma.py). Inntil
        # videre klassifiseres ALT konservativt som ukjent - ALDRI automatisk retry, siden vi ikke kan
        # utelukke at Visma faktisk opprettet fakturaen selv om kallet feilet lokalt.
        db.sett_faktura_forsok_ukjent(k.con, p["id"], kursdag_id, str(e))
        db.logg(k.con, "faktura_ukjent", {"paamelding_id": p["id"], "kursdag_id": kursdag_id, "feil": str(e)})
        k.con.commit()
        raise

    k.con.execute(
        """INSERT INTO faktura (paamelding_id, kursdag_id, visma_id, faktura_nr, belop_nok, status)
           VALUES (?,?,?,?,?, 'sendt')""",
        (p["id"], kursdag_id, res.visma_id, res.faktura_nr, g.belop_nok))
    k.con.execute("UPDATE deltaker SET visma_kunde_id=? WHERE id=? AND visma_kunde_id IS NULL", (res.kunde_id, p["did"]))
    db.fjern_faktura_forsok(k.con, p["id"], kursdag_id)
    db.logg(k.con, "fakturert", {"paamelding_id": p["id"], "kursdag_id": kursdag_id,
                                 "faktura_nr": res.faktura_nr, "belop": g.belop_nok})
    k.si(f"  faktura {res.faktura_nr} {g.belop_nok} kr -> {g.kunde_navn} ({linjetekst})")


def _forsok_er_gammel(rad) -> bool:
    alder = datetime.now() - datetime.fromisoformat(rad["opprettet"])
    return alder.total_seconds() > _FORSOK_ALDERSGRENSE_MIN * 60
