from __future__ import annotations
"""Standalone FVG (Fair Value Gap = imbalance) zone sweep -- same rigorous
methodology as ob_depth_sweep_haircut.py (60/20/20 train/validation/holdout,
zones re-detected per split, MIN_N=30 on all 3 splits, realistic 0.85 fill
haircut baked into pricing from the start), but the zone source is a
standalone 3-candle FVG instead of the OB order-block pattern.

Why: FVG detection already exists (structure.detect_fvg) but is currently
ONLY used to widen an OB zone's boundary when the two coincide -- it has
never been traded as its own signal. SMC theory treats an FVG as its own
entity (price tends to retrace into an unfilled gap before continuing), a
different and much more permissive thesis than OB's (needs a swing-sweep +
impulse + engulfing-confirmation structural pattern). FVG fires far more
often since it drops all of that structural context -- worth knowing whether
that's more raw material for a real edge, or just more noise.

Entry/stop mirror OB's convention exactly (same find_depth_entry, same
buffer, same IV filter) so this is an apples-to-apples comparison, not a
different methodology. direction: FVG.direction "up" (gap up) -> bullish
(price retraces down into the gap) -> SELL PUT; "down" -> bearish -> SELL
CALL. Same as OB's bullish/bearish convention.
"""
import sys
import multiprocessing as mp
from dataclasses import dataclass

import numpy as np
import pandas as pd

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
import structure
from dvol import load_dvol_aligned
import bs_pricer as bsp

DATA_DIR = "/Users/styserg/Desktop/Tyagach/data"
DVOL_JSON = f"{DATA_DIR}/eth_dvol_1h_4y.json"
TF_FILES = {
    "15m": f"{DATA_DIR}/eth_15m.csv",
    "30m": f"{DATA_DIR}/eth_30m.csv",
    "1h": f"{DATA_DIR}/eth_1h_rs.csv",
    "2h": f"{DATA_DIR}/eth_2h.csv",
}
BPD = {"15m": 96, "30m": 48, "1h": 24, "2h": 12}
BASE_LOOKAHEAD_15M = 800
MAX_LOOKAHEAD = {tf: round(BASE_LOOKAHEAD_15M * bpd / 96) for tf, bpd in BPD.items()}

BUFFER_FRAC = 0.0015
DAYS_PER_YEAR = 365.0
IV_THRESHOLD = 50.0
HAIRCUT = 0.85

DEPTH_FRACS = [round(0.30 + 0.025 * i, 3) for i in range(17)]
R_TARGETS = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 7.0, 10.0]  # widened past ob_depth_sweep's 5.0 ceiling from the start
EXPIRIES_DAYS = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0]
MIN_N = 30
SPLIT_FRACS = {"train": (0.0, 0.6), "validation": (0.6, 0.8), "holdout": (0.8, 1.0)}


@dataclass
class FvgZone:
    idx: int  # confirmation candle
    valid_from: int
    direction: str  # "bullish" / "bearish"
    zone_low: float
    zone_high: float


def detect_fvg_zones(df: pd.DataFrame) -> list[FvgZone]:
    fvgs = structure.detect_fvg(df)
    out = []
    for f in fvgs:
        direction = "bullish" if f.direction == "up" else "bearish"
        out.append(FvgZone(f.idx, f.idx + 1, direction, f.gap_low, f.gap_high))
    return out


@dataclass
class Entry:
    direction: str
    entry_idx: int
    entry_price: float
    stop_price: float
    iv0: float


def find_depth_entry(o, h, l, c, zone: FvgZone, n: int, depth_frac: float, max_lookahead: int):
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


def entries_for_depth(df, zones, iv_series, depth_frac, max_lookahead) -> list[Entry]:
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


def sweep_rt_expiry(df: pd.DataFrame, entries: list[Entry], tf: str) -> list[dict]:
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


def run_tf(tf: str) -> pd.DataFrame:
    path = TF_FILES[tf]
    full = structure.load_csv(path)
    n = len(full)
    max_lookahead = MAX_LOOKAHEAD[tf]
    all_rows = []
    for split_name, (f0, f1) in SPLIT_FRACS.items():
        i0, i1 = int(n * f0), int(n * f1)
        sdf = full.iloc[i0:i1].reset_index(drop=True)
        zones = detect_fvg_zones(sdf)
        iv_series = load_dvol_aligned(DVOL_JSON, sdf)
        for depth in DEPTH_FRACS:
            entries = entries_for_depth(sdf, zones, iv_series, depth, max_lookahead)
            for row in sweep_rt_expiry(sdf, entries, tf):
                row.update({"tf": tf, "split": split_name, "depth_frac": depth,
                            "n_zones": len(zones)})
                all_rows.append(row)
        print(f"[{tf}/{split_name}] candles={len(sdf)} fvg_zones={len(zones)}", flush=True)
    return pd.DataFrame(all_rows)


def main():
    with mp.Pool(min(len(TF_FILES), mp.cpu_count())) as pool:
        dfs = pool.map(run_tf, list(TF_FILES.keys()))
    full = pd.concat(dfs, ignore_index=True)
    full.to_csv(f"{DATA_DIR}/../results/fvg_depth_sweep_haircut85_full.csv", index=False)

    key = ["tf", "depth_frac", "r_target", "expiry_days"]
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
    robust = robust.sort_values(["tf", "avg_pnl_train"], ascending=[True, False])
    robust.to_csv(f"{DATA_DIR}/../results/fvg_depth_sweep_haircut85_robust.csv", index=False)

    total_combos = len(piv)
    print(f"\n=== Total combos tested: {total_combos} (haircut={HAIRCUT}) ===")
    print(f"=== Robust (n>={MIN_N} AND avg_pnl>0 on ALL 3 splits): {len(robust)} / {total_combos} ===\n")
    for tf, g in robust.groupby("tf"):
        print(f"-- {tf} (top 5 by train avg_pnl) --")
        print(g.head(5).to_string(index=False))
        print()


if __name__ == "__main__":
    main()
