"""Databasemigrering som eget, idempotent steg:  python -m kurs.migrer

Oppretter manglende tabeller/indekser, legger til manglende kolonner og den aller forste admin-brukeren (db.init).
Kan kjoeres flere ganger uten skade. Bruker samme backend-valg som appen: DATABASE_URL satt -> PostgreSQL, ellers
SQLite i DB_STI.

I drift (Azure) kjoeres dette som eget steg i deploy-loepet eller manuelt via SSH - ALDRI av gunicorn/startup.py,
slik at flere instanser/workere aldri migrerer samtidig og oppstart ikke endrer databasen.

Sikkerhet: i prod (MODUS=prod) nektes det aa opprette den forste admin-brukeren med standard- eller et for kort
passord. Tilkoblingsstrengen (som inneholder passord) skrives aldri ut.
"""
import sys

from . import config, db

MIN_PASSORDLENGDE = 12


def _backend_navn() -> str:
    return "PostgreSQL (DATABASE_URL)" if config.DATABASE_URL else f"SQLite ({config.DB_STI})"


def kjor() -> int:
    print(f"Migrerer database: {_backend_navn()}")
    con = db.koble()
    try:
        if (not config.DEMO and not db.har_admin_bruker(con)
                and (config.ADMIN_PASSORD in ("", "demo") or len(config.ADMIN_PASSORD) < MIN_PASSORDLENGDE)):
            print(f"STOPP: Databasen har ingen admin-bruker ennaa. Sett ADMIN_BRUKERNAVN og et sterkt ADMIN_PASSORD "
                  f"(minst {MIN_PASSORDLENGDE} tegn, ikke «demo») foer migreringen kjoeres i prod. Ingenting er endret.")
            return 2
        db.init(con)
        print("Migrering fullfoert.")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(kjor())
