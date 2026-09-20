"""Faktura tidligst seks kalendermaaneder foer foerste kursdag - steg 1: KUN kalenderaritmetikken.

Ingen databasetilgang og ingen fakturaflyt her (kommer i senere sjekkpunkter).
"""
from datetime import date, timedelta

import pytest

from kurs.sveiper import seks_maaneder_for


@pytest.mark.parametrize("kursdag,forventet", [
    (date(2027, 9, 20), date(2027, 3, 20)),    # vanlig tilfelle
    (date(2027, 8, 31), date(2027, 2, 28)),    # 31. finnes ikke i februar (ikke skaaar)
    (date(2028, 8, 31), date(2028, 2, 29)),    # skuddaar: siste dag i februar er 29.
    (date(2028, 2, 29), date(2027, 8, 29)),    # 29.02 -> august har 29.
    (date(2027, 10, 31), date(2027, 4, 30)),   # april har 30 dager
    (date(2027, 3, 31), date(2026, 9, 30)),    # september har 30 dager, og bakover over aarsskiftet
    (date(2027, 1, 31), date(2026, 7, 31)),    # aarsskifte, juli har 31
    (date(2027, 6, 30), date(2026, 12, 30)),   # desember
    (date(2027, 7, 15), date(2027, 1, 15)),    # januar
    (date(2027, 12, 31), date(2027, 6, 30)),   # juni har 30 dager
    (date(2027, 1, 1), date(2026, 7, 1)),
])
def test_seks_maaneder_for_gir_riktig_kalenderdato(kursdag, forventet):
    assert seks_maaneder_for(kursdag) == forventet


def test_det_er_kalendermaaneder_og_ikke_180_dager():
    kursdag = date(2027, 9, 20)
    assert seks_maaneder_for(kursdag) == date(2027, 3, 20)
    assert seks_maaneder_for(kursdag) != kursdag - timedelta(days=180)      # 180 dager tilbake ville gitt 24.03.2027
    assert kursdag - timedelta(days=180) == date(2027, 3, 24)
    # ende-av-maaned: heller ikke her tilsvarer resultatet 180 dager tilbake
    assert seks_maaneder_for(date(2027, 8, 31)) == date(2027, 2, 28)
    assert seks_maaneder_for(date(2027, 8, 31)) != date(2027, 8, 31) - timedelta(days=180)


def test_seks_maaneder_er_alltid_mellom_181_og_184_dager():
    """Aldri 180: derfor er faktura_dager_for <= 180 (per_samling) alltid innenfor seksmaanedersregelen."""
    for n in range(4 * 366):
        kursdag = date(2026, 1, 1) + timedelta(days=n)
        antall = (kursdag - seks_maaneder_for(kursdag)).days
        assert 181 <= antall <= 184, (kursdag, antall)


def test_resultatet_ligger_i_maaneden_seks_maaneder_tilbake_og_aldri_etter_kursdagen():
    for n in range(4 * 366):
        kursdag = date(2026, 1, 1) + timedelta(days=n)
        r = seks_maaneder_for(kursdag)
        assert (kursdag.year * 12 + kursdag.month) - (r.year * 12 + r.month) == 6, (kursdag, r)
        assert r.day == kursdag.day or r.day < kursdag.day               # bare klipping nedover, aldri oppover
        assert r < kursdag


def test_funksjonen_er_ren_og_deterministisk():
    """Tar og returnerer date, uten dagens dato eller database: samme svar hver gang, og input endres ikke."""
    kursdag = date(2027, 9, 20)
    r1, r2 = seks_maaneder_for(kursdag), seks_maaneder_for(kursdag)
    assert r1 == r2 == date(2027, 3, 20) and type(r1) is date
    assert kursdag == date(2027, 9, 20)
