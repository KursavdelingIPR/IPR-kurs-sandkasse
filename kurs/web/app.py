"""Webapp: paamelding, QR-innsjekk, Min side og admin.

    python -m kurs.web.app        # http://127.0.0.1:5000
"""
import calendar
import csv
import hashlib
import io
import json
import logging
import re
import secrets
import time
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import qrcode
import qrcode.image.svg
from flask import (Flask, Response, abort, flash, g, jsonify, make_response, redirect, render_template, request,
                   session, url_for)
from markupsafe import Markup
from werkzeug.datastructures import MultiDict
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.routing import IntegerConverter
from werkzeug.utils import secure_filename

from .. import (aarsplan, aktivitetsliste, behandling, booking, config, daglig, db, deltakerliste, deltakerside, deltakersok,
                ekstrafelt, eposthistorikk, firmaopplysninger, hendelseslogg, import_deltakere, kursdatoer, kursfarger,
                kurskalender, kursholdere, lenker,
                mal_eksempler, maltekster, migreringer, minside, okonomi, paameldingsside, planlagte_kurs, privatadresse,
                signaturer, sjekklister, skjemafelt, statustekster, sveiper, tema)
from ..deltakerliste import fakturastatus as _fakturastatus
from ..feil import sikker_feiltekst
from ..integrasjoner import brreg, epost, sharepoint
from ..kjoring import Kjoring
from ..kursbevis import timer_i_lop
from . import deltakerside_ruter, entra, kursside_admin_ruter, oppmoteliste_ruter, sikkerhet


class _TryggHeltall(IntegerConverter):
    """`<int:...>` i adressene, men aldri større enn en gyldig id (2 147 483 647): et absurd tall i adressen ga OverflowError mot
    databasen og en 500-side i stedet for 404."""

    def __init__(self, map, fixed_digits=0, min=None, max=2_147_483_647, signed=False):
        super().__init__(map, fixed_digits, min, max, signed)


app = Flask(__name__)
app.url_map.converters["int"] = _TryggHeltall
app.secret_key = config.HEMMELIG_NOKKEL
# Sesjonscookien: HttpOnly + SameSite=Lax alltid; Secure i drift (HTTPS bak App Service sin proxy - ProxyFix leser
# X-Forwarded-Proto/-For, slik at request.is_secure og klientens IP blir riktige). Cookie-levetiden gjelder deltakernes
# «husk meg» (innsjekk); admin-oekter har egne, kortere grenser i krever_admin (ADMIN_MAKS_TIMER / ADMIN_INAKTIV_MIN).
MATERIELL_MAKS_BYTES = 40 * 1024 * 1024   # stoerste tillatte opplasting (kursmateriell); andre ruter setter lavere grenser
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=not config.DEMO,
                  PERMANENT_SESSION_LIFETIME=timedelta(days=30), MAX_CONTENT_LENGTH=MATERIELL_MAKS_BYTES + 128 * 1024,
                  MAX_FORM_MEMORY_SIZE=2 * 1024 * 1024, PREFERRED_URL_SCHEME="http" if config.DEMO else "https")
if not config.DEMO:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
sikkerhet.installer(app)

ADMIN_MAKS_TIMER = 12      # absolutt levetid for en admin-oekt
ADMIN_INAKTIV_MIN = 90     # logges ut etter saa lang inaktivitet


@app.errorhandler(HTTPException)
def _http_feil(e):
    """Kontrollerte feilsider (404/403/405/413/429/...) uten tekniske detaljer. JSON-endepunkter faar JSON."""
    if request.path.startswith("/api/"):
        return {"status": "feil", "melding": e.name}, e.code
    tekster = {
        404: ("Siden finnes ikke", "Adressen er feil, eller siden er fjernet."),
        403: ("Ingen tilgang", "Du har ikke tilgang til denne siden."),
        405: ("Ikke tillatt", "Denne handlingen kan ikke utføres slik."),
        413: ("For stor forespørsel", "Filen eller skjemaet er for stort."),
        429: ("For mange forsøk", "Vent noen minutter og prøv igjen."),
        503: ("Tjenesten er midlertidig utilgjengelig", "Prøv igjen om noen minutter."),
    }
    tittel, tekst = tekster.get(e.code, ("Noe gikk galt", "Prøv igjen, eller ta kontakt med kursadministrasjonen."))
    return render_template("feil.html", tittel=tittel, tekst=tekst), e.code


@app.errorhandler(Exception)
def _uventet_feil(e):
    """500 uten traceback til brukeren. Loggen faar referanse (request-id), rute og feiltype - aldri skjemadata,
    persondata eller feilmeldingens tekst (kan inneholde e-post/URL, se feil.py)."""
    if app.debug or app.testing and app.config.get("PROPAGATE_EXCEPTIONS"):
        raise e
    ref = getattr(g, "request_id", "-")
    app.logger.error("Uventet feil [ref %s] %s %s: %s", ref, request.method, request.url_rule, sikker_feiltekst(e))
    c = g.pop("con", None)
    if c:
        try:
            c.rollback()
            c.close()
        except Exception:  # noqa: BLE001 - opprydding skal aldri skjule den opprinnelige feilen
            pass
    tekst = f"Prøv igjen om litt. Vedvarer feilen, oppgi referansen {ref} til kursadministrasjonen."
    try:
        return render_template("feil.html", tittel="Noe gikk galt", tekst=tekst), 500
    except Exception:  # noqa: BLE001 - selv feilsiden feilet: enkel tekst, aldri traceback
        return Response(f"Noe gikk galt. {tekst}", status=500, mimetype="text/plain; charset=utf-8")


@app.after_request
def _tilgangslogg(respons):
    """Drift: én linje per request med rutemoenster (aldri konkret sti - den kan inneholde tokens), status og
    referanse. Ingen spoerrestreng, ingen skjemadata."""
    if not config.DEMO and not app.testing:
        app.logger.info("%s %s %s [ref %s]", request.method, request.url_rule or request.path.split("/")[1:2],
                        respons.status_code, getattr(g, "request_id", "-"))
    return respons


@app.context_processor
def _globale():
    # assistent_aktiv leses per forespørsel: «Spør oss» og Kunnskapsbase vises i menyene bare når ASSISTENT_AKTIV=1 (standard: av)
    return {"demo": config.DEMO, "idag": _idag().isoformat(), "admin_rolle": g.get("admin_rolle"),
            "rolle_navn": db.ROLLE_NAVN, "assistent_aktiv": config.ASSISTENT_AKTIV,
            "tema_logo": tema.logo, "kontakt_epost": config.AVSENDER_EPOST}      # tema_logo kalles bare på sider med Terapiakademiet-drakten


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


_database_klar = False   # per prosess: True naar databasens versjon er kontrollert og i takt med koden


@app.before_request
def _krev_migrert_database():
    """Drift: appen kjoerer aldri mot en database med feil versjon (umigrert ELLER nyere enn koden). Kontrolleres ved
    foerste request per prosess; ved avvik svares 503 paa alle requests og det kontrolleres paa nytt hver gang, slik at
    appen tar seg inn igjen uten omstart naar `python -m kurs.migrer` er kjoert. I demo/sandkasse holder kjor.py
    databasen i takt selv (db.init), saa kontrollen er ikke noedvendig der."""
    global _database_klar
    if config.DEMO or _database_klar:
        return None
    for feil in sikkerhet.produksjonsfeil():
        app.logger.error("Utrygg produksjonskonfigurasjon: %s", feil)
    try:
        if sikkerhet.produksjonsfeil():
            raise migreringer.VersjonsFeil("konfigurasjon")
        migreringer.kontroller(con())
    except migreringer.VersjonsFeil:
        app.logger.error("Databasen har feil versjon eller konfigurasjonen er utrygg - se loggen over (503)")
        return render_template("feil.html", tittel="Tjenesten er midlertidig utilgjengelig",
                               tekst="Systemet vedlikeholdes. Prøv igjen om noen minutter."), 503
    _database_klar = True
    return None


# Rollebasert tilgang (fase 13). Haandheves HER, server-side, for hver admin-request - aldri bare skjult i UI.
#   system    - alt, inkludert brukere og (toerr)kjoering av daglig jobb
#   kursadmin - alt daglig kursarbeid (kurs, deltakere, e-post, maler, rapporter, kunnskapsbase)
#   lese      - kun se: ingen POST (unntatt utlogging og deltakersøket), ingen eksport av personopplysninger, ingen allergiliste
SYSTEM_ENDEPUNKTER = frozenset({"admin_brukere", "admin_bruker_deaktiver", "admin_bruker_rolle", "admin_daglig",
                                "admin_anonymiser_deltaker"})
KUN_KURSADMIN_ENDEPUNKTER = frozenset({"admin_csv", "admin_eksporter_valgte_csv", "admin_allergiliste",
                                       "admin_deltakerliste_csv", "admin_rapport_kurs_csv", "admin_rapport_okonomi_csv",
                                       "admin_rapport_deltakere_csv", "admin_utboks", "admin_kursside_deltakernavn"})
# admin_sok_json er en ren lesing som sendes som POST bare for at søketeksten ikke skal stå i adressen (se deltakersøket).
LESE_TILLATTE_POST = frozenset({"admin_logg_ut", "admin_sok_json"})


def _admin_okt_gyldig() -> bool:
    """Server-side kontroll av admin-oekten ved HVER request: bruker finnes og er aktiv (deaktivering virker straks),
    absolutt levetid og inaktivitetsgrense. Rollen leses fra databasen hver gang (endringer virker straks) og legges
    i g.admin_rolle. Ugyldig oekt fjernes fra sesjonen."""
    admin_id = session.get("admin_id")
    if not admin_id:
        return False
    naa = time.time()
    start, sist = session.get("admin_start", 0), session.get("admin_sist", 0)
    utlopt = naa - start > ADMIN_MAKS_TIMER * 3600 or naa - sist > ADMIN_INAKTIV_MIN * 60
    rad = None if utlopt else con().execute("SELECT rolle FROM admin_bruker WHERE id=? AND aktiv=1", (admin_id,)).fetchone()
    if not rad:
        _admin_logg_ut()
        return False
    g.admin_rolle = rad["rolle"]
    if naa - sist > 60:                        # skriv cookien paa nytt hoeyst hvert minutt
        session["admin_sist"] = naa
    return True


def _admin_logg_ut() -> None:
    for n in ("admin_id", "admin_brukernavn", "admin_navn", "admin_start", "admin_sist", "admin_rolle"):
        session.pop(n, None)


def _logg_inn_admin(bruker, neste=None):
    """Setter opp admin-sesjonen etter bekreftet identitet (lokalt passord ELLER Entra): ny sesjon (mot session fixation,
    deltakerinnlogging beholdes), tidsstempler, hendelse. Returnerer viderekoblingen."""
    behold = {n: session[n] for n in _DELTAKEROKT_NOKLER if n in session}   # nivået (og lenkens id) følger med: en admin-innlogging gjør ikke en lenke-økt til en e-post-økt
    session.clear()
    session.update(behold)
    session.permanent = True
    session["admin_id"], session["admin_brukernavn"], session["admin_navn"] = (
        bruker["id"], bruker["brukernavn"], bruker["navn"])
    session["admin_start"] = session["admin_sist"] = time.time()
    db.logg(con(), "admin_innlogget", {"admin_id": bruker["id"], "entra": bool(bruker["entra_oid"])},
            aktor=f"admin:{bruker['brukernavn']}")
    con().commit()
    return redirect(sikkerhet.trygg_neste(neste, url_for("admin")))


def _har_rolle_for(endepunkt: str | None, metode: str) -> bool:
    rolle = g.get("admin_rolle")
    if rolle == "system":
        return True
    if endepunkt in SYSTEM_ENDEPUNKTER:
        return False
    if rolle == "kursadmin":
        return True
    # lese
    if metode not in ("GET", "HEAD", "OPTIONS") and endepunkt not in LESE_TILLATTE_POST:
        return False
    return endepunkt not in KUN_KURSADMIN_ENDEPUNKTER


def krever_admin(f):
    @wraps(f)
    def inner(*a, **kw):
        if not _admin_okt_gyldig():
            return redirect(url_for("admin_login", neste=request.path))
        if not _har_rolle_for(request.endpoint, request.method):
            abort(403)
        return f(*a, **kw)
    return inner


def krever_admin_json(f):
    """Som krever_admin, men svarer med JSON og statuskode (401 uten innlogging, 403 uten rolle) i stedet for en
    viderekobling til innloggingssiden - for fetch fra siden (søkefeltets rullegardin). Ingen data lekker ved avvisning."""
    @wraps(f)
    def inner(*a, **kw):
        if not _admin_okt_gyldig():
            return _sok_json({"feil": "ikke_innlogget"}, 401)
        if not _har_rolle_for(request.endpoint, request.method):
            return _sok_json({"feil": "ingen_tilgang"}, 403)
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


# ======================= offentlig =======================

# Publiseringsvern (hardening-checkpoint foer skjemabygger). Kun disse statusverdiene gir OFFENTLIG tilgang til
# BAADE /kurs/<kode> (ordinaer paamelding) og /kurs/<kode>/gruppe (bedriftspaamelding) - GET og POST. Fail-closed:
# en statusverdi som IKKE staar i denne lista - ogsaa en fremtidig, ukjent en som en senere migrering skulle legge
# til i CHECK-constrainten i schema.sql - regnes som IKKE offentlig tilgjengelig, uten unntak (allow-list, ikke
# deny-list). Unntak, med hensikt: innloggede administratorer SER /kurs/<kode> (GET) uansett status - det er
# forhaandsvisningen i skjemabyggeren - men kan aldri sende inn skjemaet for et kurs som ikke er offentlig (se kursside).
OFFENTLIG_SYNLIGE_KURSSTATUSER = frozenset({"aapen", "full", "aktiv"})


def _kurs_offentlig_tilgjengelig(kurs) -> bool:
    return kurs["status"] in OFFENTLIG_SYNLIGE_KURSSTATUSER


def _skjema_snapshot(kurs) -> tuple:
    """Leser kursets skjemaoppsett EN gang og lager requestens ENE effektive skjema (fase 12C3): overstyringene av
    standardfeltene (kurs_skjemafelt) og kursets egne felt (kurs_ekstrafelt, skjemabyggeren).

    Snapshotet brukes til ALT i resten av requesten: rendring, hvilke POST-felt som leses, obligatorisk-validering,
    hva som lagres og ev. re-rendring ved feil. Det leses aldri paa nytt - endrer admin skjemaet mens en POST paagaar,
    fullfores POST-en etter sitt opprinnelige snapshot. Kaster skjemafelt.SkjemaLesefeil ved reell DB-lesefeil (aldri
    stille fallback til standardskjema). Returnerer (skjema, advarsler)."""
    les = db.hent_skjemaoverstyringer(con(), kurs["id"])
    egne = db.hent_ekstrafelt(con(), kurs["id"])
    return skjemafelt.effektivt_skjema(kurs, les, egne.felt), (*les.advarsler, *egne.advarsler)


def _offentlig_lenke(kurs) -> str:
    """Den faste adressen til paameldingsskjemaet: den deltakerne bruker, og som kan sendes til dem og legges paa ipr.no."""
    return f"{config.BASE_URL}{url_for('kursside', kode=kurs['kode'])}"


def _plasser_igjen(kurs) -> int | None:
    return None if kurs["kapasitet"] is None else kurs["kapasitet"] - db.antall_bekreftet(con(), kurs["id"])


def _render_paameldingsside(kurs, dager, f, plasser_igjen, skjema, **ekstra):
    """ENESTE vei til kurs.html - brukes av kursside() (GET, ogsaa naar admin forhaandsviser, og begge POST-feilstier).
    Tar imot requestens skjema-snapshot og leser ALDRI skjemaoppsettet selv. Sidens tekster (intro/knapp, 12C5)
    beregnes her fra SAMME kursrad som resten av requesten bruker."""
    ekstra.setdefault("forhandsvisning", False)
    ekstra.setdefault("admin_visning", False)
    return render_template("kurs.html", kurs=kurs, dager=dager, f=f, plasser_igjen=plasser_igjen, skjema=skjema,
                           side=paameldingsside.effektiv_side(kurs), offentlig_lenke=_offentlig_lenke(kurs), **ekstra)


@app.template_global()
def vis_naar_oppfylt(vis_naar, verdier) -> bool:
    """Til kurs.html: vises feltet med betingelsen `vis_naar` for de innsendte verdiene? (Samme regel som serveren.)"""
    return ekstrafelt.betingelse_oppfylt(vis_naar, verdier or {})


app.jinja_env.globals.update(AVKRYSSET=ekstrafelt.AVKRYSSET, BETALER_VALG=ekstrafelt.BETALER_VALG,
                             firmaopplysninger_mangler=firmaopplysninger.mangler, FIRMA_MERKE=firmaopplysninger.MERKE)
# Adressen (deltakerens private adresse): merket «Privat adresse mangler» (deltakerlisten, deltakervinduet, importens forhåndsvisning) og
# om filimporten krever adressen. Innstillingen leses fra miljøvariabelen når appen starter (config.py); en endring krever omstart.


def _er_anonymisert(p) -> bool:
    """Er personen anonymisert (retten til sletting)? Da står e-posten på @anonymisert.invalid (db.anonymiser_deltaker)."""
    return (p["epost"] or "").lower().endswith("@" + db.ANONYM_DOMENE)


def _adresse_oppfoelging(p) -> bool:
    """Skal varselboksen «Privat adresse mangler» vises i deltakervinduet for personen? Adressen må mangle helt eller delvis
    (skjemafelt.adresse_mangler), og personen kan ikke være anonymisert: en som har krevd sletting, skal aldri bes om en ny adresse."""
    return skjemafelt.adresse_mangler(p) and not _er_anonymisert(p)


app.jinja_env.globals.update(adresse_mangler=skjemafelt.adresse_mangler, adresse_oppfoelging=_adresse_oppfoelging,
                             ADRESSE_MANGLER_MERKE=skjemafelt.ADRESSE_MANGLER_MERKE,
                             ADRESSE_UFULLSTENDIG_MERKE=skjemafelt.ADRESSE_UFULLSTENDIG_MERKE, adresse_merke=skjemafelt.adresse_merke,
                             FAKTURA_HOLDT_ADRESSE=privatadresse.FAKTURA_HOLDT_GENERELT, holdt_overskrift=privatadresse.holdt_overskrift,
                             FAKTURA_HOLDT_TEKST=privatadresse.TEKST_HOLDT,
                             FAKTURA_VENTER_ADRESSE=privatadresse.FAKTURA_VENTER,
                             adresse_kreves_i_csv=lambda: config.ADRESSE_KREVES_I_CSV, kurs_viser_hpr=skjemafelt.vis_hpr)


def _skjema_utilgjengelig(kurs, forhandsvisning: bool = False):
    """Kontrollert 503 naar skjemakonfigurasjonen ikke kan leses. Ingen detaljer om feilen (SQL, sti, unntak) og ingen
    persondata - kun kursnavnet. Fail-closed: skjemaet vises IKKE med standardoppsett, og ingenting registreres."""
    return render_template("paamelding_utilgjengelig.html", kurs=kurs, forhandsvisning=forhandsvisning), 503


# Faste identitetsfelt som alltid finnes i skjemaet (validering: se kursside()). Deltakerens adresse er ogsaa alltid med
# (skjema.adressefelt).
_IDENTITETSFELT = ("fornavn", "etternavn", "epost", "samtykke")


def _tillatte_innsendte_verdier(form, skjema) -> dict:
    """Raa POST -> KUN feltene dette skjema-snapshotet faktisk viser (fase 12C3). Alt annet ignoreres fullstendig, uansett
    hva klienten sender: skjulte felt, HPR uten spesialistlop, sensitive felt paa digitale kurs, egne felt som ikke
    finnes eller er skjult, og hele faktura-/betalingsblokken naar den ikke vises. Verdiene er uendrede raa strenger
    (flervalg: liste med de avkryssede verdiene)."""
    ut = {k: form[k] for k in _IDENTITETSFELT if k in form}
    felt = [*skjema.adressefelt, *skjema.deltakerfelt, *skjema.sluttfelt, *skjema.sensitive_felt]
    if skjema.vis_fakturablokk:
        ut.update({k: form[k] for k in skjemafelt.FAKTURABLOKK.felt if k in form})
        felt += [*skjema.firmafelt]
    for x in felt:
        if x.nokkel in form:
            ut[x.nokkel] = form.getlist(x.nokkel) if x.type == "flervalg" else form[x.nokkel]
    return ut


def _advarsel_i_logg(a) -> dict:
    if isinstance(a, ekstrafelt.Advarsel):
        felt = f"{skjemafelt.EKSTRA_REF}{a.felt_id}" if a.felt_id else None
        return {"felt": felt, "egenskap": a.egenskap, "grunn": a.grunn}
    return {"felt": a.felt, "egenskap": a.egenskap, "grunn": a.grunn}


def _logg_skjemaadvarsler(kurs_id: int, advarsler) -> None:
    """PII-fritt: kun kurs_id, kjent felt (eller id-en til et eget felt), kjent egenskap og grunnkode - advarslene baerer
    aldri tekst eller ukjente feltnavn."""
    if advarsler:
        db.logg(con(), "skjemafelt_advarsel", {"kurs_id": kurs_id,
                                               "advarsler": [_advarsel_i_logg(a) for a in advarsler]})


@app.get("/helse")
def helse():
    """Helsesjekk for App Service (Health check path). 200 «ok» når databasen svarer og har kodens versjon, ellers 503.
    Gir aldri detaljer ut (feilen står i loggen via _krev_migrert_database/feilhåndteringen)."""
    try:
        c = con()
        c.execute("SELECT 1").fetchone()
        migreringer.kontroller(c)
    except Exception:  # noqa: BLE001 - enhver feil betyr «ikke frisk»
        return Response("ikke klar", status=503, mimetype="text/plain")
    return Response("ok", mimetype="text/plain", headers={"Cache-Control": "no-store"})


@app.get("/")
def forside():
    """Startsiden er innloggingen til Admin og ingenting annet: den enkle adressen som kan deles med alle som bruker systemet.
    Er man allerede innlogget, går man rett til Oversikten. Kurslisten som sto her før, er borte. Påmeldingen ligger på
    /kurs/<kode> (lenken legges på ipr.no). Kodesiden /innsjekk er fjernet: uten QR-kode registrerer deltakeren seg på Min side."""
    if _admin_okt_gyldig():
        return redirect(url_for("admin"))
    return _admin_innloggingsside()


def _tekst(f: dict, navn: str) -> str | None:
    """En innsendt tekstverdi uten mellomrom foran og bak, eller None (tom, ikke sendt, eller ikke tekst)."""
    verdi = f.get(navn)
    return (verdi.strip() or None) if isinstance(verdi, str) else None


@app.route("/kurs/<kode>", methods=["GET", "POST"])
def kursside(kode):
    """Den offentlige paameldingssiden - og samme side er admin sin forhaandsvisning (skjemabyggerens «Forhåndsvis»), saa
    adressen i nettleseren alltid er den som kan sendes til deltakerne og legges paa ipr.no.

    Offentlig tilgjengelige kurs (OFFENTLIG_SYNLIGE_KURSSTATUSER): alle kan se og sende skjemaet. Andre kurs (utkast,
    avsluttet, avlyst): 404 for alle andre enn innloggede administratorer, som ser siden med en merknad og UTEN
    innsending. POST mot et kurs som ikke er offentlig, er 404 for alle.

    Naar firma betaler, hentes firmanavn og adresse ALLTID paa nytt fra Enhetsregisteret ved innsending (det deltakeren
    ser i skjemaet, er bare en visning - det deltakeren sender av firmanavn og adresse, brukes ALDRI). Svarer ikke
    registeret, lagres bare organisasjonsnummeret: paameldingen gaar gjennom og merkes «Firmaopplysninger maa
    kontrolleres» (firmaopplysninger.py), og opplysningene hentes senere av administrator eller morgenjobben."""
    kurs = con().execute("SELECT * FROM kurs WHERE kode=?", (kode,)).fetchone() or abort(404)
    offentlig = _kurs_offentlig_tilgjengelig(kurs)
    admin_visning = request.method == "GET" and bool(session.get("admin_id")) and _admin_okt_gyldig()
    if not offentlig and not admin_visning:
        abort(404)
    # Skjema-snapshot FOER all validering og FOER foerste varige skriving (meld_paa) / eksterne sideeffekter (sveiper).
    try:
        skjema, advarsler = _skjema_snapshot(kurs)
    except skjemafelt.SkjemaLesefeil:
        return _skjema_utilgjengelig(kurs, forhandsvisning=not offentlig)
    dager = db.kursdager(con(), kurs["id"])
    if request.method == "GET":
        return _render_paameldingsside(kurs, dager, {}, _plasser_igjen(kurs), skjema, forhandsvisning=not offentlig,
                                       admin_visning=admin_visning)
    sikkerhet.krev_kvote("paamelding")
    # Herfra er `f` KUN de feltene snapshotet viser - raa request.form brukes ikke lenger.
    f = _tillatte_innsendte_verdier(request.form, skjema)
    # Skjemaet krever at deltakeren velger (required). Sendes valget ikke (eldre klienter), gjelder «betale privat» som foer.
    betaler = (f.get("betaler") or skjemafelt.PERSON) if skjema.vis_fakturablokk else skjemafelt.PERSON
    if betaler not in (skjemafelt.PERSON, skjemafelt.ORGANISASJON):
        betaler = None
    org = betaler == skjemafelt.ORGANISASJON
    fakturafelt = skjema.firmafelt if org else ()      # betaler deltakeren selv, er fakturaadressen hans egen adresse
    # Deltakerens private adresse (adresse, postnr, poststed) er ALLTID krav - ogsaa naar arbeidsgiver betaler
    felt_feil = skjemafelt.valider_adresse(f)
    for felt in (*skjema.deltakerfelt, *fakturafelt, *skjema.sluttfelt, *skjema.sensitive_felt):
        if felt.ekstra_id is None and felt.obligatorisk and not _tekst(f, felt.nokkel):
            felt_feil[felt.nokkel] = f"Fyll inn «{felt.label}»."
    if org and _tekst(f, "faktura_epost") and "@" not in f["faktura_epost"]:
        felt_feil["faktura_epost"] = "Skriv en gyldig e-postadresse for faktura."
    svar, svarfeil = ekstrafelt.tolk_svar(skjema.egne_felt, f)
    felt_feil.update(svarfeil)

    def meldinger(felter) -> list:
        return [felt_feil[x.nokkel] for x in felter if x.nokkel in felt_feil]

    feil = []
    if not (f.get("fornavn", "").strip() and f.get("etternavn", "").strip()) or "@" not in f.get("epost", ""):
        feil.append("Fyll inn fornavn, etternavn og gyldig e-post.")
    feil += meldinger(skjema.adressefelt)
    feil += meldinger(skjema.deltakerfelt)
    if betaler is None:
        feil.append("Velg om du betaler privat, eller om firma betaler.")
    orgnr = brreg.normaliser_orgnr(f.get("org_nr", "")) if org else ""
    if org and not brreg.gyldig_orgnr(orgnr):
        feil.append(firmaopplysninger.TEKST_UGYLDIG + ", eller søk opp firmaet og velg det i listen.")
    feil += meldinger(fakturafelt) + meldinger(skjema.sluttfelt) + meldinger(skjema.sensitive_felt)
    if not f.get("samtykke"):
        feil.append("Du må godta vilkår og personvernerklæring.")
    oppslag = None
    if org and not feil:
        # Den felles regelen (firmaopplysninger.py): firmanavn og adresse kommer bare fra registeret. Svarer det ikke,
        # beholdes organisasjonsnummeret, påmeldingen kan fullføres og merkes «Firmaopplysninger må kontrolleres», og
        # fakturaen venter. Deltakeren skriver ALDRI firmanavn eller adresse selv.
        oppslag = firmaopplysninger.slaa_opp(orgnr)
        if tekst := firmaopplysninger.avvisning(oppslag, med_soek=True):
            feil.append(tekst)
        else:     # vises ved en ev. senere feil (allerede påmeldt); tomt når registeret ikke svarte
            e = oppslag.enhet
            f.update(org_nr=oppslag.orgnr, org_navn=e.navn if e else "", org_adresse=e.adresse if e else "",
                     org_postnr=e.postnr if e else "", org_sted=e.poststed if e else "")
    registeret_svarte_ikke = oppslag is not None and oppslag.utfall == firmaopplysninger.UTILGJENGELIG
    if feil:
        for x in feil:
            flash(x, "feil")
        return _render_paameldingsside(kurs, dager, f, _plasser_igjen(kurs), skjema), 400
    # Verdiene lagres som de ble sendt (som før): ingen ny trimming. Felt skjemaet ikke viser, er ikke med i `f`.
    paamelding = {"betaler": betaler, "ehf": 0, "betaling": f.get("betaling", "samlet")}
    if org:
        paamelding |= {**firmaopplysninger.paamelding_felter(oppslag), "ehf": 1 if f.get("ehf") else 0}
        paamelding |= {k: f.get(k) or None for k in ("faktura_ref", "faktura_epost", "faktura_kommentar")}
    # Betaler deltakeren selv, kopierer db.meld_paa personens adresse til fakturaadressen. Betaler firma, brukes bare
    # firmaets adresse fra registeret - deltakerens private adresse lagres da bare paa personen.
    try:
        with db.transaksjon(con()):
            _logg_skjemaadvarsler(kurs["id"], advarsler)
            pid, status = db.meld_paa(
                con(), kurs["id"], epost=f["epost"], fornavn=f["fornavn"], etternavn=f["etternavn"],
                # None for skjulte felt: finn_eller_opprett_deltaker overskriver aldri med tom verdi, saa en eksisterende
                # persons telefon/arbeidssted/yrkestittel/HPR blir verken endret eller slettet av et kurs som skjuler dem.
                # Adressen er alltid med (paakrevd): en ny adresse overskriver den gamle, en tom aldri.
                deltaker={k: f.get(k) for k in ("telefon", "arbeidssted", "yrkestittel", "hpr_nr")}
                | skjemafelt.rens_adresse(f),
                paamelding=paamelding,
                sensitivt={"allergier": f.get("allergier"), "tilrettelegging": f.get("tilrettelegging")},
                svar=svar, idag=_idag(),
            )
            if registeret_svarte_ikke:
                firmaopplysninger.logg_ikke_hentet(con(), kurs["id"], pid)
    except db.Paameldingsfeil as e:
        flash(str(e), "feil")
        return _render_paameldingsside(kurs, dager, f, _plasser_igjen(kurs), skjema), 400
    # "Webhook": kjor sveipene med en gang (bekreftelse + faktura). Daglig jobb tar det som evt. feiler.
    sveiper.kjor(Kjoring(con(), idag=_idag()), pid)
    con().commit()
    return render_template("kvittering.html", kurs=kurs, status=status, fornavn=f["fornavn"].strip(),
                           firmaopplysninger_mangler=registeret_svarte_ikke)


def _json_svar(data: dict, status: int = 200):
    svar = make_response(data, status)
    svar.headers["Cache-Control"] = "no-store"
    return svar


@app.get("/kurs/<kode>/enhet")
def kurs_enhetsoppslag(kode):
    """Firmanavn og adresse fra Enhetsregisteret til paameldingsskjemaet (static/app.js): ?orgnr=... eller ?sok=...
    Bare for kurs der skjemaet kan vises (offentlig, eller admin som forhaandsviser). Takbegrenset per IP. Svarene er
    aapne data fra registeret; ingenting lagres, og organisasjonsnummer/soek logges aldri."""
    kurs = con().execute("SELECT status FROM kurs WHERE kode=?", (kode,)).fetchone() or abort(404)
    if not (_kurs_offentlig_tilgjengelig(kurs) or (session.get("admin_id") and _admin_okt_gyldig())):
        abort(404)
    sikkerhet.krev_kvote("enhetsoppslag")
    try:
        if "orgnr" in request.args:
            nr = brreg.normaliser_orgnr(request.args.get("orgnr", ""))
            if not brreg.gyldig_orgnr(nr):
                return _json_svar({"status": "ugyldig"})
            enhet = brreg.hent(nr)
            return _json_svar({"status": "funnet", "enhet": enhet.som_dict()} if enhet else {"status": "ikke_funnet"})
        return _json_svar({"status": "ok", "treff": [e.som_dict() for e in brreg.sok(request.args.get("sok", ""))]})
    except brreg.Utilgjengelig:
        return _json_svar({"status": "utilgjengelig"}, 503)


# ---------- bedriftspaamelding (fase 7) ----------

MAKS_DELTAKERE_GRUPPE = 30


def _adressefeil_tekst(feil: dict) -> str:
    """Feilene for en persons adresse (skjemafelt.valider_adresse) som en setning: «fyll inn adresse, postnummer og
    poststed.» naar alt mangler, ellers meldingene etter hverandre. Aldri selve verdiene."""
    if all(m.startswith("Fyll inn") for m in feil.values()):
        navn = [skjemafelt.REGISTER[k].label.lower() for k in feil]
        return "fyll inn " + (", ".join(navn[:-1]) + " og " + navn[-1] if len(navn) > 1 else navn[0]) + "."
    return " ".join(feil.values())


def _grupperad_liste(f) -> list[dict]:
    """Bygger deltakerraden-listen fra skjemaet, uten aa filtrere bort noe - brukes til aa vise
    skjemaet paa nytt akkurat slik brukeren skrev det, ved valideringsfeil."""
    kolonner = {k: f.getlist(f"deltaker_{k}") for k in ("fornavn", "etternavn", "epost", "telefon", "arbeidssted", "hpr",
                                                          *skjemafelt.ADRESSEFELT)}
    n = max(len(kolonner["fornavn"]), len(kolonner["etternavn"]))
    rader = [{k: (v[i] if i < len(v) else "") for k, v in kolonner.items()} for i in range(n)]
    for r in rader:
        r["hpr_nr"] = r.pop("hpr")
    return rader


@app.route("/kurs/<kode>/gruppe", methods=["GET", "POST"])
def kurs_gruppe(kode):
    kurs = con().execute("SELECT * FROM kurs WHERE kode=?", (kode,)).fetchone() or abort(404)
    if not _kurs_offentlig_tilgjengelig(kurs):
        abort(404)
    if request.method == "GET":
        return render_template("kurs_gruppe.html", kurs=kurs, f={}, deltakere=[{}], maks_deltakere=MAKS_DELTAKERE_GRUPPE)

    sikkerhet.krev_kvote("paamelding")
    f = request.form
    feil = []
    if kurs["status"] not in ("aapen", "full", "aktiv"):
        feil.append("Kurset er ikke åpent for påmelding.")
    if kurs["paameldingsfrist"] and _idag() > date.fromisoformat(kurs["paameldingsfrist"]):
        feil.append(f"Påmeldingsfristen ({kurs['paameldingsfrist']}) er passert.")
    if not (f.get("kontakt_fornavn", "").strip() and f.get("kontakt_etternavn", "").strip()) \
            or "@" not in f.get("kontakt_epost", ""):
        feil.append("Fyll inn kontaktpersonens fornavn, etternavn og en gyldig e-postadresse.")
    if not f.get("org_nr", "").strip():
        feil.append("Fyll inn organisasjonsnummer.")
    elif not brreg.gyldig_orgnr(brreg.normaliser_orgnr(f["org_nr"])):
        feil.append(firmaopplysninger.TEKST_UGYLDIG + ".")
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
        fornavn, etternavn = r["fornavn"].strip(), r["etternavn"].strip()
        deltaker_epost = r["epost"].strip().lower()
        if not fornavn and not etternavn and not deltaker_epost:
            continue
        if not (fornavn and etternavn) or "@" not in deltaker_epost:
            feil.append(f"Deltaker {i}: fyll inn fornavn, etternavn og en gyldig e-postadresse.")
            continue
        # Deltakerens private adresse er krav for HVER deltaker - ogsaa naar arbeidsgiver betaler
        if adressefeil := skjemafelt.valider_adresse(r):
            feil.append(f"Deltaker {i}: " + _adressefeil_tekst(adressefeil))
            continue
        deltaker_rader.append({"fornavn": fornavn, "etternavn": etternavn, "navn": db.fullt_navn(fornavn, etternavn),
                               "epost": deltaker_epost, "telefon": r["telefon"].strip() or None,
                               "arbeidssted": r["arbeidssted"].strip() or None,
                               "hpr_nr": (r["hpr_nr"].strip() or None) if hpr_synlig else None,
                               **skjemafelt.rens_adresse(r)})
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

    # Den felles regelen (firmaopplysninger.py): firmanavn og adresse kommer bare fra registeret. Er nummeret ukjent, får
    # kontaktpersonen beskjed. Svarer ikke registeret, går påmeldingen gjennom med organisasjonsnummeret alene, merket
    # «Firmaopplysninger må kontrolleres», og fakturaene venter.
    oppslag = firmaopplysninger.slaa_opp(f["org_nr"])
    if tekst := firmaopplysninger.avvisning(oppslag):
        flash(tekst, "feil")
        return render_template("kurs_gruppe.html", kurs=kurs, f=f, deltakere=rader_visning or [{}],
                               maks_deltakere=MAKS_DELTAKERE_GRUPPE), 400
    firma_felter = firmaopplysninger.paamelding_felter(oppslag)
    kontakt_fornavn, kontakt_etternavn = f["kontakt_fornavn"].strip(), f["kontakt_etternavn"].strip()
    kontakt = {
        "fornavn": kontakt_fornavn, "etternavn": kontakt_etternavn,
        "navn": db.fullt_navn(kontakt_fornavn, kontakt_etternavn), "epost": f["kontakt_epost"].strip().lower(),
        "telefon": f.get("kontakt_telefon", "").strip() or None, "firmanavn": firma_felter["org_navn"] or "",
        "org_nr": firma_felter["org_nr"], "faktura_ref": f.get("faktura_ref", "").strip() or None,
        "faktura_adresse": firma_felter["faktura_adresse"], "faktura_postnr": firma_felter["faktura_postnr"],
        "faktura_sted": firma_felter["faktura_sted"], "ehf": bool(f.get("ehf")),
    }
    # Det e-posten til kontaktpersonen ser: «firmaet» når registeret ikke svarte (firmanavnet lagres tomt)
    kontakt_visning = {**kontakt, "firmanavn": kontakt["firmanavn"] or "firmaet"}
    antall_totalt = len(deltaker_rader)

    # Preflight FOER noen varig sideeffekt (firmapaamelding/deltaker-registrering/deltakermail): kun det
    # ADMIN-REDIGERBARE innholdet (emne/innledning/avslutning) rendres her, med data som er kjent uansett hva
    # registreringen ender med. Den ferdige malteksten gjenbrukes ordrett i den faktiske kvitteringen lenger
    # ned - de faktiske utfallstallene (bekreftet/venteliste/feilet) er ALDRI koder og krever derfor ingen nytt
    # DB-oppslag der. En korrupt override oppdages dermed FOER firma/deltakere finnes, og ingenting registreres.
    try:
        firma_maltekst = maltekster.maltekst_for_utsending(
            con(), "firmapaamelding_kvittering", {"kontakt": kontakt_visning, "kurs": kurs, "antall_totalt": antall_totalt})
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
                    con(), kurs["id"], epost=rad["epost"], fornavn=rad["fornavn"], etternavn=rad["etternavn"],
                    deltaker={"telefon": rad["telefon"], "arbeidssted": rad["arbeidssted"], "hpr_nr": rad["hpr_nr"],
                              **{k: rad[k] for k in skjemafelt.ADRESSEFELT}},
                    paamelding={"betaler": "organisasjon", **firma_felter,
                                "faktura_epost": kontakt["epost"], "faktura_ref": kontakt["faktura_ref"],
                                "ehf": 1 if kontakt["ehf"] else 0, "kilde": "gruppe"},
                    idag=_idag())
                if oppslag.utfall == firmaopplysninger.UTILGJENGELIG:
                    firmaopplysninger.logg_ikke_hentet(con(), kurs["id"], pid)
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
    emne, html = epost.render("firmapaamelding_kvittering", maltekst=firma_maltekst, kontakt=kontakt_visning, kurs=kurs,
                              kvittering_url=kvittering_url, antall_totalt=antall_totalt,
                              antall_bekreftet=antall_bekreftet, antall_venteliste=antall_venteliste,
                              antall_feilet=antall_feilet)
    Kjoring(con(), idag=_idag()).send_ferdigrendret_en_gang(      # kvitteringslenken er et token: ingen kopi
        f"firmapaamelding:{firma['id']}", kontakt["epost"], "firmapaamelding_kvittering", emne, html,
        kurs_id=kurs["id"], lagre_kopi=False)
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
        """SELECT r.navn, r.feilmelding, p.status AS paamelding_status, p.betaler, p.org_navn, p.org_nr
           FROM firmapaamelding_rad r LEFT JOIN paamelding p ON p.id=r.paamelding_id
           WHERE r.firmapaamelding_id=? ORDER BY r.id""", (firma["id"],)).fetchall()
    # Vises så lenge en av påmeldingene venter på firmaopplysninger fra registeret (forsvinner når de er hentet)
    firma_mangler = any(r["paamelding_status"] and firmaopplysninger.mangler(r) for r in rader)
    return render_template("kurs_gruppe_kvittering.html", kurs=kurs, firma=firma, rader=rader,
                           firmaopplysninger_mangler=firma_mangler)


# ---------- innsjekk ----------

# Identifisering (SPEC §10.2): innlogget økt hvis den finnes, ellers e-post - aldri navn (navn er ikke unike, og en «velg navnet ditt»-liste
# ville vist hvem som er påmeldt til alle med QR-lenken). E-post gir ALDRI innlogging. «Bare én gang per dag» ligger i databasen
# (oppmote har primærnøkkel påmelding + kursdag). Sidene får aldri innsjekk-kode eller -token, bare det de skal vise.

def _sjekk_inn(kursdag, epost_: str | None = None, deltaker_id: int | None = None, kilde="qr"):
    """Tynn innpakning rundt deltakerside.registrer_qr (navn og signatur som før): (status, tekst), der status er «ok» eller «feil»."""
    res = deltakerside.registrer_qr(con(), kursdag, _idag(), deltaker_id=deltaker_id, epost=epost_, kilde=kilde)
    con().commit()
    return ("ok" if res.status in ("ny", "allerede") else "feil"), deltakerside.tekst_for(res, innlogget=bool(deltaker_id))


def _innlogget_deltaker():
    """Den innloggede deltakeren (id, fornavn, epost), eller None (ingen økt, eller økten tilhører en slettet person)."""
    did = session.get("deltaker_id")
    return con().execute("SELECT id, fornavn, epost FROM deltaker WHERE id=?", (did,)).fetchone() if did else None


def _innsjekk_resultat(kd, res, deltaker, epost_: str):
    """Resultatsiden: hake eller utropstegn, teksten fra tekst_for, hvilken dag det gjelder, og hva deltakeren kan gjøre videre."""
    innlogget = deltaker is not None
    ok = res.status in ("ny", "allerede")
    har_side = ok and deltakerside.har_side(con(), kd["kurs_id"], _idag())
    k = deltakerside.kursinfo_for_innsjekk(con(), kd, _idag())
    if res.status == "ikke_paa_samlingen":           # dagnummer og fremdrift gjelder ikke en dag deltakeren ikke er satt opp på
        k = {**k, "dag_nr": None, "dag_av": None, "dag_tekst": ""}
    elif ok and res.kursdag_nr and res.kursdag_av:   # deltakerens EGNE dagnumre (en ekstradeltaker på noen samlinger: «Dag 1 av 2»)
        k = {**k, "dag_nr": res.kursdag_nr, "dag_av": res.kursdag_av, "dag_tekst": f"Dag {res.kursdag_nr} av {res.kursdag_av}"}
    return render_template("innsjekk_resultat.html", status="ok" if ok else "feil", res=res, k=k, innlogget=innlogget, har_side=har_side,
                           tekst=deltakerside.tekst_for(res, innlogget=innlogget),
                           maskert=deltakerside.maskert_epost(epost_) if ok and not innlogget else None,
                           epost=epost_ if har_side and not innlogget else "", neste=url_for("deltakerside", kode=k["kode"]))


@app.route("/innsjekk/<token>", methods=["GET", "POST"])
def innsjekk_qr(token):
    """QR-koden i kurslokalet. GET skriver aldri. POST registrerer oppmøtet (én gang per dag) - innlogget med ett trykk, ellers med e-post.
    Det gjør deg aldri innlogget: «husk meg» finnes ikke."""
    kd = con().execute("SELECT id, kurs_id, dato FROM kursdag WHERE innsjekk_token=?", (token,)).fetchone() or abort(404)
    deltaker = _innlogget_deltaker()
    if config.INNSJEKK_KREVER_INNLOGGING and not deltaker:      # aldri registrering på en e-postadresse alene (også ikke ved POST)
        k = deltakerside.kursinfo_for_innsjekk(con(), kd, _idag())
        return render_template("innsjekk.html", k=k, deltaker=None, tid=None, har_side=False, maskert=None,
                               krever_innlogging=True, neste=url_for("innsjekk_qr", token=token))
    if request.method == "POST":
        sikkerhet.krev_kvote("innsjekk_qr", f"{sikkerhet.klient_ip()}|{token}")
        epost_ = "" if deltaker else (request.form.get("epost") or "").strip().lower()
        if not deltaker:
            sikkerhet.krev_kvote("innsjekk_qr_epost", f"{epost_}|{sikkerhet.klient_ip()}")
        res = deltakerside.registrer_qr(con(), kd, _idag(), deltaker_id=deltaker["id"] if deltaker else None, epost=epost_)
        con().commit()
        return _innsjekk_resultat(kd, res, deltaker, epost_)
    k = deltakerside.kursinfo_for_innsjekk(con(), kd, _idag(), deltaker["id"] if deltaker else None)
    tid = deltakerside.registrert_tid(con(), deltaker["id"], kd) if deltaker and k["apen_i_dag"] else None
    return render_template("innsjekk.html", k=k, deltaker=deltaker, tid=tid,
                           ikke_paa_tekst=deltakerside.TEKST_IKKE_PAA_SAMLINGEN,
                           har_side=bool(tid) and deltakerside.har_side(con(), kd["kurs_id"], _idag()),
                           maskert=deltakerside.maskert_epost(deltaker["epost"]) if deltaker else None)


# ---------- Min side ----------

_ADMIN_NOKLER = ("admin_id", "admin_brukernavn", "admin_navn", "admin_start", "admin_sist", "admin_rolle")
# Deltakerøkten: hvem, på hvilket nivå, og (for nivået «lenke») hvilken lenke økten kom inn med (påmelding og versjon)
_DELTAKEROKT_NOKLER = ("deltaker_id", "deltaker_niva", "min_side_pid", "min_side_ver")


@app.before_request
def _avslutt_ugyldig_lenkeokt():
    """En økt som kom inn med den personlige lenken (nivå «lenke») gjelder bare så lenge lenken gjelder. Stenger eller fornyer en administrator
    lenken, rettes e-postadressen, eller utløper lenken (30 dager etter siste kursdag), avsluttes økten ved neste forespørsel: cookien ligger hos
    nettleseren og kan ellers ikke trekkes tilbake. En økt på nivå «epost» (deltakeren har vist at hen når e-posten sin) berøres ikke. Mangler
    økten lenkens id eller versjon, avvises den (lukket som standard)."""
    if session.get("deltaker_niva") != minside.NIVAA_LENKE or request.endpoint in (None, "static", "helse"):
        return None
    pid, versjon = session.get("min_side_pid"), session.get("min_side_ver")
    res = minside.kontroller(con(), pid, versjon, _idag()) if isinstance(pid, int) and isinstance(versjon, int) else None
    if res is None or not res.ok or res.deltaker_id != session.get("deltaker_id"):
        for n in _DELTAKEROKT_NOKLER:
            session.pop(n, None)
    return None


def _ny_deltakersesjon(deltaker_id: int, niva: str = minside.NIVAA_EPOST, paamelding_id: int | None = None, versjon: int | None = None) -> None:
    """Ny deltakerøkt (mot session fixation) uten å logge ut en administrator i samme nettleser (speiler _logg_inn_admin): admin-økten
    og demoens spolte dato beholdes. `niva`: «epost» (innlogget med engangslenke på e-post, alt) eller «lenke» (kom inn med den
    personlige lenken til Min side: ikke privatadresse, fakturaer og kursbevis før e-posten er bekreftet, se kurs/minside.py). Nivået «lenke» husker
    hvilken lenke (påmelding og versjon) økten kom inn med, så den kan avsluttes når lenken stenges, fornyes eller utløper."""
    behold = {n: session[n] for n in (*_ADMIN_NOKLER, "demo_dato") if n in session}
    session.clear()
    session.update(behold)
    session.permanent = True
    session["deltaker_id"] = deltaker_id
    session["deltaker_niva"] = niva
    if niva == minside.NIVAA_LENKE:
        session["min_side_pid"], session["min_side_ver"] = paamelding_id, versjon


def _deltaker_full() -> bool:
    """Er deltakeren innlogget med e-postlenke (alt), og ikke bare med den personlige lenken? Økter fra før nivåene fantes er e-postøkter."""
    return session.get("deltaker_niva", minside.NIVAA_EPOST) == minside.NIVAA_EPOST


def _landing_url(deltaker_id: int) -> str:
    """Dit en deltaker sendes etter innlogging uten `neste`: kurssiden hvis det er ett tydelig kurs, ellers Min side."""
    kode = deltakerside.landing(con(), deltaker_id, _idag())
    return url_for("deltakerside", kode=kode) if kode else url_for("min_side")


def _send_innloggingslenke(d, neste: str) -> str:
    """Lager en engangslenke (30 minutter, kan brukes én gang) til deltakeren og sender den på e-post. Lenken går ikke via utsendingsmotoren
    og lagres aldri i e-posthistorikken. Returnerer lenken (demoen viser den)."""
    token = secrets.token_urlsafe(32)
    con().execute("INSERT INTO innlogging_token (token, deltaker_id, utloper) VALUES (?,?,?)",
                  (token, d["id"], (datetime.now() + timedelta(minutes=30)).isoformat()))
    con().commit()
    lenke = f"{config.BASE_URL}{url_for('logg_inn_token', token=token, neste=neste or None)}"   # url_for prosentkoder
    emne, html = epost.render("innlogging", fornavn=d["fornavn"], lenke=lenke)
    epost.send(d["epost"], emne, html)
    return lenke


@app.route("/logg-inn", methods=["GET", "POST"])
def logg_inn():
    # `neste` (en sti på dette nettstedet) følger med hele veien: lenke fra terapiakademiet.no -> skjulte felt -> e-postlenken -> mottak.
    neste = sikkerhet.trygg_neste(request.form.get("neste") or request.args.get("neste"), "")
    if request.method == "GET" and session.get("deltaker_id"):                       # allerede innlogget: hopp videre
        if con().execute("SELECT 1 FROM deltaker WHERE id=?", (session["deltaker_id"],)).fetchone():
            return redirect(neste or _landing_url(session["deltaker_id"]))
        session.pop("deltaker_id", None)                                             # gammel økt for en slettet person
    if request.method == "POST":
        adr = request.form.get("epost", "").strip().lower()
        sikkerhet.krev_kvote("innloggingslenke")
        sikkerhet.krev_kvote("innloggingslenke_epost", adr)
        d = con().execute("SELECT * FROM deltaker WHERE epost=?", (adr,)).fetchone()
        if d:
            lenke = _send_innloggingslenke(d, neste)
            if config.DEMO:
                flash(Markup('Demo: innloggingslenken er «sendt». <a href="{}">Klikk her for å logge inn</a>.').format(lenke), "info")
        # Samme svar uansett – avslorer ikke hvem som er registrert
        return render_template("logg_inn.html", sendt=True, neste=neste)
    return render_template("logg_inn.html", sendt=False, neste=neste)


@app.get("/logg-inn/<token>")
def logg_inn_token(token):
    neste = sikkerhet.trygg_neste(request.args.get("neste"), "")                   # valideres også her: aldri stol på lenken
    t = con().execute("SELECT * FROM innlogging_token WHERE token=?", (token,)).fetchone()
    if not t or t["brukt"] or t["utloper"] < datetime.now().isoformat():
        flash("Lenken er utløpt eller allerede brukt. Be om en ny.", "feil")
        return redirect(url_for("logg_inn", neste=neste or None))                   # neste bevares
    con().execute("UPDATE innlogging_token SET brukt=1 WHERE token=?", (token,))
    con().commit()
    _ny_deltakersesjon(t["deltaker_id"], minside.NIVAA_EPOST)   # ny identitet -> ny sesjon (mot session fixation), admin-økten står
    return redirect(neste or _landing_url(t["deltaker_id"]))


@app.get("/min/<token>")
def min_side_lenke(token):
    """Den personlige lenken til Min side (fra e-postene): åpner siden uten ny innlogging, på nivået «lenke» (se kurs/minside.py). Lenken
    er signert og lagres ikke. Ugyldig, stengt eller utløpt lenke gir samme tekst for alle, med innlogging som vei videre. Deltakeren som
    alt er innlogget med e-post som samme person, beholder det høyere nivået."""
    sikkerhet.krev_kvote("min_side_lenke")
    res = minside.slaa_opp(con(), token, _idag())
    if not res.ok:
        return render_template("min_side_lenke_ugyldig.html"), 404
    if not (session.get("deltaker_id") == res.deltaker_id and _deltaker_full()):
        _ny_deltakersesjon(res.deltaker_id, minside.NIVAA_LENKE, res.paamelding_id, res.versjon)
    return redirect(url_for("deltakerside", kode=res.kurs_kode))


@app.post("/bekreft-epost")
@krever_deltaker
def bekreft_epost():
    """«Bekreft e-posten din»: sender deltakeren (fra økten, aldri fra skjemaet) en engangslenke på e-post. Etter klikket er økten på nivået
    «epost»: privatadresse, fakturaer og kursbevis vises. Samme takbegrensning som innloggingen. Svaret nevner bare en delvis skjult adresse."""
    neste = sikkerhet.trygg_neste(request.form.get("neste"), "")
    d = con().execute("SELECT * FROM deltaker WHERE id=?", (session["deltaker_id"],)).fetchone() or abort(404)
    if str(d["epost"]).lower().endswith(deltakerside.ANONYM_DOMENE):
        abort(404)
    sikkerhet.krev_kvote("innloggingslenke")
    sikkerhet.krev_kvote("innloggingslenke_epost", d["epost"])
    lenke = _send_innloggingslenke(d, neste)
    flash(f"Vi har sendt en lenke til {deltakerside.maskert_epost(d['epost'])}. Klikk på den for å se privatadresse, fakturaer og kursbevis.", "ok")
    if config.DEMO:
        flash(Markup('Demo: lenken er «sendt». <a href="{}">Klikk her for å bekrefte</a>.').format(lenke), "info")
    return redirect(neste or url_for("min_side"))


@app.get("/logg-ut")
def logg_ut():
    if session.get("admin_id"):
        for n in _DELTAKEROKT_NOKLER:                   # bare deltakerøkten; admin-økten står (admin har egen «Logg ut»)
            session.pop(n, None)
        return redirect(url_for("forside"))             # innlogget administrator: startsiden sender videre til Oversikten
    session.clear()
    return redirect(url_for("logg_inn"))                # deltakeren: tilbake til deltakerinnloggingen, ikke til admin-innloggingen


@app.get("/min-side")
@krever_deltaker
def min_side():
    """Deltakerens inngang: «Mine kurs» (med «Åpne Min side» og «Registrer oppmøte i dag») og «Min konto» (fakturaer, dokumenter, timer).
    Innholdet ligger på kurssiden til hvert kurs; denne siden sender aldri videre (landing skjer bare rett etter innlogging).
    Eksplisitte kolonner: aldri hele deltakerraden (adresse m.m.) eller intern kommentar til malen."""
    did = session["deltaker_id"]
    full = _deltaker_full()
    deltaker = con().execute("SELECT id, fornavn, navn, epost, arbeidssted FROM deltaker WHERE id=?", (did,)).fetchone()
    paameldinger = con().execute(
        """SELECT p.id, p.kurs_id, p.status, p.betaling, p.ekstradeltaker_ts, k.navn, k.kode, k.type, k.sted, k.zoom_url,
                  k.sharepoint_mappe, k.status AS kursstatus
           FROM paamelding p JOIN kurs k ON k.id=p.kurs_id
           WHERE p.deltaker_id=? AND p.status!='avmeldt' ORDER BY p.opprettet DESC""", (did,)).fetchall()
    info = deltakerside.min_side_info(con(), did, paameldinger, _idag())
    paameldinger = sorted(paameldinger, key=lambda p: info[p["kurs_id"]]["i_dag"] is None)     # kurs med kursdag i dag øverst, ellers nyeste først (stabilt)
    melding = deltakerside_ruter.hent_oppmote_melding()
    kursliste = []
    for p in paameldinger:
        egne = {d["id"] for d in db.paameldingens_kursdager(con(), p)}         # bare dagene deltakeren er på (ekstradeltaker: valgte samlinger)
        dager = [d for d in con().execute(
            """SELECT kd.id, kd.dato, o.kilde FROM kursdag kd LEFT JOIN oppmote o ON o.kursdag_id=kd.id AND o.paamelding_id=?
               WHERE kd.kurs_id=? ORDER BY kd.dato""", (p["id"], p["kurs_id"])).fetchall() if d["id"] in egne]
        ks = info[p["kurs_id"]]
        # Har kurset en kursside som deltakeren kommer inn på, står kursholders filer der (og SharePoint spørres ikke herfra)
        filer = (sharepoint.list_filer(f"{p['sharepoint_mappe']}/Presentasjoner")
                 if p["sharepoint_mappe"] and p["status"] == "bekreftet" and not ks["kan_apne"]
                 and deltakerside.materiell_apent(con(), p["kurs_id"], _idag()) else [])
        fakturaer = con().execute(
            """SELECT f.faktura_nr, f.belop_nok, f.status, kd.dato FROM faktura f LEFT JOIN kursdag kd ON kd.id=f.kursdag_id
               WHERE f.paamelding_id=? ORDER BY kd.dato, f.id""", (p["id"],)).fetchall() if full else []
        kursliste.append({"p": p, "dager": dager, "filer": filer, "fakturaer": fakturaer, "kursside": ks, "i_dag": ks["i_dag"],
                          "periode": kursdatoer.datoliste([date.fromisoformat(str(d["dato"])) for d in dager]) if dager else "",
                          "melding": {"niva": melding["niva"], "tekst": melding["tekst"]} if melding and melding["kode"] == p["kode"] else None})
    dokumenter = con().execute(
        """SELECT d.id, d.tittel, d.type, d.opprettet, k.navn AS kursnavn FROM dokument d LEFT JOIN kurs k ON k.id=d.kurs_id
           WHERE d.publisert=1 AND (d.deltaker_id=? OR (d.deltaker_id IS NULL AND d.kurs_id IN
                 (SELECT kurs_id FROM paamelding WHERE deltaker_id=? AND status='bekreftet')))
           ORDER BY d.opprettet DESC""", (did, did)).fetchall() if full else []
    lop = {r["spesialistlop"]: timer_i_lop(con(), did, r["spesialistlop"]) for r in con().execute(
        """SELECT DISTINCT k.spesialistlop FROM paamelding p JOIN kurs k ON k.id=p.kurs_id
           WHERE p.deltaker_id=? AND k.spesialistlop IS NOT NULL""", (did,))} if full else {}
    antall_fakturaer = sum(len(x["fakturaer"]) for x in kursliste)
    ubetalt = sum(1 for x in kursliste for fak in x["fakturaer"] if fak["status"] in ("opprettet", "sendt"))
    return render_template("min_side.html", deltaker=deltaker, kursliste=kursliste, dokumenter=dokumenter, lop=lop, full=full,
                           antall_fakturaer=antall_fakturaer, antall_ubetalt=ubetalt,
                           antall_kursbevis=sum(1 for d in dokumenter if d["type"] == "kursbevis"))


def _har_tilgang_kurs(did: int, kurs_id: int) -> bool:
    return con().execute("SELECT 1 FROM paamelding WHERE deltaker_id=? AND kurs_id=? AND status='bekreftet'",
                         (did, kurs_id)).fetchone() is not None


@app.get("/dokument/<int:dok_id>")
def dokument(dok_id):
    d = con().execute("SELECT * FROM dokument WHERE id=?", (dok_id,)).fetchone() or abort(404)
    did = session.get("deltaker_id")
    admin = bool(session.get("admin_id")) and _admin_okt_gyldig()      # aldri rå session.get("admin_id"): utløpt eller deaktivert økt slipper ikke inn
    lov = admin or (did and _deltaker_full() and (d["deltaker_id"] == did or
                                                   (d["deltaker_id"] is None and _har_tilgang_kurs(did, d["kurs_id"]))))
    if not lov:
        abort(403)
    if d["url"] == "db:":                               # laget av systemet selv (kursbevis) - lagret i databasen
        innhold = con().execute("SELECT mimetype, innhold FROM dokument_innhold WHERE dokument_id=?",
                                (dok_id,)).fetchone() or abort(404)
        return Response(innhold["innhold"], mimetype=innhold["mimetype"])
    if d["url"].startswith("lokal:"):                   # eldre kursbevis som ennå ikke er migrert (migrering 4)
        rot = (config.ROT / "data").resolve()
        sti = (config.ROT / d["url"][6:]).resolve()
        if rot not in sti.parents or not sti.is_file():
            abort(404)
        return Response(sti.read_bytes(), mimetype="text/html" if sti.suffix == ".html" else "application/octet-stream")
    if d["url"].startswith("sp:"):
        return _send_fil(d["url"][3:])
    try:
        return redirect(sikkerhet.trygt_url_felt(d["url"]))
    except ValueError:
        abort(404)


@app.get("/materiell/<int:kurs_id>/<path:navn>")
@krever_deltaker
def materiell(kurs_id, navn):
    kurs = con().execute("SELECT * FROM kurs WHERE id=?", (kurs_id,)).fetchone() or abort(404)
    if not _har_tilgang_kurs(session["deltaker_id"], kurs_id):
        abort(403)
    if not kurs["sharepoint_mappe"] or "/" in navn or "\\" in navn or navn in (".", "..") or "\x00" in navn:
        abort(404)                                      # aldri sti-deler i filnavnet
    if not deltakerside.materiell_apent(con(), kurs_id, _idag()):
        abort(404)                                      # kurssiden er tatt ned eller stengt: da er kursholders filer også stengt
    return _send_fil(f"{kurs['sharepoint_mappe']}/Presentasjoner/{navn}")


def _send_fil(sti: str):
    try:
        innhold = sharepoint.hent_fil(sti)
    except (FileNotFoundError, PermissionError):
        abort(404)
    except Exception as e:  # noqa: BLE001 - SharePoint utilgjengelig: si fra, ikke krasj
        logging.getLogger("kurs.integrasjoner").warning("Henting fra SharePoint feilet: %s", sikker_feiltekst(e))
        abort(503)
    filnavn = secure_filename(sti.rsplit("/", 1)[-1]) or "fil"
    return Response(innhold, mimetype="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{filnavn}"', "X-Content-Type-Options": "nosniff"})


# ---------- levering av materiell (kursholder) ----------

MATERIELL_FILTYPER = frozenset({"pdf", "pptx", "ppt", "docx", "doc", "xlsx", "odp", "odt", "txt", "zip", "key"})


@app.route("/lever/<int:krav_id>/<signatur>", methods=["GET", "POST"])
def lever(krav_id, signatur):
    """Kursholder laster opp materiell. Lenken er signert (ingen innlogging for eksterne kursholdere), filnavnet renses,
    kun kjente filtyper og en stoerrelsesgrense godtas, og opplastinger takbegrenses."""
    if not lenker.signatur_ok(signatur, "lever", krav_id):
        abort(404)
    m = con().execute("SELECT m.*, k.navn AS kursnavn, k.kode, k.sharepoint_mappe FROM materiell_krav m "
                      "JOIN kurs k ON k.id=m.kurs_id WHERE m.id=?", (krav_id,)).fetchone() or abort(404)
    if request.method == "POST":
        sikkerhet.krev_kvote("lever")
        request.max_content_length = MATERIELL_MAKS_BYTES + 64 * 1024
        try:
            fil = request.files.get("fil")
        except RequestEntityTooLarge:
            flash(f"Filen er for stor (maks {MATERIELL_MAKS_BYTES // (1024 * 1024)} MB).", "feil")
            return render_template("lever.html", m=m), 413
        filnavn = secure_filename(fil.filename) if fil and fil.filename else ""
        endelse = filnavn.rsplit(".", 1)[-1].lower() if "." in filnavn else ""
        if not filnavn:
            flash("Velg en fil.", "feil")
        elif endelse not in MATERIELL_FILTYPER:
            flash("Filtypen støttes ikke. Last opp PDF, PowerPoint, Word, Excel eller en zip-fil.", "feil")
        else:
            innhold = fil.stream.read(MATERIELL_MAKS_BYTES + 1)
            if len(innhold) > MATERIELL_MAKS_BYTES:
                flash(f"Filen er for stor (maks {MATERIELL_MAKS_BYTES // (1024 * 1024)} MB).", "feil")
                return render_template("lever.html", m=m), 413
            mappe = m["sharepoint_mappe"] or _opprett_sharepoint_mappe(m["kurs_id"], m["kode"])
            try:
                if not mappe:
                    raise RuntimeError("SharePoint-mappen mangler")
                sharepoint.last_opp(f"{mappe}/Presentasjoner", filnavn, innhold)
            except Exception as e:  # noqa: BLE001 - opplastingen kan trygt proeves igjen (samme sti overskrives)
                db.logg(con(), "materiell_opplasting_feilet", {"krav_id": krav_id, "feil": sikker_feiltekst(e)},
                        aktor=f"materiell:{krav_id}")
                con().commit()
                flash("Filen kunne ikke lastes opp akkurat nå. Prøv igjen litt senere.", "feil")
                return render_template("lever.html", m=m), 503
            con().execute("UPDATE materiell_krav SET levert_ts=? WHERE id=?", (db.naa_utc(), krav_id))
            db.logg(con(), "materiell_levert", {"krav_id": krav_id, "fil": filnavn}, aktor=f"materiell:{krav_id}")
            con().commit()
            flash("Takk! Filen er lastet opp og er nå tilgjengelig for deltakerne på Min side.", "ok")
            return redirect(url_for("lever", krav_id=krav_id, signatur=signatur))
    return render_template("lever.html", m=m)


# ======================= admin =======================

@app.route("/admin/logg-inn", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if not config.ADMIN_LOKAL_INNLOGGING:
            abort(404)                                  # drift uten noedbrukere: kun Entra
        brukernavn = request.form.get("brukernavn", "")
        sikkerhet.krev_kvote("admin_login")
        sikkerhet.krev_kvote("admin_login_bruker", brukernavn)
        bruker = db.verifiser_admin(con(), brukernavn, request.form.get("passord", ""))
        if bruker and not bruker["entra_oid"]:          # Entra-brukere har ikke lokalt passord
            return _logg_inn_admin(bruker, request.args.get("neste"))
        db.logg(con(), "admin_innlogging_avvist", {"brukernavn_hash": hashlib.sha256(
            brukernavn.strip().lower().encode()).hexdigest()[:16]})
        con().commit()
        flash("Feil brukernavn eller passord.", "feil")
    return _admin_innloggingsside()


def _admin_innloggingsside():
    """Innloggingen til Admin: siden på /admin/logg-inn og samme side som startsiden (/). Skjemaet sendes alltid til /admin/logg-inn."""
    return render_template("admin_login.html", entra=entra.aktiv(), lokal=config.ADMIN_LOKAL_INNLOGGING,
                           neste=sikkerhet.trygg_neste(request.args.get("neste"), ""))


@app.route("/admin/brukere", methods=["GET", "POST"])
@krever_admin
def admin_brukere():
    if request.method == "POST":
        f = request.form
        rolle = f.get("rolle", "kursadmin")
        if not f.get("navn", "").strip() or not f.get("brukernavn", "").strip() or not f.get("passord"):
            flash("Fyll inn navn, brukernavn og passord.", "feil")
        elif len(f["passord"]) < 12:
            flash("Passordet må være minst 12 tegn.", "feil")
        elif rolle not in db.ROLLER:
            flash("Ugyldig rolle.", "feil")
        else:
            try:
                db.opprett_admin_bruker(con(), f["brukernavn"], f["navn"], f["passord"], aktor=_aktor(), rolle=rolle)
                con().commit()
                flash(f"Brukeren «{f['brukernavn'].strip().lower()}» er opprettet som {db.ROLLE_NAVN[rolle].lower()}.", "ok")
            except db.IntegritetsFeil:
                con().rollback()
                flash("Det finnes allerede en bruker med det brukernavnet.", "feil")
        return redirect(url_for("admin_brukere"))
    brukere = con().execute("SELECT * FROM admin_bruker ORDER BY navn").fetchall()
    return render_template("admin_brukere.html", brukere=brukere, roller=db.ROLLER, entra=entra.aktiv(),
                           lokal=config.ADMIN_LOKAL_INNLOGGING, innloggingslenke=config.BASE_URL.rstrip("/") + "/")


@app.post("/admin/brukere/<int:bid>/rolle")
@krever_admin
def admin_bruker_rolle(bid):
    try:
        endret = db.sett_admin_rolle(con(), bid, request.form.get("rolle", ""), aktor=_aktor())
        con().commit()
    except db.RolleFeil as e:
        con().rollback()
        flash(str(e), "feil")
        return redirect(url_for("admin_brukere"))
    flash("Rollen er endret." if endret else "Rollen var allerede satt.", "ok")
    return redirect(url_for("admin_brukere"))


@app.post("/admin/brukere/<int:bid>/deaktiver")
@krever_admin
def admin_bruker_deaktiver(bid):
    bruker = con().execute("SELECT * FROM admin_bruker WHERE id=?", (bid,)).fetchone() or abort(404)
    if bid == session.get("admin_id"):
        flash("Du kan ikke deaktivere din egen bruker.", "feil")
        return redirect(url_for("admin_brukere"))
    if bruker["aktiv"] and bruker["rolle"] == "system" and db.antall_aktive_systemadmin(con()) <= 1:
        flash("Den siste aktive systemadministratoren kan ikke deaktiveres.", "feil")
        return redirect(url_for("admin_brukere"))
    con().execute("UPDATE admin_bruker SET aktiv = 1 - aktiv WHERE id=?", (bid,))
    db.logg(con(), "admin_bruker_endret", {"id": bid, "aktiv": 0 if bruker["aktiv"] else 1}, aktor=_aktor())
    con().commit()
    flash("Brukeren er deaktivert." if bruker["aktiv"] else "Brukeren er aktivert igjen.", "ok")
    return redirect(url_for("admin_brukere"))


@app.get("/admin")
@krever_admin
def admin():
    """Oversikten er forsiden i Admin: søk etter deltaker og kurs, det som trenger oppfølging, og kurslisten med filtre
    (Aktiviteter er slått sammen med denne siden). «Siste hendelser» er tatt bort."""
    mangler = con().execute(
        """SELECT m.*, k.kursnr, k.kode, k.navn AS kursnavn FROM materiell_krav m JOIN kurs k ON k.id=m.kurs_id
           WHERE m.levert_ts IS NULL ORDER BY m.frist""").fetchall()
    # Levende telling fra NAAVAERENDE tilstand (ikke kumulativ hendelseslogg): uavklarte e-post-/faktura-
    # operasjoner som vi bevisst IKKE prover automatisk paa nytt. Samme staleness-grense som motoren.
    uavklart = db.uavklarte_operasjoner(con())
    if sjekklister.synk_kurs(con(), _idag()):         # påminnelsen tar med kursene i systemet med riktige datoer
        con().commit()
    return render_template("admin.html", mangler=mangler, uavklart=uavklart, statistikk=_kursstatistikk(),
                           firma_mangler=firmaopplysninger.liste(con()),
                           adresse_venter=privatadresse.liste(con(), uten_avsluttede=True),
                           sjekk=sjekklister.paaminnelser(con(), _idag()), snart_dager=sjekklister.SNART_DAGER,
                           **_kursliste())


def _kursstatistikk() -> dict:
    """Tallene i statuskortene på Oversikten: åpne kurs og hvor mange som er påmeldt der. Gjelder alle kurs, uavhengig av
    søket og filtrene i kurslisten."""
    rader = con().execute(
        """SELECT (SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=k.id AND p.status='bekreftet'
                                                      AND p.ekstradeltaker_ts IS NULL) AS bekreftet,
                  (SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=k.id AND p.status='bekreftet'
                                                      AND p.ekstradeltaker_ts IS NOT NULL) AS ekstra_antall
           FROM kurs k WHERE k.status IN ('aapen','full','aktiv')""").fetchall()
    return {"aapne_kurs": len(rader), "paameldt": sum(r["bekreftet"] for r in rader),
            "ekstra": sum(r["ekstra_antall"] for r in rader)}


# ------- globalt deltakersøk (søkefeltet i toppmenyen, Oversikt og /admin/sok) -------
# Finner personer på tvers av alle kurs (navn, e-post, telefon, HPR-nummer, påmeldingsnummer, kursnummer/kurskode) og viser
# hva som står om kursbeviset. Logikken ligger i kurs/deltakersok.py. Søketeksten kan være et navn eller en e-postadresse:
# den lagres ikke noe sted (ikke i hendelsesloggen, ikke i applikasjonsloggen, ikke i feilmeldinger), og svarene sendes
# med Cache-Control: no-store. Rullegardinen sender teksten som POST (i kroppen, ikke i adressen - da havner ikke hvert
# tastetrykk i en URL-logg), og søkesidene svarer med Referrer-Policy: no-referrer (sikkerhet.REFERRER_INGEN), slik at
# adressen med ?q= aldri følger med som Referer når man klikker seg videre. Lesetilgang kan søke (samme innsyn som i
# deltakerregisteret).
app.jinja_env.globals.update(SOK_MAKS_TEGN=deltakersok.MAKS_TEGN, SOK_MAKS_ORD=deltakersok.MAKS_ORD,
                             SOK_MIN_TEGN=deltakersok.MIN_TEGN, SOK_MIN_SIFFER=deltakersok.MIN_SIFFER,
                             SOK_MIN_KURSKODE=deltakersok.MIN_KURSKODE, SOK_GRENSE_SIDE=deltakersok.GRENSE_SIDE)

def _sok_json(data: dict, status: int = 200):
    svar = jsonify(data)
    svar.status_code = status
    svar.headers["Cache-Control"] = "no-store"
    return svar


@app.get("/admin/sok")
@krever_admin
def admin_sok():
    """Resultatsiden: alle treff (maks GRENSE_SIDE personer) med alle påmeldinger og kursbevis."""
    q = request.args.get("q", "")
    if deltakersok.tolk(q).gyldig:
        sikkerhet.krev_kvote("admin_sok")          # samme teller som rullegardinen: 429-siden ved masseuthenting
    res = deltakersok.sok(con(), q, grense=deltakersok.GRENSE_SIDE, idag=_idag())
    svar = make_response(render_template("admin_sok.html", res=res, sok_q=res.sok.tekst))
    svar.headers["Cache-Control"] = "no-store"
    return svar


@app.post("/admin/sok.json")
@krever_admin_json
def admin_sok_json():
    """De beste treffene til rullegardinen. POST med {"q": "..."} og CSRF-hodet (X-CSRF-Token): søketeksten ligger i kroppen,
    aldri i adressen. Takbegrenset (sikkerhet.GRENSER) uten å hindre vanlig bruk med tasting."""
    try:
        sikkerhet.krev_kvote("admin_sok")
    except HTTPException:
        return _sok_json({"feil": "for_mange_sok"}, 429)
    kropp = request.get_json(silent=True)
    q = kropp.get("q", "") if isinstance(kropp, dict) else ""
    res = deltakersok.sok(con(), q if isinstance(q, str) else "", grense=deltakersok.GRENSE_LISTE, idag=_idag())
    data = deltakersok.liste_data(res)
    alle = url_for("admin_sok", q=res.sok.tekst)
    for treff in data["treff"]:
        treff["url"] = f"{alle}#person-{treff['id']}"
    data["alle_url"] = alle
    return _sok_json(data)


# ------- aktivitetsoversikt -------

def _kurs_sok_vilkar(sok: str) -> tuple[str, list]:
    """Soek i kurslister: kursnavn (delstreng) ELLER kursnummer. Et rent tall treffer kursnummer som starter med tallet
    («10» finner 1001-1099 ...) - eksakt nummer kommer alltid med. Returnerer (SQL-vilkaar, parametre)."""
    if not sok:
        return "1=1", []
    if sok.isdigit():
        return "(LOWER(k.navn) LIKE ? OR CAST(k.kursnr AS TEXT) LIKE ?)", [f"%{sok.lower()}%", f"{sok}%"]
    return "LOWER(k.navn) LIKE ?", [f"%{sok.lower()}%"]


KURS_ALLE = "alle"        # «Alle kurs» i statusvalget: også avsluttede og avlyste (uten valgt status skjules de)


def _kursliste() -> dict:
    """Kurslisten på Oversikten (tidligere Aktiviteter → Liste): søk på kursnavn eller kursnr., filtre, sortering og
    sidenummerering. Uten valgt status vises kurs som ikke er avsluttet eller avlyst. Et søk leter derimot i alle kurs,
    slik at et gammelt kurs alltid kan finnes med nummeret eller navnet. Returnerer det malen trenger."""
    sok = request.args.get("sok", "").strip()
    mine = request.args.get("mine") == "1"
    ansvarlig = request.args.get("ansvarlig", type=int) or 0
    status = request.args.get("status") or ""
    if status not in (*KURS_STATUSVERDIER, KURS_ALLE):
        status = ""
    status_gjelder = KURS_ALLE if (sok and not status) else status      # statusvalget som faktisk gjelder (og vises i valget)
    sted = request.args.get("sted") or ""
    if sted not in ("bergen", "oslo", "online"):
        sted = ""
    sortering = aktivitetsliste.tolk(request.args.get("sorter"), request.args.get("retning"))
    antall = request.args.get("antall", type=int) or 25
    antall = antall if antall in (10, 25, 50, 100) else 25
    side = max(1, request.args.get("side", type=int) or 1)

    # Samme sted-heuristikk og statusverdier som kurskalenderen (admin_kalender) - se den for begrunnelse.
    sok_sql, sok_parametre = _kurs_sok_vilkar(sok)
    vilkar = [sok_sql, "(? = 0 OR k.ansvarlig_admin_id = ?)"]
    parametre = [_idag().isoformat(), *sok_parametre, 1 if mine else 0, session.get("admin_id")]
    if ansvarlig:
        vilkar.append("k.ansvarlig_admin_id = ?")
        parametre.append(ansvarlig)
    if not status_gjelder:
        vilkar.append("k.status NOT IN ('avsluttet', 'avlyst')")
    elif status_gjelder != KURS_ALLE:
        vilkar.append("k.status = ?")
        parametre.append(status_gjelder)
    if sted == "bergen":
        vilkar.append("LOWER(COALESCE(k.sted,'')) LIKE '%bergen%'")
    elif sted == "oslo":
        vilkar.append("LOWER(COALESCE(k.sted,'')) LIKE '%oslo%'")
    elif sted == "online":
        vilkar.append("(k.type IN ('digital','hybrid') OR LOWER(COALESCE(k.sted,'')) LIKE '%zoom%' "
                      "OR LOWER(COALESCE(k.sted,'')) LIKE '%online%')")

    # Sorteringen (aktivitetsliste.sorter) skjer etter uthentingen: norsk rekkefølge for navn, og standarden
    # «Kommende først» regnes ut fra datoene.
    sql = f"""SELECT k.*, ab.navn AS ansvarlig_navn, MIN(kd.dato) AS start, MAX(kd.dato) AS slutt,
                     COUNT(kd.id) AS antall_dager,
                     (SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=k.id AND p.status='bekreftet'
                                                         AND p.ekstradeltaker_ts IS NULL) AS bekreftet,
                     (SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=k.id AND p.status='bekreftet'
                                                         AND p.ekstradeltaker_ts IS NOT NULL) AS ekstra_antall,
                     (SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=k.id AND p.status='venteliste') AS venteliste,
                     (SELECT COUNT(*) FROM faktura f JOIN paamelding p ON p.id=f.paamelding_id WHERE p.kurs_id=k.id) AS fakturert,
                     (SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=k.id) AS totalt_registrert,
                     (SELECT MIN(kd2.id) FROM kursdag kd2 WHERE kd2.kurs_id=k.id AND kd2.dato=?) AS idag_dag_id
              FROM kurs k
              LEFT JOIN kursdag kd ON kd.kurs_id=k.id
              LEFT JOIN admin_bruker ab ON ab.id=k.ansvarlig_admin_id
              WHERE {' AND '.join(vilkar)}
              GROUP BY k.id, ab.navn"""
    rader = aktivitetsliste.sorter(con().execute(sql, parametre).fetchall(), sortering, _idag())

    totalt = len(rader)
    siste_side = max(1, -(-totalt // antall))
    side = min(side, siste_side)
    start_i = (side - 1) * antall
    kurs = rader[start_i:start_i + antall]

    def _felles_parametre():
        return dict(sok=sok or None, mine=1 if mine else None, ansvarlig=ansvarlig or None,
                   status=status or None, sted=sted or None)

    def sidelenke(n):
        return url_for("admin", **_felles_parametre(), antall=antall,
                       sorter=None if sortering == aktivitetsliste.STANDARD else sortering, side=n, _anchor="kurs")

    def sorterlenke(felt, tekst):
        """Klikkbar kolonneoverskrift: stigende, så synkende. Samme sortering som «Sorter etter»."""
        aktiv = sortering in (f"{felt}_asc", f"{felt}_desc")
        ny = f"{felt}_desc" if sortering == f"{felt}_asc" else f"{felt}_asc"
        pil = (" ↓" if sortering.endswith("_desc") else " ↑") if aktiv else ""
        lenke = url_for("admin", **_felles_parametre(), antall=antall, sorter=ny, _anchor="kurs")
        sortert = f' aria-sort="{"descending" if sortering.endswith("_desc") else "ascending"}"' if aktiv else ""
        return Markup(f'<a href="{lenke}"{sortert}>{Markup.escape(tekst)}{pil}</a>')

    admins = con().execute("SELECT id, navn FROM admin_bruker WHERE aktiv=1 ORDER BY navn").fetchall()
    return dict(
        kurs=kurs, sok=sok, mine=mine, ansvarlig=ansvarlig, status=status_gjelder, sted=sted,
        antall=antall, side=side, totalt=totalt, siste_side=siste_side, sidelenke=sidelenke,
        sorterlenke=sorterlenke, admins=admins, kurs_statusverdier=KURS_STATUSVERDIER, status_navn=aktivitetsliste.STATUSNAVN,
        nullstill_lenke=url_for("admin", _anchor="kurs"), sortering=sortering, sorteringer=aktivitetsliste.SORTERINGER,
        sortering_tekst=aktivitetsliste.beskrivelse(sortering), dato_tekst=aktivitetsliste.dato_tekst,
        formater=aktivitetsliste.FORMAT, sted_tekst=aktivitetsliste.sted_tekst)


@app.get("/admin/aktiviteter")
@krever_admin
def admin_aktiviteter():
    """Aktiviteter er slått sammen med Oversikten. Gamle lenker og bokmerker, også med søk og filtre, sendes dit."""
    soek = request.query_string.decode("ascii", "ignore")
    return redirect(url_for("admin") + (f"?{soek}" if soek else "") + "#kurs")


@app.get("/admin/aktiviteter/kalender")
@krever_admin
def admin_kalender():
    """Kurskalenderen: kursoversikt (standard - kursene kronologisk per måned, neste kurs først), måned eller uke.
    Uten ?visning= gir ?aar= og ?maned= månedsvisningen (lenkene fra før). Se kurs/kurskalender.py."""
    idag = _idag()
    if sjekklister.synk_kurs(con(), idag):           # sjekkliste på alle kurs i systemet (og datoene i takt)
        con().commit()
    visning = request.args.get("visning") or ""
    if visning == "agenda":                  # agendaen er erstattet av kursoversikten
        visning = "oversikt"
    if visning not in ("oversikt", "maned", "uke"):
        visning = "maned" if request.args.get("aar") and request.args.get("maned") else "oversikt"
    aar = request.args.get("aar", type=int) or 0
    maned = request.args.get("maned", type=int) or 0
    if not (1 <= maned <= 12) or not (1 <= aar <= 9999):
        aar, maned = idag.year, idag.month
    try:
        dato = date.fromisoformat(request.args.get("dato") or "")
    except ValueError:
        dato = idag if (idag.year, idag.month) == (aar, maned) else date(aar, maned, 1)

    ansvarlig = request.args.get("ansvarlig", type=int) or 0
    status = request.args.get("status") or ""
    if status not in KURS_STATUSVERDIER and status != "planlagt":
        status = ""
    sted = request.args.get("sted") or ""
    if sted not in kurskalender.STEDER:
        sted = ""
    filtre = dict(ansvarlig=ansvarlig or None, status=status or None, sted=sted or None)
    utvalg = dict(ansvarlig=ansvarlig, status=status, sted=sted, med_planlagte=True)

    def lenke(visning_, **kw):
        return url_for("admin_kalender", visning=None if visning_ in ("maned", "oversikt") else visning_, **kw,
                       **filtre)

    forste = date(aar, maned, 1)
    siste = date(aar, maned, calendar.monthrange(aar, maned)[1])
    forrige_aar, forrige_maned = (aar - 1, 12) if maned == 1 else (aar, maned - 1)
    neste_aar, neste_maned = (aar + 1, 1) if maned == 12 else (aar, maned + 1)
    uker = ukedager = oversikt = maanedsrader = None
    forrige_lenke = neste_lenke = None
    fargeforklaring = []
    if visning == "oversikt":
        hendelser = kurskalender.hent(con(), date(idag.year - 1, idag.month, 1), date(idag.year + 3, 12, 31), **utvalg)
        oversikt = kurskalender.oversikt(hendelser, idag)
        fargeforklaring = kurskalender.fargeforklaring(hendelser, date(idag.year, idag.month, 1))
        tittel = "Kursoversikt"
    elif visning == "uke":
        mandag = dato - timedelta(days=dato.weekday())
        hendelser = kurskalender.hent(con(), mandag, mandag + timedelta(days=6), **utvalg)
        ukedager = kurskalender.uke(dato, hendelser, idag)
        tittel = f"Uke {mandag.isocalendar()[1]} · {kurskalender.periode_tekst(mandag, mandag + timedelta(days=6))}"
        forrige_lenke = lenke("uke", dato=(mandag - timedelta(days=7)).isoformat())
        neste_lenke = lenke("uke", dato=(mandag + timedelta(days=7)).isoformat())
        aar, maned = mandag.year, mandag.month
    else:
        kalender_ = calendar.Calendar(firstweekday=0).monthdatescalendar(aar, maned)
        hendelser = kurskalender.hent(con(), kalender_[0][0], kalender_[-1][-1], **utvalg)
        uker = kurskalender.uker(aar, maned, hendelser, idag)
        maanedsrader = kurskalender.rader([h for h in hendelser if h.til >= forste and h.fra <= siste], idag)
        tittel = f"{kurskalender.MAANEDER[maned - 1]} {aar}"
        forrige_lenke = lenke(visning, aar=forrige_aar, maned=forrige_maned)
        neste_lenke = lenke(visning, aar=neste_aar, maned=neste_maned)

    admins = con().execute("SELECT id, navn FROM admin_bruker WHERE aktiv=1 ORDER BY navn").fetchall()
    sjekklister.legg_paa(con(), hendelser, idag)
    her = _her()

    def apne_lenke(samling_id: int, lukk: bool = False) -> str:
        """Pila på raden uten JavaScript: siden med sjekklisten åpen (eller lukket), tilbake til raden (#sk-<samling>)."""
        lenke = _tilbake(her, None if lukk else samling_id, url_for("admin_kalender"))
        return f"{lenke}#sk-{samling_id}" if lukk else lenke

    return render_template(
        "admin_kalender.html", kalendervisning=visning, uker=uker, ukedager=ukedager, oversikt=oversikt,
        apen=request.args.get("apen", type=int), her=her, apne_lenke=apne_lenke,
        maanedsrader=maanedsrader, idag=idag, aar=aar,
        maned=maned, dato=dato, tittel=tittel, ukedagnavn=kurskalender.UKEDAGER,
        forrige_lenke=forrige_lenke, neste_lenke=neste_lenke,
        idag_lenke=(lenke("oversikt") if visning == "oversikt" else
                    lenke(visning, **({"dato": idag.isoformat()} if visning == "uke" else
                                      {"aar": idag.year, "maned": idag.month}))),
        visningslenker={"oversikt": lenke("oversikt"),
                        "maned": lenke("maned", aar=aar, maned=maned),
                        "uke": lenke("uke", dato=dato.isoformat())},
        nullstill_lenke=(url_for("admin_kalender") if visning == "oversikt" else
                         url_for("admin_kalender", visning=None if visning == "maned" else visning, aar=aar,
                                 maned=maned, dato=dato.isoformat() if visning == "uke" else None)),
        ansvarlig=ansvarlig, status=status, sted=sted, admins=admins, kurs_statusverdier=KURS_STATUSVERDIER,
        antall_hendelser=len(hendelser), fargeforklaring=fargeforklaring,
        antall_planlagte=len({h.plan_id for h in hendelser if h.planlagt}))


def _vis_aarsplan(aar: int, skjema=None, status: int = 200, periodeskjema=None):
    """Årsplanen: «Året» (tolv små månedskalendere, standard) eller «Uke for uke» (?vis=uker). ?omraade= viser
    skoleferiene for ett område, ?ledig= er minste lengde på ledige perioder (dager). Passerte ledige perioder vises ikke."""
    idag = _idag()
    omraader = aarsplan.omraader(con())
    omraade = request.args.get("omraade") or ""
    if omraade not in omraader:
        omraade = ""
    aarsvisning = "uker" if request.args.get("vis") == "uker" else "aar"
    min_dager = request.args.get("ledig", type=int)
    if min_dager not in (3, 5, 7, 14):
        min_dager = aarsplan.LEDIG_MIN_DAGER
    plan = aarsplan.hent(con(), aar, omraade)
    ledige = aarsplan.ledige(plan, min_dager)
    kommende = [p for p in ledige if p.til >= idag]
    kurs_i_systemet = [k for k in plan.kurs if k.get("plan_id") is None]
    return render_template(
        "admin_aarsplan.html", aar=aar, idag=idag, visning="aarsplan",
        maaneder=aarsplan.aarskalender(plan, idag) if aarsvisning == "aar" else None,
        planer=aarsplan.planliste(plan), overlapp=aarsplan.overlapp_perioder(plan),
        ledige=kommende, ledige_passert=len(ledige) - len(kommende), ledige_uker=aarsplan.ledige_perioder(plan),
        min_dager=min_dager,
        konflikter=aarsplan.konflikter(plan), uker=aarsplan.uker(plan, idag) if aarsvisning == "uker" else None,
        aarsvisning=aarsvisning, ukedagnavn=aarsplan.UKEDAGER, omraader=omraader, omraade=omraade,
        perioder=aarsplan.perioder_i_aaret(con(), aar), periodetyper=aarsplan.PERIODETYPER, gjelder=aarsplan.GJELDER,
        antall_kurs=len(kurs_i_systemet), antall_kursdager=sum(len(k["datoer"]) for k in kurs_i_systemet),
        antall_planlagte_kurs=len(plan.kurs) - len(kurs_i_systemet),
        antall_planer=sum(p["type"] == "plan" for p in plan.planer), skjema=skjema or {},
        periodeskjema=periodeskjema or {}, fargeforklaring=aarsplan.fargeforklaring(plan),
        forfalte=sjekklister.forfalte_samlinger(con(), idag)), status


@app.get("/admin/aktiviteter/aarsplan")
@krever_admin
def admin_aarsplan():
    """Årsplan (kurshjul): alle kurs i året automatisk, pluss planlagte aktiviteter og notater, overlapp og ledige
    perioder. Se kurs/aarsplan.py."""
    aar = request.args.get("aar", type=int) or _idag().year
    if not aarsplan.FORSTE_AAR <= aar <= aarsplan.SISTE_AAR:
        aar = _idag().year
    if sjekklister.synk_kurs(con(), _idag()):         # rød prikk også for kursene i systemet
        con().commit()
    return _vis_aarsplan(aar)


@app.post("/admin/aktiviteter/aarsplan/ny")
@krever_admin
def admin_aarsplan_ny():
    try:
        verdier = aarsplan.valider(request.form)
    except aarsplan.AarsplanFeil as e:
        flash(str(e), "feil")
        aar = request.form.get("aar", type=int) or _idag().year
        if not aarsplan.FORSTE_AAR <= aar <= aarsplan.SISTE_AAR:
            aar = _idag().year
        return _vis_aarsplan(aar, skjema=request.form, status=400)
    with db.transaksjon(con()):
        aarsplan.opprett(con(), verdier, _aktor())
    flash(f"{aarsplan.TYPER[verdier['type']]} lagt inn i årsplanen.", "ok")
    return redirect(url_for("admin_aarsplan", aar=int(verdier["fra_dato"][:4])))


@app.post("/admin/aktiviteter/aarsplan/<int:plan_id>/slett")
@krever_admin
def admin_aarsplan_slett(plan_id):
    with db.transaksjon(con()):
        rad = aarsplan.slett(con(), plan_id, _aktor())
    if not rad:
        abort(404)
    flash("Fjernet fra årsplanen.", "ok")
    return redirect(url_for("admin_aarsplan", aar=int(rad["fra_dato"][:4])))


def _aar_fra_skjema() -> int:
    aar = request.form.get("aar", type=int) or _idag().year
    return aar if aarsplan.FORSTE_AAR <= aar <= aarsplan.SISTE_AAR else _idag().year


@app.post("/admin/aktiviteter/aarsplan/periode/ny")
@krever_admin
def admin_aarsplan_periode_ny():
    """Ny skoleferie (for ett område), sperret periode eller advarsel. Hindrer aldri at kurs opprettes."""
    try:
        verdier = aarsplan.valider_periode(request.form)
    except aarsplan.AarsplanFeil as e:
        flash(str(e), "feil")
        return _vis_aarsplan(_aar_fra_skjema(), periodeskjema=request.form, status=400)
    with db.transaksjon(con()):
        aarsplan.opprett_periode(con(), verdier, _aktor())
    flash(f"{aarsplan.PERIODETYPER[verdier['type']]} lagt inn i årsplanen.", "ok")
    return redirect(url_for("admin_aarsplan", aar=int(verdier["fra_dato"][:4])) + "#perioder")


@app.route("/admin/aktiviteter/aarsplan/periode/<int:periode_id>", methods=["GET", "POST"])
@krever_admin
def admin_aarsplan_periode(periode_id):
    periode = aarsplan.hent_periode(con(), periode_id) or abort(404)
    skjema = periode
    if request.method == "POST":
        try:
            verdier = aarsplan.valider_periode(request.form)
        except aarsplan.AarsplanFeil as e:
            flash(str(e), "feil")
            skjema = request.form
        else:
            with db.transaksjon(con()):
                aarsplan.oppdater_periode(con(), periode_id, verdier, _aktor())
            flash("Perioden er endret.", "ok")
            return redirect(url_for("admin_aarsplan", aar=int(verdier["fra_dato"][:4])) + "#perioder")
    return render_template("admin_aarsplan_periode.html", periode=periode, skjema=skjema, omraader=aarsplan.omraader(con()),
                           periodetyper=aarsplan.PERIODETYPER, gjelder=aarsplan.GJELDER,
                           visning="aarsplan"), (400 if skjema is not periode else 200)


@app.post("/admin/aktiviteter/aarsplan/periode/<int:periode_id>/slett")
@krever_admin
def admin_aarsplan_periode_slett(periode_id):
    with db.transaksjon(con()):
        rad = aarsplan.slett_periode(con(), periode_id, _aktor())
    if not rad:
        abort(404)
    flash("Perioden er fjernet fra årsplanen.", "ok")
    return redirect(url_for("admin_aarsplan", aar=int(rad["fra_dato"][:4])) + "#perioder")


# ------- planlagte kurs (Kalender → Planlagte kurs): ikke opprettet i systemet, se kurs/planlagte_kurs.py -------

def _planlagt_maler() -> dict:
    return dict(visning="planlagte", farger=range(kursfarger.ANTALL), statuser=planlagte_kurs.STATUSER,
                kort_periode=kurskalender.kort_periode, periode_tekst=kurskalender.periode_tekst,
                maler=sjekklister.maler(con()))


def _her() -> str:
    """Denne sidens adresse (sti og spørring) - til «tilbake hit» etter et skjema."""
    return request.full_path.rstrip("?")


def _tilbake(neste, apen: int | None, standard: str) -> str:
    """Tilbake til siden skjemaet kom fra (bare sider under /admin/aktiviteter/), med ?apen=<samling> og #sk-<samling>
    så sjekklisten står åpen der man var."""
    sti = sikkerhet.trygg_neste(neste, "")
    if not sti.startswith("/admin/aktiviteter/"):
        sti = standard
    deler = urlsplit(sti)
    sporring = [(k, v) for k, v in parse_qsl(deler.query) if k != "apen"] + ([("apen", str(apen))] if apen else [])
    return deler.path + (f"?{urlencode(sporring)}" if sporring else "") + (f"#sk-{apen}" if apen else "")


def _mal_fra_skjema() -> tuple[bool, int | None]:
    """(feltet fantes, mal-id) fra «Sjekkliste» i skjemaet for planlagt kurs. Tomt = ingen mal."""
    if "sjekkliste_mal" not in request.form:
        return False, None
    verdi = (request.form.get("sjekkliste_mal") or "").strip()
    if not verdi:
        return True, None
    if not verdi.isascii() or not verdi.isdigit():
        raise sjekklister.SjekklisteFeil("Ugyldig sjekklistemal.")
    return True, int(verdi)


@app.get("/admin/aktiviteter/planlagte-kurs")
@krever_admin
def admin_kalender_planlagte():
    """Alle planlagte kurs med samlingene. Kurs som er ferdige før denne måneden, vises med ?tidligere=1."""
    idag = _idag()
    alle = planlagte_kurs.liste(con())
    tidligere = request.args.get("tidligere") == "1"
    grense = date(idag.year, idag.month, 1)
    synlige = [k for k in alle if tidligere or k["til"] is None or k["til"] >= grense]
    lister = sjekklister.for_samlinger(con(), [s["id"] for k in synlige for s in k["samlinger"]], idag)
    for k in synlige:
        k["forfalt"] = sum(lister[s["id"]].forfalt for s in k["samlinger"] if s["id"] in lister)
        k["snart"] = sum(lister[s["id"]].snart for s in k["samlinger"] if s["id"] in lister)
    return render_template("admin_planlagte_kurs.html", kursliste=synlige, vis_tidligere=tidligere,
                           antall_skjult=len(alle) - len(synlige), maa_sjekkes=sum(k["maa_sjekkes"] for k in synlige),
                           **_planlagt_maler())


@app.route("/admin/aktiviteter/planlagte-kurs/ny", methods=["GET", "POST"])
@krever_admin
def admin_kalender_planlagt_ny():
    """Nytt planlagt kurs med første samling. Fargen velges ut fra datoene. Med mal lages sjekklisten for samlingen."""
    if request.method == "POST":
        try:
            kurs = planlagte_kurs.valider_kurs(request.form)
            samling = planlagte_kurs.valider_samling(request.form, "s-")
            _, mal_id = _mal_fra_skjema()
            with db.transaksjon(con()):
                plan_id = planlagte_kurs.opprett(con(), kurs, [samling], _aktor())
                if mal_id:
                    sjekklister.sett_mal(con(), plan_id, mal_id, _aktor())
                    sjekklister.lag_for_kurs(con(), plan_id, _aktor(), fra=_idag())
        except (planlagte_kurs.PlanFeil, sjekklister.SjekklisteFeil) as e:
            flash(str(e), "feil")
            return render_template("admin_planlagt_kurs_ny.html", skjema=request.form, **_planlagt_maler()), 400
        flash("Det planlagte kurset er lagt inn i kalenderen. Legg til flere samlinger under.", "ok")
        return redirect(url_for("admin_kalender_planlagt", plan_id=plan_id))
    return render_template("admin_planlagt_kurs_ny.html", skjema={}, **_planlagt_maler())


def _planlagt_side(kurs, *, skjema=None, nyskjema=None, samlingsskjema=None, status=200):
    idag = _idag()
    lister = sjekklister.for_samlinger(con(), [s["id"] for s in kurs["samlinger"]], idag)
    apen = request.args.get("apen", type=int)
    if apen is None:        # uten valg: den første samlingen som ikke er over, står åpen
        apen = next((s["id"] for s in kurs["samlinger"] if s["til"] >= idag and s["id"] in lister), None)
    mal_id = sjekklister.mal_for_kurs(con(), kurs["id"])
    bookinger = booking.for_samlinger(con(), [s["id"] for s in kurs["samlinger"]])
    return render_template("admin_planlagt_kurs.html", kurs={**kurs, "sjekkliste_mal": mal_id}, skjema=skjema,
                           nyskjema=nyskjema or {}, samlingsskjema=samlingsskjema or {}, sjekklister=lister, apen=apen,
                           bookinger=bookinger, her=_her(), idag=idag, mal_id=mal_id, **_planlagt_maler()), status


@app.route("/admin/aktiviteter/planlagte-kurs/<int:plan_id>", methods=["GET", "POST"])
@krever_admin
def admin_kalender_planlagt(plan_id):
    """Ett planlagt kurs: kursets felt, samlingene med sjekklistene, og «Må sjekkes». Lagring av kursets felt (POST)."""
    kurs = planlagte_kurs.hent(con(), plan_id) or abort(404)
    if request.method == "POST":
        try:
            verdier = planlagte_kurs.valider_kurs(request.form)
            sendt, mal_id = _mal_fra_skjema()
            with db.transaksjon(con()):
                planlagte_kurs.oppdater_kurs(con(), plan_id, verdier, _aktor())
                ny_mal = sendt and sjekklister.sett_mal(con(), plan_id, mal_id, _aktor())
                laget = sjekklister.lag_for_kurs(con(), plan_id, _aktor(), fra=_idag()) if ny_mal and mal_id else 0
        except (planlagte_kurs.PlanFeil, sjekklister.SjekklisteFeil) as e:
            flash(str(e), "feil")
            return _planlagt_side(kurs, skjema=request.form, status=400)
        flash("Endringene er lagret." + (f" Sjekkliste laget for {laget} samling{'er' if laget != 1 else ''}." if laget else "")
              + (" Samlinger som alt har sjekkliste, beholder den: bruk «Oppdater fra malen» på samlingen." if ny_mal and mal_id
                 else ""), "ok")
        return redirect(url_for("admin_kalender_planlagt", plan_id=plan_id))
    return _planlagt_side(kurs)


@app.post("/admin/aktiviteter/planlagte-kurs/<int:plan_id>/slett")
@krever_admin
def admin_kalender_planlagt_slett(plan_id):
    with db.transaksjon(con()):
        rad = planlagte_kurs.slett(con(), plan_id, _aktor())
    if not rad:
        abort(404)
    flash(f"«{planlagte_kurs.visningsnavn(rad['prosjektnr'], rad['navn'])}» er slettet fra kalenderen.", "ok")
    return redirect(url_for("admin_kalender_planlagte"))


@app.post("/admin/aktiviteter/planlagte-kurs/<int:plan_id>/samling/ny")
@krever_admin
def admin_kalender_planlagt_samling_ny(plan_id):
    """Ny samling. Har kurset en mal og samlingen ikke er over, lages sjekklisten med en gang."""
    kurs = planlagte_kurs.hent(con(), plan_id) or abort(404)
    try:
        samling = planlagte_kurs.valider_samling(request.form)
        with db.transaksjon(con()):
            sid = planlagte_kurs.legg_til_samling(con(), plan_id, samling, _aktor())
            if samling["til_dato"] >= _idag().isoformat():
                sjekklister.lag_for_samling(con(), sid, _aktor())
    except planlagte_kurs.PlanFeil as e:
        flash(str(e), "feil")
        return _planlagt_side(kurs, nyskjema=request.form, status=400)
    flash("Samlingen er lagt til.", "ok")
    return redirect(url_for("admin_kalender_planlagt", plan_id=plan_id) + "#samlinger")


@app.post("/admin/aktiviteter/planlagte-kurs/<int:plan_id>/samling/<int:samling_id>")
@krever_admin
def admin_kalender_planlagt_samling(plan_id, samling_id):
    """Lagrer samlingen. Nye datoer flytter fristene i sjekklisten (ikke frister som er endret for hånd)."""
    kurs = planlagte_kurs.hent(con(), plan_id) or abort(404)
    if not any(s["id"] == samling_id for s in kurs["samlinger"]):
        abort(404)
    try:
        samling = planlagte_kurs.valider_samling(request.form)
    except planlagte_kurs.PlanFeil as e:
        flash(str(e), "feil")
        return _planlagt_side(kurs, samlingsskjema={samling_id: request.form}, status=400)
    with db.transaksjon(con()):
        planlagte_kurs.oppdater_samling(con(), plan_id, samling_id, samling, _aktor())
        flyttet = sjekklister.flytt_frister(con(), samling_id)
    flash("Samlingen er lagret." + (f" {flyttet} frist{'er' if flyttet != 1 else ''} i sjekklisten er flyttet etter de nye "
                                    "datoene." if flyttet else ""), "ok")
    return redirect(url_for("admin_kalender_planlagt", plan_id=plan_id) + f"#samling-{samling_id}")


@app.post("/admin/aktiviteter/planlagte-kurs/<int:plan_id>/samling/<int:samling_id>/slett")
@krever_admin
def admin_kalender_planlagt_samling_slett(plan_id, samling_id):
    try:
        with db.transaksjon(con()):
            rad = planlagte_kurs.slett_samling(con(), plan_id, samling_id, _aktor())
    except planlagte_kurs.PlanFeil as e:
        flash(str(e), "feil")
        return redirect(url_for("admin_kalender_planlagt", plan_id=plan_id) + "#samlinger")
    if not rad:
        abort(404)
    flash("Samlingen er slettet.", "ok")
    return redirect(url_for("admin_kalender_planlagt", plan_id=plan_id) + "#samlinger")


# ------- sjekklister (kurs/sjekklister.py): én per samling i et planlagt kurs, laget fra kursets mal -------

@app.post("/admin/aktiviteter/planlagte-kurs/<int:plan_id>/samling/<int:samling_id>/sjekkliste")
@krever_admin
def admin_kalender_planlagt_sjekkliste(plan_id, samling_id):
    """Samlingens sjekkliste: «lag» (fra kursets mal), «oppdater» (etter malen) eller «nytt_punkt» (eget punkt)."""
    kurs = planlagte_kurs.hent(con(), plan_id) or abort(404)
    if not any(s["id"] == samling_id for s in kurs["samlinger"]):
        abort(404)
    handling = request.form.get("handling") or ""
    if handling not in ("lag", "oppdater", "nytt_punkt"):
        abort(400)
    try:
        with db.transaksjon(con()):
            if handling == "lag":
                n = sjekklister.lag_for_samling(con(), samling_id, _aktor())
                melding = (f"Sjekklisten er laget ({n} punkter)." if n else
                           "Ingen sjekkliste laget: kurset har ingen mal, eller samlingen har sjekkliste fra før.")
            elif handling == "oppdater":
                r = sjekklister.oppdater_fra_mal(con(), samling_id, _aktor())
                melding = (f"Oppdatert fra malen: {r['lagt_til']} nye, {r['endret']} endret og {r['fjernet']} fjernet. "
                           "Det som er krysset av, er ikke rørt.")
            else:
                sjekklister.legg_til_punkt(con(), samling_id, request.form, _aktor())
                melding = "Punktet er lagt til."
        flash(melding, "ok")
    except sjekklister.SjekklisteFeil as e:
        flash(str(e), "feil")
    return redirect(_tilbake(request.form.get("neste"), samling_id, url_for("admin_kalender_planlagt", plan_id=plan_id)))


@app.post("/admin/aktiviteter/sjekkliste/<int:punkt_id>")
@krever_admin
def admin_kalender_sjekkliste_punkt(punkt_id):
    """Ett punkt: «kryss» (utfort=1 eller tomt, og ev. tekstfeltet), «ikke_aktuelt», «aktuelt», «frist» (tom = følg
    datoene), «notat» (hva som er gjort), «slett» (eget punkt slettes, punkt fra malen tas bort fra denne sjekklisten) eller
    «gjenopprett» (et slettet punkt fra malen tilbake). Tilbake til siden man var på, med sjekklisten åpen."""
    plan_id, samling_id = sjekklister.samling_for_punkt(con(), punkt_id) or abort(404)
    handling = request.form.get("handling") or "kryss"
    try:
        with db.transaksjon(con()):
            if handling == "kryss":
                if "felt" in request.form:
                    sjekklister.sett_felt(con(), punkt_id, request.form.get("felt"), _aktor())
                sjekklister.sett_status(con(), punkt_id, "utfort" if request.form.get("utfort") else "aapen", _aktor())
            elif handling == "ikke_aktuelt":
                sjekklister.sett_status(con(), punkt_id, "ikke_aktuelt", _aktor())
            elif handling == "aktuelt":
                sjekklister.sett_status(con(), punkt_id, "aapen", _aktor())
            elif handling == "frist":
                sjekklister.sett_frist(con(), punkt_id, request.form.get("frist"), _aktor())
            elif handling == "notat":
                sjekklister.sett_notat(con(), punkt_id, request.form.get("notat"), _aktor())
            elif handling == "slett":
                sjekklister.slett_punkt(con(), punkt_id, _aktor())
            elif handling == "gjenopprett":
                sjekklister.gjenopprett_punkt(con(), punkt_id, _aktor())
            else:
                abort(400)
    except sjekklister.SjekklisteFeil as e:
        flash(str(e), "feil")
    return redirect(_tilbake(request.form.get("neste"), samling_id, url_for("admin_kalender_planlagt", plan_id=plan_id)))


app.jinja_env.globals.update(booking_linjer=booking.linjer, booking_statuser=booking.STATUSER)   # _sjekkliste.html


@app.post("/admin/aktiviteter/booking/<int:samling_id>")
@krever_admin
def admin_kalender_booking(samling_id):
    """Booking for samlingen (nederst når kurset åpnes i Kalender, og på kursets side): type (lokale, hotell, grupperom,
    lunsj), status (booket, ikke booket, trengs ikke, tom = ikke satt) og tekst (hvor, referansenummer ...)."""
    rad = con().execute("SELECT planlagt_kurs_id FROM planlagt_samling WHERE id=?", (samling_id,)).fetchone() or abort(404)
    try:
        with db.transaksjon(con()):
            booking.sett(con(), samling_id, request.form.get("type") or "", request.form.get("status"),
                         request.form.get("tekst"), _aktor())
    except booking.BookingFeil as e:
        flash(str(e), "feil")
    return redirect(_tilbake(request.form.get("neste"), samling_id, url_for("admin_kalender_planlagt", plan_id=rad[0])))


# ------- kursholderoversikten (Kalender → Kursholdere): som fanen «Kommende kurs» i Excel, se kurs/kursholdere.py -------

@app.get("/admin/aktiviteter/kursholdere")
@krever_admin
def admin_kursholdere():
    """En rad per samling og veiledningsdag, en kolonne per kursholder, rollen i ruta (trykk for å endre). Rød skrift =
    ikke lagt inn i Psybase, svart = booket. Samme person på to samlinger samtidig markeres. ?kursholder=<id> viser bare
    radene der personen har en rolle, ?tidligere=1 tar med fjoråret."""
    idag = _idag()
    if sjekklister.synk_kurs(con(), idag):           # kursene i systemet er med (de har et skjult planlagt kurs)
        con().commit()
    valgt = request.args.get("kursholder", type=int)
    tidligere = bool(request.args.get("tidligere"))
    m = kursholdere.matrise(con(), idag, fra=date(idag.year - 1, 1, 1) if tidligere else None, kursholder_id=valgt)
    return render_template("admin_kursholdere.html", m=m, valgt=valgt, tidligere=tidligere, roller=kursholdere.ROLLER,
                           alle=kursholdere.liste(con(), med_inaktive=True), visning="kursholdere", her=_her())


def _tilbake_kursholdere(neste, anker: str = "") -> str:
    sti = sikkerhet.trygg_neste(neste, "")
    if not sti.startswith("/admin/aktiviteter/kursholdere"):
        sti = url_for("admin_kursholdere")
    deler = urlsplit(sti)
    return deler.path + (f"?{deler.query}" if deler.query else "") + (f"#{anker}" if anker else "")


@app.post("/admin/aktiviteter/kursholdere/rolle")
@krever_admin
def admin_kursholdere_rolle():
    """Rollen og Psybase-status i én rute (tom rolle = fjern). Tilbake til raden."""
    samling_id = request.form.get("samling_id", type=int) or 0
    dag = (request.form.get("dag") or "").strip()
    try:
        with db.transaksjon(con()):
            kursholdere.sett_rolle(con(), samling_id, dag, request.form.get("kursholder_id", type=int) or 0,
                                   request.form.get("rolle"), request.form.get("psybase") == "1", _aktor())
    except kursholdere.KursholderFeil as e:
        flash(str(e), "feil")
    return redirect(_tilbake_kursholdere(request.form.get("neste"), f"rad-{samling_id}" + (f"-{dag}" if dag else "")))


@app.post("/admin/aktiviteter/kursholdere/ny")
@krever_admin
def admin_kursholdere_ny():
    try:
        with db.transaksjon(con()):
            kursholdere.opprett(con(), kursholdere.valider(request.form), _aktor())
        flash("Kursholderen er lagt til.", "ok")
    except kursholdere.KursholderFeil as e:
        flash(str(e), "feil")
    return redirect(_tilbake_kursholdere(request.form.get("neste"), "kursholderne"))


@app.post("/admin/aktiviteter/kursholdere/<int:kid>")
@krever_admin
def admin_kursholdere_endre(kid):
    try:
        with db.transaksjon(con()):
            if not kursholdere.oppdater(con(), kid, kursholdere.valider(request.form), request.form.get("aktiv") == "1",
                                        _aktor()):
                abort(404)
        flash("Endringene er lagret.", "ok")
    except kursholdere.KursholderFeil as e:
        flash(str(e), "feil")
    return redirect(_tilbake_kursholdere(request.form.get("neste"), "kursholderne"))


def _mal_side(mal, *, skjema=None, nyskjema=None, punktskjema=None, status=200):
    return render_template("admin_sjekklistemal.html", mal=mal, skjema=skjema or mal, nyskjema=nyskjema or {},
                           punktskjema=punktskjema or {}, gjelder=sjekklister.GJELDER, enheter=sjekklister.ENHETER,
                           typer=sjekklister.TYPER, visning="maler"), status


@app.get("/admin/aktiviteter/sjekklistemaler")
@krever_admin
def admin_kalender_maler():
    """Sjekklistemalene (én per kurstype). Et planlagt kurs velger mal på sin side."""
    return render_template("admin_sjekklistemaler.html", maler=sjekklister.maler(con()), skjema={}, visning="maler")


@app.post("/admin/aktiviteter/sjekklistemaler/ny")
@krever_admin
def admin_kalender_mal_ny():
    try:
        verdier = sjekklister.valider_mal(request.form)
    except sjekklister.SjekklisteFeil as e:
        flash(str(e), "feil")
        return render_template("admin_sjekklistemaler.html", maler=sjekklister.maler(con()), skjema=request.form,
                               visning="maler"), 400
    with db.transaksjon(con()):
        mal_id = sjekklister.opprett_mal(con(), verdier, _aktor())
    flash("Malen er laget. Legg inn punktene under.", "ok")
    return redirect(url_for("admin_kalender_mal", mal_id=mal_id))


@app.route("/admin/aktiviteter/sjekklistemaler/<int:mal_id>", methods=["GET", "POST"])
@krever_admin
def admin_kalender_mal(mal_id):
    mal = sjekklister.hent_mal(con(), mal_id) or abort(404)
    if request.method == "POST":
        try:
            verdier = sjekklister.valider_mal(request.form)
        except sjekklister.SjekklisteFeil as e:
            flash(str(e), "feil")
            return _mal_side(mal, skjema=request.form, status=400)
        with db.transaksjon(con()):
            sjekklister.oppdater_mal(con(), mal_id, verdier, _aktor())
        flash("Malen er lagret.", "ok")
        return redirect(url_for("admin_kalender_mal", mal_id=mal_id))
    return _mal_side(mal)


@app.post("/admin/aktiviteter/sjekklistemaler/<int:mal_id>/slett")
@krever_admin
def admin_kalender_mal_slett(mal_id):
    with db.transaksjon(con()):
        rad = sjekklister.slett_mal(con(), mal_id, _aktor())
    if not rad:
        abort(404)
    flash(f"Malen «{rad['navn']}» er slettet. Sjekklistene som alt er laget, beholdes.", "ok")
    return redirect(url_for("admin_kalender_maler"))


@app.post("/admin/aktiviteter/sjekklistemaler/<int:mal_id>/punkt/ny")
@krever_admin
def admin_kalender_malpunkt_ny(mal_id):
    mal = sjekklister.hent_mal(con(), mal_id) or abort(404)
    try:
        verdier = sjekklister.valider_malpunkt(request.form)
    except sjekklister.SjekklisteFeil as e:
        flash(str(e), "feil")
        return _mal_side(mal, nyskjema=request.form, status=400)
    with db.transaksjon(con()):
        pid = sjekklister.opprett_malpunkt(con(), mal_id, verdier, _aktor())
    flash("Punktet er lagt til i malen.", "ok")
    return redirect(url_for("admin_kalender_mal", mal_id=mal_id) + f"#punkt-{pid}")


@app.post("/admin/aktiviteter/sjekklistemaler/<int:mal_id>/punkt/<int:punkt_id>")
@krever_admin
def admin_kalender_malpunkt(mal_id, punkt_id):
    mal = sjekklister.hent_mal(con(), mal_id) or abort(404)
    if not any(p["id"] == punkt_id for p in mal["punkter"]):
        abort(404)
    try:
        verdier = sjekklister.valider_malpunkt(request.form)
    except sjekklister.SjekklisteFeil as e:
        flash(str(e), "feil")
        return _mal_side(mal, punktskjema={punkt_id: request.form}, status=400)
    with db.transaksjon(con()):
        sjekklister.oppdater_malpunkt(con(), mal_id, punkt_id, verdier, _aktor())
    flash("Punktet er lagret. Sjekklister som alt er laget, endres når du velger «Oppdater fra malen» på samlingen.", "ok")
    return redirect(url_for("admin_kalender_mal", mal_id=mal_id) + f"#punkt-{punkt_id}")


@app.post("/admin/aktiviteter/sjekklistemaler/<int:mal_id>/punkt/<int:punkt_id>/flytt")
@krever_admin
def admin_kalender_malpunkt_flytt(mal_id, punkt_id):
    retning = request.form.get("retning")
    if retning not in ("opp", "ned"):
        abort(400)
    with db.transaksjon(con()):
        ok = sjekklister.flytt_malpunkt(con(), mal_id, punkt_id, retning)
    if not ok:
        abort(404)
    return redirect(url_for("admin_kalender_mal", mal_id=mal_id) + f"#punkt-{punkt_id}")


@app.post("/admin/aktiviteter/sjekklistemaler/<int:mal_id>/punkt/<int:punkt_id>/slett")
@krever_admin
def admin_kalender_malpunkt_slett(mal_id, punkt_id):
    with db.transaksjon(con()):
        ok = sjekklister.slett_malpunkt(con(), mal_id, punkt_id, _aktor())
    if not ok:
        abort(404)
    flash("Punktet er fjernet fra malen. Sjekklister som alt er laget, beholder det.", "ok")
    return redirect(url_for("admin_kalender_mal", mal_id=mal_id))


# ------- uavklarte operasjoner (e-post og faktura med ukjent utfall) -------

def _foreslatt_belop(rad) -> int:
    """Beloepet motoren ville fakturert: hele prisen, eller denne samlingens andel (samme deling som sveiper)."""
    if rad["kursdag_id"] is None:
        return rad["pris_nok"]
    dager = [d["id"] for d in db.kursdager(con(), rad["kurs_id"])]
    andeler = sveiper.delbelop(rad["pris_nok"], len(dager)) if dager else []
    return andeler[dager.index(rad["kursdag_id"])] if rad["kursdag_id"] in dager else rad["pris_nok"]


def _kurs_fra_nokkel(nokkel: str):
    if nokkel.startswith("kurs:") and nokkel[5:].isdigit():
        return con().execute("SELECT id, kursnr, navn FROM kurs WHERE id=?", (int(nokkel[5:]),)).fetchone()
    return None


@app.get("/admin/uavklart")
@krever_admin
def admin_uavklart():
    """E-poster og fakturaer med uavklart utfall. De sendes/faktureres aldri automatisk paa nytt - her registrerer admin
    hva som faktisk skjedde, etter kontroll i Outlook (Sendte elementer) eller Visma."""
    eposter = [dict(r, kurs=_kurs_fra_nokkel(r["nokkel"])) for r in db.uavklarte_eposter(con())]
    fakturaer = [dict(r, forslag=_foreslatt_belop(r)) for r in db.uavklarte_fakturaer(con())]
    return render_template("admin_uavklart.html", eposter=eposter, fakturaer=fakturaer)


@app.post("/admin/uavklart/epost")
@krever_admin
def admin_uavklart_epost():
    f = request.form
    if f.get("utfall") not in ("sendt", "ikke_sendt"):
        abort(400)
    with db.transaksjon(con()):
        ok = db.avklar_epost(con(), f.get("nokkel", ""), f.get("mottaker", ""), f.get("type", ""),
                             sendt=f["utfall"] == "sendt", aktor=_aktor())
    flash("Registrert." if ok else "Denne e-posten er ikke lenger uavklart (noen andre kan ha avklart den).",
          "ok" if ok else "info")
    return redirect(url_for("admin_uavklart"))


@app.post("/admin/uavklart/faktura/<int:forsok_id>")
@krever_admin
def admin_uavklart_faktura(forsok_id):
    f = request.form
    utfall = f.get("utfall")
    if utfall not in ("fakturert", "ikke_fakturert"):
        abort(400)
    faktura_nr = (f.get("faktura_nr") or "").strip()
    belop = f.get("belop_nok", type=int)
    if utfall == "fakturert" and (not faktura_nr or len(faktura_nr) > 40 or belop is None or belop <= 0):
        flash("Fyll inn fakturanummeret fra Visma og et beløp større enn 0.", "feil")
        return redirect(url_for("admin_uavklart"))
    try:
        with db.transaksjon(con()):
            ok = db.avklar_faktura(con(), forsok_id, fakturert=utfall == "fakturert", aktor=_aktor(),
                                   faktura_nr=faktura_nr or None, belop_nok=belop,
                                   visma_id=(f.get("visma_id") or "").strip()[:80] or None)
    except db.IntegritetsFeil:
        flash("Det finnes allerede en faktura for denne påmeldingen/samlingen. Ingenting er endret.", "feil")
        return redirect(url_for("admin_uavklart"))
    if not ok:
        flash("Dette fakturaforsøket er ikke lenger uavklart (noen andre kan ha avklart det).", "info")
    elif utfall == "fakturert":
        flash(f"Faktura {faktura_nr} er registrert.", "ok")
    else:
        flash("Registrert som ikke fakturert. Fakturaen lages ved neste daglige kjøring.", "ok")
    return redirect(url_for("admin_uavklart"))


# ------- kursadministrasjon: faner -------

@app.get("/admin/kurs/<int:kurs_id>")
@krever_admin
def admin_kurs(kurs_id):
    return redirect(url_for("admin_kurs_deltakere", kurs_id=kurs_id))


def _hent_kurs(kurs_id: int):
    return con().execute("SELECT * FROM kurs WHERE id=?", (kurs_id,)).fetchone() or abort(404)


# ------- optimistisk samtidighetskontroll («den som lagrer sist, vinner» skal ikke skje i det stille) -------
# Skjemaet faar med et fingeravtrykk av feltene slik de var da siden ble vist. Er raden endret av noen andre (en annen
# admin, eller morgenjobben som legger inn en Zoom-lenke) naar skjemaet lagres, lagres INGENTING og admin faar beskjed.
# Klienter som ikke sender feltet (eldre skjema/API) kontrolleres ikke. Kontroll og lagring skjer i samme forespoersel,
# saa vinduet er millisekunder - det lange vinduet (skjemaet staar aapent i minutter) er det som lukkes.
_VERSJONSFELTER = {
    "kurs": ("navn", "type", "sted", "zoom_url", "kapasitet", "pris_nok", "fakturering", "betaling", "faktura_dager_for",
             "kursholder_epost", "notat", "paameldingsfrist"),
    "person": ("fornavn", "etternavn", "epost", "telefon", "yrkestittel", "arbeidssted", "adresse", "postnr", "poststed"),
    "paamelding": ("betaler", "betaling", "org_navn", "org_nr", "faktura_adresse", "faktura_postnr", "faktura_sted",
                   "faktura_ref", "faktura_kommentar", "intern_kommentar", "allergier", "tilrettelegging"),
    "nettside": (paameldingsside.INTRO, paameldingsside.KNAPPETEKST),
}
_ENDRET_AV_ANDRE = ("Endringene ble ikke lagret: noen andre har endret dette mens du hadde siden åpen. Siden viser nå "
                    "de nyeste verdiene – gjør endringen din på nytt.")


def _versjon(hva: str, rad) -> str:
    verdier = [None if rad[f] is None else str(rad[f]) for f in _VERSJONSFELTER[hva]]
    return hashlib.sha256(json.dumps(verdier, ensure_ascii=False).encode()).hexdigest()[:20]


@app.template_global()
def versjon_for(hva: str, rad) -> str:
    """Til skjult felt i skjemaet. Vises skjemaet paa nytt etter en valideringsfeil, beholdes versjonen det ble aapnet med."""
    if request.method == "POST" and request.form.get("versjon"):
        return request.form["versjon"]
    return _versjon(hva, rad)


def _endret_av_andre(hva: str, rad) -> bool:
    mottatt = request.form.get("versjon")
    return mottatt is not None and mottatt != _versjon(hva, rad)


def _utvalg_versjon(p, valgte) -> str:
    """Fingeravtrykk av en ekstradeltakers samlingsutvalg slik det var da siden ble vist (samme prinsipp som _versjon, men utvalget
    ligger i en egen tabell): lagrer noen andre et annet utvalg mens skjemaet står åpent, lagres ingenting."""
    return hashlib.sha256(json.dumps([p["ekstradeltaker_ts"], sorted(valgte)], ensure_ascii=False).encode()).hexdigest()[:20]


def _opprett_sharepoint_mappe(kurs_id: int, kode: str) -> str | None:
    """Lager kursets SharePoint-mappe og lagrer stien. Kalles først NÅR kurset er lagret: en feil her stopper aldri
    kurset, og det blir aldri en foreldreløs mappe for et kurs som ikke finnes. Idempotent - kan prøves igjen.
    Returnerer mappen, eller None hvis den ikke kunne lages nå (feilen logges uten personopplysninger)."""
    try:
        mappe = sharepoint.opprett_kursmappe(kode)
    except Exception as e:  # noqa: BLE001 - nett, Graph eller oppsett: kurset står, mappen mangler og kan lages senere
        db.logg(con(), "sharepoint_mappe_feilet", {"kurs_id": kurs_id, "feil": sikker_feiltekst(e)}, aktor=_aktor())
        con().commit()
        logging.getLogger("kurs.integrasjoner").warning("SharePoint-mappe for kurs %s feilet: %s", kurs_id,
                                                        sikker_feiltekst(e))
        return None
    con().execute("UPDATE kurs SET sharepoint_mappe=? WHERE id=? AND sharepoint_mappe IS NULL", (mappe, kurs_id))
    con().commit()
    return mappe


@app.post("/admin/kurs/<int:kurs_id>/sharepoint-mappe")
@krever_admin
def admin_sharepoint_mappe(kurs_id):
    kurs = _hent_kurs(kurs_id)
    if kurs["sharepoint_mappe"]:
        flash("Kurset har allerede en SharePoint-mappe.", "info")
    elif _opprett_sharepoint_mappe(kurs_id, kurs["kode"]):
        flash("SharePoint-mappen er laget.", "ok")
    else:
        flash("SharePoint-mappen kunne ikke lages nå. Prøv igjen litt senere, eller kontakt systemansvarlig.", "feil")
    return redirect(url_for("admin_kurs_oppsett", kurs_id=kurs_id))


@app.get("/admin/kurs/<int:kurs_id>/oppsett")
@krever_admin
def admin_kurs_oppsett(kurs_id):
    return _render_oppsett(_hent_kurs(kurs_id))


def _kursdato_versjon(kurs_id: int) -> str:
    """Fingeravtrykk av kursgjennomføringen (samlinger og kursdager) slik den var da siden ble vist. Endrer noen andre
    datoene mens skjemaet står åpent, lagres ingenting (samme prinsipp som _versjon for kursfeltene)."""
    samlinger = [list(r) for r in con().execute(
        "SELECT id, navn, start_kl, slutt_kl, timer_pr_dag FROM samling WHERE kurs_id=? ORDER BY id", (kurs_id,))]
    dager = [list(r) for r in con().execute(
        "SELECT id, dato, samling_id, start_kl, slutt_kl, timer, merknad FROM kursdag WHERE kurs_id=? ORDER BY dato",
        (kurs_id,))]
    return hashlib.sha256(json.dumps([samlinger, dager], ensure_ascii=False, default=str).encode()).hexdigest()[:20]


def _render_oppsett(kurs, *, samlingsrader=None, datofeil=None, status: int = 200):
    """Oppsett-fanen. `samlingsrader` er innsendte rader fra kursgjennomføringen (vises igjen med feilene i `datofeil`)."""
    kurs_id = kurs["id"]
    dager = db.kursdager(con(), kurs_id)
    filer = sharepoint.list_filer(f"{kurs['sharepoint_mappe']}/Presentasjoner") if kurs["sharepoint_mappe"] else []
    # Oppmøte per kursdag: de som er satt opp på dagen (en ekstradeltaker bare på sine samlinger) - både oppmøtet og nevneren
    oppmote_antall = {r["kursdag_id"]: r["antall"] for r in con().execute(
        f"""SELECT o.kursdag_id, COUNT(*) AS antall FROM oppmote o JOIN kursdag kd ON kd.id=o.kursdag_id
            JOIN paamelding p ON p.id=o.paamelding_id
            WHERE kd.kurs_id=? AND {db.sql_egen_dag('p', 'kd')} GROUP BY o.kursdag_id""", (kurs_id,))}
    paa_dagen = {r["kursdag_id"]: r["antall"] for r in con().execute(
        f"""SELECT kd.id AS kursdag_id, COUNT(*) AS antall FROM kursdag kd JOIN paamelding p ON p.kurs_id=kd.kurs_id
            WHERE kd.kurs_id=? AND p.status='bekreftet' AND {db.sql_egen_dag('p', 'kd')} GROUP BY kd.id""", (kurs_id,))}
    if samlingsrader is None:
        lagrede = db.lagrede_samlinger(con(), kurs_id)
        samlingsrader = kursdatoer.rader_for_samlinger(lagrede)
        oppsummering = kursdatoer.oppsummering(lagrede)
    else:
        oppsummering = kursdatoer.oppsummering(kursdatoer.fra_skjema(request.form)[0])
    rad_for = {d["id"]: d for d in dager}
    dager_per_samling = [(g, [rad_for[i] for i in g["ider"]]) for g in kursdatoer.visning(dager, kurs)]
    forste = date.fromisoformat(dager[0]["dato"]) if dager else None
    materiell = con().execute("SELECT * FROM materiell_krav WHERE kurs_id=? ORDER BY frist, id", (kurs_id,)).fetchall()
    faktura_oversikt = db.okonomisk_binding_oversikt(con(), kurs_id)
    return render_template(
        "admin_kurs_oppsett.html", kurs=kurs, dager=dager, dager_per_samling=dager_per_samling, filer=filer,
        admins=_aktive_admins(), oppmote_antall=oppmote_antall, paa_dagen=paa_dagen,
        # bekreftet_antall = ALLE med plass (påmeldte og ekstradeltakere): alle får utsendinger og berøres av endret sted, format
        # og datoer. paameldte_antall og ekstra_antall er tallene hver for seg (ekstradeltakere teller ikke som påmeldte).
        bekreftet_antall=db.antall_med_plass(con(), kurs_id), paameldte_antall=db.antall_bekreftet(con(), kurs_id),
        ekstra_antall=db.antall_ekstradeltakere(con(), kurs_id),
        faktura_bundet=db.har_okonomisk_binding_for_kurs(con(), kurs_id),
        faktura_binding=_binding_tekst(faktura_oversikt), faktura_forsok=faktura_oversikt["forsok"],
        uvarslede=_uvarslede_avlysninger(kurs_id) if kurs["status"] == "avlyst" else 0,
        kurs_statusvalg=db.KURS_STATUS_OVERGANGER.get(kurs["status"], ()), fane="oppsett",
        v=_kursverdier(kurs), formater=_FORMAT, rader=samlingsrader or [kursdatoer.rad()], oppsummering=oppsummering,
        datofeil=datofeil or [], kursdato_versjon=_kursdato_versjon(kurs_id),
        forste_kursdag=forste.isoformat() if forste else "",
        fristforslag=kursdatoer.foreslaatt_frist(forste, _idag()).isoformat() if forste else "",
        materiell=materiell, idag_dato=_idag().isoformat()), status


@app.post("/admin/kurs/<int:kurs_id>/oppsett")
@krever_admin
def admin_kurs_oppsett_lagre(kurs_id):
    """Kursinnstillingene. Kursdatoene lagres for seg (admin_kurs_kursdatoer). Pris og fakturaoppsett kan endres, også når kurset
    har fakturaer (økonomisk bundet) - ellers måtte en utvikler inn i databasen for å rette en pris. Da må administratoren krysse
    av for bekreftelsen (bekreft_okonomi): fakturaer som er laget, endres ikke, og endringen gjelder bare det som ikke er laget
    ennå. Uten avkryssingen lagres alt det andre, men pris og fakturaoppsett står urørt (db.oppdater_kurs_felter stripper dem
    uansett), og administratoren får beskjed. Kursholderens e-post lagres bare når et eldre skjema sender feltet: forespørsler om
    presentasjon ligger nå under Kursmateriell."""
    kurs = _hent_kurs(kurs_id)
    if _endret_av_andre("kurs", kurs):
        flash(_ENDRET_AV_ANDRE, "feil")
        return redirect(url_for("admin_kurs_oppsett", kurs_id=kurs_id))
    f = request.form
    bundet = db.har_okonomisk_binding_for_kurs(con(), kurs_id)
    felter, feil = _les_kursskjema(f, bare_innsendte=bundet)
    kapasitet = felter.pop("kapasitet")
    frist, manuell, fristfeil = _les_frist(f)
    feil += fristfeil
    zoom_url = None
    try:
        zoom_url = sikkerhet.trygt_url_felt(f.get("zoom_url"))
    except ValueError as e:
        feil.append(f"Zoom-lenke: {e}")
    okonomi_endret = [n for n in db.KURS_LAASTE_FELT if n in felter and felter[n] != kurs[n]]
    bekreftet = bundet and f.get("bekreft_okonomi") == "1"
    if bekreftet and "pris_nok" in okonomi_endret:        # en pris som ville gitt en faktura på 0 kr, avvises før noe lagres
        prisfeil = db.prisendring_feil(con(), kurs_id, felter["pris_nok"], felter.get("fakturering", kurs["fakturering"]))
        if prisfeil:
            feil.append(prisfeil)
    if feil:
        for x in feil:
            flash(x, "feil")
        return redirect(url_for("admin_kurs_oppsett", kurs_id=kurs_id))

    felter.update(zoom_url=zoom_url, paameldingsfrist=frist)
    if manuell is not None:
        felter["paameldingsfrist_manuell"] = manuell
    if "kursholder_epost" in f:                       # eldre skjema
        felter["kursholder_epost"] = f.get("kursholder_epost", "").strip() or None
    if felter["type"] == "fysisk":
        felter["zoom_url"] = felter["zoom_id"] = felter["zoom_pw"] = None

    try:
        db.oppdater_kurs_felter(con(), kurs_id, felter, aktor=_aktor(), tillat_okonomi=bekreftet)
    except db.Paameldingsfeil as e:                      # ingenting er skrevet ennå (kontrollene i db kommer før lagringen)
        flash(str(e), "feil")
        return redirect(url_for("admin_kurs_oppsett", kurs_id=kurs_id))
    if "kursholder_epost" in felter and felter["kursholder_epost"] != (kurs["kursholder_epost"] or None):
        db.synk_materiell_ansvarlig(con(), kurs_id, felter["kursholder_epost"], aktor=_aktor())
    opprykket = db.endre_kapasitet(con(), kurs_id, kapasitet, aktor=_aktor()) if kapasitet != kurs["kapasitet"] else []
    con().commit()
    if opprykket:
        sveiper.kjor(Kjoring(con(), idag=_idag(), aktor=_aktor()))
        con().commit()
    flash("Kursoppsett oppdatert.", "ok")
    if bundet and okonomi_endret:
        if bekreftet:
            flash("Pris og fakturaoppsett er endret. Fakturaer som allerede er laget, er ikke endret.", "info")
        else:
            flash("Pris og fakturaoppsett er IKKE endret: kurset har allerede fakturaer, og endringen må bekreftes. Kryss av for "
                  "«Jeg forstår dette og vil endre pris eller fakturaoppsett», og lagre på nytt.", "feil")
    return redirect(url_for("admin_kurs_oppsett", kurs_id=kurs_id))


_MAKS_NAVN_I_MELDING = 5


def _utvalg_endret_melding(endret: list[dict]) -> str:
    """Meldingen når kursdatoene er lagret og ekstradeltakere har fått andre dager (db.lagre_samlinger: `utvalg_endret`): hvem som
    mistet eller fikk hvilke dager. Et samlingsutvalg skal aldri endres i det stille: påminnelser, innsjekk og kursbevis følger
    dagene. Navnene vises bare her, til administrator (aldri i loggen)."""
    ider = [u["paamelding_id"] for u in endret]
    navn = {r["id"]: r["navn"] for r in con().execute(
        f"SELECT p.id, d.navn FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE p.id IN ({','.join('?' * len(ider))})", ider)}
    deler = []
    for u in endret[:_MAKS_NAVN_I_MELDING]:
        fra, til = {date.fromisoformat(x) for x in u["fra_dager"]}, {date.fromisoformat(x) for x in u["til_dager"]}
        hva = [t for t in ((f"mistet {kursdatoer.datoliste(sorted(fra - til))}" if fra - til else ""),
                           (f"fikk {kursdatoer.datoliste(sorted(til - fra))}" if til - fra else "")) if t]
        deler.append(f"{navn.get(u['paamelding_id'], 'Ukjent')}: {' og '.join(hva)}.")
    flere = len(endret) - _MAKS_NAVN_I_MELDING
    antall = len(endret)
    ord_ = (db.PAAMELDINGSSTATUSER if antall == 1 else db.PAAMELDINGSSTATUSER_FLERTALL)["ekstradeltaker"].lower()
    return (f"Obs: {antall} {ord_} har fått andre kursdager fordi samlingene er endret. " + " ".join(deler)
            + (f" Og {flere} til." if flere > 0 else "")
            + f" Kontroller «{db.PAAMELDINGSSTATUSER['ekstradeltaker']}: deltar på» i deltakervinduet. "
            f"{'Deltakeren får' if antall == 1 else 'Deltakerne får'} ikke beskjed om endringen automatisk.")


@app.post("/admin/kurs/<int:kurs_id>/kursdatoer")
@krever_admin
def admin_kurs_kursdatoer(kurs_id):
    """Kursgjennomføringen (samlinger og kursdager) på Oppsett. En dag med oppmøte eller faktura kan ikke fjernes
    (db.lagre_samlinger). Ved feil vises skjemaet igjen med det admin skrev inn, og ingenting er lagret. Deltakerne
    varsles ikke automatisk om nye datoer."""
    kurs = _hent_kurs(kurs_id)
    f = request.form
    if f.get("kursdato_versjon") is not None and f.get("kursdato_versjon") != _kursdato_versjon(kurs_id):
        flash("Datoene ble ikke lagret: noen andre har endret kursdatoene mens du hadde siden åpen. Siden viser nå "
              "de nyeste datoene – gjør endringen din på nytt.", "feil")
        return redirect(url_for("admin_kurs_oppsett", kurs_id=kurs_id))
    samlinger, feil = kursdatoer.fra_skjema(f)
    resultat = None
    if not feil:
        try:
            with db.transaksjon(con()):
                resultat = db.lagre_samlinger(con(), kurs_id, samlinger, aktor=_aktor(), idag=_idag())
        except db.Kursdagfeil as e:
            feil = e.meldinger
    if feil:
        return _render_oppsett(kurs, samlingsrader=kursdatoer.rader_fra_innsendt(f), datofeil=feil, status=400)
    endring = []
    if resultat["lagt_til"]:
        endring.append(f"{resultat['lagt_til']} ny{'e' if resultat['lagt_til'] != 1 else ''} kursdag"
                       f"{'er' if resultat['lagt_til'] != 1 else ''}")
    if resultat["fjernet"]:
        endring.append(f"{resultat['fjernet']} kursdag{'er' if resultat['fjernet'] != 1 else ''} fjernet")
    flash("Kursdatoene er lagret" + (f" ({', '.join(endring)})." if endring else "."), "ok")
    if resultat["paameldingsfrist"] and resultat["paameldingsfrist"] != kurs["paameldingsfrist"]:
        flash("Påmeldingsfristen fulgte datoene og er nå "
              f"{kursdatoer.kort_dato(date.fromisoformat(resultat['paameldingsfrist']))}.", "info")
    if resultat["utvalg_endret"]:
        flash(_utvalg_endret_melding(resultat["utvalg_endret"]), "info")
    paameldte, ekstra = db.antall_bekreftet(con(), kurs_id), db.antall_ekstradeltakere(con(), kurs_id)
    if paameldte or ekstra:
        flash(f"Kurset har {paameldte} {db.PAAMELDINGSSTATUSER_FLERTALL['paameldt'].lower()} deltakere"
              + (f" og {ekstra} {(db.PAAMELDINGSSTATUSER if ekstra == 1 else db.PAAMELDINGSSTATUSER_FLERTALL)['ekstradeltaker'].lower()}" if ekstra else "")
              + ". De får ikke beskjed om endrede datoer automatisk – "
              "send dem gjerne en e-post fra Deltakere-fanen.", "info")
    return redirect(url_for("admin_kurs_oppsett", kurs_id=kurs_id))


@app.post("/admin/kurs/<int:kurs_id>/materiell")
@krever_admin
def admin_be_om_materiell(kurs_id):
    """Ber kursholderen om presentasjon (flyttet hit fra «Opprett kurs»)."""
    _hent_kurs(kurs_id)
    f = request.form
    navn, epost_, frist = (f.get("ansvarlig_navn", "").strip(), f.get("ansvarlig_epost", "").strip(),
                           f.get("frist", "").strip())
    feil = []
    if not navn:
        feil.append("Fyll inn navnet til kursholderen.")
    if not _GYLDIG_EPOST.fullmatch(epost_):
        feil.append("Fyll inn en gyldig e-postadresse til kursholderen.")
    try:
        date.fromisoformat(frist)
    except ValueError:
        feil.append("Fyll inn en gyldig frist for presentasjonen.")
    if feil:
        for x in feil:
            flash(x, "feil")
    else:
        db.be_om_materiell(con(), kurs_id, navn=navn, epost=epost_, frist=frist, aktor=_aktor())
        con().commit()
        flash(f"Forespørselen er lagret. Kursholderen får påminnelse med opplastingslenke 7, 2 og 0 dager før fristen "
              f"({kursdatoer.kort_dato(date.fromisoformat(frist))}).", "ok")
    return redirect(url_for("admin_kurs_oppsett", kurs_id=kurs_id) + "#kursmateriell")


@app.post("/admin/kurs/<int:kurs_id>/materiell/<int:krav_id>/fjern")
@krever_admin
def admin_fjern_materiell_krav(kurs_id, krav_id):
    _hent_kurs(kurs_id)
    if db.fjern_materiell_krav(con(), kurs_id, krav_id, aktor=_aktor()):
        con().commit()
        flash("Forespørselen er fjernet. Kursholderen får ingen flere påminnelser om den.", "ok")
    else:
        flash("Forespørselen finnes ikke, eller presentasjonen er allerede levert.", "feil")
    return redirect(url_for("admin_kurs_oppsett", kurs_id=kurs_id) + "#kursmateriell")


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


def _avlysningsmottakere(kurs_id: int):
    return con().execute(
        """SELECT p.id, d.navn, d.fornavn, d.epost FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id
           WHERE p.kurs_id=? AND p.status IN ('bekreftet','venteliste') ORDER BY p.id""", (kurs_id,)).fetchall()


def _uvarslede_avlysninger(kurs_id: int) -> int:
    """Mottakere av et avlyst kurs som ikke har fått varselet og kan få det nå: ingen forsøk, eller et forsøk som er
    avklart som ikke sendt ('feilet'). Uavklarte forsøk vises under «Uavklarte operasjoner» og røres ikke her."""
    return con().execute(
        """SELECT COUNT(*) FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id
           WHERE p.kurs_id=? AND p.status IN ('bekreftet','venteliste')
             AND NOT EXISTS (SELECT 1 FROM utsending_logg u WHERE u.nokkel=? AND u.mottaker=d.epost
                             AND u.type='avlysning' AND u.status != 'feilet')""",
        (kurs_id, f"kurs:{kurs_id}")).fetchone()[0]


def _send_avlysninger(k: Kjoring, kurs, mottakere):
    """Avlysningsvarsel til alle - via claim-motoren (aldri to ganger til samme person, en feil stopper ikke de andre)."""
    ut = k.send_til_mange(f"kurs:{kurs['id']}", "avlysning", mottakere,
                          lambda d: k.render_for_sending("avlysning", d=d, kurs=kurs), mal="avlysning")
    con().commit()
    return ut


@app.post("/admin/kurs/<int:kurs_id>/avlys")
@krever_admin
def admin_avlys_kurs(kurs_id):
    kurs = _hent_kurs(kurs_id)
    mottakere = _avlysningsmottakere(kurs_id)
    k = Kjoring(con(), idag=_idag(), aktor=_aktor())
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
    ut = _send_avlysninger(k, kurs, mottakere)
    flash(f"Kurset er avlyst og {len(ut.sendt)} deltaker(e) er varslet på e-post. "
         "Husk å kreditere eventuelle fakturaer manuelt i Visma – det gjøres ikke automatisk.", "ok")
    _flash_sendefeil(ut)
    return redirect(url_for("admin_kurs_oppsett", kurs_id=kurs_id))


@app.post("/admin/kurs/<int:kurs_id>/avlys/varsle")
@krever_admin
def admin_avlysning_varsle(kurs_id):
    """Sender avlysningsvarselet til dem som ikke fikk det (f.eks. fordi e-posttjenesten var nede). Trygt å trykke flere
    ganger: claim-motoren sender aldri to ganger til samme person, og uavklarte forsøk røres ikke."""
    kurs = _hent_kurs(kurs_id)
    if kurs["status"] != "avlyst":
        flash("Kurset er ikke avlyst.", "feil")
        return redirect(url_for("admin_kurs_oppsett", kurs_id=kurs_id))
    ut = _send_avlysninger(Kjoring(con(), idag=_idag(), aktor=_aktor()), kurs, _avlysningsmottakere(kurs_id))
    if ut.sendt:
        flash(f"Avlysningsvarsel sendt til {len(ut.sendt)} deltaker(e).", "ok")
    elif not (ut.uavklart or ut.ikke_forsokt or ut.uavklart_fra_for):
        flash("Alle har allerede fått avlysningsvarselet.", "info")
    _flash_sendefeil(ut)
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
    verdi = request.form.get("ansvarlig_admin_id", type=int)
    if verdi and not con().execute("SELECT 1 FROM admin_bruker WHERE id=?", (verdi,)).fetchone():
        abort(400)
    con().execute("UPDATE kurs SET ansvarlig_admin_id=? WHERE id=?", (verdi or None, kurs_id))
    db.logg(con(), "kurs_ansvarlig_endret", {"kurs_id": kurs_id, "ansvarlig_admin_id": verdi}, aktor=_aktor())
    con().commit()
    flash("Ansvarlig oppdatert.", "ok")
    return redirect(url_for("admin_kurs_oppsett", kurs_id=kurs_id))


def _render_nettside_admin(kurs, intro: str, knappetekst: str, status: int = 200):
    """`intro`/`knappetekst` er det som staar i feltene (lagret verdi, eller admins innsending ved feil)."""
    ugyldig = []
    for kolonne, navn, normaliser in ((paameldingsside.INTRO, "intro", paameldingsside.normaliser_intro),
                                      (paameldingsside.KNAPPETEKST, "knappetekst",
                                       paameldingsside.normaliser_knappetekst)):
        try:
            normaliser(kurs[kolonne])
        except paameldingsside.SideFeil:
            ugyldig.append(navn)
    return render_template("admin_kurs_nettside.html", kurs=kurs, fane="nettside", intro=intro, knappetekst=knappetekst,
                           offentlig_lenke=_offentlig_lenke(kurs), offentlig=_kurs_offentlig_tilgjengelig(kurs),
                           ugyldig=ugyldig, maks_intro=paameldingsside.MAKS_INTRO,
                           maks_knappetekst=paameldingsside.MAKS_KNAPPETEKST,
                           standard_knappetekst=paameldingsside.STANDARD_KNAPPETEKST), status


@app.get("/admin/kurs/<int:kurs_id>/nettside")
@krever_admin
def admin_kurs_nettside(kurs_id):
    kurs = _hent_kurs(kurs_id)
    # Den LAGREDE verdien vises (ogsaa en ugyldig en), slik at admin kan se og rette den.
    return _render_nettside_admin(kurs, kurs[paameldingsside.INTRO] or "", kurs[paameldingsside.KNAPPETEKST] or "")


@app.post("/admin/kurs/<int:kurs_id>/nettside")
@krever_admin
def admin_kurs_nettside_lagre(kurs_id):
    """Fase 12C5: lagrer introduksjonstekst og knappetekst. BEGGE valideres foer noe skrives; ved feil 400 og ingen
    endring. Ett samlet oppdater_kurs_felter-kall (logger kun feltnavn, aldri innhold). Tom verdi -> NULL (standard)."""
    kurs = _hent_kurs(kurs_id)
    if _endret_av_andre("nettside", kurs):
        flash(_ENDRET_AV_ANDRE, "feil")
        return redirect(url_for("admin_kurs_nettside", kurs_id=kurs_id))
    intro, knappetekst = request.form.get("intro", ""), request.form.get("knappetekst", "")
    feil, verdier = [], {}
    for kolonne, verdi, normaliser in ((paameldingsside.INTRO, intro, paameldingsside.normaliser_intro),
                                       (paameldingsside.KNAPPETEKST, knappetekst,
                                        paameldingsside.normaliser_knappetekst)):
        try:
            verdier[kolonne] = normaliser(verdi)
        except paameldingsside.SideFeil as e:
            feil.append(e.forklaring)
    if feil:
        for x in feil:
            flash(f"{x} Ingen endringer er lagret.", "feil")
        return _render_nettside_admin(kurs, intro, knappetekst, 400)
    with db.transaksjon(con()):
        endret = db.oppdater_kurs_felter(con(), kurs_id, verdier, aktor=_aktor())
    flash("Nettsideinnstillingene er lagret." if endret else "Ingen endringer å lagre.", "ok")
    return redirect(url_for("admin_kurs_nettside", kurs_id=kurs_id))


@app.get("/admin/kurs/<int:kurs_id>/forhandsvis-paamelding")
@krever_admin
def admin_forhandsvis_paamelding(kurs_id):
    """Forhaandsvisningen ER den ekte paameldingssiden (/kurs/<kode>) - samme adresse som deltakerne bruker, og som kan
    kopieres fra nettleseren og sendes videre. Kurs som ikke er offentlige, ser bare innloggede administratorer, og da
    uten innsending (se kursside). Ruten finnes for gamle lenker og bokmerker."""
    return redirect(url_for("kursside", kode=_hent_kurs(kurs_id)["kode"]))


# ---------- skjemabyggeren (fanen «Påmeldingsskjema») ----------
# All validering/normalisering skjer i skjemafelt/ekstrafelt/db - web-laget oversetter KUN det faste admin-skjemaet til
# komplette overstyringer (standardfeltene og informasjonsboksen) og komplette felt (kursets egne felt). Andre POST-felt
# leses aldri, saa laaste felt, systemblokker, ukjente felt/egenskaper og HPR-laasene kan ikke naas herfra. Hele
# oppsettet sendes og lagres hver gang i EN transaksjon. Ingen laasing mellom samtidige admin-brukere, men et skjema som
# ikke passer med det som er lagret (et felt er lagt til eller slettet i mellomtiden), avvises uten lagring.

_SKJEMA_EGENSKAP_NAVN = {"synlig": "vis feltet", "obligatorisk": "obligatorisk", "rekkefolge": "rekkefølge",
                         "label": "feltnavn", "hjelpetekst": "hjelpetekst", "type": "type", "valg": "svaralternativer",
                         "plassering": "plassering", "vis_naar": "vis bare når"}
_SKJEMA_GRUNN_TEKST = {skjemafelt.UKJENT_FELT: "ukjent felt", skjemafelt.LAAST_FELT: "låst felt",
                       skjemafelt.IKKE_TILLATT: "kan ikke endres for dette feltet",
                       skjemafelt.UGYLDIG_TYPE: "ugyldig verdi", skjemafelt.FOR_LANG: "for lang tekst",
                       skjemafelt.KONTROLLTEGN: "ugyldige tegn", skjemafelt.UTENFOR_OMRAADE: "ugyldig verdi",
                       ekstrafelt.UKJENT_TYPE: "ukjent felttype", ekstrafelt.MANGLER: "mangler",
                       ekstrafelt.UGYLDIGE_VALG: "ugyldige svaralternativer",
                       ekstrafelt.UKJENT_PLASSERING: "ukjent plassering",
                       ekstrafelt.UGYLDIG_BETINGELSE: "«Vis bare når» kan ikke brukes, så feltet vises ikke i skjemaet"}
_STANDARDFELT_I_BYGGEREN = (*skjemafelt.KONFIGURERBAR_GRUPPE, *skjemafelt.FAKTURAFELT, *skjemafelt.SENSITIVE_FELT)
_SKJEMA_UTDATERT = ("Skjemaet er endret et annet sted siden du åpnet siden (for eksempel av en annen administrator). "
                    "Ingen endringer er lagret. Last siden på nytt og gjør endringen igjen.")


class _SkjemaUtdatert(Exception):
    """Det innsendte skjemaet passer ikke med oppsettet som er lagret (manipulert, eller endret i mellomtiden)."""


def _skjema_advarsel_tekster(advarsler, egne=()) -> list[str]:
    """PII-fri, lesbar tekst per advarsel: kjent felt (eller feltnavnet admin har gitt et eget felt), kjent egenskap og
    grunn - aldri lagret innhold eller ukjente navn fra databasen."""
    navn_egne = {e.id: e.label for e in egne}
    ut = []
    for a in advarsler:
        if isinstance(a, ekstrafelt.Advarsel):
            felt = (f"Eget felt «{navn_egne[a.felt_id]}»" if a.felt_id in navn_egne else
                    f"Eget felt nr. {a.felt_id}" if a.felt_id else "Et eget felt")
        else:
            felt = skjemafelt.REGISTER[a.felt].label if a.felt in skjemafelt.REGISTER else "Ukjent felt"
        egenskap = f" – {_SKJEMA_EGENSKAP_NAVN[a.egenskap]}" if a.egenskap in _SKJEMA_EGENSKAP_NAVN else ""
        ut.append(f"{felt}{egenskap}: {_SKJEMA_GRUNN_TEKST.get(a.grunn, 'ugyldig verdi')}")
    return ut


def _egne_i_rekkefolge(egne) -> list:
    """Kursets egne felt slik skjemaet viser dem: «Om deg» foer «Til slutt», deretter plass og id."""
    return sorted(egne, key=lambda e: (e.plassering != skjemafelt.OM_DEG, e.rekkefolge, e.id))


def _skjema_admin_verdier(overstyringer, egne, antall_svar=None) -> dict:
    """Det skjemabyggeren skal vise for dagens oppsett: effektive verdier (standard der ingen overstyring finnes)."""
    antall_svar = antall_svar or {}
    verdier = {}
    for n in (*_STANDARDFELT_I_BYGGEREN, *skjemafelt.INFOFELT, "hpr_nr"):
        f, o = skjemafelt.REGISTER[n], overstyringer.get(n) or skjemafelt.Overstyring()
        verdier[n] = {"synlig": f.synlig if o.synlig is None else o.synlig,
                      "obligatorisk": f.obligatorisk if o.obligatorisk is None else o.obligatorisk,
                      "label": o.label or f.label,
                      "hjelpetekst": (f.hjelpetekst or "") if o.hjelpetekst is None else o.hjelpetekst,
                      # rekkefølgen vises av selve plasseringen i tabellen, ikke som «Tilpasset»
                      "tilpasset": any(getattr(o, e) is not None for e in ("synlig", "obligatorisk", "label",
                                                                           "hjelpetekst"))}
    for e in egne:
        verdier[e.referanse] = {
            "type": e.type, "label": e.label, "hjelpetekst": e.hjelpetekst or "", "valg": "\n".join(e.valg),
            "obligatorisk": e.obligatorisk, "synlig": e.synlig, "plassering": e.plassering,
            "vis_naar": f"{e.vis_naar_felt}={e.vis_naar_verdi}" if e.vis_naar_felt else "",
            "betingelse_ok": e.betingelse_ok, "antall_svar": antall_svar.get(e.id, 0)}
    verdier["rekkefolge"] = {p: skjemafelt.plassrekkefolge(overstyringer, egne, p)
                             for p in (skjemafelt.OM_DEG, skjemafelt.TIL_SLUTT)}
    return verdier


def _skjema_admin_innsendt(form, overstyringer, egne, antall_svar=None) -> tuple:
    """Admin-POST -> (komplette egenskaper per standardfelt til db.lagre_skjemafelt, {felt_id: data} per eget felt til
    ekstrafelt.normaliser, {felt_id: plass}, verdier til ev. re-rendring). Leser KUN de faste navnene i skjemabyggeren og
    felt som finnes paa kurset. Avmerkingsboks: tilstede = ja. Rekkefolgen settes for ALLE felt i «Om deg» og «Til
    slutt» hver gang (lik standard -> normaliseres bort i db), slik at en flytting aldri kan gi et halvt oppsett.
    Kaster _SkjemaUtdatert naar feltene eller rekkefolgen ikke passer med det som er lagret."""
    antall_svar = antall_svar or {}
    egenskaper, verdier = {}, {}
    for n in _STANDARDFELT_I_BYGGEREN:
        f = skjemafelt.REGISTER[n]
        e = {"synlig": f"{n}_synlig" in form, "label": form.get(f"{n}_label", ""),
             "hjelpetekst": form.get(f"{n}_hjelpetekst", "")}
        if skjemafelt.OBLIGATORISK in f.overstyrbart:
            e["obligatorisk"] = f"{n}_obligatorisk" in form
        egenskaper[n] = e
        verdier[n] = {"obligatorisk": f.obligatorisk, **e, "tilpasset": None}   # None = ukjent -> intet merke
    for n in skjemafelt.INFOFELT:
        egenskaper[n] = {"synlig": f"{n}_synlig" in form}
        verdier[n] = {**egenskaper[n], "tilpasset": None}
    egenskaper["hpr_nr"] = {"hjelpetekst": form.get("hpr_nr_hjelpetekst", "")}
    verdier["hpr_nr"] = {"hjelpetekst": egenskaper["hpr_nr"]["hjelpetekst"], "tilpasset": None}

    ekstra = {}
    for x in egne:
        p = f"ekstra_{x.id}_"
        if p + "label" not in form:
            raise _SkjemaUtdatert()
        vis_naar = form.get(p + "vis_naar", "")
        styrer, _, verdi = vis_naar.partition("=")
        ekstra[x.id] = {"type": form.get(p + "type", x.type), "label": form.get(p + "label", ""),
                        "hjelpetekst": form.get(p + "hjelpetekst", ""), "valg": form.get(p + "valg", ""),
                        "obligatorisk": p + "obligatorisk" in form, "synlig": p + "synlig" in form,
                        "plassering": form.get(p + "plassering", x.plassering),
                        "vis_naar_felt": styrer or None, "vis_naar_verdi": verdi if styrer else None}
        verdier[x.referanse] = {**ekstra[x.id], "vis_naar": vis_naar, "betingelse_ok": True,
                                "antall_svar": antall_svar.get(x.id, 0)}

    naa, rekkefolge = {}, {}
    for plassering in (skjemafelt.OM_DEG, skjemafelt.TIL_SLUTT):
        naa[plassering] = skjemafelt.plassrekkefolge(overstyringer, egne, plassering)
        innsendt = [n for n in form.get(f"rekkefolge_{plassering}", "").split(",") if n]
        if len(innsendt) != len(set(innsendt)) or set(innsendt) != set(naa[plassering]):
            raise _SkjemaUtdatert()
        rekkefolge[plassering] = innsendt
    for felt_id, data in ekstra.items():            # flyttet til en annen del av skjemaet: sist der
        ref = f"{skjemafelt.EKSTRA_REF}{felt_id}"
        ny = data["plassering"]
        if ny in rekkefolge and ref not in rekkefolge[ny]:
            for liste in rekkefolge.values():
                if ref in liste:
                    liste.remove(ref)
            rekkefolge[ny].append(ref)
    felt, _, retning = form.get("flytt", "").rpartition(":")    # ▲/▼ uten JavaScript: «telefon:opp»
    for liste in rekkefolge.values():
        if felt in liste and retning in ("opp", "ned"):
            i = liste.index(felt)
            j = i - 1 if retning == "opp" else i + 1
            if 0 <= j < len(liste):
                liste[i], liste[j] = liste[j], liste[i]
    # Plassene nummereres 1, 2, 3 ... bare i en del av skjemaet der rekkefølgen er endret. Ellers beholdes dagens
    # plasser, slik at en lagring uten endringer aldri skriver noe.
    dagens = {f"{skjemafelt.EKSTRA_REF}{x.id}": x.rekkefolge for x in egne}
    for n in skjemafelt.KONFIGURERBAR_GRUPPE:
        o = overstyringer.get(n) or skjemafelt.Overstyring()
        dagens[n] = skjemafelt.REGISTER[n].rekkefolge if o.rekkefolge is None else o.rekkefolge
    plass = {}
    for plassering, liste in rekkefolge.items():
        for i, n in enumerate(liste, 1):
            nummer = dagens[n] if liste == naa[plassering] else i
            if n.startswith(skjemafelt.EKSTRA_REF):
                plass[int(n[len(skjemafelt.EKSTRA_REF):])] = nummer
            else:
                egenskaper[n]["rekkefolge"] = nummer
    verdier["rekkefolge"] = rekkefolge
    return egenskaper, ekstra, plass, verdier


def _betingelsesvalg(kurs, egne) -> dict:
    """{felt_id: [(verdi, tekst)] eller None} - det hvert eget felt kan vises etter («Vis bare når»): «Betale privat /
    firma» (naar kurset har fakturablokk) og kursets andre felt med svaralternativer som selv vises alltid. Ett nivaa:
    et felt som andre vises etter, kan ikke selv vises bare av og til (None)."""
    styrer = {e.vis_naar_felt for e in egne if e.vis_naar_felt}
    kandidater = [e for e in egne if e.type in ekstrafelt.KAN_STYRE and e.vis_naar_felt is None]
    ut = {}
    for e in egne:
        if e.referanse in styrer:
            ut[e.id] = None
            continue
        valg = []
        if skjemafelt.vis_fakturablokk(kurs) or e.vis_naar_felt == skjemafelt.BETALER:
            valg += [(f"{skjemafelt.BETALER}={v}", f"«{ekstrafelt.BETALER_NAVN}» er «{tekst}»")
                     for v, tekst in ekstrafelt.BETALER_VALG.items()]
        for k in kandidater:
            if k.id != e.id:
                valg += ([(f"{k.referanse}={ekstrafelt.AVKRYSSET}", f"«{k.label}» er krysset av")]
                         if k.type == "avkrysning" else [(f"{k.referanse}={v}", f"«{k.label}» er «{v}»") for v in k.valg])
        naa = f"{e.vis_naar_felt}={e.vis_naar_verdi}" if e.vis_naar_felt else ""
        if naa and naa not in dict(valg):
            valg.append((naa, "Ugyldig – velg en ny betingelse"))
        ut[e.id] = valg
    return ut


def _render_skjema_admin(kurs, verdier, advarsler=(), egne=(), antall_svar=None, *, nytt=None, aapne=(), status=200):
    lesbare = {e.id for e in egne}
    return render_template(
        "admin_kurs_paameldingsskjema.html", kurs=kurs, fane="paameldingsskjema", v=verdier,
        register=skjemafelt.REGISTER, egne={e.id: e for e in egne}, antall_svar=antall_svar or {},
        adressefelt=skjemafelt.ADRESSEFELT,
        firmafelt=[n for n in skjemafelt.FAKTURAFELT if skjemafelt.REGISTER[n].betaler == skjemafelt.ORGANISASJON],
        sensitive=skjemafelt.SENSITIVE_FELT, infofelt=skjemafelt.INFOFELT, typer=ekstrafelt.TYPER,
        med_valg=ekstrafelt.MED_VALG, plasseringer=ekstrafelt.PLASSERINGER, maler=ekstrafelt.MALER,
        betingelsesvalg=_betingelsesvalg(kurs, egne), vis_blokk=skjemafelt.vis_fakturablokk(kurs),
        vis_sensitiv=skjemafelt.vis_sensitive_felt(kurs), vis_hpr=skjemafelt.vis_hpr(kurs), hpr_i_skjema=skjemafelt.HPR_I_SKJEMA,
        offentlig=_kurs_offentlig_tilgjengelig(kurs), offentlig_lenke=_offentlig_lenke(kurs),
        maks_label=skjemafelt.MAKS_LABEL, maks_hjelpetekst=skjemafelt.MAKS_HJELPETEKST,
        maks_hjelpetekst_egne=ekstrafelt.MAKS_HJELPETEKST, advarsler=_skjema_advarsel_tekster(advarsler, egne),
        uleselige=sorted({a.felt_id for a in advarsler if isinstance(a, ekstrafelt.Advarsel) and a.felt_id
                          and a.felt_id not in lesbare}),
        nytt=nytt or {}, aapne=set(aapne)), status


def _skjema_admin_utilgjengelig(kurs):
    return render_template("admin_kurs_paameldingsskjema.html", kurs=kurs, fane="paameldingsskjema",
                           lesefeil=True), 503


def _les_skjemaoppsett(kurs_id: int) -> tuple:
    """(overstyringer, egne felt, alle advarsler, antall svar per eget felt). Kaster SkjemaLesefeil."""
    les = db.hent_skjemaoverstyringer(con(), kurs_id)
    egne = db.hent_ekstrafelt(con(), kurs_id)
    return les.overstyringer, egne.felt, (*les.advarsler, *egne.advarsler), db.antall_svar_per_ekstrafelt(con(), kurs_id)


@app.get("/admin/kurs/<int:kurs_id>/paameldingsskjema")
@krever_admin
def admin_kurs_paameldingsskjema(kurs_id):
    """Skjemabyggeren. Kun lesing - skriver aldri noe (heller ikke advarsler). Fail-closed ved lesefeil: da vises IKKE
    et redigerbart standardskjema."""
    kurs = _hent_kurs(kurs_id)
    try:
        overstyringer, egne, advarsler, antall_svar = _les_skjemaoppsett(kurs_id)
    except skjemafelt.SkjemaLesefeil:
        return _skjema_admin_utilgjengelig(kurs)
    return _render_skjema_admin(kurs, _skjema_admin_verdier(overstyringer, egne, antall_svar), advarsler, egne,
                                antall_svar)


@app.post("/admin/kurs/<int:kurs_id>/paameldingsskjema")
@krever_admin
def admin_kurs_paameldingsskjema_lagre(kurs_id):
    """Lagrer HELE oppsettet atomisk: standardfeltene, informasjonsboksen, kursets egne felt og rekkefolgen i EN
    transaksjon. En ugyldig verdi (SkjemafeltFeil/EkstrafeltFeil) eller DB-feil ruller tilbake alt - aldri en halv
    oppdatering. Lagrer ingenting hvis dagens oppsett ikke kan leses, eller skjemaet ikke passer med det som er lagret."""
    kurs = _hent_kurs(kurs_id)
    try:
        overstyringer, egne, advarsler, antall_svar = _les_skjemaoppsett(kurs_id)
    except skjemafelt.SkjemaLesefeil:
        return _skjema_admin_utilgjengelig(kurs)
    try:
        egenskaper, ekstra, plass, verdier = _skjema_admin_innsendt(request.form, overstyringer, egne, antall_svar)
    except _SkjemaUtdatert:
        flash(_SKJEMA_UTDATERT, "feil")
        return _render_skjema_admin(kurs, _skjema_admin_verdier(overstyringer, egne, antall_svar), advarsler, egne,
                                    antall_svar, status=400)
    try:
        normaliserte = {felt_id: ekstrafelt.normaliser(data, felt_id) for felt_id, data in ekstra.items()}
        ekstrafelt.kontroller_betingelser({felt_id: ekstrafelt.som_felt(felt_id, v, plass[felt_id])
                                           for felt_id, v in normaliserte.items()})
        with db.transaksjon(con()):
            endret = [db.lagre_skjemafelt(con(), kurs_id, felt, e, aktor=_aktor()) for felt, e in egenskaper.items()]
            endret += [db.oppdater_ekstrafelt(con(), kurs_id, felt_id, v, plass[felt_id], aktor=_aktor())
                       for felt_id, v in normaliserte.items()]
    except skjemafelt.SkjemafeltFeil as e:
        navn = skjemafelt.REGISTER[e.felt].label if e.felt in skjemafelt.REGISTER else "Skjemaet"
        flash(f"{navn}: {e.forklaring or 'Ugyldig verdi.'} Ingen endringer er lagret.", "feil")
        return _render_skjema_admin(kurs, verdier, advarsler, egne, antall_svar, aapne=[e.felt], status=400)
    except ekstrafelt.EkstrafeltFeil as e:
        navn = (ekstra.get(e.felt_id) or {}).get("label", "").strip() or "Et eget felt"
        tekst = e.forklaring if e.grunn == ekstrafelt.UGYLDIG_BETINGELSE else f"«{navn}»: {e.forklaring}"
        flash(f"{tekst} Ingen endringer er lagret.", "feil")
        return _render_skjema_admin(kurs, verdier, advarsler, egne, antall_svar,
                                    aapne=[f"{skjemafelt.EKSTRA_REF}{e.felt_id}"], status=400)
    except db.DatabaseFeil:
        return _skjema_admin_utilgjengelig(kurs)
    flash("Påmeldingsskjemaet er lagret." if any(endret) else "Ingen endringer å lagre.", "ok")
    return redirect(url_for("admin_kurs_paameldingsskjema", kurs_id=kurs_id))


@app.post("/admin/kurs/<int:kurs_id>/paameldingsskjema/nytt-felt")
@krever_admin
def admin_kurs_ekstrafelt_ny(kurs_id):
    """Legger til et eget felt («Legg til et ekstrafelt» eller et hurtigvalg) sist i valgt del av skjemaet."""
    kurs = _hent_kurs(kurs_id)
    mal = request.form.get("mal")
    if mal is not None:
        if mal not in ekstrafelt.MALER:
            abort(400)
        data = dict(ekstrafelt.MALER[mal][1])
    else:
        data = {"type": request.form.get("type", ""), "label": request.form.get("label", ""),
                "valg": request.form.get("valg", ""),
                "plassering": request.form.get("plassering", skjemafelt.OM_DEG)}
    try:
        overstyringer, egne, advarsler, antall_svar = _les_skjemaoppsett(kurs_id)
    except skjemafelt.SkjemaLesefeil:
        return _skjema_admin_utilgjengelig(kurs)
    plassering = data["plassering"] if data.get("plassering") in ekstrafelt.PLASSERINGER else skjemafelt.OM_DEG
    try:
        with db.transaksjon(con()):
            felt_id = db.opprett_ekstrafelt(con(), kurs_id, data, skjemafelt.neste_plass(overstyringer, egne, plassering),
                                            aktor=_aktor())
    except ekstrafelt.EkstrafeltFeil as e:
        flash(f"Feltet ble ikke lagt til: {e.forklaring or 'ugyldig verdi.'}", "feil")
        return _render_skjema_admin(kurs, _skjema_admin_verdier(overstyringer, egne, antall_svar), advarsler, egne,
                                    antall_svar, nytt=data if mal is None else None, status=400)
    except db.DatabaseFeil:
        return _skjema_admin_utilgjengelig(kurs)
    hvor = "under «Om deg»" if plassering == skjemafelt.OM_DEG else "til slutt i skjemaet"
    flash(f"Feltet «{data['label'].strip()}» er lagt til {hvor}. Under «Mer» kan du endre hjelpetekst, "
          "svaralternativer og når feltet skal vises.", "ok")
    return redirect(url_for("admin_kurs_paameldingsskjema", kurs_id=kurs_id, _anchor=f"felt-ekstra-{felt_id}"))


@app.post("/admin/kurs/<int:kurs_id>/paameldingsskjema/felt/<int:felt_id>/slett")
@krever_admin
def admin_kurs_ekstrafelt_slett(kurs_id, felt_id):
    """Sletter et eget felt. Et felt som har svar, eller som et annet felt vises etter, slettes aldri (db nekter)."""
    kurs = _hent_kurs(kurs_id)
    try:
        with db.transaksjon(con()):
            db.slett_ekstrafelt(con(), kurs_id, felt_id, aktor=_aktor())
    except ekstrafelt.EkstrafeltFeil as e:
        flash(f"Feltet ble ikke slettet. {e.forklaring}", "feil")
        return redirect(url_for("admin_kurs_paameldingsskjema", kurs_id=kurs_id))
    except db.DatabaseFeil:
        return _skjema_admin_utilgjengelig(kurs)
    flash("Feltet er slettet.", "ok")
    return redirect(url_for("admin_kurs_paameldingsskjema", kurs_id=kurs_id))


@app.post("/admin/kurs/<int:kurs_id>/paameldingsskjema/tilbakestill")
@krever_admin
def admin_kurs_paameldingsskjema_tilbakestill(kurs_id):
    """Fjerner ALLE overstyringer av standardfeltene og informasjonsboksen (ogsaa korrupte/ukjente rader) -> kodet
    standard. Kursets egne felt beholdes (de slettes enkeltvis i skjemabyggeren)."""
    kurs = _hent_kurs(kurs_id)
    try:
        with db.transaksjon(con()):
            antall = db.tilbakestill_skjema(con(), kurs_id, aktor=_aktor())
    except db.DatabaseFeil:
        return _skjema_admin_utilgjengelig(kurs)
    flash("Standardfeltene er tilbakestilt til standard." if antall else "Standardfeltene har allerede standardoppsettet.",
          "ok")
    return redirect(url_for("admin_kurs_paameldingsskjema", kurs_id=kurs_id))


# Statusvalgene i deltakerlisten er hele statusmodellen (db.PAAMELDINGSSTATUSER), i visningsrekkefølge. Verdiene er de
# samme som data-status på radene (filteret i static/app.js).
DELTAKER_STATUSVALG = db.PAAMELDINGSSTATUSER


def _deltaker_sok_status(request_args) -> tuple[str, list[str]]:
    """Leser/validerer søk og statusvalg (ingen, én eller flere status=...) fra query-parametre. Delt mellom
    deltakerlisten og CSV-eksporten, slik at eksporten følger NØYAKTIG samme filter som admin ser på skjermen.
    Ukjente statusverdier ignoreres."""
    sok = request_args.get("sok", "").strip()
    valgt = request_args.getlist("status")
    return sok, [s for s in DELTAKER_STATUSVALG if s in valgt]


def _liste_adresse(kurs_id: int, sok: str, statuser: list[str]) -> str:
    """Adressen til deltakerlisten for kurset med søket og statusvalgene (samme form som live-søket skriver i adressefeltet)."""
    return url_for("admin_kurs_deltakere", kurs_id=kurs_id, sok=sok or None, status=statuser or None)


def _tilbake_til_listen(kurs_id: int, neste) -> str | None:
    """Statusvelgeren i deltakerlisten sender med `neste`: listen slik den står nå (søk og statusvalg). Returnerer en
    adresse til DENNE kursets deltakerliste bygget på nytt fra søket og statusene (aldri `neste` selv), ellers None.
    Alt annet - en annen side, et annet kurs, en full adresse eller //annet.sted - avvises, så det ikke kan brukes til
    å sende admin videre til et annet sted."""
    if not isinstance(neste, str) or sikkerhet.trygg_neste(neste, "") == "":
        return None
    deler = urlsplit(neste)
    if deler.path != url_for("admin_kurs_deltakere", kurs_id=kurs_id):
        return None
    sok, statuser = _deltaker_sok_status(MultiDict(parse_qsl(deler.query, keep_blank_values=False)))
    return _liste_adresse(kurs_id, sok[:200], statuser)


def _statusnokkel(p) -> str:
    return db.paameldingsstatus(p)


def _sokbar(p) -> str:
    """Teksten søket leter i: navn og e-post med små bokstaver. Python gjør også Æ, Ø og Å små (det gjør ikke SQLite)."""
    return f"{p['navn']} {p['epost']}".lower()


def _treffer(p, sok: str, statuser: list[str]) -> bool:
    """Samme regel som live-søket i static/app.js: delvis treff i navn eller e-post uten hensyn til store og små
    bokstaver, og én av de valgte statusene (ingen valgt = alle)."""
    return (not sok or sok.lower() in _sokbar(p)) and (not statuser or _statusnokkel(p) in statuser)


def _paameldinger_som_treffer(kurs_id: int, sok: str, statuser: list[str]) -> list[int]:
    rader = con().execute("""SELECT p.id, p.status, p.avslatt_ts, p.utgatt_ts, p.forlatt_ts, p.ekstradeltaker_ts, d.navn, d.epost
                             FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id WHERE p.kurs_id=?""", (kurs_id,))
    return [r["id"] for r in rader if _treffer(r, sok, statuser)]


# Påmeldinger som ikke er aktive (status avmeldt; Avslått, Utgått og Forlatt er også avmeldt, se schema.sql) og kurs som er over
# eller avlyst trenger ikke oppfølging av adressen. En ny status (f.eks. en annen type deltaker) er aktiv til noe annet er bestemt.
_KURSSTATUS_UTEN_ADRESSEOPPFOELGING = ("avsluttet", "avlyst")


def _adressemerke_i_listen(r, kurs) -> str | None:
    """Merket i deltakerlisten: «Privat adresse mangler» (ingen del er lagt inn) eller «Privat adresse er ufullstendig» (bare noen), ellers
    None. `r` er påmeldingsraden med adresse_fylt (0-3, antall utfylte felt fra SQL, uten selve adressen)."""
    if not (r["adresse_fylt"] < 3 and r["status"] != "avmeldt" and kurs["status"] not in _KURSSTATUS_UTEN_ADRESSEOPPFOELGING
            and not _er_anonymisert(r)):
        return None
    return skjemafelt.ADRESSE_UFULLSTENDIG_MERKE if r["adresse_fylt"] else skjemafelt.ADRESSE_MANGLER_MERKE


@app.get("/admin/kurs/<int:kurs_id>/deltakere")
@krever_admin
def admin_kurs_deltakere(kurs_id):
    kurs = _hent_kurs(kurs_id)
    dager = db.kursdager(con(), kurs_id)
    sok, statuser = _deltaker_sok_status(request.args)

    # Alle påmeldingene hentes: søket og statusvalgene filtrerer i nettleseren mens man skriver (static/app.js).
    # Serveren bestemmer bare startvisningen (hidden på radene som ikke passer), så adressen, siden etter omlasting og
    # siden uten JavaScript viser det samme.
    # adresse_fylt: bare antall utfylte felt fra databasen (aldri selve adressen inn i listen). Merket «Privat adresse mangler/er ufullstendig» vises bare der det er
    # noe å følge opp (_adressemerke_i_listen): aktive påmeldinger på kurs som ikke er avsluttet eller avlyst, aldri anonymiserte.
    sql = """SELECT p.*, d.navn, d.epost, d.telefon, d.arbeidssted,
                    (CASE WHEN COALESCE(TRIM(d.adresse), '') != '' THEN 1 ELSE 0 END
                     + CASE WHEN COALESCE(TRIM(d.postnr), '') != '' THEN 1 ELSE 0 END
                     + CASE WHEN COALESCE(TRIM(d.poststed), '') != '' THEN 1 ELSE 0 END) AS adresse_fylt,
                    (SELECT COUNT(*) FROM faktura WHERE paamelding_id=p.id) AS faktura_antall,
                    (SELECT COUNT(*) FROM faktura WHERE paamelding_id=p.id AND status!='betalt') AS faktura_ubetalt
             FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id
             WHERE p.kurs_id=?
             ORDER BY CASE p.status WHEN 'bekreftet' THEN 0 WHEN 'venteliste' THEN 1 ELSE 2 END,
                      CASE WHEN p.ekstradeltaker_ts IS NULL THEN 0 ELSE 1 END, d.navn"""
    # Fakturaen venter på privat adresse (privatadresse.py): fakturastatus «Venter på privat adresse» og en egen melding øverst
    venter_adresse = {r["id"] for r in privatadresse.liste(con(), kurs_id)}
    deltakere = [dict(r, fakturastatus=_fakturastatus(kurs, {**dict(r), "faktura_venter_adresse": r["id"] in venter_adresse}),
                      faktura_venter_adresse=r["id"] in venter_adresse,
                      statusnokkel=_statusnokkel(r), sokbar=_sokbar(r),
                      synlig=_treffer(r, sok, statuser),
                      adresse_merke=_adressemerke_i_listen(r, kurs), adresse_mangler=bool(_adressemerke_i_listen(r, kurs)),
                      firma_mangler=kurs["status"] != "avlyst" and r["status"] in ("bekreftet", "venteliste")
                      and firmaopplysninger.mangler(r)) for r in con().execute(sql, (kurs_id,))]
    # Tallet bak hver status: hvor mange med den statusen som passer med søket (uten søk: alle med statusen)
    antall_per_status = {s: sum(1 for p in deltakere if p["statusnokkel"] == s and _treffer(p, sok, []))
                         for s in DELTAKER_STATUSVALG}

    tellere = {s: sum(1 for p in deltakere if p["statusnokkel"] == s) for s in DELTAKER_STATUSVALG}
    antall_med_plass = sum(tellere[s] for s in db.HAR_PLASS)       # Påmeldt og Ekstradeltaker er begge på kurset (e-post, innsjekk ...)
    samlinger = _samlinger_for_valg(kurs_id)
    # Hvilke samlinger en ekstradeltaker er på: teksten under statusmerket, og dagene i oppmøtematrisen som er utenfor utvalget
    utvalg = db.samlingsutvalg_for_kurs(con(), kurs_id)
    titler = {s["id"]: s["tittel"] for s in samlinger}
    samling_av_dag = {d["id"]: d["samling_id"] for d in dager}
    for p in deltakere:
        valgt = utvalg.get(p["id"]) if p["statusnokkel"] == "ekstradeltaker" else None
        p["utvalg_tekst"] = ((db.samlingstekst([titler[i] for i in valgt if i in titler]) if valgt else "Hele kurset")
                             if p["statusnokkel"] == "ekstradeltaker" else "")
        p["egne_dager"] = {did for did, sid in samling_av_dag.items() if sid in valgt} if valgt else None
    if g.get("admin_rolle") != "lese":       # lesetilgang ser bare statusen, ikke velgeren som endrer den
        _statustekster_per_rad(kurs, deltakere, tellere["paameldt"], flere_samlinger=len(samlinger) > 1)
    oppmote = {(r["paamelding_id"], r["kursdag_id"]): r["kilde"] for r in con().execute(
        "SELECT o.* FROM oppmote o JOIN kursdag kd ON kd.id=o.kursdag_id WHERE kd.kurs_id=?", (kurs_id,))}
    return render_template("admin_kurs_deltakere.html", kurs=kurs, dager=dager, deltakere=deltakere,
                           tellere=tellere, antall_med_plass=antall_med_plass, totalt=sum(tellere.values()), oppmote=oppmote,
                           flere_samlinger=len(samlinger) > 1,
                           neste=_liste_adresse(kurs_id, sok, statuser),
                           sok=sok, statuser=statuser, statusvalg=DELTAKER_STATUSVALG,
                           antall_per_status=antall_per_status, synlige=sum(p["synlig"] for p in deltakere),
                           antall_firma_mangler=sum(p["firma_mangler"] for p in deltakere),
                           antall_adresse_venter=(0 if kurs["status"] in _KURSSTATUS_UTEN_ADRESSEOPPFOELGING
                                                  else sum(p["faktura_venter_adresse"] for p in deltakere)),
                           fane="deltakere")


def _statustekster_per_rad(kurs, deltakere: list[dict], antall_paameldt: int, flere_samlinger: bool = False) -> None:
    """Legger på hver rad teksten i bekreftelsesdialogen for hver mulige ny status (`status_tekster`), og spørsmålet
    om overbooking (`full_tekst`) når kurset er fullt og raden ikke allerede er påmeldt. Ren tekst - se statustekster.
    `antall_paameldt` er antall PÅMELDTE (fullverdige, uten ekstradeltakere): bare Påmeldt tar en plass."""
    # Et avlyst eller avsluttet kurs tar ikke imot flere (db.sett_paamelding_status): da ingen overbooking-spørsmål
    fullt = (kurs["kapasitet"] is not None and antall_paameldt >= kurs["kapasitet"]
             and kurs["status"] not in ("avlyst", "avsluttet"))
    for p in deltakere:
        rest = _rest_faktura(kurs, p, p["statusnokkel"])
        p["status_tekster"] = {
            ny: statustekster.dialogtekst(p["navn"], p["statusnokkel"], ny, kurs=kurs, fakturert=bool(p["faktura_antall"]),
                                          antall_paameldt=antall_paameldt, flere_samlinger=flere_samlinger,
                                          behandlet=bool(p["sveiper_kjort"]), rest_faktura=rest)
            for ny in DELTAKER_STATUSVALG if ny != p["statusnokkel"]}
        # Overbooking gjelder bare når noen får en plass som Påmeldt: en ekstradeltaker tar ingen plass (og har heller ikke
        # plass som Påmeldt: Ekstradeltaker -> Påmeldt krever en ledig plass)
        p["full_tekst"] = (statustekster.fullt_kurs_tekst(p["navn"], antall_paameldt, kurs["kapasitet"])
                           + _retur_tillegg(kurs, p["statusnokkel"], bool(p["faktura_antall"]), bool(p["sveiper_kjort"]), rest)
                           if fullt and p["statusnokkel"] != "paameldt" else None)


def _rest_faktura(kurs, p, statusnokkel: str) -> bool:
    """Har en ekstradeltaker alt fått noen fakturaer, mens en faktura eller delfaktura fortsatt gjenstår? Bare til dialogteksten: settes
    hun tilbake til Påmeldt, lages ikke resten av seg selv (db.sett_paamelding_status). `p` er påmeldingsraden (id, kurs_id, betaling)."""
    if statusnokkel != "ekstradeltaker" or not p["faktura_antall"] or kurs["fakturering"] != "person" or not kurs["pris_nok"]:
        return False
    return sveiper.fakturering_gjenstaar(con(), {**dict(p), "pris_nok": kurs["pris_nok"]})


def _retur_tillegg(kurs, statusnokkel: str, fakturert: bool, behandlet: bool, rest_faktura: bool = False) -> str:
    """Et fullt kurs spør om overbooking FØR statusen endres, og det spørsmålet erstatter dialogteksten. Setter du en ekstradeltaker
    tilbake til Påmeldt, skal spørsmålet likevel si hva som skjer med bekreftelse og faktura (statustekster.retur_tekst)."""
    if statusnokkel != "ekstradeltaker":
        return ""
    return " " + statustekster.retur_tekst(kurs, fakturert=fakturert, behandlet=behandlet, rest_faktura=rest_faktura)


def _samlinger_for_valg(kurs_id: int) -> list[dict]:
    """Kursets samlinger (med kursdager) til samlingsvalget for en ekstradeltaker: id, tittel («Samling 2» eller navnet),
    periode og antall dager. Samme «Samling N» som kurssiden og e-postene."""
    return [{**s, "periode": kursdatoer.periode(date.fromisoformat(s["fra"]), date.fromisoformat(s["til"]))}
            for s in db.kursets_samlinger(con(), kurs_id)]


def _les_samlingsvalg(form) -> tuple[list[int] | None, str | None]:
    """(samlinger, feil) fra samlingsvalget (_samlingsvalg.html): utvalg=noen gir id-ene og krever minst én; «Hele kurset» uten
    avkrysninger gir None = hele kurset. Id-ene kontrolleres mot kurset av db.

    Motstridende valg avvises, aldri tolkes i det stille: samlinger avkrysset mens «Hele kurset» står valgt (uten JavaScript flytter ikke
    valget seg av seg selv når en samling krysses av). Ellers kunne noen som bare skulle på samling 2, fått alle påminnelsene."""
    ider = form.getlist("samling", type=int)
    if form.get("utvalg") != "noen":
        if ider:
            return None, ("Du har krysset av samlinger, men «Hele kurset» er valgt. Velg «Bare utvalgte samlinger», eller fjern "
                          "avkrysningene.")
        return None, None
    if not ider:
        return None, "Velg minst én samling, eller velg «Hele kurset»."
    return ider, None


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
    samlinger_valg = _samlinger_for_valg(kurs_id)

    def vis(f, status: int = 200):
        """Skjemaet (igjen): med det som ble fylt ut, og samlingsvalget for ekstradeltaker."""
        return render_template("admin_deltaker_ny.html", kurs=kurs, dager=dager, f=f, plasser_igjen=plasser_igjen, fane="deltakere",
                               samlinger=samlinger_valg, ekstra_valgt=f.get("ekstradeltaker") == "1",
                               valgte_samlinger=f.getlist("samling", type=int) if hasattr(f, "getlist") else []), status

    if request.method == "GET":
        return vis({})

    f = request.form
    feil = []
    ekstra = f.get("ekstradeltaker") == "1"
    samlinger_ekstra, samlingsfeil = _les_samlingsvalg(f) if ekstra else (None, None)
    if not ekstra and (f.get("utvalg") == "noen" or f.getlist("samling")):
        # Samlingsvalget gjelder bare en ekstradeltaker. Uten avkrysningen ville personen blitt registrert som en vanlig deltaker
        # (tar en plass, eller havner på venteliste på et fullt kurs) uten at samlingsvalget ble brukt: derfor avvises det.
        samlingsfeil = ("Du har valgt samlinger, men ikke krysset av for «Registrer som ekstradeltaker». Samlingsvalget gjelder bare "
                        f"{db.PAAMELDINGSSTATUSER_FLERTALL['ekstradeltaker'].lower()}: kryss av for ekstradeltaker, eller fjern avkrysningene.")
    if samlingsfeil:
        feil.append(samlingsfeil)
    if not (f.get("fornavn", "").strip() and f.get("etternavn", "").strip()) or "@" not in f.get("epost", ""):
        feil.append("Fyll inn fornavn, etternavn og en gyldig e-postadresse.")
    feil += skjemafelt.valider_adresse(f).values()     # deltakerens private adresse er alltid krav
    org = f.get("betaler") == "organisasjon"
    if org and not f.get("org_nr", "").strip():
        feil.append("Fyll inn organisasjonsnummer når arbeidsgiver betaler.")
    elif org and not brreg.gyldig_orgnr(brreg.normaliser_orgnr(f["org_nr"])):
        feil.append(firmaopplysninger.TEKST_UGYLDIG + ".")
    oppslag = None
    if org and not feil:
        # Den felles regelen (firmaopplysninger.py): firmanavn og adresse hentes fra registeret, ikke skrives inn.
        oppslag = firmaopplysninger.slaa_opp(f["org_nr"])
        if tekst := firmaopplysninger.avvisning(oppslag):
            feil.append(tekst)
    if feil:
        for x in feil:
            flash(x, "feil")
        return vis(f, 400)

    try:
        with db.transaksjon(con()):
            pid, status = db.meld_paa(
                con(), kurs_id, epost=f["epost"], fornavn=f["fornavn"], etternavn=f["etternavn"],
                deltaker={"telefon": f.get("telefon"), "arbeidssted": f.get("arbeidssted"),
                         "hpr_nr": f.get("hpr_nr"), "yrkestittel": f.get("yrkestittel")} | skjemafelt.rens_adresse(f),
                # Betaler deltakeren selv, kopierer db.meld_paa personens adresse til fakturaadressen (firma: registeret)
                paamelding={k: f.get(k) or None for k in ("faktura_epost", "faktura_ref", "faktura_kommentar")}
                | (firmaopplysninger.paamelding_felter(oppslag) if org else {})
                | {"betaler": "organisasjon" if org else "person", "ehf": 1 if f.get("ehf") else 0,
                   "betaling": f.get("betaling", "samlet"), "kilde": "admin", "sveiper_utsatt": 1},
                sensitivt={"allergier": f.get("allergier"), "tilrettelegging": f.get("tilrettelegging")},
                idag=_idag(), aktor=_aktor(), tillat_utkast=True, ignorer_frist=True,
                # Ekstradeltaker: på kurset, men tar ikke plass (ingen kapasitetssjekk, aldri venteliste) og holdes tilbake
                ekstradeltaker=ekstra, samlinger=samlinger_ekstra,
            )
            if oppslag is not None and oppslag.utfall == firmaopplysninger.UTILGJENGELIG:
                firmaopplysninger.logg_ikke_hentet(con(), kurs_id, pid, _aktor())
    except db.Paameldingsfeil as e:
        flash(str(e), "feil")
        return vis(f, 400)

    ikke_hentet = oppslag is not None and oppslag.utfall == firmaopplysninger.UTILGJENGELIG
    navn = db.statusnavn("ekstradeltaker" if ekstra else status).lower()      # databaseverdien «bekreftet» vises som «påmeldt»
    if ekstra:
        deltar = db.i_setning(db.samlingsutvalg_tekst(con(), pid, og=True)) if samlinger_ekstra else "hele kurset"
        tekst = (f"{db.fullt_navn(f['fornavn'], f['etternavn'])} er registrert som «{navn}» og deltar på {deltar}. "
                 "Tar ikke plass og teller ikke som påmeldt. Ingen bekreftelse eller faktura er sendt. Bekreftelsen kan du sende "
                 "fra deltakervinduet («Send bekreftelse nå»); fakturaen lager du selv, utenfor systemet.")
    else:
        tekst = (f"{db.fullt_navn(f['fornavn'], f['etternavn'])} er registrert og satt til «{navn}». "
                 "Ingen bekreftelse eller faktura er sendt ennå.")
    flash(tekst + (" " + firmaopplysninger.TEKST_IKKE_HENTET if ikke_hentet else ""), "info" if ikke_hentet else "ok")
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
    # Den felles regelen (firmaopplysninger.py): firmanavn og adresse hentes fra registeret rett etter importen. Svarer
    # ikke registeret, står firmapåmeldingene merket «Firmaopplysninger må kontrolleres», og fakturaen venter.
    firmaopplysninger.prov_alle(con(), _aktor(), kurs_id, forste_forsok=True)
    con().commit()
    venter = len(firmaopplysninger.liste(con(), kurs_id))
    t = import_deltakere.oppsummer(resultat)
    melding = (f"Import fullført: {_tall(t['antall_importert'], 'deltaker registrert', 'deltakere registrert')} "
              f"({t['antall_bekreftet']} {db.PAAMELDINGSSTATUSER['paameldt'].lower()}, {t['antall_venteliste']} venteliste)")
    if t["antall_reaktivert"]:
        melding += f", {t['antall_reaktivert']} reaktivert"
    if t["antall_hoppet_over"]:
        melding += f", {t['antall_hoppet_over']} allerede påmeldt hoppet over"
    melding += "."
    if venter:
        melding += (f" {_tall(venter, 'påmelding venter', 'påmeldinger venter')} på firmaopplysninger fra Enhetsregisteret "
                    f"(merket «{firmaopplysninger.MERKE}»). Fakturaen venter til de er hentet.")
    flash(melding, "ok")
    return redirect(url_for("admin_kurs_deltakere", kurs_id=kurs_id))


@app.get("/admin/deltaker-import-mal.csv")
@krever_admin
def admin_deltaker_import_mal():
    return Response("﻿" + import_deltakere.malfil_csv(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=deltaker-import-mal.csv"})


def _hent_paamelding(kurs_id: int, paamelding_id: int):
    return con().execute(
        """SELECT p.*, d.navn, d.fornavn, d.etternavn, d.epost, d.telefon, d.yrkestittel, d.arbeidssted,
                  d.adresse, d.postnr, d.poststed, s.allergier, s.tilrettelegging,
                  (SELECT COUNT(*) FROM faktura WHERE paamelding_id=p.id) AS faktura_antall,
                  (SELECT COUNT(*) FROM faktura WHERE paamelding_id=p.id AND status!='betalt') AS faktura_ubetalt
           FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id
           LEFT JOIN sensitivt s ON s.paamelding_id=p.id
           WHERE p.id=? AND p.kurs_id=?""", (paamelding_id, kurs_id)).fetchone() or abort(404)


def _skjemasvar(kurs_id: int, paamelding_id: int):
    """[(feltnavn, svar)] for kursets egne felt i paameldingsskjemaet, i skjemaets rekkefolge: felt med svar, og synlige
    felt uten svar ('' = ikke besvart). [] = kurset har ingen egne felt. None = kunne ikke leses akkurat naa."""
    try:
        egne = db.hent_ekstrafelt(con(), kurs_id).felt
    except skjemafelt.SkjemaLesefeil:
        return None
    svar = db.hent_svar(con(), paamelding_id)
    return [(e.label, ekstrafelt.vis_svar(svar.get(e.id))) for e in _egne_i_rekkefolge(egne) if e.id in svar or e.synlig]


def _prisvalg(kurs, p) -> tuple[bool, str]:
    """Kan administrator velge fakturaplan (samlet eller per samling) for akkurat denne påmeldingen, og hvis ikke: hvorfor? (redigerbar, grunn).
    Reglene ligger i db.fakturaplan_sperre: deltakerne skal helst ikke endre på fakturaen, men administrator kan gjøre unntak (Camilla 02.10.2026)."""
    grunn = db.fakturaplan_sperre(con(), kurs["id"], p)
    return grunn is None, grunn or ""


_PLAN_SOM_TEKST = {"samlet": "én samlet faktura", "per_samling": "én faktura per samling", "deltaker_velger": "at deltakeren velger i skjemaet"}


def _statusvalg(p) -> list[str]:
    """Statusene admin kan sette manuelt i deltakervinduet: alle unntatt den påmeldingen har (db.sett_paamelding_status
    håndterer plassen, ventelisten og overbooking)."""
    naa = db.paameldingsstatus(p)
    return [s for s in db.PAAMELDINGSSTATUSER if s != naa]


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
    prisvalg_redigerbar, prisvalg_grunn = _prisvalg(kurs, p)
    bekreftet_antall = db.antall_bekreftet(con(), kurs_id)       # påmeldte (fullverdige): ekstradeltakere teller ikke mot kapasiteten
    samlinger = _samlinger_for_valg(kurs_id)
    statusvalg = _statusvalg(p)
    # Dialogtekstene per ny status, som i deltakerlisten: «Endre status til påmeldt?» alene sier ikke hva som skjer (opprykk,
    # bekreftelse, faktura, plass). static/app.js bruker dem når et valg ikke har egen data-bekreft (valgene i vinduet har ingen).
    rest_faktura = _rest_faktura(kurs, p, db.paameldingsstatus(p))
    status_tekster = {ny: statustekster.dialogtekst(p["navn"], db.paameldingsstatus(p), ny, kurs=kurs, fakturert=bool(p["faktura_antall"]),
                                                    antall_paameldt=bekreftet_antall, flere_samlinger=len(samlinger) > 1,
                                                    behandlet=bool(p["sveiper_kjort"]), rest_faktura=rest_faktura)
                      for ny in statusvalg}
    faktura_venter_adresse = privatadresse.venter(con(), paamelding_id)       # fakturaen holdes tilbake: privat adresse mangler
    return render_template(
        "admin_deltaker.html", kurs=kurs, p=p, fane="deltaker", faktura_venter_adresse=faktura_venter_adresse,
        fakturastatus=_fakturastatus(kurs, {**dict(p), "faktura_venter_adresse": faktura_venter_adresse}),
        skjemasvar=_skjemasvar(kurs_id, paamelding_id),
        andre_paameldinger=andre_paameldinger, prisvalg_redigerbar=prisvalg_redigerbar, prisvalg_grunn=prisvalg_grunn,
        kurs_plan_tekst=_PLAN_SOM_TEKST.get(kurs["betaling"], kurs["betaling"]),
        statusvalg=statusvalg, statusnavn=db.PAAMELDINGSSTATUSER, statusnokkel=db.paameldingsstatus(p),
        status_tekster=status_tekster,
        retur_tillegg=_retur_tillegg(kurs, db.paameldingsstatus(p), bool(p["faktura_antall"]), bool(p["sveiper_kjort"]), rest_faktura),
        bekreftet_antall=bekreftet_antall,
        kurs_fullt=kurs["kapasitet"] is not None and bekreftet_antall >= kurs["kapasitet"],
        samlinger=samlinger, valgte_samlinger=db.ekstradeltaker_samlinger(con(), paamelding_id),
        utvalg_versjon=_utvalg_versjon(p, db.ekstradeltaker_samlinger(con(), paamelding_id)),
        bekreftelse_sendt=db.allerede_sendt(con(), f"kurs:{kurs_id}", p["epost"], "bekreftelse"),
        utvalg_tekst=db.samlingsutvalg_tekst(con(), p), **_min_side_data(kurs_id, paamelding_id, p))


def _min_side_data(kurs_id: int, paamelding_id: int, p) -> dict:
    """Min side-boksen i deltakervinduet: deltakerens personlige lenke (ikke for lesetilgang: lenken er en tilgang til deltakerens side), når
    den virker til, og om kurset har en åpen Min side. Lenken regnes ut; ingenting lagres."""
    if p["status"] != "bekreftet" or g.get("admin_rolle") == "lese":
        return {"min_side_lenke": None, "min_side_stengt": False, "min_side_utloper": None, "min_side_utlopt": False, "min_side_utloper_hvorfor": "",
                "min_side_apen": False, "min_side_vis": False}
    utloper = minside.utloper(con(), paamelding_id)
    return {"min_side_vis": True, "min_side_lenke": minside.url(con(), paamelding_id), "min_side_stengt": minside.er_stengt(con(), paamelding_id),
            "min_side_utloper": f"{utloper.day:02d}.{utloper.month:02d}.{utloper.year}" if utloper else None,
            "min_side_utlopt": bool(utloper and _idag() > utloper), "min_side_utloper_hvorfor": minside.utloper_forklaring(con(), paamelding_id),
            "min_side_apen": deltakerside.har_side(con(), kurs_id, _idag())}


@app.post("/admin/kurs/<int:kurs_id>/deltaker/<int:paamelding_id>/min-side-lenke")
@krever_admin
def admin_deltaker_min_side_lenke(kurs_id, paamelding_id):
    """«Lag ny lenke» (eldre lenker, også i e-poster som er sendt, slutter å virke) og «Steng lenken» (ingen lenke virker før en ny lages)
    for deltakerens personlige lenke til Min side. Bare for dem som kan endre (lesetilgang kan ikke sende skjema)."""
    _hent_kurs(kurs_id)
    p = _hent_paamelding(kurs_id, paamelding_id)             # 404 når påmeldingen ikke hører til kurset
    handling = request.form.get("handling")
    if handling == "ny":
        minside.ny_lenke(con(), paamelding_id, _aktor())
        flash(f"Ny lenke til Min side er laget til {p['navn']}. Den gamle lenken virker ikke lenger.", "ok")
    elif handling == "steng":
        minside.steng(con(), paamelding_id, _aktor())
        flash(f"Lenken til Min side er stengt for {p['navn']}. Ingen lenke virker før du lager en ny.", "ok")
    else:
        abort(400)
    con().commit()
    return redirect(url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id))


@app.post("/admin/kurs/<int:kurs_id>/deltaker/<int:paamelding_id>/person")
@krever_admin
def admin_deltaker_person(kurs_id, paamelding_id):
    _hent_kurs(kurs_id)
    p = _hent_paamelding(kurs_id, paamelding_id)
    if _endret_av_andre("person", p):
        flash(_ENDRET_AV_ANDRE, "feil")
        return redirect(url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id))
    f = request.form
    if not (f.get("fornavn", "").strip() and f.get("etternavn", "").strip()) or "@" not in f.get("epost", ""):
        flash("Fyll inn fornavn, etternavn og en gyldig e-postadresse.", "feil")
        return redirect(url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id))
    ventet = [r["id"] for r in privatadresse.liste(con(), deltaker_id=p["deltaker_id"])]      # fakturaer som venter på adressen
    try:
        db.oppdater_deltaker(con(), p["deltaker_id"], {
            "fornavn": f["fornavn"].strip(), "etternavn": f["etternavn"].strip(), "epost": f["epost"].strip(),
            "telefon": f.get("telefon", "").strip() or None,
            "yrkestittel": f.get("yrkestittel", "").strip() or None,
            "arbeidssted": f.get("arbeidssted", "").strip() or None,
            # Adressen: er alle tre feltene tomme og personen har ingen adresse fra før, lagres de andre feltene som vanlig. Er
            # noen fylt ut, kreves alle tre, og en adresse som står der kan aldri tømmes (db.oppdater_deltaker sier fra med
            # f.eks. «Fyll inn «Adresse».»). Versjonsvakten (_endret_av_andre) er den samme som før.
            **{k: f.get(k, "") for k in skjemafelt.ADRESSEFELT},
        }, aktor=_aktor())
        con().commit()
    except db.DeltakerFeil as e:
        con().rollback()
        flash(str(e), "feil")
        return redirect(url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id))
    flash("Personopplysninger oppdatert. Gjelder alle kurs vedkommende er meldt på.", "ok")
    if videre := _faktura_videre_etter_adresse(p["deltaker_id"], ventet):
        flash(videre, "ok")
    return redirect(url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id))


def _faktura_videre_etter_adresse(deltaker_id: int, ventet: list[int]) -> str | None:
    """Privat adresse er lagt inn i deltakervinduet: påmeldingene der fakturaen ventet på adressen (`ventet`, lest FØR rettelsen)
    går videre etter vanlig fakturaplan med en gang, som rett etter en påmelding (bekreftelsen sendes aldri to ganger). Holdte
    påmeldinger (sveiper_utsatt=1) røres ikke: de behandles når administrator ber om det. Returnerer en melding til administrator,
    eller None når ingenting ventet eller adressen fortsatt mangler (privatadresse.py)."""
    if not ventet:
        return None
    fortsatt = {r["id"] for r in privatadresse.liste(con(), deltaker_id=deltaker_id)}
    klare = [pid for pid in ventet if pid not in fortsatt]
    if not klare:
        return None
    k = Kjoring(con(), idag=_idag())
    for pid in klare:
        sveiper.kjor(k, pid)
        sveiper.utsatte_fakturaer(k, pid)
        con().commit()
    opprettet = con().execute(
        f"SELECT COUNT(DISTINCT paamelding_id) FROM faktura WHERE paamelding_id IN ({','.join('?' * len(klare))})", klare).fetchone()[0]
    if opprettet:
        return ("Fakturaen som ventet på adressen er opprettet." if opprettet == 1
                else f"{opprettet} fakturaer som ventet på adressen er opprettet.")
    holdt = con().execute(
        f"SELECT COUNT(*) FROM paamelding WHERE sveiper_utsatt=1 AND id IN ({','.join('?' * len(klare))})", klare).fetchone()[0]
    if holdt:      # holdt tilbake av administrator (for eksempel en ekstradeltaker satt tilbake til Påmeldt): aldri automatisk faktura
        return ("Adressen er på plass, men påmeldingen er holdt tilbake (for eksempel en ekstradeltaker som er satt tilbake til Påmeldt): "
                "fakturaen lages først når du selv velger «Behandle fakturering nå» på deltakersiden.")
    return ("Adressen er på plass, så fakturaen holdes ikke lenger tilbake. Den lages etter kursets vanlige regler (for eksempel "
            "tidligst seks måneder før første kursdag).")


@app.post("/admin/kurs/<int:kurs_id>/deltaker/<int:paamelding_id>/paamelding")
@krever_admin
def admin_deltaker_paamelding(kurs_id, paamelding_id):
    kurs = _hent_kurs(kurs_id)
    p = _hent_paamelding(kurs_id, paamelding_id)
    if _endret_av_andre("paamelding", p):
        flash(_ENDRET_AV_ANDRE, "feil")
        return redirect(url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id))
    f = request.form
    betaler = f.get("betaler") if f.get("betaler") in ("person", "organisasjon") else p["betaler"]
    org_nr = f.get("org_nr", "").strip()
    if betaler == "organisasjon" and not org_nr:
        flash("Fyll inn organisasjonsnummer når firma betaler.", "feil")
        return redirect(url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id))

    felter = {
        "betaler": betaler,
        "faktura_ref": f.get("faktura_ref", "").strip() or None,
        "faktura_kommentar": f.get("faktura_kommentar", "").strip() or None,
        "intern_kommentar": f.get("intern_kommentar", "").strip() or None,
    }
    ikke_hentet = False
    if betaler == "organisasjon":
        # Den felles regelen (firmaopplysninger.py): firmanavn og adresse skrives aldri inn, heller ikke av administrator.
        # Er det samme firma som før, står opplysningene som de er. Er nummeret endret (eller det byttes til firma), hentes
        # de på nytt fra registeret: ukjent nummer avvises, og svarer ikke registeret, tømmes de og påmeldingen merkes.
        samme_firma = p["betaler"] == "organisasjon" and (
            brreg.normaliser_orgnr(p["org_nr"]) == brreg.normaliser_orgnr(org_nr) != "" or (p["org_nr"] or "") == org_nr)
        if samme_firma:
            felter |= {k: p[k] for k in ("org_nr", "org_navn", "faktura_adresse", "faktura_postnr", "faktura_sted")}
        else:
            oppslag = firmaopplysninger.slaa_opp(org_nr)
            if tekst := firmaopplysninger.avvisning(oppslag):
                flash(tekst, "feil")
                return redirect(url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id))
            felter |= firmaopplysninger.paamelding_felter(oppslag)
            ikke_hentet = oppslag.utfall == firmaopplysninger.UTILGJENGELIG
    else:      # betaler selv: deltakerens egen fakturaadresse kan skrives
        adresse = {k: f.get(k, "").strip() or None for k in ("faktura_adresse", "faktura_postnr", "faktura_sted")}
        if not any(adresse.values()):
            # Ingenting skrevet. Typisk byttet fra firma: skjemaet har da ingen felt for den private adressen. Fakturaen
            # gaar da til deltakerens egen adresse (Personopplysninger), aldri til en tom eller gammel firmaadresse.
            adresse = {"faktura_adresse": p["adresse"], "faktura_postnr": p["postnr"], "faktura_sted": p["poststed"]}
        felter |= adresse
    # Fakturaplanen kan bare endres når ingenting er fakturert eller forsøkt fakturert ennå, og kurset har flere samlinger, selv om noen skulle
    # poste feltet uansett (samme sjekk som avgjør om det vises redigerbart, _prisvalg). Kurset bestemmer standarden; dette er unntaket.
    valg = f.get("betaling")
    ny_plan = valg if valg in ("samlet", "per_samling") and valg != p["betaling"] and _prisvalg(kurs, p)[0] else None

    db.oppdater_paamelding(con(), paamelding_id, felter, aktor=_aktor())
    if ny_plan:
        try:
            db.bytt_fakturaplan(con(), paamelding_id, ny_plan, aktor=_aktor())
        except db.Paameldingsfeil as e:       # en faktura rakk å bli laget mellom visningen og lagringen: ingenting lagres
            con().rollback()
            flash(str(e), "feil")
            return redirect(url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id))
    if ikke_hentet:
        firmaopplysninger.logg_ikke_hentet(con(), kurs_id, paamelding_id, _aktor())
    if kurs["type"] != "digital":
        db.oppdater_sensitivt(con(), paamelding_id, f.get("allergier"), f.get("tilrettelegging"), aktor=_aktor())
    con().commit()
    if ny_plan and p["status"] == "bekreftet":
        _fakturaplan_videre(paamelding_id)
    if ikke_hentet:
        flash("Påmeldingen er oppdatert. " + firmaopplysninger.TEKST_IKKE_HENTET, "info")
    else:
        flash("Påmeldingen er oppdatert.", "ok")
    if ny_plan:
        regel = ("fakturaen for hver samling lages {dager} dager før samlingen".format(dager=kurs["faktura_dager_for"]) if ny_plan == "per_samling"
                 else "fakturaen lages etter kursets vanlige regler (tidligst seks måneder før første kursdag, ellers med en gang)")
        flash(f"Fakturaplanen for {p['navn']} er endret til {_PLAN_SOM_TEKST[ny_plan]}: {regel}. Deltakeren får ikke ny bekreftelse "
              "automatisk – send en e-post under Kommunikasjon hvis du vil gi beskjed.", "info")
    return redirect(url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id))


def _fakturaplan_videre(paamelding_id: int) -> None:
    """Firmaopplysningene er hentet: la påmeldingen gå videre etter vanlig fakturaplan med en gang, som rett etter en
    påmelding (bekreftelsen er allerede sendt og sendes aldri to ganger). Holdte påmeldinger (sveiper_utsatt=1) røres
    ikke: de behandles når administrator ber om det."""
    sveiper.kjor(Kjoring(con(), idag=_idag()), paamelding_id)
    con().commit()


_FIRMA_MELDING = {
    firmaopplysninger.HENTET: ("Firmaopplysningene er hentet fra Enhetsregisteret. Firmanavn og adresse er lagt inn.", "ok"),
    firmaopplysninger.UTILGJENGELIG: ("Enhetsregisteret svarer ikke akkurat nå. Prøv igjen om litt – systemet prøver "
                                      "også hver morgen.", "feil"),
    firmaopplysninger.IKKE_FUNNET: ("Fant ikke organisasjonsnummeret i Enhetsregisteret. Kontroller nummeret med "
                                    "deltakeren.", "feil"),
    firmaopplysninger.UGYLDIG_NUMMER: ("Organisasjonsnummeret mangler eller er ugyldig. Rett det under «Påmelding og "
                                       "faktura».", "feil"),
    firmaopplysninger.IKKE_AKTUELL: ("Firmaopplysningene er allerede på plass.", "info"),
}


@app.post("/admin/kurs/<int:kurs_id>/deltaker/<int:paamelding_id>/firmaopplysninger")
@krever_admin
def admin_deltaker_firmaopplysninger(kurs_id, paamelding_id):
    """Nytt oppslag i Enhetsregisteret for en påmelding merket «Firmaopplysninger må kontrolleres»
    (se firmaopplysninger.py). Firmanavnet og adressen kommer bare fra registeret."""
    _hent_kurs(kurs_id)
    _hent_paamelding(kurs_id, paamelding_id)              # 404 når påmeldingen ikke hører til kurset
    utfall = firmaopplysninger.prov_igjen(con(), paamelding_id, _aktor())
    con().commit()
    if utfall == firmaopplysninger.HENTET:
        _fakturaplan_videre(paamelding_id)
    tekst, kategori = _FIRMA_MELDING[utfall]
    flash(tekst, kategori)
    return redirect(url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id))


@app.post("/admin/firmaopplysninger")
@krever_admin
def admin_firmaopplysninger():
    """Nytt oppslag for ALLE påmeldinger som mangler firmaopplysninger - for ett kurs (skjemafeltet kurs_id) eller alle."""
    kurs_id = None
    if "kurs_id" in request.form:
        kurs_id = request.form.get("kurs_id", type=int) or abort(400)
        _hent_kurs(kurs_id)
    u = firmaopplysninger.prov_alle(con(), _aktor(), kurs_id, ved_hentet=_fakturaplan_videre)
    con().commit()
    totalt = sum(u.values())
    if not totalt:
        flash("Ingen påmeldinger mangler firmaopplysninger.", "info")
    else:
        deler = [f"Firmaopplysninger hentet for {u[firmaopplysninger.HENTET]} av {_tall(totalt, 'påmelding', 'påmeldinger')}."]
        if u[firmaopplysninger.IKKE_FUNNET]:
            deler.append(f"{_tall(u[firmaopplysninger.IKKE_FUNNET], 'organisasjonsnummer', 'organisasjonsnumre')} ble ikke "
                         "funnet i Enhetsregisteret – kontroller med deltakeren.")
        if u[firmaopplysninger.UGYLDIG_NUMMER]:
            deler.append(f"{_tall(u[firmaopplysninger.UGYLDIG_NUMMER], 'påmelding har', 'påmeldinger har')} et ugyldig "
                         "organisasjonsnummer.")
        if u[firmaopplysninger.UTILGJENGELIG]:
            deler.append("Enhetsregisteret svarer ikke akkurat nå – prøv igjen om litt.")
        flash(" ".join(deler), "ok" if u[firmaopplysninger.HENTET] == totalt else "info")
    return redirect(url_for("admin_kurs_deltakere", kurs_id=kurs_id) if kurs_id else url_for("admin"))


def _ekstra_avvist(kurs, gammel: str) -> str | None:
    """Hvorfor en påmelding ikke kan settes til Ekstradeltaker nå (samme regel som db.sett_paamelding_status), eller None. Et avlyst
    eller avsluttet kurs tar ikke imot flere: bare en som alt er på kurset (Påmeldt) kan få merkelappen. Sjekkes FØR steget for
    samlingsvalg, så admin ikke får et komplett skjema som uansett blir avvist."""
    if kurs["status"] in ("avlyst", "avsluttet") and gammel not in db.HAR_PLASS:
        return f"Kurset er {kurs['status']} – kan ikke melde på flere."
    return None


def _retur_til_paameldt_melding(kurs, etter, kjort_na: bool) -> str:
    """Hva som ble gjort med bekreftelse og faktura da en ekstradeltaker ble satt tilbake til Påmeldt (db.sett_paamelding_status).
    `etter` er påmeldingen slik den står nå, `kjort_na` om den ble kjørt gjennom bekreftelse og faktura akkurat nå."""
    if kurs["status"] in ("avlyst", "avsluttet"):
        return f"Kurset er {kurs['status']}: bare merkelappen er endret. Ingenting er sendt eller fakturert."
    if etter["sveiper_utsatt"] and not etter["sveiper_kjort"] and etter["faktura_antall"]:
        return ("Bekreftelsen er allerede sendt, og fakturaene som er laget, står og krediteres ikke. Resten av fakturaene (delfakturaene) "
                "lages ikke av seg selv: påmeldingen er holdt tilbake. Velg «Behandle fakturering nå» i deltakervinduet hvis systemet skal "
                "lage de som gjenstår (alle forfalte delfakturaer lages da). Har du fakturert resten i Visma, skal du ikke gjøre det.")
    if etter["sveiper_utsatt"] and not etter["sveiper_kjort"]:
        return ("Bekreftelsen er allerede sendt, og systemet har ingen faktura til deltakeren. Ingen faktura lages av seg selv: "
                "påmeldingen er holdt tilbake. Velg «Behandle fakturering nå» i deltakervinduet hvis systemet skal lage fakturaen. "
                "Har du fakturert deltakeren i Visma, skal du ikke gjøre det.")
    if kjort_na:
        if etter["sveiper_kjort"]:
            return "Bekreftelse og faktura er behandlet etter kursets vanlige regler, som for alle andre påmeldte."
        return "Bekreftelse og faktura behandles etter kursets vanlige regler. Det som ikke ble ferdig nå, tas av morgenjobben."
    return "Bekreftelse og faktura var allerede behandlet: ingenting er sendt på nytt, og fakturaen som finnes er ikke endret."


@app.post("/admin/kurs/<int:kurs_id>/deltaker/<int:paamelding_id>/status")
@krever_admin
def admin_deltaker_status(kurs_id, paamelding_id):
    """Endrer påmeldingsstatus (db.sett_paamelding_status). Kalles fra deltakervinduet og fra statusvelgeren i
    deltakerlisten. Fra listen følger `neste` med (listen med søk og statusvalg): da går admin tilbake dit, og meldingene
    starter med navnet på deltakeren. Ellers tilbake til deltakeren."""
    kurs = _hent_kurs(kurs_id)
    p = _hent_paamelding(kurs_id, paamelding_id)
    liste = _tilbake_til_listen(kurs_id, request.form.get("neste"))
    # Fra listen tilbake til RADEN (#rad-<id>, se admin_kurs_deltakere.html og static/app.js): siden lastes på nytt, og
    # ellers hopper den til toppen mens raden har flyttet seg til en annen statusgruppe.
    tilbake = f"{liste}#rad-{paamelding_id}" if liste else url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id)
    forst = f"{p['navn']}: " if liste else ""        # fra listen: hvem gjaldt det?
    ny_status = request.form.get("status", "")
    # Listen sender med statusen raden hadde da den ble lastet (`forventet`), og dialogen admin bekreftet beskrev det som
    # skjer FRA den statusen. Er påmeldingen endret siden (annen fane, annen administrator, morgenjobben), ville en annen
    # ting skjedd enn dialogen lovet (f.eks. opprykk og bekreftelse på e-post): da gjøres ingenting.
    forventet = request.form.get("forventet")
    if forventet and forventet != db.paameldingsstatus(p):
        flash(f"{forst}Statusen er endret siden listen ble lastet (nå «{db.PAAMELDINGSSTATUSER[db.paameldingsstatus(p)].lower()}»). "
              "Ingenting er endret. Kontroller statusen og prøv på nytt.", "feil")
        return redirect(tilbake)
    if ny_status == db.paameldingsstatus(p):         # uendret valg (f.eks. «Endre» uten å velge noe annet)
        flash(f"{forst}Statusen er allerede «{db.PAAMELDINGSSTATUSER[ny_status].lower()}». Ingenting er endret.", "info")
        return redirect(tilbake)
    if ny_status not in _statusvalg(p):
        flash(f"{forst}Ugyldig statusendring.", "feil")
        return redirect(tilbake)
    gammel_status = db.paameldingsstatus(p)
    # Ekstradeltaker: har kurset flere samlinger, velges hvilke i et eget steg FØR noe endres (siden med samlingsvalget). Uten
    # JavaScript kommer alle hit; med JavaScript hopper listen og vinduet over dialogen, siden steget har sin egen forklaring.
    # Fra steget kommer samlingsvalg=1 med utvalget. Kurs med én samling har ikke noe å velge: da er det hele kurset.
    samlinger = None
    if ny_status == "ekstradeltaker":
        if avvist := _ekstra_avvist(kurs, gammel_status):         # før steget: ikke tilby et valg som uansett avvises
            flash(f"{forst}{avvist}", "feil")
            return redirect(tilbake)
        if len(db.kursets_samlinger(con(), kurs_id)) > 1 and request.form.get("samlingsvalg") != "1":
            return redirect(url_for("admin_deltaker_ekstra_valg", kurs_id=kurs_id, paamelding_id=paamelding_id,
                                    neste=request.form.get("neste") if liste else None))       # bare en liste vi selv kjenner igjen
        samlinger, samlingsfeil = _les_samlingsvalg(request.form) if request.form.get("samlingsvalg") == "1" else (None, None)
        if samlingsfeil:
            flash(f"{forst}{samlingsfeil}", "feil")
            return redirect(url_for("admin_deltaker_ekstra_valg", kurs_id=kurs_id, paamelding_id=paamelding_id,
                                    neste=request.form.get("neste") if liste else None))
    # overbooking=1 settes bare når admin har svart ja på «Kurset er fullt …» (static/app.js): i deltakervinduet og listen
    tillat_overbooking = request.form.get("overbooking") == "1"
    try:
        kjor_sveiper_for = db.sett_paamelding_status(con(), paamelding_id, ny_status, aktor=_aktor(),
                                                     tillat_overbooking=tillat_overbooking, samlinger=samlinger)
        con().commit()
    except db.Paameldingsfeil as e:
        con().rollback()
        flash(f"{forst}{e}", "feil")
        return redirect(tilbake)
    # Bare den som ble bekreftet (bekreftelse og faktura) eller rykket opp fra ventelisten behandles nå. Den som settes
    # til avmeldt, avslått, utgått eller forlatt, får ingen e-post (avslag med e-post: «Avslå påmeldingen»).
    if kjor_sveiper_for:
        sveiper.kjor(Kjoring(con(), idag=_idag(), aktor=_aktor()), kjor_sveiper_for)
        con().commit()
    navn = db.PAAMELDINGSSTATUSER[ny_status].lower()
    bekreftet_antall = db.antall_bekreftet(con(), kurs_id)
    fikk_plass = ny_status == "paameldt"        # bare Påmeldt tar en plass (Ekstradeltaker tar ingen)
    if fikk_plass and kurs["kapasitet"] is not None and bekreftet_antall > kurs["kapasitet"]:
        flash(f"{forst}Status endret til «{navn}». Kurset har nå {bekreftet_antall} "
              f"{db.PAAMELDINGSSTATUSER_FLERTALL['paameldt'].lower()} deltakere på "
              f"{kurs['kapasitet']} {'plass' if kurs['kapasitet'] == 1 else 'plasser'}.", "ok")
    elif ny_status == "ekstradeltaker":
        deltar = db.i_setning(db.samlingsutvalg_tekst(con(), paamelding_id, og=True)) if samlinger else "hele kurset"
        flash(f"{forst}Status endret til «{navn}». Deltar på {deltar}. Tar ikke plass og teller ikke som "
              f"{db.PAAMELDINGSSTATUSER['paameldt'].lower()}. Ingen bekreftelse eller faktura sendes automatisk. "
              "Bekreftelsen kan du sende fra deltakervinduet («Send bekreftelse nå»); fakturaen lager du selv, utenfor systemet.", "ok")
    else:
        flash(f"{forst}Status endret til «{navn}».", "ok")
    if gammel_status == "ekstradeltaker" and ny_status == "paameldt":
        flash(f"{forst}{_retur_til_paameldt_melding(kurs, _hent_paamelding(kurs_id, paamelding_id), kjor_sveiper_for == paamelding_id)}",
              "info")
    if kjor_sveiper_for and kjor_sveiper_for != paamelding_id:
        flash("Første på ventelisten har rykket opp og fått plassen.", "info")
    if (ny_status not in db.HAR_PLASS or ny_status == "ekstradeltaker") and p["faktura_antall"]:
        flash("Deltakeren er allerede fakturert. Fakturaen " + ("endres ikke og " if ny_status == "ekstradeltaker" else "")
              + "krediteres ikke automatisk – det må gjøres i Visma.", "info")
    return redirect(tilbake)


@app.get("/admin/kurs/<int:kurs_id>/deltaker/<int:paamelding_id>/ekstradeltaker")
@krever_admin
def admin_deltaker_ekstra_valg(kurs_id, paamelding_id):
    """Steget for samlingsvalg når noen settes til Ekstradeltaker (fra listen eller deltakervinduet): «Hele kurset» (forhåndsvalgt)
    eller avkrysning av samlinger. Ingenting endres her - skjemaet sender statusendringen til admin_deltaker_status med
    samlingsvalg=1. `neste` (listen med søk og statusvalg) bygges på nytt av _tilbake_til_listen: aldri en åpen omdirigering."""
    kurs = _hent_kurs(kurs_id)
    p = _hent_paamelding(kurs_id, paamelding_id)
    neste = request.args.get("neste")
    liste = _tilbake_til_listen(kurs_id, neste)
    tilbake = f"{liste}#rad-{paamelding_id}" if liste else url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id)
    gammel = db.paameldingsstatus(p)
    if gammel == "ekstradeltaker":
        flash("Deltakeren er allerede ekstradeltaker. Endre samlingene i deltakervinduet.", "info")
        return redirect(url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id))
    if avvist := _ekstra_avvist(kurs, gammel):          # ikke tilby et skjema som uansett avvises (avlyst eller avsluttet kurs)
        flash(f"{p['navn']}: {avvist}" if liste else avvist, "feil")
        return redirect(tilbake)
    samlinger = _samlinger_for_valg(kurs_id)
    tekst = statustekster.dialogtekst(p["navn"], gammel, "ekstradeltaker", kurs=kurs, fakturert=bool(p["faktura_antall"]),
                                      antall_paameldt=db.antall_bekreftet(con(), kurs_id), flere_samlinger=len(samlinger) > 1)
    return render_template("admin_deltaker_ekstra_valg.html", kurs=kurs, p=p, samlinger=samlinger, valgte=[], tekst=tekst,
                           gammel=gammel, neste=liste and neste or "", tilbake=tilbake, fane="deltakere")


@app.post("/admin/kurs/<int:kurs_id>/deltaker/<int:paamelding_id>/samlinger")
@krever_admin
def admin_deltaker_samlinger(kurs_id, paamelding_id):
    """Lagrer hvilke samlinger en ekstradeltaker deltar på (deltakervinduet). Bare en ekstradeltaker kan ha utvalg; id-ene
    kontrolleres mot kurset. Endrer verken status, plass, bekreftelse eller faktura."""
    _hent_kurs(kurs_id)
    p = _hent_paamelding(kurs_id, paamelding_id)
    tilbake = redirect(url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id))
    # Samme optimistiske kontroll som de andre skjemaene i vinduet (_endret_av_andre): har noen andre lagret et annet utvalg siden
    # siden ble vist, lagres ingenting (ellers overskriver den som lagrer sist, i det stille). Skjema uten feltet kontrolleres ikke.
    mottatt = request.form.get("versjon")
    if mottatt is not None and mottatt != _utvalg_versjon(p, db.ekstradeltaker_samlinger(con(), paamelding_id)):
        flash("Samlingene ble ikke lagret: noen andre har endret dem mens du hadde siden åpen. Siden viser nå de nyeste valgene – "
              "gjør endringen din på nytt.", "feil")
        return tilbake
    samlinger, feil = _les_samlingsvalg(request.form)
    if feil:
        flash(feil, "feil")
        return tilbake
    try:
        valgt = db.sett_ekstradeltaker_samlinger(con(), paamelding_id, samlinger, aktor=_aktor())
        con().commit()
    except db.Paameldingsfeil as e:
        con().rollback()
        flash(str(e), "feil")
        return tilbake
    flash("Samlingene er lagret. Deltar på " + (db.i_setning(db.samlingsutvalg_tekst(con(), paamelding_id, og=True)) if valgt else "hele kurset")
          + ". Påminnelser, innsjekk og kursbevis følger dette.", "ok")
    return tilbake


@app.post("/admin/kurs/<int:kurs_id>/deltaker/<int:paamelding_id>/avsla")
@krever_admin
def admin_deltaker_avsla(kurs_id, paamelding_id):
    """Fase 17: avslå påmeldingen. Plassen frigjøres (første på ventelisten rykker opp og behandles), og deltakeren får
    et avslag på e-post hvis admin ikke har valgt det bort. Begrunnelsen står bare i e-posten - den lagres og logges ikke."""
    kurs = _hent_kurs(kurs_id)
    p = _hent_paamelding(kurs_id, paamelding_id)
    melding = (request.form.get("melding") or "").strip()
    if len(melding) > 2000:
        flash("Begrunnelsen kan være høyst 2000 tegn.", "feil")
        return redirect(url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id))
    try:
        opprykket = db.avsla_paamelding(con(), paamelding_id, aktor=_aktor())
        con().commit()
    except db.Paameldingsfeil as e:
        con().rollback()
        flash(str(e), "feil")
        return redirect(url_for("admin_deltaker", kurs_id=kurs_id, paamelding_id=paamelding_id))
    k = Kjoring(con(), idag=_idag(), aktor=_aktor())
    if request.form.get("send_epost"):
        try:
            k.send_en_gang(f"kurs:{kurs_id}", p["epost"], "avslag", "avslag", paamelding_id=paamelding_id,
                           d={"navn": p["navn"], "fornavn": p["fornavn"]}, kurs=kurs, melding=melding)
            flash("Påmeldingen er avslått, og deltakeren har fått beskjed på e-post.", "ok")
        except Exception:  # noqa: BLE001 - motoren har alt satt 'ukjent' og logget (PII-fritt)
            flash("Påmeldingen er avslått, men e-posten til deltakeren fikk et uavklart utfall. Se «Uavklarte "
                  "operasjoner» på oversikten.", "feil")
    else:
        flash("Påmeldingen er avslått. Deltakeren er ikke varslet.", "ok")
    con().commit()
    if p["faktura_antall"]:
        flash("Deltakeren er allerede fakturert. Fakturaen krediteres ikke automatisk – det må gjøres i Visma.", "info")
    if opprykket:
        sveiper.kjor(k, opprykket)
        con().commit()
        flash("Første på ventelisten har rykket opp og fått plassen.", "info")
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
    _hent_kurs(kurs_id)                           # 404 hvis kurset ikke finnes
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
    elif res.kode == behandling.FULLFORT and p["status"] == "bekreftet" and p["ekstradeltaker_ts"]:
        # Sier aldri «sendt» når ingenting gikk ut: en bekreftelse til samme e-post og kurs fra før dedupliseres (aldri to ganger)
        forste = ("Bekreftelsen er ikke sendt på nytt: en bekreftelse til denne e-postadressen for dette kurset er sendt tidligere "
                  "(for eksempel før personen ble meldt av). Holdet er opphevet. Vil du sende en ny e-post, bruk «Send e-post til "
                  "valgte» i deltakerlisten." if res.epost_var_sendt else
                  "Bekreftelsen er sendt, og den viser bare samlingene deltakeren deltar på.")
        flash(forste + " " + db.PAAMELDINGSSTATUSER_FLERTALL["ekstradeltaker"]
              + " faktureres ikke automatisk: fakturaen lager du selv, utenfor systemet.", "ok")
    elif res.kode == behandling.FULLFORT and res.arsak_kode == behandling.ARSAK_FAKTURA_VENTER:
        flash("Bekreftelsen er sendt (eller var sendt fra før).", "ok")
    elif res.kode == behandling.FULLFORT and p["status"] == "bekreftet":
        flash(("Bekreftelsen var allerede sendt til denne e-postadressen, så den er ikke sendt på nytt. Fakturering er behandlet."
               if res.epost_var_sendt else "Bekreftelse er sendt og fakturering er behandlet."), "ok")
    elif res.kode == behandling.FULLFORT:
        flash("Ventelistebeskjed er sendt.", "ok")
    elif res.kode == behandling.UAVKLART:
        flash("Behandlingen er ikke fullført: en e-post- eller fakturaoperasjon er uavklart eller pågår i en "
             "annen prosess. Systemet sender/fakturerer IKKE automatisk på nytt når utfallet er uklart – "
             "dette må kontrolleres manuelt (se «Uavklarte operasjoner» på forsiden).", "feil")
    elif p["ekstradeltaker_ts"]:
        # Daglig jobb tar aldri en ekstradeltaker: holdet står igjen (behandling.py), og admin prøver på nytt selv
        flash("Behandlingen ble ikke fullført (feil ved sending). Bekreftelsen er fortsatt holdt tilbake: rett feilen og prøv "
              "igjen fra denne siden. En " + db.PAAMELDINGSSTATUSER["ekstradeltaker"].lower()
              + " behandles aldri av den daglige jobben.", "feil")
    else:
        flash("Behandlingen ble ikke fullført (feil ved sending eller fakturering). Den fanges "
             "opp automatisk igjen ved neste daglige kjøring.", "feil")
    if res.forsokt and privatadresse.venter(con(), paamelding_id):      # også når bekreftelsen gikk ut: ingen faktura uten privat adresse
        flash(privatadresse.FAKTURA_HOLDT_GENERELT + ". " + privatadresse.TEKST_HOLDT, "feil")
    return tilbake


@app.get("/admin/kurs/<int:kurs_id>/deltaker/<int:paamelding_id>/kommunikasjon")
@krever_admin
def admin_deltaker_kommunikasjon(kurs_id, paamelding_id):
    """Fanen E-poster: e-postene for påmeldingen, nyeste først (eposthistorikk.for_paamelding)."""
    kurs = _hent_kurs(kurs_id)
    p = _hent_paamelding(kurs_id, paamelding_id)
    return render_template("admin_deltaker_kommunikasjon.html", kurs=kurs, p=p,
                           eposter=eposthistorikk.for_paamelding(con(), p), ikke_lagret=eposthistorikk.IKKE_LAGRET,
                           fane="kommunikasjon")


@app.get("/admin/kurs/<int:kurs_id>/deltaker/<int:paamelding_id>/epost/<int:epost_id>")
@krever_admin
def admin_deltaker_epost(kurs_id, paamelding_id, epost_id):
    """Én e-post nøyaktig slik den ble sendt (den lagrede kopien). Innholdet vises i en låst ramme uten skript."""
    kurs = _hent_kurs(kurs_id)
    p = _hent_paamelding(kurs_id, paamelding_id)
    e = eposthistorikk.visning(con(), p, epost_id) or abort(404)
    return render_template("admin_deltaker_epost.html", kurs=kurs, p=p, e=e, kan_aapnes=eposthistorikk.KAN_AAPNES,
                           fane="kommunikasjon")


@app.get("/admin/kurs/<int:kurs_id>/deltaker/<int:paamelding_id>/epost-eldre")
@krever_admin
def admin_deltaker_epost_eldre(kurs_id, paamelding_id):
    """En eldre e-post uten lagret kopi (sendt før kopiene fantes): når den ble sendt, til hvem og hva slags e-post det var. Innholdet finnes ikke
    og gjettes aldri. Alle radene i fanen E-poster kan klikkes (Camilla 02.10.2026)."""
    kurs = _hent_kurs(kurs_id)
    p = _hent_paamelding(kurs_id, paamelding_id)
    e = eposthistorikk.eldre_visning(con(), p, request.args.get("nokkel", ""), request.args.get("type", "")) or abort(404)
    return render_template("admin_deltaker_epost.html", kurs=kurs, p=p, e=e, kan_aapnes=eposthistorikk.KAN_AAPNES,
                           ikke_lagret=eposthistorikk.IKKE_LAGRET, fane="kommunikasjon")


@app.get("/admin/kurs/<int:kurs_id>/deltaker/<int:paamelding_id>/epost/<int:epost_id>/vedlegg/<int:nr>")
@krever_admin
def admin_deltaker_epost_vedlegg(kurs_id, paamelding_id, epost_id, nr):
    """Et vedlegg slik det ble sendt. Bare trygge filtyper åpnes i nettleseren; alt annet lastes ned. Innholdet tolkes
    aldri som en side på vårt domene (nosniff og CSP sandbox)."""
    _hent_kurs(kurs_id)
    p = _hent_paamelding(kurs_id, paamelding_id)
    fil = eposthistorikk.vedlegg(con(), p, epost_id, nr) or abort(404)
    filnavn, mimetype, innhold = fil
    aapne = mimetype in eposthistorikk.KAN_AAPNES and not request.args.get("last_ned")
    r = make_response(innhold)
    r.headers["Content-Type"] = mimetype if aapne else "application/octet-stream"
    r.headers["Content-Disposition"] = f"{'inline' if aapne else 'attachment'}; filename*=UTF-8''{quote(filnavn)}"
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
    return r


@app.get("/admin/kurs/<int:kurs_id>/deltaker/<int:paamelding_id>/logger")
@krever_admin
def admin_deltaker_logger(kurs_id, paamelding_id):
    kurs = _hent_kurs(kurs_id)
    p = _hent_paamelding(kurs_id, paamelding_id)
    # Innsyn (hvem som har åpnet allergier/tilrettelegging) og tekniske data: bare for systemadministrator
    logg = hendelseslogg.for_paamelding(con(), p, systemadmin=g.get("admin_rolle") == "system")
    return render_template("admin_deltaker_logger.html", kurs=kurs, p=p, rader=logg.rader, innsyn=logg.innsyn,
                           fane="logger")


# ------- admin: signaturbiblioteket (E-postmaler → Signaturer, se kurs/signaturer.py) -------

def _signatureditor_kontekst() -> dict:
    """Det signatureditoren (_signatureditor.html) trenger: verktøylinjens valg og bildene i biblioteket."""
    return dict(skrifter=signaturer.SKRIFTER, storrelser=signaturer.STORRELSER, farger=signaturer.FARGER,
                signaturbilder=signaturer.bilder(con()))


def _trygg_signatur(innhold: str | None) -> str:
    """Innhold som vises i en side eller legges tilbake i editoren: alltid renset på nytt - aldri rått innhold."""
    try:
        return signaturer.rens(innhold or "", signaturer.bildekart(con()))
    except signaturer.Signaturfeil:
        return ""


def _signaturliste() -> list[dict]:
    """Biblioteket med innholdet renset for visning (innhold_renset)."""
    return [{**s, "innhold_renset": _trygg_signatur(s["innhold"])} for s in signaturer.alle(con())]


@app.get("/admin/signaturer")
@krever_admin
def admin_signaturer():
    meg = con().execute("SELECT hovedsignatur_id FROM admin_bruker WHERE id=?", (session.get("admin_id"),)).fetchone()
    return render_template("admin_signaturer.html", signaturliste=_signaturliste(),
                           bilder=signaturer.bilder(con()), standard=signaturer.standard(con()),
                           min_hovedsignatur=meg["hovedsignatur_id"] if meg else None,
                           bilde_maks_bredde=signaturer.BILDE_MAKS_BREDDE)


@app.post("/admin/signaturer/hovedsignatur")
@krever_admin
def admin_signatur_hovedsignatur():
    try:
        signaturer.sett_hovedsignatur(con(), session["admin_id"], request.form.get("signatur_id", type=int) or None,
                                      aktor=_aktor())
        con().commit()
        flash("Hovedsignaturen din er lagret. Den fylles inn når du sender e-post.", "ok")
    except signaturer.Signaturfeil as e:
        con().rollback()
        flash(str(e), "feil")
    return redirect(url_for("admin_signaturer"))


def _vis_signatur(signatur, navn: str, innhold: str, status: int = 200):
    return render_template("admin_signatur_rediger.html", signatur=signatur, navn=navn, innhold=innhold,
                           i_bruk=signaturer.i_bruk(con(), signatur["id"]) if signatur else [],
                           **_signatureditor_kontekst()), status


@app.route("/admin/signaturer/ny", methods=["GET", "POST"])
@krever_admin
def admin_signatur_ny():
    if request.method == "GET":
        return _vis_signatur(None, "", "")
    navn, innhold = request.form.get("navn", ""), request.form.get("innhold", "")
    try:
        signaturer.opprett(con(), navn, innhold, aktor=_aktor())
        con().commit()
    except signaturer.Signaturfeil as e:
        con().rollback()
        flash(str(e), "feil")
        return _vis_signatur(None, navn, _trygg_signatur(innhold), 400)
    flash(f"Signaturen «{' '.join(navn.split())}» er lagret.", "ok")
    return redirect(url_for("admin_signaturer"))


@app.route("/admin/signaturer/<int:signatur_id>", methods=["GET", "POST"])
@krever_admin
def admin_signatur_rediger(signatur_id):
    s = signaturer.hent(con(), signatur_id) or abort(404)
    if request.method == "GET":
        return _vis_signatur(s, s["navn"], _trygg_signatur(s["innhold"]))
    navn, innhold = request.form.get("navn", ""), request.form.get("innhold", "")
    try:
        signaturer.oppdater(con(), signatur_id, navn, innhold, request.form.get("versjon", type=int) or 0,
                            aktor=_aktor())
        con().commit()
    except signaturer.Signaturfeil as e:
        con().rollback()
        flash(str(e), "feil")
        return _vis_signatur(s, navn, _trygg_signatur(innhold), 400)
    flash("Signaturen er lagret. E-poster som alt er sendt, endres ikke.", "ok")
    return redirect(url_for("admin_signaturer"))


@app.post("/admin/signaturer/<int:signatur_id>/standard")
@krever_admin
def admin_signatur_standard(signatur_id):
    try:
        signaturer.sett_standard(con(), signatur_id, aktor=_aktor())
        con().commit()
        flash("Standardsignaturen er endret.", "ok")
    except signaturer.Signaturfeil as e:
        con().rollback()
        flash(str(e), "feil")
    return redirect(url_for("admin_signaturer"))


@app.post("/admin/signaturer/<int:signatur_id>/slett")
@krever_admin
def admin_signatur_slett(signatur_id):
    try:
        signaturer.slett(con(), signatur_id, aktor=_aktor())
        con().commit()
        flash("Signaturen er slettet.", "ok")
    except signaturer.Signaturfeil as e:
        con().rollback()
        flash(str(e), "feil")
    return redirect(url_for("admin_signaturer"))


@app.post("/admin/signaturer/bilder")
@krever_admin
def admin_signatur_bilde_last_opp():
    fil = request.files.get("bilde")
    data = fil.read(signaturer.BILDE_MAKS_BYTE + 1) if fil else b""
    try:
        signaturer.lagre_bilde(con(), fil.filename if fil else "", data, request.form.get("alt", ""), aktor=_aktor())
        con().commit()
        flash("Bildet er lastet opp. Du kan nå sette det inn i en signatur.", "ok")
    except signaturer.Signaturfeil as e:
        con().rollback()
        flash(str(e), "feil")
    return redirect(url_for("admin_signaturer") + "#bilder")


@app.get("/admin/signaturer/bilde/<int:bilde_id>")
@krever_admin
def admin_signatur_bilde(bilde_id):
    """Et bilde fra biblioteket (bare PNG/JPEG/GIF, kontrollert ved opplasting) - bare for innloggede administratorer."""
    mimetype, innhold = signaturer.bilde(con(), bilde_id) or abort(404)
    r = make_response(innhold)
    r.headers["Content-Type"] = mimetype
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["Cache-Control"] = "private, max-age=86400"
    r.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'"
    return r


@app.post("/admin/signaturer/bilde/<int:bilde_id>/slett")
@krever_admin
def admin_signatur_bilde_slett(bilde_id):
    try:
        signaturer.slett_bilde(con(), bilde_id, aktor=_aktor())
        con().commit()
        flash("Bildet er slettet.", "ok")
    except signaturer.Signaturfeil as e:
        con().rollback()
        flash(str(e), "feil")
    return redirect(url_for("admin_signaturer") + "#bilder")


@app.route("/admin/kurs/<int:kurs_id>/signatur", methods=["GET", "POST"])
@krever_admin
def admin_kurs_signatur(kurs_id):
    """Signaturen i kursets påminnelser, kursbevis, avlysning og avslag: standard, en fra biblioteket, eller en egen,
    tilpasset versjon for kurset («Tilpass for dette kurset» - biblioteket endres ikke)."""
    kurs = _hent_kurs(kurs_id)
    valg = signaturer.kursvalg(con(), kurs_id)
    if request.method == "POST":
        modus = request.form.get("modus", "")
        try:
            signaturer.sett_for_kurs(con(), kurs_id, modus, signatur_id=request.form.get("signatur_id", type=int),
                                     innhold=request.form.get("innhold"), aktor=_aktor())
            con().commit()
            flash("Kursets signatur er lagret. Den brukes i e-postene som sendes fra nå av.", "ok")
            return redirect(url_for("admin_kurs_kommunikasjon", kurs_id=kurs_id))
        except signaturer.Signaturfeil as e:
            con().rollback()
            flash(str(e), "feil")
            valg = {**valg, "modus": modus, "innhold": _trygg_signatur(request.form.get("innhold")),
                    "signatur_id": request.form.get("signatur_id", type=int)}
    standard = signaturer.standard(con())
    return render_template("admin_kurs_signatur.html", kurs=kurs, valg={**valg, "innhold": _trygg_signatur(valg["innhold"])},
                           signaturliste=_signaturliste(), standard=standard,
                           standard_renset=_trygg_signatur(standard["innhold"]), fane="kommunikasjon",
                           **_signatureditor_kontekst())


def _signatur_i_epost(signatur_html: str | None = None, valgt: int | None = None) -> dict:
    """Signaturfeltet i «Send e-post»: avsenderens hovedsignatur (ellers standard) ferdig utfylt, og biblioteket å
    velge fra. Innholdet i editoren er alltid renset."""
    if signatur_html is None:
        s = signaturer.for_admin(con(), session.get("admin_id"))
        signatur_html, valgt = _trygg_signatur(s["innhold"]), s["id"]
    return dict(signaturliste=_signaturliste(), signatur_html=signatur_html, signatur_valgt=valgt,
                **_signatureditor_kontekst())


# ------- admin: manuell e-post (fase 5) -------

EPOST_EMNE_MAKS = 200
EPOST_TEKST_MAKS = 5000
# Flettefeltene som vises som klikkbare knapper under meldingsfeltet (se _kodeknapper.html og maltekster.MANUELLE_KODER).
_EPOST_KODER = {"koder": maltekster.KODER, "manuelle_koder": maltekster.kodeliste(maltekster.MANUELLE_KODER)}


def _hent_epost_mottakere(kurs_id: int, ider: list[int]) -> list:
    """Validerer mottaker-ID-er MOT DATABASEN - stoler aldri paa at ID-ene fra skjemaet er gyldige/
    hoerer til dette kurset. Ukjente/fremmede ID-er faller bare bort."""
    if not ider:
        return []
    plassholdere = ",".join("?" * len(ider))
    return con().execute(
        f"""SELECT p.id, d.navn, d.fornavn, d.epost FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id
            WHERE p.kurs_id=? AND p.id IN ({plassholdere}) ORDER BY d.navn""",
        (kurs_id, *ider)).fetchall()


@app.route("/admin/kurs/<int:kurs_id>/epost/ny", methods=["GET", "POST"])
@krever_admin
def admin_epost_ny(kurs_id):
    """GET med ?gruppe=... (alle påmeldte/venteliste) eller POST med valgte paamelding_id fra deltakerlisten (POST, slik
    at utvalget og CSRF-tokenet aldri havner i en URL)."""
    kurs = _hent_kurs(kurs_id)
    gruppe = request.args.get("gruppe")
    if gruppe in ("bekreftet", "venteliste"):
        ider = [r["id"] for r in con().execute(
            "SELECT id FROM paamelding WHERE kurs_id=? AND status=?", (kurs_id, gruppe))]
    else:
        ider = request.values.getlist("paamelding_id", type=int)
    mottakere = _hent_epost_mottakere(kurs_id, ider)
    if not mottakere:
        flash("Velg minst én mottaker.", "feil")
        return redirect(url_for("admin_kurs_deltakere", kurs_id=kurs_id))
    return render_template("admin_epost_ny.html", kurs=kurs, mottakere=mottakere, emne="", tekst="",
                           emne_maks=EPOST_EMNE_MAKS, tekst_maks=EPOST_TEKST_MAKS, forhandsvisning=None, **_EPOST_KODER,
                           **_signatur_i_epost())


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
    elif "{" in emne or "}" in emne:    # ellers ville «{fornavn}» blitt sendt ordrett i emnefeltet
        feil.append("Flettefelt som {fornavn} kan bare brukes i selve meldingen, ikke i emnet.")
    if not tekst:
        feil.append("Fyll inn en melding.")
    elif len(tekst) > EPOST_TEKST_MAKS:
        feil.append(f"Meldingen kan være maks {EPOST_TEKST_MAKS} tegn (er nå {len(tekst)}).")
    else:
        try:
            maltekster.valider_manuell_tekst(tekst)     # ukjent kode / loes klamme: stoppes FOER noe lagres eller sendes
        except maltekster.MalFeil as e:
            feil.append(e.forklaring)
    if not mottakere:
        feil.append("Velg minst én gyldig mottaker.")
    # Signaturen i akkurat denne e-posten (renses her og igjen ved sending). Uten JavaScript: den valgte i biblioteket.
    signatur_valgt = request.form.get("signatur_valg", type=int)
    raa_signatur = request.form.get("signatur_html")
    if raa_signatur is None:
        s = (signaturer.hent(con(), signatur_valgt) if signatur_valgt else None) or signaturer.for_admin(
            con(), session.get("admin_id"))
        raa_signatur = s["innhold"]
    try:
        signatur_html = signaturer.klargjor_innhold(con(), raa_signatur)
    except signaturer.Signaturfeil as e:
        feil.append(str(e))
        signatur_html = _trygg_signatur(raa_signatur)
    if feil:
        for x in feil:
            flash(x, "feil")
        return render_template("admin_epost_ny.html", kurs=kurs, mottakere=mottakere, emne=emne, tekst=tekst,
                               emne_maks=EPOST_EMNE_MAKS, tekst_maks=EPOST_TEKST_MAKS, forhandsvisning=None,
                               **_EPOST_KODER, **_signatur_i_epost(signatur_html, signatur_valgt))

    utsending_id, _ = db.opprett_admin_utsending(
        con(), kurs_id, emne, tekst, [m["id"] for m in mottakere], sendt_av_admin_id=session.get("admin_id"),
        signatur_html=signatur_html)
    con().commit()
    eksempel_id = request.form.get("eksempel_paamelding_id", type=int)
    eksempel = next((m for m in mottakere if m["id"] == eksempel_id), mottakere[0])
    _, eksempel_html = epost.render("admin_melding", emne=emne, tekst=tekst,
                                    d={**dict(eksempel), "min_side_url": Kjoring(con(), idag=_idag()).min_side_lenke(eksempel["id"])},
                                    signatur=signaturer.som_markup(signatur_html))
    return render_template("admin_epost_ny.html", kurs=kurs, mottakere=mottakere, emne=emne, tekst=tekst,
                           emne_maks=EPOST_EMNE_MAKS, tekst_maks=EPOST_TEKST_MAKS,
                           forhandsvisning={"utsending_id": utsending_id, "html": eksempel_html,
                                            "eksempel_navn": eksempel["navn"], "eksempel_id": eksempel["id"]},
                           **_EPOST_KODER, **_signatur_i_epost(signatur_html, signatur_valgt))


def _flash_sendefeil(ut) -> None:
    """Ærlig beskjed når ikke alle fikk e-posten (se Kjoring.send_til_mange)."""
    if ut.uavklart:
        flash(f"{ut.uavklart} e-post(er) fikk et uavklart utfall og sendes ikke automatisk på nytt. Se «Uavklarte "
              "operasjoner» på oversikten.", "feil")
    if ut.ikke_forsokt:
        flash(f"{ut.ikke_forsokt} mottaker(e) fikk ikke e-posten fordi e-posttjenesten ikke svarte. Prøv igjen litt "
              "senere – de som alt har fått den, får den ikke to ganger.", "feil")
    if ut.uavklart_fra_for:
        flash(f"{ut.uavklart_fra_for} mottaker(e) har et uavklart eller pågående forsøk fra før og fikk ikke e-posten "
              "på nytt. Se «Uavklarte operasjoner» på oversikten.", "info")


@app.post("/admin/kurs/<int:kurs_id>/epost/send")
@krever_admin
def admin_epost_send(kurs_id):
    _hent_kurs(kurs_id)
    utsending_id = request.form.get("utsending_id", type=int)
    rad = con().execute("SELECT id FROM admin_utsending WHERE id=? AND kurs_id=?", (utsending_id, kurs_id)).fetchone()
    if not rad:
        flash("Fant ikke utsendelsen. Forhåndsvis på nytt.", "feil")
        return redirect(url_for("admin_kurs_deltakere", kurs_id=kurs_id))
    ut = Kjoring(con(), idag=_idag(), aktor=_aktor()).send_admin_utsending(utsending_id)
    for pid in ut.sendt:
        db.logg(con(), "admin_epost_sendt", {"paamelding_id": pid, "kurs_id": kurs_id}, aktor=_aktor())
    con().commit()
    if ut.sendt:
        flash(f"E-post sendt til {len(ut.sendt)} mottaker(e).", "ok")
    elif not (ut.uavklart or ut.ikke_forsokt or ut.uavklart_fra_for):
        flash("Denne utsendelsen er allerede sendt – ingenting ble sendt på nytt.", "info")
    _flash_sendefeil(ut)
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
    signatur = (signaturer.som_markup(_trygg_signatur(utsending["signatur_html"]))
                if utsending["signatur_html"] is not None else None)
    return render_template("admin_epost_detalj.html", kurs=kurs, utsending=utsending, mottakere=mottakere,
                           signatur=signatur)


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
           WHERE au.kurs_id=? AND ul.status='sendt' GROUP BY au.id, ab.navn ORDER BY au.opprettet DESC""",
        (kurs_id,)).fetchall()
    return render_template("admin_kurs_kommunikasjon.html", kurs=kurs, meldinger=meldinger, manuelle=manuelle,
                           kurssignatur=signaturer.kursvalg(con(), kurs_id), fane="kommunikasjon")


@app.post("/admin/kurs/<int:kurs_id>/oppmote")
@krever_admin
def admin_oppmote(kurs_id):
    pid, kdid = request.form.get("paamelding_id", type=int), request.form.get("kursdag_id", type=int)
    hoerer_til = pid and kdid and con().execute(
        "SELECT 1 FROM paamelding p JOIN kursdag kd ON kd.kurs_id=p.kurs_id WHERE p.id=? AND kd.id=? AND p.kurs_id=?",
        (pid, kdid, kurs_id)).fetchone()
    if not hoerer_til:
        abort(404)
    registrert = db.registrer_oppmote(con(), pid, kdid, "manuell")    # False: var registrert fra før -> fjernes
    if not registrert:
        con().execute("DELETE FROM oppmote WHERE paamelding_id=? AND kursdag_id=?", (pid, kdid))
    db.logg(con(), "oppmote_manuell", {"paamelding_id": pid, "kursdag_id": kdid, "registrert": registrert},
            aktor=_aktor())
    con().commit()
    return redirect(url_for("admin_kurs_deltakere", kurs_id=kurs_id) + "#oppmote")


@app.post("/admin/paamelding/<int:pid>/meld-av")
@krever_admin
def admin_meld_av(pid):
    rad = con().execute("SELECT kurs_id FROM paamelding WHERE id=?", (pid,)).fetchone() or abort(404)
    kurs_id = rad["kurs_id"]
    opp = db.meld_av(con(), pid, aktor=_aktor())
    if opp:
        sveiper.kjor(Kjoring(con(), idag=_idag(), aktor=_aktor()), opp)
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
    # Bare de som er satt opp på dagen teller (en ekstradeltaker på andre samlinger kan ha manuelt oppmøte her: det teller ikke),
    # slik Oppsett-fanen teller. Dagnummeret på plakaten er kursets eget.
    n = con().execute(
        f"""SELECT COUNT(*) FROM oppmote o JOIN kursdag kd ON kd.id=o.kursdag_id JOIN paamelding p ON p.id=o.paamelding_id
            WHERE o.kursdag_id=? AND {db.sql_egen_dag('p', 'kd')}""", (dag_id,)).fetchone()[0]
    nr, av = deltakerside.dagnummer(con(), kurs_id, dag_id)
    dato = date.fromisoformat(kd["dato"])
    return render_template("admin_qr.html", kd=kd, svg=qr_svg(lenke), lenke=lenke, antall=n,
                           kode_url=f"{config.BASE_URL}/logg-inn", dag_nr=nr, dag_av=av, dato_lang=deltakerside.dato_lang(dato, aar=True),
                           gjelder_i_dag=dato == _idag(), idag_lang=deltakerside.dato_lang(_idag(), aar=True),
                           krever_innlogging=config.INNSJEKK_KREVER_INNLOGGING)


@app.get("/admin/kurs/<int:kurs_id>/allergiliste")
@krever_admin
def admin_allergiliste(kurs_id):
    kurs = con().execute("SELECT * FROM kurs WHERE id=?", (kurs_id,)).fetchone() or abort(404)
    rader = con().execute(
        """SELECT d.navn, s.allergier, s.tilrettelegging, p.id AS pid FROM sensitivt s JOIN paamelding p ON p.id=s.paamelding_id
           JOIN deltaker d ON d.id=p.deltaker_id WHERE p.kurs_id=? AND p.status='bekreftet' ORDER BY d.navn""",
        (kurs_id,)).fetchall()      # Påmeldt og Ekstradeltaker: begge er på kurset og spiser der
    db.logg(con(), "sensitivt_vist", {"kurs_id": kurs_id}, aktor=_aktor())
    con().commit()
    # «Deltar på» vises bare når noen er på noen av samlingene (ekstradeltakere): kjøkken og lokale trenger å vite når de kommer
    deltar_paa = _samlingsutvalg_tekster(kurs_id)
    return render_template("admin_allergi.html", kurs=kurs, rader=rader, deltar_paa=deltar_paa)


_DELTAKER_CSV_KOLONNER = ["Fornavn", "Etternavn", "E-post", "Telefon", "Arbeidssted", "Status", "Samlinger", "Betaler", "Betaling",
                         "Organisasjon", "Org.nr", "Fakturanr", "Fakturert (kr)"]
def _deltaker_csv_select() -> str:
    """Felles SELECT for deltaker-CSV. Tekstaggregatet kommer fra db.sql_tekstliste (riktig funksjon per databasebackend)."""
    return f"""SELECT d.fornavn, d.etternavn, d.epost, d.telefon, d.arbeidssted,
                      {db.sql_visningsstatus("p")} AS status, p.betaler, p.betaling,
                      p.org_navn, p.org_nr,
                      (SELECT {db.sql_tekstliste(con(), "faktura_nr")} FROM faktura WHERE paamelding_id=p.id) AS faktura_nr,
                      (SELECT COALESCE(SUM(belop_nok),0) FROM faktura WHERE paamelding_id=p.id) AS fakturert_belop,
                      p.id AS pid, p.kurs_id AS kid
               FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id"""


def _csv_respons(overskrifter, rader, filnavn: str) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(overskrifter)
    w.writerows([tuple(sikkerhet.csv_trygg(v) for v in r) for r in rader])
    # Inneholder personopplysninger (navn/e-post/telefon) - skal aldri mellomlagres av nettleser/proxy.
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",  # BOM -> Excel leser æøå riktig
                    headers={"Content-Disposition": f"attachment; filename={filnavn}",
                             "Cache-Control": "no-store"})


def _samlingsutvalg_tekster(kurs_id: int) -> dict[int, str]:
    """{paamelding_id: «Samling 1, 3»} for ekstradeltakere på kurset som bare er på noen samlinger. De andre er på hele kurset."""
    utvalg = db.samlingsutvalg_for_kurs(con(), kurs_id)
    if not utvalg:
        return {}
    titler = {s["id"]: s["tittel"] for s in db.kursets_samlinger(con(), kurs_id)}
    return {pid: db.samlingstekst([titler[i] for i in ider if i in titler]) for pid, ider in utvalg.items()}


def _deltaker_csv_respons(rader, filnavn: str) -> Response:
    """Statuskolonnen får navnet slik det vises i listen («Påmeldt», «Avslått» …), ikke databaseverdien. Kolonnen «Samlinger» sier
    «Hele kurset» eller «Samling 1, 3» for alle som har plass (Påmeldt og Ekstradeltaker), ellers tom."""
    i = _DELTAKER_CSV_KOLONNER.index("Status")
    per_kurs: dict[int, dict[int, str]] = {}
    ut = []
    for r in rader:
        r = list(r)
        kid, pid = r.pop(), r.pop()             # de to siste feltene i SELECT-en er bare til dette
        har_plass = r[i] in db.HAR_PLASS or r[i] == db.DATABASEVERDI_PLASS
        r[i] = db.statusnavn(r[i])
        if kid not in per_kurs:
            per_kurs[kid] = _samlingsutvalg_tekster(kid)
        r.insert(i + 1, per_kurs[kid].get(pid, "Hele kurset") if har_plass else "")
        ut.append(r)
    return _csv_respons(_DELTAKER_CSV_KOLONNER, ut, filnavn)


@app.get("/admin/kurs/<int:kurs_id>/deltakere.csv")
@krever_admin
def admin_csv(kurs_id):
    """Eksporterer deltakere for kurset. Uten sok/status-parametre: hele kurset (uendret
    oppforsel). Med sok og/eller status satt (samme parametre som deltakerlisten sitt eget filter, status
    kan gjentas): eksporterer kun det admin faktisk ser paa skjermen akkurat na - med samme treffregel (_treffer)."""
    sok, statuser = _deltaker_sok_status(request.args)
    sql, args = _deltaker_csv_select() + " WHERE p.kurs_id=?", [kurs_id]
    if sok or statuser:
        ider = _paameldinger_som_treffer(kurs_id, sok, statuser)
        sql += f" AND p.id IN ({','.join('?' * len(ider))})" if ider else " AND 1=0"
        args += ider
    rader = con().execute(sql + " ORDER BY p.status, d.navn", args).fetchall()
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
    sql = _deltaker_csv_select() + f" WHERE p.kurs_id=? AND p.id IN ({plassholdere}) ORDER BY p.status, d.navn"
    rader = con().execute(sql, [kurs_id, *ider]).fetchall()
    return _deltaker_csv_respons(rader, f"deltakere_utvalg_{kurs_id}.csv")


def _deltakerliste(kurs_id: int, *, csv: bool = False):
    """Felles for utskrift og CSV: samme valg (fra adressen) gir samme rader og kolonner. Svarene paa kursets egne felt
    kan velges som egne kolonner (se deltakerliste.katalog)."""
    kurs = _hent_kurs(kurs_id)
    try:
        egne = _egne_i_rekkefolge(db.hent_ekstrafelt(con(), kurs_id).felt)
    except skjemafelt.SkjemaLesefeil:
        egne = []
    katalog = deltakerliste.katalog(egne)
    valg = deltakerliste.les_valg(request.args, katalog)
    dager = db.kursdager(con(), kurs_id)
    rader = deltakerliste.hent(con(), kurs_id, valg.status)
    if any(k.gruppe == deltakerliste.SVAR for k in valg.kolonner):
        svar = db.hent_svar_for_kurs(con(), kurs_id)
        for r in rader:
            r["svar"] = svar.get(r["id"], {})
    oppmott = deltakerliste.oppmote(con(), kurs_id) if "oppmote" in valg.nokler else set()
    return kurs, katalog, valg, dager, deltakerliste.tabell(kurs, rader, valg, dager, oppmott, csv=csv)


@app.get("/admin/kurs/<int:kurs_id>/deltakerliste")
@krever_admin
def admin_deltakerliste(kurs_id):
    """Deltakerliste med kolonnevalg for utskrift / PDF (nettleserens «Lagre som PDF»). Personvernreglene står i
    kurs/deltakerliste.py: bare nr og navn som standard, aldri allergier/tilrettelegging."""
    kurs, katalog, valg, dager, tabell = _deltakerliste(kurs_id)
    logo = deltakerliste.velg_logo(request.args.get("logo"))
    return render_template("admin_deltakerliste.html", kurs=kurs, valg=valg, tabell=tabell,
                           katalog=katalog, grupper=deltakerliste.GRUPPER,
                           statusvalg=deltakerliste.STATUSVALG, logo=logo, logoer=deltakerliste.logoer(),
                           datoer=deltakerliste.datoer_tekst(dager), utskrevet=_idag().strftime("%d.%m.%Y"),
                           # «Påmeldte og ekstradeltakere»: tallene vises hver for seg (12 påmeldte + 2 ekstradeltakere)
                           antall_paameldte=db.antall_bekreftet(con(), kurs_id), antall_ekstra=db.antall_ekstradeltakere(con(), kurs_id))


@app.get("/admin/kurs/<int:kurs_id>/deltakerliste.csv")
@krever_admin
def admin_deltakerliste_csv(kurs_id):
    """Samme liste som CSV (uten tomme utfyllingskolonner). Eksporten logges - med kolonnenavn og antall, aldri
    personopplysninger."""
    kurs, _, valg, _, tabell = _deltakerliste(kurs_id, csv=True)
    db.logg(con(), "deltakerliste_eksportert", {"kurs_id": kurs_id, "status": valg.status, "kolonner": valg.nokler,
                                                "antall": len(tabell.rader)}, aktor=_aktor())
    con().commit()
    return _csv_respons(tabell.overskrifter, tabell.rader, f"deltakerliste_{kurs['kursnr'] or kurs_id}.csv")


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
        f"""SELECT p.id, p.status, p.avslatt_ts, p.utgatt_ts, p.forlatt_ts, p.ekstradeltaker_ts, p.sveiper_kjort,
                   p.sveiper_utsatt, d.navn, d.epost
            FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id
            WHERE p.kurs_id=? AND p.id IN ({plassholdere})""", [kurs_id, *ider]).fetchall()
    # ID-er som ikke tilhorer DETTE kurset forsvinner stille her - ingen feilmelding som
    # bekrefter/avkrefter at de finnes et annet sted (unngaar informasjonslekkasje).

    resultater = []
    for rad in rader:
        behandlingsbar, arsak = _bulk_klassifiser(kurs, rad)
        resultater.append({"paamelding_id": rad["id"], "navn": rad["navn"], "epost": rad["epost"],
                           "status": db.paameldingsstatus(rad), "behandlingsbar": behandlingsbar, "arsak": arsak,
                           "hva_skjer": behandling.forventet_handling(kurs, rad, con()) if behandlingsbar else ""})
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
    sok_sql, sok_parametre = _kurs_sok_vilkar(sok)

    sql = f"""SELECT k.*, ab.navn AS ansvarlig_navn, MIN(kd.dato) AS start, MAX(kd.dato) AS slutt,
                    (SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=k.id AND p.status='bekreftet'
                        AND p.ekstradeltaker_ts IS NULL) AS bekreftet,
                    (SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=k.id AND p.status='bekreftet'
                        AND p.ekstradeltaker_ts IS NOT NULL) AS ekstradeltaker,
                    (SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=k.id AND p.status='venteliste') AS venteliste,
                    (SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=k.id AND p.status='avmeldt'
                        AND p.avslatt_ts IS NULL AND p.utgatt_ts IS NULL AND p.forlatt_ts IS NULL) AS avmeldt,
                    (SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=k.id AND p.avslatt_ts IS NOT NULL) AS avslatt,
                    (SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=k.id AND p.utgatt_ts IS NOT NULL) AS utgatt,
                    (SELECT COUNT(*) FROM paamelding p WHERE p.kurs_id=k.id AND p.forlatt_ts IS NOT NULL) AS forlatt,
                    (SELECT COALESCE(SUM(f.belop_nok),0) FROM faktura f JOIN paamelding p ON p.id=f.paamelding_id
                        WHERE p.kurs_id=k.id) AS fakturert
             FROM kurs k
             LEFT JOIN kursdag kd ON kd.kurs_id=k.id
             LEFT JOIN admin_bruker ab ON ab.id=k.ansvarlig_admin_id
             WHERE {sok_sql} AND (? = 0 OR k.ansvarlig_admin_id = ?)
                   AND (? = '' OR k.status = ?)
             GROUP BY k.id, ab.navn
             HAVING (? = '' OR MAX(kd.dato) >= ?) AND (? = '' OR MIN(kd.dato) <= ?)
             ORDER BY start"""
    args = (*sok_parametre, ansvarlig, ansvarlig, status, status, fra, fra, til, til)
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
    s = db.PAAMELDINGSSTATUSER
    # Påmeldt er de fullverdige deltakerne (mot kapasiteten); Ekstradeltaker er en egen kolonne ved siden av og teller ikke med
    w.writerow(["Kursnr", "Tittel", "Start", "Slutt", "Status", "Ansvarlig", s["paameldt"], "Kapasitet",
                "Fyllingsgrad (%)", s["ekstradeltaker"], s["venteliste"],
                s["avmeldt"], s["avslatt"], s["utgatt"], s["forlatt"], "Fakturert (kr)"])
    for k in rader:
        fyllingsgrad = round(k["bekreftet"] / k["kapasitet"] * 100) if k["kapasitet"] else ""
        w.writerow([sikkerhet.csv_trygg(v) for v in (
            k["kursnr"], k["navn"], k["start"] or "", k["slutt"] or "", k["status"], k["ansvarlig_navn"] or "",
            k["bekreftet"], k["kapasitet"] or "", fyllingsgrad, k["ekstradeltaker"], k["venteliste"], k["avmeldt"],
            k["avslatt"],
            k["utgatt"], k["forlatt"], k["fakturert"])])
    db.logg(con(), "rapport_eksportert", {"rapport": "kurs", **filtre, "antall_rader": len(rader)}, aktor=_aktor())
    con().commit()
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=kursrapport.csv"})


def _deltakere_rapport_rader():
    sok = request.args.get("sok", "").strip()
    kun_flere = request.args.get("kun_flere_kurs") == "1"
    sql = """SELECT d.id, d.navn, d.fornavn, d.etternavn, d.epost, d.telefon, d.arbeidssted,
                    COUNT(DISTINCT CASE WHEN p.status='bekreftet' AND p.ekstradeltaker_ts IS NULL THEN p.kurs_id END) AS antall_kurs
             FROM deltaker d LEFT JOIN paamelding p ON p.deltaker_id=d.id
             WHERE (? = '' OR LOWER(d.navn) LIKE ? OR LOWER(d.epost) LIKE ? OR LOWER(COALESCE(d.arbeidssted,'')) LIKE ?)
             GROUP BY d.id
             HAVING (? = 0 OR COUNT(DISTINCT CASE WHEN p.status='bekreftet' AND p.ekstradeltaker_ts IS NULL THEN p.kurs_id END) > 1)
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
    w.writerow(["Fornavn", "Etternavn", "E-post", "Telefon", "Arbeidssted",
                f"Antall kurs ({db.PAAMELDINGSSTATUSER['paameldt'].lower()})"])
    for d in rader:
        w.writerow([sikkerhet.csv_trygg(v) for v in (
            d["fornavn"], d["etternavn"], d["epost"], d["telefon"] or "", d["arbeidssted"] or "", d["antall_kurs"])])
    db.logg(con(), "rapport_eksportert", {"rapport": "deltakere", **filtre, "antall_rader": len(rader)}, aktor=_aktor())
    con().commit()
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=deltakerregister.csv"})


@app.get("/admin/rapporter/deltaker/<int:deltaker_id>")
@krever_admin
def admin_rapport_deltaker(deltaker_id):
    deltaker = con().execute("SELECT * FROM deltaker WHERE id=?", (deltaker_id,)).fetchone() or abort(404)
    paameldinger = con().execute(
        """SELECT p.id AS paamelding_id, p.kurs_id, p.status, p.avslatt_ts, p.utgatt_ts, p.forlatt_ts, p.ekstradeltaker_ts,
                  p.opprettet,
                  k.navn AS kurs_navn, k.kursnr, k.kode, k.status AS kurs_status
           FROM paamelding p JOIN kurs k ON k.id=p.kurs_id WHERE p.deltaker_id=? ORDER BY p.opprettet DESC""",
        (deltaker_id,)).fetchall()
    fakturaer = okonomi.rapport(okonomi.fakturaer(con(), date(2000, 1, 1), date(2100, 12, 31), deltaker_id=deltaker_id))
    return render_template("admin_rapport_deltaker.html", deltaker=deltaker, paameldinger=paameldinger,
                           fakturaer=fakturaer)


@app.post("/admin/rapporter/deltaker/<int:deltaker_id>/anonymiser")
@krever_admin
def admin_anonymiser_deltaker(deltaker_id):
    """Retten til sletting (kun systemadministrator). Krever at admin skriver ANONYMISER - handlingen kan ikke angres."""
    if request.form.get("bekreft", "").strip().upper() != "ANONYMISER":
        flash("Skriv ANONYMISER i feltet for å bekrefte. Ingenting er endret.", "feil")
        return redirect(url_for("admin_rapport_deltaker", deltaker_id=deltaker_id))
    try:
        with db.transaksjon(con()):
            db.anonymiser_deltaker(con(), deltaker_id, aktor=_aktor())
    except db.DeltakerFeil as e:
        flash(str(e), "feil")
        return redirect(url_for("admin_rapport_deltaker", deltaker_id=deltaker_id))
    flash("Personopplysningene er fjernet fra påmeldingssystemet. Husk de manuelle stegene: Visma (regnskapsplikt – "
          "vurder), «Sendte elementer» i kurs-postboksen, personlige mapper i SharePoint og Zoom-rapporter.", "ok")
    return redirect(url_for("admin_rapport_deltaker", deltaker_id=deltaker_id))


# ---------- oekonomi (fase 14) ----------

MAKS_FAKTURALISTE = 500          # flere rader vises ikke paa skjermen (CSV har alle)


def _okonomi_utvalg():
    fra, til = okonomi.periode(request.args, _idag())
    kurs_id = request.args.get("kurs", type=int) or None
    return fra, til, kurs_id, okonomi.fakturaer(con(), fra, til, kurs_id)


@app.get("/admin/rapporter/okonomi")
@krever_admin
def admin_rapport_okonomi():
    fra, til, kurs_id, rader = _okonomi_utvalg()
    kursvalg = con().execute(
        """SELECT id, kursnr, navn FROM kurs WHERE id IN (SELECT p.kurs_id FROM faktura f
             JOIN paamelding p ON p.id=f.paamelding_id) ORDER BY kursnr DESC""").fetchall()
    return render_template("admin_rapport_okonomi.html", r=okonomi.rapport(rader), fra=fra, til=til, kurs_id=kurs_id,
                           kursvalg=kursvalg, maks=MAKS_FAKTURALISTE)


@app.get("/admin/rapporter/okonomi.csv")
@krever_admin
def admin_rapport_okonomi_csv():
    fra, til, kurs_id, rader = _okonomi_utvalg()
    db.logg(con(), "rapport_eksportert", {"rapport": "okonomi", "fra": fra.isoformat(), "til": til.isoformat(),
                                          "kurs_id": kurs_id, "antall_rader": len(rader)}, aktor=_aktor())
    con().commit()
    return _csv_respons(okonomi.CSV_KOLONNER, okonomi.csv_rader(rader), f"fakturaliste_{fra}_{til}.csv")


# Ordene for påmeldingsstatus står bare i db.PAAMELDINGSSTATUSER (og flertallet i db.PAAMELDINGSSTATUSER_FLERTALL).
# Malene bruker disse, aldri ordet «Påmeldt»/«Ekstradeltaker» direkte: {{ STATUS.paameldt }}, {{ STATUS_FLERTALL.paameldt|lower }}
app.jinja_env.globals.update(STATUS=db.PAAMELDINGSSTATUSER, STATUS_FLERTALL=db.PAAMELDINGSSTATUSER_FLERTALL)


@app.template_filter("paameldingsstatus")
def _paameldingsstatus(p) -> str:
    """Status slik den vises («Påmeldt», «Avslått» …). Avslått, utgått og forlatt er avmeldte påmeldinger med en dato."""
    return db.PAAMELDINGSSTATUSER[db.paameldingsstatus(p)]


@app.template_filter("statusetikett")
def _statusetikett(nokkel) -> str:
    """Navnet på en statusnøkkel (en visningsstatus, eller databaseverdien bekreftet = Påmeldt) slik det vises."""
    return db.statusnavn(nokkel)


# Påmeldt er grønt, og Ekstradeltaker blått («bla» har ingen egen regel: standardfargen på .merke og .statusvelger er blå),
# så de to som begge har plass, lett skilles fra hverandre.
STATUSMERKE = {"paameldt": "ok", "ekstradeltaker": "bla", "venteliste": "gul", "avmeldt": "gra", "avslatt": "feil",
               "utgatt": "gra", "forlatt": "feil"}
app.jinja_env.globals["STATUSMERKE"] = STATUSMERKE


@app.template_filter("statusmerke")
def _statusmerke(p) -> str:
    """Fargen (klassen på .merke) til påmeldingsstatusen."""
    return STATUSMERKE[db.paameldingsstatus(p)]


@app.template_filter("i_setning")
def _i_setning(tekst) -> str:
    """Samlingsteksten midt i en setning: «Samling 1, 3» -> «samling 1, 3», men et eget samlingsnavn («EMDR») beholdes (db.i_setning)."""
    return db.i_setning(tekst or "")


@app.template_filter("kr")
def _kr(belop) -> str:
    """12345 -> «12 345 kr» (hardt mellomrom, saa beloepet aldri deles over to linjer)."""
    return f"{int(belop or 0):,}".replace(",", "\u00a0") + "\u00a0kr"


@app.template_filter("samlinger")
def _samlinger(dager, kurs) -> list[dict]:
    """Kursdagene gruppert per samling til visning (kursdatoer.visning)."""
    return kursdatoer.visning(dager, kurs)


@app.template_filter("dagdato")
def _dagdato(iso: str) -> str:
    """'2027-09-20' -> 'ma. 20.09.2027'."""
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or ""
    return f"{kursdatoer.ukedag(d)} {kursdatoer.kort_dato(d)}"
@app.template_filter("epostadresse")
def _epostadresse(adresse) -> Markup:
    """E-postadresse som kan brytes pent etter @ i smale kolonner (escapes først)."""
    return Markup.escape(adresse or "").replace("@", Markup("@<wbr>"))


@app.template_filter("filstorrelse")
def _filstorrelse(byte) -> str:
    """612 -> «612 byte», 2458 -> «2,4 kB», 1300000 -> «1,2 MB»."""
    byte = int(byte or 0)
    if byte < 1024:
        return f"{byte} byte"
    if byte < 1024 * 1024:
        return f"{byte / 1024:.1f} kB".replace(".", ",")
    return f"{byte / 1024 / 1024:.1f} MB".replace(".", ",")


_UKEDAGER = ("mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag")


@app.template_filter("langdato")
def _langdato(iso) -> str:
    """'2027-01-10' -> 'søndag 10. januar 2027' (påmeldingssiden). Ugyldig dato vises som den er."""
    try:
        d = date.fromisoformat(str(iso or "")[:10])
    except ValueError:
        return str(iso or "")
    return f"{_UKEDAGER[d.weekday()]} {d.day}. {aarsplan.MAANEDER[d.month - 1].lower()} {d.year}"


@app.template_filter("maaned")
def _maaned(aar_maaned: str) -> str:
    """'2027-01' -> 'januar 2027'."""
    aar, _, mnd = aar_maaned.partition("-")
    return f"{aarsplan.MAANEDER[int(mnd) - 1].lower()} {aar}" if mnd.isdigit() and 1 <= int(mnd) <= 12 else aar_maaned


@app.get("/admin/rapporter")
@krever_admin
def admin_rapporter():
    fra = request.args.get("fra") or ""
    til = request.args.get("til") or ""
    periode_args = (fra, fra, til, til)

    # Påmeldte (fullverdige) og ekstradeltakere hver for seg: en ekstradeltaker er på kurset, men teller ikke som påmeldt
    deltakelser, ekstradeltakelser = con().execute(
        """SELECT COALESCE(SUM(CASE WHEN p.ekstradeltaker_ts IS NULL THEN 1 ELSE 0 END), 0),
                  COALESCE(SUM(CASE WHEN p.ekstradeltaker_ts IS NOT NULL THEN 1 ELSE 0 END), 0)
           FROM paamelding p WHERE p.status='bekreftet' AND p.kurs_id IN (
             SELECT kurs_id FROM kursdag GROUP BY kurs_id
             HAVING (? = '' OR MAX(dato) >= ?) AND (? = '' OR MIN(dato) <= ?))""", periode_args).fetchone()

    venteliste_kurs = con().execute(
        """SELECT k.id, k.navn, k.kursnr, COUNT(*) AS antall FROM paamelding p JOIN kurs k ON k.id=p.kurs_id
           WHERE p.status='venteliste' AND k.id IN (
             SELECT kurs_id FROM kursdag GROUP BY kurs_id
             HAVING (? = '' OR MAX(dato) >= ?) AND (? = '' OR MIN(dato) <= ?))
           GROUP BY k.id ORDER BY antall DESC""", periode_args).fetchall()

    avlyste_kurs = con().execute(
        """SELECT k.id, k.navn, k.kursnr FROM kurs k WHERE k.status='avlyst' AND k.id IN (
             SELECT kurs_id FROM kursdag GROUP BY kurs_id
             HAVING (? = '' OR MAX(dato) >= ?) AND (? = '' OR MIN(dato) <= ?))""", periode_args).fetchall()

    avmeldt_antall = con().execute(
        """SELECT COUNT(*) FROM hendelse WHERE handling='avmelding'
           AND (? = '' OR substr(ts, 1, 10) >= ?) AND (? = '' OR substr(ts, 1, 10) <= ?)""", periode_args).fetchone()[0]

    fakturert_sum = con().execute(
        """SELECT COALESCE(SUM(belop_nok),0) FROM faktura
           WHERE (? = '' OR substr(opprettet, 1, 10) >= ?) AND (? = '' OR substr(opprettet, 1, 10) <= ?)""",
        periode_args).fetchone()[0]

    flere_kurs_antall = con().execute(
        """SELECT COUNT(*) FROM (SELECT deltaker_id FROM paamelding WHERE status='bekreftet' AND ekstradeltaker_ts IS NULL
             GROUP BY deltaker_id HAVING COUNT(DISTINCT kurs_id) > 1) AS flere""").fetchone()[0]   # alias: PostgreSQL < 16

    return render_template(
        "admin_rapporter.html", fra=fra, til=til, deltakelser=deltakelser, ekstradeltakelser=ekstradeltakelser,
        venteliste_kurs=venteliste_kurs,
        avlyste_kurs=avlyste_kurs, avmeldt_antall=avmeldt_antall, fakturert_sum=fakturert_sum,
        flere_kurs_antall=flere_kurs_antall)


# ---------- kursduplisering: kilde og kopi av paameldingsskjema (fase 12C4B) ----------
_MAKS_KURS_ID = 2 ** 63 - 1                          # SQLite INTEGER; storre verdier ville gitt OverflowError (500)
_KURS_ID_MOENSTER = re.compile(r"[1-9][0-9]*")       # kun ASCII-sifre, ingen fortegn/desimaler/mellomrom/ledende 0
_SKJEMA_KOPI_ADVARSEL = ("Noen skjemainnstillinger på originalkurset kunne ikke kopieres. "
                         "Kontroller Påmeldingsskjema på det nye kurset.")
_SIDE_KOPI_ADVARSEL = ("Noen nettsideinnstillinger på originalkurset kunne ikke kopieres. "
                       "Kontroller Nettside på det nye kurset.")


def _kildekurs_fra(verdier):
    """Autoritativ tolkning av `fra` (brukes av baade GET-args og POST-skjema). Ingen `fra`-nokkel -> None (vanlig nytt
    kurs). Finnes nokkelen, er det en EKSPLISITT duplisering: ugyldig verdi -> 400, ukjent kurs -> 404. En ugyldig
    `fra` blir ALDRI stille tolket som et vanlig nytt kurs."""
    if "fra" not in verdier:
        return None
    raa = verdier["fra"]
    if not _KURS_ID_MOENSTER.fullmatch(raa) or int(raa) > _MAKS_KURS_ID:
        abort(400)
    return _hent_kurs(int(raa))


def _skjema_kopiplan(kilde_id: int) -> tuple[list, bool]:
    """Kildekursets skjemaoverstyringer som et fast, rent snapshot - lest NOYAKTIG EN gang, FOER SharePoint/DB-skriving.

    Bygger kun paa Leseresultat.overstyringer (kjente felt, tillatte og gyldige egenskaper, standardverdier allerede
    normalisert bort) - aldri raa rader, oppdatert eller oppdatert_av. Hver post forhaandsvalideres med samme funksjon
    som lagringen bruker. SkjemaLesefeil sendes videre (fail-closed). Returnerer (plan, kilden hadde advarsler)."""
    les = db.hent_skjemaoverstyringer(con(), kilde_id)
    plan = []
    for felt in sorted(les.overstyringer):
        o = les.overstyringer[felt]
        egenskaper = {e: getattr(o, e) for e in o.satte_egenskaper()}
        skjemafelt.normaliser_overstyring(felt, egenskaper)
        plan.append((felt, egenskaper))
    return plan, les.har_advarsler


_FORMAT = {"fysisk": "Fysisk", "digital": "Online", "hybrid": "Hybrid"}
_BETALINGSPLANER = ("samlet", "per_samling", "deltaker_velger")
_GYLDIG_EPOST = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def _standardverdier() -> dict:
    """Kursskjemaet for et helt nytt kurs: fysisk, faktura til hver deltaker, deltakeren velger fakturaplan (bare når
    kurset har flere samlinger - ellers én vanlig faktura), 14 dager, og den innloggede brukeren som ansvarlig.
    Påmeldingsfristen foreslås ut fra datoene."""
    return {"navn": "", "type": "fysisk", "sted": "", "zoom_url": "", "kapasitet": "", "pris_nok": "0", "faktura": True,
            "fakturering": "person", "betaling": "deltaker_velger", "faktura_dager_for": "14", "notat": "",
            "paameldingsfrist": "", "paameldingsfrist_manuell": "0", "ansvarlig_admin_id": str(session.get("admin_id") or "")}


def _binding_tekst(b: dict) -> str:
    """Det som har bundet kurset økonomisk, som en setningsdel til advarselen i Oppsett: «3 fakturaer, 1 uavklart fakturaforsøk og
    2 planlagte fakturaer». Tallene kommer fra db.okonomisk_binding_oversikt. Tom streng når kurset ikke har noe av det."""
    deler = []
    if b["fakturaer"]:
        deler.append(f"{b['fakturaer']} {'faktura' if b['fakturaer'] == 1 else 'fakturaer'}")
    if b["forsok"]:
        deler.append(f"{b['forsok']} uavklart{'e' if b['forsok'] > 1 else ''} fakturaforsøk")
    if b["planlagt"]:
        deler.append(f"{b['planlagt']} planlagt{'e' if b['planlagt'] > 1 else ''} {'faktura' if b['planlagt'] == 1 else 'fakturaer'}")
    return ", ".join(deler[:-1]) + " og " + deler[-1] if len(deler) > 1 else "".join(deler)


def _kursverdier(kurs) -> dict:
    """Kursskjemaets verdier (tekst) fra et lagret kurs. Fakturering «ingen» vises som at Faktura ikke er krysset av."""
    return {"navn": kurs["navn"], "type": kurs["type"], "sted": kurs["sted"] or "", "zoom_url": kurs["zoom_url"] or "",
            "kapasitet": str(kurs["kapasitet"] or ""), "pris_nok": str(kurs["pris_nok"]),
            "faktura": kurs["fakturering"] != "ingen",
            "fakturering": kurs["fakturering"] if kurs["fakturering"] != "ingen" else "person",
            "betaling": kurs["betaling"], "faktura_dager_for": str(kurs["faktura_dager_for"]), "notat": kurs["notat"] or "",
            "paameldingsfrist": kurs["paameldingsfrist"] or "",
            "paameldingsfrist_manuell": "1" if kurs["paameldingsfrist_manuell"] else "0",
            "ansvarlig_admin_id": str(kurs["ansvarlig_admin_id"] or "")}


def _innsendte_verdier(f) -> dict:
    """Det admin sendte inn, slik at skjemaet vises igjen etter en feil uten at noe går tapt."""
    v = {k: f.get(k, "") for k in ("navn", "type", "sted", "zoom_url", "kapasitet", "pris_nok", "fakturering", "betaling",
                                    "faktura_dager_for", "notat", "paameldingsfrist", "paameldingsfrist_manuell",
                                    "ansvarlig_admin_id")}
    v["faktura"] = "faktura" in f.getlist("betalingsmaate")
    return v


def _les_kursskjema(f, *, bare_innsendte: bool = False) -> tuple[dict, list[str]]:
    """Kursfeltene i «Opprett kurs» og i kursinnstillingene i Oppsett: verdier klare for databasen og norske feilmeldinger.

    Format: sted er påkrevd for fysisk og hybrid, og et onlinekurs har ikke sted. Pris og fakturafeltene leses alltid, men med
    bare_innsendte (Oppsett for et kurs med fakturaer, se db.har_okonomisk_binding_for_kurs) bare de som faktisk er sendt: en
    side som var åpen før feltene kom med, endrer dem ikke. En ugyldig verdi avvises før noe lagres. Om verdiene er endret, og om
    endringen er bekreftet, avgjør kalleren (admin_kurs_oppsett_lagre)."""
    feil: list[str] = []
    felter: dict = {"navn": f.get("navn", "").strip(), "type": f.get("type", "")}
    if not felter["navn"]:
        feil.append("Fyll inn kursnavn.")
    if felter["type"] not in _FORMAT:
        feil.append("Velg format: fysisk, online eller hybrid.")
    sted = f.get("sted", "").strip() or None
    felter["sted"] = None if felter["type"] == "digital" else sted
    if felter["type"] in ("fysisk", "hybrid") and not sted:
        feil.append("Fyll inn sted. Det er påkrevd for fysiske kurs og hybridkurs.")
    kapasitet = f.get("kapasitet", "").strip()
    felter["kapasitet"] = int(kapasitet) if kapasitet.isdigit() and int(kapasitet) >= 1 else None
    if kapasitet and felter["kapasitet"] is None:
        feil.append("Kapasitet må være et helt tall fra 1 og oppover (tom = ubegrenset).")
    felter["notat"] = f.get("notat", "").strip() or None
    okonomi, okonomifeil = _les_okonomi(f, bare_innsendte=bare_innsendte)
    return {**felter, **okonomi}, feil + okonomifeil


def _les_okonomi(f, *, bare_innsendte: bool = False) -> tuple[dict, list[str]]:
    """Pris og fakturafeltene. Betaling: «Faktura» er eneste betalingsmåte inntil kortbetaling finnes (Kort vises bare
    grået ut og lagres ikke). Uten Faktura blir fakturering «ingen», og da må kurset være gratis. Et eldre skjema uten
    feltet «betalingsmaater» sender fakturering direkte (person, organisasjon eller ingen).
    Med bare_innsendte leses bare feltene som faktisk er sendt (kurs med fakturaer, se _les_kursskjema)."""
    felter: dict = {}
    feil: list[str] = []
    med = (lambda navn: navn in f) if bare_innsendte else (lambda navn: True)
    if med("pris_nok"):
        pris = f.get("pris_nok", "").strip() or "0"
        felter["pris_nok"] = int(pris) if pris.isdigit() else 0
        if not pris.isdigit():
            feil.append("Pris må være et helt tall i kroner (0 = gratis).")
    if "betalingsmaater" in f:
        if "faktura" not in f.getlist("betalingsmaate"):
            felter["fakturering"] = "ingen"
            if felter.get("pris_nok", 0) > 0:
                feil.append("Kurset har en pris, men ingen betalingsmåte. Kryss av for Faktura (kortbetaling kommer senere).")
        else:
            felter["fakturering"] = f.get("fakturering", "person")
    elif med("fakturering"):
        felter["fakturering"] = f.get("fakturering", "")
    if "fakturering" in felter and felter["fakturering"] not in ("person", "organisasjon", "ingen"):
        feil.append("Ugyldig faktureringsvalg.")
    if med("betaling"):
        felter["betaling"] = f.get("betaling", "samlet")
        if felter["betaling"] not in _BETALINGSPLANER:
            feil.append("Ugyldig fakturaplan.")
    if med("faktura_dager_for"):
        try:
            felter["faktura_dager_for"] = db.valider_faktura_dager_for(int(f.get("faktura_dager_for") or 14))
        except ValueError:
            feil.append("«Dager før hver samling» må være tall – hele dager, f.eks. 14.")
        except db.Paameldingsfeil as e:
            feil.append(str(e))
    return felter, feil


def _les_frist(f) -> tuple[str | None, int | None, list[str]]:
    """Påmeldingsfristen fra skjemaet: (frist, manuell, feil). manuell=1 når admin har satt fristen selv (app.js setter
    feltet paameldingsfrist_manuell når fristen endres for hånd, og tilbake til 0 ved «Bruk forslag»). Et eldre skjema
    uten feltet gir manuell=None: fristen lagres som skrevet, og markeringen står urørt. Tom frist = ingen frist."""
    frist = f.get("paameldingsfrist", "").strip() or None
    feil = []
    if frist:
        try:
            date.fromisoformat(frist)
        except ValueError:
            feil.append("Påmeldingsfristen må være en gyldig dato.")
            frist = None
    manuell = None if "paameldingsfrist_manuell" not in f else (1 if f.get("paameldingsfrist_manuell") == "1" else 0)
    return frist, manuell, feil


def _aktive_admins():
    return con().execute("SELECT id, navn FROM admin_bruker WHERE aktiv=1 ORDER BY navn").fetchall()


def _render_ny_kurs(fra_kurs, status: int = 200, *, skjema=None, feil=None):
    """Skjemaet «Opprett kurs». `skjema` er det admin sendte inn (vises igjen etter en feil). Et duplikat får kursets
    felt og samlingene uten datoer (kursdatoer.rader_for_duplikat), men den innloggede brukeren som ansvarlig og en
    frist som foreslås på nytt."""
    if skjema is not None:
        v = _innsendte_verdier(skjema)
        rader = kursdatoer.rader_fra_innsendt(skjema)
        oppsummering = kursdatoer.oppsummering(kursdatoer.fra_skjema(skjema)[0])
    else:
        v = {**_kursverdier(fra_kurs), "paameldingsfrist": "", "paameldingsfrist_manuell": "0",
             "ansvarlig_admin_id": str(session.get("admin_id") or "")} if fra_kurs else _standardverdier()
        rader = kursdatoer.rader_for_duplikat(db.lagrede_samlinger(con(), fra_kurs["id"])) if fra_kurs else []
        oppsummering = ""
    kilde_visning = kursdatoer.visning(db.kursdager(con(), fra_kurs["id"]), fra_kurs) if fra_kurs else []
    return render_template("admin_ny_kurs.html", fra_kurs=fra_kurs, v=v, rader=rader or [kursdatoer.rad()],
                           oppsummering=oppsummering, kilde_visning=kilde_visning, admins=_aktive_admins(),
                           formater=_FORMAT, feil=feil or []), status


def _ekstrafelt_kopiplan(kilde_id: int) -> tuple[list, bool]:
    """Kildekursets egne felt som et fast, rent snapshot - lest EN gang, FOER SharePoint/DB-skriving (som
    _skjema_kopiplan). Hvert felt forhaandsvalideres med samme funksjon som lagringen bruker. Et felt med ugyldig
    betingelse kopieres SKJULT og uten betingelse. Svarene kopieres aldri. SkjemaLesefeil sendes videre (fail-closed).
    Returnerer ([(gammel_id, verdier, plass)], kilden hadde advarsler)."""
    les = db.hent_ekstrafelt(con(), kilde_id)
    plan = []
    for e in les.felt:
        data = {"type": e.type, "label": e.label, "hjelpetekst": e.hjelpetekst, "valg": e.valg,
                "obligatorisk": e.obligatorisk, "synlig": e.synlig and e.betingelse_ok, "plassering": e.plassering,
                "vis_naar_felt": e.vis_naar_felt if e.betingelse_ok else None,
                "vis_naar_verdi": e.vis_naar_verdi if e.betingelse_ok else None}
        plan.append((e.id, ekstrafelt.normaliser(data, e.id), e.rekkefolge))
    return plan, les.har_advarsler



@app.route("/admin/kurs/ny", methods=["GET", "POST"])
@krever_admin
def admin_ny_kurs():
    if request.method == "POST":
        f = request.form
        kilde = _kildekurs_fra(f)          # 400/404 FOER all annen behandling og FOER SharePoint
        if any(n.startswith("s-") for n in f):
            return _opprett_kurs(f, kilde)
        resultat = _opprett_kurs_gammelt_skjema(f, kilde)
        if resultat is not None:
            return resultat
    return _render_ny_kurs(_kildekurs_fra(request.args))


def _opprett_kurs(f, kilde):
    """Skjemaet «Opprett kurs»: kursfelt, samlinger med kursdager, påmeldingsfrist, ansvarlig og betaling. Ved feil vises
    skjemaet igjen med alt admin skrev inn, og ingenting er lagret."""
    er_duplikat = kilde is not None
    felter, feil = _les_kursskjema(f)
    samlinger, datofeil = kursdatoer.fra_skjema(f)
    feil += datofeil or kursdatoer.kontroller(samlinger)
    frist, manuell, fristfeil = _les_frist(f)
    feil += fristfeil
    ansvarlig = f.get("ansvarlig_admin_id", "").strip()
    ansvarlig_id = int(ansvarlig) if ansvarlig.isdigit() and int(ansvarlig) <= _MAKS_KURS_ID else None
    if ansvarlig and not (ansvarlig_id and con().execute(
            "SELECT 1 FROM admin_bruker WHERE id=? AND aktiv=1", (ansvarlig_id,)).fetchone()):
        feil.append("Velg en ansvarlig fra listen.")
    if feil:
        return _render_ny_kurs(kilde, 400, skjema=f, feil=feil)
    try:
        # 12C4B/12C5: kildens skjemaoppsett og nettsidetekster leses EN gang - FOER SharePoint og DB-skriving.
        kopiplan, kopi_advarsel = _skjema_kopiplan(kilde["id"]) if er_duplikat else ([], False)
        ekstraplan, ekstra_advarsel = _ekstrafelt_kopiplan(kilde["id"]) if er_duplikat else ([], False)
        side_kopi, side_advarsel = paameldingsside.kopi_av_side(kilde) if er_duplikat else ({}, False)
        with db.transaksjon(con()):
            navn = felter.pop("navn")
            kode = db.generer_kode(con(), navn, kursdatoer.datoer_iso(samlinger))
            felter.update(ansvarlig_admin_id=ansvarlig_id, paameldingsfrist=frist if manuell else None,
                          paameldingsfrist_manuell=1 if manuell else 0)
            if er_duplikat:
                # Et duplisert kurs skal gjennomgås og åpnes bevisst - ikke være synlig/åpent for påmelding med en gang.
                felter["status"] = "utkast"
                felter.update(side_kopi)
            kid = db.opprett_kurs(con(), kode=kode, navn=navn, datoer=[], samlinger=samlinger, idag=_idag(),
                                  aktor=_aktor(), **felter)
            for felt, egenskaper in kopiplan:
                db.lagre_skjemafelt(con(), kid, felt, egenskaper, aktor=_aktor())
            db.kopier_ekstrafelt(con(), kid, ekstraplan, aktor=_aktor())
    except skjemafelt.SkjemaLesefeil:
        flash("Skjemainnstillingene til originalkurset kunne ikke leses akkurat nå. Kurset er ikke opprettet. "
              "Prøv igjen om litt.", "feil")
        return _render_ny_kurs(kilde, 503, skjema=f)
    except db.Kursdagfeil as e:
        return _render_ny_kurs(kilde, 400, skjema=f, feil=e.meldinger)
    except (db.IntegritetsFeil, db.Paameldingsfeil, skjemafelt.SkjemafeltFeil, ekstrafelt.EkstrafeltFeil) as e:
        flash(f"Kunne ikke opprette kurs: {e}", "feil")
        return _render_ny_kurs(kilde, 400, skjema=f)
    _kurs_opprettet_meldinger(kid, kode, kopi_advarsel or ekstra_advarsel, side_advarsel)
    if not manuell:
        ny_frist = con().execute("SELECT paameldingsfrist FROM kurs WHERE id=?", (kid,)).fetchone()[0]
        flash(f"Påmeldingsfristen er satt til {kursdatoer.kort_dato(date.fromisoformat(ny_frist))}. Den følger datoene "
              "til du endrer den selv under Oppsett.", "info")
    return redirect(url_for("admin_kurs_oppsett", kurs_id=kid))


def _kurs_opprettet_meldinger(kid: int, kode: str, kopi_advarsel: bool, side_advarsel: bool) -> None:
    kursnr = con().execute("SELECT kursnr FROM kurs WHERE id=?", (kid,)).fetchone()["kursnr"]
    if _opprett_sharepoint_mappe(kid, kode):   # ekstern sideeffekt: først når kurset er lagret
        flash(f"Kurset er opprettet med kursnummer {kursnr}, og har fått påmeldingsside, SharePoint-mappe og "
              "innsjekkkoder.", "ok")
    else:
        flash(f"Kurset er opprettet med kursnummer {kursnr}, og har fått påmeldingsside og innsjekkkoder. "
              "SharePoint-mappen kunne ikke lages akkurat nå – bruk knappen «Lag SharePoint-mappe» under "
              "Kursmateriell for å prøve igjen.", "info")
    if kopi_advarsel:
        flash(_SKJEMA_KOPI_ADVARSEL, "info")
    if side_advarsel:
        flash(_SIDE_KOPI_ADVARSEL, "info")


def _opprett_kurs_gammelt_skjema(f, kilde):
    """Den gamle formen av skjemaet (datoer som én ISO-dato per linje, kursets klokkeslett, spesialistløp og kursholder),
    uendret, for klienter som fortsatt sender den. Returnerer None ved feil (skjemaet vises da på nytt)."""
    er_duplikat = kilde is not None
    datoer = sorted({d.strip() for d in f.get("datoer", "").replace(",", "\n").splitlines() if d.strip()})
    try:
        for d in datoer:
            date.fromisoformat(d)
        paameldingsfrist = f.get("paameldingsfrist") or None
        if paameldingsfrist:
            date.fromisoformat(paameldingsfrist)
        faktura_dager_for = db.valider_faktura_dager_for(int(f.get("faktura_dager_for") or 14))   # foer SharePoint/DB
        # 12C4B: kildens skjemaoppsett leses EN gang her - etter vanlig validering, FOER SharePoint og DB-skriving.
        kopiplan, kopi_advarsel = _skjema_kopiplan(kilde["id"]) if er_duplikat else ([], False)
        ekstraplan, ekstra_advarsel = _ekstrafelt_kopiplan(kilde["id"]) if er_duplikat else ([], False)
        # 12C5: nettsidens tekster fra den allerede leste kilderaden (ingen ny lesing) - kun gyldige verdier.
        side_kopi, side_advarsel = paameldingsside.kopi_av_side(kilde) if er_duplikat else ({}, False)
        with db.transaksjon(con()):
            kode = db.generer_kode(con(), f["navn"].strip(), datoer)
            felter = dict(
                type=f["type"], sted=f.get("sted") or None, start_kl=f["start_kl"], slutt_kl=f["slutt_kl"],
                timer_pr_dag=float(f.get("timer_pr_dag") or 6), kapasitet=int(f["kapasitet"]) if f.get("kapasitet") else None,
                pris_nok=int(f.get("pris_nok") or 0), fakturering=f["fakturering"],
                betaling=f.get("betaling", "samlet"), faktura_dager_for=faktura_dager_for,
                spesialistlop=f.get("spesialistlop") or None, kursholder_epost=f.get("kursholder_epost") or None,
                notat=f.get("notat") or None, ansvarlig_admin_id=f.get("ansvarlig_admin_id", type=int),
                paameldingsfrist=paameldingsfrist,
            )
            if er_duplikat:
                # Et duplisert kurs skal gjennomgås og åpnes bevisst - ikke være synlig/åpent for påmelding med en gang.
                felter["status"] = "utkast"
                felter.update(side_kopi)
            kid = db.opprett_kurs(con(), kode=kode, navn=f["navn"].strip(), datoer=datoer, aktor=_aktor(), **felter)
            # Kopien faar EGNE rader paa den NYE kurs-id-en, i samme transaksjon som kurs/kursdager/materiell.
            for felt, egenskaper in kopiplan:
                db.lagre_skjemafelt(con(), kid, felt, egenskaper, aktor=_aktor())
            db.kopier_ekstrafelt(con(), kid, ekstraplan, aktor=_aktor())
            if f.get("kursholder_epost") and f.get("materiell_frist"):
                con().execute("INSERT INTO materiell_krav (kurs_id, ansvarlig_navn, ansvarlig_epost, frist) VALUES (?,?,?,?)",
                              (kid, f.get("kursholder_navn") or f["kursholder_epost"], f["kursholder_epost"], f["materiell_frist"]))
        _kurs_opprettet_meldinger(kid, kode, kopi_advarsel or ekstra_advarsel, side_advarsel)
        return redirect(url_for("admin_kurs_oppsett", kurs_id=kid))
    except skjemafelt.SkjemaLesefeil:
        flash("Skjemainnstillingene til originalkurset kunne ikke leses akkurat nå. Kurset er ikke opprettet. "
              "Prøv igjen om litt.", "feil")
        return _render_ny_kurs(kilde, 503)
    except (ValueError, KeyError, db.IntegritetsFeil, db.Paameldingsfeil, skjemafelt.SkjemafeltFeil,
            ekstrafelt.EkstrafeltFeil) as e:
        flash(f"Kunne ikke opprette kurs: {e}", "feil")


@app.route("/admin/daglig", methods=["GET", "POST"])
@krever_admin
def admin_daglig():
    """Demo: kjoer/spol den daglige jobben fra admin. Drift: KUN toerrkjoering herfra - den ekte jobben kjoeres av
    planlagt oppgave (python -m kurs.daglig), aldri med vilkaarlig dato fra et skjema."""
    utskrift = None
    if request.method == "POST":
        try:
            dato = date.fromisoformat(request.form.get("dato") or date.today().isoformat())
        except ValueError:
            flash("Ugyldig dato.", "feil")
            return render_template("admin_daglig.html", utskrift=None), 400
        tor = bool(request.form.get("tor")) or not config.DEMO
        if config.DEMO:
            session["demo_dato"] = dato.isoformat()
        k = Kjoring(con(), idag=dato, tor=tor)
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
                      "tekst": tekst, "tilpasset": felt in raa,
                      "koder": [k for k in maltekster.kodeliste(f.kode)
                                if k != "sporsmal_url" or config.ASSISTENT_AKTIV or "{sporsmal_url}" in (tekst or "")],
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
            visninger.append({"variant": variant, "emne": emne, "html": signaturer.til_visning(html)})
    except maltekster.MalFeil as e:
        feil = e.forklaring or "Malen kunne ikke forhåndsvises akkurat nå."
    return render_template("admin_maltekst_forhandsvis.html", mal=mal, mal_navn=maltekster.MALER[mal].navn,
                           visninger=visninger, feil=feil, med_signatur=mal in signaturer.KURSETS_MALER,
                           standardsignatur=signaturer.standard(con())["navn"])


@app.get("/admin/utboks")
@krever_admin
def admin_utboks():
    return render_template("admin_utboks.html", meldinger=epost.les_utboks())


@app.route("/admin/logg-ut", methods=["GET", "POST"])
def admin_logg_ut():
    _admin_logg_ut()
    return redirect(url_for("forside"))


# ======================= mottak fra nettsidens skjema =======================

# Feltnavn fra ulike skjema-plugins -> vaare navn. Utvides naar vi ser hva ipr.no sitt skjema sender. Fornavn og etternavn
# er paakrevd hver for seg; "navn" (fullt navn i ett felt) brukes KUN til en tydelig avvisning - det gjettes aldri paa hva
# som er fornavn og etternavn.
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
    # Deltakerens PRIVATE adresse (MAA vaere med; paakrevd med mindre noedbremsen ADRESSE_KREVES_I_WEBHOOK=0 er satt, se api_paamelding). Feltene
    # faktura_adresse/faktura_postnr/faktura_sted er ikke lenger med: en privat betaler faktureres paa denne adressen, og firma
    # faktureres paa adressen fra Enhetsregisteret.
    "adresse": ["adresse", "address", "gateadresse"],
    "postnr": ["postnr", "postnummer", "zip", "postal_code"],
    "poststed": ["poststed", "sted", "city"],
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
    Paakrevd: fornavn, etternavn, epost og kurs (kode eller kursnummer). Fullt navn i ett felt avvises med 400.

    ADRESSEN (deltakerens PRIVATE adresse: adresse, postnr og poststed, også når arbeidsgiver betaler) MÅ være med: fakturaen
    sendes automatisk ved påmelding (tidligst seks måneder før første kursdag), og da må adressen være klar. Kravet styres av
    config.ADRESSE_KREVES_I_WEBHOOK (miljøvariabelen ADRESSE_KREVES_I_WEBHOOK), som er PÅ som standard; bare verdien «0» slår det av.
      * PÅ (standard): som i det offentlige skjemaet. Mangler adressen (eller noen av delene), er svaret 400 med melding om hvilke
        felt som mangler, og ingenting registreres.
      * AV («0», en NØDBREMS som bare skal brukes midlertidig i overgangsperioden hvis ipr.no ennå ikke sender adressen): påmeldingen
        går ikke tapt. Den registreres (201) selv om adressen mangler. Kommer bare ett eller to av feltene inn, TAS DE VARE PÅ som
        ufullstendige data (de kastes ikke), men bare når personen ikke har noen adresse fra før: en komplett adresse som står der,
        beholdes, og en halv adresse blandes aldri med en annen (da kunne to halve bli til en komplett adresse som aldri har eksistert).
        Personen er merket «Privat adresse mangler» (ingenting mottatt) eller «Privat adresse er ufullstendig» (bare noe mottatt) i
        deltakerlisten og i deltakervinduet. En ufullstendig adresse gjelder aldri som adresse: fakturaen HOLDES TILBAKE til alle tre
        feltene er komplette (privatadresse.py). Bekreftelsen går ut som vanlig, men ingen faktura lages eller sendes uten komplett
        privat adresse, heller ikke når kurset starter om under seks måneder.
        Det som ER sendt, må være gyldig (ikke for langt, ingen kontrolltegn), ellers 400. Svaret får feltet «merknad» når adressen mangler.
        Er ipr.no oppdatert og testet, settes ADRESSE_KREVES_I_WEBHOOK tilbake til PÅ (fjern «0»).
    Betaler deltakeren selv, faktureres adressen; betaler firma (org_nr er sendt), faktureres firmaets adresse fra Enhetsregisteret,
    og den private adressen lagres bare på personen.

    FØR PRODUKSJON (OPERATIONS.md, «Adressekravet i webhook og CSV-import»):
      1. ipr.no-skjemaet/pluginen MÅ sende feltene adresse, postnr og poststed (navnene er i FELTMAP over; «adresse»,
         «postnr» og «poststed» er de tre faste). Uten dem svarer webhooken 400, og ingen kan melde seg på fra nettsiden.
      2. Test mot testmiljøet med curl (eksempel i OPERATIONS.md): med alle tre feltene (201), uten dem (400) og med bare
         noen av dem (400).
      3. Slå ikke kravet av i produksjon annet enn som midlertidig nødbrems (ADRESSE_KREVES_I_WEBHOOK=0, App Setting i Azure).
    """
    import hashlib
    import hmac
    sikkerhet.krev_kvote("webhook")
    if not config.DEMO and config.WEBHOOK_HEMMELIG in sikkerhet.SVAKE_NOKLER:
        return {"status": "feil", "melding": "mottak er ikke konfigurert"}, 503
    raa = request.get_data()
    sig = request.headers.get("X-IPR-Signatur", "")
    token = request.headers.get("X-IPR-Token", "")    # kun som hode - aldri i URL (havner i logger/proxyer)
    riktig = hmac.new(config.WEBHOOK_HEMMELIG.encode(), raa, hashlib.sha256).hexdigest()
    if not (hmac.compare_digest(sig, riktig) or (token and hmac.compare_digest(token, config.WEBHOOK_HEMMELIG))):
        return {"status": "feil", "melding": "ugyldig signatur"}, 401

    data = request.get_json(silent=True) or request.form.to_dict(flat=True)
    fornavn, etternavn = _hent(data, "fornavn"), _hent(data, "etternavn")
    epost_, kurskode = _hent(data, "epost"), _hent(data, "kurs")
    if not fornavn or not etternavn or not epost_ or "@" not in epost_ or not kurskode:
        db.logg(con(), "webhook_avvist", {"felter": sorted(data.keys())})
        con().commit()
        melding = "mangler fornavn, etternavn, epost eller kurs"
        if _hent(data, "navn") and not (fornavn and etternavn):
            melding += " (fullt navn i ett felt tas ikke imot - send fornavn og etternavn hver for seg)"
        return {"status": "feil", "melding": melding}, 400
    adresse = {k: _hent(data, k) for k in skjemafelt.ADRESSEFELT}      # _hent gir None for et tomt felt: tomt = ikke sendt
    krever = config.ADRESSE_KREVES_I_WEBHOOK      # PÅ som standard (adressen MÅ være med); AV bare som midlertidig nødbrems
    if adressefeil := (skjemafelt.valider_adresse(adresse) if krever else skjemafelt.valider_adresse_nodbrems(adresse)):
        # Bare feltnavnene logges - aldri verdiene
        db.logg(con(), "webhook_avvist", {"felter": sorted(data.keys()), "adresse_mangler": sorted(adressefeil)})
        con().commit()
        forklaring = ("deltakerens private adresse er påkrevd" if krever else
                      "adressen er midlertidig valgfri (nødbrems), men det som er sendt, må være gyldig")
        return {"status": "feil", "melding": f"mangler eller ugyldig adresse, postnr eller poststed ({forklaring}): "
                                             + ", ".join(sorted(adressefeil))}, 400
    kurs = con().execute("SELECT * FROM kurs WHERE kode=? OR CAST(kursnr AS TEXT)=?",
                         (kurskode.upper(), kurskode)).fetchone()
    if not kurs or not _kurs_offentlig_tilgjengelig(kurs):
        return {"status": "feil", "melding": "ukjent kurs"}, 404

    # Samme regler som det offentlige skjemaet: HPR kun i spesialistloep, allergier kun for fysiske/hybride kurs,
    # faktura-/betalingsfelt kun naar kurset fakturerer per deltaker med pris. Alt annet ignoreres.
    # Den felles regelen (firmaopplysninger.py): firma betaler når org_nr er sendt. Firmanavn og adresse hentes fra
    # registeret og skrives aldri av nettsiden. En påmelding avvises ikke fordi nummeret er ugyldig eller registeret ikke
    # svarer: den beholdes, merket «Firmaopplysninger må kontrolleres», og fakturaen venter.
    org_nr = _hent(data, "org_nr") if skjemafelt.vis_fakturablokk(kurs) else None
    oppslag = firmaopplysninger.slaa_opp(org_nr) if org_nr else None
    fakturafelt = {"faktura_ref": _hent(data, "faktura_ref")} if skjemafelt.vis_fakturablokk(kurs) else {}
    # En komplett adresse lagres som før. Med PÅ-kravet er den alltid komplett her. Nødbremsen kan slippe gjennom en ufullstendig: den
    # tas vare på (informasjonen kastes ikke), men bare når personen ikke har noen adresse fra før. Har personen noe fra før, røres det
    # ikke: en komplett adresse beholdes, og to halve adresser blandes aldri (de kunne blitt en komplett adresse som aldri har
    # eksistert, og fakturaen ville gått dit). Er adressen ikke komplett, er personen merket «Privat adresse mangler/er ufullstendig»,
    # og fakturaen holdes tilbake (privatadresse.py).
    adresse_komplett = privatadresse.komplett([adresse[k] for k in skjemafelt.ADRESSEFELT])
    eksisterende = con().execute("SELECT adresse, postnr, poststed FROM deltaker WHERE epost=?", (epost_.strip().lower(),)).fetchone()
    lagre_delvis = (not adresse_komplett and any(adresse[k] for k in skjemafelt.ADRESSEFELT)
                    and not (eksisterende and any((eksisterende[k] or "").strip() for k in skjemafelt.ADRESSEFELT)))
    lagre_adresse = adresse_komplett or lagre_delvis
    try:
        with db.transaksjon(con()):
            pid, status = db.meld_paa(
                con(), kurs["id"], epost=epost_, fornavn=fornavn, etternavn=etternavn,
                deltaker={"telefon": _hent(data, "telefon"), "arbeidssted": _hent(data, "arbeidssted"),
                          "hpr_nr": _hent(data, "hpr_nr") if skjemafelt.vis_hpr(kurs) else None,
                          **(skjemafelt.rens_adresse(adresse) if lagre_adresse else dict.fromkeys(skjemafelt.ADRESSEFELT))},
                paamelding={"kilde": "nettside", "betaler": "organisasjon" if oppslag else "person",
                            **(firmaopplysninger.paamelding_felter(oppslag) if oppslag else {}), **fakturafelt},
                sensitivt={"allergier": _hent(data, "allergier") if skjemafelt.vis_sensitive_felt(kurs) else None},
                idag=_idag(),
            )
            if oppslag is not None and oppslag.utfall == firmaopplysninger.UTILGJENGELIG:
                firmaopplysninger.logg_ikke_hentet(con(), kurs["id"], pid)
    except db.Paameldingsfeil as e:
        if "allerede påmeldt" in str(e):
            return {"status": "ok", "melding": "allerede påmeldt"}, 200   # idempotent gjeninnsending
        return {"status": "feil", "melding": str(e)}, 409
    sveiper.kjor(Kjoring(con(), idag=_idag()), pid)
    con().commit()
    svar = {"status": "ok", "paamelding_id": pid, "paameldingsstatus": status}
    person = con().execute("SELECT d.adresse, d.postnr, d.poststed FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id "
                           "WHERE p.id=?", (pid,)).fetchone()
    if person and skjemafelt.adresse_mangler(person):        # bare mulig med nødbremsen (ADRESSE_KREVES_I_WEBHOOK=0)
        svar["merknad"] = ("registrert uten komplett privat adresse (nødbrems): en privat faktura holdes tilbake til alle tre feltene "
                           "(adresse, postnr, poststed) er lagt inn")
    return svar, 201


# ======================= KI-assistent =======================

@app.route("/sporsmal", methods=["GET", "POST"])
def sporsmal_side():
    if not config.ASSISTENT_AKTIV:
        abort(404)
    resultat, tekst = None, ""
    if request.method == "POST":
        from .. import assistent
        sikkerhet.krev_kvote("sporsmal")
        tekst = request.form.get("sporsmal", "")
        did = session.get("deltaker_id") if _deltaker_full() else None      # bare e-post-økten er identifisert nok til å svare om faktura og betaling
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
                             oppdatert=? WHERE id=?""", (*felter, db.naa_utc(), int(f["id"])))
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
        oppforinger=con().execute("SELECT k.*, ku.kursnr FROM kunnskap k LEFT JOIN kurs ku ON ku.id=k.kurs_id ORDER BY k.godkjent, k.kategori, k.id").fetchall(),
        henvendelser=con().execute("SELECT * FROM henvendelse WHERE status='til_adm' ORDER BY id DESC").fetchall(),
        siste=con().execute("SELECT * FROM henvendelse ORDER BY id DESC LIMIT 20").fetchall(),
        kurs=con().execute("SELECT id, kursnr, kode, navn FROM kurs ORDER BY id DESC").fetchall(),
        rediger=rediger, fra=fra_henvendelse)


@app.post("/admin/henvendelse/<int:hid>/lukk")
@krever_admin
def admin_lukk_henvendelse(hid):
    con().execute("UPDATE henvendelse SET status='lukket' WHERE id=?", (hid,))
    con().commit()
    return redirect(url_for("admin_kunnskap"))


kursside_admin_ruter.installer(app, con, krever_admin=krever_admin, admin_okt_gyldig=_admin_okt_gyldig, aktor=_aktor,
                               hent_kurs=_hent_kurs, idag=lambda: _idag())
deltakerside_ruter.installer(app, con, krever_deltaker=krever_deltaker, admin_okt_gyldig=_admin_okt_gyldig, idag=lambda: _idag(),
                             deltaker_full=_deltaker_full)
oppmoteliste_ruter.installer(app, con, krever_admin=krever_admin, krever_admin_json=krever_admin_json, aktor=_aktor,
                             hent_kurs=_hent_kurs, idag=lambda: _idag())
entra.installer(app, con, _logg_inn_admin)


def main(omstart: bool = True):
    """Lokal utviklingsserver (kjor.py / start.bat). I demo holdes databasen i takt automatisk; i prod brukes gunicorn
    (startup.py) og migrering er et eget steg - denne serveren nekter da aa starte mot en umigrert database."""
    c = db.koble()
    if config.DEMO:
        db.init(c)
    else:
        migreringer.kontroller(c)
    c.close()
    app.run(debug=config.DEMO, use_reloader=omstart and config.DEMO, host="127.0.0.1", port=5000)


if __name__ == "__main__":
    main()
