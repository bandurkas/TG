# Tyagach — paper bot handoff

ETH options paper-trading bot based on ICT/SMC zone detection (OB/BB/MB) +
real-IV sell-premium signal. This doc is the single source of truth to
start the next session building the actual paper bot ("Tyagach").

## Source code (read-only research, reuse/port from here)

- `~/Desktop/smc_zones/` — first pass: zone detectors + spot R-multiple entry study (1yr ETH).
- `~/Desktop/smc_options/` — current, authoritative: same detectors + options overlay + portfolio sim. **Use this as the base.**
  - `src/structure.py` — swing detection (fractal, order=3), HH/HL/LH/LL labeling, BOS/MSS structure events, FVG detection.
  - `src/ob.py` — Order Block detector (3-candle: impulse→sweep+body zone→engulf confirm; FVG merges into zone if it forms).
  - `src/bb.py` — Breaker Block detector (former opposite-polarity OB broken AFTER an MSS; zone = full wick range of original OB candle).
  - `src/mb.py` — Mitigation Block detector (impulsive BOS break, no opposite-structure break; zone = full wick range of breakout candle).
  - `src/zones.py` — unifies OB/BB/MB into a common `Zone` shape.
  - `src/bs_pricer.py` — Black-Scholes pricer, ported from `~/Desktop/options/backend/services/backtest_bs.py`.
  - `src/dvol.py` — loads real Deribit ETH DVOL, forward-fill aligned to 15m grid (no lookahead).
  - `src/fetch_klines.py` / `src/fetch_dvol.py` — historical data fetchers (Bybit via VPS3 — Mac blocks direct Bybit API; Deribit DVOL works directly from Mac).
  - `src/sweep_sell.py` — per-zone entry finder (`_find_midpoint_entry`) + exit simulator + BS sell P&L, plus the R-target/expiry/IV-threshold grid sweep.
  - `src/portfolio.py` — **the core engine to carry forward**: event-driven single-balance simulator across OB/MB/BB candidates, priority-based conflict resolution, lot/margin/fee frictions.
  - `src/sweep_portfolio.py` — portfolio-layer grid search (8-core multiprocessing).
  - `src/account_sim.py` — deposit sweep ($400/$800/$2000) with $/month and APR reporting.

## Data

- `data/eth_15m.csv`, `data/eth_1h.csv` — ETH USDT-perp OHLCV from Bybit, 2022-06-18 → now (full available history — that's the actual listing date, not a fetch limit).
- `data/eth_dvol_1h.json` — real Deribit ETH DVOL hourly, 2022-06 → now, gap-free (Deribit history actually goes back to 2021, only fetched what overlaps spot data).
- `data/eth_15m_train.csv` / `_holdout.csv` — legacy 80/20 split (superseded by the 60/20/20 split used in `sweep_portfolio.py`/`account_sim.py`).

## Validated rules (exact, as specified by user)

**Order Block (OB):** liquidity sweep (SSL/BSL) by the EXACT candle that becomes the OB (not before/after). Confirmation = next candle's body engulfs OB's body. Zone = OB candle's body only, UNLESS an FVG forms immediately after → then the swept wick + FVG merge into the zone. Pattern = 3 candles (impulse → OB → confirmation).

**Breaker Block (BB):** former OB of opposite polarity, impulsively broken AFTER an HH/LL update (MSS). Both wicks always included (no FVG merge, unlike OB).

**Mitigation Block (MB):** impulsive break of swing high/low WITHOUT opposite-structure break (continuation, not reversal). Zone = both wicks of the breakout candle. OB inside is optional reinforcement (not required).

## Validated entry trigger

**Midpoint entry (50% zone depth)** beats touch/close_back/engulf on ALL THREE zone types, robust train+holdout. Don't use any other entry trigger.

## Validated edge: SELL premium when IV is rich

Per-zone optimized configs (grid search, confirmed positive sign+magnitude on train/validation/holdout, 4yr ETH):

| Zone | R-target | Expiry | IV threshold (entry DVOL%) | Option sold |
|---|---|---|---|---|
| OB | 3.0R | 0.5 day | >60 | put (bullish zone) / call (bearish zone) |
| MB | 3.0R | 0.5 day | >70 | same convention |
| BB | 2.5R | 5 days | >60 | same convention |

**Buying premium when IV<50 was tested and REJECTED** — no reproducible edge across zone types/splits (flat or sign-flips OOS). Don't build a buy-side leg.

## Portfolio allocation (single balance, multi-zone)

Priority order: **BB > MB > OB** (by structural confirmation strength — also used for conflict resolution, not just tie-breaking).

Conflict rules:
- Never stack same-direction positions (any zone) — skip new signal if a same-direction position is already open anywhere in the portfolio.
- Per-zone concurrency caps + a global cap, evaluated in priority order.

Recommended (manually chosen, NOT the raw grid-search optimum — the optimizer pushes weight to its grid ceiling because the backtest is too kind to leverage; same lesson as the rejected carry-bot leverage research — scale deposit, not leverage):

```python
weight_pct        = {"OB": 0.12, "MB": 0.18, "BB": 0.28}  # % of CURRENT balance per new position
max_open_per_zone = {"OB": 3,    "MB": 2,    "BB": 1}
max_open_total    = 5
lot_size          = 0.01   # ETH options min lot, Bybit
margin_pct        = 0.15   # same convention as btc_straddle_dollar_account_sim.py
fee_rate          = 0.0003 # 0.03% of notional/side
fee_cap_pct       = 0.125  # capped at 12.5% of premium/side
```

## Backtest results (4yr continuous, WITH lot/margin/fee frictions)

| Deposit | Final | Total return | Max DD | Trades | APR (compounded) |
|---|---|---|---|---|---|
| $400 | $1512.5 | +278% | 15.5% | 3932 | 39.2% |
| $800 | $3038.8 | +280% | 15.8% | 3932 | 39.4% |
| $2000 | $7648.5 | +282% | 15.8% | 3932 | 39.6% |

No lot-size blocking at any of these deposit sizes (0.01 ETH min lot doesn't bind even at $400). Per-zone trade frequency (holdout estimate): OB ~46/month, MB ~19/month, BB ~15/month, ~80/month total combined.

## What's honestly NOT validated — read before trusting the APR number

1. **The 39% APR figure is NOT pure out-of-sample.** It's computed on the full continuous 4yr run, which includes the same train period used to pick the per-zone R/expiry/IV-threshold configs. The cleaner (lower, more honest) forward estimate is the validation+holdout-only performance from `sweep_portfolio.py`'s confirm step (pre-fee idealized model: +11-12% per ~8mo split).
2. **Edge magnitude is NOT constant** — sign is consistent across splits, but size swings meaningfully (e.g. MB avg $/trade dropped ~2x train→holdout). Treat any point estimate as a range, not a guarantee.
3. **Synthetic Black-Scholes pricing via real DVOL, not a real recorded options chain** — no real bid/ask spread, no skew/smile, no real fill risk. This is the same documented limitation as `~/Desktop/options/backend/services/backtest.py`.
4. **No real execution tested** — no slippage beyond the modeled stop, no API integration, no live order book interaction.
5. Single asset (ETH), single methodology. Not cross-checked against BTC or other coins.

## Next steps for the new session (building "Tyagach")

1. Decide: paper-trade via real Bybit options orders (testnet or live-paper flag, matching the existing options bots' pattern — e.g. `ETH_STRADDLE_PAPER_BOT_HANDOFF.md` precedent) vs. a pure internal simulator fed by live data.
2. Port `structure.py/ob.py/bb.py/mb.py/zones.py/portfolio.py` into the new Tyagach project (don't depend on `smc_options` or `smc_zones` directly — same isolation pattern used throughout this research).
3. Wire live ETH spot feed (15m) + live Deribit DVOL feed to drive zone detection + IV-threshold filtering in real time.
4. Implement the SELL-only execution path (put for bullish zone / call for bearish zone), ATM strike at signal, expiry per the table above.
5. Apply the exact portfolio config above (weights/caps/priority) as a starting point — re-validate once real fills are available before scaling deposit.
6. Dashboard/monitoring — reuse Mission Control conventions if useful (`project_mission_control` memory) for visibility, given this will run alongside the existing live bots.
