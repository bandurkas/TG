from __future__ import annotations
"""Does partial profit-taking beat the single-leg R-multiple exit for 2h/FVG
(current best candidate: depth=0.675, r_target=10.0, expiry=0.167d/4h)?

Models a partial close as two half-size legs sharing one entry/stop: leg A
(half the position) targets a NEARER r_target and locks in profit early,
leg B (the other half) rides to the original far r_target=10.0. Both legs
share the same stop_price (SL) and expiry — isolates ONLY the "does locking
in part of the position early help" question, not stop-trailing (a separate,
not-yet-tested idea).

No engine rewrite needed: two independent 0.5-unit single-leg simulations of
the same entries, combined PnL = 0.5*pnl_A + 0.5*pnl_B.
"""
import sys
import numpy as np

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
import structure
from dvol import load_dvol_aligned
import bs_pricer as bsp
import fvg_depth_sweep as fvgm

DATA_DIR = "/Users/styserg/Desktop/Tyagach/data"
DEPTH = 0.675
EXPIRY_DAYS = 0.167
RT_FAR = 10.0
HAIRCUT = 0.85


def leg_pnl(df, entries, tf, rt, exp_days):
    """Same math as fvg_depth_sweep.sweep_rt_expiry's inner loop, but returns
    per-entry pnl array (not aggregated) so two legs can be combined."""
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
    print(f"{'split':<10} {'rt_near':>8} {'single(rt=10)':>14} {'partial(near+10)/2':>19} {'delta':>8}")
    for split_name, (f0, f1) in fvgm.SPLIT_FRACS.items():
        i0, i1 = int(n * f0), int(n * f1)
        sdf = full.iloc[i0:i1].reset_index(drop=True)
        zones = fvgm.detect_fvg_zones(sdf)
        iv_series = load_dvol_aligned(fvgm.DVOL_JSON, sdf)
        entries = fvgm.entries_for_depth(sdf, zones, iv_series, DEPTH, fvgm.MAX_LOOKAHEAD["2h"])

        pnl_far = leg_pnl(sdf, entries, "2h", RT_FAR, EXPIRY_DAYS)
        single_avg = pnl_far.mean()

        for rt_near in [2.0, 3.0, 4.0, 5.0]:
            pnl_near = leg_pnl(sdf, entries, "2h", rt_near, EXPIRY_DAYS)
            combined = 0.5 * pnl_near + 0.5 * pnl_far
            partial_avg = combined.mean()
            print(f"{split_name:<10} {rt_near:>8.1f} {single_avg:>14.4f} {partial_avg:>19.4f} {partial_avg-single_avg:>+8.4f}")
        print()


if __name__ == "__main__":
    main()
