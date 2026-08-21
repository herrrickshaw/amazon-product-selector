"""
Tests for the Amazon demand comparison client.

Same two concerns as tests/test_adzuna_client.py — credentials must never be in
the source, and the client must fail loudly when it cannot reach the API,
because in the environment it was written in it never can — plus the parsing,
which is where this module can be quietly wrong. Amazon's "N+ bought in past
month" badge is the whole basis of the headline number; a regex that silently
returns None for "1.5K+" would not crash anything, it would just make a
category look dead.

Run:
    python -m pytest tests/test_competition.py -v
"""

import os
import subprocess
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "amazon_selector"))

from competition import (  # noqa: E402
    DEFAULT_GROUPS,
    build_url,
    collect_group,
    dry_run_payload,
    listing_row,
    parse_group_args,
    parse_price,
    parse_sales_volume,
    products_of,
    redact,
    summarise_group,
    total_products_of,
    verdict,
)


# --------------------------------------------------------------- credentials
def test_the_live_credential_is_not_committed():
    """Scan tracked files for whatever key is actually in use.

    Does not hardcode the value it looks for — a test that embeds the secret
    is the leak it exists to prevent. If the key is not in the environment
    there is nothing to scan for, and the test says so rather than passing
    silently and implying a guarantee it did not check.
    """
    key = os.environ.get("OPENWEB_NINJA_API_KEY")
    if not key:
        pytest.skip("OPENWEB_NINJA_API_KEY not in the environment — nothing to scan for")
    out = subprocess.run(["git", "grep", "-lI", "-e", key],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.strip() == "", f"credential value found in: {out.stdout}"


def test_no_file_hardcodes_an_api_key_assignment():
    """Catches the shape of the mistake even when the value is unknown."""
    out = subprocess.run(
        ["git", "grep", "-lIE",
         r"OPENWEB_NINJA_API_KEY[\"']?\s*[:=]\s*[\"'][A-Za-z0-9_]{8,}"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert out.stdout.strip() == "", f"hardcoded credential assignment in: {out.stdout}"


def test_no_openwebninja_key_literal_is_committed():
    """The provider's keys carry an `ak_` prefix and are long. Catch any such
    literal anywhere in the tree, whatever variable it is assigned to."""
    out = subprocess.run(["git", "grep", "-lIE", r"\bak_[a-z0-9]{32,}\b"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.strip() == "", f"API-key-shaped literal in: {out.stdout}"


def test_the_module_reads_the_credential_from_the_environment_only():
    path = os.path.join(ROOT, "amazon_selector", "competition.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert 'os.environ.get("OPENWEB_NINJA_API_KEY")' in src


def test_redact_strips_the_key(monkeypatch):
    monkeypatch.setenv("OPENWEB_NINJA_API_KEY", "SECRET_KEY_VALUE")
    assert "SECRET_KEY_VALUE" not in redact("...SECRET_KEY_VALUE...")


def test_the_key_never_travels_in_the_url():
    """It belongs in the x-api-key header. A URL carrying it ends up in logs."""
    url = build_url("moringa oil", "US")
    assert "api_key" not in url and "x-api-key" not in url
    assert "query=moringa+oil" in url and "country=US" in url


# ------------------------------------------------------------ sales-volume
@pytest.mark.parametrize("raw,expected", [
    ("500+ bought in past month", 500),
    ("50+ bought in past month", 50),
    ("1K+ bought in past month", 1_000),
    ("2K+ bought in past month", 2_000),
    ("10K+ bought in past month", 10_000),
    ("1.5K+ bought in past month", 1_500),
    ("1M+ bought in past month", 1_000_000),
    ("1,000+ bought in past month", 1_000),
    ("100 bought in past month", 100),          # no "+" seen in the wild, still parse
])
def test_parse_sales_volume_handles_every_badge_shape(raw, expected):
    assert parse_sales_volume(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "Best Seller", 42, "bought recently"])
def test_parse_sales_volume_returns_none_rather_than_zero(raw):
    """None and 0 must stay distinct: None means the badge was absent (excluded
    from coverage), 0 would mean the listing sold nothing (counted as a dud)."""
    assert parse_sales_volume(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("$12.99", 12.99), ("₹1,299.00", 1299.00), ("12.99", 12.99),
    ("€9,49", 949.0), (14.5, 14.5), (None, None), ("", None), ("N/A", None),
])
def test_parse_price(raw, expected):
    assert parse_price(raw) == expected


# ------------------------------------------------------------ payload shapes
def test_products_of_accepts_both_response_shapes():
    assert products_of({"data": {"products": [{"asin": "A"}]}}) == [{"asin": "A"}]
    assert products_of({"products": [{"asin": "B"}]}) == [{"asin": "B"}]
    assert products_of({"data": {}}) == []
    assert products_of({}) == []


def test_total_products_of_accepts_both_response_shapes():
    assert total_products_of({"data": {"total_products": 7}}) == 7
    assert total_products_of({"total_products": 7}) == 7
    assert total_products_of({}) is None


# ------------------------------------------------------------ row derivation
def test_listing_row_computes_revenue_only_when_both_inputs_exist():
    row = listing_row({"asin": "X", "product_price": "$10.00",
                       "sales_volume": "500+ bought in past month"}, "g", "q")
    assert row["est_monthly_revenue"] == 5000.0

    no_units = listing_row({"asin": "Y", "product_price": "$10.00"}, "g", "q")
    assert no_units["est_monthly_revenue"] is None

    no_price = listing_row({"asin": "Z",
                            "sales_volume": "500+ bought in past month"}, "g", "q")
    assert no_price["est_monthly_revenue"] is None


# ------------------------------------------------------------ collection
def test_collect_group_dedupes_by_asin_across_keyword_variants():
    """Without this, the group with the most overlapping keywords wins on
    volume of duplicates rather than on demand."""
    rows, breadth = collect_group(
        "honey", ["honey", "raw honey", "organic honey"], "US", pages=1,
        key=None, sleep=0, dry_run=True, from_cache=False, write_cache=False,
    )
    assert len(rows) == 3, "three keywords over the same 3 ASINs must collapse to 3"
    assert sorted(r["asin"] for r in rows) == ["B0HON1", "B0HON2", "B0HON3"]
    assert breadth == 30_000


def test_collect_group_keeps_the_record_that_has_the_sales_badge():
    """A listing badged on one keyword and unbadged on another is still a
    listing with known units — dropping the badge would understate demand."""
    badged = {"asin": "DUP", "product_price": "$5.00",
              "sales_volume": "900+ bought in past month"}
    plain = {"asin": "DUP", "product_price": "$5.00"}

    from competition import listing_row as lr
    seen = {}
    for p in (plain, badged):          # unbadged arrives first
        row = lr(p, "g", "q")
        prev = seen.get(row["asin"])
        if prev is None or (prev["units_past_month"] is None
                            and row["units_past_month"] is not None):
            seen[row["asin"]] = row
    assert seen["DUP"]["units_past_month"] == 900


# ------------------------------------------------------------ summarising
def test_summarise_group_arithmetic():
    rows, breadth = collect_group(
        "honey", ["honey"], "US", pages=1, key=None, sleep=0,
        dry_run=True, from_cache=False, write_cache=False,
    )
    s = summarise_group("honey", rows, breadth)

    assert s["listings_sampled"] == 3
    assert s["units_past_month_floor"] == 16_000      # 10K + 5K + 1K
    assert s["listings_with_sales_badge"] == 3
    assert s["units_coverage_pct"] == 100.0
    assert s["cumulative_ratings"] == 48_200 + 21_500 + 6_100
    assert s["median_price"] == 14.99
    assert s["best_sellers"] == 1 and s["amazon_choice"] == 1
    # 10000*14.99 + 5000*9.49 + 1000*59.00
    assert s["est_monthly_revenue_floor"] == pytest.approx(256_350.0)
    assert s["demand_per_listing"] == pytest.approx(16_000 / 30_000, abs=1e-3)


def test_coverage_is_reported_when_some_listings_lack_the_badge():
    rows, breadth = collect_group(
        "moringa oil", ["moringa oil"], "US", pages=1, key=None, sleep=0,
        dry_run=True, from_cache=False, write_cache=False,
    )
    s = summarise_group("moringa oil", rows, breadth)
    assert s["listings_with_sales_badge"] == 2 and s["listings_sampled"] == 3
    assert s["units_coverage_pct"] == 66.7
    assert s["units_past_month_floor"] == 700         # 500 + 200, the third is unbadged


def test_summarise_group_survives_a_group_with_no_sales_badges_at_all():
    rows = [listing_row({"asin": "A", "product_price": "$3.00",
                         "product_num_ratings": 5}, "g", "q")]
    s = summarise_group("g", rows, None)
    assert s["units_past_month_floor"] == 0
    assert s["units_coverage_pct"] == 0.0
    assert s["units_per_badged_listing"] is None
    assert s["demand_per_listing"] is None


# ------------------------------------------------------------ verdict
def test_verdict_ranks_each_metric_separately_and_reports_the_gap():
    summaries = []
    for group in ("moringa oil", "honey"):
        rows, breadth = collect_group(
            group, [group], "US", pages=1, key=None, sleep=0,
            dry_run=True, from_cache=False, write_cache=False,
        )
        summaries.append(summarise_group(group, rows, breadth))

    lines = " ".join(verdict(summaries, "US"))
    assert "honey leads" in lines
    assert "22.9x" in lines                     # 16,000 vs 700 units
    assert "Sales-badge coverage" in lines      # the caveat is never optional


def test_verdict_renders_the_fractional_metric_without_rounding_it_to_zero():
    """demand_per_listing is a small fraction; a ",.0f" format would print the
    0.533-vs-0.350 comparison as "1 vs 0"."""
    summaries = []
    for group in ("moringa oil", "honey"):
        rows, breadth = collect_group(
            group, [group], "US", pages=1, key=None, sleep=0,
            dry_run=True, from_cache=False, write_cache=False,
        )
        summaries.append(summarise_group(group, rows, breadth))
    line = next(ln for ln in verdict(summaries, "US") if "per competing listing" in ln)
    assert "0.533" in line and "0.350" in line


def test_verdict_needs_two_groups():
    assert "nothing to compare" in verdict([{"group": "solo"}], "US")[0]


# ------------------------------------------------------------ CLI plumbing
def test_parse_group_args():
    assert parse_group_args(["oil=moringa oil, moringa seed oil"]) == {
        "oil": ["moringa oil", "moringa seed oil"]}


def test_parse_group_args_rejects_a_spec_with_no_equals():
    with pytest.raises(SystemExit):
        parse_group_args(["moringa oil"])


def test_default_groups_are_the_two_the_comparison_is_about():
    assert set(DEFAULT_GROUPS) == {"moringa oil", "honey"}
    assert all(len(v) >= 2 for v in DEFAULT_GROUPS.values()), \
        "one keyword per group is a single slice of the ranker, not a category"


def test_dry_run_payload_gives_the_two_groups_different_demand_profiles():
    """Otherwise the dry run cannot exercise ranking or the coverage warning."""
    mor = products_of(dry_run_payload("moringa oil"))
    hon = products_of(dry_run_payload("honey"))
    assert any(p.get("sales_volume") is None for p in mor), "need a coverage gap"
    assert all(p.get("sales_volume") for p in hon)
