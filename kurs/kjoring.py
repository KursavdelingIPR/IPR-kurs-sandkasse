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

    def avslutt(self) -> None:
        if self.tor:
            self.con.rollback()
            self.si("Tørrkjøring: ingen endringer lagret.")
        else:
            self.con.commit()
