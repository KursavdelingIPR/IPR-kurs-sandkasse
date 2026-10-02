"""Deltakersøkets JavaScript i en ekte nettleser (tests/nettleser/sok_scenario.js).

De andre testene sjekker serveren og JavaScript-kilden som tekst. Rullegardinen (ventetid, piltaster, Enter, Esc, «/»,
feilmeldinger, at gamle svar ikke vises) er hele «enkelt tilgjengelig»-kravet, og den virker bare i en nettleser. Testen
starter appen lokalt med oppdiktede data, kjører scenarioet i Edge eller Chrome uten vindu (via DevTools-protokollen, uten
ekstra pakker) og krever at alle kontrollene er grønne.

Hoppes over når Node (versjon 22 eller nyere) eller Edge/Chrome ikke finnes. Sett IPR_NETTLESER_STI til nettleserens
programfil hvis den ligger et uvanlig sted.
"""
import json
import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from kurs import config
from kurs.web import app as webapp
from test_deltakersok import _bygg, _kurs, _meld, con  # noqa: F401

MAPPE = Path(__file__).parent / "nettleser"
NETTLESERE = (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              "/usr/bin/microsoft-edge", "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
              "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def _finn_nettleser() -> str | None:
    for sti in (os.environ.get("IPR_NETTLESER_STI"), *NETTLESERE):
        if sti and Path(sti).is_file():
            return sti
    return next((s for s in (shutil.which(n) for n in ("msedge", "google-chrome", "chromium")) if s), None)


def _finn_node() -> str | None:
    node = shutil.which("node")
    if not node:
        return None
    try:                       # innebygd WebSocket kom i Node 22
        ok = subprocess.run([node, "-e", "process.exit(typeof WebSocket === 'function' ? 0 : 1)"], timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None
    return node if ok else None


def _ledig_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _bygg_data(con):
    """De vanlige testdataene pluss nok «Kari» til at rullegardinen blir full (8 av 13), en person med HTML i navnet og æøå."""
    _bygg(con)
    kid = _kurs(con, "FYL-2027-5", "Fylleskurs", ["2027-09-01"])
    for i in range(10):
        _meld(con, kid, "Kari", f"Fyll{i}", f"kari.fyll{i}@eksempel.no")
    _meld(con, kid, "Xena", "<i>Kursiv</i>", "xena.kursiv@eksempel.no")
    _meld(con, kid, "Sølvi", "Tørresen", "st1970@example.no")
    con.commit()


def test_soekets_javascript_i_ekte_nettleser(con):
    nettleser, node = _finn_nettleser(), _finn_node()
    if not nettleser or not node:
        pytest.skip("krever Node 22+ og Edge eller Chrome (sett IPR_NETTLESER_STI om nettleseren ligger et uvanlig sted)")
    _bygg_data(con)
    tjener = make_server("127.0.0.1", 0, webapp.app, threaded=True)
    traad = threading.Thread(target=tjener.serve_forever, daemon=True)
    traad.start()
    try:
        prove = subprocess.run(
            [node, "sok_scenario.js", str(tjener.server_port), str(_ledig_port()), config.ADMIN_BRUKERNAVN, config.ADMIN_PASSORD],
            cwd=MAPPE, env={**os.environ, "IPR_NETTLESER_STI": nettleser}, capture_output=True, text=True, encoding="utf-8",
            timeout=300)
    finally:
        tjener.shutdown()
        traad.join(timeout=10)
    linje = next((l for l in prove.stdout.splitlines() if l.startswith("RESULTAT_JSON:")), None)
    assert linje, f"scenarioet ga ikke resultat:\n{prove.stdout[-1500:]}\n{prove.stderr[-1500:]}"
    resultat = json.loads(linje[len("RESULTAT_JSON:"):])
    feil = [f"{r['navn']} – {r['detalj']}" for r in resultat if not r["ok"]]
    assert not feil, "Feil i nettleseren:\n" + "\n".join(feil)
    assert len(resultat) >= 60                        # scenarioet ble kjørt ferdig, ikke bare startet
