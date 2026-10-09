"""Fornavn og etternavn som egne felt (migrering 7) og flettefeltene {fornavn}/{navn} i e-post.

  * migreringen deler EKSISTERENDE navn én gang (siste ord = etternavn), er idempotent og logger bare antall
  * alle registreringsveier krever fornavn og etternavn hver for seg - fullt navn i ett felt gjettes det aldri paa
  * navn (fullt navn) settes automatisk og brukes der det trengs (kursbevis, lister, fakturaer, CSV)
  * {fornavn}/{navn} i e-postmalene og i egenskrevet e-post, uten automatisk hilsen, og som klikkbare knapper
"""
import hashlib
import hmac
import json
import re
import sqlite3
from datetime import date, timedelta

import pytest

from adressehjelp import ADRESSE, gruppeadresse
from kurs import config, db, import_deltakere, maltekster, migreringer, migrer


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    return c


def _kurs(con, kode="FN1", **kw):
    start = date.today() + timedelta(days=30)
    return db.opprett_kurs(con, kode=kode, navn="Navnekurs", datoer=[start.isoformat()], sharepoint_mappe=f"Kurs/{kode}",
                           **{"pris_nok": 0, **kw})


def _klient():
    from kurs.web import app as webapp
    return webapp.app.test_client()


def _admin():
    k = _klient()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def _fersk():
    return db.koble(config.DB_STI)


def _paameldte(con):
    kid = _kurs(con)
    p1, _ = db.meld_paa(con, kid, epost="kari@x.no", fornavn="Kari", etternavn="Nordmann")
    p2, _ = db.meld_paa(con, kid, epost="ola@x.no", fornavn="Ola Johan", etternavn="Hansen")
    con.commit()
    return kid, [p1, p2]


def _utboks() -> dict:
    """{mottaker: html} for e-postene i demo-utboksen."""
    ut = {}
    for fil in config.UTBOKS.glob("*.html"):
        tekst = fil.read_text(encoding="utf-8")
        ut[json.loads(tekst.split("<!--META ", 1)[1].split(" -->", 1)[0])["til"]] = tekst
    return ut


# ============================ migrering 7 ============================

@pytest.mark.kun_sqlite  # gjenskaper en SQLite-database slik den saa ut FOER migrering 7
def test_migrering_deler_eksisterende_navn_en_gang_og_logger_bare_antall(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_STI", tmp_path / "gammel.db")
    monkeypatch.setattr(config, "DEMO", True)
    skjema = re.sub(r"--[^\n]*", "", (config.ROT / "kurs" / "schema.sql").read_text(encoding="utf-8"))
    skjema = re.sub(r"\n\s+(kontakt_)?(fornavn|etternavn)\s+TEXT NOT NULL DEFAULT '',", "", skjema)
    assert "fornavn" not in skjema and "etternavn" not in skjema
    raa = sqlite3.connect(tmp_path / "gammel.db")
    raa.executescript(skjema)
    for nr, navn, _ in migreringer.MIGRERINGER[:6]:
        raa.execute("INSERT INTO schema_versjon (versjon, navn, kjort) VALUES (?,?,?)", (nr, navn, "2026-01-01 00:00:00"))
    for epost, navn in (("a@x.no", "Anne Marie Eksempelsen"), ("b@x.no", "Ola Nordmann"), ("c@x.no", "Cher"),
                        ("d@x.no", "Anonymisert deltaker")):
        raa.execute("INSERT INTO deltaker (epost, navn) VALUES (?,?)", (epost, navn))
    raa.execute("INSERT INTO kurs (kode, navn, sharepoint_mappe) VALUES ('K', 'Kurs', 'Kurs/K')")
    raa.execute("""INSERT INTO firmapaamelding (kurs_id, innsendingsnokkel, kvittering_token, kontakt_navn, kontakt_epost,
                   firmanavn) VALUES (1, 'n', 't', 'Kari Hansen', 'kari@firma.no', 'Firma AS')""")
    raa.commit()
    raa.close()

    assert migrer.kjor([]) == 0
    c = db.koble()
    assert migreringer.gjeldende_versjon(c) == migreringer.KODEVERSJON
    rader = {r["epost"]: (r["navn"], r["fornavn"], r["etternavn"]) for r in c.execute("SELECT * FROM deltaker")}
    assert rader == {"a@x.no": ("Anne Marie Eksempelsen", "Anne Marie", "Eksempelsen"),   # doble fornavn blir riktige
                     "b@x.no": ("Ola Nordmann", "Ola", "Nordmann"),
                     "c@x.no": ("Cher", "Cher", ""),                                     # ett ord: etternavn fylles inn senere
                     "d@x.no": ("Anonymisert deltaker", "Anonymisert", "deltaker")}
    kontakt = c.execute("SELECT kontakt_navn, kontakt_fornavn, kontakt_etternavn FROM firmapaamelding").fetchone()
    assert tuple(kontakt) == ("Kari Hansen", "Kari", "Hansen")
    logg = c.execute("SELECT detaljer FROM hendelse WHERE handling='navn_delt_ved_migrering'").fetchall()
    assert len(logg) == 1
    assert json.loads(logg[0]["detaljer"]) == {"deltakere": 4, "deltakere_ett_ord": 1, "deltakere_tre_eller_flere_ord": 1,
                                               "kontaktpersoner": 1, "kontaktpersoner_ett_ord": 0,
                                               "kontaktpersoner_tre_eller_flere_ord": 0}
    assert "Eksempelsen" not in logg[0]["detaljer"] and "Kari" not in logg[0]["detaljer"]      # aldri navn i loggen

    # Idempotent: kjoert paa nytt endres ingenting - heller ikke et navn som er rettet etterpaa - og ingen ny logg
    c.execute("UPDATE deltaker SET etternavn='Sarkisian', navn='Cher Sarkisian' WHERE epost='c@x.no'")
    c.commit()
    assert migrer.kjor([]) == 0
    migreringer._m7_fornavn_etternavn(c)
    assert c.execute("SELECT etternavn FROM deltaker WHERE epost='c@x.no'").fetchone()[0] == "Sarkisian"
    assert c.execute("SELECT COUNT(*) FROM hendelse WHERE handling='navn_delt_ved_migrering'").fetchone()[0] == 1
    c.close()


def test_ny_database_har_feltene_fra_start_og_migreringen_deler_ingenting(con):
    assert db.har_kolonne(con, "deltaker", "fornavn") and db.har_kolonne(con, "deltaker", "etternavn")
    assert db.har_kolonne(con, "firmapaamelding", "kontakt_fornavn") and db.har_kolonne(con, "firmapaamelding", "kontakt_etternavn")
    assert not con.execute("SELECT 1 FROM hendelse WHERE handling='navn_delt_ved_migrering'").fetchone()


@pytest.mark.parametrize("navn, delt", [("Anne Marie Eksempelsen", ("Anne Marie", "Eksempelsen")), ("Ola  Nordmann ", ("Ola", "Nordmann")),
                                         ("Cher", ("Cher", "")), ("", ("", ""))])
def test_migreringens_deleregel(navn, delt):
    assert migreringer.del_eksisterende_navn(navn) == delt


# ============================ databaselaget ============================

def test_paamelding_lagrer_fornavn_og_etternavn_og_setter_fullt_navn(con):
    kid = _kurs(con)
    db.meld_paa(con, kid, epost="r@x.no", fornavn=" Anne Marie ", etternavn=" Eksempelsen ")
    d = con.execute("SELECT navn, fornavn, etternavn FROM deltaker WHERE epost='r@x.no'").fetchone()
    assert tuple(d) == ("Anne Marie Eksempelsen", "Anne Marie", "Eksempelsen")
    assert db.fullt_navn("Anne Marie", "Eksempelsen") == "Anne Marie Eksempelsen" and db.fullt_navn("Cher", "") == "Cher"


@pytest.mark.parametrize("fornavn, etternavn", [("", "Eksempelsen"), ("Anne", ""), ("  ", "Eksempelsen")])
def test_paamelding_krever_baade_fornavn_og_etternavn(con, fornavn, etternavn):
    kid = _kurs(con)
    with pytest.raises(db.Paameldingsfeil, match="fornavn og etternavn"):
        db.meld_paa(con, kid, epost="r@x.no", fornavn=fornavn, etternavn=etternavn)
    assert not con.execute("SELECT 1 FROM deltaker").fetchone()


def test_navnet_til_en_eksisterende_person_endres_ikke_av_en_ny_paamelding(con):
    k1, k2 = _kurs(con, "FN1"), _kurs(con, "FN2")
    db.meld_paa(con, k1, epost="r@x.no", fornavn="Anne Marie", etternavn="Eksempelsen")
    db.meld_paa(con, k2, epost="r@x.no", fornavn="Anne", etternavn="Eksempelsen")
    assert con.execute("SELECT fornavn FROM deltaker WHERE epost='r@x.no'").fetchone()[0] == "Anne Marie"


def test_redigering_setter_fullt_navn_automatisk_og_navn_kan_ikke_settes_direkte(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="o@x.no", fornavn="Ola", etternavn="Nordmann")
    did = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pid,)).fetchone()[0]
    assert set(db.oppdater_deltaker(con, did, {"fornavn": "Ola Johan"})) == {"fornavn", "navn"}
    assert con.execute("SELECT navn FROM deltaker WHERE id=?", (did,)).fetchone()[0] == "Ola Johan Nordmann"
    with pytest.raises(db.DeltakerFeil, match="fornavn og etternavn"):
        db.oppdater_deltaker(con, did, {"etternavn": " "})
    with pytest.raises(db.DeltakerFeil):
        db.oppdater_deltaker(con, did, {"navn": "Noe annet"})
    assert con.execute("SELECT navn FROM deltaker WHERE id=?", (did,)).fetchone()[0] == "Ola Johan Nordmann"


def test_anonymisering_fjerner_ogsaa_fornavn_og_etternavn(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="Anna", etternavn="Hemmelig")
    db.meld_av(con, pid, aktor="test")
    did = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pid,)).fetchone()[0]
    db.anonymiser_deltaker(con, did, aktor="test")
    d = con.execute("SELECT navn, fornavn, etternavn FROM deltaker WHERE id=?", (did,)).fetchone()
    assert tuple(d) == (db.ANONYM_NAVN, db.ANONYM_FORNAVN, db.ANONYM_ETTERNAVN) and "Hemmelig" not in tuple(d)


# ============================ registreringsveiene ============================

def test_offentlig_skjema_har_egne_felt_og_krever_begge(con):
    _kurs(con)
    con.commit()
    k = _klient()
    html = k.get("/kurs/FN1").get_data(as_text=True)
    assert 'name="fornavn"' in html and 'name="etternavn"' in html and 'name="navn"' not in html
    r = k.post("/kurs/FN1", data={"fornavn": "Kari", "etternavn": "", "epost": "k@x.no", "samtykke": "on", "samtykke_lagring": "on", **ADRESSE})
    assert r.status_code == 400 and "Fyll inn fornavn, etternavn og gyldig e-post." in r.get_data(as_text=True)
    r = k.post("/kurs/FN1", data={"fornavn": "Kari", "etternavn": "Nordmann", "epost": "k@x.no", "samtykke": "on", "samtykke_lagring": "on", **ADRESSE})
    assert r.status_code == 200 and "Takk, Kari." in r.get_data(as_text=True)
    d = _fersk().execute("SELECT navn, fornavn, etternavn FROM deltaker WHERE epost='k@x.no'").fetchone()
    assert tuple(d) == ("Kari Nordmann", "Kari", "Nordmann")
    assert "Hei Kari," in _utboks()["k@x.no"]                       # bekreftelsen hilser med fornavnet


def _webhook(data: dict):
    body = json.dumps(data).encode()
    sig = hmac.new(config.WEBHOOK_HEMMELIG.encode(), body, hashlib.sha256).hexdigest()
    return _klient().post("/api/paamelding", data=body, content_type="application/json", headers={"X-IPR-Signatur": sig})


def test_nettsidemottak_tar_imot_fornavn_og_etternavn(con):
    _kurs(con)
    con.commit()
    r = _webhook({"first_name": "Kari", "last_name": "Nordmann", "email": "k@x.no", "kurs": "FN1", **ADRESSE})
    assert r.status_code == 201
    d = _fersk().execute("SELECT navn, fornavn, etternavn FROM deltaker WHERE epost='k@x.no'").fetchone()
    assert tuple(d) == ("Kari Nordmann", "Kari", "Nordmann")


def test_nettsidemottak_avviser_fullt_navn_i_ett_felt_uten_aa_gjette(con):
    _kurs(con)
    con.commit()
    r = _webhook({"navn": "Kari Nordmann", "epost": "k@x.no", "kurs": "FN1", **ADRESSE})
    assert r.status_code == 400 and "fornavn og etternavn hver for seg" in r.get_json()["melding"]
    assert not _fersk().execute("SELECT 1 FROM deltaker").fetchone()


def test_import_krever_egne_kolonner_og_avviser_gammel_navnekolonne():
    rader = import_deltakere.parse_csv(
        "Fornavn;Etternavn;E-post;Adresse;Postnr;Poststed\nAnne Marie;Eksempelsen;r@x.no;Eksempelveien 1;0150;Oslo\n".encode())
    assert rader == [{"fornavn": "Anne Marie", "etternavn": "Eksempelsen", "epost": "r@x.no", **ADRESSE}]
    with pytest.raises(import_deltakere.ImportFeil, match="hver sin kolonne"):
        import_deltakere.parse_csv("Navn;E-post\nKari Nordmann;k@x.no\n".encode())
    with pytest.raises(import_deltakere.ImportFeil, match="Etternavn"):
        import_deltakere.parse_csv("Fornavn;E-post\nKari;k@x.no\n".encode())
    assert import_deltakere.MALFIL_HEADER[:3] == ["Fornavn", "Etternavn", "E-post"]
    assert import_deltakere._valider_rad({"fornavn": "Kari", "etternavn": "", "epost": "k@x.no", **ADRESSE}) == [
        "Etternavn mangler"]


def test_bedriftspaamelding_lagrer_kontaktperson_og_deltakere_med_egne_felt(con):
    _kurs(con)
    con.commit()
    data = {"kontakt_fornavn": "Kari", "kontakt_etternavn": "Hansen", "kontakt_epost": "kari@firma.no",
            "kontakt_telefon": "", "firmanavn": "Firma AS", "org_nr": "999900003", "faktura_ref": "",
            "deltaker_fornavn": ["Ola Johan"], "deltaker_etternavn": ["Nordmann"], "deltaker_epost": ["ola@firma.no"],
            "deltaker_telefon": [""], "deltaker_arbeidssted": [""], "samtykke": "on", "samtykke_lagring": "on", **gruppeadresse()}
    assert _klient().post("/kurs/FN1/gruppe", data=data, follow_redirects=True).status_code == 200
    fersk = _fersk()
    kontakt = fersk.execute("SELECT kontakt_navn, kontakt_fornavn, kontakt_etternavn FROM firmapaamelding").fetchone()
    assert tuple(kontakt) == ("Kari Hansen", "Kari", "Hansen")
    d = fersk.execute("SELECT navn, fornavn, etternavn FROM deltaker WHERE epost='ola@firma.no'").fetchone()
    assert tuple(d) == ("Ola Johan Nordmann", "Ola Johan", "Nordmann")
    assert fersk.execute("SELECT navn FROM firmapaamelding_rad").fetchone()[0] == "Ola Johan Nordmann"
    utboks = _utboks()
    assert "Hei Kari," in utboks["kari@firma.no"] and "Hei Ola Johan," in utboks["ola@firma.no"]


def test_bedriftspaamelding_krever_fornavn_og_etternavn_paa_hver_deltaker(con):
    _kurs(con)
    con.commit()
    data = {"kontakt_fornavn": "Kari", "kontakt_etternavn": "Hansen", "kontakt_epost": "kari@firma.no",
            "firmanavn": "Firma AS", "org_nr": "999900003", "deltaker_fornavn": ["Ola"], "deltaker_etternavn": [""],
            "deltaker_epost": ["ola@firma.no"], "deltaker_telefon": [""], "deltaker_arbeidssted": [""], "samtykke": "on", "samtykke_lagring": "on",
            **gruppeadresse()}
    r = _klient().post("/kurs/FN1/gruppe", data=data)
    assert r.status_code == 400 and "Deltaker 1: fyll inn fornavn, etternavn" in r.get_data(as_text=True)
    assert not _fersk().execute("SELECT 1 FROM deltaker").fetchone()


def test_manuell_registrering_i_admin_har_egne_felt(con):
    kid = _kurs(con)
    con.commit()
    admin = _admin()
    assert 'name="fornavn"' in admin.get(f"/admin/kurs/{kid}/deltaker/ny").get_data(as_text=True)
    r = admin.post(f"/admin/kurs/{kid}/deltaker/ny", data={"fornavn": "Per", "etternavn": "Hansen", "epost": "per@x.no",
                                                           **ADRESSE})
    assert r.status_code == 302
    assert tuple(_fersk().execute("SELECT navn, fornavn, etternavn FROM deltaker").fetchone()) == ("Per Hansen", "Per", "Hansen")


def test_deltakervinduet_har_egne_felt_og_lagring_oppdaterer_fullt_navn(con):
    kid, (p1, _) = _paameldte(con)
    admin = _admin()
    side = admin.get(f"/admin/kurs/{kid}/deltaker/{p1}").get_data(as_text=True)
    assert 'id="f-fornavn" name="fornavn" required value="Kari"' in side
    assert 'id="f-etternavn" name="etternavn" required value="Nordmann"' in side
    versjon = side.split('name="versjon" value="')[1].split('"')[0]
    r = admin.post(f"/admin/kurs/{kid}/deltaker/{p1}/person",
                   data={"fornavn": "Kari Anne", "etternavn": "Nordmann", "epost": "kari@x.no", "versjon": versjon, **ADRESSE})
    assert r.status_code == 302
    d = _fersk().execute("SELECT navn, fornavn FROM deltaker WHERE epost='kari@x.no'").fetchone()
    assert tuple(d) == ("Kari Anne Nordmann", "Kari Anne")


def test_deltakervinduet_avviser_tomt_etternavn(con):
    kid, (p1, _) = _paameldte(con)
    admin = _admin()
    r = admin.post(f"/admin/kurs/{kid}/deltaker/{p1}/person", data={"fornavn": "Kari", "etternavn": "", "epost": "kari@x.no", **ADRESSE},
                   follow_redirects=True)
    assert "Fyll inn fornavn, etternavn og en gyldig e-postadresse." in r.get_data(as_text=True)
    assert _fersk().execute("SELECT etternavn FROM deltaker WHERE epost='kari@x.no'").fetchone()[0] == "Nordmann"


def test_csv_eksport_har_fornavn_og_etternavn_i_egne_kolonner(con):
    kid, _ = _paameldte(con)
    hode, *rader = _admin().get(f"/admin/kurs/{kid}/deltakere.csv").get_data(as_text=True).lstrip("﻿").splitlines()
    assert hode.startswith("Fornavn;Etternavn;E-post;")
    assert any(r.startswith("Ola Johan;Hansen;ola@x.no;") for r in rader)


# ============================ e-post ============================

# (06.10.2026: kursholder-lenken og SharePoint er tatt bort, og med dem «purring» til kursholdere, som brukte fullt navn;
# alle de 8 redigerbare malene går nå til deltakere eller kontaktpersoner)
@pytest.mark.parametrize("mal", ["bekreftelse", "venteliste", "ukefor", "dagfor", "avlysning", "kursbevis_klar",
                                 "firmapaamelding_kvittering", "evaluering"])
def test_alle_maler_til_deltakere_tillater_fornavn_og_navn(mal):
    for felt in maltekster.MALER[mal].felt.values():
        assert {"fornavn", "navn"} <= felt.kode
    assert all(f.standard.startswith("Hei {fornavn},") for f in maltekster.MALER[mal].felt.values()
               if f.standard.startswith("Hei "))


def test_standardhilsen_bruker_fornavn_og_navn_gir_fortsatt_fullt_navn():
    data = {"p": {"fornavn": "Anne Marie", "navn": "Anne Marie Eksempelsen"}, "kurs": {"navn": "Kurs"}}
    assert str(maltekster.standard_maltekst("venteliste", data)["tekst"]).startswith("<p>Hei Anne Marie,</p>")
    html = maltekster.felttekst_til_html("venteliste", "tekst", "Kjære {navn} ({fornavn})",
                                         maltekster._venteliste_verdier(data))
    assert "Kjære Anne Marie Eksempelsen (Anne Marie)" in str(html)


def test_egen_epost_fletter_inn_hver_mottakers_navn_uten_automatisk_hilsen(con):
    kid, pider = _paameldte(con)
    admin = _admin()
    r = admin.post(f"/admin/kurs/{kid}/epost/forhandsvis",
                   data={"emne": "Info", "tekst": "Kjære {fornavn}!\n\nVelkommen, {navn}.", "paamelding_id": pider})
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "Kjære Kari!" in html and "Velkommen, Kari Nordmann." in html    # eksempel: første mottaker
    utsending_id = int(re.search(r'name="utsending_id" value="(\d+)"', html).group(1))
    assert admin.post(f"/admin/kurs/{kid}/epost/send", data={"utsending_id": utsending_id}).status_code == 302
    utboks = _utboks()
    assert "Kjære Kari!" in utboks["kari@x.no"] and "Velkommen, Kari Nordmann." in utboks["kari@x.no"]
    assert "Kjære Ola Johan!" in utboks["ola@x.no"] and "Velkommen, Ola Johan Hansen." in utboks["ola@x.no"]
    assert all("<p>Hei " not in e for e in utboks.values())                  # ingen automatisk hilsen


@pytest.mark.parametrize("tekst, forklaring", [("Hei {fornamn},", "Koden {fornamn} kan ikke brukes her"),
                                               ("Hei {fornavn,", "uten avsluttende"),
                                               ("Hei {kursnavn},", "Gyldige koder: {fornavn}, {min_side}, {navn}")])
def test_egen_epost_med_ukjent_kode_eller_loes_klamme_stoppes_foer_noe_lagres(con, tekst, forklaring):
    kid, pider = _paameldte(con)
    r = _admin().post(f"/admin/kurs/{kid}/epost/forhandsvis", data={"emne": "Info", "tekst": tekst, "paamelding_id": pider})
    assert forklaring in r.get_data(as_text=True)
    assert _fersk().execute("SELECT COUNT(*) FROM admin_utsending").fetchone()[0] == 0


def test_flettefelt_i_emnet_avvises_med_forklaring(con):
    kid, pider = _paameldte(con)
    r = _admin().post(f"/admin/kurs/{kid}/epost/forhandsvis",
                      data={"emne": "Til {fornavn}", "tekst": "Hei {fornavn},", "paamelding_id": pider})
    assert "kan bare brukes i selve meldingen" in r.get_data(as_text=True)
    assert _fersk().execute("SELECT COUNT(*) FROM admin_utsending").fetchone()[0] == 0


def test_flettet_navn_escapes(con):
    html = str(maltekster.manuell_tekst_til_html("Hei {fornavn} <b>!</b>", {"fornavn": "<i>Ola</i> & Co", "navn": "x"}))
    assert "&lt;i&gt;Ola&lt;/i&gt; &amp; Co" in html and "&lt;b&gt;!&lt;/b&gt;" in html and "<i>" not in html


def test_flettefeltene_er_klikkbare_knapper_i_epost_og_i_malene(con):
    kid, pider = _paameldte(con)
    admin = _admin()
    html = admin.post(f"/admin/kurs/{kid}/epost/ny", data={"paamelding_id": pider}).get_data(as_text=True)
    assert 'data-sett-inn="{fornavn}" data-felt="f-tekst"' in html and 'data-sett-inn="{navn}" data-felt="f-tekst"' in html
    assert html.index('data-sett-inn="{fornavn}"') < html.index('data-sett-inn="{navn}"')     # fornavn først
    mal = admin.get("/admin/e-postmaler/bekreftelse").get_data(as_text=True)
    assert 'data-sett-inn="{fornavn}" data-felt="f-innledning"' in mal
    assert 'data-sett-inn="{min_side}" data-felt="f-innledning"' in mal
