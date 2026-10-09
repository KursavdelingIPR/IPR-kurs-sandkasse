"""Skjemabyggeren (fanen «Påmeldingsskjema»), påmelding med kursets egne felt og firmaoppslag, forhåndsvisning = den
ekte siden, og svarene i deltakervinduet, deltakerlisten, anonymiseringen og kursdupliseringen.

Laaste krav:
  * Hele oppsettet lagres atomisk; en ugyldig verdi eller et utdatert skjema lagrer INGENTING.
  * Et felt med svar slettes aldri (og typen endres ikke); et felt andre felt vises etter, slettes ikke.
  * Firmanavn og adresse kommer alltid fra Enhetsregisteret ved innsending - aldri fra det deltakeren sender.
  * Forhåndsvisningen er /kurs/<kode>. Kurs som ikke er offentlige, ser bare innloggede administratorer, og da uten
    innsending (POST er 404 for alle).
  * Svarene logges aldri og sendes aldri på e-post; de slettes ved anonymisering.
Alle testdata er fiktive (oppdiktede virksomheter fra brreg.DEMO_ENHETER)."""
import json
import re

import pytest

from kurs import config, db, ekstrafelt
from kurs import skjemafelt as sf
from kurs.integrasjoner import brreg
from adressehjelp import ADRESSE


# HPR-nummer er slått av i skjemaene (skjemafelt.HPR_I_SKJEMA = False, Camilla 02.10.2026). Disse testene gjelder koden som viser, validerer og lagrer det
# (den finnes fortsatt og kan slås på igjen), så de slår det på. Standarden (av) testes i test_hpr_nummer_samles_ikke_inn.py.
pytestmark = pytest.mark.usefixtures("hpr_i_skjema")


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    monkeypatch.setattr(config, "BASE_URL", "https://kurs.eksempel.no")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def _klient():
    from kurs.web import app as webapp
    return webapp.app.test_client()


def _admin(brukernavn=None, passord=None):
    k = _klient()
    k.post("/admin/logg-inn", data={"brukernavn": brukernavn or config.ADMIN_BRUKERNAVN,
                                    "passord": passord or config.ADMIN_PASSORD})
    return k


@pytest.fixture
def admin():
    return _admin()


def _kurs(con, kode="S1", **kw):
    felter = {"navn": "Eksempelkurs", "type": "fysisk", "sted": "Eksempelsted", "pris_nok": 4500,
              "fakturering": "person", "betaling": "samlet", "kapasitet": 10, **kw}
    kid = db.opprett_kurs(con, kode=kode, datoer=["2099-03-02", "2099-03-03"], sharepoint_mappe=f"K/{kode}", **felter)
    con.commit()
    return kid


def _eget(con, kid, rekkefolge=10, **data):
    fid = db.opprett_ekstrafelt(con, kid, {"type": "tekst", "label": "Land", **data}, rekkefolge, aktor="test")
    con.commit()
    return fid


def _url(kid, ekstra=""):
    return f"/admin/kurs/{kid}/paameldingsskjema{ekstra}"


def _skjemadata(html: str) -> dict:
    """Det skjemabyggeren sender uendret: alle felt i hovedskjemaet med verdiene siden viser."""
    skjema = html.split('data-skjemabygger', 1)[1].split("</form>", 1)[0]
    data = {}
    for tag in re.findall(r"<input[^>]*>", skjema):
        navn = re.search(r'name="([^"]+)"', tag)
        if not navn or navn.group(1) in ("csrf_token", "flytt") or " disabled" in tag:
            continue
        if 'type="checkbox"' in tag:
            if " checked" in tag:
                data[navn.group(1)] = "on"
        else:
            verdi = re.search(r'value="([^"]*)"', tag)
            data[navn.group(1)] = _uescape(verdi.group(1)) if verdi else ""
    for navn, innhold in re.findall(r'<textarea[^>]*name="([^"]+)"[^>]*>(.*?)</textarea>', skjema, re.S):
        data[navn] = _uescape(innhold)
    for navn, innhold in re.findall(r'<select[^>]*name="([^"]+)"(?![^>]*disabled)[^>]*>(.*?)</select>', skjema, re.S):
        valgt = re.search(r'<option value="([^"]*)" selected', innhold) or re.search(r'<option value="([^"]*)"', innhold)
        data[navn] = _uescape(valgt.group(1))
    return data


def _uescape(tekst: str) -> str:
    return (tekst.replace("&#34;", '"').replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&amp;", "&"))


def _lagre(admin, kid, **endringer):
    data = _skjemadata(admin.get(_url(kid)).get_data(as_text=True))
    for k, v in endringer.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    return admin.post(_url(kid), data=data)


def _hendelser(con):
    return [dict(r) for r in con.execute("SELECT handling, detaljer FROM hendelse ORDER BY id")]


BASIS = {"fornavn": "Test", "etternavn": "Person", "epost": "test.person@eksempel.no", "samtykke": "on", "samtykke_lagring": "on", **ADRESSE}


def _paamelding(con):
    return con.execute("SELECT * FROM paamelding ORDER BY id DESC").fetchone()


# ============================ skjemabyggeren: visning ============================

def test_byggeren_viser_lenke_standardfelt_og_egne_felt(con, admin):
    kid = _kurs(con)
    _eget(con, kid, label="Hvilken klinikk?")
    html = admin.get(_url(kid)).get_data(as_text=True)
    assert 'id="f-lenke" value="https://kurs.eksempel.no/kurs/S1" readonly' in html
    assert 'data-kopier="f-lenke"' in html
    assert 'href="/kurs/S1" target="_blank" rel="noopener">Forhåndsvis påmeldingsskjema' in html
    for navn in ("telefon_label", "arbeidssted_label", "yrkestittel_label", "faktura_ref_label", "faktura_epost_label",
                 "faktura_kommentar_label", "ehf_label", "allergier_label",
                 "tilrettelegging_label", "vis_pris_synlig", "hpr_nr_hjelpetekst"):
        assert f'name="{navn}"' in html, navn
    # Adressen er låst (fast rad, ingen felt å endre), og de private fakturafeltene finnes ikke lenger
    for avviklet in ("faktura_adresse", "faktura_postnr", "faktura_sted", "adresse_label", "adresse_synlig",
                     "adresse_obligatorisk", "postnr_label", "poststed_label"):
        assert avviklet not in html, avviklet
    for fast in ("Adresse", "Postnummer", "Poststed"):
        assert f'<span class="sb-fast">{fast}</span>' in html, fast
    assert 'value="Hvilken klinikk?"' in html and 'name="rekkefolge_om_deg" value="telefon,arbeidssted,yrkestittel,ekstra:' in html
    for laast in ("fornavn_label", "epost_label", "samtykke_label", "allergier_obligatorisk", "ehf_obligatorisk",
                  "hpr_nr_synlig", "betaler_label", "org_nr_label"):
        assert f'name="{laast}"' not in html, laast
    assert "Ikke bruk egne felt til helseopplysninger" in html


def test_utkast_faar_forklaring_om_lenken(con, admin):
    kid = _kurs(con, status="utkast")
    assert "Kurset er et utkast. Lenken virker for deltakerne når kurset er åpnet" in admin.get(_url(kid)).get_data(
        as_text=True)


def test_lesetilgang_kan_se_men_ikke_endre(con, admin):
    kid = _kurs(con)
    fid = _eget(con, kid)
    admin.post("/admin/brukere", data={"brukernavn": "leser", "navn": "Leser", "passord": "leser-passord-123",
                                       "rolle": "lese"})
    leser = _admin("leser", "leser-passord-123")
    foer = _hendelser(con)
    assert leser.get(_url(kid)).status_code == 200
    for url, data in ((_url(kid), {}), (_url(kid, "/nytt-felt"), {"mal": "land"}),
                      (_url(kid, f"/felt/{fid}/slett"), {}), (_url(kid, "/tilbakestill"), {})):
        assert leser.post(url, data=data).status_code == 403, url
    assert db.hent_ekstrafelt(con, kid).felt[0].label == "Land"
    assert [h for h in _hendelser(con) if h["handling"] != "admin_innlogget"] == \
        [h for h in foer if h["handling"] != "admin_innlogget"]


# ============================ skjemabyggeren: lagring ============================

def test_uendret_lagring_skriver_ingenting(con, admin):
    kid = _kurs(con)
    _eget(con, kid)
    foer = (_hendelser(con), [dict(r) for r in con.execute("SELECT * FROM kurs_ekstrafelt")])
    r = _lagre(admin, kid)
    assert r.status_code == 302
    assert (_hendelser(con), [dict(r) for r in con.execute("SELECT * FROM kurs_ekstrafelt")]) == foer
    assert "Ingen endringer å lagre." in admin.get(_url(kid)).get_data(as_text=True)


def test_standardfelt_og_informasjonsboksen_lagres(con, admin):
    kid = _kurs(con)
    r = _lagre(admin, kid, yrkestittel_synlig="on", yrkestittel_obligatorisk="on", faktura_ref_obligatorisk="on",
               faktura_ref_hjelpetekst="", vis_pris_synlig=None, allergier_label="Allergier",
               tilrettelegging_synlig=None)
    assert r.status_code == 302
    o = db.hent_skjemaoverstyringer(con, kid).overstyringer
    # yrkestittel er synlig som standard (Camilla 05.10.2026): bare kravet er et avvik som lagres
    assert o["yrkestittel"] == sf.Overstyring(obligatorisk=True)
    assert o["faktura_ref"] == sf.Overstyring(obligatorisk=True, hjelpetekst="")      # standard hjelpetekst fjernet
    assert o["vis_pris"] == sf.Overstyring(synlig=False)
    assert o["allergier"] == sf.Overstyring(label="Allergier") and o["tilrettelegging"] == sf.Overstyring(synlig=False)
    html = _klient().get("/kurs/S1").get_data(as_text=True)
    assert 'name="yrkestittel"' in html and "4 500 kr" not in html and "NB! Bruk bare tall" not in html
    assert 'name="tilrettelegging"' not in html and ">Allergier <" not in html and "Allergier" in html


def test_egne_felt_endres_og_betingelse_lagres(con, admin):
    kid = _kurs(con)
    deling = _eget(con, kid, 20, **ekstrafelt.MALER["deling_epost"][1])
    land = _eget(con, kid, 10)
    r = _lagre(admin, kid, **{f"ekstra_{land}_label": "Bostedsland", f"ekstra_{land}_obligatorisk": "on",
                              f"ekstra_{land}_hjelpetekst": "Linje 1\r\nLinje 2",
                              f"ekstra_{land}_vis_naar": f"ekstra:{deling}=Ja"})
    assert r.status_code == 302
    e = db.hent_ekstrafelt(con, kid).per_id()[land]
    assert (e.label, e.obligatorisk, e.hjelpetekst, e.vis_naar_felt, e.vis_naar_verdi) == (
        "Bostedsland", True, "Linje 1\nLinje 2", f"ekstra:{deling}", "Ja")
    endret = [json.loads(h["detaljer"]) for h in _hendelser(con) if h["handling"] == "ekstrafelt_endret"]
    assert endret == [{"kurs_id": kid, "felt_id": land,
                       "egenskaper": ["label", "hjelpetekst", "obligatorisk", "vis_naar_felt", "vis_naar_verdi"]}]
    assert "Bostedsland" not in json.dumps(_hendelser(con), ensure_ascii=False)


@pytest.mark.parametrize("endring,melding", [
    ({"ekstra_{f}_label": ""}, "Feltnavnet må fylles ut."),
    ({"ekstra_{f}_type": "envalg"}, "Skriv minst to svaralternativer"),
    ({"ekstra_{f}_type": "fil"}, "Velg en gyldig felttype."),
    ({"ekstra_{f}_vis_naar": "ekstra:{f}=Ja"}, "kan ikke vises etter sitt eget svar"),
    ({"ekstra_{f}_plassering": "midten"}, "Velg hvor i skjemaet"),
    ({"telefon_label": "x" * 81}, "Feltnavnet kan være maks 80 tegn"),
])
def test_ugyldig_verdi_lagrer_ingenting_heller_ikke_de_gyldige_endringene(con, admin, endring, melding):
    kid = _kurs(con)
    fid = _eget(con, kid)
    foer = (_hendelser(con), [dict(r) for r in con.execute("SELECT * FROM kurs_ekstrafelt")],
            [dict(r) for r in con.execute("SELECT * FROM kurs_skjemafelt")])
    endring = {k.format(f=fid): v.format(f=fid) for k, v in endring.items()}
    r = _lagre(admin, kid, arbeidssted_obligatorisk="on", yrkestittel_synlig="on", **endring)
    html = r.get_data(as_text=True)
    assert r.status_code == 400 and melding in html and "Ingen endringer er lagret." in html
    assert (_hendelser(con), [dict(r) for r in con.execute("SELECT * FROM kurs_ekstrafelt")],
            [dict(r) for r in con.execute("SELECT * FROM kurs_skjemafelt")]) == foer


@pytest.mark.parametrize("manipuler", ["mangler_felt", "mangler_feltnavn", "ukjent_i_rekkefolge", "dobbel",
                                       "mangler_rekkefolge"])
def test_utdatert_eller_manipulert_skjema_lagrer_ingenting(con, admin, manipuler):
    kid = _kurs(con)
    fid = _eget(con, kid)
    data = _skjemadata(admin.get(_url(kid)).get_data(as_text=True))
    if manipuler == "mangler_felt":           # et felt er lagt til av en annen administrator etter at siden ble åpnet
        _eget(con, kid, label="Nytt felt")
    elif manipuler == "mangler_feltnavn":     # feltet står i rekkefølgen, men skjemaet mangler feltene for det
        data.pop(f"ekstra_{fid}_label")
    elif manipuler == "ukjent_i_rekkefolge":
        data["rekkefolge_om_deg"] += ",ekstra:999"
    elif manipuler == "dobbel":
        data["rekkefolge_om_deg"] = data["rekkefolge_om_deg"].replace("telefon", "arbeidssted", 1)
    else:
        data.pop("rekkefolge_om_deg")
    data["telefon_label"] = "Mobil"
    foer = [dict(r) for r in con.execute("SELECT * FROM kurs_skjemafelt")]
    r = admin.post(_url(kid), data=data)
    assert r.status_code == 400 and "endret et annet sted" in r.get_data(as_text=True)
    assert [dict(r) for r in con.execute("SELECT * FROM kurs_skjemafelt")] == foer
    assert db.hent_ekstrafelt(con, kid).per_id()[fid].label == "Land"


def test_flytting_med_rekkefolgen_og_uten_javascript(con, admin):
    kid = _kurs(con)
    land = _eget(con, kid, 10)
    r = _lagre(admin, kid, rekkefolge_om_deg=f"ekstra:{land},arbeidssted,telefon,yrkestittel")
    assert r.status_code == 302
    assert sf.plassrekkefolge(db.hent_skjemaoverstyringer(con, kid), db.hent_ekstrafelt(con, kid).felt, sf.OM_DEG) == [
        f"ekstra:{land}", "arbeidssted", "telefon", "yrkestittel"]
    assert _lagre(admin, kid, flytt="telefon:opp").status_code == 302          # ▲ uten JavaScript: lagre + flytt
    kurs = con.execute("SELECT * FROM kurs WHERE id=?", (kid,)).fetchone()
    skjema = sf.effektivt_skjema(kurs, db.hent_skjemaoverstyringer(con, kid), db.hent_ekstrafelt(con, kid).felt)
    assert [f.nokkel for f in skjema.deltakerfelt] == [f"ekstra_{land}", "telefon", "arbeidssted", "yrkestittel"]  # yrkestittel vises som standard (Camilla 05.10.2026)
    assert _lagre(admin, kid, flytt=f"ekstra:{land}:opp").status_code == 302   # øverst allerede: ingen endring
    assert _lagre(admin, kid, flytt="ukjent:ned").status_code == 302


def test_plassering_flytter_feltet_sist_i_den_andre_delen(con, admin):
    kid = _kurs(con)
    land = _eget(con, kid, 10)
    kommentar = _eget(con, kid, 20, label="Kommentar", type="langtekst", plassering="til_slutt")  # høyere plass enn 10
    assert _lagre(admin, kid, **{f"ekstra_{land}_plassering": "til_slutt"}).status_code == 302
    egne = db.hent_ekstrafelt(con, kid).felt
    assert sf.plassrekkefolge(None, egne, sf.TIL_SLUTT) == [f"ekstra:{kommentar}", f"ekstra:{land}"]
    assert f"ekstra:{land}" not in sf.plassrekkefolge(None, egne, sf.OM_DEG)


def test_typen_kan_ikke_endres_naar_feltet_har_svar(con, admin):
    kid = _kurs(con)
    fid = _eget(con, kid)
    _klient().post("/kurs/S1", data={**BASIS, f"ekstra_{fid}": "Norge"})
    html = admin.get(_url(kid)).get_data(as_text=True)
    assert f'name="ekstra_{fid}_type" aria-label="Type: Land" disabled' in html and "1 svar" in html
    data = _skjemadata(html)
    assert f"ekstra_{fid}_type" not in data                                  # deaktivert: sendes ikke -> uendret
    assert admin.post(_url(kid), data=data).status_code == 302
    r = admin.post(_url(kid), data={**data, f"ekstra_{fid}_type": "langtekst"})     # manipulert
    assert r.status_code == 400 and "har allerede svar, så typen kan ikke endres" in r.get_data(as_text=True)
    assert db.hent_ekstrafelt(con, kid).felt[0].type == "tekst"


# ============================ legge til og slette egne felt ============================

@pytest.mark.parametrize("mal", list(ekstrafelt.MALER))
def test_hurtigvalg_legger_til_feltet_sist(con, admin, mal):
    kid = _kurs(con)
    r = admin.post(_url(kid, "/nytt-felt"), data={"mal": mal})
    assert r.status_code == 302 and "#felt-ekstra-" in r.headers["Location"]
    e = db.hent_ekstrafelt(con, kid).felt[0]
    assert e.label == ekstrafelt.MALER[mal][0] and e.type == ekstrafelt.MALER[mal][1]["type"]
    assert e.rekkefolge == (4 if e.plassering == sf.OM_DEG else 1)


def test_nytt_felt_fra_skjemaet(con, admin):
    kid = _kurs(con)
    r = admin.post(_url(kid, "/nytt-felt"), data={"label": " Hvilken klinikk? ", "type": "nedtrekk",
                                                  "plassering": "til_slutt", "valg": "Nord\r\nSør"})
    assert r.status_code == 302
    e = db.hent_ekstrafelt(con, kid).felt[0]
    assert (e.label, e.type, e.valg, e.plassering, e.synlig, e.obligatorisk) == (
        "Hvilken klinikk?", "nedtrekk", ("Nord", "Sør"), "til_slutt", True, False)
    logg = [json.loads(h["detaljer"]) for h in _hendelser(con) if h["handling"] == "ekstrafelt_opprettet"]
    assert logg == [{"kurs_id": kid, "felt_id": e.id, "type": "nedtrekk"}]


@pytest.mark.parametrize("data,melding", [({"label": "", "type": "tekst"}, "Feltnavnet må fylles ut."),
                                          ({"label": "Tema", "type": "envalg", "valg": "Bare ett"},
                                           "Skriv minst to svaralternativer"),
                                          ({"label": "Studentbevis", "type": "fil"}, "Velg en gyldig felttype.")])
def test_ugyldig_nytt_felt_lagres_ikke_og_skjemaet_vises_igjen(con, admin, data, melding):
    kid = _kurs(con)
    r = admin.post(_url(kid, "/nytt-felt"), data=data)
    html = r.get_data(as_text=True)
    assert r.status_code == 400 and melding in html and f'id="ny-label" name="label" maxlength="80" required value="{data["label"]}"' in html
    assert db.hent_ekstrafelt(con, kid).felt == ()


def test_ukjent_hurtigvalg_gir_400(con, admin):
    kid = _kurs(con)
    assert admin.post(_url(kid, "/nytt-felt"), data={"mal": "fodselsnummer"}).status_code == 400


def test_maks_antall_egne_felt(con, admin):
    kid = _kurs(con)
    for i in range(ekstrafelt.MAKS_FELT):
        _eget(con, kid, i, label=f"Felt {i}")
    r = admin.post(_url(kid, "/nytt-felt"), data={"mal": "land"})
    assert r.status_code == 400 and "maks 40 egne felt" in r.get_data(as_text=True)


def test_slett_felt_uten_svar(con, admin):
    kid = _kurs(con)
    fid = _eget(con, kid)
    html = admin.get(_url(kid)).get_data(as_text=True)
    assert f'<form id="slett-{fid}" method="post" action="/admin/kurs/{kid}/paameldingsskjema/felt/{fid}/slett" ' \
           f'data-bekreft="Slette feltet «Land»? Det kan ikke angres.">' in html
    assert admin.post(_url(kid, f"/felt/{fid}/slett")).status_code == 302
    assert db.hent_ekstrafelt(con, kid).felt == ()
    assert admin.get(_url(kid, f"/felt/{fid}/slett")).status_code == 405


def test_felt_med_svar_slettes_aldri(con, admin):
    kid = _kurs(con)
    fid = _eget(con, kid)
    _klient().post("/kurs/S1", data={**BASIS, f"ekstra_{fid}": "Norge"})
    html = admin.get(_url(kid)).get_data(as_text=True)
    assert "Feltet har svar og kan ikke slettes." in html and f'form="slett-{fid}"' not in html   # ingen slett-knapp
    r = admin.post(_url(kid, f"/felt/{fid}/slett"), follow_redirects=True)                    # manipulert
    assert "Feltet har svar fra 1 deltaker og kan ikke slettes." in r.get_data(as_text=True)
    assert db.hent_ekstrafelt(con, kid).felt[0].id == fid
    with pytest.raises(db.IntegritetsFeil):          # databasen hindrer det uansett (fremmednøkkel)
        con.execute("DELETE FROM kurs_ekstrafelt WHERE id=?", (fid,))
    con.rollback()


def test_vis_bare_naar_tilbyr_betaler_og_andre_valgfelt_men_aldri_feltet_selv(con, admin):
    kid = _kurs(con)
    deling = _eget(con, kid, 5, **ekstrafelt.MALER["deling_epost"][1])
    nyhetsbrev = _eget(con, kid, 6, **ekstrafelt.MALER["nyhetsbrev"][1])
    land = _eget(con, kid, 7)
    html = admin.get(_url(kid)).get_data(as_text=True)

    def valg(fid):
        velger = html.split(f'name="ekstra_{fid}_vis_naar"', 1)[1].split("</select>", 1)[0]
        return re.findall(r'<option value="([^"]*)"', velger)
    alle = ["", "betaler=person", "betaler=organisasjon", f"ekstra:{deling}=Ja", f"ekstra:{deling}=Nei",
            f"ekstra:{nyhetsbrev}=Ja"]
    assert valg(land) == alle
    assert valg(deling) == [v for v in alle if not v.startswith(f"ekstra:{deling}=")]          # aldri seg selv
    assert valg(nyhetsbrev) == [v for v in alle if not v.startswith(f"ekstra:{nyhetsbrev}=")]
    gratis = _kurs(con, kode="S3", pris_nok=0)
    fid = _eget(con, gratis)
    html = admin.get(_url(gratis)).get_data(as_text=True)
    assert "betaler=" not in html.split(f'name="ekstra_{fid}_vis_naar"', 1)[1].split("</select>", 1)[0]


def test_felt_andre_vises_etter_slettes_ikke(con, admin):
    kid = _kurs(con)
    deling = _eget(con, kid, **ekstrafelt.MALER["deling_epost"][1])
    _eget(con, kid, label="Hvorfor", vis_naar_felt=f"ekstra:{deling}", vis_naar_verdi="Ja")
    r = admin.post(_url(kid, f"/felt/{deling}/slett"), follow_redirects=True)
    assert "«Hvorfor» vises bare etter svaret på dette feltet." in r.get_data(as_text=True)
    assert len(db.hent_ekstrafelt(con, kid).felt) == 2


def test_felt_paa_et_annet_kurs_kan_ikke_slettes_herfra(con, admin):
    kid, annet = _kurs(con), _kurs(con, kode="S2")
    fid = _eget(con, annet)
    r = admin.post(_url(kid, f"/felt/{fid}/slett"), follow_redirects=True)
    assert "Feltet finnes ikke lenger." in r.get_data(as_text=True)
    assert len(db.hent_ekstrafelt(con, annet).felt) == 1


def test_tilbakestill_beholder_egne_felt(con, admin):
    kid = _kurs(con)
    _eget(con, kid)
    db.lagre_skjemafelt(con, kid, "telefon", {"label": "Mobil"})
    con.commit()
    html = admin.get(_url(kid)).get_data(as_text=True)
    assert 'data-bekreft="Tilbakestille påmeldingsskjemaet for dette kurset til standard? Alle endringer i ' \
           'standardfeltene og informasjonsboksen fjernes. Kursets egne felt beholdes.">' in html
    assert admin.post(_url(kid, "/tilbakestill")).status_code == 302
    assert not db.hent_skjemaoverstyringer(con, kid).overstyringer and len(db.hent_ekstrafelt(con, kid).felt) == 1


# ============================ den offentlige siden med egne felt ============================

def _side(kode="S1", klient=None):
    return (klient or _klient()).get(f"/kurs/{kode}").get_data(as_text=True)


def test_egne_felt_rendres_etter_type_og_escapes(con):
    kid = _kurs(con)
    _eget(con, kid, 5, label="<b>Land</b>", hjelpetekst="{{ 7*7 }}\nLinje 2")
    _eget(con, kid, 6, label="Tema", type="flervalg", valg=("A & B", "C"))
    _eget(con, kid, 7, label="Klinikk", type="nedtrekk", valg=("Nord", "Sør"), obligatorisk=True)
    _eget(con, kid, 1, label="Nyhetsbrev", type="avkrysning", valg=("Ja takk",), plassering="til_slutt")
    html = _side()
    assert "&lt;b&gt;Land&lt;/b&gt;" in html and "<b>Land</b>" not in html and "{{ 7*7 }}\nLinje 2" in html
    assert re.search(r'<input id="f-ekstra_\d+" name="ekstra_\d+" maxlength="300" aria-describedby="hjelp-ekstra_\d+" value="">', html)
    assert 'type="checkbox" name="ekstra_2" value="A &amp; B">A &amp; B</label>' in html
    assert re.search(r'<select id="f-ekstra_3" name="ekstra_3" required>\s*<option value="">Velg …</option>'
                     r'<option value="Nord">Nord</option><option value="Sør">Sør</option>', html)
    assert '<input type="checkbox" name="ekstra_4" value="Ja">Ja takk</label>' in html
    assert html.index('name="ekstra_1"') < html.index('name="betaler"') < html.index('name="ekstra_4"') < html.index(
        'name="allergier"') < html.index('name="samtykke"')


def test_betinget_felt_er_skjult_og_deaktivert_til_betingelsen_er_oppfylt(con):
    kid = _kurs(con)
    deling = _eget(con, kid, 5, **{**ekstrafelt.MALER["deling_epost"][1], "plassering": "om_deg"})
    hvorfor = _eget(con, kid, 6, label="Hvorfor", obligatorisk=True, vis_naar_felt=f"ekstra:{deling}",
                    vis_naar_verdi="Ja")
    html = _side()
    rad = re.search(rf'<div class="pm-rad" data-vis-naar="ekstra_{deling}" data-vis-verdi="Ja" hidden>.*?</div>\s*</div>',
                    html, re.S).group(0)
    assert f'name="ekstra_{hvorfor}"' in rad and " disabled" in rad and " required" not in rad
    r = _klient().post("/kurs/S1", data={**BASIS, f"ekstra_{deling}": "Ja"})     # «Ja», men uten svar -> vises igjen
    html = r.get_data(as_text=True)
    assert r.status_code == 400 and "Fyll inn «Hvorfor»." in html
    assert f'data-vis-naar="ekstra_{deling}" data-vis-verdi="Ja">' in html and f'value="Ja" checked' in html


def test_svar_lagres_og_betingede_felt_uten_oppfylt_betingelse_ignoreres(con):
    kid = _kurs(con)
    deling = _eget(con, kid, 5, **ekstrafelt.MALER["deling_epost"][1])
    hvorfor = _eget(con, kid, 6, label="Hvorfor", obligatorisk=True, vis_naar_felt=f"ekstra:{deling}",
                    vis_naar_verdi="Ja")
    tema = _eget(con, kid, 7, label="Tema", type="flervalg", valg=("A", "B", "C"))
    skjult = _eget(con, kid, 8, label="Skjult", synlig=False)
    r = _klient().post("/kurs/S1", data={**BASIS, f"ekstra_{deling}": "Nei", f"ekstra_{hvorfor}": "MANIPULERT",
                                         f"ekstra_{tema}": ["C", "A", "X"], f"ekstra_{skjult}": "MANIPULERT",
                                         "ekstra_999": "MANIPULERT"})
    assert r.status_code == 200 and "Du er påmeldt!" in r.get_data(as_text=True)
    assert db.hent_svar(con, _paamelding(con)["id"]) == {deling: "Nei", tema: '["A", "C"]'}


def test_obligatorisk_eget_felt_gir_400_uten_registrering(con):
    kid = _kurs(con)
    fid = _eget(con, kid, 5, label="Klinikk", type="nedtrekk", valg=("Nord", "Sør"), obligatorisk=True)
    r = _klient().post("/kurs/S1", data={**BASIS, f"ekstra_{fid}": "Øst"})
    assert r.status_code == 400 and "Velg et svar i «Klinikk»." in r.get_data(as_text=True)
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 0


def test_svarene_logges_aldri_og_sendes_aldri_paa_epost(con, tmp_path):
    kid = _kurs(con)
    fid = _eget(con, kid, 5, label="Hemmelig spørsmål")
    assert _klient().post("/kurs/S1", data={**BASIS, f"ekstra_{fid}": "HEMMELIG-SVAR"}).status_code == 200
    assert "HEMMELIG-SVAR" not in json.dumps(_hendelser(con), ensure_ascii=False)
    utboks = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in (tmp_path / "utboks").rglob("*")
                     if p.is_file())
    assert utboks and "HEMMELIG-SVAR" not in utboks and "Hemmelig spørsmål" not in utboks


def test_reaktivering_erstatter_svarene(con):
    kid = _kurs(con)
    fid = _eget(con, kid, 5)
    _klient().post("/kurs/S1", data={**BASIS, f"ekstra_{fid}": "Norge"})
    pid = _paamelding(con)["id"]
    db.meld_av(con, pid)
    con.commit()
    assert _klient().post("/kurs/S1", data=BASIS).status_code == 200
    assert _paamelding(con)["id"] == pid and db.hent_svar(con, pid) == {}


def test_svar_til_felt_paa_et_annet_kurs_lagres_aldri(con):
    kid, annet = _kurs(con), _kurs(con, kode="S2")
    _eget(con, kid)                                   # kurset har egne felt, men ikke det svaret gjelder
    fremmed = _eget(con, annet)
    pid, _ = db.meld_paa(con, kid, epost="x@eksempel.no", fornavn="X", etternavn="Y", svar={fremmed: "Lurt"})
    assert db.hent_svar(con, pid) == {}


def test_yrkestittel_lagres_paa_deltakeren_naar_feltet_vises(con):
    kid = _kurs(con)
    db.lagre_skjemafelt(con, kid, "yrkestittel", {"synlig": True, "obligatorisk": True})
    con.commit()
    r = _klient().post("/kurs/S1", data=BASIS)
    assert r.status_code == 400 and "Fyll inn «Yrkestittel»." in r.get_data(as_text=True)
    assert _klient().post("/kurs/S1", data={**BASIS, "yrkestittel": "Psykolog"}).status_code == 200
    assert con.execute("SELECT yrkestittel FROM deltaker").fetchone()[0] == "Psykolog"


# ============================ informasjonsboksen ============================

def test_informasjonsboksen_folger_valgene(con):
    kid = _kurs(con, paameldingsfrist="2099-02-01")
    html = _side()
    # Felles tidspunkt på én linje med punktum i klokkeslettet (Camilla 05.10.2026)
    for tekst in ("Kursdager", "02.–03.03.2099", "<strong>Tidspunkt:</strong> 09.00–16.00 alle dager", "Eksempelsted",
                  "4 500 kr eks. mva",
                  "Påmeldingsfrist", "søndag 1. februar 2099"):
        assert tekst in html, tekst
    assert "plasser igjen" not in html and "Ledige plasser" not in html      # antallet vises aldri
    for n in sf.INFOFELT:
        db.lagre_skjemafelt(con, kid, n, {"synlig": False})
    con.commit()
    html = _side()
    for tekst in ("Kursdager", "Eksempelsted", "4 500 kr", "Påmeldingsfrist", "plasser igjen"):
        assert tekst not in html, tekst


def test_fullt_kurs_viser_alltid_venteliste(con):
    kid = _kurs(con, kapasitet=1)
    db.meld_paa(con, kid, epost="a@eksempel.no", fornavn="A", etternavn="B")
    con.commit()
    assert "Fullt – du settes på venteliste" in _side()


@pytest.mark.parametrize("kapasitet", [1, 2, 5, 60, 250])
def test_deltakerne_ser_aldri_antall_ledige_plasser(con, admin, kapasitet):
    kid = _kurs(con, kapasitet=kapasitet)
    for i in range(kapasitet - 1):
        db.meld_paa(con, kid, epost=f"d{i}@eksempel.no", fornavn="D", etternavn=str(i))
    con.commit()
    for html in (_side(), _side(klient=admin)):
        assert "plasser igjen" not in html and "plass igjen" not in html and "Ledige plasser" not in html
        assert "Fullt" not in html                          # ikke fullt ennå: ingenting om plasser
    byggeren = admin.get(_url(kid)).get_data(as_text=True)
    assert "ledige plasser" not in byggeren.lower()         # og det finnes ikke som valg i skjemabyggeren
    assert "Vis påmeldingsfrist" in byggeren                # de andre valgene for informasjonsboksen er der


def test_ingen_administratorbanner_paa_den_ekte_paameldingssiden_heller_ikke_for_administrator(con, admin):
    """Camilla (01.10.2026): «dette må bort i påmeldingsskjema» (banneret «Dette er det ekte påmeldingsskjemaet» med lenke og «Rediger skjemaet»).
    Bare et kurs som ikke er åpent for påmelding har fortsatt et banner for administratoren (se test_kurs_som_ikke_er_offentlig_...)."""
    kid = _kurs(con)
    bannertekster = ("Dette er det ekte påmeldingsskjemaet", "Kopier lenke", "Rediger skjemaet", "pm-admin", "pm-lenke")
    offentlig = _side()
    assert not any(t in offentlig for t in bannertekster)
    assert not any(t in _side(klient=admin) for t in bannertekster)
    # Heller ikke når skjemaet vises på nytt etter en valideringsfeil (POST), selv for en innlogget administrator
    feil = admin.post("/kurs/S1", data={"fornavn": "Test"})
    assert feil.status_code == 400
    assert not any(t in feil.get_data(as_text=True) for t in bannertekster)
    assert not any(t in _klient().post("/kurs/S1", data={"fornavn": "Test"}).get_data(as_text=True) for t in bannertekster)
    ny = _klient()                                          # en annen nettleser uten innlogging
    assert not any(t in ny.get("/kurs/S1").get_data(as_text=True) for t in bannertekster)


# ============================ firma betaler: Enhetsregisteret ============================

def _firma(**over):
    return {**BASIS, "betaler": "organisasjon", "org_nr": "999 900 003", **over}


def test_firmanavn_og_adresse_kommer_fra_registeret_ikke_fra_skjemaet(con):
    kid = _kurs(con)
    # EHF er skjult som standard (Camilla 05.10.2026) - vises her via kursets skjemaoppsett, slik at verdien lagres
    db.lagre_skjemafelt(con, kid, "ehf", {"synlig": True})
    con.commit()
    r = _klient().post("/kurs/S1", data=_firma(org_navn="MANIP AS", org_adresse="Manipveien 1", org_postnr="9999",
                                               org_sted="MANIPBY", faktura_ref="12345", faktura_epost="faktura@eksempel.no",
                                               faktura_kommentar="Avdeling 7", ehf="Ja",
                                               faktura_adresse="Privatveien 1"))
    assert r.status_code == 200
    p = _paamelding(con)
    assert (p["betaler"], p["org_nr"], p["org_navn"], p["faktura_adresse"], p["faktura_postnr"], p["faktura_sted"]) == (
        "organisasjon", "999900003", "EKSEMPEL KOMMUNE", "Postboks 100", "1234", "EKSEMPELBY")
    assert (p["faktura_ref"], p["faktura_epost"], p["faktura_kommentar"], p["ehf"]) == (
        "12345", "faktura@eksempel.no", "Avdeling 7", 1)


def test_privat_betaler_bruker_egen_adresse_og_ingen_firmafelt(con):
    """Betaler deltakeren selv, er fakturaadressen hans egen adresse (kopiert fra personen). Et manipulert
    «faktura_adresse» fra klienten brukes aldri, og firmafeltene ignoreres."""
    _kurs(con)
    assert _klient().post("/kurs/S1", data={**BASIS, "betaler": "person", "faktura_adresse": "Manipveien 1",
                                            "faktura_postnr": "9999", "faktura_sted": "Manipby", "org_nr": "999900003",
                                            "faktura_ref": "MANIP", "ehf": "Ja"}).status_code == 200
    p = _paamelding(con)
    assert (p["betaler"], p["faktura_adresse"], p["faktura_postnr"], p["faktura_sted"], p["org_nr"], p["org_navn"],
            p["faktura_ref"], p["ehf"]) == ("person", ADRESSE["adresse"], ADRESSE["postnr"], ADRESSE["poststed"],
                                            None, None, None, 0)


@pytest.mark.parametrize("orgnr,melding", [
    ("", "Skriv et gyldig organisasjonsnummer (9 siffer)"), ("12345", "Skriv et gyldig organisasjonsnummer"),
    ("999900004", "Skriv et gyldig organisasjonsnummer"), ("EKSEMPEL KOMMUNE", "Skriv et gyldig organisasjonsnummer"),
    ("999900070", "Fant ikke organisasjonsnummeret i Brønnøysundregistrene."),
])
def test_ugyldig_eller_ukjent_organisasjonsnummer_gir_400(con, orgnr, melding):
    _kurs(con)
    r = _klient().post("/kurs/S1", data=_firma(org_nr=orgnr))
    assert r.status_code == 400 and melding in r.get_data(as_text=True)
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 0


def test_ugyldig_betalervalg_gir_400_og_manglende_valg_er_privat_som_foer(con):
    _kurs(con)
    r = _klient().post("/kurs/S1", data={**BASIS, "betaler": "staten"})
    assert r.status_code == 400 and "Velg om du betaler privat, eller om firma betaler." in r.get_data(as_text=True)
    assert _klient().post("/kurs/S1", data=BASIS).status_code == 200 and _paamelding(con)["betaler"] == "person"


def test_registeret_svarer_ikke_deltakeren_skriver_aldri_firmaopplysningene_og_paameldingen_merkes(con, monkeypatch):
    """Endret 29.09.2026 (brukerens beslutning): før kunne deltakeren skrive firmanavn og adresse selv når registeret var
    nede. Nå lagres bare organisasjonsnummeret, og påmeldingen merkes «Firmaopplysninger må kontrolleres»
    (tests/test_firmaopplysninger.py har hele forløpet)."""
    kid = _kurs(con)

    def nede(orgnr):
        raise brreg.Utilgjengelig()
    monkeypatch.setattr(brreg, "hent", nede)
    skrevet = ({}, {"org_navn": " Eksempel Klinikk AS ", "org_adresse": "Gata 1", "org_postnr": "1234",
                    "org_sted": "Eksempelby"})
    for i, tillegg in enumerate(skrevet):
        r = _klient().post("/kurs/S1", data=_firma(org_nr="999900070", epost=f"firma{i}@eksempel.no", **tillegg))
        assert r.status_code == 200 and "Vi kunne ikke hente firmaopplysningene akkurat nå" in r.get_data(as_text=True)
    rader = con.execute("SELECT * FROM paamelding ORDER BY id").fetchall()
    assert len(rader) == 2
    for p in rader:                        # bare organisasjonsnummeret - ALDRI det deltakeren skrev
        assert (p["org_nr"], p["org_navn"], p["faktura_adresse"], p["faktura_postnr"], p["faktura_sted"]) == (
            "999900070", None, None, None, None)
    logg = [json.loads(h["detaljer"]) for h in _hendelser(con) if h["handling"] == "enhetsoppslag_utilgjengelig"]
    assert logg == [{"kurs_id": kid, "paamelding_id": p["id"]} for p in rader]        # uten organisasjonsnummer


def test_ingen_oppslag_naar_skjemaet_har_andre_feil(con, monkeypatch):
    _kurs(con)
    monkeypatch.setattr(brreg, "hent", lambda nr: pytest.fail("oppslag ved andre feil"))
    r = _klient().post("/kurs/S1", data=_firma(samtykke=None, org_navn="Vises igjen"))
    assert r.status_code == 400 and 'value="Vises igjen"' in r.get_data(as_text=True)


def test_obligatoriske_fakturafelt_gjelder_bare_riktig_betaler(con):
    """Et felt som bare gjelder firma («Faktura merkes med») kreves aldri av en deltaker som betaler selv."""
    kid = _kurs(con)
    db.lagre_skjemafelt(con, kid, "faktura_ref", {"obligatorisk": True})
    con.commit()
    r = _klient().post("/kurs/S1", data=_firma())
    assert r.status_code == 400 and "Fyll inn «Faktura merkes med»." in r.get_data(as_text=True)
    r = _klient().post("/kurs/S1", data={**BASIS, "betaler": "person"})
    assert r.status_code == 200 and _paamelding(con)["betaler"] == "person"


def test_ugyldig_faktura_epost(con):
    _kurs(con)
    r = _klient().post("/kurs/S1", data=_firma(faktura_epost="ikke-en-adresse"))
    assert r.status_code == 400 and "Skriv en gyldig e-postadresse for faktura." in r.get_data(as_text=True)


# ============================ oppslaget fra skjemaet (/kurs/<kode>/enhet) ============================

def test_oppslag_og_sok(con):
    _kurs(con)
    k = _klient()
    assert k.get("/kurs/S1/enhet?orgnr=999%20900%20011").get_json() == {"status": "funnet", "enhet": {
        "orgnr": "999900011", "navn": "EKSEMPEL KOMMUNE HELSE OG MESTRING", "adresse": "Rådhusgata 1",
        "postnr": "1234", "poststed": "EKSEMPELBY", "underenhet": True}}
    assert k.get("/kurs/S1/enhet?orgnr=999900070").get_json() == {"status": "ikke_funnet"}
    assert k.get("/kurs/S1/enhet?orgnr=123").get_json() == {"status": "ugyldig"}
    assert [e["orgnr"] for e in k.get("/kurs/S1/enhet?sok=sykehus").get_json()["treff"]] == ["999900038"]
    assert k.get("/kurs/S1/enhet?sok=ab").get_json() == {"status": "ok", "treff": []}
    r = k.get("/kurs/S1/enhet?orgnr=999900003")
    assert r.headers["Cache-Control"] == "no-store" and r.mimetype == "application/json"


def test_oppslag_bare_for_kurs_skjemaet_kan_vises(con, admin):
    _kurs(con, kode="UT", status="utkast")
    assert _klient().get("/kurs/UT/enhet?orgnr=999900003").status_code == 404
    assert _klient().get("/kurs/FINNES-IKKE/enhet?orgnr=999900003").status_code == 404
    assert admin.get("/kurs/UT/enhet?orgnr=999900003").get_json()["status"] == "funnet"   # forhåndsvisning


def test_oppslag_registeret_nede_gir_503_og_logger_ingenting(con, monkeypatch):
    _kurs(con)

    def nede(*a):
        raise brreg.Utilgjengelig()
    monkeypatch.setattr(brreg, "hent", nede)
    monkeypatch.setattr(brreg, "sok", nede)
    foer = _hendelser(con)
    assert _klient().get("/kurs/S1/enhet?orgnr=999900003").status_code == 503
    assert _klient().get("/kurs/S1/enhet?sok=eksempel").get_json() == {"status": "utilgjengelig"}
    assert _hendelser(con) == foer


def test_oppslag_er_takbegrenset(con):
    _kurs(con)
    k = _klient()
    maks = __import__("kurs.web.sikkerhet", fromlist=["GRENSER"]).GRENSER["enhetsoppslag"][0]
    for _ in range(maks):
        assert k.get("/kurs/S1/enhet?orgnr=999900003").status_code == 200
    assert k.get("/kurs/S1/enhet?orgnr=999900003").status_code == 429


# ============================ forhåndsvisning = den ekte siden ============================

def test_forhandsvisning_gaar_til_den_ekte_siden(con, admin):
    kid = _kurs(con)
    r = admin.get(f"/admin/kurs/{kid}/forhandsvis-paamelding")
    assert r.status_code == 302 and r.headers["Location"].endswith("/kurs/S1")
    assert _klient().get(f"/admin/kurs/{kid}/forhandsvis-paamelding").status_code == 302      # til innlogging
    assert "/admin/logg-inn" in _klient().get(f"/admin/kurs/{kid}/forhandsvis-paamelding").headers["Location"]
    assert admin.post(f"/admin/kurs/{kid}/forhandsvis-paamelding").status_code == 405
    assert admin.get("/admin/kurs/99999/forhandsvis-paamelding").status_code == 404


def test_admin_ser_offentlig_kurs_uten_banner_og_med_ekte_innsending(con, admin):
    _kurs(con)
    html = _side(klient=admin)
    assert "<button>Meld meg på</button>" in html and "data-ingen-innsending" not in html
    assert "pm-lenke" not in html and "Dette er det ekte påmeldingsskjemaet." not in html
    offentlig = _side()
    assert "pm-lenke" not in offentlig and "Dette er det ekte påmeldingsskjemaet." not in offentlig
    skjema = lambda h: re.sub(r'value="[^"]{20,}"', "", h.split('<form method="post" class="kort pm-skjema"', 1)[1])  # noqa: E731
    assert skjema(html) == skjema(offentlig)                  # nøyaktig samme skjema som deltakerne får


@pytest.mark.parametrize("status", ["utkast", "avsluttet", "avlyst"])
def test_kurs_som_ikke_er_offentlig_ser_bare_admin_og_ingen_kan_sende(con, admin, status):
    _kurs(con, kode="IO", status=status)
    assert _klient().get("/kurs/IO").status_code == 404
    html = _side("IO", admin)
    assert f"Forhåndsvisning – kurset er ikke åpent for påmelding ({status})." in html
    assert "data-ingen-innsending" in html and '<button type="button">Meld meg på</button>' in html
    assert "Bruk bedriftspåmelding" not in html and ">Admin</strong></a>" not in html
    for klient in (_klient(), admin):
        assert klient.post("/kurs/IO", data=BASIS).status_code == 404
    assert con.execute("SELECT COUNT(*) FROM paamelding").fetchone()[0] == 0


def test_forhandsvisning_har_ingen_sideeffekter(con, admin):
    kid = _kurs(con, status="utkast", spesialistlop="EFT")
    _eget(con, kid)
    foer = [con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in (
        "hendelse", "paamelding", "deltaker", "paamelding_svar", "utsending_logg", "faktura", "sensitivt")]
    admin.get(f"/admin/kurs/{kid}/forhandsvis-paamelding", follow_redirects=True)
    admin.get("/kurs/S1")
    assert [con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in (
        "hendelse", "paamelding", "deltaker", "paamelding_svar", "utsending_logg", "faktura", "sensitivt")] == foer


def test_nettsidefanen_har_lenke_og_kopier(con, admin):
    kid = _kurs(con, status="avsluttet")
    html = admin.get(f"/admin/kurs/{kid}/nettside").get_data(as_text=True)
    assert 'id="f-lenke" value="https://kurs.eksempel.no/kurs/S1" readonly' in html and 'data-kopier="f-lenke"' in html
    assert "Kurset er avsluttet, så siden er stengt for deltakerne" in html
    assert admin.get("/kurs/S1").status_code == 200                                 # lenken virker for admin


# ============================ svarene i admin ============================

def _med_svar(con):
    kid = _kurs(con)
    deling = _eget(con, kid, 5, **ekstrafelt.MALER["deling_epost"][1])
    tema = _eget(con, kid, 6, label="Tema", type="flervalg", valg=("A", "B"))
    ubesvart = _eget(con, kid, 7, label="Ubesvart")
    assert _klient().post("/kurs/S1", data={**BASIS, f"ekstra_{deling}": "Ja", f"ekstra_{tema}": ["A", "B"]}).status_code == 200
    return kid, deling, tema, ubesvart, _paamelding(con)["id"]


def test_deltakervinduet_viser_svarene(con, admin):
    kid, *_, pid = _med_svar(con)
    html = admin.get(f"/admin/kurs/{kid}/deltaker/{pid}").get_data(as_text=True)
    seksjon = html.split("<h2>Svar i påmeldingsskjemaet</h2>", 1)[1].split("statusboks", 1)[0]
    # skjemaets rekkefølge: «Om deg» (Tema, Ubesvart) før «Til slutt» (Deling av e-post)
    assert re.findall(r'<span class="etikett">([^<]+)</span>', seksjon) == ["Tema", "Ubesvart", "Deling av e-post"]
    assert "A, B" in seksjon and "Ikke besvart" in seksjon


def test_deltakerlisten_har_svarene_som_valgbare_kolonner(con, admin):
    kid, deling, tema, ubesvart, _ = _med_svar(con)
    html = admin.get(f"/admin/kurs/{kid}/deltakerliste").get_data(as_text=True)
    assert "Svar i påmeldingsskjemaet" in html and f'value="svar_{deling}"' in html
    assert ">Deling av e-post<" not in html.split("<table", 1)[1]            # ikke valgt som standard
    r = admin.get(f"/admin/kurs/{kid}/deltakerliste.csv?valgt=1&status=paameldt&kol=svar_{deling}&kol=svar_{tema}")
    linjer = r.get_data(as_text=True).lstrip("﻿").splitlines()
    assert linjer == ["Navn;Tema;Deling av e-post", "Test Person;A, B;Ja"]
    uten = admin.get(f"/admin/kurs/{kid}/deltakerliste.csv?valgt=1&status=paameldt").get_data(as_text=True)
    assert "Deling av e-post" not in uten
    assert "Svar i påmeldingsskjemaet" not in admin.get(f"/admin/kurs/{_kurs(con, kode='S9')}/deltakerliste").get_data(
        as_text=True)


def test_anonymisering_sletter_svarene(con):
    kid, *_, pid = _med_svar(con)
    con.execute("UPDATE kurs SET status='avsluttet' WHERE id=?", (kid,))
    ut = db.anonymiser_deltaker(con, _paamelding(con)["deltaker_id"], aktor="test")
    assert ut["skjemasvar"] == 2 and db.hent_svar(con, pid) == {}


def test_duplisering_kopierer_egne_felt_med_betingelser_men_ikke_svar(con, admin):
    kid = _kurs(con)
    deling = _eget(con, kid, 5, **ekstrafelt.MALER["deling_epost"][1])
    _eget(con, kid, 6, label="Hvorfor", vis_naar_felt=f"ekstra:{deling}", vis_naar_verdi="Ja", synlig=False)
    _klient().post("/kurs/S1", data={**BASIS, f"ekstra_{deling}": "Ja"})
    r = admin.post("/admin/kurs/ny", data={
        "navn": "Eksempelkurs", "type": "fysisk", "sted": "Eksempelsted", "kapasitet": "10", "datoer": "2099-09-01",
        "start_kl": "09:00", "slutt_kl": "16:00", "timer_pr_dag": "6", "pris_nok": "4500", "fakturering": "person",
        "betaling": "samlet", "faktura_dager_for": "14", "fra": str(kid)})
    assert r.status_code == 302
    ny = con.execute("SELECT id FROM kurs WHERE id != ? ORDER BY id DESC", (kid,)).fetchone()["id"]
    kopi = db.hent_ekstrafelt(con, ny).felt
    assert [(e.label, e.synlig, e.rekkefolge) for e in kopi] == [("Deling av e-post", True, 5), ("Hvorfor", False, 6)]
    assert kopi[1].vis_naar_felt == f"ekstra:{kopi[0].id}" and kopi[0].id != deling
    assert db.antall_svar_per_ekstrafelt(con, ny) == {}
