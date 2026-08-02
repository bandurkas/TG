from __future__ import annotations
"""Portfolio-level check (compounding, real per-TF concurrency caps, per_tf
same-direction scope) for the FVG candidates found by fvg_depth_sweep.py.

Per-trade robust != portfolio robust (30m/OB was the proof this session:
passed per-trade, dragged the combined book to -11%/-13% train/validation).
Before trusting FVG as a real alternative to OB, check the same way.

Compares: current live (2h/OB retuned only), FVG-only variants (2h, 1h+2h,
30m+1h+2h), and FVG+OB combined (2h/OB + 2h/FVG + 1h/FVG) to see whether FVG
adds to OB or just competes for the same margin/slots.
"""
import sys
import numpy as np

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
import structure
from dvol import load_dvol_aligned
import bs_pricer as bsp
from portfolio import Candidate, stats
from tyagach_samedir_ab import simulate_tagged
from tyagach_portfolio_multitf import STARTING_BALANCE
from ob_depth_sweep import detect_ob_zones, find_depth_entry as ob_find_depth_entry, MAX_LOOKAHEAD, BPD, DAYS_PER_YEAR, IV_THRESHOLD
from fvg_depth_sweep import detect_fvg_zones, find_depth_entry as fvg_find_depth_entry

DATA_DIR = "/Users/styserg/Desktop/Tyagach/data"
DVOL_JSON = f"{DATA_DIR}/eth_dvol_1h_4y.json"
TF_FILES = {
    "15m": f"{DATA_DIR}/eth_15m.csv",
    "30m": f"{DATA_DIR}/eth_30m.csv",
    "1h": f"{DATA_DIR}/eth_1h_rs.csv",
    "2h": f"{DATA_DIR}/eth_2h.csv",
}
HAIRCUT = 0.85

# WEIGHT_PCT/MAX_OPEN_PER_ZONE/PRIORITY are keyed by zone_kind ("OB","MB","BB")
# -- give FVG the same weight/cap/tier as OB (same risk budget philosophy, no
# separate sizing validation done yet). simulate_tagged (tyagach_samedir_ab.py)
# reads these as ITS OWN module-level globals, not whatever we import here --
# must patch the module attributes directly or FVG candidates silently get a
# $0 budget (WEIGHT_PCT.get("FVG", 0.0) == 0.0) and never open.
import portfolio as portfolio_mod
import tyagach_samedir_ab as samedir_mod
samedir_mod.PRIORITY = {**portfolio_mod.PRIORITY, "FVG": 2}
samedir_mod.WEIGHT_PCT = {**samedir_mod.WEIGHT_PCT, "FVG": samedir_mod.WEIGHT_PCT["OB"]}
samedir_mod.MAX_OPEN_PER_ZONE = {**samedir_mod.MAX_OPEN_PER_ZONE, "FVG": samedir_mod.MAX_OPEN_PER_ZONE["OB"]}


def build_candidates(kind: str, tf: str, cfg: dict, cut1_ts: int, cut2_ts: int):
    path = TF_FILES[tf]
    bpd = BPD[tf]
    max_lookahead = MAX_LOOKAHEAD[tf]
    df = structure.load_csv(path)
    n = len(df)
    ts = df["ts_ms"].values
    iv_series = load_dvol_aligned(DVOL_JSON, df)
    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    depth_frac, rt, exp_days = cfg["depth_frac"], cfg["r_target"], cfg["expiry_days"]

    if kind == "OB":
        zones = detect_ob_zones(df)
        find_entry = ob_find_depth_entry
    else:
        zones = detect_fvg_zones(df)
        find_entry = fvg_find_depth_entry

    by_split = {"train": [], "validation": [], "holdout": []}
    for zone in zones:
        found = find_entry(o, h, l, c, zone, n, depth_frac, max_lookahead)
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
        premium = bsp.price(side, entry_price, strike, T_entry, iv0) * HAIRCUT
        value_exit = bsp.price(side, spot_exit, strike, T_remaining, iv0)
        pnl_per_unit = premium - value_exit
        entry_ts, exit_ts = int(ts[entry_idx]), int(ts[exit_idx])
        cand = Candidate(kind, zone.direction, entry_ts, exit_ts, entry_price, pnl_per_unit)
        if entry_ts < cut1_ts:
            by_split["train"].append(cand)
        elif entry_ts < cut2_ts:
            by_split["validation"].append(cand)
        else:
            by_split["holdout"].append(cand)
    return by_split


# (kind, tf, cfg) tuples per scenario
CFG_2H_OB = ("OB", "2h", dict(depth_frac=0.675, r_target=5.0, expiry_days=0.25))
CFG_2H_FVG = ("FVG", "2h", dict(depth_frac=0.675, r_target=10.0, expiry_days=0.25))
CFG_1H_FVG = ("FVG", "1h", dict(depth_frac=0.700, r_target=10.0, expiry_days=0.25))
CFG_30M_FVG = ("FVG", "30m", dict(depth_frac=0.675, r_target=10.0, expiry_days=0.25))

SCENARIOS = {
    "2h-OB (current live)": [CFG_2H_OB],
    "2h-FVG only": [CFG_2H_FVG],
    "2h-FVG + 1h-FVG": [CFG_2H_FVG, CFG_1H_FVG],
    "2h-FVG + 1h-FVG + 30m-FVG": [CFG_2H_FVG, CFG_1H_FVG, CFG_30M_FVG],
    "2h-OB + 2h-FVG + 1h-FVG": [CFG_2H_OB, CFG_2H_FVG, CFG_1H_FVG],
}


def run(cells: list, cut1_ts: int, cut2_ts: int, span_days: float):
    split_days = {"train": span_days * 0.6, "validation": span_days * 0.2, "holdout": span_days * 0.2}
    combined = {"train": [], "validation": [], "holdout": []}
    for kind, tf, cfg in cells:
        by_split = build_candidates(kind, tf, cfg, cut1_ts, cut2_ts)
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

    print(f"haircut={HAIRCUT}")
    print(f"{'scenario':<28} {'split':<12} {'n_closed':>8} {'trades/day':>10} {'return%':>10} {'maxDD%':>7} {'calmar':>8}")
    print("-" * 96)
    for name, cells in SCENARIOS.items():
        for split, n_closed, tpd, ret, dd, calmar, final in run(cells, cut1_ts, cut2_ts, span_days):
            print(f"{name:<28} {split:<12} {n_closed:>8} {tpd:>10.2f} {ret:>+10.1f} {dd:>7.1f} {calmar:>8.2f}")
        print()


if __name__ == "__main__":
    main()
