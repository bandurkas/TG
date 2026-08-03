from __future__ import annotations
"""MB-only flat-dollar quick-take: does closing an MB position the instant its
unrealized profit reaches a flat $X (regardless of premium size) beat MB's
current live exit behavior? Prompted by a real live pattern (MB net -$21.89/
-$13.30/-$11.40 on 15m/1h/2h since the 2026-08-02 reactivation) plus a direct
observation: position #37 (15m/FVG, not MB, but same shape) peaked at $9.95
unrealized and gave back to $5.67 before the existing trailing mechanism
caught it -- the user wants MB specifically to just take $3-4 the moment it's
there, no arm/peak logic, no waiting.

Different from trailing_profit_lock_sweep.py's arm_frac/trail_frac (scales
with premium_entry, needs a peak to have formed and then partially reversed)
-- this is a flat, one-shot check: unrealized >= FLAT_USD -> close now, no
"wait for a giveback" step. Simpler mechanic, needs its own sweep since the
existing trailing grid never tested a flat-$ trigger.

Correct baseline per cell (mirrors what's ACTUALLY live today, not a generic
r_target baseline):
  - 15m/MB, 2h/MB: baseline = the currently-deployed TRAIL_PARAMS trailing
    (arm/trail from tyagach/services/config.py), since that's what a flat-$
    quick-take would be REPLACING for those two cells.
  - 30m/MB, 1h/MB: baseline = plain r_target (no trailing deployed there --
    the per-cell trailing sweep found no consistent winning grid point).

Self-check: FLAT_USD=1e9 (never fires) must reproduce the plain r_target
baseline byte-for-byte (n_quick=0, identical stats) -- run --selfcheck first.

Run: python3 mb_quick_take_sweep.py [--selfcheck]
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
from trailing_profit_lock_sweep import build_candidates_trail

# Current LIVE MB config (== tyagach/services/config.py CELL_CONFIG, verified
# 2026-08-03) + the two cells' live TRAIL_PARAMS (None where no trailing is
# deployed today).
MB_CELLS = {
    ("15m", "MB"): dict(cfg=dict(kind="MB", depth_frac=0.425, r_target=2.1, expiry_days=0.125),
                        trail_params=(0.3, 0.5)),
    ("30m", "MB"): dict(cfg=dict(kind="MB", depth_frac=0.400, r_target=10.0, expiry_days=0.167),
                        trail_params=None),
    ("1h", "MB"):  dict(cfg=dict(kind="MB", depth_frac=0.400, r_target=10.0, expiry_days=0.25),
                        trail_params=None),
    ("2h", "MB"):  dict(cfg=dict(kind="MB", depth_frac=0.500, r_target=3.0, expiry_days=0.25),
                        trail_params=(0.5, 0.5)),
}

FLAT_GRID = (1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0, 8.0, 10.0)


def build_candidates_quick_take(tf, kind, cfg, df, n, ts, iv_series, o, h, l, c, atr, flat_usd):
    zones = detect_zones(kind, df)
    depth_frac, rt, exp_days = cfg["depth_frac"], cfg["r_target"], cfg["expiry_days"]
    bpd = BPD[tf]
    max_lookahead = MAX_LOOKAHEAD[tf]
    iv_thresh = IV_THRESHOLD[kind]

    out = []
    n_tp = n_sl = n_expiry = n_quick = 0
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
            if unrealized >= flat_usd:
                exit_idx, reason = j, "quick_take"
                break
        else:
            exit_idx, reason = expiry_idx, "expiry"

        if reason == "tp":
            n_tp += 1
        elif reason == "sl":
            n_sl += 1
        elif reason == "quick_take":
            n_quick += 1
        else:
            n_expiry += 1

        spot_exit = c[exit_idx]
        elapsed_days = (exit_idx - entry_idx) / bpd
        T_remaining = max(0.0, (exp_days - elapsed_days) / DAYS_PER_YEAR)
        value_exit = bsp.price(side, spot_exit, strike, T_remaining, iv0)
        pnl_per_unit_gross = premium_entry - value_exit
        entry_ts, exit_ts = int(ts[entry_idx]), int(ts[exit_idx])
        out.append((tf, Candidate(kind, zone.direction, entry_ts, exit_ts, entry_price, pnl_per_unit_gross)))
    n_total = n_tp + n_sl + n_expiry + n_quick
    win_rate = (n_tp + n_quick) / n_total if n_total else 0.0
    return out, dict(n_tp=n_tp, n_sl=n_sl, n_expiry=n_expiry, n_quick=n_quick, win_rate=win_rate)


def eval_quick_take(tf, kind, cfg, flat_usd):
    df, n, ts, iv_series, o, h, l, c, atr = load_tf(tf)
    candidates, meta = build_candidates_quick_take(tf, kind, cfg, df, n, ts, iv_series, o, h, l, c, atr, flat_usd)
    splits = run_splits(candidates, ts)
    rets, dds = run_quarters(candidates, ts)
    n_negative = sum(1 for r in rets if r < 0)
    return dict(splits=splits, meta=meta, n_negative=n_negative, worst_q=min(rets), maxdd_q=max(dds))


def eval_live_trailing(tf, kind, cfg, arm_frac, trail_frac):
    df, n, ts, iv_series, o, h, l, c, atr = load_tf(tf)
    candidates, meta = build_candidates_trail(tf, kind, cfg, df, n, ts, iv_series, o, h, l, c, atr, arm_frac, trail_frac)
    splits = run_splits(candidates, ts)
    rets, dds = run_quarters(candidates, ts)
    n_negative = sum(1 for r in rets if r < 0)
    meta = dict(meta)
    meta["n_quick"] = meta.pop("n_trail")  # unify column label across baseline/candidate rows
    return dict(splits=splits, meta=meta, n_negative=n_negative, worst_q=min(rets), maxdd_q=max(dds))


def eval_plain(tf, kind, cfg):
    return eval_quick_take(tf, kind, cfg, flat_usd=1e9)  # never fires -> plain r_target


def print_row(label, r):
    v, h = r["splits"]["validation"], r["splits"]["holdout"]
    m = r["meta"]
    print(f"{label:<16} win={m['win_rate']*100:>5.1f}% n_quick={m['n_quick']:>4} "
          f"val_ret={v['total_return_pct']:>+8.1f} val_calmar={v['calmar']:>7.2f} "
          f"hold_ret={h['total_return_pct']:>+8.1f} hold_calmar={h['calmar']:>7.2f} "
          f"q_neg={r['n_negative']}/{N_QUARTERS} q_worst={r['worst_q']:>+6.1f} q_maxdd={r['maxdd_q']:>5.1f}")


def selfcheck():
    print("[selfcheck] flat_usd=1e9 (never fires) must equal plain r_target baseline...\n")
    from tp_retarget_sweep import build_candidates as build_baseline
    ok = True
    for (tf, kind), spec in MB_CELLS.items():
        cfg = spec["cfg"]
        df, n, ts, iv_series, o, h, l, c, atr = load_tf(tf)
        base_cands, base_meta = build_baseline(tf, kind, cfg, df, n, ts, iv_series, o, h, l, c, atr, cfg["r_target"])
        quick_cands, quick_meta = build_candidates_quick_take(tf, kind, cfg, df, n, ts, iv_series, o, h, l, c, atr, flat_usd=1e9)
        base_pnls = [round(cand.pnl_per_unit, 6) for _, cand in base_cands]
        quick_pnls = [round(cand.pnl_per_unit, 6) for _, cand in quick_cands]
        match = base_pnls == quick_pnls and quick_meta["n_quick"] == 0
        if not match:
            ok = False
        print(f"{tf}/{kind:<4} n_base={len(base_pnls):>4} n_quick_run={len(quick_pnls):>4} "
              f"n_quick_fired={quick_meta['n_quick']} match={match}")
    print(f"\n[selfcheck] {'PASS' if ok else 'FAIL'}")
    return ok


def main():
    if "--selfcheck" in sys.argv:
        ok = selfcheck()
        sys.exit(0 if ok else 1)

    for (tf, kind), spec in MB_CELLS.items():
        cfg = spec["cfg"]
        trail_params = spec["trail_params"]
        print(f"\n=== {tf}/{kind}  (live r_target={cfg['r_target']}, "
              f"live_trailing={'ON ' + str(trail_params) if trail_params else 'OFF'}) ===")
        if trail_params:
            live_baseline = eval_live_trailing(tf, kind, cfg, *trail_params)
            print_row(f"LIVE(trail{trail_params})", live_baseline)
        else:
            live_baseline = eval_plain(tf, kind, cfg)
            print_row("LIVE(plain r_target)", live_baseline)
        for flat in FLAT_GRID:
            r = eval_quick_take(tf, kind, cfg, flat)
            print_row(f"flat${flat}", r)


if __name__ == "__main__":
    main()
