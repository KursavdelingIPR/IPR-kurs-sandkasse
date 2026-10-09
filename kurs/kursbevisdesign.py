"""Kursbevis-design (Camilla 06.10.2026: «Hvordan kan dette tilpasses slik at vi kan velge farger osv på kursbevisene?»).

Et design lages én gang og velges per kurs under fanen Kursbevis: farge (rammen og overskriften), logo øverst, en illustrasjon
på hver side av overskriften (stolene for EFT, strutsene for EFST, som i Word-malene deres), skrifttype, overskrift og
signatur (bilde fra signaturbiblioteket, navn og tittel). Teksten i midten er den samme som før: standardteksten, eller
kursets egen tekst fra redigereren.

Kurs uten design får kursbeviset som før. Bildene bakes inn i kursbeviset (data:-adresser), så det er komplett i seg selv
og kan vises på Min side og lagres som PDF. Kursbevis som alt er utstedt, endres aldri.
"""
import base64
import re
from functools import lru_cache
from pathlib import Path

from . import db, signaturer

STATIC = Path(__file__).resolve().parent / "web" / "static"

# Logoene øverst (nøkkel -> (navn, fil i static/))
LOGOER = {
    "nieft": ("NIEFT", "logo/nieft.png"),
    "ipr": ("IPR", "logo/ipr.png"),
    "terapiakademiet": ("Terapiakademiet", "logo/terapiakademiet.png"),
    "": ("Ingen logo", None),
}
# Illustrasjonene ved overskriften. To filer = én på hver side (vender mot hverandre); én fil = et bilde med begge figurene
# og plass til overskriften i midten (strutsene i EFST-malen).
ILLUSTRASJONER = {
    "stoler": ("To stoler (EFT)", ("kursbevis/stol-venstre.png", "kursbevis/stol-hoyre.png")),
    "strutser": ("To strutser (EFST)", ("kursbevis/strutser.png",)),
    "": ("Ingen illustrasjon", ()),
}
SKRIFTER = {
    "serif": ("Klassisk (Georgia / Cambria)", 'Georgia, Cambria, "Times New Roman", serif'),
    "sans": ("Moderne (Calibri / Segoe UI)", 'Calibri, "Segoe UI", Arial, sans-serif'),
}
_FARGE = re.compile(r"#[0-9a-fA-F]{6}")
NAVN_MAKS = 60
TEKST_MAKS = 120

# Designene fra de gamle kursbevisene i Kurs Adm (Word-malene): lages første gang listen vises, kan endres etterpå
STANDARD = [
    {"navn": "EFT", "tittel": "Kursbevis", "farge": "#2f9e44", "logo": "nieft", "illustrasjon": "stoler", "skrift": "serif"},
    {"navn": "EFST", "tittel": "Kursbevis", "farge": "#33358a", "logo": "nieft", "illustrasjon": "strutser", "skrift": "serif"},
    {"navn": "IPR", "tittel": "Kursbevis", "farge": "#1f5c73", "logo": "ipr", "illustrasjon": "", "skrift": "serif"},
    {"navn": "Terapiakademiet", "tittel": "Kursbevis", "farge": "#562a3e", "logo": "terapiakademiet", "illustrasjon": "",
     "skrift": "sans"},
]
STANDARD_SIGNATUR_TITTEL = "Daglig leder, IPR"


class Designfeil(ValueError):
    """Designet kan ikke lagres. Meldingen er norsk og trygg å vise."""


def sikre_standard(con) -> None:
    """Lager standarddesignene hvis det ikke finnes noen designer ennå (første gang). Kalleren committer."""
    if con.execute("SELECT 1 FROM kursbevis_design").fetchone():
        return
    for d in STANDARD:
        db.sett_inn(con, """INSERT INTO kursbevis_design (navn, tittel, farge, logo, illustrasjon, skrift, signatur_tittel, opprettet)
                            VALUES (?,?,?,?,?,?,?,?)""",
                    (d["navn"], d["tittel"], d["farge"], d["logo"], d["illustrasjon"], d["skrift"], STANDARD_SIGNATUR_TITTEL,
                     db.naa_utc()))


def alle(con) -> list:
    sikre_standard(con)
    return con.execute("SELECT * FROM kursbevis_design ORDER BY navn").fetchall()


def hent(con, design_id):
    if not design_id:
        return None
    return con.execute("SELECT * FROM kursbevis_design WHERE id=?", (design_id,)).fetchone()


def kurs_som_bruker(con, design_id: int) -> list:
    return con.execute("SELECT id, kursnr, navn FROM kurs WHERE kursbevis_design_id=? ORDER BY kursnr DESC", (design_id,)).fetchall()


def lagre(con, design_id: int | None, felt: dict, *, aktor: str) -> int:
    """Oppretter (design_id None) eller endrer et design. Alle verdier kontrolleres; returnerer id-en."""
    navn = (felt.get("navn") or "").strip()
    if not navn:
        raise Designfeil("Gi designet et navn, for eksempel «EFT».")
    if len(navn) > NAVN_MAKS:
        raise Designfeil(f"Navnet kan være maks {NAVN_MAKS} tegn.")
    finnes = con.execute("SELECT id FROM kursbevis_design WHERE LOWER(navn)=LOWER(?)", (navn,)).fetchone()
    if finnes and finnes["id"] != design_id:
        raise Designfeil(f"Det finnes allerede et design som heter «{navn}».")
    tittel = (felt.get("tittel") or "").strip() or "Kursbevis"
    farge = (felt.get("farge") or "").strip()
    if not _FARGE.fullmatch(farge):
        raise Designfeil("Velg en farge.")
    logo, illustrasjon, skrift = felt.get("logo") or "", felt.get("illustrasjon") or "", felt.get("skrift") or "serif"
    if logo not in LOGOER or illustrasjon not in ILLUSTRASJONER or skrift not in SKRIFTER:
        raise Designfeil("Ugyldig valg av logo, illustrasjon eller skrift.")
    signatur = felt.get("signatur_bilde_id")
    signatur = int(signatur) if str(signatur or "").isdigit() else None
    if signatur is not None and not signaturer.bilde(con, signatur):
        raise Designfeil("Signaturbildet finnes ikke lenger. Velg et annet.")
    sig_navn = (felt.get("signatur_navn") or "").strip()
    sig_tittel = (felt.get("signatur_tittel") or "").strip()
    for tekst, hva in ((tittel, "Overskriften"), (sig_navn, "Navnet under signaturen"), (sig_tittel, "Tittelen under signaturen")):
        if len(tekst) > TEKST_MAKS:
            raise Designfeil(f"{hva} kan være maks {TEKST_MAKS} tegn.")
    verdier = (navn, tittel, farge.lower(), logo, illustrasjon, skrift, signatur, sig_navn or None, sig_tittel or None)
    if design_id is None:
        design_id = db.sett_inn(con, """INSERT INTO kursbevis_design (navn, tittel, farge, logo, illustrasjon, skrift, signatur_bilde_id,
                                        signatur_navn, signatur_tittel, opprettet) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                                (*verdier, db.naa_utc()))
        db.logg(con, "kursbevisdesign_opprettet", {"design_id": design_id}, aktor=aktor)
        return design_id
    if not con.execute("""UPDATE kursbevis_design SET navn=?, tittel=?, farge=?, logo=?, illustrasjon=?, skrift=?, signatur_bilde_id=?,
                          signatur_navn=?, signatur_tittel=?, endret=? WHERE id=?""", (*verdier, db.naa_utc(), design_id)).rowcount:
        raise Designfeil("Fant ikke designet.")
    db.logg(con, "kursbevisdesign_endret", {"design_id": design_id}, aktor=aktor)
    return design_id


def velg_for_kurs(con, kurs_id: int, design_id: int | None, *, aktor: str) -> None:
    """Kursets design (None = som før, uten design)."""
    if design_id is not None and not hent(con, design_id):
        raise Designfeil("Fant ikke designet.")
    con.execute("UPDATE kurs SET kursbevis_design_id=? WHERE id=?", (design_id, kurs_id))
    db.logg(con, "kursbevis_design_valgt", {"kurs_id": kurs_id, "design_id": design_id}, aktor=aktor)


@lru_cache(maxsize=16)
def _data_url(fil: str) -> str:
    data = (STATIC / fil).read_bytes()
    return f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"


def for_kursbevis(con, kurs) -> dict | None:
    """Det kursbeviset trenger for kursets design, med bildene som data:-adresser - eller None (kurset har ikke design)."""
    try:
        design_id = kurs["kursbevis_design_id"]
    except (KeyError, IndexError):
        return None
    d = hent(con, design_id)
    return til_visning(con, d) if d else None


def til_visning(con, d) -> dict:
    logo_fil = LOGOER.get(d["logo"] or "", (None, None))[1]
    filer = ILLUSTRASJONER.get(d["illustrasjon"] or "", (None, ()))[1]
    signatur = signaturer.bilde(con, d["signatur_bilde_id"]) if d["signatur_bilde_id"] else None
    return {
        "farge": d["farge"],
        "tittel": d["tittel"] or "Kursbevis",
        "skrift": SKRIFTER.get(d["skrift"], SKRIFTER["serif"])[1],
        "logo": _data_url(logo_fil) if logo_fil else None,
        "logo_alt": LOGOER.get(d["logo"] or "", ("",))[0],
        "illustrasjon": [_data_url(f) for f in filer],
        "signatur": f"data:{signatur[0]};base64,{base64.b64encode(signatur[1]).decode('ascii')}" if signatur else None,
        "signatur_navn": d["signatur_navn"] or "",
        "signatur_tittel": d["signatur_tittel"] or "",
    }
