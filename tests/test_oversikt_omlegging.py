"""Omleggingen av startsiden og Oversikten (01.10.2026). Bare oppdiktede data.

* Startsiden (/) er bare innloggingen til Admin: ingen kursliste, ingen «Kurs»- og «Innsjekk»-lenke i menyen.
* Aktiviteter er slått sammen med Oversikten: søk på kursnummer/kursnavn, filtre og kursliste ligger på forsiden i Admin.
* Deltakersøk og kurssøk står side ved side på Oversikten, og toppmenyen har ikke lenger et eget deltakersøk.
* «Siste hendelser» er tatt bort, og Kalender har egen fane i hovedmenyen.
"""
import re
from datetime import date, timedelta

import pytest

from kurs import config, db

from kurssidehjelp import IDAG, admin_klient, deltaker_klient, fast_dato, lag_deltaker, lag_kurs, ny_database


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    fast_dato(monkeypatch)
    monkeypatch.setattr(config, "ASSISTENT_AKTIV", False)
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
    return [re.sub(r"<[^>]+>", "", t).strip() for t in re.findall(r"<a\b[^>]*>(.*?)</a>", _nav(html), re.S)]


def _tekst(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


def _kurs(con, kode, navn, start, status="aapen", **felt) -> int:
    felt = {"type": "fysisk", "sted": "Bergen", **felt}
    kid = db.opprett_kurs(con, kode=kode, navn=navn, datoer=[start.isoformat()], status=status,
                          sharepoint_mappe=f"Kurs/{kode}", **felt)
    con.commit()
    return kid


def _tall(html: str, etikett: str) -> int:
    return int(re.search(re.escape(etikett) + r'</div><div class="stat"[^>]*>(\d+)</div>', html).group(1))


# ============================ startsiden ============================

def test_startsiden_er_bare_innloggingen_til_admin(con):
    lag_kurs(con, "K1")
    r = _klient().get("/")
    html = _html(r)
    assert r.status_code == 200 and "<h1>Logg inn til Admin</h1>" in html
    assert 'name="brukernavn"' in html and 'name="passord"' in html
    assert "Kurs og utdanning" not in html and "Kurs K1" not in html                 # ingen kursliste
    assert _lenker(html) == []                                                         # ingen meny: verken Kurs, Innsjekk eller deltakerinnlogging
    assert "data-deltakersok" not in html


def test_startsiden_sender_innlogget_admin_til_oversikten(con):
    r = admin_klient(con).get("/")
    assert r.status_code == 302 and r.headers["Location"] == "/admin"


def test_startsiden_med_utloept_admin_okt_viser_innloggingen_igjen(con):
    k = admin_klient(con)
    with k.session_transaction() as s:
        s["admin_sist"] = 0                                                            # inaktivitetsgrensen er passert
    r = k.get("/")
    assert r.status_code == 200 and "<h1>Logg inn til Admin</h1>" in _html(r)
    with k.session_transaction() as s:
        assert "admin_id" not in s


def test_innloggingsskjemaet_sendes_til_admin_logg_inn_og_husker_neste_side(con):
    html = _html(_klient().get("/?neste=/admin/rapporter"))
    assert re.search(r'<form method="post" action="/admin/logg-inn\?neste=(%2F|/)admin(%2F|/)rapporter">', html)
    assert '<form method="post" action="/admin/logg-inn">' in _html(_klient().get("/"))          # uten neste
    r = _klient().post("/admin/logg-inn?neste=/admin/rapporter",
                       data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    assert r.status_code == 302 and r.headers["Location"] == "/admin/rapporter"
    r = _klient().post("/admin/logg-inn", data={"brukernavn": config.ADMIN_BRUKERNAVN, "passord": config.ADMIN_PASSORD})
    assert r.status_code == 302 and r.headers["Location"] == "/admin"


def test_utlogging_gaar_til_riktig_innlogging(con):
    kid = lag_kurs(con)
    did, _ = lag_deltaker(con, kid)
    r = deltaker_klient(did).get("/logg-ut")                                           # deltakeren: deltakerinnloggingen
    assert r.status_code == 302 and r.headers["Location"] == "/logg-inn"
    r = admin_klient(con).get("/admin/logg-ut")                                        # administratoren: startsiden
    assert r.status_code == 302 and r.headers["Location"] == "/"


def test_kurs_og_innsjekk_er_ikke_lenket_fra_menyen_og_kodesiden_er_fjernet(con):
    for url in ("/", "/logg-inn"):
        html = _html(_klient().get(url))
        assert ">Kurs</a>" not in html and ">Innsjekk</a>" not in html, url
    assert _klient().get("/innsjekk").status_code == 404           # reserven på QR-plakaten er nå Min side (innlogget), ikke en egen kodeside


# ============================ Aktiviteter er slått sammen med Oversikten ============================

def test_gamle_aktivitetslenker_sendes_til_oversikten_med_soek_og_filtre(con):
    admin = admin_klient(con)
    r = admin.get("/admin/aktiviteter")
    assert r.status_code == 302 and r.headers["Location"] == "/admin#kurs"
    r = admin.get("/admin/aktiviteter?sok=eft&sted=bergen&sorter=navn_desc")
    assert r.status_code == 302 and r.headers["Location"] == "/admin?sok=eft&sted=bergen&sorter=navn_desc#kurs"
    r = _klient().get("/admin/aktiviteter")                                            # uinnlogget: videre til innloggingen
    assert r.status_code == 302 and r.headers["Location"].startswith("/admin/logg-inn")


def test_menyen_har_ikke_aktiviteter_og_oversikt_er_aktiv_paa_kurssidene(con):
    kid = lag_kurs(con)
    admin = admin_klient(con)
    for url in ("/admin", f"/admin/kurs/{kid}/deltakere", f"/admin/kurs/{kid}/oppsett", "/admin/kurs/ny", f"/admin/kurs/{kid}/kursside"):
        meny = _nav(_html(admin.get(url)))
        assert "Aktiviteter" not in meny, url
        assert 'aria-current="page">Oversikt</a>' in meny and 'aria-current="page">Kalender</a>' not in meny, url
    for url in ("/admin/aktiviteter/kalender", "/admin/aktiviteter/aarsplan"):
        meny = _nav(_html(admin.get(url)))
        assert 'aria-current="page">Kalender</a>' in meny and 'aria-current="page">Oversikt</a>' not in meny, url
    for url, tekst in (("/admin/rapporter", "Rapporter"), ("/admin/e-postmaler", "E-postmaler")):
        meny = _nav(_html(admin.get(url)))
        assert f'aria-current="page">{tekst}</a>' in meny and 'aria-current="page">Oversikt</a>' not in meny, url


def test_kurssidene_har_tilbake_til_oversikten(con):
    kid = lag_kurs(con)
    html = _html(admin_klient(con).get(f"/admin/kurs/{kid}/deltakere"))
    assert '<a href="/admin">← Oversikt</a>' in html and "← Aktiviteter" not in html


def test_oversikten_har_ikke_siste_hendelser_og_ingen_loggtekst(con):
    db.logg(con, "unik_testhendelse_xyz", {"x": 1}, aktor="system")
    con.commit()
    html = _html(admin_klient(con).get("/admin"))
    assert "Siste hendelser" not in html and "unik_testhendelse_xyz" not in html
    assert "<title>Oversikt</title>" in html


def test_oversikten_skjuler_avsluttede_og_avlyste_men_soek_og_status_viser_dem(con):
    _kurs(con, "AL", "Alfakurset", IDAG + timedelta(days=30))
    _kurs(con, "BE", "Betautkastet", IDAG + timedelta(days=40), status="utkast")
    _kurs(con, "GA", "Gammakurset", date(2026, 1, 12), status="avsluttet")
    _kurs(con, "DE", "Deltakansellering", IDAG + timedelta(days=50), status="avlyst")
    admin = admin_klient(con)
    standard = _html(admin.get("/admin"))
    assert "Alfakurset" in standard and "Betautkastet" in standard                    # utkast regnes som kommende
    assert "Gammakurset" not in standard and "Deltakansellering" not in standard
    assert "Avsluttede og avlyste kurs er skjult her" in standard
    alle = _html(admin.get("/admin?status=alle"))
    assert all(n in alle for n in ("Alfakurset", "Betautkastet", "Gammakurset", "Deltakansellering"))
    assert "er skjult her" not in alle
    assert "Gammakurset" in _html(admin.get("/admin?sok=gamma"))                      # et søk leter i alle kurs
    soek = _html(admin.get("/admin?sok=deltakans"))
    assert "Deltakansellering" in soek and '<option value="alle" selected>' in soek      # valget viser at søket gjelder alle kurs
    bare = _html(admin.get("/admin?status=avsluttet"))
    assert "Gammakurset" in bare and "Alfakurset" not in bare and "Deltakansellering" not in bare
    ugyldig = _html(admin.get("/admin?status=tull"))
    assert "Alfakurset" in ugyldig and "Gammakurset" not in ugyldig                   # ukjent status gir standardvisningen


def test_kurssoeket_hoerer_til_filterskjemaet_og_soeker_paa_navn_og_nummer(con):
    _kurs(con, "EFT-1", "Parterapi i praksis", IDAG + timedelta(days=30))
    _kurs(con, "SUP-1", "Veiledning i gruppe", IDAG + timedelta(days=31))
    nr = {r["kode"]: r["kursnr"] for r in con.execute("SELECT kode, kursnr FROM kurs")}
    admin = admin_klient(con)
    html = _html(admin.get("/admin"))
    assert '<form method="get" id="kursfilter" action="/admin#kurs" class="filterlinje">' in html
    assert 'id="sok-kurs" name="sok" form="kursfilter"' in html
    assert '<button type="submit" form="kursfilter" class="sok-knapp">Søk</button>' in html
    navn = _html(admin.get("/admin?sok=parterapi"))
    assert "Parterapi i praksis" in navn and "Veiledning i gruppe" not in navn
    assert 'value="parterapi"' in navn                                                 # søkeordet står igjen i feltet
    nummer = _html(admin.get(f"/admin?sok={nr['SUP-1']}"))
    assert "Veiledning i gruppe" in nummer and "Parterapi i praksis" not in nummer
    ingen = _html(admin.get("/admin?sok=finnesikke"))
    assert "Ingen kurs matcher «finnesikke»." in ingen


def test_statuskortene_teller_alle_aapne_kurs_uansett_soek_og_filter(con):
    k1 = _kurs(con, "A1", "Alfa-kurset", IDAG + timedelta(days=30), kapasitet=5)
    k2 = _kurs(con, "B1", "Beta-kurset", IDAG + timedelta(days=31), kapasitet=5)
    _kurs(con, "C1", "Gammel-kurset", date(2026, 1, 12), status="avsluttet")
    lag_deltaker(con, k1, "a@example.no", "Ada", "En")
    lag_deltaker(con, k2, "b@example.no", "Bo", "To")
    lag_deltaker(con, k2, "c@example.no", "Cy", "Tre")
    admin = admin_klient(con)
    for url in ("/admin", "/admin?sok=alfa", "/admin?sted=oslo", "/admin?status=avsluttet", "/admin?antall=10&side=3"):
        html = _html(admin.get(url))
        assert _tall(html, "Aktive / åpne kurs") == 2 and _tall(html, "Påmeldte (åpne kurs)") == 3, url


def test_kurslisten_viser_paameldte_venteliste_og_fakturering_i_samme_celle(con):
    kid = _kurs(con, "CELLE", "Cellekurset", IDAG + timedelta(days=30), kapasitet=1, pris_nok=1000)
    lag_deltaker(con, kid, "a@example.no", "Ada", "En")
    lag_deltaker(con, kid, "b@example.no", "Bo", "To")                                  # kurset er fullt: venteliste
    _kurs(con, "GRATIS", "Gratiskurset", IDAG + timedelta(days=31), fakturering="ingen")
    _kurs(con, "SAMLET", "Samletkurset", IDAG + timedelta(days=32), fakturering="organisasjon")
    html = _html(admin_klient(con).get("/admin"))
    assert "<th" in html and "Fakturert</th>" not in html                               # ingen egen kolonne lenger
    celler = {m[0]: _tekst(m[1]) for m in re.findall(r'<td class="tittel"><a [^>]*><strong>(.*?)</strong></a>.*?'
                                                      r'<td class="tall" data-etikett="Påmeldte"><span>(.*?)</span></td>', html, re.S)}
    assert celler["Cellekurset"] == "1 / 1 1 på venteliste 2 registrert · 0 fakturert"
    assert celler["Gratiskurset"].endswith("0 registrert · gratis kurs")
    assert celler["Samletkurset"].endswith("0 registrert · faktureres samlet")


def test_sortering_og_sidenummerering_peker_paa_oversikten_og_hopper_til_kurslisten(con):
    for n in range(12):
        _kurs(con, f"S{n:02d}", f"Serie {n:02d}", IDAG + timedelta(days=30 + n))
    html = _html(admin_klient(con).get("/admin?antall=10&sok=serie"))
    assert re.search(r'href="/admin\?[^"]*sorter=navn_asc[^"]*#kurs"', html)
    assert "Side 1 av 2" in html and "Viser 10 av 12 kurs" in html
    neste = re.search(r'href="(/admin\?[^"]*side=2[^"]*#kurs)">Neste »</a>', html)
    assert neste and "sok=serie" in neste.group(1) and "antall=10" in neste.group(1)
    assert "/admin/aktiviteter?" not in html and 'href="/admin/aktiviteter"' not in html     # ingen lenker til den gamle listen
    side2 = _html(admin_klient(con).get("/admin?antall=10&sok=serie&side=2"))
    assert "Side 2 av 2" in side2 and "Viser 2 av 12 kurs" in side2 and "« Forrige" in side2


def test_filterlinjen_beholder_valgene_og_nullstill_gaar_til_kurslisten(con):
    _kurs(con, "O1", "Oslokurset", IDAG + timedelta(days=30), sted="Oslo")
    html = _html(admin_klient(con).get("/admin?sted=oslo&mine=1&antall=50"))
    assert '<option value="oslo" selected>' in html and '<option value="50" selected>' in html
    assert re.search(r'name="mine" value="1" checked', html)
    assert '<a class="liten" href="/admin#kurs">Nullstill filtre</a>' in html
    for felt in ("f-sted", "f-ansvarlig", "f-status", "f-sorter", "f-antall"):                # velgerne sender seg selv
        assert re.search(rf'<select id="{felt}" name="\w+" data-send-ved-endring>', html), felt


def test_lesetilgang_ser_oversikten_uten_nytt_kurs_men_med_soek_og_oppmoteliste(con):
    lag_kurs(con, "K1", start=IDAG)                                                    # kursdag i dag
    lese = _html(admin_klient(con, "lese").get("/admin"))
    assert "Nytt kurs +" not in lese and ">Dupliser</a>" not in lese
    assert "data-deltakersok" in lese and 'id="sok-kurs"' in lese
    assert "Se oppmøteliste i dag" in lese and "Ta opp oppmøte i dag" not in lese
    assert "Ta opp oppmøte i dag" in _html(admin_klient(con).get("/admin"))


def test_oppfoelgingskortene_og_detaljene_under_kurslisten_er_uendret(con):
    """Firma- og adressekontrollen og materiell som mangler ligger fortsatt på Oversikten, og kortene lenker til dem."""
    html = _html(admin_klient(con).get("/admin"))
    for etikett in ("Mangler materiell", "Firmaopplysninger må kontrolleres", "Uavklarte operasjoner som krever manuell kontroll"):
        assert etikett in html, etikett
    assert "Trenger oppfølging" in html
    assert html.index('<h2 id="kurs">Kurs</h2>') < html.index("<h2>Status</h2>") < html.index("Trenger oppfølging")     # Kurs, så Status


def test_logoen_lenker_ikke_deltakere_til_admin_innloggingen(con):
    kid = lag_kurs(con)
    did, _ = lag_deltaker(con, kid)
    assert 'class="logo" href="/admin"' in _html(admin_klient(con).get("/admin"))
    assert 'class="logo" href="/min-side"' in _html(deltaker_klient(did).get("/min-side"))
    for url in ("/logg-inn", "/", "/innsjekk"):                                         # uten innlogging: bare tekst, ingen lenke
        html = _html(_klient().get(url))
        tekst_eller_logo = '<span class="logo">IPR Påmeldingssystem</span>' in html or '<span class="logo"><img class="ta-logo"' in html      # deltakersidene har logoen i stedet for teksten
        assert tekst_eller_logo and 'class="logo" href' not in html, url


def test_feilsiden_sender_ikke_deltakere_til_admin_innloggingen(con):
    html = _html(_klient().get("/finnes-ikke"))
    assert "Til innlogging" in html and 'href="/logg-inn"' in html and "Til forsiden" not in html
    kid = lag_kurs(con)
    did, _ = lag_deltaker(con, kid)
    assert "Til Min side" in _html(deltaker_klient(did).get("/finnes-ikke"))
    assert "Til Oversikten" in _html(admin_klient(con).get("/finnes-ikke"))


def test_sporsmal_url_sender_ikke_deltakere_til_admin_innloggingen(con, monkeypatch):
    """Med «Spør oss» av pekte koden {sporsmal_url} på forsiden. Forsiden er nå innloggingen til Admin, så en gammel lagret tekst peker på
    innloggingen for deltakere."""
    from kurs import maltekster
    monkeypatch.setattr(config, "ASSISTENT_AKTIV", False)
    verdier = {"kursnavn": "K", "fornavn": "Kari", "navn": "Kari N"}
    tekst = maltekster.felttekst_til_html("avlysning", "tekst", "Se {sporsmal_url}", verdier)
    assert f"Se {config.BASE_URL}/logg-inn" in tekst and "/sporsmal" not in tekst


def test_brukere_viser_lenken_til_innloggingen_med_kopiknapp(con):
    html = _html(admin_klient(con).get("/admin/brukere"))
    assert "Lenke til innloggingen" in html and 'data-kopier="innloggingslenke"' in html and "Kopier lenke" in html
    assert f'<input id="innloggingslenke" value="{config.BASE_URL}/" readonly' in html
    assert admin_klient(con, "kursadmin").get("/admin/brukere").status_code == 403                  # bare systemadministrator ser siden


def test_sidene_har_litt_luft_ved_kantene_paa_pc_men_ikke_mer_paa_mobil():
    from pathlib import Path
    mal = (Path(__file__).resolve().parent.parent / "kurs" / "web" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "header .inner, main { max-width: 1080px; margin: 0 auto; padding: 0 20px; }" in mal           # mobil: som før
    assert "@media (min-width: 700px) { header .inner, main { padding-inline: 36px; } }" in mal          # PC og nettbrett: litt mer


def test_de_brede_sidene_er_midtstilt_og_ikke_helt_ut_til_kantene():
    """Camilla (01.10.2026): «hvorfor går alt av informasjon, kursnr, tittel osv. helt fra høyre til venstre ... Mener det var mer oversiktlig i sted? Da var det litt
    mer midtstilt». Oversikt, Kalender, Årsplan og Min side-redigereren er «bred», men maks 1240 px (var 1640 px, som ga en tabell over hele skjermen)."""
    from pathlib import Path
    mal = (Path(__file__).resolve().parent.parent / "kurs" / "web" / "templates" / "base.html").read_text(encoding="utf-8")
    m = re.search(r"body\.bred header \.inner, body\.bred main \{ max-width:(\d+)px; \}", mal)
    assert m and 1080 < int(m.group(1)) <= 1240
    assert "header .inner, main { max-width: 1080px; margin: 0 auto; padding: 0 20px; }" in mal           # vanlige sider: som før
