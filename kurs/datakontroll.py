"""Fang opp opplysninger som ser feil ut (Camilla 07.10.2026: «fange opp ting som kan virke feil, som feks hvis noen skriver
gmal.com, eller hotmail.no osv. … Da kan det markeres et sted du tenker blir bra og oversiktlig»).

Kontrollene er bare forslag: ingenting stoppes eller endres automatisk. Funnene vises
  * i påmeldingsskjemaet (et «Mente du …?» under e-postfeltet, static/app.js - samme domeneliste som her),
  * på Oversikten (kortet «Mulig feil i påmeldinger») og på siden /admin/datakontroll,
  * i deltakervinduet («Ser dette riktig ut?») og i deltakerlisten (gult merke ved navnet).
«Det stemmer» lagrer at akkurat denne verdien er sjekket (datakontroll_ok, bare en sha256 av verdien - ingen kopi av
personopplysningen). Endres verdien senere, kontrolleres den på nytt.
"""
import hashlib
import json
import re
from dataclasses import dataclass

from . import db

# E-posttjenester som er vanlige i Norge. En adresse hos en av disse er alltid i orden.
VANLIGE_DOMENER = ("gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "live.com", "live.no", "msn.com",
                   "yahoo.com", "yahoo.no", "ymail.com", "icloud.com", "me.com", "mac.com", "online.no", "mail.com",
                   "protonmail.com", "proton.me", "aol.com")
# Adresser som finnes, men som oftest er en skrivefeil (Camilla: «hotmail.no»)
SJELDNE_DOMENER = {"hotmail.no": "hotmail.com", "gmail.no": "gmail.com", "outlook.no": "outlook.com", "icloud.no": "icloud.com"}
# Endelser som ikke finnes: rettes til det som nesten alltid er ment
TLD_FEIL = {"con": "com", "cmo": "com", "cpm": "com", "comm": "com", "vom": "com", "xom": "com", "nett": "net", "noo": "no",
            "nno": "no"}

KODER = {"epost": "E-post", "navn": "Navn", "telefon": "Telefon", "postnr": "Postnummer", "dublett": "Mulig dublett"}
_KURSSTATUSER = ("utkast", "aapen", "full", "aktiv")       # kurs som ikke er avsluttet eller avlyst


@dataclass(frozen=True)
class Funn:
    kode: str                   # epost / navn / telefon / postnr / dublett
    tekst: str                  # det som vises: «Mente du kari@gmail.com?»
    verdi: str                  # verdien som ble kontrollert («Det stemmer» gjelder bare den)
    forslag: str | None = None

    @property
    def felt(self) -> str:
        return KODER[self.kode]


def avstand(a: str, b: str) -> int:
    """Antall tastefeil mellom a og b (bokstav for mye, for lite, feil eller to som har byttet plass)."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            kost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + kost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def domene_forslag(domene: str) -> str | None:
    """Det domenet som trolig var ment, eller None når domenet ser riktig ut."""
    domene = (domene or "").strip().lower().rstrip(".")
    if not domene or domene in VANLIGE_DOMENER:
        return None
    if domene in SJELDNE_DOMENER:
        return SJELDNE_DOMENER[domene]
    grense = 1 if len(domene) <= 8 else 2
    naermest = min(VANLIGE_DOMENER, key=lambda d: avstand(domene, d))
    if avstand(domene, naermest) <= grense:
        return naermest
    if "." in domene:
        navn, tld = domene.rsplit(".", 1)
        if tld in TLD_FEIL:
            return f"{navn}.{TLD_FEIL[tld]}"
    return None


def sjekk_epost(epost) -> Funn | None:
    epost = (epost or "").strip()
    if not epost:
        return None
    if epost.count("@") != 1 or " " in epost or ".." in epost or "," in epost:
        return Funn("epost", "E-postadressen ser ufullstendig eller feilskrevet ut.", epost)
    lokal, domene = epost.split("@")
    if not lokal or "." not in domene:
        return Funn("epost", "E-postadressen ser ufullstendig ut (mangler noe etter @).", epost)
    riktig = domene_forslag(domene)
    if riktig:
        forslag = f"{lokal}@{riktig}"
        return Funn("epost", f"Mente du {forslag}?", epost, forslag)
    return None


def _stor_forbokstav(navn: str) -> str:
    """«kari-anne nordmann» -> «Kari-Anne Nordmann»."""
    return re.sub(r"[^\W\d_]+", lambda m: m.group(0)[:1].upper() + m.group(0)[1:].lower(), navn)


def sjekk_navn(fornavn, etternavn) -> Funn | None:
    fornavn, etternavn = (fornavn or "").strip(), (etternavn or "").strip()
    navn = f"{fornavn} {etternavn}".strip()
    bokstaver = [c for c in navn if c.isalpha()]
    if not bokstaver:
        return None
    if "@" in navn:
        return Funn("navn", "Navnet ser ut som en e-postadresse.", navn)
    if any(c.isdigit() for c in navn):
        return Funn("navn", "Navnet inneholder tall.", navn)
    if all(c.islower() for c in bokstaver):
        return Funn("navn", f"Navnet står med små bokstaver. Mente du {_stor_forbokstav(navn)}?", navn, _stor_forbokstav(navn))
    if len(bokstaver) > 3 and all(c.isupper() for c in bokstaver):
        return Funn("navn", f"Navnet står med store bokstaver. Mente du {_stor_forbokstav(navn)}?", navn, _stor_forbokstav(navn))
    if fornavn and fornavn.casefold() == etternavn.casefold():
        return Funn("navn", "Fornavn og etternavn er like.", navn)
    return None


def sjekk_telefon(telefon) -> Funn | None:
    telefon = (telefon or "").strip()
    if not telefon:
        return None
    if re.search(r"[^\W\d_]", telefon):
        return Funn("telefon", "Telefonnummeret inneholder bokstaver.", telefon)
    sifre = re.sub(r"\D", "", telefon)
    utenlandsk = telefon.startswith("+") or telefon.startswith("00")
    if telefon.startswith("+47") or telefon.startswith("0047"):
        sifre, utenlandsk = sifre[2:] if telefon.startswith("+47") else sifre[4:], False
    if utenlandsk:
        return None if 6 <= len(sifre) <= 15 else Funn("telefon", f"Telefonnummeret har {len(sifre)} siffer.", telefon)
    if len(sifre) != 8:
        return Funn("telefon", f"Telefonnummeret har {len(sifre)} siffer (norske nummer har 8).", telefon)
    return None


def sjekk_postnr(postnr) -> Funn | None:
    postnr = (postnr or "").strip()
    if not postnr or re.fullmatch(r"\d{4}", postnr):
        return None
    return Funn("postnr", "Postnummeret skal ha fire siffer (norske adresser).", postnr)


def sjekk_person(d) -> list[Funn]:
    """Kontrollene for én person (en rad med epost, fornavn, etternavn, telefon og postnr)."""
    def verdi(navn):
        try:
            return d[navn]
        except (KeyError, IndexError):
            return None
    funn = [sjekk_epost(verdi("epost")), sjekk_navn(verdi("fornavn"), verdi("etternavn")), sjekk_telefon(verdi("telefon")),
            sjekk_postnr(verdi("postnr"))]
    return [f for f in funn if f]


def _normalt_navn(r) -> str:
    return re.sub(r"\s+", " ", f"{r['fornavn'] or ''} {r['etternavn'] or ''}".strip()).casefold()


def dubletter(rader) -> dict[int, Funn]:
    """{paamelding_id: Funn} for påmeldinger på samme kurs som trolig er samme person: samme navn eller samme telefonnummer,
    men ulike deltakere (e-postadressene er ulike - samme e-post er alltid samme deltaker)."""
    ut: dict[int, Funn] = {}
    for nokkel, beskrivelse in ((_normalt_navn, "samme navn"),
                                (lambda r: re.sub(r"\D", "", r["telefon"] or "")[-8:], "samme telefonnummer")):
        grupper: dict[str, list] = {}
        for r in rader:
            k = nokkel(r)
            if k and len(k) >= 3:
                grupper.setdefault(k, []).append(r)
        for gruppe in grupper.values():
            if len({r["deltaker_id"] for r in gruppe}) < 2:
                continue
            for r in gruppe:
                andre = [a for a in gruppe if a["deltaker_id"] != r["deltaker_id"]]
                if r["id"] in ut:
                    continue
                ut[r["id"]] = Funn("dublett", f"Kan være påmeldt to ganger: {beskrivelse} som "
                                   + ", ".join(f"{a['navn']} ({a['epost']})" for a in andre) + ".",
                                   ",".join(str(a["deltaker_id"]) for a in sorted(andre, key=lambda a: a["deltaker_id"])))
    return ut


# ============================ «Det stemmer» ============================

def _hash(verdi: str) -> str:
    return hashlib.sha256((verdi or "").strip().casefold().encode()).hexdigest()


def godkjente(con, deltaker_ider) -> set[tuple[int, str, str]]:
    ider = sorted({int(i) for i in deltaker_ider})
    if not ider:
        return set()
    rader = con.execute(f"SELECT deltaker_id, kode, verdi_hash FROM datakontroll_ok WHERE deltaker_id IN ({','.join('?' * len(ider))})",
                        ider).fetchall()
    return {(r["deltaker_id"], r["kode"], r["verdi_hash"]) for r in rader}


def godta(con, deltaker_id: int, kode: str, verdi: str, *, aktor: str) -> None:
    """«Det stemmer»: verdien er sjekket og skal ikke markeres igjen (før den endres). Kalleren committer."""
    if kode not in KODER:
        raise ValueError("Ukjent kontroll.")
    con.execute("""INSERT INTO datakontroll_ok (deltaker_id, kode, verdi_hash, opprettet, av) VALUES (?,?,?,?,?)
                   ON CONFLICT DO NOTHING""", (deltaker_id, kode, _hash(verdi), db.naa_utc(), aktor))
    db.logg(con, "datakontroll_ok", {"deltaker_id": deltaker_id, "kode": kode}, aktor=aktor)


def _uten_godkjente(funn: list[Funn], deltaker_id: int, ok: set) -> list[Funn]:
    return [f for f in funn if (deltaker_id, f.kode, _hash(f.verdi)) not in ok]


# ============================ for sidene ============================

_SQL = """SELECT p.id, p.kurs_id, p.status, p.deltaker_id, d.navn, d.fornavn, d.etternavn, d.epost, d.telefon, d.postnr
          FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id"""


def for_kurs(con, kurs_id: int) -> dict[int, list[Funn]]:
    """{paamelding_id: [Funn]} for påmeldingene med plass eller på venteliste på kurset (deltakerlisten)."""
    rader = con.execute(_SQL + " WHERE p.kurs_id=? AND p.status IN ('bekreftet','venteliste')", (kurs_id,)).fetchall()
    return _samle(con, rader)


def for_paamelding(con, paamelding_id: int) -> list[Funn]:
    """Funnene for én påmelding (deltakervinduet), også mulige dubletter på samme kurs."""
    p = con.execute("SELECT kurs_id FROM paamelding WHERE id=?", (paamelding_id,)).fetchone()
    if not p:
        return []
    return for_kurs(con, p["kurs_id"]).get(paamelding_id, [])


def oversikt(con) -> list[dict]:
    """Alle funn på kurs som ikke er avsluttet eller avlyst, gruppert per kurs: [{kurs, rader: [{p, funn}]}] (Oversikten)."""
    rader = con.execute(_SQL + f""" JOIN kurs k ON k.id=p.kurs_id
                         WHERE p.status IN ('bekreftet','venteliste') AND k.status IN ({','.join('?' * len(_KURSSTATUSER))})""",
                        _KURSSTATUSER).fetchall()
    per_kurs: dict[int, list] = {}
    for r in rader:
        per_kurs.setdefault(r["kurs_id"], []).append(r)
    ut = []
    for kurs_id, kursrader in per_kurs.items():
        funn = _samle(con, kursrader)
        if not funn:
            continue
        kurs = con.execute("SELECT id, kursnr, navn FROM kurs WHERE id=?", (kurs_id,)).fetchone()
        per_id = {r["id"]: r for r in kursrader}
        ut.append({"kurs": kurs, "rader": [{"p": per_id[pid], "funn": f} for pid, f in sorted(
            funn.items(), key=lambda kv: (per_id[kv[0]]["navn"] or "").casefold())]})
    return sorted(ut, key=lambda g: g["kurs"]["kursnr"] or 0)


def antall(con) -> int:
    """Antall påmeldinger med minst ett funn (kortet på Oversikten)."""
    return sum(len(g["rader"]) for g in oversikt(con))


def _samle(con, rader) -> dict[int, list[Funn]]:
    ok = godkjente(con, [r["deltaker_id"] for r in rader])
    dobbelt = dubletter(rader)
    ut = {}
    for r in rader:
        funn = sjekk_person(r) + ([dobbelt[r["id"]]] if r["id"] in dobbelt else [])
        funn = _uten_godkjente(funn, r["deltaker_id"], ok)
        if funn:
            ut[r["id"]] = funn
    return ut


def for_skjema() -> str:
    """Domenelistene til «Mente du …?» i skjemaet (static/app.js), som JSON i et data-attributt: samme regler som her."""
    return json.dumps({"vanlige": list(VANLIGE_DOMENER), "sjeldne": SJELDNE_DOMENER, "tld": TLD_FEIL}, ensure_ascii=False)
