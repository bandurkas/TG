# Tyagach Session Handoff — 2026-07-03

## What was done this session

### Root cause found: DVOL below all entry thresholds
- ETH DVOL = **55.0%** → was below OB/BB threshold (60) and MB threshold (70)
- Every tick: zones detected, price touching them, but `filter_by_iv` returned empty list
- **Zero trades were possible under old thresholds**

### Extended IV threshold backtest (local Mac)
Script: `sweep_iv_lower.py` → results: `results/sweep_iv_lower_multitf.csv`

Ran across all TFs (15m/30m/1h/2h) with IV_THRESHOLDS=[50,55,60,65,70,75],
using fee-adjusted net PnL (WEIGHT_PCT×balance→n_lots sizing, 0.03%×notional fees).

**Holdout results (key cells):**

| Cell | iv≥50 net$/trade | iv≥55 | iv≥60 | Decision |
|------|-----------------|-------|-------|----------|
| 15m/OB | +$0.14 | +$0.11 | +$0.18 | keep, thr→50 |
| 15m/MB | +$0.92 | +$0.71 | +$0.67 | keep, thr→50 |
| 15m/BB | +$1.70 | +$1.55 | +$1.32 | keep, thr→55 |
| 30m/OB | +$0.66 | +$0.63 | +$0.56 | keep, thr→50 |
| **30m/MB** | **+$1.70** | +$1.23 | +$1.38 | **ADDED** |
| **30m/BB** | +$0.13 | **+$0.67** | +$3.02 | **ADDED (iv≥55)** |
| 1h/MB | +$4.64 | +$4.55 | +$4.17 | keep, thr→50 |
| 1h/OB | -$0.47 | -$0.48 | -$0.44 | excluded |
| 1h/BB | -$0.33 | +$0.36 | +$0.63 | excluded (n<40) |
| **2h/MB** | **+$6.57** | +$7.50 | +$7.61 | **ADDED (strongest cell!)** |
| 2h/OB | +$1.33 | +$1.49 | +$2.15 | keep, thr→50 |
| 2h/BB | -$4.66 | -$4.14 | -$6.50 | excluded |

### Config changes deployed (commit 205ac32)

`tyagach/services/config.py`:
```python
ZONE_CONFIG = {
    "OB": {"r_target": 3.0, "expiry_days": 0.5, "iv_threshold": 50.0},  # was 60
    "MB": {"r_target": 3.0, "expiry_days": 0.5, "iv_threshold": 50.0},  # was 70
    "BB": {"r_target": 2.5, "expiry_days": 5.0, "iv_threshold": 55.0},  # was 60
}

ACTIVE_CELLS = frozenset({
    ("15m", "OB"), ("15m", "MB"), ("15m", "BB"),
    ("30m", "OB"), ("30m", "MB"), ("30m", "BB"),   # +30m/MB, +30m/BB
    ("1h",  "MB"),
    ("2h",  "OB"), ("2h",  "MB"),                  # +2h/MB
})
```

### Deployment
- Rebuilt image on VPS3 (`docker compose build tyagach_loop`)
- Reset 30m/2h tf_state cursors → bot rescanned rolling window for new cells
- Bot running: 9 active cells, DVOL=55% UNBLOCKED on all three types
- **Balance: $2009.97** (one artifact trade: 2h/MB -$1.10, expiry-day one-off — ETH-3JUL26 was expiring same day as deploy)

## Current bot state

| Parameter | Value |
|-----------|-------|
| VPS3 | 187.127.114.34 |
| Loop container | tyagach-tyagach_loop-1 |
| API | :8100 /api/v1/tyagach/ |
| Balance | $2009.97 |
| ETH DVOL | ~55% (unblocked at new thresholds) |
| Active cells | 9 |
| Pending zones | 6 (bullish support zones at $1533–$1664) |
| Closed trades | 6 total (5 old + 1 artifact) |

### Pending zones as of session end

| TF | Kind | Zone range | Age |
|----|------|-----------|-----|
| 30m | MB | $1569–$1598 | 27h |
| 1h | MB | $1661–$1664 | 3h |
| 1h | MB | $1619–$1651 | 7h |
| 15m | OB | $1597–$1602 | 16h |
| 15m | OB | $1533–$1534 | 147h |
| 30m | OB | $1533–$1535 | 147h |

All zones are BULLISH (support) — ETH needs to pull back to these levels to trigger.

## Issues noted (non-critical)

1. **Bybit rate limit hits** in logs (ErrCode: 10006) at ~12:00 and 12:30 UTC — pybit retries automatically after 2s, no trades missed. Multiple bots sharing same IP/key might contribute. Monitor.

2. **2h/MB artifact trade (-$1.10)**: opened ETH-3JUL26-1650-P (expiring same day) → near-zero time value → fees dominated. One-off. Future 2h/MB trades will have correct time value.

## Future work (do NOT implement yet — wait for live data)

### Engine rebuild (after 20-30 cycles per cell)
User requested: replace per-KIND shared config with per-CELL independent params:
```python
# Future architecture:
CELL_CONFIG: dict[tuple[str,str], CellParams] = {
    ("15m", "OB"): CellParams(iv_threshold=50, r_target=3.0, expiry_days=0.5, weight_pct=0.12),
    ("2h",  "MB"): CellParams(iv_threshold=50, r_target=3.0, expiry_days=0.5, weight_pct=0.28),
    # ...
}
```
See `results/sweep_iv_lower_multitf.csv` for per-cell holdout data as starting point.

### Monitoring checklist (next session)
- [ ] Check trade frequency: expect 1-3 trades/week vs 0 before
- [ ] Verify 30m/MB and 2h/MB generate fresh zones (rolling window has expired most history)
- [ ] Confirm no stale-signal trades after today's expiry-day issue
- [ ] After ~20 live trades: review per-cell win rates vs backtest holdout expectations

## Files changed this session

| File | What |
|------|------|
| `tyagach/services/config.py` | IV thresholds + ACTIVE_CELLS (committed 205ac32) |
| `results/sweep_iv_lower_multitf.csv` | New extended backtest results (commit separately) |
| `TYAGACH_HANDOFF_2026-07-03.md` | This file |
