from __future__ import annotations
"""Portfolio-layer grid search: fixed per-zone signal configs (already
validated individually), sweep position-sizing weights + concurrency caps
across an 8-core pool, rank on TRAIN by Calmar (return/maxDD), confirm
top candidates on VALIDATION + HOLDOUT with zero further tuning."""
import sys
import itertools
import multiprocessing as mp
import numpy as np
import pandas as pd
import structure
import ob
import bb
import mb
from zones import build_zones
from dvol import load_dvol_aligned
from sweep_sell import find_entries, simulate_exit, sell_pnl
from portfolio import Candidate, PortfolioConfig, simulate, stats

# fixed, individually-validated zone configs (R_target, expiry_days, iv_threshold)
ZONE_CONFIG = {
    "BB": (2.5, 5.0, 60),
    "MB": (3.0, 0.5, 70),
    "OB": (3.0, 0.5, 60),
}

WEIGHT_GRID = {
    "OB": [0.10, 0.20, 0.35, 0.50, 0.70],
    "MB": [0.15, 0.30, 0.50, 0.70, 0.95],
    "BB": [0.20, 0.40, 0.65, 0.90, 1.20],
}
CAP_GRID = {
    "OB": [2, 3, 4],
    "MB": [1, 2, 3],
    "BB": [1, 2],
}
GLOBAL_CAP_GRID = [3, 4, 6, 8]
MAX_DD_CONSTRAINT_PCT = 30.0  # "stable" = don't let drawdown exceed this


def build_candidates(sdf, entries) -> list[Candidate]:
    out = []
    for kind, (rt, exp, thr) in ZONE_CONFIG.items():
        ents = [e for e in entries if e.zone_kind == kind and e.iv0 * 100 > thr]
        for e in ents:
            exit_idx, spot_exit = simulate_exit(sdf, e, rt, exp)
            pnl = sell_pnl(e, exit_idx, spot_exit, exp)
            out.append(Candidate(kind, e.direction, e.entry_idx, exit_idx, e.entry_price, pnl))
    return out


def _eval_one(args):
    candidates, ow, mw, bw, oc, mc, bc, gc = args
    cfg = PortfolioConfig(
        weight_pct={"OB": ow, "MB": mw, "BB": bw},
        max_open_per_zone={"OB": oc, "MB": mc, "BB": bc},
        max_open_total=gc,
    )
    final_balance, equity_curve, closed, n_blocked = simulate(candidates, cfg)
    s = stats(cfg.starting_balance, final_balance, equity_curve, len(closed))
    s.update({"ob_w": ow, "mb_w": mw, "bb_w": bw, "ob_cap": oc, "mb_cap": mc, "bb_cap": bc, "global_cap": gc})
    return s


def grid_search(candidates: list[Candidate], n_workers: int = 8) -> pd.DataFrame:
    combos = list(itertools.product(
        WEIGHT_GRID["OB"], WEIGHT_GRID["MB"], WEIGHT_GRID["BB"],
        CAP_GRID["OB"], CAP_GRID["MB"], CAP_GRID["BB"],
        GLOBAL_CAP_GRID,
    ))
    args = [(candidates, *c) for c in combos]
    print(f"  grid combos: {len(args)}")
    with mp.Pool(n_workers) as pool:
        results = pool.map(_eval_one, args, chunksize=50)
    return pd.DataFrame(results)


def confirm(cfg_row, candidates_by_split: dict) -> pd.DataFrame:
    cfg = PortfolioConfig(
        weight_pct={"OB": cfg_row["ob_w"], "MB": cfg_row["mb_w"], "BB": cfg_row["bb_w"]},
        max_open_per_zone={"OB": cfg_row["ob_cap"], "MB": cfg_row["mb_cap"], "BB": cfg_row["bb_cap"]},
        max_open_total=cfg_row["global_cap"],
    )
    rows = []
    for split_name, cands in candidates_by_split.items():
        final_balance, equity_curve, closed, n_blocked = simulate(cands, cfg)
        s = stats(cfg.starting_balance, final_balance, equity_curve, len(closed))
        s["split"] = split_name
        rows.append(s)
    return pd.DataFrame(rows)


def main(tf_csv: str, dvol_json: str):
    df = structure.load_csv(tf_csv)
    n = len(df)
    cut1, cut2 = int(n * 0.6), int(n * 0.8)
    splits = {"train": df.iloc[:cut1].reset_index(drop=True),
              "validation": df.iloc[cut1:cut2].reset_index(drop=True),
              "holdout": df.iloc[cut2:].reset_index(drop=True)}

    candidates_by_split = {}
    for name, sdf in splits.items():
        swings = structure.detect_swings(sdf, order=3)
        swings, events = structure.label_and_track(sdf, swings)
        fvgs = structure.detect_fvg(sdf)
        obs = ob.detect_ob(sdf, swings, fvgs)
        bbs = bb.detect_bb(sdf, obs, events)
        mbs = mb.detect_mb(sdf, events, obs)
        zones = build_zones(obs, bbs, mbs)
        iv_series = load_dvol_aligned(dvol_json, sdf)
        entries = find_entries(sdf, zones, iv_series)
        candidates = build_candidates(sdf, entries)
        print(f"[{name}] candidates: {len(candidates)} "
              f"(OB={sum(1 for c in candidates if c.zone_kind=='OB')}, "
              f"MB={sum(1 for c in candidates if c.zone_kind=='MB')}, "
              f"BB={sum(1 for c in candidates if c.zone_kind=='BB')})")
        candidates_by_split[name] = candidates

    print("\nRunning grid search on TRAIN...")
    train_results = grid_search(candidates_by_split["train"], n_workers=8)
    train_results.to_csv("../results/portfolio_sweep_train.csv", index=False)

    # "max profitable AND stable": maximize return subject to a drawdown ceiling,
    # not raw Calmar (which degenerates to trivially-tiny position sizes)
    valid = train_results[(train_results["n_closed"] >= 50) & (train_results["max_dd_pct"] <= MAX_DD_CONSTRAINT_PCT)]
    top10 = valid.sort_values(["total_return_pct"], ascending=False).head(10)
    print(f"\n=== TOP 10 on TRAIN (max total_return_pct, dd<={MAX_DD_CONSTRAINT_PCT}%) ===")
    print(top10.to_string(index=False))

    print("\n=== Confirming TOP 10 on VALIDATION + HOLDOUT ===")
    all_confirms = []
    for rank, (_, row) in enumerate(top10.iterrows()):
        cdf = confirm(row, candidates_by_split)
        cdf["train_rank"] = rank
        for k in ["ob_w", "mb_w", "bb_w", "ob_cap", "mb_cap", "bb_cap", "global_cap"]:
            cdf[k] = row[k]
        all_confirms.append(cdf)
    confirm_all = pd.concat(all_confirms, ignore_index=True)
    confirm_all.to_csv("../results/portfolio_top10_confirmed.csv", index=False)
    print(confirm_all.to_string(index=False))

    return candidates_by_split, train_results, confirm_all


if __name__ == "__main__":
    tf_csv = sys.argv[1] if len(sys.argv) > 1 else "../data/eth_15m.csv"
    dvol_json = sys.argv[2] if len(sys.argv) > 2 else "../data/eth_dvol_1h.json"
    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 200)
    pd.set_option("display.max_columns", 30)
    main(tf_csv, dvol_json)
