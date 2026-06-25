"""SQLite repo for Tyagach. Single-writer convention: only the loop process
writes; the API process opens its own read connections (WAL mode allows
concurrent readers without blocking the writer)."""
from __future__ import annotations

import os
import sqlite3
import time

DB_PATH = os.environ.get("TYAGACH_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "tyagach.db"))


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(starting_balance: float) -> None:
    conn = _connect()
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path) as f:
        conn.executescript(f.read())
    conn.execute(
        "INSERT OR IGNORE INTO bot_state (id, balance_usdt, paused, last_processed_ts_ms, updated_at_ms) "
        "VALUES (1, ?, 0, NULL, ?)",
        (starting_balance, int(time.time() * 1000)),
    )
    conn.commit()
    conn.close()


def get_state() -> dict:
    conn = _connect()
    row = conn.execute("SELECT * FROM bot_state WHERE id = 1").fetchone()
    conn.close()
    return dict(row) if row else {}


def set_balance(balance_usdt: float) -> None:
    conn = _connect()
    conn.execute(
        "UPDATE bot_state SET balance_usdt = ?, updated_at_ms = ? WHERE id = 1",
        (balance_usdt, int(time.time() * 1000)),
    )
    conn.execute(
        "INSERT INTO equity_snapshots (ts_ms, balance_usdt) VALUES (?, ?)",
        (int(time.time() * 1000), balance_usdt),
    )
    conn.commit()
    conn.close()


def set_paused(paused: bool) -> None:
    conn = _connect()
    conn.execute(
        "UPDATE bot_state SET paused = ?, updated_at_ms = ? WHERE id = 1",
        (1 if paused else 0, int(time.time() * 1000)),
    )
    conn.commit()
    conn.close()


def set_last_processed(ts_ms: int) -> None:
    conn = _connect()
    conn.execute(
        "UPDATE bot_state SET last_processed_ts_ms = ?, updated_at_ms = ? WHERE id = 1",
        (ts_ms, int(time.time() * 1000)),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- zone_signals

def upsert_zone_signal(zone_key: str, kind: str, direction: str, formed_ts_ms: int,
                        valid_from_ts_ms: int, zone_low: float, zone_high: float) -> None:
    conn = _connect()
    conn.execute(
        "INSERT OR IGNORE INTO zone_signals "
        "(zone_key, kind, direction, formed_ts_ms, valid_from_ts_ms, zone_low, zone_high, status, created_at_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
        (zone_key, kind, direction, formed_ts_ms, valid_from_ts_ms, zone_low, zone_high, int(time.time() * 1000)),
    )
    conn.commit()
    conn.close()


def get_pending_zone_signals() -> list[dict]:
    conn = _connect()
    rows = conn.execute("SELECT * FROM zone_signals WHERE status = 'pending'").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_zone_signal_status(zone_key: str, status: str) -> None:
    conn = _connect()
    conn.execute("UPDATE zone_signals SET status = ? WHERE zone_key = ?", (status, zone_key))
    conn.commit()
    conn.close()


# ----------------------------------------------------------------- positions

def open_position(*, zone_key: str, zone_kind: str, direction: str, option_side: str, symbol: str,
                   strike: float, entry_ts_ms: int, entry_spot: float, stop_price: float,
                   tp_price: float, expiry_ts_ms: int, iv_entry: float, num_units: float,
                   notional: float, sell_premium_received: float, open_fee: float,
                   open_order_id: str | None) -> int:
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO positions (zone_key, zone_kind, direction, option_side, symbol, strike, entry_ts_ms, "
        "entry_spot, stop_price, tp_price, expiry_ts_ms, iv_entry, num_units, notional, "
        "sell_premium_received, open_fee, open_order_id, status, created_at_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
        (zone_key, zone_kind, direction, option_side, symbol, strike, entry_ts_ms, entry_spot, stop_price,
         tp_price, expiry_ts_ms, iv_entry, num_units, notional, sell_premium_received, open_fee,
         open_order_id, int(time.time() * 1000)),
    )
    conn.commit()
    pos_id = cur.lastrowid
    conn.close()
    return pos_id


def get_open_positions() -> list[dict]:
    conn = _connect()
    rows = conn.execute("SELECT * FROM positions WHERE status = 'open'").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def close_position(position_id: int, *, exit_ts_ms: int, exit_spot: float, exit_reason: str,
                    close_order_id: str | None, pnl_net: float) -> None:
    conn = _connect()
    conn.execute(
        "UPDATE positions SET status = 'closed', exit_ts_ms = ?, exit_spot = ?, exit_reason = ?, "
        "close_order_id = ?, pnl_net = ? WHERE id = ?",
        (exit_ts_ms, exit_spot, exit_reason, close_order_id, pnl_net, position_id),
    )
    conn.commit()
    conn.close()


def get_positions(status: str | None = None, limit: int = 200) -> list[dict]:
    conn = _connect()
    if status:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status = ? ORDER BY entry_ts_ms DESC LIMIT ?", (status, limit)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM positions ORDER BY entry_ts_ms DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_equity_history(limit: int = 1000) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT ts_ms, balance_usdt FROM equity_snapshots ORDER BY ts_ms DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]


def close_all_pending(reason: str = "manual_close_all") -> int:
    """Marks every pending zone signal invalidated, used by emergency stop to
    prevent new entries from already-detected zones. Does NOT touch open
    positions — closing real option shorts is handled by the loop via the
    execution client, not here, since that needs a live Bybit call."""
    conn = _connect()
    cur = conn.execute("UPDATE zone_signals SET status = 'invalidated' WHERE status = 'pending'")
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n
