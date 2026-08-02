from __future__ import annotations
"""Does the LIVE, already-deployed CELL_CONFIG per (tf, kind=OB) stay net-
positive on all 3 splits once a realistic sell-fill haircut is applied to
entry premium?

Motivation (2026-08-02, "make Tyagach profitable" P2): the 2026-07-09 review
found live sold premium = ~85% of BS-mid and treated it as an ANALYSIS
DISCIPLINE (discount backtest expectations when eyeballing live results) --
it never re-ran the parameter sweep with that friction baked into the
pricing model itself. A fresh audit of Tyagach's own 27 closed live trades
(2026-08-02) confirms ~83-90% haircut on the currently-active cells
specifically, and critically: haircut does NOT correlate with win/loss
(losers 83.0% vs winners 83.5%) -- so it's a constant tax on every trade's
revenue leg, not what's driving individual trade outcomes. The open
question this answers: does that constant tax erase the edge the deployed
depth_frac/r_target/expiry_days were chosen for?

Design: reuse ob_depth_sweep.py's exact entry/zone logic (same detector,
same buffer, same IV threshold) but only re-price the FOUR already-deployed
OB cells (config.py's CELL_CONFIG) at haircut in {1.00 (original/baseline),
0.90, 0.85, 0.80} (brackets the empirical 83-90% range). Only the entry
premium (the SELL leg -- what the live audit actually measured) is
haircut; the exit/buy-back leg is left at theoretical BS value since no
live data exists yet on buy-to-close fill quality.
"""
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
import structure
import ob
from zones import build_zones, Zone
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
SWING_ORDER = 3
IV_THRESHOLD = 50.0

# Live CELL_CONFIG (tyagach/services/config.py, commit 48167c7) -- the thing
# under test.
LIVE_CELLS = {
    "15m": {"depth_frac": 0.575, "r_target": 10.0, "expiry_days": 0.25},
    "30m": {"depth_frac": 0.500, "r_target": 7.0,  "expiry_days": 0.25},
    "1h":  {"depth_frac": 0.325, "r_target": 8.0,  "expiry_days": 0.75},
    "2h":  {"depth_frac": 0.675, "r_target": 3.0,  "expiry_days": 1.00},
}
HAIRCUTS = [1.00, 0.90, 0.85, 0.80]
SPLIT_FRACS = {"train": (0.0, 0.6), "validation": (0.6, 0.8), "holdout": (0.8, 1.0)}
MIN_N = 30


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


def detect_ob_zones(df: pd.DataFrame) -> list[Zone]:
    swings = structure.detect_swings(df, order=SWING_ORDER)
    swings, events = structure.label_and_track(df, swings)
    fvgs = structure.detect_fvg(df)
    obs = ob.detect_ob(df, swings, fvgs)
    return build_zones(obs, [], [])


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


def price_entries(df, entries: list[Entry], tf: str, r_target: float, expiry_days: float, haircut: float) -> np.ndarray:
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    bpd = BPD[tf]
    pnls = []
    for e in entries:
        is_long = e.direction == "bullish"
        risk = abs(e.entry_price - e.stop_price)
        tp_price = e.entry_price + r_target * risk if is_long else e.entry_price - r_target * risk
        expiry_bars = int(expiry_days * bpd)
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
        T_remaining = max(0.0, (expiry_days - elapsed_days) / DAYS_PER_YEAR)
        T_entry = expiry_days / DAYS_PER_YEAR
        strike = e.entry_price
        premium = bsp.price(side, e.entry_price, strike, T_entry, e.iv0) * haircut
        value_exit = bsp.price(side, spot_exit, strike, T_remaining, e.iv0)
        pnls.append(premium - value_exit)
    return np.array(pnls)


def run_tf(tf: str) -> pd.DataFrame:
    cfg = LIVE_CELLS[tf]
    path = TF_FILES[tf]
    full = structure.load_csv(path)
    n = len(full)
    max_lookahead = MAX_LOOKAHEAD[tf]
    rows = []
    for split_name, (f0, f1) in SPLIT_FRACS.items():
        i0, i1 = int(n * f0), int(n * f1)
        sdf = full.iloc[i0:i1].reset_index(drop=True)
        zones = detect_ob_zones(sdf)
        iv_series = load_dvol_aligned(DVOL_JSON, sdf)
        entries = entries_for_depth(sdf, zones, iv_series, cfg["depth_frac"], max_lookahead)
        for hc in HAIRCUTS:
            arr = price_entries(sdf, entries, tf, cfg["r_target"], cfg["expiry_days"], hc)
            n_trades = len(arr)
            rows.append({
                "tf": tf, "split": split_name, "haircut": hc, "n": n_trades,
                "win_rate": round((arr > 0).mean(), 3) if n_trades else float("nan"),
                "avg_pnl": round(arr.mean(), 4) if n_trades else float("nan"),
                "total_pnl": round(arr.sum(), 2) if n_trades else 0.0,
            })
    return pd.DataFrame(rows)


def main():
    dfs = [run_tf(tf) for tf in TF_FILES]
    full = pd.concat(dfs, ignore_index=True)
    full.to_csv(f"{DATA_DIR}/../results/haircut_robustness_check.csv", index=False)

    print("tf   split       haircut   n   win_rate   avg_pnl   total_pnl")
    for _, r in full.iterrows():
        print(f"{r['tf']:>4} {r['split']:>10} {r['haircut']:>8.2f} {r['n']:>5.0f} "
              f"{r['win_rate']:>9.3f} {r['avg_pnl']:>9.4f} {r['total_pnl']:>10.2f}")

    print("\n=== Robustness verdict per TF (avg_pnl > 0 at haircut=0.80, worst case tested) ===")
    for tf in TF_FILES:
        sub = full[(full["tf"] == tf) & (full["haircut"] == 0.80)]
        ok = (sub["avg_pnl"] > 0).all() and (sub["n"] >= MIN_N).all()
        print(f"{tf}: {'ROBUST' if ok else 'FRAGILE/NEGATIVE'} at 0.80 haircut  "
              f"({dict(zip(sub['split'], sub['avg_pnl']))})")


if __name__ == "__main__":
    main()
