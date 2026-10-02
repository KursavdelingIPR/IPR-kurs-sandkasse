"""Kursside i administrasjonen: redigering, publisering, filer, forhåndsvisning (SPEC §5.1).

Kobles på appen med `installer(app, con, ...)` (samme mønster som entra.installer): `con` er funksjonen som gir databasetilkoblingen for
forespørselen, ikke en tilkobling. Alle endepunkter starter med `admin_kursside`, så «Oversikt» forblir markert i menyen.

Alle POST har CSRF (skjemafelt eller hodet X-CSRF-Token, sjekket i sikkerhet.py) og krever rollen kursadmin eller system (lese får 403
av _har_rolle_for). JSON-svar har `{"status": "ok"|"feil"|"konflikt"|"i_bruk"|"ingen_side", ...}`. Siden lagres alltid som ett helt
dokument med versjonskontroll: to som redigerer samtidig får en tydelig konflikt (409), aldri en stille overskriving.
Redigeringsvisningen (kursside_admin.html) er foreløpig en enkel, fungerende utgave; utseendet lages i et eget byggetrinn.
"""
import re
from urllib.parse import unquote

from flask import Response, abort, g, jsonify, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException

from .. import config, db, deltakerside, minside, norsk_tid, sidelager
from .. import sideinnhold as si
from ..sideinnhold import Sidefeil
from . import filrespons, sikkerhet

_MB = 1024 * 1024
_ID = 987654321          # plassholder når adresser til enkeltfiler og versjoner bygges (byttes ut med {id})


class _Konflikt(Exception):
    """Noen andre har lagret siden mens du redigerte (versjonen stemmer ikke lenger)."""


def _svar(data: dict, status: int = 200) -> Response:
    r = jsonify(data)
    r.status_code = status
    return r


def _feil(melding: str, status: int = 422, **ekstra) -> Response:
    return _svar({"status": "feil", "melding": melding, **ekstra}, status)


def _heltall(verdi, minst: int = 0) -> int | None:
    """Et heltall fra `minst` til største gyldige id. Enorme tall ga OverflowError mot databasen (500)."""
    if isinstance(verdi, bool) or not isinstance(verdi, int) or verdi < minst or verdi > si.MAKS_ID:
        return None
    return verdi


def _tid(utc: str | None) -> str:
    """UTC-tidspunkt fra databasen -> «14:32» i norsk tid."""
    return norsk_tid.klokkeslett(norsk_tid.fra_utc(utc)) if utc else ""


def _dato_tid(utc: str | None) -> str:
    tid = norsk_tid.fra_utc(utc) if utc else None
    return f"{norsk_tid.dato(tid)} {norsk_tid.klokkeslett(tid)}" if tid else ""


def installer(app, con, *, krever_admin, admin_okt_gyldig, aktor, hent_kurs, idag) -> None:
    """Registrerer rutene. `krever_admin`, `aktor` og `hent_kurs` kommer fra app.py; `idag()` gir dagens dato (demo kan spole)."""

    # ------------------------------------------------------------------ hjelpere
    def _navn(aktor_tekst: str | None) -> str:
        """«admin:marte» -> «Marte Solberg» (ellers brukernavnet)."""
        if not aktor_tekst:
            return ""
        brukernavn = aktor_tekst.split(":", 1)[-1]
        rad = con().execute("SELECT navn FROM admin_bruker WHERE brukernavn=?", (brukernavn,)).fetchone()
        return rad["navn"] if rad else brukernavn

    def _kropp(maks: int = si.MAKS_KROPP_BYTES):
        """(json-objekt, feilsvar). Kroppen må ha lengde og være høyst `maks` byte; den leses aldri som skjema."""
        lengde = request.content_length
        if lengde is None:
            return None, _feil("Forespørselen mangler lengde.", 411)
        if lengde > maks:
            return None, _feil("Siden er for stor til å lagres.", 413)
        try:
            data = request.get_json(silent=True)
        except RecursionError:            # dypt nestet JSON («[[[[ ...»): silent=True fanger bare ValueError
            data = None
        if not isinstance(data, dict):
            return None, _feil("Ugyldig forespørsel.", 400)
        return data, None

    def _ingen_side() -> Response:
        return _svar({"status": "ingen_side", "melding": "Siden er ikke lagret ennå. Lagre den først."}, 409)

    def _konflikt(kurs_id: int, mitt: dict | None = None) -> Response:
        """409 med serverens siste utkast og en oversikt over hva som skiller, så ingenting går tapt."""
        siste, versjon = sidelager.hent_utkast(con(), kurs_id)
        side = sidelager.hent(con(), kurs_id)
        return _svar({"status": "konflikt", "versjon": versjon, "endret_av": _navn(side["endret_av"]) if side else "",
                      "endret": _tid(side["endret"]) if side else "",
                      "diff": si.diff(mitt, siste) if mitt is not None else [], "dokument": siste}, 409)

    def _kontroll(kurs_id: int, kurs, dok: dict):
        filer = {f["id"]: {"type": f["type"], "filnavn": f["filnavn"]} for f in sidelager.fil_liste(con(), kurs_id)}
        return si.publiseringssjekk(dok, filer=filer, bekreftede_epost=sidelager.bekreftede_epost(con(), kurs_id),
                                    kursdager=deltakerside.kursdager_for(con(), kurs), idag=idag())

    def _urls(kurs_id: int) -> dict:
        def u(endepunkt, **kw):
            return url_for(endepunkt, kurs_id=kurs_id, **kw)
        return {"side": u("admin_kursside"), "lagre": u("admin_kursside_lagre"), "redigerer": u("admin_kursside_redigerer"),
                "sjekk": u("admin_kursside_sjekk"), "publiser": u("admin_kursside_publiser"), "aktiv": u("admin_kursside_aktiv"),
                "innstillinger": u("admin_kursside_innstillinger"),
                "forhandsvis": u("admin_kursside_forhandsvis"), "fil_opp": u("admin_kursside_fil_opp"),
                "fil": u("admin_kursside_fil", fil_id=_ID).replace(str(_ID), "{id}"),
                "fil_slett": u("admin_kursside_fil_slett", fil_id=_ID).replace(str(_ID), "{id}"),
                "gjenopprett": u("admin_kursside_gjenopprett", versjon_id=_ID).replace(str(_ID), "{id}"),
                "kilder": u("admin_kursside_kilder"), "hent_fra": u("admin_kursside_hent_fra"), "mal": u("admin_kursside_mal"),
                "deltakernavn": u("admin_kursside_deltakernavn"), "token": u("admin_kursside_token"),
                "logg_inn": url_for("admin_login")}

    def _versjonsliste(kurs_id: int) -> list[dict]:
        return [{"id": v["id"], "arsak": v["arsak"], "merknad": v["merknad"], "versjon": v["versjon"],
                 "opprettet": _dato_tid(v["opprettet"]), "opprettet_av": _navn(v["opprettet_av"])}
                for v in sidelager.versjoner(con(), kurs_id)]

    def _apningstid_data(kurs_id: int, side) -> dict:
        """Åpningstiden til redigeringsvisningen: lagret dato («stenges») eller dager («apen_dager»: None = standard, 0 = ingen tidsbegrensning),
        standardverdien, grensene, siste kursdag og den utregnede siste åpne dagen. Bare tall og datoer (aldri persondata)."""
        a = sidelager.apningstid(con(), kurs_id, side)
        return {"stenges": a["stenges"], "apen_dager": a["apen_dager"], "standard_dager": a["standard_dager"],
                "apen_dager_min": sidelager.APEN_DAGER_MIN, "apen_dager_maks": sidelager.APEN_DAGER_MAKS,
                "siste_kursdag": a["siste_kursdag"], "stenges_effektiv": a["stenges_effektiv"]}

    def _sidedata(kurs) -> dict:
        """Alt redigeringsvisningen trenger (SPEC §6.12), som JSON i siden. Skriver aldri."""
        kurs_id = kurs["id"]
        side = sidelager.hent(con(), kurs_id)
        utkast, versjon = sidelager.hent_utkast(con(), kurs_id)
        publisert = si.les(side["publisert"]) if side and side["publisert"] else None
        ansvarlig = None
        if kurs["ansvarlig_admin_id"]:
            a = con().execute("SELECT navn, epost FROM admin_bruker WHERE id=?", (kurs["ansvarlig_admin_id"],)).fetchone()
            ansvarlig = {"navn": a["navn"], "epost": a["epost"]} if a else None
        return {
            "kurs": {"id": kurs_id, "navn": kurs["navn"], "kode": kurs["kode"], "type": kurs["type"], "sted": kurs["sted"],
                     "har_zoom": bool(kurs["zoom_url"]), "status": kurs["status"]},
            "kursdager": deltakerside.kursdager_for(con(), kurs),
            "dokument": utkast, "publisert": publisert, "versjon": versjon,
            "publisert_versjon": int(side["publisert_versjon"]) if side and side["publisert_versjon"] is not None else None,
            "publisert_tid": _dato_tid(side["publisert_tid"]) if side and side["publisert_tid"] else None,
            "aktiv": bool(side["aktiv"]) if side else True,
            "innstillinger": {"innsjekk_krever_kode": bool(side["innsjekk_krever_kode"]) if side else True,
                              **_apningstid_data(kurs_id, side)},
            "filer": sidelager.fil_liste(con(), kurs_id), "kvote": sidelager.kvote(con(), kurs_id),
            "versjoner": _versjonsliste(kurs_id),
            "urls": _urls(kurs_id),
            "lenker": {"direkte": f"{config.BASE_URL}/logg-inn?neste=/kurs/{kurs['kode']}/deltakerside",
                       "generell": f"{config.BASE_URL}/logg-inn"},
            "ansvarlig": ansvarlig, "antall_deltakere": sidelager.antall_bekreftede(con(), kurs_id),
            "innsjekk_info": {"type": kurs["type"], "zoom_import": bool(kurs["zoom_id"])},          # til teksten om innsjekk på nett (ingen personopplysninger)
            "rolle": g.get("admin_rolle"), "redigerer": sidelager.hent_redigerer(con(), kurs_id, aktor()),
            "grenser": {**si.GRENSER, "fil_mb": config.KURSSIDE_FIL_MAKS_MB, "bilde_mb": config.KURSSIDE_BILDE_MAKS_MB,
                        "kurs_mb": config.KURSSIDE_KURS_MAKS_MB, "filer": config.KURSSIDE_MAKS_FILER,
                        "endelser": list(sidelager.TILLATTE_ENDELSER)},
        }

    # ------------------------------------------------------------------ siden
    @app.get("/admin/kurs/<int:kurs_id>/kursside")
    @krever_admin
    def admin_kursside(kurs_id):
        kurs = hent_kurs(kurs_id)
        return render_template("kursside_admin.html", kurs=kurs, fane="kursside", data=_sidedata(kurs),
                               skrivebeskyttet=g.get("admin_rolle") == "lese",
                               tom_dager=config.KURSSIDE_TOM_TABELLER_ETTER_DAGER)          # teksten om når lister med navn tømmes: samme tall som morgenjobben bruker

    @app.post("/admin/kurs/<int:kurs_id>/kursside/lagre")
    @krever_admin
    def admin_kursside_lagre(kurs_id):
        hent_kurs(kurs_id)
        data, feil = _kropp()
        if feil:
            return feil
        versjon = _heltall(data.get("versjon"))
        if versjon is None or not isinstance(data.get("dokument"), dict):
            return _feil("Ugyldig forespørsel.", 400)
        filer, dager = sidelager.valideringsgrunnlag(con(), kurs_id)
        try:
            dok, merknader = si.valider(data["dokument"], fil_ider=filer, kursdag_ider=dager)
        except Sidefeil as e:
            return _feil(e.melding, e.status)
        ny = sidelager.lagre_utkast(con(), kurs_id, dok, versjon, aktor())
        if ny is None:
            con().rollback()
            return _konflikt(kurs_id, dok)
        con().commit()
        publisert = sidelager.hent_publisert(con(), kurs_id)
        return _svar({"status": "ok", "versjon": ny, "endret": _tid(db.naa_utc()), "merknader": merknader,
                      "endringer": len(si.diff(publisert, dok)) if publisert is not None else None})

    @app.post("/admin/kurs/<int:kurs_id>/kursside/redigerer")
    @krever_admin
    def admin_kursside_redigerer(kurs_id):
        hent_kurs(kurs_id)
        side = sidelager.hent(con(), kurs_id)
        if not side:
            return _svar({"status": "ok", "versjon": 0, "annen": None})
        annen = sidelager.hent_redigerer(con(), kurs_id, aktor())
        sidelager.sett_redigerer(con(), kurs_id, aktor())
        con().commit()
        return _svar({"status": "ok", "versjon": int(side["versjon"]), "annen": annen})

    @app.get("/admin/kurs/<int:kurs_id>/kursside/sjekk")
    @krever_admin
    def admin_kursside_sjekk(kurs_id):
        kurs = hent_kurs(kurs_id)
        side = sidelager.hent(con(), kurs_id)
        if not side:
            return _ingen_side()
        utkast = si.les(side["utkast"])
        publisert = si.les(side["publisert"]) if side["publisert"] else None
        feil, advarsler = _kontroll(kurs_id, kurs, utkast)
        forrige = norsk_tid.fra_utc(side["publisert_tid"]) if side["publisert_tid"] else None
        return _svar({"status": "ok", "versjon": int(side["versjon"]), "diff": si.diff(publisert, utkast), "feil": feil,
                      "advarsler": advarsler, "antall_deltakere": sidelager.antall_bekreftede(con(), kurs_id),
                      "ingen_endringer": publisert is not None and side["publisert_versjon"] == side["versjon"],
                      "forrige_publisert": deltakerside._sist_oppdatert(side["publisert_tid"]) if forrige else None})

    @app.post("/admin/kurs/<int:kurs_id>/kursside/publiser")
    @krever_admin
    def admin_kursside_publiser(kurs_id):
        kurs = hent_kurs(kurs_id)
        data, feil = _kropp()
        if feil:
            return feil
        versjon = _heltall(data.get("versjon"), 1)
        if versjon is None:
            return _feil("Ugyldig forespørsel.", 400)
        side = sidelager.hent(con(), kurs_id)
        if not side:
            return _ingen_side()
        if int(side["versjon"]) != versjon:
            return _konflikt(kurs_id)
        dok = si.les(side["utkast"])
        feil_liste, _advarsler = _kontroll(kurs_id, kurs, dok)
        if feil_liste:
            return _svar({"status": "feil", "melding": "Siden kan ikke publiseres før feilene er rettet.", "feil": feil_liste}, 422)
        if side["publisert"] and side["publisert_versjon"] == versjon:
            return _svar({"status": "ok", "publisert_versjon": versjon, "ingen_endringer": True, "versjoner": _versjonsliste(kurs_id)})
        with db.transaksjon(con()):
            publisert_versjon = sidelager.publiser(con(), kurs_id, versjon, aktor())
            if publisert_versjon is not None:
                db.logg(con(), "kursside_publisert", {"kurs_id": kurs_id, "versjon": publisert_versjon,
                                                      "antall_blokker": len(dok["blokker"])}, aktor=aktor())
        if publisert_versjon is None:
            return _konflikt(kurs_id)
        return _svar({"status": "ok", "publisert_versjon": publisert_versjon, "versjoner": _versjonsliste(kurs_id),
                      "publisert": _tid(db.naa_utc())})

    @app.post("/admin/kurs/<int:kurs_id>/kursside/aktiv")
    @krever_admin
    def admin_kursside_aktiv(kurs_id):
        hent_kurs(kurs_id)
        data, feil = _kropp(10_000)
        if feil:
            return feil
        if not isinstance(data.get("aktiv"), bool):
            return _feil("Ugyldig forespørsel.", 400)
        with db.transaksjon(con()):
            funnet = sidelager.sett_aktiv(con(), kurs_id, data["aktiv"], aktor())
            if funnet:
                db.logg(con(), "kursside_aktiv", {"kurs_id": kurs_id, "aktiv": data["aktiv"]}, aktor=aktor())
        return _svar({"status": "ok", "aktiv": data["aktiv"]}) if funnet else _ingen_side()

    @app.post("/admin/kurs/<int:kurs_id>/kursside/innstillinger")
    @krever_admin
    def admin_kursside_innstillinger(kurs_id):
        hent_kurs(kurs_id)
        data, feil = _kropp(10_000)
        if feil:
            return feil
        krever_kode = data.get("innsjekk_krever_kode")
        if krever_kode is not None and not isinstance(krever_kode, bool):
            return _feil("Ugyldig forespørsel.", 400)
        stenges = data["stenges"] if "stenges" in data else None
        if "stenges" in data and (stenges is None or stenges == ""):
            stenges = ""
        elif stenges is not None and not isinstance(stenges, str):
            return _feil("Ugyldig forespørsel.", 400)
        # apen_dager: null eller «» = tilbake til standard, 0 = ingen tidsbegrensning, 1-3650 = dager etter siste kursdag (bare heltall)
        apen_dager = data["apen_dager"] if "apen_dager" in data else None
        if "apen_dager" in data and (apen_dager is None or apen_dager == ""):
            apen_dager = sidelager.STANDARD
        elif apen_dager is not None and (isinstance(apen_dager, bool) or not isinstance(apen_dager, int)):
            return _feil("Ugyldig forespørsel.", 400)
        felt = [n for n, v in (("innsjekk_krever_kode", krever_kode), ("stenges", stenges), ("apen_dager", apen_dager)) if v is not None]
        try:
            with db.transaksjon(con()):
                funnet = sidelager.sett_innstillinger(con(), kurs_id, innsjekk_krever_kode=krever_kode, stenges=stenges,
                                                      apen_dager=apen_dager, aktor=aktor())
                if funnet:
                    detaljer = {"kurs_id": kurs_id, "felt": felt}
                    if "apen_dager" in felt:                      # bare tallet (ingen persondata): hvem åpnet siden lenger, og hvor lenge?
                        detaljer["apen_dager"] = sidelager.gyldig_apen_dager(sidelager.hent(con(), kurs_id)["apen_dager"])
                    db.logg(con(), "kursside_innstillinger", detaljer, aktor=aktor())
        except Sidefeil as e:
            return _feil(e.melding, e.status)
        if not funnet:
            return _ingen_side()
        side = sidelager.hent(con(), kurs_id)
        return _svar({"status": "ok", "innsjekk_krever_kode": bool(side["innsjekk_krever_kode"]),
                      **_apningstid_data(kurs_id, side)})

    # ------------------------------------------------------------------ forhåndsvisning
    @app.get("/admin/kurs/<int:kurs_id>/kursside/forhandsvis")
    @krever_admin
    def admin_kursside_forhandsvis(kurs_id):
        """Deltakersiden slik den ser ut, rendret av samme mal. Den ENESTE ruten som kan vises i en ramme (samme nettsted)."""
        kurs = hent_kurs(kurs_id)
        side = sidelager.hent(con(), kurs_id)
        versjon = request.args.get("versjon", "utkast")
        bruk_publisert = versjon == "publisert" and side is not None and bool(side["publisert"])
        dok = si.les(side["publisert"]) if bruk_publisert else sidelager.hent_utkast(con(), kurs_id)[0]
        forh_tekst = "Slik ser deltakerne siden nå" if bruk_publisert else "Forhåndsvisning – deltakerne ser ikke dette"
        if versjon == "publisert" and not bruk_publisert:
            # Aldri publisert: utkastet vises (så administratoren ser noe), men teksten sier tydelig hva deltakerne faktisk får
            forh_tekst = "Ikke publisert ennå: deltakerne ser «Min side er ikke åpnet ennå». Dette er utkastet."
        if bruk_publisert:
            stenges = sidelager.effektiv_stenges(con(), kurs_id, side)
            if not side["aktiv"]:
                forh_tekst = "Siden er tatt ned: deltakerne ser «Min side er midlertidig stengt». Dette er innholdet som ligger klart."
            elif stenges is not None and idag() > stenges:
                forh_tekst = "Siden er stengt: deltakerne ser «Min side er stengt». Dette er innholdet som ligger klart."
        dag = request.args.get("dag", "")
        dag_id = int(dag) if re.fullmatch(r"[0-9]{1,9}", dag) else None      # aldri isdigit(): «²» og tusen sifre ga ValueError
        v = deltakerside.bygg_visning(
            con(), kurs, dok, deltaker_id=None, idag=idag(), forhandsvisning=True,
            fil_url=lambda fid: url_for("admin_kursside_fil", kurs_id=kurs_id, fil_id=fid), sp_url=lambda navn: None,
            simuler_kursdag_id=dag_id,
            sist_publisert=side["publisert_tid"] if bruk_publisert else None,
            forh_tekst=forh_tekst)
        v["min_side_url"] = url_for("min_side")
        v["person"] = minside.eksempel_opplysninger()
        svar = Response(render_template("kursside_deltaker.html", v=v, forhandsvisning=True))
        svar.headers["X-Frame-Options"] = "SAMEORIGIN"
        svar.headers["Content-Security-Policy"] = sikkerhet.csp_for_egen_ramme(g.get("csp_nonce", ""))
        return svar

    # ------------------------------------------------------------------ filer
    @app.post("/admin/kurs/<int:kurs_id>/kursside/fil")
    @krever_admin
    def admin_kursside_fil_opp(kurs_id):
        """Opplasting som rå kropp (Content-Type: application/octet-stream, X-Filnavn, X-CSRF-Token), én fil om gangen."""
        hent_kurs(kurs_id)
        try:
            sikkerhet.krev_kvote("kursside_opplasting", str(session.get("admin_id")))
        except HTTPException:
            return _feil("For mange opplastinger på kort tid. Vent noen minutter og prøv igjen.", 429)
        maks = max(config.KURSSIDE_FIL_MAKS_MB, config.KURSSIDE_BILDE_MAKS_MB) * _MB
        lengde = request.content_length
        if lengde is None:
            return _feil("Forespørselen mangler lengde.", 411)
        if lengde > maks + 1:
            return _feil(sidelager.for_stor_tekst(config.KURSSIDE_FIL_MAKS_MB), 413)
        innhold = request.stream.read(maks + 1)
        if len(innhold) > maks:
            return _feil(sidelager.for_stor_tekst(config.KURSSIDE_FIL_MAKS_MB), 413)
        navn = unquote(request.headers.get("X-Filnavn", ""), errors="replace")
        try:
            with db.transaksjon(con()):
                fil_id, duplikat = sidelager.lagre_fil(con(), kurs_id, navn, innhold, aktor())
                if not duplikat:
                    db.logg(con(), "kursside_fil_lastet_opp", {"kurs_id": kurs_id, "fil_id": fil_id, "storrelse": len(innhold)},
                            aktor=aktor())
        except Sidefeil as e:
            return _feil(e.melding, e.status)
        return _svar({"status": "ok", "fil": sidelager.fil_meta(con(), kurs_id, fil_id), "duplikat": duplikat,
                      "kvote": sidelager.kvote(con(), kurs_id)})

    @app.get("/admin/kurs/<int:kurs_id>/kursside/fil/<int:fil_id>")
    @krever_admin
    def admin_kursside_fil(kurs_id, fil_id):
        """Alle kursets filer (også de som ikke er publisert) til administratorer. Alle roller kan se."""
        hent_kurs(kurs_id)
        treff = sidelager.fil_innhold(con(), kurs_id, fil_id) or abort(404)
        return filrespons.fil_respons(*treff)

    @app.post("/admin/kurs/<int:kurs_id>/kursside/fil/<int:fil_id>/slett")
    @krever_admin
    def admin_kursside_fil_slett(kurs_id, fil_id):
        hent_kurs(kurs_id)
        if sidelager.fil_meta(con(), kurs_id, fil_id) is None:
            abort(404)
        with db.transaksjon(con()):
            grunn = sidelager.slett_fil(con(), kurs_id, fil_id, aktor())
            if grunn is None:
                db.logg(con(), "kursside_fil_slettet", {"kurs_id": kurs_id, "fil_id": fil_id}, aktor=aktor())
        if grunn is not None:
            return _svar({"status": "i_bruk", "melding": grunn}, 409)
        return _svar({"status": "ok", "kvote": sidelager.kvote(con(), kurs_id)})

    # ------------------------------------------------------------------ versjoner, mal, andre kurs
    @app.post("/admin/kurs/<int:kurs_id>/kursside/versjon/<int:versjon_id>/gjenopprett")
    @krever_admin
    def admin_kursside_gjenopprett(kurs_id, versjon_id):
        hent_kurs(kurs_id)
        if sidelager.hent_versjon(con(), kurs_id, versjon_id) is None:
            abort(404)
        data, feil = _kropp()
        if feil:
            return feil
        versjon = _heltall(data.get("versjon"), 1)
        if versjon is None:
            return _feil("Ugyldig forespørsel.", 400)
        if sidelager.hent(con(), kurs_id) is None:
            return _ingen_side()
        with db.transaksjon(con()):
            resultat = sidelager.gjenopprett(con(), kurs_id, versjon_id, versjon, aktor())
            if resultat is not None:
                db.logg(con(), "kursside_gjenopprettet", {"kurs_id": kurs_id, "versjon_id": versjon_id}, aktor=aktor())
        if resultat is None:
            return _konflikt(kurs_id)
        ny, dok, merknader = resultat
        return _svar({"status": "ok", "versjon": ny, "dokument": dok, "merknader": merknader})

    @app.get("/admin/kurs/<int:kurs_id>/kursside/kilder")
    @krever_admin
    def admin_kursside_kilder(kurs_id):
        hent_kurs(kurs_id)
        kilder = []
        for k in sidelager.kurs_med_side(con(), kurs_id):
            dok, _v = sidelager.hent_utkast(con(), k["id"])
            kilder.append({**k, "blokker": [{"id": b["id"], "type": b["type"], "tittel": b["tittel"] or si.BLOKKTYPE_NAVN[b["type"]]}
                                            for b in dok["blokker"]]})
        return _svar({"status": "ok", "kurs": kilder})

    def _erstatt_og_svar(kurs_id: int, versjon: int, dok: dict, merknad: str, logg_handling: str, logg_detaljer: dict):
        """Validerer det nye utkastet, tar sikkerhetskopi og lagrer i én transaksjon. Kaster _Konflikt ved versjonskonflikt."""
        filer, dager = sidelager.valideringsgrunnlag(con(), kurs_id)
        dok, merknader = si.valider(dok, fil_ider=filer, kursdag_ider=dager)
        ny = sidelager.erstatt_utkast(con(), kurs_id, dok, versjon, aktor(), merknad)
        if ny is None:
            raise _Konflikt()
        db.logg(con(), logg_handling, {"kurs_id": kurs_id, **logg_detaljer}, aktor=aktor())
        return ny, dok, merknader

    @app.post("/admin/kurs/<int:kurs_id>/kursside/hent-fra")
    @krever_admin
    def admin_kursside_hent_fra(kurs_id):
        kurs = hent_kurs(kurs_id)
        data, feil = _kropp()
        if feil:
            return feil
        versjon, fra = _heltall(data.get("versjon"), 1), _heltall(data.get("fra_kurs_id"), 1)
        ider = data.get("blokk_ider")
        if versjon is None or fra is None or not isinstance(ider, list) or not all(isinstance(i, str) for i in ider):
            return _feil("Ugyldig forespørsel.", 400)
        if sidelager.hent(con(), kurs_id) is None:
            return _ingen_side()
        if fra == kurs_id or sidelager.hent(con(), fra) is None:
            return _feil("Det valgte kurset har ingen Min side å hente fra.", 422)
        kilde, _v = sidelager.hent_utkast(con(), fra)
        valgte = [b for b in kilde["blokker"] if b["id"] in set(ider)]
        if not valgte:
            return _feil("Velg minst én blokk å hente.", 422)
        try:
            with db.transaksjon(con()):
                fil_kart = {}
                for fid in si.fil_ider_i({"blokker": valgte}):
                    ny_fil = sidelager.kopier_fil(con(), fra, kurs_id, fid, aktor())
                    if ny_fil is not None:
                        fil_kart[fid] = ny_fil
                nye = si.kopier_blokker(kilde, [b["id"] for b in valgte], fil_kart=fil_kart,
                                        kursdager=deltakerside.kursdager_for(con(), kurs))
                dok, _versjon = sidelager.hent_utkast(con(), kurs_id)
                dok["blokker"] = dok["blokker"] + nye
                ny_versjon, dok, merknader = _erstatt_og_svar(kurs_id, versjon, dok, "Før «Hent fra annet kurs»",
                                                              "kursside_hentet_fra", {"fra_kurs_id": fra, "antall": len(nye)})
        except _Konflikt:
            return _konflikt(kurs_id)
        except Sidefeil as e:
            return _feil(e.melding, e.status)
        return _svar({"status": "ok", "versjon": ny_versjon, "dokument": dok, "filer": sidelager.fil_liste(con(), kurs_id),
                      "merknader": merknader})

    @app.post("/admin/kurs/<int:kurs_id>/kursside/mal")
    @krever_admin
    def admin_kursside_mal(kurs_id):
        kurs = hent_kurs(kurs_id)
        data, feil = _kropp()
        if feil:
            return feil
        versjon, navn = _heltall(data.get("versjon"), 1), data.get("mal")
        if versjon is None or not isinstance(navn, str):
            return _feil("Ugyldig forespørsel.", 400)
        if sidelager.hent(con(), kurs_id) is None:
            return _ingen_side()
        try:
            dok = si.mal(navn, deltakerside.kursdager_for(con(), kurs))
            with db.transaksjon(con()):
                ny_versjon, dok, merknader = _erstatt_og_svar(kurs_id, versjon, dok, f"Før mal «{navn}»", "kursside_mal",
                                                              {"mal": navn})
        except _Konflikt:
            return _konflikt(kurs_id)
        except Sidefeil as e:
            return _feil(e.melding, e.status)
        return _svar({"status": "ok", "versjon": ny_versjon, "dokument": dok, "filer": sidelager.fil_liste(con(), kurs_id),
                      "merknader": merknader})

    @app.get("/admin/kurs/<int:kurs_id>/kursside/deltakernavn")
    @krever_admin
    def admin_kursside_deltakernavn(kurs_id):
        """Påmeldte deltakere som «Kari N.» (kun kursadmin og system: dette er personnavn)."""
        hent_kurs(kurs_id)
        return _svar({"status": "ok", "navn": sidelager.deltakernavn(con(), kurs_id)})

    @app.get("/admin/kurs/<int:kurs_id>/kursside/token")
    @krever_admin
    def admin_kursside_token(kurs_id):
        """Nytt CSRF-token uten å skrive noe (når økten er fornyet i en annen fane)."""
        hent_kurs(kurs_id)
        return _svar({"status": "ok", "csrf": sikkerhet.csrf_token()})
