from __future__ import annotations
"""OB-only entry-depth x r_target x expiry_days sweep, per TF.

Motivation: `_find_midpoint_entry` (options_backtest.py) hardcodes the zone
entry level at exactly 50% depth. That 50% was only ever validated as one of
four CATEGORICAL entry styles (touch/midpoint/close_back/engulf) in
smc_zones/src/backtest.py -- never swept as a continuous fraction against
neighbouring values (40/45/49/51/...). This generalizes that function with a
`depth_frac` parameter (0.0 = touch the near edge, 1.0 = touch the far edge,
0.5 = the existing midpoint) and sweeps it jointly with r_target/expiry_days,
since depth changes both the effective risk (stop distance) and how often a
zone gets touched at all.

Methodology mirrors sweep_sell.py exactly (the project's own precedent for
this exact kind of multi-parameter optimization): 60/20/20 train/validation/
holdout, zones re-detected INDEPENDENTLY per split (no lookahead leakage
across split boundaries), only trust a config with n>=MIN_N on ALL THREE
splits, real BS pricer, real Deribit DVOL, live IV_THRESHOLD=50 held fixed
(already validated 2026-07-03 -- not re-litigated here to keep the grid
focused on the genuinely untested depth question).

OB only. Per TF: 15m/30m/1h/2h (matches live ACTIVE_CELLS as of commit
48167c7). MAX_LOOKAHEAD scaled per TF to keep ~8.3 wall-clock days constant,
matching tyagach/services/config.py's TIMEFRAMES convention (research script
options_backtest.py's flat 800 was only ever exercised on 15m data).
"""
import sys
import multiprocessing as mp
from dataclasses import dataclass

import numpy as np
import pandas as pd

sys.path.insert(0, "/Users/sabar/Desktop/smc_options/src")
import structure
import ob
from zones import build_zones, Zone
from dvol import load_dvol_aligned
import bs_pricer as bsp

DATA_DIR = "/Users/sabar/Desktop/smc_options/data"
DVOL_JSON = f"{DATA_DIR}/eth_dvol_1h_4y.json"
TF_FILES = {
    "15m": f"{DATA_DIR}/eth_15m.csv",
    "30m": f"{DATA_DIR}/eth_30m.csv",
    "1h": f"{DATA_DIR}/eth_1h_rs.csv",   # corrected listing-date version, not the padded eth_1h.csv
    "2h": f"{DATA_DIR}/eth_2h.csv",
}
BPD = {"15m": 96, "30m": 48, "1h": 24, "2h": 12}          # bars/day, matches tyagach_portfolio_multitf.py
BASE_LOOKAHEAD_15M = 800                                    # options_backtest.py's MAX_LOOKAHEAD at bpd=96
MAX_LOOKAHEAD = {tf: round(BASE_LOOKAHEAD_15M * bpd / 96) for tf, bpd in BPD.items()}

BUFFER_FRAC = 0.0015     # same as options_backtest.py
DAYS_PER_YEAR = 365.0
SWING_ORDER = 3          # matches tyagach config.py SWING_ORDER
IV_THRESHOLD = 50.0      # live value (2026-07-03 validated), held FIXED -- not re-swept here

DEPTH_FRACS = [round(0.30 + 0.025 * i, 3) for i in range(17)]  # 0.300 .. 0.700 step 0.025
R_TARGETS = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0]
EXPIRIES_DAYS = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0]
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
    """Generalized `_find_midpoint_entry` (options_backtest.py): depth_frac=0.0
    -> touch the near edge (matches smc_zones' "touch" variant), 0.5 -> the
    existing hardcoded midpoint, 1.0 -> touch the far edge. Same invalidation
    (close beyond stop) and lookahead-window logic, unchanged."""
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


def detect_ob_zones(df: pd.DataFrame) -> list[Zone]:
    swings = structure.detect_swings(df, order=SWING_ORDER)
    swings, events = structure.label_and_track(df, swings)
    fvgs = structure.detect_fvg(df)
    obs = ob.detect_ob(df, swings, fvgs)
    return build_zones(obs, [], [])  # OB only


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
                spot_exit = c[exit_idx]  # same close-price-at-exit-bar convention as sweep_sell.py/options_backtest.py
                side = "P" if is_long else "C"
                elapsed_days = (exit_idx - e.entry_idx) / bpd
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


def run_tf(tf: str) -> pd.DataFrame:
    path = TF_FILES[tf]
    full = structure.load_csv(path)
    n = len(full)
    max_lookahead = MAX_LOOKAHEAD[tf]
    all_rows = []
    for split_name, (f0, f1) in SPLIT_FRACS.items():
        i0, i1 = int(n * f0), int(n * f1)
        sdf = full.iloc[i0:i1].reset_index(drop=True)
        zones = detect_ob_zones(sdf)
        iv_series = load_dvol_aligned(DVOL_JSON, sdf)
        for depth in DEPTH_FRACS:
            entries = entries_for_depth(sdf, zones, iv_series, depth, max_lookahead)
            for row in sweep_rt_expiry(sdf, entries, tf):
                row.update({"tf": tf, "split": split_name, "depth_frac": depth,
                            "n_zones": len(zones)})
                all_rows.append(row)
        print(f"[{tf}/{split_name}] candles={len(sdf)} ob_zones={len(zones)}", flush=True)
    return pd.DataFrame(all_rows)


def main():
    with mp.Pool(min(len(TF_FILES), mp.cpu_count())) as pool:
        dfs = pool.map(run_tf, list(TF_FILES.keys()))
    full = pd.concat(dfs, ignore_index=True)
    full.to_csv(f"{DATA_DIR}/../results/ob_depth_sweep_full.csv", index=False)

    key = ["tf", "depth_frac", "r_target", "expiry_days"]
    piv = full.pivot_table(index=key, columns="split",
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
    robust = robust.sort_values(["tf", "avg_pnl_train"], ascending=[True, False])
    robust.to_csv(f"{DATA_DIR}/../results/ob_depth_sweep_robust.csv", index=False)

    total_combos = len(piv)
    print(f"\n=== Total combos tested: {total_combos} (tf x depth x r_target x expiry, "
          f"{len(TF_FILES)}TF x {len(DEPTH_FRACS)}depth x {len(R_TARGETS)}rt x {len(EXPIRIES_DAYS)}exp) ===")
    print(f"=== Robust (n>={MIN_N} AND avg_pnl>0 on ALL 3 splits): {len(robust)} / {total_combos} ===\n")
    for tf, g in robust.groupby("tf"):
        print(f"-- {tf} (top 5 by train avg_pnl) --")
        print(g.head(5).to_string(index=False))
        print()

    # baseline (live config) for comparison, same elimination bar
    baseline = piv[(piv["depth_frac"] == 0.50) & (piv["r_target"] == 3.0) & (piv["expiry_days"] == 0.5)]
    print("=== Baseline (live: depth=0.50 r_target=3.0 expiry=0.5) for comparison ===")
    print(baseline.to_string(index=False))


if __name__ == "__main__":
    main()
