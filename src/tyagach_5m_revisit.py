"""Revisit 5m TF at the new (2026-07-03) lower IV thresholds. Previously
excluded from ACTIVE_CELLS with the note "5m - dead (gross < round-trip fee)"
-- that call was made at the OLD higher thresholds (60/70/60); re-check
whether it holds at the new ones, applying the 2026-07-03 amendment's own
bar (positive on train AND validation AND holdout, not holdout alone) from
the start this time.

Usage: python3 tyagach_5m_revisit.py
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, "/Users/sabar/Desktop/smc_options/src")
import structure
import ob
import bb
import mb
from zones import build_zones
from dvol import load_dvol_aligned
import bs_pricer as bsp
from options_backtest import _find_midpoint_entry, DAYS_PER_YEAR

DATA_DIR = "/Users/sabar/Desktop/smc_options/data"
DVOL_JSON = f"{DATA_DIR}/eth_dvol_1h_4y.json"
CSV_PATH = f"{DATA_DIR}/eth_5m.csv"
BPD = 288  # 5m bars/day

IV_THRESHOLDS = [50, 55, 60, 65, 70, 75]
BEST_PARAMS = {
    "OB": {"r_target": 3.0, "expiry_days": 0.5},
    "MB": {"r_target": 3.0, "expiry_days": 0.5},
    "BB": {"r_target": 2.5, "expiry_days": 5.0},
}
FEE_RATE = 0.0003
FEE_CAP_PCT = 0.125
LOT_SIZE = 0.10
MARGIN_PCT = 0.15
WEIGHT_PCT = {"OB": 0.12, "MB": 0.18, "BB": 0.28}
BALANCE = 2000.0


def compute_fee(n_lots, spot, premium_per_unit):
    num_units = n_lots * LOT_SIZE
    notional = num_units * spot
    premium_total = premium_per_unit * num_units
    fee_per_side = min(notional * FEE_RATE, premium_total * FEE_CAP_PCT)
    return 2 * fee_per_side


def lots_for_zone(kind, spot):
    budget = WEIGHT_PCT[kind] * BALANCE
    margin_per_lot = LOT_SIZE * spot * MARGIN_PCT
    return max(0, int(budget / margin_per_lot))


def main():
    df = structure.load_csv(CSV_PATH)
    n = len(df)
    cut1, cut2 = int(n * 0.6), int(n * 0.8)
    splits = {
        "train": df.iloc[:cut1].reset_index(drop=True),
        "validation": df.iloc[cut1:cut2].reset_index(drop=True),
        "holdout": df.iloc[cut2:].reset_index(drop=True),
    }

    rows = []
    for split_name, sdf in splits.items():
        swings = structure.detect_swings(sdf, order=3)
        swings, events = structure.label_and_track(sdf, swings)
        fvgs = structure.detect_fvg(sdf)
        obs_ = ob.detect_ob(sdf, swings, fvgs)
        bbs_ = bb.detect_bb(sdf, obs_, events)
        mbs_ = mb.detect_mb(sdf, events, obs_)
        zones = build_zones(obs_, bbs_, mbs_)
        iv_series = load_dvol_aligned(DVOL_JSON, sdf)

        o, h, l, c = sdf["open"].values, sdf["high"].values, sdf["low"].values, sdf["close"].values
        n_bars = len(sdf)

        for kind, params in BEST_PARAMS.items():
            rt, exp_days = params["r_target"], params["expiry_days"]
            expiry_bars = int(exp_days * BPD)
            kind_zones = [z for z in zones if z.kind == kind]

            entries = []
            for zone in kind_zones:
                found = _find_midpoint_entry(o, h, l, c, zone, n_bars)
                if found is None:
                    continue
                entry_idx, entry_price, stop_price = found
                iv0 = iv_series[entry_idx]
                if np.isnan(iv0) or abs(entry_price - stop_price) <= 0:
                    continue
                is_long = zone.direction == "bullish"
                risk = abs(entry_price - stop_price)
                tp_price = entry_price + rt * risk if is_long else entry_price - rt * risk
                expiry_idx = min(n_bars - 1, entry_idx + expiry_bars)
                exit_idx = expiry_idx
                for j in range(entry_idx + 1, expiry_idx + 1):
                    hit_sl = (l[j] <= stop_price) if is_long else (h[j] >= stop_price)
                    hit_tp = (h[j] >= tp_price) if is_long else (l[j] <= tp_price)
                    if hit_sl or hit_tp:
                        exit_idx = j
                        break
                spot_exit = c[exit_idx]
                side = "P" if is_long else "C"
                elapsed_days = (exit_idx - entry_idx) / BPD
                T_remaining = max(0.0, (exp_days - elapsed_days) / DAYS_PER_YEAR)
                T_entry = exp_days / DAYS_PER_YEAR
                sigma = iv0
                premium = bsp.price(side, entry_price, entry_price, T_entry, sigma)
                value_exit = bsp.price(side, spot_exit, entry_price, T_remaining, sigma)
                gross_pnl_per_eth = premium - value_exit
                n_lots = lots_for_zone(kind, entry_price)
                if n_lots < 1:
                    continue
                num_units = n_lots * LOT_SIZE
                gross_pnl = gross_pnl_per_eth * num_units
                fee_rt = compute_fee(n_lots, entry_price, premium)
                net_pnl = gross_pnl - fee_rt
                entries.append({"iv0_pct": iv0 * 100, "gross_pnl": gross_pnl, "net_pnl": net_pnl})

            if not entries:
                continue
            df_e = pd.DataFrame(entries)
            for thr in IV_THRESHOLDS:
                sel = df_e[df_e["iv0_pct"] > thr]
                n_sel = len(sel)
                if n_sel < 10:
                    continue
                rows.append({
                    "kind": kind, "split": split_name, "iv_threshold": thr, "n": n_sel,
                    "avg_gross_$": round(sel["gross_pnl"].mean(), 4),
                    "avg_net_$": round(sel["net_pnl"].mean(), 4),
                    "pct_positive": round((sel["net_pnl"] > 0).mean() * 100, 1),
                })

    out = pd.DataFrame(rows)
    out.to_csv("/Users/sabar/Desktop/smc_options/results/sweep_5m_revisit.csv", index=False)

    print(f"{'kind':<4} {'thr':>4} {'train':>16} {'valid':>16} {'holdout':>16}  verdict")
    print("-" * 75)
    for kind in ["OB", "MB", "BB"]:
        for thr in IV_THRESHOLDS:
            g = out[(out.kind == kind) & (out.iv_threshold == thr)].set_index("split")
            if not all(s in g.index for s in ("train", "validation", "holdout")):
                continue
            tr, va, ho = g.loc["train"], g.loc["validation"], g.loc["holdout"]
            ok = tr["avg_net_$"] > 0 and va["avg_net_$"] > 0 and ho["avg_net_$"] > 0
            print(f"{kind:<4} {thr:>4} "
                  f"{tr['avg_net_$']:+.3f}(n={int(tr['n']):<5}) "
                  f"{va['avg_net_$']:+.3f}(n={int(va['n']):<5}) "
                  f"{ho['avg_net_$']:+.3f}(n={int(ho['n']):<5})  "
                  f"{'PASS' if ok else ''}")


if __name__ == "__main__":
    main()
