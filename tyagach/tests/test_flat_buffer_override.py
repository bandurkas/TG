"""Tests for config.FLAT_BUFFER_FRAC_OVERRIDE (2026-08-03 round 2): per-cell
flat buffer fractions for 15m/30m/2h cells found better than both the
0.0015 default and ATR scaling. Cells not listed must keep BUFFER_FRAC.

Run: cd tyagach && python3 -m pytest tests/test_flat_buffer_override.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from db import repo
from services import config, signal_engine


def _setup_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    repo.DB_PATH = tmp.name
    repo.init_db(2000.0)


def test_flat_override_and_atr_mult_keys_stay_disjoint():
    """A cell accidentally added to both dicts would silently break round 1's
    ATR behavior (the else-branch flat lookup never runs for an ATR cell with
    a valid, non-NaN atr value) -- guard the invariant directly."""
    assert set(config.FLAT_BUFFER_FRAC_OVERRIDE) & set(config.ATR_BUFFER_MULT) == set()


def test_overridden_cell_uses_wider_stop_than_default_would_give():
    tf, kind = "30m", "FVG"
    assert (tf, kind) in config.FLAT_BUFFER_FRAC_OVERRIDE
    assert (tf, kind) not in config.ATR_BUFFER_MULT
    zlo, zhi = 1995.0, 2005.0
    default_stop = zlo - config.BUFFER_FRAC * ((zlo + zhi) / 2)
    override_stop = zlo - config.FLAT_BUFFER_FRAC_OVERRIDE[(tf, kind)] * ((zlo + zhi) / 2)
    assert override_stop < default_stop  # override must be wider (further from zone edge)

    _setup_db()
    ts0 = 1_750_000_000_000
    key = f"{tf}:{kind}:bullish:1:1995.000000:2005.000000"
    repo.upsert_zone_signal(key, tf, kind, "bullish", 1, ts0, zlo, zhi)

    trigger_close = default_stop - 1.0  # breaches the (narrower) default, not the override
    df = pd.DataFrame([{"ts_ms": ts0, "ts": ts0, "open": 2000.0, "high": 2000.0,
                        "low": trigger_close - 1.0, "close": trigger_close, "volume": 1.0}])

    triggered = signal_engine.scan_pending_zones(df, tf)
    assert triggered == []
    row = repo.get_pending_zone_signals(tf)
    assert len(row) == 1 and row[0]["zone_key"] == key  # still pending -- override buffer used


def test_cell_without_override_still_uses_default_buffer_frac():
    tf, kind = "15m", "OB"
    assert (tf, kind) not in config.FLAT_BUFFER_FRAC_OVERRIDE
    assert (tf, kind) not in config.ATR_BUFFER_MULT
    zlo, zhi = 1995.0, 2005.0
    default_stop = zlo - config.BUFFER_FRAC * ((zlo + zhi) / 2)

    _setup_db()
    ts0 = 1_750_000_000_000
    key = f"{tf}:{kind}:bullish:1:1995.000000:2005.000000"
    repo.upsert_zone_signal(key, tf, kind, "bullish", 1, ts0, zlo, zhi)

    trigger_close = default_stop - 1.0
    df = pd.DataFrame([{"ts_ms": ts0, "ts": ts0, "open": 2000.0, "high": 2000.0,
                        "low": trigger_close - 1.0, "close": trigger_close, "volume": 1.0}])

    triggered = signal_engine.scan_pending_zones(df, tf)
    assert triggered == []
    row = repo.get_pending_zone_signals(tf)
    assert row == []  # invalidated via the (unchanged) default buffer


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"\n{len(tests)} tests passed")
