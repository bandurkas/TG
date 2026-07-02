"""Grid search weight_pct / max_open_per_zone / max_open_total for the CURRENT
multi-TF 6-cell Tyagach config. The live values (WEIGHT_PCT/MAX_OPEN_PER_ZONE/
MAX_OPEN_TOTAL_GLOBAL in tyagach/services/config.py) were carried over from the
original single-TF (15m-only) sizing search and never re-tuned for the 6-cell
multi-TF candidate set validated in tyagach_portfolio_multitf.py.

Rank on TRAIN (return subject to a maxDD ceiling, like sweep_portfolio.py),
confirm top candidates on VALIDATION + HOLDOUT with zero further tuning, and
apply the 2026-07-03 amendment's own bar: only trust a config if positive on
all three splits.

Usage: python3 tyagach_portfolio_gridsearch.py
"""
from __future__ import annotations
import itertools
import multiprocessing as mp
import os
import pandas as pd

from tyagach_portfolio_multitf import (
    TF_FILES, ACTIVE_CELLS, build_candidates_for_tf, structure,
)
from portfolio import PortfolioConfig, simulate, stats

STARTING_BALANCE = 2000.0
LOT_SIZE = 0.10
MARGIN_PCT = 0.15
FEE_RATE = 0.0003
FEE_CAP_PCT = 0.125

# live values, kept as the center of the grid
WEIGHT_GRID = {
    "OB": [0.06, 0.09, 0.12, 0.16, 0.20],
    "MB": [0.10, 0.14, 0.18, 0.24, 0.30],
    "BB": [0.16, 0.22, 0.28, 0.36, 0.45],
}
CAP_GRID = {
    "OB": [1, 2, 3],
    "MB": [1, 2, 3],
    "BB": [1, 2],
}
GLOBAL_CAP_GRID = [4, 6, 8, 10, 12]
MAX_DD_CONSTRAINT_PCT = 30.0
MIN_TRADES_TRAIN = 200  # 6-cell multi-TF has much higher volume than single-TF


def _eval_one(args):
    candidates, ow, mw, bw, oc, mc, bc, gc = args
    cfg = PortfolioConfig(
        weight_pct={"OB": ow, "MB": mw, "BB": bw},
        max_open_per_zone={"OB": oc, "MB": mc, "BB": bc},
        max_open_total=gc,
        starting_balance=STARTING_BALANCE, lot_size=LOT_SIZE, margin_pct=MARGIN_PCT,
        fee_rate=FEE_RATE, fee_cap_pct=FEE_CAP_PCT,
    )
    final_balance, equity_curve, closed, n_blocked = simulate(candidates, cfg)
    s = stats(cfg.starting_balance, final_balance, equity_curve, len(closed))
    s.update({"ob_w": ow, "mb_w": mw, "bb_w": bw, "ob_cap": oc, "mb_cap": mc, "bb_cap": bc, "global_cap": gc})
    return s


def grid_search(candidates, n_workers: int) -> pd.DataFrame:
    combos = list(itertools.product(
        WEIGHT_GRID["OB"], WEIGHT_GRID["MB"], WEIGHT_GRID["BB"],
        CAP_GRID["OB"], CAP_GRID["MB"], CAP_GRID["BB"],
        GLOBAL_CAP_GRID,
    ))
    args = [(candidates, *c) for c in combos]
    print(f"  grid combos: {len(args)}")
    with mp.Pool(n_workers) as pool:
        results = pool.map(_eval_one, args, chunksize=100)
    return pd.DataFrame(results)


def confirm(cfg_row, candidates_by_split: dict) -> pd.DataFrame:
    cfg = PortfolioConfig(
        weight_pct={"OB": cfg_row["ob_w"], "MB": cfg_row["mb_w"], "BB": cfg_row["bb_w"]},
        max_open_per_zone={"OB": cfg_row["ob_cap"], "MB": cfg_row["mb_cap"], "BB": cfg_row["bb_cap"]},
        max_open_total=cfg_row["global_cap"],
        starting_balance=STARTING_BALANCE, lot_size=LOT_SIZE, margin_pct=MARGIN_PCT,
        fee_rate=FEE_RATE, fee_cap_pct=FEE_CAP_PCT,
    )
    rows = []
    for split_name, cands in candidates_by_split.items():
        final_balance, equity_curve, closed, n_blocked = simulate(sorted(cands, key=lambda c: c.entry_idx), cfg)
        s = stats(cfg.starting_balance, final_balance, equity_curve, len(closed))
        s["split"] = split_name
        rows.append(s)
    return pd.DataFrame(rows)


def main():
    df15 = structure.load_csv(TF_FILES["15m"])
    n15 = len(df15)
    ts15 = df15["ts_ms"].values
    cut1_ts = int(ts15[int(n15 * 0.6)])
    cut2_ts = int(ts15[int(n15 * 0.8)])

    combined = {"train": [], "validation": [], "holdout": []}
    for tf, path in TF_FILES.items():
        if not any(t == tf for (t, k) in ACTIVE_CELLS):
            continue
        by_split = build_candidates_for_tf(tf, path, cut1_ts, cut2_ts)
        for k in combined:
            combined[k].extend(by_split[k])
    for k in combined:
        combined[k] = sorted(combined[k], key=lambda c: c.entry_idx)
    print({k: len(v) for k, v in combined.items()})

    n_workers = max(1, (os.cpu_count() or 4) - 1)
    print(f"\nRunning grid search on TRAIN ({n_workers} workers)...")
    train_results = grid_search(combined["train"], n_workers)
    train_results.to_csv("../results/tyagach_gridsearch_train.csv", index=False)

    valid = train_results[(train_results["n_closed"] >= MIN_TRADES_TRAIN)
                           & (train_results["max_dd_pct"] <= MAX_DD_CONSTRAINT_PCT)]
    top10 = valid.sort_values("total_return_pct", ascending=False).head(10)
    print(f"\n=== TOP 10 on TRAIN (max total_return_pct, dd<={MAX_DD_CONSTRAINT_PCT}%, n>={MIN_TRADES_TRAIN}) ===")
    print(top10.to_string(index=False))

    print("\n=== Confirming TOP 10 on VALIDATION + HOLDOUT ===")
    all_confirms = []
    for rank, (_, row) in enumerate(top10.iterrows()):
        cdf = confirm(row, combined)
        cdf["train_rank"] = rank
        for k in ["ob_w", "mb_w", "bb_w", "ob_cap", "mb_cap", "bb_cap", "global_cap"]:
            cdf[k] = row[k]
        all_confirms.append(cdf)
    confirm_all = pd.concat(all_confirms, ignore_index=True)
    confirm_all.to_csv("../results/tyagach_gridsearch_confirmed.csv", index=False)
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    print(confirm_all.to_string(index=False))

    # apply the 2026-07-03 amendment bar: positive return on ALL 3 splits
    print("\n=== Candidates passing ALL-3-splits-positive, ranked by holdout return ===")
    robust = []
    for rank in confirm_all["train_rank"].unique():
        g = confirm_all[confirm_all["train_rank"] == rank].set_index("split")
        if (g["total_return_pct"] > 0).all():
            robust.append((rank, g))
    robust.sort(key=lambda t: t[1].loc["holdout", "total_return_pct"], reverse=True)
    for rank, g in robust:
        row0 = g.iloc[0]
        print(f"\nrank#{rank}: OB(w={row0['ob_w']},cap={row0['ob_cap']}) "
              f"MB(w={row0['mb_w']},cap={row0['mb_cap']}) BB(w={row0['bb_w']},cap={row0['bb_cap']}) "
              f"global_cap={row0['global_cap']}")
        print(g[["total_return_pct", "max_dd_pct", "calmar", "n_closed"]].to_string())


if __name__ == "__main__":
    main()
