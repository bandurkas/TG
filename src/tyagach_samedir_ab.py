"""A/B: global same-direction blocking (research portfolio.py behavior, what
the 2026-07-03 portfolio validation implicitly assumed) vs per-TF sub-book
blocking (what the LIVE bot actually does since the 07-02 multi-TF upgrade).

Motivation (2026-07-05): three live positions (15m/1h/2h, all bullish MB,
all short ~1775 puts) were open simultaneously and hit SL together for
-$27.9 — the exact correlated-exposure stack portfolio.py's global
same-direction rule exists to prevent. The live decide_entries only blocks
same-direction WITHIN a TF sub-book, so the validated backtest and live
diverge on this rule. Quantify which is better before changing anything.

Usage: python3 tyagach_samedir_ab.py
"""
from __future__ import annotations
import sys

sys.path.insert(0, "/Users/sabar/Desktop/smc_options/src")
from tyagach_portfolio_multitf import (
    TF_FILES, ACTIVE_CELLS, WEIGHT_PCT, MAX_OPEN_PER_ZONE, MAX_OPEN_TOTAL_GLOBAL,
    LOT_SIZE, MARGIN_PCT, FEE_RATE, FEE_CAP_PCT, STARTING_BALANCE,
    build_candidates_for_tf, structure,
)
from portfolio import PRIORITY, stats

MAX_TOTAL_MARGIN_PCT = 0.60  # live config.py's global margin ceiling


def simulate_tagged(tagged, same_dir_scope: str):
    """Mirror of portfolio.simulate() but candidates carry their TF, and the
    same-direction conflict check is either 'global' (research behavior) or
    'per_tf' (live bot behavior). Per-zone caps are always per-TF (live
    behavior; in the 07-03 validation the difference never bound because the
    global same-dir rule fires first). Also models live's MAX_TOTAL_MARGIN_PCT,
    which portfolio.py doesn't have."""
    order = sorted(tagged, key=lambda tc: (tc[1].entry_idx, PRIORITY[tc[1].zone_kind]))
    balance = STARTING_BALANCE
    open_positions = []  # (tf, kind, direction, exit_idx, num_units, pnl_per_unit, notional)
    equity_curve = [(0, balance)]
    closed = []

    def fee(notional, premium_total):
        return min(notional * FEE_RATE, abs(premium_total) * FEE_CAP_PCT)

    def close_due(up_to_idx):
        nonlocal balance
        still = []
        for p in sorted(open_positions, key=lambda p: p[3]):
            if p[3] <= up_to_idx:
                tf, kind, direction, exit_idx, num_units, ppu, notional = p
                gross = ppu * num_units
                premium_total = abs(ppu) * num_units
                net = gross - 2 * fee(notional, premium_total)
                balance += net
                closed.append(net)
                equity_curve.append((exit_idx, balance))
            else:
                still.append(p)
        open_positions[:] = still

    for tf, c in order:
        close_due(c.entry_idx)

        if same_dir_scope == "global":
            conflict = any(p[2] == c.direction for p in open_positions)
        else:  # per_tf
            conflict = any(p[0] == tf and p[2] == c.direction for p in open_positions)
        if conflict:
            continue
        per_zone = sum(1 for p in open_positions if p[0] == tf and p[1] == c.zone_kind)
        if per_zone >= MAX_OPEN_PER_ZONE.get(c.zone_kind, 0):
            continue
        if len(open_positions) >= MAX_OPEN_TOTAL_GLOBAL:
            continue
        if balance <= 0:
            continue

        budget = WEIGHT_PCT.get(c.zone_kind, 0.0) * balance
        margin_per_lot = LOT_SIZE * c.spot_entry * MARGIN_PCT
        n_lots = int(budget // margin_per_lot) if margin_per_lot > 0 else 0
        if n_lots < 1:
            continue
        num_units = n_lots * LOT_SIZE
        notional = num_units * c.spot_entry
        margin_required = n_lots * margin_per_lot
        total_margin = sum(p[6] * MARGIN_PCT for p in open_positions)
        if total_margin + margin_required > balance * MAX_TOTAL_MARGIN_PCT:
            continue

        open_positions.append((tf, c.zone_kind, c.direction, c.exit_idx,
                               num_units, c.pnl_per_unit, notional))

    if order:
        close_due(order[-1][1].exit_idx + 1)
    return balance, equity_curve, closed


def main():
    df15 = structure.load_csv(TF_FILES["15m"])
    n15 = len(df15)
    ts15 = df15["ts_ms"].values
    cut1_ts, cut2_ts = int(ts15[int(n15 * 0.6)]), int(ts15[int(n15 * 0.8)])
    span_days = (int(ts15[-1]) - int(ts15[0])) / 1000 / 86400
    split_days = {"train": span_days * 0.6, "validation": span_days * 0.2, "holdout": span_days * 0.2}

    combined = {"train": [], "validation": [], "holdout": []}
    for tf, path in TF_FILES.items():
        if not any(t == tf for (t, k) in ACTIVE_CELLS):
            continue
        by_split = build_candidates_for_tf(tf, path, cut1_ts, cut2_ts)
        for split in combined:
            combined[split].extend((tf, c) for c in by_split[split])

    print(f"{'scope':<8} {'split':<12} {'n_closed':>8} {'trades/day':>10} {'return%':>10} "
          f"{'maxDD%':>7} {'calmar':>8} {'final$':>10}")
    print("-" * 80)
    for scope in ["global", "per_tf"]:
        for split in ["train", "validation", "holdout"]:
            final, curve, closed = simulate_tagged(combined[split], scope)
            s = stats(STARTING_BALANCE, final, curve, len(closed))
            tpd = len(closed) / split_days[split]
            print(f"{scope:<8} {split:<12} {s['n_closed']:>8} {tpd:>10.2f} "
                  f"{s['total_return_pct']:>+10.1f} {s['max_dd_pct']:>7.1f} "
                  f"{s['calmar']:>8.2f} {s['final_balance']:>10.1f}")
        print()


if __name__ == "__main__":
    main()
