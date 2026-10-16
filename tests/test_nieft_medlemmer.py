"""NIEFT-medlemslisten (Camilla 15.10.2026, kurs/nieft.py, migrering 36): NIEFT-pris i påmeldingen krever e-posten personen er
NIEFT-medlem med. Står den ikke på listen, stoppes påmeldingen med en melding; står den der, er NIEFT-prisen godkjent med en gang.
Uten liste er alt som før. Alle adresser er oppdiktet, ingen nettverk."""
import html
import re
import io
import zipfile

import pytest

from adressehjelp import ADRESSE
from kurs import config, db, hendelseslogg, nieft, rabatter
from kurs.integrasjoner import epost
from navnehjelp import gruppeskjema

ALLE = {"nieft": (30, True), "ipr_terapeut": (50, True), "student": (50, True), "psyflix": (25, True)}


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


@pytest.fixture
def sendt(monkeypatch):
    ut = []
    monkeypatch.setattr(epost, "send", lambda til, emne, html_, vedlegg=(): ut.append((til, emne, html_)))
    return ut


def _klient(brukernavn=None, passord=None):
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": brukernavn or config.ADMIN_BRUKERNAVN, "passord": passord or config.ADMIN_PASSORD})
    return k


def _offentlig():
    from kurs.web import app as webapp
    return webapp.app.test_client()


def _kurs(con, kode="N1", rabatter_=ALLE, **kw):
    from datetime import date, timedelta
    start = date.today() + timedelta(days=40)
    kid = db.opprett_kurs(con, kode=kode, navn="NIEFT-kurs", datoer=[start.isoformat()], sharepoint_mappe=f"Kurs/{kode}",
                          pris_nok=9900, type="fysisk", sted="Bergen", status="aapen", **kw)
    rabatter.lagre_for_kurs(con, kid, rabatter_, aktor="admin:test")
    con.commit()
    return kid


def _skjema(**over):
    data = {"fornavn": "Kari", "etternavn": "Nordmann", "epost": "kari@eksempel.no", "samtykke": "on", "samtykke_lagring": "on",
            "betaler": "person", "priskategori": "nieft", "rabatt_bekreftet": "1", **ADRESSE}
    data.update(over)
    return data


def _gruppe(**over):
    data = {"kontakt_navn": "Kari HR", "kontakt_epost": "kari.hr@firma.no", "org_nr": "999900003", "faktura_ref": "BEST-1",
            "deltaker_navn": ["Ola Nordmann", "Per Hansen"], "deltaker_epost": ["ola@firma.no", "per@firma.no"],
            "deltaker_telefon": ["", ""], "deltaker_arbeidssted": ["", ""], "deltaker_priskategori": ["", "nieft"],
            "rabatt_bekreftet": "1", "samtykke": "on", "samtykke_lagring": "on"}
    data.update(over)
    return gruppeskjema(data)


def _p(con, epost_="kari@eksempel.no"):
    return con.execute("SELECT p.* FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE d.epost=?", (epost_,)).fetchone()


def _fakturaer(con, pid):
    return [r["belop_nok"] for r in con.execute("SELECT belop_nok FROM faktura WHERE paamelding_id=? ORDER BY id", (pid,))]


def _liste(con, *adresser):
    nieft.erstatt(con, list(adresser), aktor="admin:test")
    con.commit()


def _xlsx(delte=(), i_arket=(), rik=()):
    """En liten Excel-fil: delte tekster, tekst rett i arket, og delte tekster formatert i biter (<r>)."""
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        si = "".join(f"<si><t>{html.escape(t)}</t></si>" for t in delte)
        si += "".join("<si>" + "".join(f"<r><rPr><b/></rPr><t>{html.escape(d)}</t></r>" for d in deler) + "</si>" for deler in rik)
        z.writestr("xl/sharedStrings.xml", f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{si}</sst>')
        rader = "".join(f'<row r="{n}"><c r="A{n}" t="s"><v>{n - 1}</v></c><c r="B{n}" t="inlineStr"><is><t>{html.escape(t)}</t></is></c></row>'
                        for n, t in enumerate(i_arket, start=1))
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet><sheetData>{rader}</sheetData></worksheet>")
    return b.getvalue()


# ============================ filen med listen ============================

def test_excel_alle_adresser_uansett_kolonne_og_formatering():
    data = _xlsx(delte=["Navn", "Ola.Medlem@Eksempel.no", "Kari Medlem <kari.medlem@eksempel.no>", "ikke en adresse"],
                 i_arket=["per_medlem@eksempel.no", "ola.medlem@eksempel.no"], rik=[("anne.", "medlem@eksempel.no")])
    assert nieft.les_fil("Medlemmer 2026.xlsx", data) == nieft.Lesing(
        ["anne.medlem@eksempel.no", "kari.medlem@eksempel.no", "ola.medlem@eksempel.no", "per_medlem@eksempel.no"], 0)


def test_excel_med_formler_leser_bare_verdien():
    """Gjennomgangen 16.10.2026: en kolonne bygget med =B2 (eller =Medlemmer!B3) skal gi adressen, ikke «b2ola@…»."""
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml",
                   '<worksheet><sheetData><row r="1"><c r="A1" t="str"><f>B2</f><v>ola@eksempel.no</v></c>'
                   '<c r="B1" t="str"><f>Medlemmer!B3</f><v>kari@eksempel.no</v></c><c r="C1" t="str"><f t="shared" si="0"/>'
                   '<v>per@eksempel.no</v></c></row></sheetData><autoFilter><filterColumn colId="0"/></autoFilter></worksheet>')
    assert nieft.les_fil("m.xlsx", b.getvalue()).adresser == ["kari@eksempel.no", "ola@eksempel.no", "per@eksempel.no"]


def test_csv_med_semikolon_og_windows_tegnsett():
    data = "Navn;E-post\nØyvind Ås;oyvind@eksempel.no\nÅse;ASE@EKSEMPEL.NO;\n".encode("cp1252")
    assert nieft.les_fil("liste.csv", data) == nieft.Lesing(["ase@eksempel.no", "oyvind@eksempel.no"], 0)


def test_adresser_med_aeoeaa_klippes_aldri_til_en_annen_adresse():
    """Gjennomgangen 16.10.2026: «åse.ødegaard@…» ble før til «degaard@…»; nå er den ikke med, og den telles."""
    data = "åse.ødegaard@eksempel.no\nola&kari@eksempel.no\nmailto:per@eksempel.no.\n'liv@eksempel.no'\n".encode("utf-8")
    assert nieft.les_fil("liste.txt", data) == nieft.Lesing(["liv@eksempel.no", "per@eksempel.no"], 2)


def test_lange_tekster_uten_at_gaar_fort():
    """Gjennomgangen 16.10.2026: regexen brukte kvadratisk tid på lange strekninger uten @."""
    import time
    start = time.monotonic()
    assert nieft.les_fil("liste.csv", b"a" * 400_000 + b"\nola@eksempel.no\n" + b"b@" * 100_000).adresser == ["ola@eksempel.no"]
    assert time.monotonic() - start < 5


def test_zip_bombe_stoppes_mens_den_pakkes_ut(monkeypatch):
    """Gjennomgangen 16.10.2026: størrelsen filen oppgir, kan være feil - grensen telles mens den pakkes ut."""
    monkeypatch.setattr(nieft, "_MAKS_UTPAKKET", 1000)
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/sharedStrings.xml", "<sst>" + "0" * 50_000 + "</sst>")
    data = bytearray(b.getvalue())
    # oppgitt størrelse settes til 10 byte både i det lokale hodet og i katalogen
    lokal = data.index(b"PK\x03\x04")
    data[lokal + 22:lokal + 26] = (10).to_bytes(4, "little")
    katalog = data.index(b"PK\x01\x02")
    data[katalog + 24:katalog + 28] = (10).to_bytes(4, "little")
    with pytest.raises(nieft.Nieftfeil, match="for stor pakket ut|kunne ikke leses"):
        nieft.les_fil("m.xlsx", bytes(data))


@pytest.mark.parametrize("filnavn,data,melding", [
    ("liste.pdf", b"%PDF", "Excel-fil (.xlsx) eller en CSV-fil"),
    ("liste.csv", b"", "tom"),
    ("liste.csv", b"Navn;Telefon\nOla;12345678\n", "Fant ingen e-postadresser"),
    ("liste.xlsx", b"ikke en zip-fil", "kunne ikke leses som en Excel-fil"),
    ("liste.csv", b"a@eksempel.no\n" * (nieft.MAKS_MB * 1024 * 1024 // 14 + 1), "for stor"),
], ids=["pdf", "tom", "ingen-adresser", "ikke-zip", "for-stor"])
def test_filer_som_avvises(filnavn, data, melding):
    with pytest.raises(nieft.Nieftfeil, match=re.escape(melding)):
        nieft.les_fil(filnavn, data)


def test_ny_liste_erstatter_den_gamle_og_bare_antall_logges(con):
    assert not nieft.har_liste(con)
    assert nieft.erstatt(con, ["a@eksempel.no", "B@eksempel.no"], aktor="admin:test") == {"antall": 2, "nye": 2, "fjernet": 0}
    assert nieft.erstatt(con, ["b@eksempel.no", "c@eksempel.no"], aktor="admin:test") == {"antall": 2, "nye": 1, "fjernet": 1}
    assert (nieft.er_medlem(con, " B@Eksempel.NO "), nieft.er_medlem(con, "a@eksempel.no"), nieft.antall(con)) == (True, False, 2)
    detaljer = " ".join(r[0] for r in con.execute("SELECT detaljer FROM hendelse WHERE handling LIKE 'nieft%'"))
    assert "@" not in detaljer and '"antall": 2' in detaljer


def test_legg_til_fjern_og_sok(con):
    assert nieft.legg_til(con, "Ny.Medlem@eksempel.no", aktor="admin:test") is True
    assert nieft.legg_til(con, "ny.medlem@eksempel.no", aktor="admin:test") is False
    with pytest.raises(nieft.Nieftfeil):
        nieft.legg_til(con, "ikke-epost", aktor="admin:test")
    nieft.legg_til(con, "ny_x@eksempel.no", aktor="admin:test")
    assert nieft.sok(con, "ny") == [] and nieft.sok(con, "ny.") == ["ny.medlem@eksempel.no"]
    assert nieft.sok(con, "ny_") == ["ny_x@eksempel.no"]                       # _ er ikke et jokertegn i søket
    assert nieft.fjern(con, "NY.MEDLEM@eksempel.no", aktor="admin:test") is True
    assert nieft.fjern(con, "ny.medlem@eksempel.no", aktor="admin:test") is False


# ============================ påmeldingsskjemaet ============================

def test_uten_liste_er_alt_som_for(con, sendt):
    _kurs(con)
    side = _offentlig().get("/kurs/N1").get_data(as_text=True)
    assert "Bruk e-posten du er registrert med som NIEFT-medlem. Vi sjekker medlemskapet før fakturaen sendes." in side
    assert _offentlig().post("/kurs/N1", data=_skjema()).status_code == 200
    p = _p(con)
    assert (p["priskategori"], p["rabatt_status"]) == ("nieft", "venter") and _fakturaer(con, p["id"]) == []


def test_epost_som_ikke_staar_paa_listen_stoppes(con, sendt):
    _kurs(con)
    _liste(con, "medlem@eksempel.no")
    side = _offentlig().get("/kurs/N1").get_data(as_text=True)
    assert "<strong>Bruk e-posten du er registrert med som NIEFT-medlem.</strong>" in side and "kurs@ipr.no" in side
    r = _offentlig().post("/kurs/N1", data=_skjema())
    tekst = r.get_data(as_text=True)
    assert r.status_code == 400
    assert "Vi finner ikke kari@eksempel.no blant NIEFT-medlemmene" in tekst and "kontakt oss på kurs@ipr.no" in tekst
    assert _p(con) is None and sendt == []


def test_skjemaet_kan_ikke_brukes_til_aa_sla_opp_medlemmer(con, sendt):
    """Gjennomgangen 16.10.2026: et ufullstendig skjema svarer likt for medlem og ikke-medlem - NIEFT-sjekken kommer sist."""
    _kurs(con, fakturering="person")
    _liste(con, "medlem@eksempel.no")
    for e in ("medlem@eksempel.no", "fremmed@eksempel.no"):
        r = _offentlig().post("/kurs/N1", data=_skjema(epost=e, samtykke=""))
        assert r.status_code == 400 and "blant NIEFT-medlemmene" not in r.get_data(as_text=True)
        r = _offentlig().post("/kurs/N1/gruppe", data=_gruppe(deltaker_epost=["ola@firma.no", e], samtykke=""))
        assert r.status_code == 400 and "blant NIEFT-medlemmene" not in r.get_data(as_text=True)
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 0


def test_soket_paa_listen_sender_ikke_adressen_videre():
    from kurs.web import sikkerhet
    assert "admin_nieft" in sikkerhet.REFERRER_INGEN


def test_epost_paa_listen_godkjennes_med_en_gang(con, sendt):
    kid = _kurs(con)
    _liste(con, "kari@eksempel.no")
    assert _offentlig().post("/kurs/N1", data=_skjema(epost="Kari@Eksempel.no", epost_bekreft="kari@eksempel.no")).status_code == 200
    p = _p(con)
    assert (p["priskategori"], p["rabatt_prosent"], p["rabatt_status"]) == ("nieft", 30, "godkjent")
    assert _fakturaer(con, p["id"]) == [6930]                     # fakturaen holdes ikke tilbake
    h = con.execute("SELECT aktor, detaljer FROM hendelse WHERE handling='rabatt_godkjent'").fetchone()
    assert h["aktor"] == rabatter.NIEFT_AKTOR and '"automatisk": "nieft_liste"' in h["detaljer"]
    logger = _klient().get(f"/admin/kurs/{kid}/deltaker/{p['id']}/logger").get_data(as_text=True)
    assert "NIEFT-pris godkjent" in logger and "Automatisk: e-posten står på NIEFT-medlemslisten." in logger
    assert "Systemet (automatisk)" in logger and "Ukjent" not in logger


def test_andre_priser_bryr_seg_ikke_om_listen(con, sendt):
    _kurs(con)
    _liste(con, "medlem@eksempel.no")
    assert _offentlig().post("/kurs/N1", data=_skjema(priskategori="ipr_terapeut")).status_code == 200
    assert _offentlig().post("/kurs/N1", data=_skjema(epost="ola@eksempel.no", priskategori="")).status_code == 200
    assert (_p(con)["priskategori"], _p(con)["rabatt_status"]) == ("ipr_terapeut", "venter")
    assert _p(con, "ola@eksempel.no")["priskategori"] is None


def test_nieft_uten_godkjenning_stoppes_likevel_naar_epost_mangler(con, sendt):
    _kurs(con, rabatter_={"nieft": (30, False)})
    _liste(con, "medlem@eksempel.no")
    assert _offentlig().post("/kurs/N1", data=_skjema()).status_code == 400
    assert _offentlig().post("/kurs/N1", data=_skjema(epost="medlem@eksempel.no")).status_code == 200
    p = _p(con, "medlem@eksempel.no")
    assert (p["rabatt_status"], _fakturaer(con, p["id"])) == (None, [6930])
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='rabatt_godkjent'").fetchone()[0] == 0


# ============================ bedriftspåmeldingen ============================

def test_bedriftspaamelding_sjekker_hver_deltaker(con, sendt):
    _kurs(con, fakturering="person")
    _liste(con, "medlem@firma.no")
    side = _offentlig().get("/kurs/N1/gruppe").get_data(as_text=True)
    assert "bruk e-posten deltakeren er registrert med som NIEFT-medlem" in side
    r = _offentlig().post("/kurs/N1/gruppe", data=_gruppe())
    assert r.status_code == 400 and "Deltaker 2: Vi finner ikke per@firma.no blant NIEFT-medlemmene" in r.get_data(as_text=True)
    assert _p(con, "ola@firma.no") is None
    r = _offentlig().post("/kurs/N1/gruppe", data=_gruppe(deltaker_epost=["ola@firma.no", "medlem@firma.no"]))
    assert r.status_code == 302
    p = _p(con, "medlem@firma.no")
    assert (p["priskategori"], p["rabatt_status"]) == ("nieft", "godkjent")
    assert con.execute("SELECT aktor FROM hendelse WHERE handling='rabatt_godkjent'").fetchone()[0] == rabatter.NIEFT_AKTOR


# ============================ administrator ============================

def test_administrator_som_melder_paa_stoppes_ikke(con):
    kid = _kurs(con)
    _liste(con, "medlem@eksempel.no")
    r = _klient().post(f"/admin/kurs/{kid}/deltaker/ny", data={"fornavn": "Kari", "etternavn": "Nordmann", "epost": "kari@eksempel.no",
                                                               "betaler": "person", "priskategori": "nieft", **ADRESSE})
    assert r.status_code == 302 and (_p(con)["priskategori"], _p(con)["rabatt_status"]) == ("nieft", "godkjent")


def test_siden_last_opp_sok_legg_til_og_fjern(con):
    k = _klient()
    side = k.get("/admin/rabatter/nieft").get_data(as_text=True)
    assert "Listen er tom." in side and "NIEFT-medlemmer" in k.get("/admin/rabatter").get_data(as_text=True)
    r = k.post("/admin/rabatter/nieft/last-opp", data={"liste": (io.BytesIO(_xlsx(delte=["a@eksempel.no", "b@eksempel.no"])), "m.xlsx")})
    assert r.status_code == 302 and nieft.antall(con) == 2
    side = k.get("/admin/rabatter/nieft").get_data(as_text=True)
    assert "2 e-postadresser" in side and "a@eksempel.no" not in side            # hele listen vises aldri
    assert "a@eksempel.no" in k.get("/admin/rabatter/nieft?q=a@eks").get_data(as_text=True)
    k.post("/admin/rabatter/nieft/legg-til", data={"epost": "C@eksempel.no"})
    k.post("/admin/rabatter/nieft/fjern", data={"epost": "a@eksempel.no"})
    assert sorted(r[0] for r in con.execute("SELECT epost FROM nieft_medlem")) == ["b@eksempel.no", "c@eksempel.no"]
    r = k.post("/admin/rabatter/nieft/last-opp", data={"liste": (io.BytesIO(b"bare tekst"), "m.csv")}, follow_redirects=True)
    assert "Fant ingen e-postadresser" in r.get_data(as_text=True) and nieft.antall(con) == 2      # en feil fil endrer ingenting


def test_lesetilgang_kan_soke_men_ikke_endre(con):
    _liste(con, "a@eksempel.no")
    db.opprett_admin_bruker(con, "leser", "Leser", "passord-som-holder", rolle="lese")
    con.commit()
    lese = _klient("leser", "passord-som-holder")
    side = lese.get("/admin/rabatter/nieft?q=a@eks").get_data(as_text=True)
    assert "a@eksempel.no" in side and "Last opp listen" not in side and "Fjern" not in side
    for sti, data in (("last-opp", {}), ("legg-til", {"epost": "x@eksempel.no"}), ("fjern", {"epost": "a@eksempel.no"}),
                      ("godkjenn-ventende", {})):
        assert lese.post(f"/admin/rabatter/nieft/{sti}", data=data).status_code == 403
    assert nieft.antall(con) == 1


def test_ventende_fra_for_listen_kan_godkjennes_samlet(con, sendt):
    _kurs(con)
    _offentlig().post("/kurs/N1", data=_skjema())
    _offentlig().post("/kurs/N1", data=_skjema(epost="ola@eksempel.no"))
    _liste(con, "kari@eksempel.no")
    k = _klient()
    rabattside = k.get("/admin/rabatter").get_data(as_text=True)
    assert "Står på NIEFT-listen" in rabattside and "Står ikke på NIEFT-listen" in rabattside
    assert "1 NIEFT-rabatt venter" in k.get("/admin/rabatter/nieft").get_data(as_text=True)
    k.post("/admin/rabatter/nieft/godkjenn-ventende")
    kari, ola = _p(con), _p(con, "ola@eksempel.no")
    assert (kari["rabatt_status"], _fakturaer(con, kari["id"])) == ("godkjent", [6930])
    assert (ola["rabatt_status"], _fakturaer(con, ola["id"])) == ("venter", [])
    assert "venter, og e-posten står på listen" not in k.get("/admin/rabatter/nieft").get_data(as_text=True)


def test_hendelsen_uten_automatikk_er_som_for():
    assert hendelseslogg._TEKSTER["rabatt_godkjent"]({"kategori": "nieft"}, None, None) == [("NIEFT-pris godkjent", [])]
