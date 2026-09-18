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
        """Sender e-post med malen hvis (nokkel, til, type) ikke er sendt for. True hvis sendt."""
        if db.allerede_sendt(self.con, nokkel, til, type_):
            return False
        emne, html = epost.render(mal, **data)
        if self.tor:
            self.si(f"  [TØRR] {type_:<22} -> {til}  «{emne}»")
        else:
            epost.send(til, emne, html)
            self.si(f"  sendt  {type_:<22} -> {til}  «{emne}»")
        db.marker_sendt(self.con, nokkel, til, type_)
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
