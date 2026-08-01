"""Regression for close_position_and_set_balance (2026-08-02 reliability
review, mirroring the Jony fleet's equivalent fix): closing a position and
crediting its pnl_net to balance_usdt used to be three separate SQLite
connections/commits (close_position, then a get_state read, then
set_balance). A crash between the first and the rest would leave the
position permanently marked closed with a recorded pnl_net that never
actually landed in the balance -- silently losing that PnL with no way to
detect or replay it. The fix does both writes in one transaction.

Run: cd tyagach && python3 tests/test_atomic_close.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import repo


def _setup_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    repo.DB_PATH = tmp.name
    repo.init_db(2000.0)


def _open_dummy_position() -> int:
    return repo.open_position(
        zone_key="15m:OB:bullish:1:1500.000000:1510.000000", timeframe="15m", zone_kind="OB",
        direction="bullish", option_side="P", symbol="ETH-1AUG26-1500-P-USDT", strike=1500.0,
        entry_ts_ms=1, entry_spot=1505.0, stop_price=1495.0, tp_price=1520.0,
        expiry_ts_ms=2, iv_entry=55.0, num_units=0.8, notional=1200.0,
        sell_premium_received=10.0, open_fee=0.3, open_order_id="PAPER-x",
    )


def test_close_and_balance_commit_together():
    _setup_db()
    pos_id = _open_dummy_position()
    new_balance = repo.close_position_and_set_balance(
        pos_id, exit_ts_ms=100, exit_spot=1502.0, exit_reason="tp",
        close_order_id="PAPER-y", pnl_net=7.5,
    )
    assert new_balance == 2007.5
    state = repo.get_state()
    assert state["balance_usdt"] == 2007.5
    closed = repo.get_positions(status="closed")
    assert len(closed) == 1 and closed[0]["pnl_net"] == 7.5


def test_failure_rolls_back_both_writes():
    """If the transaction fails partway, NEITHER the position-close NOR the
    balance update may be visible afterward -- a partial commit (closed
    position, stale balance) is exactly the bug this function exists to
    prevent."""
    _setup_db()
    pos_id = _open_dummy_position()
    balance_before = repo.get_state()["balance_usdt"]

    orig_connect = repo._connect

    class _BoomConn:
        def __init__(self, real):
            self._real = real
            self._n = 0

        def execute(self, *a, **kw):
            self._n += 1
            if self._n == 3:  # after the position UPDATE and the balance SELECT
                raise sqlite3.OperationalError("simulated failure")
            return self._real.execute(*a, **kw)

        def __getattr__(self, name):
            return getattr(self._real, name)

    import sqlite3
    def _boom_connect():
        return _BoomConn(orig_connect())

    repo._connect = _boom_connect
    try:
        raised = False
        try:
            repo.close_position_and_set_balance(
                pos_id, exit_ts_ms=100, exit_spot=1502.0, exit_reason="tp",
                close_order_id="PAPER-y", pnl_net=7.5,
            )
        except sqlite3.OperationalError:
            raised = True
        assert raised, "expected the simulated failure to propagate"
    finally:
        repo._connect = orig_connect

    state = repo.get_state()
    assert state["balance_usdt"] == balance_before, "balance must be unchanged on failure"
    open_positions = repo.get_open_positions()
    assert len(open_positions) == 1, "position must still be open (not closed) on failure"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"\n{len(tests)} tests passed")
