"""Regression tests from the 2026-10-25 static-analysis sweep (ruff DTZ004)."""
import warnings
from datetime import date

from amazon_selector.trends import _parse_date


def test_parse_epoch_does_not_use_deprecated_utcfromtimestamp():
    # datetime.utcfromtimestamp() is deprecated since 3.12 and scheduled for removal; every epoch-valued
    # observation from the trends API emitted a DeprecationWarning (visible in the suite) and will raise once
    # the API is dropped. The result must stay the UTC calendar date.
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert _parse_date(1767571200) == date(2026, 1, 5)
        assert _parse_date(1767571200000) == date(2026, 1, 5)
        assert _parse_date(1767571200 + 23 * 3600) == date(2026, 1, 5)   # still Jan 5 in UTC, local tz must not leak in
