"""Tests for portfolio_state.check_trailing_exits (per-position trailing
profit-lock, config.TRAIL_PARAMS) and its DB migration column.

Run: cd tyagach && python3 tests/test_trailing_profit_lock.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import config, portfolio_state as ps


# Fake cell, isolated from live tuning -- these tests must not break every
# time TRAIL_PARAMS gets re-tuned (it already has once). arm_frac=0.5,
# trail_frac=0.5 fixed regardless of what's live for any real cell.
_TEST_TF, _TEST_KIND = "TEST", "FAKE"
config.TRAIL_PARAMS[(_TEST_TF, _TEST_KIND)] = (0.5, 0.5)


def _pos(tf=_TEST_TF, kind=_TEST_KIND, premium=100.0, unrealized=None, peak=0.0, pos_id=1):
    p = {
        "id": pos_id, "timeframe": tf, "zone_kind": kind,
        "sell_premium_received": premium, "trail_peak_usd": peak,
    }
    if unrealized is not None:
        p["unrealized_pnl_usd"] = unrealized
    return p


def test_cell_absent_from_trail_params_never_fires():
    p = _pos(tf="TEST", kind="FAKE_OFF", premium=100.0, unrealized=90.0, peak=0.0)  # not in TRAIL_PARAMS
    exits, updates = ps.check_trailing_exits([p])
    assert exits == []
    assert updates == []


def test_not_armed_yet_tracks_peak_but_no_exit():
    # arm threshold = 0.5 * 100 = 50; unrealized=30 never reaches it
    p = _pos(unrealized=30.0, peak=0.0)
    exits, updates = ps.check_trailing_exits([p])
    assert exits == []
    assert updates == [(1, 30.0)]


def test_armed_no_giveback_tracks_new_peak_no_exit():
    # peak=60 (already armed, >=50); unrealized rises to 70 -> new peak, no giveback
    p = _pos(unrealized=70.0, peak=60.0)
    exits, updates = ps.check_trailing_exits([p])
    assert exits == []
    assert updates == [(1, 70.0)]


def test_armed_and_gives_back_more_than_trail_frac_exits():
    # peak=80 (armed), trail_frac=0.5 -> close threshold = 80*0.5 = 40
    # unrealized drops to 35 <= 40 -> trail exit
    p = _pos(unrealized=35.0, peak=80.0)
    exits, updates = ps.check_trailing_exits([p])
    assert len(exits) == 1 and exits[0].exit_reason == "trail"
    assert updates == []


def test_armed_but_giveback_within_tolerance_no_exit():
    # peak=80, threshold=40, unrealized=45 > 40 -> still holding, no exit
    p = _pos(unrealized=45.0, peak=80.0)
    exits, updates = ps.check_trailing_exits([p])
    assert exits == []
    # unrealized(45) < peak(80) so peak doesn't move -> no update needed
    assert updates == []


def test_peak_never_decreases_below_stored_value():
    # unrealized dips well below stored peak -- peak must stay at stored value
    p = _pos(unrealized=-10.0, peak=20.0)  # arm threshold 50, peak stays 20 < 50 -> not armed
    exits, updates = ps.check_trailing_exits([p])
    assert exits == []
    assert updates == []  # peak unchanged (max(20, -10) == 20), nothing to persist


def test_missing_unrealized_pnl_is_skipped():
    p = _pos(unrealized=None, peak=10.0)
    exits, updates = ps.check_trailing_exits([p])
    assert exits == []
    assert updates == []


def test_exact_giveback_boundary_exits():
    # peak=80, trail_frac=0.5 -> threshold exactly 40; unrealized==40 -> <=, exits
    p = _pos(unrealized=40.0, peak=80.0)
    exits, updates = ps.check_trailing_exits([p])
    assert len(exits) == 1 and exits[0].exit_reason == "trail"


def test_multiple_positions_independent():
    armed_and_exits = _pos(pos_id=1, unrealized=35.0, peak=80.0)
    still_running = _pos(pos_id=2, unrealized=70.0, peak=60.0)
    off_cell = _pos(pos_id=3, tf="TEST", kind="FAKE_OFF", unrealized=90.0, peak=0.0)
    exits, updates = ps.check_trailing_exits([armed_and_exits, still_running, off_cell])
    assert len(exits) == 1 and exits[0].position["id"] == 1
    assert updates == [(2, 70.0)]


# ── DB migration: trail_peak_usd column ─────────────────────────────────────


def test_migration_adds_trail_peak_column():
    import sqlite3
    import tempfile
    import db.repo as repo

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.unlink(tmp.name)
    conn = sqlite3.connect(tmp.name)
    conn.execute(
        "CREATE TABLE positions (id INTEGER PRIMARY KEY, zone_key TEXT, zone_kind TEXT, "
        "direction TEXT, option_side TEXT, symbol TEXT, strike REAL, entry_ts_ms INTEGER, "
        "entry_spot REAL, stop_price REAL, tp_price REAL, expiry_ts_ms INTEGER, iv_entry REAL, "
        "num_units REAL, notional REAL, sell_premium_received REAL, open_fee REAL, "
        "open_order_id TEXT, status TEXT, created_at_ms INTEGER)"
    )
    conn.execute("CREATE TABLE bot_state (id INTEGER)")
    conn.execute("CREATE TABLE zone_signals (zone_key TEXT)")
    repo._ensure_columns(conn)
    repo._ensure_columns(conn)  # idempotent, must not raise
    cols = {r[1] for r in conn.execute("PRAGMA table_info(positions)")}
    assert "trail_peak_usd" in cols
    conn.close()
    os.unlink(tmp.name)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"\n{len(tests)} tests passed")
