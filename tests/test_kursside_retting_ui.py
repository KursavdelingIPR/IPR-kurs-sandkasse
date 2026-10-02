"""Rettinger i redigeringsvisningens og deltakersidens utseende og bruk (kritikernes funn 9, 11, 15-20): kildekontroll av CSS, maler og JavaScript, og
de delene som kan prøves uten nettleser. Selve utseendet er prøvd i Edge (menyene, toppbildet, innliming, opplasting, Tab-rekkefølge); se BYGG-notatene.
Bare oppdiktede data.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROT = Path(__file__).resolve().parent.parent
STATIC = ROT / "kurs" / "web" / "static"
TEMPLATES = ROT / "kurs" / "web" / "templates"


def _les(sti: Path) -> str:
    return sti.read_text(encoding="utf-8")


def _regel(css: str, velger: str) -> str:
    """Innholdet i første regel som er nøyaktig `velger { ... }` (linjeskift og ledende mellomrom tåles)."""
    treff = re.search(r"(?:^|\})\s*" + re.escape(velger) + r"\s*\{([^}]*)\}", css, re.M)
    assert treff, f"fant ikke regelen {velger}"
    return treff.group(1)


# ============================================================ (9) blokkmenyen og «+»-menyen ============================================================

def test_9_ikonstilen_gjelder_ogsaa_menyene_og_drag_skyggen_som_legges_i_body():
    """Menyene og drag-skyggen ligger i <body>, utenfor .ks-rot: uten egen regel ble hvert ikon en uformet SVG på 300 x 150 px og menyen 1 566 px høy."""
    css = _les(STATIC / "kursside-admin.css")
    ikonregel = re.search(r"^([^\n{]*\.ik[^\n{]*)\{\s*width:18px;\s*height:18px;", css, re.M).group(1)
    for velger in (".ks-rot .ik", ".ks-meny .ik", ".ks-skygge-kort .ik"):
        assert velger in ikonregel, velger


def test_9_menyen_kan_rulles_og_er_aldri_hoeyere_enn_vinduet():
    meny = _regel(_les(STATIC / "kursside-admin.css"), ".ks-meny")
    assert "position:fixed" in meny and "max-height:calc(100vh - 16px)" in meny and "overflow-y:auto" in meny


def test_9_menyvalgene_har_ikon_og_tekst_ved_siden_av_hverandre():
    css = _les(STATIC / "kursside-admin.css")
    assert "display:flex" in _regel(css, ".ks-meny button") and "gap:10px" in _regel(css, ".ks-meny button")


def test_9_menyen_har_alle_valgene_som_dokumentasjonen_lover_paa_mobil():
    """Pil opp/ned og dupliser er skjult i blokkhodet på mobil: de må finnes i ⋯-menyen (og menyen må kunne nås, se forrige tester)."""
    js = _les(STATIC / "kursside-admin-liste.js")
    meny = js[js.index("function blokkMeny("):js.index("KS.typeMeny")]
    for valg in ("Flytt opp", "Flytt ned", "Flytt til toppen", "Flytt til bunnen", "Dupliser", "Slett blokken", "Skjul for deltakerne"):
        assert valg in meny, valg
    assert '[data-ks="opp"], .ks-hnapper [data-ks="ned"], .ks-hnapper [data-ks="dupliser"] { display:none; }' in _les(STATIC / "kursside-admin.css")


# ============================================================ (15) stor forhåndsvisning ============================================================

def test_15_pc_forhandsvisningen_kan_aapnes_i_full_stoerrelse():
    side = _les(TEMPLATES / "kursside_admin.html")
    dialoger = _les(TEMPLATES / "_kursside_admin_dialoger.html")
    assert 'data-ks="forh-stor"' in side and "Åpne stor visning" in side
    dialog = re.search(r'<dialog id="ks-dialog-forh-stor" class="ks-dialog ks-dialog-forh" aria-labelledby="ks-forh-stor-h">.*?</dialog>', dialoger, re.S).group(0)
    assert 'id="ks-forh-stor-h"' in dialog and "<iframe" in dialog and 'sandbox="allow-same-origin"' in dialog and 'title="' in dialog
    assert 'data-ks="forh-stor-last"' in dialog and "data-lukk" in dialog
    js = _les(STATIC / "kursside-admin-dialoger.js")
    assert 'KS.ks("forh-stor")' in js and "KS.forhUrl()" in js and "KS.sikreLagret().then" in js.split("function apneForhStor")[1][:200]
    assert "KS.forhUrl = forhUrl;" in _les(STATIC / "kursside-admin.js")
    css = _les(STATIC / "kursside-admin.css")
    assert "dialog.ks-dialog-forh { width:min(1280px, calc(100vw - 24px));" in css


# ============================================================ (16) «Kopier fra dag 1» ============================================================

def test_16_kopier_fra_dag_1_skjules_paa_dag_1_selv():
    js = _les(STATIC / "kursside-admin-blokker.js")
    bygg = js[js.index("function byggDag("):js.index('} else if (navn === "dag-kopier")')]
    assert 'KS.ks("dag-kopier", el)' in bygg and "i === 0" in bygg and ".hidden = true" in bygg
    assert "Kopier fra dag 1" in _les(TEMPLATES / "_kursside_admin_maler.html")            # knappen finnes fortsatt for dag 2 og utover


# ============================================================ (17) tabellceller ============================================================

def test_17_tabellceller_har_synlig_ramme_og_bakgrunn_ogsaa_naar_de_er_tomme():
    regel = _regel(_les(STATIC / "kursside-admin.css"), ".ks-tab td input")
    assert "border:1px solid #c3ced4" in regel and "background:#fff" in regel and "transparent" not in regel


# ============================================================ (18) «Hent fra annet kurs» ============================================================

def test_18_hent_fra_har_velg_alle_og_fjern_alle():
    dialoger = _les(TEMPLATES / "_kursside_admin_dialoger.html")
    hent = dialoger[dialoger.index('<dialog id="ks-dialog-hent"'):dialoger.index("</dialog>", dialoger.index('<dialog id="ks-dialog-hent"'))]
    assert 'data-ks="hent-alle">Velg alle<' in hent and 'data-ks="hent-ingen">Fjern alle<' in hent
    assert "ekstra programdagene stående uten dato" in hent
    js = _les(STATIC / "kursside-admin-dialoger.js")
    assert 'KS.ks("hent-alle")' in js and "hentVelgAlle(true)" in js and "hentVelgAlle(false)" in js


# ============================================================ (20) tastaturfokus ============================================================

def test_20_fokus_og_hopp_havner_ikke_bak_den_klebrige_statuslinjen():
    css = _les(STATIC / "kursside-admin.css")
    m = re.search(r"^html \{ scroll-padding-top:(\d+)px; \}", css, re.M)
    assert m and int(m.group(1)) >= 100                          # statuslinjen er 58 px (89 px på mobil), og bannerne kommer under den


def test_21_kursfanene_er_alle_synlige_paa_mobil_og_bryter_til_flere_linjer():
    """Verifiseringen (390 px): en egen regel gjorde fanene til en rad som kunne rulles sidelengs uten rullefelt, så «Kursside» ble klippet og «Deltakere» og
    «Kommunikasjon» var usynlige. Fanene skal bryte til flere linjer, som på alle de andre kurssidene (base.html: flex-wrap:wrap)."""
    css = _les(STATIC / "kursside-admin.css")
    mobil = css[css.index("@media (max-width:760px)"):]
    for m in re.finditer(r"\.faner\s*\{([^}]*)\}", mobil):
        assert "overflow-x" not in m.group(1) and "nowrap" not in m.group(1), m.group(0)
    assert "flex-wrap:wrap" in _regel(_les(TEMPLATES / "base.html"), ".faner")


# ============================================================ JavaScript-testene kjøres her (Node) ============================================================

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node er ikke installert")
def test_javascript_i_redigeringsvisningen_autolagring_versjon_konflikt_rekkefolge_og_plakat():
    """tests/js/kursside_admin_test.js: kjernen (kursside-admin.js) og opplastingen lastes i en vm med falskt DOM og falsk fetch. Fanger blant annet
    at autolagring aldri lagrer, at versjon 0 sendes hver gang, at en 409-konflikt ikke vises som konflikt og at plakaten laster på nytt under utskrift."""
    r = subprocess.run([NODE, str(ROT / "tests" / "js" / "kursside_admin_test.js")], capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip().startswith("OK")
