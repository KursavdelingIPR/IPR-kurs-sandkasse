"""Alle samlingene på Min side (kurs med flere samlinger): kortet «Samlinger og kursdager», overskrift for hver samling i programmet og i filene,
klokkeslett og merknad per dag, og at merkelappen «Samling 1 av 2» er borte (den så ut som om siden bare gjaldt én samling). Bare oppdiktede data.
"""
import re
from datetime import date
from pathlib import Path

import pytest
from ekstrahjelp import DAGER, S, ekstra, kurs_med_samlinger, meld, rad
from kurssidehjelp import deltaker_klient, dokument, fast_dato, ny_database, skriv_side

from kurs import deltakerside, kursdatoer
from kurs import sideinnhold as si

IDAG_FOER = date(2031, 10, 1)


def _tekst_uten_tagger(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).strip()


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    fast_dato(monkeypatch)
    yield c
    c.close()


def _kurs(con, kid):
    return deltakerside.hent_kurs_for_side(con, con.execute("SELECT kode FROM kurs WHERE id=?", (kid,)).fetchone()[0])


def _program(con, kid):
    """En programblokk med ett punkt for hver kursdag (knyttet til kursdagens id)."""
    from kurs import db
    dager = [{"kursdag_id": d["id"], "dato": d["dato"], "tittel": "", "punkter": [{"fra": "09:00", "til": "10:00", "tema": f"Tema {d['dato']}"}]}
             for d in db.kursdager(con, kid)]
    return dokument(si._ny_blokk("program", {"dager": dager}))


def _visning(con, kid, did, idag, **kw):
    return deltakerside.bygg_visning(con, _kurs(con, kid), kw.pop("dok", None) or _program(con, kid), deltaker_id=did, idag=idag,
                                     forhandsvisning=kw.pop("forhandsvisning", False), fil_url=lambda i: f"/fil/{i}", sp_url=lambda n: None, **kw)


def _side(con, monkeypatch, kid, did, idag) -> str:
    """Deltakersiden slik den vises for deltakeren (publisert side med programmet)."""
    fast_dato(monkeypatch, idag)
    kode = con.execute("SELECT kode FROM kurs WHERE id=?", (kid,)).fetchone()[0]
    skriv_side(con, kid, _program(con, kid))
    r = deltaker_klient(did).get(f"/kurs/{kode}/deltakerside")
    assert r.status_code == 200, r.status_code
    return r.get_data(as_text=True)


def _did(con, pid):
    return rad(con, pid)["deltaker_id"]


# ============================ dataene til malen ============================

def test_merkelappen_samling_n_av_m_er_borte_og_alle_samlingene_staar_i_kortet(con):
    kid = kurs_med_samlinger(con)
    ola = meld(con, kid, "Ola")
    v = _visning(con, kid, _did(con, ola), date(2031, 10, 16))
    assert [p["tekst"] for p in v["pillene"]] == ["Du er påmeldt", "Dag 1 av 5"]                              # ingen «Samling 1 av 3»
    # Én linje per samling (04.10.2026): «1. samling» og datoene fra og til (én dag: bare datoen)
    assert [(g["navn"], g["etikett"], g["fra_til"]) for g in v["samlinger"]] == [
        ("Samling 1", "1. samling", "16.10.2031 – 17.10.2031"), ("Samling 2", "2. samling", "13.11.2031 – 14.11.2031"),
        ("Samling 3", "3. samling", "04.12.2031")]
    assert [d["nr"] for d in v["kursdager"]] == [1, 2, 3, 4, 5]                                              # dagene nummereres gjennom hele kurset
    assert [d["samling"] for d in v["kursdager"]] == ["Samling 1", "Samling 1", "Samling 2", "Samling 2", "Samling 3"]


@pytest.mark.parametrize("idag, forventet", [
    (IDAG_FOER, ["Neste samling", None, None]),                # før alt: den første kommer først
    (date(2031, 10, 17), ["Pågår nå", None, None]),            # siste dag i samling 1
    (date(2031, 10, 20), [None, "Neste samling", None]),       # mellom samling 1 og 2
    (date(2031, 11, 14), [None, "Pågår nå", None]),
    (date(2031, 12, 4), [None, None, "Pågår nå"]),
    (date(2031, 12, 10), [None, None, None]),                  # alt er over: ingen merkes, alle står fortsatt i kortet
])
def test_bare_samlingen_som_pagaar_eller_kommer_forst_er_merket(con, idag, forventet):
    kid = kurs_med_samlinger(con)
    v = _visning(con, kid, _did(con, meld(con, kid, "Ola")), idag)
    assert [g["tag"] for g in v["samlinger"]] == forventet and len(v["samlinger"]) == 3


def test_et_kurs_med_en_samling_har_ingen_gruppering_og_heter_fortsatt_kursdager(con, monkeypatch):
    kid = kurs_med_samlinger(con, "ENKEL", samlinger=[S(date(2031, 10, 16), date(2031, 10, 17), "09:00", "16:00", 6.0)])
    pid = meld(con, kid, "Ola")
    v = _visning(con, kid, _did(con, pid), date(2031, 10, 16))
    assert v["samlinger"] == [] and [d["samling"] for d in v["kursdager"]] == [None, None]
    html = _side(con, monkeypatch, kid, _did(con, pid), date(2031, 10, 16))
    kort = html.split('class="dp-dager"')[1].split("</section>")[0]
    assert ">Kursdager<" in kort and "Samlinger og kursdager" not in kort and "dp-samling" not in kort


def test_en_ekstradeltaker_ser_bare_sine_samlinger_med_kursets_eget_nummer(con):
    kid = kurs_med_samlinger(con)
    eva, nina = ekstra(con, kid, "Eva", nr=[1, 3]), ekstra(con, kid, "Nina", nr=[3])
    v = _visning(con, kid, _did(con, eva), date(2031, 10, 1))
    # kursets eget nummer også for en ekstradeltaker på samling 1 og 3 (ikke «1.» og «2. samling»)
    assert [(g["navn"], g["etikett"]) for g in v["samlinger"]] == [("Samling 1", "1. samling"), ("Samling 3", "3. samling")]
    assert [d["nr"] for d in v["kursdager"]] == [1, 2, 3]                                                      # deres egne dager
    v2 = _visning(con, kid, _did(con, nina), date(2031, 10, 1))
    assert [g["navn"] for g in v2["samlinger"]] == ["Samling 3"] and v2["samlinger"][0]["tag"] == "Neste samling"


def test_forhandsvisningen_i_admin_viser_alle_samlingene(con):
    kid = kurs_med_samlinger(con)
    v = _visning(con, kid, None, IDAG_FOER, forhandsvisning=True)
    assert [g["navn"] for g in v["samlinger"]] == ["Samling 1", "Samling 2", "Samling 3"] and len(v["kursdager"]) == 5


def test_samlinger_med_eget_navn_beholder_navnet(con):
    kid = kurs_med_samlinger(con, "NAVN", samlinger=[S(date(2031, 10, 16), date(2031, 10, 17), "09:00", "16:00", 6.0, navn="Grunnkurs"),
                                                      S(date(2031, 11, 13), None, "09:00", "16:00", 6.0, navn="Fordypning")])
    v = _visning(con, kid, _did(con, meld(con, kid, "Ola")), IDAG_FOER)
    assert [g["navn"] for g in v["samlinger"]] == ["Grunnkurs", "Fordypning"]


def test_en_kursdag_uten_samling_i_et_kurs_med_samlinger_far_gruppen_ovrige_kursdager(con):
    kid = kurs_med_samlinger(con)
    con.execute("UPDATE kursdag SET samling_id=NULL WHERE kurs_id=? AND dato=?", (kid, DAGER[3][0]))
    con.commit()
    v = _visning(con, kid, _did(con, meld(con, kid, "Ola")), IDAG_FOER)
    assert [g["navn"] for g in v["samlinger"]] == ["Samling 1", "Samling 2", "Øvrige kursdager"] and len(v["kursdager"]) == 5


def test_toppfeltet_sier_varierer_naar_klokkeslettene_er_ulike(con):
    kid = kurs_med_samlinger(con)                              # samling 3 er 10:00–15:00, de andre 09:00–16:00
    v = _visning(con, kid, _did(con, meld(con, kid, "Ola")), IDAG_FOER)
    assert v["kurs"]["tid"] == "Varierer – se kursdagene"
    assert [d["tid"] for d in v["kursdager"]] == ["09:00–16:00"] * 4 + ["10:00–15:00"]                        # hver dag har sitt eget klokkeslett
    lik = kurs_med_samlinger(con, "LIK", samlinger=[S(date(2031, 10, 16), date(2031, 10, 17), "09:00", "16:00", 6.0),
                                                     S(date(2031, 11, 13), None, "09:00", "16:00", 6.0)])
    assert _visning(con, lik, _did(con, meld(con, lik, "Una")), IDAG_FOER)["kurs"]["tid"] == "09:00–16:00"


def test_i_dag_og_neste_kursdag_sier_hvilken_samling_det_er(con):
    kid = kurs_med_samlinger(con)
    did = _did(con, meld(con, kid, "Ola"))
    v = _visning(con, kid, did, date(2031, 10, 20))                                                           # mellom samling 1 og 2
    assert v["neste_dag"]["samling"] == "Samling 2" and v["neste_dag"]["dag_nr"] == 3 and v["i_dag"] is None
    v = _visning(con, kid, did, date(2031, 11, 13))
    assert v["i_dag"]["samling"] == "Samling 2" and v["i_dag"]["nr"] == 3
    enkel = kurs_med_samlinger(con, "ENK2", samlinger=[S(date(2031, 10, 16), date(2031, 10, 17), "09:00", "16:00", 6.0)])
    v = _visning(con, enkel, _did(con, meld(con, enkel, "Una")), date(2031, 10, 1))
    assert v["neste_dag"]["samling"] is None


def test_programmet_og_filene_vet_hvilken_samling_dagene_hoerer_til(con):
    kid = kurs_med_samlinger(con)
    kurs = _kurs(con, kid)
    dager = deltakerside.kursdager_for(con, kurs)
    prog = deltakerside._program_data(_program(con, kid)["blokker"][0], dager, IDAG_FOER, frozenset(), True)
    assert [(d["samling"], d["nr"]) for d in prog["dager"]] == [("Samling 1", 1), ("Samling 1", 2), ("Samling 2", 3), ("Samling 2", 4), ("Samling 3", 5)]
    uten = deltakerside._program_data(_program(con, kid)["blokker"][0], dager, IDAG_FOER)
    assert [d["samling"] for d in uten["dager"]] == [None] * 5                                                # kurs med én samling: ingen overskrifter
    meta = {i: {"id": i, "filnavn": f"fil{i}.pdf", "type": "dokument", "storrelse": 2048, "opprettet": "2031-10-01 10:00:00"} for i in (1, 2, 3)}
    b = si._ny_blokk("filer", {"filer": [{"fil_id": 1, "tittel": "Fra samling 2", "gruppe": dager[2]["id"]},
                                         {"fil_id": 2, "tittel": "Fra samling 1", "gruppe": dager[0]["id"]},
                                         {"fil_id": 3, "tittel": "Felles", "gruppe": None}], "sharepoint": False})
    med = deltakerside._filer_data(con, kurs, b, meta, dager, IDAG_FOER, lambda i: f"/fil/{i}", lambda n: None, frozenset(), True)
    tittel = [g["tittel"] for g in med["grupper"]]
    assert tittel[0].startswith("Samling 1 · Dag 1 · ") and tittel[1].startswith("Samling 2 · Dag 3 · ") and tittel[2] == "Øvrige filer"
    ingen = deltakerside._filer_data(con, kurs, b, meta, dager, IDAG_FOER, lambda i: f"/fil/{i}", lambda n: None)
    assert ingen["grupper"][0]["tittel"].startswith("Dag 1 · ") and "Samling" not in ingen["grupper"][0]["tittel"]


# ============================ siden slik deltakeren ser den ============================

def test_siden_viser_alle_samlingene_med_sine_dager_og_ingen_samling_n_av_m(con, monkeypatch):
    kid = kurs_med_samlinger(con)
    pid = meld(con, kid, "Ola")
    html = _side(con, monkeypatch, kid, _did(con, pid), date(2031, 10, 16))
    assert "Samling 1 av" not in html and "Samling 2 av" not in html and "av 3</span>" not in html
    kort = html.split('class="dp-dager"')[1].split("</section>")[0]
    # Camilla 04.10.2026: én enkel linje per samling, «1. samling» og datoene fra og til - ingen dagslinjer
    assert ">Samlinger</h2>" in kort and "Dag 1" not in kort
    linjer = [_tekst_uten_tagger(li) for li in re.findall(r"<li>(.*?)</li>", kort, re.S)]
    assert linjer[0].startswith("1. samling16.10.2031 – 17.10.2031") and "Pågår nå" in linjer[0]
    assert [l[:10] for l in linjer] == ["1. samling", "2. samling", "3. samling"]


def test_en_samling_paa_en_dag_har_bare_en_dato(con, monkeypatch):
    kid = kurs_med_samlinger(con, "EN", samlinger=[S(date(2031, 1, 10), None, "09:00", "16:00", 6.0),
                                                    S(date(2031, 2, 10), date(2031, 2, 11), "09:00", "16:00", 6.0)])
    html = _side(con, monkeypatch, kid, _did(con, meld(con, kid, "Ola")), date(2031, 1, 1))
    kort = html.split('class="dp-dager"')[1].split("</section>")[0]
    assert "<b>1. samling</b><span>10.01.2031</span>" in kort and "<b>2. samling</b><span>10.02.2031 – 11.02.2031</span>" in kort


def test_programmet_har_en_overskrift_foer_hver_samling(con, monkeypatch):
    kid = kurs_med_samlinger(con)
    html = _side(con, monkeypatch, kid, _did(con, meld(con, kid, "Ola")), IDAG_FOER)
    program = html.split('id="blokk-')[1:]
    program = next(b for b in program if "dp-pr-dag" in b)
    rekke = [("overskrift", m.group(1)) if m.group(1) else ("dag", None)
             for m in re.finditer(r'<h3 class="dp-gruppe dp-samling-h">([^<]+)</h3>|(<details class="dp-pr-dag)', program)]
    assert rekke == [("overskrift", "Samling 1"), ("dag", None), ("dag", None), ("overskrift", "Samling 2"), ("dag", None), ("dag", None),
                     ("overskrift", "Samling 3"), ("dag", None)]


def test_i_dag_kortet_og_neste_kursdag_nevner_samlingen(con, monkeypatch):
    kid = kurs_med_samlinger(con)
    did = _did(con, meld(con, kid, "Ola"))
    html = _side(con, monkeypatch, kid, did, date(2031, 10, 20))
    assert "Samling 2 · Dag 3 av 5" in html
    html = _side(con, monkeypatch, kid, did, date(2031, 11, 13))
    assert "Samling 2 · Dag 3 av 5" in html and 'id="i-dag"' in html


def test_kursdagens_merknad_vises_i_kortet_og_escapes(con, monkeypatch):
    kid = kurs_med_samlinger(con, "MERK", samlinger=[S(date(2031, 10, 16), date(2031, 10, 17), "09:00", "16:00", 6.0,
                                                       avvik={date(2031, 10, 17): kursdatoer.Dagavvik(merknad="Veiledningsdag <b>fet</b>")}),
                                                      S(date(2031, 11, 13), None, "09:00", "16:00", 6.0)])
    html = _side(con, monkeypatch, kid, _did(con, meld(con, kid, "Ola")), IDAG_FOER)
    kort = html.split('class="dp-dager"')[1].split("</section>")[0]
    assert "<b>fet</b>" not in html and "Veiledningsdag" not in kort      # kortet viser bare samlingene (04.10.2026); merknaden escapes der den står


def test_forhandsvisningen_av_siden_i_admin_viser_samlingene_uten_oppmoetemerker(con):
    from kurssidehjelp import admin_klient
    kid = kurs_med_samlinger(con)
    skriv_side(con, kid, _program(con, kid))
    r = admin_klient(con).get(f"/admin/kurs/{kid}/kursside/forhandsvis?versjon=publisert")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    kort = html.split('class="dp-dager"')[1].split("</section>")[0]
    assert re.findall(r"<b>(\d\. samling)</b>", kort) == ["1. samling", "2. samling", "3. samling"]
    assert "Møtt" not in kort and "Kommer" not in kort                                                        # merkene gjelder en innlogget deltaker


def test_kursdagene_i_kortet_staar_paa_en_linje_og_merkene_er_smaa():
    """Camilla (01.10.2026): «denne delen her med samlingene ble veldig lang. Kan du gjøre denne litt mer komprimert». Dag og dato står på én linje
    (flex som bryter bare når det er trangt, ikke hver på sin linje) og merkene er små, så «Ikke registrert» får plass ved siden av dato og tid."""
    css = re.sub(r"/\*.*?\*/", "", (Path(deltakerside.__file__).resolve().parent / "web" / "static" / "kursside-delt.css").read_text(encoding="utf-8"), flags=re.S)
    assert re.search(r"\.dp-dager li > span:first-child \{[^}]*display:flex", css)
    assert re.search(r"\.dp-dager li small \{[^}]*display:inline", css)
    assert re.search(r"\.dp-dager \.merke \{[^}]*font-size:11\.5px", css)
