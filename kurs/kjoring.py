"""Kontekst for en kjoring: database, "dagens dato" (kan overstyres) og tor-modus.

Tor-modus (--tor): viser hva som VILLE skjedd. Ingen e-post sendes, ingen API-kall, og alle
databaseendringer rulles tilbake til slutt.
"""
from dataclasses import dataclass, field
from datetime import date

from . import db, maltekster
from .feil import sikker_feiltekst
from .integrasjoner import epost


@dataclass
class Kjoring:
    con: object
    idag: date = field(default_factory=date.today)
    tor: bool = False
    utskrift: list[str] = field(default_factory=list)

    def si(self, tekst: str) -> None:
        self.utskrift.append(tekst)
        print(tekst)

    def render_for_sending(self, mal: str, **data) -> tuple[str, str]:
        """Rendrer en e-post NOYAKTIG slik en faktisk utsending gjor: override-bevisst maltekst (kun for maler som er koblet til
        maltekstsystemet) og deretter epost.render. Ingen sideeffekter: leser kun, committer/ruller ikke tilbake, skriver ingen
        hendelse og rorer ikke utsending_logg. Brukes av send_en_gang (FOER claim) og av preflight foer irreversible
        admin-handlinger (f.eks. avlysning). Ugyldig/korrupt maltekst eller DB-lesefeil gir MalFeil."""
        maltekst = maltekster.maltekst_for_utsending(self.con, mal, data)   # None = ikke-koblet mal -> gammel rendering
        return epost.render(mal, maltekst=maltekst, **data)

    def send_en_gang(self, nokkel: str, til: str, type_: str, mal: str, *, paamelding_id: int | None = None,
                     **data) -> bool:
        """Sender e-post med malen hvis (nokkel, til, type) ikke er sendt for. True hvis sendt naa.

        Rendrer FOERST (en malfeil skal ikke etterlate noen rad - kan proves igjen etter retting), og
        delegerer deretter til send_ferdigrendret_en_gang() for selve claim/send/status-motoren (se
        dens docstring for den fulle kontrakten - uendret her)."""
        emne, html = self.render_for_sending(mal, **data)
        return self.send_ferdigrendret_en_gang(nokkel, til, type_, emne, html, paamelding_id=paamelding_id)

    def send_ferdigrendret_en_gang(self, nokkel: str, til: str, type_: str, emne: str, html: str, *,
                                   paamelding_id: int | None = None) -> bool:
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
        """
        vant = db.reserver_sending(self.con, nokkel, til, type_) or             db.reserver_sending_pa_nytt(self.con, nokkel, til, type_)
        if not self.tor:
            self.con.commit()  # claim (eller tapt claim) - frigjor skrivelaasen FOR det evt. lange eksterne kallet
        if not vant:
            return False
        if self.tor:
            self.si(f"  [TØRR] {type_:<22} -> {til}  «{emne}»")
            db.sett_sendt(self.con, nokkel, til, type_)
            return True
        try:
            epost.send(til, emne, html)
        except Exception as e:  # noqa: BLE001 - klassifisering "feilet"/"ukjent" ikke avklart enna, se docstring
            db.sett_sending_ukjent(self.con, nokkel, til, type_)
            detaljer = {"nokkel": nokkel, "type": type_, "feil": sikker_feiltekst(e)}
            if paamelding_id is not None:
                detaljer["paamelding_id"] = paamelding_id
            db.logg(self.con, "epost_ukjent", detaljer)
            self.con.commit()
            raise
        db.sett_sendt(self.con, nokkel, til, type_)
        self.con.commit()  # resultatet er kort og lokalt - lagre det umiddelbart (avgrenser krasjvindu B)
        self.si(f"  sendt  {type_:<22} «{emne}»")  # ingen mottakeradresse i driftsutskriften
        return True

    def send_admin_utsending(self, utsending_id: int) -> list[int]:
        """Sender en manuell, forhaandsvist utsendelse - én e-post pr mottaker, ingen ser andres adresse.

        Bruker utsendelsens egen, stabile nokkel til deduplisering: er den allerede sendt til en
        mottaker (f.eks. fra et tidligere, tapt forsok), sendes den ikke paa nytt til akkurat den.
        Returnerer paamelding_id-ene det faktisk ble sendt til naa (tom liste = alt var alt sendt fra for).
        """
        rad = self.con.execute("SELECT * FROM admin_utsending WHERE id=?", (utsending_id,)).fetchone()
        if not rad:
            return []
        sendt_til = []
        for m in db.admin_utsending_mottakere(self.con, utsending_id):
            if db.allerede_sendt(self.con, rad["nokkel"], m["epost"], "admin_epost"):
                continue
            emne, html = epost.render("admin_melding", emne=rad["emne"], tekst=rad["tekst"], d=m)
            if self.tor:
                self.si(f"  [TØRR] admin_epost -> {m['epost']}  «{emne}»")
            else:
                epost.send(m["epost"], emne, html)
                self.si(f"  sendt  admin_epost -> {m['epost']}  «{emne}»")
            db.marker_sendt(self.con, rad["nokkel"], m["epost"], "admin_epost")
            sendt_til.append(m["id"])
        return sendt_til

    def avslutt(self) -> None:
        if self.tor:
            self.con.rollback()
            self.si("Tørrkjøring: ingen endringer lagret.")
        else:
            self.con.commit()
