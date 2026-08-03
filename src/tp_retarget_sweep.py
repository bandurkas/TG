from __future__ import annotations
"""TP retune: does a smaller r_target (closer take-profit -> more frequent
wins) beat the current live r_target once REAL fees are accounted for? SL
buffer stays at its already-retuned live value per cell (ATR for 1h/OB+
1h/FVG, flat overrides for 7 more cells, default flat elsewhere -- see
SL_BUFFER_HANDOFF_2026-08-02.md and the 2026-08-03 follow-up) -- only
r_target is swept here, isolating the TP question.

Reports win_rate, trades/day, and explicit FEE DRAG (total fees / gross
premium collected) alongside the usual per-trade + portfolio(60/20/20) +
8-quarter robustness -- a smaller r_target fires more often, so the "does
it overpay in fees" question needs fees broken out, not just net return.

Run: python3 tp_retarget_sweep.py
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
IV_THRESHOLD = {"OB": 50.0, "FVG": 50.0, "BB": 55.0, "MB": 50.0}
FEE_RATE = 0.0003
FEE_CAP_PCT = 0.125

# Current LIVE cell configs (== tyagach/services/config.py, 2026-08-03).
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
ATR_BUFFER_MULT = {("1h", "OB"): 2.0, ("1h", "FVG"): 2.0}
FLAT_BUFFER_FRAC_OVERRIDE = {
    ("15m", "FVG"): 0.0025, ("15m", "MB"): 0.005, ("30m", "OB"): 0.002,
    ("30m", "FVG"): 0.003, ("30m", "MB"): 0.0025, ("2h", "OB"): 0.005, ("2h", "FVG"): 0.004,
}
DEFAULT_FLAT = 0.0015
MULT_GRID = (0.3, 0.5, 0.75, 1.0, 1.5, 2.0)  # relative to each cell's current live r_target

import portfolio as portfolio_mod
import tyagach_samedir_ab as samedir_mod
samedir_mod.PRIORITY = {**portfolio_mod.PRIORITY, "FVG": 2}
samedir_mod.WEIGHT_PCT = {**samedir_mod.WEIGHT_PCT, "FVG": samedir_mod.WEIGHT_PCT["OB"]}
samedir_mod.MAX_OPEN_PER_ZONE = {**samedir_mod.MAX_OPEN_PER_ZONE, "FVG": samedir_mod.MAX_OPEN_PER_ZONE["OB"]}


def atr_series(h, l, c, period=ATR_PERIOD):
    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    return pd.Series(tr).rolling(period, min_periods=period).mean().values


def find_entry(o, h, l, c, atr, zone, n, depth_frac, max_lookahead, tf, kind):
    is_long = zone.direction == "bullish"
    zlo, zhi = zone.zone_low, zone.zone_high
    atr_mult = ATR_BUFFER_MULT.get((tf, kind))
    if atr_mult is not None:
        if zone.valid_from >= len(atr) or np.isnan(atr[zone.valid_from]):
            return None
        buf = atr_mult * atr[zone.valid_from]
    else:
        flat_frac = FLAT_BUFFER_FRAC_OVERRIDE.get((tf, kind), DEFAULT_FLAT)
        buf = flat_frac * ((zlo + zhi) / 2)
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


def _fee(notional, premium_total):
    return min(notional * FEE_RATE, abs(premium_total) * FEE_CAP_PCT)


def build_candidates(tf, kind, cfg, df, n, ts, iv_series, o, h, l, c, atr, r_target):
    zones = detect_zones(kind, df)
    depth_frac, exp_days = cfg["depth_frac"], cfg["expiry_days"]
    bpd = BPD[tf]
    max_lookahead = MAX_LOOKAHEAD[tf]
    iv_thresh = IV_THRESHOLD[kind]

    out = []
    fee_total = 0.0
    gross_premium_total = 0.0
    n_tp = n_sl = n_expiry = 0
    for zone in zones:
        found = find_entry(o, h, l, c, atr, zone, n, depth_frac, max_lookahead, tf, kind)
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
        tp_price = entry_price + r_target * risk if is_long else entry_price - r_target * risk
        expiry_bars = int(exp_days * bpd)
        expiry_idx = min(n - 1, entry_idx + expiry_bars)
        exit_idx = expiry_idx
        reason = "expiry"
        for j in range(entry_idx + 1, expiry_idx + 1):
            hit_sl = (l[j] <= stop_price) if is_long else (h[j] >= stop_price)
            hit_tp = (h[j] >= tp_price) if is_long else (l[j] <= tp_price)
            if hit_sl or hit_tp:
                exit_idx = j
                reason = "sl" if hit_sl else "tp"
                break
        if reason == "tp":
            n_tp += 1
        elif reason == "sl":
            n_sl += 1
        else:
            n_expiry += 1
        spot_exit = c[exit_idx]
        side = "P" if is_long else "C"
        elapsed_days = (exit_idx - entry_idx) / bpd
        T_remaining = max(0.0, (exp_days - elapsed_days) / DAYS_PER_YEAR)
        T_entry = exp_days / DAYS_PER_YEAR
        strike = entry_price
        premium = bsp.price(side, entry_price, strike, T_entry, iv0) * HAIRCUT
        value_exit = bsp.price(side, spot_exit, strike, T_remaining, iv0)
        pnl_per_unit_gross = premium - value_exit
        # fee proxy at 1 lot-equivalent unit -- absolute $ fee scales with
        # position sizing in the portfolio sim, but the DRAG RATIO (fee /
        # gross premium) is size-invariant, which is what we need to answer
        # "does a tighter TP overpay in fees relative to what it collects".
        gross_premium_total += abs(premium)
        fee_total += 2 * _fee(entry_price, abs(pnl_per_unit_gross))  # notional proxy = entry_price (1-unit)
        entry_ts, exit_ts = int(ts[entry_idx]), int(ts[exit_idx])
        out.append((tf, Candidate(kind, zone.direction, entry_ts, exit_ts, entry_price, pnl_per_unit_gross)))
    n_total = n_tp + n_sl + n_expiry
    win_rate = n_tp / n_total if n_total else 0.0
    fee_drag = fee_total / gross_premium_total if gross_premium_total else 0.0
    return out, dict(n_tp=n_tp, n_sl=n_sl, n_expiry=n_expiry, win_rate=win_rate, fee_drag=fee_drag)


def load_tf(tf):
    df = structure.load_csv(TF_FILES[tf])
    n = len(df)
    ts = df["ts_ms"].values
    iv_series = load_dvol_aligned(DVOL_JSON, df)
    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    atr = atr_series(h, l, c)
    return df, n, ts, iv_series, o, h, l, c, atr


def run_splits(candidates, ts):
    n = len(ts)
    cut1_ts, cut2_ts = int(ts[int(n * 0.6)]), int(ts[int(n * 0.8)])
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
        rows[split] = stats(STARTING_BALANCE, final, curve, len(closed))
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


def eval_cell(tf, kind, cfg, r_target):
    df, n, ts, iv_series, o, h, l, c, atr = load_tf(tf)
    candidates, meta = build_candidates(tf, kind, cfg, df, n, ts, iv_series, o, h, l, c, atr, r_target)
    splits = run_splits(candidates, ts)
    rets, dds = run_quarters(candidates, ts)
    n_negative = sum(1 for r in rets if r < 0)
    return dict(splits=splits, meta=meta, n_negative=n_negative, mean_q=np.mean(rets),
                worst_q=min(rets), maxdd_q=max(dds))


def print_row(label, r):
    v, h = r["splits"]["validation"], r["splits"]["holdout"]
    m = r["meta"]
    print(f"{label:<10} win={m['win_rate']*100:>5.1f}% fee_drag={m['fee_drag']*100:>5.2f}% "
          f"val_ret={v['total_return_pct']:>+8.1f} val_calmar={v['calmar']:>7.2f} "
          f"hold_ret={h['total_return_pct']:>+8.1f} hold_calmar={h['calmar']:>7.2f} "
          f"q_neg={r['n_negative']}/8 q_worst={r['worst_q']:>+6.1f} q_maxdd={r['maxdd_q']:>5.1f}")


def main():
    for (tf, kind), cfg in LIVE_CELLS.items():
        base_rt = cfg["r_target"]
        print(f"\n=== {tf}/{kind}  (live r_target={base_rt}) ===")
        for mult in MULT_GRID:
            rt = round(base_rt * mult, 2)
            label = f"rt{rt}" + ("*" if mult == 1.0 else "")
            r = eval_cell(tf, kind, cfg, rt)
            print_row(label, r)


if __name__ == "__main__":
    main()
