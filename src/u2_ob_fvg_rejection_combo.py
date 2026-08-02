from __future__ import annotations
"""U2 (TYAGACH_UPGRADE_PLAN_2026-08-02.md): does the 2h-OB(rejection) +
2h-FVG(rejection) COMBINATION beat either cell alone? The only prior
combination check (fvg_portfolio_compare.py) used the old touch-only entry;
2h/FVG's rejection-close config is stronger now, so re-run with both cells
on find_rejection_entry.

Configs: 2h/OB = the live U1 config (depth=0.675, r_target=5.0,
expiry=0.25d, commit e1556b5). 2h/FVG = the session's strongest standalone
candidate (depth=0.675, r_target=10.0, expiry=0.167d), validated separately
at 8/8 quarters positive, mean +25.2%, worst +2.1%.

Same two-stage methodology as every other portfolio decision this session:
60/20/20 train/validation/holdout portfolio compounding (fvg_portfolio_compare.py)
+ 8-quarter fresh-capital robustness (ob_2h_quarter_robustness.py). Only
1.8% zone overlap between OB/FVG at 2h (checked earlier this session) so
combining is plausible, but per-trade robust != portfolio robust has bitten
this project twice already (30m/OB, multi-TF FVG) -- check properly.
"""
import sys
import numpy as np

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
import structure
from dvol import load_dvol_aligned
import bs_pricer as bsp
from portfolio import Candidate, stats
from tyagach_samedir_ab import simulate_tagged
from tyagach_portfolio_multitf import STARTING_BALANCE
from ob_depth_sweep import detect_ob_zones, MAX_LOOKAHEAD, BPD, DAYS_PER_YEAR, IV_THRESHOLD
from fvg_depth_sweep import detect_fvg_zones
from fvg_rejection_entry import find_rejection_entry

DATA_DIR = "/Users/styserg/Desktop/Tyagach/data"
DVOL_JSON = f"{DATA_DIR}/eth_dvol_1h_4y.json"
TF_FILE_2H = f"{DATA_DIR}/eth_2h.csv"
HAIRCUT = 0.85
N_QUARTERS = 8

CFG_2H_OB = ("OB", dict(depth_frac=0.675, r_target=5.0, expiry_days=0.25))
CFG_2H_FVG = ("FVG", dict(depth_frac=0.675, r_target=10.0, expiry_days=0.167))

# fvg_portfolio_compare.py's finding: simulate_tagged (tyagach_samedir_ab.py)
# reads WEIGHT_PCT/MAX_OPEN_PER_ZONE/PRIORITY as ITS OWN module globals --
# FVG must be patched in directly or it silently gets a $0 budget.
import portfolio as portfolio_mod
import tyagach_samedir_ab as samedir_mod
samedir_mod.PRIORITY = {**portfolio_mod.PRIORITY, "FVG": 2}
samedir_mod.WEIGHT_PCT = {**samedir_mod.WEIGHT_PCT, "FVG": samedir_mod.WEIGHT_PCT["OB"]}
samedir_mod.MAX_OPEN_PER_ZONE = {**samedir_mod.MAX_OPEN_PER_ZONE, "FVG": samedir_mod.MAX_OPEN_PER_ZONE["OB"]}


def build_candidates(kind: str, cfg: dict, df, ts, iv_series, o, h, l, c, n):
    zones = detect_ob_zones(df) if kind == "OB" else detect_fvg_zones(df)
    depth_frac, rt, exp_days = cfg["depth_frac"], cfg["r_target"], cfg["expiry_days"]
    bpd = BPD["2h"]
    max_lookahead = MAX_LOOKAHEAD["2h"]

    out = []
    for zone in zones:
        found = find_rejection_entry(o, h, l, c, zone, n, depth_frac, max_lookahead)
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
        out.append(("2h", Candidate(kind, zone.direction, entry_ts, exit_ts, entry_price, pnl_per_unit)))
    return out


def load_2h():
    df = structure.load_csv(TF_FILE_2H)
    n = len(df)
    ts = df["ts_ms"].values
    iv_series = load_dvol_aligned(DVOL_JSON, df)
    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    return df, n, ts, iv_series, o, h, l, c


SCENARIOS = {
    "2h-OB only": ["OB"],
    "2h-FVG only": ["FVG"],
    "2h-OB + 2h-FVG": ["OB", "FVG"],
}
CFG_BY_KIND = {"OB": CFG_2H_OB[1], "FVG": CFG_2H_FVG[1]}


def run_splits():
    df, n, ts, iv_series, o, h, l, c = load_2h()
    cut1_ts, cut2_ts = int(ts[int(n * 0.6)]), int(ts[int(n * 0.8)])
    span_days = (int(ts[-1]) - int(ts[0])) / 1000 / 86400
    split_days = {"train": span_days * 0.6, "validation": span_days * 0.2, "holdout": span_days * 0.2}

    all_candidates = {}
    for kind in ["OB", "FVG"]:
        all_candidates[kind] = build_candidates(kind, CFG_BY_KIND[kind], df, ts, iv_series, o, h, l, c, n)

    print(f"haircut={HAIRCUT}")
    print(f"{'scenario':<20} {'split':<12} {'n_closed':>8} {'trades/day':>10} {'return%':>10} {'maxDD%':>7} {'calmar':>8}")
    print("-" * 88)
    for name, kinds in SCENARIOS.items():
        by_split = {"train": [], "validation": [], "holdout": []}
        for kind in kinds:
            for tf, cand in all_candidates[kind]:
                if cand.entry_idx < cut1_ts:
                    by_split["train"].append((tf, cand))
                elif cand.entry_idx < cut2_ts:
                    by_split["validation"].append((tf, cand))
                else:
                    by_split["holdout"].append((tf, cand))
        for split in ["train", "validation", "holdout"]:
            final, curve, closed = simulate_tagged(by_split[split], "per_tf")
            s = stats(STARTING_BALANCE, final, curve, len(closed))
            tpd = len(closed) / split_days[split]
            print(f"{name:<20} {split:<12} {s['n_closed']:>8} {tpd:>10.2f} {s['total_return_pct']:>+10.1f} "
                  f"{s['max_dd_pct']:>7.1f} {s['calmar']:>8.2f}")
        print()
    return all_candidates


def run_quarters(all_candidates):
    df, n, ts, iv_series, o, h, l, c = load_2h()
    ts_start, ts_end = int(ts[0]), int(ts[-1])
    span = ts_end - ts_start
    edges = [ts_start + int(span * i / N_QUARTERS) for i in range(N_QUARTERS + 1)]

    for name, kinds in SCENARIOS.items():
        print(f"\n=== {name} (quarter robustness) ===")
        print(f"{'quarter':<10} {'n_closed':>8} {'return%':>10} {'maxDD%':>7} {'calmar':>8}")
        rets = []
        for q in range(N_QUARTERS):
            q_lo, q_hi = edges[q], edges[q + 1]
            q_cands = [(tf, cand) for kind in kinds for tf, cand in all_candidates[kind]
                       if q_lo <= cand.entry_idx < q_hi]
            final, curve, closed = simulate_tagged(q_cands, "per_tf")
            s = stats(STARTING_BALANCE, final, curve, len(closed))
            rets.append(s["total_return_pct"])
            print(f"Q{q+1:<9} {s['n_closed']:>8} {s['total_return_pct']:>+10.1f} {s['max_dd_pct']:>7.1f} {s['calmar']:>8.2f}")
        n_negative = sum(1 for r in rets if r < 0)
        print(f"-- {n_negative}/{N_QUARTERS} quarters negative, mean return {np.mean(rets):+.1f}%, worst {min(rets):+.1f}%")


def main():
    all_candidates = run_splits()
    run_quarters(all_candidates)


if __name__ == "__main__":
    main()
