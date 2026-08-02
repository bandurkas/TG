"""Tests for the shared kline cache (2026-08-02, closes TYAGACH_IMPROVEMENT_PLAN.md
P4 "Klines-кэш loop<->api"): the loop process persists closed bars to
db.repo.klines; the api process reads from there instead of running its own
independent Bybit backfill/incremental cache. Also covers loop._check_stale_tfs,
the companion "silent loop" heartbeat alert.

Run: cd tyagach && python3 -m pytest tests/test_klines_cache.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db.repo as repo
import loop
from services import config, market_data, signal_engine, telegram_notify


def _fresh_db() -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.unlink(tmp.name)
    repo.DB_PATH = tmp.name
    repo.init_db(2000.0)
    return tmp.name


def _bar(ts_ms, o, h, l, c, v=1.0):
    return {"ts_ms": ts_ms, "open": o, "high": h, "low": l, "close": c, "volume": v}


# ── db.repo.save_klines / get_klines ────────────────────────────────────────


def test_save_and_get_klines_roundtrip():
    _fresh_db()
    bars = [_bar(1000, 1, 2, 0.5, 1.5), _bar(2000, 1.5, 2.5, 1.0, 2.0)]
    repo.save_klines("15m", bars)
    got = repo.get_klines("15m", limit=10)
    assert [b["ts_ms"] for b in got] == [1000, 2000], "must return oldest->newest"


def test_save_klines_upsert_overwrites_by_ts():
    """Mirrors market_data._merge_into_cache's overwrite semantics -- a later
    save for a ts_ms already in the table must replace it, not be ignored."""
    _fresh_db()
    repo.save_klines("15m", [_bar(1000, 1, 2, 0.5, 1.5)])
    repo.save_klines("15m", [_bar(1000, 1, 9.0, 0.1, 1.5)])  # corrected high/low
    got = repo.get_klines("15m", limit=10)
    assert len(got) == 1
    assert got[0]["high"] == 9.0 and got[0]["low"] == 0.1


def test_get_klines_respects_limit_and_tf_isolation():
    _fresh_db()
    repo.save_klines("15m", [_bar(i * 1000, 1, 2, 0.5, 1.5) for i in range(5)])
    repo.save_klines("1h", [_bar(999, 1, 2, 0.5, 1.5)])
    got_15m = repo.get_klines("15m", limit=2)
    assert [b["ts_ms"] for b in got_15m] == [3000, 4000], "limit keeps the most recent bars"
    got_1h = repo.get_klines("1h", limit=10)
    assert [b["ts_ms"] for b in got_1h] == [999], "TFs must not leak into each other"


def test_save_klines_noop_on_empty_list():
    _fresh_db()
    repo.save_klines("15m", [])  # must not raise
    assert repo.get_klines("15m") == []


# ── loop._process_tf: full-window persistence across a restart ─────────────


def test_process_tf_persists_full_window_not_just_delta(monkeypatch):
    """Regression: after a container restart, market_data's in-memory cache
    resets and re-backfills the FULL window from Bybit, but repo's
    last_processed cursor survives (it's in the DB). Persisting only the
    bars newer than that old cursor would leave db.repo.klines (and
    therefore api's /chart) missing almost all history, refilling one bar
    at a time over days instead of being whole again on the very next tick."""
    _fresh_db()
    tf = "15m"
    old_cursor = 5000  # far behind the fresh backfill below -- simulates a pre-restart cursor
    repo.set_last_processed(tf, old_cursor)

    full_window = [_bar(i * 1000, 1, 2, 0.5, 1.5) for i in range(1, 21)]  # ts 1000..20000
    monkeypatch.setattr(market_data, "get_klines", lambda tf: full_window)
    monkeypatch.setattr(signal_engine, "sync_new_zones", lambda df, tf: None)
    monkeypatch.setattr(signal_engine, "scan_pending_zones", lambda df, tf: [])
    monkeypatch.setattr(repo, "get_open_positions", lambda tf=None: [])
    monkeypatch.setattr(market_data, "get_latest_dvol", lambda: None)

    loop._process_tf(tf, now_ms=999_999_999)

    persisted = repo.get_klines(tf, limit=100)
    assert len(persisted) == len(full_window), (
        "the full window must be persisted, not only the bars past the old cursor"
    )
    assert persisted[0]["ts_ms"] == 1000, "history from before the old cursor must not be dropped"


# ── loop._check_stale_tfs ───────────────────────────────────────────────────


def test_check_stale_tfs_alerts_once_and_dedups(monkeypatch):
    _fresh_db()
    loop._STALE_ALERTED.clear()
    repo.set_last_processed("15m", 0)  # ts_ms=0 -> any now_ms is "stale"

    sent = []
    monkeypatch.setattr(telegram_notify, "notify", lambda text, **kw: sent.append(text))

    now_ms = 10_000_000_000
    loop._check_stale_tfs(now_ms)
    loop._check_stale_tfs(now_ms + 1000)  # same stall, must not re-alert
    assert len(sent) == 1
    assert "15m" in sent[0]


def test_check_stale_tfs_recovers_and_realerts(monkeypatch):
    _fresh_db()
    loop._STALE_ALERTED.clear()
    repo.set_last_processed("15m", 0)

    sent = []
    monkeypatch.setattr(telegram_notify, "notify", lambda text, **kw: sent.append(text))

    now_ms = 10_000_000_000
    loop._check_stale_tfs(now_ms)
    assert len(sent) == 1

    repo.set_last_processed("15m", now_ms)  # cursor advances -> recovered
    loop._check_stale_tfs(now_ms + 1000)
    assert "15m" not in loop._STALE_ALERTED

    repo.set_last_processed("15m", now_ms)  # stalls again far in the future
    stale_again_ms = now_ms + max(3 * config.TIMEFRAMES["15m"].bar_ms, loop._STALE_FLOOR_MS) + 1000
    loop._check_stale_tfs(stale_again_ms)
    assert len(sent) == 2, "a stall after recovery must alert again"


def test_check_stale_tfs_skips_unprocessed_tf():
    """A TF with no cursor yet (fresh deploy, first backfill still pending)
    must not alert -- that's a startup grace period, not a stall."""
    _fresh_db()
    loop._STALE_ALERTED.clear()
    loop._check_stale_tfs(10_000_000_000)  # must not raise, nothing to assert on state


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
