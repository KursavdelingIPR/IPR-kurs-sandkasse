"""Kursside for deltakerne: siden og filene (SPEC §5.2, §8.1, §8.3).

Kobles på appen med `installer(app, con, ...)`. Tilgangen sjekkes i selve ruten ved HVER forespørsel (deltakerside.tilgang): bare
deltakere med BEKREFTET påmelding på akkurat dette kurset, og bare når siden er publisert, åpen og ikke stengt. URL-en er ingen
hemmelighet (kurs.kode er gjettbar), og en skjult lenke er ikke tilgangskontroll.

  * Uinnlogget deltaker uten admin-økt: viderekobles til innlogging (`/logg-inn?neste=<sti>`), uansett om kurset finnes.
  * Uinnlogget deltaker med gyldig admin-økt: til forhåndsvisningen i administrasjonen. (Admin kan aldri hente filer via deltakerruten.)
  * Innlogget, men ikke bekreftet på kurset (ukjent kode, venteliste, avmeldt, annet kurs, kurs som er utkast eller avlyst): 404 med
    nøyaktig samme tekst for alle, så siden aldri avslører om et kurs finnes.
  * «Registrer oppmøte i dag» (POST) er reserven på nett for den som ikke får skannet QR-koden i lokalet (SPEC §10.5).
"""
from flask import abort, redirect, render_template, request, session, url_for

from .. import deltakerside as deltakerside_modul   # modulen (logikk); endepunktet under heter også «deltakerside»
from .. import minside, sidelager
from .. import sideinnhold as si
from . import filrespons, sikkerhet

_UTILGJENGELIG = {
    "ikke_funnet": ("Vi finner ikke siden, eller du har ikke tilgang til den. Min side er bare for deltakere med bekreftet "
                    "påmelding. Sjekk at du er logget inn med e-postadressen du meldte deg på med."),
    "ikke_publisert": deltakerside_modul.TEKST_IKKE_PUBLISERT,
    "nedtatt": "Min side er midlertidig stengt.",
}


def hent_oppmote_melding() -> dict | None:
    """Meldingen fra «Registrer oppmøte i dag» (lagt i økten av ruten under). Leses bare én gang: {"kode": kurskode, "niva": "ok"|"feil", "tekst"}."""
    m = session.pop("oppmote_melding", None)
    return m if isinstance(m, dict) and {"kode", "niva", "tekst"} <= set(m) else None


def installer(app, con, *, krever_deltaker, admin_okt_gyldig, idag, deltaker_full=lambda: False) -> None:
    """`krever_deltaker` og `idag()` kommer fra app.py (idag kalles ved hver forespørsel, så demo-spoling og tester virker).
    `deltaker_full()`: er deltakeren innlogget med e-postlenke (privatadressen vises), og ikke bare med den personlige lenken?"""

    def _utilgjengelig(t):
        if t.arsak == "stengt":
            tekst = f"Min side er stengt (den var åpen til {deltakerside_modul.dag_maaned_aar(t.stenges)})."
        else:
            tekst = _UTILGJENGELIG[t.arsak]
        return render_template("kursside_utilgjengelig.html", tekst=tekst), 404

    @app.get("/kurs/<kode>/deltakerside")
    def deltakerside(kode):
        did = session.get("deltaker_id")
        if not did:
            if admin_okt_gyldig():                       # aldri rå session.get("admin_id"): utløpt/deaktivert økt slipper ikke inn
                kurs = deltakerside_modul.hent_kurs_for_side(con(), kode) or abort(404)
                return redirect(url_for("admin_kursside_forhandsvis", kurs_id=kurs["id"], versjon="publisert"))
            return redirect(url_for("logg_inn", neste=request.path))
        t = deltakerside_modul.tilgang(con(), did, kode, idag())
        if not t.ok:
            return _utilgjengelig(t)
        dok = si.les(t.side["publisert"])
        v = deltakerside_modul.bygg_visning(
            con(), t.kurs, dok, deltaker_id=did, idag=idag(), forhandsvisning=False,
            fil_url=lambda fid: url_for("deltakerside_fil", kode=kode, fil_id=fid),
            sist_publisert=t.side["publisert_tid"])
        v["min_side_url"] = url_for("min_side")          # oversikten «Mine kurs»
        v["person"] = minside.mine_opplysninger(con(), did, t.kurs["id"], full=deltaker_full())
        v["bekreft_url"] = url_for("bekreft_epost")
        v["oppmote_url"] = url_for("deltakerside_oppmote", kode=kode)
        melding = hent_oppmote_melding()
        v["oppmote_melding"] = {"niva": melding["niva"], "tekst": melding["tekst"]} if melding and melding["kode"] == kode else None
        return render_template("kursside_deltaker.html", v=v, forhandsvisning=False)

    @app.post("/kurs/<kode>/deltakerside/oppmote")
    def deltakerside_oppmote(kode):
        """«Registrer oppmøte i dag» på nett for innlogget deltaker (reserve for QR-koden). Påmeldingen slås opp fra deltakeren i økten og
        kurskoden i adressen (aldri en id fra skjemaet). Takbegrenset per deltaker FØR koden sjekkes, så koden ikke kan gjettes. Kilde «kode».
        Utfallet vises i kortet etter viderekobling (en melding i økten som leses én gang), til kurssiden eller Min side (`retur`)."""
        did = session.get("deltaker_id")
        if not did:
            return redirect(url_for("logg_inn", neste=url_for("deltakerside", kode=kode)))
        sikkerhet.krev_kvote("oppmote_selv", str(did))
        res = deltakerside_modul.registrer_selv(con(), did, kode, request.form.get("kode", ""), idag())
        if res.status == "ikke_paameldt":
            abort(404)
        con().commit()
        session["oppmote_melding"] = {"kode": kode, "niva": "ok" if res.status in ("ny", "allerede") else "feil",
                                      "tekst": deltakerside_modul.tekst_for(res, innlogget=True, nett=True)}
        # Tilbake til kortet: Min side når `retur` er «min_side», ellers kurssiden (skjemaet står bare på kurssiden og Min side). Aldri en fri adresse.
        if request.form.get("retur") == "min_side":
            return redirect(url_for("min_side", _anchor="mine-kurs"))
        return redirect(url_for("deltakerside", kode=kode, _anchor="i-dag"))

    @app.get("/kurs/<kode>/deltakerside/fil/<int:fil_id>")
    def deltakerside_fil(kode, fil_id):
        """Filen, ellers 404 (samme 404 for alle avvisningsgrunner: utkast-filer, skjulte blokker og andre kurs sine filer lekker aldri)."""
        did = session.get("deltaker_id")
        if not did:
            return redirect(url_for("logg_inn", neste=request.path))
        t = deltakerside_modul.tilgang(con(), did, kode, idag())
        if not t.ok:
            abort(404)
        dok = si.les(t.side["publisert"])
        # Filer knyttet til kursdager deltakeren ikke er på (en ekstradeltaker på andre samlinger), hentes ikke: som i listen på siden
        if fil_id not in si.synlige_fil_ider(dok, idag(), deltakerside_modul.dager_utenfor(con(), t.kurs, did)):
            abort(404)
        meta = sidelager.fil_meta(con(), t.kurs["id"], fil_id) or abort(404)
        # Hver nedlasting av en stor fil dekoder hele innholdet i minnet: takbegrenset per deltaker (bilder har egen, romsligere grense)
        sikkerhet.krev_kvote("kursside_bilde" if meta.get("type") == "bilde" else "kursside_fil", str(did))
        treff = sidelager.fil_innhold(con(), t.kurs["id"], fil_id) or abort(404)
        return filrespons.fil_respons(*treff)
