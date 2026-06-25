from __future__ import annotations
"""Unify OB/BB/MB into a common Zone shape for backtesting."""
from dataclasses import dataclass
from typing import Literal
from ob import OrderBlock
from bb import BreakerBlock
from mb import MitigationBlock


@dataclass
class Zone:
    kind: Literal["OB", "BB", "MB"]
    formed_idx: int  # candle that completes/confirms the zone
    valid_from: int  # first candle index the zone can be traded from
    direction: Literal["bullish", "bearish"]
    zone_low: float
    zone_high: float
    meta: dict


def build_zones(obs: list[OrderBlock], bbs: list[BreakerBlock], mbs: list[MitigationBlock]) -> list[Zone]:
    zones: list[Zone] = []
    for o in obs:
        zones.append(Zone("OB", o.confirm_idx, o.confirm_idx + 1, o.direction, o.zone_low, o.zone_high,
                           {"has_fvg_merge": o.has_fvg_merge, "swept_swing_idx": o.swept_swing_idx}))
    for b in bbs:
        zones.append(Zone("BB", b.break_idx, b.break_idx + 1, b.direction, b.zone_low, b.zone_high,
                           {"origin_ob_idx": b.origin_ob_idx, "mss_event_idx": b.mss_event_idx}))
    for m in mbs:
        zones.append(Zone("MB", m.idx, m.idx + 1, m.direction, m.zone_low, m.zone_high,
                           {"reinforced_by_ob": m.reinforced_by_ob, "broken_swing_idx": m.broken_swing_idx}))
    zones.sort(key=lambda z: z.valid_from)
    return zones
