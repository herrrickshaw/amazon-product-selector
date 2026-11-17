"""Fixture payloads for ``competition.py --dry-run`` (no network, no key).

Split out of competition.py so the module that talks to the API is not also
the home of 100+ lines of literal test data. ``competition`` re-exports
``dry_run_payload``, so the old import path keeps working.
"""
from __future__ import annotations

import hashlib

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


# Named fixtures, matched by group-name prefix. Each is (products, total).
_MORINGA = [
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

_TURMERIC = [
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

_SHILAJIT = [
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

_HONEY = [
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

_NAMED_FIXTURES = (
    ("moringa", _MORINGA, 2_000),
    # Open category: low review moat, wide price spread, many listings.
    ("turmeric", _TURMERIC, 900),
    # Closed category: entrenched incumbents, tight pricing.
    ("shilajit", _SHILAJIT, 4200),
    ("honey", _HONEY, 30_000),
)


def _hashed_fixture(group: str, country: str) -> tuple[list[dict], int]:
    """Unnamed keyword: a stable, varied competition profile. Constant
    fixtures here would give every candidate in a large dry run the same
    review moat and the same price, collapsing the openness and headroom
    components to 0.5 across the board — a ranking with nothing to rank."""
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
    return products, 500 + (h // 3) % 40000


def dry_run_payload(group: str, country: str = "US") -> dict:
    """Shaped like a real response, with deliberately different demand
    profiles so the ranking, the ratio and the coverage warning all fire."""
    for prefix, products, total in _NAMED_FIXTURES:
        if group.startswith(prefix):
            break
    else:
        products, total = _hashed_fixture(group, country)
    return {"status": "OK", "data": {"country": "US", "total_products": total,
                                     "products": products}}
