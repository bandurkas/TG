from __future__ import annotations
"""SL buffer recalibration -- flat BUFFER_FRAC=0.0015 (services/config.py)
never scaled by timeframe or volatility, inherited unchanged from
options_backtest.py, never independently tuned. Live-data check (2026-08-02
chat) found the median single-bar range already exceeds the observed SL
distance at every TF (15m bar range median 0.371% vs observed SL dist
~0.15-0.35%; 2h bar range median 1.09-1.14% vs observed SL dist ~0.17%) --
the stop is tighter than ordinary single-candle noise, worse at higher TFs
where price range grows but the flat buffer doesn't.

This tests replacing the flat buffer with buf = atr_mult * ATR(14) computed
causally on each TF's own bars (Wilder-style simple rolling mean of true
range, no lookahead -- uses only bars up to and including the zone's
valid_from index).

Run: python3 sl_buffer_atr_sweep.py
"""
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
import structure
from dvol import load_dvol_aligned
import bs_pricer as bsp
from portfolio import Candidate, stats
from tyagach_samedir_ab import simulate_tagged
from tyagach_portfolio_multitf import STARTING_BALANCE
from ob_depth_sweep import detect_ob_zones, MAX_LOOKAHEAD, BPD, DAYS_PER_YEAR, IV_THRESHOLD
from fvg_depth_sweep import detect_fvg_zones

DATA_DIR = "/Users/styserg/Desktop/Tyagach/data"
DVOL_JSON = f"{DATA_DIR}/eth_dvol_1h_4y.json"
TF_FILES = {
    "15m": f"{DATA_DIR}/eth_15m.csv",
    "30m": f"{DATA_DIR}/eth_30m.csv",
    "1h": f"{DATA_DIR}/eth_1h_rs.csv",
    "2h": f"{DATA_DIR}/eth_2h.csv",
}
HAIRCUT = 0.85
N_QUARTERS = 8
ATR_PERIOD = 14
BUFFER_FRAC = 0.0015  # current live value, kept as the k=0 baseline reference

CANDIDATES = {
    ("OB", "1h"):   dict(depth_frac=0.650, r_target=5.0,  expiry_days=0.125),
    ("OB", "30m"):  dict(depth_frac=0.300, r_target=10.0, expiry_days=0.125),
    ("OB", "15m"):  dict(depth_frac=0.300, r_target=10.0, expiry_days=0.125),
    ("FVG", "1h"):  dict(depth_frac=0.325, r_target=7.0,  expiry_days=0.167),
    ("FVG", "30m"): dict(depth_frac=0.300, r_target=7.0,  expiry_days=0.125),
    ("FVG", "15m"): dict(depth_frac=0.300, r_target=10.0, expiry_days=0.125),
}

import portfolio as portfolio_mod
import tyagach_samedir_ab as samedir_mod
samedir_mod.PRIORITY = {**portfolio_mod.PRIORITY, "FVG": 2}
samedir_mod.WEIGHT_PCT = {**samedir_mod.WEIGHT_PCT, "FVG": samedir_mod.WEIGHT_PCT["OB"]}
samedir_mod.MAX_OPEN_PER_ZONE = {**samedir_mod.MAX_OPEN_PER_ZONE, "FVG": samedir_mod.MAX_OPEN_PER_ZONE["OB"]}


def atr_series(h, l, c, period=ATR_PERIOD):
    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    return pd.Series(tr).rolling(period, min_periods=period).mean().values


def find_entry(o, h, l, c, atr, zone, n, depth_frac, max_lookahead, buf_mode, atr_mult):
    is_long = zone.direction == "bullish"
    zlo, zhi = zone.zone_low, zone.zone_high
    if buf_mode == "flat":
        buf = BUFFER_FRAC * ((zlo + zhi) / 2)
    else:
        a = atr[zone.valid_from]
        if np.isnan(a):
            return None
        buf = atr_mult * a
    stop_price = (zlo - buf) if is_long else (zhi + buf)
    entry_level = (zhi - depth_frac * (zhi - zlo)) if is_long else (zlo + depth_frac * (zhi - zlo))
    start, end = zone.valid_from, min(n - 1, zone.valid_from + max_lookahead)
    for i in range(start, end + 1):
        hi_, lo_, cl_ = h[i], l[i], c[i]
        if is_long and cl_ < stop_price:
            return None
        if (not is_long) and cl_ > stop_price:
            return None
        touched = (lo_ <= entry_level) if is_long else (hi_ >= entry_level)
        if touched:
            rejected = (cl_ > entry_level) if is_long else (cl_ < entry_level)
            if rejected:
                return i, entry_level, stop_price
    return None


def load_tf(tf):
    df = structure.load_csv(TF_FILES[tf])
    n = len(df)
    ts = df["ts_ms"].values
    iv_series = load_dvol_aligned(DVOL_JSON, df)
    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    atr = atr_series(h, l, c)
    return df, n, ts, iv_series, o, h, l, c, atr


def build_candidates(kind, tf, cfg, df, n, ts, iv_series, o, h, l, c, atr, buf_mode, atr_mult):
    zones = detect_ob_zones(df) if kind == "OB" else detect_fvg_zones(df)
    depth_frac, rt, exp_days = cfg["depth_frac"], cfg["r_target"], cfg["expiry_days"]
    bpd = BPD[tf]
    max_lookahead = MAX_LOOKAHEAD[tf]

    out = []
    for zone in zones:
        found = find_entry(o, h, l, c, atr, zone, n, depth_frac, max_lookahead, buf_mode, atr_mult)
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
        out.append((tf, Candidate(kind, zone.direction, entry_ts, exit_ts, entry_price, pnl_per_unit)))
    return out


def run_splits(candidates, ts):
    n = len(ts)
    cut1_ts, cut2_ts = int(ts[int(n * 0.6)]), int(ts[int(n * 0.8)])
    span_days = (int(ts[-1]) - int(ts[0])) / 1000 / 86400
    split_days = {"train": span_days * 0.6, "validation": span_days * 0.2, "holdout": span_days * 0.2}
    by_split = {"train": [], "validation": [], "holdout": []}
    for tf_, cand in candidates:
        if cand.entry_idx < cut1_ts:
            by_split["train"].append((tf_, cand))
        elif cand.entry_idx < cut2_ts:
            by_split["validation"].append((tf_, cand))
        else:
            by_split["holdout"].append((tf_, cand))
    rows = []
    for split in ["train", "validation", "holdout"]:
        final, curve, closed = simulate_tagged(by_split[split], "per_tf")
        s = stats(STARTING_BALANCE, final, curve, len(closed))
        tpd = len(closed) / split_days[split] if split_days[split] else 0
        rows.append((split, s["n_closed"], tpd, s["total_return_pct"], s["max_dd_pct"], s["calmar"]))
    return rows


def run_quarters(candidates, ts):
    ts_start, ts_end = int(ts[0]), int(ts[-1])
    span = ts_end - ts_start
    edges = [ts_start + int(span * i / N_QUARTERS) for i in range(N_QUARTERS + 1)]
    rets, dds = [], []
    for q in range(N_QUARTERS):
        q_lo, q_hi = edges[q], edges[q + 1]
        q_cands = [(tf_, cand) for tf_, cand in candidates if q_lo <= cand.entry_idx < q_hi]
        final, curve, closed = simulate_tagged(q_cands, "per_tf")
        s = stats(STARTING_BALANCE, final, curve, len(closed))
        rets.append(s["total_return_pct"])
        dds.append(s["max_dd_pct"])
    return rets, dds


def eval_variant(buf_mode, atr_mult, label):
    print(f"\n=== {label} ===")
    print(f"{'pair':<10} {'split':<12} {'n_closed':>8} {'trades/day':>10} {'return%':>10} {'maxDD%':>7} {'calmar':>8}")
    summary = []
    for (kind, tf), cfg in CANDIDATES.items():
        df, n, ts, iv_series, o, h, l, c, atr = load_tf(tf)
        candidates = build_candidates(kind, tf, cfg, df, n, ts, iv_series, o, h, l, c, atr, buf_mode, atr_mult)
        pair_label = f"{tf}/{kind}"
        for split, n_closed, tpd, ret, dd, calmar in run_splits(candidates, ts):
            print(f"{pair_label:<10} {split:<12} {n_closed:>8} {tpd:>10.2f} {ret:>+10.1f} {dd:>7.1f} {calmar:>8.2f}")
        rets, dds = run_quarters(candidates, ts)
        n_negative = sum(1 for r in rets if r < 0)
        print(f"{pair_label:<10} quarters: {n_negative}/{N_QUARTERS} negative, "
              f"mean {np.mean(rets):+.1f}%, worst {min(rets):+.1f}%, maxDD-worst {max(dds):.1f}%")
        summary.append((pair_label, n_negative, np.mean(rets), min(rets), max(dds)))
    return summary


def main():
    baseline = eval_variant("flat", 0.0, "BASELINE (flat BUFFER_FRAC=0.0015, live)")
    results = {"flat": baseline}
    for mult in (1.0, 1.5, 2.0, 3.0):
        results[f"atr{mult}"] = eval_variant("atr", mult, f"ATR buffer, mult={mult}")

    print("\n\n=== SUMMARY: negative quarters / mean / worst by variant ===")
    print(f"{'pair':<10} {'flat':>18}", *(f"{'atr'+str(m):>18}" for m in (1.0, 1.5, 2.0, 3.0)))
    pairs = [p[0] for p in baseline]
    for i, pair in enumerate(pairs):
        row = [f"{results['flat'][i][1]}/{N_QUARTERS} m{results['flat'][i][2]:+.0f} w{results['flat'][i][3]:+.0f}"]
        for mult in (1.0, 1.5, 2.0, 3.0):
            r = results[f"atr{mult}"][i]
            row.append(f"{r[1]}/{N_QUARTERS} m{r[2]:+.0f} w{r[3]:+.0f}")
        print(f"{pair:<10}", *[f"{x:>18}" for x in row])


if __name__ == "__main__":
    main()
