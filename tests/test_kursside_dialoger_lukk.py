"""Alle dialoger i Kursside-editoren med en «Lukk»/«Avbryt»-knapp (data-lukk) må kobles opp i kursside-admin-dialoger.js.

Feilen som ble rettet 01.10.2026: dialogen «Slik ser deltakerne siden på PC» (ks-dialog-forh-stor) hadde en Lukk-knapp, men sto ikke i
oppkoblingslisten, så knappen reagerte ikke (bare Esc lukket den). Testen fanger neste dialog som glemmes på samme måte.
"""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "kurs" / "web"
DIALOGER = (WEB / "templates" / "_kursside_admin_dialoger.html").read_text(encoding="utf-8")
JS = (WEB / "static" / "kursside-admin-dialoger.js").read_text(encoding="utf-8")


def _dialoger_med_lukk() -> dict[str, str]:
    """{navn: html} for hver <dialog id="ks-dialog-NAVN"> som har en knapp med data-lukk."""
    ut = {}
    for m in re.finditer(r'<dialog id="ks-dialog-([^"]+)".*?</dialog>', DIALOGER, re.S):
        if "data-lukk" in m.group(0):
            ut[m.group(1)] = m.group(0)
    return ut


def _koblede_dialoger() -> set[str]:
    m = re.search(r'\[((?:"[a-z-]+",\s*)*"[a-z-]+")\]\.forEach\(function \(n\) \{ forbered\(dlg\(n\)\); \}\);', JS)
    assert m, "fant ikke oppkoblingslisten (forbered(dlg(n))) i kursside-admin-dialoger.js"
    return set(re.findall(r'"([a-z-]+)"', m.group(1)))


def test_alle_dialoger_med_lukkeknapp_er_koblet_opp():
    med_lukk = _dialoger_med_lukk()
    assert len(med_lukk) >= 10, "forventer de eksisterende dialogene; er malen flyttet?"
    mangler = sorted(set(med_lukk) - _koblede_dialoger())
    assert not mangler, f"Dialoger med Lukk-knapp som ikke er koblet opp (knappen gjør ingenting): {mangler}"


def test_forh_stor_er_koblet_opp_og_har_lukk_knapp():
    """Regresjon: «Slik ser deltakerne siden på PC» (Åpne stor visning) kan lukkes med Lukk."""
    assert "forh-stor" in _dialoger_med_lukk()
    assert "forh-stor" in _koblede_dialoger()


def test_oppkoblingen_lukker_dialogen_og_gir_fokus_tilbake():
    """forbered(): klikk på [data-lukk] kaller d.close(), og close-hendelsen flytter fokus tilbake til knappen som åpnet dialogen."""
    kropp = JS.split("function forbered(d) {")[1].split("function felt(")[0]
    assert 'KS.$$("[data-lukk]", d)' in kropp and 'addEventListener("click", function () { d.close(); })' in kropp
    assert 'd.addEventListener("close"' in kropp and "o.focus()" in kropp
