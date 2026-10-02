"""Kontekst for en kjoring: database, "dagens dato" (kan overstyres) og tor-modus.

Tor-modus (--tor): viser hva som VILLE skjedd. Ingen e-post sendes, ingen API-kall, og alle
databaseendringer rulles tilbake til slutt.
"""
from dataclasses import dataclass, field
from datetime import date

from . import config, db, deltakerside, maltekster, minside, signaturer
from .feil import sikker_feiltekst
from .integrasjoner import epost

# Sendefeil paa rad i én kjoering foer resten av utsendingene utsettes: e-posttjenesten er da trolig nede, og hvert nye
# forsoek ville bare gitt enda en uavklart melding. Utsatte meldinger er IKKE reservert - de tas ved neste kjoering.
STOPP_ETTER = 3

# Epostmalene som får en personlig lenke til Min side (en knapp), når kurset har en åpen Min side (Kjoring._legg_til_min_side)
MIN_SIDE_MALER = frozenset({"bekreftelse", "ukefor", "dagfor"})


class SendingStanset(RuntimeError):
    """E-posttjenesten har feilet flere ganger paa rad i denne kjoeringen. Ingenting er reservert eller sendt."""


class EpostkopiFeil(SendingStanset):
    """Kopien til e-posthistorikken kunne ikke lagres. Da sendes e-posten ikke: ingenting er reservert eller sendt, og
    den tas ved neste forsøk (samme håndtering hos kallerne som SendingStanset)."""


@dataclass
class Sendeutfall:
    """Resultatet av én utsending til mange mottakere (se Kjoring.send_til_mange)."""
    sendt: list = field(default_factory=list)   # paamelding_id-er som fikk e-posten naa
    allerede: int = 0                           # hadde fått den fra foer
    uavklart: int = 0                           # feilet under sending NAA - utfallet er uavklart
    uavklart_fra_for: int = 0                   # hadde et uavklart eller paagaaende forsoek fra foer - ikke roert
    ikke_forsokt: int = 0                       # utsatt: e-posttjenesten nede / ugyldig mal - ingenting reservert


@dataclass
class Kjoring:
    con: object
    idag: date = field(default_factory=date.today)
    tor: bool = False
    utskrift: list[str] = field(default_factory=list)
    sendefeil_paa_rad: int = 0
    aktor: str = "system"                   # hvem som sendte (e-posthistorikken): 'system' eller 'admin:<brukernavn>'
    feil: list[str] = field(default_factory=list)       # steg som feilet (daglig.py) - gir avslutningskode 1

    @property
    def sending_stanset(self) -> bool:
        return self.sendefeil_paa_rad >= STOPP_ETTER

    def si(self, tekst: str) -> None:
        self.utskrift.append(tekst)
        print(tekst)

    def min_side_lenke(self, paamelding_id: int) -> str | None:
        """Den personlige lenken til Min side for en påmelding, eller None: påmeldingen må ha plass (bekreftet), kurset må ha en publisert og
        åpen Min side (ellers ville lenken ført til «ikke åpnet ennå»), en administrator må ikke ha stengt lenken, og den må ikke være utløpt
        (30 dager etter siste kursdag: siden kan stå åpen lenger, men en lenke som ikke virker skal ikke sendes). Leser bare."""
        rad = self.con.execute("SELECT kurs_id, status FROM paamelding WHERE id=?", (paamelding_id,)).fetchone()
        if not rad or rad["status"] != "bekreftet" or not deltakerside.har_side(self.con, rad["kurs_id"], self.idag):
            return None
        slutt = minside.utloper(self.con, paamelding_id)
        if slutt is not None and self.idag > slutt:
            return None
        return minside.url(self.con, paamelding_id)

    def _legg_til_min_side(self, data: dict) -> None:
        """Legger `min_side_url` til dataene for e-poster om ett kurs (se min_side_lenke). Ingen påmelding (eksempeldata i forhåndsvisningen
        av malen) gir ingen lenke."""
        rad = data.get("p") if data.get("p") is not None else data.get("d")
        try:
            paamelding_id = int(rad["id"])
        except (KeyError, IndexError, TypeError, ValueError):
            return
        lenke = self.min_side_lenke(paamelding_id)
        if lenke:
            data["min_side_url"] = lenke

    def render_for_sending(self, mal: str, **data) -> tuple[str, str]:
        """Rendrer en e-post NOYAKTIG slik en faktisk utsending gjor: override-bevisst maltekst (kun for maler som er koblet til
        maltekstsystemet) og deretter epost.render. Ingen sideeffekter: leser kun, committer/ruller ikke tilbake, skriver ingen
        hendelse og rorer ikke utsending_logg. Brukes av send_en_gang (FOER claim) og av preflight foer irreversible
        admin-handlinger (f.eks. avlysning). Ugyldig/korrupt maltekst eller DB-lesefeil gir MalFeil."""
        if mal in MIN_SIDE_MALER and "min_side_url" not in data:
            self._legg_til_min_side(data)
        maltekst = maltekster.maltekst_for_utsending(self.con, mal, data)   # None = ikke-koblet mal -> gammel rendering
        if mal in signaturer.KURSETS_MALER and "signatur" not in data:
            # Kursets signatur (ellers standardsignaturen), renset på nytt nå - bildene som innebygde bilder (cid).
            # Eksempeldata i forhåndsvisningen har ikke noe kurs-id: da vises standardsignaturen.
            try:
                kurs_id = data["kurs"]["id"]
            except (KeyError, IndexError, TypeError):
                kurs_id = None
            data["signatur"] = signaturer.som_markup(signaturer.til_epost(signaturer.for_kurs(self.con, kurs_id)))
        return epost.render(mal, maltekst=maltekst, **data)

    def send_en_gang(self, nokkel: str, til: str, type_: str, mal: str, *, paamelding_id: int | None = None,
                     kurs_id: int | None = None, lagre_kopi: bool = True, **data) -> bool:
        """Sender e-post med malen hvis (nokkel, til, type) ikke er sendt for. True hvis sendt naa.

        Rendrer FOERST (en malfeil skal ikke etterlate noen rad - kan proves igjen etter retting), og
        delegerer deretter til send_ferdigrendret_en_gang() for selve claim/send/status-motoren (se
        dens docstring for den fulle kontrakten - uendret her)."""
        emne, html = self.render_for_sending(mal, **data)
        return self.send_ferdigrendret_en_gang(nokkel, til, type_, emne, html, paamelding_id=paamelding_id, mal=mal,
                                               kurs_id=kurs_id, lagre_kopi=lagre_kopi)

    def send_ferdigrendret_en_gang(self, nokkel: str, til: str, type_: str, emne: str, html: str, *,
                                   paamelding_id: int | None = None, mal: str | None = None,
                                   kurs_id: int | None = None, vedlegg=(), lagre_kopi: bool = True) -> bool:
        """Sender en ALLEREDE RENDRET (emne, html) hvis (nokkel, til, type) ikke er sendt for. True hvis sendt naa.

        Dette ER selve fase-11-motoren (claim/commit/send/status) - send_en_gang() er kun et tynt lag som
        rendrer og delegerer hit. Brukes direkte av kallere som MÅ garantere at ingen ny DB-avhengig
        maltekstlesing skjer mellom rendring og sending (TOCTOU-vakt) - typisk fordi en varig sideeffekt
        (f.eks. en generert fil + dokumentrad) allerede er bygget paa nøyaktig dette rendrede resultatet,
        og en eventuell SENERE malfeil (override endret, DB-lesefeil) IKKE skal kunne oppstaa etter at den
        sideeffekten er utfoert. Se kursbevis.py.

        Rekkefolge (hver overgang med sin egen korte transaksjon - INGEN skrivelaas holdes over det
        eksterne kallet):
          1. claim (atomisk)      nytt forsok, eller retry etter en kjent, trygg feil
          2. commit               reservasjonen blir synlig for andre, laasen frigis - ogsaa hvis claimen tapes
          3. epost.send           eksternt kall, ingen laas
          4. lagre resultat + commit umiddelbart (sendt / ukjent)
        Taper claimen (noen andre har den fra for, enten ferdig sendt eller et uavklart forsok som
        paagaar/star fast), gjor denne kallen ingenting og returnerer False - kalleren maa selv sjekke
        db.allerede_sendt() etterpaa for aa vite om meldingen faktisk gikk ut.

        Commit-punktene tar med seg evt. ventende, ikke-committet arbeid fra kalleren. Det er allerede
        tilfellet for claim-committen; resultat-committen tar KUN med resultatskrivingen, siden ingenting
        annet skrives mellom claim og resultat. I tor-modus committes aldri noe (avslutt() ruller tilbake).

        Taksonomien for "sikkert feilet" (trygt aa prove paa nytt) vs. "ukjent utfall" (ALDRI automatisk
        paa nytt) er ikke avklart for Microsoft Graph enna - inntil videre klassifiseres derfor ALLE feil
        konservativt som ukjent, og hendelsen logges (kun sikker feiltekst, aldri str(e) - se feil.py).
        `paamelding_id` (valgfri) tas med i hendelsen slik at en admin kan finne frem - aldri navn/e-post.

        E-posthistorikk: vinner kallet claimen, lagres en uforanderlig kopi (emne, html, bilder/vedlegg, til, fra, hvem som
        sendte) i SAMME transaksjon som claimen - FØR sendingen. Feiler lagringen, rulles claimen tilbake (savepoint:
        kallerens eget, ventende arbeid røres ikke), ingenting sendes, og EpostkopiFeil kastes. Kopien får status sendt /
        ukjent sammen med utsending_logg. lagre_kopi=False gjelder bare e-poster med en personlig tilgangslenke (token),
        som aldri skal lagres (kursholderens opplastingslenke, bedriftens kvitteringslenke). Tørrkjøring lagrer ingen kopi.
        `vedlegg` er epost.Vedlegg (bilder i teksten med cid, og vanlige vedlegg) - de samme bytene sendes og lagres.
        """
        if self.sending_stanset:        # FOER claim: ingenting reserveres, meldingen tas ved neste kjoering
            raise SendingStanset("E-posttjenesten har feilet flere ganger på rad - resten sendes ved neste kjøring.")
        # Signaturbildene (cid:signaturbilde-...) sendes som innebygde bilder - og lagres dermed i kopien
        vedlegg = [epost.som_vedlegg(v) for v in vedlegg] + signaturer.innebygde_bilder(self.con, html, mal)
        kopi_id = None
        db.begynn_transaksjon(self.con)     # claim og kopi i samme transaksjon, også i tørrkjøring (rulles tilbake)
        try:
            with db.isolert(self.con, "epostclaim"):
                vant = (db.reserver_sending(self.con, nokkel, til, type_)
                        or db.reserver_sending_pa_nytt(self.con, nokkel, til, type_))
                if vant and lagre_kopi and not self.tor:
                    try:
                        kopi_id = db.lagre_epostkopi(
                            self.con, nokkel=nokkel, type_=type_, til=til, fra=config.AVSENDER_EPOST, sendt_av=self.aktor,
                            emne=emne, html=minside.skjul_i_kopi(html), paamelding_id=paamelding_id, kurs_id=kurs_id, mal=mal,
                            vedlegg=vedlegg)           # den personlige lenken til Min side lagres aldri i historikken
                    except Exception as e:  # noqa: BLE001 - uansett årsak: uten kopi ingen sending
                        raise EpostkopiFeil(f"Kopien av e-posten kunne ikke lagres ({sikker_feiltekst(e)}). "
                                            "E-posten er ikke sendt.") from e
        except EpostkopiFeil as e:
            detaljer = {"nokkel": nokkel, "type": type_, "feil": sikker_feiltekst(e.__cause__ or e)}
            if paamelding_id is not None:
                detaljer["paamelding_id"] = paamelding_id
            db.logg(self.con, "epostkopi_feilet", detaljer)
            if not self.tor:
                self.con.commit()
            raise
        if not self.tor:
            self.con.commit()  # claim + kopi (eller tapt claim) - frigjor skrivelaasen FOR det evt. lange eksterne kallet
        if not vant:
            return False
        if self.tor:
            self.si(f"  [TØRR] {type_:<22} -> {til}  «{emne}»")
            db.sett_sendt(self.con, nokkel, til, type_)
            return True
        try:
            epost.send(til, emne, html, vedlegg=vedlegg)
        except Exception as e:  # noqa: BLE001 - klassifisering "feilet"/"ukjent" ikke avklart enna, se docstring
            self.sendefeil_paa_rad += 1
            db.sett_sending_ukjent(self.con, nokkel, til, type_)
            if kopi_id is not None:
                db.sett_epostkopi_status(self.con, kopi_id, "ukjent", feilmelding=sikker_feiltekst(e))
            detaljer = {"nokkel": nokkel, "type": type_, "feil": sikker_feiltekst(e)}
            if paamelding_id is not None:
                detaljer["paamelding_id"] = paamelding_id
            db.logg(self.con, "epost_ukjent", detaljer)
            self.con.commit()
            raise
        self.sendefeil_paa_rad = 0
        db.sett_sendt(self.con, nokkel, til, type_)
        if kopi_id is not None:
            db.sett_epostkopi_status(self.con, kopi_id, "sendt")
        self.con.commit()  # resultatet er kort og lokalt - lagre det umiddelbart (avgrenser krasjvindu B)
        self.si(f"  sendt  {type_:<22} «{emne}»")  # ingen mottakeradresse i driftsutskriften
        return True

    def send_til_mange(self, nokkel: str, type_: str, mottakere, lag, *, mal: str | None = None) -> Sendeutfall:
        """Samme type melding til mange mottakere (rader med id = paamelding_id og epost), hver gjennom claim-motoren
        (send_ferdigrendret_en_gang). `lag(mottaker)` gir (emne, html) eller (emne, html, vedlegg). `mal` er malnavnet
        (til e-posthistorikken). En mottaker som alt har en rad (sendt, uavklart
        eller paagaaende) faar den aldri paa nytt - heller ikke ved dobbeltklikk, to faner eller et nytt forsoek. En
        feil hos én mottaker stopper ikke de andre; etter STOPP_ETTER feil paa rad utsettes resten (ikke reservert)."""
        ut = Sendeutfall()
        for m in mottakere:
            if self.sending_stanset:
                ut.ikke_forsokt += 1
                continue
            try:
                emne, html, *vedlegg = lag(m)
                if self.send_ferdigrendret_en_gang(nokkel, m["epost"], type_, emne, html, paamelding_id=m["id"], mal=mal,
                                                   vedlegg=vedlegg[0] if vedlegg else ()):
                    ut.sendt.append(m["id"])
                elif db.allerede_sendt(self.con, nokkel, m["epost"], type_):
                    ut.allerede += 1
                else:
                    ut.uavklart_fra_for += 1            # et annet forsoek er uavklart eller paagaar
            except (maltekster.MalFeil, SendingStanset):
                ut.ikke_forsokt += 1                    # ingenting reservert - kan sendes igjen senere
            except Exception:  # noqa: BLE001 - motoren har alt satt 'ukjent' og logget (PII-fritt)
                ut.uavklart += 1
        return ut

    def send_admin_utsending(self, utsending_id: int) -> Sendeutfall:
        """Sender en manuell, forhaandsvist utsendelse - én e-post pr mottaker, ingen ser andres adresse. Utsendelsens
        egen, stabile nokkel dedupliserer via claim-motoren (se send_til_mange)."""
        rad = self.con.execute("SELECT * FROM admin_utsending WHERE id=?", (utsending_id,)).fetchone()
        if not rad:
            return Sendeutfall()
        signatur = {}
        if rad["signatur_html"] is not None:    # renses på nytt i det den sendes (eldre utsendelser: fast hilsen)
            signatur["signatur"] = signaturer.som_markup(signaturer.til_epost(
                signaturer.rens(rad["signatur_html"], signaturer.bildekart(self.con))))
        return self.send_til_mange(
            rad["nokkel"], "admin_epost", db.admin_utsending_mottakere(self.con, utsending_id),
            lambda m: epost.render("admin_melding", emne=rad["emne"], tekst=rad["tekst"],
                                   d={**dict(m), "min_side_url": self.min_side_lenke(m["id"])}, **signatur),
            mal="admin_melding")

    def avslutt(self) -> None:
        if self.tor:
            self.con.rollback()
            self.si("Tørrkjøring: ingen endringer lagret.")
        else:
            self.con.commit()
