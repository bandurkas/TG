from __future__ import annotations
"""Same buy-vs-sell-at-midpoint-signal test as options_backtest.py, but:
  - sigma comes from REAL Deribit ETH DVOL (forward-filled to 15m), not a
    realized-vol proxy.
  - BUY is only taken when entry IV < BUY_IV_MAX (cheap premium, long-vol bias).
  - SELL is only taken when entry IV > SELL_IV_MIN (rich premium, short-vol bias).
A given signal can qualify for BUY, for SELL, for both (impossible here since
the bands don't overlap), or for neither (skipped for that side)."""
from dataclasses import dataclass
import numpy as np
import pandas as pd
import bs_pricer as bs
from zones import Zone
from options_backtest import _find_midpoint_entry, BARS_PER_DAY, DAYS_PER_YEAR

BUY_IV_MAX = 0.50
SELL_IV_MIN = 0.70


@dataclass
class RealIVTrade:
    zone_kind: str
    direction: str
    side: str  # "buy" or "sell"
    r_target: float
    expiry_days: float
    entry_idx: int
    exit_idx: int
    exit_reason: str
    iv_entry: float
    spot_entry: float
    spot_exit: float
    strike: float
    option_side: str
    premium_entry: float
    value_exit: float
    pnl: float
    ret_pct: float


def run(df: pd.DataFrame, all_zones: list[Zone], iv_series: np.ndarray,
        r_targets: list[float], expiries_days: list[float]) -> list[RealIVTrade]:
    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    trades: list[RealIVTrade] = []

    for zone in all_zones:
        found = _find_midpoint_entry(o, h, l, c, zone, n)
        if found is None:
            continue
        entry_idx, entry_price, stop_price = found
        iv0 = iv_series[entry_idx]
        if np.isnan(iv0):
            continue  # before DVOL history starts
        risk = abs(entry_price - stop_price)
        if risk <= 0:
            continue
        is_long = zone.direction == "bullish"
        strike = entry_price

        qualifies_buy = iv0 < BUY_IV_MAX
        qualifies_sell = iv0 > SELL_IV_MIN
        if not qualifies_buy and not qualifies_sell:
            continue

        for expiry_days in expiries_days:
            expiry_bars = int(expiry_days * BARS_PER_DAY)
            expiry_idx = min(n - 1, entry_idx + expiry_bars)

            for rt in r_targets:
                tp_price = entry_price + rt * risk if is_long else entry_price - rt * risk
                exit_idx, exit_reason = expiry_idx, "expiry"
                for j in range(entry_idx + 1, expiry_idx + 1):
                    hit_sl = (l[j] <= stop_price) if is_long else (h[j] >= stop_price)
                    hit_tp = (h[j] >= tp_price) if is_long else (l[j] <= tp_price)
                    if hit_sl and hit_tp:
                        exit_idx, exit_reason = j, "sl"
                        break
                    if hit_sl:
                        exit_idx, exit_reason = j, "sl"
                        break
                    if hit_tp:
                        exit_idx, exit_reason = j, "tp"
                        break

                spot_exit = c[exit_idx]
                elapsed_days = (exit_idx - entry_idx) / BARS_PER_DAY
                T_remaining = max(0.0, (expiry_days - elapsed_days) / DAYS_PER_YEAR)
                T_entry = expiry_days / DAYS_PER_YEAR

                if qualifies_buy:
                    side = "C" if is_long else "P"
                    premium = bs.price(side, entry_price, strike, T_entry, iv0)
                    value_exit = bs.price(side, spot_exit, strike, T_remaining, iv0)
                    pnl = value_exit - premium
                    trades.append(RealIVTrade(zone.kind, zone.direction, "buy", rt, expiry_days,
                                               entry_idx, exit_idx, exit_reason, iv0, entry_price,
                                               spot_exit, strike, side, premium, value_exit, pnl,
                                               pnl / premium if premium > 0 else np.nan))

                if qualifies_sell:
                    side = "P" if is_long else "C"
                    premium = bs.price(side, entry_price, strike, T_entry, iv0)
                    value_exit = bs.price(side, spot_exit, strike, T_remaining, iv0)
                    pnl = premium - value_exit
                    trades.append(RealIVTrade(zone.kind, zone.direction, "sell", rt, expiry_days,
                                               entry_idx, exit_idx, exit_reason, iv0, entry_price,
                                               spot_exit, strike, side, premium, value_exit, pnl,
                                               pnl / premium if premium > 0 else np.nan))

    return trades


def summarize(trades: list[RealIVTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    df = pd.DataFrame([t.__dict__ for t in trades])
    rows = []
    for (kind, side, rt, exp), g in df.groupby(["zone_kind", "side", "r_target", "expiry_days"]):
        rows.append({
            "zone_kind": kind, "side": side, "r_target": rt, "expiry_days": exp, "n": len(g),
            "win_rate": round((g["pnl"] > 0).mean(), 3),
            "avg_pnl_$": round(g["pnl"].mean(), 2),
            "avg_ret_%": round(g["ret_pct"].mean() * 100, 1),
            "avg_iv": round(g["iv_entry"].mean() * 100, 1),
        })
    return pd.DataFrame(rows).sort_values(["zone_kind", "side", "expiry_days", "r_target"])
