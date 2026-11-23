"""End-to-end CLI flows for trends / competition / shortlist: dry-run fixtures, cache policy,
failure isolation and the report writers. No network (conftest blocks sockets); keys are fake."""
import csv
import json
import os
import sys
import time

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "amazon_selector"))

import competition as comp  # noqa: E402
import shortlist as sl  # noqa: E402
import trends as tr  # noqa: E402


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    for k in ("NEXSCOPE_API_KEY", "OPENWEB_NINJA_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def run(mod, monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", [mod.__name__ + ".py", *argv])
    return mod.main()


def read_csv(p):
    with open(p, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# =============================== trends ===============================

def test_trends_dry_run_writes_ranked_reports(sandbox, monkeypatch, capsys):
    rc = run(tr, monkeypatch, "--dry-run", "--out", "out")
    assert rc == 0
    rows = read_csv(sandbox / "out/nexscope_keyword_trends.csv")
    assert [r["keyword"] for r in rows] and len(rows) == 4
    moms = [float(r["momentum_pct"]) for r in rows if r["momentum_pct"]]
    assert moms == sorted(moms, reverse=True)                      # sorted by momentum, desc
    md = (sandbox / "out/nexscope_keyword_trends.md").read_text()
    assert md.startswith("# Amazon keyword demand trends") and "Marketplace: **US** · 4 keywords" in md
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "Ranked by momentum" in out and "Assumed request shape" in out
    assert not (sandbox / "data").exists()                         # dry run never caches


def test_trends_keywords_file_and_empty_keywords(sandbox, monkeypatch, capsys):
    (sandbox / "k.txt").write_text("# comment\nmoringa oil\n\n  honey  \n")
    assert run(tr, monkeypatch, "--dry-run", "--keywords-file", "k.txt", "--country", "in") == 0
    rows = read_csv(sandbox / "reports/nexscope_keyword_trends.csv")
    assert sorted(r["keyword"] for r in rows) == ["honey", "moringa oil"]
    assert "Marketplace: **IN**" in (sandbox / "reports/nexscope_keyword_trends.md").read_text()
    with pytest.raises(SystemExit, match="No keywords"):
        run(tr, monkeypatch, "--dry-run", "--keywords", " , ")


def test_trends_live_requires_key(monkeypatch):
    with pytest.raises(SystemExit, match="NEXSCOPE_API_KEY"):
        run(tr, monkeypatch, "--keywords", "x")


def test_trends_redact(monkeypatch):
    monkeypatch.setenv("NEXSCOPE_API_KEY", "SECRETVALUE")
    assert tr.redact("a SECRETVALUE b") == "a <NEXSCOPE_API_KEY> b"
    monkeypatch.delenv("NEXSCOPE_API_KEY")
    assert tr.redact("a b") == "a b"


def test_trends_live_fetch_caches_and_second_run_uses_cache(sandbox, monkeypatch, capsys):
    monkeypatch.setenv("NEXSCOPE_API_KEY", "TESTKEY")
    calls = []

    def fake_fetch(kw, country, key, endpoint, auth_header, auth_prefix, start=None, end=None, **k):
        calls.append((kw, country, key, start))
        return tr.dry_run_payload(kw)

    monkeypatch.setattr(tr, "fetch", fake_fetch)
    assert run(tr, monkeypatch, "--keywords", "moringa oil,honey", "--weeks", "10") == 0
    assert [c[0] for c in calls] == ["moringa oil", "honey"] and calls[0][2] == "TESTKEY"
    from datetime import date, timedelta
    assert calls[0][3] == (date.today() - timedelta(weeks=10)).isoformat()
    assert len(list((sandbox / "data/nexscope_raw").glob("*.json"))) == 2
    calls.clear()
    assert run(tr, monkeypatch, "--keywords", "moringa oil,honey") == 0      # fresh cache: no refetch
    assert calls == []


def test_trends_no_cache_flag_and_fetch_failure_skips_keyword(sandbox, monkeypatch, capsys):
    monkeypatch.setenv("NEXSCOPE_API_KEY", "TESTKEY")

    def fake_fetch(kw, *a, **k):
        if kw == "bad":
            raise RuntimeError("HTTP 500 for url")
        return tr.dry_run_payload(kw)

    monkeypatch.setattr(tr, "fetch", fake_fetch)
    assert run(tr, monkeypatch, "--keywords", "bad,honey", "--no-cache") == 0
    assert not (sandbox / "data").exists()
    rows = read_csv(sandbox / "reports/nexscope_keyword_trends.csv")
    assert [r["keyword"] for r in rows] == ["honey"]
    assert "'bad': HTTP 500 for url" in capsys.readouterr().out


def test_trends_schema_mismatch_row_is_skipped_and_all_failed_returns_1(sandbox, monkeypatch, capsys):
    monkeypatch.setenv("NEXSCOPE_API_KEY", "TESTKEY")
    monkeypatch.setattr(tr, "fetch", lambda *a, **k: {"unexpected": []})
    assert run(tr, monkeypatch, "--keywords", "x") == 1
    out = capsys.readouterr().out
    assert "SCHEMA MISMATCH" in out and "Nothing parsed" in out
    assert not (sandbox / "reports").exists()


def test_trends_from_cache_missing_stale_and_fresh(sandbox, monkeypatch, capsys):
    assert run(tr, monkeypatch, "--from-cache", "--keywords", "honey") == 1
    assert "no cache — skipped" in capsys.readouterr().out
    cp = tr.cache_path("honey", "US")
    cp.parent.mkdir(parents=True)
    cp.write_text(json.dumps(tr.dry_run_payload("honey")))
    old = time.time() - 400 * 3600
    os.utime(cp, (old, old))
    assert run(tr, monkeypatch, "--from-cache", "--keywords", "honey") == 0
    out = capsys.readouterr().out
    assert "STALE CACHE" in out and "CACHE ONLY" in out
    os.utime(cp, None)
    assert run(tr, monkeypatch, "--from-cache", "--keywords", "honey") == 0
    assert "STALE CACHE" not in capsys.readouterr().out


def test_trends_cache_max_age_zero_never_expires(sandbox, monkeypatch, capsys):
    cp = tr.cache_path("honey", "US")
    cp.parent.mkdir(parents=True)
    cp.write_text(json.dumps(tr.dry_run_payload("honey")))
    old = time.time() - 9999 * 3600
    os.utime(cp, (old, old))
    assert run(tr, monkeypatch, "--from-cache", "--keywords", "honey", "--cache-max-age-hours", "0") == 0
    assert "STALE CACHE" not in capsys.readouterr().out


def test_trends_writers_directly(tmp_path):
    tr.write_csv(tmp_path / "e.csv", [])
    assert not (tmp_path / "e.csv").exists()
    rows = [{"keyword": "a", "momentum_pct": None}, {"keyword": "b", "momentum_pct": 5.0}]
    tr.write_markdown(tmp_path / "m.md", rows, "US")
    md = (tmp_path / "m.md").read_text()
    assert "| a |  |" in md and "| b | 5.0 |" in md and "Schema caveat" in md


# =============================== competition ===============================

def test_competition_dry_run_default_groups(sandbox, monkeypatch, capsys):
    assert run(comp, monkeypatch, "--dry-run", "--out", "o", "--pages", "1") == 0
    summ = read_csv(sandbox / "o/amazon_demand_summary.csv")
    assert [s["group"] for s in summ] == list(comp.DEFAULT_GROUPS)
    listings = read_csv(sandbox / "o/amazon_demand_listings.csv")
    assert listings and {r["group"] for r in listings} == set(comp.DEFAULT_GROUPS)
    md = (sandbox / "o/amazon_demand_comparison.md").read_text()
    assert md.startswith("# Amazon demand comparison") and "## Verdict" in md and "## Keywords searched" in md
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "--- Verdict ---" in out and "Written:" in out


def test_competition_custom_groups_and_country(sandbox, monkeypatch, capsys):
    assert run(comp, monkeypatch, "--dry-run", "--pages", "1", "--country", "in",
               "--group", "tea=green tea, black tea", "--group", "coffee=espresso") == 0
    summ = read_csv(sandbox / "reports/amazon_demand_summary.csv")
    assert [s["group"] for s in summ] == ["tea", "coffee"]
    assert "Marketplace: **IN**" in (sandbox / "reports/amazon_demand_comparison.md").read_text()


def test_parse_group_args_errors():
    assert comp.parse_group_args(["a = x , y ,, z"]) == {"a": ["x", "y", "z"]}
    with pytest.raises(SystemExit, match="NAME=kw1,kw2"):
        comp.parse_group_args(["nokeywords"])
    with pytest.raises(SystemExit, match="no keywords"):
        comp.parse_group_args(["a= , "])


def test_competition_live_requires_key_and_redacts(monkeypatch):
    with pytest.raises(SystemExit, match="OPENWEB_NINJA_API_KEY"):
        run(comp, monkeypatch, "--group", "a=b")
    monkeypatch.setenv("OPENWEB_NINJA_API_KEY", "SEKRET")
    assert comp.api_key() == "SEKRET"
    assert comp.redact("x SEKRET y") == "x <OPENWEB_NINJA_API_KEY> y"
    monkeypatch.delenv("OPENWEB_NINJA_API_KEY")
    assert comp.redact("x") == "x"


def test_competition_live_fetch_cache_and_failure(sandbox, monkeypatch, capsys):
    monkeypatch.setenv("OPENWEB_NINJA_API_KEY", "TESTKEY")
    seen = []

    def fake_fetch(query, country, page, key):
        seen.append((query, page))
        if query == "bad":
            raise RuntimeError("HTTP 500 for url")
        return comp.dry_run_payload(query, country)

    monkeypatch.setattr(comp, "fetch", fake_fetch)
    assert run(comp, monkeypatch, "--group", "g=moringa oil,bad", "--pages", "2") == 0
    assert ("moringa oil", 1) in seen and ("moringa oil", 2) in seen and ("bad", 1) in seen
    assert len(list((sandbox / "data/amazon_raw").glob("*.json"))) == 2          # only successes cached
    out = capsys.readouterr().out
    assert "'bad' p1: HTTP 500 for url" in out
    seen.clear()
    assert run(comp, monkeypatch, "--group", "g=moringa oil", "--pages", "2") == 0
    assert seen == []                                                             # served from fresh cache
    assert "cached" in capsys.readouterr().out


def test_competition_all_failed_returns_one_and_no_cache_flag(sandbox, monkeypatch, capsys):
    monkeypatch.setenv("OPENWEB_NINJA_API_KEY", "TESTKEY")
    monkeypatch.setattr(comp, "fetch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert run(comp, monkeypatch, "--group", "g=x", "--pages", "1") == 1
    out = capsys.readouterr().out
    assert "nothing collected for g" in out and "Nothing fetched" in out
    monkeypatch.setattr(comp, "fetch", lambda q, c, p, k: comp.dry_run_payload(q, c))
    assert run(comp, monkeypatch, "--group", "g=moringa oil", "--pages", "1", "--no-cache") == 0
    assert not (sandbox / "data").exists()


def test_competition_from_cache_missing_stale_and_fresh(sandbox, monkeypatch, capsys):
    assert run(comp, monkeypatch, "--from-cache", "--group", "g=moringa oil", "--pages", "1") == 1
    assert "no cache for 'moringa oil' p1 — skipped" in capsys.readouterr().out
    cp = comp.cache_path("moringa oil", "US", 1)
    cp.parent.mkdir(parents=True)
    cp.write_text(json.dumps(comp.dry_run_payload("moringa oil")))
    old = time.time() - 100 * 3600
    os.utime(cp, (old, old))
    assert run(comp, monkeypatch, "--from-cache", "--group", "g=moringa oil", "--pages", "1") == 0
    out = capsys.readouterr().out
    assert "STALE CACHE 'moringa oil' p1" in out and "limit 24h" in out and "CACHE ONLY" in out


def test_competition_verdict_and_writers(tmp_path):
    assert comp.verdict([{"group": "a"}], "US") == ["Only one group — nothing to compare."]
    a = {"group": "a", "units_past_month_floor": 100, "est_monthly_revenue_floor": 1000,
         "cumulative_ratings": 50, "demand_per_listing": 0.5, "units_coverage_pct": 50, "median_price": 10}
    b = dict(a, group="b", units_past_month_floor=50, cumulative_ratings=500, demand_per_listing=0.25)
    lines = comp.verdict([a, b], "US")
    assert any("Units bought in past month (floor)" in ln and "2.0x" in ln for ln in lines)
    zero = {"units_past_month_floor": 0, "est_monthly_revenue_floor": 0, "cumulative_ratings": 0,
            "demand_per_listing": 0, "units_coverage_pct": None, "median_price": None}
    empty = comp.verdict([dict(zero, group="a"), dict(zero, group="b")], "US")
    assert any("no data on any group" in ln for ln in empty)
    comp.write_csv(tmp_path / "x.csv", [])
    assert not (tmp_path / "x.csv").exists()
    comp.write_markdown(tmp_path / "d/m.md", [a, dict(b, median_price=None)], lines, "US", 2, {"a": ["k1"]})
    md = (tmp_path / "d/m.md").read_text()
    assert "top 2 search page(s)" in md and "- **a**: k1" in md and "| b |" in md


def test_http_gave_up_after_zero_retries():
    import _http
    with pytest.raises(RuntimeError, match="gave up after 0 attempts"):
        _http.get_json("http://x", {}, timeout=1, retries=0, http_error=lambda c, lbl: lbl,
                       url_error=lambda r: str(r))


# =============================== shortlist ===============================

def test_shortlist_dry_run_both_profiles(sandbox, monkeypatch, capsys):
    assert run(sl, monkeypatch, "--dry-run", "--out", "o") == 0
    for name in ("b2c", "b2b"):
        assert (sandbox / f"o/amazon_shortlist_{name}.csv").exists()
        assert (sandbox / f"o/amazon_shortlist_{name}.md").exists()
    out = capsys.readouterr().out
    assert "=== B2C vs B2B ===" in out and "shortlisted in both:" in out and "Written:" in out
    assert "(INR)" in out                                           # default marketplace is India


def test_shortlist_single_profile_and_keywords_file(sandbox, monkeypatch, capsys):
    (sandbox / "k.txt").write_text("# c\nmoringa oil\nhoney\n")
    assert run(sl, monkeypatch, "--dry-run", "--profile", "b2c", "--keywords-file", "k.txt",
               "--country", "US") == 0
    out = capsys.readouterr().out
    assert "B2C vs B2B" not in out and "(USD)" in out
    assert not (sandbox / "reports/amazon_shortlist_b2b.csv").exists()
    with pytest.raises(SystemExit, match="No keywords"):
        run(sl, monkeypatch, "--dry-run", "--keywords", ",")


def test_shortlist_live_requires_both_keys(monkeypatch):
    with pytest.raises(SystemExit, match="NEXSCOPE_API_KEY"):
        run(sl, monkeypatch, "--keywords", "x")
    monkeypatch.setenv("NEXSCOPE_API_KEY", "T")
    with pytest.raises(SystemExit, match="OPENWEB_NINJA_API_KEY"):
        run(sl, monkeypatch, "--keywords", "x")


def test_shortlist_nothing_collected_returns_one(sandbox, monkeypatch, capsys):
    monkeypatch.setenv("NEXSCOPE_API_KEY", "T")
    monkeypatch.setenv("OPENWEB_NINJA_API_KEY", "T")
    monkeypatch.setattr(tr, "fetch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert run(sl, monkeypatch, "--keywords", "x", "--profile", "b2c") == 1
    assert "Nothing collected" in capsys.readouterr().out


def test_shortlist_schema_mismatch_is_reported_per_keyword(sandbox, monkeypatch, capsys):
    monkeypatch.setenv("NEXSCOPE_API_KEY", "T")
    monkeypatch.setenv("OPENWEB_NINJA_API_KEY", "T")
    monkeypatch.setattr(tr, "fetch", lambda *a, **k: {"junk": 1})
    assert run(sl, monkeypatch, "--keywords", "x", "--profile", "b2c") == 1
    assert "SCHEMA MISMATCH" in capsys.readouterr().out


def test_shortlist_live_path_writes_both_caches(sandbox, monkeypatch, capsys):
    monkeypatch.setenv("NEXSCOPE_API_KEY", "T")
    monkeypatch.setenv("OPENWEB_NINJA_API_KEY", "T")
    monkeypatch.setattr(tr, "fetch", lambda kw, *a, **k: tr.dry_run_payload(kw))
    monkeypatch.setattr(comp, "fetch", lambda q, c, p, k: comp.dry_run_payload(q, c))
    assert run(sl, monkeypatch, "--keywords", "moringa oil", "--profile", "b2c", "--pages", "1") == 0
    assert list((sandbox / "data/nexscope_raw").glob("*.json"))
    assert list((sandbox / "data/amazon_raw").glob("*.json"))
    # second run: everything fresh, no fetches
    monkeypatch.setattr(tr, "fetch", lambda *a, **k: pytest.fail("trend refetched"))
    monkeypatch.setattr(comp, "fetch", lambda *a, **k: pytest.fail("competition refetched"))
    assert run(sl, monkeypatch, "--keywords", "moringa oil", "--profile", "b2c", "--pages", "1") == 0


@pytest.mark.xfail(strict=True, raises=SystemExit,
                   reason="shortlist.main only skips the API-key check for --dry-run, so --from-cache "
                          "(documented as 'no network') still exits asking for NEXSCOPE_API_KEY; "
                          "trends.py and competition.py both skip it for --from-cache "
                          "(shortlist.py:~454)")
def test_shortlist_from_cache_does_not_need_api_keys(sandbox, monkeypatch, capsys):
    run(sl, monkeypatch, "--from-cache", "--keywords", "x", "--profile", "b2c")


def test_shortlist_from_cache_missing_trend_series_is_reported(sandbox, monkeypatch, capsys):
    monkeypatch.setenv("NEXSCOPE_API_KEY", "T")
    monkeypatch.setenv("OPENWEB_NINJA_API_KEY", "T")
    assert run(sl, monkeypatch, "--from-cache", "--keywords", "x", "--profile", "b2c") == 1
    assert "no cached trend series" in capsys.readouterr().out


def test_load_trend_payload_stale_cache_warning(sandbox, monkeypatch, capsys):
    class A:
        country = "US"
        trend_cache_max_age_hours = 1.0
        dry_run = False
        from_cache = True
        weeks = 52
        endpoint = "e"
        auth_header = "h"
        auth_prefix = "p"
        no_cache = False

    cp = tr.cache_path("honey", "US")
    cp.parent.mkdir(parents=True)
    cp.write_text('{"a": 1}')
    old = time.time() - 50 * 3600
    os.utime(cp, (old, old))
    assert sl.load_trend_payload("honey", A, None) == {"a": 1}
    assert "STALE TREND CACHE" in capsys.readouterr().out


def test_split_by_gates_keeps_pre_gate_reason():
    class A:
        allowed_flags = ["RISING"]
    gates = {"min_volume": 10, "min_observations": 3, "max_review_moat": 100, "min_median_price": None}
    pre = {"keyword": "a", "gate_failures": "trend: flat; low volume"}
    ok = {"keyword": "b", "opportunity_flag": "RISING", "latest_volume": 1000, "observations": 10,
          "review_moat": 5, "median_price": 20.0, "listings_sampled": 30}
    bad = dict(ok, keyword="c", review_moat=10_000)
    passed, rejected = sl.split_by_gates([pre, ok, bad], A, gates)
    assert [c["keyword"] for c in passed] == ["b"]
    assert [c["keyword"] for c in rejected] == ["a", "c"]
    assert rejected[0]["gate_failures"] == "trend: flat; low volume"        # not re-gated
    assert rejected[1]["gate_failures"] == "review moat 10,000 above 100"


def test_print_profile_report_and_read_keywords(capsys, tmp_path):
    sl.print_profile_report([], [{"keyword": "k", "gate_failures": "why"}], 1)
    out = capsys.readouterr().out
    assert "nothing passed the gates" in out and "k" in out and "why" in out
    sl.print_profile_report([{"rank": 1, "keyword": "z", "score": 0.5, "score_growth": 0.1,
                              "score_demand": 0.2, "score_openness": 0.3, "score_headroom": 0.4}], [], 1)
    assert "1. z" in capsys.readouterr().out
    f = tmp_path / "k.txt"
    f.write_text("# c\n a \n\nb\n")
    assert sl.read_keywords("ignored", str(f)) == ["a", "b"]
    assert sl.read_keywords(" x , ,y ", None) == ["x", "y"]


def test_shortlist_writers_union_columns_and_empty_sections(tmp_path):
    sl.write_csv(tmp_path / "e.csv", [])
    assert not (tmp_path / "e.csv").exists()
    sl.write_csv(tmp_path / "u.csv", [{"a": 1}, {"a": 2, "b": 3}])
    assert read_csv(tmp_path / "u.csv") == [{"a": "1", "b": ""}, {"a": "2", "b": "3"}]

    class A:
        allowed_flags = ["RISING"]
    prof = sl.PROFILES["b2b"]
    gates = {"min_volume": 100, "min_observations": 8, "max_review_moat": 1500, "min_median_price": 2000}
    sl.write_markdown(tmp_path / "m.md", [], [], "IN", A, prof, gates)
    md = (tmp_path / "m.md").read_text()
    assert "_Nothing passed the gates._" in md and "_Nothing was rejected._" in md
    assert "median price ≥ ₹2,000" in md and "0 of 0 candidates" in md
    sl.write_markdown(tmp_path / "m2.md", [{"rank": 1, "keyword": "k", "score": 0.9, "latest_volume": None}],
                      [{"keyword": "r", "opportunity_flag": "flat", "latest_volume": 5, "gate_failures": "x"}],
                      "US", A, prof, dict(gates, min_median_price=None))
    md2 = (tmp_path / "m2.md").read_text()
    assert "| 1 | k | 0.9 |  |" in md2 and "| r | flat | 5 | x |" in md2 and "median price" not in md2


# =============================== fetch error messages ===============================

def _capture_get_json(monkeypatch, mod):
    seen = {}

    def fake(url, headers, timeout, retries, http_error, url_error):
        seen.update(url=url, headers=headers, http_error=http_error, url_error=url_error,
                    timeout=timeout, retries=retries)
        return {"ok": True}

    monkeypatch.setattr(mod, "get_json", fake)
    return seen


def test_competition_fetch_builds_request_and_error_messages(monkeypatch):
    seen = _capture_get_json(monkeypatch, comp)
    assert comp.fetch("moringa oil", "IN", 2, "KEY", timeout=7, retries=2) == {"ok": True}
    assert seen["headers"] == {"x-api-key": "KEY"} and (seen["timeout"], seen["retries"]) == (7, 2)
    assert "query=moringa+oil" in seen["url"] and "country=IN" in seen["url"] and "page=2" in seen["url"]
    assert "KEY" not in seen["url"]
    assert "auth or plan rejection" in seen["http_error"](401, "HTTP 401")
    assert "auth or plan rejection" in seen["http_error"](403, "HTTP 403")
    assert "auth or plan" not in seen["http_error"](500, "HTTP 500")
    assert "cannot reach api.openwebninja.com (boom)" in seen["url_error"]("boom")


def test_trends_fetch_builds_request_and_error_messages(monkeypatch):
    seen = _capture_get_json(monkeypatch, tr)
    tr.fetch("honey", "US", "KEY", "https://h.example/path", "Authorization", "Bearer ",
             start="2025-01-01", end="2025-06-01")
    assert seen["headers"] == {"Authorization": "Bearer KEY"}
    assert "start_date=2025-01-01" in seen["url"] and "end_date=2025-06-01" in seen["url"]
    assert "Auth rejected" in seen["http_error"](401, "HTTP 401")
    assert "endpoint path is an assumption" in seen["http_error"](404, "HTTP 404")
    assert seen["http_error"](500, "HTTP 500").startswith("HTTP 500 for ")
    assert "cannot reach h.example (boom)" in seen["url_error"]("boom")
    assert tr.build_url("http://e", "k", "US", None, None) == "http://e?keyword=k&country=US"


# =============================== boundaries found by mutation testing ===============================

def test_ratio_zero_operand_is_not_applicable():
    assert comp._ratio(0, 5) == "n/a" and comp._ratio(5, 0) == "n/a" and comp._ratio(None, 3) == "n/a"
    assert comp._ratio(4, 2) == "2.0x" and comp._ratio(2, 4) == "2.0x"


def test_dedupe_prefers_listing_with_units(monkeypatch):
    seq = iter([{"data": {"products": [{"asin": "A1", "product_title": "x", "sales_volume": None,
                                        "product_price": "$1", "product_num_ratings": 1}]}},
                {"data": {"products": [{"asin": "A1", "product_title": "x",
                                        "sales_volume": "100+ bought in past month",
                                        "product_price": "$1", "product_num_ratings": 1}]}},
                {"data": {"products": [{"asin": "A1", "product_title": "x", "sales_volume": None,
                                        "product_price": "$1", "product_num_ratings": 1}]}}])
    monkeypatch.setattr(comp, "dry_run_payload", lambda g, c: next(seq))
    rows, _ = comp.collect_group("g", ["a", "b", "c"], "US", 1, None, 0, True, False, False, 24)
    assert len(rows) == 1 and rows[0]["units_past_month"] == 100          # later richer row wins, then is kept


def test_parse_price_rejects_two_decimal_points_but_not_one():
    assert comp.parse_price("1.2.3") is None and comp.parse_price("$1,299.00") == 1299.0


def test_default_pages_sleep_and_windows(monkeypatch, sandbox):
    assert tr.build_parser().parse_args([]).weeks == 52 and tr.build_parser().parse_args([]).window == 4
    seen = []
    monkeypatch.setattr(time, "sleep", seen.append)
    monkeypatch.setenv("OPENWEB_NINJA_API_KEY", "T")
    pages = []
    monkeypatch.setattr(comp, "fetch", lambda q, c, p, k: pages.append(p) or comp.dry_run_payload(q, c))
    assert run(comp, monkeypatch, "--group", "g=moringa oil") == 0
    assert pages == [1, 2] and seen == [1.0, 1.0]                         # defaults: 2 pages, 1 s between calls


def test_opportunity_flag_momentum_thresholds_are_strict():
    base = {"growth_per_period_pct": 1.0, "volatility_cv": 0.1, "trend_fit_r2": 0.9, "yoy_pct": 5.0}
    assert tr.opportunity_flag(dict(base, momentum_pct=10.0)) == "flat"
    assert tr.opportunity_flag(dict(base, momentum_pct=10.1)) == "RISING"
    down = {"growth_per_period_pct": -1.0, "volatility_cv": 0.1, "trend_fit_r2": 0.9, "yoy_pct": -5.0}
    assert tr.opportunity_flag(dict(down, momentum_pct=-10.0)) == "flat"
    assert tr.opportunity_flag(dict(down, momentum_pct=-9.5)) == "flat"
    assert tr.opportunity_flag(dict(down, momentum_pct=-10.1)) == "declining"


def _gate(**kw):
    cand = {"opportunity_flag": "RISING", "observations": 10, "latest_volume": 100, "review_moat": 50,
            "median_price": 25.0, "listings_sampled": 5}
    cand.update(kw)
    return sl.gate(cand, allowed_flags=("RISING",), min_volume=100, min_observations=10,
                   max_review_moat=50, min_median_price=25.0)


def test_gate_thresholds_are_inclusive():
    assert _gate() == []                                   # every value exactly at its threshold passes
    assert len(_gate(observations=9)) == 1 and "9 observations" in _gate(observations=9)[0]
    assert len(_gate(latest_volume=99)) == 1
    assert len(_gate(review_moat=51)) == 1 and "above 50" in _gate(review_moat=51)[0]
    assert len(_gate(median_price=24.99)) == 1


def test_gate_no_competition_data_for_none_or_zero_listings():
    assert _gate(listings_sampled=0) == ["no competition data"]
    assert _gate(listings_sampled=None) == ["no competition data"]
    assert _gate(listings_sampled=1) == []


def test_profiles_run_in_order_b2c_then_b2b(sandbox, monkeypatch, capsys):
    run(sl, monkeypatch, "--dry-run", "--out", "o")
    out = capsys.readouterr().out
    assert out.index(sl.PROFILES["b2c"]["label"]) < out.index(sl.PROFILES["b2b"]["label"])


def test_in_market_volume_scale_applies_to_gates():
    from _markets import resolve_gates
    g = resolve_gates(sl.PROFILES["b2c"], sl.MARKETS["IN"])
    assert g["min_volume"] == max(1.0, round(sl.PROFILES["b2c"]["gates"]["min_volume"] * 0.2))
    assert resolve_gates(sl.PROFILES["b2b"], sl.MARKETS["US"])["min_median_price"] == 25.0


def test_get_json_backoff_doubles_from_two_seconds(monkeypatch):
    import urllib.error

    import _http
    sleeps = []
    calls = []

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": 1}'

    def fake_urlopen(req, timeout):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.HTTPError(req.full_url, 429, "x", {}, None)
        return R()

    monkeypatch.setattr(_http.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(_http.time, "sleep", sleeps.append)
    assert _http.get_json("http://t.test/", {}, timeout=1, retries=3, http_error=lambda c, lbl: lbl,
                          url_error=str) == {"ok": 1}
    assert sleeps == [2, 4]
