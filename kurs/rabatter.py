"""Rabattpriser (Camilla 09.10.2026, plan i OneDrive: Plan_rabattpriser_2026-10-09.md).

Rabattene velges per kurs (Oppsett → Priser og rabatter, tabellen kurs_rabatt): NIEFT-medlem, IPR-terapeut, student og
Psyflix-medlem, hver med sin prosent. Én rabatt per person. Deltakeren velger pris i påmeldingsskjemaet; i bedriftspåmeldingen
velges prisen per deltaker. Prisen er kursets pris minus prosenten, avrundet til hele kroner (deltakerpris / SQL_PRIS), og
følger kursets pris til fakturaen er laget. Prosenten står fast fra påmeldingen (paamelding.rabatt_prosent).

Retten til rabatten sjekkes når kurset har krysset av for «Må godkjennes» (standard for alle fire): påmeldingen får status
'venter', et gult merke (SJEKK_TEKST), og fakturaen holdes tilbake (sveiper._opprett) til en administrator trykker «Godkjent»
(rabatten står) eller «Ikke godkjent» (ordinær pris). Studentene laster opp studentbevis (tabellen rabatt_bevis), som slettes når
rabatten er avgjort. NIEFT sjekkes på e-posten (medlemsoversikten flyttes fra Pindena senere). Psyflix sjekkes hos Psyflix: hver
person, med organisasjonen avtalen går gjennom og samtykke til sjekken (Camilla: «ellers kan jo hvem som helst bare skrive Solli»).
"""
import base64
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import db

# Rekkefølgen her er rekkefølgen overalt (skjemaene, oppsettet og listene)
KATEGORIER = {"nieft": "NIEFT-medlem", "ipr_terapeut": "IPR-terapeut", "student": "Student", "psyflix": "Psyflix-medlem"}
PRISNAVN = {"nieft": "NIEFT-pris", "ipr_terapeut": "IPR-terapeutpris", "student": "Studentpris", "psyflix": "Psyflix-pris"}
# Navnet midt i en setning: forkortelser og navn beholder store bokstaver («NIEFT-pris», «Psyflix-pris»), resten får liten forbokstav
PRISNAVN_I_SETNING = {"nieft": "NIEFT-pris", "ipr_terapeut": "IPR-terapeutpris", "student": "studentpris", "psyflix": "Psyflix-pris"}
_I_SETNING = {PRISNAVN[k]: PRISNAVN_I_SETNING[k] for k in PRISNAVN}
STANDARD_PROSENT = {"nieft": 30, "ipr_terapeut": 50, "student": 50, "psyflix": 25}
SJEKK_TEKST = {"nieft": "Sjekk NIEFT-medlemskap", "ipr_terapeut": "Sjekk IPR-terapeut", "student": "Sjekk studentbevis",
               "psyflix": "Sjekk Psyflix-medlemskap"}
ORDINAER_NAVN = "Ordinær pris"
VENTER, GODKJENT, AVVIST = "venter", "godkjent", "avvist"

BEVIS_ENDELSER = ("pdf", "png", "jpg", "jpeg", "webp")
BEVIS_MAKS_MB = 10
_MIMETYPE = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}

# Prisen i SQL (samme regel som deltakerpris): kursets pris, eller kursets pris minus rabatten avrundet til hele kroner.
# Heltallsregning: (pris × (100 − prosent) + 50) / 100 gir samme avrunding i SQLite og PostgreSQL.
SQL_PRIS = "(CASE WHEN p.rabatt_prosent IS NULL THEN k.pris_nok ELSE (k.pris_nok * (100 - p.rabatt_prosent) + 50) / 100 END)"


class Rabattfeil(Exception):
    pass


def deltakerpris(kurspris, prosent) -> int:
    """Prisen for en deltaker: kursets pris minus rabatten i prosent, avrundet til hele kroner (Camilla 09.10.2026)."""
    kurspris = int(kurspris or 0)
    if not prosent:
        return kurspris
    return (kurspris * (100 - int(prosent)) + 50) // 100


def _felt(p, navn):
    """p[navn] for en rad eller dict uten å feile når feltet mangler (eldre rader, eksempler i malene)."""
    if not p:                      # None, tom dict eller Jinjas Undefined (malene)
        return None
    try:
        return p[navn]
    except (KeyError, IndexError, TypeError):
        return None


def pris_for(kurspris, p) -> int:
    """Filteret `deltakerpris` i e-postmalene: prisen deltakeren faktisk faktureres for."""
    return deltakerpris(kurspris, _felt(p, "rabatt_prosent"))


def prisnavn(p) -> str | None:
    """«Studentpris» osv. når påmeldingen har en rabatt som gjelder, ellers None (ordinær pris)."""
    kategori = _felt(p, "priskategori")
    return PRISNAVN.get(kategori) if kategori and _felt(p, "rabatt_prosent") else None


def i_setning(navn) -> str:
    """«Studentpris» -> «studentpris», men «NIEFT-pris» og «Psyflix-pris» står som de er (filteret pris_i_setning i malene)."""
    return _I_SETNING.get(navn, navn or "")


def venter(p) -> bool:
    """Venter rabatten på godkjenning? Da holdes fakturaen tilbake (sveiper._opprett)."""
    return _felt(p, "rabatt_status") == VENTER


def merke(p) -> str | None:
    """Det gule merket i deltakerlisten og deltakervinduet når rabatten venter på godkjenning."""
    return SJEKK_TEKST.get(_felt(p, "priskategori")) if venter(p) else None


# ============================ rabattene på kurset ============================

@dataclass(frozen=True)
class Rabatt:
    kategori: str
    prosent: int
    maa_godkjennes: bool
    pris: int                   # prisen i kroner med kursets pris nå

    @property
    def navn(self) -> str:
        return KATEGORIER[self.kategori]

    @property
    def prisnavn(self) -> str:
        return PRISNAVN[self.kategori]

    @property
    def prisnavn_i_setning(self) -> str:
        return PRISNAVN_I_SETNING[self.kategori]


def for_kurs(con, kurs) -> list[Rabatt]:
    """Rabattene kurset gir, i fast rekkefølge. Gratis kurs har ingen rabatter (det er ingenting å gi rabatt på)."""
    if not kurs["pris_nok"]:
        return []
    rader = {r["kategori"]: r for r in con.execute(
        "SELECT kategori, prosent, maa_godkjennes FROM kurs_rabatt WHERE kurs_id=?", (kurs["id"],))}
    return [Rabatt(k, rader[k]["prosent"], bool(rader[k]["maa_godkjennes"]), deltakerpris(kurs["pris_nok"], rader[k]["prosent"]))
            for k in KATEGORIER if k in rader]


def oppsett(con, kurs) -> list[dict]:
    """Radene i Oppsett → Priser og rabatter: alle fire, med det som er lagret eller standardverdiene."""
    lagret = {r["kategori"]: r for r in con.execute(
        "SELECT kategori, prosent, maa_godkjennes FROM kurs_rabatt WHERE kurs_id=?", (kurs["id"],))}
    ut = []
    for k, navn in KATEGORIER.items():
        r = lagret.get(k)
        prosent = r["prosent"] if r else STANDARD_PROSENT[k]
        ut.append({"kategori": k, "navn": navn, "aktiv": r is not None, "prosent": prosent,
                   "maa_godkjennes": bool(r["maa_godkjennes"]) if r else True,
                   "pris": deltakerpris(kurs["pris_nok"], prosent)})
    return ut


def les_oppsett(form) -> dict[str, tuple[int, bool]]:
    """{kategori: (prosent, må godkjennes)} fra skjemaet i Oppsett, for rabattene som er krysset av. Rabattfeil ved ugyldig prosent."""
    valg = {}
    for k, navn in KATEGORIER.items():
        if not form.get(f"rabatt_{k}"):
            continue
        tekst = (form.get(f"prosent_{k}") or "").strip().replace("%", "").strip()
        if not tekst.isdigit() or not 1 <= int(tekst) <= 99:
            raise Rabattfeil(f"Rabatten for {navn.lower()} må være et helt tall mellom 1 og 99 prosent.")
        valg[k] = (int(tekst), bool(form.get(f"godkjennes_{k}")))
    return valg


def lagre_for_kurs(con, kurs_id: int, valg: dict[str, tuple[int, bool]], *, aktor: str) -> bool:
    """Lagrer rabattene kurset gir (de som ikke er med i `valg`, fjernes). Gjelder nye påmeldinger: de som er påmeldt, beholder
    prosenten de fikk. Returnerer om noe ble endret. Kalleren committer."""
    for k, (prosent, _) in valg.items():
        if k not in KATEGORIER:
            raise Rabattfeil("Ukjent rabatt.")
        if not 1 <= int(prosent) <= 99:
            raise Rabattfeil(f"Rabatten for {KATEGORIER[k].lower()} må være mellom 1 og 99 prosent.")
    gamle = {r["kategori"]: (r["prosent"], bool(r["maa_godkjennes"])) for r in con.execute(
        "SELECT kategori, prosent, maa_godkjennes FROM kurs_rabatt WHERE kurs_id=?", (kurs_id,))}
    nye = {k: (int(p), bool(g)) for k, (p, g) in valg.items()}
    if gamle == nye:
        return False
    con.execute("DELETE FROM kurs_rabatt WHERE kurs_id=?", (kurs_id,))
    for k in KATEGORIER:
        if k in nye:
            con.execute("INSERT INTO kurs_rabatt (kurs_id, kategori, prosent, maa_godkjennes) VALUES (?,?,?,?)",
                        (kurs_id, k, nye[k][0], 1 if nye[k][1] else 0))
    db.logg(con, "kurs_rabatter_endret", {"kurs_id": kurs_id,
                                          "for": {k: list(v) for k, v in gamle.items()},
                                          "etter": {k: list(v) for k, v in nye.items()}}, aktor=aktor)
    return True


def kopier(con, fra_kurs_id: int, til_kurs_id: int) -> None:
    """Rabattene følger med når et kurs dupliseres (samme transaksjon som kopien)."""
    for r in con.execute("SELECT kategori, prosent, maa_godkjennes FROM kurs_rabatt WHERE kurs_id=?", (fra_kurs_id,)).fetchall():
        con.execute("INSERT INTO kurs_rabatt (kurs_id, kategori, prosent, maa_godkjennes) VALUES (?,?,?,?)",
                    (til_kurs_id, r["kategori"], r["prosent"], r["maa_godkjennes"]))


# ============================ valget ved påmelding ============================

@dataclass(frozen=True)
class Valg:
    kategori: str | None = None                       # None = ordinær pris
    bevis: tuple[str, str, bytes] | None = None       # studentbeviset: (filnavn, mimetype, innhold)
    psyflix_org: str | None = None
    psyflix_epost: str | None = None


ORDINAER = Valg()


def kontroller_bevis(filnavn: str, data: bytes) -> tuple[str, str, bytes]:
    """Studentbeviset: bilde (PNG, JPEG, WEBP) eller PDF, høyst BEVIS_MAKS_MB. Endelsen OG innholdet kontrolleres (aldri
    nettleserens Content-Type). Returnerer (filnavn, mimetype, innhold)."""
    from . import sidelager        # importeres her: sidelager henter mye annet
    navn = re.sub(r"[^\w .()-]", "_", (filnavn or "").replace("\\", "/").rsplit("/", 1)[-1]).strip()[:120] or "studentbevis"
    endelse = navn.rsplit(".", 1)[-1].lower() if "." in navn else ""
    if endelse not in BEVIS_ENDELSER:
        raise Rabattfeil("Studentbeviset må være et bilde (JPG, PNG eller WEBP) eller en PDF.")
    if not data:
        raise Rabattfeil("Filen med studentbeviset er tom. Prøv igjen.")
    if len(data) > BEVIS_MAKS_MB * 1024 * 1024:
        raise Rabattfeil(f"Filen med studentbeviset er for stor (maks {BEVIS_MAKS_MB} MB). Ta et nytt bilde, eller lagre den som PDF.")
    try:
        sidelager.kontroller_fil(navn, data)
    except sidelager.Sidefeil as e:
        raise Rabattfeil("Filen med studentbeviset kunne ikke leses som et bilde eller en PDF.") from e
    return navn, _MIMETYPE[endelse], data


def les_valg(rabatter: list[Rabatt], form, filer) -> tuple[Valg, list[str]]:
    """Prisvalget i påmeldingsskjemaet (kurs.html): (valget, feilmeldinger). Prosenten hentes aldri fra skjemaet, bare
    kategorien: alt annet kommer fra kursets oppsett."""
    kategori = (form.get("priskategori") or "").strip()
    tilgjengelig = {r.kategori for r in rabatter}
    if not kategori or not tilgjengelig:
        return ORDINAER, []
    if kategori not in tilgjengelig:
        return ORDINAER, ["Velg en av prisene i listen."]
    feil = []
    if not form.get("rabatt_bekreftet"):
        feil.append("Bekreft at du har rett på prisen du har valgt.")
    bevis = None
    if kategori == "student":
        fil = filer.get("studentbevis") if filer else None
        if not fil or not fil.filename:
            feil.append("Last opp studentbeviset ditt (bilde eller PDF).")
        else:
            try:
                bevis = kontroller_bevis(fil.filename, fil.read())
            except Rabattfeil as e:
                feil.append(str(e))
    org = epost = None
    if kategori == "psyflix":
        org = (form.get("psyflix_org") or "").strip()[:200] or None
        epost = (form.get("psyflix_epost") or "").strip()[:200] or None
        if not org:
            feil.append("Skriv hvilken organisasjon Psyflix-avtalen din går gjennom.")
        if epost and "@" not in epost:
            feil.append("Skriv en gyldig e-postadresse for Psyflix, eller la feltet stå tomt.")
        if not form.get("psyflix_samtykke"):
            feil.append("Kryss av for at IPR kan sjekke medlemskapet ditt hos Psyflix.")
    return Valg(kategori, bevis, org, epost), feil


def paamelding_felter(valg: Valg, rabatter: list[Rabatt], *, av_admin: bool = False) -> dict:
    """Feltene til db.meld_paa(paamelding=...). Ordinær pris: tomt (meld_paa nullstiller feltene). Velger en administrator
    prisen, er den avgjort med en gang (ingen sjekk). Prosenten er kursets prosent nå, og står fast for påmeldingen."""
    if not valg.kategori:
        return {}
    r = next((x for x in rabatter if x.kategori == valg.kategori), None)
    if r is None:
        raise Rabattfeil("Kurset gir ikke denne rabatten.")
    status = (GODKJENT if av_admin else VENTER) if r.maa_godkjennes else None
    return {"priskategori": r.kategori, "rabatt_prosent": r.prosent, "rabatt_status": status,
            "psyflix_org": valg.psyflix_org, "psyflix_epost": valg.psyflix_epost}


def lagre_bevis(con, paamelding_id: int, bevis: tuple[str, str, bytes]) -> None:
    filnavn, mimetype, data = bevis
    con.execute("DELETE FROM rabatt_bevis WHERE paamelding_id=?", (paamelding_id,))
    con.execute("""INSERT INTO rabatt_bevis (paamelding_id, filnavn, mimetype, storrelse, innhold, opprettet)
                   VALUES (?,?,?,?,?,?)""",
                (paamelding_id, filnavn, mimetype, len(data), base64.b64encode(data).decode("ascii"), db.naa_utc()))


def hent_bevis(con, paamelding_id: int) -> tuple[str, str, bytes] | None:
    r = con.execute("SELECT filnavn, mimetype, innhold FROM rabatt_bevis WHERE paamelding_id=?", (paamelding_id,)).fetchone()
    return (r["filnavn"], r["mimetype"], base64.b64decode(r["innhold"])) if r else None


def har_bevis(con, paamelding_id: int) -> bool:
    return con.execute("SELECT 1 FROM rabatt_bevis WHERE paamelding_id=?", (paamelding_id,)).fetchone() is not None


# ============================ avgjørelsen ============================

def kan_endres(con, paamelding_id: int) -> bool:
    """Prisen kan endres så lenge ingen faktura er laget eller forsøkt laget for påmeldingen (en laget faktura endres aldri)."""
    return not con.execute(
        """SELECT 1 FROM faktura WHERE paamelding_id=? UNION ALL SELECT 1 FROM faktura_forsok WHERE paamelding_id=?""",
        (paamelding_id, paamelding_id)).fetchone()


def _oppdater(con, paamelding_id: int, felter: dict) -> None:
    na = datetime.now().isoformat(timespec="seconds")
    con.execute(f"UPDATE paamelding SET {','.join(f'{k}=?' for k in felter)}, oppdatert=? WHERE id=?",
                [*felter.values(), na, paamelding_id])


def _ventende_rad(con, paamelding_id: int):
    p = con.execute("SELECT id, kurs_id, priskategori, rabatt_status FROM paamelding WHERE id=?", (paamelding_id,)).fetchone()
    if not p or p["rabatt_status"] != VENTER:
        raise Rabattfeil("Rabatten venter ikke på godkjenning (den kan være avgjort av noen andre).")
    return p


def godkjenn(con, paamelding_id: int, *, aktor: str) -> None:
    """«Godkjent»: rabatten står, og fakturaen kan lages. Studentbeviset slettes. Kalleren committer og setter faktureringen i gang."""
    p = _ventende_rad(con, paamelding_id)
    _oppdater(con, paamelding_id, {"rabatt_status": GODKJENT})
    con.execute("DELETE FROM rabatt_bevis WHERE paamelding_id=?", (paamelding_id,))
    db.logg(con, "rabatt_godkjent", {"paamelding_id": paamelding_id, "kurs_id": p["kurs_id"], "kategori": p["priskategori"]},
            aktor=aktor)


def avvis(con, paamelding_id: int, *, aktor: str) -> None:
    """«Ikke godkjent»: ordinær pris, og fakturaen kan lages. Kategorien står igjen (så det synes hva som ble valgt), men prosenten
    fjernes. Studentbeviset slettes. Kalleren committer."""
    p = _ventende_rad(con, paamelding_id)
    _oppdater(con, paamelding_id, {"rabatt_status": AVVIST, "rabatt_prosent": None})
    con.execute("DELETE FROM rabatt_bevis WHERE paamelding_id=?", (paamelding_id,))
    db.logg(con, "rabatt_avvist", {"paamelding_id": paamelding_id, "kurs_id": p["kurs_id"], "kategori": p["priskategori"]},
            aktor=aktor)


def endre(con, kurs, paamelding_id: int, kategori: str | None, *, aktor: str) -> bool:
    """Administrator endrer prisen i deltakervinduet (før fakturaen er laget). Valget er avgjort med en gang: ingen sjekk.
    `kategori` None/'' = ordinær pris. Returnerer om noe ble endret. Kalleren committer."""
    if not kan_endres(con, paamelding_id):
        raise Rabattfeil("Prisen kan ikke endres etter at fakturaen er laget. Rett fakturaen i Visma.")
    p = con.execute("SELECT priskategori, rabatt_prosent, rabatt_status FROM paamelding WHERE id=?", (paamelding_id,)).fetchone()
    kategori = kategori or None
    if kategori is None:
        if p["rabatt_prosent"] is None and p["rabatt_status"] in (None, AVVIST) and p["priskategori"] is None:
            return False
        nye = {"priskategori": None, "rabatt_prosent": None, "rabatt_status": None, "psyflix_org": None, "psyflix_epost": None}
    else:
        r = next((x for x in for_kurs(con, kurs) if x.kategori == kategori), None)
        if r is None:
            raise Rabattfeil("Kurset gir ikke denne rabatten. Slå den på under Oppsett først.")
        if p["priskategori"] == kategori and p["rabatt_prosent"] == r.prosent and p["rabatt_status"] in (None, GODKJENT):
            return False
        nye = {"priskategori": kategori, "rabatt_prosent": r.prosent, "rabatt_status": GODKJENT if r.maa_godkjennes else None}
        if kategori != "psyflix":
            nye |= {"psyflix_org": None, "psyflix_epost": None}
    _oppdater(con, paamelding_id, nye)
    con.execute("DELETE FROM rabatt_bevis WHERE paamelding_id=?", (paamelding_id,))
    db.logg(con, "pris_endret", {"paamelding_id": paamelding_id, "kurs_id": kurs["id"], "fra": p["priskategori"] if p["rabatt_prosent"]
                                 else None, "til": kategori}, aktor=aktor)
    return True


# ============================ for sidene ============================

def psyflix_siste_aar(con, deltaker_id: int, unntatt_paamelding_id: int, idag: date) -> bool:
    """Har personen fått Psyflix-rabatt hos IPR de siste 12 månedene (en annen påmelding)? Vilkåret er én videreutdanning med
    rabatt per år; administrator får et varsel i listen over rabatter som venter."""
    fra = (idag - timedelta(days=365)).isoformat()
    return con.execute(
        """SELECT 1 FROM paamelding WHERE deltaker_id=? AND id!=? AND priskategori='psyflix' AND rabatt_prosent IS NOT NULL
             AND status IN ('bekreftet','venteliste') AND COALESCE(rabatt_status,'')!='avvist' AND opprettet>=?""",
        (deltaker_id, unntatt_paamelding_id, fra)).fetchone() is not None


def ventende(con, idag: date) -> list[dict]:
    """Rabatter som venter på godkjenning, gruppert per kurs: [{kurs, rader}] (siden Rabatter som venter). Bare påmeldinger med
    plass eller på venteliste, på kurs som ikke er avlyst."""
    rader = con.execute(
        """SELECT p.id, p.kurs_id, p.deltaker_id, p.priskategori, p.rabatt_prosent, p.psyflix_org, p.psyflix_epost, p.status,
                  p.opprettet, d.navn, d.epost, k.kursnr, k.navn AS kursnavn, k.pris_nok,
                  (SELECT COUNT(*) FROM rabatt_bevis b WHERE b.paamelding_id=p.id) AS har_bevis
           FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id JOIN kurs k ON k.id=p.kurs_id
           WHERE p.rabatt_status='venter' AND p.status IN ('bekreftet','venteliste') AND k.status!='avlyst'
           ORDER BY k.kursnr, d.navn""").fetchall()
    grupper: dict[int, dict] = {}
    for r in rader:
        g = grupper.setdefault(r["kurs_id"], {"kurs": {"id": r["kurs_id"], "kursnr": r["kursnr"], "navn": r["kursnavn"]},
                                              "rader": []})
        g["rader"].append({"p": r, "pris": deltakerpris(r["pris_nok"], r["rabatt_prosent"]),
                           "merke": SJEKK_TEKST.get(r["priskategori"]), "prisnavn": PRISNAVN.get(r["priskategori"]),
                           "psyflix_aar": r["priskategori"] == "psyflix" and psyflix_siste_aar(con, r["deltaker_id"], r["id"], idag)})
    return list(grupper.values())


def antall_ventende(con) -> int:
    return con.execute(
        """SELECT COUNT(*) FROM paamelding p JOIN kurs k ON k.id=p.kurs_id
           WHERE p.rabatt_status='venter' AND p.status IN ('bekreftet','venteliste') AND k.status!='avlyst'""").fetchone()[0]


def psyflix_tekst(grupper: list[dict]) -> str:
    """Listen til e-posten til Psyflix: én linje per person som venter på Psyflix-sjekken (navn, e-post hos Psyflix,
    organisasjon og kurs). Ingenting annet om personen."""
    linjer = []
    for g in grupper:
        for r in g["rader"]:
            p = r["p"]
            if p["priskategori"] != "psyflix":
                continue
            linjer.append(f"{p['navn']} – {p['psyflix_epost'] or p['epost']} – {p['psyflix_org'] or 'organisasjon ikke oppgitt'} "
                          f"({g['kurs']['navn']})")
    return "\n".join(linjer)


def rydd(con) -> int:
    """Daglig: studentbevis til påmeldinger som er avmeldt, og til kurs som er avsluttet eller avlyst, slettes (de skal bare ligge
    her til rabatten er avgjort). Idempotent. Returnerer antall slettede bevis."""
    return con.execute(
        """DELETE FROM rabatt_bevis WHERE paamelding_id IN (
             SELECT p.id FROM paamelding p JOIN kurs k ON k.id=p.kurs_id
             WHERE p.status='avmeldt' OR k.status IN ('avsluttet','avlyst'))""").rowcount
