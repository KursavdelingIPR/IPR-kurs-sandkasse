"""start.bat sin portsjekk (kurs/portsjekk.py): start aldri en server nummer to i det stille."""
import socket
from pathlib import Path

from kurs import portsjekk

ROT = Path(__file__).resolve().parent.parent


def test_ledig_port_gir_0(capsys):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        ledig = s.getsockname()[1]
    assert portsjekk.main([str(ledig)]) == 0
    assert capsys.readouterr().out == ""


def test_opptatt_port_gir_1_og_forklaring_uten_aa_roere_prosessen(capsys):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        assert portsjekk.main([str(port)]) == 1
        ut = capsys.readouterr().out
        assert f"Port {port} er allerede i bruk" in ut and f"http://127.0.0.1:{port}" in ut
        assert "taskkill" not in ut or "PID" in ut          # kommandoen vises kun sammen med en identifisert PID
        assert portsjekk.port_ledig(port) is False          # serveren lever fortsatt - ingenting ble drept


def test_start_bat_sjekker_porten_foer_serveren_startes():
    bat = (ROT / "start.bat").read_text(encoding="utf-8")
    sjekk, start = bat.index("kurs.portsjekk 5000"), bat.index("kjor.py")
    assert sjekk < start and "if errorlevel 1" in bat and "taskkill" not in bat   # bat-fila dreper aldri noe selv
