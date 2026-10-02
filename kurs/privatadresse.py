"""Deltakerens PRIVATE adresse og fakturaen: ÉN felles regel for alle veier (brukerens beslutning 01.10.2026).

Regelen:
  * For alle nye, reelle deltakere er privat adresse, postnummer og poststed ALLTID påkrevd, også når arbeidsgiver/firma betaler
    (se skjemafelt.valider_adresse og config.ADRESSE_KREVES_I_WEBHOOK/_CSV). Firmaets adresse hentes fortsatt separat fra
    Enhetsregisteret (firmaopplysninger.py).
  * En PRIVAT faktura (deltakeren betaler selv) opprettes og sendes ALDRI uten komplett privat adresse. Mangler den, holdes
    faktureringen tilbake (`faktura_adresse` gir None, og sveiper._opprett lager ingen faktura), og administrator får beskjed:
    merket «Privat adresse mangler» i deltakerlisten, en varselboks i deltakervinduet, et kort på Oversikten og meldingen etter
    «Send bekreftelse og behandle fakturering nå». Fakturaen lages av seg selv når adressen er lagt inn.
  * Firma som betaler faktureres på firmaets adresse. Manglende privat adresse holder aldri tilbake en firmafaktura.
  * Eldre/test-deltakere uten adresse kan fortsatt redigeres uten at adressen må fylles inn først (db.oppdater_deltaker).
  * Webhook-NØDBREMSEN (ADRESSE_KREVES_I_WEBHOOK=0, av som standard) tar inn påmeldingen selv om adressen mangler. Den er da
    merket «Privat adresse mangler», og fakturaen holdes tilbake til adressen er komplett (samme regel som over).

Merket og holdet er ikke egne kolonner, men følger av dataene: de forsvinner av seg selv når adressen er lagt inn. Ingenting
skrives når fakturaen venter (heller ingen logg), så morgenjobben forblir idempotent: raden tas igjen neste kjøring.
Modulen leser bare databasen; kalleren committer.
"""
from . import db, skjemafelt

MERKE = skjemafelt.ADRESSE_MANGLER_MERKE            # «Privat adresse mangler» (ingenting lagt inn)
MERKE_UFULLSTENDIG = skjemafelt.ADRESSE_UFULLSTENDIG_MERKE      # «Privat adresse er ufullstendig» (bare noen av feltene)
FAKTURA_VENTER = "Venter på privat adresse"         # fakturastatus i deltakerlisten og deltakervinduet
FAKTURA_HOLDT = "Fakturering holdt tilbake: privat adresse mangler"      # overskriften når ingenting er lagt inn (og i oversikter)
FAKTURA_HOLDT_UFULLSTENDIG = "Fakturering holdt tilbake: privat adresse er ufullstendig"
FAKTURA_HOLDT_GENERELT = "Fakturering holdt tilbake: privat adresse mangler eller er ufullstendig"      # kort og lister
VENTER_TEKST = "privat adresse mangler eller er ufullstendig"          # i meldinger fra morgenjobben og fakturamotoren
TEKST_HOLDT = ("Fakturaen lages ikke før privat adresse, postnummer og poststed alle er lagt inn. Fyll inn det som mangler under "
               "Personopplysninger og lagre, så lages fakturaen av seg selv.")


def holdt_overskrift(person) -> str:
    """Overskriften i varselboksen: «… privat adresse mangler» eller «… privat adresse er ufullstendig» (en delvis adresse tas vare på,
    men gjelder aldri som adresse). `person` har adresse, postnr og poststed."""
    return FAKTURA_HOLDT_UFULLSTENDIG if skjemafelt.adresse_status(person) == "ufullstendig" else FAKTURA_HOLDT

_KOPI = ("faktura_adresse", "faktura_postnr", "faktura_sted")      # adressen på påmeldingen (kopi fra registreringen)
_PERSON = skjemafelt.ADRESSEFELT                                    # adressen på personen (adresse, postnr, poststed)


def komplett(tre) -> bool:
    """Er alle tre delene (adresse, postnr, poststed) fylt ut? Mellomrom alene teller som tomt."""
    return all(isinstance(v, str) and v.strip() for v in tre)


def velg_adresse(kopi, person):
    """Den komplette PRIVATE fakturaadressen (adresse, postnr, sted), eller None når ingen av kildene er komplett.

    Kopien på påmeldingen vinner når den er komplett (administrator kan ha skrevet en egen), ellers brukes personens egen adresse
    når den er komplett. En delvis adresse brukes aldri: fakturaen skal ikke sendes til en ufullstendig adresse."""
    for kilde in (tuple(kopi), tuple(person)):
        if komplett(kilde):
            return tuple(v.strip() for v in kilde)
    return None


def faktura_adresse(con, p):
    """Til fakturamotoren: den komplette private fakturaadressen til en PRIVAT betaler, eller None (fakturaen må vente).
    `p` er en påmeldingsrad med deltaker_id og faktura_adresse/-postnr/-sted."""
    d = con.execute("SELECT adresse, postnr, poststed FROM deltaker WHERE id=?", (p["deltaker_id"],)).fetchone()
    return velg_adresse([p[k] for k in _KOPI], [d[k] for k in _PERSON] if d else [None] * 3)


# Påmeldinger som skal få en automatisk, privat faktura og ikke har fått noen ennå. Tilsvarer sveiper.skal_faktureres (bekreftet, kurset
# fakturerer per person med pris, ikke ekstradeltaker) og betaler != organisasjon (som _opprett). Anonymiserte personer er ute.
_SQL_KANDIDATER = """
    SELECT p.id, p.kurs_id, p.status, p.betaler, p.deltaker_id, p.sveiper_utsatt, p.faktura_adresse, p.faktura_postnr, p.faktura_sted,
           d.adresse, d.postnr, d.poststed, d.navn, k.kursnr, k.kode, k.navn AS kursnavn, k.status AS kursstatus
    FROM paamelding p JOIN kurs k ON k.id=p.kurs_id JOIN deltaker d ON d.id=p.deltaker_id
    WHERE p.status='bekreftet' AND p.ekstradeltaker_ts IS NULL AND COALESCE(p.betaler, 'person')!='organisasjon'
      AND k.fakturering='person' AND k.pris_nok > 0 AND k.status!='avlyst'
      AND d.epost NOT LIKE ?
      AND NOT EXISTS (SELECT 1 FROM faktura f WHERE f.paamelding_id=p.id)"""


def liste(con, kurs_id: int | None = None, paamelding_id: int | None = None, deltaker_id: int | None = None, *,
          uten_avsluttede: bool = False) -> list:
    """Påmeldinger der fakturaen venter på privat adresse (eldste først): privat betaler, automatisk faktura ventet, ingen faktura
    ennå, og ingen komplett privat adresse. Valgfritt for ett kurs, én påmelding eller én person. `uten_avsluttede`: hopp over
    kurs som er avsluttet (Oversikten og listene)."""
    sql = (_SQL_KANDIDATER + (" AND p.kurs_id=?" if kurs_id else "") + (" AND p.id=?" if paamelding_id else "")
           + (" AND p.deltaker_id=?" if deltaker_id else ""))
    sql += " AND k.status!='avsluttet'" if uten_avsluttede else ""
    argumenter = ["%@" + db.ANONYM_DOMENE, *([kurs_id] if kurs_id else []), *([paamelding_id] if paamelding_id else []),
                  *([deltaker_id] if deltaker_id else [])]
    rader = con.execute(sql + " ORDER BY p.id", argumenter).fetchall()
    return [r for r in rader if velg_adresse([r[k] for k in _KOPI], [r[k] for k in _PERSON]) is None]


def venter(con, paamelding_id: int) -> bool:
    """Venter fakturaen til denne påmeldingen på privat adresse akkurat nå?"""
    return bool(liste(con, paamelding_id=paamelding_id))
