"""Webapp: paamelding, QR-innsjekk, Min side og admin.

    python -m kurs.web.app        # http://127.0.0.1:5000
"""
import csv
import io
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps

import qrcode
import qrcode.image.svg
from flask import (Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for)
from markupsafe import Markup

from .. import config, daglig, db, sveiper
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

@app.get("/")
def forside():
    kurs = con().execute(
        """SELECT k.*, MIN(kd.dato) AS start, COUNT(kd.id) AS dager FROM kurs k JOIN kursdag kd ON kd.kurs_id=k.id
           WHERE k.status IN ('aapen','full') GROUP BY k.id ORDER BY start""").fetchall()
    return render_template("forside.html", kurs=kurs)


@app.route("/kurs/<kode>", methods=["GET", "POST"])
def kursside(kode):
    kurs = con().execute("SELECT * FROM kurs WHERE kode=?", (kode,)).fetchone() or abort(404)
    dager = db.kursdager(con(), kurs["id"])
    if request.method == "GET":
        return render_template("kurs.html", kurs=kurs, dager=dager, f={},
                               plasser_igjen=None if kurs["kapasitet"] is None
                               else kurs["kapasitet"] - db.antall_bekreftet(con(), kurs["id"]))
    f = request.form
    feil = []
    if not f.get("navn") or "@" not in f.get("epost", ""):
        feil.append("Fyll inn navn og gyldig e-post.")
    if not f.get("samtykke"):
        feil.append("Du må godta vilkår og personvernerklæring.")
    org = f.get("betaler") == "organisasjon"
    if org and not (f.get("org_navn") and f.get("org_nr")):
        feil.append("Fyll inn organisasjon og org.nr. når arbeidsgiver betaler.")
    if feil:
        for x in feil:
            flash(x, "feil")
        return render_template("kurs.html", kurs=kurs, dager=dager, f=f, plasser_igjen=None), 400
    try:
        with db.transaksjon(con()):
            pid, status = db.meld_paa(
                con(), kurs["id"], epost=f["epost"], navn=f["navn"],
                deltaker={"telefon": f.get("telefon"), "arbeidssted": f.get("arbeidssted"), "hpr_nr": f.get("hpr_nr")},
                paamelding={k: f.get(k) or None for k in (
                    "org_navn", "org_nr", "faktura_epost", "faktura_ref", "faktura_adresse", "faktura_postnr", "faktura_sted")}
                | {"betaler": "organisasjon" if org else "person", "ehf": 1 if f.get("ehf") else 0,
                   "betaling": f.get("betaling", "samlet")},
                sensitivt={"allergier": f.get("allergier"), "tilrettelegging": f.get("tilrettelegging")},
            )
    except db.Paameldingsfeil as e:
        flash(str(e), "feil")
        return render_template("kurs.html", kurs=kurs, dager=dager, f=f, plasser_igjen=None), 400
    # "Webhook": kjor sveipene med en gang (bekreftelse + faktura). Daglig jobb tar det som evt. feiler.
    sveiper.kjor(Kjoring(con(), idag=_idag()), pid)
    con().commit()
    return render_template("kvittering.html", kurs=kurs, status=status, navn=f["navn"])


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
    feil = con().execute("SELECT COUNT(*) FROM hendelse WHERE handling='sveip_feil'").fetchone()[0]
    return render_template("admin.html", kurs=kurs, mangler=mangler, hendelser=hendelser, feil=feil)


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
    sorter = request.args.get("sorter") or ""
    sorter = sorter if sorter in AKTIVITET_SORTER else None
    retning = "desc" if request.args.get("retning") == "desc" else "asc"
    antall = request.args.get("antall", type=int) or 25
    antall = antall if antall in (10, 25, 50, 100) else 25
    side = max(1, request.args.get("side", type=int) or 1)

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
              WHERE (? = '' OR LOWER(k.navn) LIKE ?) AND (? = 0 OR k.ansvarlig_admin_id = ?)
              GROUP BY k.id ORDER BY {order_sql}"""
    rader = con().execute(sql, (sok, f"%{sok.lower()}%", 1 if mine else 0, session.get("admin_id"))).fetchall()

    totalt = len(rader)
    siste_side = max(1, -(-totalt // antall))
    side = min(side, siste_side)
    start_i = (side - 1) * antall
    kurs = rader[start_i:start_i + antall]

    def sidelenke(n):
        return url_for("admin_aktiviteter", sok=sok or None, mine=1 if mine else None,
                       antall=antall, sorter=sorter, retning=retning, side=n)

    def sorterlenke(felt, tekst):
        ny_retning = "desc" if sorter == felt and retning == "asc" else "asc"
        pil = (" ↑" if retning == "asc" else " ↓") if sorter == felt else ""
        lenke = url_for("admin_aktiviteter", sok=sok or None, mine=1 if mine else None,
                        antall=antall, sorter=felt, retning=ny_retning)
        return Markup(f'<a href="{lenke}">{Markup.escape(tekst)}{pil}</a>')

    return render_template("admin_aktiviteter.html", kurs=kurs, sok=sok, mine=mine, antall=antall,
                           side=side, totalt=totalt, siste_side=siste_side,
                           sidelenke=sidelenke, sorterlenke=sorterlenke)


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
    return render_template("admin_kurs_oppsett.html", kurs=kurs, dager=dager, filer=filer, admins=admins,
                           oppmote_antall=oppmote_antall, bekreftet_antall=db.antall_bekreftet(con(), kurs_id),
                           fane="oppsett")


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


@app.get("/admin/kurs/<int:kurs_id>/deltakere")
@krever_admin
def admin_kurs_deltakere(kurs_id):
    kurs = _hent_kurs(kurs_id)
    dager = db.kursdager(con(), kurs_id)
    sok = request.args.get("sok", "").strip()
    status = request.args.get("status", "")
    if status not in ("", "bekreftet", "venteliste", "avmeldt"):
        status = ""

    sql = """SELECT p.*, d.navn, d.epost, d.telefon, d.arbeidssted,
                    (SELECT COUNT(*) FROM faktura WHERE paamelding_id=p.id) AS faktura_antall,
                    (SELECT COUNT(*) FROM faktura WHERE paamelding_id=p.id AND status!='betalt') AS faktura_ubetalt
             FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id
             WHERE p.kurs_id=?"""
    args = [kurs_id]
    if sok:
        sql += " AND (LOWER(d.navn) LIKE ? OR LOWER(d.epost) LIKE ?)"
        args += [f"%{sok.lower()}%", f"%{sok.lower()}%"]
    if status:
        sql += " AND p.status=?"
        args.append(status)
    sql += " ORDER BY CASE p.status WHEN 'bekreftet' THEN 0 WHEN 'venteliste' THEN 1 ELSE 2 END, d.navn"
    deltakere = [dict(r, fakturastatus=_fakturastatus(kurs, r)) for r in con().execute(sql, args)]

    tellere = {r["status"]: r["antall"] for r in con().execute(
        "SELECT status, COUNT(*) AS antall FROM paamelding WHERE kurs_id=? GROUP BY status", (kurs_id,))}
    oppmote = {(r["paamelding_id"], r["kursdag_id"]): r["kilde"] for r in con().execute(
        "SELECT o.* FROM oppmote o JOIN kursdag kd ON kd.id=o.kursdag_id WHERE kd.kurs_id=?", (kurs_id,))}
    return render_template("admin_kurs_deltakere.html", kurs=kurs, dager=dager, deltakere=deltakere,
                           tellere=tellere, totalt=sum(tellere.values()), oppmote=oppmote,
                           sok=sok, status=status, fane="deltakere")


@app.get("/admin/kurs/<int:kurs_id>/kommunikasjon")
@krever_admin
def admin_kurs_kommunikasjon(kurs_id):
    kurs = _hent_kurs(kurs_id)
    meldinger = con().execute(
        "SELECT * FROM utsending_logg WHERE nokkel=? ORDER BY sendt_ts DESC LIMIT 300", (f"kurs:{kurs_id}",)
    ).fetchall()
    return render_template("admin_kurs_kommunikasjon.html", kurs=kurs, meldinger=meldinger, fane="kommunikasjon")


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


@app.get("/admin/kurs/<int:kurs_id>/deltakere.csv")
@krever_admin
def admin_csv(kurs_id):
    rader = con().execute(
        """SELECT d.navn, d.epost, d.telefon, d.arbeidssted, p.status, p.betaler, p.betaling, p.org_navn, p.org_nr,
                  (SELECT GROUP_CONCAT(faktura_nr, ' ') FROM faktura WHERE paamelding_id=p.id) AS faktura_nr,
                  (SELECT COALESCE(SUM(belop_nok),0) FROM faktura WHERE paamelding_id=p.id) AS fakturert_belop
           FROM paamelding p JOIN deltaker d ON d.id=p.deltaker_id
           WHERE p.kurs_id=? ORDER BY p.status, d.navn""", (kurs_id,)).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Navn", "E-post", "Telefon", "Arbeidssted", "Status", "Betaler", "Betaling", "Organisasjon", "Org.nr",
                "Fakturanr", "Fakturert (kr)"])
    w.writerows([tuple(r) for r in rader])
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",  # BOM -> Excel leser æøå riktig
                    headers={"Content-Disposition": f"attachment; filename=deltakere_{kurs_id}.csv"})


@app.route("/admin/kurs/ny", methods=["GET", "POST"])
@krever_admin
def admin_ny_kurs():
    if request.method == "POST":
        f = request.form
        datoer = sorted({d.strip() for d in f.get("datoer", "").replace(",", "\n").splitlines() if d.strip()})
        try:
            for d in datoer:
                date.fromisoformat(d)
            with db.transaksjon(con()):
                kode = db.generer_kode(con(), f["navn"].strip(), datoer)
                kid = db.opprett_kurs(
                    con(), kode=kode, navn=f["navn"].strip(), datoer=datoer, aktor=_aktor(),
                    type=f["type"], sted=f.get("sted") or None, start_kl=f["start_kl"], slutt_kl=f["slutt_kl"],
                    timer_pr_dag=float(f.get("timer_pr_dag") or 6), kapasitet=int(f["kapasitet"]) if f.get("kapasitet") else None,
                    pris_nok=int(f.get("pris_nok") or 0), fakturering=f["fakturering"],
                    betaling=f.get("betaling", "samlet"), faktura_dager_for=int(f.get("faktura_dager_for") or 14),
                    spesialistlop=f.get("spesialistlop") or None, kursholder_epost=f.get("kursholder_epost") or None,
                    notat=f.get("notat") or None,
                    sharepoint_mappe=sharepoint.opprett_kursmappe(kode),
                )
                if f.get("kursholder_epost") and f.get("materiell_frist"):
                    con().execute("INSERT INTO materiell_krav (kurs_id, ansvarlig_navn, ansvarlig_epost, frist) VALUES (?,?,?,?)",
                                  (kid, f.get("kursholder_navn") or f["kursholder_epost"], f["kursholder_epost"], f["materiell_frist"]))
            flash(f"Kurset er opprettet med koden «{kode}», og har fått påmeldingsside, SharePoint-mappe og innsjekkkoder.", "ok")
            return redirect(url_for("admin_kurs_oppsett", kurs_id=kid))
        except (ValueError, KeyError) as e:
            flash(f"Kunne ikke opprette kurs: {e}", "feil")
    return render_template("admin_ny_kurs.html")


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
