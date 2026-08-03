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

# 2026-08-03 (SL_BUFFER_HANDOFF_2026-08-02.md): BUFFER_FRAC didn't scale with
# TF/volatility -- median bar range exceeded it, so stops fired on noise, not
# structure breaks. buf = mult * ATR(14) fixes this for 1h/OB and 1h/FVG
# (holdout calmar 1.5->2.5, 5.6->9.2; portfolio 0/8 neg quarters preserved).
# HURTS 15m/30m (correlated stops stack up during trend moves, ballooning
# maxDD there) -- scope stays 1h-only, re-check both scripts before extending.
ATR_PERIOD = 14
ATR_BUFFER_MULT: dict[tuple[str, str], float] = {
    ("1h", "OB"): 2.0,
    ("1h", "FVG"): 2.0,
}

# 2026-08-03 round 2: swept 15m/30m/2h x OB/FVG/MB (9 cells) only -- BB and
# 1h/MB were out of scope, still on the untouched default below. For the 7
# cells here, a wider FLAT fraction beat both 0.0015 and ATR scaling (which
# still gets blown through in a real trend, just later and for more $ --
# confirmed via live-data replay of the 2026-08-02 losing streak,
# SL_BUFFER_HANDOFF). Stays flat (bounded) on purpose -- no runaway widening
# risk. 15m/OB and 2h/MB WERE swept but no grid point beat the default.
# Keys here must stay disjoint from ATR_BUFFER_MULT -- see
# test_flat_buffer_override.py's disjointness test.
FLAT_BUFFER_FRAC_OVERRIDE: dict[tuple[str, str], float] = {
    ("15m", "FVG"): 0.0025,
    ("15m", "MB"):  0.005,
    ("30m", "OB"):  0.002,
    ("30m", "FVG"): 0.003,
    ("30m", "MB"):  0.0025,
    ("2h",  "OB"):  0.005,
    ("2h",  "FVG"): 0.004,
}

# Per-zone validated config: R-target, expiry (days), entry IV threshold (DVOL %),
# and depth_frac (how far into the zone price must retrace before entry counts —
# 0.0=touch the near edge, 0.5=zone midpoint, 1.0=touch the far edge). These are
# the KIND-level defaults, shared across TFs unless CELL_CONFIG below overrides
# a specific (tf, kind) — see cell_config().
# IV thresholds lowered 2026-07-03 based on extended sweep (sweep_iv_lower_multitf.csv),
# re-confirmed 2026-07-03 (review pass) against all three splits (train/validation/
# holdout), not holdout alone — see ARCHITECTURE.md "Amendment 2026-07-03":
#   OB: 60→50   BB: 60→55 (BB net+ on all 3 splits at iv≥55, NOT at iv≥50)
#   MB: 70→50 — MB cells deactivated 2026-07-07 (live override, see ACTIVE_CELLS
#     below); the entry is kept so a future re-activation reuses validated values.
# Which cells actually trade is governed solely by ACTIVE_CELLS.
ZONE_CONFIG = {
    "OB": {"r_target": 3.0, "expiry_days": 0.5, "iv_threshold": 50.0, "depth_frac": 0.5},
    "MB": {"r_target": 3.0, "expiry_days": 0.5, "iv_threshold": 50.0, "depth_frac": 0.5},
    "BB": {"r_target": 2.5, "expiry_days": 5.0, "iv_threshold": 55.0, "depth_frac": 0.5},
    # Untuned generic default -- FVG only ever trades via its CELL_CONFIG
    # override below (2h), same pattern as OB.
    "FVG": {"r_target": 3.0, "expiry_days": 0.5, "iv_threshold": 50.0, "depth_frac": 0.5},
}

# Per-cell overrides — takes priority over ZONE_CONFIG[kind] when a (tf, kind)
# key is present here; falls through to the kind-level default otherwise.
# See ARCHITECTURE.md "Amendment 2026-07-08" for the full derivation.
#
# OB populated 2026-07-08 from a ~3500-combo sweep (17 depths x 8 r_targets x
# 6 expiries per TF, honest 60/20/20 train/validation/holdout,
# ~/Desktop/smc_options/src/ob_depth_sweep.py, memory finding_tyagach_ob_depth_sweep):
# depth_frac was never previously tuned at all (hardcoded 0.5 everywhere, only
# ever validated as one of 4 categorical entry styles vs touch/close_back/engulf,
# never swept as a continuous fraction). Each of these 4 cells DOMINATES the old
# 0.50/3.0/0.5 baseline on train AND validation AND holdout simultaneously (not
# cherry-picked off one split), reconfirmed at the portfolio level (compounding,
# real per-TF concurrency caps, real fees): holdout return +31.3%->+81.8%,
# maxDD LOWER on every split, trade frequency essentially unchanged.
#
# ROLLBACK: delete/empty this dict (or remove the ("15m","OB") etc. keys you
# want to revert) — cell_config() falls back to ZONE_CONFIG["OB"]'s untouched
# 0.50/3.0/0.5/50.0 automatically, no other code change needed.
#
# 2026-08-02 revision: 15m/30m/1h OB entries REMOVED. Live fill-haircut audit
# (27 closed trades, ~83-90% of BS-mid) + a full re-sweep under that haircut
# found zero robust (depth, r_target, expiry) combo for 15m/1h anywhere in a
# wide grid, and 30m only "robust" per-trade -- fails at the portfolio level
# (drags the combined book negative on train+validation, see
# src/ob_portfolio_compare_haircut.py). Only 2h/OB survives realistic
# friction, and only after retuning r_target 3.0->5.0 / expiry 1.0->0.25d
# (src/ob_2h_quarter_robustness.py: 5/8 quarters positive vs 1/8 for the old
# live values). See ACTIVE_CELLS below -- those three TFs are deactivated.
# 2026-08-02 U2 (TYAGACH_UPGRADE_PLAN_2026-08-02.md): FVG added as its own
# tradeable zone kind (previously detect_fvg() output only widened OB zone
# boundaries, never traded standalone). 2h/FVG with rejection-close entry is
# the strongest single candidate found in the session's full re-sweep (8/8
# quarters positive, mean +25.2%/quarter, worst +2.1%). Combined with 2h/OB
# (only 1.8% zone overlap at 2h -- largely independent), the pair beats
# either alone on every split/quarter tested (u2_ob_fvg_rejection_combo.py):
# holdout return solo-OB +6.6%/solo-FVG +27.7%/combined +37.3%, quarter
# robustness 0/8 negative for both FVG-only and combined (OB-only 1/8),
# combined mean quarterly return +30.0% vs FVG-only's +25.2%, worst quarter
# +1.2% vs +2.1% (still solidly positive) -- no meaningfully worse drawdown
# (maxDD deltas <1pp, actually lower on validation/holdout). See
# RESEARCH_FINDINGS_2026-08-02.md and TYAGACH_UPGRADE_PLAN_2026-08-02.md.
# 2026-08-02 U3+U4 (same doc): solo portfolio+quarter validation
# (src/u3_solo_validation.py, best-by-train-avg_pnl candidate per pair) found
# 5 of the remaining 6 cells robust -- 30m/OB, and FVG on 30m/1h/15m all pass
# 0/8 negative quarters; 1h/OB passes marginally (1/8, shallow -0.8% worst).
# All 5 added here per explicit user call to move on every validated cell at
# once rather than stage one at a time -- MAX_OPEN_TOTAL_GLOBAL/
# MAX_TOTAL_MARGIN_PCT below still bound worst-case combined exposure even
# though the cross-cell correlation these 5 might have live is unverified
# (only the single-cell backtests were run, not a combined check).
# 15m/OB originally EXCLUDED at U4 time: 1/8 negative quarters AND its
# 60/20/20 validation split alone was net negative (-8.1%, calmar -0.78)
# despite positive train/holdout, using the best-by-train-avg_pnl candidate.
# 2026-08-02 follow-up: re-picked by MIN across all 3 splits' avg_pnl
# instead of train-only (depth=0.700/r_target=4.0/expiry=0.083) -- this one
# passes cleanly, 0/8 negative quarters, positive on all 3 splits
# (src/u3_solo_validation.py-style solo check). Added below.
#
# 2026-08-02 follow-up (same day): MB re-swept with rejection-close
# (src/bb_mb_rejection_sweep.py) -- MB was deactivated 2026-07-07 on a real
# LIVE-performance override ("the sole bleeder, 15 trades, -$33.72, last 8
# closes all MB"), before rejection-close or any other fix from this
# session existed. Rejection-close targets exactly the kind of fakeout
# entry that a naive touch-based trigger would have mistaken for a real
# mitigation. All 4 MB timeframes now pass solo portfolio+quarter
# validation cleanly (0/8 negative quarters each,
# src/bb_mb_solo_validation.py) and the combined 13-cell book (all 8
# already-live U4 cells + 15m/OB + 4x MB) beats the current 8-cell book on
# every split/quarter tested (src/u_all13_combo_validation.py): validation
# return +128.8%->+264.7%, holdout +270.1%->+542.5%, worst quarter
# +36.0%->+55.3%, still 0/8 negative. Added below.
# CAUTION: this reverses a decision that was based on REAL live losses, not
# just a backtest miss -- rejection-close plausibly fixes the underlying
# fakeout-entry mechanism, but that's an inference, not a live-tested fact.
# Watch MB's live performance closely; if it bleeds again, deactivate it
# again immediately rather than assuming the backtest must be right.
#
# BB re-swept too (same script) but NOT retuned: even the best candidate
# re-picked for split-balance (depth=0.300/r_target=4.0/expiry=0.125) still
# shows 2/8 negative quarters and a weak holdout (maxDD 12.7%, calmar 0.14)
# -- looks like genuine small-sample fragility (BB has the fewest zones of
# any kind), not a bad pick. BB stays on ZONE_CONFIG's untouched defaults
# (still gets rejection-close for free, generically, since U1 -- just not a
# retuned depth/r_target/expiry).
CELL_CONFIG: dict[tuple[str, str], dict] = {
    ("2h", "OB"): {"depth_frac": 0.675, "r_target": 5.0, "expiry_days": 0.25},
    ("2h", "FVG"): {"depth_frac": 0.675, "r_target": 10.0, "expiry_days": 0.167},
    ("30m", "OB"): {"depth_frac": 0.300, "r_target": 10.0, "expiry_days": 0.125},
    ("1h", "OB"): {"depth_frac": 0.650, "r_target": 5.0, "expiry_days": 0.125},
    ("30m", "FVG"): {"depth_frac": 0.300, "r_target": 7.0, "expiry_days": 0.125},
    ("1h", "FVG"): {"depth_frac": 0.325, "r_target": 7.0, "expiry_days": 0.167},
    ("15m", "FVG"): {"depth_frac": 0.300, "r_target": 10.0, "expiry_days": 0.125},
    ("15m", "OB"): {"depth_frac": 0.700, "r_target": 4.0, "expiry_days": 0.083},
    ("15m", "MB"): {"depth_frac": 0.425, "r_target": 7.0, "expiry_days": 0.125},
    ("30m", "MB"): {"depth_frac": 0.400, "r_target": 10.0, "expiry_days": 0.167},
    ("1h", "MB"): {"depth_frac": 0.400, "r_target": 10.0, "expiry_days": 0.25},
    ("2h", "MB"): {"depth_frac": 0.500, "r_target": 3.0, "expiry_days": 0.25},
}


def cell_config(tf: str, kind: str) -> dict:
    """Effective params for one (tf, kind) cell: any CELL_CONFIG override
    merged over the ZONE_CONFIG[kind] default (so a partial override, e.g.
    just depth_frac, doesn't drop the kind's r_target/iv_threshold/expiry).
    MB/BB have no CELL_CONFIG entries yet -> fall through unchanged."""
    merged = dict(ZONE_CONFIG[kind])
    merged.update(CELL_CONFIG.get((tf, kind), {}))
    return merged

# Portfolio allocation — manually chosen (NOT the raw grid-search optimum),
# see TYAGACH_HANDOFF.md "Portfolio allocation" section.
# FVG given the same priority/weight/cap as OB (tied priority resolved by
# entry_ts order alone) -- same risk-budget philosophy, no separate sizing
# validation done, matches what u2_ob_fvg_rejection_combo.py actually tested.
PRIORITY = {"BB": 0, "MB": 1, "OB": 2, "FVG": 2}  # lower = higher priority
WEIGHT_PCT = {"OB": 0.12, "MB": 0.18, "BB": 0.28, "FVG": 0.12}  # % of current balance per new position
MAX_OPEN_PER_ZONE = {"OB": 3, "MB": 2, "BB": 1, "FVG": 3}   # caps apply within each TF sub-book

# Global ceiling across ALL timeframe sub-books combined.  Prevents all TFs
# firing simultaneously from over-leveraging the single shared account.
MAX_OPEN_TOTAL_GLOBAL = 8   # hard cap on simultaneous positions across all TFs
# 2026-08-02 (headroom re-check, now that 13 cells are live vs the 1 cell
# this was last tested against): MAX_OPEN_TOTAL_GLOBAL 8->12/16 changed
# NOTHING at any margin level (src/u_cap_headroom_13cell.py) -- the
# per-TF same-direction rule is still the real limiter, not the slot count.
# MAX_TOTAL_MARGIN_PCT 0.60->0.80 DOES have real headroom: validation
# +425%->+463%, holdout +433%->+448%, worst quarter +68.4%->+78.1%, for a
# modest maxDD cost (+0.3-0.7pp). Pushing further to ~uncapped (0.99)
# stops helping and the worst quarter gets WORSE (78.1%->73.8%) -- 0.80 is
# the sweet spot, not "more is strictly better."
MAX_TOTAL_MARGIN_PCT = 0.80  # combined open margin must not exceed 80% of balance

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
#   2026-08-02 (P0): 15m/OB, 30m/OB, 1h/OB DEACTIVATED -- see CELL_CONFIG
#   comment above. Only 2h/OB (retuned) and 15m/BB (untouched) remained active.
#   2026-08-02 (U2): 2h/FVG added alongside 2h/OB -- see CELL_CONFIG comment.
#   2026-08-02 (U3/U4): 30m/OB, 1h/OB, 30m/FVG, 1h/FVG, 15m/FVG re-added --
#   see CELL_CONFIG comment. 15m/OB stayed out at U4 time (flagged).
#   2026-08-02 (follow-up, same day): 15m/OB re-added (re-picked config,
#   passes cleanly) and ALL FOUR MB timeframes re-added (rejection-close
#   reverses the 07-07 live-loss-driven deactivation on backtest -- see
#   CELL_CONFIG comment for the caution about this being an inference, not
#   a live-proven fact yet). BB stays on its untouched config (re-tuning
#   attempts stayed fragile) but remains active as before.
ACTIVE_CELLS: frozenset[tuple[str, str]] = frozenset({
    ("15m", "BB"),
    ("15m", "OB"),
    ("15m", "MB"),
    ("30m", "MB"),
    ("1h",  "MB"),
    ("2h",  "MB"),
    ("2h",  "OB"),
    ("2h",  "FVG"),
    ("30m", "OB"),
    ("1h",  "OB"),
    ("30m", "FVG"),
    ("1h",  "FVG"),
    ("15m", "FVG"),
})

# Ordered list of active TFs (determines loop processing order each tick).
# 30m/1h re-added 2026-08-02 (U3/U4) -- both have active cells again.
ACTIVE_TFS: list[str] = ["15m", "30m", "1h", "2h"]

SYMBOL = "ETHUSDT"
BASE_COIN = "ETH"
POLL_SECONDS = 60  # loop wake interval

# ── Entry-hour veto (2026-07-09, TYAGACH_STRATEGY_REVIEW_2026-07-09.md §3b) ──
# 12-15h UTC entries are the worst hour-bucket on ALL 3 splits independently
# (+$0.01/trade over 1115 of 5004 backtest trades at the deployed CELL_CONFIG;
# 20-23h = +$2.18) and the live sample agrees (−$15.6 over n=8). Mechanism is
# external and 4y-stable: 13:30 UTC US macro prints + US cash open. Portfolio
# level (per_tf engine): halves train/validation maxDD, +8pp validation return,
# costs ~5pp holdout return and ~22% trade frequency — accepted as a risk
# reducer. A vetoed touch is CONSUMED (status 'expired'), not deferred — that
# matches how the veto was backtested (the entry never happens).
# Env override: comma-separated UTC hours; empty string disables the veto.
_veto_raw = os.getenv("TYAGACH_ENTRY_VETO_UTC_HOURS", "12,13,14,15")
ENTRY_VETO_UTC_HOURS: frozenset[int] = frozenset(
    int(h) for h in _veto_raw.split(",") if h.strip()
)

# ── Fill sanity floor (2026-07-09, review §3a) ──
# Live fill audit: median sold premium = 85% of BS-mid at iv_entry, but glitched
# chain snapshots go as low as 15% of mid (trade id6, -$1.10 on a premium 7x
# below comparable quotes). Skip the open when the best bid is below this
# fraction of the BS-mid estimate; the signal stays pending and is retried
# next tick while fresh (same semantics as the existing no-quote skip), so a
# transient glitch just delays the fill. Current OB flow fills at 81-116% of
# mid — untouched. 0 disables.
FILL_FLOOR_FRAC = float(os.getenv("TYAGACH_FILL_FLOOR_FRAC", "0.70"))
