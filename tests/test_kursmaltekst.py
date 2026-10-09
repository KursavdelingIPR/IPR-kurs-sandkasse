"""Påmeldingsbekreftelsen per kurs (Camilla 09.10.2026): teksten kan ses og tilpasses i fanen Kommunikasjon på hvert kurs, med hele
e-posten slik den blir for kurset. Et kurs uten egen tekst følger fellesteksten under E-postmaler. Alt er oppdiktet, ingen nettverk."""
from datetime import date, timedelta

import pytest

from adressehjelp import ADRESSE
from kurs import config, db, maltekster
from kurs.integrasjoner import epost


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
    monkeypatch.setattr(epost, "send", lambda til, emne, html, vedlegg=(): ut.append((til, emne, html)))
    return ut


def _klient(brukernavn=None, passord=None):
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": brukernavn or config.ADMIN_BRUKERNAVN, "passord": passord or config.ADMIN_PASSORD})
    return k


def _kurs(con, kode="M1", navn="Vedlikeholdskurs", **kw):
    start = date.today() + timedelta(days=40)
    kid = db.opprett_kurs(con, kode=kode, navn=navn, datoer=[start.isoformat()], sharepoint_mappe=f"Kurs/{kode}",
                          pris_nok=4500, type="fysisk", sted="Bergen", status="aapen", **kw)
    con.commit()
    return kid


def _meld_paa(epost_="kari@eksempel.no", kode="M1"):
    from kurs.web import app as webapp
    return webapp.app.test_client().post(f"/kurs/{kode}", data={
        "fornavn": "Kari", "etternavn": "Nordmann", "epost": epost_, "samtykke": "on", "samtykke_lagring": "on", "betaler": "person",
        **ADRESSE})


def _bekreftelse(sendt, til="kari@eksempel.no"):
    return next((emne, html) for t, emne, html in sendt if t == til)


TEKST = "Hei {fornavn},\n\nKurset er godkjent som vedlikeholdsaktivitet for spesialister."


def test_fanen_viser_fellesteksten_og_hele_eposten_for_kurset(con):
    kid = _kurs(con)
    side = _klient().get(f"/admin/kurs/{kid}/kommunikasjon").get_data(as_text=True)
    assert "Påmeldingsbekreftelse" in side and "Fellesteksten" in side and "Lagre for dette kurset" in side
    assert maltekster.MALER["bekreftelse"].felt["innledning"].standard.split("\n")[0] in side      # utkastet = fellesteksten
    assert "Slik ser e-posten ut for dette kurset" in side and "Sted: Bergen." in side and "Faktura på 4500 kr" in side
    assert "Ola" in side                                                                            # den oppdiktede deltakeren


def test_kursets_tekst_brukes_naar_bekreftelsen_sendes(con, sendt):
    kid = _kurs(con)
    annet = _kurs(con, "M2", navn="Annet kurs")
    k = _klient()
    r = k.post(f"/admin/kurs/{kid}/kommunikasjon/bekreftelse", data={"emne": "Velkommen til {kursnavn}", "innledning": TEKST})
    assert r.status_code == 302 and r.headers["Location"].endswith("#bekreftelse")
    side = k.get(f"/admin/kurs/{kid}/kommunikasjon").get_data(as_text=True)
    assert "Tilpasset for dette kurset" in side and "godkjent som vedlikeholdsaktivitet" in side
    _meld_paa()
    emne, html = _bekreftelse(sendt)
    assert emne == "Velkommen til Vedlikeholdskurs" and "Kurset er godkjent som vedlikeholdsaktivitet for spesialister." in html
    assert "Hei Kari," in html
    _meld_paa("ola@eksempel.no", kode="M2")                     # et annet kurs bruker fortsatt fellesteksten
    emne2, html2 = _bekreftelse(sendt, "ola@eksempel.no")
    assert "vedlikeholdsaktivitet" not in html2 and "Annet kurs" in emne2
    assert con.execute("SELECT COUNT(*) FROM hendelse WHERE handling='kursmal_endret'").fetchone()[0] == 1


def test_bare_felt_som_er_ulike_fellesteksten_lagres(con):
    kid = _kurs(con)
    felles = maltekster.effektive_tekster(con, "bekreftelse")
    assert maltekster.lagre_kurstekster(con, kid, "bekreftelse", dict(felles), aktor="admin:test") == []
    assert con.execute("SELECT COUNT(*) FROM kurs_maltekst").fetchone()[0] == 0
    assert maltekster.lagre_kurstekster(con, kid, "bekreftelse", {**felles, "avslutning": "Vel møtt!"}, aktor="admin:test") == ["avslutning"]
    assert {r["felt"] for r in con.execute("SELECT felt FROM kurs_maltekst")} == {"avslutning"}
    # en senere endring i fellesteksten gjelder feltene kurset ikke har tilpasset
    maltekster.lagre_maltekst(con, "bekreftelse", "emne", "Ny felles: {kursnavn}", aktor="admin:test")
    assert maltekster.effektive_tekster(con, "bekreftelse", kid)["emne"] == "Ny felles: {kursnavn}"
    assert maltekster.effektive_tekster(con, "bekreftelse", kid)["avslutning"] == "Vel møtt!"


def test_ugyldig_tekst_lagres_ikke(con):
    kid = _kurs(con)
    r = _klient().post(f"/admin/kurs/{kid}/kommunikasjon/bekreftelse", data={"innledning": "Hei {ukjent_felt}"})
    assert r.status_code == 400 and "Hei {ukjent_felt}" in r.get_data(as_text=True)
    assert con.execute("SELECT COUNT(*) FROM kurs_maltekst").fetchone()[0] == 0


def test_tilbake_til_fellesteksten(con, sendt):
    kid = _kurs(con)
    k = _klient()
    k.post(f"/admin/kurs/{kid}/kommunikasjon/bekreftelse", data={"innledning": TEKST})
    assert k.post(f"/admin/kurs/{kid}/kommunikasjon/bekreftelse/tilbakestill").status_code == 302
    assert con.execute("SELECT COUNT(*) FROM kurs_maltekst").fetchone()[0] == 0
    _meld_paa()
    assert "vedlikeholdsaktivitet" not in _bekreftelse(sendt)[1]


def test_tekstene_folger_med_naar_kurset_kopieres(con):
    kid, ny = _kurs(con), _kurs(con, "M2")
    maltekster.lagre_kurstekster(con, kid, "bekreftelse", {"innledning": TEKST}, aktor="admin:test")
    maltekster.kopier_kurstekster(con, kid, ny)
    assert maltekster.effektive_tekster(con, "bekreftelse", ny)["innledning"] == TEKST


def test_lesetilgang_ser_men_kan_ikke_endre(con):
    kid = _kurs(con)
    db.opprett_admin_bruker(con, "leser", "Leser", "passord-som-holder", rolle="lese")
    con.commit()
    lese = _klient("leser", "passord-som-holder")
    side = lese.get(f"/admin/kurs/{kid}/kommunikasjon").get_data(as_text=True)
    assert "Påmeldingsbekreftelse" in side and "Lagre for dette kurset" not in side
    assert lese.post(f"/admin/kurs/{kid}/kommunikasjon/bekreftelse", data={"innledning": TEKST}).status_code == 403


def test_bare_bekreftelsen_kan_tilpasses_per_kurs(con):
    kid = _kurs(con)
    with pytest.raises(maltekster.MalFeil):
        maltekster.lagre_kurstekster(con, kid, "venteliste", {"tekst": "x"}, aktor="admin:test")


def test_fellesmalen_sier_at_kursene_kan_ha_egen_tekst(con):
    side = _klient().get("/admin/e-postmaler/bekreftelse").get_data(as_text=True)
    assert "Hvert kurs kan ha sin egen tekst under fanen" in side
