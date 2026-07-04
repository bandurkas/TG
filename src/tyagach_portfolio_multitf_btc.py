"""Full portfolio-level (compounding, real concurrency caps) backtest of the
CURRENT deployed Tyagach config (6 active cells, multi-TF, live thresholds),
reusing the project's own validated engine (portfolio.py's Candidate/
PortfolioConfig/simulate/stats — the same one that produced the original
39.6% APR / 3932-trade single-TF number in TYAGACH_HANDOFF.md).

Candidates from different TFs are merged into one timeline using absolute
ts_ms (instead of each TF's own positional bar index) so portfolio.simulate's
chronological ordering + concurrency caps work correctly across TFs.

Usage: python3 tyagach_portfolio_multitf.py
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
from portfolio import Candidate, PortfolioConfig, simulate, stats

DATA_DIR = "/Users/sabar/Desktop/smc_options/data"
DVOL_JSON = f"{DATA_DIR}/btc_dvol_1h_4y.json"

# Live config as deployed (tyagach/services/config.py after 2026-07-03 review)
ZONE_CONFIG = {
    "OB": {"r_target": 3.0, "expiry_days": 0.5, "iv_threshold": 50.0},
    "MB": {"r_target": 3.0, "expiry_days": 0.5, "iv_threshold": 50.0},
    "BB": {"r_target": 2.5, "expiry_days": 5.0, "iv_threshold": 55.0},
}
ACTIVE_CELLS = {
    ("15m", "MB"), ("15m", "BB"),
    ("30m", "OB"),
    ("1h", "MB"),
    ("2h", "OB"), ("2h", "MB"),
}
WEIGHT_PCT = {"OB": 0.12, "MB": 0.18, "BB": 0.28}
MAX_OPEN_PER_ZONE = {"OB": 3, "MB": 2, "BB": 1}
MAX_OPEN_TOTAL_GLOBAL = 8
LOT_SIZE = 0.01  # real Bybit BTC options min lot (vs ETH's 0.10) — see btc_straddle_loop.py LOT_BTC
MARGIN_PCT = 0.15
FEE_RATE = 0.0003
FEE_CAP_PCT = 0.125
STARTING_BALANCE = 2000.0

TF_FILES = {
    "15m": f"{DATA_DIR}/btc_15m.csv",
    "30m": f"{DATA_DIR}/btc_30m.csv",
    "1h": f"{DATA_DIR}/btc_1h.csv",
    "2h": f"{DATA_DIR}/btc_2h.csv",
}
TF_BPD = {"15m": 96, "30m": 48, "1h": 24, "2h": 12}


def find_midpoint_entries(df, zones):
    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    out = []
    for zone in zones:
        found = _find_midpoint_entry(o, h, l, c, zone, n)
        if found is None:
            continue
        entry_idx, entry_price, stop_price = found
        out.append((zone, entry_idx, entry_price, stop_price))
    return out


def build_candidates_for_tf(tf: str, csv_path: str, cut1_ts: int, cut2_ts: int):
    """Returns dict split_name -> list[Candidate], using absolute ts_ms as
    entry_idx/exit_idx so candidates from different TFs merge on one timeline."""
    bpd = TF_BPD[tf]
    df = structure.load_csv(csv_path)
    n = len(df)
    ts = df["ts_ms"].values if "ts_ms" in df.columns else df["ts"].astype("int64").values // 10**6

    swings = structure.detect_swings(df, order=3)
    swings, events = structure.label_and_track(df, swings)
    fvgs = structure.detect_fvg(df)
    obs_ = ob.detect_ob(df, swings, fvgs)
    bbs_ = bb.detect_bb(df, obs_, events)
    mbs_ = mb.detect_mb(df, events, obs_)
    zones = [z for z in build_zones(obs_, bbs_, mbs_) if (tf, z.kind) in ACTIVE_CELLS]
    iv_series = load_dvol_aligned(DVOL_JSON, df)

    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    entries = find_midpoint_entries(df, zones)

    by_split = {"train": [], "validation": [], "holdout": []}
    for zone, entry_idx, entry_price, stop_price in entries:
        iv0 = iv_series[entry_idx]
        if np.isnan(iv0) or abs(entry_price - stop_price) <= 0:
            continue
        kind = zone.kind
        thr = ZONE_CONFIG[kind]["iv_threshold"]
        if iv0 * 100 <= thr:
            continue
        rt = ZONE_CONFIG[kind]["r_target"]
        exp_days = ZONE_CONFIG[kind]["expiry_days"]
        expiry_bars = int(exp_days * bpd)
        is_long = zone.direction == "bullish"
        risk = abs(entry_price - stop_price)
        tp_price = entry_price + rt * risk if is_long else entry_price - rt * risk
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
        sigma = iv0
        premium = bsp.price(side, entry_price, entry_price, T_entry, sigma)
        value_exit = bsp.price(side, spot_exit, entry_price, T_remaining, sigma)
        pnl_per_unit = premium - value_exit

        entry_ts = int(ts[entry_idx])
        exit_ts = int(ts[exit_idx])
        cand = Candidate(kind, zone.direction, entry_ts, exit_ts, entry_price, pnl_per_unit)

        if entry_ts < cut1_ts:
            by_split["train"].append(cand)
        elif entry_ts < cut2_ts:
            by_split["validation"].append(cand)
        else:
            by_split["holdout"].append(cand)

    return by_split


def main():
    # Master time cuts from 15m data (60/20/20), used to bucket ALL TFs consistently
    df15 = structure.load_csv(TF_FILES["15m"])
    n15 = len(df15)
    ts15 = df15["ts_ms"].values
    cut1_ts = int(ts15[int(n15 * 0.6)])
    cut2_ts = int(ts15[int(n15 * 0.8)])
    span_days = (int(ts15[-1]) - int(ts15[0])) / 1000 / 86400
    print(f"Full span: {span_days:.0f} days. train<{cut1_ts} <=val< {cut2_ts} <=holdout")
    print(f"train days~{span_days*0.6:.0f}, validation days~{span_days*0.2:.0f}, holdout days~{span_days*0.2:.0f}\n")

    combined = {"train": [], "validation": [], "holdout": []}
    for tf, path in TF_FILES.items():
        active_kinds = [k for (t, k) in ACTIVE_CELLS if t == tf]
        if not active_kinds:
            print(f"[{tf}] no active cells, skip")
            continue
        by_split = build_candidates_for_tf(tf, path, cut1_ts, cut2_ts)
        counts = {k: len(v) for k, v in by_split.items()}
        print(f"[{tf}] active_kinds={active_kinds} candidates: {counts}")
        for k in combined:
            combined[k].extend(by_split[k])

    cfg = PortfolioConfig(
        weight_pct=WEIGHT_PCT, max_open_per_zone=MAX_OPEN_PER_ZONE,
        max_open_total=MAX_OPEN_TOTAL_GLOBAL, starting_balance=STARTING_BALANCE,
        lot_size=LOT_SIZE, margin_pct=MARGIN_PCT, fee_rate=FEE_RATE, fee_cap_pct=FEE_CAP_PCT,
    )

    print(f"\n=== Portfolio sim per split (starting_balance=${STARTING_BALANCE}) ===")
    split_days = {"train": span_days * 0.6, "validation": span_days * 0.2, "holdout": span_days * 0.2}
    for split in ["train", "validation", "holdout"]:
        cands = sorted(combined[split], key=lambda c: c.entry_idx)
        final_balance, equity_curve, closed, n_blocked = simulate(cands, cfg)
        s = stats(cfg.starting_balance, final_balance, equity_curve, len(closed))
        days = split_days[split]
        trades_per_day = len(closed) / days if days else 0
        # annualize simple compounding return for comparability across splits of different length
        ann_factor = 365.0 / days if days else 0
        apr_approx = ((final_balance / cfg.starting_balance) ** ann_factor - 1) * 100 if final_balance > 0 else -100
        print(f"{split:12s} n_candidates={len(cands):5d} n_closed={s['n_closed']:5d} "
              f"blocked(lotsize)={n_blocked:5d} trades/day={trades_per_day:.2f} "
              f"return={s['total_return_pct']:+.1f}% approxAPR={apr_approx:+.1f}% "
              f"maxDD={s['max_dd_pct']:.1f}% calmar={s['calmar']:.2f} final=${s['final_balance']}")


if __name__ == "__main__":
    main()
