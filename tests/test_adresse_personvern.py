"""Deltakerens private adresse: personvern, anonymisering, migrering 14 og vakt mot nye veier inn uten adresse.

  * Adressen er en personopplysning (ikke sensitiv etter art. 9): aldri i hendelseslogg, applikasjonslogg, e-post
    (bekreftelse, påminnelser, kvittering, kursbevis) eller CSV-eksport/deltakerliste. Den vises bare for administratorer.
  * Anonymisering (retten til sletting) tømmer adressen på personen og fakturaadressen på påmeldingene.
  * Migrering 14 (deltaker_adresse) legger til tre tomme kolonner og virker på både tom og eksisterende database.
  * En vakt sørger for at hver vei inn som registrerer en påmelding også tar med adressen.
Alle testdata er fiktive."""
import ast
import csv
import io
import logging
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from adressehjelp import ADRESSE, SPORBAR_ADRESSE
from kurs import config, daglig, db, deltakerliste, hendelseslogg, kursbevis, migreringer, seed_demo
from kurs import skjemafelt as sf
from kurs.kjoring import Kjoring

pytestmark = pytest.mark.uten_standardadresse   # registrerer bevisst uten adresse (se conftest.py)

ROT = Path(__file__).resolve().parent.parent
EPOST = "ola.nordmann@eksempel.no"
SPOR = ("sporbarveien", "sporbarby", "4711")           # fra SPORBAR_ADRESSE (små bokstaver)


@pytest.fixture(autouse=True)
def _demo(monkeypatch):
    monkeypatch.setattr(config, "DEMO", True)


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    monkeypatch.setattr(config, "BASE_URL", "https://kurs.eksempel.no")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _klient():
    from kurs.web import app as webapp
    return webapp.app.test_client()


def _admin():
    k = _klient()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def _kurs(con, kode="P1", frem=6, **kw):
    """Et kurs som starter om `frem` dager (6 dager gir «uke før»-påminnelsen i morgenjobben)."""
    start = date.today() + timedelta(days=frem)
    felter = {"navn": "Eksempelkurs", "type": "fysisk", "sted": "Eksempelsted", "pris_nok": 4500,
              "fakturering": "person", "betaling": "samlet", "kapasitet": 10, **kw}
    kid = db.opprett_kurs(con, kode=kode, datoer=[start.isoformat(), (start + timedelta(days=1)).isoformat()],
                          sharepoint_mappe=f"K/{kode}", **felter)
    con.commit()
    return kid


def _sendt_tekst(con) -> str:
    """ALT som er sendt på e-post: filene i utboksen og kopiene i e-posthistorikken (emne og innhold)."""
    filer = " ".join(f.read_text(encoding="utf-8") for f in sorted(config.UTBOKS.glob("*.html"))) \
        if config.UTBOKS.exists() else ""
    kopier = " ".join((r["html"] or "") + " " + (r["emne"] or "") + " " + (r["til"] or "")
                      for r in con.execute("SELECT html, emne, til FROM sendt_epost"))
    return (filer + " " + kopier).lower()


def _alle_tabeller_utenom_deltaker_og_paamelding(con) -> str:
    """Alt i databasen som IKKE skal inneholde adressen: hendelser, utsendingslogg, e-postkopier, dokumenter (kursbevis)."""
    tabeller = ("hendelse", "utsending_logg", "sendt_epost", "dokument", "dokument_innhold", "firmapaamelding",
                "firmapaamelding_rad", "faktura", "faktura_forsok", "sensitivt", "import_forhaandsvisning")
    return "\n".join(str([tuple(r) for r in con.execute(f"SELECT * FROM {t}")]) for t in tabeller
                     if db.har_tabell(con, t)).lower()


def _registrer(con, kode="P1", epost=EPOST, **over):
    r = _klient().post(f"/kurs/{kode}", data={"fornavn": "Ola", "etternavn": "Nordmann", "epost": epost,
                                              "samtykke": "on", "betaler": "person", **SPORBAR_ADRESSE, **over})
    assert r.status_code == 200, r.get_data(as_text=True)[:400]


# ============================ aldri i logg, e-post eller eksport ============================

def test_adressen_lekker_ikke_til_hendelser_epost_eller_utsendingslogg(con, caplog):
    _kurs(con)
    with caplog.at_level(logging.DEBUG):
        _registrer(con)                                                    # bekreftelse + faktura (privat betaler)
        _registrer(con, epost="firma@eksempel.no", betaler="organisasjon", org_nr="999900003")
        daglig.kjor(Kjoring(con, idag=date.today()))                       # morgenjobben: «uke før»-påminnelser m.m.
        con.commit()
    sendt = _sendt_tekst(con)
    assert sendt.strip() and con.execute("SELECT COUNT(*) FROM sendt_epost").fetchone()[0] >= 2   # det er e-post å lete i
    assert "ola" in sendt                                                    # navnet står i e-posten (adressen gjør det ikke)
    for spor in SPOR:
        assert spor not in sendt, f"«{spor}» står i en sendt e-post"
        assert spor not in _alle_tabeller_utenom_deltaker_og_paamelding(con), f"«{spor}» står i loggen/utsendingene"
        assert spor not in caplog.text.lower(), f"«{spor}» står i applikasjonsloggen"
    # ... og adressen ligger faktisk på personen (testen leter ikke etter noe som ikke finnes)
    assert con.execute("SELECT adresse FROM deltaker WHERE epost=?", (EPOST,)).fetchone()[0] == SPORBAR_ADRESSE["adresse"]


def test_adressen_er_ikke_i_kursbevis(con):
    kid = _kurs(con, "KB", frem=-10)
    pid, _ = db.meld_paa(con, kid, epost=EPOST, fornavn="Ola", etternavn="Nordmann", deltaker=dict(SPORBAR_ADRESSE))
    db.registrer_oppmote(con, pid, db.kursdager(con, kid)[0]["id"], "manuell")
    db.registrer_oppmote(con, pid, db.kursdager(con, kid)[1]["id"], "manuell")
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()
    kursbevis.kjor(Kjoring(con, idag=date.today()))
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM dokument WHERE type='kursbevis'").fetchone()[0] >= 1      # et kursbevis finnes
    alt = _alle_tabeller_utenom_deltaker_og_paamelding(con) + " " + _sendt_tekst(con)
    for spor in SPOR:
        assert spor not in alt, spor


def test_adressen_er_ikke_med_i_noen_csv_eksport_eller_i_deltakerlisten(con):
    kid = _kurs(con)
    _registrer(con)
    admin = _admin()
    pid = con.execute("SELECT id FROM paamelding").fetchone()[0]
    svar = [
        admin.get(f"/admin/kurs/{kid}/deltakere.csv"),
        admin.post(f"/admin/kurs/{kid}/deltakere/eksporter-valgte.csv", data={"paamelding_id": [pid]}),
        admin.get("/admin/rapporter/deltakere.csv"),
        admin.get("/admin/rapporter/kurs.csv"),
        admin.get("/admin/rapporter/okonomi.csv"),
        # Deltakerlisten med ALLE kolonner som kan velges
        admin.get(f"/admin/kurs/{kid}/deltakerliste.csv?valgt=1&status=alle&"
                  + "&".join(f"kol={k.nokkel}" for k in deltakerliste.katalog() if not k.kun_utskrift)),
        admin.get(f"/admin/kurs/{kid}/deltakerliste?valgt=1&status=alle&"
                  + "&".join(f"kol={k.nokkel}" for k in deltakerliste.katalog())),
        admin.get(f"/admin/kurs/{kid}/deltakere"),
        admin.get(f"/admin/kurs/{kid}/allergiliste"),
    ]
    for r in svar:
        assert r.status_code == 200, r.request.path
        tekst = r.get_data(as_text=True).lower()
        for spor in SPOR:
            assert spor not in tekst, f"«{spor}» i {r.request.path}"
    # Katalogen over valgbare kolonner har ingen adressekolonne (adressen er heller ikke en valgbar kolonne)
    assert not {"adresse", "postnr", "poststed"} & {k.nokkel for k in deltakerliste.katalog()}
    hode = admin.get(f"/admin/kurs/{kid}/deltakere.csv").get_data(as_text=True).lstrip("﻿").splitlines()[0]
    kolonner = next(csv.reader(io.StringIO(hode), delimiter=";"))
    assert not {"adresse", "postnr", "poststed"} & {h.lower() for h in kolonner}


def test_eksportlogging_har_bare_kolonnenavn_og_antall(con):
    kid = _kurs(con)
    _registrer(con)
    _admin().get(f"/admin/kurs/{kid}/deltakere.csv")
    con.commit()
    for spor in SPOR:
        assert spor not in _alle_tabeller_utenom_deltaker_og_paamelding(con)


def test_adressen_vises_for_administrator_i_deltakervinduet(con):
    kid = _kurs(con)
    _registrer(con)
    pid = con.execute("SELECT id FROM paamelding").fetchone()[0]
    html = _admin().get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
    for verdi in SPORBAR_ADRESSE.values():
        assert f'value="{verdi}"' in html


def test_hendelseslogg_har_ingen_tekst_der_adressen_kan_staa(con):
    """Alle lesbare tekster for personfeltene bruker feltnavn (aldri verdier), og adressefeltene har lesbare navn."""
    assert hendelseslogg._PERSONFELT["adresse"] == "Adresse"
    assert hendelseslogg._PERSONFELT["postnr"] == "Postnummer" and hendelseslogg._PERSONFELT["poststed"] == "Poststed"


# ============================ anonymisering ============================

def test_anonymisering_toemmer_adressen_og_fakturaadressen(con):
    kid = _kurs(con, "AN", frem=-20)
    pid, _ = db.meld_paa(con, kid, epost=EPOST, fornavn="Ola", etternavn="Nordmann", deltaker=dict(SPORBAR_ADRESSE))
    db.sett_paamelding_status(con, pid, "avmeldt", aktor="admin:test")
    did = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pid,)).fetchone()[0]
    assert dict(con.execute("SELECT faktura_adresse, faktura_postnr, faktura_sted FROM paamelding WHERE id=?",
                            (pid,)).fetchone()) != {"faktura_adresse": None, "faktura_postnr": None, "faktura_sted": None}
    db.anonymiser_deltaker(con, did, aktor="admin:test")
    con.commit()
    d = con.execute("SELECT adresse, postnr, poststed FROM deltaker WHERE id=?", (did,)).fetchone()
    p = con.execute("SELECT faktura_adresse, faktura_postnr, faktura_sted FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert tuple(d) == (None, None, None) and tuple(p) == (None, None, None)
    alt = "\n".join(str([tuple(r) for r in con.execute(f"SELECT * FROM {t}")]) for t in ("deltaker", "paamelding", "hendelse"))
    for spor in SPOR:
        assert spor not in alt.lower()


# ============================ migrering 14 ============================

def test_migrering_14_finnes_sist_og_ny_database_har_kolonnene_sist_i_tabellen(con):
    assert [m[:2] for m in migreringer.MIGRERINGER][13] == (14, "deltaker_adresse")
    assert migreringer.KODEVERSJON >= 14 and migreringer.gjeldende_versjon(con) == migreringer.KODEVERSJON
    assert db.kolonner(con, "deltaker")[-3:] == ["adresse", "postnr", "poststed"]
    for tabell in ("schema.sql", "schema_postgres.sql"):                     # begge skjemafilene beskriver kolonnene
        tekst = (ROT / "kurs" / tabell).read_text(encoding="utf-8")
        deltaker = re.search(r"CREATE TABLE IF NOT EXISTS deltaker \((.*?)^\);", tekst, re.S | re.M).group(1)
        for kolonne in ("adresse", "postnr", "poststed"):
            assert re.search(rf"^\s+{kolonne}\s+TEXT,?\s*(--.*)?$", deltaker, re.M), (tabell, kolonne)
    assert "14" in (ROT / "MIGRATIONS.md").read_text(encoding="utf-8") and "deltaker_adresse" in (ROT / "MIGRATIONS.md").read_text(encoding="utf-8")


def test_migrering_14_legger_kolonnene_til_i_en_eksisterende_database_uten_datatap(con):
    kid = _kurs(con, "MG")
    pid, _ = db.meld_paa(con, kid, epost=EPOST, fornavn="Eldre", etternavn="Person",
                         deltaker={"telefon": "90011222", "arbeidssted": "Klinikken"},
                         paamelding={"faktura_adresse": "Gammelveien 1", "faktura_postnr": "5000", "faktura_sted": "Bergen"})
    con.commit()
    for kolonne in ("adresse", "postnr", "poststed"):                          # slik databasen så ut før migrering 14
        con.execute(f"ALTER TABLE deltaker DROP COLUMN {kolonne}")
    con.execute("DELETE FROM schema_versjon WHERE versjon >= 14")
    con.commit()
    assert not db.har_kolonne(con, "deltaker", "adresse") and migreringer.gjeldende_versjon(con) == 13
    with pytest.raises(migreringer.VersjonsFeil):
        migreringer.kontroller(con)                                            # appen nekter å kjøre før migreringen er kjørt
    assert migreringer.kjor_manglende(con) == list(range(14, migreringer.KODEVERSJON + 1))      # senere migreringer (kursside, ekstradeltaker …) kjøres også
    assert db.kolonner(con, "deltaker")[-3:] == ["adresse", "postnr", "poststed"]
    d = con.execute("SELECT * FROM deltaker WHERE epost=?", (EPOST,)).fetchone()
    assert (d["fornavn"], d["telefon"], d["arbeidssted"]) == ("Eldre", "90011222", "Klinikken")     # eksisterende data urørt
    assert (d["adresse"], d["postnr"], d["poststed"]) == (None, None, None)                         # ingen automatisk adresse
    p = con.execute("SELECT faktura_adresse FROM paamelding WHERE id=?", (pid,)).fetchone()
    assert p["faktura_adresse"] == "Gammelveien 1"                                                   # fakturaadressen røres ikke
    assert migreringer.kjor_manglende(con) == []                                                     # idempotent
    migreringer.kontroller(con)
    # Den eldre personen kan få adresse ved neste påmelding
    db.meld_paa(con, _kurs(con, "MG2"), epost=EPOST, fornavn="Eldre", etternavn="Person", deltaker=dict(ADRESSE))
    assert con.execute("SELECT adresse FROM deltaker WHERE epost=?", (EPOST,)).fetchone()[0] == ADRESSE["adresse"]


def test_migrering_14_er_idempotent_selv_om_kolonnene_finnes(con):
    con.execute("DELETE FROM schema_versjon WHERE versjon >= 14")
    con.commit()
    assert migreringer.kjor_manglende(con) == list(range(14, migreringer.KODEVERSJON + 1))
    assert db.kolonner(con, "deltaker").count("adresse") == 1


# ============================ eksempeldata ============================

def test_demopersonene_har_oppdiktede_gyldige_adresser():
    assert len(seed_demo.PERSONER) >= 8
    for fornavn, etternavn, epost, arbeidssted, (adresse, postnr, poststed) in seed_demo.PERSONER:
        assert sf.valider_adresse({"adresse": adresse, "postnr": postnr, "poststed": poststed}) == {}, epost
        assert epost.endswith("@example.no")


# ============================ vakt: hver vei inn tar med adressen ============================

def _deltaker_argumenter(fil: Path) -> list[str]:
    """`deltaker=`-argumentet i hvert db.meld_paa-kall, som kode (ast.unparse tar aldri med kommentarer). Et kall uten
    `deltaker=` gir en tom tekst."""
    ut = []
    for node in ast.walk(ast.parse(fil.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "meld_paa":
            kw = next((k for k in node.keywords if k.arg == "deltaker"), None)
            ut.append(ast.unparse(kw.value) if kw else "")
    return ut


def test_hver_vei_inn_som_registrerer_en_paamelding_tar_med_adressen():
    """Adressen er påkrevd i ALLE veier inn. Legges det til en ny vei som kaller db.meld_paa uten å ta med adressen
    (skjemafelt.rens_adresse eller ADRESSEFELT i selve `deltaker=`-argumentet), feiler denne testen - og utvikleren må ta
    stilling til adressen først. Vakten leser argumentet, ikke kommentarene og ikke «faktura_adresse» andre steder i kallet."""
    kall = {f: _deltaker_argumenter(ROT / f) for f in ("kurs/web/app.py", "kurs/import_deltakere.py")}
    antall = {f: len(k) for f, k in kall.items()}
    assert antall == {"kurs/web/app.py": 4, "kurs/import_deltakere.py": 1}, (
        "en ny vei inn er lagt til (eller fjernet): sjekk at den krever og lagrer deltakerens adresse, og oppdater tallet")
    for fil, argumenter in kall.items():
        for a in argumenter:
            assert "rens_adresse" in a or "ADRESSEFELT" in a, f"{fil}: deltaker= mangler adressen:\n{a[:300]}"


def test_vakten_ser_forskjell_paa_adressen_i_deltaker_og_ordet_i_en_kommentar_eller_fakturaadresse(tmp_path):
    """Selve vakten: en kilde som bare NEVNER adresse (kommentar, faktura_adresse) uten å sende den i deltaker= slipper ikke
    gjennom, mens en som sender den gjør det."""
    kilde = tmp_path / "vei.py"
    kilde.write_text(
        "def a(db, con):\n"
        "    # adresse er påkrevd\n"
        "    db.meld_paa(con, 1, epost='a@x.no', deltaker={'telefon': '1'}, paamelding={'faktura_adresse': 'x'})\n"
        "def b(db, con, sf, f):\n"
        "    db.meld_paa(con, 1, epost='a@x.no', deltaker={'telefon': '1'} | sf.rens_adresse(f))\n"
        "def c(db, con):\n"
        "    db.meld_paa(con, 1, epost='a@x.no')\n", encoding="utf-8")
    a, b, c = _deltaker_argumenter(kilde)
    assert "rens_adresse" not in a and "ADRESSEFELT" not in a and "adresse" not in a
    assert "rens_adresse" in b
    assert c == ""


# ============================ oppdateringer etter kritikerrunden ============================

def test_anonymisering_sletter_csv_forhaandsvisningen_som_har_personen_men_ikke_andres(con):
    """import_forhaandsvisning har navn, e-post og adresse i opptil 30 minutter. Slettes personen, skal ikke en ventende
    forhåndsvisning med henne stå igjen - men andres forhåndsvisninger røres ikke."""
    gammelt = _kurs(con, "AN", frem=-20)
    a, b = _kurs(con, "AN2"), _kurs(con, "AN3")
    pid, _ = db.meld_paa(con, gammelt, epost=EPOST, fornavn="Ola", etternavn="Nordmann", deltaker=dict(SPORBAR_ADRESSE))
    db.sett_paamelding_status(con, pid, "avmeldt", aktor="admin:test")
    con.commit()
    did = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pid,)).fetchone()[0]
    k = _admin()
    adr = ";".join(SPORBAR_ADRESSE.values())
    for kurs, epost, navn in ((a, EPOST, "Ola;Nordmann"), (b, "kari@eksempel.no", "Kari;Test")):
        fil = f"Fornavn;Etternavn;E-post;Adresse;Postnr;Poststed\n{navn};{epost};{adr}\n".encode("utf-8-sig")
        assert k.post(f"/admin/kurs/{kurs}/deltakere/importer", data={"fil": (io.BytesIO(fil), "i.csv")},
                      content_type="multipart/form-data").status_code == 200
    assert con.execute("SELECT COUNT(*) FROM import_forhaandsvisning").fetchone()[0] == 2
    ut = db.anonymiser_deltaker(con, did, aktor="admin:test")
    con.commit()
    assert ut["importforhandsvisninger"] == 1
    igjen = con.execute("SELECT kurs_id, rader_json FROM import_forhaandsvisning").fetchall()
    assert [r["kurs_id"] for r in igjen] == [b] and "kari@eksempel.no" in igjen[0]["rader_json"]
    alt = "\n".join(str([tuple(r) for r in con.execute(f"SELECT * FROM {t}")]) for t in ("import_forhaandsvisning", "hendelse"))
    assert EPOST not in alt.lower()
    assert "nordmann" not in con.execute("SELECT rader_json FROM import_forhaandsvisning").fetchone()[0].lower()


def test_anonymisering_sletter_ogsaa_en_forhaandsvisning_som_ikke_kan_leses(con):
    kid = _kurs(con)
    _admin().get("/admin/")                                    # sørger for at det finnes en administrator
    admin_id = con.execute("SELECT id FROM admin_bruker LIMIT 1").fetchone()[0]
    con.execute("INSERT INTO import_forhaandsvisning (token, kurs_id, admin_id, rader_json, resultater_json, utloper) "
                "VALUES ('t1', ?, ?, 'ikke json', '[]', '2999-01-01T00:00:00')", (kid, admin_id))
    pid, _ = db.meld_paa(con, _kurs(con, "AN", frem=-20), epost=EPOST, fornavn="Ola", etternavn="Nordmann")
    db.sett_paamelding_status(con, pid, "avmeldt", aktor="admin:test")
    did = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pid,)).fetchone()[0]
    assert db.anonymiser_deltaker(con, did, aktor="admin:test")["importforhandsvisninger"] == 1


def test_personrapporten_viser_adressen_for_innsyn_men_ingen_liste_eller_csv_gjor(con):
    """Innsyn (art. 15): personrapporten viser alt vi har om personen, også den private adressen. Lister og CSV har den ikke."""
    kid = _kurs(con)
    _registrer(con)
    did = con.execute("SELECT id FROM deltaker").fetchone()[0]
    admin = _admin()
    rapport = admin.get(f"/admin/rapporter/deltaker/{did}").get_data(as_text=True)
    assert "Privat adresse:" in rapport
    for verdi in SPORBAR_ADRESSE.values():
        assert verdi in rapport
    for url in ("/admin/rapporter/deltakere", "/admin/rapporter/deltakere.csv", f"/admin/kurs/{kid}/deltakere.csv"):
        tekst = admin.get(url).get_data(as_text=True).lower()
        for spor in SPOR:
            assert spor not in tekst, (url, spor)


def test_personrapporten_sier_fra_naar_adressen_mangler_og_viser_den_ikke_etter_sletting(con):
    kid = _kurs(con, "AN", frem=-20)
    pid, _ = db.meld_paa(con, kid, epost=EPOST, fornavn="Ola", etternavn="Nordmann")               # ingen adresse (eldre person)
    did = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pid,)).fetchone()[0]
    con.commit()
    assert "Privat adresse: ikke registrert" in _admin().get(f"/admin/rapporter/deltaker/{did}").get_data(as_text=True)
    db.oppdater_deltaker(con, did, dict(SPORBAR_ADRESSE))
    db.sett_paamelding_status(con, pid, "avmeldt", aktor="admin:test")
    db.anonymiser_deltaker(con, did, aktor="admin:test")
    con.commit()
    html = _admin().get(f"/admin/rapporter/deltaker/{did}").get_data(as_text=True)
    assert "Privat adresse: ikke registrert" in html and "Sporbar" not in html


def test_migrering_14_lager_tekstkolonner_som_beholder_foranstilt_null_i_postnummeret(con):
    """Oppgraderte databaser får kolonnene fra migreringen (ikke fra schema.sql): en heltallskolonne ville lagret postnummer
    «0150» som 150."""
    for kolonne in ("adresse", "postnr", "poststed"):
        con.execute(f"ALTER TABLE deltaker DROP COLUMN {kolonne}")
    con.execute("DELETE FROM schema_versjon WHERE versjon >= 14")
    con.commit()
    assert migreringer.kjor_manglende(con) == list(range(14, migreringer.KODEVERSJON + 1))
    if not db.er_postgres(con):
        typer = {r["name"]: r["type"] for r in con.execute("PRAGMA table_info(deltaker)")}
        assert [typer[k] for k in ("adresse", "postnr", "poststed")] == ["TEXT", "TEXT", "TEXT"]
    db.meld_paa(con, _kurs(con), epost=EPOST, fornavn="Eldre", etternavn="Person", deltaker=dict(ADRESSE))
    assert con.execute("SELECT postnr FROM deltaker WHERE epost=?", (EPOST,)).fetchone()[0] == "0150"      # ikke 150
