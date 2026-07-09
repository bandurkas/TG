"""Tests for the periodic equity snapshot (2026-07-09): repo helpers +
loop._snapshot_equity throttle/unrealized math. Before this, equity_snapshots
was written only on trade close, so the dashboard curve froze between closes.

Run: cd tyagach && python3 -m pytest tests/test_equity_snapshot.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

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


def test_insert_and_last_ts_roundtrip():
    _fresh_db()
    assert repo.last_equity_snapshot_ts_ms() >= 0
    before = repo.last_equity_snapshot_ts_ms()
    repo.insert_equity_snapshot(1999.5, ts_ms=before + 60_000)
    assert repo.last_equity_snapshot_ts_ms() == before + 60_000
    hist = repo.get_equity_history(limit=10)
    assert any(abs(r["balance_usdt"] - 1999.5) < 1e-9 for r in hist)


def test_snapshot_equity_throttles():
    _fresh_db()
    now = int(time.time() * 1000)
    loop._snapshot_equity(now)
    n1 = len(repo.get_equity_history(limit=100))
    # second call inside the cadence window must be a no-op
    loop._snapshot_equity(now + 1000)
    assert len(repo.get_equity_history(limit=100)) == n1
    # past the cadence window a new point lands
    loop._snapshot_equity(now + loop.EQUITY_SNAPSHOT_EVERY_MS + 1000)
    assert len(repo.get_equity_history(limit=100)) == n1 + 1


def test_snapshot_includes_unrealized(monkeypatch):
    _fresh_db()
    now = int(time.time() * 1000)
    open_row = {
        "status": "open",
        "symbol": "ETH-10JUL26-1775-C-USDT",
        "num_units": 1.0,
        "sell_premium_received": 10.0,
    }
    monkeypatch.setattr(repo, "get_open_positions", lambda: [open_row])

    class _FakeClient:
        def get_quote(self, sym):
            return {"mark": 4.0, "bid": 3.9, "ask": 4.1}

    monkeypatch.setattr(loop.execution, "get_client", lambda: _FakeClient())
    mark_pricing._QUOTE_CACHE.clear()
    loop._snapshot_equity(now + loop.EQUITY_SNAPSHOT_EVERY_MS + 1000)
    latest = repo.get_equity_history(limit=1)[-1]
    # balance 2000 + unrealized (10 - 4) = 2006
    assert abs(latest["balance_usdt"] - 2006.0) < 1e-9


def test_snapshot_degrades_to_realized_on_quote_failure(monkeypatch):
    _fresh_db()
    now = int(time.time() * 1000)
    monkeypatch.setattr(repo, "get_open_positions",
                        lambda: [{"status": "open", "symbol": "X", "num_units": 1.0,
                                  "sell_premium_received": 10.0}])

    def _boom():
        raise RuntimeError("no client")

    monkeypatch.setattr(loop.execution, "get_client", _boom)
    loop._snapshot_equity(now + loop.EQUITY_SNAPSHOT_EVERY_MS + 1000)
    latest = repo.get_equity_history(limit=1)[-1]
    assert abs(latest["balance_usdt"] - 2000.0) < 1e-9
