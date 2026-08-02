"""Tests for config.cell_config() and its wiring into scan_pending_zones
(entry depth) + filter_by_iv (per-cell IV threshold).

Regression for the 2026-07-08 per-cell OB rollout (finding_tyagach_ob_depth_sweep):
depth_frac/r_target/expiry_days/iv_threshold must be overridable per (tf, kind)
without disturbing kinds/cells that have no CELL_CONFIG entry.

Run: cd tyagach && python3 tests/test_cell_config.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import repo
from services import config, signal_engine, portfolio_state
from services.signal_engine import TriggeredEntry


def _setup_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    repo.DB_PATH = tmp.name
    repo.init_db(2000.0)


def _df_one_bar(ts_ms: int, low: float, high: float):
    import pandas as pd
    mid = (low + high) / 2
    return pd.DataFrame([{
        "ts_ms": ts_ms, "ts": ts_ms, "open": mid, "high": high,
        "low": low, "close": mid, "volume": 1.0,
    }])


def test_cell_config_falls_through_when_no_override():
    # MB/BB have no CELL_CONFIG entries -> must equal ZONE_CONFIG[kind] exactly
    for kind in ("MB", "BB"):
        for tf in ("15m", "30m", "1h", "2h"):
            assert config.cell_config(tf, kind) == config.ZONE_CONFIG[kind]


def test_cell_config_override_merges_not_replaces():
    orig = dict(config.CELL_CONFIG)
    try:
        config.CELL_CONFIG = {("15m", "OB"): {"depth_frac": 0.7}}
        merged = config.cell_config("15m", "OB")
        assert merged["depth_frac"] == 0.7  # overridden
        assert merged["r_target"] == config.ZONE_CONFIG["OB"]["r_target"]  # kept from default
        assert merged["iv_threshold"] == config.ZONE_CONFIG["OB"]["iv_threshold"]
        # a TF with no override still gets the plain kind-level default
        assert config.cell_config("30m", "OB") == config.ZONE_CONFIG["OB"]
    finally:
        config.CELL_CONFIG = orig


def test_only_2h_ob_has_override_populated():
    # 2026-08-02: 15m/30m/1h OB overrides removed (deactivated in ACTIVE_CELLS).
    assert set(config.CELL_CONFIG.keys()) == {("2h", "OB")}
    cfg = config.cell_config("2h", "OB")
    assert {"depth_frac", "r_target", "expiry_days", "iv_threshold"} <= cfg.keys()


def test_scan_pending_zones_uses_cell_depth_frac_not_hardcoded_midpoint():
    """A zone [1500, 1510]: at depth_frac=0.5 (default) entry=1505 (midpoint).
    Override 15m/OB to depth_frac=0.8 -> a bullish zone's entry should sit at
    zhi - 0.8*(zhi-zlo) = 1510 - 8 = 1502, NOT the old hardcoded 1505."""
    _setup_db()
    orig_cells, orig_cell_cfg = config.ACTIVE_CELLS, dict(config.CELL_CONFIG)
    try:
        config.ACTIVE_CELLS = frozenset({("15m", "OB")})
        config.CELL_CONFIG = {("15m", "OB"): {"depth_frac": 0.8}}
        ts0 = 1_750_018_000_000  # 20:26 UTC — outside ENTRY_VETO_UTC_HOURS (12-15h veto, 2026-07-09)
        repo.upsert_zone_signal("15m:OB:bullish:1:1500.000000:1510.000000",
                                 "15m", "OB", "bullish", 1, ts0, 1500.0, 1510.0)
        # bar touches down to 1502 (the depth=0.8 level) but NOT down to 1505
        # (the old midpoint) -- only triggers if depth_frac is actually being read
        df = _df_one_bar(ts0, low=1502.0, high=1510.0)
        triggered = signal_engine.scan_pending_zones(df, "15m")
        assert len(triggered) == 1
        assert triggered[0].entry_price == 1502.0
    finally:
        config.ACTIVE_CELLS = orig_cells
        config.CELL_CONFIG = orig_cell_cfg


def test_scan_pending_zones_default_depth_frac_matches_old_midpoint_behavior():
    """No override for 30m/OB -> must still behave exactly like the old
    hardcoded mid=(zlo+zhi)/2 (backward compatibility for un-migrated cells)."""
    _setup_db()
    orig_cells, orig_cell_cfg = config.ACTIVE_CELLS, dict(config.CELL_CONFIG)
    try:
        config.ACTIVE_CELLS = frozenset({("30m", "OB")})
        config.CELL_CONFIG = {k: v for k, v in orig_cell_cfg.items() if k[0] != "30m"}
        ts0 = 1_750_018_000_000  # 20:26 UTC — outside ENTRY_VETO_UTC_HOURS (12-15h veto, 2026-07-09)
        repo.upsert_zone_signal("30m:OB:bullish:1:1500.000000:1510.000000",
                                 "30m", "OB", "bullish", 1, ts0, 1500.0, 1510.0)
        df = _df_one_bar(ts0, low=1505.0, high=1510.0)  # touches exactly the midpoint
        triggered = signal_engine.scan_pending_zones(df, "30m")
        assert len(triggered) == 1
        assert triggered[0].entry_price == 1505.0
    finally:
        config.ACTIVE_CELLS = orig_cells
        config.CELL_CONFIG = orig_cell_cfg


def test_filter_by_iv_uses_per_cell_threshold():
    """15m/OB overridden to iv_threshold=80 (stricter than the shared 50)
    -> an entry at DVOL=60 must be rejected for 15m but still pass for 30m
    (which has no override and keeps the shared 50 threshold)."""
    orig_cell_cfg = dict(config.CELL_CONFIG)
    try:
        config.CELL_CONFIG = {("15m", "OB"): {"iv_threshold": 80.0}}
        e15 = TriggeredEntry("k1", "15m", "OB", "bullish", 1, 100.0, 99.0)
        e30 = TriggeredEntry("k2", "30m", "OB", "bullish", 1, 100.0, 99.0)
        passed = portfolio_state.filter_by_iv([e15, e30], current_dvol=60.0)
        assert e30 in passed and e15 not in passed
    finally:
        config.CELL_CONFIG = orig_cell_cfg


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"\n{len(tests)} tests passed")
