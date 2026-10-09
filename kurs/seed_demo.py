"""Nullstiller databasen og legger inn OPPDIKTEDE demodata. Datoer er relative til i dag,
slik at demoen alltid viser: et kurs som gaar i dag (QR), et som starter om 6 dager (ukefor-mail),
et avsluttet kurs med kursbevis, og et fullt kurs med venteliste.

    python -m kurs.seed_demo
"""
import shutil
import struct
import zlib
from datetime import date, timedelta

from . import config, daglig, db, deltakerside, firmaopplysninger, sidelager
from . import sideinnhold as si
from .integrasjoner import brreg
from .kjoring import Kjoring

PERSONER = [   # (fornavn, etternavn, e-post, arbeidssted, (adresse, postnr, poststed))
    # Alle adresser er OPPDIKTET (eksempelveier). Deltakerens private adresse er påkrevd for alle deltakere.
    ("Kari", "Nordmann", "kari.nordmann@example.no", "Familievernkontoret Bergen", ("Eksempelveien 1", "5003", "Bergen")),
    ("Ola", "Hansen", "ola.hansen@example.no", "DPS Nordfjord", ("Prøvegata 12", "6770", "Nordfjordeid")),
    ("Aisha", "Rahman", "aisha.rahman@example.no", "Privat praksis", ("Testbakken 3", "0150", "Oslo")),
    ("Jonas", "Berg", "jonas.berg@example.no", "BUP Oslo", ("Demoveien 22", "0360", "Oslo")),
    ("Ingrid Marie", "Solheim", "ingrid.solheim@example.no", "Bergen kommune", ("Eksempelstien 5", "5006", "Bergen")),
    ("Mateusz", "Nowak", "mateusz.nowak@example.no", "DPS Vest", ("Fiktivgaten 8", "5020", "Bergen")),
    ("Silje", "Dahl", "silje.dahl@example.no", "Privat praksis", ("Prøveveien 40", "4006", "Stavanger")),
    ("Eirik", "Lunde", "eirik.lunde@example.no", "Helse Fonna", ("Demogaten 17", "5527", "Haugesund")),
]


def _demo_png() -> bytes:
    """En 1x1 piksel PNG (blå), laget i kode - ingen ekte bildefil."""
    def bit(navn, d):
        return struct.pack(">I", len(d)) + navn + d + struct.pack(">I", zlib.crc32(navn + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + bit(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + bit(b"IDAT", zlib.compress(b"\x00\x1f\x5c\x73")) + bit(b"IEND", b""))


_DEMO_PDF = (b"%PDF-1.1\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
             b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 100]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")


def _demo_kursside(con, parterapi_id: int, eft_s2_id: int) -> None:
    """Kurssiden i demoen: en PUBLISERT side for det digitale kurset som har kursdag i dag (Kari Nordmann logger inn og lander her), og
    et UTKAST (ikke publisert) for EFT samling 2. Alt er oppdiktet: to små demofiler laget i kode, gruppetabell med oppdiktede
    fornavn og forbokstav, og en oppdiktet arbeidsadresse i Kontakt-blokken."""
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (parterapi_id,)).fetchone()
    dager = deltakerside.kursdager_for(con, kurs)
    pdf_id, _ = sidelager.lagre_fil(con, parterapi_id, "Dag 1 – Parterapi grunnmodell.pdf", _DEMO_PDF, "system")
    png_id, _ = sidelager.lagre_fil(con, parterapi_id, "Skjema for øvelsen.png", _demo_png(), "system")
    program = si.program_fra_kursdager(dager)
    for dag, punkter in zip(program, (
            [("12:00", "12:15", "Velkommen og oppsummering", "", "Kursleder", False), ("12:15", "13:30", "Grunnmodellen i parterapi", "Zoom", "Kursleder", False)],
            [("12:00", "12:10", "Innsjekk og dagens plan", "", "Kursleder", False), ("12:10", "13:10", "Øvelse i grupper", "Grupperom", "Veileder", False),
             ("13:10", "13:25", "Pause", "", "", True), ("13:25", "14:45", "Samtale om case", "Zoom", "Kursleder", False)],
            [("12:00", "13:30", "Oppsummering og veien videre", "Zoom", "Kursleder", False)])):
        dag["punkter"] = [{"fra": f, "til": t, "tema": tema, "sted": sted, "hvem": hvem, "pause": pause} for f, t, tema, sted, hvem, pause in punkter]
    dok = si.tomt_dokument()
    dok.update(tittel="Parterapi i praksis", ingress="Program, presentasjoner, grupper og litteratur for det digitale fordypningskurset.",
               rom="Rom 3, 2. etasje", melding={"tekst": "Husk å ta med egne case-notater (anonymisert).", "niva": "info"})
    dok["blokker"] = [
        {"id": "b_viktig", "type": "viktig", "tittel": "Praktisk", "skjult": False, "vis_fra": None, "i_meny": True,
         "data": {"niva": "viktig", "tekst": "Logg deg på Zoom fem minutter før start.\n\nSkriv deg inn i deltakerlisten når du har kommet."}},
        {"id": "b_program", "type": "program", "tittel": "Program", "skjult": False, "vis_fra": None, "i_meny": True, "data": {"dager": program}},
        {"id": "b_filer", "type": "filer", "tittel": "Presentasjoner og dokumenter", "skjult": False, "vis_fra": None, "i_meny": True,
         "data": {"filer": [{"fil_id": pdf_id, "tittel": "Dag 1 – Parterapi grunnmodell", "gruppe": dager[0]["id"], "synlig_fra": None},
                            {"fil_id": png_id, "tittel": "Skjema for øvelsen", "gruppe": None, "synlig_fra": None}],
                  "vis_kommende": True}},
        {"id": "b_lenker", "type": "lenker", "tittel": "Litteratur og lenker", "skjult": False, "vis_fra": None, "i_meny": True,
         "data": {"lenker": [{"tittel": "Zoom-møtet", "url": "", "tekst": "Samme lenke alle dagene", "nivaa": None, "kilde": "zoom"},
                             {"tittel": "Innføring i parterapi (artikkel)", "url": "https://example.com/parterapi-artikkel", "tekst": "Leses før dag 1",
                              "nivaa": "obligatorisk", "kilde": None},
                             {"tittel": "Videre lesing", "url": "https://example.com/videre-lesing", "tekst": "", "nivaa": "anbefalt", "kilde": None}]}},
        {"id": "b_grupper", "type": "tabell", "tittel": "Gruppeinndeling", "skjult": False, "vis_fra": None, "i_meny": True,
         "data": {"kolonner": ["Gruppe", "Deltakere", "Rom", "Veileder"],
                  "rader": [["Gruppe 1", "Kari N., Ola H.", "Grupperom 1", "Veileder A"], ["Gruppe 2", "Aisha R., Mateusz N.", "Grupperom 2", "Veileder B"]]}},
        {"id": "b_kontakt", "type": "kontakt", "tittel": "Kontakt", "skjult": False, "vis_fra": None, "i_meny": True,
         "data": {"personer": [{"navn": "Kursadministrasjonen", "rolle": "Praktiske spørsmål", "telefon": "000 00 000", "epost": "kurs@ipr.example"}]}},
    ]
    filer, dagids = sidelager.valideringsgrunnlag(con, parterapi_id)
    dok, _merknader = si.valider(dok, fil_ider=filer, kursdag_ider=dagids)
    versjon = sidelager.lagre_utkast(con, parterapi_id, dok, 0, "system")
    sidelager.publiser(con, parterapi_id, versjon, "system")
    # Utkast (ikke publisert) for EFT samling 2: standardmalen fra kursdatoene
    eft = con.execute("SELECT * FROM kurs WHERE id=?", (eft_s2_id,)).fetchone()
    utkast = si.standardmal(deltakerside.kursdager_for(con, eft))
    filer, dagids = sidelager.valideringsgrunnlag(con, eft_s2_id)
    sidelager.lagre_utkast(con, eft_s2_id, si.valider(utkast, fil_ider=filer, kursdag_ider=dagids)[0], 0, "system")


def main():
    if config.MODUS == "prod":
        raise SystemExit("Nekter å kjøre seed_demo i prod.")
    if config.DB_STI.exists():
        config.DB_STI.unlink()
    for mappe in (config.UTBOKS, config.ROT / "data" / "kursbevis"):
        shutil.rmtree(mappe, ignore_errors=True)

    con = db.koble()
    db.init(con)
    i = date.today()
    d = lambda n: (i + timedelta(days=n)).isoformat()  # noqa: E731
    standardadmin = con.execute("SELECT id FROM admin_bruker WHERE brukernavn=?", (config.ADMIN_BRUKERNAVN,)).fetchone()["id"]

    # 1) Avsluttet EFT-samling (for 3 uker siden) – gir kursbevis + timer i spesialistlop
    k1 = db.opprett_kurs(con, kode="EFT-S1", navn="EFT spesialistutdanning – samling 1", datoer=[d(-22), d(-21)],
                         type="fysisk", sted="IPR, Bergen", pris_nok=14500, spesialistlop="EFT", timer_pr_dag=7,
                         kapasitet=16, status="aktiv",
                         ansvarlig_admin_id=standardadmin)
    # 2) EFT samling 2 – starter om 6 dager (ukefor-mail), 2 dager
    k2 = db.opprett_kurs(con, kode="EFT-S2", navn="EFT spesialistutdanning – samling 2", datoer=[d(6), d(7)],
                         type="fysisk", sted="IPR, Bergen", pris_nok=14500, spesialistlop="EFT", timer_pr_dag=7,
                         kapasitet=16, kursholder_epost="psykolog.a@ipr.no",
                         betaling="deltaker_velger", faktura_dager_for=14,
                         notat="Lunsj er inkludert. Ta med egne case-notater (anonymisert).",
                         ansvarlig_admin_id=standardadmin)
    # 3) Digitalt kurs med kursdag I DAG (QR/kode-demo + Zoom)
    k3 = db.opprett_kurs(con, kode="PAR-DIG", navn="Parterapi i praksis – digitalt fordypningskurs",
                         datoer=[d(-7), d(0), d(7)], type="hybrid", sted="Zoom / IPR Bergen", pris_nok=4900,
                         start_kl="12:00", slutt_kl="15:00", timer_pr_dag=3)
    # 4) Lite kurs som er fullt -> venteliste
    k4 = db.opprett_kurs(con, kode="SUPERV-1", navn="Veiledning i gruppe – introduksjon", datoer=[d(20)],
                         type="fysisk", sted="IPR, Bergen", pris_nok=2500, kapasitet=3)
    # 5) Bedriftskurs fakturert samlet
    db.opprett_kurs(con, kode="BHT-2027", navn="Stressmestring for ledere (bedriftsinternt)", datoer=[d(35)],
                    type="fysisk", sted="Kundens lokaler", pris_nok=0, fakturering="organisasjon")

    def meld(kurs_id, idx, **kw):
        fornavn, etternavn, epost_, arb, (adresse, postnr, poststed) = PERSONER[idx]
        pid, _ = db.meld_paa(con, kurs_id, epost=epost_, fornavn=fornavn, etternavn=etternavn,
                             deltaker={"arbeidssted": arb, "adresse": adresse, "postnr": postnr, "poststed": poststed}, **kw)
        return pid

    # Firmaopplysningene kommer fra (demo)registeret, som alt annet: den oppdiktede virksomheten «EKSEMPEL KOMMUNE»
    org = {"betaler": "organisasjon", "ehf": 1, "faktura_ref": "BK-4471", **firmaopplysninger.paamelding_felter(
        firmaopplysninger.Oppslag(brreg.DEMO_ENHETER[1].orgnr, firmaopplysninger.FUNNET, brreg.DEMO_ENHETER[1]))}
    s1 = [meld(k1, n) for n in range(6)]
    s2 = []
    for n in range(6):
        # Kari (n=0) velger å dele opp betalingen, Ingrid (n=4) faktureres via arbeidsgiver
        s2.append(meld(k2, n, paamelding={**org} if n == 4 else ({"betaling": "per_samling"} if n == 0 else None),
                       sensitivt={"allergier": "Glutenfri" if n == 1 else None,
                                  "tilrettelegging": "Rullestoltilgang" if n == 3 else None}))
    # Demo av Ekstradeltaker: er på kurset, men tar ikke plass og teller ikke som påmeldt. Får ikke bekreftelse eller faktura
    # automatisk (holdt tilbake), og deltar bare på samling 1 av de to (hver dag er en samling på dette kurset).
    samlinger_k2 = [s["id"] for s in db.kursets_samlinger(con, k2)]
    db.sett_paamelding_status(con, s2[5], "ekstradeltaker", aktor="system", samlinger=samlinger_k2[:1] if len(samlinger_k2) > 1 else None)
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

    # Personlig kontrakt for spesialistkandidat
    con.execute("INSERT INTO dokument (kurs_id, deltaker_id, type, tittel, url) VALUES (?,?,?,?,?)",
                (k1, 1, "kontrakt", "Utdanningskontrakt EFT 2026–2028", "https://example.com/demo-kontrakt"))
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
    _demo_kursside(con, k3, k2)
    con.commit()

    # Kjor daglig jobb for i dag: sveiper (bekreftelser+faktura), Zoom, innkallinger, kursbevis
    daglig.kjor(Kjoring(con, idag=i))
    print(f"\nDemo klar. Database: {config.DB_STI}")


if __name__ == "__main__":
    main()
