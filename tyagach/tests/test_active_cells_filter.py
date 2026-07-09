"""Tests for scan_pending_zones respecting config.ACTIVE_CELLS at trigger time,
not just at zone-creation time (sync_new_zones already did this correctly).

Regression for: a (tf, kind) cell removed from ACTIVE_CELLS after a zone_signal
row was already stored as 'pending' must stop being evaluated — otherwise a
live config change silently fails to affect signals created before the change.

Run: cd tyagach && python3 tests/test_active_cells_filter.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import repo
from services import config, signal_engine


def _setup_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    repo.DB_PATH = tmp.name
    repo.init_db(2000.0)


def _df_one_bar(ts_ms: int, price: float = 1505.0):
    import pandas as pd
    return pd.DataFrame([{
        "ts_ms": ts_ms, "ts": ts_ms, "open": price, "high": price,
        "low": price, "close": price, "volume": 1.0,
    }])


def test_inactive_cell_pending_signal_is_expired_not_triggered():
    _setup_db()
    orig_cells = config.ACTIVE_CELLS
    try:
        config.ACTIVE_CELLS = frozenset({("15m", "MB")})  # OB not active
        ts0 = 1_750_018_000_000  # 20:26 UTC — outside ENTRY_VETO_UTC_HOURS (12-15h veto, 2026-07-09)
        repo.upsert_zone_signal("15m:OB:bullish:1:1500.000000:1510.000000",
                                 "15m", "OB", "bullish", 1, ts0, 1500.0, 1510.0)
        triggered = signal_engine.scan_pending_zones(_df_one_bar(ts0), "15m")
        assert triggered == []
        pending_after = repo.get_pending_zone_signals("15m")
        assert pending_after == []  # expired, not left dangling as pending
    finally:
        config.ACTIVE_CELLS = orig_cells


def test_active_cell_pending_signal_still_evaluated():
    _setup_db()
    orig_cells = config.ACTIVE_CELLS
    try:
        config.ACTIVE_CELLS = frozenset({("15m", "OB")})
        ts0 = 1_750_018_000_000  # 20:26 UTC — outside ENTRY_VETO_UTC_HOURS (12-15h veto, 2026-07-09)
        zlo, zhi = 1500.0, 1510.0
        # Price touches this cell's configured entry depth (not necessarily the
        # 50% midpoint -- 15m/OB has its own CELL_CONFIG override since
        # 2026-07-08) immediately -- should trigger, not expire.
        depth_frac = config.cell_config("15m", "OB")["depth_frac"]
        entry_level = zhi - depth_frac * (zhi - zlo)  # bullish zone formula
        repo.upsert_zone_signal("15m:OB:bullish:1:1500.000000:1510.000000",
                                 "15m", "OB", "bullish", 1, ts0, zlo, zhi)
        triggered = signal_engine.scan_pending_zones(_df_one_bar(ts0, price=entry_level), "15m")
        assert len(triggered) == 1
        assert triggered[0].kind == "OB"
    finally:
        config.ACTIVE_CELLS = orig_cells


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"\n{len(tests)} tests passed")
