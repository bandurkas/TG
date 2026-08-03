from __future__ import annotations
"""Per-POSITION trailing profit-lock: does letting winners run while
protecting an already-banked profit beat the plain single-leg r_target exit?

Different question from basket_trailing_stop_research.py, which trailed
AGGREGATE unrealized PnL across the whole open basket and force-closed
everything at once -- found harmful (clips winners before full R). This
version is per-trade: only THIS position's own path matters, only THIS
position closes early, sized independently per cell like every other sweep
here. Also different from fvg_trail_to_breakeven.py (stop-to-breakeven on a
price trigger, found a no-op for 2h/FVG because its 4h/2-bar expiry leaves
no time for "trigger then reverse" to play out) -- this trails the actual
$ P&L (bar-close BS mark), not the stop price, and runs on ALL 12 active
cells so cells with longer expiries (more bars = more room for the dynamic
to matter) get a fair test too.

Mechanic per open position, bar-by-bar from entry to natural exit/expiry:
  1. intrabar hit_sl/hit_tp checked first (identical to every other sweep
     here) -- if hit, natural exit, unchanged.
  2. else mark unrealized $/unit via BS at this bar's CLOSE (same
     mark convention as basket_trailing_stop_research.py's tick engine).
     peak = running max of unrealized since entry. Once peak >= arm_frac *
     premium_entry ("armed" -- we've banked at least this fraction of the
     max we could ever collect), if unrealized retraces to
     <= peak * (1 - trail_frac), close now at this bar's mark ("trail").
  3. loop exhausts -> natural expiry, unchanged.

Self-check: arm_frac=1e9 (never arms) must reproduce the plain r_target
baseline byte-for-byte (n_trail=0, identical stats) -- run --selfcheck
before trusting any swept result.

Run: python3 trailing_profit_lock_sweep.py [--selfcheck]
"""
import sys
import numpy as np

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
import bs_pricer as bsp
from portfolio import Candidate, stats
from tyagach_samedir_ab import simulate_tagged
from tyagach_portfolio_multitf import STARTING_BALANCE
from ob_depth_sweep import MAX_LOOKAHEAD, BPD, DAYS_PER_YEAR
from tp_retarget_sweep import find_entry, detect_zones, atr_series, load_tf, run_splits, run_quarters, HAIRCUT, IV_THRESHOLD, N_QUARTERS

# Current LIVE config (== tyagach/services/config.py CELL_CONFIG + ACTIVE_CELLS,
# 2026-08-03 post TP-retune / BB-deactivation deploy `ec00ae5`). Copied, not
# imported -- same rationale as every other research script here.
LIVE_CELLS = {
    ("2h", "OB"):  dict(kind="OB",  depth_frac=0.675, r_target=3.75, expiry_days=0.25),
    ("2h", "FVG"): dict(kind="FVG", depth_frac=0.675, r_target=10.0, expiry_days=0.167),
    ("30m", "OB"): dict(kind="OB",  depth_frac=0.300, r_target=5.0,  expiry_days=0.125),
    ("1h", "OB"):  dict(kind="OB",  depth_frac=0.650, r_target=5.0,  expiry_days=0.125),
    ("30m", "FVG"): dict(kind="FVG", depth_frac=0.300, r_target=3.5, expiry_days=0.125),
    ("1h", "FVG"): dict(kind="FVG", depth_frac=0.325, r_target=7.0, expiry_days=0.167),
    ("15m", "FVG"): dict(kind="FVG", depth_frac=0.300, r_target=10.0, expiry_days=0.125),
    ("15m", "OB"): dict(kind="OB",  depth_frac=0.700, r_target=4.0, expiry_days=0.083),
    ("15m", "MB"): dict(kind="MB",  depth_frac=0.425, r_target=2.1, expiry_days=0.125),
    ("30m", "MB"): dict(kind="MB",  depth_frac=0.400, r_target=10.0, expiry_days=0.167),
    ("1h", "MB"):  dict(kind="MB",  depth_frac=0.400, r_target=10.0, expiry_days=0.25),
    ("2h", "MB"):  dict(kind="MB",  depth_frac=0.500, r_target=3.0, expiry_days=0.25),
}  # 15m/BB intentionally excluded -- deactivated (ACTIVE_CELLS), calmar -1.00.

ARM_GRID = (0.1, 0.2, 0.3, 0.5, 0.7, 1.0)
TRAIL_GRID = (0.1, 0.2, 0.3, 0.5, 0.7)


def _fee(notional, premium_total, rate, cap_pct):
    return min(notional * rate, abs(premium_total) * cap_pct)


def build_candidates_trail(tf, kind, cfg, df, n, ts, iv_series, o, h, l, c, atr, arm_frac, trail_frac):
    zones = detect_zones(kind, df)
    depth_frac, rt, exp_days = cfg["depth_frac"], cfg["r_target"], cfg["expiry_days"]
    bpd = BPD[tf]
    max_lookahead = MAX_LOOKAHEAD[tf]
    iv_thresh = IV_THRESHOLD[kind]

    out = []
    n_tp = n_sl = n_expiry = n_trail = 0
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
        side = "P" if is_long else "C"
        T_entry = exp_days / DAYS_PER_YEAR
        strike = entry_price
        premium_entry = bsp.price(side, entry_price, strike, T_entry, iv0) * HAIRCUT

        exit_idx, reason = expiry_idx, "expiry"
        peak = -1e18
        for j in range(entry_idx + 1, expiry_idx + 1):
            hit_sl = (l[j] <= stop_price) if is_long else (h[j] >= stop_price)
            hit_tp = (h[j] >= tp_price) if is_long else (l[j] <= tp_price)
            if hit_sl or hit_tp:
                exit_idx, reason = j, ("sl" if hit_sl else "tp")
                break
            elapsed_days = (j - entry_idx) / bpd
            T_rem = max(0.0, (exp_days - elapsed_days) / DAYS_PER_YEAR)
            mark = bsp.price(side, c[j], strike, T_rem, iv0)
            unrealized = premium_entry - mark
            if unrealized > peak:
                peak = unrealized
            armed = peak >= arm_frac * premium_entry
            if armed and peak > 0 and unrealized <= peak * (1 - trail_frac):
                exit_idx, reason = j, "trail"
                break
        else:
            exit_idx, reason = expiry_idx, "expiry"

        if reason == "tp":
            n_tp += 1
        elif reason == "sl":
            n_sl += 1
        elif reason == "trail":
            n_trail += 1
        else:
            n_expiry += 1

        spot_exit = c[exit_idx]
        elapsed_days = (exit_idx - entry_idx) / bpd
        T_remaining = max(0.0, (exp_days - elapsed_days) / DAYS_PER_YEAR)
        value_exit = bsp.price(side, spot_exit, strike, T_remaining, iv0)
        pnl_per_unit_gross = premium_entry - value_exit
        entry_ts, exit_ts = int(ts[entry_idx]), int(ts[exit_idx])
        out.append((tf, Candidate(kind, zone.direction, entry_ts, exit_ts, entry_price, pnl_per_unit_gross)))
    n_total = n_tp + n_sl + n_expiry + n_trail
    win_rate = (n_tp + n_trail) / n_total if n_total else 0.0
    return out, dict(n_tp=n_tp, n_sl=n_sl, n_expiry=n_expiry, n_trail=n_trail, win_rate=win_rate)


def eval_cell(tf, kind, cfg, arm_frac, trail_frac):
    df, n, ts, iv_series, o, h, l, c, atr = load_tf(tf)
    candidates, meta = build_candidates_trail(tf, kind, cfg, df, n, ts, iv_series, o, h, l, c, atr, arm_frac, trail_frac)
    splits = run_splits(candidates, ts)
    rets, dds = run_quarters(candidates, ts)
    n_negative = sum(1 for r in rets if r < 0)
    return dict(splits=splits, meta=meta, n_negative=n_negative, worst_q=min(rets), maxdd_q=max(dds))


def print_row(label, r):
    v, h = r["splits"]["validation"], r["splits"]["holdout"]
    m = r["meta"]
    print(f"{label:<14} win={m['win_rate']*100:>5.1f}% n_trail={m['n_trail']:>4} "
          f"val_ret={v['total_return_pct']:>+8.1f} val_calmar={v['calmar']:>7.2f} "
          f"hold_ret={h['total_return_pct']:>+8.1f} hold_calmar={h['calmar']:>7.2f} "
          f"q_neg={r['n_negative']}/{N_QUARTERS} q_worst={r['worst_q']:>+6.1f} q_maxdd={r['maxdd_q']:>5.1f}")


def selfcheck():
    print("[selfcheck] arm_frac=1e9 (never arms) must equal plain r_target baseline...\n")
    from tp_retarget_sweep import build_candidates as build_baseline
    ok = True
    for (tf, kind), cfg in LIVE_CELLS.items():
        df, n, ts, iv_series, o, h, l, c, atr = load_tf(tf)
        base_cands, base_meta = build_baseline(tf, kind, cfg, df, n, ts, iv_series, o, h, l, c, atr, cfg["r_target"])
        trail_cands, trail_meta = build_candidates_trail(tf, kind, cfg, df, n, ts, iv_series, o, h, l, c, atr, arm_frac=1e9, trail_frac=0.5)
        base_pnls = [round(cand.pnl_per_unit, 6) for _, cand in base_cands]
        trail_pnls = [round(cand.pnl_per_unit, 6) for _, cand in trail_cands]
        match = base_pnls == trail_pnls and trail_meta["n_trail"] == 0
        if not match:
            ok = False
        print(f"{tf}/{kind:<4} n_base={len(base_pnls):>4} n_trail_run={len(trail_pnls):>4} "
              f"n_trail_fired={trail_meta['n_trail']} match={match}")
    print(f"\n[selfcheck] {'PASS' if ok else 'FAIL'}")
    return ok


def main():
    if "--selfcheck" in sys.argv:
        ok = selfcheck()
        sys.exit(0 if ok else 1)

    for (tf, kind), cfg in LIVE_CELLS.items():
        print(f"\n=== {tf}/{kind}  (live r_target={cfg['r_target']}) ===")
        base = eval_cell(tf, kind, cfg, arm_frac=1e9, trail_frac=0.5)
        print_row("BASELINE", base)
        for arm in ARM_GRID:
            for trail in TRAIL_GRID:
                r = eval_cell(tf, kind, cfg, arm, trail)
                print_row(f"arm{arm} tr{trail}", r)


if __name__ == "__main__":
    main()
