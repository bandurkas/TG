"""Tests for portfolio_state per-TF exit rules and decide_entries conflict logic.

Run: cd tyagach && python3 tests/test_per_tf_exits.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import portfolio_state as ps
from services.signal_engine import TriggeredEntry


def _pos(direction="bullish", zone_kind="OB", stop_price=1400.0, tp_price=1700.0,
         expiry_ts_ms=9_999_999_999_999, timeframe="15m"):
    return {
        "id": 1, "direction": direction, "zone_kind": zone_kind,
        "stop_price": stop_price, "tp_price": tp_price,
        "expiry_ts_ms": expiry_ts_ms, "timeframe": timeframe,
        "num_units": 0.1, "sell_premium_received": 50.0, "open_fee": 0.06,
        "notional": 200.0, "symbol": "ETH-X-P-USDT",
    }


def _entry(direction="bullish", kind="OB", ts_ms=1_000_000):
    mid = 1505.0
    return TriggeredEntry(
        zone_key=f"15m:{kind}:{direction}:{ts_ms}:1500.000000:1510.000000",
        timeframe="15m", kind=kind, direction=direction,
        entry_ts_ms=ts_ms, entry_price=mid, stop_price=mid - 50.0,
    )


# ── check_exits ───────────────────────────────────────────────────────────────


def test_bullish_sl_hit():
    exits = ps.check_exits([_pos(direction="bullish", stop_price=1400.0)],
                            latest_high=1500.0, latest_low=1390.0, now_ts_ms=1000)
    assert len(exits) == 1 and exits[0].exit_reason == "sl"


def test_bullish_tp_hit():
    exits = ps.check_exits([_pos(direction="bullish", tp_price=1700.0)],
                            latest_high=1710.0, latest_low=1550.0, now_ts_ms=1000)
    assert len(exits) == 1 and exits[0].exit_reason == "tp"


def test_bearish_sl_hit_from_above():
    exits = ps.check_exits([_pos(direction="bearish", stop_price=1600.0)],
                            latest_high=1610.0, latest_low=1550.0, now_ts_ms=1000)
    assert len(exits) == 1 and exits[0].exit_reason == "sl"


def test_bearish_tp_hit_from_below():
    # SL for bearish is above entry (stop_price=1600); bar must stay below that
    exits = ps.check_exits([_pos(direction="bearish", stop_price=1600.0, tp_price=1300.0)],
                            latest_high=1490.0, latest_low=1290.0, now_ts_ms=1000)
    assert len(exits) == 1 and exits[0].exit_reason == "tp"


def test_sl_takes_priority_over_tp_in_same_bar():
    # both SL and TP struck in same bar: SL wins (checked first)
    exits = ps.check_exits([_pos(direction="bullish", stop_price=1400.0, tp_price=1700.0)],
                            latest_high=1750.0, latest_low=1390.0, now_ts_ms=1000)
    assert len(exits) == 1 and exits[0].exit_reason == "sl"


def test_expiry_exit():
    exits = ps.check_exits([_pos(expiry_ts_ms=1000)],
                            latest_high=1500.0, latest_low=1490.0, now_ts_ms=2000)
    assert len(exits) == 1 and exits[0].exit_reason == "expiry"


def test_no_exit_when_bar_is_safe():
    exits = ps.check_exits([_pos(stop_price=1400.0, tp_price=1700.0,
                                   expiry_ts_ms=9_999_999_999_999)],
                            latest_high=1550.0, latest_low=1450.0, now_ts_ms=1000)
    assert exits == []


# ── check_expiry_only ─────────────────────────────────────────────────────────


def test_check_expiry_only_returns_only_expired():
    p_exp = _pos(expiry_ts_ms=500)
    p_live = _pos(expiry_ts_ms=9_999_999_999_999)
    exits = ps.check_expiry_only([p_exp, p_live], now_ts_ms=1000)
    assert len(exits) == 1 and exits[0].position is p_exp


def test_check_expiry_only_empty_when_none_expired():
    assert ps.check_expiry_only([_pos(expiry_ts_ms=9_999_999_999_999)], now_ts_ms=1000) == []


# ── decide_entries ────────────────────────────────────────────────────────────


def test_same_direction_blocked_within_tf():
    entry = _entry(direction="bullish")
    tf_open = [{"direction": "bullish", "zone_kind": "OB"}]
    decisions = ps.decide_entries([entry], 2000.0,
                                   tf_open_positions=tf_open,
                                   all_open_positions=tf_open,
                                   current_dvol=65.0)
    assert decisions == []


def test_opposite_direction_allowed_within_tf():
    entry = _entry(direction="bullish")
    tf_open = [{"direction": "bearish", "zone_kind": "OB"}]
    decisions = ps.decide_entries([entry], 2000.0,
                                   tf_open_positions=tf_open,
                                   all_open_positions=tf_open,
                                   current_dvol=65.0)
    assert len(decisions) == 1


def test_global_slot_ceiling_blocks_all_entries():
    from services import config
    entries = [_entry(direction="bullish", ts_ms=1_000_000 + i) for i in range(3)]
    all_open = [{"direction": "bearish", "zone_kind": "OB", "notional": 0.0}
                for _ in range(config.MAX_OPEN_TOTAL_GLOBAL)]
    decisions = ps.decide_entries(entries, 2000.0,
                                   tf_open_positions=[],
                                   all_open_positions=all_open,
                                   current_dvol=65.0)
    assert decisions == []


def test_per_tf_conflict_does_not_block_different_tf():
    # same-direction conflict is scoped to TF; a bullish open in 30m sub-book
    # must NOT block a bullish entry in 15m sub-book.
    # This tests that tf_open_positions (the 15m book) is empty, even though
    # all_open_positions has a bullish 30m position.
    entry_15m = _entry(direction="bullish")  # 15m entry
    tf_15m_open: list[dict] = []  # 15m sub-book: empty
    all_open = [{"direction": "bullish", "zone_kind": "OB", "notional": 0.0}]  # 30m pos
    decisions = ps.decide_entries([entry_15m], 2000.0,
                                   tf_open_positions=tf_15m_open,
                                   all_open_positions=all_open,
                                   current_dvol=65.0)
    assert len(decisions) == 1  # 15m entry goes through despite 30m bullish open


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"\n{len(tests)} tests passed")
