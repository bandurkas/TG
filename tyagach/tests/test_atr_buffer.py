"""Tests for the ATR(14)-scaled SL buffer (2026-08-03, SL_BUFFER_HANDOFF_2026-08-02.md
+ same-day combo-check follow-up): config.ATR_BUFFER_MULT scopes the replacement
of the flat BUFFER_FRAC stop to ("1h","OB") and ("1h","FVG") only -- every other
active cell must keep using the flat buffer unchanged, and a cell in
ATR_BUFFER_MULT must fall back to the flat buffer if its ATR isn't warmed up yet
(cold start, ATR_PERIOD=14 bars).

Run: cd tyagach && python3 -m pytest tests/test_atr_buffer.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest

from db import repo
from services import config, signal_engine


def _setup_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    repo.DB_PATH = tmp.name
    repo.init_db(2000.0)


def _flat_bar(ts_ms, close=2000.0, rng=4.0):
    return {"ts_ms": ts_ms, "ts": ts_ms, "open": close, "high": close + rng / 2,
            "low": close - rng / 2, "close": close, "volume": 1.0}


def test_atr_series_matches_manual_wilder_tr():
    highs = [2002.0] * 20
    lows = [1998.0] * 20
    closes = [2000.0] * 20
    atr = signal_engine.atr_series(highs, lows, closes, period=14)
    assert len(atr) == 20
    assert pd.isna(atr[:13]).all()  # not warmed up yet (min_periods=14)
    assert atr[13] == pytest.approx(4.0)
    assert atr[19] == pytest.approx(4.0)


@pytest.mark.parametrize("tf,kind", [("1h", "OB"), ("1h", "FVG")])
def test_atr_buffer_cell_survives_a_close_that_would_invalidate_under_flat_buffer(tf, kind):
    """Every cell in config.ATR_BUFFER_MULT -- once its ATR is warmed up, the
    stop must come from atr_mult * ATR(14), not BUFFER_FRAC. Construct a close
    that breaches the (tight) flat stop but not the (wider) ATR stop, and
    confirm the zone stays pending instead of invalidating."""
    _setup_db()
    assert (tf, kind) in config.ATR_BUFFER_MULT
    bar_ms = config.TIMEFRAMES[tf].bar_ms
    zlo, zhi = 1995.0, 2005.0
    ts0 = 1_750_000_000_000
    valid_from_ts_ms = ts0 + 13 * bar_ms
    key = f"{tf}:{kind}:bullish:1:1995.000000:2005.000000"
    repo.upsert_zone_signal(key, tf, kind, "bullish", 1, valid_from_ts_ms, zlo, zhi)

    flat_stop = zlo - config.BUFFER_FRAC * ((zlo + zhi) / 2)
    trigger_close = flat_stop - 1.0
    bars = [_flat_bar(ts0 + i * bar_ms) for i in range(13)]
    bars.append({"ts_ms": valid_from_ts_ms, "ts": valid_from_ts_ms, "open": 2000.0,
                 "high": 2000.0, "low": trigger_close - 1.0, "close": trigger_close, "volume": 1.0})
    df = pd.DataFrame(bars)

    atr = signal_engine.atr_series(df["high"].values, df["low"].values, df["close"].values)
    atr_stop = zlo - config.ATR_BUFFER_MULT[(tf, kind)] * atr[13]
    # sanity: the scenario must actually differentiate the two buffers
    assert trigger_close < flat_stop
    assert trigger_close > atr_stop

    triggered = signal_engine.scan_pending_zones(df, tf)
    assert triggered == []
    row = repo.get_pending_zone_signals(tf)
    assert len(row) == 1 and row[0]["zone_key"] == key  # still pending -- ATR buffer used, not flat


def test_non_atr_cell_still_uses_flat_buffer_and_invalidates():
    """Control: a cell NOT in config.ATR_BUFFER_MULT (e.g. 15m/OB) must be
    unaffected by this change -- same relative close still invalidates."""
    _setup_db()
    tf, kind = "15m", "OB"
    assert (tf, kind) not in config.ATR_BUFFER_MULT
    zlo, zhi = 1995.0, 2005.0
    ts0 = 1_750_000_000_000
    key = f"{tf}:{kind}:bullish:1:1995.000000:2005.000000"
    repo.upsert_zone_signal(key, tf, kind, "bullish", 1, ts0, zlo, zhi)

    flat_stop = zlo - config.BUFFER_FRAC * ((zlo + zhi) / 2)
    trigger_close = flat_stop - 1.0
    df = pd.DataFrame([{"ts_ms": ts0, "ts": ts0, "open": 2000.0, "high": 2000.0,
                        "low": trigger_close - 1.0, "close": trigger_close, "volume": 1.0}])

    triggered = signal_engine.scan_pending_zones(df, tf)
    assert triggered == []
    row = repo.get_pending_zone_signals(tf)
    assert row == []  # invalidated via the (unchanged) flat buffer


def test_atr_cell_falls_back_to_flat_buffer_when_atr_not_warmed_up():
    """(1h, OB) is ATR-buffered, but with fewer than ATR_PERIOD bars available
    atr_series returns NaN -- scan_pending_zones must fall back to the flat
    buffer rather than leave the zone without a usable stop."""
    _setup_db()
    tf, kind = "1h", "OB"
    zlo, zhi = 1995.0, 2005.0
    ts0 = 1_750_000_000_000
    key = f"{tf}:{kind}:bullish:1:1995.000000:2005.000000"
    repo.upsert_zone_signal(key, tf, kind, "bullish", 1, ts0, zlo, zhi)

    flat_stop = zlo - config.BUFFER_FRAC * ((zlo + zhi) / 2)
    trigger_close = flat_stop - 1.0
    # only one bar in the window -- rolling(14, min_periods=14) atr is NaN here
    df = pd.DataFrame([{"ts_ms": ts0, "ts": ts0, "open": 2000.0, "high": 2000.0,
                        "low": trigger_close - 1.0, "close": trigger_close, "volume": 1.0}])

    triggered = signal_engine.scan_pending_zones(df, tf)
    assert triggered == []
    row = repo.get_pending_zone_signals(tf)
    assert row == []  # invalidated via flat-buffer fallback, not stuck pending on a NaN atr


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"\n{len(tests)} tests passed")
