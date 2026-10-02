"""Rutene til oppmøtelisten (kursleders reserve når QR-koden ikke virker). Logikken ligger i kurs/oppmoteliste.py.

Kobles på appen med `installer(app, con, ...)` (samme mønster som kursside_admin_ruter). Alle endepunkter heter `admin_kurs_oppmote...`,
så «Oversikt» forblir markert i menyen.

  GET  /admin/kurs/<id>/oppmote                       til dagens kursdag (ellers den nærmeste)
  GET  /admin/kurs/<id>/oppmote/<dag>                 siden. Lesetilgang ser listen uten knapper.
  POST /admin/kurs/<id>/oppmote/<dag>/rad             én person: ønsket tilstand «til» eller «fra» (idempotent, ikke «bytt»)
  POST /admin/kurs/<id>/oppmote/<dag>/alle            «Merk alle til stede» og «Fjern alle merkede» (krever bekreft=1)
  GET  /admin/kurs/<id>/oppmote/<dag>/status          tilstanden uten navn, til automatisk oppdatering av siden

Alle POST har CSRF (skjemafelt eller hodet X-CSRF-Token, sjekket i sikkerhet.py) og krever kursadmin eller system (lese får 403 av
_har_rolle_for). Hver id kontrolleres mot kurset i adressen, så en kursdag eller påmelding fra et annet kurs gir 404. Fra siden
svares det med JSON (Accept: application/json); uten JavaScript sendes skjemaene vanlig, og svaret er en viderekobling tilbake til
listen med en melding. Adressene inneholder bare id-er (aldri navn eller søketekst), og loggen får aldri navn.
"""
from datetime import date
from functools import wraps

from flask import abort, flash, g, jsonify, redirect, render_template, request, url_for

from .. import db, deltakerside, oppmoteliste


def _vil_ha_json() -> bool:
    """Siden (fetch) ber om JSON. Vanlige skjemaer fra nettleseren ber om HTML."""
    return request.accept_mimetypes.best_match(["text/html", "application/json"]) == "application/json"


def _json(data: dict, status: int = 200):
    svar = jsonify(data)
    svar.status_code = status
    svar.headers["Cache-Control"] = "no-store"
    return svar


def installer(app, con, *, krever_admin, krever_admin_json, aktor, hent_kurs, idag) -> None:
    """`krever_admin`, `krever_admin_json`, `aktor` og `hent_kurs` kommer fra app.py; `idag()` gir dagens dato (demo kan spole)."""

    def krever(f):
        """Som krever_admin, men svarer med JSON (401/403) til fetch fra siden, så en utløpt økt aldri viser innloggingssiden i stedet
        for et svar. Vanlige skjemaer får den vanlige viderekoblingen til innloggingen."""
        vanlig, som_json = krever_admin(f), krever_admin_json(f)

        @wraps(f)
        def inner(*a, **kw):
            return (som_json if _vil_ha_json() else vanlig)(*a, **kw)
        return inner

    def kurs_og_dag(kurs_id: int, dag_id: int):
        kurs = hent_kurs(kurs_id)                                  # 404 ved ukjent kurs
        dag = oppmoteliste.hent_dag(con(), kurs_id, dag_id)
        if dag is None:                                            # ukjent dag, eller en dag som hører til et annet kurs
            abort(404)
        return kurs, dag

    def til_listen(kurs_id: int, dag_id: int, **spor):
        return redirect(url_for("admin_kurs_oppmote", kurs_id=kurs_id, dag_id=dag_id, **spor))

    def avvis(melding: str, status: int, kurs_id: int, dag_id: int, *, kode: str = "feil"):
        """JSON-svar til siden; en melding og tilbake til listen for et vanlig skjema."""
        if _vil_ha_json():
            return _json({"status": kode, "melding": melding}, status)
        flash(melding, "feil")
        return til_listen(kurs_id, dag_id)

    # ------------------------------------------------------------------ siden

    @app.get("/admin/kurs/<int:kurs_id>/oppmote", endpoint="admin_kurs_oppmote_standard")
    @krever_admin
    def oppmote_standard(kurs_id):
        """Åpner oppmøtelisten på dagens kursdag, ellers den nærmeste."""
        hent_kurs(kurs_id)
        dag = oppmoteliste.standarddag(db.kursdager(con(), kurs_id), idag())
        if dag is None:
            flash("Kurset har ingen kursdager ennå, så det er ikke noe oppmøte å ta opp.", "info")
            return redirect(url_for("admin_kurs_oppsett", kurs_id=kurs_id))
        return til_listen(kurs_id, dag["id"])

    @app.get("/admin/kurs/<int:kurs_id>/oppmote/<int:dag_id>", endpoint="admin_kurs_oppmote")
    @krever_admin
    def oppmote_side(kurs_id, dag_id):
        kurs, dag = kurs_og_dag(kurs_id, dag_id)
        i_dag = idag()
        dager = db.kursdager(con(), kurs_id)
        dato = date.fromisoformat(dag["dato"])
        tillatt = oppmoteliste.kan_ta_opp(dag, i_dag)
        alle = [oppmoteliste.rad_for_visning(r) for r in oppmoteliste.deltakere_for_dag(con(), kurs_id, dag_id)]
        rader = alle if tillatt else []          # en kursdag som ikke er startet: bare forklaringen, ingen liste og ingen knapper
        kan_endre = g.get("admin_rolle") != "lese" and tillatt
        nr, av = deltakerside.dagnummer(con(), kurs_id, dag_id)
        return render_template(
            "admin_oppmoteliste.html", kurs=kurs, dag=dag, dato_lang=deltakerside.dato_lang(dato, aar=True), dag_nr=nr, dag_av=av,
            faner=oppmoteliste.dagfaner(dager, dag_id, i_dag) if len(dager) > 1 else [], rader=rader, totalt=len(alle),
            antall=sum(r["til_stede"] for r in rader), tillatt=tillatt, kan_endre=kan_endre, er_idag=dato == i_dag,
            panel=_bekreftelsespanel(kurs_id, dag_id, rader) if kan_endre else None, fane="deltakere")

    def _bekreftelsespanel(kurs_id: int, dag_id: int, rader: list[dict]) -> dict | None:
        """Bekreftelsen uten JavaScript: knappene på siden er lenker hit (?bekreft=...), og skjemaet her sender selve handlingen. Bare
        kjente verdier gjelder; alt annet gir ingen panel. Ingenting skrives før skjemaet sendes."""
        valg = request.args.get("bekreft")
        if valg not in ("merk_alle", "fjern_alle", "zoom"):
            return None
        alle_url = url_for("admin_kurs_oppmote_alle", kurs_id=kurs_id, dag_id=dag_id)
        if valg == "zoom":
            pid = request.args.get("p", type=int)
            r = next((r for r in rader if r["paamelding_id"] == pid and r["kilde"] == "zoom"), None)
            if r is None:
                return None
            return {"id": "zoom", "tittel": "Fjerne oppmøte som er hentet fra Zoom?", "knapp": "Ja, fjern oppmøtet", "fare": True,
                    "tekst": f"Oppmøtet for {r['navn']} er registrert fra Zoom. Fjerner du det, teller ikke dagen som oppmøte "
                             "(det kan påvirke kursbeviset). Vil du fjerne det likevel?",
                    "action": url_for("admin_kurs_oppmote_rad", kurs_id=kurs_id, dag_id=dag_id),
                    "felt": [("paamelding_id", pid), ("onsket", "fra"), ("bekreft_zoom", "1")], "kan_sendes": True}
        if valg == "merk_alle":
            n = sum(not r["til_stede"] for r in rader)
            tekst = (f"{n} {'deltaker' if n == 1 else 'deltakere'} som ikke er registrert ennå, blir registrert som til stede (manuelt). "
                     "De som allerede er registrert, endres ikke." if n else "Alle er allerede registrert som til stede.")
            return {"id": "merk_alle", "tittel": "Merke alle som til stede?", "knapp": "Ja, merk alle", "fare": False, "tekst": tekst,
                    "action": alle_url, "felt": [("handling", "merk_alle"), ("bekreft", "1")], "kan_sendes": n > 0}
        n = sum(r["til_stede"] and r["kilde"] != "zoom" for r in rader)
        m = sum(r["kilde"] == "zoom" for r in rader)
        tekst = (f"Oppmøtet fjernes for {n} {'deltaker' if n == 1 else 'deltakere'}." if n else "Ingen registreringer å fjerne.")
        if m:
            tekst += f" {m} {'registrering' if m == 1 else 'registreringer'} fra Zoom beholdes (de fjernes én og én)."
        return {"id": "fjern_alle", "tittel": "Fjerne alle registreringene?", "knapp": "Ja, fjern alle", "fare": True, "tekst": tekst,
                "action": alle_url, "felt": [("handling", "fjern_alle"), ("bekreft", "1")], "kan_sendes": n > 0}

    @app.get("/admin/kurs/<int:kurs_id>/oppmote/<int:dag_id>/status", endpoint="admin_kurs_oppmote_status")
    @krever
    def oppmote_status(kurs_id, dag_id):
        """Tilstanden til listen uten navn og uten noen personopplysninger: antall, totalt og per påmeldings-id."""
        kurs_og_dag(kurs_id, dag_id)
        return _json({"status": "ok", **oppmoteliste.tilstand(con(), kurs_id, dag_id)})

    # ------------------------------------------------------------------ handlinger

    def _ikke_startet(kurs_id: int, dag, dag_id: int):
        """Svaret når kursdagen ikke kan tas opp ennå (fremtidig dato), ellers None."""
        if oppmoteliste.kan_ta_opp(dag, idag()):
            return None
        return avvis("Kursdagen er ikke startet ennå. Oppmøte kan bare tas opp fra og med kursdagen.", 409, kurs_id, dag_id)

    @app.post("/admin/kurs/<int:kurs_id>/oppmote/<int:dag_id>/rad", endpoint="admin_kurs_oppmote_rad")
    @krever
    def oppmote_rad(kurs_id, dag_id):
        """Setter oppmøtet for én person til det som ønskes («til» eller «fra»). Å sende det samme to ganger gir samme resultat."""
        _kurs, dag = kurs_og_dag(kurs_id, dag_id)
        pid, onsket = request.form.get("paamelding_id", type=int), request.form.get("onsket")
        if pid is None or onsket not in ("til", "fra"):
            return avvis("Ugyldig forespørsel.", 400, kurs_id, dag_id)
        if not 0 < pid < 2**63:                                     # for stort til å være en id: som om den ikke fantes (ikke feil 500)
            abort(404)
        hoerer_til = con().execute("SELECT 1 FROM paamelding WHERE id=? AND kurs_id=?", (pid, kurs_id)).fetchone()
        if not hoerer_til:                                          # en påmelding fra et annet kurs: som om den ikke fantes
            abort(404)
        if (stopp := _ikke_startet(kurs_id, dag, dag_id)) is not None:
            return stopp
        utfall = oppmoteliste.sett_oppmote(con(), kurs_id, dag_id, pid, onsket == "til", aktor(),
                                           bekreft_zoom=request.form.get("bekreft_zoom") == "1")
        if utfall.status == "ikke_i_listen":
            return avvis("Personen har ikke plass på kurset lenger. Last siden på nytt for å se den siste listen.", 409, kurs_id, dag_id)
        if utfall.status == "trenger_zoom_bekreftelse":
            if _vil_ha_json():
                return _json({"status": "trenger_bekreftelse",
                              "melding": "Oppmøtet er registrert fra Zoom. Fjern det bare hvis du er sikker."}, 409)
            return redirect(url_for("admin_kurs_oppmote", kurs_id=kurs_id, dag_id=dag_id, bekreft="zoom", p=pid) + "#bekreft")
        con().commit()
        tilstand = oppmoteliste.tilstand(con(), kurs_id, dag_id)
        if _vil_ha_json():
            return _json({"status": "ok", "paamelding_id": pid, "endret": utfall.status == "endret",
                          **tilstand["rader"][str(pid)], "antall": tilstand["antall"], "totalt": tilstand["totalt"]})
        return redirect(url_for("admin_kurs_oppmote", kurs_id=kurs_id, dag_id=dag_id) + f"#rad-{pid}")

    @app.post("/admin/kurs/<int:kurs_id>/oppmote/<int:dag_id>/alle", endpoint="admin_kurs_oppmote_alle")
    @krever
    def oppmote_alle(kurs_id, dag_id):
        """«Merk alle til stede» (handling=merk_alle) og «Fjern alle merkede» (handling=fjern_alle). Krever bekreft=1: uten den skjer
        ingenting (siden og bekreftelsessiden uten JavaScript sender den først når kursleder har bekreftet)."""
        _kurs, dag = kurs_og_dag(kurs_id, dag_id)
        handling = request.form.get("handling")
        if handling not in ("merk_alle", "fjern_alle"):
            return avvis("Ugyldig forespørsel.", 400, kurs_id, dag_id)
        if (stopp := _ikke_startet(kurs_id, dag, dag_id)) is not None:
            return stopp
        if request.form.get("bekreft") != "1":
            if _vil_ha_json():
                return _json({"status": "trenger_bekreftelse", "melding": "Handlingen må bekreftes først."}, 409)
            return redirect(url_for("admin_kurs_oppmote", kurs_id=kurs_id, dag_id=dag_id, bekreft=handling) + "#bekreft")
        if handling == "merk_alle":
            n = oppmoteliste.merk_alle(con(), kurs_id, dag_id, aktor())
            tekst = f"{n} {'deltaker' if n == 1 else 'deltakere'} merket som til stede." if n else "Alle var allerede registrert."
        else:
            n, m = oppmoteliste.fjern_alle(con(), kurs_id, dag_id, aktor())
            tekst = (f"Oppmøtet er fjernet for {n} {'deltaker' if n == 1 else 'deltakere'}." if n else "Ingen registreringer å fjerne.")
            if m:
                tekst += f" {m} fra Zoom er beholdt."
        con().commit()
        if _vil_ha_json():
            return _json({"status": "ok", "melding": tekst, "endret": n, **oppmoteliste.tilstand(con(), kurs_id, dag_id)})
        flash(tekst, "ok")
        return til_listen(kurs_id, dag_id)
