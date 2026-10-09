"""Visma Business NXT, fase 1: KUN lesing av fakturastatus (kurs/integrasjoner/business_nxt.py, kurs/bnxt_sjekk.py).

All kontakt med Visma er etterlignet (requests.post byttes ut) - testene gjoer aldri nettverkskall. Fakturanumre,
beloep og selskaper er oppdiktede.
"""
import json
import logging
import time
from datetime import date
from decimal import Decimal
from urllib.parse import quote

import pytest
import requests

from kurs import bnxt_sjekk, config, db
from kurs.integrasjoner import business_nxt as bnxt

HEMMELIGHET = "hemmelig verdi/+&=123"        # med tegn som maa url-kodes i Basic Auth-hodet
TOKEN = "tilgangstoken-abc-123"
IDAG = date(2026, 9, 26)
NBSP = " "


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UTBOKS", tmp_path / "utboks")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DB_STI", tmp_path / "test.db")
    c = db.koble(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _tom_buffer(monkeypatch):
    monkeypatch.setattr(bnxt, "_cache", {})


@pytest.fixture
def ingen_nett(monkeypatch):
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(requests, "post", lambda *a, **kw: pytest.fail("nettverkskall i demo"))


def _svar(status: int, kropp) -> requests.Response:
    r = requests.Response()
    r.status_code, r._content = status, json.dumps(kropp).encode()
    return r


def _token_ok(scope=bnxt.SCOPE, token=TOKEN):
    return 200, {"access_token": token, "expires_in": 3600, "token_type": "Bearer", "scope": scope}


def _rad(nr="10001", belop=4900, rest=0, kreditert=0, forfall=20260915, fakturadato=20260901, betalt=20260910,
         purringer=0, siste_purring=0, inkasso="", kundenr=10023):
    return {"invoiceNo": nr, "customerNo": kundenr, "voucherDate": fakturadato, "dueDate": forfall, "amountDomestic": belop,
            "outstandingAmountDomestic": rest, "paidStatus": 4, "paidLast": betalt, "creditedAmountDomestic": kreditert,
            "noOfRemindersSent": purringer, "lastReminderDate": siste_purring, "debtCollectionStatus": inkasso,
            "orderNo": 555}


def _data(*rader):
    return 200, {"data": {"useCompany": {"customerTransaction": {"items": list(rader)}}}}


class FalskVisma:
    """Etterligner Visma Connect (token) og GraphQL-API-et. Svarene gis i rekkefoelge per adresse."""

    def __init__(self, token=(), graphql=()):
        self.token_svar, self.graphql_svar = list(token), list(graphql)
        self.token_kall, self.graphql_kall = [], []

    def __call__(self, url, data=None, json=None, headers=None, auth=None, timeout=None, **kw):
        if url == config.BNXT_TOKEN_URL:
            self.token_kall.append({"data": data, "auth": auth, "headers": headers, "json": json})
            neste = self.token_svar.pop(0) if self.token_svar else _token_ok()
        elif url == config.BNXT_API:
            self.graphql_kall.append({"json": json, "headers": headers, "data": data, "auth": auth})
            neste = self.graphql_svar.pop(0)
        else:
            pytest.fail(f"uventet adresse: {url}")
        if isinstance(neste, Exception):
            raise neste
        return _svar(*neste)


@pytest.fixture
def drift(monkeypatch):
    monkeypatch.setattr(config, "DEMO", False)
    monkeypatch.setattr(config, "BNXT_CLIENT_ID", "isv_testklient")
    monkeypatch.setattr(config, "BNXT_CLIENT_SECRET", HEMMELIGHET)
    monkeypatch.setattr(config, "BNXT_SELSKAP", "1234567")
    monkeypatch.setattr(config, "BNXT_KUNDENR", "7654321")
    pauser = []
    monkeypatch.setattr(bnxt.time, "sleep", pauser.append)

    def server(**kw):
        falsk = FalskVisma(**kw)
        falsk.pauser = pauser
        monkeypatch.setattr(requests, "post", falsk)
        return falsk
    return server


# ============================ demo: ingen nettverkskall ============================

def test_demo_gjoer_ingen_nettverkskall_og_gir_faste_statuser(ingen_nett):
    ut = bnxt.hent_fakturastatus({"D1001": 4900, "D1002": 4900}, idag=IDAG)
    assert set(ut) == {"D1001", "D1002"}
    assert all(o.demo and o.status and o.status.belop == Decimal(4900) and o.avvik is None for o in ut.values())
    assert bnxt.hent_fakturastatus({"D1001": 4900}, idag=IDAG)["D1001"].status == ut["D1001"].status  # samme hver gang
    assert bnxt.sjekk_tilgang()["scope"] == bnxt.SCOPE
    assert bnxt.tilgjengelige_selskaper("123") and bnxt.antall_kundetransaksjoner() == 0


def test_demo_dekker_alle_statusene_og_er_innbyrdes_konsistent(ingen_nett):
    sett = {}
    for i in range(1, 60):
        s = bnxt.hent_fakturastatus({f"D{i}": 3000}, idag=IDAG)[f"D{i}"].status
        assert s.status == bnxt._utled_status(s.belop, s.utestaende, s.kreditert, s.forfallsdato, IDAG)
        sett[s.status] = True
    assert set(sett) == {"Betalt", "Ubetalt", "Forfalt", "Delvis betalt", "Kreditert"}


def test_tomme_og_blanke_fakturanumre_gir_ingen_kall(drift):
    server = drift(graphql=[])
    assert bnxt.hent_fakturastatus({None: 1, "": 2, "  ": 3}) == {}
    assert server.token_kall == [] and server.graphql_kall == []


# ============================ tilgang: scope og hemmelighet ============================

def test_token_bes_om_med_scopet_for_bare_lesing_og_basic_auth(drift):
    server = drift(graphql=[_data(_rad())])
    bnxt.hent_fakturastatus({"10001": 4900}, idag=IDAG)
    (kall,) = server.token_kall
    assert kall["data"] == {"grant_type": "client_credentials", "scope": "business-graphql-service-api:access-group-based-readonly"}
    assert kall["auth"] == ("isv_testklient", quote(HEMMELIGHET, safe=""))          # Basic Auth, url-kodet
    assert HEMMELIGHET not in json.dumps(kall["data"]) and kall["json"] is None
    (gql,) = server.graphql_kall
    assert gql["headers"] == {"Authorization": f"Bearer {TOKEN}"} and gql["auth"] is None


def test_scopet_er_fast_i_koden_og_ikke_en_innstilling():
    assert bnxt.SCOPE.endswith("-readonly")
    assert not any("SCOPE" in navn for navn in vars(config))


def test_tokenet_gjenbrukes_og_fornyes_naar_det_nesten_er_utloept(drift):
    server = drift(graphql=[_data(_rad()), _data(_rad("10002")), _data(_rad("10003"))])
    bnxt.hent_fakturastatus({"10001": None}, ekte=True)
    bnxt.hent_fakturastatus({"10002": None}, ekte=True)
    assert len(server.token_kall) == 1
    bnxt._cache["utloper"] = time.time() + 30                                          # under ett minutt igjen
    bnxt.hent_fakturastatus({"10003": None}, ekte=True)
    assert len(server.token_kall) == 2


@pytest.mark.parametrize("feltnavn", ["BNXT_CLIENT_ID", "BNXT_CLIENT_SECRET"])
def test_manglende_oppsett_gir_forstaaelig_feil_uten_nettverkskall(drift, monkeypatch, feltnavn):
    server = drift(graphql=[])
    monkeypatch.setattr(config, feltnavn, "")
    with pytest.raises(bnxt.BnxtFeil, match="ikke satt opp"):
        bnxt.hent_fakturastatus({"10001": 1})
    assert server.token_kall == []


def test_manglende_selskap_gir_forstaaelig_feil(drift, monkeypatch):
    drift(graphql=[])
    monkeypatch.setattr(config, "BNXT_SELSKAP", "")
    with pytest.raises(bnxt.BnxtFeil, match="BNXT_SELSKAP"):
        bnxt.hent_fakturastatus({"10001": 1})


@pytest.mark.parametrize("svar, forventet", [
    ((400, {"error": "invalid_scope", "error_description": f"detaljer {HEMMELIGHET}"}), "readonly"),
    ((401, {"error": "invalid_client"}), "invalid_client"),
    ((500, {"noe": "annet"}), "HTTP 500"),
    (requests.ConnectionError("https://connect.visma.com/..."), "Fikk ikke kontakt med Visma Connect"),
])
def test_tokenfeil_gir_forstaaelig_melding_uten_detaljer(drift, svar, forventet):
    drift(token=[svar], graphql=[])
    with pytest.raises(bnxt.BnxtFeil) as feil:
        bnxt.hent_fakturastatus({"10001": 1})
    assert forventet in str(feil.value)
    assert HEMMELIGHET not in str(feil.value) and "detaljer" not in str(feil.value)


# ============================ bare lesing ============================

def test_sperren_avviser_mutasjoner_foer_noe_sendes(drift):
    server = drift(graphql=[])
    for dokument in ("mutation m { useCompany(no: 1) { order_create(values: []) { affectedRows } } }",
                     "query q { x } mutation m { y }", "{ useCompany(no: 1) { order { totalCount } } }"):
        with pytest.raises(bnxt.BnxtFeil, match="Bare lesing"):
            bnxt._graphql(dokument, {})
    assert server.token_kall == [] and server.graphql_kall == []


def test_alle_graphql_dokumentene_i_modulen_er_rene_spoerringer():
    dokumenter = [v for k, v in vars(bnxt).items() if isinstance(v, str) and ("useCompany" in v or "availableCompanies" in v)]
    assert len(dokumenter) == 3
    for d in dokumenter:
        assert d.lstrip().startswith("query ") and "mutation" not in d.lower() and "_create" not in d and "_update" not in d


def test_fakturanumre_sendes_bare_som_variabler(drift):
    ondsinnet = 'x"]}}) { mutation'
    server = drift(graphql=[_data(_rad("10001"), _rad("10002"))])
    bnxt.hent_fakturastatus({"10002": None, "10001": None, ondsinnet: None}, idag=IDAG)
    (kall,) = server.graphql_kall
    assert kall["json"]["query"] == bnxt._FAKTURASTATUS                          # dokumentet er alltid det faste
    assert kall["json"]["variables"] == {"selskap": 1234567, "fakturanr": sorted(["10001", "10002", ondsinnet])}


# ============================ tolking av svaret ============================

@pytest.mark.parametrize("rad, status", [
    (_rad(rest=0), "Betalt"),
    (_rad(rest=0, kreditert=4900), "Kreditert"),
    (_rad(rest=0, kreditert=900), "Betalt"),                                   # delvis kreditert, resten betalt
    (_rad(rest=2000, forfall=20261010, betalt=20260920), "Delvis betalt"),
    (_rad(rest=4900, forfall=20261010, betalt=0), "Ubetalt"),
    (_rad(rest=4900, forfall=20260915, betalt=0), "Forfalt"),
    (_rad(rest=2000, forfall=20260915), "Forfalt"),
])
def test_status_utledes_fra_utestaaende_kreditert_og_forfall(drift, rad, status):
    drift(graphql=[_data(rad)])
    assert bnxt.hent_fakturastatus({"10001": 4900}, idag=IDAG)["10001"].status.status == status


def test_felt_og_datoer_tolkes(drift):
    drift(graphql=[_data(_rad(rest=4900, forfall=20260915, betalt=0, purringer=2, siste_purring=20260922,
                               inkasso="Sendt til inkasso"))])
    s = bnxt.hent_fakturastatus({"10001": 4900}, idag=IDAG)["10001"].status
    assert (s.fakturanr, s.kundenr, s.fakturadato, s.forfallsdato) == ("10001", 10023, date(2026, 9, 1), date(2026, 9, 15))
    assert (s.belop, s.utestaende, s.kreditert, s.sist_betalt) == (Decimal(4900), Decimal(4900), Decimal(0), None)
    assert (s.antall_purringer, s.siste_purring, s.inkasso) == (2, date(2026, 9, 22), "Sendt til inkasso")


def test_innbetalinger_med_samme_nummer_ignoreres_og_flere_fakturarader_gir_uklar_kobling(drift):
    innbetaling = _rad(belop=-4900, rest=0)
    drift(graphql=[_data(_rad(), innbetaling), _data(_rad(), _rad()), _data(innbetaling)])
    assert bnxt.hent_fakturastatus({"10001": 4900}, ekte=True)["10001"].status.status == "Betalt"
    o = bnxt.hent_fakturastatus({"10001": 4900}, ekte=True)["10001"]
    assert o.status is None and o.problem.startswith("Uklar kobling")
    o = bnxt.hent_fakturastatus({"10001": 4900}, ekte=True)["10001"]
    assert o.status is None and o.problem == "Fant ikke fakturaen i Business NXT."


def test_avvik_naar_beloepet_ikke_stemmer_med_vaar_faktura(drift):
    drift(graphql=[_data(_rad("10001", belop=4900), _rad("10002", belop=4500))])
    ut = bnxt.hent_fakturastatus({"10001": 4500, "10002": 4500}, idag=IDAG)
    assert ut["10002"].avvik is None
    assert f"4{NBSP}900{NBSP}kr" in ut["10001"].avvik and f"4{NBSP}500{NBSP}kr" in ut["10001"].avvik


def test_svar_gjenbrukes_i_appen_men_tilkoblingstesten_henter_alltid(drift):
    server = drift(graphql=[_data(_rad()), _data(_rad())])
    bnxt.hent_fakturastatus({"10001": 4900})
    bnxt.hent_fakturastatus({"10001": 4900})
    assert len(server.graphql_kall) == 1
    bnxt.hent_fakturastatus({"10001": 4900}, ekte=True)
    assert len(server.graphql_kall) == 2


def test_kroner_med_og_uten_oere():
    assert bnxt.kroner(Decimal("4900")) == f"4{NBSP}900{NBSP}kr"
    assert bnxt.kroner(Decimal("4900.5")) == f"4{NBSP}900,50{NBSP}kr"
    assert bnxt.kroner(1234567) == f"1{NBSP}234{NBSP}567{NBSP}kr"


# ============================ feilhaandtering ============================

def test_errors_i_svaret_gir_feil_med_kode_og_detaljer_men_aldri_hemmeligheter(drift):
    drift(graphql=[(200, {"errors": [{"message": "Cannot query field 'x' on type 'CustomerTransaction'.",
                                      "extensions": {"code": "FIELDS_ON_CORRECT_TYPE"}}], "data": None})])
    with pytest.raises(bnxt.BnxtFeil) as feil:
        bnxt.hent_fakturastatus({"10001": 1})
    assert "FIELDS_ON_CORRECT_TYPE" in str(feil.value) and "Cannot query field" not in str(feil.value)
    assert feil.value.detaljer == ("Cannot query field 'x' on type 'CustomerTransaction'.",)
    assert TOKEN not in str(feil.value) and HEMMELIGHET not in str(feil.value)


def test_401_gir_ett_nytt_forsoek_med_nytt_token(drift):
    server = drift(token=[_token_ok(token="gammelt"), _token_ok(token="nytt")],
                   graphql=[(401, {}), _data(_rad())])
    assert bnxt.hent_fakturastatus({"10001": 4900}, idag=IDAG)["10001"].status.status == "Betalt"
    assert [k["headers"]["Authorization"] for k in server.graphql_kall] == ["Bearer gammelt", "Bearer nytt"]


def test_401_to_ganger_gir_feil(drift):
    drift(graphql=[(401, {}), (401, {})])
    with pytest.raises(bnxt.BnxtFeil, match="HTTP 401"):
        bnxt.hent_fakturastatus({"10001": 1})


@pytest.mark.parametrize("foerste", [
    (429, {}),
    (200, {"errors": [{"message": "Company 1234567 has 60 requests in progress. Limit is 60.",
                       "extensions": {"code": "OUTSTANDING_LIMIT_EXCEEDED", "limit": 60}}]}),
])
def test_for_mange_forespoersler_gir_ett_nytt_forsoek_etter_en_pause(drift, foerste):
    server = drift(graphql=[foerste, _data(_rad())])
    assert bnxt.hent_fakturastatus({"10001": 4900}, idag=IDAG)["10001"].status
    assert server.pauser == [bnxt.PAUSE_SEK] and len(server.graphql_kall) == 2


def test_429_to_ganger_gir_feil(drift):
    drift(graphql=[(429, {}), (429, {})])
    with pytest.raises(bnxt.BnxtFeil, match="429"):
        bnxt.hent_fakturastatus({"10001": 1})


@pytest.mark.parametrize("svar, forventet", [
    ((502, {}), "HTTP 502"),
    (requests.Timeout("https://business.visma.net/api/graphql-service"), "Fikk ikke kontakt med Business NXT (Timeout)"),
])
def test_nettverks_og_serverfeil_gir_forstaaelig_feil(drift, svar, forventet):
    drift(graphql=[svar])
    with pytest.raises(bnxt.BnxtFeil) as feil:
        bnxt.hent_fakturastatus({"10001": 1})
    assert forventet in str(feil.value) and feil.value.__cause__ is None


def test_hemmelighet_og_token_havner_aldri_i_logg_eller_database(drift, con, caplog):
    caplog.set_level(logging.DEBUG)
    drift(graphql=[_data(_rad()), (200, {"errors": [{"message": "feil"}]})])
    bnxt.hent_fakturastatus({"10001": 4900}, ekte=True)
    with pytest.raises(bnxt.BnxtFeil):
        bnxt.hent_fakturastatus({"10001": 4900}, ekte=True)
    assert HEMMELIGHET not in caplog.text and TOKEN not in caplog.text
    assert con.execute("SELECT COUNT(*) FROM integrasjon_token").fetchone()[0] == 0


# ============================ tilkoblingstesten ============================

def _selskaper_ok():
    return 200, {"data": {"availableCompanies": {"totalCount": 1, "items": [
        {"name": "Oppdiktet Institutt AS", "vismaNetCompanyId": 1234567}]}}}


def _antall_ok(n=42):
    return 200, {"data": {"useCompany": {"customerTransaction": {"totalCount": n}}}}


def test_tilkoblingstesten_uten_oppsett_gir_1_uten_nettverkskall(monkeypatch, capsys, ingen_nett):
    monkeypatch.setattr(config, "BNXT_CLIENT_ID", "")
    monkeypatch.setattr(config, "BNXT_CLIENT_SECRET", "")
    assert bnxt_sjekk.kjor([]) == 1
    assert "MANGLER" in capsys.readouterr().out


def test_tilkoblingstesten_leser_ekte_ogsaa_i_demo_og_viser_aldri_hemmeligheten(drift, monkeypatch, capsys):
    server = drift(graphql=[_selskaper_ok(), _antall_ok(), _data(_rad(rest=4900, forfall=20260915, betalt=0,
                                                                     purringer=1, siste_purring=20260922))])
    monkeypatch.setattr(config, "DEMO", True)                     # sandkassen staar i demo - testen ber selv om ekte lesing
    assert bnxt_sjekk.kjor(["--faktura", "10001"]) == 0
    ut = capsys.readouterr().out
    assert "satt (vises ikke)" in ut and "Tilgang OK (bare lesing)" in ut
    assert "Oppdiktet Institutt AS" in ut and "1234567" in ut and "Lesetilgang OK: 42" in ut
    assert "Faktura 10001" in ut and "Forfall:      15.09.2026" in ut and "Purringer:    1 (siste 22.09.2026)" in ut
    assert HEMMELIGHET not in ut and TOKEN not in ut
    assert len(server.token_kall) == 1 and len(server.graphql_kall) == 3


def test_tilkoblingstesten_stopper_hvis_tilgangen_ikke_er_bare_lesing(drift, capsys):
    drift(token=[_token_ok(scope="business-graphql-service-api:access-group-based")], graphql=[])
    assert bnxt_sjekk.kjor([]) == 2
    assert "annet scope enn bare lesing" in capsys.readouterr().out


def test_tilkoblingstesten_viser_business_nxt_sine_feiltekster_i_terminalen(drift, capsys, monkeypatch):
    monkeypatch.setattr(config, "BNXT_KUNDENR", "")
    drift(graphql=[_antall_ok(), (200, {"errors": [{"message": "Variable '$fakturanr' is invalid.",
                                                   "extensions": {"code": "INVALID_VALUE"}}]})])
    assert bnxt_sjekk.kjor(["--faktura", "10001"]) == 3
    ut = capsys.readouterr().out
    assert "STOPP: Business NXT avviste oppslaget (INVALID_VALUE)." in ut and "Business NXT: Variable '$fakturanr'" in ut


def test_tilkoblingstesten_uten_selskap_ber_om_aa_velge_et(drift, monkeypatch, capsys):
    monkeypatch.setattr(config, "BNXT_SELSKAP", "")
    drift(graphql=[_selskaper_ok()])
    assert bnxt_sjekk.kjor([]) == 0
    assert "BNXT_SELSKAP er ikke satt" in capsys.readouterr().out




def test_demo_lister_visma_net_kunder_uten_nettverk():
    assert bnxt.tilgjengelige_kunder() == [{"navn": "Demokunde AS", "kundenr": 7654321}]


def test_kundelisten_leser_vismanetcustomerid(monkeypatch):
    monkeypatch.setattr(bnxt, "_graphql", lambda dok, var: {"availableCustomers": {"items": [{"name": "IPR", "vismaNetCustomerId": 42}]}})
    assert bnxt.tilgjengelige_kunder(ekte=True) == [{"navn": "IPR", "kundenr": 42}]
    assert bnxt._KUNDER.lstrip().startswith("query")
