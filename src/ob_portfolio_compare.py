from __future__ import annotations
"""Portfolio-level (compounding, real per-TF concurrency caps) comparison of
current live OB config vs the per-TF candidates found by ob_depth_sweep.py's
"dominates baseline on all 3 splits" table.

Per feedback_backtest_methodology_pitfalls pitfall #4: a per-trade average
improvement can still mislead on sizing/frequency once real concurrency caps
and compounding are applied -- always re-check at the portfolio level before
trusting an isolated sweep's numbers. Reuses tyagach_samedir_ab.simulate_tagged
(the per-TF same-direction-conflict scope, confirmed 2026-07-05 to be what the
LIVE bot actually does, not the older global-conflict portfolio.py behavior).

OB only, all 4 TFs (matches live ACTIVE_CELLS as of commit 48167c7). BB/MB
excluded entirely to isolate the OB question.
"""
import sys
import numpy as np

sys.path.insert(0, "/Users/sabar/Desktop/smc_options/src")
import structure
from dvol import load_dvol_aligned
import bs_pricer as bsp
from portfolio import Candidate, PRIORITY, stats
from tyagach_samedir_ab import simulate_tagged
from tyagach_portfolio_multitf import (
    TF_FILES, WEIGHT_PCT, MAX_OPEN_PER_ZONE, MAX_OPEN_TOTAL_GLOBAL,
    LOT_SIZE, MARGIN_PCT, FEE_RATE, FEE_CAP_PCT, STARTING_BALANCE, DVOL_JSON,
)
from ob_depth_sweep import detect_ob_zones, find_depth_entry, MAX_LOOKAHEAD, BPD, DAYS_PER_YEAR, IV_THRESHOLD

# current live OB params (same for every TF today)
BASELINE_CFG = {tf: dict(depth_frac=0.50, r_target=3.0, expiry_days=0.5) for tf in TF_FILES}

# best-per-TF candidates: depth_frac/expiry_days from the 3264-combo grid, r_target
# refined by a follow-up wide sweep (3..15) after noticing r_target=5.0 sat at the
# first grid's boundary for several TFs. 2h's holdout PEAKS at r_target=3.0 (live
# value, unchanged) and monotonically DECLINES past it -- the initial r=5.0 pick for
# 2h was a boundary artifact, corrected here after checking the wider range.
CANDIDATE_CFG = {
    "15m": dict(depth_frac=0.575, r_target=10.0, expiry_days=0.25),
    "30m": dict(depth_frac=0.500, r_target=7.0, expiry_days=0.25),
    "1h":  dict(depth_frac=0.325, r_target=8.0, expiry_days=0.75),   # noisiest TF, smallest n
    "2h":  dict(depth_frac=0.675, r_target=3.0, expiry_days=1.00),   # r_target unchanged from live
}


def build_candidates_for_tf(tf: str, cfg: dict, cut1_ts: int, cut2_ts: int):
    path = TF_FILES[tf]
    bpd = BPD[tf]
    max_lookahead = MAX_LOOKAHEAD[tf]
    df = structure.load_csv(path)
    n = len(df)
    ts = df["ts_ms"].values
    zones = detect_ob_zones(df)
    iv_series = load_dvol_aligned(DVOL_JSON, df)

    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    depth_frac, rt, exp_days = cfg["depth_frac"], cfg["r_target"], cfg["expiry_days"]

    by_split = {"train": [], "validation": [], "holdout": []}
    for zone in zones:
        found = find_depth_entry(o, h, l, c, zone, n, depth_frac, max_lookahead)
        if found is None:
            continue
        entry_idx, entry_price, stop_price = found
        if abs(entry_price - stop_price) <= 0:
            continue
        iv0 = iv_series[entry_idx]
        if np.isnan(iv0) or iv0 * 100 <= IV_THRESHOLD:
            continue

        is_long = zone.direction == "bullish"
        risk = abs(entry_price - stop_price)
        tp_price = entry_price + rt * risk if is_long else entry_price - rt * risk
        expiry_bars = int(exp_days * bpd)
        expiry_idx = min(n - 1, entry_idx + expiry_bars)
        exit_idx = expiry_idx
        for j in range(entry_idx + 1, expiry_idx + 1):
            hit_sl = (l[j] <= stop_price) if is_long else (h[j] >= stop_price)
            hit_tp = (h[j] >= tp_price) if is_long else (l[j] <= tp_price)
            if hit_sl or hit_tp:
                exit_idx = j
                break
        spot_exit = c[exit_idx]
        side = "P" if is_long else "C"
        elapsed_days = (exit_idx - entry_idx) / bpd
        T_remaining = max(0.0, (exp_days - elapsed_days) / DAYS_PER_YEAR)
        T_entry = exp_days / DAYS_PER_YEAR
        strike = entry_price
        premium = bsp.price(side, entry_price, strike, T_entry, iv0)
        value_exit = bsp.price(side, spot_exit, strike, T_remaining, iv0)
        pnl_per_unit = premium - value_exit

        entry_ts, exit_ts = int(ts[entry_idx]), int(ts[exit_idx])
        cand = Candidate("OB", zone.direction, entry_ts, exit_ts, entry_price, pnl_per_unit)
        if entry_ts < cut1_ts:
            by_split["train"].append(cand)
        elif entry_ts < cut2_ts:
            by_split["validation"].append(cand)
        else:
            by_split["holdout"].append(cand)
    return by_split


def run(cfg_by_tf: dict, cut1_ts: int, cut2_ts: int, span_days: float):
    split_days = {"train": span_days * 0.6, "validation": span_days * 0.2, "holdout": span_days * 0.2}
    combined = {"train": [], "validation": [], "holdout": []}
    for tf, cfg in cfg_by_tf.items():
        by_split = build_candidates_for_tf(tf, cfg, cut1_ts, cut2_ts)
        for split in combined:
            combined[split].extend((tf, c) for c in by_split[split])

    rows = []
    for split in ["train", "validation", "holdout"]:
        final, curve, closed = simulate_tagged(combined[split], "per_tf")
        s = stats(STARTING_BALANCE, final, curve, len(closed))
        tpd = len(closed) / split_days[split]
        rows.append((split, s["n_closed"], tpd, s["total_return_pct"], s["max_dd_pct"], s["calmar"], s["final_balance"]))
    return rows


def main():
    df15 = structure.load_csv(TF_FILES["15m"])
    n15 = len(df15)
    ts15 = df15["ts_ms"].values
    cut1_ts, cut2_ts = int(ts15[int(n15 * 0.6)]), int(ts15[int(n15 * 0.8)])
    span_days = (int(ts15[-1]) - int(ts15[0])) / 1000 / 86400

    print(f"{'config':<10} {'split':<12} {'n_closed':>8} {'trades/day':>10} {'return%':>10} "
          f"{'maxDD%':>7} {'calmar':>8} {'final$':>10}")
    print("-" * 82)
    for name, cfg in [("BASELINE", BASELINE_CFG), ("CANDIDATE", CANDIDATE_CFG)]:
        for split, n_closed, tpd, ret, dd, calmar, final in run(cfg, cut1_ts, cut2_ts, span_days):
            print(f"{name:<10} {split:<12} {n_closed:>8} {tpd:>10.2f} {ret:>+10.1f} {dd:>7.1f} {calmar:>8.2f} {final:>10.1f}")
        print()


if __name__ == "__main__":
    main()
