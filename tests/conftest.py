import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _alltid_sqlite_i_tester(monkeypatch):
    """Testene bruker ALLTID SQLite (egen midlertidig fil per test), ogsaa om DATABASE_URL skulle vaere satt i miljoet
    eller i .env - en test skal aldri kunne skrive til en ekte PostgreSQL-database."""
    from kurs import config
    monkeypatch.setattr(config, "DATABASE_URL", "")
