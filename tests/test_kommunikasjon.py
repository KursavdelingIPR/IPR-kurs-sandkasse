"""Fase 5: manuell e-post fra admin - kompose/forhaandsvisning/send, dedup og Kommunikasjon-fanene."""
import json
import re
from datetime import date, timedelta

import pytest

from kurs import config, db


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    return c


def _klient():
    from kurs.web import app as webapp
    return webapp.app.test_client()


def _logg_inn(klient):
    klient.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})


def _kurs(con, kode="T1", start=None, **kw):
    start = start or (date.today() + timedelta(days=30))
    return db.opprett_kurs(con, kode=kode, navn="Testkurs", datoer=[start.isoformat()],
                           sharepoint_mappe=f"Kurs/{kode}", **{"pris_nok": 1000, **kw})


def _fersk(con):
    return db.koble(config.DB_STI)


def _utsending_id(html: str) -> str:
    return re.search(r'name="utsending_id" value="(\d+)"', html).group(1)


def _forhandsvis(klient, kid, emne, tekst, paamelding_ider):
    return klient.post(f"/admin/kurs/{kid}/epost/forhandsvis",
                       data={"emne": emne, "tekst": tekst, "paamelding_id": [str(i) for i in paamelding_ider]})


# ---------------- kompose ----------------

def test_gruppe_bekreftet_og_venteliste_gir_riktige_mottakere(con):
    kid = _kurs(con, kapasitet=1)
    db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Bekreftet")
    db.meld_paa(con, kid, epost="b@x.no", fornavn="B", etternavn="Venteliste")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t1 = klient.get(f"/admin/kurs/{kid}/epost/ny?gruppe=bekreftet").get_data(as_text=True)
    assert "A Bekreftet" in t1 and "B Venteliste" not in t1
    t2 = klient.get(f"/admin/kurs/{kid}/epost/ny?gruppe=venteliste").get_data(as_text=True)
    assert "B Venteliste" in t2 and "A Bekreftet" not in t2


def test_ingen_mottakere_redirigerer_med_feilmelding(con):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = klient.get(f"/admin/kurs/{kid}/epost/ny", follow_redirects=True)
    assert "velg minst" in r.get_data(as_text=True).lower()


def test_mottaker_id_fra_annet_kurs_filtreres_bort_paa_serveren(con):
    kid1 = _kurs(con, "K1")
    kid2 = _kurs(con, "K2")
    p1, _ = db.meld_paa(con, kid1, epost="a@x.no", fornavn="A Riktig", etternavn="Kurs")
    p2, _ = db.meld_paa(con, kid2, epost="c@x.no", fornavn="C Feil", etternavn="Kurs")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = _forhandsvis(klient, kid1, "Emne", "Tekst", [p1, p2])
    tekst = r.get_data(as_text=True)
    assert "A Riktig Kurs" in tekst
    assert "C Feil Kurs" not in tekst


def test_for_lang_emne_og_tekst_avvises(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r1 = _forhandsvis(klient, kid, "x" * 201, "kort tekst", [pid])
    assert "maks 200 tegn" in r1.get_data(as_text=True)
    r2 = _forhandsvis(klient, kid, "Kort emne", "x" * 5001, [pid])
    assert "maks 5000 tegn" in r2.get_data(as_text=True)


def test_tom_emne_eller_tekst_avvises(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = _forhandsvis(klient, kid, "", "", [pid])
    tekst = r.get_data(as_text=True)
    assert "Fyll inn et emne" in tekst and "Fyll inn en melding" in tekst
    assert _fersk(con).execute("SELECT COUNT(*) FROM admin_utsending").fetchone()[0] == 0


def test_rediger_etter_forhaandsvisning_beholder_verdiene(con):
    """«Tilbake og rediger» = feltene er allerede utfylt og redigerbare i samme skjema."""
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = _forhandsvis(klient, kid, "Mitt emne", "Min tekst", [pid])
    tekst = r.get_data(as_text=True)
    assert 'value="Mitt emne"' in tekst
    assert "Min tekst</textarea>" in tekst
    assert f'value="{pid}"' in tekst  # mottaker fulgte ogsaa med


# ---------------- forhaandsvisning oppretter utsendelse (ikke sender) ----------------

def test_forhaandsvisning_oppretter_utsending_men_sender_ingenting(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    _forhandsvis(klient, kid, "Emne", "Tekst", [pid])
    fersk = _fersk(con)
    assert fersk.execute("SELECT COUNT(*) FROM admin_utsending").fetchone()[0] == 1
    assert fersk.execute("SELECT COUNT(*) FROM utsending_logg WHERE type='admin_epost'").fetchone()[0] == 0
    assert not (config.UTBOKS.exists() and list(config.UTBOKS.glob("*.html")))


def test_teksten_lagres_kun_en_gang_ikke_pr_mottaker(con):
    kid = _kurs(con)
    p1, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    p2, _ = db.meld_paa(con, kid, epost="b@x.no", fornavn="B", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    _forhandsvis(klient, kid, "Emne", "En ganske lang tekst som ikke skal gjentas", [p1, p2])
    fersk = _fersk(con)
    assert fersk.execute("SELECT COUNT(*) FROM admin_utsending").fetchone()[0] == 1
    assert fersk.execute("SELECT COUNT(*) FROM admin_utsending_mottaker").fetchone()[0] == 2


# ---------------- send ----------------

def test_send_gir_en_epost_pr_mottaker_ingen_ser_andres_adresse(con):
    kid = _kurs(con)
    p1, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    p2, _ = db.meld_paa(con, kid, epost="b@x.no", fornavn="B", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = _forhandsvis(klient, kid, "Emne", "Tekst", [p1, p2])
    uid = _utsending_id(r.get_data(as_text=True))
    klient.post(f"/admin/kurs/{kid}/epost/send", data={"utsending_id": uid})

    filer = list(config.UTBOKS.glob("*.html"))
    assert len(filer) == 2
    for fil in filer:
        forste, _, _ = fil.read_text(encoding="utf-8").partition("\n")
        meta = json.loads(forste.removeprefix("<!--META ").removesuffix(" -->"))
        assert meta["til"] in ("a@x.no", "b@x.no")
        # kun EN mottaker pr fil/epost - de to andre filene i utboks er de andre "til"-adressene, ikke i SAMME fil
    adresser = {json.loads(f.read_text(encoding="utf-8").partition("\n")[0]
                           .removeprefix("<!--META ").removesuffix(" -->"))["til"] for f in filer}
    assert adresser == {"a@x.no", "b@x.no"}


def test_send_logger_hendelse_og_utsending_logg_med_admin(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    admin_id = con.execute("SELECT id FROM admin_bruker WHERE brukernavn=?", (config.ADMIN_BRUKERNAVN,)).fetchone()[0]
    klient = _klient()
    _logg_inn(klient)
    r = _forhandsvis(klient, kid, "Emne", "Tekst", [pid])
    uid = _utsending_id(r.get_data(as_text=True))
    klient.post(f"/admin/kurs/{kid}/epost/send", data={"utsending_id": uid})

    fersk = _fersk(con)
    utsending = fersk.execute("SELECT * FROM admin_utsending WHERE id=?", (uid,)).fetchone()
    assert utsending["sendt_av_admin_id"] == admin_id
    logg_rad = fersk.execute("SELECT * FROM utsending_logg WHERE type='admin_epost'").fetchone()
    assert logg_rad["nokkel"] == utsending["nokkel"]
    hendelse = fersk.execute("SELECT * FROM hendelse WHERE handling='admin_epost_sendt'").fetchone()
    assert f'"paamelding_id": {pid}' in hendelse["detaljer"]


def test_dobbelklikk_paa_send_sender_ikke_to_ganger(con):
    """Nokkelen opprettes ved forhaandsvisning og gjenbrukes - dobbelt POST til /send skal IKKE duplisere."""
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = _forhandsvis(klient, kid, "Emne", "Tekst", [pid])
    uid = _utsending_id(r.get_data(as_text=True))

    r1 = klient.post(f"/admin/kurs/{kid}/epost/send", data={"utsending_id": uid}, follow_redirects=True)
    r2 = klient.post(f"/admin/kurs/{kid}/epost/send", data={"utsending_id": uid}, follow_redirects=True)  # "refresh"

    assert "sendt til 1 mottaker" in r1.get_data(as_text=True).lower()
    assert "allerede sendt" in r2.get_data(as_text=True).lower()
    fersk = _fersk(con)
    assert fersk.execute("SELECT COUNT(*) FROM utsending_logg WHERE type='admin_epost'").fetchone()[0] == 1
    assert fersk.execute("SELECT COUNT(*) FROM hendelse WHERE handling='admin_epost_sendt'").fetchone()[0] == 1
    assert len(list(config.UTBOKS.glob("*.html"))) == 1


def test_ny_utsendelse_med_samme_tekst_til_samme_person_blokkeres_ikke(con):
    """I motsetning til automatiske e-poster skal IKKE samme melding til samme person blokkeres
    naar det er en helt NY utsendelse (ny nokkel) - bare selve dobbeltsendingen skal hindres."""
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)

    r1 = _forhandsvis(klient, kid, "Samme emne", "Samme tekst", [pid])
    uid1 = _utsending_id(r1.get_data(as_text=True))
    klient.post(f"/admin/kurs/{kid}/epost/send", data={"utsending_id": uid1})

    r2 = _forhandsvis(klient, kid, "Samme emne", "Samme tekst", [pid])  # ny, separat utsendelse
    uid2 = _utsending_id(r2.get_data(as_text=True))
    assert uid2 != uid1
    r3 = klient.post(f"/admin/kurs/{kid}/epost/send", data={"utsending_id": uid2}, follow_redirects=True)
    assert "sendt til 1 mottaker" in r3.get_data(as_text=True).lower()
    assert len(list(config.UTBOKS.glob("*.html"))) == 2


def test_ukjent_utsending_id_haandteres_paent(con):
    kid = _kurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = klient.post(f"/admin/kurs/{kid}/epost/send", data={"utsending_id": "99999"}, follow_redirects=True)
    assert r.status_code == 200
    assert "fant ikke" in r.get_data(as_text=True).lower()


# ---------------- kommunikasjon-fanene ----------------

def test_kursnivaa_kommunikasjon_viser_manuell_utsendelse_gruppert(con):
    kid = _kurs(con)
    p1, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    p2, _ = db.meld_paa(con, kid, epost="b@x.no", fornavn="B", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = _forhandsvis(klient, kid, "Viktig melding", "Tekst", [p1, p2])
    uid = _utsending_id(r.get_data(as_text=True))
    klient.post(f"/admin/kurs/{kid}/epost/send", data={"utsending_id": uid})

    tekst = klient.get(f"/admin/kurs/{kid}/kommunikasjon").get_data(as_text=True)
    assert "Viktig melding" in tekst
    assert config.ADMIN_BRUKERNAVN in tekst or "Standardbruker" in tekst


def test_deltaker_kommunikasjon_viser_kun_egne_meldinger(con):
    kid = _kurs(con)
    p1, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    p2, _ = db.meld_paa(con, kid, epost="b@x.no", fornavn="B", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = _forhandsvis(klient, kid, "Kun til A", "Tekst", [p1])  # kun A er mottaker
    uid = _utsending_id(r.get_data(as_text=True))
    klient.post(f"/admin/kurs/{kid}/epost/send", data={"utsending_id": uid})

    tekst_a = klient.get(f"/admin/kurs/{kid}/deltaker/{p1}/kommunikasjon").get_data(as_text=True)
    tekst_b = klient.get(f"/admin/kurs/{kid}/deltaker/{p2}/kommunikasjon").get_data(as_text=True)
    assert "Kun til A" in tekst_a
    assert "Kun til A" not in tekst_b


def test_send_til_en_deltaker_knapp_finnes_paa_deltaker_kommunikasjon(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    tekst = klient.get(f"/admin/kurs/{kid}/deltaker/{pid}/kommunikasjon").get_data(as_text=True)
    assert f"/admin/kurs/{kid}/epost/ny?kurs_id={kid}&amp;paamelding_id={pid}" in tekst \
        or f"paamelding_id={pid}" in tekst


# ---------------- justering 1: tydeligere eksempel + valgfri mottaker ----------------

def test_forhaandsvisning_viser_tydelig_hvem_som_er_eksempel(con):
    kid = _kurs(con)
    pa, _ = db.meld_paa(con, kid, epost="aisha@x.no", fornavn="Aisha", etternavn="Rahman")
    pi, _ = db.meld_paa(con, kid, epost="ingrid@x.no", fornavn="Ingrid", etternavn="Solheim")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = _forhandsvis(klient, kid, "Info", "Velkommen", [pa, pi])
    assert "Forhåndsvisning for Aisha Rahman" in r.get_data(as_text=True)


def test_kan_bytte_eksempelmottaker_i_forhaandsvisning(con):
    kid = _kurs(con)
    pa, _ = db.meld_paa(con, kid, epost="aisha@x.no", fornavn="Aisha", etternavn="Rahman")
    pi, _ = db.meld_paa(con, kid, epost="ingrid@x.no", fornavn="Ingrid", etternavn="Solheim")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = klient.post(f"/admin/kurs/{kid}/epost/forhandsvis", data={
        "emne": "Info", "tekst": "Hei {fornavn},\n\nVelkommen", "paamelding_id": [str(pa), str(pi)],
        "eksempel_paamelding_id": str(pi)})
    tekst = r.get_data(as_text=True)
    assert "Forhåndsvisning for Ingrid Solheim" in tekst
    assert "Hei Ingrid," in tekst  # selve eksempel-HTML-en er ogsaa personalisert ({fornavn} til Ingrid)


def test_hver_mottaker_faar_faktisk_sitt_eget_navn_ved_sending(con):
    kid = _kurs(con)
    pa, _ = db.meld_paa(con, kid, epost="aisha@x.no", fornavn="Aisha", etternavn="Rahman")
    pi, _ = db.meld_paa(con, kid, epost="ingrid@x.no", fornavn="Ingrid", etternavn="Solheim")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = _forhandsvis(klient, kid, "Info", "Hei {fornavn},\n\nVelkommen", [pa, pi])
    uid = _utsending_id(r.get_data(as_text=True))
    klient.post(f"/admin/kurs/{kid}/epost/send", data={"utsending_id": uid})

    tekster = {f.name: f.read_text(encoding="utf-8") for f in config.UTBOKS.glob("*.html")}
    innhold = list(tekster.values())
    assert any("Hei Aisha," in t for t in innhold)
    assert any("Hei Ingrid," in t for t in innhold)
    # og ingen av filene skal inneholde BEGGE navnene (bekrefter at de er separate, personaliserte e-poster)
    assert not any("Hei Aisha," in t and "Hei Ingrid," in t for t in innhold)


# ---------------- justering 2: aapne en sendt e-post i etterkant ----------------

def test_detaljside_viser_emne_tekst_avsender_tidspunkt_og_mottakere(con):
    kid = _kurs(con)
    p1, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Deltaker")
    p2, _ = db.meld_paa(con, kid, epost="b@x.no", fornavn="B", etternavn="Deltaker")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = _forhandsvis(klient, kid, "Detaljert emne", "Hele meldingsteksten står her.", [p1, p2])
    uid = _utsending_id(r.get_data(as_text=True))
    klient.post(f"/admin/kurs/{kid}/epost/send", data={"utsending_id": uid})

    tekst = klient.get(f"/admin/kurs/{kid}/epost/{uid}").get_data(as_text=True)
    assert "Detaljert emne" in tekst
    assert "Hele meldingsteksten står her." in tekst
    assert config.ADMIN_BRUKERNAVN in tekst or "Standardbruker" in tekst
    assert "A Deltaker" in tekst and "a@x.no" in tekst
    assert "B Deltaker" in tekst and "b@x.no" in tekst


def test_aldri_sendt_utsendelse_gir_404_paa_detaljsiden(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = _forhandsvis(klient, kid, "Aldri sendt", "Tekst", [pid])
    uid = _utsending_id(r.get_data(as_text=True))
    assert klient.get(f"/admin/kurs/{kid}/epost/{uid}").status_code == 404


def test_utsendelse_fra_annet_kurs_gir_404(con):
    kid1 = _kurs(con, "K1")
    kid2 = _kurs(con, "K2")
    pid, _ = db.meld_paa(con, kid1, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = _forhandsvis(klient, kid1, "Emne", "Tekst", [pid])
    uid = _utsending_id(r.get_data(as_text=True))
    klient.post(f"/admin/kurs/{kid1}/epost/send", data={"utsending_id": uid})
    assert klient.get(f"/admin/kurs/{kid2}/epost/{uid}").status_code == 404


def test_emne_er_klikkbart_i_kursnivaa_kommunikasjon(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = _forhandsvis(klient, kid, "Klikk meg", "Tekst", [pid])
    uid = _utsending_id(r.get_data(as_text=True))
    klient.post(f"/admin/kurs/{kid}/epost/send", data={"utsending_id": uid})
    tekst = klient.get(f"/admin/kurs/{kid}/kommunikasjon").get_data(as_text=True)
    assert f'href="/admin/kurs/{kid}/epost/{uid}"' in tekst


def test_emne_er_klikkbart_paa_deltaker_kommunikasjon(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = _forhandsvis(klient, kid, "Klikk meg", "Tekst", [pid])
    uid = _utsending_id(r.get_data(as_text=True))
    klient.post(f"/admin/kurs/{kid}/epost/send", data={"utsending_id": uid})
    tekst = klient.get(f"/admin/kurs/{kid}/deltaker/{pid}/kommunikasjon").get_data(as_text=True)
    kopi = re.search(rf'href="(/admin/kurs/{kid}/deltaker/{pid}/epost/\d+)"[^>]*>Klikk meg</a>', tekst)
    assert kopi                                        # emnet åpner e-posten slik den ble sendt ...
    assert f'href="/admin/kurs/{kid}/epost/{uid}"' in klient.get(kopi[1]).get_data(as_text=True)  # ... og utsendelsen


# ---------------- kun status='sendt' vises som sendt (trinn 2.5, sluttkontroll) ----------------

@pytest.mark.parametrize("status,skal_vises", [("sendt", True), ("reservert", False), ("ukjent", False), ("feilet", False)])
def test_kun_sendt_status_vises_som_sendt_i_alle_kommunikasjonsvisninger(con, status, skal_vises):
    """Fire lesere av utsending_logg: deltakerens kommunikasjon, kursets automatiske OG manuelle
    kommunikasjon, og detaljsiden for en manuell utsendelse. En reservert/ukjent/feilet rad skal
    aldri presenteres som en sendt melding - hverken automatisk eller manuell."""
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", fornavn="A", etternavn="Test")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = _forhandsvis(klient, kid, "Manuelt emne", "Tekst", [pid])
    uid = _utsending_id(r.get_data(as_text=True))
    klient.post(f"/admin/kurs/{kid}/epost/send", data={"utsending_id": uid})  # gir en 'sendt'-rad (adhoc-nokkel)
    con.execute("INSERT INTO utsending_logg (nokkel, mottaker, type, status) VALUES (?,?,?,?)",
                (f"kurs:{kid}", "a@x.no", "autotype", status))
    con.execute("UPDATE utsending_logg SET status=? WHERE nokkel LIKE 'adhoc:%'", (status,))
    con.execute("UPDATE sendt_epost SET status=?", ({"reservert": "sender"}.get(status, status),))
    con.commit()

    deltaker = klient.get(f"/admin/kurs/{kid}/deltaker/{pid}/kommunikasjon").get_data(as_text=True)
    kurs_side = klient.get(f"/admin/kurs/{kid}/kommunikasjon").get_data(as_text=True)
    detalj = klient.get(f"/admin/kurs/{kid}/epost/{uid}")

    merke = {"sendt": "Sendt", "reservert": "Sendes nå", "ukjent": "Uavklart", "feilet": "Feilet"}[status]
    assert "Manuelt emne" in deltaker and "autotype" in deltaker      # E-poster-fanen viser alle forsøk ...
    assert deltaker.count(f">{merke}</span>") == 2                    # ... med riktig status ...
    assert (">Sendt</span>" in deltaker) is skal_vises                # ... og aldri som sendt når de ikke er det
    assert ("Manuelt emne" in kurs_side) is skal_vises and ("autotype" in kurs_side) is skal_vises
    assert (detalj.status_code == 200) is skal_vises  # ellers 404: aldri vist som sendt
    if not skal_vises:
        assert "Ingen manuell e-post sendt for dette kurset ennå." in kurs_side
        assert "Ingen automatisk e-post sendt for dette kurset ennå." in kurs_side
