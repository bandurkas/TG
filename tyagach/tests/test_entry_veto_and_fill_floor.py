"""Tests for the 2026-07-09 review deployables:
  P1 — entry-hour veto (config.ENTRY_VETO_UTC_HOURS): a fresh touch whose bar
       falls in 12-15h UTC is CONSUMED (status 'expired'), outside the window
       it triggers normally; empty veto set disables.
  P2 — fill sanity floor (config.FILL_FLOOR_FRAC): a bid below the floor
       fraction of BS-mid skips the open WITHOUT consuming the signal;
       a normal bid opens.

Run: cd tyagach && python3 -m pytest tests/test_entry_veto_and_fill_floor.py -q
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from db import repo
from services import config, signal_engine, portfolio_state
import loop


def _setup_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    repo.DB_PATH = tmp.name
    repo.init_db(2000.0)


def _force_15m_ob_active(monkeypatch):
    # 15m/OB deactivated 2026-08-02 (see config.py) -- these tests are about
    # veto/fill-floor logic, not cell activation, so force it active for the
    # duration of the test rather than rewriting fixtures around a live cell.
    monkeypatch.setattr(config, "ACTIVE_CELLS", config.ACTIVE_CELLS | {("15m", "OB")})


def _ts_at_hour(hour: int) -> int:
    return int(dt.datetime(2026, 7, 9, hour, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)


def _df_one_bar(ts_ms: int, price: float):
    return pd.DataFrame([{
        "ts_ms": ts_ms, "ts": ts_ms, "open": price, "high": price,
        "low": price, "close": price, "volume": 1.0,
    }])


def _pending_ob_signal(ts_ms: int):
    # zone 1500-1510 bullish; deployed 15m/OB depth 0.575 -> entry level 1504.25;
    # a bar at 1504.0 touches without closing below the stop.
    key = f"15m:OB:bullish:{ts_ms}:1500.000000:1510.000000"
    repo.upsert_zone_signal(key, "15m", "OB", "bullish", ts_ms, ts_ms, 1500.0, 1510.0)
    return key


def _signal_status(key: str):
    import sqlite3
    conn = sqlite3.connect(repo.DB_PATH)
    row = conn.execute("SELECT status FROM zone_signals WHERE zone_key=?", (key,)).fetchone()
    conn.close()
    return row[0]


def test_veto_hour_touch_is_consumed_not_traded(monkeypatch):
    _setup_db()
    _force_15m_ob_active(monkeypatch)
    ts = _ts_at_hour(13)  # inside default 12-15h veto
    key = _pending_ob_signal(ts)
    trig = signal_engine.scan_pending_zones(_df_one_bar(ts, 1504.0), "15m")
    assert trig == []
    assert _signal_status(key) == "expired"


def test_outside_veto_hour_triggers(monkeypatch):
    _setup_db()
    _force_15m_ob_active(monkeypatch)
    ts = _ts_at_hour(20)
    key = _pending_ob_signal(ts)
    trig = signal_engine.scan_pending_zones(_df_one_bar(ts, 1504.0), "15m")
    assert len(trig) == 1 and trig[0].zone_key == key
    assert _signal_status(key) == "pending"


def test_empty_veto_set_disables(monkeypatch):
    _setup_db()
    _force_15m_ob_active(monkeypatch)
    monkeypatch.setattr(config, "ENTRY_VETO_UTC_HOURS", frozenset())
    ts = _ts_at_hour(13)
    key = _pending_ob_signal(ts)
    trig = signal_engine.scan_pending_zones(_df_one_bar(ts, 1504.0), "15m")
    assert len(trig) == 1
    assert _signal_status(key) == "pending"


# ---------------------------------------------------------------- fill floor

class _FakeClient:
    def __init__(self, bid: float):
        self.bid = bid
        self.sold = []

    def find_instrument(self, side, strike, expiry_days):
        return {"symbol": "ETH-10JUL26-1500-P-USDT",
                "deliveryTime": _ts_at_hour(20) + 86_400_000}

    def get_quote(self, symbol):
        return {"bid": self.bid, "ask": self.bid * 1.05, "mark": self.bid * 1.02}

    def round_qty(self, instrument, units):
        return units

    def sell_to_open(self, symbol, qty, bid, spot):
        self.sold.append(symbol)
        return None  # not-filled path ends _execute_open harmlessly for the test


def _decision(iv_entry=52.0):
    e = signal_engine.TriggeredEntry("k", "15m", "OB", "bullish",
                                     _ts_at_hour(20), 1504.0, 1497.7)
    return portfolio_state.EntryDecision(entry=e, option_side="P", strike=1504.0,
                                         n_lots=1, num_units=0.9, notional=1353.6,
                                         margin_required=135.0, iv_entry=iv_entry)


def test_fill_floor_blocks_glitched_bid(monkeypatch):
    _setup_db()
    # BS-mid for 1d ATM put @IV52 on 1504 spot ≈ $16.3; bid $3 is id6-style glitch
    fake = _FakeClient(bid=3.0)
    monkeypatch.setattr(loop.execution, "get_client", lambda: fake)
    loop._execute_open(_decision())
    assert fake.sold == []  # never reached sell_to_open


def test_fill_floor_passes_normal_bid(monkeypatch):
    _setup_db()
    fake = _FakeClient(bid=14.0)  # ~86% of BS-mid — normal fill
    monkeypatch.setattr(loop.execution, "get_client", lambda: fake)
    loop._execute_open(_decision())
    assert fake.sold == ["ETH-10JUL26-1500-P-USDT"]


def test_fill_floor_disabled(monkeypatch):
    _setup_db()
    monkeypatch.setattr(config, "FILL_FLOOR_FRAC", 0.0)
    fake = _FakeClient(bid=3.0)
    monkeypatch.setattr(loop.execution, "get_client", lambda: fake)
    loop._execute_open(_decision())
    assert fake.sold == ["ETH-10JUL26-1500-P-USDT"]
