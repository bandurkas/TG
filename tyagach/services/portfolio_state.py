"""Online adaptation of smc_options/src/portfolio.py's conflict-resolution +
sizing rules, extended to per-TF sub-books.

Architecture (A) — per-TF sub-books:
  - same-direction conflict and per-zone caps are evaluated WITHIN a single TF.
    A 2h-OB bullish position does NOT block a 15m-OB bullish — they live in
    separate validated books.
  - Sizing draws from the single shared balance (weight_pct * balance).
  - A global ceiling (MAX_OPEN_TOTAL_GLOBAL + MAX_TOTAL_MARGIN_PCT) prevents
    all TFs firing simultaneously from over-leveraging the account.

The backtest version (`simulate()`) knows every candidate's exit_idx in
advance; live, candidates and exits arrive one tick at a time, so this module
re-implements the same RULES against the actual open-positions table."""
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


def decide_entries(
    entries: list[TriggeredEntry],
    balance: float,
    tf_open_positions: list[dict],    # positions open in THIS TF's sub-book
    all_open_positions: list[dict],   # ALL open positions (for global ceiling)
    current_dvol: float,
) -> list[EntryDecision]:
    """Applies conflict rules in priority order.

    Per-TF rules (use tf_open_positions):
      - same-direction skip within the TF
      - per-zone concurrency cap within the TF

    Global ceiling (use all_open_positions + sim_all):
      - MAX_OPEN_TOTAL_GLOBAL: hard slot cap across all TFs
      - MAX_TOTAL_MARGIN_PCT: total open margin ≤ X% of balance

    The caller is responsible for re-querying positions after each actual
    open so two approved-same-tick entries can't both claim the same slot."""
    decisions: list[EntryDecision] = []
    sim_tf = list(tf_open_positions)    # provisional TF-scoped book
    sim_all = list(all_open_positions)  # provisional global book

    # Pre-compute current total margin from all already-open positions
    current_total_margin = sum(
        p.get("notional", 0.0) * config.MARGIN_PCT for p in all_open_positions
    )

    for e in rank_by_priority(entries):
        # -- per-TF conflict rules --
        same_dir_conflict = any(p["direction"] == e.direction for p in sim_tf)
        if same_dir_conflict:
            continue
        per_zone_count = sum(1 for p in sim_tf if p["zone_kind"] == e.kind)
        if per_zone_count >= config.MAX_OPEN_PER_ZONE.get(e.kind, 0):
            continue

        # -- global ceiling --
        if len(sim_all) >= config.MAX_OPEN_TOTAL_GLOBAL:
            continue
        if balance <= 0:
            continue

        # Size the candidate
        is_long = e.direction == "bullish"
        option_side = "P" if is_long else "C"
        strike = e.entry_price

        budget = config.WEIGHT_PCT.get(e.kind, 0.0) * balance
        margin_per_lot = config.LOT_SIZE * e.entry_price * config.MARGIN_PCT
        n_lots = int(budget // margin_per_lot) if margin_per_lot > 0 else 0
        if n_lots < 1:
            continue
        num_units = n_lots * config.LOT_SIZE
        notional = num_units * e.entry_price
        margin_required = n_lots * margin_per_lot

        # Check that adding this position stays under the total margin cap
        if (current_total_margin + margin_required) > balance * config.MAX_TOTAL_MARGIN_PCT:
            continue

        decisions.append(EntryDecision(e, option_side, strike, n_lots, num_units, notional,
                                        margin_required, current_dvol))
        # Reserve slot in both scoped and global books for the next candidate
        sim_tf.append({"direction": e.direction, "zone_kind": e.kind})
        sim_all.append({"direction": e.direction, "zone_kind": e.kind})
        current_total_margin += margin_required

    return decisions


@dataclass
class ExitDecision:
    position: dict
    exit_reason: str  # tp / sl / expiry


def check_exits(open_positions: list[dict], latest_high: float, latest_low: float,
                now_ts_ms: int) -> list[ExitDecision]:
    """Check SL/TP/expiry for a list of positions against a single bar's high/low.
    Caller passes only the positions that belong to the TF whose bar just closed
    (per-TF exit fidelity) plus any positions expired by wall-clock."""
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


def check_expiry_only(open_positions: list[dict], now_ts_ms: int) -> list[ExitDecision]:
    """Wall-clock expiry sweep regardless of TF bar cadence — called once per
    loop tick for ALL open positions so nothing is held past instrument expiry."""
    return [ExitDecision(p, "expiry") for p in open_positions if now_ts_ms >= p["expiry_ts_ms"]]


def fee(notional: float, premium_total: float) -> float:
    return min(notional * config.FEE_RATE, abs(premium_total) * config.FEE_CAP_PCT)
