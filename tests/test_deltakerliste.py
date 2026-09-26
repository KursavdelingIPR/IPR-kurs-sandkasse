"""Deltakerliste med kolonnevalg (utskrift/PDF og CSV): personvern, filtrering, sortering, roller og logging."""
import csv
import io
import json
from datetime import date, timedelta

import pytest
from werkzeug.datastructures import MultiDict

from kurs import config, db, deltakerliste
from navnehjelp import navnedeler


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


FIKTIVE = (("Øystein Test", "oystein@eksempel.no", "90000001"), ("Åse Test", "aase@eksempel.no", "90000002"),
           ("Anders Test", "anders@eksempel.no", "90000003"), ("Venter Test", "venter@eksempel.no", "90000004"))


def _kurs(con):
    """Kapasitet 3: de tre første blir bekreftet, den fjerde havner på venteliste. Alle har en (fiktiv) allergi."""
    start = date.today() + timedelta(days=20)
    kid = db.opprett_kurs(con, kode="DL1", navn="Listekurs i EFT", sharepoint_mappe="Kurs/DL1", kapasitet=3,
                          pris_nok=1000, type="fysisk", sted="Oslo",
                          datoer=[start.isoformat(), (start + timedelta(days=1)).isoformat()])
    pids = {}
    for navn, epost, tlf in FIKTIVE:
        pids[navn], _ = db.meld_paa(con, kid, epost=epost, **navnedeler(navn),
                                    deltaker={"telefon": tlf, "arbeidssted": "Testklinikken"},
                                    sensitivt={"allergier": "Hemmelig-nøtteallergi"})
    con.commit()
    return kid, pids


def _tabell(html: str) -> str:
    return html.split('<table class="liste">')[1].split("</table>")[0]


def _csv(tekst: str) -> list[list[str]]:
    assert tekst.startswith("﻿")                   # BOM -> Excel leser æøå riktig
    return list(csv.reader(io.StringIO(tekst[1:]), delimiter=";"))


# ============================ valg (ren logikk) ============================

def test_les_valg_standard_er_nr_og_navn_og_bekreftede():
    v = deltakerliste.les_valg(MultiDict())
    assert v.nokler == ["nr", "navn"] and v.status == "bekreftet"


def test_les_valg_ignorerer_ukjente_kolonner_og_statuser():
    v = deltakerliste.les_valg(MultiDict([("valgt", "1"), ("kol", "telefon"), ("kol", "allergier"),
                                          ("kol", "finnes-ikke"), ("status", "tull")]))
    assert v.nokler == ["navn", "telefon"] and v.status == "bekreftet"


def test_les_valg_alt_krysset_bort_gir_bare_navn():
    assert deltakerliste.les_valg(MultiDict([("valgt", "1")])).nokler == ["navn"]


def test_sensitive_felt_finnes_ikke_i_katalogen():
    assert not {"allergier", "tilrettelegging", "sensitivt"} & {k.nokkel for k in deltakerliste.KOLONNER}


def test_norsk_alfabetisk_rekkefolge():
    navn = ["Åse", "Øystein", "Zara", "Ærlig", "Élise", "anders"]
    assert sorted(navn, key=deltakerliste._sortering) == ["anders", "Élise", "Zara", "Ærlig", "Øystein", "Åse"]


# ============================ utskriftsvisning ============================

def test_standard_viser_bare_nr_og_navn_for_bekreftede_i_norsk_rekkefolge(con):
    kid, _ = _kurs(con)
    html = _admin().get(f"/admin/kurs/{kid}/deltakerliste").get_data(as_text=True)
    t = _tabell(html)
    assert '<th scope="col" class="nr">Nr.</th>' in t and ">Navn</th>" in t
    assert "@eksempel.no" not in html and "9000000" not in html and "Testklinikken" not in html
    assert "Venter Test" not in t
    assert t.index("Anders Test") < t.index("Øystein Test") < t.index("Åse Test")


def test_personopplysninger_vises_bare_naar_de_er_krysset_av(con):
    kid, _ = _kurs(con)
    html = _admin().get(f"/admin/kurs/{kid}/deltakerliste?valgt=1&kol=epost&kol=telefon").get_data(as_text=True)
    t = _tabell(html)
    assert "oystein@eksempel.no" in t and "90000001" in t
    assert "Testklinikken" not in html and ">Nr.</th>" not in t


def test_allergier_kommer_aldri_med_selv_om_adressen_ber_om_det(con):
    kid, _ = _kurs(con)
    k = _admin()
    for url in (f"/admin/kurs/{kid}/deltakerliste?valgt=1&kol=allergier&kol=tilrettelegging&status=alle",
                f"/admin/kurs/{kid}/deltakerliste.csv?valgt=1&kol=allergier&kol=tilrettelegging&status=alle"):
        r = k.get(url)
        assert r.status_code == 200 and "Hemmelig-nøtteallergi" not in r.get_data(as_text=True)


@pytest.mark.parametrize("status,med,uten", [
    ("venteliste", ["Venter Test"], ["Anders Test"]),
    ("alle", ["Venter Test", "Anders Test"], []),
    ("tull", ["Anders Test"], ["Venter Test"]),            # ugyldig -> bekreftede
])
def test_statusfilter(con, status, med, uten):
    kid, _ = _kurs(con)
    t = _tabell(_admin().get(f"/admin/kurs/{kid}/deltakerliste?status={status}").get_data(as_text=True))
    assert all(n in t for n in med) and not any(n in t for n in uten)


def test_avmeldte_er_ikke_med_som_standard(con):
    kid, pids = _kurs(con)
    db.meld_av(con, pids["Anders Test"])
    con.commit()
    k = _admin()
    assert "Anders Test" not in _tabell(k.get(f"/admin/kurs/{kid}/deltakerliste").get_data(as_text=True))
    assert "Anders Test" in _tabell(k.get(f"/admin/kurs/{kid}/deltakerliste?status=avmeldt").get_data(as_text=True))


def test_utskriften_starter_med_kursnavn_kursnr_datoer_og_sted(con):
    kid, _ = _kurs(con)
    html = _admin().get(f"/admin/kurs/{kid}/deltakerliste").get_data(as_text=True)
    kursnr = con.execute("SELECT kursnr FROM kurs WHERE id=?", (kid,)).fetchone()[0]
    utskrift = html.split('<section class="deltakerliste">')[1]
    assert utskrift.index("Listekurs i EFT") < utskrift.index("<table")
    assert f"Kursnr. {kursnr}" in utskrift and "(2 kursdager)" in utskrift and "Oslo" in utskrift
    assert "Bekreftede: 3" in utskrift
    kontroller = html.split('<section class="deltakerliste">')[0]
    assert 'class="ikke-utskrift"' in kontroller and "data-skriv-ut" in kontroller   # skjules ved utskrift


def test_oppmote_signatur_og_merknad_i_utskrift(con):
    kid, pids = _kurs(con)
    dager = db.kursdager(con, kid)
    db.registrer_oppmote(con, pids["Anders Test"], dager[0]["id"], "manuell")
    con.commit()
    t = _tabell(_admin().get(f"/admin/kurs/{kid}/deltakerliste?valgt=1&kol=oppmote&kol=signatur&kol=merknad")
                .get_data(as_text=True))
    d0 = dager[0]["dato"]
    assert f'<th scope="col" class="avkrysning">{d0[8:10]}.{d0[5:7]}.</th>' in t
    assert '<th scope="col" class="signatur">Signatur</th>' in t and '<th scope="col" class="merknad">Merknad</th>' in t
    anders = t.split("Anders Test")[1].split("</tr>")[0]
    assert anders.count('<td class="avkrysning">✓</td>') == 1 and anders.count('<td class="avkrysning"></td>') == 1


def test_logo_vises_bare_naar_godkjent_fil_finnes(con, tmp_path, monkeypatch):
    kid, _ = _kurs(con)
    monkeypatch.setattr(deltakerliste, "LOGO_MAPPE", tmp_path / "logo")
    k = _admin()
    assert '<img class="liste-logo"' not in k.get(f"/admin/kurs/{kid}/deltakerliste").get_data(as_text=True)
    (tmp_path / "logo").mkdir()
    (tmp_path / "logo" / "deltakerliste.png").write_bytes(b"\x89PNG\r\n")
    html = k.get(f"/admin/kurs/{kid}/deltakerliste").get_data(as_text=True)
    assert '<img class="liste-logo" src="/static/logo/deltakerliste.png" alt="Logo">' in html


def test_lenke_fra_deltakersiden(con):
    kid, _ = _kurs(con)
    html = _admin().get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    assert f'href="/admin/kurs/{kid}/deltakerliste"' in html


def test_krever_innlogging_og_eksisterende_kurs(con):
    from kurs.web import app as webapp
    kid, _ = _kurs(con)
    anonym = webapp.app.test_client()
    for url in (f"/admin/kurs/{kid}/deltakerliste", f"/admin/kurs/{kid}/deltakerliste.csv"):
        assert anonym.get(url).status_code == 302
    k = _admin()
    assert k.get("/admin/kurs/99999/deltakerliste").status_code == 404
    assert k.get("/admin/kurs/99999/deltakerliste.csv").status_code == 404


# ============================ CSV ============================

def test_csv_har_valgte_kolonner_oppmote_per_dag_og_ingen_utfyllingskolonner(con):
    kid, pids = _kurs(con)
    dager = db.kursdager(con, kid)
    db.registrer_oppmote(con, pids["Anders Test"], dager[0]["id"], "manuell")
    con.commit()
    r = _admin().get(f"/admin/kurs/{kid}/deltakerliste.csv?valgt=1&kol=nr&kol=epost&kol=oppmote&kol=signatur"
                     "&kol=merknad")
    assert r.status_code == 200 and r.mimetype == "text/csv" and r.headers["Cache-Control"] == "no-store"
    kursnr = con.execute("SELECT kursnr FROM kurs WHERE id=?", (kid,)).fetchone()[0]
    assert r.headers["Content-Disposition"] == f"attachment; filename=deltakerliste_{kursnr}.csv"
    rader = _csv(r.get_data(as_text=True))
    assert rader[0] == ["Nr.", "Navn", "E-post"] + [f"Oppmøte {d['dato']}" for d in dager]
    assert rader[1] == ["1", "Anders Test", "anders@eksempel.no", "ja", ""]
    assert len(rader) == 4                                    # overskrift + 3 bekreftede


def test_csv_noytraliserer_formler(con):
    kid, _ = _kurs(con)
    db.meld_paa(con, kid, epost="formel@eksempel.no", fornavn="=1+2", etternavn="Test")
    con.commit()
    rader = _csv(_admin().get(f"/admin/kurs/{kid}/deltakerliste.csv?status=alle").get_data(as_text=True))
    assert ["1", "'=1+2 Test"] in rader


def test_csv_eksport_logges_uten_personopplysninger(con):
    kid, _ = _kurs(con)
    _admin().get(f"/admin/kurs/{kid}/deltakerliste.csv?valgt=1&kol=epost&kol=telefon")
    rad = con.execute("SELECT aktor, detaljer FROM hendelse WHERE handling='deltakerliste_eksportert'").fetchone()
    assert json.loads(rad["detaljer"]) == {"kurs_id": kid, "status": "bekreftet", "kolonner": ["navn", "epost", "telefon"],
                                           "antall": 3}
    assert "@" not in rad["detaljer"] and "9000000" not in rad["detaljer"]
    assert rad["aktor"] == f"admin:{config.ADMIN_BRUKERNAVN}"


def test_lesetilgang_kan_se_listen_men_ikke_laste_ned_csv(con):
    kid, _ = _kurs(con)
    db.opprett_admin_bruker(con, "leser", "Leser", "passord-som-holder", rolle="lese")
    con.commit()
    k = _admin("leser", "passord-som-holder")
    html = k.get(f"/admin/kurs/{kid}/deltakerliste").get_data(as_text=True)
    assert "Anders Test" in html and "deltakerliste.csv" not in html
    assert k.get(f"/admin/kurs/{kid}/deltakerliste.csv").status_code == 403
