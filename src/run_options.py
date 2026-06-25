from __future__ import annotations
import sys
import pandas as pd
import structure
import ob
import bb
import mb
from zones import build_zones
from realized_vol import trailing_rv_series
from options_backtest import run_options_backtest, summarize

R_TARGETS = [1.5, 2.0]
EXPIRIES_DAYS = [1.0, 3.0]


def run(tf_csv: str, swing_order: int = 3):
    df = structure.load_csv(tf_csv)
    swings = structure.detect_swings(df, order=swing_order)
    swings, events = structure.label_and_track(df, swings)
    fvgs = structure.detect_fvg(df)
    obs = ob.detect_ob(df, swings, fvgs)
    bbs = bb.detect_bb(df, obs, events)
    mbs = mb.detect_mb(df, events, obs)
    zones = build_zones(obs, bbs, mbs)

    sigma_series = trailing_rv_series(df)

    trades = run_options_backtest(df, zones, sigma_series, R_TARGETS, EXPIRIES_DAYS)
    summary = summarize(trades)
    print(f"[{tf_csv}] zones={len(zones)} option_trades={len(trades)}")
    return df, zones, trades, summary


if __name__ == "__main__":
    tf_csv = sys.argv[1] if len(sys.argv) > 1 else "../data/eth_15m.csv"
    pd.set_option("display.width", 160)
    pd.set_option("display.max_rows", 100)
    df, zones, trades, summary = run(tf_csv)
    print(summary.to_string(index=False))
    out_path = f"../results/options_summary_{tf_csv.split('/')[-1].replace('.csv','')}.csv"
    summary.to_csv(out_path, index=False)
    print(f"\nSaved -> {out_path}")
