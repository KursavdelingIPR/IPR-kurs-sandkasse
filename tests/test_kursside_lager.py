"""Kursside: SQL-laget (kurs/sidelager.py) - lagring med versjonskontroll, publisering, versjoner, innstillinger, «noen redigerer», filer
(typer, innholdssjekk, størrelse, kvote, deduplisering), sletting og opprydding, gruppelister etter kurset.

Portabel SQL: testene kjøres også mot PostgreSQL når TEST_DATABASE_URL er satt (tests/conftest.py). Bare oppdiktede data.
"""
import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from kurs import config, db
from kurs import sideinnhold as si
from kurs import sidelager
from kurs.sideinnhold import Sidefeil

from kurssidehjelp import IDAG, dokument, gif, jpeg, lag_deltaker, lag_kurs, ny_database, ole, pdf, png, skriv_side, tekstblokk, webp, zip_lik

ADMIN = "admin:test"


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    yield c
    c.close()


@pytest.fixture
def kid(con):
    return lag_kurs(con)


def _dok(tittel="Side"):
    return dokument(tekstblokk("A", id="b_aaaa1111"), tittel=tittel)


# ============================ utkast og versjonskontroll ============================

def test_ingen_rad_gir_tomt_dokument_og_versjon_null(con, kid):
    assert sidelager.hent(con, kid) is None
    assert sidelager.hent_utkast(con, kid) == (si.tomt_dokument(), 0) and sidelager.hent_publisert(con, kid) is None


def test_foerste_lagring_fra_versjon_null_gir_versjon_en(con, kid):
    assert sidelager.lagre_utkast(con, kid, _dok(), 0, ADMIN) == 1
    dok, versjon = sidelager.hent_utkast(con, kid)
    assert versjon == 1 and dok["tittel"] == "Side"
    rad = sidelager.hent(con, kid)
    assert rad["endret_av"] == ADMIN and rad["endret"] and rad["publisert"] is None and rad["aktiv"] == 1 and rad["innsjekk_krever_kode"] == 1


def test_lagring_oeker_versjonen_og_feil_versjon_gir_none_uten_a_skrive(con, kid):
    sidelager.lagre_utkast(con, kid, _dok("En"), 0, ADMIN)
    assert sidelager.lagre_utkast(con, kid, _dok("To"), 1, ADMIN) == 2
    assert sidelager.lagre_utkast(con, kid, _dok("Gammel"), 1, ADMIN) is None                 # to som redigerer: den siste taper
    assert sidelager.lagre_utkast(con, kid, _dok("Fremtid"), 5, ADMIN) is None
    assert sidelager.hent_utkast(con, kid)[0]["tittel"] == "To" and sidelager.hent_utkast(con, kid)[1] == 2


def test_to_som_lagrer_fra_samme_versjon_bare_den_foerste_vinner(con, kid):
    sidelager.lagre_utkast(con, kid, _dok("Start"), 0, ADMIN)
    a = sidelager.lagre_utkast(con, kid, _dok("Kari"), 1, "admin:kari")
    b = sidelager.lagre_utkast(con, kid, _dok("Ola"), 1, "admin:ola")
    assert (a, b) == (2, None) and sidelager.hent_utkast(con, kid)[0]["tittel"] == "Kari"
    assert sidelager.hent(con, kid)["endret_av"] == "admin:kari"


def test_versjon_null_naar_raden_finnes_er_en_konflikt(con, kid):
    sidelager.lagre_utkast(con, kid, _dok(), 0, ADMIN)
    assert sidelager.lagre_utkast(con, kid, _dok("Overskriver ikke"), 0, "admin:annen") is None
    assert sidelager.hent_utkast(con, kid)[0]["tittel"] == "Side"


def test_erstatt_utkast_tar_sikkerhetskopi_foerst_og_gjoer_ingenting_ved_konflikt(con, kid):
    sidelager.lagre_utkast(con, kid, _dok("Gammelt utkast"), 0, ADMIN)
    assert sidelager.erstatt_utkast(con, kid, _dok("Fra mal"), 1, ADMIN, "Før mal") == 2
    v = sidelager.versjoner(con, kid)
    assert len(v) == 1 and v[0]["arsak"] == "sikkerhetskopi" and v[0]["merknad"] == "Før mal" and v[0]["versjon"] == 1
    assert json.loads(sidelager.hent_versjon(con, kid, v[0]["id"])["innhold"])["tittel"] == "Gammelt utkast"
    assert sidelager.erstatt_utkast(con, kid, _dok("Konflikt"), 1, ADMIN, "Før mal") is None
    assert len(sidelager.versjoner(con, kid)) == 1 and sidelager.hent_utkast(con, kid)[0]["tittel"] == "Fra mal"


def test_erstatt_utkast_uten_rad_lager_raden_uten_sikkerhetskopi(con, kid):
    assert sidelager.erstatt_utkast(con, kid, _dok(), 0, ADMIN, "Før mal") == 1 and sidelager.versjoner(con, kid) == []


# ============================ publisering ============================

def test_publiser_kopierer_utkastet_og_lager_versjonsrad(con, kid):
    sidelager.lagre_utkast(con, kid, _dok("Ferdig"), 0, ADMIN)
    assert sidelager.publiser(con, kid, 1, "admin:kari") == 1
    rad = sidelager.hent(con, kid)
    assert rad["publisert"] == rad["utkast"] and rad["publisert_versjon"] == 1 and rad["publisert_av"] == "admin:kari" and rad["publisert_tid"]
    assert sidelager.hent_publisert(con, kid)["tittel"] == "Ferdig"
    v = sidelager.versjoner(con, kid)
    assert [(x["arsak"], x["versjon"], x["opprettet_av"]) for x in v] == [("publisert", 1, "admin:kari")]
    assert "innhold" not in v[0]


def test_publiser_med_gammel_versjon_eller_uten_rad_gir_none(con, kid):
    assert sidelager.publiser(con, kid, 1, ADMIN) is None                                  # ingen rad
    sidelager.lagre_utkast(con, kid, _dok(), 0, ADMIN)
    sidelager.lagre_utkast(con, kid, _dok("To"), 1, ADMIN)
    assert sidelager.publiser(con, kid, 1, ADMIN) is None and sidelager.hent_publisert(con, kid) is None
    assert sidelager.publiser(con, kid, 2, ADMIN) == 2


def test_utkast_endres_uten_at_den_publiserte_siden_endres(con, kid):
    sidelager.lagre_utkast(con, kid, _dok("Publisert"), 0, ADMIN)
    sidelager.publiser(con, kid, 1, ADMIN)
    sidelager.lagre_utkast(con, kid, _dok("Nytt utkast"), 1, ADMIN)
    rad = sidelager.hent(con, kid)
    assert sidelager.hent_publisert(con, kid)["tittel"] == "Publisert" and sidelager.hent_utkast(con, kid)[0]["tittel"] == "Nytt utkast"
    assert rad["versjon"] == 2 and rad["publisert_versjon"] == 1                          # versjon > publisert_versjon = upubliserte endringer


def test_versjoner_beskjaeres_til_30_publiserte_og_20_sikkerhetskopier(con, kid):
    versjon = 0
    for n in range(35):
        versjon = sidelager.lagre_utkast(con, kid, _dok(f"P{n}"), versjon, ADMIN)
        sidelager.publiser(con, kid, versjon, ADMIN)
    for n in range(25):
        versjon = sidelager.erstatt_utkast(con, kid, _dok(f"S{n}"), versjon, ADMIN, f"Før {n}")
    alle = sidelager.versjoner(con, kid, grense=500)
    assert sum(v["arsak"] == "publisert" for v in alle) == sidelager.MAKS_PUBLISERTE_VERSJONER == 30
    assert sum(v["arsak"] == "sikkerhetskopi" for v in alle) == sidelager.MAKS_SIKKERHETSKOPIER == 20
    assert [v["id"] for v in alle] == sorted((v["id"] for v in alle), reverse=True)        # nyeste først
    assert len(sidelager.versjoner(con, kid, grense=5)) == 5


def test_gjenopprett_tar_sikkerhetskopi_gir_ny_versjon_og_publiserer_ikke(con, kid):
    sidelager.lagre_utkast(con, kid, _dok("Versjon A"), 0, ADMIN)
    sidelager.publiser(con, kid, 1, ADMIN)
    sidelager.lagre_utkast(con, kid, _dok("Versjon B"), 1, ADMIN)
    publisert_id = sidelager.versjoner(con, kid)[0]["id"]
    ny, dok, merknader = sidelager.gjenopprett(con, kid, publisert_id, 2, ADMIN)
    assert ny == 3 and dok["tittel"] == "Versjon A" and merknader == []
    assert sidelager.hent_utkast(con, kid) == (dok, 3) and sidelager.hent(con, kid)["publisert_versjon"] == 1
    kopi = [v for v in sidelager.versjoner(con, kid) if v["arsak"] == "sikkerhetskopi"]
    assert len(kopi) == 1 and kopi[0]["merknad"] == "Før gjenoppretting"
    assert json.loads(sidelager.hent_versjon(con, kid, kopi[0]["id"])["innhold"])["tittel"] == "Versjon B"


def test_gjenopprett_konflikt_ukjent_versjon_og_annet_kurs(con, kid):
    andre = lag_kurs(con, "K2")
    sidelager.lagre_utkast(con, kid, _dok(), 0, ADMIN)
    sidelager.publiser(con, kid, 1, ADMIN)
    vid = sidelager.versjoner(con, kid)[0]["id"]
    assert sidelager.gjenopprett(con, kid, vid, 99, ADMIN) is None                         # gammel versjon
    with pytest.raises(Sidefeil) as e:
        sidelager.gjenopprett(con, kid, 9999, 1, ADMIN)
    assert e.value.status == 404
    sidelager.lagre_utkast(con, andre, _dok(), 0, ADMIN)
    with pytest.raises(Sidefeil):
        sidelager.gjenopprett(con, andre, vid, 1, ADMIN)                                   # versjonen hører til et annet kurs
    assert sidelager.hent_versjon(con, andre, vid) is None


def test_gjenopprett_fjerner_referanse_til_slettet_fil_med_merknad(con, kid):
    fil_id, _ = sidelager.lagre_fil(con, kid, "presentasjon.pdf", pdf(), ADMIN)
    dok = dokument({"id": "b_filer001", "type": "filer", "tittel": "Filer", "data": {"filer": [{"fil_id": fil_id, "tittel": "P"}]}})
    skriv_side(con, kid, dok)
    vid = sidelager.versjoner(con, kid)[0]["id"]
    sidelager.lagre_utkast(con, kid, _dok("Uten fil"), 1, ADMIN)
    sidelager.publiser(con, kid, 2, ADMIN)                                                 # filen er ikke lenger referert i utkast/publisert
    con.execute("DELETE FROM kursside_fil_innhold WHERE fil_id=?", (fil_id,))
    con.execute("DELETE FROM kursside_fil WHERE id=?", (fil_id,))
    ny, gjenopprettet, merknader = sidelager.gjenopprett(con, kid, vid, 2, ADMIN)
    assert gjenopprettet["blokker"][0]["data"]["filer"] == [] and merknader and "finnes ikke lenger" in merknader[0]


# ============================ aktiv, innstillinger, åpen til ============================

def test_sett_aktiv_og_innstillinger_krever_at_raden_finnes(con, kid):
    assert sidelager.sett_aktiv(con, kid, False, ADMIN) is False
    assert sidelager.sett_innstillinger(con, kid, innsjekk_krever_kode=False, aktor=ADMIN) is False
    assert sidelager.sett_innstillinger(con, kid, aktor=ADMIN) is False
    sidelager.lagre_utkast(con, kid, _dok(), 0, ADMIN)
    assert sidelager.sett_aktiv(con, kid, False, ADMIN) is True and sidelager.hent(con, kid)["aktiv"] == 0
    assert sidelager.sett_aktiv(con, kid, True, ADMIN) is True and sidelager.hent(con, kid)["aktiv"] == 1
    assert sidelager.sett_innstillinger(con, kid, aktor=ADMIN) is True                     # ingenting å endre: True når raden finnes


def test_sett_innstillinger_none_er_uendret_og_tom_streng_tilbakestiller(con, kid):
    sidelager.lagre_utkast(con, kid, _dok(), 0, ADMIN)
    sidelager.sett_innstillinger(con, kid, innsjekk_krever_kode=False, stenges="2028-01-31", aktor=ADMIN)
    rad = sidelager.hent(con, kid)
    assert (rad["innsjekk_krever_kode"], rad["stenges"]) == (0, "2028-01-31")
    sidelager.sett_innstillinger(con, kid, aktor=ADMIN, innsjekk_krever_kode=True)         # stenges uendret
    assert sidelager.hent(con, kid)["stenges"] == "2028-01-31"
    sidelager.sett_innstillinger(con, kid, stenges="", aktor=ADMIN)
    rad = sidelager.hent(con, kid)
    assert (rad["innsjekk_krever_kode"], rad["stenges"]) == (1, None)


@pytest.mark.parametrize("daarlig", ["31.01.2028", "2028-13-01", "2028-02-30", "snart", "2028-1-1"])
def test_ugyldig_aapen_til_dato_gir_sidefeil(con, kid, daarlig):
    sidelager.lagre_utkast(con, kid, _dok(), 0, ADMIN)
    with pytest.raises(Sidefeil):
        sidelager.sett_innstillinger(con, kid, stenges=daarlig, aktor=ADMIN)


def test_effektiv_stenges_er_siste_kursdag_pluss_180_dager_og_kan_overstyres(con, kid, monkeypatch):
    siste = IDAG + timedelta(days=1)
    assert sidelager.siste_kursdag(con, kid) == siste
    assert sidelager.effektiv_stenges(con, kid, None) == siste + timedelta(days=180)
    sidelager.lagre_utkast(con, kid, _dok(), 0, ADMIN)
    assert sidelager.effektiv_stenges(con, kid, sidelager.hent(con, kid)) == siste + timedelta(days=180)
    monkeypatch.setattr(config, "KURSSIDE_ETTERTILGANG_DAGER", 10)
    assert sidelager.effektiv_stenges(con, kid, None) == siste + timedelta(days=10)
    sidelager.sett_innstillinger(con, kid, stenges="2027-12-24", aktor=ADMIN)
    assert sidelager.effektiv_stenges(con, kid, sidelager.hent(con, kid)).isoformat() == "2027-12-24"


def test_kurs_uten_kursdager_har_ingen_stengedato(con):
    kid = lag_kurs(con, "K9")
    con.execute("DELETE FROM kursdag WHERE kurs_id=?", (kid,))
    assert sidelager.siste_kursdag(con, kid) is None and sidelager.effektiv_stenges(con, kid, None) is None


# ============================ «noen redigerer nå» ============================

def test_redigerer_vises_bare_for_andre_og_bare_mens_den_er_gyldig(con, kid):
    con.execute("INSERT INTO admin_bruker (brukernavn, navn, passord_hash, rolle) VALUES ('marte', 'Marte Solberg', 'x', 'kursadmin')")
    sidelager.lagre_utkast(con, kid, _dok(), 0, "admin:marte")
    assert sidelager.hent_redigerer(con, kid, "admin:marte") is None                       # meg selv: aldri
    annen = sidelager.hent_redigerer(con, kid, "admin:kari")
    assert annen["navn"] == "Marte Solberg" and 0 <= annen["sekunder_siden"] < 60
    sidelager.sett_redigerer(con, kid, "admin:marte", sekunder=-5)                         # markeringen har utløpt
    assert sidelager.hent_redigerer(con, kid, "admin:kari") is None
    sidelager.sett_redigerer(con, kid, "admin:ukjent")
    assert sidelager.hent_redigerer(con, kid, "admin:kari")["navn"] == "ukjent"           # ukjent bruker: brukernavnet
    assert sidelager.hent_redigerer(con, lag_kurs(con, "K3"), "admin:kari") is None       # kurs uten side
    sidelager.sett_redigerer(con, kid, "system")
    assert sidelager.hent_redigerer(con, kid, "admin:kari") is None                        # systemet (demodata, morgenjobb) redigerer aldri «nå»


# ============================ filer: typer og innholdssjekk ============================

GYLDIGE = [("plan.pdf", pdf(), "dokument"), ("pres.pptx", zip_lik(), "dokument"), ("notat.docx", zip_lik(), "dokument"),
           ("tall.xlsx", zip_lik(), "dokument"), ("kurs.odp", zip_lik(), "dokument"), ("kurs.odt", zip_lik(), "dokument"),
           ("kurs.ods", zip_lik(), "dokument"), ("kurs.key", zip_lik(), "dokument"), ("alt.zip", zip_lik(), "dokument"),
           ("gammel.ppt", ole(), "dokument"), ("gammel.doc", ole(), "dokument"), ("gammel.xls", ole(), "dokument"),
           ("notat.txt", "Hei æøå\nAndre linje".encode("utf-8"), "dokument"), ("gammel.txt", "Blåbær".encode("cp1252"), "dokument"),
           ("bilde.png", png(), "bilde"), ("bilde.jpg", jpeg(), "bilde"), ("bilde.jpeg", jpeg(), "bilde"), ("bilde.gif", gif(), "bilde"),
           ("bilde.webp", webp(), "bilde"), ("STOR.PDF", pdf(), "dokument")]


@pytest.mark.parametrize("navn, data, type_", GYLDIGE)
def test_hver_tillatt_filtype_lagres_med_riktig_type(con, kid, navn, data, type_):
    fil_id, duplikat = sidelager.lagre_fil(con, kid, navn, data, ADMIN)
    assert duplikat is False
    meta, innhold = sidelager.fil_innhold(con, kid, fil_id)
    assert innhold == data and meta["type"] == type_ and meta["storrelse"] == len(data) and meta["filnavn"] == navn


def test_bildemaal_lagres_for_bilder(con, kid):
    for navn, data, bredde, hoyde in (("a.png", png(7, 5), 7, 5), ("b.jpg", jpeg(40, 30), 40, 30), ("c.gif", gif(9, 6), 9, 6), ("d.webp", webp(100, 50), 100, 50)):
        meta = sidelager.fil_meta(con, kid, sidelager.lagre_fil(con, kid, navn, data, ADMIN)[0])
        assert (meta["bredde"], meta["hoyde"]) == (bredde, hoyde), navn
    assert sidelager.fil_meta(con, kid, sidelager.lagre_fil(con, kid, "e.pdf", pdf(), ADMIN)[0])["bredde"] is None


@pytest.mark.parametrize("navn", ["bilde.svg", "side.html", "side.htm", "skript.js", "program.exe", "kjor.bat", "makro.docm", "makro.pptm", "makro.xlsm",
                                  "arkiv.rar", "arkiv.7z", "ukjent", "filnavn.", "skript.php", "fil.pdf.exe", "fil.svgz", ".pdf.js"])
def test_filtyper_som_ikke_er_tillatt_avvises_med_415(con, kid, navn):
    with pytest.raises(Sidefeil) as e:
        sidelager.lagre_fil(con, kid, navn, pdf(), ADMIN)
    assert e.value.status == 415
    assert sidelager.fil_liste(con, kid) == []


def test_svg_med_bildeendelse_avvises_selv_om_innholdet_er_svg(con, kid):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>alert(1)</script></svg>'
    for navn in ("logo.png", "logo.jpg", "logo.gif", "logo.webp", "logo.svg", "logo.pdf"):
        with pytest.raises(Sidefeil) as e:
            sidelager.lagre_fil(con, kid, navn, svg, ADMIN)
        assert e.value.status == 415, navn


@pytest.mark.parametrize("navn, data", [
    ("falsk.pdf", b"<html><script>alert(1)</script>"), ("falsk.pdf", b"MZ\x90\x00 kjorbar"), ("falsk.pptx", pdf()), ("falsk.docx", b"ikke en zip"),
    ("falsk.ppt", zip_lik()), ("falsk.xls", pdf()), ("falsk.png", jpeg()), ("falsk.jpg", png()), ("falsk.gif", png()), ("falsk.webp", png()),
    ("falsk.png", b"\x89PNG"), ("falsk.txt", b"binaer\x00data"), ("falsk.txt", b"\xff\xfe\x81\x8d\x8f"), ("falsk.zip", b"PK\x05\x06" + b"\x00" * 18)])
def test_innholdet_maa_passe_med_endelsen(con, kid, navn, data):
    with pytest.raises(Sidefeil, match="passer ikke med filendelsen") as e:
        sidelager.lagre_fil(con, kid, navn, data, ADMIN)
    assert e.value.status == 415 and sidelager.fil_liste(con, kid) == []


def test_pdf_signaturen_kan_ligge_innen_de_foerste_1024_byte(con, kid):
    assert sidelager.lagre_fil(con, kid, "a.pdf", b"\x00" * 500 + b"%PDF-1.7 ...", ADMIN)
    with pytest.raises(Sidefeil):
        sidelager.lagre_fil(con, kid, "b.pdf", b"\x00" * 2000 + b"%PDF-1.7 ...", ADMIN)


def test_tom_fil_gir_400(con, kid):
    with pytest.raises(Sidefeil) as e:
        sidelager.lagre_fil(con, kid, "tom.pdf", b"", ADMIN)
    assert e.value.status == 400 and "tom" in e.value.melding


def test_for_stor_fil_gir_413_og_bilder_har_egen_grense(con, kid, monkeypatch):
    monkeypatch.setattr(config, "KURSSIDE_FIL_MAKS_MB", 1)
    monkeypatch.setattr(config, "KURSSIDE_BILDE_MAKS_MB", 2)
    assert sidelager.lagre_fil(con, kid, "ok.pdf", pdf(b"0" * (1024 * 1024 - 100)), ADMIN)
    with pytest.raises(Sidefeil) as e:
        sidelager.lagre_fil(con, kid, "for-stor.pdf", pdf(b"1" * (1024 * 1024 + 1)), ADMIN)
    assert e.value.status == 413 and "maks 1 MB" in e.value.melding
    assert sidelager.lagre_fil(con, kid, "stort.png", png() + b"\x00" * (1024 * 1024 + 500_000), ADMIN)       # under bildegrensen (2 MB)
    with pytest.raises(Sidefeil) as e:
        sidelager.lagre_fil(con, kid, "for-stort.png", png() + b"\x00" * (2 * 1024 * 1024), ADMIN)
    assert e.value.status == 413 and "maks 2 MB" in e.value.melding


def test_standardgrensene_er_15_mb_for_dokument_5_mb_for_bilde_og_100_mb_og_200_filer_per_kurs():
    assert (config.KURSSIDE_FIL_MAKS_MB, config.KURSSIDE_BILDE_MAKS_MB, config.KURSSIDE_KURS_MAKS_MB, config.KURSSIDE_MAKS_FILER) == (15, 5, 100, 200)
    assert (config.KURSSIDE_ETTERTILGANG_DAGER, config.KURSSIDE_TOM_TABELLER_ETTER_DAGER) == (180, 30)


def test_bilde_over_6000_piksler_avvises(con, kid):
    with pytest.raises(Sidefeil) as e:
        sidelager.lagre_fil(con, kid, "kjempe.jpg", jpeg(6001, 100), ADMIN)
    assert e.value.status == 413
    assert sidelager.lagre_fil(con, kid, "grense.jpg", jpeg(6000, 6000), ADMIN)


def test_kvote_per_kurs_i_antall_og_stoerrelse(con, kid, monkeypatch):
    monkeypatch.setattr(config, "KURSSIDE_MAKS_FILER", 3)
    for n in range(3):
        sidelager.lagre_fil(con, kid, f"f{n}.pdf", pdf(bytes([n]) * 10), ADMIN)
    with pytest.raises(Sidefeil) as e:
        sidelager.lagre_fil(con, kid, "fire.pdf", pdf(b"x"), ADMIN)
    assert e.value.status == 409 and "3 filer" in e.value.melding
    k = sidelager.kvote(con, kid)
    assert k["antall"] == 3 and k["maks_antall"] == 3 and k["bytes"] == sum(f["storrelse"] for f in sidelager.fil_liste(con, kid))
    monkeypatch.setattr(config, "KURSSIDE_MAKS_FILER", 200)
    monkeypatch.setattr(config, "KURSSIDE_KURS_MAKS_MB", 1)
    andre = lag_kurs(con, "K2")
    sidelager.lagre_fil(con, andre, "a.pdf", pdf(b"0" * 600_000), ADMIN)
    with pytest.raises(Sidefeil) as e:
        sidelager.lagre_fil(con, andre, "b.pdf", pdf(b"1" * 600_000), ADMIN)
    assert e.value.status == 409 and "1 MB" in e.value.melding
    assert sidelager.lagre_fil(con, kid, "annet-kurs.pdf", pdf(b"2" * 600_000), ADMIN)   # kvoten gjelder per kurs


def test_samme_innhold_to_ganger_i_samme_kurs_blir_en_rad(con, kid):
    a, d1 = sidelager.lagre_fil(con, kid, "foerste.pdf", pdf(), ADMIN)
    b, d2 = sidelager.lagre_fil(con, kid, "annet-navn.pdf", pdf(), ADMIN)
    assert (a, d1, d2) == (b, False, True) and len(sidelager.fil_liste(con, kid)) == 1
    assert sidelager.fil_meta(con, kid, a)["filnavn"] == "foerste.pdf"                      # navnet fra første opplasting beholdes
    andre = lag_kurs(con, "K2")
    c, d3 = sidelager.lagre_fil(con, andre, "foerste.pdf", pdf(), ADMIN)
    assert d3 is False and c != a                                                          # samme innhold i et annet kurs er en egen rad


def test_duplikat_teller_ikke_mot_kvoten(con, kid, monkeypatch):
    monkeypatch.setattr(config, "KURSSIDE_MAKS_FILER", 1)
    a, _ = sidelager.lagre_fil(con, kid, "a.pdf", pdf(), ADMIN)
    assert sidelager.lagre_fil(con, kid, "b.pdf", pdf(), ADMIN) == (a, True)


@pytest.mark.parametrize("inn, ut", [
    ("Dag 1 – Grunnmodell.pdf", "Dag 1 – Grunnmodell.pdf"), ("Øvelse på Åsane.pdf", "Øvelse på Åsane.pdf"),
    ("../../etc/passwd.pdf", "_.._etc_passwd.pdf"), ("C:\\Windows\\fil.pdf", "C_Windows_fil.pdf"), ("a/b\\c.pdf", "a_b_c.pdf"),
    ('fil"med*farlige?tegn<>|.pdf', "fil_med_farlige_tegn_.pdf"), ("kontroll\x00\x1f\x7ftegn.pdf", "kontroll_tegn.pdf"), ("  .skjult.pdf  ", "skjult.pdf"),
    ("a" * 200 + ".pdf", "a" * 116 + ".pdf")])
def test_filnavnet_renses_men_aesoeaa_beholdes(con, kid, inn, ut):
    meta = sidelager.fil_meta(con, kid, sidelager.lagre_fil(con, kid, inn, pdf(), ADMIN)[0])
    assert meta["filnavn"] == ut and len(meta["filnavn"]) <= 120


def test_innholdet_er_base64_i_egen_tabell_og_lister_har_aldri_innhold(con, kid):
    data = pdf(b"hemmelig innhold")
    fil_id, _ = sidelager.lagre_fil(con, kid, "a.pdf", data, ADMIN)
    rad = con.execute("SELECT innhold FROM kursside_fil_innhold WHERE fil_id=?", (fil_id,)).fetchone()
    assert base64.b64decode(rad["innhold"]) == data
    assert "innhold" not in con.execute("SELECT * FROM kursside_fil WHERE id=?", (fil_id,)).fetchone().keys()
    for f in sidelager.fil_liste(con, kid):
        assert "innhold" not in f and "hemmelig" not in json.dumps(f)
    assert "innhold" not in sidelager.fil_meta(con, kid, fil_id) and "innhold" not in sidelager.fil_innhold(con, kid, fil_id)[0]
    assert con.execute("SELECT sha256 FROM kursside_fil WHERE id=?", (fil_id,)).fetchone()[0] == hashlib.sha256(data).hexdigest()


def test_fil_fra_annet_kurs_kan_ikke_leses(con, kid):
    andre = lag_kurs(con, "K2")
    fil_id, _ = sidelager.lagre_fil(con, kid, "a.pdf", pdf(), ADMIN)
    assert sidelager.fil_innhold(con, andre, fil_id) is None and sidelager.fil_meta(con, andre, fil_id) is None
    assert sidelager.fil_liste(con, andre) == []


# ============================ slette og rydde filer ============================

def _side_med_fil(con, kid, fil_id, **felt):
    return dokument({"id": "b_filer001", "type": "filer", "tittel": "Presentasjoner", "data": {"filer": [{"fil_id": fil_id, "tittel": "P"}]}, **felt})


def test_fil_som_er_brukt_i_utkast_eller_publisert_kan_ikke_slettes(con, kid):
    fil_id, _ = sidelager.lagre_fil(con, kid, "a.pdf", pdf(), ADMIN)
    skriv_side(con, kid, _side_med_fil(con, kid, fil_id), publiser=False)
    assert sidelager.slett_fil(con, kid, fil_id, ADMIN) == "Brukes i blokken «Presentasjoner»"
    sidelager.publiser(con, kid, 1, ADMIN)
    sidelager.lagre_utkast(con, kid, _dok("Uten fil"), 1, ADMIN)                           # fjernet fra utkastet, men fortsatt publisert
    assert sidelager.slett_fil(con, kid, fil_id, ADMIN) == "Brukes i blokken «Presentasjoner»"
    assert sidelager.fil_meta(con, kid, fil_id) is not None
    sidelager.publiser(con, kid, 2, ADMIN)
    assert sidelager.slett_fil(con, kid, fil_id, ADMIN) is None
    assert sidelager.fil_meta(con, kid, fil_id) is None
    assert con.execute("SELECT COUNT(*) FROM kursside_fil_innhold WHERE fil_id=?", (fil_id,)).fetchone()[0] == 0


def test_slett_fil_uten_at_den_er_brukt_og_ukjent_fil(con, kid):
    fil_id, _ = sidelager.lagre_fil(con, kid, "a.pdf", pdf(), ADMIN)
    assert sidelager.slett_fil(con, lag_kurs(con, "K2"), fil_id, ADMIN) == "Filen finnes ikke."   # andres fil
    assert sidelager.slett_fil(con, kid, fil_id, ADMIN) is None and sidelager.slett_fil(con, kid, fil_id, ADMIN) == "Filen finnes ikke."


def _gjor_gammel(con, fil_id, dager=40):
    gammel = (datetime.now(timezone.utc) - timedelta(days=dager)).strftime("%Y-%m-%d %H:%M:%S")
    con.execute("UPDATE kursside_fil SET opprettet=? WHERE id=?", (gammel, fil_id))


def test_foreldrelose_filer_ryddes_bare_naar_de_er_gamle_og_ingen_bruker_dem(con, kid):
    ubrukt, _ = sidelager.lagre_fil(con, kid, "ubrukt.pdf", pdf(b"1"), ADMIN)
    ny, _ = sidelager.lagre_fil(con, kid, "ny.pdf", pdf(b"2"), ADMIN)
    brukt, _ = sidelager.lagre_fil(con, kid, "brukt.pdf", pdf(b"3"), ADMIN)
    i_versjon, _ = sidelager.lagre_fil(con, kid, "i-versjon.pdf", pdf(b"4"), ADMIN)
    skriv_side(con, kid, _side_med_fil(con, kid, i_versjon))                               # publisert versjon refererer til den
    sidelager.lagre_utkast(con, kid, _side_med_fil(con, kid, brukt), 1, ADMIN)             # utkastet bruker en annen; i_versjon er nå bare i en versjon
    sidelager.publiser(con, kid, 2, ADMIN)                                                 # ... og nå publisert: i_versjon bare i den gamle versjonen
    for fil in (ubrukt, brukt, i_versjon):
        _gjor_gammel(con, fil)
    assert sidelager.rydd_foreldrelose_filer(con, kid) == 1                                # bare «ubrukt» (ny er for ny)
    assert {f["filnavn"] for f in sidelager.fil_liste(con, kid)} == {"ny.pdf", "brukt.pdf", "i-versjon.pdf"}
    assert sidelager.rydd_foreldrelose_filer(con, kid) == 0                                # idempotent
    assert sidelager.rydd_foreldrelose_filer(con, kid, eldre_enn_dager=-1) == 1 and ny not in {f["id"] for f in sidelager.fil_liste(con, kid)}


def test_publisering_rydder_bort_gamle_filer_som_ingen_bruker(con, kid):
    ubrukt, _ = sidelager.lagre_fil(con, kid, "ubrukt.pdf", pdf(b"1"), ADMIN)
    _gjor_gammel(con, ubrukt)
    skriv_side(con, kid, _dok())
    assert sidelager.fil_meta(con, kid, ubrukt) is None


def test_kopier_fil_gjenbruker_lik_fil_og_kopierer_innholdet(con, kid):
    andre = lag_kurs(con, "K2")
    data = pdf(b"kopi")
    fil_id, _ = sidelager.lagre_fil(con, kid, "a.pdf", data, ADMIN)
    ny = sidelager.kopier_fil(con, kid, andre, fil_id, ADMIN)
    assert ny is not None and sidelager.fil_innhold(con, andre, ny)[1] == data and sidelager.fil_meta(con, andre, ny)["filnavn"] == "a.pdf"
    assert sidelager.kopier_fil(con, kid, andre, fil_id, ADMIN) == ny and len(sidelager.fil_liste(con, andre)) == 1
    assert sidelager.kopier_fil(con, kid, andre, 9999, ADMIN) is None
    assert sidelager.kopier_fil(con, andre, kid, ny, ADMIN) == fil_id                       # kursets egen rad gjenbrukes


def test_sletter_man_kurset_forsvinner_siden_filene_og_versjonene(con):
    kid = lag_kurs(con, "SLETT")
    fil_id, _ = sidelager.lagre_fil(con, kid, "a.pdf", pdf(), ADMIN)
    skriv_side(con, kid, _side_med_fil(con, kid, fil_id))
    assert con.execute("SELECT COUNT(*) FROM kursside_versjon WHERE kurs_id=?", (kid,)).fetchone()[0] == 1
    con.execute("DELETE FROM kurs WHERE id=?", (kid,))
    con.commit()
    for tabell, kolonne in (("kursside", "kurs_id"), ("kursside_versjon", "kurs_id"), ("kursside_fil", "kurs_id")):
        assert con.execute(f"SELECT COUNT(*) FROM {tabell} WHERE {kolonne}=?", (kid,)).fetchone()[0] == 0, tabell
    assert con.execute("SELECT COUNT(*) FROM kursside_fil_innhold").fetchone()[0] == 0


# ============================ deltakere og andre kurs ============================

def test_bekreftede_epost_og_deltakernavn_bare_for_bekreftede(con, kid):
    lag_deltaker(con, kid, "kari@example.no", "Kari", "Nordmann")
    lag_deltaker(con, kid, "ola@example.no", "Ola", "Hansen")
    lag_deltaker(con, kid, "aisha@example.no", "Aisha Marie", "rahman")
    lag_deltaker(con, kid, "borte@example.no", "Bo", "Borte", status="avmeldt")
    lag_deltaker(con, kid, "venter@example.no", "Vera", "Venteliste", status="venteliste")
    assert sidelager.bekreftede_epost(con, kid) == {"kari@example.no", "ola@example.no", "aisha@example.no"}
    assert sidelager.deltakernavn(con, kid) == ["Aisha Marie R.", "Kari N.", "Ola H."]
    assert sidelager.antall_bekreftede(con, kid) == 3


def test_kurs_med_side_lister_bare_kurs_med_side_utenom_dette(con, kid):
    andre, tredje = lag_kurs(con, "K2", start=IDAG + timedelta(days=30)), lag_kurs(con, "K3")
    assert sidelager.kurs_med_side(con, kid) == []
    sidelager.lagre_utkast(con, andre, _dok(), 0, ADMIN)
    sidelager.lagre_utkast(con, kid, _dok(), 0, ADMIN)
    liste = sidelager.kurs_med_side(con, kid)
    assert [(k["id"], k["navn"], k["start"]) for k in liste] == [(andre, "Kurs K2", (IDAG + timedelta(days=30)).isoformat())]
    assert tredje not in [k["id"] for k in liste]


# ============================ gruppelister etter kurset ============================

def _side_med_tabell(con, kid):
    dok = dokument({"id": "b_tabell01", "type": "tabell", "tittel": "Grupper", "data": {"kolonner": ["Gruppe", "Deltakere"], "rader": [["1", "Kari N."], ["2", "Ola H."]]}},
                   tekstblokk("Tekst", id="b_tekst001"))
    skriv_side(con, kid, dok)                                                              # versjon 1: publisert
    sidelager.lagre_utkast(con, kid, dok, 1, ADMIN)                                        # versjon 2: utkast med samme rader
    sidelager.erstatt_utkast(con, kid, dok, 2, ADMIN, "Sikkerhetskopi med rader")          # sikkerhetskopi av utkast med rader
    con.commit()


def _rader(json_tekst):
    return [b["data"]["rader"] for b in si.les(json_tekst)["blokker"] if b["type"] == "tabell"]


def test_gruppelister_toemmes_foerst_naar_siste_kursdag_er_mer_enn_30_dager_siden(con, kid):
    _side_med_tabell(con, kid)
    siste = sidelager.siste_kursdag(con, kid)
    assert sidelager.tom_gamle_tabeller(con, siste + timedelta(days=30), 30) == 0          # nøyaktig 30 dager: ikke ennå
    assert sidelager.tom_gamle_tabeller(con, siste + timedelta(days=31), 30, torr=True) == 1
    rad = sidelager.hent(con, kid)
    assert _rader(rad["utkast"]) == [[["1", "Kari N."], ["2", "Ola H."]]]                  # tørrkjøring endrer ingenting
    assert sidelager.tom_gamle_tabeller(con, siste + timedelta(days=31), 30) == 1
    rad = sidelager.hent(con, kid)
    assert _rader(rad["utkast"]) == [[]] and _rader(rad["publisert"]) == [[]]
    assert all(_rader(v["innhold"]) == [[]] for v in con.execute("SELECT innhold FROM kursside_versjon WHERE kurs_id=?", (kid,)).fetchall())
    assert si.les(rad["utkast"])["blokker"][0]["data"]["kolonner"] == ["Gruppe", "Deltakere"]     # kolonnene og andre blokker beholdes
    assert si.les(rad["utkast"])["blokker"][1]["tittel"] == "Tekst"


def test_tomming_av_gruppelister_er_idempotent_og_roerer_ikke_kurs_uten_rader_eller_uten_side(con, kid):
    andre = lag_kurs(con, "K2")                                                            # kurs uten side
    tredje = lag_kurs(con, "K3")
    skriv_side(con, tredje, dokument(tekstblokk()))                                        # side uten tabell
    _side_med_tabell(con, kid)
    langt_etter = sidelager.siste_kursdag(con, kid) + timedelta(days=400)
    versjon_foer = con.execute("SELECT versjon FROM kursside WHERE kurs_id=?", (tredje,)).fetchone()[0]
    assert sidelager.tom_gamle_tabeller(con, langt_etter, 30) == 1
    versjon_etter_forste = con.execute("SELECT versjon FROM kursside WHERE kurs_id=?", (kid,)).fetchone()[0]
    assert sidelager.tom_gamle_tabeller(con, langt_etter, 30) == 0                         # andre gang: ingenting
    assert con.execute("SELECT versjon FROM kursside WHERE kurs_id=?", (kid,)).fetchone()[0] == versjon_etter_forste
    assert con.execute("SELECT versjon FROM kursside WHERE kurs_id=?", (tredje,)).fetchone()[0] == versjon_foer
    assert sidelager.hent(con, andre) is None
