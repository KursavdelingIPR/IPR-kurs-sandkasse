"""Terapiakademiet-drakten for deltakersidene (static/tema-terapiakademiet.css, `tema_ta` i base.html): hvilke logofiler som er lagt inn.

Logoen er Terapiakademiets, og en godkjent fil legges av dem som eier den (som NIEFT-logoen til deltakerlisten, se deltakerliste.logo_fil).
Camilla ga lov til å legge den inn på Min side 01.10.2026, og filene hun la ved ligger i kurs/web/static/logo/ (751x186, gjennomsiktig bakgrunn):
  * terapiakademiet.png (eller .svg)       – til toppen, på kremfarget bakgrunn (burgunder utgave, `logo-burgunder.png` på terapiakademiet.no)
  * terapiakademiet-lys.png (eller .svg)   – til den mørke bunnen (lys utgave, `logo-lys.png`)
Fjernes filene, står navnet på systemet som tekst i toppen, og bunnen har bare kontaktopplysninger. Ingen omstart trengs.
"""
from pathlib import Path

LOGO_MAPPE = Path(__file__).resolve().parent / "web" / "static" / "logo"
_FILER = {"topp": ("terapiakademiet.svg", "terapiakademiet.png"), "bunn": ("terapiakademiet-lys.svg", "terapiakademiet-lys.png")}


def logo(plass: str) -> str | None:
    """Stien under static/ til logoen for «topp» eller «bunn», eller None når filen ikke er lagt inn."""
    for navn in _FILER.get(plass, ()):
        if (LOGO_MAPPE / navn).is_file():
            return f"logo/{navn}"
    return None
