"""Fase 12C4A: adminflate for paameldingsskjemaet (/admin/kurs/<id>/paameldingsskjema).

Web-laget oversetter KUN det faste admin-skjemaet til komplette overstyringer for telefon, arbeidssted og HPR-hjelpetekst;
all validering skjer i 12C2-laget. Lagring er atomisk (alle felt i en transaksjon). Fiktive testdata."""
import json

import pytest

from kurs import config, db
from kurs import skjemafelt as sf


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _klient():
    from kurs.web import app as webapp
    return webapp.app.test_client()


@pytest.fixture
def admin():
    k = _klient()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def _kurs(con, kode="A1", **kw):
    felter = {"navn": "Eksempelkurs", "type": "fysisk", "pris_nok": 4500, "fakturering": "person", "kapasitet": 10, **kw}
    kid = db.opprett_kurs(con, kode=kode, datoer=["2099-03-02"], sharepoint_mappe=f"K/{kode}", **felter)
    con.commit()
    return kid


def _url(kid, ekstra=""):
    return f"/admin/kurs/{kid}/paameldingsskjema{ekstra}"


def _skjema(**over):
    """Komplett admin-skjema med standardverdier (slik siden sender det uendret). None fjerner en nokkel (avkrysning av)."""
    data = {"telefon_synlig": "on", "telefon_label": "Telefon", "telefon_hjelpetekst": "",
            "arbeidssted_synlig": "on", "arbeidssted_label": "Arbeidssted", "arbeidssted_hjelpetekst": "",
            "hpr_nr_hjelpetekst": "", "rekkefolge": "telefon_forst", **over}
    return {k: v for k, v in data.items() if v is not None}


def _rader(con, kid=None):
    sql = "SELECT * FROM kurs_skjemafelt" + (" WHERE kurs_id=?" if kid else "") + " ORDER BY felt"
    return [dict(r) for r in con.execute(sql, (kid,) if kid else ())]


def _over(con, kid):
    return dict(db.hent_skjemaoverstyringer(con, kid).overstyringer)


def _lagre(admin, kid, **over):
    return admin.post(_url(kid), data=_skjema(**over))


def _db_tilstand(con):
    return (_rader(con), [dict(r) for r in con.execute("SELECT * FROM hendelse ORDER BY id")])


# ============================ 1, 17: autentisering ============================

def test_krever_innlogging_for_visning_lagring_og_tilbakestilling(con):
    kid = _kurs(con)
    db.lagre_skjemafelt(con, kid, "telefon", {"label": "Mobil"})
    con.commit()
    foer = _db_tilstand(con)
    k = _klient()
    for r in (k.get(_url(kid)), k.post(_url(kid), data=_skjema(telefon_synlig=None, telefon_label="HACK")),
              k.post(_url(kid, "/tilbakestill"))):
        assert r.status_code == 302 and "/admin/logg-inn" in r.headers["Location"]
    assert _db_tilstand(con) == foer


def test_ukjent_kurs_gir_404(con, admin):
    assert admin.get(_url(9999)).status_code == 404
    assert admin.post(_url(9999), data=_skjema()).status_code == 404


# ============================ 2, 3, 23: visning ============================

def test_standardverdier_vises(con, admin):
    kid = _kurs(con)
    html = admin.get(_url(kid)).get_data(as_text=True)
    assert "Påmeldingsskjema" in html and "Eksempelkurs" in html
    assert 'name="telefon_synlig" checked' in html and 'name="arbeidssted_synlig" checked' in html
    assert 'name="telefon_obligatorisk" >' in html and 'name="arbeidssted_obligatorisk" >' in html
    assert 'name="telefon_label" maxlength="80" value="Telefon"' in html
    assert 'name="arbeidssted_label" maxlength="80" value="Arbeidssted"' in html
    assert 'value="telefon_forst" checked' in html
    assert html.index('name="telefon_label"') < html.index('name="arbeidssted_label"')
    for fast in ("Navn", "E-post", "Samtykke", "HPR-nummer"):
        assert f"<strong>{fast}</strong>" in html
    assert "Dette kurset har ikke spesialistløp" in html
    assert 'name="hpr_nr_synlig"' not in html and 'name="hpr_nr_label"' not in html and 'name="navn_label"' not in html
    for internt in ("override", "NULL", "rekkefolge</", "kurs_skjemafelt"):
        assert internt not in html


def test_eksisterende_overstyringer_vises(con, admin):
    kid = _kurs(con, spesialistlop="EFT")
    db.lagre_skjemafelt(con, kid, "telefon", {"synlig": False, "obligatorisk": True, "label": "Mobil",
                                              "hjelpetekst": "Linje 1\nLinje 2", "rekkefolge": 2})
    db.lagre_skjemafelt(con, kid, "arbeidssted", {"rekkefolge": 1})
    db.lagre_skjemafelt(con, kid, "hpr_nr", {"hjelpetekst": "HPR-hjelp"})
    con.commit()
    html = admin.get(_url(kid)).get_data(as_text=True)
    assert 'name="telefon_synlig" >' in html and 'name="telefon_obligatorisk" checked' in html
    assert 'value="Mobil"' in html and ">Linje 1\nLinje 2</textarea>" in html and ">HPR-hjelp</textarea>" in html
    assert 'value="arbeidssted_forst" checked' in html
    assert html.index('name="arbeidssted_label"') < html.index('name="telefon_label"')   # vist i effektiv rekkefolge
    assert "spesialistløpet «EFT», så feltet vises" in html
    assert html.count('<span class="merke gul">Tilpasset</span>') == 3


def test_forhaandsvisningslenken_peker_til_eksisterende_preview(con, admin):
    kid = _kurs(con)
    html = admin.get(_url(kid)).get_data(as_text=True)
    assert f'href="/admin/kurs/{kid}/forhandsvis-paamelding"' in html and "Forhåndsvis påmeldingsskjema" in html
    assert admin.get(f"/admin/kurs/{kid}/forhandsvis-paamelding").status_code == 200


def test_fanen_finnes_paa_kurssidene(con, admin):
    kid = _kurs(con)
    for side in ("oppsett", "nettside", "deltakere", "paameldingsskjema"):
        assert f'href="/admin/kurs/{kid}/paameldingsskjema">Påmeldingsskjema</a>' in admin.get(
            f"/admin/kurs/{kid}/{side}").get_data(as_text=True)


def test_alle_kursfaner_rendres_og_fanelenkene_peker_til_registrerte_ruter(con, admin):
    """Regresjon (BuildError i admin_kurs_faner.html): ALLE kurssider som inkluderer fanene skal faktisk rendres av
    Flask (200), og hver fanelenke skal treffe et registrert GET-endpoint - ikke bare finnes som tekst i malen."""
    import re

    from kurs.web import app as webapp
    kid = _kurs(con)
    adapter = webapp.app.url_map.bind("localhost")
    forventet = {"Oppsett": "admin_kurs_oppsett", "Nettside": "admin_kurs_nettside",
                 "Påmeldingsskjema": "admin_kurs_paameldingsskjema", "Deltakere": "admin_kurs_deltakere",
                 "Kommunikasjon": "admin_kurs_kommunikasjon"}
    for side in ("oppsett", "nettside", "paameldingsskjema", "deltakere", "kommunikasjon"):
        r = admin.get(f"/admin/kurs/{kid}/{side}")
        assert r.status_code == 200, side
        faner = r.get_data(as_text=True).split('<nav class="faner">')[1].split("</nav>")[0]
        lenker = dict((tekst, href) for href, tekst in re.findall(r'href="([^"]+)">([^<]+)</a>', faner))
        assert set(lenker) == set(forventet), side
        for tekst, endpoint in forventet.items():
            assert adapter.match(lenker[tekst], method="GET") == (endpoint, {"kurs_id": kid}), (side, tekst)


# ============================ 20: GET skriver aldri ============================

def test_get_skriver_aldri_til_databasen(con, admin):
    kid = _kurs(con, spesialistlop="EFT")
    con.execute("INSERT INTO kurs_skjemafelt (kurs_id, felt, label) VALUES (?, 'ukjent', 'x')", (kid,))
    con.commit()
    foer = _db_tilstand(con)
    assert admin.get(_url(kid)).status_code == 200
    assert _db_tilstand(con) == foer


# ============================ 4-9: lagring ============================

@pytest.mark.parametrize("felt", ["telefon", "arbeidssted"])
def test_felt_kan_skjules(con, admin, felt):
    kid = _kurs(con)
    r = _lagre(admin, kid, **{f"{felt}_synlig": None})
    assert r.status_code == 302 and r.headers["Location"].endswith(_url(kid))
    assert _over(con, kid) == {felt: sf.Overstyring(synlig=False)}


@pytest.mark.parametrize("felt", ["telefon", "arbeidssted"])
def test_felt_kan_gjores_obligatorisk(con, admin, felt):
    kid = _kurs(con)
    _lagre(admin, kid, **{f"{felt}_obligatorisk": "on"})
    assert _over(con, kid) == {felt: sf.Overstyring(obligatorisk=True)}


def test_label_og_hjelpetekst_kan_endres(con, admin):
    kid = _kurs(con)
    _lagre(admin, kid, telefon_label="  Mobilnummer ", telefon_hjelpetekst="Linje 1\r\nLinje 2",
           arbeidssted_label="Arbeidsgiver", arbeidssted_hjelpetekst="Hjelp")
    assert _over(con, kid) == {"telefon": sf.Overstyring(label="Mobilnummer", hjelpetekst="Linje 1\nLinje 2"),
                               "arbeidssted": sf.Overstyring(label="Arbeidsgiver", hjelpetekst="Hjelp")}
    r = admin.get(_url(kid))
    assert "Påmeldingsskjemaet er lagret." in r.get_data(as_text=True)


def test_skjult_og_obligatorisk_bevares(con, admin):
    kid = _kurs(con)
    _lagre(admin, kid, telefon_synlig=None, telefon_obligatorisk="on")
    assert _over(con, kid)["telefon"] == sf.Overstyring(synlig=False, obligatorisk=True)
    html = admin.get(_url(kid)).get_data(as_text=True)
    assert 'name="telefon_synlig" >' in html and 'name="telefon_obligatorisk" checked' in html


# ============================ 10, 11: rekkefolge ============================

def test_rekkefolge_kan_byttes_og_tilbake(con, admin):
    kid = _kurs(con)
    _lagre(admin, kid, rekkefolge="arbeidssted_forst")
    assert _over(con, kid) == {"telefon": sf.Overstyring(rekkefolge=2), "arbeidssted": sf.Overstyring(rekkefolge=1)}
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    assert [f.nokkel for f in sf.effektivt_skjema(kurs, _over(con, kid)).deltakerfelt] == ["arbeidssted", "telefon"]
    _lagre(admin, kid, rekkefolge="telefon_forst")
    assert _rader(con, kid) == []


def test_rekkefolgebytte_mister_ingen_andre_innstillinger(con, admin):
    kid = _kurs(con, spesialistlop="EFT")
    oppsett = dict(telefon_synlig=None, telefon_obligatorisk="on", telefon_label="Mobil", telefon_hjelpetekst="T-hjelp",
                   arbeidssted_obligatorisk="on", arbeidssted_label="Arbeidsgiver", arbeidssted_hjelpetekst="A-hjelp",
                   hpr_nr_hjelpetekst="H-hjelp")
    _lagre(admin, kid, **oppsett)
    _lagre(admin, kid, **oppsett, rekkefolge="arbeidssted_forst")
    assert _over(con, kid) == {
        "telefon": sf.Overstyring(False, True, 2, "Mobil", "T-hjelp"),
        "arbeidssted": sf.Overstyring(None, True, 1, "Arbeidsgiver", "A-hjelp"),
        "hpr_nr": sf.Overstyring(hjelpetekst="H-hjelp")}


@pytest.mark.parametrize("rekkefolge", [None, "", "hpr_forst", "1", "telefon_forst "])
def test_ugyldig_eller_manglende_rekkefolge_avvises_uten_lagring(con, admin, rekkefolge):
    kid = _kurs(con)
    foer = _db_tilstand(con)
    r = _lagre(admin, kid, rekkefolge=rekkefolge, telefon_label="Mobil")
    assert r.status_code == 400 and "Velg rekkefølge" in r.get_data(as_text=True)
    assert _db_tilstand(con) == foer


# ============================ 12, 13: HPR ============================

def test_hpr_kun_hjelpetekst_kan_endres(con, admin):
    kid = _kurs(con, spesialistlop="EFT")
    _lagre(admin, kid, hpr_nr_hjelpetekst="Finnes i Helsepersonellregisteret.")
    assert _over(con, kid) == {"hpr_nr": sf.Overstyring(hjelpetekst="Finnes i Helsepersonellregisteret.")}
    _lagre(admin, kid, hpr_nr_hjelpetekst="   ")
    assert _rader(con, kid) == []


def test_hpr_laasene_kan_ikke_omgaas_med_manipulert_post(con, admin):
    kid = _kurs(con, spesialistlop="EFT")
    manip = {"hpr_nr_synlig": "", "hpr_nr_obligatorisk": "on", "hpr_nr_label": "HACK", "hpr_nr_rekkefolge": "0",
             "rekkefolge": "telefon_forst"}
    assert _lagre(admin, kid, **manip).status_code == 302
    assert _rader(con, kid) == []
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    s = sf.effektivt_skjema(kurs, _over(con, kid))
    assert [f.nokkel for f in s.deltakerfelt] == ["telefon", "arbeidssted", "hpr_nr"]
    assert s.deltakerfelt[-1] == sf.EffektivtFelt("hpr_nr", "HPR-nummer", False)


# ============================ 14, 15: laaste felt / ukjent ============================

def test_laaste_felt_systemblokker_og_ukjent_kan_ikke_injiseres(con, admin):
    kid = _kurs(con)
    manip = {f"{felt}_{e}": v for felt in ("navn", "epost", "samtykke", "allergier", "tilrettelegging", "betaler",
                                           "faktura", "eget_felt")
             for e, v in (("synlig", ""), ("obligatorisk", "on"), ("label", "HACK"), ("hjelpetekst", "HACK"))}
    manip |= {"felt": "navn", "egenskap": "label", "telefon_placeholder": "HACK", "telefon_type": "email",
              "kurs_id": "1", "oppdatert_av": "HACK"}
    assert _lagre(admin, kid, **manip).status_code == 302
    assert _rader(con, kid) == []
    assert "HACK" not in json.dumps([dict(r) for r in con.execute("SELECT * FROM hendelse")])


# ============================ 16: standardverdier rydder ============================

def test_standardverdier_via_admin_fjerner_alle_rader(con, admin):
    kid = _kurs(con, spesialistlop="EFT")
    _lagre(admin, kid, telefon_synlig=None, telefon_obligatorisk="on", telefon_label="Mobil", telefon_hjelpetekst="H",
           arbeidssted_label="X", hpr_nr_hjelpetekst="H", rekkefolge="arbeidssted_forst")
    assert len(_rader(con, kid)) == 3
    r = _lagre(admin, kid, telefon_label="", arbeidssted_label="  Arbeidssted ", arbeidssted_hjelpetekst=" \n ")
    assert r.status_code == 302 and _rader(con, kid) == []


def test_uendret_lagring_gir_ingen_skriving(con, admin):
    kid = _kurs(con)
    _lagre(admin, kid, telefon_label="Mobil")
    foer = _db_tilstand(con)
    _lagre(admin, kid, telefon_label="Mobil")
    assert _db_tilstand(con) == foer
    assert "Ingen endringer å lagre." in admin.get(_url(kid)).get_data(as_text=True)


# ============================ 17: tilbakestill ============================

def test_tilbakestill_hele_skjemaet(con, admin):
    kid, annet = _kurs(con, spesialistlop="EFT"), _kurs(con, "A2")
    _lagre(admin, kid, telefon_synlig=None, arbeidssted_label="X", hpr_nr_hjelpetekst="H", rekkefolge="arbeidssted_forst")
    _lagre(admin, annet, telefon_label="Annet kurs")
    con.execute("INSERT INTO kurs_skjemafelt (kurs_id, felt, label) VALUES (?, 'ukjent', 'x')", (kid,))
    con.commit()
    r = admin.post(_url(kid, "/tilbakestill"))
    assert r.status_code == 302
    assert _rader(con, kid) == [] and len(_rader(con, annet)) == 1
    html = admin.get(_url(kid)).get_data(as_text=True)
    assert "tilbakestilt til standard" in html and 'value="Telefon"' in html and 'name="telefon_synlig" checked' in html
    assert "kunne ikke brukes" not in html


def test_tilbakestill_er_kun_post_med_bekreftelse(con, admin):
    kid = _kurs(con)
    assert admin.get(_url(kid, "/tilbakestill")).status_code == 405
    html = admin.get(_url(kid)).get_data(as_text=True)
    assert f'action="/admin/kurs/{kid}/paameldingsskjema/tilbakestill"' in html
    assert 'data-bekreft="Tilbakestille påmeldingsskjemaet' in html


# ============================ 18: korrupt overstyring ============================

def test_korrupt_overstyring_gir_trygg_advarsel(con, admin):
    kid = _kurs(con, spesialistlop="EFT")
    con.execute("INSERT INTO kurs_skjemafelt (kurs_id, felt, label) VALUES (?, 'HEMMELIG_FELT', 'HEMMELIG')", (kid,))
    con.execute("INSERT INTO kurs_skjemafelt (kurs_id, felt, synlig, label) VALUES (?, 'hpr_nr', 0, 'HEMMELIG2')", (kid,))
    con.execute("INSERT INTO kurs_skjemafelt (kurs_id, felt, obligatorisk, label) VALUES (?, 'telefon', 1, ?)",
                (kid, "HEMMELIG3" + "x" * 90))
    con.commit()
    r = admin.get(_url(kid))
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "Noen lagrede skjemainnstillinger kunne ikke brukes" in html
    for linje in ("<li>Ukjent felt: ukjent felt</li>", "<li>HPR-nummer – vis feltet: kan ikke endres for dette feltet</li>",
                  "<li>HPR-nummer – ledetekst: kan ikke endres for dette feltet</li>",
                  "<li>Telefon – ledetekst: for lang tekst</li>"):
        assert linje in html
    assert "HEMMELIG" not in html
    assert 'name="telefon_obligatorisk" checked' in html and 'value="Telefon"' in html   # gyldig del + standard


def test_lagring_rydder_korrupte_verdier_for_redigerbare_felt(con, admin):
    kid = _kurs(con)
    if db.er_postgres(con):     # typesikker kolonne / ingen NUL i PostgreSQL: kun kontrolltegn-korrupsjon er mulig
        con.execute("INSERT INTO kurs_skjemafelt (kurs_id, felt, label) VALUES (?, 'telefon', ?)", (kid, "\x01"))
    else:
        con.execute("INSERT INTO kurs_skjemafelt (kurs_id, felt, rekkefolge, label) VALUES (?, 'telefon', 'abc', ?)",
                    (kid, "\x00"))
    con.commit()
    assert _lagre(admin, kid).status_code == 302
    assert _rader(con, kid) == []
    assert not db.hent_skjemaoverstyringer(con, kid).har_advarsler


# ============================ 19: DB-lesefeil ============================

def _bryt(con):
    con.execute("DROP TABLE kurs_skjemafelt")
    con.commit()


def test_lesefeil_gir_503_uten_redigerbart_skjema(con, admin):
    kid = _kurs(con)
    _bryt(con)
    r = admin.get(_url(kid))
    html = r.get_data(as_text=True)
    assert r.status_code == 503
    assert "Skjemainnstillingene kunne ikke leses akkurat nå. Ingen endringer er gjort." in html
    assert "<form" not in html.split('<nav class="faner">')[1] and 'name="telefon_label"' not in html
    for lekkasje in ("kurs_skjemafelt", "no such table", "Traceback", "sqlite", "SELECT"):
        assert lekkasje not in html


def test_lesefeil_ved_post_lagrer_ingenting(con, admin, monkeypatch):
    kid = _kurs(con)
    kall = []

    def feiler(c, kurs_id):
        raise sf.SkjemaLesefeil(kurs_id)
    monkeypatch.setattr(db, "hent_skjemaoverstyringer", feiler)
    monkeypatch.setattr(db, "lagre_skjemafelt", lambda *a, **kw: kall.append(a))
    foer = _db_tilstand(con)
    r = _lagre(admin, kid, telefon_label="Mobil")
    assert r.status_code == 503 and kall == [] and _db_tilstand(con) == foer


def test_skrivefeil_ved_lagring_og_tilbakestilling_gir_503(con, admin, monkeypatch):
    kid = _kurs(con)
    _lagre(admin, kid, telefon_label="Mobil")
    ekte = db.hent_skjemaoverstyringer

    def les_og_bryt(c, k):          # lesing OK, men tabellen forsvinner foer skrivingen -> kontrollert 503
        res = ekte(c, k)
        c.execute("DROP TABLE kurs_skjemafelt")
        c.commit()                  # varig i begge databaser (PostgreSQL har transaksjonell DDL; SQLite autocommit-er DDL)
        return res
    monkeypatch.setattr(db, "hent_skjemaoverstyringer", les_og_bryt)
    r = _lagre(admin, kid, telefon_label="Ny")
    assert r.status_code == 503 and "Ingen endringer er gjort." in r.get_data(as_text=True)
    monkeypatch.setattr(db, "hent_skjemaoverstyringer", ekte)
    assert admin.post(_url(kid, "/tilbakestill")).status_code == 503


# ============================ 21: atomisk lagring ============================

@pytest.mark.parametrize("ugyldig", [dict(arbeidssted_label="x" * 81), dict(arbeidssted_hjelpetekst="x" * 301),
                                     dict(arbeidssted_label="A\tB"), dict(hpr_nr_hjelpetekst="\x00"),
                                     dict(hpr_nr_hjelpetekst="x" * 301)])
def test_ugyldig_verdi_gir_ingen_delvis_lagring(con, admin, ugyldig):
    kid = _kurs(con)
    _lagre(admin, kid, telefon_label="Gammel")
    foer = _db_tilstand(con)
    r = _lagre(admin, kid, telefon_label="Ny", telefon_synlig=None, **ugyldig)   # telefon er gyldig, resten ikke
    html = r.get_data(as_text=True)
    assert r.status_code == 400 and "Ingen endringer er lagret." in html
    assert _db_tilstand(con) == foer                                              # telefon er IKKE lagret
    assert 'value="Ny"' in html and 'name="telefon_synlig" >' in html              # innsendte verdier vises igjen


# ============================ 22: HTML/Jinja ============================

def test_html_og_jinja_lagres_som_tekst_og_escapes_overalt(con, admin):
    kid = _kurs(con, spesialistlop="EFT")
    farlig = "<script>alert(1)</script> {{ 7*7 }} <b>tekst</b>"
    _lagre(admin, kid, telefon_label=farlig[:80], telefon_hjelpetekst=farlig, hpr_nr_hjelpetekst=farlig)
    assert _over(con, kid)["telefon"].hjelpetekst == farlig
    escapet = "&lt;script&gt;alert(1)&lt;/script&gt; {{ 7*7 }} &lt;b&gt;tekst&lt;/b&gt;"
    sider = [admin.get(_url(kid)), admin.get(f"/admin/kurs/{kid}/forhandsvis-paamelding"), _klient().get("/kurs/A1")]
    for r in sider:
        html = r.get_data(as_text=True)
        assert escapet in html and "<script>alert(1)</script>" not in html and "<b>tekst</b>" not in html


# ============================ 24: admin -> offentlig -> POST ============================

def test_admin_til_offentlig_skjema_til_post(con, admin):
    kid = _kurs(con)
    assert _lagre(admin, kid, telefon_label="Mobilnummer", telefon_obligatorisk="on",
                  telefon_hjelpetekst="Nummer vi kan nå deg på", arbeidssted_synlig=None).status_code == 302
    k = _klient()
    html = k.get("/kurs/A1").get_data(as_text=True)
    assert "<label>Mobilnummer *</label>" in html and "Nummer vi kan nå deg på" in html
    assert 'name="arbeidssted"' not in html
    basis = {"navn": "Test Person", "epost": "test@eksempel.no", "samtykke": "on", "arbeidssted": "MANIPULERT"}
    r = k.post("/kurs/A1", data=basis)
    assert r.status_code == 400 and "Fyll inn «Mobilnummer»." in r.get_data(as_text=True)
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 0
    assert k.post("/kurs/A1", data={**basis, "telefon": "12345678"}).status_code == 200
    d = con.execute("SELECT * FROM deltaker WHERE epost='test@eksempel.no'").fetchone()
    assert (d["telefon"], d["arbeidssted"]) == ("12345678", None)


def test_lagring_logger_uten_tekstinnhold(con, admin):
    kid = _kurs(con)
    _lagre(admin, kid, telefon_label="HEMMELIG-LABEL", telefon_hjelpetekst="HEMMELIG-HJELP")
    logg = [(r["handling"], r["aktor"], r["detaljer"]) for r in
            con.execute("SELECT * FROM hendelse WHERE handling LIKE 'skjema%'")]
    assert logg and all(a == f"admin:{config.ADMIN_BRUKERNAVN}" for _, a, _ in logg)
    assert "HEMMELIG" not in json.dumps(logg)
