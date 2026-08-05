from __future__ import annotations
"""MB reversal: does selling the OPPOSITE option side at MB zones (CALL where
we currently sell PUT, and vice versa) beat MB's current live exit behavior?
Prompted by MB being almost the entire bot's net loss (-$64.81 of -$85.41,
see SESSION_HANDOFF_2026-08-05_MB_REVERSAL_IDEA.md) and SL-rate consistently
running >50% on several MB timeframes -- if that's a stable pattern (not
this era's noise), the mirror bet on the SAME signal must have a positive
edge on the same data, arithmetically.

This is NOT "take the closed trades and flip the PnL sign" -- it's a new
trade from the SAME entry (same touch+rejection signal, same entry_idx/
entry_price), with stop/tp MIRRORED around entry_price and the option side
flipped. Mirroring a price level L around entry e is L' = 2*e - L: this is
an involution (mirror(mirror(L)) == L), which is what makes "reverse twice
== original" true by construction, not just by observation -- verified by
selfcheck() below at both the primitive level (mirror_trade) and the full
pipeline level (reverse=False must equal tp_retarget_sweep.build_candidates
byte-for-byte).

Known limitation, same as every other sweep here: bs_pricer has no vol skew
-- both C and P at a cell are priced off the SAME iv0, so the "PUT/CALL
premium isn't identical" risk flagged in the handoff (skew) is NOT modeled.
Fee drag IS modeled (same _fee() convention as tp_retarget_sweep) since
that's a real, already-supported cost this sweep can and should account for.

Reports the live baseline (trailing where deployed, else plain r_target --
same convention as mb_quick_take_sweep.py) next to reversed-at-same-r_target
and a reversed r_target sweep (the flipped payoff shape -- frequent small TP
/ rare large SL instead of frequent small SL / rare large TP -- has no reason
to share the original's optimal r_target, so it needs its own grid, not just
a single point).

Run: python3 mb_reversal_sweep.py [--selfcheck]
"""
import sys
import numpy as np

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
import bs_pricer as bsp
from portfolio import Candidate, stats
from tyagach_samedir_ab import simulate_tagged
from tyagach_portfolio_multitf import STARTING_BALANCE
from ob_depth_sweep import MAX_LOOKAHEAD, BPD, DAYS_PER_YEAR
from tp_retarget_sweep import (
    find_entry, detect_zones, atr_series, load_tf, run_splits, run_quarters,
    build_candidates as build_baseline, _fee, HAIRCUT, IV_THRESHOLD, N_QUARTERS,
)
from mb_quick_take_sweep import MB_CELLS, eval_live_trailing, eval_plain

RT_MULT_GRID = (0.3, 0.5, 0.75, 1.0, 1.5, 2.0)


def mirror_trade(entry_price, stop_price, tp_price, is_long):
    """Reflect a trade's stop/tp around its entry_price and flip its
    direction. Pure involution: mirror_trade(*mirror_trade(...)) == original
    -- see selfcheck()."""
    return 2 * entry_price - stop_price, 2 * entry_price - tp_price, not is_long


def build_candidates_reversed(tf, kind, cfg, df, n, ts, iv_series, o, h, l, c, atr, r_target, reverse=True):
    zones = detect_zones(kind, df)
    depth_frac, exp_days = cfg["depth_frac"], cfg["expiry_days"]
    bpd = BPD[tf]
    max_lookahead = MAX_LOOKAHEAD[tf]
    iv_thresh = IV_THRESHOLD[kind]

    out = []
    fee_total = 0.0
    gross_premium_total = 0.0
    n_tp = n_sl = n_expiry = 0
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
        tp_price = entry_price + r_target * risk if is_long else entry_price - r_target * risk

        if reverse:
            stop_price, tp_price, is_long = mirror_trade(entry_price, stop_price, tp_price, is_long)

        expiry_bars = int(exp_days * bpd)
        expiry_idx = min(n - 1, entry_idx + expiry_bars)
        exit_idx = expiry_idx
        reason = "expiry"
        for j in range(entry_idx + 1, expiry_idx + 1):
            hit_sl = (l[j] <= stop_price) if is_long else (h[j] >= stop_price)
            hit_tp = (h[j] >= tp_price) if is_long else (l[j] <= tp_price)
            if hit_sl or hit_tp:
                exit_idx = j
                reason = "sl" if hit_sl else "tp"
                break
        if reason == "tp":
            n_tp += 1
        elif reason == "sl":
            n_sl += 1
        else:
            n_expiry += 1
        spot_exit = c[exit_idx]
        side = "P" if is_long else "C"
        elapsed_days = (exit_idx - entry_idx) / bpd
        T_remaining = max(0.0, (exp_days - elapsed_days) / DAYS_PER_YEAR)
        T_entry = exp_days / DAYS_PER_YEAR
        strike = entry_price
        premium = bsp.price(side, entry_price, strike, T_entry, iv0) * HAIRCUT
        value_exit = bsp.price(side, spot_exit, strike, T_remaining, iv0)
        pnl_per_unit_gross = premium - value_exit
        gross_premium_total += abs(premium)
        fee_total += 2 * _fee(entry_price, abs(pnl_per_unit_gross))
        entry_ts, exit_ts = int(ts[entry_idx]), int(ts[exit_idx])
        out.append((tf, Candidate(kind, zone.direction, entry_ts, exit_ts, entry_price, pnl_per_unit_gross)))
    n_total = n_tp + n_sl + n_expiry
    win_rate = n_tp / n_total if n_total else 0.0
    fee_drag = fee_total / gross_premium_total if gross_premium_total else 0.0
    return out, dict(n_tp=n_tp, n_sl=n_sl, n_expiry=n_expiry, win_rate=win_rate, fee_drag=fee_drag)


def eval_reversed(tf, kind, cfg, r_target, reverse=True):
    df, n, ts, iv_series, o, h, l, c, atr = load_tf(tf)
    candidates, meta = build_candidates_reversed(tf, kind, cfg, df, n, ts, iv_series, o, h, l, c, atr, r_target, reverse=reverse)
    splits = run_splits(candidates, ts)
    rets, dds = run_quarters(candidates, ts)
    n_negative = sum(1 for r in rets if r < 0)
    return dict(splits=splits, meta=meta, n_negative=n_negative, worst_q=min(rets), maxdd_q=max(dds))


def print_row(label, r):
    v, h = r["splits"]["validation"], r["splits"]["holdout"]
    m = r["meta"]
    fee_str = f"{m['fee_drag']*100:>5.2f}%" if "fee_drag" in m else "  n/a"
    print(f"{label:<16} win={m['win_rate']*100:>5.1f}% fee_drag={fee_str} "
          f"val_ret={v['total_return_pct']:>+8.1f} val_calmar={v['calmar']:>7.2f} "
          f"hold_ret={h['total_return_pct']:>+8.1f} hold_calmar={h['calmar']:>7.2f} "
          f"q_neg={r['n_negative']}/{N_QUARTERS} q_worst={r['worst_q']:>+6.1f} q_maxdd={r['maxdd_q']:>5.1f}")


def selfcheck():
    print("[selfcheck 1/2] mirror_trade is an involution (double-reverse == identity)...")
    ok = True
    samples = [
        (4000.0, 3900.0, 4400.0, True),
        (4000.0, 4100.0, 3600.0, False),
        (1.2345, 1.20, 1.30, True),
        (50000.0, 49000.0, 55000.0, False),
    ]
    for e, s, t, L in samples:
        s2, t2, L2 = mirror_trade(e, s, t, L)
        s3, t3, L3 = mirror_trade(e, s2, t2, L2)
        match = np.isclose(s3, s) and np.isclose(t3, t) and L3 == L
        if not match:
            ok = False
        print(f"  entry={e:<10} orig=(stop={s},tp={t},long={L}) -> once=(stop={s2:.4f},tp={t2:.4f},long={L2}) "
              f"-> twice=(stop={s3:.4f},tp={t3:.4f},long={L3}) match={match}")
    print(f"  {'PASS' if ok else 'FAIL'}\n")

    print("[selfcheck 2/2] reverse=False must reproduce tp_retarget_sweep.build_candidates byte-for-byte...")
    ok2 = True
    for (tf, kind), spec in MB_CELLS.items():
        cfg = spec["cfg"]
        df, n, ts, iv_series, o, h, l, c, atr = load_tf(tf)
        base_cands, base_meta = build_baseline(tf, kind, cfg, df, n, ts, iv_series, o, h, l, c, atr, cfg["r_target"])
        rev_cands, rev_meta = build_candidates_reversed(tf, kind, cfg, df, n, ts, iv_series, o, h, l, c, atr, cfg["r_target"], reverse=False)
        base_pnls = [round(cand.pnl_per_unit, 6) for _, cand in base_cands]
        rev_pnls = [round(cand.pnl_per_unit, 6) for _, cand in rev_cands]
        match = base_pnls == rev_pnls and base_meta["win_rate"] == rev_meta["win_rate"]
        if not match:
            ok2 = False
        print(f"  {tf}/{kind:<4} n_base={len(base_pnls):>4} n_rev={len(rev_pnls):>4} match={match}")
    print(f"  {'PASS' if ok2 else 'FAIL'}")
    return ok and ok2


def main():
    if "--selfcheck" in sys.argv:
        ok = selfcheck()
        sys.exit(0 if ok else 1)

    for (tf, kind), spec in MB_CELLS.items():
        cfg = spec["cfg"]
        trail_params = spec["trail_params"]
        base_rt = cfg["r_target"]
        print(f"\n=== {tf}/{kind}  (live r_target={base_rt}, live_trailing={'ON ' + str(trail_params) if trail_params else 'OFF'}) ===")
        if trail_params:
            live_baseline = eval_live_trailing(tf, kind, cfg, *trail_params)
            print_row(f"LIVE(trail{trail_params})", live_baseline)
        else:
            live_baseline = eval_plain(tf, kind, cfg)
            print_row("LIVE(plain r_target)", live_baseline)

        print_row(f"REVERSED(rt{base_rt}*)", eval_reversed(tf, kind, cfg, base_rt))
        for mult in RT_MULT_GRID:
            if mult == 1.0:
                continue  # already printed as the "*" row above
            rt = round(base_rt * mult, 2)
            print_row(f"REVERSED(rt{rt})", eval_reversed(tf, kind, cfg, rt))


if __name__ == "__main__":
    main()
