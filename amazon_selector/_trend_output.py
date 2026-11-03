"""CSV and Markdown writers for trends.py; re-exported there for the old import path.
"""
from __future__ import annotations

import csv
from pathlib import Path


# ---------------------------------------------------------------- output
def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def write_markdown(path: Path, rows: list[dict], country: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = list(rows[0].keys())
    md = [
        "# Amazon keyword demand trends",
        "",
        f"Marketplace: **{country}** · {len(rows)} keywords · sorted by momentum.",
        "",
        "| " + " | ".join(cols) + " |",
        "| " + " | ".join("---" for _ in cols) + " |",
    ]
    for r in rows:
        md.append("| " + " | ".join("" if r[c] is None else str(r[c]) for c in cols) + " |")
    md += [
        "",
        "## Reading the columns",
        "",
        "- `growth_per_period_pct` is a least-squares fit on **log** volume, so "
        "it is a growth rate: 100→200 and 10,000→20,000 score the same.",
        "- `momentum_pct` compares the last 4 periods to the 4 before, not "
        "latest-vs-oldest — search volume is spiky and endpoint comparisons "
        "swing on a single reading.",
        "- `yoy_pct` is the seasonality control. Positive momentum with negative "
        "YoY is a category coming off a trough, not one that is growing.",
        "- `volatility_cv` guards the growth numbers. Above ~0.6 the trend is "
        "within the noise and `opportunity_flag` says so.",
        "- `pct_of_peak` shows whether the latest reading is near the series "
        "high or recovering from a dip.",
        "",
        "**Schema caveat:** the API response shape this was parsed from is an "
        "assumption (see the module docstring). If a column is empty across "
        "every keyword, the field name is probably wrong rather than the data "
        "missing.",
    ]
    path.write_text("\n".join(md) + "\n", encoding="utf-8")
