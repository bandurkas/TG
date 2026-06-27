"""Mark-to-market enrichment for open Tyagach positions — adds
current_mark_usd/unrealized_pnl_usd the same way the other 3 bots
(Sniper1/Boba1/Grogu1) do in opt-app's backend/services/paper_loop.py
(current_mark_or_bs): prefer the live markPrice, fall back to bid/ask mid;
PnL = (entry_credit_per_unit - mark) * num_units for a short option (premium
falling = profit).

Tyagach had none of this — every open position shipped to the dashboard with
no live mark at all (2026-06-27 finding). Quote fetching is injected via
`quote_fn` so this stays testable without a real Bybit client/credentials.
"""
from __future__ import annotations

import time
from typing import Callable

_QUOTE_CACHE: dict[str, tuple[float, dict]] = {}
_QUOTE_CACHE_TTL_S = 8.0  # under the dashboard's 15s poll interval, so quotes
# stay fresh, but back-to-back requests within the same window don't multiply
# Bybit get_tickers calls — Tyagach has no existing rate-limit backoff.


def cached_quote(get_quote: Callable[[str], dict | None], symbol: str) -> dict | None:
    now = time.time()
    cached = _QUOTE_CACHE.get(symbol)
    if cached is not None and now - cached[0] < _QUOTE_CACHE_TTL_S:
        return cached[1]
    quote = get_quote(symbol)
    if quote is not None:
        _QUOTE_CACHE[symbol] = (now, quote)
    return quote


def enrich_positions_with_mark(rows: list[dict], quote_fn: Callable[[str], dict | None]) -> list[dict]:
    for r in rows:
        r.setdefault("current_mark_usd", None)
        r.setdefault("unrealized_pnl_usd", None)
        if r.get("status") != "open":
            continue
        num_units = r.get("num_units") or 0
        if num_units <= 0:
            continue
        quote = quote_fn(r["symbol"])
        if quote is None:
            continue
        mark = quote.get("mark") or 0.0
        if mark <= 0:
            bid, ask = quote.get("bid") or 0.0, quote.get("ask") or 0.0
            mark = (bid + ask) / 2.0 if bid > 0 and ask > 0 else None
        if mark is None:
            continue
        entry_credit_per_unit = r["sell_premium_received"] / num_units
        r["current_mark_usd"] = mark
        r["unrealized_pnl_usd"] = (entry_credit_per_unit - mark) * num_units
    return rows
