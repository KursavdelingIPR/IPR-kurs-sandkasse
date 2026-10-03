"""Planlagte kurs: kursene fra kursplanleggingen (fanen «Kommende kurs» i Kurskalender-Excelen) som vises i Kalender og
Årsplan, men som ikke er opprettet i systemet ennå (bestillingen 02.10.2026: «IKKE legg de inn som kurs i oversikten
foreløpig»).

  * Et planlagt kurs har én eller flere samlinger (datoperioder), gjerne med veiledningsdager. Samlingens nummer («2. samling»)
    lagres, så et kurs som er lagt inn fra 2. samling (1. samling var før) får riktig nummer.
  * Fargen velges én gang, ut fra datoene (kursfarger.velg), og lagres: alle samlingene i kurset har samme farge overalt, og
    kurs som går nær hverandre i tid får ulike farger. Den kan byttes for hånd.
  * Ingen påmeldingsside, ingen deltakere, ingen e-post og ingen fakturering, og de står ikke i kurslisten på Oversikten.
    Ingen deltakerdata.
  * «Annen arrangør» (ekstern, f.eks. IPRO) og avlyste kurs vises grå. Avlyste kurs vises ikke i Årsplan (som avlyste kurs).
  * «Må sjekkes» er en merknad om noe som må kontrolleres (f.eks. en usikker dato fra Excel). Den tømmes når det er sjekket.
  * Hendelsesloggen får aldri fritekst - bare id, antall og datoer.
"""
import re
from datetime import date, timedelta

from . import db, kursfarger

STATUSER = {"planlagt": "Planlagt", "avlyst": "Avlyst"}
MAKS = {"prosjektnr": 20, "navn": 120, "arrangor": 20, "notat": 2000, "sjekk": 300, "samlingsnavn": 60, "tema": 200,
        "sted": 60, "lokale": 60, "kursholdere": 300, "veiledere": 300}
MAKS_SAMLING_DAGER = 31             # en samling kan vare høyst en måned
MAKS_SAMLINGER = 20
FORSTE_AAR, SISTE_AAR = 2000, 2100
FARGEVINDU_DAGER = 180              # kurs lenger unna enn dette påvirker ikke fargevalget (nærheten er da nesten 0)
ONLINE = ("online", "zoom", "digital", "teams", "webinar")
BYER = ("oslo", "bergen", "trondheim", "stavanger", "tromsø", "kristiansand", "drammen", "bodø", "ålesund", "haugesund")
LOKALER_I_BY = {"paleet": "oslo", "n58": "bergen", "n.58": "bergen", "chr. mich": "bergen", "chr.mich": "bergen",
                "christian michelsens": "bergen"}       # lokaler uten by i teksten (kursplanen skriver ofte bare lokalet)

_KONTROLLTEGN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ISO_DATO = re.compile(r"\d{4}-\d{2}-\d{2}")
_NORSK_DATO = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
_KURSFELT = ("prosjektnr", "navn", "arrangor", "ekstern", "status", "farge", "antall_samlinger", "notat", "sjekk")
_SAMLINGSFELT = ("nr", "navn", "tema", "fra_dato", "til_dato", "veiledningsdager", "sted", "lokale", "kursholdere",
                 "veiledere", "notat", "sjekk")


class PlanFeil(ValueError):
    """Ugyldig input. Meldingen er trygg å vise til admin."""


# ============================ validering ============================

def _tekst(verdi, navn: str, maks: int, *, paakrevd: bool = False, flerlinjet: bool = False) -> str | None:
    tekst = str(verdi or "").strip()
    if not tekst:
        if paakrevd:
            raise PlanFeil(f"{navn} må fylles ut.")
        return None
    if len(tekst) > maks:
        raise PlanFeil(f"{navn} kan være høyst {maks} tegn.")
    if _KONTROLLTEGN.search(tekst) or (not flerlinjet and ("\n" in tekst or "\r" in tekst)):
        raise PlanFeil(f"{navn} inneholder ugyldige tegn.")
    return tekst


def _dato(verdi, navn: str) -> date:
    tekst = str(verdi or "").strip()
    m = _NORSK_DATO.fullmatch(tekst)
    if m:
        tekst = f"{m[3]}-{int(m[2]):02}-{int(m[1]):02}"
    if not _ISO_DATO.fullmatch(tekst):
        raise PlanFeil(f"{navn} må være en dato (ÅÅÅÅ-MM-DD eller DD.MM.ÅÅÅÅ).")
    try:
        d = date.fromisoformat(tekst)
    except ValueError:
        raise PlanFeil(f"{navn} er ikke en gyldig dato.") from None
    if not FORSTE_AAR <= d.year <= SISTE_AAR:
        raise PlanFeil(f"{navn} må være mellom år {FORSTE_AAR} og {SISTE_AAR}.")
    return d


def _heltall(verdi, navn: str, minst: int, hoyst: int) -> int | None:
    tekst = str(verdi if verdi is not None else "").strip()
    if not tekst:
        return None
    if not tekst.isascii() or not tekst.isdigit() or not minst <= int(tekst) <= hoyst:
        raise PlanFeil(f"{navn} må være et tall fra {minst} til {hoyst}.")
    return int(tekst)


def _avkrysset(verdi) -> int:
    return 1 if str(verdi or "").strip().lower() in ("1", "on", "ja", "true") else 0


def _veiledningsdager(verdi, fra: date, til: date) -> str | None:
    """«18.01.2027, 2027-01-19» -> «2027-01-18,2027-01-19» (sortert, uten dubletter). Dagene må ligge i samlingen."""
    deler = [d for d in re.split(r"[,;\s]+", str(verdi or "").strip()) if d]
    dager = sorted({_dato(d, "Veiledningsdag") for d in deler})
    utenfor = [d for d in dager if not fra <= d <= til]
    if utenfor:
        raise PlanFeil(f"Veiledningsdagen {utenfor[0].strftime('%d.%m.%Y')} ligger utenfor samlingen.")
    return ",".join(d.isoformat() for d in dager) or None


def valider_kurs(skjema) -> dict:
    """Leser og validerer kursets felt fra skjemaet. Farge None = velges automatisk ut fra datoene. PlanFeil ved feil."""
    status = (skjema.get("status") or "planlagt").strip()
    if status not in STATUSER:
        raise PlanFeil("Ukjent status.")
    farge = str(skjema.get("farge") or "").strip()
    if farge in ("", "auto"):
        farge_nr = None
    elif farge.isascii() and farge.isdigit() and int(farge) < kursfarger.ANTALL:
        farge_nr = int(farge)
    else:
        raise PlanFeil("Ugyldig farge.")
    return {
        "prosjektnr": _tekst(skjema.get("prosjektnr"), "Prosjektnummer", MAKS["prosjektnr"]),
        "navn": _tekst(skjema.get("navn"), "Navn", MAKS["navn"], paakrevd=True),
        "arrangor": _tekst(skjema.get("arrangor"), "Arrangør", MAKS["arrangor"]) or "IPR",
        "ekstern": _avkrysset(skjema.get("ekstern")),
        "status": status,
        "farge": farge_nr,
        "antall_samlinger": _heltall(skjema.get("antall_samlinger"), "Antall samlinger", 1, MAKS_SAMLINGER),
        "notat": _tekst(skjema.get("notat"), "Notat", MAKS["notat"], flerlinjet=True),
        "sjekk": _tekst(skjema.get("sjekk"), "Må sjekkes", MAKS["sjekk"], flerlinjet=True),
    }


def valider_samling(skjema, prefiks: str = "") -> dict:
    """Leser og validerer én samling fra skjemaet (feltene kan ha et prefiks, f.eks. «s-» i skjemaet for nytt kurs)."""
    def felt(navn):
        return skjema.get(prefiks + navn)
    fra = _dato(felt("fra_dato"), "Fra-dato")
    til = _dato(felt("til_dato"), "Til-dato") if str(felt("til_dato") or "").strip() else fra
    if til < fra:
        raise PlanFeil("Til-dato kan ikke være før fra-dato.")
    if (til - fra).days >= MAKS_SAMLING_DAGER:
        raise PlanFeil(f"En samling kan vare høyst {MAKS_SAMLING_DAGER} dager.")
    return {
        "nr": _heltall(felt("nr"), "Samlingens nummer", 1, MAKS_SAMLINGER),
        "navn": _tekst(felt("navn"), "Navn på samlingen", MAKS["samlingsnavn"]),
        "tema": _tekst(felt("tema"), "Tema", MAKS["tema"]),
        "fra_dato": fra.isoformat(), "til_dato": til.isoformat(),
        "veiledningsdager": _veiledningsdager(felt("veiledningsdager"), fra, til),
        "sted": _tekst(felt("sted"), "Sted", MAKS["sted"]),
        "lokale": _tekst(felt("lokale"), "Lokale", MAKS["lokale"]),
        "kursholdere": _tekst(felt("kursholdere"), "Kursholdere", MAKS["kursholdere"]),
        "veiledere": _tekst(felt("veiledere"), "Veiledere", MAKS["veiledere"]),
        "notat": _tekst(felt("notat"), "Notat", MAKS["notat"], flerlinjet=True),
        "sjekk": _tekst(felt("sjekk"), "Må sjekkes", MAKS["sjekk"], flerlinjet=True),
    }


# ============================ farge ============================

def _perioder(rader) -> list[tuple[date, date]]:
    return [(date.fromisoformat(r["fra_dato"]), date.fromisoformat(r["til_dato"])) for r in rader]


def _lop(datoer: list[date]) -> list[tuple[date, date]]:
    """Sammenhengende datoer som (fra, til)."""
    ut: list[list[date]] = []
    for d in sorted(set(datoer)):
        if ut and ut[-1][1] + timedelta(days=1) == d:
            ut[-1][1] = d
        else:
            ut.append([d, d])
    return [(a, b) for a, b in ut]


def velg_farge(con, perioder: list[tuple[date, date]], *, unntatt: int | None = None) -> int:
    """Fargen et planlagt kurs med disse samlingene skal ha (kursfarger.velg): den som er lengst unna i tid hos kursene som
    allerede har den. Teller med de andre planlagte kursene (lagret farge) og kursene i systemet (fargen følger
    kursserien). Avlyste kurs og kurs med annen arrangør er grå og teller ikke."""
    fra = min(p[0] for p in perioder) - timedelta(days=FARGEVINDU_DAGER)
    til = max(p[1] for p in perioder) + timedelta(days=FARGEVINDU_DAGER)
    andre: dict[tuple, list] = {}
    for r in con.execute(
            """SELECT pk.id, pk.farge, ps.fra_dato, ps.til_dato FROM planlagt_samling ps
               JOIN planlagt_kurs pk ON pk.id=ps.planlagt_kurs_id
               WHERE pk.status='planlagt' AND pk.ekstern=0 AND pk.kurs_id IS NULL AND ps.til_dato >= ? AND ps.fra_dato <= ?
               ORDER BY pk.id, ps.fra_dato""", (fra.isoformat(), til.isoformat())):
        if r["id"] != unntatt:
            andre.setdefault(("plan", r["id"]), [r["farge"], []])[1].append(
                (date.fromisoformat(r["fra_dato"]), date.fromisoformat(r["til_dato"])))
    kursdager: dict[int, list] = {}
    for r in con.execute(
            """SELECT k.id, k.navn, k.spesialistlop, kd.dato FROM kursdag kd JOIN kurs k ON k.id=kd.kurs_id
               WHERE k.status != 'avlyst' AND kd.dato BETWEEN ? AND ? ORDER BY k.id, kd.dato""",
            (fra.isoformat(), til.isoformat())):
        kursdager.setdefault(r["id"], [kursfarger.farge(r["navn"], r["spesialistlop"]), []])[1].append(
            date.fromisoformat(r["dato"]))
    for kid, (nr, datoer) in kursdager.items():
        andre[("kurs", kid)] = [nr, _lop(datoer)]
    return kursfarger.velg(perioder, [(nr, p) for nr, p in andre.values()])


# ============================ lagring ============================

def _sett_inn_samling(con, plan_id: int, s: dict) -> int:
    return db.sett_inn(
        con, f"INSERT INTO planlagt_samling (planlagt_kurs_id, {', '.join(_SAMLINGSFELT)}) "
             f"VALUES (?, {', '.join('?' * len(_SAMLINGSFELT))})",
        (plan_id, *(s.get(f) for f in _SAMLINGSFELT)))


def opprett(con, kurs: dict, samlinger: list[dict], aktor: str, *, kilde: str | None = None) -> int:
    """Lagrer et planlagt kurs med samlingene. Uten farge velges den ut fra datoene (velg_farge)."""
    if not samlinger:
        raise PlanFeil("Et planlagt kurs må ha minst én samling.")
    if len(samlinger) > MAKS_SAMLINGER:
        raise PlanFeil(f"Et planlagt kurs kan ha høyst {MAKS_SAMLINGER} samlinger.")
    farge = kurs.get("farge")
    if farge is None:
        farge = velg_farge(con, _perioder(samlinger))
    verdier = {"arrangor": "IPR", "ekstern": 0, "status": "planlagt",
               **{f: v for f, v in kurs.items() if v is not None}, "farge": farge}
    plan_id = db.sett_inn(
        con, f"INSERT INTO planlagt_kurs ({', '.join(_KURSFELT)}, kilde, opprettet_av) "
             f"VALUES ({', '.join('?' * len(_KURSFELT))}, ?, ?)",
        (*(verdier.get(f) for f in _KURSFELT), kilde, aktor))
    for s in samlinger:
        _sett_inn_samling(con, plan_id, s)
    db.logg(con, "planlagt_kurs_opprettet", {"id": plan_id, "samlinger": len(samlinger),
                                             "fra_dato": min(s["fra_dato"] for s in samlinger),
                                             "til_dato": max(s["til_dato"] for s in samlinger)}, aktor=aktor)
    return plan_id


def oppdater_kurs(con, plan_id: int, v: dict, aktor: str) -> bool:
    """Lagrer kursets felt. Farge None = velg på nytt ut fra datoene (de andre kursene beholder sine)."""
    if not con.execute("SELECT 1 FROM planlagt_kurs WHERE id=?", (plan_id,)).fetchone():
        return False
    farge = v.get("farge")
    if farge is None:
        rader = con.execute("SELECT fra_dato, til_dato FROM planlagt_samling WHERE planlagt_kurs_id=?", (plan_id,)).fetchall()
        farge = velg_farge(con, _perioder(rader), unntatt=plan_id) if rader else 0
    verdier = {**v, "farge": farge}
    con.execute(f"UPDATE planlagt_kurs SET {', '.join(f + '=?' for f in _KURSFELT)} WHERE id=?",
                (*(verdier.get(f) for f in _KURSFELT), plan_id))
    db.logg(con, "planlagt_kurs_endret", {"id": plan_id}, aktor=aktor)
    return True


def slett(con, plan_id: int, aktor: str) -> dict | None:
    """Sletter kurset med samlingene og returnerer raden (None hvis det ikke finnes)."""
    rad = con.execute("SELECT * FROM planlagt_kurs WHERE id=?", (plan_id,)).fetchone()
    if not rad:
        return None
    con.execute("DELETE FROM planlagt_samling WHERE planlagt_kurs_id=?", (plan_id,))
    con.execute("DELETE FROM planlagt_kurs WHERE id=?", (plan_id,))
    db.logg(con, "planlagt_kurs_slettet", {"id": plan_id}, aktor=aktor)
    return dict(rad)


def _samling_i_kurset(con, plan_id: int, samling_id: int):
    return con.execute("SELECT * FROM planlagt_samling WHERE id=? AND planlagt_kurs_id=?", (samling_id, plan_id)).fetchone()


def legg_til_samling(con, plan_id: int, s: dict, aktor: str) -> int | None:
    """Ny samling i kurset (None hvis kurset ikke finnes). Kursets farge er uendret."""
    if not con.execute("SELECT 1 FROM planlagt_kurs WHERE id=?", (plan_id,)).fetchone():
        return None
    if con.execute("SELECT COUNT(*) FROM planlagt_samling WHERE planlagt_kurs_id=?", (plan_id,)).fetchone()[0] >= MAKS_SAMLINGER:
        raise PlanFeil(f"Et planlagt kurs kan ha høyst {MAKS_SAMLINGER} samlinger.")
    sid = _sett_inn_samling(con, plan_id, s)
    db.logg(con, "planlagt_samling_lagt_til", {"id": sid, "planlagt_kurs_id": plan_id, "fra_dato": s["fra_dato"],
                                               "til_dato": s["til_dato"]}, aktor=aktor)
    return sid


def oppdater_samling(con, plan_id: int, samling_id: int, s: dict, aktor: str) -> bool:
    if not _samling_i_kurset(con, plan_id, samling_id):
        return False
    con.execute(f"UPDATE planlagt_samling SET {', '.join(f + '=?' for f in _SAMLINGSFELT)} WHERE id=?",
                (*(s.get(f) for f in _SAMLINGSFELT), samling_id))
    db.logg(con, "planlagt_samling_endret", {"id": samling_id, "planlagt_kurs_id": plan_id, "fra_dato": s["fra_dato"],
                                             "til_dato": s["til_dato"]}, aktor=aktor)
    return True


def slett_samling(con, plan_id: int, samling_id: int, aktor: str) -> dict | None:
    """Sletter én samling. Den siste samlingen kan ikke slettes (slett heller kurset)."""
    rad = _samling_i_kurset(con, plan_id, samling_id)
    if not rad:
        return None
    if con.execute("SELECT COUNT(*) FROM planlagt_samling WHERE planlagt_kurs_id=?", (plan_id,)).fetchone()[0] <= 1:
        raise PlanFeil("Kurset må ha minst én samling. Slett heller hele det planlagte kurset.")
    con.execute("DELETE FROM planlagt_samling WHERE id=?", (samling_id,))
    db.logg(con, "planlagt_samling_slettet", {"id": samling_id, "planlagt_kurs_id": plan_id,
                                              "fra_dato": rad["fra_dato"]}, aktor=aktor)
    return dict(rad)


# ============================ lesing ============================

def visningsnavn(prosjektnr: str | None, navn: str, byer=()) -> str:
    """«673 EFST 1-årig Oslo» - prosjektnummeret foran navnet, som i kursplanen, og byen bak (Camilla 02.10: «Gjerne byen
    i navnet»). Går kurset i flere byer: «Bergen/Oslo». En by som alt står i navnet, gjentas ikke."""
    tekst = f"{prosjektnr} {navn}" if prosjektnr else navn
    nye = [b for b in dict.fromkeys(byer) if b and b.casefold() not in navn.casefold()]
    return f"{tekst} {'/'.join(nye)}" if nye else tekst


def er_online(*tekster) -> bool:
    return any(o in (t or "").casefold() for t in tekster for o in ONLINE)


def by_i(*tekster) -> str | None:
    """Byen i sted/lokale med små bokstaver («IPR, Bergen» -> «bergen», «Paleet» -> «oslo»), ellers None."""
    t = " ".join(x for x in tekster if x).casefold()
    for by in BYER:
        if re.search(rf"\b{by}\b", t):
            return by
    return next((by for lokale, by in LOKALER_I_BY.items() if lokale in t), None)


def samlingens_by(sted: str | None, lokale: str | None) -> str | None:
    """Byen en fysisk samling går i, med stor forbokstav («Oslo»). Online: None (lokalet kan være der det sendes fra)."""
    if er_online(sted):
        return None
    by = by_i(sted, lokale)
    return by.capitalize() if by else None


def kolliderer(a_ekstern: bool, a_fysisk: bool, a_by: str | None, b_ekstern: bool, b_fysisk: bool, b_by: str | None, *,
               a_online: bool | None = None, b_online: bool | None = None) -> bool:
    """Er to kurs samme dag en dobbeltbooking? Egne kurs (Camilla 03.10): to fysiske kurs bare i samme by («det trenger heller
    ikke være noe varsel om det er ett kurs i bergen og ett i oslo»; ukjent by: si fra), to kurs med nettdel (online eller
    hybrid) alltid, og et fysisk kurs og et online-/hybridkurs aldri («Det trenger ikke stå varsel hvis fysiske kurs og online
    kurs går samtidig»; hybrid regnes som online her). Uten a_online/b_online er et kurs som ikke er fysisk, online.
    Et kurs med annen arrangør (f.eks. IPRO) bare når både de og vi har FYSISK kurs på samme sted, fordi vi da deler lokaler
    (Camilla 02.10: «Kun hvis både vi og de har kurs fysisk i Oslo»). Online-kurs kolliderer aldri med annen arrangør, og to
    kurs med annen arrangør er ikke vår sak."""
    if not a_ekstern and not b_ekstern:
        a_nett = not a_fysisk if a_online is None else a_online
        b_nett = not b_fysisk if b_online is None else b_online
        if a_nett or b_nett:
            return a_nett and b_nett
        return a_by is None or b_by is None or a_by == b_by
    if a_ekstern and b_ekstern:
        return False
    return a_fysisk and b_fysisk and a_by is not None and a_by == b_by


def sted_tekst(sted: str | None, lokale: str | None) -> str:
    """«Oslo · Paleet», «Online», «Bergen» - by og lokale i én tekst."""
    deler = [" ".join(t.split()) for t in (sted, lokale) if t and t.strip()]
    if len(deler) == 2 and deler[0].casefold() == deler[1].casefold():
        deler = deler[:1]
    return " · ".join(deler)


def _dager(tekst: str | None) -> tuple[date, ...]:
    return tuple(date.fromisoformat(d) for d in (tekst or "").split(",") if d)


def _nummerer(samlinger: list[dict], antall_samlinger: int | None) -> int:
    """Gir hver samling (sortert på dato) nummer («nr_vist») og returnerer antallet samlinger i kurset: det lagrede
    antallet, ellers det største av antallet som er lagt inn og det høyeste nummeret. Uten lagret nummer brukes plassen."""
    hoyeste = max((s["nr"] or 0 for s in samlinger), default=0)
    antall = antall_samlinger or max(len(samlinger), hoyeste)
    for plass, s in enumerate(samlinger, start=1):
        s["nr_vist"] = s["nr"] or (plass if antall > 1 else None)
    return antall


def _kursrad(r) -> dict:
    return {"id": r["id"], "prosjektnr": r["prosjektnr"], "navn": r["navn"], "arrangor": r["arrangor"],
            "ekstern": bool(r["ekstern"]), "status": r["status"], "farge": r["farge"],
            "antall_samlinger": r["antall_samlinger"], "notat": r["notat"], "sjekk": r["sjekk"], "kilde": r["kilde"],
            "visningsnavn": visningsnavn(r["prosjektnr"], r["navn"]), "kurs_id": r["kurs_id"]}


def _samlingsrad(r) -> dict:
    dager = _dager(r["veiledningsdager"])
    return {"id": r["id"], "nr": r["nr"], "navn": r["navn"], "tema": r["tema"],
            "fra": date.fromisoformat(r["fra_dato"]), "til": date.fromisoformat(r["til_dato"]),
            "fra_dato": r["fra_dato"], "til_dato": r["til_dato"], "veiledningsdager": dager,
            "veiledningsdager_visning": ", ".join(d.strftime("%d.%m.%Y") for d in dager),
            "sted": r["sted"], "lokale": r["lokale"], "sted_tekst": sted_tekst(r["sted"], r["lokale"]),
            "kursholdere": r["kursholdere"], "veiledere": r["veiledere"], "notat": r["notat"], "sjekk": r["sjekk"],
            "kurs_samling_id": r["kurs_samling_id"]}


def roller_per_samling(con, samling_ider) -> dict[int, list]:
    """{samling_id: [(dag, navn, rolle)]} fra kursholderoversikten (tabellene planlagt_rolle og kursholder, kurs/kursholdere.py),
    uten «-», i kursholdernes rekkefølge. dag = '' er samlingen, ellers en veiledningsdag."""
    ider = list(dict.fromkeys(samling_ider))
    if not ider:
        return {}
    ut: dict[int, list] = {}
    for r in con.execute(f"""SELECT r.samling_id, r.dag, r.rolle, k.navn FROM planlagt_rolle r
                             JOIN kursholder k ON k.id=r.kursholder_id
                             WHERE r.samling_id IN ({','.join('?' * len(ider))}) AND r.rolle != '-'
                             ORDER BY k.rekkefolge, k.navn, r.dag""", tuple(ider)):
        ut.setdefault(r["samling_id"], []).append((r["dag"], r["navn"], r["rolle"]))
    return ut


def rolletekst(roller: list, *, veiledere: bool, fritekst: str | None) -> str | None:
    """«Anne Hilde (T1), Vanja (T2)» for samlingen (veiledere=False) eller veiledningsdagene (True, hver person én gang),
    med fritekstfeltet etter (andre enn kursholderne i listen, for eksempel gjester)."""
    deler = list(dict.fromkeys(f"{navn} ({rolle})" for dag, navn, rolle in roller if bool(dag) == veiledere))
    if fritekst:
        deler.append(fritekst)
    return ", ".join(deler) or None


def samlingsnavn(s: dict) -> str:
    """«2. samling», «Veiledning (2. samling)» eller «Veiledning» - tomt for et kurs med én dato/samling uten navn."""
    nr = f"{s['nr_vist']}. samling" if s.get("nr_vist") else ""
    if s.get("navn"):
        return f"{s['navn']} ({nr})" if nr else s["navn"]
    return nr


def _med_samlinger(con, kursrader) -> list[dict]:
    """Kursene med alle samlingene (sortert på dato og nummerert), sortert på første dato."""
    kurs = {r["id"]: {**_kursrad(r), "samlinger": []} for r in kursrader}
    if kurs:
        for r in con.execute(f"SELECT * FROM planlagt_samling WHERE planlagt_kurs_id IN ({','.join('?' * len(kurs))}) "
                             "ORDER BY fra_dato, id", tuple(kurs)):
            kurs[r["planlagt_kurs_id"]]["samlinger"].append(_samlingsrad(r))
    roller = roller_per_samling(con, [s["id"] for k in kurs.values() for s in k["samlinger"]])
    for k in kurs.values():
        for s in k["samlinger"]:
            s["roller"] = roller.get(s["id"], [])
            s["kursholdere_tekst"] = rolletekst(s["roller"], veiledere=False, fritekst=s["kursholdere"])
            s["veiledere_tekst"] = rolletekst(s["roller"], veiledere=True, fritekst=s["veiledere"])
        k["antall"] = _nummerer(k["samlinger"], k["antall_samlinger"])
        k["visningsnavn"] = visningsnavn(k["prosjektnr"], k["navn"],
                                         [samlingens_by(s["sted"], s["lokale"]) for s in k["samlinger"]])
        k["fra"] = k["samlinger"][0]["fra"] if k["samlinger"] else None
        k["til"] = max((s["til"] for s in k["samlinger"]), default=None)
        for s in k["samlinger"]:
            s["samlingsnavn"] = samlingsnavn(s)
        k["sjekk_tekster"] = ([k["sjekk"]] if k["sjekk"] else []) + [
            f"{s['samlingsnavn'] or s['fra'].strftime('%d.%m.%Y')}: {s['sjekk']}" for s in k["samlinger"] if s["sjekk"]]
        k["maa_sjekkes"] = bool(k["sjekk_tekster"])
    return sorted(kurs.values(), key=lambda k: (k["fra"] or date.max, k["visningsnavn"].casefold(), k["id"]))


def hent(con, plan_id: int) -> dict | None:
    """Kurset med alle samlingene, eller None."""
    rad = con.execute("SELECT * FROM planlagt_kurs WHERE id=?", (plan_id,)).fetchone()
    return _med_samlinger(con, [rad])[0] if rad else None


def liste(con) -> list[dict]:
    """Alle planlagte kurs med samlingene, sortert på første dato. Ikke de skjulte som holder sjekklisten til et kurs i
    systemet (kurs_id satt): de vises som kurset selv."""
    return _med_samlinger(con, con.execute("SELECT * FROM planlagt_kurs WHERE kurs_id IS NULL").fetchall())


def hent_flere(con, ider) -> dict[int, dict]:
    """{id: kurset med alle samlingene} for disse planlagte kursene."""
    ider = list(ider)
    if not ider:
        return {}
    rader = con.execute(f"SELECT * FROM planlagt_kurs WHERE id IN ({','.join('?' * len(ider))})", tuple(ider)).fetchall()
    return {k["id"]: k for k in _med_samlinger(con, rader)}


def samlinger_i_perioden(con, fra: date, til: date, *, sted: str = "", med_avlyste: bool = True,
                         med_knyttede: bool = False) -> list[dict]:
    """Samlingene med minst én dag fra og med `fra` til og med `til`, som dict med kursets felt (plan_id, visningsnavn,
    farge, ekstern, status, antall, kurs_id ...) og samlingens (nr_vist, fra, til, veiledningsdager ...). Nummereringen
    regnes ut fra ALLE kursets samlinger. `sted` er Kalenderens stedsfilter (bergen, oslo, online). De skjulte som holder
    sjekklisten til et kurs i systemet, er bare med når med_knyttede=True (kursholderoversikten)."""
    vilkar = ("ps.til_dato >= ? AND ps.fra_dato <= ?" + ("" if med_avlyste else " AND pk.status != 'avlyst'")
              + ("" if med_knyttede else " AND pk.kurs_id IS NULL"))
    ider = [r["id"] for r in con.execute(
        f"SELECT DISTINCT pk.id FROM planlagt_samling ps JOIN planlagt_kurs pk ON pk.id=ps.planlagt_kurs_id WHERE {vilkar}",
        (fra.isoformat(), til.isoformat()))]
    if not ider:
        return []
    ut = []
    for k in _med_samlinger(con, con.execute(f"SELECT * FROM planlagt_kurs WHERE id IN ({','.join('?' * len(ider))})",
                                             tuple(ider)).fetchall()):
        for s in k["samlinger"]:
            if s["til"] < fra or s["fra"] > til or not _passer_sted(s, sted):
                continue
            ut.append({**s, "samling_id": s["id"], "plan_id": k["id"], "prosjektnr": k["prosjektnr"],
                       "kursnavn": k["navn"], "visningsnavn": k["visningsnavn"], "arrangor": k["arrangor"],
                       "ekstern": k["ekstern"], "status": k["status"], "farge": k["farge"], "antall": k["antall"],
                       "kurs_sjekk": k["sjekk"], "kurs_id": k["kurs_id"]})
    return sorted(ut, key=lambda s: (s["fra"], s["visningsnavn"].casefold(), s["plan_id"]))


def _passer_sted(s: dict, sted: str) -> bool:
    if not sted:
        return True
    tekst = f"{s['sted'] or ''} {s['lokale'] or ''}".casefold()
    if sted == "online":
        return er_online(tekst)
    return sted in tekst
