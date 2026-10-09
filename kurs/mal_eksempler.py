"""Fiktive eksempeldata for admin-forhaandsvisning av redigerbare e-postmaler (fase 12B3A).

KUN til forhaandsvisning. Aldri ekte deltaker-/kurs-/firmadata fra databasen, aldri en tilfeldig ekte rad. Samme
data sendes baade til Kjoring.render_for_sending() (som gir den EFFEKTIVE malteksten - override hvis den finnes,
ellers standard - noeyaktig som en ekte utsending) og videre til epost.render() sine LAASTE systemblokker i selve
malfilen. Forhaandsvisningen bruker dermed noeyaktig samme rendringsvei som faktisk utsending, bare med paadiktede
verdier - ingen parallell "preview-renderer", ingen claim, ingen epost.send, ingen utsending_logg-rad.

For maler med flere reelle varianter (dagfor: forste/midt/siste kursdag)
returnerer eksempler() flere (variantnavn, data) - en for hver variant - slik at admin kan se dem alle.
"""
from datetime import date, timedelta

from .sveiper import PLAN_NA, FakturaPlan

_OM = date.today() + timedelta(days=30)

KURS = {"navn": "Eksempelkurs i emosjonsfokusert terapi", "type": "fysisk", "sted": "IPR, Bergen",
        "start_kl": "09:00", "slutt_kl": "16:00", "pris_nok": 4500, "fakturering": "person",
        "faktura_dager_for": 14, "notat": None, "zoom_url": None, "zoom_id": None, "zoom_pw": None}
KURS_DIGITALT = {**KURS, "type": "digital", "sted": None,
                 "zoom_url": "https://zoom.eksempel.no/j/1234567890", "zoom_id": "123 456 7890", "zoom_pw": "eksempel-pw"}
DELTAKER = {"navn": "Ola Nordmann", "fornavn": "Ola", "betaler": "person", "betaling": "samlet", "org_navn": None}
DAG1 = {"dato": _OM.isoformat(), "start_kl": None, "slutt_kl": None}
DAG2 = {"dato": (_OM + timedelta(days=1)).isoformat(), "start_kl": None, "slutt_kl": None}
DAG3 = {"dato": (_OM + timedelta(days=2)).isoformat(), "start_kl": None, "slutt_kl": None}


def eksempler(mal: str) -> list[tuple[str, dict]]:
    """[(variantnavn, data)] for en redigerbar mal. Tom variantnavn ("") betyr at malen bare har én variant."""
    if mal == "bekreftelse":
        return [("", dict(p=DELTAKER, kurs=KURS, dager=[DAG1], faktura_plan=FakturaPlan(PLAN_NA)))]
    if mal == "venteliste":
        return [("", dict(p=DELTAKER, kurs=KURS))]
    if mal == "avlysning":
        return [("", dict(d=DELTAKER, kurs=KURS))]
    if mal == "ukefor":
        return [("", dict(d=DELTAKER, kurs=KURS_DIGITALT, dager=[DAG1, DAG2, DAG3]))]
    if mal == "dagfor":
        return [
            ("Første kursdag", dict(d=DELTAKER, kurs=KURS_DIGITALT, dag=DAG1, nr=1, antall=3)),
            ("Dag midt i kurset", dict(d=DELTAKER, kurs=KURS_DIGITALT, dag=DAG2, nr=2, antall=3)),
            ("Siste kursdag", dict(d=DELTAKER, kurs=KURS_DIGITALT, dag=DAG3, nr=3, antall=3)),
        ]
    if mal == "kursbevis_klar":
        return [("", dict(navn="Ola Nordmann", fornavn="Ola", kurs=KURS))]
    if mal == "evaluering":
        return [("", dict(d=DELTAKER, kurs=KURS, evaluering_url="/evaluering/eksempel"))]
    if mal == "firmapaamelding_kvittering":
        return [("", dict(kontakt={"navn": "Kari HR", "fornavn": "Kari", "firmanavn": "Eksempel AS"}, kurs=KURS,
                          kvittering_url="/kurs/EKS-1/gruppe/kvittering/eksempel-token",
                          antall_totalt=3, antall_bekreftet=2, antall_venteliste=1, antall_feilet=0))]
    raise ValueError(f"Ingen eksempeldata for mal {mal!r}")
