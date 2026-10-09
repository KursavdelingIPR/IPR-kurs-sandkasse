"""Evaluering etter kurset (Camilla 05.10.2026). Ny utgave 09.10.2026 etter Forms-skjemaet deres: «Ikke i farger, men design. Også må
det være mulig å dele en lenke til evalueringen. Svarene må være anonyme, men det må være mulig å se svarene inne på kursene. Kan du
også lage en fin oversikt på svarene med ulike detaljer». Plan: OneDrive Plan_evaluering_2026-10-09.md.

* Spørsmålene settes opp per kurs (tabellen evaluering_sporsmal). Et kurs uten egne spørsmål bruker standardsettet: Camillas 12
  spørsmål, uten spørsmålet om EFT 2. år, som legges til på kursene der det passer (FERDIGE). Første endring lagrer standardsettet på
  kurset (sikre_sporsmal). Typer: skala 1–6, fritekst og valg (ett svar). Når noen har svart på et spørsmål, kan det ikke slettes eller
  bytte type, og svaralternativene kan ikke fjernes (ellers ville svarene blitt blandet) - teksten kan alltid rettes.
* Når (kurs.evaluering): NULL = dagen etter siste kursdag, 'samling' = dagen etter hver samling (Camilla: «på noen kurs skal det
  sendes etter hver samling»), 'av' = ikke. Morgenjobben (kjor) sender den personlige lenken til alle med plass på kurset eller samlingen,
  via Kjoring.send_en_gang (dedup per runde og mottaker). Bare runder med siste dag de siste DAGER_ETTER dagene.
* Lenkene: den personlige (signert, én gang per påmelding og runde) og en delt lenke per runde, som kan deles og vises som QR-kode. Den
  delte lenken kan ikke hindre dobbeltsvar (Camilla: «ja, det er ok»). «Ny lenke» gjør den gamle ugyldig (evaluering_oppsett.delt_versjon),
  og evalueringen kan stenges (evaluering_oppsett.stengt).
* Anonymt: evaluering_svar har kurs, samling, dato, kilde (epost/delt), tid brukt (sekunder) og svarene - aldri hvem eller klokkeslett.
  evaluering_besvart og evaluering_besvart_samling sier bare AT en påmelding har svart i runden (i samme transaksjon som svaret).
"""
import csv
import io
import json
import re
from datetime import date, timedelta

from . import db, lenker

DAGER_ETTER = 14                 # sendes bare for runder der siste dag var for høyst så mange dager siden
MAKS_TEKST = 2000
SKALA = (1, 2, 3, 4, 5, 6)
TYPER = {"skala": "Skala 1–6", "tekst": "Fritekst", "valg": "Valg (ett svar)"}
MODUS = {"slutt": "Dagen etter siste kursdag", "samling": "Dagen etter hver samling", "av": "Ikke send"}
MAKS_SPORSMAL = 40
MAKS_VALG = 12
_MAKS_SEKUNDER = 4 * 3600        # lenger enn dette er ikke «tid brukt på skjemaet» (siden sto åpen)

SKALA_HJELP = "1 er lavest, 6 er høyest"
SKALA_HJELP_BEST = "1 er lavest/dårligst, 6 er høyest/best"

# Standardsettet (Camilla 09.10.2026: «Ja, dette kan være standardspørsmål»): spørsmålene fra Forms-skjemaet deres. {del} er «samlingen»
# når evalueringen sendes etter hver samling, ellers «kurset». Nøkkelen er det som lagres i svarene.
STANDARD = (
    {"nokkel": "tilfreds", "type": "skala", "tekst": "Hvor tilfreds er du med {del} i sin helhet?", "hjelpetekst": SKALA_HJELP},
    {"nokkel": "formidling", "type": "skala", "tekst": "Hvordan vil du rangere kursholders formidlingsevne?",
     "hjelpetekst": SKALA_HJELP_BEST},
    {"nokkel": "formidling_utdyp", "type": "tekst", "tekst": "Utdyp gjerne besvarelsen fra spørsmål 2 under."},
    {"nokkel": "faglig", "type": "skala", "tekst": "Hvor fornøyd er du med det faglige innholdet på {del}?",
     "hjelpetekst": SKALA_HJELP_BEST},
    {"nokkel": "nyttig_info", "type": "tekst", "tekst": "Opplevde du noe av informasjonen formidlet på kurset som spesielt nyttig?"},
    {"nokkel": "unyttig", "type": "tekst", "tekst": "Opplevde du noe av informasjonen på kurset som unyttig?"},
    {"nokkel": "forslag", "type": "tekst", "tekst": "Har du forslag til hvordan vi kan gjøre kurset bedre?"},
    {"nokkel": "kursavdelingen", "type": "tekst", "tekst": "Har du en tilbakemelding til kursavdelingen?",
     "hjelpetekst": "Kursavdelingen er ansvarlig for selve organisering av kurs i regi av IPR/NIEFT, og utsendelse av informasjon og "
                    "kursmateriell i forkant/etterkant av kurs."},
    {"nokkel": "lokale", "type": "skala", "tekst": "Hvordan opplevde du lokalet og omgivelsene der kurset tok sted?",
     "tekst_digital": "Hvordan opplevde du den tekniske gjennomføringen (Zoom)?", "hjelpetekst": SKALA_HJELP},
    {"nokkel": "lokale_kommentar", "type": "tekst", "tekst": "Har du noen kommentarer til spørsmål 9?"},
    {"nokkel": "markedsforing", "type": "tekst",
     "tekst": "Har du lyst å gi oss en tilbakemelding som vi kan bruke anonymt i markedsføring?"},
)
# Ferdige spørsmål som kan legges til på kurs der de passer (Camilla: spørsmålet om EFT 2. år bare der det passer)
FERDIGE = {
    "eft2": {"type": "valg", "tekst": "Har du planer om å delta på EFT 2. år?", "valg": ["Ja", "Nei", "Har ikke bestemt meg enda"],
             "paakrevd": True, "hjelpetekst": "Informasjon om EFT 2. år finner du", "lenketekst": "her", "lenke": ""},
}


class Evalueringsfeil(ValueError):
    pass


# ============================ når ============================

def modus(kurs) -> str:
    verdi = kurs["evaluering"] if "evaluering" in kurs.keys() else None
    return verdi if verdi in ("samling", "av") else "slutt"


def er_paa(kurs) -> bool:
    return modus(kurs) != "av"


def sett_modus(con, kurs_id: int, ny: str, *, aktor: str) -> None:
    if ny not in MODUS:
        raise Evalueringsfeil("Velg når evalueringen skal sendes.")
    con.execute("UPDATE kurs SET evaluering=? WHERE id=?", (None if ny == "slutt" else ny, kurs_id))
    db.logg(con, "kurs_evaluering_endret", {"kurs_id": kurs_id, "modus": ny}, aktor=aktor)


def runder(con, kurs) -> list[dict]:
    """Evalueringens runder: hele kurset, eller én per samling (modus 'samling'). [{samling_id, navn, fra, til}] med datoer som date."""
    if modus(kurs) == "samling":
        samlinger = db.kursets_samlinger(con, kurs["id"])
        if samlinger:
            return [{"samling_id": s["id"], "navn": s["tittel"], "fra": _dato(s["fra"]), "til": _dato(s["til"])} for s in samlinger]
    dager = [_dato(d["dato"]) for d in db.kursdager(con, kurs["id"])]
    return [{"samling_id": None, "navn": "Hele kurset", "fra": min(dager) if dager else None, "til": max(dager) if dager else None}]


def _dato(verdi) -> date:
    return verdi if isinstance(verdi, date) else date.fromisoformat(str(verdi)[:10])


def nokkel(kurs_id: int, samling_id: int | None = None) -> str:
    """Utsendingsnøkkelen for runden (dedup per runde og mottaker)."""
    return f"evaluering:{kurs_id}" + (f":s{samling_id}" if samling_id else "")


def samling_fra_nokkel(tekst: str) -> int | None:
    treff = re.fullmatch(r"evaluering:\d+:s(\d+)", tekst or "")
    return int(treff[1]) if treff else None


# ============================ oppsettet (innledning, stengt, delt lenke) ============================

def standard_innledning(kurs) -> str:
    arrangor = "Terapiakademiet" if (kurs["merke"] if "merke" in kurs.keys() else None) == "terapiakademiet" \
        else "Institutt for Psykologisk Rådgivning"
    return (f"Du har deltatt på videreutdanning/kurs i regi av {arrangor}. Dine tilbakemeldinger om kurset er viktige for "
            "videreutviklingen av våre kurs. Ved å svare på evalueringen samtykker du til at vi lagrer svarene. Svarene er anonyme. "
            "Formålet med evalueringen er å forbedre våre tjenester. Det tar omtrent 5 minutter. Takk for at du tar deg tid til å svare!")


def oppsett(con, kurs) -> dict:
    r = con.execute("SELECT innledning, stengt, delt_versjon FROM evaluering_oppsett WHERE kurs_id=?", (kurs["id"],)).fetchone()
    return {"innledning": (r["innledning"] if r and r["innledning"] else standard_innledning(kurs)),
            "egen_innledning": bool(r and r["innledning"]), "stengt": bool(r and r["stengt"]),
            "delt_versjon": int(r["delt_versjon"]) if r else 0}


def _sikre_oppsett(con, kurs_id: int) -> None:
    con.execute("INSERT INTO evaluering_oppsett (kurs_id, stengt, delt_versjon) VALUES (?,0,0) ON CONFLICT DO NOTHING", (kurs_id,))


def lagre_innledning(con, kurs, tekst: str, *, aktor: str) -> None:
    tekst = (tekst or "").strip()
    if len(tekst) > MAKS_TEKST:
        raise Evalueringsfeil(f"Innledningen kan være høyst {MAKS_TEKST} tegn.")
    _sikre_oppsett(con, kurs["id"])
    lagret = None if not tekst or tekst == standard_innledning(kurs) else tekst
    con.execute("UPDATE evaluering_oppsett SET innledning=? WHERE kurs_id=?", (lagret, kurs["id"]))
    db.logg(con, "evaluering_innledning_endret", {"kurs_id": kurs["id"]}, aktor=aktor)


def sett_stengt(con, kurs_id: int, stengt: bool, *, aktor: str) -> None:
    _sikre_oppsett(con, kurs_id)
    con.execute("UPDATE evaluering_oppsett SET stengt=? WHERE kurs_id=?", (1 if stengt else 0, kurs_id))
    db.logg(con, "evaluering_stengt" if stengt else "evaluering_aapnet", {"kurs_id": kurs_id}, aktor=aktor)


def ny_delt_lenke(con, kurs_id: int, *, aktor: str) -> None:
    """Lager en ny delt lenke: den gamle (og QR-koden) slutter å virke."""
    _sikre_oppsett(con, kurs_id)
    con.execute("UPDATE evaluering_oppsett SET delt_versjon=delt_versjon+1 WHERE kurs_id=?", (kurs_id,))
    db.logg(con, "evaluering_ny_lenke", {"kurs_id": kurs_id}, aktor=aktor)


# ============================ spørsmålene ============================

def _del(kurs) -> str:
    return "samlingen" if modus(kurs) == "samling" else "kurset"


def _standard_rader(kurs) -> list[dict]:
    digital = (kurs["type"] if "type" in kurs.keys() else None) == "digital"
    ut = []
    for nr, s in enumerate(STANDARD, start=1):
        tekst = s.get("tekst_digital") if digital and s.get("tekst_digital") else s["tekst"]
        ut.append({"id": None, "nr": nr, "nokkel": s["nokkel"], "type": s["type"], "tekst": tekst.replace("{del}", _del(kurs)),
                   "hjelpetekst": s.get("hjelpetekst"), "lenketekst": None, "lenke": None, "valg": [], "paakrevd": False})
    return ut


def _rad(r) -> dict:
    return {"id": r["id"], "nr": r["nr"], "nokkel": r["nokkel"], "type": r["type"], "tekst": r["tekst"],
            "hjelpetekst": r["hjelpetekst"], "lenketekst": r["lenketekst"], "lenke": r["lenke"],
            "valg": json.loads(r["valg"]) if r["valg"] else [], "paakrevd": bool(r["paakrevd"])}


def har_egne_sporsmal(con, kurs_id: int) -> bool:
    return con.execute("SELECT 1 FROM evaluering_sporsmal WHERE kurs_id=? LIMIT 1", (kurs_id,)).fetchone() is not None


def sporsmal(con, kurs) -> list[dict]:
    """Spørsmålene i rekkefølge: kursets egne, ellers standardsettet."""
    rader = con.execute("SELECT * FROM evaluering_sporsmal WHERE kurs_id=? ORDER BY nr, id", (kurs["id"],)).fetchall()
    return [_rad(r) for r in rader] if rader else _standard_rader(kurs)


def sikre_sporsmal(con, kurs) -> None:
    """Lagrer standardsettet på kurset før den første endringen (så nøklene og rekkefølgen står fast)."""
    if har_egne_sporsmal(con, kurs["id"]):
        return
    for s in _standard_rader(kurs):
        _sett_inn(con, kurs["id"], s["nr"], s["nokkel"], s)


def _sett_inn(con, kurs_id: int, nr: int, nokkel_: str, s: dict) -> None:
    con.execute("""INSERT INTO evaluering_sporsmal (kurs_id, nr, nokkel, type, tekst, hjelpetekst, lenketekst, lenke, valg, paakrevd)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (kurs_id, nr, nokkel_, s["type"], s["tekst"], s.get("hjelpetekst") or None, s.get("lenketekst") or None,
                 s.get("lenke") or None, json.dumps(s.get("valg") or [], ensure_ascii=False) if s["type"] == "valg" else None,
                 1 if s.get("paakrevd") else 0))


def les_sporsmal(form) -> dict:
    """Et spørsmål fra skjemaet i fanen Evaluering, kontrollert. Evalueringsfeil med en forklaring ved feil."""
    type_ = (form.get("type") or "").strip()
    if type_ not in TYPER:
        raise Evalueringsfeil("Velg hva slags spørsmål det er.")
    tekst = (form.get("tekst") or "").strip()
    if not tekst:
        raise Evalueringsfeil("Skriv spørsmålet.")
    if len(tekst) > 300:
        raise Evalueringsfeil("Spørsmålet kan være høyst 300 tegn.")
    hjelp = (form.get("hjelpetekst") or "").strip()
    if len(hjelp) > 600:
        raise Evalueringsfeil("Hjelpeteksten kan være høyst 600 tegn.")
    lenketekst, lenke = (form.get("lenketekst") or "").strip(), (form.get("lenke") or "").strip()
    if lenke and not re.fullmatch(r"https?://[^\s<>\"']{3,500}", lenke):
        raise Evalueringsfeil("Lenken må være en hel adresse som begynner med https://.")
    if lenketekst and not lenke:
        raise Evalueringsfeil("Skriv adressen lenken skal gå til, eller fjern lenketeksten.")
    if len(lenketekst) > 80:
        raise Evalueringsfeil("Lenketeksten kan være høyst 80 tegn.")
    valg = []
    if type_ == "valg":
        for linje in (form.get("valg") or "").splitlines():
            linje = linje.strip()
            if linje and linje not in valg:
                valg.append(linje[:120])
        if not 2 <= len(valg) <= MAKS_VALG:
            raise Evalueringsfeil(f"Skriv mellom 2 og {MAKS_VALG} svaralternativer, ett på hver linje.")
    return {"type": type_, "tekst": tekst, "hjelpetekst": hjelp or None, "lenketekst": (lenketekst or "her") if lenke else None,
            "lenke": lenke or None, "valg": valg, "paakrevd": bool(form.get("paakrevd"))}


def _besvarte_nokler(con, kurs_id: int) -> set[str]:
    ut = set()
    for r in con.execute("SELECT svar FROM evaluering_svar WHERE kurs_id=?", (kurs_id,)):
        ut |= set(json.loads(r["svar"]))
    return ut


def _besvarte_valg(con, kurs_id: int, nokkel_: str) -> set[str]:
    return {json.loads(r["svar"]).get(nokkel_) for r in con.execute("SELECT svar FROM evaluering_svar WHERE kurs_id=?", (kurs_id,))} - {None}


def legg_til(con, kurs, data: dict, *, aktor: str) -> None:
    sikre_sporsmal(con, kurs)
    antall = con.execute("SELECT COUNT(*), COALESCE(MAX(nr),0) FROM evaluering_sporsmal WHERE kurs_id=?", (kurs["id"],)).fetchone()
    if antall[0] >= MAKS_SPORSMAL:
        raise Evalueringsfeil(f"Evalueringen kan ha høyst {MAKS_SPORSMAL} spørsmål.")
    brukte = {r["nokkel"] for r in con.execute("SELECT nokkel FROM evaluering_sporsmal WHERE kurs_id=?", (kurs["id"],))}
    brukte |= _besvarte_nokler(con, kurs["id"])
    n = 1
    while f"s{n}" in brukte:
        n += 1
    _sett_inn(con, kurs["id"], antall[1] + 1, f"s{n}", data)
    db.logg(con, "evaluering_sporsmal_lagt_til", {"kurs_id": kurs["id"]}, aktor=aktor)


def _hent(con, kurs, nokkel_: str):
    """Spørsmålet med nøkkelen (standardsettet lagres på kurset først, så det kan endres)."""
    sikre_sporsmal(con, kurs)
    r = con.execute("SELECT * FROM evaluering_sporsmal WHERE kurs_id=? AND nokkel=?", (kurs["id"], nokkel_)).fetchone()
    if not r:
        raise Evalueringsfeil("Spørsmålet finnes ikke (det kan være slettet av noen andre).")
    return r


def endre(con, kurs, nokkel_: str, data: dict, *, aktor: str) -> None:
    """Endrer et spørsmål. Har noen svart på det, kan det ikke bytte type, og svaralternativer som er brukt, kan ikke fjernes."""
    r = _hent(con, kurs, nokkel_)
    if r["nokkel"] in _besvarte_nokler(con, kurs["id"]):
        if data["type"] != r["type"]:
            raise Evalueringsfeil("Noen har svart på spørsmålet, så det kan ikke bytte type. Lag et nytt spørsmål i stedet.")
        if data["type"] == "valg":
            mangler = _besvarte_valg(con, kurs["id"], r["nokkel"]) - set(data["valg"])
            if mangler:
                raise Evalueringsfeil("Noen har valgt «" + "», «".join(sorted(mangler)) + "», så det svaralternativet kan ikke fjernes.")
    con.execute("""UPDATE evaluering_sporsmal SET type=?, tekst=?, hjelpetekst=?, lenketekst=?, lenke=?, valg=?, paakrevd=?
                   WHERE id=?""",
                (data["type"], data["tekst"], data["hjelpetekst"], data["lenketekst"], data["lenke"],
                 json.dumps(data["valg"], ensure_ascii=False) if data["type"] == "valg" else None, 1 if data["paakrevd"] else 0,
                 r["id"]))
    db.logg(con, "evaluering_sporsmal_endret", {"kurs_id": kurs["id"], "sporsmal": nokkel_}, aktor=aktor)


def slett(con, kurs, nokkel_: str, *, aktor: str) -> None:
    r = _hent(con, kurs, nokkel_)
    if r["nokkel"] in _besvarte_nokler(con, kurs["id"]):
        raise Evalueringsfeil("Noen har svart på spørsmålet, så det kan ikke slettes (da ville svarene forsvunnet fra oversikten).")
    con.execute("DELETE FROM evaluering_sporsmal WHERE id=?", (r["id"],))
    _nummerer(con, kurs["id"])
    db.logg(con, "evaluering_sporsmal_slettet", {"kurs_id": kurs["id"], "sporsmal": nokkel_}, aktor=aktor)


def flytt(con, kurs, nokkel_: str, retning: str, *, aktor: str) -> None:
    sporsmal_id = _hent(con, kurs, nokkel_)["id"]
    ider = [r["id"] for r in con.execute("SELECT id FROM evaluering_sporsmal WHERE kurs_id=? ORDER BY nr, id", (kurs["id"],))]
    i = ider.index(sporsmal_id)
    j = i - 1 if retning == "opp" else i + 1
    if 0 <= j < len(ider):
        ider[i], ider[j] = ider[j], ider[i]
        for nr, sid in enumerate(ider, start=1):
            con.execute("UPDATE evaluering_sporsmal SET nr=? WHERE id=?", (nr, sid))
        db.logg(con, "evaluering_sporsmal_flyttet", {"kurs_id": kurs["id"]}, aktor=aktor)


def _nummerer(con, kurs_id: int) -> None:
    for nr, r in enumerate(con.execute("SELECT id FROM evaluering_sporsmal WHERE kurs_id=? ORDER BY nr, id", (kurs_id,)).fetchall(), 1):
        con.execute("UPDATE evaluering_sporsmal SET nr=? WHERE id=?", (nr, r["id"]))


def tilbake_til_standard(con, kurs, *, aktor: str) -> None:
    """Fjerner kursets egne spørsmål (standardsettet gjelder igjen). Bare når ingen har svart."""
    if _besvarte_nokler(con, kurs["id"]):
        raise Evalueringsfeil("Noen har svart på evalueringen, så spørsmålene kan ikke tilbakestilles.")
    con.execute("DELETE FROM evaluering_sporsmal WHERE kurs_id=?", (kurs["id"],))
    db.logg(con, "evaluering_sporsmal_tilbakestilt", {"kurs_id": kurs["id"]}, aktor=aktor)


def kopier(con, fra_kurs_id: int, til_kurs_id: int) -> None:
    """Spørsmålene, innledningen og når evalueringen sendes, følger med når et kurs dupliseres (ikke svarene, stengt eller lenken)."""
    for r in con.execute("SELECT * FROM evaluering_sporsmal WHERE kurs_id=? ORDER BY nr, id", (fra_kurs_id,)).fetchall():
        s = _rad(r)
        _sett_inn(con, til_kurs_id, s["nr"], s["nokkel"], s)
    inn = con.execute("SELECT innledning FROM evaluering_oppsett WHERE kurs_id=?", (fra_kurs_id,)).fetchone()
    if inn and inn["innledning"]:
        _sikre_oppsett(con, til_kurs_id)
        con.execute("UPDATE evaluering_oppsett SET innledning=? WHERE kurs_id=?", (inn["innledning"], til_kurs_id))
    fra = con.execute("SELECT evaluering FROM kurs WHERE id=?", (fra_kurs_id,)).fetchone()
    con.execute("UPDATE kurs SET evaluering=? WHERE id=?", (fra["evaluering"] if fra else None, til_kurs_id))


# ============================ lenkene ============================

_TOKEN = re.compile(r"(0|[1-9][0-9]{0,11})(?:\.s([1-9][0-9]{0,11}))?\.([0-9a-f]{32})")
_DELT = re.compile(r"k([1-9][0-9]{0,11})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,11})\.([0-9a-f]{32})")


def token(paamelding_id: int, samling_id: int | None = None) -> str:
    if samling_id:
        return f"{int(paamelding_id)}.s{int(samling_id)}.{lenker.signatur('evaluering', int(paamelding_id), f's{int(samling_id)}')}"
    return f"{int(paamelding_id)}.{lenker.signatur('evaluering', int(paamelding_id))}"


def les_token(tekst) -> tuple[int, int | None] | None:
    """(påmelding, samling) lenken gjelder, eller None når lenken er feil eller forfalsket."""
    treff = _TOKEN.fullmatch(tekst or "") if isinstance(tekst, str) else None
    if not treff:
        return None
    pid, sid = int(treff[1]), int(treff[2]) if treff[2] else None
    deler = (pid, f"s{sid}") if sid else (pid,)
    return (pid, sid) if lenker.signatur_ok(treff[3], "evaluering", *deler) else None


def lenke(paamelding_id: int, samling_id: int | None = None) -> str:
    from . import config
    return f"{config.BASE_URL}/evaluering/{token(paamelding_id, samling_id)}"


def delt_token(kurs_id: int, versjon: int, samling_id: int | None = None) -> str:
    sid = int(samling_id or 0)
    return f"k{int(kurs_id)}.{int(versjon)}.{sid}.{lenker.signatur('evaluering-delt', int(kurs_id), int(versjon), sid)}"


def les_delt_token(tekst) -> tuple[int, int, int | None] | None:
    treff = _DELT.fullmatch(tekst or "") if isinstance(tekst, str) else None
    if not treff:
        return None
    kid, versjon, sid = int(treff[1]), int(treff[2]), int(treff[3])
    if not lenker.signatur_ok(treff[4], "evaluering-delt", kid, versjon, sid):
        return None
    return kid, versjon, sid or None


def delt_lenke(con, kurs, samling_id: int | None = None) -> str:
    from . import config
    return f"{config.BASE_URL}/evaluering/delt/{delt_token(kurs['id'], oppsett(con, kurs)['delt_versjon'], samling_id)}"


# ============================ svarene ============================

def tolk(form, sporsmal_: list[dict]) -> dict:
    """Skjemaet -> {nøkkel: svar}. Påkrevde spørsmål må besvares, og minst ett spørsmål må være besvart. Ukjente felt ignoreres."""
    svar = {}
    for nr, s in enumerate(sporsmal_, start=1):
        verdi = (form.get(s["nokkel"]) or "").strip()
        if not verdi:
            if s["paakrevd"]:
                raise Evalueringsfeil(f"Svar på spørsmål {nr}: {s['tekst']}")
            continue
        if s["type"] == "skala":
            if verdi not in {str(x) for x in SKALA}:
                raise Evalueringsfeil("Ugyldig svar.")
            svar[s["nokkel"]] = int(verdi)
        elif s["type"] == "valg":
            if verdi not in s["valg"]:
                raise Evalueringsfeil("Ugyldig svar.")
            svar[s["nokkel"]] = verdi
        else:
            svar[s["nokkel"]] = verdi[:MAKS_TEKST]
    if not svar:
        raise Evalueringsfeil("Svar på minst ett av spørsmålene.")
    return svar


def har_svart(con, paamelding_id: int, samling_id: int | None = None) -> bool:
    if samling_id:
        return con.execute("SELECT 1 FROM evaluering_besvart_samling WHERE paamelding_id=? AND samling_id=?",
                           (paamelding_id, samling_id)).fetchone() is not None
    return con.execute("SELECT 1 FROM evaluering_besvart WHERE paamelding_id=?", (paamelding_id,)).fetchone() is not None


def sekunder_brukt(startet, naa: float) -> int | None:
    """Tid brukt på skjemaet ut fra når siden ble vist (et tall serveren satte i skjemaet). None når det ikke gir mening."""
    try:
        sek = int(naa - float(startet))
    except (TypeError, ValueError):
        return None
    return sek if 0 < sek <= _MAKS_SEKUNDER else None


def lagre(con, kurs_id: int, paamelding_id: int | None, svar: dict, idag: date, *, samling_id: int | None = None,
          kilde: str = "epost", sekunder: int | None = None) -> bool:
    """Lagrer et anonymt svar. Fra en personlig lenke: False når påmeldingen alt har svart i runden (ingenting skrives). Fra den delte
    lenken (paamelding_id None): alltid lagret. Kalleren committer."""
    if paamelding_id is not None:
        try:
            if samling_id:
                con.execute("INSERT INTO evaluering_besvart_samling (paamelding_id, samling_id, kurs_id, besvart) VALUES (?,?,?,?)",
                            (paamelding_id, samling_id, kurs_id, idag.isoformat()))
            else:
                con.execute("INSERT INTO evaluering_besvart (paamelding_id, kurs_id, besvart) VALUES (?,?,?)",
                            (paamelding_id, kurs_id, idag.isoformat()))
        except db.IntegritetsFeil:
            return False
    con.execute("INSERT INTO evaluering_svar (kurs_id, besvart, svar, samling_id, kilde, sekunder) VALUES (?,?,?,?,?,?)",
                (kurs_id, idag.isoformat(), json.dumps(svar, ensure_ascii=False), samling_id, kilde, sekunder))
    return True


# ============================ oversikten ============================

ALLE = "alle"


def _svarrader(con, kurs_id: int, samling) -> list:
    if samling == ALLE:
        return con.execute("SELECT * FROM evaluering_svar WHERE kurs_id=?", (kurs_id,)).fetchall()
    if samling:
        return con.execute("SELECT * FROM evaluering_svar WHERE kurs_id=? AND samling_id=?", (kurs_id, samling)).fetchall()
    return con.execute("SELECT * FROM evaluering_svar WHERE kurs_id=? AND samling_id IS NULL", (kurs_id,)).fetchall()


def _invitert(con, kurs_id: int, samling) -> tuple[int, date | None]:
    """(antall som fikk lenken på e-post, datoen den første ble sendt) for runden (eller alle rundene)."""
    if samling == ALLE:
        vilkar, arg = "(nokkel=? OR nokkel LIKE ?)", (nokkel(kurs_id), nokkel(kurs_id) + ":s%")
    else:
        vilkar, arg = "nokkel=?", (nokkel(kurs_id, samling or None),)
    r = con.execute(f"SELECT COUNT(*), MIN(sendt_ts) FROM utsending_logg WHERE {vilkar} AND type='evaluering' AND status='sendt'",
                    arg).fetchone()
    forste = _dato(r[1]) if r[1] else None
    return int(r[0]), forste


def resultater(con, kurs, samling=None, idag: date | None = None) -> dict:
    """Oversikten for runden `samling` (None = hele kurset, en samling-id, eller ALLE): antall svar (via e-post og delt lenke),
    svarprosent, gjennomsnittlig tid, hvor lenge evalueringen har vært åpen, og per spørsmål: gjennomsnitt og fordeling (skala), antall
    og prosent (valg) og alle tekstene (fritekst, sortert alfabetisk så rekkefølgen ikke avslører hvem som svarte når)."""
    idag = idag or date.today()
    rader = _svarrader(con, kurs["id"], samling)
    svar = [json.loads(r["svar"]) for r in rader]
    via_delt = sum(1 for r in rader if r["kilde"] == "delt")
    via_epost = len(rader) - via_delt
    invitert, forste_sendt = _invitert(con, kurs["id"], samling)
    tider = [r["sekunder"] for r in rader if r["sekunder"]]
    datoer = sorted(_dato(r["besvart"]) for r in rader)
    kandidater = [d for d in (forste_sendt, datoer[0] if datoer else None) if d]
    start = min(kandidater) if kandidater else None
    ut = []
    for nr, s in enumerate(sporsmal(con, kurs), start=1):
        verdier = [x[s["nokkel"]] for x in svar if s["nokkel"] in x]
        base = {**s, "nr": nr, "antall": len(verdier)}
        if s["type"] == "skala":
            fordeling = {n: sum(1 for v in verdier if v == n) for n in SKALA}
            ut.append({**base, "snitt": round(sum(verdier) / len(verdier), 2) if verdier else None, "fordeling": fordeling,
                       "maks": max(fordeling.values()) if verdier else 0})
        elif s["type"] == "valg":
            alternativer = list(s["valg"]) + sorted({v for v in verdier if v not in s["valg"]})
            telling = [{"valg": v, "antall": verdier.count(v), "prosent": round(100 * verdier.count(v) / len(verdier)) if verdier else 0}
                       for v in alternativer]
            ut.append({**base, "telling": telling, "maks": max((t["antall"] for t in telling), default=0)})
        else:
            ut.append({**base, "tekster": sorted((str(v) for v in verdier), key=str.casefold)})
    return {"antall_svar": len(rader), "via_epost": via_epost, "via_delt": via_delt, "invitert": invitert,
            "svarprosent": round(100 * via_epost / invitert) if invitert else None,
            "snitt_sekunder": round(sum(tider) / len(tider)) if tider else None,
            "forste": datoer[0] if datoer else None, "siste": datoer[-1] if datoer else None,
            "apen_siden": start, "apen_dager": (idag - start).days + 1 if start else None, "sporsmal": ut}


def som_csv(con, kurs, samling=ALLE) -> str:
    """Svarene som CSV (Excel): én rad per svar, én kolonne per spørsmål. Ingen dato, rekkefølgen er ikke tidsbestemt."""
    s_liste = sporsmal(con, kurs)
    rader = sorted((json.loads(r["svar"]) for r in _svarrader(con, kurs["id"], samling)),
                   key=lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True))
    ut = io.StringIO()
    skriver = csv.writer(ut, delimiter=";")
    skriver.writerow([f"{nr}. {s['tekst']}" for nr, s in enumerate(s_liste, start=1)])
    for x in rader:
        skriver.writerow([x.get(s["nokkel"], "") for s in s_liste])
    return "﻿" + ut.getvalue()


def tid_tekst(sekunder) -> str:
    if not sekunder:
        return "–"
    return f"{sekunder // 60:02d}:{sekunder % 60:02d}"


# ============================ utsending (morgenjobben) ============================

def kjor(k) -> None:
    """Sender evalueringen dagen etter siste dag i runden (høyst DAGER_ETTER dager etter): for hele kurset til alle med plass, eller
    (modus 'samling') for hver samling til alle med plass som er på samlingen."""
    idag = k.idag
    for kurs in k.con.execute("SELECT * FROM kurs WHERE status NOT IN ('avlyst', 'utkast')").fetchall():
        if not er_paa(kurs):
            continue
        mottakere = None
        for runde in runder(k.con, kurs):
            if runde["til"] is None or not (runde["til"] < idag <= runde["til"] + timedelta(days=DAGER_ETTER)):
                continue
            if mottakere is None:
                mottakere = k.con.execute(
                    """SELECT p.id, p.kurs_id, p.status, p.ekstradeltaker_ts, d.navn, d.fornavn, d.epost FROM paamelding p
                       JOIN deltaker d ON d.id=p.deltaker_id WHERE p.kurs_id=? AND p.status='bekreftet'""", (kurs["id"],)).fetchall()
            for m in mottakere:
                if k.sending_stanset:
                    return
                if runde["samling_id"] and not _er_paa_samling(k.con, m, runde["samling_id"]):
                    continue
                k.send_en_gang(nokkel(kurs["id"], runde["samling_id"]), m["epost"], "evaluering", "evaluering", paamelding_id=m["id"],
                               kurs_id=kurs["id"], d=m, kurs=kurs, evaluering_url=lenke(m["id"], runde["samling_id"]))


def _er_paa_samling(con, p, samling_id: int) -> bool:
    """Er påmeldingen på samlingen? Alle med plass er det, unntatt en ekstradeltaker som bare er satt opp på andre samlinger."""
    return any(d["samling_id"] == samling_id for d in db.paameldingens_kursdager(con, p))
