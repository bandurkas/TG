from __future__ import annotations
"""Does the current 12-cell live config's headline backtest return (validation
+425%->+463%, holdout +433%->+448%, 0/8 negative quarters per CELL_CONFIG's
comments in tyagach/services/config.py) survive once position sizing is
de-coupled from the ever-growing backtest balance?

Motivation: live paper balance is DOWN -4.27% over 47 closed trades / 40 days
(2026-06-25 to 2026-08-05), and the worst single week (2026-31, -$42.08) is
the week that ran the FULLY tuned config (trailing + TP retune + MB
rejection-close + IV=45) -- exactly opposite of what the backtest's claimed
numbers would predict. Same compounding-artifact concern already flagged in
simulate_since_inception.py (an earlier, now-stale 8-cell snapshot) is
retested here against the CURRENT, full 12-cell CELL_CONFIG (copied from
tyagach/services/config.py as of 2026-08-05, trailing included on the 8 cells
where it's actually deployed).

Position sizing today is `budget = weight_pct * CURRENT balance` (portfolio.py
docstring: "compounding"). In a multi-year backtest that starts at $2000 and
claims +400%+ returns, the simulated balance grows past $8-10k, so later-dated
signals get sized several times larger than the SAME signal would be sized
today -- a feedback loop where reported "return" is partly a mechanical
consequence of unbounded compounding + correlated bets, not evidence of
repeatable per-trade edge. This script re-runs the SAME candidates (same
signals, same entries/exits, same fees) through a capacity-capped sizing rule
(`budget = weight_pct * min(balance, CAP)`) across a grid of caps, including
CAP=STARTING_BALANCE (no compounding at all -- closest to "what live is
actually doing" since the live account has barely moved from $2000).

Two views:
  1. Full history since inception (single equity curve, matches how
     simulate_since_inception.py reported it) -- shows the raw sensitivity.
  2. The ACTUAL train/60/val/20/holdout/20 split the "+425%/+433%" claim in
     config.py's comments came from -- apples-to-apples with the number being
     tested, not just a vibe check on the whole history.

Run: python3 overfit_capacity_check.py
"""
import sys
import numpy as np

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
from portfolio import Candidate, stats, PRIORITY as BASE_PRIORITY
from tp_retarget_sweep import (
    build_candidates as build_baseline, load_tf, N_QUARTERS,
)
from trailing_profit_lock_sweep import build_candidates_trail

STARTING_BALANCE = 2000.0
LOT_SIZE = 0.10
MARGIN_PCT = 0.15
FEE_RATE = 0.0003
FEE_CAP_PCT = 0.125
PRIORITY = {"BB": 0, "MB": 1, "OB": 2, "FVG": 2}
WEIGHT_PCT = {"OB": 0.12, "MB": 0.18, "BB": 0.28, "FVG": 0.12}
MAX_OPEN_PER_ZONE = {"OB": 3, "MB": 2, "BB": 1, "FVG": 3}
MAX_OPEN_TOTAL_GLOBAL = 8
MAX_TOTAL_MARGIN_PCT = 0.80

# CELL_CONFIG + TRAIL_PARAMS copied verbatim from tyagach/services/config.py
# as read 2026-08-05 (commit fe40cd9 HEAD). 12 active cells, no BB (deactivated
# 2026-08-03).
LIVE_CELLS = {
    ("2h", "OB"):   dict(kind="OB",  depth_frac=0.675, r_target=3.75, expiry_days=0.25),
    ("2h", "FVG"):  dict(kind="FVG", depth_frac=0.675, r_target=10.0, expiry_days=0.167),
    ("30m", "OB"):  dict(kind="OB",  depth_frac=0.300, r_target=5.0,  expiry_days=0.125),
    ("1h", "OB"):   dict(kind="OB",  depth_frac=0.650, r_target=5.0,  expiry_days=0.125),
    ("30m", "FVG"): dict(kind="FVG", depth_frac=0.300, r_target=3.5,  expiry_days=0.125),
    ("1h", "FVG"):  dict(kind="FVG", depth_frac=0.325, r_target=7.0,  expiry_days=0.167),
    ("15m", "FVG"): dict(kind="FVG", depth_frac=0.300, r_target=10.0, expiry_days=0.125),
    ("15m", "OB"):  dict(kind="OB",  depth_frac=0.700, r_target=4.0,  expiry_days=0.083),
    ("15m", "MB"):  dict(kind="MB",  depth_frac=0.425, r_target=2.1,  expiry_days=0.125),
    ("30m", "MB"):  dict(kind="MB",  depth_frac=0.400, r_target=10.0, expiry_days=0.167),
    ("1h", "MB"):   dict(kind="MB",  depth_frac=0.400, r_target=10.0, expiry_days=0.25),
    ("2h", "MB"):   dict(kind="MB",  depth_frac=0.500, r_target=3.0,  expiry_days=0.25),
}
TRAIL_PARAMS = {
    ("1h", "OB"):    (0.1, 0.1),
    ("30m", "OB"):   (0.7, 0.3),
    ("30m", "FVG"):  (0.2, 0.7),
    ("15m", "FVG"):  (0.5, 0.3),
    ("15m", "MB"):   (0.3, 0.5),
    ("15m", "OB"):   (0.5, 0.5),
    ("1h", "FVG"):   (0.1, 0.3),
    ("2h", "MB"):    (0.5, 0.5),
}


def _fee(notional, premium_total):
    return min(notional * FEE_RATE, abs(premium_total) * FEE_CAP_PCT)


def simulate_capped(tagged, capacity_cap):
    """budget = weight_pct * min(balance, capacity_cap). capacity_cap=None ==
    uncapped compounding (today's live sizing rule). capacity_cap=STARTING_
    BALANCE == fixed $ per position, balance never influences sizing."""
    order = sorted(tagged, key=lambda tc: (tc[1].entry_idx, PRIORITY[tc[1].zone_kind]))
    balance = STARTING_BALANCE
    open_positions = []  # (tf, kind, direction, exit_idx, num_units, ppu, notional)
    equity_curve = [(0, balance)]
    closed = []

    def close_due(up_to_idx):
        nonlocal balance
        still = []
        for p in sorted(open_positions, key=lambda p: p[3]):
            if p[3] <= up_to_idx:
                tf, kind, direction, exit_idx, num_units, ppu, notional = p
                gross = ppu * num_units
                premium_total = abs(ppu) * num_units
                net = gross - 2 * _fee(notional, premium_total)
                balance += net
                closed.append((tf, kind, p[0], net))
                equity_curve.append((exit_idx, balance))
            else:
                still.append(p)
        open_positions[:] = still

    for tf, c in order:
        close_due(c.entry_idx)
        conflict = any(p[0] == tf and p[2] == c.direction for p in open_positions)
        if conflict:
            continue
        per_zone = sum(1 for p in open_positions if p[0] == tf and p[1] == c.zone_kind)
        if per_zone >= MAX_OPEN_PER_ZONE.get(c.zone_kind, 0):
            continue
        if len(open_positions) >= MAX_OPEN_TOTAL_GLOBAL:
            continue
        if balance <= 0:
            continue

        sizing_balance = min(balance, capacity_cap) if capacity_cap is not None else balance
        budget = WEIGHT_PCT.get(c.zone_kind, 0.0) * sizing_balance
        margin_per_lot = LOT_SIZE * c.spot_entry * MARGIN_PCT
        n_lots = int(budget // margin_per_lot) if margin_per_lot > 0 else 0
        if n_lots < 1:
            continue
        num_units = n_lots * LOT_SIZE
        notional = num_units * c.spot_entry
        margin_required = n_lots * margin_per_lot
        total_margin = sum(p[6] * MARGIN_PCT for p in open_positions)
        if total_margin + margin_required > balance * MAX_TOTAL_MARGIN_PCT:
            continue

        open_positions.append((tf, c.zone_kind, c.direction, c.exit_idx, num_units, c.pnl_per_unit, notional))

    if order:
        close_due(order[-1][1].exit_idx + 1)
    return balance, equity_curve, closed


def build_all_candidates():
    """Returns (all_tagged, ts_by_tf) -- all 12 cells' candidates tagged with
    tf, using trailing where TRAIL_PARAMS defines it, plain r_target otherwise
    -- exactly matches what's actually deployed today."""
    all_tagged = []
    ts_by_tf = {}
    tf_cache = {}
    for (tf, kind), cfg in LIVE_CELLS.items():
        if tf not in tf_cache:
            tf_cache[tf] = load_tf(tf)
        df, n, ts, iv_series, o, h, l, c, atr = tf_cache[tf]
        ts_by_tf[tf] = ts
        if (tf, kind) in TRAIL_PARAMS:
            arm, trail = TRAIL_PARAMS[(tf, kind)]
            cands, _meta = build_candidates_trail(tf, kind, cfg, df, n, ts, iv_series, o, h, l, c, atr, arm, trail)
        else:
            cands, _meta = build_baseline(tf, kind, cfg, df, n, ts, iv_series, o, h, l, c, atr, cfg["r_target"])
        all_tagged.extend(cands)
    return all_tagged, ts_by_tf


CAP_GRID = [None, 50_000, 20_000, 10_000, 5_000, 2_000]


def run_since_inception(all_tagged):
    print("=== View 1: full history since inception, single equity curve ===")
    print(f"{'cap':>10} {'n_closed':>9} {'final_$':>12} {'return%':>10} {'maxDD%':>7} {'calmar':>8}")
    for cap in CAP_GRID:
        final, curve, closed = simulate_capped(all_tagged, cap)
        s = stats(STARTING_BALANCE, final, curve, len(closed))
        cap_label = "uncapped*" if cap is None else f"${cap:,}"
        print(f"{cap_label:>10} {s['n_closed']:>9} {s['final_balance']:>12,.1f} "
              f"{s['total_return_pct']:>+10.1f} {s['max_dd_pct']:>7.1f} {s['calmar']:>8.2f}")
    print("* uncapped == today's actual live sizing rule (budget = weight_pct * current balance)")
    print("  $2,000 cap == fixed-$ per position, balance never inflates sizing (closest to what the")
    print("  live account, which has barely moved off $2000, is actually experiencing)\n")


def run_splits(all_tagged, ts_by_tf):
    # same 60/20/20 cutpoints tp_retarget_sweep.run_splits uses, keyed off 15m
    # (the reference TF used everywhere else in this codebase for cut timestamps)
    ts15 = ts_by_tf["15m"]
    n15 = len(ts15)
    cut1_ts, cut2_ts = int(ts15[int(n15 * 0.6)]), int(ts15[int(n15 * 0.8)])

    by_split = {"train": [], "validation": [], "holdout": []}
    for tf, cand in all_tagged:
        if cand.entry_idx < cut1_ts:
            by_split["train"].append((tf, cand))
        elif cand.entry_idx < cut2_ts:
            by_split["validation"].append((tf, cand))
        else:
            by_split["holdout"].append((tf, cand))

    print("=== View 2: same train/validation/holdout split the config.py headline numbers cite ===")
    for split in ["train", "validation", "holdout"]:
        print(f"\n--- {split} (n_candidates={len(by_split[split])}) ---")
        print(f"{'cap':>10} {'n_closed':>9} {'final_$':>12} {'return%':>10} {'maxDD%':>7} {'calmar':>8}")
        for cap in CAP_GRID:
            final, curve, closed = simulate_capped(by_split[split], cap)
            s = stats(STARTING_BALANCE, final, curve, len(closed))
            cap_label = "uncapped" if cap is None else f"${cap:,}"
            print(f"{cap_label:>10} {s['n_closed']:>9} {s['final_balance']:>12,.1f} "
                  f"{s['total_return_pct']:>+10.1f} {s['max_dd_pct']:>7.1f} {s['calmar']:>8.2f}")


def main():
    all_tagged, ts_by_tf = build_all_candidates()
    print(f"Loaded {len(all_tagged)} raw candidates across {len(LIVE_CELLS)} cells.\n")
    run_since_inception(all_tagged)
    run_splits(all_tagged, ts_by_tf)


if __name__ == "__main__":
    main()
