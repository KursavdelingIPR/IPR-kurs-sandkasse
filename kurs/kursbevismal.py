"""Kursbeviset per kurs (fanen Kursbevis, Camilla 04.10.2026: «Her skal vi kunne redigere … kursbeviset som sendes ut på hvert
av kursene. Vi skal kunne endre og legge ting til», «ferdige rammer, men i ulike farger»).

Kurs uten egen versjon (kurs.kursbevis_html IS NULL) får standardbeviset (maler/kursbevis.html) som før. En egen versjon er
renset HTML fra redigereren (samme trygge rensing som signaturene, kurs/signaturer.py) med flettefelt som {navn} og
{kursnavn}; de fylles ut for hver deltaker når kursbeviset utstedes. Bilder fra biblioteket bakes inn i selve kursbeviset
(data:-adresser), så deltakerne ser dem på Min side uten å være innlogget som administrator.
Kursbevis som alt er utstedt, endres aldri.
"""
import base64
import html as html_
import re

from . import db, signaturer

# Rammene: navn og farge (None = uten ramme). Rammen er en dobbel linje rundt hele beviset.
RAMMER = {
    "blaa": ("Blå (IPR)", "#1f5c73"),
    "oransje": ("Oransje (NIEFT)", "#e8572a"),
    "rod": ("Rød", "#b3261e"),
    "gronn": ("Grønn", "#2e7d4f"),
    "lilla": ("Lilla", "#6b4c9a"),
    "gull": ("Gull", "#b08d2e"),
    "gra": ("Grå", "#667788"),
    "ingen": ("Uten ramme", None),
}
STANDARD_RAMME = "blaa"
# Tekststørrelsene i verktøylinjen: som i signaturene, pluss en overskrift (rensingen gjør 6 om til xx-large)
STORRELSER = {**signaturer.STORRELSER, "Overskrift": "6"}

# Flettefeltene som kan klikkes inn i redigereren (navn -> forklaring)
KODER = {
    "navn": "Deltakerens fulle navn",
    "fornavn": "Deltakerens fornavn",
    "kursnavn": "Kursets navn",
    "kursnr": "Kursnummeret",
    "deltakelse": "«har gjennomført», eller «har deltatt på samling 1 og 3 i» for den som bare var på noen samlinger",
    "kursdager": "Dagene deltakeren møtte",
    "timer": "Antall timer",
    "sted": "Kursstedet",
    "dato": "Datoen kursbeviset utstedes",
    "detaljer": "Tabell med samlinger, kursdager, timer, sted og eventuelt timer i spesialistløpet",
}
_KODE = re.compile(r"\{([a-zæøå_]+)\}")

# Utgangspunktet i redigereren: samme innhold som standardbeviset, med flettefelt
STANDARD_HTML = (
    '<div style="text-align: center"><span style="color: #1f5f7a; font-size: small">INSTITUTT FOR PSYKOLOGISK RÅDGIVNING</span></div>'
    '<div style="text-align: center"><br></div>'
    '<div style="text-align: center"><span style="font-size: xx-large">Kursbevis</span></div>'
    '<div style="text-align: center"><br></div>'
    '<div style="text-align: center">Dette bekrefter at</div>'
    '<div style="text-align: center"><span style="font-size: x-large">{navn}</span></div>'
    '<div style="text-align: center">{deltakelse}</div>'
    '<div style="text-align: center"><span style="font-size: large"><b>{kursnavn}</b></span></div>'
    '<div style="text-align: center"><br></div>'
    '<div style="text-align: center">{detaljer}</div>'
    '<div style="text-align: center"><br><br>______________________________<br>'
    '<span style="font-size: small">Kursansvarlig, IPR</span></div>'
)


class Kursbevisfeil(ValueError):
    """Kursbeviset kan ikke lagres. Meldingen er norsk og trygg å vise."""


def ramme_farge(kurs) -> str | None:
    nokkel = (kurs["kursbevis_ramme"] if kurs is not None else None) or STANDARD_RAMME
    return RAMMER.get(nokkel, RAMMER[STANDARD_RAMME])[1]


def for_redigering(kurs) -> str:
    """Innholdet som vises i redigereren: kursets egen versjon, ellers utgangspunktet (standard med flettefelt)."""
    return kurs["kursbevis_html"] if kurs["kursbevis_html"] else STANDARD_HTML


def klargjor(con, innhold: str, *, aktor: str) -> str:
    """Renset innhold klart til å lagres eller forhåndsvises: innlimte bilder i biblioteket, bare kjente flettefelt."""
    try:
        ren = signaturer.klargjor_innhold(con, innhold or "", aktor=aktor)
    except signaturer.Signaturfeil as e:
        raise Kursbevisfeil(str(e).replace("Signaturen", "Kursbeviset").replace("signaturen", "kursbeviset"))
    if not re.sub(r"<[^>]+>|&nbsp;|\s", "", ren):
        raise Kursbevisfeil("Kursbeviset er tomt. Skriv en tekst, eller trykk «Tilbakestill til standard».")
    ukjente = sorted({k for k in _KODE.findall(html_.unescape(re.sub(r"<[^>]+>", "", ren))) if k not in KODER})
    if ukjente:
        raise Kursbevisfeil("Ukjent flettefelt: " + ", ".join("{" + k + "}" for k in ukjente)
                            + ". Bruk knappene under redigereren for å sette inn flettefelt.")
    return ren


def lagre(con, kurs_id: int, innhold: str, ramme: str, *, aktor: str) -> None:
    if ramme not in RAMMER:
        raise Kursbevisfeil("Velg en ramme.")
    ren = klargjor(con, innhold, aktor=aktor)
    con.execute("UPDATE kurs SET kursbevis_html=?, kursbevis_ramme=? WHERE id=?", (ren, ramme, kurs_id))
    db.logg(con, "kursbevis_endret", {"kurs_id": kurs_id, "ramme": ramme}, aktor=aktor)


def tilbakestill(con, kurs_id: int, *, aktor: str) -> None:
    con.execute("UPDATE kurs SET kursbevis_html=NULL, kursbevis_ramme=NULL WHERE id=?", (kurs_id,))
    db.logg(con, "kursbevis_tilbakestilt", {"kurs_id": kurs_id}, aktor=aktor)


def kopier(con, til_kurs_id: int, fra_kurs_id: int, *, aktor: str) -> None:
    fra = con.execute("SELECT kursbevis_html, kursbevis_ramme FROM kurs WHERE id=?", (fra_kurs_id,)).fetchone()
    if not fra:
        raise Kursbevisfeil("Fant ikke kurset det skal kopieres fra.")
    con.execute("UPDATE kurs SET kursbevis_html=?, kursbevis_ramme=? WHERE id=?",
                (fra["kursbevis_html"], fra["kursbevis_ramme"], til_kurs_id))
    db.logg(con, "kursbevis_kopiert", {"kurs_id": til_kurs_id, "fra_kurs_id": fra_kurs_id}, aktor=aktor)


def kurs_med_egen_versjon(con, unntatt: int) -> list:
    return con.execute("""SELECT id, kursnr, navn FROM kurs WHERE kursbevis_html IS NOT NULL AND id<>?
                          ORDER BY kursnr DESC""", (unntatt,)).fetchall()


def _norsk_dato(iso: str) -> str:
    return f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", iso or "") else (iso or "")


def detaljer_html(v: dict) -> str:
    """Tabellen med samlinger, kursdager, timer, sted og løp - samme opplysninger som i standardbeviset."""
    rader = []
    if v.get("samlinger"):
        rader.append(("Samlinger", f"{v['samlinger']} (av kursets {v['antall_samlinger']})"))
    rader.append(("Kursdager", ", ".join(_norsk_dato(d) for d in v["dager"])))
    rader.append(("Timer", "%g" % v["timer"]))
    if v["kurs"]["sted"]:
        rader.append(("Sted", v["kurs"]["sted"]))
    if v.get("lop_timer") is not None:
        rader.append((f"Akkumulert i {v['kurs']['spesialistlop']}-løpet", "%g timer" % v["lop_timer"]))
    celle = 'style="padding:3px 12px; text-align:left"'
    return ('<table style="margin:0 auto; border-collapse:collapse; font-size:14px">'
            + "".join(f"<tr><td {celle}>{html_.escape(a)}</td><td {celle}>{html_.escape(b)}</td></tr>" for a, b in rader)
            + "</table>")


def _bilder_inn(con, ren: str) -> str:
    """Bilder fra biblioteket som data:-adresser, så kursbeviset er komplett i seg selv."""
    def bytt(m: re.Match) -> str:
        b = signaturer.bilde(con, int(m[1]))
        if not b:
            return 'src=""'
        mimetype, innhold = b
        return f'src="data:{mimetype};base64,{base64.b64encode(innhold).decode("ascii")}"'
    return re.sub(r'src="/admin/signaturer/bilde/(\d{1,9})"', bytt, ren)


def flett(con, ren: str, v: dict, i_setning) -> str:
    """Kursets egen versjon med flettefeltene fylt ut for én deltaker. `v` er det samme som standardbeviset får (navn,
    kurs, dager, timer, samlinger, antall_samlinger, lop_timer); `i_setning` gjør «Samling 1 og 3» om til «samling 1 og 3»."""
    deltakelse = f"har deltatt på {i_setning(v['samlinger'])} i" if v.get("samlinger") else "har gjennomført"
    verdier = {
        "navn": v["navn"], "fornavn": v.get("fornavn") or v["navn"].split(" ")[0], "kursnavn": v["kurs"]["navn"],
        "kursnr": str(v["kurs"]["kursnr"] or ""), "deltakelse": deltakelse,
        "kursdager": ", ".join(_norsk_dato(d) for d in v["dager"]), "timer": "%g" % v["timer"],
        "sted": v["kurs"]["sted"] or "", "dato": _norsk_dato(v.get("dato") or ""),
    }

    def bytt(m: re.Match) -> str:
        if m[1] == "detaljer":
            return detaljer_html(v)
        # quote=True: et flettefelt kan stå inne i en lenke eller alt-tekst, og et navn med «"» skal ikke kunne lage nye attributter
        return html_.escape(verdier[m[1]], quote=True) if m[1] in verdier else m[0]
    return _bilder_inn(con, _KODE.sub(bytt, ren))
