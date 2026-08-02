from __future__ import annotations
"""Capacity-aware re-check of the "all 4 TFs, rejection-close FVG" combo.

RESEARCH_FINDINGS_2026-08-02.md flagged the raw compounded numbers as
untrustworthy: weight_pct * balance sizing lets position notional grow
without bound as balance compounds, which the backtest's fixed HALF_SPREAD/
fee model doesn't penalize -- unrealistic once notional exceeds what the
real ETH options market can actually absorb per fill. No real depth data
exists to calibrate an exact ceiling, so this runs a SENSITIVITY sweep:
cap position budget at weight_pct * min(balance, CAP) for several CAP
values (effectively "pretend the strategy can never size bigger than this
much capital, no matter how much it's compounded to") and see how
quarter-robustness degrades as the cap tightens. If the story holds up even
at a conservative cap, the multi-TF combo is real; if it collapses, the
extreme numbers were compounding artifacts as suspected.

Custom simulate_tagged variant (capacity_cap param) copied from
tyagach_samedir_ab.py -- only change is the budget line.
"""
import sys

import numpy as np

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
import structure
from dvol import load_dvol_aligned
import bs_pricer as bsp
from portfolio import Candidate, PRIORITY as BASE_PRIORITY, stats
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
IV_THRESHOLD = 50.0
ZONE_FN = {"OB": detect_ob_zones, "FVG": detect_fvg_zones}

PRIORITY = {**BASE_PRIORITY, "FVG": 2}
WEIGHT_PCT = {"OB": 0.12, "MB": 0.18, "BB": 0.28, "FVG": 0.12}
MAX_OPEN_PER_ZONE = {"OB": 3, "MB": 2, "BB": 1, "FVG": 3}
MAX_OPEN_TOTAL_GLOBAL = 8
MAX_TOTAL_MARGIN_PCT = 0.60
LOT_SIZE = 0.10
MARGIN_PCT = 0.15
FEE_RATE = 0.0003
FEE_CAP_PCT = 0.125
STARTING_BALANCE = 2000.0


def simulate_capacity(tagged, capacity_cap: float | None):
    """Mirror of tyagach_samedir_ab.simulate_tagged(scope='per_tf'), with an
    optional ceiling on the balance used for position-budget sizing.
    capacity_cap=None reproduces the original uncapped compounding exactly."""
    order = sorted(tagged, key=lambda tc: (tc[1].entry_idx, PRIORITY[tc[1].zone_kind]))
    balance = STARTING_BALANCE
    open_positions = []
    equity_curve = [(0, balance)]
    closed = []

    def fee(notional, premium_total):
        return min(notional * FEE_RATE, abs(premium_total) * FEE_CAP_PCT)

    def close_due(up_to_idx):
        nonlocal balance
        still = []
        for p in sorted(open_positions, key=lambda p: p[3]):
            if p[3] <= up_to_idx:
                tf, kind, direction, exit_idx, num_units, ppu, notional = p
                gross = ppu * num_units
                premium_total = abs(ppu) * num_units
                net = gross - 2 * fee(notional, premium_total)
                balance += net
                closed.append(net)
                equity_curve.append((exit_idx, balance))
            else:
                still.append(p)
        open_positions[:] = still

    for tf, c in order:
        close_due(c.entry_idx)
        conflict = any(p[0] == tf and p[2] == c.direction for p in open_positions)
        if conflict:
            continue
        per_zone = sum(1 for p in open_positions if p[0] == tf and p[1] == c.zone_kind)
        if per_zone >= MAX_OPEN_PER_ZONE.get(c.zone_kind, 0):
            continue
        if len(open_positions) >= MAX_OPEN_TOTAL_GLOBAL:
            continue
        if balance <= 0:
            continue

        sizing_balance = min(balance, capacity_cap) if capacity_cap is not None else balance
        budget = WEIGHT_PCT.get(c.zone_kind, 0.0) * sizing_balance
        margin_per_lot = LOT_SIZE * c.spot_entry * MARGIN_PCT
        n_lots = int(budget // margin_per_lot) if margin_per_lot > 0 else 0
        if n_lots < 1:
            continue
        num_units = n_lots * LOT_SIZE
        notional = num_units * c.spot_entry
        margin_required = n_lots * margin_per_lot
        total_margin = sum(p[6] * MARGIN_PCT for p in open_positions)
        if total_margin + margin_required > balance * MAX_TOTAL_MARGIN_PCT:
            continue

        open_positions.append((tf, c.zone_kind, c.direction, c.exit_idx, num_units, c.pnl_per_unit, notional))

    if order:
        close_due(order[-1][1].exit_idx + 1)
    return balance, equity_curve, closed


def build_all(kind, tf, cfg):
    df = structure.load_csv(TF_FILES[tf])
    n = len(df); ts = df["ts_ms"].values
    zones = ZONE_FN[kind](df)
    iv_series = load_dvol_aligned(DVOL_JSON, df)
    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    depth_frac, rt, exp_days = cfg["depth_frac"], cfg["r_target"], cfg["expiry_days"]
    bpd = BPD[tf]
    out = []
    for zone in zones:
        found = find_rejection_entry(o, h, l, c, zone, n, depth_frac, MAX_LOOKAHEAD[tf])
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
        out.append((tf, int(ts[entry_idx]),
                    Candidate(kind, zone.direction, int(ts[entry_idx]), int(ts[exit_idx]), entry_price, premium - value_exit)))
    return out


CELLS = [
    ("FVG", "2h", dict(depth_frac=0.675, r_target=10.0, expiry_days=0.25)),
    ("FVG", "1h", dict(depth_frac=0.325, r_target=7.0, expiry_days=0.167)),
    ("FVG", "30m", dict(depth_frac=0.300, r_target=7.0, expiry_days=0.125)),
    ("FVG", "15m", dict(depth_frac=0.300, r_target=10.0, expiry_days=0.125)),
]


def main():
    all_cands = []
    for kind, tf, cfg in CELLS:
        all_cands.extend(build_all(kind, tf, cfg))
    print(f"total candidates across all 4 TFs: {len(all_cands)}")

    ts_start = min(t for _, t, _ in all_cands)
    ts_end = max(t for _, t, _ in all_cands)
    span = ts_end - ts_start
    N_Q = 8
    edges = [ts_start + int(span * i / N_Q) for i in range(N_Q + 1)]

    CAPS = [None, 100_000, 50_000, 20_000, 10_000, 5_000]
    for cap in CAPS:
        label = "uncapped" if cap is None else f"cap=${cap:,.0f}"
        full_final, full_curve, full_closed = simulate_capacity(
            [(tf, c) for tf, t, c in all_cands], cap)
        full_ret = (full_final - STARTING_BALANCE) / STARTING_BALANCE * 100
        rets = []
        for q in range(N_Q):
            qc = [(tf, c) for tf, t, c in all_cands if edges[q] <= t < edges[q + 1]]
            final, curve, closed = simulate_capacity(qc, cap)
            s = stats(STARTING_BALANCE, final, curve, len(closed))
            rets.append(s["total_return_pct"])
        neg = sum(1 for r in rets if r < 0)
        print(f"{label:<14} full_period_return={full_ret:>+12.1f}%  "
              f"quarters_negative={neg}/{N_Q}  mean_quarter={np.mean(rets):>+8.1f}%  "
              f"worst_quarter={min(rets):>+7.1f}%")


if __name__ == "__main__":
    main()
