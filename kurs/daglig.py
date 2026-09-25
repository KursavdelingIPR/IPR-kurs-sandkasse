"""Daglig jobb (cron / Planlagt oppgave kl 07:00). Idempotent: kan kjores flere ganger samme dag.

    python -m kurs.daglig                  # vanlig kjoring
    python -m kurs.daglig --tor            # vis hva som ville skjedd
    python -m kurs.daglig --dato 2027-01-11 --tor   # lat som det er en annen dag (testing)

Rekkefolge:
  1. Sveiper for nye paameldinger som ikke er ferdigbehandlet
     (1b: delfakturaer som forfaller, 1c: utsatte samlede fakturaer som har naadd tidligst-datoen)
  2. Opprett Zoom-mote ~8 dager for start (digitale/hybride kurs)
  3. Innkallinger: uka for + dagen for hver kursdag
  4. Kursstatus (aktiv / avsluttet) + oppmoteimport fra Zoom
  5. Kursbevis til de som har mott
  6. Purring paa materiell fra kursholdere
  7. Sletting av sensitive opplysninger
  8. Rydding av utlopte import-forhaandsvisninger (fase 10)
"""
import argparse
from datetime import date, timedelta

from . import config, db, import_deltakere, kursbevis, lenker, maltekster, sveiper
from .integrasjoner import zoom
from .feil import sikker_feiltekst
from .kjoring import Kjoring


def _steg(k: Kjoring, navn: str, funksjon, *args, **logg) -> None:
    """Kjoerer ett steg med feilisolering. Hvert vellykket steg lagres (commit) for seg; feiler et steg, rulles bare
    DETS ulagrede arbeid tilbake, feilen logges PII-fritt, og resten av morgenjobben fortsetter - en feil hos Microsoft
    Graph i ett kurs skal aldri stoppe de andre kursene, kursbevis, purringer eller slettingen av sensitive data.
    Allerede reserverte/sendte e-poster er committet av claim-motoren og roeres ikke."""
    try:
        funksjon(*args)
    except Exception as e:  # noqa: BLE001 - logg og fortsett med neste steg
        if k.tor:
            db.rull_tilbake_hvis_avbrutt(k.con)
        else:
            k.con.rollback()
        feil = sikker_feiltekst(e)
        db.logg(k.con, "daglig_feil", {"steg": navn, **logg, "feil": feil})
        k.feil.append(navn)
        k.si(f"  FEIL i «{navn}»: {feil} - resten av kjøringen fortsetter")
    if not k.tor:
        k.con.commit()


def _kurs(k: Kjoring, kurs) -> None:
    dager = db.kursdager(k.con, kurs["id"])
    if not dager:
        return
    forste, siste = date.fromisoformat(dager[0]["dato"]), date.fromisoformat(dager[-1]["dato"])
    k.si(f"-- {kurs['kode']} {kurs['navn']} ({forste} – {siste})")
    # 1. Preflight (ren lesing): er malteksten for dagens innkallinger gyldig? FOER en evt. NY ekstern Zoom-opprettelse.
    deltakere = _deltakere(k, kurs["id"])
    blokkert = _forhandsvalider_innkallinger(k, kurs, dager, forste, deltakere)
    kurs = _zoom(k, kurs, forste, utsett=_skal_utsette_zoom(k, kurs, dager, forste, blokkert))
    _innkallinger(k, kurs, dager, forste, deltakere=deltakere, blokkert=blokkert)
    _status_og_oppmote(k, kurs, dager, forste, siste)


def kjor(k: Kjoring) -> None:
    k.si(f"== Daglig kjøring for {k.idag}  (modus={config.MODUS}{', TØRR' if k.tor else ''}) ==")
    k.si("1. Nye påmeldinger")
    _steg(k, "nye påmeldinger", sveiper.kjor, k)
    k.si("1b. Delfakturaer som forfaller")
    _steg(k, "delfakturaer", sveiper.forfalte_delfakturaer, k)
    k.si("1c. Utsatte samlede fakturaer")
    _steg(k, "utsatte fakturaer", sveiper.utsatte_fakturaer, k)

    kursliste = k.con.execute(
        "SELECT * FROM kurs WHERE status IN ('aapen','full','aktiv') ORDER BY id"
    ).fetchall()
    for kurs in kursliste:
        _steg(k, "kurs", _kurs, k, kurs, kurs_id=kurs["id"])

    k.si("5. Kursbevis")
    _steg(k, "kursbevis", kursbevis.kjor, k)
    k.si("6. Purring på materiell")
    _steg(k, "purring", _purring, k)
    k.si("7. Personvern")
    _steg(k, "personvern", _slett_sensitivt, k)
    k.si("8. Import-forhåndsvisninger")
    _steg(k, "importrydding", _rydd_import_forhaandsvisninger, k)
    if k.sending_stanset:
        k.si("OBS: e-posttjenesten feilet flere ganger på rad - resten av dagens e-poster sendes ved neste kjøring.")
    if k.feil:
        k.si(f"Ferdig med {len(k.feil)} feil (se hendelsesloggen, «daglig_feil»): {', '.join(k.feil)}")
    k.avslutt()


def _deltakere(k, kurs_id):
    return k.con.execute(
        """SELECT p.id, d.navn, d.epost FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id
           WHERE p.kurs_id=? AND p.status='bekreftet'""", (kurs_id,)).fetchall()


def _trenger_zoom(k, kurs, forste) -> bool:
    """Skal det opprettes et NYTT Zoom-moete for dette kurset naa? (Uendret vilkaar, flyttet ut av _zoom.)"""
    return not (kurs["type"] == "fysisk" or kurs["zoom_url"] or (forste - k.idag).days > 8)


def _skal_utsette_zoom(k, kurs, dager, forste, blokkert) -> bool:
    """Maltype-spesifikk beslutning. Ugyldig maltekst for en innkalling som skal sendes i dag utsetter NY Zoom-opprettelse
    (ingen ny ekstern sideeffekt foer malen er rettet) - MED MINDRE en GYLDIG dagfor er aktuell i dag: dagfor er den eneste
    innkallingen som trenger Zoom-lenken, og en korrupt ukefor (som ikke inneholder lenken) skal aldri hindre den.
    Korrupt dagfor (eneste som trenger lenken) eller korrupt ukefor uten dagfor i dag utsetter altsaa Zoom."""
    if not blokkert or not _trenger_zoom(k, kurs, forste):
        return False
    gyldig_dagfor_i_dag = "dagfor" not in blokkert and any(mal == "dagfor" for mal, _t, _d in _dagens_innkallinger(k, dager, forste))
    return not gyldig_dagfor_i_dag


def _zoom(k, kurs, forste, utsett: bool = False):
    if not _trenger_zoom(k, kurs, forste):
        return kurs
    if utsett:   # en innkalling som skal sendes i dag har ugyldig maltekst: ingen NY ekstern sideeffekt foer malen er rettet
        k.si("  Zoom-opprettelse utsatt: ugyldig e-postmal for dagens innkalling (rettes malen, tas det neste kjøring)")
        return kurs
    if k.tor:
        k.si("  [TØRR] oppretter Zoom-møte")
        return kurs
    m = zoom.opprett_mote(kurs["navn"])
    # Lagres og committes STRAKS (før e-postene som inneholder lenken): en senere feil i samme kjøring skal aldri kunne
    # rulle tilbake lenken, slik at neste kjøring lager et nytt møte med en annen lenke enn den deltakerne alt har fått.
    # «AND zoom_url IS NULL»: en lenke admin har lagt inn i mellomtiden overskrives aldri.
    lagret = k.con.execute("UPDATE kurs SET zoom_url=?, zoom_id=?, zoom_pw=? WHERE id=? AND zoom_url IS NULL",
                           (m["join_url"], str(m["id"]), m.get("password"), kurs["id"])).rowcount == 1
    db.logg(k.con, "zoom_opprettet" if lagret else "zoom_mote_ubrukt", {"kurs_id": kurs["id"], "zoom_id": m["id"]})
    k.con.commit()
    if lagret:
        k.si(f"  Zoom-møte opprettet (møte-ID {m['id']})")   # aldri selve lenken i loggen (den gir adgang til møtet)
    else:
        k.si(f"  Zoom-møte {m['id']} ble ikke brukt: kurset fikk en lenke i mellomtiden (kan slettes i Zoom)")
    return k.con.execute("SELECT * FROM kurs WHERE id=?", (kurs["id"],)).fetchone()


def _dagens_innkallinger(k, dager, forste) -> list:
    """Hvilke innkallinger som skal sendes i dag - UENDRET tidsvindu og utvalg. [(mal, type_, data_for(kurs, deltaker))]."""
    ut = []
    # Uka for: sendes fra 7 dager for og fram til start – sene paameldte faar den ogsaa
    if 0 < (forste - k.idag).days <= 7:
        ut.append(("ukefor", "ukefor", lambda kurs, d: dict(d=d, kurs=kurs, dager=dager)))
    # Dagen for hver kursdag
    for i, dag in enumerate(dager, start=1):
        if date.fromisoformat(dag["dato"]) - k.idag == timedelta(days=1):
            ut.append(("dagfor", f"dagfor-{dag['dato']}",
                       lambda kurs, d, dag=dag, i=i: dict(d=d, kurs=kurs, dag=dag, nr=i, antall=len(dager))))
    return ut


def _forhandsvalider_innkallinger(k, kurs, dager, forste, deltakere) -> dict:
    """Ren lesing (ingen skriving, ingen ekstern sideeffekt): rendrer dagens innkallinger for foerste mottaker og returnerer
    {mal: MalFeil} for maltyper med ugyldig MALTEKST (korrupt override, ukjent felt, DB-lesefeil). Data som mangler for en
    enkelt mottaker (mangler_verdi) regnes IKKE som malfeil her - den mottakeren feiler lukket ved selve sendingen."""
    blokkert = {}
    if not deltakere:
        return blokkert
    for mal, _type, data_for in _dagens_innkallinger(k, dager, forste):
        if mal in blokkert:
            continue
        try:
            k.render_for_sending(mal, **data_for(kurs, deltakere[0]))
        except maltekster.MalFeil as e:
            if e.grunn != maltekster.MANGLER_VERDI:
                blokkert[mal] = e
    return blokkert


def _logg_innkalling_feil(k, kurs, mal, feil, antall) -> None:
    """Trygg logg, EN hendelse per (kurs, mal) per kjoering: kun kurs_id, mal, felt, grunnkode og antall - aldri tekst, navn eller e-post."""
    db.logg(k.con, "innkalling_feil", {"kurs_id": kurs["id"], **feil.detaljer(), "antall": antall})
    k.si(f"  FEIL {mal}: {feil} - {antall} mottaker(e) fikk ikke innkallingen")


def _send(k, nokkel, d, mal, type_, data):
    """EN utsending. Malnavnet staar som strengliteral i hvert kall (driftvakt: mal-ID = Jinja-fil = navn i send_en_gang)."""
    if mal == "ukefor":
        return k.send_en_gang(nokkel, d["epost"], type_, "ukefor", paamelding_id=d["id"], **data)
    return k.send_en_gang(nokkel, d["epost"], type_, "dagfor", paamelding_id=d["id"], **data)


def _innkallinger(k, kurs, dager, forste, deltakere=None, blokkert=None):
    """Sender dagens innkallinger. MalFeil isoleres per utsending (fail closed for AKKURAT den meldingen): den aborterer aldri
    daglig.kjor(), og en mottaker med manglende data stopper ikke de neste. Andre feil (f.eks. Graph) er uendret."""
    nokkel = f"kurs:{kurs['id']}"
    deltakere = _deltakere(k, kurs["id"]) if deltakere is None else deltakere
    blokkert = _forhandsvalider_innkallinger(k, kurs, dager, forste, deltakere) if blokkert is None else blokkert
    for mal, type_, data_for in _dagens_innkallinger(k, dager, forste):
        if mal in blokkert:   # ugyldig maltekst: ingen render/claim/sending for noen mottaker
            _logg_innkalling_feil(k, kurs, mal, blokkert[mal], len(deltakere))
            continue
        feil, antall = None, 0
        for d in deltakere:
            try:
                _send(k, nokkel, d, mal, type_, data_for(kurs, d))
            except maltekster.MalFeil as e:
                feil, antall = feil or e, antall + 1
        if feil:
            _logg_innkalling_feil(k, kurs, mal, feil, antall)


def _status_og_oppmote(k, kurs, dager, forste, siste):
    if kurs["status"] in ("aapen", "full") and k.idag >= forste:
        k.con.execute("UPDATE kurs SET status='aktiv' WHERE id=?", (kurs["id"],))
        k.si("  status -> aktiv")
    # Oppmote fra Zoom for gaarsdagens (eller tidligere) digitale kursdager
    if kurs["type"] != "fysisk" and kurs["zoom_id"]:
        for dag in dager:
            if date.fromisoformat(dag["dato"]) < k.idag and not db.allerede_sendt(k.con, f"zoomimport:{dag['id']}", "-", "import"):
                _importer_zoom(k, kurs, dag)
    if k.idag > siste:
        k.con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kurs["id"],))
        k.si("  status -> avsluttet")


def _importer_zoom(k, kurs, dag, min_minutter: int = 30):
    if k.tor:
        k.si(f"  [TØRR] importerer Zoom-oppmøte for {dag['dato']}")
        return
    liste = zoom.deltakere_for_dato(kurs["zoom_id"], dag["dato"])
    deltakere = {d["epost"].lower(): d["id"] for d in _deltakere(k, kurs["id"])}
    navn = {d["navn"].lower(): d["id"] for d in _deltakere(k, kurs["id"])}
    treff = 0
    for z in liste:
        pid = deltakere.get((z.get("epost") or "").lower()) or navn.get((z.get("navn") or "").lower())
        if pid and z["minutter"] >= min_minutter:
            treff += db.registrer_oppmote(k.con, pid, dag["id"], "zoom", z["minutter"])
    db.marker_sendt(k.con, f"zoomimport:{dag['id']}", "-", "import")
    k.si(f"  Zoom-oppmøte {dag['dato']}: {treff} registrert av {len(liste)} i Zoom")


def _purring(k):
    """Purrer paa materiell inntil frist (tre trinn: 7/2/0 dager igjen), deretter eskalerer til admin hver dag etter frist.

    Kandidatregel (RETTET 12B2C-5 - se dokumentasjon/audit): et trinn er kandidat HVER dag fra igjen==7 ned til
    igjen==0, men bare naar NOEYAKTIG DET trinnets type (purring-7/-2/-0) IKKE allerede er sendt. Foer denne
    rettelsen sjekket koden kun om purring-7 var sendt, ogsaa naar dagens trinn var purring-2/-0 - en render-/
    malfeil noeyaktig paa igjen==2 eller igjen==0 (naar purring-7 alt var sendt tidligere) hadde da INGEN
    retry-mulighet og gikk permanent tapt. Naa faar hvert trinn sitt eget, flerdagers retry-vindu paa samme
    maate som purring-7 alt hadde (igjen==0 er fortsatt siste sjanse for TRINN 0 spesifikt, siden frist da er
    passert dagen etter - kjent, akseptert begrensning, samme kategori som dagfor sitt endagsvindu).
    Ingen dobbeltsending: claim/status-motoren (fase 11) hindrer det uansett, og guarden under unngaar i tillegg
    unoedvendig re-rendring naar trinnet alt er sendt.

    En MalFeil (ugyldig lagret mal) isoleres PER MATERIELLKRAV: den aktuelle paaminnelsen feiler lukket (ingen
    claim, ingen mail), mens andre materiellkrav og resten av daglig.kjor() fortsetter uendret."""
    krav = k.con.execute(
        """SELECT m.*, k.navn AS kursnavn, k.kursnr, k.kode FROM materiell_krav m JOIN kurs k ON k.id=m.kurs_id
           WHERE m.levert_ts IS NULL AND k.status!='avlyst'""").fetchall()
    for m in krav:
        igjen = (date.fromisoformat(m["frist"]) - k.idag).days
        nokkel = f"materiell:{m['id']}"
        if 0 <= igjen <= 7:
            type_ = "purring-7" if igjen > 2 else ("purring-2" if igjen > 0 else "purring-0")
            if not db.allerede_sendt(k.con, nokkel, m["ansvarlig_epost"], type_):
                try:
                    k.send_en_gang(nokkel, m["ansvarlig_epost"], type_, "purring", m=m, igjen=igjen,
                                   lever_lenke=lenker.lever_lenke(m["id"]))
                except maltekster.MalFeil as e:
                    db.logg(k.con, "purring_mal_feil", {"kurs_id": m["kurs_id"], "materiell_id": m["id"], **e.detaljer()})
                    k.si(f"  FEIL purring ({type_}): {e} - materiellkrav {m['id']} fikk ikke påminnelsen")
        elif igjen < 0:
            k.send_en_gang(nokkel, config.ADMIN_EPOST, "eskalering", "eskalering", m=m, igjen=igjen)


def _slett_sensitivt(k):
    grense = (k.idag - timedelta(days=config.SLETT_SENSITIVT_ETTER_DAGER)).isoformat()
    cur = k.con.execute(
        """DELETE FROM sensitivt WHERE paamelding_id IN (
             SELECT p.id FROM paamelding p WHERE p.kurs_id IN (
               SELECT kurs_id FROM kursdag GROUP BY kurs_id HAVING MAX(dato) < ?))""", (grense,))
    if cur.rowcount:
        db.logg(k.con, "sensitivt_slettet", {"antall": cur.rowcount})
        k.si(f"  slettet sensitive opplysninger for {cur.rowcount} påmeldinger")


def _rydd_import_forhaandsvisninger(k):
    """Fase 10: fjerner utlopte CSV-import-forhaandsvisninger (kan inneholde allergi/tilrette-
    legging/fakturainformasjon - skal ikke ligge lenger enn levetiden, se import_deltakere.py).
    Bruker faktisk klokkeslett (ikke k.idag, som kun styrer HVILKEN DATO jobben later som det er)
    - en forhaandsvisning er tidsbegrenset i minutter, ikke i kalenderdager."""
    antall = import_deltakere.rydd_utlopte_forhaandsvisninger(k.con)
    if antall:
        k.si(f"  ryddet {antall} utløpt(e) import-forhåndsvisning(er)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tor", action="store_true", help="tørrkjøring – ingenting sendes eller lagres")
    ap.add_argument("--dato", type=date.fromisoformat, help="overstyr dagens dato (YYYY-MM-DD)")
    a = ap.parse_args(argv)
    con = db.koble()
    if config.DEMO:
        db.init(con)          # lokal sandkasse: hold databasen i takt automatisk
    else:
        from . import migreringer
        try:
            migreringer.kontroller(con)   # drift: migrering er et eget, eksplisitt steg (python -m kurs.migrer)
        except migreringer.VersjonsFeil as e:
            print(f"STOPP: {e}")
            return 3
    k = Kjoring(con, idag=a.dato or date.today(), tor=a.tor)
    kjor(k)
    return 1 if k.feil else 0          # 1: minst ett steg feilet (driften varsles), resten ble likevel kjoert


if __name__ == "__main__":
    raise SystemExit(main())
