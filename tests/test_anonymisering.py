"""Retten til sletting (GDPR art. 17): anonymisering av en deltaker i hele påmeldingssystemet."""
import json
from datetime import date

import pytest

from kurs import config, db, okonomi


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _admin(brukernavn=None, passord=None):
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    r = k.post("/admin/logg-inn", data={"brukernavn": brukernavn or config.ADMIN_BRUKERNAVN,
                                        "passord": passord or config.ADMIN_PASSORD})
    assert r.status_code == 302
    return k


EPOST = "Mona.Person@Eksempel.no"


@pytest.fixture
def person(con):
    """En deltaker med spor i alle tabeller som kan inneholde personopplysninger. Kurset er avsluttet."""
    kid = db.opprett_kurs(con, kode="GDPR-2030", navn="Personvernkurs", datoer=["2030-03-01"], pris_nok=3000,
                          type="fysisk", sharepoint_mappe="K/G")
    felt_id = db.opprett_ekstrafelt(con, kid, {"type": "tekst", "label": "Hvor jobber du?"}, 4)
    pid, _ = db.meld_paa(con, kid, epost=EPOST, fornavn="Mona", etternavn="Person", svar={felt_id: "Mona-klinikken"},
                         deltaker={"telefon": "90011222", "arbeidssted": "Klinikken", "yrkestittel": "Psykolog",
                                   "hpr_nr": "1234567", "adresse": "Sporbarveien 4711", "postnr": "4711",
                                   "poststed": "Sporbarby"},
                         paamelding={"faktura_adresse": "Gate 1", "faktura_postnr": "5000", "faktura_sted": "Bergen",
                                     "intern_kommentar": "Ring henne", "faktura_ref": "Mona"},
                         sensitivt={"allergier": "Nøtter"})
    did = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pid,)).fetchone()[0]
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    db.registrer_oppmote(con, pid, db.kursdager(con, kid)[0]["id"], "manuell")
    db.sett_inn(con, "INSERT INTO faktura (paamelding_id, faktura_nr, belop_nok, status, opprettet) VALUES (?,?,?,?,?)",
                (pid, "7001", 3000, "sendt", "2030-02-01 10:00:00"))
    dok = db.sett_inn(con, "INSERT INTO dokument (kurs_id, deltaker_id, type, tittel, url) VALUES (?,?,?,?,?)",
                      (kid, did, "kursbevis", "Kursbevis – Personvernkurs", "db:"))
    con.execute("INSERT INTO dokument_innhold (dokument_id, innhold) VALUES (?, '<h1>Mona Person</h1>')", (dok,))
    con.execute("INSERT INTO innlogging_token (token, deltaker_id, utloper) VALUES ('tok', ?, '2030-01-01')", (did,))
    db.marker_sendt(con, f"kurs:{kid}", EPOST.lower(), "bekreftelse")
    kopi = db.lagre_epostkopi(con, nokkel=f"kurs:{kid}", type_="bekreftelse", til=EPOST.lower(), fra=config.AVSENDER_EPOST,
                              sendt_av="system", emne="Bekreftelse", html="<p>Hei Mona Person, 90011222</p>",
                              paamelding_id=pid, kurs_id=kid)
    db.sett_epostkopi_status(con, kopi, "sendt")
    fid = db.sett_inn(con, """INSERT INTO firmapaamelding (kurs_id, innsendingsnokkel, kvittering_token, kontakt_navn,
                              kontakt_epost, kontakt_telefon, firmanavn) VALUES (?,?,?,?,?,?,?)""",
                      (kid, "n1", "k1", "Mona Person", EPOST.upper(), "90011222", "Klinikken AS"))
    con.execute("INSERT INTO firmapaamelding_rad (firmapaamelding_id, navn, epost, paamelding_id) VALUES (?,?,?,?)",
                (fid, "Mona Person", EPOST, pid))
    con.execute("INSERT INTO henvendelse (deltaker_id, epost, sporsmal, status) VALUES (?,?,?,'til_adm')",
                (did, EPOST, "Jeg har nøtteallergi, går det bra?"))
    con.commit()
    return {"kid": kid, "pid": pid, "did": did}


def _alt(con) -> str:
    """Hele databasen som tekst (for å lete etter rester av personopplysninger)."""
    tabeller = ("deltaker", "paamelding", "sensitivt", "dokument", "dokument_innhold", "innlogging_token", "utsending_logg",
                "firmapaamelding", "firmapaamelding_rad", "henvendelse", "hendelse", "sendt_epost", "sendt_epost_vedlegg",
                "epost_fil", "paamelding_svar")
    return "\n".join(str([tuple(r) for r in con.execute(f"SELECT * FROM {t}")]) for t in tabeller)


def test_anonymisering_fjerner_personopplysningene_overalt(con, person):
    for_ = _alt(con).lower()
    assert "mona" in for_ and "90011222" in for_ and "nøtteallergi" in for_                 # sporene finnes før
    assert "sporbarveien 4711" in for_ and "sporbarby" in for_                              # også den private adressen
    ut = db.anonymiser_deltaker(con, person["did"], aktor="admin:test")
    con.commit()
    alt = _alt(con).lower()
    for spor in ("mona", "90011222", "psykolog", "1234567", "nøtter", "gate 1", "ring henne", "nøtteallergi",
                 "mona-klinikken", "sporbarveien", "sporbarby"):
        assert spor not in alt, spor
    # Deltakerens private adresse er tømt på personen, og fakturaadressen (kopien) på påmeldingene
    d = con.execute("SELECT adresse, postnr, poststed FROM deltaker WHERE id=?", (person["did"],)).fetchone()
    assert tuple(d) == (None, None, None)
    p = con.execute("SELECT faktura_adresse, faktura_postnr, faktura_sted FROM paamelding WHERE id=?",
                    (person["pid"],)).fetchone()
    assert tuple(p) == (None, None, None)
    d = con.execute("SELECT navn, epost FROM deltaker WHERE id=?", (person["did"],)).fetchone()
    assert (d["navn"], d["epost"]) == (db.ANONYM_NAVN, f"anonymisert-{person['did']}@{db.ANONYM_DOMENE}")
    assert ut == {"paameldinger": 1, "sensitivt": 1, "skjemasvar": 1, "dokumenter": 1, "innloggingslenker": 1,
                  "utsendingslogg": 1, "epostkopier": 1, "importforhandsvisninger": 0, "firmarader": 1, "firmakontakt": 1,
                  "henvendelser": 1}
    logg = con.execute("SELECT detaljer FROM hendelse WHERE handling='deltaker_anonymisert'").fetchone()["detaljer"]
    assert json.loads(logg) == {"deltaker_id": person["did"], **ut}


def test_anonyme_tall_og_regnskap_stemmer_fortsatt(con, person):
    for_ = okonomi.rapport(okonomi.fakturaer(con, date(2030, 1, 1), date(2030, 12, 31))).totalt.belop
    db.anonymiser_deltaker(con, person["did"], aktor="admin:test")
    con.commit()
    assert okonomi.rapport(okonomi.fakturaer(con, date(2030, 1, 1), date(2030, 12, 31))).totalt.belop == for_ == 3000
    assert con.execute("SELECT COUNT(*) FROM oppmote WHERE paamelding_id=?", (person["pid"],)).fetchone()[0] == 1
    assert con.execute("SELECT status FROM paamelding WHERE id=?", (person["pid"],)).fetchone()[0] == "bekreftet"


def test_personen_kan_melde_seg_paa_igjen_som_ny(con, person):
    db.anonymiser_deltaker(con, person["did"], aktor="admin:test")
    kid = db.opprett_kurs(con, kode="NY-2031", navn="Nytt kurs", datoer=["2031-01-01"])
    db.meld_paa(con, kid, epost=EPOST, fornavn="Mona", etternavn="Person")
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM deltaker WHERE LOWER(epost)=LOWER(?)", (EPOST,)).fetchone()[0] == 1


def test_nekter_ved_aktive_paameldinger_og_to_ganger(con, person):
    kid = db.opprett_kurs(con, kode="AKTIV-2031", navn="Aktivt kurs", datoer=["2031-01-01"])
    db.meld_paa(con, kid, epost=EPOST, fornavn="Mona", etternavn="Person")
    con.commit()
    with pytest.raises(db.DeltakerFeil, match="aktive påmeldinger"):
        db.anonymiser_deltaker(con, person["did"], aktor="admin:test")
    con.rollback()
    con.execute("UPDATE paamelding SET status='avmeldt' WHERE kurs_id=?", (kid,))
    db.anonymiser_deltaker(con, person["did"], aktor="admin:test")
    with pytest.raises(db.DeltakerFeil, match="allerede anonymisert"):
        db.anonymiser_deltaker(con, person["did"], aktor="admin:test")


def test_ruten_krever_systemrolle_og_bekreftelse(con, person):
    db.opprett_admin_bruker(con, "kurs", "Kurs", "passord-som-holder", rolle="kursadmin")
    con.commit()
    kursadmin = _admin("kurs", "passord-som-holder")
    assert "Retten til sletting" not in kursadmin.get(f"/admin/rapporter/deltaker/{person['did']}").get_data(as_text=True)
    assert kursadmin.post(f"/admin/rapporter/deltaker/{person['did']}/anonymiser",
                          data={"bekreft": "ANONYMISER"}).status_code == 403
    system = _admin()
    assert "Retten til sletting" in system.get(f"/admin/rapporter/deltaker/{person['did']}").get_data(as_text=True)
    r = system.post(f"/admin/rapporter/deltaker/{person['did']}/anonymiser", data={"bekreft": "ja"})
    assert "Skriv ANONYMISER" in system.get(r.headers["Location"]).get_data(as_text=True)
    assert con.execute("SELECT navn FROM deltaker WHERE id=?", (person["did"],)).fetchone()[0] == "Mona Person"
    r = system.post(f"/admin/rapporter/deltaker/{person['did']}/anonymiser", data={"bekreft": "anonymiser"})
    side = system.get(r.headers["Location"]).get_data(as_text=True)
    assert "Personopplysningene er fjernet" in side and "Retten til sletting" not in side
    assert con.execute("SELECT navn FROM deltaker WHERE id=?", (person["did"],)).fetchone()[0] == db.ANONYM_NAVN
