"""Tests for the rejection-close entry filter (2026-08-02,
RESEARCH_FINDINGS_2026-08-02.md U1): a bar that wicks through entry_level
but closes back on the UNFAVORABLE side is a fakeout, not a real touch --
scan_pending_zones must keep scanning instead of triggering on it.

Independent review (2026-08-02) confirmed the control-flow logic is sound
but flagged that no test exercised the new "touch but no reject" path
directly, nor the same-bar stop-breach-vs-non-rejected-touch priority.
This file closes that gap.

Run: cd tyagach && python3 -m pytest tests/test_rejection_close_entry.py -q
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


def _bar(ts_ms, o, h, l, c):
    return {"ts_ms": ts_ms, "ts": ts_ms, "open": o, "high": h, "low": l, "close": c, "volume": 1.0}


def _bullish_zone(zone_key, ts0, zlo=1500.0, zhi=1510.0):
    repo.upsert_zone_signal(zone_key, "15m", "OB", "bullish", 1, ts0, zlo, zhi)


def test_touch_without_reject_does_not_trigger_and_stays_pending(monkeypatch):
    _setup_db()
    monkeypatch.setattr(config, "ACTIVE_CELLS", config.ACTIVE_CELLS | {("15m", "OB")})
    zlo, zhi = 1500.0, 1510.0
    depth_frac = config.cell_config("15m", "OB")["depth_frac"]
    entry_level = zhi - depth_frac * (zhi - zlo)
    ts0 = 1_750_018_000_000  # 20:26 UTC, outside the entry-veto window
    key = "15m:OB:bullish:1:1500.000000:1510.000000"
    _bullish_zone(key, ts0, zlo, zhi)

    # wicks down to entry_level but closes BELOW it -- a fakeout, not a
    # rejection (bullish rejection requires close > entry_level)
    df = pd.DataFrame([_bar(ts0, entry_level - 0.02, entry_level + 0.01,
                             entry_level - 0.02, entry_level - 0.01)])
    triggered = signal_engine.scan_pending_zones(df, "15m")
    assert triggered == []
    row = repo.get_pending_zone_signals("15m")
    assert len(row) == 1 and row[0]["zone_key"] == key  # still pending, not expired/invalidated


def test_scanning_resumes_and_binds_to_the_later_rejecting_bar(monkeypatch):
    """After a non-rejecting touch, a later bar that DOES reject must
    trigger, with touch_ts_ms bound to that later bar, not the first one --
    proves the `continue` correctly resumes scanning instead of getting
    stuck or double-counting."""
    _setup_db()
    monkeypatch.setattr(config, "ACTIVE_CELLS", config.ACTIVE_CELLS | {("15m", "OB")})
    zlo, zhi = 1500.0, 1510.0
    depth_frac = config.cell_config("15m", "OB")["depth_frac"]
    entry_level = zhi - depth_frac * (zhi - zlo)
    ts0 = 1_750_018_000_000
    bar_ms = 15 * 60_000
    key = "15m:OB:bullish:1:1500.000000:1510.000000"
    _bullish_zone(key, ts0, zlo, zhi)

    fakeout = _bar(ts0, entry_level - 0.02, entry_level + 0.01, entry_level - 0.02, entry_level - 0.01)
    real_touch = _bar(ts0 + bar_ms, entry_level + 0.05, entry_level + 0.05,
                       entry_level - 0.01, entry_level + 0.05)
    df = pd.DataFrame([fakeout, real_touch])
    triggered = signal_engine.scan_pending_zones(df, "15m")
    assert len(triggered) == 1
    assert triggered[0].entry_ts_ms == ts0 + bar_ms  # bound to the rejecting bar, not the fakeout


def test_same_bar_stop_breach_invalidates_even_without_a_confirmed_touch(monkeypatch):
    """A bar that both fails to confirm a rejection AND closes beyond
    stop_price must still invalidate the zone -- the stop check runs before
    touched/rejected are even evaluated, so this must take priority over
    "keep scanning" regardless of the touch outcome."""
    _setup_db()
    monkeypatch.setattr(config, "ACTIVE_CELLS", config.ACTIVE_CELLS | {("15m", "OB")})
    zlo, zhi = 1500.0, 1510.0
    ts0 = 1_750_018_000_000
    key = "15m:OB:bullish:1:1500.000000:1510.000000"
    _bullish_zone(key, ts0, zlo, zhi)

    buf = config.BUFFER_FRAC * ((zlo + zhi) / 2)
    stop_price = zlo - buf
    # closes well below stop_price -- invalidation must fire regardless of
    # where entry_level sits relative to this bar's low/close
    df = pd.DataFrame([_bar(ts0, zlo, zlo, stop_price - 1.0, stop_price - 1.0)])
    triggered = signal_engine.scan_pending_zones(df, "15m")
    assert triggered == []
    row = repo.get_pending_zone_signals("15m")
    assert row == []  # invalidated, not left pending


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"\n{len(tests)} tests passed")
