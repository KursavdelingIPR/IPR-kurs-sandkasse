"""Logger-fanen i deltakervinduet: lesbar historikk for én påmelding (kurs/hendelseslogg.py).

Hva som skjedde, hvem som gjorde det, og dato og klokkeslett i norsk tid. Ingen rå JSON for vanlige brukere, ingen
e-poster (de har egen fane), og innsyn i allergier/tilrettelegging bare for systemadministrator.
"""
from datetime import date

import pytest

from kurs import config, db, hendelseslogg

ADMIN = f"admin:{config.ADMIN_BRUKERNAVN}"          # standardbrukeren «Standardbruker», rolle system


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _kurs(con, kode="LOGG1", kapasitet=3):
    return db.opprett_kurs(con, kode=kode, navn="Veiledning i gruppe", datoer=[date(2031, 10, 16).isoformat()],
                           sharepoint_mappe=f"Kurs/{kode}", pris_nok=2500, kapasitet=kapasitet)


def _meld_paa(con, kid, epost="kari@eksempel.no", fornavn="Kari", **kw):
    pid, _ = db.meld_paa(con, kid, epost=epost, fornavn=fornavn, etternavn="Test", **kw)
    con.commit()
    return pid


def _logg(con, pid, systemadmin=False):
    p = con.execute("SELECT * FROM paamelding WHERE id=?", (pid,)).fetchone()
    return hendelseslogg.for_paamelding(con, p, systemadmin=systemadmin)


def _hva(con, pid):
    return [r.hva for r in _logg(con, pid).rader]


def _rad(con, pid, hva):
    return next(r for r in _logg(con, pid).rader if r.hva == hva)


def _hendelse(con, handling, detaljer, ts=None, aktor="system"):
    db.logg(con, handling, detaljer, aktor=aktor)
    if ts:
        con.execute("UPDATE hendelse SET ts=? WHERE id=(SELECT MAX(id) FROM hendelse)", (ts,))
    con.commit()


def _klient(con, rolle=None):
    """Innlogget testklient. Uten rolle: standardbrukeren (systemadministrator)."""
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    data = {"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD}
    if rolle:
        db.opprett_admin_bruker(con, f"test.{rolle}", f"Test {rolle.capitalize()}", "passord-som-holder", rolle=rolle)
        con.commit()
        data = {"brukernavn": f"test.{rolle}", "passord": "passord-som-holder"}
    assert k.post("/admin/logg-inn", data=data).status_code == 302
    return k


# ============================ påmelding og status ============================

def test_avmelding_og_automatisk_opprykk_i_lesbar_tekst(con):
    kid = _kurs(con, kapasitet=1)
    kari = _meld_paa(con, kid)
    nina = _meld_paa(con, kid, epost="nina@eksempel.no", fornavn="Nina")
    assert _hva(con, nina) == ["Påmeldt – satt på venteliste"]

    db.sett_paamelding_status(con, kari, "avmeldt", aktor=ADMIN)
    con.commit()
    assert _hva(con, kari) == ["Avmeldt", "Påmeldt – bekreftet"]      # «avmelding» + statusendring = én rad
    assert _rad(con, kari, "Avmeldt").detaljer == ["Status før: Bekreftet"]
    assert _rad(con, kari, "Avmeldt").hvem == "Standardbruker"

    opprykk = _rad(con, nina, "Flyttet opp fra venteliste")
    assert (opprykk.hvem, opprykk.detaljer) == ("Systemet (automatisk)", ["En plass ble ledig."])


def test_bekreftet_over_kapasitet_og_status_fra_venteliste_til_bekreftet(con):
    kid = _kurs(con, kapasitet=1)
    _meld_paa(con, kid)
    nina = _meld_paa(con, kid, epost="nina@eksempel.no", fornavn="Nina")
    ola = _meld_paa(con, kid, epost="ola@eksempel.no", fornavn="Ola")
    db.sett_paamelding_status(con, nina, "bekreftet", aktor=ADMIN, tillat_overbooking=True)
    con.commit()
    over = _rad(con, nina, "Bekreftet over kapasitet")
    assert over.detaljer == ["Kurset hadde 1 plass.", "Status før: Venteliste"]

    con.execute("UPDATE kurs SET kapasitet=10 WHERE id=?", (kid,))           # ledig plass, uten automatisk opprykk
    db.sett_paamelding_status(con, ola, "bekreftet", aktor=ADMIN)
    con.commit()
    assert _hva(con, ola)[0] == "Status endret fra venteliste til bekreftet"


def test_avslag_vises_som_en_rad(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    db.avsla_paamelding(con, pid, aktor=ADMIN)
    con.commit()
    assert _hva(con, pid) == ["Avslått", "Påmeldt – bekreftet"]
    assert _rad(con, pid, "Avslått").detaljer == ["Status før: Bekreftet"]


def test_satt_paa_venteliste_og_paameldt_paa_nytt(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    db.sett_paamelding_status(con, pid, "avmeldt", aktor=ADMIN)
    con.commit()
    assert _meld_paa(con, kid) == pid                                        # samme påmelding tas i bruk igjen
    db.sett_paamelding_status(con, pid, "avmeldt", aktor=ADMIN)
    db.sett_paamelding_status(con, pid, "venteliste", aktor=ADMIN)
    con.commit()
    assert _hva(con, pid) == ["Satt på venteliste", "Avmeldt", "Påmeldt på nytt – bekreftet", "Avmeldt",
                              "Påmeldt – bekreftet"]
    assert _rad(con, pid, "Satt på venteliste").detaljer == ["Status før: Avmeldt"]
    assert _rad(con, pid, "Påmeldt på nytt – bekreftet").detaljer == ["Kilde: Påmeldingsskjemaet"]
    assert _rad(con, pid, "Påmeldt – bekreftet").detaljer == []               # kilden gjelder den siste påmeldingen


def test_opprykk_naar_kurset_faar_flere_plasser(con):
    kid = _kurs(con, kapasitet=1)
    _meld_paa(con, kid)
    nina = _meld_paa(con, kid, epost="nina@eksempel.no", fornavn="Nina")
    db.endre_kapasitet(con, kid, 2, aktor=ADMIN)
    con.commit()
    opprykk = _rad(con, nina, "Flyttet opp fra venteliste")
    assert (opprykk.hvem, opprykk.detaljer) == ("Standardbruker", ["Kurset fikk flere plasser."])


def test_manuell_behandling_viser_resultatet(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    _hendelse(con, "manuell_behandling_utlost",
              {"paamelding_id": pid, "kurs_id": kid, "resultat": "fullfort", "via": "bulk", "bulk_id": "b1"},
              aktor=ADMIN)
    assert _rad(con, pid, "Påmeldingen ble behandlet manuelt").detaljer == [
        "Resultat: Fullført", "Behandlet sammen med flere påmeldinger fra deltakerlisten."]


# ============================ hvem ============================

def test_hvem_viser_navn_systemet_deltakeren_selv_og_bedriftens_kontaktperson(con):
    kid = _kurs(con, kapasitet=10)
    kari = _meld_paa(con, kid)
    assert _rad(con, kari, "Påmeldt – bekreftet").hvem == "Deltakeren selv"
    firma = _meld_paa(con, kid, epost="per@eksempel.no", fornavn="Per", paamelding={"kilde": "gruppe"})
    assert _rad(con, firma, "Påmeldt – bekreftet").hvem == "Bedriftens kontaktperson"
    assert _rad(con, firma, "Påmeldt – bekreftet").detaljer == ["Kilde: Bedriftspåmelding"]

    _hendelse(con, "sensitivt_endret", {"paamelding_id": kari}, aktor="admin:sluttet.ansatt")
    _hendelse(con, "sensitivt_endret", {"paamelding_id": kari}, aktor="admin")
    _hendelse(con, "sensitivt_endret", {"paamelding_id": kari}, aktor="system")
    assert [r.hvem for r in _logg(con, kari).rader[:3]] == ["Systemet (automatisk)", "Administrator", "sluttet.ansatt"]


def test_admin_registrering_viser_navnet_paa_den_som_registrerte(con):
    kid = _kurs(con)
    pid, _ = db.meld_paa(con, kid, epost="kari@eksempel.no", fornavn="Kari", etternavn="Test", aktor=ADMIN,
                         paamelding={"kilde": "admin"})
    con.commit()
    rad = _rad(con, pid, "Påmeldt – bekreftet")
    assert (rad.hvem, rad.detaljer) == ("Standardbruker", ["Kilde: Manuelt av administrator"])


# ============================ dato og klokkeslett ============================

@pytest.mark.parametrize("utc, dato, klokkeslett", [
    ("2026-09-26 18:15:00", "26.09.2026", "20:15"),   # sommertid: UTC+2
    ("2026-12-01 18:15:00", "01.12.2026", "19:15"),   # vintertid: UTC+1
    ("2026-12-31 23:30:00", "01.01.2027", "00:30"),   # over midnatt og nyttår
])
def test_dato_og_klokkeslett_i_norsk_tid(con, utc, dato, klokkeslett):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    _hendelse(con, "sensitivt_endret", {"paamelding_id": pid}, ts=utc, aktor=ADMIN)
    rad = _rad(con, pid, "Allergier/tilrettelegging endret")
    assert (rad.dato, rad.klokkeslett) == (dato, klokkeslett)


# ============================ endringer ============================

def test_personopplysninger_og_interne_opplysninger_uten_verdier(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    did = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pid,)).fetchone()[0]
    db.oppdater_deltaker(con, did, {"telefon": "98765432", "fornavn": "Kari Anne"}, aktor=ADMIN)
    db.oppdater_paamelding(con, pid, {"faktura_adresse": "Storgata 1", "intern_kommentar": "Ring først"}, aktor=ADMIN)
    con.commit()
    rader = _logg(con, pid).rader
    assert [r.hva for r in rader[:3]] == ["Påmeldingsopplysninger endret", "Interne opplysninger endret",
                                         "Personopplysninger endret"]
    assert rader[0].detaljer == ["Endret: Fakturaadresse"]
    assert rader[1].detaljer == ["Endret: Intern kommentar"]
    assert rader[2].detaljer == ["Endret: Telefon, Fornavn", "Gjelder personen, ikke bare denne påmeldingen."]
    alt = " ".join(r.hva + " ".join(r.detaljer) for r in rader)
    for verdi in ("98765432", "Kari Anne", "Storgata 1", "Ring først"):          # loggen har aldri verdiene
        assert verdi not in alt


def test_personendringer_gjelder_bare_riktig_person(con):
    """deltaker_id 1 må ikke treffe 10 og 11 (samme vern som for paamelding_id)."""
    kid = _kurs(con, kapasitet=20)
    pider = [_meld_paa(con, kid, epost=f"p{n}@eksempel.no", fornavn=f"P{n}") for n in range(11)]
    deltakere = [con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (p,)).fetchone()[0] for p in pider]
    for did in deltakere[9:]:
        db.oppdater_deltaker(con, did, {"telefon": "11223344"}, aktor=ADMIN)
    con.commit()
    assert deltakere[0] == 1 and "Personopplysninger endret" not in _hva(con, pider[0])
    assert "Personopplysninger endret" in _hva(con, pider[10])


def test_faktura_og_oppmote(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    kdid = con.execute("SELECT id FROM kursdag WHERE kurs_id=?", (kid,)).fetchone()[0]
    _hendelse(con, "fakturert", {"paamelding_id": pid, "kursdag_id": kdid, "faktura_nr": "D1234", "belop": 2500})
    assert _rad(con, pid, "Faktura opprettet").detaljer == ["Fakturanummer: D1234", "Beløp: 2 500 kr",
                                                           "Gjelder samlingen 16.10.2031"]
    _hendelse(con, "oppmote_manuell", {"paamelding_id": pid, "kursdag_id": kdid})          # eldre hendelse
    assert _hva(con, pid)[0] == "Oppmøte endret for kursdagen 16.10.2031"

    k = _klient(con)
    for _ in range(2):                                                    # registrerer og fjerner igjen
        assert k.post(f"/admin/kurs/{kid}/oppmote", data={"paamelding_id": pid, "kursdag_id": kdid}).status_code == 302
    assert _hva(con, pid)[:2] == ["Oppmøte fjernet for kursdagen 16.10.2031",
                                  "Oppmøte registrert for kursdagen 16.10.2031"]


def test_fakturafeil_og_avklaring(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    _hendelse(con, "faktura_feil", {"paamelding_id": pid, "kurs_id": kid, "arsak": "mangler_kursdag"})
    _hendelse(con, "faktura_ukjent", {"paamelding_id": pid, "kursdag_id": None, "feil": "Tidsavbrudd"})
    _hendelse(con, "uavklart_faktura_avklart",
              {"paamelding_id": pid, "kursdag_id": None, "utfall": "fakturert", "faktura_nr": "D9"}, aktor=ADMIN)
    assert _hva(con, pid)[:3] == ["Uavklart faktura avklart: fakturaen finnes",
                                  "Fakturering uavklart – må kontrolleres i Visma", "Faktura ble ikke laget"]
    assert _rad(con, pid, "Faktura ble ikke laget").detaljer == ["Kurset mangler kursdager."]
    assert _rad(con, pid, "Uavklart faktura avklart: fakturaen finnes").detaljer == ["Fakturanummer: D9"]


def test_anonymisering_vises(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    did = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (pid,)).fetchone()[0]
    db.sett_paamelding_status(con, pid, "avmeldt", aktor=ADMIN)       # aktive påmeldinger kan ikke anonymiseres
    db.anonymiser_deltaker(con, did, aktor=ADMIN)
    con.commit()
    assert _hva(con, pid)[:2] == ["Personopplysninger slettet (retten til sletting)", "Avmeldt"]


# ============================ det som ikke skal vises ============================

def test_eposter_vises_ikke_i_logger(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    _hendelse(con, "admin_epost_sendt", {"paamelding_id": pid, "kurs_id": kid}, aktor=ADMIN)
    _hendelse(con, "epost_ukjent", {"nokkel": f"kurs:{kid}", "type": "bekreftelse", "feil": "x", "paamelding_id": pid})
    assert _hva(con, pid) == ["Påmeldt – bekreftet"]


def test_ukjent_hendelse_faar_generell_tekst_og_tekniske_data_bare_for_systemadministrator(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    _hendelse(con, "noe_helt_nytt", {"paamelding_id": pid})
    assert _hva(con, pid)[0] == "Annen hendelse"
    assert all(r.teknisk == "" for r in _logg(con, pid).rader)
    teknisk = _logg(con, pid, systemadmin=True).rader[0].teknisk
    assert "noe_helt_nytt" in teknisk and f'"paamelding_id": {pid}' in teknisk


def test_innsyn_bare_for_systemadministrator(con):
    kid = _kurs(con)
    _hendelse(con, "sensitivt_vist", {"kurs_id": kid}, ts="2020-01-01 10:00:00", aktor=ADMIN)   # før påmeldingen
    pid = _meld_paa(con, kid)
    _hendelse(con, "sensitivt_vist", {"paamelding_id": pid, "kurs_id": kid}, aktor=ADMIN)
    _hendelse(con, "sensitivt_vist", {"kurs_id": kid}, aktor=ADMIN)
    _hendelse(con, "sensitivt_vist", {"kurs_id": kid + 1}, aktor=ADMIN)                         # et annet kurs

    assert _logg(con, pid).innsyn is None
    assert _hva(con, pid) == ["Påmeldt – bekreftet"]                          # innsyn er aldri med i hovedlisten
    innsyn = _logg(con, pid, systemadmin=True).innsyn
    assert [(r.hva, r.hvem) for r in innsyn] == [("Åpnet allergilisten for kurset", "Standardbruker"),
                                                 ("Åpnet deltakeren", "Standardbruker")]


def test_kursadmin_ser_allergier_og_tilrettelegging_bare_innsynsloggen_er_for_systemadministrator(con):
    """IPR: allergier og tilrettelegging skal være lett tilgjengelig i Deltaker-fanen for både systemadministrator og
    kursadministrator. Det er bare innsynsloggen (hvem som har åpnet opplysningene) som er begrenset."""
    kid = db.opprett_kurs(con, kode="FYS1", navn="Fysisk kurs", datoer=[date(2031, 10, 16).isoformat()],
                          sharepoint_mappe="Kurs/FYS1", pris_nok=2500, kapasitet=3, type="fysisk")
    pid, _ = db.meld_paa(con, kid, epost="kari@eksempel.no", fornavn="Kari", etternavn="Test",
                         sensitivt={"allergier": "Nøtteallergi", "tilrettelegging": "Trenger rampe"})
    con.commit()
    kursadmin = _klient(con, "kursadmin")
    deltakerfanen = kursadmin.get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
    assert 'value="Nøtteallergi"' in deltakerfanen and 'value="Trenger rampe"' in deltakerfanen
    assert "Innsyn" not in kursadmin.get(f"/admin/kurs/{kid}/deltaker/{pid}/logger").get_data(as_text=True)

    systemadmin = _klient(con)
    assert 'value="Nøtteallergi"' in systemadmin.get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
    logger = systemadmin.get(f"/admin/kurs/{kid}/deltaker/{pid}/logger").get_data(as_text=True)
    assert "Innsyn i allergier og tilrettelegging (2)" in logger          # kursadmin og systemadministrator åpnet
    assert "Test Kursadmin" in logger


# ============================ fanen i deltakervinduet ============================

def test_fanen_for_kursadmin_og_lesetilgang_er_lesbar_uten_json_og_innsyn(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    _hendelse(con, "sensitivt_vist", {"paamelding_id": pid, "kurs_id": kid}, aktor=ADMIN)
    db.sett_paamelding_status(con, pid, "avmeldt", aktor=ADMIN)
    con.commit()
    for rolle in ("kursadmin", "lese"):
        html = _klient(con, rolle).get(f"/admin/kurs/{kid}/deltaker/{pid}/logger").get_data(as_text=True)
        for tekst in ("Hva som skjedde", "Hvem", "Dato", "Klokkeslett", "Påmeldt – bekreftet", "Avmeldt",
                      "Status før: Bekreftet", "Standardbruker", "Deltakeren selv"):
            assert tekst in html, (rolle, tekst)
        for tekst in ('"paamelding_id"', "status_endret", "sensitivt_vist", "Innsyn", "Tekniske data",
                      "Åpnet deltakeren"):
            assert tekst not in html, (rolle, tekst)


def test_fanen_for_systemadministrator_har_innsyn_og_tekniske_data(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    _hendelse(con, "sensitivt_vist", {"paamelding_id": pid, "kurs_id": kid}, aktor=ADMIN)
    html = _klient(con).get(f"/admin/kurs/{kid}/deltaker/{pid}/logger").get_data(as_text=True)
    assert "Innsyn i allergier og tilrettelegging (1)" in html
    assert "Åpnet deltakeren" in html
    assert "Tekniske data" in html


def test_feltnavn_fra_loggen_vises_som_tekst_ikke_html(con):
    kid = _kurs(con)
    pid = _meld_paa(con, kid)
    _hendelse(con, "paamelding_endret", {"paamelding_id": pid, "felt": ["<b>farlig</b>"]}, aktor=ADMIN)
    html = _klient(con, "kursadmin").get(f"/admin/kurs/{kid}/deltaker/{pid}/logger").get_data(as_text=True)
    assert "<b>farlig</b>" not in html and "&lt;b&gt;farlig&lt;/b&gt;" in html
