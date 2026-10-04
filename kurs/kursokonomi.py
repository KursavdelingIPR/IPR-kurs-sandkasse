"""Kursoversikten øverst i Rapporter (Camilla 04.10.2026): antall påmeldte og inntekter per kurs og til sammen.

Steg 1: forventet inntekt = kursets pris × påmeldte (fullverdige), og fakturert = fakturaer laget i systemet. Senere
kommer priskategorier (ordinær, NIEFT, student …) og utgifter (honorar, lokaler, lunsj …) per kurs.
Krediterte og feilede fakturaer teller ikke i «Fakturert» (som i økonomirapporten, okonomi.TELLER_IKKE).
Ekstradeltakere teller ikke som påmeldte og er ikke med i forventet inntekt (de betaler ofte for et utvalg samlinger);
det de faktisk er fakturert for, er med i «Fakturert».
"""
from dataclasses import dataclass

from . import okonomi

# Kurs i disse statusene er ikke med (utkast er ikke satt opp ennå)
UTELATT = ("utkast",)


@dataclass
class Totalt:
    kurs: int = 0
    paameldte: int = 0
    ekstra: int = 0
    venteliste: int = 0
    forventet: int = 0
    fakturert: int = 0


def kursoversikt(con, fra: str = "", til: str = "") -> tuple[list[dict], Totalt]:
    """Én rad per kurs (eldste først) innenfor perioden (kursdatoene, som resten av Rapporter), og summen av radene.
    Avlyste kurs vises, men teller ikke med i forventet inntekt."""
    rader = con.execute(
        f"""SELECT k.id, k.kursnr, k.navn, k.status, k.kapasitet, k.pris_nok, k.fakturering,
                   MIN(kd.dato) AS start, MAX(kd.dato) AS slutt,
                   (SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=k.id AND p.status='bekreftet'
                       AND p.ekstradeltaker_ts IS NULL) AS paameldte,
                   (SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=k.id AND p.status='bekreftet'
                       AND p.ekstradeltaker_ts IS NOT NULL) AS ekstra,
                   (SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=k.id AND p.status='venteliste') AS venteliste,
                   (SELECT COALESCE(SUM(f.belop_nok),0) FROM faktura f JOIN paamelding p ON p.id=f.paamelding_id
                       WHERE p.kurs_id=k.id AND f.status NOT IN ({",".join("?" * len(okonomi.TELLER_IKKE))})) AS fakturert
            FROM kurs k LEFT JOIN kursdag kd ON kd.kurs_id=k.id
            WHERE k.status NOT IN ({",".join("?" * len(UTELATT))})
            GROUP BY k.id
            HAVING (? = '' OR MAX(kd.dato) >= ?) AND (? = '' OR MIN(kd.dato) <= ?)
            ORDER BY start, k.kursnr""", (*okonomi.TELLER_IKKE, *UTELATT, fra, fra, til, til)).fetchall()
    ut, t = [], Totalt()
    for r in rader:
        forventet = 0 if r["status"] == "avlyst" else (r["pris_nok"] or 0) * r["paameldte"]
        ut.append({**dict(r), "forventet": forventet})
        t.kurs += 1
        t.paameldte += r["paameldte"]
        t.ekstra += r["ekstra"]
        t.venteliste += r["venteliste"]
        t.forventet += forventet
        t.fakturert += r["fakturert"]
    return ut, t
