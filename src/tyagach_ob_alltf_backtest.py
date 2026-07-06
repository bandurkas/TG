"""Portfolio backtest of the 2026-07-07 config change: MB deactivated
(live bleed: 15 trades -$33.7 vs OB +$12.2 over the first 21 live closes),
OB enabled on ALL four TFs by explicit user decision, 15m/BB kept.

NEW cells:  {15m/OB, 30m/OB, 1h/OB, 2h/OB, 15m/BB}
OLD cells:  {15m/MB, 15m/BB, 30m/OB, 1h/MB, 2h/OB, 2h/MB}  (2026-07-03 config)

NOTE: 15m/OB and 1h/OB do NOT pass the all-3-splits admission criterion
(sweep_iv_lower_multitf.csv: 15m/OB negative train+validation at iv>=50;
1h/OB negative validation+holdout at iv>=50). They are included anyway as a
deliberate live-performance override — this script exists to document what
the historical data says about the chosen config, not to select cells.

Engine: simulate_tagged(scope="per_tf") from tyagach_samedir_ab.py — the
variant that mirrors the LIVE bot (per-TF sub-book same-direction blocking +
MAX_TOTAL_MARGIN_PCT), whose "global" arm exactly reproduced the 07-03
validation table.

Usage: python3 tyagach_ob_alltf_backtest.py
"""
from __future__ import annotations
import csv
import sys

sys.path.insert(0, "/Users/sabar/Desktop/smc_options/src")
import tyagach_portfolio_multitf as base
from tyagach_portfolio_multitf import TF_FILES, STARTING_BALANCE, structure
from tyagach_samedir_ab import simulate_tagged
from portfolio import stats

OLD_CELLS = {
    ("15m", "MB"), ("15m", "BB"),
    ("30m", "OB"),
    ("1h", "MB"),
    ("2h", "OB"), ("2h", "MB"),
}
NEW_CELLS = {
    ("15m", "OB"), ("30m", "OB"), ("1h", "OB"), ("2h", "OB"),
    ("15m", "BB"),
}
RESULTS_CSV = "/Users/sabar/Desktop/smc_options/results/tyagach_ob_alltf.csv"


def main():
    df15 = structure.load_csv(TF_FILES["15m"])
    n15 = len(df15)
    ts15 = df15["ts_ms"].values
    cut1_ts, cut2_ts = int(ts15[int(n15 * 0.6)]), int(ts15[int(n15 * 0.8)])
    span_days = (int(ts15[-1]) - int(ts15[0])) / 1000 / 86400
    split_days = {"train": span_days * 0.6, "validation": span_days * 0.2, "holdout": span_days * 0.2}

    # Build candidates ONCE with the union of both configs' cells, tagged with
    # TF, then filter per config — zone detection is the expensive part and
    # build_candidates_for_tf gates zones on the module-global ACTIVE_CELLS.
    base.ACTIVE_CELLS = OLD_CELLS | NEW_CELLS
    tagged = {"train": [], "validation": [], "holdout": []}
    for tf, path in TF_FILES.items():
        by_split = base.build_candidates_for_tf(tf, path, cut1_ts, cut2_ts)
        counts = {k: len(v) for k, v in by_split.items()}
        print(f"[{tf}] union-cells candidates: {counts}")
        for split in tagged:
            tagged[split].extend((tf, c) for c in by_split[split])

    rows = []
    print(f"\n{'config':<6} {'split':<12} {'n_closed':>8} {'trades/day':>10} {'return%':>10} "
          f"{'maxDD%':>7} {'calmar':>8} {'final$':>10}")
    print("-" * 80)
    for label, cells in [("old", OLD_CELLS), ("new", NEW_CELLS)]:
        for split in ["train", "validation", "holdout"]:
            subset = [(tf, c) for (tf, c) in tagged[split] if (tf, c.zone_kind) in cells]
            final, curve, closed = simulate_tagged(subset, "per_tf")
            s = stats(STARTING_BALANCE, final, curve, len(closed))
            tpd = len(closed) / split_days[split]
            print(f"{label:<6} {split:<12} {s['n_closed']:>8} {tpd:>10.2f} "
                  f"{s['total_return_pct']:>+10.1f} {s['max_dd_pct']:>7.1f} "
                  f"{s['calmar']:>8.2f} {s['final_balance']:>10.1f}")
            rows.append({"config": label, "split": split, "n_closed": s["n_closed"],
                         "trades_per_day": round(tpd, 2),
                         "return_pct": s["total_return_pct"], "max_dd_pct": s["max_dd_pct"],
                         "calmar": s["calmar"], "final_balance": s["final_balance"]})
        print()

    with open(RESULTS_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"saved -> {RESULTS_CSV}")


if __name__ == "__main__":
    main()
