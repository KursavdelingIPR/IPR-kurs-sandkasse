"""Påmeldingsstatusene «Påmeldt» og «Ekstradeltaker»: databaseverdien er fortsatt «bekreftet» for begge (Ekstradeltaker er en
påmelding med plass og ekstradeltaker_ts satt), men navnene som vises står bare ett sted (db.PAAMELDINGSSTATUSER og
db.PAAMELDINGSSTATUSER_FLERTALL) og brukes likt overalt: filter, statuskolonne, deltakervindu, Logger, rapporter, eksporter
og meldinger. «Bekreftet» finnes ikke lenger som navn på en status. E-posten deltakeren får (bekreftelsen) er uendret.
"""
import ast
import csv
import io
import json
import re
from datetime import date
from pathlib import Path

import pytest

from adressehjelp import ADRESSE
from kurs import config, db, deltakerliste, hendelseslogg

ROT = Path(__file__).resolve().parent.parent
KURS = ROT / "kurs"
ADMIN = f"admin:{config.ADMIN_BRUKERNAVN}"
NAVN = db.PAAMELDINGSSTATUSER
FORNAVN = {"paameldt": "Bente", "ekstradeltaker": "Eva", "venteliste": "Vera", "avmeldt": "Arne", "avslatt": "Siri",
           "utgatt": "Una", "forlatt": "Finn"}
GAMMELT_ORD = re.compile(r"\b(Bekreftet|Bekreftede|bekreftet|bekreftede)\b")


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _admin(con=None, rolle=None):
    """Innlogget testklient: standardbrukeren (systemadministrator), eller en bruker med `rolle`."""
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    data = {"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD}
    if rolle:
        db.opprett_admin_bruker(con, f"test.{rolle}", f"Test {rolle.capitalize()}", "passord-som-holder", rolle=rolle)
        con.commit()
        data = {"brukernavn": f"test.{rolle}", "passord": "passord-som-holder"}
    assert k.post("/admin/logg-inn", data=data).status_code == 302
    return k


def _kurs_med_alle_statuser(con, kode="ETI1"):
    """Ett kurs med ledig plass og én påmelding i hver av de syv statusene. Returnerer (kurs_id, {status: paamelding_id})."""
    kid = db.opprett_kurs(con, kode=kode, navn="Veiledning i gruppe", datoer=[date(2031, 10, 16).isoformat()],
                          sharepoint_mappe=f"Kurs/{kode}", pris_nok=2500, kapasitet=10, fakturering="person")
    pid = {}
    for status in ("avmeldt", "avslatt", "utgatt", "forlatt", "paameldt", "ekstradeltaker", "venteliste"):   # venteliste sist
        fornavn = FORNAVN[status]
        p, _ = db.meld_paa(con, kid, epost=f"{fornavn.lower()}@eksempel.no", fornavn=fornavn, etternavn="Test")
        if status != "paameldt":
            db.sett_paamelding_status(con, p, status, aktor=ADMIN)
        pid[status] = p
    con.commit()
    return kid, pid


def _synlig_tekst(html: str) -> str:
    """Teksten en person ser: uten skript, stil og tagger (attributter som value=«bekreftet» regnes ikke med)."""
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _uten_gammelt_ord(html: str, hva: str) -> None:
    funn = GAMMELT_ORD.findall(_synlig_tekst(html))
    assert not funn, f"{hva}: viser fortsatt {funn}"


def _csv(tekst: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(tekst.lstrip("﻿")), delimiter=";"))


# ============================ navnet er definert ett sted, databasen er uendret ============================

def test_navnene_og_visningsstatusene(con):
    """Syv statuser i denne rekkefølgen overalt. Nøklene er visningsstatusene, ikke databaseverdien «bekreftet»."""
    assert list(NAVN) == ["paameldt", "ekstradeltaker", "venteliste", "avmeldt", "avslatt", "utgatt", "forlatt"]
    assert list(NAVN.values()) == ["Påmeldt", "Ekstradeltaker", "Venteliste", "Avmeldt", "Avslått", "Utgått", "Forlatt"]
    assert list(db.PAAMELDINGSSTATUSER_FLERTALL) == list(NAVN)
    assert list(db.PAAMELDINGSSTATUSER_FLERTALL.values()) == \
        ["Påmeldte", "Ekstradeltakere", "Venteliste", "Avmeldte", "Avslåtte", "Utgåtte", "Forlatte"]
    assert db.HAR_PLASS == ("paameldt", "ekstradeltaker")
    assert "bekreftet" not in NAVN and not GAMMELT_ORD.search(" ".join(NAVN.values()))       # «Bekreftet» er ikke en status
    # databaseverdien (i eldre hendelser og i kolonnen status) heter Påmeldt når den vises
    assert db.statusnavn("bekreftet") == "Påmeldt" and db.statusnavn("bekreftet", flertall=True) == "Påmeldte"
    assert db.statusnavn("ekstradeltaker") == "Ekstradeltaker" and db.statusnavn("ukjent") == "ukjent"


def test_databasen_lagrer_fortsatt_bekreftet_for_begge_og_status_sjekken_er_uendret(con):
    """Påmeldt og Ekstradeltaker er begge status='bekreftet' i databasen: bare ekstradeltaker_ts skiller dem."""
    kid, pid = _kurs_med_alle_statuser(con)
    sql = "SELECT status, avslatt_ts, utgatt_ts, forlatt_ts, ekstradeltaker_ts FROM paamelding WHERE id=?"
    assert tuple(con.execute(sql, (pid["paameldt"],)).fetchone()) == ("bekreftet", None, None, None, None)
    ekstra = tuple(con.execute(sql, (pid["ekstradeltaker"],)).fetchone())
    assert ekstra[:4] == ("bekreftet", None, None, None) and ekstra[4]                       # tidsstempelet er satt
    assert (db.antall_bekreftet(con, kid), db.antall_med_plass(con, kid)) == (1, 2)         # begge har databaseverdien, men bare Påmeldt teller mot plassene
    for fil in ("schema.sql", "schema_postgres.sql"):
        tekst = (KURS / fil).read_text(encoding="utf-8")
        assert "CHECK (status IN ('bekreftet','venteliste','avmeldt'))" in tekst, fil     # status-sjekken er urørt
        assert "Påmeldt" not in tekst and "Ekstradeltaker" not in tekst, fil
    migreringer = (KURS / "migreringer.py").read_text(encoding="utf-8")
    assert "Påmeldt" not in migreringer and "Ekstradeltaker" not in migreringer            # ingen migrering for navn


def test_navnet_defineres_bare_i_db_ingen_gammelt_ord_i_tekster_og_maler():
    """Ingen tekst i kildekoden (strenger, maler, JavaScript) sier «Bekreftet»/«bekreftede» om påmeldingsstatus: de
    henter ordet fra db.PAAMELDINGSSTATUSER. Ordet «Bekreftet» er ikke lenger navnet på en status i det hele tatt.
    Navnet «Ekstradeltaker» (og «ekstradeltakere») skrives heller ikke ut i tekster: det står bare i definisjonen i db.py.
    Unntak står i TILLATT med grunn - alt annet er nye forekomster å ta stilling til.
    (Databaseverdien 'bekreftet' i SQL og koden er selvsagt uendret, og kommentarer og docstrings regnes ikke med for
    «Ekstradeltaker», som er utviklerforklaring og ikke tekst brukeren ser.)"""
    tillatt = {("kurs/import_deltakere.py", "ikke ennaa bekreftede"): "import: forhåndsvisninger som ennå ikke er bekreftet"}
    definisjon = {("kurs/db.py", "Ekstradeltaker"), ("kurs/db.py", "Ekstradeltakere")}      # db.PAAMELDINGSSTATUSER(_FLERTALL)
    gammelt, nytt = r"Bekreftet|Bekreftede|bekreftede|«bekreftet»", r"Ekstradeltaker|ekstradeltakere"
    funn = []
    for sti in sorted(KURS.rglob("*.py")):
        rel = sti.relative_to(ROT).as_posix()
        tre = ast.parse(sti.read_text(encoding="utf-8"))
        docstrings = {id(n.body[0].value) for n in ast.walk(tre)
                      if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body
                      and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        for node in ast.walk(tre):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for m in re.finditer(gammelt, node.value):
                    if not any(rel == f and t in node.value for f, t in tillatt):
                        funn.append(f"{rel}:{node.lineno}: {node.value[max(0, m.start() - 30):m.end() + 30]!r}")
                if id(node) not in docstrings and (rel, node.value) not in definisjon:
                    funn += [f"{rel}:{node.lineno}: {node.value[max(0, m.start() - 30):m.end() + 30]!r}"
                             for m in re.finditer(nytt, node.value)]
    for sti in sorted([*(KURS / "web" / "templates").glob("*.html"), *(KURS / "maler").rglob("*.html"),
                       *(KURS / "web" / "static").glob("*.js")]):
        tekst = sti.read_text(encoding="utf-8")
        if sti.suffix == ".js":                                   # ikke kommentarene
            tekst = re.sub(r"/\*.*?\*/|//[^\n]*", "", tekst, flags=re.S)
        for m in re.finditer(f"{gammelt}|{nytt}", tekst):
            funn.append(f"{sti.relative_to(ROT).as_posix()}: {tekst[max(0, m.start() - 30):m.end() + 30]!r}")
    assert not funn, "Hardkodet statusnavn (bruk db.PAAMELDINGSSTATUSER):\n" + "\n".join(funn)


def test_ingen_kode_slaar_opp_navnet_paa_databaseverdien_bekreftet():
    """Nøklene i db.PAAMELDINGSSTATUSER er visningsstatusene (paameldt, ekstradeltaker ...). Kode som slår opp
    PAAMELDINGSSTATUSER['bekreftet'] eller malens STATUS.bekreftet gir KeyError: bruk 'paameldt' for ordet «Påmeldt»
    (alle som har plass), eller db.statusnavn(verdi) når verdien kan være databaseverdien bekreftet."""
    funn = []
    for sti in sorted(KURS.rglob("*.py")):
        for node in ast.walk(ast.parse(sti.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and node.slice.value == "bekreftet"
                    and "PAAMELDINGSSTATUSER" in ast.unparse(node.value)):
                funn.append(f"{sti.relative_to(ROT).as_posix()}:{node.lineno}: {ast.unparse(node)}")
    for sti in sorted((KURS / "web" / "templates").glob("*.html")):
        for m in re.finditer(r"STATUS(_FLERTALL)?\.bekreftet|STATUS(_FLERTALL)?\[.bekreftet.\]", sti.read_text(encoding="utf-8")):
            funn.append(f"{sti.name}: {m.group(0)}")
    assert not funn, "Slår opp navnet på databaseverdien:\n" + "\n".join(funn)


# ============================ alle steder navnet vises ============================

def test_deltakerlisten_chips_kort_og_statuskolonne(con):
    kid, pid = _kurs_med_alle_statuser(con)
    html = _admin().get(f"/admin/kurs/{kid}/deltakere").get_data(as_text=True)
    assert dict(re.findall(r'<label><input type="checkbox" name="status" value="(\w+)" >\s*([^<(]+?) <span', html)) == \
        {k: v for k, v in NAVN.items()}                                                    # «Påmeldt (1)» først, som før
    assert re.findall(r'name="status" value="(\w+)"', html) == list(NAVN)                  # rekkefølgen er uendret
    for navn in NAVN.values():
        assert f'<div class="liten dempet">{navn}</div><div class="stat">1</div>' in html
    valgt = re.findall(r'<option value="(\w+)" selected>([^<]+)</option>', html)
    assert dict(valgt) == dict(NAVN)                                                       # hver rad viser sin status
    assert "Send e-post til alle påmeldte" in html and "Send e-post til venteliste" in html
    assert re.search(r'<tr data-filtrer="deltaker" data-status="ekstradeltaker"', html)     # raden bærer visningsstatusen
    assert 'class="statusvelger ok"' in html and 'class="statusvelger bla"' in html         # grønt for Påmeldt, blått for Ekstradeltaker
    _uten_gammelt_ord(html, "deltakerlisten")


def test_oppmotelisten_sier_paameldte(con):
    kid, _ = _kurs_med_alle_statuser(con)
    html = _admin().get(f"/admin/kurs/{kid}/deltakere?sok=finnes.ikke").get_data(as_text=True)
    assert "Ingen påmeldte deltakere matcher søket." in html
    kid2 = db.opprett_kurs(con, kode="ETI2", navn="Tomt kurs", datoer=[date(2031, 11, 16).isoformat()],
                           sharepoint_mappe="Kurs/ETI2")
    con.commit()
    tomt = _admin().get(f"/admin/kurs/{kid2}/deltakere").get_data(as_text=True)
    assert "Ingen påmeldte deltakere." in tomt and "Ingen deltakere er registrert ennå." in tomt
    _uten_gammelt_ord(tomt, "kurs uten deltakere")


def test_deltakervinduet_badge_nedtrekk_og_melding(con):
    kid, pid = _kurs_med_alle_statuser(con)
    k = _admin()
    html = k.get(f"/admin/kurs/{kid}/deltaker/{pid['paameldt']}").get_data(as_text=True)
    assert '<p class="feltverdi"><span class="merke ok">Påmeldt</span></p>' in html
    valg = re.search(r'<select id="f-ny-status".*?</select>', html, re.S).group(0)
    assert re.findall(r'<option value="(\w*)"[^>]*>([^<]+)</option>', valg) == \
        [("", "Velg ny status")] + [(s, n) for s, n in NAVN.items() if s != "paameldt"]   # «Ekstradeltaker» er ett av valgene
    assert "Blir en plass ledig, rykker første på ventelisten opp." in html
    _uten_gammelt_ord(html, "deltakervinduet")
    ekstra = k.get(f"/admin/kurs/{kid}/deltaker/{pid['ekstradeltaker']}").get_data(as_text=True)
    assert '<p class="feltverdi"><span class="merke bla">Ekstradeltaker</span></p>' in ekstra
    assert "Satt til ekstradeltaker " in ekstra and "tar ikke plass og teller ikke som påmeldt" in ekstra
    valg = re.search(r'<select id="f-ny-status".*?</select>', ekstra, re.S).group(0)
    assert re.findall(r'<option value="(\w*)"[^>]*>', valg) == ["", *[s for s in NAVN if s != "ekstradeltaker"]]
    r = k.post(f"/admin/kurs/{kid}/deltaker/{pid['venteliste']}/status", data={"status": "paameldt"}, follow_redirects=True)
    assert "Status endret til «påmeldt»." in r.get_data(as_text=True)
    assert '<span class="merke ok">Påmeldt</span>' in r.get_data(as_text=True)
    r = k.post(f"/admin/kurs/{kid}/deltaker/{pid['paameldt']}/status", data={"status": "ekstradeltaker"}, follow_redirects=True)
    assert "Status endret til «ekstradeltaker»." in r.get_data(as_text=True)
    assert '<span class="merke bla">Ekstradeltaker</span>' in r.get_data(as_text=True)


def test_logger_bruker_navnet(con):
    kid, pid = _kurs_med_alle_statuser(con)
    db.sett_paamelding_status(con, pid["venteliste"], "paameldt", aktor=ADMIN)
    db.sett_paamelding_status(con, pid["paameldt"], "avmeldt", aktor=ADMIN)
    con.commit()
    k = _admin(con, "kursadmin")            # systemadministrator ser også rå tekniske data (databaseverdier) i Logger
    for status in ("venteliste", "paameldt", "ekstradeltaker"):
        p = con.execute("SELECT * FROM paamelding WHERE id=?", (pid[status],)).fetchone()
        rader = hendelseslogg.for_paamelding(con, p, systemadmin=False).rader
        hva = [r.hva for r in rader]
        assert "Påmeldt" in hva and not any(GAMMELT_ORD.search(t) for t in hva), hva
        html = k.get(f"/admin/kurs/{kid}/deltaker/{pid[status]}/logger").get_data(as_text=True)
        _uten_gammelt_ord(html, "Logger")
    venteliste = hendelseslogg.for_paamelding(con, con.execute("SELECT * FROM paamelding WHERE id=?", (pid["venteliste"],)).fetchone(),
                                              systemadmin=False).rader
    assert [r.hva for r in venteliste][0] == "Status endret fra venteliste til påmeldt"
    assert venteliste[0].detaljer == ["Status før: Venteliste", "Ny status: Påmeldt"]
    ekstra = hendelseslogg.for_paamelding(con, con.execute("SELECT * FROM paamelding WHERE id=?", (pid["ekstradeltaker"],)).fetchone(),
                                          systemadmin=False).rader
    assert [r.hva for r in ekstra][0] == "Status endret fra påmeldt til ekstradeltaker"
    assert ekstra[0].detaljer == ["Status før: Påmeldt", "Ny status: Ekstradeltaker", "Deltar på: hele kurset"]


def test_deltaker_csv_og_eksporter_valgte_viser_navnet_ikke_databaseverdien(con):
    kid, pid = _kurs_med_alle_statuser(con)
    k = _admin()
    for resp in (k.get(f"/admin/kurs/{kid}/deltakere.csv"),
                 k.post(f"/admin/kurs/{kid}/deltakere/eksporter-valgte.csv", data={"paamelding_id": list(pid.values())})):
        rader = {r["Fornavn"]: r["Status"] for r in _csv(resp.get_data(as_text=True))}
        assert rader == {FORNAVN[s]: navn for s, navn in NAVN.items()}
    for verdi, navn in (("paameldt", "Påmeldt"), ("ekstradeltaker", "Ekstradeltaker")):
        filtrert = k.get(f"/admin/kurs/{kid}/deltakere.csv?status={verdi}").get_data(as_text=True)
        assert [r["Status"] for r in _csv(filtrert)] == [navn]                              # begge har databaseverdien bekreftet


def test_utskriftslisten_og_dens_csv(con):
    kid, pid = _kurs_med_alle_statuser(con)
    k = _admin()
    html = k.get(f"/admin/kurs/{kid}/deltakerliste?valgt=1&kol=status&kol=paameldt&status=alle").get_data(as_text=True)
    for navn in NAVN.values():
        assert f"<td class=\"\">{navn}</td>" in html
    assert "Påmeldingsdato" in html and ">Påmeldte</option>" in html and ">Alle</option>" in html   # datokolonnen ≠ statusen
    assert deltakerliste.STATUSVALG == {"plass": "Påmeldte og ekstradeltakere", **db.PAAMELDINGSSTATUSER_FLERTALL, "alle": "Alle"}
    assert ">Ekstradeltakere</option>" in html and ">Påmeldte og ekstradeltakere</option>" in html
    standard = k.get(f"/admin/kurs/{kid}/deltakerliste?valgt=1&kol=status").get_data(as_text=True)
    assert "1 påmeldt + 1 ekstradeltaker" in re.sub(r"\s+", " ", standard)                     # utskriften åpner med alle som har plass, tallene hver for seg
    bare_paameldte = k.get(f"/admin/kurs/{kid}/deltakerliste?valgt=1&kol=status&status=paameldt").get_data(as_text=True)
    assert "Påmeldte: 1" in bare_paameldte
    csv_ = _csv(k.get(f"/admin/kurs/{kid}/deltakerliste.csv?valgt=1&kol=status&kol=paameldt&status=alle").get_data(as_text=True))
    assert sorted(r["Status"] for r in csv_) == sorted(NAVN.values()) and "Påmeldingsdato" in csv_[0]
    _uten_gammelt_ord(html, "utskriftslisten")


def test_kursrapporten_og_deltakerregisteret(con):
    kid, pid = _kurs_med_alle_statuser(con)
    k = _admin()
    html = k.get("/admin/rapporter/kurs").get_data(as_text=True)
    assert '<th scope="col">Påmeldt/kapasitet</th>' in html
    assert '<th scope="col">Venteliste</th><th scope="col">Avmeldt</th><th scope="col">Avslått</th>' in html
    _uten_gammelt_ord(html, "kursrapporten")
    rader = _csv(k.get("/admin/rapporter/kurs.csv").get_data(as_text=True))
    assert list(rader[0])[6:15] == ["Påmeldt", "Kapasitet", "Fyllingsgrad (%)", "Ekstradeltaker", "Venteliste",
                                    "Avmeldt", "Avslått", "Utgått", "Forlatt"]
    assert [rader[0][k] for k in ("Påmeldt", "Ekstradeltaker", "Venteliste")] == ["1", "1", "1"]   # Påmeldt teller ikke ekstradeltakerne
    assert '>Ekstradeltaker</th>' in html and 'title="Ekstradeltakere er på kurset, men tar ikke plass og er ikke regnet med i påmeldt eller fyllingsgraden"' in html
    reg = k.get("/admin/rapporter/deltakere").get_data(as_text=True)
    assert '<th scope="col">Antall kurs (påmeldt)</th>' in reg
    _uten_gammelt_ord(reg, "deltakerregisteret")
    assert "Antall kurs (påmeldt)" in list(_csv(k.get("/admin/rapporter/deltakere.csv").get_data(as_text=True))[0])
    dashbord = k.get("/admin/rapporter").get_data(as_text=True)
    assert "Påmeldte i perioden" in dashbord
    _uten_gammelt_ord(dashbord, "rapporter")
    person = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pid["paameldt"],)).fetchone()[0]
    html = k.get(f"/admin/rapporter/deltaker/{person}").get_data(as_text=True)
    assert '<span class="merke ok">Påmeldt</span>' in html and "Påmeldingsdato" in html
    _uten_gammelt_ord(html, "deltakerens rapport")
    person = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pid["ekstradeltaker"],)).fetchone()[0]
    assert '<span class="merke bla">Ekstradeltaker</span>' in k.get(f"/admin/rapporter/deltaker/{person}").get_data(as_text=True)
    # deltakerregisteret teller kurs personen er PÅMELDT på: en ekstradeltaker er ikke en fullverdig deltaker
    antall = {r["Fornavn"]: r["Antall kurs (påmeldt)"] for r in _csv(k.get("/admin/rapporter/deltakere.csv").get_data(as_text=True))}
    assert antall[FORNAVN["paameldt"]] == "1" and antall[FORNAVN["ekstradeltaker"]] == antall[FORNAVN["venteliste"]] == "0"


def test_oversikt_og_kursoppsett(con):
    kid, _ = _kurs_med_alle_statuser(con)
    k = _admin()
    oversikt = k.get("/admin").get_data(as_text=True)
    assert "Påmeldte (åpne kurs)" in oversikt                                                       # statuskortet
    assert "Påmeldte / kapasitet" in oversikt and 'data-etikett="Påmeldte"' in oversikt            # kurslisten
    oppsett = k.get(f"/admin/kurs/{kid}/oppsett").get_data(as_text=True)
    assert "Kurset har 1 påmeldte deltakere og 1 ekstradeltaker. Endres format eller sted" in oppsett   # tallene hver for seg
    for hva, html in (("oversikten", oversikt), ("oppsettet", oppsett)):
        _uten_gammelt_ord(html, hva)


def test_bulk_forhandsvisning_og_import_viser_navnet(con):
    kid, pid = _kurs_med_alle_statuser(con)
    ider = (pid["paameldt"], pid["ekstradeltaker"], pid["venteliste"])
    con.execute("UPDATE paamelding SET sveiper_utsatt=1 WHERE id IN (?, ?, ?)", ider)
    con.commit()                                       # holdt tilbake (manuell registrering): kan behandles
    k = _admin()
    html = k.post(f"/admin/kurs/{kid}/deltakere/bulk/forhandsvis", data={"paamelding_id": list(ider)}).get_data(as_text=True)
    assert '<span class="merke ok">Påmeldt</span>' in html and '<span class="merke gul">Venteliste</span>' in html
    assert '<span class="merke bla">Ekstradeltaker</span>' in html
    assert "Påmeldte deltakere får bekreftelse på e-post" in html
    _uten_gammelt_ord(html, "bulk-forhåndsvisningen")


def test_min_side_og_kvitteringer_bruker_samme_navn(con):
    """Deltakerens egne sider: «Påmeldt» kommer fra samme sted (og bekreftelsen som sendes er uendret)."""
    tekst = (KURS / "web" / "templates" / "min_side.html").read_text(encoding="utf-8")
    assert "{{ STATUS.paameldt }}" in tekst              # deltakeren ser alltid «Påmeldt», også en ekstradeltaker
    gruppe = (KURS / "web" / "templates" / "kurs_gruppe_kvittering.html").read_text(encoding="utf-8")
    assert "{{ STATUS.paameldt }}" in gruppe
    assert (KURS / "maler" / "epost" / "bekreftelse.html").is_file()                       # e-postmalen er urørt


def test_maltekstene_beskriver_mottakerne_med_navnet():
    from kurs import maltekster
    assert maltekster.MALER["ukefor"].beskrivelse == ("Sendes til påmeldte deltakere en uke før kursstart. Ekstradeltakere får den "
                                                      "en uke før den første samlingen de er satt opp på.")
    assert maltekster.MALER["dagfor"].beskrivelse.startswith("Sendes til påmeldte deltakere dagen før hver kursdag")
    assert maltekster.MALER["dagfor"].beskrivelse.endswith("Ekstradeltakere får den bare dagen før kursdagene i samlingene de er satt opp på.")


# ============================ navnet følger db.PAAMELDINGSSTATUSER (ett sted) ============================

def test_endres_navnet_i_db_endres_det_overalt(con, monkeypatch):
    """Bytter vi ordet i db-tabellene, følger alle sider og eksporter med - og ingen viser «Påmeldt» lenger."""
    monkeypatch.setitem(NAVN, "paameldt", "Xstatus")
    monkeypatch.setitem(db.PAAMELDINGSSTATUSER_FLERTALL, "paameldt", "Xstatuser")
    monkeypatch.setitem(NAVN, "ekstradeltaker", "Yekstra")
    monkeypatch.setitem(db.PAAMELDINGSSTATUSER_FLERTALL, "ekstradeltaker", "Yekstraer")
    kid, pid = _kurs_med_alle_statuser(con)
    k = _admin()
    sider = {
        "deltakerlisten": f"/admin/kurs/{kid}/deltakere", "deltakervinduet": f"/admin/kurs/{kid}/deltaker/{pid['paameldt']}",
        "kursrapporten": "/admin/rapporter/kurs", "deltakerregisteret": "/admin/rapporter/deltakere",
        "rapporter": "/admin/rapporter", "oversikten": "/admin",
        "oppsettet": f"/admin/kurs/{kid}/oppsett",
    }
    for hva, adresse in sider.items():
        html = k.get(adresse).get_data(as_text=True)
        assert not re.findall(r"\bPåmeldte?\b|\bEkstradeltakere?\b", _synlig_tekst(html)), hva   # ordene kommer bare fra db-tabellene
        _uten_gammelt_ord(html, hva)
    assert 'Xstatus <span class="antall" data-antall="paameldt">(1)</span>' in k.get(sider["deltakerlisten"]).get_data(as_text=True)
    assert 'Yekstra <span class="antall" data-antall="ekstradeltaker">(1)</span>' in k.get(sider["deltakerlisten"]).get_data(as_text=True)
    assert '<span class="merke ok">Xstatus</span>' in k.get(sider["deltakervinduet"]).get_data(as_text=True)
    assert "Xstatus/kapasitet" in k.get(sider["kursrapporten"]).get_data(as_text=True)
    assert "Xstatuser i perioden" in k.get(sider["rapporter"]).get_data(as_text=True)
    assert "Send e-post til alle xstatuser" in k.get(sider["deltakerlisten"]).get_data(as_text=True)
    csv_ = _csv(k.get(f"/admin/kurs/{kid}/deltakere.csv").get_data(as_text=True))
    assert {r["Status"] for r in csv_} == {"Xstatus", "Yekstra", "Venteliste", "Avmeldt", "Avslått", "Utgått", "Forlatt"}
    kolonner = list(_csv(k.get("/admin/rapporter/kurs.csv").get_data(as_text=True))[0])
    assert kolonner[6] == "Xstatus" and kolonner[9] == "Yekstra"
    assert "Yekstra" in k.get(sider["kursrapporten"]).get_data(as_text=True)
    db.sett_paamelding_status(con, pid["venteliste"], "paameldt", aktor=ADMIN)
    con.commit()
    hva = [r.hva for r in hendelseslogg.for_paamelding(
        con, con.execute("SELECT * FROM paamelding WHERE id=?", (pid["venteliste"],)).fetchone(), systemadmin=False).rader]
    assert hva[0] == "Status endret fra venteliste til xstatus"
    r = k.post(f"/admin/kurs/{kid}/deltaker/{pid['avmeldt']}/status", data={"status": "paameldt",
               "neste": f"/admin/kurs/{kid}/deltakere"}, follow_redirects=True)
    assert f"{FORNAVN['avmeldt']} Test: Status endret til «xstatus»." in r.get_data(as_text=True)
    assert db.paameldingsstatus(con.execute("SELECT * FROM paamelding WHERE id=?", (pid["avmeldt"],)).fetchone()) == "paameldt"
    r = k.post(f"/admin/kurs/{kid}/deltaker/{pid['avslatt']}/status", data={"status": "ekstradeltaker",
               "neste": f"/admin/kurs/{kid}/deltakere"}, follow_redirects=True)
    assert f"{FORNAVN['avslatt']} Test: Status endret til «yekstra»." in r.get_data(as_text=True)


# ============================ databaseverdien satt inn via en variabel (funn fra kritikerne) ============================

def _meldinger(html: str) -> list[str]:
    return [m.strip() for m in re.findall(r'<div class="flash \w+" role="[a-z]+">(.*?)</div>', html, re.S)]


def test_manuell_registrering_sier_paamelt_og_venteliste_i_meldingen(con):
    """«Legg til deltaker»: meldingen bygges av statusen db.meld_paa returnerer (databaseverdien), og skal likevel si «påmeldt»."""
    kid = db.opprett_kurs(con, kode="ETI3", navn="Veiledning i gruppe", datoer=[date(2031, 10, 16).isoformat()],
                          sharepoint_mappe="Kurs/ETI3", pris_nok=1000, kapasitet=1, fakturering="person")
    con.commit()
    k = _admin()
    meldinger = []
    for fornavn in ("Mia", "Nora"):                                    # Mia får plassen, Nora havner på ventelisten
        r = k.post(f"/admin/kurs/{kid}/deltaker/ny", follow_redirects=True, data={
            "fornavn": fornavn, "etternavn": "Test", "epost": f"{fornavn.lower()}@eksempel.no", "betaler": "person",
            "betaling": "samlet", **ADRESSE})
        meldinger.append(_meldinger(r.get_data(as_text=True)))
        _uten_gammelt_ord(r.get_data(as_text=True), f"siden etter registreringen av {fornavn}")
    assert meldinger[0] == ["Mia Test er registrert og satt til «påmeldt». Ingen bekreftelse eller faktura er sendt ennå."]
    assert meldinger[1] == ["Nora Test er registrert og satt til «venteliste». Ingen bekreftelse eller faktura er sendt ennå."]


def test_spor_oss_forteller_deltakeren_status_med_samme_navn_som_resten(con):
    """Assistenten viser faktasetningen ordrett til deltakeren: «har status: påmeldt», ikke databaseverdien."""
    from kurs import assistent
    kid = db.opprett_kurs(con, kode="ETI4", navn="Assistentkurs", datoer=["2031-10-16"], sharepoint_mappe="Kurs/ETI4",
                          kapasitet=1)
    con.execute("INSERT INTO kunnskap (kategori, sporsmal, svar, godkjent) VALUES ('x', 'Hva koster kurset?', 'Se kurssiden.', 1)")
    for fornavn in ("Bente", "Vera"):                                  # Bente får plassen, Vera havner på ventelisten
        db.meld_paa(con, kid, epost=f"{fornavn.lower()}@eksempel.no", fornavn=fornavn, etternavn="Test")
    con.commit()
    for fornavn, forventet in (("Bente", "påmeldt"), ("Vera", "venteliste")):
        did = con.execute("SELECT id FROM deltaker WHERE epost=?", (f"{fornavn.lower()}@eksempel.no",)).fetchone()[0]
        setning = f"Din påmelding til «Assistentkurs» har status: {forventet}."
        assert setning in assistent._fakta(con, did, date(2031, 1, 1))
        svar = assistent.svar(con, "Er jeg påmeldt?", deltaker_id=did, idag=date(2031, 1, 1))
        assert svar.besvart and setning in svar.tekster, svar.tekster
        assert not any("status: bekreftet" in t for t in svar.tekster)


def test_ingen_tekst_setter_inn_en_statusvariabel_uten_a_slaa_opp_navnet():
    """Vokterettesten øverst finner bare ordet «bekreftet» skrevet i tekst. Denne finner tekst som setter inn en
    STATUSVARIABEL (f"… {status} …", f"… {p['status']} …"): da vises databaseverdien i stedet for navnet, slik
    «Legg til deltaker» og «Spør oss» gjorde. Slå opp navnet i db.PAAMELDINGSSTATUSER (og bruk .lower() midt i en setning)."""
    tillatt = {
        ("kurs/behandling.py", "rad['status']"): "feilmelding om en status som ikke skulle finnes (ingen visningsnavn)",
        ("kurs/db.py", "status"): "ValueError til utvikleren for e-postkopi, vises ikke for brukere",
        ("kurs/feil.py", "status"): "HTTP-status i en feiltekst",
    }
    mistenkelig = re.compile(r"^(status|ny_status|gammel|(p|r|rad|paamelding)\['status'\])$")
    funn = []
    for sti in sorted(KURS.rglob("*.py")):
        rel = sti.relative_to(ROT).as_posix()
        for node in ast.walk(ast.parse(sti.read_text(encoding="utf-8"))):
            if isinstance(node, ast.JoinedStr):
                for verdi in node.values:
                    if isinstance(verdi, ast.FormattedValue):
                        kode = ast.unparse(verdi.value)
                        if mistenkelig.match(kode) and (rel, kode) not in tillatt:
                            funn.append(f"{rel}:{node.lineno}: {{{kode}}}")
    assert not funn, "Statusvariabel satt inn i tekst (slå opp db.PAAMELDINGSSTATUSER):\n" + "\n".join(funn)
