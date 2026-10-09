"""IPR-utseendet på Min side for kurs IPR arrangerer (Camilla 05.10.2026): samme oppsett som Terapiakademiet-drakten, men i
fargene fra IPR-logoen og med IPR-logoen i toppen (static/tema-ipr.css). Terapiakademiet-kurs er uendret."""
import re
from pathlib import Path

import pytest

from kurs import tema
from kurssidehjelp import admin_klient, dokument, fast_dato, lag_kurs, ny_database, skriv_side, tekstblokk

CSS = Path(__file__).resolve().parents[1] / "kurs" / "web" / "static" / "tema-ipr.css"


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = ny_database(tmp_path, monkeypatch)
    fast_dato(monkeypatch)
    (tmp_path / "logo").mkdir()
    for navn in ("ipr.png", "terapiakademiet.png", "terapiakademiet-lys.png"):
        (tmp_path / "logo" / navn).write_bytes(b"PNG")
    monkeypatch.setattr(tema, "LOGO_MAPPE", tmp_path / "logo")
    yield c
    c.close()


def _side(con, **felt) -> str:
    kid = lag_kurs(con, "K1", **felt)
    skriv_side(con, kid, dokument(tekstblokk("Velkommen", "<p>Hei</p>")))
    return admin_klient(con).get(f"/admin/kurs/{kid}/kursside/forhandsvis?versjon=publisert").get_data(as_text=True)


def test_ipr_kurs_faar_ipr_fargene_og_logoen(con):
    html = _side(con)
    assert '<body data-tema="ta" data-merke="ipr"' in html and "tema-ipr.css" in html
    assert 'src="/static/logo/ipr.png" alt="Institutt for Psykologisk Rådgivning"' in html
    assert 'alt="Terapiakademiet"' not in html                    # heller ikke den lyse Terapiakademiet-logoen i bunnen


def test_terapiakademiet_kurs_er_uendret(con):
    html = _side(con, merke="terapiakademiet")
    assert '<body data-tema="ta" class=' in html and "tema-ipr.css" not in html and 'data-merke' not in html
    assert 'src="/static/logo/terapiakademiet.png" alt="Terapiakademiet"' in html


def _hex(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def _kontrast(a, b) -> float:
    def lum(rgb):
        c = [x / 255 for x in rgb]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _palett() -> dict:
    kilde = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    return dict(re.findall(r"--(ta-[a-z-]+):\s*(#[0-9a-fA-F]{6})", kilde))


@pytest.mark.parametrize("tekst, bakgrunn, minst", [
    ("ta-plomme", "ta-krem", 4.5), ("ta-plomme", "ta-band", 4.5), ("ta-mauve", "ta-krem", 4.5), ("ta-mauve", "ta-band", 4.5),
    ("ta-baer", "ta-krem", 4.5), ("ta-baer", "ta-band", 4.5), ("ta-krem", "ta-plomme", 4.5),
    ("ta-etikett-pa-plomme", "ta-plomme", 4.5), ("ta-lenke-pa-plomme", "ta-plomme", 4.5), ("ta-plomme", "ta-felt-bg", 4.5),
    ("ta-felt", "ta-felt-bg", 3.0), ("ta-felt", "ta-krem", 3.0),
])
def test_kontrasten_holder(tekst, bakgrunn, minst):
    p = _palett()
    assert _kontrast(_hex(p[tekst]), _hex(p[bakgrunn])) >= minst, (tekst, bakgrunn)


def test_alt_gjelder_bare_ipr_sidene_og_ingen_eksterne_ressurser():
    kilde = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    assert not re.search(r"https?:|//|@import|url\(", kilde) and "!important" not in kilde
    velgere = re.findall(r"([^{}]+)\{[^{}]*\}", kilde.replace("@media screen {", "", 1))
    assert velgere and all(v.strip().startswith('body[data-tema="ta"][data-merke="ipr"]')
                           for liste in velgere for v in liste.split(",") if v.strip())
