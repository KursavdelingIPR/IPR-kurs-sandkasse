"""Kontekst for en kjoring: database, "dagens dato" (kan overstyres) og tor-modus.

Tor-modus (--tor): viser hva som VILLE skjedd. Ingen e-post sendes, ingen API-kall, og alle
databaseendringer rulles tilbake til slutt.
"""
from dataclasses import dataclass, field
from datetime import date

from . import db
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

    def send_en_gang(self, nokkel: str, til: str, type_: str, mal: str, **data) -> bool:
        """Sender e-post med malen hvis (nokkel, til, type) ikke er sendt for. True hvis sendt naa.

        Reserverer forst sendingen atomisk (nytt forsok, eller retry etter en kjent, trygg feil) -
        dette hindrer to samtidige kjoringer (f.eks. daglig jobb + manuell fase 9/11-behandling) i aa
        sende samme e-post to ganger. Taper claimen (noen andre har den fra for, enten ferdig sendt
        eller et uavklart forsok som paagaar/star fast), gjor denne kallen ingenting og returnerer False
        - kalleren maa selv sjekke db.allerede_sendt() etterpaa for aa vite om meldingen faktisk gikk ut.

        Taksonomien for "sikkert feilet" (trygt aa prove paa nytt) vs. "ukjent utfall" (ALDRI automatisk
        paa nytt) er ikke avklart for Microsoft Graph enna (se trinn 2.5-designet) - inntil videre
        klassifiseres derfor ALLE feil konservativt som ukjent, og hendelsen logges slik at en admin kan
        oppdage at noe krever manuell kontroll.
        """
        vant = db.reserver_sending(self.con, nokkel, til, type_) or \
            db.reserver_sending_pa_nytt(self.con, nokkel, til, type_)
        if not vant:
            return False
        if not self.tor:
            self.con.commit()  # gjor reservasjonen synlig for andre FOR det evt. lange eksterne kallet

        emne, html = epost.render(mal, **data)
        if self.tor:
            self.si(f"  [TØRR] {type_:<22} -> {til}  «{emne}»")
            db.sett_sendt(self.con, nokkel, til, type_)
            return True
        try:
            epost.send(til, emne, html)
        except Exception as e:  # noqa: BLE001 - klassifisering "feilet"/"ukjent" ikke avklart enna, se docstring
            db.sett_sending_ukjent(self.con, nokkel, til, type_)
            db.logg(self.con, "epost_ukjent", {"nokkel": nokkel, "type": type_, "feil": str(e)})
            self.con.commit()
            raise
        db.sett_sendt(self.con, nokkel, til, type_)
        self.si(f"  sendt  {type_:<22} -> {til}  «{emne}»")
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
