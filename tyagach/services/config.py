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
# IV thresholds lowered 2026-07-03 based on extended sweep (sweep_iv_lower_multitf.csv),
# re-confirmed 2026-07-03 (review pass) against all three splits (train/validation/
# holdout), not holdout alone — see ARCHITECTURE.md "Amendment 2026-07-03":
#   OB: 60→50   BB: 60→55 (BB net+ on all 3 splits at iv≥55, NOT at iv≥50)
#   MB: 70→50 — MB cells deactivated 2026-07-07 (live override, see ACTIVE_CELLS
#     below); the entry is kept so a future re-activation reuses validated values.
# Which cells actually trade is governed solely by ACTIVE_CELLS.
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
# Re-derived 2026-07-07 (see ARCHITECTURE.md "Amendment 2026-07-07"): the
# first live sample (21 closed paper trades) showed MB as the sole bleeder
# (15 trades, -$33.72, last 8 closes all MB) while OB was the only
# live-profitable kind (5 trades, +$12.15, 4/5 wins). User decision:
#   MB — DEACTIVATED entirely (live-performance override of the backtest,
#     where MB cells passed all 3 splits; live contradicts them).
#   OB — enabled on ALL four TFs. DELIBERATE OVERRIDE of the 2026-07-03
#     all-3-splits admission criterion: 15m/OB and 1h/OB FAIL it
#     (sweep_iv_lower_multitf.csv), included anyway by explicit user call
#     prioritizing live evidence + trade frequency. Portfolio backtest of
#     this exact set is still positive on all 3 splits with lower maxDD
#     (results/tyagach_ob_alltf.csv). Revisit after ~20-30 OB closes/cell.
#   15m/BB — kept (passes all 3 splits at iv>=55; only 1 live trade so far).
#   5m — still dead (gross < round-trip fee).
ACTIVE_CELLS: frozenset[tuple[str, str]] = frozenset({
    ("15m", "OB"), ("15m", "BB"),
    ("30m", "OB"),
    ("1h",  "OB"),
    ("2h",  "OB"),
})

# Ordered list of active TFs (determines loop processing order each tick)
ACTIVE_TFS: list[str] = ["15m", "30m", "1h", "2h"]

SYMBOL = "ETHUSDT"
BASE_COIN = "ETH"
POLL_SECONDS = 60  # loop wake interval

# Legacy scalar kept for any remaining callers; canonical source is TIMEFRAMES["15m"]
BARS_PER_DAY = 96
