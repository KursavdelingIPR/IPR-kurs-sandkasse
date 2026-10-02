"""Testdata: deltakerens PRIVATE adresse er påkrevd i alle veier inn (adresse, postnr, poststed). Alle adresser her er
oppdiktet. Hjelperne gjør det enkelt å legge en gyldig adresse til et skjema uten å svekke testen."""

ADRESSE = {"adresse": "Eksempelveien 1", "postnr": "0150", "poststed": "Oslo"}
ANNEN_ADRESSE = {"adresse": "Prøvegata 22", "postnr": "5003", "poststed": "Bergen"}
# Bevisst uvanlig tekst, så en lekkasje til logg, e-post eller eksport er lett å finne
SPORBAR_ADRESSE = {"adresse": "Sporbarveien 4711", "postnr": "4711", "poststed": "Sporbarby"}


def gruppeadresse(antall: int = 1, adresse: dict | None = None) -> dict:
    """Adressefeltene i bedriftspåmeldingen: ett sett per deltakerrad (lister, i samme rekkefølge som deltakerne)."""
    a = adresse or ADRESSE
    return {"deltaker_adresse": [a["adresse"]] * antall, "deltaker_postnr": [a["postnr"]] * antall,
            "deltaker_poststed": [a["poststed"]] * antall}


def csv_med_adresse(header: str, rader, adresse: bool = True) -> bytes:
    """CSV til importtestene. Adressekolonnene (Adresse, Postnr, Poststed) legges sist i headeren og med en gyldig,
    oppdiktet adresse sist på hver rad (rader som er kortere enn headeren, fylles ut først, så adressen havner i riktig
    kolonne; tomme linjer beholdes). `adresse=False` gir filen uten adressekolonner, til å teste at de kreves."""
    if not adresse:
        return "\n".join([header, *rader]).encode("utf-8-sig")
    kolonner = header.count(";") + 1
    ut = []
    for r in (linje for rad in rader for linje in rad.split("\n")):       # en «rad» kan være flere linjer
        if r.strip():
            r = r + ";" * max(0, kolonner - 1 - r.count(";")) + ";" + ";".join(ADRESSE.values())
        ut.append(r)
    return "\n".join([header + ";Adresse;Postnr;Poststed", *ut]).encode("utf-8-sig")
