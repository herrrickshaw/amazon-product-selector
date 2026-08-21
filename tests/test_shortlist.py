"""
Tests for the shortlist pipeline.

The pipeline's whole value is that it REJECTS things, so most of these are
about rejection: that each gate fires for its own reason, that the reason is
recorded rather than the candidate silently vanishing, and that a candidate
disqualified on the trend never costs a competition API call.

Run:
    python -m pytest tests/test_shortlist.py -v
"""

import argparse
import math
import os
import sys
import time
from datetime import date, timedelta

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "amazon_selector"))

import competition as comp  # noqa: E402
import trends as trend  # noqa: E402
from shortlist import (  # noqa: E402
    DEFAULT_ALLOWED_FLAGS,
    DEFAULT_COUNTRY,
    MARKETS,
    PROFILES,
    WEIGHTS,
    _minmax,
    build_candidate,
    collect,
    expand,
    gate,
    market_for,
    profile_queries,
    resolve_gates,
    score_candidates,
)


def _args(**over):
    base = dict(country="US", pages=1, window=4, weeks=52, profile_name="b2c",
                allowed_flags=list(DEFAULT_ALLOWED_FLAGS), price_all=False,
                endpoint="https://example.test/x", auth_header="Authorization",
                auth_prefix="Bearer ", sleep=0, dry_run=True, from_cache=False,
                no_cache=True, cache_max_age_hours=24.0,
                trend_cache_max_age_hours=168.0)
    base.update(over)
    return argparse.Namespace(**base)


# ------------------------------------------------------------------ weights
def test_weights_sum_to_one():
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_growth_carries_the_most_weight():
    """The tool exists to find demand before saturation; if any other term
    outweighed growth the ranking would be answering a different question."""
    assert max(WEIGHTS, key=WEIGHTS.get) == "growth"


# -------------------------------------------------------------------- gates
def test_a_clean_candidate_passes_every_gate():
    cand = {"opportunity_flag": "RISING", "observations": 60,
            "latest_volume": 5000, "review_moat": 400, "listings_sampled": 5}
    assert gate(cand, allowed_flags=("RISING",), min_volume=500,
                min_observations=12, max_review_moat=2000) == []


@pytest.mark.parametrize("override,fragment", [
    ({"opportunity_flag": "flat"}, "not in"),
    ({"opportunity_flag": "spiky — growth may be noise"}, "not in"),
    ({"observations": 4}, "observations"),
    ({"latest_volume": 50}, "below"),
    ({"review_moat": 40000}, "review moat"),
    ({"listings_sampled": 0}, "no competition data"),
])
def test_each_gate_fires_for_its_own_reason(override, fragment):
    cand = {"opportunity_flag": "RISING", "observations": 60,
            "latest_volume": 5000, "review_moat": 400, "listings_sampled": 5}
    cand.update(override)
    fails = gate(cand, allowed_flags=("RISING",), min_volume=500,
                 min_observations=12, max_review_moat=2000)
    assert any(fragment in f for f in fails), fails


def test_multiple_failures_are_all_reported_not_just_the_first():
    """A candidate that is both unsearched and entrenched should say so —
    fixing one problem would not make it viable."""
    cand = {"opportunity_flag": "flat", "observations": 2, "latest_volume": 1,
            "review_moat": 90000, "listings_sampled": 5}
    fails = gate(cand, allowed_flags=("RISING",), min_volume=500,
                 min_observations=12, max_review_moat=2000)
    assert len(fails) >= 4


def test_a_missing_review_moat_does_not_silently_disqualify():
    """None means 'not measured', not 'infinitely entrenched'."""
    cand = {"opportunity_flag": "RISING", "observations": 60,
            "latest_volume": 5000, "review_moat": None, "listings_sampled": 5}
    assert gate(cand, allowed_flags=("RISING",), min_volume=500,
                min_observations=12, max_review_moat=2000) == []


# ----------------------------------------------------------------- min-max
def test_minmax_scales_to_the_unit_interval():
    assert _minmax([0.0, 5.0, 10.0]) == [0.0, 0.5, 1.0]


def test_a_constant_column_maps_to_one_half_not_to_an_extreme():
    """With no spread there is no evidence either way. Collapsing to 0 or 1
    would hand that component's entire weight to everyone or to no one."""
    assert _minmax([7.0, 7.0, 7.0]) == [0.5, 0.5, 0.5]


def test_minmax_treats_none_as_no_evidence():
    assert _minmax([None, None]) == [0.5, 0.5]
    assert _minmax([0.0, None, 10.0]) == [0.0, 0.5, 1.0]


# ----------------------------------------------------------------- scoring
def _cand(kw, growth, volume, moat, dpl, disp):
    return {"keyword": kw, "growth_per_period_pct": growth, "latest_volume": volume,
            "review_moat": moat, "demand_per_listing": dpl, "price_dispersion": disp}


def test_the_better_candidate_on_every_component_ranks_first():
    ranked = score_candidates([
        _cand("weak", 1.0, 1000, 9000, 0.1, 0.1),
        _cand("strong", 8.0, 40000, 200, 2.0, 1.5),
    ])
    assert ranked[0]["keyword"] == "strong" and ranked[0]["rank"] == 1
    assert ranked[1]["rank"] == 2


def test_every_component_is_reported_alongside_the_total():
    """A score you cannot decompose is a score you cannot argue with."""
    ranked = score_candidates([_cand("a", 1.0, 1000, 100, 0.5, 0.2),
                               _cand("b", 2.0, 2000, 200, 0.6, 0.3)])
    for key in ("score_growth", "score_demand", "score_openness", "score_headroom"):
        assert key in ranked[0]


def test_a_lower_review_moat_scores_more_open():
    """Openness must move the right way — an inverted sign here would rank the
    most entrenched category top and look entirely plausible doing it."""
    ranked = score_candidates([_cand("open", 5.0, 10000, 100, 1.0, 0.5),
                               _cand("closed", 5.0, 10000, 90000, 1.0, 0.5)])
    by_kw = {r["keyword"]: r for r in ranked}
    assert by_kw["open"]["score_openness"] > by_kw["closed"]["score_openness"]


def test_demand_is_scored_on_a_log_scale():
    """Linear scaling would let one huge term flatten every other candidate to
    ~0; 500 vs 5,000 must separate more than 50,000 vs 54,500."""
    ranked = score_candidates([_cand("tiny", 5.0, 500, 100, 1.0, 0.5),
                               _cand("small", 5.0, 5000, 100, 1.0, 0.5),
                               _cand("huge", 5.0, 500000, 100, 1.0, 0.5)])
    by_kw = {r["keyword"]: r["score_demand"] for r in ranked}
    assert by_kw["small"] > 0.25, "log scaling must keep mid candidates off the floor"


def test_the_score_is_relative_to_the_candidate_set():
    """Documented caveat, pinned as behaviour: adding a candidate changes the
    others' scores, so a score means 'best of what you supplied' and must never
    be compared across runs."""
    pair = [_cand("a", 1.0, 1000, 100, 0.5, 0.2), _cand("b", 2.0, 2000, 200, 0.6, 0.3)]
    alone = {r["keyword"]: r["score"] for r in score_candidates(list(pair))}
    with_third = {r["keyword"]: r["score"]
                  for r in score_candidates(pair + [_cand("c", 99.0, 999999, 10, 9.0, 9.0)])}
    assert alone["b"] != with_third["b"]


def test_scoring_an_empty_list_is_not_an_error():
    assert score_candidates([]) == []


# ------------------------------------------------------- credit conservation
def test_a_trend_rejected_keyword_is_never_priced_for_competition():
    """The competition call bills per listing returned. Paying it for a keyword
    already disqualified as seasonal is the most expensive way to learn
    nothing."""
    row = collect("honey", _args(), None, None, PROFILES["b2c"])          # fixture: flat
    assert row["priced_competition"] is False
    assert "gate_failures" in row


def test_a_trend_passing_keyword_is_priced():
    row = collect("moringa oil", _args(), None, None, PROFILES["b2c"])    # fixture: RISING
    assert row["priced_competition"] is True
    assert row["listings_sampled"] > 0


def test_price_all_overrides_the_pre_gate():
    row = collect("honey", _args(price_all=True), None, None, PROFILES["b2c"])
    assert row["priced_competition"] is True


# --------------------------------------------------------------- end to end
def _run(keywords, profile_name="b2c", **over):
    profile = PROFILES[profile_name]
    g = profile["gates"]
    cands = [collect(k, _args(profile_name=profile_name, **over), None, None, profile)
             for k in keywords]
    passed, rejected = [], []
    for c in cands:
        fails = c.get("gate_failures")
        fails = fails.split("; ") if fails else gate(
            c, allowed_flags=("RISING",), min_volume=g["min_volume"],
            min_observations=g["min_observations"],
            max_review_moat=g["max_review_moat"],
            min_median_price=g["min_median_price"])
        (rejected if fails else passed).append(c)
        if fails:
            c["gate_failures"] = "; ".join(fails)
    return score_candidates(passed, profile["weights"]), rejected


def test_the_pipeline_shortlists_the_open_riser_over_the_tighter_one():
    shortlist, _ = _run(["moringa oil", "turmeric soap"])
    assert [r["keyword"] for r in shortlist] == ["turmeric soap", "moringa oil"]


def test_a_riser_in_an_entrenched_category_is_rejected_on_the_moat_not_the_trend():
    """The case the whole gate design exists for: demand is genuinely rising,
    and the category is still not enterable."""
    shortlist, rejected = _run(["shilajit resin"])
    assert shortlist == []
    reason = rejected[0]["gate_failures"]
    assert "review moat" in reason
    assert "trend" not in reason, "it passed the trend gate — the moat is what killed it"


def test_each_way_of_looking_rising_without_being_rising_is_rejected_separately():
    _, rejected = _run(["honey", "mouth tape", "sleep gummies"])
    reasons = {r["keyword"]: r["gate_failures"] for r in rejected}
    assert "flat" in reasons["honey"]
    assert "seasonal" in reasons["mouth tape"]
    assert "spiky" in reasons["sleep gummies"]


def test_build_candidate_keeps_the_keyword_once():
    row = build_candidate("kw", {"keyword": "kw", "latest_volume": 5}, None, None)
    assert row["keyword"] == "kw" and row["latest_volume"] == 5


# ----------------------------------------------- trend-fit regression guard
def test_a_steep_clean_riser_is_not_mistaken_for_noise():
    """Regression test. The spiky check originally keyed on raw CV, which a
    cleanly compounding series inflates BY CONSTRUCTION — a term growing
    4.5%/week for a year triples, and its raw CV exceeds the noise threshold.
    That flagged the best candidates as noise. The check now keys on variance
    the trend does NOT explain."""
    steep = [4000 * (1.045 ** i) for i in range(60)]
    assert trend.trend_fit_r2(steep) > 0.99, "a clean exponential must fit near-perfectly"
    s = trend.summarise_keyword("steep riser",
                                trend.parse_series(trend.dry_run_payload("turmeric soap"),
                                                   "turmeric soap"))
    s["opportunity_flag"] = trend.opportunity_flag(s)
    assert s["volatility_cv"] > 0.6, "raw spread is high — this is the trap"
    assert s["opportunity_flag"] == "RISING", "but the trend explains it, so it is not noise"


def test_a_series_that_swings_without_going_anywhere_fits_badly():
    noisy = [1000, 9000, 1500, 12000, 800, 7000, 2000, 15000] * 4
    assert trend.trend_fit_r2(noisy) < 0.3


def test_trend_fit_r2_is_scale_and_steepness_invariant():
    gentle = [100 * (1.005 ** i) for i in range(40)]
    steep = [100 * (1.20 ** i) for i in range(40)]
    assert trend.trend_fit_r2(gentle) == pytest.approx(trend.trend_fit_r2(steep), abs=1e-6)


def test_trend_fit_r2_is_one_for_a_flat_series():
    assert trend.trend_fit_r2([500.0] * 20) == pytest.approx(1.0)


def test_trend_fit_r2_needs_enough_points():
    assert trend.trend_fit_r2([100.0, 110.0]) is None


def test_r2_and_slope_agree_on_a_synthetic_series():
    vals = [1000 * (1.03 ** i) for i in range(30)]
    assert math.exp(trend.log_slope(vals)) - 1 == pytest.approx(0.03, abs=1e-6)
    assert trend.trend_fit_r2(vals) == pytest.approx(1.0, abs=1e-6)


# ------------------------------------------------------------------ profiles
def test_every_profile_weights_sum_to_one():
    for name, p in PROFILES.items():
        assert sum(p["weights"].values()) == pytest.approx(1.0), name


def test_growth_leads_in_both_profiles():
    """B2C and B2B differ on a lot, but not on the question being asked."""
    for name, p in PROFILES.items():
        assert max(p["weights"], key=p["weights"].get) == "growth", name


def test_b2b_searches_bulk_terms_and_b2c_the_bare_one():
    """The profiles are separate analyses because they do not share a search
    series: a consumer searches 'moringa oil', a buyer searches 'bulk moringa
    oil'. If these ever collapsed to the same terms the split would be
    cosmetic."""
    assert expand(PROFILES["b2c"]["trend_term"], "moringa oil") == "moringa oil"
    assert expand(PROFILES["b2b"]["trend_term"], "moringa oil") == "bulk moringa oil"
    b2b_queries = profile_queries(PROFILES["b2b"], "moringa oil")
    assert len(b2b_queries) > 1
    assert all(q != "moringa oil" for q in b2b_queries)


def test_b2b_tolerates_far_lower_search_volume():
    """Not a laxer standard — bulk terms carry a fraction of consumer volume,
    so the B2C floor would reject every viable B2B niche."""
    assert PROFILES["b2b"]["gates"]["min_volume"] < PROFILES["b2c"]["gates"]["min_volume"]


def test_b2b_tolerates_a_much_larger_review_moat():
    """Procurement is spec- and price-driven; review count is a weaker barrier
    than it is for a consumer. Looser, but still gated — a dominant incumbent
    is still a problem."""
    assert (PROFILES["b2b"]["gates"]["max_review_moat"]
            > PROFILES["b2c"]["gates"]["max_review_moat"])
    assert PROFILES["b2b"]["gates"]["max_review_moat"] is not None


def test_only_b2b_enforces_a_price_floor():
    """B2B earns on order value, not unit count."""
    assert PROFILES["b2b"]["gates"]["min_median_price"] is not None
    assert PROFILES["b2c"]["gates"]["min_median_price"] is None


def test_b2b_weights_headroom_higher_and_raw_demand_lower_than_b2c():
    b2c, b2b = PROFILES["b2c"]["weights"], PROFILES["b2b"]["weights"]
    assert b2b["headroom"] > b2c["headroom"]
    assert b2b["demand"] < b2c["demand"]


def test_the_price_floor_gate_fires_and_says_so():
    cand = {"opportunity_flag": "RISING", "observations": 60, "latest_volume": 5000,
            "review_moat": 400, "listings_sampled": 5, "median_price": 9.99}
    fails = gate(cand, allowed_flags=("RISING",), min_volume=50, min_observations=12,
                 max_review_moat=25000, min_median_price=25.0)
    assert any("median price" in f for f in fails)


def test_a_missing_price_does_not_silently_disqualify():
    """None means 'not measured', not 'free'."""
    cand = {"opportunity_flag": "RISING", "observations": 60, "latest_volume": 5000,
            "review_moat": 400, "listings_sampled": 5, "median_price": None}
    assert gate(cand, allowed_flags=("RISING",), min_volume=50, min_observations=12,
                max_review_moat=25000, min_median_price=25.0) == []


def test_the_same_product_can_be_closed_at_retail_and_open_in_bulk():
    """The reason two analyses beat one. shilajit resin carries a ~21,750
    review moat: disqualifying for a consumer listing, tolerable for a
    procurement one, and its price clears the B2B order-value floor."""
    b2c_short, b2c_rej = _run(["shilajit resin"], profile_name="b2c")
    b2b_short, _ = _run(["shilajit resin"], profile_name="b2b")
    assert b2c_short == [], "entrenched at retail"
    assert "review moat" in b2c_rej[0]["gate_failures"]
    assert [r["keyword"] for r in b2b_short] == ["shilajit resin"], "open in bulk"


def test_a_cheap_consumer_winner_fails_the_b2b_order_value_floor():
    """And the reverse: selling a $15 soap by the box is not a B2B business."""
    b2c_short, _ = _run(["turmeric soap"], profile_name="b2c")
    b2b_short, b2b_rej = _run(["turmeric soap"], profile_name="b2b")
    assert [r["keyword"] for r in b2c_short] == ["turmeric soap"]
    assert b2b_short == []
    assert "median price" in b2b_rej[0]["gate_failures"]


def test_each_candidate_records_which_profile_and_term_produced_it():
    """Two reports with identical column names are easy to mix up; every row
    carries its own provenance."""
    row = collect("moringa oil", _args(profile_name="b2b"), None, None, PROFILES["b2b"])
    assert row["profile"] == "b2b"
    assert row["trend_term"] == "bulk moringa oil"


# --------------------------------------------------- history window is sent
def test_the_history_window_is_actually_requested(monkeypatch):
    """Regression test. --weeks was accepted and silently dropped, so the API
    returned its own default window. If that default is under 52 weeks,
    year_over_year() has nothing to compare against and returns None for every
    candidate — the seasonality gate then never fires and trough-bounce
    categories pass as RISING. A short window must not look like an absent
    season."""
    seen = {}

    def fake_fetch(keyword, country, key, endpoint, auth_header, auth_prefix,
                   start=None, end=None, **kw):
        seen["start"] = start
        return trend.dry_run_payload(keyword)

    monkeypatch.setattr(trend, "fetch", fake_fetch)
    collect("moringa oil", _args(dry_run=False, weeks=52), "k", "k", PROFILES["b2c"])
    assert seen["start"] is not None, "start_date must be sent, not left to the API default"

    expected = (date.today() - timedelta(weeks=52)).isoformat()
    assert seen["start"] == expected


def test_a_longer_window_is_passed_through(monkeypatch):
    seen = {}

    def fake_fetch(keyword, country, key, endpoint, auth_header, auth_prefix,
                   start=None, end=None, **kw):
        seen["start"] = start
        return trend.dry_run_payload(keyword)

    monkeypatch.setattr(trend, "fetch", fake_fetch)
    collect("moringa oil", _args(dry_run=False, weeks=104), "k", "k", PROFILES["b2c"])
    assert seen["start"] == (date.today() - timedelta(weeks=104)).isoformat()


# --------------------------------------------------------------- cache expiry
def _age_file(path, hours):
    """Backdate a file's mtime so freshness can be tested without waiting."""
    old = time.time() - hours * 3600
    os.utime(path, (old, old))


def test_a_fresh_cache_is_used(tmp_path):
    f = tmp_path / "c.json"
    f.write_text("{}")
    assert comp.cache_is_fresh(f, 24) is True
    assert trend.cache_is_fresh(f, 168) is True


def test_a_stale_cache_is_not_considered_fresh(tmp_path):
    """The defect this fixes: without an age check a month-old snapshot is
    served as current and nothing downstream can tell."""
    f = tmp_path / "c.json"
    f.write_text("{}")
    _age_file(f, 30 * 24)
    assert comp.cache_is_fresh(f, 24) is False
    assert trend.cache_is_fresh(f, 168) is False


def test_a_missing_cache_is_never_fresh(tmp_path):
    assert comp.cache_is_fresh(tmp_path / "nope.json", 24) is False


def test_zero_max_age_disables_expiry(tmp_path):
    """An explicit opt-out, for reproducing an old run deliberately."""
    f = tmp_path / "c.json"
    f.write_text("{}")
    _age_file(f, 365 * 24)
    assert comp.cache_is_fresh(f, 0) is True


def test_the_boundary_is_inclusive(tmp_path):
    f = tmp_path / "c.json"
    f.write_text("{}")
    _age_file(f, 23.9)
    assert comp.cache_is_fresh(f, 24) is True
    _age_file(f, 24.1)
    assert comp.cache_is_fresh(f, 24) is False


def test_trend_data_is_cached_far_longer_than_competition_data():
    """Not arbitrary: a weekly series gains one point a week, so refetching it
    daily only spends quota. Prices and rankings move daily, so a day-old
    competition snapshot is already at the edge of useful."""
    assert trend.DEFAULT_CACHE_MAX_AGE_HOURS > comp.DEFAULT_CACHE_MAX_AGE_HOURS
    assert comp.DEFAULT_CACHE_MAX_AGE_HOURS == 24
    assert trend.DEFAULT_CACHE_MAX_AGE_HOURS == 168


def test_cache_age_reports_hours(tmp_path):
    f = tmp_path / "c.json"
    f.write_text("{}")
    _age_file(f, 5)
    assert comp.cache_age_hours(f) == pytest.approx(5.0, abs=0.1)
    assert comp.cache_age_hours(tmp_path / "nope.json") is None


def test_the_shortlist_writes_and_then_reuses_a_trend_cache(tmp_path, monkeypatch):
    """Regression test. The shortlist path never cached trend responses at all,
    so --from-cache silently did nothing for the trend half and every run
    re-spent one request per keyword per profile."""
    monkeypatch.setattr(trend, "CACHE_DIR", tmp_path)
    calls = []

    def fake_fetch(keyword, country, key, endpoint, auth_header, auth_prefix,
                   start=None, end=None, **kw):
        calls.append(keyword)
        return trend.dry_run_payload(keyword)

    monkeypatch.setattr(trend, "fetch", fake_fetch)
    args = _args(dry_run=False, no_cache=False)
    collect("moringa oil", args, "k", "k", PROFILES["b2c"])
    assert len(calls) == 1, "first run must fetch"

    collect("moringa oil", args, "k", "k", PROFILES["b2c"])
    assert len(calls) == 1, "second run must reuse the cache, not refetch"


def test_a_stale_trend_cache_is_refetched_rather_than_served(tmp_path, monkeypatch):
    monkeypatch.setattr(trend, "CACHE_DIR", tmp_path)
    calls = []

    def fake_fetch(keyword, country, key, endpoint, auth_header, auth_prefix,
                   start=None, end=None, **kw):
        calls.append(keyword)
        return trend.dry_run_payload(keyword)

    monkeypatch.setattr(trend, "fetch", fake_fetch)
    args = _args(dry_run=False, no_cache=False)
    collect("moringa oil", args, "k", "k", PROFILES["b2c"])
    _age_file(trend.cache_path("moringa oil", "US"), 30 * 24)
    collect("moringa oil", args, "k", "k", PROFILES["b2c"])
    assert len(calls) == 2, "a month-old series must be refetched, not served as current"


# ------------------------------------------------------------------ markets
def test_the_b2b_price_floor_is_denominated_in_the_local_currency():
    """The bug this guards. 25 is a sensible USD floor and a meaningless INR
    one — Rs 25 is about $0.30, so a currency-naive floor on amazon.in passes
    every product and the gate silently stops existing."""
    us = resolve_gates(PROFILES["b2b"], market_for("US"))["min_median_price"]
    inr = resolve_gates(PROFILES["b2b"], market_for("IN"))["min_median_price"]
    assert us == 25.0
    assert inr >= 1000, f"an INR floor of {inr} would pass everything"


def test_every_known_market_sets_a_b2b_floor_above_a_trivial_amount():
    for code, m in MARKETS.items():
        g = resolve_gates(PROFILES["b2b"], m)
        assert g["min_median_price"] == m["b2b_min_price"], code
        assert g["min_median_price"] > 0, code


def test_b2c_never_gains_a_price_floor_from_the_market():
    """The floor is a statement about the B2B channel, not about the country."""
    for m in MARKETS.values():
        assert resolve_gates(PROFILES["b2c"], m)["min_median_price"] is None


def test_volume_gates_scale_down_for_smaller_marketplaces():
    """Amazon India's absolute search counts are far below the US's for the
    same category; an unscaled US floor rejects viable Indian niches for being
    Indian."""
    us = resolve_gates(PROFILES["b2c"], market_for("US"))["min_volume"]
    inr = resolve_gates(PROFILES["b2c"], market_for("IN"))["min_volume"]
    assert inr < us


def test_the_volume_gate_never_scales_to_zero():
    """A floor of 0 would admit every keyword including dead ones."""
    tiny = {"currency": "X", "symbol": "", "b2b_min_price": 1.0, "volume_scale": 0.00001}
    for name in PROFILES:
        assert resolve_gates(PROFILES[name], tiny)["min_volume"] >= 1


def test_an_unknown_country_falls_back_without_crashing():
    m = market_for("ZZ")
    assert m["b2b_min_price"] > 0
    assert resolve_gates(PROFILES["b2b"], m)["min_median_price"] > 0


def test_market_lookup_is_case_insensitive():
    assert market_for("in") == market_for("IN")


def test_resolve_gates_does_not_mutate_the_profile():
    """Profiles are module-level dicts; mutating them would leak the first
    country's thresholds into every later run in the same process."""
    before = dict(PROFILES["b2b"]["gates"])
    resolve_gates(PROFILES["b2b"], market_for("IN"))
    resolve_gates(PROFILES["b2b"], market_for("JP"))
    assert PROFILES["b2b"]["gates"] == before


def test_india_is_the_default_marketplace():
    """Stated target. A US default would silently apply USD gates to rupee
    prices, which is the failure the market block exists to prevent."""
    assert DEFAULT_COUNTRY == "IN"
    assert DEFAULT_COUNTRY in MARKETS


def test_the_dry_run_fixture_prices_scale_with_the_marketplace():
    """Without this the fixture emits dollar-scale prices against a rupee
    gate, the B2B floor rejects every candidate, and a correct gate on
    unrealistic data looks exactly like a broken gate."""
    us = comp.dry_run_payload("some unnamed keyword", "US")
    inr = comp.dry_run_payload("some unnamed keyword", "IN")
    us_price = comp.parse_price(comp.products_of(us)[0]["product_price"])
    in_price = comp.parse_price(comp.products_of(inr)[0]["product_price"])
    assert in_price > us_price * 50


def test_the_named_fixtures_stay_in_usd_whatever_the_country():
    """They carry the exact values the arithmetic tests assert on."""
    for country in ("US", "IN", "JP"):
        p = comp.products_of(comp.dry_run_payload("honey", country))
        assert p[0]["product_price"] == "$14.99", country


def test_both_profiles_produce_a_shortlist_on_the_india_defaults():
    """A profile that rejects 100% is either correctly strict or broken, and
    from the outside those look identical — so both must be non-empty on
    fixtures built to contain viable candidates."""
    kws = ["moringa oil", "turmeric soap", "nitrile examination gloves",
           "a4 copier paper ream", "corrugated box 5 ply", "safety shoes steel toe"]
    b2c, _ = _run(kws, profile_name="b2c", country="IN")
    b2b, _ = _run(kws, profile_name="b2b", country="IN")
    assert b2c, "B2C shortlist empty on India defaults"
    assert b2b, "B2B shortlist empty on India defaults"


def test_the_default_keywords_demonstrate_both_profiles_on_the_default_market():
    """UX regression guard. The first command in the README is a bare
    --dry-run. When IN became the default marketplace the default keywords
    were still the USD-priced named fixtures, so B2B came back '0 of 4'
    against a rupee floor — a correct pipeline that reads as broken to anyone
    running it for the first time."""
    kws = ["giloy juice", "aloe vera gel", "chyawanprash", "moringa oil"]
    b2c, _ = _run(kws, profile_name="b2c", country=DEFAULT_COUNTRY)
    b2b, _ = _run(kws, profile_name="b2b", country=DEFAULT_COUNTRY)
    assert b2c, "default dry-run shows an empty B2C shortlist"
    assert b2b, "default dry-run shows an empty B2B shortlist"

    only_b2c = {r["keyword"] for r in b2c} - {r["keyword"] for r in b2b}
    only_b2b = {r["keyword"] for r in b2b} - {r["keyword"] for r in b2c}
    assert only_b2c and only_b2b, (
        "the defaults should show a term on each side alone — that divergence "
        "is the reason two profiles exist")
