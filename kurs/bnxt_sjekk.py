"""Tilkoblingstest mot Visma Business NXT - KUN lesing:  python -m kurs.bnxt_sjekk [--faktura NR]

Kjoeres manuelt i terminalen for aa kontrollere oppsettet mot det EKTE Business NXT foer statusvisningen tas i bruk:
  1. hvilke innstillinger som er satt (hemmeligheten vises aldri - bare «satt» eller «mangler»)
  2. tilgang med scopet for bare lesing (et nytt tilgangstoken; selve tokenet skrives aldri ut)
  3. selskapene tjenesten har tilgang til (krever BNXT_KUNDENR) - der finner du verdien til BNXT_SELSKAP
  4. lesetilgang til kundetransaksjonene i selskapet (antall rader)
  5. med --faktura NR: statusfeltene for én faktura (ingen kundenavn eller adresser hentes)

Lagrer ingenting og endrer ingenting. Dette er det eneste stedet som leser ekte data fra Business NXT mens sandkassen
staar i demo - et bevisst, avgrenset unntak fra regel 3 i CLAUDE.md: du kjoerer det selv, og utskriften gaar bare til
terminalen din.

Avslutningskode: 0 = alt OK, 1 = innstillinger mangler, 2 = tilgangen feilet, 3 = et oppslag feilet.
"""
import sys

from . import config
from .integrasjoner import business_nxt as bnxt


def _dato(d) -> str:
    return d.strftime("%d.%m.%Y") if d else "–"


def _vis_feil(e: bnxt.BnxtFeil) -> None:
    print(f"STOPP: {e}")
    for linje in e.detaljer:
        print(f"  Business NXT: {linje}")


def _vis_faktura(nr: str) -> int:
    try:
        o = bnxt.hent_fakturastatus({nr: None}, ekte=True)[nr]
    except bnxt.BnxtFeil as e:
        _vis_feil(e)
        return 3
    if o.problem:
        print(f"  Faktura {nr}: {o.problem}")
        return 3
    s = o.status
    print(f"  Faktura {s.fakturanr}  (kundenr. {s.kundenr if s.kundenr is not None else '–'})")
    print(f"    Fakturadato:  {_dato(s.fakturadato)}")
    print(f"    Forfall:      {_dato(s.forfallsdato)}")
    print(f"    Beløp:        {bnxt.kroner(s.belop)}")
    print(f"    Utestående:   {bnxt.kroner(s.utestaende)}")
    if s.kreditert:
        print(f"    Kreditert:    {bnxt.kroner(s.kreditert)}")
    print(f"    Status:       {s.status}" + (f" (siste innbetaling {_dato(s.sist_betalt)})" if s.sist_betalt else ""))
    print(f"    Purringer:    {s.antall_purringer}" + (f" (siste {_dato(s.siste_purring)})" if s.siste_purring else ""))
    if s.inkasso:
        print(f"    Inkasso:      {s.inkasso}")
    return 0


def kjor(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    faktura = argv[argv.index("--faktura") + 1] if "--faktura" in argv and argv.index("--faktura") + 1 < len(argv) else None

    print("Visma Business NXT – tilkoblingstest (bare lesing, lagrer ingenting)")
    print(f"  BNXT_CLIENT_ID:     {config.BNXT_CLIENT_ID or 'MANGLER'}")
    print(f"  BNXT_CLIENT_SECRET: {'satt (vises ikke)' if config.BNXT_CLIENT_SECRET else 'MANGLER'}")
    print(f"  BNXT_KUNDENR:       {config.BNXT_KUNDENR or 'ikke satt (trengs bare for å liste selskapene)'}")
    print(f"  BNXT_SELSKAP:       {config.BNXT_SELSKAP or 'ikke satt'}")
    if not (config.BNXT_CLIENT_ID and config.BNXT_CLIENT_SECRET):
        print("STOPP: Sett BNXT_CLIENT_ID og BNXT_CLIENT_SECRET som miljøvariabler og åpne en ny terminal (se OPERATIONS.md).")
        return 1

    try:
        tilgang = bnxt.sjekk_tilgang(ekte=True)
    except bnxt.BnxtFeil as e:
        _vis_feil(e)
        return 2
    if tilgang["scope"] and bnxt.SCOPE not in tilgang["scope"].split():
        print(f"STOPP: Visma Connect ga et annet scope enn bare lesing ({tilgang['scope']}).")
        return 2
    print(f"Tilgang OK (bare lesing). Tilgangstokenet varer i {tilgang['minutter']} minutter.")

    kundenumre = [config.BNXT_KUNDENR] if config.BNXT_KUNDENR else []
    if not kundenumre:
        try:
            kunder = bnxt.tilgjengelige_kunder(ekte=True)
        except bnxt.BnxtFeil as e:
            _vis_feil(e)
            kunder = []
        print(f"Visma.net-kunder tjenesten er koblet til ({len(kunder)}):")
        for k in kunder:
            print(f"  {k['navn']}  –  Visma.net-kundenummer {k['kundenr']}")
        kundenumre = [str(k["kundenr"]) for k in kunder if k["kundenr"]]
    for kundenr in kundenumre:
        try:
            selskaper = bnxt.tilgjengelige_selskaper(kundenr, ekte=True)
        except bnxt.BnxtFeil as e:
            _vis_feil(e)
            return 3
        print(f"Selskaper tjenesten har tilgang til ({len(selskaper)}):")
        for s in selskaper:
            print(f"  {s['navn']}  –  Visma.net-selskaps-ID {s['selskaps_id']}")

    if not config.BNXT_SELSKAP:
        print("BNXT_SELSKAP er ikke satt: velg selskaps-ID-en fra listen over, sett den som miljøvariabel og kjør på nytt.")
        return 0 if faktura is None else 1
    try:
        print(f"Lesetilgang OK: {bnxt.antall_kundetransaksjoner(ekte=True)} kundetransaksjoner i selskapet.")
    except bnxt.BnxtFeil as e:
        _vis_feil(e)
        if "ACCESS_DENIED" in str(e) or any("tenant" in d for d in e.detaljer):
            print("  Tips: BNXT_SELSKAP skal være Visma.net-selskaps-ID-en (et langt tall, f.eks. 9112233) fra listen over,")
            print("  ikke firmanummeret inne i Business NXT. Står selskapet ikke i listen, er tjenestebrukeren ikke koblet til det.")
        return 3
    return _vis_faktura(faktura) if faktura else 0


if __name__ == "__main__":
    sys.exit(kjor())
