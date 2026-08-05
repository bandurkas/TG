"""Tests for the 2026-08-05 strategy reset (SESSION_HANDOFF_2026-08-05_STRATEGY_RESET.md):
  1. zone_signals.expire_reason distinguishes the 4 causes that all previously
     collapsed into the single 'expired' status (lookahead_timeout,
     stale_touch, entry_veto_hour, inactive_cell) -- added specifically to
     diagnose why live fires ~16x fewer entries than a backtest replay of the
     same config over the same calendar window predicts
     (src/overfit_capacity_check.py).
  2. portfolio_state sizing is capped at config.SIZING_BALANCE_CAP regardless
     of how large the tracked balance grows.

Run: cd tyagach && python3 -m pytest tests/test_strategy_reset_2026_08_05.py -q
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from db import repo
from services import config, signal_engine, portfolio_state


def _setup_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    repo.DB_PATH = tmp.name
    repo.init_db(2000.0)


def _force_15m_ob_active(monkeypatch):
    monkeypatch.setattr(config, "ACTIVE_CELLS", config.ACTIVE_CELLS | {("15m", "OB")})


def _ts_at_hour(day: int, hour: int) -> int:
    return int(dt.datetime(2026, 7, day, hour, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)


def _entry_level_15m_ob() -> float:
    depth_frac = config.cell_config("15m", "OB")["depth_frac"]
    zlo, zhi = 1500.0, 1510.0
    return zhi - depth_frac * (zhi - zlo)


def _reason(key: str):
    conn = sqlite3.connect(repo.DB_PATH)
    row = conn.execute("SELECT status, expire_reason FROM zone_signals WHERE zone_key=?", (key,)).fetchone()
    conn.close()
    return row


# ------------------------------------------------------------- expire_reason


def test_lookahead_timeout_reason(monkeypatch):
    _setup_db()
    _force_15m_ob_active(monkeypatch)
    max_lookahead = config.TIMEFRAMES["15m"].max_lookahead
    ts0 = _ts_at_hour(1, 0)
    key = f"15m:OB:bullish:{ts0}:1500.000000:1510.000000"
    repo.upsert_zone_signal(key, "15m", "OB", "bullish", ts0, ts0, 1500.0, 1510.0)
    # price stays flat well above both entry_level and the zone -- never
    # touches, never invalidates -- for the full lookahead window + 1
    n = max_lookahead + 1
    bars = [{"ts_ms": ts0 + i * 900_000, "ts": ts0 + i * 900_000,
              "open": 1520.0, "high": 1520.0, "low": 1520.0, "close": 1520.0, "volume": 1.0}
            for i in range(n)]
    trig = signal_engine.scan_pending_zones(pd.DataFrame(bars), "15m")
    assert trig == []
    status, reason = _reason(key)
    assert status == "expired" and reason == "lookahead_timeout"


def test_stale_touch_reason(monkeypatch):
    _setup_db()
    _force_15m_ob_active(monkeypatch)
    stale_after = config.TIMEFRAMES["15m"].stale_after
    ts0 = _ts_at_hour(1, 0)
    key = f"15m:OB:bullish:{ts0}:1500.000000:1510.000000"
    repo.upsert_zone_signal(key, "15m", "OB", "bullish", ts0, ts0, 1500.0, 1510.0)
    entry_level = _entry_level_15m_ob()
    # bar 0: touches and rejects (low<=entry_level, close>entry_level)
    bars = [{"ts_ms": ts0, "ts": ts0, "open": entry_level + 0.05, "high": entry_level + 0.05,
             "low": entry_level - 0.01, "close": entry_level + 0.05, "volume": 1.0}]
    # enough trailing bars that (n-1-0) > stale_after by the time this scan runs
    for i in range(1, stale_after + 3):
        ts_i = ts0 + i * 900_000
        bars.append({"ts_ms": ts_i, "ts": ts_i, "open": 1520.0, "high": 1520.0,
                     "low": 1520.0, "close": 1520.0, "volume": 1.0})
    trig = signal_engine.scan_pending_zones(pd.DataFrame(bars), "15m")
    assert trig == []
    status, reason = _reason(key)
    assert status == "expired" and reason == "stale_touch"


def test_entry_veto_hour_reason(monkeypatch):
    _setup_db()
    _force_15m_ob_active(monkeypatch)
    ts0 = _ts_at_hour(9, 13)  # inside default 12-15h veto
    key = f"15m:OB:bullish:{ts0}:1500.000000:1510.000000"
    repo.upsert_zone_signal(key, "15m", "OB", "bullish", ts0, ts0, 1500.0, 1510.0)
    entry_level = _entry_level_15m_ob()
    df = pd.DataFrame([{
        "ts_ms": ts0, "ts": ts0, "open": entry_level + 0.05, "high": entry_level + 0.05,
        "low": entry_level - 0.01, "close": entry_level + 0.05, "volume": 1.0,
    }])
    trig = signal_engine.scan_pending_zones(df, "15m")
    assert trig == []
    status, reason = _reason(key)
    assert status == "expired" and reason == "entry_veto_hour"


def test_inactive_cell_reason():
    _setup_db()
    orig_cells = config.ACTIVE_CELLS
    try:
        config.ACTIVE_CELLS = frozenset({("15m", "MB")})  # OB not active
        ts0 = _ts_at_hour(9, 20)
        key = f"15m:OB:bullish:{ts0}:1500.000000:1510.000000"
        repo.upsert_zone_signal(key, "15m", "OB", "bullish", ts0, ts0, 1500.0, 1510.0)
        df = pd.DataFrame([{"ts_ms": ts0, "ts": ts0, "open": 1505.0, "high": 1505.0,
                            "low": 1505.0, "close": 1505.0, "volume": 1.0}])
        trig = signal_engine.scan_pending_zones(df, "15m")
        assert trig == []
        status, reason = _reason(key)
        assert status == "expired" and reason == "inactive_cell"
    finally:
        config.ACTIVE_CELLS = orig_cells


def test_triggered_signal_has_no_expire_reason(monkeypatch):
    _setup_db()
    _force_15m_ob_active(monkeypatch)
    ts0 = _ts_at_hour(9, 20)  # outside veto
    key = f"15m:OB:bullish:{ts0}:1500.000000:1510.000000"
    repo.upsert_zone_signal(key, "15m", "OB", "bullish", ts0, ts0, 1500.0, 1510.0)
    entry_level = _entry_level_15m_ob()
    df = pd.DataFrame([{
        "ts_ms": ts0, "ts": ts0, "open": entry_level + 0.05, "high": entry_level + 0.05,
        "low": entry_level - 0.01, "close": entry_level + 0.05, "volume": 1.0,
    }])
    trig = signal_engine.scan_pending_zones(df, "15m")
    assert len(trig) == 1
    status, reason = _reason(key)
    assert status == "pending" and reason is None


# --------------------------------------------------------------- sizing cap


def test_sizing_capped_at_configured_balance(monkeypatch):
    monkeypatch.setattr(config, "SIZING_BALANCE_CAP", 2000.0)
    entries = [signal_engine.TriggeredEntry("k1", "15m", "OB", "bullish",
                                             _ts_at_hour(9, 20), 1504.0, 1497.7)]
    huge_balance = 50_000.0
    decisions = portfolio_state.decide_entries(entries, huge_balance, [], [], current_dvol=52.0)
    assert len(decisions) == 1
    # budget used for n_lots must reflect the CAPPED balance, not the huge one
    capped_budget = config.WEIGHT_PCT["OB"] * 2000.0
    uncapped_budget = config.WEIGHT_PCT["OB"] * huge_balance
    margin_per_lot = config.LOT_SIZE * 1504.0 * config.MARGIN_PCT
    expected_n_lots = int(capped_budget // margin_per_lot)
    assert decisions[0].n_lots == expected_n_lots
    assert decisions[0].n_lots < int(uncapped_budget // margin_per_lot)


def test_sizing_uncapped_when_balance_below_cap(monkeypatch):
    monkeypatch.setattr(config, "SIZING_BALANCE_CAP", 2000.0)
    entries = [signal_engine.TriggeredEntry("k1", "15m", "OB", "bullish",
                                             _ts_at_hour(9, 20), 1504.0, 1497.7)]
    small_balance = 1500.0  # below the cap -- cap must not distort this case
    decisions = portfolio_state.decide_entries(entries, small_balance, [], [], current_dvol=52.0)
    assert len(decisions) == 1
    budget = config.WEIGHT_PCT["OB"] * small_balance
    margin_per_lot = config.LOT_SIZE * 1504.0 * config.MARGIN_PCT
    assert decisions[0].n_lots == int(budget // margin_per_lot)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"\n{len(tests)} tests passed")
