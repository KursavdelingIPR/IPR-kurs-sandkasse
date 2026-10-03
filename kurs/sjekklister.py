"""Sjekklister for planlagte kurs (Camilla 02.10.2026: «litt som en nedtrekksmeny hvor det er en sjekkliste under hvert kurs»,
«Sjekklisten må være oversiktlig og vi må få påminnelse eller varsel dersom det er noe som ikke er gjort innen det står at
det skal være gjort»).

  * En MAL per kurstype (f.eks. «EFST 1-årig») har punkter med regler: hvilke samlinger punktet gjelder (alle, første,
    siste, alle unntatt første/siste eller nr. N), hvor (fysisk, online eller en by), for hvilke kurs (navnet inneholder en
    tekst) og fristen (antall dager, virkedager, uker eller måneder før eller etter samlingens start eller slutt).
  * Et planlagt kurs bruker én mal (planlagt_kurs_sjekkliste). Hver samling får sin egen sjekkliste med punktene som gjelder
    den, og fristene regnes ut fra datoene. En frist i en helg eller på en rød dag flyttes til virkedagen før.
  * Punkter krysses av (utført: hvem og når), kan merkes «Ikke aktuelt», få frist for hånd og tekstfelt («Lim inn her:»).
    Egne punkter kan legges til. «Husk»-punkter (påminnelser og lenker) har ingen avkryssing.
  * Endres samlingens datoer, flyttes fristene for åpne punkter som ikke er endret for hånd (flytt_frister).
  * «Oppdater fra malen» legger til nye punkter og retter tekst og frist på åpne punkter. Det som er krysset av eller merket
    «Ikke aktuelt», røres aldri.
  * Tilstand: forfalt (fristen er passert), snart (frist innen SNART_DAGER), senere, ferdig.
  * Lenker skrives som [tekst](https://…) og vises som lenker (bare http og https). Resten av teksten escapes.
  * Hendelsesloggen får aldri fritekst - bare id, antall og status.
"""
import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from markupsafe import Markup, escape

from . import booking, db, helligdager, kurskalender, planlagte_kurs

GJELDER = {"alle": "Alle samlinger", "forste": "Bare første samling", "siste": "Bare siste samling",
           "ikke_forste": "Alle unntatt første", "ikke_siste": "Alle unntatt siste", "nr": "Bare samling nr."}
ENHETER = {"dager": "dager", "virkedager": "virkedager", "uker": "uker", "maaneder": "måneder"}
ENHET_EN = {"dager": "dag", "virkedager": "virkedag", "uker": "uke", "maaneder": "måned"}
TYPER = {"oppgave": "Oppgave (avkryssing)", "husk": "Husk (påminnelse eller lenke, uten avkryssing)"}
STATUSER = {"aapen": "Ikke gjort", "utfort": "Utført", "ikke_aktuelt": "Ikke aktuelt"}
SNART_DAGER = 7
MAKS_TEKST, MAKS_NAVN, MAKS_BESKRIVELSE, MAKS_FELT, MAKS_VILKAR = 2000, 80, 500, 500, 80
MAKS_NOTAT = 1000
FASER = (("forfalt", "Forfalt"), ("maaned", "God tid før (ca. 1 måned)"), ("uker", "1–2 uker før"),
         ("dager", "Dagene før"), ("etter", "Under og etter samlingen"), ("uten", "Uten frist"))

_KONTROLLTEGN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LENKE = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^\s)<>\"']{1,2000})\)")
_ISO_DATO = re.compile(r"\d{4}-\d{2}-\d{2}")
_MND_KORT = ("jan", "feb", "mar", "apr", "mai", "jun", "jul", "aug", "sep", "okt", "nov", "des")


class SjekklisteFeil(ValueError):
    """Ugyldig input. Meldingen er trygg å vise til admin."""


# ============================ frister ============================

def er_virkedag(d: date) -> bool:
    return d.weekday() < 5 and d not in helligdager.i_perioden(d, d)


def _pluss_maaneder(d: date, antall: int) -> date:
    m = d.month - 1 + antall
    aar, maaned = d.year + m // 12, m % 12 + 1
    return date(aar, maaned, min(d.day, calendar.monthrange(aar, maaned)[1]))


def regn_frist(fra: date, til: date, antall: int | None, enhet: str, anker: str) -> date | None:
    """Fristen for et punkt: `antall` (negativ = før) dager/virkedager/uker/måneder fra samlingens start eller slutt.
    «1 måned før» 18. januar = 18. desember. En frist i en helg eller på en rød dag flyttes til virkedagen før."""
    if antall is None:
        return None
    d = fra if anker == "start" else til
    if enhet == "virkedager":
        steg, igjen = (1 if antall > 0 else -1), abs(antall)
        while igjen:
            d += timedelta(days=steg)
            igjen -= er_virkedag(d)
    elif enhet == "uker":
        d += timedelta(weeks=antall)
    elif enhet == "maaneder":
        d = _pluss_maaneder(d, antall)
    else:
        d += timedelta(days=antall)
    while not er_virkedag(d):
        d -= timedelta(days=1)
    return d


def regel_tekst(mp) -> str:
    """«1 måned før start», «3 virkedager etter slutt», «Ingen frist»."""
    if mp["frist_antall"] is None:
        return "Ingen frist"
    n = mp["frist_antall"]
    anker = "start" if mp["frist_fra"] == "start" else "slutt"
    if n == 0:
        return f"Samme dag som {anker}"
    enhet = ENHET_EN[mp["frist_enhet"]] if abs(n) == 1 else ENHETER[mp["frist_enhet"]]
    return f"{abs(n)} {enhet} {'før' if n < 0 else 'etter'} {anker}"


def gjelder_tekst(mp) -> str:
    """«Bare siste samling · Oslo · kurs med «Modul 3»» - tom når punktet gjelder alt."""
    deler = [] if mp["gjelder"] == "alle" else [
        f"Bare samling nr. {mp['gjelder_nr']}" if mp["gjelder"] == "nr" else GJELDER[mp["gjelder"]]]
    sted = (mp["gjelder_sted"] or "").strip()
    if sted:
        deler.append({"fysisk": "Bare fysiske", "online": "Bare online"}.get(sted.casefold(), f"Bare {sted}"))
    if mp["gjelder_navn"]:
        deler.append(f"Bare kurs med «{mp['gjelder_navn']}» i navnet")
    return " · ".join(deler)


# ============================ hvilke punkter som gjelder en samling ============================

@dataclass
class Samlingskontekst:
    samling_id: int
    plan_id: int
    kursnavn: str
    fra: date
    til: date
    nr: int | None              # samlingens nummer (lagret eller plass), None = kurset har én samling
    antall: int                 # samlinger i hele kurset
    sted: str | None
    lokale: str | None
    mal_id: int | None

    @property
    def forste(self) -> bool:
        return self.nr in (None, 1)

    @property
    def siste(self) -> bool:
        return self.nr is None or self.nr >= self.antall


def kontekst(con, samling_id: int) -> Samlingskontekst | None:
    rad = con.execute("SELECT planlagt_kurs_id FROM planlagt_samling WHERE id=?", (samling_id,)).fetchone()
    if not rad:
        return None
    kurs = planlagte_kurs.hent(con, rad["planlagt_kurs_id"])
    s = next(x for x in kurs["samlinger"] if x["id"] == samling_id)
    return Samlingskontekst(samling_id, kurs["id"], kurs["navn"], s["fra"], s["til"],
                            s["nr_vist"] if kurs["antall"] > 1 else None, kurs["antall"], s["sted"], s["lokale"],
                            mal_for_kurs(con, kurs["id"]))


def _passer(mp, ktx: Samlingskontekst) -> bool:
    g = mp["gjelder"]
    if (g == "forste" and not ktx.forste) or (g == "siste" and not ktx.siste) or (g == "ikke_forste" and ktx.forste) \
            or (g == "ikke_siste" and ktx.siste) or (g == "nr" and (ktx.nr or 1) != mp["gjelder_nr"]):
        return False
    sted = (mp["gjelder_sted"] or "").strip().casefold()
    online = planlagte_kurs.er_online(ktx.sted)
    if sted == "fysisk" and online or sted == "online" and not online:
        return False
    if sted and sted not in ("fysisk", "online"):
        by = planlagte_kurs.samlingens_by(ktx.sted, ktx.lokale)
        if not by or by.casefold() != sted:
            return False
    navn = (mp["gjelder_navn"] or "").strip().casefold()
    return not navn or navn in ktx.kursnavn.casefold()


def gjeldende_malpunkter(con, ktx: Samlingskontekst) -> list:
    """Malens punkter som gjelder samlingen, i malens rekkefølge. Et underpunkt tas bare med når punktet over er med."""
    if ktx.mal_id is None:
        return []
    ut, forelder_med = [], False
    for mp in con.execute("SELECT * FROM sjekkliste_malpunkt WHERE mal_id=? ORDER BY rekkefolge, id", (ktx.mal_id,)):
        med = _passer(mp, ktx) and (mp["nivaa"] == 0 or forelder_med)
        if mp["nivaa"] == 0:
            forelder_med = med
        if med:
            ut.append(mp)
    return ut


def _frist_for(mp, ktx: Samlingskontekst) -> str | None:
    frist = regn_frist(ktx.fra, ktx.til, mp["frist_antall"], mp["frist_enhet"], mp["frist_fra"])
    return frist.isoformat() if frist else None


# ============================ maler ============================

def _tekst(verdi, navn: str, maks: int, *, paakrevd: bool = False, flerlinjet: bool = False) -> str | None:
    tekst = str(verdi or "").strip()
    if not tekst:
        if paakrevd:
            raise SjekklisteFeil(f"{navn} må fylles ut.")
        return None
    if len(tekst) > maks:
        raise SjekklisteFeil(f"{navn} kan være høyst {maks} tegn.")
    if _KONTROLLTEGN.search(tekst) or (not flerlinjet and ("\n" in tekst or "\r" in tekst)):
        raise SjekklisteFeil(f"{navn} inneholder ugyldige tegn.")
    return tekst.replace("\r\n", "\n")


def _heltall(verdi, navn: str, minst: int, hoyst: int) -> int | None:
    tekst = str(verdi if verdi is not None else "").strip()
    if not tekst:
        return None
    if not tekst.isascii() or not tekst.isdigit() or not minst <= int(tekst) <= hoyst:
        raise SjekklisteFeil(f"{navn} må være et tall fra {minst} til {hoyst}.")
    return int(tekst)


def valider_mal(skjema) -> dict:
    return {"navn": _tekst(skjema.get("navn"), "Navn", MAKS_NAVN, paakrevd=True),
            "beskrivelse": _tekst(skjema.get("beskrivelse"), "Beskrivelse", MAKS_BESKRIVELSE, flerlinjet=True)}


def valider_malpunkt(skjema) -> dict:
    """Ett punkt i en mal fra skjemaet. Fristen skrives som tall + enhet + før/etter + start/slutt."""
    tekst = _tekst(skjema.get("tekst"), "Teksten", MAKS_TEKST, paakrevd=True, flerlinjet=True)      # første felt først
    type_ = skjema.get("type") or "oppgave"
    if type_ not in TYPER:
        raise SjekklisteFeil("Ukjent type.")
    gjelder = skjema.get("gjelder") or "alle"
    if gjelder not in GJELDER:
        raise SjekklisteFeil("Ugyldig valg for hvilke samlinger punktet gjelder.")
    nr = _heltall(skjema.get("gjelder_nr"), "Samlingens nummer", 1, 20)
    if gjelder == "nr" and nr is None:
        raise SjekklisteFeil("Skriv hvilken samling punktet gjelder (nummeret).")
    antall = _heltall(skjema.get("frist_antall"), "Fristen", 0, 366)
    enhet = skjema.get("frist_enhet") or "dager"
    retning = skjema.get("frist_retning") or "for"
    fra = skjema.get("frist_fra") or "start"
    if enhet not in ENHETER or retning not in ("for", "etter") or fra not in ("start", "slutt"):
        raise SjekklisteFeil("Ugyldig frist.")
    return {
        "type": type_, "tekst": tekst,
        "nivaa": 1 if str(skjema.get("nivaa") or "") in ("1", "on") else 0,
        "felt": 1 if str(skjema.get("felt") or "") in ("1", "on") else 0,
        "gjelder": gjelder, "gjelder_nr": nr if gjelder == "nr" else None,
        "gjelder_sted": _tekst(skjema.get("gjelder_sted"), "Sted", MAKS_VILKAR),
        "gjelder_navn": _tekst(skjema.get("gjelder_navn"), "Kursnavnet", MAKS_VILKAR),
        "frist_antall": None if antall is None or type_ == "husk" else (-antall if retning == "for" else antall),
        "frist_enhet": enhet, "frist_fra": fra,
    }


_MALPUNKTFELT = ("type", "tekst", "nivaa", "felt", "gjelder", "gjelder_nr", "gjelder_sted", "gjelder_navn", "frist_antall",
                 "frist_enhet", "frist_fra")


def opprett_mal(con, v: dict, aktor: str) -> int:
    mal_id = db.sett_inn(con, "INSERT INTO sjekkliste_mal (navn, beskrivelse, opprettet_av) VALUES (?,?,?)",
                         (v["navn"], v["beskrivelse"], aktor))
    db.logg(con, "sjekkliste_mal_opprettet", {"id": mal_id}, aktor=aktor)
    return mal_id


def oppdater_mal(con, mal_id: int, v: dict, aktor: str) -> bool:
    ok = con.execute("UPDATE sjekkliste_mal SET navn=?, beskrivelse=? WHERE id=?", (v["navn"], v["beskrivelse"], mal_id)).rowcount
    if ok:
        db.logg(con, "sjekkliste_mal_endret", {"id": mal_id}, aktor=aktor)
    return bool(ok)


def slett_mal(con, mal_id: int, aktor: str) -> dict | None:
    """Sletter malen med punktene. Kursene som brukte den, får ingen mal; sjekklistene som er laget, beholdes (punktene
    blir «egne punkter»)."""
    rad = con.execute("SELECT * FROM sjekkliste_mal WHERE id=?", (mal_id,)).fetchone()
    if not rad:
        return None
    con.execute("UPDATE sjekkliste_punkt SET malpunkt_id=NULL WHERE malpunkt_id IN "
                "(SELECT id FROM sjekkliste_malpunkt WHERE mal_id=?)", (mal_id,))
    con.execute("DELETE FROM planlagt_kurs_sjekkliste WHERE mal_id=?", (mal_id,))
    con.execute("DELETE FROM sjekkliste_malpunkt WHERE mal_id=?", (mal_id,))
    con.execute("DELETE FROM sjekkliste_mal WHERE id=?", (mal_id,))
    db.logg(con, "sjekkliste_mal_slettet", {"id": mal_id}, aktor=aktor)
    return dict(rad)


def hent_mal(con, mal_id: int) -> dict | None:
    rad = con.execute("SELECT * FROM sjekkliste_mal WHERE id=?", (mal_id,)).fetchone()
    if not rad:
        return None
    punkter = [{**dict(p), "regel": regel_tekst(p), "gjelder_tekst": gjelder_tekst(p), "tekst_html": tekst_html(p["tekst"])}
               for p in con.execute("SELECT * FROM sjekkliste_malpunkt WHERE mal_id=? ORDER BY rekkefolge, id", (mal_id,))]
    kurs = [r["planlagt_kurs_id"] for r in con.execute(
        "SELECT planlagt_kurs_id FROM planlagt_kurs_sjekkliste WHERE mal_id=?", (mal_id,))]
    return {**dict(rad), "punkter": punkter, "antall_kurs": len(kurs)}


def maler(con) -> list[dict]:
    """Alle malene med antall punkter og antall kurs som bruker dem, sortert på navn."""
    return [dict(r) for r in con.execute(
        """SELECT m.*, (SELECT COUNT(*) FROM sjekkliste_malpunkt p WHERE p.mal_id=m.id) AS antall_punkter,
                  (SELECT COUNT(*) FROM planlagt_kurs_sjekkliste k WHERE k.mal_id=m.id) AS antall_kurs
           FROM sjekkliste_mal m ORDER BY m.navn, m.id""")]


def opprett_malpunkt(con, mal_id: int, v: dict, aktor: str) -> int | None:
    if not con.execute("SELECT 1 FROM sjekkliste_mal WHERE id=?", (mal_id,)).fetchone():
        return None
    rekkefolge = con.execute("SELECT COALESCE(MAX(rekkefolge), 0) + 10 FROM sjekkliste_malpunkt WHERE mal_id=?",
                             (mal_id,)).fetchone()[0]
    pid = db.sett_inn(con, f"INSERT INTO sjekkliste_malpunkt (mal_id, rekkefolge, {', '.join(_MALPUNKTFELT)}) "
                           f"VALUES (?, ?, {', '.join('?' * len(_MALPUNKTFELT))})",
                      (mal_id, rekkefolge, *(v[f] for f in _MALPUNKTFELT)))
    db.logg(con, "sjekkliste_malpunkt_lagt_til", {"id": pid, "mal_id": mal_id}, aktor=aktor)
    return pid


def oppdater_malpunkt(con, mal_id: int, punkt_id: int, v: dict, aktor: str) -> bool:
    ok = con.execute(f"UPDATE sjekkliste_malpunkt SET {', '.join(f + '=?' for f in _MALPUNKTFELT)} WHERE id=? AND mal_id=?",
                     (*(v[f] for f in _MALPUNKTFELT), punkt_id, mal_id)).rowcount
    if ok:
        db.logg(con, "sjekkliste_malpunkt_endret", {"id": punkt_id, "mal_id": mal_id}, aktor=aktor)
    return bool(ok)


def slett_malpunkt(con, mal_id: int, punkt_id: int, aktor: str) -> bool:
    """Sletter punktet fra malen. Sjekklistene som er laget, beholder punktet som et eget punkt."""
    if not con.execute("SELECT 1 FROM sjekkliste_malpunkt WHERE id=? AND mal_id=?", (punkt_id, mal_id)).fetchone():
        return False
    con.execute("UPDATE sjekkliste_punkt SET malpunkt_id=NULL WHERE malpunkt_id=?", (punkt_id,))
    con.execute("DELETE FROM sjekkliste_malpunkt WHERE id=?", (punkt_id,))
    db.logg(con, "sjekkliste_malpunkt_slettet", {"id": punkt_id, "mal_id": mal_id}, aktor=aktor)
    return True


def flytt_malpunkt(con, mal_id: int, punkt_id: int, retning: str) -> bool:
    """Bytter plass med punktet over («opp») eller under («ned»). Nummererer malen på nytt (10, 20, 30 …)."""
    ider = [r["id"] for r in con.execute("SELECT id FROM sjekkliste_malpunkt WHERE mal_id=? ORDER BY rekkefolge, id",
                                         (mal_id,))]
    if punkt_id not in ider:
        return False
    i = ider.index(punkt_id)
    j = i - 1 if retning == "opp" else i + 1
    if 0 <= j < len(ider):
        ider[i], ider[j] = ider[j], ider[i]
    for nr, pid in enumerate(ider, start=1):
        con.execute("UPDATE sjekkliste_malpunkt SET rekkefolge=? WHERE id=?", (nr * 10, pid))
    return True


# ============================ kursets mal ============================

def mal_for_kurs(con, plan_id: int) -> int | None:
    rad = con.execute("SELECT mal_id FROM planlagt_kurs_sjekkliste WHERE planlagt_kurs_id=?", (plan_id,)).fetchone()
    return rad["mal_id"] if rad else None


def sett_mal(con, plan_id: int, mal_id: int | None, aktor: str) -> bool:
    """Velger kursets mal (None = ingen). Endrer ingen sjekklister som finnes (bruk «Oppdater fra malen»)."""
    if mal_id is not None and not con.execute("SELECT 1 FROM sjekkliste_mal WHERE id=?", (mal_id,)).fetchone():
        raise SjekklisteFeil("Malen finnes ikke.")
    if mal_for_kurs(con, plan_id) == mal_id:
        return False
    con.execute("DELETE FROM planlagt_kurs_sjekkliste WHERE planlagt_kurs_id=?", (plan_id,))
    if mal_id is not None:
        con.execute("INSERT INTO planlagt_kurs_sjekkliste (planlagt_kurs_id, mal_id) VALUES (?,?)", (plan_id, mal_id))
    db.logg(con, "planlagt_kurs_mal_valgt", {"id": plan_id, "mal_id": mal_id}, aktor=aktor)
    return True


# ============================ sjekklistene ============================

def _har_punkter(con, samling_id: int) -> bool:
    return con.execute("SELECT 1 FROM sjekkliste_punkt WHERE samling_id=? LIMIT 1", (samling_id,)).fetchone() is not None


def _sett_inn(con, samling_id: int, mp, rekkefolge: int, frist: str | None) -> int:
    return db.sett_inn(
        con, "INSERT INTO sjekkliste_punkt (samling_id, malpunkt_id, rekkefolge, nivaa, type, tekst, felt, frist) "
             "VALUES (?,?,?,?,?,?,?,?)",
        (samling_id, mp["id"], rekkefolge, mp["nivaa"], mp["type"], mp["tekst"], mp["felt"], frist))


def lag_for_samling(con, samling_id: int, aktor: str) -> int:
    """Lager sjekklisten for samlingen fra kursets mal - bare hvis samlingen ikke har punkter fra før. Antall punkter."""
    ktx = kontekst(con, samling_id)
    if ktx is None or ktx.mal_id is None or _har_punkter(con, samling_id):
        return 0
    punkter = gjeldende_malpunkter(con, ktx)
    for i, mp in enumerate(punkter, start=1):
        _sett_inn(con, samling_id, mp, i * 10, _frist_for(mp, ktx))
    db.logg(con, "sjekkliste_laget", {"samling_id": samling_id, "punkter": len(punkter)}, aktor=aktor)
    return len(punkter)


def lag_for_kurs(con, plan_id: int, aktor: str, *, fra: date | None = None) -> int:
    """Lager sjekklister for kursets samlinger som ikke har punkter, og som starter `fra` eller senere. Antall samlinger."""
    kurs = planlagte_kurs.hent(con, plan_id)
    if not kurs:
        return 0
    return sum(1 for s in kurs["samlinger"] if (fra is None or s["fra"] >= fra) and lag_for_samling(con, s["id"], aktor))


def oppdater_fra_mal(con, samling_id: int, aktor: str) -> dict:
    """Retter sjekklisten etter malen: nye punkter legges til, åpne punkter får malens tekst og frist (ikke en frist som er
    endret for hånd), og åpne punkter som ikke lenger gjelder, fjernes. Utførte og «ikke aktuelt» røres ikke, egne
    punkter heller ikke, og et punkt som er slettet fra denne sjekklisten, kommer ikke tilbake. Et punkt med notat fjernes
    aldri. {lagt_til, endret, fjernet}."""
    ktx = kontekst(con, samling_id)
    if ktx is None:
        return {"lagt_til": 0, "endret": 0, "fjernet": 0}
    finnes = {r["malpunkt_id"]: r for r in con.execute(
        "SELECT * FROM sjekkliste_punkt WHERE samling_id=? AND malpunkt_id IS NOT NULL", (samling_id,))}
    onsket = gjeldende_malpunkter(con, ktx)
    lagt_til = endret = fjernet = 0
    for i, mp in enumerate(onsket, start=1):
        rad = finnes.get(mp["id"])
        if rad is None:
            _sett_inn(con, samling_id, mp, i * 10, _frist_for(mp, ktx))
            lagt_til += 1
        elif rad["status"] == "aapen" and not rad["slettet"]:
            frist = rad["frist"] if rad["frist_manuell"] else _frist_for(mp, ktx)
            nytt = (i * 10, mp["nivaa"], mp["type"], mp["tekst"], mp["felt"], frist)
            if nytt != (rad["rekkefolge"], rad["nivaa"], rad["type"], rad["tekst"], rad["felt"], rad["frist"]):
                con.execute("UPDATE sjekkliste_punkt SET rekkefolge=?, nivaa=?, type=?, tekst=?, felt=?, frist=? WHERE id=?",
                            (*nytt, rad["id"]))
                endret += 1
        else:
            con.execute("UPDATE sjekkliste_punkt SET rekkefolge=? WHERE id=?", (i * 10, rad["id"]))
    onskede = {mp["id"] for mp in onsket}
    for mp_id, rad in finnes.items():
        if mp_id not in onskede and rad["status"] == "aapen" and not rad["notat"]:
            con.execute("DELETE FROM sjekkliste_punkt WHERE id=?", (rad["id"],))
            fjernet += 1
    db.logg(con, "sjekkliste_oppdatert", {"samling_id": samling_id, "lagt_til": lagt_til, "endret": endret,
                                          "fjernet": fjernet}, aktor=aktor)
    return {"lagt_til": lagt_til, "endret": endret, "fjernet": fjernet}


def flytt_frister(con, samling_id: int) -> int:
    """Samlingens datoer er endret: åpne punkter fra malen får ny frist (ikke frister som er endret for hånd)."""
    ktx = kontekst(con, samling_id)
    if ktx is None:
        return 0
    n = 0
    for rad in con.execute("""SELECT sp.id, sp.frist, mp.frist_antall, mp.frist_enhet, mp.frist_fra
                              FROM sjekkliste_punkt sp JOIN sjekkliste_malpunkt mp ON mp.id=sp.malpunkt_id
                              WHERE sp.samling_id=? AND sp.status='aapen' AND sp.frist_manuell=0""", (samling_id,)).fetchall():
        ny = _frist_for(rad, ktx)
        if ny != rad["frist"]:
            con.execute("UPDATE sjekkliste_punkt SET frist=? WHERE id=?", (ny, rad["id"]))
            n += 1
    return n


def _punkt(con, punkt_id: int):
    return con.execute("SELECT * FROM sjekkliste_punkt WHERE id=?", (punkt_id,)).fetchone()


def sett_status(con, punkt_id: int, status: str, aktor: str) -> bool:
    if status not in STATUSER:
        raise SjekklisteFeil("Ukjent status.")
    rad = _punkt(con, punkt_id)
    if not rad or rad["type"] != "oppgave":
        return False
    if rad["status"] != status:
        con.execute("UPDATE sjekkliste_punkt SET status=?, status_tid=?, status_av=? WHERE id=?",
                    (status, db.naa_utc(), aktor if status != "aapen" else None, punkt_id))
        db.logg(con, "sjekkliste_punkt_status", {"id": punkt_id, "samling_id": rad["samling_id"], "status": status},
                aktor=aktor)
    return True


def sett_felt(con, punkt_id: int, verdi, aktor: str) -> bool:
    rad = _punkt(con, punkt_id)
    if not rad or not rad["felt"]:
        return False
    tekst = _tekst(verdi, "Feltet", MAKS_FELT)
    if tekst != rad["felt_verdi"]:
        con.execute("UPDATE sjekkliste_punkt SET felt_verdi=? WHERE id=?", (tekst, punkt_id))
        db.logg(con, "sjekkliste_punkt_felt", {"id": punkt_id, "samling_id": rad["samling_id"]}, aktor=aktor)
    return True


def sett_frist(con, punkt_id: int, verdi, aktor: str) -> bool:
    """Frist for hånd (ÅÅÅÅ-MM-DD). Tom = følg datoene igjen (regnes ut fra malen)."""
    rad = _punkt(con, punkt_id)
    if not rad or rad["type"] != "oppgave":
        return False
    tekst = str(verdi or "").strip()
    if tekst:
        if not _ISO_DATO.fullmatch(tekst):
            raise SjekklisteFeil("Fristen må være en dato.")
        try:
            date.fromisoformat(tekst)
        except ValueError:
            raise SjekklisteFeil("Fristen er ikke en gyldig dato.") from None
        con.execute("UPDATE sjekkliste_punkt SET frist=?, frist_manuell=1 WHERE id=?", (tekst, punkt_id))
    else:
        con.execute("UPDATE sjekkliste_punkt SET frist_manuell=0 WHERE id=?", (punkt_id,))
        if rad["malpunkt_id"] is None:
            con.execute("UPDATE sjekkliste_punkt SET frist=NULL WHERE id=?", (punkt_id,))
        else:
            flytt_frister(con, rad["samling_id"])
    db.logg(con, "sjekkliste_punkt_frist", {"id": punkt_id, "samling_id": rad["samling_id"], "frist": tekst or None},
            aktor=aktor)
    return True


def legg_til_punkt(con, samling_id: int, skjema, aktor: str) -> int | None:
    """Eget punkt på én samlings sjekkliste (tekst og ev. frist)."""
    if not con.execute("SELECT 1 FROM planlagt_samling WHERE id=?", (samling_id,)).fetchone():
        return None
    tekst = _tekst(skjema.get("tekst"), "Teksten", MAKS_TEKST, paakrevd=True, flerlinjet=True)
    frist = str(skjema.get("frist") or "").strip() or None
    if frist:
        if not _ISO_DATO.fullmatch(frist):
            raise SjekklisteFeil("Fristen må være en dato.")
        try:
            date.fromisoformat(frist)
        except ValueError:
            raise SjekklisteFeil("Fristen er ikke en gyldig dato.") from None
    type_ = "husk" if skjema.get("type") == "husk" else "oppgave"
    rekkefolge = con.execute("SELECT COALESCE(MAX(rekkefolge), 0) + 10 FROM sjekkliste_punkt WHERE samling_id=?",
                             (samling_id,)).fetchone()[0]
    pid = db.sett_inn(con, "INSERT INTO sjekkliste_punkt (samling_id, rekkefolge, type, tekst, frist, frist_manuell) "
                           "VALUES (?,?,?,?,?,?)", (samling_id, rekkefolge, type_, tekst, frist if type_ == "oppgave" else None,
                                                    1 if frist else 0))
    db.logg(con, "sjekkliste_punkt_lagt_til", {"id": pid, "samling_id": samling_id}, aktor=aktor)
    return pid


def slett_punkt(con, punkt_id: int, aktor: str) -> dict | None:
    """Tar punktet bort fra sjekklisten (Camilla 03.10: «slette det som ikke er aktuelt»). Et eget punkt slettes helt; et
    punkt fra malen merkes slettet (sjekkliste_punkt.slettet), så «Oppdater fra malen» ikke legger det inn igjen. Det kan
    tas tilbake (gjenopprett_punkt)."""
    rad = _punkt(con, punkt_id)
    if not rad:
        return None
    fra_malen = rad["malpunkt_id"] is not None
    if fra_malen:
        con.execute("UPDATE sjekkliste_punkt SET slettet=1 WHERE id=?", (punkt_id,))
    else:
        con.execute("DELETE FROM sjekkliste_punkt WHERE id=?", (punkt_id,))
    db.logg(con, "sjekkliste_punkt_slettet", {"id": punkt_id, "samling_id": rad["samling_id"], "fra_malen": fra_malen},
            aktor=aktor)
    return dict(rad)


def gjenopprett_punkt(con, punkt_id: int, aktor: str) -> bool:
    """Tar et slettet punkt fra malen tilbake på sjekklisten."""
    rad = _punkt(con, punkt_id)
    if not rad or not rad["slettet"]:
        return False
    con.execute("UPDATE sjekkliste_punkt SET slettet=0 WHERE id=?", (punkt_id,))
    db.logg(con, "sjekkliste_punkt_gjenopprettet", {"id": punkt_id, "samling_id": rad["samling_id"]}, aktor=aktor)
    return True


def sett_notat(con, punkt_id: int, verdi, aktor: str) -> bool:
    """Notatet på punktet: hva som er gjort, og når (Camilla 03.10: «vi må skrive når vi har gjort oppgavene»). Tomt =
    ingen notat. Loggen får bare id-er, aldri teksten."""
    rad = _punkt(con, punkt_id)
    if not rad:
        return False
    tekst = _tekst(verdi, "Notatet", MAKS_NOTAT, flerlinjet=True)
    if tekst != rad["notat"]:
        con.execute("UPDATE sjekkliste_punkt SET notat=? WHERE id=?", (tekst, punkt_id))
        db.logg(con, "sjekkliste_punkt_notat", {"id": punkt_id, "samling_id": rad["samling_id"]}, aktor=aktor)
    return True


def samling_for_punkt(con, punkt_id: int) -> tuple[int, int] | None:
    """(planlagt_kurs_id, samling_id) for punktet."""
    rad = con.execute("""SELECT ps.planlagt_kurs_id, ps.id FROM sjekkliste_punkt sp
                         JOIN planlagt_samling ps ON ps.id=sp.samling_id WHERE sp.id=?""", (punkt_id,)).fetchone()
    return (rad[0], rad[1]) if rad else None


# ============================ visning ============================

def tekst_html(tekst: str) -> Markup:
    """Teksten med lenkene [tekst](https://…) som <a>. Alt annet escapes; bare http(s) blir lenke."""
    ut, pos = [], 0
    for m in _LENKE.finditer(tekst or ""):
        ut.append(escape(tekst[pos:m.start()]))
        ut.append(Markup('<a href="{}" target="_blank" rel="noopener noreferrer">{}</a>').format(m.group(2), m.group(1)))
        pos = m.end()
    ut.append(escape((tekst or "")[pos:]))
    return Markup("").join(ut)


def kort_tekst(tekst: str, maks: int = 40) -> str:
    """Første linje av teksten uten lenkesyntaks, forkortet med «…» (til «Hovedpunkt › underpunkt»)."""
    ren = _LENKE.sub(lambda m: m.group(1), (tekst or "").strip()).splitlines()
    ren = ren[0].strip() if ren else ""
    if len(ren) <= maks:
        return ren
    kutt = ren[:maks - 1]
    if ren[maks - 1] != " " and " " in kutt[maks // 2:]:
        kutt = kutt[:kutt.rindex(" ")]                                # helst ved et mellomrom
    return kutt.rstrip(" ,.:;–-") + "…"


def dato_tekst(d: date | None) -> str:
    return f"{d.day}. {_MND_KORT[d.month - 1]}" if d else ""


@dataclass
class Punkt:
    id: int
    tekst: str
    tekst_html: Markup
    type: str
    nivaa: int
    felt: bool
    felt_verdi: str | None
    frist: date | None
    frist_manuell: bool
    status: str
    status_tid: str | None
    status_av: str | None
    eget: bool                      # lagt til for hånd (kan slettes)
    tilstand: str = ""              # forfalt | snart | senere | uten_frist | utfort | ikke_aktuelt | husk
    fase: str = ""
    forelder_id: int | None = None  # underpunkt: hovedpunktet over i malens rekkefølge
    forelder_kort: str = ""         # underpunkt borte fra hovedpunktet sitt: hovedpunktet vises grått over (uten avkrysning)
    innrykk: bool = False           # underpunkt (rykkes inn under hovedpunktet, et søsken eller den grå linjen)
    notat: str | None = None        # hva som er gjort (skrives inn under «Endre»)

    @property
    def ferdig(self) -> bool:
        return self.status in ("utfort", "ikke_aktuelt")

    @property
    def utfort_tekst(self) -> str:
        """«utført · Camilla 16. okt» (brukernavnet fra aktøren), «ikke aktuelt · …»."""
        hvem = (self.status_av or "").split(":", 1)[-1]
        naar = dato_tekst(date.fromisoformat(self.status_tid[:10])) if self.status_tid else ""
        return " · ".join(x for x in (STATUSER[self.status].lower(), hvem, naar) if x)

    @property
    def frist_tekst(self) -> str:
        return f"frist {dato_tekst(self.frist)}" if self.frist else ""


@dataclass
class Sjekkliste:
    samling_id: int
    punkter: list
    idag: date
    grupper: list = field(default_factory=list)     # [(fase, navn, [Punkt])]
    husk: list = field(default_factory=list)
    slettede: list = field(default_factory=list)    # punkter fra malen som er slettet her (kan tas tilbake)

    @property
    def oppgaver(self) -> list:
        return [p for p in self.punkter if p.type == "oppgave" and p.status != "ikke_aktuelt"]

    @property
    def antall(self) -> int:
        return len(self.oppgaver)

    @property
    def ferdige(self) -> int:
        return sum(1 for p in self.oppgaver if p.status == "utfort")

    @property
    def forfalt(self) -> int:
        return sum(1 for p in self.oppgaver if p.tilstand == "forfalt")

    @property
    def snart(self) -> int:
        return sum(1 for p in self.oppgaver if p.tilstand == "snart")

    @property
    def neste_frist(self) -> date | None:
        return min((p.frist for p in self.oppgaver if p.status == "aapen" and p.frist and p.frist >= self.idag),
                   default=None)

    @property
    def neste_frist_tekst(self) -> str:
        return dato_tekst(self.neste_frist)

    @property
    def ferdig(self) -> bool:
        return self.antall > 0 and self.ferdige == self.antall


def _fase(frist: date | None, fra: date) -> str:
    if frist is None:
        return "uten"
    dager = (fra - frist).days
    return "maaned" if dager >= 21 else "uker" if dager >= 6 else "dager" if dager >= 1 else "etter"


def for_samlinger(con, samling_ider, idag: date) -> dict[int, Sjekkliste]:
    """{samling_id: Sjekkliste} for samlingene som har punkter. Punktene er gruppert: forfalte øverst, så etter når
    fristen er (ca. 1 måned før, 1–2 uker før, dagene før, under og etter samlingen, uten frist)."""
    ider = list(dict.fromkeys(samling_ider))
    if not ider:
        return {}
    starter = {r["id"]: date.fromisoformat(r["fra_dato"]) for r in con.execute(
        f"SELECT id, fra_dato FROM planlagt_samling WHERE id IN ({','.join('?' * len(ider))})", tuple(ider))}
    per: dict[int, list] = {}
    slettet: dict[int, list] = {}
    hoved: dict[int, Punkt] = {}        # siste hovedpunkt per samling (forelder for underpunktene etter det)
    for r in con.execute(f"SELECT * FROM sjekkliste_punkt WHERE samling_id IN ({','.join('?' * len(ider))}) "
                         "ORDER BY rekkefolge, id", tuple(ider)):
        frist = date.fromisoformat(r["frist"]) if r["frist"] else None
        p = Punkt(r["id"], r["tekst"], tekst_html(r["tekst"]), r["type"], r["nivaa"], bool(r["felt"]), r["felt_verdi"],
                  frist, bool(r["frist_manuell"]), r["status"], r["status_tid"], r["status_av"], r["malpunkt_id"] is None,
                  notat=r["notat"])
        if r["slettet"]:
            slettet.setdefault(r["samling_id"], []).append(p)
            continue
        if p.nivaa == 0:
            if not p.eget:                  # egne punkter er aldri hovedpunkt for malens underpunkter
                hoved[r["samling_id"]] = p
        elif r["samling_id"] in hoved:
            p.forelder_id = hoved[r["samling_id"]].id
        if p.type == "husk":
            p.tilstand = "husk"
        elif p.status != "aapen":
            p.tilstand = p.status
        elif frist is None:
            p.tilstand = "uten_frist"
        else:
            p.tilstand = "forfalt" if frist < idag else "snart" if (frist - idag).days <= SNART_DAGER else "senere"
        p.fase = "forfalt" if p.tilstand == "forfalt" else _fase(frist, starter[r["samling_id"]])
        per.setdefault(r["samling_id"], []).append(p)
    ut = {}
    for sid in dict.fromkeys([*per, *slettet]):
        punkter = per.get(sid, [])
        sk = Sjekkliste(sid, punkter, idag, slettede=slettet.get(sid, []))
        sk.husk = [p for p in punkter if p.type == "husk"]
        oppgaver = [p for p in punkter if p.type == "oppgave"]
        for fase, navn in FASER:
            i_fasen = [p for p in oppgaver if p.fase == fase]
            if fase not in ("forfalt", "uten"):
                i_fasen.sort(key=lambda p: (p.frist or date.max))       # stabil: malens rekkefølge ved lik frist
            if i_fasen:
                _merk_underpunkter(i_fasen, {p.id: p for p in punkter})
                sk.grupper.append((fase, navn, i_fasen))
        ut[sid] = sk
    return ut


def _merk_underpunkter(liste: list, alle: dict) -> None:
    """Underpunktene rykkes inn. Står et underpunkt ikke rett under hovedpunktet sitt (eller et søsken) – fordi fristen
    legger det i en annen fase enn hovedpunktet – vises hovedpunktet grått over det, så det ikke ser løsrevet ut."""
    forrige = None
    for p in liste:
        if p.nivaa:
            rett_under = forrige is not None and p.forelder_id is not None and (
                forrige.id == p.forelder_id or (forrige.nivaa == 1 and forrige.forelder_id == p.forelder_id))
            if not rett_under and p.forelder_id in alle:
                p.forelder_kort = kort_tekst(alle[p.forelder_id].tekst, 90)
            p.innrykk = rett_under or bool(p.forelder_kort)
        forrige = p


def forfalte_samlinger(con, idag: date) -> set[int]:
    """Samlingene (id) med minst ett forfalt punkt."""
    return {r[0] for r in con.execute("SELECT DISTINCT samling_id FROM sjekkliste_punkt WHERE status='aapen' AND slettet=0 "
                                      "AND type='oppgave' AND frist IS NOT NULL AND frist < ?", (idag.isoformat(),))}


def legg_paa(con, hendelser, idag: date) -> None:
    """Setter h.sjekkliste (Sjekkliste eller None), h.har_mal og h.booking på kalenderens hendelser som har en
    sjekkliste-holder (h.sjekk_plan_id): de planlagte kursene, og kursene i systemet som er knyttet til et skjult planlagt
    kurs (synk_kurs)."""
    med = [h for h in hendelser if getattr(h, "sjekk_plan_id", None) is not None and getattr(h, "samling_id", None)]
    lister = for_samlinger(con, [h.samling_id for h in med], idag)
    bookinger = booking.for_samlinger(con, [h.samling_id for h in med])
    med_mal = {r[0] for r in con.execute("SELECT planlagt_kurs_id FROM planlagt_kurs_sjekkliste")} if med else set()
    for h in med:
        h.sjekkliste = lister.get(h.samling_id)
        h.har_mal = h.sjekk_plan_id in med_mal
        h.booking = bookinger.get(h.samling_id, {})


# ============================ kurs i systemet ============================

# Malen et kurs i systemet får, ut fra navnet (som da kursplanen ble lest inn): første treff gjelder, ellers Kortkurs
_MAL_ETTER_NAVN = (("når angsten styrer familien", "Enkel"), ("påbygg", "EFT påbygg"), ("efst", "EFST 1-årig"),
                   ("spesialistutdanning", "Modul (M1–M3)"), ("modul", "Modul (M1–M3)"), ("eft 1-årig", "EFT 1-årig"),
                   ("videreutdanning", "EFT 1-årig"))
STANDARDMAL = "Kortkurs (1–3 dager)"


def gjett_mal(con, kursnavn: str) -> int | None:
    """Malen som passer best til et kurs ut fra navnet, eller None når den malen ikke finnes."""
    navn = (kursnavn or "").casefold()
    malnavn = next((mal for nokkel, mal in _MAL_ETTER_NAVN if nokkel in navn), STANDARDMAL)
    rad = con.execute("SELECT id FROM sjekkliste_mal WHERE navn=?", (malnavn,)).fetchone()
    return rad["id"] if rad else None


def _kursets_samlinger(con, kurs_id: int) -> list[dict]:
    """Samlingene i et kurs i systemet med datoene fra kursdagene, i den rekkefølgen de starter."""
    return [dict(r) for r in con.execute(
        """SELECT s.id, s.navn, MIN(kd.dato) AS fra, MAX(kd.dato) AS til FROM samling s
           JOIN kursdag kd ON kd.samling_id=s.id WHERE s.kurs_id=? GROUP BY s.id, s.navn ORDER BY MIN(kd.dato), s.id""",
        (kurs_id,))]


def synk_kurs(con, idag: date, aktor: str = "system:sjekkliste") -> int:
    """Sjekkliste på alle kurs i systemet (Camilla 03.10.2026: «Må være sjekkliste på alle kurs i kurskalenderen»).

    Hvert kurs som ikke er over (siste kursdag tidligst 30 dager før `idag`) og som har samlinger, får et skjult planlagt
    kurs (planlagt_kurs.kurs_id) med én planlagt samling per samling i kurset (kurs_samling_id), malen som passer best
    (gjett_mal) og sjekkliste per samling. Finnes det fra før, følger det kurset: navn, datoer, nummer og sted oppdateres
    (åpne frister flyttes), nye samlinger får sjekkliste, samlinger som er tatt bort, fjernes, og et avlyst kurs gir en
    avlyst sjekkliste (ikke med i påminnelsene). Idempotent. Returnerer antall endringer (0 = ingenting er endret)."""
    endringer = 0
    grense = (idag - timedelta(days=30)).isoformat()
    for k in con.execute("""SELECT k.id, k.navn, k.type, k.sted, k.status FROM kurs k
                            WHERE EXISTS (SELECT 1 FROM kursdag kd WHERE kd.kurs_id=k.id AND kd.dato >= ?)
                               OR EXISTS (SELECT 1 FROM planlagt_kurs pk WHERE pk.kurs_id=k.id)
                            ORDER BY k.id""", (grense,)).fetchall():
        plan = con.execute("SELECT id, navn, status FROM planlagt_kurs WHERE kurs_id=?", (k["id"],)).fetchone()
        status = "avlyst" if k["status"] == "avlyst" else "planlagt"
        if plan is None:
            samlinger = _kursets_samlinger(con, k["id"])
            mal_id = gjett_mal(con, k["navn"]) if status == "planlagt" and samlinger else None
            if mal_id is None:
                continue
            plan_id = db.sett_inn(con, "INSERT INTO planlagt_kurs (navn, arrangor, status, farge, kilde, opprettet_av, kurs_id) "
                                       "VALUES (?,?,?,?,?,?,?)",
                                  (k["navn"], "IPR", status, 0, "Kurs i systemet", aktor, k["id"]))
            sett_mal(con, plan_id, mal_id, aktor)
            endringer += 1
        else:
            plan_id = plan["id"]
            if (plan["navn"], plan["status"]) != (k["navn"], status):
                con.execute("UPDATE planlagt_kurs SET navn=?, status=? WHERE id=?", (k["navn"], status, plan_id))
                endringer += 1
            samlinger = _kursets_samlinger(con, k["id"])
        endringer += _synk_samlinger(con, plan_id, samlinger, "Online" if k["type"] == "digital" else k["sted"], aktor)
    return endringer


def _synk_samlinger(con, plan_id: int, samlinger: list[dict], sted: str | None, aktor: str) -> int:
    endringer = 0
    finnes: dict[int, object] = {}
    borte = []
    for r in con.execute("SELECT * FROM planlagt_samling WHERE planlagt_kurs_id=?", (plan_id,)):
        if r["kurs_samling_id"] is None or r["kurs_samling_id"] in finnes:
            borte.append(r["id"])
        else:
            finnes[r["kurs_samling_id"]] = r
    for nr, s in enumerate(samlinger, start=1):
        onsket = (s["fra"], s["til"], nr if len(samlinger) > 1 else None, s["navn"], sted)
        r = finnes.pop(s["id"], None)
        if r is None:
            sid = db.sett_inn(con, "INSERT INTO planlagt_samling (planlagt_kurs_id, fra_dato, til_dato, nr, navn, sted, "
                                   "kurs_samling_id) VALUES (?,?,?,?,?,?,?)", (plan_id, *onsket, s["id"]))
            lag_for_samling(con, sid, aktor)
            endringer += 1
        elif (r["fra_dato"], r["til_dato"], r["nr"], r["navn"], r["sted"]) != onsket:
            con.execute("UPDATE planlagt_samling SET fra_dato=?, til_dato=?, nr=?, navn=?, sted=? WHERE id=?", (*onsket, r["id"]))
            flytt_frister(con, r["id"])
            endringer += 1
    for sid in borte + [r["id"] for r in finnes.values()]:
        con.execute("DELETE FROM planlagt_samling WHERE id=?", (sid,))
        endringer += 1
    return endringer


# ============================ påminnelser ============================

def _periode(fra: date, til: date) -> str:
    if fra == til:
        return f"{fra.day}. {kurskalender.MAANEDER[fra.month - 1].lower()} {fra.year}"
    return kurskalender.periode_tekst(fra, til)


def paaminnelser(con, idag: date) -> dict:
    """Det som skal minnes om, til kortet på Oversikten og morgen-e-posten til kurs@ipr.no: åpne oppgaver med frist
    som er passert (forfalt) eller kommer de neste SNART_DAGER dagene (snart), i planlagte kurs som ikke er avlyst.
    Gruppert per samling: de med noe forfalt først, så etter tidligste frist.
    {"samlinger": [{plan_id, samling_id, kursnavn, samling, forfalt: [...], snart: [...]}], "forfalt": n, "snart": n}"""
    rader = con.execute(
        """SELECT sp.id, sp.tekst, sp.frist, sp.samling_id, ps.planlagt_kurs_id AS plan_id
           FROM sjekkliste_punkt sp JOIN planlagt_samling ps ON ps.id=sp.samling_id
           JOIN planlagt_kurs pk ON pk.id=ps.planlagt_kurs_id
           WHERE sp.status='aapen' AND sp.slettet=0 AND sp.type='oppgave' AND sp.frist IS NOT NULL AND sp.frist <= ?
             AND pk.status != 'avlyst' AND pk.ekstern = 0
           ORDER BY sp.frist, sp.rekkefolge, sp.id""",
        ((idag + timedelta(days=SNART_DAGER)).isoformat(),)).fetchall()
    samlinger = {s["id"]: (k, s) for k in planlagte_kurs.hent_flere(con, {r["plan_id"] for r in rader}).values()
                 for s in k["samlinger"]}
    per: dict[int, dict] = {}
    for r in rader:
        k, s = samlinger[r["samling_id"]]
        frist = date.fromisoformat(r["frist"])
        v = per.setdefault(s["id"], {
            "plan_id": k["id"], "samling_id": s["id"], "kursnavn": k["visningsnavn"], "fra": s["fra"],
            "samling": " · ".join(x for x in (s["samlingsnavn"], _periode(s["fra"], s["til"])) if x),
            "forfalt": [], "snart": []})
        (v["forfalt"] if frist < idag else v["snart"]).append(
            {"id": r["id"], "tekst": r["tekst"], "tekst_html": tekst_html(r["tekst"]), "frist": frist,
             "frist_tekst": dato_tekst(frist), "forfalt": frist < idag})
    liste = sorted(per.values(), key=lambda v: (not v["forfalt"], (v["forfalt"] or v["snart"])[0]["frist"], v["fra"],
                                                 v["kursnavn"].casefold()))
    for v in liste:
        v["punkter"] = v["forfalt"] + v["snart"]          # forfalte først, så etter frist (visningen viser de første)
    return {"samlinger": liste, "forfalt": sum(len(v["forfalt"]) for v in liste),
            "snart": sum(len(v["snart"]) for v in liste)}
