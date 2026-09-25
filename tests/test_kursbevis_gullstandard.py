"""Fase 12A: GULLSTANDARD for dagens kursbevis (kurs/kursbevis.py + kurs/maler/kursbevis.html).

Laaser dagens oppforsel FOER kursbevismalen gjores delvis redigerbar (12D): innhold, beregning, lagring,
tilgang og varsling. Kursbevis er UTSTEDTE dokumenter: lagres som ferdig HTML-fil + rad i `dokument`, og
paavirkes ikke av senere malendringer.
Skriver til en midlertidig ROT - ikke til repoets data/-mappe.
"""
from datetime import date

import pytest

from kurs import config, db, kursbevis
from kurs.integrasjoner import epost
from kurs.kjoring import Kjoring

IDAG = date(2027, 6, 1)


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "ROT", tmp_path)
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


@pytest.fixture
def sendt(monkeypatch):
    ut = []
    monkeypatch.setattr(epost, "send", lambda til, emne, html, *a, **kw: ut.append((til, emne, " ".join(html.split()))))
    return ut


def _n(html: str) -> str:
    return " ".join(html.split())


def _kurs(con, kode="K1", datoer=("2027-03-01", "2027-03-02"), status="avsluttet", **kw):
    kid = db.opprett_kurs(con, kode=kode, navn=kw.pop("navn", "Veiledning i praksis"), datoer=list(datoer),
                          sharepoint_mappe=f"Kurs/{kode}", status=status,
                          **{"pris_nok": 0, "sted": "Oslo", "timer_pr_dag": 6, **kw})
    con.commit()
    return kid


def _deltaker(con, kid, navn="Ola Nordmann", epost_="ola@x.no", moter=None):
    """Melder paa og registrerer oppmote paa de angitte kursdagene (default: alle)."""
    status = con.execute("SELECT status FROM kurs WHERE id=?", (kid,)).fetchone()[0]
    con.execute("UPDATE kurs SET status='aapen' WHERE id=?", (kid,))          # paamelding krever apent kurs
    pid, _ = db.meld_paa(con, kid, epost=epost_, navn=navn)
    con.execute("UPDATE kurs SET status=? WHERE id=?", (status, kid))          # ... saa settes den reelle statusen
    dager = db.kursdager(con, kid)
    for dag in (dager if moter is None else [dager[i] for i in moter]):
        db.registrer_oppmote(con, pid, dag["id"], "manuell")
    con.commit()
    return pid


def _innhold(con, kode, deltaker_id):
    """Det lagrede kursbeviset (tabellen dokument_innhold), eller None hvis det ikke er utstedt."""
    rad = con.execute("""SELECT i.innhold FROM dokument_innhold i JOIN dokument d ON d.id=i.dokument_id
                         JOIN kurs k ON k.id=d.kurs_id WHERE d.type='kursbevis' AND k.kode=? AND d.deltaker_id=?""",
                      (kode, deltaker_id)).fetchone()
    return rad["innhold"] if rad else None


def _utsted(con):
    kursbevis.kjor(Kjoring(con, idag=IDAG))
    con.commit()


def _bevis(con, kode="K1", navn="Ola Nordmann"):
    """Returnerer (normalisert html, dokument-rad, deltaker_id) for det utstedte beviset."""
    d = con.execute("SELECT d.* FROM dokument d JOIN kurs k ON k.id=d.kurs_id WHERE d.type='kursbevis' AND k.kode=?",
                    (kode,)).fetchone()
    assert d is not None, "ingen kursbevis utstedt"
    return _n(_innhold(con, kode, d["deltaker_id"])), d, d["deltaker_id"]


# ============================ innhold: faste elementer ============================

def test_standard_kursbevis_har_alle_faste_elementer_og_riktige_data(con, sendt):
    kid = _kurs(con)
    _deltaker(con, kid)
    _utsted(con)
    html, dok, did = _bevis(con)
    assert "<title>Kursbevis – Ola Nordmann</title>" in html
    assert '<div class="liten">Institutt for Psykologisk Rådgivning</div>' in html
    assert "<h1>Kursbevis</h1>" in html and "<p>Dette bekrefter at</p>" in html
    assert '<p class="navn">Ola Nordmann</p>' in html and "<p>har gjennomført</p>" in html
    assert "<strong>Veiledning i praksis</strong>" in html
    assert "<tr><td>Kursdager</td><td>2027-03-01, 2027-03-02</td></tr>" in html   # datoene deltakeren MOTTE
    assert "<tr><td>Timer</td><td>12</td></tr>" in html                             # 2 moter x 6 timer
    assert "<tr><td>Sted</td><td>Oslo</td></tr>" in html
    assert "Akkumulert" not in html                                                # ikke spesialistlop
    assert "______________________________<br>Kursansvarlig, IPR" in html
    assert "@page { size: A4;" in html                                             # fast A4-layout


@pytest.mark.parametrize("timer_pr_dag,forventet", [(6, "12"), (7.5, "15"), (3.25, "6.5")])
def test_timer_er_moter_ganger_timer_pr_dag_formatert_uten_overflodige_nuller(con, sendt, timer_pr_dag, forventet):
    kid = _kurs(con, timer_pr_dag=timer_pr_dag)
    _deltaker(con, kid)
    _utsted(con)
    assert f"<tr><td>Timer</td><td>{forventet}</td></tr>" in _bevis(con)[0]


def test_sted_vises_kun_naar_kurset_har_sted(con, sendt):
    kid = _kurs(con, sted=None)
    _deltaker(con, kid)
    _utsted(con)
    assert "<td>Sted</td>" not in _bevis(con)[0]


def test_spesialistlop_viser_akkumulerte_timer_paa_tvers_av_kurs(con, sendt):
    k1 = _kurs(con, "K1", spesialistlop="EFT")                                   # avsluttet: 2 dager
    k2 = _kurs(con, "K2", datoer=("2027-01-10",), status="aktiv", spesialistlop="EFT")  # 1 dag, ikke avsluttet
    _deltaker(con, k2)
    _deltaker(con, k1)
    _utsted(con)
    html, _, _ = _bevis(con, "K1")
    assert "<tr><td>Timer</td><td>12</td></tr>" in html                             # kun dette kurset
    assert "<tr><td>Akkumulert i EFT-løpet</td><td>18 timer</td></tr>" in html      # (2 + 1) x 6 pa tvers av kurs
    assert con.execute("SELECT COUNT(*) FROM dokument WHERE type='kursbevis'").fetchone()[0] == 1  # K2 ikke avsluttet


def test_escaper_navn_og_kursnavn_i_kursbeviset(con, sendt):
    kid = _kurs(con, navn="Kurs A & B <x>")
    _deltaker(con, kid, navn="<script>alert(1)</script> Ola")
    _utsted(con)
    html, _, _ = _bevis(con)
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; Ola" in html and "Kurs A &amp; B &lt;x&gt;" in html


# ============================ hvem faar kursbevis ============================

def test_terskelen_er_100_prosent_oppmote(con, sendt):
    assert kursbevis.MIN_ANDEL == 1.0
    kid = _kurs(con)
    _deltaker(con, kid, moter=[0])                                                  # motte bare 1 av 2 dager
    _utsted(con)
    assert con.execute("SELECT COUNT(*) FROM dokument WHERE type='kursbevis'").fetchone()[0] == 0 and sendt == []


def test_kun_avsluttet_kurs_og_kun_bekreftede_faar_bevis(con, sendt):
    apen = _kurs(con, "K1", status="aapen")
    _deltaker(con, apen)
    avm = _kurs(con, "K2")
    pid = _deltaker(con, avm, epost_="avm@x.no", navn="Avmeldt")
    con.execute("UPDATE paamelding SET status='avmeldt' WHERE id=?", (pid,))
    con.commit()
    _utsted(con)
    assert con.execute("SELECT COUNT(*) FROM dokument WHERE type='kursbevis'").fetchone()[0] == 0


def test_toer_kjoring_lager_hverken_fil_dokument_eller_epost(con, sendt):
    kid = _kurs(con)
    _deltaker(con, kid)
    kursbevis.kjor(Kjoring(con, idag=IDAG, tor=True))
    assert con.execute("SELECT COUNT(*) FROM dokument").fetchone()[0] == 0 and sendt == []
    assert not (config.ROT / "data").exists()


# ============================ lagring, tilgang, varsling ============================

def test_lagring_dokumentrad_og_filsti(con, sendt):
    kid = _kurs(con)
    pid = _deltaker(con, kid)
    _utsted(con)
    _, dok, did = _bevis(con)
    assert dok["kurs_id"] == kid and dok["type"] == "kursbevis" and dok["publisert"] == 1
    assert dok["tittel"] == "Kursbevis – Veiledning i praksis"
    assert dok["url"] == "db:"                                                  # lagret i databasen, ikke som fil
    assert _innhold(con, "K1", did) is not None and not (config.ROT / "data").exists()


def test_kursbevis_varsles_paa_epost_med_lenke_til_min_side(con, sendt):
    kid = _kurs(con)
    _deltaker(con, kid)
    _utsted(con)
    (til, emne, html), = sendt
    assert (til, emne) == ("ola@x.no", "Kursbevis: Veiledning i praksis")
    assert "Takk for deltakelsen på <strong>Veiledning i praksis</strong>." in html
    assert f'<a href="{config.BASE_URL}/min-side">Min side</a>' in html


def test_tilgang_eier_og_admin_faar_bevis_andre_deltakere_faar_403(con, sendt):
    from kurs.web import app as webapp
    kid = _kurs(con)
    _deltaker(con, kid)
    andre = _deltaker(con, _kurs(con, "K9", status="aapen"), navn="Annen", epost_="annen@x.no", moter=[])
    _utsted(con)
    _, dok, did = _bevis(con)
    annen_did = con.execute("SELECT deltaker_id FROM paamelding WHERE id=?", (andre,)).fetchone()[0]

    eier = webapp.app.test_client()
    with eier.session_transaction() as s:
        s["deltaker_id"] = did
    r = eier.get(f"/dokument/{dok['id']}")
    assert r.status_code == 200 and r.mimetype == "text/html" and "Ola Nordmann" in r.get_data(as_text=True)

    fremmed = webapp.app.test_client()
    with fremmed.session_transaction() as s:
        s["deltaker_id"] = annen_did
    assert fremmed.get(f"/dokument/{dok['id']}").status_code == 403

    admin = webapp.app.test_client()
    admin.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    assert admin.get(f"/dokument/{dok['id']}").status_code == 200
    assert webapp.app.test_client().get(f"/dokument/{dok['id']}").status_code == 403   # uinnlogget


# ============================ utstedt bevis er uavhengig av senere malendring ============================

def test_utstedt_kursbevis_regenereres_aldri_og_paavirkes_ikke_av_senere_malendring(con, sendt, monkeypatch):
    kid = _kurs(con)
    _deltaker(con, kid)
    _utsted(con)
    _, dok, did = _bevis(con)
    for_ = _innhold(con, "K1", did)

    class _AnnenMal:
        def render(self, **kw):
            return "ENDRET MAL"
    kalt = []
    monkeypatch.setattr(kursbevis._env, "get_template", lambda navn: kalt.append(navn) or _AnnenMal())
    _utsted(con)                                                                   # kjorer daglig jobb igjen
    assert kalt == []                                                              # malen brukes ikke paa nytt
    assert _innhold(con, "K1", did) == for_
    assert con.execute("SELECT COUNT(*) FROM dokument WHERE type='kursbevis'").fetchone()[0] == 1
    assert len(sendt) == 1                                                         # og ingen ny e-post
