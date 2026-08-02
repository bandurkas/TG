from __future__ import annotations
"""Combined portfolio+quarter check of the FULL proposed cell set (13
cells) before shipping: the 8 already-live U4 cells + the 5 new candidates
found this round -- 15m/OB (re-picked for validation/holdout robustness,
not just train avg_pnl) and all 4 MB timeframes (rejection-close reverses
the 2026-07-07 live-performance deactivation on backtest). Each was only
solo-validated so far; per-trade/solo-portfolio robust != combined-portfolio
robust, proven repeatedly this session. BB stays at its current live config
(untuned depth/r_target/expiry -- retuning it still left 2/8 quarters
negative even after re-picking for balance, likely genuine small-sample
fragility, not a bad pick) but IS included here since it's live today.
"""
import sys
import numpy as np

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
import structure
import ob as ob_mod
import bb as bb_mod
import mb as mb_mod
from zones import build_zones
from dvol import load_dvol_aligned
import bs_pricer as bsp
from portfolio import Candidate, stats
from tyagach_samedir_ab import simulate_tagged
from tyagach_portfolio_multitf import STARTING_BALANCE
from ob_depth_sweep import detect_ob_zones, MAX_LOOKAHEAD, BPD, DAYS_PER_YEAR
from fvg_depth_sweep import detect_fvg_zones
from fvg_rejection_entry import find_rejection_entry

DATA_DIR = "/Users/styserg/Desktop/Tyagach/data"
DVOL_JSON = f"{DATA_DIR}/eth_dvol_1h_4y.json"
TF_FILES = {
    "15m": f"{DATA_DIR}/eth_15m.csv", "30m": f"{DATA_DIR}/eth_30m.csv",
    "1h": f"{DATA_DIR}/eth_1h_rs.csv", "2h": f"{DATA_DIR}/eth_2h.csv",
}
HAIRCUT = 0.85
SWING_ORDER = 3
N_QUARTERS = 8
IV_THRESHOLD = {"OB": 50.0, "FVG": 50.0, "BB": 55.0, "MB": 50.0}

# Full proposed live set (13 cells): 8 already-shipped (U4) + 5 new this round.
PROPOSED_CELLS = {
    ("15m", "BB"):  dict(depth_frac=0.500, r_target=2.5,  expiry_days=5.0),    # unchanged live
    ("15m", "OB"):  dict(depth_frac=0.700, r_target=4.0,  expiry_days=0.083),  # NEW re-pick
    ("15m", "MB"):  dict(depth_frac=0.425, r_target=7.0,  expiry_days=0.125), # NEW
    ("30m", "MB"):  dict(depth_frac=0.400, r_target=10.0, expiry_days=0.167), # NEW
    ("1h",  "MB"):  dict(depth_frac=0.400, r_target=10.0, expiry_days=0.25),  # NEW
    ("2h",  "MB"):  dict(depth_frac=0.500, r_target=3.0,  expiry_days=0.25),  # NEW
    ("2h",  "OB"):  dict(depth_frac=0.675, r_target=5.0,  expiry_days=0.25),
    ("2h",  "FVG"): dict(depth_frac=0.675, r_target=10.0, expiry_days=0.167),
    ("30m", "OB"):  dict(depth_frac=0.300, r_target=10.0, expiry_days=0.125),
    ("1h",  "OB"):  dict(depth_frac=0.650, r_target=5.0,  expiry_days=0.125),
    ("30m", "FVG"): dict(depth_frac=0.300, r_target=7.0,  expiry_days=0.125),
    ("1h",  "FVG"): dict(depth_frac=0.325, r_target=7.0,  expiry_days=0.167),
    ("15m", "FVG"): dict(depth_frac=0.300, r_target=10.0, expiry_days=0.125),
}
# for comparison: currently-live 8-cell set only
CURRENT_8_CELLS = {k: v for k, v in PROPOSED_CELLS.items()
                   if k not in {("15m", "OB"), ("15m", "MB"), ("30m", "MB"), ("1h", "MB"), ("2h", "MB")}}

import portfolio as portfolio_mod
import tyagach_samedir_ab as samedir_mod
samedir_mod.PRIORITY = {**portfolio_mod.PRIORITY, "FVG": 2}
samedir_mod.WEIGHT_PCT = {**samedir_mod.WEIGHT_PCT, "FVG": samedir_mod.WEIGHT_PCT["OB"]}
samedir_mod.MAX_OPEN_PER_ZONE = {**samedir_mod.MAX_OPEN_PER_ZONE, "FVG": samedir_mod.MAX_OPEN_PER_ZONE["OB"]}


def detect_zones(kind, df):
    if kind == "OB":
        return detect_ob_zones(df)
    if kind == "FVG":
        return detect_fvg_zones(df)
    swings = structure.detect_swings(df, order=SWING_ORDER)
    swings, events = structure.label_and_track(df, swings)
    fvgs = structure.detect_fvg(df)
    obs = ob_mod.detect_ob(df, swings, fvgs)
    if kind == "BB":
        bbs = bb_mod.detect_bb(df, obs, events)
        return build_zones([], bbs, [])
    mbs = mb_mod.detect_mb(df, events, obs)
    return build_zones([], [], mbs)


def build_candidates(kind, tf, cfg, df, n, ts, iv_series, o, h, l, c):
    zones = detect_zones(kind, df)
    depth_frac, rt, exp_days = cfg["depth_frac"], cfg["r_target"], cfg["expiry_days"]
    bpd = BPD[tf]
    max_lookahead = MAX_LOOKAHEAD[tf]
    iv_thresh = IV_THRESHOLD[kind]

    out = []
    for zone in zones:
        found = find_rejection_entry(o, h, l, c, zone, n, depth_frac, max_lookahead)
        if found is None:
            continue
        entry_idx, entry_price, stop_price = found
        if abs(entry_price - stop_price) <= 0:
            continue
        iv0 = iv_series[entry_idx]
        if np.isnan(iv0) or iv0 * 100 <= iv_thresh:
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
        premium = bsp.price(side, entry_price, strike, T_entry, iv0) * HAIRCUT
        value_exit = bsp.price(side, spot_exit, strike, T_remaining, iv0)
        pnl_per_unit = premium - value_exit
        entry_ts, exit_ts = int(ts[entry_idx]), int(ts[exit_idx])
        out.append((tf, Candidate(kind, zone.direction, entry_ts, exit_ts, entry_price, pnl_per_unit)))
    return out


def build_all(cell_set, tf_cache):
    all_candidates = []
    for (tf, kind), cfg in cell_set.items():
        if tf not in tf_cache:
            df = structure.load_csv(TF_FILES[tf])
            n = len(df)
            ts = df["ts_ms"].values
            iv_series = load_dvol_aligned(DVOL_JSON, df)
            o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
            tf_cache[tf] = (df, n, ts, iv_series, o, h, l, c)
        df, n, ts, iv_series, o, h, l, c = tf_cache[tf]
        all_candidates.extend(build_candidates(kind, tf, cfg, df, n, ts, iv_series, o, h, l, c))
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
    return rets, dds


def main():
    tf_cache = {}
    print("Building candidates for PROPOSED (13-cell) set...")
    proposed_candidates = build_all(PROPOSED_CELLS, tf_cache)
    print(f"total signals (proposed, pre-slot-competition): {len(proposed_candidates)}")

    print("\nBuilding candidates for CURRENT (8-cell, already live) set...")
    current_candidates = build_all(CURRENT_8_CELLS, tf_cache)
    print(f"total signals (current, pre-slot-competition): {len(current_candidates)}")

    ts_ref = tf_cache["15m"][2]  # 15m has the finest granularity, use its span as the split reference

    run_splits("CURRENT 8-cell (live)", current_candidates, ts_ref)
    run_quarters("CURRENT 8-cell (live)", current_candidates, ts_ref)

    run_splits("PROPOSED 13-cell", proposed_candidates, ts_ref)
    run_quarters("PROPOSED 13-cell", proposed_candidates, ts_ref)


if __name__ == "__main__":
    main()
