from __future__ import annotations
"""Faithful current-live-config portfolio engine (2026-08-02), built to extend
ob_portfolio_compare.py (OB-only, per-cell tuned depth/r_target/expiry) with
the two pieces it deliberately excluded to isolate the OB question:
  - 15m/BB (live ACTIVE_CELLS includes it, ZONE_CONFIG defaults, untouched
    since no BB depth sweep has been done yet — see TYAGACH_IMPROVEMENT_PLAN.md P5)
  - ENTRY_VETO_UTC_HOURS (P1, shipped 3254f7d): entries whose entry_ts falls
    in 12-15h UTC are dropped entirely (consumed, not deferred) — matches
    services/config.py's live veto semantics exactly.

Fidelity check (before trusting this for new conclusions): with
ENTRY_VETO=empty and BB excluded, this script's build_all() must reproduce
ob_portfolio_compare.py's CANDIDATE row exactly. Verified 2026-08-02 (see
session notes) — do not skip this if the engine changes again.

Purpose: get the TRUE current-live-matching baseline (5 active cells + veto),
as the base for MAX_OPEN_TOTAL_GLOBAL / MAX_TOTAL_MARGIN_PCT sweeps (mirrors
the Jony fleet's 2026-08-02 position-cap headroom finding: research/sweep_position_caps.py).
"""
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/Users/sabar/Desktop/smc_options/src")
import structure
from portfolio import PRIORITY, stats
import tyagach_portfolio_multitf as base
from tyagach_portfolio_multitf import TF_FILES, STARTING_BALANCE
from ob_portfolio_compare import build_candidates_for_tf as build_ob, CANDIDATE_CFG

ENTRY_VETO_UTC_HOURS = frozenset({12, 13, 14, 15})  # live default, services/config.py

# live ACTIVE_CELLS as of aa8315e-era Tyagach config.py (2026-07-09 review + 07-07 override)
LIVE_ACTIVE_CELLS = {("15m", "OB"), ("15m", "BB"), ("30m", "OB"), ("1h", "OB"), ("2h", "OB")}


def in_veto_hour(ts_ms: int) -> bool:
    hour = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).hour
    return hour in ENTRY_VETO_UTC_HOURS


def build_all(cut1_ts: int, cut2_ts: int, apply_veto: bool, include_bb: bool):
    """Returns {split: [(tf, Candidate), ...]} across live cells."""
    combined = {"train": [], "validation": [], "holdout": []}

    # OB, per-cell tuned params (live CELL_CONFIG)
    for tf in ["15m", "30m", "1h", "2h"]:
        by_split = build_ob(tf, CANDIDATE_CFG[tf], cut1_ts, cut2_ts)
        for split, cands in by_split.items():
            for c in cands:
                if apply_veto and in_veto_hour(c.entry_idx):  # entry_idx is absolute ts_ms here
                    continue
                combined[split].append((tf, c))

    # BB, ZONE_CONFIG defaults (module default ACTIVE_CELLS already includes ("15m","BB"))
    if include_bb:
        base.ACTIVE_CELLS = {("15m", "BB")}
        by_split_15m = base.build_candidates_for_tf("15m", TF_FILES["15m"], cut1_ts, cut2_ts)
        for split, cands in by_split_15m.items():
            for c in cands:
                if c.zone_kind != "BB":
                    continue
                if apply_veto and in_veto_hour(c.entry_idx):
                    continue
                combined[split].append(("15m", c))

    return combined


def run_and_print(label: str, combined: dict, span_days: float, simulate_tagged_fn,
                   scope: str = "per_tf", max_open_total=None, max_total_margin_pct=None):
    import tyagach_samedir_ab as ab
    old_cap, old_margin = ab.MAX_OPEN_TOTAL_GLOBAL, ab.MAX_TOTAL_MARGIN_PCT
    if max_open_total is not None:
        ab.MAX_OPEN_TOTAL_GLOBAL = max_open_total
    if max_total_margin_pct is not None:
        ab.MAX_TOTAL_MARGIN_PCT = max_total_margin_pct
    try:
        split_days = {"train": span_days * 0.6, "validation": span_days * 0.2, "holdout": span_days * 0.2}
        rows = []
        for split in ["train", "validation", "holdout"]:
            final, curve, closed = simulate_tagged_fn(combined[split], scope)
            s = stats(STARTING_BALANCE, final, curve, len(closed))
            tpd = len(closed) / split_days[split]
            rows.append((split, s["n_closed"], tpd, s["total_return_pct"], s["max_dd_pct"], s["calmar"], s["final_balance"]))
            print(f"{label:<22} {split:<12} {s['n_closed']:>8} {tpd:>10.2f} "
                  f"{s['total_return_pct']:>+10.1f} {s['max_dd_pct']:>7.1f} "
                  f"{s['calmar']:>8.2f} {s['final_balance']:>10.1f}")
        return rows
    finally:
        ab.MAX_OPEN_TOTAL_GLOBAL, ab.MAX_TOTAL_MARGIN_PCT = old_cap, old_margin


def main():
    df15 = structure.load_csv(TF_FILES["15m"])
    n15 = len(df15)
    ts15 = df15["ts_ms"].values
    cut1_ts, cut2_ts = int(ts15[int(n15 * 0.6)]), int(ts15[int(n15 * 0.8)])
    span_days = (int(ts15[-1]) - int(ts15[0])) / 1000 / 86400

    import tyagach_samedir_ab as ab

    print(f"{'label':<22} {'split':<12} {'n_closed':>8} {'trades/day':>10} {'return%':>10} "
          f"{'maxDD%':>7} {'calmar':>8} {'final$':>10}")
    print("-" * 92)

    # Fidelity check: no veto, OB-only -> must equal ob_portfolio_compare.py's CANDIDATE row
    combined_fidelity = build_all(cut1_ts, cut2_ts, apply_veto=False, include_bb=False)
    run_and_print("FIDELITY(OB-only,noveto)", combined_fidelity, span_days, ab.simulate_tagged)
    print()

    # True live baseline: OB (tuned) + BB + veto
    combined_live = build_all(cut1_ts, cut2_ts, apply_veto=True, include_bb=True)
    run_and_print("LIVE_BASELINE", combined_live, span_days, ab.simulate_tagged)
    print()

    return combined_live, span_days, ab.simulate_tagged


if __name__ == "__main__":
    main()
