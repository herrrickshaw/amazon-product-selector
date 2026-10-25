"""competition_metrics: review moat, price dispersion and top-3 concentration,
checked against hand-computed values. This is the 'can a slot be taken' half
of the ranking and had no direct tests."""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "amazon_selector"))

from competition import competition_metrics, summarise_group  # noqa: E402


def _row(ratings, price, units):
    return {"num_ratings": ratings, "price": price, "units_past_month": units,
            "est_monthly_revenue": (units * price) if units and price else None,
            "star_rating": 4.0, "is_best_seller": False, "is_amazon_choice": False}


def test_review_moat_is_median_of_top_n_leaders_not_whole_page():
    # 3 entrenched leaders + 7 dead listings. Whole-page median would be 3.
    rows = [_row(r, 10.0, 0) for r in (5000, 4000, 3000, 3, 3, 3, 3, 3, 3, 3)]
    assert competition_metrics(rows, top_n=3)["review_moat"] == 4000
    assert competition_metrics(rows, top_n=10)["review_moat"] == 3


def test_top3_unit_share_hand_computed():
    rows = [_row(1, 10.0, u) for u in (500, 300, 100, 50, 50)]
    # (500+300+100) / 1000
    assert competition_metrics(rows)["top3_unit_share"] == pytest.approx(0.9)


def test_top3_share_is_none_without_any_sales_badges():
    rows = [_row(1, 10.0, None) for _ in range(5)]
    assert competition_metrics(rows)["top3_unit_share"] is None


def test_price_dispersion_needs_four_prices():
    rows = [_row(1, p, 0) for p in (10.0, 20.0, 30.0)]
    assert competition_metrics(rows)["price_dispersion"] is None


def test_price_dispersion_is_scale_free():
    a = [_row(1, p, 0) for p in (10, 12, 14, 16, 18, 20, 22, 24)]
    b = [_row(1, p * 50, 0) for p in (10, 12, 14, 16, 18, 20, 22, 24)]
    assert competition_metrics(a)["price_dispersion"] == competition_metrics(b)["price_dispersion"]
    # sorted prices, q1 = p[2] = 14, q3 = p[6] = 22, median = 17 -> 8/17
    assert competition_metrics(a)["price_dispersion"] == pytest.approx(round(8 / 17, 3))


def test_empty_page_gives_all_none():
    assert competition_metrics([]) == {"review_moat": None, "price_dispersion": None, "top3_unit_share": None}


def test_summarise_group_demand_per_listing_uses_breadth():
    rows = [_row(10, 20.0, 1000), _row(20, 30.0, 2000), _row(30, None, None)]
    s = summarise_group("g", rows, breadth=600)
    assert s["units_past_month_floor"] == 3000
    assert s["demand_per_listing"] == 5.0
    assert s["units_coverage_pct"] == pytest.approx(66.7)
    assert s["est_monthly_revenue_floor"] == 20_000 + 60_000
    assert s["median_price"] == 25.0
    assert s["cumulative_ratings"] == 60
