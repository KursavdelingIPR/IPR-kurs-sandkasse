"""Daglig jobb (cron / Planlagt oppgave kl 07:00). Idempotent: kan kjores flere ganger samme dag.

    python -m kurs.daglig                  # vanlig kjoring
    python -m kurs.daglig --tor            # vis hva som ville skjedd
    python -m kurs.daglig --dato 2027-01-11 --tor   # lat som det er en annen dag (testing)

Rekkefolge:
  1. Sveiper for nye paameldinger som ikke er ferdigbehandlet
  2. Opprett Zoom-mote ~8 dager for start (digitale/hybride kurs)
  3. Innkallinger: uka for + dagen for hver kursdag
  4. Kursstatus (aktiv / avsluttet) + oppmoteimport fra Zoom
  5. Kursbevis til de som har mott
  6. Purring paa materiell fra kursholdere
  7. Sletting av sensitive opplysninger
"""
import argparse
from datetime import date, timedelta

from . import config, db, kursbevis, sveiper
from .integrasjoner import zoom
from .kjoring import Kjoring


def kjor(k: Kjoring) -> None:
    k.si(f"== Daglig kjøring for {k.idag}  (modus={config.MODUS}{', TØRR' if k.tor else ''}) ==")
    k.si("1. Nye påmeldinger")
    sveiper.kjor(k)
    k.si("1b. Delfakturaer som forfaller")
    sveiper.forfalte_delfakturaer(k)

    kursliste = k.con.execute(
        "SELECT * FROM kurs WHERE status IN ('aapen','full','aktiv') ORDER BY id"
    ).fetchall()
    for kurs in kursliste:
        dager = db.kursdager(k.con, kurs["id"])
        if not dager:
            continue
        forste, siste = date.fromisoformat(dager[0]["dato"]), date.fromisoformat(dager[-1]["dato"])
        k.si(f"-- {kurs['kode']} {kurs['navn']} ({forste} – {siste})")
        kurs = _zoom(k, kurs, forste)
        _innkallinger(k, kurs, dager, forste)
        _status_og_oppmote(k, kurs, dager, forste, siste)

    k.si("5. Kursbevis")
    kursbevis.kjor(k)
    k.si("6. Purring på materiell")
    _purring(k)
    k.si("7. Personvern")
    _slett_sensitivt(k)
    k.avslutt()


def _deltakere(k, kurs_id):
    return k.con.execute(
        """SELECT p.id, d.navn, d.epost FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id
           WHERE p.kurs_id=? AND p.status='bekreftet'""", (kurs_id,)).fetchall()


def _zoom(k, kurs, forste):
    if kurs["type"] == "fysisk" or kurs["zoom_url"] or (forste - k.idag).days > 8:
        return kurs
    if k.tor:
        k.si("  [TØRR] oppretter Zoom-møte")
        return kurs
    m = zoom.opprett_mote(kurs["navn"])
    k.con.execute("UPDATE kurs SET zoom_url=?, zoom_id=?, zoom_pw=? WHERE id=?",
                  (m["join_url"], str(m["id"]), m.get("password"), kurs["id"]))
    db.logg(k.con, "zoom_opprettet", {"kurs_id": kurs["id"], "zoom_id": m["id"]})
    k.si(f"  Zoom-møte opprettet: {m['join_url']}")
    return k.con.execute("SELECT * FROM kurs WHERE id=?", (kurs["id"],)).fetchone()


def _innkallinger(k, kurs, dager, forste):
    nokkel = f"kurs:{kurs['id']}"
    deltakere = _deltakere(k, kurs["id"])
    # Uka for: sendes fra 7 dager for og fram til start – sene paameldte faar den ogsaa
    if 0 < (forste - k.idag).days <= 7:
        for d in deltakere:
            k.send_en_gang(nokkel, d["epost"], "ukefor", "ukefor", d=d, kurs=kurs, dager=dager)
    # Dagen for hver kursdag
    for i, dag in enumerate(dager, start=1):
        if date.fromisoformat(dag["dato"]) - k.idag == timedelta(days=1):
            for d in deltakere:
                k.send_en_gang(nokkel, d["epost"], f"dagfor-{dag['dato']}", "dagfor",
                               d=d, kurs=kurs, dag=dag, nr=i, antall=len(dager))


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
    krav = k.con.execute(
        """SELECT m.*, k.navn AS kursnavn, k.kode FROM materiell_krav m JOIN kurs k ON k.id=m.kurs_id
           WHERE m.levert_ts IS NULL AND k.status!='avlyst'""").fetchall()
    for m in krav:
        igjen = (date.fromisoformat(m["frist"]) - k.idag).days
        nokkel = f"materiell:{m['id']}"
        if igjen in (7, 2, 0) or (0 < igjen < 7 and not db.allerede_sendt(k.con, nokkel, m["ansvarlig_epost"], "purring-7")):
            type_ = "purring-7" if igjen > 2 else ("purring-2" if igjen > 0 else "purring-0")
            k.send_en_gang(nokkel, m["ansvarlig_epost"], type_, "purring", m=m, igjen=igjen)
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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tor", action="store_true", help="tørrkjøring – ingenting sendes eller lagres")
    ap.add_argument("--dato", type=date.fromisoformat, help="overstyr dagens dato (YYYY-MM-DD)")
    a = ap.parse_args(argv)
    con = db.koble()
    db.init(con)
    kjor(Kjoring(con, idag=a.dato or date.today(), tor=a.tor))


if __name__ == "__main__":
    main()
