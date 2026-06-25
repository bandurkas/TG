from __future__ import annotations
"""Parameter sweep for the validated SELL-at-midpoint-when-IV-rich strategy.
Detect zones ONCE, find midpoint entries ONCE (entry doesn't depend on
R-target/expiry/IV-threshold), then sweep (R-target x expiry x IV-threshold)
cheaply, tune on TRAIN, confirm direction on VALIDATION, and do a strict
final check on HOLDOUT with zero further tuning.

3-way split (60/20/20) instead of 2-way, since we're now actively
optimizing (not just checking one fixed rule) -> higher overfitting risk,
needs an extra OOS gate before trusting the "best" config.
"""
import sys
from dataclasses import dataclass
import numpy as np
import pandas as pd
import structure
import ob
import bb
import mb
from zones import build_zones, Zone
from dvol import load_dvol_aligned
import bs_pricer as bsp
from options_backtest import _find_midpoint_entry, BARS_PER_DAY, DAYS_PER_YEAR

R_TARGETS = [1.0, 1.5, 2.0, 2.5, 3.0]
EXPIRIES_DAYS = [0.5, 1.0, 2.0, 3.0, 5.0]
IV_THRESHOLDS = [60, 65, 70, 75, 80, 85, 90]
MIN_N = 30  # don't trust a config with fewer than this many trades on a split


@dataclass
class Entry:
    zone_kind: str
    direction: str
    entry_idx: int
    entry_price: float
    stop_price: float
    iv0: float


def find_entries(df: pd.DataFrame, zones: list[Zone], iv_series: np.ndarray) -> list[Entry]:
    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    out = []
    for zone in zones:
        found = _find_midpoint_entry(o, h, l, c, zone, n)
        if found is None:
            continue
        entry_idx, entry_price, stop_price = found
        iv0 = iv_series[entry_idx]
        if np.isnan(iv0) or abs(entry_price - stop_price) <= 0:
            continue
        out.append(Entry(zone.kind, zone.direction, entry_idx, entry_price, stop_price, iv0))
    return out


def simulate_exit(df: pd.DataFrame, e: Entry, r_target: float, expiry_days: float):
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    is_long = e.direction == "bullish"
    risk = abs(e.entry_price - e.stop_price)
    tp_price = e.entry_price + r_target * risk if is_long else e.entry_price - r_target * risk
    expiry_idx = min(n - 1, e.entry_idx + int(expiry_days * BARS_PER_DAY))
    exit_idx = expiry_idx
    for j in range(e.entry_idx + 1, expiry_idx + 1):
        hit_sl = (l[j] <= e.stop_price) if is_long else (h[j] >= e.stop_price)
        hit_tp = (h[j] >= tp_price) if is_long else (l[j] <= tp_price)
        if hit_sl or hit_tp:
            exit_idx = j
            break
    return exit_idx, c[exit_idx]


def sell_pnl(e: Entry, exit_idx: int, spot_exit: float, expiry_days: float) -> float:
    is_long = e.direction == "bullish"
    side = "P" if is_long else "C"
    strike = e.entry_price
    elapsed_days = (exit_idx - e.entry_idx) / BARS_PER_DAY
    T_remaining = max(0.0, (expiry_days - elapsed_days) / DAYS_PER_YEAR)
    T_entry = expiry_days / DAYS_PER_YEAR
    sigma = e.iv0
    premium = bsp.price(side, e.entry_price, strike, T_entry, sigma)
    value_exit = bsp.price(side, spot_exit, strike, T_remaining, sigma)
    return premium - value_exit


def sweep(df: pd.DataFrame, entries: list[Entry]) -> pd.DataFrame:
    rows = []
    by_kind: dict[str, list[Entry]] = {}
    for e in entries:
        by_kind.setdefault(e.zone_kind, []).append(e)

    for kind, ents in by_kind.items():
        for rt in R_TARGETS:
            for exp in EXPIRIES_DAYS:
                exits = [simulate_exit(df, e, rt, exp) for e in ents]
                pnls = [sell_pnl(e, ei, sp, exp) for e, (ei, sp) in zip(ents, exits)]
                ivs = [e.iv0 * 100 for e in ents]
                for thr in IV_THRESHOLDS:
                    mask = [iv > thr for iv in ivs]
                    sel_pnls = [p for p, m in zip(pnls, mask) if m]
                    n = len(sel_pnls)
                    if n == 0:
                        continue
                    arr = np.array(sel_pnls)
                    rows.append({
                        "zone_kind": kind, "r_target": rt, "expiry_days": exp, "iv_threshold": thr,
                        "n": n, "win_rate": round((arr > 0).mean(), 3),
                        "avg_pnl_$": round(arr.mean(), 3), "total_pnl_$": round(arr.sum(), 1),
                        "std_pnl_$": round(arr.std(), 3),
                    })
    return pd.DataFrame(rows)


def main(tf_csv: str, dvol_json: str):
    df = structure.load_csv(tf_csv)
    n = len(df)
    cut1, cut2 = int(n * 0.6), int(n * 0.8)
    splits = {"train": df.iloc[:cut1].reset_index(drop=True),
              "validation": df.iloc[cut1:cut2].reset_index(drop=True),
              "holdout": df.iloc[cut2:].reset_index(drop=True)}

    results = {}
    for name, sdf in splits.items():
        swings = structure.detect_swings(sdf, order=3)
        swings, events = structure.label_and_track(sdf, swings)
        fvgs = structure.detect_fvg(sdf)
        obs = ob.detect_ob(sdf, swings, fvgs)
        bbs = bb.detect_bb(sdf, obs, events)
        mbs = mb.detect_mb(sdf, events, obs)
        zones = build_zones(obs, bbs, mbs)
        iv_series = load_dvol_aligned(dvol_json, sdf)
        entries = find_entries(sdf, zones, iv_series)
        print(f"[{name}] candles={len(sdf)} zones={len(zones)} entries_with_iv={len(entries)}")
        results[name] = (sdf, entries)

    sweeps = {name: sweep(*results[name]) for name in splits}
    key = ["zone_kind", "r_target", "expiry_days", "iv_threshold"]
    merged = sweeps["train"][key + ["n", "avg_pnl_$"]].rename(columns={"n": "n_train", "avg_pnl_$": "avg_pnl_train"})
    merged = merged.merge(sweeps["validation"][key + ["n"]].rename(columns={"n": "n_val"}), on=key, how="left")
    merged = merged.merge(sweeps["holdout"][key + ["n"]].rename(columns={"n": "n_hold"}), on=key, how="left")
    merged["n_val"] = merged["n_val"].fillna(0)
    merged["n_hold"] = merged["n_hold"].fillna(0)
    # only trust configs with enough signal on EVERY split, not just train
    robust = merged[(merged["n_train"] >= MIN_N) & (merged["n_val"] >= MIN_N) & (merged["n_hold"] >= MIN_N)]
    return results, sweeps["train"], robust


if __name__ == "__main__":
    tf_csv = sys.argv[1] if len(sys.argv) > 1 else "../data/eth_15m.csv"
    dvol_json = sys.argv[2] if len(sys.argv) > 2 else "../data/eth_dvol_1h.json"
    pd.set_option("display.width", 160)
    pd.set_option("display.max_rows", 50)

    results, train_sweep, robust = main(tf_csv, dvol_json)
    train_sweep.to_csv("../results/sweep_train_full.csv", index=False)
    robust.to_csv("../results/sweep_robust_candidates.csv", index=False)

    print(f"\n=== Configs with n>={MIN_N} on ALL THREE splits: {len(robust)} / {len(train_sweep)} ===")
    print("\n=== TOP 5 per zone_kind among ROBUST candidates (by train avg_pnl_$) ===")
    best_per_kind = {}
    for kind, g in robust.groupby("zone_kind"):
        top = g.sort_values("avg_pnl_train", ascending=False).head(5)
        print(f"\n-- {kind} --")
        print(top.to_string(index=False))
        best = g.sort_values("avg_pnl_train", ascending=False).iloc[0]
        best_per_kind[kind] = pd.Series({"r_target": best["r_target"], "expiry_days": best["expiry_days"],
                                          "iv_threshold": best["iv_threshold"]})

    print("\n=== Confirming best-per-kind TRAIN config on VALIDATION + HOLDOUT ===")
    confirm_rows = []
    for kind, best in best_per_kind.items():
        rt, exp, thr = best["r_target"], best["expiry_days"], best["iv_threshold"]
        for split_name in ["train", "validation", "holdout"]:
            sdf, entries = results[split_name]
            ents = [e for e in entries if e.zone_kind == kind]
            exits = [simulate_exit(sdf, e, rt, exp) for e in ents]
            pnls = [sell_pnl(e, ei, sp, exp) for e, (ei, sp) in zip(ents, exits)]
            ivs = [e.iv0 * 100 for e in ents]
            sel = [p for p, iv in zip(pnls, ivs) if iv > thr]
            arr = np.array(sel)
            n = len(arr)
            confirm_rows.append({
                "zone_kind": kind, "r_target": rt, "expiry_days": exp, "iv_threshold": thr,
                "split": split_name, "n": n,
                "win_rate": round((arr > 0).mean(), 3) if n else None,
                "avg_pnl_$": round(arr.mean(), 3) if n else None,
                "total_pnl_$": round(arr.sum(), 1) if n else None,
            })
    confirm_df = pd.DataFrame(confirm_rows)
    print(confirm_df.to_string(index=False))
    confirm_df.to_csv("../results/sweep_best_confirmed.csv", index=False)
