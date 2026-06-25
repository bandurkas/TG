from __future__ import annotations
"""Trailing realized volatility from spot closes, annualized.
Same convention as the options project's straddle sims: 7-day trailing
window, clamped to [0.20, 1.50] annualized — avoids feeding the BS pricer
degenerate vol during dead-quiet or single-spike windows."""
import numpy as np
import pandas as pd

BARS_PER_DAY_15M = 96
LOOKBACK_DAYS = 7
LOOKBACK_BARS = BARS_PER_DAY_15M * LOOKBACK_DAYS
PERIODS_PER_YEAR = 365 * BARS_PER_DAY_15M
SIGMA_MIN, SIGMA_MAX = 0.20, 1.50


def trailing_rv_series(df: pd.DataFrame) -> np.ndarray:
    closes = df["close"].values
    log_ret = np.diff(np.log(closes), prepend=np.log(closes[0]))
    n = len(closes)
    sigma = np.full(n, np.nan)
    for i in range(LOOKBACK_BARS, n):
        window = log_ret[i - LOOKBACK_BARS : i]
        sigma[i] = window.std() * np.sqrt(PERIODS_PER_YEAR)
    # backfill the warm-up period with the first valid estimate
    first_valid = np.argmax(~np.isnan(sigma))
    sigma[:first_valid] = sigma[first_valid]
    return np.clip(sigma, SIGMA_MIN, SIGMA_MAX)
