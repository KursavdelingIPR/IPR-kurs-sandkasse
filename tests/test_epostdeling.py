"""Deling av e-post (Camilla 05.10.2026): spørsmålet «Deling av e-post» står i alle påmeldingsskjema, og deltakerlisten tar
bare med e-posten til dem som har svart Ja. De andre får et tomt felt; «Vis alle e-postadresser» overstyrer (intern bruk).
Oppdiktede testdata."""
import pytest

from kurs import config, db, deltakerliste, ekstrafelt, migreringer


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _admin():
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def _kurs(con, kode="DEL1"):
    return db.opprett_kurs(con, kode=kode, navn="Delingskurs", datoer=["2099-03-02"], sharepoint_mappe=f"K/{kode}",
                           pris_nok=0)


def _meld_paa(con, kid, nr, svar=None):
    db.meld_paa(con, kid, epost=f"deltaker{nr}@eksempel.no", fornavn=f"Deltaker{nr}", etternavn="Test",
                paamelding={}, sensitivt={}, svar={db.epostdeling_felt(con, kid): svar} if svar else None)


def test_sikre_epostdeling_lager_ett_felt_og_er_idempotent(con):
    kid = _kurs(con)
    felt_id = db.sikre_epostdeling(con, kid)
    assert felt_id and db.sikre_epostdeling(con, kid) == felt_id
    rad = con.execute("SELECT type, label, obligatorisk, plassering, rolle FROM kurs_ekstrafelt WHERE kurs_id=?",
                      (kid,)).fetchall()
    assert [tuple(r) for r in rad] == [("envalg", "Deling av e-post", 1, "til_slutt", "deling_epost")]


def test_eksisterende_felt_med_samme_navn_merkes_i_stedet_for_nytt(con):
    kid = _kurs(con)
    gammel = db.opprett_ekstrafelt(con, kid, dict(ekstrafelt.MALER["deling_epost"][1]), 1)
    assert db.sikre_epostdeling(con, kid) == gammel
    assert con.execute("SELECT COUNT(*) FROM kurs_ekstrafelt WHERE kurs_id=?", (kid,)).fetchone()[0] == 1


def test_migreringen_merker_felt_som_finnes(con):
    kid = _kurs(con)
    gammel = db.opprett_ekstrafelt(con, kid, dict(ekstrafelt.MALER["deling_epost"][1]), 1)
    con.execute("UPDATE kurs_ekstrafelt SET rolle=NULL")
    migreringer._m27_epostdeling(con)
    assert db.epostdeling_felt(con, kid) == gammel
    migreringer._m27_epostdeling(con)                 # trygg å kjøre igjen
    assert db.epostdeling_felt(con, kid) == gammel


def test_nytt_kurs_fra_admin_faar_sporsmaalet(con):
    k = _admin()
    r = k.post("/admin/kurs/ny", data={
        "navn": "Nytt kurs", "type": "fysisk", "sted": "Oslo", "kapasitet": "", "datoer": "2099-05-01",
        "start_kl": "09:00", "slutt_kl": "16:00", "timer_pr_dag": "6", "pris_nok": "0", "fakturering": "person",
        "betaling": "samlet", "faktura_dager_for": "14", "ansvarlig_admin_id": "", "paameldingsfrist": "",
        "kursholder_navn": "", "kursholder_epost": "", "materiell_frist": "", "notat": ""})
    assert r.status_code in (302, 303), r.get_data(as_text=True)[:500]
    kid = db.koble(config.DB_STI).execute("SELECT id FROM kurs WHERE navn='Nytt kurs'").fetchone()["id"]
    assert db.epostdeling_felt(db.koble(config.DB_STI), kid)


def test_deltakerlisten_viser_bare_epost_til_dem_som_vil_dele(con):
    kid = _kurs(con)
    db.sikre_epostdeling(con, kid)
    _meld_paa(con, kid, 1, "Ja")
    _meld_paa(con, kid, 2, "Nei")
    _meld_paa(con, kid, 3)                            # har ikke svart
    con.commit()
    k = _admin()
    html = k.get(f"/admin/kurs/{kid}/deltakerliste?valgt=1&kol=epost&status=alle").get_data(as_text=True)
    assert "deltaker1@eksempel.no" in html
    assert "deltaker2@eksempel.no" not in html and "deltaker3@eksempel.no" not in html
    assert "Ønsker ikke" not in html                  # tomt felt, ingen tekst (Camilla)
    csv = k.get(f"/admin/kurs/{kid}/deltakerliste.csv?valgt=1&kol=epost&status=alle").get_data(as_text=True)
    assert "deltaker1@eksempel.no" in csv and "deltaker2@eksempel.no" not in csv
    alle = k.get(f"/admin/kurs/{kid}/deltakerliste?valgt=1&kol=epost&status=alle&vis=alle_epost").get_data(as_text=True)
    assert all(f"deltaker{n}@eksempel.no" in alle for n in (1, 2, 3))
    assert 'name="vis" value="alle_epost" checked' in alle


def test_hurtigvalget_lager_ikke_to_sporsmaal(con):
    kid = _kurs(con)
    db.sikre_epostdeling(con, kid)
    con.commit()
    k = _admin()
    k.post(f"/admin/kurs/{kid}/paameldingsskjema/nytt-felt", data={"mal": "deling_epost"})
    assert db.koble(config.DB_STI).execute("SELECT COUNT(*) FROM kurs_ekstrafelt WHERE kurs_id=?", (kid,)).fetchone()[0] == 1


def test_valg_som_args_tar_med_alle_epost():
    v = deltakerliste.Valg((), "alle", True, True)
    assert v.som_args()["vis"] == ["kursnr", "alle_epost"]
