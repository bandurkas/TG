"""Online adaptation of smc_options/src/portfolio.py's conflict-resolution +
sizing rules. The backtest version (`simulate()`) knows every candidate's
exit_idx in advance because it's replaying history; live, candidates and
exits arrive one tick at a time, so this module re-implements the same RULES
(priority order, same-direction skip, per-zone/global caps, weight_pct
sizing) against the actual open-positions table instead of a precomputed
event list."""
from __future__ import annotations

from dataclasses import dataclass

from . import config
from .signal_engine import TriggeredEntry


@dataclass
class EntryDecision:
    entry: TriggeredEntry
    option_side: str       # 'C' or 'P' to SELL
    strike: float
    n_lots: int
    num_units: float
    notional: float
    margin_required: float
    iv_entry: float


def filter_by_iv(entries: list[TriggeredEntry], current_dvol: float | None) -> list[TriggeredEntry]:
    if current_dvol is None:
        return []
    out = []
    for e in entries:
        threshold = config.ZONE_CONFIG[e.kind]["iv_threshold"]
        if current_dvol > threshold:
            out.append(e)
    return out


def rank_by_priority(entries: list[TriggeredEntry]) -> list[TriggeredEntry]:
    return sorted(entries, key=lambda e: (e.entry_ts_ms, config.PRIORITY[e.kind]))


def decide_entries(entries: list[TriggeredEntry], balance: float, open_positions: list[dict],
                    current_dvol: float) -> list[EntryDecision]:
    """Applies conflict rules in priority order against a SNAPSHOT of
    open_positions (the caller is responsible for actually opening each
    approved entry and re-querying open_positions before the next batch, so
    two approved-in-the-same-tick entries can't both claim the same global
    slot — see loop.py)."""
    decisions: list[EntryDecision] = []
    sim_open = list(open_positions)  # local copy we provisionally append to as we approve

    for e in rank_by_priority(entries):
        same_dir_conflict = any(p["direction"] == e.direction for p in sim_open)
        if same_dir_conflict:
            continue
        per_zone_count = sum(1 for p in sim_open if p["zone_kind"] == e.kind)
        if per_zone_count >= config.MAX_OPEN_PER_ZONE.get(e.kind, 0):
            continue
        if len(sim_open) >= config.MAX_OPEN_TOTAL:
            continue
        if balance <= 0:
            continue

        is_long = e.direction == "bullish"
        option_side = "P" if is_long else "C"  # sell put for bullish zone, call for bearish
        strike = e.entry_price  # ATM at signal

        budget = config.WEIGHT_PCT.get(e.kind, 0.0) * balance
        margin_per_lot = config.LOT_SIZE * e.entry_price * config.MARGIN_PCT
        n_lots = int(budget // margin_per_lot) if margin_per_lot > 0 else 0
        if n_lots < 1:
            continue
        num_units = n_lots * config.LOT_SIZE
        notional = num_units * e.entry_price
        margin_required = n_lots * margin_per_lot

        decisions.append(EntryDecision(e, option_side, strike, n_lots, num_units, notional, margin_required,
                                        current_dvol))
        sim_open.append({"direction": e.direction, "zone_kind": e.kind})  # reserve the slot for the next candidate

    return decisions


@dataclass
class ExitDecision:
    position: dict
    exit_reason: str  # tp / sl / expiry


def check_exits(open_positions: list[dict], latest_high: float, latest_low: float, now_ts_ms: int) -> list[ExitDecision]:
    out = []
    for p in open_positions:
        is_long = p["direction"] == "bullish"
        hit_sl = (latest_low <= p["stop_price"]) if is_long else (latest_high >= p["stop_price"])
        hit_tp = (latest_high >= p["tp_price"]) if is_long else (latest_low <= p["tp_price"])
        expired = now_ts_ms >= p["expiry_ts_ms"]
        if hit_sl:
            out.append(ExitDecision(p, "sl"))
        elif hit_tp:
            out.append(ExitDecision(p, "tp"))
        elif expired:
            out.append(ExitDecision(p, "expiry"))
    return out


def fee(notional: float, premium_total: float) -> float:
    return min(notional * config.FEE_RATE, abs(premium_total) * config.FEE_CAP_PCT)
