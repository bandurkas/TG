"""Validated Tyagach parameters — see ~/Desktop/smc_options/TYAGACH_HANDOFF.md
for how these were derived (4yr ETH train/validation/holdout grid search).
Do not change without re-running the sweep; these are not guesses."""
from __future__ import annotations

import os

# Paper vs live gate, mirrors opt-app's execution_config.trading_armed()
# convention. The account behind BYBIT_API_KEY/SECRET is a REAL Bybit
# mainnet account (not Bybit's separate testnet environment) — in "paper"
# mode (the default) services/execution.py uses it ONLY for read-only market
# data (instrument lookup, quotes, wallet/account info) and never calls
# place_order; fills are simulated against the real live quote instead.
TRADING_MODE = os.environ.get("TYAGACH_TRADING_MODE", "paper").strip().lower()


def is_live() -> bool:
    return TRADING_MODE == "live"

BARS_PER_DAY = 96  # 15m bars
DAYS_PER_YEAR = 365.0
BUFFER_FRAC = 0.0015  # SL buffer beyond zone edge, same as options_backtest.py

# Per-zone validated config: R-target, expiry (days), entry IV threshold (DVOL %)
ZONE_CONFIG = {
    "OB": {"r_target": 3.0, "expiry_days": 0.5, "iv_threshold": 60.0},
    "MB": {"r_target": 3.0, "expiry_days": 0.5, "iv_threshold": 70.0},
    "BB": {"r_target": 2.5, "expiry_days": 5.0, "iv_threshold": 60.0},
}

# Portfolio allocation — manually chosen (NOT the raw grid-search optimum),
# see TYAGACH_HANDOFF.md "Portfolio allocation" section.
PRIORITY = {"BB": 0, "MB": 1, "OB": 2}  # lower = higher priority
WEIGHT_PCT = {"OB": 0.12, "MB": 0.18, "BB": 0.28}  # % of current balance per new position
MAX_OPEN_PER_ZONE = {"OB": 3, "MB": 2, "BB": 1}
MAX_OPEN_TOTAL = 5

LOT_SIZE = 0.10  # ETH options min lot on Bybit (matches live_sizing.py convention)
MARGIN_PCT = 0.15
FEE_RATE = 0.0003
FEE_CAP_PCT = 0.125

SWING_ORDER = 3  # fractal swing detection lookback, matches research
ROLLING_WINDOW_BARS = 2000  # ~20 days of 15m bars kept in memory for zone detection
MAX_ZONE_LOOKAHEAD = 800  # matches options_backtest.py MAX_LOOKAHEAD — zone invalidation horizon

SYMBOL = "ETHUSDT"
BASE_COIN = "ETH"
KLINE_INTERVAL = "15"  # Bybit kline interval code for 15m
POLL_SECONDS = 60  # loop wake interval; actual decisions only act on newly-closed 15m bars
