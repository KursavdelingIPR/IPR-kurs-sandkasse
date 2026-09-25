"""Er porten ledig?  python -m kurs.portsjekk 5000

Brukes av start.bat foer den lokale serveren startes. Avslutningskode 0 = ledig, 1 = opptatt (med en forklaring og
hvordan den gamle serveren stoppes). Ingen prosesser roeres - brukeren bestemmer.

Paa Windows finnes PID-en til prosessen som lytter via `netstat -ano`; den vises slik at brukeren kan stoppe riktig
prosess (og bare den) hvis det ikke er et aapent sandkasse-vindu.
"""
import socket
import subprocess
import sys


def port_ledig(port: int, vert: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((vert, port)) != 0


def lyttende_pid(port: int) -> str | None:
    """PID for prosessen som lytter paa porten (kun Windows/netstat; None ellers eller hvis ukjent)."""
    if not sys.platform.startswith("win"):
        return None
    try:
        ut = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for linje in ut.splitlines():
        deler = linje.split()
        if len(deler) >= 5 and deler[0] == "TCP" and deler[1].endswith(f":{port}") and deler[3] == "LISTENING":
            return deler[4]
    return None


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    port = int(argv[0]) if argv else 5000
    if port_ledig(port):
        return 0
    pid = lyttende_pid(port)
    print(f"  Port {port} er allerede i bruk - sannsynligvis kjoerer sandkassen fra foer.")
    print("  Se etter et svart vindu med «IPR Paameldingssystem» og lukk det, eller bruk det som allerede kjoerer:")
    print(f"  http://127.0.0.1:{port}")
    if pid:
        print(f"  Finner du ikke vinduet, kan prosessen (PID {pid}) stoppes fra Oppgavebehandling eller med:")
        print(f"      taskkill /PID {pid} /F")
    else:
        print("  Finner du ikke vinduet: aapne Oppgavebehandling og avslutt python.exe som hoerer til sandkassen.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
