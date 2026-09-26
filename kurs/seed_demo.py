"""Nullstiller databasen og legger inn OPPDIKTEDE demodata. Datoer er relative til i dag,
slik at demoen alltid viser: et kurs som gaar i dag (QR), et som starter om 6 dager (ukefor-mail),
et avsluttet kurs med kursbevis, og et fullt kurs med venteliste.

    python -m kurs.seed_demo
"""
import shutil
from datetime import date, timedelta

from . import config, daglig, db
from .integrasjoner import sharepoint
from .kjoring import Kjoring

PERSONER = [   # (fornavn, etternavn, e-post, arbeidssted)
    ("Kari", "Nordmann", "kari.nordmann@example.no", "Familievernkontoret Bergen"),
    ("Ola", "Hansen", "ola.hansen@example.no", "DPS Nordfjord"),
    ("Aisha", "Rahman", "aisha.rahman@example.no", "Privat praksis"),
    ("Jonas", "Berg", "jonas.berg@example.no", "BUP Oslo"),
    ("Ingrid Marie", "Solheim", "ingrid.solheim@example.no", "Bergen kommune"),
    ("Mateusz", "Nowak", "mateusz.nowak@example.no", "DPS Vest"),
    ("Silje", "Dahl", "silje.dahl@example.no", "Privat praksis"),
    ("Eirik", "Lunde", "eirik.lunde@example.no", "Helse Fonna"),
]


def main():
    if config.MODUS == "prod":
        raise SystemExit("Nekter å kjøre seed_demo i prod.")
    if config.DB_STI.exists():
        config.DB_STI.unlink()
    for mappe in (config.UTBOKS, config.ROT / "data" / "sharepoint_demo", config.ROT / "data" / "kursbevis"):
        shutil.rmtree(mappe, ignore_errors=True)

    con = db.koble()
    db.init(con)
    i = date.today()
    d = lambda n: (i + timedelta(days=n)).isoformat()  # noqa: E731
    standardadmin = con.execute("SELECT id FROM admin_bruker WHERE brukernavn=?", (config.ADMIN_BRUKERNAVN,)).fetchone()["id"]

    # 1) Avsluttet EFT-samling (for 3 uker siden) – gir kursbevis + timer i spesialistlop
    k1 = db.opprett_kurs(con, kode="EFT-S1", navn="EFT spesialistutdanning – samling 1", datoer=[d(-22), d(-21)],
                         type="fysisk", sted="IPR, Bergen", pris_nok=14500, spesialistlop="EFT", timer_pr_dag=7,
                         kapasitet=16, status="aktiv", sharepoint_mappe=sharepoint.opprett_kursmappe("EFT-S1"),
                         ansvarlig_admin_id=standardadmin)
    # 2) EFT samling 2 – starter om 6 dager (ukefor-mail), 2 dager
    k2 = db.opprett_kurs(con, kode="EFT-S2", navn="EFT spesialistutdanning – samling 2", datoer=[d(6), d(7)],
                         type="fysisk", sted="IPR, Bergen", pris_nok=14500, spesialistlop="EFT", timer_pr_dag=7,
                         kapasitet=16, kursholder_epost="psykolog.a@ipr.no",
                         betaling="deltaker_velger", faktura_dager_for=14,
                         notat="Lunsj er inkludert. Ta med egne case-notater (anonymisert).",
                         sharepoint_mappe=sharepoint.opprett_kursmappe("EFT-S2"),
                         ansvarlig_admin_id=standardadmin)
    # 3) Digitalt kurs med kursdag I DAG (QR/kode-demo + Zoom)
    k3 = db.opprett_kurs(con, kode="PAR-DIG", navn="Parterapi i praksis – digitalt fordypningskurs",
                         datoer=[d(-7), d(0), d(7)], type="hybrid", sted="Zoom / IPR Bergen", pris_nok=4900,
                         start_kl="12:00", slutt_kl="15:00", timer_pr_dag=3,
                         sharepoint_mappe=sharepoint.opprett_kursmappe("PAR-DIG"))
    # 4) Lite kurs som er fullt -> venteliste
    k4 = db.opprett_kurs(con, kode="SUPERV-1", navn="Veiledning i gruppe – introduksjon", datoer=[d(20)],
                         type="fysisk", sted="IPR, Bergen", pris_nok=2500, kapasitet=3,
                         sharepoint_mappe=sharepoint.opprett_kursmappe("SUPERV-1"))
    # 5) Bedriftskurs fakturert samlet
    db.opprett_kurs(con, kode="BHT-2027", navn="Stressmestring for ledere (bedriftsinternt)", datoer=[d(35)],
                    type="fysisk", sted="Kundens lokaler", pris_nok=0, fakturering="organisasjon",
                    sharepoint_mappe=sharepoint.opprett_kursmappe("BHT-2027"))

    def meld(kurs_id, idx, **kw):
        fornavn, etternavn, epost_, arb = PERSONER[idx]
        pid, _ = db.meld_paa(con, kurs_id, epost=epost_, fornavn=fornavn, etternavn=etternavn,
                             deltaker={"arbeidssted": arb}, **kw)
        return pid

    org = {"betaler": "organisasjon", "org_navn": "Bergen kommune", "org_nr": "964338531", "ehf": 1, "faktura_ref": "BK-4471"}
    s1 = [meld(k1, n) for n in range(6)]
    for n in range(6):
        # Kari (n=0) velger å dele opp betalingen, Ingrid (n=4) faktureres via arbeidsgiver
        meld(k2, n, paamelding={**org} if n == 4 else ({"betaling": "per_samling"} if n == 0 else None),
             sensitivt={"allergier": "Glutenfri" if n == 1 else None, "tilrettelegging": "Rullestoltilgang" if n == 3 else None})
    s3 = [meld(k3, n) for n in (0, 2, 5, 6, 7)]
    for n in (1, 3, 4, 6, 7):
        meld(k4, n)  # kapasitet 3 -> to paa venteliste

    # Oppmote: samling 1 (alle begge dager, utenom Ola dag 2) og digitalt kurs dag 1
    dager1 = db.kursdager(con, k1)
    for n, pid in enumerate(s1):
        for j, dag in enumerate(dager1):
            if not (n == 1 and j == 1):
                db.registrer_oppmote(con, pid, dag["id"], "qr" if j == 0 else "kode")
    dag3 = db.kursdager(con, k3)[0]
    for pid in s3[:4]:
        db.registrer_oppmote(con, pid, dag3["id"], "zoom", 170)
    db.marker_sendt(con, f"zoomimport:{dag3['id']}", "-", "import")

    # Materiell: presentasjon lastet opp for kurs 3, purring for kurs 2 (frist om 2 dager) og kurs 4 (over frist)
    sharepoint.last_opp("Kurs/PAR-DIG/Presentasjoner", "Dag 1 – Parterapi grunnmodell.pptx", b"demo")
    sharepoint.last_opp("Kurs/EFT-S1/Presentasjoner", "Samling 1 – EFT tilknytningsteori.pdf", b"demo")
    con.execute("INSERT INTO materiell_krav (kurs_id, ansvarlig_navn, ansvarlig_epost, frist) VALUES (?,?,?,?)",
                (k2, "Psykolog A", "psykolog.a@ipr.no", d(2)))
    con.execute("INSERT INTO materiell_krav (kurs_id, ansvarlig_navn, ansvarlig_epost, frist) VALUES (?,?,?,?)",
                (k3, "Psykolog B", "psykolog.b@ipr.no", d(-1)))
    # Personlig kontrakt for spesialistkandidat
    con.execute("INSERT INTO dokument (kurs_id, deltaker_id, type, tittel, url) VALUES (?,?,?,?,?)",
                (k1, 1, "kontrakt", "Utdanningskontrakt EFT 2026–2028", "https://example.sharepoint.com/demo-kontrakt"))
    # Kunnskapsbase – OPPDIKTEDE eksempeltekster. Reelle regler og tekster skrives og godkjennes av adm.
    kunnskap = [
        ("avmelding", "Hva er fristen for å melde seg av?\nKan jeg melde meg av kurset?\nHvordan melder jeg meg av?",
         "[DEMO] Avmelding må skje skriftlig til kursadministrasjonen senest 14 dager før første kursdag. "
         "Ved senere avmelding faktureres full kursavgift, men du kan sende en kollega i ditt sted.", None, None),
        ("praktisk", "Er lunsj inkludert?\nFår vi mat på kurset?",
         "[DEMO] På fysiske kurs i IPRs lokaler er lunsj, kaffe og frukt inkludert. Gi beskjed om allergier i påmeldingen.", None, None),
        ("praktisk", "Hvor kan jeg parkere?\nFinnes det parkering?",
         "[DEMO] IPR har ikke egen parkering. Nærmeste offentlige parkeringshus er [fylles inn av adm].", None, None),
        ("zoom", "Jeg får ikke logget inn på Zoom\nZoom virker ikke\nFår ikke lyd på Zoom",
         "[DEMO] Prøv å åpne lenken i nettleseren i stedet for appen, og sjekk at mikrofon og kamera er tillatt. "
         "Kommer du ikke inn innen 10 minutter, ring kurstelefonen [fylles inn].", None, None),
        ("kursbevis", "Når får jeg kursbevis?\nHvor finner jeg kursbeviset?",
         "Kursbevis lages automatisk når kurset er avsluttet og oppmøte er registrert, og legges på Min side. Du får e-post når det er klart.", None, None),
        ("faktura", "Kan arbeidsgiver betale?\nKan jeg få faktura til jobben?",
         "Ja. Velg «Arbeidsgiver betaler» i påmeldingen og fyll inn organisasjonsnummer og eventuell fakturareferanse. Vi sender EHF hvis det er valgt.", None, None),
        ("eft", "Hvor mange timer har jeg i EFT-løpet?\nTimer spesialistutdanning",
         "Du ser dine dokumenterte timer i EFT-løpet øverst på Min side. Timene summeres automatisk fra registrert oppmøte.", k1, None),
        ("praktisk", "Gammel info om lokaler",
         "[DEMO] Dette svaret er utløpt og skal aldri vises.", None, d(-30)),
    ]
    for kat, sp, sv, kid, til in kunnskap:
        con.execute("INSERT INTO kunnskap (kategori, sporsmal, svar, kurs_id, gyldig_til, godkjent, oppdatert_av) VALUES (?,?,?,?,?,1,'seed')",
                    (kat, sp, sv, kid, til))
    con.commit()

    # Kjor daglig jobb for i dag: sveiper (bekreftelser+faktura), Zoom, innkallinger, kursbevis, purring
    daglig.kjor(Kjoring(con, idag=i))
    print(f"\nDemo klar. Database: {config.DB_STI}")


if __name__ == "__main__":
    main()
