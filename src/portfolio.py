from __future__ import annotations
"""Event-driven single-balance portfolio simulator across OB/MB/BB SELL
candidates. Priority order BB > MB > OB (structural strength) resolves:
  - same-direction stacking (skip new candidate if any open position,
    any zone, shares direction -> avoids doubling correlated exposure)
  - capacity (per-zone concurrency caps + a global cap), processed in
    priority order so BB/MB claim slots before OB at the same timestamp.
Position size = weight_pct[zone] * current balance (compounding), priced
in "contracts" = notional / spot_entry so it scales with the BS pnl-per-unit
already computed upstream."""
from dataclasses import dataclass, field

PRIORITY = {"BB": 0, "MB": 1, "OB": 2}  # lower = higher priority


@dataclass
class Candidate:
    zone_kind: str
    direction: str
    entry_idx: int
    exit_idx: int
    spot_entry: float
    pnl_per_unit: float  # $ pnl for 1 unit of underlying notional (from BS sell_pnl)


@dataclass
class PortfolioConfig:
    weight_pct: dict  # {"OB":0.1,"MB":0.15,"BB":0.2} fraction of CURRENT balance per new position
    max_open_per_zone: dict  # {"OB":3,"MB":2,"BB":1}
    max_open_total: int
    starting_balance: float = 10_000.0
    # realistic frictions, same convention as btc_straddle_dollar_account_sim.py:
    lot_size: float = 0.01       # min ETH options lot on Bybit
    margin_pct: float = 0.15     # initial margin as % of lot notional
    fee_rate: float = 0.0003     # 0.03% of notional per side
    fee_cap_pct: float = 0.125   # fee capped at 12.5% of premium per side


@dataclass
class OpenPosition:
    zone_kind: str
    direction: str
    exit_idx: int
    num_units: float  # n_lots * lot_size, scales pnl_per_unit to $
    pnl_per_unit: float
    entry_idx: int
    notional: float  # for fee calc at close


def _fee(notional: float, premium_total: float, fee_rate: float, fee_cap_pct: float) -> float:
    return min(notional * fee_rate, abs(premium_total) * fee_cap_pct)


def simulate(candidates: list[Candidate], cfg: PortfolioConfig):
    candidates_sorted = sorted(candidates, key=lambda c: (c.entry_idx, PRIORITY[c.zone_kind]))
    balance = cfg.starting_balance
    open_positions: list[OpenPosition] = []
    equity_curve = [(0, balance)]
    closed_trades = []  # (zone_kind, entry_idx, exit_idx, net_pnl_dollars)
    n_blocked_lot = 0

    def close_due(up_to_idx: int):
        nonlocal balance
        still_open = []
        for p in sorted(open_positions, key=lambda p: p.exit_idx):
            if p.exit_idx <= up_to_idx:
                gross_pnl = p.pnl_per_unit * p.num_units
                premium_total = abs(p.pnl_per_unit) * p.num_units  # rough premium proxy, same spirit as the straddle sim
                fees = 2 * _fee(p.notional, premium_total, cfg.fee_rate, cfg.fee_cap_pct)
                net_pnl = gross_pnl - fees
                balance += net_pnl
                closed_trades.append((p.zone_kind, p.entry_idx, p.exit_idx, net_pnl))
                equity_curve.append((p.exit_idx, balance))
            else:
                still_open.append(p)
        open_positions[:] = still_open

    for c in candidates_sorted:
        close_due(c.entry_idx)

        same_dir_conflict = any(p.direction == c.direction for p in open_positions)
        if same_dir_conflict:
            continue
        per_zone_count = sum(1 for p in open_positions if p.zone_kind == c.zone_kind)
        if per_zone_count >= cfg.max_open_per_zone.get(c.zone_kind, 0):
            continue
        if len(open_positions) >= cfg.max_open_total:
            continue
        if balance <= 0:
            continue

        budget = cfg.weight_pct.get(c.zone_kind, 0.0) * balance
        margin_per_lot = cfg.lot_size * c.spot_entry * cfg.margin_pct
        n_lots = int(budget // margin_per_lot) if margin_per_lot > 0 else 0
        if n_lots < 1:
            n_blocked_lot += 1
            continue
        num_units = n_lots * cfg.lot_size
        notional = num_units * c.spot_entry
        open_positions.append(OpenPosition(c.zone_kind, c.direction, c.exit_idx, num_units, c.pnl_per_unit, c.entry_idx, notional))

    if candidates_sorted:
        close_due(candidates_sorted[-1].exit_idx + 1)

    return balance, equity_curve, closed_trades, n_blocked_lot


def stats(starting_balance: float, final_balance: float, equity_curve: list[tuple[int, float]], n_trades: int) -> dict:
    if len(equity_curve) < 2:
        return {"final_balance": final_balance, "total_return_pct": 0.0, "max_dd_pct": 0.0,
                "calmar": 0.0, "n_closed": 0}
    peak = starting_balance
    max_dd = 0.0
    for _, bal in equity_curve:
        peak = max(peak, bal)
        dd = (peak - bal) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
    total_return_pct = (final_balance - starting_balance) / starting_balance * 100
    calmar = (total_return_pct / 100) / max_dd if max_dd > 1e-9 else float("inf")
    return {"final_balance": round(final_balance, 1), "total_return_pct": round(total_return_pct, 2),
            "max_dd_pct": round(max_dd * 100, 2), "calmar": round(calmar, 3), "n_closed": n_trades}
