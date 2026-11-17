"""Characterisation tests: dry-run fixtures captured before the pass-3 module split."""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "amazon_selector"))

import competition as c  # noqa: E402
import trends as t  # noqa: E402

GOLDEN = {
    "c|honey|IN": "f906a185667eace2c5cb3442ec1def3d",
    "c|honey|JP": "f906a185667eace2c5cb3442ec1def3d",
    "c|honey|US": "f906a185667eace2c5cb3442ec1def3d",
    "c|moringa oil|IN": "3fe9bcf801aa345ae7855b03a821108f",
    "c|moringa oil|JP": "3fe9bcf801aa345ae7855b03a821108f",
    "c|moringa oil|US": "3fe9bcf801aa345ae7855b03a821108f",
    "c|shilajit x|IN": "1c2c47cece9057d5006c8c95a741b1b5",
    "c|shilajit x|JP": "1c2c47cece9057d5006c8c95a741b1b5",
    "c|shilajit x|US": "1c2c47cece9057d5006c8c95a741b1b5",
    "c|turmeric soap|IN": "98f5677c7d155fc2849d7d80bf1886cb",
    "c|turmeric soap|JP": "98f5677c7d155fc2849d7d80bf1886cb",
    "c|turmeric soap|US": "98f5677c7d155fc2849d7d80bf1886cb",
    "c|zzz unnamed|IN": "0f4c22bbfb8bdb1eaaadf4970c92f3f2",
    "c|zzz unnamed|JP": "a71d6f716b88653a1d1259745c8ff19e",
    "c|zzz unnamed|US": "494182f386aef5ec96e9654e78fda34f",
    "t|alpha": "dde1f34b7c6ae68443e921c6b5600299",
    "t|beta": "50c9c2612b1f4b65d1adff5ea7e81c7d",
    "t|gamma": "799f637bbdae402892b437dd519b1a1c",
    "t|honey": "671971dd32939cd7a2ecd70e236e3214",
    "t|moringa": "f096bbb6b88bcae0e94ea9a0ab74dc9c",
    "t|mouth tape": "766f745b077f69d3f5ac8fafedca320e",
    "t|shilajit": "082b433f2a3df39c1b1960bbad249ea0",
    "t|sleep gummies": "255cdf9ee27b04e38ae5b2e8f41e91b8",
    "t|turmeric soap": "59fc8d71d56fb77e175ba8f69c0f4cf5"
}


def _md5(obj):
    return hashlib.md5(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def test_competition_dry_run_payloads_are_unchanged():
    for key, want in GOLDEN.items():
        kind, *rest = key.split("|")
        if kind == "c":
            assert _md5(c.dry_run_payload(rest[0], rest[1])) == want, key


def test_trends_dry_run_payloads_are_unchanged():
    for key, want in GOLDEN.items():
        kind, *rest = key.split("|")
        if kind == "t":
            assert _md5(t.dry_run_payload(rest[0])) == want, key


def test_split_modules_reexport_the_old_names():
    import shortlist as s
    assert callable(s.write_csv) and callable(s.write_markdown)
    assert s.market_for("IN")["currency"] == "INR" and "US" in s.MARKETS
    assert callable(t.write_csv) and callable(t.write_markdown)
    assert callable(c.write_csv)
