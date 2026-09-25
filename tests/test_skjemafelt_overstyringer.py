"""Fase 12C2: per-kurs skjemaoverstyringer (tabellen kurs_skjemafelt) - datamodell, streng lagring, defensiv lesing,
DB-lesefeil og effektivt_skjema(kurs, overstyringer). Den offentlige siden skal ENNAA IKKE lese overstyringer (12C3)."""
import dataclasses
import json
import sqlite3

import pytest
from flask import render_template

from kurs import config, db
from kurs import skjemafelt as sf
from kurs.skjemafelt import Advarsel, Overstyring, SkjemafeltFeil, SkjemaLesefeil


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _kurs(con, kode="S1", **kw):
    felter = {"navn": "Eksempelkurs", "type": "fysisk", "pris_nok": 4500, "fakturering": "person", "kapasitet": 10, **kw}
    return db.opprett_kurs(con, kode=kode, datoer=["2099-03-02"], sharepoint_mappe=f"K/{kode}", **felter)


def _rader(con, kurs_id=None):
    sql = "SELECT * FROM kurs_skjemafelt" + (" WHERE kurs_id=?" if kurs_id else "") + " ORDER BY kurs_id, felt"
    return [dict(r) for r in con.execute(sql, (kurs_id,) if kurs_id else ())]


def _hendelser(con, handling):
    return [json.loads(r["detaljer"]) | {"_aktor": r["aktor"]}
            for r in con.execute("SELECT * FROM hendelse WHERE handling=? ORDER BY id", (handling,))]


def _sett_inn(con, kurs_id, felt, **kol):
    """Skriver DIREKTE i tabellen (forbi lagrings-API-et) - simulerer korrupte/ukjente rader."""
    kol = {"synlig": None, "obligatorisk": None, "rekkefolge": None, "label": None, "hjelpetekst": None, **kol}
    con.execute("INSERT INTO kurs_skjemafelt (kurs_id, felt, synlig, obligatorisk, rekkefolge, label, hjelpetekst) "
                "VALUES (?,?,?,?,?,?,?)", (kurs_id, felt, *kol.values()))


KURS = {"type": "fysisk", "fakturering": "person", "pris_nok": 4500, "spesialistlop": None}
KURS_LOP = {**KURS, "spesialistlop": "EFT"}


def _nokler(skjema):
    return [f.nokkel for f in skjema.deltakerfelt]


def _ulagringsbar_i_postgres(kol, verdi) -> bool:
    """PostgreSQL er typesikker og kan ikke lagre NUL i tekst: slike «korrupte» verdier kan bare oppstaa i SQLite."""
    if isinstance(verdi, bytes) or (isinstance(verdi, str) and "\x00" in verdi):
        return True
    return kol in ("synlig", "obligatorisk", "rekkefolge") and (isinstance(verdi, bool) or not isinstance(verdi, int))


def _felt(skjema, nokkel):
    return next((f for f in skjema.deltakerfelt if f.nokkel == nokkel), None)


# ============================ schema ============================

@pytest.mark.kun_sqlite  # PRAGMA pk/fk-detaljer; PostgreSQL-skjemaet kontrolleres i test_skjema_speiling
def test_init_oppretter_tabellen_med_riktige_kolonner_pk_og_fk(con):
    info = {r["name"]: r for r in con.execute("PRAGMA table_info(kurs_skjemafelt)")}
    assert list(info) == ["kurs_id", "felt", "synlig", "obligatorisk", "rekkefolge", "label", "hjelpetekst",
                          "oppdatert", "oppdatert_av"]
    assert [n for n, r in info.items() if r["pk"]] == ["kurs_id", "felt"]
    assert info["kurs_id"]["notnull"] and info["felt"]["notnull"] and info["oppdatert"]["notnull"]
    assert not any(info[k]["notnull"] for k in ("synlig", "obligatorisk", "rekkefolge", "label", "hjelpetekst",
                                                "oppdatert_av"))
    fk = [dict(r) for r in con.execute("PRAGMA foreign_key_list(kurs_skjemafelt)")]
    assert [(f["table"], f["from"], f["to"], f["on_delete"]) for f in fk] == [("kurs", "kurs_id", "id", "CASCADE")]


def test_ingen_check_paa_feltnavn(con):
    kid = _kurs(con)
    _sett_inn(con, kid, "et_fremtidig_felt", label="x")   # ma IKKE feile - registeret er fasit, ikke schemaet
    assert len(_rader(con, kid)) == 1


@pytest.mark.parametrize("kol,verdi", [("synlig", 2), ("synlig", "ja"), ("obligatorisk", -1), ("obligatorisk", "nei")])
def test_check_paa_bool_kolonner(con, kol, verdi):
    kid = _kurs(con)
    # SQLite: CHECK-brudd. PostgreSQL: CHECK-brudd for tall, typefeil (tekst i heltallskolonne) for tekst - begge avvises.
    with pytest.raises(db.DatabaseFeil if db.er_postgres(con) else db.IntegritetsFeil):
        _sett_inn(con, kid, "telefon", **{kol: verdi})


def test_init_er_idempotent_og_beholder_data(con):
    kid = _kurs(con)
    db.lagre_skjemafelt(con, kid, "telefon", {"label": "Mobil"})
    con.commit()
    db.init(con)
    db.init(con)
    assert db.hent_skjemaoverstyringer(con, kid).overstyringer["telefon"] == Overstyring(label="Mobil")


def test_eksisterende_database_uten_tabellen_faar_den_ved_init(con):
    con.execute("DROP TABLE kurs_skjemafelt")
    con.commit()
    db.init(con)
    assert con.execute("SELECT COUNT(*) FROM kurs_skjemafelt").fetchone()[0] == 0


def test_sletting_av_kurs_fjerner_overstyringene(con):
    if not db.er_postgres(con):                  # PostgreSQL haandhever alltid fremmednokler
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    kid, annet = _kurs(con), _kurs(con, "S2")
    db.lagre_skjemafelt(con, kid, "telefon", {"synlig": False})
    db.lagre_skjemafelt(con, kid, "hpr_nr", {"hjelpetekst": "Hjelp"})
    db.lagre_skjemafelt(con, annet, "telefon", {"synlig": False})
    con.execute("DELETE FROM kurs WHERE id=?", (kid,))
    assert _rader(con, kid) == []
    assert len(_rader(con, annet)) == 1


# ============================ lagring: gyldig ============================

@pytest.mark.parametrize("felt", ["telefon", "arbeidssted"])
def test_gyldig_lagring_av_alle_egenskaper(con, felt):
    kid = _kurs(con)
    assert db.lagre_skjemafelt(con, kid, felt, {"synlig": False, "obligatorisk": True, "rekkefolge": 7,
                                                "label": "Egen ledetekst", "hjelpetekst": "Linje 1\nLinje 2"},
                               aktor="admin:test") is True
    rad = _rader(con, kid)[0]
    assert (rad["felt"], rad["synlig"], rad["obligatorisk"], rad["rekkefolge"], rad["label"], rad["hjelpetekst"],
            rad["oppdatert_av"]) == (felt, 0, 1, 7, "Egen ledetekst", "Linje 1\nLinje 2", "admin:test")
    assert db.hent_skjemaoverstyringer(con, kid).overstyringer == {felt: Overstyring(False, True, 7, "Egen ledetekst",
                                                                                    "Linje 1\nLinje 2")}


def test_gyldig_hpr_hjelpetekst(con):
    kid = _kurs(con, spesialistlop="EFT")
    assert db.lagre_skjemafelt(con, kid, "hpr_nr", {"hjelpetekst": "Finnes i Helsepersonellregisteret."})
    assert db.hent_skjemaoverstyringer(con, kid).overstyringer["hpr_nr"] == Overstyring(
        hjelpetekst="Finnes i Helsepersonellregisteret.")


def test_oppdatert_av_er_valgfri(con):
    kid = _kurs(con)
    db.lagre_skjemafelt(con, kid, "telefon", {"label": "Mobil"})
    assert _rader(con, kid)[0]["oppdatert_av"] == "system"


def test_lagring_committer_ikke(con):
    kid = _kurs(con)
    con.commit()
    db.lagre_skjemafelt(con, kid, "telefon", {"label": "Mobil"})
    con.rollback()
    assert _rader(con, kid) == []


# ============================ lagring: avvist ============================

def _antall(con):
    return (con.execute("SELECT COUNT(*) FROM kurs_skjemafelt").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM hendelse").fetchone()[0])


@pytest.mark.parametrize("felt", ["navn", "epost", "samtykke"])
def test_laast_felt_avvises(con, felt):
    kid = _kurs(con)
    foer = _antall(con)
    for egenskaper in ({"label": "X"}, {"synlig": False}, {"obligatorisk": False}, {}):
        with pytest.raises(SkjemafeltFeil) as e:
            db.lagre_skjemafelt(con, kid, felt, egenskaper)
        assert (e.value.grunn, e.value.felt) == (sf.LAAST_FELT, felt)
    assert _antall(con) == foer


@pytest.mark.parametrize("felt", ["allergier", "tilrettelegging", "betaler", "org_nr", "faktura_epost", "betaling",
                                  "eget_felt", "Telefon", " telefon", "", None, 5])
def test_ukjent_felt_og_systemblokkfelt_avvises(con, felt):
    kid = _kurs(con)
    foer = _antall(con)
    with pytest.raises(SkjemafeltFeil) as e:
        db.lagre_skjemafelt(con, kid, felt, {"label": "X"})
    assert e.value.grunn == sf.UKJENT_FELT and e.value.felt is None
    assert _antall(con) == foer


@pytest.mark.parametrize("egenskaper", [{"placeholder": "x"}, {"type": "email"}, {"felt": "navn"}, {"kurs_id": 2},
                                        {"label": "OK", "oppdatert_av": "x"}, {"synlig; DROP TABLE kurs": 1}])
def test_ukjent_egenskap_avvises(con, egenskaper):
    kid = _kurs(con)
    foer = _antall(con)
    with pytest.raises(SkjemafeltFeil) as e:
        db.lagre_skjemafelt(con, kid, "telefon", egenskaper)
    assert e.value.grunn == sf.UKJENT_EGENSKAP
    assert _antall(con) == foer


@pytest.mark.parametrize("egenskap,verdi", [("synlig", False), ("synlig", True), ("obligatorisk", True),
                                            ("obligatorisk", False), ("label", "Annet"), ("rekkefolge", 0),
                                            ("synlig", None), ("label", None)])
def test_hpr_laasene_kan_ikke_omgaas_ved_lagring(con, egenskap, verdi):
    """Ogsaa None (= «standard») avvises: selve forsoket paa en ikke-tillatt egenskap er en feil hos kalleren."""
    kid = _kurs(con, spesialistlop="EFT")
    with pytest.raises(SkjemafeltFeil) as e:
        db.lagre_skjemafelt(con, kid, "hpr_nr", {egenskap: verdi, "hjelpetekst": "Gyldig"})
    assert (e.value.grunn, e.value.felt, e.value.egenskap) == (sf.IKKE_TILLATT, "hpr_nr", egenskap)
    assert _rader(con, kid) == []


@pytest.mark.parametrize("egenskaper", [None, [], "label=X", {("label",): "X"}])
def test_ugyldig_format_avvises(con, egenskaper):
    kid = _kurs(con)
    with pytest.raises(SkjemafeltFeil):
        db.lagre_skjemafelt(con, kid, "telefon", egenskaper)


# ============================ normalisering ============================

@pytest.mark.parametrize("felt,egenskaper", [
    ("telefon", {"synlig": True}), ("telefon", {"obligatorisk": False}), ("telefon", {"label": "Telefon"}),
    ("telefon", {"label": "   Telefon  "}), ("telefon", {"rekkefolge": 1}), ("telefon", {"hjelpetekst": ""}),
    ("arbeidssted", {"synlig": True, "obligatorisk": False, "label": "Arbeidssted", "rekkefolge": 2}),
    ("telefon", {"label": "   ", "hjelpetekst": " \n\r\n "}), ("hpr_nr", {"hjelpetekst": "   "}),
    ("telefon", {"synlig": None, "obligatorisk": None, "rekkefolge": None, "label": None, "hjelpetekst": None}),
    ("telefon", {}),
])
def test_standardverdier_og_tom_tekst_lagres_ikke(con, felt, egenskaper):
    kid = _kurs(con)
    assert db.lagre_skjemafelt(con, kid, felt, egenskaper) is False
    assert _rader(con, kid) == [] and _hendelser(con, "skjemafelt_endret") == []


def test_kun_avvikene_lagres_standard_blir_null(con):
    kid = _kurs(con)
    db.lagre_skjemafelt(con, kid, "telefon", {"synlig": True, "obligatorisk": True, "label": "Telefon",
                                              "rekkefolge": 1, "hjelpetekst": "  Hjelp  "})
    rad = _rader(con, kid)[0]
    assert (rad["synlig"], rad["obligatorisk"], rad["rekkefolge"], rad["label"], rad["hjelpetekst"]) == (
        None, 1, None, None, "Hjelp")


def test_rad_som_blir_tom_slettes(con):
    kid = _kurs(con)
    db.lagre_skjemafelt(con, kid, "telefon", {"label": "Mobil", "synlig": False})
    assert db.lagre_skjemafelt(con, kid, "telefon", {"label": "Telefon", "synlig": True}) is True
    assert _rader(con, kid) == []
    assert _hendelser(con, "skjemafelt_tilbakestilt") == [{"kurs_id": kid, "felt": "telefon", "_aktor": "system"}]


def test_oppdatering_erstatter_hele_overstyringen(con):
    kid = _kurs(con)
    db.lagre_skjemafelt(con, kid, "telefon", {"label": "Mobil", "hjelpetekst": "Hjelp"})
    db.lagre_skjemafelt(con, kid, "telefon", {"obligatorisk": True})
    assert db.hent_skjemaoverstyringer(con, kid).overstyringer["telefon"] == Overstyring(obligatorisk=True)


def test_oppdatert_endres_kun_ved_reell_endring(con):
    kid = _kurs(con)
    db.lagre_skjemafelt(con, kid, "telefon", {"label": "Mobil"}, aktor="admin:a")
    con.execute("UPDATE kurs_skjemafelt SET oppdatert='2000-01-01T00:00:00'")
    antall_logg = len(_hendelser(con, "skjemafelt_endret"))
    assert db.lagre_skjemafelt(con, kid, "telefon", {"label": "  Mobil "}, aktor="admin:b") is False
    rad = _rader(con, kid)[0]
    assert (rad["oppdatert"], rad["oppdatert_av"]) == ("2000-01-01T00:00:00", "admin:a")
    assert len(_hendelser(con, "skjemafelt_endret")) == antall_logg
    assert db.lagre_skjemafelt(con, kid, "telefon", {"label": "Mobiltelefon"}, aktor="admin:b") is True
    rad = _rader(con, kid)[0]
    assert rad["oppdatert"] != "2000-01-01T00:00:00" and rad["oppdatert_av"] == "admin:b"


def test_korrupt_eksisterende_rad_overskrives_rent_ved_lagring(con):
    kid = _kurs(con)
    if db.er_postgres(con):     # typesikker kolonne / ingen NUL: korrupsjonen kan kun vaere et kontrolltegn
        _sett_inn(con, kid, "telefon", label="\x01")
    else:
        _sett_inn(con, kid, "telefon", rekkefolge="abc", label="\x00")
    assert db.lagre_skjemafelt(con, kid, "telefon", {"label": "Mobil"}) is True
    rad = _rader(con, kid)[0]
    assert (rad["rekkefolge"], rad["label"]) == (None, "Mobil")


def test_logg_inneholder_aldri_tekstinnhold(con):
    kid = _kurs(con)
    db.lagre_skjemafelt(con, kid, "telefon", {"label": "HEMMELIG-LABEL", "hjelpetekst": "HEMMELIG-HJELP",
                                              "obligatorisk": True}, aktor="admin:a")
    assert _hendelser(con, "skjemafelt_endret") == [
        {"kurs_id": kid, "felt": "telefon", "egenskaper": ["obligatorisk", "label", "hjelpetekst"], "_aktor": "admin:a"}]
    alt = " ".join(str(r["detaljer"]) for r in con.execute("SELECT detaljer FROM hendelse"))
    assert "HEMMELIG" not in alt


# ============================ tilbakestilling ============================

def test_tilbakestill_ett_felt(con):
    kid = _kurs(con)
    db.lagre_skjemafelt(con, kid, "telefon", {"label": "Mobil"})
    db.lagre_skjemafelt(con, kid, "arbeidssted", {"label": "Arbeidsgiver"})
    assert db.tilbakestill_skjemafelt(con, kid, "telefon", aktor="admin:a") is True
    assert db.tilbakestill_skjemafelt(con, kid, "telefon") is False
    assert [r["felt"] for r in _rader(con, kid)] == ["arbeidssted"]
    assert _hendelser(con, "skjemafelt_tilbakestilt") == [{"kurs_id": kid, "felt": "telefon", "_aktor": "admin:a"}]


def test_tilbakestill_laast_felt_rydder_korrupt_rad(con):
    kid = _kurs(con)
    _sett_inn(con, kid, "navn", obligatorisk=0)
    assert db.tilbakestill_skjemafelt(con, kid, "navn") is True
    assert _rader(con, kid) == []


@pytest.mark.parametrize("felt", ["eget_felt", "allergier", None, ""])
def test_tilbakestill_ukjent_felt_avvises(con, felt):
    kid = _kurs(con)
    with pytest.raises(SkjemafeltFeil) as e:
        db.tilbakestill_skjemafelt(con, kid, felt)
    assert e.value.grunn == sf.UKJENT_FELT


def test_tilbakestill_hele_kurset_inkludert_korrupte_rader(con):
    kid, annet = _kurs(con), _kurs(con, "S2")
    db.lagre_skjemafelt(con, kid, "telefon", {"label": "Mobil"})
    db.lagre_skjemafelt(con, kid, "hpr_nr", {"hjelpetekst": "Hjelp"})
    _sett_inn(con, kid, "ukjent", label="x")
    db.lagre_skjemafelt(con, annet, "telefon", {"label": "Mobil"})
    assert db.tilbakestill_skjema(con, kid, aktor="admin:a") == 3
    assert db.tilbakestill_skjema(con, kid) == 0
    assert _rader(con, kid) == [] and len(_rader(con, annet)) == 1
    assert _hendelser(con, "skjema_tilbakestilt") == [{"kurs_id": kid, "antall": 3, "_aktor": "admin:a"}]


# ============================ tekstvalidering ============================

def _feil(felt, egenskaper):
    with pytest.raises(SkjemafeltFeil) as e:
        sf.normaliser_overstyring(felt, egenskaper)
    return e.value


def test_label_80_tillatt_81_avvist():
    assert sf.normaliser_overstyring("telefon", {"label": "x" * 80}).label == "x" * 80
    assert sf.normaliser_overstyring("telefon", {"label": "  " + "x" * 80 + "  "}).label == "x" * 80
    assert _feil("telefon", {"label": "x" * 81}).grunn == sf.FOR_LANG


def test_hjelpetekst_300_tillatt_301_avvist():
    assert sf.normaliser_overstyring("telefon", {"hjelpetekst": "x" * 300}).hjelpetekst == "x" * 300
    assert _feil("telefon", {"hjelpetekst": "x" * 301}).grunn == sf.FOR_LANG
    assert _feil("hpr_nr", {"hjelpetekst": "x" * 301}).grunn == sf.FOR_LANG


def test_hjelpetekst_linjeskift_tillatt_crlf_normaliseres_og_telles_etterpaa():
    assert sf.normaliser_overstyring("telefon", {"hjelpetekst": "A\r\nB\rC\nD"}).hjelpetekst == "A\nB\nC\nD"
    # 150 x "a\r\n" = 450 tegn raatt, men 299 tegn etter CRLF->LF og trim -> tillatt
    assert len(sf.normaliser_overstyring("telefon", {"hjelpetekst": "a\r\n" * 150}).hjelpetekst) == 299


@pytest.mark.parametrize("tegn", ["\n", "\r", "\t", "\x00", "\x07", "\x1b", "\x7f", "\x85", " ", " "])
def test_label_avviser_linjeskift_og_kontrolltegn(tegn):
    assert _feil("telefon", {"label": f"A{tegn}B"}).grunn == sf.KONTROLLTEGN


@pytest.mark.parametrize("tegn", ["\t", "\x00", "\x07", "\x0b", "\x0c", "\x1b", "\x7f", "\x85", " ", " "])
def test_hjelpetekst_avviser_andre_kontrolltegn(tegn):
    assert _feil("telefon", {"hjelpetekst": f"A{tegn}B"}).grunn == sf.KONTROLLTEGN


@pytest.mark.parametrize("tekst", ["<script>alert(1)</script>", "{{ config }}", "{% raw %}x{% endraw %}",
                                   "<b>fet</b> & <i>kursiv</i>", "**markdown** [lenke](http://x)", "{navn}"])
def test_html_jinja_markdown_lagres_som_ren_tekst(con, tekst):
    kid = _kurs(con)
    db.lagre_skjemafelt(con, kid, "telefon", {"label": tekst, "hjelpetekst": tekst})
    assert db.hent_skjemaoverstyringer(con, kid).overstyringer["telefon"] == Overstyring(label=tekst, hjelpetekst=tekst)


def test_label_fra_overstyring_escapes_ved_rendring(con):
    """kurs.html (uendret i 12C2) escaper en label fra et effektivt skjema - ingenting tolkes."""
    kid = _kurs(con)
    db.lagre_skjemafelt(con, kid, "telefon", {"label": "<script>x</script> {{ 7*7 }}"})
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    skjema = sf.effektivt_skjema(kurs, db.hent_skjemaoverstyringer(con, kid))
    from kurs.web import app as webapp
    with webapp.app.test_request_context("/"):
        from kurs import paameldingsside   # 12C5: malen krever side
        html = render_template("kurs.html", kurs=kurs, dager=[], f={}, plasser_igjen=None, skjema=skjema,
                               side=paameldingsside.effektiv_side(kurs))
    assert '<label for="f-telefon">&lt;script&gt;x&lt;/script&gt; {{ 7*7 }}</label>' in html
    assert "<script>x</script>" not in html


@pytest.mark.parametrize("label", [5, b"x", ["x"], True])
def test_label_maa_vaere_tekst(label):
    assert _feil("telefon", {"label": label}).grunn == sf.UGYLDIG_TYPE


# ============================ bool / rekkefolge ============================

@pytest.mark.parametrize("egenskap", ["synlig", "obligatorisk"])
@pytest.mark.parametrize("verdi", [1, 0, "1", "true", "ja", "on", 1.0, [], "False"])
def test_bool_egenskaper_krever_ekte_bool(egenskap, verdi):
    assert _feil("telefon", {egenskap: verdi}).grunn == sf.UGYLDIG_TYPE


@pytest.mark.parametrize("verdi", [True, False, "1", "abc", 1.0, 1.5, [1]])
def test_rekkefolge_krever_heltall_og_ikke_bool(verdi):
    assert _feil("telefon", {"rekkefolge": verdi}).grunn == sf.UGYLDIG_TYPE


def test_rekkefolge_utenfor_sqlite_heltall_avvises_men_ingen_produktgrense():
    assert sf.normaliser_overstyring("telefon", {"rekkefolge": -1000}).rekkefolge == -1000
    assert sf.normaliser_overstyring("telefon", {"rekkefolge": 2 ** 63 - 1}).rekkefolge == 2 ** 63 - 1
    assert _feil("telefon", {"rekkefolge": 2 ** 63}).grunn == sf.UTENFOR_OMRAADE


def test_rekkefolge_0_er_en_ekte_overstyring(con):
    kid = _kurs(con)
    assert db.lagre_skjemafelt(con, kid, "arbeidssted", {"rekkefolge": 0}) is True
    assert _rader(con, kid)[0]["rekkefolge"] == 0      # 0, ikke NULL
    les = db.hent_skjemaoverstyringer(con, kid)
    assert les.overstyringer["arbeidssted"] == Overstyring(rekkefolge=0) and not les.har_advarsler
    assert _nokler(sf.effektivt_skjema(KURS, les)) == ["arbeidssted", "telefon"]


def test_rekkefolge_0_for_telefon_er_ogsaa_et_avvik():
    """Standard for telefon er 1 - 0 er et avvik og skal lagres, ikke tolkes som «ingen»."""
    assert sf.normaliser_overstyring("telefon", {"rekkefolge": 0}) == Overstyring(rekkefolge=0)


# ============================ effektivt_skjema med overstyringer ============================

def test_default_uten_overstyringer_er_identisk_med_12c1():
    forventet = sf.EffektivtSkjema((sf.EffektivtFelt("telefon", "Telefon", False),
                                    sf.EffektivtFelt("arbeidssted", "Arbeidssted", False),
                                    sf.EffektivtFelt("hpr_nr", "HPR-nummer", False)), True, True)
    tomt = sf.Leseresultat(sf.MappingProxyType({}), ())
    for skjema in (sf.effektivt_skjema(KURS_LOP), sf.effektivt_skjema(KURS_LOP, None),
                   sf.effektivt_skjema(KURS_LOP, {}), sf.effektivt_skjema(KURS_LOP, tomt)):
        assert skjema == forventet


@pytest.mark.parametrize("felt,annet", [("telefon", "arbeidssted"), ("arbeidssted", "telefon")])
def test_skjult_felt_forsvinner(felt, annet):
    s = sf.effektivt_skjema(KURS, {felt: Overstyring(synlig=False)})
    assert _nokler(s) == [annet]


@pytest.mark.parametrize("felt", ["telefon", "arbeidssted"])
def test_obligatorisk_label_og_hjelpetekst_brukes(felt):
    s = sf.effektivt_skjema(KURS, {felt: Overstyring(obligatorisk=True, label="Ny", hjelpetekst="Hjelp")})
    assert _felt(s, felt) == sf.EffektivtFelt(felt, "Ny", True, "Hjelp")


def test_begge_kan_skjules():
    s = sf.effektivt_skjema(KURS_LOP, {"telefon": Overstyring(synlig=False), "arbeidssted": Overstyring(synlig=False)})
    assert _nokler(s) == ["hpr_nr"]


@pytest.mark.parametrize("over,forventet", [
    ({"telefon": Overstyring(rekkefolge=3)}, ["arbeidssted", "telefon"]),
    ({"arbeidssted": Overstyring(rekkefolge=0)}, ["arbeidssted", "telefon"]),
    ({"arbeidssted": Overstyring(rekkefolge=-5)}, ["arbeidssted", "telefon"]),
    ({"telefon": Overstyring(rekkefolge=2)}, ["telefon", "arbeidssted"]),          # likt -> standardindeks
    ({"telefon": Overstyring(rekkefolge=9), "arbeidssted": Overstyring(rekkefolge=9)}, ["telefon", "arbeidssted"]),
    ({"telefon": Overstyring(rekkefolge=2), "arbeidssted": Overstyring(rekkefolge=1)}, ["arbeidssted", "telefon"]),
])
def test_stabil_sortering_paa_effektiv_rekkefolge_og_standardindeks(over, forventet):
    assert _nokler(sf.effektivt_skjema(KURS, over)) == forventet


def test_hpr_staar_alltid_sist_uansett_rekkefolge():
    s = sf.effektivt_skjema(KURS_LOP, {"telefon": Overstyring(rekkefolge=10 ** 9),
                                       "arbeidssted": Overstyring(rekkefolge=10 ** 9 + 1)})
    assert _nokler(s)[-1] == "hpr_nr"


def test_hpr_hjelpetekst_brukes():
    s = sf.effektivt_skjema(KURS_LOP, {"hpr_nr": Overstyring(hjelpetekst="Hjelp")})
    assert _felt(s, "hpr_nr") == sf.EffektivtFelt("hpr_nr", "HPR-nummer", False, "Hjelp")


def test_hpr_laasene_kan_ikke_omgaas_med_haandlaget_overstyring():
    """Forsvar i dybden: selv en Overstyring som aldri gikk gjennom validering kan ikke skjule/flytte/relable HPR
    eller gjore det obligatorisk."""
    o = Overstyring(synlig=False, obligatorisk=True, rekkefolge=-100, label="Annet", hjelpetekst="Hjelp")
    s = sf.effektivt_skjema(KURS_LOP, {"hpr_nr": o})
    assert _nokler(s) == ["telefon", "arbeidssted", "hpr_nr"]
    assert _felt(s, "hpr_nr") == sf.EffektivtFelt("hpr_nr", "HPR-nummer", False, "Hjelp")


def test_hpr_folger_spesialistlop_uansett_overstyring():
    assert _felt(sf.effektivt_skjema(KURS, {"hpr_nr": Overstyring(synlig=True, hjelpetekst="H")}), "hpr_nr") is None


def test_laaste_felt_og_systemblokker_paavirkes_aldri():
    over = {n: Overstyring(synlig=False, obligatorisk=False, label="X", rekkefolge=0, hjelpetekst="H")
            for n in ("navn", "epost", "samtykke", "allergier", "tilrettelegging", "betaler", "faktura", "sensitivt")}
    for kurs in (KURS, KURS_LOP, {**KURS, "type": "digital"}, {**KURS, "fakturering": "ingen"}):
        assert sf.effektivt_skjema(kurs, over) == sf.effektivt_skjema(kurs)


@pytest.mark.parametrize("kurs,faktura,sensitivt", [
    (KURS, True, True), ({**KURS, "type": "digital"}, True, False), ({**KURS, "pris_nok": 0}, False, True),
    ({**KURS, "fakturering": "organisasjon"}, False, True)])
def test_blokkene_folger_kursdata_uansett_overstyringer(kurs, faktura, sensitivt):
    over = {"telefon": Overstyring(synlig=False), "arbeidssted": Overstyring(obligatorisk=True)}
    s = sf.effektivt_skjema(kurs, over)
    assert (s.vis_fakturablokk, s.vis_sensitive_felt) == (faktura, sensitivt)


@pytest.mark.parametrize("o", [
    Overstyring(synlig="nei"), Overstyring(synlig=0), Overstyring(obligatorisk=1), Overstyring(label="x" * 81),
    Overstyring(label="A\nB"), Overstyring(hjelpetekst="\x00"), Overstyring(rekkefolge=True), Overstyring(rekkefolge="1"),
    {"synlig": False}, "synlig=0", None,
])
def test_ugyldige_haandlagde_overstyringer_ignoreres(o):
    assert sf.effektivt_skjema(KURS, {"telefon": o}) == sf.effektivt_skjema(KURS)


def test_ugyldig_egenskap_ignoreres_men_gyldig_i_samme_overstyring_brukes():
    s = sf.effektivt_skjema(KURS, {"telefon": Overstyring(synlig="nei", obligatorisk=True, label="x" * 81)})
    assert _felt(s, "telefon") == sf.EffektivtFelt("telefon", "Telefon", True)


def test_overstyring_har_noyaktig_egenskapene_som_er_kolonne_whitelist():
    assert tuple(f.name for f in dataclasses.fields(Overstyring)) == sf.EGENSKAPER_REKKEFOLGE
    assert set(sf.EGENSKAPER_REKKEFOLGE) == sf.EGENSKAPER


# ============================ korrupte rader (direkte i databasen) ============================

def _les(con, kid):
    return db.hent_skjemaoverstyringer(con, kid)


def test_ukjent_felt_ignoreres_med_advarsel_uten_feltnavn(con):
    kid = _kurs(con)
    _sett_inn(con, kid, "hemmelig_felt_navn", label="HEMMELIG", synlig=0)
    les = _les(con, kid)
    assert dict(les.overstyringer) == {} and les.advarsler == (Advarsel(sf.UKJENT_FELT),)
    assert "hemmelig" not in repr(les.advarsler).lower()


@pytest.mark.parametrize("felt", ["navn", "epost", "samtykke"])
def test_rad_for_laast_felt_ignoreres(con, felt):
    kid = _kurs(con)
    _sett_inn(con, kid, felt, synlig=0, obligatorisk=0, label="X")
    les = _les(con, kid)
    assert dict(les.overstyringer) == {} and les.advarsler == (Advarsel(sf.LAAST_FELT, felt),)


@pytest.mark.parametrize("kol,verdi", [("label", "Annet"), ("synlig", 0), ("synlig", 1), ("obligatorisk", 1),
                                       ("rekkefolge", -3)])
def test_hpr_ikke_tillatt_egenskap_ignoreres_hjelpetekst_i_samme_rad_brukes(con, kol, verdi):
    kid = _kurs(con, spesialistlop="EFT")
    _sett_inn(con, kid, "hpr_nr", hjelpetekst="Gyldig hjelp", **{kol: verdi})
    les = _les(con, kid)
    assert les.overstyringer["hpr_nr"] == Overstyring(hjelpetekst="Gyldig hjelp")
    assert les.advarsler == (Advarsel(sf.IKKE_TILLATT, "hpr_nr", kol),)
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    s = sf.effektivt_skjema(kurs, les)
    assert _nokler(s) == ["telefon", "arbeidssted", "hpr_nr"]
    assert _felt(s, "hpr_nr") == sf.EffektivtFelt("hpr_nr", "HPR-nummer", False, "Gyldig hjelp")


@pytest.mark.parametrize("kol,verdi,grunn", [
    ("label", "x" * 81, sf.FOR_LANG), ("hjelpetekst", "x" * 301, sf.FOR_LANG),
    ("label", "A\nB", sf.KONTROLLTEGN), ("label", "A\x00B", sf.KONTROLLTEGN), ("hjelpetekst", "A\tB", sf.KONTROLLTEGN),
    ("rekkefolge", "abc", sf.UGYLDIG_TYPE), ("rekkefolge", 1.5, sf.UGYLDIG_TYPE),
    ("label", b"\x00\x01", sf.UGYLDIG_TYPE), ("hjelpetekst", b"x", sf.UGYLDIG_TYPE),
])
def test_ugyldig_egenskap_faller_tilbake_til_standard_resten_brukes(con, kol, verdi, grunn):
    if db.er_postgres(con) and _ulagringsbar_i_postgres(kol, verdi):
        pytest.skip("verdien kan ikke lagres i PostgreSQL (typesikker kolonne / NUL) - korrupsjonen kan ikke oppstaa der")
    kid = _kurs(con)
    _sett_inn(con, kid, "telefon", obligatorisk=1, **{kol: verdi})
    les = _les(con, kid)
    assert les.overstyringer["telefon"] == Overstyring(obligatorisk=True)
    assert les.advarsler == (Advarsel(grunn, "telefon", kol),)
    assert _felt(sf.effektivt_skjema(KURS, les), "telefon") == sf.EffektivtFelt("telefon", "Telefon", True)


def test_sqlite_konverterer_tall_i_tekstkolonne_til_tekst(con):
    """Dokumenterer SQLite-affinitet: et heltall i label-kolonnen lagres som TEXT '5' og er da en gyldig label."""
    kid = _kurs(con)
    _sett_inn(con, kid, "telefon", label=5)
    assert _les(con, kid).overstyringer["telefon"] == Overstyring(label="5")


@pytest.mark.parametrize("rad,grunn,egenskap", [
    ({"synlig": "ja"}, sf.UGYLDIG_TYPE, "synlig"), ({"synlig": 2}, sf.UGYLDIG_TYPE, "synlig"),
    ({"obligatorisk": "1"}, sf.UGYLDIG_TYPE, "obligatorisk"), ({"obligatorisk": 1.0}, sf.UGYLDIG_TYPE, "obligatorisk"),
    ({"synlig": True}, None, None),      # ikke mulig fra SQLite, men en ekte bool er gyldig
])
def test_bool_kolonner_som_check_hindrer_testes_direkte_i_valideringen(rad, grunn, egenskap):
    """CHECK-constraintene gjor disse umulige i databasen - testes derfor direkte paa les_overstyringer (schemaet
    svekkes ikke for testens skyld)."""
    raa = {"felt": "telefon", "synlig": None, "obligatorisk": None, "rekkefolge": None, "label": None,
           "hjelpetekst": None, **rad}
    les = sf.les_overstyringer([raa])
    if grunn:
        assert les.advarsler == (Advarsel(grunn, "telefon", egenskap),) and dict(les.overstyringer) == {}
    else:
        assert not les.har_advarsler


def test_standardverdier_i_databasen_er_ikke_advarsel_og_ikke_overstyring(con):
    kid = _kurs(con)
    _sett_inn(con, kid, "telefon", synlig=1, obligatorisk=0, rekkefolge=1, label="Telefon", hjelpetekst="  ")
    les = _les(con, kid)
    assert dict(les.overstyringer) == {} and not les.har_advarsler


def test_flere_korrupte_rader_gir_en_advarsel_per_problem_og_er_pii_frie(con):
    kid = _kurs(con, spesialistlop="EFT")
    _sett_inn(con, kid, "ukjent1", label="PII-1")
    _sett_inn(con, kid, "navn", label="PII-2")
    _sett_inn(con, kid, "hpr_nr", label="PII-3", synlig=0)
    _sett_inn(con, kid, "telefon", label="PII-4\n", hjelpetekst="x" * 400, synlig=0)
    _sett_inn(con, kid, "arbeidssted", label="Gyldig")
    les = _les(con, kid)
    assert dict(les.overstyringer) == {"telefon": Overstyring(synlig=False), "arbeidssted": Overstyring(label="Gyldig")}
    assert sorted(les.advarsler, key=repr) == sorted([
        Advarsel(sf.UKJENT_FELT), Advarsel(sf.LAAST_FELT, "navn"),
        Advarsel(sf.IKKE_TILLATT, "hpr_nr", "synlig"), Advarsel(sf.IKKE_TILLATT, "hpr_nr", "label"),
        Advarsel(sf.KONTROLLTEGN, "telefon", "label"), Advarsel(sf.FOR_LANG, "telefon", "hjelpetekst")], key=repr)
    assert "PII" not in repr(les.advarsler) and "x" * 10 not in repr(les.advarsler)
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    assert _nokler(sf.effektivt_skjema(kurs, les)) == ["arbeidssted", "hpr_nr"]


def test_rader_for_andre_kurs_leses_ikke(con):
    kid, annet = _kurs(con), _kurs(con, "S2")
    db.lagre_skjemafelt(con, annet, "telefon", {"synlig": False})
    assert dict(_les(con, kid).overstyringer) == {}


# ============================ DB-lesefeil ============================

class _FeilendeTilkobling:
    """Simulerer en reell lesefeil (f.eks. disk I/O / laast database) paa selve SQL-lesingen."""
    def execute(self, *a, **kw):
        raise sqlite3.OperationalError("disk I/O error")


@pytest.mark.parametrize("oppsett", ["mangler_tabell", "lukket", "io_feil"])
def test_db_lesefeil_blir_aldri_et_tomt_overstyringssett(con, oppsett):
    kid = _kurs(con)
    db.lagre_skjemafelt(con, kid, "telefon", {"synlig": False})
    con.commit()
    c = con
    if oppsett == "mangler_tabell":
        con.execute("DROP TABLE kurs_skjemafelt")
    elif oppsett == "lukket":
        c = db.koble(config.DB_STI)
        c.close()
    else:
        c = _FeilendeTilkobling()
    with pytest.raises(SkjemaLesefeil) as e:
        db.hent_skjemaoverstyringer(c, kid)
    assert isinstance(e.value.__cause__, sqlite3.Error)
    assert e.value.kurs_id == kid
    assert str(e.value) == f"skjema_lesefeil (kurs {kid})"   # ingen SQL, sti eller innhold


def test_skjemalesefeil_er_ikke_en_skjemafeltfeil():
    """12C3 skal kunne skille «kan ikke lese» fra «ugyldig data» - de er urelaterte typer."""
    assert not issubclass(SkjemaLesefeil, SkjemafeltFeil) and not issubclass(SkjemafeltFeil, SkjemaLesefeil)

# (12C2-vakten «offentlig side ignorerer DB-overstyringer» er fjernet i 12C3, der siden bevisst begynner aa bruke dem -
# se tests/test_skjemafelt_paamelding.py.)
