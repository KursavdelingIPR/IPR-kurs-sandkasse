"""Versjonerte migreringer (kurs/migreringer.py, python -m kurs.migrer) og det permanente kursnummeret (migrering 1).

Kravene:
  * migrering er eksplisitt og idempotent (kjoert to ganger -> ingenting andre gang), hver i egen transaksjon
  * en gammel database (uten kursnr/teller/schema_versjon) migreres uten datatap, og eksisterende kurs faar nummer
  * en NYERE database enn koden gir tydelig feil; appen og daglig jobb nekter aa kjoere mot feil versjon i drift
  * kursnummer: automatisk, unikt, numerisk, permanent (uendret ved navneendring), aldri gjenbrukt, nytt ved duplisering,
    soekbart i Aktiviteter, synlig i admin. Offentlige lenker (/kurs/<kode>) virker som foer.
"""
import sqlite3
from datetime import date, timedelta

import pytest

from kurs import config, db, migreringer, migrer


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    return c


def _kurs(con, kode="T1", navn="Testkurs", **kw):
    start = date.today() + timedelta(days=30)
    return db.opprett_kurs(con, kode=kode, navn=navn, datoer=[start.isoformat()], sharepoint_mappe=f"Kurs/{kode}",
                           **{"pris_nok": 1000, **kw})


def _kursnr(con, kid):
    return con.execute("SELECT kursnr FROM kurs WHERE id=?", (kid,)).fetchone()["kursnr"]


def _admin(con):
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


# ============================ rammeverk ============================

def test_ny_database_er_paa_kodens_versjon_og_init_er_idempotent(con):
    assert migreringer.gjeldende_versjon(con) == migreringer.KODEVERSJON
    assert db.init(con) == []                                       # ingenting kjoeres andre gang
    assert con.execute("SELECT COUNT(*) FROM schema_versjon").fetchone()[0] == migreringer.KODEVERSJON
    migreringer.kontroller(con)                                      # ingen feil


def test_migreringene_er_nummerert_fortloepende_fra_1():
    assert [nr for nr, _, _ in migreringer.MIGRERINGER] == list(range(1, len(migreringer.MIGRERINGER) + 1))
    assert all(navn and callable(fn) for _, navn, fn in migreringer.MIGRERINGER)


def test_nyere_database_enn_koden_gir_tydelig_feil(con):
    con.execute("INSERT INTO schema_versjon (versjon, navn, kjort) VALUES (?, 'fremtid', ?)",
                (migreringer.KODEVERSJON + 1, db.naa_utc()))
    con.commit()
    with pytest.raises(migreringer.VersjonsFeil) as e:
        migreringer.kontroller(con)
    assert "nyere" in str(e.value).lower() or "kjenner bare" in str(e.value)
    with pytest.raises(migreringer.VersjonsFeil):
        migreringer.kjor_manglende(con)                              # kjoerer aldri noe mot en nyere database


def test_umigrert_database_gir_tydelig_feil_ved_kontroll(con):
    con.execute("DELETE FROM schema_versjon")
    con.commit()
    with pytest.raises(migreringer.VersjonsFeil) as e:
        migreringer.kontroller(con)
    assert "python -m kurs.migrer" in str(e.value)


@pytest.mark.kun_sqlite  # gjenskaper en database slik den saa ut FOER migrering 1 (SQLite-fil)
def test_gammel_database_migreres_uten_datatap_og_eksisterende_kurs_faar_nummer(tmp_path, monkeypatch):
    import re
    monkeypatch.setattr(config, "DB_STI", tmp_path / "gammel.db")
    monkeypatch.setattr(config, "DEMO", True)
    # 1. Databasen slik koden FOER kursnummer laget den: dagens schema.sql uten kursnr, teller og schema_versjon.
    skjema = re.sub(r"--[^\n]*", "", (config.ROT / "kurs" / "schema.sql").read_text(encoding="utf-8"))
    skjema = re.sub(r"\n\s+kursnr\s+INTEGER UNIQUE,", "", skjema)
    for tabell in ("teller", "schema_versjon"):
        skjema = re.sub(rf"CREATE TABLE IF NOT EXISTS {tabell} \(.*?\);", "", skjema, flags=re.S)
    assert "kursnr" not in skjema and "schema_versjon" not in skjema and "teller" not in skjema
    raa = sqlite3.connect(tmp_path / "gammel.db")
    raa.executescript(skjema)
    raa.execute("INSERT INTO admin_bruker (brukernavn, navn, passord_hash) VALUES ('admin', 'A', 'x')")
    for kode, navn in (("A", "Første"), ("B", "Andre")):
        raa.execute("INSERT INTO kurs (kode, navn, sharepoint_mappe) VALUES (?,?,?)", (kode, navn, f"Kurs/{kode}"))
        raa.execute("INSERT INTO kursdag (kurs_id, dato, innsjekk_token, innsjekk_kode) VALUES "
                    "((SELECT id FROM kurs WHERE kode=?), '2027-05-01', ?, 'ABC123')", (kode, f"tok-{kode}"))
    raa.commit()
    assert "kursnr" not in [r[1] for r in raa.execute("PRAGMA table_info(kurs)")]
    raa.close()
    # 2. Migrer.
    assert migrer.kjor([]) == 0
    c = db.koble()
    assert migreringer.gjeldende_versjon(c) == migreringer.KODEVERSJON
    rader = c.execute("SELECT id, navn, kursnr FROM kurs ORDER BY id").fetchall()
    assert [r["navn"] for r in rader] == ["Første", "Andre"]                         # ingen data tapt
    assert [r["kursnr"] for r in rader] == [1001, 1002]                             # eldste kurs lavest nummer
    assert c.execute("SELECT COUNT(*) FROM kursdag").fetchone()[0] == 2
    # 3. Idempotent, og nye kurs fortsetter telleren.
    assert migrer.kjor([]) == 0
    assert [r["kursnr"] for r in c.execute("SELECT kursnr FROM kurs ORDER BY id")] == [1001, 1002]
    assert _kursnr(c, _kurs(c, "C")) == 1003
    with pytest.raises(db.IntegritetsFeil):                                          # unik ogsaa i den migrerte tabellen
        c.execute("UPDATE kurs SET kursnr=1001 WHERE kode='C'")
    c.rollback()
    c.close()


def test_migrer_status_viser_versjon_uten_aa_endre(con, capsys, monkeypatch):
    assert migrer.kjor(["--status"]) == 0
    ut = capsys.readouterr().out
    assert f"Kodens versjon: {migreringer.KODEVERSJON}" in ut and "kursnummer" in ut
    con.execute("DELETE FROM schema_versjon")
    con.commit()
    assert migrer.kjor(["--status"]) == 1                            # ikke i takt -> avslutningskode 1, ingen endring
    assert migreringer.gjeldende_versjon(con) == 0


def test_migrering_kjoerer_i_egen_transaksjon_og_registreres_ikke_ved_feil(con, monkeypatch):
    con.execute("DELETE FROM schema_versjon")
    con.commit()

    def feiler(c):
        c.execute("UPDATE teller SET verdi=999999 WHERE navn='kursnr'")
        raise RuntimeError("midt i migreringen")
    monkeypatch.setattr(migreringer, "MIGRERINGER", [(1, "feiler", feiler)])
    monkeypatch.setattr(migreringer, "KODEVERSJON", 1)
    with pytest.raises(RuntimeError):
        migreringer.kjor_manglende(con)
    assert migreringer.gjeldende_versjon(con) == 0                                  # ikke registrert
    assert con.execute("SELECT verdi FROM teller WHERE navn='kursnr'").fetchone()[0] != 999999   # rullet tilbake


# ============================ drift: appen og daglig jobb nekter feil versjon ============================

def test_appen_svarer_503_i_drift_til_migreringen_er_kjoert(con, monkeypatch):
    from kurs.web import app as webapp
    monkeypatch.setattr(config, "DEMO", False)
    monkeypatch.setattr(config, "HEMMELIG_NOKKEL", "x" * 40)         # ellers stopper produksjonskontrollen foerst
    monkeypatch.setattr(config, "WEBHOOK_HEMMELIG", "y" * 40)
    monkeypatch.setattr(config, "BASE_URL", "https://kurs.eksempel.no")
    monkeypatch.setattr(webapp, "_database_klar", False)
    con.execute("DELETE FROM schema_versjon")
    con.commit()
    klient = webapp.app.test_client()
    r = klient.get("/")
    assert r.status_code == 503 and "vedlikeholdes" in r.get_data(as_text=True)
    html = r.get_data(as_text=True)
    assert "versjon" not in html.lower() and "migrer" not in html.lower()          # ingen tekniske detaljer offentlig
    migrer.kjor([])                                                                  # migrering kjoert (eget steg)
    assert klient.get("/").status_code == 200                                        # tar seg inn uten omstart
    assert webapp._database_klar is True


def test_appen_kontrollerer_ikke_versjon_i_demo(con, monkeypatch):
    from kurs.web import app as webapp
    monkeypatch.setattr(webapp, "_database_klar", False)
    con.execute("DELETE FROM schema_versjon")
    con.commit()
    assert webapp.app.test_client().get("/").status_code == 200


def test_daglig_jobb_nekter_umigrert_database_i_drift(con, monkeypatch, capsys):
    from kurs import daglig
    monkeypatch.setattr(config, "DEMO", False)
    monkeypatch.setattr(config, "MODUS", "prod")
    con.execute("DELETE FROM schema_versjon")
    con.commit()
    assert daglig.main(["--tor"]) == 3
    assert "STOPP" in capsys.readouterr().out
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling LIKE 'sveip%'").fetchone()[0] == 0


def test_gunicorn_oppstart_migrerer_ikke():
    """startup.py importerer kun appen; db.init/migreringer kjoeres aldri der."""
    kilde = (config.ROT / "startup.py").read_text(encoding="utf-8")
    assert "init" not in kilde and "migrer" not in kilde


# ============================ kursnummer ============================

def test_kursnummer_tildeles_automatisk_stigende_og_unikt(con):
    a, b, c = _kurs(con, "A"), _kurs(con, "B"), _kurs(con, "C")
    numre = [_kursnr(con, k) for k in (a, b, c)]
    assert numre == [1001, 1002, 1003]
    assert all(isinstance(n, int) for n in numre)
    with pytest.raises(db.IntegritetsFeil):                                          # UNIQUE haandheves av databasen
        con.execute("UPDATE kurs SET kursnr=? WHERE id=?", (numre[0], b))
    con.rollback()


def test_kursnummer_kan_ikke_velges_av_kalleren(con):
    kid = _kurs(con, "A", kursnr=77)
    assert _kursnr(con, kid) == 1001


def test_kursnummer_er_permanent_ved_navneendring_og_annen_redigering(con):
    kid = _kurs(con, "A", "Gammelt navn")
    nr = _kursnr(con, kid)
    db.oppdater_kurs_felter(con, kid, {"navn": "Helt nytt navn", "sted": "Oslo", "pris_nok": 2000})
    db.endre_kapasitet(con, kid, 5)
    db.sett_kurs_status(con, kid, "utkast")
    assert _kursnr(con, kid) == nr
    assert con.execute("SELECT navn FROM kurs WHERE id=?", (kid,)).fetchone()["navn"] == "Helt nytt navn"


def test_kursnummer_gjenbrukes_aldri(con):
    a = _kurs(con, "A")
    nr_a = _kursnr(con, a)
    con.execute("DELETE FROM kursdag WHERE kurs_id=?", (a,))
    con.execute("DELETE FROM kurs WHERE id=?", (a,))                                 # (finnes ingen sletting i appen)
    con.commit()
    assert _kursnr(con, _kurs(con, "B")) == nr_a + 1                                 # telleren gaar aldri tilbake


def test_duplisert_kurs_faar_nytt_kursnummer(con):
    kid = _kurs(con, "A", "Original")
    con.commit()
    admin = _admin(con)
    r = admin.post(f"/admin/kurs/ny?fra={kid}", data={
        "fra": str(kid), "navn": "Kopi", "type": "fysisk", "datoer": "2028-01-10", "start_kl": "09:00",
        "slutt_kl": "16:00", "fakturering": "person", "betaling": "samlet"})
    assert r.status_code == 302
    ny = con.execute("SELECT * FROM kurs WHERE navn='Kopi'").fetchone()
    assert ny["kursnr"] == _kursnr(con, kid) + 1 and ny["kode"] != "A"


def test_kursnummer_vises_i_admin_og_offentlig_lenke_virker_som_foer(con):
    kid = _kurs(con, "EFT-2027", "EFT samling")
    con.commit()
    admin = _admin(con)
    for url in ("/admin", "/admin/aktiviteter", f"/admin/kurs/{kid}/oppsett", "/admin/rapporter/kurs"):
        html = admin.get(url).get_data(as_text=True)
        assert "1001" in html, url
    assert admin.get("/admin/rapporter/kurs.csv").get_data(as_text=True).startswith("﻿Kursnr;")
    assert admin.get("/kurs/EFT-2027").status_code == 200                            # offentlig lenke uendret


def test_kursnummer_er_soekbart_i_aktiviteter(con):
    a = _kurs(con, "A", "Parterapi")
    b = _kurs(con, "B", "Veiledning")
    _kurs(con, "C", "Stressmestring")
    con.commit()
    admin = _admin(con)
    html = admin.get(f"/admin/aktiviteter?sok={_kursnr(con, b)}").get_data(as_text=True)
    assert "Veiledning" in html and "Parterapi" not in html and "Stressmestring" not in html
    html = admin.get("/admin/aktiviteter?sok=100").get_data(as_text=True)             # prefiks: alle 1001-1003
    assert "Veiledning" in html and "Parterapi" in html and "Stressmestring" in html
    html = admin.get("/admin/aktiviteter?sok=parterapi").get_data(as_text=True)       # navn virker fortsatt
    assert "Parterapi" in html and "Veiledning" not in html
    html = admin.get("/admin/aktiviteter?sorter=kursnr&retning=desc").get_data(as_text=True)
    assert html.index("Stressmestring") < html.index("Veiledning") < html.index("Parterapi")


def test_webhook_godtar_kursnummer(con, monkeypatch):
    import hashlib
    import hmac
    import json
    from kurs.web import app as webapp
    kid = _kurs(con, "A")
    con.commit()
    body = json.dumps({"fornavn": "Test", "etternavn": "Person", "epost": "test@example.no", "kurs": str(_kursnr(con, kid))}).encode()
    sig = hmac.new(config.WEBHOOK_HEMMELIG.encode(), body, hashlib.sha256).hexdigest()
    r = webapp.app.test_client().post("/api/paamelding", data=body, content_type="application/json",
                                      headers={"X-IPR-Signatur": sig})
    assert r.status_code == 201 and r.get_json()["paameldingsstatus"] == "bekreftet"


def test_hendelseslogg_faar_kursnr_ved_opprettelse(con):
    kid = _kurs(con, "A")
    rad = con.execute("SELECT detaljer FROM hendelse WHERE handling='kurs_opprettet' ORDER BY id DESC LIMIT 1").fetchone()
    assert f'"kursnr": {_kursnr(con, kid)}' in rad["detaljer"]


def test_helsesjekk_svarer_ok_og_503_ved_feil_versjon(con):
    from kurs.web import app as webapp
    klient = webapp.app.test_client()
    r = klient.get("/helse")
    assert (r.status_code, r.get_data(as_text=True)) == (200, "ok")
    con.execute("DELETE FROM schema_versjon")
    con.commit()
    r = klient.get("/helse")
    assert r.status_code == 503 and "versjon" not in r.get_data(as_text=True).lower()
