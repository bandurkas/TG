from __future__ import annotations
"""Item 3 of the "how do we raise profit" follow-up round: re-test
MAX_OPEN_TOTAL_GLOBAL / MAX_TOTAL_MARGIN_PCT headroom now that 13 cells are
live (was tested earlier this session against 2h/OB alone and rejected --
caps weren't the binding constraint, the per-TF same-direction rule was).
With 8x more cells now competing for the same global slot/margin budget,
that conclusion may no longer hold.

Sweeps both caps together (matching the live bot's actual two-gate check:
per_tf same-direction conflict -> per-zone cap -> global slot cap -> global
margin cap) against the full corrected-data 13-cell candidate pool, split
60/20/20 + quarter robustness at each level.
"""
import sys
import numpy as np

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
from portfolio import stats
import tyagach_samedir_ab as samedir_mod
import u_all13_combo_validation as sim

CAP_GRID = [
    (8, 0.60),    # live baseline
    (12, 0.60),
    (16, 0.60),
    (8, 0.80),
    (12, 0.80),
    (20, 0.80),
    (30, 0.99),   # effectively uncapped -- sanity ceiling
]


def run_splits(all_candidates, ts_ref):
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
    out = {}
    for split in ["train", "validation", "holdout"]:
        final, curve, closed = samedir_mod.simulate_tagged(by_split[split], "per_tf")
        s = stats(sim.STARTING_BALANCE, final, curve, len(closed))
        out[split] = s
    return out


def run_quarters(all_candidates, ts_ref, n_quarters=8):
    ts_start, ts_end = int(ts_ref[0]), int(ts_ref[-1])
    span = ts_end - ts_start
    edges = [ts_start + int(span * i / n_quarters) for i in range(n_quarters + 1)]
    rets = []
    for q in range(n_quarters):
        q_lo, q_hi = edges[q], edges[q + 1]
        q_cands = [(tf_, cand) for tf_, cand in all_candidates if q_lo <= cand.entry_idx < q_hi]
        final, curve, closed = samedir_mod.simulate_tagged(q_cands, "per_tf")
        s = stats(sim.STARTING_BALANCE, final, curve, len(closed))
        rets.append(s["total_return_pct"])
    return rets


def main():
    tf_cache = {}
    print("Building candidates for the full 13-cell set (corrected data)...")
    all_candidates = sim.build_all(sim.PROPOSED_CELLS, tf_cache)
    ts_ref = tf_cache["15m"][2]
    print(f"total signals: {len(all_candidates)}\n")

    orig_global, orig_margin = samedir_mod.MAX_OPEN_TOTAL_GLOBAL, samedir_mod.MAX_TOTAL_MARGIN_PCT
    try:
        print(f"{'global_cap':>10} {'margin_pct':>10} {'val_ret%':>10} {'hold_ret%':>10} "
              f"{'val_maxDD%':>11} {'hold_maxDD%':>12} {'neg_q':>6} {'worst_q%':>9}")
        print("-" * 90)
        for global_cap, margin_pct in CAP_GRID:
            samedir_mod.MAX_OPEN_TOTAL_GLOBAL = global_cap
            samedir_mod.MAX_TOTAL_MARGIN_PCT = margin_pct

            splits = run_splits(all_candidates, ts_ref)
            rets = run_quarters(all_candidates, ts_ref)
            n_neg = sum(1 for r in rets if r < 0)

            print(f"{global_cap:>10} {margin_pct:>10.2f} "
                  f"{splits['validation']['total_return_pct']:>10.1f} {splits['holdout']['total_return_pct']:>10.1f} "
                  f"{splits['validation']['max_dd_pct']:>11.1f} {splits['holdout']['max_dd_pct']:>12.1f} "
                  f"{n_neg:>6} {min(rets):>9.1f}")
    finally:
        samedir_mod.MAX_OPEN_TOTAL_GLOBAL = orig_global
        samedir_mod.MAX_TOTAL_MARGIN_PCT = orig_margin


if __name__ == "__main__":
    main()
