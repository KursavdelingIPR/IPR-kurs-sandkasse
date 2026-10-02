"""Kurslisten på Oversikten (tidligere Aktiviteter → Liste): «Sorter etter», klikkbare kolonneoverskrifter, norsk A–Å og visningen på stor skjerm og mobil.

Kravene (27.09.2026):
  * sorter etter oppstart (tidligste/seneste først), opprettet (sist/eldste først) og kursnavn (A–Å / Å–A)
  * standard: kommende kurs etter oppstart (kurs som ikke er ferdige først, snarest først), deretter tidligere kurs
  * søk, filtre og sidenummerering virker sammen med sorteringen, og eldre lenker (sorter=tittel&retning=desc) virker
  * norsk alfabet: Æ, Ø og Å kommer etter Z, uavhengig av store og små bokstaver
"""
import re
from datetime import date, timedelta

import pytest

from kurs import aktivitetsliste, config, db

IDAG = date.today()


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    return c


def _klient():
    from kurs.web import app as webapp
    k = webapp.app.test_client()
    k.post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    return k


def _kurs(con, kode, navn, start_om, dager=1, opprettet=None, **kw):
    datoer = [(IDAG + timedelta(days=start_om + n)).isoformat() for n in range(dager)]
    kid = db.opprett_kurs(con, kode=kode, navn=navn, datoer=datoer, sharepoint_mappe=f"Kurs/{kode}",
                          **{"type": "fysisk", "sted": "IPR, Bergen", **kw})
    if opprettet:
        con.execute("UPDATE kurs SET opprettet=? WHERE id=?", (opprettet, kid))
    con.commit()
    return kid


def _titler(side: str) -> list[str]:
    return re.findall(r'<td class="tittel"><a [^>]*><strong>([^<]+)</strong></a>', side)


@pytest.fixture
def kurs(con):
    _kurs(con, "A", "Øvelse i veiledning", 30, opprettet="2026-01-03 10:00:00")
    _kurs(con, "B", "Angst hos barn", 10, dager=3, opprettet="2026-02-01 10:00:00")
    _kurs(con, "C", "Ærlighet i terapi", -40, opprettet="2026-03-01 10:00:00")
    _kurs(con, "D", "zeta – nettkurs", 60, type="digital", sted=None, opprettet="2025-12-01 10:00:00")
    _kurs(con, "E", "Åpen samtalegruppe", -5, opprettet="2026-04-01 10:00:00")
    _kurs(con, "F", "emosjonsfokus", 0, dager=2, opprettet="2026-05-01 10:00:00")    # pågår i dag


@pytest.mark.parametrize("sorter,forventet", [
    (None, ["emosjonsfokus", "Angst hos barn", "Øvelse i veiledning", "zeta – nettkurs",      # kommende
            "Åpen samtalegruppe", "Ærlighet i terapi"]),                                     # tidligere, nyeste først
    ("start_asc", ["Ærlighet i terapi", "Åpen samtalegruppe", "emosjonsfokus", "Angst hos barn", "Øvelse i veiledning",
                   "zeta – nettkurs"]),
    ("start_desc", ["zeta – nettkurs", "Øvelse i veiledning", "Angst hos barn", "emosjonsfokus", "Åpen samtalegruppe",
                    "Ærlighet i terapi"]),
    ("opprettet_desc", ["emosjonsfokus", "Åpen samtalegruppe", "Ærlighet i terapi", "Angst hos barn",
                        "Øvelse i veiledning", "zeta – nettkurs"]),
    ("opprettet_asc", ["zeta – nettkurs", "Øvelse i veiledning", "Angst hos barn", "Ærlighet i terapi",
                       "Åpen samtalegruppe", "emosjonsfokus"]),
    ("navn_asc", ["Angst hos barn", "emosjonsfokus", "zeta – nettkurs", "Ærlighet i terapi", "Øvelse i veiledning",
                  "Åpen samtalegruppe"]),                                                       # norsk: Æ Ø Å etter Z
    ("navn_desc", ["Åpen samtalegruppe", "Øvelse i veiledning", "Ærlighet i terapi", "zeta – nettkurs",
                   "emosjonsfokus", "Angst hos barn"]),
])
def test_sorter_etter(con, kurs, sorter, forventet):
    url = "/admin" + (f"?sorter={sorter}" if sorter else "")
    side = _klient().get(url).get_data(as_text=True)
    assert _titler(side) == forventet
    valgt = re.search(r'<option value="([a-z_]+)" selected>', side.split('id="f-sorter"', 1)[1])[1]
    assert valgt == (sorter or "kommende")


def test_grensetilfeller_i_sorteringen():
    """Et kurs som slutter i dag er fortsatt kommende; kurs uten datoer står sist blant de kommende, og sist uansett
    retning når man sorterer på et felt de mangler."""
    idag = date(2027, 3, 10)
    rader = [{"id": 1, "navn": "Slutter i dag", "start": "2027-03-09", "slutt": "2027-03-10"},
             {"id": 2, "navn": "Uten datoer", "start": None, "slutt": None},
             {"id": 3, "navn": "Neste uke", "start": "2027-03-17", "slutt": "2027-03-17"},
             {"id": 4, "navn": "I går", "start": "2027-03-09", "slutt": "2027-03-09"}]
    assert [r["navn"] for r in aktivitetsliste.sorter(rader, "kommende", idag)] == [
        "Slutter i dag", "Neste uke", "Uten datoer", "I går"]
    for sortering in ("start_asc", "start_desc"):
        assert aktivitetsliste.sorter(rader, sortering, idag)[-1]["navn"] == "Uten datoer"


def test_alle_sorteringsvalgene_finnes_i_velgeren(con):
    side = _klient().get("/admin").get_data(as_text=True)
    velger = side.split('id="f-sorter"', 1)[1].split("</select>", 1)[0]
    for tekst in ("Kommende først", "Oppstart – tidligste først", "Oppstart – seneste først",
                  "Opprettet – sist opprettet først", "Opprettet – eldste først", "Kursnavn A–Å", "Kursnavn Å–A"):
        assert tekst in velger
    assert "data-send-ved-endring" in velger.split(">", 1)[0]


def test_sortering_virker_sammen_med_sok_filter_og_sider(con, kurs):
    k = _klient()
    side = k.get("/admin?sok=e&sted=bergen&sorter=navn_desc&antall=10").get_data(as_text=True)
    assert _titler(side) == ["Åpen samtalegruppe", "Øvelse i veiledning", "Ærlighet i terapi",
                             "emosjonsfokus"]                # «Angst hos barn» har ingen e, zeta er online
    for n in range(12):
        _kurs(con, f"M{n}", f"Masseutdanning {n:02d}", 100 + n)
    side1 = k.get("/admin?sorter=start_desc&antall=10").get_data(as_text=True)
    assert _titler(side1)[0] == "Masseutdanning 11"
    neste = re.search(r'<a href="([^"]+)">Neste »</a>', side1)[1].replace("&amp;", "&")
    assert "sorter=start_desc" in neste and "side=2" in neste
    side2 = k.get(neste).get_data(as_text=True)
    assert _titler(side2)[-1] == "Ærlighet i terapi"                            # eldste til sist på siste side


def test_kolonneoverskriftene_sorterer_og_viser_retningen(con, kurs):
    k = _klient()
    side = k.get("/admin").get_data(as_text=True)
    lenke = re.search(r'<a href="([^"]+)">Tittel</a>', side)[1].replace("&amp;", "&")
    assert "sorter=navn_asc" in lenke
    side = k.get(lenke).get_data(as_text=True)
    assert 'aria-sort="ascending">Tittel ↑</a>' in side
    assert _titler(side)[0] == "Angst hos barn"
    lenke = re.search(r'<a href="([^"]+)" aria-sort="ascending">Tittel', side)[1].replace("&amp;", "&")
    assert "sorter=navn_desc" in lenke
    side = k.get("/admin?sorter=kursnr_desc").get_data(as_text=True)
    assert '<option value="kursnr_desc" selected>Kolonne: Kursnr ↓</option>' in side


@pytest.mark.parametrize("gammel,ny", [("sorter=tittel&retning=desc", "navn_desc"), ("sorter=start", "start_asc"),
                                       ("sorter=paameldte&retning=desc", "paameldte_desc"), ("sorter=tull", "kommende")])
def test_eldre_lenker_virker(con, kurs, gammel, ny):
    assert aktivitetsliste.tolk(*[dict(x.split("=") for x in gammel.split("&")).get(n) for n in ("sorter", "retning")]) == ny
    assert _klient().get(f"/admin?{gammel}").status_code == 200


def test_visning_bred_side_format_og_etiketter_til_mobil(con, kurs):
    side = _klient().get("/admin").get_data(as_text=True)
    assert '<body class="bred">' in side and "planlegging.css" in side
    assert '<span class="format digital">Online</span>' in side and '<span class="format fysisk">Fysisk</span>' in side
    for etikett in ("Start", "Sted", "Påmeldte", "Status", "Ansvarlig"):
        assert f'data-etikett="{etikett}"' in side
    assert "3 kursdager, til" in side                                          # fler-dagers kurs
    dag = (IDAG + timedelta(days=10)).strftime("%d.%m.%Y")
    assert f"{dag} kl. 09:00" in side                                          # norsk dato på én linje


def test_offentlige_sider_er_uendret(con, kurs):
    from kurs.web import app as webapp
    side = webapp.app.test_client().get("/kurs/B").get_data(as_text=True)
    # Poenget: adminsidenes stilark og breddeklasse lekker aldri over på den offentlige påmeldingen. (Den har siden 01.10.2026 Terapiakademiet-drakten: <body data-tema="ta">.)
    assert '<body data-tema="ta">' in side and 'class="bred"' not in side and "planlegging.css" not in side
