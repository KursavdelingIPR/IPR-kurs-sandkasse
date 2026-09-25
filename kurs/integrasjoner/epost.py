"""Utsending av e-post.

Demo: skriver hver e-post som en .html-fil i utboks/ (vises ogsaa i admin -> Utboks).
Prod:  Microsoft Graph sendMail fra kurs-postboksen (samme M365 som resten av IPR).
"""
import base64
import json
import re
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import config, lenker, maltekster
from . import m365

_maler = Environment(
    loader=FileSystemLoader(Path(__file__).resolve().parent.parent / "maler" / "epost"),
    autoescape=select_autoescape(["html"]),
)


# Emnet er REN TEKST (e-postemne, ikke HTML): egen renderer UTEN autoescape. Ellers ble '&' til '&amp;' og '<' til
# '&lt;' i selve emnefeltet. (Kroppen rendres fortsatt med autoescape.)
_emne_env = Environment(autoescape=False)
_KONTROLLTEGN = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]+")


def har_kontrolltegn(tekst: str) -> bool:
    """True hvis teksten inneholder linjeskift/kontrolltegn - brukes til aa AVVISE slike emner fra admin-skjema."""
    return _KONTROLLTEGN.search(tekst) is not None


def _rent_emne(tekst: str) -> str:
    """Ett rent linjeskiftfritt emne: CR/LF og andre kontrolltegn (ogsaa Unicode-linjeskift) erstattes med mellomrom,
    slik at verken kursnavn, deltakernavn eller admin-tekst kan lage ekstra e-posthoder eller lekke inn i kroppen."""
    return _KONTROLLTEGN.sub(" ", tekst).strip()


def render(mal: str, maltekst: dict | None = None, **data) -> tuple[str, str]:
    """Returnerer (emne, html). Forste linje i malen er 'Emne: ...'.

    Emnet rendres som ren tekst (se _emne_env); kroppen rendres fra resten av malen med HTML-escaping. Emne-linja
    skilles ut paa KILDENIVAA (ikke ved aa dele ferdig output paa linjeskift), saa et linjeskift i en verdi aldri
    kan flytte tekst mellom emne og kropp."""
    kilde = _maler.loader.get_source(_maler, f"{mal}.html")[0]
    forste, _, resten = kilde.partition("\n")
    if maltekst is None and mal in maltekster.AKTIVE_MALER:
        maltekst = maltekster.standard_maltekst(mal, data)   # direkte render (ingen DB): STANDARDtekst
    verdier = {"base_url": config.BASE_URL, **data}
    if "m" in data and "lever_lenke" not in data:     # purring: signert opplastingslenke (laast systemblokk i malen)
        try:
            verdier["lever_lenke"] = lenker.lever_lenke(data["m"]["id"])
        except (KeyError, IndexError, TypeError, ValueError):
            verdier["lever_lenke"] = ""
    if maltekst is not None:
        verdier["maltekst"] = maltekst    # ferdig rendret (emne: ren tekst, tekst: trygg Markup) fra maltekster - IKKE fra DB her
    emne = _rent_emne(_emne_env.from_string(forste.rstrip("\r").removeprefix("Emne:")).render(**verdier))
    html = _maler.from_string(resten).render(**verdier)
    return emne, html


def send(til: str, emne: str, html: str, vedlegg: list[Path] | None = None, kopi: str | None = None) -> None:
    if config.DEMO:
        _til_utboks(til, emne, html, vedlegg, kopi)
    else:
        _graph_send(til, emne, html, vedlegg or [], kopi)


def _til_utboks(til, emne, html, vedlegg, kopi) -> None:
    config.UTBOKS.mkdir(parents=True, exist_ok=True)
    stempel = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    trygt = re.sub(r"[^a-z0-9]+", "_", til.lower())[:40]
    meta = {"til": til, "kopi": kopi, "emne": emne, "vedlegg": [str(v) for v in (vedlegg or [])],
            "tid": datetime.now().isoformat(timespec="seconds")}
    (config.UTBOKS / f"{stempel}_{trygt}.html").write_text(
        f"<!--META {json.dumps(meta, ensure_ascii=False)} -->\n{html}", encoding="utf-8"
    )


def _graph_send(til, emne, html, vedlegg, kopi) -> None:
    melding = {
        "subject": emne,
        "body": {"contentType": "HTML", "content": html},
        "toRecipients": [{"emailAddress": {"address": til}}],
        "attachments": [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": v.name,
                "contentBytes": base64.b64encode(v.read_bytes()).decode(),
            }
            for v in vedlegg
        ],
    }
    if kopi:
        melding["ccRecipients"] = [{"emailAddress": {"address": kopi}}]
    m365.graph("POST", f"/users/{config.AVSENDER_EPOST}/sendMail", json={"message": melding, "saveToSentItems": True})


def les_utboks(maks: int = 200) -> list[dict]:
    if not config.UTBOKS.exists():
        return []
    ut = []
    for f in sorted(config.UTBOKS.glob("*.html"), reverse=True)[:maks]:
        forste, _, html = f.read_text(encoding="utf-8").partition("\n")
        meta = json.loads(forste.removeprefix("<!--META ").removesuffix(" -->"))
        ut.append({**meta, "fil": f.name, "html": html})
    return ut
