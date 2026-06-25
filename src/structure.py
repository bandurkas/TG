from __future__ import annotations
"""Market structure primitives: swing points, HH/HL/LH/LL labeling,
BOS (continuation break) / MSS (reversal break) events, and FVG detection.
Pure pandas, index-based (integer positions), timeframe-agnostic."""
from dataclasses import dataclass
from typing import Literal
import pandas as pd
import numpy as np


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms")
    df = df.sort_values("ts_ms").reset_index(drop=True)
    return df[["ts_ms", "ts", "open", "high", "low", "close", "volume"]]


@dataclass
class Swing:
    idx: int
    price: float
    kind: Literal["high", "low"]
    label: str = ""  # HH/HL/LH/LL, filled in by label_structure


def detect_swings(df: pd.DataFrame, order: int = 3) -> list[Swing]:
    """Fractal swing points: a high strictly greater than `order` candles on
    each side, a low strictly less than `order` candles on each side."""
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    swings: list[Swing] = []
    for i in range(order, n - order):
        window_h = highs[i - order : i + order + 1]
        if highs[i] == window_h.max() and np.argmax(window_h) == order:
            swings.append(Swing(i, highs[i], "high"))
        window_l = lows[i - order : i + order + 1]
        if lows[i] == window_l.min() and np.argmin(window_l) == order:
            swings.append(Swing(i, lows[i], "low"))
    swings.sort(key=lambda s: s.idx)
    return swings


@dataclass
class StructureEvent:
    idx: int  # candle index where the break CLOSE occurred
    direction: Literal["up", "down"]
    kind: Literal["BOS", "MSS"]
    broken_swing_idx: int
    broken_price: float


def label_and_track(df: pd.DataFrame, swings: list[Swing]) -> tuple[list[Swing], list[StructureEvent]]:
    """Walk swings in time order, label HH/HL/LH/LL, and walk candle closes
    to detect BOS (continuation, trend-aligned break) vs MSS (reversal,
    counter-trend break) events against the *last confirmed* opposite-kind
    swing extreme.

    Trend state: 'up' once we've seen a HH followed by HL pattern forming,
    'down' once LH followed by LL. Starts 'unknown' until the first
    classified swing pair.
    """
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]

    last_high_price = None
    for s in highs:
        if last_high_price is None:
            s.label = "H"
        else:
            s.label = "HH" if s.price > last_high_price else "LH"
        last_high_price = s.price

    last_low_price = None
    for s in lows:
        if last_low_price is None:
            s.label = "L"
        else:
            s.label = "HL" if s.price > last_low_price else "LL"
        last_low_price = s.price

    closes = df["close"].values
    n = len(df)

    trend: str | None = None
    last_swing_high: Swing | None = None
    last_swing_low: Swing | None = None
    broken_high_ids: set[int] = set()
    broken_low_ids: set[int] = set()
    events: list[StructureEvent] = []

    all_swings_sorted = sorted(swings, key=lambda s: s.idx)
    swing_ptr = 0

    for i in range(n):
        while swing_ptr < len(all_swings_sorted) and all_swings_sorted[swing_ptr].idx <= i:
            s = all_swings_sorted[swing_ptr]
            if s.kind == "high":
                last_swing_high = s
            else:
                last_swing_low = s
            swing_ptr += 1

        if last_swing_high is not None and last_swing_high.idx not in broken_high_ids:
            if closes[i] > last_swing_high.price:
                broken_high_ids.add(last_swing_high.idx)
                if trend == "down":
                    kind = "MSS"
                else:
                    kind = "BOS"
                events.append(StructureEvent(i, "up", kind, last_swing_high.idx, last_swing_high.price))
                trend = "up"

        if last_swing_low is not None and last_swing_low.idx not in broken_low_ids:
            if closes[i] < last_swing_low.price:
                broken_low_ids.add(last_swing_low.idx)
                if trend == "up":
                    kind = "MSS"
                else:
                    kind = "BOS"
                events.append(StructureEvent(i, "down", kind, last_swing_low.idx, last_swing_low.price))
                trend = "down"

    events.sort(key=lambda e: e.idx)
    return swings, events


@dataclass
class FVG:
    idx: int  # index of the 3rd candle (the one that confirms the gap)
    direction: Literal["up", "down"]
    gap_low: float
    gap_high: float


def detect_fvg(df: pd.DataFrame) -> list[FVG]:
    """3-candle imbalance: bullish if candle[i-2].high < candle[i].low,
    bearish if candle[i-2].low > candle[i].high."""
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    out = []
    for i in range(2, n):
        if highs[i - 2] < lows[i]:
            out.append(FVG(i, "up", highs[i - 2], lows[i]))
        elif lows[i - 2] > highs[i]:
            out.append(FVG(i, "down", highs[i], lows[i - 2]))
    return out
