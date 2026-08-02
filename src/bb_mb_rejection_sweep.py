from __future__ import annotations
"""Extends this session's rejection-close re-sweep (rejection_full_sweep.py,
OB+FVG x 4 TFs) to BB and MB -- the two zone kinds that never got it. BB has
been live since day one on flat untuned defaults (depth=0.5/r_target=2.5/
expiry=5.0); MB was deactivated 2026-07-07 on a live-performance override,
before rejection-close (or any of today's other fixes) existed. Rejection-
close turned out to be a GENERAL entry-quality fix (flipped 15m/OB from
losing to winning on all 3 splits) -- worth checking whether it does the
same for BB/MB before assuming they're dead for good.

BB: 15m only (the only TF BB ever traded -- needs OB+MSS structure inputs,
same as bb_depth_sweep.py). MB: all 4 TFs (was live on 15m/1h/2h before
deactivation, per ARCHITECTURE.md "Amendment 2026-07-07"; sweeping 30m too
for completeness).

Same rigor as rejection_full_sweep.py: 60/20/20 train/validation/holdout,
zones re-detected per split, MIN_N=30 on all 3, haircut=0.85. IV thresholds
held at each kind's live value (BB=55, MB=50) -- not re-swept, same
reasoning as the original OB/BB depth sweeps (keep the grid focused on the
genuinely untested depth/r_target/expiry question).
"""
import sys
import multiprocessing as mp

import numpy as np
import pandas as pd

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
import structure
import ob as ob_mod
import bb as bb_mod
import mb as mb_mod
from zones import build_zones
from dvol import load_dvol_aligned
import bs_pricer as bsp
from ob_depth_sweep import MAX_LOOKAHEAD, BPD, DAYS_PER_YEAR
from fvg_depth_sweep import Entry
from fvg_rejection_entry import find_rejection_entry

DATA_DIR = "/Users/styserg/Desktop/Tyagach/data"
DVOL_JSON = f"{DATA_DIR}/eth_dvol_1h_4y.json"
TF_FILES = {
    "15m": f"{DATA_DIR}/eth_15m.csv",
    "30m": f"{DATA_DIR}/eth_30m.csv",
    "1h": f"{DATA_DIR}/eth_1h_rs.csv",
    "2h": f"{DATA_DIR}/eth_2h.csv",
}
HAIRCUT = 0.85
SWING_ORDER = 3
IV_THRESHOLD = {"BB": 55.0, "MB": 50.0}

DEPTH_FRACS = [round(0.30 + 0.025 * i, 3) for i in range(17)]
R_TARGETS = [2.0, 3.0, 4.0, 5.0, 7.0, 10.0]
EXPIRIES_DAYS = [0.083, 0.125, 0.167, 0.25, 0.5, 1.0, 2.0, 5.0]  # widened past 1.0 -- BB's live expiry is 5.0d
MIN_N = 30
SPLIT_FRACS = {"train": (0.0, 0.6), "validation": (0.6, 0.8), "holdout": (0.8, 1.0)}

JOBS = [("BB", "15m")] + [("MB", tf) for tf in TF_FILES]


def detect_zones(kind, df):
    swings = structure.detect_swings(df, order=SWING_ORDER)
    swings, events = structure.label_and_track(df, swings)
    fvgs = structure.detect_fvg(df)
    obs = ob_mod.detect_ob(df, swings, fvgs)
    if kind == "BB":
        bbs = bb_mod.detect_bb(df, obs, events)
        return build_zones([], bbs, [])
    else:  # MB
        mbs = mb_mod.detect_mb(df, events, obs)
        return build_zones([], [], mbs)


def entries_for_depth(df, zones, iv_series, depth_frac, max_lookahead, iv_threshold):
    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    out = []
    for zone in zones:
        found = find_rejection_entry(o, h, l, c, zone, n, depth_frac, max_lookahead)
        if found is None:
            continue
        entry_idx, entry_price, stop_price = found
        if abs(entry_price - stop_price) <= 0:
            continue
        iv0 = iv_series[entry_idx]
        if np.isnan(iv0) or iv0 * 100 <= iv_threshold:
            continue
        out.append(Entry(zone.direction, entry_idx, entry_price, stop_price, iv0))
    return out


def sweep_rt_expiry(df, entries, tf):
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    bpd = BPD[tf]
    rows = []
    for rt in R_TARGETS:
        for exp in EXPIRIES_DAYS:
            pnls = []
            for e in entries:
                is_long = e.direction == "bullish"
                risk = abs(e.entry_price - e.stop_price)
                tp_price = e.entry_price + rt * risk if is_long else e.entry_price - rt * risk
                expiry_bars = int(exp * bpd)
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
                elapsed_days = (exit_idx - e.entry_idx) / bpd
                T_remaining = max(0.0, (exp - elapsed_days) / DAYS_PER_YEAR)
                T_entry = exp / DAYS_PER_YEAR
                strike = e.entry_price
                premium = bsp.price(side, e.entry_price, strike, T_entry, e.iv0) * HAIRCUT
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


def run_one(args):
    kind, tf = args
    path = TF_FILES[tf]
    full = structure.load_csv(path)
    n = len(full)
    max_lookahead = MAX_LOOKAHEAD[tf]
    iv_threshold = IV_THRESHOLD[kind]
    all_rows = []
    for split_name, (f0, f1) in SPLIT_FRACS.items():
        i0, i1 = int(n * f0), int(n * f1)
        sdf = full.iloc[i0:i1].reset_index(drop=True)
        zones = detect_zones(kind, sdf)
        iv_series = load_dvol_aligned(DVOL_JSON, sdf)
        for depth in DEPTH_FRACS:
            entries = entries_for_depth(sdf, zones, iv_series, depth, max_lookahead, iv_threshold)
            for row in sweep_rt_expiry(sdf, entries, tf):
                row.update({"kind": kind, "tf": tf, "split": split_name, "depth_frac": depth,
                            "n_zones": len(zones)})
                all_rows.append(row)
        print(f"[{kind}/{tf}/{split_name}] candles={len(sdf)} zones={len(zones)}", flush=True)
    return pd.DataFrame(all_rows)


def main():
    with mp.Pool(min(len(JOBS), mp.cpu_count())) as pool:
        dfs = pool.map(run_one, JOBS)
    full = pd.concat(dfs, ignore_index=True)
    full.to_csv(f"{DATA_DIR}/../results/bb_mb_rejection_sweep_full.csv", index=False)

    key = ["kind", "tf", "depth_frac", "r_target", "expiry_days"]
    piv = full.pivot_table(index=key, columns="split", values=["n", "avg_pnl"], aggfunc="first").reset_index()
    piv.columns = ["_".join(c).rstrip("_") for c in piv.columns]
    for s in ["train", "validation", "holdout"]:
        if f"n_{s}" not in piv.columns:
            piv[f"n_{s}"] = 0
        if f"avg_pnl_{s}" not in piv.columns:
            piv[f"avg_pnl_{s}"] = np.nan
    piv = piv.fillna({"n_train": 0, "n_validation": 0, "n_holdout": 0})

    robust = piv[(piv["n_train"] >= MIN_N) & (piv["n_validation"] >= MIN_N) & (piv["n_holdout"] >= MIN_N) &
                 (piv["avg_pnl_train"] > 0) & (piv["avg_pnl_validation"] > 0) & (piv["avg_pnl_holdout"] > 0)]
    robust = robust.sort_values(["kind", "tf", "avg_pnl_train"], ascending=[True, True, False])
    robust.to_csv(f"{DATA_DIR}/../results/bb_mb_rejection_sweep_robust.csv", index=False)

    print(f"\n=== Total combos: {len(piv)} | Robust (n>={MIN_N}, avg_pnl>0 all 3 splits): {len(robust)} ===\n")
    for (kind, tf), g in robust.groupby(["kind", "tf"]):
        print(f"-- {kind}/{tf} (top 3 by train avg_pnl, n={len(g)} robust combos) --")
        print(g.head(3).to_string(index=False))
        print()

    for kind, tf in JOBS:
        n_robust = len(robust[(robust.kind == kind) & (robust.tf == tf)])
        if n_robust == 0:
            print(f"-- {kind}/{tf}: 0 robust combos (dead) --")


if __name__ == "__main__":
    main()
