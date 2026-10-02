"""Adressen MÅ være med ved påmelding, uansett vei inn (brukerens ord 30.09.2026: «de MÅ ha fakturaadresse klar ved påmelding. Derfor skal
det ikke være mulig å ikke legge dette til i påmeldingsskjema»).

Med STANDARD-innstillingene (ingen ADRESSE_KREVES_* i miljøet) er det ikke mulig å registrere en påmelding uten adresse noe sted:
  * det offentlige påmeldingsskjemaet, bedriftspåmeldingen (per deltaker) og «Legg til deltaker» (alltid krav)
  * webhooken fra ipr.no (400) og CSV-importen (kolonnene er påkrevd, og en rad uten adresse blokkeres): PÅ som standard, og bare verdien
    «0» er en nødbrems (testet i test_adresse_valgfri.py)
  * skjemabyggeren lar ingen skjule adressefeltene eller gjøre dem valgfrie (låste felt), heller ikke med en manipulert innsending
  * ingen annen kode oppretter en påmelding: INSERT INTO paamelding finnes bare i db.meld_paa, og alle kall til den tar med adressen
Innstillingene settes i hver test til det en ren installasjon gir (en delprosess uten ADRESSE_KREVES_*), så en feil standard ikke kan
skjules av en test som setter innstillingen selv. Alle testdata er fiktive."""
import ast
import functools
import re
from pathlib import Path

import pytest

from adressehjelp import ADRESSE
from kurs import config, skjemafelt
from test_adresse_personvern import _deltaker_argumenter
from test_adresse_valgfri import _les_innstillinger
from test_admin_paameldingsskjema import _lagre, _rader
from test_adresse_veier import (EPOST, HEADER, _admin, _antall, _csv, _gruppe, _importer, _klient, _kurs, _ny, _nett,  # noqa: F401
                                _webhook, con)

pytestmark = pytest.mark.uten_standardadresse   # registrerer bevisst uten adresse (se conftest.py)

ROT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _demo(monkeypatch):
    monkeypatch.setattr(config, "DEMO", True)


@functools.lru_cache(maxsize=1)
def _standard() -> tuple[bool, bool]:
    webhook, csv = (v == "True" for v in _les_innstillinger().split())
    return webhook, csv


@pytest.fixture
def standard(monkeypatch):
    """Innstillingene slik en ren installasjon har dem (ingen ADRESSE_KREVES_* i miljøet), uansett hva en lokal .env sier."""
    webhook, csv = _standard()
    monkeypatch.setattr(config, "ADRESSE_KREVES_I_WEBHOOK", webhook)
    monkeypatch.setattr(config, "ADRESSE_KREVES_I_CSV", csv)


def test_standarden_er_at_adressen_kreves_i_webhook_og_csv(standard):
    assert _standard() == (True, True)
    assert config.ADRESSE_KREVES_I_WEBHOOK is True and config.ADRESSE_KREVES_I_CSV is True


# ============================ hver vei inn: uten adresse avvises, med adresse går det gjennom ============================

VEIER = ["skjema", "bedriftspaamelding", "legg_til_deltaker", "webhook", "csv_uten_kolonner", "csv_tom_rad"]


def _forsok(vei: str, kid: int, adresse: bool) -> bool:
    """Prøver å registrere Ola Nordmann på kurset A1 via `vei`, med eller uten adressen. Returnerer om påmeldingen ble tatt imot."""
    a = ADRESSE if adresse else {}
    if vei == "skjema":
        r = _klient().post("/kurs/A1", data={"fornavn": "Ola", "etternavn": "Nordmann", "epost": EPOST, "samtykke": "on", **a})
        return r.status_code == 200
    if vei == "bedriftspaamelding":
        return _gruppe(uten_adresse=not adresse).status_code == 302
    if vei == "legg_til_deltaker":
        return _ny(kid, **a).status_code == 302
    if vei == "webhook":
        return _webhook(_nett(**a)).status_code == 201
    if vei == "csv_uten_kolonner":
        # med adresse: en fil med kolonnene (som malen); uten: en fil uten adressekolonnene
        fil = (_csv(f"Ola;Nordmann;{EPOST};" + ";".join(a.values()) + ";person;") if adresse
               else _csv(f"Ola;Nordmann;{EPOST}", header="Fornavn;Etternavn;E-post"))
        return _importer(_admin(), kid, fil)[1] is not None
    rad = f"Ola;Nordmann;{EPOST};" + (";".join(a.values()) if adresse else ";;") + ";person;"
    return _importer(_admin(), kid, _csv(rad))[1] is not None


@pytest.mark.parametrize("vei", VEIER)
def test_ingen_vei_registrerer_en_paamelding_uten_adresse(con, standard, vei):
    kid = _kurs(con)
    assert _forsok(vei, kid, adresse=False) is False, vei
    assert _antall(con) == 0 and _antall(con, "paamelding") == 0             # ingen person og ingen påmelding


@pytest.mark.parametrize("vei", VEIER)
def test_hver_vei_registrerer_paameldingen_naar_adressen_er_med(con, standard, vei):
    """Kontroll: samme forsøk med adresse går gjennom, og adressen ligger på personen (testene over er ikke tomme)."""
    kid = _kurs(con)
    assert _forsok(vei, kid, adresse=True) is True, vei
    assert _antall(con, "paamelding") == 1
    d = con.execute("SELECT adresse, postnr, poststed FROM deltaker WHERE epost=?", (EPOST,)).fetchone()
    assert dict(d) == ADRESSE


def test_webhook_med_delvis_adresse_avvises_med_standardinnstillingene(con, standard):
    _kurs(con)
    r = _webhook(_nett(adresse="Eksempelveien 1"))
    assert r.status_code == 400 and r.get_json()["melding"].endswith("postnr, poststed") and _antall(con, "paamelding") == 0


# ============================ skjemabyggeren: adressefeltene er låst ============================

def test_skjemabyggeren_kan_ikke_skjule_adressen_eller_gjoere_den_valgfri_ogsaa_med_manipulert_innsending(con, standard):
    kid = _kurs(con)
    felt = (*skjemafelt.ADRESSEFELT, "faktura_adresse", "faktura_postnr", "faktura_sted")
    manip = {f"{f}_{e}": v for f in felt for e, v in (("synlig", ""), ("obligatorisk", ""), ("label", "HACK"), ("hjelpetekst", "HACK"))}
    assert _lagre(_admin(), kid, **manip).status_code == 302
    assert _rader(con, kid) == []                                             # ingen overstyring lagret for noe felt
    html = _klient().get("/kurs/A1").get_data(as_text=True)
    for f in skjemafelt.ADRESSEFELT:
        assert f'name="{f}"' in html and "HACK" not in html, f                  # feltene vises fortsatt, med fast tekst
    assert _forsok("skjema", kid, adresse=False) is False and _antall(con, "paamelding") == 0


# ============================ vakt: ingen annen vei oppretter en påmelding ============================

def _kall_til(navn: str) -> dict[str, int]:
    """Antall kall til `navn` (som metode eller funksjon) i hver fil under kurs/ - AST, ikke tekstsøk."""
    ut = {}
    for fil in sorted((ROT / "kurs").rglob("*.py")):
        n = sum(1 for k in ast.walk(ast.parse(fil.read_text(encoding="utf-8")))
                if isinstance(k, ast.Call) and getattr(k.func, "attr", getattr(k.func, "id", None)) == navn)
        if n:
            ut[fil.relative_to(ROT).as_posix()] = n
    return ut


def test_paameldinger_opprettes_bare_i_db_meld_paa():
    innsetting = re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+paamelding\s*\(", re.I)
    treff = []
    for fil in sorted((ROT / "kurs").rglob("*.py")):
        tekst = fil.read_text(encoding="utf-8")
        for m in innsetting.finditer(tekst):
            treff.append((fil.relative_to(ROT).as_posix(), tekst[:m.start()].rsplit("\ndef ", 1)[1].split("(")[0]))
    assert treff == [("kurs/db.py", "meld_paa")], treff                      # aldri en påmelding uten å gå via meld_paa


def test_alle_kall_til_meld_paa_tar_med_adressen():
    """Nye kall må ta stilling til adressen: tallene under må oppdateres bevisst, og hvert deltaker=-argument må ha adressen."""
    antall = _kall_til("meld_paa")
    assert antall == {"kurs/import_deltakere.py": 1, "kurs/seed_demo.py": 1, "kurs/web/app.py": 4}, antall
    for fil in antall:
        for argument in _deltaker_argumenter(ROT / fil):
            assert re.search(r"rens_adresse|ADRESSEFELT|'adresse': adresse", argument), f"{fil}: {argument[:200]}"
