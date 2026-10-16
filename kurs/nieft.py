"""NIEFT-medlemslisten (Camilla 15.10.2026, migrering 36, tabellen nieft_medlem).

Camilla: «når de prøver å melde seg på med NIEFT-pris, så må de registrere seg med e-posten som de er NIEFT-medlem med. Det må
stå et sted. Og hvis de ikke bruker NIEFT-medlemmailen … så skal det komme opp en melding som stopper dem.» og «NIEFT-prisen skal
godkjennes automatisk når e-posten er på listen.»

- Listen er bare e-postadressene (små bokstaver). Kursadministrasjonen laster den opp (Excel eller CSV: alle e-postadresser i
  filen leses, uansett kolonne) under Rabatter → NIEFT-medlemmer, og den nye listen erstatter den gamle. Enkeltadresser kan legges
  til og fjernes.
- Er listen tom (før den er lastet opp), er alt som før: NIEFT-prisen kan velges, venter på godkjenning og sjekkes for hånd.
- Med en liste: velger noen NIEFT-pris i påmeldingsskjemaet (også per deltaker i bedriftspåmeldingen) med en e-post som ikke står
  på listen, stoppes påmeldingen med en melding (`stopptekst`). Står e-posten på listen, er NIEFT-prisen godkjent med en gang.
- En administrator som melder på eller endrer prisen, stoppes aldri (prisen er avgjort av administrator, som før).
- Listen er ekte persondata: den skal aldri i demo, tester eller git, og e-postadressene logges aldri (bare antall).
"""
import html
import io
import re
import zipfile
import zlib
from dataclasses import dataclass

from . import db

MAKS_MB = 5                     # filen med listen
MAKS_ADRESSER = 20000
_MAKS_UTPAKKET = 30 * 1024 * 1024   # Excel-filen pakket ut (vern mot «zip-bomber»: telles mens den pakkes ut)
ENDELSER = ("xlsx", "csv", "txt")
MAKS_LENGDE = 254               # lengste lovlige e-postadresse
_EPOST = re.compile(r"[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,24}")
# Skilletegn mellom tekstbitene i filen: en e-postadresse har aldri mellomrom, ; , < > " ( ) [ ] { } | : = / eller \
_SKILLE = re.compile(r"[\s;,<>\"()\[\]{}|:=/\\]+")


@dataclass
class Lesing:
    adresser: list[str]         # små bokstaver, uten dubletter, sortert
    avvist: int                 # biter med @ som ikke er en gyldig adresse (f.eks. med æ, ø eller å): ikke tatt med


class Nieftfeil(Exception):
    """Feil som kan vises til administrator som de er."""


def normaliser(epost) -> str:
    return (epost or "").strip().lower()


def har_liste(con) -> bool:
    return con.execute("SELECT 1 FROM nieft_medlem LIMIT 1").fetchone() is not None


def antall(con) -> int:
    return con.execute("SELECT COUNT(*) FROM nieft_medlem").fetchone()[0]


def er_medlem(con, epost) -> bool:
    e = normaliser(epost)
    return bool(e) and con.execute("SELECT 1 FROM nieft_medlem WHERE epost=?", (e,)).fetchone() is not None


def stoppes(con, kategori, epost) -> bool:
    """Om en påmelding med denne prisen og e-posten skal stoppes: NIEFT-pris, en liste finnes, og e-posten står ikke på den."""
    return kategori == "nieft" and har_liste(con) and not er_medlem(con, epost)


def stopptekst(epost, kontakt: str, *, deltaker: str = "") -> str:
    """Meldingen som stopper påmeldingen. `deltaker` = «Deltaker 2: » i bedriftspåmeldingen."""
    hvem = "e-posten deltakeren er" if deltaker else "e-posten du er"
    return (f"{deltaker}Vi finner ikke {normaliser(epost)} blant NIEFT-medlemmene. NIEFT-pris krever {hvem} registrert med som "
            f"NIEFT-medlem: bytt til den e-posten, eller velg ordinær pris. Vet du ikke hvilken e-post medlemskapet er registrert med, "
            f"kontakt oss på {kontakt}.")


# ============================ filen med listen ============================

def les_fil(filnavn: str, data: bytes) -> Lesing:
    """Alle e-postadresser i filen (Excel .xlsx, eller .csv/.txt). Kolonnene spiller ingen rolle: filen deles opp på mellomrom og
    skilletegn, og hver bit med @ som er en hel, gyldig adresse tas med. En bit med @ som ikke er det (f.eks. «åse@eksempel.no»),
    klippes aldri til en annen adresse: den telles i `avvist` og vises som et antall."""
    endelse = filnavn.rsplit(".", 1)[-1].lower() if "." in (filnavn or "") else ""
    if endelse not in ENDELSER:
        raise Nieftfeil("Listen må være en Excel-fil (.xlsx) eller en CSV-fil.")
    if not data:
        raise Nieftfeil("Filen er tom.")
    if len(data) > MAKS_MB * 1024 * 1024:
        raise Nieftfeil(f"Filen er for stor (maks {MAKS_MB} MB).")
    tekst = _tekst_fra_excel(data) if endelse == "xlsx" else _tekst_fra_csv(data)
    adresser, avvist = set(), set()
    for bit in _SKILLE.split(tekst):
        if "@" not in bit:
            continue
        e = normaliser(bit.strip(".'"))
        # lengden først: regexen kjøres bare på biter som kan være en adresse
        (adresser if len(e) <= MAKS_LENGDE and _EPOST.fullmatch(e) else avvist).add(e)
    if not adresser:
        raise Nieftfeil("Fant ingen e-postadresser i filen.")
    if len(adresser) > MAKS_ADRESSER:
        raise Nieftfeil(f"Filen har flere enn {MAKS_ADRESSER} e-postadresser. Er det riktig fil?")
    return Lesing(sorted(adresser), len(avvist))


def _tekst_fra_excel(data: bytes) -> str:
    """Teksten i cellene (delte tekster og tekst i arkene). Ingen formler eller makroer kjøres: XML-en leses bare som tekst. Hver del
    pakkes ut med en øvre grense (størrelsen filen selv oppgir, kan være feil)."""
    if not data.startswith(b"PK\x03\x04"):
        raise Nieftfeil("Filen kunne ikke leses som en Excel-fil (.xlsx).")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            deler = [i for i in z.infolist()
                     if i.filename == "xl/sharedStrings.xml" or re.fullmatch(r"xl/worksheets/[^/]+\.xml", i.filename)]
            if not deler:
                raise Nieftfeil("Filen kunne ikke leses som en Excel-fil (.xlsx).")
            if sum(i.file_size for i in deler) > _MAKS_UTPAKKET:
                raise Nieftfeil("Excel-filen er for stor pakket ut.")
            rest, tekster = _MAKS_UTPAKKET, []
            for i in deler:
                with z.open(i) as fil:
                    innhold = fil.read(rest + 1)
                if len(innhold) > rest:
                    raise Nieftfeil("Excel-filen er for stor pakket ut.")
                rest -= len(innhold)
                tekster.append(_xml_tekst(innhold.decode("utf-8", "replace")))
            return "\n".join(tekster)
    except (zipfile.BadZipFile, RuntimeError, ValueError, NotImplementedError, EOFError, zlib.error) as e:
        raise Nieftfeil("Filen kunne ikke leses som en Excel-fil (.xlsx). Er den passordbeskyttet?") from e   # RuntimeError: passord


def _xml_tekst(xml: str) -> str:
    """Teksten i XML-en: hver celle (og hver delt tekst) på egen linje, mens formaterte biter av samme tekst (<r>) henger sammen -
    en e-post der bare en del er i fet skrift, blir hel. Formler (<f>, f.eks. =B2) tas bort: bare verdien i cellen (<v>) leses."""
    xml = re.sub(r"<f(?:\s[^>]*)?/>|<f(?:\s[^>]*)?>.*?</f>", "", xml, flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"</(?:si|c|row|is|v)>", "\n", xml)))


def _tekst_fra_csv(data: bytes) -> str:
    for koding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(koding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


# ============================ endringer ============================

def erstatt(con, adresser: list[str], *, aktor: str) -> dict:
    """Den nye listen erstatter den gamle. Bare antallene logges, aldri adressene. Kalleren committer.
    Returnerer {"antall": n, "nye": a, "fjernet": b}."""
    nye_liste = {normaliser(e) for e in adresser if normaliser(e)}
    gamle = {r[0] for r in con.execute("SELECT epost FROM nieft_medlem")}
    fjernes, legges_til = gamle - nye_liste, nye_liste - gamle
    na = db.naa_utc()
    if fjernes:
        con.executemany("DELETE FROM nieft_medlem WHERE epost=?", [(e,) for e in sorted(fjernes)])
    if legges_til:
        con.executemany("INSERT INTO nieft_medlem (epost, opprettet, av) VALUES (?,?,?)", [(e, na, aktor) for e in sorted(legges_til)])
    resultat = {"antall": len(nye_liste), "nye": len(legges_til), "fjernet": len(fjernes)}
    db.logg(con, "nieft_liste_lastet_opp", resultat, aktor=aktor)
    return resultat


def legg_til(con, epost, *, aktor: str) -> bool:
    """Én adresse. False når den alt står på listen. Kalleren committer."""
    e = normaliser(epost)
    if len(e) > MAKS_LENGDE or not _EPOST.fullmatch(e):
        raise Nieftfeil("Skriv en gyldig e-postadresse (uten æ, ø og å).")
    if er_medlem(con, e):
        return False
    con.execute("INSERT INTO nieft_medlem (epost, opprettet, av) VALUES (?,?,?)", (e, db.naa_utc(), aktor))
    db.logg(con, "nieft_medlem_lagt_til", {"antall": antall(con)}, aktor=aktor)
    return True


def fjern(con, epost, *, aktor: str) -> bool:
    """False når adressen ikke står på listen. Kalleren committer."""
    cur = con.execute("DELETE FROM nieft_medlem WHERE epost=?", (normaliser(epost),))
    if cur.rowcount != 1:
        return False
    db.logg(con, "nieft_medlem_fjernet", {"antall": antall(con)}, aktor=aktor)
    return True


def sok(con, tekst, *, grense: int = 50) -> list[str]:
    """Adressene som inneholder teksten (minst 3 tegn), høyst `grense`. Hele listen vises aldri på én gang."""
    t = normaliser(tekst)
    if len(t) < 3:
        return []
    # % og _ er jokertegn i LIKE (og _ er vanlig i e-postadresser): LIKE grovsorterer, og treffene sjekkes ordrett her
    treff = (r[0] for r in con.execute("SELECT epost FROM nieft_medlem WHERE epost LIKE ? ORDER BY epost", (f"%{t}%",)))
    return [e for e in treff if t in e][:grense]


# ============================ rabatter som venter ============================

def ventende_paa_listen(con) -> list[dict]:
    """NIEFT-rabatter som venter på godkjenning, der deltakerens e-post står på listen: [{paamelding_id, kurs_id, status}]. Samme
    utvalg som Rabatter som venter (rabatter.ventende): med plass eller på venteliste, på kurs som ikke er avlyst."""
    return [dict(r) for r in con.execute(
        """SELECT p.id AS paamelding_id, p.kurs_id, p.status FROM paamelding p
           JOIN deltaker d ON d.id = p.deltaker_id
           JOIN kurs k ON k.id = p.kurs_id
           JOIN nieft_medlem n ON n.epost = lower(d.epost)
           WHERE p.priskategori = 'nieft' AND p.rabatt_status = 'venter' AND p.status IN ('bekreftet','venteliste')
             AND k.status != 'avlyst'
           ORDER BY p.id""")]


def paa_listen(con, paamelding_ider) -> set[int]:
    """Hvilke av påmeldingene som har en deltaker-e-post på listen (merket i Rabatter som venter)."""
    ider = [int(i) for i in paamelding_ider]
    if not ider:
        return set()
    return {r[0] for r in con.execute(
        f"""SELECT p.id FROM paamelding p JOIN deltaker d ON d.id = p.deltaker_id JOIN nieft_medlem n ON n.epost = lower(d.epost)
            WHERE p.id IN ({','.join('?' * len(ider))})""", ider)}
