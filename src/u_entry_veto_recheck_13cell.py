from __future__ import annotations
"""Item 4 of the "how do we raise profit" follow-up round: re-check the
entry-hour veto's cost/benefit now that 13 cells are live (was tuned
2026-07-09 against a single-cell-ish live book -- TYAGACH_STRATEGY_REVIEW_
2026-07-09.md §3b: costs ~22% trade frequency and ~5pp holdout return,
accepted as a risk reducer at the time). With 13 cells firing, the same
flat 4-hour block removes a much bigger slice of TOTAL opportunity --
worth re-weighing against the full corrected-data candidate pool.
"""
import sys
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
from portfolio import stats
import tyagach_samedir_ab as samedir_mod
import u_all13_combo_validation as sim

VETO_HOURS = frozenset({12, 13, 14, 15})


def in_veto_hour(ts_ms: int) -> bool:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).hour in VETO_HOURS


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
        tpd = len(closed) / split_days[split]
        out[split] = (s, tpd)
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
    print(f"total signals: {len(all_candidates)}")

    no_veto = all_candidates
    with_veto = [(tf_, cand) for tf_, cand in all_candidates if not in_veto_hour(cand.entry_idx)]
    print(f"dropped by veto: {len(no_veto) - len(with_veto)} ({(len(no_veto)-len(with_veto))/len(no_veto):.1%})\n")

    for label, cands in [("WITHOUT veto (12-15h UTC allowed)", no_veto),
                          ("WITH veto (current live behavior)", with_veto)]:
        print(f"=== {label} ===")
        splits = run_splits(cands, ts_ref)
        for split in ["train", "validation", "holdout"]:
            s, tpd = splits[split]
            print(f"{split:<12} n_closed={s['n_closed']:>6} trades/day={tpd:>6.2f} "
                  f"return={s['total_return_pct']:>+9.1f}% maxDD={s['max_dd_pct']:>6.1f}% calmar={s['calmar']:>7.2f}")
        rets = run_quarters(cands, ts_ref)
        n_neg = sum(1 for r in rets if r < 0)
        print(f"quarters: {n_neg}/8 negative, mean {np.mean(rets):+.1f}%, worst {min(rets):+.1f}%\n")


if __name__ == "__main__":
    main()
