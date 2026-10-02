"""Deltakersøket: regler, grensetilfeller og vern som ikke dekkes av tests/test_deltakersok.py.

Bakgrunn: tre kritikere gjennomgikk søket, og en mutasjonsprøve (én liten endring i koden om gangen) viste at mange
endringer ikke ble oppdaget av noen test. Denne filen låser det som skiller riktig fra feil: hva som regnes som
kursbevis, rangering, kurs-oppslaget, escaping, hvilke felt som kan vises, at søketeksten aldri havner i logger eller
adresser, og tolking av tekst limt inn fra e-postprogram, med og uten æøå. Alle personer og data er oppdiktede.
"""
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from kurs import config, db, deltakersok, kursbevis
from kurs.kjoring import Kjoring
from kurs.web import app as webapp
from kurs.web import sikkerhet
from test_deltakersok import (IDAG, _bevis, _bygg, _html, _innlogget, _json, _kurs, _meld, _navn, _rader, _s,  # noqa: F401
                              _synlig, con, sendt)


# ============================ tolking ============================

def test_kanttegn_rundt_ord_fjernes_ogsaa_anforselstegn_og_guillemets():
    for tekst in ('"Kari Hansen"', "«Kari» «Hansen»", "'kari' hansen:", "„Kari“ ”hansen”"):
        assert [t.tekst for t in deltakersok.tolk(tekst).termer] == ["kari", "hansen"], tekst


@pytest.mark.parametrize("ord_,forventet", [
    ("<kari@x.no>", "kari@x.no"), ("(kari@x.no)", "kari@x.no"), ("[kari@x.no]", "kari@x.no"),
    ("mailto:kari@x.no", "kari@x.no"), ("MAILTO:kari@x.no", "kari@x.no"), ("<mailto:kari@x.no>", "kari@x.no"),
    ("Hansen,", "Hansen"), ("kari@x.no.", "kari@x.no"), ("«Kari»", "Kari"), ("...", ""), ("<>", ""),
    ("@eksempel.no", "@eksempel.no"), (".no", ".no"), ("kari.hansen@x.no", "kari.hansen@x.no")])
def test_rens_ord_fjerner_tegnsetting_og_parenteser_rundt_ordet(ord_, forventet):
    assert deltakersok._rens_ord(ord_) == forventet


@pytest.mark.parametrize("tekst", [
    "Kari Hansen <kari.hansen@eksempel.no>", "Hansen, Kari <kari.hansen@eksempel.no>",
    "Kari Hansen (kari.hansen@eksempel.no)", "[kari.hansen@eksempel.no]", "mailto:kari.hansen@eksempel.no",
    "<kari.hansen@eksempel.no>;", '"Kari Hansen" <kari.hansen@eksempel.no>', "Kari Hansen <kari.hansen@eksempel.no>,",
    "kari.hansen@eksempel.no.", "MAILTO:Kari.Hansen@Eksempel.no", "kari.hansen@eksempel.no;"])
def test_innliming_fra_epostprogram_finner_personen(con, tekst):
    """«Kari sier hun mangler kursbevis» og har skrevet til kurs@ipr.no: avsenderlinjen limes rett inn fra Outlook."""
    _bygg(con)
    assert _navn(_s(con, tekst)) == ["Kari Hansen"], tekst


def test_innlimt_avsenderlinje_treffer_bare_naar_alle_ord_hoerer_til_samme_person(con):
    _bygg(con)
    assert _s(con, "Kari Nilsen <kari.hansen@eksempel.no>").totalt == 0
    k = _innlogget(con)
    tekst = "Kari Hansen <kari.hansen@eksempel.no>"
    assert _json(k, tekst).get_json()["totalt"] == 1                                      # rullegardinen
    html = k.get("/admin/sok", query_string={"q": tekst}).get_data(as_text=True)
    assert "1 person funnet." in html and "Kari Hansen" in _synlig(html)                   # resultatsiden
    assert 'value="Kari Hansen &lt;kari.hansen@eksempel.no&gt;"' in html                   # teksten står escapet i feltet


def test_enkeltbokstaver_som_egne_ord_ignoreres():
    for tekst in ("k h", "a b", "x", "å ø"):
        s = deltakersok.tolk(tekst)
        assert s.termer == () and s.for_kort and not s.gyldig, tekst
    assert [t.tekst for t in deltakersok.tolk("k hansen").termer] == ["hansen"]
    assert [t.tekst for t in deltakersok.tolk("kari 9").termer] == ["kari", "9"]           # tall er tall, også med ett siffer
    assert [t.tekst for t in deltakersok.tolk("#9 kari").termer] == ["#9", "kari"]
    assert deltakersok.MIN_ORD == 2


def test_soek_med_bare_enkeltbokstaver_gir_ingen_tilfeldige_treff(con):
    _bygg(con)
    res = _s(con, "k h")
    assert res.totalt == 0 and res.sok.for_kort and not res.sok.gyldig
    assert _navn(_s(con, "k hansen")) == ["Kari Hansen"]
    assert _json(_innlogget(con), "k h").get_json() == {"totalt": 0, "for_kort": True, "treff": [], "alle_url": "/admin/sok?q=k+h"}


def test_grensene_er_som_avtalt():
    """Verdiene står i hjelpeteksten, i dokumentasjonen og i de andre testene: endres de, skal det gjøres bevisst."""
    d = deltakersok
    assert (d.MIN_TEGN, d.MAKS_TEGN, d.MAKS_ORD, d.MIN_ORD) == (2, 100, 6, 2)
    assert (d.GRENSE_SIDE, d.GRENSE_LISTE, d.MIN_SIFFER, d.MIN_KURSKODE, d.MAKS_NR_SIFRE, d.MAKS_KURSTREFF) == (50, 8, 4, 4, 9, 3)


def test_nummergrenser_er_trygge_for_databasens_heltall():
    assert 10 ** deltakersok.MAKS_NR_SIFRE - 1 <= 2 ** 31 - 1           # PostgreSQL INTEGER (kurs.kursnr, paamelding.id)
    assert deltakersok.tolk("9" * (deltakersok.MAKS_NR_SIFRE + 1)).termer[0].pnr is None
    assert deltakersok.tolk("9" * deltakersok.MAKS_NR_SIFRE).termer[0].pnr == 10 ** deltakersok.MAKS_NR_SIFRE - 1


def test_sammenslaing_av_telefongrupper_grenser():
    t = deltakersok.tolk
    assert [x.siffer for x in t("12 34").termer] == ["1234"]             # akkurat fire sifre: ett nummer
    assert [x.tekst for x in t("1 23").termer] == ["1", "23"]            # tre sifre: for kort til å være telefon
    assert [x.siffer for x in t("900 123 456").termer] == ["900123456"]  # bare tresifrede ledd slås sammen
    assert len(t("+47 900 12 345").termer) == 1
    assert [x.tekst for x in t("kari - 411 22 333").termer] == ["kari", "411 22 333"]   # tegnsetting uten sifre er ikke del av nummeret
    assert deltakersok._er_nummerbit("22") and deltakersok._er_nummerbit("+47") and not deltakersok._er_nummerbit("-")


def test_telefon_med_punktum_er_ett_nummer(con):
    _bygg(con)
    assert [t.siffer for t in deltakersok.tolk("900.12.345").termer] == ["90012345"]
    assert _navn(_s(con, "900.12.345")) == ["Kari Hansen"]


@pytest.mark.parametrize("lagret,forventet", [
    ("47123456", "47123456"),               # gyldig norsk nummer på 8 sifre som tilfeldigvis starter på 47
    ("4747123456", "47123456"),             # med landskode uten pluss
    ("471234567", "471234567"), ("471234567890", "471234567890")])
def test_normaliser_telefon_landskode_bare_paa_ti_sifre(lagret, forventet):
    assert deltakersok.normaliser_telefon(lagret) == forventet


def test_ord_med_bokstaver_og_sifre_tolkes_ikke_som_telefon(con):
    _bygg(con)
    assert deltakersok.tolk("kari2345").termer[0].siffer == "" and deltakersok.tolk("eft-2027-1").termer[0].siffer == ""
    assert _s(con, "kari2345").totalt == 0 and _s(con, "abc12345").totalt == 0      # Hansen har 2345 i telefonnummeret


def test_casefold_gir_ss_for_eszett(con):
    kid = _kurs(con, "ESZ-1", "Eszett", ["2027-09-01"])
    _meld(con, kid, "Anja", "Strauß", "anja.s@x.no")
    con.commit()
    for tekst in ("strauss", "STRAUSS", "strauß"):
        assert _navn(_s(con, tekst)) == ["Anja Strauß"], tekst


# ============================ æøå og aksenter ============================

def _diakritisk(con):
    kid = _kurs(con, "DIA-1", "Diakritiske tegn", ["2027-09-01"])
    _meld(con, kid, "Sølvi", "Tørresen", "st1970@example.no")             # e-posten inneholder ikke navnet
    _meld(con, kid, "Hakon", "Braten", "hb@eksempel.no")                 # registrert uten æøå (skjemaet tar imot begge deler)
    _meld(con, kid, "Zoë", "Müller", "zm@eksempel.no")
    con.commit()
    return kid


@pytest.mark.parametrize("tekst", ["solvi", "SOLVI", "sølvi", "Sølvi", "torresen", "tørresen", "TØRRESEN", "solvi torresen",
                                   "sølvi tørresen", "solvi tørresen", "tor", "olv"])
def test_soek_uten_aeoa_finner_navn_med_aeoa(con, tekst):
    _diakritisk(con)
    assert _navn(_s(con, tekst)) == ["Sølvi Tørresen"], tekst


@pytest.mark.parametrize("tekst", ["håkon bråten", "HÅKON BRÅTEN", "hakon braten", "håkon", "bråten", "hakon bråten"])
def test_soek_med_aeoa_finner_navn_registrert_uten(con, tekst):
    _diakritisk(con)
    assert _navn(_s(con, tekst)) == ["Hakon Braten"], tekst


@pytest.mark.parametrize("tekst,navn", [("zoe", "Zoë Müller"), ("zoë", "Zoë Müller"), ("muller", "Zoë Müller"),
                                        ("müller", "Zoë Müller"), ("MULLER", "Zoë Müller")])
def test_aksenter_og_omlyd_spiller_ingen_rolle(con, tekst, navn):
    _diakritisk(con)
    assert _navn(_s(con, tekst)) == [navn]


def test_soek_uten_aeoa_finner_ogsaa_epost_med_aeoa(con):
    kid = _kurs(con, "DIA-2", "Epost", ["2027-09-01"])
    _meld(con, kid, "Test", "Person", "sørensen.test@x.no")
    con.commit()
    for tekst in ("sorensen.test", "sørensen.test", "sorensen"):
        res = _s(con, tekst)
        assert _navn(res) == ["Test Person"] and res.personer[0]["grunner"] == ["e-post"], tekst


def test_grunn_og_markering_ved_soek_uten_aeoa(con):
    _diakritisk(con)
    p = _s(con, "solvi").personer[0]
    assert p["grunner"] == ["navn"] and p["navn_deler"] == [("Sølvi", True), (" Tørresen", False)]
    assert _s(con, "torresen").personer[0]["navn_deler"] == [("Sølvi ", False), ("Tørresen", True)]
    assert _s(con, "håkon").personer[0]["navn_deler"] == [("Hakon", True), (" Braten", False)]
    assert _s(con, "sølvi").personer[0]["navn_deler"] == [("Sølvi", True), (" Tørresen", False)]


def test_navn_som_starter_uten_aeoa_rangeres_foran(con):
    kid = _kurs(con, "DIA-3", "Rangering", ["2027-09-01"])
    _meld(con, kid, "Anna", "Osolvia", "anna.o@x.no")                   # «solvi» står inne i etternavnet (alfabetisk først)
    _meld(con, kid, "Sølvi", "Zeta", "solvi.z@x.no")                   # navnet starter med «solvi» hvis æøå ses bort fra
    con.commit()
    assert _navn(_s(con, "solvi")) == ["Sølvi Zeta", "Anna Osolvia"]


def test_ascii_nokkelen():
    assert deltakersok._ascii("Sølvi Tørresen Ærlig Åse Ünal Straße Łódź") == "solvi torresen aerlig ase unal strasse lodz"
    assert deltakersok._ascii(None) == "" and deltakersok._ascii("Kari Hansen") == "kari hansen"
    assert deltakersok._ascii("Åse") == "ase" and deltakersok._ascii("Åse") == "ase"   # også Å som A + ring over (Mac)


# ============================ markering ============================

def test_markering_flere_ord_overlapp_og_unicode():
    f = deltakersok._markeringer
    assert f("Kari Hansen", ["kari", "hansen"]) == [("Kari", True), (" ", False), ("Hansen", True)]
    assert f("Kari Hansen", ["hansen", "ans"]) == [("Kari ", False), ("Hansen", True)]      # innenfor et annet treff
    assert f("Kari Hansen", ["kar", "i"]) == [("Kari", True), (" Hansen", False)]           # tilstøtende slås sammen
    assert f("Kari Hansen", ["han", "ansen"]) == [("Kari ", False), ("Hansen", True)]       # overlapp
    assert f("Kari Hansen", []) == [("Kari Hansen", False)] and f(None, ["x"]) == [("", False)]
    assert f("Ørjan Åsheim", ["ørjan", "åsheim"]) == [("Ørjan", True), (" ", False), ("Åsheim", True)]
    assert f("Åse", ["åse"]) == [("Åse", True)]
    assert f("Åse", ["åse"]) == [("Åse", True)]                  # Å skrevet som A + ring over (Mac): utheves også
    assert f("Åse", ["ase"]) == [("Åse", True)]


def test_markering_i_json_dekker_alle_soekeord_i_navn_og_epost(con):
    _bygg(con)
    treff = _json(_innlogget(con), "kari hansen").get_json()["treff"][0]
    assert treff["navn_deler"] == [["Kari", True], [" ", False], ["Hansen", True]]
    assert treff["epost_deler"] == [["kari", True], [".", False], ["hansen", True], ["@eksempel.no", False]]


# ============================ kurs ============================

def test_delvis_kurskode_krever_fire_tegn_og_gjelder_ikke_rene_tall(con):
    _bygg(con)
    assert len(_s(con, "vig-").personer) == 2 and len(_s(con, "eft-").personer) == 5
    assert _s(con, "vig").totalt == 0 and _s(con, "eft").totalt == 0      # tre tegn: for uspesifikt
    assert _s(con, "2027").totalt == 0 and _s(con, "202").totalt == 0      # årstallet står i alle kurskodene, men er ikke kurskode


def test_navnefragment_som_ligner_kurskode_gir_ikke_alle_paa_kurset(con):
    """«vig» skal finne Vigdis, ikke i tillegg alle som er påmeldt VIG-kurset (det fylte listen og telte feil)."""
    vig = _kurs(con, "VIG-2027-9", "Veiledning", ["2027-04-12"], "avsluttet")
    andre = _kurs(con, "ABC-2027-1", "Annet", ["2027-04-13"], "avsluttet")
    _meld(con, vig, "Kari", "Hansen", "kari.hansen@eksempel.no")
    _meld(con, vig, "Åse", "Ørsted", "ase.orsted@eksempel.no")
    _meld(con, andre, "Vigdis", "Tråsdahl", "vigdis.trasdahl@eksempel.no")
    con.commit()
    res = _s(con, "vig")
    assert _navn(res) == ["Vigdis Tråsdahl"] and res.totalt == 1
    assert sorted(_navn(_s(con, "vig-2027"))) == ["Kari Hansen", "Åse Ørsted"]        # koden er fortsatt søkbar


def test_snarvei_til_deltakerliste_bare_naar_soeket_peker_paa_faa_kurs(con):
    d = _bygg(con)
    avl = con.execute("SELECT kursnr FROM kurs WHERE id=?", (d.avl,)).fetchone()[0]
    nr = [d.kursnr["eft"], d.kursnr["vig"], d.kursnr["skj"], avl]
    assert len(_s(con, " ".join(map(str, nr[:3]))).kurs) == deltakersok.MAKS_KURSTREFF == 3
    assert _s(con, " ".join(map(str, nr))).kurs == []                    # fire kurs: ingen snarvei


def test_i_biter_folger_konstanten(monkeypatch):
    monkeypatch.setattr(deltakersok, "BIT_STORRELSE", 3)
    assert [len(b) for b in deltakersok._i_biter(range(7))] == [3, 3, 1]


def test_i_biter_deler_uten_aa_miste_noe():
    biter = list(deltakersok._i_biter(range(1001), 400))
    assert [len(b) for b in biter] == [400, 400, 201] and [x for b in biter for x in b] == list(range(1001))
    assert [len(b) for b in deltakersok._i_biter(range(1001))] == [400, 400, 201] and deltakersok.BIT_STORRELSE == 400
    assert list(deltakersok._i_biter([])) == []
    assert [len(b) for b in deltakersok._i_biter(range(800), 400)] == [400, 400]


def test_soek_paa_mange_kurs_faar_med_alle_paameldinger(con, monkeypatch):
    """Et søk som treffer flere kurs enn det går i én spørring skal likevel få med hver eneste påmelding."""
    monkeypatch.setattr(deltakersok, "BIT_STORRELSE", 3)
    ids = [_kurs(con, f"BLK-{i:03d}", f"Blokk {i}", ["2027-09-01"]) for i in range(8)]
    for kid in ids:
        _meld(con, kid, "Bo", "Blokk", "bo.blokk@x.no")
    for kid in ids[:2]:
        _meld(con, kid, "Ida", "Blokk", "ida.blokk@x.no")
    con.commit()
    res = _s(con, "blk-")
    assert res.totalt == 2
    bo = next(p for p in res.personer if p["navn"] == "Bo Blokk")
    ida = next(p for p in res.personer if p["navn"] == "Ida Blokk")
    assert len(bo["paameldinger"]) == 8 and all(m["treff"] for m in bo["paameldinger"])
    assert len(ida["paameldinger"]) == 2 and all(m["treff"] for m in ida["paameldinger"])
    assert bo["grunn"] == "Treff på kurs BLK-000, BLK-001 og 6 til"


# ============================ rangering ============================

def test_eksakt_telefon_og_hpr_rangeres_foer_delvis_treff(con):
    kid = _kurs(con, "RNG-2", "Rangering", ["2027-09-01"])
    _meld(con, kid, "Anna", "Aa", "anna@x.no", telefon="+47 590012345", hpr="12345678")      # delvis treff (alfabetisk først)
    _meld(con, kid, "Zoe", "Zz", "zoe@x.no", telefon="900 12 345", hpr="123456")             # eksakt (alfabetisk sist)
    con.commit()
    assert _navn(_s(con, "90012345")) == ["Zoe Zz", "Anna Aa"]
    assert _navn(_s(con, "123456")) == ["Zoe Zz", "Anna Aa"]


def test_eksakt_paameldingsnummer_rangeres_foer_delvis_treff(con):
    kid = _kurs(con, "RNG-3", "Rangering", ["2027-09-01"])
    for i in range(10):                                                  # så neste påmeldingsnummer har to sifre
        _meld(con, kid, "Fyll", f"Ut{chr(97 + i)}", f"fyll.{chr(97 + i)}@x.no")
    pid_zoe, _ = _meld(con, kid, "Zoe", "Zz", "zoe@x.no")                # eksakt: påmeldingsnr. = søkeordet (alfabetisk sist)
    _meld(con, kid, "Anna", "Aa", f"anna{pid_zoe}@x.no")                # delvis: e-posten inneholder søkeordet (alfabetisk først)
    con.commit()
    assert pid_zoe >= 10
    assert _navn(_s(con, str(pid_zoe))) == ["Zoe Zz", "Anna Aa"]


def test_navn_som_starter_rangeres_paa_alle_ord_og_etter_bindestrek(con):
    kid = _kurs(con, "RNG-4", "Rangering", ["2027-09-01"])
    _meld(con, kid, "Ane", "Bergkari", "ane.bergkari@x.no")             # «kari» står inne i etternavnet
    _meld(con, kid, "Anne-Kari", "Hus", "anne.kari.hus@x.no")           # «kari» starter ordet etter bindestrek
    _meld(con, kid, "Kari", "Zeta", "kari.zeta@x.no")
    con.commit()
    assert _navn(_s(con, "kari")) == ["Anne-Kari Hus", "Kari Zeta", "Ane Bergkari"]
    _meld(con, kid, "Berit", "Karisen", "berit.karisen@x.no")           # «ber kar»: begge starter ord hos Berit, bare «ber» hos Ane
    assert _navn(_s(con, "ber kar"))[0] == "Berit Karisen"


# ============================ «Treff på …» ============================

def test_grunn_tekst_flere_grunner():
    g = deltakersok.grunn_tekst
    assert g([]) == "" and g(["navn"]) == "Treff på navn"
    assert g(["navn", "telefon"]) == "Treff på navn og telefon"
    assert g(["navn", "telefon", "kurs EFT-1"]) == "Treff på navn, telefon og kurs EFT-1"
    assert g(["a", "b", "c", "d"]) == "Treff på a, b, c og d"


def test_grunn_for_kurs_og_paameldingsnummer_kortes_ned(con):
    kids = [_kurs(con, f"GRN-{i}", f"Grunn {i}", ["2027-09-01"]) for i in range(1, 5)]
    pids = [_meld(con, kid, "Gro", "Grunn", "gro.grunn@x.no")[0] for kid in kids]
    con.commit()
    assert _s(con, "grn-").personer[0]["grunn"] == "Treff på kurs GRN-1, GRN-2 og 2 til"
    assert _s(con, " ".join(f"#{p}" for p in pids)).personer[0]["grunn"] == "Treff på påmeldingsnr. " + ", ".join(
        f"#{p}" for p in pids[:3])


def test_grunn_rekkefoelge_er_fast(con):
    d = _bygg(con)
    p = _s(con, f"kari {d.kursnr['eft']}").personer[0]
    assert p["grunner"] == ["kurs EFT-2027-1", "navn"] and p["grunn"] == "Treff på kurs EFT-2027-1 og navn"
    p = _s(con, "kari.hansen 900 12 345").personer[0]
    assert p["grunn"] == "Treff på telefon og e-post"
    assert deltakersok.GRUNN_REKKEFOLGE == ("pnr", "telefon", "epost", "hpr", "kurs", "navn")


# ============================ kursbevis ============================

def test_annen_dokumenttype_gir_ikke_utstedt_kursbevis(con):
    d = _bygg(con)
    for type_ in ("kontrakt", "presentasjon", "veiledning", "annet"):
        con.execute("INSERT INTO dokument (kurs_id, deltaker_id, type, tittel, url) VALUES (?,?,?,?,?)",
                    (d.eft, d.olsen_id, type_, "Noe annet", "db:"))
    con.commit()
    olsen = _rader(_s(con, "kari olsen", idag=date(2027, 3, 3)), "Kari Olsen")["EFT-2027-1"]
    assert olsen["kursbevis"] == "Ikke utstedt ennå: utstedes av morgenjobben" and olsen["kursbevis_farge"] == "gul"
    assert olsen["kursbevis_dok"] is None and olsen["kursbevis_epost"] == ""


def test_kursbevis_forklaring_venteliste_og_avmeldt_er_ikke_bekreftet():
    for status in ("venteliste", "avmeldt", "avslatt"):
        assert deltakersok.kursbevis_forklaring(utstedt=None, kurs_status="avsluttet", paamelding_status=status, dager=2,
                                                mott=2) == ("Ikke utstedt: påmeldingen er ikke bekreftet", "gra")


def test_kursbevis_farger_i_hvert_tilfelle():
    f = deltakersok.kursbevis_forklaring
    assert f(utstedt="2027-03-03 07:00:01", kurs_status="avsluttet", paamelding_status="bekreftet", dager=1, mott=1)[1] == "ok"
    assert f(utstedt=None, kurs_status="avsluttet", paamelding_status="bekreftet", dager=2, mott=1)[1] == "gul"
    assert f(utstedt=None, kurs_status="avsluttet", paamelding_status="bekreftet", dager=2, mott=2)[1] == "gul"
    assert f(utstedt=None, kurs_status="avsluttet", paamelding_status="bekreftet", dager=0, mott=0)[1] == "gra"
    assert f(utstedt=None, kurs_status="aapen", paamelding_status="bekreftet", dager=2, mott=0)[1] == "gra"


def test_kursbevis_utstedt_gaar_foran_avlyst():
    f = deltakersok.kursbevis_forklaring
    assert f(utstedt="2027-03-03 07:00:01", kurs_status="avlyst", paamelding_status="bekreftet", dager=1, mott=1)[0] == (
        "Utstedt 03.03.2027")


def test_kursbevis_dato_er_siste_utstedelse(con):
    d = _bygg(con)
    _bevis(con, d.eft, d.hansen_id, "2027-05-05 08:00:00")             # gjenutstedt senere
    _bevis(con, d.eft, d.hansen_id, "2027-01-01 08:00:00")             # eldre rad settes inn sist (høyere id)
    assert _rader(_s(con, "kari hansen"), "Kari Hansen")["EFT-2027-1"]["kursbevis"] == "Utstedt 05.05.2027"


def test_kursbevis_flertall_paa_dag():
    f = deltakersok.kursbevis_forklaring
    tekst = lambda dager, mott: f(utstedt=None, kurs_status="avsluttet", paamelding_status="bekreftet", dager=dager,  # noqa: E731
                                  mott=mott)[0]
    assert tekst(1, 0) == "Ikke utstedt: oppmøte 0 av 1 dag (kursbevis utstedes ved fullt oppmøte)"
    assert tekst(2, 1) == "Ikke utstedt: oppmøte 1 av 2 dager (kursbevis utstedes ved fullt oppmøte)"
    assert tekst(5, 0).startswith("Ikke utstedt: oppmøte 0 av 5 dager")


# Kurset er «gammelt» når det er gått FORSINKET_DAGER dager eller mer siden siste kursdag uten at morgenjobben har gjort jobben.
SISTE = date(2026, 5, 1)


def _fork(idag, **kw):
    krav = dict(utstedt=None, kurs_status="avsluttet", paamelding_status="bekreftet", dager=1, mott=1, siste_dato=SISTE.isoformat(),
                idag=idag)
    krav.update(kw)
    return deltakersok.kursbevis_forklaring(**krav)


def test_ikke_utstedt_ennaa_gjelder_bare_de_forste_dagene_etter_kurset():
    assert deltakersok.FORSINKET_DAGER == 2
    assert _fork(SISTE + timedelta(days=1)) == ("Ikke utstedt ennå: utstedes av morgenjobben", "gul")
    forsinket = ("Oppfyller vilkårene, men er ikke utstedt (kurset sluttet 01.05.2026) – sjekk Daglig kjøring og hendelsesloggen",
                 "feil")
    assert _fork(SISTE + timedelta(days=2)) == forsinket
    assert _fork(date(2026, 9, 29)) == forsinket                        # måneder etter: noe er galt, ikke «ennå»
    assert _fork(date(2026, 9, 29), utstedt="2026-05-02 07:00:01") == ("Utstedt 02.05.2026", "ok")


def test_forsinkelsen_gjelder_ikke_naar_beviset_ikke_skal_utstedes():
    for idag in (SISTE + timedelta(days=1), date(2026, 9, 29)):
        assert _fork(idag, mott=0) == ("Ikke utstedt: oppmøte 0 av 1 dag (kursbevis utstedes ved fullt oppmøte)", "gul")
        assert _fork(idag, paamelding_status="avmeldt") == ("Ikke utstedt: påmeldingen er ikke bekreftet", "gra")
        assert _fork(idag, dager=0, mott=0) == ("Ikke utstedt: kurset har ingen kursdager", "gra")
        assert _fork(idag, kurs_status="avlyst")[1] == "gra"


def test_kurs_som_er_ferdig_men_ikke_avsluttet_i_systemet():
    """Alle kursdatoene er passert, men kurset står fortsatt som pågående: det sies, i stedet for «ikke avsluttet ennå»."""
    i_gaar = _fork(SISTE + timedelta(days=1), kurs_status="aktiv")
    assert i_gaar == ("Kurset er ferdig, men ikke avsluttet i systemet ennå – morgenjobben avslutter det og utsteder kursbevis", "gra")
    assert _fork(SISTE + timedelta(days=1), kurs_status="aktiv", paamelding_status="avmeldt") == (
        "Kurset er ferdig, men ikke avsluttet i systemet ennå", "gra")
    gammelt = ("Kurset sluttet 01.05.2026, men er ikke avsluttet i systemet – sjekk Daglig kjøring og hendelsesloggen", "feil")
    assert _fork(SISTE + timedelta(days=2), kurs_status="aktiv") == gammelt
    assert _fork(date(2026, 9, 29), kurs_status="aapen", paamelding_status="avmeldt") == gammelt
    for idag in (SISTE, SISTE - timedelta(days=5)):                     # siste kursdag i dag eller senere: ikke ferdig ennå
        assert _fork(idag, kurs_status="aktiv") == ("Kurset er ikke avsluttet ennå", "gra")
    assert _fork(date(2026, 9, 29), kurs_status="avlyst") == ("Kurset er avlyst – kursbevis utstedes ikke", "gra")
    # uten datoer (eldre kall): som før
    assert deltakersok.kursbevis_forklaring(utstedt=None, kurs_status="aktiv", paamelding_status="bekreftet", dager=1,
                                            mott=1) == ("Kurset er ikke avsluttet ennå", "gra")


def test_gammelt_kurs_uten_kursbevis_vises_som_feil_i_soeket(con):
    """Slik Kari Hansen sitt kurs ville sett ut om morgenjobben hadde stått stille i månedsvis."""
    d = _bygg(con)
    olsen = _rader(_s(con, "kari olsen"), "Kari Olsen")["EFT-2027-1"]            # kurset sluttet 02.03.2027, i dag er 01.06.2027
    assert olsen["kursbevis"] == ("Oppfyller vilkårene, men er ikke utstedt (kurset sluttet 02.03.2027) – "
                                  "sjekk Daglig kjøring og hendelsesloggen") and olsen["kursbevis_farge"] == "feil"
    con.execute("UPDATE kurs SET status='aktiv' WHERE id=?", (d.eft,))
    con.commit()
    assert _rader(_s(con, "kari olsen"), "Kari Olsen")["EFT-2027-1"]["kursbevis"] == (
        "Kurset sluttet 02.03.2027, men er ikke avsluttet i systemet – sjekk Daglig kjøring og hendelsesloggen")
    html = _html(_innlogget(con), "/admin/sok?q=kari olsen")
    assert re.search(r'class="merke brytes feil">Kurset sluttet 02\.03\.2027, men er ikke avsluttet', html)


def test_kursbevis_lenke_og_status_paa_e_posten(con, sendt):
    d = _bygg(con)
    dagen_etter = date(2027, 3, 3)
    kursbevis.kjor(Kjoring(con, idag=dagen_etter))                       # utsteder for Olsen (fullt oppmøte) og sender e-post
    con.commit()
    k = _innlogget(con)
    olsen = _rader(_s(con, "kari olsen", idag=dagen_etter), "Kari Olsen")["EFT-2027-1"]
    dok_id = con.execute("SELECT id FROM dokument WHERE deltaker_id=? AND type='kursbevis'", (d.olsen_id,)).fetchone()[0]
    assert olsen["kursbevis_dok"] == dok_id and re.fullmatch(r"E-post sendt \d\d\.\d\d\.\d{4}", olsen["kursbevis_epost"])
    html = _html(k, "/admin/sok?q=kari olsen")
    assert f'href="/dokument/{dok_id}" target="_blank" rel="noopener noreferrer">Åpne kursbeviset</a>' in html
    assert "E-post sendt " in html
    assert k.get(f"/dokument/{dok_id}").status_code == 200               # admin kan åpne beviset fra lenken
    # Hansen har et bevis satt inn uten at noen e-post er logget: det sies, i stedet for å late som
    hansen = _rader(_s(con, "kari hansen"), "Kari Hansen")["EFT-2027-1"]
    assert hansen["kursbevis_epost"] == "Ingen e-postutsending registrert til nåværende adresse" and hansen["kursbevis_dok"]
    # ikke utstedt: ingen lenke og ingen e-poststatus
    nilsen = _rader(_s(con, "kari nilsen"), "Kari Nilsen")["EFT-2027-1"]
    assert nilsen["kursbevis_dok"] is None and nilsen["kursbevis_epost"] == ""
    assert html.count("Åpne kursbeviset") == 1 and "Åpne kursbeviset" not in _html(k, "/admin/sok?q=kari nilsen")


@pytest.mark.parametrize("status,tekst", [("feilet", "E-posten er ikke bekreftet sendt – se hendelsesloggen"),
                                          ("ukjent", "E-posten er ikke bekreftet sendt – se hendelsesloggen"),
                                          ("reservert", "E-posten er ikke bekreftet sendt – se hendelsesloggen"),
                                          ("sendt", "E-post sendt 04.03.2027")])
def test_epostens_status_kommer_fra_utsendingsloggen(con, status, tekst):
    d = _bygg(con)
    con.execute("INSERT INTO utsending_logg (nokkel, mottaker, type, status, sendt_ts) VALUES (?,?,?,?,?)",
                (f"kurs:{d.eft}", "KARI.HANSEN@EKSEMPEL.NO", "kursbevis", status, "2027-03-04 07:00:05"))   # stor/liten bokstav er likegyldig
    con.commit()
    assert _rader(_s(con, "kari hansen"), "Kari Hansen")["EFT-2027-1"]["kursbevis_epost"] == tekst


def test_epostens_status_finnes_uansett_store_og_smaa_bokstaver_paa_personens_adresse(con):
    d = _bygg(con)
    con.execute("UPDATE deltaker SET epost='Kari.Hansen@Eksempel.no' WHERE id=?", (d.hansen_id,))
    con.execute("INSERT INTO utsending_logg (nokkel, mottaker, type, status, sendt_ts) VALUES (?,?,?,?,?)",
                (f"kurs:{d.eft}", "kari.hansen@eksempel.no", "kursbevis", "sendt", "2027-03-04 07:00:05"))
    con.commit()
    assert _rader(_s(con, "kari hansen"), "Kari Hansen")["EFT-2027-1"]["kursbevis_epost"] == "E-post sendt 04.03.2027"


def test_epost_til_en_annen_adresse_eller_annet_kurs_regnes_ikke(con):
    d = _bygg(con)
    for nokkel, mottaker, type_ in ((f"kurs:{d.eft}", "annen.adresse@eksempel.no", "kursbevis"),
                                    (f"kurs:{d.vig}", "kari.hansen@eksempel.no", "kursbevis"),
                                    (f"kurs:{d.eft}", "kari.hansen@eksempel.no", "bekreftelse")):
        con.execute("INSERT INTO utsending_logg (nokkel, mottaker, type) VALUES (?,?,?)", (nokkel, mottaker, type_))
    con.commit()
    assert _rader(_s(con, "kari hansen"), "Kari Hansen")["EFT-2027-1"]["kursbevis_epost"] == (
        "Ingen e-postutsending registrert til nåværende adresse")


def test_oppmote_teller_fra_kursets_foerste_dag(con):
    kid = _kurs(con, "DAG-1", "Første dag i dag", [IDAG.isoformat()])
    _meld(con, kid, "Dag", "Idag", "dag.idag@x.no")
    con.commit()
    assert _rader(_s(con, "dag idag"), "Dag Idag")["DAG-1"]["oppmote"] == "0 av 1 kursdag"          # starter i dag = har startet
    morgen = _kurs(con, "DAG-2", "Neste dag", [(IDAG + timedelta(days=1)).isoformat()])
    _meld(con, morgen, "Dag", "Idag", "dag.idag@x.no")
    con.commit()
    assert _rader(_s(con, "dag idag"), "Dag Idag")["DAG-2"]["oppmote"] == "–"


def test_oppmote_for_ikke_bekreftet_uten_moete_er_strek(con):
    kid = _kurs(con, "OPP-1", "Oppmøte", ["2027-03-01"], "avsluttet")
    pid, _ = _meld(con, kid, "Tone", "Tomme", "tone.tomme@x.no")
    db.meld_av(con, pid)
    con.commit()
    assert _rader(_s(con, "tone tomme"), "Tone Tomme")["OPP-1"]["oppmote"] == "–"          # avmeldt og aldri møtt


def test_paameldinger_sorteres_paa_kursdato_ikke_paameldingsrekkefolge(con):
    sent = _kurs(con, "ORD-2", "Senere kurs", ["2027-11-01"])
    tidlig = _kurs(con, "ORD-1", "Tidligere kurs", ["2027-02-01"])
    _meld(con, sent, "Ola", "Rekke", "ola.rekke@x.no")          # meldt på det seneste kurset først (lavest påmeldingsnr.)
    _meld(con, tidlig, "Ola", "Rekke", "ola.rekke@x.no")
    con.commit()
    assert [m["kode"] for m in _s(con, "ola rekke").personer[0]["paameldinger"]] == ["ORD-2", "ORD-1"]


# ============================ HTML: farger, merker, escaping ============================

def test_kursbevis_farge_og_statusmerke_er_med_i_html(con):
    d = _bygg(con)
    k = _innlogget(con)
    html = _html(k, "/admin/sok?q=kari hansen")
    assert re.search(r'class="merke brytes ok">Utstedt 03\.03\.2027<', html)
    assert re.search(r'class="merke brytes gra">Kurset er ikke avsluttet ennå<', html)
    assert re.search(r'class="merke brytes gul">Ikke utstedt: oppmøte 0 av 1 dag', html)
    assert 'class="treff-rad"' not in html                                   # søket traff personen, ikke en enkelt påmelding
    treff = _html(k, f"/admin/sok?q=%23{d.hansen_skj}")
    assert treff.count('class="treff-rad"') == 1


@pytest.mark.parametrize("felt", ["telefon", "hpr", "arbeidssted", "kursnavn", "kode", "epost"])
def test_alle_felt_fra_databasen_escapes_i_resultatsiden(con, felt):
    payload = "<u>" + felt + "</u>"
    kode = payload if felt == "kode" else "XSS-9"
    kid = _kurs(con, kode, payload if felt == "kursnavn" else "Xss", ["2027-09-01"])
    epost = f"xena.xss{'<u>e</u>' if felt == 'epost' else ''}@x.no"
    _meld(con, kid, "Xena", "Xss", epost, telefon=payload if felt == "telefon" else None,
          hpr="7777" if felt != "hpr" else "<u>7777</u>", arbeidssted=payload if felt == "arbeidssted" else None)
    con.commit()
    html = _html(_innlogget(con), "/admin/sok?q=xena 7777")
    assert "<u>" not in html and "&lt;u&gt;" in html, felt


def test_html_i_det_soeket_traff_escapes_ogsaa_inne_i_mark(con):
    """Det som er markert med <mark> kommer fra samme rå tekst som resten: søk på selve tegnene og se at de escapes."""
    kid = _kurs(con, "MRK-1", "Markering", ["2027-09-01"])
    _meld(con, kid, "<b>x</b>", "Merk", "<i>e</i>@x.no")
    con.commit()
    k = _innlogget(con)
    for q in ("<b>x</b>", "<i>e</i>@x.no"):
        html = k.get("/admin/sok", query_string={"q": q}).get_data(as_text=True)
        main = html.split('<main id="innhold">')[1]
        assert "&lt;b&gt;x&lt;/b&gt;" in main and "<b>x</b>" not in main and "<i>e</i>" not in main, q
    # det uthevede er selve tegnene i navnet og e-posten (søketeksten står også escapet i feltet, så sjekk <mark> og selve raden)
    navn = k.get("/admin/sok", query_string={"q": "x</b"}).get_data(as_text=True)
    assert "&lt;b&gt;<mark>x&lt;/b</mark>&gt; Merk" in navn and "<mark>x</b" not in navn
    epost = k.get("/admin/sok", query_string={"q": "e</i>@x.no"}).get_data(as_text=True)
    assert "&lt;i&gt;<mark>e&lt;/i&gt;@x.no</mark>" in epost and "<mark>e</i>" not in epost


def test_kursnavn_og_kurskode_escapes_i_snarveien_til_deltakerlisten(con):
    kid = _kurs(con, "XSS-7", "<u>kursnavn</u>", ["2027-09-01"])
    _meld(con, kid, "Xena", "Xss", "xena.xss@x.no")
    con.commit()
    kursnr = con.execute("SELECT kursnr FROM kurs WHERE id=?", (kid,)).fetchone()[0]
    html = _html(_innlogget(con), f"/admin/sok?q={kursnr}")
    assert "Åpne deltakerlisten for kurset" in html and "<u>" not in html and "&lt;u&gt;kursnavn" in html


def test_markering_statusmerke_og_tilgjengelighet_i_html(con):
    _bygg(con)
    k = _innlogget(con)
    html = _html(k, "/admin/sok?q=kari hansen")
    assert "<mark>Kari</mark>" in html and "<mark>Hansen</mark>" in html                # navnet
    assert "<mark>kari</mark>" in html and "<mark>hansen</mark>" in html                # e-posten
    assert 'aria-labelledby="person-' in html
    assert re.search(r'class="merke ok">' + re.escape(db.PAAMELDINGSSTATUSER["paameldt"]) + "<", html)
    assert re.search(r'class="merke gra">Avmeldt<', _html(k, "/admin/sok?q=marie berg"))
    assert "personer til" not in _html(k, "/admin/sok?q=kari hansen")


def test_flere_enn_grensen_gir_melding_om_resten(con):
    kid = _kurs(con, "GRS-3", "Mange", ["2027-09-01"])
    for i in range(53):
        _meld(con, kid, "Kari", f"Test{i:02d}", f"kari.test{i:02d}@x.no")
    con.commit()
    assert "Det finnes 3 personer til. Skriv mer" in _html(_innlogget(con), "/admin/sok?q=kari")


def test_persondata_uten_paameldinger_faar_egen_rad(con):
    _kurs(con, "ING-1", "Ingen", ["2027-09-01"])
    con.execute("INSERT INTO deltaker (epost, navn, fornavn, etternavn) VALUES ('uten.paamelding@x.no', 'Uno Uten', 'Uno', 'Uten')")
    con.commit()
    assert "Ingen påmeldinger." in _html(_innlogget(con), "/admin/sok?q=uno uten")


def test_sokefeltets_attributter_uten_javascript(con):
    _bygg(con)
    k = _innlogget(con)
    side = _html(k, "/admin/sok")
    oversikt = _html(k, "/admin")
    assert "data-deltakersok" not in side.split("</header>")[0]                     # ikke i toppmenyen lenger
    for html in (side.split("</header>")[1], oversikt.split("</header>")[1]):
        assert f'maxlength="{deltakersok.MAKS_TEGN}" autocomplete="off" autocapitalize="off" spellcheck="false"' in html
        assert 'aria-label="Søketreff" hidden></div>' in html
    assert 'placeholder="Navn, e-post, telefon, kursnummer eller påmeldingsnr. (#12)"' in side
    assert 'class="sok-etikett" for="sok-side"' in side and 'aria-label="Deltakersøk på siden"' in side
    assert 'placeholder="Navn, e-post, telefon eller påmeldingsnr."' in oversikt     # Oversiktens felt: samme søk, kortere tekst
    assert 'data-json="/admin/sok.json"' in side and 'data-json="/admin/sok.json"' in oversikt
    assert 'class="sok-liste" tabindex="-1" id="sok-deltaker-liste" role="listbox"' in oversikt   # ikke et tabulatorstopp (rullbar liste)


def test_soeksiden_har_autofokus_bare_uten_resultat_og_listen_er_skjult_uten_js(con):
    _bygg(con)
    k = _innlogget(con)
    assert " autofocus" in _html(k, "/admin/sok").split("</header>")[1]
    assert " autofocus" not in _html(k, "/admin/sok?q=kari").split("</header>")[1]
    assert 'aria-label="Søketreff" hidden></div>' in _html(k, "/admin")


def test_sidens_tekster_folger_konstantene(con):
    _bygg(con)
    k = _innlogget(con)
    html = _html(k, "/admin/sok?q=" + "x" * 5000)
    assert f"de første {deltakersok.MAKS_TEGN} tegnene og {deltakersok.MAKS_ORD} ordene" in html
    assert f'maxlength="{deltakersok.MAKS_TEGN}"' in html
    assert f"Skriv minst ett ord med {deltakersok.MIN_TEGN} tegn" in _html(k, "/admin/sok?q=k")
    hjelp = _html(k, "/admin/sok")
    assert f"Minst {deltakersok.MIN_SIFFER} sifre" in hjelp and f"de første {deltakersok.MIN_KURSKODE} tegnene" in hjelp


def test_kontrasten_i_pillene_med_kursbevis_forklaringen_er_minst_4_5_til_1():
    """Setninger på 12 px i .merke.brytes.gul/.ok må ha minst 4,5:1 (WCAG AA). Fargene leses ut av base.html."""
    css = (Path(webapp.app.root_path) / "templates" / "base.html").read_text(encoding="utf-8")

    def lum(hex_):
        r, g, b = (int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5))
        lin = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
        return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)

    def kontrast(a, b):
        hoy, lav = sorted((lum(a), lum(b)), reverse=True)
        return (hoy + 0.05) / (lav + 0.05)

    variabler = dict(re.findall(r"--([a-z-]+):(#[0-9a-f]{6})", css))

    def tekstfarge(klasse):
        egen = re.search(r"\.merke\.brytes\.%s \{ color:(#[0-9a-f]{6});" % klasse, css)       # mørkere farge for setningene
        if egen:
            return egen.group(1)
        return variabler[re.search(r"\.merke\.%s \{ background:#[0-9a-f]{6}; color:var\(--([a-z-]+)\)" % klasse, css).group(1)]

    for klasse in ("ok", "gul", "feil", "gra"):
        bakgrunn = re.search(r"\.merke\.%s \{ background:(#[0-9a-f]{6});" % klasse, css).group(1)
        assert kontrast(tekstfarge(klasse), bakgrunn) >= 4.5, (klasse, tekstfarge(klasse), bakgrunn)
    assert "a[role=option].sok-alle" in css                                 # «Åpne resultatsiden»-raden får sin egen (blå) farge


# ============================ hvilke felt resultatet har ============================

def test_hpr_nummer_vises_bare_naar_soeket_traff_det(con):
    _bygg(con)
    k = _innlogget(con)
    assert _s(con, "kari hansen").personer[0]["vis_hpr"] is False
    assert "1234567" not in _html(k, "/admin/sok?q=kari hansen")
    assert "1234567" not in _json(k, "kari hansen").get_data(as_text=True)
    assert "HPR-nummer 1234567" in _synlig(_html(k, "/admin/sok?q=1234567"))


def test_resultatet_har_bare_forventede_felt_og_ingen_interne_data(con):
    _bygg(con)
    con.execute("UPDATE paamelding SET intern_kommentar='INTERN-QQ', faktura_kommentar='FAKTURA-QQ', faktura_adresse='ADR-QQ', "
                "faktura_epost='faktura-qq@x.no', org_navn='ORG-QQ', faktura_ref='REF-QQ', rabattkode='RABATT-QQ'")
    con.execute("UPDATE deltaker SET yrkestittel='TITTEL-QQ', visma_kunde_id='VISMA-QQ'")
    con.commit()
    res = _s(con, "kari hansen")
    assert set(res.personer[0]) == {"id", "navn", "epost", "telefon", "arbeidssted", "hpr_nr", "vis_hpr", "grunner", "grunn",
                                    "navn_deler", "epost_deler", "paameldinger"}
    assert set(res.personer[0]["paameldinger"][0]) == {
        "paamelding_id", "deltaker_id", "kurs_id", "status", "avslatt_ts", "utgatt_ts", "forlatt_ts", "ekstradeltaker_ts", "opprettet", "kurs_navn",
        "kode", "kursnr", "kurs_status", "status_navn", "datoer", "oppmote", "kursbevis", "kursbevis_farge", "kursbevis_dok",
        "kursbevis_epost", "treff", "_sortering"}
    k = _innlogget(con)
    alt = repr(res) + _html(k, "/admin/sok?q=kari hansen") + _json(k, "kari hansen").get_data(as_text=True)
    # Det tilfeldige CSRF-tokenet i skjemaene kan inneholde «QQ» av ren tilfeldighet (ca. 4 % av kjøringene): det er ikke data fra radene
    alt = re.sub(r'(name="csrf_token" value|data-csrf|name="csrf-token" content)="[^"]*"', r'\1=""', alt)       # skjemafelt, data-attributt og meta
    assert "QQ" not in alt.upper().replace("EKSEMPEL", "")
    tokens = [r[0] for r in con.execute("SELECT innsjekk_token FROM kursdag")]
    assert tokens and not any(t in alt for t in tokens)


# ============================ personvern: søketeksten står aldri i logger eller adresser ============================

def test_soeketeksten_skrives_ikke_til_stdout_eller_stderr(con, capfd):
    _bygg(con)
    k = _innlogget(con)
    capfd.readouterr()
    for url in ("/admin/sok?q=hansenspesial", "/admin/sok?q=k"):
        k.get(url)
    _json(k, "hansenspesial")
    _s(con, "hansenspesial")
    ut, feil = capfd.readouterr()
    assert "hansenspesial" not in (ut + feil).lower()


def test_tilgangslogg_og_takbegrensning_logger_aldri_soeketeksten(con, caplog, monkeypatch):
    """Tilgangsloggen (drift) er avskrudd i test/demo, så den må slås på her: det er den som kjører i Azure."""
    import logging
    _bygg(con)
    k = _innlogget(con)
    monkeypatch.setattr(config, "DEMO", False)
    monkeypatch.setattr(webapp.app, "testing", False)
    monkeypatch.setattr(webapp, "_database_klar", True)
    sikkerhet.takbegrenser.nullstill()
    monkeypatch.setitem(sikkerhet.GRENSER, "admin_sok", (2, 60))
    caplog.set_level(logging.DEBUG)
    statuser = [k.get("/admin/sok?q=hansenspesial").status_code, _json(k, "hansenspesial").status_code,
                _json(k, "hansenspesial").status_code, k.get("/admin/sok?q=hansenspesial").status_code]
    assert statuser == [200, 200, 429, 429]                                           # begge 429-loggene er kjørt
    alle = " | ".join(f"{r.getMessage()}" for r in caplog.records)
    assert "/admin/sok" in alle and "Takbegrensning" in alle                          # loggene ble faktisk skrevet
    assert "hansenspesial" not in alle.lower() and "q=" not in alle


def test_soeketeksten_staar_aldri_i_adressen_som_rullegardinen_bruker():
    """Rullegardinen sender teksten som POST (i kroppen). En adresse med ?q= havner i URL-logger for hvert tastetrykk."""
    with open(webapp.app.static_folder + "/app.js", encoding="utf-8") as f:
        js = f.read()
    bit = js[js.index("Globalt deltakersøk (søkefeltet"):js.index("Deltakervinduet: et klikk")]
    kode = "\n".join(l for l in bit.splitlines() if not l.strip().startswith("//"))
    assert 'method: "POST"' in kode and "JSON.stringify({ q: tekst })" in kode and '"X-CSRF-Token": csrf' in kode
    assert "?q=" not in kode and "encodeURIComponent" not in kode and "adresse + " not in kode
    assert 'referrerPolicy: "no-referrer"' in kode


def test_soekesidene_sender_ikke_referer_med_soeketeksten(con):
    """Klikker man et treff, sender nettleseren adressen til forrige side som Referer - med ?q=navn eller e-post."""
    _bygg(con)
    k = _innlogget(con)
    assert sikkerhet.REFERRER_INGEN == {"admin_sok", "admin_sok_json", "min_side_lenke"}             # lenken til Min side (/min/<lenke>) er også en nøkkel i adressen
    for url in ("/admin/sok", "/admin/sok?q=kari", "/admin/sok?q=x"):
        assert k.get(url).headers["Referrer-Policy"] == "no-referrer", url
    r = _json(k, "kari")
    assert r.headers["Referrer-Policy"] == "no-referrer"
    with k.session_transaction() as s:
        s["admin_sist"] = 0
    assert _json(k, "kari").headers["Referrer-Policy"] == "no-referrer"               # også 401
    assert webapp.app.test_client().get("/admin/sok?q=kari").headers["Referrer-Policy"] == "no-referrer"   # også viderekobling
    k = _innlogget(con)
    for url in ("/admin", "/admin/rapporter", "/kurs/SKJ-2027-3"):                    # andre sider: som før
        assert k.get(url).headers["Referrer-Policy"] == "strict-origin-when-cross-origin", url


def test_dokumentasjonen_er_enig_om_at_url_loggene_staar_av():
    """AZURE-SETUP.md sa «App Service logs → Log Analytics» mens GDPR.md sa at HTTP-loggen skal stå av: den viser adressen
    med ?q=. Begge dokumentene skal si det samme."""
    rot = Path(webapp.app.root_path).parents[1]
    tekst = {navn: (rot / navn).read_text(encoding="utf-8") for navn in ("AZURE-SETUP.md", "GDPR.md", "SECURITY.md")}
    assert "AppServiceHTTPLogs" in tekst["AZURE-SETUP.md"] and "HTTP-logg" in tekst["AZURE-SETUP.md"]
    assert "AppServiceHTTPLogs" in tekst["GDPR.md"] and "Referer" in tekst["GDPR.md"]
    assert "Referrer-Policy" in tekst["SECURITY.md"] and "POST" in tekst["SECURITY.md"]


# ============================ ruter ============================

def test_json_gir_403_uten_data_naar_rollen_mangler_tilgang(con, monkeypatch):
    _bygg(con)
    k = _innlogget(con)
    monkeypatch.setattr(webapp, "_har_rolle_for", lambda *a, **kw: False)
    r = _json(k, "kari")
    assert r.status_code == 403 and r.get_json() == {"feil": "ingen_tilgang"}
    assert b"Hansen" not in r.data and r.headers["Cache-Control"] == "no-store"


def test_takgrensen_er_ikke_slappere_enn_avtalt():
    assert sikkerhet.GRENSER["admin_sok"] == (120, 60)


def test_resultatsiden_og_rullegardinen_deler_takbegrensningen(con, monkeypatch):
    """Ellers kan hele registeret hentes ut, 50 personer om gangen, uten at noen grense slår inn."""
    _bygg(con)
    k = _innlogget(con)
    monkeypatch.setitem(sikkerhet.GRENSER, "admin_sok", (4, 60))
    assert [k.get("/admin/sok?q=kari").status_code for _ in range(4)] == [200, 200, 200, 200]
    assert k.get("/admin/sok?q=kari").status_code == 429 and _json(k, "kari").status_code == 429
    assert k.get("/admin/sok").status_code == 200 and k.get("/admin/sok?q=k").status_code == 200       # gratis: ingen søking


def test_lesetilgang_kan_bruke_rullegardinen_men_fortsatt_ingen_andre_post(con):
    _bygg(con)
    k = _innlogget(con, "lese")
    assert _json(k, "hansen").status_code == 200
    assert webapp.LESE_TILLATTE_POST == {"admin_logg_ut", "admin_sok_json"}
    r = k.post("/admin/kurs/ny", data={"navn": "Nytt", "type": "fysisk", "datoer": "2028-01-01", "start_kl": "09:00",
                                       "slutt_kl": "16:00", "fakturering": "person"})
    assert r.status_code == 403


# ============================ JavaScript (strengsjekker; ekte oppførsel: tests/test_deltakersok_nettleser.py) ============================

def _js():
    with open(webapp.app.static_folder + "/app.js", encoding="utf-8") as f:
        js = f.read()
    start, slutt = js.index("Globalt deltakersøk (søkefeltet"), js.index("Deltakervinduet: et klikk")
    return "\n".join(l for l in js[start:slutt].splitlines() if not l.strip().startswith("//"))


def test_js_lagrer_ikke_soeketeksten_i_nettleseren_eller_sender_den_andre_steder():
    bit = _js()
    for forbudt in ("localStorage", "sessionStorage", "indexedDB", "document.cookie", "sendBeacon", "history.", "XMLHttpRequest"):
        assert forbudt not in bit, forbudt
    assert bit.count("fetch(") == 1 and 'cache: "no-store"' in bit and 'credentials: "same-origin"' in bit


def test_js_slaar_ikke_til_paa_tegn_i_felt_eller_vinduer():
    bit = _js()
    assert 'tag === "INPUT"' in bit and 'tag === "TEXTAREA"' in bit and "isContentEditable" in bit
    assert 'dialog[open]' in bit and "e.ctrlKey" in bit and "e.isComposing" in bit


def test_js_terskler_og_tastatur_er_ikke_endret_ved_uhell():
    """Strengsjekk (nettleseratferden testes i tests/test_deltakersok_nettleser.py): terskelverdier og tastaturhåndtering."""
    bit = _js()
    assert "setTimeout(sok, 200)" in bit and bit.count(".length < 2") == 2
    assert "nr === forespoersel" in bit and "svar.status === 401" in bit and "svar.status === 429" in bit
    assert "svar.status === 400" in bit
    assert "e.metaKey" in bit and "e.altKey" in bit and 'e.key === "ArrowUp"' in bit and "% alle.length" in bit
    assert 'e.relatedTarget' in bit and '"pageshow"' in bit and "e.persisted" in bit and '"mousedown"' in bit
    assert 'indexOf("/admin/") === 0' in bit and 'indexOf("/admin/") !== 0' in bit
    assert "utdatert" in bit and 'liste.removeAttribute("role")' in bit and 'liste.setAttribute("role", "listbox")' in bit
