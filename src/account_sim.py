from __future__ import annotations
"""Deposit sweep with realistic lot/margin/fee frictions, same convention as
~/Desktop/options/backend/services/btc_straddle_dollar_account_sim.py.
Reports $/month and APR for each deposit size, on the FULL 4yr history
(no train/holdout split here -- the strategy itself is already validated;
this is purely an account-sizing simulation)."""
import sys
from datetime import datetime, timezone
from collections import defaultdict
import pandas as pd
import structure
import ob
import bb
import mb
from zones import build_zones
from dvol import load_dvol_aligned
from sweep_sell import find_entries
from sweep_portfolio import build_candidates
from portfolio import PortfolioConfig, simulate, stats

DEPOSITS = (400.0, 800.0, 2000.0)
WEIGHTS = {"OB": 0.12, "MB": 0.18, "BB": 0.28}
CAPS = {"OB": 3, "MB": 2, "BB": 1}
GLOBAL_CAP = 5


def monthly_breakdown(closed_trades, ts_ms_arr):
    monthly = defaultdict(float)
    for zone_kind, entry_idx, exit_idx, pnl in closed_trades:
        ts = ts_ms_arr[exit_idx]
        mk = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m")
        monthly[mk] += pnl
    return dict(sorted(monthly.items()))


def main(tf_csv: str, dvol_json: str):
    df = structure.load_csv(tf_csv)
    swings = structure.detect_swings(df, order=3)
    swings, events = structure.label_and_track(df, swings)
    fvgs = structure.detect_fvg(df)
    obs = ob.detect_ob(df, swings, fvgs)
    bbs = bb.detect_bb(df, obs, events)
    mbs = mb.detect_mb(df, events, obs)
    zones = build_zones(obs, bbs, mbs)
    iv_series = load_dvol_aligned(dvol_json, df)
    entries = find_entries(df, zones, iv_series)
    candidates = build_candidates(df, entries)
    ts_ms_arr = df["ts_ms"].values
    days = len(df) * 15 / 1440
    months = days / 30.4368

    print(f"candles={len(df)} days={days:.0f} months={months:.1f} candidates={len(candidates)}\n")

    rows = []
    monthly_tables = {}
    for dep in DEPOSITS:
        cfg = PortfolioConfig(weight_pct=WEIGHTS, max_open_per_zone=CAPS, max_open_total=GLOBAL_CAP,
                               starting_balance=dep)
        final_balance, equity_curve, closed, n_blocked = simulate(candidates, cfg)
        s = stats(dep, final_balance, equity_curve, len(closed))
        avg_monthly_dollar = (final_balance - dep) / months
        apr = (((final_balance / dep) ** (1 / (months / 12)) - 1) * 100) if final_balance > 0 else -100.0
        simple_annualized = (s["total_return_pct"] / months) * 12
        rows.append({
            "deposit": dep, "final_balance": s["final_balance"], "total_return_pct": s["total_return_pct"],
            "max_dd_pct": s["max_dd_pct"], "n_trades": s["n_closed"], "n_blocked_lotsize": n_blocked,
            "avg_$_per_month": round(avg_monthly_dollar, 2),
            "APR_compounded_%": round(apr, 2), "APR_simple_%": round(simple_annualized, 2),
        })
        monthly_tables[dep] = monthly_breakdown(closed, ts_ms_arr)

    summary = pd.DataFrame(rows)
    print("=== Deposit sweep (4yr backtest, with lot/margin/fee frictions) ===")
    print(summary.to_string(index=False))
    summary.to_csv("../results/account_sim_deposit_sweep.csv", index=False)

    print("\n=== Monthly $ breakdown per deposit ===")
    all_months = sorted(set().union(*[set(t.keys()) for t in monthly_tables.values()]))
    monthly_df = pd.DataFrame({"month": all_months})
    for dep in DEPOSITS:
        monthly_df[f"${dep:.0f}"] = [round(monthly_tables[dep].get(m, 0.0), 2) for m in all_months]
    print(monthly_df.to_string(index=False))
    monthly_df.to_csv("../results/account_sim_monthly.csv", index=False)

    return summary, monthly_df


if __name__ == "__main__":
    tf_csv = sys.argv[1] if len(sys.argv) > 1 else "../data/eth_15m.csv"
    dvol_json = sys.argv[2] if len(sys.argv) > 2 else "../data/eth_dvol_1h.json"
    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 200)
    main(tf_csv, dvol_json)
