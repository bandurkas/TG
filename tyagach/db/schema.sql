-- Tyagach SQLite schema. Single-writer (the loop process); the API process
-- opens read-only connections, except for the explicit pause/close-all writes.

CREATE TABLE IF NOT EXISTS bot_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),  -- singleton row
    balance_usdt REAL NOT NULL,
    paused INTEGER NOT NULL DEFAULT 0,
    last_processed_ts_ms INTEGER,
    updated_at_ms INTEGER NOT NULL
);

-- One row per zone the detectors have ever surfaced, keyed by a stable
-- timestamp-based signature (NOT the rolling window's positional index,
-- which shifts every tick). Prevents re-triggering the same zone twice and
-- tracks whether it's still waiting for its midpoint touch.
CREATE TABLE IF NOT EXISTS zone_signals (
    zone_key TEXT PRIMARY KEY,         -- f"{kind}:{direction}:{formed_ts_ms}:{zone_low}:{zone_high}"
    kind TEXT NOT NULL,                -- OB / BB / MB
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
    exit_reason TEXT,                  -- tp / sl / expiry
    close_order_id TEXT,
    pnl_net REAL,
    created_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    balance_usdt REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_snapshots(ts_ms);
