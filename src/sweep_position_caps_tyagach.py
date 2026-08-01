from __future__ import annotations
"""Position/margin-cap headroom sweep for Tyagach (2026-08-02), directly
porting the Jony fleet's 2026-08-02 finding (research/sweep_position_caps.py
there): MAX_OPEN_POSITIONS/PER_COIN_CAP sat BELOW what the margin budget
alone would allow, and raising them to the margin-bound elbow was a clean
win on both the synthetic backtest and a live-signal replay.

Tyagach's structural analogue: MAX_OPEN_TOTAL_GLOBAL=8 (global slot cap) and
MAX_OPEN_PER_ZONE={"OB":3,"BB":1} (per-kind cap, the closest thing Tyagach
has to Jony's PER_COIN_CAP) sit on top of the INDEPENDENT margin budget
MAX_TOTAL_MARGIN_PCT=0.60. Question: same shape of headroom, or not?

Base: live_engine_2026-08-02.py's LIVE_BASELINE (OB tuned-per-cell + BB +
12-15h UTC entry veto) -- the faithful current-live-config candidate set,
fidelity-checked against ob_portfolio_compare.py's CANDIDATE row.

Uses the SAME candidate set for every cap combo (position caps don't change
which zones trigger, only which triggered candidates get a slot) -- so
candidates are built ONCE and only simulate_tagged() is rerun per combo.
"""
import sys

sys.path.insert(0, "/Users/sabar/Desktop/smc_options/src")
import structure
from portfolio import stats
from tyagach_portfolio_multitf import TF_FILES, STARTING_BALANCE
import tyagach_samedir_ab as ab
from live_engine_20260802 import build_all  # noqa

GLOBAL_CAP_GRID = [8, 10, 12, 14, 16, 20]           # live=8
OB_ZONE_CAP_GRID = [3, 4, 5, 6]                      # live=3
MARGIN_PCT_GRID = [0.60, 0.75, 0.80]                 # live=0.60


def main():
    df15 = structure.load_csv(TF_FILES["15m"])
    n15 = len(df15)
    ts15 = df15["ts_ms"].values
    cut1_ts, cut2_ts = int(ts15[int(n15 * 0.6)]), int(ts15[int(n15 * 0.8)])
    span_days = (int(ts15[-1]) - int(ts15[0])) / 1000 / 86400
    split_days = {"train": span_days * 0.6, "validation": span_days * 0.2, "holdout": span_days * 0.2}

    combined = build_all(cut1_ts, cut2_ts, apply_veto=True, include_bb=True)

    old_cap, old_zone_cap, old_margin = ab.MAX_OPEN_TOTAL_GLOBAL, dict(ab.MAX_OPEN_PER_ZONE), ab.MAX_TOTAL_MARGIN_PCT
    rows = []
    print(f"{'global_cap':>10} {'ob_cap':>7} {'margin_pct':>10} {'split':<12} {'n_closed':>8} "
          f"{'return%':>10} {'maxDD%':>7} {'calmar':>8} {'final$':>10}")
    print("-" * 92)
    try:
        for margin_pct in MARGIN_PCT_GRID:
            for global_cap in GLOBAL_CAP_GRID:
                for ob_cap in OB_ZONE_CAP_GRID:
                    ab.MAX_OPEN_TOTAL_GLOBAL = global_cap
                    ab.MAX_OPEN_PER_ZONE = {"OB": ob_cap, "BB": 1}
                    ab.MAX_TOTAL_MARGIN_PCT = margin_pct
                    for split in ["train", "validation", "holdout"]:
                        final, curve, closed = ab.simulate_tagged(combined[split], "per_tf")
                        s = stats(STARTING_BALANCE, final, curve, len(closed))
                        rows.append({"global_cap": global_cap, "ob_cap": ob_cap, "margin_pct": margin_pct,
                                     "split": split, **s})
                    print(f"{global_cap:>10} {ob_cap:>7} {margin_pct:>10.2f} "
                          f"{'(see csv)':<12} {'':>8} {'':>10} {'':>7} {'':>8} {'':>10}")
    finally:
        ab.MAX_OPEN_TOTAL_GLOBAL, ab.MAX_OPEN_PER_ZONE, ab.MAX_TOTAL_MARGIN_PCT = old_cap, old_zone_cap, old_margin

    import pandas as pd
    df = pd.DataFrame(rows)
    df.to_csv("/root/tyagach/results/sweep_position_caps_tyagach.csv", index=False)
    print("\nsaved -> results/sweep_position_caps_tyagach.csv")

    # Compact holdout-only view sorted by return, for the elbow read
    ho = df[df["split"] == "holdout"].sort_values("total_return_pct", ascending=False)
    print("\n=== HOLDOUT, sorted by return ===")
    print(ho[["global_cap", "ob_cap", "margin_pct", "n_closed", "total_return_pct", "max_dd_pct", "calmar"]].to_string(index=False))


if __name__ == "__main__":
    main()
