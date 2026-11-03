"""Fixture series for ``trends.py --dry-run`` (no network, no key).

Split out of trends.py; ``trends`` re-exports ``dry_run_payload`` so the old
import path keeps working.
"""
from __future__ import annotations

import hashlib
from datetime import date, timedelta


def _archetype_for(keyword: str) -> int:
    """Stable archetype index for a keyword with no named fixture.

    Hashed rather than random so a dry run is reproducible, and varied rather
    than constant because the alternative — sending every unnamed keyword down
    one archetype — makes a large dry run degenerate: 100 candidates all
    receiving the same series produce one verdict, no ranking, and no evidence
    that the pipeline handles scale.
    """
    return int(hashlib.md5(keyword.encode()).hexdigest(), 16) % 5


def _series(kind: int, scale: int) -> list[int]:
    """The five archetypes, parameterised by volume so the demand component
    has a spread to normalise over."""
    if kind == 0:                                   # steady riser
        return [round(scale * (1.03 ** i)) for i in range(60)]
    if kind == 1:                                   # faster riser
        return [round(scale * (1.045 ** i)) for i in range(60)]
    if kind == 2:                                   # flat
        return [scale * 10 + (i % 5) * (scale // 4 or 1) for i in range(60)]
    if kind == 3:                                   # seasonal, below last year
        peak = scale * 6
        return ([peak // 5, peak // 3, peak // 2, peak, peak, peak, peak, peak,
                 peak // 2, peak // 4, peak // 6, peak // 8]
                + [peak // 10] * 43
                + [peak // 8, peak // 5, peak // 3, peak // 2, round(peak * 0.6)])
    pattern = [scale, scale * 9, scale * 2, scale * 12, scale, scale * 7]
    return ([pattern[i % len(pattern)] for i in range(52)]
            + [scale, scale, scale * 2, scale, scale * 14, scale * 15,
               scale * 13, scale * 16])


def dry_run_payload(keyword: str) -> dict:
    """Named fixtures for the keywords the tests pin, hashed archetypes for
    everything else.

    The five named series exist so the tests can assert exact numbers and so
    every branch of opportunity_flag() is exercised: a clean riser, a flat
    line, a seasonal category rising short-term but BELOW the same week last
    year, and a noisy one whose apparent growth sits inside its own variance.

    60 weekly points, so index 59 is "now" and index 7 is ~52 weeks earlier —
    the pair year_over_year() compares.
    """
    start = date(2025, 8, 25)
    kw = keyword.lower()
    if "moringa" in kw:                       # steady ~3%/week riser
        vals = [round(1000 * (1.03 ** i)) for i in range(60)]
    elif "turmeric soap" in kw:               # faster riser, higher volume
        vals = [round(4000 * (1.045 ** i)) for i in range(60)]
    elif "shilajit" in kw:                    # riser, but a small prize
        vals = [round(300 * (1.035 ** i)) for i in range(60)]
    elif "honey" in kw:                       # flat, mild noise
        vals = [40000 + (i % 5) * 300 for i in range(60)]
    elif "mouth tape" in kw:
        # Seasonal: last year's peak (~30k around index 7) dwarfs this year's
        # (~18k at index 59), so momentum is positive but YoY is negative.
        vals = [6000, 9000, 14000, 22000, 28000, 30000, 30000, 30000,
                24000, 16000, 10000, 7000] + [5000] * 43 + \
               [6000, 9000, 13000, 16000, 18000]
    elif "sleep gummies" in kw:
        # Noisy: oscillates hard enough that the last-4-vs-prior-4 comparison
        # reads as growth while the series has no trend the fit can explain.
        pattern = [1000, 9000, 1500, 12000, 800, 7000, 2000, 15000]
        vals = [pattern[i % len(pattern)] for i in range(52)] + \
               [1000, 800, 1200, 900, 14000, 15000, 13000, 16000]
    else:
        h = int(hashlib.md5(kw.encode()).hexdigest(), 16)
        vals = _series(_archetype_for(kw), 200 + (h // 5) % 4000)
    history = [
        {"week_start": (start + timedelta(weeks=i)).isoformat(),
         "search_volume": v, "rank": max(1, 100000 - v)}
        for i, v in enumerate(vals)
    ]
    return {"status": "ok", "data": {"keyword": keyword, "history": history}}
