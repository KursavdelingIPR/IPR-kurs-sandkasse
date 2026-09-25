"""Tester som KUN kjoerer mot en ekte PostgreSQL-testdatabase (TEST_DATABASE_URL, se conftest.py).

Resten av testsuiten kjoerer ogsaa mot PostgreSQL i den modusen; disse testene kontrollerer i tillegg det som er
PostgreSQL-spesifikt og som SQLite ikke kan bevise: at schema_postgres.sql faktisk gir den datamodellen parseren i
test_skjema_speiling ser, CITEXT-semantikk, unike indekser med COALESCE, fremmednokler med CASCADE, transaksjoner,
typesikkerhet, adapterens transaksjonsflagg og at migreringen er idempotent ogsaa der.

    TEST_DATABASE_URL=postgresql://bruker:passord@127.0.0.1:5432/ipr_test python -m pytest tests -q
"""
from datetime import date, datetime

import pytest

from kurs import config, db, migrer

pytestmark = pytest.mark.kun_postgres


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble()
    db.init(c)
    yield c
    c.close()


def _kurs(con, kode="PG1", **kw):
    return db.opprett_kurs(con, kode=kode, navn="Testkurs", datoer=["2027-05-01", "2027-05-02"],
                           sharepoint_mappe=f"Kurs/{kode}", **{"pris_nok": 1000, **kw})


# ============================ skjemaet i katalogen = det parseren ser ============================

def test_katalogen_stemmer_med_schema_postgres(con):
    from test_skjema_speiling import PG_IDX, PG_TAB
    assert db.er_postgres(con)
    tabeller = {r["table_name"] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() AND table_type='BASE TABLE'")}
    assert tabeller == set(PG_TAB)
    for tabell, info in PG_TAB.items():
        kolonner = con.execute(
            """SELECT column_name, is_nullable, column_default, data_type, udt_name, is_identity
               FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = ?
               ORDER BY ordinal_position""", (tabell,)).fetchall()
        assert [k["column_name"] for k in kolonner] == [k["navn"] for k in info["kolonner"]], tabell
        for forventet, faktisk in zip(info["kolonner"], kolonner):
            hvor = f"{tabell}.{forventet['navn']}"
            assert (faktisk["is_nullable"] == "NO") == forventet["not_null"], hvor
            assert (faktisk["is_identity"] == "YES") == forventet["identitet"], hvor
            typer = {"BIGINT": "int8", "TEXT": "text", "CITEXT": "citext", "DOUBLE PRECISION": "float8"}
            assert faktisk["udt_name"] == typer[forventet["type"]], hvor
            if forventet["default"] and "now()" in forventet["default"]:
                assert "now()" in (faktisk["column_default"] or ""), hvor
    indekser = {r["indexname"] for r in con.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")}
    assert set(PG_IDX) <= indekser


def test_alle_fremmednokler_og_cascade_finnes(con):
    from test_skjema_speiling import PG_TAB
    fk = con.execute(
        """SELECT tc.table_name, kcu.column_name, ccu.table_name AS ref_tabell, rc.delete_rule
           FROM information_schema.table_constraints tc
           JOIN information_schema.key_column_usage kcu ON kcu.constraint_name = tc.constraint_name
                AND kcu.constraint_schema = tc.constraint_schema
           JOIN information_schema.referential_constraints rc ON rc.constraint_name = tc.constraint_name
                AND rc.constraint_schema = tc.constraint_schema
           JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name = tc.constraint_name
                AND ccu.constraint_schema = tc.constraint_schema
           WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = current_schema()""").fetchall()
    faktisk = {(r["table_name"], r["column_name"]): (r["ref_tabell"], r["delete_rule"]) for r in fk}
    forventet = {}
    for tabell, info in PG_TAB.items():
        for k in info["kolonner"]:
            if k["ref"]:
                forventet[(tabell, k["navn"])] = (k["ref"][0], "CASCADE" if k["ref"][2] else "NO ACTION")
    assert faktisk == forventet


# ============================ semantikk som SQLite ikke kan bevise ============================

def test_citext_gir_case_insensitiv_epost_og_mottaker(con):
    kid = _kurs(con)
    did = db.finn_eller_opprett_deltaker(con, "Kari@Eksempel.NO", "Kari")
    assert db.finn_eller_opprett_deltaker(con, "kari@eksempel.no", "Kari") == did
    assert con.execute("SELECT id FROM deltaker WHERE epost=?", ("KARI@EKSEMPEL.NO",)).fetchone()["id"] == did
    with pytest.raises(db.IntegritetsFeil):
        con.execute("INSERT INTO deltaker (epost, navn) VALUES (?,?)", ("KARI@eksempel.no", "Dublett"))
    con.rollback()
    assert db.reserver_sending(con, f"kurs:{kid}", "Kari@Eksempel.NO", "bekreftelse") is True
    assert db.reserver_sending(con, f"kurs:{kid}", "kari@eksempel.no", "bekreftelse") is False   # samme mottaker
    db.sett_sendt(con, f"kurs:{kid}", "KARI@EKSEMPEL.NO", "bekreftelse")
    assert db.allerede_sendt(con, f"kurs:{kid}", "kari@eksempel.no", "bekreftelse") is True


def test_admin_brukernavn_er_case_insensitivt(con):
    db.opprett_admin_bruker(con, "Kari.Admin", "Kari", "passord-som-holder")
    con.commit()
    assert db.verifiser_admin(con, "KARI.ADMIN", "passord-som-holder") is not None
    with pytest.raises(db.IntegritetsFeil):
        db.opprett_admin_bruker(con, "kari.admin", "Kari 2", "passord-som-holder")
    con.rollback()


def test_unik_faktura_ogsaa_for_samlet_faktura_null_kursdag(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.execute("INSERT INTO faktura (paamelding_id, kursdag_id, belop_nok) VALUES (?, NULL, 1000)", (pid,))
    con.commit()
    with pytest.raises(db.IntegritetsFeil):   # COALESCE(kursdag_id, 0) i indeksen: NULL != NULL hjelper ikke
        con.execute("INSERT INTO faktura (paamelding_id, kursdag_id, belop_nok) VALUES (?, NULL, 1000)", (pid,))
    con.rollback()
    assert con.execute("SELECT COUNT(*) FROM faktura WHERE paamelding_id=?", (pid,)).fetchone()[0] == 1
    assert db.reserver_faktura(con, pid, None) is True and db.reserver_faktura(con, pid, None) is False


def test_on_conflict_do_nothing_og_upsert(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A", sensitivt={"allergier": "Nøtter"})
    db.oppdater_sensitivt(con, pid, "Gluten", None)
    rad = con.execute("SELECT * FROM sensitivt WHERE paamelding_id=?", (pid,)).fetchone()
    assert (rad["allergier"], rad["tilrettelegging"]) == ("Gluten", None)
    dag = db.kursdager(con, kid)[0]
    assert db.registrer_oppmote(con, pid, dag["id"], "qr") is True
    assert db.registrer_oppmote(con, pid, dag["id"], "kode") is False        # ON CONFLICT DO NOTHING -> rowcount 0
    db.sett_maltekst(con, "bekreftelse", "emne", "A")
    db.sett_maltekst(con, "bekreftelse", "emne", "B")
    assert db.hent_maltekst(con, "bekreftelse", "emne") == "B"


def test_returning_id_og_identitet(con):
    a = _kurs(con, "A")
    b = _kurs(con, "B")
    assert isinstance(a, int) and b == a + 1
    assert con.execute("SELECT id FROM kurs WHERE kode='B'").fetchone()[0] == b


def test_cascade_sletter_kursdager_og_skjemafelt_men_paamelding_stopper(con):
    kid = _kurs(con)
    db.lagre_skjemafelt(con, kid, "telefon", {"synlig": False})
    con.execute("DELETE FROM kurs WHERE id=?", (kid,))
    assert con.execute("SELECT COUNT(*) FROM kursdag WHERE kurs_id=?", (kid,)).fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM kurs_skjemafelt WHERE kurs_id=?", (kid,)).fetchone()[0] == 0
    con.rollback()
    kid = _kurs(con, "C")
    db.meld_paa(con, kid, epost="a@x.no", navn="A")
    with pytest.raises(db.IntegritetsFeil):      # paamelding.kurs_id har INGEN cascade - historikk beskyttes
        con.execute("DELETE FROM kurs WHERE id=?", (kid,))
    con.rollback()


def test_transaksjon_rollback_og_savepoint(con):
    kid = _kurs(con)
    con.commit()
    with pytest.raises(RuntimeError):
        with db.transaksjon(con):
            db.meld_paa(con, kid, epost="a@x.no", navn="A")
            raise RuntimeError("avbryt")
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 0
    db.meld_paa(con, kid, epost="b@x.no", navn="B")
    with pytest.raises(db.DatabaseFeil):
        with db.isolert(con):
            con.execute("SELECT * FROM finnes_ikke")
    assert not con.avbrutt                                       # tilkoblingen kan brukes videre etter SAVEPOINT-rollback
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 1   # arbeidet foer savepointet er intakt
    con.commit()


def test_feilet_setning_avbryter_transaksjonen_og_rull_tilbake_hvis_avbrutt(con):
    with pytest.raises(db.DatabaseFeil):
        con.execute("SELECT * FROM finnes_ikke")
    assert con.avbrutt and con.in_transaction
    with pytest.raises(db.DatabaseFeil):
        con.execute("SELECT 1")                                  # PostgreSQL: alt feiler til rollback
    assert db.rull_tilbake_hvis_avbrutt(con) is True
    assert con.execute("SELECT 1").fetchone()[0] == 1
    assert db.rull_tilbake_hvis_avbrutt(con) is False


def test_in_transaction_teller_kun_skriving(con):
    con.commit()
    con.execute("SELECT 1").fetchone()
    assert not con.in_transaction                                # ren lesing = ingen aapen skrivetransaksjon
    _kurs(con)
    assert con.in_transaction
    con.commit()
    assert not con.in_transaction


def test_typesikkerhet_bool_dato_og_datetime_lagres_som_i_sqlite(con):
    kid = _kurs(con)
    con.execute("UPDATE kurs SET paameldingsfrist=? WHERE id=?", (date(2027, 4, 1), kid))
    con.execute("UPDATE paamelding SET ehf=? WHERE id=-1", (True,))       # bool -> 1
    con.execute("UPDATE kurs SET notat=? WHERE id=?", (datetime(2027, 4, 1, 12, 30), kid))
    rad = con.execute("SELECT paameldingsfrist, notat FROM kurs WHERE id=?", (kid,)).fetchone()
    assert (rad["paameldingsfrist"], rad["notat"]) == ("2027-04-01", "2027-04-01 12:30:00")
    with pytest.raises(db.DatabaseFeil):
        con.execute("UPDATE kurs SET kapasitet=? WHERE id=?", ("abc", kid))
    con.rollback()


def test_null_semantikk_group_by_having_og_string_agg(con):
    kid = _kurs(con)
    a, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    b, _ = db.meld_paa(con, kid, epost="b@x.no", navn="B", deltaker={"arbeidssted": "BUP"})
    con.execute("INSERT INTO faktura (paamelding_id, kursdag_id, faktura_nr, belop_nok) VALUES (?, NULL, 'F1', 500)", (a,))
    dag = db.kursdager(con, kid)
    con.execute("INSERT INTO faktura (paamelding_id, kursdag_id, faktura_nr, belop_nok) VALUES (?, ?, 'F2', 500)",
                (b, dag[0]["id"]))
    con.execute("INSERT INTO faktura (paamelding_id, kursdag_id, faktura_nr, belop_nok) VALUES (?, ?, 'F3', 500)",
                (b, dag[1]["id"]))
    rader = con.execute(
        f"""SELECT p.id, {db.sql_tekstliste(con, 'f.faktura_nr')} AS nr, COUNT(f.id) AS antall
            FROM paamelding p LEFT JOIN faktura f ON f.paamelding_id=p.id
            GROUP BY p.id HAVING COUNT(f.id) > 1 ORDER BY p.id""").fetchall()
    assert [(r["id"], sorted(r["nr"].split(" ")), r["antall"]) for r in rader] == [(b, ["F2", "F3"], 2)]
    assert con.execute("SELECT COUNT(*) FROM deltaker WHERE arbeidssted IS NULL").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM deltaker WHERE arbeidssted = ''").fetchone()[0] == 0
    assert con.execute("SELECT COALESCE(NULL, 'x')").fetchone()[0] == "x"


def test_like_med_prosent_og_parametre(con):
    _kurs(con, "L1", sted="IPR, Bergen")
    _kurs(con, "L2", sted="Oslo")
    rader = con.execute("SELECT kode FROM kurs WHERE LOWER(COALESCE(sted,'')) LIKE '%bergen%' AND kode LIKE ?",
                        ("L%",)).fetchall()
    assert [r["kode"] for r in rader] == ["L1"]


def test_nul_byte_avvises_kontrollert_som_databasefeil(con):
    kid = _kurs(con)
    with pytest.raises(db.DatabaseFeil):
        con.execute("UPDATE kurs SET notat=? WHERE id=?", ("A\x00B", kid))
    con.rollback()


def test_migrer_er_idempotent_mot_postgres(capsys, monkeypatch):
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://bruker:lokal-test-kun@127.0.0.1/db")  # kun for utskriften
    assert migrer.kjor() == 0
    assert migrer.kjor() == 0
    ut = capsys.readouterr().out
    assert "PostgreSQL" in ut and "lokal-test-kun" not in ut and "127.0.0.1" not in ut   # aldri tilkoblingsstrengen


def test_concurrent_claims_med_to_ekte_tilkoblinger(con):
    """To uavhengige forbindelser (som to gunicorn-workere) om samme e-postclaim: akkurat én vinner, og taperen blokkeres
    ikke for alltid - claimen committes umiddelbart av vinneren."""
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="a@x.no", navn="A")
    con.commit()
    annen = db.koble()
    try:
        assert db.reserver_sending(con, f"kurs:{kid}", "a@x.no", "bekreftelse") is True
        con.commit()
        assert db.reserver_sending(annen, f"kurs:{kid}", "a@x.no", "bekreftelse") is False
        annen.commit()
        assert db.reserver_faktura(annen, pid, None) is True
        annen.commit()
        assert db.reserver_faktura(con, pid, None) is False
        con.commit()
    finally:
        annen.close()
