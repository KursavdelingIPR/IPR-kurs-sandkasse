"""Globalt deltakersøk for administratorer (kurs/deltakersok.py, /admin/sok og /admin/sok.json).

Bruksområdet: «Kari sier hun mangler kursbevis» - da må administrasjonen finne henne på tvers av alle kurs (navn,
e-post, telefon, HPR-nummer, påmeldingsnummer, kursnummer/kurskode) og se hvorfor kursbeviset eventuelt mangler.
Alle personer og data her er oppdiktede.
"""
import ast
import inspect
import logging
import re
from datetime import date

import pytest

from kurs import config, db, deltakersok, kursbevis
from kurs.integrasjoner import epost
from kurs.kjoring import Kjoring
from kurs.web import app as webapp
from kurs.web import sikkerhet

IDAG = date(2027, 6, 1)


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "ROT", tmp_path)
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    monkeypatch.setattr(webapp, "_idag", lambda: IDAG)
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


@pytest.fixture
def sendt(monkeypatch):
    ut = []
    monkeypatch.setattr(epost, "send", lambda til, emne, html, *a, **kw: ut.append((til, emne)))
    return ut


# ============================ testdata ============================

def _kurs(con, kode, navn, datoer, status="aapen", **kw):
    kid = db.opprett_kurs(con, kode=kode, navn=navn, datoer=list(datoer), sharepoint_mappe=f"Kurs/{kode}", type="fysisk",
                          pris_nok=0, fakturering="ingen", **kw)
    con.execute("UPDATE kurs SET status=? WHERE id=?", (status, kid))
    return kid


def _meld(con, kid, fornavn, etternavn, epost_, telefon=None, hpr=None, arbeidssted=None, moter=0, sensitivt=None):
    """Melder paa (kurset settes midlertidig aapent) og registrerer oppmote paa de `moter` foerste kursdagene."""
    status = con.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()[0]
    con.execute("UPDATE kurs SET status='aapen' WHERE id=?", (kid,))
    deltaker = {k: v for k, v in (("telefon", telefon), ("hpr_nr", hpr), ("arbeidssted", arbeidssted)) if v}
    pid, _ = db.meld_paa(con, kid, epost=epost_, fornavn=fornavn, etternavn=etternavn, deltaker=deltaker,
                         sensitivt=sensitivt, ignorer_frist=True)
    con.execute("UPDATE kurs SET status=? WHERE id=?", (status, kid))
    for dag in db.kursdager(con, kid)[:moter]:
        db.registrer_oppmote(con, pid, dag["id"], "manuell")
    did = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pid,)).fetchone()[0]
    return pid, did


def _bevis(con, kid, deltaker_id, tid="2027-03-03 07:00:01"):
    con.execute("INSERT INTO dokument (kurs_id, deltaker_id, type, tittel, url, opprettet) VALUES (?,?,?,?,?,?)",
                (kid, deltaker_id, "kursbevis", "Kursbevis", "db:", tid))


class Data:
    """Fire kurs og en håndfull personer, blant dem tre «Kari»."""


def _bygg(con) -> Data:
    d = Data()
    d.eft = _kurs(con, "EFT-2027-1", "Emosjonsfokusert terapi", ["2027-03-01", "2027-03-02"], "avsluttet")
    d.vig = _kurs(con, "VIG-2027-2", "Veiledning i grupper", ["2027-04-12"], "avsluttet")
    d.skj = _kurs(con, "SKJ-2027-3", "Skjematerapi", ["2027-09-06", "2027-09-07"], "aapen")
    d.avl = _kurs(con, "AVL-2027-4", "Avlyst temadag", ["2027-05-05"], "avlyst")
    d.kursnr = {k: con.execute("SELECT kursnr FROM kurs WHERE id=?", (v,)).fetchone()[0]
                for k, v in (("eft", d.eft), ("vig", d.vig), ("skj", d.skj))}
    d.hansen, d.hansen_id = _meld(con, d.eft, "Kari", "Hansen", "kari.hansen@eksempel.no", "900 12 345", "1234567",
                                  "Nordlys legesenter", moter=2)
    _bevis(con, d.eft, d.hansen_id)
    d.hansen_vig, _ = _meld(con, d.vig, "Kari", "Hansen", "kari.hansen@eksempel.no", moter=0)
    d.hansen_skj, _ = _meld(con, d.skj, "Kari", "Hansen", "kari.hansen@eksempel.no")
    d.nilsen, d.nilsen_id = _meld(con, d.eft, "Kari", "Nilsen", "kari.nilsen@eksempel.no", "+47 411 22 333", moter=1)
    d.olsen, d.olsen_id = _meld(con, d.eft, "Kari", "Olsen", "kari.olsen@eksempel.no", "0047 99887766", moter=2)
    d.orjan, d.orjan_id = _meld(con, d.eft, "Ørjan", "Åsheim", "orjan.asheim@eksempel.no", moter=2)
    d.ase, d.ase_id = _meld(con, d.vig, "Åse", "Ørsted", "ase.orsted@eksempel.no", moter=1)
    d.berg, d.berg_id = _meld(con, d.eft, "Marie", "Berg", "marie.berg@eksempel.no", moter=2)
    db.meld_av(con, d.berg)
    con.commit()
    return d


def _navn(res) -> list[str]:
    return [p["navn"] for p in res.personer]


def _s(con, tekst, idag=IDAG, **kw):
    return deltakersok.sok(con, tekst, idag=idag, **kw)


# ============================ tolking av søket ============================

def test_tolk_ord_mellomrom_og_kanttegn():
    s = deltakersok.tolk("  Hansen,   Kari ")
    assert [t.tekst for t in s.termer] == ["hansen", "kari"] and s.tekst == "Hansen, Kari" and not s.avkortet


def test_tolk_tomt_og_for_kort():
    assert deltakersok.tolk(None).tom and deltakersok.tolk("   ").tom
    assert deltakersok.tolk("k").for_kort and not deltakersok.tolk("ka").for_kort
    assert deltakersok.tolk(" , ; ").for_kort            # bare tegnsetting = ingen søkeord
    assert not deltakersok.tolk("").gyldig and deltakersok.tolk("ka").gyldig


def test_tolk_telefonnummer_i_grupper_er_ett_ord():
    for tekst in ("+47 900 12 345", "0047 900 12 345", "0047 90012345", "+47 90012345", "(+47) 900-12-345", "900 12 345",
                  "90012345", "22 33 44 55"):
        s = deltakersok.tolk(tekst)
        assert len(s.termer) == 1 and s.termer[0].siffer in ("90012345", "22334455"), tekst
    s = deltakersok.tolk("kari 900 12 345")                         # navn + nummer: nummeret er fortsatt ett ord
    assert [t.tekst for t in s.termer] == ["kari", "900 12 345"] and s.termer[1].siffer == "90012345"
    s = deltakersok.tolk("1001 2027")                                # to fire-sifrede tall er to ord (ikke ett nummer)
    assert [t.tekst for t in s.termer] == ["1001", "2027"]


def test_tolk_paameldingsnummer_og_kursnummer():
    kun = deltakersok.tolk("#12").termer[0]
    assert kun.kun_pnr and kun.pnr == 12 and kun.kursnr is None
    tall = deltakersok.tolk("1001").termer[0]
    assert tall.pnr == 1001 and tall.kursnr == 1001 and not tall.kun_pnr
    lang = deltakersok.tolk("1" * 20).termer[0]                  # sprenger aldri databasens heltall
    assert lang.pnr is None and lang.kursnr is None


def test_tolk_avkorter_lange_soek_og_mange_ord():
    lang = deltakersok.tolk("x" * 500)
    assert lang.avkortet and len(lang.tekst) == deltakersok.MAKS_TEGN
    mange = deltakersok.tolk("aa bb cc dd ee ff gg hh")
    assert mange.avkortet and len(mange.termer) == deltakersok.MAKS_ORD
    assert not deltakersok.tolk("kari hansen").avkortet


def test_tolk_kontrolltegn_blir_mellomrom():
    s = deltakersok.tolk("kari\x00\t\nhansen\x1f")
    assert [t.tekst for t in s.termer] == ["kari", "hansen"] and "\x00" not in s.tekst


@pytest.mark.parametrize("lagret,forventet", [
    ("+47 900 12 345", "90012345"), ("0047 90012345", "90012345"), ("900-12-345", "90012345"),
    ("(900) 12345", "90012345"), ("4790012345", "90012345"), ("90012345", "90012345"),
    ("+46 70 123 45 67", "46701234567"), ("", ""), (None, "")])
def test_normaliser_telefon(lagret, forventet):
    assert deltakersok.normaliser_telefon(lagret) == forventet


# ============================ navn, store/små bokstaver og æøå ============================

def test_navn_delvis_og_i_begge_rekkefoelger(con):
    _bygg(con)
    for tekst in ("kari hansen", "hansen kari", "kar han", "HANSEN", "  Kari   Hansen  ", "ansen"):
        assert _navn(_s(con, tekst)) == ["Kari Hansen"], tekst
    assert _navn(_s(con, "kari")) == ["Kari Hansen", "Kari Nilsen", "Kari Olsen"]


@pytest.mark.parametrize("tekst,navn", [
    ("ørjan", "Ørjan Åsheim"), ("ØRJAN", "Ørjan Åsheim"), ("Ørjan", "Ørjan Åsheim"), ("åsheim", "Ørjan Åsheim"),
    ("ÅSHEIM", "Ørjan Åsheim"), ("åse", "Åse Ørsted"), ("Åse", "Åse Ørsted"), ("ÅSE", "Åse Ørsted"),
    ("ørsted", "Åse Ørsted"), ("Åse", "Åse Ørsted")])       # siste: Å skrevet som A + ring over (Mac)
def test_norske_bokstaver_og_store_smaa_finner_riktig_person(con, tekst, navn):
    _bygg(con)
    assert _navn(_s(con, tekst)) == [navn]


@pytest.mark.kun_sqlite
def test_sqlite_lower_er_bare_ascii_derfor_gjoeres_sammenligningen_i_python(con):
    """Begrunnelsen for løsningen: SQLite sin LOWER() rører ikke Ø/Å, så et SQL-søk ville funnet «Ørjan» bare med stor Ø."""
    _bygg(con)
    assert con.execute("SELECT LOWER('ØRJAN')").fetchone()[0] == "Ørjan"          # Ø er uendret, bare «RJAN» ble små
    sql = con.execute("SELECT COUNT(*) FROM deltaker WHERE LOWER(navn) LIKE '%ørjan%'").fetchone()[0]
    assert sql == 0                                                # SQL finner ikke «Ørjan» med liten ø ...
    assert _navn(_s(con, "ørjan")) == ["Ørjan Åsheim"]             # ... men søket vårt gjør det


# ============================ e-post ============================

def test_epost_delvis_og_eksakt(con):
    _bygg(con)
    assert _navn(_s(con, "kari.nilsen@")) == ["Kari Nilsen"]
    assert _navn(_s(con, "KARI.NILSEN@EKSEMPEL.NO")) == ["Kari Nilsen"]
    assert len(_s(con, "@eksempel.no").personer) == 6            # alle seks personene
    assert _navn(_s(con, "eksempel.no marie")) == ["Marie Berg"]


def test_grunn_epost_og_navn_prioriterer_navn(con):
    _bygg(con)
    p = _s(con, "hansen").personer[0]
    assert p["grunner"] == ["navn"] and p["grunn"] == "Treff på navn"     # e-posten inneholder også «hansen»
    p = _s(con, "@eksempel.no kari.hansen").personer[0]
    assert "e-post" in p["grunner"] and p["navn"] == "Kari Hansen"
    assert _s(con, "eksempel.no").personer[0]["grunn"] == "Treff på e-post"


# ============================ telefon ============================

@pytest.mark.parametrize("tekst", ["90012345", "900 12 345", "+47 900 12 345", "0047 90012345", "900-12-345",
                                   "(+47) 900 12 345", "12345", "0012", "+47 9001"])
def test_telefon_i_alle_formater_finner_kari_hansen(con, tekst):
    _bygg(con)
    res = _s(con, tekst)
    assert _navn(res) == ["Kari Hansen"], tekst
    assert "telefon" in res.personer[0]["grunner"]
    if tekst != "12345":                                            # «12345» ligger også i HPR-nummeret 1234567
        assert res.personer[0]["grunn"] == "Treff på telefon"


def test_telefon_lagret_med_landskode_finnes_uten(con):
    _bygg(con)
    assert _navn(_s(con, "41122333")) == ["Kari Nilsen"]           # lagret «+47 411 22 333»
    assert _navn(_s(con, "411 22 333")) == ["Kari Nilsen"]
    assert _navn(_s(con, "99887766")) == ["Kari Olsen"]            # lagret «0047 99887766»
    assert _navn(_s(con, "+47 998 87 766")) == ["Kari Olsen"]


def test_telefon_delvis_treff_krever_fire_sifre(con):
    _bygg(con)
    assert _s(con, "345").totalt == 0 and _s(con, "900").totalt == 0
    assert _navn(_s(con, "2345")) == ["Kari Hansen"]


# ============================ HPR og påmeldingsnummer ============================

def test_hpr_nummer(con):
    _bygg(con)
    res = _s(con, "1234567")
    assert _navn(res) == ["Kari Hansen"] and res.personer[0]["grunner"] == ["HPR-nummer"] and res.personer[0]["vis_hpr"]
    assert _navn(_s(con, "34567")) == ["Kari Hansen"]              # delvis, minst fire sifre
    assert _s(con, "567").totalt == 0


def test_paameldingsnummer_med_nummertegn(con):
    d = _bygg(con)
    for tekst in (f"#{d.nilsen}", f" #{d.nilsen} "):
        res = _s(con, tekst)
        assert _navn(res) == ["Kari Nilsen"], tekst
        assert res.personer[0]["grunn"] == f"Treff på påmeldingsnr. #{d.nilsen}"
        assert [p["paamelding_id"] for p in res.personer[0]["paameldinger"] if p["treff"]] == [d.nilsen]
    assert _s(con, "#99999").totalt == 0
    assert _s(con, "#" + "9" * 30).totalt == 0                      # for langt til å være et nummer: ingen feil
    assert _s(con, "9" * 30).totalt == 0


def test_paameldingsnummer_uten_nummertegn_fra_to_sifre(con):
    kid = _kurs(con, "PNR-1", "Nummer", ["2027-09-01"])
    for i in range(10):
        _meld(con, kid, "Fyll", f"Ut{i}", f"fyll.ut{i}@x.no")
    pid, _ = _meld(con, kid, "Nora", "Nummersen", "nora.nummersen@x.no")
    con.commit()
    assert pid >= 10
    res = _s(con, str(pid))
    assert _navn(res) == ["Nora Nummersen"] and res.personer[0]["grunn"] == f"Treff på påmeldingsnr. #{pid}"
    assert deltakersok.tolk("9").for_kort                                            # ett tegn er for kort: bruk «#9»
    assert _navn(_s(con, "#9")) == ["Fyll Ut8"]


def test_paameldingsnummer_med_nummertegn_er_ikke_kursnummer(con):
    d = _bygg(con)
    assert _s(con, f"#{d.kursnr['eft']}").totalt == 0               # #1001 er et påmeldingsnummer, ikke kursnummer


# ============================ kursnummer og kurskode ============================

def test_kursnummer_viser_alle_paameldte_paa_kurset(con):
    d = _bygg(con)
    res = _s(con, str(d.kursnr["eft"]))
    assert sorted(_navn(res)) == ["Kari Hansen", "Kari Nilsen", "Kari Olsen", "Marie Berg", "Ørjan Åsheim"]
    assert all("kurs EFT-2027-1" in p["grunner"] for p in res.personer)
    assert [k["kode"] for k in res.kurs] == ["EFT-2027-1"]         # snarvei til deltakerlisten
    treff = [m for p in res.personer for m in p["paameldinger"] if m["treff"]]
    assert len(treff) == 5 and all(m["kode"] == "EFT-2027-1" for m in treff)


def test_kurskode_hel_og_delvis(con):
    _bygg(con)
    assert len(_s(con, "EFT-2027-1").personer) == 5
    assert len(_s(con, "eft-2027").personer) == 5                   # delvis kode, minst fire tegn
    assert len(_s(con, "vig-").personer) == 2                       # Hansen (også på VIG) og Åse
    assert _s(con, "vig").totalt == 0 and _s(con, "ef").totalt == 0   # tre tegn er for lite for kurskode: ga støy mot navn
    assert deltakersok.MIN_KURSKODE == 4


def test_kurs_uten_paameldte_gir_snarvei_men_ingen_personer(con):
    _bygg(con)
    res = _s(con, "AVL-2027-4")
    assert res.totalt == 0 and [k["kode"] for k in res.kurs] == ["AVL-2027-4"]


def test_kursnummer_og_navn_sammen(con):
    d = _bygg(con)
    assert _navn(_s(con, f"kari {d.kursnr['vig']}")) == ["Kari Hansen"]      # bare Hansen er også på VIG
    assert _navn(_s(con, f"ørjan {d.kursnr['vig']}")) == []


# ============================ flere ord: alle må treffe ============================

def test_alle_ord_maa_treffe_samme_person(con):
    _bygg(con)
    assert _navn(_s(con, "kari nilsen")) == ["Kari Nilsen"]
    assert _s(con, "kari xyz").totalt == 0
    assert _navn(_s(con, "kari 411 22 333")) == ["Kari Nilsen"]     # navn + telefon skrevet i grupper
    assert _navn(_s(con, "kari +47 411 22 333")) == ["Kari Nilsen"]
    assert _s(con, "hansen 411 22 333").totalt == 0
    assert _navn(_s(con, "nilsen eksempel.no")) == ["Kari Nilsen"]  # navn + del av e-post


def test_ord_utover_seks_ignoreres_men_soket_virker(con):
    _bygg(con)
    res = _s(con, "kari hansen kari hansen kari hansen kari hansen")
    assert res.sok.avkortet and _navn(res) == ["Kari Hansen"]


# ============================ rangering og grense ============================

def test_rangering_eksakt_foer_navn_som_starter_foer_ovrige_deretter_alfabetisk(con):
    kid = _kurs(con, "RNG-1", "Rangering", ["2027-09-01"])
    _meld(con, kid, "Nikola", "Berg", "nikola@x.no")                # «ola» midt i navnet og eksakt-lignende e-post
    _meld(con, kid, "Ola", "Nordmann", "ola@x.no")
    _meld(con, kid, "Aase", "Ola", "aase.ola@x.no")
    _meld(con, kid, "Åge", "Solaas", "age@x.no")
    con.commit()
    # nøyaktig e-post først, selv om «Nikola Berg» kommer før «Ola Nordmann» alfabetisk
    assert _navn(_s(con, "ola@x.no")) == ["Ola Nordmann", "Aase Ola", "Nikola Berg"]      # e-postene til Aase og Nikola inneholder den
    # navn som starter med søket, alfabetisk, så de andre
    assert _navn(_s(con, "ola")) == ["Aase Ola", "Ola Nordmann", "Nikola Berg", "Åge Solaas"]


def test_alfabetisk_rekkefoelge_er_norsk(con):
    kid = _kurs(con, "NOR-1", "Norsk rekkefølge", ["2027-09-01"])
    for fornavn in ("Åse", "Zoe", "Ørjan", "Aase", "Øystein", "Ola"):
        _meld(con, kid, fornavn, "Testesen", f"{fornavn.lower()}.t@x.no")
    con.commit()
    assert [n.split()[0] for n in _navn(_s(con, "testesen"))] == ["Aase", "Ola", "Zoe", "Ørjan", "Øystein", "Åse"]


def test_grensen_paa_personer_med_totalt_antall(con):
    kid = _kurs(con, "GRS-1", "Mange", ["2027-09-01"])
    for i in range(60):
        _meld(con, kid, "Kari", f"Test{i:02d}", f"kari.test{i:02d}@x.no")
    con.commit()
    res = _s(con, "kari")
    assert len(res.personer) == deltakersok.GRENSE_SIDE == 50 and res.totalt == 60 and res.flere
    assert _navn(res)[0] == "Kari Test00" and _navn(res)[-1] == "Kari Test49"
    liste = _s(con, "kari", grense=deltakersok.GRENSE_LISTE)
    assert len(liste.personer) == deltakersok.GRENSE_LISTE == 8 and liste.totalt == 60
    assert not _s(con, "kari test5").flere and _s(con, "kari test5").totalt == 10


# ============================ anonymiserte finnes ikke ============================

def test_anonymiserte_personer_kan_ikke_finnes(con):
    d = _bygg(con)
    pid, did = _meld(con, d.vig, "Anne", "Anonym", "anne.anonym@eksempel.no", "777 88 999", "5544332")
    assert _navn(_s(con, "anne anonym")) == ["Anne Anonym"]        # finnes før anonymiseringen
    db.anonymiser_deltaker(con, did, aktor="test")
    con.commit()
    for tekst in ("anne", "anonym", "anonymisert", "deltaker", "anonymisert.invalid", "777 88 999", "5544332",
                  f"#{pid}", "@anonymisert.invalid", "Anonymisert deltaker"):
        assert _s(con, tekst).totalt == 0, tekst
    assert "Anonymisert deltaker" not in _navn(_s(con, str(d.kursnr["vig"])))       # heller ikke via kurset
    assert sorted(_navn(_s(con, str(d.kursnr["vig"])))) == ["Kari Hansen", "Åse Ørsted"]


# ============================ spesialtegn er vanlig tekst ============================

def test_spesialtegn_er_vanlig_tekst_ikke_wildcards(con):
    kid = _kurs(con, "SPE-1", "Spesialtegn", ["2027-09-01"])
    _meld(con, kid, "Per", "Under_strek", "per_under@x.no")
    _meld(con, kid, "Pia", "Understrek", "pia.understrek@x.no")
    _meld(con, kid, "Uno", "Underxstrek", "uno.underxstrek@x.no")      # ville truffet et LIKE-søk på «under_strek»
    _meld(con, kid, "Pål", "50 Prosent", "paal.prosent@x.no")           # ville truffet et LIKE-søk på «50%»
    con.commit()
    assert _navn(_s(con, "under_strek")) == ["Per Under_strek"]     # «_» er ikke «hvilket som helst tegn»
    assert _navn(_s(con, "per_u")) == ["Per Under_strek"]
    assert _navn(_s(con, "50 prosent")) == ["Pål 50 Prosent"]
    for tekst in ("%%", "%", "_%", "\\", "\\\\", "'", "\"\"", "' OR '1'='1", "'; DROP TABLE deltaker; --", "a\\b", "50%"):
        assert _s(con, tekst).totalt == 0, tekst
    assert con.execute("SELECT COUNT(*) FROM deltaker").fetchone()[0] == 4         # ingenting er slettet


def test_soek_endrer_ikke_databasen(con):
    _bygg(con)
    con.commit()
    foer = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("hendelse", "deltaker", "paamelding",
                                                                                  "dokument", "utsending_logg")}
    for tekst in ("kari", "1001", "#3", "900 12 345", "eft-2027-1", "zzz"):
        _s(con, tekst)
    etter = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in foer}
    assert foer == etter


# ============================ påmeldinger, oppmøte og kursbevis ============================

def _rader(res, navn):
    p = next(p for p in res.personer if p["navn"] == navn)
    return {m["kode"]: m for m in p["paameldinger"]}


def test_alle_paameldinger_paa_tvers_av_kurs_nyeste_foerst(con):
    d = _bygg(con)
    p = _s(con, "kari hansen").personer[0]
    assert [m["kode"] for m in p["paameldinger"]] == ["SKJ-2027-3", "VIG-2027-2", "EFT-2027-1"]
    eft = p["paameldinger"][2]
    assert (eft["kurs_navn"], eft["kursnr"], eft["paamelding_id"], eft["kurs_id"]) == (
        "Emosjonsfokusert terapi", d.kursnr["eft"], d.hansen, d.eft)
    assert eft["datoer"] == "01.03.2027 – 02.03.2027" and p["paameldinger"][1]["datoer"] == "12.04.2027"
    assert (p["telefon"], p["arbeidssted"]) == ("900 12 345", "Nordlys legesenter")


def test_status_bruker_eksisterende_etiketter(con):
    d = _bygg(con)
    assert _rader(_s(con, "marie berg"), "Marie Berg")["EFT-2027-1"]["status_navn"] == "Avmeldt"
    db.avsla_paamelding(con, d.olsen, aktor="test")
    assert _rader(_s(con, "kari olsen"), "Kari Olsen")["EFT-2027-1"]["status_navn"] == "Avslått"
    db.sett_paamelding_status(con, d.nilsen, "forlatt", aktor="test")
    assert _rader(_s(con, "kari nilsen"), "Kari Nilsen")["EFT-2027-1"]["status_navn"] == "Forlatt"
    assert _rader(_s(con, "kari hansen"), "Kari Hansen")["EFT-2027-1"]["status_navn"] == db.PAAMELDINGSSTATUSER["paameldt"]


def test_oppmote_vises_naar_kurset_har_startet(con):
    _bygg(con)
    hansen = _rader(_s(con, "kari hansen"), "Kari Hansen")
    assert hansen["EFT-2027-1"]["oppmote"] == "2 av 2 kursdager"
    assert hansen["VIG-2027-2"]["oppmote"] == "0 av 1 kursdag"
    assert hansen["SKJ-2027-3"]["oppmote"] == "–"                    # starter først i september
    assert _rader(_s(con, "kari nilsen"), "Kari Nilsen")["EFT-2027-1"]["oppmote"] == "1 av 2 kursdager"
    assert _rader(_s(con, "marie berg"), "Marie Berg")["EFT-2027-1"]["oppmote"] == "2 av 2 kursdager"   # møtte før avmelding


def test_kursbevis_utstedt_med_dato(con):
    _bygg(con)
    m = _rader(_s(con, "kari hansen"), "Kari Hansen")["EFT-2027-1"]
    assert (m["kursbevis"], m["kursbevis_farge"]) == ("Utstedt 03.03.2027", "ok")


def test_kursbevis_forklaringer_for_hvert_tilfelle(con):
    _bygg(con)
    dagen_etter = date(2027, 3, 3)                                  # dagen etter siste kursdag på EFT: morgenjobben har ikke gått ennå
    hansen = _rader(_s(con, "kari hansen"), "Kari Hansen")
    assert hansen["VIG-2027-2"]["kursbevis"] == "Ikke utstedt: oppmøte 0 av 1 dag (kursbevis utstedes ved fullt oppmøte)"
    assert hansen["SKJ-2027-3"]["kursbevis"] == "Kurset er ikke avsluttet ennå"
    nilsen = _rader(_s(con, "kari nilsen"), "Kari Nilsen")["EFT-2027-1"]
    assert nilsen["kursbevis"] == "Ikke utstedt: oppmøte 1 av 2 dager (kursbevis utstedes ved fullt oppmøte)"
    assert nilsen["kursbevis_farge"] == "gul"
    olsen = _rader(_s(con, "kari olsen", idag=dagen_etter), "Kari Olsen")["EFT-2027-1"]
    assert olsen["kursbevis"] == "Ikke utstedt ennå: utstedes av morgenjobben" and olsen["kursbevis_farge"] == "gul"
    berg = _rader(_s(con, "marie berg"), "Marie Berg")["EFT-2027-1"]
    assert berg["kursbevis"] == "Ikke utstedt: påmeldingen er ikke bekreftet"


def test_kursbevis_forklaring_avlyst_og_uten_kursdager():
    f = deltakersok.kursbevis_forklaring
    assert f(utstedt=None, kurs_status="avlyst", paamelding_status="bekreftet", dager=1, mott=0) == (
        "Kurset er avlyst – kursbevis utstedes ikke", "gra")
    for status in ("utkast", "aapen", "full", "aktiv"):
        assert f(utstedt=None, kurs_status=status, paamelding_status="bekreftet", dager=2, mott=2) == (
            "Kurset er ikke avsluttet ennå", "gra")
    assert f(utstedt=None, kurs_status="avsluttet", paamelding_status="bekreftet", dager=0, mott=0)[0] == (
        "Ikke utstedt: kurset har ingen kursdager")
    # utstedt går foran alt annet
    assert f(utstedt="2027-03-03 07:00:01", kurs_status="avsluttet", paamelding_status="avmeldt", dager=2, mott=0) == (
        "Utstedt 03.03.2027", "ok")


def test_kursbevis_forklaring_folger_minste_andel(con, monkeypatch):
    _bygg(con)
    monkeypatch.setattr(kursbevis, "MIN_ANDEL", 0.5)
    nilsen = _rader(_s(con, "kari nilsen", idag=date(2027, 3, 3)), "Kari Nilsen")["EFT-2027-1"]          # 1 av 2 = 50 %
    assert nilsen["kursbevis"] == "Ikke utstedt ennå: utstedes av morgenjobben"
    hansen = _rader(_s(con, "kari hansen"), "Kari Hansen")["VIG-2027-2"]          # 0 av 1
    assert hansen["kursbevis"] == "Ikke utstedt: oppmøte 0 av 1 dag (kursbevis utstedes ved minst 50 % oppmøte)"


def test_forklaringen_stemmer_med_det_morgenjobben_faktisk_gjor(con, sendt):
    """Det søket lover («utstedes av morgenjobben» / «oppmøte 1 av 2») skal være det kursbevis.kjor faktisk gjør."""
    _bygg(con)
    dagen_etter = date(2027, 3, 3)
    for navn in ("kari olsen", "kari nilsen", "marie berg"):
        assert _rader(_s(con, navn, idag=dagen_etter), navn.title())["EFT-2027-1"]["kursbevis"].startswith("Ikke utstedt")
    kursbevis.kjor(Kjoring(con, idag=dagen_etter))
    con.commit()
    olsen = _rader(_s(con, "kari olsen", idag=dagen_etter), "Kari Olsen")["EFT-2027-1"]
    assert olsen["kursbevis"].startswith("Utstedt ") and olsen["kursbevis_farge"] == "ok"       # ble utstedt
    assert olsen["kursbevis_epost"].startswith("E-post sendt ") and olsen["kursbevis_dok"]      # og e-posten gikk
    assert _rader(_s(con, "kari nilsen", idag=dagen_etter), "Kari Nilsen")["EFT-2027-1"]["kursbevis"].startswith(
        "Ikke utstedt: oppmøte 1 av 2")
    assert _rader(_s(con, "marie berg", idag=dagen_etter), "Marie Berg")["EFT-2027-1"]["kursbevis"] == (
        "Ikke utstedt: påmeldingen er ikke bekreftet")
    assert _rader(_s(con, "kari hansen"), "Kari Hansen")["VIG-2027-2"]["kursbevis"].startswith("Ikke utstedt: oppmøte 0 av 1")
    assert len(sendt) >= 1                                          # varselet gikk (til demo-utboksen/stubben)


def test_ingen_allergier_eller_tilrettelegging_i_resultatet(con):
    kid = _kurs(con, "ALL-1", "Med allergi", ["2027-09-01"])
    _meld(con, kid, "Nina", "Nøtt", "nina.nott@x.no", sensitivt={"allergier": "Nøtteallergi-XYZ", "tilrettelegging": "Rullestol-XYZ"})
    con.commit()
    res = _s(con, "nina")
    assert _navn(res) == ["Nina Nøtt"]
    assert "XYZ" not in repr(res) and "XYZ" not in repr(deltakersok.liste_data(res))
    assert _s(con, "Nøtteallergi").totalt == 0 and _s(con, "Rullestol").totalt == 0      # kan heller ikke søkes opp


def test_modulen_bruker_ikke_logging_og_ikke_sensitivt_eller_lenker():
    tre = ast.parse(inspect.getsource(deltakersok))
    for node in ast.walk(tre):                                      # docstrings forklarer reglene og teller ikke
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body and isinstance(node.body[0], ast.Expr) \
                and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
            node.body = node.body[1:] or [ast.Pass()]
    kode = ast.unparse(tre)                                         # all kode og alle SQL-tekster
    assert "logging" not in kode and "db.logg" not in kode and "logger" not in kode
    assert "print" not in kode and "sys.std" not in kode and "warnings" not in kode           # heller ikke til stdout/stderr (Azure-loggen)
    assert "sensitivt" not in kode and "allergi" not in kode.lower() and "innlogging_token" not in kode
    assert "innerHTML" not in kode
    tabeller = set(re.findall(r"(?:FROM|JOIN)\s+([a-z_]+)", kode))
    assert tabeller == {"deltaker", "kurs", "paamelding", "kursdag", "oppmote", "dokument",
                        "utsending_logg"}               # de eneste som leses (utsending_logg: bare status og dato for kursbevis-e-posten)


# ============================ ruter ============================

def _innlogget(con, rolle=None):
    k = webapp.app.test_client()
    if rolle:
        db.opprett_admin_bruker(con, rolle, rolle.title(), "passord-som-holder", rolle=rolle)
        con.commit()
        r = k.post("/admin/logg-inn", data={"brukernavn": rolle, "passord": "passord-som-holder"})
    else:
        r = k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    assert r.status_code == 302
    return k


def _html(k, url):
    r = k.get(url)
    assert r.status_code == 200, url
    return r.get_data(as_text=True)


def _json(k, q, **kw):
    """Rullegardinens kall: POST med teksten i kroppen og CSRF-tokenet i hodet - aldri i adressen."""
    with k.session_transaction() as s:
        token = s.get("csrf") or s.setdefault("csrf", "test-csrf-token")
    return k.post("/admin/sok.json", json={"q": q}, headers={"X-CSRF-Token": token}, **kw)


def _synlig(html: str) -> str:
    """Bare teksten på siden: uten tagger (også <mark> rundt det søket traff) og med escape tilbake."""
    import html as h
    return h.unescape(re.sub(r"<[^>]+>", "", html))


def test_krever_innlogging_siden_og_json(con):
    _bygg(con)
    k = webapp.app.test_client()
    r = k.get("/admin/sok?q=kari")
    assert r.status_code == 302 and "logg-inn" in r.headers["Location"] and b"Hansen" not in r.data
    assert "kari" not in r.headers["Location"]                     # søketeksten følger ikke med til innloggingssiden
    r = k.post("/admin/sok.json", json={"q": "kari"})                # uten CSRF-token: avvist før noe annet
    assert r.status_code == 400 and b"Hansen" not in r.data
    with k.session_transaction() as s:
        s["csrf"] = "t"
    r = k.post("/admin/sok.json", json={"q": "kari"}, headers={"X-CSRF-Token": "t"})     # ikke innlogget
    assert r.status_code == 401 and r.get_json() == {"feil": "ikke_innlogget"}
    assert b"Hansen" not in r.data and r.headers["Cache-Control"] == "no-store"


def test_alle_roller_kan_soeke_ogsaa_lesetilgang(con):
    _bygg(con)
    for rolle in (None, "kursadmin", "lese"):
        k = _innlogget(con, rolle)
        assert "Kari Hansen" in _synlig(_html(k, "/admin/sok?q=hansen")), rolle
        r = _json(k, "hansen")
        assert r.status_code == 200 and r.get_json()["totalt"] == 1, rolle


def test_utlogget_okt_gir_401_paa_json(con):
    _bygg(con)
    k = _innlogget(con)
    with k.session_transaction() as s:
        s["admin_sist"] = 0                                          # inaktiv for lenge: økten er utløpt
    assert _json(k, "kari").status_code == 401


def test_svar_er_aldri_mellomlagret(con):
    _bygg(con)
    k = _innlogget(con)
    for url in ("/admin/sok", "/admin/sok?q=kari", "/admin/sok?q=x"):
        assert k.get(url).headers["Cache-Control"] == "no-store", url
    for q in ("kari", "x", ""):
        assert _json(k, q).headers["Cache-Control"] == "no-store", q


def test_json_har_forventet_form(con):
    d = _bygg(con)
    k = _innlogget(con)
    r = _json(k, "kari")
    assert r.mimetype == "application/json"
    data = r.get_json()
    assert set(data) == {"totalt", "for_kort", "treff", "alle_url"} and data["totalt"] == 3 and not data["for_kort"]
    assert data["alle_url"] == "/admin/sok?q=kari"
    t = data["treff"][0]
    assert set(t) == {"id", "navn_deler", "epost_deler", "grunn", "antall", "siste_kurs", "url"}
    assert t["id"] == d.hansen_id and t["antall"] == 3 and t["siste_kurs"] == "SKJ-2027-3" and t["grunn"] == "Treff på navn"
    assert t["url"] == f"/admin/sok?q=kari#person-{d.hansen_id}"
    assert "".join(del_[0] for del_ in t["navn_deler"]) == "Kari Hansen" and t["navn_deler"][0] == ["Kari", True]
    assert "".join(del_[0] for del_ in t["epost_deler"]) == "kari.hansen@eksempel.no"


def test_json_er_bare_post_og_soeketeksten_staar_aldri_i_adressen(con):
    """Rullegardinen skal ikke sende søketeksten (navn, e-post, telefon) i adressen: den havner i URL-logger for hvert tastetrykk."""
    _bygg(con)
    k = _innlogget(con)
    r = k.get("/admin/sok.json?q=kari")
    assert r.status_code == 405 and b"Hansen" not in r.data                        # GET med ?q= finnes ikke lenger
    for metode in (k.put, k.delete):
        r = metode("/admin/sok.json?q=kari")
        assert r.status_code in (400, 405) and b"Hansen" not in r.data
    with k.session_transaction() as s:
        token = s["csrf"] = "t"
    r = k.post("/admin/sok.json?q=kari", json={"q": "nilsen"}, headers={"X-CSRF-Token": token})
    assert [t["id"] for t in r.get_json()["treff"]] == [_s(con, "nilsen").personer[0]["id"]]      # bare kroppen gjelder
    assert k.post("/admin/sok.json?q=kari", headers={"X-CSRF-Token": token}).get_json()["treff"] == []
    for kropp in ('{"q": 5}', '{"q": ["kari"]}', "[1]", "ikke json", ""):
        r = k.post("/admin/sok.json", data=kropp, content_type="application/json", headers={"X-CSRF-Token": token})
        assert r.status_code == 200 and r.get_json()["treff"] == [], kropp
    r = k.post("/admin/sok.json", data="q=kari", content_type="application/x-www-form-urlencoded",
               headers={"X-CSRF-Token": token})
    assert r.get_json()["treff"] == []                                              # skjemadata er ikke et gyldig kall


def test_json_krever_csrf_token(con):
    _bygg(con)
    k = _innlogget(con)
    with k.session_transaction() as s:
        s["csrf"] = "riktig"
    for hode in ({}, {"X-CSRF-Token": "feil"}):
        r = k.post("/admin/sok.json", json={"q": "kari"}, headers=hode)
        assert r.status_code == 400 and b"Hansen" not in r.data
    assert k.post("/admin/sok.json", json={"q": "kari"}, headers={"X-CSRF-Token": "riktig"}).status_code == 200


def test_json_url_koder_soeket(con):
    _bygg(con)
    k = _innlogget(con)
    data = _json(k, "kari hansen&x=1#å").get_json()
    assert data["totalt"] == 0
    data = _json(k, "kari hansen").get_json()
    assert data["alle_url"] == "/admin/sok?q=kari+hansen" and data["treff"][0]["url"].startswith("/admin/sok?q=kari+hansen#person-")


def test_json_maks_atte_treff_og_kort_soek_gir_ingen(con):
    kid = _kurs(con, "JSN-1", "Mange", ["2027-09-01"])
    for i in range(12):
        _meld(con, kid, "Kari", f"Rad{i:02d}", f"kari.rad{i:02d}@x.no")
    con.commit()
    k = _innlogget(con)
    data = _json(k, "kari").get_json()
    assert len(data["treff"]) == 8 and data["totalt"] == 12
    for q in ("k", "", " ", "k h"):
        data = _json(k, q).get_json()
        assert data["treff"] == [] and data["totalt"] == 0
    assert _json(k, "k h").get_json()["for_kort"] is True
    with k.session_transaction() as s:
        token = s["csrf"]
    assert k.post("/admin/sok.json", json={}, headers={"X-CSRF-Token": token}).get_json()["treff"] == []       # uten q


def test_json_takbegrensning_gir_429_uten_aa_hindre_vanlig_bruk(con, monkeypatch):
    _bygg(con)
    k = _innlogget(con)
    for i in range(80):                                              # en lang, rask tasteøkt: ingen 429
        assert _json(k, f"kari{i % 3}").status_code == 200
    sikkerhet.takbegrenser.nullstill()
    monkeypatch.setitem(sikkerhet.GRENSER, "admin_sok", (3, 60))
    assert [_json(k, "kari").status_code for _ in range(3)] == [200, 200, 200]
    r = _json(k, "kari")
    assert r.status_code == 429 and r.get_json() == {"feil": "for_mange_sok"} and r.headers["Cache-Control"] == "no-store"
    r = k.get("/admin/sok?q=kari")                                   # resultatsiden deler telleren: ingen omvei uten grense
    assert r.status_code == 429 and b"Hansen" not in r.data
    assert k.get("/admin/sok").status_code == 200                    # hjelpesiden og for korte søk koster ingenting
    assert k.get("/admin/sok?q=k").status_code == 200


def test_standardgrensen_tillater_vanlig_bruk():
    maks, sekunder = sikkerhet.GRENSER["admin_sok"]
    assert maks / sekunder >= 1                                      # minst ett søk i sekundet i snitt


def test_soekesiden_uten_soek_viser_hjelp(con):
    _bygg(con)
    html = _html(_innlogget(con), "/admin/sok")
    assert "Hva kan du søke på?" in html
    for ord_ in ("Navn", "E-post", "Telefon", "HPR-nummer", "Påmeldingsnummer", "Kursnummer eller kurskode"):
        assert ord_ in html
    assert "Hansen" not in html and "persontreff" not in html.split("</style>")[1]


def test_soekesiden_for_kort_og_ingen_treff(con):
    _bygg(con)
    k = _innlogget(con)
    assert "Skriv minst ett ord med 2 tegn (eller et tall)" in _html(k, "/admin/sok?q=k")
    assert "Skriv minst ett ord med 2 tegn" in _html(k, "/admin/sok?q=k+h")       # bare enkeltbokstaver er heller ikke et søk
    html = _html(k, "/admin/sok?q=xyzzy")
    assert "Ingen treff på «xyzzy»" in html and "Sjekk stavemåten" in html and "Deltakerregister" in html


def test_soekesiden_veldig_langt_og_spesialtegn(con):
    _bygg(con)
    k = _innlogget(con)
    html = _html(k, "/admin/sok?q=" + "x" * 5000)
    assert "Søket er avkortet" in html and "x" * 101 not in html
    for q in ("%", "%%", "_", "\\", "'", '"', "<b>", "%00", "a%00b", "' OR 1=1 --"):
        r = k.get("/admin/sok", query_string={"q": q})
        assert r.status_code == 200, q
    assert k.get("/admin/sok?q=%00%00").status_code == 200
    assert k.get("/admin/sok?q=kari&q=hansen").status_code == 200    # flere q: første brukes


def test_soekesiden_viser_personen_med_alle_paameldinger_og_lenker(con):
    d = _bygg(con)
    html = _html(_innlogget(con), "/admin/sok?q=kari hansen")
    assert "1 person funnet." in html and f'id="person-{d.hansen_id}"' in html
    tekst = _synlig(html)
    assert "kari.hansen@eksempel.no" in tekst and "900 12 345" in tekst and "Nordlys legesenter" in tekst
    for kid, pid in ((d.eft, d.hansen), (d.vig, d.hansen_vig), (d.skj, d.hansen_skj)):
        assert f'href="/admin/kurs/{kid}/deltaker/{pid}"' in html                     # deltakervinduet
    assert f'href="/admin/rapporter/deltaker/{d.hansen_id}"' in html                  # personsiden
    assert "Utstedt 03.03.2027" in html and "Kurset er ikke avsluttet ennå" in html
    assert "Ikke utstedt: oppmøte 0 av 1 dag (kursbevis utstedes ved fullt oppmøte)" in html
    assert "2 av 2 kursdager" in html and "Treff på navn" in html
    assert "SKJ-2027-3 · kursnr. " in html and f"påmelding #{d.hansen_skj}" in html


def test_soekesiden_grense_og_forklaring(con):
    kid = _kurs(con, "GRS-2", "Mange", ["2027-09-01"])
    for i in range(55):
        _meld(con, kid, "Kari", f"Test{i:02d}", f"kari.test{i:02d}@x.no")
    con.commit()
    html = _html(_innlogget(con), "/admin/sok?q=kari")
    assert "Viser 50 av 55 personer – skriv mer for å snevre inn." in html
    assert html.count('class="kort persontreff"') == 50


def test_soekesiden_kurs_gir_snarvei_til_deltakerlisten(con):
    d = _bygg(con)
    html = _html(_innlogget(con), f"/admin/sok?q={d.kursnr['eft']}")
    assert f'href="/admin/kurs/{d.eft}/deltakere"' in html and "Åpne deltakerlisten for kurset" in html
    assert "Treff på kurs EFT-2027-1" in html and 'class="merke">Treff</span>' in html


def test_navn_med_html_vises_som_tekst_i_side_og_json(con):
    kid = _kurs(con, "XSS-1", "Xss", ["2027-09-01"])
    _meld(con, kid, "<script>alert(1)</script>", "Xss & \"Co\"", "xss.person@x.no", arbeidssted="<img src=x onerror=alert(2)>")
    con.commit()
    k = _innlogget(con)
    html = _html(k, "/admin/sok?q=xss")
    assert "<script>alert(1)" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img src=x" not in html and "&lt;img src=x onerror=alert(2)&gt;" in html
    r = _json(k, "xss")
    assert r.mimetype == "application/json" and r.headers["X-Content-Type-Options"] == "nosniff"
    navn = "".join(del_[0] for del_ in r.get_json()["treff"][0]["navn_deler"])
    assert navn == "<script>alert(1)</script> Xss & \"Co\""          # rå tekst i JSON - JavaScript bruker textContent
    # søketeksten selv (som kommer fra adressen) skrives også escapet tilbake
    assert "&lt;script&gt;" in _html(k, "/admin/sok?q=<script>alert(3)</script>")
    assert "<script>alert(3)" not in _html(k, "/admin/sok?q=<script>alert(3)</script>")


def test_soeketeksten_lagres_ikke_i_hendelseslogg_eller_applikasjonslogg(con, caplog):
    _bygg(con)
    k = _innlogget(con)
    con.commit()
    caplog.set_level(logging.DEBUG)
    foer = con.execute("SELECT COUNT(*) FROM hendelse").fetchone()[0]
    hemmelig = "kari.hansen@eksempel.no"
    for url in (f"/admin/sok?q={hemmelig}", "/admin/sok?q=Hansenspesial", "/admin/sok?q=" + "x" * 300):
        k.get(url)
    for q in (hemmelig, "Hansenspesial", "x" * 300):
        _json(k, q)
    assert con.execute("SELECT COUNT(*) FROM hendelse").fetchone()[0] == foer            # ingen hendelse i det hele tatt
    alle = " ".join(f"{r.getMessage()} {r.args}" for r in caplog.records)
    assert "Hansen" not in alle and hemmelig not in alle and "eksempel.no" not in alle
    for navn in ("admin_sok", "admin_sok_json"):
        kilde = inspect.getsource(getattr(webapp, navn))
        assert "logg" not in kilde and "logger" not in kilde


def test_ingen_lenker_eller_sensitive_data_paa_soekesiden(con):
    kid = _kurs(con, "SEN-1", "Sensitiv", ["2027-09-01"])
    _meld(con, kid, "Nina", "Nøtt", "nina.nott@x.no", sensitivt={"allergier": "Nøtteallergi-XYZ", "tilrettelegging": "Rullestol-XYZ"})
    con.commit()
    k = _innlogget(con)
    html = _html(k, "/admin/sok?q=nina")
    assert "Nina Nøtt" in _synlig(html) and "XYZ" not in html and "/logg-inn/" not in html and "allergi" not in html.lower().split("</style>")[1]
    assert "XYZ" not in _json(k, "nina").get_data(as_text=True)


def test_feil_i_soeket_lekker_ikke_soeketeksten(con, monkeypatch):
    _bygg(con)
    k = _innlogget(con)
    monkeypatch.setitem(webapp.app.config, "PROPAGATE_EXCEPTIONS", False)     # vis den vanlige 500-siden, ikke unntaket

    def boom(*a, **kw):
        raise RuntimeError("feil med kari.hansen@eksempel.no")
    monkeypatch.setattr(deltakersok, "sok", boom)
    r = k.get("/admin/sok?q=kari.hansen@eksempel.no")
    assert r.status_code == 500 and "kari.hansen" not in r.get_data(as_text=True)


# ============================ søkefeltet: på Oversikten og søkesiden, ikke i menyen ============================

ADMINSIDER = ("/admin", "/admin/aktiviteter/kalender", "/admin/rapporter", "/admin/rapporter/deltakere", "/admin/e-postmaler",
              "/admin/kunnskap", "/admin/sok", "/admin/sok?q=kari")


def _alle_adminsider(d):
    return (*ADMINSIDER, f"/admin/kurs/{d.eft}/deltakere", f"/admin/kurs/{d.eft}/oppsett",
            f"/admin/kurs/{d.eft}/deltaker/{d.hansen}", f"/admin/rapporter/deltaker/{d.hansen_id}")


def test_toppmenyen_har_ikke_soekefelt_men_slash_tar_deg_til_oversikten(con):
    d = _bygg(con)
    k = _innlogget(con)
    for url in _alle_adminsider(d):
        header = _html(k, url).split("</header>")[0]
        assert "data-deltakersok" not in header and 'name="q"' not in header, url      # to søkefelt i samme bilde er borte
        assert 'data-sok-adresse="/admin#sok-deltaker"' in header, url                 # «/» hopper til søket på Oversikten


def test_oversikten_og_soekesiden_har_ett_deltakersoek_og_ingen_andre_sider_har_noe(con):
    d = _bygg(con)
    k = _innlogget(con)
    for url in _alle_adminsider(d):
        html = _html(k, url)
        assert html.count("data-deltakersok") == (1 if url.split("?")[0] in ("/admin", "/admin/sok") else 0), url
    side = _html(k, "/admin/sok").split("</header>")[1]
    assert 'role="search"' in side and 'action="/admin/sok"' in side and 'method="get"' in side
    assert 'data-json="/admin/sok.json"' in side
    assert re.search(r'data-csrf="[A-Za-z0-9_-]{20,}"', side)                           # tokenet rullegardinen sender med
    assert 'id="sok-side" name="q" type="search"' in side and 'role="combobox"' in side
    assert 'aria-controls="sok-side-liste"' in side and 'id="sok-side-liste" role="listbox"' in side
    assert 'role="status" aria-live="polite" data-sok-status' in side
    assert '<button type="submit" class="sok-knapp">Søk</button>' in side


def test_soekefeltet_paa_soekesiden_faar_soeketeksten(con):
    _bygg(con)
    k = _innlogget(con)
    assert 'value="kari hansen"' in _html(k, "/admin/sok?q=kari hansen").split("</header>")[1]
    assert 'value=""' in _html(k, "/admin").split("</header>")[1].split('id="sok-deltaker"')[1].split(">")[0]
    innhold = _html(k, "/admin/sok?q=" + '"><b>').split("</header>")[1]
    assert "<b>" not in innhold and "&#34;&gt;&lt;b&gt;" in innhold              # escapet i attributtet


def test_soekefeltet_finnes_ikke_paa_offentlige_sider(con):
    d = _bygg(con)
    anonym = webapp.app.test_client()
    for url in ("/", "/kurs/SKJ-2027-3", "/logg-inn", "/sporsmal", "/admin/logg-inn", "/innsjekk"):
        html = anonym.get(url).get_data(as_text=True)
        assert "data-deltakersok" not in html and 'name="q"' not in html and "sok-liste" not in html.split("</style>")[1], url
    k = _innlogget(con)                                              # ...heller ikke som innlogget admin på offentlige sider
    for url in ("/", "/kurs/SKJ-2027-3", "/sporsmal", f"/admin/kurs/{d.skj}/forhandsvis-paamelding"):
        html = k.get(url).get_data(as_text=True)
        assert "data-deltakersok" not in html, url


def test_oversikten_har_deltakersoek_og_kurssoek_side_ved_side_og_menyen_er_delt_i_to(con):
    _bygg(con)
    html = _html(_innlogget(con), "/admin")
    hovedinnhold = html.split('<main id="innhold">')[1]
    panel = hovedinnhold.split('<div class="sokepanel">')[1].split('<h2 id="kurs">')[0]
    assert 'id="sok-deltaker"' in panel and 'id="sok-kurs"' in panel
    assert panel.index('id="sok-deltaker"') < panel.index('id="sok-kurs"')                  # side ved side, deltakere først
    assert 'class="sokefelt stor"' not in hovedinnhold                                     # ikke lenger ett felt over hele bredden
    assert html.count("data-deltakersok") == 1                                              # bare ett deltakersøk i bildet
    hode = html.split("</header>")[0]
    hovedmeny = hode.split("<nav>")[1].split("</nav>")[0]
    for tekst in ("Oversikt", "Kalender", "Rapporter", "E-postmaler", "Daglig kjøring", "Brukere", "Utboks"):
        assert f">{tekst}</a>" in hovedmeny, tekst
    assert "Aktiviteter" not in hovedmeny                           # slått sammen med Oversikten
    assert "Kunnskapsbase" not in hovedmeny                         # av sammen med «Spør oss» (ASSISTENT_AKTIV=0)
    assert "Logg ut" not in hovedmeny and "Offentlig side" not in hovedmeny
    bruker = hode.split('<nav class="bruker" aria-label="Bruker">')[1].split("</nav>")[0]
    assert "Offentlig side" not in bruker and 'href="/admin/logg-ut">Logg ut' in bruker


def test_menyen_viser_riktig_valg_per_rolle(con):
    _bygg(con)
    kursadmin = _html(_innlogget(con, "kursadmin"), "/admin").split("</header>")[0]
    assert "Daglig kjøring" not in kursadmin and "Brukere" not in kursadmin and ">Rapporter</a>" in kursadmin
    lese = _html(_innlogget(con, "lese"), "/admin")
    assert "Logg ut (Lese, lesetilgang)" in lese.split("</header>")[0] and 'data-deltakersok' in lese     # lesetilgang kan søke


def test_aktiv_menyside_er_fortsatt_markert(con):
    _bygg(con)
    k = _innlogget(con)
    assert 'aria-current="page">Rapporter</a>' in _html(k, "/admin/rapporter")
    assert 'aria-current="page">Oversikt</a>' in _html(k, "/admin")
    meny = _html(k, "/admin/sok").split("</header>")[0].split("<nav>")[1].split("</nav>")[0]
    assert 'aria-current="page"' not in meny                        # søkesiden er ikke et menyvalg


def test_lenker_til_soeket_fra_rapporter_og_deltakerregisteret(con):
    _bygg(con)
    k = _innlogget(con)
    assert 'href="/admin/sok">Søk deltaker →</a>' in _html(k, "/admin/rapporter")
    register = _html(k, "/admin/rapporter/deltakere")
    assert 'href="/admin/sok">Søk deltaker</a>' in register
    assert "Kari Hansen" in register and 'name="kun_flere_kurs"' in register       # selve registeret er uendret


def test_javascript_er_dokumentert_og_bruker_ikke_innerhtml_for_treffene():
    with open(webapp.app.static_folder + "/app.js", encoding="utf-8") as f:
        js = f.read()
    assert "data-deltakersok" in js and "data-json" in js
    start = js.index("Globalt deltakersøk (søkefeltet")
    slutt = js.index("Deltakervinduet: et klikk")
    bit = "\n".join(l for l in js[start:slutt].splitlines() if not l.strip().startswith("//"))     # uten kommentarene
    assert "innerHTML" not in bit and "insertAdjacentHTML" not in bit and "document.write" not in bit
    assert "textContent" in bit and "createTextNode" in bit and 'indexOf("/admin/") !== 0' in bit
    assert 'e.key !== "/"' in bit and "ArrowDown" in bit and "Escape" in bit and "Enter" in bit
    assert "data-sok-adresse" in bit and "#sok-deltaker" in bit                    # «/» fra andre sider går til Oversikten
