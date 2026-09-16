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

from .. import config
from . import m365

_maler = Environment(
    loader=FileSystemLoader(Path(__file__).resolve().parent.parent / "maler" / "epost"),
    autoescape=select_autoescape(["html"]),
)


def render(mal: str, **data) -> tuple[str, str]:
    """Returnerer (emne, html). Forste linje i malen er 'Emne: ...'."""
    tekst = _maler.get_template(f"{mal}.html").render(base_url=config.BASE_URL, **data)
    forste, _, html = tekst.partition("\n")
    return forste.removeprefix("Emne:").strip(), html


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
