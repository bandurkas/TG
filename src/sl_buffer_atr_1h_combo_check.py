from __future__ import annotations
"""Combo-portfolio check: does swapping ONLY 1h/OB and 1h/FVG from the flat
BUFFER_FRAC=0.0015 SL buffer to an ATR(14)-scaled buffer help once it's
sitting inside the FULL currently-live 13-cell book (shared balance,
MAX_OPEN_TOTAL_GLOBAL=8, MAX_TOTAL_MARGIN_PCT=0.80 -- correlated with the
other 11 cells)?

Context (SL_BUFFER_HANDOFF_2026-08-02.md + 2026-08-03 follow-up): solo
per-pair sweep (sl_buffer_atr_sweep.py) found the ATR buffer clearly helps
1h/OB and 1h/FVG (holdout calmar 1.50->2.53 and 5.61->9.20 at mult=1.5) but
hurts or is neutral on 15m/30m (correlated tail-loss stacking during the
2024-12->2025-04 ETH crash + 2025-04->08 rally inside the validation split
blows up portfolio maxDD there, even though per-trade edge is fine). Scope
is deliberately narrowed to 1h only. This checks that narrowed change
doesn't get diluted or hurt by interaction with the other 11 live cells
sharing the same account/margin cap.

13-cell set == tyagach/services/config.py ACTIVE_CELLS + CELL_CONFIG as of
2026-08-03 (verified to match u_all13_combo_validation.py's PROPOSED_CELLS).
"""
import sys
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
from portfolio import Candidate, stats
from tyagach_samedir_ab import simulate_tagged
from tyagach_portfolio_multitf import STARTING_BALANCE
from ob_depth_sweep import detect_ob_zones, MAX_LOOKAHEAD, BPD, DAYS_PER_YEAR
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
N_QUARTERS = 8
ATR_PERIOD = 14
BUFFER_FRAC = 0.0015
IV_THRESHOLD = {"OB": 50.0, "FVG": 50.0, "BB": 55.0, "MB": 50.0}

# Live 13-cell set (== tyagach/services/config.py ACTIVE_CELLS/CELL_CONFIG).
LIVE_CELLS = {
    ("15m", "BB"):  dict(kind="BB",  depth_frac=0.500, r_target=2.5,  expiry_days=5.0),
    ("15m", "OB"):  dict(kind="OB",  depth_frac=0.700, r_target=4.0,  expiry_days=0.083),
    ("15m", "MB"):  dict(kind="MB",  depth_frac=0.425, r_target=7.0,  expiry_days=0.125),
    ("30m", "MB"):  dict(kind="MB",  depth_frac=0.400, r_target=10.0, expiry_days=0.167),
    ("1h",  "MB"):  dict(kind="MB",  depth_frac=0.400, r_target=10.0, expiry_days=0.25),
    ("2h",  "MB"):  dict(kind="MB",  depth_frac=0.500, r_target=3.0,  expiry_days=0.25),
    ("2h",  "OB"):  dict(kind="OB",  depth_frac=0.675, r_target=5.0,  expiry_days=0.25),
    ("2h",  "FVG"): dict(kind="FVG", depth_frac=0.675, r_target=10.0, expiry_days=0.167),
    ("30m", "OB"):  dict(kind="OB",  depth_frac=0.300, r_target=10.0, expiry_days=0.125),
    ("1h",  "OB"):  dict(kind="OB",  depth_frac=0.650, r_target=5.0,  expiry_days=0.125),
    ("30m", "FVG"): dict(kind="FVG", depth_frac=0.300, r_target=7.0,  expiry_days=0.125),
    ("1h",  "FVG"): dict(kind="FVG", depth_frac=0.325, r_target=7.0,  expiry_days=0.167),
    ("15m", "FVG"): dict(kind="FVG", depth_frac=0.300, r_target=10.0, expiry_days=0.125),
}
ATR_TARGET_CELLS = {("1h", "OB"), ("1h", "FVG")}

import portfolio as portfolio_mod
import tyagach_samedir_ab as samedir_mod
samedir_mod.PRIORITY = {**portfolio_mod.PRIORITY, "FVG": 2}
samedir_mod.WEIGHT_PCT = {**samedir_mod.WEIGHT_PCT, "FVG": samedir_mod.WEIGHT_PCT["OB"]}
samedir_mod.MAX_OPEN_PER_ZONE = {**samedir_mod.MAX_OPEN_PER_ZONE, "FVG": samedir_mod.MAX_OPEN_PER_ZONE["OB"]}


def atr_series(h, l, c, period=ATR_PERIOD):
    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    return pd.Series(tr).rolling(period, min_periods=period).mean().values


def find_entry_atr(o, h, l, c, atr, zone, n, depth_frac, max_lookahead, atr_mult):
    is_long = zone.direction == "bullish"
    zlo, zhi = zone.zone_low, zone.zone_high
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


def detect_zones(kind, df):
    if kind == "OB":
        return detect_ob_zones(df)
    if kind == "FVG":
        return detect_fvg_zones(df)
    swings = structure.detect_swings(df, order=SWING_ORDER)
    swings, events = structure.label_and_track(df, swings)
    fvgs = structure.detect_fvg(df)
    obs = ob_mod.detect_ob(df, swings, fvgs)
    if kind == "BB":
        bbs = bb_mod.detect_bb(df, obs, events)
        return build_zones([], bbs, [])
    mbs = mb_mod.detect_mb(df, events, obs)
    return build_zones([], [], mbs)


def build_candidates(tf, kind, cfg, df, n, ts, iv_series, o, h, l, c, atr, use_atr, atr_mult):
    zones = detect_zones(kind, df)
    depth_frac, rt, exp_days = cfg["depth_frac"], cfg["r_target"], cfg["expiry_days"]
    bpd = BPD[tf]
    max_lookahead = MAX_LOOKAHEAD[tf]
    iv_thresh = IV_THRESHOLD[kind]

    out = []
    for zone in zones:
        if use_atr and (tf, kind) in ATR_TARGET_CELLS:
            found = find_entry_atr(o, h, l, c, atr, zone, n, depth_frac, max_lookahead, atr_mult)
        else:
            found = find_rejection_entry(o, h, l, c, zone, n, depth_frac, max_lookahead)
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


def build_all(use_atr, atr_mult, tf_cache):
    all_candidates = []
    for (tf, kind), cfg in LIVE_CELLS.items():
        if tf not in tf_cache:
            df = structure.load_csv(TF_FILES[tf])
            n = len(df)
            ts = df["ts_ms"].values
            iv_series = load_dvol_aligned(DVOL_JSON, df)
            o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
            atr = atr_series(h, l, c)
            tf_cache[tf] = (df, n, ts, iv_series, o, h, l, c, atr)
        df, n, ts, iv_series, o, h, l, c, atr = tf_cache[tf]
        all_candidates.extend(build_candidates(tf, kind, cfg, df, n, ts, iv_series, o, h, l, c, atr, use_atr, atr_mult))
    return all_candidates


def run_splits(label, all_candidates, ts_ref):
    n = len(ts_ref)
    cut1_ts, cut2_ts = int(ts_ref[int(n * 0.6)]), int(ts_ref[int(n * 0.8)])
    span_days = (int(ts_ref[-1]) - int(ts_ref[0])) / 1000 / 86400
    split_days = {"train": span_days * 0.6, "validation": span_days * 0.2, "holdout": span_days * 0.2}

    by_split = {"train": [], "validation": [], "holdout": []}
    for tf_, cand in all_candidates:
        if cand.entry_idx < cut1_ts:
            by_split["train"].append((tf_, cand))
        elif cand.entry_idx < cut2_ts:
            by_split["validation"].append((tf_, cand))
        else:
            by_split["holdout"].append((tf_, cand))

    print(f"\n--- {label}: 60/20/20 portfolio ---")
    for split in ["train", "validation", "holdout"]:
        final, curve, closed = simulate_tagged(by_split[split], "per_tf")
        s = stats(STARTING_BALANCE, final, curve, len(closed))
        tpd = len(closed) / split_days[split]
        print(f"{split:<12} n_closed={s['n_closed']:>6} trades/day={tpd:>6.2f} "
              f"return={s['total_return_pct']:>+8.1f}% maxDD={s['max_dd_pct']:>6.1f}% calmar={s['calmar']:>7.2f}")


def run_quarters(label, all_candidates, ts_ref):
    ts_start, ts_end = int(ts_ref[0]), int(ts_ref[-1])
    span = ts_end - ts_start
    edges = [ts_start + int(span * i / N_QUARTERS) for i in range(N_QUARTERS + 1)]
    rets, dds = [], []
    for q in range(N_QUARTERS):
        q_lo, q_hi = edges[q], edges[q + 1]
        q_cands = [(tf_, cand) for tf_, cand in all_candidates if q_lo <= cand.entry_idx < q_hi]
        final, curve, closed = simulate_tagged(q_cands, "per_tf")
        s = stats(STARTING_BALANCE, final, curve, len(closed))
        rets.append(s["total_return_pct"])
        dds.append(s["max_dd_pct"])
    n_negative = sum(1 for r in rets if r < 0)
    print(f"--- {label}: quarters --- {n_negative}/{N_QUARTERS} negative, mean {np.mean(rets):+.1f}%, "
          f"worst {min(rets):+.1f}%, maxDD-worst {max(dds):.1f}%")
    return rets, dds


def main():
    tf_cache = {}
    print("Building candidates for BASELINE (13-cell, flat buffer everywhere, == live today)...")
    baseline = build_all(use_atr=False, atr_mult=0.0, tf_cache=tf_cache)
    print(f"total signals (baseline): {len(baseline)}")

    ts_ref = tf_cache["15m"][2]
    run_splits("BASELINE (all flat)", baseline, ts_ref)
    run_quarters("BASELINE (all flat)", baseline, ts_ref)

    for mult in (1.5, 2.0):
        print(f"\nBuilding candidates for VARIANT (1h/OB+1h/FVG -> ATR mult={mult}, rest unchanged)...")
        variant = build_all(use_atr=True, atr_mult=mult, tf_cache=tf_cache)
        print(f"total signals (variant, mult={mult}): {len(variant)}")
        run_splits(f"VARIANT atr{mult} (1h only)", variant, ts_ref)
        run_quarters(f"VARIANT atr{mult} (1h only)", variant, ts_ref)


if __name__ == "__main__":
    main()
