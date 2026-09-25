"""Fase 8, trinn 3: kurskalender (lesevisning), ingen deltakerdata, ingen parallelle kalenderdata."""
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


def _kurs(con, kode, navn, datoer, **kw):
    return db.opprett_kurs(con, kode=kode, navn=navn, datoer=datoer, sharepoint_mappe=f"Kurs/{kode}", **kw)


def _hent(klient, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return klient.get(f"/admin/aktiviteter/kalender?{qs}" if qs else "/admin/aktiviteter/kalender")


# ---------------- rute/innlogging ----------------

def test_krever_innlogging(con):
    con.commit()
    klient = _klient()
    r = klient.get("/admin/aktiviteter/kalender")
    assert r.status_code == 302
    assert "logg-inn" in r.headers["Location"]


# ---------------- maanedsvisning ----------------

def test_innevaerende_maaned_som_standard(con):
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = _hent(klient).get_data(as_text=True)
    idag = date.today()
    maaned_navn = ["", "Januar", "Februar", "Mars", "April", "Mai", "Juni", "Juli", "August",
                  "September", "Oktober", "November", "Desember"][idag.month]
    assert f"{maaned_navn} {idag.year}" in t


def test_eksplisitt_maaned_og_aar(con):
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = _hent(klient, aar=2027, maned=3).get_data(as_text=True)
    assert "Mars 2027" in t


def test_forrige_og_neste_maaned_lenker(con):
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = _hent(klient, aar=2027, maned=6).get_data(as_text=True)
    assert "maned=5" in t and "aar=2027" in t  # forrige
    assert "maned=7" in t  # neste


def test_aarsskifte_fungerer(con):
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    desember = _hent(klient, aar=2026, maned=12).get_data(as_text=True)
    assert "maned=1" in desember and "aar=2027" in desember  # "neste" fra des 2026 -> jan 2027
    januar = _hent(klient, aar=2027, maned=1).get_data(as_text=True)
    assert januar.count("maned=12") >= 1 and "aar=2026" in januar  # "forrige" fra jan 2027 -> des 2026
    assert _hent(klient, aar=2027, maned=1).status_code == 200


def test_ugyldig_maaned_og_aar_haandteres_trygt(con):
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    for params in ({"aar": "abc", "maned": "99"}, {"aar": "2027", "maned": "0"}, {"aar": "0", "maned": "5"}):
        r = _hent(klient, **params)
        assert r.status_code == 200


# ---------------- data ----------------

def test_kurs_med_flere_kursdager_vises_paa_hver_dato(con):
    _kurs(con, "FLERE", "Kurs med flere dager", ["2027-03-05", "2027-03-19"], type="fysisk", sted="IPR, Bergen")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = _hent(klient, aar=2027, maned=3).get_data(as_text=True)
    assert t.count("Kurs med flere dager") == 2


def test_flere_kurs_samme_dag_vises_alle(con):
    _kurs(con, "A1", "Kurs A", ["2027-03-10"], type="digital")
    _kurs(con, "A2", "Kurs B", ["2027-03-10"], type="fysisk", sted="Oslo, hotell")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = _hent(klient, aar=2027, maned=3).get_data(as_text=True)
    assert "Kurs A" in t and "Kurs B" in t


def test_kurs_utenfor_maaneden_vises_ikke(con):
    _kurs(con, "APRIL", "Aprilkurs", ["2027-04-15"])
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = _hent(klient, aar=2027, maned=3).get_data(as_text=True)
    assert "Aprilkurs" not in t


def test_korrekt_kurslenke(con):
    kid = _kurs(con, "LENKE", "Kurs med lenke", ["2027-03-10"])
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = _hent(klient, aar=2027, maned=3).get_data(as_text=True)
    assert f'href="/admin/kurs/{kid}/deltakere"' in t


def test_ingen_deltakerdata_vises(con):
    kid = _kurs(con, "PERS", "Personvern-kurs", ["2027-03-10"])
    db.meld_paa(con, kid, epost="skal.ikke.vises@x.no", navn="Skal Ikke Vises")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = _hent(klient, aar=2027, maned=3).get_data(as_text=True)
    assert "skal.ikke.vises@x.no" not in t
    assert "Skal Ikke Vises" not in t


# ---------------- filtre ----------------

def test_ansvarlig_filter(con):
    admin_id = _admin_id(con)
    annen_id = db.opprett_admin_bruker(con, "kari", "Kari Admin", "passord123")
    _kurs(con, "MITT", "Mitt kurs", ["2027-03-10"], ansvarlig_admin_id=admin_id)
    _kurs(con, "ANNET", "Annet kurs", ["2027-03-10"], ansvarlig_admin_id=annen_id)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = _hent(klient, aar=2027, maned=3, ansvarlig=admin_id).get_data(as_text=True)
    assert "Mitt kurs" in t and "Annet kurs" not in t


def test_status_filter(con):
    _kurs(con, "AAPEN", "Åpent kurs", ["2027-03-10"])
    _kurs(con, "AVLYST", "Avlyst kurs", ["2027-03-10"], status="avlyst")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = _hent(klient, aar=2027, maned=3, status="avlyst").get_data(as_text=True)
    assert "Avlyst kurs" in t and "Åpent kurs" not in t


def test_bergen_filter(con):
    _kurs(con, "BGO", "Bergenskurs", ["2027-03-10"], type="fysisk", sted="IPR, Bergen")
    _kurs(con, "OSL", "Oslokurs", ["2027-03-10"], type="fysisk", sted="Oslo, hotell")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = _hent(klient, aar=2027, maned=3, sted="bergen").get_data(as_text=True)
    assert "Bergenskurs" in t and "Oslokurs" not in t


def test_oslo_filter(con):
    _kurs(con, "BGO", "Bergenskurs", ["2027-03-10"], type="fysisk", sted="IPR, Bergen")
    _kurs(con, "OSL", "Oslokurs", ["2027-03-10"], type="fysisk", sted="Oslo, hotell")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = _hent(klient, aar=2027, maned=3, sted="oslo").get_data(as_text=True)
    assert "Oslokurs" in t and "Bergenskurs" not in t


def test_online_filter_fanger_digital_type_og_stedtekst(con):
    _kurs(con, "DIG", "Digitalt kurs", ["2027-03-10"], type="digital")
    _kurs(con, "ZOOMSTED", "Kurs med Zoom i stedfelt", ["2027-03-10"], type="fysisk", sted="Zoom-rom 1")
    _kurs(con, "FYS", "Fysisk kurs", ["2027-03-10"], type="fysisk", sted="IPR, Bergen")
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = _hent(klient, aar=2027, maned=3, sted="online").get_data(as_text=True)
    assert "Digitalt kurs" in t
    assert "Kurs med Zoom i stedfelt" in t
    assert "Fysisk kurs" not in t


def test_kombinerte_filtre(con):
    admin_id = _admin_id(con)
    annen_id = db.opprett_admin_bruker(con, "kari", "Kari Admin", "passord123")
    _kurs(con, "TREFF", "Treffer alt", ["2027-03-10"], type="fysisk", sted="IPR, Bergen", ansvarlig_admin_id=admin_id)
    _kurs(con, "FEIL_ANS", "Feil ansvarlig", ["2027-03-10"], type="fysisk", sted="IPR, Bergen", ansvarlig_admin_id=annen_id)
    _kurs(con, "FEIL_STED", "Feil sted", ["2027-03-10"], type="fysisk", sted="Oslo", ansvarlig_admin_id=admin_id)
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = _hent(klient, aar=2027, maned=3, sted="bergen", ansvarlig=admin_id).get_data(as_text=True)
    assert "Treffer alt" in t
    assert "Feil ansvarlig" not in t
    assert "Feil sted" not in t


def test_filtre_beholdes_ved_manedsskifte(con):
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = _hent(klient, aar=2027, maned=3, sted="bergen").get_data(as_text=True)
    assert "sted=bergen" in t  # baade i forrige- og neste-lenken


def test_nullstill_filtre(con):
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = _hent(klient, aar=2027, maned=3, sted="bergen").get_data(as_text=True)
    assert 'href="/admin/aktiviteter/kalender?aar=2027&amp;maned=3"' in t


# ---------------- trinn 4: Liste | Kalender-veksling ----------------

def test_veksling_vises_paa_begge_sider(con):
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t_liste = klient.get("/admin/aktiviteter").get_data(as_text=True)
    t_kalender = _hent(klient).get_data(as_text=True)
    for tekst in (t_liste, t_kalender):
        assert "Liste" in tekst and "Kalender" in tekst
        assert 'href="/admin/aktiviteter"' in tekst
        assert 'href="/admin/aktiviteter/kalender"' in tekst


def test_liste_er_aktiv_paa_aktivitetsoversikten(con):
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = klient.get("/admin/aktiviteter").get_data(as_text=True)
    assert '<a class="aktiv" href="/admin/aktiviteter">Liste</a>' in t
    assert '<a class="" href="/admin/aktiviteter/kalender">Kalender</a>' in t


def test_kalender_er_aktiv_paa_kalendersiden(con):
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = _hent(klient).get_data(as_text=True)
    assert '<a class="aktiv" href="/admin/aktiviteter/kalender">Kalender</a>' in t
    assert '<a class="" href="/admin/aktiviteter">Liste</a>' in t


def test_kalender_ligger_ikke_i_hovedmenyen(con):
    con.commit()
    klient = _klient()
    _logg_inn(klient)
    t = klient.get("/admin/aktiviteter").get_data(as_text=True)
    hovedmeny = t.split('<nav class="faner">')[0]
    assert "Kalender" not in hovedmeny
