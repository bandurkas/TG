from __future__ import annotations
"""Research: does a portfolio-level ("basket") trailing take-profit help?
Trigger: track the running peak of AGGREGATE unrealized PnL across every
currently-open position (reset to 0 whenever the basket is empty, "armed"
only once peak exceeds arm_frac * balance-at-arm-time); if unrealized PnL
gives back trail_frac of that peak, force-close every open position
immediately (same accounting as a normal exit, just early/simultaneous).

Existing sweep scripts (sl_buffer_*, ob_depth_sweep, etc.) only ever compute
a candidate's entry and its ONE final exit (SL/TP/expiry) -- they never
track the continuous mark-to-market path of an open position, so they can't
answer this question. This script adds that: a 15m-tick master clock marks
every open position (any TF) via BS reprice at each tick using ITS OWN
entry iv0/strike/side/expiry (same convention as every other backtest here
-- iv0 held constant from entry, only spot and time-to-expiry move), sums
to a basket unrealized PnL, and runs the trailing logic against it.

Self-check: with trail_frac=1.0 (never fires) this MUST reproduce
tyagach_samedir_ab.simulate_tagged's per_tf baseline numbers exactly --
run --selfcheck first before trusting any swept result.

Run: python3 basket_trailing_stop_research.py [--selfcheck]
"""
import sys
import time
import numpy as np

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
import structure
import ob as ob_mod
import bb as bb_mod
import mb as mb_mod
from zones import build_zones
from dvol import load_dvol_aligned
import bs_pricer as bsp
from ob_depth_sweep import detect_ob_zones, MAX_LOOKAHEAD, BPD, DAYS_PER_YEAR, IV_THRESHOLD as OB_IV_THRESHOLD
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
ATR_PERIOD = 14
MS_PER_YEAR = DAYS_PER_YEAR * 86_400_000
IV_THRESHOLD = {"OB": 50.0, "FVG": 50.0, "BB": 55.0, "MB": 50.0}

STARTING_BALANCE = 10_000.0
LOT_SIZE = 0.10
MARGIN_PCT = 0.15
FEE_RATE = 0.0003
FEE_CAP_PCT = 0.125
PRIORITY = {"BB": 0, "MB": 1, "OB": 2, "FVG": 2}
WEIGHT_PCT = {"OB": 0.12, "MB": 0.18, "BB": 0.28, "FVG": 0.12}
MAX_OPEN_PER_ZONE = {"OB": 3, "MB": 2, "BB": 1, "FVG": 3}
MAX_OPEN_TOTAL_GLOBAL = 8
MAX_TOTAL_MARGIN_PCT = 0.80

# Current LIVE cell configs + buffer choices (== tyagach/services/config.py
# as of 2026-08-03, after both SL-buffer retune rounds). Copied rather than
# imported -- these research scripts intentionally don't import the live
# service module (keeps research reproducible even if live config drifts).
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


def atr_series(h, l, c, period=ATR_PERIOD):
    import pandas as pd
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


def build_positions(tf, kind, cfg, df, n, ts, iv_series, o, h, l, c, atr):
    """Raw position records (NOT the lightweight Candidate tuple) -- keeps
    everything needed to mark-to-market at an arbitrary intermediate time:
    side/strike/iv0/expiry_ts/premium_entry, plus the natural (SL/TP/expiry)
    exit already computed the normal way."""
    zones = detect_zones(kind, df)
    depth_frac, rt, exp_days = cfg["depth_frac"], cfg["r_target"], cfg["expiry_days"]
    bpd = BPD[tf]
    max_lookahead = MAX_LOOKAHEAD[tf]
    iv_thresh = IV_THRESHOLD[kind]

    out = []
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
        tp_price = entry_price + rt * risk if is_long else entry_price - rt * risk
        expiry_bars = int(exp_days * bpd)
        expiry_idx = min(n - 1, entry_idx + expiry_bars)
        exit_idx = expiry_idx
        exit_reason = "expiry"
        for j in range(entry_idx + 1, expiry_idx + 1):
            hit_sl = (l[j] <= stop_price) if is_long else (h[j] >= stop_price)
            hit_tp = (h[j] >= tp_price) if is_long else (l[j] <= tp_price)
            if hit_sl or hit_tp:
                exit_idx = j
                exit_reason = "sl" if hit_sl else "tp"
                break
        spot_exit = c[exit_idx]
        side = "P" if is_long else "C"
        elapsed_days = (exit_idx - entry_idx) / bpd
        T_remaining = max(0.0, (exp_days - elapsed_days) / DAYS_PER_YEAR)
        T_entry = exp_days / DAYS_PER_YEAR
        strike = entry_price
        premium_entry = bsp.price(side, entry_price, strike, T_entry, iv0) * HAIRCUT
        value_exit = bsp.price(side, spot_exit, strike, T_remaining, iv0)
        natural_pnl_per_unit = premium_entry - value_exit
        out.append(dict(
            tf=tf, kind=kind, direction=zone.direction, side=side, strike=strike, iv0=iv0,
            entry_ts=int(ts[entry_idx]), expiry_ts=int(ts[entry_idx]) + int(exp_days * 86_400_000),
            spot_entry=entry_price, premium_entry=premium_entry,
            natural_exit_ts=int(ts[exit_idx]), natural_pnl_per_unit=natural_pnl_per_unit, natural_exit_reason=exit_reason,
        ))
    return out


def build_all_positions(tf_cache):
    all_positions = []
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
        all_positions.extend(build_positions(tf, kind, cfg, df, n, ts, iv_series, o, h, l, c, atr))
    return all_positions


def _fee(notional, premium_total):
    return min(notional * FEE_RATE, abs(premium_total) * FEE_CAP_PCT)


def run_sim(all_positions, master_ts, master_spot, arm_frac, trail_frac, ts_lo=None, ts_hi=None):
    """Event-driven per_tf engine (same conflict/cap/margin rules as
    tyagach_samedir_ab.simulate_tagged's 'per_tf' scope) + a continuous
    15m-tick overlay that marks open positions and can force-close the
    whole basket early. trail_frac=1.0 (or arm_frac=inf) never fires --
    must reproduce the baseline exactly (see --selfcheck)."""
    positions = [p for p in all_positions if (ts_lo is None or p["entry_ts"] >= ts_lo)
                 and (ts_hi is None or p["entry_ts"] < ts_hi)]
    positions.sort(key=lambda p: (p["entry_ts"], PRIORITY[p["kind"]]))

    balance = STARTING_BALANCE
    open_positions = []  # each: dict(pos fields..., num_units, notional, exit_ts=natural_exit_ts (mutable))
    equity_curve = [(master_ts[0], balance)]
    closed = []  # (tf, kind, entry_ts, exit_ts, net_pnl, reason)

    def close_one(p, exit_ts, pnl_per_unit, reason):
        nonlocal balance
        gross = pnl_per_unit * p["num_units"]
        premium_total = abs(pnl_per_unit) * p["num_units"]
        net = gross - 2 * _fee(p["notional"], premium_total)
        balance += net
        closed.append((p["tf"], p["kind"], p["entry_ts"], exit_ts, net, reason))
        equity_curve.append((exit_ts, balance))

    n_master = len(master_ts)
    mi = 0  # master tick pointer
    pi = 0  # pending-entry pointer
    peak = 0.0
    peak_balance_ref = balance  # balance at moment peak was last reset (arm_frac is relative to this)

    def try_enter(p, now_ts):
        nonlocal balance
        conflict = any(op["tf"] == p["tf"] and op["direction"] == p["direction"] for op in open_positions)
        if conflict:
            return
        per_zone = sum(1 for op in open_positions if op["tf"] == p["tf"] and op["kind"] == p["kind"])
        if per_zone >= MAX_OPEN_PER_ZONE.get(p["kind"], 0):
            return
        if len(open_positions) >= MAX_OPEN_TOTAL_GLOBAL or balance <= 0:
            return
        budget = WEIGHT_PCT.get(p["kind"], 0.0) * balance
        margin_per_lot = LOT_SIZE * p["spot_entry"] * MARGIN_PCT
        n_lots = int(budget // margin_per_lot) if margin_per_lot > 0 else 0
        if n_lots < 1:
            return
        num_units = n_lots * LOT_SIZE
        notional = num_units * p["spot_entry"]
        margin_required = n_lots * margin_per_lot
        total_margin = sum(op["margin"] for op in open_positions)
        if total_margin + margin_required > balance * MAX_TOTAL_MARGIN_PCT:
            return
        entry = dict(p)
        entry["num_units"] = num_units
        entry["notional"] = notional
        entry["margin"] = margin_required
        entry["exit_ts"] = p["natural_exit_ts"]
        open_positions.append(entry)

    while pi < len(positions) or open_positions or mi < n_master:
        # next candidate event times
        next_entry_ts = positions[pi]["entry_ts"] if pi < len(positions) else None
        next_exit_ts = min((op["exit_ts"] for op in open_positions), default=None)
        next_tick_ts = master_ts[mi] if mi < n_master else None
        candidates = [t for t in (next_entry_ts, next_exit_ts, next_tick_ts) if t is not None]
        if not candidates:
            break
        now_ts = min(candidates)

        # advance master tick pointer/spot to `now_ts` (tick-marking happens at tick events)
        while mi < n_master and master_ts[mi] < now_ts:
            mi += 1

        # 1) natural exits due at now_ts
        still_open = []
        for op in open_positions:
            if op["exit_ts"] <= now_ts:
                close_one(op, op["exit_ts"], op["natural_pnl_per_unit"], op["natural_exit_reason"])
            else:
                still_open.append(op)
        open_positions[:] = still_open

        # 2) entries due at now_ts (priority order already sorted)
        while pi < len(positions) and positions[pi]["entry_ts"] <= now_ts:
            try_enter(positions[pi], now_ts)
            pi += 1

        # 3) mark + trailing check, only meaningful on an actual master tick
        if mi < n_master and master_ts[mi] == now_ts and open_positions:
            spot = master_spot[mi]
            basket_unrealized = 0.0
            for op in open_positions:
                T_rem = max(0.0, (op["expiry_ts"] - now_ts) / MS_PER_YEAR)
                mark = bsp.price(op["side"], spot, op["strike"], T_rem, op["iv0"])
                basket_unrealized += (op["premium_entry"] - mark) * op["num_units"]
            if basket_unrealized > peak:
                peak = basket_unrealized
            armed = peak >= arm_frac * peak_balance_ref
            if armed and peak > 0 and basket_unrealized <= peak * (1 - trail_frac):
                for op in list(open_positions):
                    T_rem = max(0.0, (op["expiry_ts"] - now_ts) / MS_PER_YEAR)
                    mark = bsp.price(op["side"], spot, op["strike"], T_rem, op["iv0"])
                    close_one(op, now_ts, op["premium_entry"] - mark, "basket_trail")
                open_positions.clear()
                peak = 0.0
                peak_balance_ref = balance

        if not open_positions:
            peak = 0.0
            peak_balance_ref = balance

        if mi < n_master and master_ts[mi] == now_ts:
            mi += 1

    return balance, equity_curve, closed


def stats(equity_curve, n_trades):
    if len(equity_curve) < 2:
        return dict(total_return_pct=0.0, max_dd_pct=0.0, calmar=0.0, n_closed=0)
    peak = STARTING_BALANCE
    max_dd = 0.0
    for _, bal in equity_curve:
        peak = max(peak, bal)
        dd = (peak - bal) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
    final = equity_curve[-1][1]
    total_return_pct = (final - STARTING_BALANCE) / STARTING_BALANCE * 100
    calmar = (total_return_pct / 100) / max_dd if max_dd > 1e-9 else float("inf")
    return dict(total_return_pct=round(total_return_pct, 2), max_dd_pct=round(max_dd * 100, 2),
                calmar=round(calmar, 3), n_closed=n_trades, final=round(final, 1))


def selfcheck(all_positions, master_ts, master_spot):
    """trail_frac=1.0 -> basket_unrealized <= peak*(1-1.0) == peak*0 requires
    basket_unrealized<=0 while armed at a huge threshold -> effectively never
    fires if arm_frac is huge. Use arm_frac=1e9 to guarantee it never arms."""
    bal, curve, closed = run_sim(all_positions, master_ts, master_spot, arm_frac=1e9, trail_frac=0.5)
    s = stats(curve, len(closed))
    print(f"[selfcheck] never-armed run: n_closed={s['n_closed']} return={s['total_return_pct']:+.1f}% "
          f"maxDD={s['max_dd_pct']:.1f}% calmar={s['calmar']:.2f}")
    print("[selfcheck] compare this n_closed/return by eye against "
          "tyagach_samedir_ab.py's per_tf baseline for the same live cell set "
          "(exact match not expected -- STARTING_BALANCE/date range here may differ -- "
          "this is a sanity magnitude check, not a byte-exact regression test).")


def main():
    t0 = time.time()
    tf_cache = {}
    print("Building positions for all 13 live cells...")
    all_positions = build_all_positions(tf_cache)
    print(f"total positions: {len(all_positions)} ({time.time()-t0:.1f}s)")

    df15, n15, ts15 = tf_cache["15m"][0], tf_cache["15m"][1], tf_cache["15m"][2]
    master_ts = ts15
    master_spot = tf_cache["15m"][7]  # close series

    if "--selfcheck" in sys.argv:
        selfcheck(all_positions, master_ts, master_spot)
        return

    cut1_ts, cut2_ts = int(ts15[int(n15 * 0.6)]), int(ts15[int(n15 * 0.8)])
    splits = {"train": (None, cut1_ts), "validation": (cut1_ts, cut2_ts), "holdout": (cut2_ts, None)}

    print(f"\n{'arm%':>6} {'trail%':>7} {'split':<12} {'n_closed':>8} {'n_trail':>8} {'return%':>10} {'maxDD%':>7} {'calmar':>8}")
    for lo_hi_label, (lo, hi) in [("BASELINE(no-trail) train", (None, cut1_ts)),
                                    ("BASELINE(no-trail) val", (cut1_ts, cut2_ts)),
                                    ("BASELINE(no-trail) holdout", (cut2_ts, None))]:
        bal, curve, closed = run_sim(all_positions, master_ts, master_spot, arm_frac=1e9, trail_frac=0.5, ts_lo=lo, ts_hi=hi)
        s = stats(curve, len(closed))
        print(f"{'-':>6} {'-':>7} {lo_hi_label:<12} {s['n_closed']:>8} {0:>8} {s['total_return_pct']:>+10.1f} {s['max_dd_pct']:>7.1f} {s['calmar']:>8.2f}")

    for arm_pct in (0.005, 0.01, 0.02):
        for trail_pct in (0.3, 0.5, 0.7):
            for split, (lo, hi) in splits.items():
                bal, curve, closed = run_sim(all_positions, master_ts, master_spot,
                                              arm_frac=arm_pct, trail_frac=trail_pct, ts_lo=lo, ts_hi=hi)
                s = stats(curve, len(closed))
                n_trail = sum(1 for c in closed if c[5] == "basket_trail")
                print(f"{arm_pct*100:>6.1f} {trail_pct*100:>7.0f} {split:<12} {s['n_closed']:>8} {n_trail:>8} "
                      f"{s['total_return_pct']:>+10.1f} {s['max_dd_pct']:>7.1f} {s['calmar']:>8.2f}")
    print(f"\ndone in {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
