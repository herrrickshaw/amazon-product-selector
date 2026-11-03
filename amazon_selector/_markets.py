"""Per-marketplace gate scaling for shortlist.py (re-exported there)."""
from __future__ import annotations

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
