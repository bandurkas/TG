from __future__ import annotations
"""Mitigation Block detector — impulsive break of swing high/low that is a
BOS (trend continuation, opposite-side structure NOT broken), not an MSS.
Zone = both wicks of the breakout candle. Presence of an OB inside the zone
is optional but flagged as reinforcement."""
from dataclasses import dataclass
from typing import Literal
import pandas as pd
from structure import StructureEvent
from ob import OrderBlock


@dataclass
class MitigationBlock:
    idx: int  # breakout candle idx
    direction: Literal["bullish", "bearish"]
    zone_low: float
    zone_high: float
    broken_swing_idx: int
    reinforced_by_ob: bool


def detect_mb(df: pd.DataFrame, events: list[StructureEvent], obs: list[OrderBlock]) -> list[MitigationBlock]:
    highs = df["high"].values
    lows = df["low"].values

    out: list[MitigationBlock] = []
    for e in events:
        if e.kind != "BOS":
            continue
        direction = "bullish" if e.direction == "up" else "bearish"
        zone_low, zone_high = lows[e.idx], highs[e.idx]

        reinforced = any(
            ob.direction == direction
            and e.broken_swing_idx <= ob.idx <= e.idx
            and not (ob.zone_high < zone_low or ob.zone_low > zone_high)
            for ob in obs
        )

        out.append(MitigationBlock(e.idx, direction, zone_low, zone_high, e.broken_swing_idx, reinforced))

    return out
