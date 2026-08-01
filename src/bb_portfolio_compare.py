from __future__ import annotations
"""Portfolio-level (compounding, real per-TF concurrency caps + margin cap,
real fees) check of the BB depth-sweep candidate found in bb_depth_sweep.py,
against the live baseline -- same discipline ob_portfolio_compare.py already
applies to OB candidates (feedback_backtest_methodology_pitfalls pitfall #4:
a per-trade average improvement can mislead once sizing/frequency/compounding
apply). OB side is held at its already-tuned live CELL_CONFIG in both arms
(ob_portfolio_compare.CANDIDATE_CFG) -- only BB's depth/r_target/expiry
changes between BASELINE and CANDIDATE, isolating the BB question exactly
like ob_portfolio_compare isolated OB.
"""
import sys
import numpy as np

sys.path.insert(0, "/Users/sabar/Desktop/smc_options/src")
import structure
from portfolio import stats
import tyagach_samedir_ab as ab
from tyagach_portfolio_multitf import TF_FILES, STARTING_BALANCE
from ob_portfolio_compare import build_candidates_for_tf as build_ob, CANDIDATE_CFG as OB_CFG
from bb_depth_sweep import detect_bb_zones, find_depth_entry, MAX_LOOKAHEAD, BPD, DAYS_PER_YEAR, IV_THRESHOLD
from dvol import load_dvol_aligned
import bs_pricer as bsp

DVOL_JSON = f"/Users/sabar/Desktop/smc_options/data/eth_dvol_1h_4y.json"

BB_BASELINE = dict(depth_frac=0.50, r_target=2.5, expiry_days=5.0)      # live
BB_CANDIDATE = dict(depth_frac=0.60, r_target=5.0, expiry_days=7.0)     # top train-ranked, beats baseline on all 3 splits


def build_bb_candidates(cfg: dict, cut1_ts: int, cut2_ts: int):
    path = TF_FILES["15m"]
    df = structure.load_csv(path)
    n = len(df)
    ts = df["ts_ms"].values
    zones = detect_bb_zones(df)
    iv_series = load_dvol_aligned(DVOL_JSON, df)
    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    depth_frac, rt, exp_days = cfg["depth_frac"], cfg["r_target"], cfg["expiry_days"]

    by_split = {"train": [], "validation": [], "holdout": []}
    for zone in zones:
        found = find_depth_entry(o, h, l, c, zone, n, depth_frac, MAX_LOOKAHEAD)
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
        expiry_bars = int(exp_days * BPD)
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
        elapsed_days = (exit_idx - entry_idx) / BPD
        T_remaining = max(0.0, (exp_days - elapsed_days) / DAYS_PER_YEAR)
        T_entry = exp_days / DAYS_PER_YEAR
        strike = entry_price
        premium = bsp.price(side, entry_price, strike, T_entry, iv0)
        value_exit = bsp.price(side, spot_exit, strike, T_remaining, iv0)
        pnl_per_unit = premium - value_exit

        from portfolio import Candidate
        cand = Candidate("BB", zone.direction, int(ts[entry_idx]), int(ts[exit_idx]), entry_price, pnl_per_unit)
        entry_ts = int(ts[entry_idx])
        if entry_ts < cut1_ts:
            by_split["train"].append(cand)
        elif entry_ts < cut2_ts:
            by_split["validation"].append(cand)
        else:
            by_split["holdout"].append(cand)
    return by_split


def build_combined(bb_cfg: dict, cut1_ts: int, cut2_ts: int, apply_veto: bool):
    from datetime import datetime, timezone
    ENTRY_VETO_UTC_HOURS = frozenset({12, 13, 14, 15})

    def in_veto(ts_ms):
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).hour in ENTRY_VETO_UTC_HOURS

    combined = {"train": [], "validation": [], "holdout": []}
    for tf in ["15m", "30m", "1h", "2h"]:
        by_split = build_ob(tf, OB_CFG[tf], cut1_ts, cut2_ts)
        for split, cands in by_split.items():
            for c in cands:
                if apply_veto and in_veto(c.entry_idx):
                    continue
                combined[split].append((tf, c))

    bb_by_split = build_bb_candidates(bb_cfg, cut1_ts, cut2_ts)
    for split, cands in bb_by_split.items():
        for c in cands:
            if apply_veto and in_veto(c.entry_idx):
                continue
            combined[split].append(("15m", c))
    return combined


def main():
    df15 = structure.load_csv(TF_FILES["15m"])
    n15 = len(df15)
    ts15 = df15["ts_ms"].values
    cut1_ts, cut2_ts = int(ts15[int(n15 * 0.6)]), int(ts15[int(n15 * 0.8)])
    span_days = (int(ts15[-1]) - int(ts15[0])) / 1000 / 86400
    split_days = {"train": span_days * 0.6, "validation": span_days * 0.2, "holdout": span_days * 0.2}

    print(f"{'config':<10} {'split':<12} {'n_closed':>8} {'trades/day':>10} {'return%':>10} "
          f"{'maxDD%':>7} {'calmar':>8} {'final$':>10}")
    print("-" * 82)
    for label, bb_cfg in [("BASELINE", BB_BASELINE), ("CANDIDATE", BB_CANDIDATE)]:
        combined = build_combined(bb_cfg, cut1_ts, cut2_ts, apply_veto=True)
        for split in ["train", "validation", "holdout"]:
            final, curve, closed = ab.simulate_tagged(combined[split], "per_tf")
            s = stats(STARTING_BALANCE, final, curve, len(closed))
            tpd = len(closed) / split_days[split]
            print(f"{label:<10} {split:<12} {s['n_closed']:>8} {tpd:>10.2f} {s['total_return_pct']:>+10.1f} "
                  f"{s['max_dd_pct']:>7.1f} {s['calmar']:>8.2f} {s['final_balance']:>10.1f}")
        print()


if __name__ == "__main__":
    main()
