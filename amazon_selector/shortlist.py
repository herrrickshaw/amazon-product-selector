#!/usr/bin/env python3
"""
One ranked shortlist of Amazon products worth selling.

Wires the two halves together: `trends` says whether demand
for a keyword is RISING, `competition` says whether the category has
a slot free. Neither alone answers "what should I sell" — a rising term in a
category owned by three entrenched ASINs is not an opportunity, and an open
category nobody searches for is not either.

GATE FIRST, THEN RANK
---------------------
The pipeline does not blend everything into one number and sort. It applies
pass/fail gates, records WHY each rejection failed, and only then ranks the
survivors. Two reasons:

  * A blended score lets a strong growth number outvote a disqualifying review
    moat. Those are not commensurable — no amount of growth makes a category
    with 40,000-review incumbents enterable — so the moat is a gate, not a term.
  * Rejections are more informative than the shortlist. "Rising, but seasonal"
    and "open, but nobody searches for it" are different next actions, and a
    tool that silently drops both teaches you nothing.

THE SCORE IS RELATIVE TO THE CANDIDATE SET
------------------------------------------
Survivors are ranked on a weighted composite of min-max normalised components.
That makes the score comparative, not absolute: adding an eleventh candidate
changes the scores of the other ten, and a score of 0.9 means "best of what you
supplied", never "good in absolute terms". Component values are reported
alongside the total so the ranking can be argued with. Compare scores within a
run; do not compare them across runs.

NEITHER API IS REACHABLE FROM THE CLAUDE CODE SANDBOX — see the two modules
this composes. `--dry-run` exercises the whole pipeline on fixtures.

Usage:
    python3 amazon_selector/shortlist.py --dry-run
    export NEXSCOPE_API_KEY=... OPENWEB_NINJA_API_KEY=...
    python3 amazon_selector/shortlist.py --keywords "moringa oil,mouth tape"
    python3 amazon_selector/shortlist.py --keywords-file candidates_in.txt \
        --min-volume 1000 --max-review-moat 1500
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import competition as comp  # noqa: E402
import trends as trend  # noqa: E402

# Trend verdicts allowed through. "spiky" and "seasonal bounce" are excluded
# because they are the two ways a rising-looking series lies.
DEFAULT_ALLOWED_FLAGS = ("RISING",)

# ---------------------------------------------------------------- markets
# Thresholds denominated in money or in absolute search counts cannot be one
# global number. The B2B order-value floor is the clearest case: 25 is a
# sensible USD floor and a meaningless INR one — Rs 25 is about $0.30, so on
# amazon.in a currency-naive floor passes literally everything and the gate
# silently stops existing.
#
# `volume_scale` adjusts the search-volume gates for absolute market size:
# Amazon India's search counts are far smaller than the US's for the same
# category, so a US floor would reject viable Indian niches for being Indian.
#
# HONEST STATUS: b2b_min_price values are converted at rough spot rates and
# rounded to a round number a buyer would recognise. volume_scale values are
# ESTIMATES, not measurements — nothing here is calibrated against observed
# data, and they should be retuned after a first real run in the target
# marketplace. They are explicit and in one place precisely so that retuning
# is a one-line edit rather than an archaeology exercise.
MARKETS = {
    "US": {"currency": "USD", "symbol": "$",  "b2b_min_price": 25.0,   "volume_scale": 1.0},
    "IN": {"currency": "INR", "symbol": "\u20b9", "b2b_min_price": 2000.0, "volume_scale": 0.2},
    "GB": {"currency": "GBP", "symbol": "\u00a3",  "b2b_min_price": 20.0,   "volume_scale": 0.25},
    "DE": {"currency": "EUR", "symbol": "\u20ac",  "b2b_min_price": 25.0,   "volume_scale": 0.25},
    "CA": {"currency": "CAD", "symbol": "$",  "b2b_min_price": 35.0,   "volume_scale": 0.12},
    "AU": {"currency": "AUD", "symbol": "$",  "b2b_min_price": 40.0,   "volume_scale": 0.08},
    "JP": {"currency": "JPY", "symbol": "\u00a5",  "b2b_min_price": 4000.0, "volume_scale": 0.3},
    "AE": {"currency": "AED", "symbol": "AED", "b2b_min_price": 90.0,  "volume_scale": 0.05},
}
DEFAULT_MARKET = {"currency": "?", "symbol": "", "b2b_min_price": 25.0, "volume_scale": 1.0}

# The stated target marketplace. A US default would silently apply USD-
# denominated gates to rupee prices, which is the failure the block above
# exists to prevent.
DEFAULT_COUNTRY = "IN"


def market_for(country: str) -> dict:
    return MARKETS.get(country.upper(), DEFAULT_MARKET)


def resolve_gates(profile: dict, market: dict) -> dict:
    """Profile gates expressed in the target marketplace's units.

    Kept separate from the profile definition so the profile stays a statement
    about the CHANNEL (retail vs procurement) and the market stays a statement
    about the COUNTRY. Folding them together would need one entry per
    channel-country pair.
    """
    g = dict(profile["gates"])
    if g["min_median_price"] is not None:
        g["min_median_price"] = market["b2b_min_price"]
    g["min_volume"] = max(1.0, round(g["min_volume"] * market["volume_scale"]))
    return g


# ---------------------------------------------------------------- profiles
# B2C and B2B are run as SEPARATE analyses rather than one with a filter,
# because they do not share a question. The difference starts at the keywords:
# a consumer searches "moringa oil", a purchasing manager searches "bulk
# moringa oil" or "moringa oil case pack". Those are different search series
# with different volumes, so a single trend read cannot describe both, and
# every threshold downstream inherits the mismatch.
#
# Each field below differs for a stated reason. Where a reason is weak, the
# value is left equal across profiles rather than varied for appearance.
PROFILES = {
    "b2c": {
        "label": "B2C — consumer retail",
        # The bare term. Consumer search is the default surface.
        "trend_term": "{kw}",
        "query_templates": ["{kw}"],
        "gates": {
            # Consumer categories are large; a term under a few hundred
            # searches is a hobby, not a business.
            "min_volume": 500,
            "min_observations": 12,
            # The decisive B2C gate. Consumer conversion runs on social proof,
            # so entrenched review counts are a hard barrier no amount of
            # growth overcomes.
            "max_review_moat": 2000,
            "min_median_price": None,
        },
        "weights": {
            "growth": 0.35,     # enter before saturation
            "demand": 0.25,     # the prize has to be worth having
            "openness": 0.30,   # can you actually take a slot
            "headroom": 0.10,   # room to position on price
        },
    },
    "b2b": {
        "label": "B2B — Amazon Business / bulk",
        "trend_term": "bulk {kw}",
        "query_templates": ["bulk {kw}", "{kw} case pack", "wholesale {kw}",
                            "commercial {kw}"],
        "gates": {
            # Bulk terms carry a fraction of consumer search volume. A B2C
            # floor of 500 would reject every viable B2B niche, so this is not
            # a laxer standard — it is the same standard on a smaller scale.
            "min_volume": 50,
            "min_observations": 12,
            # Deliberately an order of magnitude looser than B2C. Procurement
            # is spec- and price-driven and frequently a repeat contract; a
            # purchasing manager comparing case prices is not moved by review
            # count the way a consumer is. Still gated rather than disabled,
            # because a genuinely dominant incumbent is still a problem.
            "max_review_moat": 25000,
            # B2B earns on order value, not unit count. A $6 item does not
            # become a B2B business by being sold in a box.
            "min_median_price": 25.0,
        },
        "weights": {
            "growth": 0.35,     # unchanged — the question is still "rising?"
            "demand": 0.15,     # raw search volume matters less per order
            "openness": 0.20,   # moat is a weaker barrier here
            "headroom": 0.30,   # price spread means tiering room = the margin
        },
    },
}

for _name, _p in PROFILES.items():
    assert abs(sum(_p["weights"].values()) - 1.0) < 1e-9, \
        f"{_name} weights must sum to 1.0"

# Kept as the module-level default so existing callers and the B2C path read
# naturally; every profile carries its own.
WEIGHTS = PROFILES["b2c"]["weights"]


def expand(template: str, keyword: str) -> str:
    return template.format(kw=keyword)


def profile_queries(profile: dict, keyword: str) -> list[str]:
    return [expand(t, keyword) for t in profile["query_templates"]]


def _minmax(values: list[float]) -> list[float]:
    """Scale to 0-1 across the candidate set. A constant column maps to 0.5
    rather than 0 or 1 — with no spread there is no evidence either way, and
    collapsing it to an extreme would silently hand that component's whole
    weight to every candidate or to none."""
    usable = [v for v in values if v is not None]
    if not usable:
        return [0.5] * len(values)
    lo, hi = min(usable), max(usable)
    if hi - lo < 1e-12:
        return [0.5] * len(values)
    return [0.5 if v is None else (v - lo) / (hi - lo) for v in values]


def gate(cand: dict, *, allowed_flags: tuple[str, ...], min_volume: float,
         min_observations: int, max_review_moat: float | None,
         min_median_price: float | None = None) -> list[str]:
    """-> list of failure reasons. Empty list means the candidate passes."""
    fails = []
    flag = cand.get("opportunity_flag")
    if flag not in allowed_flags:
        fails.append(f"trend is {flag!r}, not in {list(allowed_flags)}")
    if (cand.get("observations") or 0) < min_observations:
        fails.append(f"only {cand.get('observations')} observations "
                     f"(need {min_observations})")
    if (cand.get("latest_volume") or 0) < min_volume:
        fails.append(f"latest volume {cand.get('latest_volume')} below "
                     f"{min_volume:g}")
    moat = cand.get("review_moat")
    if max_review_moat is not None and moat is not None and moat > max_review_moat:
        fails.append(f"review moat {moat:,} above {max_review_moat:,.0f}")
    price = cand.get("median_price")
    if min_median_price is not None and price is not None and price < min_median_price:
        fails.append(f"median price {price} below {min_median_price:g}")
    if cand.get("listings_sampled") in (None, 0):
        fails.append("no competition data")
    return fails


def score_candidates(cands: list[dict], weights: dict | None = None) -> list[dict]:
    """Rank survivors. Mutates nothing; returns new dicts with score fields."""
    if not cands:
        return []

    growth = _minmax([c.get("growth_per_period_pct") for c in cands])
    # Demand on a log scale: the gap between 500 and 5,000 searches matters
    # far more than the gap between 50,000 and 54,500, and a linear scale
    # would let one huge term flatten every other candidate to ~0.
    demand = _minmax([math.log(c["latest_volume"]) if c.get("latest_volume") else None
                      for c in cands])
    # Openness combines a low moat with high demand per competing listing.
    moat_inv = _minmax([-c["review_moat"] if c.get("review_moat") is not None else None
                        for c in cands])
    dpl = _minmax([c.get("demand_per_listing") for c in cands])
    headroom = _minmax([c.get("price_dispersion") for c in cands])

    out = []
    for i, c in enumerate(cands):
        openness = (moat_inv[i] + dpl[i]) / 2
        parts = {"growth": growth[i], "demand": demand[i],
                 "openness": openness, "headroom": headroom[i]}
        w = weights or WEIGHTS
        total = sum(w[k] * v for k, v in parts.items())
        row = dict(c)
        row["score"] = round(total, 4)
        for k, v in parts.items():
            row[f"score_{k}"] = round(v, 3)
        out.append(row)

    out.sort(key=lambda r: -r["score"])
    for rank, row in enumerate(out, 1):
        row["rank"] = rank
    return out


def build_candidate(keyword: str, trend_summary: dict,
                    comp_summary: dict | None, comp_extra: dict | None) -> dict:
    """Flatten the two halves into one row, prefixing nothing — the column
    names are already distinct and a `trend_`/`comp_` prefix would only make
    the CSV harder to read."""
    row = {"keyword": keyword}
    row.update({k: v for k, v in trend_summary.items() if k != "keyword"})
    if comp_summary:
        for k in ("listings_sampled", "units_past_month_floor", "units_coverage_pct",
                  "est_monthly_revenue_floor", "median_price", "cumulative_ratings",
                  "demand_per_listing", "total_listings_on_primary_keyword"):
            row[k] = comp_summary.get(k)
    if comp_extra:
        row.update(comp_extra)
    return row


def collect(keyword: str, args, trend_key, comp_key, profile: dict) -> dict:
    """Trend first, competition second — and only if the trend passed.

    Ordering is deliberate: the competition call costs credits per listing
    returned, and roughly half of any candidate list dies on the trend gate.
    Paying for competition data on a keyword already disqualified for being
    seasonal is the most expensive way to learn nothing.

    The trend is read on the PROFILE'S term, not the bare keyword — "bulk
    moringa oil" is a different search series from "moringa oil", with its own
    volume and its own seasonality, which is the reason the two profiles are
    separate analyses rather than one with a filter.
    """
    gates = resolve_gates(profile, market_for(args.country))
    trend_term = expand(profile["trend_term"], keyword)

    tcp = trend.cache_path(trend_term, args.country)
    tfresh = trend.cache_is_fresh(tcp, args.trend_cache_max_age_hours)
    if args.dry_run:
        payload = trend.dry_run_payload(trend_term)
    elif args.from_cache or tfresh:
        # Previously this path never cached trend responses at all, so
        # --from-cache silently did nothing for the trend half and every run
        # re-spent one request per keyword per profile.
        if not tcp.exists():
            raise RuntimeError(f"no cached trend series for {trend_term!r}")
        payload = json.loads(tcp.read_text())
        if not tfresh:
            print(f"    STALE TREND CACHE {trend.cache_age_hours(tcp):.0f}h old "
                  f"(limit {args.trend_cache_max_age_hours:g}h), using it anyway "
                  f"because --from-cache forbids a refetch")
    else:
        # start_date must be sent explicitly. Without it the API returns
        # whatever default window it likes, and if that is under 52 weeks then
        # year_over_year() has nothing to compare against, returns None for
        # every candidate, and the seasonality gate silently never fires —
        # letting exactly the trough-bounce categories it exists to catch
        # through as RISING. A short window must not look like an absent season.
        start = (date.today() - timedelta(weeks=args.weeks)).isoformat()
        payload = trend.fetch(trend_term, args.country, trend_key, args.endpoint,
                              args.auth_header, args.auth_prefix, start=start)
        if not args.no_cache:
            tcp.parent.mkdir(parents=True, exist_ok=True)
            tcp.write_text(json.dumps(payload))
    points = trend.parse_series(payload, trend_term)
    tsum = trend.summarise_keyword(keyword, points, args.window)
    tsum["opportunity_flag"] = trend.opportunity_flag(tsum)

    row = build_candidate(keyword, tsum, None, None)
    row["profile"] = args.profile_name
    row["trend_term"] = trend_term

    # Pre-gate on the trend alone, before spending competition credits. The
    # moat and price gates are excluded here because neither is known yet —
    # both are computed from the competition call this is trying to avoid.
    pre = [f for f in gate(row, allowed_flags=tuple(args.allowed_flags),
                           min_volume=gates["min_volume"],
                           min_observations=gates["min_observations"],
                           max_review_moat=None, min_median_price=None)
           if "competition" not in f]
    if pre and not args.price_all:
        row["gate_failures"] = "; ".join(pre)
        row["priced_competition"] = False
        return row

    queries = profile_queries(profile, keyword)
    rows, breadth = comp.collect_group(
        keyword, queries, args.country, args.pages, comp_key, args.sleep,
        args.dry_run, args.from_cache, not args.no_cache,
        args.cache_max_age_hours,
    )
    if rows:
        csum = comp.summarise_group(keyword, rows, breadth)
        extra = comp.competition_metrics(rows)
        row = build_candidate(keyword, tsum, csum, extra)
        row["profile"] = args.profile_name
        row["trend_term"] = trend_term
    row["priced_competition"] = bool(rows)
    return row


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


def run_profile(name: str, keywords: list[str], args, trend_key, comp_key,
                out: Path) -> tuple[list[dict], list[dict]]:
    profile = PROFILES[name]
    args.profile_name = name
    market = market_for(args.country)
    gates = resolve_gates(profile, market)
    print(f"\n=== {profile['label']} · {args.country.upper()} "
          f"({market['currency']}) ===")

    candidates = []
    for kw in keywords:
        print(f"  {kw}:")
        try:
            candidates.append(collect(kw, args, trend_key, comp_key, profile))
        except trend.SchemaMismatch as e:
            print(f"    SCHEMA MISMATCH — {e}")
        except RuntimeError as e:
            print(f"    {e}")

    passed, rejected = [], []
    for c in candidates:
        fails = c.get("gate_failures")
        fails = fails.split("; ") if fails else gate(
            c, allowed_flags=tuple(args.allowed_flags),
            min_volume=gates["min_volume"],
            min_observations=gates["min_observations"],
            max_review_moat=gates["max_review_moat"],
            min_median_price=gates["min_median_price"])
        if fails:
            c["gate_failures"] = "; ".join(fails)
            rejected.append(c)
        else:
            passed.append(c)

    shortlist = score_candidates(passed, profile["weights"])

    print(f"  --- shortlist ({len(shortlist)} of {len(candidates)}) ---")
    for r in shortlist:
        print(f"    {r['rank']}. {r['keyword']:<18s} score {r['score']:.3f}  "
              f"(growth {r['score_growth']:.2f} demand {r['score_demand']:.2f} "
              f"openness {r['score_openness']:.2f} headroom {r['score_headroom']:.2f})")
    if not shortlist:
        print("    nothing passed the gates")
    print(f"  --- rejected ({len(rejected)}) ---")
    for r in rejected:
        print(f"    {r['keyword']:<18s} {r['gate_failures']}")

    write_csv(out / f"amazon_shortlist_{name}.csv", shortlist + rejected)
    write_markdown(out / f"amazon_shortlist_{name}.md", shortlist, rejected,
                   args.country.upper(), args, profile, gates)
    return shortlist, rejected


def main() -> int:
    ap = argparse.ArgumentParser(description="Rank Amazon product candidates.")
    # India-relevant by default, since IN is the default marketplace, and
    # chosen so a bare --dry-run demonstrates the divergence that is the
    # point of running two profiles: one term shortlisted in both, one
    # consumer-only, one bulk-only, one rejected outright.
    ap.add_argument("--keywords",
                    default="giloy juice,aloe vera gel,chyawanprash,moringa oil")
    ap.add_argument("--keywords-file")
    ap.add_argument("--profile", default="both", choices=["b2c", "b2b", "both"],
                    help="B2C and B2B are separate analyses; 'both' runs each "
                         "and writes its own report")
    ap.add_argument("--country", default=DEFAULT_COUNTRY,
                    help="target marketplace; sets currency-denominated gates "
                         f"(known: {', '.join(sorted(MARKETS))})")
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--window", type=int, default=4)
    ap.add_argument("--weeks", type=int, default=52)
    ap.add_argument("--allowed-flags", nargs="+", default=list(DEFAULT_ALLOWED_FLAGS))
    ap.add_argument("--price-all", action="store_true",
                    help="fetch competition data even for trend-rejected keywords")
    ap.add_argument("--endpoint", default=trend.default_endpoint())
    ap.add_argument("--auth-header", default=trend.DEFAULT_AUTH_HEADER)
    ap.add_argument("--auth-prefix", default=trend.DEFAULT_AUTH_PREFIX)
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--from-cache", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--cache-max-age-hours", type=float,
                    default=comp.DEFAULT_CACHE_MAX_AGE_HOURS,
                    help="competition snapshots older than this are refetched "
                         "(prices and rankings move daily); 0 = never expire")
    ap.add_argument("--trend-cache-max-age-hours", type=float,
                    default=trend.DEFAULT_CACHE_MAX_AGE_HOURS,
                    help="trend series older than this are refetched (a weekly "
                         "series gains one point a week); 0 = never expire")
    ap.add_argument("--out", default="reports")
    args = ap.parse_args()
    args.profile_name = None

    if args.keywords_file:
        text = Path(args.keywords_file).read_text(encoding="utf-8")
        keywords = [ln.strip() for ln in text.splitlines()
                    if ln.strip() and not ln.strip().startswith("#")]
    else:
        keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    if not keywords:
        sys.exit("No keywords given.")

    if args.dry_run:
        print("DRY RUN — no network call is made.")
        trend_key = comp_key = None
    else:
        trend_key = trend.api_key()
        comp_key = comp.api_key()

    names = ["b2c", "b2b"] if args.profile == "both" else [args.profile]
    out = Path(args.out)
    results = {}
    for name in names:
        results[name] = run_profile(name, keywords, args, trend_key, comp_key, out)

    if not any(s or r for s, r in results.values()):
        print("\nNothing collected. If every keyword failed on reachability, "
              "this environment's egress policy is the cause.")
        return 1

    if len(names) > 1:
        print("\n=== B2C vs B2B ===")
        b2c = {r["keyword"] for r in results["b2c"][0]}
        b2b = {r["keyword"] for r in results["b2b"][0]}
        both = sorted(b2c & b2b)
        print(f"  shortlisted in both: {', '.join(both) if both else '—'}")
        print(f"  B2C only:            {', '.join(sorted(b2c - b2b)) or '—'}")
        print(f"  B2B only:            {', '.join(sorted(b2b - b2c)) or '—'}")
        print("  A term on only one side is the useful signal: the same product "
              "can be\n  saturated at retail and open in bulk, or the reverse.")

    print("\nWritten:")
    for name in names:
        print(f"  {out / f'amazon_shortlist_{name}.csv'}")
        print(f"  {out / f'amazon_shortlist_{name}.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
