from __future__ import annotations
"""BB-only entry-depth x r_target x expiry_days sweep, 15m only (the only
live BB cell -- ACTIVE_CELLS has no other (tf,"BB") entry).

Direct port of ob_depth_sweep.py's methodology to BB, closing out
TYAGACH_IMPROVEMENT_PLAN.md's P5 backlog item ("BB per-cell depth sweep --
depth-optimization as u OB was not done, do when BB has a meaningful live
sample"). BB has never been swept at all: it trades on ZONE_CONFIG's flat
defaults (depth_frac=0.5, r_target=2.5, expiry_days=5.0, iv_threshold=55)
since day one, unlike OB which got a full 3500-combo sweep on 2026-07-08.

Same rigor as ob_depth_sweep.py: 60/20/20 train/validation/holdout, zones
re-detected INDEPENDENTLY per split (no lookahead leakage across split
boundaries), only trust a config with n>=MIN_N on ALL THREE splits, real BS
pricer, real Deribit DVOL, live IV_THRESHOLD=55 held fixed (not re-swept
here, same reasoning as the OB sweep: keep the grid focused on the
genuinely untested depth question).

BB detection needs OB + MSS structure events as inputs (bb.detect_bb), unlike
OB which is self-contained -- so this reuses structure.detect_swings/
label_and_track + ob.detect_ob to build those inputs, then bb.detect_bb.
"""
import sys
import time as _time
import multiprocessing as mp
from dataclasses import dataclass

import numpy as np
import pandas as pd

sys.path.insert(0, "/Users/sabar/Desktop/smc_options/src")
import structure
import ob as ob_mod
import bb as bb_mod
from zones import build_zones, Zone
from dvol import load_dvol_aligned
import bs_pricer as bsp

DATA_DIR = "/Users/sabar/Desktop/smc_options/data"
DVOL_JSON = f"{DATA_DIR}/eth_dvol_1h_4y.json"
TF = "15m"
DATA_PATH = f"{DATA_DIR}/eth_15m.csv"
BPD = 96
BASE_LOOKAHEAD_15M = 800  # matches config.TIMEFRAMES["15m"].max_lookahead
MAX_LOOKAHEAD = BASE_LOOKAHEAD_15M

BUFFER_FRAC = 0.0015
DAYS_PER_YEAR = 365.0
SWING_ORDER = 3
IV_THRESHOLD = 55.0  # live value, held fixed

DEPTH_FRACS = [round(0.30 + 0.025 * i, 3) for i in range(17)]  # 0.300 .. 0.700 step 0.025
R_TARGETS = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0]
EXPIRIES_DAYS = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]  # live=5.0; BB is a swing hold, keep the range wide
MIN_N = 30

SPLIT_FRACS = {"train": (0.0, 0.6), "validation": (0.6, 0.8), "holdout": (0.8, 1.0)}


@dataclass
class Entry:
    direction: str
    entry_idx: int
    entry_price: float
    stop_price: float
    iv0: float


def find_depth_entry(o, h, l, c, zone: Zone, n: int, depth_frac: float, max_lookahead: int):
    is_long = zone.direction == "bullish"
    zlo, zhi = zone.zone_low, zone.zone_high
    buf = BUFFER_FRAC * ((zlo + zhi) / 2)
    stop_price = (zlo - buf) if is_long else (zhi + buf)
    entry_level = (zhi - depth_frac * (zhi - zlo)) if is_long else (zlo + depth_frac * (zhi - zlo))
    start, end = zone.valid_from, min(n - 1, zone.valid_from + max_lookahead)
    for i in range(start, end + 1):
        hi_, lo_, cl_ = h[i], l[i], c[i]
        if is_long and cl_ < stop_price:
            return None
        if (not is_long) and cl_ > stop_price:
            return None
        if (is_long and lo_ <= entry_level) or ((not is_long) and hi_ >= entry_level):
            return i, entry_level, stop_price
    return None


def detect_bb_zones(df: pd.DataFrame) -> list[Zone]:
    swings = structure.detect_swings(df, order=SWING_ORDER)
    swings, events = structure.label_and_track(df, swings)
    fvgs = structure.detect_fvg(df)
    obs = ob_mod.detect_ob(df, swings, fvgs)
    bbs = bb_mod.detect_bb(df, obs, events)
    return build_zones([], bbs, [])  # BB only


def entries_for_depth(df: pd.DataFrame, zones: list[Zone], iv_series: np.ndarray,
                       depth_frac: float, max_lookahead: int) -> list[Entry]:
    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    out = []
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
        out.append(Entry(zone.direction, entry_idx, entry_price, stop_price, iv0))
    return out


def sweep_rt_expiry(df: pd.DataFrame, entries: list[Entry]) -> list[dict]:
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    rows = []
    for rt in R_TARGETS:
        for exp in EXPIRIES_DAYS:
            pnls = []
            for e in entries:
                is_long = e.direction == "bullish"
                risk = abs(e.entry_price - e.stop_price)
                tp_price = e.entry_price + rt * risk if is_long else e.entry_price - rt * risk
                expiry_bars = int(exp * BPD)
                expiry_idx = min(n - 1, e.entry_idx + expiry_bars)
                exit_idx = expiry_idx
                for j in range(e.entry_idx + 1, expiry_idx + 1):
                    hit_sl = (l[j] <= e.stop_price) if is_long else (h[j] >= e.stop_price)
                    hit_tp = (h[j] >= tp_price) if is_long else (l[j] <= tp_price)
                    if hit_sl or hit_tp:
                        exit_idx = j
                        break
                spot_exit = c[exit_idx]
                side = "P" if is_long else "C"
                elapsed_days = (exit_idx - e.entry_idx) / BPD
                T_remaining = max(0.0, (exp - elapsed_days) / DAYS_PER_YEAR)
                T_entry = exp / DAYS_PER_YEAR
                strike = e.entry_price
                premium = bsp.price(side, e.entry_price, strike, T_entry, e.iv0)
                value_exit = bsp.price(side, spot_exit, strike, T_remaining, e.iv0)
                pnls.append(premium - value_exit)
            n_trades = len(pnls)
            if n_trades == 0:
                continue
            arr = np.array(pnls)
            rows.append({"r_target": rt, "expiry_days": exp, "n": n_trades,
                         "win_rate": round((arr > 0).mean(), 3),
                         "avg_pnl": round(arr.mean(), 4), "total_pnl": round(arr.sum(), 2)})
    return rows


def run_split(args):
    """One split: load + detect zones ONCE (the expensive part), then sweep
    all depth/r_target/expiry combos against that fixed zone set -- mirrors
    ob_depth_sweep.py's run_tf structure (zone detection does not depend on
    depth_frac, only entries_for_depth's touch-level does)."""
    split_name, f0, f1 = args
    full = structure.load_csv(DATA_PATH)
    n = len(full)
    i0, i1 = int(n * f0), int(n * f1)
    sdf = full.iloc[i0:i1].reset_index(drop=True)
    zones = detect_bb_zones(sdf)
    iv_series = load_dvol_aligned(DVOL_JSON, sdf)
    rows = []
    for depth in DEPTH_FRACS:
        entries = entries_for_depth(sdf, zones, iv_series, depth, MAX_LOOKAHEAD)
        for row in sweep_rt_expiry(sdf, entries):
            row.update({"split": split_name, "depth_frac": depth, "n_zones": len(zones)})
            rows.append(row)
    print(f"[{split_name}] candles={len(sdf)} bb_zones={len(zones)}", flush=True)
    return rows


def main():
    t0 = _time.time()

    jobs = [(split_name, f0, f1) for split_name, (f0, f1) in SPLIT_FRACS.items()]
    with mp.Pool(min(mp.cpu_count(), len(jobs))) as pool:
        results = pool.map(run_split, jobs)
    all_rows = [r for rows in results for r in rows]
    full_df = pd.DataFrame(all_rows)
    full_df.to_csv(f"{DATA_DIR}/../results/bb_depth_sweep_full.csv", index=False)
    print(f"elapsed: {_time.time() - t0:.1f}s, rows={len(full_df)}")

    key = ["depth_frac", "r_target", "expiry_days"]
    piv = full_df.pivot_table(index=key, columns="split",
                               values=["n", "avg_pnl"], aggfunc="first").reset_index()
    piv.columns = ["_".join(c).rstrip("_") for c in piv.columns]
    for s in ["train", "validation", "holdout"]:
        if f"n_{s}" not in piv.columns:
            piv[f"n_{s}"] = 0
        if f"avg_pnl_{s}" not in piv.columns:
            piv[f"avg_pnl_{s}"] = np.nan
    piv = piv.fillna({"n_train": 0, "n_validation": 0, "n_holdout": 0})

    robust = piv[(piv["n_train"] >= MIN_N) & (piv["n_validation"] >= MIN_N) & (piv["n_holdout"] >= MIN_N) &
                 (piv["avg_pnl_train"] > 0) & (piv["avg_pnl_validation"] > 0) & (piv["avg_pnl_holdout"] > 0)]
    robust = robust.sort_values("avg_pnl_train", ascending=False)
    robust.to_csv(f"{DATA_DIR}/../results/bb_depth_sweep_robust.csv", index=False)

    total_combos = len(piv)
    print(f"\n=== Total combos tested: {total_combos} "
          f"({len(DEPTH_FRACS)}depth x {len(R_TARGETS)}rt x {len(EXPIRIES_DAYS)}exp) ===")
    print(f"=== Robust (n>={MIN_N} AND avg_pnl>0 on ALL 3 splits): {len(robust)} / {total_combos} ===\n")
    print(robust.head(10).to_string(index=False))

    baseline = piv[(piv["depth_frac"] == 0.50) & (piv["r_target"] == 2.5) & (piv["expiry_days"] == 5.0)]
    print("\n=== Baseline (live: depth=0.50 r_target=2.5 expiry=5.0) for comparison ===")
    print(baseline.to_string(index=False))


if __name__ == "__main__":
    main()
