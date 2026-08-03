"""Tests for manual close-all / close-one (2026-08-03): the Mission Control
"close all" button previously only paused the bot and invalidated pending
zone SIGNALS -- it never actually flattened open option positions (api.py
said so in its own docstring, and the frontend warned about it). This adds
the real thing, mirroring Jony's close_all_requested/close_requests pattern:
API queues a request, the loop (single writer) executes it via the same
_execute_close accounting path used for SL/TP.

Run: cd tyagach && python3 -m pytest tests/test_manual_close.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db.repo as repo
import loop
from services import mark_pricing


def _fresh_db() -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.unlink(tmp.name)
    repo.DB_PATH = tmp.name
    repo.init_db(2000.0)
    return tmp.name


def _open_position(symbol="ETH-1AUG26-1500-P-USDT", zone_key=None) -> int:
    zone_key = zone_key or f"15m:OB:bullish:1:1500.000000:1510.000000:{symbol}"
    repo.upsert_zone_signal(zone_key, "15m", "OB", "bullish", 1, 1, 1500.0, 1510.0)
    return repo.open_position(
        zone_key=zone_key, timeframe="15m", zone_kind="OB",
        direction="bullish", option_side="P", symbol=symbol, strike=1500.0,
        entry_ts_ms=1, entry_spot=1505.0, stop_price=1495.0, tp_price=1520.0,
        expiry_ts_ms=99_999_999_999_999, iv_entry=55.0, num_units=0.8, notional=1200.0,
        sell_premium_received=10.0, open_fee=0.3, open_order_id="PAPER-x",
    )


class _FakeClient:
    def get_quote(self, symbol):
        return {"bid": 3.5, "ask": 4.0, "mark": 3.75}

    def buy_to_close(self, symbol, qty, limit_price, spot):
        return execution_result(qty)


def execution_result(qty):
    from services.execution import OrderResult
    return OrderResult(order_id="PAPER-close", avg_price=4.0, filled_qty=qty, fees=0.05, status="Filled")


def _patch_execution(monkeypatch):
    monkeypatch.setattr(loop.execution, "get_client", lambda: _FakeClient())
    monkeypatch.setattr(loop.market_data, "get_spot_price", lambda: 1502.0)
    mark_pricing._QUOTE_CACHE.clear()


def test_request_close_all_sets_flag_and_pauses():
    _fresh_db()
    assert repo.get_state()["paused"] == 0
    repo.request_close_all()
    state = repo.get_state()
    assert state["paused"] == 1
    assert repo.pop_close_all_requested() is True
    assert repo.pop_close_all_requested() is False  # read-and-reset, consumed once


def test_close_all_now_closes_every_open_position(monkeypatch):
    _fresh_db()
    _patch_execution(monkeypatch)
    id1 = _open_position("ETH-1AUG26-1500-P-USDT")
    id2 = _open_position("ETH-1AUG26-1600-P-USDT")
    assert len(repo.get_open_positions()) == 2

    loop._close_all_now()

    assert repo.get_open_positions() == []
    closed = repo.get_positions(status="closed")
    assert len(closed) == 2
    assert all(c["exit_reason"] == "manual_close_all" for c in closed)
    # premium 10.0 - buy_premium (4.0*0.8=3.2) - open_fee 0.3 - close_fee 0.05 = 6.45 per position
    assert repo.get_state()["balance_usdt"] == 2000.0 + 2 * 6.45


def test_close_position_now_closes_only_the_requested_one(monkeypatch):
    _fresh_db()
    _patch_execution(monkeypatch)
    id1 = _open_position("ETH-1AUG26-1500-P-USDT")
    id2 = _open_position("ETH-1AUG26-1600-P-USDT")

    loop._close_position_now(id1)

    still_open = repo.get_open_positions()
    assert len(still_open) == 1 and still_open[0]["id"] == id2
    closed = repo.get_positions(status="closed")
    assert len(closed) == 1 and closed[0]["exit_reason"] == "manual_close_one"


def test_close_position_now_is_a_noop_if_already_closed(monkeypatch):
    """Race with an SL/TP sweep that resolved it first in the same tick --
    must not raise or double-close."""
    _fresh_db()
    _patch_execution(monkeypatch)
    id1 = _open_position()
    repo.close_position_and_set_balance(
        id1, exit_ts_ms=2, exit_spot=1500.0, exit_reason="sl",
        close_order_id="PAPER-y", pnl_net=-2.0,
    )
    loop._close_position_now(id1)  # must not raise
    closed = repo.get_positions(status="closed")
    assert len(closed) == 1 and closed[0]["exit_reason"] == "sl"  # unchanged


def test_close_request_queue_roundtrip_and_dedup():
    _fresh_db()
    repo.request_close_position(101, now_ms=1)
    repo.request_close_position(102, now_ms=2)
    repo.request_close_position(101, now_ms=3)  # duplicate, INSERT OR IGNORE
    ids = repo.pop_close_requests()
    assert sorted(ids) == [101, 102]
    assert repo.pop_close_requests() == []  # consumed


def test_get_open_position_scopes_to_open_status():
    _fresh_db()
    pos_id = _open_position()
    assert repo.get_open_position(pos_id) is not None
    assert repo.get_open_position(999999) is None
    repo.close_position_and_set_balance(
        pos_id, exit_ts_ms=2, exit_spot=1500.0, exit_reason="sl",
        close_order_id="PAPER-y", pnl_net=-2.0,
    )
    assert repo.get_open_position(pos_id) is None  # now closed, no longer "open"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"\n{len(tests)} tests passed")
