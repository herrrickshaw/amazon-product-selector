# Amazon demand data sources

**Objective this serves:** find products worth selling, by spotting demand that
is *rising* before the category is saturated. That is a discovery problem with a
time axis, and it is not the same problem as "which of these two products sells
more" — a snapshot ranks what already exists, a trend tells you what to enter.

Sources are grouped by the stage of that workflow they serve. Prices and access
rules are as advertised in August 2026 and move often.

---

## First, three corrections worth making up front

**Personal search history is not available, and you do not want it.** Amazon
exposes no API for an individual user's searches or browsing, and no legitimate
third party has one either. This is not the obstacle it sounds like: for product
research the unit of analysis is *aggregate search volume for a keyword over
time*, which several sources below do provide. A single user's history would be
noise even if you could get it.

**Two commonly-cited APIs are real but solve a different problem:**

- **Alexa Intent Request History API** returns utterances for *your own Alexa
  skill*. It is voice-assistant telemetry for skill developers. It tells you
  nothing about Amazon.com shopping demand.
- **Amazon Ads API "Change History"** tracks edits *you* made to *your own* ad
  campaigns — budget and bid changes over 90 days. It is a campaign audit log,
  not a search-metrics endpoint.

Both appear in AI-generated comparison tables of "Amazon search history APIs".
Neither belongs in a product-research stack.

---

## Stage 1 — Discovery: what is rising?

The stage that actually answers "what should I sell". Everything else validates
a candidate you already have.

| Source | What it gives you | Access |
|---|---|---|
| **Amazon Product Opportunity Explorer** | Amazon's own purpose-built tool for this exact question: niches ranked by search volume, 90-day demand trend, units sold, average price, review moat, and an opportunity score. Sourced from Amazon's real data, not modelled. | Seller Central, Professional seller account. **The strongest option, and free if you already sell.** UI-first; no clean public API. |
| **Brand Analytics → Top Search Terms** | **Search Frequency Rank for all of Amazon**, not just your brand — the top clicked ASINs and click/conversion share per term, weekly. This is the discovery-grade Brand Analytics report. | Brand Registry required. |
| **Helium 10 Magnet / Cerebro** | Keyword search-volume estimates **with history**, plus reverse-ASIN (what terms a competitor ranks for). Magnet expands a seed term into a category map. | Paid; API on top tiers. |
| **MerchantWords** | Specialist in Amazon **keyword history and trends** — multi-year search-volume series per term. Has an API. | Paid. |
| **Google Trends** (`pytrends`) | Free, years of history, catches a rise before it shows up in Amazon tooling. Measures interest, not purchases, and is not Amazon-specific. | Free. |
| **Nexscope, SoldScope, AmazVol, SellerSprite** | Newer keyword/trend APIs; Nexscope also carries BSR, price and monthly-sales time series. | Paid; verify coverage against a keyword you already know before committing. |

Note on **Brand Analytics Search Query Performance (SQP)**: often recommended
for this, but it is scoped to **your own ASINs**. It is excellent for optimising
a product you already sell and structurally useless for discovering one you
don't — a chicken-and-egg you cannot code around. Top Search Terms is the report
that sees the whole marketplace.

## Stage 2 — Validation: is the demand real and growing?

| Source | What it gives you |
|---|---|
| **Keepa** | **BSR and price history** per ASIN, deep and cheap (~€49/mo tier, real API). The best affordable way to turn a snapshot into a trend, and far less lossy than Amazon's bucketed "N+ bought in past month" badge. If you add one paid source, add this. |
| **Jungle Scout / Helium 10 Xray** | BSR→units sales estimates. Directional only, and weakest in exactly the thin niches new sellers target. |
| **Search-result aggregators** — Rainforest (~$66/mo), Canopy, SerpApi, DataForSEO (~$0.60/1k), OpenWeb Ninja, Bright Data, Oxylabs | Parse the live search page: price, rating count, BSR, and the "N+ bought in past month" badge. This is what `amazon_selector/competition.py` consumes. They all read the same page, so switching changes cost and reliability, not what is measurable. |

## Stage 3 — Competition: can you actually win the slot?

Demand alone is a trap. A large category split across thousands of entrenched
listings is a worse entry than a small one with forty. From the aggregator data:

- **`demand_per_listing`** — implemented in `amazon_selector/competition.py`.
- **Review moat** — median review count of page-one listings. A niche where the
  top ten all have 5,000+ reviews is closed to a new entrant regardless of
  search volume.
- **Price dispersion** — wide spread implies room to position; tight clustering
  implies commodity competition on price alone.

## Stage 4 — Once you sell it

Tier 1 replaces every estimate above **for your own ASINs**: SQP gives the true
impressions → clicks → add-to-carts → **purchases** funnel per search term,
programmatic via SP-API since Feb 2025. SP-API sales & traffic reports give your
units and conversion.

---

## Recommended stack

1. **Amazon Product Opportunity Explorer** for discovery — it is Amazon's own
   data, purpose-built for this, and free with a seller account. Start here
   before paying anyone.
2. **Keepa** for the trend on candidates it surfaces.
3. **Google Trends** as a free cross-check that the rise is real and not a
   tooling artefact.
4. **An aggregator** (already integrated) for the competition read.
5. Add **MerchantWords or Helium 10** only if you need keyword history at a
   granularity the above cannot reach.

## Note on running any of this

None of these hosts are reachable from the Claude Code sandbox.
`api.openwebninja.com`, `api.keepa.com`, `api.rainforestapi.com`, `serpapi.com`,
`api.dataforseo.com`, `graphql.canopyapi.co`, `trends.google.com`,
`sellingpartnerapi-na.amazon.com` and `www.amazon.com` were each tested and each
returned a 403 on CONNECT from the egress proxy. The constraint is the sandbox's
allowlist, not any vendor, so changing provider does not make this runnable
there. Run it anywhere with ordinary internet access.
