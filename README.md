# amazon-product-selector

Shortlist Amazon products worth selling: rank keywords by **rising** demand,
then gate on whether the category can actually be entered — separately for
consumer retail and for bulk B2B. Defaults to **Amazon India**.

```bash
pip install -r requirements.txt
python amazon_selector/shortlist.py --dry-run          # no API key, no network

export NEXSCOPE_API_KEY=...  NEXSCOPE_PROXY_BASE=https://api.nexscope.ai/
export OPENWEB_NINJA_API_KEY=...
python amazon_selector/shortlist.py --keywords-file candidates_in.txt
```

```
=== B2C — consumer retail · IN (INR) ===
  --- shortlist (3 of 4) ---
    1. giloy juice     score 0.789  (growth 0.50 demand 0.93 openness 0.94 headroom 1.00)
    2. aloe vera gel   score 0.445  (growth 0.50 demand 1.00 openness 0.07 headroom 0.00)
    3. moringa oil     score 0.375  (growth 0.50 demand 0.00 openness 0.50 headroom 0.50)
  --- rejected (1) ---
    chyawanprash     trend is 'flat', not in ['RISING']

=== B2B — Amazon Business / bulk · IN (INR) ===
  --- shortlist (2 of 4) ---
    1. giloy juice     score 1.000
    2. chyawanprash    score 0.000
  --- rejected (2) ---
    aloe vera gel    trend is 'flat', not in ['RISING']
    moringa oil      median price 18.99 below 2000

=== B2C vs B2B ===
  shortlisted in both: giloy juice
  B2C only:            aloe vera gel, moringa oil
  B2B only:            chyawanprash
```

## The idea

Picking a product to sell needs two answers, and most tools give only one.

**Is demand rising?** The biggest category is usually the most thoroughly owned.
By the time a niche is visibly the largest, entering it means fighting incumbents
with review moats built over years. A term with a quarter of the volume and a
steady climb is the better slot.

**Can you get in?** Growth in a category where three ASINs hold every unit and
the leaders carry 40,000 reviews each is somebody else's growth.

## Gate first, then rank

The pipeline does **not** blend everything into one number and sort. It applies
pass/fail gates, records *why* each rejection failed, and ranks only survivors.

A blended score would let a strong growth number outvote a disqualifying review
moat, and those are not commensurable — no amount of growth makes an entrenched
category enterable. So the moat disqualifies rather than subtracts.

**The rejected table is usually the more useful half.** "Rising, but seasonal"
and "open, but nobody searches for it" are different next actions.

Trend runs before competition on purpose — the competition call bills per listing
returned, and roughly half of any candidate list dies on the trend gate.

## Marketplace: India by default

`--country` defaults to **IN**. Thresholds denominated in money or in absolute
search counts cannot be one global number, so they live in a `MARKETS` table and
resolve per run:

| | US | IN | Why |
|---|---|---|---|
| B2B price floor | $25 | ₹2,000 | ₹25 is about $0.30 — a currency-naive floor passes literally everything and the gate silently stops existing |
| Volume gate scale | ×1.0 | ×0.2 | Amazon India's absolute search counts sit far below the US's for the same category; an unscaled floor rejects viable Indian niches for being Indian |

Price floors are converted at rough spot rates; **the volume scales are
estimates, not measurements.** Nothing here is calibrated against observed data.
They sit in one table so retuning after a first real run is a one-line edit.

`candidates_in.txt` (119 keywords) is the India universe; `candidates_us.txt` is
kept for the US. The catalogues genuinely differ rather than translating —
ayurvedic and hair-oil categories are mainstream in one and niche in the other.

## B2C and B2B are separate analyses

Not one run with a filter, because they do not share a search series. A consumer
searches `moringa oil`; a purchasing manager searches `bulk moringa oil` or
`moringa oil case pack`. Different volumes, different seasonality — one trend
read cannot describe both. `--profile b2c | b2b | both` (default `both`).

| | B2C | B2B | Why |
|---|---|---|---|
| Trend term | `{kw}` | `bulk {kw}` | The searches genuinely differ |
| Competition queries | `{kw}` | `bulk`, `case pack`, `wholesale`, `commercial` | Procurement language |
| Min volume | 500 | 50 | Bulk terms carry a fraction of consumer volume — the same standard at smaller scale, not a laxer one |
| Max review moat | 2,000 | 25,000 | Consumer conversion runs on social proof; procurement is spec- and price-driven. Looser, still gated |
| Min median price | — | market floor | B2B earns on order value, not unit count |
| Weights | growth 35 · demand 25 · openness 30 · headroom 10 | growth 35 · demand 15 · openness 20 · headroom 30 | Growth leads in both — the question is unchanged. Tiering is where B2B margin lives |

**A term on only one side is the signal.** The same product can be saturated at
retail and open in bulk, or the reverse.

## Caching

Raw responses cache under `data/` and **expire**: competition snapshots after
24h, trend series after 168h. Not arbitrary — prices and rankings move daily,
while a weekly search-volume series gains one point a week, so refetching it
daily only spends quota. `--cache-max-age-hours` and
`--trend-cache-max-age-hours` override; `0` disables expiry.

A cache with no expiry is worse than no cache: it answers today's question with
last month's data and nothing downstream can tell. Under `--from-cache` a stale
entry is still used — with no network it is all there is — but never quietly:
the run prints its age and the limit it exceeded.

## The three ways a rising line lies

| Verdict | What it caught |
|---|---|
| `seasonal bounce — down year-over-year` | Up sharply this month, below the same week last year. A trough recovery, not growth |
| `spiky — growth may be noise` | Swings the trend does not explain. Keyed on the **R² of the log fit**, not raw spread — a cleanly compounding series has a large raw spread *by construction*, so a naive volatility check rejects exactly the risers you want |
| `flat` | Moving, but not enough to matter |

Seasonality is checked **before** volatility. A strongly seasonal series is
high-variance by definition, so a volatility-first ordering labels every seasonal
category "spiky" and the specific diagnosis never fires.

## Metrics

**Trend** — `growth_per_period_pct` is a least-squares slope on **log** volume,
so it is a rate: 100→200 and 10,000→20,000 score alike, where a linear fit ranks
the second 100× higher. `momentum_pct` compares the last 4 periods to the prior 4
rather than the endpoints, because one viral week should not decide a verdict.
`yoy_pct` is the seasonality control. `trend_fit_r2` separates trend from noise.

**Competition** — `review_moat` is the median review count of the *leaders*, not
the whole page: the tail of a search page is dead listings with 3 reviews, and
including them makes an entrenched category look wide open. `price_dispersion` is
IQR/median, so a ₹900 and a ₹40,000 category compare. `demand_per_listing` and
`top3_unit_share` measure how contested the slot is.

> **The score is relative to the candidate set.** Components are min-max
> normalised across the candidates *you* supply, so adding an eleventh changes
> the other ten. `0.9` means "best of what you supplied", never "good in absolute
> terms". Compare within a run, never across runs.

## Data sources

Two paid APIs, both swappable — see [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md)
for the survey: which sources report real orders (almost none), which model them
from Best Sellers Rank, and two commonly-recommended Amazon APIs that report
neither.

The Nexscope response schema is **assumed** — the docs host was unreachable from
the environment this was written in. The guesses are isolated: `--endpoint`,
`--auth-header` and `--auth-prefix` override the request side, the parser accepts
every plausible field name at any nesting depth, and when nothing matches it
raises `SchemaMismatch` **naming the keys the API actually returned**. A rigid
parser guessing wrong would return an empty series, and an empty series is
indistinguishable from "nobody searches for this" — a silently wrong trend is
worse than a crash. One live call is enough to correct `trends.py`.

## Credentials

Environment only, never in source:

```bash
export NEXSCOPE_API_KEY=...          # trends
export OPENWEB_NINJA_API_KEY=...     # competition
```

The suite scans the tracked tree for both keys' values and for their key-shaped
literals, so one committed by accident fails CI rather than shipping.

## Caveats worth reading before you spend money on a decision

- **Keyword demand is not category demand.** A staple term reaches a far wider
  population than a niche one; part of any gap is a gap in what the words mean.
- **A search page is a ranked sample, not a census.**
- **Amazon publishes no unit sales.** The `"N+ bought in past month"` badge is a
  bucketed floor on some listings only; `units_coverage_pct` reports how much of
  the sample it covers, and it is never 100%.
- **The candidate files are scaffolding, not advice.** Read their headers. Seed
  from Amazon Product Opportunity Explorer instead if you have a Professional
  seller account — that is real Amazon data answering "what is rising" directly.

## Tests

```bash
python -m pytest -q     # 162 tests
```

Weighted toward failures that are silent rather than loud: sales-badge formats a
narrower regex would read as no-data, the None-vs-zero distinction that keeps
unbadged listings out of the coverage denominator, log-slope refusing to
substitute for `log(0)`, booleans rejected as volumes (`bool` subclasses `int`,
so `float(True) == 1.0` would turn a flag into a search volume of 1), openness
moving the right way — an inverted sign there would rank the most entrenched
category top and look plausible doing it — that the two profiles never collapse
to the same search terms, and that neither profile rejects 100% of candidates,
since a correct gate on unrealistic data is indistinguishable from a broken one.

CI runs lint, the suite, and the dry-run pipeline end to end, so the fixtures
cannot quietly stop exercising the real code paths.
