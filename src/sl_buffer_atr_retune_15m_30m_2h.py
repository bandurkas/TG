from __future__ import annotations
"""Retune the SL buffer for 15m/30m/2h (OB/FVG/MB) -- the ATR mult grid tried
for 1h (1.0/1.5/2.0/3.0, sl_buffer_atr_sweep.py) made these TFs worse, but
that doesn't mean flat BUFFER_FRAC=0.0015 is actually good there either --
it was kept only because it lost less badly than the tested ATR mults, never
retuned itself. This sweeps a finer, LOWER mult grid (0.25-2.0) plus flat,
per-pair solo (train/val/holdout + 8-quarter), to see if some smaller mult
beats flat on 15m/30m/2h instead of just being "the least-bad ATR option".

Adds MB (all 3 TFs) alongside OB/FVG -- the real 6-trade losing streak
(2026-08-02, SL_BUFFER_HANDOFF) included a 15m/MB trade, not just OB/FVG.
2h/OB and 2h/FVG were never swept at all before (original script only did
15m/30m/1h) -- added here since 2h is live and in scope per this request.

Run: python3 sl_buffer_atr_retune_15m_30m_2h.py
"""
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
import structure
import ob as ob_mod
import mb as mb_mod
from zones import build_zones
from dvol import load_dvol_aligned
import bs_pricer as bsp
from portfolio import Candidate, stats
from tyagach_samedir_ab import simulate_tagged
from tyagach_portfolio_multitf import STARTING_BALANCE
from ob_depth_sweep import detect_ob_zones, MAX_LOOKAHEAD, BPD, DAYS_PER_YEAR
from fvg_depth_sweep import detect_fvg_zones

DATA_DIR = "/Users/styserg/Desktop/Tyagach/data"
DVOL_JSON = f"{DATA_DIR}/eth_dvol_1h_4y.json"
TF_FILES = {
    "15m": f"{DATA_DIR}/eth_15m.csv", "30m": f"{DATA_DIR}/eth_30m.csv",
    "1h": f"{DATA_DIR}/eth_1h_rs.csv", "2h": f"{DATA_DIR}/eth_2h.csv",
}
HAIRCUT = 0.85
SWING_ORDER = 3
N_QUARTERS = 8
ATR_PERIOD = 14
BUFFER_FRAC = 0.0015
IV_THRESHOLD = {"OB": 50.0, "FVG": 50.0, "MB": 50.0}
MULT_GRID = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0)

import portfolio as portfolio_mod
import tyagach_samedir_ab as samedir_mod
samedir_mod.PRIORITY = {**portfolio_mod.PRIORITY, "FVG": 2}
samedir_mod.WEIGHT_PCT = {**samedir_mod.WEIGHT_PCT, "FVG": samedir_mod.WEIGHT_PCT["OB"]}
samedir_mod.MAX_OPEN_PER_ZONE = {**samedir_mod.MAX_OPEN_PER_ZONE, "FVG": samedir_mod.MAX_OPEN_PER_ZONE["OB"]}

# Live cell configs for the cells in scope (== tyagach/services/config.py CELL_CONFIG).
CELLS = {
    ("15m", "OB"):  dict(depth_frac=0.700, r_target=4.0,  expiry_days=0.083),
    ("15m", "FVG"): dict(depth_frac=0.300, r_target=10.0, expiry_days=0.125),
    ("15m", "MB"):  dict(depth_frac=0.425, r_target=7.0,  expiry_days=0.125),
    ("30m", "OB"):  dict(depth_frac=0.300, r_target=10.0, expiry_days=0.125),
    ("30m", "FVG"): dict(depth_frac=0.300, r_target=7.0,  expiry_days=0.125),
    ("30m", "MB"):  dict(depth_frac=0.400, r_target=10.0, expiry_days=0.167),
    ("2h",  "OB"):  dict(depth_frac=0.675, r_target=5.0,  expiry_days=0.25),
    ("2h",  "FVG"): dict(depth_frac=0.675, r_target=10.0, expiry_days=0.167),
    ("2h",  "MB"):  dict(depth_frac=0.500, r_target=3.0,  expiry_days=0.25),
}


def atr_series(h, l, c, period=ATR_PERIOD):
    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    return pd.Series(tr).rolling(period, min_periods=period).mean().values


def detect_zones(kind, df):
    if kind == "OB":
        return detect_ob_zones(df)
    if kind == "FVG":
        return detect_fvg_zones(df)
    swings = structure.detect_swings(df, order=SWING_ORDER)
    swings, events = structure.label_and_track(df, swings)
    fvgs = structure.detect_fvg(df)
    obs = ob_mod.detect_ob(df, swings, fvgs)
    mbs = mb_mod.detect_mb(df, events, obs)
    return build_zones([], [], mbs)


def find_entry(o, h, l, c, atr, zone, n, depth_frac, max_lookahead, buf_mode, atr_mult):
    is_long = zone.direction == "bullish"
    zlo, zhi = zone.zone_low, zone.zone_high
    if buf_mode == "flat":
        buf = BUFFER_FRAC * ((zlo + zhi) / 2)
    else:
        if zone.valid_from >= len(atr):
            return None
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
    zones = detect_zones(kind, df)
    depth_frac, rt, exp_days = cfg["depth_frac"], cfg["r_target"], cfg["expiry_days"]
    bpd = BPD[tf]
    max_lookahead = MAX_LOOKAHEAD[tf]
    iv_thresh = IV_THRESHOLD[kind]

    out = []
    for zone in zones:
        found = find_entry(o, h, l, c, atr, zone, n, depth_frac, max_lookahead, buf_mode, atr_mult)
        if found is None:
            continue
        entry_idx, entry_price, stop_price = found
        if abs(entry_price - stop_price) <= 0:
            continue
        iv0 = iv_series[entry_idx]
        if np.isnan(iv0) or iv0 * 100 <= iv_thresh:
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
    rows = {}
    for split in ["train", "validation", "holdout"]:
        final, curve, closed = simulate_tagged(by_split[split], "per_tf")
        s = stats(STARTING_BALANCE, final, curve, len(closed))
        rows[split] = s
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


def eval_cell(kind, tf, cfg, buf_mode, atr_mult):
    df, n, ts, iv_series, o, h, l, c, atr = load_tf(tf)
    candidates = build_candidates(kind, tf, cfg, df, n, ts, iv_series, o, h, l, c, atr, buf_mode, atr_mult)
    splits = run_splits(candidates, ts)
    rets, dds = run_quarters(candidates, ts)
    n_negative = sum(1 for r in rets if r < 0)
    return dict(splits=splits, n_negative=n_negative, mean_q=np.mean(rets), worst_q=min(rets), maxdd_q=max(dds))


def main():
    for (tf, kind), cfg in CELLS.items():
        pair_label = f"{tf}/{kind}"
        print(f"\n=== {pair_label} ===")
        base = eval_cell(kind, tf, cfg, "flat", 0.0)
        h = base["splits"]["holdout"]
        print(f"{'variant':<10} {'val_ret':>8} {'val_calmar':>10} {'hold_ret':>8} {'hold_calmar':>11} "
              f"{'q_neg':>6} {'q_mean':>8} {'q_worst':>8} {'q_maxdd':>8}")
        v = base["splits"]["validation"]
        print(f"{'flat':<10} {v['total_return_pct']:>+8.1f} {v['calmar']:>10.2f} "
              f"{h['total_return_pct']:>+8.1f} {h['calmar']:>11.2f} "
              f"{base['n_negative']:>6} {base['mean_q']:>+8.1f} {base['worst_q']:>+8.1f} {base['maxdd_q']:>8.1f}")
        for mult in MULT_GRID:
            r = eval_cell(kind, tf, cfg, "atr", mult)
            v = r["splits"]["validation"]
            h = r["splits"]["holdout"]
            print(f"{'atr'+str(mult):<10} {v['total_return_pct']:>+8.1f} {v['calmar']:>10.2f} "
                  f"{h['total_return_pct']:>+8.1f} {h['calmar']:>11.2f} "
                  f"{r['n_negative']:>6} {r['mean_q']:>+8.1f} {r['worst_q']:>+8.1f} {r['maxdd_q']:>8.1f}")


if __name__ == "__main__":
    main()
