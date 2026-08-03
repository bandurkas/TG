from __future__ import annotations
"""Portfolio combo-check for per-position trailing profit-lock
(trailing_profit_lock_sweep.py's per-cell finding): does the win survive
once all 12 cells compete for the SAME margin/priority/conflict slots in
the real portfolio engine, instead of being evaluated in isolation?

Per-cell sweep found a clean (val AND holdout, robustness preserved) win
on 8/12 cells; 4 showed no benefit (2h/OB, 2h/FVG -- too little time to
expiry, matches fvg_trail_to_breakeven's earlier finding; 30m/MB, 1h/MB --
mixed/negative) and are left OFF (arm=1e9, never fires) here.

VARIANT best (arm_frac, trail_frac) per cell, from the per-cell sweep:
  1h/OB    0.5/0.5   30m/OB   0.7/0.3   30m/FVG  0.3/0.7
  15m/FVG  0.5/0.3   15m/MB   0.3/0.5   15m/OB   0.5/0.5
  1h/FVG   0.3/0.3   2h/MB    0.5/0.5

BASELINE = current live (post 2026-08-03 TP-retune/BB-deactivation deploy
`ec00ae5`), no trailing anywhere.

Run: python3 trailing_profit_lock_combo_check.py
"""
import sys
import numpy as np

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
from portfolio import stats
from tyagach_samedir_ab import simulate_tagged
from tyagach_portfolio_multitf import STARTING_BALANCE
from trailing_profit_lock_sweep import LIVE_CELLS, load_tf, build_candidates_trail, N_QUARTERS

OFF = (1e9, 0.5)  # arm never reached -> identical to plain r_target exit
VARIANT_PARAMS = {
    ("1h", "OB"): (0.1, 0.1),
    ("30m", "OB"): (0.7, 0.3),
    ("30m", "FVG"): (0.2, 0.7),
    ("15m", "FVG"): (0.5, 0.3),
    ("15m", "MB"): (0.3, 0.5),
    ("15m", "OB"): (0.5, 0.5),
    ("1h", "FVG"): (0.1, 0.3),
    ("2h", "MB"): (0.5, 0.5),
    # left OFF: ("2h","OB"), ("2h","FVG"), ("30m","MB"), ("1h","MB")
}


def build_all(params, tf_cache):
    all_candidates = []
    for (tf, kind), cfg in LIVE_CELLS.items():
        if tf not in tf_cache:
            tf_cache[tf] = load_tf(tf)
        df, n, ts, iv_series, o, h, l, c, atr = tf_cache[tf]
        arm_frac, trail_frac = params.get((tf, kind), OFF)
        cands, meta = build_candidates_trail(tf, kind, cfg, df, n, ts, iv_series, o, h, l, c, atr, arm_frac, trail_frac)
        all_candidates.extend(cands)
    return all_candidates


def run_splits(label, all_candidates, ts_ref):
    n = len(ts_ref)
    cut1_ts, cut2_ts = int(ts_ref[int(n * 0.6)]), int(ts_ref[int(n * 0.8)])
    span_days = (int(ts_ref[-1]) - int(ts_ref[0])) / 1000 / 86400
    split_days = {"train": span_days * 0.6, "validation": span_days * 0.2, "holdout": span_days * 0.2}
    by_split = {"train": [], "validation": [], "holdout": []}
    for tf_, cand in all_candidates:
        if cand.entry_idx < cut1_ts:
            by_split["train"].append((tf_, cand))
        elif cand.entry_idx < cut2_ts:
            by_split["validation"].append((tf_, cand))
        else:
            by_split["holdout"].append((tf_, cand))
    print(f"\n--- {label}: 60/20/20 portfolio ---")
    for split in ["train", "validation", "holdout"]:
        final, curve, closed = simulate_tagged(by_split[split], "per_tf")
        s = stats(STARTING_BALANCE, final, curve, len(closed))
        tpd = len(closed) / split_days[split]
        print(f"{split:<12} n_closed={s['n_closed']:>6} trades/day={tpd:>6.2f} "
              f"return={s['total_return_pct']:>+8.1f}% maxDD={s['max_dd_pct']:>6.1f}% calmar={s['calmar']:>7.2f}")


def run_quarters(label, all_candidates, ts_ref):
    ts_start, ts_end = int(ts_ref[0]), int(ts_ref[-1])
    span = ts_end - ts_start
    edges = [ts_start + int(span * i / N_QUARTERS) for i in range(N_QUARTERS + 1)]
    rets, dds = [], []
    for q in range(N_QUARTERS):
        q_lo, q_hi = edges[q], edges[q + 1]
        q_cands = [(tf_, cand) for tf_, cand in all_candidates if q_lo <= cand.entry_idx < q_hi]
        final, curve, closed = simulate_tagged(q_cands, "per_tf")
        s = stats(STARTING_BALANCE, final, curve, len(closed))
        rets.append(s["total_return_pct"])
        dds.append(s["max_dd_pct"])
    n_negative = sum(1 for r in rets if r < 0)
    print(f"--- {label}: quarters --- {n_negative}/{N_QUARTERS} negative, mean {np.mean(rets):+.1f}%, "
          f"worst {min(rets):+.1f}%, maxDD-worst {max(dds):.1f}%")


def main():
    tf_cache = {}
    baseline = build_all({}, tf_cache)  # empty params -> everything OFF -> current live
    ts_ref = tf_cache["15m"][2]
    run_splits("BASELINE (current live, no trailing)", baseline, ts_ref)
    run_quarters("BASELINE (current live, no trailing)", baseline, ts_ref)

    variant = build_all(VARIANT_PARAMS, tf_cache)
    run_splits("VARIANT (per-position trailing on 8/12 cells)", variant, ts_ref)
    run_quarters("VARIANT (per-position trailing on 8/12 cells)", variant, ts_ref)


if __name__ == "__main__":
    main()
