"""Characterisation tests for the shared HTTP/cache helpers. No network."""
import io
import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "amazon_selector"))

import _http  # noqa: E402


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _kw():
    return dict(timeout=1, retries=3,
                http_error=lambda c, label: f"E:{label}",
                url_error=lambda r: f"U:{r}")


def test_slugify():
    assert _http.slugify("  Moringa Oil! (1L) ") == "moringa_oil_1l"


def test_cache_age_and_freshness(tmp_path):
    p = tmp_path / "x.json"
    assert _http.cache_age_hours(p) is None
    assert _http.cache_is_fresh(p, 24) is False
    p.write_text("{}")
    old = p.stat().st_mtime - 48 * 3600
    os.utime(p, (old, old))
    assert round(_http.cache_age_hours(p)) == 48
    assert _http.cache_is_fresh(p, 24) is False
    assert _http.cache_is_fresh(p, 72) is True
    assert _http.cache_is_fresh(p, 0) is True  # 0 disables expiry


def test_get_json_retries_then_succeeds(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout):
        calls.append(req.full_url)
        if len(calls) < 3:
            raise urllib.error.HTTPError(req.full_url, 503, "x", {}, None)
        return _Resp(b'{"ok": 1}')

    monkeypatch.setattr(_http.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(_http.time, "sleep", lambda s: None)
    assert _http.get_json("http://t.test/", {}, **_kw()) == {"ok": 1}
    assert len(calls) == 3


def test_get_json_non_retryable_uses_callback(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 403, "x", {}, None)

    monkeypatch.setattr(_http.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="E:HTTP 403"):
        _http.get_json("http://t.test/", {}, **_kw())


def test_get_json_gives_up_after_retries(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 429, "x", {}, None)

    monkeypatch.setattr(_http.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(_http.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="E:HTTP 429"):
        _http.get_json("http://t.test/", {}, **_kw())


def test_get_json_unreachable(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.URLError("boom")

    monkeypatch.setattr(_http.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="U:boom"):
        _http.get_json("http://t.test/", {}, **_kw())
