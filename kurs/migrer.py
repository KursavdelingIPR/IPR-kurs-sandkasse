"""Databasemigrering som eget, idempotent steg:  python -m kurs.migrer  [--status]

Oppretter manglende tabeller/indekser, legger til manglende kolonner (eldre databaser), kjoerer versjonerte migreringer
(kurs/migreringer.py) og den aller forste admin-brukeren (db.init). Kan kjoeres flere ganger uten skade. Bruker samme
backend-valg som appen: DATABASE_URL satt -> PostgreSQL, ellers SQLite i DB_STI.

I drift (Azure) kjoeres dette som eget steg i deploy-loepet eller manuelt via SSH - ALDRI av gunicorn/startup.py,
slik at flere instanser/workere aldri migrerer samtidig og oppstart ikke endrer databasen. Ta sikkerhetskopi foerst
(se MIGRATIONS.md).

--status: viser databasens versjon og kodens versjon uten aa endre noe (avslutningskode 0 = i takt, 1 = ikke i takt).

Sikkerhet: i prod (MODUS=prod) nektes det aa opprette den forste admin-brukeren med standard- eller et for kort
passord. Tilkoblingsstrengen (som inneholder passord) skrives aldri ut.
"""
import sys

from . import config, db, migreringer

MIN_PASSORDLENGDE = 12


def _backend_navn() -> str:
    return "PostgreSQL (DATABASE_URL)" if config.DATABASE_URL else f"SQLite ({config.DB_STI})"


def status(con) -> int:
    versjon = migreringer.gjeldende_versjon(con)
    print(f"Database: {_backend_navn()}")
    print(f"Databasens versjon: {versjon}   Kodens versjon: {migreringer.KODEVERSJON}")
    for nummer, navn, _ in migreringer.MIGRERINGER:
        print(f"  {nummer:>3}  {navn:<30} {'kjoert' if nummer <= versjon else 'MANGLER'}")
    return 0 if versjon == migreringer.KODEVERSJON else 1


def kjor(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    con = db.koble()
    try:
        if "--status" in argv:
            return status(con)
        print(f"Migrerer database: {_backend_navn()}")
        if (not config.DEMO and not db.har_admin_bruker(con)
                and (config.ADMIN_PASSORD in ("", "demo") or len(config.ADMIN_PASSORD) < MIN_PASSORDLENGDE)):
            print(f"STOPP: Databasen har ingen admin-bruker ennaa. Sett ADMIN_BRUKERNAVN og et sterkt ADMIN_PASSORD "
                  f"(minst {MIN_PASSORDLENGDE} tegn, ikke «demo») foer migreringen kjoeres i prod. Ingenting er endret.")
            return 2
        try:
            kjort = db.init(con)
        except migreringer.VersjonsFeil as e:
            print(f"STOPP: {e}")
            return 3
        for nummer in kjort:
            navn = next(n for nr, n, _ in migreringer.MIGRERINGER if nr == nummer)
            print(f"  migrering {nummer} ({navn}) kjoert")
        print(f"Migrering fullfoert. Databasens versjon: {migreringer.gjeldende_versjon(con)}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(kjor())
