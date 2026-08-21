#!/usr/bin/env python3
"""
Rank keywords by whether Amazon demand for them is RISING, using Nexscope's
Amazon Keyword Search History API.

WHY THIS EXISTS SEPARATELY FROM competition.py
--------------------------------------------------------
That module ranks a snapshot: who is selling most today. This one ranks a
derivative: whose demand is growing. For picking a product to sell those are
different questions, and the snapshot answers the less useful one — by the time
a category is visibly the biggest, entering it means fighting incumbents with
review moats. A term with a quarter of the volume and a steady climb is the
better slot.

THE SCHEMA HERE IS ASSUMED, NOT VERIFIED
----------------------------------------
www.nexscope.ai is refused by the egress proxy in the environment this was
written in, so the docs page could not be read. Endpoint, auth header and
response shape below are educated guesses, and the guesses are isolated:

  * ENDPOINT and the auth header are overridable from the CLI/env, so a wrong
    guess is a flag, not an edit.
  * The response parser accepts every plausible field name rather than one
    (SERIES_KEYS / DATE_KEYS / VOLUME_KEYS below).
  * When nothing matches, it raises SchemaMismatch listing the keys the API
    ACTUALLY returned. That is the important part: a rigid parser that guessed
    wrong would return an empty series, and an empty series reads exactly like
    "no demand" — a silently wrong trend is worse than a loud failure.

So the first live run either works or tells you precisely which names to fix.
Correct them in the *_KEYS tuples and nothing else changes.

CREDENTIALS
-----------
Read from the environment, never from this file and never committed:

    export NEXSCOPE_API_KEY=...

Usage:
    python3 amazon_selector/trends.py --dry-run
    python3 amazon_selector/trends.py \
        --keywords "moringa oil,raw honey,mouth tape"
    python3 amazon_selector/trends.py \
        --keywords-file candidates_in.txt --country US --weeks 52
    python3 amazon_selector/trends.py \
        --auth-header X-API-Key --endpoint https://api.nexscope.ai/v1/...
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import statistics
import sys
import time as _time
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

# --- Endpoint resolution ----------------------------------------------------
# The base is known (NEXSCOPE_PROXY_BASE); the PATH below is still an
# assumption, because the docs page could not be read. Resolution order:
#   NEXSCOPE_ENDPOINT (full URL)  >  NEXSCOPE_PROXY_BASE + DEFAULT_PATH  >  default
DEFAULT_BASE = "https://api.nexscope.ai/"
DEFAULT_PATH = "v1/amazon/keyword-search-history"
DEFAULT_AUTH_HEADER = "Authorization"
DEFAULT_AUTH_PREFIX = "Bearer "


def default_endpoint() -> str:
    full = os.environ.get("NEXSCOPE_ENDPOINT")
    if full:
        return full
    base = os.environ.get("NEXSCOPE_PROXY_BASE", DEFAULT_BASE)
    return base.rstrip("/") + "/" + DEFAULT_PATH.lstrip("/")

# --- ASSUMED response shapes. First match wins; extend rather than replace. --
SERIES_KEYS = ("history", "search_volume_history", "series", "trends",
               "data_points", "results", "records", "data")
DATE_KEYS = ("date", "week", "week_start", "week_starting", "period",
             "period_start", "timestamp", "day", "month")
VOLUME_KEYS = ("search_volume", "searchVolume", "volume", "searches",
               "search_count", "value", "sv", "count")
RANK_KEYS = ("search_frequency_rank", "searchFrequencyRank", "rank", "sfr")

CACHE_DIR = Path("data/nexscope_raw")

# A weekly search-volume series gains at most one new point per week, so a
# day-old copy is identical to a fresh fetch and refetching it only spends
# quota. Deliberately much longer than the competition module's 24h: that one
# reads prices and rankings, which move daily.
DEFAULT_CACHE_MAX_AGE_HOURS = 168.0


def cache_is_fresh(path: Path, max_age_hours: float) -> bool:
    """A cache with no expiry is worse than no cache: it answers today's
    question with last month's data and nothing downstream can tell.
    max_age_hours <= 0 disables expiry (keep forever)."""
    if not path.exists():
        return False
    if max_age_hours <= 0:
        return True
    return (_time.time() - path.stat().st_mtime) / 3600.0 <= max_age_hours


def cache_age_hours(path: Path) -> float | None:
    if not path.exists():
        return None
    return (_time.time() - path.stat().st_mtime) / 3600.0


class SchemaMismatch(RuntimeError):
    """The response did not contain a recognisable time series.

    Carries the keys actually seen, because the whole point of an assumed
    schema is that the first real response tells you what to fix.
    """


# --------------------------------------------------------------- credentials
def api_key() -> str:
    key = os.environ.get("NEXSCOPE_API_KEY")
    if not key:
        sys.exit(
            "Set NEXSCOPE_API_KEY in the environment. Do not put it in this "
            "file — tests/test_trends.py scans the tree for it."
        )
    return key


def redact(text: str) -> str:
    key = os.environ.get("NEXSCOPE_API_KEY")
    return text.replace(key, "<NEXSCOPE_API_KEY>") if key else text


# ---------------------------------------------------------------- fetching
def build_url(endpoint: str, keyword: str, country: str,
              start: str | None, end: str | None) -> str:
    params = {"keyword": keyword, "country": country}
    if start:
        params["start_date"] = start
    if end:
        params["end_date"] = end
    return f"{endpoint}?{urllib.parse.urlencode(params)}"


def cache_path(keyword: str, country: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "_", keyword.lower()).strip("_")
    return CACHE_DIR / f"{slug}__{country.lower()}.json"


def fetch(keyword: str, country: str, key: str, endpoint: str,
          auth_header: str, auth_prefix: str,
          start: str | None = None, end: str | None = None,
          timeout: int = 30, retries: int = 3) -> dict:
    url = build_url(endpoint, keyword, country, start, end)
    last = ""
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={auth_header: f"{auth_prefix}{key}"})
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
                    f"{last} for {redact(url)}. Auth rejected — check "
                    f"NEXSCOPE_API_KEY, and check the auth scheme: this module "
                    f"assumes '{auth_header}: {auth_prefix.strip()} <key>'. If "
                    f"the API wants a bare key or a different header, pass "
                    f"--auth-header / --auth-prefix."
                ) from None
            if e.code == 404:
                raise RuntimeError(
                    f"{last} for {redact(url)}. The endpoint path is an "
                    f"assumption in this module — pass the real one with "
                    f"--endpoint (or set NEXSCOPE_ENDPOINT)."
                ) from None
            raise RuntimeError(f"{last} for {redact(url)}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"cannot reach {urllib.parse.urlparse(endpoint).netloc} "
                f"({e.reason}). If this is the Claude Code sandbox, that host "
                f"is refused by the egress proxy with a 403 — run this where "
                f"you have ordinary internet access."
            ) from None
    raise RuntimeError(f"gave up after {retries} attempts ({last})")


# ---------------------------------------------------------------- parsing
def _first_key(d: dict, candidates: tuple[str, ...]):
    for k in candidates:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _find_series(payload) -> list[dict]:
    """Locate the time-series array wherever the response happens to nest it.

    Walks the response rather than assuming a path, because the nesting depth
    ({'data': {'history': [...]}} vs {'history': [...]}) is exactly the kind of
    detail an assumed schema gets wrong, and it is also the cheapest to absorb.
    """
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if not isinstance(payload, dict):
        return []
    for key in SERIES_KEYS:
        val = payload.get(key)
        if isinstance(val, list) and val and isinstance(val[0], dict):
            return val
    # One level down, any container key.
    for val in payload.values():
        if isinstance(val, (dict, list)):
            found = _find_series(val)
            if found:
                return found
    return []


def _parse_date(raw) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):  # epoch seconds or ms
        secs = raw / 1000 if raw > 10_000_000_000 else raw
        try:
            return datetime.utcfromtimestamp(secs).date()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%m/%d/%Y", "%Y-%m", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _parse_number(raw) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    cleaned = re.sub(r"[^\d.\-]", "", str(raw).replace(",", ""))
    if not cleaned or cleaned in ("-", ".") or cleaned.count(".") > 1:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_series(payload, keyword: str) -> list[tuple[date, float, float | None]]:
    """-> [(date, search_volume, rank_or_None)], ascending by date.

    Raises SchemaMismatch naming the observed keys when nothing parses, so one
    live call is enough to correct the assumptions at the top of this file.
    """
    raw_points = _find_series(payload)
    if not raw_points:
        top = list(payload)[:20] if isinstance(payload, dict) else type(payload).__name__
        raise SchemaMismatch(
            f"no time series found for {keyword!r}. Top-level keys: {top}. "
            f"Add the right key to SERIES_KEYS in this module."
        )

    points: list[tuple[date, float, float | None]] = []
    for p in raw_points:
        d = _parse_date(_first_key(p, DATE_KEYS))
        v = _parse_number(_first_key(p, VOLUME_KEYS))
        rank = _parse_number(_first_key(p, RANK_KEYS))
        if d is not None and v is not None:
            points.append((d, v, rank))

    if not points:
        raise SchemaMismatch(
            f"found {len(raw_points)} records for {keyword!r} but none had a "
            f"parseable date+volume pair. Record keys: {list(raw_points[0])}. "
            f"Add the right names to DATE_KEYS / VOLUME_KEYS in this module."
        )
    points.sort(key=lambda t: t[0])
    return points


# ---------------------------------------------------------------- trend math
def infer_period_days(points) -> int:
    """Weekly vs monthly vs daily, from the median gap between observations."""
    if len(points) < 2:
        return 7
    gaps = [(points[i][0] - points[i - 1][0]).days for i in range(1, len(points))]
    gaps = [g for g in gaps if g > 0]
    return int(statistics.median(gaps)) if gaps else 7


def log_slope(values: list[float]) -> float | None:
    """Least-squares slope of log(volume) against index = growth per period.

    Fitted in log space so the answer is a growth RATE rather than an absolute
    step: a term going 100 -> 200 and one going 10,000 -> 20,000 are the same
    opportunity signal, and a linear fit would rank the second 100x higher.
    Zero and negative readings are dropped rather than clamped — log(0) is not
    a small number, it is undefined, and substituting one silently invents data.
    """
    pairs = [(i, v) for i, v in enumerate(values) if v > 0]
    if len(pairs) < 3:
        return None
    xs = [p[0] for p in pairs]
    ys = [math.log(p[1]) for p in pairs]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


def trend_fit_r2(values: list[float]) -> float | None:
    """R^2 of the log-linear fit — how much of the series the TREND explains.

    This exists because the obvious noise measure is wrong here. The raw
    coefficient of variation of a cleanly compounding series is large *by
    construction*: a term growing 4.5%/week for a year triples, and a CV
    computed on those raw levels is dominated by the growth itself. Using CV
    alone to detect "spiky" therefore penalises precisely the clean risers this
    tool exists to find.

    Residual variation around the fitted trend is the quantity actually wanted:
    a clean exponential scores near 1.0 however steep it is, while a series that
    swings without going anywhere scores near 0.
    """
    pairs = [(i, v) for i, v in enumerate(values) if v > 0]
    if len(pairs) < 3:
        return None
    xs = [p[0] for p in pairs]
    ys = [math.log(p[1]) for p in pairs]
    slope = log_slope(values)
    if slope is None:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    intercept = my - slope * mx
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    if ss_tot <= 0:
        return 1.0
    return max(0.0, 1.0 - ss_res / ss_tot)


def momentum(values: list[float], window: int = 4) -> float | None:
    """Recent window mean vs the window before it, as a fraction.

    Deliberately not "latest vs oldest": a single spiky reading at either end
    swings that badly, and search volume is spiky (one viral post, one holiday).
    """
    if len(values) < window * 2:
        return None
    recent = statistics.mean(values[-window:])
    prior = statistics.mean(values[-window * 2:-window])
    if prior <= 0:
        return None
    return recent / prior - 1


def year_over_year(points, period_days: int) -> float | None:
    """Latest reading vs the closest reading ~365 days earlier.

    The metric that separates a trend from a season. "Up 130%" measured from a
    trough in a strongly seasonal category is not an opportunity, and momentum
    alone cannot tell the two apart.
    """
    if not points:
        return None
    latest_date, latest_val = points[-1][0], points[-1][1]
    target = latest_date - timedelta(days=365)
    if points[0][0] > target + timedelta(days=period_days):
        return None  # series does not reach back a year
    prior = min(points, key=lambda p: abs((p[0] - target).days))
    if abs((prior[0] - target).days) > max(period_days * 2, 14) or prior[1] <= 0:
        return None
    return latest_val / prior[1] - 1


def summarise_keyword(keyword: str, points, window: int = 4) -> dict:
    values = [p[1] for p in points]
    period_days = infer_period_days(points)
    slope = log_slope(values)
    mean_v = statistics.mean(values)

    # Coefficient of variation. Reported next to every growth number because a
    # high-CV series can produce a large momentum figure out of pure noise.
    cv = (statistics.pstdev(values) / mean_v) if mean_v > 0 else None
    peak = max(values)

    return {
        "keyword": keyword,
        "observations": len(points),
        "period_days": period_days,
        "first_date": points[0][0].isoformat(),
        "last_date": points[-1][0].isoformat(),
        "latest_volume": round(values[-1]),
        "mean_volume": round(mean_v),
        "peak_volume": round(peak),
        "pct_of_peak": round(values[-1] / peak * 100, 1) if peak > 0 else None,
        "growth_per_period_pct": round((math.exp(slope) - 1) * 100, 2) if slope is not None else None,
        "momentum_pct": round(momentum(values, window) * 100, 1) if momentum(values, window) is not None else None,
        "yoy_pct": round(year_over_year(points, period_days) * 100, 1) if year_over_year(points, period_days) is not None else None,
        "volatility_cv": round(cv, 3) if cv is not None else None,
        "trend_fit_r2": round(trend_fit_r2(values), 3) if trend_fit_r2(values) is not None else None,
        "latest_rank": points[-1][2],
    }


def opportunity_flag(s: dict) -> str:
    """A label, not a score.

    A single blended number would hide the one thing that matters here: whether
    growth survives the volatility and seasonality checks. Each label states
    which test decided it.
    """
    g = s.get("growth_per_period_pct")
    mom = s.get("momentum_pct")
    yoy = s.get("yoy_pct")
    cv = s.get("volatility_cv")
    r2 = s.get("trend_fit_r2")

    if g is None and mom is None:
        return "no-trend-data"
    # Seasonality is checked BEFORE volatility, though the reverse reads more
    # naturally. A strongly seasonal series has high variance *by definition*,
    # so a volatility-first ordering labels every seasonal category "spiky" and
    # the seasonal branch never fires — which is exactly what the dry run did
    # before this ordering. "Up short-term, below the same week last year" is a
    # specific diagnosis; "spiky" is the fallback for variance with no better
    # explanation, so the specific test has to run first.
    if yoy is not None and yoy < 0 and (mom or 0) > 0:
        return "seasonal bounce — down year-over-year"
    # Spiky means "swings the trend does NOT explain" — hence the r2 term.
    # Testing cv alone would flag every steep clean riser, because compounding
    # produces a large raw spread on its own.
    if r2 is not None and r2 < 0.3 and cv is not None and cv > 0.6 and (mom or 0) > 0:
        return "spiky — growth may be noise"
    if (g or 0) > 0.5 and (mom or 0) > 10:
        return "RISING"
    if (g or 0) < -0.5 and (mom or 0) < -10:
        return "declining"
    return "flat"


# ---------------------------------------------------------------- dry run
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


def load_keywords(args) -> list[str]:
    if args.keywords_file:
        text = Path(args.keywords_file).read_text(encoding="utf-8")
        kws = [ln.strip() for ln in text.splitlines() if ln.strip()
               and not ln.strip().startswith("#")]
    else:
        kws = [k.strip() for k in args.keywords.split(",") if k.strip()]
    if not kws:
        sys.exit("No keywords given — use --keywords or --keywords-file.")
    return kws


def main() -> int:
    ap = argparse.ArgumentParser(description="Rank Amazon keywords by demand trend.")
    ap.add_argument("--keywords", default="moringa oil,honey,mouth tape,sleep gummies",
                    help="comma-separated keywords")
    ap.add_argument("--keywords-file", help="one keyword per line; # comments ignored")
    ap.add_argument("--country", default="US")
    ap.add_argument("--weeks", type=int, default=52,
                    help="how far back to request (sets start_date)")
    ap.add_argument("--window", type=int, default=4, help="momentum window, periods")
    ap.add_argument("--endpoint", default=default_endpoint(),
                    help="full URL; defaults to NEXSCOPE_ENDPOINT, else "
                         "NEXSCOPE_PROXY_BASE + the assumed path")
    ap.add_argument("--auth-header", default=DEFAULT_AUTH_HEADER)
    ap.add_argument("--auth-prefix", default=DEFAULT_AUTH_PREFIX,
                    help="use --auth-prefix '' for a bare key")
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--from-cache", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--cache-max-age-hours", type=float,
                    default=DEFAULT_CACHE_MAX_AGE_HOURS,
                    help="refetch cached series older than this; 0 = never expire")
    ap.add_argument("--out", default="reports")
    args = ap.parse_args()

    keywords = load_keywords(args)
    country = args.country.upper()
    start = (date.today() - timedelta(weeks=args.weeks)).isoformat()

    if args.dry_run:
        print("DRY RUN — no network call is made.\n")
        print("  Assumed request shape:")
        print(f"    GET {redact(build_url(args.endpoint, keywords[0], country, start, None))}")
        print(f"    {args.auth_header}: {args.auth_prefix}<NEXSCOPE_API_KEY>\n")
        key = None
    elif args.from_cache:
        print(f"CACHE ONLY — reading {CACHE_DIR}/, no network call is made.\n")
        key = None
    else:
        key = api_key()

    rows: list[dict] = []
    for kw in keywords:
        cp = cache_path(kw, country)
        fresh = cache_is_fresh(cp, args.cache_max_age_hours)
        if args.dry_run:
            payload = dry_run_payload(kw)
        elif args.from_cache or fresh:
            if not cp.exists():
                print(f"  {kw!r}: no cache — skipped")
                continue
            payload = json.loads(cp.read_text())
            if not fresh:
                print(f"  {kw!r}: STALE CACHE {cache_age_hours(cp):.0f}h old "
                      f"(limit {args.cache_max_age_hours:g}h), using it anyway "
                      f"because --from-cache forbids a refetch")
        else:
            try:
                payload = fetch(kw, country, key, args.endpoint,
                                args.auth_header, args.auth_prefix, start=start)
            except RuntimeError as e:
                print(f"  {kw!r}: {e}")
                continue
            if not args.no_cache:
                cp.parent.mkdir(parents=True, exist_ok=True)
                cp.write_text(json.dumps(payload))
            time.sleep(args.sleep)

        try:
            points = parse_series(payload, kw)
        except SchemaMismatch as e:
            print(f"  {kw!r}: SCHEMA MISMATCH — {e}")
            continue

        s = summarise_keyword(kw, points, args.window)
        s["opportunity_flag"] = opportunity_flag(s)
        rows.append(s)
        print(f"  {kw:<20s} n={s['observations']:<4d} "
              f"latest {s['latest_volume']:>8,}  "
              f"growth/period {s['growth_per_period_pct']}%  "
              f"momentum {s['momentum_pct']}%  → {s['opportunity_flag']}")

    if not rows:
        print("\nNothing parsed. If every keyword failed on reachability, this "
              "environment's egress policy is the cause. If they failed on "
              "SCHEMA MISMATCH, the message above names the keys the API "
              "actually returned — correct the *_KEYS tuples in this module.")
        return 1

    rows.sort(key=lambda r: (r["momentum_pct"] is None, -(r["momentum_pct"] or 0)))

    print("\n--- Ranked by momentum ---")
    for r in rows:
        print(f"  {r['keyword']:<20s} {r['opportunity_flag']:<32s} "
              f"yoy {r['yoy_pct']}%  cv {r['volatility_cv']}")

    out = Path(args.out)
    write_csv(out / "nexscope_keyword_trends.csv", rows)
    write_markdown(out / "nexscope_keyword_trends.md", rows, country)
    print(f"\nWritten: {out / 'nexscope_keyword_trends.csv'}"
          f"\n         {out / 'nexscope_keyword_trends.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
