-- Tyagach SQLite schema. Single-writer (the loop process); the API process
-- opens read-only connections, except for the explicit pause/close-all writes.

CREATE TABLE IF NOT EXISTS bot_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),  -- singleton row
    balance_usdt REAL NOT NULL,
    start_balance_usdt REAL NOT NULL DEFAULT 0,
    started_at_ms INTEGER,
    paused INTEGER NOT NULL DEFAULT 0,
    close_all_requested INTEGER NOT NULL DEFAULT 0,  -- API sets, loop executes+resets
    last_processed_ts_ms INTEGER,           -- legacy 15m cursor; canonical cursors in tf_state
    updated_at_ms INTEGER NOT NULL
);

-- Partial (single-position) close requests: API inserts a row, loop deletes
-- it once executed (read-and-reset, same convention as close_all_requested,
-- but per-position and multi-row so more than one can queue between ticks).
-- Deliberately does NOT pause the bot -- closing one position is an ordinary
-- risk-management action that shouldn't halt new entries on other cells.
CREATE TABLE IF NOT EXISTS close_requests (
    position_id INTEGER PRIMARY KEY,
    requested_at_ms INTEGER NOT NULL
);

-- Per-timeframe processing cursors.  Replaces the single last_processed_ts_ms
-- in bot_state so each TF advances independently.
CREATE TABLE IF NOT EXISTS tf_state (
    timeframe TEXT PRIMARY KEY,
    last_processed_ts_ms INTEGER NOT NULL
);

-- One row per zone the detectors have ever surfaced, keyed by a stable
-- timestamp-based signature (NOT the rolling window's positional index,
-- which shifts every tick). Prevents re-triggering the same zone twice and
-- tracks whether it's still waiting for its midpoint touch.
CREATE TABLE IF NOT EXISTS zone_signals (
    zone_key TEXT PRIMARY KEY,         -- f"{tf}:{kind}:{direction}:{formed_ts_ms}:{zone_low}:{zone_high}"
    timeframe TEXT NOT NULL DEFAULT '15m',
    kind TEXT NOT NULL,                -- OB / BB / MB / FVG
    direction TEXT NOT NULL,           -- bullish / bearish
    formed_ts_ms INTEGER NOT NULL,
    valid_from_ts_ms INTEGER NOT NULL,
    zone_low REAL NOT NULL,
    zone_high REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending / triggered / invalidated / expired
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_key TEXT NOT NULL REFERENCES zone_signals(zone_key),
    timeframe TEXT NOT NULL DEFAULT '15m',
    zone_kind TEXT NOT NULL,
    direction TEXT NOT NULL,           -- bullish / bearish (of the zone)
    option_side TEXT NOT NULL,         -- 'C' or 'P' sold
    symbol TEXT NOT NULL,              -- real Bybit instrument symbol, e.g. ETH-30MAY26-3000-C
    strike REAL NOT NULL,
    entry_ts_ms INTEGER NOT NULL,
    entry_spot REAL NOT NULL,
    stop_price REAL NOT NULL,
    tp_price REAL NOT NULL,
    expiry_ts_ms INTEGER NOT NULL,
    iv_entry REAL NOT NULL,
    num_units REAL NOT NULL,           -- ETH notional units (n_lots * lot_size)
    notional REAL NOT NULL,
    sell_premium_received REAL NOT NULL,
    open_fee REAL NOT NULL DEFAULT 0,
    open_order_id TEXT,
    status TEXT NOT NULL DEFAULT 'open',  -- open / closed
    exit_ts_ms INTEGER,
    exit_spot REAL,
    exit_reason TEXT,                  -- tp / sl / expiry / trail / manual_close_all / manual_close_one
    close_order_id TEXT,
    pnl_net REAL,
    created_at_ms INTEGER NOT NULL,
    trail_peak_usd REAL NOT NULL DEFAULT 0  -- running peak of unrealized $ PnL, per-position trailing profit-lock
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    balance_usdt REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_snapshots(ts_ms);

-- Closed klines, written by the loop process as it fetches them (single
-- source of truth), read by the api process for /chart -- replaces api's
-- own independent Bybit cold-start backfill + incremental polling, which
-- doubled kline fetch volume against the same rate-limited endpoint.
CREATE TABLE IF NOT EXISTS klines (
    timeframe TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    PRIMARY KEY (timeframe, ts_ms)
);
