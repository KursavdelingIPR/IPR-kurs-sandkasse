"""Fase 12B2C-1: bekreftelsesmailen koblet til maltekstsystemet + KORREKT, LAAST fakturainformasjon.

Redigerbar tekst (emne, innledning, avslutning) kommer fra maltekstsystemet. Fakturainformasjonen er en LAAST systemblokk i
malfilen, bygget fra samme FakturaPlan som fakturamotoren (sveiper._lag_plan) - aldri fra admintekst. Bekreftelsen sendes FOER
fakturaforsoket og paastaar derfor aldri at faktura er sendt/opprettet.
"""
import re
from datetime import date

import pytest

from kurs import config, db, maltekster, sveiper
from kurs.integrasjoner import epost, visma
from kurs.kjoring import Kjoring
from kurs.maltekster import KODER, MALER, MANGLER_VERDI, MalFeil
from kurs.sveiper import PLAN_INGEN, PLAN_MANGLER_KURSDAG, PLAN_NA, PLAN_PER_SAMLING, PLAN_UTSATT, FakturaPlan

FORSTE = date(2027, 9, 20)          # grense: 2027-03-20
GRENSE = date(2027, 3, 20)
FAR = date(2027, 1, 1)              # langt foer grensen


def _n(html):
    return " ".join(html.split())


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
def ut(monkeypatch):
    """Fanger utgaaende e-post (til, emne, html) og Visma-kall, i den REKKEFOLGEN de skjer."""
    kall = {"epost": [], "visma": [], "logg": []}
    monkeypatch.setattr(epost, "send", lambda til, emne, html, *a, **kw: (kall["logg"].append("send"), kall["epost"].append((til, emne, html))))
    ekte = visma.fakturer

    def v(g):
        kall["logg"].append("visma")
        kall["visma"].append(g.belop_nok)
        return ekte(g)
    monkeypatch.setattr(visma, "fakturer", v)
    return kall


def _kurs(con, kode="B1", start=FORSTE, ant_dager=1, **kw):
    datoer = [date.fromordinal(start.toordinal() + 30 * n).isoformat() for n in range(ant_dager)] if start else []
    kid = db.opprett_kurs(con, kode=kode, navn="Veiledning i praksis", datoer=datoer, sharepoint_mappe="Kurs/" + kode,
                          **{"pris_nok": 1000, "fakturering": "person", "type": "fysisk", "sted": "Oslo", **kw})
    con.commit()
    return kid


def _paamelding(con, kid, epost_="ola@x.no", **pm):
    pid, _ = db.meld_paa(con, kid, epost=epost_, fornavn="Ola", etternavn="Nordmann", paamelding=pm or None)
    con.commit()
    return pid


def _kjor(con, idag, pid=None):
    sveiper.kjor(Kjoring(con, idag=idag), pid)
    con.commit()


def _mail(ut, til="ola@x.no"):
    (m,) = [m for m in ut["epost"] if m[0] == til]
    return m[1], _n(m[2])


def _antall(con, tabell):
    return con.execute(f"SELECT COUNT(*) FROM {tabell}").fetchone()[0]


def _tilstand(con, pid):
    return tuple(con.execute("SELECT sveiper_kjort, faktura_tidligst_dato FROM paamelding WHERE id=?", (pid,)).fetchone())


def _lagre(con, felt, tekst):
    maltekster.lagre_maltekst(con, "bekreftelse", felt, tekst, aktor="admin:test")
    con.commit()


def _korrupt(con, felt="innledning", tekst="Hei {ukjent_kode}"):
    con.execute("INSERT INTO mal_tekst (mal, felt, tekst) VALUES ('bekreftelse', ?, ?) "
                "ON CONFLICT (mal, felt) DO UPDATE SET tekst=excluded.tekst", (felt, tekst))
    con.commit()


SAMLET_NA = "Faktura på 1000 kr sendes separat til deg."
SAMLET_UTSATT = "Faktura på 1000 kr sendes separat til deg, tidligst 20.03.2027."


# ============================ 16. fakturatekst gjennom den ekte flyten ============================

def test_a_samlet_utsatt_sier_tidligst_dato_og_ingen_faktura_er_forsoekt(con, ut):
    kid = _kurs(con)
    pid = _paamelding(con, kid)
    _kjor(con, FAR, pid)
    emne, html = _mail(ut)
    assert emne == "Bekreftelse: Veiledning i praksis"
    assert SAMLET_UTSATT in html and "tidligst 20.03.2027" in html
    assert "2027-03-20" not in html                                            # DD.MM.YYYY, ikke ISO
    assert ut["visma"] == [] and _antall(con, "faktura") == 0 and _antall(con, "faktura_forsok") == 0
    assert _tilstand(con, pid) == (1, "2027-03-20")                            # lovlig hold (3B): datoen i mailen = lagret dato


def test_b_grensedato_sier_faktura_sendes_separat_uten_tidligst(con, ut):
    kid = _kurs(con)
    pid = _paamelding(con, kid)
    _kjor(con, GRENSE, pid)
    _, html = _mail(ut)
    assert SAMLET_NA in html and "tidligst" not in html
    assert "Faktura er sendt" not in html and "Faktura er opprettet" not in html
    assert ut["visma"] == [1000]


def test_c_faktura_naa_sier_faktura_sendes_separat_uten_tidligst(con, ut):
    kid = _kurs(con)
    pid = _paamelding(con, kid, faktura_onskes_na=1)
    _kjor(con, FAR, pid)
    _, html = _mail(ut)
    assert SAMLET_NA in html and "tidligst" not in html


def test_d_mangler_kursdag_sier_faktura_sendes_separat_uten_dato_og_uten_intern_feiltekst(con, ut):
    kid = _kurs(con, start=None)
    pid = _paamelding(con, kid, faktura_onskes_na=1)
    _kjor(con, FAR, pid)
    _, html = _mail(ut)
    assert SAMLET_NA in html and "tidligst" not in html
    assert not re.search(r"\d\d\.\d\d\.\d{4}", html) and not re.search(r"\d{4}-\d\d-\d\d", html)   # ingen oppdiktet dato
    for intern in ("mangler_kursdag", "kursdag", "konfigurasjon", "feil", "Traceback"):
        assert intern not in html.replace("kursdager", ""), intern
    assert ut["visma"] == [] and _antall(con, "faktura") == 0 and _antall(con, "faktura_forsok") == 0
    assert _tilstand(con, pid) == (0, None)                                    # 3B: konfigurasjonsfeilen skjules ikke (sveiper_kjort=0)


def test_e_per_samling_beholder_delfakturateksten_uten_seksmaanedersdato(con, ut):
    kid = _kurs(con, ant_dager=2, betaling="per_samling", faktura_dager_for=30)
    pid = _paamelding(con, kid)
    _kjor(con, FAR, pid)
    _, html = _mail(ut)
    assert "Kursavgiften på 1000 kr deles på 2 samlinger, og faktura for hver samling sendes 30 dager før samlingen til deg." in html
    assert "tidligst" not in html and "sendes separat" not in html and "Faktura på" not in html and "20.03.2027" not in html


@pytest.mark.parametrize("kw", [{"pris_nok": 0}, {"fakturering": "ingen"}, {"fakturering": "organisasjon"}], ids=["gratis", "ingen", "organisasjon"])
def test_f_g_h_ingen_automatisk_deltakerfaktura_gir_ingen_fakturatekst(con, ut, kw):
    kid = _kurs(con, **kw)
    pid = _paamelding(con, kid)
    _kjor(con, FAR, pid)
    _, html = _mail(ut)
    for misvisende in ("Faktura på", "sendes separat", "Kursavgiften", "faktura for hver samling", "tidligst"):
        assert misvisende not in html, misvisende
    assert "Takk for påmeldingen!" in html and "Sted: Oslo." in html            # resten av mailen er som vanlig
    assert _tilstand(con, pid)[0] == 1


def test_i_personfakturering_med_arbeidsgiver_som_betaler_faar_seksmaanedersregelen_og_riktig_mottaker(con, ut):
    kid = _kurs(con)
    pid = _paamelding(con, kid, betaler="organisasjon", org_navn="Firma AS")
    _kjor(con, FAR, pid)
    _, html = _mail(ut)
    assert "Faktura på 1000 kr sendes separat til Firma AS, tidligst 20.03.2027." in html
    assert _antall(con, "faktura") == 0


@pytest.mark.parametrize("idag,onskes,forventet", [
    (FAR, 0, "Faktura på 1000 kr sendes separat til Firma AS, tidligst 20.03.2027."),
    (GRENSE, 0, "Faktura på 1000 kr sendes separat til Firma AS."),
    (FAR, 1, "Faktura på 1000 kr sendes separat til Firma AS."),
], ids=["utsatt", "na", "faktura_naa"])
def test_arbeidsgiver_som_mottaker_i_alle_samlet_varianter(con, ut, idag, onskes, forventet):
    kid = _kurs(con)
    pid = _paamelding(con, kid, betaler="organisasjon", org_navn="Firma AS", faktura_onskes_na=onskes)
    _kjor(con, idag, pid)
    _, html = _mail(ut)
    assert forventet in html
    assert "til deg" not in html.split("Faktura på")[1].split("</p>")[0]
    assert ("tidligst" in html) is ("tidligst" in forventet)


def test_mangler_kursdag_med_arbeidsgiver_har_mottaker_men_ingen_dato(con, ut):
    kid = _kurs(con, start=None)
    pid = _paamelding(con, kid, betaler="organisasjon", org_navn="Firma AS")
    _kjor(con, FAR, pid)
    _, html = _mail(ut)
    assert "Faktura på 1000 kr sendes separat til Firma AS." in html and "tidligst" not in html
    assert not re.search(r"\d\d\.\d\d\.\d{4}", html)


@pytest.mark.parametrize("dager_for,tekst,ikke", [(0, "0 dager før samlingen", "0 dag før"), (1, "1 dag før samlingen", "1 dager"),
                                                  (2, "2 dager før samlingen", "2 dag før")])
def test_per_samling_0_1_og_flere_dager_gjennom_ekte_flyt(con, ut, dager_for, tekst, ikke):
    kid = _kurs(con, ant_dager=2, betaling="per_samling", faktura_dager_for=dager_for)
    pid = _paamelding(con, kid)
    _kjor(con, FAR, pid)
    _, html = _mail(ut)
    assert f"faktura for hver samling sendes {tekst} til deg." in html and ikke not in html
    assert "tidligst" not in html and "sendes separat" not in html
    lagret = con.execute("SELECT faktura_dager_for FROM kurs WHERE id=?", (kid,)).fetchone()[0]
    assert lagret == dager_for                                                # faktura_dager_for-logikken er uendret


def test_alle_planmodus_har_en_definert_fakturablokk_og_ingen_faller_stille_gjennom():
    modus = {v for k, v in vars(sveiper).items() if k.startswith("PLAN_")}
    assert modus == {PLAN_INGEN, PLAN_PER_SAMLING, PLAN_NA, PLAN_UTSATT, PLAN_MANGLER_KURSDAG}
    forventet = {PLAN_INGEN: None, PLAN_PER_SAMLING: "faktura for hver samling", PLAN_NA: "Faktura på 1000 kr sendes separat til deg.",
                 PLAN_UTSATT: "Faktura på 1000 kr sendes separat til deg, tidligst 20.03.2027.",
                 PLAN_MANGLER_KURSDAG: "Faktura på 1000 kr sendes separat til deg."}
    d = dict(p={"navn": "Ola Test", "fornavn": "Ola", "betaler": "person", "org_navn": None}, kurs={"navn": "K", "type": "fysisk", "sted": "Oslo", "pris_nok": 1000,
             "start_kl": "09:00", "slutt_kl": "16:00", "faktura_dager_for": 14}, dager=[{"dato": "2027-09-20", "start_kl": None, "slutt_kl": None}])
    for m in modus:
        plan = FakturaPlan(m, date(2027, 3, 20)) if m == PLAN_UTSATT else FakturaPlan(m)
        html = _n(epost.render("bekreftelse", faktura_plan=plan, **d)[1])
        if forventet[m] is None:
            assert "Faktura på" not in html and "sendes separat" not in html and "Kursavgiften" not in html
        else:
            assert forventet[m] in html


# ============================ 17. maltekst ============================

def test_1_standardtekst_brukes_uten_override_og_er_lik_direkte_render(con, ut):
    kid = _kurs(con)
    pid = _paamelding(con, kid)
    _kjor(con, FAR, pid)
    emne, html = _mail(ut)
    assert emne == "Bekreftelse: Veiledning i praksis"
    assert "<p>Hei Ola,</p>" in html                                                                # standardhilsenen: fornavn
    assert "Takk for påmeldingen! Du har fått plass på <strong>Veiledning i praksis</strong>." in html
    assert f'Du kan når som helst logge inn på <a href="{config.BASE_URL}/min-side">Min side</a> med e-postadressen din.' in html
    assert "<li>2027-09-20 kl. 09:00–16:00</li>" in html and "Sted: Oslo." in html                   # laaste systemblokker uendret
    p = con.execute(sveiper.SQL_DELTAKER + " WHERE p.id=?", (pid,)).fetchone()
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    direkte = epost.render("bekreftelse", p=p, kurs=kurs, dager=db.kursdager(con, kid), faktura_plan=FakturaPlan(PLAN_UTSATT, GRENSE))[1]
    assert _n(direkte) == html


def test_2_gyldig_global_override_brukes_men_fakturablokken_er_uendret(con, ut):
    _lagre(con, "emne", "Velkommen til {kursnavn}")
    _lagre(con, "innledning", "Hei {navn}!" + chr(10) + chr(10) + "Vi gleder oss til {kursnavn}, som starter {startdato}.")
    _lagre(con, "avslutning", "Se {min_side} for mer.")
    kid = _kurs(con)
    pid = _paamelding(con, kid)
    _kjor(con, FAR, pid)
    emne, html = _mail(ut)
    assert emne == "Velkommen til Veiledning i praksis"
    assert "<p>Hei Ola Nordmann!</p>" in html
    assert "Vi gleder oss til <strong>Veiledning i praksis</strong>, som starter 2027-09-20." in html
    assert f'Se <a href="{config.BASE_URL}/min-side">Min side</a> for mer.' in html
    assert "Takk for påmeldingen!" not in html
    assert SAMLET_UTSATT in html and "<li>2027-09-20 kl. 09:00–16:00</li>" in html                  # systemblokkene er uendret


def test_3_tilbakestilling_gir_standardtekst_igjen(con, ut):
    _lagre(con, "innledning", "Egen tekst til {navn}.")
    kid = _kurs(con)
    _kjor(con, FAR, _paamelding(con, kid, "a@x.no"))
    assert "Egen tekst til Ola Nordmann." in _mail(ut, "a@x.no")[1]
    assert maltekster.tilbakestill_maltekst(con, "bekreftelse", "innledning", aktor="admin:test") is True
    con.commit()
    _kjor(con, FAR, _paamelding(con, kid, "b@x.no"))
    _, html = _mail(ut, "b@x.no")
    assert "Takk for påmeldingen! Du har fått plass på <strong>Veiledning i praksis</strong>." in html and "Egen tekst" not in html


@pytest.mark.parametrize("tekst", ["Hei {telefon}", "Faktura: {belop}", "Frist {dato}", "{faktura}", "{{ kurs.pris_nok }}",
                                   "{% if x %}", "{navn.__class__}", "{startdato}{"])
def test_4_ugyldig_placeholder_avvises_ved_lagring_og_skriver_ingenting(con, tekst):
    with pytest.raises(MalFeil):
        maltekster.lagre_maltekst(con, "bekreftelse", "innledning", tekst)
    assert _antall(con, "mal_tekst") == 0


def test_4b_registeret_gir_ingen_fakturakoder_og_ingen_ekstra_felt():
    assert set(MALER["bekreftelse"].felt) == {"emne", "innledning", "avslutning"}
    tillatt = set().union(*(f.kode for f in MALER["bekreftelse"].felt.values()))
    assert tillatt == {"fornavn", "navn", "kursnavn", "startdato", "min_side"}                       # ingen beloep/dato/betaling/status
    assert not [k for k in KODER if "faktura" in k or "belop" in k or "betal" in k]


def test_5_korrupt_override_ved_sending_gir_ingen_mail_ingen_faktura_ingen_forsok_og_sveiper_kjort_0(con, ut, monkeypatch):
    def forbudt(*a, **k):
        raise AssertionError("claim ble forsoekt selv om malen er ugyldig")
    monkeypatch.setattr(db, "reserver_sending", forbudt)
    monkeypatch.setattr(db, "reserver_sending_pa_nytt", forbudt)
    _korrupt(con)
    kid = _kurs(con)
    pid = _paamelding(con, kid, faktura_onskes_na=1)                     # ville gitt faktura naa hvis malen var i orden
    _kjor(con, FAR, pid)
    assert ut["epost"] == [] and ut["visma"] == []
    assert _antall(con, "faktura") == 0 and _antall(con, "faktura_forsok") == 0 and _antall(con, "utsending_logg") == 0
    assert _tilstand(con, pid) == (0, None)
    h = " ".join(r[0] or "" for r in con.execute("SELECT detaljer FROM hendelse WHERE handling='sveip_feil'"))
    assert "Ola" not in h and "@" not in h and "ukjent_kode" not in h and "Hei" not in h          # ingen tekst, navn eller e-post i loggen


def test_5b_uten_kursdager_og_override_som_bruker_startdato_feiler_lukket(con, ut):
    _lagre(con, "innledning", "Hei {navn}, kurset starter {startdato}.")
    kid = _kurs(con, start=None)
    pid = _paamelding(con, kid)
    _kjor(con, FAR, pid)
    assert ut["epost"] == [] and _antall(con, "utsending_logg") == 0 and _tilstand(con, pid)[0] == 0   # aldri en oppdiktet dato


@pytest.mark.parametrize("skade", ["innledning_ugyldig", "ukjent_felt"])
def test_5c_andre_typer_korrupsjon_feiler_ogsaa_lukket(con, ut, skade):
    if skade == "innledning_ugyldig":
        _korrupt(con, "innledning", "Hei {navn")
    else:
        _korrupt(con, "finnes_ikke", "Hei")
    kid = _kurs(con)
    pid = _paamelding(con, kid)
    _kjor(con, FAR, pid)
    assert ut["epost"] == [] and _antall(con, "utsending_logg") == 0 and _antall(con, "faktura_forsok") == 0 and _tilstand(con, pid)[0] == 0


@pytest.mark.parametrize("idag,onskes,faktura_forventet", [(FAR, 0, 0), (GRENSE, 0, 1), (FAR, 1, 1)],
                         ids=["utsatt", "grensedato", "faktura_naa"])
def test_6_etter_korrigert_mal_sendes_bekreftelsen_planen_fortsetter_og_ingen_duplikat(con, ut, idag, onskes, faktura_forventet):
    _korrupt(con)
    kid = _kurs(con)
    pid = _paamelding(con, kid, faktura_onskes_na=onskes)
    _kjor(con, idag, pid)
    assert ut["epost"] == []
    con.execute("DELETE FROM mal_tekst")                                 # malen rettes
    con.commit()
    _kjor(con, idag)                                                     # daglig/global sveiper tar raden igjen
    _kjor(con, idag)
    assert len([m for m in ut["epost"] if m[0] == "ola@x.no"]) == 1      # nøyaktig én bekreftelse
    assert _antall(con, "faktura") == faktura_forventet and len(ut["visma"]) == faktura_forventet
    assert _tilstand(con, pid)[0] == 1
    assert (_tilstand(con, pid)[1] == "2027-03-20") is (faktura_forventet == 0)


def test_7_laast_fakturablokk_kan_ikke_fjernes_med_tom_eller_minimal_redigerbar_tekst(con, ut):
    _lagre(con, "innledning", "X")
    _lagre(con, "avslutning", "")                                        # avslutning kan vaere tom
    _lagre(con, "emne", "E")
    for tom in ("", "   "):
        with pytest.raises(MalFeil):
            maltekster.lagre_maltekst(con, "bekreftelse", "innledning", tom)      # hovedteksten kan ikke tommes
    kid = _kurs(con)
    _kjor(con, FAR, _paamelding(con, kid))
    emne, html = _mail(ut)
    assert emne == "E" and "<p>X</p>" in html
    assert SAMLET_UTSATT in html and "<li>2027-09-20 kl. 09:00–16:00</li>" in html and "Sted: Oslo." in html


def test_8_html_i_redigerbar_tekst_escapes(con, ut):
    _lagre(con, "innledning", "Hei <b>{navn}</b> & Co" + chr(10) + chr(10) + "<script>alert(1)</script> {kursnavn}")
    kid = _kurs(con)
    _kjor(con, FAR, _paamelding(con, kid))
    _, html = _mail(ut)
    assert "<script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;b&gt;Ola Nordmann&lt;/b&gt; &amp; Co" in html and "<b>" not in html


def test_9_manglende_faktura_plan_feiler_lukket_med_malfeil(con):
    with pytest.raises(MalFeil) as e:
        epost.render("bekreftelse", p={"navn": "Ola Test", "fornavn": "Ola"}, kurs={"navn": "K"}, dager=[])
    assert e.value.grunn == MANGLER_VERDI


# ============================ 12. claim-rekkefolge ============================

def test_rekkefolge_maltekst_og_systemblokk_rendres_foer_claim_som_er_foer_ekstern_sending(con, ut, monkeypatch):
    rekkefolge = []
    ekte_mfu, ekte_res = maltekster.maltekst_for_utsending, db.reserver_sending
    monkeypatch.setattr(maltekster, "maltekst_for_utsending", lambda *a, **k: (rekkefolge.append("maltekst"), ekte_mfu(*a, **k))[1])
    monkeypatch.setattr(db, "reserver_sending", lambda *a, **k: (rekkefolge.append("claim"), ekte_res(*a, **k))[1])
    ekte_render = epost.render
    monkeypatch.setattr(epost, "render", lambda *a, **k: (rekkefolge.append("render"), ekte_render(*a, **k))[1])
    ekte_send = epost.send
    monkeypatch.setattr(epost, "send", lambda *a, **k: (rekkefolge.append("send"), ekte_send(*a, **k))[1])
    kid = _kurs(con)
    _kjor(con, GRENSE, _paamelding(con, kid))
    forste = [x for x in rekkefolge if x in ("maltekst", "render", "claim", "send")]
    assert forste[:4] == ["maltekst", "render", "claim", "send"]                                # bekreftelsen foerst ...
    assert ut["visma"] == [1000]


def test_bekreftelsen_sendes_foer_fakturaforsoket(con, ut):
    kid = _kurs(con)
    _kjor(con, GRENSE, _paamelding(con, kid))
    assert ut["logg"] == ["send", "visma"]


# ============================ 18. idempotens / sideeffekter ============================

def test_bekreftelse_sendes_en_gang_og_utsatt_faktura_lager_aldri_faktura_forsok(con, ut):
    kid = _kurs(con)
    pid = _paamelding(con, kid)
    for _ in range(3):
        _kjor(con, FAR)
    assert len(ut["epost"]) == 1 and _antall(con, "faktura_forsok") == 0 and _antall(con, "faktura") == 0


def test_plan_na_gir_maks_ett_fakturaforsoek_etter_vellykket_bekreftelse(con, ut):
    kid = _kurs(con)
    _paamelding(con, kid, faktura_onskes_na=1)
    for _ in range(3):
        _kjor(con, FAR)
    assert len(ut["epost"]) == 1 and ut["visma"] == [1000] and _antall(con, "faktura") == 1
    assert ut["logg"] == ["send", "visma"]


def test_ukjent_epostutfall_gir_ingen_faktura_og_ingen_automatisk_retry(con, monkeypatch):
    forsok = []

    def send_feiler(*a, **k):
        forsok.append(1)
        raise RuntimeError("Graph nede")
    monkeypatch.setattr(epost, "send", send_feiler)
    vismakall = []
    monkeypatch.setattr(visma, "fakturer", lambda g: vismakall.append(g))
    kid = _kurs(con)
    pid = _paamelding(con, kid, faktura_onskes_na=1)
    for _ in range(3):
        _kjor(con, FAR)
    assert forsok == [1]                                                 # aldri automatisk nytt e-postforsoek
    assert vismakall == [] and _antall(con, "faktura") == 0 and _antall(con, "faktura_forsok") == 0
    assert con.execute("SELECT status FROM utsending_logg WHERE mottaker='ola@x.no'").fetchone()[0] == "ukjent"
    assert _tilstand(con, pid)[0] == 0
