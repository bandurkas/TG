from __future__ import annotations
"""Quarter-robustness check (fresh capital each quarter, matching the Jony
fleet's own quarter_robustness.py methodology) for the single candidate that
survived portfolio-level haircut testing: 2h/OB ONLY, retuned to
r_target=5.0/expiry_days=0.25/depth_frac=0.675 (live is r_target=3.0/
expiry_days=1.00), haircut=0.85 on entry premium.

ob_portfolio_compare_haircut.py found this the only cell/config combo
positive on all of train/validation/holdout at portfolio level (15m/1h have
no robust combo at all; 30m passes per-trade but drags the combined
portfolio negative). Before recommending deactivating 3 of 4 live cells,
check the survivor isn't just a lucky 60/20/20 split -- split the full
history into independent quarters (fresh $2000 each) and require it to hold
up broadly, not just in aggregate.
"""
import sys
import numpy as np

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
import ob_portfolio_compare_haircut as m
from tyagach_samedir_ab import simulate_tagged
from portfolio import stats
from tyagach_portfolio_multitf import STARTING_BALANCE

CFG_2H_OPT = {"2h": dict(depth_frac=0.675, r_target=5.0, expiry_days=0.25)}
CFG_2H_LIVE = {"2h": dict(depth_frac=0.675, r_target=3.0, expiry_days=1.00)}

N_QUARTERS = 8  # ~4y history / 8 = ~6mo per quarter, matches Jony's convention


def build_all_candidates(cfg: dict):
    """Full-history candidates (no train/val/holdout split), tagged with tf."""
    df = m.structure.load_csv(m.TF_FILES["2h"])
    n = len(df)
    ts = df["ts_ms"].values
    zones = m.detect_ob_zones(df)
    iv_series = m.load_dvol_aligned(m.DVOL_JSON, df)
    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    tf_cfg = cfg["2h"]
    depth_frac, rt, exp_days = tf_cfg["depth_frac"], tf_cfg["r_target"], tf_cfg["expiry_days"]
    bpd = m.BPD["2h"]

    out = []
    for zone in zones:
        found = m.find_depth_entry(o, h, l, c, zone, n, depth_frac, m.MAX_LOOKAHEAD["2h"])
        if found is None:
            continue
        entry_idx, entry_price, stop_price = found
        if abs(entry_price - stop_price) <= 0:
            continue
        iv0 = iv_series[entry_idx]
        if np.isnan(iv0) or iv0 * 100 <= m.IV_THRESHOLD:
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
        T_remaining = max(0.0, (exp_days - elapsed_days) / m.DAYS_PER_YEAR)
        T_entry = exp_days / m.DAYS_PER_YEAR
        strike = entry_price
        premium = m.bsp.price(side, entry_price, strike, T_entry, iv0) * m.HAIRCUT
        value_exit = m.bsp.price(side, spot_exit, strike, T_remaining, iv0)
        pnl_per_unit = premium - value_exit
        entry_ts, exit_ts = int(ts[entry_idx]), int(ts[exit_idx])
        out.append(("2h", m.Candidate("OB", zone.direction, entry_ts, exit_ts, entry_price, pnl_per_unit)))
    return out, int(ts[0]), int(ts[-1])


def run_quarters(cfg: dict, label: str):
    candidates, ts_start, ts_end = build_all_candidates(cfg)
    span = ts_end - ts_start
    edges = [ts_start + int(span * i / N_QUARTERS) for i in range(N_QUARTERS + 1)]

    print(f"\n=== {label} ===")
    print(f"{'quarter':<10} {'n_closed':>8} {'return%':>10} {'maxDD%':>7} {'calmar':>8}")
    rets = []
    for q in range(N_QUARTERS):
        q_lo, q_hi = edges[q], edges[q + 1]
        q_cands = [(tf, c) for tf, c in candidates if q_lo <= c.entry_idx < q_hi]
        final, curve, closed = simulate_tagged(q_cands, "per_tf")
        s = stats(STARTING_BALANCE, final, curve, len(closed))
        rets.append(s["total_return_pct"])
        print(f"Q{q+1:<9} {s['n_closed']:>8} {s['total_return_pct']:>+10.1f} {s['max_dd_pct']:>7.1f} {s['calmar']:>8.2f}")
    n_negative = sum(1 for r in rets if r < 0)
    print(f"-- {n_negative}/{N_QUARTERS} quarters negative, mean return {np.mean(rets):+.1f}%, worst {min(rets):+.1f}%")


if __name__ == "__main__":
    run_quarters(CFG_2H_LIVE, "2h/OB LIVE (r_target=3.0, expiry=1.0) -- for comparison")
    run_quarters(CFG_2H_OPT, "2h/OB PROPOSED (r_target=5.0, expiry=0.25)")
