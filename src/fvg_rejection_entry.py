from __future__ import annotations
"""Does requiring a REJECTION CLOSE at the entry level (not just a wick
touch) improve 2h/FVG (depth=0.675, r_target=10.0, expiry=4h)?

Current find_depth_entry triggers the instant price's low/high touches
entry_level, regardless of where that bar CLOSES -- a wick-and-continue
(fakeout) counts the same as a genuine rejection. This variant only accepts
the touch if the SAME bar's close is back on the favorable side of
entry_level (bullish: close > entry_level; bearish: close < entry_level);
otherwise it keeps scanning forward (not invalidated, just not triggered
yet -- same semantics as any other non-touching bar).
"""
import sys
import numpy as np

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
import structure
from dvol import load_dvol_aligned
import bs_pricer as bsp
import fvg_depth_sweep as fvgm

DEPTH = 0.675
R_TARGET = 10.0
EXPIRY_DAYS = 0.167
HAIRCUT = 0.85


def find_rejection_entry(o, h, l, c, zone, n, depth_frac, max_lookahead):
    is_long = zone.direction == "bullish"
    zlo, zhi = zone.zone_low, zone.zone_high
    buf = fvgm.BUFFER_FRAC * ((zlo + zhi) / 2)
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
            # touched but no rejection close -- keep scanning, not invalidated
    return None


def entries_for_depth(df, zones, iv_series, depth_frac, max_lookahead, entry_fn):
    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    out = []
    for zone in zones:
        found = entry_fn(o, h, l, c, zone, n, depth_frac, max_lookahead)
        if found is None:
            continue
        entry_idx, entry_price, stop_price = found
        if abs(entry_price - stop_price) <= 0:
            continue
        iv0 = iv_series[entry_idx]
        if np.isnan(iv0) or iv0 * 100 <= fvgm.IV_THRESHOLD:
            continue
        out.append(fvgm.Entry(zone.direction, entry_idx, entry_price, stop_price, iv0))
    return out


def leg_pnl(df, entries, tf, rt, exp_days):
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
    print(f"{'split':<10} {'variant':<12} {'n':>6} {'win_rate':>9} {'avg_pnl':>9}")
    for split_name, (f0, f1) in fvgm.SPLIT_FRACS.items():
        i0, i1 = int(n * f0), int(n * f1)
        sdf = full.iloc[i0:i1].reset_index(drop=True)
        zones = fvgm.detect_fvg_zones(sdf)
        iv_series = load_dvol_aligned(fvgm.DVOL_JSON, sdf)

        touch_entries = entries_for_depth(sdf, zones, iv_series, DEPTH, fvgm.MAX_LOOKAHEAD["2h"], fvgm.find_depth_entry)
        rej_entries = entries_for_depth(sdf, zones, iv_series, DEPTH, fvgm.MAX_LOOKAHEAD["2h"], find_rejection_entry)

        touch_pnl = leg_pnl(sdf, touch_entries, "2h", R_TARGET, EXPIRY_DAYS)
        rej_pnl = leg_pnl(sdf, rej_entries, "2h", R_TARGET, EXPIRY_DAYS)

        print(f"{split_name:<10} {'touch(old)':<12} {len(touch_pnl):>6} {(touch_pnl>0).mean():>9.3f} {touch_pnl.mean():>9.4f}")
        print(f"{split_name:<10} {'rejection':<12} {len(rej_pnl):>6} {(rej_pnl>0).mean():>9.3f} {rej_pnl.mean():>9.4f}")
        print()


if __name__ == "__main__":
    main()
