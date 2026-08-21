#!/usr/bin/env python3
"""
Compare Amazon demand between two (or more) product groups — built to answer
"does moringa oil or honey get more orders on Amazon?"

WHAT COUNTS AS "ORDERS"
-----------------------
Amazon does not publish unit sales. Three public signals proxy for it, and they
measure different things, so this module reports all three rather than blending
them into one score:

  1. UNITS BOUGHT IN THE PAST MONTH — the `sales_volume` badge ("2K+ bought in
     past month"). This is the only *recent, unit-denominated* signal Amazon
     shows, and it is the closest thing to an order count that exists. Two
     caveats that the output repeats and you should not forget: the badge is a
     FLOOR ("2K+" could be 2,000 or 2,900), and Amazon only shows it on some
     listings, so the total is a lower bound over a partial sample. Coverage is
     reported as `units_coverage_pct` — read it before reading the total.

  2. CUMULATIVE RATINGS — `product_num_ratings`. Lifetime, not current, and
     scaled by whatever share of buyers leave a rating (low single-digit % and
     it varies by category). Good for "which category is bigger over its
     history", bad for "which is selling now".

  3. BREADTH — how many distinct listings and sellers compete on the keyword
     (`total_products`). A proxy for how much money the category attracts, not
     for how much any one seller earns.

Revenue is estimated as units x price, which is why a category can lose on
units and win on GMV. Both are reported.

WHAT THIS COMPARISON CANNOT TELL YOU
------------------------------------
Keyword demand is not category demand. A search for "honey" reaches a grocery
staple bought on repeat by the general population; "moringa oil" reaches a
niche cosmetic ingredient. Whichever wins, the interesting number for a seller
is not the total but the ratio of demand to competing listings — a big category
with 20,000 sellers can be a worse place to enter than a small one with 40.
`demand_per_listing` in the summary is there for that reason.

CREDENTIALS
-----------
Read from the environment, never from this file and never committed:

    export OPENWEB_NINJA_API_KEY=...

THIS SCRIPT CANNOT REACH THE API FROM THE CLAUDE CODE SANDBOX. The egress proxy
there answers 403 to CONNECT for api.openwebninja.com, the same organization
policy that blocks api.adzuna.com for research/diaspora. Run it somewhere with
ordinary internet access. `--dry-run` exercises every code path except the
network call, so the parsing and scoring can be checked without one.

Usage:
    export OPENWEB_NINJA_API_KEY=...
    python3 amazon_selector/competition.py --dry-run
    python3 amazon_selector/competition.py
    python3 amazon_selector/competition.py --country IN --pages 3
    python3 amazon_selector/competition.py --group "moringa oil=moringa oil,moringa seed oil"
    python3 amazon_selector/competition.py --from-cache
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import statistics
import sys
import time as _time
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://api.openwebninja.com/realtime-amazon-data/search"

# Several keyword variants per group, deduplicated by ASIN afterwards. One
# keyword is a single slice of a category and skews with whatever Amazon's
# ranker happens to favour that day; the union across variants is steadier.
DEFAULT_GROUPS: dict[str, list[str]] = {
    "moringa oil": [
        "moringa oil",
        "moringa seed oil",
        "moringa hair oil",
        "moringa oil for skin",
    ],
    "honey": [
        "honey",
        "raw honey",
        "organic honey",
        "manuka honey",
    ],
}

CACHE_DIR = Path("data/amazon_raw")

# "2K+ bought in past month", "50+ bought in past month", "1.5K+ bought ..."
_SALES_RE = re.compile(r"([\d.,]+)\s*([KkMm]?)\s*\+?\s*bought", re.I)
_MULT = {"": 1, "k": 1_000, "m": 1_000_000}


# --------------------------------------------------------------- credentials
def api_key() -> str:
    key = os.environ.get("OPENWEB_NINJA_API_KEY")
    if not key:
        sys.exit(
            "Set OPENWEB_NINJA_API_KEY in the environment. Do not put it in "
            "this file — tests/test_competition.py scans the tree for it."
        )
    return key


def redact(text: str) -> str:
    """Never print anything with the key in it — logs and tracebacks leak."""
    key = os.environ.get("OPENWEB_NINJA_API_KEY")
    return text.replace(key, "<OPENWEB_NINJA_API_KEY>") if key else text


# ---------------------------------------------------------------- fetching
def build_url(query: str, country: str = "US", page: int = 1,
              sort_by: str = "RELEVANCE") -> str:
    q = urllib.parse.urlencode({
        "query": query, "country": country, "page": page, "sort_by": sort_by,
    })
    return f"{BASE}?{q}"


# Search results — prices, the sales badge, which listings rank — turn over
# daily. A day-old snapshot is a fair read of "now"; a week-old one is fiction
# presented as current. trends.py uses a longer default for the same reason
# inverted: its series is weekly, so a fresh fetch cannot say anything new.
DEFAULT_CACHE_MAX_AGE_HOURS = 24


def cache_is_fresh(path: Path, max_age_hours: float) -> bool:
    """A cache with no expiry is worse than no cache: it silently answers
    today's question with last month's data, and nothing downstream can tell.
    max_age_hours <= 0 disables the check (keep forever)."""
    if not path.exists():
        return False
    if max_age_hours <= 0:
        return True
    age_hours = (_time.time() - path.stat().st_mtime) / 3600.0
    return age_hours <= max_age_hours


def cache_age_hours(path: Path) -> float | None:
    if not path.exists():
        return None
    return (_time.time() - path.stat().st_mtime) / 3600.0


def cache_path(query: str, country: str, page: int) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_")
    return CACHE_DIR / f"{slug}__{country.lower()}__p{page}.json"


def fetch(query: str, country: str, page: int, key: str,
          timeout: int = 30, retries: int = 3) -> dict:
    """One search page. Retries 429/5xx with backoff; other errors fail loudly.

    The key travels in a header, not the query string, so the URL is safe to
    log — but redact() is applied anyway, because a future edit that moves it
    into the query should not silently start leaking.
    """
    url = build_url(query, country, page)
    last = ""
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"x-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = 2 ** (attempt + 1)
                print(f"    {last} — retrying in {wait}s")
                time.sleep(wait)
                continue
            if e.code in (401, 403):
                raise RuntimeError(
                    f"{last} from the API for {redact(url)}. That is an auth "
                    f"or plan rejection, not a network problem — check "
                    f"OPENWEB_NINJA_API_KEY and your quota."
                ) from None
            raise RuntimeError(f"{last} for {redact(url)}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"cannot reach api.openwebninja.com ({e.reason}). If this is "
                f"the Claude Code sandbox, that host is refused by the egress "
                f"proxy with a 403 — run this where you have ordinary "
                f"internet access. See the module docstring."
            ) from None
    raise RuntimeError(f"gave up after {retries} attempts ({last})")


def products_of(payload: dict) -> list[dict]:
    """Tolerate both {'data': {'products': [...]}} and a bare {'products': [...]}."""
    data = payload.get("data")
    if isinstance(data, dict):
        return data.get("products") or []
    return payload.get("products") or []


def total_products_of(payload: dict) -> int | None:
    data = payload.get("data")
    if isinstance(data, dict):
        return data.get("total_products")
    return payload.get("total_products")


# ---------------------------------------------------------------- parsing
def parse_sales_volume(raw) -> int | None:
    """'2K+ bought in past month' -> 2000. Returns None when absent.

    The value is a FLOOR. Amazon buckets it, so 2,900 units also reads "2K+".
    Everything downstream that sums these is a lower bound and says so.
    """
    if not raw or not isinstance(raw, str):
        return None
    m = _SALES_RE.search(raw)
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return int(n * _MULT[m.group(2).lower()])


def parse_price(raw) -> float | None:
    """'$12.99' / '₹1,299.00' / '12.99' -> float. Currency-symbol agnostic."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    cleaned = re.sub(r"[^\d.]", "", str(raw).replace(",", ""))
    if not cleaned or cleaned.count(".") > 1:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def listing_row(p: dict, group: str, query: str) -> dict:
    units = parse_sales_volume(p.get("sales_volume"))
    price = parse_price(p.get("product_price"))
    return {
        "group": group,
        "query": query,
        "asin": p.get("asin"),
        "title": (p.get("product_title") or "")[:120],
        "price": price,
        "currency": p.get("currency"),
        "star_rating": parse_price(p.get("product_star_rating")),
        "num_ratings": p.get("product_num_ratings") or 0,
        "units_past_month": units,
        "est_monthly_revenue": round(units * price, 2) if units and price else None,
        "is_best_seller": bool(p.get("is_best_seller")),
        "is_amazon_choice": bool(p.get("is_amazon_choice")),
        "is_prime": bool(p.get("is_prime")),
        "sales_volume_raw": p.get("sales_volume"),
        "url": p.get("product_url"),
    }


# ---------------------------------------------------------------- collection
def collect_group(group: str, queries: list[str], country: str, pages: int,
                  key: str | None, sleep: float, dry_run: bool,
                  from_cache: bool, write_cache: bool,
                  cache_max_age_hours: float = DEFAULT_CACHE_MAX_AGE_HOURS
                  ) -> tuple[list[dict], int | None]:
    """Union of listings across a group's keyword variants, deduped by ASIN.

    Dedupe matters: "honey" and "raw honey" return heavily overlapping results,
    and counting a listing's units once per keyword it ranks for would reward
    the group with the most redundant keywords rather than the most demand.
    """
    seen: dict[str, dict] = {}
    breadth: int | None = None

    for query in queries:
        for page in range(1, pages + 1):
            payload = None
            cp = cache_path(query, country, page)

            fresh = cache_is_fresh(cp, cache_max_age_hours)
            if dry_run:
                payload = dry_run_payload(group, country)
            elif from_cache or fresh:
                if not cp.exists():
                    print(f"    no cache for {query!r} p{page} — skipped")
                    continue
                payload = json.loads(cp.read_text())
                age = cache_age_hours(cp)
                if not fresh:
                    # --from-cache means no network, so a stale entry is the
                    # only thing available. Use it, but never quietly: the
                    # caller is about to treat it as current.
                    print(f"    STALE CACHE {query!r} p{page} — {age:.0f}h old "
                          f"(limit {cache_max_age_hours:g}h), using it anyway "
                          f"because --from-cache forbids a refetch")
                else:
                    print(f"    cached  {query!r} p{page} ({age:.0f}h old)")
            else:
                try:
                    payload = fetch(query, country, page, key)
                except RuntimeError as e:
                    print(f"    {query!r} p{page}: {e}")
                    continue
                if write_cache:
                    cp.parent.mkdir(parents=True, exist_ok=True)
                    cp.write_text(json.dumps(payload))
                time.sleep(sleep)

            tp = total_products_of(payload)
            # Breadth is taken from the group's primary (first) keyword only.
            # Summing it across variants would double-count the same catalogue.
            if query == queries[0] and page == 1 and isinstance(tp, int):
                breadth = tp

            rows = products_of(payload)
            for p in rows:
                row = listing_row(p, group, query)
                asin = row["asin"] or f"{query}:{row['title']}"
                # Keep the richer record when the same ASIN appears twice: a
                # listing that showed a sales badge on one keyword and not on
                # another is still a listing with known units.
                prev = seen.get(asin)
                if prev is None or (prev["units_past_month"] is None
                                    and row["units_past_month"] is not None):
                    seen[asin] = row
            print(f"    {query!r} p{page}: {len(rows)} listings "
                  f"({len(seen)} unique so far)")

    return list(seen.values()), breadth


# ---------------------------------------------------------------- summarising
def summarise_group(group: str, rows: list[dict], breadth: int | None) -> dict:
    with_units = [r for r in rows if r["units_past_month"]]
    units_total = sum(r["units_past_month"] for r in with_units)
    revenue = sum(r["est_monthly_revenue"] for r in rows if r["est_monthly_revenue"])
    prices = [r["price"] for r in rows if r["price"]]
    ratings = [int(r["num_ratings"] or 0) for r in rows]

    return {
        "group": group,
        "listings_sampled": len(rows),
        # --- recent, unit-denominated demand (the headline) ---
        "listings_with_sales_badge": len(with_units),
        "units_coverage_pct": round(len(with_units) / len(rows) * 100, 1) if rows else None,
        "units_past_month_floor": units_total,
        "units_per_badged_listing": round(units_total / len(with_units)) if with_units else None,
        # --- money ---
        "est_monthly_revenue_floor": round(revenue, 2),
        "median_price": round(statistics.median(prices), 2) if prices else None,
        # --- lifetime demand ---
        "cumulative_ratings": sum(ratings),
        "median_ratings_per_listing": int(statistics.median(ratings)) if ratings else None,
        "mean_star_rating": (
            round(statistics.mean([r["star_rating"] for r in rows if r["star_rating"]]), 2)
            if any(r["star_rating"] for r in rows) else None
        ),
        # --- competition ---
        "total_listings_on_primary_keyword": breadth,
        "demand_per_listing": (
            round(units_total / breadth, 3) if units_total and breadth else None
        ),
        "best_sellers": sum(1 for r in rows if r["is_best_seller"]),
        "amazon_choice": sum(1 for r in rows if r["is_amazon_choice"]),
    }


def competition_metrics(rows: list[dict], top_n: int = 10) -> dict:
    """Whether a category can be entered, as distinct from how big it is.

    Demand alone is a trap: the biggest category is usually the one most
    thoroughly owned. These three ask whether there is a slot free.
    """
    ratings = sorted((int(r["num_ratings"] or 0) for r in rows), reverse=True)
    prices = sorted(r["price"] for r in rows if r["price"])
    units = sorted((r["units_past_month"] or 0 for r in rows), reverse=True)
    total_units = sum(units)

    # Review moat: the median review count of the leaders, not of the whole
    # page. The tail of a search page is full of dead listings with 3 reviews,
    # and including them makes an entrenched category look wide open.
    leaders = ratings[:top_n]
    review_moat = int(statistics.median(leaders)) if leaders else None

    # Price dispersion as IQR/median — scale-free, so a $12 category and a
    # $600 one are comparable. Wide spread means room to position; tight
    # clustering means competing on price alone.
    dispersion = None
    if len(prices) >= 4:
        q1 = prices[len(prices) // 4]
        q3 = prices[3 * len(prices) // 4]
        med = statistics.median(prices)
        dispersion = round((q3 - q1) / med, 3) if med > 0 else None

    # Concentration: if three ASINs hold most of the volume, the category is
    # decided regardless of how many listings exist.
    top3_share = round(sum(units[:3]) / total_units, 3) if total_units else None

    return {
        "review_moat": review_moat,
        "price_dispersion": dispersion,
        "top3_unit_share": top3_share,
    }


def _ratio(a, b) -> str:
    if not a or not b:
        return "n/a"
    hi, lo = (a, b) if a >= b else (b, a)
    return f"{hi / lo:.1f}x"


def verdict(summaries: list[dict], country: str) -> list[str]:
    """Rank on each metric separately and say where they disagree.

    Deliberately not a single blended score. The metrics measure different
    quantities on different time bases; averaging them would produce one
    confident-looking number with no defensible unit.
    """
    out: list[str] = []
    if len(summaries) < 2:
        return ["Only one group — nothing to compare."]

    # The format spec is per-metric on purpose: demand_per_listing is a small
    # fraction, and rendering it with the same ",.0f" as the count metrics
    # rounds 0.53 to "1" and 0.35 to "0" — a 1.5x gap displayed as 1 vs 0.
    metrics = [
        ("units_past_month_floor", "Units bought in past month (floor)", "recent orders", ",.0f"),
        ("est_monthly_revenue_floor", "Estimated monthly revenue (floor)", "recent money", ",.0f"),
        ("cumulative_ratings", "Cumulative ratings", "lifetime demand", ",.0f"),
        ("demand_per_listing", "Units per competing listing", "demand vs competition", ",.3f"),
    ]

    for field, label, means, fmt in metrics:
        ranked = sorted(
            (s for s in summaries if s.get(field)),
            key=lambda s: s[field], reverse=True,
        )
        if not ranked:
            out.append(f"{label}: no data on any group.")
            continue
        top = ranked[0]
        rest = ", ".join(f"{s['group']} {s[field]:{fmt}}" for s in ranked[1:]) or "—"
        gap = _ratio(top[field], ranked[1][field]) if len(ranked) > 1 else "n/a"
        out.append(
            f"{label} ({means}): {top['group']} leads with "
            f"{top[field]:{fmt}} vs {rest} — {gap} gap."
        )

    coverage = ", ".join(
        f"{s['group']} {s['units_coverage_pct']}%" for s in summaries
        if s["units_coverage_pct"] is not None
    )
    out.append("")
    out.append(
        f"Sales-badge coverage ({country}): {coverage}. Every units figure is a "
        f"floor over that share of the sample only — if coverage differs a lot "
        f"between groups, the units comparison is biased toward the better-"
        f"covered one and the ratings column is the safer read."
    )
    return out


# ---------------------------------------------------------------- dry run
# Fixture-only. Rough units-per-USD, so a dry run against a non-USD
# marketplace produces prices on that marketplace's scale. Without this the
# fixture emits dollar-scale numbers against a rupee-denominated gate and the
# B2B price floor rejects everything — the gate behaving correctly on
# unrealistic data, which looks exactly like a broken gate.
#
# Deliberately NOT the same table as shortlist.MARKETS: that one holds
# thresholds a user tunes, this one holds fake prices. Sharing them would
# couple a fixture detail to a business rule.
_DRY_RUN_PRICE_SCALE = {"US": 1, "IN": 80, "JP": 150, "GB": 1, "DE": 1,
                        "CA": 1.4, "AU": 1.5, "AE": 4}


def dry_run_payload(group: str, country: str = "US") -> dict:
    """Shaped like a real response, with deliberately different demand
    profiles so the ranking, the ratio and the coverage warning all fire."""
    if group.startswith("moringa"):
        products = [
            {"asin": "B0MOR1", "product_title": "Cold-Pressed Moringa Oil 100ml",
             "product_price": "$18.99", "currency": "USD", "product_star_rating": "4.4",
             "product_num_ratings": 1240, "sales_volume": "500+ bought in past month",
             "is_amazon_choice": True, "is_prime": True,
             "product_url": "https://amazon.com/dp/B0MOR1"},
            {"asin": "B0MOR2", "product_title": "Organic Moringa Seed Oil 2oz",
             "product_price": "$24.50", "currency": "USD", "product_star_rating": "4.6",
             "product_num_ratings": 860, "sales_volume": "200+ bought in past month",
             "is_best_seller": True, "product_url": "https://amazon.com/dp/B0MOR2"},
            {"asin": "B0MOR3", "product_title": "Moringa Hair Oil 200ml",
             "product_price": "$12.00", "currency": "USD", "product_star_rating": "4.1",
             "product_num_ratings": 310, "sales_volume": None,
             "product_url": "https://amazon.com/dp/B0MOR3"},
        ]
        total = 2_000
    elif group.startswith("turmeric"):
        # Open category: low review moat, wide price spread, many listings.
        products = [
            {"asin": "B0TUR1", "product_title": "Turmeric Soap Bar 100g",
             "product_price": "$8.99", "currency": "USD", "product_star_rating": "4.3",
             "product_num_ratings": 420, "sales_volume": "2K+ bought in past month",
             "product_url": "https://amazon.com/dp/B0TUR1"},
            {"asin": "B0TUR2", "product_title": "Turmeric & Kojic Soap 2-pack",
             "product_price": "$14.99", "currency": "USD", "product_star_rating": "4.5",
             "product_num_ratings": 310, "sales_volume": "1K+ bought in past month",
             "product_url": "https://amazon.com/dp/B0TUR2"},
            {"asin": "B0TUR3", "product_title": "Organic Turmeric Cleansing Bar",
             "product_price": "$22.00", "currency": "USD", "product_star_rating": "4.2",
             "product_num_ratings": 150, "sales_volume": "500+ bought in past month",
             "product_url": "https://amazon.com/dp/B0TUR3"},
            {"asin": "B0TUR4", "product_title": "Turmeric Soap Gift Set",
             "product_price": "$34.50", "currency": "USD", "product_star_rating": "4.6",
             "product_num_ratings": 95, "sales_volume": "200+ bought in past month",
             "product_url": "https://amazon.com/dp/B0TUR4"},
            {"asin": "B0TUR5", "product_title": "Turmeric Face Soap 50g",
             "product_price": "$6.49", "currency": "USD", "product_star_rating": "4.0",
             "product_num_ratings": 60, "sales_volume": "100+ bought in past month",
             "product_url": "https://amazon.com/dp/B0TUR5"},
        ]
        total = 900
    elif group.startswith("shilajit"):
        # Closed category: entrenched incumbents, tight pricing.
        products = [
            {"asin": "B0SHI1", "product_title": "Pure Shilajit Resin 30g",
             "product_price": "$29.99", "currency": "USD", "product_star_rating": "4.4",
             "product_num_ratings": 38000, "sales_volume": "3K+ bought in past month",
             "is_best_seller": True, "product_url": "https://amazon.com/dp/B0SHI1"},
            {"asin": "B0SHI2", "product_title": "Himalayan Shilajit Resin",
             "product_price": "$31.50", "currency": "USD", "product_star_rating": "4.3",
             "product_num_ratings": 24000, "sales_volume": "2K+ bought in past month",
             "product_url": "https://amazon.com/dp/B0SHI2"},
            {"asin": "B0SHI3", "product_title": "Shilajit Resin Gold Grade",
             "product_price": "$28.00", "currency": "USD", "product_star_rating": "4.5",
             "product_num_ratings": 19500, "sales_volume": "1K+ bought in past month",
             "product_url": "https://amazon.com/dp/B0SHI3"},
            {"asin": "B0SHI4", "product_title": "Shilajit Drops 60ml",
             "product_price": "$26.99", "currency": "USD", "product_star_rating": "4.1",
             "product_num_ratings": 12000, "sales_volume": "900+ bought in past month",
             "product_url": "https://amazon.com/dp/B0SHI4"},
        ]
        total = 4200
    elif not group.startswith("honey"):
        # Unnamed keyword: a stable, varied competition profile. Constant
        # fixtures here would give every candidate in a large dry run the same
        # review moat and the same price, collapsing the openness and headroom
        # components to 0.5 across the board — a ranking with nothing to rank.
        h = int(hashlib.md5(group.encode()).hexdigest(), 16)
        scale = _DRY_RUN_PRICE_SCALE.get(country.upper(), 1)
        moat = 80 * (2 ** (h % 10))              # ~80 to ~40,000
        base = (4 + (h // 7) % 90) * scale       # local-currency scale
        spread = 1 + (h // 11) % 4               # tight to wide pricing
        units = 100 * (1 + (h // 13) % 40)
        products = [
            {"asin": f"B0{h % 100000:05d}{i}",
             "product_title": f"{group} variant {i}",
             "product_price": f"${round(base * (1 + i * spread * 0.25), 2)}",
             "currency": "USD",
             "product_star_rating": f"{4.0 + (h // (17 + i)) % 9 / 10:.1f}",
             "product_num_ratings": max(1, moat // (1 + i)),
             "sales_volume": f"{max(50, units // (1 + i))}+ bought in past month",
             "product_url": f"https://amazon.com/dp/B0{h % 100000:05d}{i}"}
            for i in range(6)
        ]
        total = 500 + (h // 3) % 40000
    else:
        products = [
            {"asin": "B0HON1", "product_title": "Raw Unfiltered Honey 32oz",
             "product_price": "$14.99", "currency": "USD", "product_star_rating": "4.7",
             "product_num_ratings": 48200, "sales_volume": "10K+ bought in past month",
             "is_best_seller": True, "is_prime": True,
             "product_url": "https://amazon.com/dp/B0HON1"},
            {"asin": "B0HON2", "product_title": "Organic Clover Honey 24oz",
             "product_price": "$9.49", "currency": "USD", "product_star_rating": "4.6",
             "product_num_ratings": 21500, "sales_volume": "5K+ bought in past month",
             "is_amazon_choice": True, "product_url": "https://amazon.com/dp/B0HON2"},
            {"asin": "B0HON3", "product_title": "Manuka Honey UMF 15+ 250g",
             "product_price": "$59.00", "currency": "USD", "product_star_rating": "4.5",
             "product_num_ratings": 6100, "sales_volume": "1K+ bought in past month",
             "product_url": "https://amazon.com/dp/B0HON3"},
        ]
        total = 30_000
    return {"status": "OK", "data": {"country": "US", "total_products": total,
                                     "products": products}}


# ---------------------------------------------------------------- output
def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def write_markdown(path: Path, summaries: list[dict], lines: list[str],
                   country: str, pages: int, groups: dict[str, list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = list(summaries[0].keys())
    md = [
        "# Amazon demand comparison",
        "",
        f"Marketplace: **{country}** · top {pages} search page(s) per keyword · "
        f"{len(summaries)} product groups.",
        "",
        "## Verdict",
        "",
        *[f"- {ln}" for ln in lines if ln],
        "",
        "## Summary table",
        "",
        "| " + " | ".join(cols) + " |",
        "| " + " | ".join("---" for _ in cols) + " |",
    ]
    for s in summaries:
        md.append("| " + " | ".join("" if s[c] is None else str(s[c]) for c in cols) + " |")
    md += [
        "",
        "## Keywords searched",
        "",
        *[f"- **{g}**: {', '.join(qs)}" for g, qs in groups.items()],
        "",
        "## How to read this",
        "",
        "- `units_past_month_floor` sums Amazon's \"N+ bought in past month\" badge. "
        "The badge is a bucketed floor and appears on only some listings, so the "
        "total understates the truth by an unknown amount. Check "
        "`units_coverage_pct` first.",
        "- `cumulative_ratings` is lifetime, not current, and only a small "
        "single-digit share of buyers rate. Use it for category size, not for "
        "this month's orders.",
        "- `demand_per_listing` is the number a seller should care about: a large "
        "category split across thousands of listings can be a worse entry than a "
        "small one with few.",
        "- Keyword demand is not category demand. A staple grocery term reaches a "
        "much wider population than a niche cosmetic-ingredient term; the gap "
        "between them is partly a gap in what the words mean.",
    ]
    path.write_text("\n".join(md) + "\n", encoding="utf-8")


def parse_group_args(specs: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for spec in specs:
        if "=" not in spec:
            sys.exit(f"--group needs NAME=kw1,kw2 — got {spec!r}")
        name, kws = spec.split("=", 1)
        queries = [k.strip() for k in kws.split(",") if k.strip()]
        if not queries:
            sys.exit(f"--group {name!r} has no keywords")
        groups[name.strip()] = queries
    return groups


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare Amazon demand between product groups.")
    ap.add_argument("--group", action="append", default=[],
                    help="NAME=kw1,kw2 (repeatable). Default: moringa oil vs honey.")
    ap.add_argument("--country", default="US", help="Amazon marketplace, e.g. US, IN, GB, DE")
    ap.add_argument("--pages", type=int, default=2, help="search pages per keyword")
    ap.add_argument("--sort-by", default="RELEVANCE")
    ap.add_argument("--sleep", type=float, default=1.0, help="seconds between API calls")
    ap.add_argument("--dry-run", action="store_true",
                    help="exercise every path except the network call")
    ap.add_argument("--from-cache", action="store_true",
                    help="analyse previously cached responses only, no network")
    ap.add_argument("--no-cache", action="store_true", help="do not write raw responses")
    ap.add_argument("--cache-max-age-hours", type=float,
                    default=DEFAULT_CACHE_MAX_AGE_HOURS,
                    help="refetch cached responses older than this; 0 = never expire")
    ap.add_argument("--out", default="reports")
    args = ap.parse_args()

    groups = parse_group_args(args.group) if args.group else DEFAULT_GROUPS
    country = args.country.upper()

    if args.dry_run:
        print("DRY RUN — no network call is made.\n")
        print("  URL shape (key travels in the x-api-key header, not the URL):")
        print(f"    {redact(build_url('moringa oil', country))}\n")
        key = None
    elif args.from_cache:
        print(f"CACHE ONLY — reading {CACHE_DIR}/, no network call is made.\n")
        key = None
    else:
        key = api_key()

    all_rows: list[dict] = []
    summaries: list[dict] = []
    for group, queries in groups.items():
        print(f"  {group}:")
        rows, breadth = collect_group(
            group, queries, country, args.pages, key, args.sleep,
            args.dry_run, args.from_cache, not args.no_cache,
            args.cache_max_age_hours,
        )
        if not rows:
            print(f"    nothing collected for {group}")
            continue
        all_rows += rows
        summaries.append(summarise_group(group, rows, breadth))

    if not summaries:
        print("\nNothing fetched. If every keyword failed on reachability, this "
              "environment's egress policy is the cause — see the module docstring.")
        return 1

    print("\n--- Summary ---")
    for s in summaries:
        print(f"  {s['group']:<14s} units(floor) {s['units_past_month_floor']:>9,}  "
              f"coverage {s['units_coverage_pct']}%  "
              f"ratings {s['cumulative_ratings']:>10,}  "
              f"median price {s['median_price']}")

    print("\n--- Verdict ---")
    lines = verdict(summaries, country)
    for ln in lines:
        print(f"  {ln}" if ln else "")

    out = Path(args.out)
    listings_csv = out / "amazon_demand_listings.csv"
    summary_csv = out / "amazon_demand_summary.csv"
    md_path = out / "amazon_demand_comparison.md"
    write_csv(listings_csv, all_rows)
    write_csv(summary_csv, summaries)
    write_markdown(md_path, summaries, lines, country, args.pages, groups)
    print(f"\nWritten: {listings_csv}\n         {summary_csv}\n         {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
