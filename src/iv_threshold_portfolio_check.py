from __future__ import annotations
"""Portfolio+quarter check for the IV-threshold resweep's per-trade winners
(iv_threshold_resweep.py) -- every FVG/MB cell showed a "better" (higher)
IV threshold on train avg_pnl, but per-trade robust != portfolio robust,
proven repeatedly this session. Rebuilds the full 13-cell book with the
NEW per-cell IV thresholds for FVG/MB (OB=50/BB=55 untouched, not part of
this resweep) and compares against the CURRENT all-iv=50(FVG)/50(MB) book.
"""
import sys
import numpy as np

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
from portfolio import stats
import tyagach_samedir_ab as samedir_mod
import u_all13_combo_validation as sim

# per-trade winners from iv_threshold_resweep.py
NEW_IV = {
    ("FVG", "2h"): 60.0, ("FVG", "1h"): 70.0, ("FVG", "30m"): 55.0, ("FVG", "15m"): 60.0,
    ("MB", "2h"): 60.0, ("MB", "1h"): 55.0, ("MB", "30m"): 60.0, ("MB", "15m"): 70.0,
}
CURRENT_IV = {("FVG", tf): 50.0 for tf in ["2h", "1h", "30m", "15m"]}
CURRENT_IV.update({("MB", tf): 50.0 for tf in ["2h", "1h", "30m", "15m"]})


def build_all_with_iv(iv_overrides, tf_cache):
    all_candidates = []
    for (tf, kind), cfg in sim.PROPOSED_CELLS.items():
        if tf not in tf_cache:
            df = sim.structure.load_csv(sim.TF_FILES[tf])
            n = len(df)
            ts = df["ts_ms"].values
            iv_series = sim.load_dvol_aligned(sim.DVOL_JSON, df)
            o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
            tf_cache[tf] = (df, n, ts, iv_series, o, h, l, c)
        df, n, ts, iv_series, o, h, l, c = tf_cache[tf]
        iv_thresh = iv_overrides.get((kind, tf), sim.IV_THRESHOLD[kind])
        cfg_with_iv = dict(cfg)
        orig_get = sim.IV_THRESHOLD.get(kind)
        sim.IV_THRESHOLD[kind] = iv_thresh  # build_candidates reads sim.IV_THRESHOLD[kind] internally
        try:
            all_candidates.extend(sim.build_candidates(kind, tf, cfg_with_iv, df, n, ts, iv_series, o, h, l, c))
        finally:
            sim.IV_THRESHOLD[kind] = orig_get
    return all_candidates


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
    for label, iv_overrides in [("CURRENT (iv=50 everywhere for FVG/MB)", CURRENT_IV),
                                 ("NEW (per-trade-winning thresholds)", NEW_IV)]:
        candidates = build_all_with_iv(iv_overrides, tf_cache)
        ts_ref = tf_cache["15m"][2]
        print(f"=== {label} ===  total signals: {len(candidates)}")
        splits = run_splits(candidates, ts_ref)
        for split in ["train", "validation", "holdout"]:
            s, tpd = splits[split]
            print(f"{split:<12} n_closed={s['n_closed']:>6} trades/day={tpd:>6.2f} "
                  f"return={s['total_return_pct']:>+9.1f}% maxDD={s['max_dd_pct']:>6.1f}% calmar={s['calmar']:>7.2f}")
        rets = run_quarters(candidates, ts_ref)
        n_neg = sum(1 for r in rets if r < 0)
        print(f"quarters: {n_neg}/8 negative, mean {np.mean(rets):+.1f}%, worst {min(rets):+.1f}%\n")


if __name__ == "__main__":
    main()
