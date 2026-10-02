"""«Min side» for ett kurs: den personlige lenken, tilgangsnivået og «Mine opplysninger» (ren logikk: ingen Flask).

Min side er siden deltakeren ser for ett kurs (admin-fanen heter også «Min side»): det felles innholdet (blokkene fra editoren), innsjekk
og opplysningene deltakeren selv ga ved påmeldingen. Tilgang (bare bekreftet påmelding, publisert og åpen side) avgjøres av
deltakerside.tilgang; denne modulen tar seg av veien inn og det personlige.

Den personlige lenken i e-postene (`/min/<lenke>`):
  * er en SIGNERT adresse (lenker.min_side_token) som regnes ut hver gang og aldri lagres. Bare tabellen min_side_lenke finnes, og den
    har en rad bare når en administrator har stengt lenken eller laget en ny (versjonen økes, så lenker i sendte e-poster slutter å virke);
  * virker til 30 dager etter siste kursdag (deretter bruker deltakeren innloggingen med e-post);
  * gir NIVÅET «lenke»: det felles innholdet, innsjekk og opplysninger om deltakeren selv, men IKKE privatadressen, fakturaer og kursbevis. Dem
    ser deltakeren først etter å ha bekreftet e-posten sin (engangslenke på e-post, nivå «epost»). Firmaets fakturaadresse vises direkte;
  * skjules i kopien i e-posthistorikken (skjul_i_kopi), så ikke alle med tilgang til historikken kan åpne deltakerens side.
Allergier og tilrettelegging, interne kommentarer og HPR-nummer vises aldri.
"""
import re
from dataclasses import dataclass
from datetime import date, timedelta

from . import config, db, lenker, privatadresse

DAGER_ETTER_KURS = 30           # lenken virker så mange dager etter siste kursdag
RESERVE_DAGER = 90              # et kurs uten kursdager: så mange dager etter påmeldingen
NIVAA_EPOST = "epost"           # innlogget med e-postlenke: alt
NIVAA_LENKE = "lenke"           # kommet inn med den personlige lenken: alt unntatt privatadresse, fakturaer og kursbevis
ANONYM = "@" + db.ANONYM_DOMENE


def _tilstand(con, paamelding_id: int) -> tuple[int, bool]:
    """(versjon, stengt) for påmeldingens lenke. Ingen rad = versjon 0 og åpen."""
    r = con.execute("SELECT versjon, stengt FROM min_side_lenke WHERE paamelding_id=?", (paamelding_id,)).fetchone()
    return (int(r["versjon"]), bool(r["stengt"])) if r else (0, False)


def url(con, paamelding_id: int) -> str | None:
    """Den personlige lenken til Min side for påmeldingen (hel adresse), eller None når en administrator har stengt den."""
    versjon, stengt = _tilstand(con, paamelding_id)
    return None if stengt else f"{config.BASE_URL}/min/{lenker.min_side_token(paamelding_id, versjon)}"


def er_stengt(con, paamelding_id: int) -> bool:
    return _tilstand(con, paamelding_id)[1]


def utloper(con, paamelding_id: int) -> date | None:
    """Siste dag lenken virker: siste kursdag + 30 dager (uten kursdager: 90 dager etter påmeldingen). None = påmeldingen finnes ikke."""
    r = con.execute(
        """SELECT p.opprettet, (SELECT MAX(kd.dato) FROM kursdag kd WHERE kd.kurs_id=p.kurs_id) AS siste
           FROM paamelding p WHERE p.id=?""", (paamelding_id,)).fetchone()
    if not r:
        return None
    if r["siste"]:
        return date.fromisoformat(str(r["siste"])[:10]) + timedelta(days=DAGER_ETTER_KURS)
    return date.fromisoformat(str(r["opprettet"])[:10]) + timedelta(days=RESERVE_DAGER)


def utloper_forklaring(con, paamelding_id: int) -> str:
    """Hvorfor lenken utløper når den gjør: «30 dager etter siste kursdag», eller «90 dager etter påmeldingen» for et kurs uten kursdager."""
    r = con.execute("SELECT 1 FROM kursdag kd JOIN paamelding p ON p.kurs_id=kd.kurs_id WHERE p.id=? LIMIT 1", (paamelding_id,)).fetchone()
    return f"{DAGER_ETTER_KURS} dager etter siste kursdag" if r else f"{RESERVE_DAGER} dager etter påmeldingen"


@dataclass(frozen=True)
class Oppslag:
    ok: bool
    arsak: str                  # "ok" | "ugyldig" | "stengt" | "utlopt" | "ikke_funnet" (siden viser den samme teksten for alle)
    deltaker_id: int | None = None
    kurs_kode: str | None = None
    paamelding_id: int | None = None
    versjon: int | None = None


def slaa_opp(con, token, idag: date) -> Oppslag:
    """Sjekker lenken når den åpnes: riktig signert, og så alt i `kontroller`."""
    lest = lenker.les_min_side_token(token)
    if lest is None:
        return Oppslag(False, "ugyldig")
    return kontroller(con, lest[0], lest[1], idag)


def kontroller(con, pid: int, versjon: int, idag: date) -> Oppslag:
    """Gjelder lenken (påmelding + versjon) ennå? Påmeldingen finnes og har plass, deltakeren er ikke anonymisert, lenken er ikke stengt eller
    byttet ut, og den er ikke utløpt. Brukes når lenken åpnes (`slaa_opp`) og ved HVER forespørsel i en økt som kom inn med lenken
    (kurs/web/app.py, `_avslutt_ugyldig_lenkeokt`), så «Steng lenken» og «Lag ny lenke» virker straks også for økter som alt er åpnet.
    Selve tilgangen til siden (publisert, åpen) sjekkes av deltakerside.tilgang etterpå."""
    r = con.execute(
        """SELECT p.deltaker_id, p.status, k.kode, d.epost FROM paamelding p
           JOIN kurs k ON k.id=p.kurs_id JOIN deltaker d ON d.id=p.deltaker_id WHERE p.id=?""", (pid,)).fetchone()
    if not r or r["status"] != "bekreftet" or str(r["epost"]).lower().endswith(ANONYM):
        return Oppslag(False, "ikke_funnet")
    gjeldende, stengt = _tilstand(con, pid)
    if stengt or versjon != gjeldende:
        return Oppslag(False, "stengt")
    slutt = utloper(con, pid)
    if slutt is not None and idag > slutt:
        return Oppslag(False, "utlopt")
    return Oppslag(True, "ok", r["deltaker_id"], r["kode"], pid, versjon)


def _lagre(con, paamelding_id: int, versjon: int, stengt: int, aktor: str) -> None:
    con.execute(
        """INSERT INTO min_side_lenke (paamelding_id, versjon, stengt, endret, endret_av) VALUES (?,?,?,?,?)
           ON CONFLICT (paamelding_id) DO UPDATE SET versjon=excluded.versjon, stengt=excluded.stengt,
                                                     endret=excluded.endret, endret_av=excluded.endret_av""",
        (paamelding_id, versjon, stengt, db.naa_utc(), aktor))


def steng(con, paamelding_id: int, aktor: str) -> None:
    """Stenger lenken: ingen lenke til Min side virker for påmeldingen før administrator lager en ny. Kalleren committer."""
    versjon, _ = _tilstand(con, paamelding_id)
    _lagre(con, paamelding_id, versjon, 1, aktor)
    db.logg(con, "min_side_lenke_stengt", {"paamelding_id": paamelding_id}, aktor=aktor)


def ny_lenke(con, paamelding_id: int, aktor: str) -> None:
    """Lager en ny lenke: versjonen økes, så alle eldre lenker (også i e-poster som er sendt) slutter å virke. Kalleren committer."""
    versjon, _ = _tilstand(con, paamelding_id)
    _lagre(con, paamelding_id, versjon + 1, 0, aktor)
    db.logg(con, "min_side_lenke_fornyet", {"paamelding_id": paamelding_id}, aktor=aktor)


def roter_for_deltaker(con, deltaker_id: int, aktor: str, grunn: str) -> int:
    """Øker versjonen på alle lenkene til personen: lenker som alt er sendt (kanskje til en feilskrevet e-postadresse som nå er rettet) slutter å
    virke. En lenke en administrator har stengt, forblir stengt. Kalleren committer. Returnerer antall lenker."""
    pider = [r[0] for r in con.execute("SELECT id FROM paamelding WHERE deltaker_id=?", (deltaker_id,))]
    for pid in pider:
        versjon, stengt = _tilstand(con, pid)
        _lagre(con, pid, versjon + 1, 1 if stengt else 0, aktor)
        db.logg(con, "min_side_lenke_fornyet", {"paamelding_id": pid, "grunn": grunn}, aktor=aktor)
    return len(pider)


def skjul_i_kopi(html: str) -> str:
    """E-posthistorikken lagrer en kopi av hver e-post, men aldri personlige tilgangslenker: lenken til Min side skjules i kopien (resten
    av e-posten er lik). Den som ble sendt, er uendret."""
    monster = re.escape(config.BASE_URL) + r"/min/[0-9]+\.[0-9]+\.[0-9a-f]{32}"
    return re.sub(monster, f"{config.BASE_URL}/min/skjult", html)


# ============================ «Mine opplysninger» ============================

def _dato(iso) -> str:
    iso = str(iso or "")[:10]
    return f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}" if len(iso) == 10 else ""


def _adresselinjer(adresse, postnr, sted) -> list[str]:
    linjer = [str(adresse).strip()] if adresse and str(adresse).strip() else []
    ost = " ".join(x for x in (str(postnr or "").strip(), str(sted or "").strip()) if x)
    return linjer + ([ost] if ost else [])


def mine_opplysninger(con, deltaker_id: int, kurs_id: int, *, full: bool) -> dict | None:
    """Opplysningene deltakeren selv ga ved påmeldingen, for akkurat denne påmeldingen: {"rader": [(etikett, [linjer])], "bekreft": bool,
    "full": bool}. `full` = innlogget med e-post: da vises også privatadressen. Uten det står bare at den er registrert (`bekreft`).
    Aldri allergier og tilrettelegging, interne kommentarer, HPR-nummer, rabattkode eller svar på kursets egne spørsmål."""
    r = con.execute(
        """SELECT d.navn, d.epost, d.telefon, d.arbeidssted, d.yrkestittel, d.adresse, d.postnr, d.poststed,
                  p.id AS paamelding_id, p.opprettet, p.betaler, p.org_navn, p.org_nr, p.faktura_epost, p.faktura_adresse,
                  p.faktura_postnr, p.faktura_sted, p.faktura_ref, p.ehf
           FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE p.deltaker_id=? AND p.kurs_id=?""",
        (deltaker_id, kurs_id)).fetchone()
    if not r:
        return None
    rader: list[tuple[str, list[str]]] = [("Navn", [r["navn"]]), ("E-post", [r["epost"]])]
    for etikett, verdi in (("Telefon", r["telefon"]), ("Arbeidssted", r["arbeidssted"]), ("Yrkestittel", r["yrkestittel"])):
        if verdi and str(verdi).strip():
            rader.append((etikett, [str(verdi).strip()]))
    rader.append(("Påmelding", [f"Nr. {r['paamelding_id']} · registrert {_dato(r['opprettet'])}"]))
    bekreft = False
    if r["betaler"] == "organisasjon":
        firma = [str(r["org_navn"]).strip()] if r["org_navn"] and str(r["org_navn"]).strip() else []
        if r["org_nr"] and str(r["org_nr"]).strip():
            firma.append(f"Org.nr. {str(r['org_nr']).strip()}")
        rader.append(("Betaler", firma or ["Firma"]))
        adresse = _adresselinjer(r["faktura_adresse"], r["faktura_postnr"], r["faktura_sted"])
        if adresse:
            rader.append(("Fakturaadresse", adresse))
    else:
        rader.append(("Betaler", ["Deg selv"]))
        privat = privatadresse.velg_adresse([r["faktura_adresse"], r["faktura_postnr"], r["faktura_sted"]],
                                            [r["adresse"], r["postnr"], r["poststed"]])
        if privat is None:
            rader.append(("Fakturaadresse", ["Ikke registrert"]))
        elif full:
            rader.append(("Fakturaadresse", _adresselinjer(*privat)))
        else:
            rader.append(("Fakturaadresse", ["Registrert. Vises når du har bekreftet e-posten din."]))
            bekreft = True
    if r["faktura_epost"] and str(r["faktura_epost"]).strip():
        rader.append(("Faktura sendes til", [str(r["faktura_epost"]).strip()]))
    if r["faktura_ref"] and str(r["faktura_ref"]).strip():
        rader.append(("Referanse", [str(r["faktura_ref"]).strip()]))
    if r["ehf"]:
        rader.append(("EHF-faktura", ["Ja"]))
    return {"rader": rader, "bekreft": bekreft, "full": full, "eksempel": False, "kontakt": config.AVSENDER_EPOST}


def eksempel_opplysninger() -> dict:
    """Til forhåndsvisningen i administrasjonen: oppdiktede opplysninger, slik kortet ser ut for en deltaker. Aldri ekte data."""
    return {"rader": [("Navn", ["Kari Eksempel"]), ("E-post", ["kari@example.no"]), ("Telefon", ["900 00 000"]),
                      ("Arbeidssted", ["Eksempelklinikken"]), ("Påmelding", ["Nr. 123 · registrert 01.01.2027"]),
                      ("Betaler", ["Deg selv"]), ("Fakturaadresse", ["Eksempelveien 1", "0123 Oslo"])],
            "bekreft": False, "full": True, "eksempel": True, "kontakt": config.AVSENDER_EPOST}
