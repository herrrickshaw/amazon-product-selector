"""Plumbing shared by trends.py and competition.py: cache ageing and the
retrying JSON GET. Standard library only; no module-level state.

Kept separate so the two fetchers differ only in what is genuinely theirs
(URL shape, auth header, error wording), not in copy-pasted retry loops.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

RETRYABLE_STATUS = (429, 500, 502, 503, 504)


def slugify(text: str) -> str:
    """Lower-case, non-alphanumerics collapsed to '_' — safe cache filename part."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def cache_age_hours(path: Path) -> float | None:
    """Hours since `path` was last written, or None if it does not exist."""
    if not path.exists():
        return None
    return (time.time() - path.stat().st_mtime) / 3600.0


def cache_is_fresh(path: Path, max_age_hours: float) -> bool:
    """A cache with no expiry is worse than no cache: it silently answers
    today's question with last month's data, and nothing downstream can tell.
    max_age_hours <= 0 disables the check (keep forever)."""
    if not path.exists():
        return False
    if max_age_hours <= 0:
        return True
    return (time.time() - path.stat().st_mtime) / 3600.0 <= max_age_hours


def get_json(
    url: str,
    headers: dict[str, str],
    *,
    timeout: int,
    retries: int,
    http_error: Callable[[int, str], str],
    url_error: Callable[[object], str],
) -> dict:
    """GET `url` and decode JSON. Retries 429/5xx with exponential backoff.

    `http_error(code, label)` and `url_error(reason)` build the RuntimeError
    message for a non-retryable HTTP status / an unreachable host, so callers
    keep their own, API-specific wording.
    """
    last = ""
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in RETRYABLE_STATUS and attempt < retries - 1:
                wait = 2 ** (attempt + 1)
                print(f"    {last} — retrying in {wait}s")
                time.sleep(wait)
                continue
            raise RuntimeError(http_error(e.code, last)) from None
        except urllib.error.URLError as e:
            raise RuntimeError(url_error(e.reason)) from None
    raise RuntimeError(f"gave up after {retries} attempts ({last})")
