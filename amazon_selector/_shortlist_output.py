"""CSV and Markdown writers for shortlist.py; re-exported there for the old import path.
"""
from __future__ import annotations

import csv
from pathlib import Path

from _markets import market_for


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, restval="")
        w.writeheader()
        w.writerows(rows)


def write_markdown(path: Path, shortlist: list[dict], rejected: list[dict],
                   country: str, args, profile: dict, gates: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    md = [
        f"# Amazon product shortlist — {profile['label']}",
        "",
        f"Marketplace: **{country}** · {len(shortlist)} of "
        f"{len(shortlist) + len(rejected)} candidates passed the gates.",
        "",
        f"Search terms: `{profile['trend_term']}` for the trend, "
        + ", ".join(f"`{t}`" for t in profile["query_templates"])
        + " for competition.",
        "",
        f"Gates: trend in {list(args.allowed_flags)} · latest volume ≥ "
        f"{gates['min_volume']:g} · ≥ {gates['min_observations']} observations "
        f"· review moat ≤ {gates['max_review_moat']:,.0f}"
        + (f" · median price ≥ {market_for(country)['symbol']}"
           f"{gates['min_median_price']:,.0f}"
           if gates["min_median_price"] else ""),
        "",
        "## Shortlist",
        "",
    ]
    if shortlist:
        cols = ["rank", "keyword", "score", "latest_volume", "growth_per_period_pct",
                "momentum_pct", "yoy_pct", "review_moat", "demand_per_listing",
                "price_dispersion", "median_price"]
        md += ["| " + " | ".join(cols) + " |",
               "| " + " | ".join("---" for _ in cols) + " |"]
        for r in shortlist:
            md.append("| " + " | ".join(
                "" if r.get(c) is None else str(r.get(c)) for c in cols) + " |")
    else:
        md.append("_Nothing passed the gates._")

    md += ["", "## Rejected, and why", ""]
    if rejected:
        md += ["| keyword | trend | latest volume | reason |",
               "| --- | --- | --- | --- |"]
        for r in rejected:
            md.append(f"| {r['keyword']} | {r.get('opportunity_flag')} | "
                      f"{r.get('latest_volume')} | {r.get('gate_failures', '')} |")
    else:
        md.append("_Nothing was rejected._")

    md += [
        "",
        "## How the ranking works",
        "",
        "Weights: " + ", ".join(f"`{k}` {v:.0%}"
                                for k, v in profile["weights"].items()) + ".",
        "",
        "- **growth** — trend slope, fitted on log volume so it is a rate.",
        "- **demand** — latest volume, also logged: 500→5,000 matters more than "
        "50,000→54,500.",
        "- **openness** — low review moat and high demand-per-listing, averaged. "
        "This is the column that separates a big category from an enterable one.",
        "- **headroom** — price dispersion (IQR/median). Wide spread means room "
        "to position; tight clustering means competing on price alone.",
        "",
        "**The score is relative to this candidate set.** Components are min-max "
        "normalised across the candidates you supplied, so adding an eleventh "
        "changes the other ten, and `0.9` means \"best of what you supplied\", "
        "not \"good in absolute terms\". Compare within a run, never across runs.",
        "",
        "**Gates are not score terms.** No amount of growth makes a category with "
        "entrenched review incumbents enterable, so the moat disqualifies rather "
        "than subtracts. The rejected table above is usually the more useful half: "
        "\"rising but seasonal\" and \"open but unsearched\" are different next "
        "actions.",
    ]
    path.write_text("\n".join(md) + "\n", encoding="utf-8")
