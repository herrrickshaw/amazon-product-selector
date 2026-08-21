"""
Tests for the Nexscope keyword-history adapter.

This module was written against an ASSUMED response schema — the docs host is
blocked by the egress proxy in the environment it was built in — so the tests
carry more weight than usual. Two things they must guarantee:

  1. A wrong guess fails LOUDLY and names the keys the API actually returned.
    A rigid parser that guessed wrong would return an empty series, and an
    empty series is indistinguishable from "nobody searches for this" — a
    silently wrong trend is worse than a crash.
  2. The trend arithmetic is right on series whose answers are known by
    construction, because nothing downstream can check it.

Run:
    python -m pytest tests/test_trends.py -v
"""

import math
import os
import subprocess
import sys
from datetime import date, timedelta

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "amazon_selector"))

from trends import (  # noqa: E402
    DEFAULT_PATH,
    SchemaMismatch,
    _find_series,
    _parse_date,
    _parse_number,
    build_url,
    default_endpoint,
    dry_run_payload,
    infer_period_days,
    log_slope,
    momentum,
    opportunity_flag,
    parse_series,
    redact,
    summarise_keyword,
    year_over_year,
)


# --------------------------------------------------------------- credentials
def test_the_live_credential_is_not_committed():
    key = os.environ.get("NEXSCOPE_API_KEY")
    if not key:
        pytest.skip("NEXSCOPE_API_KEY not in the environment — nothing to scan for")
    out = subprocess.run(["git", "grep", "-lI", "-e", key],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.strip() == "", f"credential value found in: {out.stdout}"


def test_no_nexscope_key_literal_is_committed():
    """Nexscope keys carry an `nk-` prefix. Catch the shape anywhere in the
    tree, whatever variable it is assigned to."""
    out = subprocess.run(["git", "grep", "-lIE", r"\bnk-[A-Za-z0-9]{20,}\b"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.strip() == "", f"API-key-shaped literal in: {out.stdout}"


def test_the_module_reads_the_credential_from_the_environment_only():
    path = os.path.join(ROOT, "amazon_selector", "trends.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert 'os.environ.get("NEXSCOPE_API_KEY")' in src


def test_redact_strips_the_key(monkeypatch):
    monkeypatch.setenv("NEXSCOPE_API_KEY", "SECRET_VALUE_HERE")
    assert "SECRET_VALUE_HERE" not in redact("...SECRET_VALUE_HERE...")


def test_the_key_never_travels_in_the_url(monkeypatch):
    """It belongs in the auth header. Checked by looking for credential-shaped
    QUERY PARAMS and for the secret's own value — not for the substring "key",
    which `keyword=` legitimately contains."""
    monkeypatch.setenv("NEXSCOPE_API_KEY", "SENTINEL_SECRET_0000")
    url = build_url("https://api.nexscope.ai/v1/x", "moringa oil", "US", None, None)
    assert "SENTINEL_SECRET_0000" not in url
    for param in ("api_key=", "apikey=", "access_token=", "token=", "&key=", "?key="):
        assert param not in url.lower()
    assert "keyword=moringa+oil" in url and "country=US" in url


# ------------------------------------------------------- endpoint resolution
def test_endpoint_prefers_the_explicit_full_url(monkeypatch):
    monkeypatch.setenv("NEXSCOPE_ENDPOINT", "https://example.test/custom")
    monkeypatch.setenv("NEXSCOPE_PROXY_BASE", "https://api.nexscope.ai/")
    assert default_endpoint() == "https://example.test/custom"


def test_endpoint_falls_back_to_the_proxy_base(monkeypatch):
    monkeypatch.delenv("NEXSCOPE_ENDPOINT", raising=False)
    monkeypatch.setenv("NEXSCOPE_PROXY_BASE", "https://api.nexscope.ai/")
    assert default_endpoint() == f"https://api.nexscope.ai/{DEFAULT_PATH}"


def test_endpoint_does_not_double_the_slash(monkeypatch):
    monkeypatch.delenv("NEXSCOPE_ENDPOINT", raising=False)
    monkeypatch.setenv("NEXSCOPE_PROXY_BASE", "https://api.nexscope.ai///")
    assert "//" not in default_endpoint().replace("https://", "")


# ------------------------------------------------------------ schema tolerance
@pytest.mark.parametrize("payload", [
    {"history": [{"date": "2026-01-01", "search_volume": 10}]},
    {"data": {"history": [{"date": "2026-01-01", "search_volume": 10}]}},
    {"data": {"series": [{"week_start": "2026-01-01", "volume": 10}]}},
    {"result": {"payload": {"records": [{"period": "2026-01-01", "searches": 10}]}}},
    [{"date": "2026-01-01", "sv": 10}],
])
def test_find_series_locates_the_array_whatever_the_nesting(payload):
    assert len(_find_series(payload)) == 1


def test_parse_series_accepts_alternative_field_names():
    payload = {"data": {"series": [
        {"week_start": "2026-01-05", "volume": "1,200"},
        {"week_start": "2026-01-12", "volume": 1400},
    ]}}
    points = parse_series(payload, "kw")
    assert [p[1] for p in points] == [1200.0, 1400.0]


def test_parse_series_sorts_ascending_by_date():
    payload = {"history": [
        {"date": "2026-02-01", "search_volume": 2},
        {"date": "2026-01-01", "search_volume": 1},
    ]}
    points = parse_series(payload, "kw")
    assert [p[0] for p in points] == [date(2026, 1, 1), date(2026, 2, 1)]


def test_unrecognised_series_key_raises_and_names_the_real_keys():
    """The whole design depends on this: one live call must tell you the fix."""
    with pytest.raises(SchemaMismatch) as exc:
        parse_series({"weekly_demand_curve": "not-a-list", "meta": 1}, "moringa oil")
    msg = str(exc.value)
    assert "moringa oil" in msg
    assert "weekly_demand_curve" in msg, "must name the keys actually returned"
    assert "SERIES_KEYS" in msg, "must say which tuple to correct"


def test_unrecognised_point_keys_raise_and_name_the_record_keys():
    payload = {"history": [{"yyyymmdd": "20260101", "demand_index": 12}]}
    with pytest.raises(SchemaMismatch) as exc:
        parse_series(payload, "kw")
    msg = str(exc.value)
    assert "yyyymmdd" in msg and "demand_index" in msg
    assert "DATE_KEYS" in msg and "VOLUME_KEYS" in msg


def test_an_empty_series_is_never_silently_reported_as_zero_demand():
    with pytest.raises(SchemaMismatch):
        parse_series({"status": "ok"}, "kw")


# ------------------------------------------------------------------ parsing
@pytest.mark.parametrize("raw,expected", [
    ("2026-01-05", date(2026, 1, 5)),
    ("2026/01/05", date(2026, 1, 5)),
    ("20260105", date(2026, 1, 5)),
    ("2026-01-05T00:00:00Z", date(2026, 1, 5)),
    (1767571200, date(2026, 1, 5)),          # epoch seconds
    (1767571200000, date(2026, 1, 5)),       # epoch millis
    ("not a date", None),
    (None, None),
])
def test_parse_date(raw, expected):
    assert _parse_date(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    (1200, 1200.0), ("1,200", 1200.0), ("1200.5", 1200.5),
    (None, None), ("", None), ("N/A", None), (True, None),
])
def test_parse_number(raw, expected):
    assert _parse_number(raw) == expected


def test_parse_number_rejects_booleans_rather_than_reading_them_as_one():
    """bool is a subclass of int in Python; float(True) == 1.0 would turn a
    flag field into a search volume of 1."""
    assert _parse_number(True) is None and _parse_number(False) is None


# --------------------------------------------------------------- trend math
def test_log_slope_recovers_a_known_growth_rate():
    """A series growing exactly 3% per period must come back as 3%."""
    values = [1000 * (1.03 ** i) for i in range(30)]
    slope = log_slope(values)
    assert math.exp(slope) - 1 == pytest.approx(0.03, abs=1e-6)


def test_log_slope_is_scale_invariant():
    """100->200 and 10000->20000 are the same opportunity signal. A linear fit
    would rank the second 100x higher; this is why the fit is in log space."""
    small = log_slope([100 * (1.05 ** i) for i in range(20)])
    large = log_slope([10000 * (1.05 ** i) for i in range(20)])
    assert small == pytest.approx(large, abs=1e-9)


def test_log_slope_is_zero_for_a_flat_series():
    assert log_slope([500] * 20) == pytest.approx(0.0, abs=1e-12)


def test_log_slope_drops_zeros_rather_than_substituting_for_them():
    """log(0) is undefined, not small. Clamping it invents a data point."""
    assert log_slope([0, 0, 100, 110, 121]) == pytest.approx(math.log(1.1), abs=1e-9)


def test_log_slope_needs_three_usable_points():
    assert log_slope([100, 110]) is None
    assert log_slope([0, 0, 0, 5]) is None


def test_momentum_compares_windows_not_endpoints():
    """Last 4 mean 200, prior 4 mean 100 -> +100%."""
    assert momentum([100] * 4 + [200] * 4, window=4) == pytest.approx(1.0)


def test_momentum_is_not_swung_by_a_single_spike():
    steady = [100] * 8
    spiked = [100, 100, 100, 900, 100, 100, 100, 100]
    assert abs(momentum(spiked, 4) - momentum(steady, 4)) < 2.0, \
        "a lone spike must not dominate a windowed comparison"


def test_momentum_needs_two_full_windows():
    assert momentum([100] * 7, window=4) is None


def test_year_over_year_compares_against_the_reading_a_year_back():
    start = date(2025, 1, 6)
    pts = [(start + timedelta(weeks=i), 100.0, None) for i in range(52)]
    pts.append((start + timedelta(weeks=52), 150.0, None))
    assert year_over_year(pts, 7) == pytest.approx(0.5)


def test_year_over_year_is_none_when_the_series_is_too_short():
    start = date(2026, 1, 5)
    pts = [(start + timedelta(weeks=i), 100.0, None) for i in range(10)]
    assert year_over_year(pts, 7) is None


def test_infer_period_days_detects_weekly_and_monthly():
    start = date(2026, 1, 5)
    weekly = [(start + timedelta(weeks=i), 1.0, None) for i in range(10)]
    assert infer_period_days(weekly) == 7
    monthly = [(start + timedelta(days=30 * i), 1.0, None) for i in range(10)]
    assert infer_period_days(monthly) == 30


# ------------------------------------------------------------------- flags
def _summary_for(keyword):
    points = parse_series(dry_run_payload(keyword), keyword)
    s = summarise_keyword(keyword, points)
    s["opportunity_flag"] = opportunity_flag(s)
    return s


def test_a_steady_riser_is_flagged_rising():
    s = _summary_for("moringa oil")
    assert s["opportunity_flag"] == "RISING"
    assert s["growth_per_period_pct"] == pytest.approx(3.0, abs=0.1)


def test_a_flat_series_is_flagged_flat():
    s = _summary_for("honey")
    assert s["opportunity_flag"] == "flat"


def test_a_seasonal_category_is_diagnosed_as_seasonal_not_merely_spiky():
    """Regression test for an ordering bug: a strongly seasonal series has high
    variance BY DEFINITION, so checking volatility first labelled every
    seasonal category 'spiky' and the seasonal branch never fired. The specific
    diagnosis has to be tested before the generic one."""
    s = _summary_for("mouth tape")
    assert s["yoy_pct"] < 0, "fixture must be below the same week last year"
    assert s["volatility_cv"] > 0.6, "and must also be volatile enough to trip the spiky check"
    assert s["opportunity_flag"] == "seasonal bounce — down year-over-year"


def test_a_noisy_series_with_no_trend_is_flagged_spiky():
    s = _summary_for("sleep gummies")
    assert s["opportunity_flag"] == "spiky — growth may be noise"


def test_the_spiky_flag_does_not_fire_on_a_clean_riser():
    assert "spiky" not in _summary_for("moringa oil")["opportunity_flag"]


def test_summarise_reports_the_caveat_columns_alongside_every_growth_number():
    """Growth without volatility and YoY next to it is not interpretable."""
    s = _summary_for("moringa oil")
    for col in ("volatility_cv", "yoy_pct", "momentum_pct", "pct_of_peak", "observations"):
        assert col in s


def test_the_dry_run_fixture_exercises_every_flag():
    """If a fixture stops discriminating, the dry run silently stops being a
    check — this asserts the four series still produce four distinct verdicts."""
    flags = {_summary_for(k)["opportunity_flag"]
             for k in ("moringa oil", "honey", "mouth tape", "sleep gummies")}
    assert len(flags) == 4, f"expected four distinct verdicts, got {flags}"
