from __future__ import annotations
import sys
import pandas as pd
import structure
import ob
import bb
import mb
from zones import build_zones
from dvol import load_dvol_aligned
from options_backtest_real_iv import run, summarize

R_TARGETS = [1.5, 2.0]
EXPIRIES_DAYS = [1.0, 3.0]


def main(tf_csv: str, dvol_json: str, swing_order: int = 3):
    df = structure.load_csv(tf_csv)
    swings = structure.detect_swings(df, order=swing_order)
    swings, events = structure.label_and_track(df, swings)
    fvgs = structure.detect_fvg(df)
    obs = ob.detect_ob(df, swings, fvgs)
    bbs = bb.detect_bb(df, obs, events)
    mbs = mb.detect_mb(df, events, obs)
    zones = build_zones(obs, bbs, mbs)

    iv_series = load_dvol_aligned(dvol_json, df)
    n_valid = (~pd.isna(iv_series)).sum()
    print(f"[{tf_csv}] zones={len(zones)} candles_with_dvol={n_valid}/{len(df)}")

    trades = run(df, zones, iv_series, R_TARGETS, EXPIRIES_DAYS)
    summary = summarize(trades)
    return df, zones, trades, summary


if __name__ == "__main__":
    tf_csv = sys.argv[1] if len(sys.argv) > 1 else "../data/eth_15m.csv"
    dvol_json = sys.argv[2] if len(sys.argv) > 2 else "../data/eth_dvol_1h.json"
    pd.set_option("display.width", 160)
    pd.set_option("display.max_rows", 200)
    df, zones, trades, summary = main(tf_csv, dvol_json)
    print(summary.to_string(index=False))
    out_path = f"../results/real_iv_summary_{tf_csv.split('/')[-1].replace('.csv','')}.csv"
    summary.to_csv(out_path, index=False)
    print(f"\nSaved -> {out_path}")
