from __future__ import annotations
"""Breaker Block detector — a former OB of opposite polarity that gets
impulsively broken AFTER structure has already shifted (an MSS event in the
breakout's direction). Zone = full candle range (both wicks) of the ORIGINAL
OB candle. No FVG merge (unlike OB)."""
from dataclasses import dataclass
from typing import Literal
import pandas as pd
from structure import StructureEvent
from ob import OrderBlock


@dataclass
class BreakerBlock:
    origin_ob_idx: int  # idx of the original OB candle
    break_idx: int  # idx of the candle that impulsively broke through it
    direction: Literal["bullish", "bearish"]  # polarity AFTER the flip
    zone_low: float
    zone_high: float
    mss_event_idx: int


def detect_bb(df: pd.DataFrame, obs: list[OrderBlock], events: list[StructureEvent]) -> list[BreakerBlock]:
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)

    mss_up = sorted([e for e in events if e.kind == "MSS" and e.direction == "up"], key=lambda e: e.idx)
    mss_down = sorted([e for e in events if e.kind == "MSS" and e.direction == "down"], key=lambda e: e.idx)

    def first_mss_after(mss_list: list[StructureEvent], lo_idx: int) -> StructureEvent | None:
        for e in mss_list:
            if e.idx >= lo_idx:
                return e
        return None

    out: list[BreakerBlock] = []

    for ob in obs:
        if ob.direction == "bearish":
            # bearish OB = resistance zone; breaking UP through it after an MSS-up flips it bullish
            for j in range(ob.confirm_idx + 1, n):
                if closes[j] > ob.zone_high:
                    mss = first_mss_after(mss_up, ob.confirm_idx)
                    if mss is not None and ob.idx < mss.idx <= j:
                        out.append(BreakerBlock(ob.idx, j, "bullish", lows[ob.idx], highs[ob.idx], mss.idx))
                    break
        else:
            # bullish OB = support zone; breaking DOWN through it after an MSS-down flips it bearish
            for j in range(ob.confirm_idx + 1, n):
                if closes[j] < ob.zone_low:
                    mss = first_mss_after(mss_down, ob.confirm_idx)
                    if mss is not None and ob.idx < mss.idx <= j:
                        out.append(BreakerBlock(ob.idx, j, "bearish", lows[ob.idx], highs[ob.idx], mss.idx))
                    break

    return out
