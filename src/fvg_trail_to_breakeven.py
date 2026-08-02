from __future__ import annotations
"""Does trailing the stop to breakeven after an early partial-profit trigger
beat the single-leg R-multiple exit for 2h/FVG? fvg_partial_close.py found
plain partial-close (no stop change) strictly worse -- its worst-case trade
was IDENTICAL to the single-leg version because both legs shared the same
SL, so it did nothing about tail risk. This tests the actual fix: once price
touches a near trigger (rt_trigger R), move the stop to entry price
(breakeven) for the remainder of the position. Requires a genuine
bar-by-bar walk (the stop is now path-dependent) -- can't reuse the
two-independent-legs shortcut.
"""
import sys
import numpy as np

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
import structure
from dvol import load_dvol_aligned
import bs_pricer as bsp
import fvg_depth_sweep as fvgm

DEPTH = 0.675
EXPIRY_DAYS = 0.167
RT_FAR = 10.0
HAIRCUT = 0.85


def pnl_trail_to_be(df, entries, tf, rt_trigger, rt_far, exp_days):
    """Half position: at rt_trigger, move stop to breakeven (entry price),
    keep riding to rt_far. If never triggers, behaves exactly like the
    single-leg rt_far exit. If SL hits before trigger, full loss (like
    normal). If trigger hits, worst case after that is breakeven (0 P&L on
    the underlying leg, still pays fees/theta -- modeled via bs_pricer at
    exit exactly like any other exit)."""
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    bpd = fvgm.BPD[tf]
    pnls = []
    for e in entries:
        is_long = e.direction == "bullish"
        risk = abs(e.entry_price - e.stop_price)
        trigger_price = e.entry_price + rt_trigger * risk if is_long else e.entry_price - rt_trigger * risk
        tp_price = e.entry_price + rt_far * risk if is_long else e.entry_price - rt_far * risk
        expiry_bars = int(exp_days * bpd)
        expiry_idx = min(n - 1, e.entry_idx + expiry_bars)

        stop_price = e.stop_price
        triggered = False
        exit_idx = expiry_idx
        for j in range(e.entry_idx + 1, expiry_idx + 1):
            hit_sl = (l[j] <= stop_price) if is_long else (h[j] >= stop_price)
            hit_tp = (h[j] >= tp_price) if is_long else (l[j] <= tp_price)
            if hit_sl or hit_tp:
                exit_idx = j
                break
            if not triggered:
                hit_trigger = (h[j] >= trigger_price) if is_long else (l[j] <= trigger_price)
                if hit_trigger:
                    triggered = True
                    stop_price = e.entry_price  # move to breakeven
        else:
            exit_idx = expiry_idx

        spot_exit = c[exit_idx]
        side = "P" if is_long else "C"
        elapsed_days = (exit_idx - e.entry_idx) / bpd
        T_remaining = max(0.0, (exp_days - elapsed_days) / fvgm.DAYS_PER_YEAR)
        T_entry = exp_days / fvgm.DAYS_PER_YEAR
        strike = e.entry_price
        premium = bsp.price(side, e.entry_price, strike, T_entry, e.iv0) * HAIRCUT
        value_exit = bsp.price(side, spot_exit, strike, T_remaining, e.iv0)
        pnls.append(premium - value_exit)
    return np.array(pnls)


def single_leg_pnl(df, entries, tf, rt, exp_days):
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    bpd = fvgm.BPD[tf]
    pnls = []
    for e in entries:
        is_long = e.direction == "bullish"
        risk = abs(e.entry_price - e.stop_price)
        tp_price = e.entry_price + rt * risk if is_long else e.entry_price - rt * risk
        expiry_bars = int(exp_days * bpd)
        expiry_idx = min(n - 1, e.entry_idx + expiry_bars)
        exit_idx = expiry_idx
        for j in range(e.entry_idx + 1, expiry_idx + 1):
            hit_sl = (l[j] <= e.stop_price) if is_long else (h[j] >= e.stop_price)
            hit_tp = (h[j] >= tp_price) if is_long else (l[j] <= tp_price)
            if hit_sl or hit_tp:
                exit_idx = j
                break
        spot_exit = c[exit_idx]
        side = "P" if is_long else "C"
        elapsed_days = (exit_idx - e.entry_idx) / bpd
        T_remaining = max(0.0, (exp_days - elapsed_days) / fvgm.DAYS_PER_YEAR)
        T_entry = exp_days / fvgm.DAYS_PER_YEAR
        strike = e.entry_price
        premium = bsp.price(side, e.entry_price, strike, T_entry, e.iv0) * HAIRCUT
        value_exit = bsp.price(side, spot_exit, strike, T_remaining, e.iv0)
        pnls.append(premium - value_exit)
    return np.array(pnls)


def main():
    full = structure.load_csv(fvgm.TF_FILES["2h"])
    n = len(full)
    print(f"{'split':<10} {'rt_trigger':>10} {'single_mean':>12} {'trail_mean':>11} {'delta':>8} {'single_std':>11} {'trail_std':>10} {'single_min':>11} {'trail_min':>10}")
    for split_name, (f0, f1) in fvgm.SPLIT_FRACS.items():
        i0, i1 = int(n * f0), int(n * f1)
        sdf = full.iloc[i0:i1].reset_index(drop=True)
        zones = fvgm.detect_fvg_zones(sdf)
        iv_series = load_dvol_aligned(fvgm.DVOL_JSON, sdf)
        entries = fvgm.entries_for_depth(sdf, zones, iv_series, DEPTH, fvgm.MAX_LOOKAHEAD["2h"])

        single = single_leg_pnl(sdf, entries, "2h", RT_FAR, EXPIRY_DAYS)
        for rt_trigger in [2.0, 3.0, 4.0, 5.0]:
            trail = pnl_trail_to_be(sdf, entries, "2h", rt_trigger, RT_FAR, EXPIRY_DAYS)
            print(f"{split_name:<10} {rt_trigger:>10.1f} {single.mean():>12.4f} {trail.mean():>11.4f} "
                  f"{trail.mean()-single.mean():>+8.4f} {single.std():>11.4f} {trail.std():>10.4f} "
                  f"{single.min():>11.2f} {trail.min():>10.2f}")
        print()


if __name__ == "__main__":
    main()
