"""Menyene (K2, K3, K4) og Min side som inngang. Bare oppdiktede data.

K2: «Offentlig side» er borte fra admin-menyen (ruten `forside` finnes fortsatt).
K3: «Spør oss» og Kunnskapsbase er av som standard (menyvalg borte, /sporsmal gir 404). Kode og data er beholdt: ASSISTENT_AKTIV=1 slår begge på igjen.
K4: «Mine kurs» er inngangen (adressen /min-side): uinnlogget sier menyen «Logg inn», innlogget «Mine kurs» og «Logg ut», atskilt fra hovedlenkene.
/min-side sender aldri videre. «Min side» er navnet på siden til ETT kurs (/kurs/<kode>/deltakerside).
"""
import re
from datetime import timedelta
from pathlib import Path

import pytest

from kurs import config, db, maltekster
from kurs.integrasjoner import sharepoint

from kurssidehjelp import IDAG, admin_klient, deltaker_klient, dokument, fast_dato, lag_deltaker, lag_kurs, ny_database, skriv_side, tekstblokk

ROT = Path(__file__).resolve().parent.parent


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    fast_dato(monkeypatch)
    monkeypatch.setattr(config, "ASSISTENT_AKTIV", False)          # standarden, uansett miljøet testen kjøres i
    yield c
    c.close()


def _klient():
    from kurs.web import app as webapp
    return webapp.app.test_client()


def _html(r) -> str:
    return r.get_data(as_text=True)


def _nav(html: str) -> str:
    return re.search(r"<nav>(.*?)</nav>", html, re.S).group(1)


def _lenker(html: str) -> list[str]:
    """Teksten i lenkene i menyen øverst, i rekkefølge."""
    return [re.sub(r"<[^>]+>", "", t).strip() for t in re.findall(r"<a\b[^>]*>(.*?)</a>", _nav(html), re.S)]


# ============================ K3: Spør oss og Kunnskapsbase er av som standard ============================

def test_standarden_i_koden_er_at_assistenten_er_av():
    kilde = (ROT / "kurs" / "config.py").read_text(encoding="utf-8")
    assert 'ASSISTENT_AKTIV = get("ASSISTENT_AKTIV", "0") == "1"' in kilde


def test_dokumentasjon_og_eksempelfil_sier_at_standarden_er_0():
    assert re.search(r"(?m)^ASSISTENT_AKTIV=0\b", (ROT / ".env.example").read_text(encoding="utf-8"))
    rad = [linje for linje in (ROT / "DEPLOYMENT.md").read_text(encoding="utf-8").splitlines() if linje.startswith("| `ASSISTENT_AKTIV`")]
    assert len(rad) == 1 and rad[0].split("|")[2].strip() == "`0`"


def test_offentlig_meny_har_ikke_sporsmal_oss_som_standard(con):
    for url in ("/", "/logg-inn", "/innsjekk"):
        html = _html(_klient().get(url))
        assert "Spør oss" not in html and "/sporsmal" not in html


def test_sporsmal_gir_404_som_standard_ogsa_for_post(con):
    assert _klient().get("/sporsmal").status_code == 404
    assert _klient().post("/sporsmal", data={"sporsmal": "Når er kurset?"}).status_code == 404
    assert con.execute("SELECT COUNT(*) FROM henvendelse").fetchone()[0] == 0


def test_med_assistent_paa_vises_sporsmal_oss_og_siden_virker(con, monkeypatch):
    monkeypatch.setattr(config, "ASSISTENT_AKTIV", True)
    html = _html(_klient().get("/logg-inn"))
    assert '<a href="/sporsmal">Spør oss</a>' in html
    assert _klient().get("/sporsmal").status_code == 200
    assert _klient().post("/sporsmal", data={"sporsmal": "Hva er lunsjrutinene?"}).status_code == 200


def test_admin_menyen_har_ikke_kunnskapsbase_som_standard_men_vises_med_assistent_paa(con, monkeypatch):
    admin = admin_klient(con)
    html = _html(admin.get("/admin"))
    assert "Kunnskapsbase" not in _nav(html)
    monkeypatch.setattr(config, "ASSISTENT_AKTIV", True)
    html = _html(admin.get("/admin"))
    assert '<a href="/admin/kunnskap"' in html and ">Kunnskapsbase</a>" in html


def test_kunnskapsbasen_nas_fortsatt_pa_adressen_med_forklaring_og_dataene_er_beholdt(con, monkeypatch):
    con.execute("INSERT INTO kunnskap (kategori, sporsmal, svar, godkjent) VALUES ('praktisk', 'Er lunsj inkludert?', 'Ja, lunsj er inkludert.', 1)")
    con.commit()
    admin = admin_klient(con)
    r = admin.get("/admin/kunnskap")
    html = _html(r)
    assert r.status_code == 200 and "«Spør oss» er slått av." in html and "Er lunsj inkludert?" in html
    assert "Kunnskapsbase" not in _nav(html)
    admin.post("/admin/kunnskap", data={"kategori": "praktisk", "sporsmal": "Hvor er parkeringen?", "svar": "Se kartet.", "godkjent": "on"})
    assert con.execute("SELECT COUNT(*) FROM kunnskap").fetchone()[0] == 2                    # kode og data virker som før
    monkeypatch.setattr(config, "ASSISTENT_AKTIV", True)
    assert "«Spør oss» er slått av." not in _html(admin.get("/admin/kunnskap"))


def test_avlysningsteksten_peker_ikke_paa_sporsmal_oss_men_koden_er_gyldig_i_overstyringer():
    standard = maltekster.standard_tekst("avlysning", "tekst")
    assert "Spør oss" not in standard and "{sporsmal_url}" not in standard and "svare på denne e-posten." in standard
    assert maltekster.valider("avlysning", "tekst", "Se {sporsmal_url} og {min_side}.")          # lagrede overstyringer med koden er fortsatt gyldige


# ============================ K2: Offentlig side ut av admin-menyen ============================

def test_admin_menyen_har_ikke_offentlig_side_og_forsiden_finnes(con, monkeypatch):
    admin = admin_klient(con)
    for aktiv in (False, True):
        monkeypatch.setattr(config, "ASSISTENT_AKTIV", aktiv)
        for url in ("/admin", "/admin", "/admin/rapporter", "/admin/e-postmaler"):
            html = _html(admin.get(url))
            assert "Offentlig side" not in html
    assert admin.get("/admin").status_code == 200 and _klient().get("/").status_code == 200        # ruten forside er urørt
    assert 'class="logo" href="/admin"' in _html(admin.get("/admin"))                              # logoen fører til Oversikten


def test_admin_menyen_har_de_andre_valgene_som_for(con):
    admin = admin_klient(con)
    lenker = _lenker(_html(admin.get("/admin")))
    # Rapporter står sist i menyen, etter Utboks (Camilla 04.10.2026)
    assert lenker[:3] == ["Oversikt", "Kalender", "E-postmaler"] and "Aktiviteter" not in lenker
    assert lenker[3:6] == ["Daglig kjøring", "Brukere", "Utboks"] and lenker[6] == "Økonomi"      # demo
    assert 'href="/admin/logg-ut">Logg ut' in _html(admin.get("/admin"))       # «Logg ut» står i gruppen til høyre (globalt søk)


def test_lesebruker_far_ikke_daglig_kjoring_og_brukere_i_menyen(con):
    lenker = _lenker(_html(admin_klient(con, rolle="lese").get("/admin")))
    assert "Daglig kjøring" not in lenker and "Brukere" not in lenker and "Kunnskapsbase" not in lenker


# ============================ K4: deltakermenyen ============================

def test_uinnlogget_meny_er_bare_logg_inn(con):
    """Kurslisten og innsjekk-siden er tatt ut av menyen: igjen står bare kontolenken."""
    html = _html(_klient().get("/logg-inn"))
    assert _lenker(html) == ["Logg inn"]
    assert 'href="/logg-inn"' in _nav(html) and "Mine kurs" not in _nav(html) and "Logg ut" not in _nav(html)
    assert "Kurs<" not in _nav(html) and "Innsjekk" not in _nav(html)


def test_innlogget_meny_er_mine_kurs_og_logg_ut(con):
    kid = lag_kurs(con)
    did, _ = lag_deltaker(con, kid)
    html = _html(deltaker_klient(did).get("/min-side"))
    assert _lenker(html) == ["Mine kurs", "Logg ut"]
    assert 'href="/min-side"' in _nav(html) and 'href="/logg-ut"' in _nav(html) and "Logg inn" not in _nav(html)


def test_kontolenkene_star_for_seg_atskilt_fra_hovedlenkene(con, monkeypatch):
    """Hovedlenker (nå bare «Spør oss», når den er slått på) først; Logg inn (eller Mine kurs og Logg ut) i en egen gruppe til høyre."""
    monkeypatch.setattr(config, "ASSISTENT_AKTIV", True)
    nav = _nav(_html(_klient().get("/logg-inn")))
    assert nav.index(">Spør oss</a>") < nav.index('<span class="nav-konto">') < nav.index(">Logg inn</a>")
    assert nav.count('class="nav-konto"') == 1 and nav.index("</span>") > nav.index(">Logg inn</a>")
    kid = lag_kurs(con)
    did, _ = lag_deltaker(con, kid)
    nav = _nav(_html(deltaker_klient(did).get("/min-side")))
    assert nav.index(">Spør oss</a>") < nav.index('<span class="nav-konto">') < nav.index(">Mine kurs</a>") < nav.index(">Logg ut</a>")


def test_mine_kurs_er_markert_som_gjeldende_side_i_menyen(con):
    kid = lag_kurs(con)
    did, _ = lag_deltaker(con, kid)
    assert 'href="/min-side" aria-current="page">Mine kurs' in _html(deltaker_klient(did).get("/min-side"))
    skriv_side(con, kid, dokument(tekstblokk()))
    kode = con.execute("SELECT kode FROM kurs WHERE id=?", (kid,)).fetchone()["kode"]
    assert 'aria-current' not in _nav(_html(deltaker_klient(did).get(f"/kurs/{kode}/deltakerside")))      # Min side for ett kurs er ikke «Mine kurs»
    assert 'href="/logg-inn" aria-current="page">Logg inn' in _html(_klient().get("/logg-inn"))


def test_admin_lenken_star_i_kontogruppen_for_innlogget_admin(con):
    nav = _nav(_html(admin_klient(con).get("/logg-inn")))                    # en offentlig side: Admin-lenken står i kontogruppen
    assert '<a href="/admin"><strong>Admin</strong></a>' in nav and nav.index('<span class="nav-konto">') < nav.index("<strong>Admin</strong>")


def test_innloggingssiden_er_en_vanlig_side_med_klar_tekst_og_ingen_ramme(con):
    r = _klient().get("/logg-inn")
    html = _html(r)
    assert "<h1>Logg inn</h1>" in html and "Ingen passord" in html and "Etter innlogging ser du kursene dine. Har kurset Min side, finner du program" in html
    assert "rett til Min side for kurset ditt" not in html            # ikke love Min side til dem som har kurs uten (Camilla 02.10.2026)
    assert 'name="epost"' in html and 'type="email"' in html and "Sjekk inn med kode" not in html          # kodesiden /innsjekk er fjernet
    assert r.headers["X-Frame-Options"] == "DENY" and "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]     # må være en lenke, ikke en ramme


# ============================ Min side som inngang ============================

def _min_side(con, did):
    r = deltaker_klient(did).get("/min-side")
    assert r.status_code == 200
    return _html(r)


def test_min_side_uinnlogget_sendes_til_innlogging(con):
    r = _klient().get("/min-side")
    assert r.status_code == 302 and r.headers["Location"].startswith("/logg-inn")


def test_min_side_sender_aldri_videre_uansett_antall_kurs(con):
    """Ingen kurs, ett kurs med publisert side og flere kurs: alltid 200 og aldri en viderekobling (ingen løkker, faktura og kursbevis alltid nåbare)."""
    kid = lag_kurs(con, "A1")
    did, _ = lag_deltaker(con, kid)
    r = deltaker_klient(did).get("/min-side")
    assert r.status_code == 200
    skriv_side(con, kid, dokument(tekstblokk()))
    assert deltaker_klient(did).get("/min-side").status_code == 200
    for kode in ("A2", "A3"):
        k2 = lag_kurs(con, kode)
        lag_deltaker(con, k2, "kari@example.no")
        skriv_side(con, k2, dokument(tekstblokk()))
    assert deltaker_klient(did).get("/min-side").status_code == 200
    ingen = db.finn_eller_opprett_deltaker(con, epost="ingen@example.no", fornavn="Ingen", etternavn="Kurs")
    con.commit()
    assert deltaker_klient(ingen).get("/min-side").status_code == 200
    assert "Du har ingen påmeldinger." in _html(deltaker_klient(ingen).get("/min-side"))


def test_mine_kurs_har_apne_min_side_bare_nar_siden_er_publisert(con):
    kid = lag_kurs(con, "A1")
    did, _ = lag_deltaker(con, kid)
    html = _min_side(con, did)
    assert "Åpne Min side" not in html and "Kurs A1" in html
    skriv_side(con, kid, dokument(tekstblokk()), publiser=False)
    assert "Åpne Min side" not in _min_side(con, did)                                # utkast er ikke publisert
    skriv_side(con, kid, dokument(tekstblokk()))
    html = _min_side(con, did)
    assert 'href="/kurs/A1/deltakerside"' in html and "Åpne Min side" in html and 'class="knapp ms-apne"' in html
    con.execute("UPDATE kursside SET aktiv=0 WHERE kurs_id=?", (kid,))
    con.commit()
    assert "Åpne Min side" not in _min_side(con, did)                                # nedtatt


FORKLARING_IKKE_APEN = "Min side er ikke åpen for dette kurset"


def test_mine_kurs_forklarer_at_min_side_ikke_er_aapen_i_stedet_for_at_knappen_bare_mangler(con):
    """Camilla (02.10.2026): en deltaker som logger inn fra terapiakademiet.no og har et kurs uten åpen Min side, skal få en forklaring, ikke bare en manglende knapp."""
    kid = lag_kurs(con, "A1")
    did, _ = lag_deltaker(con, kid)
    assert FORKLARING_IKKE_APEN in _min_side(con, did)                                # ingen kursside
    skriv_side(con, kid, dokument(tekstblokk()), publiser=False)
    assert FORKLARING_IKKE_APEN in _min_side(con, did)                                # utkast
    skriv_side(con, kid, dokument(tekstblokk()))
    html = _min_side(con, did)
    assert FORKLARING_IKKE_APEN not in html and "Åpne Min side" in html               # åpen side: knappen, ingen forklaring
    con.execute("UPDATE kursside SET aktiv=0 WHERE kurs_id=?", (kid,))
    con.commit()
    html = _min_side(con, did)
    assert FORKLARING_IKKE_APEN in html and "Åpne Min side" not in html               # tatt ned
    con.execute("UPDATE kursside SET aktiv=1, stenges=? WHERE kurs_id=?", ((IDAG - timedelta(days=1)).isoformat(), kid))
    con.commit()
    assert FORKLARING_IKKE_APEN in _min_side(con, did)                                # stengt (siste åpne dag var i går)


def test_forklaringen_staar_bare_paa_kurset_uten_aapen_side_og_ikke_ved_venteliste_eller_avlysning(con):
    med = lag_kurs(con, "A1")
    skriv_side(con, med, dokument(tekstblokk()))
    uten = lag_kurs(con, "A2")
    did, _ = lag_deltaker(con, med)
    lag_deltaker(con, uten, "kari@example.no")                                         # samme person på to kurs
    html = _min_side(con, did)
    assert html.count(FORKLARING_IKKE_APEN) == 1 and html.count("Åpne Min side") == 1  # forklaringen bare ved kurset uten side
    from kurs import deltakerside
    rader = [{"kurs_id": k, "kode": kode, "status": "bekreftet", "kursstatus": "aapen"} for k, kode in ((med, "A1"), (uten, "A2"))]
    info = deltakerside.min_side_info(con, did, rader, IDAG)                            # selve dataene, ikke bare siden: en åpen side har ingen forklaring
    assert info[med]["kan_apne"] is True and info[med]["tekst"] is None
    assert info[uten]["kan_apne"] is False and info[uten]["tekst"] == deltakerside.TEKST_IKKE_APEN
    vente = lag_kurs(con, "A3")
    skriv_side(con, vente, dokument(tekstblokk()))
    dv, _ = lag_deltaker(con, vente, "vera@example.no", "Vera", "Venteliste", status="venteliste")
    html = _min_side(con, dv)
    assert "Min side åpnes når du har fått plass" in html and FORKLARING_IKKE_APEN not in html
    avlyst = lag_kurs(con, "A4")
    da, _ = lag_deltaker(con, avlyst, "ola@example.no", "Ola", "Avlyst")
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (avlyst,))
    con.commit()
    html = _min_side(con, da)
    assert 'class="merke feil">Avlyst' in html and FORKLARING_IKKE_APEN not in html   # «Avlyst» sier det allerede


def test_mine_kurs_venteliste_far_forklaring_i_stedet_for_knapp(con):
    kid = lag_kurs(con, "A1")
    skriv_side(con, kid, dokument(tekstblokk()))
    lag_deltaker(con, kid, "arne@example.no", "Arne", "Avmeldt", status="avmeldt")
    did, _ = lag_deltaker(con, kid, "vera@example.no", "Vera", "Venteliste", status="venteliste")
    html = _min_side(con, did)
    assert "Min side åpnes når du har fått plass" in html and "Venteliste" in html
    assert "Åpne Min side" not in html and "/deltakerside" not in html


def test_min_side_merker_avlyste_og_gjennomforte_kurs(con):
    kid = lag_kurs(con, "A1")
    did, _ = lag_deltaker(con, kid)
    assert 'class="merke ok">Påmeldt' in _min_side(con, did)
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    con.commit()
    assert 'class="merke gra">Gjennomført' in _min_side(con, did)
    con.execute("UPDATE kurs SET status='avlyst' WHERE id=?", (kid,))
    con.commit()
    html = _min_side(con, did)
    assert 'class="merke feil">Avlyst' in html and "Åpne Min side" not in html and "Påmeldt" not in html


def test_min_side_kaller_ikke_sharepoint_for_kurs_med_kursside_men_beholder_listen_ellers(con, monkeypatch):
    kall = []

    def fake(mappe):
        kall.append(mappe)
        return [{"navn": "Dag 1.pdf", "sti": f"{mappe}/Dag 1.pdf", "storrelse": 10}]

    monkeypatch.setattr(sharepoint, "list_filer", fake)
    med, uten = lag_kurs(con, "MED"), lag_kurs(con, "UTEN")
    did, _ = lag_deltaker(con, med)
    lag_deltaker(con, uten, "kari@example.no")
    skriv_side(con, med, dokument(tekstblokk()))
    html = _min_side(con, did)
    assert kall == ["Kurs/UTEN/Presentasjoner"]                                     # ingen Graph-spørring for kurset som har kursside
    assert html.count("Kursmateriell") == 1 and 'href="/materiell/' in html and "Dag 1.pdf" in html


def test_min_side_har_min_konto_med_ankere_og_sammendrag(con):
    kid = lag_kurs(con, "A1", spesialistlop="EFT", timer_pr_dag=7)
    did, pid = lag_deltaker(con, kid)
    html = _min_side(con, did)
    for anker in ("#fakturaer", "#dokumenter"):
        assert f'href="{anker}"' in html and f'id="{anker[1:]}"' in html
    assert "Ingen fakturaer ennå" in html and "Ingen dokumenter ennå" in html and 'href="#timer"' in html and "EFT · 0 timer" in html
    con.execute("INSERT INTO faktura (paamelding_id, belop_nok, status, faktura_nr) VALUES (?,?,?,?)", (pid, 4900, "sendt", "D100"))
    con.execute("INSERT INTO faktura (paamelding_id, kursdag_id, belop_nok, status, faktura_nr) VALUES (?,?,?,?,?)",
                (pid, db.kursdager(con, kid)[0]["id"], 2500, "betalt", "D101"))
    con.execute("INSERT INTO dokument (kurs_id, deltaker_id, type, tittel, url) VALUES (?,?,?,?,?)", (kid, did, "kursbevis", "Kursbevis A1", "db:"))
    con.commit()
    html = _min_side(con, did)
    assert "2 fakturaer · 1 ubetalt" in html and "1 kursbevis" in html
    assert "D100" in html and "4 900 kr" in html and "D101" in html and "Samling 10.03.2027" in html
    assert 'href="/dokument/' in html and "Kursbevis A1" in html


def test_min_side_viser_aldri_intern_kommentar_notat_adresse_eller_innsjekk_kode(con):
    kid = lag_kurs(con, "A1", notat="HEMMELIG-NOTAT")
    did, pid = lag_deltaker(con, kid)
    con.execute("UPDATE paamelding SET intern_kommentar='HEMMELIG-KOMMENTAR' WHERE id=?", (pid,))
    con.commit()
    dag = db.kursdager(con, kid)[0]
    html = _min_side(con, did)
    for hemmelig in ("HEMMELIG-NOTAT", "HEMMELIG-KOMMENTAR", dag["innsjekk_kode"], dag["innsjekk_token"]):
        assert hemmelig not in html


def test_min_side_har_registrer_oppmote_bare_for_bekreftet_paamelding_paa_kursdagen(con, monkeypatch):
    kid = lag_kurs(con, "A1")
    did, _ = lag_deltaker(con, kid)
    html = _min_side(con, did)
    assert "Registrer oppmøte i dag" in html and 'action="/kurs/A1/deltakerside/oppmote"' in html and 'name="retur" value="min_side"' in html
    assert 'name="kode"' in html and "Dagens kode" in html
    dag = db.kursdager(con, kid)[0]
    assert dag["innsjekk_kode"] not in html and dag["innsjekk_token"] not in html
    fast_dato(monkeypatch, IDAG.replace(day=20))                                     # ingen kursdag i dag: ingen kort
    assert "Registrer oppmøte" not in _min_side(con, did)
    fast_dato(monkeypatch, IDAG)
    lag_deltaker(con, kid, "vera@example.no", "Vera", "Venteliste", status="venteliste")
    vera = con.execute("SELECT id FROM deltaker WHERE epost='vera@example.no'").fetchone()[0]
    assert "Registrer oppmøte" not in _min_side(con, vera)                           # venteliste: ikke bekreftet
