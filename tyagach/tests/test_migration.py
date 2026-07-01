"""Tests for DB migration: _ensure_columns idempotency, _seed_tf_state,
_expire_legacy_zone_keys, and full init_db idempotency.

Run: cd tyagach && python3 tests/test_migration.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db.repo as repo

_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "db", "schema.sql")


def _fresh_db() -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.unlink(tmp.name)
    repo.DB_PATH = tmp.name
    return tmp.name


def _raw(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _schema_conn(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    with open(_SCHEMA_PATH) as f:
        conn.executescript(f.read())
    return conn


# ── _ensure_columns ──────────────────────────────────────────────────────────


def test_ensure_columns_idempotent():
    path = _fresh_db()
    conn = _schema_conn(path)
    repo._ensure_columns(conn)
    repo._ensure_columns(conn)  # second call must not raise
    cols_z = {r[1] for r in conn.execute("PRAGMA table_info(zone_signals)")}
    cols_p = {r[1] for r in conn.execute("PRAGMA table_info(positions)")}
    assert "timeframe" in cols_z
    assert "timeframe" in cols_p
    conn.close()


# ── _seed_tf_state ───────────────────────────────────────────────────────────


def test_seed_tf_state_seeds_15m_from_legacy():
    _fresh_db()
    repo.init_db(1000.0)
    conn = _raw(repo.DB_PATH)
    conn.execute("UPDATE bot_state SET last_processed_ts_ms = 1750000000000 WHERE id = 1")
    conn.commit()
    repo._seed_tf_state(conn)
    row = conn.execute(
        "SELECT last_processed_ts_ms FROM tf_state WHERE timeframe = '15m'"
    ).fetchone()
    assert row is not None and row[0] == 1750000000000
    for tf in ("30m", "1h", "2h"):
        r = conn.execute("SELECT * FROM tf_state WHERE timeframe = ?", (tf,)).fetchone()
        assert r is None, f"{tf} must not appear in tf_state after seed"
    conn.close()


def test_seed_tf_state_is_idempotent():
    _fresh_db()
    repo.init_db(1000.0)
    conn = _raw(repo.DB_PATH)
    conn.execute("UPDATE bot_state SET last_processed_ts_ms = 1750000000000 WHERE id = 1")
    conn.commit()
    repo._seed_tf_state(conn)
    repo._seed_tf_state(conn)
    rows = conn.execute("SELECT * FROM tf_state WHERE timeframe = '15m'").fetchall()
    assert len(rows) == 1 and rows[0]["last_processed_ts_ms"] == 1750000000000
    conn.close()


# ── _expire_legacy_zone_keys ─────────────────────────────────────────────────


def _insert_zone(conn, zone_key, status="pending"):
    now_ms = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO zone_signals "
        "(zone_key, timeframe, kind, direction, formed_ts_ms, valid_from_ts_ms, "
        "zone_low, zone_high, status, created_at_ms) "
        "VALUES (?, '15m', 'OB', 'bullish', ?, ?, 1500.0, 1510.0, ?, ?)",
        (zone_key, now_ms, now_ms, status, now_ms),
    )
    conn.commit()


def test_expire_legacy_zone_keys_expires_old_format():
    _fresh_db()
    repo.init_db(1000.0)
    conn = _raw(repo.DB_PATH)
    old_key = "OB:bullish:1750000000000:1500.000000:1510.000000"
    _insert_zone(conn, old_key, "pending")
    repo._expire_legacy_zone_keys(conn)
    row = conn.execute("SELECT status FROM zone_signals WHERE zone_key = ?", (old_key,)).fetchone()
    assert row["status"] == "expired"
    conn.close()


def test_expire_legacy_zone_keys_keeps_new_format():
    _fresh_db()
    repo.init_db(1000.0)
    conn = _raw(repo.DB_PATH)
    for tf_prefix in ("15m", "30m", "1h", "2h"):
        new_key = f"{tf_prefix}:OB:bullish:1750000000000:1500.000000:1510.000000"
        _insert_zone(conn, new_key, "pending")
    repo._expire_legacy_zone_keys(conn)
    for tf_prefix in ("15m", "30m", "1h", "2h"):
        new_key = f"{tf_prefix}:OB:bullish:1750000000000:1500.000000:1510.000000"
        row = conn.execute("SELECT status FROM zone_signals WHERE zone_key = ?", (new_key,)).fetchone()
        assert row["status"] == "pending", f"{new_key} should remain pending"
    conn.close()


def test_expire_legacy_zone_keys_leaves_non_pending_alone():
    _fresh_db()
    repo.init_db(1000.0)
    conn = _raw(repo.DB_PATH)
    old_triggered = "OB:bullish:1750000000000:1500.000000:1600.000000"
    _insert_zone(conn, old_triggered, "triggered")  # already triggered — don't change
    repo._expire_legacy_zone_keys(conn)
    row = conn.execute("SELECT status FROM zone_signals WHERE zone_key = ?", (old_triggered,)).fetchone()
    assert row["status"] == "triggered"
    conn.close()


# ── init_db idempotency ───────────────────────────────────────────────────────


def test_init_db_idempotent_preserves_balance():
    _fresh_db()
    repo.init_db(2000.0)
    repo.set_balance(1850.0)
    repo.init_db(2000.0)  # second boot
    state = repo.get_state()
    assert state["balance_usdt"] == 1850.0
    assert state["start_balance_usdt"] == 2000.0


def test_init_db_runs_legacy_expiry_on_fresh_boot():
    _fresh_db()
    repo.init_db(1000.0)
    conn = _raw(repo.DB_PATH)
    old_key = "OB:bearish:1750000000000:1500.000000:1510.000000"
    _insert_zone(conn, old_key, "pending")
    conn.close()
    repo.init_db(1000.0)  # second boot should expire the old key
    conn = _raw(repo.DB_PATH)
    row = conn.execute("SELECT status FROM zone_signals WHERE zone_key = ?", (old_key,)).fetchone()
    assert row["status"] == "expired"
    conn.close()


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"\n{len(tests)} tests passed")
