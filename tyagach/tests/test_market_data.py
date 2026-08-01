"""Regression for the 2026-08-02 finding: the live loop's zone-formation rate
collapsed from ~5-19/day to ~0-2/day within days of the last container
restart, while re-running the exact same detector on a fresh re-fetch of the
identical real price history found the normal rate — proving the bug was in
the live kline cache, not the market.

Root cause: `get_klines`'s incremental path fetched the latest ~50 bars
(almost always including the still-forming current bar as the newest entry)
and merged them straight into `_kline_cache` without filtering through
`_closed_bars` first. `_merge_into_cache` used to skip any ts_ms already
present — so a bar's ~0-60s-old partial OHLC snapshot (captured the moment
it started forming) got locked in permanently: by the time real wall-clock
time caught up and `_closed_bars` started returning that bar as "closed", a
correct, final-OHLC re-fetch of the same ts_ms was silently dropped because
the key already existed. Swing/order-block/FVG detection depends on accurate
high/low, so an increasing fraction of understated-range bars in the rolling
window quietly starved zone detection over time.

Fix (services/market_data.py, 2026-08-02): (1) `get_klines`'s incremental
path now filters `fresh` through `_closed_bars` before merging — matching
what the cold-start path already did — so a forming bar is never persisted.
(2) `_merge_into_cache` now overwrites by ts_ms (last-write-wins) instead of
skip-if-present, so the cache is self-healing even if a stale value ever
gets in some other way.

Run: cd tyagach && python3 tests/test_market_data.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import config, market_data


def _bar(ts_ms, o, h, l, c, v=1.0):
    return {"ts_ms": ts_ms, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _reset():
    market_data._kline_cache.clear()
    market_data._next_bar_due.clear()


def test_merge_into_cache_overwrites_stale_ts_with_later_fetch():
    """Direct unit test of the merge primitive: a later fetch's bar for a
    ts_ms already in the cache must REPLACE it, not be dropped."""
    _reset()
    tf = "15m"
    partial = _bar(1_000_000_000_000, o=100.0, h=100.1, l=99.9, c=100.0)  # ~0s into formation
    market_data._merge_into_cache(tf, [partial])
    assert market_data._kline_cache[tf][0]["high"] == 100.1

    final = _bar(1_000_000_000_000, o=100.0, h=112.0, l=97.0, c=105.0)  # true final range
    market_data._merge_into_cache(tf, [final])
    assert market_data._kline_cache[tf][0]["high"] == 112.0, (
        "a later fetch's correct OHLC for the same ts_ms must overwrite an earlier "
        "partial snapshot, not be silently dropped"
    )
    assert len(market_data._kline_cache[tf]) == 1


def test_get_klines_incremental_never_persists_a_forming_bar():
    """End-to-end: simulate the exact sequence that broke live trading —
    an incremental get_klines() call whose fetch includes the still-forming
    bar as the newest entry. That forming bar must NOT appear in the
    persisted cache (and therefore must not be returned as "closed" with
    wrong values once real time catches up to it)."""
    _reset()
    tf = "15m"
    bar_ms = config.TIMEFRAMES[tf].bar_ms
    now_ms = int(__import__("time").time() * 1000)
    closed_bar_ts = (now_ms // bar_ms) * bar_ms - bar_ms  # a genuinely closed bar
    forming_bar_ts = closed_bar_ts + bar_ms               # currently forming, NOT closed

    # cold start
    market_data._fetch_klines_paged = lambda interval, limit, end_ms=None: [
        _bar(closed_bar_ts, 100, 101, 99, 100.5),
        _bar(forming_bar_ts, 100.5, 100.6, 100.4, 100.5),  # forming, understated range
    ]
    klines = market_data.get_klines(tf)
    assert [b["ts_ms"] for b in klines] == [closed_bar_ts], "forming bar must be excluded from cold start"

    # force the incremental path on the next call (bypass the due-time skip)
    market_data._next_bar_due[tf] = 0
    market_data._fetch_klines_paged = lambda interval, limit, end_ms=None: [
        _bar(closed_bar_ts, 100, 101, 99, 100.5),
        _bar(forming_bar_ts, 100.5, 100.6, 100.4, 100.5),  # still forming at this fetch too
    ]
    market_data.get_klines(tf)
    assert forming_bar_ts not in {b["ts_ms"] for b in market_data._kline_cache[tf]}, (
        "the incremental path must not persist a bar that was still forming at fetch time"
    )


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    orig_fetch = market_data._fetch_klines_paged
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    market_data._fetch_klines_paged = orig_fetch
    print(f"\n{len(tests)} tests passed")
