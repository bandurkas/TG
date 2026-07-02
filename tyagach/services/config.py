"""Validated Tyagach parameters — see ~/Desktop/smc_options/TYAGACH_HANDOFF.md
for how these were derived (4yr ETH train/validation/holdout grid search).
Do not change without re-running the sweep; these are not guesses."""
from __future__ import annotations

import os
from dataclasses import dataclass

# Paper vs live gate, mirrors opt-app's execution_config.trading_armed()
# convention. The account behind BYBIT_API_KEY/SECRET is a REAL Bybit
# mainnet account (not Bybit's separate testnet environment) — in "paper"
# mode (the default) services/execution.py uses it ONLY for read-only market
# data (instrument lookup, quotes, wallet/account info) and never calls
# place_order; fills are simulated against the real live quote instead.
TRADING_MODE = os.environ.get("TYAGACH_TRADING_MODE", "paper").strip().lower()


def is_live() -> bool:
    return TRADING_MODE == "live"


DAYS_PER_YEAR = 365.0
BUFFER_FRAC = 0.0015  # SL buffer beyond zone edge, same as options_backtest.py

# Per-zone validated config: R-target, expiry (days), entry IV threshold (DVOL %).
# Same values apply regardless of timeframe — the backtest confirmed the deployed
# per-zone config transfers to all active TFs without re-optimisation.
# IV thresholds lowered 2026-07-03 based on extended sweep (sweep_iv_lower_multitf.csv):
#   OB: 60→50 (holdout +$0.18→+$0.14 at iv≥50, more trades, net+ confirmed)
#   MB: 70→50 (holdout +$0.67→+$0.92 at iv≥50, more trades AND better quality)
#   BB: 60→55 (holdout +$1.32→+$1.55 at iv≥55, 30m/BB at iv≥50 too weak +$0.13)
ZONE_CONFIG = {
    "OB": {"r_target": 3.0, "expiry_days": 0.5, "iv_threshold": 50.0},
    "MB": {"r_target": 3.0, "expiry_days": 0.5, "iv_threshold": 50.0},
    "BB": {"r_target": 2.5, "expiry_days": 5.0, "iv_threshold": 55.0},
}

# Portfolio allocation — manually chosen (NOT the raw grid-search optimum),
# see TYAGACH_HANDOFF.md "Portfolio allocation" section.
PRIORITY = {"BB": 0, "MB": 1, "OB": 2}  # lower = higher priority
WEIGHT_PCT = {"OB": 0.12, "MB": 0.18, "BB": 0.28}  # % of current balance per new position
MAX_OPEN_PER_ZONE = {"OB": 3, "MB": 2, "BB": 1}   # caps apply within each TF sub-book

# Global ceiling across ALL timeframe sub-books combined.  Prevents all TFs
# firing simultaneously from over-leveraging the single shared account.
MAX_OPEN_TOTAL_GLOBAL = 8   # hard cap on simultaneous positions across all TFs
MAX_TOTAL_MARGIN_PCT = 0.60  # combined open margin must not exceed 60% of balance

LOT_SIZE = 0.10  # ETH options min lot on Bybit (matches live_sizing.py convention)
MARGIN_PCT = 0.15
FEE_RATE = 0.0003    # 0.03% of underlying notional per side (real Bybit options taker)
FEE_CAP_PCT = 0.125  # capped at 12.5% of option premium per side

SWING_ORDER = 3  # fractal swing detection lookback, matches research


# ---------------------------------------------------------------------------
# Timeframe registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TF:
    label: str           # human key, e.g. "15m"
    interval: str        # Bybit kline interval code, e.g. "15"
    bar_ms: int          # milliseconds per bar
    bars_per_day: int    # closed bars per 24h
    rolling_window: int  # bars kept in memory for zone detection
    max_lookahead: int   # zone→entry search horizon (bars); scaled to ~8.3 days
    stale_after: int     # bars since touch before signal expires (~1h wall-clock)


# All lookahead / stale values keep constant wall-clock coverage across TFs:
#   lookahead = 800 bars × (bars_per_day_tf / 96)   ≈ 8.3 days
#   stale     = 4 bars   × (bars_per_day_tf / 96)   ≈ 1 hour  (min 1)
TIMEFRAMES: dict[str, TF] = {
    "15m": TF("15m", "15",  15 * 60_000,  96, 2000,  800, 4),
    "30m": TF("30m", "30",  30 * 60_000,  48, 1200,  400, 2),
    "1h":  TF("1h",  "60",  60 * 60_000,  24,  800,  200, 1),
    "2h":  TF("2h",  "120", 120 * 60_000, 12,  500,  100, 1),
}

# Which (tf, zone_kind) cells are active for trading.
# Derived from the net-of-fee multi-TF backtest (2026-07-02) + IV-threshold sweep (2026-07-03):
#   5m — dead (gross < round-trip fee)
#   15m OB/MB/BB — net+ confirmed on holdout at iv≥50 (OB barely: +$0.14, MB: +$0.92, BB: +$1.70)
#   30m OB/MB/BB — net+ on holdout (OB: +$0.66, MB: +$1.70, BB: +$0.67 at iv≥55)
#   1h  MB — net+ (strong: +$4.64); 1h OB/BB negative — excluded
#   2h  OB — net+ (+$1.33); 2h MB — very strong (+$6.57); 2h BB negative — excluded
ACTIVE_CELLS: frozenset[tuple[str, str]] = frozenset({
    ("15m", "OB"), ("15m", "MB"), ("15m", "BB"),
    ("30m", "OB"), ("30m", "MB"), ("30m", "BB"),
    ("1h",  "MB"),
    ("2h",  "OB"), ("2h",  "MB"),
})

# Ordered list of active TFs (determines loop processing order each tick)
ACTIVE_TFS: list[str] = ["15m", "30m", "1h", "2h"]

SYMBOL = "ETHUSDT"
BASE_COIN = "ETH"
POLL_SECONDS = 60  # loop wake interval

# Legacy scalar kept for any remaining callers; canonical source is TIMEFRAMES["15m"]
BARS_PER_DAY = 96
