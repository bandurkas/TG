"""Unit tests for services/mark_pricing.py (2026-06-27) — Tyagach's
dashboard "Active trades" rail previously showed no live PnL for open
positions, unlike Sniper1/Boba1/Grogu1. Covers the pure enrichment function
in isolation (no Bybit credentials/network needed).

Run: cd tyagach && python3 tests/test_mark_pricing.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import mark_pricing as mp


def _open_row(**overrides):
    row = {
        "id": 1, "symbol": "ETH-3JUL26-1575-P-USDT", "status": "open",
        "num_units": 2.0, "sell_premium_received": 100.0,
    }
    row.update(overrides)
    return row


def test_open_position_gets_mark_and_pnl_from_mark_price():
    rows = [_open_row()]
    out = mp.enrich_positions_with_mark(rows, lambda sym: {"bid": 40.0, "ask": 42.0, "mark": 41.0})
    assert out[0]["current_mark_usd"] == 41.0
    # entry credit/unit = 100/2 = 50; pnl = (50-41)*2 = 18
    assert out[0]["unrealized_pnl_usd"] == 18.0


def test_falls_back_to_bid_ask_mid_when_mark_price_zero():
    rows = [_open_row()]
    out = mp.enrich_positions_with_mark(rows, lambda sym: {"bid": 40.0, "ask": 44.0, "mark": 0.0})
    assert out[0]["current_mark_usd"] == 42.0


def test_closed_position_left_untouched():
    rows = [_open_row(status="closed", num_units=1.0)]
    out = mp.enrich_positions_with_mark(rows, lambda sym: {"bid": 1, "ask": 1, "mark": 1})
    assert out[0]["current_mark_usd"] is None
    assert out[0]["unrealized_pnl_usd"] is None


def test_missing_quote_leaves_fields_none():
    rows = [_open_row()]
    out = mp.enrich_positions_with_mark(rows, lambda sym: None)
    assert out[0]["current_mark_usd"] is None
    assert out[0]["unrealized_pnl_usd"] is None


def test_zero_num_units_skipped_safely():
    rows = [_open_row(num_units=0.0)]
    out = mp.enrich_positions_with_mark(rows, lambda sym: {"bid": 1, "ask": 1, "mark": 1})
    assert out[0]["current_mark_usd"] is None


def test_cached_quote_dedupes_within_ttl():
    calls = []

    def fake_get_quote(sym):
        calls.append(sym)
        return {"bid": 1.0, "ask": 1.0, "mark": 1.0}

    mp._QUOTE_CACHE.clear()
    mp.cached_quote(fake_get_quote, "ETH-X")
    mp.cached_quote(fake_get_quote, "ETH-X")
    assert calls == ["ETH-X"]  # second call served from cache, not re-fetched


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"\n{len(tests)} tests passed")
