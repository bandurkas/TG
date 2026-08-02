from __future__ import annotations
"""Portfolio+quarter validation (same pipeline as u3_solo_validation.py) for
the 5 new BB/MB rejection-close robust candidates found by
bb_mb_rejection_sweep.py (best-by-train-avg_pnl per pair). Per-trade robust
!= portfolio robust != quarter robust -- proven repeatedly this session
(30m/OB, multi-TF FVG, and U3's own table). MB in particular was
deactivated 2026-07-07 for a real live-money reason ("the sole bleeder,
15 trades, -$33.72, last 8 closes all MB") -- rejection-close might fix the
fakeout-entry problem that caused that, but it needs the same scrutiny as
every other candidate before trusting it, not a free pass because the
per-trade numbers look good.
"""
import sys
import numpy as np

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
from ob_depth_sweep import MAX_LOOKAHEAD, BPD, DAYS_PER_YEAR
from fvg_rejection_entry import find_rejection_entry

DATA_DIR = "/Users/styserg/Desktop/Tyagach/data"
DVOL_JSON = f"{DATA_DIR}/eth_dvol_1h_4y.json"
TF_FILES = {
    "15m": f"{DATA_DIR}/eth_15m.csv",
    "30m": f"{DATA_DIR}/eth_30m.csv",
    "1h": f"{DATA_DIR}/eth_1h_rs.csv",
    "2h": f"{DATA_DIR}/eth_2h.csv",
}
HAIRCUT = 0.85
SWING_ORDER = 3
N_QUARTERS = 8
IV_THRESHOLD = {"BB": 55.0, "MB": 50.0}

# Best-by-train-avg_pnl robust combo per pair (results/bb_mb_rejection_sweep_robust.csv)
CANDIDATES = {
    ("BB", "15m"): dict(depth_frac=0.325, r_target=4.0,  expiry_days=0.125),
    ("MB", "15m"): dict(depth_frac=0.425, r_target=7.0,  expiry_days=0.125),
    ("MB", "30m"): dict(depth_frac=0.400, r_target=10.0, expiry_days=0.167),
    ("MB", "1h"):  dict(depth_frac=0.400, r_target=10.0, expiry_days=0.25),
    ("MB", "2h"):  dict(depth_frac=0.500, r_target=3.0,  expiry_days=0.25),
}


def detect_zones(kind, df):
    swings = structure.detect_swings(df, order=SWING_ORDER)
    swings, events = structure.label_and_track(df, swings)
    fvgs = structure.detect_fvg(df)
    obs = ob_mod.detect_ob(df, swings, fvgs)
    if kind == "BB":
        bbs = bb_mod.detect_bb(df, obs, events)
        return build_zones([], bbs, [])
    else:  # MB
        mbs = mb_mod.detect_mb(df, events, obs)
        return build_zones([], [], mbs)


def load_tf(tf):
    df = structure.load_csv(TF_FILES[tf])
    n = len(df)
    ts = df["ts_ms"].values
    iv_series = load_dvol_aligned(DVOL_JSON, df)
    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    return df, n, ts, iv_series, o, h, l, c


def build_candidates(kind, tf, cfg, df, n, ts, iv_series, o, h, l, c):
    zones = detect_zones(kind, df)
    depth_frac, rt, exp_days = cfg["depth_frac"], cfg["r_target"], cfg["expiry_days"]
    bpd = BPD[tf]
    max_lookahead = MAX_LOOKAHEAD[tf]
    iv_thresh = IV_THRESHOLD[kind]

    out = []
    for zone in zones:
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
        tpd = len(closed) / split_days[split]
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


def main():
    print(f"haircut={HAIRCUT}")
    print(f"{'pair':<10} {'split':<12} {'n_closed':>8} {'trades/day':>10} {'return%':>10} {'maxDD%':>7} {'calmar':>8}")
    print("-" * 78)
    summary = []
    for (kind, tf), cfg in CANDIDATES.items():
        df, n, ts, iv_series, o, h, l, c = load_tf(tf)
        candidates = build_candidates(kind, tf, cfg, df, n, ts, iv_series, o, h, l, c)
        pair_label = f"{tf}/{kind}"

        for split, n_closed, tpd, ret, dd, calmar in run_splits(candidates, ts):
            print(f"{pair_label:<10} {split:<12} {n_closed:>8} {tpd:>10.2f} {ret:>+10.1f} {dd:>7.1f} {calmar:>8.2f}")

        rets, dds = run_quarters(candidates, ts)
        n_negative = sum(1 for r in rets if r < 0)
        print(f"{pair_label:<10} quarters: {n_negative}/{N_QUARTERS} negative, "
              f"mean {np.mean(rets):+.1f}%, worst {min(rets):+.1f}%, maxDD-worst {max(dds):.1f}%")
        print()
        summary.append((pair_label, cfg, n_negative, np.mean(rets), min(rets), max(dds)))

    print("\n=== RANKED SUMMARY (by fewest negative quarters, then best worst-quarter) ===")
    summary.sort(key=lambda r: (r[2], -r[4]))
    print(f"{'pair':<10} {'neg_q':>6} {'mean%':>8} {'worst%':>8} {'maxDD%':>7}  cfg")
    for pair_label, cfg, n_negative, mean_r, worst_r, worst_dd in summary:
        print(f"{pair_label:<10} {n_negative:>6} {mean_r:>+8.1f} {worst_r:>+8.1f} {worst_dd:>7.1f}  {cfg}")


if __name__ == "__main__":
    main()
