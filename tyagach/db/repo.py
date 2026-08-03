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


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """schema.sql's CREATE TABLE IF NOT EXISTS doesn't retroactively add
    columns to a table that already exists from an earlier deploy — add them
    here, ignoring the 'duplicate column' error on every later boot."""
    migrations = [
        # v1 backfills
        "ALTER TABLE bot_state ADD COLUMN start_balance_usdt REAL NOT NULL DEFAULT 0",
        "ALTER TABLE bot_state ADD COLUMN started_at_ms INTEGER",
        # multi-TF additions
        "ALTER TABLE zone_signals ADD COLUMN timeframe TEXT NOT NULL DEFAULT '15m'",
        "ALTER TABLE positions ADD COLUMN timeframe TEXT NOT NULL DEFAULT '15m'",
        "ALTER TABLE bot_state ADD COLUMN close_all_requested INTEGER NOT NULL DEFAULT 0",
    ]
    for stmt in migrations:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
    conn.commit()


def _seed_tf_state(conn: sqlite3.Connection) -> None:
    """Seed tf_state for 15m from the legacy bot_state.last_processed_ts_ms
    so the first boot after the multi-TF upgrade doesn't reprocess old 15m
    history. Other TFs start at NULL (normal cold start)."""
    row = conn.execute("SELECT last_processed_ts_ms FROM bot_state WHERE id = 1").fetchone()
    legacy_ts = row["last_processed_ts_ms"] if row else None
    if legacy_ts:
        conn.execute(
            "INSERT OR IGNORE INTO tf_state (timeframe, last_processed_ts_ms) VALUES ('15m', ?)",
            (legacy_ts,),
        )
        conn.commit()


def _expire_legacy_zone_keys(conn: sqlite3.Connection) -> None:
    """Expire pending zone_signals whose zone_key pre-dates the multi-TF
    upgrade (old format: 'OB:bullish:...' — no TF prefix).  New zones always
    use 'tf:OB:...' so old keys would create ghost duplicates alongside freshly
    detected zones for the same spatial zone, potentially triggering twice."""
    conn.execute(
        "UPDATE zone_signals SET status = 'expired' "
        "WHERE status = 'pending' "
        "AND zone_key NOT LIKE '15m:%' "
        "AND zone_key NOT LIKE '30m:%' "
        "AND zone_key NOT LIKE '1h:%' "
        "AND zone_key NOT LIKE '2h:%'",
    )
    conn.commit()


def init_db(starting_balance: float) -> None:
    conn = _connect()
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path) as f:
        conn.executescript(f.read())
    _ensure_columns(conn)
    now_ms = int(time.time() * 1000)
    conn.execute(
        "INSERT OR IGNORE INTO bot_state "
        "(id, balance_usdt, start_balance_usdt, started_at_ms, paused, last_processed_ts_ms, updated_at_ms) "
        "VALUES (1, ?, ?, ?, 0, NULL, ?)",
        (starting_balance, starting_balance, now_ms, now_ms),
    )
    # start_balance_usdt/started_at_ms only get set by the INSERT above on a
    # genuinely fresh row — backfill them on a pre-existing row from an
    # earlier deploy where they're still the ALTER TABLE default (0/NULL).
    conn.execute(
        "UPDATE bot_state SET start_balance_usdt = ?, started_at_ms = ? "
        "WHERE id = 1 AND (start_balance_usdt = 0 OR started_at_ms IS NULL)",
        (starting_balance, now_ms),
    )
    conn.commit()
    _seed_tf_state(conn)
    _expire_legacy_zone_keys(conn)
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


def insert_equity_snapshot(equity_usdt: float, ts_ms: int | None = None) -> None:
    """Standalone equity point (balance + unrealized) — the loop writes one
    every ~10 min so the dashboard curve moves BETWEEN closes too; set_balance
    keeps writing its realized-balance point on every close as before."""
    conn = _connect()
    conn.execute(
        "INSERT INTO equity_snapshots (ts_ms, balance_usdt) VALUES (?, ?)",
        (int(ts_ms if ts_ms is not None else time.time() * 1000), equity_usdt),
    )
    conn.commit()
    conn.close()


def last_equity_snapshot_ts_ms() -> int:
    conn = _connect()
    row = conn.execute("SELECT MAX(ts_ms) AS m FROM equity_snapshots").fetchone()
    conn.close()
    return int(row["m"] or 0)


def save_klines(tf: str, bars: list[dict]) -> None:
    """Upsert closed bars for `tf`, written by the loop process only. Last-
    write-wins per (tf, ts_ms), same self-healing semantics as
    market_data._merge_into_cache's in-memory overwrite."""
    if not bars:
        return
    conn = _connect()
    conn.executemany(
        "INSERT INTO klines (timeframe, ts_ms, open, high, low, close, volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(timeframe, ts_ms) DO UPDATE SET "
        "open=excluded.open, high=excluded.high, low=excluded.low, "
        "close=excluded.close, volume=excluded.volume",
        [(tf, b["ts_ms"], b["open"], b["high"], b["low"], b["close"], b["volume"]) for b in bars],
    )
    conn.commit()
    conn.close()


def get_klines(tf: str, limit: int = 1000) -> list[dict]:
    """Most recent `limit` closed bars for `tf`, oldest->newest."""
    conn = _connect()
    rows = conn.execute(
        "SELECT ts_ms, open, high, low, close, volume FROM klines "
        "WHERE timeframe = ? ORDER BY ts_ms DESC LIMIT ?",
        (tf, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]


def set_paused(paused: bool) -> None:
    conn = _connect()
    conn.execute(
        "UPDATE bot_state SET paused = ?, updated_at_ms = ? WHERE id = 1",
        (1 if paused else 0, int(time.time() * 1000)),
    )
    conn.commit()
    conn.close()


def request_close_all() -> None:
    """API-side: flags every open position for buyback and pauses new entries.
    The loop (single writer) executes the actual closes on its next tick."""
    conn = _connect()
    conn.execute(
        "UPDATE bot_state SET close_all_requested = 1, paused = 1, updated_at_ms = ? WHERE id = 1",
        (int(time.time() * 1000),),
    )
    conn.commit()
    conn.close()


def pop_close_all_requested() -> bool:
    """Read-and-reset the close-all flag (loop side)."""
    conn = _connect()
    row = conn.execute("SELECT close_all_requested FROM bot_state WHERE id = 1").fetchone()
    requested = bool(row and row["close_all_requested"])
    if requested:
        conn.execute("UPDATE bot_state SET close_all_requested = 0 WHERE id = 1")
        conn.commit()
    conn.close()
    return requested


def request_close_position(position_id: int, now_ms: int) -> None:
    """API-side: queues a single-position buyback for the loop to execute on
    its next tick. Does NOT pause the bot -- an ordinary risk-management
    action, not an emergency stop."""
    conn = _connect()
    conn.execute(
        "INSERT OR IGNORE INTO close_requests (position_id, requested_at_ms) VALUES (?, ?)",
        (position_id, now_ms),
    )
    conn.commit()
    conn.close()


def pop_close_requests() -> list[int]:
    """Read-and-reset all pending single-position close requests (loop side)."""
    conn = _connect()
    ids = [r["position_id"] for r in conn.execute("SELECT position_id FROM close_requests")]
    if ids:
        conn.execute("DELETE FROM close_requests")
        conn.commit()
    conn.close()
    return ids


def set_last_processed(tf: str, ts_ms: int) -> None:
    """Update the per-TF cursor in tf_state (canonical) and keep the legacy
    bot_state.last_processed_ts_ms in sync for 15m so older API clients /
    dashboards reading it directly continue to work."""
    conn = _connect()
    conn.execute(
        "INSERT INTO tf_state (timeframe, last_processed_ts_ms) VALUES (?, ?) "
        "ON CONFLICT(timeframe) DO UPDATE SET last_processed_ts_ms = excluded.last_processed_ts_ms",
        (tf, ts_ms),
    )
    if tf == "15m":
        conn.execute(
            "UPDATE bot_state SET last_processed_ts_ms = ?, updated_at_ms = ? WHERE id = 1",
            (ts_ms, int(time.time() * 1000)),
        )
    conn.commit()
    conn.close()


def get_last_processed(tf: str) -> int | None:
    conn = _connect()
    row = conn.execute(
        "SELECT last_processed_ts_ms FROM tf_state WHERE timeframe = ?", (tf,)
    ).fetchone()
    conn.close()
    return row["last_processed_ts_ms"] if row else None


# ---------------------------------------------------------------- zone_signals

def upsert_zone_signal(zone_key: str, timeframe: str, kind: str, direction: str,
                        formed_ts_ms: int, valid_from_ts_ms: int,
                        zone_low: float, zone_high: float) -> None:
    conn = _connect()
    conn.execute(
        "INSERT OR IGNORE INTO zone_signals "
        "(zone_key, timeframe, kind, direction, formed_ts_ms, valid_from_ts_ms, "
        "zone_low, zone_high, status, created_at_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
        (zone_key, timeframe, kind, direction, formed_ts_ms, valid_from_ts_ms,
         zone_low, zone_high, int(time.time() * 1000)),
    )
    conn.commit()
    conn.close()


def get_pending_zone_signals(timeframe: str) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM zone_signals WHERE status = 'pending' AND timeframe = ?", (timeframe,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_pending_zone_signals() -> list[dict]:
    """Unlike get_pending_zone_signals, not scoped to one TF -- for cleanup
    sweeps that must reach rows on a TF no longer in ACTIVE_TFS (scan_pending_
    zones's own ACTIVE_CELLS cleanup only runs for TFs still being ticked)."""
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

def open_position(*, zone_key: str, timeframe: str, zone_kind: str, direction: str,
                   option_side: str, symbol: str, strike: float, entry_ts_ms: int,
                   entry_spot: float, stop_price: float, tp_price: float,
                   expiry_ts_ms: int, iv_entry: float, num_units: float,
                   notional: float, sell_premium_received: float, open_fee: float,
                   open_order_id: str | None) -> int:
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO positions (zone_key, timeframe, zone_kind, direction, option_side, symbol, "
        "strike, entry_ts_ms, entry_spot, stop_price, tp_price, expiry_ts_ms, iv_entry, "
        "num_units, notional, sell_premium_received, open_fee, open_order_id, status, created_at_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
        (zone_key, timeframe, zone_kind, direction, option_side, symbol, strike, entry_ts_ms,
         entry_spot, stop_price, tp_price, expiry_ts_ms, iv_entry, num_units, notional,
         sell_premium_received, open_fee, open_order_id, int(time.time() * 1000)),
    )
    conn.commit()
    pos_id = cur.lastrowid
    conn.close()
    return pos_id


def get_open_positions(timeframe: str | None = None) -> list[dict]:
    """Return open positions. Pass timeframe to scope to a single TF sub-book;
    omit to get all open positions (used for the global ceiling check)."""
    conn = _connect()
    if timeframe is not None:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status = 'open' AND timeframe = ?", (timeframe,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM positions WHERE status = 'open'").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_open_position(position_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM positions WHERE id = ? AND status = 'open'", (position_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_position_stats() -> dict:
    conn = _connect()
    row = conn.execute(
        "SELECT COUNT(*) AS n_closed, "
        "SUM(CASE WHEN pnl_net > 0 THEN 1 ELSE 0 END) AS wins, "
        "SUM(CASE WHEN pnl_net <= 0 THEN 1 ELSE 0 END) AS losses, "
        "SUM(pnl_net) AS realized "
        "FROM positions WHERE status = 'closed'"
    ).fetchone()
    conn.close()
    n_closed = row["n_closed"] or 0
    wins = row["wins"] or 0
    losses = row["losses"] or 0
    return {
        "n_closed": n_closed,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / n_closed) if n_closed else None,
        "realized_usd": row["realized"] or 0.0,
    }


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


def close_position_and_set_balance(position_id: int, *, exit_ts_ms: int, exit_spot: float,
                                     exit_reason: str, close_order_id: str | None,
                                     pnl_net: float) -> float:
    """Atomically close a position and apply its pnl_net to balance_usdt (plus
    an equity snapshot) in ONE transaction. loop.py used to do this as three
    separate connections (close_position, then a get_state read, then
    set_balance) -- a crash between the first and the rest would leave a
    position permanently marked closed with a recorded pnl_net that never
    actually landed in balance_usdt, silently losing that PnL from the
    account with no way to detect or replay it later. Returns the new
    balance. Raises and rolls back (nothing persisted) on any failure."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE positions SET status = 'closed', exit_ts_ms = ?, exit_spot = ?, exit_reason = ?, "
            "close_order_id = ?, pnl_net = ? WHERE id = ?",
            (exit_ts_ms, exit_spot, exit_reason, close_order_id, pnl_net, position_id),
        )
        row = conn.execute("SELECT balance_usdt FROM bot_state WHERE id = 1").fetchone()
        new_balance = (row["balance_usdt"] if row else 0.0) + pnl_net
        now_ms = int(time.time() * 1000)
        conn.execute(
            "UPDATE bot_state SET balance_usdt = ?, updated_at_ms = ? WHERE id = 1",
            (new_balance, now_ms),
        )
        conn.execute(
            "INSERT INTO equity_snapshots (ts_ms, balance_usdt) VALUES (?, ?)",
            (now_ms, new_balance),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return new_balance


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
