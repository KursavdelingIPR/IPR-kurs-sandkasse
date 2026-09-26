"""Visma Business NXT - fase 1: KUN lesing av fakturastatus (GraphQL-API, klient-legitimasjon).

Business NXT er fasiten for betalingsstatus. Denne modulen LESER bare: den lager, endrer eller sletter aldri noe i
Business NXT, og den endrer aldri faktura-tabellen i paameldingssystemet. Tre lag sikrer det:
  1. Tilgangstokenet bes om med scopet for bare lesing (SCOPE) - fast i koden, ikke en innstilling.
  2. Alle GraphQL-dokumentene er faste konstanter som begynner med «query». _graphql() avviser alt annet og alt som
     inneholder «mutation». Fakturanumre sendes KUN som variabler og flettes aldri inn i dokumentet.
  3. I Business NXT boer tjenesten ha en tilgangsgruppe som bare kan lese (tabellen «Connect Application Access»).

Tilgang: klient-id og -hemmelighet (BNXT_CLIENT_ID / BNXT_CLIENT_SECRET) sendes i Basic Auth-hodet til Visma Connect.
Klient-legitimasjon gir ikke noe refresh-token, saa ingenting lagres i databasen; tilgangstokenet holdes bare i minnet.
Hemmeligheten og tokenet skrives aldri ut, logges aldri og tas aldri med i feilmeldinger (BnxtFeil).

Demo (MODUS=demo): faste, oppdiktede statuser uten nettverkskall (CLAUDE.md regel 3). Ekte oppslag skjer bare i drift -
og i tilkoblingstesten (python -m kurs.bnxt_sjekk), som selv ber om ekte lesing med ekte=True.

Felt: GraphQL-navnene («API identifier») fra Business NXT-dokumentasjonen, tabellen customerTransaction (CustTr), som
har alle posteringer paa kunder. openCustomerEntry brukes ikke: der slettes en post naar den er ferdig oppgjort.
"""
import time
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import requests

from .. import config

SCOPE = "business-graphql-service-api:access-group-based-readonly"
TIDSAVBRUDD_SEK = 15
PAUSE_SEK = 2                    # ventetid foer ett nytt forsoek ved for mange forespoersler (HTTP 429 / OUTSTANDING_...)
BUFFER_SEK = 60                  # samme faktura hentes hoeyst en gang i minuttet i appen; tilkoblingstesten henter alltid

_cache: dict = {}                # token, utloper, scope - og fakturarader: {"rad:<nr>": (tidspunkt, rader)}

_FAKTURASTATUS = """query fakturastatus($selskap: Int!, $fakturanr: [String]) {
  useCompany(no: $selskap) {
    customerTransaction(filter: {invoiceNo: {_in: $fakturanr}}) {
      items {
        invoiceNo customerNo voucherDate dueDate amountDomestic outstandingAmountDomestic paidStatus paidLast
        creditedAmountDomestic noOfRemindersSent lastReminderDate debtCollectionStatus orderNo
      }
    }
  }
}"""

_ANTALL_TRANSAKSJONER = """query antall($selskap: Int!) {
  useCompany(no: $selskap) {
    customerTransaction(first: 1) { totalCount }
  }
}"""

_SELSKAPER = """query selskaper($kundenr: Int!) {
  availableCompanies(customerNo: $kundenr) {
    totalCount
    items { name vismaNetCompanyId }
  }
}"""


class BnxtFeil(RuntimeError):
    """Oppslaget mot Business NXT feilet. Meldingen er trygg aa vise og logge: aldri token, hemmelighet eller persondata.

    `detaljer` er Business NXT sine egne feiltekster (f.eks. et ukjent feltnavn). De vises KUN i tilkoblingstesten, i
    terminalen til den som kjoerer den - aldri i appen, i en logg eller i databasen."""

    def __init__(self, melding: str, detaljer: tuple[str, ...] = ()):
        super().__init__(melding)
        self.detaljer = detaljer


@dataclass(frozen=True)
class Fakturastatus:
    fakturanr: str
    kundenr: int | None
    fakturadato: date | None
    forfallsdato: date | None
    belop: Decimal
    utestaende: Decimal
    kreditert: Decimal
    sist_betalt: date | None          # datoen for siste innbetaling (paidLast)
    antall_purringer: int
    siste_purring: date | None
    inkasso: str | None
    status: str                       # Betalt, Kreditert, Delvis betalt, Ubetalt eller Forfalt


@dataclass(frozen=True)
class Oppslag:
    """Resultatet for ett fakturanummer. Enten `status`, eller `problem` (ikke funnet / uklar kobling)."""
    fakturanr: str
    status: Fakturastatus | None = None
    problem: str | None = None
    avvik: str | None = None          # beloepet i Business NXT er ikke det samme som paa vaar faktura
    hentet: datetime | None = None
    demo: bool = False


# ============================ offentlige funksjoner ============================

def hent_fakturastatus(forventet: dict, *, idag: date | None = None, ekte: bool = False) -> dict[str, Oppslag]:
    """Status i Business NXT for fakturanumrene i `forventet` ({fakturanr: vaart beloep i kroner eller None}).

    Ett GraphQL-kall for alle numrene. Vaart beloep brukes til kontrollen av koblingen (avvik) og i demo. Kaster
    BnxtFeil hvis Business NXT ikke er satt opp eller ikke svarer som forventet. `ekte=True` brukes KUN av
    tilkoblingstesten: den leser fra det ekte Business NXT ogsaa mens sandkassen staar i demo."""
    idag = idag or date.today()
    numre = sorted({str(n).strip() for n in forventet if n is not None and str(n).strip()})
    if not numre:
        return {}
    if config.DEMO and not ekte:
        naa = datetime.now()
        return {nr: _med_avvik(Oppslag(nr, status=_demo_status(nr, _forventet_belop(forventet, nr), idag),
                                       hentet=naa, demo=True), forventet, nr) for nr in numre}
    rader, hentet = _fakturarader(numre, bruk_buffer=not ekte)
    ut = {}
    for nr in numre:
        status, problem = _tolk(nr, rader.get(nr, []), idag)
        ut[nr] = _med_avvik(Oppslag(nr, status=status, problem=problem, hentet=hentet), forventet, nr)
    return ut


def sjekk_tilgang(*, ekte: bool = False) -> dict:
    """Henter et nytt tilgangstoken (tilkoblingstesten). Returnerer scopet Visma Connect ga og hvor mange minutter
    tokenet varer - aldri selve tokenet."""
    if config.DEMO and not ekte:
        return {"scope": SCOPE, "minutter": 60}
    _nytt_token()
    return {"scope": _cache.get("scope") or "", "minutter": max(0, int((_cache["utloper"] - time.time()) // 60))}


def tilgjengelige_selskaper(kundenr: str, *, ekte: bool = False) -> list[dict]:
    """Selskapene tjenesten har tilgang til hos Visma.net-kunden: [{"navn", "selskaps_id"}] (tilkoblingstesten)."""
    if config.DEMO and not ekte:
        return [{"navn": "Demoselskap AS", "selskaps_id": 1234567}]
    data = _graphql(_SELSKAPER, {"kundenr": _heltall(kundenr, "BNXT_KUNDENR")})
    items = ((data.get("availableCompanies") or {}).get("items")) or []
    return [{"navn": i.get("name") or "", "selskaps_id": i.get("vismaNetCompanyId")} for i in items]


def antall_kundetransaksjoner(*, ekte: bool = False) -> int:
    """Antall rader i customerTransaction i selskapet - bekrefter lesetilgangen (tilkoblingstesten)."""
    if config.DEMO and not ekte:
        return 0
    data = _graphql(_ANTALL_TRANSAKSJONER, {"selskap": _selskap()})
    return int((((data.get("useCompany") or {}).get("customerTransaction")) or {}).get("totalCount") or 0)


def kroner(belop) -> str:
    """Decimal('4900.5') -> «4 900,50 kr», Decimal('4900') -> «4 900 kr» (hardt mellomrom, som filteret kr)."""
    b = _belop(belop)
    tekst = f"{b:,.2f}" if b != b.to_integral_value() else f"{b:,.0f}"
    return tekst.replace(",", " ").replace(".", ",") + " kr"


# ============================ tolking av svaret ============================

def _tolk(nr: str, rader: list[dict], idag: date) -> tuple[Fakturastatus | None, str | None]:
    """Fakturaraden er den med positivt beloep; innbetalinger og kreditnotaer med samme nummer har negativt beloep og
    er allerede trukket fra i utestaaende beloep. Flere fakturarader med samme nummer: vi gjetter ikke."""
    fakturarader = [r for r in rader if _belop(r.get("amountDomestic")) > 0]
    if not fakturarader:
        return None, "Fant ikke fakturaen i Business NXT."
    if len(fakturarader) > 1:
        return None, "Uklar kobling – flere fakturaer i Business NXT har dette nummeret. Kontroller i Business NXT."
    r = fakturarader[0]
    belop, utestaende, kreditert = (_belop(r.get(f)) for f in
                                    ("amountDomestic", "outstandingAmountDomestic", "creditedAmountDomestic"))
    forfall = _dato(r.get("dueDate"))
    return Fakturastatus(
        fakturanr=nr, kundenr=_heltall_eller_none(r.get("customerNo")), fakturadato=_dato(r.get("voucherDate")),
        forfallsdato=forfall, belop=belop, utestaende=utestaende, kreditert=kreditert, sist_betalt=_dato(r.get("paidLast")),
        antall_purringer=_heltall_eller_none(r.get("noOfRemindersSent")) or 0,
        siste_purring=_dato(r.get("lastReminderDate")), inkasso=(r.get("debtCollectionStatus") or "").strip() or None,
        status=_utled_status(belop, utestaende, kreditert, forfall, idag)), None


def _utled_status(belop: Decimal, utestaende: Decimal, kreditert: Decimal, forfall: date | None, idag: date) -> str:
    if utestaende <= 0:
        return "Kreditert" if kreditert > 0 and kreditert >= belop else "Betalt"
    if forfall and forfall < idag:
        return "Forfalt"
    return "Delvis betalt" if utestaende < belop else "Ubetalt"


def _med_avvik(o: Oppslag, forventet: dict, nr: str) -> Oppslag:
    vaart = _forventet_belop(forventet, nr)
    if o.status is None or vaart is None or o.status.belop == Decimal(vaart):
        return o
    return Oppslag(o.fakturanr, status=o.status, problem=o.problem, hentet=o.hentet, demo=o.demo,
                   avvik=f"Beløpet i Business NXT ({kroner(o.status.belop)}) er ikke det samme som på påmeldingens "
                         f"faktura ({kroner(vaart)}). Kontroller koblingen.")


def _forventet_belop(forventet: dict, nr: str) -> int | None:
    for n, belop in forventet.items():
        if n is not None and str(n).strip() == nr:
            return belop
    return None


def _dato(verdi) -> date | None:
    """Business NXT lagrer datoer som heltall ååååmmdd; 0 betyr ingen dato."""
    v = _heltall_eller_none(verdi)
    if not v or v <= 0:
        return None
    try:
        return date(v // 10000, v // 100 % 100, v % 100)
    except ValueError:
        return None


def _belop(verdi) -> Decimal:
    try:
        return Decimal(str(verdi)) if verdi is not None else Decimal(0)
    except InvalidOperation:
        return Decimal(0)


def _heltall_eller_none(verdi) -> int | None:
    try:
        return int(verdi)
    except (TypeError, ValueError):
        return None


def _heltall(verdi, navn: str) -> int:
    v = _heltall_eller_none(str(verdi or "").strip())
    if v is None:
        raise BnxtFeil(f"Business NXT er ikke satt opp: {navn} mangler eller er ikke et tall.")
    return v


def _selskap() -> int:
    return _heltall(config.BNXT_SELSKAP, "BNXT_SELSKAP")


# ============================ demo ============================

def _demo_status(nr: str, vaart_belop: int | None, idag: date) -> Fakturastatus:
    """Faste, oppdiktede statuser - samme fakturanummer gir alltid samme variant. Ingen nettverkskall."""
    belop = Decimal(vaart_belop or 1000)
    d = lambda dager: idag + timedelta(days=dager)  # noqa: E731
    variant = zlib.crc32(nr.encode()) % 5
    felles = dict(fakturanr=nr, kundenr=None, belop=belop, inkasso=None)
    if variant == 0:
        return Fakturastatus(**felles, fakturadato=d(-30), forfallsdato=d(-16), utestaende=Decimal(0), kreditert=Decimal(0),
                             sist_betalt=d(-22), antall_purringer=0, siste_purring=None, status="Betalt")
    if variant == 1:
        return Fakturastatus(**felles, fakturadato=d(-3), forfallsdato=d(11), utestaende=belop, kreditert=Decimal(0),
                             sist_betalt=None, antall_purringer=0, siste_purring=None, status="Ubetalt")
    if variant == 2:
        return Fakturastatus(**felles, fakturadato=d(-40), forfallsdato=d(-26), utestaende=belop, kreditert=Decimal(0),
                             sist_betalt=None, antall_purringer=1, siste_purring=d(-12), status="Forfalt")
    if variant == 3:
        rest = (belop / 2).quantize(Decimal(1))
        return Fakturastatus(**felles, fakturadato=d(-5), forfallsdato=d(9), utestaende=rest, kreditert=Decimal(0),
                             sist_betalt=d(-1), antall_purringer=0, siste_purring=None, status="Delvis betalt")
    return Fakturastatus(**felles, fakturadato=d(-20), forfallsdato=d(-6), utestaende=Decimal(0), kreditert=belop,
                         sist_betalt=None, antall_purringer=0, siste_purring=None, status="Kreditert")


# ============================ kall mot Business NXT ============================

def _fakturarader(numre: list[str], *, bruk_buffer: bool) -> tuple[dict[str, list[dict]], datetime]:
    """Radene i customerTransaction per fakturanummer. I appen gjenbrukes et svar i BUFFER_SEK sekunder."""
    naa = time.time()
    if bruk_buffer:
        treff = [_cache.get(f"rad:{nr}") for nr in numre]
        if all(t and naa - t[0] < BUFFER_SEK for t in treff):
            return {nr: t[1] for nr, t in zip(numre, treff)}, datetime.fromtimestamp(min(t[0] for t in treff))
    data = _graphql(_FAKTURASTATUS, {"selskap": _selskap(), "fakturanr": numre})
    items = ((((data.get("useCompany") or {}).get("customerTransaction")) or {}).get("items")) or []
    rader = {nr: [] for nr in numre}
    for item in items:
        nr = str(item.get("invoiceNo") or "").strip()
        if nr in rader:
            rader[nr].append(item)
    if bruk_buffer:
        for nr in numre:
            _cache[f"rad:{nr}"] = (naa, rader[nr])
    return rader, datetime.fromtimestamp(naa)


def _bare_lesing(dokument: str) -> None:
    tekst = dokument.lstrip()
    if not tekst.startswith("query") or "mutation" in tekst.lower():
        raise BnxtFeil("Bare lesing er tillatt mot Business NXT.")


def _graphql(dokument: str, variabler: dict) -> dict:
    """POST mot GraphQL-API-et. Ett nytt forsoek ved utloept token (401) og ved for mange forespoersler (429 eller
    OUTSTANDING_LIMIT_EXCEEDED). Business NXT svarer HTTP 200 ogsaa ved feil, saa `errors` sjekkes alltid."""
    _bare_lesing(dokument)
    for forsok in range(2):
        siste = forsok == 1
        try:
            r = requests.post(config.BNXT_API, json={"query": dokument, "variables": variabler},
                              headers={"Authorization": f"Bearer {_token()}"}, timeout=TIDSAVBRUDD_SEK)
        except requests.RequestException as e:
            raise BnxtFeil(f"Fikk ikke kontakt med Business NXT ({type(e).__name__}).") from None
        if r.status_code == 401:
            _glem_token()                 # trukket tilbake eller utloept foer tiden: hent et nytt og proev én gang til
            if siste:
                raise BnxtFeil("Business NXT avviste tilgangen (HTTP 401). Sjekk tilgangen i «Connect Application Access».")
            continue
        if r.status_code == 429:
            if siste:
                raise BnxtFeil("Business NXT har for mange forespørsler akkurat nå (HTTP 429). Prøv igjen om litt.")
            time.sleep(PAUSE_SEK)
            continue
        if not r.ok:
            raise BnxtFeil(f"Business NXT svarte med en feil (HTTP {r.status_code}).")
        try:
            svar = r.json()
        except ValueError:
            raise BnxtFeil("Business NXT svarte med noe som ikke er JSON.") from None
        feil = svar.get("errors") or []
        if feil:
            koder = sorted({str((f.get("extensions") or {}).get("code") or "") for f in feil if isinstance(f, dict)} - {""})
            if "OUTSTANDING_LIMIT_EXCEEDED" in koder and not siste:
                time.sleep(PAUSE_SEK)
                continue
            detaljer = tuple(str(f.get("message") or "")[:300] for f in feil if isinstance(f, dict))
            raise BnxtFeil("Business NXT avviste oppslaget" + (f" ({', '.join(koder)})." if koder else "."), detaljer)
        return svar.get("data") or {}
    raise BnxtFeil("Business NXT svarte ikke som forventet.")   # naas ikke: siste forsoek returnerer eller kaster


def _token() -> str:
    if _cache.get("token") and _cache.get("utloper", 0) > time.time() + 60:
        return _cache["token"]
    return _nytt_token()


def _nytt_token() -> str:
    """Klient-legitimasjon mot Visma Connect med scopet for bare lesing. Klient-id og hemmelighet url-kodes og sendes i
    Basic Auth-hodet (RFC 6749, 2.3.1) - aldri i adressen eller i en logg."""
    if not (config.BNXT_CLIENT_ID and config.BNXT_CLIENT_SECRET):
        raise BnxtFeil("Business NXT er ikke satt opp (BNXT_CLIENT_ID / BNXT_CLIENT_SECRET mangler).")
    try:
        r = requests.post(config.BNXT_TOKEN_URL, data={"grant_type": "client_credentials", "scope": SCOPE},
                          auth=(quote(config.BNXT_CLIENT_ID, safe=""), quote(config.BNXT_CLIENT_SECRET, safe="")),
                          timeout=TIDSAVBRUDD_SEK)
    except requests.RequestException as e:
        raise BnxtFeil(f"Fikk ikke kontakt med Visma Connect ({type(e).__name__}).") from None
    if not r.ok:
        raise BnxtFeil(_tokenfeil(r))
    try:
        d = r.json()
        token, varighet = d["access_token"], int(d.get("expires_in") or 3600)
    except (ValueError, KeyError, TypeError):
        raise BnxtFeil("Visma Connect svarte uten et gyldig tilgangstoken.") from None
    _cache.update(token=token, utloper=time.time() + varighet, scope=str(d.get("scope") or ""))
    return token


def _glem_token() -> None:
    for n in ("token", "utloper", "scope"):
        _cache.pop(n, None)


_TOKENFEIL = {
    "invalid_client": "Visma Connect avviste klient-id eller hemmelighet (invalid_client). Sjekk BNXT_CLIENT_ID og "
                      "BNXT_CLIENT_SECRET.",
    "invalid_scope": "Visma Connect ga ikke tilgang med scopet for bare lesing (invalid_scope). Sjekk at integrasjonen i "
                     "Visma Developer Portal har «access-group-based-readonly».",
    "unauthorized_client": "Visma Connect tillater ikke klient-legitimasjon for denne appen (unauthorized_client).",
}


def _tokenfeil(r: requests.Response) -> str:
    """Bare OAuth-feilkoden (et fast ordforraad) og HTTP-statusen - aldri error_description eller svaret for oevrig."""
    try:
        kode = str(r.json().get("error") or "")
    except (ValueError, AttributeError):
        kode = ""
    trygg_kode = kode if kode.isidentifier() and len(kode) <= 40 else ""
    return _TOKENFEIL.get(kode, f"Visma Connect ga ikke tilgang (HTTP {r.status_code}{', ' + trygg_kode if trygg_kode else ''}).")
