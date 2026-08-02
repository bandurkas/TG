from __future__ import annotations
"""What-if: if today's live config (ACTIVE_CELLS as of commit addce8a --
8 cells: 15m/BB, 2h/OB, 2h/FVG, 30m/OB, 1h/OB, 30m/FVG, 1h/FVG, 15m/FVG,
all with the generic rejection-close entry filter live since U1) had been
running since the start of the price history, with the same $2000 starting
balance -- one combined equity curve over the FULL span, no train/val/
holdout split (this is a "what if" replay, not a fresh validation).

Every (kind, tf) cell uses find_rejection_entry (fvg_rejection_entry.py) --
matches tyagach/services/signal_engine.py::scan_pending_zones, which applies
rejection-close generically to every kind, not just OB/FVG. BB has no
CELL_CONFIG override so it runs at ZONE_CONFIG["BB"]'s defaults
(depth=0.5, r_target=2.5, expiry=5.0, iv_threshold=55), same as live.

Caveat printed in the output too: this is a backtest replay against
historical price/IV data with realistic frictions (0.85 haircut, real fees,
real caps) -- not a guarantee live fills would match. Compare it to the
bot's ACTUAL realized numbers as context, not as a like-for-like number
(the live bot only ran ~1 month, mostly on older/broken configs -- see
SESSION_HANDOFF_2026-08-02.md).
"""
import sys
import numpy as np

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
import structure
import ob as ob_mod
import bb as bb_mod
from zones import build_zones as build_ob_bb_zones
from dvol import load_dvol_aligned
import bs_pricer as bsp
from portfolio import Candidate, stats
from tyagach_samedir_ab import simulate_tagged
from ob_depth_sweep import detect_ob_zones, MAX_LOOKAHEAD, BPD, DAYS_PER_YEAR, IV_THRESHOLD
from fvg_depth_sweep import detect_fvg_zones
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
STARTING_BALANCE = 2000.0
SWING_ORDER = 3
BB_IV_THRESHOLD = 55.0

# Live CELL_CONFIG as of commit addce8a (tyagach/services/config.py). BB has
# no override -> uses ZONE_CONFIG["BB"] defaults, matched here explicitly.
ACTIVE_CELLS = {
    ("15m", "BB"):  dict(depth_frac=0.500, r_target=2.5,  expiry_days=5.0,   iv_threshold=BB_IV_THRESHOLD),
    ("2h",  "OB"):  dict(depth_frac=0.675, r_target=5.0,  expiry_days=0.25,  iv_threshold=IV_THRESHOLD),
    ("2h",  "FVG"): dict(depth_frac=0.675, r_target=10.0, expiry_days=0.167, iv_threshold=IV_THRESHOLD),
    ("30m", "OB"):  dict(depth_frac=0.300, r_target=10.0, expiry_days=0.125, iv_threshold=IV_THRESHOLD),
    ("1h",  "OB"):  dict(depth_frac=0.650, r_target=5.0,  expiry_days=0.125, iv_threshold=IV_THRESHOLD),
    ("30m", "FVG"): dict(depth_frac=0.300, r_target=7.0,  expiry_days=0.125, iv_threshold=IV_THRESHOLD),
    ("1h",  "FVG"): dict(depth_frac=0.325, r_target=7.0,  expiry_days=0.167, iv_threshold=IV_THRESHOLD),
    ("15m", "FVG"): dict(depth_frac=0.300, r_target=10.0, expiry_days=0.125, iv_threshold=IV_THRESHOLD),
}

import portfolio as portfolio_mod
import tyagach_samedir_ab as samedir_mod
samedir_mod.PRIORITY = {**portfolio_mod.PRIORITY, "FVG": 2}
samedir_mod.WEIGHT_PCT = {**samedir_mod.WEIGHT_PCT, "FVG": samedir_mod.WEIGHT_PCT["OB"]}
samedir_mod.MAX_OPEN_PER_ZONE = {**samedir_mod.MAX_OPEN_PER_ZONE, "FVG": samedir_mod.MAX_OPEN_PER_ZONE["OB"]}


def detect_bb_zones_15m(df):
    swings = structure.detect_swings(df, order=SWING_ORDER)
    swings, events = structure.label_and_track(df, swings)
    fvgs = structure.detect_fvg(df)
    obs = ob_mod.detect_ob(df, swings, fvgs)
    bbs = bb_mod.detect_bb(df, obs, events)
    return build_ob_bb_zones([], bbs, [])


def detect_zones(kind, tf, df):
    if kind == "OB":
        return detect_ob_zones(df)
    if kind == "FVG":
        return detect_fvg_zones(df)
    if kind == "BB":
        return detect_bb_zones_15m(df)
    raise ValueError(kind)


def build_candidates(kind, tf, cfg, df, n, ts, iv_series, o, h, l, c):
    zones = detect_zones(kind, tf, df)
    depth_frac, rt, exp_days, iv_thresh = cfg["depth_frac"], cfg["r_target"], cfg["expiry_days"], cfg["iv_threshold"]
    bpd = BPD[tf]
    max_lookahead = MAX_LOOKAHEAD[tf]

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


def simulate_capacity(tagged, capacity_cap: float | None):
    """Same engine as tyagach_samedir_ab.simulate_tagged(scope='per_tf'), plus
    an optional ceiling on the balance used for position-budget sizing --
    budget = weight_pct * min(balance, capacity_cap) instead of weight_pct *
    balance. capacity_cap=None reproduces the uncapped run exactly. Ported
    from src/rejection_multitf_capacity.py (already used+reviewed this
    session for the same "raw compounded % isn't trustworthy" problem),
    extended here to cover all 8 live cells including BB, not just FVG."""
    order = sorted(tagged, key=lambda tc: (tc[1].entry_idx, samedir_mod.PRIORITY[tc[1].zone_kind]))
    balance = STARTING_BALANCE
    open_positions = []
    equity_curve = [(0, balance)]
    closed = []

    def fee(notional, premium_total):
        return min(notional * samedir_mod.FEE_RATE, abs(premium_total) * samedir_mod.FEE_CAP_PCT)

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
        if per_zone >= samedir_mod.MAX_OPEN_PER_ZONE.get(c.zone_kind, 0):
            continue
        if len(open_positions) >= samedir_mod.MAX_OPEN_TOTAL_GLOBAL:
            continue
        if balance <= 0:
            continue

        sizing_balance = min(balance, capacity_cap) if capacity_cap is not None else balance
        budget = samedir_mod.WEIGHT_PCT.get(c.zone_kind, 0.0) * sizing_balance
        margin_per_lot = samedir_mod.LOT_SIZE * c.spot_entry * samedir_mod.MARGIN_PCT
        n_lots = int(budget // margin_per_lot) if margin_per_lot > 0 else 0
        if n_lots < 1:
            continue
        num_units = n_lots * samedir_mod.LOT_SIZE
        notional = num_units * c.spot_entry
        margin_required = n_lots * margin_per_lot
        total_margin = sum(p[6] * samedir_mod.MARGIN_PCT for p in open_positions)
        if total_margin + margin_required > balance * samedir_mod.MAX_TOTAL_MARGIN_PCT:
            continue

        open_positions.append((tf, c.zone_kind, c.direction, c.exit_idx, num_units, c.pnl_per_unit, notional))

    if order:
        close_due(order[-1][1].exit_idx + 1)
    return balance, equity_curve, closed


def main():
    print(f"haircut={HAIRCUT}, starting_balance=${STARTING_BALANCE:.0f}\n")
    all_candidates = []
    per_cell_counts = {}
    tf_cache = {}
    for (tf, kind), cfg in ACTIVE_CELLS.items():
        if tf not in tf_cache:
            df = structure.load_csv(TF_FILES[tf])
            n = len(df)
            ts = df["ts_ms"].values
            iv_series = load_dvol_aligned(DVOL_JSON, df)
            o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
            tf_cache[tf] = (df, n, ts, iv_series, o, h, l, c)
        df, n, ts, iv_series, o, h, l, c = tf_cache[tf]
        cell_cands = build_candidates(kind, tf, cfg, df, n, ts, iv_series, o, h, l, c)
        per_cell_counts[f"{tf}/{kind}"] = len(cell_cands)
        all_candidates.extend(cell_cands)

    print(f"{'cell':<10} {'n_signals':>10}  (raw signal count per cell before slot/margin competition)")
    for label, n_sig in sorted(per_cell_counts.items()):
        print(f"{label:<10} {n_sig:>10}")
    print(f"{'TOTAL':<10} {len(all_candidates):>10}\n")

    span_days = max(
        (int(cache[2][-1]) - int(cache[2][0])) / 1000 / 86400
        for cache in tf_cache.values()
    )

    # simulate_tagged's `closed` is a plain list of net-$ pnl floats (no kind
    # tag) -- combined headline numbers only, no per-kind $ attribution
    # (would require re-running each kind separately against a DIFFERENT
    # slot-competition context, not a real attribution of this run).
    final, curve, closed = simulate_tagged(all_candidates, "per_tf")
    s = stats(STARTING_BALANCE, final, curve, len(closed))
    wins = sum(1 for pnl in closed if pnl > 0)
    win_rate = wins / len(closed) if closed else 0.0
    tpd = len(closed) / span_days

    print("=== Combined 8-cell simulation, full history, since inception (UNCAPPED compounding) ===")
    print(f"span_days={span_days:.0f}  n_closed={s['n_closed']}  trades/day={tpd:.2f}  win_rate={win_rate:.1%}")
    print(f"final_balance=${s['final_balance']:.2f}  total_return={s['total_return_pct']:+.1f}%  "
          f"max_dd={s['max_dd_pct']:.1f}%  calmar={s['calmar']:.2f}")
    print("^ NOT credible as-is -- same compounding artifact flagged earlier this session for the")
    print("  multi-TF FVG combo: unbounded position-budget sizing lets notional grow without limit as")
    print("  balance compounds, which no real ETH options market could actually absorb at ~17 trades/day.")

    print("\n=== Capacity-cap sensitivity (same run, budget = weight_pct * min(balance, CAP)) ===")
    print(f"{'cap':>10} {'n_closed':>9} {'trades/day':>10} {'win_rate':>9} {'final_$':>14} {'return%':>12} {'maxDD%':>7}")
    for cap in [None, 100_000, 50_000, 20_000, 10_000, 5_000, 2_000]:
        final_c, curve_c, closed_c = simulate_capacity(all_candidates, cap)
        s_c = stats(STARTING_BALANCE, final_c, curve_c, len(closed_c))
        wins_c = sum(1 for pnl in closed_c if pnl > 0)
        wr_c = wins_c / len(closed_c) if closed_c else 0.0
        tpd_c = len(closed_c) / span_days
        cap_label = "uncapped" if cap is None else f"${cap:,}"
        print(f"{cap_label:>10} {s_c['n_closed']:>9} {tpd_c:>10.2f} {wr_c:>9.1%} "
              f"{s_c['final_balance']:>14,.2f} {s_c['total_return_pct']:>+12.1f} {s_c['max_dd_pct']:>7.1f}")


if __name__ == "__main__":
    main()
