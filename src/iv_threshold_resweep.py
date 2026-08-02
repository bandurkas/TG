from __future__ import annotations
"""IV-threshold resweep for FVG and MB. Neither was ever independently
tuned -- FVG just inherited OB's IV_THRESHOLD=50 by convention when it was
created this session (fvg_depth_sweep.py hardcodes the same 50 OB used, no
dedicated sweep); MB's 50 comes from the 2026-07-03 amendment
(ARCHITECTURE.md, "MB: 70->50") but that predates rejection-close entirely
-- the entry-quality change might shift the optimal threshold.

Holds each cell's already-validated depth_frac/r_target/expiry_days FIXED
(from CELL_CONFIG) and sweeps ONLY iv_threshold -- a marginal single-
dimension check, same scope as the original 2026-07-03 IV sweep, not a
joint re-optimization. Entries (touch + rejection + direction + iv0) are
computed ONCE per split, independent of iv_threshold; each grid value just
filters that already-computed list -- no redundant re-scanning.
"""
import sys
import numpy as np

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
import structure
import ob as ob_mod
import mb as mb_mod
from zones import build_zones
from dvol import load_dvol_aligned
import bs_pricer as bsp
from ob_depth_sweep import MAX_LOOKAHEAD, BPD, DAYS_PER_YEAR
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
SPLIT_FRACS = {"train": (0.0, 0.6), "validation": (0.6, 0.8), "holdout": (0.8, 1.0)}
MIN_N = 30
IV_GRID = [30.0, 35.0, 40.0, 45.0, 50.0, 55.0, 60.0, 65.0, 70.0]

# Already-validated depth/r_target/expiry per cell, held fixed (live CELL_CONFIG)
CANDIDATES = {
    ("FVG", "2h"):  dict(depth_frac=0.675, r_target=10.0, expiry_days=0.167, current_iv=50.0),
    ("FVG", "1h"):  dict(depth_frac=0.325, r_target=7.0,  expiry_days=0.167, current_iv=50.0),
    ("FVG", "30m"): dict(depth_frac=0.300, r_target=7.0,  expiry_days=0.125, current_iv=50.0),
    ("FVG", "15m"): dict(depth_frac=0.300, r_target=10.0, expiry_days=0.125, current_iv=50.0),
    ("MB", "2h"):   dict(depth_frac=0.500, r_target=3.0,  expiry_days=0.25,  current_iv=50.0),
    ("MB", "1h"):   dict(depth_frac=0.400, r_target=10.0, expiry_days=0.25,  current_iv=50.0),
    ("MB", "30m"):  dict(depth_frac=0.400, r_target=10.0, expiry_days=0.167, current_iv=50.0),
    ("MB", "15m"):  dict(depth_frac=0.425, r_target=7.0,  expiry_days=0.125, current_iv=50.0),
}


def detect_zones(kind, df):
    if kind == "FVG":
        return detect_fvg_zones(df)
    swings = structure.detect_swings(df, order=SWING_ORDER)
    swings, events = structure.label_and_track(df, swings)
    fvgs = structure.detect_fvg(df)
    obs = ob_mod.detect_ob(df, swings, fvgs)
    mbs = mb_mod.detect_mb(df, events, obs)
    return build_zones([], [], mbs)


def all_entries(o, h, l, c, zones, iv_series, depth_frac, max_lookahead):
    """One rejection-close scan per zone, IV-threshold-independent. Returns
    (entry_idx, entry_price, stop_price, iv0, is_long) for every zone that
    ever triggers, regardless of its IV level at trigger time."""
    n = len(c)
    out = []
    for zone in zones:
        found = find_rejection_entry(o, h, l, c, zone, n, depth_frac, max_lookahead)
        if found is None:
            continue
        entry_idx, entry_price, stop_price = found
        if abs(entry_price - stop_price) <= 0:
            continue
        iv0 = iv_series[entry_idx]
        if np.isnan(iv0):
            continue
        out.append((entry_idx, entry_price, stop_price, iv0, zone.direction == "bullish"))
    return out


def pnl_for_entries(entries, c, h, l, rt, exp_days, bpd):
    n = len(c)
    pnls = []
    for entry_idx, entry_price, stop_price, iv0, is_long in entries:
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
        pnls.append(premium - value_exit)
    return np.array(pnls)


def main():
    print(f"{'kind/tf':<10} {'iv_thresh':>10} {'n_train':>8} {'n_val':>8} {'n_hold':>8} "
          f"{'avg_train':>10} {'avg_val':>10} {'avg_hold':>10}  flags")
    print("-" * 96)
    winners = {}
    for (kind, tf), cfg in CANDIDATES.items():
        full = structure.load_csv(TF_FILES[tf])
        n = len(full)
        bpd = BPD[tf]
        max_lookahead = MAX_LOOKAHEAD[tf]

        entries_by_split = {}
        for split_name, (f0, f1) in SPLIT_FRACS.items():
            i0, i1 = int(n * f0), int(n * f1)
            sdf = full.iloc[i0:i1].reset_index(drop=True)
            zones = detect_zones(kind, sdf)
            iv_series = load_dvol_aligned(DVOL_JSON, sdf)
            o, h, l, c = sdf["open"].values, sdf["high"].values, sdf["low"].values, sdf["close"].values
            entries = all_entries(o, h, l, c, zones, iv_series, cfg["depth_frac"], max_lookahead)
            entries_by_split[split_name] = (entries, c, h, l)

        by_iv = {}
        for iv_thresh in IV_GRID:
            row = {}
            for split_name, (entries, c, h, l) in entries_by_split.items():
                filtered = [e for e in entries if e[3] * 100 > iv_thresh]
                pnls = pnl_for_entries(filtered, c, h, l, cfg["r_target"], cfg["expiry_days"], bpd)
                row[split_name] = (len(pnls), pnls.mean() if len(pnls) else np.nan)
            by_iv[iv_thresh] = row

        best_robust, best_train_avg = None, -np.inf
        for iv_thresh in IV_GRID:
            row = by_iv[iv_thresh]
            ntr, atr = row["train"]
            nva, ava = row["validation"]
            nho, aho = row["holdout"]
            robust = (ntr >= MIN_N and nva >= MIN_N and nho >= MIN_N and
                      atr > 0 and ava > 0 and aho > 0)
            flags = []
            if robust:
                flags.append("ROBUST")
            if iv_thresh == cfg["current_iv"]:
                flags.append("CURRENT LIVE")
            print(f"{kind+'/'+tf:<10} {iv_thresh:>10.0f} {ntr:>8} {nva:>8} {nho:>8} "
                  f"{atr:>10.4f} {ava:>10.4f} {aho:>10.4f}  {' + '.join(flags)}")
            if robust and atr > best_train_avg:
                best_robust, best_train_avg = iv_thresh, atr
        winners[(kind, tf)] = best_robust
        print()

    print("=== Best robust IV threshold per cell (None = no threshold in grid clears all 3 splits) ===")
    for (kind, tf), best in winners.items():
        cur = CANDIDATES[(kind, tf)]["current_iv"]
        flag = "SAME as current" if best == cur else ("CHANGE" if best is not None else "NO ROBUST THRESHOLD FOUND")
        print(f"{kind}/{tf}: best={best} current={cur}  [{flag}]")


if __name__ == "__main__":
    main()
