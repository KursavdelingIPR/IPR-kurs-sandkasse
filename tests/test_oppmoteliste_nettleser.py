"""Oppmøtelisten i en ekte nettleser (tests/nettleser/oppmote_scenario.js).

De andre testene (test_oppmoteliste.py) sjekker serveren og JavaScript-kilden som tekst. Søket som filtrerer mens man skriver, trykk uten
sideomlasting, raske trykk, Zoom-bekreftelsen, «Merk alle», feilene (raden går tilbake), den faste topplinjen, mobilbredde og siden uten
JavaScript virker bare i en nettleser. Testen starter appen lokalt med oppdiktede data, kjører scenarioet i Edge eller Chrome uten vindu
(via DevTools-protokollen, uten ekstra pakker) og krever at alle kontrollene er grønne.

Hoppes over når Node (versjon 22 eller nyere) eller Edge/Chrome ikke finnes. Sett IPR_NETTLESER_STI til nettleserens programfil hvis den
ligger et uvanlig sted. Miljøvariabelen BILDEMAPPE gir skjermbilder når cdp.js har bilde() (se scenarioet).
"""
import json
import os
import subprocess
import threading
from datetime import timedelta

import pytest
from werkzeug.serving import make_server

from kurs import config, db, oppmoteliste
from kurs.web import app as webapp
from test_deltakersok_nettleser import MAPPE, _finn_node, _finn_nettleser, _ledig_port
from test_oppmoteliste import IDAG, _kurs, _meld, con  # noqa: F401

NAVN = ["Kari Hansen", "Ola Nordmann", "Sølvi Tørresen", "Åse Ødegård", "Håkon Åsen", "Ingrid Solberg-Eriksen", "Mohammed Al-Farouk",
        "Nina Bakke", "Øystein Berg", "Petter Dass", "Silje Lund", "Tor Arne Haugen-Mathiesen", "Vibeke Strand", "Zara Zed"]


def _bygg_data(con) -> dict:
    """Kurs med tre kursdager (i går, i dag, i morgen) og 14 deltakere; oppmøte fra QR, kode, Zoom og manuelt i dag, og fra Zoom i går."""
    kid = _kurs(con, "NL1", [IDAG - timedelta(days=1), IDAG, IDAG + timedelta(days=1)], navn="Emosjonsfokusert terapi – grunnkurs (eksempel)")
    pids = [_meld(con, kid, *n.split(" ", 1)) for n in NAVN]
    i_gaar, i_dag, i_morgen = (d["id"] for d in db.kursdager(con, kid))
    for pid, kilde, min_ in zip(pids[:4], ("qr", "kode", "zoom", "manuell"), (None, None, 95, None)):
        db.registrer_oppmote(con, pid, i_dag, kilde, min_)
    db.registrer_oppmote(con, pids[0], i_gaar, "qr")
    db.registrer_oppmote(con, pids[6], i_gaar, "zoom", 80)
    db.sett_paamelding_status(con, pids[12], "ekstradeltaker")          # har plass som Påmeldt: vises og kan merkes
    db.opprett_admin_bruker(con, "lese", "Lese Tilgang", "passord-som-holder", rolle="lese")
    con.commit()
    return {"kid": kid, "pids": pids, "navn": NAVN, "i_gaar": i_gaar, "i_dag": i_dag, "i_morgen": i_morgen, "stede_i_dag": 4,
            "navn_sortert": [r["navn"] for r in oppmoteliste.deltakere_for_dag(con, kid, i_dag)],
            "lese": "lese", "lese_passord": "passord-som-holder"}


def test_oppmotelisten_i_ekte_nettleser(con):
    nettleser, node = _finn_nettleser(), _finn_node()
    if not nettleser or not node:
        pytest.skip("krever Node 22+ og Edge eller Chrome (sett IPR_NETTLESER_STI om nettleseren ligger et uvanlig sted)")
    data = _bygg_data(con)
    tjener = make_server("127.0.0.1", 0, webapp.app, threaded=True)
    traad = threading.Thread(target=tjener.serve_forever, daemon=True)
    traad.start()
    try:
        prove = subprocess.run(
            [node, "oppmote_scenario.js", str(tjener.server_port), str(_ledig_port()), config.ADMIN_BRUKERNAVN, config.ADMIN_PASSORD, json.dumps(data)],
            cwd=MAPPE, env={**os.environ, "IPR_NETTLESER_STI": nettleser}, capture_output=True, text=True, encoding="utf-8", timeout=400)
    finally:
        tjener.shutdown()
        traad.join(timeout=10)
    linje = next((l for l in prove.stdout.splitlines() if l.startswith("RESULTAT_JSON:")), None)
    assert linje, f"scenarioet ga ikke resultat:\n{prove.stdout[-1500:]}\n{prove.stderr[-1500:]}"
    resultat = json.loads(linje[len("RESULTAT_JSON:"):])
    feil = [f"{r['navn']} – {r['detalj']}" for r in resultat if not r["ok"]]
    assert not feil, "Feil i nettleseren:\n" + "\n".join(feil)
    assert len(resultat) >= 60                        # scenarioet ble kjørt ferdig, ikke bare startet
