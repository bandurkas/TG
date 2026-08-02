from __future__ import annotations
"""Full re-sweep with the rejection-close entry filter (fvg_rejection_entry.
find_rejection_entry) across BOTH zone types (OB, FVG) x all 4 TFs, since
the plain wick-touch entry used by every earlier sweep this session
understated 15m/30m/1h -- 15m/OB alone flipped from negative to positive on
all 3 splits once fakeout touches are filtered out.

Same methodology as ob_depth_sweep_haircut.py / fvg_depth_sweep.py: 60/20/20
train/validation/holdout, zones re-detected per split, MIN_N=30 on all 3,
haircut=0.85. Expiry grid widened down to sub-bar-day values (0.083-0.25d)
since that mattered for FVG's exit-timing finding; r_target widened past
the original 5.0 ceiling since that mattered for the original OB re-sweep.
"""
import sys
import multiprocessing as mp

import numpy as np
import pandas as pd

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
import structure
from dvol import load_dvol_aligned
import bs_pricer as bsp
from ob_depth_sweep import detect_ob_zones, MAX_LOOKAHEAD, BPD, DAYS_PER_YEAR
from fvg_depth_sweep import detect_fvg_zones, Entry
from fvg_rejection_entry import find_rejection_entry

DATA_DIR = "/Users/styserg/Desktop/Tyagach/data"
DVOL_JSON = f"{DATA_DIR}/eth_dvol_1h_4y.json"
TF_FILES = {
    "15m": f"{DATA_DIR}/eth_15m.csv",
    "30m": f"{DATA_DIR}/eth_30m.csv",
    "1h": f"{DATA_DIR}/eth_1h_rs.csv",
    "2h": f"{DATA_DIR}/eth_2h.csv",
}
IV_THRESHOLD = 50.0
HAIRCUT = 0.85

DEPTH_FRACS = [round(0.30 + 0.025 * i, 3) for i in range(17)]
R_TARGETS = [2.0, 3.0, 4.0, 5.0, 7.0, 10.0]
EXPIRIES_DAYS = [0.083, 0.125, 0.167, 0.25, 0.5, 1.0]
MIN_N = 30
SPLIT_FRACS = {"train": (0.0, 0.6), "validation": (0.6, 0.8), "holdout": (0.8, 1.0)}

ZONE_KINDS = {"OB": detect_ob_zones, "FVG": detect_fvg_zones}


def entries_for_depth(df, zones, iv_series, depth_frac, max_lookahead):
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
        if np.isnan(iv0) or iv0 * 100 <= IV_THRESHOLD:
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
    detect_fn = ZONE_KINDS[kind]
    path = TF_FILES[tf]
    full = structure.load_csv(path)
    n = len(full)
    max_lookahead = MAX_LOOKAHEAD[tf]
    all_rows = []
    for split_name, (f0, f1) in SPLIT_FRACS.items():
        i0, i1 = int(n * f0), int(n * f1)
        sdf = full.iloc[i0:i1].reset_index(drop=True)
        zones = detect_fn(sdf)
        iv_series = load_dvol_aligned(DVOL_JSON, sdf)
        for depth in DEPTH_FRACS:
            entries = entries_for_depth(sdf, zones, iv_series, depth, max_lookahead)
            for row in sweep_rt_expiry(sdf, entries, tf):
                row.update({"kind": kind, "tf": tf, "split": split_name, "depth_frac": depth,
                            "n_zones": len(zones)})
                all_rows.append(row)
        print(f"[{kind}/{tf}/{split_name}] candles={len(sdf)} zones={len(zones)}", flush=True)
    return pd.DataFrame(all_rows)


def main():
    jobs = [(kind, tf) for kind in ZONE_KINDS for tf in TF_FILES]
    with mp.Pool(min(len(jobs), mp.cpu_count())) as pool:
        dfs = pool.map(run_one, jobs)
    full = pd.concat(dfs, ignore_index=True)
    full.to_csv(f"{DATA_DIR}/../results/rejection_full_sweep_full.csv", index=False)

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
    robust.to_csv(f"{DATA_DIR}/../results/rejection_full_sweep_robust.csv", index=False)

    print(f"\n=== Total combos: {len(piv)} | Robust (n>={MIN_N}, avg_pnl>0 all 3 splits): {len(robust)} ===\n")
    for (kind, tf), g in robust.groupby(["kind", "tf"]):
        print(f"-- {kind}/{tf} (top 3 by train avg_pnl, n={len(g)} robust combos) --")
        print(g.head(3).to_string(index=False))
        print()


if __name__ == "__main__":
    main()
