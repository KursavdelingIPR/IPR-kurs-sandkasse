"""Norske offentlige helligdager («røde dager»), regnet ut for hvert år - ingen lister som må vedlikeholdes.

Påskedag regnes ut med den gregorianske påskeformelen (Meeus/Jones/Butcher). Resten av de bevegelige helligdagene følger
av påsken: skjærtorsdag, langfredag, 2. påskedag, Kristi himmelfartsdag (+39) og pinse (+49/+50). Søndager er ikke med
her - de vises som helg.

Julaften og nyttårsaften er ikke offentlige helligdager, men de fleste har fri: de er med i `andre_fridager` og vises som
merknad, ikke som røde dager. (Samme fil i grenene for kalenderen og årsplanen.)
"""
from datetime import date, timedelta


def paaskedag(aar: int) -> date:
    """1. påskedag i den gregorianske kalenderen."""
    a, b, c = aar % 19, aar // 100, aar % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    maaned = (h + l_ - 7 * m + 114) // 31
    dag = (h + l_ - 7 * m + 114) % 31 + 1
    return date(aar, maaned, dag)


def helligdager(aar: int) -> dict[date, str]:
    """{dato: navn} for årets offentlige helligdager, i dato-rekkefølge. Faller to på samme dag (17. mai og 2. pinsedag i
    2027, 1. mai og Kristi himmelfartsdag i 2008), står begge navnene."""
    p = paaskedag(aar)
    alle = [
        (date(aar, 1, 1), "1. nyttårsdag"),
        (p - timedelta(days=3), "Skjærtorsdag"),
        (p - timedelta(days=2), "Langfredag"),
        (p, "1. påskedag"),
        (p + timedelta(days=1), "2. påskedag"),
        (date(aar, 5, 1), "Offentlig høytidsdag (1. mai)"),
        (date(aar, 5, 17), "Grunnlovsdag (17. mai)"),
        (p + timedelta(days=39), "Kristi himmelfartsdag"),
        (p + timedelta(days=49), "1. pinsedag"),
        (p + timedelta(days=50), "2. pinsedag"),
        (date(aar, 12, 25), "1. juledag"),
        (date(aar, 12, 26), "2. juledag"),
    ]
    dager: dict[date, str] = {}
    for dato, navn in alle:
        dager[dato] = f"{dager[dato]} og {navn}" if dato in dager else navn
    return dict(sorted(dager.items()))


def andre_fridager(aar: int) -> dict[date, str]:
    """Dager de fleste har fri uten at de er offentlige helligdager."""
    return {date(aar, 12, 24): "Julaften", date(aar, 12, 31): "Nyttårsaften"}


def i_perioden(fra: date, til: date) -> dict[date, str]:
    """Helligdagene fra og med `fra` til og med `til` (kan gå over årsskifter)."""
    ut: dict[date, str] = {}
    for aar in range(fra.year, til.year + 1):
        ut.update({d: n for d, n in helligdager(aar).items() if fra <= d <= til})
    return ut
