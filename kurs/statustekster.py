"""Tekstene i dialogen når admin endrer påmeldingsstatus direkte i deltakerlisten.

Bare tekst. Selve endringen (plassen, ventelisten, overbooking, bekreftelse og faktura) gjøres av
db.sett_paamelding_status og ruten admin_deltaker_status - her står ingen statuslogikk. Tekstene skal si hva som faktisk
skjer når admin trykker OK, slik at riktig status velges før bekreftelsen. Navnene på statusene hentes fra
db.PAAMELDINGSSTATUSER (der og bare der står ordene).
"""
from . import db

_AVSLUTTENDE = ("avmeldt", "avslatt", "utgatt", "forlatt")     # sender ingen e-post, og plassen (om noen) blir ledig
_LUKKET = ("avlyst", "avsluttet")                                  # kursstatus: ingen nye påmeldte, ingen opprykk

# Ekstradeltaker: på kurset, men tar ikke plass og teller ikke som fullverdig deltaker. Får ikke bekreftelse eller faktura automatisk
# (prisen er ikke kursets standardpris): den holdes tilbake, admin sender bekreftelsen fra deltakervinduet ved behov, og fakturaen lager
# admin selv utenfor systemet (det finnes ingen fakturafunksjon for ekstradeltakere).
HOLDT_EKSTRADELTAKER = (f"{db.PAAMELDINGSSTATUSER['ekstradeltaker']}: ingen automatisk bekreftelse eller faktura. Send manuelt ved behov. "
                        "Bekreftelsen sender du fra deltakervinduet («Send bekreftelse nå»); fakturaen lager du selv, utenfor systemet. "
                        "Er bekreftelse eller faktura allerede sendt, endres ingenting, og fakturaen krediteres ikke.")


def _navn(status: str) -> str:
    return db.PAAMELDINGSSTATUSER[status]


def dialogtekst(deltaker: str, gammel: str, ny: str, *, kurs, fakturert: bool = False,
                antall_paameldt: int | None = None, flere_samlinger: bool = False, behandlet: bool = False,
                rest_faktura: bool = False) -> str:
    """Spørsmålet admin får før statusen endres, med det som skjer: «Endre status for Kari Nordmann fra Påmeldt til
    Avmeldt? Plassen blir ledig …». `gammel` og `ny` er statusnøkler (db.PAAMELDINGSSTATUSER), `kurs` kursraden,
    `fakturert` om påmeldingen allerede har faktura, `antall_paameldt` hvor mange PÅMELDTE (fullverdige, uten ekstradeltakere)
    som har plass nå (for å vite om plassen faktisk blir ledig når kurset er overbooket), `flere_samlinger` om kurset har flere
    samlinger å velge mellom (ekstradeltaker), `behandlet` om bekreftelsen til påmeldingen er behandlet (sveiper_kjort) og
    `rest_faktura` om det gjenstår en faktura eller delfaktura som ikke er laget (sveiper.fakturering_gjenstaar): det avgjør hva
    som skjer når en ekstradeltaker settes tilbake til Påmeldt.

    Bare Påmeldt tar en plass. Ekstradeltaker tar ingen: Påmeldt -> Ekstradeltaker frigjør en plass, Ekstradeltaker -> Påmeldt
    krever en ledig plass, og fra/til alle andre statuser rykker ingen opp for en ekstradeltaker."""
    tekst = [f"Endre status for {deltaker} fra {_navn(gammel)} til {_navn(ny)}?"]
    if ny == "ekstradeltaker":
        if kurs["status"] in _LUKKET and gammel in db.HAR_PLASS:
            # Påmeldt -> Ekstradeltaker etter kursslutt: ingen ny kommer på kurset, så bare merkelappen (og utvalget) endres
            tekst.append(f"Kurset er {kurs['status']}: bare merkelappen endres. Ingen rykker opp fra ventelisten, og ingenting sendes "
                         "eller faktureres.")
            if flere_samlinger:
                tekst.append("Du velger hvilke samlinger deltakeren deltar på (som standard hele kurset).")
            return " ".join(tekst)
        if kurs["status"] in _LUKKET:                      # db.sett_paamelding_status avviser det
            tekst.append(f"Kurset er {kurs['status']}, så deltakeren kan ikke settes til {_navn(ny).lower()}.")
            return " ".join(tekst)
        tekst.append("Deltakeren er på kurset, men tar ikke plass og teller ikke som " + _navn("paameldt").lower()
                     + (" – heller ikke når kurset er fullt." if gammel != "paameldt" else "."))
        if gammel == "paameldt":
            tekst.append(_plass_tekst(kurs, antall_paameldt, med_epost=True))
        elif gammel == "venteliste":
            tekst.append("Deltakeren tas av ventelisten.")
        if flere_samlinger:
            tekst.append("Du velger hvilke samlinger deltakeren deltar på (som standard hele kurset).")
        tekst.append(HOLDT_EKSTRADELTAKER)
        return " ".join(tekst)
    if ny == "paameldt":
        if kurs["status"] in _LUKKET and gammel != "ekstradeltaker":     # db.sett_paamelding_status avviser det
            tekst.append(f"Kurset er {kurs['status']}, så deltakeren kan ikke settes til {_navn(ny).lower()}.")
        elif kurs["status"] in _LUKKET:
            tekst.append(f"Kurset er {kurs['status']}: bare merkelappen endres (ingen ny kommer på kurset, så det kreves ingen ledig plass). "
                         "Samlingsutvalget fjernes. Ingenting sendes eller faktureres.")
        elif gammel == "ekstradeltaker":
            tekst.append("Deltakeren tar en plass og teller som " + _navn("paameldt").lower() + ", og det krever en ledig plass. "
                         "Samlingsutvalget fjernes: deltakeren er på hele kurset.")
            tekst.append(retur_tekst(kurs, fakturert=fakturert, behandlet=behandlet, rest_faktura=rest_faktura))
        else:
            tekst.append("Deltakeren får bekreftelse på e-post (hvis den ikke er sendt før)"
                         + (", og fakturaen behandles etter kursets oppsett." if _har_faktura(kurs) else "."))
    elif ny == "venteliste":
        if gammel == "paameldt":
            tekst.append(_plass_tekst(kurs, antall_paameldt, med_epost=False))
        elif gammel == "ekstradeltaker":
            tekst.append("Deltakeren har ikke plass: en ekstradeltaker tok ingen plass, så ingen rykker opp fra ventelisten. "
                         "Samlingsutvalget fjernes.")
        tekst.append("Deltakeren får ventelistebeskjed på e-post hvis den ikke er sendt før.")
    elif ny in _AVSLUTTENDE:
        if gammel == "paameldt":
            tekst.append(_plass_tekst(kurs, antall_paameldt, med_epost=True))
        elif gammel == "ekstradeltaker":
            tekst.append(f"{_navn('ekstradeltaker')}en tok ingen plass, så ingen rykker opp fra ventelisten. Samlingsutvalget fjernes.")
        elif gammel == "venteliste":
            tekst.append("Deltakeren tas av ventelisten.")
        tekst.append("Deltakeren får ingen e-post."
                     + (" Vil du sende avslag på e-post, bruk «Avslå påmeldingen» i deltakervinduet."
                        if ny == "avslatt" and gammel in ("paameldt", "ekstradeltaker", "venteliste") else ""))
        if fakturert:
            tekst.append("Deltakeren er allerede fakturert. Fakturaen krediteres ikke automatisk – det må gjøres i Visma.")
    if gammel == "ekstradeltaker" and ny != "paameldt":
        tekst.append(f"Får deltakeren plass igjen senere, blir statusen {_navn('paameldt')}.")
    return " ".join(tekst)


def retur_tekst(kurs, *, fakturert: bool, behandlet: bool, rest_faktura: bool = False) -> str:
    """Hva som skjer med bekreftelse og faktura når en ekstradeltaker settes tilbake til Påmeldt (samme regel som
    db.sett_paamelding_status, se dens docstring): bekreftelsen sendes aldri to ganger, og en faktura lages aldri av seg selv når
    bekreftelsen alt er behandlet - da bestemmer administrator selv (systemet ser ikke fakturaer som er laget utenfor det, f.eks. i Visma).
    Brukes i dialogen og i spørsmålet om overbooking (et fullt kurs spør om plassen først, og skal ikke skjule dette)."""
    if not behandlet:
        return ("Bekreftelsen er ikke sendt ennå. Den sendes nå automatisk, som for alle andre påmeldte"
                + (", sammen med fakturaen til kursets vanlige pris. Har du allerede fakturert deltakeren selv, bør du ikke gjøre dette."
                   if _har_faktura(kurs) else "."))
    if fakturert and rest_faktura and _har_faktura(kurs):
        return ("Bekreftelsen er allerede behandlet, så ingenting sendes på nytt, og fakturaene som er laget, endres ikke og krediteres ikke. "
                "Systemet lager IKKE resten av fakturaene (delfakturaene) av seg selv: påmeldingen holdes tilbake, og du velger selv "
                "«Behandle fakturering nå» i deltakervinduet hvis systemet skal lage de som gjenstår (alle forfalte delfakturaer lages da). "
                "Har du fakturert resten selv, skal du ikke trykke på den.")
    if fakturert or not _har_faktura(kurs):
        return ("Bekreftelsen er allerede behandlet, så ingenting sendes på nytt"
                + (", og fakturaen som finnes, endres ikke og krediteres ikke." if fakturert else "."))
    return ("Bekreftelsen er allerede sendt og sendes ikke på nytt. Systemet har ingen faktura til deltakeren, og lager ingen av seg "
            "selv: påmeldingen holdes tilbake, og du velger selv «Behandle fakturering nå» i deltakervinduet hvis systemet skal lage "
            "fakturaen til kursets vanlige pris (er kurset delt i samlinger, lages alle forfalte delfakturaer da). Har du fakturert "
            "deltakeren selv, skal du ikke trykke på den.")


def _plass_tekst(kurs, antall_paameldt: int | None, *, med_epost: bool) -> str:
    """Hva som skjer med plassen når en påmeldt får en annen status. Samme regel som db._rykk_opp: den første på
    ventelisten rykker opp bare når kurset får en ledig plass - ikke på et avlyst eller avsluttet kurs, og ikke når kurset
    er overbooket (flere påmeldte enn plasser, så det er fortsatt fullt etter at denne har gått)."""
    if kurs["status"] in _LUKKET:
        return f"Kurset er {kurs['status']}, så ingen rykker opp fra ventelisten."
    kapasitet = kurs["kapasitet"]
    if kapasitet is not None and antall_paameldt is not None and antall_paameldt > kapasitet:
        return (f"Kurset er overbooket ({antall_paameldt} {db.PAAMELDINGSSTATUSER_FLERTALL['paameldt'].lower()} på "
                f"{kapasitet} {'plass' if kapasitet == 1 else 'plasser'}), så plassen blir ikke ledig og ingen rykker "
                "opp fra ventelisten.")
    return ("Plassen blir ledig. Står noen på ventelisten, rykker den første opp og får plassen"
            + (" (med bekreftelse på e-post)." if med_epost else "."))


def fullt_kurs_tekst(deltaker: str, antall: int, kapasitet: int) -> str:
    """Spørsmålet når kurset er fullt og admin likevel gir noen en plass som «Påmeldt» (overbooking, som i deltakervinduet).
    En ekstradeltaker tar ingen plass og er aldri en overbooking."""
    return (f"Er du sikker på at du vil melde {deltaker} på? Kurset er fullt ({antall} av {kapasitet} "
            f"{'plass' if kapasitet == 1 else 'plasser'} er tatt).")


def _har_faktura(kurs) -> bool:
    return kurs["fakturering"] == "person" and bool(kurs["pris_nok"])
