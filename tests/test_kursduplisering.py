"""Fase 8, trinn 1: duplisering av kurs - forhaandsutfylling og de to nye skjemafeltene
(ansvarlig, paameldingsfrist) paa "Nytt kurs"."""
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


def _admin_id(con):
    return con.execute("SELECT id FROM admin_bruker WHERE brukernavn=?", (config.ADMIN_BRUKERNAVN,)).fetchone()[0]


def _kildekurs(con, **over):
    admin_id = _admin_id(con)
    start = date.today() + timedelta(days=10)
    felter = {
        "kode": "EFT-S1", "navn": "EFT spesialistutdanning", "type": "fysisk", "sted": "IPR, Bergen",
        "pris_nok": 14500, "kapasitet": 16, "fakturering": "person", "betaling": "per_samling",
        "faktura_dager_for": 14, "spesialistlop": "EFT", "timer_pr_dag": 7,
        "kursholder_epost": "psykolog.a@ipr.no", "notat": "Ta med egne notater.",
        "ansvarlig_admin_id": admin_id, "sharepoint_mappe": "Kurs/EFT-S1",
    }
    felter.update(over)
    datoer = [start.isoformat(), (start + timedelta(days=1)).isoformat()]
    kid = db.opprett_kurs(con, datoer=datoer, **felter)
    con.execute("INSERT INTO materiell_krav (kurs_id, ansvarlig_navn, ansvarlig_epost, frist) VALUES (?,?,?,?)",
               (kid, "Psykolog A", "psykolog.a@ipr.no", (start - timedelta(days=5)).isoformat()))
    con.execute("UPDATE kurs SET zoom_url='https://zoom/x', zoom_id='1', zoom_pw='pw' WHERE id=?", (kid,))
    return kid


def _fersk(con):
    return db.koble(config.DB_STI)


def _grunnlag(**over):
    ny_start = date.today() + timedelta(days=400)
    data = {
        "navn": "EFT spesialistutdanning", "type": "fysisk", "sted": "IPR, Bergen", "kapasitet": "16",
        "datoer": ny_start.isoformat(), "start_kl": "09:00", "slutt_kl": "16:00", "timer_pr_dag": "6",
        "pris_nok": "14500", "fakturering": "person", "betaling": "samlet", "faktura_dager_for": "14",
        "ansvarlig_admin_id": "", "paameldingsfrist": "", "kursholder_navn": "", "kursholder_epost": "",
        "materiell_frist": "", "notat": "",
    }
    data.update(over)
    return data


# ---------------- forhaandsutfylling (GET ?fra=) ----------------

def test_prefyller_felt_fra_kildekurs(con):
    kid = _kildekurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = klient.get(f"/admin/kurs/ny?fra={kid}").get_data(as_text=True)
    assert "basert på" in t
    assert 'value="EFT spesialistutdanning"' in t
    assert 'value="IPR, Bergen"' in t
    assert 'value="psykolog.a@ipr.no"' in t
    assert 'value="Psykolog A"' in t  # kursholdernavn hentet fra materiell_krav


def test_dupliser_lenke_finnes_i_kursoversikten_og_peker_riktig(con):
    kid = _kildekurs(con)
    annet_kid = db.opprett_kurs(con, kode="ANNET", navn="Et annet kurs",
                                datoer=[(date.today() + timedelta(days=50)).isoformat()],
                                sharepoint_mappe="Kurs/ANNET")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = klient.get("/admin/aktiviteter").get_data(as_text=True)
    assert f'href="/admin/kurs/ny?fra={kid}"' in t
    assert f'href="/admin/kurs/ny?fra={annet_kid}"' in t  # hvert kurs har sin egen, riktige lenke
    assert "Dupliser" in t


def test_dupliser_lenke_ikke_lenger_i_oppsett(con):
    kid = _kildekurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = klient.get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True)
    assert "Dupliser" not in t


def test_ansvarlig_forhaandsvalgt(con):
    admin_id = _admin_id(con)
    kid = _kildekurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = klient.get(f"/admin/kurs/ny?fra={kid}").get_data(as_text=True)
    valgdel = t.split('name="ansvarlig_admin_id"')[1].split("</select>")[0]
    assert f'value="{admin_id}" selected' in valgdel


def test_datofelt_tomt_men_kildedatoer_vises_som_referanse(con):
    kid = _kildekurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = klient.get(f"/admin/kurs/ny?fra={kid}").get_data(as_text=True)
    assert '<textarea name="datoer" rows="4" required></textarea>' in t
    kildedatoer = [d["dato"] for d in db.kursdager(con, kid)]
    assert "kun til referanse" in t
    for dato in kildedatoer:
        assert dato in t


def test_paameldingsfrist_og_materiellfrist_alltid_tomme_ved_duplisering(con):
    kid = _kildekurs(con)
    con.execute("UPDATE kurs SET paameldingsfrist=? WHERE id=?", ("2020-01-01", kid))
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = klient.get(f"/admin/kurs/ny?fra={kid}").get_data(as_text=True)
    assert '<input name="paameldingsfrist" type="date">' in t
    assert '<input name="materiell_frist" type="date">' in t
    assert "2020-01-01" not in t


def test_ukjent_fra_gir_404(con):
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    assert klient.get("/admin/kurs/ny?fra=9999").status_code == 404


def test_vanlig_nytt_kurs_uten_fra_har_ingen_banner_eller_forhaandsutfylling(con):
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = klient.get("/admin/kurs/ny").get_data(as_text=True)
    assert "basert på" not in t
    assert 'value="EFT spesialistutdanning"' not in t


# ---------------- opprettelse via duplisering ----------------

def test_duplisert_kurs_starter_som_utkast(con):
    admin_id = _admin_id(con)
    kid = _kildekurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post("/admin/kurs/ny", data=_grunnlag(fra=str(kid), ansvarlig_admin_id=str(admin_id)))
    fersk = _fersk(con)
    nytt = fersk.execute("SELECT * FROM kurs WHERE id != ?", (kid,)).fetchone()
    assert nytt["status"] == "utkast"


def test_vanlig_nytt_kurs_starter_fortsatt_som_aapen(con):
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    klient.post("/admin/kurs/ny", data=_grunnlag())  # ingen 'fra'
    nytt = _fersk(con).execute("SELECT status FROM kurs").fetchone()
    assert nytt["status"] == "aapen"


def test_duplisert_kurs_er_helt_selvstendig_ingen_historikk_folger_med(con):
    kid = _kildekurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.execute("INSERT INTO faktura (paamelding_id, belop_nok, status) VALUES (?, 14500, 'sendt')", (pid,))
    con.execute("UPDATE materiell_krav SET levert_ts=? WHERE kurs_id=?", (db.naa_utc(), kid))
    con.commit()
    admin_id = _admin_id(con)
    klient = _klient()
    _logg_inn(klient)
    klient.post("/admin/kurs/ny", data=_grunnlag(fra=str(kid), ansvarlig_admin_id=str(admin_id)))

    fersk = _fersk(con)
    nytt = fersk.execute("SELECT * FROM kurs WHERE id != ?", (kid,)).fetchone()
    nytt_id = nytt["id"]
    assert fersk.execute("SELECT COUNT(*) FROM paamelding WHERE kurs_id=?", (nytt_id,)).fetchone()[0] == 0
    assert fersk.execute("SELECT COUNT(*) FROM materiell_krav WHERE kurs_id=?", (nytt_id,)).fetchone()[0] == 0
    assert fersk.execute("SELECT COUNT(*) FROM faktura").fetchone()[0] == 1  # kun det gamle
    # kildekurset er uendret
    gammel = fersk.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()
    assert gammel["status"] == "aapen"
    assert fersk.execute("SELECT COUNT(*) FROM paamelding WHERE kurs_id=?", (kid,)).fetchone()[0] == 1


def test_zoom_kopieres_aldri(con):
    kid = _kildekurs(con)
    con.commit()
    admin_id = _admin_id(con)
    klient = _klient()
    _logg_inn(klient)
    klient.post("/admin/kurs/ny", data=_grunnlag(fra=str(kid), ansvarlig_admin_id=str(admin_id)))
    nytt = _fersk(con).execute("SELECT * FROM kurs WHERE id != ?", (kid,)).fetchone()
    assert (nytt["zoom_url"], nytt["zoom_id"], nytt["zoom_pw"]) == (None, None, None)


def test_ny_sharepoint_mappe_opprettes(con):
    kid = _kildekurs(con)
    con.commit()
    admin_id = _admin_id(con)
    klient = _klient()
    _logg_inn(klient)
    klient.post("/admin/kurs/ny", data=_grunnlag(fra=str(kid), ansvarlig_admin_id=str(admin_id)))
    nytt = _fersk(con).execute("SELECT sharepoint_mappe FROM kurs WHERE id != ?", (kid,)).fetchone()
    assert nytt["sharepoint_mappe"] != "Kurs/EFT-S1"
    assert nytt["sharepoint_mappe"]


def test_nye_kursdager_faar_egne_innsjekk_tokens(con):
    kid = _kildekurs(con)
    con.commit()
    admin_id = _admin_id(con)
    klient = _klient()
    _logg_inn(klient)
    klient.post("/admin/kurs/ny", data=_grunnlag(fra=str(kid), ansvarlig_admin_id=str(admin_id)))
    fersk = _fersk(con)
    gammel_token = fersk.execute("SELECT innsjekk_token FROM kursdag WHERE kurs_id=?", (kid,)).fetchone()[0]
    nytt_id = fersk.execute("SELECT id FROM kurs WHERE id != ?", (kid,)).fetchone()[0]
    ny_token = fersk.execute("SELECT innsjekk_token FROM kursdag WHERE kurs_id=?", (nytt_id,)).fetchone()[0]
    assert gammel_token != ny_token


def test_ansvarlig_og_paameldingsfrist_lagres_for_vanlig_nytt_kurs(con):
    admin_id = _admin_id(con)
    con.commit()
    frist = (date.today() + timedelta(days=5)).isoformat()
    klient = _klient()
    _logg_inn(klient)
    klient.post("/admin/kurs/ny", data=_grunnlag(ansvarlig_admin_id=str(admin_id), paameldingsfrist=frist))
    nytt = _fersk(con).execute("SELECT ansvarlig_admin_id, paameldingsfrist FROM kurs").fetchone()
    assert nytt["ansvarlig_admin_id"] == admin_id
    assert nytt["paameldingsfrist"] == frist


def test_ugyldig_paameldingsfrist_avvises_paent(con):
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    r = klient.post("/admin/kurs/ny", data=_grunnlag(paameldingsfrist="ikke-en-dato"), follow_redirects=True)
    assert r.status_code == 200
    assert "Kunne ikke opprette kurs" in r.get_data(as_text=True)
    assert _fersk(con).execute("SELECT COUNT(*) FROM kurs").fetchone()[0] == 0


# ---------------- faktura_dager_for ved duplisering (server-side 0..180) ----------------

@pytest.mark.parametrize("verdi", [0, 30, 180])
def test_duplisering_bevarer_gyldig_faktura_dager_for(con, verdi):
    kid = _kildekurs(con, faktura_dager_for=verdi)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    side = klient.get(f"/admin/kurs/ny?fra={kid}").get_data(as_text=True)
    assert f'name="faktura_dager_for" type="number" min="0" max="180" value="{verdi}"' in side    # forhaandsutfylt fra kilden
    klient.post("/admin/kurs/ny", data=_grunnlag(faktura_dager_for=str(verdi), fra=str(kid), betaling="per_samling"))
    ny = _fersk(con).execute("SELECT faktura_dager_for, status FROM kurs WHERE id != ?", (kid,)).fetchone()
    assert (ny["faktura_dager_for"], ny["status"]) == (verdi, "utkast")


def test_duplisering_med_ugyldig_verdi_avvises_uten_aa_opprette_kopi(con):
    kid = _kildekurs(con)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    side = klient.post("/admin/kurs/ny", data=_grunnlag(faktura_dager_for="181", fra=str(kid))).get_data(as_text=True)
    assert "mellom 0 og 180" in side
    assert _fersk(con).execute("SELECT COUNT(*) FROM kurs").fetchone()[0] == 1
