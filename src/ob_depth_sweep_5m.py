from __future__ import annotations
"""5m/OB depth x r_target x expiry sweep — the question 2026-07-09 (user wants
5m to raise OB signal frequency after MB removal).

History: 5m was rejected 2026-07-03 (all kinds negative on every split) BUT
that check ran at the hardcoded depth=0.5 / r_target=3.0 / expiry=0.5 — the
per-cell depth/r_target/expiry optimization (ob_depth_sweep.py, deployed
2026-07-08 as CELL_CONFIG) was never applied to 5m. This runs the SAME engine
and 60/20/20 methodology on eth_5m.csv with one tightening: elimination is on
FEE-ADJUSTED net avg PnL (0.03% of underlying notional per side, capped at
12.5% of the option value on that side — tyagach's live fee model), because
on 5m the per-trade edge is small enough that fees flip signs; gross columns
are kept for comparability with the 07-03 rejection and the 07-08 sweep.

Parallelism: zones/IV per split computed ONCE in the parent, workers sweep
one depth each (3 splits x 17 depths = 51 tasks) — unlike ob_depth_sweep.py's
per-TF pooling, which would leave a single-TF run on one core.

Run: python3 src/ob_depth_sweep_5m.py
"""
import multiprocessing as mp
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/Users/sabar/Desktop/smc_options/src")
import structure
from dvol import load_dvol_aligned
import bs_pricer as bsp
from ob_depth_sweep import (
    DATA_DIR, DVOL_JSON, DEPTH_FRACS, R_TARGETS, EXPIRIES_DAYS, MIN_N,
    SPLIT_FRACS, DAYS_PER_YEAR, BASE_LOOKAHEAD_15M,
    detect_ob_zones, entries_for_depth,
)

# TF from argv: "5m" (default) or "10m" (10m has no native Bybit interval —
# eth_10m.csv is resampled from eth_5m.csv; live would need the same resample).
TF = sys.argv[1] if len(sys.argv) > 1 else "5m"
_TF_DEF = {"5m": 288, "10m": 144}
CSV = f"{DATA_DIR}/eth_{TF}.csv"
BPD_5M = _TF_DEF[TF]
MAX_LOOKAHEAD_5M = round(BASE_LOOKAHEAD_15M * BPD_5M / 96)  # ~8.3 wall-clock days

FEE_RATE = 0.0003            # 0.03% of underlying notional per side
FEE_CAP_PCT = 0.125          # capped at 12.5% of that side's option value


def _fee(spot: float, option_value: float) -> float:
    return min(FEE_RATE * spot, FEE_CAP_PCT * max(option_value, 0.0))


def sweep_rt_expiry_net(df: pd.DataFrame, entries, bpd: int) -> list[dict]:
    """ob_depth_sweep.sweep_rt_expiry with the live fee model subtracted."""
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    rows = []
    for rt in R_TARGETS:
        for exp in EXPIRIES_DAYS:
            gross, net = [], []
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
                premium = bsp.price(side, e.entry_price, strike, T_entry, e.iv0)
                value_exit = bsp.price(side, spot_exit, strike, T_remaining, e.iv0)
                g = premium - value_exit
                gross.append(g)
                net.append(g - _fee(e.entry_price, premium) - _fee(spot_exit, value_exit))
            if not gross:
                continue
            ga, na = np.array(gross), np.array(net)
            rows.append({"r_target": rt, "expiry_days": exp, "n": len(ga),
                         "win_rate": round((na > 0).mean(), 3),
                         "avg_gross": round(ga.mean(), 4), "avg_net": round(na.mean(), 4),
                         "total_net": round(na.sum(), 2)})
    return rows


def _run_one(args):
    split_name, depth, sdf, zones, iv_series = args
    entries = entries_for_depth(sdf, zones, iv_series, depth, MAX_LOOKAHEAD_5M)
    rows = sweep_rt_expiry_net(sdf, entries, BPD_5M)
    for r in rows:
        r.update({"tf": TF, "split": split_name, "depth_frac": depth, "n_zones": len(zones)})
    return rows


def main():
    full = structure.load_csv(CSV)
    n = len(full)
    print(f"[5m] candles={n}  lookahead={MAX_LOOKAHEAD_5M}  grid="
          f"{len(DEPTH_FRACS)}x{len(R_TARGETS)}x{len(EXPIRIES_DAYS)}", flush=True)
    tasks = []
    for split_name, (f0, f1) in SPLIT_FRACS.items():
        i0, i1 = int(n * f0), int(n * f1)
        sdf = full.iloc[i0:i1].reset_index(drop=True)
        zones = detect_ob_zones(sdf)
        iv_series = load_dvol_aligned(DVOL_JSON, sdf)
        print(f"[5m/{split_name}] candles={len(sdf)} ob_zones={len(zones)}", flush=True)
        tasks.extend((split_name, d, sdf, zones, iv_series) for d in DEPTH_FRACS)

    with mp.Pool(max(1, mp.cpu_count() - 1)) as pool:
        results = pool.map(_run_one, tasks)
    all_rows = [r for rows in results for r in rows]
    fulldf = pd.DataFrame(all_rows)
    fulldf.to_csv(f"{DATA_DIR}/../results/ob_depth_sweep_" + TF + "_full.csv", index=False)

    key = ["depth_frac", "r_target", "expiry_days"]
    piv = fulldf.pivot_table(index=key, columns="split",
                             values=["n", "avg_net", "avg_gross"], aggfunc="first").reset_index()
    piv.columns = ["_".join(map(str, c)).rstrip("_") for c in piv.columns]
    for s in SPLIT_FRACS:
        piv[f"n_{s}"] = piv.get(f"n_{s}", pd.Series(0, index=piv.index)).fillna(0)

    robust = piv[(piv["n_train"] >= MIN_N) & (piv["n_validation"] >= MIN_N) & (piv["n_holdout"] >= MIN_N)
                 & (piv["avg_net_train"] > 0) & (piv["avg_net_validation"] > 0) & (piv["avg_net_holdout"] > 0)]
    robust = robust.sort_values("avg_net_train", ascending=False)
    robust.to_csv(f"{DATA_DIR}/../results/ob_depth_sweep_" + TF + "_robust.csv", index=False)

    print(f"\n=== 5m combos tested: {len(piv)} ===")
    print(f"=== NET-robust (n>={MIN_N} AND avg_NET>0 on ALL 3 splits): {len(robust)} ===\n")
    if len(robust):
        cols = key + ["n_train", "n_validation", "n_holdout",
                      "avg_net_train", "avg_net_validation", "avg_net_holdout",
                      "avg_gross_holdout"]
        print(robust[cols].head(10).to_string(index=False))
    # gross-only bar, to compare against the 07-08 sweep's elimination style
    grob = piv[(piv["n_train"] >= MIN_N) & (piv["n_validation"] >= MIN_N) & (piv["n_holdout"] >= MIN_N)
               & (piv["avg_gross_train"] > 0) & (piv["avg_gross_validation"] > 0) & (piv["avg_gross_holdout"] > 0)]
    print(f"\n(gross-only robust, 07-08-style bar: {len(grob)} combos)")
    base = piv[(piv["depth_frac"] == 0.50) & (piv["r_target"] == 3.0) & (piv["expiry_days"] == 0.5)]
    print("\n=== 07-03 rejection point (depth=0.50 rt=3.0 exp=0.5) ===")
    print(base.to_string(index=False))


if __name__ == "__main__":
    main()
