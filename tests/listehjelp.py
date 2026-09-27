"""Deltakerlisten viser alle påmeldingene og skjuler (hidden) radene som ikke passer med søket og statusvalgene. Filteret
virker også mens man skriver (static/app.js), så en skjult rad finnes fortsatt i HTML-en: tester må se på hva som er
SYNLIG, ikke på hva som finnes i sidekoden."""
import re

_RAD = re.compile(r'<tr data-filtrer="(?P<type>[a-z]+)"(?P<attr>[^>]*)>(?P<innhold>.*?)</tr>', re.S)


def _skjult(attributter: str) -> bool:
    uten_verdier = re.sub(r'"[^"]*"', '""', attributter)          # «hidden» i et navn (data-sok) teller ikke
    return re.search(r"\bhidden\b", uten_verdier) is not None


def synlige_rader(html: str, type_: str = "deltaker") -> str:
    """Innholdet i de synlige radene i deltakerlisten (type_='oppmote': oppmøtetabellen)."""
    return "\n".join(m["innhold"] for m in _RAD.finditer(html) if m["type"] == type_ and not _skjult(m["attr"]))


def skjulte_rader(html: str, type_: str = "deltaker") -> str:
    return "\n".join(m["innhold"] for m in _RAD.finditer(html) if m["type"] == type_ and _skjult(m["attr"]))
