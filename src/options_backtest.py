from __future__ import annotations
"""At each validated zone signal (midpoint entry into OB/BB/BB/MB — the entry
variant that won the spot backtest), compare BUY-the-option vs SELL-the-option
expressed with the SAME directional view:

  bullish zone -> BUY call (long premium) vs SELL put (short premium)
  bearish zone -> BUY put  (long premium) vs SELL call (short premium)

Both struck ATM at signal time, same notional (1 contract), same exit rule:
whichever comes first of (spot hits the zone's R-target/stop) or (option
expiry). Pricing is synthetic Black-Scholes using trailing 7d realized vol
(spot options-chain history isn't available — same documented limitation as
the rest of the options project's backtests)."""
from dataclasses import dataclass
import numpy as np
import pandas as pd
import bs_pricer as bs
from zones import Zone

BUFFER_FRAC = 0.0015
MAX_LOOKAHEAD = 800
BARS_PER_DAY = 96
DAYS_PER_YEAR = 365.0


@dataclass
class OptionTrade:
    zone_kind: str
    direction: str
    r_target: float
    expiry_days: float
    entry_idx: int
    exit_idx: int
    exit_reason: str  # "tp", "sl", "expiry"
    spot_entry: float
    spot_exit: float
    sigma_entry: float
    strike: float
    buy_side: str
    buy_premium_paid: float
    buy_value_exit: float
    buy_pnl: float
    sell_side: str
    sell_premium_received: float
    sell_value_exit: float
    sell_pnl: float


def _find_midpoint_entry(o, h, l, c, zone: Zone, n: int):
    is_long = zone.direction == "bullish"
    zlo, zhi = zone.zone_low, zone.zone_high
    buf = BUFFER_FRAC * ((zlo + zhi) / 2)
    stop_price = (zlo - buf) if is_long else (zhi + buf)
    mid = (zlo + zhi) / 2
    start, end = zone.valid_from, min(n - 1, zone.valid_from + MAX_LOOKAHEAD)
    for i in range(start, end + 1):
        hi_, lo_, cl_ = h[i], l[i], c[i]
        if is_long and cl_ < stop_price:
            return None
        if (not is_long) and cl_ > stop_price:
            return None
        if (is_long and lo_ <= mid) or ((not is_long) and hi_ >= mid):
            return i, mid, stop_price
    return None


def run_options_backtest(df: pd.DataFrame, all_zones: list[Zone], sigma_series: np.ndarray,
                          r_targets: list[float], expiries_days: list[float]) -> list[OptionTrade]:
    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    trades: list[OptionTrade] = []

    for zone in all_zones:
        found = _find_midpoint_entry(o, h, l, c, zone, n)
        if found is None:
            continue
        entry_idx, entry_price, stop_price = found
        risk = abs(entry_price - stop_price)
        if risk <= 0:
            continue
        is_long = zone.direction == "bullish"
        sigma0 = sigma_series[entry_idx]
        strike = entry_price  # ATM at signal

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

                buy_side = "C" if is_long else "P"
                sell_side = "P" if is_long else "C"  # same directional bet, opposite leg

                buy_premium = bs.price(buy_side, entry_price, strike, T_entry, sigma0)
                buy_value_exit = bs.price(buy_side, spot_exit, strike, T_remaining, sigma0)
                buy_pnl = buy_value_exit - buy_premium

                sell_premium = bs.price(sell_side, entry_price, strike, T_entry, sigma0)
                sell_value_exit = bs.price(sell_side, spot_exit, strike, T_remaining, sigma0)
                sell_pnl = sell_premium - sell_value_exit

                trades.append(OptionTrade(
                    zone.kind, zone.direction, rt, expiry_days, entry_idx, exit_idx, exit_reason,
                    entry_price, spot_exit, sigma0, strike,
                    buy_side, buy_premium, buy_value_exit, buy_pnl,
                    sell_side, sell_premium, sell_value_exit, sell_pnl,
                ))

    return trades


def summarize(trades: list[OptionTrade]) -> pd.DataFrame:
    df = pd.DataFrame([t.__dict__ for t in trades])
    rows = []
    for (kind, rt, exp), g in df.groupby(["zone_kind", "r_target", "expiry_days"]):
        n = len(g)
        buy_win = (g["buy_pnl"] > 0).mean()
        sell_win = (g["sell_pnl"] > 0).mean()
        # normalize P&L as % of buy-premium so BUY and SELL P&L are comparable
        buy_ret_pct = (g["buy_pnl"] / g["buy_premium_paid"]).mean()
        sell_ret_pct = (g["sell_pnl"] / g["sell_premium_received"]).mean()
        rows.append({
            "zone_kind": kind, "r_target": rt, "expiry_days": exp, "n": n,
            "buy_win_rate": round(buy_win, 3), "buy_avg_pnl_$": round(g["buy_pnl"].mean(), 2),
            "buy_avg_ret_%": round(buy_ret_pct * 100, 1),
            "sell_win_rate": round(sell_win, 3), "sell_avg_pnl_$": round(g["sell_pnl"].mean(), 2),
            "sell_avg_ret_%": round(sell_ret_pct * 100, 1),
        })
    return pd.DataFrame(rows).sort_values(["zone_kind", "expiry_days", "r_target"])
