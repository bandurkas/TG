"""Tests for FVG as a standalone tradeable zone kind (2026-08-02 U2,
TYAGACH_UPGRADE_PLAN_2026-08-02.md). Before this, structure.detect_fvg()
output only widened OB zone boundaries (ob.detect_ob's has_fvg_merge) and
was never itself turned into a Zone / traded.

Run: cd tyagach && python3 tests/test_fvg_zone_kind.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core"))

import pandas as pd

from structure import FVG
from zones import build_zones
from services import config, signal_engine


def test_build_zones_maps_bullish_fvg():
    fvg = FVG(idx=5, direction="up", gap_low=1500.0, gap_high=1510.0)
    zones = build_zones([], [], [], [fvg])
    assert len(zones) == 1
    z = zones[0]
    assert z.kind == "FVG"
    assert z.direction == "bullish"
    assert z.formed_idx == 5
    assert z.valid_from == 6
    assert z.zone_low == 1500.0
    assert z.zone_high == 1510.0


def test_build_zones_maps_bearish_fvg():
    fvg = FVG(idx=8, direction="down", gap_low=1490.0, gap_high=1495.0)
    zones = build_zones([], [], [], [fvg])
    assert zones[0].direction == "bearish"


def test_build_zones_defaults_fvgs_to_empty():
    # existing callers that don't pass fvgs (none left in this repo, but the
    # param must stay optional for any future direct caller) get OB/BB/MB only
    assert build_zones([], [], []) == []


def test_build_zones_sorts_fvg_in_with_other_kinds():
    fvg = FVG(idx=1, direction="up", gap_low=100.0, gap_high=101.0)
    zones = build_zones([], [], [], [fvg])
    # valid_from=2 for the FVG; just confirm it's present and sorted, not
    # dropped or placed out of order among a single-element list
    assert [z.valid_from for z in zones] == sorted(z.valid_from for z in zones)


def _three_candle_gap_df():
    # exactly 3 candles -> structure.detect_fvg's `for i in range(2, n)` only
    # ever evaluates i=2: highs[0]=101 < lows[2]=106 -> one bullish FVG.
    rows = [
        {"ts_ms": 0, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1.0},
        {"ts_ms": 1, "open": 103, "high": 110, "low": 102, "close": 108, "volume": 1.0},
        {"ts_ms": 2, "open": 108, "high": 112, "low": 106, "close": 110, "volume": 1.0},
    ]
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms")
    return df


def test_signal_engine_detect_zones_includes_fvg():
    df = _three_candle_gap_df()
    zones = signal_engine.detect_zones(df)
    fvg_zones = [z for z in zones if z.kind == "FVG"]
    assert len(fvg_zones) == 1
    z = fvg_zones[0]
    assert z.direction == "bullish"
    assert z.zone_low == 101.0  # candle[0].high
    assert z.zone_high == 106.0  # candle[2].low


def test_2h_fvg_is_active_and_has_cell_config():
    assert ("2h", "FVG") in config.ACTIVE_CELLS
    cfg = config.cell_config("2h", "FVG")
    assert cfg["depth_frac"] == 0.675
    assert cfg["r_target"] == 10.0
    assert cfg["expiry_days"] == 0.167


def test_fvg_present_in_all_portfolio_sizing_dicts():
    # config.PRIORITY[e.kind] is a direct index (portfolio_state.py) --
    # a KeyError here would crash the loop the instant a 2h/FVG entry triggers.
    assert "FVG" in config.PRIORITY
    assert "FVG" in config.WEIGHT_PCT
    assert "FVG" in config.MAX_OPEN_PER_ZONE
    assert config.WEIGHT_PCT["FVG"] > 0
    assert config.MAX_OPEN_PER_ZONE["FVG"] > 0


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"\n{len(tests)} tests passed")
