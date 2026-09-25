"""Fase 12C4B: kursduplisering kopierer paameldingsskjemaets tilpasninger - som EGNE rader paa den NYE kurs-id-en.

Laaste krav:
  * `fra` valideres autoritativt (GET og POST): mangler -> vanlig nytt kurs; ugyldig -> 400; ukjent -> 404. Ugyldig blir
    aldri stille et vanlig kurs, og ingenting (heller ikke SharePoint) skjer foer valideringen.
  * Kildens skjemaoppsett leses NOYAKTIG EN gang (point-in-time), etter vanlig validering og FOER SharePoint.
  * SkjemaLesefeil -> 503, ingen SharePoint, ingen DB-endring, ingen fallback.
  * Kun gyldige, normaliserte egenskaper kopieres (via db.lagre_skjemafelt) - aldri raa rader/oppdatert/oppdatert_av.
  * Kurs, kursdager, skjemarader, materiell og hendelser lagres/rulles tilbake samlet.
Fiktive testdata."""
import json
import sqlite3
from datetime import date, timedelta

import pytest

from kurs import config, db
from kurs import skjemafelt as sf
from kurs.integrasjoner import sharepoint


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
def sp_kall(monkeypatch):
    """Teller SharePoint-kall (ekte demo-funksjon kjores fortsatt)."""
    kall, ekte = [], sharepoint.opprett_kursmappe

    def telle(kode):
        kall.append(kode)
        return ekte(kode)
    monkeypatch.setattr(sharepoint, "opprett_kursmappe", telle)
    return kall


@pytest.fixture
def admin():
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def _kilde(con, kode="KILDE", **kw):
    felter = {"navn": "Kildekurs", "type": "fysisk", "pris_nok": 4500, "fakturering": "person", "kapasitet": 10,
              "spesialistlop": "EFT", **kw}
    kid = db.opprett_kurs(con, kode=kode, datoer=["2099-03-02"], sharepoint_mappe=f"K/{kode}", **felter)
    con.commit()
    return kid


def _lagre(con, kid, felt, **e):
    db.lagre_skjemafelt(con, kid, felt, e)
    con.commit()


def _grunnlag(**over):
    data = {"navn": "Kopikurs", "type": "fysisk", "sted": "Eksempelsted", "kapasitet": "10",
            "datoer": (date.today() + timedelta(days=400)).isoformat(), "start_kl": "09:00", "slutt_kl": "16:00",
            "timer_pr_dag": "6", "pris_nok": "4500", "fakturering": "person", "betaling": "samlet",
            "faktura_dager_for": "14", "ansvarlig_admin_id": "", "paameldingsfrist": "", "kursholder_navn": "",
            "kursholder_epost": "", "materiell_frist": "", "notat": "", "spesialistlop": "EFT"}
    data.update(over)
    return {k: v for k, v in data.items() if v is not None}


def _dupliser(admin, kilde_id, **over):
    return admin.post(f"/admin/kurs/ny?fra={kilde_id}", data=_grunnlag(fra=str(kilde_id), **over))


def _nyeste_kurs(con):
    return con.execute("SELECT * FROM kurs ORDER BY id DESC LIMIT 1").fetchone()


def _over(con, kid):
    return dict(db.hent_skjemaoverstyringer(con, kid).overstyringer)


def _rader(con, kid):
    return [dict(r) for r in con.execute("SELECT * FROM kurs_skjemafelt WHERE kurs_id=? ORDER BY felt", (kid,))]


TABELLER = ("kurs", "kursdag", "kurs_skjemafelt", "materiell_krav", "hendelse", "deltaker", "paamelding",
            "sensitivt", "faktura", "faktura_forsok", "utsending_logg", "firmapaamelding", "innlogging_token")


def _telle(con):
    return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABELLER}


def _effektivt(con, kid):
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    return sf.effektivt_skjema(kurs, db.hent_skjemaoverstyringer(con, kid))


def _fullt_oppsett(con, kid):
    _lagre(con, kid, "telefon", synlig=False, rekkefolge=2)
    _lagre(con, kid, "arbeidssted", obligatorisk=True, label="Arbeidsgiver", hjelpetekst="Brukes på kursbeviset.",
           rekkefolge=1)
    _lagre(con, kid, "hpr_nr", hjelpetekst="Finnes i Helsepersonellregisteret.")


# ============================ 1, 9: vanlig nytt kurs uendret ============================

def test_vanlig_nytt_kurs_uten_fra_er_uendret(con, admin, sp_kall, monkeypatch):
    kid = _kilde(con)
    _fullt_oppsett(con, kid)
    les = []
    ekte = db.hent_skjemaoverstyringer
    monkeypatch.setattr(db, "hent_skjemaoverstyringer", lambda c, k: (les.append(k), ekte(c, k))[1])
    r = admin.post("/admin/kurs/ny", data=_grunnlag(), follow_redirects=True)
    ny = _nyeste_kurs(con)
    assert r.status_code == 200 and ny["id"] != kid and ny["status"] == "aapen"
    assert _rader(con, ny["id"]) == [] and les == [] and len(sp_kall) == 1
    assert "kunne ikke kopieres" not in r.get_data(as_text=True)


def test_vanlig_nytt_kurs_skjema_har_ingen_fra_nokkel(con, admin):
    assert 'name="fra"' not in admin.get("/admin/kurs/ny").get_data(as_text=True)


# ============================ 2-8: kopieringssemantikk ============================

def test_gyldig_duplisering_kopierer_alle_overstyringer_til_ny_kurs_id(con, admin, sp_kall):
    kid = _kilde(con)
    _fullt_oppsett(con, kid)
    r = _dupliser(admin, kid)
    ny = _nyeste_kurs(con)
    assert r.status_code == 302 and ny["id"] != kid and ny["status"] == "utkast" and len(sp_kall) == 1
    assert _over(con, ny["id"]) == _over(con, kid) == {
        "telefon": sf.Overstyring(synlig=False, rekkefolge=2),
        "arbeidssted": sf.Overstyring(obligatorisk=True, rekkefolge=1, label="Arbeidsgiver",
                                      hjelpetekst="Brukes på kursbeviset."),
        "hpr_nr": sf.Overstyring(hjelpetekst="Finnes i Helsepersonellregisteret.")}
    assert {r["kurs_id"] for r in _rader(con, ny["id"])} == {ny["id"]} and len(_rader(con, ny["id"])) == 3
    assert _effektivt(con, ny["id"]) == _effektivt(con, kid)
    assert [f.nokkel for f in _effektivt(con, ny["id"]).deltakerfelt] == ["arbeidssted", "hpr_nr"]


def test_kilde_uten_overstyringer_gir_null_rader(con, admin):
    kid = _kilde(con)
    _dupliser(admin, kid)
    assert _rader(con, _nyeste_kurs(con)["id"]) == []


@pytest.mark.parametrize("felt,egenskaper", [
    ("telefon", {"synlig": False}),
    ("arbeidssted", {"obligatorisk": True, "label": "Arbeidsgiver", "hjelpetekst": "Linje 1\nLinje 2"}),
    ("hpr_nr", {"hjelpetekst": "HPR-hjelp"}),
])
def test_enkeltoverstyringer_folger_med(con, admin, felt, egenskaper):
    kid = _kilde(con)
    _lagre(con, kid, felt, **egenskaper)
    _dupliser(admin, kid)
    assert _over(con, _nyeste_kurs(con)["id"]) == {felt: sf.Overstyring(**egenskaper)}


def test_rekkefolge_folger_med(con, admin):
    kid = _kilde(con)
    _lagre(con, kid, "telefon", rekkefolge=2)
    _lagre(con, kid, "arbeidssted", rekkefolge=1)
    _dupliser(admin, kid)
    ny = _nyeste_kurs(con)["id"]
    assert [f.nokkel for f in _effektivt(con, ny).deltakerfelt] == ["arbeidssted", "telefon", "hpr_nr"]


def test_hpr_hjelpetekst_folger_med_selv_om_kopien_ikke_har_spesialistlop(con, admin):
    kid = _kilde(con)
    _lagre(con, kid, "hpr_nr", hjelpetekst="HPR-hjelp")
    _dupliser(admin, kid, spesialistlop="")
    ny = _nyeste_kurs(con)
    assert ny["spesialistlop"] is None and _over(con, ny["id"]) == {"hpr_nr": sf.Overstyring(hjelpetekst="HPR-hjelp")}
    assert "hpr_nr" not in [f.nokkel for f in _effektivt(con, ny["id"]).deltakerfelt]


def test_redundante_standardverdier_materialiseres_ikke(con, admin):
    kid = _kilde(con)
    con.execute("INSERT INTO kurs_skjemafelt (kurs_id, felt, synlig, obligatorisk, rekkefolge, label, hjelpetekst) "
                "VALUES (?, 'telefon', 1, 0, 1, 'Telefon', '  ')", (kid,))
    con.commit()
    r = _dupliser(admin, kid)
    assert r.status_code == 302 and _rader(con, _nyeste_kurs(con)["id"]) == []


# ============================ korrupt kilde ============================

def _korrupt_kilde(con):
    kid = _kilde(con)
    con.execute("INSERT INTO kurs_skjemafelt (kurs_id, felt, label, hjelpetekst) "
                "VALUES (?, 'HEMMELIG_FELTNAVN', 'HEMMELIG-LABEL', 'HEMMELIG-HJELP')", (kid,))
    con.execute("INSERT INTO kurs_skjemafelt (kurs_id, felt, synlig, label, hjelpetekst) "
                "VALUES (?, 'hpr_nr', 0, 'HEMMELIG-HPR', 'Gyldig HPR-hjelp')", (kid,))
    # rekkefolge='abc' (feil type) kan bare lagres i SQLite - PostgreSQL er typesikker; der er kun label korrupt.
    con.execute("INSERT INTO kurs_skjemafelt (kurs_id, felt, obligatorisk, rekkefolge, label, oppdatert, oppdatert_av) "
                "VALUES (?, 'telefon', 1, ?, ?, '2000-01-01T00:00:00', 'admin:kilde')",
                (kid, None if db.er_postgres(con) else "abc", "HEMMELIG" + "x" * 90))
    con.execute("INSERT INTO kurs_skjemafelt (kurs_id, felt, label) VALUES (?, 'navn', 'HEMMELIG-NAVN')", (kid,))
    con.commit()
    return kid


def test_korrupt_kilde_kopierer_kun_gyldige_egenskaper(con, admin):
    kid = _korrupt_kilde(con)
    r = _dupliser(admin, kid)
    assert r.status_code == 302
    ny = _nyeste_kurs(con)["id"]
    rader = _rader(con, ny)
    assert [r["felt"] for r in rader] == ["hpr_nr", "telefon"]                      # ukjent og laast felt utelatt
    assert _over(con, ny) == {"hpr_nr": sf.Overstyring(hjelpetekst="Gyldig HPR-hjelp"),
                              "telefon": sf.Overstyring(obligatorisk=True)}         # ugyldige egenskaper utelatt
    assert all(r["oppdatert_av"] == f"admin:{config.ADMIN_BRUKERNAVN}" and r["oppdatert"] != "2000-01-01T00:00:00"
               for r in rader)                                                       # oppdatert(_av) kopieres ikke
    assert not db.hent_skjemaoverstyringer(con, ny).har_advarsler                   # kopien er ren
    assert "HEMMELIG" not in json.dumps(rader)


def test_korrupt_kilde_gir_trygg_advarsel(con, admin):
    kid = _korrupt_kilde(con)
    r = admin.post(f"/admin/kurs/ny?fra={kid}", data=_grunnlag(fra=str(kid)), follow_redirects=True)
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert ("Noen skjemainnstillinger på originalkurset kunne ikke kopieres. "
            "Kontroller Påmeldingsskjema på det nye kurset.") in html
    assert "HEMMELIG" not in html and "Gyldig HPR-hjelp" not in html


def test_ingen_advarsel_ved_ren_kilde(con, admin):
    kid = _kilde(con)
    _fullt_oppsett(con, kid)
    r = admin.post(f"/admin/kurs/ny?fra={kid}", data=_grunnlag(fra=str(kid)), follow_redirects=True)
    assert r.status_code == 200 and "er opprettet med kursnummer" in r.get_data(as_text=True)
    assert "kunne ikke kopieres" not in r.get_data(as_text=True)


# ============================ uavhengighet ============================

def test_kilde_og_kopi_er_uavhengige(con, admin):
    kid = _kilde(con)
    _fullt_oppsett(con, kid)
    _dupliser(admin, kid)
    ny = _nyeste_kurs(con)["id"]
    kilde_foer = _rader(con, kid)
    admin.post(f"/admin/kurs/{ny}/paameldingsskjema", data={
        "telefon_synlig": "on", "telefon_label": "Endret på kopien", "telefon_hjelpetekst": "",
        "arbeidssted_synlig": "on", "arbeidssted_label": "Arbeidssted", "arbeidssted_hjelpetekst": "",
        "hpr_nr_hjelpetekst": "", "rekkefolge": "telefon_forst"})
    assert _over(con, ny)["telefon"].label == "Endret på kopien" and _rader(con, kid) == kilde_foer
    admin.post(f"/admin/kurs/{ny}/paameldingsskjema/tilbakestill")
    assert _rader(con, ny) == [] and _rader(con, kid) == kilde_foer
    _dupliser(admin, kid)
    ny2 = _nyeste_kurs(con)["id"]
    kopi_foer = _rader(con, ny2)
    _lagre(con, kid, "telefon", label="Endret på kilden")
    assert _rader(con, ny2) == kopi_foer


# ============================ `fra`-validering ============================

@pytest.mark.parametrize("fra", ["", " ", "  7  ", "abc", "0", "-1", "+1", "1.5", "1e3", "01", "0x1", "١",
                                 str(2 ** 63), "9" * 40])
def test_ugyldig_fra_i_post_gir_400_uten_sideeffekter(con, admin, sp_kall, fra):
    _kilde(con)
    foer = _telle(con)
    r = admin.post("/admin/kurs/ny", data=_grunnlag(fra=fra))
    assert r.status_code == 400 and sp_kall == [] and _telle(con) == foer


def test_ukjent_kildekurs_gir_404_uten_sideeffekter(con, admin, sp_kall):
    _kilde(con)
    foer = _telle(con)
    for fra in ("9999", str(2 ** 63 - 1)):
        assert admin.post("/admin/kurs/ny", data=_grunnlag(fra=fra)).status_code == 404
    assert sp_kall == [] and _telle(con) == foer


@pytest.mark.parametrize("fra,status", [("abc", 400), ("", 400), ("0", 400), ("-1", 400), ("9" * 40, 400),
                                        ("9999", 404)])
def test_ugyldig_fra_i_get(con, admin, fra, status):
    _kilde(con)
    assert admin.get(f"/admin/kurs/ny?fra={fra}").status_code == status


def test_gyldig_fra_i_get_forhaandsutfyller_fortsatt(con, admin):
    kid = _kilde(con)
    html = admin.get(f"/admin/kurs/ny?fra={kid}").get_data(as_text=True)
    assert "basert på «Kildekurs»" in html and f'name="fra" value="{kid}"' in html


def test_krever_innlogging(con, sp_kall):
    from kurs.web import app as webapp
    kid = _kilde(con)
    foer = _telle(con)
    k = webapp.app.test_client()
    for r in (k.get(f"/admin/kurs/ny?fra={kid}"), k.post("/admin/kurs/ny", data=_grunnlag(fra=str(kid)))):
        assert r.status_code == 302 and "/admin/logg-inn" in r.headers["Location"]
    assert sp_kall == [] and _telle(con) == foer


# ============================ SkjemaLesefeil ============================

def test_skjemalesefeil_gir_503_uten_sharepoint_og_uten_db_endring(con, admin, sp_kall, monkeypatch):
    kid = _kilde(con)
    _fullt_oppsett(con, kid)
    foer = _telle(con)

    def feiler(c, kurs_id):
        raise sf.SkjemaLesefeil(kurs_id)
    monkeypatch.setattr(db, "hent_skjemaoverstyringer", feiler)
    r = _dupliser(admin, kid)
    html = r.get_data(as_text=True)
    assert r.status_code == 503
    assert ("Skjemainnstillingene til originalkurset kunne ikke leses akkurat nå. Kurset er ikke opprettet. "
            "Prøv igjen om litt.") in html
    assert sp_kall == [] and _telle(con) == foer
    for lekkasje in ("kurs_skjemafelt", "sqlite", "Traceback", "SkjemaLesefeil", "SELECT"):
        assert lekkasje not in html
    assert f'name="fra" value="{kid}"' in html                         # skjemaet vises igjen for samme kilde


def test_ekte_lesefeil_gir_503(con, admin, sp_kall):
    kid = _kilde(con)
    con.execute("DROP TABLE kurs_skjemafelt")
    con.commit()
    foer = _telle_uten_skjema(con)
    assert _dupliser(admin, kid).status_code == 503
    assert sp_kall == [] and _telle_uten_skjema(con) == foer


def _telle_uten_skjema(con):
    return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABELLER if t != "kurs_skjemafelt"}


# ============================ en lesing / point-in-time ============================

def test_kilden_leses_noyaktig_en_gang_foer_sharepoint(con, admin, sp_kall, monkeypatch):
    kid = _kilde(con)
    _fullt_oppsett(con, kid)
    hendelser = []
    ekte = db.hent_skjemaoverstyringer

    def telle(c, kurs_id):
        hendelser.append(("les", kurs_id, len(sp_kall)))
        return ekte(c, kurs_id)
    monkeypatch.setattr(db, "hent_skjemaoverstyringer", telle)
    assert _dupliser(admin, kid).status_code == 302
    assert hendelser == [("les", kid, 0)]                                  # en gang, og FOER SharePoint-kallet
    assert len(sp_kall) == 1


def test_point_in_time_snapshot_a_og_b(con, admin, monkeypatch):
    kid = _kilde(con)
    _lagre(con, kid, "telefon", label="A")
    ekte = db.hent_skjemaoverstyringer
    endre = {"til": {"label": "B"}}

    def les_og_endre(c, kurs_id):
        res = ekte(c, kurs_id)
        if endre["til"]:
            annen = db.koble(config.DB_STI)
            db.lagre_skjemafelt(annen, kurs_id, "telefon", endre.pop("til"))
            annen.commit()
            annen.close()
            endre["til"] = None
        return res
    monkeypatch.setattr(db, "hent_skjemaoverstyringer", les_og_endre)
    _dupliser(admin, kid, navn="Kopi A")
    kopi_a = _nyeste_kurs(con)["id"]
    assert _over(con, kopi_a)["telefon"].label == "A"                     # snapshot A
    assert _over(con, kid)["telefon"].label == "B"                        # kilden ble endret etter lesingen
    _dupliser(admin, kid, navn="Kopi B")
    assert _over(con, _nyeste_kurs(con)["id"])["telefon"].label == "B"    # neste duplisering: B


# ============================ atomisitet ============================

def test_db_feil_under_kopiering_ruller_tilbake_alt(con, admin, sp_kall, monkeypatch):
    kid = _kilde(con)
    _fullt_oppsett(con, kid)
    foer = _telle(con)
    ekte, kall = db.lagre_skjemafelt, []

    def feiler_paa_andre(*a, **kw):
        kall.append(a[2])
        if len(kall) == 2:
            raise sqlite3.IntegrityError("simulert")
        return ekte(*a, **kw)
    monkeypatch.setattr(db, "lagre_skjemafelt", feiler_paa_andre)
    r = _dupliser(admin, kid, kursholder_epost="k@eksempel.no", materiell_frist="2099-01-01")
    assert r.status_code == 200 and "Kunne ikke opprette kurs" in r.get_data(as_text=True)
    assert len(kall) == 2 and _telle(con) == foer                          # kurs/kursdag/skjema/materiell/hendelse
    assert len(sp_kall) == 1                                               # dokumentert: mappen ER laget (backlog)


# ============================ ingen andre data kopieres ============================

def test_ingen_deltaker_eller_historikk_kopieres(con, admin):
    kid = _kilde(con)
    _fullt_oppsett(con, kid)
    pid, _ = db.meld_paa(con, kid, epost="d@eksempel.no", navn="Deltaker", sensitivt={"allergier": "X"})
    db.logg(con, "test", {"x": 1})
    con.commit()
    foer = _telle(con)
    _dupliser(admin, kid)
    ny = _nyeste_kurs(con)["id"]
    etter = _telle(con)
    for t in ("deltaker", "paamelding", "sensitivt", "faktura", "faktura_forsok", "utsending_logg", "firmapaamelding",
              "innlogging_token"):
        assert etter[t] == foer[t], t
    assert con.execute("SELECT COUNT(*) FROM paamelding WHERE kurs_id=?", (ny,)).fetchone()[0] == 0
    assert (etter["kurs"], etter["kurs_skjemafelt"]) == (foer["kurs"] + 1, foer["kurs_skjemafelt"] + 3)


# ============================ logging ============================

def test_kopieringen_logges_med_admin_som_aktor_uten_tekstinnhold(con, admin):
    kid = _kilde(con)
    _lagre(con, kid, "telefon", label="HEMMELIG-LABEL", hjelpetekst="HEMMELIG-HJELP")
    _lagre(con, kid, "hpr_nr", hjelpetekst="HEMMELIG-HPR")
    siste = con.execute("SELECT MAX(id) FROM hendelse").fetchone()[0]
    _dupliser(admin, kid)
    ny = _nyeste_kurs(con)["id"]
    nye = [dict(r) for r in con.execute("SELECT * FROM hendelse WHERE id>? AND handling='skjemafelt_endret'", (siste,))]
    assert sorted(json.loads(h["detaljer"])["felt"] for h in nye) == ["hpr_nr", "telefon"]
    assert all(json.loads(h["detaljer"])["kurs_id"] == ny and h["aktor"] == f"admin:{config.ADMIN_BRUKERNAVN}"
               for h in nye)
    assert "HEMMELIG" not in json.dumps([dict(r) for r in con.execute("SELECT * FROM hendelse WHERE id>?", (siste,))])
