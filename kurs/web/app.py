"""Webapp: paamelding, QR-innsjekk, Min side og admin.

    python -m kurs.web.app        # http://127.0.0.1:5000
"""
import calendar
import csv
import io
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta
from functools import wraps

import qrcode
import qrcode.image.svg
from flask import (Flask, Response, abort, flash, g, make_response, redirect, render_template, request, session,
                   url_for)
from markupsafe import Markup
from werkzeug.exceptions import RequestEntityTooLarge

from .. import behandling, config, daglig, db, import_deltakere, mal_eksempler, maltekster, skjemafelt, sveiper
from ..feil import sikker_feiltekst
from ..integrasjoner import epost, sharepoint
from ..kjoring import Kjoring
from ..kursbevis import timer_i_lop

app = Flask(__name__)
app.secret_key = config.HEMMELIG_NOKKEL
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=30))


@app.context_processor
def _globale():
    return {"demo": config.DEMO, "idag": _idag().isoformat()}


def _idag() -> date:
    """I demo kan admin 'spole' datoen for aa vise flyten. Lagres i session."""
    if config.DEMO and session.get("demo_dato"):
        return date.fromisoformat(session["demo_dato"])
    return date.today()


def con():
    if "con" not in g:
        g.con = db.koble()
    return g.con


@app.teardown_appcontext
def _lukk(_exc):
    c = g.pop("con", None)
    if c:
        c.close()


def krever_admin(f):
    @wraps(f)
    def inner(*a, **kw):
        if not session.get("admin_id"):
            return redirect(url_for("admin_login", neste=request.path))
        return f(*a, **kw)
    return inner


def _aktor() -> str:
    """Hvem som utforer en handling, til hendelsesloggen."""
    return f"admin:{session['admin_brukernavn']}" if session.get("admin_id") else "system"


def krever_deltaker(f):
    @wraps(f)
    def inner(*a, **kw):
        if not session.get("deltaker_id"):
            return redirect(url_for("logg_inn", neste=request.path))
        return f(*a, **kw)
    return inner


def qr_svg(tekst: str) -> Markup:
    img = qrcode.make(tekst, image_factory=qrcode.image.svg.SvgPathImage, box_size=20, border=2)
    return Markup(img.to_string(encoding="unicode"))


def _fakturastatus(kurs, p) -> str:
    """Utleder en lesbar fakturastatus fra fakturaradene til paameldingen. Ingen egen kolonne trengs.

    NB: "Fakturert" betyr bare at fakturaen er sendt, ikke at den er betalt.
    """
    if kurs["fakturering"] != "person" or not kurs["pris_nok"]:
        return "–"
    if p["faktura_antall"] == 0:
        return "Ikke fakturert"
    if p["faktura_ubetalt"] == 0:
        return "Betalt"
    if p["faktura_ubetalt"] == p["faktura_antall"]:
        return "Fakturert"
    return "Delvis betalt"


# ======================= offentlig =======================

# Publiseringsvern (hardening-checkpoint foer skjemabygger). Kun disse statusverdiene gir OFFENTLIG tilgang til
# BAADE /kurs/<kode> (ordinaer paamelding) og /kurs/<kode>/gruppe (bedriftspaamelding) - GET og POST. Fail-closed:
# en statusverdi som IKKE staar i denne lista - ogsaa en fremtidig, ukjent en som en senere migrering skulle legge
# til i CHECK-constrainten i schema.sql - regnes som IKKE offentlig tilgjengelig, uten unntak (allow-list, ikke
# deny-list). Admin sin egen forhaandsvisning (admin_forhandsvis_paamelding) bruker IKKE denne sjekken og fungerer
# uansett status, med hensikt - se dens docstring.
OFFENTLIG_SYNLIGE_KURSSTATUSER = frozenset({"aapen", "full", "aktiv"})


def _kurs_offentlig_tilgjengelig(kurs) -> bool:
    return kurs["status"] in OFFENTLIG_SYNLIGE_KURSSTATUSER


def _skjema_snapshot(kurs) -> tuple:
    """Leser kursets skjemaoverstyringer EN gang og lager requestens ENE effektive skjema (fase 12C3).

    Snapshotet brukes til ALT i resten av requesten: rendring, hvilke POST-felt som leses, obligatorisk-validering,
    hva som lagres og ev. re-rendring ved feil. Det leses aldri paa nytt - endrer admin skjemaet mens en POST paagaar,
    fullfores POST-en etter sitt opprinnelige snapshot. Kaster skjemafelt.SkjemaLesefeil ved reell DB-lesefeil (aldri
    stille fallback til standardskjema). Returnerer (skjema, advarsler)."""
    les = db.hent_skjemaoverstyringer(con(), kurs["id"])
    return skjemafelt.effektivt_skjema(kurs, les), les.advarsler


def _render_paameldingsside(kurs, dager, f, plasser_igjen, skjema, **ekstra):
    """ENESTE vei til kurs.html - brukes av kursside() (GET og begge POST-feilstier) og admin-forhaandsvisningen. Tar
    imot requestens skjema-snapshot og leser ALDRI databasen selv."""
    return render_template("kurs.html", kurs=kurs, dager=dager, f=f, plasser_igjen=plasser_igjen, skjema=skjema,
                           **ekstra)


def _skjema_utilgjengelig(kurs, forhandsvisning: bool = False):
    """Kontrollert 503 naar skjemakonfigurasjonen ikke kan leses. Ingen detaljer om feilen (SQL, sti, unntak) og ingen
    persondata - kun kursnavnet. Fail-closed: skjemaet vises IKKE med standardoppsett, og ingenting registreres."""
    return render_template("paamelding_utilgjengelig.html", kurs=kurs, forhandsvisning=forhandsvisning), 503


# Faste identitetsfelt som alltid finnes i skjemaet (validering: se kursside()).
_IDENTITETSFELT = ("navn", "epost", "samtykke")


def _tillatte_innsendte_verdier(form, skjema) -> dict:
    """Raa POST -> KUN feltene dette skjema-snapshotet faktisk viser (fase 12C3). Alt annet ignoreres fullstendig, uansett
    hva klienten sender: skjult telefon/arbeidssted, HPR uten spesialistlop, sensitive felt paa digitale kurs og hele
    faktura-/betalingsblokken naar den ikke vises. Verdiene er uendrede raa strenger (samme som foer for synlige felt)."""
    tillatt = [*_IDENTITETSFELT, *(felt.nokkel for felt in skjema.deltakerfelt)]
    if skjema.vis_fakturablokk:
        tillatt += skjemafelt.FAKTURABLOKK.felt
    if skjema.vis_sensitive_felt:
        tillatt += skjemafelt.SENSITIV_BLOKK.felt
    return {k: form[k] for k in tillatt if k in form}


def _logg_skjemaadvarsler(kurs_id: int, advarsler) -> None:
    """PII-fritt: kun kurs_id, kjent felt, kjent egenskap og grunnkode (Advarsel baerer aldri tekst/ukjent feltnavn)."""
    if advarsler:
        db.logg(con(), "skjemafelt_advarsel", {"kurs_id": kurs_id, "advarsler": [
            {"felt": a.felt, "egenskap": a.egenskap, "grunn": a.grunn} for a in advarsler]})


@app.get("/")
def forside():
    kurs = con().execute(
        """SELECT k.*, MIN(kd.dato) AS start, COUNT(kd.id) AS dager FROM kurs k JOIN kursdag kd ON kd.kurs_id=k.id
           WHERE k.status IN ('aapen','full') GROUP BY k.id ORDER BY start""").fetchall()
    return render_template("forside.html", kurs=kurs)


@app.route("/kurs/<kode>", methods=["GET", "POST"])
def kursside(kode):
    kurs = con().execute("SELECT * FROM kurs WHERE kode=?", (kode,)).fetchone() or abort(404)
    if not _kurs_offentlig_tilgjengelig(kurs):
        abort(404)
    # Skjema-snapshot FOER all validering og FOER foerste varige skriving (meld_paa) / eksterne sideeffekter (sveiper).
    try:
        skjema, advarsler = _skjema_snapshot(kurs)
    except skjemafelt.SkjemaLesefeil:
        return _skjema_utilgjengelig(kurs)
    dager = db.kursdager(con(), kurs["id"])
    if request.method == "GET":
        return _render_paameldingsside(kurs, dager, {},
                                       None if kurs["kapasitet"] is None
                                       else kurs["kapasitet"] - db.antall_bekreftet(con(), kurs["id"]), skjema)
    # Herfra er `f` KUN de feltene snapshotet viser - raa request.form brukes ikke lenger.
    f = _tillatte_innsendte_verdier(request.form, skjema)
    feil = []
    if not f.get("navn") or "@" not in f.get("epost", ""):
        feil.append("Fyll inn navn og gyldig e-post.")
    for felt in skjema.deltakerfelt:
        if felt.obligatorisk and not (f.get(felt.nokkel) or "").strip():
            feil.append(f"Fyll inn «{felt.label}».")
    if not f.get("samtykke"):
        feil.append("Du må godta vilkår og personvernerklæring.")
    org = f.get("betaler") == "organisasjon"   # alltid False naar fakturablokken ikke vises (feltet er filtrert bort)
    if org and not (f.get("org_navn") and f.get("org_nr")):
        feil.append("Fyll inn organisasjon og org.nr. når arbeidsgiver betaler.")
    if feil:
        for x in feil:
            flash(x, "feil")
        return _render_paameldingsside(kurs, dager, f, None, skjema), 400
    try:
        with db.transaksjon(con()):
            _logg_skjemaadvarsler(kurs["id"], advarsler)
            pid, status = db.meld_paa(
                con(), kurs["id"], epost=f["epost"], navn=f["navn"],
                # None for skjulte felt: finn_eller_opprett_deltaker overskriver aldri med tom verdi, saa en eksisterende
                # persons telefon/arbeidssted/HPR blir verken endret eller slettet av et kurs som skjuler feltet.
                deltaker={"telefon": f.get("telefon"), "arbeidssted": f.get("arbeidssted"), "hpr_nr": f.get("hpr_nr")},
                paamelding={k: f.get(k) or None for k in (
                    "org_navn", "org_nr", "faktura_epost", "faktura_ref", "faktura_adresse", "faktura_postnr", "faktura_sted")}
                | {"betaler": "organisasjon" if org else "person", "ehf": 1 if f.get("ehf") else 0,
                   "betaling": f.get("betaling", "samlet")},
                sensitivt={"allergier": f.get("allergier"), "tilrettelegging": f.get("tilrettelegging")},
                idag=_idag(),
            )
    except db.Paameldingsfeil as e:
        flash(str(e), "feil")
        return _render_paameldingsside(kurs, dager, f, None, skjema), 400
    # "Webhook": kjor sveipene med en gang (bekreftelse + faktura). Daglig jobb tar det som evt. feiler.
    sveiper.kjor(Kjoring(con(), idag=_idag()), pid)
    con().commit()
    return render_template("kvittering.html", kurs=kurs, status=status, navn=f["navn"])


# ---------- bedriftspaamelding (fase 7) ----------

MAKS_DELTAKERE_GRUPPE = 30


def _grupperad_liste(f) -> list[dict]:
    """Bygger deltakerraden-listen fra skjemaet, uten aa filtrere bort noe - brukes til aa vise
    skjemaet paa nytt akkurat slik brukeren skrev det, ved valideringsfeil."""
    navn = f.getlist("deltaker_navn")
    epost = f.getlist("deltaker_epost")
    telefon = f.getlist("deltaker_telefon")
    arbeidssted = f.getlist("deltaker_arbeidssted")
    hpr = f.getlist("deltaker_hpr")
    n = len(navn)
    return [{"navn": navn[i], "epost": epost[i] if i < len(epost) else "",
            "telefon": telefon[i] if i < len(telefon) else "",
            "arbeidssted": arbeidssted[i] if i < len(arbeidssted) else "",
            "hpr_nr": hpr[i] if i < len(hpr) else ""} for i in range(n)]


@app.route("/kurs/<kode>/gruppe", methods=["GET", "POST"])
def kurs_gruppe(kode):
    kurs = con().execute("SELECT * FROM kurs WHERE kode=?", (kode,)).fetchone() or abort(404)
    if not _kurs_offentlig_tilgjengelig(kurs):
        abort(404)
    if request.method == "GET":
        return render_template("kurs_gruppe.html", kurs=kurs, f={}, deltakere=[{}], maks_deltakere=MAKS_DELTAKERE_GRUPPE)

    f = request.form
    feil = []
    if kurs["status"] not in ("aapen", "full", "aktiv"):
        feil.append("Kurset er ikke åpent for påmelding.")
    if kurs["paameldingsfrist"] and _idag() > date.fromisoformat(kurs["paameldingsfrist"]):
        feil.append(f"Påmeldingsfristen ({kurs['paameldingsfrist']}) er passert.")
    if not f.get("kontakt_navn") or "@" not in f.get("kontakt_epost", ""):
        feil.append("Fyll inn kontaktpersonens navn og en gyldig e-postadresse.")
    if not f.get("firmanavn", "").strip():
        feil.append("Fyll inn firmanavn.")
    if not f.get("org_nr", "").strip():
        feil.append("Fyll inn organisasjonsnummer.")
    if not f.get("samtykke"):
        feil.append("Dere må godta vilkår og personvernerklæring.")

    rader_visning = _grupperad_liste(f)
    if len(rader_visning) > MAKS_DELTAKERE_GRUPPE:
        feil.append(f"Maks {MAKS_DELTAKERE_GRUPPE} deltakere per innsending. Del opp i flere omganger.")

    # 12C3B: HPR-feltet vises kun naar kurset er i et spesialistlop (samme regel som kurs_gruppe.html og individuell
    # paamelding). Ellers ignoreres en innsendt deltaker_hpr fullstendig - None overskriver/sletter aldri en eksisterende
    # persons HPR (finn_eller_opprett_deltaker oppdaterer kun med ikke-tomme verdier).
    hpr_synlig = skjemafelt.vis_hpr(kurs)
    deltaker_rader = []
    for i, r in enumerate(rader_visning, start=1):
        navn, deltaker_epost = r["navn"].strip(), r["epost"].strip().lower()
        if not navn and not deltaker_epost:
            continue
        if not navn or "@" not in deltaker_epost:
            feil.append(f"Deltaker {i}: fyll inn navn og en gyldig e-postadresse.")
            continue
        deltaker_rader.append({"navn": navn, "epost": deltaker_epost, "telefon": r["telefon"].strip() or None,
                               "arbeidssted": r["arbeidssted"].strip() or None,
                               "hpr_nr": (r["hpr_nr"].strip() or None) if hpr_synlig else None})
    if not deltaker_rader:
        feil.append("Legg til minst én deltaker.")
    eposter = [d["epost"] for d in deltaker_rader]
    if len(eposter) != len(set(eposter)):
        feil.append("Samme e-postadresse er oppgitt flere ganger blant deltakerne.")

    if feil:
        for x in feil:
            flash(x, "feil")
        return render_template("kurs_gruppe.html", kurs=kurs, f=f, deltakere=rader_visning or [{}],
                               maks_deltakere=MAKS_DELTAKERE_GRUPPE), 400

    kontakt = {
        "navn": f["kontakt_navn"].strip(), "epost": f["kontakt_epost"].strip().lower(),
        "telefon": f.get("kontakt_telefon", "").strip() or None, "firmanavn": f["firmanavn"].strip(),
        "org_nr": f.get("org_nr", "").strip() or None, "faktura_ref": f.get("faktura_ref", "").strip() or None,
        "faktura_adresse": f.get("faktura_adresse", "").strip() or None,
        "faktura_postnr": f.get("faktura_postnr", "").strip() or None,
        "faktura_sted": f.get("faktura_sted", "").strip() or None, "ehf": bool(f.get("ehf")),
    }
    antall_totalt = len(deltaker_rader)

    # Preflight FOER noen varig sideeffekt (firmapaamelding/deltaker-registrering/deltakermail): kun det
    # ADMIN-REDIGERBARE innholdet (emne/innledning/avslutning) rendres her, med data som er kjent uansett hva
    # registreringen ender med. Den ferdige malteksten gjenbrukes ordrett i den faktiske kvitteringen lenger
    # ned - de faktiske utfallstallene (bekreftet/venteliste/feilet) er ALDRI koder og krever derfor ingen nytt
    # DB-oppslag der. En korrupt override oppdages dermed FOER firma/deltakere finnes, og ingenting registreres.
    try:
        firma_maltekst = maltekster.maltekst_for_utsending(
            con(), "firmapaamelding_kvittering", {"kontakt": kontakt, "kurs": kurs, "antall_totalt": antall_totalt})
    except maltekster.MalFeil as e:
        db.logg(con(), "firmapaamelding_mal_feil", {"kurs_id": kurs["id"], **e.detaljer()})
        con().commit()
        flash("Påmeldingen kunne ikke fullføres akkurat nå. Prøv igjen om noen minutter, eller ta kontakt med "
              "kursadministrasjonen dersom problemet vedvarer.", "feil")
        return render_template("kurs_gruppe.html", kurs=kurs, f=f, deltakere=rader_visning or [{}],
                               maks_deltakere=MAKS_DELTAKERE_GRUPPE), 400

    firma, ny = db.finn_eller_opprett_firmapaamelding(con(), kurs["id"], kontakt, eposter)
    if not ny:
        # Samme innsending som fra for (dobbeltklikk/refresh) - ikke behandle paa nytt, bare vis kvitteringen.
        return redirect(url_for("kurs_gruppe_kvittering", kode=kode, token=firma["kvittering_token"]))

    resultater = []  # (rad, paamelding_id|None, status|None, feilmelding|None)
    for rad in deltaker_rader:
        try:
            with db.transaksjon(con()):
                pid, status = db.meld_paa(
                    con(), kurs["id"], epost=rad["epost"], navn=rad["navn"],
                    deltaker={"telefon": rad["telefon"], "arbeidssted": rad["arbeidssted"], "hpr_nr": rad["hpr_nr"]},
                    paamelding={"betaler": "organisasjon", "org_navn": kontakt["firmanavn"], "org_nr": kontakt["org_nr"],
                                "faktura_epost": kontakt["epost"], "faktura_ref": kontakt["faktura_ref"],
                                "faktura_adresse": kontakt["faktura_adresse"], "faktura_postnr": kontakt["faktura_postnr"],
                                "faktura_sted": kontakt["faktura_sted"], "ehf": 1 if kontakt["ehf"] else 0,
                                "kilde": "gruppe"},
                    idag=_idag())
            resultater.append((rad, pid, status, None))
        except db.Paameldingsfeil as e:
            resultater.append((rad, None, None, str(e)))

    for rad, pid, _status, feilmelding in resultater:
        db.registrer_firmapaamelding_rad(con(), firma["id"], rad["navn"], rad["epost"], pid, feilmelding)
    con().commit()

    # Individuell bekreftelse/venteliste-e-post KUN til de som faktisk ble registrert nyaa - en feilet
    # rad har ingen paamelding_id og faar dermed aldri noen e-post herfra.
    for _rad, pid, _status, _feilmelding in resultater:
        if pid:
            sveiper.kjor(Kjoring(con(), idag=_idag()), pid)
    con().commit()

    antall_bekreftet = sum(1 for _, _, s, _ in resultater if s == "bekreftet")
    antall_venteliste = sum(1 for _, _, s, _ in resultater if s == "venteliste")
    antall_feilet = sum(1 for _, _, _, feil_ in resultater if feil_)
    kvittering_url = url_for("kurs_gruppe_kvittering", kode=kode, token=firma["kvittering_token"])
    # Ferdigstiller MED det allerede rendrede maltekst-resultatet fra preflighten over - IKKE et nytt DB-oppslag.
    # epost.render() med en eksplisitt maltekst=... gjor ingen maltekster-/DB-lesing (kun ren, lokal Jinja-rendring
    # av den laaste utfallsblokken), saa dette kan aldri gi MalFeil her - kun selve sendingen kan feile (Graph),
    # uendret fase-11-semantikk via send_ferdigrendret_en_gang.
    emne, html = epost.render("firmapaamelding_kvittering", maltekst=firma_maltekst, kontakt=kontakt, kurs=kurs,
                              kvittering_url=kvittering_url, antall_totalt=antall_totalt,
                              antall_bekreftet=antall_bekreftet, antall_venteliste=antall_venteliste,
                              antall_feilet=antall_feilet)
    Kjoring(con(), idag=_idag()).send_ferdigrendret_en_gang(
        f"firmapaamelding:{firma['id']}", kontakt["epost"], "firmapaamelding_kvittering", emne, html)
    db.logg(con(), "firmapaamelding_opprettet", {
        "firmapaamelding_id": firma["id"], "kurs_id": kurs["id"], "antall": len(deltaker_rader),
        "bekreftet": antall_bekreftet, "venteliste": antall_venteliste, "feilet": antall_feilet})
    con().commit()

    return redirect(kvittering_url)


@app.get("/kurs/<kode>/gruppe/kvittering/<token>")
def kurs_gruppe_kvittering(kode, token):
    kurs = con().execute("SELECT * FROM kurs WHERE kode=?", (kode,)).fetchone() or abort(404)
    firma = con().execute(
        "SELECT * FROM firmapaamelding WHERE kvittering_token=? AND kurs_id=?", (token, kurs["id"])
    ).fetchone() or abort(404)
    rader = con().execute(
        """SELECT r.navn, r.feilmelding, p.status AS paamelding_status FROM firmapaamelding_rad r
           LEFT JOIN paamelding p ON p.id=r.paamelding_id WHERE r.firmapaamelding_id=? ORDER BY r.id""",
        (firma["id"],)).fetchall()
    return render_template("kurs_gruppe_kvittering.html", kurs=kurs, firma=firma, rader=rader)


# ---------- innsjekk ----------

def _sjekk_inn(kursdag, epost_: str | None = None, deltaker_id: int | None = None, kilde="qr"):
    if kursdag["dato"] != _idag().isoformat():
        return "feil", "Innsjekk for denne kursdagen er ikke åpen i dag."
    sql = """SELECT p.id, d.navn FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id
             WHERE p.kurs_id=? AND p.status='bekreftet' AND """
    rad = (con().execute(sql + "d.id=?", (kursdag["kurs_id"], deltaker_id)).fetchone() if deltaker_id
           else con().execute(sql + "d.epost=?", (kursdag["kurs_id"], (epost_ or "").strip().lower())).fetchone())
    if not rad:
        return "feil", "Fant ingen påmelding med denne e-posten på kurset. Kontakt kursansvarlig."
    ny = db.registrer_oppmote(con(), rad["id"], kursdag["id"], kilde)
    con().commit()
    return "ok", f"Velkommen, {rad['navn']}! Oppmøte er registrert." if ny else f"Hei {rad['navn']} – du var allerede sjekket inn i dag."


@app.route("/innsjekk/<token>", methods=["GET", "POST"])
def innsjekk_qr(token):
    kd = con().execute(
        "SELECT kd.*, k.navn FROM kursdag kd JOIN kurs k ON k.id=kd.kurs_id WHERE innsjekk_token=?", (token,)
    ).fetchone() or abort(404)
    if session.get("deltaker_id") and request.method == "GET":
        status, tekst = _sjekk_inn(kd, deltaker_id=session["deltaker_id"])
        return render_template("innsjekk_resultat.html", status=status, tekst=tekst, kurs=kd)
    if request.method == "POST":
        status, tekst = _sjekk_inn(kd, epost_=request.form.get("epost"))
        if status == "ok" and request.form.get("husk"):
            d = con().execute("SELECT id FROM deltaker WHERE epost=?", (request.form["epost"].strip().lower(),)).fetchone()
            session.permanent, session["deltaker_id"] = True, d["id"]  # neste kursdag: ett skann, ingen skriving
        return render_template("innsjekk_resultat.html", status=status, tekst=tekst, kurs=kd)
    return render_template("innsjekk.html", kd=kd, med_kode=False)


@app.route("/innsjekk", methods=["GET", "POST"])
def innsjekk_kode():
    if request.method == "GET":
        return render_template("innsjekk.html", kd=None, med_kode=True)
    kode = db.alfanumerisk(request.form.get("kode", ""))
    kd = con().execute(
        "SELECT kd.*, k.navn FROM kursdag kd JOIN kurs k ON k.id=kd.kurs_id WHERE innsjekk_kode=? AND dato=?",
        (kode, _idag().isoformat())).fetchone()
    if not kd:
        flash("Ukjent kode, eller koden gjelder ikke i dag.", "feil")
        return render_template("innsjekk.html", kd=None, med_kode=True), 400
    status, tekst = _sjekk_inn(kd, epost_=request.form.get("epost"), kilde="kode")
    return render_template("innsjekk_resultat.html", status=status, tekst=tekst, kurs=kd)


# ---------- Min side ----------

@app.route("/logg-inn", methods=["GET", "POST"])
def logg_inn():
    if request.method == "POST":
        adr = request.form.get("epost", "").strip().lower()
        d = con().execute("SELECT * FROM deltaker WHERE epost=?", (adr,)).fetchone()
        if d:
            token = secrets.token_urlsafe(32)
            con().execute("INSERT INTO innlogging_token (token, deltaker_id, utloper) VALUES (?,?,?)",
                          (token, d["id"], (datetime.now() + timedelta(minutes=30)).isoformat()))
            con().commit()
            lenke = f"{config.BASE_URL}{url_for('logg_inn_token', token=token)}"
            emne, html = epost.render("innlogging", navn=d["navn"], lenke=lenke)
            epost.send(d["epost"], emne, html)
            if config.DEMO:
                flash(Markup(f'Demo: innloggingslenken er «sendt». <a href="{lenke}">Klikk her for å logge inn</a>.'), "info")
        # Samme svar uansett – avslorer ikke hvem som er registrert
        return render_template("logg_inn.html", sendt=True)
    return render_template("logg_inn.html", sendt=False)


@app.get("/logg-inn/<token>")
def logg_inn_token(token):
    t = con().execute("SELECT * FROM innlogging_token WHERE token=?", (token,)).fetchone()
    if not t or t["brukt"] or t["utloper"] < datetime.now().isoformat():
        flash("Lenken er utløpt eller allerede brukt. Be om en ny.", "feil")
        return redirect(url_for("logg_inn"))
    con().execute("UPDATE innlogging_token SET brukt=1 WHERE token=?", (token,))
    con().commit()
    session.permanent, session["deltaker_id"] = True, t["deltaker_id"]
    return redirect(request.args.get("neste") or url_for("min_side"))


@app.get("/logg-ut")
def logg_ut():
    session.clear()
    return redirect(url_for("forside"))


@app.get("/min-side")
@krever_deltaker
def min_side():
    did = session["deltaker_id"]
    deltaker = con().execute("SELECT * FROM deltaker WHERE id=?", (did,)).fetchone()
    kursliste = []
    for p in con().execute(
            """SELECT p.*, k.navn, k.kode, k.type, k.sted, k.zoom_url, k.spesialistlop, k.timer_pr_dag,
                      k.sharepoint_mappe, k.status AS kursstatus
               FROM paamelding p JOIN kurs k ON k.id=p.kurs_id
               WHERE p.deltaker_id=? AND p.status!='avmeldt' ORDER BY p.opprettet DESC""", (did,)):
        dager = con().execute(
            """SELECT kd.dato, o.kilde FROM kursdag kd LEFT JOIN oppmote o ON o.kursdag_id=kd.id AND o.paamelding_id=?
               WHERE kd.kurs_id=? ORDER BY kd.dato""", (p["id"], p["kurs_id"])).fetchall()
        filer = sharepoint.list_filer(f"{p['sharepoint_mappe']}/Presentasjoner") if p["sharepoint_mappe"] and p["status"] == "bekreftet" else []
        fakturaer = con().execute(
            """SELECT f.*, kd.dato FROM faktura f LEFT JOIN kursdag kd ON kd.id=f.kursdag_id
               WHERE f.paamelding_id=? ORDER BY kd.dato, f.id""", (p["id"],)).fetchall()
        kursliste.append({"p": p, "dager": dager, "filer": filer, "fakturaer": fakturaer})
    dokumenter = con().execute(
        """SELECT d.*, k.navn AS kursnavn FROM dokument d LEFT JOIN kurs k ON k.id=d.kurs_id
           WHERE d.publisert=1 AND (d.deltaker_id=? OR (d.deltaker_id IS NULL AND d.kurs_id IN
                 (SELECT kurs_id FROM paamelding WHERE deltaker_id=? AND status='bekreftet')))
           ORDER BY d.opprettet DESC""", (did, did)).fetchall()
    lop = {r["spesialistlop"]: timer_i_lop(con(), did, r["spesialistlop"]) for r in con().execute(
        """SELECT DISTINCT k.spesialistlop FROM paamelding p JOIN kurs k ON k.id=p.kurs_id
           WHERE p.deltaker_id=? AND k.spesialistlop IS NOT NULL""", (did,))}
    return render_template("min_side.html", deltaker=deltaker, kursliste=kursliste, dokumenter=dokumenter, lop=lop)


def _har_tilgang_kurs(did: int, kurs_id: int) -> bool:
    return con().execute("SELECT 1 FROM paamelding WHERE deltaker_id=? AND kurs_id=? AND status='bekreftet'",
                         (did, kurs_id)).fetchone() is not None


@app.get("/dokument/<int:dok_id>")
def dokument(dok_id):
    d = con().execute("SELECT * FROM dokument WHERE id=?", (dok_id,)).fetchone() or abort(404)
    did = session.get("deltaker_id")
    lov = session.get("admin_id") or (did and (d["deltaker_id"] == did or
                                            (d["deltaker_id"] is None and _har_tilgang_kurs(did, d["kurs_id"]))))
    if not lov:
        abort(403)
    if d["url"].startswith("lokal:"):
        sti = (config.ROT / d["url"][6:]).resolve()
        if config.ROT.resolve() not in sti.parents:
            abort(403)
        return Response(sti.read_bytes(), mimetype="text/html" if sti.suffix == ".html" else "application/octet-stream")
    if d["url"].startswith("sp:"):
        return _send_fil(d["url"][3:])
    return redirect(d["url"])


@app.get("/materiell/<int:kurs_id>/<path:navn>")
@krever_deltaker
def materiell(kurs_id, navn):
    kurs = con().execute("SELECT * FROM kurs WHERE id=?", (kurs_id,)).fetchone() or abort(404)
    if not _har_tilgang_kurs(session["deltaker_id"], kurs_id):
        abort(403)
    return _send_fil(f"{kurs['sharepoint_mappe']}/Presentasjoner/{navn}")


def _send_fil(sti: str):
    try:
        innhold = sharepoint.hent_fil(sti)
    except (FileNotFoundError, PermissionError):
        abort(404)
    return Response(innhold, mimetype="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{sti.rsplit("/", 1)[-1]}"'})


# ---------- levering av materiell (kursholder) ----------

@app.route("/lever/<int:krav_id>", methods=["GET", "POST"])
def lever(krav_id):
    # NB prototype: lenken er uten token. I prod: signert lenke eller Microsoft-innlogging for ansatte.
    m = con().execute("SELECT m.*, k.navn AS kursnavn, k.sharepoint_mappe FROM materiell_krav m JOIN kurs k ON k.id=m.kurs_id WHERE m.id=?",
                      (krav_id,)).fetchone() or abort(404)
    if request.method == "POST":
        fil = request.files.get("fil")
        if not fil or not fil.filename:
            flash("Velg en fil.", "feil")
        else:
            sharepoint.last_opp(f"{m['sharepoint_mappe']}/Presentasjoner", fil.filename, fil.read())
            con().execute("UPDATE materiell_krav SET levert_ts=datetime('now') WHERE id=?", (krav_id,))
            db.logg(con(), "materiell_levert", {"krav_id": krav_id, "fil": fil.filename}, aktor=m["ansvarlig_epost"])
            con().commit()
            flash("Takk! Filen er lastet opp og er nå tilgjengelig for deltakerne på Min side.", "ok")
            return redirect(url_for("lever", krav_id=krav_id))
    return render_template("lever.html", m=m)


# ======================= admin =======================

@app.route("/admin/logg-inn", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        bruker = db.verifiser_admin(con(), request.form.get("brukernavn", ""), request.form.get("passord", ""))
        if bruker:
            session.permanent = True
            session["admin_id"], session["admin_brukernavn"], session["admin_navn"] = (
                bruker["id"], bruker["brukernavn"], bruker["navn"])
            return redirect(request.args.get("neste") or url_for("admin"))
        flash("Feil brukernavn eller passord.", "feil")
    return render_template("admin_login.html")


@app.route("/admin/brukere", methods=["GET", "POST"])
@krever_admin
def admin_brukere():
    if request.method == "POST":
        f = request.form
        if not f.get("navn", "").strip() or not f.get("brukernavn", "").strip() or not f.get("passord"):
            flash("Fyll inn navn, brukernavn og passord.", "feil")
        elif len(f["passord"]) < 8:
            flash("Passordet må være minst 8 tegn.", "feil")
        else:
            try:
                db.opprett_admin_bruker(con(), f["brukernavn"], f["navn"], f["passord"], aktor=_aktor())
                con().commit()
                flash(f"Brukeren «{f['brukernavn'].strip().lower()}» er opprettet.", "ok")
            except sqlite3.IntegrityError:
                flash("Det finnes allerede en bruker med det brukernavnet.", "feil")
        return redirect(url_for("admin_brukere"))
    brukere = con().execute("SELECT * FROM admin_bruker ORDER BY navn").fetchall()
    return render_template("admin_brukere.html", brukere=brukere)


@app.post("/admin/brukere/<int:bid>/deaktiver")
@krever_admin
def admin_bruker_deaktiver(bid):
    con().execute("UPDATE admin_bruker SET aktiv = 1 - aktiv WHERE id=?", (bid,))
    db.logg(con(), "admin_bruker_endret", {"id": bid}, aktor=_aktor())
    con().commit()
    return redirect(url_for("admin_brukere"))


@app.get("/admin")
@krever_admin
def admin():
    kurs = con().execute(
        """SELECT k.*, MIN(kd.dato) AS start, MAX(kd.dato) AS slutt,
                  (SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=k.id AND p.status='bekreftet') AS bekreftet,
                  (SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=k.id AND p.status='venteliste') AS venteliste,
                  (SELECT COUNT(*) FROM faktura f JOIN paamelding p ON p.id=f.paamelding_id WHERE p.kurs_id=k.id) AS fakturert
           FROM kurs k LEFT JOIN kursdag kd ON kd.kurs_id=k.id GROUP BY k.id ORDER BY start DESC""").fetchall()
    mangler = con().execute(
        """SELECT m.*, k.kode, k.navn AS kursnavn FROM materiell_krav m JOIN kurs k ON k.id=m.kurs_id
           WHERE m.levert_ts IS NULL ORDER BY m.frist""").fetchall()
    hendelser = con().execute("SELECT * FROM hendelse ORDER BY id DESC LIMIT 15").fetchall()
    # Levende telling fra NAAVAERENDE tilstand (ikke kumulativ hendelseslogg): uavklarte e-post-/faktura-
    # operasjoner som vi bevisst IKKE prover automatisk paa nytt. Samme staleness-grense som motoren.
    uavklart = db.uavklarte_operasjoner(con())
    return render_template("admin.html", kurs=kurs, mangler=mangler, hendelser=hendelser, uavklart=uavklart)


# ------- aktivitetsoversikt -------

AKTIVITET_SORTER = {
    "kode": "k.kode", "tittel": "k.navn", "start": "start", "status": "k.status",
    "paameldte": "bekreftet", "ansvarlig": "ansvarlig_navn",
}


@app.get("/admin/aktiviteter")
@krever_admin
def admin_aktiviteter():
    sok = request.args.get("sok", "").strip()
    mine = request.args.get("mine") == "1"
    ansvarlig = request.args.get("ansvarlig", type=int) or 0
    status = request.args.get("status") or ""
    if status not in KURS_STATUSVERDIER:
        status = ""
    sted = request.args.get("sted") or ""
    if sted not in ("bergen", "oslo", "online"):
        sted = ""
    sorter = request.args.get("sorter") or ""
    sorter = sorter if sorter in AKTIVITET_SORTER else None
    retning = "desc" if request.args.get("retning") == "desc" else "asc"
    antall = request.args.get("antall", type=int) or 25
    antall = antall if antall in (10, 25, 50, 100) else 25
    side = max(1, request.args.get("side", type=int) or 1)

    # Samme sted-heuristikk og statusverdier som kurskalenderen (admin_kalender) - se den for begrunnelse.
    vilkar = ["(? = '' OR LOWER(k.navn) LIKE ?)", "(? = 0 OR k.ansvarlig_admin_id = ?)"]
    parametre = [sok.lower(), f"%{sok.lower()}%", 1 if mine else 0, session.get("admin_id")]
    if ansvarlig:
        vilkar.append("k.ansvarlig_admin_id = ?")
        parametre.append(ansvarlig)
    if status:
        vilkar.append("k.status = ?")
        parametre.append(status)
    if sted == "bergen":
        vilkar.append("LOWER(COALESCE(k.sted,'')) LIKE '%bergen%'")
    elif sted == "oslo":
        vilkar.append("LOWER(COALESCE(k.sted,'')) LIKE '%oslo%'")
    elif sted == "online":
        vilkar.append("(k.type IN ('digital','hybrid') OR LOWER(COALESCE(k.sted,'')) LIKE '%zoom%' "
                      "OR LOWER(COALESCE(k.sted,'')) LIKE '%online%')")

    # Uten eksplisitt valgt sortering: aktuelle/fremtidige kurs forst (snarest forst), avsluttede sist.
    order_sql = (f"{AKTIVITET_SORTER[sorter]} {retning.upper()}" if sorter
                else "CASE WHEN k.status='avsluttet' THEN 1 ELSE 0 END ASC, start ASC")
    sql = f"""SELECT k.*, ab.navn AS ansvarlig_navn, MIN(kd.dato) AS start,
                     COALESCE(k.sted, CASE WHEN k.type='digital' THEN 'Online' END) AS sted_visning,
                     (SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=k.id AND p.status='bekreftet') AS bekreftet,
                     (SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=k.id) AS totalt_registrert
              FROM kurs k
              LEFT JOIN kursdag kd ON kd.kurs_id=k.id
              LEFT JOIN admin_bruker ab ON ab.id=k.ansvarlig_admin_id
              WHERE {' AND '.join(vilkar)}
              GROUP BY k.id ORDER BY {order_sql}"""
    rader = con().execute(sql, parametre).fetchall()

    totalt = len(rader)
    siste_side = max(1, -(-totalt // antall))
    side = min(side, siste_side)
    start_i = (side - 1) * antall
    kurs = rader[start_i:start_i + antall]

    def _felles_parametre():
        return dict(sok=sok or None, mine=1 if mine else None, ansvarlig=ansvarlig or None,
                   status=status or None, sted=sted or None)

    def sidelenke(n):
        return url_for("admin_aktiviteter", **_felles_parametre(), antall=antall, sorter=sorter,
                       retning=retning, side=n)

    def sorterlenke(felt, tekst):
        ny_retning = "desc" if sorter == felt and retning == "asc" else "asc"
        pil = (" ↑" if retning == "asc" else " ↓") if sorter == felt else ""
        lenke = url_for("admin_aktiviteter", **_felles_parametre(), antall=antall, sorter=felt, retning=ny_retning)
        return Markup(f'<a href="{lenke}">{Markup.escape(tekst)}{pil}</a>')

    admins = con().execute("SELECT id, navn FROM admin_bruker WHERE aktiv=1 ORDER BY navn").fetchall()
    return render_template(
        "admin_aktiviteter.html", kurs=kurs, sok=sok, mine=mine, ansvarlig=ansvarlig, status=status, sted=sted,
        antall=antall, side=side, totalt=totalt, siste_side=siste_side, sidelenke=sidelenke,
        sorterlenke=sorterlenke, admins=admins, kurs_statusverdier=KURS_STATUSVERDIER,
        nullstill_lenke=url_for("admin_aktiviteter"))


@app.get("/admin/aktiviteter/kalender")
@krever_admin
def admin_kalender():
    idag = date.today()
    aar = request.args.get("aar", type=int) or 0
    maned = request.args.get("maned", type=int) or 0
    if not (1 <= maned <= 12) or not (1 <= aar <= 9999):
        aar, maned = idag.year, idag.month

    ansvarlig = request.args.get("ansvarlig", type=int) or 0
    status = request.args.get("status") or ""
    if status not in KURS_STATUSVERDIER:
        status = ""
    sted = request.args.get("sted") or ""
    if sted not in ("bergen", "oslo", "online"):
        sted = ""

    uker = calendar.Calendar(firstweekday=0).monthdatescalendar(aar, maned)  # mandag = 0

    vilkar = ["kd.dato BETWEEN ? AND ?"]
    parametre = [uker[0][0].isoformat(), uker[-1][-1].isoformat()]
    if ansvarlig:
        vilkar.append("k.ansvarlig_admin_id = ?")
        parametre.append(ansvarlig)
    if status:
        vilkar.append("k.status = ?")
        parametre.append(status)
    if sted == "bergen":
        vilkar.append("LOWER(COALESCE(k.sted,'')) LIKE '%bergen%'")
    elif sted == "oslo":
        vilkar.append("LOWER(COALESCE(k.sted,'')) LIKE '%oslo%'")
    elif sted == "online":
        vilkar.append("(k.type IN ('digital','hybrid') OR LOWER(COALESCE(k.sted,'')) LIKE '%zoom%' "
                      "OR LOWER(COALESCE(k.sted,'')) LIKE '%online%')")

    sql = f"""SELECT kd.dato, k.id, k.kode, k.navn, k.status,
                     COALESCE(k.sted, CASE WHEN k.type='digital' THEN 'Online' END) AS sted_visning
              FROM kursdag kd JOIN kurs k ON k.id=kd.kurs_id
              WHERE {' AND '.join(vilkar)}
              ORDER BY kd.dato, k.navn"""
    per_dag: dict[str, list] = {}
    for rad in con().execute(sql, parametre):
        per_dag.setdefault(rad["dato"], []).append(rad)

    forrige_aar, forrige_maned = (aar - 1, 12) if maned == 1 else (aar, maned - 1)
    neste_aar, neste_maned = (aar + 1, 1) if maned == 12 else (aar, maned + 1)

    def maanedlenke(aar_, maned_):
        return url_for("admin_kalender", aar=aar_, maned=maned_, ansvarlig=ansvarlig or None,
                       status=status or None, sted=sted or None)

    admins = con().execute("SELECT id, navn FROM admin_bruker WHERE aktiv=1 ORDER BY navn").fetchall()
    return render_template(
        "admin_kalender.html", uker=uker, per_dag=per_dag, idag=idag, aar=aar, maned=maned,
        maaned_navn=["", "Januar", "Februar", "Mars", "April", "Mai", "Juni", "Juli", "August",
                    "September", "Oktober", "November", "Desember"][maned],
        forrige_lenke=maanedlenke(forrige_aar, forrige_maned), neste_lenke=maanedlenke(neste_aar, neste_maned),
        nullstill_lenke=url_for("admin_kalender", aar=aar, maned=maned),
        ansvarlig=ansvarlig, status=status, sted=sted, admins=admins, kurs_statusverdier=KURS_STATUSVERDIER)


# ------- kursadministrasjon: faner -------

@app.get("/admin/kurs/<int:kurs_id>")
@krever_admin
def admin_kurs(kurs_id):
    return redirect(url_for("admin_kurs_deltakere", kurs_id=kurs_id))


def _hent_kurs(kurs_id: int):
    return con().execute("SELECT * FROM kurs WHERE id=?", (kurs_id,)).fetchone() or abort(404)


@app.get("/admin/kurs/<int:kurs_id>/oppsett")
@krever_admin
def admin_kurs_oppsett(kurs_id):
    kurs = _hent_kurs(kurs_id)
    dager = db.kursdager(con(), kurs_id)
    filer = sharepoint.list_filer(f"{kurs['sharepoint_mappe']}/Presentasjoner") if kurs["sharepoint_mappe"] else []
    admins = con().execute("SELECT id, navn FROM admin_bruker WHERE aktiv=1 ORDER BY navn").fetchall()
    oppmote_antall = {r["kursdag_id"]: r["antall"] for r in con().execute(
        """SELECT o.kursdag_id, COUNT(*) AS antall FROM oppmote o JOIN kursdag kd ON kd.id=o.kursdag_id
           WHERE kd.kurs_id=? GROUP BY o.kursdag_id""", (kurs_id,))}
    return render_template(
        "admin_kurs_oppsett.html", kurs=kurs, dager=dager, filer=filer, admins=admins,
        oppmote_antall=oppmote_antall, bekreftet_antall=db.antall_bekreftet(con(), kurs_id),
        faktura_laast=db.har_okonomisk_binding_for_kurs(con(), kurs_id),
        kurs_statusvalg=db.KURS_STATUS_OVERGANGER.get(kurs["status"], ()), fane="oppsett")


@app.post("/admin/kurs/<int:kurs_id>/oppsett")
@krever_admin
def admin_kurs_oppsett_lagre(kurs_id):
    kurs = _hent_kurs(kurs_id)
    f = request.form
    feil = []
    if not f.get("navn", "").strip():
        feil.append("Kursnavn kan ikke være tomt.")
    if f.get("type") not in ("fysisk", "digital", "hybrid"):
        feil.append("Ugyldig kurstype.")
    if f.get("fakturering") not in ("person", "organisasjon", "ingen"):
        feil.append("Ugyldig faktureringsvalg.")
    if f.get("betaling") not in ("samlet", "per_samling", "deltaker_velger"):
        feil.append("Ugyldig betalingsvalg.")
    kapasitet = pris_nok = faktura_dager_for = None
    paameldingsfrist = f.get("paameldingsfrist") or None
    try:
        kapasitet = int(f["kapasitet"]) if f.get("kapasitet") else None
        pris_nok = int(f.get("pris_nok") or 0)
        faktura_dager_for = db.valider_faktura_dager_for(int(f.get("faktura_dager_for") or 14))
        if paameldingsfrist:
            date.fromisoformat(paameldingsfrist)
    except ValueError:
        feil.append("Kapasitet, pris og «dager før faktura» må være tall, og fristen en gyldig dato.")
    except db.Paameldingsfeil as e:
        feil.append(str(e))
    if feil:
        for x in feil:
            flash(x, "feil")
        return redirect(url_for("admin_kurs_oppsett", kurs_id=kurs_id))

    felter = {
        "navn": f["navn"].strip(), "type": f["type"], "sted": f.get("sted", "").strip() or None,
        "zoom_url": f.get("zoom_url", "").strip() or None, "pris_nok": pris_nok,
        "fakturering": f["fakturering"], "betaling": f["betaling"], "faktura_dager_for": faktura_dager_for,
        "kursholder_epost": f.get("kursholder_epost", "").strip() or None,
        "notat": f.get("notat", "").strip() or None, "paameldingsfrist": paameldingsfrist,
    }
    if felter["type"] == "fysisk":
        felter["zoom_url"] = felter["zoom_id"] = felter["zoom_pw"] = None

    db.oppdater_kurs_felter(con(), kurs_id, felter, aktor=_aktor())
    if felter["kursholder_epost"] != (kurs["kursholder_epost"] or None):
        db.synk_materiell_ansvarlig(con(), kurs_id, felter["kursholder_epost"], aktor=_aktor())
    opprykket = db.endre_kapasitet(con(), kurs_id, kapasitet, aktor=_aktor()) if kapasitet != kurs["kapasitet"] else []
    con().commit()
    if opprykket:
        sveiper.kjor(Kjoring(con(), idag=_idag()))
        con().commit()
    flash("Kursoppsett oppdatert.", "ok")
    return redirect(url_for("admin_kurs_oppsett", kurs_id=kurs_id))


@app.post("/admin/kurs/<int:kurs_id>/status")
@krever_admin
def admin_kurs_status(kurs_id):
    _hent_kurs(kurs_id)
    try:
        db.sett_kurs_status(con(), kurs_id, request.form.get("status", ""), aktor=_aktor())
        con().commit()
    except db.Paameldingsfeil as e:
        con().rollback()
        flash(str(e), "feil")
        return redirect(url_for("admin_kurs_oppsett", kurs_id=kurs_id))
    flash("Status oppdatert.", "ok")
    return redirect(url_for("admin_kurs_oppsett", kurs_id=kurs_id))


@app.post("/admin/kurs/<int:kurs_id>/avlys")
@krever_admin
def admin_avlys_kurs(kurs_id):
    kurs = _hent_kurs(kurs_id)
    mottakere = con().execute(
        """SELECT p.id, d.navn, d.epost FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id
           WHERE p.kurs_id=? AND p.status IN ('bekreftet','venteliste')""", (kurs_id,)).fetchall()
    k = Kjoring(con(), idag=_idag())
    if kurs["status"] != "avlyst":
        # PREFLIGHT: forhaandsrender avlysningsmeldingen for ALLE mottakere FOER kursstatus endres og committes. Kun rendring -
        # ingen commit, ingen claim, ingen utsending, ingen hendelse. En ugyldig/korrupt redigert mal (MalFeil) skal ikke
        # etterlate et avlyst kurs uten varsler. send_en_gang rendrer og validerer likevel paa nytt foer hver claim.
        try:
            for d in mottakere:
                k.render_for_sending("avlysning", d=d, kurs=kurs)
        except maltekster.MalFeil:
            flash("Kurset ble ikke avlyst fordi avlysningsmeldingen ikke kunne klargjøres. "
                 "Kontroller e-postmalen og prøv igjen.", "feil")
            return redirect(url_for("admin_kurs_oppsett", kurs_id=kurs_id))
    try:
        db.avlys_kurs(con(), kurs_id, aktor=_aktor())
        con().commit()
    except db.Paameldingsfeil as e:
        con().rollback()
        flash(str(e), "feil")
        return redirect(url_for("admin_kurs_oppsett", kurs_id=kurs_id))
    for d in mottakere:
        k.send_en_gang(f"kurs:{kurs_id}", d["epost"], "avlysning", "avlysning", paamelding_id=d["id"], d=d, kurs=kurs)
    con().commit()
    flash(f"Kurset er avlyst og {len(mottakere)} deltaker(e) er varslet på e-post. "
         "Husk å kreditere eventuelle fakturaer manuelt i Visma – det gjøres ikke automatisk.", "ok")
    return redirect(url_for("admin_kurs_oppsett", kurs_id=kurs_id))


@app.post("/admin/kurs/<int:kurs_id>/gjenaapne")
@krever_admin
def admin_gjenaapne_kurs(kurs_id):
    _hent_kurs(kurs_id)
    try:
        db.gjenaapne_kurs(con(), kurs_id, aktor=_aktor())
        con().commit()
    except db.Paameldingsfeil as e:
        con().rollback()
        flash(str(e), "feil")
        return redirect(url_for("admin_kurs_oppsett", kurs_id=kurs_id))
    flash("Kurset er gjenåpnet.", "ok")
    return redirect(url_for("admin_kurs_oppsett", kurs_id=kurs_id))


@app.post("/admin/kurs/<int:kurs_id>/ansvarlig")
@krever_admin
def admin_sett_ansvarlig(kurs_id):
    _hent_kurs(kurs_id)
    verdi = request.form.get("ansvarlig_admin_id") or None
    con().execute("UPDATE kurs SET ansvarlig_admin_id=? WHERE id=?", (int(verdi) if verdi else None, kurs_id))
    db.logg(con(), "kurs_ansvarlig_endret", {"kurs_id": kurs_id, "ansvarlig_admin_id": verdi}, aktor=_aktor())
    con().commit()
    flash("Ansvarlig oppdatert.", "ok")
    return redirect(url_for("admin_kurs_oppsett", kurs_id=kurs_id))


@app.get("/admin/kurs/<int:kurs_id>/nettside")
@krever_admin
def admin_kurs_nettside(kurs_id):
    return render_template("admin_kurs_nettside.html", kurs=_hent_kurs(kurs_id), fane="nettside")


@app.get("/admin/kurs/<int:kurs_id>/forhandsvis-paamelding")
@krever_admin
def admin_forhandsvis_paamelding(kurs_id):
    """READ-ONLY forhaandsvisning av den OFFENTLIGE paameldingssiden (kurs.html) for admin - gjenbruker noyaktig
    samme mal/rendringsvei som kursside() (samme felter, samme betalingsvalg, samme laaste kursinfo), med
    forhandsvisning=True. Ingen ekte deltakerdata vises - kun kursoppsettet. `plasser_igjen` er en telling
    (uendret fra kursside()), ikke deltaker-PII. Fungerer uansett kursstatus (ogsaa 'utkast'), i motsetning til
    den offentlige siden - admin skal kunne forhaandsvise FOER kurset publiseres.

    GET-only: det finnes ingen POST-rute her, saa selv et forsokt skjemainnsending mot denne URL-en (uansett
    hvordan) ville feilet med 405 - det finnes ingen kodesti her som kan opprette en paamelding."""
    kurs = _hent_kurs(kurs_id)
    try:
        skjema, _advarsler = _skjema_snapshot(kurs)   # ingen logging ved visning (12C4 viser advarslene i admin)
    except skjemafelt.SkjemaLesefeil:
        return _skjema_utilgjengelig(kurs, forhandsvisning=True)
    dager = db.kursdager(con(), kurs_id)
    plasser_igjen = None if kurs["kapasitet"] is None else kurs["kapasitet"] - db.antall_bekreftet(con(), kurs_id)
    return _render_paameldingsside(kurs, dager, {}, plasser_igjen, skjema, forhandsvisning=True)


# ---------- adminflate for paameldingsskjemaet (fase 12C4A) ----------
# All validering/normalisering skjer i skjemafelt/db (12C2) - web-laget oversetter KUN det faste admin-skjemaet til
# komplette overstyringer for telefon, arbeidssted og (kun hjelpetekst for) HPR. Andre POST-felt leses aldri, saa laaste
# felt, systemblokker, ukjente felt/egenskaper og HPR-laasene kan ikke naas herfra. Ingen laasing mellom samtidige
# admin-brukere: den som lagrer sist, vinner (hele oppsettet sendes hver gang - ingen skjulte delvise oppdateringer).

_SKJEMA_EGENSKAP_NAVN = {"synlig": "vis feltet", "obligatorisk": "obligatorisk", "rekkefolge": "rekkefølge",
                         "label": "ledetekst", "hjelpetekst": "hjelpetekst"}
_SKJEMA_GRUNN_TEKST = {skjemafelt.UKJENT_FELT: "ukjent felt", skjemafelt.LAAST_FELT: "låst felt",
                       skjemafelt.IKKE_TILLATT: "kan ikke endres for dette feltet",
                       skjemafelt.UGYLDIG_TYPE: "ugyldig verdi", skjemafelt.FOR_LANG: "for lang tekst",
                       skjemafelt.KONTROLLTEGN: "ugyldige tegn", skjemafelt.UTENFOR_OMRAADE: "ugyldig verdi"}
_TELEFON_FORST, _ARBEIDSSTED_FORST = "telefon_forst", "arbeidssted_forst"


def _skjema_advarsel_tekster(advarsler) -> list[str]:
    """PII-fri, lesbar tekst per advarsel: kun kjent felt, kjent egenskap og grunn - aldri lagret innhold/ukjent navn."""
    ut = []
    for a in advarsler:
        felt = skjemafelt.REGISTER[a.felt].label if a.felt in skjemafelt.REGISTER else "Ukjent felt"
        egenskap = f" – {_SKJEMA_EGENSKAP_NAVN[a.egenskap]}" if a.egenskap in _SKJEMA_EGENSKAP_NAVN else ""
        ut.append(f"{felt}{egenskap}: {_SKJEMA_GRUNN_TEKST.get(a.grunn, 'ugyldig verdi')}")
    return ut


def _skjema_admin_verdier(overstyringer) -> dict:
    """Det admin-skjemaet skal vise for dagens oppsett: effektive verdier (standard der ingen overstyring finnes)."""
    verdier = {}
    for n in skjemafelt.KONFIGURERBAR_GRUPPE:
        f, o = skjemafelt.REGISTER[n], overstyringer.get(n) or skjemafelt.Overstyring()
        verdier[n] = {"synlig": f.synlig if o.synlig is None else o.synlig,
                      "obligatorisk": f.obligatorisk if o.obligatorisk is None else o.obligatorisk,
                      "label": o.label or f.label, "hjelpetekst": o.hjelpetekst or "", "tilpasset": not o.er_tom}
    hpr = overstyringer.get("hpr_nr") or skjemafelt.Overstyring()
    verdier["hpr_nr"] = {"hjelpetekst": hpr.hjelpetekst or "", "tilpasset": not hpr.er_tom}

    def plass(n, indeks):
        o = overstyringer.get(n) or skjemafelt.Overstyring()
        return (skjemafelt.REGISTER[n].rekkefolge if o.rekkefolge is None else o.rekkefolge, indeks)
    verdier["rekkefolge"] = (_TELEFON_FORST if plass("telefon", 0) <= plass("arbeidssted", 1) else _ARBEIDSSTED_FORST)
    return verdier


def _skjema_admin_innsendt(form) -> tuple[dict, dict]:
    """Admin-POST -> (KOMPLETTE egenskaper per felt til db.lagre_skjemafelt, verdier til ev. re-rendring). Leser KUN de
    faste navnene i admin-skjemaet. Avmerkingsboks: tilstede = ja. Rekkefolgen settes for BEGGE feltene hver gang
    (lik standard -> normaliseres bort i db), slik at et bytte aldri kan gi et halvt eller inkonsistent oppsett."""
    rekkefolge = form.get("rekkefolge")
    plasser = {"telefon": 1, "arbeidssted": 2} if rekkefolge == _TELEFON_FORST else {"telefon": 2, "arbeidssted": 1}
    egenskaper, verdier = {}, {"rekkefolge": rekkefolge}
    for n in skjemafelt.KONFIGURERBAR_GRUPPE:
        e = {"synlig": f"{n}_synlig" in form, "obligatorisk": f"{n}_obligatorisk" in form,
             "label": form.get(f"{n}_label", ""), "hjelpetekst": form.get(f"{n}_hjelpetekst", "")}
        egenskaper[n] = {**e, "rekkefolge": plasser[n]}
        verdier[n] = {**e, "tilpasset": None}      # None = ukjent (ikke lagret) -> ingen Standard/Tilpasset-merke
    egenskaper["hpr_nr"] = {"hjelpetekst": form.get("hpr_nr_hjelpetekst", "")}
    verdier["hpr_nr"] = {"hjelpetekst": egenskaper["hpr_nr"]["hjelpetekst"], "tilpasset": None}
    return egenskaper, verdier


def _render_skjema_admin(kurs, verdier, advarsler=(), status=200):
    rekkefolge = (["arbeidssted", "telefon"] if verdier["rekkefolge"] == _ARBEIDSSTED_FORST
                  else ["telefon", "arbeidssted"])
    return render_template("admin_kurs_paameldingsskjema.html", kurs=kurs, fane="paameldingsskjema", v=verdier,
                           konfig_rekkefolge=rekkefolge, register=skjemafelt.REGISTER,
                           maks_label=skjemafelt.MAKS_LABEL, maks_hjelpetekst=skjemafelt.MAKS_HJELPETEKST,
                           advarsler=_skjema_advarsel_tekster(advarsler),
                           telefon_forst=_TELEFON_FORST, arbeidssted_forst=_ARBEIDSSTED_FORST), status


def _skjema_admin_utilgjengelig(kurs):
    return render_template("admin_kurs_paameldingsskjema.html", kurs=kurs, fane="paameldingsskjema",
                           lesefeil=True), 503


@app.get("/admin/kurs/<int:kurs_id>/paameldingsskjema")
@krever_admin
def admin_kurs_paameldingsskjema(kurs_id):
    """Viser skjemaoppsettet. Kun lesing - skriver aldri noe (heller ikke advarsler). Fail-closed ved lesefeil: da vises
    IKKE et redigerbart standardskjema."""
    kurs = _hent_kurs(kurs_id)
    try:
        les = db.hent_skjemaoverstyringer(con(), kurs_id)
    except skjemafelt.SkjemaLesefeil:
        return _skjema_admin_utilgjengelig(kurs)
    return _render_skjema_admin(kurs, _skjema_admin_verdier(les.overstyringer), les.advarsler)


@app.post("/admin/kurs/<int:kurs_id>/paameldingsskjema")
@krever_admin
def admin_kurs_paameldingsskjema_lagre(kurs_id):
    """Lagrer HELE oppsettet atomisk: alle tre feltene i EN transaksjon. En ugyldig verdi (SkjemafeltFeil fra 12C2-
    valideringen) eller DB-feil ruller tilbake alt - aldri en halv oppdatering. Lagrer ingenting hvis dagens oppsett
    ikke kan leses."""
    kurs = _hent_kurs(kurs_id)
    try:
        les = db.hent_skjemaoverstyringer(con(), kurs_id)
    except skjemafelt.SkjemaLesefeil:
        return _skjema_admin_utilgjengelig(kurs)
    egenskaper, verdier = _skjema_admin_innsendt(request.form)
    if verdier["rekkefolge"] not in (_TELEFON_FORST, _ARBEIDSSTED_FORST):
        flash("Velg rekkefølge for telefon og arbeidssted.", "feil")
        verdier["rekkefolge"] = _TELEFON_FORST
        return _render_skjema_admin(kurs, verdier, les.advarsler, 400)
    try:
        with db.transaksjon(con()):
            endret = [db.lagre_skjemafelt(con(), kurs_id, felt, e, aktor=_aktor()) for felt, e in egenskaper.items()]
    except skjemafelt.SkjemafeltFeil as e:
        navn = skjemafelt.REGISTER[e.felt].label if e.felt in skjemafelt.REGISTER else "Skjemaet"
        flash(f"{navn}: {e.forklaring or 'Ugyldig verdi.'} Ingen endringer er lagret.", "feil")
        return _render_skjema_admin(kurs, verdier, les.advarsler, 400)
    except sqlite3.Error:
        return _skjema_admin_utilgjengelig(kurs)
    flash("Påmeldingsskjemaet er lagret." if any(endret) else "Ingen endringer å lagre.", "ok")
    return redirect(url_for("admin_kurs_paameldingsskjema", kurs_id=kurs_id))


@app.post("/admin/kurs/<int:kurs_id>/paameldingsskjema/tilbakestill")
@krever_admin
def admin_kurs_paameldingsskjema_tilbakestill(kurs_id):
    """Fjerner ALLE skjemaoverstyringer for kurset (ogsaa korrupte/ukjente rader) -> kodet standardskjema."""
    kurs = _hent_kurs(kurs_id)
    try:
        with db.transaksjon(con()):
            antall = db.tilbakestill_skjema(con(), kurs_id, aktor=_aktor())
    except sqlite3.Error:
        return _skjema_admin_utilgjengelig(kurs)
    flash("Påmeldingsskjemaet er tilbakestilt til standard." if antall else "Skjemaet bruker allerede standardoppsettet.",
          "ok")
    return redirect(url_for("admin_kurs_paameldingsskjema", kurs_id=kurs_id))


def _deltaker_sok_status(request_args) -> tuple[str, str]:
    """Leser/validerer sok+status-filter fra query-parametre. Delt mellom deltakerlisten og
    CSV-eksporten, slik at eksporten kan folge NOYAKTIG samme filter som det admin ser paa
    skjermen - uten aa duplisere valideringsregelen flere steder."""
    sok = request_args.get("sok", "").strip()
    status = request_args.get("status", "")
    if status not in ("", "bekreftet", "venteliste", "avmeldt"):
        status = ""
    return sok, status


def _deltaker_filter_vilkar(sok: str, status: str) -> tuple[str, list]:
    vilkar_sql, args = "", []
    if sok:
        vilkar_sql += " AND (LOWER(d.navn) LIKE ? OR LOWER(d.epost) LIKE ?)"
        args += [f"%{sok.lower()}%", f"%{sok.lower()}%"]
    if status:
        vilkar_sql += " AND p.status=?"
        args.append(status)
    return vilkar_sql, args


@app.get("/admin/kurs/<int:kurs_id>/deltakere")
@krever_admin
def admin_kurs_deltakere(kurs_id):
    kurs = _hent_kurs(kurs_id)
    dager = db.kursdager(con(), kurs_id)
    sok, status = _deltaker_sok_status(request.args)

    sql = """SELECT p.*, d.navn, d.epost, d.telefon, d.arbeidssted,
                    (SELECT COUNT(*) FROM faktura WHERE paamelding_id=p.id) AS faktura_antall,
                    (SELECT COUNT(*) FROM faktura WHERE paamelding_id=p.id AND status!='betalt') AS faktura_ubetalt
             FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id
             WHERE p.kurs_id=?"""
    vilkar_sql, vilkar_args = _deltaker_filter_vilkar(sok, status)
    args = [kurs_id, *vilkar_args]
    sql += vilkar_sql + " ORDER BY CASE p.status WHEN 'bekreftet' THEN 0 WHEN 'venteliste' THEN 1 ELSE 2 END, d.navn"
    deltakere = [dict(r, fakturastatus=_fakturastatus(kurs, r)) for r in con().execute(sql, args)]

    tellere = {r["status"]: r["antall"] for r in con().execute(
        "SELECT status, COUNT(*) AS antall FROM paamelding WHERE kurs_id=? GROUP BY status", (kurs_id,))}
    oppmote = {(r["paamelding_id"], r["kursdag_id"]): r["kilde"] for r in con().execute(
        "SELECT o.* FROM oppmote o JOIN kursdag kd ON kd.id=o.kursdag_id WHERE kd.kurs_id=?", (kurs_id,))}
    return render_template("admin_kurs_deltakere.html", kurs=kurs, dager=dager, deltakere=deltakere,
                           tellere=tellere, totalt=sum(tellere.values()), oppmote=oppmote,
                           sok=sok, status=status, fane="deltakere")


@app.route("/admin/kurs/<int:kurs_id>/deltaker/ny", methods=["GET", "POST"])
@krever_admin
def admin_deltaker_ny(kurs_id):
    """Manuell paamelding (fase 9) - for deltakere som melder seg paa via telefon/e-post i stedet
    for nettskjemaet. Bruker db.meld_paa() ubeskaaret (samme datamodell/logikk/dublett-haandtering
    som offentlig paamelding), bare med aktor/tillat_utkast/ignorer_frist satt for administrativ bruk.

    sveiper_utsatt=1 settes ALLTID her, uansett kursstatus: ingen bekreftelse/faktura skal sendes
    automatisk foer en administrator eksplisitt utloeser det (trinn 3) - ogsaa hvis kurset senere
    gaar fra utkast til aapent uten at noen har trykket paa den knappen.
    """
    kurs = _hent_kurs(kurs_id)
    dager = db.kursdager(con(), kurs_id)
    plasser_igjen = None if kurs["kapasitet"] is None else kurs["kapasitet"] - db.antall_bekreftet(con(), kurs_id)
    if request.method == "GET":
        return render_template("admin_deltaker_ny.html", kurs=kurs, dager=dager, f={},
                               plasser_igjen=plasser_igjen, fane="deltakere")

    f = request.form
    feil = []
    if not f.get("navn", "").strip() or "@" not in f.get("epost", ""):
        feil.append("Fyll inn navn og en gyldig e-postadresse.")
    org = f.get("betaler") == "organisasjon"
    if org and not (f.get("org_navn", "").strip() and f.get("org_nr", "").strip()):
        feil.append("Fyll inn firmanavn og organisasjonsnummer når arbeidsgiver betaler.")
    if feil:
        for x in feil:
            flash(x, "feil")
        return render_template("admin_deltaker_ny.html", kurs=kurs, dager=dager, f=f,
                               plasser_igjen=plasser_igjen, fane="deltakere"), 400

    try:
        with db.transaksjon(con()):
            pid, status = db.meld_paa(
                con(), kurs_id, epost=f["epost"], navn=f["navn"],
                deltaker={"telefon": f.get("telefon"), "arbeidssted": f.get("arbeidssted"),
                         "hpr_nr": f.get("hpr_nr"), "yrkestittel": f.get("yrkestittel")},
                paamelding={k: f.get(k) or None for k in (
                    "org_navn", "org_nr", "faktura_epost", "faktura_ref", "faktura_adresse",
                    "faktura_postnr", "faktura_sted", "faktura_kommentar")}
                | {"betaler": "organisasjon" if org else "person", "ehf": 1 if f.get("ehf") else 0,
                   "betaling": f.get("betaling", "samlet"), "kilde": "admin", "sveiper_utsatt": 1},
                sensitivt={"allergier": f.get("allergier"), "tilrettelegging": f.get("tilrettelegging")},
                idag=_idag(), aktor=_aktor(), tillat_utkast=True, ignorer_frist=True,
            )
    except db.Paameldingsfeil as e:
        flash(str(e), "feil")
        return render_template("admin_deltaker_ny.html", kurs=kurs, dager=dager, f=f,
                               plasser_igjen=plasser_igjen, fane="deltakere"), 400

    flash(f"{f['navn']} er registrert og satt til «{status}». "
         "Ingen bekreftelse eller faktura er sendt ennå.", "ok")
    return redirect(url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=pid))


# Marg utover selve fil-taket (import_deltakere.MAKS_FILSTORRELSE) for multipart-hodefelt/grense -
# satt KUN paa denne ruten (request.max_content_length er en per-request-egenskap), IKKE som en
# global app.config-endring som ville paavirket andre opplastingsruter (f.eks. /lever/<id>).
MAKS_REQUEST_BYTES_IMPORT = import_deltakere.MAKS_FILSTORRELSE + 64 * 1024


def _tall(antall: int, entall: str, flertall: str) -> str:
    """Enkel entall/flertall-hjelper til flash-tekster - f.eks. _tall(1, "rad", "rader")."""
    return f"{antall} {entall if antall == 1 else flertall}"


@app.route("/admin/kurs/<int:kurs_id>/deltakere/importer", methods=["GET", "POST"])
@krever_admin
def admin_import_deltakere(kurs_id):
    """Fase 10, trinn 2: last opp CSV -> forhaandsvis. IKKE importer direkte - se
    import_deltakere.py for hvorfor (forhaandsvisningen kjorer ekte db.meld_paa()-logikk i en
    SAVEPOINT som alltid rulles tilbake, og resultatet lagres server-side bak et ugjennomsiktig
    token - aldri i en signert cookie/skjult felt med persondata). Selve importen (trinn 3) er
    IKKE bygget ennaa.
    """
    kurs = _hent_kurs(kurs_id)
    if request.method == "GET":
        return render_template("admin_import_deltakere.html", kurs=kurs, fane="deltakere")

    # Settes FOR forste tilgang til request.files/.form, slik at en for stor request avvises av
    # Werkzeug for hele kroppen er lest inn - route-spesifikk, ikke en global grense.
    request.max_content_length = MAKS_REQUEST_BYTES_IMPORT
    try:
        fil = request.files.get("fil")
    except RequestEntityTooLarge:
        flash(f"Filen/forespørselen er for stor (maks "
             f"{import_deltakere.MAKS_FILSTORRELSE // (1024 * 1024)} MB).", "feil")
        return render_template("admin_import_deltakere.html", kurs=kurs, fane="deltakere"), 413

    if not fil or not fil.filename:
        flash("Velg en CSV-fil.", "feil")
        return render_template("admin_import_deltakere.html", kurs=kurs, fane="deltakere"), 400
    if not fil.filename.lower().endswith(".csv"):
        flash("Bare CSV-filer støttes.", "feil")
        return render_template("admin_import_deltakere.html", kurs=kurs, fane="deltakere"), 400

    # Les aldri mer enn taket + 1 byte inn i minnet - uansett hva Content-Length paastaar.
    raw = fil.stream.read(import_deltakere.MAKS_FILSTORRELSE + 1)
    if len(raw) > import_deltakere.MAKS_FILSTORRELSE:
        flash(f"Filen er for stor (maks {import_deltakere.MAKS_FILSTORRELSE // (1024 * 1024)} MB).", "feil")
        return render_template("admin_import_deltakere.html", kurs=kurs, fane="deltakere"), 400

    try:
        rader = import_deltakere.parse_csv(raw)
    except import_deltakere.ImportFeil as e:
        flash(str(e), "feil")
        return render_template("admin_import_deltakere.html", kurs=kurs, fane="deltakere"), 400

    try:
        resultater = import_deltakere.forhaandsvis(con(), kurs_id, rader, aktor=_aktor())
    except import_deltakere.ImportFeil as e:
        flash(str(e), "feil")
        return render_template("admin_import_deltakere.html", kurs=kurs, fane="deltakere"), 400

    token = None
    if not import_deltakere.har_blokkerende_rader(resultater):
        import_deltakere.rydd_utlopte_forhaandsvisninger(con())
        import_deltakere.slett_forhaandsvisninger_for(con(), kurs_id, session["admin_id"])
        token = import_deltakere.lagre_forhaandsvisning(con(), kurs_id, session["admin_id"], rader, resultater)
        con().commit()

    return render_template(
        "admin_import_forhaandsvisning.html", kurs=kurs, resultater=resultater, token=token,
        antall=len(rader), har_blokkerende=import_deltakere.har_blokkerende_rader(resultater), fane="deltakere")


@app.post("/admin/kurs/<int:kurs_id>/deltakere/importer/bekreft")
@krever_admin
def admin_import_deltakere_bekreft(kurs_id):
    """Fase 10, trinn 3: utforer selve importen. POST-only (GET skal ikke kunne importere noe).
    Mottar KUN det ugjennomsiktige preview-tokenet - aldri persondata i skjemaet. Revaliderer
    ALLTID mot fersk databasetilstand (import_deltakere.importer()) for aa skrive noe."""
    _hent_kurs(kurs_id)
    token = request.form.get("forhaandsvisning_token", "")
    try:
        innhold = import_deltakere.hent_forhaandsvisning(con(), token, kurs_id, session["admin_id"])
    except import_deltakere.TokenFeil as e:
        flash(str(e), "feil")
        return redirect(url_for("admin_import_deltakere", kurs_id=kurs_id))

    try:
        resultat = import_deltakere.importer(
            con(), kurs_id, innhold["rader"], innhold["resultater"], aktor=_aktor())
    except import_deltakere.EndretTilstandFeil as e:
        # Forholdene i kurset er endret siden forhaandsvisningen - IKKE en teknisk feil. Previewen
        # er ikke lenger til aa stole paa og ugyldiggjores; admin maa forhaandsvise paa nytt.
        # Selve avviks-teksten (kan inneholde e-post) vises ALDRI - kun et antall, ingen persondata.
        import_deltakere.slett_forhaandsvisning(con(), token)
        con().commit()
        flash(f"Forholdene i kurset har endret seg siden forhåndsvisningen "
             f"({_tall(len(e.avvik), 'rad berørt', 'rader berørt')} - f.eks. kapasitet, status "
             "eller en annen påmelding). Ingenting er importert. Last opp filen på nytt for en "
             "oppdatert forhåndsvisning.", "feil")
        return redirect(url_for("admin_import_deltakere", kurs_id=kurs_id))
    except import_deltakere.ImportFeil as e:
        # F.eks. kurset ble avlyst/avsluttet etter forhaandsvisningen - samme prinsipp: ugyldiggjor
        # previewen fremfor aa la den staa igjen som om den fortsatt var gyldig.
        import_deltakere.slett_forhaandsvisning(con(), token)
        con().commit()
        flash(str(e), "feil")
        return redirect(url_for("admin_import_deltakere", kurs_id=kurs_id))
    # Andre (uventede) exceptions fanges bevisst IKKE her: db.transaksjon() har allerede rullet
    # alt tilbake, og vi lar previewen staa (ikke slettet) slik at admin kan forsoke igjen innen
    # levetiden uten aa laste opp filen paa nytt. Flask sin standard feilhaandtering tar over.

    import_deltakere.slett_forhaandsvisning(con(), token)
    con().commit()
    t = import_deltakere.oppsummer(resultat)
    melding = (f"Import fullført: {_tall(t['antall_importert'], 'deltaker registrert', 'deltakere registrert')} "
              f"({t['antall_bekreftet']} bekreftet, {t['antall_venteliste']} venteliste)")
    if t["antall_reaktivert"]:
        melding += f", {t['antall_reaktivert']} reaktivert"
    if t["antall_hoppet_over"]:
        melding += f", {t['antall_hoppet_over']} allerede påmeldt hoppet over"
    flash(melding + ".", "ok")
    return redirect(url_for("admin_kurs_deltakere", kurs_id=kurs_id))


@app.get("/admin/deltaker-import-mal.csv")
@krever_admin
def admin_deltaker_import_mal():
    return Response("﻿" + import_deltakere.malfil_csv(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=deltaker-import-mal.csv"})


def _hent_paamelding(kurs_id: int, paamelding_id: int):
    return con().execute(
        """SELECT p.*, d.navn, d.epost, d.telefon, d.yrkestittel, d.arbeidssted,
                  s.allergier, s.tilrettelegging,
                  (SELECT COUNT(*) FROM faktura WHERE paamelding_id=p.id) AS faktura_antall,
                  (SELECT COUNT(*) FROM faktura WHERE paamelding_id=p.id AND status!='betalt') AS faktura_ubetalt
           FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id
           LEFT JOIN sensitivt s ON s.paamelding_id=p.id
           WHERE p.id=? AND p.kurs_id=?""", (paamelding_id, kurs_id)).fetchone() or abort(404)


# Hvilke statusoverganger vi tilbyr fra adminpanelet. Bekreftet -> venteliste er bevisst utelatt:
# den ville tatt en bekreftet plass uten aa automatisk tilby den videre (bruk avmelding i stedet).
STATUS_OVERGANGER = {
    "bekreftet": ("avmeldt",),
    "venteliste": ("bekreftet", "avmeldt"),
    "avmeldt": ("bekreftet", "venteliste"),
}


@app.get("/admin/kurs/<int:kurs_id>/deltaker/<int:paamelding_id>")
@krever_admin
def admin_deltaker(kurs_id, paamelding_id):
    kurs = _hent_kurs(kurs_id)
    p = _hent_paamelding(kurs_id, paamelding_id)
    db.logg(con(), "sensitivt_vist", {"paamelding_id": paamelding_id, "kurs_id": kurs_id}, aktor=_aktor())
    con().commit()
    andre_paameldinger = con().execute(
        "SELECT COUNT(*) FROM paamelding WHERE deltaker_id=? AND id!=? AND status!='avmeldt'",
        (p["deltaker_id"], paamelding_id)).fetchone()[0]
    prisvalg_redigerbar = kurs["betaling"] == "deltaker_velger" and p["faktura_antall"] == 0
    return render_template(
        "admin_deltaker.html", kurs=kurs, p=p, fakturastatus=_fakturastatus(kurs, p), fane="deltaker",
        andre_paameldinger=andre_paameldinger, prisvalg_redigerbar=prisvalg_redigerbar,
        statusvalg=STATUS_OVERGANGER.get(p["status"], ()))


@app.post("/admin/kurs/<int:kurs_id>/deltaker/<int:paamelding_id>/person")
@krever_admin
def admin_deltaker_person(kurs_id, paamelding_id):
    _hent_kurs(kurs_id)
    p = _hent_paamelding(kurs_id, paamelding_id)
    f = request.form
    if not f.get("navn", "").strip() or "@" not in f.get("epost", ""):
        flash("Fyll inn navn og en gyldig e-postadresse.", "feil")
        return redirect(url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id))
    try:
        db.oppdater_deltaker(con(), p["deltaker_id"], {
            "navn": f["navn"].strip(), "epost": f["epost"].strip(),
            "telefon": f.get("telefon", "").strip() or None,
            "yrkestittel": f.get("yrkestittel", "").strip() or None,
            "arbeidssted": f.get("arbeidssted", "").strip() or None,
        }, aktor=_aktor())
        con().commit()
    except db.DeltakerFeil as e:
        con().rollback()
        flash(str(e), "feil")
        return redirect(url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id))
    flash("Personopplysninger oppdatert. Gjelder alle kurs vedkommende er meldt på.", "ok")
    return redirect(url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id))


@app.post("/admin/kurs/<int:kurs_id>/deltaker/<int:paamelding_id>/paamelding")
@krever_admin
def admin_deltaker_paamelding(kurs_id, paamelding_id):
    kurs = _hent_kurs(kurs_id)
    p = _hent_paamelding(kurs_id, paamelding_id)
    f = request.form
    betaler = f.get("betaler") if f.get("betaler") in ("person", "organisasjon") else p["betaler"]
    if betaler == "organisasjon" and not (f.get("org_navn", "").strip() and f.get("org_nr", "").strip()):
        flash("Fyll inn firmanavn og organisasjonsnummer når firma betaler.", "feil")
        return redirect(url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id))

    felter = {
        "betaler": betaler,
        "org_navn": f.get("org_navn", "").strip() or None,
        "org_nr": f.get("org_nr", "").strip() or None,
        "faktura_adresse": f.get("faktura_adresse", "").strip() or None,
        "faktura_postnr": f.get("faktura_postnr", "").strip() or None,
        "faktura_sted": f.get("faktura_sted", "").strip() or None,
        "faktura_ref": f.get("faktura_ref", "").strip() or None,
        "faktura_kommentar": f.get("faktura_kommentar", "").strip() or None,
        "intern_kommentar": f.get("intern_kommentar", "").strip() or None,
    }
    # Prisvalg kan kun endres naar kurset lar deltakeren velge OG ingenting er fakturert ennaa,
    # selv om noen skulle poste feltet uansett (samme sjekk som avgjor om det vises redigerbart).
    if kurs["betaling"] == "deltaker_velger" and p["faktura_antall"] == 0 and f.get("betaling") in ("samlet", "per_samling"):
        felter["betaling"] = f["betaling"]

    db.oppdater_paamelding(con(), paamelding_id, felter, aktor=_aktor())
    if kurs["type"] != "digital":
        db.oppdater_sensitivt(con(), paamelding_id, f.get("allergier"), f.get("tilrettelegging"), aktor=_aktor())
    con().commit()
    flash("Påmeldingen er oppdatert.", "ok")
    return redirect(url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id))


@app.post("/admin/kurs/<int:kurs_id>/deltaker/<int:paamelding_id>/status")
@krever_admin
def admin_deltaker_status(kurs_id, paamelding_id):
    _hent_kurs(kurs_id)
    p = _hent_paamelding(kurs_id, paamelding_id)
    ny_status = request.form.get("status", "")
    if ny_status not in STATUS_OVERGANGER.get(p["status"], ()):
        flash("Ugyldig statusendring.", "feil")
        return redirect(url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id))
    try:
        kjor_sveiper_for = db.sett_paamelding_status(con(), paamelding_id, ny_status, aktor=_aktor())
        con().commit()
    except db.Paameldingsfeil as e:
        con().rollback()
        flash(str(e), "feil")
        return redirect(url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id))
    if kjor_sveiper_for:
        sveiper.kjor(Kjoring(con(), idag=_idag()), kjor_sveiper_for)
        con().commit()
    flash(f"Status endret til «{ny_status}».", "ok")
    return redirect(url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id))


@app.post("/admin/kurs/<int:kurs_id>/deltaker/<int:paamelding_id>/behandle")
@krever_admin
def admin_deltaker_behandle(kurs_id, paamelding_id):
    """Fase 9, trinn 3: utloeser den e-post/fakturering som bevisst ble holdt tilbake
    (sveiper_utsatt=1) ved manuell registrering. All revalidering, behandling (eksisterende sveiper-motor)
    og tolkning av utfallet ligger i behandling.behandle_holdt_paamelding() - samme funksjon som
    bulk-behandlingen (fase 11) bruker, slik at enkelt- og bulkbehandling aldri tolker en tilstand ulikt.
    Ruten her gjor kun 404 for feil kurs, kall, og oversettelse av resultatet til flash-tekst.
    POST-only (GET skal ikke kunne utlose noe). utkast/avlyst blokkeres i klassifiseringen, foer noe roeres.
    """
    kurs = _hent_kurs(kurs_id)
    p = _hent_paamelding(kurs_id, paamelding_id)  # 404 hvis paameldingen ikke hoerer til dette kurset
    res = behandling.behandle_holdt_paamelding(con(), kurs_id, paamelding_id, _idag(), aktor=_aktor())
    tilbake = redirect(url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id))

    if not res.forsokt:  # avvist foer motoren ble kalt - ingenting er rort
        if res.arsak_kode == "kurs_utkast":
            flash("Kurset er ikke publisert ennå. Behandlingen kan først utløses når kurset er åpnet.", "feil")
        elif res.arsak_kode == "kurs_avlyst":
            flash("Kurset er avlyst og kan ikke behandles.", "feil")
        else:
            flash("Denne påmeldingen er ikke lenger holdt tilbake (allerede behandlet, eller under "
                 "automatisk gjenoppretting).", "feil")
    elif res.kode == behandling.FULLFORT and p["status"] == "bekreftet":
        flash("Bekreftelse er sendt og fakturering er behandlet.", "ok")
    elif res.kode == behandling.FULLFORT:
        flash("Ventelistebeskjed er sendt.", "ok")
    elif res.kode == behandling.UAVKLART:
        flash("Behandlingen er ikke fullført: en e-post- eller fakturaoperasjon er uavklart eller pågår i en "
             "annen prosess. Systemet sender/fakturerer IKKE automatisk på nytt når utfallet er uklart – "
             "dette må kontrolleres manuelt (se «Uavklarte operasjoner» på forsiden).", "feil")
    else:
        flash("Behandlingen ble ikke fullført (feil ved sending eller fakturering). Den fanges "
             "opp automatisk igjen ved neste daglige kjøring.", "feil")
    return tilbake


@app.get("/admin/kurs/<int:kurs_id>/deltaker/<int:paamelding_id>/kommunikasjon")
@krever_admin
def admin_deltaker_kommunikasjon(kurs_id, paamelding_id):
    kurs = _hent_kurs(kurs_id)
    p = _hent_paamelding(kurs_id, paamelding_id)
    meldinger = con().execute(
        """SELECT ul.type, ul.sendt_ts, au.id AS utsending_id, au.emne FROM utsending_logg ul
           LEFT JOIN admin_utsending au ON au.nokkel=ul.nokkel
           WHERE ul.mottaker=? AND (ul.nokkel=? OR ul.nokkel LIKE ?) AND ul.status='sendt'
           ORDER BY ul.sendt_ts DESC""",
        (p["epost"], f"kurs:{kurs_id}", f"adhoc:{kurs_id}:%")).fetchall()
    return render_template("admin_deltaker_kommunikasjon.html", kurs=kurs, p=p, meldinger=meldinger, fane="kommunikasjon")


@app.get("/admin/kurs/<int:kurs_id>/deltaker/<int:paamelding_id>/logger")
@krever_admin
def admin_deltaker_logger(kurs_id, paamelding_id):
    kurs = _hent_kurs(kurs_id)
    p = _hent_paamelding(kurs_id, paamelding_id)
    prefiks = f'%"paamelding_id": {paamelding_id}'
    hendelser = con().execute(
        "SELECT * FROM hendelse WHERE detaljer LIKE ? OR detaljer LIKE ? ORDER BY id DESC",
        (prefiks + ",%", prefiks + "}%")).fetchall()
    return render_template("admin_deltaker_logger.html", kurs=kurs, p=p, hendelser=hendelser, fane="logger")


# ------- admin: manuell e-post (fase 5) -------

EPOST_EMNE_MAKS = 200
EPOST_TEKST_MAKS = 5000


def _hent_epost_mottakere(kurs_id: int, ider: list[int]) -> list[sqlite3.Row]:
    """Validerer mottaker-ID-er MOT DATABASEN - stoler aldri paa at ID-ene fra skjemaet er gyldige/
    hoerer til dette kurset. Ukjente/fremmede ID-er faller bare bort."""
    if not ider:
        return []
    plassholdere = ",".join("?" * len(ider))
    return con().execute(
        f"""SELECT p.id, d.navn, d.epost FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id
            WHERE p.kurs_id=? AND p.id IN ({plassholdere}) ORDER BY d.navn""",
        (kurs_id, *ider)).fetchall()


@app.get("/admin/kurs/<int:kurs_id>/epost/ny")
@krever_admin
def admin_epost_ny(kurs_id):
    kurs = _hent_kurs(kurs_id)
    gruppe = request.args.get("gruppe")
    if gruppe in ("bekreftet", "venteliste"):
        ider = [r["id"] for r in con().execute(
            "SELECT id FROM paamelding WHERE kurs_id=? AND status=?", (kurs_id, gruppe))]
    else:
        ider = request.args.getlist("paamelding_id", type=int)
    mottakere = _hent_epost_mottakere(kurs_id, ider)
    if not mottakere:
        flash("Velg minst én mottaker.", "feil")
        return redirect(url_for("admin_kurs_deltakere", kurs_id=kurs_id))
    return render_template("admin_epost_ny.html", kurs=kurs, mottakere=mottakere, emne="", tekst="",
                           emne_maks=EPOST_EMNE_MAKS, tekst_maks=EPOST_TEKST_MAKS, forhandsvisning=None)


@app.post("/admin/kurs/<int:kurs_id>/epost/forhandsvis")
@krever_admin
def admin_epost_forhandsvis(kurs_id):
    kurs = _hent_kurs(kurs_id)
    emne = request.form.get("emne", "").strip()
    tekst = request.form.get("tekst", "").strip()
    mottakere = _hent_epost_mottakere(kurs_id, request.form.getlist("paamelding_id", type=int))

    feil = []
    if not emne:
        feil.append("Fyll inn et emne.")
    elif len(emne) > EPOST_EMNE_MAKS:
        feil.append(f"Emnet kan være maks {EPOST_EMNE_MAKS} tegn (er nå {len(emne)}).")
    elif epost.har_kontrolltegn(emne):  # emnet er ett linjeskiftfritt e-postemne (ingen ekstra hoder)
        feil.append("Emnet kan ikke inneholde linjeskift.")
    if not tekst:
        feil.append("Fyll inn en melding.")
    elif len(tekst) > EPOST_TEKST_MAKS:
        feil.append(f"Meldingen kan være maks {EPOST_TEKST_MAKS} tegn (er nå {len(tekst)}).")
    if not mottakere:
        feil.append("Velg minst én gyldig mottaker.")
    if feil:
        for x in feil:
            flash(x, "feil")
        return render_template("admin_epost_ny.html", kurs=kurs, mottakere=mottakere, emne=emne, tekst=tekst,
                               emne_maks=EPOST_EMNE_MAKS, tekst_maks=EPOST_TEKST_MAKS, forhandsvisning=None)

    utsending_id, _ = db.opprett_admin_utsending(
        con(), kurs_id, emne, tekst, [m["id"] for m in mottakere], sendt_av_admin_id=session.get("admin_id"))
    con().commit()
    eksempel_id = request.form.get("eksempel_paamelding_id", type=int)
    eksempel = next((m for m in mottakere if m["id"] == eksempel_id), mottakere[0])
    _, eksempel_html = epost.render("admin_melding", emne=emne, tekst=tekst, d=eksempel)
    return render_template("admin_epost_ny.html", kurs=kurs, mottakere=mottakere, emne=emne, tekst=tekst,
                           emne_maks=EPOST_EMNE_MAKS, tekst_maks=EPOST_TEKST_MAKS,
                           forhandsvisning={"utsending_id": utsending_id, "html": eksempel_html,
                                            "eksempel_navn": eksempel["navn"], "eksempel_id": eksempel["id"]})


@app.post("/admin/kurs/<int:kurs_id>/epost/send")
@krever_admin
def admin_epost_send(kurs_id):
    _hent_kurs(kurs_id)
    utsending_id = request.form.get("utsending_id", type=int)
    rad = con().execute("SELECT id FROM admin_utsending WHERE id=? AND kurs_id=?", (utsending_id, kurs_id)).fetchone()
    if not rad:
        flash("Fant ikke utsendelsen. Forhåndsvis på nytt.", "feil")
        return redirect(url_for("admin_kurs_deltakere", kurs_id=kurs_id))
    sendt_til = Kjoring(con(), idag=_idag()).send_admin_utsending(utsending_id)
    for pid in sendt_til:
        db.logg(con(), "admin_epost_sendt", {"paamelding_id": pid, "kurs_id": kurs_id}, aktor=_aktor())
    con().commit()
    if sendt_til:
        flash(f"E-post sendt til {len(sendt_til)} mottaker(e).", "ok")
    else:
        flash("Denne utsendelsen er allerede sendt – ingenting ble sendt på nytt.", "info")
    return redirect(url_for("admin_kurs_kommunikasjon", kurs_id=kurs_id))


@app.get("/admin/kurs/<int:kurs_id>/epost/<int:utsending_id>")
@krever_admin
def admin_epost_detalj(kurs_id, utsending_id):
    kurs = _hent_kurs(kurs_id)
    utsending = con().execute(
        """SELECT au.*, ab.navn AS sendt_av_navn FROM admin_utsending au
           LEFT JOIN admin_bruker ab ON ab.id=au.sendt_av_admin_id
           WHERE au.id=? AND au.kurs_id=?""", (utsending_id, kurs_id)).fetchone() or abort(404)
    if not con().execute("SELECT 1 FROM utsending_logg WHERE nokkel=? AND status='sendt'", (utsending["nokkel"],)).fetchone():
        abort(404)  # aldri faktisk sendt (kun forhaandsvist) - ikke vis den som om den var det
    mottakere = db.admin_utsending_mottakere(con(), utsending_id)
    return render_template("admin_epost_detalj.html", kurs=kurs, utsending=utsending, mottakere=mottakere)


@app.get("/admin/kurs/<int:kurs_id>/kommunikasjon")
@krever_admin
def admin_kurs_kommunikasjon(kurs_id):
    kurs = _hent_kurs(kurs_id)
    meldinger = con().execute(
        "SELECT * FROM utsending_logg WHERE nokkel=? AND status='sendt' ORDER BY sendt_ts DESC LIMIT 300",
        (f"kurs:{kurs_id}",)
    ).fetchall()
    manuelle = con().execute(
        """SELECT au.id, au.emne, au.opprettet, ab.navn AS sendt_av_navn, COUNT(ul.mottaker) AS antall_sendt
           FROM admin_utsending au JOIN utsending_logg ul ON ul.nokkel=au.nokkel
           LEFT JOIN admin_bruker ab ON ab.id=au.sendt_av_admin_id
           WHERE au.kurs_id=? AND ul.status='sendt' GROUP BY au.id ORDER BY au.opprettet DESC""", (kurs_id,)).fetchall()
    return render_template("admin_kurs_kommunikasjon.html", kurs=kurs, meldinger=meldinger, manuelle=manuelle,
                           fane="kommunikasjon")


@app.post("/admin/kurs/<int:kurs_id>/oppmote")
@krever_admin
def admin_oppmote(kurs_id):
    pid, kdid = int(request.form["paamelding_id"]), int(request.form["kursdag_id"])
    if not db.registrer_oppmote(con(), pid, kdid, "manuell"):
        con().execute("DELETE FROM oppmote WHERE paamelding_id=? AND kursdag_id=?", (pid, kdid))
    db.logg(con(), "oppmote_manuell", {"paamelding_id": pid, "kursdag_id": kdid}, aktor=_aktor())
    con().commit()
    return redirect(url_for("admin_kurs_deltakere", kurs_id=kurs_id) + "#oppmote")


@app.post("/admin/paamelding/<int:pid>/meld-av")
@krever_admin
def admin_meld_av(pid):
    kurs_id = con().execute("SELECT kurs_id FROM paamelding WHERE id=?", (pid,)).fetchone()["kurs_id"]
    opp = db.meld_av(con(), pid, aktor=_aktor())
    if opp:
        sveiper.kjor(Kjoring(con(), idag=_idag()), opp)
        flash("Avmeldt. Første på ventelisten har fått plassen og bekreftelse på e-post.", "ok")
    else:
        flash("Avmeldt.", "ok")
    con().commit()
    return redirect(url_for("admin_kurs_deltakere", kurs_id=kurs_id))


@app.get("/admin/kurs/<int:kurs_id>/qr/<int:dag_id>")
@krever_admin
def admin_qr(kurs_id, dag_id):
    kd = con().execute("SELECT kd.*, k.navn FROM kursdag kd JOIN kurs k ON k.id=kd.kurs_id WHERE kd.id=? AND kd.kurs_id=?",
                       (dag_id, kurs_id)).fetchone() or abort(404)
    lenke = f"{config.BASE_URL}{url_for('innsjekk_qr', token=kd['innsjekk_token'])}"
    n = con().execute("SELECT COUNT(*) FROM oppmote WHERE kursdag_id=?", (dag_id,)).fetchone()[0]
    return render_template("admin_qr.html", kd=kd, svg=qr_svg(lenke), lenke=lenke, antall=n,
                           kode_url=f"{config.BASE_URL}/innsjekk")


@app.get("/admin/kurs/<int:kurs_id>/allergiliste")
@krever_admin
def admin_allergiliste(kurs_id):
    kurs = con().execute("SELECT * FROM kurs WHERE id=?", (kurs_id,)).fetchone() or abort(404)
    rader = con().execute(
        """SELECT d.navn, s.allergier, s.tilrettelegging FROM sensitivt s JOIN paamelding p ON p.id=s.paamelding_id
           JOIN deltaker d ON d.id=p.deltaker_id WHERE p.kurs_id=? AND p.status='bekreftet' ORDER BY d.navn""",
        (kurs_id,)).fetchall()
    db.logg(con(), "sensitivt_vist", {"kurs_id": kurs_id}, aktor=_aktor())
    con().commit()
    return render_template("admin_allergi.html", kurs=kurs, rader=rader)


_DELTAKER_CSV_KOLONNER = ["Navn", "E-post", "Telefon", "Arbeidssted", "Status", "Betaler", "Betaling",
                         "Organisasjon", "Org.nr", "Fakturanr", "Fakturert (kr)"]
_DELTAKER_CSV_SELECT = """SELECT d.navn, d.epost, d.telefon, d.arbeidssted, p.status, p.betaler, p.betaling,
                                p.org_navn, p.org_nr,
                                (SELECT GROUP_CONCAT(faktura_nr, ' ') FROM faktura WHERE paamelding_id=p.id) AS faktura_nr,
                                (SELECT COALESCE(SUM(belop_nok),0) FROM faktura WHERE paamelding_id=p.id) AS fakturert_belop
                         FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id"""


def _deltaker_csv_respons(rader, filnavn: str) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(_DELTAKER_CSV_KOLONNER)
    w.writerows([tuple(r) for r in rader])
    # Inneholder personopplysninger (navn/e-post/telefon) - skal aldri mellomlagres av nettleser/proxy.
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",  # BOM -> Excel leser æøå riktig
                    headers={"Content-Disposition": f"attachment; filename={filnavn}",
                             "Cache-Control": "no-store"})


@app.get("/admin/kurs/<int:kurs_id>/deltakere.csv")
@krever_admin
def admin_csv(kurs_id):
    """Eksporterer deltakere for kurset. Uten sok/status-parametre: hele kurset (uendret
    oppforsel). Med sok/status satt (samme parametre som deltakerlisten sitt eget filter):
    eksporterer kun det admin faktisk ser paa skjermen akkurat na."""
    sok, status = _deltaker_sok_status(request.args)
    vilkar_sql, vilkar_args = _deltaker_filter_vilkar(sok, status)
    sql = _DELTAKER_CSV_SELECT + " WHERE p.kurs_id=?" + vilkar_sql + " ORDER BY p.status, d.navn"
    rader = con().execute(sql, [kurs_id, *vilkar_args]).fetchall()
    return _deltaker_csv_respons(rader, f"deltakere_{kurs_id}.csv")


@app.post("/admin/kurs/<int:kurs_id>/deltakere/eksporter-valgte.csv")
@krever_admin
def admin_eksporter_valgte_csv(kurs_id):
    """Eksporterer KUN de avkryssede deltakerne. POST (ikke GET) fordi utvalget kan bli langt.
    ID-ene revalideres alltid mot kurs_id her - en manipulert POST kan ikke faa med rader fra
    et annet kurs."""
    _hent_kurs(kurs_id)
    ider = request.form.getlist("paamelding_id", type=int)
    if not ider:
        flash("Velg minst én deltaker.", "feil")
        return redirect(url_for("admin_kurs_deltakere", kurs_id=kurs_id))
    plassholdere = ",".join("?" * len(ider))
    sql = _DELTAKER_CSV_SELECT + f" WHERE p.kurs_id=? AND p.id IN ({plassholdere}) ORDER BY p.status, d.navn"
    rader = con().execute(sql, [kurs_id, *ider]).fetchall()
    return _deltaker_csv_respons(rader, f"deltakere_utvalg_{kurs_id}.csv")


# Maks antall deltakere i én bulk-behandling (fase 11). Satt lavt bevisst: behandlingen innebaerer
# ekte e-post-/Visma-kall per rad, og losningen skal etter hvert kunne driftes i Azure der vi ikke
# onsker svaert langvarige synkrone HTTP-requests. Kan okes senere - ikke arkitektur som hindrer at
# bulk-behandling flyttes til en bakgrunnsjobb/ko da.
MAKS_BULK_VALGT = 50

# Bulk-behandling er SYNKRON og sekvensiell (én paamelding om gangen, ingen transaksjon rundt helheten).
# Stoppregel (START av nye rader stoppes - en rad som allerede behandles avbrytes ALDRI):
#   * etter BULK_STOPP_ETTER paafolgende forsok som endte uavklart ('ukjent') eller feilet
#   * naar tidsbudsjettet er brukt opp (sjekkes FOR hver ny rad, med monotonic-tid - ikke klokkeslett)
# BULK_TIDSBUDSJETT_SEK er en prototype-/sikkerhetsgrense, IKKE en antakelse om endelig Azure-/proxy-/
# worker-timeout - produksjonsverdien maa vurderes mot faktisk hosting (se hardening-backloggen).
BULK_TIDSBUDSJETT_SEK = 120
BULK_STOPP_ETTER = 3
_klokke = time.monotonic  # egen referanse slik at tester kan styre tiden


def _bulk_klassifiser(kurs, rad) -> tuple[bool, str]:
    """Kan EN rad behandles av bulk-handlingen na, og hvorfor/hvorfor ikke. Ren lesing - samme
    klassifisering (behandling.klassifiser) som den faktiske bulk-behandlingen og fase 9 bruker."""
    avvist = behandling.klassifiser(con(), kurs, rad)
    return (True, "Klar til behandling") if avvist is None else (False, avvist.arsak)


def _bulk_ider_eller_avvis(kurs_id: int):
    """Felles for forhaandsvisning og behandling. Maks-grensen haandheves SERVER-SIDE paa ANTALL RAA request-felt
    (request.form.getlist uten konvertering) - FOER heltallsparsing, filtrering av ugyldige verdier, deduplisering og
    databaseoppslag. Deretter: kun heltall, duplikater fjernes (rekkefolgen beholdes), tomt utvalg avvises.
    Returnerer (ider, None) eller (None, redirect-respons)."""
    raa_ider = request.form.getlist("paamelding_id")
    if len(raa_ider) > MAKS_BULK_VALGT:
        flash(f"Du kan behandle maks {MAKS_BULK_VALGT} deltakere om gangen (valgte {len(raa_ider)}). "
             "Del opp i flere omganger.", "feil")
        return None, redirect(url_for("admin_kurs_deltakere", kurs_id=kurs_id))
    ider = list(dict.fromkeys(request.form.getlist("paamelding_id", type=int)))
    if not ider:
        flash("Velg minst én deltaker.", "feil")
        return None, redirect(url_for("admin_kurs_deltakere", kurs_id=kurs_id))
    return ider, None


@app.post("/admin/kurs/<int:kurs_id>/deltakere/bulk/forhandsvis")
@krever_admin
def admin_bulk_forhandsvis(kurs_id):
    """Fase 11, trinn 2: viser hvilke av de valgte deltakerne som faktisk kan behandles na, og
    en forstaaelig grunn for resten. INGEN databaseendring - kun lesing og klassifisering.
    Ingen e-post, ingen Visma, ingen faktura, ingen sveiper.kjor(), ingen endring av sveiper_utsatt.
    Forhaandsvisningen er ikke en laas: selve behandlingen (admin_bulk_behandle) revaliderer hver rad paa nytt.
    Kun rader som er behandlingsbare her sendes videre til behandlings-POST-en."""
    kurs = _hent_kurs(kurs_id)
    ider, avvist = _bulk_ider_eller_avvis(kurs_id)
    if avvist:
        return avvist

    plassholdere = ",".join("?" * len(ider))
    rader = con().execute(
        f"""SELECT p.id, p.status, p.sveiper_kjort, p.sveiper_utsatt, d.navn, d.epost
            FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id
            WHERE p.kurs_id=? AND p.id IN ({plassholdere})""", [kurs_id, *ider]).fetchall()
    # ID-er som ikke tilhorer DETTE kurset forsvinner stille her - ingen feilmelding som
    # bekrefter/avkrefter at de finnes et annet sted (unngaar informasjonslekkasje).

    resultater = []
    for rad in rader:
        behandlingsbar, arsak = _bulk_klassifiser(kurs, rad)
        resultater.append({"paamelding_id": rad["id"], "navn": rad["navn"], "epost": rad["epost"],
                           "status": rad["status"], "behandlingsbar": behandlingsbar, "arsak": arsak,
                           "hva_skjer": behandling.forventet_handling(kurs, rad) if behandlingsbar else ""})
    resultater.sort(key=lambda r: r["navn"])

    resp = make_response(render_template(
        "admin_bulk_forhandsvisning.html", kurs=kurs, resultater=resultater,
        antall_behandlingsbare=sum(1 for r in resultater if r["behandlingsbar"]),
        antall_totalt=len(resultater), fane="deltakere"))
    resp.headers["Cache-Control"] = "no-store"  # personopplysninger - skal aldri mellomlagres
    return resp


_BULK_STOPP_TEKST = {
    "tidsbudsjett": "Ikke forsøkt: behandlingen ble stoppet fordi tidsgrensen var nådd. Velg denne deltakeren "
                    "på nytt for å fortsette.",
    "uavklart_rad": "Ikke forsøkt: behandlingen ble stoppet etter flere uavklarte utfall på rad. Finn årsaken "
                    "først, og velg deretter denne deltakeren på nytt.",
}


@app.post("/admin/kurs/<int:kurs_id>/deltakere/bulk/behandle")
@krever_admin
def admin_bulk_behandle(kurs_id):
    """Fase 11, trinn 3: faktisk bulk-behandling av holdte paameldinger.

    Bruker SAMME behandling som fase 9 (behandling.behandle_holdt_paamelding -> eksisterende sveiper-motor).
    Denne ruten kaller aldri epost.send/visma.fakturer, oppretter ingen faktura og rorer aldri utsending_logg.

    * IKKE atomisk: én paamelding om gangen, hver med sine egne korte claim-/resultat-commits. Eksterne
      sideeffekter kan ikke rulles tilbake - rad 1-7 lykkes, rad 8 kan bli uavklart, og 1-7 forblir fullfort.
    * Hver rad revalideres FERSKT rett foer behandling (i helperen). Browseren sender kun ID-er.
    * Resultatet returneres direkte (ingen lagring): en ny POST er trygg (idempotent) og gir
      "allerede behandlet"/"ikke lenger behandlingsbar".
    * Personvern: navn/e-post vises kun her, til autentisert admin. Hendelsesloggen far kun ID-er og tellinger.
    """
    kurs = _hent_kurs(kurs_id)
    ider, avvist = _bulk_ider_eller_avvis(kurs_id)
    if avvist:
        return avvist

    plassholdere = ",".join("?" * len(ider))
    vis = {r["id"]: r for r in con().execute(
        f"""SELECT p.id, d.navn, d.epost FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id
            WHERE p.kurs_id=? AND p.id IN ({plassholdere})""", [kurs_id, *ider]).fetchall()}  # kun til visning
    rekkefolge = [i for i in ider if i in vis]  # fremmede/ugyldige ID-er forsvinner uten spor
    if not rekkefolge:
        flash("Velg minst én deltaker.", "feil")
        return redirect(url_for("admin_kurs_deltakere", kurs_id=kurs_id))

    bulk_id = secrets.token_hex(8)  # tilfeldig og opakt - ingen persondata
    idag, aktor = _idag(), _aktor()
    tellere = dict.fromkeys(behandling.KATEGORI_NAVN, 0)
    resultater, stoppgrunn, strak = [], None, 0
    start = _klokke()
    try:
        for pid in rekkefolge:
            if stoppgrunn is None and _klokke() - start >= BULK_TIDSBUDSJETT_SEK:
                stoppgrunn = "tidsbudsjett"  # sjekkes FOR en ny rad - en rad som pagar avbrytes aldri
            if stoppgrunn:
                res = behandling.Behandlingsresultat(
                    behandling.IKKE_FORSOKT, f"stoppet_{stoppgrunn}", _BULK_STOPP_TEKST[stoppgrunn])
            else:
                try:
                    res = behandling.behandle_holdt_paamelding(
                        con(), kurs_id, pid, idag, aktor=aktor, via="bulk", bulk_id=bulk_id)
                except Exception as e:  # noqa: BLE001 - lokal teknisk feil i EN rad stopper ikke de andre
                    con().rollback()
                    feil = sikker_feiltekst(e)
                    res = behandling.Behandlingsresultat(
                        behandling.FEILET, "teknisk", f"Teknisk feil ({feil}) – ikke fullført.", forsokt=True)
                    db.logg(con(), "manuell_behandling_utlost",
                            {"paamelding_id": pid, "kurs_id": kurs_id, "resultat": "feilet", "via": "bulk",
                             "bulk_id": bulk_id, "feil": feil}, aktor=aktor)
                    con().commit()
                if behandling.teller_som_feil(res):
                    strak += 1
                elif res.kode == behandling.FULLFORT:
                    strak = 0
                if strak >= BULK_STOPP_ETTER:
                    stoppgrunn = "uavklart_rad"
            tellere[res.kode] += 1
            resultater.append({"paamelding_id": pid, "navn": vis[pid]["navn"], "epost": vis[pid]["epost"],
                               "kode": res.kode, "arsak": res.arsak})
    finally:
        db.logg(con(), "bulk_behandling_utlost",
                {"kurs_id": kurs_id, "bulk_id": bulk_id, "antall_valgt": len(rekkefolge),
                 **{kode: antall for kode, antall in tellere.items()},
                 "stoppet": stoppgrunn is not None, "stoppgrunn": stoppgrunn}, aktor=aktor)
        con().commit()

    rekke = list(behandling.KATEGORI_NAVN)  # visningsrekkefolge
    resultater.sort(key=lambda r: (rekke.index(r["kode"]), r["navn"]))
    resp = make_response(render_template(
        "admin_bulk_resultat.html", kurs=kurs, resultater=resultater, tellere=tellere,
        kategorier=behandling.KATEGORI_NAVN, stoppgrunn=stoppgrunn, antall_totalt=len(rekkefolge),
        fane="deltakere"))
    resp.headers["Cache-Control"] = "no-store"  # personopplysninger - skal aldri mellomlagres
    return resp


# ======================= rapporter (fase 6) =======================
# Personvern: ingen av spørringene her rører "sensitivt" (allergi/tilrettelegging) - hverken i
# visning eller eksport. Eksport logges i hendelsesloggen med hvilke FILTRE som var i bruk
# (datoer/status/ansvarlig/om det var søkt), aldri med selve søketeksten eller andre personopplysninger.

KURS_STATUSVERDIER = ("utkast", "aapen", "full", "aktiv", "avsluttet", "avlyst")


def _kurs_rapport_rader():
    """Felles spørring for baade HTML-visning og CSV-eksport, slik at eksporten aldri kan
    avvike fra det som faktisk vises/filtreres paa siden."""
    fra = request.args.get("fra") or ""
    til = request.args.get("til") or ""
    status = request.args.get("status") or ""
    ansvarlig = request.args.get("ansvarlig", type=int) or 0
    sok = request.args.get("sok", "").strip()

    sql = """SELECT k.*, ab.navn AS ansvarlig_navn, MIN(kd.dato) AS start, MAX(kd.dato) AS slutt,
                    (SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=k.id AND p.status='bekreftet') AS bekreftet,
                    (SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=k.id AND p.status='venteliste') AS venteliste,
                    (SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=k.id AND p.status='avmeldt') AS avmeldt,
                    (SELECT COALESCE(SUM(f.belop_nok),0) FROM faktura f JOIN paamelding p ON p.id=f.paamelding_id
                        WHERE p.kurs_id=k.id) AS fakturert
             FROM kurs k
             LEFT JOIN kursdag kd ON kd.kurs_id=k.id
             LEFT JOIN admin_bruker ab ON ab.id=k.ansvarlig_admin_id
             WHERE (? = '' OR LOWER(k.navn) LIKE ?) AND (? = 0 OR k.ansvarlig_admin_id = ?)
                   AND (? = '' OR k.status = ?)
             GROUP BY k.id
             HAVING (? = '' OR MAX(kd.dato) >= ?) AND (? = '' OR MIN(kd.dato) <= ?)
             ORDER BY start"""
    args = (sok.lower(), f"%{sok.lower()}%", ansvarlig, ansvarlig, status, status, fra, fra, til, til)
    rader = con().execute(sql, args).fetchall()
    filtre = {"fra": fra or None, "til": til or None, "status": status or None,
             "ansvarlig_admin_id": ansvarlig or None, "har_sok": bool(sok)}
    return rader, filtre


@app.get("/admin/rapporter/kurs")
@krever_admin
def admin_rapport_kurs():
    rader, _ = _kurs_rapport_rader()
    admins = con().execute("SELECT id, navn FROM admin_bruker WHERE aktiv=1 ORDER BY navn").fetchall()
    return render_template(
        "admin_rapport_kurs.html", rader=rader, admins=admins, kurs_statusverdier=KURS_STATUSVERDIER,
        fra=request.args.get("fra", ""), til=request.args.get("til", ""), status=request.args.get("status", ""),
        ansvarlig=request.args.get("ansvarlig", type=int), sok=request.args.get("sok", ""))


@app.get("/admin/rapporter/kurs.csv")
@krever_admin
def admin_rapport_kurs_csv():
    rader, filtre = _kurs_rapport_rader()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Kode", "Tittel", "Start", "Slutt", "Status", "Ansvarlig", "Bekreftet", "Kapasitet",
                "Fyllingsgrad (%)", "Venteliste", "Avmeldt", "Fakturert (kr)"])
    for k in rader:
        fyllingsgrad = round(k["bekreftet"] / k["kapasitet"] * 100) if k["kapasitet"] else ""
        w.writerow([k["kode"], k["navn"], k["start"] or "", k["slutt"] or "", k["status"], k["ansvarlig_navn"] or "",
                   k["bekreftet"], k["kapasitet"] or "", fyllingsgrad, k["venteliste"], k["avmeldt"], k["fakturert"]])
    db.logg(con(), "rapport_eksportert", {"rapport": "kurs", **filtre, "antall_rader": len(rader)}, aktor=_aktor())
    con().commit()
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=kursrapport.csv"})


def _deltakere_rapport_rader():
    sok = request.args.get("sok", "").strip()
    kun_flere = request.args.get("kun_flere_kurs") == "1"
    sql = """SELECT d.id, d.navn, d.epost, d.telefon, d.arbeidssted,
                    COUNT(DISTINCT CASE WHEN p.status='bekreftet' THEN p.kurs_id END) AS antall_kurs
             FROM deltaker d LEFT JOIN paamelding p ON p.deltaker_id=d.id
             WHERE (? = '' OR LOWER(d.navn) LIKE ? OR LOWER(d.epost) LIKE ? OR LOWER(COALESCE(d.arbeidssted,'')) LIKE ?)
             GROUP BY d.id
             HAVING (? = 0 OR antall_kurs > 1)
             ORDER BY d.navn"""
    likemal = f"%{sok.lower()}%"
    args = (sok.lower(), likemal, likemal, likemal, 1 if kun_flere else 0)
    rader = con().execute(sql, args).fetchall()
    filtre = {"har_sok": bool(sok), "kun_flere_kurs": kun_flere}
    return rader, filtre


@app.get("/admin/rapporter/deltakere")
@krever_admin
def admin_rapport_deltakere():
    rader, _ = _deltakere_rapport_rader()
    return render_template("admin_rapport_deltakere.html", rader=rader, sok=request.args.get("sok", ""),
                           kun_flere_kurs=request.args.get("kun_flere_kurs") == "1")


@app.get("/admin/rapporter/deltakere.csv")
@krever_admin
def admin_rapport_deltakere_csv():
    rader, filtre = _deltakere_rapport_rader()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Navn", "E-post", "Telefon", "Arbeidssted", "Antall kurs (bekreftet)"])
    for d in rader:
        w.writerow([d["navn"], d["epost"], d["telefon"] or "", d["arbeidssted"] or "", d["antall_kurs"]])
    db.logg(con(), "rapport_eksportert", {"rapport": "deltakere", **filtre, "antall_rader": len(rader)}, aktor=_aktor())
    con().commit()
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=deltakerregister.csv"})


@app.get("/admin/rapporter/deltaker/<int:deltaker_id>")
@krever_admin
def admin_rapport_deltaker(deltaker_id):
    deltaker = con().execute("SELECT * FROM deltaker WHERE id=?", (deltaker_id,)).fetchone() or abort(404)
    paameldinger = con().execute(
        """SELECT p.id AS paamelding_id, p.kurs_id, p.status, p.opprettet, k.navn AS kurs_navn, k.kode,
                  k.status AS kurs_status
           FROM paamelding p JOIN kurs k ON k.id=p.kurs_id WHERE p.deltaker_id=? ORDER BY p.opprettet DESC""",
        (deltaker_id,)).fetchall()
    return render_template("admin_rapport_deltaker.html", deltaker=deltaker, paameldinger=paameldinger)


@app.get("/admin/rapporter")
@krever_admin
def admin_rapporter():
    fra = request.args.get("fra") or ""
    til = request.args.get("til") or ""
    periode_args = (fra, fra, til, til)

    deltakelser = con().execute(
        """SELECT COUNT(*) FROM paamelding p WHERE p.status='bekreftet' AND p.kurs_id IN (
             SELECT kurs_id FROM kursdag GROUP BY kurs_id
             HAVING (? = '' OR MAX(dato) >= ?) AND (? = '' OR MIN(dato) <= ?))""", periode_args).fetchone()[0]

    venteliste_kurs = con().execute(
        """SELECT k.id, k.navn, k.kode, COUNT(*) AS antall FROM paamelding p JOIN kurs k ON k.id=p.kurs_id
           WHERE p.status='venteliste' AND k.id IN (
             SELECT kurs_id FROM kursdag GROUP BY kurs_id
             HAVING (? = '' OR MAX(dato) >= ?) AND (? = '' OR MIN(dato) <= ?))
           GROUP BY k.id ORDER BY antall DESC""", periode_args).fetchall()

    avlyste_kurs = con().execute(
        """SELECT k.id, k.navn, k.kode FROM kurs k WHERE k.status='avlyst' AND k.id IN (
             SELECT kurs_id FROM kursdag GROUP BY kurs_id
             HAVING (? = '' OR MAX(dato) >= ?) AND (? = '' OR MIN(dato) <= ?))""", periode_args).fetchall()

    avmeldt_antall = con().execute(
        """SELECT COUNT(*) FROM hendelse WHERE handling='avmelding'
           AND (? = '' OR date(ts) >= ?) AND (? = '' OR date(ts) <= ?)""", periode_args).fetchone()[0]

    fakturert_sum = con().execute(
        """SELECT COALESCE(SUM(belop_nok),0) FROM faktura
           WHERE (? = '' OR date(opprettet) >= ?) AND (? = '' OR date(opprettet) <= ?)""", periode_args).fetchone()[0]

    flere_kurs_antall = con().execute(
        """SELECT COUNT(*) FROM (SELECT deltaker_id FROM paamelding WHERE status='bekreftet'
             GROUP BY deltaker_id HAVING COUNT(DISTINCT kurs_id) > 1)""").fetchone()[0]

    return render_template(
        "admin_rapporter.html", fra=fra, til=til, deltakelser=deltakelser, venteliste_kurs=venteliste_kurs,
        avlyste_kurs=avlyste_kurs, avmeldt_antall=avmeldt_antall, fakturert_sum=fakturert_sum,
        flere_kurs_antall=flere_kurs_antall)


@app.route("/admin/kurs/ny", methods=["GET", "POST"])
@krever_admin
def admin_ny_kurs():
    if request.method == "POST":
        f = request.form
        datoer = sorted({d.strip() for d in f.get("datoer", "").replace(",", "\n").splitlines() if d.strip()})
        er_duplikat = bool(f.get("fra"))
        try:
            for d in datoer:
                date.fromisoformat(d)
            paameldingsfrist = f.get("paameldingsfrist") or None
            if paameldingsfrist:
                date.fromisoformat(paameldingsfrist)
            faktura_dager_for = db.valider_faktura_dager_for(int(f.get("faktura_dager_for") or 14))   # foer SharePoint/DB
            with db.transaksjon(con()):
                kode = db.generer_kode(con(), f["navn"].strip(), datoer)
                felter = dict(
                    type=f["type"], sted=f.get("sted") or None, start_kl=f["start_kl"], slutt_kl=f["slutt_kl"],
                    timer_pr_dag=float(f.get("timer_pr_dag") or 6), kapasitet=int(f["kapasitet"]) if f.get("kapasitet") else None,
                    pris_nok=int(f.get("pris_nok") or 0), fakturering=f["fakturering"],
                    betaling=f.get("betaling", "samlet"), faktura_dager_for=faktura_dager_for,
                    spesialistlop=f.get("spesialistlop") or None, kursholder_epost=f.get("kursholder_epost") or None,
                    notat=f.get("notat") or None, ansvarlig_admin_id=f.get("ansvarlig_admin_id", type=int),
                    paameldingsfrist=paameldingsfrist, sharepoint_mappe=sharepoint.opprett_kursmappe(kode),
                )
                if er_duplikat:
                    # Et duplisert kurs skal gjennomgås og åpnes bevisst - ikke være synlig/åpent for påmelding med en gang.
                    felter["status"] = "utkast"
                kid = db.opprett_kurs(con(), kode=kode, navn=f["navn"].strip(), datoer=datoer, aktor=_aktor(), **felter)
                if f.get("kursholder_epost") and f.get("materiell_frist"):
                    con().execute("INSERT INTO materiell_krav (kurs_id, ansvarlig_navn, ansvarlig_epost, frist) VALUES (?,?,?,?)",
                                  (kid, f.get("kursholder_navn") or f["kursholder_epost"], f["kursholder_epost"], f["materiell_frist"]))
            flash(f"Kurset er opprettet med koden «{kode}», og har fått påmeldingsside, SharePoint-mappe og innsjekkkoder.", "ok")
            return redirect(url_for("admin_kurs_oppsett", kurs_id=kid))
        except (ValueError, KeyError, sqlite3.IntegrityError, db.Paameldingsfeil) as e:
            flash(f"Kunne ikke opprette kurs: {e}", "feil")

    fra_kurs = None
    kursholder_navn_forslag = ""
    kildedatoer = []
    fra_id = request.args.get("fra", type=int)
    if fra_id:
        fra_kurs = _hent_kurs(fra_id)
        siste_krav = con().execute(
            "SELECT ansvarlig_navn FROM materiell_krav WHERE kurs_id=? ORDER BY id DESC LIMIT 1", (fra_id,)).fetchone()
        kursholder_navn_forslag = siste_krav["ansvarlig_navn"] if siste_krav else ""
        kildedatoer = [d["dato"] for d in db.kursdager(con(), fra_id)]
    admins = con().execute("SELECT id, navn FROM admin_bruker WHERE aktiv=1 ORDER BY navn").fetchall()
    return render_template("admin_ny_kurs.html", fra_kurs=fra_kurs, kursholder_navn_forslag=kursholder_navn_forslag,
                           kildedatoer=kildedatoer, admins=admins)


@app.route("/admin/daglig", methods=["GET", "POST"])
@krever_admin
def admin_daglig():
    utskrift = None
    if request.method == "POST":
        dato = date.fromisoformat(request.form.get("dato") or date.today().isoformat())
        if config.DEMO:
            session["demo_dato"] = dato.isoformat()
        k = Kjoring(con(), idag=dato, tor=bool(request.form.get("tor")))
        daglig.kjor(k)
        utskrift = "\n".join(k.utskrift)
    return render_template("admin_daglig.html", utskrift=utskrift)


def _maltekst_tilpasset(mal: str) -> bool:
    """Raa tilstedevaerelse (finnes minst en rad i mal_tekst for denne malen) - IKKE via er_tilpasset()/
    effektive_tekster(), som validerer ALLE rader for malen og derfor kan kaste MalFeil paa grunn av et HELT
    ANNET felt enn det vi spor om. Oversikten skal aldri kunne krasje paa grunn av en korrupt overstyring."""
    return bool(db.hent_overstyringer_for_mal(con(), mal))


def _rediger_kontekst(mal: str, feil_felt: str | None = None, feil_tekst: str | None = None) -> dict:
    """Bygger data til redigeringssiden. Robust mot at malen har EN korrupt lagret rad: da vises hvert felts egen
    raatekst direkte fra databasen (aldri stille tom) i stedet for at HELE siden feiler - admin skal alltid kunne
    naa denne siden for aa rette eller tilbakestille en ugyldig overstyring, ikke bare se den andre steder."""
    m = maltekster.MALER[mal]
    raa = db.hent_overstyringer_for_mal(con(), mal)
    try:
        effektive = maltekster.effektive_tekster(con(), mal)
        mal_feilmelding = None
    except maltekster.MalFeil as e:
        effektive = None
        mal_feilmelding = (e.forklaring or "En lagret tekst for denne malen er ugyldig.") + \
            " Rett teksten under, eller tilbakestill feltet til standard."
    felter = []
    for felt, f in m.felt.items():
        if felt == feil_felt:
            tekst = feil_tekst
        elif effektive is not None:
            tekst = effektive[felt]
        else:
            tekst = raa.get(felt, f.standard)  # denne malens rader kan ikke valideres samlet - vis lagret raatekst
        felter.append({"navn": felt, "visningsnavn": f.navn, "type": f.type, "maks": f.maks, "tom_tillatt": f.tom_tillatt,
                      "tekst": tekst, "tilpasset": felt in raa, "koder": sorted(f.kode),
                      "ugyldig": effektive is None and felt in raa})
    return dict(mal=mal, mal_navn=m.navn, mal_beskrivelse=m.beskrivelse, felter=felter, koder=maltekster.KODER,
               systemkoder=maltekster.SYSTEMKODER, feil_felt=feil_felt, mal_feilmelding=mal_feilmelding)


@app.get("/admin/e-postmaler")
@krever_admin
def admin_maltekster():
    """Oversikt over de 8 redigerbare e-postmalene. Laaste systemmaler (innlogging/eskalering/admin_melding) er
    ikke i MALER-registeret og vises derfor aldri her - de kan ikke redigeres i det hele tatt."""
    maler = [{"mal": mal, "navn": m.navn, "beskrivelse": m.beskrivelse, "tilpasset": _maltekst_tilpasset(mal)}
            for mal, m in maltekster.MALER.items()]
    return render_template("admin_maltekster.html", maler=maler)


@app.get("/admin/e-postmaler/<mal>")
@krever_admin
def admin_maltekst_rediger(mal):
    if mal not in maltekster.MALER:
        abort(404)
    return render_template("admin_maltekst_rediger.html", **_rediger_kontekst(mal))


@app.post("/admin/e-postmaler/<mal>/<felt>")
@krever_admin
def admin_maltekst_lagre(mal, felt):
    if mal not in maltekster.MALER or felt not in maltekster.MALER[mal].felt:
        abort(404)
    tekst = request.form.get("tekst", "")
    try:
        maltekster.lagre_maltekst(con(), mal, felt, tekst, aktor=_aktor())
        con().commit()
    except maltekster.MalFeil as e:
        con().rollback()
        flash(e.forklaring or "Teksten kunne ikke lagres.", "feil")
        return render_template("admin_maltekst_rediger.html", **_rediger_kontekst(mal, feil_felt=felt, feil_tekst=tekst)), 400
    flash(f"«{maltekster.MALER[mal].felt[felt].navn}» er lagret.", "ok")
    return redirect(url_for("admin_maltekst_rediger", mal=mal))


@app.post("/admin/e-postmaler/<mal>/<felt>/tilbakestill")
@krever_admin
def admin_maltekst_tilbakestill(mal, felt):
    if mal not in maltekster.MALER or felt not in maltekster.MALER[mal].felt:
        abort(404)
    fjernet = maltekster.tilbakestill_maltekst(con(), mal, felt, aktor=_aktor())
    con().commit()
    flash(f"«{maltekster.MALER[mal].felt[felt].navn}» er tilbakestilt til standardteksten." if fjernet
         else "Dette feltet bruker allerede standardteksten.", "ok")
    return redirect(url_for("admin_maltekst_rediger", mal=mal))


@app.get("/admin/e-postmaler/<mal>/forhandsvis")
@krever_admin
def admin_maltekst_forhandsvis(mal):
    """Forhaandsviser den EFFEKTIVE malteksten (override hvis den finnes, ellers standard) med fiktive eksempeldata -
    aldri ekte deltaker-/kurs-/firmadata. Bruker samme rendringsvei som faktisk utsending
    (Kjoring.render_for_sending: override-bevisst maltekst + epost.render), men gjor INGEN claim, sender ingen
    e-post og skriver ingen utsending_logg-rad - render_for_sending leser kun og har ingen sideeffekter."""
    if mal not in maltekster.MALER:
        abort(404)
    k = Kjoring(con())
    visninger, feil = [], None
    try:
        for variant, data in mal_eksempler.eksempler(mal):
            emne, html = k.render_for_sending(mal, **data)
            visninger.append({"variant": variant, "emne": emne, "html": html})
    except maltekster.MalFeil as e:
        feil = e.forklaring or "Malen kunne ikke forhåndsvises akkurat nå."
    return render_template("admin_maltekst_forhandsvis.html", mal=mal, mal_navn=maltekster.MALER[mal].navn,
                           visninger=visninger, feil=feil)


@app.get("/admin/utboks")
@krever_admin
def admin_utboks():
    return render_template("admin_utboks.html", meldinger=epost.les_utboks())


@app.get("/admin/logg-ut")
def admin_logg_ut():
    session.pop("admin_id", None)
    session.pop("admin_brukernavn", None)
    session.pop("admin_navn", None)
    return redirect(url_for("forside"))


# ======================= mottak fra nettsidens skjema =======================

# Feltnavn fra ulike skjema-plugins -> vaare navn. Utvides naar vi ser hva ipr.no sitt skjema sender.
FELTMAP = {
    "navn": ["navn", "name", "fullt_navn", "your-name"],
    "fornavn": ["fornavn", "first_name", "firstname"],
    "etternavn": ["etternavn", "last_name", "lastname"],
    "epost": ["epost", "e-post", "email", "your-email", "e_post"],
    "kurs": ["kurs", "kurskode", "kurs_kode", "course", "course_id"],
    "telefon": ["telefon", "mobil", "phone", "tlf"],
    "arbeidssted": ["arbeidssted", "arbeidsgiver", "employer", "organisasjon"],
    "org_nr": ["org_nr", "orgnr", "organisasjonsnummer"],
    "faktura_ref": ["faktura_ref", "referanse", "bestillernr"],
    "faktura_adresse": ["faktura_adresse", "adresse", "address"],
    "faktura_postnr": ["faktura_postnr", "postnr", "postnummer", "zip"],
    "faktura_sted": ["faktura_sted", "poststed", "sted", "city"],
    "allergier": ["allergier", "allergi", "matallergi", "spesialkost"],
    "hpr_nr": ["hpr_nr", "hpr", "hprnr"],
}


def _hent(data: dict, felt: str) -> str | None:
    lav = {str(k).lower().strip(): v for k, v in data.items()}
    for alias in FELTMAP.get(felt, [felt]):
        v = lav.get(alias)
        if isinstance(v, list):
            v = v[0] if v else None
        if v not in (None, ""):
            return str(v).strip()
    return None


@app.post("/api/paamelding")
def api_paamelding():
    """Tar imot paameldinger fra skjemaet paa nettsiden (i stedet for Pindena).

    Autentisering: header X-IPR-Signatur = HMAC-SHA256(hemmelighet, raa body) (anbefalt),
    eller header X-IPR-Token / ?token= med hemmeligheten (for skjema-plugins som ikke kan signere).
    Tar imot JSON eller vanlig skjemadata. Idempotent: samme person paa samme kurs gir 200 uten ny rad.
    """
    import hashlib
    import hmac
    raa = request.get_data()
    sig = request.headers.get("X-IPR-Signatur", "")
    token = request.headers.get("X-IPR-Token") or request.args.get("token", "")
    riktig = hmac.new(config.WEBHOOK_HEMMELIG.encode(), raa, hashlib.sha256).hexdigest()
    if not (hmac.compare_digest(sig, riktig) or (token and hmac.compare_digest(token, config.WEBHOOK_HEMMELIG))):
        return {"status": "feil", "melding": "ugyldig signatur"}, 401

    data = request.get_json(silent=True) or request.form.to_dict(flat=True)
    navn = _hent(data, "navn") or " ".join(x for x in (_hent(data, "fornavn"), _hent(data, "etternavn")) if x)
    epost_, kurskode = _hent(data, "epost"), _hent(data, "kurs")
    if not navn or not epost_ or "@" not in epost_ or not kurskode:
        db.logg(con(), "webhook_avvist", {"felter": sorted(data.keys())})
        con().commit()
        return {"status": "feil", "melding": "mangler navn, epost eller kurs"}, 400
    kurs = con().execute("SELECT id FROM kurs WHERE kode=? OR CAST(id AS TEXT)=?", (kurskode.upper(), kurskode)).fetchone()
    if not kurs:
        return {"status": "feil", "melding": f"ukjent kurs {kurskode}"}, 404

    org_nr = _hent(data, "org_nr")
    try:
        with db.transaksjon(con()):
            pid, status = db.meld_paa(
                con(), kurs["id"], epost=epost_, navn=navn,
                deltaker={"telefon": _hent(data, "telefon"), "arbeidssted": _hent(data, "arbeidssted"), "hpr_nr": _hent(data, "hpr_nr")},
                paamelding={"kilde": "nettside", "betaler": "organisasjon" if org_nr else "person",
                            "org_navn": _hent(data, "arbeidssted") if org_nr else None, "org_nr": org_nr,
                            **{f: _hent(data, f) for f in ("faktura_ref", "faktura_adresse", "faktura_postnr", "faktura_sted")}},
                sensitivt={"allergier": _hent(data, "allergier")},
                idag=_idag(),
            )
    except db.Paameldingsfeil as e:
        return {"status": "ok", "melding": str(e)}, 200
    sveiper.kjor(Kjoring(con(), idag=_idag()), pid)
    con().commit()
    return {"status": "ok", "paamelding_id": pid, "paameldingsstatus": status}, 201


# ======================= KI-assistent =======================

@app.route("/sporsmal", methods=["GET", "POST"])
def sporsmal_side():
    resultat, tekst = None, ""
    if request.method == "POST":
        from .. import assistent
        tekst = request.form.get("sporsmal", "")
        did = session.get("deltaker_id")
        epost_ = request.form.get("epost") or None
        if did:
            epost_ = con().execute("SELECT epost FROM deltaker WHERE id=?", (did,)).fetchone()["epost"]
        resultat = assistent.svar(con(), tekst, deltaker_id=did, epost=epost_, idag=_idag())
        if not resultat.besvart and resultat.henvendelse_id:
            emne = f"Spørsmål fra deltaker til kursadm (#{resultat.henvendelse_id})"
            epost.send(config.ADMIN_EPOST, emne,
                       f"<p><strong>Fra:</strong> {Markup.escape(epost_ or 'ukjent (ikke innlogget)')}</p>"
                       f"<p><strong>Spørsmål:</strong><br>{Markup.escape(tekst)}</p>"
                       f"<p><strong>Hvorfor ikke besvart automatisk:</strong> {Markup.escape(resultat.grunn)}</p>"
                       f"<p><a href='{config.BASE_URL}/admin/kunnskap'>Svar og lag godkjent svar i kunnskapsbasen</a></p>")
    return render_template("sporsmal.html", resultat=resultat, tekst=tekst)


@app.route("/admin/kunnskap", methods=["GET", "POST"])
@krever_admin
def admin_kunnskap():
    if request.method == "POST":
        f = request.form
        felter = (f["kategori"].strip(), f["sporsmal"].strip(), f["svar"].strip(), int(f["kurs_id"]) if f.get("kurs_id") else None,
                  f.get("gyldig_til") or None, 1 if f.get("godkjent") else 0, "admin")
        if f.get("id"):
            con().execute("""UPDATE kunnskap SET kategori=?, sporsmal=?, svar=?, kurs_id=?, gyldig_til=?, godkjent=?, oppdatert_av=?,
                             oppdatert=datetime('now') WHERE id=?""", (*felter, int(f["id"])))
        else:
            con().execute("INSERT INTO kunnskap (kategori, sporsmal, svar, kurs_id, gyldig_til, godkjent, oppdatert_av) VALUES (?,?,?,?,?,?,?)", felter)
        if f.get("henvendelse_id"):
            con().execute("UPDATE henvendelse SET status='lukket' WHERE id=?", (int(f["henvendelse_id"]),))
        db.logg(con(), "kunnskap_lagret", {"id": f.get("id") or "ny", "godkjent": felter[5]}, aktor=_aktor())
        con().commit()
        flash("Lagret. Godkjente svar brukes av assistenten med en gang.", "ok")
        return redirect(url_for("admin_kunnskap"))
    rediger = None
    if request.args.get("rediger"):
        rediger = con().execute("SELECT * FROM kunnskap WHERE id=?", (request.args["rediger"],)).fetchone()
    fra_henvendelse = None
    if request.args.get("fra"):
        fra_henvendelse = con().execute("SELECT * FROM henvendelse WHERE id=?", (request.args["fra"],)).fetchone()
    return render_template(
        "admin_kunnskap.html",
        oppforinger=con().execute("SELECT k.*, ku.kode FROM kunnskap k LEFT JOIN kurs ku ON ku.id=k.kurs_id ORDER BY k.godkjent, k.kategori, k.id").fetchall(),
        henvendelser=con().execute("SELECT * FROM henvendelse WHERE status='til_adm' ORDER BY id DESC").fetchall(),
        siste=con().execute("SELECT * FROM henvendelse ORDER BY id DESC LIMIT 20").fetchall(),
        kurs=con().execute("SELECT id, kode, navn FROM kurs ORDER BY id DESC").fetchall(),
        rediger=rediger, fra=fra_henvendelse)


@app.post("/admin/henvendelse/<int:hid>/lukk")
@krever_admin
def admin_lukk_henvendelse(hid):
    con().execute("UPDATE henvendelse SET status='lukket' WHERE id=?", (hid,))
    con().commit()
    return redirect(url_for("admin_kunnskap"))


def main(omstart: bool = True):
    c = db.koble()
    db.init(c)
    c.close()
    app.run(debug=config.DEMO, use_reloader=omstart and config.DEMO, host="127.0.0.1", port=5000)


if __name__ == "__main__":
    main()
