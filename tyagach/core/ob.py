from __future__ import annotations
"""Order Block detector — exact 3-candle rule:
impulse (A) -> OB candle (B, sweeps SSL/BSL with this exact candle) ->
confirmation (C, body engulfs B's body, closes in reversal direction).
Zone = B's body only, UNLESS a FVG forms in the A-B-C triplet, in which
case the swept wick + FVG gap are merged into the zone."""
from dataclasses import dataclass
from typing import Literal
import pandas as pd
from structure import Swing, FVG


@dataclass
class OrderBlock:
    idx: int  # index of the OB candle (B)
    confirm_idx: int  # index of confirmation candle (C)
    direction: Literal["bullish", "bearish"]
    body_low: float
    body_high: float
    zone_low: float
    zone_high: float
    swept_swing_idx: int
    has_fvg_merge: bool


def detect_ob(df: pd.DataFrame, swings: list[Swing], fvgs: list[FVG]) -> list[OrderBlock]:
    opens = df["open"].values
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    n = len(df)

    highs_swings = sorted([s for s in swings if s.kind == "high"], key=lambda s: s.idx)
    lows_swings = sorted([s for s in swings if s.kind == "low"], key=lambda s: s.idx)
    fvg_by_idx = {f.idx: f for f in fvgs}

    def last_swing_before(swings_sorted: list[Swing], i: int) -> Swing | None:
        lo, hi = 0, len(swings_sorted) - 1
        res = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if swings_sorted[mid].idx < i:
                res = swings_sorted[mid]
                lo = mid + 1
            else:
                hi = mid - 1
        return res

    out: list[OrderBlock] = []

    for i in range(1, n - 1):
        a, b, c = i - 1, i, i + 1

        # --- bullish OB: B sweeps SSL (low below last swing low), impulse A bearish,
        # confirmation C bullish and engulfs B's body
        sw_low = last_swing_before(lows_swings, i)
        if sw_low is not None and lows[b] < sw_low.price:
            impulse_ok = closes[a] < opens[a]
            body_b_lo, body_b_hi = min(opens[b], closes[b]), max(opens[b], closes[b])
            c_bullish = closes[c] > opens[c]
            body_c_lo, body_c_hi = min(opens[c], closes[c]), max(opens[c], closes[c])
            engulf = body_c_lo <= body_b_lo and body_c_hi >= body_b_hi
            if impulse_ok and c_bullish and engulf:
                fvg = fvg_by_idx.get(c)
                has_fvg = fvg is not None and fvg.direction == "up"
                if has_fvg:
                    zone_lo = min(body_b_lo, fvg.gap_low, lows[b])
                    zone_hi = max(body_b_hi, fvg.gap_high)
                else:
                    zone_lo, zone_hi = body_b_lo, body_b_hi
                out.append(OrderBlock(b, c, "bullish", body_b_lo, body_b_hi, zone_lo, zone_hi, sw_low.idx, has_fvg))
                continue  # a candle can't be both bullish & bearish OB

        # --- bearish OB: B sweeps BSL (high above last swing high), impulse A bullish,
        # confirmation C bearish and engulfs B's body
        sw_high = last_swing_before(highs_swings, i)
        if sw_high is not None and highs[b] > sw_high.price:
            impulse_ok = closes[a] > opens[a]
            body_b_lo, body_b_hi = min(opens[b], closes[b]), max(opens[b], closes[b])
            c_bearish = closes[c] < opens[c]
            body_c_lo, body_c_hi = min(opens[c], closes[c]), max(opens[c], closes[c])
            engulf = body_c_lo <= body_b_lo and body_c_hi >= body_b_hi
            if impulse_ok and c_bearish and engulf:
                fvg = fvg_by_idx.get(c)
                has_fvg = fvg is not None and fvg.direction == "down"
                if has_fvg:
                    zone_lo = min(body_b_lo, fvg.gap_low)
                    zone_hi = max(body_b_hi, fvg.gap_high, highs[b])
                else:
                    zone_lo, zone_hi = body_b_lo, body_b_hi
                out.append(OrderBlock(b, c, "bearish", body_b_lo, body_b_hi, zone_lo, zone_hi, sw_high.idx, has_fvg))

    return out
